"""Optional machine-learning anomaly detector (Isolation Forest).

The project plan lists scikit-learn / Isolation Forest as an *optional*
secondary method.  It is implemented here as a genuine alternative that is
evaluated on the **same** runs as the calibrated total-variation detector, so
the comparison is fair and the numbers are measured rather than assumed.

Why the calibrated D_TV detector remains the primary method
-----------------------------------------------------------
* Its false-positive rate is set explicitly by ``alpha`` and validated against
  an empirical null distribution.
* Every alert is attributable to an interpretable statistic (a distance and a
  p-value), which matters because the output of this system is an *evidence
  report*, not just a label.
* It needs tens of healthy runs, not a large training set.

Isolation Forest can capture multi-dimensional deviations that a single
distance misses, but its decision threshold is learned rather than derived,
so its false-positive rate is only as trustworthy as its training sample.
Both are reported side by side in ``docs/results.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

import numpy as np

from .detection import total_variation_distance
from .execution import CheckpointObservation


def observation_features(
    observation: CheckpointObservation, expected_probs: Sequence[float]
) -> np.ndarray:
    """Feature vector describing one checkpoint observation.

    Components: the full outcome distribution, its Shannon entropy, its
    maximum probability, the number of non-negligible outcomes, and the total
    variation distance from the calibrated reference.  The last component
    makes the forest directly comparable with the primary detector, while the
    distribution itself lets it spot deviations that move mass without
    changing the overall distance much.
    """

    probs = np.asarray(observation.probs, dtype=float)
    expected = np.asarray(expected_probs, dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        entropy = float(-np.sum(probs[probs > 0] * np.log2(probs[probs > 0])))

    return np.concatenate(
        [
            probs,
            [
                entropy,
                float(probs.max()),
                float(np.sum(probs > 0.01)),
                total_variation_distance(probs, expected),
            ],
        ]
    )


@dataclass
class MLDetector:
    """Per-checkpoint Isolation Forest with a calibrated score threshold."""

    alpha: float = 0.005
    n_estimators: int = 200
    random_state: int = 20240
    models: Dict[str, Any] = field(default_factory=dict)
    thresholds: Dict[str, float] = field(default_factory=dict)
    feature_dim: int = 0

    def fit(
        self,
        healthy: Dict[str, List[Sequence[float]]],
        expected: Dict[str, Sequence[float]],
    ) -> "MLDetector":
        """Train one forest per checkpoint on healthy feature vectors.

        ``healthy`` maps a checkpoint id to a list of healthy probability
        vectors.  ``expected`` maps the same ids to the calibrated reference
        distribution used for the distance feature.
        """

        from sklearn.ensemble import IsolationForest

        for cp_id, vectors in healthy.items():
            features = np.vstack(
                [
                    observation_features(_as_observation(cp_id, v), expected[cp_id])
                    for v in vectors
                ]
            )
            self.feature_dim = features.shape[1]
            model = IsolationForest(
                n_estimators=self.n_estimators,
                contamination="auto",
                random_state=self.random_state,
            )
            model.fit(features)
            scores = -model.score_samples(features)
            self.models[cp_id] = model
            self.thresholds[cp_id] = float(
                np.quantile(scores, 1.0 - self.alpha, method="higher")
            )
        return self

    def anomaly_score(
        self, observation: CheckpointObservation, expected_probs: Sequence[float]
    ) -> float:
        model = self.models[observation.cp_id]
        features = observation_features(observation, expected_probs).reshape(1, -1)
        return float(-model.score_samples(features)[0])

    def predict(
        self, observation: CheckpointObservation, expected_probs: Sequence[float]
    ) -> bool:
        score = self.anomaly_score(observation, expected_probs)
        return bool(score > self.thresholds[observation.cp_id])

    def predict_run(
        self,
        observations: Sequence[CheckpointObservation],
        expected: Dict[str, Sequence[float]],
    ) -> bool:
        return any(
            self.predict(obs, expected[obs.cp_id])
            for obs in observations
            if obs.cp_id in self.models
        )


def _as_observation(cp_id: str, probs: Sequence[float]) -> CheckpointObservation:
    """Wrap a bare probability vector so it can be featurised."""

    probs = np.asarray(probs, dtype=float)
    return CheckpointObservation(
        cp_id=cp_id,
        name=cp_id,
        position=0,
        counts={},
        probs=probs,
        shots=int(sum(probs) > 0),
    )


@dataclass
class DetectorComparison:
    """Side-by-side detection metrics for D_TV and Isolation Forest."""

    trials_nominal: int = 0
    trials_tampered: int = 0
    tvd_detection_rate: float = 0.0
    tvd_false_positive_rate: float = 0.0
    ml_detection_rate: float = 0.0
    ml_false_positive_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trials_nominal": self.trials_nominal,
            "trials_tampered": self.trials_tampered,
            "tvd_detection_rate": self.tvd_detection_rate,
            "tvd_false_positive_rate": self.tvd_false_positive_rate,
            "ml_detection_rate": self.ml_detection_rate,
            "ml_false_positive_rate": self.ml_false_positive_rate,
        }


def compare_detectors(
    program,
    backend,
    baseline,
    tampers: Sequence,
    shots: int = 4096,
    alpha: float = 0.005,
    nominal_runs: int = 100,
    trials_per_tamper: int = 3,
    seed: int = 20240,
) -> DetectorComparison:
    """Run the same observations through both detectors and compare.

    This is deliberately separate from :mod:`quantumpulse.evaluation` so that
    the primary results never depend on the optional dependency.
    """

    from .execution import run_heartbeats
    from .tampering import apply_tamper
    from .verification import VerificationConfig, verify_heartbeats

    expected = {k: v.expected_probs for k, v in baseline.checkpoints.items()}
    verification = VerificationConfig()

    # ---- training set: healthy runs ----
    healthy: Dict[str, List[np.ndarray]] = {k: [] for k in baseline.checkpoints}
    for i in range(max(nominal_runs, 20)):
        heartbeat = run_heartbeats(program, backend, shots=shots, seed=seed + i)
        for obs in heartbeat.observations:
            healthy[obs.cp_id].append(obs.probs)

    detector = MLDetector(alpha=alpha, random_state=seed).fit(healthy, expected)

    # ---- held-out healthy runs ----
    tvd_fp = ml_fp = 0
    for i in range(nominal_runs):
        run_seed = seed + 5000 + i
        heartbeat = run_heartbeats(program, backend, shots=shots, seed=run_seed)
        verdict = verify_heartbeats(
            heartbeat.observations, baseline, verification, program=program
        )
        tvd_fp += verdict.flagged
        ml_fp += detector.predict_run(heartbeat.observations, expected)

    # ---- tampered runs ----
    tvd_tp = ml_tp = n_tampered = 0
    for index, tamper in enumerate(tampers):
        tampered = apply_tamper(program, tamper)
        for trial in range(trials_per_tamper):
            run_seed = seed + 100_000 + index * 100 + trial
            heartbeat = run_heartbeats(tampered, backend, shots=shots, seed=run_seed)
            verdict = verify_heartbeats(
                heartbeat.observations, baseline, verification, program=tampered
            )
            tvd_tp += verdict.flagged
            ml_tp += detector.predict_run(heartbeat.observations, expected)
            n_tampered += 1

    return DetectorComparison(
        trials_nominal=nominal_runs,
        trials_tampered=n_tampered,
        tvd_detection_rate=tvd_tp / n_tampered if n_tampered else 0.0,
        tvd_false_positive_rate=tvd_fp / nominal_runs if nominal_runs else 0.0,
        ml_detection_rate=ml_tp / n_tampered if n_tampered else 0.0,
        ml_false_positive_rate=ml_fp / nominal_runs if nominal_runs else 0.0,
    )
