"""Unit tests for src/data_integrity.py — the pre-signal integrity gate.

Pure Python, no deps, no network. Run: pytest tests/test_data_integrity.py -v
"""
from dataclasses import dataclass
import math

from src.data_integrity import DataIntegrityGate, MarketTick, Severity


@dataclass
class Tick:
    timestamp: float
    funding_rate: float


def test_market_tick_feeds_staleness_check():
    gate = DataIntegrityGate(staleness_threshold_s=30.0)
    now = 1_000.0
    ticks = {venue: MarketTick(timestamp=now - age, funding_rate=0.0001)
             for venue, age in (("btc", 1.0), ("eth", 120.0))}
    result = gate.check_market_data(ticks, now)
    assert result.severity == Severity.HARD_BLOCK


def test_ok_fresh_ticks_pass():
    gate = DataIntegrityGate(staleness_threshold_s=30.0)
    now = 1_000.0
    result = gate.check_market_data(
        {"btc": Tick(now - 1.0, 0.0001), "eth": Tick(now - 2.0, -0.0002)}, now
    )
    assert result.severity == Severity.OK
    assert not result.blocks_trading


def test_stale_tick_hard_blocks():
    gate = DataIntegrityGate(staleness_threshold_s=30.0)
    now = 1_000.0
    result = gate.check_market_data({"btc": Tick(now - 120.0, 0.0001)}, now)
    assert result.severity == Severity.HARD_BLOCK
    assert result.blocks_trading
    assert "stale" in result.reasons[0]


def test_aging_tick_is_soft_warning():
    gate = DataIntegrityGate(staleness_threshold_s=30.0)
    now = 1_000.0
    result = gate.check_market_data({"btc": Tick(now - 20.0, 0.0001)}, now)  # > 15s, < 30s
    assert result.severity == Severity.SOFT_WARNING
    assert not result.blocks_trading


def test_nan_funding_rate_hard_blocks():
    gate = DataIntegrityGate(staleness_threshold_s=30.0)
    now = 1_000.0
    result = gate.check_market_data({"btc": Tick(now - 1.0, math.nan)}, now)
    assert result.severity == Severity.HARD_BLOCK
    assert "funding_rate missing" in result.reasons[0]


def test_none_signal_hard_blocks():
    gate = DataIntegrityGate()
    result = gate.check_signal_freshness(None, max_age_cycles=3, current_cycle=7)
    assert result.blocks_trading


def test_signal_missing_field_hard_blocks():
    gate = DataIntegrityGate()
    result = gate.check_signal_freshness(
        {"confidence": 0.9, "direction": "long"}, max_age_cycles=3, current_cycle=7
    )
    assert result.blocks_trading
    assert "missing required field" in result.reasons[0]


def test_stale_signal_hard_blocks():
    gate = DataIntegrityGate()
    signal = {"confidence": 0.9, "direction": "long", "generated_at_cycle": 1}
    result = gate.check_signal_freshness(signal, max_age_cycles=3, current_cycle=10)
    assert result.blocks_trading
    assert "stale" in result.reasons[0]


def test_fresh_signal_passes():
    gate = DataIntegrityGate()
    signal = {"confidence": 0.9, "direction": "long", "generated_at_cycle": 9}
    result = gate.check_signal_freshness(signal, max_age_cycles=3, current_cycle=10)
    assert result.severity == Severity.OK
    assert not result.blocks_trading


def test_ledger_consistent_passes():
    gate = DataIntegrityGate()
    result = gate.check_ledger_consistency(cash=8_000.0, positions_value=2_000.0, expected_equity=10_000.0)
    assert result.severity == Severity.OK


def test_ledger_drift_hard_blocks():
    gate = DataIntegrityGate()
    result = gate.check_ledger_consistency(cash=6_000.0, positions_value=2_000.0, expected_equity=10_000.0)
    assert result.blocks_trading
    assert "ledger inconsistency" in result.reasons[0]


def test_orphaned_position_hard_blocks():
    gate = DataIntegrityGate()
    open_positions = {"pos1": 10, "pos2": 2}
    result = gate.check_orphaned_positions(open_positions, current_cycle=20, max_age_cycles=5)
    assert result.blocks_trading
    assert "pos1" in result.reasons[0]


def test_no_orphans_passes():
    gate = DataIntegrityGate()
    result = gate.check_orphaned_positions({"pos1": 19}, current_cycle=20, max_age_cycles=5)
    assert result.severity == Severity.OK


def test_combine_worst_severity_wins():
    gate = DataIntegrityGate()
    hard = gate.check_orphaned_positions({"pos1": 1}, current_cycle=20, max_age_cycles=5)
    soft = gate.check_market_data({"btc": Tick(1_000.0 - 20.0, 0.0001)}, now_s=1_000.0)
    ok = gate.check_signal_freshness(
        {"confidence": 0.9, "direction": "long", "generated_at_cycle": 19}, 5, 20
    )
    combined = gate.combine(hard, soft, ok)
    assert combined.severity == Severity.HARD_BLOCK
    assert combined.blocks_trading
    # drive it to a boolean so a future refactor can't silently drop the gate
    assert not gate.combine(soft, ok).blocks_trading


def test_combine_preserves_all_reasons():
    gate = DataIntegrityGate()
    stale_btc = gate.check_market_data({"btc": Tick(1_000.0 - 60.0, 0.0001)}, now_s=1_000.0)
    stale_eth = gate.check_market_data({"eth": Tick(1_000.0 - 120.0, 0.0001)}, now_s=1_000.0)
    combined = gate.combine(stale_btc, stale_eth)
    assert "btc" in combined.reasons[0]
    assert "eth" in combined.reasons[1]