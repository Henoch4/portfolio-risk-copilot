"""Unit tests for the centralized onchain send path (gas, nonce, revert).

No RPC is touched: web3 is mocked so we can assert the gas estimation, the
dynamic gas price, the thread-safe nonce counter, and that an on-chain revert
is surfaced instead of treated as success.
"""
import threading
from unittest.mock import MagicMock

import pytest

from src.audit_logger import OnchainLogger


def _make_logger():
    logger = OnchainLogger.__new__(OnchainLogger)
    logger.w3 = MagicMock()
    logger.contract_address = "0x" + "1" * 40
    logger.private_key = "0x" + "2" * 64
    logger.agent_address = "0x" + "3" * 40
    logger.chain_id = 1952
    logger._nonce_lock = threading.Lock()
    logger._nonce_counter = None

    logger.w3.to_wei.return_value = 10 ** 9  # 1 gwei
    logger.w3.eth.generate_gas_price.return_value = 10 ** 9
    logger.w3.eth.get_transaction_count.return_value = 5
    logger.w3.eth.account.sign_transaction.return_value = MagicMock(raw_transaction=b"raw")
    logger.w3.eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0xabc")
    logger.w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    logger.contract = MagicMock()
    func = MagicMock()
    func.estimate_gas.return_value = 100_000
    func.build_transaction.return_value = {"chainId": 1952}
    logger.contract.functions.setRiskParams.return_value = func
    return logger, func


def test_send_uses_estimated_gas_and_dynamic_price():
    logger, func = _make_logger()
    tx_hash = logger._send_transaction(lambda: func, "Risk params set")
    assert tx_hash == "0xabc"
    # estimate_gas called, then build_transaction with a capped gas value
    func.estimate_gas.assert_called_once()
    tx_dict = func.build_transaction.call_args.args[0]
    assert tx_dict["gas"] == min(int(100_000 * 1.25), 1_500_000)
    # 20% buffer over 1 gwei floor
    assert tx_dict["gasPrice"] == int(10 ** 9 * 1.2)


def test_nonce_counter_is_monotonic_and_seeded_once():
    logger, func = _make_logger()
    logger._send_transaction(lambda: func, "a")
    logger._send_transaction(lambda: func, "b")
    nonces = [c.args[0]["nonce"] for c in func.build_transaction.call_args_list]
    assert nonces == [5, 6], f"expected sequential nonces 5,6 got {nonces}"
    # get_transaction_count is only the seed, not per-call
    assert logger.w3.eth.get_transaction_count.call_count == 1


def test_reverted_receipt_raises():
    logger, func = _make_logger()
    logger.w3.eth.wait_for_transaction_receipt.return_value = {"status": 0}
    with pytest.raises(RuntimeError, match="reverted on-chain"):
        logger._send_transaction(lambda: func, "Risk params set")


def test_gas_estimate_fallback_on_revert():
    logger, func = _make_logger()
    func.estimate_gas.side_effect = Exception("execution reverted")
    logger._send_transaction(lambda: func, "Risk params set")
    tx_dict = func.build_transaction.call_args.args[0]
    assert tx_dict["gas"] == 300_000  # safe fallback, not the capped estimate


def test_chain_id_flows_from_logger_into_build_transaction():
    """The chain id used for the real transaction must be the logger's own
    chain_id — the property tests/test_chain_id_consistency.py cannot verify
    by string-matching "1952" across source files (that only proves defaults
    agree; a hardcoded literal in _send_transaction would still pass).

    Uses a NON-default chain_id so the assertion cannot pass vacuously: if
    _send_transaction hardcoded the default ("1952") instead of reading
    self.chain_id, this test fails."""
    logger, func = _make_logger()
    logger.chain_id = 1953
    logger._send_transaction(lambda: func, "Risk params set")
    tx_dict = func.build_transaction.call_args.args[0]
    assert tx_dict["chainId"] == 1953
