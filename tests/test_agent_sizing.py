"""
Unit tests for agent.py fractional-Kelly order sizing.

The agent is never invoked end-to-end here: OkxCli is never called, so
these tests are fully offline. They exercise `_compute_order_size`, the
pure sizing function, directly.

Run:
    pytest tests/test_agent_sizing.py -v
"""
import pytest

from src.agent import AutonomousTradingAgent
from src.execution import RiskGate
from src.signals import Signal


class _StubCli:
    """Minimal stand-in for OkxCli — the sizing path never touches it."""

    async def run(self, *args, **kwargs):
        raise AssertionError("sizing tests must not shell out to the CLI")


def _agent(sizing_mode="kelly", kelly_fraction=0.5, max_position_usd=5000):
    return AutonomousTradingAgent(
        okx_cli=_StubCli(),
        risk_gate=RiskGate(max_position_usd=max_position_usd),
        dry_run=True,
        max_position_usd=max_position_usd,
        sizing_mode=sizing_mode,
        kelly_fraction=kelly_fraction,
    )


def _signal(confidence_bps):
    return Signal(
        strategy="test",
        asset="BTC-USDT-SWAP",
        direction="LONG",
        confidence_bps=confidence_bps,
        entry_price=100.0,
        rationale="test",
    )


class TestKellySizing:

    def test_precisely_5000_confidence_no_trade(self):
        # p=0.50 => edge (2*0.5-1)=0 => size 0 (no positive Kelly edge).
        assert _agent()._compute_order_size(_signal(5000)) == 0.0

    def test_below_5000_confidence_no_trade(self):
        assert _agent()._compute_order_size(_signal(4000)) == 0.0

    def test_calibrated_at_7000(self):
        # p=0.70 => edge 0.40 => half-Kelly 0.20 => $1000 of $5000 max.
        assert _agent()._compute_order_size(_signal(7000)) == pytest.approx(1000.0)

    def test_kelly_uses_kelly_fraction(self):
        # Full Kelly (fraction=1.0): p=0.70 => 0.40 => $2000.
        full = _agent(kelly_fraction=1.0)
        half = _agent(kelly_fraction=0.5)
        assert full._compute_order_size(_signal(7000)) == pytest.approx(2000.0)
        assert half._compute_order_size(_signal(7000)) == pytest.approx(1000.0)

    def test_kelly_never_exceeds_linear_for_same_confidence(self):
        # Fractional Kelly must never bet more than the old linear rule,
        # holding kelly_fraction <= 1. Verify at a strong signal (95%).
        kelly = _agent()._compute_order_size(_signal(9500))
        linear = _agent(sizing_mode="linear")._compute_order_size(_signal(9500))
        assert kelly <= linear

    def test_kelly_size_never_negative(self):
        for conf in range(0, 10000, 500):
            assert _agent()._compute_order_size(_signal(conf)) >= 0.0

    def test_kelly_caps_below_max_position(self):
        # Even at p -> 1.0, half-Kelly edge is 1.0*0.5 = 0.5 * max.
        size = _agent()._compute_order_size(_signal(9999))
        assert size < 5000

    def test_linear_preserves_old_behavior(self):
        # Linear mode keeps the ORIGINAL sizing: size = max * p.
        assert _agent(sizing_mode="linear")._compute_order_size(_signal(7000)) == pytest.approx(3500.0)

    def test_signal_to_order_returns_none_for_zero_edge(self):
        # A high-confidence signal whose Kelly edge is zero must produce NO
        # order (the sizing filter applies inside _signal_to_order itself).
        agent = _agent()
        sig = _signal(5000)  # p=0.50 => edge 0 => no order
        order = agent._signal_to_order(sig, {"position": None})
        assert order is None

    def test_kelly_fraction_zero_disables_orders(self):
        # Safety: fraction 0 means never risk capital.
        assert _agent(kelly_fraction=0.0)._compute_order_size(_signal(9500)) == 0.0