"""Baseline calibration and threshold selection.

Why calibration is mandatory
----------------------------
Even a perfectly healthy circuit produces a non-zero distance against its
reference distribution, because (a) the measurement is finite-shot and (b) the
hardware/simulator is noisy.  A hand-picked threshold would either flood the
operator with false positives or silently miss weak tampering.

QuantumPulse therefore builds an *empirical null distribution* of the
detection statistic by repeatedly executing the healthy reference circuit
under the **same** noise model and shot count that will be used in production.
The alert threshold is then read off that null distribution at the configured
false-positive rate ``alpha``.

Two quantities are stored per checkpoint:

``expected_probs``
    the mean outcome distribution of healthy runs - the noise-aware reference
    the detector compares against;

``noise_floor``
    the distance between that reference and the ideal (noise-free) state,
    i.e. how much deviation hardware noise alone already explains.

The distance of a *suspect* observation from ``expected_probs`` is therefore
directly comparable to the null spread, and the part of the deviation that is
*not* explained by noise can be reported as evidence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from scipy import stats

from .backends import AerBackend
from .detection import total_variation_distance
from .program import QuantumPulseProgram


@dataclass
class CheckpointBaseline:
    """Calibrated null model for a single heartbeat checkpoint."""

    cp_id: str
    name: str
    position: int
    stage: str
    ideal_probs: List[float]
    expected_probs: List[float]
    null_tvd: List[float]
    threshold: float
    noise_floor: float
    null_mean: float
    null_std: float
    shots: int
    alpha: float
    n_runs: int

    def null_p_value(self, observed_tvd: float) -> float:
        """One-sided p-value of the observed deviation under the healthy null.

        Uses the standard ``(1 + #{null >= obs}) / (n + 1)`` estimator, which is
        a valid (conservative) p-value for exchangeable null samples.
        """

        null = np.asarray(self.null_tvd, dtype=float)
        exceed = int(np.sum(null >= observed_tvd))
        return float((1.0 + exceed) / (len(null) + 1.0))

    def anomaly_score(self, observed_tvd: float) -> float:
        """Confidence in [0, 1] that the deviation is not a healthy fluctuation."""

        return float(max(0.0, 1.0 - self.null_p_value(observed_tvd)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cp_id": self.cp_id,
            "name": self.name,
            "position": self.position,
            "stage": self.stage,
            "ideal_probs": list(self.ideal_probs),
            "expected_probs": list(self.expected_probs),
            "null_tvd": list(self.null_tvd),
            "threshold": self.threshold,
            "noise_floor": self.noise_floor,
            "null_mean": self.null_mean,
            "null_std": self.null_std,
            "shots": self.shots,
            "alpha": self.alpha,
            "n_runs": self.n_runs,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckpointBaseline":
        return cls(**data)


@dataclass
class BaselineProfile:
    """Calibrated reference behaviour for a complete program."""

    program_name: str
    program_fingerprint: str
    shots: int
    alpha: float
    n_runs: int
    noise_config: Dict[str, Any]
    created_at: str
    checkpoints: Dict[str, CheckpointBaseline] = field(default_factory=dict)

    def baseline(self, cp_id: str) -> CheckpointBaseline:
        return self.checkpoints[cp_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "program_name": self.program_name,
            "program_fingerprint": self.program_fingerprint,
            "shots": self.shots,
            "alpha": self.alpha,
            "n_runs": self.n_runs,
            "noise_config": self.noise_config,
            "created_at": self.created_at,
            "checkpoints": {k: v.to_dict() for k, v in self.checkpoints.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaselineProfile":
        return cls(
            program_name=data["program_name"],
            program_fingerprint=data["program_fingerprint"],
            shots=int(data["shots"]),
            alpha=float(data["alpha"]),
            n_runs=int(data["n_runs"]),
            noise_config=data.get("noise_config", {}),
            created_at=data.get("created_at", ""),
            checkpoints={
                k: CheckpointBaseline.from_dict(v)
                for k, v in data.get("checkpoints", {}).items()
            },
        )

    def save(self, path: str) -> str:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "BaselineProfile":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


def _select_threshold(null: np.ndarray, alpha: float) -> float:
    """Threshold that keeps the empirical false-positive rate near ``alpha``.

    The threshold is the maximum of
      * the empirical ``1 - alpha`` quantile of the null samples, and
      * ``mean + z * std`` using the Gaussian quantile,
    which protects against a null distribution that is heavy-tailed or that
    was sampled too few times to resolve the quantile directly.
    """

    if len(null) == 0:
        return 0.0
    quantile = float(np.quantile(null, 1.0 - alpha, method="higher"))
    z = float(stats.norm.ppf(1.0 - alpha))
    gaussian = float(null.mean() + z * null.std())
    return float(max(quantile, gaussian))


def calibrate(
    program: QuantumPulseProgram,
    backend: AerBackend,
    shots: int = 4096,
    n_runs: int = 200,
    alpha: float = 0.005,
    seed: Optional[int] = 12345,
    progress: bool = False,
) -> BaselineProfile:
    """Build the calibrated null model for every checkpoint of ``program``."""

    if n_runs < 5:
        raise ValueError("n_runs must be >= 5 to build a usable null distribution")

    profile = BaselineProfile(
        program_name=program.name,
        program_fingerprint=program.fingerprint(),
        shots=shots,
        alpha=alpha,
        n_runs=n_runs,
        noise_config=backend.noise_config.to_dict(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    for cp in program.checkpoints:
        prefix = program.prefix(cp.position)
        circuit = prefix.to_circuit(measure=True)
        ideal = backend.exact_probabilities(prefix.to_circuit(measure=False))

        runs: List[np.ndarray] = []
        for i in range(n_runs):
            run_seed = None if seed is None else int(seed) + i
            counts = backend.run_counts(circuit, shots=shots, seed=run_seed)
            from .execution import counts_to_probabilities

            runs.append(counts_to_probabilities(counts, program.num_qubits))
            if progress and (i + 1) % 25 == 0:
                print(f"    calibrating {cp.cp_id}: {i + 1}/{n_runs} runs")

        stacked = np.vstack(runs)
        expected = stacked.mean(axis=0)
        expected = expected / expected.sum()

        # Leave-one-out null: distance of each healthy run from the mean of
        # the *other* healthy runs. This avoids the optimistic bias of
        # comparing a run against a mean that contains itself.
        null_values: List[float] = []
        for i in range(len(runs)):
            others = np.delete(stacked, i, axis=0).mean(axis=0)
            others = others / others.sum()
            null_values.append(total_variation_distance(runs[i], others))

        null_arr = np.asarray(null_values)
        threshold = _select_threshold(null_arr, alpha)
        noise_floor = total_variation_distance(expected, ideal)

        profile.checkpoints[cp.cp_id] = CheckpointBaseline(
            cp_id=cp.cp_id,
            name=cp.name,
            position=cp.position,
            stage=program.gates[cp.position].stage if cp.position < len(program.gates) else "",
            ideal_probs=[float(p) for p in ideal],
            expected_probs=[float(p) for p in expected],
            null_tvd=[float(v) for v in null_values],
            threshold=threshold,
            noise_floor=float(noise_floor),
            null_mean=float(null_arr.mean()),
            null_std=float(null_arr.std()),
            shots=shots,
            alpha=alpha,
            n_runs=n_runs,
        )

    return profile
