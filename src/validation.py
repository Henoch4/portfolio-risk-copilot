"""
Strategy validation pipeline (walk-forward + PBO + return-quality metrics).

The gate between "a curator thinks this looks good" and "user's capital is on
the line". Implements:

  1. Walk-forward validation (not k-fold CV, which violates time ordering)
  2. Held-out out-of-sample split + Probability of Backtest Overfitting (PBO)
  3. Sharpe, CAGR, max drawdown, Calmar ratio
  4. A hard bar: the strategy does not clear for paper trading with Calmar < 1
     or PBO > 0.5

Ported from the sibling `trading_system` MVP (validation/backtest.py) so the
live agent's signal set can be validated before it is trusted. Pure numpy —
no new dependencies.
"""
from __future__ import annotations

import numpy as np


PERIODS_PER_YEAR = 3 * 365  # 8h funding periods


def returns_to_equity_curve(returns: np.ndarray, start_equity: float = 1.0) -> np.ndarray:
    return start_equity * np.cumprod(1 + returns)


def sharpe_ratio(returns: np.ndarray, periods_per_year: int = PERIODS_PER_YEAR) -> float:
    std = float(np.std(returns))
    if std < 1e-12:  # effectively zero variance -- avoid blowing up on float noise
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(periods_per_year))


def cagr(returns: np.ndarray, periods_per_year: int = PERIODS_PER_YEAR) -> float:
    equity = returns_to_equity_curve(returns)
    n_periods = len(returns)
    if n_periods == 0 or equity[-1] <= 0:
        return -1.0
    years = n_periods / periods_per_year
    return float(equity[-1] ** (1 / years) - 1) if years > 0 else 0.0


def max_drawdown(returns: np.ndarray) -> float:
    equity = returns_to_equity_curve(returns)
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    return float(drawdowns.min())  # negative number


def calmar_ratio(returns: np.ndarray, periods_per_year: int = PERIODS_PER_YEAR) -> float:
    mdd = max_drawdown(returns)
    if mdd == 0:
        # No drawdown: Calmar is undefined/infinite. A finite cap keeps the
        # report JSON-serializable (inf is not) while still clearing any bar.
        return 1e9
    return float(cagr(returns, periods_per_year) / abs(mdd))


def walk_forward_windows(n_obs: int, n_folds: int = 6, train_frac: float = 0.7):
    """
    Yield (train_slice, test_slice) index tuples that never let test data
    precede train data in time and never overlap -- the property k-fold CV
    violates for time series.
    """
    fold_size = n_obs // n_folds
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else n_obs
        window = slice(start, end)
        window_len = end - start
        split = start + int(window_len * train_frac)
        yield slice(start, split), slice(split, end)


def probability_of_backtest_overfitting(strategy_variants_is_sharpe: list[float],
                                        strategy_variants_oos_sharpe: list[float]) -> float:
    """
    Simplified PBO (Bailey et al.): for each variant, check whether the
    in-sample-best-ranked variant also ranks in the bottom half out-of-sample.
    A full CSCV implementation partitions data into combinatorial subsets;
    this compact approximation is sufficient for the MVP validation gate.
    """
    n = len(strategy_variants_is_sharpe)
    if n < 2:
        return 0.0

    is_ranks = np.argsort(np.argsort(strategy_variants_is_sharpe))
    oos_ranks = np.argsort(np.argsort(strategy_variants_oos_sharpe))

    best_is_idx = int(np.argmax(is_ranks))
    oos_rank_of_best = float(oos_ranks[best_is_idx])

    # Fraction of the search family the IS-best beat out-of-sample.
    frac_better_than_oos = oos_rank_of_best / (n - 1)
    # PBO is the chance the IS-best underperforms the family's median OOS.
    # IS-best at the OOS corner => pbo -> 1 (overfit); IS-best still OOS-best
    # => pbo -> 0. Clip to a well-formed probability even with ties.
    pbo = 1.0 - frac_better_than_oos
    return float(np.clip(pbo, 0.0, 1.0))


def evaluate_parameter_grid(returns_by_param: dict[str, np.ndarray], oos_returns_by_param: dict[str, np.ndarray]) -> dict:
    """Tracks every combination tried and flags the PBO risk past 50 combos."""
    n_combos = len(returns_by_param)
    warning = None
    if n_combos > 50:
        warning = (
            f"{n_combos} parameter combinations tested against this dataset -- "
            f"PBO risk rises sharply past 50."
        )

    is_sharpes = [sharpe_ratio(r) for r in returns_by_param.values()]
    oos_sharpes = [sharpe_ratio(r) for r in oos_returns_by_param.values()]
    pbo = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)

    return {
        "n_combinations_tested": n_combos,
        "warning": warning,
        "pbo": pbo,
        "pbo_pass": pbo <= 0.5,
    }


def validation_report(returns: np.ndarray, oos_returns: np.ndarray,
                      param_grid_returns: dict[str, np.ndarray] | None = None,
                      param_grid_oos_returns: dict[str, np.ndarray] | None = None,
                      calmar_bar: float = 1.0) -> dict:
    """Return a full validation report: in/OOS metrics, Calmar bar, PBO, and
    the `cleared_for_paper_trading` boolean gate."""
    report: dict = {
        "in_sample": {
            "cagr": cagr(returns),
            "sharpe": sharpe_ratio(returns),
            "max_drawdown": max_drawdown(returns),
            "calmar": calmar_ratio(returns),
        },
        "out_of_sample": {
            "cagr": cagr(oos_returns),
            "sharpe": sharpe_ratio(oos_returns),
            "max_drawdown": max_drawdown(oos_returns),
            "calmar": calmar_ratio(oos_returns),
        },
    }

    report["calmar_bar"] = calmar_bar
    # A strategy that never traded OOS has an all-zero return series, zero
    # drawdown, and calmar_ratio's 1e9 cap for that case -- which would
    # vacuously "clear" the bar. Found in the 2026-08-22 real-data gate run
    # (reports/validation-gate-2026-08-22.md): the funding contrarian fired
    # 0 times on BTC/ETH/BNB in 2 years and was declared PASS. Cleared must
    # mean "evidence of edge", and a never-traded OOS stream is no evidence.
    has_oos_evidence = bool(np.any(oos_returns != 0))
    report["has_oos_evidence"] = has_oos_evidence
    report["passes_calmar_bar"] = has_oos_evidence and (
        report["out_of_sample"]["calmar"] >= calmar_bar
    )

    if param_grid_returns and param_grid_oos_returns:
        report["pbo_analysis"] = evaluate_parameter_grid(param_grid_returns, param_grid_oos_returns)
    else:
        report["pbo_analysis"] = None

    report["cleared_for_paper_trading"] = bool(
        report["passes_calmar_bar"]
        and (report["pbo_analysis"] is None or report["pbo_analysis"]["pbo_pass"])
    )
    return report