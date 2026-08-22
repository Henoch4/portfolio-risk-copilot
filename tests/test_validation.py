"""Unit tests for src/validation.py — strategy validation pipeline.

Zero dependencies on network or the OKX CLI (pure numpy).
Run: pytest tests/test_validation.py -v
"""
import numpy as np

from src.validation import (
    sharpe_ratio,
    max_drawdown,
    calmar_ratio,
    cagr,
    walk_forward_windows,
    probability_of_backtest_overfitting,
    evaluate_parameter_grid,
    validation_report,
)


def test_sharpe_zero_for_zero_variance():
    returns = np.full(100, 0.001)
    assert sharpe_ratio(returns) == 0.0


def test_sharpe_positive_for_positive_mean_returns():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0005, 0.01, 1000)
    assert sharpe_ratio(returns) > 0


def test_max_drawdown_is_nonpositive():
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0002, 0.02, 500)
    mdd = max_drawdown(returns)
    assert mdd <= 0


def test_max_drawdown_known_series():
    # equity goes 1 -> 1.1 -> 0.99 -> 1.05 ; drawdown from peak 1.1 to 0.99 = -10%
    returns = np.array([0.10, -0.10, 0.06060606])
    mdd = max_drawdown(returns)
    assert abs(mdd - (-0.10)) < 1e-6


def test_calmar_ratio_relates_cagr_and_drawdown():
    rng = np.random.default_rng(2)
    returns = rng.normal(0.0004, 0.01, 3 * 365)
    c = calmar_ratio(returns)
    expected = cagr(returns) / abs(max_drawdown(returns))
    assert abs(c - expected) < 1e-9


def test_walk_forward_windows_never_overlap_or_look_ahead():
    windows = list(walk_forward_windows(n_obs=1000, n_folds=5, train_frac=0.7))
    assert len(windows) == 5
    for train, test in windows:
        assert train.stop <= test.start  # train never comes after test starts
        assert test.start < test.stop


def test_pbo_with_fewer_than_two_variants_is_zero():
    assert probability_of_backtest_overfitting([1.0], [1.0]) == 0.0
    assert probability_of_backtest_overfitting([], []) == 0.0


def test_pbo_in_black_box_range():
    # Whatever the ranking does, the simplified probability stays a
    # well-formed probability in [0.5, 1.0] even when IS-best lands OOS worst.
    is_sharpes = [1.0, 0.8, 0.6, 0.4, 0.2]
    oos_sharpes = [0.1, 0.3, 0.5, 0.7, 0.9]  # IS-best is now OOS-worst => overfit risk high
    pbo = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)
    assert 0.5 <= pbo <= 1.0


def test_pbo_high_when_best_is_worst_oos():
    # IS-best (idx 0) ranks last out-of-sample: overfitting is near-certain.
    is_sharpes = [1.0, 0.8, 0.6, 0.4, 0.2]
    oos_sharpes = [0.0, 0.3, 0.5, 0.7, 0.9]
    pbo = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)
    assert pbo > 0.5


def test_evaluate_parameter_grid_flags_pbo_past_50_combos():
    rng = np.random.default_rng(3)
    grid_is = {str(i): rng.normal(0.0003, 0.01, 500) for i in range(51)}
    grid_oos = {k: rng.normal(0.0003, 0.01, 500) for k in grid_is}
    result = evaluate_parameter_grid(grid_is, grid_oos)
    assert result["n_combinations_tested"] == 51
    assert result["warning"] is not None
    assert "pbo" in result


def test_validation_report_cleared_only_when_oos_calmar_bar_met():
    # Strong consistent edge -> high OOS Calmar -> clears the bar.
    rng = np.random.default_rng(4)
    edge = 0.0004
    returns = edge + rng.normal(0, 0.005, 600)
    report = validation_report(returns, returns, calmar_bar=1.0)
    assert report["passes_calmar_bar"] is True
    assert report["cleared_for_paper_trading"] is True


def test_validation_report_never_traded_oos_does_not_vacuously_clear():
    # All-zero OOS returns = the strategy never traded out of sample. Zero
    # drawdown makes Calmar hit the 1e9 cap; that must NOT clear the gate.
    # Regression for the vacuous pass found in the 2026-08-22 real-data run
    # (funding contrarian: 0 trades on BTC/ETH/BNB in 2y, still "PASS").
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.01, 600)
    report = validation_report(returns, np.zeros(600), calmar_bar=1.0)
    assert report["out_of_sample"]["calmar"] >= 1.0  # the vacuous number itself
    assert report["has_oos_evidence"] is False
    assert report["passes_calmar_bar"] is False
    assert report["cleared_for_paper_trading"] is False


def test_validation_report_fails_high_calmar_bar():
    # Noisy zero-edge series cannot clear a demanding bar.
    rng = np.random.default_rng(5)
    returns = rng.normal(0.0, 0.02, 600)
    report = validation_report(returns, returns, calmar_bar=50.0)
    assert report["passes_calmar_bar"] is False
    assert report["cleared_for_paper_trading"] is False


def test_validation_report_pbo_fail_blocks_clearing():
    # Under a clearly-overfit grid the report must NOT clear even if the
    # headline OOS Calmar bar is met.
    rng = np.random.default_rng(6)
    returns = 0.0004 + rng.normal(0, 0.005, 600)
    grid_is = {f"p{i}": returns.copy() for i in range(20)}
    # Every variant measured on the same series: IS-best is arbitrarily
    # ranked, so PBO will sit near the opaque middle — force a grid whose
    # IS-best is deliberately worst OOS.
    grid_is = {f"p{i}": returns.copy() for i in range(5)}
    grid_is["p_best_is"] = returns + 0.001  # highest IS
    grid_oos = {k: returns.copy() for k in grid_is}
    grid_oos["p_best_is"] = returns - 0.001  # but worst OOS
    report = validation_report(returns, returns, grid_is, grid_oos, calmar_bar=1.0)
    assert report["passes_calmar_bar"] is True
    assert report["pbo_analysis"]["pbo_pass"] is False
    assert report["cleared_for_paper_trading"] is False