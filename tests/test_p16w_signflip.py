"""Phase 16A-2a item a8 / acceptance (j): the exact cluster sign-flip test.

``cluster_signflip_p`` and ``cluster_signflip_power`` in src/eval_stats.py, on
invented numbers only. Every test is a (j) clause from IMPLEMENTATION_PLAN.md
Phase 16A-2a; the Monte Carlo tests use fixed seeds and small draw counts so the
whole file stays well under two seconds.
"""

import itertools
import math
import random

import pytest

from src.eval_stats import (
    cluster_signflip_p,
    cluster_signflip_power,
    mcnemar_exact,
    power,
)


def _brute_force_p(d):
    """p by listing all 2^len(d) sign vectors (zero entries included)."""
    observed = abs(sum(d))
    hits = sum(
        1
        for signs in itertools.product((-1, 1), repeat=len(d))
        if abs(sum(s * abs(x) for s, x in zip(signs, d))) >= observed
    )
    return hits / 2 ** len(d)


# ---- cluster_signflip_p: exactness -------------------------------------------


def test_j_signflip_matches_brute_force_on_random_small_cases():
    rng = random.Random(20261012)
    for _ in range(300):
        n = rng.randint(0, 10)
        d = [rng.randint(-4, 4) for _ in range(n)]  # zeros and big groups included
        assert cluster_signflip_p(d) == _brute_force_p(d), d


def test_j_signflip_hand_computed_case():
    # |d| = (1, 2, 3): signed sums 6, 4, 2, 0, 0, -2, -4, -6; observed |4|.
    assert cluster_signflip_p([3, -1, 0, 2]) == 0.5


def test_j_signflip_equals_mcnemar_exact_for_singletons_all_b_plus_c_le_30():
    for total in range(31):
        for b in range(total + 1):
            c = total - b
            d = [1] * b + [-1] * c
            assert cluster_signflip_p(d) == mcnemar_exact(b, c), (b, c)


def test_j_signflip_b7_c1_is_0_0703125_in_any_order():
    d = [1, -1, 1, 1, 1, 1, 1, 1]
    assert cluster_signflip_p(d) == 0.0703125 == mcnemar_exact(7, 1)
    assert cluster_signflip_p(sorted(d)) == 0.0703125


def test_j_signflip_tie_is_one_not_above_one():
    # b == c: the McNemar min(1, .) convention. Here P(|S| >= 0) is exactly 1.
    assert cluster_signflip_p([1, 1, -1, -1]) == 1.0


def test_j_signflip_is_two_sided_and_symmetric_in_sign():
    d = [2, 1, 1, 3, 1, 2, 1]
    assert cluster_signflip_p(d) == cluster_signflip_p([-x for x in d])
    # A strong loss is as significant as the same strong gain.
    assert cluster_signflip_p([-1] * 9) == cluster_signflip_p([1] * 9) == 2 / 512


# ---- cluster_signflip_p: degenerate inputs -----------------------------------


def test_j_signflip_empty_and_all_zero_give_one():
    assert cluster_signflip_p([]) == 1.0
    assert cluster_signflip_p([0]) == 1.0
    assert cluster_signflip_p([0, 0, 0]) == 1.0


def test_j_signflip_zero_entries_contribute_nothing():
    base = [1, 1, 2, 1, 3, 1]
    assert cluster_signflip_p(base + [0, 0, 0]) == cluster_signflip_p(base)
    assert cluster_signflip_p([0] + base) == cluster_signflip_p(base)


def test_j_signflip_group_with_one_gain_and_one_loss_contributes_zero():
    # (gains, losses) per group; net d_g = gains - losses. The (1, 1) group is 0.
    groups = [(2, 0), (1, 0), (1, 1), (1, 0), (0, 0), (1, 0), (1, 0), (1, 0)]
    d = [g - l for g, l in groups]
    assert d[2] == 0 and d[4] == 0  # mixed group and refusal-only / empty group
    without = [x for i, x in enumerate(d) if i not in (2, 4)]
    assert cluster_signflip_p(d) == cluster_signflip_p(without)


def test_j_signflip_single_group_is_never_significant():
    assert cluster_signflip_p([7]) == 1.0
    assert cluster_signflip_p([-7]) == 1.0


def test_j_signflip_rejects_non_int_entries():
    for bad in ([1.0], [True], ["1"], [None], [1, 0.5]):
        with pytest.raises(ValueError):
            cluster_signflip_p(bad)


# ---- cluster_signflip_p: size (Codex r5 fixture) -----------------------------


def _rejection_probability(effect_per_group, groups=20, alpha=0.05):
    """P(reject at alpha) when every group is +x or -x with probability 1/2.

    Exact: the number of gain-type groups k is Binomial(groups, 1/2), so the
    2^groups type assignments collapse to groups + 1 distinct p-values.
    """
    reject = 0
    for k in range(groups + 1):
        d = [effect_per_group] * k + [-effect_per_group] * (groups - k)
        if cluster_signflip_p(d) <= alpha:
            reject += math.comb(groups, k)
    return reject / 2**groups


def test_j_signflip_codex_r5_fixture_size_at_most_alpha():
    # 20 two-family groups; gain (+1) and loss (-1) types equally likely; true
    # family-level effect 0. Exact rejection probability at alpha = 0.05.
    p_reject = _rejection_probability(1)
    assert 0.0 < p_reject <= 0.05
    # It is the exact McNemar size at n = 20: reject iff min(b, c) <= 5.
    assert p_reject == 2 * sum(math.comb(20, k) for k in range(6)) / 2**20


def test_j_signflip_codex_r5_fixture_fully_correlated_groups():
    # The same fixture with both families of a group moving together: d = +-2.
    assert 0.0 < _rejection_probability(2) <= 0.05


def test_j_signflip_codex_r5_fixture_by_full_enumeration():
    # Same fixture without the binomial shortcut: all 2^20 type assignments.
    # Cheap because the p-value depends on d only through the gain count.
    alpha = 0.05
    memo = {}
    reject = 0
    for mask in range(2**20):
        k = mask.bit_count()
        if k not in memo:
            memo[k] = cluster_signflip_p([1] * k + [-1] * (20 - k)) <= alpha
        reject += memo[k]
    assert reject / 2**20 <= alpha


# ---- cluster_signflip_power --------------------------------------------------


@pytest.mark.parametrize(
    "n, disc, eff, alpha",
    [(36, 0.3, 0.2, 0.05), (40, 0.3, 0.25, 0.05), (45, 0.5, 0.3, 0.0125)],
)
def test_j_signflip_power_independent_singletons_matches_power(n, disc, eff, alpha):
    draws = 4000
    exact = power(n, disc, eff, alpha, "directional")
    mc = cluster_signflip_power([1] * n, disc, eff, alpha, draws, 7, "independent")
    sigma = math.sqrt(exact * (1 - exact) / draws)
    assert abs(mc - exact) <= 5 * sigma, (mc, exact)


def test_j_signflip_power_models_coincide_for_singleton_groups():
    args = ([1] * 30, 0.4, 0.2, 0.05, 500, 3)
    assert cluster_signflip_power(*args, "independent") == cluster_signflip_power(
        *args, "correlated"
    )


def test_j_signflip_power_correlated_is_lower_for_pairs_at_this_setting():
    # Full within-group correlation inflates the variance of d_g, so power drops.
    args = ([2] * 20, 0.3, 0.15, 0.05, 2000, 1)
    ind = cluster_signflip_power(*args, "independent")
    cor = cluster_signflip_power(*args, "correlated")
    assert 0.0 < cor < ind < 1.0


def test_j_signflip_power_same_seed_same_result_and_seed_matters():
    args = ([1, 2, 2, 3] * 5, 0.3, 0.15, 0.05, 800)
    for model in ("independent", "correlated"):
        a = cluster_signflip_power(*args, 11, model)
        assert a == cluster_signflip_power(*args, 11, model)
    values = {cluster_signflip_power(*args, s, "independent") for s in range(5)}
    assert len(values) > 1  # different seeds are different samples


def test_j_signflip_power_does_not_touch_the_global_random_state():
    random.seed(123)
    expected = random.random()
    random.seed(123)
    cluster_signflip_power([1] * 10, 0.3, 0.2, 0.05, 50, 5, "independent")
    assert random.random() == expected


def test_j_signflip_power_certain_gain_is_one_and_certain_loss_is_zero():
    # discordance 1, effect +-1: every family gains (or loses) with certainty.
    sizes = [1, 2, 3, 2, 1, 2, 3, 1, 2, 2]
    for model in ("independent", "correlated"):
        assert cluster_signflip_power(sizes, 1.0, 1.0, 0.05, 20, 0, model) == 1.0
        assert cluster_signflip_power(sizes, 1.0, -1.0, 0.05, 20, 0, model) == 0.0


def test_j_signflip_power_degenerate_designs_are_zero():
    for model in ("independent", "correlated"):
        assert cluster_signflip_power([], 0.3, 0.1, 0.05, 10, 0, model) == 0.0
        assert cluster_signflip_power([2, 2], 0.0, 0.0, 0.05, 10, 0, model) == 0.0
        # One group can never reach p <= alpha < 1.
        assert cluster_signflip_power([5], 1.0, 1.0, 0.05, 10, 0, model) == 0.0


def test_j_signflip_power_null_effect_stays_within_alpha_plus_noise():
    draws = 4000
    mc = cluster_signflip_power([2] * 20, 0.3, 0.0, 0.05, draws, 9, "independent")
    # Directional rejections are about half the two-sided size (<= 0.025).
    assert mc <= 0.025 + 5 * math.sqrt(0.025 * 0.975 / draws)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"group_sizes": [1, 0, 2]},
        {"group_sizes": [1, -1]},
        {"group_sizes": [1, 2.0]},
        {"group_sizes": [True]},
        {"discordance": -0.1},
        {"discordance": 1.1},
        {"effect": 0.4},  # |effect| > discordance (0.3)
        {"effect": -0.4},
        {"alpha": 0.0},
        {"alpha": 1.0},
        {"draws": 0},
        {"draws": -5},
        {"draws": 10.0},
        {"seed": 1.5},
        {"seed": True},
        {"model": "pooled"},
        {"model": None},
    ],
)
def test_j_signflip_power_validates_inputs(kwargs):
    args = dict(
        group_sizes=[1, 2, 2],
        discordance=0.3,
        effect=0.1,
        alpha=0.05,
        draws=10,
        seed=0,
        model="independent",
    )
    args.update(kwargs)
    with pytest.raises(ValueError):
        cluster_signflip_power(**args)
