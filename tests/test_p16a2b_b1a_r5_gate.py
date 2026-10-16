"""16A-2b b1a closure round r5, the slot-gate parts: the results document records the scorer version and the judge hash (R4)
and the incumbent is re-checked where it matters: by the evaluator's own token check before any call, and when the results
are stored (R6).

Synthetic data only (``tests/p16_slot.py``; the world patches ``pipeline_config.index_identity`` to a fixed value, so its
pipeline hash is the real code's plus that index, and ``record_controls`` records it as the incumbent).
"""

from __future__ import annotations

import json

import pytest

import src.evaluator as ev
from src import judge, pipeline_config, sealed_run
from src.eval_cohort import SCORER_VERSION
from src.sealed_ledger import RecoveryAction
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    WORLD_INDEX,
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    no_calls,
    world,
)
from tests.test_p16a2b_b1a_r4_gate import claims, refusal

OTHER_INDEX = "2" * 64


def check_args(world, token, **over):
    """The evaluator's own call of ``check_token_for_run`` for the world's default run."""
    args = dict(
        set_paths=[str(world.sealed_path)], expansion_mode="live", artifact_path=None, meter=None, top_k=6,
        persist_directory="unused", judge=True, judge_sample=None,
    )
    args.update(over)
    return lambda: sealed_run.check_token_for_run(token, **args)


# --- R4: the stored results carry the scorer version and the judge hash -------------------------------------------------------------
def test_the_results_document_carries_the_scorer_version_and_the_judge_hash(world):
    outcome = world.run(0)
    assert outcome.canonical is True
    doc = world.stored_doc(0)
    rows = json.loads((world.run_dir(doc["run_id"]) / "rows.json").read_text())
    assert doc["scorer_version"] == SCORER_VERSION == rows["scorer_version"]  # the value the draw sidecars carry
    assert doc["judge"] == {"config_hash": judge.judge_identity()["config_hash"], "sample": None}
    assert doc["judge"]["config_hash"] == doc["facts"]["judge_config_hash"]
    assert doc["pipeline_config_hash"] == world.config_hash()


# --- R6: the evaluator's own token check recomputes the incumbent ----------------------------------------------------------------
def test_the_token_check_recomputes_the_pipeline_hash_for_slot_0_and_refuses_another_before_any_other_check(world, monkeypatch):
    token = world.token(0, claim=False)
    try:
        # the running pipeline is the incumbent: the incumbent check passes and the next one (the meter, here None) refuses
        assert refusal(check_args(world, token)) == ("meter", None)
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)
        assert refusal(check_args(world, token)) == ("incumbent", None)
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: None)  # no index at all
        assert refusal(check_args(world, token)) == ("incumbent", None)
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: WORLD_INDEX)
        assert refusal(check_args(world, token)) == ("meter", None)
        # the hash is computed at the run's top_k and persist directory: the cut-off gate still comes first
        assert refusal(check_args(world, token, top_k=3)) == ("cutoff", None)
    finally:
        token.release()


def test_the_token_check_holds_slot_0b_to_the_incumbent_too(world, monkeypatch):
    assert world.run(0, skip_refusals=True).canonical is False  # a non-canonical slot 0 makes 0b available
    token = world.token("0b", claim=False)
    try:
        assert refusal(check_args(world, token)) == ("meter", None)
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)
        assert refusal(check_args(world, token)) == ("incumbent", None)
    finally:
        token.release()


def test_the_token_check_does_not_apply_the_incumbent_to_a_confirmatory_slot(world, monkeypatch):
    """A confirmatory slot's candidate is checked by 16B's identity check, not by the incumbent."""
    from src.expansion_artifact import rewrite_identity
    from tests.p16_slot import candidate_entry

    rewrite = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry("9" * 64, rewrite)])
    token = world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False)
    try:
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)
        assert refusal(check_args(world, token)) == ("meter", None)  # not "incumbent"
    finally:
        token.release()


def test_a_pipeline_that_changes_after_the_preflight_is_refused_by_the_evaluator_before_any_call(world, monkeypatch, no_calls):
    """The preflight saw the incumbent; the pipeline changed before the evaluator started. The evaluator's own check
    refuses, before a retrieval, a rewrite, a generation or a judge call (the slot is claimed by then, so it is spent)."""
    real = sealed_run.preflight_slot

    def preflight_then_change(*args, **kwargs):
        receipt = real(*args, **kwargs)
        monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)
        return receipt

    monkeypatch.setattr(sealed_run, "preflight_slot", preflight_then_change)
    assert refusal(lambda: world.run(0)) == ("incumbent", None)
    assert [c["slot"] for c in claims(world)] == ["0"]
    assert no_calls == ["run", "run"]  # the preflight and the refused run: no expansion (rewrite) call in between
    assert world.ledger().recover(0).action is RecoveryAction.SPENT  # nothing stored


# --- R6: nothing is stored under another pipeline than the incumbent --------------------------------------------------------------
def test_results_that_name_another_pipeline_than_the_incumbent_are_not_stored(world, monkeypatch):
    monkeypatch.setattr(ev, "_pipeline_config_hash", lambda persist_directory, top_k: "3" * 64)  # only the stored document's
    assert refusal(lambda: world.run(0)) == ("incumbent", None)
    assert world.ledger().recover(0).action is RecoveryAction.SPENT
    assert "slot_stored" not in kinds(world) and "publication_intent" not in kinds(world)


def test_a_results_document_with_no_pipeline_hash_is_not_stored_either(world, monkeypatch):
    monkeypatch.setattr(ev, "_pipeline_config_hash", lambda persist_directory, top_k: None)
    assert refusal(lambda: world.run(0)) == ("incumbent", None)
    assert "slot_stored" not in kinds(world)


def test_check_stored_incumbent_on_its_own(world, tmp_path):
    ledger = world.ledger()
    incumbent = ledger.incumbent_config_hash()
    assert incumbent == world.config_hash()

    def stored(value, name):
        run_dir = tmp_path / name
        run_dir.mkdir()
        (run_dir / sealed_run.RESULTS_FILE).write_text(json.dumps(
            {"version": 1, "kind": "sealed_slot_results", "pipeline_config_hash": value}), encoding="utf-8")
        return run_dir

    for slot in ("0", "0b"):
        sealed_run.check_stored_incumbent(slot, ledger, stored(incumbent, f"ok{slot}"))
        for bad in ("4" * 64, None):
            assert refusal(lambda: sealed_run.check_stored_incumbent(slot, ledger, stored(bad, f"bad{slot}{bad is None}"))) == (
                "incumbent", None)
    assert refusal(lambda: sealed_run.check_stored_incumbent("0", ledger, tmp_path / "nowhere")) == ("incumbent", None)  # unreadable
    sealed_run.check_stored_incumbent("1", ledger, stored("4" * 64, "candidate"))  # a candidate's results are 16B's to check


def test_slot_0_under_the_incumbent_is_stored_as_before(world):
    assert world.run(0).canonical is True
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED
