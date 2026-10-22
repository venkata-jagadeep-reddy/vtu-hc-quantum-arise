"""Evaluation harness.

Everything reported by QuantumPulse is produced here from actual simulator
runs; no accuracy figure in this project is written by hand.

The harness answers four questions:

Detection
    Given healthy runs and runs with a controlled single-gate modification,
    what are the true positives, false positives, false negatives, detection
    rate and false-positive rate?

Localisation
    When a deviation *is* detected, does the ranked candidate list contain
    the gate that was actually modified (top-1 and top-3 accuracy)?

Heartbeat versus output-only
    The same tampered runs are also checked using only the final output, and
    localised by searching the *whole* circuit.  This quantifies the value
    added by the heartbeat checkpoints instead of merely asserting it.

Overhead
    Wall-clock time of a heartbeat round versus a single output-only run.

Noise sensitivity
    Every measurement is repeated under several noise profiles (ideal and
    depolarising/thermal), because a detector that only works on a noiseless
    simulator is not a useful prototype.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .backends import AerBackend
from .calibration import BaselineProfile, calibrate
from .config import NOISE_PROFILES, ExperimentConfig
from .execution import run_full_circuit, run_heartbeats
from .localization import localize
from .program import QuantumPulseProgram
from .tampering import (
    TamperSpec,
    apply_tamper,
    build_tamper_catalog,
    exact_effect,
    is_effectless,
)
from .verification import VerificationConfig, verify_heartbeats


@dataclass
class TrialOutcome:
    """Result of a single monitored run."""

    profile: str
    label: str
    tamper_id: Optional[str] = None
    tamper_kind: Optional[str] = None
    target_gid: Optional[str] = None
    trial: int = 0
    seed: Optional[int] = None
    effect_max: float = 0.0
    """Exact (sampling-free) effect of the modification on any checkpoint."""

    effect_final: float = 0.0
    effectless: bool = False
    """True when the modification leaves every checkpoint distribution identical
    and is therefore undetectable by construction."""

    flagged_heartbeat: bool = False
    flagged_output_only: bool = False
    first_bad_cp: Optional[str] = None
    anomaly_score: float = 0.0
    output_anomaly_score: float = 0.0
    top1_gid: Optional[str] = None
    top1_correct: Optional[bool] = None
    top3_correct: Optional[bool] = None
    top1_or_tie_correct: Optional[bool] = None
    localization_ambiguous: bool = False
    confidence: float = 0.0
    output_top1_gid: Optional[str] = None
    output_top1_correct: Optional[bool] = None
    output_top3_correct: Optional[bool] = None
    hb_seconds: float = 0.0
    output_seconds: float = 0.0
    localization_seconds: float = 0.0
    output_localization_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProfileMetrics:
    """Aggregate metrics for one noise profile."""

    profile: str
    nominal_runs: int = 0
    tampered_runs: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    detection_rate: float = 0.0
    false_positive_rate: float = 0.0
    precision: float = 0.0
    f1: float = 0.0
    output_only_detection_rate: float = 0.0
    output_only_false_positive_rate: float = 0.0
    effectless_scenarios: int = 0
    """Number of tampering scenarios that leave the circuit behaviour
    mathematically unchanged and are therefore undetectable by any
    distribution-based monitor."""

    effective_scenarios: int = 0
    detection_rate_effective: float = 0.0
    """Detection rate restricted to scenarios that actually change the output."""

    localisation_trials: int = 0
    top1_accuracy: float = 0.0
    top1_or_tie_accuracy: float = 0.0
    """Top-1 accuracy counting statistically indistinguishable candidates as
    correct.  The gap between this and ``top1_accuracy`` is the identifiability
    limit of the circuit, not a search failure."""

    top3_accuracy: float = 0.0
    ambiguous_rate: float = 0.0
    mean_confidence: float = 0.0
    output_only_top1_accuracy: float = 0.0
    output_only_top3_accuracy: float = 0.0
    mean_heartbeat_seconds: float = 0.0
    mean_output_seconds: float = 0.0
    overhead_ratio: float = 0.0
    mean_localization_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    """Complete evaluation output."""

    config: Dict[str, Any]
    created_at: str
    metrics: Dict[str, ProfileMetrics] = field(default_factory=dict)
    outcomes: List[TrialOutcome] = field(default_factory=list)
    baselines: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    scenarios: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    """Per-scenario detection summary (which scenarios were missed and why)."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config,
            "created_at": self.created_at,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "scenarios": self.scenarios,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "baselines": self.baselines,
        }

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "evaluation.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, default=str)

        try:
            import pandas as pd

            frame = pd.DataFrame([o.to_dict() for o in self.outcomes])
            frame.to_csv(os.path.join(directory, "trials.csv"), index=False)
            metrics = pd.DataFrame(
                [m.to_dict() for m in self.metrics.values()]
            )
            metrics.to_csv(os.path.join(directory, "metrics.csv"), index=False)
        except ImportError:  # pragma: no cover
            pass
        return path


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def compute_metrics(outcomes: Sequence[TrialOutcome]) -> Dict[str, ProfileMetrics]:
    """Aggregate trial outcomes into per-profile metrics."""

    metrics: Dict[str, ProfileMetrics] = {}
    profiles = sorted({o.profile for o in outcomes})

    for profile in profiles:
        subset = [o for o in outcomes if o.profile == profile]
        nominal = [o for o in subset if o.label == "nominal"]
        tampered = [o for o in subset if o.label == "tampered"]

        m = ProfileMetrics(profile=profile)
        m.nominal_runs = len(nominal)
        m.tampered_runs = len(tampered)

        m.false_positives = sum(1 for o in nominal if o.flagged_heartbeat)
        m.true_negatives = len(nominal) - m.false_positives
        m.true_positives = sum(1 for o in tampered if o.flagged_heartbeat)
        m.false_negatives = len(tampered) - m.true_positives

        m.detection_rate = _safe_div(m.true_positives, m.true_positives + m.false_negatives)
        m.false_positive_rate = _safe_div(m.false_positives, m.false_positives + m.true_negatives)
        m.precision = _safe_div(m.true_positives, m.true_positives + m.false_positives)
        m.f1 = _safe_div(2 * m.precision * m.detection_rate, m.precision + m.detection_rate)

        m.output_only_detection_rate = _safe_div(
            sum(1 for o in tampered if o.flagged_output_only),
            len(tampered),
        )
        m.output_only_false_positive_rate = _safe_div(
            sum(1 for o in nominal if o.flagged_output_only),
            len(nominal),
        )

        effectless = [o for o in tampered if o.effectless]
        effective = [o for o in tampered if not o.effectless]
        m.effectless_scenarios = len({o.tamper_id for o in effectless})
        m.effective_scenarios = len({o.tamper_id for o in effective})
        m.detection_rate_effective = _safe_div(
            sum(1 for o in effective if o.flagged_heartbeat), len(effective)
        )

        localised = [
            o
            for o in tampered
            if o.flagged_heartbeat and o.top1_correct is not None
        ]
        m.localisation_trials = len(localised)
        if localised:
            m.top1_accuracy = sum(1 for o in localised if o.top1_correct) / len(localised)
            m.top1_or_tie_accuracy = (
                sum(1 for o in localised if o.top1_or_tie_correct) / len(localised)
            )
            m.top3_accuracy = sum(1 for o in localised if o.top3_correct) / len(localised)
            m.ambiguous_rate = (
                sum(1 for o in localised if o.localization_ambiguous) / len(localised)
            )
            m.mean_confidence = sum(o.confidence for o in localised) / len(localised)
            m.mean_localization_seconds = (
                sum(o.localization_seconds for o in localised) / len(localised)
            )

        out_localised = [
            o
            for o in tampered
            if o.flagged_output_only and o.output_top1_correct is not None
        ]
        if out_localised:
            m.output_only_top1_accuracy = (
                sum(1 for o in out_localised if o.output_top1_correct) / len(out_localised)
            )
            m.output_only_top3_accuracy = (
                sum(1 for o in out_localised if o.output_top3_correct) / len(out_localised)
            )

        if nominal:
            m.mean_heartbeat_seconds = sum(o.hb_seconds for o in nominal) / len(nominal)
            m.mean_output_seconds = sum(o.output_seconds for o in nominal) / len(nominal)
            m.overhead_ratio = _safe_div(
                m.mean_heartbeat_seconds, m.mean_output_seconds
            )

        metrics[profile] = m

    return metrics


def summarize_scenarios(outcomes: Sequence[TrialOutcome]) -> Dict[str, Dict[str, Any]]:
    """Per-scenario detection summary.

    This is the table that makes a missed detection explainable: a scenario
    can be missed because the modification has no effect at all, because its
    effect is below the calibrated threshold, or because the monitor was
    genuinely at fault.
    """

    grouped: Dict[Tuple[str, str], List[TrialOutcome]] = {}
    for outcome in outcomes:
        if outcome.label != "tampered" or not outcome.tamper_id:
            continue
        grouped.setdefault((outcome.profile, outcome.tamper_id), []).append(outcome)

    summary: Dict[str, Dict[str, Any]] = {}
    for (profile, tamper_id), rows in grouped.items():
        detected = sum(1 for r in rows if r.flagged_heartbeat)
        localised = [r for r in rows if r.top1_correct is not None]
        summary[f"{profile}::{tamper_id}"] = {
            "profile": profile,
            "tamper_id": tamper_id,
            "tamper_kind": rows[0].tamper_kind,
            "target_gid": rows[0].target_gid,
            "trials": len(rows),
            "detected": detected,
            "detection_rate": detected / len(rows) if rows else 0.0,
            "effect_max": rows[0].effect_max,
            "effect_final": rows[0].effect_final,
            "effectless": rows[0].effectless,
            "localisation_trials": len(localised),
            "top1_correct": sum(1 for r in localised if r.top1_correct),
            "first_bad_cp": next(
                (r.first_bad_cp for r in rows if r.flagged_heartbeat), None
            ),
        }
    return summary


@dataclass
class LocResult:
    """Outcome of one localisation attempt (used by the evaluation harness)."""

    top1_gid: Optional[str] = None
    top1_correct: Optional[bool] = None
    top3_correct: Optional[bool] = None
    tie_correct: Optional[bool] = None
    ambiguous: bool = False
    confidence: float = 0.0
    seconds: float = 0.0


def _run_localisation(
    reference: QuantumPulseProgram,
    observations,
    baseline: BaselineProfile,
    backend: AerBackend,
    shots: int,
    seed: Optional[int],
    true_gid: Optional[str],
    exact_sweep: bool,
    candidate_gids: Optional[Sequence[str]],
    cancelled: bool = False,
) -> LocResult:
    """Localise using the reference program as the hypothesis base.

    ``true_gid`` is used only to label the result for evaluation - never by
    the search itself.
    """

    report = localize(
        reference,
        observations,
        baseline,
        backend,
        shots=shots,
        seed=seed,
        candidate_gids=candidate_gids,
        true_gid=true_gid,
        exact_sweep=exact_sweep,
    )
    if not report.candidates:
        return LocResult(seconds=report.seconds)

    top3 = set(report.top3_gids)
    return LocResult(
        top1_gid=report.top1_gid,
        top1_correct=bool(true_gid and report.top1_gid == true_gid),
        top3_correct=bool(true_gid and true_gid in top3),
        tie_correct=report.top1_or_tie_correct(true_gid),
        ambiguous=report.ambiguous,
        confidence=report.confidence,
        seconds=report.seconds,
    )


def format_markdown(path: str) -> str:
    """Render a saved ``evaluation.json`` as markdown tables.

    The documentation must be reproducible from the artefacts, so the tables in
    ``docs/results.md`` are produced by this function rather than typed by
    hand.
    """

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    config = data.get("config", {})
    metrics: Dict[str, Dict[str, Any]] = data.get("metrics", {})
    scenarios: Dict[str, Dict[str, Any]] = data.get("scenarios", {})
    outcomes: List[Dict[str, Any]] = data.get("outcomes", [])

    lines: List[str] = []
    lines.append(f"_Generated from `{path}` (created {data.get('created_at', '?')})._")
    lines.append("")

    lines.append("### Configuration")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("| --- | --- |")
    for key in (
        "shots",
        "alpha",
        "calibration_runs",
        "trials",
        "nominal_trials",
        "seed",
        "noise_profiles",
    ):
        if key in config:
            lines.append(f"| `{key}` | {config[key]} |")
    lines.append("")

    lines.append("### Detection")
    lines.append("")
    lines.append(
        "| Noise profile | Healthy | Tampered | TP | FP | FN | Detection | FPR | "
        "Precision | F1 | Detection (effective only) |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for name, m in metrics.items():
        lines.append(
            f"| `{name}` | {m['nominal_runs']} | {m['tampered_runs']} | "
            f"{m['true_positives']} | {m['false_positives']} | "
            f"{m['false_negatives']} | {m['detection_rate']:.3f} | "
            f"{m['false_positive_rate']:.3f} | {m['precision']:.3f} | "
            f"{m['f1']:.3f} | {m.get('detection_rate_effective', m['detection_rate']):.3f} |"
        )
    lines.append("")

    lines.append("### Localisation")
    lines.append("")
    lines.append(
        "| Noise profile | Trials | Top-1 | Top-1 or tie | Top-3 | Ambiguous | "
        "Mean confidence | Output-only top-1 | Output-only top-3 |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for name, m in metrics.items():
        if not m.get("localisation_trials"):
            continue
        lines.append(
            f"| `{name}` | {m['localisation_trials']} | {m['top1_accuracy']:.3f} | "
            f"{m.get('top1_or_tie_accuracy', 0.0):.3f} | {m['top3_accuracy']:.3f} | "
            f"{m.get('ambiguous_rate', 0.0):.3f} | {m['mean_confidence']:.3f} | "
            f"{m['output_only_top1_accuracy']:.3f} | "
            f"{m['output_only_top3_accuracy']:.3f} |"
        )
    lines.append("")

    lines.append("### Cost")
    lines.append("")
    lines.append(
        "| Noise profile | Heartbeat round (s) | Output-only (s) | Overhead | "
        "Mean localisation (s) |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for name, m in metrics.items():
        lines.append(
            f"| `{name}` | {m['mean_heartbeat_seconds']:.3f} | "
            f"{m['mean_output_seconds']:.3f} | {m['overhead_ratio']:.2f}x | "
            f"{m['mean_localization_seconds']:.3f} |"
        )
    lines.append("")

    lines.append("### Missed detections")
    lines.append("")
    missed = [
        (key, s)
        for key, s in scenarios.items()
        if s.get("detected", 0) == 0
    ]
    if not missed:
        lines.append("None: every scenario was detected at least once.")
    else:
        lines.append("| Profile | Scenario | Gate | Effectless | Exact effect |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for key, s in missed:
            lines.append(
                f"| `{s['profile']}` | `{s['tamper_id']}` | `{s['target_gid']}` | "
                f"{s['effectless']} | {s['effect_max']:.4f} |"
            )
    lines.append("")

    lines.append("### Localisation errors")
    lines.append("")
    errors: Dict[tuple, Dict[str, Any]] = {}
    for outcome in outcomes:
        if outcome.get("top1_correct") is not False:
            continue
        key = (outcome["profile"], outcome["tamper_id"], outcome.get("top1_gid"))
        errors.setdefault(
            key,
            {
                "target": outcome.get("target_gid"),
                "predicted": outcome.get("top1_gid"),
                "tie": outcome.get("top1_or_tie_correct"),
                "ambiguous": outcome.get("localization_ambiguous"),
                "count": 0,
            },
        )["count"] += 1
    if not errors:
        lines.append("None.")
    else:
        lines.append(
            "| Profile | Scenario | True gate | Top-1 guess | In tie group | Trials |"
        )
        lines.append("| --- | --- | --- | --- | --- | ---: |")
        for (profile, tamper_id, _), info in sorted(errors.items()):
            lines.append(
                f"| `{profile}` | `{tamper_id}` | `{info['target']}` | "
                f"`{info['predicted']}` | {info['tie']} | {info['count']} |"
            )
    lines.append("")
    return "\n".join(lines)


def run_evaluation(
    program: QuantumPulseProgram,
    config: Optional[ExperimentConfig] = None,
    tampers: Optional[Sequence[TamperSpec]] = None,
    verification_config: Optional[VerificationConfig] = None,
    run_localization: bool = True,
    localization_trials: int = 3,
    exact_sweep: bool = False,
    progress: bool = True,
) -> EvaluationResult:
    """Run the full evaluation matrix and return measured results."""

    config = config or ExperimentConfig()
    verification_config = verification_config or VerificationConfig()
    if tampers is None:
        tampers = build_tamper_catalog(program)

    result = EvaluationResult(
        config=config.to_dict(), created_at=datetime.now(timezone.utc).isoformat()
    )

    for profile_name in config.noise_profiles:
        noise_config = NOISE_PROFILES.get(profile_name)
        if noise_config is None:
            continue
        if progress:
            print(f"[{profile_name}] calibrating baseline ...")

        backend = AerBackend(noise_config=noise_config, seed=config.seed)
        baseline = calibrate(
            program,
            backend,
            shots=config.shots,
            n_runs=config.calibration_runs,
            alpha=config.alpha,
            seed=config.seed,
        )
        result.baselines[profile_name] = {
            cp_id: {
                "threshold": b.threshold,
                "null_mean": b.null_mean,
                "null_std": b.null_std,
                "noise_floor": b.noise_floor,
            }
            for cp_id, b in baseline.checkpoints.items()
        }

        # ---------------- healthy (nominal) runs ----------------
        # Seeds must be disjoint from the calibration seeds, otherwise a
        # nominal run would be scored against a reference mean that already
        # contains it (an optimistically small distance, hence a downward-biased
        # false-positive estimate).  Tampered runs already use a different block.
        for trial in range(config.nominal_trials):
            seed = config.seed + 500_000 + trial
            heartbeat = run_heartbeats(program, backend, shots=config.shots, seed=seed)
            verdict = verify_heartbeats(
                heartbeat.observations, baseline, verification_config, program=program
            )
            output_obs = run_full_circuit(
                program, backend, shots=config.shots, seed=seed
            )
            output_verdict = verify_heartbeats(
                [output_obs], baseline, verification_config, program=program
            )
            result.outcomes.append(
                TrialOutcome(
                    profile=profile_name,
                    label="nominal",
                    trial=trial,
                    seed=seed,
                    flagged_heartbeat=verdict.flagged,
                    flagged_output_only=output_verdict.flagged,
                    first_bad_cp=verdict.first_bad_checkpoint,
                    anomaly_score=verdict.overall_anomaly_score,
                    output_anomaly_score=output_verdict.overall_anomaly_score,
                    hb_seconds=heartbeat.total_seconds,
                    output_seconds=output_obs.seconds,
                )
            )

        # ---------------- tampered runs ----------------
        for index, tamper in enumerate(tampers):
            tampered_program = apply_tamper(program, tamper)
            effects = exact_effect(program, tampered_program, backend)
            effectless = is_effectless(effects)
            for trial in range(config.trials):
                seed = config.seed + 1000 * (index + 1) + trial
                heartbeat = run_heartbeats(
                    tampered_program, backend, shots=config.shots, seed=seed
                )
                verdict = verify_heartbeats(
                    heartbeat.observations,
                    baseline,
                    verification_config,
                    program=tampered_program,
                )
                output_obs = run_full_circuit(
                    tampered_program, backend, shots=config.shots, seed=seed
                )
                output_verdict = verify_heartbeats(
                    [output_obs], baseline, verification_config, program=tampered_program
                )

                outcome = TrialOutcome(
                    profile=profile_name,
                    label="tampered",
                    tamper_id=tamper.tamper_id,
                    tamper_kind=tamper.kind,
                    target_gid=tamper.target_gid,
                    trial=trial,
                    seed=seed,
                    effect_max=effects["max"],
                    effect_final=effects.get("final", 0.0),
                    effectless=effectless,
                    flagged_heartbeat=verdict.flagged,
                    flagged_output_only=output_verdict.flagged,
                    first_bad_cp=verdict.first_bad_checkpoint,
                    anomaly_score=verdict.overall_anomaly_score,
                    output_anomaly_score=output_verdict.overall_anomaly_score,
                    hb_seconds=heartbeat.total_seconds,
                    output_seconds=output_obs.seconds,
                )

                if run_localization and trial < localization_trials:
                    flagged_observations = [
                        heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged
                    ]
                    if verdict.flagged and flagged_observations:
                        loc = _run_localisation(
                            program,
                            flagged_observations,
                            baseline,
                            backend,
                            config.shots,
                            seed,
                            tamper.target_gid,
                            exact_sweep,
                            None,
                        )
                        outcome.top1_gid = loc.top1_gid
                        outcome.top1_correct = loc.top1_correct
                        outcome.top3_correct = loc.top3_correct
                        outcome.top1_or_tie_correct = loc.tie_correct
                        outcome.localization_ambiguous = loc.ambiguous
                        outcome.confidence = loc.confidence
                        outcome.localization_seconds = loc.seconds

                    if output_verdict.flagged:
                        loc = _run_localisation(
                            program,
                            [output_obs],
                            baseline,
                            backend,
                            config.shots,
                            seed,
                            tamper.target_gid,
                            exact_sweep,
                            # Output-only baseline must search the whole circuit.
                            program.gate_ids(),
                        )
                        outcome.output_top1_gid = loc.top1_gid
                        outcome.output_top1_correct = loc.top1_correct
                        outcome.output_top3_correct = loc.top3_correct
                        outcome.output_localization_seconds = loc.seconds

                result.outcomes.append(outcome)

            if progress and (index + 1) % 10 == 0:
                print(f"[{profile_name}] {index + 1}/{len(tampers)} tampering scenarios")

    result.metrics = compute_metrics(result.outcomes)
    result.scenarios = summarize_scenarios(result.outcomes)
    return result
