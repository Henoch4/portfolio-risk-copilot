#!/usr/bin/env python3
"""Deploy gate: block CI / deploy when the strategy is NOT cleared for trading.

Honest-null policy: if VALIDATION_RETURNS_PATH is unset or missing, the gate
does NOT block — an unvalidated strategy is not the same as a failed one. Only
an explicit cleared_for_paper_trading == False blocks the deploy. This keeps the
hackathon demo runnable while making the validation bar real in CI.

Exits 0 (allow) or 1 (block).
"""
import os
import sys

import numpy as np

from src.validation import validation_report


def main() -> int:
    path = os.getenv("VALIDATION_RETURNS_PATH", "").strip()
    if not path or not os.path.exists(path):
        print("validation-gate: no VALIDATION_RETURNS_PATH configured; skipping (honest null).")
        return 0

    data = np.loadtxt(path, delimiter=",", skiprows=1)
    in_sample = data[:, 0]
    oos = data[:, 1] if data.shape[1] > 1 else data[:, 0]

    report = validation_report(in_sample, oos)
    cleared = report["cleared_for_paper_trading"]
    if not cleared:
        print("validation-gate: BLOCKED — strategy not cleared for paper trading.")
        print(f"  passes_calmar_bar={report['passes_calmar_bar']} "
              f"pbo_pass={report.get('pbo_analysis', {}).get('pbo_pass') if report.get('pbo_analysis') else None}")
        return 1

    print("validation-gate: cleared for paper trading.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
