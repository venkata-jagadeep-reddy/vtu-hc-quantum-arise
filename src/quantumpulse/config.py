"""Configuration objects for QuantumPulse.

All tunable parameters live in this module so that every experiment can be
described by a single serialisable object. This is what makes the
"reported results are measured, not invented" requirement practical: an
experiment is reproducible from its configuration plus a seed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class NoiseConfig:
    """Physical-noise parameters mapped onto a Qiskit Aer noise model.

    ``is_ideal`` circuits use no noise model at all, which lets the evaluation
    harness compare ideal simulation against noisy simulation under identical
    seeds.
    """

    name: str = "ideal"
    one_qubit_depolarizing: float = 0.0
    two_qubit_depolarizing: float = 0.0
    readout_error: float = 0.0
    t1: Optional[float] = None  # qubit relaxation time, seconds
    t2: Optional[float] = None  # qubit dephasing time, seconds
    one_qubit_gate_time: float = 50e-9
    two_qubit_gate_time: float = 300e-9

    @property
    def is_ideal(self) -> bool:
        return (
            self.one_qubit_depolarizing == 0.0
            and self.two_qubit_depolarizing == 0.0
            and self.readout_error == 0.0
            and self.t1 is None
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NoiseConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


#: Ready-made noise settings used by the evaluation harness.
NOISE_PROFILES: Dict[str, NoiseConfig] = {
    "ideal": NoiseConfig(name="ideal"),
    "depolarizing_mild": NoiseConfig(
        name="depolarizing_mild",
        one_qubit_depolarizing=0.001,
        two_qubit_depolarizing=0.005,
        readout_error=0.005,
    ),
    "depolarizing_strong": NoiseConfig(
        name="depolarizing_strong",
        one_qubit_depolarizing=0.005,
        two_qubit_depolarizing=0.02,
        readout_error=0.02,
    ),
    "thermal": NoiseConfig(
        name="thermal",
        t1=120e-6,
        t2=90e-6,
        one_qubit_gate_time=50e-9,
        two_qubit_gate_time=300e-9,
        readout_error=0.005,
    ),
}


@dataclass
class MonitorConfig:
    """Runtime configuration for a monitoring session."""

    shots: int = 4096
    alpha: float = 0.005
    """Target false-positive rate used when calibrating the alert threshold."""

    calibration_runs: int = 200
    """Number of healthy reference runs used to build the null distribution."""

    interval_seconds: float = 5.0
    max_iterations: int = 0
    """0 means run forever; the CLI uses a finite value for reproducible runs."""

    seed: Optional[int] = 20240
    baseline_path: str = "data/baseline.json"
    database_path: str = "data/quantumpulse.db"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExperimentConfig:
    """Configuration for the offline evaluation matrix."""

    shots: int = 4096
    alpha: float = 0.005
    calibration_runs: int = 200
    trials: int = 50
    """Number of independent repetitions per (noise profile, tamper) cell."""

    nominal_trials: int = 100
    """Healthy runs per noise profile.  This is what the false-positive rate is
    estimated from, so it is kept deliberately larger than ``trials``."""

    seed: int = 20240
    noise_profiles: list[str] = field(default_factory=lambda: list(NOISE_PROFILES))
    output_dir: str = "data/evaluation"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def config_fingerprint(payload: Dict[str, Any]) -> str:
    """Stable short hash of a configuration dictionary (for run provenance)."""

    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]
