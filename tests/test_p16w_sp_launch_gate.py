"""Phase 16A-2a phase-gate fixes for scripts/sp_launch.py (items D1-D4).

* D1: move recovery re-fsyncs the destination's parent before deleting the source, and new destination
  ancestors are made durable (a failed fsync is injected).
* D2: journal writes loop until complete (a short ``os.write`` is injected) before any move starts.
* D3: ``out/`` is an allowlist; ``pool.jsonl`` goes through ``load_sealed``, ``twins.jsonl`` and
  ``tutorial_map.json`` are checked by shape; a bad run is ``invalid`` and only its logs move.
* D4: ``--timeout`` kills the SP's process group; a timed-out run fails and its staging is cleaned up.

The real ``claude`` is never run (the fake from ``test_p16w_sp_launch``). Every text below is invented.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from scripts import sp_launch as sp
from src import eval_schema
from tests.test_p16w_schema import CANARY as ROW_CANARY
from tests.test_p16w_schema import INVENTORY, _partial, _refuse, _row
from tests.test_p16w_sp_launch import CANARY, SimulatedCrash, crash_at, env, make_tree, snapshot, twin_line  # noqa: F401  (env: fixture)

FAM_A, FAM_B, FAM_C = "f0a1b2c3d", "f1a1b2c3d", "f2a1b2c3d"


# ---------------------------------------------------------------------------
# D1: recovery makes the destination durable before it deletes the source
# ---------------------------------------------------------------------------
def test_recovery_refsyncs_the_destination_parent_before_deleting_the_source(tmp_path, monkeypatch):
    """Codex 5: the rename succeeded but its directory fsync failed. Recovery used to skip the fsync and delete the
    source, so a crash could lose both. Now it fsyncs again, and a failure keeps the source."""
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    dst, journal = tmp_path / "dst" / "tree", tmp_path / "moves.journal"
    real_fsync = sp._fsync_dir
    broken = {"on": True}

    def flaky(path):
        if broken["on"] and Path(path) == dst.parent and os.path.lexists(dst):  # the fsync that follows the rename
            raise OSError(errno.EIO, "injected fsync failure")
        real_fsync(path)

    monkeypatch.setattr(sp, "_fsync_dir", flaky)
    with pytest.raises(OSError):
        sp.journaled_move([(src, dst)], journal)
    assert src.exists() and dst.exists() and len(sp.pending_moves(journal)) == 1  # renamed, fsync failed

    with pytest.raises(OSError):  # recovery meets the same failing fsync: it must not go on to delete the source
        sp.recover_moves(journal)
    assert snapshot(src) == want and len(sp.pending_moves(journal)) == 1

    broken["on"] = False  # the disk recovers
    assert sp.recover_moves(journal) == 1
    assert not src.exists() and snapshot(dst) == want and sp.pending_moves(journal) == []


def _record_fsyncs_and_removals(monkeypatch) -> list:
    events: list = []
    real_fsync, real_remove = sp._fsync_dir, sp._remove_path

    def fsync(path):
        events.append(("fsync", Path(path)))
        real_fsync(path)

    def remove(path):
        events.append(("remove", Path(path)))
        real_remove(path)

    monkeypatch.setattr(sp, "_fsync_dir", fsync)
    monkeypatch.setattr(sp, "_remove_path", remove)
    return events


def _fsynced_before_removal(events: list, src: Path) -> set:
    return {path for kind, path in events[: events.index(("remove", src))] if kind == "fsync"}


def test_new_destination_ancestors_are_made_durable_before_the_source_is_deleted(tmp_path, monkeypatch):
    """Codex 5: a directory created for the destination is only reachable once its parent's entry is fsynced."""
    src = make_tree(tmp_path / "src")
    dst = tmp_path / "d1" / "d2" / "d3" / "tree"
    events = _record_fsyncs_and_removals(monkeypatch)
    sp.journaled_move([(src, dst)], tmp_path / "moves.journal")
    durable = _fsynced_before_removal(events, src)
    # d1, d2 and d3 were created: the entry of each is in its parent (tmp_path, d1, d2); the tree's entry is in d3.
    assert {tmp_path, tmp_path / "d1", tmp_path / "d1" / "d2", dst.parent} <= durable


def test_recovery_makes_missing_ancestors_durable_too(tmp_path, monkeypatch):
    """A journal replayed after the directories vanished (or from before they were fsynced) recreates and fsyncs them."""
    src = make_tree(tmp_path / "src")
    dst = tmp_path / "d1" / "d2" / "tree"
    journal = tmp_path / "moves.journal"
    crash_at(monkeypatch, "after_begin")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, dst)], journal)
    crash_at(monkeypatch, None)
    shutil.rmtree(tmp_path / "d1", ignore_errors=True)  # nothing of the destination ancestry survived
    events = _record_fsyncs_and_removals(monkeypatch)
    assert sp.recover_moves(journal) == 1
    assert {tmp_path, tmp_path / "d1", dst.parent} <= _fsynced_before_removal(events, src)
    assert (dst / "y.bin").exists() and not src.exists()


# ---------------------------------------------------------------------------
# D2: the journal writer loops until the record is complete
# ---------------------------------------------------------------------------
def _limit_journal_writes(monkeypatch, journal: Path, chunk: int) -> list:
    """Make every ``os.write`` to the journal accept at most ``chunk`` bytes (0: none); return the sizes taken."""
    real_write = os.write
    taken: list = []

    def limited(fd, data):
        try:
            is_journal = os.path.samestat(os.fstat(fd), os.stat(journal))
        except OSError:
            is_journal = False
        if not is_journal:
            return real_write(fd, data)
        taken.append(chunk)
        return real_write(fd, memoryview(data)[:chunk]) if chunk else 0

    monkeypatch.setattr(os, "write", limited)
    return taken


def test_a_short_journal_write_still_leaves_a_recoverable_intent_before_any_move(tmp_path, monkeypatch):
    """Codex 6: ``os.write`` may write less. The begin record used to be left unterminated (and ignored as a torn
    tail) while the move went on; now it is written in full before the first copy."""
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    dst, journal = tmp_path / "dst" / "tree", tmp_path / "moves.journal"
    real_write = os.write
    taken = _limit_journal_writes(monkeypatch, journal, chunk=7)
    crash_at(monkeypatch, "after_begin")  # the process dies right after the begin record, before any copy
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, dst)], journal)
    assert len(taken) > 3  # the record really went out in pieces
    [pending] = sp.pending_moves(journal)  # complete, parseable, unclosed
    assert [item["src"] for item in pending["items"]] == [str(src)]
    assert snapshot(src) == want and not dst.exists()  # nothing had moved yet

    crash_at(monkeypatch, None)  # a restart: no crash hook, no short writes
    monkeypatch.setattr(os, "write", real_write)
    assert sp.recover_moves(journal) == 1
    assert not src.exists() and snapshot(dst) == want


def test_a_whole_short_write_move_completes_and_closes_its_journal(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    dst, journal = tmp_path / "dst", tmp_path / "moves.journal"
    _limit_journal_writes(monkeypatch, journal, chunk=5)
    sp.journaled_move([(src, dst)], journal)
    assert not src.exists() and snapshot(dst) == want
    assert [r["t"] for r in sp._read_journal(journal)] == ["begin", "done"] and sp.pending_moves(journal) == []


def test_a_journal_write_that_makes_no_progress_stops_before_anything_moves(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    dst, journal = tmp_path / "dst", tmp_path / "moves.journal"
    _limit_journal_writes(monkeypatch, journal, chunk=0)
    with pytest.raises(sp.MoveError):
        sp.journaled_move([(src, dst)], journal)
    assert snapshot(src) == want and not dst.exists()
    assert sp.pending_moves(journal) == []  # no (partial) begin record is left to be acted on


# ---------------------------------------------------------------------------
# D3: out/ is an allowlist
# ---------------------------------------------------------------------------
@pytest.fixture
def inventory(tmp_path, monkeypatch):
    """The committed-inventory path points at a tiny invented inventory (what ``load_sealed`` reads by default)."""
    path = tmp_path / "_registry" / "section_inventory.json"
    path.parent.mkdir()
    path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", path)
    return path


def pool_rows(**first: object) -> list:
    """A valid sealed pool: three families, every row marked ``"sealed": true``; ``first`` overrides row 0's keys."""
    rows = [
        _row(0, fam=FAM_A, sealed=True), _row(1, fam=FAM_A, sealed=True),
        _partial(0, fam=FAM_B, sealed=True), _refuse(0, fam=FAM_C, sealed=True),
    ]
    rows[0] = {**rows[0], **first}
    return rows


def pool_text(**first: object) -> str:
    return "".join(json.dumps(row) + "\n" for row in pool_rows(**first))


TUTORIAL_MAP = json.dumps({FAM_A: "T02", FAM_B: "TD7", FAM_C: "T17"})
GOOD_OUT = {"pool.jsonl": pool_text(), "twins.jsonl": twin_line(FAM_A, FAM_B), "tutorial_map.json": TUTORIAL_MAP}


def write_out(directory: Path, files: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return directory


def test_a_good_out_directory_passes_and_each_file_is_optional(tmp_path, inventory):
    assert sp.validate_out(write_out(tmp_path / "all", {**GOOD_OUT, "return.json": '{"count":1}'}))
    for name in (*GOOD_OUT, "return.json"):
        assert sp.validate_out(write_out(tmp_path / f"only_{name}", {name: GOOD_OUT.get(name, '{"count":1}')}))
    assert sp.validate_out(write_out(tmp_path / "empty", {}))
    assert sp.validate_out(write_out(tmp_path / "no_pairs", {"twins.jsonl": ""}))  # zero twin pairs is a valid answer


@pytest.mark.parametrize("stray", [
    {"notes.txt": "x"}, {"extra.json": "{}"}, {"pool.jsonl.bak": "x"}, {"pool/draft.txt": "x"},
    {".hidden": "x"}, {"tutorial_map.json.tmp": "x"},
])
def test_any_other_entry_in_out_makes_it_invalid(tmp_path, inventory, stray):
    out = write_out(tmp_path / "out", {**GOOD_OUT, "return.json": '{"count":1}', **stray})
    assert not sp.validate_out(out)


def test_allowlisted_names_are_matched_exactly(tmp_path, inventory):
    """On a case-insensitive disk ``Twins.jsonl`` is the file ``twins.jsonl`` would be; the name as listed must match."""
    assert not sp.validate_out(write_out(tmp_path / "out", {"Twins.jsonl": ""}))


def test_symlinks_and_non_directories_are_invalid(tmp_path, inventory):
    out = write_out(tmp_path / "out", {"return.json": '{"count":1}'})
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(pool_text(), encoding="utf-8")
    (out / "pool.jsonl").symlink_to(target)
    assert not sp.validate_out(out)
    (out / "pool.jsonl").unlink()
    (out / "twins.jsonl").mkdir()  # an allowlisted name that is a directory
    assert not sp.validate_out(out)
    assert not sp.validate_out(target)  # not a directory
    assert not sp.validate_out(tmp_path / "missing")
    good = write_out(tmp_path / "good", {"return.json": '{"count":1}'})
    link_dir = tmp_path / "link_out"
    link_dir.symlink_to(good)
    assert sp.validate_out(good) and not sp.validate_out(link_dir)


UNMARKED = "".join(json.dumps(_row(n, fam=FAM_A)) + "\n" for n in (0, 1))  # valid rows, nothing marks the file sealed


@pytest.mark.parametrize("text", [
    UNMARKED,
    pool_text(sealed=False),                                         # the marker must be true
    pool_text(evidence=[["999.9"]]),                                 # a label not in the inventory
    pool_text(scope="surprise"),                                     # not a schema scope
    "this is not json\n",
    json.dumps({"schema": 1, "question": "q"}) + "\n",               # not schema 2
    "",                                                              # an empty file is not a sealed set
    pool_text() + '{"truncated": \n',
])
def test_pool_jsonl_must_load_through_the_sealed_loader(tmp_path, inventory, text):
    assert not sp.validate_out(write_out(tmp_path / "out", {"pool.jsonl": text}))


def test_pool_validation_uses_load_sealed(tmp_path, inventory, monkeypatch):
    seen = []

    def fake_load_sealed(path, **kw):
        seen.append(Path(path).name)
        return []

    monkeypatch.setattr(sp, "load_sealed", fake_load_sealed)
    assert sp.validate_out(write_out(tmp_path / "out", {"pool.jsonl": "anything\n"}))
    assert seen == ["pool.jsonl"]


@pytest.mark.parametrize("text", [
    "not json\n",
    json.dumps({"a": FAM_A, "b": FAM_B}) + "\n",                                      # no verdict
    json.dumps({"a": FAM_A, "b": FAM_B, "verdict": "different"}) + "\n",
    json.dumps({"a": FAM_A, "b": FAM_A, "verdict": "twin"}) + "\n",                   # a pair needs two families
    json.dumps({"a": FAM_A, "b": FAM_B, "verdict": "twin", "why": "same facts"}) + "\n",  # free text rides in an extra key
    json.dumps({"a": "two words", "b": FAM_B, "verdict": "twin"}) + "\n",
    json.dumps({"a": "x" * 65, "b": FAM_B, "verdict": "twin"}) + "\n",
    json.dumps({"a": 1, "b": FAM_B, "verdict": "twin"}) + "\n",
    '{"a": "x1", "a": "x2", "b": "y1", "verdict": "twin"}\n',                           # duplicate key
    "[]\n",
    twin_line(FAM_A, FAM_B) + "oops\n",
])
def test_twins_jsonl_is_checked_by_shape(tmp_path, inventory, text):
    assert not sp.validate_out(write_out(tmp_path / "out", {"twins.jsonl": text}))


def test_twins_jsonl_accepts_public_style_ids_and_blank_lines(tmp_path, inventory):
    text = twin_line(FAM_A, "q:0a1b2c3d4e5f") + "\n" + twin_line("q:abc", "q:def")
    assert sp.validate_out(write_out(tmp_path / "out", {"twins.jsonl": text}))


@pytest.mark.parametrize("text", [
    "[]", "{}", "not json", "3", '"T02"',
    json.dumps({FAM_A: "T01"}), json.dumps({FAM_A: "T07"}), json.dumps({FAM_A: "t02"}), json.dumps({FAM_A: "T18"}),
    json.dumps({FAM_A: "TD8"}), json.dumps({FAM_A: "T02 the client"}), json.dumps({FAM_A: 2}),
    json.dumps({"not-a-family": "T02"}), json.dumps({"F0a1b2c3d": "T02"}), json.dumps({f"{FAM_A}-1": "T02"}),
    '{"f0a1b2c3d": "T02", "f0a1b2c3d": "T03"}',
])
def test_tutorial_map_is_checked_by_shape(tmp_path, inventory, text):
    assert not sp.validate_out(write_out(tmp_path / "out", {"tutorial_map.json": text}))


def bad_run_checks(env, outcome, dispatch_id) -> None:
    """A run with a bad ``out/``: invalid, nothing released to sp_out, the logs kept, staging gone."""
    assert outcome.exit_status == sp.EXIT_FAILED and outcome.line == sp.INVALID and outcome.message is None
    assert not (env.dest / dispatch_id).exists()
    assert not env.dest.exists() or list(env.dest.iterdir()) == []
    assert (env.work / f"{dispatch_id}.stdout.log").is_file() and (env.work / f"{dispatch_id}.stderr.log").is_file()
    assert env.staging_dirs() == []


def dispatch_id_of(env) -> str:
    return env.log("sp_probe.jsonl")[-1]["dispatch_id"]


def test_a_good_run_releases_all_four_files(env, inventory):
    env.make_claude(files=GOOD_OUT, ret={"count": 1})
    outcome = env.launch(enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK and outcome.line == '{"count":1}'
    released = env.dest / dispatch_id_of(env)
    assert sorted(p.name for p in released.iterdir()) == ["pool.jsonl", "return.json", "tutorial_map.json", "twins.jsonl"]
    assert (released / "pool.jsonl").read_text(encoding="utf-8") == GOOD_OUT["pool.jsonl"]


@pytest.mark.parametrize("files", [
    {"notes.txt": "x"},
    {"pool/draft.txt": "x"},
    {"twins.jsonl": "", "scratch.txt": "ZQXV left behind"},
])
def test_a_run_with_a_stray_out_file_is_invalid_and_releases_only_its_logs(env, inventory, capfd, files):
    env.make_claude(canary=CANARY, files=files, ret={"count": 1})
    rc = env.main("--enum", "count")
    captured = capfd.readouterr()
    assert rc == sp.EXIT_FAILED and captured.out == "invalid\n" and captured.err == ""
    assert CANARY not in captured.out + captured.err
    bad_run_checks(env, sp.Outcome(rc, captured.out.strip(), None), dispatch_id_of(env))
    assert len(env.records()) == 1  # the SP did run


@pytest.mark.parametrize("name,text", [
    ("pool.jsonl", pool_text(sealed=False)),
    ("pool.jsonl", UNMARKED),
    ("pool.jsonl", pool_text(evidence=[["999.9"]])),
    ("twins.jsonl", json.dumps({"a": FAM_A, "b": FAM_B, "verdict": "twin", "why": "text"}) + "\n"),
    ("tutorial_map.json", json.dumps({FAM_A: "T07"})),
])
def test_a_run_with_a_malformed_data_file_is_invalid_and_releases_only_its_logs(env, inventory, capfd, name, text):
    env.make_claude(canary=CANARY, files={name: text}, ret={"count": 1})
    outcome = env.launch(enums=["count"])
    bad_run_checks(env, outcome, dispatch_id_of(env))
    assert ROW_CANARY not in (outcome.line or "") + (outcome.message or "")
    out = capfd.readouterr()
    assert ROW_CANARY not in out.out + out.err


def test_a_symlink_in_out_is_invalid_not_a_failed_move(env, inventory, tmp_path):
    """Before D3 the move itself refused the symlink, which lost the logs and printed nothing."""
    target = tmp_path / "somewhere.txt"
    target.write_text("elsewhere", encoding="utf-8")
    env.make_claude(symlinks={"pool.jsonl": str(target)}, ret={"count": 1})
    outcome = env.launch(enums=["count"])
    bad_run_checks(env, outcome, dispatch_id_of(env))
    assert target.read_text(encoding="utf-8") == "elsewhere"


# ---------------------------------------------------------------------------
# D4: --timeout
# ---------------------------------------------------------------------------
def stub_run_sp(monkeypatch) -> list:
    seen: list = []

    def stub(argv, run_env, task, staging, timeout=None):
        seen.append(timeout)
        return 0

    monkeypatch.setattr(sp, "_run_sp", stub)
    return seen


def test_the_default_timeout_is_three_hours_and_the_option_overrides_it(env, monkeypatch):
    assert sp.DEFAULT_TIMEOUT == 3 * 60 * 60
    seen = stub_run_sp(monkeypatch)
    env.launch()
    assert env.main() == sp.EXIT_FAILED  # the stub writes no return.json; only the timeout argument matters here
    assert env.main("--timeout", "42.5") == sp.EXIT_FAILED
    env.launch(timeout=7)
    assert seen == [sp.DEFAULT_TIMEOUT, sp.DEFAULT_TIMEOUT, 42.5, 7]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc", "", "1e12"])
def test_a_bad_timeout_is_a_usage_error_and_nothing_starts(env, value):
    assert env.main("--timeout", value) == sp.EXIT_USAGE
    assert env.records() == [] and env.staging_dirs() == []


@pytest.mark.parametrize("value", [True, None, "5", 0, -3, float("nan"), float("inf")])
def test_launch_refuses_a_bad_timeout_value(env, value):
    assert env.launch(timeout=value).exit_status == sp.EXIT_USAGE
    assert env.records() == []


def test_sweep_takes_no_timeout(env):
    assert sp.main(["--sweep", "--timeout", "5"]) == sp.EXIT_USAGE


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_sleeping_sp_is_killed_at_the_timeout_and_the_run_fails_cleanly(env, tmp_path, capfd):
    """A fake claude that has already written a valid ``out/`` and a valid return, then hangs (and has a helper
    process in its group). The timeout must kill both, fail the run, release nothing but the logs, clean up."""
    pid_file = tmp_path / "grandchild.pid"
    env.make_claude(
        canary=CANARY, files={"twins.jsonl": twin_line(FAM_A, FAM_B)}, ret={"count": 1}, sleep=60,
        grandchild_pid_file=str(pid_file),
    )
    started = time.monotonic()
    outcome = env.launch(enums=["count"], timeout=3)
    elapsed = time.monotonic() - started
    assert elapsed < 30  # it did not wait out the 60 s sleep
    assert len(env.records()) == 1  # it ran
    assert outcome.exit_status == sp.EXIT_FAILED and outcome.line == sp.INVALID
    assert outcome.message == "sp_launch: the SP ran past --timeout and was killed"
    assert CANARY not in (outcome.line or "") + (outcome.message or "")

    [row] = env.log("sp_probe.jsonl")
    assert row["exit_status"] == sp.STATUS_TIMEOUT
    assert not (env.dest / row["dispatch_id"]).exists()  # a valid out/ and return are still not released
    assert CANARY in (env.work / f"{row['dispatch_id']}.stderr.log").read_text()  # the logs are kept (stderr is line-buffered)
    assert env.staging_dirs() == []  # staging cleaned up

    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(grandchild)  # the whole process group was killed, not just the direct child

    assert sp.main(["--sweep"]) == sp.EXIT_OK  # the staging lock was released
    capfd.readouterr()


def test_a_run_inside_the_timeout_is_unaffected(env, inventory):
    env.make_claude(files=GOOD_OUT, ret={"count": 1})
    outcome = env.launch(enums=["count"], timeout=60)
    assert outcome.exit_status == sp.EXIT_OK and outcome.message is None
    assert env.log("sp_probe.jsonl")[-1]["exit_status"] == 0
