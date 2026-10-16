"""16A-2b b1a closure round r4, the slot-gate parts: the incumbent's identity (A), the registered judge (B) and the claim
boundary (C).

Synthetic data only (``tests/p16_slot.py``): the world patches ``pipeline_config.index_identity`` to a fixed value, so
its pipeline configuration hash is the real code's plus that index, and ``record_controls`` records it as the incumbent.
The ``sealed_tool`` parts are in ``tests/test_p16a2b_b1a_r4_tool.py``, the launcher's in
``tests/test_p16a2b_b1a_r4_launch.py``, ``pipeline_config`` itself in ``tests/test_p16a2b_b1a_r4_config.py``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import src.evaluator as ev
import src.pipeline
from src import generator, judge, pipeline_config, retriever, sealed_run
from src.expansion_artifact import rewrite_identity
from src.sealed_ledger import RecoveryAction
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    BATCH,
    WORLD_INDEX,
    _langsmith_env_cache,
    bare,
    candidate_entry,
    gate_of,
    kinds,
    no_calls,
    world,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_CHECK_CANDIDATE_IDENTITY = sealed_run.check_candidate_identity  # captured before any fixture replaces it
PIPELINE = "9" * 64


def refusal(call):
    """``(gate, detail)`` a call refuses with."""
    with pytest.raises(sealed_run.GateRefused) as info:
        call()
    return info.value.gate, info.value.detail


def claims(world):
    return [r for r in world.ledger().records() if r["kind"] == "slot_claim"]


# =============================================================================================================================
# A. The incumbent
# =============================================================================================================================
def test_the_world_records_its_own_pipeline_as_the_incumbent(world):
    assert world.ledger().incumbent_config_hash() == world.config_hash() and len(world.config_hash()) == 64


def test_slot_0_under_another_pipeline_than_the_incumbent_is_refused_before_the_claim_and_before_any_call(world, monkeypatch, no_calls):
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: "2" * 64)  # another index
    assert refusal(lambda: world.run(0)) == ("incumbent", None)
    assert claims(world) == [] and "slot_claim" not in kinds(world) and no_calls == []


@pytest.mark.parametrize("change", [
    pytest.param(lambda m: m.setattr(retriever, "RRF_K", retriever.RRF_K + 1), id="fusion"),
    pytest.param(lambda m: m.setattr(generator, "CAVEAT_PREFIX", "another caveat prefix"), id="generation"),
    pytest.param(lambda m: m.setattr(pipeline_config, "index_identity", lambda persist_directory: None), id="no index"),
])
def test_any_change_to_the_running_pipeline_is_the_incumbent_gate(world, monkeypatch, no_calls, change):
    # (not a change of GENERATION_MODEL: the judge runs on the same model, so that is the judge gate, which comes first)
    change(monkeypatch)
    assert gate_of(lambda: world.run(0)) == "incumbent"
    assert claims(world) == [] and no_calls == []


def test_slot_0b_is_held_to_the_same_incumbent(world, monkeypatch):
    assert world.run(0, skip_refusals=True).canonical is False  # a non-canonical slot 0 makes 0b available
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: "2" * 64)
    calls = []
    real = ev.run_eval_matrix
    monkeypatch.setattr(ev, "run_eval_matrix", lambda *a, **k: calls.append("run") or real(*a, **k))
    assert refusal(lambda: world.run("0b")) == ("incumbent", None)
    assert [c["slot"] for c in claims(world)] == ["0"] and calls == []  # not even the evaluator's preflight ran
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: WORLD_INDEX)
    assert world.run("0b").canonical is True


def test_a_slot_under_the_recorded_incumbent_runs_and_records_both_identities(world):
    outcome = world.run(0)
    assert outcome.canonical is True
    doc = world.stored_doc(0)
    assert doc["pipeline_config_hash"] == world.config_hash() == doc["facts"]["pipeline_config_hash"]
    assert doc["judge"] == {"config_hash": judge.judge_identity()["config_hash"], "sample": None}
    assert doc["facts"]["judge_config_hash"] == doc["judge"]["config_hash"]
    rows = json.loads((world.run_dir(doc["run_id"]) / "rows.json").read_text())
    assert rows["pipeline_config_hash"] == world.config_hash()  # the draw sidecar carries it too


def test_the_cutoff_gate_still_comes_before_the_incumbent_gate(world, monkeypatch):
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: "2" * 64)
    token = world.token(0, claim=False)
    try:
        assert refusal(lambda: sealed_run.preflight_slot(token, **world.run_args(top_k=3))) == ("cutoff", None)
    finally:
        token.release()


def test_a_confirmatory_slot_gives_the_running_hash_to_the_identity_check_which_still_refuses(world, monkeypatch):
    rewrite = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, rewrite)])
    seen = []
    monkeypatch.setattr(sealed_run, "check_candidate_identity", lambda *a, **k: seen.append((a, k)))
    token = world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False)
    try:
        seen.clear()
        sealed_run.preflight_slot(token, **world.run_args(expansion="live"))  # the recorder lets it through
    finally:
        token.release()
    assert seen and seen[-1][1]["running_config_hash"] == world.config_hash()
    assert seen[-1][0][0] == "1" and seen[-1][0][2] == PIPELINE
    # the code as shipped refuses for any input, the running hash included
    with pytest.raises(sealed_run.GateRefused) as info:
        REAL_CHECK_CANDIDATE_IDENTITY("1", world.ledger(), PIPELINE, "candidate", rewrite, running_config_hash=world.config_hash())
    assert info.value.gate == "identity"


# =============================================================================================================================
# B. The registered judge
# =============================================================================================================================
def test_the_judge_must_be_on_for_slot_0_before_the_claim(world, no_calls):
    assert refusal(lambda: world.run(0, judge=False)) == ("judge", "off")
    assert claims(world) == [] and no_calls == []


def test_the_judge_must_be_on_for_slot_0b(world, monkeypatch):
    assert world.run(0, skip_refusals=True).canonical is False
    calls = []
    real = ev.run_eval_matrix
    monkeypatch.setattr(ev, "run_eval_matrix", lambda *a, **k: calls.append("run") or real(*a, **k))
    assert refusal(lambda: world.run("0b", judge=False)) == ("judge", "off")
    assert [c["slot"] for c in claims(world)] == ["0"] and calls == []


def test_the_judge_must_be_on_for_a_confirmatory_slot_before_the_claim(world, monkeypatch):
    rewrite = rewrite_identity()["config_hash"]
    world.run(0)
    r0 = world.ledger().recover(0).run_id
    world.declare_batch(entries=[candidate_entry(PIPELINE, rewrite)])
    token = world.token(1, expansion="live", rewrite_candidate=rewrite, baseline_run=r0, claim=False)
    try:
        assert refusal(lambda: sealed_run.preflight_slot(token, **world.run_args(judge=False, expansion="live"))) == ("judge", "off")
        assert refusal(lambda: sealed_run.preflight_slot(token, **world.run_args(judge=None, expansion="live"))) == ("judge", "off")
    finally:
        token.release()
    assert [c["slot"] for c in claims(world)] == ["0"]


@pytest.mark.parametrize("judge_flag", [False, None, 0, 1, "yes"])
def test_only_exactly_true_counts_as_the_judge_on(world, judge_flag):
    token = world.token(0, claim=False)
    try:
        assert refusal(lambda: sealed_run.preflight_slot(token, **world.run_args(judge=judge_flag))) == ("judge", "off")
        # the evaluator's own re-check says the same (defence in depth: a caller that did not say is not a caller that checked)
        assert refusal(lambda: sealed_run.check_token_for_run(
            token, set_paths=[str(world.sealed_path)], expansion_mode="live", artifact_path=None, meter=None, top_k=6,
            judge=judge_flag)) == ("judge", "off")
    finally:
        token.release()


@pytest.mark.parametrize("sample", [0, 1, 5])
def test_a_judge_sample_is_refused_before_the_claim(world, sample, no_calls):
    assert refusal(lambda: world.run(0, judge_sample=sample)) == ("judge", "sample")
    assert claims(world) == [] and no_calls == []


def test_a_judge_that_is_not_the_registered_one_is_refused_before_the_claim(world, monkeypatch, no_calls):
    for name, value in (("JUDGE_PROMPT_VERSION", "faithfulness-judge-v2"), ("JUDGE_MODEL", "another-model")):
        with pytest.MonkeyPatch.context() as m:
            m.setattr(judge, name, value)
            assert refusal(lambda: world.run(0)) == ("judge", "identity")
    assert claims(world) == [] and no_calls == []


def test_a_timeout_or_retry_change_keeps_the_judge_identity_and_the_slot_runs(world, monkeypatch):
    original = generator.generation_llm_kwargs
    monkeypatch.setattr(generator, "generation_llm_kwargs", lambda: {**original(), "default_request_timeout": 9.0, "max_retries": 0})
    assert world.run(0).canonical is True


def test_the_slot_gate_needs_the_registered_judge_in_the_prereg(world):
    for judge_block in (None, {}, {"config_hash": "not-hex"}, {"config_hash": None}):
        doc = json.loads(world.prereg_path.read_text())
        doc.pop("judge", None)
        if judge_block is not None:
            doc["judge"] = judge_block
        world.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
        assert gate_of(lambda: world.token(0, claim=False)) == "prereg"
    assert "slot_claim" not in kinds(world)


def test_a_prereg_registering_another_judge_identity_refuses_the_slot(world):
    doc = json.loads(world.prereg_path.read_text())
    doc["judge"]["config_hash"] = "0" * 64
    world.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
    assert refusal(lambda: world.run(0)) == ("judge", "identity")
    assert claims(world) == []


# --- the CLI refuses before it touches the ledger ----------------------------------------------------------------------------
def _cli(monkeypatch, capsys, world, *extra):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    argv = [
        "--golden", str(world.golden_path), "--heldout", str(world.sealed_path),
        "--realistic", str(world.realistic_path), "--sealed-slot", "0", "--prereg", str(world.prereg_path),
        "--slot-records", str(world.records_dir), *extra,
    ]
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize("extra,detail", [((), "off"), (("--judge", "--judge-sample", "3"), "sample")])
def test_the_cli_refuses_a_sealed_slot_without_the_full_judge_before_touching_the_ledger(
    world, monkeypatch, capsys, extra, detail,
):
    def boom(*args, **kwargs):
        raise AssertionError("the ledger or the gate was reached")

    before = world.ledger().records()
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(sealed_run, "default_ledger", boom)
        patched.setattr(sealed_run, "acquire_slot_token", boom)
        patched.setattr(sealed_run, "run_slot", boom)
        code, out, err = _cli(monkeypatch, capsys, world, *extra)
    assert code == 4 and f"gate=judge ({detail})" in err
    assert world.ledger().records() == before


# =============================================================================================================================
# C. The claim boundary
# =============================================================================================================================
def test_claim_without_a_receipt_is_refused_and_the_ledger_is_untouched(world):
    token = world.token(0, claim=False)
    try:
        for bad in (None, "receipt", object(), {"run_id": token.run_id}):
            assert refusal(lambda: token.claim(bad)) == ("claim", "preflight")
        assert refusal(lambda: token.claim()) == ("claim", "preflight")
        assert token.claimed == [False] and token.claimed_args == [None] and claims(world) == []
    finally:
        token.release()


def test_a_receipt_of_another_run_is_refused(world):
    first = world.token(0, claim=False)
    foreign = sealed_run.preflight_slot(first, **world.run_args())
    first.release()
    second = world.token(0, claim=False)  # another token (another run id) for the still free slot
    try:
        assert second.run_id != first.run_id
        assert refusal(lambda: second.claim(foreign)) == ("claim", "preflight")
        assert claims(world) == [] and second.claimed == [False]
        second.claim(sealed_run.preflight_slot(second, **world.run_args()))  # its own receipt claims
        assert [c["slot"] for c in claims(world)] == ["0"]
    finally:
        second.release()


def test_a_receipt_cannot_be_forged(world):
    token = world.token(0, claim=False)
    try:
        receipt = sealed_run.preflight_slot(token, **world.run_args())
        assert isinstance(receipt, sealed_run.PreflightReceipt) and receipt.run_id == token.run_id
        for mint in (None, object(), "mint"):
            with pytest.raises(TypeError):
                sealed_run.PreflightReceipt(run_id=token.run_id, args_digest=receipt.args_digest, mint=mint)
        with pytest.raises(TypeError):
            sealed_run.PreflightReceipt(token.run_id, receipt.args_digest)
        with pytest.raises(Exception):  # frozen
            receipt.args_digest = "0" * 64  # type: ignore[misc]
        assert claims(world) == []
    finally:
        token.release()


def test_a_refused_preflight_gives_no_receipt_and_so_no_claim(world):
    token = world.token(0, claim=False)
    try:
        with pytest.raises(sealed_run.GateRefused):
            sealed_run.preflight_slot(token, **world.run_args(judge=False))
        with pytest.raises(sealed_run.GateRefused):
            sealed_run.preflight_slot(token, **world.run_args(top_k=3))
        assert claims(world) == [] and token.claimed == [False]
    finally:
        token.release()


def test_the_digest_covers_the_arguments_and_ignores_nothing_that_changes_the_run(world):
    token = world.token(0, claim=False)
    try:
        digest = lambda **kw: sealed_run.preflight_slot(token, **world.run_args(**kw)).args_digest  # noqa: E731
        base = digest()
        assert base == digest()
        assert len({base, digest(modes=["hybrid"]), digest(skip_refusals=True), digest(skip_completeness=True),
                    digest(specs=[world.specs[1], world.specs[0], world.specs[2]]),
                    digest(matrix_extra={"judge_dump_path": None})}) == 6
    finally:
        token.release()


def test_run_slot_always_preflights_even_a_token_that_was_claimed_before(world, monkeypatch):
    events = []
    real = sealed_run.preflight_slot
    monkeypatch.setattr(sealed_run, "preflight_slot", lambda *a, **k: (events.append("preflight"), real(*a, **k))[1])
    token = world.token(0)  # preflighted (once) and claimed, as the helper does
    assert events == ["preflight"] and token.claimed == [True] and token.claimed_args[0]
    world.run(0, token=token)
    assert events == ["preflight", "preflight"]  # run_slot preflighted again before running


def test_a_claimed_token_run_with_other_arguments_is_refused_and_the_same_arguments_run(world):
    token = world.token(0)  # claimed under the world's default arguments
    assert refusal(lambda: world.run(0, token=token, modes=["hybrid"])) == ("claim", "preflight_mismatch")
    assert kinds(world).count("slot_claim") == 1 and "slot_stored" not in kinds(world)  # the claim stands: spent
    assert world.ledger().recover(0).action is RecoveryAction.SPENT


def test_a_claimed_token_run_with_the_same_arguments_runs(world):
    token = world.token(0)
    outcome = world.run(0, token=token)
    assert outcome.canonical is True and "slot_stored" in kinds(world)


@pytest.mark.parametrize("other", [
    pytest.param(lambda w: {"skip_refusals": True}, id="skip refusals"),
    pytest.param(lambda w: {"skip_completeness": True}, id="skip completeness"),
    pytest.param(lambda w: {"specs": [w.specs[1], w.specs[0], w.specs[2]]}, id="set order"),
    pytest.param(lambda w: {"matrix_extra": {"judge_dump_path": None}}, id="seams"),
    pytest.param(lambda w: {"expansion": "build:somewhere"}, id="expansion"),
])
def test_any_other_argument_than_the_preflighted_ones_is_a_mismatch_or_a_refusal(world, other):
    token = world.token(0)
    with pytest.raises(sealed_run.GateRefused) as info:
        world.run(0, token=token, **other(world))
    assert info.value.gate in ("claim", "expansion_mode")
    if info.value.gate == "claim":
        assert info.value.detail == "preflight_mismatch"
    assert "slot_stored" not in kinds(world)


def test_acquire_slot_token_still_refuses_an_eager_claim(world):
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.acquire_slot_token(
            0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
            records_dir=world.records_dir, expansion="live", meter=None, claim=True, top_k=6)
    assert (info.value.gate, info.value.detail) == ("claim", "eager") and claims(world) == []


def test_no_production_code_claims_a_slot_outside_run_slot():
    """AST hygiene: a ``.claim(`` call in ``src/`` or ``scripts/`` is legal only in ``run_slot`` (the token's claim, with
    its receipt) and in ``SlotToken.claim`` itself (the ledger's claim): nothing else can spend a slot."""
    found = []
    for path in sorted([*ROOT.glob("src/*.py"), *ROOT.glob("scripts/*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "claim":
                scope, cur = [], node
                while cur in parents:
                    cur = parents[cur]
                    if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        scope.append(cur.name)
                found.append((path.relative_to(ROOT).as_posix(), tuple(reversed(scope))))
    assert sorted(found) == [
        ("src/sealed_run.py", ("SlotToken", "claim")),
        ("src/sealed_run.py", ("run_slot",)),
    ], found
