"""Phase 16A-2a a3, acceptance (e): ``sealed_tool`` twins, groups, split, release and recovery.

Synthetic, schema-valid fixtures only (``tests/p16w_sealed_world.py``). The standard world forces the split:
12 sealed-eligible drafts fill the sealed quota exactly, because the other 8 are tied to public ids.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src import eval_schema
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    CANARY, build_standard, fid_of, public_id, read_rows, repos, standard_released, world, write_rows,
)


def twins_record(w, rnd: int = 1):
    ptr = json.loads((w.sealed / "work" / f"twins_current_r{rnd}.json").read_text())
    return w.ledger().load_payload(ptr["payload"], ptr["sha256"])


def assign_payload(w, rnd: int = 1):
    rec = [r for r in w.ledger().records("release") if r["round"] == rnd][0]
    return w.ledger().load_payload(f"assign_r{rnd}", rec["payload_sha256"])


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- release: the success path ---------------------------------------------------------------
def test_release_moves_development_families_unmarked_and_keeps_sealed_marked(world):
    std = standard_released(world)
    sealed_rows = read_rows(world.sealed / "sealed_r1.jsonl")
    assert {r["family_id"] for r in sealed_rows} == set(std.sealed) and len(sealed_rows) == 24
    assert all(r["sealed"] is True for r in sealed_rows)
    tuning, realistic = read_rows(world.dev / "tuning_r1.jsonl"), read_rows(world.dev / "realistic_r1.jsonl")
    assert {r["family_id"] for r in tuning} | {r["family_id"] for r in realistic} == set(std.dev_drafts)
    assert len({r["family_id"] for r in tuning}) == 5 and len({r["family_id"] for r in realistic}) == 3
    assert not any("sealed" in r for r in tuning + realistic)  # the only sanctioned unmarking
    assert "sealed" not in (world.dev / "tuning_r1.jsonl").read_text() and "sealed" not in (world.dev / "realistic_r1.jsonl").read_text()
    eval_schema.load_v2(world.dev / "tuning_r1.jsonl", inventory=eval_schema.load_inventory())  # a plain (non-sealed) loader accepts it
    # the pool is consumed, nothing is left staged, and the journal closed its transaction
    assert not list((world.sealed / "pool").rglob("*.jsonl"))
    work = world.sealed / "work"
    assert not any(p.name.startswith(("stage_", "consumed_")) for p in work.iterdir()) and (work / "release_r1.done").is_file()
    records = [json.loads(line) for line in (work / "release.journal").read_text().splitlines()]
    assert [r["t"] for r in records] == ["begin", "done"]
    kinds = [(r["kind"], r.get("outcome")) for r in world.ledger().records() if r["kind"] in ("split_attempt", "release")]
    assert kinds == [("split_attempt", "feasible"), ("release", None)]
    payload = assign_payload(world)
    assert payload["quotas"] == {"sealed": 12, "realistic": 4, "tuning": 8} and payload["dropped"] == []
    assert sorted(payload["ineligible"]) == sorted(std.dev_drafts)


def test_split_prints_counts_only_and_a_second_split_is_refused(world):
    std = standard_released(world)
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=already_released" in err and out == ""
    assert all(fid not in err for fid in std.sealed)


# --- twins: refusals, ineligible marks, edges ---------------------------------------------------
def test_twins_refuses_a_surviving_twin_pair_inside_the_pool(world):
    a, b, c = (world.draft() for _ in range(3))
    world.flush_pool()
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict([(a, b)]))
    assert code == st.EXIT_REFUSED and "reason=twin_in_pool pairs=1" in err
    assert not (world.sealed / "work" / "twins_current_r1.json").exists()  # nothing recorded
    assert world.ledger().records() == []
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict([(a, public_id(3))]))
    assert code == 0, err  # a twin of a public family is not "inside the pool": it marks the draft instead


def test_twins_marks_drafts_tied_to_pinned_or_public_families_and_passes_no_edge_for_them(world):
    p, p2 = world.pinned("tuning", source="legacy"), world.pinned("tuning", source="legacy")
    a, b = world.draft(), world.draft()
    c, d = world.draft(source="tutorial", tag="t5"), world.draft(source="tutorial", tag="t5")
    e = world.draft()
    world.flush_pool()
    dev_only = world.verdict([(p, p2)])  # the orchestrator's development-only verdict
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict([(a, p), (b, public_id(1))]), "--dev-verdict", dev_only)
    assert code == 0, err
    assert world.last(out, "twins")["twin_pairs"] == "3"
    rec = twins_record(world)
    assert rec["ineligible"] == sorted([a, b]) and rec["dropped"] == []
    assert rec["edges"] == [[c, d]] and rec["groups"] == {c: [c, d]}  # edges only among sealed-eligible drafts
    assert all(a not in edge and b not in edge for edge in rec["edges"])
    line = world.last(out, "twins")
    assert (line["ineligible"], line["edges"], line["groups"], line["pool_families"]) == ("2", "1", "1", "5")
    # `families_digest` and `pairs_covered` are the 16A-1 record's: every batch pair, self-pairs included
    assert sorted(rec["batches"]) == ["pinned", "pool"] and len(rec["pairs_covered"]) == 3


def test_a_p_a_b_chain_makes_the_whole_group_sealed_ineligible(world):
    """r4: pinned P, draft A sharing P's primary section, draft B sharing A's tutorial: P-A-B is one group."""
    p = world.pinned("tuning", source="legacy", section="2.2")
    a = world.draft(section="2.2", source="tutorial", tag="t7")
    b = world.draft(source="tutorial", tag="t7")  # shares nothing with P directly
    c = world.draft()
    world.flush_pool()
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict())
    assert code == 0, err
    rec = twins_record(world)
    assert rec["ineligible"] == sorted([a, b])  # B is ineligible although only A touches P
    assert rec["edges"] == [] and rec["groups"] == {}  # computed BEFORE filtering: no A-B edge is passed
    assert c not in rec["ineligible"]


def test_a_draft_sharing_an_eight_token_window_with_a_public_question_is_ineligible(world, eval_registry, tmp_path):
    pub_q = "pubq0 pubq1 pubq2 pubq3 pubq4 pubq5 pubq6 pubq7 pubq8"
    pub = tmp_path / "public" / "golden.jsonl"
    pub.parent.mkdir()
    pub.write_text(json.dumps({"question": pub_q, "type": "direct", "expected_sections": ["1.1"]}) + "\n", encoding="utf-8")
    eval_registry.add(pub, privacy="public")
    a = world.draft()
    b = world.draft()
    world.specs[a].first_question = "pubq0 pubq1 pubq2 pubq3 pubq4 pubq5 pubq6 pubq7 something else"  # one shared window
    world.flush_pool()
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict())
    assert code == 0, err
    assert twins_record(world)["ineligible"] == [a]


def test_twins_input_errors_print_fixed_reasons_only(world):
    world.draft()
    world.flush_pool()
    bad = world.sp_dir / "bad.jsonl"
    bad.parent.mkdir(exist_ok=True)
    bad.write_text(json.dumps({"a": CANARY, "b": "x y", "verdict": "twin"}) + "\nnot json\n", encoding="utf-8")
    code, out, err = world.run("twins", "--round", "1", "--verdict", bad)
    assert code == st.EXIT_USAGE and "reason=verdict_invalid lines=2" in err and CANARY not in out + err
    world.tags.clear()
    world.draft(source="tutorial", tag="t9")
    world.tags.clear()  # the tag map lacks the tutorial-sourced family
    world.write_tags()
    world.flush_pool("more.jsonl")
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict())
    assert code == st.EXIT_USAGE and "reason=tutorial_tag_missing families=1" in err


# --- split: the gate, register eligibility, attempts ----------------------------------------------
def _prepare_twins(w, pairs=()):
    code, out, err = w.run("twins", "--round", "1", "--verdict", w.verdict(pairs))
    assert code == 0, err


def test_split_refuses_an_unpushed_prereg_an_undeclared_round_and_an_uncommitted_or_modified_file(world):
    std = build_standard(world)
    _prepare_twins(world, std.verdict_pairs)
    world.write_prereg()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=prereg_uncommitted" in err  # written, not committed
    world.commit_prereg(push=False)
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=prereg_not_pushed" in err  # committed, not on origin
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_REFUSED and "reason=round1_missing" not in err  # a declared round passes the gate first
    from tests.test_p16w_sp_launch import git
    git(world.repo, "push", "--quiet", "origin", "main")
    world.prereg_path.write_text(world.prereg_path.read_text() + " ", encoding="utf-8")
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=prereg_modified" in err
    world.write_prereg()  # back to the committed bytes
    # nothing was released at any point
    assert (world.sealed / "pool" / "pool.jsonl").is_file() and not list(world.dev.glob("*_r1.jsonl"))
    assert world.ledger().records("release") == [] and world.ledger().records("split_attempt") == []


def test_split_refuses_a_round_the_pushed_prereg_does_not_declare(world):
    std = build_standard(world)
    world.write_prereg(rounds=[1])
    world.commit_prereg(push=True)
    _prepare_twins(world, std.verdict_pairs)
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_REFUSED and "reason=round_undeclared" in err
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, err  # the declared round releases


def test_split_without_a_current_twins_record_is_refused(world):
    std = build_standard(world)
    world.write_prereg()
    world.commit_prereg()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=twins_missing" in err
    _prepare_twins(world, std.verdict_pairs)
    world.draft()
    world.flush_pool("late.jsonl")  # the pool grew after `twins`
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=twins_stale" in err
    assert world.ledger().records("split_attempt") == []


def test_register_eligibility_infeasible_attempt_releases_nothing_and_the_first_feasible_one_releases(world):
    """Realistic takes lay / terse drafts and tutorial-sourced families; modelled drafts go to tuning or sealed only."""
    world.write_prereg()
    world.commit_prereg()
    for _ in range(3):
        world.pinned("tuning", source="legacy", register="modelled")
    world.pinned("realistic", source="tutorial", register="lay", tag="t01")
    refuse = [world.draft("refuse", register="modelled") for _ in range(6)]
    answers = [world.draft("answer", register="modelled") for _ in range(14)]  # not one lay, terse or tutorial draft
    world.flush_pool("first.jsonl")
    _prepare_twins(world)
    before = (world.sealed / "pool" / "first.jsonl").read_bytes()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_SPLIT_INFEASIBLE and "outcome=infeasible attempt=1" in out
    assert "split_constraint constraint=eligibility" in out  # which constraint, as an enum (realistic cannot be filled)
    assert (world.sealed / "pool" / "first.jsonl").read_bytes() == before and not list(world.dev.glob("*_r1.jsonl"))
    assert [(r["kind"], r.get("outcome")) for r in world.ledger().records()] == [("split_attempt", "infeasible")]
    # the SP drafts more (lay ones); a new twins record for the new pool, then the first feasible attempt releases
    world.draft("refuse", register="lay")
    for _ in range(3):
        world.draft("answer", register="lay")
    world.flush_pool("second.jsonl")
    _prepare_twins(world)
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, err
    assert world.last(out, "split")["attempt"] == "2" and world.last(out, "split")["outcome"] == "released"
    kinds = [(r["kind"], r.get("outcome")) for r in world.ledger().records()]
    assert kinds == [("split_attempt", "infeasible"), ("split_attempt", "feasible"), ("release", None)]
    realistic = read_rows(world.dev / "realistic_r1.jsonl")
    assert realistic and all(r["register"] in ("lay", "terse") or r["source"] == "tutorial" for r in realistic)
    payload = assign_payload(world)
    for rec in payload["families"]:
        if rec["register"] == "modelled" and rec["source"] != "tutorial_or_colleague":
            assert "realistic" not in rec["eligible"]


def test_quota_short_of_tuning_is_a_recorded_infeasible_attempt(world):
    world.write_prereg(quota={**world.prereg_doc()["quota"], "tuning_min": 30, "examples": []})
    world.commit_prereg()
    std = build_standard(world)
    _prepare_twins(world, std.verdict_pairs)
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_SPLIT_INFEASIBLE and "outcome=quota_short" in out
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == ["quota_short"]
    assert world.ledger().records("release") == []


# --- release: a crash between copy and delete is replayed ---------------------------------------------
@pytest.mark.parametrize("point", ["after_begin", "after_copy", "after_rename", "after_delete"])
def test_a_crash_in_the_release_move_is_recovered_without_loss_or_repeat(world, monkeypatch, point):
    std = build_standard(world)
    world.write_prereg()
    world.commit_prereg()
    _prepare_twins(world, std.verdict_pairs)
    pool_before = read_rows(world.sealed / "pool" / "pool.jsonl")

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_FAIL and "reason=internal" in err  # the crash; no exception text is shown
    assert len(world.ledger().records("release")) == 1  # the release was recorded before the move
    assert not (world.sealed / "work" / "release_r1.done").exists()
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    code, out, err = world.run("split", "--round", "1")  # any state-changing command recovers first
    assert code == st.EXIT_REFUSED and "reason=already_released" in err
    assert (world.sealed / "work" / "release_r1.done").is_file()
    moved = read_rows(world.sealed / "sealed_r1.jsonl") + read_rows(world.dev / "tuning_r1.jsonl") + read_rows(world.dev / "realistic_r1.jsonl")
    assert sorted(r["id"] for r in moved) == sorted(r["id"] for r in pool_before)  # nothing lost, nothing repeated
    assert not list((world.sealed / "pool").rglob("*.jsonl"))
    work = world.sealed / "work"
    assert not any(p.name.startswith(("stage_", "consumed_")) for p in work.iterdir())
    assert not [p for p in world.dev.iterdir() if p.suffix == ".partial" or ".partial" in p.name]
    assert len(world.ledger().records("release")) == 1 and len(world.ledger().records("split_attempt")) == 1
    assert sp.pending_moves(world.sealed / "work" / "release.journal") == []
    assert not any("sealed" in r for r in read_rows(world.dev / "tuning_r1.jsonl"))


# --- round 2 ---------------------------------------------------------------------------------------------
def _round2_world(w, replacement_phrasings: int = 2):
    """Standard round 1, one sealed answer rejected; round-2 drafts: one sealed replacement, one related to a sealed group,
    and two more (tied to public ids) that the quotas have no slot for: surplus, not tuning's "rest"."""
    std = standard_released(w)
    rejected = std.sealed_singletons[0]
    code, _, err = w.run("sheet", "--kind", "sealed", "--name", "r1")
    assert code == 0, err
    w.mark_sheet(w.sheet_path("sealed", "r1"), {rejected: "R"}, default="A")
    code, _, err = w.run("apply", "--kind", "sealed", "--sheet", w.sheet_path("sealed", "r1"))
    assert code == 0, err
    replacement = w.draft("answer", register="modelled", phrasings=replacement_phrasings)  # free: becomes the sealed replacement
    tuning_refuse = w.draft("refuse", register="lay")
    tuning_answer = w.draft("answer", register="lay")
    related = w.draft("answer", source="tutorial", tag="t10", register="lay")  # same source tutorial as a round-1 sealed group
    w.flush_pool("round2.jsonl")
    pairs = [(tuning_refuse, public_id(0xB00)), (tuning_answer, public_id(0xB01))]
    code, out, err = w.run("twins", "--round", "2", "--verdict", w.verdict(pairs))
    assert code == 0, err
    return std, rejected, replacement, tuning_refuse, tuning_answer, related


def test_round_two_drops_a_draft_related_to_a_round_one_sealed_group_and_releases_the_rest(world):
    std, rejected, replacement, t_refuse, t_answer, related = _round2_world(world)
    rec = twins_record(world, 2)
    assert rec["dropped"] == [related]  # dropped, never joined
    assert rejected not in {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}
    assert rejected in {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")}
    code, out, err = world.run("split", "--round", "2")
    assert code == 0, err
    line = world.last(out, "split")
    assert line["outcome"] == "released" and line["dropped"] == "1"
    assert {r["family_id"] for r in read_rows(world.sealed / "sealed_r2.jsonl")} == {replacement}  # fills the sealed replacement
    assert line["surplus"] == "2"
    assert not (world.dev / "tuning_r2.jsonl").exists()  # tuning is full (its 8 are pinned): the extra drafts are not forced into it
    assert {r["family_id"] for r in read_rows(world.sealed / "quarantine_r2.jsonl")} == {related, t_refuse, t_answer}  # dropped + surplus
    payload = assign_payload(world, 2)
    assert payload["quotas"] == {"sealed": 12, "realistic": 4, "tuning": 8}  # each split: pinned + replacements = round 1's quota
    assert payload["surplus"] == sorted([t_refuse, t_answer]) and t_refuse not in payload["assignment"]
    assert all(payload["assignment"][f] == "sealed" for f in std.sealed if f != rejected)  # round 1 survivors are pinned
    survivors = set(std.sealed) - {rejected}
    assert {f for f, s in payload["preassigned"].items() if s == "sealed"} == survivors  # as preassigned families
    assert set(std.dev_drafts) <= set(payload["preassigned"]) and not payload["preassigned"].get(replacement)  # and round 1's development
    assert related not in payload["assignment"]
    assert [r["round"] for r in world.ledger().records("release")] == [1, 2]
    assert not list((world.sealed / "pool").rglob("*.jsonl"))


def test_round_two_needs_round_one(world):
    world.write_prereg()
    world.commit_prereg()
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_REFUSED and "reason=round1_missing" in err


# --- exclude -----------------------------------------------------------------------------------------------
def test_exclude_acts_on_the_whole_sealed_group_and_never_moves_a_row(world):
    std = standard_released(world)
    before = sha(world.sealed / "sealed_r1.jsonl")
    group = std.sealed_groups[0]
    code, out, err = world.run("exclude", "--family", group[1], "--reason", "s_hit")
    assert code == 0, err
    assert world.last(out, "exclude")["outcome"] == "ok" and world.last(out, "exclude")["members"] == "2"
    assert world.ledger().exclusions() == frozenset({min(group)})  # the group id, not the family id
    code, out, err = world.run("exclude", "--family", group[0], "--reason", "s_hit")
    assert world.last(out, "exclude")["outcome"] == "already" and len(world.ledger().records("exclusion")) == 1
    code, out, err = world.run("exclude", "--family", std.sealed_singletons[0], "--reason", "exposure")
    assert code == 0 and world.last(out, "exclude")["members"] == "1"
    assert world.ledger().exclusions() == frozenset({min(group), std.sealed_singletons[0]})
    assert sha(world.sealed / "sealed_r1.jsonl") == before  # never a move or an unmark
    for argv in (["--family", fid_of(0xFFFF), "--reason", "s_hit"], ["--family", std.dev_drafts[0], "--reason", "s_hit"]):
        code, out, err = world.run("exclude", *argv)
        assert code == st.EXIT_REFUSED and "reason=no_group" in err
    for argv in (["--family", group[0], "--reason", "bogus"], ["--reason", "s_hit"], ["--family", group[0], "--group", group[0], "--reason", "s_hit"]):
        code, out, err = world.run("exclude", *argv)
        assert code == st.EXIT_USAGE


def test_a_group_is_named_by_its_smallest_surviving_family_so_exclude_and_the_slot_gate_agree(world):
    """The owner rejects the group's smallest member: the group id moves to the next family, not to a quarantined one."""
    from src.sealed_ledger import group_ids

    std = standard_released(world)
    rejected, kept = sorted(std.sealed_groups[0])
    code, _, err = world.run("sheet", "--kind", "sealed", "--name", "r1")
    assert code == 0, err
    world.mark_sheet(world.sheet_path("sealed", "r1"), {rejected: "R"}, default="A")
    assert world.run("apply", "--kind", "sealed", "--sheet", world.sheet_path("sealed", "r1"))[0] == 0
    code, out, err = world.run("exclude", "--family", kept, "--reason", "exposure")
    assert code == 0, err
    assert world.ledger().exclusions() == frozenset({kept})  # not `rejected`, which is no longer in the sealed set
    assert world.run("exclude", "--family", rejected, "--reason", "exposure")[0] == st.EXIT_REFUSED  # quarantined: no group
    # the slot gate's function, fed the same families and the released edges, names the same group
    survivors = {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")} - {rejected}
    edges = [tuple(e) for e in st.release_edges(st.release_payloads(world.ledger()))]
    assert group_ids(survivors, edges)[kept] == kept
    assert st.sealed_group_index(world.ledger(), survivors)[kept] == [kept]


# --- intake: a released SP dispatch becomes pool and tutorial map -----------------------------------------
DISPATCH = "0a1b2c3d"


def sp_dispatch(w, specs, tags=None, dispatch=DISPATCH):
    """What ``sp_launch`` leaves behind: ``sealed/work/sp_out/<id>/{pool.jsonl,tutorial_map.json}``."""
    out = w.sealed / "work" / "sp_out" / dispatch
    write_rows(out / "pool.jsonl", [r for s in specs for r in s.rows(sealed=True)])
    if tags is not None:
        (out / "tutorial_map.json").write_text(json.dumps(tags), encoding="utf-8")
    return out


def tutorial_map(w):
    path = w.sealed / "work" / "tutorial_map.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def test_intake_moves_the_pool_merges_the_tags_and_prints_counts_only(world):
    tut = world.spec("answer", source="tutorial", register="lay")
    hb = world.spec("partial", source="handbook", register="modelled")
    out = sp_dispatch(world, [tut, hb], {tut.fid: "T05"})
    before = (out / "pool.jsonl").read_bytes()
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == 0, stderr
    assert stdout == "intake families=2 rows=4 tags=1\n" and stderr == ""
    moved = world.sealed / "pool" / f"{DISPATCH}.jsonl"
    assert moved.read_bytes() == before and not (out / "pool.jsonl").exists()  # moved byte for byte
    assert tutorial_map(world) == {tut.fid: "t05"}  # stored lower-case
    assert (out / "tutorial_map.json").is_file()  # the SP's copy stays as the dispatch's record
    assert sp.pending_moves(world.sealed / "work" / "release.journal") == []
    # a second dispatch adds to the map and the pool without disturbing the first
    tut2 = world.spec("answer", source="tutorial", register="terse")
    sp_dispatch(world, [tut2], {tut2.fid: "TD3", hb.fid: "T17"}, dispatch="1b2c3d4e")
    code, stdout, stderr = world.run("intake", "--dispatch", "1b2c3d4e")
    assert code == 0, stderr
    assert tutorial_map(world) == {tut.fid: "t05", tut2.fid: "td3", hb.fid: "t17"}
    assert sorted(p.name for p in (world.sealed / "pool").iterdir()) == sorted([f"{DISPATCH}.jsonl", "1b2c3d4e.jsonl"])
    assert {r["family_id"] for r in read_rows(moved)} == {tut.fid, hb.fid}


@pytest.mark.parametrize("tag", ["T01", "T07", "T18", "T00", "TD0", "TD8", "t05", "T5", "T050", " T05", "T05\n", "X05", ""])
def test_intake_refuses_a_tag_outside_the_sp_pattern_and_changes_nothing(world, tag):
    tut = world.spec("answer", source="tutorial", register="lay")
    out = sp_dispatch(world, [tut], {tut.fid: tag})
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == st.EXIT_USAGE and "reason=input_invalid" in stderr and stdout == ""
    assert (out / "pool.jsonl").is_file() and not (world.sealed / "pool").exists() and tutorial_map(world) == {}


def test_intake_accepts_every_tag_of_the_pattern(world):
    tags = [f"T{n:02d}" for n in (2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)] + [f"TD{n}" for n in range(1, 8)]
    specs = [world.spec("answer", source="tutorial", register="lay") for _ in tags]
    sp_dispatch(world, specs, {s.fid: t for s, t in zip(specs, tags)})
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == 0, stderr
    assert stdout == f"intake families={len(tags)} rows={2 * len(tags)} tags={len(tags)}\n"


def test_intake_refusals_each_leave_the_dispatch_in_place(world):
    tut = world.spec("answer", source="tutorial", register="lay")
    other = world.spec("answer", source="handbook", register="modelled")
    stray = world.spec("answer", source="handbook", register="modelled")  # a family that is not in the pool

    def refused(reason, *, tags):
        out = sp_dispatch(world, [tut, other], tags)
        code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
        assert code == st.EXIT_USAGE and f"reason={reason}" in stderr and stdout == "", (code, stderr)
        assert (out / "pool.jsonl").is_file()  # nothing moved

    refused("tutorial_tag_missing", tags={other.fid: "T05"})  # a tutorial family needs a tag
    refused("input_invalid", tags={stray.fid: "T05", tut.fid: "T05"})  # a tag for a family that is not drafted
    refused("input_invalid", tags={"q:0123456789ab": "T05", tut.fid: "T05"})  # not a private family id
    refused("input_invalid", tags=[])  # not an object
    refused("input_invalid", tags={})  # an empty map is not a map
    assert tutorial_map(world) == {} and not (world.sealed / "pool").exists()
    # a bad dispatch id and a dispatch that does not exist
    assert world.run("intake", "--dispatch", "../x")[0] == st.EXIT_USAGE
    assert world.run("intake", "--dispatch", "ffffffff")[0] == st.EXIT_USAGE
    # a map that contradicts a stored tag
    sp_dispatch(world, [tut, other], {tut.fid: "T05"})
    assert world.run("intake", "--dispatch", DISPATCH)[0] == 0
    third = world.spec("answer", source="tutorial", register="lay")
    out = sp_dispatch(world, [third], {third.fid: "T06", tut.fid: "T09"}, dispatch="2c3d4e5f")
    code, stdout, stderr = world.run("intake", "--dispatch", "2c3d4e5f")
    assert code == st.EXIT_REFUSED and "reason=tag_conflict" in stderr and (out / "pool.jsonl").is_file()
    assert tutorial_map(world) == {tut.fid: "t05"}
    # a family id already in the pool, and a dispatch id that was already taken
    again = sp_dispatch(world, [tut], {tut.fid: "T05"}, dispatch="3d4e5f60")
    code, stdout, stderr = world.run("intake", "--dispatch", "3d4e5f60")
    assert code == st.EXIT_USAGE and "reason=input_invalid" in stderr and (again / "pool.jsonl").is_file()
    sp_dispatch(world, [third], {third.fid: "T06"}, dispatch=DISPATCH)  # fresh SP output under a dispatch id already taken
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == st.EXIT_REFUSED and "reason=already_intaken" in stderr


def test_intake_refuses_a_schema_invalid_pool_with_line_and_field_only_and_a_symlink(world, tmp_path):
    fam = world.spec("answer", source="handbook", register="modelled")
    out = sp_dispatch(world, [fam])
    rows = read_rows(out / "pool.jsonl")
    rows[1]["evidence"] = [[f"{CANARY}.9"]]  # a label outside the inventory
    rows[1]["question"] = f"{CANARY} leaked?"
    write_rows(out / "pool.jsonl", rows)
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == st.EXIT_USAGE and "schema_error line=2 field=evidence[0][0]" in stdout and CANARY not in stdout + stderr
    assert (out / "pool.jsonl").is_file() and not (world.sealed / "pool").exists()
    (out / "pool.jsonl").unlink()
    real = tmp_path / "elsewhere.jsonl"
    write_rows(real, fam.rows(sealed=True))
    (out / "pool.jsonl").symlink_to(real)
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == st.EXIT_USAGE and "reason=input_invalid" in stderr and real.is_file()


def test_a_crash_in_the_intake_move_is_finished_by_the_next_command(world, monkeypatch):
    fam = world.spec("answer", source="handbook", register="modelled")
    out = sp_dispatch(world, [fam])
    before = (out / "pool.jsonl").read_bytes()

    def boom(name: str) -> None:
        if name == "after_copy":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)
    assert code == st.EXIT_FAIL and "reason=internal" in stderr
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    code, stdout, stderr = world.run("intake", "--dispatch", DISPATCH)  # recovery runs first, then finds nothing to take
    assert code == st.EXIT_USAGE and "reason=input_missing" in stderr
    assert (world.sealed / "pool" / f"{DISPATCH}.jsonl").read_bytes() == before and not (out / "pool.jsonl").exists()
    assert sp.pending_moves(world.sealed / "work" / "release.journal") == []
