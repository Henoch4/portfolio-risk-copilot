"""Behavioral tests for TradingVault.sol on a real EVM (eth-tester).

The repo's contract toolchain (hardhat + OpenZeppelin) is not installable on
this machine (no network), so the vault follows the repo's standalone-contract
convention (zero imports, like TradeAuditTrail.sol) compiled with py-solc-x and
exercised on the eth-tester EVM.

Invariants under test:
- MIN_DEPOSIT / MAX_TVL are enforced INSIDE deposit(), not stored inertly
  (the liquid-protocol-v1 lesson).
- Donation attack resistance (virtual shares): a raw transfer to the vault
  cannot inflate the share price.
- Two-step withdrawal: request (priced before burn) -> finalize -> expire.
- Settlement-window-only redemption: finalize reverts while a funding-arb
  package is open.
- Operator-attested NAV: attestTotalAssets is timelocked and agent-only.
"""
import ast
import re

import pytest
from eth_utils import keccak
from pathlib import Path
from solcx import compile_source
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

SOLC_VERSION = "0.8.29"

_MIN_DEPOSIT = 100_000          # 0.1 USDT (6 decimals)
_MAX_TVL = 10_000_000_000_000   # 10M USDT
_ATTEST_TIMELOCK = 3600         # 1 hour
_RATE_LIMIT = 3600                       # mirrors WITHDRAWAL_RATE_LIMIT
_DEADLINE = 3 * 24 * 3600               # mirrors WITHDRAWAL_DEADLINE

MOCK_USDT_SRC = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract MockUSDT {
    string public constant name = "Mock USDT";
    string public constant symbol = "USDT";
    uint8 public constant decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            require(allowed >= amount, "allowance exceeded");
            allowance[from][msg.sender] = allowed - amount;
        }
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}
"""


@pytest.fixture(scope="session")
def compiled():
    vault_path = Path(__file__).resolve().parent.parent / "contracts" / "contracts" / "TradingVault.sol"
    vault_src = vault_path.read_text(encoding="utf-8")

    vault = compile_source(vault_src, solc_version=SOLC_VERSION,
                           output_values=["abi", "bin"])
    usdt = compile_source(MOCK_USDT_SRC, solc_version=SOLC_VERSION,
                          output_values=["abi", "bin"])

    def _pick(result, name):
        data = result.get(f"<stdin>:{name}")
        if data is None:
            raise KeyError(name)
        return data["abi"], data["bin"]

    vault_abi, vault_bin = _pick(vault, "TradingVault")
    usdt_abi, usdt_bin = _pick(usdt, "MockUSDT")
    return {
        "vault_abi": vault_abi,
        "vault_bin": "0x" + vault_bin,
        "usdt_abi": usdt_abi,
        "usdt_bin": "0x" + usdt_bin,
    }


@pytest.fixture()
def w3():
    provider = EthereumTesterProvider()
    w3 = Web3(provider)
    assert w3.is_connected()
    return w3


def _deploy(w3, abi, bytecode, *args):
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx_hash = contract.constructor(*args).transact()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return w3.eth.contract(address=receipt["contractAddress"], abi=abi)


def _travel(w3, delta_seconds):
    """Advance the eth-tester clock by delta_seconds."""
    tester = w3.provider.ethereum_tester
    latest = tester.get_block_by_number("latest")
    tester.time_travel(latest["timestamp"] + delta_seconds)
    tester.mine_blocks()


@pytest.fixture()
def vault_factory(w3, compiled):
    """Deploy MockUSDT once; returns a function that deploys a fresh vault."""
    usdt = _deploy(w3, compiled["usdt_abi"], compiled["usdt_bin"])
    agent = w3.eth.accounts[1]

    def factory(min_deposit=_MIN_DEPOSIT, max_tvl=_MAX_TVL, attest_timelock=_ATTEST_TIMELOCK):
        vault = _deploy(w3, compiled["vault_abi"], compiled["vault_bin"],
                        usdt.address, agent, min_deposit, max_tvl, attest_timelock)
        return {
            "w3": w3,
            "usdt": usdt,
            "vault": vault,
            "owner": w3.eth.accounts[0],
            "agent": agent,
            "alice": w3.eth.accounts[2],
            "mallory": w3.eth.accounts[3],
        }

    return factory


@pytest.fixture()
def env(vault_factory):
    return vault_factory()


def _seed(env, who, amount):
    env["usdt"].functions.mint(who, amount).transact({"from": env["owner"]})
    env["usdt"].functions.approve(env["vault"].address, amount).transact({"from": who})


def _deposit(env, who, amount):
    return env["vault"].functions.deposit(amount).transact({"from": who})


def _expect_revert(name, fn):
    """eth-tester returns custom-error reverts as the raw 4-byte selector;
    decode it and assert it matches the expected error name."""
    selector = keccak(text=f"{name}()")[:4]
    with pytest.raises(Exception) as ei:
        fn()
    match = re.search(r"execution reverted: (b'.*'|0x[0-9a-fA-F]{8})", str(ei.value))
    assert match is not None, f"no revert data in: {str(ei.value)!r}"
    try:
        data = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        data = bytes.fromhex(match.group(1)[2:])
    assert data == selector, \
        f"expected {name} ({selector.hex()}), got {data.hex()} from: {str(ei.value)}"


def test_deposit_mints_shares_one_to_one(env):
    _seed(env, env["alice"], 1_000_000)
    assert env["vault"].functions.convertToShares(1_000_000).call() == 1_000_000

    _deposit(env, env["alice"], 1_000_000)

    assert env["vault"].functions.balanceOf(env["alice"]).call() == 1_000_000
    assert env["vault"].functions.totalAssets().call() == 1_000_000
    assert env["vault"].functions.sharePriceAsset().call() == 10 ** 18


def test_deposit_below_min_deposit_reverts(env):
    _seed(env, env["alice"], _MIN_DEPOSIT - 1)
    _expect_revert("DepositTooSmall", lambda: _deposit(env, env["alice"], _MIN_DEPOSIT - 1))


def test_deposit_above_max_tvl_reverts(env):
    _seed(env, env["alice"], _MAX_TVL + 1)
    _expect_revert("MaxTvlExceeded", lambda: _deposit(env, env["alice"], _MAX_TVL + 1))


def test_donation_attack_resistance(vault_factory):
    """A raw USDT transfer to the vault must not inflate the share price."""
    env = vault_factory(min_deposit=0)  # first depositor deposits 1 minimal unit
    _seed(env, env["mallory"], 1)
    _deposit(env, env["mallory"], 1)

    # Mallory donates a large amount directly to the vault contract.
    env["usdt"].functions.mint(env["vault"].address, 1_000_000_000).transact({"from": env["owner"]})

    # A new depositor at the same NAV must receive fair shares.
    _seed(env, env["alice"], 1_000_000)
    assert env["vault"].functions.convertToShares(1_000_000).call() == 1_000_000
    assert env["vault"].functions.sharePriceAsset().call() == 10 ** 18


def test_share_price_rises_with_attested_nav(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    env["vault"].functions.attestTotalAssets(2_000_000).transact({"from": env["agent"]})

    assert env["vault"].functions.totalAssets().call() == 2_000_000
    assert env["vault"].functions.convertToAssets(1_000_000).call() == 1_500_000


def test_two_step_withdrawal_happy_path(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    req = env["vault"].functions.withdrawalRequests(req_id).call()
    assert req[2] == env["alice"]
    assert req[1] == 500_000  # usdtOut reserved at request-time price

    env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]})

    assert env["usdt"].functions.balanceOf(env["alice"]).call() == 500_000
    assert env["vault"].functions.balanceOf(env["alice"]).call() == 500_000
    assert env["vault"].functions.totalAssets().call() == 500_000


def test_pending_withdrawal_does_not_underprice_new_deposits(env):
    """Regression: a request in flight (shares burned, value reserved) must not
    inflate the share price and shorten a subsequent depositor."""
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    assert env["vault"].functions.pendingReserved().call() == 500_000

    # While the request is pending, a new deposit is still priced 1:1.
    _seed(env, env["mallory"], 100_000)
    assert env["vault"].functions.convertToShares(100_000).call() == 100_000
    _deposit(env, env["mallory"], 100_000)
    assert env["vault"].functions.balanceOf(env["mallory"]).call() == 100_000

    # Finalize the pending request; mallory keeps full (not inflated) value.
    env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]})
    assert env["vault"].functions.pendingReserved().call() == 0
    assert env["vault"].functions.convertToAssets(env["vault"].functions.balanceOf(env["mallory"]).call()).call() == 100_000


def test_second_request_prices_against_live_pool(env):
    _seed(env, env["alice"], 1_000_000)
    _seed(env, env["mallory"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    _deposit(env, env["mallory"], 1_000_000)

    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    # Alice's 500k reserve is excluded; mallory's 500k-share request prices 1:1.
    assert env["vault"].functions.convertToAssets(500_000).call() == 500_000
    req2 = env["vault"].functions.requestWithdraw(500_000).call({"from": env["mallory"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["mallory"]})
    assert env["vault"].functions.pendingReserved().call() == 1_000_000
    assert env["vault"].functions.withdrawalRequests(req2).call()[1] == 500_000


def test_request_withdraw_rate_limited(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    env["vault"].functions.requestWithdraw(100_000).transact({"from": env["alice"]})
    _expect_revert("WithdrawalRateLimited",
                   lambda: env["vault"].functions.requestWithdraw(100_000).transact({"from": env["alice"]}))

    _travel(env["w3"], _RATE_LIMIT + 1)
    env["vault"].functions.requestWithdraw(100_000).transact({"from": env["alice"]})


def test_withdrawal_deadline_expiry_restores_shares(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    _travel(env["w3"], _DEADLINE + 1)

    _expect_revert("WithdrawalDeadlinePassed",
                   lambda: env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]}))

    env["vault"].functions.expireWithdrawal(req_id).transact({"from": env["owner"]})
    assert env["vault"].functions.balanceOf(env["alice"]).call() == 1_000_000


def test_finalize_blocked_while_package_open(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    env["vault"].functions.setFundingPackageOpen(True).transact({"from": env["agent"]})
    assert env["vault"].functions.settlementOpen().call() is False
    _expect_revert("SettlementWindowClosed",
                   lambda: env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]}))

    env["vault"].functions.setFundingPackageOpen(False).transact({"from": env["agent"]})
    env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]})
    assert env["usdt"].functions.balanceOf(env["alice"]).call() == 500_000


def test_attest_timelocked(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    env["vault"].functions.attestTotalAssets(2_000_000).transact({"from": env["agent"]})
    _expect_revert("AttestationTimelocked",
                   lambda: env["vault"].functions.attestTotalAssets(3_000_000).transact({"from": env["agent"]}))

    _travel(env["w3"], _ATTEST_TIMELOCK + 1)
    env["vault"].functions.attestTotalAssets(3_000_000).transact({"from": env["agent"]})
    assert env["vault"].functions.totalAssets().call() == 3_000_000


def test_attest_capped_by_max_tvl(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    _expect_revert("MaxTvlExceeded",
                   lambda: env["vault"].functions.attestTotalAssets(_MAX_TVL + 1).transact({"from": env["agent"]}))


def test_only_agent_can_attest_and_move(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    _expect_revert("OnlyAgent",
                   lambda: env["vault"].functions.attestTotalAssets(2_000_000).transact({"from": env["alice"]}))
    _expect_revert("OnlyAgent",
                   lambda: env["vault"].functions.setFundingPackageOpen(True).transact({"from": env["alice"]}))
    _expect_revert("OnlyAgent",
                   lambda: env["vault"].functions.transferToAgent(10_000).transact({"from": env["alice"]}))


def test_agent_transfers_reserve_without_changing_nav(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    env["vault"].functions.transferToAgent(400_000).transact({"from": env["agent"]})

    # Money moved to the agent, NAV unchanged until the next attestation.
    assert env["usdt"].functions.balanceOf(env["agent"]).call() == 400_000
    assert env["vault"].functions.totalAssets().call() == 1_000_000


def test_attest_below_reserved_reverts(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})
    _expect_revert("ReservedExceedsNav",
                   lambda: env["vault"].functions.attestTotalAssets(400_000).transact({"from": env["agent"]}))


def test_transfer_to_agent_keeps_reserve_covered(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    # 500k is reserved for the pending request; the agent can move at most the rest.
    _expect_revert("ReservedExceedsBalance",
                   lambda: env["vault"].functions.transferToAgent(500_001).transact({"from": env["agent"]}))
    env["vault"].functions.transferToAgent(500_000).transact({"from": env["agent"]})
    assert env["usdt"].functions.balanceOf(env["agent"]).call() == 500_000


def test_withdrawal_request_gates(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)

    _expect_revert("InvalidAmount",
                   lambda: env["vault"].functions.requestWithdraw(0).transact({"from": env["alice"]}))
    _expect_revert("InvalidAmount",
                   lambda: env["vault"].functions.requestWithdraw(1_000_001).transact({"from": env["alice"]}))


def test_finalize_gates(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    _expect_revert("NotWithdrawalOwner",
                   lambda: env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["mallory"]}))

    env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]})
    _expect_revert("AlreadyFinalized",
                   lambda: env["vault"].functions.finalizeWithdraw(req_id).transact({"from": env["alice"]}))


def test_expire_gates(env):
    _seed(env, env["alice"], 1_000_000)
    _deposit(env, env["alice"], 1_000_000)
    req_id = env["vault"].functions.requestWithdraw(500_000).call({"from": env["alice"]})
    env["vault"].functions.requestWithdraw(500_000).transact({"from": env["alice"]})

    # Only owner can expire.
    _expect_revert("OnlyOwner",
                   lambda: env["vault"].functions.expireWithdrawal(req_id).transact({"from": env["alice"]}))
    # Too early to expire.
    _expect_revert("WithdrawalNotExpired",
                   lambda: env["vault"].functions.expireWithdrawal(req_id).transact({"from": env["owner"]}))

    _travel(env["w3"], _DEADLINE + 1)
    # Unknown request id reverts with NothingToExpire.
    _expect_revert("NothingToExpire",
                   lambda: env["vault"].functions.expireWithdrawal(9_999).transact({"from": env["owner"]}))


def test_deposit_zero_reverts(env):
    _seed(env, env["alice"], 1_000_000)
    _expect_revert("DepositTooSmall",
                   lambda: env["vault"].functions.deposit(0).transact({"from": env["alice"]}))


def test_eth_not_accepted(env):
    _expect_revert("ETHNotAccepted",
                   lambda: env["w3"].eth.send_transaction({
                       "from": env["alice"],
                       "to": env["vault"].address,
                       "value": 1,
                   }))