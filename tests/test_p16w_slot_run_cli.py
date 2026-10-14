"""Phase 16A-2a a5: ``pipeline eval --sealed-slot`` (the CLI around the gate and the protocol).

The slot run itself is exercised in test_p16w_slot_run*.py; here the command line
is: flags thread through, every refusal exits with its code and prints a gate or
guard name and nothing else, and a slot already in the ledger is recovered from
storage instead of run again.
"""

import os
from pathlib import Path

import pytest

import src.evaluator as ev
import src.pipeline
from src import sealed_run
from src.expansion_artifact import rewrite_identity
from src.sealed_ledger import RecoveryAction
from src.spend import SpendLimitReached, SpendMeter
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    Crash,
    _langsmith_env_cache,
    bare,
    kinds,
    world,
)


def cli(monkeypatch, capsys, world, *extra, key=True, base=None, slot="0"):
    """Run ``pipeline eval --sealed-slot ...``; return ``(exit code, stdout, stderr)``."""
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    world.ensure_batch(slot)  # a confirmatory slot needs the batch declaration (b1a)
    argv = base if base is not None else [
        "--golden", str(world.golden_path), "--heldout", str(world.sealed_path),
        "--realistic", str(world.realistic_path), "--sealed-slot", slot, "--prereg", str(world.prereg_path),
        "--slot-records", str(world.records_dir), "--judge",
    ]
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv, *extra])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


def _boom(*args, **kwargs):
    raise AssertionError("a fresh call was made")


@pytest.fixture
def spy_run_slot(monkeypatch):
    captured = {}

    def spy(token, **kwargs):
        captured["token"], captured["kwargs"] = token, kwargs
        return sealed_run.SlotOutcome(token.slot, token.run_id, False, ("refusals_scored",), False, 0, "render_from_storage")

    monkeypatch.setattr(sealed_run, "run_slot", spy)
    return captured


def test_the_flags_thread_through_to_the_gate_and_the_runner(world, monkeypatch, capsys, spy_run_slot):
    code, out, err = cli(monkeypatch, capsys, world, "--top-k", "6", "--mode", "all", "--judge-sample", "5")
    assert code == 0
    token, kw = spy_run_slot["token"], spy_run_slot["kwargs"]
    assert token.slot == "0" and token.expansion_mode == "live"
    # the CLI mints the token unclaimed: run_slot claims it after its preflight, right before the first call
    assert "slot_claim" not in kinds(world) and token.claimed == [False]
    assert [label for label, _ in kw["set_specs"]] == ["golden", "held-out", "realistic"]
    assert kw["set_specs"][1][1] == str(world.sealed_path) and kw["modes"] == list(ev.EVAL_MODES)
    assert kw["top_k"] == 6 and kw["judge"] is True and kw["judge_sample"] == 5 and kw["expansion"] == "live"
    assert isinstance(kw["meter"], SpendMeter) and "records_dir" not in kw  # approvals are read at the gate only
    assert kw["skip_refusals"] is False and kw["skip_completeness"] is False


def test_the_sealed_slot_flag_may_be_abbreviated_no_further_than_argparse_allows(world, monkeypatch, capsys, spy_run_slot):
    base = [
        "--golden", str(world.golden_path), "--heldout", str(world.sealed_path), "--sealed-s", "0",
        "--prereg", str(world.prereg_path), "--slot-records", str(world.records_dir),
    ]
    code, _out, _err = cli(monkeypatch, capsys, world, base=base)
    assert code == 0 and spy_run_slot["token"].slot == "0"


@pytest.mark.parametrize("drop,extra", [("--prereg", ()), (None, ("--results", "out.md"))])
def test_usage_errors_exit_2_before_anything_is_claimed(world, monkeypatch, capsys, spy_run_slot, drop, extra):
    argv = [
        "--golden", str(world.golden_path), "--heldout", str(world.sealed_path), "--sealed-slot", "0",
        "--prereg", str(world.prereg_path), "--slot-records", str(world.records_dir),
    ]
    if drop:
        i = argv.index(drop)
        del argv[i:i + 2]
    code, out, err = cli(monkeypatch, capsys, world, *extra, base=argv)
    assert code == 2 and spy_run_slot == {} and "slot_claim" not in kinds(world)


def test_a_sealed_set_not_passed_as_heldout_or_a_bad_dev_set_is_refused_unclaimed(world, tmp_path, monkeypatch, capsys, spy_run_slot):
    for argv in (
        ["--golden", str(world.sealed_path), "--heldout", str(world.golden_path)],  # swapped
        ["--golden", str(world.sealed_path)],  # no --heldout at all
        ["--golden", str(world.golden_path), "--heldout", str(world.sealed_path), "--realistic", str(tmp_path / "missing.jsonl")],
        ["--golden", str(world.golden_path), "--heldout", str(world.sealed_path), "--realistic", str(world.golden_path)],  # same file twice
    ):
        code, out, err = cli(monkeypatch, capsys, world, base=[
            *argv, "--sealed-slot", "0", "--prereg", str(world.prereg_path), "--slot-records", str(world.records_dir)])
        assert code == 4 and "gate=set_binding" in err and spy_run_slot == {}
    assert "slot_claim" not in kinds(world)


def test_a_missing_api_key_means_no_meter_and_the_gate_refuses(world, monkeypatch, capsys, spy_run_slot):
    code, out, err = cli(monkeypatch, capsys, world, key=False)
    assert code == 4 and "gate=meter" in err and spy_run_slot == {} and "slot_claim" not in kinds(world)


def test_the_rewrite_candidate_flag_must_match_the_declaration(world, monkeypatch, capsys, spy_run_slot):
    config_hash = rewrite_identity()["config_hash"]
    code, out, err = cli(monkeypatch, capsys, world, "--rewrite-candidate", config_hash)
    assert code == 4 and "gate=expansion_mode" in err and spy_run_slot == {}
    world.write_prereg({"0": {"expansion": "candidate", "rewrite_candidate": config_hash}})
    code, out, err = cli(monkeypatch, capsys, world, "--rewrite-candidate", config_hash)
    assert code == 0 and spy_run_slot["token"].expansion_mode == "candidate"


def test_refusals_print_a_gate_name_and_no_path_text_or_id(world, monkeypatch, capsys, spy_run_slot, tmp_path):
    world.sealed_path.write_text(world.sealed_path.read_text() + "\n")
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and err.strip() == "[eval] sealed slot refused: gate=set_file" and out == ""
    assert str(tmp_path) not in out + err and not [i for i in world.ids() if i in out + err]


def test_tracing_left_on_refuses_the_gate_with_the_reasons(world, monkeypatch, capsys, spy_run_slot):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and "gate=tracing" in err and spy_run_slot == {} and "slot_claim" not in kinds(world)


def test_a_slot_already_consumed_is_a_claim_refusal_and_nothing_runs(world, monkeypatch, capsys):
    world.run(0)
    capsys.readouterr()
    monkeypatch.setattr(ev, "run_eval_matrix", _boom)
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and "gate=claim (consumed)" in err


def test_a_spent_slot_is_never_re_run(world, monkeypatch, capsys):
    with pytest.raises(Crash):
        world.run(0, generate=lambda q: (_ for _ in ()).throw(Crash()))
    monkeypatch.setattr(ev, "run_eval_matrix", _boom)
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and "gate=claim (spent)" in err


def test_a_stored_slot_is_recovered_from_the_cli_without_any_fresh_call(world, monkeypatch, capsys):
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_run, "finish_slot", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            world.run(0)
    capsys.readouterr()

    monkeypatch.setattr(ev, "run_eval_matrix", _boom)
    monkeypatch.setattr(ev, "expand_query", _boom)
    code, out, err = cli(monkeypatch, capsys, world, key=False)  # recovery needs no key and no meter
    assert code == 0 and "canonical; published results_v6.md" in out
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED
    assert sealed_run.results_v6_path().is_file()


def test_exit_codes_for_the_epoch_stops_the_publication_block_and_the_spend_limits(world, monkeypatch, capsys):
    # below the floor: a D64 owner stop, exit 8, before any claim
    world.write_prereg(floor=(9, 4))
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 8 and "gate=floor" in err and "slot_claim" not in kinds(world)
    world.write_prereg(floor=(6, 4))

    # the spend limits after the claim: 3 (week) and 6 (run); each spends its slot (0b is the spare)
    for kind, slot, expected in (("week", "0", 3), ("run", "0b", 6)):
        def spend_out(token, _k=kind, **kw):  # what run_slot does: claim, then the meter stops the run
            token.claim()
            raise SpendLimitReached(_k)

        monkeypatch.setattr(sealed_run, "run_slot", spend_out)
        code, out, err = cli(monkeypatch, capsys, world, slot=slot)
        assert code == expected and "spend limit reached" in err
        assert world.ledger().recover(slot).action is RecoveryAction.SPENT


def test_a_blocked_publication_exits_5_and_a_lost_ledger_exits_8(world, monkeypatch, capsys):
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_run, "finish_slot", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            world.run(0)
    capsys.readouterr()
    monkeypatch.setattr(sealed_run, "default_scanner", lambda run_id, notes=None: (lambda text: [None]))
    code, out, err = cli(monkeypatch, capsys, world, key=False)
    assert code == 5 and "publication blocked: unattributed_hit" in err and not sealed_run.results_v6_path().exists()
    Path(os.environ["CC_SEALED_LEDGER"]).unlink()
    (world.private / "sealed" / "ledger" / "sealed_ledger.jsonl").unlink()
    code, out, err = cli(monkeypatch, capsys, world, key=False)
    assert code == 8 and "EpochStop" in err and "D64 owner stop" in err


def test_without_the_flag_sealed_input_is_still_refused_with_exit_4(world, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--heldout", str(world.sealed_path), "--skip-refusals",
                                     "--skip-completeness"])
    with pytest.raises(SystemExit) as info:
        src.pipeline.main()
    assert info.value.code == 4
    assert "slot_claim" not in kinds(world)
