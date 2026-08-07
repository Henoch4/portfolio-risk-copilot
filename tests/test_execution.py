"""
Unit tests for execution.py — risk gate and order executor.
Zero dependencies on network or OKX CLI.
Run: pytest tests/test_execution.py -v
"""
import pytest

from src.execution import RiskGate, OrderRequest, RiskCheckResult, ExecutionError


class TestRiskGate:

    def test_allows_within_limits(self):
        gate = RiskGate(max_position_usd=5000, max_daily_loss_usd=500, max_daily_trades=10)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            size="1000",
        )
        result = gate.check_order(order, "agent1")
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
            gate.check_order(order, "agent1")
        result = gate.check_order(order, "agent1")
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
        assert result.code == "DAILY_LOSS_LIMIT_EXCEEDED"

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
        gate.check_order(order, "agent1")
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
        result1 = gate.check_order(order, "agent1")
        result2 = gate.check_order(order, "agent2")
        assert result1.approved is True
        assert result2.approved is True

    def test_report_volume_accumulates(self):
        gate = RiskGate(max_position_usd=5000)
        gate.report_volume("agent1", 1000)
        gate.report_volume("agent1", 500)
        stats = gate.get_daily_stats("agent1")
        assert stats["volume"] == 1500.0


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
