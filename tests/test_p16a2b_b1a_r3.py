"""16A-2b b1a closure round r3, the slot-gate and ledger parts: a public regression set beside the development sets (F1),
no eager claim (F3), controls before slot 0 (F6), the batch entry's two identities (F9), the identity-check stub that
keeps the embargo from being lifted by a boolean alone (F11).

Synthetic data only (``tests/p16_slot.py``). The ``sealed_tool`` parts are in ``tests/test_p16a2b_b1a_r3_tool.py`` (the
two worlds define a fixture of the same name), the launcher's in ``tests/test_p16a2b_b1a_r3_launch.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import eval_sets, sealed_ledger, sealed_run
from src.expansion_artifact import rewrite_identity
from src.sealed_ledger import SealedLedger
from tests.p16_draws import declare
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    BATCH,
    _langsmith_env_cache,
    bare,
    candidate_entry,
    gate_of,
    kinds,
    sha_of,
    world,
)

REAL_CHECK_CANDIDATE_IDENTITY = sealed_run.check_candidate_identity  # captured before any fixture replaces it
D = "a" * 64
PIPELINE = "9" * 64  # a candidate's pipeline configuration hash: not its rewrite identity


# --- F1: a registered public regression set beside the two development sets -----------------------------------------------
def _register_public_regression_set(world, tmp_path: Path) -> Path:
    """A small valid set outside the private root, registered public (what slot 0's ``--regression`` set is)."""
    rows = []
    for i in range(3):
        question = world.canaries.question(f"reg{i}")
        rows.append({
            "schema": 2, "id": f"f4000000{i}-0", "family_id": f"f4000000{i}", "question": question, "scope": "answer",
            "evidence": [["901.1"]],
        })
        world.section_of[question] = "901.1"
    path = tmp_path / "public" / "regression.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    registry = tmp_path / "_registry" / "sets.json"
    registry.write_text(json.dumps({"version": 1, "sets": [{
        "name": "regression-public", "path": str(path), "privacy": "public", "role": "regression", "status": "active",
        "sha256": sha_of(path),
    }]}), encoding="utf-8")
    assert eval_sets.classify(path) == eval_sets.PUBLIC
    return path


def test_a_slot_0_run_with_a_public_regression_set_stores_its_results_and_binds_only_the_development_sets(world, tmp_path):
    regression = _register_public_regression_set(world, tmp_path)
    specs = [*world.specs, ("regression", str(regression))]
    outcome = world.run(0, specs=specs)
    assert "slot_stored" in kinds(world) and outcome.action  # the slot was not spent without storing its results
    doc = world.stored_doc(0)
    assert doc["dev_sets"] == {"tuning": sha_of(world.golden_path), "realistic": sha_of(world.realistic_path)}
    # it ran, and is not bound: r4 D8, a public non-sealed set's stored role is "regression" (only the frozen tuning and
    # realistic versions are "development")
    assert {s["label"] for s in doc["sets"] if s["role"] == "regression"} == {"regression"}
    assert {s["label"] for s in doc["sets"] if s["role"] == "development"} == {"tuning", "realistic"}
    assert {s["label"] for s in doc["sets"] if s["role"] == "sealed"} == {"held-out"}


def test_a_changed_private_development_set_is_still_refused_beside_a_public_one(world, tmp_path):
    regression = _register_public_regression_set(world, tmp_path)
    token = world.token(0, claim=False)
    lines = world.realistic_path.read_text().splitlines()
    row = json.loads(lines[0])
    row["question"] += " again"
    world.realistic_path.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    assert gate_of(lambda: world.run(0, token=token, specs=[*world.specs, ("regression", str(regression))])) == "dev_binding"
    assert "slot_claim" not in kinds(world)


# --- F3: the gate never claims --------------------------------------------------------------------------------------------
def test_an_eager_claim_is_refused_and_leaves_no_claim_record(world):
    for top_k in (6, None, 3):
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.acquire_slot_token(
                0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
                records_dir=world.records_dir, expansion="live", meter=meter(), claim=True, top_k=top_k,
            )
        assert info.value.gate == "claim"
    assert "slot_claim" not in kinds(world)
    assert isinstance(world.token(0, claim=False), sealed_run.SlotToken)  # the slot is still free (its lock was not kept)


def test_bad_development_inputs_with_an_eager_claim_leave_no_claim_record(world):
    token = world.token(0, claim=False)  # minted as the CLI does; the run's preflight (not the gate) sees the inputs
    lines = world.golden_path.read_text().splitlines()
    row = json.loads(lines[0])
    row["question"] += " again"  # no longer the tuning freeze record's version
    world.golden_path.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    # the eager claim is refused outright (it would claim before the development-input check)
    assert gate_of(lambda: sealed_run.acquire_slot_token(
        0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
        records_dir=world.records_dir, expansion="live", meter=meter(), claim=True, top_k=6,
    )) == "claim"
    assert "slot_claim" not in kinds(world)
    # and the path that does claim, run_slot, refuses the same inputs first
    assert gate_of(lambda: world.run(0, token=token)) == "dev_binding"
    assert "slot_claim" not in kinds(world)


def test_a_judge_off_run_never_reaches_a_claim_even_through_the_helper_path(world):
    config_hash = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, config_hash)])
    assert gate_of(lambda: sealed_run.acquire_slot_token(
        1, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path, records_dir=world.records_dir,
        expansion="live", rewrite_candidate=config_hash, meter=meter(), baseline_run=r0, claim=True, top_k=6,
    )) == "claim"
    assert not any(r["kind"] == "slot_claim" and r["slot"] == "1" for r in world.ledger().records())


def meter():
    from tests.test_p16w_spend import make_meter

    return make_meter()


# --- F6: the controls are recorded before slot 0 ------------------------------------------------------------------------
@pytest.mark.parametrize("slot", [0, "0b"])
def test_slots_0_and_0b_need_the_controls_record_and_the_unchanged_file(bare, slot):
    bare.write_prereg()
    bare.write_records()
    bare.freeze(controls=False)  # the three freezes, but controls.py has not run
    assert gate_of(lambda: bare.token(slot, claim=False)) == "controls"
    assert "slot_claim" not in kinds(bare)
    bare.record_controls()
    if slot == 0:
        bare.token(0, claim=False).release()  # the record is there: the gate goes on and mints the token
    sealed_run.controls_path().write_text('{"version": 2}\n', encoding="utf-8")  # the list changed after its record
    assert gate_of(lambda: bare.token(slot, claim=False)) == "controls"
    sealed_run.controls_path().unlink()  # or went missing
    assert gate_of(lambda: bare.token(slot, claim=False)) == "controls"
    assert "slot_claim" not in kinds(bare)


def test_controls_are_checked_after_the_freeze_gates_so_an_unfrozen_epoch_says_so_first(bare):
    bare.write_prereg()
    bare.write_records()  # nothing frozen, no controls: the freeze record is the first thing missing
    assert gate_of(lambda: bare.token(0, claim=False)) == "freeze_record"


# --- F9: the batch entry carries two identities ---------------------------------------------------------------------------
def test_a_candidate_entrys_pipeline_hash_is_carried_and_its_rewrite_identity_is_checked(world):
    rewrite = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, rewrite)])
    assert world.ledger().batch_declaration()["candidates"] == [
        {"config_hash": PIPELINE, "expansion": "candidate", "rewrite_config_hash": rewrite},
    ]
    token = world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False)
    assert (token.candidate_config_hash, token.candidate_rewrite_config_hash) == (PIPELINE, rewrite)
    token.release()
    # the stored results name both
    outcome = world.run(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0)
    doc = json.loads((world.run_dir(outcome.run_id) / "results.json").read_text())
    assert (doc["candidate_config_hash"], doc["candidate_rewrite_config_hash"]) == (PIPELINE, rewrite)


def test_a_candidate_entry_whose_rewrite_identity_is_not_this_codes_is_refused_at_the_gate(world):
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, "b" * 64)])  # a rewrite configuration this code does not have
    for flag in ("b" * 64, PIPELINE, rewrite_identity()["config_hash"]):
        assert gate_of(lambda: world.token(1, expansion="live", rewrite_candidate=flag, baseline_run=r0, claim=False)) == (
            "expansion_mode"
        )
    assert "slot_claim" not in [r["kind"] for r in world.ledger().records() if r.get("slot") == "1"]


def test_the_ledger_digests_the_batch_with_its_rewrite_identities(tmp_path):
    led = SealedLedger(tmp_path / "copy", mirror=tmp_path / "mirror.jsonl")
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=D)
    entries = [candidate_entry(PIPELINE, D), BATCH[1]]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
        digest = declare(led, entries)
    import hashlib

    want = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == want and led.batch_declaration()["candidates"] == entries
    mirror = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[-1])
    assert mirror["digests"] == {"candidates_sha256": digest} and D not in json.dumps(mirror["digests"])


# --- F11: lifting the embargo flag alone does not open a confirmatory slot ------------------------------------------------
def test_with_the_embargo_off_a_confirmatory_slot_is_still_refused_identity(world, monkeypatch):
    rewrite = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, rewrite)])
    assert sealed_ledger.CONFIRMATORY_EMBARGO is False  # the world lifts it, as 16B's change would
    world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False).release()  # the pass-through
    monkeypatch.setattr(sealed_run, "check_candidate_identity", REAL_CHECK_CANDIDATE_IDENTITY)  # the code as shipped
    assert gate_of(lambda: world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False)) == "identity"
    assert gate_of(lambda: world.run(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0)) == "identity"
    assert not any(r["kind"] == "slot_claim" and r["slot"] == "1" for r in world.ledger().records())
    assert "identity" in sealed_run.GATES


def test_the_identity_stub_refuses_for_any_input_and_the_descriptive_slots_never_reach_it(world, monkeypatch):
    with pytest.raises(sealed_run.GateRefused) as info:
        REAL_CHECK_CANDIDATE_IDENTITY("1", world.ledger(), PIPELINE, "replay", None)
    assert info.value.gate == "identity"
    monkeypatch.setattr(sealed_run, "check_candidate_identity", REAL_CHECK_CANDIDATE_IDENTITY)
    assert isinstance(world.token(0, claim=False), sealed_run.SlotToken)  # slot 0 is not a candidate slot
