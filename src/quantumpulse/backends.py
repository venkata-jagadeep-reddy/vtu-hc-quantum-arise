"""Execution backends for QuantumPulse.

The prototype targets Qiskit Aer.  The backend interface is deliberately
narrow (``run_counts`` / ``exact_probabilities``) so that a future IBM
Quantum backend can be dropped in without touching the monitoring,
detection or localisation layers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit.quantum_info import Statevector

from .config import NoiseConfig

try:  # pragma: no cover - import guard keeps the package importable without Aer
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import (
        NoiseModel,
        ReadoutError,
        depolarizing_error,
        thermal_relaxation_error,
    )

    AER_AVAILABLE = True
except ImportError:  # pragma: no cover
    AER_AVAILABLE = False


class ExecutionBackend(Protocol):
    """Minimal interface required by the QuantumPulse pipeline."""

    def run_counts(self, circuit: QuantumCircuit, shots: int, seed: Optional[int] = None) -> Dict[str, int]:
        ...

    def exact_probabilities(self, circuit: QuantumCircuit) -> np.ndarray:
        ...


def build_noise_model(config: NoiseConfig) -> Optional["NoiseModel"]:
    """Translate a :class:`NoiseConfig` into a Qiskit Aer ``NoiseModel``."""

    if not AER_AVAILABLE:
        raise RuntimeError("qiskit-aer is required to build noise models")
    if config.is_ideal:
        return None

    noise = NoiseModel()

    if config.t1 is not None and config.t2 is not None:
        one_q = thermal_relaxation_error(
            config.t1, config.t2, config.one_qubit_gate_time
        )
        two_q = thermal_relaxation_error(
            config.t1, config.t2, config.two_qubit_gate_time
        ).tensor(
            thermal_relaxation_error(config.t1, config.t2, config.two_qubit_gate_time)
        )
        if config.one_qubit_depolarizing > 0:
            one_q = one_q.compose(
                depolarizing_error(config.one_qubit_depolarizing, 1)
            )
        if config.two_qubit_depolarizing > 0:
            two_q = two_q.compose(
                depolarizing_error(config.two_qubit_depolarizing, 2)
            )
        noise.add_all_qubit_quantum_error(
            one_q, ["id", "x", "y", "z", "h", "s", "sdg", "t", "tdg", "sx",
                    "rx", "ry", "rz", "p", "u", "u1", "u2", "u3"]
        )
        noise.add_all_qubit_quantum_error(two_q, ["cx", "cz", "cy", "swap", "ecr", "cp"])
    else:
        if config.one_qubit_depolarizing > 0:
            noise.add_all_qubit_quantum_error(
                depolarizing_error(config.one_qubit_depolarizing, 1),
                ["id", "x", "y", "z", "h", "s", "sdg", "t", "tdg", "sx",
                 "rx", "ry", "rz", "p", "u", "u1", "u2", "u3"],
            )
        if config.two_qubit_depolarizing > 0:
            noise.add_all_qubit_quantum_error(
                depolarizing_error(config.two_qubit_depolarizing, 2),
                ["cx", "cz", "cy", "swap", "ecr", "cp"],
            )

    if config.readout_error > 0:
        p = config.readout_error
        readout = ReadoutError([[1.0 - p, p], [p, 1.0 - p]])
        noise.add_all_qubit_readout_error(readout)

    return noise


@dataclass
class AerBackend:
    """Qiskit Aer simulator backend with optional noise model."""

    noise_config: NoiseConfig = field(default_factory=NoiseConfig)
    seed: Optional[int] = None
    method: str = "automatic"
    transpile_enabled: bool = True
    _simulator: Any = field(default=None, init=False, repr=False)
    _cache: Dict[int, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not AER_AVAILABLE:
            raise RuntimeError(
                "qiskit-aer is not installed. Install the project requirements first."
            )
        noise_model = build_noise_model(self.noise_config)
        self._simulator = AerSimulator(
            noise_model=noise_model, method=self.method
        )

    @property
    def simulator(self) -> Any:
        return self._simulator

    def _compiled(self, circuit: QuantumCircuit, seed: Optional[int]) -> QuantumCircuit:
        """Transpile once per circuit object and reuse the result.

        Transpilation dominates the wall-clock time of a small circuit, so
        caching it is what makes the calibration sweep affordable.  The cache
        keeps a reference to the source circuit so that ``id`` cannot be
        recycled while the entry is alive.
        """

        key = id(circuit)
        entry = self._cache.get(key)
        if entry is not None and entry[0] is circuit:
            return entry[1]
        if not self.transpile_enabled:
            compiled = circuit
        else:
            compiled = transpile(
                circuit, self._simulator, seed_transpiler=seed, optimization_level=1
            )
        self._cache[key] = (circuit, compiled)
        return compiled

    def run_counts(
        self, circuit: QuantumCircuit, shots: int, seed: Optional[int] = None
    ) -> Dict[str, int]:
        """Execute ``circuit`` and return counts keyed by bit string."""

        run_seed = seed if seed is not None else self.seed
        compiled = self._compiled(circuit, run_seed)
        job = self._simulator.run(compiled, shots=shots, seed_simulator=run_seed)
        result = job.result()
        return {k.replace(" ", ""): int(v) for k, v in result.get_counts().items()}

    def exact_probabilities(self, circuit: QuantumCircuit) -> np.ndarray:
        """Exact probability vector of the (measurement-free) circuit.

        Used as the noise-free reference distribution; the *observed* noisy
        behaviour is always measured with :meth:`run_counts`.
        """

        stripped = circuit.remove_final_measurements(inplace=False)
        state = Statevector.from_instruction(stripped)
        probs = np.abs(np.asarray(state.data)) ** 2
        return np.real_if_close(probs, tol=1e-12).astype(float)

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": "qiskit_aer.AerSimulator",
            "method": self.method,
            "noise": self.noise_config.to_dict(),
        }


@dataclass
class TimingResult:
    """Wall-clock measurement of a single backend call."""

    counts: Dict[str, int]
    seconds: float


def timed_run(
    backend: AerBackend, circuit: QuantumCircuit, shots: int, seed: Optional[int] = None
) -> TimingResult:
    """Run a circuit and record the elapsed wall-clock time.

    Execution overhead is one of the reported evaluation metrics, so every
    execution path in QuantumPulse goes through this helper.
    """

    start = time.perf_counter()
    counts = backend.run_counts(circuit, shots=shots, seed=seed)
    elapsed = time.perf_counter() - start
    return TimingResult(counts=counts, seconds=elapsed)
