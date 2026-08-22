# Validation gate run — 2026-08-22

First full run of `src/validation.py`'s walk-forward / Calmar / PBO gate
against real OKX history (mainnet-roadmap.md Phase 2). Data: ~2y of 1h
swap candles + settled funding for BTC/ETH/SOL/BNB-USDT-SWAP via
`scripts/fetch_history.py` (unauthenticated public endpoints).

Costs: 5 bps fee + 3 bps slippage per side; funding income while holding ignored (conservative for the contrarian). Calmar bar 1 on 8h-compounded returns, 6-fold walk-forward, train 70%. Entries require the repo's own `is_tradeable` (60% confidence floor).

## Per-symbol × strategy

| sym | strategy | trades | win% | net ret | IS Sharpe | OOS Sharpe | OOS Calmar | gate |
|---|---|---|---|---|---|---|---|---|
| BTC | mean_reversion | 647 | 36% | -73.1% | -5.050 | -4.133 | -1.381 | FAIL |
| BTC | momentum | 1602 | 25% | -93.5% | -3.330 | -3.735 | -1.190 | FAIL |
| BTC | funding_rate | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| BTC | ensemble | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| ETH | mean_reversion | 536 | 45% | -62.9% | -2.589 | -4.350 | -1.413 | FAIL |
| ETH | momentum | 1388 | 27% | -70.7% | -0.745 | -0.775 | -0.990 | FAIL |
| ETH | funding_rate | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| ETH | ensemble | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| SOL | mean_reversion | 491 | 51% | -54.2% | -2.034 | -2.297 | -1.370 | FAIL |
| SOL | momentum | 1402 | 30% | -88.7% | -0.942 | -2.565 | -1.145 | FAIL |
| SOL | funding_rate | 1 | 100% | +3.3% | 0.328 | 0.000 | 1000000000.000 | PASS |
| SOL | ensemble | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| BNB | mean_reversion | 635 | 42% | -63.4% | -4.236 | -3.435 | -1.370 | FAIL |
| BNB | momentum | 1608 | 26% | -94.2% | -2.605 | -4.041 | -1.183 | FAIL |
| BNB | funding_rate | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |
| BNB | ensemble | 0 | 0% | +0.0% | 0.000 | 0.000 | 1000000000.000 | PASS |

## Portfolio headline — LIVE config (funding contrarian, equal weight)

```json
{
  "in_sample": {
    "cagr": 0.005697112244665847,
    "sharpe": 0.3360300862006836,
    "max_drawdown": -0.01453997296682108,
    "calmar": 0.3918241290864948
  },
  "out_of_sample": {
    "cagr": 0.0,
    "sharpe": 0.0,
    "max_drawdown": 0.0,
    "calmar": 1000000000.0
  },
  "calmar_bar": 1.0,
  "passes_calmar_bar": true,
  "pbo_analysis": null,
  "cleared_for_paper_trading": true
}
```

## Funding input: venue proxy check

OKX's public funding history caps at ~3 months, so the 2-year funding input
is Binance USDT-perp funding (cross-venue proxy). Agreement on the overlap:

- BTC: {'overlap_periods': 290, 'corr': 0.6187736347769103, 'threshold_agreement': 1.0}
- ETH: {'overlap_periods': 290, 'corr': 0.6397033564964132, 'threshold_agreement': 1.0}
- SOL: {'overlap_periods': 290, 'corr': 0.8479435137766678, 'threshold_agreement': 1.0}
- BNB: {'overlap_periods': 289, 'corr': 0.4509823443789063, 'threshold_agreement': 1.0}

## PBO parameter grids

### mean_reversion
- BTC: pbo=0.09 (pass, 12 combos)
- ETH: pbo=0.00 (pass, 12 combos)
- SOL: pbo=0.18 (pass, 12 combos)
- BNB: pbo=0.09 (pass, 12 combos)
### momentum
- BTC: pbo=0.12 (pass, 9 combos)
- ETH: pbo=0.12 (pass, 9 combos)
- SOL: pbo=0.50 (pass, 9 combos)
- BNB: pbo=0.62 (FAIL, 9 combos)
### funding_rate
- BTC: pbo=0.20 (pass, 6 combos)
- ETH: pbo=1.00 (FAIL, 6 combos)
- SOL: pbo=1.00 (FAIL, 6 combos)
- BNB: pbo=0.20 (pass, 6 combos)
## Verdict — honest reading of this run

1. **Mean reversion and momentum do not have edge — they have anti-edge.**
   Net of 5+3 bps/side costs, every symbol loses heavily (BTC MR -73%, BNB
   momentum -94%). This is worse than the roadmap's "wash out to near-zero"
   expectation. The low PBO values are not good news: the parameter grid is
   consistently bad, i.e. the loss is structural, not bad luck in one variant.

2. **The LIVE configuration (funding contrarian only, per
   config/profiles.yaml) is effectively inert.** At the production 0.001
   threshold the signal fired 0 times on BTC/ETH/BNB in 2 years (1 SOL trade,
   +3.3%). A 2-year backtest with one trade cannot support any live-capital
   claim in either direction.

3. **The gate's "PASS" for funding/ensemble is a vacuous pass and exposes a
   defect in validation.py**: an all-zero (never-traded) OOS return series has
   zero drawdown, `calmar_ratio` caps that at 1e9, and the strategy is
   declared cleared for paper trading. Cleared ≠ has edge. Fixed the same day:
   validation_report now requires at least one nonzero OOS return as evidence
   (regression test in tests/test_validation.py). With the fix, the portfolio
   verdict flips to NOT cleared — honestly reflecting "no evidence", not
   "evidence of profit".

4. **Strategic consequence (answers mainnet-roadmap Phase 2's question and
   unblocks the ML roadmap's precondition):** the existing rule-based signal
   set provides NO validated edge. Any ML signal is therefore joining an
   *empty* ensemble — it would be the only source of edge, and per
   ML_ROADMAP_REVISED.md, Phase 6's graduated live weight must be treated
   with the more conservative interpretation of that doc. The one mechanism
   this run could not evaluate (funding income was deliberately ignored) is
   the delta-neutral funding package: at BTC's 2y funding mean (~0.0038%/8h)
   the collectable carry is ~4%/yr, ~11%/yr at the 0.01% cap — small but real,
   and structurally different from every directional signal tested here.

Caveats: funding input is Binance (proxy for OKX, corr 0.45-0.85, threshold
agreement 100% on the 3-month overlap); funding income while holding ignored;
positions sized at full notional per signal (per-unit-exposure edge, not
account-level PnL); 1h bar closes assumed executable at 5+3 bps/side.
