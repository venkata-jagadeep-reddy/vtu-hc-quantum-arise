"""Heartbeat verification and anomaly decision.

A *verification* takes the measurements collected at every checkpoint and
compares each one with the calibrated reference behaviour.  The result is one
verdict per checkpoint plus an overall integrity verdict.

Decision rule
-------------
Primary rule (default, ``"tvd"``):

    flagged  <=>  D_TV(observed, expected) > calibrated threshold

The threshold comes from the empirical null of healthy runs, so this rule has
a directly measured false-positive rate (see :mod:`quantumpulse.calibration`).

Optional secondary rule (``"tvd+chi2"``) additionally flags a checkpoint when
the Pearson goodness-of-fit test rejects at ``chi2_alpha`` *and* the observed
distance is larger than the noise floor.  The chi-square test has a
theoretically controlled level, so it adds sensitivity at a known cost in
false positives.

Interpretation
--------------
A flag is **evidence of a deviation**, not proof of malicious tampering.  The
report therefore always separates "deviation detected" from "modification
localised", and records the noise floor so the reader can see how much of the
deviation is explained by ordinary hardware noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .calibration import BaselineProfile
from .detection import DistributionComparison, compare_distributions
from .execution import CheckpointObservation


@dataclass
class VerificationConfig:
    """Decision-rule configuration."""

    rule: str = "tvd"
    """``"tvd"`` (threshold on D_TV) or ``"tvd+chi2"`` (adds a chi-square test)."""

    chi2_alpha: float = 0.005
    """Significance level of the supplementary chi-square test."""


@dataclass
class CheckpointVerdict:
    """Verification outcome for one heartbeat checkpoint."""

    cp_id: str
    name: str
    position: int
    stage: str
    observed_tvd: float
    threshold: float
    null_mean: float
    noise_floor: float
    """Systematic distance between the (noise-aware) reference distribution and
    the ideal noise-free state - i.e. how much deviation the noise model
    alone already explains."""

    null_p_value: float
    anomaly_score: float
    """Confidence that the deviation is not a healthy fluctuation, i.e.
    ``1 - p_value`` under the calibrated null.  For a flagged checkpoint this
    is >= ``1 - alpha`` by construction."""

    comparison: DistributionComparison
    flagged: bool
    reason: str = ""

    @property
    def excess_over_threshold(self) -> float:
        """Distance above the calibrated threshold (negative when healthy)."""

        return self.observed_tvd - self.threshold

    @property
    def excess_over_null(self) -> float:
        """Distance above the mean of the healthy null distribution."""

        return self.observed_tvd - self.null_mean

    @property
    def threshold_utilisation(self) -> float:
        """Observed distance as a multiple of the calibrated threshold."""

        return self.observed_tvd / self.threshold if self.threshold > 0 else float("inf")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cp_id": self.cp_id,
            "name": self.name,
            "position": self.position,
            "stage": self.stage,
            "observed_tvd": self.observed_tvd,
            "threshold": self.threshold,
            "null_mean": self.null_mean,
            "noise_floor": self.noise_floor,
            "null_p_value": self.null_p_value,
            "anomaly_score": self.anomaly_score,
            "threshold_utilisation": self.threshold_utilisation,
            "flagged": self.flagged,
            "reason": self.reason,
            "excess_over_threshold": self.excess_over_threshold,
            "excess_over_null": self.excess_over_null,
            "statistics": self.comparison.to_dict(),
        }


@dataclass
class IntegrityVerdict:
    """Overall result of one heartbeat verification round."""

    program_name: str
    verdicts: List[CheckpointVerdict]
    flagged: bool
    overall_anomaly_score: float
    first_bad_checkpoint: Optional[str] = None
    last_good_checkpoint: Optional[str] = None
    suspects: List[str] = field(default_factory=list)
    """Sound localisation search space: all gates before the first deviating
    checkpoint."""

    delta_band: List[str] = field(default_factory=list)
    """Narrow band between the last healthy and the first deviating checkpoint."""

    family_wise_false_positive_bound: float = 0.0
    """Union bound on the *run-level* false-positive rate.

    ``baseline.alpha`` applies to each checkpoint individually.  A round is
    flagged if any checkpoint exceeds its threshold, so with ``k`` checkpoints
    the run-level rate is at most ``k * alpha`` (union bound, no independence
    assumption).  The bound is reported so that the per-checkpoint level is
    never silently presented as the run-level rate.
    """

    notes: List[str] = field(default_factory=list)

    def verdict(self, cp_id: str) -> CheckpointVerdict:
        for v in self.verdicts:
            if v.cp_id == cp_id:
                return v
        raise KeyError(cp_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "program_name": self.program_name,
            "flagged": self.flagged,
            "overall_anomaly_score": self.overall_anomaly_score,
            "first_bad_checkpoint": self.first_bad_checkpoint,
            "last_good_checkpoint": self.last_good_checkpoint,
            "suspects": list(self.suspects),
            "delta_band": list(self.delta_band),
            "family_wise_false_positive_bound": self.family_wise_false_positive_bound,
            "notes": list(self.notes),
            "checkpoints": [v.to_dict() for v in self.verdicts],
        }


def verify_heartbeats(
    observations: Sequence[CheckpointObservation],
    baseline: BaselineProfile,
    config: Optional[VerificationConfig] = None,
    program=None,
) -> IntegrityVerdict:
    """Compare every checkpoint observation with its calibrated reference."""

    config = config or VerificationConfig()
    verdicts: List[CheckpointVerdict] = []
    notes: List[str] = []

    for obs in observations:
        ref = baseline.checkpoints.get(obs.cp_id)
        if ref is None:
            notes.append(f"no baseline for checkpoint {obs.cp_id}; skipped")
            continue

        if obs.shots != baseline.shots:
            # The threshold is derived from the finite-shot null distribution,
            # so it is only valid for the shot count it was calibrated with.
            # Comparing across shot counts would silently invalidate the
            # false-positive rate.
            raise ValueError(
                f"shot-count mismatch at {obs.cp_id}: observation used {obs.shots} "
                f"shots but the baseline was calibrated with {baseline.shots}. "
                "Re-calibrate with the same --shots value."
            )

        comparison = compare_distributions(
            obs.probs,
            ref.expected_probs,
            counts=obs.counts,
            num_qubits=int(len(obs.probs)).bit_length() - 1,
        )
        p_null = ref.null_p_value(comparison.tvd)
        score = float(max(0.0, 1.0 - p_null))

        flagged = comparison.tvd > ref.threshold
        reason = (
            f"D_TV={comparison.tvd:.4f} > threshold={ref.threshold:.4f}"
            if flagged
            else f"D_TV={comparison.tvd:.4f} <= threshold={ref.threshold:.4f}"
        )

        if not flagged and config.rule == "tvd+chi2":
            chi2_rejects = (
                np.isfinite(comparison.chi2_p_value)
                and comparison.chi2_p_value < config.chi2_alpha
            )
            if chi2_rejects and comparison.tvd > ref.null_mean:
                flagged = True
                reason += (
                    f"; chi-square p={comparison.chi2_p_value:.3g} < {config.chi2_alpha}"
                    f" and deviation exceeds the healthy null mean {ref.null_mean:.4f}"
                )

        stage = ref.stage
        verdicts.append(
            CheckpointVerdict(
                cp_id=obs.cp_id,
                name=obs.name,
                position=obs.position,
                stage=stage,
                observed_tvd=comparison.tvd,
                threshold=ref.threshold,
                null_mean=ref.null_mean,
                noise_floor=ref.noise_floor,
                null_p_value=p_null,
                anomaly_score=score,
                comparison=comparison,
                flagged=flagged,
                reason=reason,
            )
        )

    flagged_verdicts = [v for v in verdicts if v.flagged]
    # The headline score only reflects *flagged* checkpoints: a healthy
    # checkpoint's 1-p value is uniformly distributed and carries no
    # information on its own.
    overall_score = float(max((v.anomaly_score for v in flagged_verdicts), default=0.0))

    first_bad = flagged_verdicts[0].cp_id if flagged_verdicts else None
    last_good = None
    for v in verdicts:
        if v.flagged:
            break
        last_good = v.cp_id

    suspects: List[str] = []
    delta_band: List[str] = []
    if flagged_verdicts and program is not None:
        cp = program.checkpoint_by_id(first_bad)
        # Sound search space: every gate applied before the first deviating
        # checkpoint could have caused the deviation.
        suspects = program.causal_gates(cp)
        # Narrow band: where the deviation first became observable.
        delta_band = program.delta_band(cp)

    n_tested = len(verdicts)
    family_wise_bound = min(1.0, n_tested * baseline.alpha) if n_tested else 0.0
    notes.append(
        f"{n_tested} checkpoint(s) tested at alpha={baseline.alpha} each; "
        f"run-level false-positive rate <= {family_wise_bound:.4f} (union bound)."
    )

    if flagged_verdicts:
        notes.append(
            f"{len(flagged_verdicts)} of {len(verdicts)} checkpoints deviated "
            f"beyond the calibrated threshold."
        )
        if delta_band and suspects and set(delta_band) != set(suspects):
            notes.append(
                "The deviation first became observable in the band "
                f"{', '.join(delta_band)}, but the search space is widened to all "
                f"{len(suspects)} gates preceding {first_bad} because a modification "
                "can be distributionally invisible at an intermediate checkpoint."
            )
    else:
        notes.append(
            "All checkpoints are within the calibrated integrity threshold "
            "(no evidence of tampering)."
        )

    return IntegrityVerdict(
        program_name=baseline.program_name,
        verdicts=verdicts,
        flagged=bool(flagged_verdicts),
        overall_anomaly_score=overall_score,
        first_bad_checkpoint=first_bad,
        last_good_checkpoint=last_good,
        suspects=suspects,
        delta_band=delta_band,
        family_wise_false_positive_bound=family_wise_bound,
        notes=notes,
    )
