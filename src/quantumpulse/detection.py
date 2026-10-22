"""Noise-aware statistical comparison of measurement distributions.

The primary statistic of QuantumPulse is the total variation distance

    D_TV(P, Q) = 0.5 * sum_x |P(x) - Q(x)|

which is bounded to [0, 1] and is exactly the largest amount of probability
mass that has to be re-interpreted to turn P into Q.  A D_TV of 0.25 means at
least 25% of the probability mass moved.

Because measurements are probabilistic, a healthy circuit also produces a
non-zero D_TV against its reference distribution.  The threshold therefore has
to be *calibrated* from repeated healthy runs (see :mod:`quantumpulse.calibration`)
rather than fixed by hand.

Supporting statistics implemented here:

* ``chi_square_gof`` - Pearson goodness-of-fit test, gives an interpretable
  p-value and corroborates the D_TV decision;
* ``jensen_shannon_distance`` - symmetric, bounded, robust when either
  distribution has zero mass somewhere;
* ``kl_divergence`` - asymmetric, highly sensitive to missing support;
* ``hellinger_distance`` - bounded and stable for small distributions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

_EPS = 1e-12


def _as_array(values: Sequence[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("distribution must be one-dimensional")
    return arr


def total_variation_distance(p: Sequence[float], q: Sequence[float]) -> float:
    """D_TV(P, Q) = 0.5 * sum_x |P(x) - Q(x)|."""

    p_arr, q_arr = _as_array(p), _as_array(q)
    if p_arr.shape != q_arr.shape:
        raise ValueError("distributions must have the same support size")
    return float(0.5 * np.sum(np.abs(p_arr - q_arr)))


def _normalise(arr: np.ndarray) -> np.ndarray:
    total = arr.sum()
    if total <= 0:
        raise ValueError("distribution has zero mass")
    return arr / total


def jensen_shannon_divergence(p: Sequence[float], q: Sequence[float]) -> float:
    p_arr, q_arr = _as_array(p), _as_array(q)
    m = 0.5 * (p_arr + q_arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = np.where(p_arr > 0, p_arr * np.log2(p_arr / m), 0.0)
        t2 = np.where(q_arr > 0, q_arr * np.log2(q_arr / m), 0.0)
    return float(0.5 * t1.sum() + 0.5 * t2.sum())


def jensen_shannon_distance(p: Sequence[float], q: Sequence[float]) -> float:
    """Square root of the Jensen-Shannon divergence (a metric in [0, 1])."""

    return float(np.sqrt(max(jensen_shannon_divergence(p, q), 0.0)))


def kl_divergence(p: Sequence[float], q: Sequence[float], eps: float = _EPS) -> float:
    """D_KL(P || Q) in nats, with additive smoothing for empty support."""

    p_arr = _as_array(p)
    q_arr = np.maximum(_as_array(q), eps)
    mask = p_arr > 0
    return float(np.sum(p_arr[mask] * np.log(p_arr[mask] / q_arr[mask])))


def hellinger_distance(p: Sequence[float], q: Sequence[float]) -> float:
    """Hellinger distance (bounded by 1)."""

    p_arr, q_arr = _as_array(p), _as_array(q)
    bc = np.sqrt(np.clip(p_arr, 0, None) * np.clip(q_arr, 0, None))
    return float(np.sqrt(max(1.0 - bc.sum(), 0.0)))


def chi_square_gof(
    counts: Dict[str, int],
    expected_probs: Sequence[float],
    num_qubits: int,
    alpha_merge: float = 5.0,
) -> Tuple[float, int, float]:
    """Pearson goodness-of-fit test of observed counts against expected probs.

    Bins whose expected count is below ``alpha_merge`` are pooled into a
    single "other" bin, which is the standard remedy for the chi-square
    approximation failing on sparse bins.

    Returns ``(chi2_statistic, degrees_of_freedom, p_value)``.
    """

    from .execution import bitstring_to_index

    expected = _as_array(expected_probs)
    size = expected.size
    observed = np.zeros(size, dtype=float)
    for bits, count in counts.items():
        observed[bitstring_to_index(bits, num_qubits)] = count

    total = observed.sum()
    if total <= 0:
        raise ValueError("no shots recorded")
    expected_counts = expected * total

    large = expected_counts >= alpha_merge
    small = ~large

    # Keep every well-populated bin as its own cell; pool only the sparse
    # bins into a single "other" cell.  Pooling the large bins together would
    # collapse the whole test into one degree of freedom and make it useless.
    groups_o: list[float] = [float(v) for v in observed[large]]
    groups_e: list[float] = [float(v) for v in expected_counts[large]]
    if small.any():
        groups_o.append(float(observed[small].sum()))
        groups_e.append(float(expected_counts[small].sum()))

    if len(groups_o) < 2:
        return 0.0, 0, 1.0

    groups_e_arr = np.asarray(groups_e)
    mask = groups_e_arr > 0
    if mask.sum() < 2:
        return 0.0, 0, 1.0

    o = np.asarray(groups_o)[mask]
    e = groups_e_arr[mask]
    chi2 = float(np.sum((o - e) ** 2 / e))
    dof = int(len(o) - 1)
    p_value = float(stats.chi2.sf(chi2, dof))
    return chi2, dof, p_value


@dataclass
class DistributionComparison:
    """Bundle of all comparison statistics between two distributions."""

    tvd: float
    chi2: float = float("nan")
    chi2_dof: int = 0
    chi2_p_value: float = float("nan")
    js_distance: float = float("nan")
    kl_pq: float = float("nan")
    hellinger: float = float("nan")

    def to_dict(self) -> Dict[str, float]:
        return {
            "tvd": self.tvd,
            "chi2": self.chi2,
            "chi2_dof": self.chi2_dof,
            "chi2_p_value": self.chi2_p_value,
            "js_distance": self.js_distance,
            "kl_pq": self.kl_pq,
            "hellinger": self.hellinger,
        }

    def to_table(self) -> Dict[str, str]:
        return {
            "Total variation": f"{self.tvd:.6f}",
            "Chi-square (p)": f"{self.chi2:.3f} (p={self.chi2_p_value:.3g})",
            "Jensen-Shannon": f"{self.js_distance:.6f}",
            "KL(P||Q)": f"{self.kl_pq:.6f}",
            "Hellinger": f"{self.hellinger:.6f}",
        }


def compare_distributions(
    observed_probs: Sequence[float],
    expected_probs: Sequence[float],
    counts: Optional[Dict[str, int]] = None,
    num_qubits: Optional[int] = None,
) -> DistributionComparison:
    """Compare an observed distribution with the expected one."""

    obs = _as_array(observed_probs)
    exp = _as_array(expected_probs)

    result = DistributionComparison(
        tvd=total_variation_distance(obs, exp),
        js_distance=jensen_shannon_distance(obs, exp),
        kl_pq=kl_divergence(obs, exp),
        hellinger=hellinger_distance(obs, exp),
    )

    if counts is not None and num_qubits is not None:
        chi2, dof, p_value = chi_square_gof(counts, exp, num_qubits)
        result.chi2 = chi2
        result.chi2_dof = dof
        result.chi2_p_value = p_value

    return result
