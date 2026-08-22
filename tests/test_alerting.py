"""Phase-1 alerting: webhook delivery + risk_gate hook wiring.

Regression guard for the roadmap Phase-1 item "the halt must not exist only
in logs": kill-switch activation/deactivation and durable-counter write
failures must emit an alert when ALERT_WEBHOOK_URL is configured — and the
alerting layer must never take the trading loop down with it.
"""
import os

import pytest

from src import alerting
from src.execution import DurableDailyCounters, RiskGate


@pytest.fixture(autouse=True)
def _clean_alert_state(monkeypatch):
    alerting.reset_cooldowns()
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_COOLDOWN_SECONDS", raising=False)
    yield
    alerting.reset_cooldowns()


def test_send_alert_posts_payload(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    seen = {}

    def fake_post(url, payload, timeout):
        seen["url"] = url
        seen["payload"] = payload
        seen["timeout"] = timeout

    monkeypatch.setattr(alerting, "post_json", fake_post)
    assert alerting.send_alert("TEST_EVENT", "warning", "detail text") is True
    assert seen["url"] == "https://hooks.example/abc"
    assert seen["payload"]["event"] == "TEST_EVENT"
    assert seen["payload"]["severity"] == "warning"
    assert seen["payload"]["detail"] == "detail text"
    assert isinstance(seen["payload"]["timestamp"], int)


def test_disabled_without_url(monkeypatch):
    called = []

    def fake_post(url, payload, timeout):
        called.append(url)

    monkeypatch.setattr(alerting, "post_json", fake_post)
    assert alerting.send_alert("TEST_EVENT", "warning", "x") is False
    assert called == []


def test_never_raises_on_transport_failure(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")

    def boom(url, payload, timeout):
        raise OSError("network down")

    monkeypatch.setattr(alerting, "post_json", boom)
    assert alerting.send_alert("TEST_EVENT", "critical", "x") is False


def test_cooldown_suppresses_repeat_but_not_other_events(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    calls = []
    monkeypatch.setattr(alerting, "post_json", lambda u, p, t: calls.append(p["event"]))

    assert alerting.send_alert("RPC_DOWN", "critical", "once") is True
    assert alerting.send_alert("RPC_DOWN", "critical", "twice") is False
    assert alerting.send_alert("OTHER_EVENT", "warning", "different key") is True
    assert calls == ["RPC_DOWN", "OTHER_EVENT"]


def test_kill_switch_activation_and_deactivation_alert(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    events = []
    monkeypatch.setattr(alerting, "post_json", lambda u, p, t: events.append(p))

    gate = RiskGate()
    gate.activate_kill_switch("manual test")
    gate.deactivate_kill_switch()

    kinds = [(p["event"], p["severity"]) for p in events]
    assert ("KILL_SWITCH_ACTIVATED", "critical") in kinds
    assert ("KILL_SWITCH_DEACTIVATED", "warning") in kinds


def test_no_webhook_configured_means_silent_gate(monkeypatch):
    # Default posture: no ALERT_WEBHOOK_URL -> gate behavior unchanged,
    # no transport attempts, no exceptions.
    gate = RiskGate()
    gate.activate_kill_switch("no url configured")
    assert gate.kill_switch_status()["active"] is True


def test_persist_failure_alerts(monkeypatch, tmp_path):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    events = []
    monkeypatch.setattr(alerting, "post_json", lambda u, p, t: events.append(p))

    # Point the store at an existing directory: os.replace(file -> dir) raises
    # OSError on every platform, deterministically failing persistence.
    store = DurableDailyCounters(path=str(tmp_path))
    assert store.path == str(tmp_path)
    store.increment("default:2026-08-21", "loss", 1.0)

    assert any(p["event"] == "RISK_STATE_WRITE_FAILED" for p in events)
