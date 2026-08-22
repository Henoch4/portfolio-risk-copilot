"""Daily-loss attribution pinning (UTC-midnight straddle regression).

A trading cycle approved on UTC day D must book its losses against day D's
limits even if the fill/report lands after midnight. Without pinning, the
fresh day starts with the straddled loss already in it and day D's limit
never registers the breach.
"""
import re

from src.execution import RiskGate


def test_current_day_key_shape():
    gate = RiskGate()
    key = gate.current_day_key("agent1")
    assert re.fullmatch(r"agent1:\d{4}-\d{2}-\d{2}", key)


def test_report_loss_pinned_to_cycle_day_trips_kill_switch():
    gate = RiskGate(max_daily_loss_usd=500)
    pinned = "agent1:2026-08-19"  # a prior UTC day, not today

    gate.report_loss("agent1", 400, day_key=pinned)
    assert gate.kill_switch_status()["active"] is False

    gate.report_loss("agent1", 150, day_key=pinned)
    # Cumulative 550 >= 500 on the PINNED day -> auto-trip, even though
    # today's own bucket is still empty.
    status = gate.kill_switch_status()
    assert status["active"] is True
    assert "550" in status["reason"] or "$550" in status["reason"]


def test_report_loss_default_still_uses_today():
    gate = RiskGate(max_daily_loss_usd=500)
    gate.report_loss("agent1", 100)
    stats = gate.get_daily_stats("agent1")
    assert stats["loss"] == 100


def test_pinned_loss_does_not_pollute_today_bucket():
    gate = RiskGate(max_daily_loss_usd=500)
    gate.report_loss("agent1", 400, day_key="agent1:2026-08-19")
    stats = gate.get_daily_stats("agent1")
    assert stats["loss"] == 0  # today untouched by the pinned-day report
