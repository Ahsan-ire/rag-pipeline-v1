"""16A-2b b1a closure round r4, the ``sealed_tool`` and prereg parts: the registered ``incumbent`` and ``judge`` roots (A, B),
the draft margin (D1), ``controls.committed`` (D2) and the pinned quota numbers and minimum shares (D3).

Synthetic fixtures only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import judge, pipeline_config
from tests.p16w_sealed_world import repos, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r2_tool import _migrated  # the v1-set builder for count-migrated
from tests.test_p16a2b_b1a_r3_tool import REAL_QUOTA, put  # noqa: F401
from tests.test_p16w_sealed_tool import assert_allowlisted

ROOT = Path(__file__).resolve().parent.parent
REAL_PINS = st.PINNED_VALUES  # captured at import, before a World replaces them with its own small quota's


def real_prereg() -> dict:
    return json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))


# --- A: the incumbent root -----------------------------------------------------------------------------------------------------
INCUMBENT = {
    "incumbent.config_fn": "pipeline_config_v2",
    "incumbent.components": ["version", "code", "runtime", "endpoint", "embedding", "index", "bm25", "fusion", "rewrite", "generation"],
    "incumbent.code": "all_src_modules_except_code_excluded",
    "incumbent.code_excluded": list(pipeline_config.CODE_EXCLUDED),
    "incumbent.excluded": ["timeouts_retries", "git_sha", "paths", "judge", "eval_sets", "corpus_text"],
    "incumbent.fixed_by": "controls_record_incumbent_config_hash",
    "incumbent.draws": "each_counted_draw_sidecar_carries_the_incumbent_config_hash",
    "incumbent.slots": ["0", "0b"],
    "incumbent.candidates": "batch_entry_config_hash_is_pipeline_config_v2_of_the_candidate",
}


def test_the_incumbent_root_is_registered_exactly_and_the_real_prereg_carries_it():
    doc = real_prereg()
    for path, value in INCUMBENT.items():
        assert st.REGISTERED[path] == value and st._eq(st._at(doc, path), value), path
    assert set(doc["incumbent"]) == {p.split(".")[1] for p in INCUMBENT}
    assert doc["incumbent"]["components"] == list(pipeline_config.PIPELINE_CONFIG_KEYS)
    assert doc["adoption"]["confirmatory_batch"]["candidate_identity"] == (
        "config_hash_is_pipeline_config_v2_checked_by_16b_gate_before_claim")
    assert st.validate_prereg(doc, recompute=False) == [] and st.computed_errors(doc) == [] and st.pinned_errors(doc) == []


def test_prereg_check_refuses_components_that_are_not_the_codes(monkeypatch):
    doc = real_prereg()
    assert st.computed_errors(doc) == []
    with pytest.MonkeyPatch.context() as m:
        m.setattr(pipeline_config, "PIPELINE_CONFIG_KEYS", (*pipeline_config.PIPELINE_CONFIG_KEYS, "tokenizer"))
        assert ("incumbent.components", "mismatch") in st.computed_errors(doc)  # the code grew a component, the prereg did not
    # a prereg that lists a different set is refused whatever the table says
    for components in (INCUMBENT["incumbent.components"][:-1], [*INCUMBENT["incumbent.components"], "x"],
                       list(reversed(INCUMBENT["incumbent.components"])), "pipeline_config_v1", None):
        assert ("incumbent.components", "mismatch") in st.computed_errors(put(doc, "incumbent.components", components))
    assert ("incumbent.components", "mismatch") in st.computed_errors(put(doc, "incumbent.components", KeyError))


@pytest.mark.parametrize("path", sorted(INCUMBENT))
def test_every_incumbent_value_is_checked_changed_and_removed(path):
    doc = real_prereg()
    value = doc["incumbent"][path.split(".")[1]]
    changed = [*value, "x"] if isinstance(value, list) else value + "_x"
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, changed), recompute=False)
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, KeyError), recompute=False)


def test_an_unknown_incumbent_leaf_is_refused_and_the_root_is_required():
    doc = real_prereg()
    errors = st.validate_prereg(put(doc, "incumbent.extra", 1), recompute=False)
    assert ("incumbent.extra", "unknown_key") in errors and ("incumbent", "mismatch") in errors
    del doc["incumbent"]
    assert ("incumbent", "missing") in st.validate_prereg(doc, recompute=False)


# --- B: the judge root ---------------------------------------------------------------------------------------------------------
JUDGE = {
    "judge.required_on": ["0", "0b", "1", "2", "3", "4"],
    "judge.model": "claude-sonnet-5",
    "judge.prompt_version": "faithfulness-judge-v1",
    "judge.sample": "all_eligible",
    "judge.eligible": "retrieval_eligible_rows_with_complete_non_refusal_answer",
    "judge.rate": "sum_unsupported_over_sum_claims_of_ok_judged_sealed_rows",
    "judge.compare": "candidate_and_slot0_canonical_results_carry_equal_judge_config_hash_and_scorer_version",
    "judge.covers": ["model", "client", "prompt", "verdicts", "sample", "parsing", "eligibility", "scoring", "endpoint"],
    "judge.errors": "any_sealed_judge_error_means_not_shown",
}


def test_the_judge_root_is_registered_exactly_and_its_hash_is_the_codes():
    doc = real_prereg()
    for path, value in JUDGE.items():
        assert st.REGISTERED[path] == value and st._eq(st._at(doc, path), value), path
    assert doc["judge"]["config_hash"] == judge.judge_identity()["config_hash"]  # computed from the code, not typed
    assert "judge.config_hash" in st.COMPUTED_PATHS and "judge.config_hash" not in st.REGISTERED
    assert doc["judge"]["model"] == judge.JUDGE_MODEL and doc["judge"]["prompt_version"] == judge.JUDGE_PROMPT_VERSION
    assert doc["adoption"]["confirmatory_batch"]["requires"] == ["controls_record", "judge_on", "registered_judge"]


def test_the_eligibility_wording_is_the_judges_own_rule():
    """The words ``retrieval eligible rows with a complete non-refusal answer`` are the rule ``_judge_v6`` applies: rows
    whose scope is not ``refuse``, whose generation produced a result with a complete status and that is not a refusal.
    Since r5 the rule is the named function ``judge.is_judgeable`` (hashed with the judge's identity) and ``_judge_v6``
    calls it."""
    import inspect

    from src import evaluator

    source = inspect.getsource(judge.is_judgeable)
    assert 'scope == "refuse"' in source and "generation error" in source
    assert "INCOMPLETE_STATUSES" in source and "is_refusal(result[\"answer\"])" in source
    assert "is_judgeable(" in inspect.getsource(evaluator._judge_v6)


def test_a_prereg_whose_judge_identity_is_not_the_codes_is_refused_by_prereg_check_and_not_by_the_other_commands(monkeypatch):
    doc = real_prereg()
    wrong = put(doc, "judge.config_hash", "0" * 64)
    assert ("judge.config_hash", "mismatch") in st.computed_errors(wrong)  # recompute (prereg-check)
    assert st.validate_prereg(wrong, recompute=False) == []  # every other command only needs it present and shaped
    for bad in ("short", None, 5, "G" * 64):
        assert ("judge.config_hash", "mismatch") in st.validate_prereg(put(doc, "judge.config_hash", bad), recompute=False)
    assert ("judge.config_hash", "mismatch") in st.validate_prereg(put(doc, "judge.config_hash", KeyError), recompute=False)
    # the code moves: the prompt version, then the model
    with pytest.MonkeyPatch.context() as m:
        m.setattr(judge, "JUDGE_PROMPT_VERSION", "faithfulness-judge-v2")
        errors = st.computed_errors(doc)
        assert ("judge.prompt_version", "mismatch") in errors and ("judge.config_hash", "mismatch") in errors
    with pytest.MonkeyPatch.context() as m:
        m.setattr(judge, "JUDGE_MODEL", "another-model")
        errors = st.computed_errors(doc)
        assert ("judge.model", "mismatch") in errors and ("judge.config_hash", "mismatch") in errors


def test_prereg_check_runs_the_computed_checks(world):
    """The command (not just the function) reports a judge identity that is not the code's."""
    world.write_prereg(judge={**world.prereg_doc()["judge"], "config_hash": "0" * 64})
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and "field=judge.config_hash why=mismatch" in out


@pytest.mark.parametrize("path", sorted(JUDGE))
def test_every_judge_value_is_checked_changed_and_removed(path):
    doc = real_prereg()
    value = doc["judge"][path.split(".")[1]]
    changed = [*value, "x"] if isinstance(value, list) else value + "_x"
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, changed), recompute=False)
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, KeyError), recompute=False)


def test_an_unknown_judge_leaf_is_refused_and_the_root_is_required():
    doc = real_prereg()
    assert ("judge.extra", "unknown_key") in st.validate_prereg(put(doc, "judge.extra", 1), recompute=False)
    del doc["judge"]
    assert ("judge", "missing") in st.validate_prereg(doc, recompute=False)


# --- D1: the draft margin ------------------------------------------------------------------------------------------------------
def test_the_draft_count_rule_is_registered_with_a_margin_and_a_source_mix():
    attempt = real_prereg()["drafting_rule"]["attempt_1"]
    assert attempt["draft_margin"] == 5
    assert attempt["families_rule"] == "max(families_approx,(sealed_quota+tuning_min+realistic_quota)-migrated_families)+draft_margin"
    assert attempt["source_mix"] == "tutorial_sourced_about_half_rest_handbook"
    # the fixed 50/50 counts are gone; the per-tutorial caps stay
    assert "tutorial_sourced_approx" not in attempt and "handbook_drafted_approx" not in attempt
    assert (attempt["per_unseen_tutorial_approx"], attempt["unseen_tutorials"], attempt["per_title_document_set_max"],
            attempt["title_document_sets"], attempt["never_drafted_from"]) == (3, 15, 1, 7, ["t01", "t07"])
    for old in ("tutorial_sourced_approx", "handbook_drafted_approx"):
        errors = st.validate_prereg(put(real_prereg(), f"drafting_rule.attempt_1.{old}", 50), recompute=False)
        assert (f"drafting_rule.attempt_1.{old}", "unknown_key") in errors


def test_57_migrated_families_make_110_drafts_and_the_margin_is_printed(world, eval_registry, tmp_path):
    world.write_prereg(quota=dict(REAL_QUOTA))
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 40, real=["direct"] * 16, tutorial=1)  # 40 + 17 = 57
    code, out, err = run()
    assert code == 0, err
    line = world.last(out, "count_migrated")
    # max(100, (60 + 60 + 42) - 57 = 105) + 5
    assert (line["migrated_families"], line["drafts"], line["drafts_rule"], line["draft_margin"]) == ("57", "110", "pool_floor", "5")
    assert line["pool_total"] == "167"
    assert_allowlisted(out)


def test_the_margin_follows_the_prereg_and_an_explicit_drafts_count_carries_none(world, eval_registry, tmp_path):
    world.write_prereg(quota=dict(REAL_QUOTA))
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 40, real=["direct"] * 16, tutorial=1)
    code, out, err = run("--drafts", "105")
    line = world.last(out, "count_migrated")
    assert (line["drafts"], line["drafts_rule"], line["draft_margin"]) == ("105", "explicit", "0")  # the caller's count, as given
    rule = world.prereg_doc()["drafting_rule"]
    rule["attempt_1"]["draft_margin"] = 0
    world.write_prereg(quota=dict(REAL_QUOTA), drafting_rule=rule)
    line = world.last(run()[1], "count_migrated")
    assert (line["drafts"], line["draft_margin"]) == ("105", "0")
    rule["attempt_1"]["draft_margin"] = 12
    world.write_prereg(quota=dict(REAL_QUOTA), drafting_rule=rule)
    assert world.last(run()[1], "count_migrated")["drafts"] == "117"
    for bad in (-1, True, "5", None):  # a margin that is not a non-negative integer is a usage error
        rule["attempt_1"]["draft_margin"] = bad
        world.write_prereg(quota=dict(REAL_QUOTA), drafting_rule=rule)
        code, out, err = run()
        assert code == st.EXIT_USAGE and "reason=usage" in err, bad
    del rule["attempt_1"]["draft_margin"]
    world.write_prereg(quota=dict(REAL_QUOTA), drafting_rule=rule)
    assert run()[0] == st.EXIT_USAGE


# --- D2: when the controls are committed ---------------------------------------------------------------------------------------
def test_the_controls_are_committed_in_the_ledger_before_slot_0():
    assert st.REGISTERED["controls.committed"] == "ledger_controls_record_before_slot_0"
    assert real_prereg()["controls"]["committed"] == "ledger_controls_record_before_slot_0"
    assert ("controls.committed", "mismatch") in st.validate_prereg(
        put(real_prereg(), "controls.committed", "ledger_controls_record_before_batch_declaration"), recompute=False)


# --- D3: the pinned quota numbers and minimum shares ------------------------------------------------------------------------------
def test_the_pins_are_the_real_preregs_values_and_cover_every_quota_number_and_min_share():
    doc = real_prereg()
    assert {path for path, _ in REAL_PINS} == {
        "quota.sealed", "quota.spare", "quota.floor_sealed", "quota.floor_answerable", "quota.realistic_min",
        "quota.tuning_min", "min_share.realistic.tutorial_or_colleague",
    }
    assert dict(REAL_PINS) == {
        "quota.sealed": 60, "quota.spare": 10, "quota.floor_sealed": 50, "quota.floor_answerable": 35,
        "quota.realistic_min": 42, "quota.tuning_min": 60, "min_share.realistic.tutorial_or_colleague": 0.25,
    }
    for path, value in REAL_PINS:
        assert st._eq(st._at(doc, path), value), path
    assert st.pinned_errors(doc) == []
    assert {k: doc["quota"][k] for k in REAL_QUOTA} == REAL_QUOTA


@pytest.mark.parametrize("path,value", REAL_PINS)
def test_every_pinned_value_is_refused_when_changed_or_removed(path, value):
    doc = real_prereg()
    changed = value + 1 if isinstance(value, int) else value + 0.05
    assert (path, "mismatch") in st.pinned_errors(put(doc, path, changed))
    assert (path, "mismatch") in st.pinned_errors(put(doc, path, KeyError))
    assert (path, "mismatch") in st.pinned_errors(put(doc, path, True if value != 1 else 0))  # a bool is not the number


def test_a_prereg_with_the_wrong_quota_passes_every_other_command_but_not_prereg_check(world, monkeypatch):
    """The pins are the pre-registration: ``prereg-check`` holds the file to the real numbers; a command only needs a valid
    prereg (the test worlds' small quotas are valid)."""
    monkeypatch.setattr(st, "PINNED_VALUES", REAL_PINS)
    world.write_prereg(quota=dict(REAL_QUOTA))
    code, out, err = world.run("prereg-check")
    assert code == 0 and "status=ok" in out, (out, err)
    for key, bump in (("sealed", 61), ("spare", 11), ("floor_sealed", 51), ("floor_answerable", 36), ("realistic_min", 43), ("tuning_min", 61)):
        quota = dict(REAL_QUOTA, **{key: bump})
        if key == "floor_sealed":
            quota["sealed"] = quota["floor_sealed"] + quota["spare"]  # keep sealed = floor_sealed + spare valid
        if key == "spare":
            quota["sealed"] = quota["floor_sealed"] + quota["spare"]
        world.write_prereg(quota=quota)
        code, out, err = world.run("prereg-check")
        assert code == st.EXIT_FAIL and "why=mismatch" in out and "reason=prereg_invalid" in err, key
        assert any(f"field=quota.{k}" in out for k in quota if quota[k] != REAL_QUOTA[k]), (key, out)
    world.write_prereg(quota=dict(REAL_QUOTA), min_share={"realistic": {"tutorial_or_colleague": 0.3}})
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and "field=min_share.realistic.tutorial_or_colleague" in out
    # the test worlds' small quota is a valid prereg (every other command accepts it), just not the pre-registered one
    small = world.prereg_doc()
    assert st.validate_prereg(small, recompute=False) == [] and st.pinned_errors(small)
