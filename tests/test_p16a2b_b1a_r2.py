"""16A-2b b1a closure round r2, the slot-gate and ledger parts: one registered prereg per epoch, the 16B embargo,
per-slot modes from the batch, the judge and controls preconditions, and the development inputs bound at slot time.

Synthetic data only (``tests/p16_slot.py``). The ``sealed_tool`` parts are in ``tests/test_p16a2b_b1a_r2_tool.py``
(the two worlds define a fixture of the same name, so they cannot share a module).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import sealed_ledger, sealed_run
from src.expansion_artifact import rewrite_identity
from src.sealed_ledger import ProtocolError, SealedLedger, SlotRefused
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    BATCH,
    BATCH_SLOTS,
    BOUND_COMMIT,
    BOUND_PREREG_SHA,
    CANDIDATES,
    _langsmith_env_cache,
    bare,
    candidate_entry,
    gate_of,
    kinds,
    sha_of,
    world,
)

D = "a" * 64
PIPELINE = "9" * 64  # a candidate's pipeline configuration hash (not its rewrite identity)


def make(tmp_path: Path) -> SealedLedger:
    return SealedLedger(tmp_path / "copy", mirror=tmp_path / "mirror.jsonl")


# --- R1: the epoch's registered prereg (ledger) ---------------------------------------------------------------------
def test_a_split_attempt_keeps_an_optional_prereg_sha_in_the_ledger_and_the_mirror(tmp_path):
    led = make(tmp_path)
    led.record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible")
    led.record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible", prereg_sha256="b" * 64)
    first, second = led.records("split_attempt")
    assert "prereg_sha256" not in first and second["prereg_sha256"] == "b" * 64
    lines = [json.loads(line) for line in (tmp_path / "mirror.jsonl").read_text().splitlines()]
    assert "prereg_sha256" not in lines[0]["digests"] and lines[1]["digests"]["prereg_sha256"] == "b" * 64
    for bad in ("nothex", "B" * 64, "b" * 63):
        with pytest.raises(ValueError):
            led.record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible", prereg_sha256=bad)


def test_the_epochs_prereg_is_the_first_record_of_any_kind_that_carries_one(tmp_path):
    led = make(tmp_path)
    assert led.epoch_prereg_sha256() is None
    led.record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible")  # carries none
    assert led.epoch_prereg_sha256() is None
    led.record_freeze(split="tuning", set_sha256=D, totals={}, prereg_sha256="1" * 64)  # a freeze registers it
    led.record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible", prereg_sha256="2" * 64)
    led.record_freeze(split="realistic", set_sha256=D, totals={}, prereg_sha256="3" * 64)
    assert led.epoch_prereg_sha256() == "1" * 64 and make(tmp_path).epoch_prereg_sha256() == "1" * 64


# --- R1: the slot gate --------------------------------------------------------------------------------------------------
def test_the_slot_gate_needs_the_bound_prereg_to_be_the_freezes_and_the_epochs(world):
    assert world.token(0, claim=False).prereg_sha256 == BOUND_PREREG_SHA  # the original passes
    replaced = "e" * 64  # a pushed replacement prereg: committed, unmodified, on the remote, but other bytes
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_run, "bind_prereg", lambda path: (BOUND_COMMIT, replaced))
        assert gate_of(lambda: world.token(0, claim=False)) == "prereg_binding"
    assert "slot_claim" not in kinds(world)
    assert isinstance(world.token(0, claim=False), sealed_run.SlotToken)


def test_the_slot_gate_refuses_a_prereg_that_is_the_freezes_but_not_the_epochs(bare):
    bare.write_prereg()
    bare.write_records()
    bare.ledger().record_split_attempt(round_no=1, pool_digest=D, outcome="infeasible", prereg_sha256="9" * 64)
    bare.freeze()  # the freezes are made under BOUND_PREREG_SHA, which the first attempt did not register
    assert bare.ledger().epoch_prereg_sha256() == "9" * 64
    assert gate_of(lambda: bare.token(0, claim=False)) == "prereg_binding"


# --- R3: the 16B embargo (the module default is on) -------------------------------------------------------------------
def test_the_embargo_is_on_by_default():
    assert sealed_ledger.CONFIRMATORY_EMBARGO is True


def test_the_default_embargo_refuses_a_batch_and_a_claim_of_slots_1_to_4(tmp_path):
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=D)
    with pytest.raises(ProtocolError, match="embargo"):
        led.record_batch_declaration(BATCH)
    assert led.batch_declaration() is None
    for slot in (1, 2, 3, 4, "1"):
        with pytest.raises(SlotRefused, match="embargo"):
            led.claim(slot)
        with pytest.raises(SlotRefused, match="embargo"):
            led.check_claimable(slot)
    assert led.claim(0) == "0" and led.check_claimable("0b") is None  # the descriptive slots are not embargoed


def test_the_embargo_is_read_at_call_time(tmp_path, monkeypatch):
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=D)
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    led.record_batch_declaration(BATCH[:1])
    assert led.claim(1) == "1"


def test_the_slot_gate_refuses_an_embargoed_confirmatory_slot_first(world, monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", True)
    # no batch, no controls, no baseline: the embargo is still the first refusal among the confirmatory checks
    assert gate_of(lambda: world.token(1, declare=False, claim=False)) == "embargo"
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    world.declare_batch()  # (a batch exists, as it would after 16B lifts the embargo and the embargo is put back)
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", True)
    assert gate_of(lambda: world.token(3, claim=False, baseline_run="r1")) == "embargo"
    assert isinstance(world.token(0, claim=False), sealed_run.SlotToken)  # slot 0 is not embargoed
    assert "slot_claim" not in kinds(world)


# --- R3: the prereg declares slots 1-4 as ``batch`` ------------------------------------------------------------------
def test_load_prereg_accepts_batch_only_as_a_declaration_of_the_slot0_canonical_artifact(world):
    world.write_prereg()
    assert sealed_run.load_prereg(world.prereg_path)["slots"]["1"]["expansion"] == "batch"
    ok = {"expansion": "batch", "artifact": "slot0_canonical", "role": "confirmatory", "requires": "batch_declaration"}
    for decl in (
        {"expansion": "batch"}, {**ok, "artifact": "slot7"}, {**ok, "artifact_sha256": D}, {**ok, "rewrite_candidate": D},
    ):
        world.write_prereg(slots={"0": {"expansion": "live"}, "1": decl})
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.load_prereg(world.prereg_path)
        assert info.value.gate == "prereg"


def test_a_confirmatory_slot_not_declared_batch_and_a_descriptive_slot_declared_batch_are_refused(world):
    config_hash = rewrite_identity()["config_hash"]
    for slots in (
        {"0": {"expansion": "live"}, "1": {"expansion": "replay", "artifact_sha256": D}},  # an explicit replay
        {"0": {"expansion": "live"}, "1": {"expansion": "candidate", "rewrite_candidate": config_hash}},
        {"0": {"expansion": "live"}, "1": {"expansion": "live"}},
        {"0": BATCH_SLOTS["1"], "1": BATCH_SLOTS["1"]},  # slot 0 is never a batch slot
    ):
        world.write_prereg(slots=slots)
        sid = 0 if "0" in slots and slots["0"]["expansion"] == "batch" else 1
        assert gate_of(lambda: world.token(sid, claim=False, baseline_run="r1")) == "prereg", slots
    assert "slot_claim" not in kinds(world)


# --- R3: per-slot modes from the batch ------------------------------------------------------------------------------
def test_a_candidate_entry_accepts_the_matching_rewrite_candidate_and_refuses_a_replay(world):
    config_hash = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, config_hash), {"config_hash": "2" * 64, "expansion": "replay"}])
    tok = world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0, claim=False)
    # the entry's config_hash is the candidate's pipeline configuration; the run is held to its rewrite identity
    assert tok.expansion_mode == "candidate" and tok.candidate_config_hash == PIPELINE
    assert tok.candidate_rewrite_config_hash == config_hash
    assert tok.candidate_expansion == "candidate" and tok.artifact_sha256 is None
    tok.release()
    canonical = world.run_dir(r0) / "expansion_artifact.json"
    assert gate_of(lambda: world.token(1, expansion=str(canonical), baseline_run=r0, claim=False)) == "expansion_mode"
    assert gate_of(lambda: world.token(1, expansion="live", baseline_run=r0, claim=False)) == "expansion_mode"
    assert gate_of(lambda: world.token(1, rewrite_candidate="c" * 64, baseline_run=r0, claim=False)) == "expansion_mode"
    # the pipeline hash is not the rewrite identity: naming it as --rewrite-candidate is refused
    assert gate_of(lambda: world.token(1, expansion="live", rewrite_candidate=PIPELINE, baseline_run=r0, claim=False)) == "expansion_mode"


def test_a_replay_entry_replays_the_canonical_artifact_and_refuses_a_rewrite_candidate(world):
    config_hash = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[{"config_hash": "1" * 64, "expansion": "replay"}, candidate_entry(PIPELINE, config_hash)])
    canonical = world.run_dir(r0) / "expansion_artifact.json"
    tok = world.token(1, expansion=str(canonical), baseline_run=r0, claim=False)
    assert tok.expansion_mode == "replay" and tok.candidate_expansion == "replay" and tok.candidate_config_hash == "1" * 64
    assert tok.artifact_sha256 == world.ledger().canonical_artifact_sha256()
    tok.release()
    assert gate_of(lambda: world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0, claim=False)) == "expansion_mode"
    assert gate_of(lambda: world.token(1, rewrite_candidate=config_hash, expansion=str(canonical), baseline_run=r0, claim=False)) == (
        "expansion_mode"
    )


# --- R3: the judge and the controls preconditions ---------------------------------------------------------------------
def test_a_confirmatory_slot_without_the_judge_is_refused_before_the_claim(world):
    config_hash = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, config_hash)])
    token = world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0, claim=False)  # as the CLI mints it
    assert gate_of(lambda: world.run(1, token=token, judge=False, expansion="live", rewrite_candidate=config_hash)) == "judge"
    assert not any(r["kind"] == "slot_claim" and r["slot"] == "1" for r in world.ledger().records())
    # with the judge on, the same token runs (the refusal cost no slot)
    outcome = world.run(1, token=token, judge=True, expansion="live", rewrite_candidate=config_hash)
    assert outcome.rendered


def test_the_confirmatory_gate_needs_the_controls_record_and_the_unchanged_file(world, monkeypatch):
    config_hash = rewrite_identity()["config_hash"]
    world.run(0)  # slot 0 itself needs the controls record (r3 F6), so a confirmatory slot always finds one
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, config_hash)])
    call = lambda: world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0, claim=False, declare=False)  # noqa: E731
    call().release()
    with monkeypatch.context() as m:  # defensive: a ledger without the record (cannot arise once slot 0 needs it)
        m.setattr(SealedLedger, "controls_record", lambda self: None)
        assert gate_of(call) == "controls"
    sealed_run.controls_path().write_text('{"version": 2}\n', encoding="utf-8")  # the committed list changed after its record
    assert gate_of(call) == "controls"


# --- R4: development inputs are bound at slot time -----------------------------------------------------------------
def _edit_first_question(path: Path) -> None:
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row["question"] += " again"
    path.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")


def test_a_changed_development_version_is_refused_at_the_slot_preflight_before_the_claim(world):
    token = world.token(0, claim=False)
    _edit_first_question(world.realistic_path)  # no longer the freeze record's version
    assert gate_of(lambda: world.run(0, token=token)) == "dev_binding"
    assert "slot_claim" not in kinds(world)


def test_a_development_split_given_twice_is_refused_before_the_claim(world):
    token = world.token(0, claim=False)
    specs = [*world.specs, ("tuning-again", str(world.golden_path))]  # both splits are there, the tuning one twice
    assert gate_of(lambda: world.run(0, token=token, specs=specs)) == "dev_binding"
    assert "slot_claim" not in kinds(world)


def test_the_stored_results_name_the_development_versions_the_run_was_bound_to(world):
    world.run(0)
    doc = world.stored_doc(0)
    assert doc["dev_sets"] == {"tuning": sha_of(world.golden_path), "realistic": sha_of(world.realistic_path)}


def test_dev_sets_of_maps_each_split_once_and_refuses_the_rest(world):
    ledger = world.ledger()
    tuning, realistic = sha_of(world.golden_path), sha_of(world.realistic_path)
    assert sealed_run.dev_sets_of(ledger, [realistic, tuning]) == {"tuning": tuning, "realistic": realistic}
    assert sealed_run.dev_sets_of(ledger, []) == {}
    for shas in ([tuning, tuning], ["f" * 64], [tuning, "f" * 64]):
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.dev_sets_of(ledger, shas)
        assert info.value.gate == "dev_binding"
