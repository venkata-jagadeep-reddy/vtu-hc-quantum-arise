"""Detection cost of a family-wise (Bonferroni) calibration.

The default configuration calibrates each checkpoint at ``alpha = 0.005``.  A
run is flagged when any of the five checkpoints deviates, so the run-level rate
is up to ``5 * alpha``.  Calibrating each checkpoint at ``alpha / k = 0.001``
restores a run-level guarantee of ``alpha`` at the cost of a higher threshold.

This script measures that cost on the full tampering catalogue so the trade-off
is reported rather than guessed.  The ``alpha = 0.005`` detection rate is
already in ``data/evaluation/evaluation.json``; this measures the
``alpha = 0.001`` side.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/bonferroni_tradeoff.py
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
from quantumpulse.tampering import (
    apply_tamper,
    build_tamper_catalog,
    exact_effect,
    is_effectless,
)
from quantumpulse.verification import verify_heartbeats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    parser.add_argument("--shots", type=int, default=4096)
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20240)
    parser.add_argument(
        "--alphas", default="0.005,0.001", help="per-checkpoint alpha values to compare"
    )
    args = parser.parse_args()

    program = reference_program()
    backend = AerBackend(noise_config=NOISE_PROFILES[args.noise], seed=args.seed)
    catalog = build_tamper_catalog(program)

    print(
        f"{'per-cp alpha':>12} {'run alpha':>10} {'detection':>10} "
        f"{'effective':>10} {'detected':>9}  thresholds"
    )
    print("-" * 78)

    for alpha in (float(a) for a in args.alphas.split(",")):
        baseline = calibrate(
            program,
            backend,
            shots=args.shots,
            n_runs=args.runs,
            alpha=alpha,
            seed=args.seed,
        )
        detected = 0
        effective = 0
        detected_effective = 0
        total = 0
        for index, tamper in enumerate(catalog):
            tampered = apply_tamper(program, tamper)
            effectless = is_effectless(exact_effect(program, tampered, backend))
            for trial in range(args.trials):
                # Same seed formula as the evaluation harness, so the
                # alpha = 0.005 row reproduces the reported detection rate.
                heartbeat = run_heartbeats(
                    tampered,
                    backend,
                    shots=args.shots,
                    seed=args.seed + 1000 * (index + 1) + trial,
                )
                verdict = verify_heartbeats(
                    heartbeat.observations, baseline, program=tampered
                )
                total += 1
                detected += int(verdict.flagged)
                if not effectless:
                    effective += 1
                    detected_effective += int(verdict.flagged)

        thresholds = " ".join(f"{b.threshold:.4f}" for b in baseline.checkpoints.values())
        print(
            f"{alpha:>12.4f} {min(1.0, 5 * alpha):>10.3f} "
            f"{detected / max(1, total):>10.3f} "
            f"{detected_effective / max(1, effective):>10.3f} "
            f"{detected:>4}/{total:<4} {thresholds}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
