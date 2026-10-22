#!/usr/bin/env python
"""Calibration-size study: how many healthy runs are needed to control the FPR?

This is the script behind the "Calibration size" table in ``docs/results.md``.
It calibrates the monitor with different numbers of healthy runs and different
significance levels, then measures the realised false-positive rate on a
**seed-disjoint** set of fresh healthy runs.

Why this matters
----------------
The alert threshold is an order statistic of the healthy null. With only ``N``
calibration runs the smallest resolvable per-checkpoint rate is roughly
``1/(N+1)``, so a calibration set that is too small cannot honour a small
``alpha`` -- and the realised rate can exceed it. This script measures that
effect instead of asserting it.

Usage (from the repository root)::

    PYTHONPATH=src .venv/bin/python scripts/calibration_study.py

    # or with a different measurement budget
    PYTHONPATH=src .venv/bin/python scripts/calibration_study.py --trials 500

The nominal seed block (``90_000``) is deliberately disjoint from the
calibration seeds and from the tampered-run seeds used by the evaluation
harness.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quantumpulse.backends import AerBackend
from quantumpulse.calibration import calibrate
from quantumpulse.circuits import reference_program
from quantumpulse.config import NOISE_PROFILES
from quantumpulse.execution import run_heartbeats
from quantumpulse.verification import verify_heartbeats

#: (calibration runs, per-checkpoint alpha) combinations to compare.
SETTINGS = ((16, 0.02), (50, 0.02), (200, 0.02), (200, 0.005))

NOMINAL_SEED_START = 90_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=200, help="healthy runs per row")
    parser.add_argument("--shots", type=int, default=4096)
    parser.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    args = parser.parse_args()

    program = reference_program()
    backend = AerBackend(noise_config=NOISE_PROFILES[args.noise])

    print(
        f"{'calib runs':>10} {'alpha':>7} {'checkpoints':>12} {'run FPR':>9} "
        f"{'per-cp FPR':>11} {'union bound':>12}"
    )
    print("-" * 66)

    for n_runs, alpha in SETTINGS:
        baseline = calibrate(
            program,
            backend,
            shots=args.shots,
            n_runs=n_runs,
            alpha=alpha,
            seed=7,
        )
        flagged_rounds = 0
        flagged_checkpoints = 0
        n_checkpoints = 0

        for i in range(args.trials):
            heartbeat = run_heartbeats(
                program, backend, shots=args.shots, seed=NOMINAL_SEED_START + i
            )
            verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
            flagged_rounds += int(verdict.flagged)
            flagged_checkpoints += sum(v.flagged for v in verdict.verdicts)
            n_checkpoints += len(verdict.verdicts)

        union_bound = min(1.0, len(baseline.checkpoints) * alpha)
        print(
            f"{n_runs:>10} {alpha:>7} {len(baseline.checkpoints):>12} "
            f"{flagged_rounds / args.trials:>9.3f} "
            f"{flagged_checkpoints / max(1, n_checkpoints):>11.3f} "
            f"{union_bound:>12.3f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
