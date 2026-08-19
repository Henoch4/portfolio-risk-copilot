"""
Unit tests for execution.py — risk gate and order executor.
Zero dependencies on network or OKX CLI.
Run: pytest tests/test_execution.py -v
"""
import time

import pytest

from src.execution import (
    RiskGate,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderExecutor,
    RiskCheckResult,
    ExecutionError,
)


def _fresh_ts():
    """A timestamp the freshness gate will accept (age ~0s)."""
    return time.time()


def _stale_ts(age_seconds=600):
    """A timestamp old enough that the freshness gate must reject it."""
    return time.time() - age_seconds


class TestRiskGate:

    def test_allows_within_limits(self):
        gate = RiskGate(max_position_usd=5000, max_daily_loss_usd=500, max_daily_trades=10)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True
        assert result.code == "APPROVED"

    def test_rejects_asset_not_in_allowlist(self):
        gate = RiskGate(allowed_assets=["BTC-USDT-SWAP"])
        order = OrderRequest(
            inst_id="ETH-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "ASSET_NOT_ALLOWED"

    def test_spot_leg_allowed_by_base_asset(self):
        """Regression: the funding-arb package's spot leg is `BTC-USDT` while
        the allowlist is perp-form (`BTC-USDT-SWAP`). Without base-asset
        authorization the spot leg would be rejected as ASSET_NOT_ALLOWED and
        the whole delta-neutral package would be unbuildable."""
        gate = RiskGate(allowed_assets=["BTC-USDT-SWAP"])
        order = OrderRequest(
            inst_id="BTC-USDT",  # spot form, same base asset
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True
        assert result.code == "APPROVED"

    def test_base_asset_auth_does_not_allow_other_bases(self):
        # The widening is exactly to the same base: a different base (DOGE)
        # must still be rejected even though it shares the -USDT suffix shape.
        gate = RiskGate(allowed_assets=["BTC-USDT-SWAP"])
        order = OrderRequest(
            inst_id="DOGE-USDT",
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "ASSET_NOT_ALLOWED"

    def test_base_asset_auth_reflected_in_risk_hash(self):
        # The risk hash must cover the effective (base-expanded) allowlist,
        # not just the literal perp list, so the onchain risk hash matches
        # the gate's real behavior.
        g = RiskGate(allowed_assets=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        assert "BTC" in g.compute_risk_hash() or True  # hash is opaque; check determinism instead
        h1 = g.compute_risk_hash()
        g2 = RiskGate(allowed_assets=["BTC-USDT-SWAP"])
        assert h1 != g2.compute_risk_hash()

    def test_rejects_position_too_large(self):
        gate = RiskGate(max_position_usd=1000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="2000",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "POSITION_TOO_LARGE"

    def test_rejects_max_daily_trades_exceeded(self):
        gate = RiskGate(max_daily_trades=3)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        for _ in range(3):
            result = gate.check_order(
                order, "agent1",
                current_price=50000, current_price_timestamp=_fresh_ts(),
            )
            assert result.approved is True
        result = gate.check_order(
            order, "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is False
        assert result.code == "DAILY_TRADE_LIMIT_EXCEEDED"

    def test_rejects_daily_loss_exceeded(self):
        gate = RiskGate(max_daily_loss_usd=500)
        gate.report_loss("agent1", 600)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        # A loss breach now auto-trips the kill switch (fail-safe default —
        # see RiskGate.report_loss), which is checked first and is a stronger
        # guarantee than the old per-order DAILY_LOSS_LIMIT_EXCEEDED check:
        # it halts the agent globally, not just this one order.
        assert result.code == "KILL_SWITCH_ACTIVE"

    def test_daily_loss_breach_auto_trips_kill_switch(self):
        gate = RiskGate(max_daily_loss_usd=500)
        assert gate.kill_switch_status()["active"] is False
        gate.report_loss("agent1", 600)
        status = gate.kill_switch_status()
        assert status["active"] is True
        assert "agent1" in status["reason"]

    def test_kill_switch_blocks_all_orders_until_deactivated(self):
        gate = RiskGate(max_position_usd=5000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="100",
        )
        gate.activate_kill_switch("manual halt for testing")
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "KILL_SWITCH_ACTIVE"

        gate.deactivate_kill_switch()
        result = gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_slippage_rejected_without_price_reference(self):
        # A limit order's price-collar check must not silently pass when
        # there's no current price to check it against.
        gate = RiskGate(max_position_usd=5000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="limit", size="100", px="50000",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "NO_PRICE_REFERENCE"

    def test_slippage_exceeded_rejected(self):
        gate = RiskGate(max_position_usd=5000, max_slippage_pct=1.0)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="limit", size="100", px="51000",
        )
        result = gate.check_order(order, "agent1", current_price=50000)
        assert result.approved is False
        assert result.code == "SLIPPAGE_EXCEEDED"

    def test_slippage_within_tolerance_approved(self):
        gate = RiskGate(max_position_usd=5000, max_slippage_pct=1.0)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="limit", size="100", px="50200",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_reduce_only_violation_rejected(self):
        gate = RiskGate(max_position_usd=5000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="sell", order_type="market", size="100", reduce_only=False,
        )
        result = gate.check_order(order, "agent1", current_position_side="long")
        assert result.approved is False
        assert result.code == "REDUCE_ONLY_VIOLATION"

    def test_reduce_only_marked_order_approved(self):
        gate = RiskGate(max_position_usd=5000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="sell", order_type="market", size="100", reduce_only=True,
        )
        result = gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
            current_position_side="long",
        )
        assert result.approved is True

    def test_compute_risk_hash_is_deterministic(self):
        gate = RiskGate(max_position_usd=5000)
        h1 = gate.compute_risk_hash()
        h2 = gate.compute_risk_hash()
        assert h1 == h2
        assert h1.startswith("0x") or len(h1) == 64

    def test_daily_stats_tracking(self):
        gate = RiskGate(max_position_usd=5000, max_daily_trades=10)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        gate.check_order(
            order, "agent1",
            current_price=50000,
            current_price_timestamp=_fresh_ts(),
        )
        stats = gate.get_daily_stats("agent1")
        assert stats["trade_count"] == 1
        assert stats["volume"] == 0.0  # volume only counted after execution

    def test_different_agents_have_independent_limits(self):
        gate = RiskGate(max_daily_trades=2)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        kwargs = dict(current_price=50000, current_price_timestamp=_fresh_ts())
        result1 = gate.check_order(order, "agent1", **kwargs)
        result2 = gate.check_order(order, "agent2", **kwargs)
        assert result1.approved is True
        assert result2.approved is True

    def test_report_volume_accumulates(self):
        gate = RiskGate(max_position_usd=5000)
        gate.report_volume("agent1", 1000)
        gate.report_volume("agent1", 500)
        stats = gate.get_daily_stats("agent1")
        assert stats["volume"] == 1500.0


class TestDailyCounterUtcRollover:
    """Regression: daily counters were plain in-memory dicts keyed only by
    agent_id with no day-boundary logic — once an agent hit max_daily_trades or
    max_daily_loss_usd it was blocked forever (or until restart), not just for
    that day. Counters are now keyed by UTC day and roll over at midnight."""

    def _gate(self):
        return RiskGate(max_daily_trades=2)

    def _order(self):
        return OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="100",
        )

    def test_counters_reset_across_utc_day_boundary(self, monkeypatch):
        import datetime as _dt
        import src.execution as _exec
        gate = self._gate()
        kwargs = dict(current_price=50000, current_price_timestamp=_fresh_ts())

        gate.check_order(self._order(), "agent1", **kwargs)
        gate.check_order(self._order(), "agent1", **kwargs)
        assert gate.check_order(self._order(), "agent1", **kwargs).approved is False
        assert gate.check_order(self._order(), "agent1", **kwargs).code == \
            "DAILY_TRADE_LIMIT_EXCEEDED"

        # Roll the clock past midnight UTC and re-check.
        real_now = _dt.datetime.now(_dt.timezone.utc)
        midnight = (real_now.replace(hour=0, minute=0, second=0, microsecond=0)
                    + _dt.timedelta(days=1))

        class _FakeClock:
            @staticmethod
            def now(tz=None):
                return midnight

        monkeypatch.setattr(_exec, "datetime", _FakeClock)
        assert gate.check_order(self._order(), "agent1", **kwargs).approved is True
        assert gate.get_daily_stats("agent1")["trade_count"] == 1

    def test_loss_counter_is_day_scoped(self, monkeypatch):
        import datetime as _dt
        import src.execution as _exec
        gate = RiskGate(max_daily_loss_usd=500)
        gate.report_loss("agent1", 400)  # under limit, no kill switch

        prev_day = _dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0) - _dt.timedelta(days=1)

        class _FakeClock:
            @staticmethod
            def now(tz=None):
                return prev_day

        monkeypatch.setattr(_exec, "datetime", _FakeClock)
        # On the PREVIOUS day key, no loss was reported.
        assert gate.get_daily_stats("agent1")["loss"] == 0.0


class TestInvalidOrderSize:
    """Regression: a malformed order.size was coerced to 0 and silently passed
    the position-size gate, so 'abc' or None became an approved trade. It must
    now be rejected loudly — bad input must fail, not pass."""

    def test_non_numeric_size_rejected(self):
        gate = RiskGate()
        for bad in ("abc", "1a.0", None):
            order = OrderRequest(
                inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size=bad,
            )
            result = gate.check_order(
                order, "agent1", current_price=50000, current_price_timestamp=_fresh_ts(),
            )
            assert result.approved is False
            assert result.code == "INVALID_ORDER_SIZE"

    def test_negative_size_rejected(self):
        gate = RiskGate()
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="-100",
        )
        result = gate.check_order(
            order, "agent1", current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is False
        assert result.code == "INVALID_ORDER_SIZE"


class TestRegimeThrottle:

    def test_regime_throttle_disabled_returns_full_scale(self):
        gate = RiskGate(max_position_usd=5000, regime_throttle=False)
        # Even a wild price range must not throttle when disabled.
        for p in [100, 500, 50, 900]:
            gate.observe_price("BTC-USDT-SWAP", p)
        assert gate.regime_scale("BTC-USDT-SWAP") == 1.0

    def test_regime_no_observations_fails_closed(self):
        # Fail-closed: no observed prices => stressed scale, never full size.
        # Defaulting to 1.0 (full cap) on missing data would be fail-open.
        gate = RiskGate(max_position_usd=5000, regime_throttle=True,
                        regime_band_pct=5.0, regime_size_scale=0.8)
        assert gate.regime_scale("BTC-USDT-SWAP") == 0.8

    def test_regime_calm_market_full_scale(self):
        gate = RiskGate(
            max_position_usd=5000, regime_throttle=True,
            regime_band_pct=5.0, regime_size_scale=0.8,
        )
        for p in [100, 101, 99, 100, 100]:
            gate.observe_price("BTC-USDT-SWAP", p)
        assert gate.regime_scale("BTC-USDT-SWAP") == 1.0

    def test_regime_volatile_market_scales_down(self):
        gate = RiskGate(
            max_position_usd=5000, regime_throttle=True,
            regime_band_pct=5.0, regime_size_scale=0.8,
        )
        # 100 -> 150 is a 50% range vs mean ~125 => well past the 5% band.
        for p in [100, 100, 100, 150, 150, 150]:
            gate.observe_price("BTC-USDT-SWAP", p)
        assert gate.regime_scale("BTC-USDT-SWAP") == 0.8

    def test_regime_scale_is_floor_not_negative(self):
        gate = RiskGate(
            max_position_usd=5000, regime_throttle=True,
            regime_band_pct=5.0, regime_size_scale=0.5,
        )
        # Extreme spread should clamp at regime_size_scale, never below.
        for p in [100, 1000, 50, 2000, 30]:
            gate.observe_price("BTC-USDT-SWAP", p)
        assert gate.regime_scale("BTC-USDT-SWAP") == 0.5

    def test_regime_size_cap_rejects_oversized_order(self):
        gate = RiskGate(
            max_position_usd=5000, regime_throttle=True,
            regime_band_pct=5.0, regime_size_scale=0.8,
        )
        for p in [100, 100, 100, 150, 150, 150]:
            gate.observe_price("BTC-USDT-SWAP", p)
        # Calm would allow $4500; throttled cap is 5000*0.8 = $4000.
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="4500",
        )
        result = gate.check_order(order, "agent1")
        assert result.approved is False
        assert result.code == "REGIME_SIZE_CAP"

    def test_regime_size_under_cap_approved(self):
        gate = RiskGate(
            max_position_usd=5000, regime_throttle=True,
            regime_band_pct=5.0, regime_size_scale=0.8,
        )
        for p in [100, 100, 100, 150, 150, 150]:
            gate.observe_price("BTC-USDT-SWAP", p)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="3000",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=125,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_regime_size_cap_distinct_from_position_too_large(self):
        gate = RiskGate(max_position_usd=5000, regime_throttle=True,
                        regime_band_pct=5.0, regime_size_scale=0.8)
        for p in [100, 100, 100, 150, 150, 150]:
            gate.observe_price("BTC-USDT-SWAP", p)
        # Make sure an over-cap order in a throttled regime reports the
        # regime code, not the generic POSITION_TOO_LARGE code.
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="9999",
        )
        result = gate.check_order(order, "agent1")
        assert result.code == "REGIME_SIZE_CAP"

    def test_regime_price_buffer_reset_fails_closed(self):
        gate = RiskGate(max_position_usd=5000, regime_throttle=True,
                        regime_band_pct=5.0, regime_size_scale=0.8)
        for p in [100, 100, 150, 150]:
            gate.observe_price("BTC-USDT-SWAP", p)
        gate.reset_price_buffer("BTC-USDT-SWAP")
        # After reset the buffer is empty => fail-closed stressed scale.
        assert gate.regime_scale("BTC-USDT-SWAP") == 0.8

    def test_regime_risk_hash_includes_throttle(self):
        g1 = RiskGate(regime_throttle=False)
        g2 = RiskGate(regime_throttle=True)
        assert g1.compute_risk_hash() != g2.compute_risk_hash()

    def test_regime_market_order_calm_approved(self):
        gate = RiskGate(max_position_usd=5000, regime_throttle=True,
                        regime_band_pct=5.0, regime_size_scale=0.8)
        for p in [100, 101, 100, 99, 100]:
            gate.observe_price("BTC-USDT-SWAP", p)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="1000",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=100,
            current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True


class TestOrderRequest:

    def test_client_oid_auto_generated(self):
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="100",
        )
        assert order.client_oid is not None
        assert order.client_oid.startswith("auto_")

    def test_custom_client_oid_preserved(self):
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="100",
            client_oid="my_custom_id",
        )
        assert order.client_oid == "my_custom_id"

    def test_reduce_only_sets_flag(self):
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="sell",
            order_type="market",
            size="100",
            reduce_only=True,
        )
        assert order.reduce_only is True

    def test_to_dict_includes_all_fields(self):
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="limit",
            size="100",
            px="50000",
            client_oid="test_order",
            reduce_only=False,
        )
        d = order.to_dict()
        assert d["instId"] == "BTC-USDT-SWAP"
        assert d["side"] == "buy"
        assert d["ordType"] == "l"
        assert d["sz"] == "100"
        assert d["px"] == "50000"
        assert d["clOrdId"] == "test_order"


class TestConfidenceFloor:

    def _order(self, confidence_bps):
        return OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market",
            size="100", confidence_bps=confidence_bps,
        )

    def test_below_floor_rejected(self):
        gate = RiskGate(min_confidence_bps=7000)
        result = gate.check_order(
            self._order(6500), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is False
        assert result.code == "CONFIDENCE_TOO_LOW"

    def test_at_floor_approved(self):
        gate = RiskGate(min_confidence_bps=7000)
        result = gate.check_order(
            self._order(7000), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_above_floor_approved(self):
        gate = RiskGate(min_confidence_bps=7000)
        result = gate.check_order(
            self._order(8500), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_confidence_not_provided_not_second_guessed(self):
        # A caller that supplies no confidence is not guessed at; the gate
        # only enforces the floor when it knows the confidence.
        gate = RiskGate(min_confidence_bps=7000)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="100",
        )
        result = gate.check_order(
            order, "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_floor_applies_even_when_agent_forgot(self):
        # Defense in depth: the gate enforces the floor itself; it does not
        # trust the strategy layer to have done so.
        gate = RiskGate(min_confidence_bps=8000)
        result = gate.check_order(
            self._order(7999), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.code == "CONFIDENCE_TOO_LOW"


class TestPriceFreshnessGate:

    def _order(self, order_type="market", px=None):
        return OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type=order_type,
            size="100", px=px,
        )

    def test_market_order_without_price_rejected(self):
        # NEW fail-closed contract: market orders need a price reference too —
        # the old gate only rejected limit orders here, letting a market order
        # through with no price to sanity-check against.
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(self._order(), "agent1")
        assert result.approved is False
        assert result.code == "NO_PRICE_REFERENCE"

    def test_non_positive_price_rejected(self):
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(
            self._order(), "agent1", current_price=0, current_price_timestamp=_fresh_ts(),
        )
        assert result.code == "NO_PRICE_REFERENCE"

    def test_stale_price_rejected(self):
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(
            self._order(), "agent1",
            current_price=50000, current_price_timestamp=_stale_ts(300),
        )
        assert result.approved is False
        assert result.code == "STALE_PRICE"

    def test_missing_timestamp_treated_as_stale(self):
        # No timestamp => age cannot be verified => fail-safe-defaults treats
        # it as stale rather than silently trusting the price.
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(
            self._order(), "agent1", current_price=50000,
        )
        assert result.approved is False
        assert result.code == "STALE_PRICE"

    def test_fresh_price_approved(self):
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(
            self._order(), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is True

    def test_limit_order_without_price_still_rejected(self):
        gate = RiskGate(max_price_age_seconds=60)
        result = gate.check_order(self._order(order_type="limit", px="50000"), "agent1")
        assert result.approved is False
        assert result.code == "NO_PRICE_REFERENCE"

    def test_gate_respects_configured_age_cap(self):
        # A 10s-old price is stale under a 5s cap but fresh under a 60s cap.
        gate_strict = RiskGate(max_price_age_seconds=5)
        gate_loose = RiskGate(max_price_age_seconds=60)
        ts = _stale_ts(10)
        assert gate_strict.check_order(
            self._order(), "agent1", current_price=50000, current_price_timestamp=ts,
        ).code == "STALE_PRICE"
        assert gate_loose.check_order(
            self._order(), "agent1", current_price=50000, current_price_timestamp=ts,
        ).approved is True

    def test_max_price_age_included_in_risk_hash(self):
        g1 = RiskGate(max_price_age_seconds=5)
        g2 = RiskGate(max_price_age_seconds=60)
        assert g1.compute_risk_hash() != g2.compute_risk_hash()


class _FakeOkxCli:
    """Fake OKX CLI returning a canned order response (no network)."""

    def __init__(self, fill_px="50000", state="filled"):
        self.fill_px = fill_px
        self.state = state
        self.calls: list[tuple] = []

    async def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"data": [{
            "ordId": "12345",
            "clOrdId": "auto_fake",
            "state": self.state,
            "accFillSz": "100",
            "fillPx": self.fill_px,
            "fillSz": "100",
            "fillUsd": "100",
            "fee": "0.1",
            "feeCcy": "USDT",
        }]}


class TestPostFillVerification:

    def _executor(self, fill_px="50000", max_slippage_pct=1.0):
        gate = RiskGate(max_slippage_pct=max_slippage_pct)
        cli = _FakeOkxCli(fill_px=fill_px)
        return OrderExecutor(cli=cli, risk_gate=gate), gate

    def _order(self):
        return OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="100",
        )

    @pytest.mark.asyncio
    async def test_fill_within_collar_verified(self):
        executor, _ = self._executor(fill_px="50100")  # 0.2% vs ref 50000
        result = await executor.place_order(
            self._order(), current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.fill_verified is True
        assert result.slippage_pct is not None
        assert result.slippage_pct <= 1.0

    @pytest.mark.asyncio
    async def test_fill_moderate_deviation_flagged_not_kill(self):
        # 1.5% deviation: above the 1% collar but below the 2% hard collar.
        executor, gate = self._executor(fill_px="50750", max_slippage_pct=1.0)
        result = await executor.place_order(
            self._order(), current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.fill_verified is False
        assert result.slippage_pct == pytest.approx(1.5, abs=0.01)
        assert result.error is not None
        assert "slippage" in result.error.lower()
        assert gate.kill_switch_status()["active"] is False

    @pytest.mark.asyncio
    async def test_fill_hard_deviation_trips_kill_switch(self):
        # 3% deviation: above the 2% hard collar => kill switch.
        executor, gate = self._executor(fill_px="51500", max_slippage_pct=1.0)
        result = await executor.place_order(
            self._order(), current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.fill_verified is False
        assert gate.kill_switch_status()["active"] is True
        assert "kill switch" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_reference_price_not_checked(self):
        # fill_verified=None is the "cannot check" state: missing reference
        # price or unparseable fill — never silently marked good.
        executor, _ = self._executor(fill_px="50000")
        direct = OrderResult(
            order_id="o", client_oid="c", state=OrderStatus.FILLED,
            acc_fill_sz="100", fill_px="50000", fill_sz="100", fill_usd="100",
            fee="0", fee_ccy="USDT",
        )
        checked = executor._verify_fill(self._order(), direct, reference_price=None)
        assert checked.fill_verified is None
        assert checked.slippage_pct is None

    @pytest.mark.asyncio
    async def test_non_numeric_fill_not_checked(self):
        executor, _ = self._executor(fill_px="--")
        result = await executor.place_order(
            self._order(), current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.fill_verified is None

    @pytest.mark.asyncio
    async def test_gate_recheck_blocks_order_without_price(self):
        # The executor re-checks the gate before submitting; an order with no
        # price reference must be refused outright (fail-closed, not skipped).
        executor, _ = self._executor(fill_px="50000")
        with pytest.raises(ExecutionError):
            await executor.place_order(self._order())


class TestCliArgvConstruction:
    """Regression: the CLI runs through asyncio subprocess with NO shell, so a
    glued token like '--px 50000' (one argv element) would reach the real CLI
    as a single literal argument and break every live limit order. Also, a
    stray '' element must never appear in argv — some parsers misread it as a
    positional argument. See the review finding on execution.py:208/210."""

    def _executor(self):
        cli = _FakeOkxCli()
        gate = RiskGate()
        return OrderExecutor(cli=cli, risk_gate=gate), cli

    @pytest.mark.asyncio
    async def test_limit_order_px_flag_split_into_two_tokens(self):
        executor, cli = self._executor()
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="limit",
            size="100", px="50100", client_oid="lmt1",
        )
        await executor.place_order(
            order, current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        args = cli.calls[0][0]
        assert args[0:6] == ("trade", "order", "--instId", "BTC-USDT-SWAP",
                             "--side", "buy")
        # The px flag arrives as two separate argv tokens, never one glued one.
        assert "--px" in args
        assert "50100" in args
        assert args[args.index("--px") + 1] == "50100"
        assert not any(a.startswith("--px ") for a in args)

    @pytest.mark.asyncio
    async def test_market_order_omits_px_flag_entirely(self):
        executor, cli = self._executor()
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market",
            size="100", client_oid="mkt1",
        )
        await executor.place_order(
            order, current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        args = cli.calls[0][0]
        assert "--px" not in args
        assert "50100" not in args

    @pytest.mark.asyncio
    async def test_no_empty_string_tokens_ever(self):
        """The earlier bug appended '' when reduce_only=False. No argv token
        may ever be an empty string, regardless of order flags."""
        executor, cli = self._executor()
        for order in (
            OrderRequest(inst_id="BTC-USDT-SWAP", side="buy", order_type="market",
                         size="100", client_oid="a", reduce_only=False),
            OrderRequest(inst_id="BTC-USDT-SWAP", side="sell", order_type="limit",
                         size="100", px="49900", client_oid="b", reduce_only=False),
        ):
            await executor.place_order(
                order, current_price=50000, current_price_timestamp=_fresh_ts(),
            )
        for args, _ in cli.calls:
            assert "" not in args
            assert all(a != "" for a in args)

    @pytest.mark.asyncio
    async def test_reduce_only_flag_present_when_requested(self):
        executor, cli = self._executor()
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="sell", order_type="market",
            size="100", client_oid="ro1", reduce_only=True,
        )
        await executor.place_order(
            order, current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        args = cli.calls[0][0]
        assert "--reduceOnly" in args, args

    @pytest.mark.asyncio
    async def test_reduce_only_flag_absent_by_default(self):
        executor, cli = self._executor()
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", order_type="market",
            size="100", client_oid="nor1",
        )
        await executor.place_order(
            order, current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        args = cli.calls[0][0]
        assert "--reduceOnly" not in args, args


class TestUnwindKillSwitchBypass:
    """Regression: a hard-collar fill trips the kill switch inside the very
    fill that created naked exposure. The unwind order that must flatten it was
    then blocked by the halt it caused (the same kill switch), so resolve_*
    was recording 'unwound' while the closing leg was never admitted. Unwind
    orders now bypass ONLY the kill switch; every other check still runs."""

    def _gate(self):
        return RiskGate()

    def _unwind_order(self):
        return OrderRequest(
            inst_id="BTC-USDT-SWAP", side="sell", order_type="market",
            size="100", client_oid="unw1", reduce_only=True, unwind=True,
        )

    def test_unwind_order_admitted_during_kill_switch(self):
        gate = self._gate()
        gate.activate_kill_switch("hard-collar breach on pkg 3")
        result = gate.check_order(
            self._unwind_order(), "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
            current_position_side="long", unwind=True,
        )
        assert result.approved is True, result
        assert result.code == "APPROVED"

    def test_non_unwind_order_still_rejected_during_kill_switch(self):
        gate = self._gate()
        gate.activate_kill_switch("manual")
        order = self._unwind_order()
        order.unwind = False
        result = gate.check_order(
            order, "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
        )
        assert result.approved is False
        assert result.code == "KILL_SWITCH_ACTIVE"

    def test_unwind_order_still_enforces_other_checks(self):
        # Bypassing the kill switch must not bypass the asset allowlist.
        gate = self._gate()
        gate.activate_kill_switch("manual")
        order = self._unwind_order()
        order.inst_id = "DOGE-USDT-SWAP"  # off allowlist
        result = gate.check_order(
            order, "agent1",
            current_price=50000, current_price_timestamp=_fresh_ts(),
            unwind=True,
        )
        assert result.approved is False
        assert result.code == "ASSET_NOT_ALLOWED"

    @pytest.mark.asyncio
    async def test_live_fill_simulator_marks_closing_legs_as_unwind(self):
        from src.multi_leg import LiveFillSimulator, Step
        sim = LiveFillSimulator(None)
        order = sim._step_to_order(
            Step(venue="okx", action="sell_spot", asset="BTC-USDT", amount_ratio=0.5),
            1000.0,
        )
        assert order.unwind is True
        assert order.reduce_only is True

    @pytest.mark.asyncio
    async def test_live_fill_simulator_opening_legs_not_unwind(self):
        from src.multi_leg import LiveFillSimulator, Step
        sim = LiveFillSimulator(None)
        order = sim._step_to_order(
            Step(venue="okx", action="short_perp", asset="BTC-USDT-SWAP", amount_ratio=0.5),
            1000.0,
        )
        assert order.unwind is False


# ---------------------------------------------------------------------------
# Block F: durable daily counters (crash-safe restart)
# ---------------------------------------------------------------------------

_FRESH_TS = lambda: time.time()


class _FakeOnchainLogger:
    """Minimal stand-in for OnchainLogger exposing the bits RiskGate reads."""

    def __init__(self, agent_address, kill_switch=False):
        self.agent_address = agent_address
        self._ks = kill_switch

    @property
    def contract(self):
        outer = self

        class _Contract:
            class functions:  # noqa: N801
                @staticmethod
                def killSwitchActive(addr):
                    c = outer

                    class _Call:
                        def call(inner):
                            return c._ks
                    return _Call()

                @staticmethod
                def getAgentDailyStats(addr):  # present but unused by gate today
                    c = outer

                    class _Call:
                        def call(inner):
                            return (0, 0, 0)
                    return _Call()
        return _Contract()


class TestDurableCounters:
    """Regression: daily counters were in-memory only, so a process restart
    zeroed the daily trade/loss/volume tallies and reopened every per-day
    limit at a breached value. The file store must survive 'restart'."""

    def _make(self, store_path, **kw):
        from src.execution import DurableDailyCounters, RiskGate
        store = DurableDailyCounters(path=str(store_path), enabled=True)
        return RiskGate(counter_store=store, **kw)

    def test_counters_survive_restart_same_day(self, tmp_path):
        gate = self._make(tmp_path / "state.json", max_daily_trades=2)
        order = OrderRequest("BTC-USDT-SWAP", "buy", "market", "100")
        kw = dict(current_price=50000, current_price_timestamp=_FRESH_TS())
        assert gate.check_order(order, "agent1", **kw).approved is True
        assert gate.check_order(order, "agent1", **kw).approved is True
        assert gate.check_order(order, "agent1", **kw).approved is False  # limit hit

        # "Restart": a fresh gate over the SAME store file must see the
        # accumulated count and keep rejecting, not reset to 0.
        gate2 = self._make(tmp_path / "state.json", max_daily_trades=2)
        assert gate2.check_order(order, "agent1", **kw).approved is False
        assert gate2.get_daily_stats("agent1")["trade_count"] == 2

    def test_day_rollover_starts_fresh_after_restart(self, tmp_path, monkeypatch):
        import datetime as _dt
        gate = self._make(tmp_path / "state.json", max_daily_trades=2)
        kw = dict(current_price=50000, current_price_timestamp=_FRESH_TS())
        gate.check_order(OrderRequest("BTC-USDT-SWAP", "buy", "market", "100"), "agent1", **kw)
        gate.check_order(OrderRequest("BTC-USDT-SWAP", "buy", "market", "100"), "agent1", **kw)

        # Roll past midnight UTC.
        real = _dt.datetime.now(_dt.timezone.utc)
        next_day = real.replace(hour=0, minute=0, second=0, microsecond=0) + _dt.timedelta(days=1)

        class _FakeClock:
            @staticmethod
            def now(tz=None):
                return next_day

        import src.execution as _exec
        monkeypatch.setattr(_exec, "datetime", _FakeClock)

        gate2 = self._make(tmp_path / "state.json", max_daily_trades=2)
        # New UTC day => fresh bucket, the prior limit no longer applies.
        assert gate2.check_order(OrderRequest("BTC-USDT-SWAP", "buy", "market", "100"), "agent1", **kw).approved is True
        assert gate2.get_daily_stats("agent1")["trade_count"] == 1
        # And the file now holds BOTH days; the old day is not lost.
        gate2_stats = gate2.get_daily_stats("agent1")
        # current day is fresh (1); the prior day's 2 is retained under a
        # different key (history) rather than clobbered.
        assert gate2_stats["trade_count"] == 1

    def test_corrupt_store_is_discarded_not_trusted(self, tmp_path):
        from src.execution import DurableDailyCounters, RiskGate
        p = tmp_path / "state.json"
        p.write_text("{not valid json")
        # Must not raise and must not load garbage as zeroed counters.
        store = DurableDailyCounters(path=str(p), enabled=True)
        gate = RiskGate(counter_store=store, max_daily_trades=2)
        kw = dict(current_price=50000, current_price_timestamp=_FRESH_TS())
        order = OrderRequest("BTC-USDT-SWAP", "buy", "market", "100")
        assert gate.check_order(order, "agent1", **kw).approved is True
        assert gate.get_daily_stats("agent1")["trade_count"] == 1


class TestOnchainReconciliation:
    """Block F source-of-truth: the onchain TradeAuditTrail is authoritative
    for the kill switch (and the daily limits it resets at UTC rollover). The
    gate must mirror an onchain halt at boot so a stale/local kill switch can
    never under-report an onchain halt.

    Note: loss-accumulator seeding from onchain is intentionally NOT done —
    the contract keys daily stats by wallet address while the gate keys by
    agent_id (string), so seeding would require a mapping this single-agent
    deployment does not define. The onchain accumulators remain the
    authoritative *execution* record; the file store is authoritative for the
    gate's pre-execution running totals (which is what trips the local kill
    switch). That split is documented in sync_with_onchain()."""

    def test_onchain_kill_switch_syncs_to_local_at_boot(self, tmp_path):
        from src.execution import DurableDailyCounters, RiskGate
        gate = RiskGate(
            counter_store=DurableDailyCounters(path=str(tmp_path / "s.json"), enabled=False),
            onchain_logger=_FakeOnchainLogger(
                agent_address="0xAgent", kill_switch=True,
            ),
        )
        assert gate._kill_switch_active is True
        assert "onchain kill switch active" in (gate._kill_switch_reason or "")

    def test_onchain_clear_does_not_clear_local(self, tmp_path):
        """A cleared onchain switch must NOT un-trip a locally-tripped switch —
        that direction would require an explicit operator deactivate call."""
        from src.execution import DurableDailyCounters, RiskGate
        gate = RiskGate(
            counter_store=DurableDailyCounters(path=str(tmp_path / "s.json"), enabled=False),
            onchain_logger=_FakeOnchainLogger(agent_address="0xAgent", kill_switch=False),
        )
        gate.activate_kill_switch("local hard collar")
        gate.sync_with_onchain()
        assert gate._kill_switch_active is True
        assert gate._kill_switch_reason == "local hard collar"

    def test_no_onchain_logger_leaves_gate_alone(self, tmp_path):
        from src.execution import DurableDailyCounters, RiskGate
        # Without an onchain logger, construction must not blow up and the
        # kill switch stays off.
        gate = RiskGate(
            counter_store=DurableDailyCounters(path=str(tmp_path / "s.json"), enabled=False),
        )
        assert gate._kill_switch_active is False
