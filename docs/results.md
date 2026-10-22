# Results

**Provenance.** Every number below was produced by the code in this repository
from Qiskit Aer runs. Nothing is estimated by hand. The authoritative raw
outputs are `data/evaluation/evaluation.json`, `data/evaluation/metrics.csv`,
`data/evaluation/trials.csv`, `data/ml_comparison.json` and the calibrated
`data/baseline.json`.

Environment: Python 3.12, qiskit 1.4.6, qiskit-aer 0.18.0, numpy 2.5.4, on
Linux x86-64 (CPU simulation).

Reproduce:

```bash
make calibrate      # data/baseline.json           (~60 s)
make evaluate       # data/evaluation/             (~30-45 min)
python -m quantumpulse.cli summarize --input data/evaluation/evaluation.json
```

---

## 1. What is being measured

| Item | Value |
| --- | --- |
| Reference program | 4 qubits, 16 gates, 5 heartbeat checkpoints |
| Tampering catalogue | 117 single-gate scenarios: 65 replacements, 16 removals, 36 angle alterations |
| Scenarios that cannot be detected in principle | 8 (`effectless`: the exact output distribution is unchanged) |
| Detection statistic | `D_TV` against a leave-one-out calibrated reference |
| Significance level | `alpha = 0.005` **per checkpoint**; measured 0.0044 per checkpoint and 0.022 per round (ideal, 500 rounds); 0.060–0.070 per round under noise (§6) |
| Calibration | 200 healthy runs per checkpoint per noise profile |
| Healthy (FPR) runs | 100 per noise profile, seed-disjoint from calibration (plus a 500-round ideal study, §6) |
| Tampered runs | 3 per scenario per noise profile |

`effectless` scenarios are not counted as detector failures: a modification
that provably leaves every checkpoint distribution unchanged cannot be seen by
any distribution-based monitor. Detection rates are therefore also reported
over `effective_scenarios` only.

---

## 2. Detection

_Produced by `quantumpulse summarize --input data/evaluation/evaluation.json`._

| Noise profile | Healthy | Tampered | TP | FP | FN | Detection | FPR | Precision | F1 | Detection (detectable only) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `ideal` | 100 | 351 | 330 | 5 | 21 | **0.940** | 0.050 | 0.985 | 0.962 | **1.000** |
| `depolarizing_mild` | 100 | 351 | 327 | 7 | 24 | 0.932 | 0.070 | 0.979 | 0.955 | **1.000** |
| `depolarizing_strong` | 100 | 351 | 327 | 6 | 24 | 0.932 | 0.060 | 0.982 | 0.956 | **1.000** |
| `thermal` | 100 | 351 | 329 | 6 | 22 | 0.937 | 0.060 | 0.982 | 0.959 | **1.000** |

How to read this table — the distinction matters:

* **Detection over the full catalogue is 0.932–0.940.** The 21–24 false
  negatives per profile are the runs of the **8 scenarios that are provably
  undetectable** — 8 × 3 = 24 runs per profile. In the `ideal` and `thermal`
  profiles a few of those 24 were flagged by chance, which is why their false
  negative counts are 21 and 22 rather than 24. Those scenarios leave every
  checkpoint distribution bit-identical (`effect_max = 0.0000`, computed with
  the statevector simulator), so no distribution-based monitor can see them.
* **Detection over detectable scenarios is 1.000 in every profile, in every
  trial** (327 / 327). Every one of the 109 scenarios that actually changes the
  distribution was caught every time, under all four noise models, including
  the strong-depolarising and thermal models.
* The small overshoot in `TP` for `ideal` and `thermal` (330 and 329 rather than
  327) comes from flags on effectless *tampered* runs. Those circuits are
  behaviourally identical to the healthy circuit, so such a flag is a false
  positive that the label calls a true positive. It is the same phenomenon as
  the healthy-round false positives, and it is disclosed rather than smoothed
  over.
* **FPR here is the run-level (any-checkpoint) rate**, measured on 100
  seed-disjoint healthy rounds. It is larger than `alpha` because a round tests
  five checkpoints; see §6 and the union-bound discussion in
  [`method.md`](method.md) §3.

### Heartbeat versus output-only detection

The harness runs the *same* tampered executions through an output-only check
(`CP-FINAL` only) so the heartbeat's value can be measured instead of asserted:

| Noise profile | Heartbeat detection | Output-only detection | Heartbeat top-1 | Output-only top-1 |
| --- | ---: | ---: | ---: | ---: |
| `ideal` | 0.940 | 0.932 | 0.950 | 0.927 |
| `depolarizing_mild` | 0.932 | 0.932 | 0.972 | 0.936 |
| `depolarizing_strong` | 0.932 | 0.932 | 0.972 | 0.899 |
| `thermal` | 0.937 | 0.932 | 0.954 | 0.945 |

Honest conclusion: **detection is essentially unchanged** (+0.8 points in the
ideal case, equal elsewhere), because a modification of this circuit's gates
almost always propagates to the output. What the heartbeats measurably buy is
**localisation** (+0.9 to +7.3 points of strict top-1 across the four profiles)
and a much smaller
search space: an output-only alert must search all 16 gates, while the earliest
deviating checkpoint typically narrows that to 4–9.

Output-only run-level FPR is 0.000 in all profiles because it performs **one**
statistical test per round instead of five. That is multiplicity, not superior
calibration.

---

## 3. Localisation

Localisation is attempted on every detected tampered run (2 trials per scenario
in this run, so 218–219 attempts per profile).

| Noise profile | Attempts | Strict top-1 | Top-1 or reported tie | Top-3 | Ambiguous | Mean confidence | Output-only top-1 | Output-only top-3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `ideal` | 219 | 0.950 | **1.000** | 1.000 | 0.082 | 0.955 | 0.927 | 1.000 |
| `depolarizing_mild` | 218 | **0.972** | **1.000** | 1.000 | 0.078 | 0.957 | 0.936 | 1.000 |
| `depolarizing_strong` | 218 | **0.972** | **1.000** | 1.000 | 0.101 | 0.929 | 0.899 | 0.986 |
| `thermal` | 219 | 0.954 | **1.000** | 1.000 | 0.082 | 0.955 | 0.945 | 1.000 |

* **The true gate is always inside the reported tie group, in every attempt,
  under every noise model.** Strict top-1 is 0.950–0.972; the difference is
  identifiability (§5), not a search failure.
* The strong-depolarising output-only search is the only configuration that
  also misses the top three (0.986): the extra noise blurs the exact-equivalence
  classes beyond the tie tolerance.
* Ambiguity is reported, not hidden: 7.8–10.1% of attempts return
  `ambiguous = true` with the `tied_gids` list.
* The heartbeat advantage is largest exactly where it matters — under strong
  noise, 0.972 versus 0.899 strict top-1 for the output-only search.

### Evidence fusion ablation

The fusion step is not assumed to help; it is measured by re-running the *same*
observations and the *same* candidate set while changing only how many deviating
checkpoints are fused into the score (`scripts/fusion_ablation.py`, ideal
simulation, 4096 shots, 109 detected scenarios):

| Deviating checkpoints fused | Strict top-1 | Top-1 or tie | Top-3 | Ambiguous |
| ---: | ---: | ---: | ---: | ---: |
| 1 (earliest only) | 0.853 | 1.000 | 0.982 | 0.239 |
| 3 | **0.954** | **1.000** | **1.000** | **0.083** |
| 5 (all) | 0.954 | 1.000 | 1.000 | 0.083 |

Fusing three checkpoints instead of one raises strict top-1 by **10.1 points**
and cuts the ambiguity rate from 23.9% to 8.3%. Going beyond three changes
nothing on this catalogue, which is why three is the default cap: later
checkpoints add cost without adding information here.

This is independent of the circuit-design fix in §5 — that one was measured by
changing the readout axes with fusion held at three, and this one by changing
the fusion depth with the readout axes already fixed.

---

## 4. Cost

| Noise profile | Heartbeat round (s) | Output-only (s) | Overhead | Mean localisation (s) |
| --- | ---: | ---: | ---: | ---: |
| `ideal` | 0.484 | 0.093 | **5.22×** | 0.823 |
| `depolarizing_mild` | 0.368 | 0.072 | 5.12× | 0.697 |
| `depolarizing_strong` | 0.385 | 0.073 | 5.24× | 0.782 |
| `thermal` | 0.351 | 0.068 | 5.17× | 0.730 |

The overhead is what the architecture predicts: **one prefix execution per
checkpoint**, and there are five checkpoints, so a heartbeat round costs
≈5× a single output-only run. It is linear in the number of checkpoints, not
exponential, and the localisation sweep runs only when an alert fires.

These are wall-clock numbers on CPU statevector/shot simulation; they are not a
hardware cost model, where each prefix would be a separate job submission with
queueing latency.

---

## 5. Identifiability: why strict top-1 is not 100%

Strict top-1 is 0.950–0.972, and **every** miss is inside the reported tie
group, so the true gate is always within `2 * null_std` of the best score.
Two distinct mechanisms produce these ties, and both were verified at the
statevector level (the predicted distributions are identical, not merely close):

**(a) Phase absorption on qubit 0.** `g00` (`h(0)`) is the only gate that puts
qubit 0 into superposition, and every gate between it and the readout layer
uses qubit 0 as a *control* (`cx(0,1)`, `cz(0,3)`), which is diagonal in the
computational basis. A relative phase on qubit 0 therefore commutes with
everything until the `rz(0, ·)` / `ry(0, ·)` gates, where it is exactly an angle
change. Consequence: replacing `h` by `rx(π/2)` (which differs from `h` only by
a relative phase) is **the same operation** as altering the later `rz` angle.

**(b) Net-rotation collapse on qubit 2.** Nothing acts on qubit 2 between `g08`
(`ry(2, −π/7)`) and `g15` (`rz(2, π/7)`). Any single-qubit gate placed at `g15`
composes with `g08` into one net `SU(2)` element, which a different rotation at
`g08` can reproduce exactly.

| Scenario | True gate | Reported top-1 | Mechanism |
| --- | --- | --- | --- |
| `g00:replace:rx` | `g00` (`h(0)`) | `g07` (`rz(0,π/5)`) | (a) phase absorption |
| `g07:alter:±π/2` | `g07` | `g00` / `g14` | (a) phase absorption |
| `g14:replace:rx` | `g14` (`ry(0,π/5)`) | `g07` | (a) phase absorption |
| `g15:replace:ry` | `g15` (`rz(2,π/7)`) | `g08` | (b) net-rotation collapse |
| `g15:replace:h` | `g15` | `g08` | (b) net-rotation collapse |
| `g15:replace:x` | `g15` | `g08` | (b) net-rotation collapse |

For these cases the two candidates score identically to machine precision and
the reported confidence is ≈0.50, which is the correct answer: the circuit does
not contain the information needed to distinguish them.

A few further strict misses in the evaluation (`g05:removal`,
`g07:alter:+π/2`, `g13:alter:+π`, `g08:replace:h` under strong noise) are inside
the `2 * null_std` band but were **not** proven exactly equivalent. They are
reported as `ambiguous` for the same operational reason — the measurement
cannot separate them at this shot count — but they should not be described as
mathematically identical.

### The design response

This is also why the reference circuit has a readout layer with alternating
axes. An earlier design used `ry` for both the final rotation and the readout
rotation on qubit 1; those commute, so all four `g13` angle alterations were
exactly equivalent to `g11` alterations. On the same ideal bench that measured
95.4% afterwards, strict top-1 was **88.3%** before the change. Making the
readout rotation `rx` removed that equivalence class. The improvement is a
**circuit design** change, not a tuning of the scoring function.

---

## 6. False-positive rate: per checkpoint versus per round

`alpha = 0.005` is the level **per checkpoint**. A round is flagged when *any*
of the five checkpoints deviates, so the run-level rate is what an operator
experiences, and it is larger. Both levels are measured:

| Noise profile | Healthy rounds | Run-level FPR (any checkpoint) | Per-checkpoint FPR | Union bound `5·alpha` |
| --- | ---: | ---: | ---: | ---: |
| `ideal` | 500 | **0.022** (11 / 500) | **0.0044** (11 / 2500 tests) | 0.025 |
| `depolarizing_mild` | 100 | 0.070 | — | 0.025 |
| `depolarizing_strong` | 100 | 0.060 | — | 0.025 |
| `thermal` | 100 | 0.060 | — | 0.025 |

* **Ideal noise matches the design exactly.** The per-checkpoint rate is 0.44%
  against a nominal 0.5%, and the run-level rate is 2.2% against the 2.5% union
  bound. This is what five tests at `alpha = 0.005` should produce.
* **Under noise the measured run-level rate is 6–7%**, about three times the
  bound. Each figure rests on 100 rounds, so a count of 6 or 7 spans roughly
  2–13% at 95% confidence; the three profiles nevertheless agree with each
  other. The likely cause is that a 200-run empirical null slightly
  underestimates the tail when the distributions are noisy — consistent with
  the calibration-size table below. We report the measurement rather than the
  bound alone.
* Output-only checking shows 0/100 in the same experiment **because it performs
  one test per round**, not because its calibration is better.

The honest summary: **do not quote 0.5% as the run-level false-positive rate.**
The defensible statements are "0.5% per checkpoint" and "2.2% per round under
ideal noise, 6–7% under the noisy profiles". The remedy — calibrating each
checkpoint at `alpha / k` — is measured in the last subsection: it is cheap in
detection and cuts the rate by 10× (ideal) and 4–5× (noise).

### Calibration size matters more than `alpha`

The threshold is an order statistic of the healthy null, so the number of
calibration runs bounds the smallest resolvable rate. Measured with the
seed-disjoint nominal block (`scripts/calibration_study.py`, 200 healthy rounds
per row, 4096 shots, ideal simulation):

| Calibration runs | Per-checkpoint `alpha` | Run-level FPR (measured) | Per-checkpoint FPR (measured) | Union bound `5·alpha` |
| ---: | ---: | ---: | ---: | ---: |
| 16 | 0.02 | **0.215** | 0.046 | 0.100 |
| 50 | 0.02 | 0.060 | 0.012 | 0.100 |
| 200 | 0.02 | 0.070 | 0.014 | 0.100 |
| 200 | 0.005 | 0.010 | 0.002 | 0.025 |

With only 16 calibration runs the threshold is badly estimated and the realised
rate is more than twice the union bound: **a small calibration set cannot honour
a small `alpha`.** The shipped default is 200 runs. This is also why the test
suite's 16-run fixture must not be read as a performance measurement.

Note on the last row: it is a 200-round sample (2 events), so it reads 1.0%,
while the 500-round measurement at the same setting reads 2.2%. The two are
consistent — 2/200 and 11/500 have overlapping confidence intervals — and the
difference is a useful reminder that a false-positive rate needs hundreds of
healthy rounds before it can be quoted to one significant figure.

### The Bonferroni option, measured

Since five tests at `alpha = 0.005` give a ~2.5% run-level rate, the obvious fix
is to calibrate each checkpoint at `alpha / k = 0.001`. That choice was measured
rather than assumed to be expensive
(`scripts/bonferroni_tradeoff.py`, full catalogue, 3 trials, ideal):

| Per-checkpoint `alpha` | Run-level bound | Detection (catalogue) | Detection (detectable) | Run FPR, ideal (500 rounds) | Run FPR, noisy profiles |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.005 (default) | 0.025 | 0.940 | **1.000** | 0.022 | 0.060–0.070 (100 rounds each) |
| 0.001 (`alpha/k`) | **0.005** | 0.934 | **1.000** | **0.002** | **0.010–0.015** (200 rounds each) |

The trade-off is almost free on this catalogue: under ideal noise the run-level
false-positive rate drops **tenfold** (2.2% → 0.2%) and catalogue-wide detection
falls by 2 flags out of 351. Detection over *detectable* scenarios stays at
**1.000** — the two flags that disappear were on runs of the
provably-undetectable scenarios, i.e. they were false alarms. The higher
thresholds are visible in the calibrated values
(`0.0365 0.0358 0.0359 0.0361 0.0402` versus `0.0365 0.0334 0.0347 0.0337
0.0402`).

Under noise the correction cuts the run-level rate by **4–5×**
(6–7% → 1.0–1.5%) but does not reach the 0.5% bound: the per-checkpoint rates
are 0.2–0.3% against a nominal 0.1%. The 200-run empirical null still
underestimates the tail when the distributions are noisy. More calibration runs
are the remedy (the table above shows the sensitivity), and that is the honest
limit of the current configuration rather than something to claim away
(`scripts/fpr_under_noise.py`).

For deployment we therefore recommend `--alpha 0.001` (equivalently, calibrating
at `alpha/k`). The main evaluation table above reports the default
configuration, because that is the configuration the matrix was run with; the
comparison here is what quantifies the alternative.

---

## 7. Optional machine-learning comparison

The project plan lists scikit-learn / Isolation Forest as an optional
secondary method, so it was implemented and measured on the **same** runs
rather than assumed to help. Detector: one Isolation Forest per checkpoint over
the outcome vector plus entropy, maximum probability, support size and `D_TV`
from the reference; threshold at the `1-alpha` quantile of the training scores;
trained on 100 healthy runs, evaluated on 100 seed-disjoint healthy runs and
117 scenarios × 3 trials.

```bash
python -m quantumpulse.cli ml-evaluate --trials 3 --nominal-runs 100
```

| Detector | Detection rate | False-positive rate |
| --- | ---: | ---: |
| Calibrated `D_TV` (primary) | 0.932 | 0.030 |
| Isolation Forest (optional) | 0.932 | 0.010 |

(351 tampered rounds, 100 seed-disjoint healthy rounds, ideal simulation;
`data/ml_comparison.json`.)

Honest reading of the measurement:

* **Detection is identical (0.932 / 0.932).** There is no measured evidence that
  the Isolation Forest finds anything the calibrated rule misses, on this
  catalogue.
* **The Isolation Forest's measured FPR is lower — 1 of 100 healthy rounds
  versus 3 of 100.** With 1 and 3 events, the confidence intervals overlap
  almost completely; this is far too little data to rank the two detectors.
  Reporting "the ML detector has a lower false-positive rate" would be an
  over-claim, and so would the opposite.
* What *can* be said is structural rather than empirical: the `D_TV` threshold
  is derived from a calibrated null with a stated per-test level and a union
  bound, it needs no training set or model selection, and every alert is
  attributable to one reported statistic. The Isolation Forest threshold is
  fitted at the `1-alpha` quantile of its own training scores, so its operating
  point is a property of the fitted model rather than a stated guarantee.

It is therefore shipped as an optional comparison (`quantumpulse ml-evaluate`,
`pip install -e ".[ml]"`), not as the primary detector. Nothing in the
monitoring pipeline depends on scikit-learn.

A larger healthy-run budget is the only way to separate the two on FPR; that is
listed as future work rather than papered over with a small-sample claim.

---

## 8. What these results do *not* show

* **No hardware validation.** All runs are Qiskit Aer with explicit noise
  models. Real devices add drift, crosstalk and gate-dependent errors that these
  models only approximate.
* **No multi-gate attacks.** The hypothesis space is single-gate modifications.
  Two simultaneous changes would not be found by this search.
* **No adversarial optimality.** An adversary who knows the circuit can choose a
  modification inside an equivalence class, which is undetectable in principle.
* **One circuit family.** The reference is a 4-qubit circuit chosen so the
  evaluation can be exhaustive (117 scenarios × trials × 4 noise profiles). The
  method is circuit-agnostic, but the measured numbers are for this circuit.
* **Single-machine timing.** The overhead figures are wall-clock on CPU
  simulation and are not a hardware cost model.
