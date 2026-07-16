#!/usr/bin/env python3
"""
smoke_test.py — zero-dependency sanity check.

Runs the ACTUAL risk-audit logic (src/auditor.py) against fixture data
that mimics real OKX CLI output. No pip install required, no OKX
credentials required, no network required.

If this prints "ALL CHECKS PASSED" at the bottom, the core logic is
sound. It does NOT prove the `okx` CLI itself will behave identically —
that can only be confirmed by running it against a real (demo) account,
which is a step for whoever has Node.js + internet (see SETUP.md).

Run:
    python3 scripts/smoke_test.py
"""

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src import auditor  # noqa: E402


class FakeCli:
    """Stands in for OkxCli — same method signatures, fixture data instead
    of a real subprocess call to the `okx` binary."""

    def __init__(self, balance_all=None, positions=None, smartmoney=None, authenticated=True):
        self._balance_all = balance_all or {
            "trading": {"totalEq": "1000", "details": []},
            "funding": {"details": []},
        }
        self._positions = positions if positions is not None else []
        self._smartmoney = smartmoney or {}
        self._authenticated = authenticated

    async def check_auth(self):
        return {"authenticated": self._authenticated, "method": "api_key", "detail": {}}

    async def balance_all(self):
        return self._balance_all

    async def positions(self, inst_type=None):
        return self._positions

    async def smartmoney_signal(self, inst_ccy):
        return self._smartmoney.get(inst_ccy, {"data": []})


def _patch(fake):
    auditor.OkxCli = lambda config: fake  # module-level swap, restored by caller


async def main() -> int:
    failures = []

    def check(label, condition):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}")
        if not condition:
            failures.append(label)

    print("1) Unauthenticated account short-circuits safely")
    _patch(FakeCli(authenticated=False))
    report = await auditor.run_audit(demo=True)
    check("authenticated=False on report", report.authenticated is False)
    check("needs_human_review=True", report.needs_human_review is True)
    check("no flags raised when unauthenticated", report.flags == [])

    print("\n2) Concentration risk — 90% in one asset")
    _patch(FakeCli(balance_all={
        "trading": {
            "totalEq": "1000",
            "details": [{"ccy": "PEPE", "eq": "900"}, {"ccy": "USDT", "eq": "100"}],
        },
        "funding": {"details": []},
    }))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("CONCENTRATION flag raised", "CONCENTRATION" in codes)

    print("\n3) Balanced portfolio — no concentration flag")
    _patch(FakeCli(balance_all={
        "trading": {
            "totalEq": "1000",
            "details": [{"ccy": "BTC", "eq": "500"}, {"ccy": "USDT", "eq": "500"}],
        },
        "funding": {"details": []},
    }))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("no CONCENTRATION flag when balanced", "CONCENTRATION" not in codes)

    print("\n4) High leverage position (15x)")
    _patch(FakeCli(positions=[{"instId": "BTC-USDT-SWAP", "side": "long", "lever": "15", "upl": "-50"}]))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("HIGH_LEVERAGE flag raised", "HIGH_LEVERAGE" in codes)
    check("risk_score > 0", report.risk_score > 0)

    # NOTE: fixture shape below is the REAL documented response shape --
    # notional/longShortRatio are nested sub-objects, per
    # skills/okx-cex-smartmoney/references/signal-commands.md. An earlier
    # version of this fixture (and the code) had these fields flat/top-level,
    # which meant the divergence check silently never fired against the
    # real CLI even though the old fixture-based test still "passed" --
    # it was validating the same wrong assumption, not the real contract.
    print("\n5) Smart-money divergence (account short, pool 80% long, thick pool)")
    _patch(FakeCli(
        positions=[{"instId": "BTC-USDT-SWAP", "side": "short", "lever": "2"}],
        smartmoney={"BTC": {"data": [{
            "ccy": "BTC-USDT-SWAP",
            "notional": {"netNotionalUsdt": "500000"},
            "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
        }]}},
    ))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("SMART_MONEY_DIVERGENCE flag raised", "SMART_MONEY_DIVERGENCE" in codes)

    print("\n6) Thin smart-money pool is ignored (avoids noisy flags)")
    _patch(FakeCli(
        positions=[{"instId": "BTC-USDT-SWAP", "side": "short", "lever": "2"}],
        smartmoney={"BTC": {"data": [{
            "ccy": "BTC-USDT-SWAP",
            "notional": {"netNotionalUsdt": "500"},  # below DIVERGENCE_MIN_NOTIONAL_USDT
            "longShortRatio": {"weightedLongRatio": "0.8", "weightedShortRatio": "0.2"},
        }]}},
    ))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("no divergence flag on thin pool", "SMART_MONEY_DIVERGENCE" not in codes)

    print("\n7) Lookalike ticker doesn't steal the match (BTCUP-USDT-SWAP vs BTC-USDT-SWAP)")
    # Two positions: BTCUP is listed FIRST and is short; the real BTC
    # position is long. With the old startswith() bug, next() would hit
    # "BTCUP-USDT-SWAP".startswith("BTC") == True FIRST and wrongly use its
    # "short" side for the BTC divergence check -- masking a real divergence
    # (a false negative, worse than a false positive for a risk tool). The
    # fix (exact split('-')[0] match) must pick the actual BTC position's
    # "long" side regardless of list order.
    _patch(FakeCli(
        positions=[
            {"instId": "BTCUP-USDT-SWAP", "side": "short", "lever": "2"},
            {"instId": "BTC-USDT-SWAP", "side": "long", "lever": "2"},
        ],
        smartmoney={"BTC": {"data": [{
            "ccy": "BTC-USDT-SWAP",
            "notional": {"netNotionalUsdt": "500000"},
            "longShortRatio": {"weightedLongRatio": "0.2", "weightedShortRatio": "0.8"},
        }]}},
    ))
    report = await auditor.run_audit(demo=True)
    codes = [f.code for f in report.flags]
    check("BTC divergence correctly detected despite BTCUP listed first",
          "SMART_MONEY_DIVERGENCE" in codes)

    print()
    if failures:
        print(f"ALL CHECKS DID NOT PASS — {len(failures)} failure(s): {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
