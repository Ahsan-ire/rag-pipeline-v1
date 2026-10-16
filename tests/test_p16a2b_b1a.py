"""16A-2b b1a: prereg completeness and binding (ledger, slot gate, launcher ancestry, prereg validation).

Synthetic data only. The ``sealed_tool`` commands (split cap, freeze, calibrate, declare-batch, power) are in
``tests/test_p16a2b_b1a_tool.py``; this file holds the parts that do not need that tool's world.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src import sealed_ledger, sealed_run
from src.expansion_artifact import rewrite_identity
from src.sealed_ledger import ProtocolError, SealedLedger, SlotRefused
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    BATCH,
    BOUND_COMMIT,
    BOUND_PREREG_SHA,
    CANDIDATES,
    _langsmith_env_cache,
    bare,
    candidate_entry,
    gate_of,
    world,
)
from tests.p16w_sealed_world import b1a_blocks
from tests.test_p16w_sp_launch import GitRepos, git, repos  # noqa: F401  (fixture)

D = "a" * 64


def make(tmp_path: Path) -> SealedLedger:
    return SealedLedger(tmp_path / "copy", mirror=tmp_path / "mirror.jsonl")


@pytest.fixture(autouse=True)
def _embargo_off(monkeypatch):
    """The 16B embargo (``CONFIRMATORY_EMBARGO``) is tested in ``tests/test_p16a2b_b1a_r2.py``; the batch and slot
    rules behind it are tested here, so it is off."""
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    # ... and so is 16B's candidate-identity check, which refuses always in this phase (tests/test_p16a2b_b1a_r3.py)
    monkeypatch.setattr(sealed_run, "check_candidate_identity", lambda *args, **kwargs: None)


def frozen(tmp_path: Path) -> SealedLedger:
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    return led


def declarable(tmp_path: Path) -> SealedLedger:
    """A ledger that is frozen and holds the controls record: what a batch declaration needs."""
    led = frozen(tmp_path)
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=D)
    return led


def canonical_json_sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_dir(tmp_path: Path, name: str, artifact: bytes | None = None) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "results.json").write_text("{}", encoding="utf-8")
    if artifact is not None:
        (d / "expansion_artifact.json").write_bytes(artifact)
    return d


# --- 1. launcher ancestry ---------------------------------------------------------------------------------------
def test_check_prereg_accepts_the_branch_or_main_and_refuses_neither(repos):
    work = repos.work
    sha = repos.commit(work, "prereg.json")
    with pytest.raises(sp.LaunchError, match="not reachable"):  # local only
        sp.check_prereg(sha, "feature", repo=work)
    git(work, "push", "--quiet", "origin", "HEAD:refs/heads/feature")
    sp.check_prereg(sha, "feature", repo=work)  # on origin/<branch>
    only_main = repos.commit(work, "later.json")
    git(work, "push", "--quiet", "origin", "main")
    sp.check_prereg(only_main, "gone-branch", repo=work)  # the branch has no ref (deleted): origin/main carries it
    with pytest.raises(sp.LaunchError):
        sp.check_prereg(repos.commit(work, "unpushed.json"), "gone-branch", repo=work)


# --- 2. ledger --------------------------------------------------------------------------------------------------
def test_batch_declaration_rules(tmp_path):
    led = make(tmp_path)
    with pytest.raises(ProtocolError):  # needs the sealed freeze
        led.record_batch_declaration(BATCH)
    led = frozen(tmp_path)
    with pytest.raises(ProtocolError, match="controls"):  # and the controls record
        led.record_batch_declaration(BATCH)
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=D)
    one = {"config_hash": CANDIDATES[0], "expansion": "replay"}
    bad_entries = [
        [], BATCH + [{"config_hash": "5" * 64, "expansion": "replay"}], [one, dict(one)],  # none, five, a duplicate hash
        [{"config_hash": "A" * 64, "expansion": "replay"}], [{"config_hash": "short", "expansion": "replay"}],
        [{"config_hash": CANDIDATES[0], "expansion": "live"}],  # not a batch mode
        [{"config_hash": CANDIDATES[0]}], [{"expansion": "replay"}], [{**one, "extra": 1}],  # not {config_hash, expansion}
        # r3 F9: a candidate entry carries its rewrite identity (64 hex), a replay entry carries none
        [{"config_hash": CANDIDATES[0], "expansion": "candidate"}],
        [{"config_hash": CANDIDATES[0], "expansion": "candidate", "rewrite_config_hash": "short"}],
        [{"config_hash": CANDIDATES[0], "expansion": "candidate", "rewrite_config_hash": "A" * 64}],
        [{**one, "rewrite_config_hash": CANDIDATES[1]}],
        [{"config_hash": CANDIDATES[0], "expansion": "candidate", "rewrite_config_hash": CANDIDATES[1], "extra": 1}],
        CANDIDATES,  # the old form: bare hashes
        "1" * 64,
    ]
    for bad in bad_entries:
        with pytest.raises(ValueError):
            led.record_batch_declaration(bad)
    assert led.batch_declaration() is None
    entries = [BATCH[0], candidate_entry(CANDIDATES[1], D), BATCH[2]]
    digest = led.record_batch_declaration(entries)
    assert digest == canonical_json_sha(entries)  # the digest of the canonical JSON of the list, modes included
    rec = make(tmp_path).batch_declaration()
    assert rec["candidates"] == entries and rec["candidates_sha256"] == digest
    with pytest.raises(ProtocolError):  # appendable once
        led.record_batch_declaration(BATCH[:2])


def test_the_mirror_carries_the_batch_digest_only(tmp_path):
    led = declarable(tmp_path)
    digest = led.record_batch_declaration(BATCH)
    line = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[-1])
    assert line["kind"] == "batch_declaration" and line["digests"] == {"candidates_sha256": digest}
    assert "candidates" not in line and CANDIDATES[0] not in json.dumps(line)


def test_no_batch_after_a_confirmatory_claim_and_claims_follow_the_batch(tmp_path):
    led = declarable(tmp_path)
    for slot in (1, 4):
        with pytest.raises(SlotRefused):  # no declaration yet
            led.claim(slot)
    assert led.claim(0) == "0"  # slots 0 and 0b never need one
    led.record_batch_declaration(BATCH[:2])
    assert led.claim(2) == "2"
    with pytest.raises(SlotRefused):
        led.claim(3)  # beyond the declared candidates
    with pytest.raises(SlotRefused):
        led.check_claimable(4)
    led.check_claimable(1)


def test_no_batch_can_be_declared_once_a_confirmatory_slot_was_claimed(tmp_path, monkeypatch):
    led = declarable(tmp_path)
    monkeypatch.setattr(sealed_ledger, "_batch_of", lambda records: {"candidates": BATCH})  # claim 1 without a batch
    led.claim(1)
    monkeypatch.undo()
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    with pytest.raises(ProtocolError, match="confirmatory slot"):
        led.record_batch_declaration(BATCH[:1])


def test_slot_stored_records_the_expansion_artifact_sha_for_slots_0_and_0b_only(tmp_path):
    led = declarable(tmp_path)
    led.record_batch_declaration(BATCH)
    art = b'{"expansions": []}'
    led.claim(0)
    led.store_results(0, run_dir(tmp_path, "r0", art))
    stored = led.records("slot_stored")[0]
    assert stored["expansion_artifact_sha256"] == hashlib.sha256(art).hexdigest()
    mirror = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[-1])
    assert mirror["digests"]["expansion_artifact_sha256"] == stored["expansion_artifact_sha256"]
    led.claim(1)
    led.store_results(1, run_dir(tmp_path, "r1", art))  # a confirmatory slot's file is not recorded
    assert "expansion_artifact_sha256" not in led.records("slot_stored")[1]


def test_slot_stored_has_no_artifact_field_when_the_run_built_none(tmp_path):
    led = frozen(tmp_path)
    led.claim(0)
    led.store_results(0, run_dir(tmp_path, "r0"))
    assert "expansion_artifact_sha256" not in led.records("slot_stored")[0]


def test_canonical_artifact_sha256_follows_the_slot_with_a_publication_intent(tmp_path):
    led = frozen(tmp_path)
    assert led.canonical_artifact_sha256() is None  # nothing run
    led.claim(0)
    led.store_results(0, run_dir(tmp_path, "r0", b"zero"))
    assert led.canonical_artifact_sha256() is None  # stored, no intent: not canonical
    led.record_consumed(0)  # slot 0 ended non-canonical
    led.claim("0b")
    led.store_results("0b", run_dir(tmp_path, "rb", b"zero-b"))
    assert led.canonical_artifact_sha256() is None
    led.record_intent("0b")
    assert led.canonical_artifact_sha256() == hashlib.sha256(b"zero-b").hexdigest()


def test_canonical_artifact_sha256_is_none_for_a_canonical_run_that_built_none(tmp_path):
    led = frozen(tmp_path)
    led.claim(0)
    led.store_results(0, run_dir(tmp_path, "r0"))
    led.record_intent(0)
    assert led.canonical_artifact_sha256() is None


def test_a_freeze_keeps_an_optional_prereg_sha_in_the_ledger_and_the_mirror(tmp_path):
    led = make(tmp_path)
    led.record_freeze(split="tuning", set_sha256=D, totals={}, prereg_sha256="b" * 64)
    led.record_freeze(split="realistic", set_sha256=D, totals={})
    assert led.freeze_record("tuning")["prereg_sha256"] == "b" * 64
    assert "prereg_sha256" not in led.freeze_record("realistic")
    first = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[0])
    assert first["digests"]["prereg_sha256"] == "b" * 64
    with pytest.raises(ValueError):
        led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={}, prereg_sha256="nothex")


def test_a_second_or_late_batch_record_in_the_file_is_corruption(tmp_path):
    records = [
        {"v": 1, "seq": 0, "kind": "batch_declaration", "candidates": CANDIDATES},
        {"v": 1, "seq": 1, "kind": "batch_declaration", "candidates": CANDIDATES},
    ]
    with pytest.raises(sealed_ledger.LedgerCorrupt):
        sealed_ledger._replay(records)
    late = [{"v": 1, "seq": 0, "kind": "slot_claim", "slot": "1"}, {"v": 1, "seq": 1, "kind": "batch_declaration"}]
    with pytest.raises(sealed_ledger.LedgerCorrupt):
        sealed_ledger._replay(late)


# --- 3. slot gate: prereg forms, canonical artifact, binding, batch ----------------------------------------------
def _slots(**decl1):
    return {"0": {"expansion": "live"}, "0b": {"expansion": "live"}, "1": {"expansion": "replay", **decl1}}


def test_load_prereg_replay_declares_exactly_one_artifact_form(world):
    world.write_prereg(slots=_slots(artifact_sha256=D))
    assert sealed_run.load_prereg(world.prereg_path)["slots"]["1"]["artifact_sha256"] == D
    world.write_prereg(slots=_slots(artifact="slot0_canonical"))
    assert sealed_run.load_prereg(world.prereg_path)["slots"]["1"]["artifact"] == "slot0_canonical"
    for decl in ({}, {"artifact": "slot0_canonical", "artifact_sha256": D}, {"artifact": "slot7"}, {"artifact_sha256": "xyz"}):
        world.write_prereg(slots=_slots(**decl))
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.load_prereg(world.prereg_path)
        assert info.value.gate == "prereg"


def test_slot0_canonical_resolves_through_the_ledger(tmp_path):
    art = tmp_path / "expansion_artifact.json"
    art.write_text('{"x": 1}', encoding="utf-8")
    sha = hashlib.sha256(art.read_bytes()).hexdigest()
    decl = {"expansion": "replay", "artifact": "slot0_canonical"}

    class L:
        def __init__(self, value, batch=None):
            self.value, self.batch = value, batch

        def canonical_artifact_sha256(self):
            return self.value

        def batch_declaration(self):
            return self.batch

    assert sealed_run._check_expansion(decl, str(art), None, L(sha)) == ("replay", sha, str(art), None, None, None)
    for ledger in (L(None), L("f" * 64), None):  # no canonical artifact, a different one, no ledger
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run._check_expansion(decl, str(art), None, ledger)
        assert info.value.gate == "expansion_mode"
    explicit = {"expansion": "replay", "artifact_sha256": sha}
    assert sealed_run._check_expansion(explicit, str(art), None)[1] == sha  # the explicit form is unchanged


def test_a_batch_declaration_resolves_the_slots_mode_from_the_ledgers_batch(tmp_path):
    art = tmp_path / "expansion_artifact.json"
    art.write_text('{"x": 1}', encoding="utf-8")
    sha = hashlib.sha256(art.read_bytes()).hexdigest()
    decl = {"expansion": "batch", "artifact": "slot0_canonical", "role": "confirmatory", "requires": "batch_declaration"}
    entries = [{"config_hash": CANDIDATES[0], "expansion": "replay"}]
    candidate = candidate_entry(CANDIDATES[1], rewrite_identity()["config_hash"])

    class L:
        def __init__(self, batch):
            self.batch = batch

        def canonical_artifact_sha256(self):
            return sha

        def batch_declaration(self):
            return self.batch

    ledger = L({"candidates": entries})
    assert sealed_run._check_expansion(decl, str(art), None, ledger, "1") == ("replay", sha, str(art), CANDIDATES[0], "replay", None)
    # a candidate entry: the run is checked against its REWRITE identity; the pipeline config hash is only carried
    both = L({"candidates": [candidate]})
    assert sealed_run._check_expansion(decl, "live", candidate["rewrite_config_hash"], both, "1") == (
        "candidate", None, None, CANDIDATES[1], "candidate", candidate["rewrite_config_hash"])
    for flag in (CANDIDATES[1], "c" * 64):  # the pipeline hash is not the rewrite identity
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run._check_expansion(decl, "live", flag, both, "1")
        assert info.value.gate == "expansion_mode"
    for ledger, slot in ((L(None), "1"), (L({"candidates": entries}), "2"), (L({"candidates": entries}), "0"),
                         (L({"candidates": entries}), None), (None, "1")):  # no batch, no entry, not a batch slot
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run._check_expansion(decl, str(art), None, ledger, slot)
        assert info.value.gate == "batch"
    with pytest.raises(sealed_run.GateRefused) as info:  # a replay entry run live
        sealed_run._check_expansion(decl, "live", None, L({"candidates": entries}), "1")
    assert info.value.gate == "expansion_mode"


def test_the_gate_binds_the_prereg_first_and_the_token_and_results_carry_it(world, monkeypatch):
    tok = world.token(0, claim=False)
    assert (tok.prereg_commit, tok.prereg_sha256) == (BOUND_COMMIT, BOUND_PREREG_SHA) and tok.candidate_config_hash is None
    tok.release()
    outcome = world.run(0)
    doc = json.loads((world.run_dir(outcome.run_id) / "results.json").read_text())
    assert doc["prereg_commit"] == BOUND_COMMIT and doc["prereg_sha256"] == BOUND_PREREG_SHA
    assert doc["candidate_config_hash"] is None

    def refuse(path):
        raise sealed_run.GateRefused("prereg_binding")

    monkeypatch.setattr(sealed_run, "bind_prereg", refuse)
    assert gate_of(lambda: world.token(9, claim=False)) == "prereg_binding"  # slot 9 is undeclared: binding came first
    monkeypatch.undo()


def test_confirmatory_slots_need_a_batch_declaration_and_carry_their_candidate(bare):
    config_hash = rewrite_identity()["config_hash"]
    bare.write_prereg()
    bare.write_records()
    bare.freeze(controls=False)
    call = lambda slot: bare.token(  # noqa: E731
        slot, expansion="live", rewrite_candidate=config_hash, baseline_run="run-x", claim=False, declare=False)
    assert gate_of(lambda: call(1)) == "controls"  # the controls are not recorded yet
    bare.record_controls()
    assert gate_of(lambda: call(1)) == "batch"  # nothing declared
    bare.declare_batch(entries=[BATCH[0], candidate_entry(CANDIDATES[1], config_hash)])
    assert gate_of(lambda: call(3)) == "batch"  # beyond the two declared candidates
    assert gate_of(lambda: call(2)) == "baseline"  # the batch gate passed; the baseline gate is next
    assert gate_of(lambda: bare.token(2, expansion="live", rewrite_candidate=config_hash, claim=False, declare=False)) == "baseline"
    assert "batch" in sealed_run.GATES and "prereg_binding" in sealed_run.GATES


def test_the_token_for_a_confirmatory_slot_names_the_slots_candidate(world, tmp_path, monkeypatch):
    world.run(0)
    baseline = world.ledger().records("slot_stored")[0]["run_id"]
    art = world.run_dir(baseline) / "expansion_artifact.json"
    sha = hashlib.sha256(art.read_bytes()).hexdigest()
    assert world.ledger().canonical_artifact_sha256() == sha
    tok = world.token(2, expansion=str(art), baseline_run=baseline, claim=False)
    assert tok.candidate_config_hash == CANDIDATES[1] and tok.artifact_sha256 == sha and tok.candidate_expansion == "replay"
    tok.release()
    outcome = world.run(1, expansion=str(art), baseline_run=baseline)
    doc = json.loads((world.run_dir(outcome.run_id) / "results.json").read_text())
    assert doc["candidate_config_hash"] == CANDIDATES[0] and doc["candidate_expansion"] == "replay"


# --- 3b. the real bind_prereg against a temp repo with an origin ---------------------------------------------------
def test_bind_prereg_requires_a_committed_unmodified_pushed_prereg(repos):
    work = repos.work
    path = work / "eval" / "prereg.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"branch": "feature", "note": 1}), encoding="utf-8")

    def refused():
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.bind_prereg(path)
        assert info.value.gate == "prereg_binding"

    refused()  # untracked
    git(work, "add", "eval/prereg.json")
    git(work, "commit", "--quiet", "-m", "prereg")
    commit = git(work, "rev-parse", "HEAD")
    refused()  # committed, not pushed
    git(work, "push", "--quiet", "origin", "HEAD:refs/heads/feature")
    assert sealed_run.bind_prereg(path) == (commit, hashlib.sha256(path.read_bytes()).hexdigest())  # origin/<branch>
    path.write_text(json.dumps({"branch": "feature", "note": 2}), encoding="utf-8")
    refused()  # modified working file
    git(work, "checkout", "--quiet", "--", "eval/prereg.json")
    assert sealed_run.bind_prereg(path)[0] == commit


def test_bind_prereg_accepts_a_commit_on_main_when_the_branch_is_gone(repos):
    work = repos.work
    path = work / "prereg.json"
    path.write_text(json.dumps({"branch": "deleted-branch"}), encoding="utf-8")
    git(work, "add", "prereg.json")
    git(work, "commit", "--quiet", "-m", "prereg")
    commit = git(work, "rev-parse", "HEAD")
    with pytest.raises(sealed_run.GateRefused):
        sealed_run.bind_prereg(path)
    git(work, "push", "--quiet", "origin", "main")  # merged; origin has no ref for the branch
    assert sealed_run.bind_prereg(path)[0] == commit


def test_bind_prereg_refuses_a_file_outside_any_repo_or_unreadable(tmp_path):
    lone = tmp_path / "lone.json"
    lone.write_text(json.dumps({"branch": "main"}), encoding="utf-8")
    for p in (lone, tmp_path / "missing.json"):
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.bind_prereg(p)
        assert info.value.gate == "prereg_binding"


# --- 4. prereg validation -----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def good_prereg():
    quota = {"sealed": 12, "spare": 4, "floor_sealed": 8, "floor_answerable": 5, "realistic_min": 4, "tuning_min": 8}
    quota["examples"] = [
        {"pool_total": 24, "preassigned_realistic": 1, **st.compute_quotas(quota, 24, 1), "label": "ok"},
        {"pool_total": 18, "preassigned_realistic": 1, **st.compute_quotas(quota, 18, 1), "label": "quota_short"},
    ]
    power = {"alphas": [0.05, 0.0125], "discordance": [0.5], "planned_groups": [1] * 10 + [2] * 3, "draws": 40, "seed": 5}
    doc = {
        "version": 1, "branch": "main", "rounds": [1, 2], "seeds": {"split_r1": 1, "split_r2": 2, "repair": 3, "calibration": 4},
        "quota": quota, "band": {"min_pct": 25, "max_pct": 30},
        "min_share": {"realistic": {"tutorial_or_colleague": 0.25}}, "power": power, **b1a_blocks(),
    }
    power["table"] = st.power_cells(power, 0.1)
    power["family_table"] = st.family_cells(power, 0.1)
    assert st.validate_prereg(doc) == []
    return doc


def mutate(doc, path, value):
    out = copy.deepcopy(doc)
    node = out
    *parents, last = path.split(".")
    for part in parents:
        node = node[int(part) if isinstance(node, list) else part]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return out


MISMATCHES = [
    ("primary_metric.cutoff", 3), ("primary_metric.mode", "hybrid"), ("primary_metric.metric", "related_hit"),
    ("collapse", {"rule": "all", "tie": "miss"}), ("collapse", {"rule": "any"}), ("k_max", 5),
    ("adoption.rule", "b"), ("adoption.confirmatory_batch.k_max", 3), ("adoption.confirmatory_batch.holm_k", 3),
    ("adoption.confirmatory_batch.slots", ["1", "2"]), ("adoption.confirmatory_batch.replays", "canonical_baseline_expansion_artifact"),
    ("adoption.development_evidence.cutoff", 3), ("adoption.development_evidence.draws", 4),
    ("adoption.development_evidence.extra", 1), ("adoption.development_evidence.sets", ["tuning"]),
    ("adoption.unsupported_claims.max_excess_abs", 0.02), ("adoption.control_flip.draws", "every"),
    ("adoption.development_family_effect_min", 0.2),
    ("controls.rule", "c2"), ("controls.rank_ceiling", 6), ("controls.modes", ["hybrid"]), ("controls.draws_r", 4),
    ("controls.collapse", "any"), ("controls.binds_to", KeyError),
    ("slots.0.expansion", "replay"), ("slots.1.artifact", "x"), ("slots.1.requires", KeyError), ("slots.3", {"expansion": "live"}),
    ("slots.5", {"expansion": "live"}),
    ("drafting_rule.attempt_1.refusal_share_target", 0.3), ("drafting_rule.attempt_1.refusal_share_counts", "new_drafts"),
    ("drafting_rule.attempt_1.realistic_lay_min_share", 0.4), ("drafting_rule.next_attempt.max_round_1_attempts", 4),
    ("drafting_rule.next_attempt.after_cap", "go_on"), ("drafting_rule.paraphrase.t1_t7_cap", 4),
    ("power.alphas", [0.05]), ("power.alphas", [0.05, 0.02]),
]

#: r2 (R5): every other registered value b1a now pins, changed and (below) removed
NEW_REGISTERED = [
    ("mwe", 0.2), ("alpha", 0.1), ("primary_metric.test", "permutation"), ("primary_metric.sided", "one"),
    ("primary_metric.multiplicity", "none"), ("adoption.sealed_point_estimate_min", 0.01),
    ("adoption.sealed_holm_significant_loss", "allowed"), ("adoption.critical_control_flips_max", 1),
    ("adoption.confirmatory_batch.spent_slot_counts", False),
    ("adoption.confirmatory_batch.state", "open"), ("adoption.confirmatory_batch.modes", ["replay"]),
    ("adoption.confirmatory_batch.mode_declared_by", "prereg"), ("adoption.confirmatory_batch.candidate_identity", "none"),
    ("adoption.confirmatory_batch.embargo_lifted_by", "nobody"), ("adoption.confirmatory_batch.requires", ["judge_on"]),
    ("adoption.development_evidence.versions", "frozen_at_slot_0"), ("adoption.development_evidence.pairing", "pooled"),
    ("adoption.development_evidence.incumbent_draws", "every_draw"), ("adoption.development_evidence.candidate_draws", "any"),
    ("adoption.unsupported_claims.reference", "incumbent_dev"), ("adoption.control_flip.versions", "frozen_at_slot_0"),
    ("adoption.control_flip.draws", "any_of_first_3_recorded"),
    ("controls.draws", "every"), ("controls.committed", "before_slot_1"),
    ("slots.1.expansion", "replay"), ("slots.4.expansion", "candidate"),
    ("drafting_rule.attempt_1.refusal_drafts", "round(target_x_pool_total)_minus_preassigned_refusals"),
    ("drafting_rule.attempt_1.realistic_lay_share_of", "all_realistic"), ("drafting_rule.attempt_1.band_unreachable", "continue"),
    ("drafting_rule.round_2.max_attempts", 4), ("drafting_rule.round_2.after_cap", "go_on"),
    ("drafting_rule.round_2.refusal_drafts", "none"),
]
NEW_REGISTERED_KEYS = [path for path, _ in NEW_REGISTERED]


@pytest.mark.parametrize("path,value", MISMATCHES + NEW_REGISTERED)
def test_validate_prereg_registered_values(good_prereg, path, value):
    errors = st.validate_prereg(mutate(good_prereg, path, value))
    assert errors and all(why in st.PREREG_ERRORS for _, why in errors)
    assert any(why in ("mismatch", "out_of_range") for _, why in errors), errors


@pytest.mark.parametrize("path", NEW_REGISTERED_KEYS)
def test_validate_prereg_requires_every_registered_value_to_be_present(good_prereg, path):
    errors = st.validate_prereg(mutate(good_prereg, path, KeyError))
    assert errors and any(why in ("mismatch", "missing") for _, why in errors), errors


def test_validate_prereg_slots_1_to_4_are_batch_declarations(good_prereg):
    for sid in ("1", "2", "3", "4"):
        assert good_prereg["slots"][sid]["expansion"] == "batch"
    old = copy.deepcopy(good_prereg)
    old["slots"]["2"] = {"expansion": "replay", "artifact": "slot0_canonical", "role": "confirmatory", "requires": "batch_declaration"}
    assert any(f.startswith("slots.2") for f, _ in st.validate_prereg(old))


def test_validate_prereg_required_keys_and_dropped_keys(good_prereg):
    for key in ("primary_metric", "collapse", "adoption", "controls", "slots", "drafting_rule"):
        assert (key, "missing") in st.validate_prereg(mutate(good_prereg, key, KeyError))
    assert ("adoption.sealed_guard", "unknown_key") in st.validate_prereg(
        mutate(good_prereg, "adoption.sealed_guard", "non_inferiority"))
    for old in ("refusal_pct_approx", "plus"):
        assert (f"drafting_rule.attempt_1.{old}", "unknown_key") in st.validate_prereg(
            mutate(good_prereg, f"drafting_rule.attempt_1.{old}", 30))
    # the band for the refusal share target: inside 0.25-0.30 is still not the registered 0.275
    assert any(f == "drafting_rule.attempt_1.refusal_share_target" for f, _ in st.validate_prereg(
        mutate(good_prereg, "drafting_rule.attempt_1.refusal_share_target", 0.26)))
    assert ("drafting_rule.attempt_1.refusal_share_target", "out_of_range") in st.validate_prereg(
        mutate(good_prereg, "drafting_rule.attempt_1.refusal_share_target", 0.4))


def test_validate_prereg_quota_example_labels(good_prereg):
    ex = good_prereg["quota"]["examples"]
    assert ex[1]["tuning"] < good_prereg["quota"]["tuning_min"] <= ex[0]["tuning"]
    assert ("quota.examples[1]", "mismatch") in st.validate_prereg(mutate(good_prereg, "quota.examples.1.label", "ok"))
    assert ("quota.examples[0]", "mismatch") in st.validate_prereg(mutate(good_prereg, "quota.examples.0.label", "quota_short"))
    assert ("quota.examples[1]", "mismatch") in st.validate_prereg(mutate(good_prereg, "quota.examples.1.label", KeyError))
    assert st.validate_prereg(mutate(good_prereg, "quota.examples.0.label", KeyError)) == []  # an ok example may omit it


def test_validate_prereg_power_tables_are_recomputed(good_prereg):
    cells = st.power_cells(good_prereg["power"], 0.1)
    assert [(c["model"], c["alpha"], c["discordance"]) for c in cells] == [
        (m, a, 0.5) for m in st.MODELS for a in (0.05, 0.0125)]
    assert set(cells[0]) == {"model", "alpha", "discordance", "power_pm", "mde_hundredths"}
    fam = st.family_cells(good_prereg["power"], 0.1)
    assert [(c["alpha"], c["discordance"], c["families"]) for c in fam] == [(0.05, 0.5, 16), (0.0125, 0.5, 16)]
    assert set(fam[0]) == {"alpha", "discordance", "families", "power_pm", "mde_hundredths"}
    wrong = copy.deepcopy(good_prereg)
    wrong["power"]["family_table"][0]["power_pm"] += 1
    assert ("power.family_table", "mismatch") in st.validate_prereg(wrong)
    wrong = copy.deepcopy(good_prereg)
    wrong["power"]["table"][1]["power_pm"] += 1
    assert ("power.table", "mismatch") in st.validate_prereg(wrong)
    old = copy.deepcopy(good_prereg)
    old["power"]["alpha"] = old["power"].pop("alphas")[0]  # the old single-alpha shape is not valid
    assert st.validate_prereg(old)


def test_the_committed_prereg_validates_with_its_seeds_unchanged():
    path = Path(__file__).resolve().parent.parent / "eval" / "prereg_16a2.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert st.validate_prereg(doc) == []
    assert doc["seeds"] == {
        "split_r1": 4721112936955565465, "split_r2": 2060714279786243518,
        "repair": 4132900947249597239, "calibration": 1958881144304175148,
    }
    assert doc["power"]["seed"] == 5722486815998357046
    assert doc["power"]["alphas"] == [0.05, 0.0125] and len(doc["power"]["table"]) == 12 and len(doc["power"]["family_table"]) == 6
    assert [e["label"] for e in doc["quota"]["examples"]] == ["ok", "ok", "quota_short"]
