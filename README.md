# QuantumPulse

**Heartbeat-based quantum circuit integrity monitoring and gate-tampering
localisation.**

A quantum program is often compiled and executed by someone other than its
author. QuantumPulse monitors the *behaviour* of the executed circuit, detects
when it stops matching the expected reference, points at the gate that changed,
and produces an evidence report.

Built for the VTU HC Quantum ARISE problem statement. Everything reported here
is produced by the code in this repository from real simulator runs — no
accuracy number is hand-written.

---

## What it does

| Capability | How |
| --- | --- |
| **Detect** behavioural deviation | Total variation distance between each checkpoint's measured distribution and a calibrated, noise-aware reference; alert threshold taken from an empirical healthy null |
| **Locate** the change | The first deviating checkpoint bounds the search; every candidate gate is then mutated (replaced / removed / angle-altered) and the hypothesis that best reproduces the observation is ranked first |
| **Explain** the alert | Per-checkpoint `D_TV`, threshold, p-value, noise floor, the top candidate gates with their evidence, and a tie group when the circuit cannot distinguish them |
| **Audit** | Every run is written to SQLite and can be reviewed in the Streamlit dashboard |

```text
$ quantumpulse localize --tamper g00:replace:x --exact-sweep
...
Status         : DEVIATION DETECTED

-- Heartbeat verification -------------------------------------------------
  checkpoint      D_TV  threshold   p(null)   score  verdict
  CP-PREP      0.49952    0.03647    0.0050   0.995  ALERT
  CP-ENT       0.50024    0.03337    0.0050   0.995  ALERT
  CP-ROT       0.49944    0.03472    0.0050   0.995  ALERT
  CP-OUT       0.49901    0.03367    0.0050   0.995  ALERT
  CP-FINAL     0.42841    0.04015    0.0050   0.995  ALERT

  * 5 of 5 checkpoints deviated beyond the calibrated threshold.

-- Suspected locations --------------------------------------------------
  Search space : 4 gate(s) preceding CP-PREP
  Evidence     : 3 deviating checkpoint(s) fused (CP-PREP, CP-ENT, CP-ROT)
  Top-1        : g00 (confidence 1.000, margin 13.34)

  #1 g00 (h(0)) stage=prep D_TV=0.01362 score=1.000
      hypothesis: replace h(0) with x on (0,)
  #2 g01 (ry(1, 0.7854)) stage=prep D_TV=0.50000 score=0.000
      hypothesis: remove ry(1, 0.7854)
```

---

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. calibrate the healthy reference (≈60 s)
PYTHONPATH=src python -m quantumpulse.cli calibrate --runs 200

# 2. inspect the circuit and the tampering catalogue
PYTHONPATH=src python -m quantumpulse.cli reference

# 3. monitor the healthy circuit
PYTHONPATH=src python -m quantumpulse.cli check

# 4. inject a controlled tampering scenario, detect and localise it
PYTHONPATH=src python -m quantumpulse.cli localize --tamper g00:replace:x --exact-sweep

# 5. dashboard
PYTHONPATH=src .venv/bin/streamlit run src/quantumpulse/dashboard.py

# 6. tests
.venv/bin/python -m pytest
```

If the package is installed (`pip install -e .`) the `quantumpulse` console
script replaces `PYTHONPATH=src python -m quantumpulse.cli`.

---

## CLI

| Command | Purpose |
| --- | --- |
| `quantumpulse reference` | Print the reference circuit, checkpoint layout and tampering catalogue |
| `quantumpulse calibrate` | Build and save the per-checkpoint healthy null and thresholds |
| `quantumpulse check` | Run one heartbeat verification |
| `quantumpulse localize` | Verify, then rank candidate gate locations |
| `quantumpulse evaluate` | Run the full evaluation matrix and write `evaluation.json` / CSVs |
| `quantumpulse summarize` | Render a saved evaluation as markdown tables |
| `quantumpulse ml-evaluate` | Compare calibrated `D_TV` against the optional Isolation Forest |
| `quantumpulse monitor` | Periodic monitoring loop with SQLite history |
| `quantumpulse report` | Summarise stored monitoring history |

Useful flags: `--noise {ideal,depolarizing_mild,depolarizing_strong,thermal}`,
`--shots`, `--tamper <scenario-id-or-gate-id>`, `--json`, `--exact-sweep`.

---

## The reference circuit

4 qubits, 16 gates, 5 heartbeat checkpoints. Gates have stable ids (`g00…g15`)
so a checkpoint position never shifts.

| Stage | Gates | Checkpoint |
| --- | --- | --- |
| preparation | `h(0)`, `ry(1,π/4)`, `h(2)`, `ry(3,π/3)` | `CP-PREP` |
| nearest-neighbour entanglement | `cx(0,1)`, `cx(1,2)`, `cx(2,3)` | `CP-ENT` |
| mid-circuit rotations | `rz(0,π/5)`, `ry(2,−π/7)` | `CP-ROT` |
| long-range entanglement | `cz(0,3)`, `cx(1,3)` | — |
| final algorithmic rotations | `ry(1,π/2)`, `rx(3,π/6)` | `CP-OUT` |
| readout rotations | `rx(1,π/3)`, `ry(0,π/5)`, `rz(2,π/7)` | `CP-FINAL` |

The readout layer is a measured design decision, not decoration: without it,
phase-only modifications on a qubit's final rotation are mathematically
undetectable (`H = Ry(π/2)·Z`), and adjacent same-axis rotations commute, so
two different gates become indistinguishable. The readout gates deliberately
use a **different axis** from the preceding rotation on the same qubit. See
[`docs/method.md`](docs/method.md) §6.

---

## How detection works in one paragraph

For each checkpoint, QuantumPulse measures the outcome distribution and computes
`D_TV` against a reference obtained by running the *healthy* circuit many times
under the same noise model and shot count. The alert threshold is the
`1 − alpha` quantile of the healthy null, computed leave-one-out so that no run
is compared against a mean containing itself. Because the threshold is derived
from the null rather than guessed, the false-positive rate is an explicit,
measured quantity. The first failing checkpoint then restricts localisation to
the gates applied before it. Full derivation:
[`docs/method.md`](docs/method.md).

---

## Measured results

All figures come from `data/evaluation/` produced by
`quantumpulse evaluate` on the full 117-scenario catalogue, except the
false-positive study (`docs/results.md` §6) and the ML comparison
(`data/ml_comparison.json`, §7), which have their own artifacts. The
authoritative tables, including per-noise-profile detection rates,
false-positive counts, overhead and the heartbeat-versus-output-only comparison,
are in [`docs/results.md`](docs/results.md).

Headline, measured on the full evaluation matrix (117 scenarios × 3 trials × 4
noise profiles, 100 healthy rounds per profile):

| Metric | Result |
| --- | --- |
| Detection over the full catalogue | 0.932 – 0.940 |
| Detection over **detectable** scenarios | **1.000** (327 / 327) in every profile |
| Strict top-1 localisation | 0.950 – 0.972 |
| Top-1 or reported tie | **1.000** in every profile |
| Top-3 localisation | 1.000 (output-only: 0.986 worst case) |
| Strict top-1, heartbeat vs output-only | 0.972 vs 0.899 under strong noise; +0.9 to +7.3 points across profiles |
| Strict top-1, fusion ablation | 0.853 with one checkpoint → **0.954** with three (ambiguity 23.9% → 8.3%) |
| Overhead of a heartbeat round | 5.12× – 5.24× |
| Run-level false-positive rate, default `alpha=0.005` | 0.022 (ideal, 500 rounds); 0.060–0.070 under noise (100 rounds each) |
| Run-level false-positive rate, recommended `alpha/k=0.001` | **0.002** (ideal); **0.010–0.015** under noise (200 rounds each) |

Recommended deployment setting: `--alpha 0.001` (per-checkpoint `alpha/k`). It
keeps detection of every detectable scenario at 1.000 and cuts the run-level
false-positive rate tenfold under ideal noise and 4–5× under noise, at the cost
of 2 flags out of 351 over the whole catalogue. The reasoning and the raw
numbers are in [`docs/results.md`](docs/results.md) §6.

8 of the 117 scenarios are *provably undetectable* (they leave every
checkpoint distribution bit-identical), which is why catalogue-wide detection
is 0.932–0.940 while every detectable scenario is caught every time.

The strict top-1 misses are identifiability limits: a different single-gate
modification produces an identical distribution, so the monitor reports a tie
group instead of inventing a winner. They are explained in
[`docs/results.md`](docs/results.md) §5.

The optional Isolation Forest was measured on the same runs: identical detection
(0.932), and a false-positive count of 1/100 versus 3/100 for the calibrated
rule — too few events to rank the two. See
[`docs/results.md`](docs/results.md) §7 for why the calibrated rule remains the
primary detector.

---

## Repository layout

```text
src/quantumpulse/
├── config.py        # all tunables, noise profiles
├── program.py       # declarative circuit model, stable gate ids
├── circuits.py      # the reference circuit + design rationale
├── backends.py      # Qiskit Aer wrapper (counts + exact statevector)
├── execution.py     # heartbeat and full-circuit execution
├── detection.py     # D_TV, JS, KL, Hellinger, chi-square
├── calibration.py   # healthy null + threshold selection
├── verification.py  # decision rule -> per-checkpoint verdicts
├── tampering.py     # controlled single-gate catalogue
├── localization.py  # differential hypothesis testing + tie groups
├── evaluation.py    # evaluation matrix and metrics
├── evidence.py      # text / JSON integrity report
├── storage.py       # SQLite history
├── monitoring.py    # periodic monitoring session
├── ml.py            # optional Isolation Forest comparison
├── dashboard.py     # Streamlit application
└── cli.py           # command-line interface
tests/               # 70 pytest tests
scripts/             # reproducible studies (calibration size, fusion, Bonferroni)
docs/                # architecture, method, results, judge Q&A
```

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Pipeline, module map, execution model, limitations |
| [`docs/method.md`](docs/method.md) | Statistics, identifiability, what is not claimed |
| [`docs/results.md`](docs/results.md) | Measured tables per noise profile |
| [`docs/judge-qa.md`](docs/judge-qa.md) | Anticipated questions with honest answers |

---

## Implementation status

**Implemented and tested**

* reference circuit with 5 heartbeat checkpoints
* calibrated, noise-aware detection (`D_TV`, optional chi-square)
* 117-scenario controlled tampering catalogue with ground truth
* differential-testing localisation with multi-checkpoint evidence fusion
* tie/ambiguity reporting (no arbitrary winners)
* heartbeat vs output-only comparison and overhead measurement
* text/JSON evidence report, SQLite history, Streamlit dashboard
* optional Isolation Forest comparison
* 70 pytest tests

**Not implemented (stated, not implied)**

* FastAPI service layer
* execution on IBM Quantum hardware
* multi-gate / colluding attack search
* adaptive or randomised checkpoint placement

---

## Limitations

* Simulation only (Qiskit Aer with explicit noise models); no hardware
  validation.
* Hypotheses are single-gate modifications; two simultaneous changes are not
  searched.
* The reference circuit and baseline are assumed trustworthy.
* Thresholds assume stationary noise and a fixed shot count (the verifier
  enforces the shot count).
* A detection is evidence of behavioural change, not proof of malicious intent.

---

## Note on legacy files

`src/circuit.py`, `src/optimizer.py`, `src/main.py` and `tests/test_circuit.py`
are leftovers from the initial repository template. They are untouched and are
not used by QuantumPulse; they can be deleted.
