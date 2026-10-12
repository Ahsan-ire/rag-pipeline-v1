"""Phase 16A-2a gate fix A1: the per-slot execution lock (a live slot is not a spent slot).

The ledger alone cannot tell a running slot from a crashed one (both are "claimed, nothing
stored"), so slot 0b could be claimed while slot 0 was still running, and two recoveries of
one slot could both finish it. These tests use real second holders of the lock: a child
process holding the slot's flock file, and forked processes racing through recovery.
"""

import multiprocessing
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import sealed_run
from src.sealed_ledger import ProtocolError, RecoveryAction, SealedLedger, SlotBusy, SlotRefused
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    Crash,
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    world,
)
from tests.test_p16w_sealed_ledger import advance, make, make_run

_HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"  # holds the lock until the parent closes our stdin (or dies)
)


@contextmanager
def other_process_holding(path: Path):
    """A second process that holds an exclusive flock on ``path`` for the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=20)


# --- the ledger: 0b and the lock ------------------------------------------------------------------


def test_0b_is_refused_while_another_process_holds_slot_zeros_lock(tmp_path):
    led = make(tmp_path)
    led.claim(0)  # claimed, nothing stored: by the ledger alone this looks exactly like a crashed slot
    assert led.may_use_0b() is True
    with other_process_holding(led.slot_lock_path(0)):
        with pytest.raises(SlotBusy):  # slot 0 is running in the other process
            led.claim("0b")
        with pytest.raises(SlotRefused):  # (SlotBusy is a SlotRefused: older callers still catch it)
            make(tmp_path).claim("0b")
    assert kinds_of(led) == ["slot_claim"]  # nothing was claimed
    assert led.claim("0b") == "0b"  # the other process is gone: its lock went with it


def kinds_of(led):
    return [r["kind"] for r in led.records()]


def test_the_lock_is_exclusive_non_blocking_and_released_by_release(tmp_path):
    led = make(tmp_path)
    first = led.lock_slot("0")
    with pytest.raises(SlotBusy):
        led.lock_slot(0)  # the same slot, even from this process and a second ledger object
    with pytest.raises(SlotBusy):
        make(tmp_path).lock_slot("0")
    other = led.lock_slot("1")  # other slots are independent
    other.release()
    first.release()
    first.release()  # idempotent
    with led.lock_slot("0"):
        pass
    assert led.lock_slot("0").held


def test_slot_zero_cannot_store_results_once_0b_is_claimed(tmp_path):
    led = make(tmp_path)
    led.claim(0)  # spent: its process died before step 3
    led.claim("0b")
    with pytest.raises(ProtocolError, match="superseded"):
        led.store_results(0, make_run(tmp_path, "zombie"))
    assert kinds_of(led) == ["slot_claim", "slot_claim"]


def test_slot_zero_cannot_record_an_intent_once_0b_is_claimed(tmp_path):
    led = make(tmp_path)
    advance(led, tmp_path, 0, ["claim", "store", "consumed"])  # rendered non-canonically, no intent
    led.claim("0b")
    with pytest.raises(ProtocolError, match="superseded"):
        led.record_intent(0)
    assert led.records("publication_intent") == []
    advance(led, tmp_path, "0b", ["store", "intent"])  # 0b itself is unaffected


# --- the gate and the token --------------------------------------------------------------------------


def test_a_live_slot_zero_keeps_0b_out_until_its_run_ends_or_dies(world):
    token0 = world.token(0)  # claimed, matrix not yet run: the ledger says "spent", the lock says "running"
    assert world.ledger().may_use_0b() is True
    with pytest.raises(sealed_run.GateRefused) as info:
        world.token("0b")
    assert (info.value.gate, info.value.detail) == ("claim", "busy")
    assert kinds(world) == ["freeze", "slot_claim"]  # 0b was not claimed
    token0.release()  # the process of slot 0 ends or dies
    assert world.token("0b").slot == "0b"


def test_a_slot_another_process_runs_is_not_claimable_and_not_a_spent_slot(world):
    lock_path = world.ledger().slot_lock_path(0)
    with other_process_holding(lock_path):
        with pytest.raises(sealed_run.GateRefused) as info:
            world.token(0)
        assert (info.value.gate, info.value.detail) == ("claim", "busy")
        assert kinds(world) == ["freeze"]
    assert world.token(0).slot == "0"


def test_run_slot_releases_the_lock_when_the_run_dies(world):
    def die(question):
        raise Crash("mid-run")

    with pytest.raises(Crash):
        world.run(0, generate=die)
    assert world.ledger().recover(0).action is RecoveryAction.SPENT
    with world.ledger().lock_slot(0):  # free: the dead run does not keep 0b out
        pass
    assert world.run("0b").canonical


def test_a_finished_run_leaves_its_slot_lock_free(world):
    world.run(0)
    with world.ledger().lock_slot(0):
        pass


# --- recovery --------------------------------------------------------------------------------------------


def crash_after_store(world):
    def boom(*args, **kwargs):
        raise Crash("finish_slot")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_run, "finish_slot", boom)
        with pytest.raises(Crash):
            world.run(0)


def test_recovery_refuses_a_slot_another_process_holds_and_changes_nothing(world):
    crash_after_store(world)
    before = kinds(world)
    assert world.ledger().recover(0).action is RecoveryAction.RENDER_FROM_STORAGE
    with other_process_holding(world.ledger().slot_lock_path(0)):
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.recover_slot(0, ledger=world.ledger())
        assert (info.value.gate, info.value.detail) == ("claim", "busy")
        assert kinds(world) == before and not sealed_run.results_v6_path().exists()
    outcome = sealed_run.recover_slot(0, ledger=world.ledger())  # the holder is gone: recovery goes through
    assert outcome.canonical and kinds(world) == [*before, "publication_intent", "slot_consumed"]


def test_a_live_unstored_slot_is_busy_not_spent_to_recovery(world):
    token = world.token(0)  # a running slot: claimed, nothing stored yet
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.recover_slot(0, ledger=world.ledger())
    assert (info.value.gate, info.value.detail) == ("claim", "busy")  # not "spent"
    token.release()
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.recover_slot(0, ledger=world.ledger())
    assert (info.value.gate, info.value.detail) == ("claim", "spent")


def _recover_child(queue, name):
    try:
        outcome = sealed_run.recover_slot(0, ledger=sealed_run.default_ledger(), scan=lambda text: [])
        queue.put((name, f"ok:{outcome.action}"))
    except sealed_run.GateRefused as exc:
        queue.put((name, f"gate:{exc.gate}:{exc.detail}"))
    except BaseException as exc:  # noqa: BLE001 - whatever a false D64 stop would raise
        queue.put((name, f"error:{type(exc).__name__}"))


def test_two_processes_recovering_one_slot_cannot_both_finish_it(world, monkeypatch):
    """One finishes it; the other is refused cleanly (busy), never a ProtocolError from a second finish."""
    crash_after_store(world)
    real = sealed_run.render_and_publish

    def slow(*args, **kwargs):  # keeps the first recovery inside its critical section while the second starts
        time.sleep(0.8)
        return real(*args, **kwargs)

    monkeypatch.setattr(sealed_run, "render_and_publish", slow)
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    first = ctx.Process(target=_recover_child, args=(queue, "first"))
    second = ctx.Process(target=_recover_child, args=(queue, "second"))
    first.start()
    time.sleep(0.3)
    second.start()
    results = dict(queue.get(timeout=60) for _ in range(2))
    first.join(30)
    second.join(30)
    assert results["first"] == "ok:render_from_storage"
    assert results["second"] == "gate:claim:busy"
    assert kinds(world).count("publication_intent") == 1 and kinds(world).count("slot_consumed") == 1
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED


# --- the CLI maps the refusal to its exit code --------------------------------------------------------------


def test_the_cli_reports_a_busy_slot_as_a_claim_refusal_not_a_ledger_stop(world, monkeypatch, capsys):
    from tests.test_p16w_slot_run_cli import cli

    with other_process_holding(world.ledger().slot_lock_path(0)):
        code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and "gate=claim (busy)" in err and "D64" not in err
    assert "slot_claim" not in kinds(world)
