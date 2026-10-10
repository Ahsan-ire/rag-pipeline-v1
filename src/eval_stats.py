"""Paired-comparison statistics for the eval instrument (Phase 16A-1, item 4; D67).

Stdlib only, data-free: every function takes plain counts or booleans, never eval
rows. The building blocks are consumed by 16A-2 (verdicts and K_max are out of
scope here).

Vocabulary used throughout
--------------------------
* A **family** is one underlying question asked in several **phrasings**
  (paraphrases). Three paraphrases count once: ``collapse_family`` turns a
  family's per-phrasing outcomes into ONE outcome.
* A **paired comparison** runs a *baseline* and a *candidate* on the same items.
  ``b`` counts items the candidate gets right and the baseline wrong (a gain for
  the candidate); ``c`` counts items the baseline gets right and the candidate
  wrong (a loss). Concordant items (both right or both wrong) make up the rest.
* ``UNAVAILABLE`` is returned (never raised) when a statistic is undefined for
  the data given, e.g. a Wilson interval on zero trials.

Numerical approach
------------------
Binomial tail sums for the exact McNemar test are computed with integers and
compared to ``alpha`` as exact ``Fraction`` values, so a rejection decision never
depends on float rounding. Power sums are floats (``math.comb`` times powers),
accurate to well beyond the 7 decimals the spec quotes. The MDE grid iterates
integer hundredths, so grid points are exactly ``k / 100``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Literal, Mapping, Optional, Sequence, Union

UNAVAILABLE = "unavailable"

WILSON_Z = 1.96

CollapseRule = Literal["all", "majority"]
Sided = Literal["two", "directional"]

__all__ = [
    "UNAVAILABLE",
    "WILSON_Z",
    "DurkalskiResult",
    "collapse_family",
    "collapse_families",
    "family_cluster",
    "mcnemar_exact",
    "durkalski_chi2",
    "chi2_1df_sf",
    "wilson_interval",
    "conditional_power",
    "power",
    "mde",
    "holm",
    "flip_frequency",
    "family_flip_frequencies",
]


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _check_count(name: str, value: int) -> None:
    """Raise ValueError unless ``value`` is a non-negative int (bool refused)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


def _check_alpha(alpha: float) -> None:
    """Raise ValueError unless 0 < alpha < 1."""
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")


def _check_sided(sided: str) -> None:
    """Raise ValueError unless ``sided`` is ``"two"`` or ``"directional"``."""
    if sided not in ("two", "directional"):
        raise ValueError(f"sided must be 'two' or 'directional', got {sided!r}")


def _check_probability(name: str, value: float) -> None:
    """Raise ValueError unless 0 <= value <= 1."""
    if isinstance(value, bool) or not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be in [0, 1], got {value!r}")


# --------------------------------------------------------------------------- #
# Family collapse
# --------------------------------------------------------------------------- #


def collapse_family(outcomes: Sequence[bool], rule: CollapseRule) -> bool:
    """Collapse one family's per-phrasing outcomes into a single outcome.

    Args:
        outcomes: one bool per phrasing (True = HIT / correct). Must be non-empty.
        rule: ``"all"`` — HIT only if every phrasing hits; ``"majority"`` — HIT
            only if strictly more than half the phrasings hit, so a tie (e.g.
            2 of 4) is a MISS.

    Returns:
        The family's single outcome; a family of three phrasings counts once.

    Raises:
        ValueError: empty ``outcomes``, a non-bool entry, or an unknown rule.
    """
    if rule not in ("all", "majority"):
        raise ValueError(f"rule must be 'all' or 'majority', got {rule!r}")
    if len(outcomes) == 0:
        raise ValueError("a family needs at least one phrasing outcome")
    for o in outcomes:
        if not isinstance(o, bool):
            raise ValueError(f"phrasing outcomes must be bool, got {o!r}")
    hits = sum(outcomes)
    if rule == "all":
        return hits == len(outcomes)
    return 2 * hits > len(outcomes)


def collapse_families(
    families: Mapping[str, Sequence[bool]], rule: CollapseRule
) -> dict[str, bool]:
    """Apply ``collapse_family`` to every family: family id -> one outcome."""
    return {fid: collapse_family(outs, rule) for fid, outs in families.items()}


def family_cluster(
    pairs: Sequence[tuple[bool, bool]],
) -> tuple[int, int, int]:
    """Build one Durkalski cluster ``(m, b, c)`` from a family's paired phrasings.

    Args:
        pairs: one ``(baseline_ok, candidate_ok)`` per phrasing. Non-empty.

    Returns:
        ``(m, b, c)``: m = number of phrasings (concordant included), b =
        phrasings where only the candidate is right, c = phrasings where only
        the baseline is right.

    Raises:
        ValueError: empty ``pairs`` or non-bool entries.
    """
    if len(pairs) == 0:
        raise ValueError("a family needs at least one phrasing pair")
    b = c = 0
    for pair in pairs:
        if len(pair) != 2 or not all(isinstance(x, bool) for x in pair):
            raise ValueError(f"each pair must be (bool, bool), got {pair!r}")
        base_ok, cand_ok = pair
        if cand_ok and not base_ok:
            b += 1
        elif base_ok and not cand_ok:
            c += 1
    return (len(pairs), b, c)


# --------------------------------------------------------------------------- #
# McNemar tests
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=None)
def _exact_p_fraction(b: int, c: int) -> Fraction:
    """Exact two-sided McNemar p-value as a Fraction (internal, cached).

    p = min(1, 2 * P(X <= min(b, c))), X ~ Binomial(b + c, 1/2). b + c = 0
    gives 1 (no discordant pairs: no evidence either way).
    """
    n = b + c
    if n == 0:
        return Fraction(1)
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(Fraction(1), Fraction(2 * tail, 2**n))


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar test (binomial test of b vs c at p = 1/2).

    Args:
        b: discordant pairs favouring the candidate (candidate right, baseline wrong).
        c: discordant pairs favouring the baseline.

    Returns:
        ``min(1, 2 * P(X <= min(b, c)))`` with X ~ Binomial(b + c, 1/2); 1.0
        when b + c = 0. E.g. b = 10, c = 2 -> 0.03857421875.

    Raises:
        ValueError: negative or non-int counts.
    """
    _check_count("b", b)
    _check_count("c", c)
    return float(_exact_p_fraction(b, c))


def chi2_1df_sf(x: float) -> float:
    """Survival function of the chi-squared distribution with 1 df: erfc(sqrt(x/2))."""
    if x < 0:
        raise ValueError(f"chi-squared statistic must be >= 0, got {x!r}")
    return math.erfc(math.sqrt(x / 2.0))


@dataclass(frozen=True)
class DurkalskiResult:
    """Result of Durkalski's clustered McNemar test.

    Attributes:
        chi2: the statistic (sum d_k)^2 / sum d_k^2, 1 df.
        p_value: chi-squared(1 df) upper tail of ``chi2``.
        d: the per-cluster d_k = (b_k - c_k) / m_k, in input order.
    """

    chi2: float
    p_value: float
    d: tuple[float, ...]


def durkalski_chi2(
    clusters: Sequence[tuple[int, int, int]],
) -> Union[DurkalskiResult, str]:
    """Durkalski's clustered McNemar chi-squared (Durkalski et al. 2003).

    Matches htestClust ``mcnemartestClust``. Each cluster (family) k is given as
    ``(m_k, b_k, c_k)``: m_k = the family's phrasings, concordant included;
    b_k / c_k = its discordant phrasings favouring candidate / baseline. Then
    d_k = (b_k - c_k) / m_k and chi2 = (sum d_k)^2 / sum d_k^2 on 1 df.

    With every m_k = 1 (singletons) this is the *uncorrected asymptotic*
    McNemar (b - c)^2 / (b + c) — not the exact test: b = 10, c = 2 gives
    chi2 = 5.3333, p = 0.0209 vs the exact 0.0386.

    Args:
        clusters: sequence of ``(m, b, c)`` int tuples.

    Returns:
        A ``DurkalskiResult``, or ``UNAVAILABLE`` when sum d_k^2 = 0 (no
        clusters, or every cluster's discordance cancels within itself).

    Raises:
        ValueError: m < 1, negative b or c, or b + c > m.
    """
    d_values: list[Fraction] = []
    for cluster in clusters:
        if len(cluster) != 3:
            raise ValueError(f"cluster must be (m, b, c), got {cluster!r}")
        m, b, c = cluster
        _check_count("m", m)
        _check_count("b", b)
        _check_count("c", c)
        if m < 1:
            raise ValueError(f"cluster size m must be >= 1, got {m}")
        if b + c > m:
            raise ValueError(f"b + c ({b + c}) exceeds cluster size m ({m})")
        d_values.append(Fraction(b - c, m))
    sum_sq = sum((d * d for d in d_values), Fraction(0))
    if sum_sq == 0:
        return UNAVAILABLE
    total = sum(d_values, Fraction(0))
    chi2 = float(total * total / sum_sq)
    return DurkalskiResult(
        chi2=chi2, p_value=chi2_1df_sf(chi2), d=tuple(float(d) for d in d_values)
    )


# --------------------------------------------------------------------------- #
# Wilson interval
# --------------------------------------------------------------------------- #


def wilson_interval(
    successes: int, n: int, z: float = WILSON_Z
) -> Union[tuple[float, float], str]:
    """Wilson score interval for a binomial proportion (default z = 1.96).

    Returns ``(low, high)`` clamped to [0, 1], with the bound exactly 0 when
    successes = 0 and exactly 1 when successes = n (e.g. 0/10 -> (0, 0.27754),
    10/10 -> (0.72246, 1)); ``UNAVAILABLE`` when n = 0.

    Raises:
        ValueError: negative counts or successes > n.
    """
    _check_count("successes", successes)
    _check_count("n", n)
    if successes > n:
        raise ValueError(f"successes ({successes}) exceeds n ({n})")
    if n == 0:
        return UNAVAILABLE
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return (low, high)


# --------------------------------------------------------------------------- #
# Power and MDE
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=None)
def _rejection_region(d: int, alpha: float, sided: str) -> tuple[int, ...]:
    """Values of b (0..d) where the exact two-sided McNemar test rejects at alpha.

    ``sided="two"`` keeps every rejection; ``"directional"`` keeps only those
    with b > d - b (the candidate wins). Comparison is exact (Fraction(alpha)
    is the exact value of the float alpha).
    """
    a = Fraction(alpha)
    region = []
    for b in range(d + 1):
        c = d - b
        if _exact_p_fraction(b, c) <= a and (sided == "two" or b > c):
            region.append(b)
    return tuple(region)


def _binom_pmf(n: int, k: int, p: float) -> float:
    """Binomial(n, p) probability mass at k (0**0 == 1 handles p in {0, 1})."""
    return math.comb(n, k) * (p**k) * ((1.0 - p) ** (n - k))


def conditional_power(d: int, gain: float, alpha: float, sided: Sided) -> float:
    """Power of the exact McNemar test given exactly ``d`` discordant pairs.

    Each discordant pair favours the candidate with probability ``gain``, so
    b ~ Binomial(d, gain). Power = sum of that mass over the b where the exact
    two-sided p <= alpha (``"two"``), additionally requiring b > d - b
    (``"directional"``). The rejection region is discrete, so power is NOT
    monotone in d: at gain 0.8, alpha 0.05, d = 7 gives 0.209728 (two) /
    0.2097152 (directional) but d = 8 only 0.16777472 / 0.16777216.

    Raises:
        ValueError: negative d, gain outside [0, 1], bad alpha or sided.
    """
    _check_count("d", d)
    _check_probability("gain", gain)
    _check_alpha(alpha)
    _check_sided(sided)
    return math.fsum(_binom_pmf(d, b, gain) for b in _rejection_region(d, alpha, sided))


def power(
    N: int, discordance: float, effect: float, alpha: float, sided: Sided
) -> float:
    """Exact (unconditional) power of the exact two-sided McNemar test.

    Model: each of ``N`` paired items is independently discordant with
    probability ``discordance`` = p10 + p01, and ``effect`` = p10 - p01 (p10 =
    candidate right / baseline wrong). Given a discordant pair, the candidate
    wins with probability g = (discordance + effect) / (2 * discordance). Power
    = sum over d of Binomial(N, discordance)(d) * ``conditional_power(d, g)``.

    ``sided="two"`` counts every two-sided rejection; ``"directional"`` only
    rejections favouring the candidate (b > c). Example: N = 50, effect 0.10,
    discordance 0.2 -> 0.2411886 (two) / 0.2411428 (directional).

    Args:
        N: number of paired items (families after collapse), >= 0.
        discordance: p10 + p01, in [0, 1].
        effect: p10 - p01; |effect| > discordance is impossible and refused.
        alpha: two-sided significance level in (0, 1).
        sided: ``"two"`` or ``"directional"``.

    Returns:
        Power in [0, 1]. discordance = 0 (hence effect = 0) gives 0.0: with no
        discordant pairs the test never rejects.

    Raises:
        ValueError: on any invalid argument.
    """
    _check_count("N", N)
    _check_probability("discordance", discordance)
    _check_alpha(alpha)
    _check_sided(sided)
    if isinstance(effect, bool) or abs(effect) > discordance:
        raise ValueError(
            f"|effect| ({effect!r}) cannot exceed discordance ({discordance!r})"
        )
    if discordance == 0:
        return 0.0
    gain = (discordance + effect) / (2.0 * discordance)
    gain = min(1.0, max(0.0, gain))
    return math.fsum(
        _binom_pmf(N, d, discordance) * conditional_power(d, gain, alpha, sided)
        for d in range(N + 1)
    )


def mde(
    N: int, discordance: float, alpha: float, target: float = 0.8
) -> Optional[float]:
    """Minimum detectable effect: smallest grid effect with directional power >= target.

    Scans effect = k / 100 for k = 1, 2, ... up to ``discordance`` (inclusive;
    integer hundredths, so grid points are exact) and returns the first effect
    whose ``power(..., sided="directional")`` reaches ``target``. Returns
    ``None`` if none does. Because the exact test is discrete, MDE is not
    monotone in N. Example (N = 36, alpha = 0.05): discordance 0.2 / 0.3 / 0.5
    -> None / 0.26 / 0.34.

    Raises:
        ValueError: bad N, discordance, alpha, or target outside (0, 1].
    """
    _check_count("N", N)
    _check_probability("discordance", discordance)
    _check_alpha(alpha)
    if not (0.0 < target <= 1.0):
        raise ValueError(f"target must be in (0, 1], got {target!r}")
    k = 1
    while k <= discordance * 100 + 1e-9:
        effect = min(k / 100, discordance)
        if power(N, discordance, effect, alpha, "directional") >= target:
            return k / 100
        k += 1
    return None


# --------------------------------------------------------------------------- #
# Multiplicity
# --------------------------------------------------------------------------- #


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, returned in input order.

    Sorted ascending, the i-th (0-based) of m p-values is multiplied by
    (m - i); a running maximum enforces monotonicity and values cap at 1.
    E.g. (0.01, 0.04, 0.03, 0.005) -> (0.03, 0.06, 0.06, 0.02). Empty -> [].

    Raises:
        ValueError: a p-value outside [0, 1].
    """
    for p in p_values:
        _check_probability("p-value", p)
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[idx]))
        adjusted[idx] = running
    return adjusted


# --------------------------------------------------------------------------- #
# Per-family flip frequency
# --------------------------------------------------------------------------- #


def flip_frequency(baseline: bool, draws: Sequence[bool]) -> Union[float, str]:
    """Fraction of repeated draws whose outcome differs from a baseline outcome.

    Used for draw (in)stability of one family: ``baseline`` is the family's
    reference (collapsed) outcome, ``draws`` its collapsed outcomes across
    repeated runs (e.g. different live expansion draws). Returns a value in
    [0, 1], or ``UNAVAILABLE`` when there are no draws.

    Raises:
        ValueError: non-bool baseline or draw.
    """
    if not isinstance(baseline, bool):
        raise ValueError(f"baseline must be bool, got {baseline!r}")
    for x in draws:
        if not isinstance(x, bool):
            raise ValueError(f"draw outcomes must be bool, got {x!r}")
    if len(draws) == 0:
        return UNAVAILABLE
    return sum(1 for x in draws if x != baseline) / len(draws)


def family_flip_frequencies(
    baseline: Mapping[str, bool], draws: Mapping[str, Sequence[bool]]
) -> dict[str, Union[float, str]]:
    """``flip_frequency`` per family id; both mappings must have the same keys.

    Raises:
        ValueError: the key sets differ, or any value is invalid.
    """
    if set(baseline) != set(draws):
        missing = sorted(set(baseline) ^ set(draws))
        raise ValueError(f"baseline and draws cover different families: {missing}")
    return {fid: flip_frequency(baseline[fid], draws[fid]) for fid in sorted(baseline)}
