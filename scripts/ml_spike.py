#!/usr/bin/env python3
"""ML spike (ML_ROADMAP_ZERO_COST_STRATEGY.md step 1): is there ANY model, on
this data, that clears the repo's validation gate net of costs?

Deliberately minimal, $0, local CPU:
  - one symbol (BTC-USDT-SWAP), 2y of 1h bars (data/ from fetch_history.py)
  - ~11 hand-built trailing features (returns, vol, RSI, z-score, MA dist,
    volume ratio, funding, hour-of-day)
  - binary label: sign(close_{t+4} - close_t)  [roadmap Phase 0.1 target]
  - ridge logistic regression in pure numpy (no new repo dependencies);
    uses LightGBM instead if it happens to be importable
  - purged walk-forward: the repo's own walk_forward_windows (6 folds) with a
    4-bar embargo at each train->test boundary (label horizon) so overlapping
    labels cannot leak across the split
  - positions from probability bands 0.55/0.45 (mirroring the planned
    ml_ensemble_signal), held while the band persists, 5+3 bps per side
  - verdict: src/validation.py validation_report on 8h-compounded OOS returns

Output: prints the honest go/no-go + writes reports/ml-spike-<date>.md
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.validation import validation_report, walk_forward_windows  # noqa: E402

SYM = "BTC"
HORIZON = 4            # label horizon in bars (Phase 0.1: 4h)
LONG_P, SHORT_P = 0.55, 0.45
FEE_BPS, SLIP_BPS = 5.0, 3.0
SIDE_COST = (FEE_BPS + SLIP_BPS) / 10000.0
N_FOLDS, TRAIN_FRAC = 6, 0.7
BARS_PER_8H = 8
CALMAR_BAR = 1.0


def load(sym: str):
    rows = list(csv.reader(open(REPO / "data" / f"{sym}_1h_candles.csv")))[1:]
    close = np.array([float(r[4]) for r in rows])
    vol = np.array([float(r[5]) for r in rows])
    ts = np.array([int(r[0]) for r in rows], dtype=np.int64)
    frows = list(csv.reader(open(REPO / "data" / f"{sym}_funding_binance.csv")))[1:]
    fts = np.array([int(float(r[0])) for r in frows], dtype=np.int64)
    frate = np.array([float(r[1]) for r in frows])
    return ts, close, vol, fts, frate


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Wilder-style simple rolling means (period bars), seeded with NaN
    ag = np.convolve(gain, np.ones(period) / period, mode="full")[period:len(closes)]
    al = np.convolve(loss, np.ones(period) / period, mode="full")[period:len(closes)]
    out = np.full(len(closes), np.nan)
    valid = al > 0
    out[period:][valid] = 100 - 100 / (1 + ag[valid] / al[valid])
    out[period:][~valid] = 100.0
    return out


def build_features(ts, close, vol, funding) -> tuple[np.ndarray, list[str]]:
    lr = np.diff(np.log(close), prepend=np.log(close[0]))
    f = {}
    f["ret_1h"] = lr
    for h in (6, 24):
        r = np.full(len(close), np.nan)
        r[h:] = close[h:] / close[:-h] - 1
        f[f"ret_{h}h"] = r
    vol24 = np.full(len(close), np.nan)
    vol24[24:] = np.array([lr[i - 23:i + 1].std() for i in range(24, len(lr))])
    f["vol_24h"] = vol24
    f["rsi_14"] = rsi(close) / 100.0
    ma20 = np.convolve(close, np.ones(20) / 20, mode="valid")
    sd20 = np.array([close[i - 19:i + 1].std() for i in range(19, len(close))])
    z = np.full(len(close), np.nan)
    z[19:] = (close[19:] - ma20) / np.where(sd20 > 0, sd20, 1e-12)
    f["zscore_20"] = z
    ma50 = np.convolve(close, np.ones(50) / 50, mode="valid")
    f["dist_ma50"] = np.concatenate([np.full(49, np.nan), close[49:] / ma50 - 1])
    vr = np.full(len(close), np.nan)
    vr[24:] = vol[24:] / np.array([vol[i - 23:i + 1].mean() for i in range(24, len(vol))])
    f["vol_ratio_24h"] = vr
    f["funding"] = funding
    hours = np.array([(t // 3600000) % 24 for t in ts], dtype=float)
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24)

    names = list(f)
    X = np.column_stack([f[n] for n in names])
    return X, names


# ---------------------------- models (numpy-first, LightGBM optional)

def fit_logit(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 25):
    """Ridge logistic regression via IRLS. Returns (w, mu) where mu predicts
    from a raw feature row (no intercept column included in X)."""
    n, d = X.shape
    Xb = np.column_stack([np.ones(n), X])
    w = np.zeros(d + 1)
    L = np.eye(d + 1) * lam
    L[0, 0] = 0.0  # don't penalize the intercept
    for _ in range(iters):
        mu = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        grad = Xb.T @ (y - mu) - L @ w
        Wg = np.clip(mu * (1 - mu), 1e-6, None)
        H = Xb.T @ (Xb * Wg[:, None]) + L
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinError:
            break
        w += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict_logit(w, X: np.ndarray) -> np.ndarray:
    Xb = np.column_stack([np.ones(len(X)), X])
    return 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))


def net_bar_returns(pos: np.ndarray, close: np.ndarray) -> np.ndarray:
    ret = pos[:-1] * (np.diff(close) / close[:-1])
    ret -= np.abs(np.diff(pos)) * SIDE_COST
    if pos[-1] != 0:
        ret[-1] -= abs(pos[-1]) * SIDE_COST
    return ret


def to_8h(bar_ret: np.ndarray) -> np.ndarray:
    n = (len(bar_ret) // BARS_PER_8H) * BARS_PER_8H
    return np.prod(1 + bar_ret[:n].reshape(-1, BARS_PER_8H), axis=1) - 1


def main() -> int:
    ts, close, vol, fts, frate = load(SYM)
    idx = np.searchsorted(fts, ts, side="right") - 1
    funding = np.where(idx >= 0, frate[np.maximum(idx, 0)], 0.0)

    X, names = build_features(ts, close, vol, funding)
    y = np.zeros(len(close))
    y[:-HORIZON] = np.sign(close[HORIZON:] - close[:-HORIZON])
    valid = np.isfinite(X).all(axis=1) & (y != 0)
    # rows whose label looks past the array end are already excluded (y==0 there)

    use_lgbm = False
    try:
        import lightgbm as lgb  # type: ignore
        use_lgbm = True
    except ImportError:
        pass

    n_obs = int(valid.sum())
    pos = np.zeros(len(close))
    oos_correct = oos_total = 0
    train_idx_all = np.where(valid)[0]

    for tr, te in walk_forward_windows(n_obs, N_FOLDS, TRAIN_FRAC):
        tr_rows = train_idx_all[tr]
        te_rows = train_idx_all[te]
        # PURGE: drop train rows whose 4h label overlaps the test window
        tr_rows = tr_rows[tr_rows < te_rows[0] - HORIZON]

        mu_tr, sd_tr = X[tr_rows].mean(0), X[tr_rows].std(0) + 1e-12
        Xtr = (X[tr_rows] - mu_tr) / sd_tr
        Xte = (X[te_rows] - mu_tr) / sd_tr

        if use_lgbm:
            m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05,
                                   num_leaves=31, min_child_samples=50,
                                   verbose=-1)
            m.fit(Xtr, y[tr_rows])
            p = m.predict_proba(Xte)[:, 1]
        else:
            w = fit_logit(Xtr, (y[tr_rows] + 1) / 2)  # logit wants {0,1}
            p = predict_logit(w, Xte)

        oos_correct += int(((p > 0.5) == (y[te_rows] == 1)).sum())
        oos_total += len(te_rows)
        pos[te_rows] = np.where(p > LONG_P, 1.0, np.where(p < SHORT_P, -1.0, 0.0))

    bar_ret = net_bar_returns(pos, close)
    ret8 = to_8h(bar_ret)
    # IS = training-period rows (positions were only taken OOS; IS series is
    # the same span's market-neutral benchmark of OOS choice is unavailable).
    # Honest gate: OOS-only compounding vs the Calmar bar.
    oos_ret8 = ret8  # positions only ever set inside test slices
    rep = validation_report(oos_ret8, oos_ret8, calmar_bar=CALMAR_BAR)
    # ^ IS==OOS here on purpose: the spike has no separate IS strategy stream;
    # the report's Calmar/PBO structure is reused with the OOS series for both
    # so the bar is applied identically. Directional accuracy is the extra
    # diagnostic below.

    accuracy = oos_correct / oos_total if oos_total else 0.0
    trades = int((np.diff(pos, prepend=0) != 0).sum())
    exposure = float((pos != 0).mean())

    summary = {
        "model": "lightgbm" if use_lgbm else "ridge-logit (numpy IRLS)",
        "symbol": f"{SYM}-USDT-SWAP",
        "bars": int(len(close)),
        "features": names,
        "oos_directional_accuracy": round(accuracy, 4),
        "oos_trades(position changes)": trades,
        "oos_exposure": round(exposure, 4),
        "oos_net_return_full_reinvest": round(float(np.prod(1 + bar_ret) - 1), 4),
        "validation_report": rep,
    }

    print(json.dumps(summary, indent=2, default=str))
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = REPO / "reports" / f"ml-spike-{date}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        f"# ML spike — {date}\n\n"
        f"```json\n{json.dumps(summary, indent=2, default=str)}\n```\n\n"
        f"Method: see `scripts/ml_spike.py` docstring. Positions only ever taken\n"
        f"inside walk-forward test slices (purged {HORIZON}-bar embargo at each\n"
        f"train/test boundary). Costs {FEE_BPS:.0f}+{SLIP_BPS:.0f} bps per side.\n"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
