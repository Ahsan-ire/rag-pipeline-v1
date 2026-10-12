"""Tests for src/sealed_ledger.py — the sealed-slot ledger and state machine
(Phase 16A-2a item a4, D72; acceptance (f), the ledger's own parts).

Synthetic data only. Every path is under ``tmp_path``: the autouse fixture
points ``CC_SEALED_LEDGER`` at a per-test file (the module honours it only
under pytest and refuses the real per-user ledger there), so no test can read
or write ``~/.local/state/claudecode/sealed_ledger.jsonl``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
import os
import pwd
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

from src import sealed_ledger
from src.sealed_ledger import (
    EpochStop,
    LedgerCorrupt,
    LedgerRefused,
    MirrorTampered,
    ProtocolError,
    Recovery,
    RecoveryAction,
    SealedLedger,
    SealedLedgerError,
    SlotRefused,
    below_floor,
    check_ledger_path,
    default_ledger_path,
    digest_run_dir,
    resolve_ledger_path,
)

ROOT = Path(__file__).resolve().parent.parent
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
FORBIDDEN_WORDS = ("scope", "chapter", "stratum", "sealed")


@pytest.fixture(autouse=True)
def _tmp_sealed_ledger(monkeypatch, tmp_path):
    """Point the sealed ledger at a per-test file (never the per-user ledger)."""
    monkeypatch.setenv("CC_SEALED_LEDGER", str(tmp_path / "state" / "sealed_ledger.jsonl"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ledger_file() -> Path:
    return Path(os.environ["CC_SEALED_LEDGER"])


def copy_file(tmp_path: Path) -> Path:
    return tmp_path / "eval" / "private" / "sealed" / "ledger" / "sealed_ledger.jsonl"


def mirror_file(tmp_path: Path) -> Path:
    return tmp_path / "eval" / "sealed_ledger.jsonl"


def make(tmp_path: Path, *, mirror: bool = True, clock: Any = None) -> SealedLedger:
    """A fresh ledger view over the same files: every call is a 'restarted process'."""
    return SealedLedger(
        copy_file(tmp_path).parent,
        mirror=mirror_file(tmp_path) if mirror else None,
        clock=clock,
    )


def make_run(tmp_path: Path, name: str, payload: str = "rows") -> Path:
    """A run directory like sealed/runs/<id>/: per-row results, sidecar, artifact."""
    run = tmp_path / "sealed" / "runs" / name
    (run / "nested").mkdir(parents=True, exist_ok=True)
    (run / "rows.jsonl").write_text(payload + "\n")
    (run / "sidecar.json").write_text('{"k": 1}\n')
    (run / "nested" / "artifact.json").write_text("{}\n")
    return run


def lines_of(path: Path) -> List[bytes]:
    return path.read_bytes().splitlines()


def kinds_in(path: Path) -> List[str]:
    return [json.loads(line)["kind"] for line in lines_of(path)]


def advance(led: SealedLedger, tmp_path: Path, slot: int | str, steps: List[str]) -> None:
    """Apply protocol steps in order: claim, store, intent, consumed."""
    for step in steps:
        if step == "claim":
            led.claim(slot)
        elif step == "store":
            led.store_results(slot, make_run(tmp_path, f"run-{slot}"))
        elif step == "intent":
            led.record_intent(slot)
        elif step == "consumed":
            led.record_consumed(slot)
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(step)


class FakeRunner:
    """Counts sealed calls so that recovery can be shown to make none."""

    def __init__(self) -> None:
        self.sealed_calls = 0
        self.rendered: List[str] = []
        self.published: List[str] = []

    def sealed_call(self) -> None:
        self.sealed_calls += 1


def resume(led: SealedLedger, slot: int | str, runner: FakeRunner) -> Recovery:
    """What a caller does after a crash: act on ``recover`` only, never call sealed."""
    rec = led.recover(slot)
    if rec.action is RecoveryAction.RENDER_FROM_STORAGE:
        runner.rendered.append(rec.run_id)
        led.record_consumed(slot)  # consumed only once rendered
    elif rec.action is RecoveryAction.REPUBLISH:
        runner.published.append(rec.run_id)
        led.record_consumed(slot)
    return rec


# ---------------------------------------------------------------------------
# Ledger location (mirrors D70)
# ---------------------------------------------------------------------------


def test_default_path_is_keyed_on_the_passwd_home(monkeypatch):
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/claudecode/sealed_ledger.jsonl"
    assert default_ledger_path() == expected
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert default_ledger_path() == expected  # HOME cannot hide the ledger


def test_override_is_honoured_only_under_pytest(monkeypatch, tmp_path):
    assert resolve_ledger_path() == ledger_file()  # under pytest, the autouse override
    monkeypatch.setattr(sealed_ledger, "_under_pytest", lambda: False)
    with pytest.raises(LedgerRefused):
        resolve_ledger_path()  # the variable is set, but we are "outside pytest"
    with pytest.raises(LedgerRefused):
        check_ledger_path(tmp_path / "x.jsonl")  # explicit path with the variable set
    monkeypatch.delenv("CC_SEALED_LEDGER")
    assert resolve_ledger_path() == default_ledger_path()
    with pytest.raises(LedgerRefused):
        check_ledger_path(tmp_path / "x.jsonl")  # outside pytest only the default is accepted
    assert check_ledger_path(default_ledger_path()) == default_ledger_path()


def test_pytest_refuses_the_default_ledger_and_a_missing_override(monkeypatch, tmp_path):
    with pytest.raises(LedgerRefused):
        SealedLedger(tmp_path / "copy", ledger=default_ledger_path())
    monkeypatch.setenv("CC_SEALED_LEDGER", str(default_ledger_path()))
    with pytest.raises(LedgerRefused):
        SealedLedger(tmp_path / "copy")
    monkeypatch.delenv("CC_SEALED_LEDGER")
    with pytest.raises(LedgerRefused):
        SealedLedger(tmp_path / "copy")


def test_an_exported_pytest_variable_alone_does_not_make_a_process_pytest(monkeypatch):
    assert sealed_ledger._under_pytest() is True
    monkeypatch.delitem(sys.modules, "pytest")  # the variable is set, the module is not loaded
    assert sealed_ledger._under_pytest() is False


def test_three_stores_must_be_different_files(tmp_path):
    with pytest.raises(LedgerRefused):
        SealedLedger(ledger_file().parent)  # the private copy would be the ledger itself
    with pytest.raises(LedgerRefused):
        SealedLedger(tmp_path / "copy", mirror=ledger_file())


def test_the_ledger_module_follows_the_ast_rule_on_sealed_parameters():
    tree = ast.parse((ROOT / "src" / "sealed_ledger.py").read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *filter(None, [a.vararg, a.kwarg])]:
            assert "sealed" not in arg.arg, f"{fn.name}: parameter {arg.arg!r}"


# ---------------------------------------------------------------------------
# Records: split attempts, release, freeze, content-free validation
# ---------------------------------------------------------------------------


def test_every_append_lands_in_all_three_stores_byte_identical(tmp_path):
    led = make(tmp_path, clock=lambda: datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc))
    led.record_split_attempt(round_no=1, pool_digest=DIGEST_A, outcome="infeasible")
    led.record_split_attempt(round_no=1, pool_digest=DIGEST_B, outcome="released")
    led.record_release(round_no=1, assignment_digest=DIGEST_C, totals={"families": 60, "groups": 52})
    assert ledger_file().read_bytes() == copy_file(tmp_path).read_bytes()
    recs = [json.loads(line) for line in lines_of(ledger_file())]
    assert [r["seq"] for r in recs] == [0, 1, 2]
    assert [r["kind"] for r in recs] == ["split_attempt", "split_attempt", "release"]
    assert recs[0]["ts"] == "2026-10-12T09:30:00Z"
    assert recs[0]["pool_digest"] == DIGEST_A and recs[0]["outcome"] == "infeasible"
    assert recs[2]["totals"] == {"families": 60, "groups": 52}
    assert len(lines_of(mirror_file(tmp_path))) == 3
    assert [r["outcome"] for r in led.records("split_attempt")] == ["infeasible", "released"]


def test_the_sealed_freeze_binds_the_three_digests_and_totals_and_is_final(tmp_path):
    led = make(tmp_path)
    assert led.freeze_record("sealed") is None
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C,
        totals={"rows": 120, "families": 60, "groups": 52},
    )
    frozen = led.freeze_record("sealed")
    assert frozen["split"] == "sealed"
    assert frozen["set_sha256"] == DIGEST_A
    assert frozen["twins_sha256"] == DIGEST_B
    assert frozen["exclusions_sha256"] == DIGEST_C
    assert frozen["totals"] == {"rows": 120, "families": 60, "groups": 52}
    with pytest.raises(ProtocolError):
        led.record_freeze(
            split="sealed", set_sha256=DIGEST_B, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={}
        )
    assert led.freeze_record("sealed") == frozen  # unchanged: the frozen file is immutable for the epoch
    assert kinds_in(ledger_file()) == ["freeze"]
    with pytest.raises(ProtocolError):
        make(tmp_path).record_freeze(
            split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={}
        )  # a restarted process is refused too


def test_each_split_freezes_once_and_only_sealed_needs_twins_and_exclusions(tmp_path):
    led = make(tmp_path)
    with pytest.raises(ValueError):  # sealed without the twins record and the exclusion list
        led.record_freeze(split="sealed", set_sha256=DIGEST_A, totals={"families": 60})
    with pytest.raises(ValueError):
        led.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, totals={"families": 60})
    assert led.records() == []  # the refusals wrote nothing
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={"families": 60}
    )
    led.record_freeze(split="tuning", set_sha256=DIGEST_B, totals={"families": 150})  # no twins, no exclusions
    led.record_freeze(  # optional fields are accepted and kept when given
        split="realistic", set_sha256=DIGEST_C, twins_sha256=DIGEST_A, totals={"families": 42}
    )
    tuning = led.freeze_record("tuning")
    assert (tuning["split"], tuning["set_sha256"], tuning["totals"]) == ("tuning", DIGEST_B, {"families": 150})
    assert "twins_sha256" not in tuning and "exclusions_sha256" not in tuning
    assert led.freeze_record("realistic")["twins_sha256"] == DIGEST_A
    assert "exclusions_sha256" not in led.freeze_record("realistic")
    assert led.freeze_record("sealed")["set_sha256"] == DIGEST_A  # each split has its own record
    assert led.freeze_record("heldout") is None
    for split in ("tuning", "realistic"):  # the same split twice is refused
        with pytest.raises(ProtocolError):
            led.record_freeze(split=split, set_sha256=DIGEST_A, totals={})
    assert [r["split"] for r in led.records("freeze")] == ["sealed", "tuning", "realistic"]
    for bad in ("Tuning", "has space", "", "x" * 41, 3):
        with pytest.raises(ValueError):
            led.record_freeze(split=bad, set_sha256=DIGEST_A, totals={})
        with pytest.raises(ValueError):
            led.freeze_record(bad)


def test_the_mirror_names_the_split_as_a_value_and_still_carries_digests_only(tmp_path):
    led = make(tmp_path)
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={"families": 60}
    )
    led.record_freeze(split="tuning", set_sha256=DIGEST_B, totals={"families": 150})
    sealed, tuning = (json.loads(line) for line in lines_of(mirror_file(tmp_path)))
    assert sealed["split"] == "sealed" and tuning["split"] == "tuning"  # a value, not a key
    assert sealed["digests"] == {"set_sha256": DIGEST_A, "twins_sha256": DIGEST_B, "exclusions_sha256": DIGEST_C}
    assert tuning["digests"] == {"set_sha256": DIGEST_B}
    for entry in (sealed, tuning):
        assert set(entry) == {"seq", "kind", "digest", "digests", "split"}
        assert not any(w in key for key in _all_keys(entry) for w in FORBIDDEN_WORDS)
    assert "families" not in mirror_file(tmp_path).read_text()  # no totals in the mirror
    assert make(tmp_path).freeze_record("sealed")["set_sha256"] == DIGEST_A  # and the ledger still verifies


def test_a_second_freeze_of_one_split_in_the_file_fails_closed(tmp_path):
    led = make(tmp_path, mirror=False)
    led.record_freeze(split="tuning", set_sha256=DIGEST_A, totals={})
    line = lines_of(ledger_file())[0]
    dup = json.loads(line)
    dup["seq"] = 1
    both = line + b"\n" + json.dumps(dup, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    for path in (ledger_file(), copy_file(tmp_path)):
        path.write_bytes(both)
    with pytest.raises(LedgerCorrupt):
        led.freeze_record("tuning")


@pytest.mark.parametrize(
    "call",
    [
        lambda led: led.record_split_attempt(round_no=1, pool_digest="not-a-digest", outcome="released"),
        lambda led: led.record_split_attempt(round_no=1, pool_digest=DIGEST_A.upper(), outcome="released"),
        lambda led: led.record_split_attempt(round_no=1, pool_digest=DIGEST_A, outcome="a free text outcome"),
        lambda led: led.record_split_attempt(round_no=0, pool_digest=DIGEST_A, outcome="released"),
        lambda led: led.record_release(round_no=1, assignment_digest="x" * 64),
        lambda led: led.record_release(round_no=1, assignment_digest=DIGEST_A, totals={"Families": 1}),
        lambda led: led.record_release(round_no=1, assignment_digest=DIGEST_A, totals={"families": -1}),
        lambda led: led.record_release(round_no=1, assignment_digest=DIGEST_A, totals={"families": True}),
        lambda led: led.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256="short", exclusions_sha256=DIGEST_C, totals={}),
        lambda led: led.record_freeze(split="tuning", set_sha256=DIGEST_A, exclusions_sha256="short", totals={}),
        lambda led: led.record_freeze(split="tuning", set_sha256="short", totals={}),
        lambda led: led.record_split_attempt(round_no=1, pool_digest=DIGEST_A, outcome="released", payload_sha256="short"),
        lambda led: led.record_release(round_no=1, assignment_digest=DIGEST_A, payload_sha256=DIGEST_A.upper()),
        lambda led: led.exclude("a group id with spaces", "twin"),
        lambda led: led.exclude("g1", "Some Reason Text"),
        lambda led: led.claim("one"),
        lambda led: led.claim(-1),
        lambda led: led.claim(True),
        lambda led: led.claim("0B"),
        lambda led: led.recover("01"),
    ],
)
def test_records_are_content_free_and_bad_arguments_write_nothing(tmp_path, call):
    led = make(tmp_path)
    with pytest.raises(ValueError):
        call(led)
    assert not ledger_file().exists() or lines_of(ledger_file()) == []


def test_slot_ids_are_normalised(tmp_path):
    led = make(tmp_path)
    assert led.claim(0) == "0"
    assert led.claim("1") == "1"
    assert led.claim(2) == "2"
    assert led.recover(0).slot == "0"
    with pytest.raises(SlotRefused):
        led.claim("0")  # the int 0 and the string "0" are one slot


# ---------------------------------------------------------------------------
# Slot protocol: claim, refusals, ordering
# ---------------------------------------------------------------------------


def test_a_claimed_slot_cannot_be_claimed_again_at_any_stage(tmp_path):
    led = make(tmp_path)
    led.claim(1)
    with pytest.raises(SlotRefused):
        led.claim(1)  # claimed, nothing stored (spent)
    led.store_results(1, make_run(tmp_path, "r1"))
    with pytest.raises(SlotRefused):
        led.claim(1)  # stored
    led.record_intent(1)
    with pytest.raises(SlotRefused):
        led.claim(1)  # intent
    led.record_consumed(1)
    with pytest.raises(SlotRefused):
        led.claim(1)  # consumed
    assert make(tmp_path).recover(1).action is RecoveryAction.CONSUMED
    with pytest.raises(SlotRefused):
        make(tmp_path).claim(1)  # a restarted process is refused too
    assert kinds_in(ledger_file()) == ["slot_claim", "slot_stored", "publication_intent", "slot_consumed"]


def test_the_happy_path_walks_every_step(tmp_path):
    led = make(tmp_path)
    assert led.recover(3) == Recovery("3", RecoveryAction.NOT_STARTED)
    led.claim(3)
    assert led.recover(3).action is RecoveryAction.SPENT
    digest = led.store_results(3, make_run(tmp_path, "run-3"))
    rec = led.recover(3)
    assert (rec.action, rec.run_id, rec.results_digest) == (RecoveryAction.RENDER_FROM_STORAGE, "run-3", digest)
    led.record_intent(3)
    assert led.recover(3).action is RecoveryAction.REPUBLISH
    led.record_consumed(3)
    assert led.recover(3).action is RecoveryAction.CONSUMED
    intent = led.records("publication_intent")[0]
    assert intent["slot"] == "3" and intent["results_digest"] == digest  # binds the stored result


def test_out_of_order_protocol_calls_are_refused(tmp_path):
    led = make(tmp_path)
    run = make_run(tmp_path, "r")
    with pytest.raises(ProtocolError):
        led.store_results(1, run)  # not claimed
    with pytest.raises(ProtocolError):
        led.record_intent(1)  # not claimed
    with pytest.raises(ProtocolError):
        led.record_consumed(1)  # not claimed
    led.claim(1)
    with pytest.raises(ProtocolError):
        led.record_intent(1)  # nothing stored
    with pytest.raises(ProtocolError):
        led.record_consumed(1)  # nothing stored: a spent slot is not "consumed"
    led.store_results(1, run)
    with pytest.raises(ProtocolError):
        led.store_results(1, run)  # results are stored once
    led.record_intent(1)
    with pytest.raises(ProtocolError):
        led.record_intent(1)
    led.record_consumed(1)
    with pytest.raises(ProtocolError):
        led.record_consumed(1)
    with pytest.raises(ProtocolError):
        led.record_intent(1)  # a consumed slot cannot turn canonical afterwards
    assert kinds_in(ledger_file()) == ["slot_claim", "slot_stored", "publication_intent", "slot_consumed"]


def test_protocol_errors_are_not_caught_by_a_broad_except_exception(tmp_path):
    led = make(tmp_path)
    led.claim(1)
    assert issubclass(SealedLedgerError, BaseException) and not issubclass(SealedLedgerError, Exception)
    caught = None
    try:
        try:
            led.claim(1)
        except Exception:  # noqa: BLE001 - the point of the test
            caught = "swallowed"
    except SlotRefused:
        caught = "propagated"
    assert caught == "propagated"


def test_store_results_digests_the_run_directory(tmp_path):
    led = make(tmp_path)
    run = make_run(tmp_path, "run-1", payload="alpha")
    led.claim(1)
    digest = led.store_results(1, run)
    assert digest == digest_run_dir(run) and len(digest) == 64
    assert digest_run_dir(make_run(tmp_path, "run-1", payload="alpha")) == digest  # deterministic
    (run / "rows.jsonl").write_text("edited\n")
    assert digest_run_dir(run) != digest  # an edited result is visible to recovery
    (run / "rows.jsonl").write_text("alpha\n")
    (run / "extra.json").write_text("{}")
    assert digest_run_dir(run) != digest  # so is an added file
    assert led.records("slot_stored")[0]["run_id"] == "run-1"


def test_store_results_refuses_a_missing_empty_or_symlinked_directory(tmp_path):
    led = make(tmp_path)
    led.claim(1)
    with pytest.raises(ProtocolError):
        led.store_results(1, tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProtocolError):
        led.store_results(1, empty)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "rows.jsonl").symlink_to(make_run(tmp_path, "real") / "rows.jsonl")
    with pytest.raises(ProtocolError):
        led.store_results(1, linked)
    with pytest.raises(ValueError):
        led.store_results(1, make_run(tmp_path, "run with spaces"))
    assert led.recover(1).action is RecoveryAction.SPENT  # nothing was recorded


# ---------------------------------------------------------------------------
# Fault injection at every step boundary (acceptance (f))
# ---------------------------------------------------------------------------

# (steps completed before the crash, action recover must return)
BOUNDARIES = [
    pytest.param([], RecoveryAction.NOT_STARTED, id="before-claim"),
    pytest.param(["claim"], RecoveryAction.SPENT, id="after-claim-before-store"),
    pytest.param(["claim", "store"], RecoveryAction.RENDER_FROM_STORAGE, id="after-store-before-intent"),
    pytest.param(["claim", "store", "intent"], RecoveryAction.REPUBLISH, id="after-intent-before-or-after-first-number"),
    pytest.param(["claim", "store", "intent", "consumed"], RecoveryAction.CONSUMED, id="after-consumed-canonical"),
    pytest.param(["claim", "store", "consumed"], RecoveryAction.CONSUMED, id="after-consumed-non-canonical"),
]


@pytest.mark.parametrize("done,expected", BOUNDARIES)
def test_recovery_action_at_each_boundary_makes_no_fresh_call(tmp_path, done, expected):
    runner = FakeRunner()
    first = make(tmp_path)
    advance(first, tmp_path, 1, done)
    if done:
        runner.sealed_calls = 1  # the one call made before the crash
    before = ledger_file().read_bytes() if ledger_file().exists() else b""

    restarted = make(tmp_path)  # the crash: a new process, no memory of the old one
    rec = restarted.recover(1)
    assert rec.action is expected
    after = ledger_file().read_bytes() if ledger_file().exists() else b""
    assert after == before  # recover appends nothing
    assert runner.sealed_calls == (1 if done else 0)  # and made no sealed call

    acted = resume(make(tmp_path), 1, runner)
    assert acted.action is expected
    assert runner.sealed_calls == (1 if done else 0)  # acting on it made none either
    if expected is RecoveryAction.RENDER_FROM_STORAGE:
        assert runner.rendered == ["run-1"] and runner.published == []
        assert make(tmp_path).recover(1).action is RecoveryAction.CONSUMED  # consumed once rendered
    elif expected is RecoveryAction.REPUBLISH:
        assert runner.published == ["run-1"] and runner.rendered == []
        assert make(tmp_path).recover(1).action is RecoveryAction.CONSUMED
    else:
        assert runner.rendered == [] and runner.published == []
    if expected is RecoveryAction.SPENT:
        assert make(tmp_path).recover(1).action is RecoveryAction.SPENT  # still spent, never re-run
        with pytest.raises(SlotRefused):
            make(tmp_path).claim(1)


def test_a_slot_is_consumed_only_once_rendered(tmp_path):
    runner = FakeRunner()
    advance(make(tmp_path), tmp_path, 1, ["claim", "store"])
    # Crash during the render: nothing recorded, so a restart renders again.
    assert make(tmp_path).recover(1).action is RecoveryAction.RENDER_FROM_STORAGE
    assert make(tmp_path).recover(1).action is RecoveryAction.RENDER_FROM_STORAGE
    resume(make(tmp_path), 1, runner)
    assert runner.rendered == ["run-1"]
    assert make(tmp_path).recover(1).action is RecoveryAction.CONSUMED


def _crash_after(step: str, run_dir: str) -> None:
    led = SealedLedger(copy_file(Path(os.environ["TEST_TMP"])).parent, mirror=mirror_file(Path(os.environ["TEST_TMP"])))
    led.claim(1)
    if step in ("store", "intent"):
        led.store_results(1, run_dir)
    if step == "intent":
        led.record_intent(1)
    os._exit(17)  # die mid-protocol, no cleanup, no flush


@pytest.mark.parametrize(
    "step,expected",
    [
        ("claim", RecoveryAction.SPENT),
        ("store", RecoveryAction.RENDER_FROM_STORAGE),
        ("intent", RecoveryAction.REPUBLISH),
    ],
)
def test_a_real_process_crash_recovers_the_same_way(tmp_path, monkeypatch, step, expected):
    monkeypatch.setenv("TEST_TMP", str(tmp_path))
    run = make_run(tmp_path, "run-1")
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=_crash_after, args=(step, str(run)))
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode == 17
    led = make(tmp_path)
    assert led.recover(1).action is expected
    with pytest.raises(SlotRefused):
        led.claim(1)


# ---------------------------------------------------------------------------
# 0b
# ---------------------------------------------------------------------------


def test_0b_is_unavailable_until_slot_0_was_claimed(tmp_path):
    led = make(tmp_path)
    assert led.may_use_0b() is False
    with pytest.raises(SlotRefused):
        led.claim("0b")
    assert led.recover("0b").action is RecoveryAction.NOT_STARTED


def test_0b_is_allowed_once_after_a_non_canonical_slot_0(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 0, ["claim", "store", "consumed"])  # no intent: non-canonical, rendered
    assert led.recover(0).action is RecoveryAction.CONSUMED
    assert led.records("publication_intent") == []
    assert led.may_use_0b() is True
    assert make(tmp_path).may_use_0b() is True  # durable across restarts
    assert led.claim("0b") == "0b"
    assert led.may_use_0b() is False  # once only
    with pytest.raises(SlotRefused):
        led.claim("0b")
    with pytest.raises(SlotRefused):
        make(tmp_path).claim("0b")


def test_0b_is_allowed_after_a_spent_slot_0(tmp_path):
    led = make(tmp_path)
    led.claim(0)  # crash before step 3: the slot is spent, no intent was ever recorded
    assert make(tmp_path).recover(0).action is RecoveryAction.SPENT
    assert make(tmp_path).may_use_0b() is True


def test_0b_waits_while_slot_0_holds_stored_unrendered_results(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 0, ["claim", "store"])  # between steps 3 and 4: canonical status undecided
    assert led.may_use_0b() is False
    with pytest.raises(SlotRefused):
        led.claim("0b")
    led.record_consumed(0)  # rendered from storage, non-canonical
    assert led.may_use_0b() is True


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(["claim", "store", "intent"], id="intent-render-interrupted-or-after-first-number"),
        pytest.param(["claim", "store", "intent", "consumed"], id="intent-then-rendered-and-consumed"),
    ],
)
def test_0b_is_refused_after_any_durable_intent_on_slot_0(tmp_path, steps):
    led = make(tmp_path)
    advance(led, tmp_path, 0, steps)
    assert led.may_use_0b() is False
    assert make(tmp_path).may_use_0b() is False
    with pytest.raises(SlotRefused):
        led.claim("0b")
    assert make(tmp_path).recover(0).action in (RecoveryAction.REPUBLISH, RecoveryAction.CONSUMED)


def test_an_intent_on_another_slot_does_not_touch_0b(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 0, ["claim", "store", "consumed"])
    advance(led, tmp_path, 1, ["claim", "store", "intent", "consumed"])
    assert led.may_use_0b() is True


def test_0b_runs_the_same_protocol_as_any_slot(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 0, ["claim"])
    advance(led, tmp_path, "0b", ["claim", "store", "intent"])
    assert led.recover("0b").action is RecoveryAction.REPUBLISH
    assert led.records("slot_claim")[1]["slot"] == "0b"


# ---------------------------------------------------------------------------
# Exclusions and the floor
# ---------------------------------------------------------------------------


def test_exclusions_append_and_accumulate_across_restarts(tmp_path):
    led = make(tmp_path)
    assert led.exclusions() == frozenset()
    led.exclude("grp-1", "s_hit")
    led.exclude("grp-2", "exposure")
    led.exclude("grp-1", "twin")  # a second reason for the same group: recorded, one member
    assert led.exclusions() == frozenset({"grp-1", "grp-2"})
    assert make(tmp_path).exclusions() == frozenset({"grp-1", "grp-2"})
    assert [(r["group_id"], r["reason"]) for r in led.records("exclusion")] == [
        ("grp-1", "s_hit"), ("grp-2", "exposure"), ("grp-1", "twin"),
    ]


def test_a_post_freeze_exclusion_leaves_the_freeze_and_later_slots_alone(tmp_path):
    led = make(tmp_path)
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={"families": 60}
    )
    frozen = led.freeze_record("sealed")
    advance(led, tmp_path, 0, ["claim", "store", "intent", "consumed"])
    led.exclude("grp-9", "s_hit")  # an exclusion, never a move or unmark
    assert led.freeze_record("sealed") == frozen
    assert led.exclusions() == frozenset({"grp-9"})
    assert led.claim(1) == "1"  # the next slot is claimable; slot state is untouched
    assert led.recover(0).action is RecoveryAction.CONSUMED


def test_below_floor_signals_the_epoch_end(tmp_path):
    assert below_floor(34, 35) is True
    assert below_floor(35, 35) is False  # exactly at the floor is enough
    assert below_floor(60, 35) is False
    assert make(tmp_path).below_floor(34, 35) is True
    for bad in (True, -1, 3.5, "9"):
        with pytest.raises(ValueError):
            below_floor(bad, 35)
    with pytest.raises(ValueError):
        below_floor(10, True)


# ---------------------------------------------------------------------------
# Committed mirror: kinds and digests only, tamper-evident
# ---------------------------------------------------------------------------


def _full_history(tmp_path: Path) -> SealedLedger:
    led = make(tmp_path)
    led.record_split_attempt(round_no=1, pool_digest=DIGEST_A, outcome="released")
    led.record_release(round_no=1, assignment_digest=DIGEST_B, totals={"families": 60})
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=DIGEST_C, totals={"families": 60}
    )
    advance(led, tmp_path, 0, ["claim", "store", "intent", "consumed"])
    led.exclude("grp-7", "s_hit")
    return led


def _all_keys(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _all_keys(value)


def test_the_mirror_carries_record_kinds_and_digests_only(tmp_path):
    _full_history(tmp_path)
    mirror = [json.loads(line) for line in lines_of(mirror_file(tmp_path))]
    private = lines_of(copy_file(tmp_path))
    assert [m["kind"] for m in mirror] == [
        "split_attempt", "release", "freeze", "slot_claim", "slot_stored",
        "publication_intent", "slot_consumed", "exclusion",
    ]
    assert [m["seq"] for m in mirror] == list(range(8))
    for entry, line in zip(mirror, private):
        assert set(entry) <= {"seq", "kind", "digest", "digests", "split"}
        assert entry["digest"] == hashlib.sha256(line).hexdigest()
        for key in _all_keys(entry):
            assert not any(word in key for word in FORBIDDEN_WORDS), key
        for value in entry.get("digests", {}).values():
            assert len(value) == 64
    assert mirror[2]["digests"] == {"set_sha256": DIGEST_A, "twins_sha256": DIGEST_B, "exclusions_sha256": DIGEST_C}
    assert mirror[2]["split"] == "sealed"
    text = mirror_file(tmp_path).read_text()
    for word in ("scope", "chapter", "stratum"):
        assert word not in text
    assert text.replace('"split":"sealed"', "").count("sealed") == 0  # the split value is the only mention
    for private_value in ("grp-7", "s_hit", "run-0", "families", '"slot"', '"0"'):
        assert private_value not in text  # no ids, reasons, run ids, counts or slots


def test_an_edited_earlier_mirror_line_is_refused(tmp_path):
    led = _full_history(tmp_path)
    before = ledger_file().read_bytes()
    lines = lines_of(mirror_file(tmp_path))
    edited = json.loads(lines[1])
    edited["kind"] = "freeze"
    lines[1] = json.dumps(edited, sort_keys=True, separators=(",", ":")).encode()
    mirror_file(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    for attempt in (led, make(tmp_path)):
        with pytest.raises(MirrorTampered):
            attempt.exclude("grp-8", "twin")
        with pytest.raises(MirrorTampered):
            attempt.recover(0)  # reads refuse as well
    assert ledger_file().read_bytes() == before  # nothing was appended


def test_an_edited_last_mirror_line_is_refused_too(tmp_path):
    led = _full_history(tmp_path)
    lines = lines_of(mirror_file(tmp_path))
    lines[-1] = lines[-1].replace(b'"digest":"', b'"digest":"0')
    mirror_file(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(MirrorTampered):
        led.exclude("grp-8", "twin")


def test_a_lagging_mirror_is_caught_up_and_an_absent_one_is_optional(tmp_path):
    led = _full_history(tmp_path)
    full = mirror_file(tmp_path).read_bytes()
    lines = lines_of(mirror_file(tmp_path))
    mirror_file(tmp_path).write_bytes(b"\n".join(lines[:5]) + b"\n")  # crash before the last mirror writes
    led.exclude("grp-8", "twin")
    assert len(lines_of(mirror_file(tmp_path))) == 9
    assert mirror_file(tmp_path).read_bytes().startswith(full)
    mirror_file(tmp_path).unlink()  # the committed file deleted: it is derived, so rebuilt
    assert make(tmp_path).exclusions() == frozenset({"grp-7", "grp-8"})
    assert len(lines_of(mirror_file(tmp_path))) == 9

    other = tmp_path / "other"
    plain = SealedLedger(other / "copy", ledger=other / "ledger.jsonl")  # no mirror configured
    plain.exclude("g", "twin")
    assert not (other / "mirror.jsonl").exists() and plain.exclusions() == frozenset({"g"})


# ---------------------------------------------------------------------------
# Torn last line, corruption, and the private copies
# ---------------------------------------------------------------------------


def test_a_torn_last_line_is_ignored_and_cut_off_by_the_next_append(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 1, ["claim"])
    for path in (ledger_file(), copy_file(tmp_path), mirror_file(tmp_path)):
        with path.open("ab") as fh:
            fh.write(b'{"v":1,"seq":1,"kind":"slot_sto')  # a crash mid-write, no newline
    fresh = make(tmp_path)
    assert fresh.recover(1).action is RecoveryAction.SPENT  # the torn line is not a record
    fresh.exclude("grp-1", "twin")
    for path in (ledger_file(), copy_file(tmp_path), mirror_file(tmp_path)):
        data = path.read_bytes()
        assert data.endswith(b"\n") and b"slot_sto" not in data
        assert [json.loads(line)["seq"] for line in data.splitlines()] == [0, 1]
    assert ledger_file().read_bytes() == copy_file(tmp_path).read_bytes()


def test_a_ledger_holding_only_a_torn_line_is_empty(tmp_path):
    ledger_file().parent.mkdir(parents=True)
    ledger_file().write_bytes(b'{"v":1,"seq":0,"kind":"slot_cl')
    led = make(tmp_path)
    assert led.records() == []
    assert led.claim(1) == "1"
    assert [json.loads(line)["seq"] for line in lines_of(ledger_file())] == [0]


@pytest.mark.parametrize(
    "bad_line",
    [
        b"not json",
        b'{"v":1,"seq":5,"kind":"slot_claim","slot":"1"}',  # out of sequence
        b'{"v":2,"seq":0,"kind":"slot_claim","slot":"1"}',  # unknown version
        b'{"v":1,"seq":0,"kind":"mystery"}',  # unknown kind
        b'{"v":1,"seq":0,"kind":"slot_stored","slot":"1"}',  # stored without a claim
    ],
)
def test_a_bad_complete_line_fails_closed(tmp_path, bad_line):
    ledger_file().parent.mkdir(parents=True)
    copy_file(tmp_path).parent.mkdir(parents=True)
    for path in (ledger_file(), copy_file(tmp_path)):
        path.write_bytes(bad_line + b"\n")
    with pytest.raises(LedgerCorrupt):
        make(tmp_path, mirror=False).claim(1)


def test_an_impossible_persisted_order_fails_closed(tmp_path):
    led = make(tmp_path, mirror=False)
    advance(led, tmp_path, 1, ["claim"])
    line = lines_of(ledger_file())[0]
    dup = json.loads(line)
    dup["seq"] = 1
    both = line + b"\n" + json.dumps(dup, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    for path in (ledger_file(), copy_file(tmp_path)):
        path.write_bytes(both)  # the same slot claimed twice
    with pytest.raises(LedgerCorrupt):
        led.recover(1)


def test_a_lost_or_lagging_private_store_is_restored_from_the_other(tmp_path):
    led = _full_history(tmp_path)
    original = ledger_file().read_bytes()
    copy_file(tmp_path).unlink()  # the private copy is lost
    assert make(tmp_path).recover(0).action is RecoveryAction.CONSUMED
    assert copy_file(tmp_path).read_bytes() == original
    ledger_file().unlink()  # now the out-of-tree ledger is lost
    assert make(tmp_path).exclusions() == frozenset({"grp-7"})
    assert ledger_file().read_bytes() == original
    lines = lines_of(copy_file(tmp_path))  # a crash between the two writes: the copy lags by one
    copy_file(tmp_path).write_bytes(b"\n".join(lines[:-1]) + b"\n")
    led.exclude("grp-8", "twin")
    assert ledger_file().read_bytes() == copy_file(tmp_path).read_bytes()
    assert len(lines_of(ledger_file())) == 9


def test_disagreeing_private_stores_fail_closed(tmp_path):
    led = _full_history(tmp_path)
    lines = lines_of(copy_file(tmp_path))
    edited = json.loads(lines[3])
    edited["slot"] = "9"
    lines[3] = json.dumps(edited, sort_keys=True, separators=(",", ":")).encode()
    copy_file(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(LedgerCorrupt):
        led.exclude("grp-8", "twin")


def test_losing_both_private_copies_is_an_epoch_stop(tmp_path):
    led = _full_history(tmp_path)
    ledger_file().unlink()
    copy_file(tmp_path).unlink()
    assert not issubclass(EpochStop, LedgerCorrupt) and issubclass(EpochStop, SealedLedgerError)
    with pytest.raises(EpochStop):
        led.exclude("grp-8", "twin")
    with pytest.raises(EpochStop):
        make(tmp_path).claim(1)  # it does not heal by itself
    with pytest.raises(EpochStop):
        make(tmp_path).may_use_0b()
    assert len(lines_of(mirror_file(tmp_path))) == 8  # the mirror is untouched


def test_both_private_copies_truncated_behind_the_mirror_is_an_epoch_stop(tmp_path):
    led = _full_history(tmp_path)
    for path in (ledger_file(), copy_file(tmp_path)):
        lines = lines_of(path)
        path.write_bytes(b"\n".join(lines[:4]) + b"\n")  # both lost the same tail
    with pytest.raises(EpochStop):
        led.recover(0)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _append_child(barrier: Any, tag: int, count: int, tmp: str) -> None:
    led = SealedLedger(copy_file(Path(tmp)).parent, mirror=mirror_file(Path(tmp)))
    barrier.wait(timeout=30)
    for i in range(count):
        led.exclude(f"g{tag}-{i}", "twin")


def test_concurrent_appends_are_serialised_and_all_three_stores_agree(tmp_path):
    procs_n, count = 4, 12
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(procs_n)
    procs = [ctx.Process(target=_append_child, args=(barrier, p, count, str(tmp_path))) for p in range(procs_n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    recs = [json.loads(line) for line in lines_of(ledger_file())]
    assert [r["seq"] for r in recs] == list(range(procs_n * count))  # no lost or interleaved line
    assert {r["group_id"] for r in recs} == {f"g{p}-{i}" for p in range(procs_n) for i in range(count)}
    assert ledger_file().read_bytes() == copy_file(tmp_path).read_bytes()
    assert len(lines_of(mirror_file(tmp_path))) == procs_n * count
    assert len(make(tmp_path).exclusions()) == procs_n * count  # and the mirror still verifies


def _claim_child(barrier: Any, queue: Any, tmp: str) -> None:
    led = SealedLedger(copy_file(Path(tmp)).parent, mirror=mirror_file(Path(tmp)))
    barrier.wait(timeout=30)
    try:
        led.claim(1)
        queue.put("claimed")
    except SlotRefused:
        queue.put("refused")


def test_two_processes_racing_for_one_slot_exactly_one_claims_it(tmp_path):
    procs_n = 4
    ctx = multiprocessing.get_context("fork")
    barrier, queue = ctx.Barrier(procs_n), ctx.Queue()
    procs = [ctx.Process(target=_claim_child, args=(barrier, queue, str(tmp_path))) for _ in range(procs_n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    outcomes = sorted(queue.get(timeout=10) for _ in procs)
    assert outcomes == ["claimed"] + ["refused"] * (procs_n - 1)
    assert kinds_in(ledger_file()) == ["slot_claim"]


# ---------------------------------------------------------------------------
# Payloads: content-free records too big for a ledger line
# ---------------------------------------------------------------------------

PAYLOAD = {
    "seed": 1234,
    "assignment": {"fam-b": "sealed", "fam-a": "tuning"},
    "families": [{"id": "fam-a", "register": "lay", "source": "tutorial"}],
}


def payload_paths(tmp_path: Path, name: str) -> List[Path]:
    """The two copies: out-of-tree (next to the ledger) first, then the private copy's."""
    return [
        ledger_file().parent / "sealed_payloads" / f"{name}.json",
        copy_file(tmp_path).parent / "payloads" / f"{name}.json",
    ]


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def test_store_payload_writes_the_same_canonical_file_to_both_directories(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("release_r1", PAYLOAD)
    expected = canonical(PAYLOAD)
    assert digest == hashlib.sha256(expected).hexdigest()
    out_of_tree, private = payload_paths(tmp_path, "release_r1")
    assert out_of_tree.read_bytes() == expected == private.read_bytes()
    assert led.store_payload("release_r1", dict(reversed(list(PAYLOAD.items())))) == digest  # key order is moot
    assert led.load_payload("release_r1", digest) == PAYLOAD
    assert not list(out_of_tree.parent.glob(".*.tmp")) and not list(private.parent.glob(".*.tmp"))


def test_the_out_of_tree_payload_directory_sits_next_to_the_overridden_ledger(tmp_path):
    led = make(tmp_path)
    led.store_payload("p", {"k": 1})
    assert payload_paths(tmp_path, "p")[0].parent == ledger_file().parent / "sealed_payloads"
    assert payload_paths(tmp_path, "p")[0].exists()
    assert str(default_ledger_path().parent) not in str(payload_paths(tmp_path, "p")[0])
    # The default ledger (and with it the default payload directory) is refused under pytest.
    with pytest.raises(LedgerRefused):
        SealedLedger(tmp_path / "copy", ledger=default_ledger_path())


def test_store_payload_fsyncs_each_file_and_its_new_directory(tmp_path, monkeypatch):
    led = make(tmp_path)
    led.claim(1)  # create the ledger files first, so only the payload fsyncs are counted
    synced: List[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    led.store_payload("p", {"k": 1})
    assert len(synced) == 4  # two payload files, two directory entries


def test_load_payload_reads_whichever_copy_exists_and_verifies(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("p", PAYLOAD)
    out_of_tree, private = payload_paths(tmp_path, "p")
    out_of_tree.unlink()
    assert make(tmp_path).load_payload("p", digest) == PAYLOAD  # only the private copy is left
    led.store_payload("p", PAYLOAD)  # re-storing the same content fills the missing copy in
    assert out_of_tree.read_bytes() == private.read_bytes()
    private.unlink()
    assert make(tmp_path).load_payload("p", digest) == PAYLOAD  # only the out-of-tree copy is left


def test_load_payload_skips_a_copy_that_fails_its_digest(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("p", PAYLOAD)
    out_of_tree, private = payload_paths(tmp_path, "p")
    out_of_tree.write_bytes(canonical({**PAYLOAD, "seed": 9999}))  # altered, still valid JSON
    assert led.load_payload("p", digest) == PAYLOAD  # served from the copy that verifies
    private.write_bytes(b"{torn")
    with pytest.raises(EpochStop):
        led.load_payload("p", digest)  # neither copy verifies


def test_load_payload_raises_epoch_stop_when_both_copies_are_gone_or_the_digest_is_wrong(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("p", PAYLOAD)
    with pytest.raises(EpochStop):
        led.load_payload("p", DIGEST_A)  # a digest that matches neither copy
    with pytest.raises(EpochStop):
        led.load_payload("never_stored", digest)
    for path in payload_paths(tmp_path, "p"):
        path.unlink()
    with pytest.raises(EpochStop):
        led.load_payload("p", digest)
    assert not issubclass(EpochStop, LedgerCorrupt)


def test_a_payload_is_immutable(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("p", PAYLOAD)
    with pytest.raises(ProtocolError):
        led.store_payload("p", {**PAYLOAD, "seed": 1})
    out_of_tree, private = payload_paths(tmp_path, "p")
    private.unlink()
    with pytest.raises(ProtocolError):  # a different payload is refused even when only one copy exists
        led.store_payload("p", {**PAYLOAD, "seed": 1})
    assert not private.exists()  # and nothing was half-written
    assert led.load_payload("p", digest) == PAYLOAD
    out_of_tree.unlink()
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(canonical(PAYLOAD))
    with pytest.raises(ProtocolError):  # ... the same when the surviving copy is the private one
        led.store_payload("p", {**PAYLOAD, "seed": 1})
    assert not out_of_tree.exists()  # the missing copy was not filled with the refused content


@pytest.mark.parametrize("bad_name", ["", "../escape", "a/b", "a.b", "has space", "x" * 65, 3, None])
def test_payload_names_are_plain_tokens(tmp_path, bad_name):
    led = make(tmp_path)
    with pytest.raises(ValueError):
        led.store_payload(bad_name, {"k": 1})
    with pytest.raises(ValueError):
        led.load_payload(bad_name, DIGEST_A)
    assert not (ledger_file().parent / "sealed_payloads").exists()


@pytest.mark.parametrize("bad_obj", [float("nan"), {"k": float("inf")}, {"k": object()}, {1: "int key", "a": 2}, {1, 2}])
def test_a_payload_must_be_canonical_json(tmp_path, bad_obj):
    with pytest.raises(ValueError):
        make(tmp_path).store_payload("p", bad_obj)
    assert not (ledger_file().parent / "sealed_payloads").exists()


def test_load_payload_needs_a_hex_digest(tmp_path):
    led = make(tmp_path)
    led.store_payload("p", PAYLOAD)
    for bad in ("short", DIGEST_A.upper(), None):
        with pytest.raises(ValueError):
            led.load_payload("p", bad)


def test_records_reference_a_payload_by_digest_and_the_mirror_never_carries_content(tmp_path):
    led = make(tmp_path)
    canary = "CANARY-FAMILY-TEXT"
    big = {"families": [{"id": f"fam-{i}", "register": "lay", "note": canary} for i in range(50)], "seed": 7}
    digest = led.store_payload("assignment_r1", big)
    led.record_split_attempt(round_no=1, pool_digest=DIGEST_A, outcome="released", payload_sha256=digest)
    led.record_release(round_no=1, assignment_digest=DIGEST_B, totals={"families": 50}, payload_sha256=digest)
    led.record_release(round_no=2, assignment_digest=DIGEST_C)  # the field is optional
    attempt, release = led.records("split_attempt")[0], led.records("release")
    assert attempt["payload_sha256"] == digest and release[0]["payload_sha256"] == digest
    assert "payload_sha256" not in release[1]
    mirror = [json.loads(line) for line in lines_of(mirror_file(tmp_path))]
    assert mirror[0]["digests"] == {"pool_digest": DIGEST_A, "payload_sha256": digest}
    assert mirror[1]["digests"] == {"assignment_digest": DIGEST_B, "payload_sha256": digest}
    assert mirror[2]["digests"] == {"assignment_digest": DIGEST_C}
    for path in (mirror_file(tmp_path), ledger_file(), copy_file(tmp_path)):
        text = path.read_text()
        assert canary not in text and "fam-0" not in text  # payload content is in the payload files only
    assert led.load_payload("assignment_r1", digest) == big


def test_payload_storage_fails_closed_on_a_broken_ledger_and_survives_a_restart(tmp_path):
    led = make(tmp_path)
    digest = led.store_payload("p", PAYLOAD)
    assert make(tmp_path).load_payload("p", digest) == PAYLOAD  # a restarted process reads it
    led.exclude("grp-1", "twin")  # a ledger record, so the mirror has lines to tamper with
    mirror_file(tmp_path).write_bytes(b"tampered\n")
    with pytest.raises(MirrorTampered):
        led.store_payload("q", {"k": 1})  # no payload is written beside a ledger that fails its checks
    assert not any(path.exists() for path in payload_paths(tmp_path, "q"))
