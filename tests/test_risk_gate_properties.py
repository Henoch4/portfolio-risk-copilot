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


FRESH = time.time()
STALE = time.time() - 100  # > default max_price_age_seconds (60)


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
    res = _check(gate, _order(side, order_type, px), 100.0, STALE, position)
    assert res.approved is False


def test_kill_switch_wins_over_everything():
    gate = RiskGate()
    gate.activate_kill_switch("manual")
    for side in ("buy", "sell"):
        for otype in ("market", "limit"):
            for px in (None, "100"):
                res = _check(gate, _order(side, otype, px), 100.0, FRESH)
                assert res.code == "KILL_SWITCH_ACTIVE"
                assert res.approved is False


def test_limit_fat_finger_rejected():
    gate = RiskGate()
    # 121 vs 100 = 21% > 20% fat-finger band, price is fresh
    res = _check(gate, _order("buy", "limit", "121"), 100.0, FRESH)
    assert res.approved is False
    assert res.code == "FAT_FINGER_REJECTED"


def test_limit_slippage_exceeded():
    gate = RiskGate()
    # 101.5 vs 100 = 1.5% > 1.0% collar, under the 20% fat-finger band
    res = _check(gate, _order("buy", "limit", "101.5"), 100.0, FRESH)
    assert res.approved is False
    assert res.code == "SLIPPAGE_EXCEEDED"


def test_reduce_only_enforced_on_flip():
    gate = RiskGate()
    res = _check(gate, _order("sell", "market", reduce_only=False), 100.0, FRESH, "long")
    assert res.approved is False
    assert res.code == "REDUCE_ONLY_VIOLATION"


def test_valid_market_order_approved():
    gate = RiskGate()
    res = _check(gate, _order("buy", "market"), 100.0, FRESH)
    assert res.approved is True
    assert res.code == "APPROVED"


def test_valid_market_order_reduce_only_ok():
    gate = RiskGate()
    res = _check(gate, _order("sell", "market", reduce_only=True), 100.0, FRESH, "long")
    assert res.approved is True
