"""Declarative circuit-program model used by QuantumPulse.

Why a program model instead of a raw ``QuantumCircuit``?
--------------------------------------------------------
Heartbeat checkpoints must remain meaningful *after* a gate has been tampered
with.  If checkpoints were anchored to Qiskit instruction indices, deleting a
gate would silently shift every later checkpoint and corrupt the localisation
logic.  Therefore QuantumPulse represents a circuit as an ordered list of
:class:`GateSpec` records with stable identifiers, and anchors checkpoints to
*program positions*.  A tampered program keeps the same positions and
identifiers, so "which gate was modified?" is answerable by construction.

The program is compiled to a Qiskit ``QuantumCircuit`` only at execution time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Tuple

from qiskit import QuantumCircuit

#: Operations acting on a single qubit.
ONE_QUBIT_OPS = {
    "id", "x", "y", "z", "s", "sdg", "t", "tdg",
    "rx", "ry", "rz", "p", "u", "u1", "u2", "u3", "sx",
}

#: Operations acting on two qubits.
TWO_QUBIT_OPS = {"cx", "cz", "cy", "swap", "ecr", "rzz", "cp"}


@dataclass(frozen=True)
class GateSpec:
    """A single, uniquely identified gate in a QuantumPulse program.

    ``active`` is set to ``False`` instead of deleting the record when a gate
    is removed by tampering.  Keeping the record preserves the *position* of
    every later gate, so checkpoints keep pointing at the same point of the
    logical circuit no matter how many gates were removed.
    """

    gid: str
    op: str
    qubits: Tuple[int, ...] = ()
    params: Tuple[float, ...] = ()
    stage: str = ""
    active: bool = True

    @property
    def arity(self) -> int:
        return len(self.qubits)

    def describe(self) -> str:
        if not self.active:
            return f"{self.op}(removed)"
        args = [str(q) for q in self.qubits]
        args += [f"{p:.4g}" for p in self.params]
        return f"{self.op}({', '.join(args)})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gid": self.gid,
            "op": self.op,
            "qubits": list(self.qubits),
            "params": list(self.params),
            "stage": self.stage,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateSpec":
        return cls(
            gid=data["gid"],
            op=data["op"],
            qubits=tuple(data.get("qubits", ())),
            params=tuple(data.get("params", ())),
            stage=data.get("stage", ""),
            active=bool(data.get("active", True)),
        )


@dataclass(frozen=True)
class Checkpoint:
    """A heartbeat checkpoint anchored to a program position."""

    cp_id: str
    name: str
    position: int
    """Inclusive index into ``program.gates``; the checkpoint observes the state
    after the gate at this position has been applied."""

    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cp_id": self.cp_id,
            "name": self.name,
            "position": self.position,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Checkpoint":
        return cls(
            cp_id=data["cp_id"],
            name=data["name"],
            position=int(data["position"]),
            description=data.get("description", ""),
        )


@dataclass
class QuantumPulseProgram:
    """An ordered list of gates plus the heartbeat checkpoints that observe it."""

    name: str
    num_qubits: int
    gates: List[GateSpec]
    checkpoints: List[Checkpoint] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.checkpoints = sorted(self.checkpoints, key=lambda c: c.position)

    # ------------------------------------------------------------------
    # Structural helpers
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.gates)

    @property
    def active_gates(self) -> List[GateSpec]:
        """Gates that are actually executed (removed gates are skipped)."""

        return [g for g in self.gates if g.active]

    @property
    def checkpoint_ids(self) -> List[str]:
        return [c.cp_id for c in self.checkpoints]

    def index_of(self, gid: str) -> int:
        for i, gate in enumerate(self.gates):
            if gate.gid == gid:
                return i
        raise KeyError(f"unknown gate id: {gid}")

    def gate(self, gid: str) -> GateSpec:
        return self.gates[self.index_of(gid)]

    def position_of_gate(self, gid: str) -> int:
        return self.index_of(gid)

    def checkpoint_by_id(self, cp_id: str) -> Checkpoint:
        for cp in self.checkpoints:
            if cp.cp_id == cp_id:
                return cp
        raise KeyError(f"unknown checkpoint id: {cp_id}")

    def stages(self) -> List[str]:
        """Ordered list of distinct stage labels."""
        seen: List[str] = []
        for g in self.gates:
            if g.stage and g.stage not in seen:
                seen.append(g.stage)
        return seen

    def gate_ids(self) -> List[str]:
        return [g.gid for g in self.gates]

    def causal_gates(self, cp: Checkpoint) -> List[str]:
        """All gates that could have influenced the state observed at ``cp``.

        A modification can only affect a checkpoint if it is applied *before*
        that checkpoint, so the causal region of a checkpoint is the prefix of
        the program up to and including its position.  This is the sound
        search space for localisation.

        Note that this prefix is deliberately *wider* than the band between
        two consecutive checkpoints: a modification can be invisible at an
        intermediate checkpoint and only become observable further down the
        pipeline.  Example from the reference circuit: replacing ``cx(1, 2)``
        with ``cz(1, 2)`` leaves the CP-ENT measurement distribution
        unchanged (the target qubit is already in a uniform superposition)
        but is clearly visible at CP-ROT.
        """
        return [g.gid for g in self.gates[: cp.position + 1]]

    def delta_band(self, cp: Checkpoint) -> List[str]:
        """Gate ids between the previous checkpoint and ``cp`` (inclusive).

        This narrow band is informative - it says *where the deviation first
        became observable* - but it is not a sound search space on its own.
        """
        prev = None
        for c in self.checkpoints:
            if c.position < cp.position:
                prev = c
            else:
                break
        start = 0 if prev is None else prev.position + 1
        return [g.gid for g in self.gates[start : cp.position + 1]]

    # ------------------------------------------------------------------
    # Compilation
    # ------------------------------------------------------------------
    def prefix(self, position: int) -> "QuantumPulseProgram":
        """Return a program containing only the gates up to ``position``."""
        return QuantumPulseProgram(
            name=f"{self.name}::prefix@{position}",
            num_qubits=self.num_qubits,
            gates=list(self.gates[: position + 1]),
            checkpoints=[],
            metadata={**self.metadata, "prefix_of": self.name},
        )

    def to_circuit(self, measure: bool = True) -> QuantumCircuit:
        """Compile the program into a Qiskit circuit.

        When ``measure`` is true a ``measure_all`` instruction is appended so
        that Aer returns counts in the computational basis.
        """
        qc = QuantumCircuit(self.num_qubits)
        for gate in self.gates:
            if not gate.active:
                continue
            self._apply(qc, gate)
        if measure:
            qc.measure_all()
        return qc

    @staticmethod
    def _apply(qc: QuantumCircuit, gate: GateSpec) -> None:
        method = getattr(qc, gate.op, None)
        if method is None:
            raise ValueError(f"unsupported operation '{gate.op}'")
        method(*gate.params, *gate.qubits)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "num_qubits": self.num_qubits,
            "gates": [g.to_dict() for g in self.gates],
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QuantumPulseProgram":
        return cls(
            name=data["name"],
            num_qubits=int(data["num_qubits"]),
            gates=[GateSpec.from_dict(g) for g in data["gates"]],
            checkpoints=[Checkpoint.from_dict(c) for c in data.get("checkpoints", [])],
            metadata=data.get("metadata", {}),
        )

    def fingerprint(self) -> str:
        """Content hash of the program; distinguishes reference vs tampered."""
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def with_gate(self, gid: str, **changes: Any) -> "QuantumPulseProgram":
        """Return a copy of the program with one gate replaced.

        Positions, gate identifiers and checkpoints are preserved, which is
        exactly what the localisation and evaluation code needs.
        """
        gates = list(self.gates)
        idx = self.index_of(gid)
        gates[idx] = replace(gates[idx], **changes)
        return QuantumPulseProgram(
            name=self.name,
            num_qubits=self.num_qubits,
            gates=gates,
            checkpoints=list(self.checkpoints),
            metadata=dict(self.metadata),
        )

    def without_gate(self, gid: str) -> "QuantumPulseProgram":
        """Return a copy of the program with one gate deactivated.

        The gate record is kept (with ``active=False``) so that the position
        of every later gate - and therefore every checkpoint - is unchanged.
        """
        return self.with_gate(gid, active=False)
