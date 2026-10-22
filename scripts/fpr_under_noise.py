"""Does a family-wise (Bonferroni) calibration fix the FPR under noise?

The default evaluation calibrates each checkpoint at ``alpha = 0.005`` and
measures run-level false-positive rates of 6-7% under the noisy profiles.
Calibrating at ``alpha / k = 0.001`` restores a run-level guarantee of
``alpha``; this script measures whether that also holds under noise.

Output feeds the Bonferroni table in ``docs/results.md`` §6.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/fpr_under_noise.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quantumpulse.backends import AerBackend
from quantumpulse.calibration import calibrate
from quantumpulse.circuits import reference_program
from quantumpulse.config import NOISE_PROFILES
from quantumpulse.execution import run_heartbeats
from quantumpulse.verification import verify_heartbeats

PROFILES = ("depolarizing_mild", "depolarizing_strong", "thermal")
NOMINAL_SEED_START = 900_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--shots", type=int, default=4096)
    parser.add_argument("--alphas", default="0.001")
    args = parser.parse_args()

    program = reference_program()
    alphas = [float(a) for a in args.alphas.split(",")]

    print(
        f"{'noise':<22} {'per-cp alpha':>12} {'rounds':>7} {'run FPR':>8} "
        f"{'per-cp FPR':>11} {'bound':>7}"
    )
    print("-" * 72)
    for name in PROFILES:
        backend = AerBackend(noise_config=NOISE_PROFILES[name])
        for alpha in alphas:
            baseline = calibrate(
                program,
                backend,
                shots=args.shots,
                n_runs=args.runs,
                alpha=alpha,
                seed=20240,
            )
            flagged_rounds = 0
            flagged_cp = 0
            tests = 0
            for i in range(args.rounds):
                heartbeat = run_heartbeats(
                    program,
                    backend,
                    shots=args.shots,
                    seed=NOMINAL_SEED_START + i,
                )
                verdict = verify_heartbeats(
                    heartbeat.observations, baseline, program=program
                )
                flagged_rounds += int(verdict.flagged)
                flagged_cp += sum(v.flagged for v in verdict.verdicts)
                tests += len(verdict.verdicts)
            print(
                f"{name:<22} {alpha:>12.4f} {args.rounds:>7} "
                f"{flagged_rounds / args.rounds:>8.3f} "
                f"{flagged_cp / max(1, tests):>11.4f} "
                f"{min(1.0, len(baseline.checkpoints) * alpha):>7.3f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
