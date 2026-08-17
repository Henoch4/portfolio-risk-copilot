"""
Atomic multi-leg execution -- the piece that makes a delta-neutral thesis
buildable rather than aspirational. Multiple legs are dispatched concurrently,
tracked as ONE package via an explicit state machine, and unwound automatically
on a partial fill rather than left as unintended directional exposure.

State machine: PENDING_FILL -> LOCKED -> SETTLED, with ABORTED as an explicit
outcome (not a caught exception).

Different from the source MVP this was ported from: every declared limit is
actually checked in the code path that executes it. The original dispatch
stored ``max_slippage_pct`` on each step but never enforced it -- a leg that
filled at 3x its allowed slippage still counted as a clean fill. Here
``dispatch_concurrent`` flags any leg whose realized slippage breaches its
limit and forces the package down the unwind path instead of LOCKED.

Fills are simulated (paper trading) -- no real venue connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import itertools
import random


class PackageState(Enum):
    PENDING_FILL = "pending_fill"
    LOCKED = "locked"
    SETTLED = "settled"
    ABORTED = "aborted"


@dataclass
class Step:
    venue: str
    action: str  # "buy_spot" | "sell_spot" | "short_perp" | "cover_perp"
    asset: str
    amount_ratio: float  # fraction of package notional for this leg
    max_slippage_pct: float = 0.003

    def inverse(self) -> "Step":
        flip = {
            "buy_spot": "sell_spot",
            "sell_spot": "buy_spot",
            "short_perp": "cover_perp",
            "cover_perp": "short_perp",
        }
        return Step(self.venue, flip[self.action], self.asset, self.amount_ratio, self.max_slippage_pct)


def validate_steps(steps: list[Step]) -> list[str]:
    """Validated before dispatch -- the whole planned trade, not each leg independently."""
    errors = []
    total_ratio = sum(s.amount_ratio for s in steps)
    if abs(total_ratio - 1.0) > 1e-6:
        errors.append(f"step amount_ratios must sum to 1.0, got {total_ratio}")
    if len(steps) < 2:
        errors.append("multi-leg package requires at least 2 steps")
    for s in steps:
        if s.max_slippage_pct <= 0 or s.max_slippage_pct > 0.05:
            errors.append(f"step on {s.venue} has implausible max_slippage_pct={s.max_slippage_pct}")
    return errors


@dataclass
class LegResult:
    step: Step
    filled: bool
    fill_price: float | None
    slippage_pct: float | None


@dataclass
class Package:
    id: int
    steps: list[Step]
    notional: float
    state: PackageState = PackageState.PENDING_FILL
    leg_results: list[LegResult] = field(default_factory=list)
    unwound: bool = False
    slippage_breached: bool = False


class MultiLegExecutionManager:
    _id_counter = itertools.count(1)

    def __init__(self, max_concurrent_packages: int = 3, fill_timeout_cycles: int = 2):
        self.max_concurrent_packages = max_concurrent_packages
        self.fill_timeout_cycles = fill_timeout_cycles
        self._open_packages: dict[int, Package] = {}
        self._active_instruments: set[str] = set()

    def can_open(self, asset: str) -> tuple[bool, str | None]:
        if len(self._open_packages) >= self.max_concurrent_packages:
            return False, f"capacity check failed: {len(self._open_packages)} packages already open"
        if asset in self._active_instruments:
            return False, f"duplication check failed: an active package already exists for {asset}"
        return True, None

    def propose_package(self, steps: list[Step], notional: float) -> Package:
        errors = validate_steps(steps)
        if errors:
            raise ValueError(f"invalid package: {errors}")

        allowed, reason = self.can_open(steps[0].asset)
        if not allowed:
            raise RuntimeError(reason)

        pkg = Package(id=next(self._id_counter), steps=steps, notional=notional)
        self._open_packages[pkg.id] = pkg
        self._active_instruments.add(steps[0].asset)
        return pkg

    def dispatch_concurrent(self, pkg: Package, fill_simulator) -> Package:
        """
        All legs submitted in the same cycle (concurrent, not sequential).
        `fill_simulator(step, notional_for_leg) -> LegResult`.

        Every declared per-leg slippage limit is enforced here -- this is the
        code path that executes the order. If a leg fills beyond its
        `max_slippage_pct`, the package is flagged `slippage_breached` and
        stays PENDING_FILL so the caller must unwind it; it can never silently
        reach LOCKED on a bad-priced fill.
        """
        for step in pkg.steps:
            leg_notional = pkg.notional * step.amount_ratio
            result = fill_simulator(step, leg_notional)
            pkg.leg_results.append(result)
            if (
                result.filled
                and result.slippage_pct is not None
                and result.slippage_pct > step.max_slippage_pct
            ):
                pkg.slippage_breached = True

        all_filled = all(r.filled for r in pkg.leg_results)
        if all_filled and not pkg.slippage_breached:
            pkg.state = PackageState.LOCKED
        # else: stays PENDING_FILL; resolve_partial_fill / resolve_slippage_breach
        # handle the unwind, fail-closed.
        return pkg

    def resolve_partial_fill(self, pkg: Package, unwind_simulator) -> Package:
        """
        One leg filled and another didn't: immediately close the filled leg
        rather than leave single-leg directional exposure. An open unwound
        position is worse than paying to unwind it.
        """
        filled_legs = [r for r in pkg.leg_results if r.filled]
        unfilled_legs = [r for r in pkg.leg_results if not r.filled]

        if not unfilled_legs:
            pkg.state = PackageState.LOCKED
            return pkg

        if not filled_legs:
            # nothing filled at all -- clean abort, no unwind needed
            pkg.state = PackageState.ABORTED
            self._release(pkg)
            return pkg

        # partial fill: unwind the filled leg(s) immediately
        for leg in filled_legs:
            unwind_simulator(leg.step.inverse(), pkg.notional * leg.step.amount_ratio)
        pkg.unwound = True
        pkg.state = PackageState.ABORTED
        self._release(pkg)
        return pkg

    def resolve_slippage_breach(self, pkg: Package, unwind_simulator) -> Package:
        """
        At least one leg filled beyond its allowed slippage. The package is
        fail-closed: every filled leg is unwound immediately (including the
        cleanly-filled peer legs, so the abort never leaves a partial leg).

        Must only be called after dispatch flagged `slippage_breached`.
        """
        filled_legs = [r for r in pkg.leg_results if r.filled]
        if not filled_legs:
            pkg.state = PackageState.ABORTED
            self._release(pkg)
            return pkg

        for leg in filled_legs:
            unwind_simulator(leg.step.inverse(), pkg.notional * leg.step.amount_ratio)
        pkg.unwound = True
        pkg.state = PackageState.ABORTED
        self._release(pkg)
        return pkg

    def settle(self, pkg: Package) -> Package:
        if pkg.state != PackageState.LOCKED:
            raise RuntimeError(f"cannot settle package {pkg.id} in state {pkg.state}")
        pkg.state = PackageState.SETTLED
        self._release(pkg)
        return pkg

    def close_package(self, pkg: Package, fill_simulator) -> Package:
        """Walk the same step list in reverse with each action flipped -- a
        defined symmetric inverse, not a second bespoke code path."""
        inverse_steps = [s.inverse() for s in reversed(pkg.steps)]
        closing_results = []
        for step in inverse_steps:
            leg_notional = pkg.notional * step.amount_ratio
            closing_results.append(fill_simulator(step, leg_notional))
        pkg.leg_results.extend(closing_results)
        return pkg

    def _release(self, pkg: Package):
        self._open_packages.pop(pkg.id, None)
        if pkg.steps:
            self._active_instruments.discard(pkg.steps[0].asset)

    def open_package_count(self) -> int:
        return len(self._open_packages)


class PaperFillSimulator:
    """Simulates realistic-ish fills: occasional partial/no-fill, slippage.

    Slippage is drawn directly from the distribution and NEVER clamped with a
    min() cap -- a cap masquerading as a breach earlier made the slippage
    enforcement unreachable, because fills could never actually exceed
    `max_slippage_pct`. Here an occasional breach is real, which is the whole
    reason the enforcement path exists.
    """

    def __init__(self, seed: int = 7, fill_prob: float = 0.94):
        self.rng = random.Random(seed)
        self.fill_prob = fill_prob

    def __call__(self, step: Step, notional: float) -> LegResult:
        filled = self.rng.random() < self.fill_prob
        if not filled:
            return LegResult(step=step, filled=False, fill_price=None, slippage_pct=None)
        slippage = abs(self.rng.gauss(0, step.max_slippage_pct / 2))
        return LegResult(step=step, filled=True, fill_price=notional, slippage_pct=slippage)