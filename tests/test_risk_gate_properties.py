"""Property/edge coverage for RiskGate.check_order.

The review asked for property-based tests across the order_type x price_reference
x timestamp matrix. We can't pull hypothesis into the offline suite cheaply, so
this brute-forces the full matrix and asserts the fail-closed invariants the
gate must hold: no trustworthy price => reject; stale price => reject; kill
switch wins first; limit-price deviations are caught at the right tier; and a
fully valid order is the only thing that approves.
"""
import time

import pytest

from src.execution import OrderRequest, RiskCheckResult, RiskGate


def _order(side, order_type, px=None, confidence_bps=8000, reduce_only=False):
    return OrderRequest(
        inst_id="BTC-USDT-SWAP",
        side=side,
        order_type=order_type,
        size="100",  # $100 << max_position_usd (5000)
        px=px,
        confidence_bps=confidence_bps,
        reduce_only=reduce_only,
    )


def _check(gate, order, current_price, ts, position_side=None):
    return gate.check_order(
        order,
        agent_id="default",
        current_price=current_price,
        current_price_timestamp=ts,
        current_position_side=position_side,
    )


# Timestamps must be computed per-call, not at import time: the full suite
# outlives the 60s freshness cap, so an import-time FRESH goes stale mid-run
# and valid orders get rejected with STALE_PRICE.
def _fresh():
    return time.time()


def _stale():
    return time.time() - 100  # > default max_price_age_seconds (60)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("order_type", ["market", "limit"])
@pytest.mark.parametrize("px", [None, "100", "121", "101.5"])
def test_missing_price_always_rejected(side, order_type, px):
    # A missing market price makes freshness AND slippage unverifiable => reject.
    gate = RiskGate()
    res = _check(gate, _order(side, order_type, px), None, None)
    assert res.approved is False
    assert res.code == "NO_PRICE_REFERENCE"


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("order_type", ["market", "limit"])
@pytest.mark.parametrize("px", [None, "100", "121", "101.5"])
def test_stale_price_always_rejected(side, order_type, px):
    # A stale reference price is untrustworthy: the gate must reject it. The
    # exact rejection code depends on which earlier check fires first (fat-finger
    # / slippage / reduce-only can precede freshness), but approval is never
    # granted on a stale feed.
    gate = RiskGate()
    position = "long" if side == "sell" else None
    res = _check(gate, _order(side, order_type, px), 100.0, _stale(), position)
    assert res.approved is False


def test_kill_switch_wins_over_everything():
    gate = RiskGate()
    gate.activate_kill_switch("manual")
    for side in ("buy", "sell"):
        for otype in ("market", "limit"):
            for px in (None, "100"):
                res = _check(gate, _order(side, otype, px), 100.0, _fresh())
                assert res.code == "KILL_SWITCH_ACTIVE"
                assert res.approved is False


def test_limit_fat_finger_rejected():
    gate = RiskGate()
    # 121 vs 100 = 21% > 20% fat-finger band, price is fresh
    res = _check(gate, _order("buy", "limit", "121"), 100.0, _fresh())
    assert res.approved is False
    assert res.code == "FAT_FINGER_REJECTED"


def test_limit_slippage_exceeded():
    gate = RiskGate()
    # 101.5 vs 100 = 1.5% > 1.0% collar, under the 20% fat-finger band
    res = _check(gate, _order("buy", "limit", "101.5"), 100.0, _fresh())
    assert res.approved is False
    assert res.code == "SLIPPAGE_EXCEEDED"


def test_reduce_only_enforced_on_flip():
    gate = RiskGate()
    res = _check(gate, _order("sell", "market", reduce_only=False), 100.0, _fresh(), "long")
    assert res.approved is False
    assert res.code == "REDUCE_ONLY_VIOLATION"


def test_valid_market_order_approved():
    gate = RiskGate()
    res = _check(gate, _order("buy", "market"), 100.0, _fresh())
    assert res.approved is True
    assert res.code == "APPROVED"


def test_valid_market_order_reduce_only_ok():
    gate = RiskGate()
    res = _check(gate, _order("sell", "market", reduce_only=True), 100.0, _fresh(), "long")
    assert res.approved is True


@pytest.mark.parametrize("side,position", [
    ("sell", "long"),  # sell flipping a long -> short
    ("buy", "short"),  # buy flipping a short -> long  (was unchecked before)
])
def test_reduce_only_enforced_symmetrically(side, position):
    """Regression: the reduce-only gate only guarded sell-against-long. A buy
    order flipping an existing short into a long passed unchecked, asymmetric
    with the docstring's 'does not allow flipping a position' guarantee."""
    gate = RiskGate()
    res = _check(gate, _order(side, "market", reduce_only=False), 100.0, _fresh(), position)
    assert res.approved is False
    assert res.code == "REDUCE_ONLY_VIOLATION"


def test_reduce_only_marked_order_ok_in_both_directions():
    gate = RiskGate()
    res1 = _check(gate, _order("sell", "market", reduce_only=True), 100.0, _fresh(), "long")
    res2 = _check(gate, _order("buy", "market", reduce_only=True), 100.0, _fresh(), "short")
    assert res1.approved is True
    assert res2.approved is True


def test_missing_confidence_logged_but_not_rejected(caplog):
    """Regression: omitting confidence_bps silently skipped the confidence
    floor (equal to a forgotten confidence). It is still not a rejection — the
    gate cannot veto what it was never told — but it is now logged so the
    pattern is visible instead of silent."""
    gate = RiskGate(min_confidence_bps=7000)
    import logging
    with caplog.at_level(logging.WARNING, logger="src.execution"):
        res = _check(gate, _order("buy", "market", confidence_bps=None), 100.0, _fresh())
    assert res.approved is True
    assert any("CONFIDENCE_MISSING" in r.message for r in caplog.records) or \
        any("CONFIDENCE_MISSING" in r.getMessage() for r in caplog.records)


def test_low_confidence_still_rejected():
    gate = RiskGate(min_confidence_bps=7000)
    res = _check(gate, _order("buy", "market", confidence_bps=5000), 100.0, _fresh())
    assert res.approved is False
    assert res.code == "CONFIDENCE_TOO_LOW"
