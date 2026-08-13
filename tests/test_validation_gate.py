"""The validation gate must be wired: the endpoint runs the real report when
data is configured and the deploy script blocks only on an explicit fail."""
import os
import sys
import pathlib

import numpy as np
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "sample_returns.csv"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("VALIDATION_RETURNS_PATH", raising=False)
    import src.main as main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_validation_endpoint_honest_null_without_data(client):
    res = client.get("/api/v1/validation")
    assert res.status_code == 200
    body = res.json()
    assert body["gate_configured"] is False
    assert body["cleared_for_paper_trading"] is None


def test_validation_endpoint_runs_real_report_with_data(client, monkeypatch):
    monkeypatch.setenv("VALIDATION_RETURNS_PATH", str(FIXTURE))
    res = client.get("/api/v1/validation")
    assert res.status_code == 200
    body = res.json()
    assert body["gate_configured"] is True
    # monotonic fixture clears the Calmar bar
    assert body["cleared_for_paper_trading"] is True
    assert body["last_report"]["passes_calmar_bar"] is True


def test_check_validation_gate_blocks_on_failure(monkeypatch, tmp_path):
    # Calmar fails when out-of-sample is a straight downtrend.
    bad = tmp_path / "bad.csv"
    bad.write_text("in_sample,out_of_sample\n" + "\n".join(
        ["0.001,0.001"] * 20 + ["-0.05,-0.05"] * 20
    ))
    monkeypatch.setenv("VALIDATION_RETURNS_PATH", str(bad))
    sys.path.insert(0, str(REPO))
    import scripts.check_validation_gate as gate
    assert gate.main() == 1


def test_check_validation_gate_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("VALIDATION_RETURNS_PATH", raising=False)
    sys.path.insert(0, str(REPO))
    import scripts.check_validation_gate as gate
    assert gate.main() == 0
