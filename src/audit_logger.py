"""
Onchain audit logger for the autonomous trading agent.
Logs every trade decision to the TradeAuditTrail.sol contract on X Layer
BEFORE the order is submitted to OKX. Creates an immutable, verifiable audit trail.

The contract enforces:
  1. Risk parameters are set before any trading
  2. Position size and daily loss limits are never exceeded
  3. Confidence threshold is always met
  4. Every decision has a valid agent signature (EIP-191 personal_sign)

If the contract rejects, the trade is blocked — the agent cannot bypass it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from web3 import Web3 as Web
from eth_account import Account

logger = logging.getLogger(__name__)


@dataclass
class DecisionPayload:
    """The data that gets logged to the blockchain before execution."""
    decision_id: str
    agent_address: str
    asset: str
    signal: str
    strategy: str
    confidence_bps: int
    entry_price: float
    size_usd: float
    risk_params_hash: str
    timestamp: int
    is_short: bool = False


# Compiled ABI from TradeAuditTrail.sol (via-ir)
_ABI = [
    {
        "inputs": [
            {"name": "_maxPositionSizeUsd", "type": "uint256"},
            {"name": "_maxDailyLossUsd", "type": "uint256"},
            {"name": "_maxLeverageBps", "type": "uint256"},
            {"name": "_minConfidenceBps", "type": "uint256"},
        ],
        "name": "setRiskParams",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [
            {
                "name": "input",
                "type": "tuple",
                "components": [
                    {"name": "decisionId", "type": "bytes32"},
                    {"name": "asset", "type": "string"},
                    {"name": "signal", "type": "string"},
                    {"name": "strategy", "type": "string"},
                    {"name": "confidence", "type": "int256"},
                    {"name": "entryPrice", "type": "uint256"},
                    {"name": "sizeUsd", "type": "uint256"},
                    {"name": "riskHash", "type": "bytes32"},
                    {"name": "signature", "type": "bytes"},
                    {"name": "isShort", "type": "bool"},
                ],
            },
        ],
        "name": "logDecision",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [{"name": "reason", "type": "string"}],
        "name": "activateKillSwitch",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "deactivateKillSwitch",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "killSwitchActive",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "decisionId", "type": "bytes32"},
            {"name": "fillPrice", "type": "uint256"},
            {"name": "fillSizeUsd", "type": "uint256"},
            {"name": "feeUsd", "type": "uint256"},
            {"name": "success", "type": "bool"},
        ],
        "name": "recordExecution",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [{"name": "agent", "type": "address"}],
        "name": "agentRiskParams",
        "outputs": [
            {"name": "maxPositionSizeUsd", "type": "uint256"},
            {"name": "maxDailyLossUsd", "type": "uint256"},
            {"name": "maxLeverageBps", "type": "uint256"},
            {"name": "minConfidenceBps", "type": "uint256"},
        ],
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "decisionId", "type": "bytes32"},
            {"indexed": True, "name": "agent", "type": "address"},
            {"name": "asset", "type": "string"},
            {"name": "signal", "type": "string"},
            {"name": "confidence", "type": "int256"},
            {"name": "sizeUsd", "type": "uint256"},
            {"name": "riskHash", "type": "bytes32"},
        ],
        "name": "DecisionLogged",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "decisionId", "type": "bytes32"},
            {"indexed": True, "name": "agent", "type": "address"},
            {"name": "fillPrice", "type": "uint256"},
            {"name": "fillSizeUsd", "type": "uint256"},
            {"name": "feeUsd", "type": "uint256"},
            {"name": "success", "type": "bool"},
        ],
        "name": "TradeExecuted",
        "type": "event",
    },
]


class OnchainLogger:
    """Logs trade decisions to TradeAuditTrail.sol on X Layer."""

    def __init__(
        self,
        rpc_url: str,
        contract_address: str,
        private_key: str,
        chain_id: int = 195,
    ):
        self.w3 = Web(Web.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot connect to X Layer RPC at {rpc_url}")
        self.contract_address = Web.to_checksum_address(contract_address)
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.chain_id = chain_id
        self.agent_address = self.account.address
        self.contract = self.w3.eth.contract(address=self.contract_address, abi=_ABI)

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_nonce(self) -> int:
        return self.w3.eth.get_transaction_count(self.agent_address)

    def _build_tx(self, data: bytes, value: int = 0, gas: int = 300000) -> dict:
        return {
            "chainId": self.chain_id,
            "gas": gas,
            "gasPrice": self.w3.to_wei("1", "gwei"),
            "nonce": self.get_nonce(),
            "data": data,
            "value": value,
            "to": self.contract_address,
        }

    def set_risk_params(
        self,
        max_position_usd: float,
        max_daily_loss_usd: float,
        max_leverage_bps: int = 500,
        min_confidence_bps: int = 7000,
    ) -> str:
        """Set non-overridable risk parameters on the contract."""
        tx_data = self.contract.functions.setRiskParams(
            int(max_position_usd * 1e8),
            int(max_daily_loss_usd * 1e8),
            max_leverage_bps,
            min_confidence_bps,
        ).build_transaction({"chainId": self.chain_id, "nonce": self.get_nonce()})

        # Sign and send
        signed = self.w3.eth.account.sign_transaction(tx_data, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info(f"Risk params set. Tx: {tx_hash.hex()}")
        return tx_hash.hex()

    def _compute_payload_hash(self, payload: DecisionPayload) -> bytes:
        """Compute the keccak256 hash of the decision payload (what the agent signs).

        The contract computes: keccak256(abi.encodePacked(decisionId, agent, asset, ...))
        The agent signs this hash with personal_sign (EIP-191), which prepends
        "\x19Ethereum Signed Message:\n32" + hash.
        """
        decision_id_hash = Web.keccak(text=payload.decision_id)
        risk_hash_bytes = bytes.fromhex(
            payload.risk_params_hash[2:] if payload.risk_params_hash.startswith("0x")
            else payload.risk_params_hash
        )

        # abi.encodePacked equivalent in web3
        # We need to pack the values the same way Solidity does
        packed = (
            decision_id_hash
            + bytes.fromhex(payload.agent_address[2:])
            + payload.asset.encode("utf-8")
            + payload.signal.encode("utf-8")
            + payload.strategy.encode("utf-8")
        )
        # Need to handle int256 and uint256 as 32-byte values
        confidence_bytes = payload.confidence_bps.to_bytes(32, "big", signed=True)
        entry_price_bytes = int(payload.entry_price * 1e8).to_bytes(32, "big")
        size_usd_bytes = int(payload.size_usd * 1e8).to_bytes(32, "big")
        packed += confidence_bytes + entry_price_bytes + size_usd_bytes + risk_hash_bytes

        return Web.keccak(packed)

    def _sign_payload(self, payload: DecisionPayload) -> bytes:
        """Sign the payload hash using EIP-191 (personal_sign).

        The contract uses ecrecover with the Ethereum signed message prefix:
        keccak256("\x19Ethereum Signed Message:\n32" + payload_hash)

        So we must use personal_sign, not EIP-712.
        """
        payload_hash = self._compute_payload_hash(payload)
        signed_msg = self.account.sign_message(
            payload_hash,
            mechanism="personal",
        )
        return signed_msg.signature

    def log_decision(self, payload: DecisionPayload) -> str:
        """Log a trade decision to the blockchain. Returns tx hash.

        This is the hard gate — must be called BEFORE any order placement.
        If the contract reverts, the trade is blocked.
        """
        risk_hash_bytes = bytes.fromhex(
            payload.risk_params_hash[2:] if payload.risk_params_hash.startswith("0x")
            else payload.risk_params_hash
        )
        signature = self._sign_payload(payload)
        decision_id_hash = Web.keccak(text=payload.decision_id)

        # Build struct for logDecision
        decision_input = {
            "decisionId": decision_id_hash,
            "asset": payload.asset,
            "signal": payload.signal,
            "strategy": payload.strategy,
            "confidence": payload.confidence_bps,
            "entryPrice": int(payload.entry_price * 1e8),
            "sizeUsd": int(payload.size_usd * 1e8),
            "riskHash": risk_hash_bytes,
            "signature": signature,
            "isShort": payload.is_short,
        }

        tx_data = self.contract.functions.logDecision(decision_input).build_transaction({
            "chainId": self.chain_id,
            "nonce": self.get_nonce(),
        })

        signed = self.w3.eth.account.sign_transaction(tx_data, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info(f"Decision logged. Tx: {tx_hash.hex()}")
        return tx_hash.hex()

    def record_execution(
        self,
        decision_id: str,
        fill_price: float,
        fill_size_usd: float,
        fee_usd: float,
        success: bool,
    ) -> str:
        """Record execution result. Must reference a previously logged decision."""
        decision_id_hash = Web.keccak(text=decision_id)

        tx_data = self.contract.functions.recordExecution(
            decision_id_hash,
            int(fill_price * 1e8),
            int(fill_size_usd * 1e8),
            int(fee_usd * 1e8),
            success,
        ).build_transaction({
            "chainId": self.chain_id,
            "nonce": self.get_nonce(),
        })

        signed = self.w3.eth.account.sign_transaction(tx_data, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info(f"Execution recorded. Tx: {tx_hash.hex()}")
        return tx_hash.hex()

    def activate_kill_switch(self, reason: str) -> str:
        """Halt all onchain logDecision calls from this agent. Mirrors
        RiskGate.activate_kill_switch — call both so the halt is enforced
        even if only one layer is checked by a given caller."""
        tx_data = self.contract.functions.activateKillSwitch(reason).build_transaction({
            "chainId": self.chain_id,
            "nonce": self.get_nonce(),
        })
        signed = self.w3.eth.account.sign_transaction(tx_data, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.warning(f"Onchain kill switch activated: {reason}. Tx: {tx_hash.hex()}")
        return tx_hash.hex()

    def deactivate_kill_switch(self) -> str:
        """Resume onchain trading. A deliberate, separate call."""
        tx_data = self.contract.functions.deactivateKillSwitch().build_transaction({
            "chainId": self.chain_id,
            "nonce": self.get_nonce(),
        })
        signed = self.w3.eth.account.sign_transaction(tx_data, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info(f"Onchain kill switch deactivated. Tx: {tx_hash.hex()}")
        return tx_hash.hex()

    def is_kill_switch_active(self) -> bool:
        return bool(self.contract.functions.killSwitchActive(self.agent_address).call())

    def get_decision(self, decision_id: str) -> dict:
        """Query a decision from the contract by ID."""
        decision_id_hash = Web.keccak(text=decision_id)
        try:
            events = self.contract.events.DecisionLogged.get_logs(fromBlock=0)
            for evt in events:
                if evt["args"]["decisionId"] == decision_id_hash:
                    return dict(evt["args"])
        except Exception as e:
            logger.warning(f"Failed to query decision: {e}")
        return {}

    def compute_risk_hash(self, params: dict) -> str:
        """Compute a deterministic hash of risk parameters for logging."""
        serialized = json.dumps(params, sort_keys=True)
        return Web.keccak(text=serialized).hex()

    def get_contract_stats(self, days: int = 7) -> dict:
        """Query onchain decisions and executions from the past N days."""
        from_block = max(0, self.w3.eth.block_number - days * 5760)
        decisions = []

        try:
            decision_events = self.contract.events.DecisionLogged.get_logs(
                fromBlock=from_block
            )
            for evt in decision_events:
                decisions.append(dict(evt["args"]))
        except Exception as e:
            logger.warning(f"Failed to query decisions: {e}")

        return {
            "decisions": decisions,
            "num_decisions": len(decisions),
            "from_block": from_block,
            "current_block": self.w3.eth.block_number,
            "agent_address": self.agent_address,
        }
