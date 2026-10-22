"""Command-line interface for QuantumPulse.

    quantumpulse reference                 show the reference circuit and checkpoints
    quantumpulse calibrate                 build and store the calibrated baseline
    quantumpulse check                     run one heartbeat verification
    quantumpulse localize                  run one verification and localise
    quantumpulse evaluate                  run the evaluation matrix
    quantumpulse summarize                 render a saved evaluation as markdown
    quantumpulse ml-evaluate               compare calibrated D_TV with Isolation Forest
    quantumpulse monitor                   run the periodic monitoring loop
    quantumpulse report                    summarise stored monitoring history
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .backends import AerBackend
from .calibration import BaselineProfile, calibrate
from .circuits import program_table, reference_program
from .config import NOISE_PROFILES, ExperimentConfig, MonitorConfig
from .evaluation import run_evaluation
from .evidence import build_report
from .execution import run_heartbeats
from .localization import localize
from .monitoring import MonitorSession
from .program import QuantumPulseProgram
from .storage import MonitoringStore
from .tampering import apply_tamper, build_tamper_catalog, tamper_catalog_summary
from .verification import VerificationConfig, verify_heartbeats


def _resolve_noise(name: str):
    if name not in NOISE_PROFILES:
        raise SystemExit(
            f"unknown noise profile '{name}'. "
            f"Available: {', '.join(sorted(NOISE_PROFILES))}"
        )
    return NOISE_PROFILES[name]


def _load_program(args) -> QuantumPulseProgram:
    program = reference_program()
    tamper_id = getattr(args, "tamper", None)
    if not tamper_id:
        return program

    catalog = build_tamper_catalog(program)
    exact = [t for t in catalog if t.tamper_id == tamper_id]
    if exact:
        return apply_tamper(program, exact[0])

    by_gate = [t for t in catalog if t.target_gid == tamper_id]
    if by_gate:
        print(f"'{tamper_id}' matches {len(by_gate)} scenarios; using the first one:")
        for t in by_gate:
            print(f"    {t.tamper_id}  ({t.description})")
        print("    -> full list: quantumpulse reference")
        return apply_tamper(program, by_gate[0])

    raise SystemExit(
        f"unknown tampering scenario '{tamper_id}'. "
        "Run 'quantumpulse reference' to list the catalogue."
    )


def _load_baseline(path: str) -> BaselineProfile:
    try:
        return BaselineProfile.load(path)
    except FileNotFoundError:
        raise SystemExit(
            f"no baseline found at '{path}'. Run 'quantumpulse calibrate' first."
        )


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_reference(args) -> int:
    program = reference_program()
    print(f"QuantumPulse reference circuit: {program.name}")
    print(f"qubits: {program.num_qubits}   gates: {len(program.gates)}")
    print()
    print(f"{'pos':>3}  {'gid':<5} {'gate':<24} {'stage':<14} checkpoint")
    print("-" * 62)
    for row in program_table(program):
        print(
            f"{row['pos']:>3}  {row['gid']:<5} {row['gate']:<24} "
            f"{row['stage']:<14} {row['checkpoint']}"
        )
    print()
    print("Heartbeat checkpoints:")
    for cp in program.checkpoints:
        print(f"  {cp.cp_id:<9} position {cp.position:>2}  {cp.description}")
    print()
    print("Controlled tampering catalogue:")
    catalog = build_tamper_catalog(program)
    summary = tamper_catalog_summary(catalog)
    print(f"  {len(catalog)} single-gate scenarios: {summary}")
    return 0


def cmd_calibrate(args) -> int:
    program = reference_program()
    backend = AerBackend(noise_config=_resolve_noise(args.noise), seed=args.seed)
    print(
        f"Calibrating '{program.name}' under noise profile '{args.noise}' "
        f"({args.runs} runs x {args.shots} shots, alpha={args.alpha}) ..."
    )
    profile = calibrate(
        program,
        backend,
        shots=args.shots,
        n_runs=args.runs,
        alpha=args.alpha,
        seed=args.seed,
        progress=True,
    )
    profile.save(args.output)
    print(f"\nBaseline written to {args.output}\n")
    print(f"{'checkpoint':<10} {'threshold':>11} {'null mean':>11} {'null std':>10} {'noise floor':>12}")
    print("-" * 58)
    for cp_id, b in profile.checkpoints.items():
        print(
            f"{cp_id:<10} {b.threshold:>11.5f} {b.null_mean:>11.5f} "
            f"{b.null_std:>10.5f} {b.noise_floor:>12.5f}"
        )
    return 0


def cmd_check(args) -> int:
    program = _load_program(args)
    baseline = _load_baseline(args.baseline)
    backend = AerBackend(noise_config=_resolve_noise(args.noise), seed=args.seed)

    heartbeat = run_heartbeats(program, backend, shots=args.shots, seed=args.seed)
    verdict = verify_heartbeats(
        heartbeat.observations, baseline, VerificationConfig(rule=args.rule), program=program
    )
    report = build_report(
        program,
        verdict,
        heartbeat=heartbeat,
        backend_description=backend.describe(),
    )
    print(report.to_text())
    if args.json:
        print(report.to_json())
    return 1 if verdict.flagged else 0


def cmd_localize(args) -> int:
    program = _load_program(args)
    reference = reference_program()
    baseline = _load_baseline(args.baseline)
    backend = AerBackend(noise_config=_resolve_noise(args.noise), seed=args.seed)

    heartbeat = run_heartbeats(program, backend, shots=args.shots, seed=args.seed)
    verdict = verify_heartbeats(
        heartbeat.observations, baseline, VerificationConfig(rule=args.rule), program=program
    )
    localization = None
    if verdict.flagged:
        flagged_observations = [
            heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged
        ]
        if flagged_observations:
            # Hypotheses are always single-gate modifications of the
            # *reference* circuit, compared against what was observed.
            localization = localize(
                reference,
                flagged_observations,
                baseline,
                backend,
                shots=args.shots,
                seed=args.seed,
                exact_sweep=args.exact_sweep,
            )
    report = build_report(
        program,
        verdict,
        heartbeat=heartbeat,
        localization=localization,
        backend_description=backend.describe(),
    )
    print(report.to_text())
    if args.json:
        print(report.to_json())
    return 1 if verdict.flagged else 0


def cmd_evaluate(args) -> int:
    config = ExperimentConfig(
        shots=args.shots,
        alpha=args.alpha,
        calibration_runs=args.runs,
        trials=args.trials,
        nominal_trials=args.nominal_trials,
        seed=args.seed,
        noise_profiles=args.profiles,
        output_dir=args.output,
    )
    program = reference_program()
    catalog = build_tamper_catalog(program)
    if args.max_tampers and args.max_tampers < len(catalog):
        # Keep a balanced sample across tampering kinds.
        selected = []
        per_kind = max(1, args.max_tampers // len(tamper_catalog_summary(catalog)))
        for kind in ("replacement", "removal", "alteration"):
            selected.extend([t for t in catalog if t.kind == kind][:per_kind])
        catalog = selected

    print(
        f"Evaluation matrix: {len(config.noise_profiles)} noise profile(s) x "
        f"{len(catalog)} tampering scenario(s) x {config.trials} trial(s)"
    )
    result = run_evaluation(
        program,
        config,
        tampers=catalog,
        run_localization=not args.no_localization,
        localization_trials=args.localization_trials,
        exact_sweep=args.exact_sweep,
        progress=True,
    )
    path = result.save(args.output)
    print(f"\nResults written to {path}\n")

    header = (
        f"{'profile':<22} {'detect':>8} {'FPR':>8} {'prec':>8} {'F1':>8} "
        f"{'top1':>8} {'tie':>8} {'top3':>8} {'out top1':>10} {'overhead':>9}"
    )
    print(header)
    print("-" * len(header))
    for name, m in result.metrics.items():
        print(
            f"{name:<22} {m.detection_rate:>8.3f} {m.false_positive_rate:>8.3f} "
            f"{m.precision:>8.3f} {m.f1:>8.3f} {m.top1_accuracy:>8.3f} "
            f"{m.top1_or_tie_accuracy:>8.3f} {m.top3_accuracy:>8.3f} "
            f"{m.output_only_top1_accuracy:>10.3f} "
            f"{m.overhead_ratio:>8.2f}x"
        )
    return 0


def cmd_ml_evaluate(args) -> int:
    """Compare the calibrated D_TV detector with the optional Isolation Forest."""

    from .ml import compare_detectors

    program = reference_program()
    catalog = build_tamper_catalog(program)
    if args.max_tampers and args.max_tampers < len(catalog):
        catalog = catalog[: args.max_tampers]

    backend = AerBackend(noise_config=_resolve_noise(args.noise), seed=args.seed)
    baseline = _load_baseline(args.baseline)

    print(
        f"Comparing detectors on {len(catalog)} scenario(s) x {args.trials} trial(s), "
        f"{args.nominal_runs} held-out healthy runs, noise='{args.noise}' ..."
    )
    result = compare_detectors(
        program,
        backend,
        baseline,
        catalog,
        shots=args.shots,
        alpha=args.alpha,
        nominal_runs=args.nominal_runs,
        trials_per_tamper=args.trials,
        seed=args.seed,
    )

    header = (
        f"{'detector':<16} {'detection':>10} {'false positive':>15}"
    )
    print()
    print(header)
    print("-" * len(header))
    print(
        f"{'calibrated D_TV':<16} {result.tvd_detection_rate:>10.3f} "
        f"{result.tvd_false_positive_rate:>15.3f}"
    )
    print(
        f"{'isolation forest':<16} {result.ml_detection_rate:>10.3f} "
        f"{result.ml_false_positive_rate:>15.3f}"
    )
    print()
    print(
        f"tampered rounds: {result.trials_tampered}, "
        f"healthy rounds: {result.trials_nominal}"
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2)
        print(f"written to {args.output}")
    return 0


def cmd_summarize(args) -> int:
    """Render a saved evaluation as markdown tables (for docs/results.md)."""

    from .evaluation import format_markdown

    text = format_markdown(args.input)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"written to {args.output}")
    else:
        print(text)
    return 0


def cmd_monitor(args) -> int:
    program = reference_program()
    baseline = _load_baseline(args.baseline)
    backend = AerBackend(noise_config=_resolve_noise(args.noise), seed=args.seed)
    config = MonitorConfig(
        shots=args.shots,
        interval_seconds=args.interval,
        max_iterations=args.iterations,
        seed=args.seed,
    )
    store = MonitoringStore(args.database)
    session = MonitorSession(
        program=program,
        backend=backend,
        baseline=baseline,
        config=config,
        store=store,
    )

    def _print(report):
        print(report.to_text())
        print()

    session.on_report = _print
    print(f"Monitoring '{program.name}' every {config.interval_seconds}s "
          f"for {config.max_iterations or 'infinite'} iteration(s). Ctrl-C to stop.\n")
    try:
        session.run()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        store.close()
    return 0


def cmd_report(args) -> int:
    store = MonitoringStore(args.database)
    summary = store.summary()
    print("Stored monitoring history")
    print(f"  total runs     : {summary['total_runs']}")
    print(f"  flagged runs   : {summary['flagged_runs']}")
    print(f"  tampered runs  : {summary['tampered_runs']}")
    print()
    rows = store.runs(limit=args.limit)
    if not rows:
        print("no runs stored yet.")
        store.close()
        return 0
    header = f"{'id':>4} {'timestamp':<26} {'label':<9} {'flag':>5} {'top1':>6} {'cp':<9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['id']:>4} {row['timestamp']:<26} {row['label']:<9} "
            f"{row['flagged']:>5} {str(row['top1_gid'] or '-'):>6} "
            f"{str(row['first_bad_cp'] or '-'):<9}"
        )
    store.close()
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantumpulse",
        description="QuantumPulse: heartbeat-based quantum circuit integrity monitoring",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ref = sub.add_parser("reference", help="show the reference circuit and checkpoints")
    p_ref.set_defaults(func=cmd_reference)

    p_cal = sub.add_parser("calibrate", help="calibrate and store the baseline profile")
    p_cal.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    p_cal.add_argument("--shots", type=int, default=4096)
    p_cal.add_argument("--runs", type=int, default=200)
    p_cal.add_argument("--alpha", type=float, default=0.005)
    p_cal.add_argument("--seed", type=int, default=20240)
    p_cal.add_argument("--output", default="data/baseline.json")
    p_cal.set_defaults(func=cmd_calibrate)

    def _add_common(p):
        p.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
        p.add_argument("--shots", type=int, default=4096)
        p.add_argument("--seed", type=int, default=20240)
        p.add_argument("--baseline", default="data/baseline.json")
        p.add_argument("--rule", default="tvd", choices=["tvd", "tvd+chi2"])
        p.add_argument("--tamper", default=None, help="tampering scenario id or gate id")
        p.add_argument("--json", action="store_true", help="also print the JSON report")

    p_check = sub.add_parser("check", help="run one heartbeat verification")
    _add_common(p_check)
    p_check.set_defaults(func=cmd_check)

    p_loc = sub.add_parser("localize", help="verify and localise a suspected modification")
    _add_common(p_loc)
    p_loc.add_argument(
        "--exact-sweep",
        action="store_true",
        help="rank hypotheses with the exact statevector (faster), then confirm with shots",
    )
    p_loc.set_defaults(func=cmd_localize)

    p_eval = sub.add_parser("evaluate", help="run the evaluation matrix")
    p_eval.add_argument("--shots", type=int, default=4096)
    p_eval.add_argument("--runs", type=int, default=200)
    p_eval.add_argument("--alpha", type=float, default=0.005)
    p_eval.add_argument("--trials", type=int, default=10)
    p_eval.add_argument(
        "--nominal-trials",
        type=int,
        default=100,
        help="healthy runs per profile (governs the false-positive estimate)",
    )
    p_eval.add_argument("--seed", type=int, default=20240)
    p_eval.add_argument(
        "--profiles",
        nargs="+",
        default=sorted(NOISE_PROFILES),
        choices=sorted(NOISE_PROFILES),
    )
    p_eval.add_argument("--max-tampers", type=int, default=0)
    p_eval.add_argument("--localization-trials", type=int, default=3)
    p_eval.add_argument("--exact-sweep", action="store_true")
    p_eval.add_argument("--no-localization", action="store_true")
    p_eval.add_argument("--output", default="data/evaluation")
    p_eval.set_defaults(func=cmd_evaluate)

    p_ml = sub.add_parser(
        "ml-evaluate",
        help="compare calibrated D_TV against the optional Isolation Forest detector",
    )
    p_ml.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    p_ml.add_argument("--shots", type=int, default=4096)
    p_ml.add_argument("--alpha", type=float, default=0.005)
    p_ml.add_argument("--seed", type=int, default=20240)
    p_ml.add_argument("--baseline", default="data/baseline.json")
    p_ml.add_argument("--trials", type=int, default=3)
    p_ml.add_argument("--nominal-runs", type=int, default=100)
    p_ml.add_argument("--max-tampers", type=int, default=0)
    p_ml.add_argument("--output", default="data/ml_comparison.json")
    p_ml.set_defaults(func=cmd_ml_evaluate)

    p_sum = sub.add_parser(
        "summarize", help="render a saved evaluation.json as markdown tables"
    )
    p_sum.add_argument("--input", default="data/evaluation/evaluation.json")
    p_sum.add_argument("--output", default=None)
    p_sum.set_defaults(func=cmd_summarize)

    p_mon = sub.add_parser("monitor", help="run the periodic monitoring loop")
    p_mon.add_argument("--noise", default="ideal", choices=sorted(NOISE_PROFILES))
    p_mon.add_argument("--shots", type=int, default=4096)
    p_mon.add_argument("--seed", type=int, default=20240)
    p_mon.add_argument("--interval", type=float, default=5.0)
    p_mon.add_argument("--iterations", type=int, default=5)
    p_mon.add_argument("--baseline", default="data/baseline.json")
    p_mon.add_argument("--database", default="data/quantumpulse.db")
    p_mon.set_defaults(func=cmd_monitor)

    p_rep = sub.add_parser("report", help="summarise stored monitoring history")
    p_rep.add_argument("--database", default="data/quantumpulse.db")
    p_rep.add_argument("--limit", type=int, default=20)
    p_rep.set_defaults(func=cmd_report)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
