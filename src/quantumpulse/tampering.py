"""Controlled gate-tampering scenarios.

Three tampering families are modelled, matching the kinds of unauthorised
modification a compromised compilation/transpilation step could introduce:

``replacement``
    the gate is swapped for a different operation (e.g. ``h`` -> ``x``,
    ``cx`` -> ``cz``, or the control/target of a CNOT is reversed);

``removal``
    the gate is deleted entirely (a classic "gate dropped by the optimiser"
    failure mode);

``alteration``
    the gate keeps its identity but its parameter is changed (e.g. a
    rotation angle is nudged), which is the hardest family to detect.

Every scenario records the *ground-truth* gate identifier.  That ground truth
is what makes a measured localisation accuracy possible - QuantumPulse never
claims to localise an attack it cannot identify in a controlled experiment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .program import GateSpec, QuantumPulseProgram

TAMPER_KINDS = ("replacement", "removal", "alteration")


@dataclass(frozen=True)
class TamperSpec:
    """A single controlled gate modification."""

    tamper_id: str
    target_gid: str
    kind: str
    description: str
    new_op: Optional[str] = None
    new_qubits: Optional[Tuple[int, ...]] = None
    new_params: Optional[Tuple[float, ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tamper_id": self.tamper_id,
            "target_gid": self.target_gid,
            "kind": self.kind,
            "description": self.description,
            "new_op": self.new_op,
            "new_qubits": list(self.new_qubits) if self.new_qubits else None,
            "new_params": list(self.new_params) if self.new_params else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TamperSpec":
        return cls(
            tamper_id=data["tamper_id"],
            target_gid=data["target_gid"],
            kind=data["kind"],
            description=data.get("description", ""),
            new_op=data.get("new_op"),
            new_qubits=tuple(data["new_qubits"]) if data.get("new_qubits") else None,
            new_params=tuple(data["new_params"]) if data.get("new_params") else None,
        )


def apply_tamper(program: QuantumPulseProgram, tamper: TamperSpec) -> QuantumPulseProgram:
    """Return a copy of ``program`` with ``tamper`` applied.

    Gate identifiers, positions and checkpoints are preserved so that the
    tampered program remains directly comparable with the reference.
    """

    gate = program.gate(tamper.target_gid)

    if tamper.kind == "removal":
        new_program = program.without_gate(tamper.target_gid)
    elif tamper.kind == "replacement":
        new_program = program.with_gate(
            tamper.target_gid,
            op=tamper.new_op or gate.op,
            qubits=tamper.new_qubits if tamper.new_qubits is not None else gate.qubits,
            # A replacement operation carries its own parameter list; it does
            # *not* inherit the parameters of the operation it replaces.
            params=tamper.new_params if tamper.new_params is not None else (),
        )
    elif tamper.kind == "alteration":
        new_program = program.with_gate(
            tamper.target_gid,
            params=tamper.new_params if tamper.new_params is not None else gate.params,
        )
    else:
        raise ValueError(f"unknown tamper kind: {tamper.kind}")

    new_program.name = f"{program.name}::tampered[{tamper.tamper_id}]"
    new_program.metadata = {
        **program.metadata,
        "tamper": tamper.to_dict(),
        "tampered": True,
    }
    return new_program


# ----------------------------------------------------------------------
# Hypothesis space
# ----------------------------------------------------------------------
#: Replacement candidates per operation.  The same table is used to *generate*
#: the evaluation catalogue and to *search* for the culprit, so the search
#: space is exactly the documented hypothesis space - no hidden oracle.
REPLACEMENT_TABLE: Dict[str, Sequence[Tuple[str, Optional[Tuple[float, ...]]]]] = {
    "h": (("x", None), ("z", None), ("s", None), ("ry", (math.pi / 2,)), ("rx", (math.pi / 2,))),
    "x": (("h", None), ("z", None), ("y", None), ("ry", (math.pi,))),
    "y": (("h", None), ("x", None), ("z", None)),
    "z": (("h", None), ("x", None), ("s", None)),
    "s": (("z", None), ("t", None), ("h", None)),
    "sdg": (("s", None), ("z", None)),
    "t": (("z", None), ("s", None)),
    "tdg": (("t", None), ("z", None)),
    "rx": (("ry", None), ("rz", None), ("h", None), ("x", None)),
    "ry": (("rx", None), ("rz", None), ("h", None), ("x", None)),
    "rz": (("rx", None), ("ry", None), ("h", None), ("x", None)),
    "p": (("rz", None), ("h", None)),
    "sx": (("h", None), ("x", None), ("rx", (math.pi / 2,))),
    "cx": (("cz", None), ("swap", None), ("cy", None), ("cx", None)),
    "cz": (("cx", None), ("swap", None), ("cy", None)),
    "cy": (("cx", None), ("cz", None)),
    "swap": (("cx", None), ("cz", None)),
}

#: Angle offsets used to build "alteration" scenarios.
ALTERATION_OFFSETS: Sequence[float] = (math.pi / 2, -math.pi / 2, math.pi / 4, math.pi)

#: Operations that require an angle parameter.
PARAMETRIC_OPS = {"rx", "ry", "rz", "p", "u", "u1", "u2", "u3"}


def hypotheses_for_gate(gate: GateSpec) -> List[TamperSpec]:
    """All single-gate mutations considered for a given gate.

    This is the hypothesis space of the localisation search.  It contains the
    "removal" mutation (used as an ablation probe) plus replacements and
    parameter alterations.
    """

    out: List[TamperSpec] = []

    out.append(
        TamperSpec(
            tamper_id=f"{gate.gid}:removal",
            target_gid=gate.gid,
            kind="removal",
            description=f"remove {gate.describe()}",
        )
    )

    for op, params in REPLACEMENT_TABLE.get(gate.op, ()):
        qubits = gate.qubits
        # A CNOT with reversed operands is a realistic and subtle swap.
        if gate.op == "cx" and op == "cx":
            qubits = tuple(reversed(gate.qubits))
        new_params = tuple(params) if params is not None else ()
        if not new_params and gate.params and op in PARAMETRIC_OPS:
            # Swapping one rotation axis for another keeps the angle: a
            # realistic "axis confusion" modification.
            new_params = tuple(gate.params)
        out.append(
            TamperSpec(
                tamper_id=f"{gate.gid}:replace:{op}{'@rev' if qubits != gate.qubits else ''}",
                target_gid=gate.gid,
                kind="replacement",
                description=f"replace {gate.describe()} with {op} on {qubits}",
                new_op=op,
                new_qubits=qubits,
                new_params=new_params,
            )
        )

    if gate.params:
        base = gate.params[0]
        for offset in ALTERATION_OFFSETS:
            new_params = (base + offset,) + tuple(gate.params[1:])
            out.append(
                TamperSpec(
                    tamper_id=f"{gate.gid}:alter:{offset:+.4f}",
                    target_gid=gate.gid,
                    kind="alteration",
                    description=f"alter {gate.describe()} -> angle {new_params[0]:.4f}",
                    new_params=new_params,
                )
            )

    return out


def build_tamper_catalog(
    program: QuantumPulseProgram,
    kinds: Optional[Iterable[str]] = None,
) -> List[TamperSpec]:
    """Enumerate the controlled single-gate tampering scenarios for a program."""

    wanted = set(kinds) if kinds else set(TAMPER_KINDS)
    catalog: List[TamperSpec] = []
    for gate in program.gates:
        for hypothesis in hypotheses_for_gate(gate):
            if hypothesis.kind in wanted:
                catalog.append(hypothesis)
    return catalog


def tamper_catalog_summary(catalog: Sequence[TamperSpec]) -> Dict[str, int]:
    summary: Dict[str, int] = {kind: 0 for kind in TAMPER_KINDS}
    for tamper in catalog:
        summary[tamper.kind] = summary.get(tamper.kind, 0) + 1
    return summary


# ----------------------------------------------------------------------
# Ground-truth effect size
# ----------------------------------------------------------------------
#: Distance below which a modification is considered to have no effect at all.
EFFECT_EPSILON = 1e-9


def exact_effect(
    reference: QuantumPulseProgram,
    tampered: QuantumPulseProgram,
    backend,
) -> Dict[str, float]:
    """Exact (sampling-free) effect of a modification on each checkpoint.

    This is the ground truth used to interpret detection results.  A
    modification whose exact effect is zero cannot be detected by *any*
    distribution-based monitor, so counting it as a "miss" would overstate
    the false-negative rate.

    The reference circuit contains a concrete example: ``cx(1, 2)`` acts on a
    target qubit that is already in a uniform superposition, so the CNOT
    leaves the register state unchanged.  Removing or replacing it is a
    genuine no-op for this circuit.
    """

    effects: Dict[str, float] = {}
    for cp in reference.checkpoints:
        position = min(cp.position, len(reference.gates) - 1, len(tampered.gates) - 1)
        ref_circuit = reference.prefix(position).to_circuit(measure=False)
        tam_circuit = tampered.prefix(position).to_circuit(measure=False)
        ref_probs = backend.exact_probabilities(ref_circuit)
        tam_probs = backend.exact_probabilities(tam_circuit)
        effects[cp.cp_id] = float(
            0.5 * sum(abs(a - b) for a, b in zip(ref_probs, tam_probs))
        )
    effects["max"] = max(effects.values()) if effects else 0.0
    effects["final"] = effects.get("CP-FINAL", 0.0)
    return effects


def is_effectless(effects: Dict[str, float], epsilon: float = EFFECT_EPSILON) -> bool:
    """True when the modification leaves every checkpoint distribution unchanged."""

    return bool(effects.get("max", 0.0) <= epsilon)
