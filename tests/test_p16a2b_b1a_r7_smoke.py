"""16A-2b b1a closure round r7, T6 (auditor m5): the pre-freeze smoke ``pipeline eval --sealed-slot N --preflight-only``.

It runs every gate and the preflight with the real run's command line and claims nothing, calls no model and records nothing.
The gates that need the ledger's ``controls`` record (``controls`` at the gate, ``incumbent`` in the preflight) report ``pending``
while that record does not exist instead of refusing; every other gate refuses as it would for the real run; a token minted for
the smoke can never be claimed or run.

Synthetic data only (``tests/p16_slot.py``); the retrieval, generation and judge are faked at the matrix runner's seams.
"""

from __future__ import annotations

import json

import pytest

import src.evaluator as ev
import src.pipeline
from src import eval_privacy, sealed_run
from tests.p16_capture import PROVENANCE
from tests.p16_slot import Crash, _langsmith_env_cache, bare, kinds, world  # noqa: F401 - the fixtures are used by name


@pytest.fixture
def no_controls(bare):
    """The state before the incumbent's draws exist: the sealed set, both development splits and the prereg are frozen; the
    ``controls`` record is not there."""
    bare.write_prereg()
    bare.write_records()
    bare.freeze(controls=False)
    assert bare.ledger().controls_record() is None
    return bare


@pytest.fixture
def matrix_calls(monkeypatch):
    """Every ``run_eval_matrix`` call gets the world's fakes (the CLI passes none); the list records the calls' keywords."""
    calls = []
    real = ev.run_eval_matrix

    def wrapper(set_specs, **kwargs):
        calls.append(kwargs)
        return real(set_specs, **kwargs)

    monkeypatch.setattr(ev, "run_eval_matrix", wrapper)
    return calls


def smoke(monkeypatch, capsys, w, calls, *extra, slot="0", drop=(), key=True, preflight=True):
    """``pipeline eval --sealed-slot <slot> --preflight-only ...`` with the world's fakes at the matrix runner's seams."""
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    retrieval = w.retrieval()
    real = ev.run_eval_matrix

    def faked(set_specs, **kwargs):
        kwargs.setdefault("retrieve_fn_factory", retrieval.factory(6))
        kwargs.setdefault("generate_fn", w.generator(retrieval))
        kwargs.setdefault("judge_fn", w.judge())
        kwargs.setdefault("provenance_fn", lambda: dict(PROVENANCE))
        calls.append(kwargs)
        return real(set_specs, **kwargs)

    monkeypatch.setattr(ev, "run_eval_matrix", faked)
    argv = [
        "--golden", str(w.golden_path), "--heldout", str(w.sealed_path), "--realistic", str(w.realistic_path),
        "--sealed-slot", slot, "--prereg", str(w.prereg_path), "--slot-records", str(w.records_dir), "--judge",
    ]
    for option in drop:
        i = argv.index(option)
        del argv[i:i + (1 if option == "--judge" else 2)]
    if preflight:
        argv.append("--preflight-only")
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv, *extra])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


def assert_untouched(w, before_records):
    assert w.ledger().records() == before_records  # nothing claimed, stored, excluded or logged
    assert not (w.private / "sealed" / "runs").exists() or list((w.private / "sealed" / "runs").iterdir()) == []
    assert not sealed_run.results_v6_path().exists()
    with w.ledger().lock_slot("0"):  # the smoke released the slot's execution lock
        pass


def test_the_smoke_passes_before_the_controls_exist_reporting_the_two_gates_pending(no_controls, monkeypatch, capsys):
    calls = []
    before = no_controls.ledger().records()
    code, out, err = smoke(monkeypatch, capsys, no_controls, calls)
    assert code == 0, err
    assert out.strip() == "[eval] sealed slot 0 preflight passed; nothing was claimed; pending gates: controls, incumbent"
    assert [c.get("preflight_only") for c in calls] == [True]  # the preflight ran (the evaluator's checks, up to the first call)
    assert calls[0]["slot_token"].pending == ("controls", "incumbent")
    assert_untouched(no_controls, before)


def test_without_the_flag_the_same_command_still_refuses_for_the_missing_controls(no_controls, monkeypatch, capsys):
    calls = []
    before = no_controls.ledger().records()
    code, out, err = smoke(monkeypatch, capsys, no_controls, calls, preflight=False)
    assert code == 4 and "gate=controls" in err and out == ""
    assert calls == [] and no_controls.ledger().records() == before


def test_a_controls_record_that_exists_is_checked_for_real_not_deferred(no_controls, monkeypatch, capsys):
    no_controls.record_controls()
    calls = []
    before = no_controls.ledger().records()
    code, out, err = smoke(monkeypatch, capsys, no_controls, calls)
    assert code == 0, err
    assert out.strip() == "[eval] sealed slot 0 preflight passed; nothing was claimed; pending gates: none"
    assert calls[0]["slot_token"].pending == ()
    assert_untouched(no_controls, before)
    # a changed controls list is a refusal, with the flag as without it
    sealed_run.controls_path().write_text('{"version": 2}\n', encoding="utf-8")
    code, out, err = smoke(monkeypatch, capsys, no_controls, [])
    assert code == 4 and "gate=controls" in err and "pending" not in out


def test_a_recorded_incumbent_that_is_not_the_running_pipeline_is_refused_not_pending(no_controls, monkeypatch, capsys):
    no_controls.record_controls(incumbent_config_hash="e" * 64)
    code, out, err = smoke(monkeypatch, capsys, no_controls, [])
    assert code == 4 and "gate=incumbent" in err and out == ""


@pytest.mark.parametrize("extra,drop,gate", [
    ((), ("--judge",), "gate=judge (off)"),
    (("--judge-sample", "2"), (), "gate=judge (sample)"),
    (("--top-k", "3"), (), "gate=cutoff"),
    (("--expansion", "build:x.json"), (), "gate=expansion_mode"),
    (("--rewrite-candidate", "a" * 64), (), "gate=expansion_mode"),
])
def test_every_other_gate_refuses_in_the_smoke_as_it_would_for_the_real_run(no_controls, monkeypatch, capsys, extra, drop, gate):
    before = no_controls.ledger().records()
    code, out, err = smoke(monkeypatch, capsys, no_controls, [], *extra, drop=drop)
    assert code in (2, 4) and gate in err, err
    assert no_controls.ledger().records() == before


def test_missing_approvals_and_a_changed_sealed_file_refuse_in_the_smoke(no_controls, monkeypatch, capsys):
    (no_controls.records_dir / "approvals.json").unlink()
    code, out, err = smoke(monkeypatch, capsys, no_controls, [])
    assert code == 4 and "gate=approvals" in err
    no_controls.write_records()
    original = no_controls.sealed_path.read_bytes()
    no_controls.sealed_path.chmod(0o644)
    no_controls.sealed_path.write_bytes(original + b"\n")
    try:
        code, out, err = smoke(monkeypatch, capsys, no_controls, [])
        assert code == 4 and "gate=set_file" in err
    finally:
        no_controls.sealed_path.write_bytes(original)


def test_tracing_and_a_missing_meter_refuse_in_the_smoke(no_controls, monkeypatch, capsys):
    code, out, err = smoke(monkeypatch, capsys, no_controls, [], key=False)
    assert code == 4 and "gate=meter" in err  # (no usable key: the CLI builds no meter, and every slot needs one)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    code, out, err = smoke(monkeypatch, capsys, no_controls, [])
    assert code == 4 and "gate=tracing" in err


def test_a_slot_already_in_the_ledger_is_a_claim_refusal_and_the_smoke_never_finishes_it(world, monkeypatch, capsys):
    assert world.run(0).canonical is True
    capsys.readouterr()  # (the run's own summary)
    published = sealed_run.results_v6_path().read_bytes()
    before = world.ledger().records()
    code, out, err = smoke(monkeypatch, capsys, world, [])
    assert code == 4 and "gate=claim" in err and out == ""
    assert world.ledger().records() == before and sealed_run.results_v6_path().read_bytes() == published  # (recovery would append)


def test_the_flag_needs_a_sealed_slot(no_controls, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--golden", str(no_controls.golden_path), "--preflight-only"])
    with pytest.raises(SystemExit) as info:
        src.pipeline.main()
    assert info.value.code == 2 and "--preflight-only needs --sealed-slot" in capsys.readouterr().err


# --- the token the smoke mints can never start a run -----------------------------------------------------------------------------------
def deferred_token(w):
    return sealed_run.acquire_slot_token(
        0, ledger=w.ledger(), set_path=w.sealed_path, prereg_path=w.prereg_path, records_dir=w.records_dir, expansion="live",
        meter=__import__("tests.test_p16w_spend", fromlist=["make_meter"]).make_meter(), top_k=6, defer_controls=True,
    )


def test_a_smoke_token_cannot_be_claimed_begun_or_run(no_controls):
    w = no_controls
    token = deferred_token(w)
    try:
        assert token.pending == ("controls", "incumbent") and not token.claimed[0]
        receipt = sealed_run.preflight_slot(token, **w.run_args())
        for call in (lambda: token.claim(receipt), token.begin):
            with pytest.raises(sealed_run.GateRefused) as info:
                call()
            assert info.value.gate == "claim"
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.run_slot(token, ledger=w.ledger(), **w.run_args())
        assert (info.value.gate, info.value.detail) == ("claim", "pending")
    finally:
        token.release()
    assert "slot_claim" not in kinds(w)


def test_a_real_token_has_nothing_pending_and_the_defer_flag_needs_the_record_to_be_absent(world):
    token = world.token(0, claim=False)
    try:
        assert token.pending == ()
    finally:
        token.release()
    deferred = deferred_token(world)  # the controls record exists (the world makes it): checked in full, nothing pending
    try:
        assert deferred.pending == ()
    finally:
        deferred.release()


def test_the_smoke_summary_names_only_gates(no_controls, monkeypatch, capsys):
    code, out, err = smoke(monkeypatch, capsys, no_controls, [])
    assert code == 0
    for secret in no_controls.canaries.secrets():
        assert secret not in out + err
    assert str(no_controls.sealed_path) not in out + err and json.dumps(no_controls.prereg)[:20] not in out
