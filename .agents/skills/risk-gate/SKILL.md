---
name: risk-gate
description: |
  Runbook for the non-overridable risk layer: src/execution.py (RiskGate,
  OrderExecutor), the kill switch (off-chain gate + onchain contract + UI),
  reduce-only enforcement, slippage/price collar, and fail-closed checks.
  Load when changing how orders are validated, when a trade was rejected
  unexpectedly, or when the kill switch or daily-loss auto-trip behavior
  matters. Encodes each known pattern as symptom → mechanism → fix →
  regression test.
version: 0.1.0
---

# Risk gate runbook

`RiskGate` is the Wall Street principle made code: "the strategy is allowed to
be creative; the risk and control layer must be boring, deterministic, and
non-negotiable." Thresholds are set at construction and cannot be bypassed by
the trading agent. Every order passes `check_order` before the executor acts,
and a rejected order ends the trade for that asset.

## When to reach for this

- A trade was rejected and you need to know which check fired (the `code`).
- Adding a risk parameter, an asset to the allowlist, or a new check.
- Kill switch: enabling/disabling, auto-trip on daily loss, the two-layer
  (off-chain gate + on-chain contract) design, or the UI button wiring.
- Reduce-only semantics, slippage collar, or fat-finger checks.
- Anything touching `OrderExecutor`, `OrderRequest`, `OrderResult`.

## What you need

- `src/execution.py` — the whole risk layer lives here (`RiskCheckResult`,
  `RiskGate`, `OrderExecutor`).
- `src/main.py` `/kill-switch/*` endpoints and `src/agent.py` orchestration.
- `contracts/contracts/TradeAuditTrail.sol` — the on-chain half of the kill switch.
- `python -m pytest tests/test_execution.py` for the gate's behavior.

## Known failure patterns

Each pattern: symptom → mechanism → fix → test-for-it.

### 1. Kill switch must be checked first, and the loss auto-trip is real

**Symptom:** an order is approved when you expected a halt; or you assume a
halt clears itself.
**Mechanism:** `check_order` checks the kill switch at step 0, before anything
else — no exception paths around it. Separately, `report_loss` AUTO-TRIPS the
kill switch when an agent crosses `max_daily_loss_usd` (execution.py:458) — the
design's fail-safe-default: reject everything rather than just the next order.
Deactivation is deliberate and separate; it never auto-clears.
**Fix:** keep step 0 first. If you add checks, add them AFTER the kill switch.
Remember the daily-loss breach leaves the gate halted until someone explicitly
calls `deactivate_kill_switch`.
**Test-for-it:** `tests/test_execution.py` asserts kill-switch-active rejects
all orders regardless of other params; add coverage for the `report_loss`
auto-trip the moment you touch loss accounting.

### 2. Two-layer kill switch: off-chain gate + on-chain contract

**Symptom:** UI shows the kill switch off but on-chain `logDecision` still
rejects; or halting in one layer doesn't stop the other.
**Mechanism:** the kill switch exists twice by design: `RiskGate` (off-chain,
`src/execution.py`) and `activateKillSwitch` on `TradeAuditTrail.sol` (on-chain,
`audit_logger.py:339`). They are independent; a caller may check either layer.
**Fix:** activating a halt calls BOTH layers; deactivating calls both too. The
UI halt button (`src/main.py` `/kill-switch/*` + frontend) wires to both.
**Test-for-it:** unit tests cover the off-chain gate; on-chain activation is a
testnet `scripts/` smoke check. When you change one layer, change the other and
say so in the commit.

### 3. Limit orders without a price reference must fail closed

**Symptom:** a limit order is approved with no `current_price` available, or a
20% fat-finger price sneaks through.
**Mechanism:** the slippage/price-collar check rejects `NO_PRICE_REFERENCE`
when a limit order has no `current_price` to check against — "an unenforceable
check is not a check" (execution.py:401-413). The fat-finger check (check 5)
is independent, always runs, and rejects >20% deviation.
**Fix:** do not weaken either to "skip when unknown". `check_order` already
refuses to approve a limit order it cannot price-check.
**Test-for-it:** `tests/test_execution.py` has cases for `NO_PRICE_REFERENCE`,
`SLIPPAGE_EXCEEDED`, and `FAT_FINGER_REJECTED`. Keep them; they encode the
fail-closed contract.

### 4. Reduce-only: no flipping long→short in one unmarked order

**Symptom:** a sell order against an existing long is approved and flips the
position, or the executor silently allows opening shorts via sell.
**Mechanism:** check 7 (execution.py:426-439) rejects a `sell` against a `long`
unless `reduce_only=True` is explicit. The strategy layer (`agent.py:233`) sets
`reduce_only=(signal.direction == "SHORT")`, which only holds if shorts reduce
longs. This is the guard against position flips.
**Fix:** never auto-set `reduce_only` to bypass the check; the caller must mark
the intent.
**Test-for-it:** `tests/test_execution.py` covers `REDUCE_ONLY_VIOLATION`.

## Recipe library

- **Read a rejection:** `RiskCheckResult` = `approved` + `code` + `reason`.
  Codes: `KILL_SWITCH_ACTIVE`, `ASSET_NOT_ALLOWED`, `POSITION_TOO_LARGE`,
  `DAILY_TRADE_LIMIT_EXCEEDED`, `DAILY_LOSS_LIMIT_EXCEEDED`,
  `FAT_FINGER_REJECTED`, `NO_PRICE_REFERENCE`, `SLIPPAGE_EXCEEDED`,
  `REDUCE_ONLY_VIOLATION`, `APPROVED`. Log them — a rejected order must leave a
  reason trail (the frontend shows rejected decisions).
- **Add an allowed asset:** only add to `allowed_assets` in `RiskGate.__init__`
  AND the strategy's expected set; the gate rejects anything not listed.
- **Order sequencing:** the agent logs the decision on-chain BEFORE execution
  (agent.py Phase 4 → Phase 5), and a production onchain-log failure blocks
  execution. Keep that order.

## What this skill is NOT

- Not the execution broker. `OrderExecutor` places orders via the OKX CLI
  (`OkxCli`); position math and fill handling live there, not in the gate.
- Not the on-chain contract's spec — the gate and the contract enforce
  overlapping but separate limits; read both before assuming parity.
- Not a permissions bypass. `RiskGate` is deliberately non-overridable; do not
  add an "executor can skip the gate" path.

## When to escalate

- A new risk dimension (e.g. correlation, volatility, per-pair limits) that
  needs new state in `RiskGate` and possibly the contract — design it before
  coding; the gate is the wrong place for creative logic.
- Kill-switch semantics change (auto-clear, timers, remote kill) — this touches
  two layers and the UI; stop and map all callers first.
- Daily-loss accounting moves to Redis/DB (currently in-memory per the comment
  at execution.py:276) — the auto-trip behavior must be preserved, not lost.
