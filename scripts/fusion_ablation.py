"""Ablation: does multi-checkpoint evidence fusion improve localisation?

This is the script behind the fusion table in ``docs/results.md``.  For every
detected scenario it runs the *same* observation through localisation three
times, changing only ``max_checkpoints`` (how many deviating checkpoints are
fused into the score).  Nothing else differs, so any change in strict top-1 is
attributable to the fusion.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/fusion_ablation.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quantumpulse.backends import AerBackend
from quantumpulse.calibration import BaselineProfile
from quantumpulse.circuits import reference_program
from quantumpulse.config import NOISE_PROFILES
from quantumpulse.execution import run_heartbeats
from quantumpulse.localization import localize
from quantumpulse.tampering import apply_tamper, build_tamper_catalog
from quantumpulse.verification import verify_heartbeats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="data/baseline.json")
    parser.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--settings", default="1,3,5")
    args = parser.parse_args()

    settings = [int(s) for s in args.settings.split(",") if s]
    program = reference_program()
    backend = AerBackend(noise_config=NOISE_PROFILES[args.noise], seed=args.seed)
    baseline = BaselineProfile.load(args.baseline)
    catalog = build_tamper_catalog(program)

    stats = {k: {"n": 0, "top1": 0, "tie": 0, "top3": 0, "amb": 0} for k in settings}

    for index, tamper in enumerate(catalog):
        tampered = apply_tamper(program, tamper)
        heartbeat = run_heartbeats(
            tampered, backend, shots=baseline.shots, seed=args.seed + index
        )
        verdict = verify_heartbeats(
            heartbeat.observations, baseline, program=tampered
        )
        if not verdict.flagged:
            continue
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]
        for k in settings:
            report = localize(
                program,
                flagged,
                baseline,
                backend,
                shots=baseline.shots,
                seed=args.seed + index,
                true_gid=tamper.target_gid,
                exact_sweep=True,
                max_checkpoints=k,
            )
            bucket = stats[k]
            bucket["n"] += 1
            bucket["top1"] += int(bool(report.top1_gid == tamper.target_gid))
            bucket["tie"] += int(tamper.target_gid in report.tied_gids)
            bucket["top3"] += int(tamper.target_gid in report.top3_gids)
            bucket["amb"] += int(report.ambiguous)

    print(f"\nfusion ablation  noise={args.noise} shots={baseline.shots}")
    print(f"{'max checkpoints':>16} {'n':>4} {'top1':>8} {'top1-or-tie':>12} {'top3':>8} {'ambiguous':>10}")
    for k in settings:
        b = stats[k]
        n = max(1, b["n"])
        print(
            f"{k:>16} {b['n']:>4} {b['top1'] / n:>8.3f} {b['tie'] / n:>12.3f} "
            f"{b['top3'] / n:>8.3f} {b['amb'] / n:>10.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
