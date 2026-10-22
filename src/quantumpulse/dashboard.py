"""Streamlit dashboard for QuantumPulse.

Run with::

    streamlit run src/quantumpulse/dashboard.py

The dashboard is a thin presentation layer: it calls exactly the same pipeline
functions as the CLI, so what is shown on screen is what the evaluation
measures.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``quantumpulse`` importable when the file is run directly by Streamlit.
_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from quantumpulse.backends import AerBackend
from quantumpulse.calibration import BaselineProfile, calibrate
from quantumpulse.circuits import program_table, reference_program
from quantumpulse.config import NOISE_PROFILES
from quantumpulse.execution import run_heartbeats
from quantumpulse.localization import localize
from quantumpulse.storage import MonitoringStore
from quantumpulse.tampering import (
    apply_tamper,
    build_tamper_catalog,
    exact_effect,
    tamper_catalog_summary,
)
from quantumpulse.verification import VerificationConfig, verify_heartbeats

OK_COLOUR = "#2e7d32"
ALERT_COLOUR = "#c62828"
ACCENT = "#1f4e79"
MUTED = "#8a8a8a"

st.set_page_config(
    page_title="QuantumPulse | Circuit Integrity Monitor",
    page_icon="~",
    layout="wide",
)


# ----------------------------------------------------------------------
# cached helpers
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_program():
    return reference_program()


@st.cache_resource(show_spinner=False)
def get_backend(noise_name: str, seed: int):
    return AerBackend(noise_config=NOISE_PROFILES[noise_name], seed=seed)


def load_baseline(path: str) -> BaselineProfile | None:
    try:
        return BaselineProfile.load(path)
    except FileNotFoundError:
        return None


def checkpoint_bar(verdict) -> go.Figure:
    """D_TV per checkpoint against the calibrated threshold."""

    labels = [v.cp_id for v in verdict.verdicts]
    observed = [v.observed_tvd for v in verdict.verdicts]
    thresholds = [v.threshold for v in verdict.verdicts]
    colours = [ALERT_COLOUR if v.flagged else ACCENT for v in verdict.verdicts]

    figure = go.Figure()
    figure.add_bar(
        x=labels, y=observed, name="observed D_TV", marker_color=colours, width=0.5
    )
    figure.add_scatter(
        x=labels,
        y=thresholds,
        name="calibrated threshold",
        mode="lines+markers",
        line=dict(color="#e65100", width=2, dash="dash"),
    )
    figure.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title="D_TV",
        xaxis_title="heartbeat checkpoint",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="white",
        font=dict(size=12),
    )
    return figure


def distribution_figure(observed, expected, cp_id: str) -> go.Figure:
    size = len(observed)
    labels = [format(i, f"0{size.bit_length() - 1}b") for i in range(size)]
    figure = go.Figure()
    figure.add_bar(x=labels, y=list(expected), name="expected (calibrated)")
    figure.add_bar(x=labels, y=list(observed), name="observed")
    figure.update_layout(
        barmode="group",
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title=f"outcome at {cp_id}",
        yaxis_title="probability",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="white",
        font=dict(size=12),
    )
    return figure


def ranking_figure(report) -> go.Figure:
    ordered = list(reversed(report.candidates[:8]))
    colours = [
        ALERT_COLOUR if c.is_true_location else ACCENT for c in ordered
    ]
    figure = go.Figure(
        go.Bar(
            x=[c.tvd_to_observed for c in ordered],
            y=[f"{c.gid}  {c.gate_description}" for c in ordered],
            orientation="h",
            marker_color=colours,
        )
    )
    figure.update_layout(
        height=340,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title="mean D_TV to observation (lower = better match)",
        plot_bgcolor="white",
        font=dict(size=12),
    )
    return figure


# ----------------------------------------------------------------------
# sidebar
# ----------------------------------------------------------------------
program = get_program()
catalog = {t.tamper_id: t for t in build_tamper_catalog(program)}

with st.sidebar:
    st.markdown("### Monitoring configuration")
    noise_name = st.selectbox(
        "Noise profile",
        options=sorted(NOISE_PROFILES),
        index=sorted(NOISE_PROFILES).index("ideal"),
    )
    shots = st.select_slider("Shots per checkpoint", options=[1024, 2048, 4096, 8192], value=4096)
    seed = st.number_input("Random seed", min_value=0, max_value=10_000_000, value=20240, step=1)
    baseline_path = st.text_input("Baseline file", value="data/baseline.json")
    db_path = st.text_input("History database", value="data/quantumpulse.db")

    st.divider()
    st.markdown("### Circuit under surveillance")
    scenario = st.selectbox(
        "Controlled tampering scenario",
        options=["none (healthy reference)"] + sorted(catalog),
        index=0,
        help="Choose a single-gate modification to demonstrate detection.",
    )

    st.divider()
    st.markdown("### Baseline")
    baseline = load_baseline(baseline_path)
    if baseline is None:
        st.warning("No baseline found. Calibrate first.")
        if st.button("Calibrate baseline", type="primary", width="stretch"):
            with st.spinner("Calibrating healthy null distribution ..."):
                profile = calibrate(
                    program,
                    get_backend(noise_name, int(seed)),
                    shots=int(shots),
                    n_runs=120,
                    alpha=0.005,
                    seed=int(seed),
                )
                profile.save(baseline_path)
            st.success(f"Baseline written to {baseline_path}")
            st.rerun()
    else:
        st.caption(
            f"program `{baseline.program_name}` / {baseline.shots} shots / "
            f"alpha {baseline.alpha} / {baseline.n_runs} runs"
        )
        if baseline.shots != int(shots):
            st.error(
                f"Baseline was calibrated with {baseline.shots} shots but the "
                f"monitor is set to {shots}. Re-calibrate or change the shot count."
            )
        if st.button("Run heartbeat check", type="primary", width="stretch"):
            st.session_state["run"] = True

# ----------------------------------------------------------------------
# header
# ----------------------------------------------------------------------
st.title("QuantumPulse")
st.caption(
    "Heartbeat-based quantum circuit integrity monitoring and gate-tampering localisation"
)

tabs = st.tabs(["Monitor", "Reference circuit", "History", "Method"])

# ----------------------------------------------------------------------
# monitor tab
# ----------------------------------------------------------------------
with tabs[0]:
    if baseline is None:
        st.info("Calibrate a baseline in the sidebar to start monitoring.")
    elif baseline.shots != int(shots):
        st.error("Shot count does not match the baseline calibration.")
    elif not st.session_state.get("run"):
        st.info("Press **Run heartbeat check** in the sidebar.")
    else:
        backend = get_backend(noise_name, int(seed))
        tamper = None if scenario.startswith("none") else catalog[scenario]
        target = apply_tamper(program, tamper) if tamper else program

        with st.spinner("Executing heartbeat checkpoints ..."):
            heartbeat = run_heartbeats(target, backend, shots=int(shots), seed=int(seed))
            verdict = verify_heartbeats(
                heartbeat.observations,
                baseline,
                VerificationConfig(),
                program=target,
            )
            report = None
            localization = None
            if verdict.flagged:
                flagged = [
                    heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged
                ]
                localization = localize(
                    program,
                    flagged,
                    baseline,
                    backend,
                    shots=int(shots),
                    seed=int(seed),
                    true_gid=tamper.target_gid if tamper else None,
                    exact_sweep=True,
                )

        status_colour = ALERT_COLOUR if verdict.flagged else OK_COLOUR
        status_text = "DEVIATION DETECTED" if verdict.flagged else "INTEGRITY OK"
        col1, col2, col3, col4 = st.columns(4)
        col1.markdown(
            f"<div style='padding:10px;border-radius:6px;background:{status_colour};"
            f"color:white;text-align:center;font-weight:600'>{status_text}</div>",
            unsafe_allow_html=True,
        )
        col2.metric("Anomaly score", f"{verdict.overall_anomaly_score:.3f}")
        col3.metric(
            "First deviating checkpoint", verdict.first_bad_checkpoint or "none"
        )
        col4.metric("Heartbeat overhead", f"{heartbeat.total_seconds:.2f} s")

        if tamper:
            effects = exact_effect(program, target, backend)
            st.caption(
                f"Scenario `{tamper.tamper_id}` - {tamper.description}. "
                f"Exact effect on the output distribution: {effects['max']:.4f}"
                + ("  (no-op for this circuit)" if effects["max"] <= 1e-9 else "")
            )

        st.markdown("#### Heartbeat verification")
        st.plotly_chart(checkpoint_bar(verdict), width="stretch")

        table = pd.DataFrame(
            [
                {
                    "checkpoint": v.cp_id,
                    "stage": v.stage,
                    "D_TV": round(v.observed_tvd, 5),
                    "threshold": round(v.threshold, 5),
                    "p-value (null)": round(v.null_p_value, 4),
                    "anomaly score": round(v.anomaly_score, 3),
                    "verdict": "ALERT" if v.flagged else "ok",
                }
                for v in verdict.verdicts
            ]
        )
        st.dataframe(table, hide_index=True, width="stretch")

        st.markdown("#### Distribution comparison")
        selected = st.selectbox(
            "Checkpoint", options=[v.cp_id for v in verdict.verdicts], index=len(verdict.verdicts) - 1
        )
        observation = heartbeat.by_id(selected)
        reference = baseline.checkpoints[selected]
        st.plotly_chart(
            distribution_figure(observation.probs, reference.expected_probs, selected),
            width="stretch",
        )
        st.caption(
            f"noise floor (reference vs ideal state) = {reference.noise_floor:.4f}; "
            f"null mean = {reference.null_mean:.4f}, null std = {reference.null_std:.4f}"
        )

        if localization is not None:
            st.markdown("#### Suspected locations")
            bound = localization.search_bound_checkpoint or localization.target_checkpoint
            st.markdown(
                f"Search space: **{len(localization.suspect_segment)} gates** preceding "
                f"`{bound}` (fused evidence from "
                f"{', '.join(localization.checkpoints_used)})."
            )
            if localization.ambiguous:
                st.warning(
                    "Ambiguous result: several modifications reproduce the observation "
                    f"within measurement noise ({', '.join(localization.tied_gids)}). "
                    "This is an identifiability limit of the circuit, not a search failure."
                )
            st.plotly_chart(ranking_figure(localization), width="stretch")

            candidates = pd.DataFrame(
                [
                    {
                        "rank": c.rank,
                        "gate": c.gid,
                        "operation": c.gate_description,
                        "stage": c.stage,
                        "best hypothesis": c.hypothesis_description,
                        "mean D_TV": round(c.tvd_to_observed, 5),
                        "confidence": round(c.score, 3),
                        "true location": "yes" if c.is_true_location else "",
                    }
                    for c in localization.candidates
                ]
            )
            st.dataframe(candidates, hide_index=True, width="stretch")

        st.markdown("#### Evidence report")
        from quantumpulse.evidence import build_report

        built = build_report(
            target,
            verdict,
            heartbeat=heartbeat,
            localization=localization,
            backend_description=backend.describe(),
        )
        st.code(built.to_text(), language="text")
        with st.expander("Download JSON"):
            st.download_button(
                "integrity_report.json",
                data=built.to_json(),
                file_name="integrity_report.json",
                mime="application/json",
            )

        try:
            store = MonitoringStore(db_path)
            store.record_run(
                built.to_dict(),
                label="tampered" if tamper else "nominal",
                tamper_id=tamper.tamper_id if tamper else None,
                tamper_target=tamper.target_gid if tamper else None,
            )
            store.close()
        except Exception as exc:  # pragma: no cover - non-critical
            st.caption(f"history not recorded: {exc}")

# ----------------------------------------------------------------------
# reference tab
# ----------------------------------------------------------------------
with tabs[1]:
    st.markdown("#### Reference program")
    st.code(program.to_circuit(measure=False).draw(output="text").single_string(), language="text")

    st.markdown("#### Gate schedule and heartbeat checkpoints")
    st.dataframe(pd.DataFrame(program_table(program)), hide_index=True, width="stretch")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Checkpoints")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "checkpoint": cp.cp_id,
                        "name": cp.name,
                        "position": cp.position,
                        "description": cp.description,
                    }
                    for cp in program.checkpoints
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    with col2:
        st.markdown("#### Tampering catalogue")
        summary = tamper_catalog_summary(list(catalog.values()))
        st.dataframe(
            pd.DataFrame(
                [{"kind": k, "scenarios": v} for k, v in summary.items()]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            f"{len(catalog)} single-gate scenarios generated from the documented "
            "hypothesis space (replacement / removal / alteration)."
        )

# ----------------------------------------------------------------------
# history tab
# ----------------------------------------------------------------------
with tabs[2]:
    if not Path(db_path).exists():
        st.info("No monitoring history yet.")
    else:
        store = MonitoringStore(db_path)
        runs = pd.read_sql_query("SELECT * FROM runs ORDER BY id DESC", store.connection)
        checkpoints = pd.read_sql_query(
            "SELECT * FROM checkpoint_results", store.connection
        )
        store.close()

        col1, col2, col3 = st.columns(3)
        col1.metric("Monitoring rounds", len(runs))
        col2.metric("Flagged rounds", int(runs["flagged"].sum()) if len(runs) else 0)
        col3.metric(
            "Tampered rounds",
            int((runs["label"] == "tampered").sum()) if len(runs) else 0,
        )

        st.markdown("#### Runs")
        st.dataframe(runs, hide_index=True, width="stretch")

        if len(checkpoints):
            st.markdown("#### D_TV history per checkpoint")
            joined = checkpoints.merge(
                runs[["id", "timestamp"]], left_on="run_id", right_on="id", suffixes=("", "_run")
            )
            figure = go.Figure()
            for cp_id, group in joined.groupby("cp_id"):
                figure.add_scatter(
                    x=group["timestamp"],
                    y=group["observed_tvd"],
                    mode="markers+lines",
                    name=cp_id,
                )
            figure.update_layout(
                height=360,
                margin=dict(l=10, r=10, t=30, b=10),
                yaxis_title="D_TV",
                plot_bgcolor="white",
            )
            st.plotly_chart(figure, width="stretch")

# ----------------------------------------------------------------------
# method tab
# ----------------------------------------------------------------------
with tabs[3]:
    st.markdown(
        """
#### What the numbers mean

**Total variation distance.** For two distributions over measurement outcomes,

```
D_TV(P, Q) = 0.5 * sum_x |P(x) - Q(x)|
```

It is the largest amount of probability mass that has to be reassigned to turn
`P` into `Q`, so `D_TV = 0.25` means at least 25% of the mass moved. It is
bounded to `[0, 1]` and needs no binning choices.

**Calibrated threshold.** A healthy circuit also produces a non-zero `D_TV`
because of finite shots and hardware noise. QuantumPulse therefore executes
the healthy reference circuit many times under the **same** noise model and
shot count, builds the empirical null distribution of `D_TV` (leave-one-out,
so a run is never compared against a mean that contains it), and sets the
alert threshold at the `1 - alpha` quantile of that null. This is what makes
the false-positive rate an explicit, measurable quantity rather than a guess.

**Noise floor.** The distance between the noise-aware reference distribution
and the ideal noise-free state. It tells the reader how much of the observed
deviation ordinary hardware noise already explains.

**Localisation.** The first deviating checkpoint bounds the search to the
gates applied before it. Every candidate gate is then mutated in turn
(replaced, removed, or its angle altered) and the resulting distribution is
compared with the observation. Candidates are ranked by the mean distance
across all deviating checkpoints. When two candidates are statistically
indistinguishable the tie group is reported instead of an arbitrary winner.

**Honest caveat.** A deviation is evidence, not proof of malicious tampering.
Simulator noise models approximate real hardware; real-hardware validation is
future work.
        """
    )
    st.markdown("#### Implementation status")
    st.table(
        pd.DataFrame(
            [
                {"capability": "Reference circuit + checkpoints", "status": "implemented"},
                {"capability": "Noise-aware calibration (TVD + chi-square)", "status": "implemented"},
                {"capability": "Controlled tampering catalog (117 scenarios)", "status": "implemented"},
                {"capability": "Heartbeat + output-only comparison", "status": "implemented"},
                {"capability": "Differential-testing localisation", "status": "implemented"},
                {"capability": "Integrity report + SQLite history", "status": "implemented"},
                {"capability": "Streamlit dashboard", "status": "implemented"},
                {"capability": "Isolation Forest comparison", "status": "implemented (optional)"},
                {"capability": "FastAPI service", "status": "not implemented"},
                {"capability": "IBM Quantum hardware", "status": "future extension"},
            ]
        )
    )
