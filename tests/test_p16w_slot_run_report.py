"""Phase 16A-2a a5: report v6, the judge and the sealed rendering (acceptance (g), the rendering parts).

The rendering is a pure function of the stored results document, so most tests
here take the document a real (faked) slot run stored and render it, or edit a
copy of it, with no further run.
"""

import copy
import re

import pytest

from src import eval_stats, eval_v6, sealed_report
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    _langsmith_env_cache,
    bare,
    world,
)


@pytest.fixture
def stored(world):
    world.run(0)
    return world.stored_doc(0)


def render(doc, excluded=frozenset(), canonical=True, failed=()):
    return sealed_report.format_sealed_report(doc, excluded=frozenset(excluded), canonical=canonical, failed_guards=failed)


# --- the sealed rendering --------------------------------------------------------------------


def test_the_rendering_has_no_ids_no_text_and_no_per_family_or_per_chapter_line(world, stored):
    text = render(stored)
    assert not [i for i in world.ids() if i in text]
    assert not [s for s in world.canaries.secrets() if s in text]
    assert not re.search(r"\b9\d\d\.\d", text) and "800." not in text  # no section label anywhere
    assert "family=" not in text and "evidence=" not in text and "[answer]" not in text
    # every body line is a heading, a blank, a table row or a bullet of aggregates
    for line in text.splitlines():
        assert not line or line.startswith(("#", "-", "|", "†")), line


def test_the_rendering_shows_scope_register_and_source_aggregates(stored):
    text = render(stored)
    for heading in ("### By scope", "### By register", "### By source (tutorial and Claude-drafted kept apart)"):
        assert heading in text
    rows = {m.group(1): m for m in re.finditer(r"^\| (\w+(?: \(source: handbook\) †)?) \| (\d+) \| (\d+) \|", text, re.M)}
    assert {"answer", "partial", "refuse", "modelled", "lay", "terse", "tutorial", "colleague"} <= set(rows)
    assert int(rows["answer"].group(2)) == 10 and int(rows["answer"].group(3)) == 5  # 5 families x 2 phrasings
    assert int(rows["refuse"].group(2)) == 4 and int(rows["partial"].group(3)) == 1


def test_tutorial_and_claude_drafted_rows_are_kept_apart_and_the_drafted_part_carries_the_d46_caveat(stored):
    text = render(stored)
    tutorial = re.search(r"^\| tutorial \| (\d+) \| (\d+) \|", text, re.M)
    drafted = re.search(r"^\| Claude-drafted \(source: handbook\) † \| (\d+) \| (\d+) \|", text, re.M)
    assert tutorial and drafted and tutorial.group(1) == "6" and drafted.group(1) == "8"
    caveat = sealed_report.D46_CAVEAT
    assert caveat in text and text.index(caveat) > text.index(drafted.group(0))
    assert "D46 caveat" in text and "self-retrieval" in text
    # Without any drafted row the caveat has nothing to attach to.
    doc = copy.deepcopy(stored)
    for row in next(s for s in doc["sets"] if s["role"] == "sealed")["rows"]:
        if row["source"] == "handbook":
            row["source"] = "tutorial"
    other = render(doc)
    assert caveat not in other and "Claude-drafted (source" not in other


def test_the_group_count_is_reported_beside_the_floor(world, stored):
    text = render(stored)
    assert "eligible answerable families (answer or partial, not excluded): 6 (floor 4)" in text
    assert "sealed families after exclusion: 8 (floor 6)" in text
    assert "groups: 6 analysed (4 with at least one eligible family)" in text


def test_the_judge_renders_the_unsupported_rate_with_a_wilson_interval(stored):
    text = render(stored)
    sealed_judge = text[text.index("## Judge (sealed"):text.index("## Inference")]
    assert "answers judged: 12 (api errors 0, parse errors 0, zero-claim 0)" in sealed_judge
    assert re.search(r"unsupported claims: \d+/24 = 0\.\d{3} \(95% Wilson CI 0\.\d{3}-0\.\d{3}\)", sealed_judge)
    assert re.search(r"answers with at least one unsupported claim: \d+/12 = ", sealed_judge)
    # only answer and partial rows were judged (12 of 16 sealed rows; the 4 refusal rows were not)
    rows = next(s for s in stored["sets"] if s["role"] == "sealed")["rows"]
    assert sum(r["judge"] is not None for r in rows) == 12
    assert all(r["judge"] is None for r in rows if r["scope"] == "refuse")


def test_slot_zero_is_descriptive_with_wilson_intervals_and_no_test(stored):
    text = render(stored)
    inference = text[text.index("## Inference"):text.index("## Development sets")]
    assert "descriptive baseline" in inference and "Wilson" in inference
    assert "sign-flip" not in inference and "p =" not in inference


def test_development_sets_are_counts_and_rates_only(stored):
    text = render(stored)
    dev = text[text.index("## Development sets"):text.index("## Expansion")]
    assert "### tuning" in dev and "### realistic" in dev
    assert "rows 4; families 4; retrieval-eligible families 3" in dev
    assert "path" not in dev.lower() and "f2000000" not in dev and "f3000000" not in dev


def test_an_excluded_group_is_dropped_from_every_aggregate_of_the_sealed_set(world, stored):
    group = world.families[0]  # {0, 1}: four rows, two eligible families (families 0 and 1)
    text = render(stored, {group})
    assert "rows: 12 analysed of 16; families: 6 analysed of 8" in text
    assert "excluded groups: 1 of 6 (2 families dropped from analysis; their rows still ran)" in text
    assert "eligible answerable families (answer or partial, not excluded): 4 (floor 4)" in text
    assert "sealed families after exclusion: 6 (floor 6)" in text
    assert re.search(r"^\| answer \| 6 \| 3 \|", text, re.M)  # 3 answer families left x 2 phrasings
    assert "answers judged: 8 " in text  # 4 families x 2 phrasings of the 6 judged answer/partial families left
    # the stored document is whole: the excluded rows are still in it (their rows ran)
    assert len(next(s for s in stored["sets"] if s["role"] == "sealed")["rows"]) == 16


def test_the_rendering_is_a_pure_function_of_the_stored_document(stored):
    assert render(stored) == render(copy.deepcopy(stored))
    assert render(stored, {"f10000004"}) == render(stored, ["f10000004"])
    assert stored["created"] in render(stored)  # the stored creation time, never the clock


def test_a_non_canonical_rendering_names_the_guards_it_missed(stored):
    text = render(stored, canonical=False, failed=("top_k_6", "judge_clean"))
    assert text.splitlines()[0].endswith("not canonical)") and "Canonical run: False" in text
    assert "Guards not met: top_k_6, judge_clean" in text


# --- confirmatory slots ----------------------------------------------------------------------


def confirmatory(stored, candidate_hits, base_hits):
    """A copy of the stored document made a confirmatory slot with the given family-level outcomes."""
    doc = copy.deepcopy(stored)
    doc["slot"] = "1"
    sealed = next(s for s in doc["sets"] if s["role"] == "sealed")
    for row in sealed["rows"]:
        if row["scope"] != "refuse":
            cell = row["retrieval"][doc["primary_mode"]]
            cell["strict_hit"] = candidate_hits[row["family_id"]]
    doc["baseline"] = {"run_id": "r0", "results_digest": "0" * 64, "hits": dict(base_hits)}
    return doc


def test_the_primary_statistic_is_the_cluster_signflip_test_with_labelled_sensitivities(world, stored):
    fams = world.families
    base = {f: True for f in fams[:6]}  # the six eligible families all hit at baseline
    cand = dict(base)
    cand[fams[0]] = False  # group {0,1}: -1
    cand[fams[2]] = False  # group {2,3}: -1
    cand[fams[4]] = False  # singleton: -1
    doc = confirmatory(stored, cand, base)
    text = render(doc, canonical=False)
    inference = text[text.index("## Inference"):text.index("## Development sets")]
    stats = sealed_report.confirmatory_stats(cand, base, doc["groups"])
    assert stats["gains"] == 0 and stats["losses"] == 3 and stats["net"] == -3
    assert stats["p_signflip"] == pytest.approx(eval_stats.cluster_signflip_p([-1, -1, -1, 0]))  # = 0.25
    assert "**Primary test**: exact two-sided cluster sign-flip test on group net discordance: p = 0.2500" in inference
    assert "3 of 4 groups discordant; net -3 families" in inference
    assert "Sensitivity (labelled, not the test): Durkalski clustered McNemar" in inference
    assert "Sensitivity (labelled, not the test): exact two-sided McNemar on families: b = 0, c = 3" in inference
    assert inference.index("Primary test") < inference.index("Sensitivity")
    assert "baseline 6/6 -> candidate 3/6 (effect -0.500)" in inference


@pytest.mark.parametrize("b,c", [(7, 1), (3, 3), (0, 4), (5, 0), (0, 0)])
def test_with_singleton_groups_the_primary_test_equals_the_exact_mcnemar_test(b, c):
    fams = [f"f{i:08x}" for i in range(10)]
    groups = {f: f for f in fams}
    base, cand = {}, {}
    for i, fam in enumerate(fams):
        gain, loss = i < b, b <= i < b + c
        base[fam] = loss or (not gain and i % 2 == 0)  # a gain: base miss, candidate hit; a loss: the reverse
        cand[fam] = gain or (not loss and base[fam])
    stats = sealed_report.confirmatory_stats(cand, base, groups)
    assert (stats["gains"], stats["losses"]) == (b, c)
    assert stats["p_signflip"] == pytest.approx(eval_stats.mcnemar_exact(b, c))
    if (b, c) == (7, 1):
        assert stats["p_signflip"] == pytest.approx(0.0703125)


def test_a_group_where_one_family_gains_and_another_loses_contributes_nothing():
    groups = {"fa": "g", "fb": "g", "fc": "h"}
    stats = sealed_report.confirmatory_stats(
        {"fa": True, "fb": False, "fc": True}, {"fa": False, "fb": True, "fc": True}, groups
    )
    assert (stats["gains"], stats["losses"], stats["net"]) == (1, 1, 0)
    assert stats["groups"] == 2 and stats["groups_discordant"] == 0 and stats["p_signflip"] == 1.0


def test_a_baseline_that_covers_other_families_or_a_family_without_a_group_is_refused():
    with pytest.raises(ValueError, match="different families"):
        sealed_report.confirmatory_stats({"fa": True}, {"fb": True}, {"fa": "g", "fb": "g"})
    with pytest.raises(ValueError, match="no group"):
        sealed_report.confirmatory_stats({"fa": True}, {"fa": True}, {})


def test_a_confirmatory_slot_without_a_stored_baseline_says_so(stored):
    doc = copy.deepcopy(stored)
    doc["slot"] = "2"
    doc["baseline"] = None
    assert "carries no baseline" in render(doc, canonical=False)


def test_exclusions_drop_the_group_from_both_arms_of_the_pairing(world, stored):
    fams = world.families
    base = {f: True for f in fams[:6]}
    cand = dict(base)
    cand[fams[0]] = False  # in the excluded group below
    cand[fams[4]] = False
    doc = confirmatory(stored, cand, base)
    text = render(doc, {fams[0]}, canonical=False)  # group {0,1} excluded
    assert "net -1 families" in text and "baseline 4/4 -> candidate 3/4" in text


# --- the judge in report v6 ------------------------------------------------------------------


def j(claims, unsupported, ok=True, error=None):
    return {"ok": ok, "error_type": error, "n_claims": claims, "supported": claims - unsupported,
            "unsupported": unsupported, "unclear": 0}


def test_judge_summary_counts_claims_and_answers_with_wilson_intervals():
    block = eval_v6.judge_summary([j(4, 1), j(2, 0), j(0, 0), j(3, 3)])
    assert block["attempted"] == 4 and block["zero_claim"] == 1 and not block["suppressed"]
    assert (block["unsupported_claims"]["hits"], block["unsupported_claims"]["n"]) == (4, 9)
    assert (block["answers_with_unsupported"]["hits"], block["answers_with_unsupported"]["n"]) == (2, 3)
    assert block["unsupported_claims"]["ci"] == pytest.approx(list(eval_stats.wilson_interval(4, 9)))
    lines = "\n".join(eval_v6.judge_lines(block))
    assert "unsupported claims: 4/9 = 0.444 (95% Wilson CI" in lines


def test_judge_summary_suppresses_the_rates_when_the_judge_mostly_failed():
    block = eval_v6.judge_summary([j(0, 0, ok=False, error="api"), j(0, 0, ok=False, error="parse"), j(2, 1)])
    assert block["suppressed"] and (block["api_errors"], block["parse_errors"]) == (1, 1)
    lines = "\n".join(eval_v6.judge_lines(block))
    assert "suppressed" in lines and "unsupported claims:" not in lines


def test_judge_summary_of_nothing_is_empty_not_a_crash():
    block = eval_v6.judge_summary([])
    assert block["attempted"] == 0 and not block["suppressed"] and block["unsupported_claims"]["ci"] is None
    assert "n/a (0 claims)" in "\n".join(eval_v6.judge_lines(block))
