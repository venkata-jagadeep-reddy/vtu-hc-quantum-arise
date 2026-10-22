"""Gate-tampering localisation.

Two mechanisms are combined.

1. **Checkpoint triage (coarse).**  The first checkpoint that deviates from
   the reference localises the problem to the gates applied before it.  This is
   the "heartbeat" contribution: an output-only check can only say *that*
   something changed, a heartbeat says *where in the pipeline* it changed.

2. **Differential hypothesis testing (fine).**  Each candidate gate is mutated
   in turn - replaced, removed, or altered - and the resulting distribution is
   compared with the observed one using total variation distance.  The
   candidate whose best matching hypothesis reproduces the observation most
   closely is ranked first.

Why several checkpoints are used, not just the first
----------------------------------------------------
The map from single-gate modifications to output distributions is not
injective.  In the 4-qubit reference circuit two different modifications can
produce distributions that are within measurement noise of each other at the
*first* deviating checkpoint, so a decision based on that checkpoint alone is
a coin flip.  Later checkpoints usually break the tie because the two
hypotheses evolve differently through the remaining gates.

QuantumPulse therefore scores a candidate by the *mean* distance across all
deviating checkpoints.  This is measured, not assumed: see
``docs/results.md`` for the top-1 accuracy with and without fusion.

The hypothesis space is exactly the controlled tampering catalogue used for
evaluation (see :mod:`quantumpulse.tampering`).  QuantumPulse therefore
searches a *documented, bounded* space of single-gate modifications; it does
not claim to find arbitrary adversaries.

Confidence
----------
Candidates are converted to probabilities with a softmax over the negated
distances.  The temperature is tied to the calibrated threshold, so the
confidence is expressed in the same units as the detection statistic.  The
margin between the best and the second-best candidate is reported as
additional evidence, and is the honest indicator of ambiguity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np

from .backends import AerBackend
from .calibration import BaselineProfile
from .detection import total_variation_distance
from .execution import CheckpointObservation, counts_to_probabilities
from .program import QuantumPulseProgram
from .tampering import TamperSpec, apply_tamper, hypotheses_for_gate

METHOD = "checkpoint-triage + multi-checkpoint differential hypothesis testing"


@dataclass
class LocalizedCandidate:
    """One ranked candidate location with its supporting evidence."""

    gid: str
    position: int
    gate_description: str
    stage: str
    hypothesis_id: str
    hypothesis_description: str
    hypothesis_kind: str
    tvd_to_observed: float
    score: float
    rank: int
    per_checkpoint_tvd: Dict[str, float] = field(default_factory=dict)
    second_best_tvd: Optional[float] = None
    is_true_location: Optional[bool] = None
    noise_aware_tvd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gid": self.gid,
            "position": self.position,
            "gate": self.gate_description,
            "stage": self.stage,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis": self.hypothesis_description,
            "hypothesis_kind": self.hypothesis_kind,
            "tvd_to_observed": self.tvd_to_observed,
            "noise_aware_tvd": self.noise_aware_tvd,
            "per_checkpoint_tvd": self.per_checkpoint_tvd,
            "score": self.score,
            "rank": self.rank,
            "second_best_tvd": self.second_best_tvd,
            "is_true_location": self.is_true_location,
        }


@dataclass
class LocalizationReport:
    """Outcome of the localisation stage."""

    detected: bool
    method: str
    target_checkpoint: Optional[str]
    checkpoints_used: List[str]
    suspect_segment: List[str]
    candidates: List[LocalizedCandidate]
    top1_gid: Optional[str]
    top3_gids: List[str]
    confidence: float
    margin: float
    seconds: float = 0.0
    search_bound_checkpoint: Optional[str] = None
    """The checkpoint whose causal prefix defines the search space.

    When several checkpoints deviate, the candidate set is the prefix of the
    *latest* one used, so this can differ from ``target_checkpoint`` (the first
    deviating checkpoint, reported for triage).
    """

    ambiguous: bool = False
    """True when several candidates are statistically indistinguishable.

    Two distinct single-gate modifications can be *exactly* equivalent for a
    given circuit (measured, not assumed).  Reporting a single winner in that
    case would be dishonest, so the tie group is reported explicitly.
    """

    tied_gids: List[str] = field(default_factory=list)
    tie_tolerance: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def top1_correct(self) -> Optional[bool]:
        if self.top1_gid is None:
            return None
        for c in self.candidates:
            if c.gid == self.top1_gid:
                return c.is_true_location
        return None

    def top1_or_tie_correct(self, true_gid: Optional[str]) -> Optional[bool]:
        """Correct if the true gate is the top candidate *or* in the tie group."""

        if true_gid is None:
            return None
        return self.top1_gid == true_gid or true_gid in self.tied_gids

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.detected,
            "method": self.method,
            "target_checkpoint": self.target_checkpoint,
            "checkpoints_used": list(self.checkpoints_used),
            "search_bound_checkpoint": self.search_bound_checkpoint,
            "suspect_segment": list(self.suspect_segment),
            "candidates": [c.to_dict() for c in self.candidates],
            "top1_gid": self.top1_gid,
            "top3_gids": list(self.top3_gids),
            "ambiguous": self.ambiguous,
            "tied_gids": list(self.tied_gids),
            "tie_tolerance": self.tie_tolerance,
            "confidence": self.confidence,
            "margin": self.margin,
            "seconds": self.seconds,
            "notes": list(self.notes),
        }


def _as_observations(
    observations: Union[CheckpointObservation, Sequence[CheckpointObservation]]
) -> List[CheckpointObservation]:
    if isinstance(observations, CheckpointObservation):
        return [observations]
    ordered = sorted(observations, key=lambda o: o.position)
    if not ordered:
        raise ValueError("at least one observation is required")
    return ordered


def _predict(
    program: QuantumPulseProgram,
    hypothesis: TamperSpec,
    position: int,
    backend: AerBackend,
    shots: int,
    seed: Optional[int],
    exact: bool,
) -> np.ndarray:
    """Distribution produced by ``hypothesis`` observed at ``position``."""

    mutated = apply_tamper(program, hypothesis)
    prefix = mutated.prefix(position)
    if exact:
        return backend.exact_probabilities(prefix.to_circuit(measure=False))
    counts = backend.run_counts(prefix.to_circuit(measure=True), shots=shots, seed=seed)
    return counts_to_probabilities(counts, program.num_qubits)


def localize(
    program: QuantumPulseProgram,
    observations: Union[CheckpointObservation, Sequence[CheckpointObservation]],
    baseline: BaselineProfile,
    backend: AerBackend,
    shots: int = 4096,
    seed: Optional[int] = None,
    candidate_gids: Optional[Sequence[str]] = None,
    true_gid: Optional[str] = None,
    exact_sweep: bool = False,
    top_k: int = 3,
    hypothesis_seed_offset: int = 1,
    confirm_top_k: int = 3,
    max_checkpoints: int = 3,
) -> LocalizationReport:
    """Rank the candidate locations of a detected deviation.

    ``program`` must be the **reference** circuit (the expected behaviour);
    the hypotheses are single-gate modifications of it.  ``observations`` are
    the measurements actually recorded from the circuit under surveillance -
    pass every deviated checkpoint to enable multi-checkpoint fusion.

    ``candidate_gids`` restricts the search (normally the causal prefix of the
    earliest deviating checkpoint).  ``true_gid`` is only used to label the
    candidates for evaluation - it is never used by the search itself.
    """

    start = time.perf_counter()
    obs_list = _as_observations(observations)[:max_checkpoints]
    primary = obs_list[0]
    positions = [o.position for o in obs_list]

    if candidate_gids is None:
        # The modification must lie in the causal prefix of the *earliest*
        # deviating checkpoint: no gate applied later can influence a state
        # that was already measured.  This is the tightest valid bound, and it
        # is the heartbeat's main contribution to localisation.  Later
        # checkpoints still contribute evidence (below) but must not widen the
        # search.
        candidate_gids = program.causal_gates(program.checkpoint_by_id(primary.cp_id))
    candidate_gids = [g for g in candidate_gids if g in program.gate_ids()]

    notes: List[str] = [f"fused evidence from {len(obs_list)} checkpoint(s)"]
    if not candidate_gids:
        return LocalizationReport(
            detected=False,
            method=METHOD,
            target_checkpoint=primary.cp_id,
            checkpoints_used=[o.cp_id for o in obs_list],
            search_bound_checkpoint=primary.cp_id,
            suspect_segment=[],
            candidates=[],
            top1_gid=None,
            top3_gids=[],
            confidence=0.0,
            margin=0.0,
            seconds=time.perf_counter() - start,
            notes=["no candidate locations available for search"],
        )

    observed = [np.asarray(o.probs, dtype=float) for o in obs_list]

    # Hypotheses are evaluated with their own random seed so that the
    # comparison reflects genuine sampling noise instead of replaying the
    # identical stream.  All hypotheses share that seed (common random
    # numbers), which keeps the comparison between candidates fair.
    hypothesis_seed = None if seed is None else int(seed) + hypothesis_seed_offset

    per_candidate: List[Dict[str, Any]] = []

    for gid in candidate_gids:
        gate = program.gate(gid)
        best: Optional[Dict[str, Any]] = None
        second: Optional[float] = None

        for hypothesis in hypotheses_for_gate(gate):
            distances: Dict[str, float] = {}
            try:
                for obs, obs_probs, position in zip(obs_list, observed, positions):
                    predicted = _predict(
                        program,
                        hypothesis,
                        position,
                        backend,
                        shots,
                        hypothesis_seed,
                        exact_sweep,
                    )
                    distances[obs.cp_id] = total_variation_distance(obs_probs, predicted)
            except Exception as exc:  # pragma: no cover - defensive
                notes.append(f"hypothesis {hypothesis.tamper_id} failed: {exc}")
                continue

            aggregate = float(np.mean(list(distances.values())))
            if best is None or aggregate < best["tvd"]:
                second = best["tvd"] if best else aggregate
                best = {"tvd": aggregate, "hypothesis": hypothesis, "per_cp": distances}
            elif second is None or aggregate < second:
                second = aggregate

        if best is None:
            continue
        per_candidate.append(
            {
                "gid": gid,
                "position": program.position_of_gate(gid),
                "gate": gate.describe(),
                "stage": gate.stage,
                "tvd": best["tvd"],
                "per_cp": best["per_cp"],
                "hypothesis": best["hypothesis"],
                "second": second,
            }
        )

    per_candidate.sort(key=lambda item: item["tvd"])
    if not per_candidate:
        return LocalizationReport(
            detected=True,
            method=METHOD,
            target_checkpoint=primary.cp_id,
            checkpoints_used=[o.cp_id for o in obs_list],
            search_bound_checkpoint=primary.cp_id,
            suspect_segment=list(candidate_gids),
            candidates=[],
            top1_gid=None,
            top3_gids=[],
            confidence=0.0,
            margin=0.0,
            seconds=time.perf_counter() - start,
            notes=notes + ["no hypothesis could be evaluated"],
        )

    ref = baseline.checkpoints.get(primary.cp_id)
    best_tvd = per_candidate[0]["tvd"]
    scale = ref.threshold if ref is not None else 0.05
    temperature = max(1e-6, 0.25 * scale)

    weights = np.array(
        [np.exp(-(item["tvd"] - best_tvd) / temperature) for item in per_candidate]
    )
    weights = weights / weights.sum()

    candidates: List[LocalizedCandidate] = []
    for rank, (item, weight) in enumerate(zip(per_candidate, weights), start=1):
        candidates.append(
            LocalizedCandidate(
                gid=item["gid"],
                position=item["position"],
                gate_description=item["gate"],
                stage=item["stage"],
                hypothesis_id=item["hypothesis"].tamper_id,
                hypothesis_description=item["hypothesis"].description,
                hypothesis_kind=item["hypothesis"].kind,
                tvd_to_observed=float(item["tvd"]),
                per_checkpoint_tvd={k: float(v) for k, v in item["per_cp"].items()},
                score=float(weight),
                rank=rank,
                second_best_tvd=item["second"],
                is_true_location=(item["gid"] == true_gid) if true_gid else None,
            )
        )

    margin = (
        (per_candidate[1]["tvd"] - per_candidate[0]["tvd"]) / scale
        if len(per_candidate) > 1
        else float("inf")
    )

    # Tie group: candidates whose aggregate distance is within measurement
    # noise of the best one.  ``null_std`` is the calibrated spread of healthy
    # runs, so ``2 * null_std`` is a natural "indistinguishable" band.
    null_std = ref.null_std if ref is not None else 0.0
    tie_tolerance = max(2.0 * null_std, 1e-9)
    tied = [
        item["gid"]
        for item in per_candidate
        if item["tvd"] - per_candidate[0]["tvd"] <= tie_tolerance
    ]

    # When the sweep used the exact (sampling-free) statevector, re-score the
    # leading candidates with real noisy measurements.  This gives a
    # noise-aware number for the evidence table without paying for a noisy
    # run for every hypothesis.
    if exact_sweep:
        for candidate in candidates[:confirm_top_k]:
            hypothesis = next(
                h
                for h in hypotheses_for_gate(program.gate(candidate.gid))
                if h.tamper_id == candidate.hypothesis_id
            )
            try:
                dists = [
                    total_variation_distance(
                        obs_probs,
                        _predict(
                            program,
                            hypothesis,
                            position,
                            backend,
                            shots,
                            hypothesis_seed,
                            False,
                        ),
                    )
                    for obs_probs, position in zip(observed, positions)
                ]
                candidate.noise_aware_tvd = float(np.mean(dists))
            except Exception as exc:  # pragma: no cover - defensive
                notes.append(f"confirmation of {candidate.gid} failed: {exc}")

    return LocalizationReport(
        detected=True,
        method=METHOD,
        target_checkpoint=primary.cp_id,
        checkpoints_used=[o.cp_id for o in obs_list],
        search_bound_checkpoint=primary.cp_id,
        suspect_segment=list(candidate_gids),
        candidates=candidates,
        top1_gid=candidates[0].gid,
        top3_gids=[c.gid for c in candidates[:top_k]],
        confidence=float(weights[0]),
        margin=float(margin) if np.isfinite(margin) else 0.0,
        seconds=time.perf_counter() - start,
        ambiguous=len(tied) > 1,
        tied_gids=tied,
        tie_tolerance=float(tie_tolerance),
        notes=notes,
    )
