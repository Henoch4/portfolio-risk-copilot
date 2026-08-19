"""End-to-end integration test: deposit -> attest -> reconcile -> withdraw -> finalize.

Exercises the full vault lifecycle on a local EVM, then verifies the
off-chain reconciliation service agrees with the on-chain state.
"""
import pytest
from pathlib import Path
from solcx import compile_source
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

SOLC_VERSION = "0.8.29"

_MIN_DEPOSIT = 100_000
_MAX_TVL = 10_000_000_000_000
_ATTEST_TIMELOCK = 3600
_MAX_ATTESTATION_DELTA_BPS = 1000

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
    vault = compile_source(vault_src, solc_version=SOLC_VERSION, output_values=["abi", "bin"])
    usdt = compile_source(MOCK_USDT_SRC, solc_version=SOLC_VERSION, output_values=["abi", "bin"])

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
    return Web3(EthereumTesterProvider())


def _deploy(w3, abi, bytecode, *args):
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor(*args).transact()
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    return w3.eth.contract(address=receipt["contractAddress"], abi=abi)


def _travel(w3, delta):
    tester = w3.provider.ethereum_tester
    latest = tester.get_block_by_number("latest")
    tester.time_travel(latest["timestamp"] + delta)
    tester.mine_blocks()


def _seed(env, who, amount):
    env["usdt"].functions.mint(who, amount).transact({"from": env["owner"]})
    env["usdt"].functions.approve(env["vault"].address, amount).transact({"from": who})


@pytest.fixture()
def env(w3, compiled):
    usdt = _deploy(w3, compiled["usdt_abi"], compiled["usdt_bin"])
    agent = w3.eth.accounts[1]
    vault = _deploy(w3, compiled["vault_abi"], compiled["vault_bin"],
                    usdt.address, agent, _MIN_DEPOSIT, _MAX_TVL,
                    _ATTEST_TIMELOCK, _MAX_ATTESTATION_DELTA_BPS)
    return {
        "w3": w3,
        "usdt": usdt,
        "vault": vault,
        "owner": w3.eth.accounts[0],
        "agent": agent,
        "alice": w3.eth.accounts[2],
        "bob": w3.eth.accounts[3],
    }


class TestDepositAttestReconcileWithdraw:
    """Full lifecycle: deposit -> attest -> reconcile -> withdraw -> finalize."""

    def test_full_lifecycle(self, env):
        w3, usdt, vault = env["w3"], env["usdt"], env["vault"]
        owner, agent, alice, bob = env["owner"], env["agent"], env["alice"], env["bob"]

        # --- 1. Initial state ---
        assert vault.functions.totalAssets().call() == 0
        assert vault.functions.totalSupply().call() == 0

        # --- 2. Alice deposits 500 USDT ---
        _seed(env, alice, 500_000_000)
        vault.functions.deposit(500_000_000).transact({"from": alice})
        assert vault.functions.totalAssets().call() == 500_000_000
        alice_shares = vault.functions.balanceOf(alice).call()
        assert alice_shares > 0

        # --- 3. Bob deposits 300 USDT ---
        _seed(env, bob, 300_000_000)
        vault.functions.deposit(300_000_000).transact({"from": bob})
        assert vault.functions.totalAssets().call() == 800_000_000

        # --- 4. Agent attests NAV = 800 USDT (matches deposits) ---
        vault.functions.attestTotalAssets(800_000_000).transact({"from": agent})
        assert vault.functions.totalAssets().call() == 800_000_000

        # --- 5. Off-chain reconciliation: OKX reports 800 USDT ---
        from src.reconciliation import reconcile
        vault_state = {
            "deployed": True,
            "total_assets": vault.functions.totalAssets().call(),
            "total_supply": vault.functions.totalSupply().call(),
            "pending_reserved": vault.functions.pendingReserved().call(),
            "last_attestation": vault.functions.lastAttestation().call(),
            "attest_timelock": vault.functions.ATTEST_TIMELOCK().call(),
            "max_attestation_delta_bps": vault.functions.MAX_ATTESTATION_DELTA_BPS().call(),
            "total_assets_priced": vault.functions.totalAssetsPriced().call(),
        }
        okx_data = {"usdt_eq": 800.0, "total_eq": 800.0}
        result = reconcile(vault_state, okx_data, asset_decimals=6)
        assert result.discrepancy_usdt == pytest.approx(0.0, abs=0.01)
        assert result.suggested_attestation == 800_000_000

        # --- 6. Agent gains 50 USDT on OKX; NAV drifts ---
        okx_data_after = {"usdt_eq": 850.0, "total_eq": 850.0}
        result2 = reconcile(vault_state, okx_data_after, asset_decimals=6)
        assert result2.discrepancy_usdt == pytest.approx(-50.0, abs=0.01)
        assert result2.suggested_attestation == 850_000_000

        # --- 7. Agent attests new NAV = 850 USDT (within 10% cap) ---
        _travel(w3, _ATTEST_TIMELOCK + 1)
        vault.functions.attestTotalAssets(850_000_000).transact({"from": agent})
        assert vault.functions.totalAssets().call() == 850_000_000

        # --- 8. Alice requests withdrawal of half her shares ---
        half_shares = alice_shares // 2
        tx = vault.functions.requestWithdraw(half_shares).transact({"from": alice})
        receipt = w3.eth.get_transaction_receipt(tx)
        alice_before = usdt.functions.balanceOf(alice).call()

        # Withdrawal is pending: shares burned, USDT locked
        assert vault.functions.balanceOf(alice).call() < alice_shares
        assert vault.functions.pendingReserved().call() > 0

        # --- 9. Agent opens settlement window ---
        vault.functions.setFundingPackageOpen(True).transact({"from": agent})
        assert vault.functions.fundingPackageOpen().call() is True

        # --- 10. Alice cannot finalize while settlement window is open ---
        from tests.test_trading_vault import _expect_revert
        _expect_revert("SettlementWindowClosed",
                       lambda: vault.functions.finalizeWithdraw(1).transact({"from": alice}))

        # --- 11. Agent closes settlement window ---
        vault.functions.setFundingPackageOpen(False).transact({"from": agent})

        # --- 12. Alice finalizes withdrawal ---
        vault.functions.finalizeWithdraw(1).transact({"from": alice})
        alice_after = usdt.functions.balanceOf(alice).call()
        assert alice_after > alice_before

        # --- 13. Agent transfers 100 USDT to itself (provisioning) ---
        cap = vault.functions.AGENT_TRANSFER_CAP().call()
        transfer_amount = min(100_000_000, cap)
        req_id = vault.functions.requestAgentTransfer(transfer_amount).call({"from": agent})
        vault.functions.requestAgentTransfer(transfer_amount).transact({"from": agent})
        _travel(w3, 48 * 3600 + 1)
        agent_before = usdt.functions.balanceOf(agent).call()
        vault.functions.executeAgentTransfer(req_id).transact({"from": agent})
        agent_after = usdt.functions.balanceOf(agent).call()
        assert agent_after == agent_before + transfer_amount

        # --- 14. Final reconciliation after all moves ---
        vault_state_final = {
            "deployed": True,
            "total_assets": vault.functions.totalAssets().call(),
            "total_supply": vault.functions.totalSupply().call(),
            "pending_reserved": vault.functions.pendingReserved().call(),
            "last_attestation": vault.functions.lastAttestation().call(),
            "attest_timelock": vault.functions.ATTEST_TIMELOCK().call(),
            "max_attestation_delta_bps": vault.functions.MAX_ATTESTATION_DELTA_BPS().call(),
            "total_assets_priced": vault.functions.totalAssetsPriced().call(),
        }
        # Agent moved 100 USDT out, so real OKX balance dropped by 100
        okx_final = {"usdt_eq": 750.0, "total_eq": 750.0}
        result3 = reconcile(vault_state_final, okx_final, asset_decimals=6)
        assert result3.vault_deployed is True
        assert result3.okx_available is True
        assert result3.pending_reserved == 0


class TestAtteltaDeltaGuard:
    """Attestation delta cap is enforced on-chain."""

    def test_rejects_large_delta(self, env):
        w3, vault, agent = env["w3"], env["vault"], env["agent"]
        # Seed with 1M, attest at 1M
        _seed(env, env["alice"], 1_000_000_000)
        vault.functions.deposit(1_000_000_000).transact({"from": env["alice"]})
        vault.functions.attestTotalAssets(1_000_000_000).transact({"from": agent})

        # Try to attest 20% jump (exceeds 10% cap)
        _travel(w3, _ATTEST_TIMELOCK + 1)
        from tests.test_trading_vault import _expect_revert
        _expect_revert("AttestationDeltaTooLarge",
                       lambda: vault.functions.attestTotalAssets(1_200_000_000).transact({"from": agent}))

        # 10% jump should succeed
        _travel(w3, _ATTEST_TIMELOCK + 1)
        vault.functions.attestTotalAssets(1_100_000_000).transact({"from": agent})
        assert vault.functions.totalAssets().call() == 1_100_000_000


class TestExpireWithdrawalSelfServe:
    """Requesting user can expire their own withdrawal after deadline."""

    def test_self_serve_expire(self, env):
        w3, vault = env["w3"], env["vault"]
        alice = env["alice"]
        _seed(env, alice, 200_000_000)
        vault.functions.deposit(200_000_000).transact({"from": alice})
        shares = vault.functions.balanceOf(alice).call()

        vault.functions.requestWithdraw(shares).transact({"from": alice})
        alice_before = vault.functions.balanceOf(alice).call()

        # Travel past the 3-day deadline
        _travel(w3, 3 * 24 * 3600 + 1)

        # Alice can expire her own request (self-serve)
        vault.functions.expireWithdrawal(1).transact({"from": alice})
        alice_after = vault.functions.balanceOf(alice).call()
        assert alice_after == alice_before + shares
