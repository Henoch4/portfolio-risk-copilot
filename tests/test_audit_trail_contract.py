"""Behavioral tests for TradeAuditTrail.sol on a real EVM (eth-tester).

The repo's contract toolchain (hardhat + OpenZeppelin) is not installable on
some machines (no network), so the audit-trail contract follows the repo's
standalone-contract convention (zero imports, like TradingVault.sol) compiled
with py-solc-x via-ir and exercised on the eth-tester EVM.

The critical invariant under test here is AUTHORIZATION:
  - recordExecution must only be accepted from the decision's OWN agent.
    Before this test suite existed, recordExecution carried only the
    `onlyAgent` modifier, which checks `msg.sender == tx.origin` — it blocks
    contract callers but NOT other EOAs. Any wallet could forge an execution
    receipt on any agent's logged decision (poisoning the audit trail with
    fabricated fills) and permanently block the real agent's later
    record_execution via "execution already recorded".
"""
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from solcx import compile_source
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

SOLC_VERSION = "0.8.30"


@pytest.fixture(scope="session")
def compiled():
    path = Path(__file__).resolve().parent.parent / "contracts" / "contracts" / "TradeAuditTrail.sol"
    src = path.read_text(encoding="utf-8")
    result = compile_source(src, solc_version=SOLC_VERSION,
                            output_values=["abi", "bin"], via_ir=True)
    data = result.get("<stdin>:TradeAuditTrail")
    if data is None:
        raise KeyError("TradeAuditTrail not found in compile output")
    return data["abi"], "0x" + data["bin"]


@pytest.fixture()
def env(compiled):
    abi, bin_ = compiled
    w3 = Web3(EthereumTesterProvider())
    tester = w3.provider.ethereum_tester

    # Deterministic EOAs for agent, attacker, and a second agent.
    agent_key = Account.from_key("0x" + "11" * 32)
    attacker_key = Account.from_key("0x" + "22" * 32)
    tester.add_account("0x" + "11" * 32)
    tester.add_account("0x" + "22" * 32)
    genesis = w3.eth.accounts[0]

    for addr in (agent_key.address, attacker_key.address):
        tester.send_transaction({"from": genesis, "to": addr,
                                 "gas": 21000, "value": 10**18})

    contract = w3.eth.contract(abi=abi, bytecode=bin_)
    tx_hash = contract.constructor().transact({"from": agent_key.address})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    audit = w3.eth.contract(address=receipt["contractAddress"], abi=abi)

    audit.functions.setRiskParams(
        5000_00000000, 500_00000000, 50000, 7000,
    ).transact({"from": agent_key.address})

    return {
        "w3": w3, "audit": audit,
        "agent": agent_key.address, "attacker": attacker_key.address,
        "agent_key": agent_key,
    }


def _log_decision(audit, agent_key, decision_id: bytes):
    asset, signal, strategy = "BTC-USDT-SWAP", "LONG", "mean_reversion"
    confidence = 8500
    entry_price = 50000_00000000
    size_usd = 1000_00000000
    risk_hash = bytes.fromhex("22" * 32)
    # Mirrors src/audit_logger.py::_compute_payload_hash; abi.encodePacked with
    # no chain id — the digest is what the contract recomputes in logDecision.
    payload_hash = keccak(b"".join([
        decision_id, b"\x00" * 32, bytes.fromhex(agent_key.address[2:]),
        asset.encode(), signal.encode(), strategy.encode(),
        confidence.to_bytes(32, "big", signed=True),
        entry_price.to_bytes(32, "big"), size_usd.to_bytes(32, "big"),
        risk_hash,
    ]))
    sig = agent_key.sign_message(encode_defunct(primitive=payload_hash)).signature
    return {
        "decisionId": decision_id, "packageId": b"\x00" * 32,
        "asset": asset, "signal": signal, "strategy": strategy,
        "confidence": confidence, "entryPrice": entry_price, "sizeUsd": size_usd,
        "riskHash": risk_hash, "signature": sig, "isShort": False,
    }


def test_record_execution_rejects_forged_receipt_from_other_eoa(env):
    """Regression: before the fix, recordExecution carried only the
    `onlyAgent` (msg.sender == tx.origin) check, so ANY EOA could forge an
    execution receipt on another agent's decision — poisoning the audit trail
    and permanently DoSing the real agent's record_execution."""
    audit = env["audit"]
    decision_id = keccak(text="dec_test_001")
    audit.functions.logDecision(
        _log_decision(audit, env["agent_key"], decision_id)
    ).transact({"from": env["agent"]})

    # An attacker, not the decision's agent, tries to record the execution.
    with pytest.raises(Exception):
        audit.functions.recordExecution(
            decision_id, 49000_00000000, 1000_00000000, 0, True,
        ).transact({"from": env["attacker"]})

    # The decision must remain unexecuted and the trail unpoisoned.
    d = audit.functions.getDecision(0).call()
    assert d[12] is False  # executed
    assert audit.functions.decisionExecuted(decision_id).call() is False
    assert audit.functions.getExecutionCount().call() == 0


def test_record_execution_agent_owner_still_succeeds(env):
    """The decision's own agent must still be able to record the execution
    receipt after authorization is tightened."""
    audit = env["audit"]
    decision_id = keccak(text="dec_test_002")
    audit.functions.logDecision(
        _log_decision(audit, env["agent_key"], decision_id)
    ).transact({"from": env["agent"]})

    audit.functions.recordExecution(
        decision_id, 50000_00000000, 1000_00000000, 5_00000000, True,
    ).transact({"from": env["agent"]})

    d = audit.functions.getDecision(0).call()
    assert d[12] is True  # executed
    assert audit.functions.decisionExecuted(decision_id).call() is True
    assert audit.functions.getExecutionCount().call() == 1