"""Cost model in backtest_simple (roadmap Phase 2).

A zero-cost backtest cannot answer "is this profitable" — these tests pin
the net-of-costs contract: costs charged per side on notional, funding once
per round trip, win classification on NET pnl, and gross/net visibility.
"""
import pytest

from src.signals import Signal, backtest_simple


def _long_signal(price=100.0):
    return Signal("test", "BTC", "LONG", 8000, price)


def test_zero_costs_matches_gross_behavior():
    prices = [100.0, 110.0]
    result = backtest_simple(prices, [_long_signal()], initial_capital=10000)
    # 10% move, no costs
    assert result.total_return_bps == 1000
    assert result.gross_return_bps == 1000
    assert result.total_costs_usd == 0.0


def test_fees_turn_small_edge_negative():
    # +1% gross move; round-trip cost 2*(10+10)=40bps < 100bps → still positive
    prices = [100.0, 101.0]
    ok = backtest_simple(
        prices, [_long_signal()], initial_capital=10000,
        fee_bps=10, slippage_bps=10,
    )
    assert ok.total_return_bps == 60

    # +0.3% gross move; same 40bps round-trip cost → net negative
    bad = backtest_simple(
        [100.0, 100.3], [_long_signal()], initial_capital=10000,
        fee_bps=10, slippage_bps=10,
    )
    assert bad.gross_return_bps == pytest.approx(30, abs=1)  # float truncation
    assert bad.total_return_bps < 0
    assert bad.total_costs_usd > 0


def test_win_classified_on_net_pnl():
    # Gross win of +0.3% but net loss after 40bps round-trip costs:
    # the trade must count as a LOSS for win_rate purposes.
    prices = [100.0, 100.3]
    result = backtest_simple(
        prices, [_long_signal()], initial_capital=10000,
        fee_bps=10, slippage_bps=10,
    )
    assert result.num_trades == 1
    assert result.win_rate == 0.0


def test_funding_cost_charged_once_per_round_trip():
    prices = [100.0, 110.0]
    base = backtest_simple(prices, [_long_signal()], initial_capital=10000)
    with_funding = backtest_simple(
        prices, [_long_signal()], initial_capital=10000,
        funding_cost_bps=50,
    )
    # 50bps on 10000 notional = $50 = 50bps of initial capital, once (not twice).
    assert with_funding.total_return_bps == base.total_return_bps - 50
    # Negative funding (collected) improves the result symmetrically.
    collected = backtest_simple(
        prices, [_long_signal()], initial_capital=10000,
        funding_cost_bps=-50,
    )
    assert collected.total_return_bps == base.total_return_bps + 50


def test_costs_scale_with_compounding_capital():
    # Two sequential +10% trades: after the first, capital grew, so the second
    # trade's costs are charged on the larger notional.
    prices = [100.0, 110.0, 110.0, 121.0]
    signals = [_long_signal(100.0), Signal("t", "BTC", "NEUTRAL", 0, 110.0), _long_signal(110.0)]
    result = backtest_simple(
        prices, signals, initial_capital=10000,
        fee_bps=10, slippage_bps=10,
    )
    # Round-trip cost rate = 2*(10+10)bps = 40bps of notional.
    # Trade 1: notional 10000 → $40 costs; capital after = 10000+1000-40 = 10960.
    # Trade 2: notional 10960 → $43.84 costs.
    assert result.total_costs_usd == pytest.approx(40 + 10960 * 0.004, rel=1e-3)
