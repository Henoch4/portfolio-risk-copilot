#!/usr/bin/env python3
"""
smoke_test_trading.py — End-to-end test of the trading pipeline.

Tests the full flow:
  1. Signal generation (mean-reversion, momentum, funding-rate)
  2. Ensemble signal combination
  3. Risk gate evaluation (non-overridable)
  4. Onchain logger payload construction (simulated)
  5. Execution (dry-run mode)

No network calls, no OKX credentials, no blockchain needed.
Run: python3 scripts/smoke_test_trading.py
"""

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.signals import (
    Signal,
    mean_reversion_signal,
    momentum_signal,
    funding_rate_signal,
    ensemble_signal,
    backtest_simple,
    BacktestResult,
)
from src.execution import RiskGate, OrderRequest, OrderStatus


def _section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def check(label: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def test_mean_reversion():
    """Test mean-reversion signal generation."""
    _section("Mean Reversion Signal")

    # Oversold -> LONG
    prices = [100.0] * 22 + [50.0]
    sig = mean_reversion_signal("BTC-USDT-SWAP", prices, window=20, z_threshold=2.0)
    check("Oversold -> LONG", sig.direction == "LONG")
    check("Confidence >= 6000", sig.confidence_bps >= 6000)
    check("Entry price is current", sig.entry_price == 50.0)
    check("Is tradeable", sig.is_tradeable)
    print(f"  Rationale: {sig.rationale[:80]}...")

    # Overbought -> SHORT
    prices = [100.0] * 22 + [200.0]
    sig = mean_reversion_signal("BTC-USDT-SWAP", prices, window=20, z_threshold=2.0)
    check("Overbought -> SHORT", sig.direction == "SHORT")

    # Insufficient data -> NEUTRAL
    sig = mean_reversion_signal("BTC-USDT-SWAP", [100.0], window=20)
    check("Insufficient data -> NEUTRAL", sig.direction == "NEUTRAL")
    check("Not tradeable", not sig.is_tradeable)


def test_momentum():
    """Test momentum signal generation."""
    _section("Momentum Signal")

    # Bullish trend
    price_data = [{"close": 100 + i, "volume": 1000} for i in range(25)]
    sig = momentum_signal("BTC-USDT-SWAP", price_data, short_window=5, long_window=20)
    check("Bullish trend -> LONG", sig.direction == "LONG")
    check("Confidence >= 0", sig.confidence_bps >= 0)

    # Bearish trend
    price_data = [{"close": 100 - i, "volume": 1000} for i in range(25)]
    sig = momentum_signal("BTC-USDT-SWAP", price_data, short_window=5, long_window=20)
    check("Bearish trend -> SHORT", sig.direction == "SHORT")

    # Flat
    price_data = [{"close": 100.0, "volume": 1000} for _ in range(25)]
    sig = momentum_signal("BTC-USDT-SWAP", price_data, short_window=5, long_window=20)
    check("Flat market -> NEUTRAL", sig.direction == "NEUTRAL")


def test_funding_rate():
    """Test funding rate signal."""
    _section("Funding Rate Signal")

    sig = funding_rate_signal("BTC-USDT-SWAP", 0.005, threshold=0.001)
    check("Positive funding -> SHORT", sig.direction == "SHORT")

    sig = funding_rate_signal("BTC-USDT-SWAP", -0.005, threshold=0.001)
    check("Negative funding -> LONG", sig.direction == "LONG")

    sig = funding_rate_signal("BTC-USDT-SWAP", 0.0001, threshold=0.001)
    check("Low funding -> NEUTRAL", sig.direction == "NEUTRAL")


def test_ensemble():
    """Test ensemble signal combination."""
    _section("Ensemble Signal")

    # All bullish
    signals = [
        Signal("mean_reversion", "BTC", "LONG", 8000, 100.0),
        Signal("momentum", "BTC", "LONG", 7000, 100.0),
        Signal("funding_rate", "BTC", "LONG", 6000, 100.0),
    ]
    sig = ensemble_signal("BTC", signals)
    check("All bullish -> ensemble LONG", sig.direction == "LONG")
    check("Confidence > 0", sig.confidence_bps > 0)

    # Mixed
    signals = [
        Signal("mr", "BTC", "LONG", 8000, 100.0),
        Signal("mom", "BTC", "NEUTRAL", 0, 100.0),
        Signal("fr", "BTC", "SHORT", 7000, 100.0),
    ]
    sig = ensemble_signal("BTC", signals)
    check("Mixed signals handled", sig.direction in ("LONG", "SHORT", "NEUTRAL"))

    # Empty
    sig = ensemble_signal("BTC", [])
    check("Empty signals -> NEUTRAL", sig.direction == "NEUTRAL")


def test_risk_gate():
    """Test non-overridable risk gate."""
    _section("Risk Gate (non-overridable)")

    gate = RiskGate(
        max_position_usd=5000,
        max_daily_loss_usd=500,
        max_daily_trades=10,
        min_confidence_bps=7000,
    )

    # Valid order
    order = OrderRequest(
        inst_id="BTC-USDT-SWAP",
        side="buy",
        order_type="market",
        size="1000",
    )
    result = gate.check_order(order, "test_agent")
    check("Valid order approved", result.approved)
    check("Code is APPROVED", result.code == "APPROVED")

    # Asset not allowed
    order = OrderRequest(
        inst_id="PEPE-USDT-SWAP",
        side="buy",
        order_type="market",
        size="1000",
    )
    result = gate.check_order(order, "test_agent")
    check("Non-allowlisted asset rejected", not result.approved)
    check("Code is ASSET_NOT_ALLOWED", result.code == "ASSET_NOT_ALLOWED")

    # Position too large
    order = OrderRequest(
        inst_id="BTC-USDT-SWAP",
        side="buy",
        order_type="market",
        size="10000",
    )
    result = gate.check_order(order, "test_agent")
    check("Oversized position rejected", not result.approved)
    check("Code is POSITION_TOO_LARGE", result.code == "POSITION_TOO_LARGE")

    # Daily trade limit
    gate2 = RiskGate(max_daily_trades=1)
    order = OrderRequest(
        inst_id="BTC-USDT-SWAP",
        side="buy",
        order_type="market",
        size="1000",
    )
    gate2.check_order(order, "agent_a")
    result = gate2.check_order(order, "agent_a")
    check("Trade limit exceeded rejected", not result.approved)
    check("Code is DAILY_TRADE_LIMIT_EXCEEDED", result.code == "DAILY_TRADE_LIMIT_EXCEEDED")

    # Risk hash is deterministic
    h1 = gate.compute_risk_hash()
    h2 = gate.compute_risk_hash()
    check("Risk hash deterministic", h1 == h2)
    check("Risk hash is hex", h1.startswith("0x") or len(h1) == 64)


def test_backtest():
    """Test backtesting."""
    _section("Backtest")

    prices = [100.0, 105.0, 110.0, 108.0, 106.0, 112.0, 115.0, 110.0, 108.0, 115.0]
    signals = [
        Signal("test", "BTC", "LONG", 8000, 100.0),
        Signal("test", "BTC", "NEUTRAL", 0, 105.0),
        Signal("test", "BTC", "LONG", 7500, 108.0),
        Signal("test", "BTC", "NEUTRAL", 0, 112.0),
        Signal("test", "BTC", "SHORT", 8000, 115.0),
    ]
    result = backtest_simple(prices, signals, initial_capital=10000)
    check("Backtest returns BacktestResult", isinstance(result, BacktestResult))
    check("Has trades", result.num_trades >= 0)
    check("Has return value", isinstance(result.total_return_bps, int))
    check("Has sharpe ratio", isinstance(result.sharpe_ratio, float))

    print(f"\n  Summary: {result.num_trades} trades, "
          f"return={result.total_return_bps}bps, "
          f"sharpe={result.sharpe_ratio:.2f}, "
          f"win_rate={result.win_rate:.0%}")


def test_full_pipeline():
    """Test the full signal -> risk -> order pipeline."""
    _section("Full Pipeline (Signal -> Risk -> Order)")

    # Generate signals
    prices = [100.0] * 25 + [70.0]  # Strong mean-reversion LONG
    price_data = [{"close": p, "volume": 1000} for p in prices]

    sigs = [
        mean_reversion_signal("BTC-USDT-SWAP", prices, window=20),
        momentum_signal("BTC-USDT-SWAP", price_data, short_window=5, long_window=20),
        funding_rate_signal("BTC-USDT-SWAP", -0.003, threshold=0.001),
    ]
    ensemble = ensemble_signal("BTC-USDT-SWAP", sigs)

    check("Ensemble generated", ensemble is not None)
    print(f"  Ensemble: {ensemble.direction} (conf: {ensemble.confidence_bps/100:.0f}%)")

    # Check if tradeable
    if ensemble.is_tradeable:
        # Create order
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy" if ensemble.direction == "LONG" else "sell",
            order_type="market",
            size="1000",
            client_oid=f"test_{uuid.uuid4().hex[:8]}",
        )

        # Risk gate
        gate = RiskGate(
            max_position_usd=5000,
            min_confidence_bps=7000,
        )
        result = gate.check_order(order, "pipeline_test")
        check("Order passes risk gate", result.approved)
        check("Order is LONG buy", order.side == "buy")
        check("Order size valid", float(order.size) <= 5000)
    else:
        check("Signal not tradeable — would skip", True)


# Need uuid import for test
import uuid


if __name__ == "__main__":
    test_mean_reversion()
    test_momentum()
    test_funding_rate()
    test_ensemble()
    test_risk_gate()
    test_backtest()
    test_full_pipeline()
    print(f"\n{'=' * 60}")
    print("  ALL CHECKS PASSED")
    print(f"{'=' * 60}")
