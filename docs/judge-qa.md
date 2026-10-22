# Judge Q&A

Short, defensible answers. Numbers refer to
[`results.md`](results.md); where a number depends on the noise profile or
configuration, the table there is the authority.

---

### 1. What problem are you solving?

A quantum program is not always run by the people who wrote it. Circuits are
compiled by third-party toolchains, submitted to cloud backends, and executed
by a provider. The party that runs the circuit can alter it — swap a gate,
delete an entangling operation, change a rotation angle — and the user sees
only a measurement histogram.

QuantumPulse is a **runtime integrity monitor** for that gap: it detects that
the executed circuit no longer behaves like the expected one, says **where** in
the circuit the behaviour diverged, and produces an evidence report.

### 2. Why heartbeat checkpoints instead of just checking the final output?

Two measured reasons:

1. **Localisation.** A final-output check can only say *that* something
   changed, so the search must cover every gate. A heartbeat narrows the search
   to the gates before the first deviating checkpoint. The evaluation runs both
   and reports top-1 accuracy for each (see the `output-only` column in the
   results).
2. **Sensitivity.** Some modifications are visible earlier than at the output,
   because later gates can partially mask them.

The cost is also measured: one execution per checkpoint instead of one, so the
overhead ratio is reported rather than hidden.

### 3. Isn't this just comparing two histograms?

The naive version — compare the output histogram with a reference and alert if
it differs — is uncalibrated and unusable in practice, because a healthy noisy
circuit already differs from its reference. What is added here:

* an **empirical null distribution** per checkpoint, built under the same noise
  model and shot count, so the threshold corresponds to an explicit
  false-positive level;
* **checkpoint anchoring** for localisation;
* **differential hypothesis testing** against a documented tampering catalogue;
* **identifiability-aware reporting** (tie groups instead of arbitrary winners);
* an **evidence report** and a persisted audit trail.

### 4. What is total variation distance and why use it?

`D_TV(P,Q) = 0.5 * sum_x |P(x) - Q(x)|`, the largest mass that has to be moved
to turn one distribution into the other. It is a metric, bounded in `[0,1]`,
and needs no binning decisions. Accuracy collapses the distribution to one
bitstring; fidelity needs tomography; chi-square needs bin pooling. See
[`method.md`](method.md) §1.

### 5. How do you know your false-positive rate?

It is measured, not asserted. Calibration runs the *healthy* circuit 200 times
with fresh seeds, computes each run's distance from the leave-one-out mean of
the others, and sets the threshold at the `1-alpha` quantile (with a Gaussian
floor). The evaluation then runs a separate, seed-disjoint set of healthy runs
and counts how many were flagged.

One subtlety we report rather than hide: `alpha = 0.005` is the level **per
checkpoint**. A round is flagged if any of the five checkpoints deviates, so
the run-level rate is bounded by `5 * alpha = 0.025` (union bound). The bound is
printed in every evidence report, and both levels were measured: **0.0044 per
checkpoint (11 of 2500 checkpoint-tests) and 0.022 per round (11 of 500 rounds)
under ideal noise** — i.e. α per test and the union bound per round, exactly as
the design predicts. Under the noisy profiles the measured run-level rate is
higher (0.060–0.070 over 100 rounds each); see the results document.

The remedy is measured too: calibrating each checkpoint at `alpha / k = 0.001`
drops the run-level rate to 0.002 (ideal) and 0.010–0.015 (noise), and costs
only 2 flags out of 351 over the whole catalogue — detection of *detectable*
scenarios stays at 1.000. That is the setting we recommend for deployment,
`--alpha 0.001`.

### 6. What if the tampering happens in the last gate?

`CP-FINAL` is placed on the last gate, so the final output is always checked —
that is the output-only baseline, included in every heartbeat round. There is
no unmonitored tail.

### 7. Does it still work with noise?

Yes, and that is why calibration exists. Four noise profiles are evaluated:
`ideal`, mild depolarising, strong depolarising, and a thermal (T1/T2) model.
The monitor is calibrated separately for each and the noise floor — how much
deviation the noise alone explains — is reported as part of the evidence.

Detection over the **detectable** scenarios is 1.000 under all four profiles.
Catalogue-wide it is 0.932–0.940, and the entire difference is the 8 scenarios
that provably cannot be detected by any distribution-based monitor. Localisation
under strong noise is actually the best of the four (0.972 strict top-1), while
the output-only baseline degrades to 0.899 — the noisier the device, the more
the heartbeats help. The one place noise does hurt is the false-positive rate:
2.2% per round under ideal noise versus 6–7% under the noisy profiles (see
`docs/results.md` §6).

### 8. What about scaling to real circuits?

Honest answer: the current prototype is limited to what a statevector simulator
can handle (4 qubits here), because localisation enumerates single-gate
hypotheses and needs many fast re-executions. Scaling requires:

* restricting the candidate set (the causal prefix already does this, and it
  stays small if checkpoints are dense);
* sampling hypotheses instead of enumerating them;
* running prefixes on hardware, where each prefix is a separate job.

The architecture does not depend on the simulator; the *cost* does.

### 9. Why did you also implement Isolation Forest?

It was in the proposed stack, so we implemented it and measured it on the same
runs instead of claiming it helps. Result on the full matrix: **identical
detection (0.932 both)**, and a false-positive count of 1/100 healthy rounds
for the Isolation Forest versus 3/100 for the calibrated rule. With 1 and 3
events that difference is not statistically meaningful — we do not claim the
Forest is better or worse on FPR.

What we can defend is structural: the `D_TV` threshold comes from a calibrated
null with a stated per-test level and a union bound, needs no training data, and
every alert is attributable to one reported statistic. The Forest's operating
point is a property of its fitted model. It remains an optional secondary
detector (`quantumpulse ml-evaluate`); the primary detector stays the calibrated
`D_TV` rule.

### 10. How is this different from formal verification or compiler testing?

Those are **static** techniques: they prove properties of a circuit before it
runs, assuming the description is the one that executes. QuantumPulse is a
**runtime** check: it assumes nothing about what executed and measures the
behaviour. The two are complementary — static verification prevents known
mistakes, runtime monitoring catches a divergence between the intended and the
executed program.

### 11. What did you actually implement?

Everything in `src/quantumpulse/` plus 70 tests. Explicitly **not**
implemented: a FastAPI service, IBM Quantum hardware execution, and multi-gate
attack search. The dashboard, the CLI and the evaluation all call the same
pipeline. The status table is also visible in the dashboard's *Method* tab.

### 12. What is your localisation accuracy?

Measured on the full evaluation matrix (117 scenarios × 3 trials × 4 noise
profiles):

* **strict top-1: 0.950 (ideal) to 0.972 (mild and strong depolarising)**;
* **top-1 or inside the reported tie group: 1.000 in every profile**;
* **top-3: 1.000 in every profile**.

The strict misses are identifiability limits: a different single-gate
modification produces the same distribution, so no number of shots can
separate them. We report the tie group instead of hiding it. See
[`method.md`](method.md) §6.

### 13. Why only 4 qubits and 16 gates?

Because the evaluation is exhaustive: 117 scenarios × 3 trials × 4 noise
profiles, with a localisation sweep over every gate and every hypothesis. A
larger circuit would make the sweep the bottleneck and reduce the number of
measured trials. The design is circuit-agnostic; the program is data.

### 14. Can a malicious provider defeat this?

Yes, in specific ways, and we say so:

* If the provider fakes **all** checkpoint results consistently, the monitor
  sees a healthy circuit. Detecting a lying provider needs trusted execution or
  an independent cross-check.
* If the tampering is inside an **equivalence class**, it is undetectable by
  distribution monitoring by construction.
* If the modification is **effectless** at every checkpoint, no monitor can see
  it.

What the monitor does detect is a *behavioural inconsistency* with the expected
circuit, which is the practical failure mode for third-party compilation and
accidental or targeted gate-level changes.

### 15. Why does removing a gate keep its position?

Because checkpoints are defined by position. If removal deleted the entry,
every later gate would shift and every later checkpoint would compare the wrong
prefix — a real bug that was found and fixed. Gates carry an `active` flag
instead. There is a regression test for it (`tests/test_program.py`).

### 16. What happens if I monitor with a different shot count than calibration?

The verifier raises an error. The threshold is derived from the finite-shot
null, so it is only valid for the shot count it was calibrated with; comparing
across shot counts silently invalidates the false-positive rate. This check was
also added because a test caught exactly that mistake.

### 17. What is the overhead?

Five prefix executions instead of one for a heartbeat round, plus the
localisation sweep when an alert fires. The measured wall-clock ratio between a
heartbeat round and an output-only round, and the mean localisation time, are
reported in the results table. Localisation only runs on alerts.

### 18. What would you do next?

1. Validate on real hardware and characterise drift (re-calibration policy).
2. Extend the hypothesis space to two-gate modifications, with sampling.
3. Randomise checkpoint placement so an adversary cannot target unmonitored
   boundaries.
4. Add a service layer (FastAPI) so a backend provider can expose the monitor
   as an API.
5. Learn the noise model online, so the null tracks hardware drift instead of
   assuming stationarity.
