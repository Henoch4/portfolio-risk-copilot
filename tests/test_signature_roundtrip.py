"""Proves the sign -> verify round trip for on-chain decision logging.

This was previously the single biggest unverified assumption in the repo:
EIP-191 signing exists in audit_logger.py and ecrecover-based verification
exists in TradeAuditTrail.sol, but nothing tied them together. A mismatch
here (wrong encoding, wrong hash order, wrong signing scheme) would make
every logDecision() call revert with "invalid signature" on a live chain
in a way this repo's other 66 tests can't catch, since they don't touch
audit_logger.py's payload hashing at all.

This test doesn't need a deployed contract or a live chain — it recomputes
the payload hash exactly as OnchainLogger._compute_payload_hash() does,
signs it exactly as _sign_payload() does, and then does what the
contract's _isValidSignature() does (prepend the EIP-191 prefix, recover
the signer), entirely in Python via eth_account. If this passes, the
sign/verify path is proven correct independent of any RPC or gas cost.
"""
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from src.audit_logger import DecisionPayload, OnchainLogger


TEST_PRIVATE_KEY = "0x" + "11" * 32  # arbitrary, deterministic, not a real fund-bearing key


def _make_test_logger():
    """Build an OnchainLogger without touching the network.

    OnchainLogger's __init__ connects to an RPC for its Web3 instance, but
    the hashing/signing helpers we're testing don't make network calls, so
    we construct it against a Web3 instance with no live provider and skip
    anything that would actually hit a chain.
    """
    logger = OnchainLogger.__new__(OnchainLogger)
    logger.w3 = Web3()  # no provider attached; fine for pure hashing/signing
    logger.private_key = TEST_PRIVATE_KEY
    logger.account = Account.from_key(TEST_PRIVATE_KEY)
    return logger


def _contract_side_recover(payload_hash: bytes, signature: bytes) -> str:
    """Reproduce exactly what TradeAuditTrail.sol._isValidSignature() does:
    prepend the EIP-191 personal-sign prefix to the digest and ecrecover.
    This does NOT reuse eth_account's sign_message/recover_message helpers
    for the message construction — it does the prefix-and-hash by hand,
    the same way the contract's Solidity does it with abi.encodePacked, so
    the test isn't just checking a Python library against itself.
    """
    prefix = b"\x19Ethereum Signed Message:\n32"
    eth_signed_message_hash = Web3.keccak(prefix + payload_hash)
    r = signature[0:32]
    s = signature[32:64]
    v = signature[64]
    if v < 27:
        v += 27
    sig_hex = "0x" + r.hex() + s.hex() + v.to_bytes(1, "big").hex()
    recovered = Account._recover_hash(eth_signed_message_hash, signature=sig_hex)
    return recovered


class TestSignatureRoundtrip:
    def test_signer_recovered_matches_agent_address(self):
        logger = _make_test_logger()
        payload = DecisionPayload(
            decision_id="test-decision-001",
            agent_address=logger.account.address,
            asset="BTC-USDT-SWAP",
            signal="LONG",
            strategy="mean_reversion",
            confidence_bps=8500,
            entry_price=50000.0,
            size_usd=1000.0,
            risk_params_hash="0x" + "22" * 32,
            timestamp=1700000000,
        )

        payload_hash = logger._compute_payload_hash(payload)
        signature = logger._sign_payload(payload)

        recovered_address = _contract_side_recover(payload_hash, signature)

        assert recovered_address.lower() == logger.account.address.lower(), (
            "The address recovered via the contract's exact verification "
            "scheme doesn't match the signer — logDecision() would revert "
            "with 'invalid signature' on-chain for every call."
        )

    def test_tampered_payload_fails_verification(self):
        """A payload hash that doesn't match what was actually signed must
        not verify — this is what stops a relayer from altering a decision
        after the agent signed it but before it's submitted on-chain."""
        logger = _make_test_logger()
        payload = DecisionPayload(
            decision_id="test-decision-002",
            agent_address=logger.account.address,
            asset="ETH-USDT-SWAP",
            signal="SHORT",
            strategy="funding_rate",
            confidence_bps=7200,
            entry_price=3000.0,
            size_usd=500.0,
            risk_params_hash="0x" + "33" * 32,
            timestamp=1700000001,
        )

        signature = logger._sign_payload(payload)

        tampered_payload = DecisionPayload(
            decision_id=payload.decision_id,
            agent_address=payload.agent_address,
            asset=payload.asset,
            signal=payload.signal,
            strategy=payload.strategy,
            confidence_bps=payload.confidence_bps,
            entry_price=payload.entry_price,
            size_usd=50000.0,  # tampered: 100x the signed size
            risk_params_hash=payload.risk_params_hash,
            timestamp=payload.timestamp,
        )
        tampered_hash = logger._compute_payload_hash(tampered_payload)

        recovered_address = _contract_side_recover(tampered_hash, signature)
        assert recovered_address.lower() != logger.account.address.lower(), (
            "A signature for one payload verified against a different, "
            "tampered payload — this would let an executed size be altered "
            "after signing without invalidating the signature."
        )

    def test_wrong_signer_key_fails_verification(self):
        logger = _make_test_logger()
        other_key = "0x" + "99" * 32
        other_account = Account.from_key(other_key)

        payload = DecisionPayload(
            decision_id="test-decision-003",
            agent_address=other_account.address,  # claims to be a different agent
            asset="SOL-USDT-SWAP",
            signal="LONG",
            strategy="momentum",
            confidence_bps=9000,
            entry_price=150.0,
            size_usd=200.0,
            risk_params_hash="0x" + "44" * 32,
            timestamp=1700000002,
        )

        # Signed by `logger`'s key, but the payload's agent_address field
        # claims to be `other_account`. The contract checks msg.sender
        # (the actual caller) against the recovered signer, so this should
        # recover to logger's address, not other_account's.
        payload_hash = logger._compute_payload_hash(payload)
        signature = logger._sign_payload(payload)
        recovered_address = _contract_side_recover(payload_hash, signature)

        assert recovered_address.lower() == logger.account.address.lower()
        assert recovered_address.lower() != other_account.address.lower()

    def _package_payload(self, logger, decision_id, asset, package_id):
        return DecisionPayload(
            decision_id=decision_id,
            agent_address=logger.account.address,
            asset=asset,
            signal="NEUTRAL",
            strategy="funding_arbitrage",
            confidence_bps=7000,
            entry_price=100.0,
            size_usd=5000.0,
            risk_params_hash="0x" + "55" * 32,
            timestamp=1700000003,
            package_id=package_id,
        )

    def test_both_package_legs_share_package_id_and_verify(self):
        """Regression: the funding-arb package logs two legs (spot buy +
        perp short) with the SAME package_id. Each leg is signed separately,
        and each signature must recover the agent — proving the contract
        receives a bytes32 packageId that links the legs without breaking
        per-leg signature verification."""
        logger = _make_test_logger()
        package_id = "pkg-abc-001"

        spot_leg = self._package_payload(
            logger, "pkg-decision-spot-001", "BTC-USDT", package_id
        )
        perp_leg = self._package_payload(
            logger, "pkg-decision-perp-001", "BTC-USDT-SWAP", package_id
        )

        for leg in (spot_leg, perp_leg):
            payload_hash = logger._compute_payload_hash(leg)
            signature = logger._sign_payload(leg)
            recovered = _contract_side_recover(payload_hash, signature)
            assert recovered.lower() == logger.account.address.lower()

        # Legs differ by decision_id/asset, so their hashes differ — but the
        # shared package_id is what a relayer can't unlink (covered below).
        assert logger._compute_payload_hash(spot_leg) != logger._compute_payload_hash(perp_leg)

    def test_tampered_package_id_fails_verification(self):
        """A signature for the real package_id must not verify against a
        payload whose package_id was swapped after signing."""
        logger = _make_test_logger()
        original = self._package_payload(
            logger, "pkg-decision-perp-002", "BTC-USDT-SWAP", "pkg-abc-002"
        )
        signature = logger._sign_payload(original)

        tampered = self._package_payload(
            logger, "pkg-decision-perp-002", "BTC-USDT-SWAP", "pkg-evil-999"
        )
        tampered_hash = logger._compute_payload_hash(tampered)

        recovered_address = _contract_side_recover(tampered_hash, signature)
        assert recovered_address.lower() != logger.account.address.lower(), (
            "Swapping a leg's package_id after signing must invalidate the "
            "signature, or a relayer could unlink a leg from its package."
        )

    def test_single_leg_payload_hashes_zero_package_id(self):
        """A single-leg (non-package) decision keeps packageId bytes32(0)
        in the hash layout — the ABI/layout regression pin so the Python
        hash can never drift from the contract's abi.encodePacked order
        (decisionId, packageId, agent, asset, signal, strategy, confidence,
        entryPrice, sizeUsd, riskHash)."""
        logger = _make_test_logger()
        payload = DecisionPayload(
            decision_id="test-decision-single-001",
            agent_address=logger.account.address,
            asset="ETH-USDT-SWAP",
            signal="LONG",
            strategy="mean_reversion",
            confidence_bps=8500,
            entry_price=3000.0,
            size_usd=1000.0,
            risk_params_hash="0x" + "66" * 32,
            timestamp=1700000004,
        )
        assert payload.package_id is None

        # Decision id and package id both hash to bytes32 and are packed in
        # that order — assert the layout matches the contract field order by
        # building the reference hash independently (no logger helper).
        decision_id_hash = Web3.keccak(text=payload.decision_id)
        package_id_hash = b"\x00" * 32
        reference = Web3.keccak(
            decision_id_hash
            + package_id_hash
            + bytes.fromhex(payload.agent_address[2:])
            + payload.asset.encode("utf-8")
            + payload.signal.encode("utf-8")
            + payload.strategy.encode("utf-8")
            + payload.confidence_bps.to_bytes(32, "big", signed=True)
            + int(payload.entry_price * 1e8).to_bytes(32, "big")
            + int(payload.size_usd * 1e8).to_bytes(32, "big")
            + bytes.fromhex(payload.risk_params_hash[2:])
        )

        assert logger._compute_payload_hash(payload) == reference, (
            "Python hash layout drifted from TradeAuditTrail.sol's "
            "abi.encodePacked order — every logDecision() would revert "
            "with 'invalid signature' on-chain."
        )
