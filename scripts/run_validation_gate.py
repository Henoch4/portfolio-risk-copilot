#!/usr/bin/env python3
"""Phase 2 validation gate: the repo's real signals on real OKX history.

Runs the production signal functions from src/signals.py bar-by-bar over the
fetched 1h history (scripts/fetch_history.py), net of trading costs, through
src/validation.py's walk-forward / Calmar / PBO gate -- the run that
mainnet-roadmap.md Phase 2 has been waiting on.

Modeling choices (kept conservative where ambiguous):
  - Parametrization is the production default: mean_reversion window=20,
    z=2.0; momentum 5/20 (off in prod, reported separately); funding
    threshold=0.001; regime filter window=50 (main.py default).
  - A position is held while the signal stays tradeable in the same
    direction (entries gated by Signal.is_tradeable, the risk gate's own
    60% confidence floor). Costs are charged per SIDE on every position
    change: fee 5 bps + slippage 3 bps (the roadmap's own cost assumption).
  - Funding payments/income while holding are IGNORED. For the funding
    contrarian this understates its edge (the contrarian side usually
    collects); for price-action strategies it is roughly neutral.
  - Per-bar 1h returns are compounded into 8h periods before
    validation_report() so its 3*365 periods/year annualization is correct.
  - The funding rate known at bar t is the most recently SETTLED rate
    (forward-fill) -- strictly no lookahead, even though OKX announces the
    current period's rate in advance.

Outputs: stdout summary, reports/validation-gate-<date>.md, and
data/validation_returns_<name>.csv (in_sample,out_of_sample) consumable by
scripts/check_validation_gate.py via VALIDATION_RETURNS_PATH.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.signals import (  # noqa: E402
    ensemble_signal,
    funding_rate_signal,
    mean_reversion_signal,
    momentum_signal,
)
from src.validation import (  # noqa: E402
    evaluate_parameter_grid,
    validation_report,
    walk_forward_windows,
)

SYMBOLS = ["BTC", "ETH", "SOL", "BNB"]
FEE_BPS = 5.0
SLIP_BPS = 3.0
SIDE_COST = (FEE_BPS + SLIP_BPS) / 10000.0
REGIME_WINDOW = 50
CALMAR_BAR = 1.0
BARS_PER_8H = 8
N_FOLDS = 6
TRAIN_FRAC = 0.7
TAIL = 130  # bars of context passed to signal fns (max lookback used + margin)


# ---------------------------------------------------------------- data

def load_candles(sym: str):
    path = REPO / "data" / f"{sym}_1h_candles.csv"
    with open(path, newline="") as f:
        rows = list(csv.reader(f))[1:]
    ts = np.array([int(r[0]) for r in rows], dtype=np.int64)
    close = np.array([float(r[4]) for r in rows])
    vol = np.array([float(r[5]) for r in rows])
    return ts, close, vol


def load_funding(sym: str):
    """2-year funding input: Binance USDT-perp series (OKX's public funding
    history caps at ~3 months). Cross-venue proxy -- main() reports the
    OKX/Binance agreement on the overlapping window."""
    path = REPO / "data" / f"{sym}_funding_binance.csv"
    with open(path, newline="") as f:
        rows = list(csv.reader(f))[1:]
    ts = np.array([int(float(r[0])) for r in rows], dtype=np.int64)
    rate = np.array([float(r[1]) for r in rows])
    return ts, rate


def load_funding_okx(sym: str):
    path = REPO / "data" / f"{sym}_funding.csv"
    with open(path, newline="") as f:
        rows = list(csv.reader(f))[1:]
    ts = np.array([int(float(r[0])) for r in rows], dtype=np.int64)
    rate = np.array([float(r[1]) for r in rows])
    return ts, rate


def venue_agreement(sym: str) -> dict:
    """OKX vs Binance funding on their overlapping window: correlation and
    agreement on the |rate| >= 0.001 production threshold."""
    ots, orate = load_funding_okx(sym)
    bts, brate = load_funding(sym)
    lo = max(ots[0], bts[0])
    hi = min(ots[-1], bts[-1])
    om = (ots >= lo) & (ots <= hi)
    bm = (bts >= lo) & (bts <= hi)
    o, b = orate[om], brate[bm]
    if len(o) < 10 or len(o) != len(b):
        return {"overlap_periods": int(min(len(o), len(b)))}
    corr = float(np.corrcoef(o, b)[0, 1])
    thr_agree = float(np.mean((np.abs(o) >= 0.001) == (np.abs(b) >= 0.001)))
    return {"overlap_periods": int(len(o)), "corr": corr,
            "threshold_agreement": thr_agree}


def funding_ffill(bar_ts: np.ndarray, fund_ts: np.ndarray, rate: np.ndarray) -> np.ndarray:
    """Rate known at each bar = most recently settled rate (no lookahead)."""
    idx = np.searchsorted(fund_ts, bar_ts, side="right") - 1
    out = np.zeros(len(bar_ts))
    valid = idx >= 0
    out[valid] = rate[idx[valid]]
    return out


# ---------------------------------------------------------------- signals

def positions_all_strategies(sym: str, close: np.ndarray, vol: np.ndarray,
                             funding: np.ndarray) -> dict[str, np.ndarray]:
    """Per-bar positions (+1/0/-1) for each strategy, using the repo's real
    signal functions with production parametrization. Entries require
    Signal.is_tradeable (the 60% confidence floor)."""
    n = len(close)
    prices_all = close.tolist()
    pd_all = [{"close": float(c), "volume": float(v)} for c, v in zip(close, vol)]

    pos = {
        "mean_reversion": np.zeros(n),
        "momentum": np.zeros(n),
        "funding_rate": np.zeros(n),
        "ensemble": np.zeros(n),
    }
    for t in range(n):
        lo = max(0, t - TAIL)
        # every signal function reads only the tail of its input, so passing
        # a bounded tail is semantically identical to the full history
        tail_prices = prices_all[lo:t + 1]
        tail_pd = pd_all[lo:t + 1]

        mr = mean_reversion_signal(sym, tail_prices, window=20, z_threshold=2.0,
                                   regime_window=REGIME_WINDOW)
        mom = momentum_signal(sym, tail_pd, short_window=5, long_window=20,
                              regime_window=REGIME_WINDOW)
        fund = funding_rate_signal(sym, float(funding[t]), threshold=0.001)
        ens = ensemble_signal(sym, [mr, mom, fund])

        for name, sig in (("mean_reversion", mr), ("momentum", mom),
                          ("funding_rate", fund), ("ensemble", ens)):
            if sig.is_tradeable:
                pos[name][t] = 1.0 if sig.direction == "LONG" else -1.0
    return pos


def positions_param_variant(kind: str, sym: str, close: np.ndarray, vol: np.ndarray,
                            funding: np.ndarray, **params) -> np.ndarray:
    """Same loop for one parameterized variant (PBO grid)."""
    n = len(close)
    prices_all = close.tolist()
    pd_all = [{"close": float(c), "volume": float(v)} for c, v in zip(close, vol)]
    pos = np.zeros(n)
    for t in range(n):
        lo = max(0, t - TAIL)
        if kind == "mean_reversion":
            sig = mean_reversion_signal(sym, prices_all[lo:t + 1],
                                        window=params["window"],
                                        z_threshold=params["z"],
                                        regime_window=REGIME_WINDOW)
        elif kind == "momentum":
            sig = momentum_signal(sym, pd_all[lo:t + 1],
                                  short_window=params["short"],
                                  long_window=params["long"],
                                  regime_window=REGIME_WINDOW)
        else:  # funding
            sig = funding_rate_signal(sym, float(funding[t]),
                                      threshold=params["threshold"])
        if sig.is_tradeable:
            pos[t] = 1.0 if sig.direction == "LONG" else -1.0
    return pos


# ---------------------------------------------------------------- returns

def net_bar_returns(pos: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Per-interval strategy returns net of per-side costs on position changes.

    ret[j] is the P&L over interval (bar j, bar j+1]: held position pos[j]
    earns the price move; the cost of the position change made AT bar j+1
    (|pos[j+1]-pos[j]| sides) is charged in the same interval."""
    price_ret = np.diff(close) / close[:-1]
    ret = pos[:-1] * price_ret
    ret -= np.abs(np.diff(pos)) * SIDE_COST
    if pos[-1] != 0:
        ret[-1] -= abs(pos[-1]) * SIDE_COST  # close the still-open final position
    return ret


def episode_stats(pos: np.ndarray, close: np.ndarray) -> dict:
    """Per-trade stats: a trade = maximal run of constant nonzero position.
    Each trade pays exactly one entry + one exit side of costs; its P&L is
    the compounded move over its intervals."""
    price_ret = np.diff(close) / close[:-1]
    pnl = pos[:-1] * price_ret
    n = len(pos)
    trades = wins = 0
    eq = 1.0
    j = 0
    while j < n:
        if pos[j] == 0:
            j += 1
            continue
        k = j
        while k + 1 < n and pos[k + 1] == pos[j]:
            k += 1
        seg = pnl[j:min(k + 1, n - 1)]
        ep = float(seg.sum()) - 2 * SIDE_COST
        trades += 1
        if ep > 0:
            wins += 1
        eq *= (1 + ep)
        j = k + 1
    return {"trades": trades,
            "win_rate": wins / trades if trades else 0.0,
            "net_return": eq - 1.0}


def to_8h(bar_ret: np.ndarray) -> np.ndarray:
    n = (len(bar_ret) // BARS_PER_8H) * BARS_PER_8H
    chunks = bar_ret[:n].reshape(-1, BARS_PER_8H)
    return np.prod(1 + chunks, axis=1) - 1


def walk_forward_is_oos(ret8: np.ndarray):
    is_parts, oos_parts = [], []
    for tr, te in walk_forward_windows(len(ret8), N_FOLDS, TRAIN_FRAC):
        is_parts.append(ret8[tr])
        oos_parts.append(ret8[te])
    return np.concatenate(is_parts), np.concatenate(oos_parts)


def gate_for_returns(ret8: np.ndarray) -> dict:
    is_r, oos_r = walk_forward_is_oos(ret8)
    return validation_report(is_r, oos_r, calmar_bar=CALMAR_BAR)


def fmt(x) -> str:
    return f"{x:.3f}" if isinstance(x, (int, float)) else str(x)


# ---------------------------------------------------------------- run

def main() -> int:
    per_symbol: dict[str, dict[str, dict]] = {}
    detail_lines: list[str] = []
    grid_results: dict[str, dict] = {}

    mr_grid = [{"window": w, "z": z} for w in (10, 20, 40) for z in (1.5, 2.0, 2.5, 3.0)]
    mom_grid = [{"short": s, "long": l} for s in (3, 5, 8) for l in (20, 40, 80)]
    fund_grid = [{"threshold": t}
                 for t in (0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005)]

    for sym in SYMBOLS:
        t0 = time.time()
        ts, close, vol = load_candles(sym)
        fts, frate = load_funding(sym)
        funding = funding_ffill(ts, fts, frate)
        span = f"{datetime.fromtimestamp(ts[0]/1000, tz=timezone.utc):%Y-%m-%d}.." \
               f"{datetime.fromtimestamp(ts[-1]/1000, tz=timezone.utc):%Y-%m-%d}"

        positions = positions_all_strategies(sym, close, vol, funding)
        per_symbol[sym] = {"ts": ts, "close": close}
        for name, pos in positions.items():
            bar_ret = net_bar_returns(pos, close)
            stats = episode_stats(pos, close)
            rep = gate_for_returns(to_8h(bar_ret))
            per_symbol[sym][name] = {"bar_ret": bar_ret, "stats": stats, "report": rep}
            detail_lines.append(
                f"| {sym} | {name} | {stats['trades']} | {stats['win_rate']:.0%} "
                f"| {stats['net_return']:+.1%} "
                f"| {fmt(rep['in_sample']['sharpe'])} "
                f"| {fmt(rep['out_of_sample']['sharpe'])} "
                f"| {fmt(rep['out_of_sample']['calmar'])} "
                f"| {'PASS' if rep['cleared_for_paper_trading'] else 'FAIL'} |"
            )

        for kind, grid, pname in (
            ("mean_reversion", mr_grid, lambda p: f"w{p['window']}_z{p['z']}"),
            ("momentum", mom_grid, lambda p: f"s{p['short']}_l{p['long']}"),
            ("funding_rate", fund_grid, lambda p: f"thr{p['threshold']}"),
        ):
            is_by_p, oos_by_p = {}, {}
            for p in grid:
                pos = positions_param_variant(kind, sym, close, vol, funding, **p)
                r8 = to_8h(net_bar_returns(pos, close))
                is_r, oos_r = walk_forward_is_oos(r8)
                is_by_p[pname(p)] = is_r
                oos_by_p[pname(p)] = oos_r
            grid_results.setdefault(kind, {})[sym] = evaluate_parameter_grid(is_by_p, oos_by_p)

        print(f"{sym}: done in {time.time() - t0:.1f}s "
              f"({len(close)} bars, {span})", flush=True)

    # Portfolio headline: LIVE config (funding contrarian on all 4 symbols),
    # equal weight, aligned on the BTC hourly grid (symbols with shorter or
    # offset history contribute 0 for hours they lack -- flat, not fabricated).
    master_ts = per_symbol["BTC"]["ts"]
    port_bar = np.zeros(len(master_ts) - 1)
    for sym in SYMBOLS:
        ts = per_symbol[sym]["ts"]
        ret = per_symbol[sym]["funding_rate"]["bar_ret"]
        idx = np.searchsorted(master_ts, ts[:-1])  # bar_ret[j] covers (ts[j], ts[j+1]]
        ok = idx < len(port_bar)
        # skip bars whose master interval doesn't line up with this symbol's
        aligned = (master_ts[idx[ok]] == ts[:-1][ok])
        port_bar[idx[ok][aligned]] += ret[ok][aligned]
    port_bar /= len(SYMBOLS)
    port_report = gate_for_returns(to_8h(port_bar))

    # ---- report ----
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# Validation gate run — {date}")
    lines.append("")
    lines.append("First full run of `src/validation.py`'s walk-forward / Calmar / PBO gate")
    lines.append("against real OKX history (mainnet-roadmap.md Phase 2). Data: ~2y of 1h")
    lines.append(f"swap candles + settled funding for {'/'.join(SYMBOLS)}-USDT-SWAP via")
    lines.append("`scripts/fetch_history.py` (unauthenticated public endpoints).")
    lines.append("")
    lines.append(f"Costs: {FEE_BPS:.0f} bps fee + {SLIP_BPS:.0f} bps slippage per side; funding "
                 "income while holding ignored (conservative for the contrarian). "
                 f"Calmar bar {CALMAR_BAR:.0f} on 8h-compounded returns, {N_FOLDS}-fold "
                 f"walk-forward, train {TRAIN_FRAC:.0%}. Entries require the repo's own "
                 "`is_tradeable` (60% confidence floor).")
    lines.append("")
    lines.append("## Per-symbol × strategy")
    lines.append("")
    lines.append("| sym | strategy | trades | win% | net ret | IS Sharpe | OOS Sharpe | OOS Calmar | gate |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    lines.extend(detail_lines)
    lines.append("")
    lines.append("## Portfolio headline — LIVE config (funding contrarian, equal weight)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(port_report, indent=2, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Funding input: venue proxy check")
    lines.append("")
    lines.append("OKX's public funding history caps at ~3 months, so the 2-year funding input")
    lines.append("is Binance USDT-perp funding (cross-venue proxy). Agreement on the overlap:")
    lines.append("")
    for sym in SYMBOLS:
        lines.append(f"- {sym}: {venue_agreement(sym)}")
    lines.append("")

    lines.append("## PBO parameter grids")
    lines.append("")
    for kind in ("mean_reversion", "momentum", "funding_rate"):
        lines.append(f"### {kind}")
        for sym in SYMBOLS:
            g = grid_results[kind][sym]
            lines.append(f"- {sym}: pbo={g['pbo']:.2f} "
                         f"({'pass' if g['pbo_pass'] else 'FAIL'}, "
                         f"{g['n_combinations_tested']} combos)")
    lines.append("")

    out = REPO / "reports" / f"validation-gate-{date}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"\nwrote {out}")

    # deploy-gate CSV for the headline (live config) portfolio
    is_r, oos_r = walk_forward_is_oos(to_8h(port_bar))
    csv_path = REPO / "data" / "validation_returns_portfolio_funding.csv"
    csv_path.parent.mkdir(exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["in_sample", "out_of_sample"])
        for a, b in zip(is_r, oos_r):
            w.writerow([f"{a:.8f}", f"{b:.8f}"])
    print(f"wrote {csv_path}")
    print(f"\nDeploy gate verdict (live config portfolio): "
          f"cleared_for_paper_trading = {port_report['cleared_for_paper_trading']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
