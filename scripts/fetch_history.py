#!/usr/bin/env python3
"""Fetch free historical market data for the Phase 2 validation gate.

Unauthenticated OKX public endpoints only (the venue the bot actually
trades), so no key handling and no cost:

  - /api/v5/market/history-candles        -> data/{SYM}_1h_candles.csv
  - /api/v5/public/funding-rate-history   -> data/{SYM}_funding.csv

Candles: 1h bars for the swap instruments (BTC/ETH/SOL/BNB-USDT-SWAP),
~2 years by default, paginated 100/request. Funding: 8h realizations for
the same instruments. Files are plain CSV so the gate script stays
stdlib+numpy (pandas is not a repo dependency).

Cached: a symbol whose CSV already exists is skipped unless --refresh.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

SYMBOLS = ["BTC", "ETH", "SOL", "BNB"]  # roadmap Phase 2 asset list
BASE = "https://www.okx.com"
RATE_SLEEP_S = 0.15  # history-candles limit: 20 req / 2 s per IP
MAX_RETRIES = 4


def _get(path: str, params: dict[str, str]) -> list:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}?{qs}"
    for attempt in range(MAX_RETRIES):
        try:
            # OKX 403s the default python-urllib UA on public endpoints
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AuditTrailTrader-validation/1.0",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("code") != "0":
                # rate limit / transient server error -> back off and retry
                print(f"  okx code={payload.get('code')} msg={payload.get('msg')}; retrying")
                time.sleep(1.0 * (attempt + 1))
                continue
            return payload.get("data", [])
        except Exception as e:  # noqa: BLE001 - network layer, report and retry
            print(f"  request error: {e}; retrying")
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {path} {params}")


def fetch_candles(inst_id: str, bar: str = "1H", years: float = 2.0) -> list[list[str]]:
    """Page backwards through history-candles until `years` back from now.

    Endpoint returns rows newest-first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm].
    `after=<ts>` returns records strictly earlier than ts.
    """
    cutoff_ms = int((time.time() - years * 365 * 24 * 3600) * 1000)
    after = int(time.time() * 1000)
    rows: list[list[str]] = []
    while True:
        page = _get(
            "/api/v5/market/history-candles",
            {"instId": inst_id, "bar": bar, "limit": "100", "after": str(after)},
        )
        if not page:
            break
        rows.extend(page)
        oldest = int(page[-1][0])
        print(f"  {inst_id} {bar}: {len(rows)} bars (back to "
              f"{time.strftime('%Y-%m-%d', time.gmtime(oldest / 1000))})", end="\r")
        if oldest <= cutoff_ms or len(page) < 100:
            break
        after = oldest
        time.sleep(RATE_SLEEP_S)
    print()
    # oldest-first, keep [ts, o, h, l, c, vol]
    rows.sort(key=lambda r: int(r[0]))
    return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]


def fetch_funding(inst_id: str, years: float = 2.0) -> list[list[str]]:
    """Page backwards through funding-rate-history (8h realizations).

    NOTE: OKX caps this endpoint at ~3 months of history. For the 2-year
    gate run, fetch_funding_binance() backfills the rest (cross-venue
    proxy, cross-checked on the overlap by the gate script)."""
    cutoff_ms = int((time.time() - years * 365 * 24 * 3600) * 1000)
    after = int(time.time() * 1000)
    rows: list[list[str]] = []
    while True:
        page = _get(
            "/api/v5/public/funding-rate-history",
            {"instId": inst_id, "limit": "100", "after": str(after)},
        )
        if not page:
            break
        rows.extend(page)
        oldest = int(page[-1]["fundingTime"])
        if oldest <= cutoff_ms or len(page) < 100:
            break
        after = oldest
        time.sleep(RATE_SLEEP_S)
    rows.sort(key=lambda r: int(r["fundingTime"]))
    return [[r["fundingTime"], r["fundingRate"], r.get("realizedRate", "")] for r in rows]


def fetch_funding_binance(symbol: str, years: float = 2.0) -> list[list[str]]:
    """Full-depth funding history from Binance USDT-perp fapi (no auth).

    Proxy for the long OKX history OKX doesn't expose publicly; the gate
    script cross-checks the two venues on their overlapping window."""
    base = "https://fapi.binance.com"
    cutoff_ms = int((time.time() - years * 365 * 24 * 3600) * 1000)
    start = cutoff_ms
    rows: list[list[str]] = []
    while True:
        qs = f"symbol={symbol}&startTime={start}&limit=1000"
        req = urllib.request.Request(
            f"{base}/fapi/v1/fundingRate?{qs}",
            headers={"User-Agent": "AuditTrailTrader-validation/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = json.loads(resp.read().decode())
        if not page:
            break
        rows.extend(page)
        last_ts = int(page[-1]["fundingTime"])
        if last_ts >= int(time.time() * 1000) - 8 * 3600 * 1000 or len(page) < 1000:
            break
        start = last_ts + 1
        time.sleep(RATE_SLEEP_S)
    rows.sort(key=lambda r: int(r["fundingTime"]))
    return [[str(int(r["fundingTime"])), str(r["fundingRate"])] for r in rows]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--refresh", action="store_true", help="refetch even if CSV exists")
    args = ap.parse_args()

    for sym in SYMBOLS:
        swap = f"{sym}-USDT-SWAP"
        candles_csv = DATA / f"{sym}_1h_candles.csv"
        funding_csv = DATA / f"{sym}_funding.csv"
        funding_binance_csv = DATA / f"{sym}_funding_binance.csv"

        if all(p.exists() for p in (candles_csv, funding_csv, funding_binance_csv)) \
                and not args.refresh:
            print(f"{swap}: cached, skipping (use --refresh to refetch)")
            continue

        print(f"{swap}: fetching {args.years}y of history")
        if not candles_csv.exists() or args.refresh:
            bars = fetch_candles(swap, years=args.years)
            if len(bars) < 1000:
                print(f"  WARNING: only {len(bars)} bars returned")
            _write_csv(candles_csv, ["ts", "open", "high", "low", "close", "vol"], bars)

        if not funding_csv.exists() or args.refresh:
            funding = fetch_funding(swap, years=args.years)
            _write_csv(funding_csv, ["ts", "fundingRate", "realizedRate"], funding)

        if not funding_binance_csv.exists() or args.refresh:
            funding_b = fetch_funding_binance(f"{sym}USDT", years=args.years)
            _write_csv(funding_binance_csv, ["ts", "fundingRate"], funding_b)

    return 0


if __name__ == "__main__":
    sys.exit(main())
