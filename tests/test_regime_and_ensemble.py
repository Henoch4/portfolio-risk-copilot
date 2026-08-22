"""Regime filter, correlated-evidence haircut, and funding-signal honesty.

Roadmap Phase 2 fixes, each pinned by a regression test:
- trend_regime classifies structural drift; unknown on insufficient data.
- mean-reversion LONG suppressed in a sustained downtrend (knife-catching),
  SHORT suppressed in a sustained uptrend; inactive when disabled/short data.
- momentum crossovers against the longer horizon are pullbacks, not regime
  changes — suppressed.
- ensemble: two price-action signals agreeing count LESS than two signals
  from different evidence families (correlation haircut).
- funding_rate_signal describes itself honestly as directional.
"""
import pytest

from src.signals import (
    Signal,
    ensemble_signal,
    funding_rate_signal,
    mean_reversion_signal,
    momentum_signal,
    trend_regime,
)


# --- trend_regime ---

def test_trend_regime_classifies_up_down_flat():
    up = [100.0 + i for i in range(60)]          # +59 over the window
    down = [160.0 - i for i in range(60)]        # -59
    flat = [100.0 + (i % 2) * 0.1 for i in range(60)]
    assert trend_regime(up, 50)["regime"] == "up"
    assert trend_regime(down, 50)["regime"] == "down"
    assert trend_regime(flat, 50)["regime"] == "flat"


def test_trend_regime_unknown_on_insufficient_data():
    assert trend_regime([100.0, 101.0], 50)["regime"] == "unknown"


# --- mean reversion suppression ---

def _downtrend_with_dip():
    """~14% structural decline over the last 50 bars, then a sharp final dip
    that drives the 20-bar z-score well below -2."""
    prices = [100.0 - 0.15 * i for i in range(52)]   # 100 -> 92.35
    prices += [94.0, 93.5, 93.0, 92.5, 92.0, 88.0, 85.0]
    return prices


def _uptrend_with_spike():
    """Mirror image: structural uptrend, then an overbought spike."""
    prices = [100.0 + 0.15 * i for i in range(52)]   # 100 -> 107.65
    prices += [106.0, 106.5, 107.0, 107.5, 108.0, 112.0, 115.0]
    return prices


def test_mean_reversion_long_suppressed_in_downtrend():
    prices = _downtrend_with_dip()
    unfiltered = mean_reversion_signal("BTC", prices, window=20, z_threshold=2.0)
    filtered = mean_reversion_signal(
        "BTC", prices, window=20, z_threshold=2.0, regime_window=50,
    )
    # The setup must actually be an oversold LONG without the guard, or this
    # test proves nothing.
    assert unfiltered.direction == "LONG"
    assert filtered.direction == "NEUTRAL"
    assert "suppressed" in filtered.rationale


def test_mean_reversion_unfiltered_when_disabled_or_short_data():
    prices = _downtrend_with_dip()
    assert mean_reversion_signal(
        "BTC", prices, window=20, z_threshold=2.0, regime_window=0,
    ).direction == "LONG"
    # Window longer than history -> filter inactive -> still LONG. (Slice
    # keeps the dip, so the unfiltered setup still fires.)
    assert mean_reversion_signal(
        "BTC", prices[-30:], window=20, z_threshold=2.0, regime_window=50,
    ).direction == "LONG"


def test_mean_reversion_short_suppressed_in_uptrend():
    prices = _uptrend_with_spike()
    unfiltered = mean_reversion_signal("BTC", prices, window=20, z_threshold=2.0)
    filtered = mean_reversion_signal(
        "BTC", prices, window=20, z_threshold=2.0, regime_window=50,
    )
    assert unfiltered.direction == "SHORT"
    assert filtered.direction == "NEUTRAL"


# --- momentum suppression ---

def test_momentum_bullish_crossover_suppressed_in_downtrend():
    # Deep structural decline, then a sharp V-bounce: short MA pops above
    # long MA (crossover LONG fires) while the 50-bar regime is still DOWN.
    closes = [200.0 - 2.0 * i for i in range(46)]      # 200 -> 110
    closes += [105.0, 115.0, 125.0, 135.0, 145.0]      # V-bounce, current 145
    price_data = [{"close": c, "volume": 1000} for c in closes]
    unfiltered = momentum_signal("BTC", price_data, short_window=5, long_window=20)
    filtered = momentum_signal(
        "BTC", price_data, short_window=5, long_window=20, regime_window=50,
    )
    assert unfiltered.direction == "LONG"  # crossover fired
    assert filtered.direction == "NEUTRAL"
    assert "pullback" in filtered.rationale


# --- ensemble correlation haircut ---

def test_price_action_agreement_counts_less_than_cross_family():
    mr = Signal("mean_reversion", "BTC", "LONG", 9000, 100.0)
    mom = Signal("momentum", "BTC", "LONG", 8000, 100.0)
    fund = Signal("funding_rate", "BTC", "LONG", 8000, None)

    same_family = ensemble_signal("BTC", [mr, mom])
    cross_family = ensemble_signal("BTC", [mr, fund])

    assert same_family.direction == "LONG"
    assert cross_family.direction == "LONG"
    # Same evidence series agreeing is weaker confirmation than a genuinely
    # different source agreeing.
    assert same_family.confidence_bps < cross_family.confidence_bps
    assert "haircut" in same_family.rationale


def test_haircut_only_hits_second_price_action_signal():
    mr = Signal("mean_reversion", "BTC", "LONG", 9000, 100.0)
    fund = Signal("funding_rate", "BTC", "LONG", 8000, None)
    mom = Signal("momentum", "BTC", "LONG", 8000, 100.0)

    # Same signal count on both sides (confidence normalizes by len(signals)):
    # a cross-family second vote adds more than a same-family one.
    cross = ensemble_signal("BTC", [mr, fund])
    same = ensemble_signal("BTC", [mr, mom])
    assert same.confidence_bps < cross.confidence_bps


# --- funding honesty ---

def test_funding_signal_rationale_says_directional_not_arb():
    sig = funding_rate_signal("BTC-USDT-SWAP", 0.005, threshold=0.001)
    assert sig.direction == "SHORT"
    assert "NOT a delta-neutral arb" in sig.rationale
