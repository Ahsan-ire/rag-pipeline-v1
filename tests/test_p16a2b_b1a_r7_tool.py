"""16A-2b b1a closure round r7, the ``sealed_tool`` parts: late exclusions (T6 m1), the ``declare-batch`` bindings (T3, T5) and the
registered values of the round. Synthetic fixtures only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read,
never written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import judge, pipeline_config, scoring_identity, sealed_ledger
from tests.p16_draws import record_draws
from tests.p16w_sealed_world import fid_of, repos, standard_released, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r3_tool import put
from tests.test_p16a2b_b1a_tool import record_controls, sealed_frozen
from tests.test_p16w_sealed_tool import assert_allowlisted

ROOT = Path(__file__).resolve().parent.parent


def real_prereg() -> dict:
    return json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))


def store_slot(world, slot="0"):
    """Slot ``slot`` claimed and its results stored (a4 steps 1 and 3), the way the slot protocol leaves it."""
    run_dir = world.root / "runs" / f"run-{slot}-r7"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text("{}", encoding="utf-8")
    ledger = world.ledger()
    ledger.claim(slot)
    ledger.store_results(slot, run_dir)


# =================================================================================================================================
# T6 m1: exclusions carry the slot state they were made in, and the owner's late ones are refused (r8: every reason is refused)
# =================================================================================================================================
def test_an_exclusion_records_which_slots_were_stored_when_it_was_made(world):
    std = standard_released(world)
    code, out, err = world.run("exclude", "--family", std.sealed_singletons[0], "--reason", "exposure")
    assert code == 0, err
    store_slot(world)
    # r8: the owner's command writes nothing once slot 0 is stored, so the later exclusion is the scanner's (the ledger API
    # ``render_and_publish`` calls), which also names the slot whose report it scanned
    world.ledger().exclude(std.sealed_singletons[1], "s_hit", scan_slot="0")
    records = world.ledger().records("exclusion")
    assert [r["slots_stored"] for r in records] == [[], ["0"]]
    assert [r.get("scan_slot") for r in records] == [None, "0"]
    assert world.ledger().stored_slots() == ["0"]


def test_stored_slots_lists_only_slots_with_stored_results_in_slot_order(tmp_path):
    from tests.test_p16a2b_b1a_r6_draws import isolated

    led = isolated(tmp_path)
    assert led.stored_slots() == []
    led.exclude("g1", "s_hit")
    led.claim("0")
    assert led.stored_slots() == []  # claimed, nothing stored yet
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    (run_dir / "results.json").write_text("{}", encoding="utf-8")
    led.store_results("0", run_dir)
    led.exclude("g2", "exposure")
    assert led.stored_slots() == ["0"]
    assert [r["slots_stored"] for r in led.records("exclusion")] == [[], ["0"]]


@pytest.mark.parametrize("reason", ["s_hit", "exposure", "twin_dev_version"])
def test_every_manual_exclusion_reason_is_refused_once_slot_0_is_stored(world, reason):  # r8: s_hit too (Codex B1)
    std = standard_released(world)
    code, out, err = world.run("exclude", "--family", std.sealed_singletons[0], "--reason", reason)
    assert code == 0, err  # before slot 0 is stored they are allowed
    store_slot(world)
    before = world.ledger().records()
    code, out, err = world.run("exclude", "--family", std.sealed_singletons[1], "--reason", reason)
    assert code == st.EXIT_REFUSED and "reason=exclusion_late" in err and out == ""
    code, out, err = world.run("exclude", "--group", std.sealed_singletons[1], "--reason", reason)
    assert code == st.EXIT_REFUSED and "reason=exclusion_late" in err
    assert world.ledger().records() == before  # nothing was recorded


def test_a_hand_made_s_hit_exclusion_is_refused_once_slot_0_is_stored_because_no_scan_receipt_stands_behind_it(world):
    """r8 (Codex B1): the installed command used to accept ``--reason s_hit`` after slot 0 with no scanner behind it, changing the
    population the later slots are compared on. The scanner's own ``s_hit`` exclusions (``scan_slot``) are the only late ones."""
    std = standard_released(world)
    store_slot(world)
    before = world.ledger().records()
    for args in (("--family", std.sealed_singletons[0]), ("--group", std.sealed_singletons[0])):
        code, out, err = world.run("exclude", *args, "--reason", "s_hit")
        assert code == st.EXIT_REFUSED and "reason=exclusion_late" in err and out == ""
        assert_allowlisted(err)
    assert world.ledger().records() == before and world.ledger().records("exclusion") == []


def test_slot_0b_stored_counts_as_late_too(world):
    std = standard_released(world)
    ledger = world.ledger()
    ledger.claim("0")  # slot 0 spent (claimed, nothing stored): 0b becomes available
    store_slot(world, "0b")
    assert ledger.stored_slots() == ["0b"]
    for reason in st.EXCLUSION_REASONS:
        code, out, err = world.run("exclude", "--family", std.sealed_singletons[0], "--reason", reason)
        assert code == st.EXIT_REFUSED and "reason=exclusion_late" in err, reason


def test_the_new_reasons_are_printable_enum_words():
    for word in ("exclusion_late", "draws_mismatch"):
        assert word in st.REASONS and word in st.ENUM_WORDS
    assert st.LATE_EXCLUSION_REASONS == st.EXCLUSION_REASONS == ("s_hit", "exposure", "twin_dev_version")  # r8: every reason


# =================================================================================================================================
# T3 / T5: declare-batch binds the entries to their draws
# =================================================================================================================================
def ready(world, monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    sealed_frozen(world)
    record_controls(world)  # the incumbent is f * 64


def test_declare_batch_refuses_a_replay_entry_whose_draws_are_build_draws(world, monkeypatch):
    ready(world, monkeypatch)
    a = "1" * 64
    record_draws(world.ledger(), "f" * 64)
    record_draws(world.ledger(), a, mode="build")  # five draws, but not replay draws of the incumbent's
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_mismatch" in err and out == "" and world.ledger().batch_declaration() is None
    assert_allowlisted(err)


def test_declare_batch_refuses_a_candidate_entry_with_replay_draws_or_a_rewrite_that_is_not_new(world, monkeypatch):
    ready(world, monkeypatch)
    a, b = "1" * 64, "2" * 64
    rewrite = "7" * 64
    record_draws(world.ledger(), a, mode="replay")
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:candidate:{rewrite}")
    assert code == st.EXIT_REFUSED and "reason=draws_mismatch" in err  # replay draws under a rewrite-changing entry
    record_draws(world.ledger(), "f" * 64)
    from tests.p16_draws import REWRITE

    record_draws(world.ledger(), b, rewrite=REWRITE)  # build draws, but under the incumbent's own rewrite identity
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{b}:candidate:{REWRITE}")
    assert code == st.EXIT_REFUSED and "reason=draws_mismatch" in err  # a is fine as a replay entry; b's rewrite is not new
    assert world.ledger().batch_declaration() is None


def test_declare_batch_refuses_a_candidate_whose_draws_were_scored_by_another_scorer(world, monkeypatch):
    ready(world, monkeypatch)
    a = "1" * 64
    record_draws(world.ledger(), "f" * 64)
    record_draws(world.ledger(), a, mode="replay", scorer="9" * 64)
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_mismatch" in err and world.ledger().batch_declaration() is None


def test_declare_batch_declares_a_batch_whose_draws_are_the_modes_it_names(world, monkeypatch):
    ready(world, monkeypatch)
    a, b, c = "1" * 64, "2" * 64, "3" * 64
    rewrite = "7" * 64
    record_draws(world.ledger(), a, mode="replay")
    record_draws(world.ledger(), b, rewrite=rewrite)
    record_draws(world.ledger(), c, n=2)  # partly drawn: abandoned
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{b}:candidate:{rewrite}",
                               "--candidate", f"{c}:abandoned")
    assert code == 0, err
    assert [e["expansion"] for e in world.ledger().batch_declaration()["candidates"]] == ["replay", "candidate", "abandoned"]


# =================================================================================================================================
# the registered values of this round
# =================================================================================================================================
NEW = {
    "adoption.confirmatory_batch.modes": ["replay", "candidate", "abandoned"],
    "adoption.confirmatory_batch.mode_binding":
        "replay_entries_need_replay_draws_candidate_entries_need_build_draws_and_a_new_rewrite_identity",
    "adoption.development_evidence.fallback_draws": "void_logged_not_counted",
    "adoption.scoring.compare": "candidate_and_slot0_equal_and_counted_draws_equal_incumbent_draws",
    "adoption.exclusions":
        "confirmatory_comparison_uses_exclusions_recorded_before_slot0_store_plus_each_confirmatory_slots_own_s_hit_exclusions_applied_to_both_arms",
    "incumbent.config_fn": "pipeline_config_v4",
    "incumbent.code_scripts": ["scan_leaks.py", "sp_launch.py"],
    "incumbent.code_scripts_inert_imports": ["sp_launch.py:sealed_tool.py"],
    "incumbent.candidates": "batch_entry_config_hash_is_pipeline_config_v4_of_the_candidate",
    "adoption.confirmatory_batch.candidate_identity": "config_hash_is_pipeline_config_v4_checked_by_16b_gate_before_claim",
}


@pytest.mark.parametrize("path", sorted(NEW))
def test_the_r7_registered_values_are_in_the_table_and_the_real_prereg_and_each_is_checked(path):
    doc = real_prereg()
    value = NEW[path]
    assert st.REGISTERED[path] == value and st._eq(st._at(doc, path), value), path
    changed = [*value, "x"] if isinstance(value, list) else value + "_x"
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, changed), recompute=False)
    assert (path, "mismatch") in st.validate_prereg(put(doc, path, KeyError), recompute=False)


def test_the_computed_values_of_the_round_are_the_codes_and_a_mismatch_is_refused_by_prereg_check(monkeypatch):
    doc = real_prereg()
    assert st.computed_errors(doc) == [] and st.validate_prereg(doc) == [] and st.pinned_errors(doc) == []
    assert doc["judge"]["covers"][-2:] == ["client_builder", "metering_wrapper"] and "judge_runtime" not in doc["judge"]["covers"]  # r8
    assert doc["judge"]["config_hash"] == judge.judge_identity()["config_hash"]
    assert doc["adoption"]["scoring"]["covers"] == scoring_identity.covered() and "scoring_glue.py" in doc["adoption"]["scoring"]["covers"]
    assert doc["incumbent"]["components"] == list(pipeline_config.PIPELINE_CONFIG_KEYS)
    for path, other in (
        ("incumbent.code_scripts", ["scan_leaks.py"]), ("incumbent.code_scripts_inert_imports", []),
        ("incumbent.code_scripts_inert_imports", ["sp_launch.py:sealed_tool.py", "scan_leaks.py:sealed_tool.py"]),
        ("judge.covers", list(judge.JUDGE_COVERS)[:-1]), ("adoption.scoring.covers", scoring_identity.covered()[:-1]),
    ):
        assert (path, "mismatch") in st.computed_errors(put(doc, path, other)), path
    monkeypatch.setattr(pipeline_config, "HASHED_SCRIPT_IMPORTS_INERT", (*pipeline_config.HASHED_SCRIPT_IMPORTS_INERT, "x.py:y.py"))
    assert ("incumbent.code_scripts_inert_imports", "mismatch") in st.computed_errors(doc)  # the code grew an allowance, the prereg did not
    monkeypatch.undo()
    monkeypatch.setattr(pipeline_config, "HASHED_SCRIPTS", ("scan_leaks.py",))
    assert ("incumbent.code_scripts", "mismatch") in st.computed_errors(doc)


def test_an_unregistered_leaf_under_the_new_blocks_is_refused():
    for path in ("adoption.exclusions_extra", "adoption.confirmatory_batch.extra", "incumbent.extra", "adoption.development_evidence.extra"):
        assert st.validate_prereg(put(real_prereg(), path, 1), recompute=False), path
    doc = real_prereg()
    del doc["adoption"]["exclusions"]
    assert ("adoption.exclusions", "mismatch") in st.validate_prereg(doc, recompute=False)
