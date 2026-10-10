"""Phase 16A-1 item 4 / acceptance (j): src/eval_stats.py on invented numbers only.

Every (j) number from IMPLEMENTATION_PLAN.md Phase 16A has its own test, at a
tolerance matching the decimals the spec quotes.
"""

import math

import pytest

from src.eval_stats import (
    UNAVAILABLE,
    DurkalskiResult,
    chi2_1df_sf,
    collapse_families,
    collapse_family,
    conditional_power,
    durkalski_chi2,
    family_cluster,
    family_flip_frequencies,
    flip_frequency,
    holm,
    mcnemar_exact,
    mde,
    power,
    wilson_interval,
)

# Twelve singleton clusters: ten gains, two losses (b = 10, c = 2).
SINGLETONS = [(1, 1, 0)] * 10 + [(1, 0, 1)] * 2
CLUSTERS = [(2, 2, 0), (2, 1, 1), (3, 3, 0), (2, 0, 1), (2, 1, 0)]


# ---- (j) McNemar / Durkalski ------------------------------------------------


def test_j_exact_mcnemar_b10_c2():
    assert mcnemar_exact(10, 2) == 0.03857421875  # 158 / 4096, exact


def test_j_singleton_durkalski_is_uncorrected_asymptotic():
    r = durkalski_chi2(SINGLETONS)
    assert isinstance(r, DurkalskiResult)
    assert r.chi2 == pytest.approx(5.3333, abs=5e-5)
    assert r.chi2 == pytest.approx((10 - 2) ** 2 / 12, rel=1e-12)
    assert r.p_value == pytest.approx(0.0209213353, abs=5e-11)
    assert r.p_value != pytest.approx(mcnemar_exact(10, 2), abs=1e-3)


def test_j_clustered_durkalski_d_values():
    r = durkalski_chi2(CLUSTERS)
    assert r.d == (1.0, 0.0, 1.0, -0.5, 0.5)


def test_j_clustered_durkalski_chi2():
    assert durkalski_chi2(CLUSTERS).chi2 == pytest.approx(2**2 / 2.5, rel=1e-12)
    assert durkalski_chi2(CLUSTERS).chi2 == pytest.approx(1.6, rel=1e-12)


def test_j_clustered_durkalski_p():
    assert durkalski_chi2(CLUSTERS).p_value == pytest.approx(0.2059032107, abs=5e-11)


def test_j_durkalski_all_cancelling_unavailable():
    assert durkalski_chi2([(2, 1, 1), (4, 2, 2), (3, 0, 0)]) == UNAVAILABLE


def test_durkalski_no_clusters_unavailable():
    assert durkalski_chi2([]) == UNAVAILABLE


def test_chi2_sf_is_erfc():
    assert chi2_1df_sf(1.6) == math.erfc(math.sqrt(0.8))
    assert chi2_1df_sf(0.0) == 1.0


# ---- (j) power ----------------------------------------------------------------


def test_j_conditional_power_two_sided_d7():
    assert conditional_power(7, 0.8, 0.05, "two") == pytest.approx(0.209728, abs=1e-12)


def test_j_conditional_power_two_sided_d8():
    assert conditional_power(8, 0.8, 0.05, "two") == pytest.approx(0.16777472, abs=1e-12)


def test_j_conditional_power_directional_d7():
    assert conditional_power(7, 0.8, 0.05, "directional") == pytest.approx(
        0.2097152, abs=1e-12
    )


def test_j_conditional_power_directional_d8():
    assert conditional_power(8, 0.8, 0.05, "directional") == pytest.approx(
        0.16777216, abs=1e-12
    )


def test_j_power_not_monotone_in_d():
    """The (j) non-monotone example: one more discordant pair LOWERS power."""
    for sided in ("two", "directional"):
        assert conditional_power(7, 0.8, 0.05, sided) > conditional_power(
            8, 0.8, 0.05, sided
        )


def test_j_power_n50_disc02_two():
    assert power(50, 0.2, 0.10, 0.05, "two") == pytest.approx(0.2411886, abs=5e-8)


def test_j_power_n50_disc02_directional():
    assert power(50, 0.2, 0.10, 0.05, "directional") == pytest.approx(
        0.2411428, abs=5e-8
    )


def test_j_power_n50_disc08_two():
    assert power(50, 0.8, 0.10, 0.05, "two") == pytest.approx(0.0944530, abs=5e-8)


def test_j_power_n50_disc08_directional():
    assert power(50, 0.8, 0.10, 0.05, "directional") == pytest.approx(
        0.0925917, abs=5e-8
    )


def test_power_directional_never_exceeds_two_sided():
    for disc, eff in [(0.2, 0.1), (0.5, 0.0), (0.5, -0.3), (1.0, 0.6)]:
        assert power(30, disc, eff, 0.05, "directional") <= power(
            30, disc, eff, 0.05, "two"
        )


def test_power_zero_discordance_is_zero():
    assert power(40, 0.0, 0.0, 0.05, "two") == 0.0


def test_power_negative_effect_directional_is_tiny():
    # Candidate is worse: directional power (wins only) is near zero.
    assert power(50, 0.4, -0.3, 0.05, "directional") < 1e-6


# ---- (j) MDE ------------------------------------------------------------------


@pytest.mark.parametrize(
    "alpha, disc, expected",
    [
        (0.05, 0.2, None),
        (0.05, 0.3, 0.26),
        (0.05, 0.5, 0.34),
        (0.0125, 0.2, None),
        (0.0125, 0.3, 0.29),
        (0.0125, 0.5, 0.39),
    ],
)
def test_j_mde_n36(alpha, disc, expected):
    assert mde(36, disc, alpha, target=0.8) == expected


def test_mde_uses_directional_power():
    m = mde(36, 0.3, 0.05)
    assert power(36, 0.3, m, 0.05, "directional") >= 0.8
    assert power(36, 0.3, m - 0.01, 0.05, "directional") < 0.8


def test_mde_not_monotone_in_n():
    # Invented example: at discordance 1.0, adding a tenth pair RAISES the MDE.
    assert mde(9, 1.0, 0.05) == 0.82
    assert mde(10, 1.0, 0.05) == 0.84


def test_mde_none_when_n_zero():
    assert mde(0, 0.5, 0.05) is None


# ---- (j) Wilson ---------------------------------------------------------------


def test_j_wilson_0_of_10():
    low, high = wilson_interval(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.27754, abs=5e-6)


def test_j_wilson_10_of_10():
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.72246, abs=5e-6)
    assert high == 1.0


def test_j_wilson_empty_unavailable():
    assert wilson_interval(0, 0) == UNAVAILABLE


def test_wilson_interior_contains_point_estimate():
    low, high = wilson_interval(3, 10)
    assert low < 0.3 < high


# ---- (j) Holm -----------------------------------------------------------------


def test_j_holm():
    assert holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx(
        [0.03, 0.06, 0.06, 0.02], abs=1e-12
    )


def test_holm_caps_at_one_and_empty():
    assert holm([0.6, 0.9]) == [1.0, 1.0]
    assert holm([]) == []


# ---- (j) family collapse ------------------------------------------------------


def test_j_three_paraphrases_count_once():
    families = {"F1": [True, True, False], "F2": [True, True, True]}
    out = collapse_families(families, "majority")
    assert out == {"F1": True, "F2": True}
    assert len(out) == 2  # six phrasings, two outcomes


def test_collapse_all_rule():
    assert collapse_family([True, True, True], "all") is True
    assert collapse_family([True, True, False], "all") is False


def test_collapse_majority_tie_is_miss():
    assert collapse_family([True, False], "majority") is False
    assert collapse_family([True, True, False, False], "majority") is False
    assert collapse_family([True, True, False], "majority") is True


def test_family_cluster_counts_concordant_in_m():
    pairs = [(False, True), (True, True), (False, False), (True, False), (False, True)]
    assert family_cluster(pairs) == (5, 2, 1)


# ---- flip frequency -----------------------------------------------------------


def test_flip_frequency():
    assert flip_frequency(True, [True, False, False, True]) == 0.5
    assert flip_frequency(False, [False, False]) == 0.0


def test_flip_frequency_no_draws_unavailable():
    assert flip_frequency(True, []) == UNAVAILABLE


def test_family_flip_frequencies():
    out = family_flip_frequencies(
        {"A": True, "B": False}, {"A": [True, False], "B": []}
    )
    assert out == {"A": 0.5, "B": UNAVAILABLE}


def test_family_flip_frequencies_key_mismatch():
    with pytest.raises(ValueError):
        family_flip_frequencies({"A": True}, {"B": [True]})


# ---- validation ---------------------------------------------------------------


@pytest.mark.parametrize("b, c", [(-1, 2), (2, -1)])
def test_mcnemar_negative_counts(b, c):
    with pytest.raises(ValueError):
        mcnemar_exact(b, c)


def test_mcnemar_no_discordance_is_one():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0


@pytest.mark.parametrize(
    "cluster", [(2, 2, 1), (1, -1, 0), (2, 0, -1), (0, 0, 0), (-1, 0, 0), (2, 1)]
)
def test_durkalski_invalid_cluster(cluster):
    with pytest.raises(ValueError):
        durkalski_chi2([cluster])


@pytest.mark.parametrize("effect", [0.31, -0.31])
def test_power_effect_exceeds_discordance(effect):
    with pytest.raises(ValueError):
        power(36, 0.3, effect, 0.05, "two")


@pytest.mark.parametrize("sided", ["one", "greater", "", None])
def test_bad_sided(sided):
    with pytest.raises(ValueError):
        power(36, 0.3, 0.1, 0.05, sided)
    with pytest.raises(ValueError):
        conditional_power(7, 0.8, 0.05, sided)


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1])
def test_bad_alpha(alpha):
    with pytest.raises(ValueError):
        power(36, 0.3, 0.1, alpha, "two")


def test_power_invalid_n_and_discordance():
    with pytest.raises(ValueError):
        power(-1, 0.3, 0.1, 0.05, "two")
    with pytest.raises(ValueError):
        power(36, 1.2, 0.1, 0.05, "two")


def test_mde_bad_target():
    with pytest.raises(ValueError):
        mde(36, 0.3, 0.05, target=0.0)


def test_wilson_invalid():
    with pytest.raises(ValueError):
        wilson_interval(-1, 10)
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_holm_invalid():
    with pytest.raises(ValueError):
        holm([0.5, 1.2])


def test_collapse_invalid():
    with pytest.raises(ValueError):
        collapse_family([], "all")
    with pytest.raises(ValueError):
        collapse_family([True], "any")
    with pytest.raises(ValueError):
        collapse_family([1, 0], "all")
    with pytest.raises(ValueError):
        family_cluster([])
