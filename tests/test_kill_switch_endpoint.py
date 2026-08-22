"""Endpoint-level tests for /kill-switch/deactivate layer ordering.

Previously the local gate was cleared FIRST and the onchain deactivation was
best-effort: if the onchain tx failed, the endpoint still reported
"deactivated" while the TradeAuditTrail contract stayed halted — a false
status and two layers disagreeing until restart. The non-overridable onchain
layer must be lifted before the local gate, and a failure must leave the
local halt active (fail-closed).
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import src.main as main
    monkeypatch.setattr(main, "_onchain_logger", None)
    # Deterministic baseline: ensure the shared singleton gate is not tripped
    # by a previous test before this fixture builds the client.
    main._risk_gate.deactivate_kill_switch()
    yield TestClient(main.app)
    # Teardown: the onchain-failure test intentionally leaves the shared
    # singleton gate tripped — restore it so no later session test inherits
    # KILL_SWITCH_ACTIVE through main._risk_gate.
    main._risk_gate.deactivate_kill_switch()


class _FakeOnchainLogger:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def deactivate_kill_switch(self) -> str:
        if self.fail:
            raise RuntimeError("chain down")
        return "0xdeadbeef"


def test_deactivate_without_onchain_clears_local(client, monkeypatch):
    import src.main as main
    main._risk_gate.activate_kill_switch("manual")
    assert main._risk_gate.kill_switch_status()["active"] is True

    res = client.post("/kill-switch/deactivate")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "deactivated"
    assert body["onchain"] is None
    assert main._risk_gate.kill_switch_status()["active"] is False


def test_deactivate_onchain_success_clears_local(client, monkeypatch):
    import src.main as main
    monkeypatch.setattr(main, "_onchain_logger", _FakeOnchainLogger(fail=False))
    main._risk_gate.activate_kill_switch("manual")
    assert main._risk_gate.kill_switch_status()["active"] is True

    res = client.post("/kill-switch/deactivate")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "deactivated"
    assert body["onchain"]["tx_hash"] == "0xdeadbeef"
    assert main._risk_gate.kill_switch_status()["active"] is False


def test_deactivate_onchain_failure_keeps_local_halted(client, monkeypatch):
    """Regression: the old order (local first, onchain best-effort) left the
    local gate cleared when the onchain deactivation failed, while claiming
    'deactivated'. The local halt must stay active and the status must be an
    honest failure."""
    import src.main as main
    monkeypatch.setattr(main, "_onchain_logger", _FakeOnchainLogger(fail=True))
    main._risk_gate.activate_kill_switch("manual")
    assert main._risk_gate.kill_switch_status()["active"] is True

    res = client.post("/kill-switch/deactivate")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "deactivate_failed"
    assert "Onchain deactivation failed" in body["error"]
    assert body["onchain"]["error"] == "chain down"
    # Fail-closed: the local gate must still block trading on this layer.
    assert main._risk_gate.kill_switch_status()["active"] is True