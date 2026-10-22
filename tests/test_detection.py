"""Tests for execution, statistics and calibration."""

from __future__ import annotations

import numpy as np
import pytest

from quantumpulse.detection import (
    chi_square_gof,
    compare_distributions,
    hellinger_distance,
    jensen_shannon_distance,
    kl_divergence,
    total_variation_distance,
)
from quantumpulse.execution import bitstring_to_index, counts_to_probabilities
from quantumpulse.tampering import apply_tamper, build_tamper_catalog


class TestBitOrdering:
    """The count key and Qiskit's Statevector index must agree."""

    def test_bitstring_index_is_plain_integer(self):
        # "01" means classical bit 0 = 1, i.e. qubit 0 = 1 -> index 1
        assert bitstring_to_index("01", 2) == 1
        assert bitstring_to_index("10", 2) == 2

    def test_counts_match_statevector_convention(self, ideal_backend):
        from qiskit import QuantumCircuit

        qc = QuantumCircuit(2)
        qc.x(0)
        qc.measure_all()
        counts = ideal_backend.run_counts(qc, shots=50, seed=1)
        probs = counts_to_probabilities(counts, 2)
        assert np.argmax(probs) == 1

    def test_wrong_length_is_rejected(self):
        with pytest.raises(ValueError):
            bitstring_to_index("010", 2)


class TestProbabilityVectors:
    def test_probabilities_sum_to_one(self, ideal_backend, program):
        circuit = program.to_circuit(measure=True)
        counts = ideal_backend.run_counts(circuit, shots=1000, seed=2)
        probs = counts_to_probabilities(counts, 4)
        assert probs.shape == (16,)
        assert probs.sum() == pytest.approx(1.0)

    def test_unknown_outcome_count_matches_shots(self, ideal_backend, program):
        circuit = program.to_circuit(measure=True)
        counts = ideal_backend.run_counts(circuit, shots=500, seed=3)
        assert sum(counts.values()) == 500

    def test_exact_probabilities_are_normalised(self, ideal_backend, program):
        probs = ideal_backend.exact_probabilities(program.to_circuit(measure=False))
        assert probs.sum() == pytest.approx(1.0, abs=1e-9)
        assert np.all(probs >= 0)

    def test_exact_probabilities_are_non_uniform(self, ideal_backend, program):
        probs = ideal_backend.exact_probabilities(program.to_circuit(measure=False))
        entropy = -np.sum(probs[probs > 0] * np.log2(probs[probs > 0]))
        assert 2.0 < entropy < 4.0


class TestDistances:
    def test_tvd_identity_is_zero(self):
        p = np.array([0.25, 0.25, 0.25, 0.25])
        assert total_variation_distance(p, p) == pytest.approx(0.0)

    def test_tvd_of_disjoint_distributions_is_one(self):
        assert total_variation_distance([1, 0], [0, 1]) == pytest.approx(1.0)

    def test_tvd_is_symmetric_and_bounded(self):
        p = np.array([0.7, 0.2, 0.1])
        q = np.array([0.1, 0.1, 0.8])
        assert total_variation_distance(p, q) == pytest.approx(
            total_variation_distance(q, p)
        )
        assert 0 <= total_variation_distance(p, q) <= 1

    def test_shape_mismatch_is_rejected(self):
        with pytest.raises(ValueError):
            total_variation_distance([0.5, 0.5], [1.0, 0.0, 0.0])

    def test_js_distance_is_zero_for_identical(self):
        p = np.array([0.3, 0.7])
        assert jensen_shannon_distance(p, p) == pytest.approx(0.0, abs=1e-9)

    def test_kl_handles_zero_support(self):
        assert kl_divergence([1.0, 0.0], [0.5, 0.5]) == pytest.approx(np.log(2))

    def test_hellinger_is_bounded(self):
        assert hellinger_distance([1, 0], [0, 1]) == pytest.approx(1.0)


class TestChiSquare:
    def test_matching_distribution_is_not_significant(self):
        expected = np.full(16, 1 / 16)
        # perfectly uniform counts
        counts = {format(i, "04b"): 100 for i in range(16)}
        chi2, dof, p_value = chi_square_gof(counts, expected, 4)
        assert chi2 == pytest.approx(0.0, abs=1e-9)
        assert p_value == pytest.approx(1.0)

    def test_strong_mismatch_is_significant(self):
        # All four bins carry expected mass, but the observations have moved.
        expected = np.array([0.45, 0.45, 0.05, 0.05])
        counts = {"00": 10, "01": 10, "10": 490, "11": 490}
        chi2, dof, p_value = chi_square_gof(counts, expected, 2)
        assert chi2 > 100
        assert dof == 3
        assert p_value < 1e-6

    def test_degenerate_expected_support_is_handled(self):
        # If expected support collapses to one bin the test is undefined;
        # the helper must return a neutral result rather than divide by zero.
        expected = np.array([1.0, 0.0, 0.0, 0.0])
        counts = {"00": 100}
        chi2, dof, p_value = chi_square_gof(counts, expected, 2)
        assert chi2 == 0.0 and dof == 0 and p_value == 1.0


class TestComparisonBundle:
    def test_compare_returns_all_statistics(self):
        p = np.array([0.5, 0.3, 0.2])
        q = np.array([0.4, 0.4, 0.2])
        result = compare_distributions(p, q)
        assert result.tvd == pytest.approx(0.1)
        assert result.js_distance > 0
        assert result.hellinger > 0


class TestCalibration:
    def test_threshold_exceeds_null_mean(self, baseline):
        for ref in baseline.checkpoints.values():
            assert ref.threshold > ref.null_mean
            assert ref.threshold > 0

    def test_expected_probs_are_normalised(self, baseline):
        for ref in baseline.checkpoints.values():
            assert sum(ref.expected_probs) == pytest.approx(1.0, abs=1e-6)

    def test_noise_floor_is_small_for_ideal_simulation(self, baseline):
        for ref in baseline.checkpoints.values():
            assert ref.noise_floor < 0.05

    def test_null_p_value_is_bounded(self, baseline):
        ref = next(iter(baseline.checkpoints.values()))
        assert 0.0 < ref.null_p_value(0.0) <= 1.0
        assert ref.null_p_value(10.0) < ref.null_p_value(0.0)

    def test_baseline_round_trip(self, baseline, tmp_path):
        path = str(tmp_path / "baseline.json")
        baseline.save(path)
        from quantumpulse.calibration import BaselineProfile

        restored = BaselineProfile.load(path)
        assert restored.program_fingerprint == baseline.program_fingerprint
        assert set(restored.checkpoints) == set(baseline.checkpoints)

    def test_rejects_too_few_runs(self, program, ideal_backend):
        from quantumpulse.calibration import calibrate

        with pytest.raises(ValueError):
            calibrate(program, ideal_backend, n_runs=3)


class TestTampering:
    def test_catalog_covers_all_kinds(self, program):
        catalog = build_tamper_catalog(program)
        kinds = {t.kind for t in catalog}
        assert kinds == {"replacement", "removal", "alteration"}
        assert all(t.target_gid in program.gate_ids() for t in catalog)

    def test_every_scenario_compiles(self, program):
        for tamper in build_tamper_catalog(program):
            apply_tamper(program, tamper).to_circuit(measure=True)

    def test_removal_marks_gate_inactive(self, program):
        tamper = next(
            t for t in build_tamper_catalog(program) if t.kind == "removal"
        )
        tampered = apply_tamper(program, tamper)
        assert tampered.gate(tamper.target_gid).active is False

    def test_replacement_uses_new_parameters(self, program):
        # ry -> h must not inherit the rotation angle.
        tamper = next(
            t
            for t in build_tamper_catalog(program)
            if t.tamper_id == "g01:replace:h"
        )
        tampered = apply_tamper(program, tamper)
        assert tampered.gate("g01").params == ()

    def test_identity_modification_has_zero_effect(self, program, ideal_backend):
        from quantumpulse.tampering import exact_effect

        effects = exact_effect(program, program, ideal_backend)
        assert effects["max"] == pytest.approx(0.0, abs=1e-12)

    def test_effective_modification_has_non_zero_effect(self, program, ideal_backend):
        from quantumpulse.tampering import exact_effect, is_effectless

        tamper = next(
            t
            for t in build_tamper_catalog(program)
            if t.tamper_id == "g00:replace:x"
        )
        effects = exact_effect(program, apply_tamper(program, tamper), ideal_backend)
        assert effects["max"] > 0.1
        assert not is_effectless(effects)
