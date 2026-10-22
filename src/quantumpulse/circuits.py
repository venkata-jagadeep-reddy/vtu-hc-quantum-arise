"""Reference circuit definition and heartbeat checkpoint layout.

Design rationale
----------------
The reference circuit is small (4 qubits / 16 gates) but deliberately built to
be *monitorable*, not merely to produce an interesting distribution.

1. **Non-uniform output.**  The measurement distribution has ~3.7 bits of
   entropy over 16 outcomes, so total-variation distance reacts to small
   perturbations instead of being saturated.

2. **Semantic stages.**  Gates are grouped into prep / entangle / rotate /
   long-entangle / final / readout.  A checkpoint failure therefore maps onto
   a human-meaningful pipeline stage rather than an arbitrary instruction
   index.

3. **Readout layer (identifiability).**  This is the subtle one.  A circuit
   whose last gate on a qubit is a rotation has *unobservable phase
   equivalences*: because ``H = Ry(pi/2) . Z``, replacing a final ``ry(1, pi/2)``
   with ``h(1)`` leaves the measurement distribution bit-identical, and no
   amount of extra shots can tell them apart.  We measured exactly this
   (6 of 79 detected scenarios were indistinguishable) before adding a
   closing rotation on every qubit.  The ``readout`` stage applies one extra
   single-qubit rotation per qubit so that such phase differences become
   observable, which is the same trick tomography uses.

4. **Several checkpoints per stage boundary**, so that localisation has a
   narrow causal region to search and the cost of that narrowing can be
   measured as execution overhead.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

from .program import Checkpoint, GateSpec, QuantumPulseProgram

# (op, qubits, params, stage)
_REFERENCE_GATES: Sequence[Tuple[str, Tuple[int, ...], Tuple[float, ...], str]] = (
    # --- preparation -------------------------------------------------
    ("h", (0,), (), "prep"),
    ("ry", (1,), (math.pi / 4,), "prep"),
    ("h", (2,), (), "prep"),
    ("ry", (3,), (math.pi / 3,), "prep"),
    # --- nearest-neighbour entanglement ------------------------------
    ("cx", (0, 1), (), "entangle"),
    ("cx", (1, 2), (), "entangle"),
    ("cx", (2, 3), (), "entangle"),
    # --- mid-circuit rotations ---------------------------------------
    ("rz", (0,), (math.pi / 5,), "rotate"),
    ("ry", (2,), (-math.pi / 7,), "rotate"),
    # --- long-range entanglement -------------------------------------
    ("cz", (0, 3), (), "long-entangle"),
    ("cx", (1, 3), (), "long-entangle"),
    # --- final algorithmic rotations ---------------------------------
    ("ry", (1,), (math.pi / 2,), "final"),
    ("rx", (3,), (math.pi / 6,), "final"),
    # --- readout rotations (make phase differences observable) -------
    # Each readout rotation deliberately uses a *different axis* from the
    # preceding rotation on the same qubit.  Two rotations about the same axis
    # on the same qubit commute, so with nothing acting on that qubit in
    # between, "modify the early one" and "modify the late one" become exactly
    # equivalent and therefore impossible to localise.  Measured example:
    # with ``ry`` at both g11 and g13, all four g13 alterations were
    # indistinguishable from g11 alterations.
    ("rx", (1,), (math.pi / 3,), "readout"),
    ("ry", (0,), (math.pi / 5,), "readout"),
    ("rz", (2,), (math.pi / 7,), "readout"),
)

#: (checkpoint id, display name, inclusive program position, description)
_REFERENCE_CHECKPOINTS: Sequence[Tuple[str, str, int, str]] = (
    ("CP-PREP", "Preparation", 3, "State after the single-qubit preparation layer."),
    ("CP-ENT", "Entanglement", 6, "State after the nearest-neighbour CNOT layer."),
    ("CP-ROT", "Rotation", 8, "State after the mid-circuit rotation layer."),
    ("CP-OUT", "Algorithm output", 12, "State before the readout rotations."),
    ("CP-FINAL", "Final", 15, "Circuit output - the traditional output-only check."),
)


def reference_program() -> QuantumPulseProgram:
    """Build the reference QuantumPulse program with its heartbeat layout."""

    gates: List[GateSpec] = [
        GateSpec(gid=f"g{i:02d}", op=op, qubits=qubits, params=params, stage=stage)
        for i, (op, qubits, params, stage) in enumerate(_REFERENCE_GATES)
    ]
    checkpoints = [
        Checkpoint(cp_id=cid, name=name, position=pos, description=desc)
        for cid, name, pos, desc in _REFERENCE_CHECKPOINTS
    ]
    return QuantumPulseProgram(
        name="reference-4q",
        num_qubits=4,
        gates=gates,
        checkpoints=checkpoints,
        metadata={
            "purpose": "QuantumPulse reference circuit for integrity monitoring",
            "stages": ["prep", "entangle", "rotate", "long-entangle", "final", "readout"],
            "design_note": (
                "The readout stage exists so that phase-only modifications on a "
                "qubit's final rotation remain observable."
            ),
        },
    )


def program_table(program: QuantumPulseProgram) -> List[Dict[str, str]]:
    """Flat, printable description of the program (used by CLI and dashboard)."""

    rows: List[Dict[str, str]] = []
    checkpoint_at: Dict[int, str] = {cp.position: cp.cp_id for cp in program.checkpoints}
    for i, gate in enumerate(program.gates):
        rows.append(
            {
                "pos": str(i),
                "gid": gate.gid,
                "gate": gate.describe(),
                "stage": gate.stage,
                "checkpoint": checkpoint_at.get(i, ""),
            }
        )
    return rows
