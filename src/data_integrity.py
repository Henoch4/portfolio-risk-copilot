"""
Pre-signal data-integrity gate.

Runs BEFORE signal generation, not between signal and execution. It catches
bad inputs before they can ever produce a trade candidate -- a different
failure mode from the risk gate's job of catching bad trades built on good
inputs. In the trading loop this means: a feed that has gone stale, a signal
that was built on missing fields, a ledger whose book no longer reconciles, or
a position that has outlived its intended window must each halt the cycle
before any order is even considered.

Every check declares explicitly whether it is a HARD block or a SOFT warning.

Ported from the sibling `trading_system` MVP (risk_engine/data_integrity.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math


class Severity(Enum):
    HARD_BLOCK = "hard_block"
    SOFT_WARNING = "soft_warning"
    OK = "ok"


@dataclass
class IntegrityResult:
    severity: Severity
    reasons: list[str] = field(default_factory=list)

    @property
    def blocks_trading(self) -> bool:
        return self.severity == Severity.HARD_BLOCK


@dataclass
class MarketTick:
    """The per-venue market snapshot the pre-signal gate checks: a timestamp
    and the instrument's funding rate. Results either stay fresh or the gate
    blocks before a signal is ever built on them."""
    timestamp: float
    funding_rate: float


class DataIntegrityGate:
    def __init__(self, staleness_threshold_s: float = 30.0):
        self.staleness_threshold_s = staleness_threshold_s
        self._last_equity_check_ok = True
        self._open_positions_registry: dict[str, float] = {}

    def check_market_data(self, ticks: dict, now_s: float) -> IntegrityResult:
        """Feed-level integrity: staleness (hard block) and missing values."""
        reasons = []
        severity = Severity.OK

        for venue, tick in ticks.items():
            age = now_s - tick.timestamp
            if age > self.staleness_threshold_s:
                reasons.append(
                    f"{venue}: feed stale by {age:.1f}s (> {self.staleness_threshold_s}s) -> HARD BLOCK"
                )
                severity = Severity.HARD_BLOCK
            elif age > self.staleness_threshold_s * 0.5:
                reasons.append(f"{venue}: feed aging ({age:.1f}s) -> soft warning")
                if severity != Severity.HARD_BLOCK:
                    severity = Severity.SOFT_WARNING

            if math.isnan(tick.funding_rate):
                reasons.append(f"{venue}: funding_rate missing -> HARD BLOCK")
                severity = Severity.HARD_BLOCK

        return IntegrityResult(severity=severity, reasons=reasons)

    def check_signal_freshness(self, signal: dict | None, max_age_cycles: int,
                                current_cycle: int) -> IntegrityResult:
        if signal is None:
            return IntegrityResult(Severity.HARD_BLOCK, ["no signal produced this cycle -> HARD BLOCK"])

        required_fields = ("confidence", "direction", "generated_at_cycle")
        missing = [f for f in required_fields if f not in signal]
        if missing:
            return IntegrityResult(
                Severity.HARD_BLOCK,
                [f"signal missing required field(s) {missing} -> HARD BLOCK"],
            )

        age = current_cycle - signal["generated_at_cycle"]
        if age > max_age_cycles:
            return IntegrityResult(Severity.HARD_BLOCK, [f"signal stale ({age} cycles old) -> HARD BLOCK"])

        return IntegrityResult(Severity.OK, [])

    def check_ledger_consistency(self, cash: float, positions_value: float, expected_equity: float,
                                  tolerance: float = 0.005) -> IntegrityResult:
        actual = cash + positions_value
        drift = abs(actual - expected_equity) / max(expected_equity, 1e-9)
        if drift > tolerance:
            return IntegrityResult(
                Severity.HARD_BLOCK,
                [f"ledger inconsistency: cash+positions={actual:.2f} vs expected_equity={expected_equity:.2f} "
                 f"(drift {drift:.3%} > {tolerance:.3%}) -> HARD BLOCK, trading halted until reconciled"],
            )
        return IntegrityResult(Severity.OK, [])

    def check_orphaned_positions(self, open_positions: dict, current_cycle: int,
                                  max_age_cycles: int) -> IntegrityResult:
        orphans = []
        for pos_id, opened_cycle in open_positions.items():
            if current_cycle - opened_cycle > max_age_cycles:
                orphans.append(pos_id)
        if orphans:
            return IntegrityResult(
                Severity.HARD_BLOCK,
                [f"orphaned position(s) past intended window: {orphans} -> HARD BLOCK until reconciled"],
            )
        return IntegrityResult(Severity.OK, [])

    def combine(self, *results: IntegrityResult) -> IntegrityResult:
        """Worst-severity wins; every reason is preserved."""
        all_reasons = []
        worst = Severity.OK
        order = {Severity.OK: 0, Severity.SOFT_WARNING: 1, Severity.HARD_BLOCK: 2}
        for r in results:
            all_reasons.extend(r.reasons)
            if order[r.severity] > order[worst]:
                worst = r.severity
        return IntegrityResult(worst, all_reasons)