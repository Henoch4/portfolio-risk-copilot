"""
Unit tests for auditor.py risk logic.

These tests NEVER call the real `okx` CLI or any network endpoint --
OkxCli is monkeypatched with a fake that returns fixture JSON shaped
exactly like the documented output schemas. This lets you verify the
risk-scoring logic before you have real credentials or a live account.

Run:
    pytest tests/ -v
"""
import pytest

from src import auditor


class FakeCli:
    """Stands in for OkxCli. Matches the same method signatures."""

    def __init__(self, balance_all=None, positions=None, smartmoney=None,
                 authenticated=True):
        self._balance_all = balance_all or {
            "trading": {"totalEq": "1000", "details": []},
            "funding": {"details": []},
        }
        self._positions = positions if positions is not None else []
        self._smartmoney = smartmoney or {}
        self._authenticated = authenticated

    async def check_auth(self):
        return {
            "authenticated": self._authenticated,
            "method": "api_key" if self._authenticated else None,
            "detail": {},
        }

    async def balance_all(self):
        return self._balance_all

    async def positions(self, inst_type=None):
        return self._positions

    async def smartmoney_signal(self, inst_ccy):
        return self._smartmoney.get(inst_ccy, {"data": []})


def _patch_cli(monkeypatch, fake: FakeCli):
    # auditor.py does `OkxCli(OkxCliConfig(...))` -- replace the class
    # itself so construction returns our fake regardless of args passed.
    monkeypatch.setattr(auditor, "OkxCli", lambda config: fake)


@pytest.mark.asyncio
async def test_not_authenticated_short_circuits(monkeypatch):
    fake = FakeCli(authenticated=False)
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    assert report.authenticated is False
    assert report.needs_human_review is True
    assert report.error is not None
    assert report.flags == []


@pytest.mark.asyncio
async def test_flags_concentration_risk(monkeypatch):
    fake = FakeCli(balance_all={
        "trading": {
            "totalEq": "1000",
            "details": [
                {"ccy": "PEPE", "eq": "900"},
                {"ccy": "USDT", "eq": "100"},
            ],
        },
        "funding": {"details": []},
    })
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "CONCENTRATION" in codes
    assert report.authenticated is True


@pytest.mark.asyncio
async def test_no_concentration_flag_when_balanced(monkeypatch):
    fake = FakeCli(balance_all={
        "trading": {
            "totalEq": "1000",
            "details": [
                {"ccy": "BTC", "eq": "500"},
                {"ccy": "USDT", "eq": "500"},
            ],
        },
        "funding": {"details": []},
    })
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "CONCENTRATION" not in codes


@pytest.mark.asyncio
async def test_flags_high_leverage(monkeypatch):
    fake = FakeCli(positions=[
        {"instId": "BTC-USDT-SWAP", "side": "long", "lever": "15", "upl": "-50"},
    ])
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "HIGH_LEVERAGE" in codes
    assert report.risk_score > 0


@pytest.mark.asyncio
async def test_flags_elevated_but_not_high_leverage(monkeypatch):
    fake = FakeCli(positions=[
        {"instId": "ETH-USDT-SWAP", "side": "short", "lever": "6", "upl": "10"},
    ])
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "ELEVATED_LEVERAGE" in codes
    assert "HIGH_LEVERAGE" not in codes


@pytest.mark.asyncio
async def test_smart_money_divergence_detected(monkeypatch):
    # NOTE: notional/longShortRatio are nested sub-objects in the real API
    # response (skills/okx-cex-smartmoney/references/signal-commands.md).
    # A prior version of this fixture had them flat/top-level, matching a
    # bug in auditor.py that meant this check silently never fired against
    # the real CLI -- the test passed against the wrong assumption. Fixed.
    fake = FakeCli(
        positions=[{"instId": "BTC-USDT-SWAP", "side": "short", "lever": "2"}],
        smartmoney={
            "BTC": {
                "data": [{
                    "ccy": "BTC-USDT-SWAP",
                    "notional": {"netNotionalUsdt": "500000"},
                    "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
                }],
            },
        },
    )
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "SMART_MONEY_DIVERGENCE" in codes


@pytest.mark.asyncio
async def test_smart_money_thin_pool_ignored(monkeypatch):
    fake = FakeCli(
        positions=[{"instId": "BTC-USDT-SWAP", "side": "short", "lever": "2"}],
        smartmoney={
            "BTC": {
                "data": [{
                    "ccy": "BTC-USDT-SWAP",
                    "notional": {"netNotionalUsdt": "500"},  # below threshold
                    "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
                }],
            },
        },
    )
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "SMART_MONEY_DIVERGENCE" not in codes


@pytest.mark.asyncio
async def test_lookalike_ticker_does_not_steal_match(monkeypatch):
    # Regression test for the startswith() bug: BTCUP-USDT-SWAP listed
    # before BTC-USDT-SWAP used to hijack the account-side lookup via
    # "BTCUP-USDT-SWAP".startswith("BTC"), silently masking a real
    # divergence (false negative). Fixed with an exact split('-')[0] match.
    fake = FakeCli(
        positions=[
            {"instId": "BTCUP-USDT-SWAP", "side": "short", "lever": "2"},
            {"instId": "BTC-USDT-SWAP", "side": "long", "lever": "2"},
        ],
        smartmoney={
            "BTC": {
                "data": [{
                    "ccy": "BTC-USDT-SWAP",
                    "notional": {"netNotionalUsdt": "500000"},
                    "longShortRatio": {"weightedLongRatio": "0.2", "weightedShortRatio": "0.8"},
                }],
            },
        },
    )
    _patch_cli(monkeypatch, fake)

    report = await auditor.run_audit(demo=True)

    codes = [f.code for f in report.flags]
    assert "SMART_MONEY_DIVERGENCE" in codes
