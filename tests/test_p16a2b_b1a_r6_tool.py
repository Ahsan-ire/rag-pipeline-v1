"""16A-2b b1a closure round r6, the ``sealed_tool`` and prereg parts: ``record-draw`` is gone (a draw is recorded inside the run),
``draws`` counts recorded draws and unrecorded runs per configuration, ``declare-batch`` takes abandoned entries, and the
registered values of the scorer, the exclusion list, the judge and the draws are in the table and recomputed by ``prereg-check``.

Synthetic fixtures only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import judge, pipeline_config, scoring_identity, sealed_ledger
from tests.p16w_sealed_world import repos, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r3_tool import put
from tests.test_p16a2b_b1a_tool import record_controls, record_draws, sealed_frozen
from tests.test_p16w_sealed_tool import assert_allowlisted

ROOT = Path(__file__).resolve().parent.parent


def real_prereg() -> dict:
    return json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))


# --- record-draw is gone ---------------------------------------------------------------------------------------------------------
def test_there_is_no_command_that_records_a_draw_afterwards(world):
    assert "record-draw" not in st.STATE_COMMANDS and not hasattr(st, "cmd_record_draw")
    code, out, err = world.run("record-draw", "--sidecar", "x", "--artifact", "y")
    assert code == st.EXIT_USAGE and "reason=usage" in err and out == ""
    assert "record-draw" not in st.build_parser().format_help()


# --- draws --------------------------------------------------------------------------------------------------------------------------
def test_draws_is_read_only_and_counts_per_configuration(world):
    assert "draws" not in st.STATE_COMMANDS
    code, out, err = world.run("draws")
    assert code == 0 and world.last(out, "draws") == {"_kind": "draws", "configs": "0", "recorded": "0", "unrecorded": "0"}
    sealed_frozen(world)
    record_controls(world)  # the incumbent is f * 64
    ledger = world.ledger()
    record_draws(ledger, "f" * 64, n=3)
    record_draws(ledger, "1" * 64, n=2)
    ledger.record_dev_run_unrecorded("f" * 64, "run-a")
    ledger.record_dev_run_unrecorded("2" * 64, "run-b")  # a configuration that was only ever run unrecorded
    ledger.record_dev_run_unrecorded(None, "run-c")
    code, out, err = world.run("draws")
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.startswith("draw_config ")]
    assert sorted(lines) == sorted([
        f"draw_config config={'f' * 64} recorded=3 unrecorded=1", f"draw_config config={'1' * 64} recorded=2 unrecorded=0",
        f"draw_config config={'2' * 64} recorded=0 unrecorded=1", "draw_config config=none recorded=0 unrecorded=1",
    ])
    assert world.last(out, "draws") == {"_kind": "draws", "configs": "4", "recorded": "5", "unrecorded": "3"}
    assert_allowlisted(out)
    assert world.ledger().records() == ledger.records()  # it recorded nothing


# --- declare-batch: abandoned entries -------------------------------------------------------------------------------------------------
def test_declare_batch_takes_an_abandoned_entry_for_a_partly_drawn_configuration(world, monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    sealed_frozen(world)
    record_controls(world)
    a, b, c = "1" * 64, "2" * 64, "3" * 64
    record_draws(world.ledger(), a, n=5, mode="replay")  # r7: a replay entry's draws are replay draws of the incumbent's
    record_draws(world.ledger(), b, n=2)
    record_draws(world.ledger(), c, n=5, mode="replay")
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{c}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err  # b was tried and recorded and is left out
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{b}:replay", "--candidate", f"{c}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err  # two of five draws are not a runnable candidate
    for bad in (f"{a}:abandoned:{'7' * 64}", f"{a}:abandoned:x"):
        code, out, err = world.run("declare-batch", "--candidate", bad)
        assert code == st.EXIT_REFUSED and "reason=batch_invalid" in err
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:abandoned", "--candidate", f"{b}:abandoned",
                               "--candidate", f"{c}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err  # a is fully drawn: it is not abandoned
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{b}:abandoned", "--candidate", f"{c}:replay")
    assert code == 0, err
    assert [c_["expansion"] for c_ in world.ledger().batch_declaration()["candidates"]] == ["replay", "abandoned", "replay"]


# --- the registered values --------------------------------------------------------------------------------------------------------------
NEW = {
    "adoption.development_evidence.draws_recorded_by": "pipeline_eval_record_draw_in_run",
    "adoption.development_evidence.unrecorded_runs": "never_count_and_are_logged_per_config",
    "adoption.development_evidence.abandoned": "counts_toward_k",
    "adoption.control_flip.candidate_draws": "any_of_its_first_5_recorded_paired_by_draw_index",
    "adoption.scoring.identity": "scorer_identity_v1",
    "adoption.scoring.compare": "candidate_and_slot0_equal_and_counted_draws_equal_incumbent_draws",
    "incumbent.config_fn": "pipeline_config_v4",
    "incumbent.code": "all_src_modules_except_code_excluded_and_hashed_scripts",
    "incumbent.candidates": "batch_entry_config_hash_is_pipeline_config_v4_of_the_candidate",
    "adoption.confirmatory_batch.candidate_identity": "config_hash_is_pipeline_config_v4_checked_by_16b_gate_before_claim",
    "judge.compare": "candidate_and_slot0_canonical_results_carry_equal_judge_config_hash_and_scorer_identity",
}


@pytest.mark.parametrize("path", sorted(NEW))
def test_the_r6_registered_values_are_in_the_table_and_the_real_prereg_and_each_is_checked(path):
    doc = real_prereg()
    assert st.REGISTERED[path] == NEW[path] and st._eq(st._at(doc, path), NEW[path])
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, NEW[path] + "_x"), recompute=False)
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, KeyError), recompute=False)


def test_the_recomputed_values_are_the_codes_and_a_mismatch_is_refused_by_prereg_check(monkeypatch):
    doc = real_prereg()
    assert st.computed_errors(doc) == []
    assert doc["adoption"]["scoring"]["covers"] == scoring_identity.covered()
    assert doc["judge"]["covers"] == list(judge.JUDGE_COVERS) and doc["incumbent"]["components"][-2:] == ["scoring_inputs", "scan_inputs"]
    for path, other in (
        ("adoption.scoring.covers", scoring_identity.covered()[:-1]), ("adoption.scoring.covers", list(reversed(scoring_identity.covered()))),
        ("judge.covers", list(judge.JUDGE_COVERS)[:-1]), ("incumbent.code_excluded_imports", list(pipeline_config.CODE_EXCLUDED_IMPORTS)[:-1]),
        ("incumbent.code_scripts", []), ("incumbent.code_excluded", [*pipeline_config.CODE_EXCLUDED, "evaluator.py"]),
    ):
        assert (path, "mismatch") in st.computed_errors(put(doc, path, other)), path
    # the code moves without the prereg: the scorer covers one more file, the judge one more module
    monkeypatch.setattr(scoring_identity, "SCORING_DATA_FILES", (*scoring_identity.SCORING_DATA_FILES, "eval/sets.json"))
    assert ("adoption.scoring.covers", "mismatch") in st.computed_errors(doc)
    monkeypatch.undo()
    monkeypatch.setattr(judge, "JUDGE_COVERS", (*judge.JUDGE_COVERS, "more"))
    assert ("judge.covers", "mismatch") in st.computed_errors(doc)


def test_the_real_prereg_passes_every_check_and_names_no_judge_among_the_excluded_things():
    doc = real_prereg()
    assert st.validate_prereg(doc) == [] and st.pinned_errors(doc) == []
    assert "judge" not in doc["incumbent"]["excluded"] and "judge.covers" in st.REGISTERED
    assert st.REGISTERED["incumbent.excluded"] == [
        "timeouts_retries", "git_sha", "paths", "eval_set_contents_bound_by_freeze_records", "corpus_text"]  # r8: eval_sets is not excluded wholesale
    assert st.REGISTERED["incumbent.components"] == list(pipeline_config.PIPELINE_CONFIG_KEYS)
    assert st.REGISTERED["incumbent.code_excluded"] == list(pipeline_config.CODE_EXCLUDED)
    assert st.REGISTERED["incumbent.code_excluded_imports"] == list(pipeline_config.CODE_EXCLUDED_IMPORTS)


def test_an_unregistered_leaf_under_the_new_scoring_block_is_refused():
    errors = st.validate_prereg(put(real_prereg(), "adoption.scoring.extra", 1), recompute=False)
    assert ("adoption.scoring.extra", "unknown_key") in errors
