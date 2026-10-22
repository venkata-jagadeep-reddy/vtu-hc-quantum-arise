"""Circuit execution and heartbeat collection.

Heartbeat semantics
-------------------
A checkpoint observes the state of the register *after* a given program
position.  QuantumPulse implements this by executing the *prefix* of the
program up to that position and measuring the whole register.

For a program without mid-circuit measurement or reset this is exact: the
state reached after a prefix is identical to the state the full circuit
reaches at that point, because the prefix is a prefix of a unitary
evolution.  The cost is one extra simulator run per checkpoint, which is
measured and reported as execution overhead.

Bit ordering convention
-----------------------
Qiskit returns count keys as ``c_{n-1} ... c_1 c_0`` and Qiskit's
``Statevector`` is little-endian as well, i.e. qubit 0 is the least
significant bit of the basis index.  The two conventions therefore already
agree: the integer value of the count key *is* the basis index used by
``Statevector``.  QuantumPulse relies on that agreement and normalises
everything to an integer index ``x`` where qubit 0 is the LSB, so that a
probability vector of length ``2**n`` can be compared element-wise regardless
of the backend's string convention.

(Verified empirically: a circuit with ``x(0)`` on 2 qubits yields the count key
``"01"`` and a ``Statevector`` whose non-zero entry is at index 1.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .backends import AerBackend, timed_run
from .program import QuantumPulseProgram


def bitstring_to_index(bits: str, num_qubits: int) -> int:
    """Convert a Qiskit count key into QuantumPulse's integer index."""

    bits = bits.strip()
    if len(bits) != num_qubits:
        raise ValueError(f"expected {num_qubits} bits, got {len(bits)!r}")
    # The count key is c_{n-1}...c_0, so the plain integer value already uses
    # qubit 0 as the least significant bit - identical to Qiskit's Statevector
    # basis indexing.  Do *not* reverse the string.
    return int(bits, 2)


def counts_to_probabilities(
    counts: Dict[str, int], num_qubits: int, shots: Optional[int] = None
) -> np.ndarray:
    """Convert counts into a normalised probability vector."""

    total = shots if shots is not None else sum(counts.values())
    if total <= 0:
        raise ValueError("no shots recorded")
    probs = np.zeros(2 ** num_qubits, dtype=float)
    for bits, count in counts.items():
        probs[bitstring_to_index(bits, num_qubits)] = count / total
    return probs


@dataclass
class CheckpointObservation:
    """The measurement outcome recorded at one heartbeat checkpoint."""

    cp_id: str
    name: str
    position: int
    counts: Dict[str, int]
    probs: np.ndarray
    shots: int
    seconds: float = 0.0
    gate_ids: List[str] = field(default_factory=list)

    def top_outcomes(self, k: int = 3) -> List[Dict[str, Any]]:
        order = np.argsort(self.probs)[::-1][:k]
        return [
            {
                "outcome": format(int(i), f"0{len(self.probs).bit_length() - 1}b"),
                "probability": float(self.probs[i]),
                "count": int(round(self.probs[i] * self.shots)),
            }
            for i in order
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cp_id": self.cp_id,
            "name": self.name,
            "position": self.position,
            "shots": self.shots,
            "seconds": self.seconds,
            "probs": [float(p) for p in self.probs],
            "counts": {k: int(v) for k, v in self.counts.items()},
            "gate_ids": list(self.gate_ids),
        }


@dataclass
class HeartbeatResult:
    """All checkpoint observations collected from one circuit execution."""

    program_name: str
    observations: List[CheckpointObservation]
    seconds: float = 0.0
    seed: Optional[int] = None
    shots: int = 0

    def by_id(self, cp_id: str) -> CheckpointObservation:
        for obs in self.observations:
            if obs.cp_id == cp_id:
                return obs
        raise KeyError(f"no observation for checkpoint {cp_id}")

    @property
    def total_seconds(self) -> float:
        return float(self.seconds)


def run_heartbeats(
    program: QuantumPulseProgram,
    backend: AerBackend,
    shots: int = 4096,
    seed: Optional[int] = None,
) -> HeartbeatResult:
    """Execute the program and collect a measurement at every checkpoint.

    The final checkpoint (position of the last gate) is the full circuit, so
    the output-only check is obtained without an extra run.

    Each checkpoint receives its own derived seed so that the checkpoints are
    statistically independent of one another, while two runs that share the
    same base seed still use the same random stream (common random numbers),
    which reduces the variance of A/B comparisons such as healthy vs tampered.
    """

    observations: List[CheckpointObservation] = []
    total_seconds = 0.0

    for index, cp in enumerate(program.checkpoints):
        prefix = program.prefix(cp.position)
        circuit = prefix.to_circuit(measure=True)
        run_seed = None if seed is None else int(seed) * 1000 + index
        timed = timed_run(backend, circuit, shots=shots, seed=run_seed)
        total_seconds += timed.seconds
        observations.append(
            CheckpointObservation(
                cp_id=cp.cp_id,
                name=cp.name,
                position=cp.position,
                counts=timed.counts,
                probs=counts_to_probabilities(timed.counts, program.num_qubits),
                shots=shots,
                seconds=timed.seconds,
                gate_ids=[g.gid for g in prefix.gates],
            )
        )

    return HeartbeatResult(
        program_name=program.name,
        observations=observations,
        seconds=total_seconds,
        seed=seed,
        shots=shots,
    )


def run_full_circuit(
    program: QuantumPulseProgram,
    backend: AerBackend,
    shots: int = 4096,
    seed: Optional[int] = None,
) -> CheckpointObservation:
    """Execute only the complete circuit (the output-only baseline)."""

    circuit = program.to_circuit(measure=True)
    timed = timed_run(backend, circuit, shots=shots, seed=seed)
    return CheckpointObservation(
        cp_id="CP-FINAL",
        name="Final",
        position=len(program.gates) - 1,
        counts=timed.counts,
        probs=counts_to_probabilities(timed.counts, program.num_qubits),
        shots=shots,
        seconds=timed.seconds,
        gate_ids=program.gate_ids(),
    )
