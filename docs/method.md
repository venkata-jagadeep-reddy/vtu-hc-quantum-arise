# Method

This document states exactly what is computed, so that every claim in
[`results.md`](results.md) can be traced to a formula and a code path.

---

## 1. The detection statistic

Let `P` be the observed outcome distribution of a checkpoint (a vector over
`2^n` bitstrings, normalised to sum to 1) and `Q` the calibrated reference.

```
D_TV(P, Q) = 1/2 * sum_x |P(x) - Q(x)|
```

`D_TV` is the total variation distance: the largest amount of probability mass
that must be moved to turn `P` into `Q`. It is in `[0, 1]`, it is a metric, and
it needs no binning decisions. `D_TV = 0.25` means at least 25% of the mass
moved.

Why not accuracy or fidelity?

* **Accuracy** collapses the whole distribution to the single most likely
  bitstring and is blind to everything else.
* **Fidelity** between measured and ideal states requires state tomography,
  which needs many more measurements than a monitor can afford.
* **Chi-square** is a valid test but requires pooling sparse bins, which
  introduces arbitrary choices. It is implemented as an *optional*
  corroboration (`--rule tvd+chi2`) and is not the primary decision rule.

Implementation: `detection.total_variation_distance`.

---

## 2. Calibrated null and threshold

A healthy circuit still produces `D_TV > 0` because of finite shots and
hardware noise, so a fixed threshold would be arbitrary. QuantumPulse measures
the null instead:

1. Execute the healthy reference circuit `N` times with fresh seeds, at the
   **same** shot count and under the **same** noise model that will be used in
   production. Collect the distributions `p_1 … p_N`.
2. For each `i`, compute

   ```
   d_i = D_TV( p_i , mean_{j != i} p_j )
   ```

   The mean excludes `p_i` (**leave-one-out**) so that a run is never scored
   against a reference that contains itself. Without this, the null is
   optimistically narrow and the threshold too tight.
3. Set the reference distribution `Q = mean_i p_i` and the threshold

   ```
   tau = max( quantile_{1-alpha}({d_i}),  mean(d) + z_{1-alpha} * std(d) )
   ```

   The second term is a Gaussian bound; the maximum of the two protects against
   a heavy-tailed null or too few samples to resolve the empirical quantile
   directly. This is conservative **for a single test**, but it is not a licence
   to claim that the run-level rate equals `alpha`: a round performs `k` tests
   (§3), and with too few calibration runs the quantile itself is unreliable.
   Both effects are measured in [`results.md`](results.md) §6 rather than
   assumed away.

4. Report, as evidence:

   * `noise_floor = D_TV(Q, ideal_state)` — how much deviation the noise model
     already explains. A measured deviation near this value is unremarkable.
   * `null_mean`, `null_std` — the centre and spread of healthy behaviour.

Implementation: `calibration.calibrate`, `calibration._select_threshold`.

### The p-value

```
p = (1 + #{ d_i >= d_obs }) / (N + 1)
```

This is the standard conservative empirical p-value for exchangeable null
samples. With `N = 200` and `alpha = 0.005` the smallest attainable value is
`1/201 = 0.00498`, i.e. the calibration size is matched to the significance
level: a smaller `alpha` would require more healthy runs to be resolvable.

The reported `anomaly_score = 1 - p` is a confidence, not a probability that an
attack occurred.

Implementation: `calibration.CheckpointBaseline.null_p_value`.

---

## 3. Decision rule

```
flagged = (D_TV > tau)
```

with an optional additional requirement `chi2_p < chi2_alpha` when
`--rule tvd+chi2` is used. A run is flagged if **any** checkpoint is flagged;
the first flagged checkpoint (in program order) is reported as
`first_bad_checkpoint`.

The threshold is valid only for the shot count and noise model it was
calibrated with. `verify_heartbeats` therefore **raises** on a shot-count
mismatch rather than silently producing an invalid false-positive rate.

### Multiplicity: per-checkpoint α versus per-run α

`alpha` applies to **each checkpoint individually**. A round is flagged when
*any* checkpoint exceeds its threshold, so with `k` checkpoints tested the
run-level false-positive rate is at most `k * alpha` by the union bound (no
independence assumption). With five checkpoints and `alpha = 0.005` that bound
is `0.025`.

QuantumPulse reports the bound explicitly (`family_wise_false_positive_bound`
in the verdict and in every evidence report) instead of presenting the
per-checkpoint level as the run-level rate. Both levels were measured over 500
seed-disjoint healthy rounds: **0.44% per checkpoint (11 / 2500 tests) and 2.2%
per round (11 / 500)** against the 2.5% bound. Under the noisy profiles the
measured run-level rate is 6–7% over 100 rounds each, where a 200-run empirical
null slightly underestimates the tail. Output-only checking shows 0/100 in the
same experiment only because it performs one test per round.

A Bonferroni-corrected variant (calibrating each checkpoint at `alpha / k`) was
also measured: 0.2% per round (1 / 500) against a 0.5% bound, and 1.0–1.5% under
the noisy profiles (200 rounds each) — a 4–5× reduction, though still above the
bound because a 200-run null underestimates the tail under noise. On the full
catalogue it costs 0.6 points of catalogue-wide detection (0.940 → 0.934) while
detection over detectable scenarios stays at **1.000**, so it is the
configuration recommended for deployment. It is not the shipped default, so the
evaluation matrix in [`results.md`](results.md) describes the uncorrected
configuration and the comparison there quantifies the alternative.

Implementation: `verification.verify_heartbeats`.

---

## 4. Tampering model

The system is evaluated against a documented, bounded catalogue of
**single-gate** modifications of the reference program:

| Kind | Construction | Count |
| --- | --- | --- |
| replacement | replace the gate's operation with another from a fixed table, keeping the qubit(s) | 65 |
| removal | set `active = False` (the position stays) | 16 |
| alteration | add `±pi/2`, `±pi/4` or `pi` to a rotation angle | 36 |

Two details that matter for interpreting the numbers:

* For two-qubit gates the replacement table includes **operand reversal**
  (`cx(0,1) -> cx(1,0)`), reported as `…:replace:cx@rev`. Swapping control and
  target is a subtle, realistic modification that a visual diff can easily
  miss, so it belongs in the catalogue.
* Replacing a rotation with another rotation axis keeps the original angle
  ("axis confusion"); replacing it with a non-parametric gate drops the angle.

Total: **117** scenarios, generated mechanically by
`tampering.build_tamper_catalog`.

Two properties are computed per scenario and used when reporting:

* `exact_effect` — the sampling-free `D_TV` between the reference state and the
  tampered state at every checkpoint, using the statevector simulator.
* `is_effectless` — true when every checkpoint is unchanged
  (`D_TV <= 1e-9`). Such a scenario is **undetectable in principle** by any
  distribution-based monitor, so detection rates are also reported over
  `effective_scenarios` only, and effectless scenarios are excluded from the
  false-positive count rather than being silently scored as misses.

This catalogue is a *test suite*, not a claim about real adversaries. It exists
so that detection and localisation can be measured with ground truth.

---

## 5. Localisation

Given a detected deviation:

1. **Bound the search.** The first deviating checkpoint `c*` was produced by the
   prefix of gates `g_0 … g_{pos(c*)}`. Only those gates can have caused it, so
   the candidate set is that causal prefix. This is the heartbeat's second
   contribution: an output-only check can only say *that* something changed and
   must search the whole circuit.

2. **Enumerate hypotheses.** For each candidate gate `g`, the same catalogue
   generator produces a hypothesis set `H(g)` (replacements, removal, angle
   alterations), applied to the **reference** program — never to the tampered
   one, which is the unknown.

3. **Predict and score.** For hypothesis `h`, apply it to the reference and
   compute the predicted distribution at every deviating checkpoint:

   ```
   score(g) = min_{h in H(g)}  mean_{c in deviating checkpoints}  D_TV( observed_c , predicted_c(g,h) )
   ```

   The mean across checkpoints is the **evidence fusion**. It is measured, not
   assumed, that fusing helps: scored against the *same* observations with the
   *same* candidates, using one checkpoint instead of three drops strict top-1
   from 0.954 to 0.853 and raises the ambiguity rate from 8.3% to 23.9%
   (`scripts/fusion_ablation.py`). A single checkpoint often leaves two
   hypotheses within sampling noise of each other; a later checkpoint
   separates them.

4. **Rank and report.** Candidates are sorted by `score`. The tie group is
   every candidate within `2 * null_std` of the best:

   ```
   tied = { g : score(g) - score(best) <= 2 * null_std }
   ```

   `null_std` is the calibrated spread of healthy runs, so the band is
   expressed in the same units as the measurement noise.

5. **Confidence.** Candidate weights are a softmax over the negated scores with
   temperature `T = 0.25 * tau`:

   ```
   w_i = exp( -(score_i - score_best) / T ) / sum_j exp( -(score_j - score_best) / T )
   ```

   Reporting `w_best` together with the margin is more informative than a bare
   label, and a near-0.5 confidence correctly signals ambiguity.

Implementation: `localization.localize`.

### Reproducibility detail

Hypotheses are evaluated with a seed offset relative to the observation
(`seed + 1`) so that the comparison measures genuine sampling noise rather than
replaying the identical random stream. All hypotheses share the same offset
(**common random numbers**), which keeps the comparison between candidates
fair.

With `--exact-sweep`, the sweep uses statevector probabilities (deterministic,
and much faster) and then re-scores the top candidates with real shots to
report a noise-aware distance. Both numbers appear in the evidence report.

---

## 6. Identifiability: what no monitor can do

Two distinct facts limit localisation, and both are reported rather than hidden.

### 6.1 Effectless modifications

If a modification commutes with everything after it and is diagonal in the
measurement basis, or otherwise leaves the measurement distribution unchanged,
then `D_TV = 0` at every checkpoint. No statistical test can detect it. The
catalogue contains **8** such scenarios out of 117 (e.g. inserting a relative
phase on a qubit that is never rotated or entangled again).

### 6.2 Equivalent modifications

Two **different** single-gate modifications can be *exactly* equivalent for a
given circuit, in which case their predicted distributions are identical and
the scores are equal to machine precision. This is not a search failure.

Measured example from development: with the readout rotation on qubit 1 being
`ry(1, pi/3)` directly after `ry(1, pi/2)`, the two rotations commute, so
"alter g11 by delta" and "alter g13 by delta" change the state by the same
total rotation and are indistinguishable. All four `g13` angle alterations
were scored as ties with `g11`.

The response was a **design fix, not a scoring trick**: the readout layer now
uses a *different axis* from the preceding rotation on the same qubit
(`rx` after `ry`, `ry` after `rz`, `rz` after `ry`), so the rotations no longer
commute. This measurably removed that equivalence class.

Residual ties are structural (a qubit with two single-qubit gates and nothing
between them reduces to one net rotation). They are reported as
`ambiguous` with an explicit `tied_gids` list, and top-1 accuracy is reported
both strictly and tie-aware:

| Metric | Definition |
| --- | --- |
| `top1_accuracy` | best-ranked gate is exactly the modified gate |
| `top1_or_tie_accuracy` | the modified gate is the best-ranked **or** in the reported tie group |
| `top3_accuracy` | the modified gate is among the top three candidates |

The gap between the first two is the circuit's identifiability limit. Presenting
only the strict number would understate the method; presenting only the
tie-aware number would overstate the precision of the answer.

---

## 7. What is *not* claimed

* No claim of detecting **every** attack. The hypothesis space is single-gate
  modifications; multi-gate collusion is out of scope.
* No claim of optimality against an adversary who knows the monitor and chooses
  a modification inside an equivalence class.
* No claim of hardware validity. All measurements are from Qiskit Aer with
  explicit noise models.
* No claim that a flag proves malicious intent. A flag is evidence that
  behaviour changed.
