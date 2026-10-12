"""Phase 16A-2a phase-gate fixes, Area B (B1-B13): ``scripts/sealed_tool.py``.

Each test names the gate finding it closes and was written to FAIL on the code before the fix. Synthetic,
schema-valid fixtures only (``tests/p16w_sealed_world.py``); nothing here reads a real eval set, the corpus,
``~/.claude`` or the owner's staging area (the projects root, staging root and repository are all injected).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src.sealed_ledger import SealedLedger, group_ids
from tests.p16w_sealed_world import fid_of, read_rows, repos, standard_released, world, write_rows  # noqa: F401  (fixtures)
from tests.test_p16w_sealed_tool_audit import audit_world, lines, plant, run_audit  # noqa: F401
from tests.test_p16w_sealed_tool_freeze import _reject, frozen_ids, sealed_ready
from tests.test_p16w_sealed_tool_split import _round2_world, assign_payload
from tests.test_p16w_sp_launch import git


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- B1 (Cx1): a name is a basename, a destination cannot leave its directory --------------------------------------------
@pytest.mark.parametrize("bad", ["ABSOLUTE", "../../../../review", "a/b", "..", ".", "", "x" * 65, "has space", "dot.name", "tab\tname"])
def test_a_sheet_or_form_name_that_is_not_a_plain_basename_is_refused_and_writes_nothing(world, tmp_path, bad):
    std = standard_released(world)
    outside = tmp_path / "outside"
    name = str(outside / "stolen") if bad == "ABSOLUTE" else bad
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", name)
    assert code == st.EXIT_USAGE and out == "" and "reason=usage" in err
    assert not outside.exists() and not (world.root.parent / "review.csv").exists()
    # the same name on an escalation form
    world.run("sheet", "--kind", "sealed", "--name", "e1")
    sheet = world.mark_sheet(world.sheet_path("sealed", "e1"), {std.sealed_singletons[0]: "E"}, default="A")
    code, out, err = world.run("escalate", "--kind", "sealed", "--sheet", sheet, "--name", name)
    assert code == st.EXIT_USAGE and out == "" and "reason=usage" in err
    assert not outside.exists() and not (world.root.parent / "review.txt").exists() and not (world.root.parent / "form_review.txt").exists()
    ok = world.run("escalate", "--kind", "sealed", "--sheet", sheet, "--name", "good-name_1")
    assert ok[0] == 0, ok[2]  # a plain basename still works
    assert (world.sealed / "work" / "escalations" / "form_good-name_1.txt").is_file()


def test_a_symlinked_sheets_directory_or_destination_is_refused_and_nothing_is_written_through_it(world, tmp_path):
    standard_released(world)
    outside = tmp_path / "outside"
    outside.mkdir()
    sheets = world.sealed / "work" / "sheets"
    sheets.symlink_to(outside, target_is_directory=True)  # the whole directory leads elsewhere
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", "ok")
    assert code == st.EXIT_USAGE and "reason=path_outside" in err and out == ""
    assert list(outside.iterdir()) == []
    sheets.unlink()
    sheets.mkdir()
    victim = outside / "victim.csv"
    victim.write_text("keep me", encoding="utf-8")
    (sheets / "ok.csv").symlink_to(victim)  # only the destination is a link
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", "ok")
    assert code == st.EXIT_USAGE and "reason=path_outside" in err
    assert victim.read_text(encoding="utf-8") == "keep me"
    (sheets / "ok.csv").unlink()
    escalations = world.sealed / "work" / "escalations"
    escalations.symlink_to(outside, target_is_directory=True)
    world.run("sheet", "--kind", "sealed", "--name", "e1")
    sheet = world.mark_sheet(world.sheet_path("sealed", "e1"), {}, default="E")
    code, out, err = world.run("escalate", "--kind", "sealed", "--sheet", sheet)
    assert code == st.EXIT_USAGE and "reason=path_outside" in err and list(outside.iterdir()) == [victim]


# --- B2 (Cx8, Rev5): release recovery expects only the files a release writes ---------------------------------------------
@pytest.mark.parametrize("point", ["after_begin", "after_copy", "after_rename", "after_delete"])
def test_a_round_two_release_crash_is_recovered_when_a_pinned_only_split_gets_no_new_file(world, monkeypatch, point):
    std, rejected, replacement, *_ = _round2_world(world)  # realistic: pinned families only, so no realistic_r2.jsonl

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_FAIL and "reason=internal" in err
    assert len(world.ledger().records("release")) == 2 and not (world.sealed / "work" / "release_r2.done").exists()
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    code, out, err = world.run("split", "--round", "2")  # recovery first; it must not stop with pool_changed
    assert code == st.EXIT_REFUSED and "reason=already_released" in err, err
    assert (world.sealed / "work" / "release_r2.done").is_file()
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", "after")  # and the next command is not wedged either
    assert code == 0, err
    assert not (world.dev / "realistic_r2.jsonl").exists()
    assert {r["family_id"] for r in read_rows(world.sealed / "sealed_r2.jsonl")} == {replacement}
    assert not list((world.sealed / "pool").rglob("*.jsonl")) and sp.pending_moves(world.sealed / "work" / "release.journal") == []


# --- B4 (PT e, defect 3): round-2 quotas are pinned + replacements -----------------------------------------------------------
def test_round_two_fills_a_development_shortfall_and_shelves_the_surplus_instead_of_forcing_it_into_tuning(world):
    std = standard_released(world)
    realistic = read_rows(world.dev / "realistic_r1.jsonl")
    refused = next(r["family_id"] for r in realistic if r["scope"] == "refuse")  # the one refusal family realistic's band needs
    assert world.run("sheet", "--kind", "development", "--name", "d1")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("development", "d1"), {refused: "R"}, default="A")
    assert world.run("apply", "--kind", "development", "--sheet", sheet)[0] == 0
    assert refused in {r["family_id"] for r in read_rows(world.dev / "quarantine.jsonl")}  # the owner rejected it
    replacement = world.draft("refuse", register="lay")  # a lay draft replaces it
    surplus = world.draft("answer", register="lay")  # and one more that no split has room for
    world.flush_pool("round2.jsonl")
    assert world.run("twins", "--round", "2", "--verdict", world.verdict())[0] == 0
    code, out, err = world.run("split", "--round", "2")
    assert code == 0, err
    line = world.last(out, "split")
    assert line["outcome"] == "released" and line["surplus"] == "1" and line["dropped"] == "0"
    assert {r["family_id"] for r in read_rows(world.dev / "realistic_r2.jsonl")} == {replacement}  # the shortfall is filled
    assert not (world.dev / "tuning_r2.jsonl").exists() and not (world.sealed / "sealed_r2.jsonl").exists()
    assert {r["family_id"] for r in read_rows(world.sealed / "quarantine_r2.jsonl")} == {surplus}  # shelved, not forced into tuning
    payload = assign_payload(world, 2)
    assert payload["quotas"] == {"sealed": 12, "realistic": 4, "tuning": 8} and payload["surplus"] == [surplus]
    assert surplus not in payload["assignment"] and payload["assignment"][replacement] == "realistic"
    totals = [r for r in world.ledger().records("release") if r["round"] == 2][0]["totals"]
    assert totals["surplus"] == 1 and totals["realistic"] == 1
    # the surplus draft is accounted for: a freeze (which replays the release and checks the files) agrees
    world.approve_all("sealed")
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err  # it got past the replay and the files check


def test_round_two_with_fewer_drafts_than_replacement_slots_is_an_infeasible_attempt(world):
    std = standard_released(world)
    one, two = std.sealed_singletons[:2]
    assert world.run("sheet", "--kind", "sealed", "--name", "r1")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("sealed", "r1"), {one: "R", two: "R"}, default="A")
    assert world.run("apply", "--kind", "sealed", "--sheet", sheet)[0] == 0
    world.draft("answer", register="modelled")  # two slots, one draft
    world.flush_pool("round2.jsonl")
    assert world.run("twins", "--round", "2", "--verdict", world.verdict())[0] == 0
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_SPLIT_INFEASIBLE and "outcome=infeasible" in out and "split_constraint constraint=quota_total" in out
    assert [r["outcome"] for r in world.ledger().records("split_attempt")][-1] == "infeasible"
    assert [r["round"] for r in world.ledger().records("release")] == [1]


def test_surplus_choices_shelve_exactly_the_surplus_in_whole_twin_units_and_are_seeded():
    drafts = [f"d{i}" for i in range(6)]
    edges = [["d0", "d1"], ["d2", "d3"]]
    choices = list(st.surplus_choices(drafts, edges, 3, seed=5))
    assert choices and all(len(c) == 3 for c in choices)
    for c in choices:
        assert ("d0" in c) == ("d1" in c) and ("d2" in c) == ("d3" in c)  # a twin unit is shelved whole or not at all
    assert len({tuple(c) for c in choices}) == len(choices)  # each distinct set once
    assert list(st.surplus_choices(drafts, edges, 3, seed=5)) == choices  # reproducible
    assert list(st.surplus_choices(["a", "b"], [["a", "b"]], 1, seed=1)) == []  # no whole-unit set of exactly one


# --- B5 (Rev7): after the sealed freeze a sealed-sealed twin is an exclusion, never a move --------------------------------------
def test_after_the_sealed_freeze_a_sealed_sealed_twin_found_by_a_development_recheck_is_an_exclusion_not_a_move(world):
    std = sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    frozen = world.sealed / "heldout-v2.jsonl"
    before = (sha(frozen), sha(world.sealed / "sealed_r1.jsonl"))
    world.approve_all("development")
    a, b = sorted(std.sealed_singletons[:2])
    code, out, err = world.recheck("realistic-v2", [(a, b)])
    assert code == 0, err
    line = world.last(out, "freeze")
    assert (line["demoted_groups"], line["excluded_groups"]) == ("0", "1")
    assert (sha(frozen), sha(world.sealed / "sealed_r1.jsonl")) == before  # neither the frozen file nor a component moved
    assert not (world.sealed / "quarantine.jsonl").exists() or b not in {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")}
    assert b in frozen_ids(world) and world.ledger().exclusions() == frozenset({b})  # the later group, excluded
    # a later development twin of the same family is not silently ignored: it is excluded (here: already), and a new one is added
    tuning_fam = read_rows(world.dev / "tuning_r1.jsonl")[0]["family_id"]
    c = std.sealed_singletons[2]
    code, out, err = world.recheck("tuning", [(tuning_fam, b), (tuning_fam, c)])
    assert code == 0, err
    assert world.ledger().exclusions() == frozenset({b, c})
    # the excluded groups are names the slot gate knows: group ids over the frozen families and the bound edges
    record = world.ledger().freeze_record("sealed")
    payload = world.ledger().load_payload(st.twins_payload_name(record["twins_sha256"]), record["twins_sha256"])
    known = set(group_ids(frozen_ids(world), [tuple(e) for e in payload["edges"]]).values())
    assert world.ledger().exclusions() <= known


def test_a_development_twin_of_a_frozen_group_is_excluded_under_the_name_the_gate_uses(world):
    std = sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    world.approve_all("development")
    realistic_fam = read_rows(world.dev / "realistic_r1.jsonl")[0]["family_id"]
    group = sorted(std.sealed_groups[2])
    code, out, err = world.recheck("realistic-v2", [(realistic_fam, group[1])])
    assert code == 0, err
    record = world.ledger().freeze_record("sealed")
    payload = world.ledger().load_payload(st.twins_payload_name(record["twins_sha256"]), record["twins_sha256"])
    known = set(group_ids(frozen_ids(world), [tuple(e) for e in payload["edges"]]).values())
    assert world.ledger().exclusions() == frozenset({group[0]}) and group[0] in known


# --- B6 (Rev8): apply --kind development refuses only rows of a frozen split ----------------------------------------------------
def test_apply_development_refuses_only_rows_whose_own_split_is_frozen(world):
    std = standard_released(world)
    realistic_ids = {r["family_id"] for r in read_rows(world.dev / "realistic_r1.jsonl")} | {r["family_id"] for r in read_rows(world.dev / "realistic_pinned.jsonl")}
    assert world.run("sheet", "--kind", "development", "--name", "d1")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("development", "d1"), {f: "A" for f in realistic_ids})
    assert world.run("apply", "--kind", "development", "--sheet", sheet)[0] == 0
    assert world.recheck("realistic-v2")[0] == 0  # realistic is frozen
    assert world.run("sheet", "--kind", "development", "--name", "d2")[0] == 0  # what is left to review is tuning
    sheet = world.mark_sheet(world.sheet_path("development", "d2"), {}, default="A")
    code, out, err = world.run("apply", "--kind", "development", "--sheet", sheet)
    assert code == 0, err  # tuning rows are not blocked by realistic's freeze
    assert world.last(out, "apply")["approved"] != "0"
    # a sheet that names a row of the frozen split is still refused
    assert world.run("sheet", "--kind", "development", "--name", "d3", "--all")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("development", "d3"), {}, default="A")
    code, out, err = world.run("apply", "--kind", "development", "--sheet", sheet)
    assert code == st.EXIT_REFUSED and "reason=frozen" in err


# --- B10 (Rev18): tutorial tags are for the pool and sealed-pinned families only -----------------------------------------------
def test_a_migrated_tutorial_family_in_development_needs_no_tag_but_a_drafted_one_still_does(world):
    from tests.p16w_sealed_world import build_standard

    std = build_standard(world)
    migrated = std.pinned_realistic[0]  # source tutorial, pinned in development: the SP never sees it, so it can never be tagged
    world.tags.pop(migrated)
    world.write_tags()
    world.write_prereg()
    world.commit_prereg()
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict(std.verdict_pairs))
    assert code == 0, err
    # a drafted tutorial family without a tag is still refused
    world.draft(source="tutorial", tag="t9")
    world.tags.pop(next(iter(k for k, v in world.tags.items() if v == "t9")))
    world.write_tags()
    world.flush_pool("more.jsonl")
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict(std.verdict_pairs))
    assert code == st.EXIT_USAGE and "reason=tutorial_tag_missing families=1" in err


# --- B11 (Rev19): the group invariant is checked over every released family ------------------------------------------------------
def test_the_group_invariant_sees_a_group_that_reaches_a_development_family(world, monkeypatch):
    std = sealed_ready(world)
    dev_member = read_rows(world.dev / "tuning_r1.jsonl")[0]["family_id"]
    sealed_member = std.sealed_singletons[0]
    real = st.release_edges
    monkeypatch.setattr(st, "release_edges", lambda payloads: [*real(payloads), [sealed_member, dev_member]])
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "freeze_constraint constraint=group_invariant" in out
    assert world.ledger().freeze_record("sealed") is None and not (world.sealed / "heldout-v2.jsonl").exists()


# --- B12 (Rev20): band repair honours floor_answerable and the current exclusions -------------------------------------------------
def test_repair_band_never_picks_a_set_that_ends_below_the_answerable_floor():
    # r1, r2: refusal families; x: an excluded 2-group (counts for no answerable); y: a 3-group; z, w, v: singletons
    units = [("r1", 1, 1, 0), ("r2", 1, 1, 0), ("x", 2, 0, 0), ("y", 3, 0, 3), ("z", 1, 0, 1), ("w", 1, 0, 1), ("v", 1, 0, 1)]
    answerable = {u[0]: u[3] for u in units}
    unconstrained = {tuple(st.repair_band(units, 2, seed=s) or ()) for s in range(25)}
    assert ("y",) in unconstrained  # without the answerable floor the minimum can be the 3-group: 3 answerable left, under 4
    for seed in range(25):
        got = st.repair_band(units, 2, seed=seed, floor_answerable=4)
        assert got == ["x"], (seed, got)  # the only single group that leaves 4 answerable (6 - 0)
        assert sum(answerable.values()) - sum(answerable[g] for g in got) >= 4
    assert st.repair_band(units, 2, seed=1, floor_answerable=7) is None  # unreachable: an honest "no repair", not a wrong one


@pytest.mark.parametrize("seed", range(8))
def test_band_repair_with_an_excluded_group_never_ends_in_a_false_floor_breach(world, seed):
    world.prereg_extra = {"seeds": {"split_r1": 11, "split_r2": 12, "repair": seed, "calibration": 14}}
    std = standard_released(world)
    _reject(world, [std.sealed_refuse[0]])  # 11 families, 2 refuse: the band needs 3 families to leave
    excluded = std.sealed_groups[0]
    assert world.run("exclude", "--family", excluded[0], "--reason", "exposure")[0] == 0
    code, out, err = world.recheck("sealed")
    # 7 non-excluded answerable families before repair; floor 5 => the demoted 2-group must be the excluded one
    assert code == 0, (seed, out, err)
    assert world.last(out, "freeze")["repaired_groups"] == "2"
    assert set(excluded).isdisjoint(frozen_ids(world))


# --- B3 (Cx9, Rev16): the freeze is journalled and resumed, never recomputed --------------------------------------------------------
POINTS = ["after_journal", "after_frozen", "after_payload", "after_record", "after_exclude", "after_quarantine"]


def _sealed_scenario(world, scenario):
    if scenario == "demotion":
        std = sealed_ready(world)
        a, b = std.sealed_singletons[0], std.sealed_singletons[1]
        return std, [(a, b)], {max(a, b)}, 1
    std = standard_released(world)
    _reject(world, [std.sealed_refuse[0]])
    return std, [], None, 2


@pytest.mark.parametrize("scenario", ["demotion", "band_repair"])
@pytest.mark.parametrize("point", POINTS)
def test_a_freeze_interrupted_at_any_step_is_finished_from_its_journal(world, monkeypatch, scenario, point):
    std, pairs, gone, n_groups = _sealed_scenario(world, scenario)

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(st, "_freeze_crash", boom)
    code, out, err = world.recheck("sealed", pairs)
    assert code == st.EXIT_FAIL and "reason=internal" in err
    assert (world.sealed / "work" / "freeze_sealed.journal").is_file()  # the transaction was decided and recorded first
    monkeypatch.setattr(st, "_freeze_crash", lambda name: None)
    code, out, err = world.run("freeze", "--split", "sealed")  # no recheck files needed: it resumes
    assert code == 0, (point, err)
    line = world.last(out, "freeze")
    assert line["status"] == "frozen"
    assert (line["demoted_groups"], line["repaired_groups"]) == (("1", "0") if scenario == "demotion" else ("0", "2"))
    frozen = world.sealed / "heldout-v2.jsonl"
    record = world.ledger().freeze_record("sealed")
    assert record["set_sha256"] == sha(frozen) == line["sha256"] and len(world.ledger().records("freeze")) == 1
    survivors = frozen_ids(world)
    components = {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}
    quarantined = {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")}
    assert components == survivors and survivors.isdisjoint(quarantined)  # every moved family left the components, none was lost
    assert survivors | quarantined == set(std.sealed)
    if gone:
        assert survivors == set(std.sealed) - gone
    else:
        assert line["families"] == "8"
    payload = world.ledger().load_payload(st.twins_payload_name(record["twins_sha256"]), record["twins_sha256"])
    assert payload["families_sha256"] == sha(frozen) and len(payload["demoted_groups"]) + len(payload["repaired_groups"]) == n_groups
    assert world.ledger().load_payload("twins_sealed", record["twins_sha256"]) == payload  # the alias older readers use
    work = world.sealed / "work"
    assert not (work / "freeze_sealed.journal").exists() and not (work / "freeze_sealed.data").exists()
    assert world.run("freeze", "--split", "sealed")[0] == st.EXIT_REFUSED  # and it is frozen exactly once


def test_a_crash_between_the_payload_and_the_ledger_record_is_resumed_not_recomputed(world, monkeypatch):
    """The reviewer's repro: record_freeze fails once; before the fix the retry saw a demoted world and could not finish."""
    std, pairs, gone, _ = _sealed_scenario(world, "demotion")
    real = SealedLedger.record_freeze
    state = {"armed": True}

    def crash(self, **kw):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("crash")
        return real(self, **kw)

    monkeypatch.setattr(SealedLedger, "record_freeze", crash)
    code, out, err = world.recheck("sealed", pairs)
    assert code == st.EXIT_FAIL
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == 0, err
    assert frozen_ids(world) == set(std.sealed) - gone and world.ledger().freeze_record("sealed") is not None


def test_any_state_changing_command_finishes_a_journalled_freeze_first(world, monkeypatch):
    std, pairs, gone, _ = _sealed_scenario(world, "demotion")

    def boom(name: str) -> None:
        if name == "after_record":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(st, "_freeze_crash", boom)
    assert world.recheck("sealed", pairs)[0] == st.EXIT_FAIL
    victim = max(gone)
    assert victim in {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}  # the moves come last: not yet made
    monkeypatch.setattr(st, "_freeze_crash", lambda name: None)
    assert world.run("sheet", "--kind", "sealed", "--name", "after")[0] == 0
    assert victim not in {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}
    assert victim in {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")}
    assert not (world.sealed / "work" / "freeze_sealed.journal").exists()


def test_a_tampered_journalled_freeze_is_a_ledger_stop_and_records_nothing(world, monkeypatch):
    std, pairs, gone, _ = _sealed_scenario(world, "demotion")

    def boom(name: str) -> None:
        if name == "after_journal":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(st, "_freeze_crash", boom)
    assert world.recheck("sealed", pairs)[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "_freeze_crash", lambda name: None)
    data = world.sealed / "work" / "freeze_sealed.data"
    data.write_bytes(data.read_bytes() + b"\n")  # the staged frozen file no longer matches the journalled sha256
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_LEDGER_STOP and "reason=freeze_recovery_failed" in err and out == ""
    assert world.ledger().freeze_record("sealed") is None and not (world.sealed / "heldout-v2.jsonl").exists()


@pytest.mark.parametrize("point", ["after_journal", "after_record", "after_quarantine"])
def test_a_development_freeze_that_demotes_a_sealed_group_before_the_sealed_freeze_is_resumed_too(world, monkeypatch, point):
    std = sealed_ready(world)
    world.approve_all("development")
    group = sorted(std.sealed_groups[0])
    realistic_fam = read_rows(world.dev / "realistic_r1.jsonl")[0]["family_id"]

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(st, "_freeze_crash", boom)
    assert world.recheck("realistic-v2", [(realistic_fam, group[0])])[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "_freeze_crash", lambda name: None)
    code, out, err = world.run("freeze", "--split", "realistic-v2")
    assert code == 0, err
    assert world.last(out, "freeze")["demoted_groups"] == "1"
    assert set(group).isdisjoint({r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")})  # the whole group left sealed
    assert {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")} == set(group)
    assert world.ledger().freeze_record("realistic")["set_sha256"] == sha(world.dev / "realistic-v2.jsonl")


# --- B7 (PT defect 2, Rev11): which transcript directories, and the staging they may sit in ----------------------------------------
def test_audit_scans_scratchpad_staging_derived_and_worktree_project_directories(world, audit_world, tmp_path):
    aw = audit_world
    fam, q = aw.fam, aw.q

    def put(directory: str, text: str) -> None:
        path = aw.projects / directory / "session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"content": text}) + "\n", encoding="utf-8")

    put(f"-private-tmp-claude-501{aw.mangled}-1234-scratchpad", q["transcript"])  # CONTAINS the repo's mangled path
    aw.staging.mkdir()
    put(st.mangle_path(aw.staging / "abcd1234"), q["tree"])  # STARTS with the SP staging root's mangled path
    wt = tmp_path / "elsewhere_wt"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(wt))
    assert st.mangle_path(world.repo) not in st.mangle_path(wt)  # only `git worktree list` can connect it to this repo
    put(st.mangle_path(wt), q["history"])
    put("-Users-someone-else-project", q["elsewhere"])
    code, out, err = run_audit(world, aw)
    assert code == st.EXIT_HIT, err
    hits = {(ln["target"], ln["family"]) for ln in lines(out, "audit_hit")}
    assert hits == {("transcript", fam["transcript"]), ("transcript", fam["tree"]), ("transcript", fam["history"])}


def test_audit_refuses_while_sp_staging_is_live_and_replays_a_half_done_move_before_scanning(world, audit_world, tmp_path, monkeypatch):
    aw = audit_world
    aw.staging.mkdir()
    with sp._exclusive_lock(aw.staging):  # a dispatch or sweep holds the launcher's lock
        code, out, err = run_audit(world, aw)
    assert code == st.EXIT_AUDIT_REFUSED == 7 and "reason=staging_live" in err and out == ""
    # a move the launcher left half done: copied, not yet deleted from staging
    src = aw.staging / "abcd1234" / "out" / "pool.jsonl"
    src.parent.mkdir(parents=True)
    src.write_text(aw.q["staging"] + "\n", encoding="utf-8")
    dst = tmp_path / "released" / "pool.jsonl"

    def boom(name: str) -> None:
        if name == "after_copy":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    with pytest.raises(RuntimeError):
        sp.journaled_move([(src, dst)], aw.staging / sp.JOURNAL_NAME)
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    assert sp.pending_moves(aw.staging / sp.JOURNAL_NAME)
    code, out, err = run_audit(world, aw)
    assert not sp.pending_moves(aw.staging / sp.JOURNAL_NAME) and dst.is_file() and not src.exists()  # replayed first
    assert code == st.EXIT_OK  # the released copy is under sealed/work territory the audit does not list; the stale source is gone


# --- B8 (Rev13): an absent handbook dump is a refusal, not a quietly smaller exemption -------------------------------------------------
def test_audit_refuses_with_its_own_exit_when_the_handbook_dump_is_absent(world, audit_world, tmp_path):
    aw = audit_world
    plant(aw, world)
    missing = tmp_path / "no_such_dump.jsonl"
    code, out, err = world.run("audit", **aw.opts, handbook_dump=missing)
    assert code == st.EXIT_AUDIT_REFUSED == 7 and err == "error reason=dump_missing\n" and out == ""
    code, out, err = run_audit(world, aw)  # with the dump it runs and finds the plants
    assert code == st.EXIT_HIT
    (world.sealed / "sealed_r1.jsonl").unlink()  # no sealed source, so no exemption to lose: not a refusal
    code, out, err = world.run("audit", **aw.opts, handbook_dump=missing)
    assert code == 0 and lines(out, "audit")[0]["s_needles"] == "0"


# --- B9 (Rev15): the history scan walks commit AND tag objects ----------------------------------------------------------------------
def test_audit_finds_a_needle_in_an_annotated_tag_message(world, audit_world):
    aw = audit_world
    git(world.repo, "tag", "-a", "v9", "-m", f"release notes {aw.q['dispatch']}")  # a tag message is in no commit message
    code, out, err = run_audit(world, aw)
    assert code == st.EXIT_HIT, err
    assert {(ln["target"], ln["family"]) for ln in lines(out, "audit_hit")} == {("history", aw.fam["dispatch"])}
    assert [ln for ln in lines(out, "audit_target") if ln["target"] == "history"][0]["files"] != "0"


# --- B13 (Rev21): one lock for every state-changing command -----------------------------------------------------------------------------
HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
)


def test_a_state_changing_command_is_refused_while_another_process_holds_the_lock(world):
    std = standard_released(world)
    lock = world.sealed / "work" / ".lock"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        before = world.ledger().records()
        for argv in (["exclude", "--family", std.sealed_singletons[0], "--reason", "exposure"], ["sheet", "--kind", "sealed", "--name", "x"],
                     ["freeze", "--split", "sealed"], ["split", "--round", "2"], ["ids", "--count", "1"],
                     ["apply", "--kind", "sealed", "--sheet", "nope.csv"], ["twins", "--round", "1", "--verdict", "nope.jsonl"]):
            code, out, err = world.run(*argv)
            assert code == st.EXIT_REFUSED and err == "error reason=locked\n" and out == "", argv
        assert world.ledger().records() == before and not (world.sealed / "work" / "sheets" / "x.csv").exists()
        # read-only commands take no lock
        assert world.run("digest", world.sealed / "sealed_r1.jsonl")[0] == 0
        assert world.run("prereg-check")[0] == 0
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    code, out, err = world.run("exclude", "--family", std.sealed_singletons[0], "--reason", "exposure")  # released: it runs
    assert code == 0, err
    assert world.ledger().exclusions() == frozenset({std.sealed_singletons[0]})


def test_the_lock_is_released_when_a_command_ends_by_refusal_or_crash(world, monkeypatch):
    standard_released(world)
    assert world.run("split", "--round", "1")[0] == st.EXIT_REFUSED  # already released: a refusal inside the lock
    real = st.cmd_sheet

    def crash(*args, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(st, "cmd_sheet", crash)
    assert world.run("sheet", "--kind", "sealed", "--name", "x")[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "cmd_sheet", real)
    assert world.run("sheet", "--kind", "sealed", "--name", "x")[0] == 0  # the lock died with the crashed command


# --- C3 follow-through: a hit can name several sealed families; audit lists each and exclude takes them all ---------------------------
def test_audit_lists_every_family_a_hit_names_and_exclude_excludes_each_ones_group(world, tmp_path):
    std = standard_released(world)
    a, b = std.sealed_singletons[0], std.sealed_groups[0][0]  # two families in two different groups hold the same question
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    full = next(r["question"] for r in rows if r["family_id"] == a and r["id"].endswith("-0"))
    shared = " ".join(full.split()[:8])  # one 8-token window of a's question ...
    for r in rows:
        if r["family_id"] == b and r["id"].endswith("-0"):
            r["question"] = shared + " tail"  # ... is also the start of b's
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    projects = tmp_path / "claude_projects"
    transcript = projects / st.mangle_path(world.repo) / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"content": shared}) + "\n", encoding="utf-8")
    code, out, err = world.run("audit", claude_projects=projects, staging_root=tmp_path / "staging")
    assert code == st.EXIT_HIT, err
    hit_families = {ln["family"] for ln in lines(out, "audit_hit")}
    assert hit_families == {a, b}  # one line per family, so the caller can exclude them all
    # exclude takes every family of the hit at once; both groups are excluded, each naming its members
    code, out, err = world.run("exclude", "--family", a, "--family", b, "--reason", "s_hit")
    assert code == 0, err
    assert [world.fields(ln)["outcome"] for ln in out.splitlines()] == ["ok", "ok"]
    assert world.ledger().exclusions() == frozenset({a, min(std.sealed_groups[0])})
    # an unknown family among them changes nothing; two families of one group are one exclusion
    c, d = std.sealed_singletons[1], std.sealed_groups[1]
    code, out, err = world.run("exclude", "--family", c, "--family", fid_of(0xFFFE), "--reason", "s_hit")
    assert code == st.EXIT_REFUSED and "reason=no_group" in err and out == ""
    assert world.ledger().exclusions() == frozenset({a, min(std.sealed_groups[0])})
    code, out, err = world.run("exclude", "--family", d[0], "--family", d[1], "--reason", "s_hit")
    assert code == 0 and out.count("exclude outcome=ok") == 1
    assert min(d) in world.ledger().exclusions() and c not in world.ledger().exclusions()


# --- B14 (Rev6): exclusions name their members; the freeze leaves the exclusion digest to the ledger -------------------------------
needs_a9 = pytest.mark.skipif(
    not hasattr(SealedLedger, "excluded_families"), reason="Area A9 (exclude(members=), excluded_families(), record_freeze(None)) has not landed"
)


def _spy_exclude(monkeypatch):
    """Record every ``SealedLedger.exclude`` call as (group id, reason, members), then run the real one."""
    import inspect

    real = SealedLedger.exclude
    calls = []

    def spy(self, group_id, reason, members=None):
        calls.append((group_id, reason, sorted(members) if members is not None else None))
        if "members" in inspect.signature(real).parameters:
            return real(self, group_id, reason, members=members)
        return real(self, group_id, reason)

    monkeypatch.setattr(SealedLedger, "exclude", spy)
    return calls


def test_exclude_hands_the_ledger_the_groups_member_families(world, monkeypatch):
    std = standard_released(world)
    calls = _spy_exclude(monkeypatch)
    group = sorted(std.sealed_groups[0])
    assert world.run("exclude", "--family", group[1], "--reason", "exposure")[0] == 0
    assert world.run("exclude", "--family", std.sealed_singletons[0], "--reason", "s_hit")[0] == 0
    assert calls == [(group[0], "exposure", group), (std.sealed_singletons[0], "s_hit", [std.sealed_singletons[0]])]


def test_a_development_twin_exclusion_after_the_sealed_freeze_also_names_its_members(world, monkeypatch):
    std = sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    world.approve_all("development")
    calls = _spy_exclude(monkeypatch)
    group = sorted(std.sealed_groups[2])
    realistic_fam = read_rows(world.dev / "realistic_r1.jsonl")[0]["family_id"]
    assert world.recheck("realistic-v2", [(realistic_fam, group[1])])[0] == 0
    assert calls == [(group[0], "twin_dev_version", group)]


def test_the_sealed_freeze_does_not_compute_the_exclusion_digest_itself_once_the_ledger_can(world, monkeypatch):
    from src.sealed_ledger import exclusions_digest

    real = SealedLedger.record_freeze
    seen = {}

    def spy(self, **kw):
        seen.update(kw)
        return real(self, **kw)

    monkeypatch.setattr(SealedLedger, "record_freeze", spy)
    sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    ledger_computes = hasattr(SealedLedger, "excluded_families")
    assert seen["exclusions_sha256"] == (None if ledger_computes else exclusions_digest(frozenset()))
    record = world.ledger().freeze_record("sealed")
    assert record["exclusions_sha256"] == exclusions_digest(world.ledger().exclusions_at_freeze("sealed"))


@needs_a9
def test_an_exclusion_that_lands_while_the_freeze_is_recorded_is_still_bound_to_the_freeze_record(world, monkeypatch):
    from src.sealed_ledger import exclusions_digest

    std = sealed_ready(world)
    real = SealedLedger.record_freeze
    target = std.sealed_singletons[2]

    def racing(self, **kw):  # a concurrent `exclude` lands right before the freeze record is written
        if kw.get("split") == "sealed":
            self.exclude(target, "exposure", members=[target])
        return real(self, **kw)

    monkeypatch.setattr(SealedLedger, "record_freeze", racing)
    assert world.recheck("sealed")[0] == 0
    ledger = world.ledger()
    assert ledger.exclusions_at_freeze("sealed") == frozenset({target})
    assert ledger.freeze_record("sealed")["exclusions_sha256"] == exclusions_digest(ledger.exclusions_at_freeze("sealed"))


@needs_a9
def test_a_pre_freeze_exclusion_of_a_group_renamed_by_a_later_rejection_still_counts(world):
    std = standard_released(world)
    small, big = sorted(std.sealed_groups[0])
    assert world.run("exclude", "--family", big, "--reason", "exposure")[0] == 0  # the group is named `small`
    assert world.run("sheet", "--kind", "sealed", "--name", "r1")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("sealed", "r1"), {small: "R"}, default="A")  # then `small` is rejected: `big` names it
    assert world.run("apply", "--kind", "sealed", "--sheet", sheet)[0] == 0
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    assert big in frozen_ids(world) and small not in frozen_ids(world)
    assert big in world.ledger().excluded_families()  # the exclusion resolves by member, not by the stale group id
    # `power` drops the renamed group from the answerable arms
    code, out, err = world.run("power", "--from-freeze", "--discordance", "0.3", "--draws", "10", "--seed", "3", "--effect", "0.1", "--alpha", "0.05")
    assert code == 0, err
    assert all(world.fields(ln)["families"] == "7" for ln in out.splitlines() if ln.startswith("power "))  # 8 answerable left, minus `big`
