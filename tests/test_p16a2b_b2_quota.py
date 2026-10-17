"""16A-2b b2: the general feasibility quota rule (owner-approved 17 Oct 2026, with the dilution term).

For a development split with F preassigned families, R of them refusal families, and a registered minimum M, the smallest quota
that can still end inside the 25-30 % refusal band is ``max(M, F, ceil(4(F-R)/3), ceil(10R/3))`` (``sealed_tool.min_feasible_quota``).
The realistic quota is that at ``realistic_min``; tuning is the rest of the pool and must reach its own minimum feasible quota (the
same rule at ``tuning_min``) or the attempt is ``quota_short``; attempt 1's draft count uses that tuning floor; ``count-migrated``
also checks the pool's refusal target against the three splits' reachable refusal intervals; round 2 reuses round 1's quotas.

Synthetic fixtures only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import eval_split
from tests.p16w_sealed_world import b1a_blocks, quota_example, public_id, read_rows, repos, standard_released, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r2_tool import _migrated
from tests.test_p16a2b_b1a_r3_tool import REAL_QUOTA, put
from tests.test_p16w_sealed_tool import assert_allowlisted
from tests.test_p16w_sealed_tool_split import _prepare_twins, _round2_world, assign_payload

ROOT = Path(__file__).resolve().parent.parent
REAL_PINS = st.PINNED_VALUES  # captured at import, before a World replaces them with its own small quota's


def real_prereg() -> dict:
    return json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))


# --- the rule ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "families,refusals,want",
    [
        (49, 10, 52),  # the b2 stop: at 51 only two refusals fit (12/51 < 25 %), at 52 three do (13/52 exactly)
        (50, 10, 54),  # half-up rounding would say 53, whose three additions give 13/53 (< 25 %)
        (45, 15, 50),  # dilution: 15 refusals are over 30 % of 49 (14), so non-refusal families must be added; 15/50 = 30 % exactly
        (31, 8, 42),  # below the minimum: the minimum
        (0, 0, 42),
        (42, 11, 42),
        (47, 10, 50),
        (60, 0, 80),  # no refusals yet: 20 open slots must be refusals, so ceil(4 x 60 / 3) = 80
        (60, 18, 60),  # already 30 %: nothing to add
    ],
)
def test_the_minimum_feasible_quota_of_a_development_split(families, refusals, want):
    assert st.min_feasible_quota(42, families, refusals) == want
    assert st.DEV_QUOTA_RULE == "max(min,F,ceil(4(F-R)/3),ceil(10R/3))"


def test_the_quota_reaches_its_band_and_one_less_does_not():
    for families, refusals, quota in ((49, 10, 52), (50, 10, 54), (45, 15, 50)):
        lo, hi = st.refusal_interval(quota, families, refusals)
        assert lo <= hi
        lo1, hi1 = st.refusal_interval(quota - 1, families, refusals)
        assert lo1 > hi1  # one less quota has no refusal count that is in the band and reachable


def test_the_rule_equals_the_splitters_exact_integer_band_for_every_small_split():
    """The splitter's own band (``eval_split._band``: ``4r >= q`` and ``10r <= 3q``) decides: the rule's quota is the smallest Q >= max(M, F)
    with a refusal count in [max(R, lo), min(R + Q - F, hi)], for every M, F and R on a grid, including the tiny minimums of the test worlds."""
    assert (st.BAND_MIN_NUM, st.BAND_MIN_DEN) == (eval_split.BAND_MIN.numerator, eval_split.BAND_MIN.denominator)
    assert (st.BAND_MAX_NUM, st.BAND_MAX_DEN) == (eval_split.BAND_MAX.numerator, eval_split.BAND_MAX.denominator)
    for q in range(0, 400):
        assert st.band_bounds(q) == eval_split._band(q), q
    for minimum in (0, 4, 8, 42, 60):
        for families in range(0, 75):
            for refusals in range(0, families + 1):
                want = max(minimum, families)
                while True:
                    lo, hi = eval_split._band(want)
                    if max(refusals, lo) <= min(refusals + want - families, hi):
                        break
                    want += 1
                assert st.min_feasible_quota(minimum, families, refusals) == want, (minimum, families, refusals)


def test_a_quota_the_band_cannot_hold_is_skipped():
    """Q = 5, 6, 9 and 13 have no refusal count between 25 % (rounded up) and 30 % (rounded down): the closed form can land on
    them for tiny minimums (the test worlds'), and the rule steps past. It never binds at or above 14."""
    for q in (5, 6, 9, 13):
        lo, hi = st.band_bounds(q)
        assert lo > hi
    assert all(st.band_bounds(q)[0] <= st.band_bounds(q)[1] for q in range(14, 2000))
    assert st.min_feasible_quota(4, 4, 0) == 7  # ceil(16/3) = 6 has an empty band; 7 holds exactly 2 refusals
    assert st.min_feasible_quota(4, 5, 1) == 7 and st.min_feasible_quota(0, 1, 0) == 4 and st.min_feasible_quota(0, 9, 3) == 10


def test_the_rule_refuses_inputs_that_are_not_counts():
    for bad in ((-1, 5, 1), (42, -1, 0), (42, 5, -1), (42, 5, 6), (42.5, 5, 1), (42, True, 0), ("42", 5, 1), (42, 5, None)):
        with pytest.raises(ValueError):
            st.min_feasible_quota(*bad)


def test_the_reachable_refusal_intervals():
    assert st.refusal_interval(60) == (15, 18)  # sealed: nothing preassigned
    assert st.refusal_interval(52, 49, 10) == (13, 13)  # realistic at the b2 stop: three open slots, and all three must be refusals
    assert st.refusal_interval(68, 26, 5) == (17, 20)  # tuning: keeps its 5 refusals, 42 open slots, the ceiling is 20
    lo, hi = st.refusal_interval(30, 40, 0)  # more families than the quota
    assert lo > hi
    lo, hi = st.refusal_interval(42, 20, 14)  # 14 refusals are over 30 % of 42
    assert lo > hi


def test_the_realistic_quota_and_the_tuning_floor_use_the_rule_and_tuning_is_the_rest():
    assert st.compute_quotas(REAL_QUOTA, 180, 49, 10) == {"sealed": 60, "realistic": 52, "tuning": 68}
    assert st.compute_quotas(REAL_QUOTA, 180, 45, 15) == {"sealed": 60, "realistic": 50, "tuning": 70}
    assert st.compute_quotas(REAL_QUOTA, 180, 31, 8) == {"sealed": 60, "realistic": 42, "tuning": 78}  # below the minimum: the minimum
    assert st.tuning_floor(REAL_QUOTA, 26, 5) == 60 and st.tuning_floor(REAL_QUOTA, 50, 22) == 74 and st.tuning_floor(REAL_QUOTA, 70, 21) == 70


# --- the prereg ----------------------------------------------------------------------------------------------------------------------
def test_the_real_prereg_registers_the_rule_text_and_the_examples_are_recomputed_with_refusal_counts():
    doc = real_prereg()
    quota = doc["quota"]
    assert (quota["dev_quota_rule"], quota["realistic_rule"], quota["tuning_floor_rule"], quota["tuning_rule"]) == (
        "max(min,F,ceil(4(F-R)/3),ceil(10R/3))", "dev_quota_rule_at_realistic_min", "dev_quota_rule_at_tuning_min",
        "pool_total-sealed-realistic_quota")
    assert doc["drafting_rule"]["attempt_1"]["families_rule"] == (
        "max(families_approx,(sealed_quota+tuning_floor+realistic_quota)-migrated_families)+draft_margin")
    assert st.REGISTERED["drafting_rule.attempt_1.families_rule"] == doc["drafting_rule"]["attempt_1"]["families_rule"]
    for ex in quota["examples"]:
        assert set(ex) == {"pool_total", "preassigned_realistic", "preassigned_realistic_refusals", "preassigned_tuning",
                           "preassigned_tuning_refusals", "sealed", "realistic", "tuning", "label"}
        want = st.compute_quotas(quota, ex["pool_total"], ex["preassigned_realistic"], ex["preassigned_realistic_refusals"])
        assert (ex["sealed"], ex["realistic"], ex["tuning"]) == (want["sealed"], want["realistic"], want["tuning"])
        short = ex["tuning"] < st.tuning_floor(quota, ex["preassigned_tuning"], ex["preassigned_tuning_refusals"])
        assert ex["label"] == ("quota_short" if short else "ok")
    realistic = {(e["preassigned_realistic"], e["preassigned_realistic_refusals"]): e["realistic"] for e in quota["examples"]}
    assert realistic[(49, 10)] == 52 and realistic[(45, 15)] == 50 and realistic[(31, 8)] == 42  # the b2 stop, the dilution, below the minimum
    assert st.validate_prereg(doc) == [] and st.pinned_errors(doc) == [] and st.computed_errors(doc) == []
    assert doc["seeds"] == {"split_r1": 4721112936955565465, "split_r2": 2060714279786243518,
                            "repair": 4132900947249597239, "calibration": 1958881144304175148}  # unchanged
    assert doc["power"]["seed"] == 5722486815998357046 and len(doc["power"]["table"]) == 12


def test_a_tuning_quota_between_the_minimum_and_its_own_floor_is_quota_short():
    quota = real_prereg()["quota"]
    short = [e for e in quota["examples"] if e["tuning"] >= quota["tuning_min"] and e["label"] == "quota_short"]
    assert short and short[0]["tuning"] == 68 and st.tuning_floor(quota, short[0]["preassigned_tuning"], short[0]["preassigned_tuning_refusals"]) == 74


@pytest.mark.parametrize(
    "path,value",
    [
        ("quota.examples.3.preassigned_realistic_refusals", 9),  # (49, 10) -> (49, 9): the rule now says 54, not 52
        ("quota.examples.1.realistic", 47),  # the old max(realistic_min, F)
        ("quota.examples.3.realistic", 51),
        ("quota.examples.1.tuning", 71),
        ("quota.examples.0.label", "quota_short"),
        ("quota.examples.5.label", "ok"),  # 68 is over tuning_min but under its own floor
        ("quota.examples.0.preassigned_tuning_refusals", 22),  # tuning's floor becomes 74: its 68 is now short, so the label "ok" is stale
    ],
)
def test_a_tampered_example_is_refused(path, value):
    parts = path.split(".")
    doc = real_prereg()
    node = doc
    for part in parts[:-1]:
        node = node[int(part) if isinstance(node, list) else part]
    node[parts[-1]] = value
    errors = st.validate_prereg(doc)
    assert any(field.startswith("quota.examples[") and why == "mismatch" for field, why in errors), errors


def test_an_example_without_the_refusal_counts_or_with_a_refusal_count_over_its_families_is_refused():
    for edit in (
        lambda ex: ex.pop("preassigned_realistic_refusals"),
        lambda ex: ex.pop("preassigned_tuning"),
        lambda ex: ex.pop("preassigned_tuning_refusals"),
        lambda ex: ex.update(preassigned_realistic_refusals=ex["preassigned_realistic"] + 1),
        lambda ex: ex.update(preassigned_tuning_refusals=-1),
        lambda ex: ex.update(pool_total="170"),
        lambda ex: ex.update(preassigned_realistic=True),
        lambda ex: ex.update(extra=1),
    ):
        doc = real_prereg()
        edit(doc["quota"]["examples"][0])
        assert ("quota.examples[0]", "wrong_type") in st.validate_prereg(doc)
        assert ("quota.examples[0]", "wrong_type") in st.validate_prereg(doc, recompute=False)  # the shape is checked on every load


@pytest.mark.parametrize("key", ["dev_quota_rule", "realistic_rule", "tuning_floor_rule", "tuning_rule"])
def test_a_tampered_or_missing_rule_text_is_refused_by_prereg_check(world, monkeypatch, key):
    monkeypatch.setattr(st, "PINNED_VALUES", REAL_PINS)
    quota = real_prereg()["quota"]
    world.write_prereg(quota=dict(quota))
    code, out, err = world.run("prereg-check")
    assert code == 0 and "status=ok" in out, (out, err)
    for edit in ({key: quota[key] + "_x"}, {key: "max(min,F)"}):
        world.write_prereg(quota={**quota, **edit})
        code, out, err = world.run("prereg-check")
        assert code == st.EXIT_FAIL and f"field=quota.{key} why=mismatch" in out and "reason=prereg_invalid" in err
    missing = {k: v for k, v in quota.items() if k != key}
    world.write_prereg(quota=missing)
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and f"field=quota.{key} why=mismatch" in out


def test_prereg_check_recomputes_the_examples_with_the_new_rule(world, monkeypatch):
    monkeypatch.setattr(st, "PINNED_VALUES", REAL_PINS)
    quota = real_prereg()["quota"]
    quota["examples"][3]["realistic"] = 49  # the old rule's number for (49, 10)
    world.write_prereg(quota=quota)
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and "prereg_error field=quota.examples[3] why=mismatch" in out


def test_the_test_worlds_examples_are_built_by_the_rule(world):
    doc = world.prereg_doc()
    assert doc["quota"]["examples"] == [quota_example(doc["quota"], 24, 1, 0, 0, 0)]
    assert st.validate_prereg(doc) == []


# --- count-migrated -----------------------------------------------------------------------------------------------------------------------
def _real_quota_world(world) -> None:
    world.write_prereg(quota=dict(REAL_QUOTA))


def _counts(world, eval_registry, tmp_path, gold, real, *extra):
    _real_quota_world(world)
    run = _migrated(world, eval_registry, tmp_path, gold=gold, real=real, tutorial=0)
    code, out, err = run(*extra)
    return code, world.last(out, "count_migrated"), out, err


def test_the_b2_stop_is_repaired_the_realistic_quota_is_52_and_every_split_can_reach_the_target(world, eval_registry, tmp_path):
    """Tuning 26 families (5 refusals), realistic 49 (10 refusals): the old quota 49 had no open slot for the 3 missing refusals."""
    gold = ["refusal"] * 5 + ["direct"] * 21
    real = ["refusal"] * 10 + ["direct"] * 39
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real)
    assert code == 0, err
    assert (line["migrated_families"], line["drafts"], line["drafts_rule"], line["pool_total"]) == ("75", "105", "families_approx", "180")
    assert (line["realistic_quota"], line["realistic_deficit"], line["realistic_band"]) == ("52", "3", "ok")
    assert (line["tuning_quota"], line["tuning_deficit"], line["tuning_floor"], line["tuning_band"]) == ("68", "12", "60", "ok")
    assert (line["refusal_target"], line["refusal_drafts"], line["preassigned_refusals"]) == ("50", "35", "15")  # 35 new refusals
    assert (line["sealed_quota"], line["sealed_refuse_min"], line["sealed_refuse_max"]) == ("60", "15", "18")
    assert (line["tuning_refuse_min"], line["tuning_refuse_max"], line["realistic_refuse_min"], line["realistic_refuse_max"]) == ("17", "20", "13", "13")
    assert (line["supply_min"], line["supply_max"], line["supply"]) == ("45", "51", "ok")  # 50 is inside 45 to 51
    assert_allowlisted(out)
    # what the owner stop was: with the realistic quota at its old value (49) the band is out of reach
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real, "--quota-realistic", "49")
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    assert (line["realistic_quota"], line["realistic_deficit"], line["realistic_open"], line["realistic_band"]) == ("49", "3", "0", "unreachable")


def test_dilution_the_realistic_quota_grows_when_the_refusals_are_over_30_percent(world, eval_registry, tmp_path):
    gold = ["direct"] * 10
    real = ["refusal"] * 15 + ["direct"] * 30  # 15 refusals of 45
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real)
    assert code == 0, err
    assert (line["realistic_quota"], line["realistic_deficit"], line["realistic_band"]) == ("50", "0", "ok")  # 15/50 = 30 % exactly
    assert (line["realistic_refuse_min"], line["realistic_refuse_max"]) == ("15", "15")
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real, "--quota-realistic", "45")  # an explicit quota still wins
    assert code == st.EXIT_REFUSED and (line["realistic_quota"], line["realistic_band"]) == ("45", "unreachable")


def test_the_draft_count_uses_tuning_s_own_floor_not_the_bare_tuning_min(world, eval_registry, tmp_path):
    """52 tuning families with no refusals: 20 of tuning's open slots must be refusals, so its quota is 70, not 60. The bare minimum
    would have said 60 + 60 + 42 - 62 = 100 drafts (no pool floor); the floor says 60 + 70 + 42 - 62 = 110."""
    gold, real = ["direct"] * 52, ["direct"] * 10
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real)
    assert code == 0, err
    assert (line["tuning_floor"], line["migrated_families"]) == ("70", "62")
    assert (line["drafts"], line["drafts_rule"], line["draft_margin"], line["pool_total"]) == ("115", "pool_floor", "5", "177")  # 110 + the margin 5
    assert int(line["tuning_quota"]) >= int(line["tuning_floor"])  # the default tuning quota (the rest of the pool) reaches it
    assert (line["tuning_quota"], line["realistic_quota"]) == ("75", "42")
    # the draft count from the bare minimum would leave tuning short of its floor
    assert 62 + 105 - 60 - 42 < 70 <= 62 + 115 - 60 - 42
    assert st.tuning_floor(REAL_QUOTA, 52, 0) == 70


def _supply(world, eval_registry, tmp_path, tuning_refusals):
    gold = ["refusal"] * tuning_refusals + ["direct"] * (60 - tuning_refusals)
    real = ["refusal"] * 11 + ["direct"] * 31
    return _counts(world, eval_registry, tmp_path, gold, real, "--drafts", "60")  # pool 162, target 45 (44.55)


def test_the_pools_refusal_target_must_lie_within_the_sum_of_the_splits_reachable_intervals(world, eval_registry, tmp_path):
    # tuning is full (60 of 60) with 16 refusals (in band), realistic full (42 of 42) with 11 refusals (in band), sealed 15 to 18: 42 to 45
    code, line, out, err = _supply(world, eval_registry, tmp_path, 16)
    assert code == 0, err
    assert (line["tuning_band"], line["realistic_band"], line["refusal_target"]) == ("ok", "ok", "45")
    assert (line["supply_min"], line["supply_max"], line["supply"]) == ("42", "45", "ok")
    # with 15 tuning refusals every split is individually fine (15 is 25 %), but together they reach 41 to 44: the target 45 is out of reach
    code, line, out, err = _supply(world, eval_registry, tmp_path, 15)
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    assert (line["tuning_band"], line["realistic_band"]) == ("ok", "ok")  # the per-split checks pass
    assert (line["supply_min"], line["supply_max"], line["supply"]) == ("41", "44", "unreachable")
    assert (line["refusal_target"], line["tuning_refuse_min"], line["tuning_refuse_max"]) == ("45", "15", "15")  # and it printed everything first
    assert_allowlisted(out)


def test_a_target_below_the_sum_of_the_minimums_is_just_as_unreachable(world, eval_registry, tmp_path):
    """Both development splits full and at 30 % (100 tuning families with 30 refusals, 80 realistic with 24): sealed 15 to 18 plus 30
    plus 24 is 69 to 72, and the target of a pool of 240 is 66. The refusals already in the pinned families are more than the
    registered share can use."""
    gold = ["refusal"] * 30 + ["direct"] * 70
    real = ["refusal"] * 24 + ["direct"] * 56
    code, line, out, err = _counts(world, eval_registry, tmp_path, gold, real, "--drafts", "60")
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    assert (line["pool_total"], line["refusal_target"]) == ("240", "66")
    assert (line["tuning_quota"], line["realistic_quota"], line["tuning_band"], line["realistic_band"]) == ("100", "80", "ok", "ok")
    assert (line["supply_min"], line["supply_max"], line["supply"]) == ("69", "72", "unreachable")


# --- split ---------------------------------------------------------------------------------------------------------------------------------
def _quota_world(w, *, tuning_refusals: int = 0, realistic=(5, 0), dev=(4, 3)):
    """Pinned: 3 tuning families (``tuning_refusals`` of them refusals) and ``realistic`` = (answers, refusals) realistic families. 12
    sealed-eligible drafts (3 refusals) and ``dev`` = (refusals, answers) development drafts tied to public ids.

    Default: 5 realistic answers, so realistic gets max(4, 5, ceil(20/3)) = 7 (the old rule: 5), tuning 8, and the 7 development drafts
    are exactly the 2 open realistic slots (both refusals) and the 5 open tuning slots (2 refusals, 3 answers)."""
    w.write_prereg()
    w.commit_prereg()
    for i in range(3):
        w.pinned("tuning", "refuse" if i < tuning_refusals else "answer", source="legacy", register="modelled")
    for scope, count in (("answer", realistic[0]), ("refuse", realistic[1])):
        for _ in range(count):
            w.pinned("realistic", scope, source="tutorial", register="lay")
    for _ in range(3):
        w.draft("refuse", register="modelled")
    for _ in range(9):
        w.draft("answer", register="modelled")
    drafts = [w.draft("refuse", register="lay") for _ in range(dev[0])] + [w.draft("answer", register="lay") for _ in range(dev[1])]
    w.flush_pool()
    _prepare_twins(w, [(d, public_id(0xC00 + i)) for i, d in enumerate(drafts)])


def test_split_gives_realistic_the_quota_the_shared_rule_gives(world):
    _quota_world(world)
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, (out, err)
    payload = assign_payload(world)
    assert payload["quotas"] == {"sealed": 12, "realistic": 7, "tuning": 8}  # max(realistic_min 4, F 5) would have been 5, and tuning 10
    assert st.compute_quotas(world.prereg_doc()["quota"], 27, 5, 0) == payload["quotas"]  # the same function count-migrated uses
    assigned = payload["assignment"]  # every family, the pinned ones too
    assert {s: sum(1 for v in assigned.values() if v == s) for s in ("sealed", "tuning", "realistic")} == {"sealed": 12, "tuning": 8, "realistic": 7}
    new_realistic = [f for f, s in assigned.items() if s == "realistic" and f not in payload["preassigned"]]
    refusals = {r["family_id"] for r in read_rows(world.dev / "realistic_r1.jsonl") if r["scope"] == "refuse"}
    assert len(new_realistic) == 2 and set(new_realistic) == refusals  # the two open slots are exactly the two refusals the band needs


def test_split_counts_the_pinned_realistic_refusals_in_the_quota(world):
    """Six pinned realistic families, two of them refusals: max(4, 6, ceil(16/3) = 6, ceil(20/3) = 7) = 7. Counted without the refusals
    the rule would say 8, and the old rule 6. The one open realistic slot must be an answer (it already holds its two refusals)."""
    _quota_world(world, realistic=(4, 2), dev=(2, 4))
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, (out, err)
    payload = assign_payload(world)
    assert payload["quotas"] == {"sealed": 12, "realistic": 7, "tuning": 8}
    assert (st.min_feasible_quota(4, 6, 2), st.min_feasible_quota(4, 6, 0)) == (7, 8)
    new_realistic = [f for f, s in payload["assignment"].items() if s == "realistic" and f not in payload["preassigned"]]
    new_rows = [r for r in read_rows(world.dev / "realistic_r1.jsonl")]
    assert len(new_realistic) == 1 and {r["scope"] for r in new_rows} == {"answer"}


def test_split_is_quota_short_when_tuning_is_under_its_own_floor_not_just_under_tuning_min(world):
    """Three pinned tuning refusals (of 3): tuning's floor is max(8, 3, 0, ceil(30/3) = 10) = 10. Tuning's quota is the rest, 8: over
    tuning_min (8) but under its floor, so no split is attempted (the old rule went on to an infeasible band)."""
    _quota_world(world, tuning_refusals=3)
    # quotas: realistic 7 and tuning 27 - 12 - 7 = 8 < 10
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_SPLIT_INFEASIBLE and "outcome=quota_short attempt=1" in out
    assert [(r["kind"], r["outcome"]) for r in world.ledger().records("split_attempt")] == [("split_attempt", "quota_short")]
    assert world.ledger().records("release") == [] and not list(world.dev.glob("*_r1.jsonl"))
    assert "split_constraint" not in out  # it never reached the splitter


def test_round_two_reuses_round_ones_released_quotas_and_recomputes_nothing(world, monkeypatch):
    _round2_world(world)

    def boom(*args, **kwargs):
        raise AssertionError("round 2 must not recompute a quota")

    monkeypatch.setattr(st, "compute_quotas", boom)  # (the prereg's examples still use tuning_floor when it is loaded)
    code, out, err = world.run("split", "--round", "2")
    assert code == 0, (out, err)
    assert assign_payload(world, 2)["quotas"] == assign_payload(world, 1)["quotas"] == {"sealed": 12, "realistic": 4, "tuning": 8}


def test_round_two_keeps_a_larger_round_one_realistic_quota_when_the_survivors_alone_would_give_a_smaller_one(world):
    """Round 1 releases realistic at 7 (5 pinned answers + 2 refusals). The owner then rejects two pinned answers and one refusal: the
    4 survivors (1 refusal) would need only max(4, 4, 4, 4) = 4, but round 2 replaces what was lost, so realistic stays at 7 and the
    replacements are one refusal and two answers."""
    _quota_world(world)
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, (out, err)
    assert st.min_feasible_quota(4, 4, 1) == 4  # the quota the survivors alone would give
    pinned_answers = [f for f, s in assign_payload(world)["preassigned"].items() if s == "realistic"]
    refusals = sorted({r["family_id"] for r in read_rows(world.dev / "realistic_r1.jsonl") if r["scope"] == "refuse"})
    reject = {pinned_answers[0]: "R", pinned_answers[1]: "R", refusals[0]: "R"}
    code, _, err = world.run("sheet", "--kind", "development", "--name", "r1")
    assert code == 0, err
    world.mark_sheet(world.sheet_path("development", "r1"), reject, default="A")
    code, _, err = world.run("apply", "--kind", "development", "--sheet", world.sheet_path("development", "r1"))
    assert code == 0, err
    replacements = [world.draft("refuse", register="lay"), world.draft("answer", register="lay"), world.draft("answer", register="lay")]
    world.flush_pool("round2.jsonl")
    code, out, err = world.run("twins", "--round", "2", "--verdict", world.verdict([(d, public_id(0xD00 + i)) for i, d in enumerate(replacements)]))
    assert code == 0, err
    code, out, err = world.run("split", "--round", "2")
    assert code == 0, (out, err)
    payload = assign_payload(world, 2)
    assert payload["quotas"] == assign_payload(world, 1)["quotas"] == {"sealed": 12, "realistic": 7, "tuning": 8}
    assert {payload["assignment"][d] for d in replacements} == {"realistic"}
    assert sum(1 for s in payload["preassigned"].values() if s == "realistic") == 4  # the survivors, pinned
