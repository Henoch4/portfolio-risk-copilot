# ADR 0001: Fractional Kelly position sizing

## Status

Accepted (2026-08).

## Context

The agent must turn a risk-gate-approved order into a concrete position size in
USD. Two obvious choices:

- **Linear sizing** — a fixed fraction of `max_position_usd` per signal.
- **Kelly criterion** — size proportional to edge/odds, which maximizes
  long-run growth but is notoriously aggressive and fragile to estimate error.

## Decision

Use **fractional Kelly** (`kelly_fraction`, default conservative, applied as a
multiplier on the gate's `max_position_usd`), not raw Kelly and not flat linear.

Rationale:
- Raw Kelly overallocates when the edge estimate is noisy; fractional Kelly
  (e.g. half-Kelly) keeps growth near-optimal while cutting ruin risk.
- It stays *under* the on-chain `maxPositionSizeUsd` hard cap — the gate can
  never be bypassed by sizing, only tightened.
- The fraction is a single knob (`kelly_fraction`) that the curator profile and
  `CURATOR_*` env overrides can tune without touching code.

## Consequences

- Sizing lives in `src/agent.py` (`_signal_to_order`) and is always passed through
  `RiskGate.check_order`, which enforces `max_position_usd` on-chain as well.
- The fraction must stay `<= 1`; values `> 1` are a config error and should fail
  closed (rejected by the gate), not silently clamped.
