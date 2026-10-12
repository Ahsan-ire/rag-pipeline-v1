"""Phase 16A-2a a3, acceptance (e) and (j): ``sealed_tool freeze`` and the numbers that depend on it.

Synthetic, schema-valid fixtures only. The SP is faked: its ``recheck`` verdict and return files are written
by the test after the freeze screen, with mtimes after the screen's (the staleness guard).
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import os
import random
import stat
import time
from pathlib import Path

from scripts import sealed_tool as st
from src import eval_schema, eval_stats
from src.eval_sealed_read import load_sealed
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    fid_of, make_row, read_rows, repos, standard_released, world, write_rows,
)
from tests.test_p16w_sealed_tool_split import _round2_world


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sealed_ready(w):
    std = standard_released(w)
    w.approve_all("sealed")
    return std


def frozen_ids(w):
    return {r["family_id"] for r in read_rows(w.sealed / "heldout-v2.jsonl")}


def inv():
    return eval_schema.load_inventory()


# --- the recheck and the lexical screen ------------------------------------------------------------------------
def test_freeze_without_a_recheck_is_refused_and_writes_only_the_screen(world):
    sealed_ready(world)
    code, out, err = world.run("freeze", "--split", "heldout-v2")
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err and out == ""
    screen = json.loads((world.sealed / "work" / "screen_sealed.json").read_text())
    assert len(screen["content_digest"]) == 64 and screen["pairs"] == []  # unique tokens: no shared window
    assert not (world.sealed / "heldout-v2.jsonl").exists() and world.ledger().freeze_record("sealed") is None


def test_a_stale_or_invalid_recheck_is_refused(world):
    sealed_ready(world)
    code, out, err = world.run("freeze", "--split", "sealed")
    required = int(dict(p.split("=") for p in err.split()[1:] if "=" in p)["pairs_required"])
    assert required == 12 * 12  # every sealed family against every development family (4 pinned + 8 released)
    good = {"status": "ok", "kind": "recheck", "pairs_checked": required, "twin_pairs": 0}
    verdict = world.verdict()
    ret = verdict.with_name("ret.json")
    later = time.time() + 5

    def write_ret(doc):
        ret.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
        for p in (verdict, ret):
            os.utime(p, (later, later))

    args = ("freeze", "--split", "sealed", "--recheck-verdict", verdict, "--recheck-return", ret)
    write_ret(good)
    # a verdict older than the screen was not made on the content the screen saw
    os.utime(verdict, (1, 1))
    code, out, err = world.run(*args)
    assert code == st.EXIT_REFUSED and "reason=recheck_stale" in err
    # a return that is not an ok recheck of enough pairs
    for bad in ({**good, "kind": "twins"}, {**good, "twin_pairs": 3}, {**good, "pairs_checked": required - 1},
                {"status": "blocked", "reason": "task_unclear"}, "free text, not a return"):
        write_ret(bad)
        code, out, err = world.run(*args)
        assert code == st.EXIT_REFUSED and "reason=recheck_invalid" in err, bad
    write_ret(good)
    # content changed after the screen: the verdict is for other content
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    rows[0]["ambiguous"] = not rows[0]["ambiguous"]
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    code, out, err = world.run(*args)
    assert code == st.EXIT_REFUSED and "reason=recheck_stale" in err
    assert world.ledger().freeze_record("sealed") is None


# --- the sealed freeze: success path -----------------------------------------------------------------------------
def test_sealed_freeze_needs_no_development_approval_and_binds_the_records(world):
    std = sealed_ready(world)
    assert not (world.root / "work" / "approvals.json").exists()  # development is unapproved
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    line = world.last(out, "freeze")
    target = world.sealed / "heldout-v2.jsonl"
    assert line["status"] == "frozen" and line["families"] == "12" and line["rows"] == "24" and line["refuse_families"] == "3"
    assert line["eligible_answerable"] == "9" and line["groups"] == "9"  # three 2-groups and six singletons
    assert (line["demoted_groups"], line["repaired_groups"], line["excluded_groups"]) == ("0", "0", "0")
    assert frozen_ids(world) == set(std.sealed)
    rows = load_sealed(target, inventory=inv())
    assert len(rows) == 24 and "sealed" in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444  # immutable for the epoch
    record = world.ledger().freeze_record("sealed")
    assert record["set_sha256"] == sha(target) == line["sha256"] and record["totals"]["families"] == 12
    twins = world.ledger().load_payload("twins_sealed", record["twins_sha256"])
    assert set(twins["rounds"]) == {"1"} and twins["recheck_twin_pairs"] == [] and twins["families_sha256"] == sha(target)
    assert line["twins_sha256"] == record["twins_sha256"]
    assert record["exclusions_sha256"] == hashlib.sha256(b"[]").hexdigest()
    mirror = [json.loads(ln) for ln in world.repo.joinpath("eval", "sealed_ledger.jsonl").read_text().splitlines()]
    freeze_line = [m for m in mirror if m["kind"] == "freeze"][0]
    assert freeze_line["split"] == "sealed" and set(freeze_line["digests"]) == {"set_sha256", "twins_sha256", "exclusions_sha256"}
    assert not any(k in json.dumps(mirror) for k in ("scope", "chapter", "stratum", "families", "answerable"))
    # frozen: a second freeze and any further review are refused
    assert world.run("freeze", "--split", "sealed")[0] == st.EXIT_REFUSED
    world.run("sheet", "--kind", "sealed", "--name", "late", "--all")
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", world.sheet_path("sealed", "late"))
    assert code == st.EXIT_REFUSED and "reason=frozen" in err
    code, out, err = world.run("digest", target)
    assert code == 0 and world.last(out, "digest")["families"] == "12" and world.last(out, "digest")["sha256"] == sha(target)


def test_freeze_catches_a_missing_approval_and_changes_nothing(world):
    std = standard_released(world)
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {}, default="A")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names, lines = list(reader.fieldnames or []), list(reader)
    for line in lines:
        if line["family_id"] == std.sealed_singletons[0] and line["phrasing_no"] == "1":
            line["A"] = ""  # one phrasing stays unapproved
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
    assert world.run("apply", "--kind", "sealed", "--sheet", path)[0] == 0
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_REFUSED and "reason=approval_missing families=1" in err
    assert not (world.sealed / "heldout-v2.jsonl").exists() and world.ledger().freeze_record("sealed") is None


def test_freeze_catches_an_approval_that_no_longer_matches_its_row(world):
    sealed_ready(world)
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    rows[3]["question"] = "an edit after the owner approved this phrasing"
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_REFUSED and "reason=approval_missing families=1" in err


def test_freeze_catches_a_changed_assignment_and_a_planted_sealed_family(world):
    std = sealed_ready(world)
    moved = std.sealed_singletons[0]
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    leaving = [dict(r) for r in rows if r["family_id"] == moved]
    write_rows(world.sealed / "sealed_r1.jsonl", [r for r in rows if r["family_id"] != moved])
    for r in leaving:
        r.pop("sealed")
    tuning = read_rows(world.dev / "tuning_r1.jsonl")
    write_rows(world.dev / "tuning_r1.jsonl", [*tuning, *leaving])  # a sealed-assigned family now sits in development
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=assignment_changed families=1" in err
    # and a family that was never assigned appears in the sealed side
    write_rows(world.dev / "tuning_r1.jsonl", tuning)
    write_rows(world.sealed / "sealed_r1.jsonl", [*rows, *[make_row(fid_of(0xDD0), n, evidence=[["9.20"]]) for n in range(2)]])
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=assignment_changed families=1" in err


def test_freeze_replays_from_the_stored_payload_and_stops_if_the_payload_is_gone(world, monkeypatch):
    std = sealed_ready(world)
    real = st.split_families
    monkeypatch.setattr(st, "split_families", lambda *a, **k: {**real(*a, **k), std.sealed[0]: "tuning"})  # the replay disagrees
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=replay_mismatch" in err
    monkeypatch.setattr(st, "split_families", real)
    for directory in world.ledger()._payload_dirs:  # both copies of the payload are altered
        path = directory / "assign_r1.json"
        path.write_bytes(path.read_bytes().replace(b'"seed"', b'"sede"'))
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_LEDGER_STOP and "reason=epoch_stop" in err


# --- twins found at freeze --------------------------------------------------------------------------------------------
def test_a_sealed_phrasing_planted_in_a_development_row_demotes_the_whole_group_and_keeps_approvals(world):
    std = sealed_ready(world)
    group = std.sealed_groups[0]
    sealed_q = next(r["question"] for r in read_rows(world.sealed / "sealed_r1.jsonl") if r["family_id"] == group[0] and r["id"].endswith("-0"))
    planted = fid_of(0xC00)
    write_rows(world.dev / "tuning_planted.jsonl", [
        make_row(planted, 0, sealed=False, source="legacy", question=f"before {sealed_q} after", evidence=[["9.19"]]),
        make_row(planted, 1, sealed=False, source="legacy", evidence=[["9.19"]]),
    ])
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err
    pairs = json.loads((world.sealed / "work" / "screen_sealed.json").read_text())["pairs"]
    assert pairs == [[group[0], planted]]  # the lexical screen found the shared window
    code, out, err = world.recheck("sealed", [(group[0], planted)])
    assert code == 0, err
    line = world.last(out, "freeze")
    assert line["demoted_groups"] == "1" and line["families"] == "10"
    assert frozen_ids(world) == set(std.sealed) - set(group)  # the WHOLE group left, not just the twin
    quarantined = read_rows(world.sealed / "quarantine.jsonl")
    assert {r["family_id"] for r in quarantined} == set(group) and all(r["sealed"] is True for r in quarantined)  # still sealed
    approved = json.loads((world.sealed / "work" / "approvals.json").read_text())["approved"]
    assert all(f"{fid}-{n}" in approved for fid in group for n in (0, 1))  # approvals kept
    assert "sealed" not in (world.dev / "tuning_planted.jsonl").read_text()  # development was not touched
    groups = st.sealed_group_index(world.ledger())
    survivors = frozen_ids(world)
    for members in groups.values():  # the group invariant: every group is wholly in or wholly out
        assert len(set(members) & survivors) in (0, len(members))
    record = world.ledger().freeze_record("sealed")
    assert world.ledger().load_payload("twins_sealed", record["twins_sha256"])["demoted_groups"] == [min(group)]


def test_a_twin_inside_sealed_drops_one_of_the_pair(world):
    std = sealed_ready(world)
    a, b = std.sealed_singletons[0], std.sealed_singletons[1]
    code, out, err = world.recheck("sealed", [(a, b)])
    assert code == 0, err
    assert frozen_ids(world) == set(std.sealed) - {max(a, b)} and world.last(out, "freeze")["demoted_groups"] == "1"
    assert world.run("freeze", "--split", "sealed")[0] == st.EXIT_REFUSED  # and it is frozen now


def test_a_twin_pair_inside_one_group_drops_nothing(world):
    """A twin pair inside ONE group is what the group already is."""
    std = sealed_ready(world)
    code, out, err = world.recheck("sealed", [tuple(std.sealed_groups[1])])
    assert code == 0, err
    assert frozen_ids(world) == set(std.sealed) and world.last(out, "freeze")["demoted_groups"] == "0"


# --- band, phrasings, floor ---------------------------------------------------------------------------------------------
def _reject(world, families):
    world.run("sheet", "--kind", "sealed", "--name", "r1", "--all")
    path = world.mark_sheet(world.sheet_path("sealed", "r1"), {f: "R" for f in families}, default="A")
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
    assert code == 0, err


def test_a_sealed_band_breach_is_repaired_by_the_fewest_seeded_group_demotions(world):
    std = standard_released(world)
    _reject(world, [std.sealed_refuse[0]])  # 11 families, 2 refuse: 18 %, below the 25 % band
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    line = world.last(out, "freeze")
    assert line["repaired_groups"] == "2" and line["families"] == "8" and line["refuse_families"] == "2"  # 2/8 = 25 %
    kept = frozen_ids(world)
    removed = set(std.sealed) - {std.sealed_refuse[0]} - kept
    group_of = {f: tuple(g) for g in std.sealed_groups for f in g}
    removed_groups = {group_of.get(f, (f,)) for f in removed}
    assert len(removed_groups) == 2 and len(removed) == 3  # one 2-group and one singleton: fewer groups than three singletons
    assert all(set(g) <= removed for g in removed_groups)  # whole groups
    assert {r["family_id"] for r in read_rows(world.sealed / "quarantine.jsonl")} == removed | {std.sealed_refuse[0]}
    assert all(f in std.sealed_refuse for f in kept & set(std.sealed_refuse))


def test_repair_band_finds_a_minimum_seeded_and_reproducible_set():
    rng = random.Random(7)
    for trial in range(40):
        units = [(f"g{i}", rng.randint(1, 3), 0) for i in range(rng.randint(6, 9))]
        units = [(g, n, rng.randint(0, n) if rng.random() < 0.3 else 0) for g, n, _ in units]
        q, r = sum(u[1] for u in units), sum(u[2] for u in units)
        floor_q = max(2, q // 2)
        ok = lambda keep: (lambda q2, r2: q2 >= floor_q and 4 * r2 >= q2 and 10 * r2 <= 3 * q2)(sum(u[1] for u in keep), sum(u[2] for u in keep))  # noqa: E731
        brute = None
        for k in range(1, 5):
            if any(ok([u for u in units if u not in combo]) for combo in itertools.combinations(units, k)):
                brute = k
                break
        got = st.repair_band(units, floor_q, seed=trial)
        if brute is None:
            assert got is None or len(got) > 4
        else:
            assert got is not None and len(got) == brute  # the fewest groups
            assert ok([u for u in units if u[0] not in got])
            assert st.repair_band(units, floor_q, seed=trial) == got  # seeded: reproducible
    # the seed breaks ties between equally small choices
    units = [(f"g{i}", 1, 0) for i in range(8)] + [("r", 1, 1)]
    picks = {tuple(st.repair_band(units, 1, seed=s) or ()) for s in range(30)}
    assert len(picks) > 1 and all(len(p) == len(next(iter(picks))) for p in picks)


def test_a_phrasing_breach_is_the_d64_stop_with_its_own_exit(world):
    std = standard_released(world)
    rows = [r for r in read_rows(world.sealed / "sealed_r1.jsonl") if r["id"] != f"{std.sealed_singletons[0]}-1"]  # a family with 1 phrasing
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE == 9 and "reason=phrasing_breach families=1" in err
    assert world.ledger().freeze_record("sealed") is None and not (world.sealed / "heldout-v2.jsonl").exists()
    extra = [*rows, make_row(std.sealed_singletons[0], 1, evidence=[["9.1"]]), make_row(std.sealed_singletons[0], 2, evidence=[["9.1"]]),
             make_row(std.sealed_singletons[0], 3, evidence=[["9.1"]])]
    write_rows(world.sealed / "sealed_r1.jsonl", extra)  # four phrasings: also a breach
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "reason=phrasing_breach" in err


def test_a_floor_breach_is_the_d64_stop_with_its_own_exit(world):
    std = standard_released(world)
    _reject(world, [std.sealed_refuse[0], *std.sealed_singletons, std.sealed_groups[0][0]])  # 7 left, 2 refuse: the band holds
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "reason=freeze_infeasible" in err
    assert "freeze_constraint constraint=floor" in out and "constraint=band" not in out
    assert world.ledger().freeze_record("sealed") is None


# --- development freezes -------------------------------------------------------------------------------------------------
def test_a_development_split_freezes_unmarked_after_its_own_validation(world):
    std = standard_released(world)
    code, out, err = world.recheck("realistic-v2")
    assert code == st.EXIT_REFUSED and "reason=approval_missing" in err  # new development labels need the owner
    world.approve_all("development")
    code, out, err = world.recheck("realistic-v2")
    assert code == 0, err
    line = world.last(out, "freeze")
    target = world.dev / "realistic-v2.jsonl"
    assert line["families"] == "4" and line["refuse_families"] == "1" and "twins_sha256" not in line
    assert "sealed" not in target.read_text() and stat.S_IMODE(target.stat().st_mode) == 0o444
    eval_schema.load_v2(target, inventory=inv())
    assert world.ledger().freeze_record("realistic")["set_sha256"] == sha(target)
    assert world.ledger().freeze_record("sealed") is None and world.ledger().freeze_record("tuning") is None


def test_a_development_twin_of_a_frozen_sealed_group_appends_an_exclusion_not_a_move(world):
    std = sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    sealed_before = sha(world.sealed / "heldout-v2.jsonl")
    world.approve_all("development")
    realistic_fam = read_rows(world.dev / "realistic_r1.jsonl")[0]["family_id"]
    group = std.sealed_groups[2]
    code, out, err = world.recheck("realistic-v2", [(realistic_fam, group[1])])
    assert code == 0, err
    assert world.last(out, "freeze")["excluded_groups"] == "1"
    assert world.ledger().exclusions() == frozenset({min(group)})  # the whole sealed group, as a record
    assert [r["reason"] for r in world.ledger().records("exclusion")] == ["twin_dev_version"]
    assert sha(world.sealed / "heldout-v2.jsonl") == sealed_before and frozen_ids(world) == set(std.sealed)  # never a move


# --- round 2 ------------------------------------------------------------------------------------------------------------
def _round2_released(world, replacement_phrasings: int = 2):
    out = _round2_world(world, replacement_phrasings)
    assert world.run("split", "--round", "2")[0] == 0
    return out


def test_freeze_refuses_a_round_two_family_without_two_phrasings_an_approval_or_a_recheck(world):
    std, rejected, replacement, *_ = _round2_released(world, replacement_phrasings=1)
    code, out, err = world.run("freeze", "--split", "sealed")  # no recheck yet
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err
    code, out, err = world.recheck("sealed")  # a recheck but no owner approval of the round-2 family
    assert code == st.EXIT_REFUSED and "reason=approval_missing families=1" in err
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")  # approved, but one phrasing only
    assert code == st.EXIT_FREEZE_INFEASIBLE and "reason=phrasing_breach families=1" in err
    assert world.ledger().freeze_record("sealed") is None
    # with two phrasings, an approval and a recheck, the round-2 family freezes with the survivors
    rows = read_rows(world.sealed / "sealed_r2.jsonl")
    write_rows(world.sealed / "sealed_r2.jsonl", [*rows, make_row(replacement, 1, evidence=rows[0]["evidence"], register="modelled")])
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    assert frozen_ids(world) == (set(std.sealed) - {rejected}) | {replacement}
    twins = world.ledger().load_payload("twins_sealed", world.ledger().freeze_record("sealed")["twins_sha256"])
    assert set(twins["rounds"]) == {"1", "2"}  # both rounds' twins records are bound


def test_round_two_is_closed_once_the_sealed_set_is_frozen(world):
    sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_REFUSED and "reason=already_frozen" in err


# --- power from the frozen set -------------------------------------------------------------------------------------------
def test_power_from_the_frozen_set_uses_eligible_answerable_families_by_group(world):
    std = sealed_ready(world)
    assert world.recheck("sealed")[0] == 0
    args = ("power", "--from-freeze", "--discordance", "0.3", "--draws", "20", "--seed", "3", "--effect", "0.1", "--alpha", "0.05")
    code, out, err = world.run(*args)
    assert code == 0, err
    lines = [world.fields(ln) for ln in out.splitlines()]
    models = {ln["model"]: ln for ln in lines if ln["_kind"] == "power"}
    assert set(models) == {"independent", "correlated"}
    assert all(m["families"] == "9" and m["groups"] == "6" for m in models.values())  # 3 two-groups + 3 singletons, refusals excluded
    ref = [ln for ln in lines if ln["_kind"] == "power_reference"][0]
    assert int(ref["power_pm"]) == round(1000 * eval_stats.power(9, 0.3, 0.1, 0.05, "directional"))
    assert world.run("exclude", "--family", std.sealed_groups[0][0], "--reason", "exposure")[0] == 0
    code, out, err = world.run(*args)
    again = {world.fields(ln)["model"]: world.fields(ln) for ln in out.splitlines() if ln.startswith("power ")}
    assert all(m["families"] == "7" and m["groups"] == "5" for m in again.values())  # the excluded group is out of both arms


# --- strata, min_share and the constraint checker -------------------------------------------------------------------------
def test_a_min_share_breach_after_a_rejection_is_the_d64_stop_and_changes_nothing(world):
    world.prereg_extra = {"min_share": {"realistic": {"tutorial_or_colleague": 0.25}, "sealed": {"tutorial_or_colleague": 0.5}}}
    std = standard_released(world)  # 6 of the 12 sealed families are tutorial-sourced: exactly the share
    _reject(world, [std.sealed_groups[0][0]])  # 5 of 11 is under ceil(0.5 * 11) = 6
    before = (world.sealed / "sealed_r1.jsonl").read_bytes()
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "freeze_constraint constraint=min_share" in out and "constraint=band" not in out
    assert (world.sealed / "sealed_r1.jsonl").read_bytes() == before and world.ledger().freeze_record("sealed") is None


def test_a_strata_breach_that_survives_the_band_repair_stops_before_any_move(world):
    world.prereg_extra = {"strata": {"sealed": {"refuse|refuse": 3}}}
    std = standard_released(world)
    _reject(world, [std.sealed_refuse[0]])  # 2 refuse families left: the stratum minimum is 3 and the band is breached too
    quarantine_before = (world.sealed / "quarantine.jsonl").read_bytes()
    code, out, err = world.recheck("sealed")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "freeze_constraint constraint=strata" in out
    assert "constraint=band" not in out  # the band could be repaired in memory; the stratum could not
    assert (world.sealed / "quarantine.jsonl").read_bytes() == quarantine_before  # the in-memory repair was never applied
    assert len(frozen_ids_or_none(world)) == 0


def frozen_ids_or_none(world):
    path = world.sealed / "heldout-v2.jsonl"
    return {r["family_id"] for r in read_rows(path)} if path.exists() else set()


def _fam(fid, scope="answer", chapter="1", source="handbook"):
    evidence = [] if scope == "refuse" else [[f"{chapter}.1"]]
    return st.Fam(fid, [make_row(fid, 0, scope=scope, evidence=evidence, source=source)])


PREREG = {"quota": {"floor_sealed": 6, "floor_answerable": 4}, "strata": {"sealed": {"answer|1": 2}}, "min_share": {"sealed": {"tutorial_or_colleague": 0.25}}}


def _eval(fams, split="sealed", groups=None, placement=None, excluded=()):
    placement = placement if placement is not None else {f.fid: split for f in fams}
    return st.evaluate_split({f.fid: f for f in fams}, split, PREREG, placement, groups or {}, set(excluded))


def _base(n_refuse=2, n_answer=6, tutorials=2, chapter="1"):
    fams = [_fam(fid_of(i), "refuse") for i in range(n_refuse)]
    fams += [_fam(fid_of(100 + i), "answer", chapter, "tutorial" if i < tutorials else "handbook") for i in range(n_answer)]
    return fams


def test_evaluate_split_names_each_violated_constraint_and_only_that_one():
    assert _eval(_base()) == []  # 2/8 refuse = 25 %, 6 answers in the stratum, 2 tutorial = ceil(25 % of 8), 8 >= floor, 6 answerable
    assert _eval(_base(n_refuse=1, n_answer=6)) == ["band"]  # 1/7 is under 25 %
    assert _eval(_base(n_refuse=0, n_answer=6)) == ["band"]
    assert _eval(_base(n_refuse=3, n_answer=5)) == ["band"]  # 3/8 is over 30 %
    assert _eval(_base(n_refuse=1, n_answer=3, tutorials=1)) == ["floor"]  # band ok (1/4), under the floor of 6 families
    assert _eval(_base(tutorials=0)) == ["min_share"]
    assert _eval(_base(chapter="2")) == ["strata"]  # no answer family in chapter 1
    fams = _base()
    answers = [f.fid for f in fams if f.scope == "answer"]
    assert _eval(fams, excluded=answers[:3]) == ["floor"]  # exclusions leave 3 eligible answerable families (< 4)
    assert _eval(fams, excluded=answers[:2]) == []
    assert _eval(_base(n_refuse=1, n_answer=3, tutorials=1), split="tuning") == []  # the floor, strata and min_share are the sealed split's


def test_evaluate_split_group_invariant_needs_every_group_wholly_in_one_split():
    fams = _base()
    groups = {"g": [fams[2].fid, fams[3].fid]}
    place = {f.fid: "sealed" for f in fams}
    assert _eval(fams, groups=groups, placement=place) == []
    place[fams[3].fid] = "tuning"  # one member of the group is in another split
    assert _eval(fams, groups=groups, placement=place) == ["group_invariant"]
    del place[fams[3].fid]  # demoted to quarantine: absent from every split, not split across two
    assert _eval(fams, groups=groups, placement=place) == []
