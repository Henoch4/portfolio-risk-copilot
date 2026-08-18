"""Unit tests for src/multi_leg.py — atomic multi-leg execution.

No deps, no network. Run: pytest tests/test_multi_leg.py -v
"""
import pytest

from src.multi_leg import (
    MultiLegExecutionManager,
    PaperFillSimulator,
    Step,
    LegResult,
    PackageState,
    validate_steps,
)


def two_leg_steps():
    return [
        Step(venue="venue_a", action="short_perp", asset="BTC", amount_ratio=0.5),
        Step(venue="venue_b", action="buy_spot", asset="BTC", amount_ratio=0.5),
    ]


def always_fill(step, notional):
    return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)


def test_validate_steps_requires_ratios_sum_to_one():
    bad_steps = [
        Step(venue="venue_a", action="short_perp", asset="BTC", amount_ratio=0.4),
        Step(venue="venue_b", action="buy_spot", asset="BTC", amount_ratio=0.4),
    ]
    errors = validate_steps(bad_steps)
    assert any("sum to 1.0" in e for e in errors)


def test_validate_steps_rejects_implausible_slippage():
    bad = [
        Step(venue="a", action="short_perp", asset="BTC", amount_ratio=0.5, max_slippage_pct=0.9),
        Step(venue="b", action="buy_spot", asset="BTC", amount_ratio=0.5, max_slippage_pct=0.003),
    ]
    errors = validate_steps(bad)
    assert any("implausible max_slippage_pct" in e for e in errors)


def test_both_legs_fill_reaches_locked_then_settled():
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    pkg = mgr.dispatch_concurrent(pkg, always_fill)
    assert pkg.state == PackageState.LOCKED
    assert pkg.slippage_breached is False

    pkg = mgr.settle(pkg)
    assert pkg.state == PackageState.SETTLED
    assert mgr.open_package_count() == 0


def test_dispatch_unwinds_on_slippage_breach():
    """Regression for the source bug: max_slippage_pct was stored on the step
    but never enforced in the dispatch path, so a 5x-slippage fill still
    counted as a clean locked fill. It must flag the breach and refuse LOCKED."""
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    def breach_on_second_leg(step, notional):
        breached = step.venue == "venue_b"
        return LegResult(
            step=step,
            filled=True,
            fill_price=notional,
            slippage_pct=0.05 if breached else 0.001,
        )

    pkg = mgr.dispatch_concurrent(pkg, breach_on_second_leg)
    assert pkg.slippage_breached is True
    assert pkg.state == PackageState.PENDING_FILL  # never LOCKED on a bad fill

    unwind_calls = []
    def unwind_sim(step, notional):
        unwind_calls.append((step.action, notional))
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.resolve_slippage_breach(pkg, unwind_sim)
    assert pkg.state == PackageState.ABORTED
    assert pkg.unwound is True
    assert {a for a, _ in unwind_calls} == {"cover_perp", "sell_spot"}  # both legs unwound
    assert mgr.open_package_count() == 0


def test_dispatch_within_slippage_is_not_a_breach():
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    def at_limit(step, notional):
        return LegResult(step=step, filled=True, fill_price=notional,
                         slippage_pct=step.max_slippage_pct)  # exactly at limit: allowed

    pkg = mgr.dispatch_concurrent(pkg, at_limit)
    assert pkg.slippage_breached is False
    assert pkg.state == PackageState.LOCKED


def test_breach_on_unfilled_leg_is_ignored():
    # A leg that did not fill has no realized slippage; it must not trip the breach.
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    def one_no_fill(step, notional):
        if step.venue == "venue_b":
            return LegResult(step=step, filled=False, fill_price=None, slippage_pct=None)
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.dispatch_concurrent(pkg, one_no_fill)
    assert pkg.slippage_breached is False  # partial fill, not slippage breach
    assert pkg.state == PackageState.PENDING_FILL


def test_partial_fill_triggers_automatic_unwind():
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    call_count = {"n": 0}

    def one_fills_one_doesnt(step, notional):
        call_count["n"] += 1
        filled = call_count["n"] == 1
        return LegResult(step=step, filled=filled,
                         fill_price=notional if filled else None,
                         slippage_pct=0.001 if filled else None)

    pkg = mgr.dispatch_concurrent(pkg, one_fills_one_doesnt)
    assert pkg.state == PackageState.PENDING_FILL  # not all filled

    unwind_calls = []

    def unwind_sim(step, notional):
        unwind_calls.append((step.action, notional))
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.resolve_partial_fill(pkg, unwind_sim)
    assert pkg.state == PackageState.ABORTED
    assert pkg.unwound is True
    assert len(unwind_calls) == 1  # only the filled leg gets unwound
    assert mgr.open_package_count() == 0  # released, not left dangling


def test_no_fill_at_all_is_clean_abort_without_unwind():
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    def never_fill(step, notional):
        return LegResult(step=step, filled=False, fill_price=None, slippage_pct=None)

    pkg = mgr.dispatch_concurrent(pkg, never_fill)

    unwind_calls = []

    def unwind_sim(step, notional):
        unwind_calls.append(step)
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.resolve_partial_fill(pkg, unwind_sim)
    assert pkg.state == PackageState.ABORTED
    assert pkg.unwound is False
    assert len(unwind_calls) == 0


def test_duplication_check_blocks_second_package_same_asset():
    mgr = MultiLegExecutionManager()
    mgr.propose_package(two_leg_steps(), notional=10_000)

    can_open, reason = mgr.can_open("BTC")
    assert can_open is False
    assert "duplication" in reason


def test_capacity_check_blocks_beyond_max_concurrent():
    mgr = MultiLegExecutionManager(max_concurrent_packages=1)
    mgr.propose_package(two_leg_steps(), notional=10_000)

    other_asset_steps = [
        Step(venue="venue_a", action="short_perp", asset="ETH", amount_ratio=0.5),
        Step(venue="venue_b", action="buy_spot", asset="ETH", amount_ratio=0.5),
    ]
    can_open, reason = mgr.can_open("ETH")
    assert can_open is False
    assert "capacity" in reason


def test_propose_raises_on_invalid_steps():
    mgr = MultiLegExecutionManager()
    with pytest.raises(ValueError):
        mgr.propose_package([two_leg_steps()[0]], notional=10_000)


def test_resolve_routes_slippage_breach_before_fill_state():
    """Regression: `resolve()` must route on slippage_breached BEFORE fill
    state. A package whose legs all filled but one breached its slippage
    collar must unwind fail-closed — the old partial-fill-first ordering
    would treat 'all legs filled' as a clean LOCKED trade and leave the
    breached leg's exposure in place."""
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    def breach_on_second_leg(step, notional):
        breached = step.venue == "venue_b"
        return LegResult(
            step=step,
            filled=True,
            fill_price=notional,
            slippage_pct=0.05 if breached else 0.001,
        )

    pkg = mgr.dispatch_concurrent(pkg, breach_on_second_leg)
    assert pkg.slippage_breached is True
    assert all(r.filled for r in pkg.leg_results)  # all filled, still breached

    unwind_calls = []

    def unwind_sim(step, notional):
        unwind_calls.append((step.action, notional))
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.resolve(pkg, unwind_sim)
    assert pkg.state == PackageState.ABORTED
    assert pkg.unwound is True
    assert {a for a, _ in unwind_calls} == {"cover_perp", "sell_spot"}
    assert mgr.open_package_count() == 0


def test_resolve_routes_partial_fill_when_no_slippage_breach():
    """A package that is partially filled but not slippage-breached routes to
    the partial-fill path (only the filled leg unwinds)."""
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    call_count = {"n": 0}

    def one_fills_one_doesnt(step, notional):
        call_count["n"] += 1
        filled = call_count["n"] == 1
        return LegResult(
            step=step, filled=filled,
            fill_price=notional if filled else None,
            slippage_pct=0.001 if filled else None,
        )

    pkg = mgr.dispatch_concurrent(pkg, one_fills_one_doesnt)
    assert pkg.slippage_breached is False

    unwind_calls = []

    def unwind_sim(step, notional):
        unwind_calls.append((step.action, notional))
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    pkg = mgr.resolve(pkg, unwind_sim)
    assert pkg.state == PackageState.ABORTED
    assert len(unwind_calls) == 1  # only the filled leg
    assert mgr.open_package_count() == 0


def test_resolve_leaves_locked_package_untouched():
    """resolve() on an already-LOCKED package (all filled, no breach) is a
    no-op — the routing must not re-enter a resolver or unwind anything."""
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)
    pkg = mgr.dispatch_concurrent(pkg, always_fill)
    assert pkg.state == PackageState.LOCKED

    called = []

    def unwind_sim(step, notional):
        called.append(step)
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=0.001)

    resolved = mgr.resolve(pkg, unwind_sim)
    assert resolved is pkg
    assert resolved.state == PackageState.LOCKED
    assert called == []


def test_close_package_uses_symmetric_inverse():
    mgr = MultiLegExecutionManager()
    pkg = mgr.propose_package(two_leg_steps(), notional=10_000)

    pkg = mgr.dispatch_concurrent(pkg, always_fill)
    pkg = mgr.settle(pkg)

    closed = mgr.close_package(pkg, always_fill)
    closing_actions = [r.step.action for r in closed.leg_results[2:]]
    # original actions were short_perp, buy_spot -> inverse+reversed should be sell_spot, cover_perp
    assert closing_actions == ["sell_spot", "cover_perp"]


def test_paper_fill_simulator_can_actually_breach_slippage():
    """Regression for the cap bug: the source clamped slippage with min(..., 
    max*1.5), so no fill could ever exceed max_slippage_pct and the breach path
    was dead code. The ported simulator must occasionally produce a true breach."""
    step = Step(venue="a", action="short_perp", asset="BTC", amount_ratio=1.0, max_slippage_pct=0.003)
    sim = PaperFillSimulator(seed=7, fill_prob=1.0)
    breaches = 0
    for _ in range(10_000):
        result = sim(step, 1_000)
        assert result.filled
        if result.slippage_pct > step.max_slippage_pct:
            breaches += 1
    assert breaches > 0  # occasionally a fill at up to several sigma slippage
    assert breaches < 5_000  # but not so often it is always breached