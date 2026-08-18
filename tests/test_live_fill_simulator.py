"""Unit tests for LiveFillSimulator — the bridge that runs multi-leg package
steps through the shared OrderExecutor instead of a second order path.

The two things this pins down:
  1. Action -> side/reduce_only mapping is correct (a mis-mapped leg would
     open the wrong direction on a real exchange).
  2. The executor reports slippage as a PERCENTAGE (0.3 = 0.3%) while the
     multi-leg breach check compares FRACTIONS (0.003 = 0.3%). LiveFillSimulator
     must normalize percent -> fraction, or every live leg would be falsely
     flagged as a slippage breach and the package would unwind itself.
  3. Per-asset reference prices: a mixed-instrument package (spot + perp) must
     verify each leg against ITS OWN instrument's price/timestamp.
"""
import pytest

from src.execution import OrderResult, OrderStatus
from src.multi_leg import LiveFillSimulator, Step


class _StubExecutor:
    """Records the order it was given and returns a canned fill."""

    def __init__(self, state=OrderStatus.FILLED, fill_px="100", slippage_pct=None):
        self.calls = []
        self.state = state
        # A non-filled terminal state (cancelled/rejected) has no fill price,
        # mirroring the real executor behavior.
        if fill_px is not None and state not in (OrderStatus.FILLED,
                                                 OrderStatus.PARTIALLY_FILLED):
            fill_px = None
        self.fill_px = fill_px
        self.slippage_pct = slippage_pct

    async def place_order(self, order, current_price=None, current_price_timestamp=None):
        self.calls.append(
            (order, current_price, current_price_timestamp)
        )
        return OrderResult(
            order_id="ord-1",
            client_oid=order.client_oid,
            state=self.state,
            acc_fill_sz=order.size,
            fill_px=self.fill_px,
            fill_sz=order.size,
            fill_usd=order.size,
            fee="0",
            fee_ccy="USDT",
            slippage_pct=self.slippage_pct,
        )


def _spot_step():
    return Step(venue="okx", action="buy_spot", asset="BTC-USDT",
                amount_ratio=0.5, max_slippage_pct=0.01)


def _perp_step():
    return Step(venue="okx", action="short_perp", asset="BTC-USDT-SWAP",
                amount_ratio=0.5, max_slippage_pct=0.01)


def test_buy_spot_maps_to_buy_non_reduce_only():
    sim = LiveFillSimulator(_StubExecutor())
    sim(_spot_step(), 1000.0)
    order, _, _ = sim.executor.calls[0]
    assert order.side == "buy"
    assert order.reduce_only is False
    assert order.inst_id == "BTC-USDT"


def test_short_perp_maps_to_sell_open_not_reduce():
    sim = LiveFillSimulator(_StubExecutor())
    sim(_perp_step(), 1000.0)
    order, _, _ = sim.executor.calls[0]
    assert order.side == "sell"
    assert order.reduce_only is False  # opening a short, not closing a long
    assert order.inst_id == "BTC-USDT-SWAP"


def test_cover_perp_maps_to_buy_reduce_only():
    sim = LiveFillSimulator(_StubExecutor())
    sim(Step(venue="okx", action="cover_perp", asset="BTC-USDT-SWAP",
             amount_ratio=0.5, max_slippage_pct=0.01), 1000.0)
    order, _, _ = sim.executor.calls[0]
    assert order.side == "buy"
    assert order.reduce_only is True


def test_sell_spot_maps_to_sell_reduce_only():
    sim = LiveFillSimulator(_StubExecutor())
    sim(Step(venue="okx", action="sell_spot", asset="BTC-USDT",
             amount_ratio=0.5, max_slippage_pct=0.01), 1000.0)
    order, _, _ = sim.executor.calls[0]
    assert order.side == "sell"
    assert order.reduce_only is True


def test_market_order_size_passed_verbatim():
    sim = LiveFillSimulator(_StubExecutor())
    sim(_perp_step(), 1234.56)
    order, _, _ = sim.executor.calls[0]
    assert order.order_type == "market"
    assert order.size == "1234.56"


def test_filled_leg_reports_fill_and_fraction_slippage():
    """The executor reports slippage 0.3 (percent). The step limit is 0.01
    (fraction). 0.3% -> 0.003 must NOT breach a 1% step collar."""
    executor = _StubExecutor(slippage_pct=0.3)
    sim = LiveFillSimulator(executor)
    result = sim(_perp_step(), 1000.0)
    assert result.filled is True
    assert result.slippage_pct == pytest.approx(0.003)
    assert result.slippage_pct <= 0.01  # same units as step.max_slippage_pct


def test_unfilled_leg_reports_not_filled():
    executor = _StubExecutor(state=OrderStatus.CANCELLED)
    sim = LiveFillSimulator(executor)
    result = sim(_perp_step(), 1000.0)
    assert result.filled is False


def test_partial_fill_counts_as_filled():
    executor = _StubExecutor(state=OrderStatus.PARTIALLY_FILLED)
    sim = LiveFillSimulator(executor)
    result = sim(_perp_step(), 1000.0)
    assert result.filled is True


def test_per_asset_reference_price_is_forwarded():
    executor = _StubExecutor()
    sim = LiveFillSimulator(
        executor,
        reference_prices={"BTC-USDT": 99.0, "BTC-USDT-SWAP": 100.0},
        reference_timestamps={"BTC-USDT": 111.0, "BTC-USDT-SWAP": 222.0},
    )
    sim(_spot_step(), 500.0)
    sim(_perp_step(), 500.0)
    _, spot_price, spot_ts = executor.calls[0]
    _, perp_price, perp_ts = executor.calls[1]
    assert spot_price == 99.0 and spot_ts == 111.0
    assert perp_price == 100.0 and perp_ts == 222.0


def test_legacy_single_reference_still_works():
    executor = _StubExecutor()
    sim = LiveFillSimulator(executor, reference_price=100.0,
                            reference_price_timestamp=111.0)
    sim(_perp_step(), 1000.0)
    _, price, ts = executor.calls[0]
    assert price == 100.0 and ts == 111.0
