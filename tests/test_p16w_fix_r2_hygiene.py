"""Phase 16A-2a phase-gate round 2, fixes 2, 4 and 5: the results-path guard, stale staging, atomic-write leftovers.

Fix 2 (pressure-tester). ``_targets_v6_report`` (and the ``eval/results.md`` guard, for symmetry) must refuse a
case-variant spelling (``eval/RESULTS_V6.md``, ``EVAL/results_v6.md``) even while the file does not exist yet. The
earlier compare used ``samefile`` (needs both files) and fell back to a case-sensitive realpath compare, so the first
slot's report could be pre-empted through a differently-cased path on the default case-insensitive APFS.

Fix 4 (code review, MINOR). ``sealed_tool audit`` sweeps dead SP staging directories (``sp_launch.sweep_stale``,
under the launcher's own lock) before it scans, so the inputs a dead dispatch copied in are not reported as an
exposure; live staging still refuses.

Fix 5 (code review, MINOR). ``recover_state`` deletes the ``.<name>.<6 hex>.tmp`` files a crashed ``write_atomic``
left under ``sealed/`` and ``dev/`` (and only those), so the scanner's pre-check does not exit 7 after a crash.
"""

import importlib
import io
import os
import re

import pytest

import src.evaluator as ev
from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src import sealed_run
from tests.p16_capture import PROVENANCE
from tests.p16w_sealed_world import repos, world  # noqa: F401  (fixtures)
from tests.test_p16w_sealed_tool_audit import audit_world, lines, plant, run_audit  # noqa: F401  (fixtures + helpers)

SAMPLE = "eval/sample_golden_set.jsonl"


# --- fix 2: a case-variant of a committed report's name --------------------------------------------------------------


V6_VARIANTS = (
    "eval/RESULTS_V6.md", "EVAL/results_v6.md", "EVAL/RESULTS_V6.MD", "./Eval/Results_V6.md", "eval/../EVAL/Results_v6.md",
)
RESULTS_VARIANTS = ("eval/RESULTS.md", "EVAL/results.md", "EVAL/RESULTS.MD", "./Eval/Results.md")


def test_a_case_variant_of_the_canonical_sealed_report_is_refused_while_the_report_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval").mkdir()  # the directory exists (the private root's parent), the report does not
    report = sealed_run.results_v6_path()
    assert report == tmp_path / "eval" / "results_v6.md" and not report.exists()
    spellings = [*V6_VARIANTS, str(tmp_path / "eval" / "RESULTS_V6.md"), str(tmp_path / "EVAL" / "results_v6.md")]
    for spelling in spellings:
        assert ev._targets_v6_report(spelling), spelling
        for canonical in (False, True):  # a run that is otherwise canonical cannot take it either
            with pytest.raises(ValueError, match="canonical sealed report"):
                ev._resolve_results_path(spelling, canonical, [])
    # the guard names the one report and its spellings, not its neighbours
    for other in ("eval/results_v6_other.md", "eval/results_v6.md.bak", "eval/results_v7.md", "eval/results.md", "eval/v6.md"):
        assert not ev._targets_v6_report(other), other
    assert not ev._targets_v6_report(None)


def test_run_eval_matrix_refuses_a_case_variant_of_the_sealed_report_before_any_work(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval").mkdir()
    for spelling in (*V6_VARIANTS, str(tmp_path / "eval" / "RESULTS_V6.md")):
        for privacy in ("public", "private"):
            with pytest.raises(ValueError, match="canonical sealed report"):
                ev.run_eval_matrix(
                    [("golden", SAMPLE)], skip_refusals=True, skip_completeness=True, results_path=spelling,
                    privacy=privacy, retrieve_fn_factory=lambda mode: (lambda question, top_k=6: []),
                    provenance_fn=lambda: dict(PROVENANCE),
                )
    assert not (tmp_path / "eval" / "results_v6.md").exists() and not (tmp_path / "eval" / "RESULTS_V6.md").exists()


def test_a_case_variant_of_results_md_is_refused_on_a_non_canonical_run_while_it_does_not_exist(monkeypatch, tmp_path):
    """The symmetric case: the committed ``eval/results.md`` before it exists (a fresh clone)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval").mkdir()
    assert not (tmp_path / "eval" / "results.md").exists()
    for spelling in (*RESULTS_VARIANTS, str(tmp_path / "eval" / "RESULTS.md")):
        with pytest.raises(ValueError, match="not canonical"):
            ev._resolve_results_path(spelling, False, [])
        assert ev._resolve_results_path(spelling, True, [])[0] == spelling  # a canonical run may write its own report
        with pytest.raises(ValueError, match="run_eval refuses"):
            ev.run_eval(SAMPLE, results_path=spelling, privacy="public", retrieve_fn=lambda *a, **k: [])
    for other in ("eval/results_partial.md", "eval/results.md.bak", "eval/results_v5.md"):
        assert ev._resolve_results_path(other, False, [])[0] == other


# --- fix 4: stale SP staging is swept before the audit scans -------------------------------------------------------------

STALE = "abcd1234"
EVERYTHING_BUT_STAGING = ("transcript", "memory", "log", "research", "work", "run", "dispatch")


def test_audit_sweeps_a_dead_staging_directory_so_its_copied_inputs_are_not_an_exposure(world, audit_world):
    aw = audit_world
    plant(aw, world, skip=EVERYTHING_BUT_STAGING)  # the sealed text sits only in a dead dispatch's copied inputs
    stale = aw.staging / STALE
    copied = stale / "inputs" / "pool.jsonl"
    copied.parent.mkdir(parents=True)
    copied.write_text(aw.q["staging"] + "\n", encoding="utf-8")
    assert copied.is_file() and stale.is_dir()
    code, out, err = run_audit(world, aw)
    assert code == 0, (out, err)  # no exposure reported
    summary = lines(out, "audit")[0]
    assert summary["status"] == "clean" and summary["hits"] == "0" and "audit_hit" not in out
    staging = {ln["target"]: ln for ln in lines(out, "audit_target")}["staging"]
    assert staging["hits"] == "0"
    assert not stale.exists()  # swept
    assert (aw.staging / sp.LOCK_NAME).exists()  # the launcher's own bookkeeping stays
    assert err == ""


def test_the_sweep_replays_a_pending_move_before_it_deletes_anything(world, audit_world, tmp_path, monkeypatch):
    """``sweep_stale`` is the launcher's own logic: a half-done move is finished (the destination complete) first."""
    aw = audit_world
    aw.staging.mkdir()
    src = aw.staging / STALE / "out" / "pool.jsonl"
    src.parent.mkdir(parents=True)
    src.write_text("synthetic released output\n", encoding="utf-8")
    dst = tmp_path / "released" / "pool.jsonl"

    def boom(name: str) -> None:
        if name == "after_copy":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(sp, "_crash_point", boom)
    with pytest.raises(RuntimeError):
        sp.journaled_move([(src, dst)], aw.staging / sp.JOURNAL_NAME)
    monkeypatch.setattr(sp, "_crash_point", lambda name: None)
    assert sp.pending_moves(aw.staging / sp.JOURNAL_NAME) and src.exists()
    code, out, err = run_audit(world, aw)
    assert code == 0, err
    assert dst.read_text() == "synthetic released output\n" and not (aw.staging / STALE).exists()
    assert not sp.pending_moves(aw.staging / sp.JOURNAL_NAME)


def test_live_staging_still_refuses_and_nothing_is_swept(world, audit_world):
    aw = audit_world
    plant(aw, world, skip=EVERYTHING_BUT_STAGING)
    kept = aw.staging / STALE / "stdout.log"
    assert kept.is_file()
    with sp._exclusive_lock(aw.staging):  # a dispatch or sweep holds the launcher's lock
        code, out, err = run_audit(world, aw)
    assert code == st.EXIT_AUDIT_REFUSED == 7 and "reason=staging_live" in err and out == ""
    assert kept.is_file()  # a live dispatch's files are never touched
    code, out, err = run_audit(world, aw)  # once it is dead, the same staging is swept and the audit is clean
    assert code == 0 and not kept.exists(), err


def test_an_absent_staging_root_is_not_created_by_the_audit(world, audit_world):
    aw = audit_world
    assert not aw.staging.exists()
    code, out, err = run_audit(world, aw)
    assert code == 0 and not aw.staging.exists()


# --- fix 5: recover_state removes write_atomic's own leftovers -------------------------------------------------------------


def ctx_of(w):
    return st.default_ctx(st.Printer(io.StringIO(), io.StringIO()), repo=w.repo, prereg_path=w.prereg_path)


def crashed_write(path, monkeypatch):
    """Leave behind exactly what a crash between ``write_atomic``'s write and its rename leaves; return the leftover."""
    before = set(path.parent.iterdir()) if path.parent.exists() else set()
    with monkeypatch.context() as m:
        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash before the rename")

        m.setattr(st.os, "replace", boom)
        with pytest.raises(RuntimeError):
            st.write_atomic(path, b"half a release\n")
    (leftover,) = set(path.parent.iterdir()) - before
    assert not path.exists() and re.fullmatch(r"\..+\.[0-9a-f]{6}\.tmp", leftover.name)  # the writer's format, restated
    return leftover


def test_recover_state_deletes_the_leftovers_of_a_crashed_write_atomic_under_sealed_and_dev(world, monkeypatch):
    precheck = importlib.import_module("scripts.scan_leaks").precheck
    leftovers = [
        crashed_write(world.sealed / "sealed_r1.jsonl", monkeypatch),
        crashed_write(world.sealed / "work" / "stage_r1" / "sealed_r1.jsonl", monkeypatch),
        crashed_write(world.sealed / "pool" / "pool.jsonl", monkeypatch),
        crashed_write(world.dev / "realistic_r1.jsonl", monkeypatch),
        crashed_write(world.dev / "nested" / "deeper" / "dev_r2.jsonl", monkeypatch),
    ]
    # the scanner's pre-check sees the ones outside work/ as offenders (exit 7): the reason for the fix
    offenders = set(precheck(set()))
    assert str(leftovers[0]) in offenders and str(leftovers[3]) in offenders
    assert all(p.is_file() for p in leftovers)

    assert st.remove_atomic_leftovers(ctx_of(world)) == len(leftovers)
    assert not any(p.exists() for p in leftovers)
    assert not set(map(str, leftovers)) & set(precheck(set()))
    assert st.remove_atomic_leftovers(ctx_of(world)) == 0  # idempotent


def test_every_state_changing_command_starts_by_recovering_so_a_crash_leftover_is_gone_after_it(world, monkeypatch):
    leftover = crashed_write(world.dev / "realistic_r1.jsonl", monkeypatch)
    sealed_left = crashed_write(world.sealed / "sealed_r1.jsonl", monkeypatch)
    code, out, err = world.run("intake", "--dispatch", "1b2c3d4e")  # refuses (no such dispatch), after recovering
    assert code != 0
    assert not leftover.exists() and not sealed_left.exists()


def test_recover_state_runs_the_cleanup_itself(world, monkeypatch):
    leftover = crashed_write(world.sealed / "sealed_r1.jsonl", monkeypatch)
    st.recover_state(ctx_of(world))
    assert not leftover.exists()


def test_only_write_atomics_exact_naming_pattern_is_deleted(world, tmp_path, monkeypatch):
    ctx = ctx_of(world)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("not ours\n")
    (outside / ".y.abcdef.tmp").write_text("in a directory reached through a symlink\n")
    ledger_dir = world.sealed / "ledger"
    ledger_dir.mkdir(exist_ok=True)
    (world.root / "work").mkdir(exist_ok=True)
    (world.root / "runs").mkdir(exist_ok=True)
    keepers = [
        world.sealed / ".notes.tmp",  # no random suffix
        world.sealed / ".x.ABCDEF.tmp",  # hex is lower case (token_hex)
        world.sealed / ".x.abc12.tmp",  # five digits
        world.sealed / ".x.abcdeff.tmp",  # seven digits
        world.sealed / ".x.abcdef.tmp.bak",  # not at the end
        world.sealed / "x.abcdef.tmp",  # no leading dot
        world.sealed / ".abcdef.tmp",  # no file name between the dot and the suffix
        world.sealed / ".x.abcdeg.tmp",  # not hex
        world.sealed / ".x.json.tmp",  # the ledger's own temp-name style
        ledger_dir / ".x.abcdef.tmp",  # the ledger's directory is not ours
        world.root / "work" / ".x.abcdef.tmp",  # not under sealed/ or dev/
        world.root / "runs" / ".x.abcdef.tmp",
        world.dev / ".kept.abcdef.tmp.keep",
    ]
    for path in keepers:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep\n")
    link = world.sealed / ".link.abcdef.tmp"
    link.symlink_to(target)  # a symlink named like a leftover: neither it nor its target is deleted
    (world.dev / "linked").symlink_to(outside, target_is_directory=True)  # a directory link is not entered
    dir_named_like_one = world.dev / ".d.abcdef.tmp"
    dir_named_like_one.mkdir()
    (dir_named_like_one / "keep.txt").write_text("keep\n")
    genuine = [
        world.sealed / ".r.0a1b2c.tmp",
        world.sealed / "work" / ".s.ffffff.tmp",
        world.dev / "sub" / ".t.000000.tmp",
        world.dev / ".name.with.dots.abcdef.tmp",
    ]
    for path in genuine:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a leftover\n")

    assert st.remove_atomic_leftovers(ctx) == len(genuine)
    assert not any(p.exists() for p in genuine)
    assert all(p.is_file() and p.read_text() == "keep\n" for p in keepers)
    assert link.is_symlink() and target.read_text() == "not ours\n"
    assert (outside / ".y.abcdef.tmp").is_file()
    assert dir_named_like_one.is_dir() and (dir_named_like_one / "keep.txt").is_file()


def test_the_pattern_matches_what_the_writer_names_its_temp_files(tmp_path):
    """Not a copy of the format: ``write_atomic`` itself is asked what it names a file mid-write."""
    names = set()
    for i in range(12):
        target = tmp_path / f"out{i}.jsonl"
        seen = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            seen.append(os.path.basename(path))
            return real_open(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as m:
            m.setattr(st.os, "open", spy)
            st.write_atomic(target, b"x\n")
        (name,) = [n for n in seen if n.endswith(".tmp")]
        assert st._ATOMIC_TMP.fullmatch(name) and name.startswith(f".{target.name}."), name
        names.add(name)
        assert target.read_bytes() == b"x\n" and not list(tmp_path.glob(".*.tmp"))  # the rename consumed it
    assert len(names) == 12  # random per write
