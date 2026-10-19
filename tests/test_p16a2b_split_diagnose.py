"""16A-2b: ``sealed_tool split --round R --diagnose``: the split attempt shown, never recorded.

A diagnosis runs ``split``'s gates and computations through the same code (``plan_split`` then ``attempt_split``) and prints
the outcome, every constraint of an infeasible attempt (violated or not) and the round's supply: integers and enum words only,
through the one allowlisted printer. It records nothing, stores no payload, releases and moves nothing, does not count toward
the attempt cap, and takes the same state lock as ``split``. The real ``split`` is unchanged, byte for byte (the goldens below
were captured from the tool at 2dd622b, before ``cmd_split`` was split into plan / attempt / conclude).

Synthetic fixtures only (``tests/p16w_sealed_world.py``); nothing here reads a real eval set, the corpus or ``eval/private/``.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src.eval_split import SplitInfeasible
from tests.p16w_sealed_world import build_standard, read_rows, repos, standard_released, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r2_tool import _terse_pool
from tests.test_p16a2b_b2_quota import _quota_world
from tests.test_p16w_sealed_tool import assert_allowlisted
from tests.test_p16w_sealed_tool_split import _prepare_twins, _round2_world, assign_payload

DIAGNOSE = ("split", "--round", "1", "--diagnose")


# --- helpers ------------------------------------------------------------------------------------------------------------------
def snapshot(w) -> Dict[str, Tuple[int, int, str]]:
    """Every file under the test's tmp dir (private root, out-of-tree ledger and payloads, the mirror, the repo's files) except git's
    own: relative path -> (size, mtime_ns, sha256). Equal snapshots mean nothing was written, moved, created or deleted."""
    out: Dict[str, Tuple[int, int, str]] = {}
    skip = {".git", "origin.git", "other", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(w.tmp):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            path = Path(dirpath) / name
            stat = path.stat()
            out[str(path.relative_to(w.tmp))] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def kinds(w, *names: str) -> List[Tuple[str, Any]]:
    return [(r["kind"], r.get("outcome")) for r in w.ledger().records() if not names or r["kind"] in names]


def modelled_world(w) -> None:
    """Infeasible: every draft is modelled, so none can go to realistic (which holds 1 pinned family of its quota of 4)."""
    w.write_prereg()
    w.commit_prereg()
    for _ in range(3):
        w.pinned("tuning", source="legacy", register="modelled")
    w.pinned("realistic", source="tutorial", register="lay", tag="t01")
    for _ in range(6):
        w.draft("refuse", register="modelled")
    for _ in range(14):
        w.draft("answer", register="modelled")
    w.flush_pool("first.jsonl")
    _prepare_twins(w)


def standard_pool(w, **marks: str):
    """The standard 24-family world with its twins recorded, nothing split yet."""
    std = build_standard(w, **marks)
    w.write_prereg()  # (``prereg_extra``, e.g. strata, is part of every prereg the world writes)
    w.commit_prereg(push=True)
    _prepare_twins(w, std.verdict_pairs)
    return std


def lines_of(w, out: str, kind: str) -> List[Dict[str, str]]:
    return [w.fields(ln) for ln in out.splitlines() if ln.split(" ")[0] == kind]


# --- the output: an infeasible attempt ------------------------------------------------------------------------------------------
INFEASIBLE_OUTPUT = """\
diagnose round=1 outcome=infeasible
diagnose_constraint constraint=quota_total families=24 quota_sum=24 violated=no
diagnose_constraint constraint=preassignment_conflicts units=0 violated=no
diagnose_constraint constraint=preassigned_ineligible units=0 violated=no
diagnose_constraint constraint=unit_no_allowed_split units=0 violated=no
diagnose_constraint constraint=eligibility split=realistic quota=4 eligible_families=1 preassigned=1 violated=yes
diagnose_constraint constraint=eligibility split=sealed quota=12 eligible_families=20 preassigned=0 violated=no
diagnose_constraint constraint=eligibility split=tuning quota=8 eligible_families=23 preassigned=3 violated=no
diagnose_constraint constraint=band split=realistic quota=4 refuse_min=1 refuse_max=1 refuse_available=0 non_refuse_available=1 refuse_preassigned=0 violated=yes
diagnose_constraint constraint=band split=sealed quota=12 refuse_min=3 refuse_max=3 refuse_available=6 non_refuse_available=14 refuse_preassigned=0 violated=no
diagnose_constraint constraint=band split=tuning quota=8 refuse_min=2 refuse_max=2 refuse_available=6 non_refuse_available=17 refuse_preassigned=0 violated=no
diagnose_constraint constraint=strata entries=0 violated=no
diagnose_constraint constraint=min_share split=realistic source=tutorial_or_colleague required=1 available=1 violated=no
diagnose_constraint constraint=min_share split=realistic total=yes required=1 quota=4 violated=no
diagnose_constraint constraint=search nodes=0 budget=200000 exhausted=no violated=no
diagnose_pool round=1 records=24 pinned=4 groups=0 surplus=0 tuning_floor=8 attempts=0 attempts_cap=3
diagnose_split split=sealed quota=12 pinned_families=0 pinned_refusals=0
diagnose_split split=tuning quota=8 pinned_families=3 pinned_refusals=0
diagnose_split split=realistic quota=4 pinned_families=1 pinned_refusals=0
diagnose_register lay_or_terse=0 lay=0 lay_needed=0 short=no
diagnose_drafts class=total families=20 answer=14 partial=0 refuse=6
diagnose_drafts class=sealed_eligible families=20 answer=14 partial=0 refuse=6
diagnose_drafts class=sealed_ineligible families=0 answer=0 partial=0 refuse=0
diagnose_drafts class=dropped families=0 answer=0 partial=0 refuse=0
diagnose_drafts class=realistic_eligible families=0 answer=0 partial=0 refuse=0
"""


def test_an_infeasible_attempt_prints_every_constraint_with_its_numbers_and_the_supply(world):
    modelled_world(world)
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE == 6 and err == ""
    assert out == INFEASIBLE_OUTPUT
    assert_allowlisted(out)


def _spy_counts(monkeypatch) -> List[Dict[str, Any]]:
    """The ``SplitInfeasible.counts`` the splitter raised, as the tool saw them."""
    seen: List[Dict[str, Any]] = []
    real = st.assign_round

    def spy(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        except SplitInfeasible as exc:
            seen.append(exc.counts)
            raise

    monkeypatch.setattr(st, "assign_round", spy)
    return seen


def _expected_records(counts: Dict[str, Any]) -> List[Tuple[str, set]]:
    """(constraint, the ``key=value`` tokens its line must carry) for every record of ``counts``: every integer under its own key
    name and ``violated``; an entry with nothing registered under it is one ``entries=0`` record."""
    want: List[Tuple[str, set]] = []

    def walk(name: str, node: Dict[str, Any]) -> None:
        if not node:
            want.append((name, {"entries=0", "violated=no"}))
        elif any(not isinstance(v, dict) for v in node.values()):
            toks = {f"{k}={v}" for k, v in node.items() if isinstance(v, int) and not isinstance(v, bool)}
            toks.update(f"{k}={'yes' if v else 'no'}" for k, v in node.items() if isinstance(v, bool))
            want.append((name, toks))
        else:
            for sub in node.values():
                walk(name, sub)

    for name, node in counts.items():
        walk(name, node)
    return want


def test_every_integer_of_every_counts_entry_is_printed_under_its_own_key(world, monkeypatch):
    seen = _spy_counts(monkeypatch)
    world.prereg_extra = {"strata": {"sealed": {"refuse|refuse": 5, "answer|1": 2}}}
    standard_pool(world)
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE and seen, err
    got = [ln.split(" ") for ln in out.splitlines() if ln.startswith("diagnose_constraint ")]
    want = _expected_records(seen[-1])
    assert len(got) == len(want)
    for tokens, (name, toks) in zip(got, want):
        assert f"constraint={name}" in tokens and toks <= set(tokens), (tokens, toks)
    # the strata lines name the stratum by a scope word and a chapter number, and the totals say so
    assert "diagnose_constraint constraint=strata split=sealed stratum_scope=answer stratum_chapter=1 required=2 available=1 violated=yes" in out
    assert "diagnose_constraint constraint=strata split=sealed stratum_scope=refuse stratum_chapter=refuse required=5 available=3 violated=yes" in out
    assert "diagnose_constraint constraint=strata split=sealed total=yes required=7 quota=12 violated=no" in out
    # `violated` is the last field of a line, and every line of the counts shows whether it was violated
    assert all(ln.split(" ")[-1] in ("violated=yes", "violated=no") for ln in out.splitlines() if ln.startswith("diagnose_constraint "))
    assert_allowlisted(out)


def test_a_constraint_label_the_printer_cannot_vouch_for_is_printed_as_other(world):
    """A stratum chapter that is not a number (here a label of the test inventory) never reaches the output."""
    world.prereg_extra = {"strata": {"sealed": {"answer|CANARYSEC": 3}}}
    standard_pool(world)
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE, err
    assert "constraint=strata split=sealed stratum_scope=answer stratum_chapter=other required=3 available=0 violated=yes" in out
    assert "canarysec" not in out.lower() and err == ""


def test_the_flattening_of_a_counts_record(world):
    assert list(st._flat_numbers({"a": 1, "violated": False, "b": {"c": 2, "d": {"e": 3}}, "f": [1, 2, 3], "g": True})) == [
        ("a", 1), ("b_c", 2), ("b_d_e", 3), ("f", 3), ("g", "yes"), ("violated", "no"),
    ]
    out = io.StringIO()
    ctx = st.default_ctx(st.Printer(out, io.StringIO()), private=world.root, repo=world.repo)
    st.emit_constraints(ctx, {"strata": {}, "mystery": {"s": {"x": 1, "violated": True}}, "search": {"nodes": 3, "violated": True, "final_check_failed": ["band:sealed", "unit"]}})
    assert out.getvalue().splitlines() == [
        "diagnose_constraint constraint=strata entries=0 violated=no",
        "diagnose_constraint constraint=other split=other x=1 violated=yes",
        "diagnose_constraint constraint=search nodes=3 final_check_failed=2 violated=yes",
    ]


# --- the other outcomes -------------------------------------------------------------------------------------------------------
def test_quota_short_prints_the_floor_it_missed_and_no_constraint(world):
    _quota_world(world, tuning_refusals=3)  # tuning's quota 8 is under its own floor, 10
    before, records = snapshot(world), world.ledger().records()
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE and err == ""
    assert out.splitlines()[0] == "diagnose round=1 outcome=quota_short" and "diagnose_constraint" not in out
    assert "diagnose_pool round=1 records=27 pinned=8 groups=0 surplus=0 tuning_floor=10 attempts=0 attempts_cap=3" in out
    assert "diagnose_split split=tuning quota=8 pinned_families=3 pinned_refusals=3" in out
    assert "diagnose_split split=realistic quota=7 pinned_families=5 pinned_refusals=0" in out
    assert snapshot(world) == before and world.ledger().records() == records
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == []
    assert world.run("split", "--round", "1")[0] == st.EXIT_SPLIT_INFEASIBLE  # the real attempt agrees
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == ["quota_short"]


def test_a_quota_below_zero_is_printed_as_zero_and_flagged(world):
    world.write_prereg()
    world.commit_prereg()
    for _ in range(10):  # 10 families: sealed 12 + realistic 4 leave tuning -6
        world.draft("answer", register="lay")
    world.flush_pool()
    _prepare_twins(world)
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE and err == "" and out.splitlines()[0] == "diagnose round=1 outcome=quota_short"
    assert "diagnose_split split=tuning quota=0 quota_negative=yes pinned_families=0 pinned_refusals=0" in out
    assert "diagnose_split split=sealed quota=12 pinned_families=0" in out and "quota_negative=no" not in out
    assert_allowlisted(out)


def test_register_short_prints_the_lay_drafts_it_has_and_needs(world):
    _terse_pool(world)  # 13 lay-or-terse new drafts, all terse: 7 lay ones are needed
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_SPLIT_INFEASIBLE and err == ""
    assert out.splitlines()[0] == "diagnose round=1 outcome=register_short" and "diagnose_constraint" not in out
    assert "diagnose_register lay_or_terse=13 lay=0 lay_needed=7 short=yes" in out
    assert world.ledger().records("split_attempt") == []


def test_a_feasible_attempt_prints_what_a_real_split_would_release_and_the_real_split_releases_exactly_that(world):
    standard_pool(world)
    before, records = snapshot(world), world.ledger().records()
    code, out, err = world.run(*DIAGNOSE)
    assert code == st.EXIT_OK == 0 and err == ""
    assert out.splitlines()[:2] == [
        "diagnose round=1 outcome=feasible",
        "diagnose_release round=1 sealed=12 tuning=8 realistic=4 new_sealed=12 new_tuning=5 new_realistic=3 groups=3 dropped=0 surplus=0",
    ]
    assert "diagnose_constraint" not in out and "sha256" not in out
    assert "diagnose_pool round=1 records=24 pinned=4 groups=3 surplus=0 tuning_floor=8 attempts=0 attempts_cap=3" in out
    assert "diagnose_drafts class=total families=20 answer=14 partial=0 refuse=6" in out
    assert "diagnose_drafts class=sealed_eligible families=12 answer=9 partial=0 refuse=3" in out
    assert "diagnose_drafts class=sealed_ineligible families=8 answer=5 partial=0 refuse=3" in out
    assert "diagnose_drafts class=dropped families=0 answer=0 partial=0 refuse=0" in out
    assert "diagnose_drafts class=realistic_eligible families=16 answer=13 partial=0 refuse=3" in out
    assert_allowlisted(out)
    # nothing happened: no record, no payload, no file moved (the pool is still in its place), nothing created
    assert snapshot(world) == before and world.ledger().records() == records
    assert (world.sealed / "pool" / "pool.jsonl").is_file() and not (world.sealed / "sealed_r1.jsonl").exists()
    assert world.ledger().records("split_attempt") == [] and world.ledger().records("release") == []
    # the real split now releases exactly what the diagnosis said
    code, real, err = world.run("split", "--round", "1")
    assert code == 0, err
    said, got = lines_of(world, out, "diagnose_release")[0], world.last(real, "split")
    assert got["outcome"] == "released" and got["attempt"] == "1"
    assert {k: said[k] for k in ("sealed", "tuning", "realistic", "groups", "dropped", "surplus")} == {k: got[k] for k in ("sealed", "tuning", "realistic", "groups", "dropped", "surplus")}
    moved = {s: len({r["family_id"] for r in read_rows(p)}) for s, p in (("sealed", world.sealed / "sealed_r1.jsonl"), ("tuning", world.dev / "tuning_r1.jsonl"), ("realistic", world.dev / "realistic_r1.jsonl"))}
    assert moved == {s: int(said[f"new_{s}"]) for s in ("sealed", "tuning", "realistic")}
    assert assign_payload(world)["quotas"] == {s: int(said[s]) for s in ("sealed", "tuning", "realistic")}


def test_round_two_diagnoses_what_round_two_would_release(world):
    _round2_world(world)
    before = snapshot(world)
    code, out, err = world.run("split", "--round", "2", "--diagnose")
    assert code == 0 and err == "" and out.splitlines()[0] == "diagnose round=2 outcome=feasible"
    assert "diagnose_release round=2 sealed=12 tuning=8 realistic=4 new_sealed=1 new_tuning=0 new_realistic=0 groups=0 dropped=1 surplus=2" in out
    assert "diagnose_pool round=2 records=26 pinned=23 groups=0 surplus=2 tuning_floor=8 attempts=0 attempts_cap=3" in out
    assert "diagnose_split split=sealed quota=12 pinned_families=11 pinned_refusals=3" in out
    assert "diagnose_drafts class=dropped families=1 answer=1 partial=0 refuse=0" in out
    assert snapshot(world) == before
    code, real, err = world.run("split", "--round", "2")
    assert code == 0, err
    assert (world.last(real, "split")["dropped"], world.last(real, "split")["surplus"]) == ("1", "2")


def test_round_two_with_too_few_drafts_is_infeasible_and_says_which_constraints(world):
    std = standard_released(world)
    one, two = std.sealed_singletons[:2]
    assert world.run("sheet", "--kind", "sealed", "--name", "r1")[0] == 0
    sheet = world.mark_sheet(world.sheet_path("sealed", "r1"), {one: "R", two: "R"}, default="A")
    assert world.run("apply", "--kind", "sealed", "--sheet", sheet)[0] == 0
    world.draft("answer", register="modelled")  # two slots, one draft
    world.flush_pool("round2.jsonl")
    assert world.run("twins", "--round", "2", "--verdict", world.verdict())[0] == 0
    before = snapshot(world)
    code, out, err = world.run("split", "--round", "2", "--diagnose")
    assert code == st.EXIT_SPLIT_INFEASIBLE and err == "" and out.splitlines()[0] == "diagnose round=2 outcome=infeasible"
    assert [ln for ln in out.splitlines() if "constraint=quota_total" in ln] == ["diagnose_constraint constraint=quota_total families=23 quota_sum=24 violated=yes"]
    assert any("constraint=band" in ln and "violated=yes" in ln for ln in out.splitlines())
    assert snapshot(world) == before and [r["outcome"] for r in world.ledger().records("split_attempt")] == ["feasible"]


# --- it records nothing and does not count toward the cap ---------------------------------------------------------------
def test_diagnoses_never_use_up_an_attempt_a_real_split_still_has_all_its_attempts(world):
    _terse_pool(world)  # register_short: every real attempt is recorded and counts toward the cap of 3
    before, records = snapshot(world), world.ledger().records()
    for _ in range(5):  # more than the cap
        code, out, err = world.run(*DIAGNOSE)
        assert code == 6 and err == "" and "outcome=register_short" in out and "attempts=0 attempts_cap=3" in out
    assert snapshot(world) == before and world.ledger().records() == records
    for attempt in (1, 2, 3):
        code, out, err = world.run("split", "--round", "1")
        assert code == 6 and world.last(out, "split")["attempt"] == str(attempt), out
        if attempt < 3:
            code, out, err = world.run(*DIAGNOSE)
            assert code == 6 and f"attempts={attempt} attempts_cap=3" in out  # it reads the real attempts, it adds none
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == ["register_short"] * 3
    # the cap stops a diagnosis exactly as it stops a split: a refusal, nothing recorded
    after = world.ledger().records()
    assert world.run("split", "--round", "1")[0] == st.EXIT_REFUSED
    assert world.run(*DIAGNOSE) == (st.EXIT_REFUSED, "", "error reason=split_infeasible\n")
    assert world.ledger().records() == after


@pytest.mark.parametrize("scenario", ["twins_missing", "twins_stale", "unpushed", "undeclared", "pool_empty", "already_released", "round1_missing", "bad_round"])
def test_a_diagnosis_stops_where_the_real_split_stops(world, scenario):
    """Same gates, same code: the refusal of ``--diagnose`` equals the refusal of the real attempt (exit code, stdout, stderr)."""
    round_no = "1"
    if scenario == "twins_missing":
        build_standard(world)
        world.write_prereg()
        world.commit_prereg()
    elif scenario == "twins_stale":
        standard_pool(world)
        world.draft()
        world.flush_pool("late.jsonl")  # the pool grew after `twins`
    elif scenario == "unpushed":
        std = build_standard(world)
        _prepare_twins(world, std.verdict_pairs)
        world.write_prereg()
        world.commit_prereg(push=False)
    elif scenario == "undeclared":
        std = build_standard(world)
        world.write_prereg(rounds=[1])
        world.commit_prereg()
        _prepare_twins(world, std.verdict_pairs)
        round_no = "2"
    elif scenario == "pool_empty":
        world.write_prereg()
        world.commit_prereg()
    elif scenario == "already_released":
        standard_released(world)
    elif scenario == "round1_missing":
        world.write_prereg()
        world.commit_prereg()
        round_no = "2"
    else:
        world.write_prereg()
        world.commit_prereg()
        round_no = "3"
    records = world.ledger().records()
    real = world.run("split", "--round", round_no)
    assert real[0] != 0 and real[1] == ""
    after_real = snapshot(world)
    assert world.run("split", "--round", round_no, "--diagnose") == real
    assert snapshot(world) == after_real and world.ledger().records() == records


def test_the_flag_needs_a_round(world):
    assert world.run("split", "--diagnose") == (st.EXIT_USAGE, "", "error reason=usage\n")


# --- the lock and recovery ----------------------------------------------------------------------------------------------------
def test_a_diagnosis_takes_the_state_lock_like_a_split(world):
    standard_pool(world)
    lock_fd = os.open(world.sealed / "work" / st.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    before, records = snapshot(world), world.ledger().records()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert world.run(*DIAGNOSE) == (st.EXIT_REFUSED, "", "error reason=locked\n")  # refused before it reads anything
        assert world.run("split", "--round", "1") == (st.EXIT_REFUSED, "", "error reason=locked\n")
    finally:
        os.close(lock_fd)
    assert snapshot(world) == before and world.ledger().records() == records
    assert world.run(*DIAGNOSE)[0] == 0  # the lock is released when it ends, so a diagnosis and then a split both run
    assert world.run("split", "--round", "1")[0] == 0


def test_the_lock_is_held_for_the_whole_diagnosis(world, monkeypatch):
    """The lock is still held while the attempt is computed, and is gone when the command ends."""
    standard_pool(world)
    held: List[bool] = []
    real = st.attempt_split

    def probe(plan):
        fd = os.open(world.sealed / "work" / st.LOCK_NAME, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        finally:
            os.close(fd)
        return real(plan)

    monkeypatch.setattr(st, "attempt_split", probe)
    assert world.run(*DIAGNOSE)[0] == 0 and held == [True]


@pytest.mark.parametrize("point", ["after_begin", "after_copy"])
def test_a_diagnosis_does_not_recover_a_crashed_release_it_is_refused(world, monkeypatch, point):
    standard_pool(world)

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    assert world.run("split", "--round", "1")[0] == st.EXIT_FAIL  # recorded, half moved
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    before, records = snapshot(world), world.ledger().records()
    assert sp.pending_moves(world.sealed / "work" / "release.journal") != []
    assert world.run(*DIAGNOSE) == (st.EXIT_REFUSED, "", "error reason=recovery_pending\n")
    assert snapshot(world) == before and world.ledger().records() == records  # it moved nothing
    assert sp.pending_moves(world.sealed / "work" / "release.journal") != [] and not (world.sealed / "work" / "release_r1.done").exists()
    code, out, err = world.run("split", "--round", "1")  # a real command recovers, then finds the round released
    assert code == st.EXIT_REFUSED and "reason=already_released" in err
    assert (world.sealed / "work" / "release_r1.done").is_file()
    assert world.run(*DIAGNOSE)[2] == "error reason=already_released\n"  # no recovery pending any more


def test_a_freeze_journal_also_counts_as_a_pending_recovery(world):
    standard_pool(world)
    journal = world.sealed / "work" / "freeze_sealed.journal"
    journal.write_text("{}\n", encoding="utf-8")
    assert world.run(*DIAGNOSE) == (st.EXIT_REFUSED, "", "error reason=recovery_pending\n")
    journal.unlink()
    assert world.run(*DIAGNOSE)[0] == 0


# --- what it prints -----------------------------------------------------------------------------------------------------------
def test_a_diagnosis_prints_no_text_and_no_id(world, monkeypatch):
    seen = _spy_counts(monkeypatch)
    world.prereg_extra = {"strata": {"sealed": {"answer|CANARYSEC": 3, "refuse|refuse": 2}}}
    std = standard_pool(world, sealed_mark="SEALEDXQ", dev_mark="DEVXQ")
    for argv in (DIAGNOSE, ("split", "--round", "1")):  # a diagnosis, then the real attempt, same world: both print counts only
        code, out, err = world.run(*argv)
        assert code == st.EXIT_SPLIT_INFEASIBLE and seen
        text = (out + err).lower()
        assert "sealedxq" not in text and "devxq" not in text and "canarysec" not in text
        assert not any(fid in out + err for fid in world.specs) and not re.search(r"\bq:[0-9a-f]{12}\b|\bf[0-9a-f]{8}\b", out + err)
        assert_allowlisted(out)
    assert std.sealed  # (the marked families exist: the canaries had somewhere to leak from)


def test_every_new_word_is_on_the_allowlist_and_every_value_is_an_integer_or_an_enum_word(world):
    standard_pool(world)
    code, out, err = world.run(*DIAGNOSE)
    for line in out.splitlines():
        for token in line.split(" ")[1:]:
            key, _, value = token.partition("=")
            assert value.isdigit() or value in st.ENUM_WORDS, token
    assert "recovery_pending" in st.REASONS and "recovery_pending" in st.ENUM_WORDS


# --- the real split is unchanged ----------------------------------------------------------------------------------------------
def _scenario_released(w):
    standard_released(w)
    return [h[1:] for h in w.history if h[0][0] == "split"]


def _scenario_quota_short(w):
    _quota_world(w, tuning_refusals=3)
    return [w.run("split", "--round", "1")]


def _scenario_register_short(w):
    _terse_pool(w)
    return [w.run("split", "--round", "1") for _ in range(3)]


def _scenario_infeasible(w):
    modelled_world(w)
    return [w.run("split", "--round", "1")]


def _scenario_round2(w):
    _round2_world(w)
    return [w.run("split", "--round", "2")]


def _scenario_round2_infeasible(w):
    std = standard_released(w)
    one, two = std.sealed_singletons[:2]
    assert w.run("sheet", "--kind", "sealed", "--name", "r1")[0] == 0
    assert w.run("apply", "--kind", "sealed", "--sheet", w.mark_sheet(w.sheet_path("sealed", "r1"), {one: "R", two: "R"}, default="A"))[0] == 0
    w.draft("answer", register="modelled")
    w.flush_pool("round2.jsonl")
    assert w.run("twins", "--round", "2", "--verdict", w.verdict())[0] == 0
    return [w.run("split", "--round", "2")]


def _scenario_strata(w):
    w.prereg_extra = {"strata": {"sealed": {"refuse|refuse": 5, "answer|1": 2}}}
    standard_pool(w)
    return [w.run("split", "--round", "1")]


R1_SHA = "322c92cafac1af14ec0f06aec626c2d1af6b11a67d0bb047445fb249b99c5cbe"
R2_SHA = "f19708298435927eae079bcf808838b1045b8266f63ef8331dcfa5ac9ba2d6eb"
GOLDEN = {
    "released": (
        _scenario_released,
        [(0, f"split round=1 outcome=released attempt=1 sealed=12 tuning=8 realistic=4 groups=3 dropped=0 surplus=0 sha256={R1_SHA}\n", "")],
        [("split_attempt", "feasible"), ("release", None)],
    ),
    "quota_short": (_scenario_quota_short, [(6, "split round=1 outcome=quota_short attempt=1\n", "")], [("split_attempt", "quota_short")]),
    "register_short": (
        _scenario_register_short,
        [(6, f"split round=1 outcome=register_short attempt={n}\n", "") for n in (1, 2, 3)],
        [("split_attempt", "register_short")] * 3,
    ),
    "infeasible": (
        _scenario_infeasible,
        [(6, "split round=1 outcome=infeasible attempt=1\nsplit_constraint constraint=band\nsplit_constraint constraint=eligibility\n", "")],
        [("split_attempt", "infeasible")],
    ),
    "round2": (
        _scenario_round2,
        [(0, f"split round=2 outcome=released attempt=1 sealed=12 tuning=8 realistic=4 groups=0 dropped=1 surplus=2 sha256={R2_SHA}\n", "")],
        [("split_attempt", "feasible"), ("release", None)] * 2,
    ),
    "round2_infeasible": (
        _scenario_round2_infeasible,
        [(6, "split round=2 outcome=infeasible attempt=1\nsplit_constraint constraint=band\nsplit_constraint constraint=eligibility\nsplit_constraint constraint=quota_total\n", "")],
        [("split_attempt", "feasible"), ("release", None), ("split_attempt", "infeasible")],
    ),
    "strata": (
        _scenario_strata,
        [(6, "split round=1 outcome=infeasible attempt=1\nsplit_constraint constraint=strata\n", "")],
        [("split_attempt", "infeasible")],
    ),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_the_real_split_prints_and_records_what_it_did_before_diagnose_existed(world, name):
    scenario, expected, ledger_kinds = GOLDEN[name]
    runs = scenario(world)
    assert [tuple(r) for r in runs] == expected
    assert kinds(world, "split_attempt", "release") == ledger_kinds
    if name == "released":
        release = world.ledger().records("release")[0]
        assert release["payload_sha256"] == R1_SHA
        assert release["assignment_digest"] == "4bf0f1165971afaa180a5b7cd4b2db5206ba8782fe1904c50042603987fb3049"
        assert release["totals"] == {"dropped": 0, "families": 24, "realistic": 3, "sealed": 12, "surplus": 0, "tuning": 5}
    if name == "round2":
        release = world.ledger().records("release")[1]
        assert release["assignment_digest"] == "ccc22dabecd218205c75c4718044c7ec4b0ce892362905e6e99ec39e8ccc3831"
        assert release["totals"] == {"dropped": 1, "families": 24, "realistic": 0, "sealed": 1, "surplus": 2, "tuning": 0}
