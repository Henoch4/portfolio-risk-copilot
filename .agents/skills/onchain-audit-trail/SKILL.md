---
name: onchain-audit-trail
description: |
  Runbook for the onchain audit trail: src/audit_logger.py (OnchainLogger),
  contracts/contracts/TradeAuditTrail.sol, scripts/set_risk_params.py and
  deploy scripts, and tests/test_signature_roundtrip.py. Load when debugging
  signature/chain-ID/gas/leverage-unit failures on X Layer, when risk params
  are wrong on-chain, when logDecision reverts, or when adding a field to
  DecisionPayload or the contract. Encodes every known failure pattern as
  symptom → mechanism → fix → regression test.
version: 0.1.0
---

# Onchain audit trail runbook

Every trade decision is logged to `TradeAuditTrail.sol` on X Layer BEFORE the
order is submitted. If the contract rejects, the trade is blocked — the agent
cannot bypass it. This is the product's core non-overridable guarantee, and it
is also where most of this repo's real bugs lived.

## When to reach for this

- `logDecision` reverts, "invalid signature", or nothing ever lands on-chain.
- Risk params are wrong on-chain (e.g. every order rejected at some dollar cap).
- Gas failures, wrong chain, or RPC connection errors on X Layer.
- Adding a field to `DecisionPayload`, `DecisionLogged`, or the contract ABI.
- Editing `scripts/set_risk_params.py` or any deploy script.
- You are about to touch `_compute_payload_hash` or `_sign_payload`.

## What you need

- `XLAYER_RPC_URL` (default `https://xlayertestrpc.okx.com`), `XLAYER_CHAIN_ID=1952`
- `AUDIT_CONTRACT_ADDRESS` (default `0x6019...` in `src/main.py`), `AGENT_WALLET_PRIVATE_KEY`
- A fresh funded agent wallet. X Layer gas is OKB, not ETH.
- `python -m pytest tests/test_signature_roundtrip.py` for the sign/verify path.

## Known failure patterns

Each pattern: symptom → mechanism → fix → test-for-it.

### 1. Signature scheme: `mechanism="personal"` is not a thing

**Symptom:** `TypeError` on every `logDecision` call — the onchain audit trail
never runs at all. Tests still passed because they monkeypatch the CLI, not the
chain.
**Mechanism:** `_sign_payload` called `self.account.sign_message(payload_hash,
mechanism="personal")`. `eth_account`'s `LocalAccount.sign_message` has no such
kwarg — EIP-191 (personal_sign) is expressed by wrapping the payload hash in an
`encode_defunct` `SignableMessage` first.
**Fix:** `encode_defunct(primitive=payload_hash)` then `account.sign_message(...)`.
The contract's `ecrecover` path expects exactly this prefix. See the fix note in
`audit_logger.py` (~line 254).
**Test-for-it:** `tests/test_signature_roundtrip.py` derives the payload hash,
signs it, recovers the signer, and asserts equality — independent of any RPC.
It exists specifically to prove this path. Keep it green.

### 2. Chain ID: 195 vs 1952

**Symptom:** transactions get rejected by the RPC / chain mismatch.
**Mechanism:** X Layer testnet is chain ID **1952**. `OnchainLogger.__init__`
defaults to `chain_id=195` (line 160); `src/main.py` overrides with
`XLAYER_CHAIN_ID` defaulting to `"1952"`. Callers that construct
`OnchainLogger` directly (scripts) can silently use the wrong default.
**Fix:** always pass `chain_id=int(os.getenv("XLAYER_CHAIN_ID", "1952"))`.
Deploy scripts were fixed to 1952 (`82694bf`, `76b624c`).
**Test-for-it:** there is no RPC in CI; instead assert the env default stays
1952 whenever you touch `main.py` or the constructor default.

### 3. Gas token: X Layer uses OKB, not ETH

**Symptom:** "insufficient funds for gas" / deploy or log reverts with gas errors
even when the wallet looks funded.
**Mechanism:** X Layer prices gas in OKB. Funding a wallet with ETH (or assuming
wei math in ETH terms) breaks the first transaction.
**Fix:** fund the agent wallet with OKB. `cc57f22` fixed the deploy script.
**Test-for-it:** `scripts/` smoke test that does a real `setRiskParams` on testnet
and waits for the receipt.

### 4. Leverage units: 500 bps ≠ 5x

**Symptom:** on-chain gate rejects every realistic order (all caps out near some
small dollar value) even though the agent thinks it has 5x leverage.
**Mechanism:** `max_leverage_bps` is basis points where **10000 = 1x**. The old
default of `500` is 0.05x: with a `$5000 * 1e8` position cap,
`maxAllowed = (5000e8 * 500) / 10000 = $250`, so the on-chain leverage check
`EXCEEDS_MAX_LEVERAGE` fired on everything. 5x is `50000`.
**Fix:** default is now `max_leverage_bps=50000` in `audit_logger.py:193`.
Verify against the contract's own check before changing.
**Test-for-it:** `tests/test_execution.py` covers the off-chain gate; for the
on-chain side, `scripts/set_risk_params.py` then read back with `agentRiskParams()`
and confirm 50000 is what lands. (No unit test can catch a wrong *deployed*
constant — this is why the script + readback matters.)

### 5. Fixed-point scaling: `* 1e8` everywhere

**Symptom:** tiny or huge on-chain values; `sizeUsd`/`entryPrice`/fees off by
8 orders of magnitude vs. what the UI shows.
**Mechanism:** the contract stores USD/price in 1e8 fixed point. Python floats
are converted with `int(x * 1e8)` in `log_decision`, `record_execution`, and
`set_risk_params`. Any new field you add must follow the same scaling or the
contract will reject or silently mis-record it.
**Fix:** match `* 1e8` on the way in for every USD/price field; `1e8` fixed
point is also used by the ABI's `uint256` inputs.
**Test-for-it:** extend `test_signature_roundtrip.py` with the new field so the
hash covers it, and assert `int(entry_price * 1e8)` round-trips.

### 6. Event query kwargs: `from_block`, and dropping `indexed`

**Symptom:** `get_logs` / `/audit-decisions` / `/audit-stats` 500s after a web3
upgrade.
**Mechanism:** web3 v6 renamed `fromBlock` → `from_block` and `get_logs` returns
event args that include a non-serializable `indexed` key. `82694bf` fixed this.
**Fix:** use `from_block=...` (see `audit_logger.py` ~line 391); when converting
event args to plain dicts for JSON, drop the `indexed` entry.
**Test-for-it:** `tests/test_auditor.py` covers the off-chain auditor; the
onchain query path is exercised by `scripts/` smoke tests on testnet.

## Recipe library

- **Re-derive the payload hash:** `_compute_payload_hash` (line 224) is the
  source of truth for what gets signed. Keep it byte-for-byte in sync with the
  contract's `abi.encodePacked(...)` order.
- **Set risk params (only tightening allowed):** `scripts/set_risk_params.py`.
  `setRiskParams` on the contract is one-way per address — once set, params can
  only tighten. Setting wrong params on-chain requires a fresh agent address.
- **Kill switch is two-layer:** call both `RiskGate.activate_kill_switch`
  (off-chain, `src/execution.py`) and `OnchainLogger.activate_kill_switch`
  (on-chain, `audit_logger.py:339`) so the halt is enforced whichever layer a
  caller checks.

## What this skill is NOT

- Not a web3 tutorial. It assumes you know EIP-191, keccak, and `encodePacked`.
- Not the contract spec. Read `contracts/contracts/TradeAuditTrail.sol` for the
  exact checks (`EXCEEDS_MAX_LEVERAGE`, kill switch, risk params) before ABI edits.
- Not a replacement for running on testnet. Offline tests can't verify RPC,
  gas, or deployment — those need `scripts/` smoke runs.

## When to escalate

- The contract ABI or struct changes — the `_ABI` in `audit_logger.py` must be
  regenerated (compiled via-ir, per the module docstring) and re-synced by hand.
- You changed `_compute_payload_hash` or the struct field order — the signature
  roundtrip test is the only thing standing between you and "invalid signature".
- Risk params are already set wrong on-chain — you need a new agent address,
  not a code change.
