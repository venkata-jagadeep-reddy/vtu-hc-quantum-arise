"""Tests for the program model and the reference circuit."""

from __future__ import annotations

import pytest

from quantumpulse.circuits import program_table
from quantumpulse.program import GateSpec, QuantumPulseProgram


class TestReferenceProgram:
    def test_shape(self, program):
        assert program.num_qubits == 4
        assert len(program.gates) == 16
        assert len(program.checkpoints) == 5

    def test_checkpoints_are_ordered_and_in_range(self, program):
        positions = [cp.position for cp in program.checkpoints]
        assert positions == sorted(positions)
        assert all(0 <= p < len(program.gates) for p in positions)

    def test_final_checkpoint_is_the_last_gate(self, program):
        assert program.checkpoints[-1].position == len(program.gates) - 1

    def test_gate_ids_are_unique(self, program):
        ids = program.gate_ids()
        assert len(ids) == len(set(ids))

    def test_program_table_covers_every_gate(self, program):
        rows = program_table(program)
        assert len(rows) == len(program.gates)
        assert sum(1 for r in rows if r["checkpoint"]) == len(program.checkpoints)


class TestCompilation:
    def test_to_circuit_has_measurements(self, program):
        circuit = program.to_circuit(measure=True)
        assert circuit.num_clbits == 4

    def test_unmeasured_circuit_has_no_clbits(self, program):
        circuit = program.to_circuit(measure=False)
        assert circuit.num_clbits == 0

    def test_prefix_only_contains_earlier_gates(self, program):
        prefix = program.prefix(3)
        assert [g.gid for g in prefix.gates] == ["g00", "g01", "g02", "g03"]

    def test_unsupported_operation_is_rejected(self):
        bad = QuantumPulseProgram(
            name="bad",
            num_qubits=2,
            gates=[GateSpec(gid="g00", op="not_a_gate", qubits=(0,))],
        )
        with pytest.raises(ValueError, match="unsupported operation"):
            bad.to_circuit()

    def test_fingerprint_changes_with_content(self, program):
        tampered = program.with_gate("g00", op="x")
        assert tampered.fingerprint() != program.fingerprint()


class TestCausalRegions:
    def test_causal_gates_is_a_prefix(self, program):
        cp = program.checkpoint_by_id("CP-ROT")
        causal = program.causal_gates(cp)
        assert causal == program.gate_ids()[: cp.position + 1]

    def test_delta_band_is_narrower_than_causal_region(self, program):
        cp = program.checkpoint_by_id("CP-ROT")
        assert len(program.delta_band(cp)) < len(program.causal_gates(cp))


class TestPositionStability:
    """Removing a gate must not shift the position of later gates."""

    def test_removal_keeps_positions(self, program):
        tampered = program.without_gate("g05")
        assert len(tampered.gates) == len(program.gates)
        assert tampered.index_of("g06") == program.index_of("g06")
        assert tampered.gate("g05").active is False
        assert tampered.gate("g06").active is True

    def test_removal_drops_exactly_one_gate_from_the_circuit(self, program):
        tampered = program.without_gate("g05")
        assert len(tampered.active_gates) == len(program.gates) - 1

    def test_prefix_after_removal_has_one_fewer_gate(self, program):
        tampered = program.without_gate("g02")
        assert len(tampered.prefix(3).active_gates) == 3

    def test_round_trip_serialisation_preserves_active_flag(self, program):
        tampered = program.without_gate("g05")
        restored = QuantumPulseProgram.from_dict(tampered.to_dict())
        assert restored.gate("g05").active is False
        assert restored.to_dict() == tampered.to_dict()
