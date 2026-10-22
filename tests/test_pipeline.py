"""End-to-end tests for verification, localisation, storage and reporting."""

from __future__ import annotations

import pytest

from quantumpulse.evidence import build_report
from quantumpulse.execution import run_full_circuit, run_heartbeats
from quantumpulse.localization import localize
from quantumpulse.storage import MonitoringStore
from quantumpulse.tampering import apply_tamper, build_tamper_catalog
from quantumpulse.verification import verify_heartbeats


@pytest.fixture(scope="module")
def catalog(program):
    return {t.tamper_id: t for t in build_tamper_catalog(program)}


class TestHealthyVerification:
    def test_healthy_circuit_is_not_flagged(self, program, ideal_backend, baseline):
        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=77)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
        assert verdict.flagged is False
        assert verdict.overall_anomaly_score == 0.0
        assert verdict.first_bad_checkpoint is None

    def test_all_checkpoints_are_evaluated(self, program, ideal_backend, baseline):
        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=78)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
        assert len(verdict.verdicts) == len(program.checkpoints)

    def test_output_only_run_uses_the_full_circuit(self, program, ideal_backend):
        obs = run_full_circuit(program, ideal_backend, shots=512, seed=5)
        assert obs.cp_id == "CP-FINAL"
        assert obs.shots == 512
        assert obs.probs.sum() == pytest.approx(1.0)


class TestTamperedVerification:
    def test_strong_tamper_is_flagged(self, program, ideal_backend, baseline, catalog):
        tamper = catalog["g00:replace:x"]
        tampered = apply_tamper(program, tamper)
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=91)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        assert verdict.flagged is True
        assert verdict.overall_anomaly_score > 0.9
        assert verdict.first_bad_checkpoint == "CP-PREP"

    def test_suspects_are_a_causal_prefix(self, program, ideal_backend, baseline, catalog):
        tampered = apply_tamper(program, catalog["g00:replace:x"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=92)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        assert verdict.suspects == program.gate_ids()[
            : program.checkpoint_by_id(verdict.first_bad_checkpoint).position + 1
        ]

    def test_noop_tamper_is_not_flagged(self, program, ideal_backend, baseline):
        """A modification that provably cannot change the output must not raise."""
        from quantumpulse.tampering import exact_effect, is_effectless

        effectless = None
        for tamper in build_tamper_catalog(program):
            if is_effectless(exact_effect(program, apply_tamper(program, tamper), ideal_backend)):
                effectless = tamper
                break
        if effectless is None:
            pytest.skip("catalogue contains no effectless scenario")

        tampered = apply_tamper(program, effectless)
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=93)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        assert verdict.flagged is False


class TestLocalization:
    def test_localises_a_replacement(self, program, ideal_backend, baseline, catalog):
        tamper = catalog["g00:replace:x"]
        tampered = apply_tamper(program, tamper)
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=101)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]

        report = localize(
            program,
            flagged,
            baseline,
            ideal_backend,
            shots=2048,
            seed=101,
            true_gid=tamper.target_gid,
            exact_sweep=True,
        )
        assert report.top1_gid == tamper.target_gid
        assert report.top1_correct is True
        assert report.candidates[0].hypothesis_kind == "replacement"
        assert 0.0 <= report.confidence <= 1.0

    def test_ranking_is_sorted(self, program, ideal_backend, baseline, catalog):
        tampered = apply_tamper(program, catalog["g02:removal"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=102)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]
        report = localize(program, flagged, baseline, ideal_backend, shots=2048, seed=102)

        distances = [c.tvd_to_observed for c in report.candidates]
        assert distances == sorted(distances)
        assert [c.rank for c in report.candidates] == list(
            range(1, len(report.candidates) + 1)
        )

    def test_search_space_is_the_causal_prefix(self, program, ideal_backend, baseline, catalog):
        tampered = apply_tamper(program, catalog["g05:replace:cz"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=103)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]
        report = localize(program, flagged, baseline, ideal_backend, shots=2048, seed=103)

        # The search space is the causal prefix of the *earliest* deviating
        # checkpoint: later gates cannot influence an earlier measured state.
        cp = program.checkpoint_by_id(min(flagged, key=lambda o: o.position).cp_id)
        assert report.suspect_segment == program.causal_gates(cp)

    def test_tie_group_is_reported_not_hidden(self, program, ideal_backend, baseline, catalog):
        tampered = apply_tamper(program, catalog["g00:replace:x"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=104)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]
        report = localize(program, flagged, baseline, ideal_backend, shots=2048, seed=104)

        assert report.top1_gid in report.tied_gids
        if report.ambiguous:
            assert len(report.tied_gids) > 1


class TestReporting:
    def test_text_report_contains_every_section(self, program, ideal_backend, baseline):
        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=111)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
        report = build_report(
            program, verdict, heartbeat=heartbeat, backend_description=ideal_backend.describe()
        )
        text = report.to_text()
        assert "QuantumPulse Integrity Report" in text
        assert "Heartbeat verification" in text
        assert "INTEGRITY OK" in text

    def test_json_report_round_trips(self, program, ideal_backend, baseline):
        import json

        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=112)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
        report = build_report(program, verdict, heartbeat=heartbeat)
        payload = json.loads(report.to_json())
        assert payload["flagged"] is False
        assert len(payload["verdict"]["checkpoints"]) == len(program.checkpoints)


class TestStatisticalGuarantees:
    def test_shot_mismatch_is_rejected(self, program, ideal_backend, baseline):
        """Thresholds are only valid for the calibrated shot count."""

        other_shots = 1024 if baseline.shots != 1024 else 2048
        observation = run_full_circuit(program, ideal_backend, shots=other_shots, seed=1)
        with pytest.raises(ValueError, match="shot-count mismatch"):
            verify_heartbeats([observation], baseline, program=program)

    def test_family_wise_bound_is_reported(self, program, ideal_backend, baseline):
        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=2)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)

        expected = min(1.0, len(program.checkpoints) * baseline.alpha)
        assert verdict.family_wise_false_positive_bound == pytest.approx(expected)
        assert any("union bound" in note for note in verdict.notes)

    def test_top_candidate_is_always_in_the_tie_group(
        self, program, ideal_backend, baseline, catalog
    ):
        tampered = apply_tamper(program, catalog["g00:replace:x"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=3)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        flagged = [heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged]
        report = localize(program, flagged, baseline, ideal_backend, shots=2048, seed=3)

        assert report.top1_gid in report.tied_gids
        assert report.tie_tolerance > 0


class TestStorage:
    def test_record_and_query(self, program, ideal_backend, baseline, tmp_path):
        heartbeat = run_heartbeats(program, ideal_backend, shots=2048, seed=121)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=program)
        report = build_report(program, verdict, heartbeat=heartbeat)

        db = str(tmp_path / "history.db")
        with MonitoringStore(db) as store:
            run_id = store.record_run(report.to_dict(), label="nominal")
            assert run_id > 0
            assert store.summary()["total_runs"] == 1
            rows = store.checkpoint_rows(run_id)
            assert len(rows) == len(program.checkpoints)

    def test_alert_is_stored_for_flagged_run(
        self, program, ideal_backend, baseline, catalog, tmp_path
    ):
        tampered = apply_tamper(program, catalog["g00:replace:x"])
        heartbeat = run_heartbeats(tampered, ideal_backend, shots=2048, seed=122)
        verdict = verify_heartbeats(heartbeat.observations, baseline, program=tampered)
        report = build_report(program, verdict, heartbeat=heartbeat)

        with MonitoringStore(str(tmp_path / "history.db")) as store:
            store.record_run(report.to_dict(), label="tampered", tamper_id="test")
            assert store.summary()["flagged_runs"] == 1
            assert len(store.alerts()) == 1


class TestMonitoringSession:
    def test_check_once_produces_a_report(self, program, ideal_backend, baseline):
        from quantumpulse.config import MonitorConfig
        from quantumpulse.monitoring import MonitorSession

        session = MonitorSession(
            program=program,
            backend=ideal_backend,
            baseline=baseline,
            config=MonitorConfig(shots=2048, seed=1),
        )
        report = session.check_once()
        assert report.status == "INTEGRITY OK"
        assert session.iteration == 1


class TestEvaluationHarness:
    def test_small_matrix_produces_consistent_metrics(self, program, ideal_backend, catalog):
        from quantumpulse.config import ExperimentConfig
        from quantumpulse.evaluation import run_evaluation

        tampers = [catalog["g00:replace:x"], catalog["g02:removal"]]
        config = ExperimentConfig(
            shots=2048,
            calibration_runs=16,
            trials=2,
            nominal_trials=2,
            alpha=0.02,
            seed=7,
            noise_profiles=["ideal"],
        )
        result = run_evaluation(
            program,
            config,
            tampers=tampers,
            run_localization=False,
            progress=False,
        )
        metrics = result.metrics["ideal"]
        assert metrics.nominal_runs == 2
        assert metrics.tampered_runs == 4
        assert 0.0 <= metrics.detection_rate <= 1.0
        assert 0.0 <= metrics.false_positive_rate <= 1.0
        assert metrics.true_positives + metrics.false_negatives == 4
        assert result.scenarios

    def test_markdown_summary_is_generated(
        self, program, ideal_backend, catalog, tmp_path
    ):
        from quantumpulse.config import ExperimentConfig
        from quantumpulse.evaluation import format_markdown, run_evaluation

        config = ExperimentConfig(
            shots=2048,
            calibration_runs=12,
            trials=1,
            nominal_trials=1,
            alpha=0.02,
            seed=11,
            noise_profiles=["ideal"],
        )
        result = run_evaluation(
            program,
            config,
            tampers=[catalog["g00:replace:x"]],
            run_localization=False,
            progress=False,
        )
        path = result.save(str(tmp_path))
        text = format_markdown(path)
        assert "### Detection" in text
        assert "`ideal`" in text
        assert "### Localisation" in text
