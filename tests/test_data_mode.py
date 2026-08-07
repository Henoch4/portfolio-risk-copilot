"""
Unit tests for data-forwarding mode (run_audit_from_data).

Tests verify that pre-gathered JSON data produces the same risk analysis
as the CLI-based path. Smart money is gathered server-side (public endpoint).

Run:
    pytest tests/test_data_mode.py -v
"""
import pytest

from src import auditor


# -- Fixtures --

BALANCE_BALANCED = {
    "trading": {
        "totalEq": "1000",
        "details": [
            {"ccy": "BTC", "eq": "500"},
            {"ccy": "USDT", "eq": "500"},
        ],
    },
    "funding": {"details": []},
}

BALANCE_CONCENTRATED = {
    "trading": {
        "totalEq": "1000",
        "details": [
            {"ccy": "PEPE", "eq": "900"},
            {"ccy": "USDT", "eq": "100"},
        ],
    },
    "funding": {"details": []},
}

POSITIONS_HIGH_LEVERAGE = [
    {"instId": "BTC-USDT-SWAP", "side": "long", "lever": "15", "upl": "-50"},
]

POSITIONS_ELEVATED_LEVERAGE = [
    {"instId": "ETH-USDT-SWAP", "side": "short", "lever": "6", "upl": "10"},
]

POSITIONS_DIVERGENT = [
    {"instId": "BTC-USDT-SWAP", "side": "short", "lever": "2"},
]

SMARTMONEY_BULLISH = {
    "BTC": {
        "data": [{
            "ccy": "BTC-USDT-SWAP",
            "notional": {"netNotionalUsdt": "500000"},
            "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
        }],
    },
}

SMARTMONEY_THIN = {
    "BTC": {
        "data": [{
            "ccy": "BTC-USDT-SWAP",
            "notional": {"netNotionalUsdt": "500"},
            "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
        }],
    },
}


# -- Smart money mock (server-side gathering) --

class FakeSmartMoneyCli:
    """Fake CLI for smart money only — sync, for data-forwarding path."""

    def __init__(self, smartmoney=None):
        self._smartmoney = smartmoney or {}

    def smartmoney_signal(self, inst_ccy):
        return self._smartmoney.get(inst_ccy, {"data": []})


def _patch_smartmoney(monkeypatch, fake: FakeSmartMoneyCli):
    monkeypatch.setattr(auditor, "OkxCli", lambda config: fake)


# -- Tests: Valid data -> correct risk analysis --

@pytest.mark.asyncio
async def test_balanced_portfolio_no_flags(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=[],
    )

    assert report.authenticated is True
    assert report.risk_score == 0.0
    assert report.flags == []
    assert report.needs_human_review is False


@pytest.mark.asyncio
async def test_concentration_risk_detected(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_CONCENTRATED,
        positions_data=[],
    )

    codes = [f.code for f in report.flags]
    assert "CONCENTRATION" in codes
    assert report.risk_score > 0


@pytest.mark.asyncio
async def test_high_leverage_detected(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=POSITIONS_HIGH_LEVERAGE,
    )

    codes = [f.code for f in report.flags]
    assert "HIGH_LEVERAGE" in codes
    assert report.risk_score > 0


@pytest.mark.asyncio
async def test_elevated_leverage_detected(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=POSITIONS_ELEVATED_LEVERAGE,
    )

    codes = [f.code for f in report.flags]
    assert "ELEVATED_LEVERAGE" in codes
    assert "HIGH_LEVERAGE" not in codes


@pytest.mark.asyncio
async def test_smart_money_divergence_detected(monkeypatch):
    fake = FakeSmartMoneyCli(smartmoney=SMARTMONEY_BULLISH)

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=POSITIONS_DIVERGENT,
        smartmoney_fn=fake.smartmoney_signal,
    )

    codes = [f.code for f in report.flags]
    assert "SMART_MONEY_DIVERGENCE" in codes


@pytest.mark.asyncio
async def test_smart_money_thin_pool_ignored(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli(smartmoney=SMARTMONEY_THIN))

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=POSITIONS_DIVERGENT,
    )

    codes = [f.code for f in report.flags]
    assert "SMART_MONEY_DIVERGENCE" not in codes


# -- Tests: Empty data -> no flags --

@pytest.mark.asyncio
async def test_empty_balance_no_flags(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data={"trading": {"totalEq": "0", "details": []}},
        positions_data=[],
    )

    assert report.flags == []
    assert report.risk_score == 0.0


@pytest.mark.asyncio
async def test_none_positions_treated_as_empty(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=None,
    )

    assert report.flags == []


# -- Tests: Input validation (whitelist-based) --

def test_invalid_balance_missing_total_eq():
    with pytest.raises(ValueError, match="totalEq"):
        auditor.run_audit_from_data(
            balance_data={"trading": {"details": []}},
            positions_data=[],
        )


def test_invalid_balance_not_dict():
    with pytest.raises(ValueError, match="must be a JSON object"):
        auditor.run_audit_from_data(
            balance_data="not a dict",
            positions_data=[],
        )


def test_invalid_balance_total_eq_not_numeric():
    with pytest.raises(ValueError, match="numeric string"):
        auditor.run_audit_from_data(
            balance_data={"trading": {"totalEq": "not_a_number", "details": []}},
            positions_data=[],
        )


def test_invalid_positions_not_array():
    with pytest.raises(ValueError, match="must be a JSON array"):
        auditor.run_audit_from_data(
            balance_data=BALANCE_BALANCED,
            positions_data="not an array",
        )


def test_invalid_position_missing_inst_id():
    with pytest.raises(ValueError, match="instId"):
        auditor.run_audit_from_data(
            balance_data=BALANCE_BALANCED,
            positions_data=[{"side": "long", "lever": "10"}],
        )


# -- Tests: Positions as dict (OKX API wraps in {data: [...]}) --

@pytest.mark.asyncio
async def test_positions_as_dict_with_data_key(monkeypatch):
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())

    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data={"data": POSITIONS_HIGH_LEVERAGE},
    )

    codes = [f.code for f in report.flags]
    assert "HIGH_LEVERAGE" in codes


# -- Golden test: data mode matches CLI mode risk analysis --

@pytest.mark.asyncio
async def test_golden_data_matches_cli_concentration(monkeypatch):
    """Same data through both paths should produce the same risk flags."""
    from src.auditor import _check_concentration

    # Direct call to extracted helper
    flags = _check_concentration(BALANCE_CONCENTRATED["trading"])
    codes = [f.code for f in flags]
    assert "CONCENTRATION" in codes

    # Via run_audit_from_data
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())
    report = auditor.run_audit_from_data(
        balance_data=BALANCE_CONCENTRATED,
        positions_data=[],
    )
    report_codes = [f.code for f in report.flags]
    assert "CONCENTRATION" in report_codes


@pytest.mark.asyncio
async def test_golden_data_matches_cli_leverage(monkeypatch):
    """Same data through both paths should produce the same risk flags."""
    from src.auditor import _check_leverage

    # Direct call to extracted helper
    flags, _ = _check_leverage(POSITIONS_HIGH_LEVERAGE)
    codes = [f.code for f in flags]
    assert "HIGH_LEVERAGE" in codes

    # Via run_audit_from_data
    _patch_smartmoney(monkeypatch, FakeSmartMoneyCli())
    report = auditor.run_audit_from_data(
        balance_data=BALANCE_BALANCED,
        positions_data=POSITIONS_HIGH_LEVERAGE,
    )
    report_codes = [f.code for f in report.flags]
    assert "HIGH_LEVERAGE" in report_codes
