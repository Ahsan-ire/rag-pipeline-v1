"""Phase 16A-2a a5: the slot-gated sealed run (D73), acceptance (b), (c), (f) and (g).

Synthetic, schema-valid fixtures only (``tests/p16_slot.py``); every model is
faked at the evaluator's seams; a socket spy proves nothing here reaches a
network. The first test runs the whole success path (a canonical slot 0), so
every later refusal or fault is judged against a run that works.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import src.evaluator as ev
from src import eval_schema, sealed_run, tracing_guard
from src.eval_privacy import SealedInputError
from src.expansion_artifact import load_artifact, rewrite_identity
from src.sealed_ledger import RecoveryAction, exclusions_digest
from tests.p16_capture import PROVENANCE
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    HELD,
    TRACING_VARS,
    _langsmith_env_cache,
    bare,
    gate_of,
    isolate_env,
    kinds,
    no_calls,
    sha_of,
    socket_spy,
    world,
)
from tests.test_p16w_spend import make_fakes, make_meter

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The success path
# ---------------------------------------------------------------------------


def test_a_canonical_slot_zero_runs_end_to_end(world, capsys):
    outcome = world.run(0)
    assert outcome.canonical and outcome.published and outcome.failed_guards == ()
    assert outcome.action == "render_from_storage"
    text = sealed_run.results_v6_path().read_text()
    assert "sealed slot 0; canonical" in text and "Canonical run: True" in text
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored", "publication_intent", "slot_consumed"]
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED
    out = capsys.readouterr().out
    assert "canonical; published results_v6.md" in out


# ---------------------------------------------------------------------------
# (b) Without a token every entry still refuses sealed input
# ---------------------------------------------------------------------------


def test_without_a_token_every_run_retrieval_generation_and_replay_entry_refuses(world, monkeypatch):
    calls = []
    monkeypatch.setattr(ev, "expand_query", lambda *a, **k: calls.append("expand"))
    retrieval = world.retrieval()

    def factory(mode):
        calls.append("retrieval")
        return retrieval.factory(6)(mode)

    kw = dict(
        privacy="private", retrieve_fn_factory=factory, provenance_fn=lambda: dict(PROVENANCE),
        generate_fn=lambda q: calls.append("generate"), judge_fn=lambda v: calls.append("judge"),
    )
    sealed = str(world.sealed_path)
    artifact = world.private / "sealed" / "artifacts" / "frozen_expansions.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}")
    doors = [
        lambda: ev.run_eval_matrix([(HELD, sealed)], **kw),
        lambda: ev.run_eval_matrix([(HELD, sealed)], slot_token=None, **kw),
        lambda: ev.run_eval_matrix([("tuning", str(world.golden_path))], expansion=str(artifact), **kw),
        lambda: ev.run_eval(sealed, privacy="private"),
        lambda: ev.load_golden_set(sealed),
        lambda: eval_schema.load_any(sealed),
        lambda: eval_schema.load_v2(sealed),
        lambda: eval_schema.detect_schema(sealed),
        lambda: load_artifact(str(artifact)),
        lambda: load_artifact(str(artifact), slot_token=None),
    ]
    for door in doors:
        with pytest.raises(SealedInputError):
            door()
    assert calls == []


def test_a_forged_token_is_refused_and_the_cli_still_exits_4_without_one(world, monkeypatch, capsys):
    with pytest.raises(TypeError):
        sealed_run.SlotToken(**{f: None for f in sealed_run.SlotToken.__dataclass_fields__})
    calls = []
    monkeypatch.setattr(ev, "expand_query", lambda *a, **k: calls.append("expand"))

    class Forged:
        def approves(self, path):
            return True

    with pytest.raises(sealed_run.GateRefused) as info:
        ev.run_eval_matrix(world.specs, privacy="private", slot_token=Forged(), meter=make_meter())
    assert info.value.gate == "slot" and calls == []
    from tests.test_p16_cli import _run

    code, captured = _run(monkeypatch, ["--heldout", str(world.sealed_path), "--skip-refusals", "--skip-completeness"])
    assert code == 4 and captured == {}


# ---------------------------------------------------------------------------
# (b) The gate: every refusal comes before any call and leaves the slot unclaimed
# ---------------------------------------------------------------------------


def unclaimed(world):
    return all(r["kind"] != "slot_claim" for r in world.ledger().records())


def test_a_valid_gate_claims_exactly_once_and_mints_a_token(world):
    token = world.token(0)
    assert isinstance(token, sealed_run.SlotToken) and token.slot == "0"
    assert kinds(world) == ["freeze", "slot_claim"]
    assert "frozen" not in repr(token) and world.canaries.questions[0] not in repr(token)
    assert token.approves(world.sealed_path) and not token.approves(world.golden_path)
    assert gate_of(lambda: world.token(0)) == "claim"  # a re-claimed slot
    assert kinds(world).count("slot_claim") == 1


def test_no_freeze_record_refuses(bare, no_calls):
    bare.write_prereg()
    bare.write_records()
    assert gate_of(lambda: bare.token(0)) == "freeze_record"
    assert unclaimed(bare) and no_calls == []


def test_a_sealed_file_off_its_record_refuses(world, no_calls):
    with world.sealed_path.open("a") as fh:
        fh.write("\n")
    assert gate_of(lambda: world.token(0)) == "set_file"
    assert unclaimed(world) and no_calls == []


def twins_payload_copies(world):
    """Both stored copies of the freeze-bound group record: next to the out-of-tree ledger, and the private copy."""
    return [Path(os.environ["CC_SEALED_LEDGER"]).parent / "sealed_payloads" / "twins_sealed.json",
            world.private / "sealed" / "ledger" / "payloads" / "twins_sealed.json"]


def test_a_twins_record_off_its_record_refuses(world, no_calls):
    copies = twins_payload_copies(world)
    assert all(p.is_file() for p in copies)  # the record is in the ledger, not in the records directory
    assert not (world.records_dir / "twins.json").exists()
    freeze = world.ledger().freeze_record("sealed")
    copies[0].write_text(json.dumps({"edges": []}))
    assert sealed_run.load_group_edges(world.ledger(), freeze)  # one intact copy still verifies against the freeze record
    copies[1].write_text(json.dumps({"edges": []}))  # now neither copy matches its digest
    assert gate_of(lambda: world.token(0)) == "twins_record"
    assert unclaimed(world) and no_calls == []


def test_a_freeze_cannot_bind_an_exclusion_list_the_ledger_never_held(bare):
    """record_freeze computes the digest inside its transaction and refuses any other (A9)."""
    from src.sealed_ledger import ProtocolError

    bare.write_prereg()
    bare.write_records()
    with pytest.raises(ProtocolError):
        bare.freeze(exclusions_sha256=exclusions_digest([bare.families[4]]))
    assert bare.ledger().freeze_record("sealed") is None


def payload_files(world, name):
    return [Path(os.environ["CC_SEALED_LEDGER"]).parent / "sealed_payloads" / f"{name}.json",
            world.private / "sealed" / "ledger" / "payloads" / f"{name}.json"]


def test_the_group_record_is_loaded_by_its_content_name_with_the_plain_alias_as_the_fallback(bare):
    """sealed_tool stores ``twins_sealed_<sha16>`` (the freeze record's own twins_sha256) and keeps the plain
    ``twins_sealed`` alias for now: the gate loads by the content name and falls back to the alias."""
    bare.write_prereg()
    bare.write_records()
    bare.freeze(twins_names=("twins_sealed_{sha16}", "twins_sealed"))
    freeze = bare.ledger().freeze_record("sealed")
    content = f"twins_sealed_{freeze['twins_sha256'][:16]}"
    both = len(sealed_run.load_group_edges(bare.ledger(), freeze))
    for path in payload_files(bare, "twins_sealed"):
        path.unlink()  # the alias is gone: the content name alone serves
    assert len(sealed_run.load_group_edges(bare.ledger(), freeze)) == both == 2
    for path in payload_files(bare, content):
        path.unlink()
    assert gate_of(lambda: bare.token(0)) == "twins_record"  # neither name verifies any more


def test_a_freeze_that_stored_only_the_content_named_group_record_still_passes_the_gate(bare):
    bare.write_prereg()
    bare.write_records()
    bare.freeze(twins_names=("twins_sealed_{sha16}",))
    assert not any(p.exists() for p in payload_files(bare, "twins_sealed"))
    assert bare.token(0).slot == "0"


def test_a_tampered_content_named_group_record_falls_back_to_the_alias_and_both_must_fail_to_refuse(bare):
    bare.write_prereg()
    bare.write_records()
    bare.freeze(twins_names=("twins_sealed_{sha16}", "twins_sealed"))
    freeze = bare.ledger().freeze_record("sealed")
    for path in payload_files(bare, f"twins_sealed_{freeze['twins_sha256'][:16]}"):
        path.write_text(json.dumps({"edges": []}))
    assert sealed_run.load_group_edges(bare.ledger(), freeze)  # the intact alias verifies
    for path in payload_files(bare, "twins_sealed"):
        path.write_text(json.dumps({"edges": []}))
    assert gate_of(lambda: bare.token(0)) == "twins_record"


def test_an_exclusion_list_off_its_record_refuses(bare, no_calls):
    """Defence in depth: were a freeze record ever to bind another list, the gate would still refuse."""
    from src import sealed_ledger

    bare.write_prereg()
    bare.write_records()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sealed_ledger, "exclusions_digest", lambda groups: exclusions_digest([bare.families[4]]))
        bare.freeze()  # the freeze record now binds a list the ledger never held
    assert gate_of(lambda: bare.token(0)) == "exclusion_list"
    assert unclaimed(bare) and no_calls == []


def test_an_exclusion_recorded_before_the_freeze_is_bound_and_after_it_is_simply_added(bare):
    bare.write_prereg()
    bare.write_records(exclusions=[bare.families[4]])
    bare.freeze()
    assert bare.token(0).excluded == {bare.families[4]}  # the freeze bound it
    bare.ledger().exclude(bare.families[5], "s_hit")  # a later exclusion does not unbind the freeze record
    assert bare.token("0b").excluded == {bare.families[4], bare.families[5]}


def test_an_unbound_sealed_row_refuses(bare, no_calls):
    bare.write_prereg()
    bare.write_records()
    path = bare.records_dir / "approvals.json"
    approved = json.loads(path.read_text())
    approved["approved"].pop(next(iter(approved["approved"])))
    path.write_text(json.dumps(approved))
    bare.freeze()
    assert gate_of(lambda: bare.token(0)) == "approvals"
    assert unclaimed(bare) and no_calls == []


@pytest.mark.parametrize("content", [
    "not json", json.dumps({"version": 2}), json.dumps({"version": 1, "approved": {"x": "y"}}),
    json.dumps({"version": 1, "approved": {"x": {"row_hash": "short", "sheet_sha256": "a" * 64}}}),
    json.dumps({"version": 1, "approved": {"x": {"row_hash": "a" * 64}}}),
    json.dumps({"version": 1, "approved": ["x"]}),
])
def test_a_malformed_approvals_file_refuses(bare, content):
    bare.write_prereg()
    bare.write_records()
    (bare.records_dir / "approvals.json").write_text(content)
    bare.freeze()
    assert gate_of(lambda: bare.token(0)) == "approvals"


def test_a_row_edited_after_approval_changes_its_hash_and_is_unbound(bare):
    """An approval binds the content: edit a phrasing and the (re-frozen) set no longer matches its approvals."""
    bare.write_prereg()
    bare.write_records()
    rows = [dict(r) for r in bare.sealed_rows]
    rows[0]["question"] = bare.canaries.question("edited")
    bare.sealed_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    bare.freeze()  # the freeze record matches the edited file; the approvals do not
    assert gate_of(lambda: bare.token(0)) == "approvals"


@pytest.mark.parametrize("twins", [{"edges": "no"}, {"edges": [["only-one"]]}, {"batches": {}}, ["edges"]])
def test_a_malformed_twins_record_refuses(bare, twins):
    bare.write_prereg()
    bare.write_records(twins=twins)
    bare.freeze()
    assert gate_of(lambda: bare.token(0)) == "groups"


def test_an_exclusion_naming_no_family_of_the_sealed_set_excludes_nothing_and_does_not_refuse(bare):
    """A stale id is no longer a permanent refusal (A9): none of its families is in the frozen set."""
    bare.write_prereg()
    bare.write_records(exclusions=["f99999999"])
    bare.freeze()
    assert bare.token(0).excluded == frozenset()


@pytest.mark.parametrize("slots", [{}, {"1": {"expansion": "live"}}])
def test_a_prereg_silent_about_the_slot_refuses(bare, slots):
    bare.write_prereg(slots)
    bare.write_records()
    bare.freeze()
    assert gate_of(lambda: bare.token(0)) == "prereg"


@pytest.mark.parametrize("doc", ["not json", {"version": 2}, {"version": 1, "slots": {}}, {"version": 1, "floor": {"families": 1}, "slots": {}},
                                  {"version": 1, "floor": {"families": 1, "eligible": 1}, "slots": {"0": {"expansion": "weird"}}},
                                  {"version": 1, "floor": {"families": 1, "eligible": 1}, "slots": {"1": {"expansion": "replay"}}}])
def test_a_malformed_prereg_refuses(world, doc):
    world.prereg_path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    assert gate_of(lambda: world.token(0)) == "prereg"
    assert unclaimed(world)


@pytest.mark.parametrize("slot", ["x", "-1", 1.5, True, "01", ""])
def test_a_bad_slot_id_refuses(world, slot):
    assert gate_of(lambda: world.token(slot)) == "slot"


def test_no_meter_or_a_stand_in_meter_refuses(world, no_calls):
    assert gate_of(lambda: world.token(0, meter=None)) == "meter"

    class StandIn:
        run_total_eur = week_total_eur = 0.0

    assert gate_of(lambda: world.token(0, meter=StandIn())) == "meter"
    assert unclaimed(world) and no_calls == []


def test_the_floor_ends_the_epoch_before_any_claim(world):
    world.write_prereg(floor=(9, 4))
    assert gate_of(lambda: world.token(0)) == "floor"
    world.write_prereg(floor=(6, 7))
    assert gate_of(lambda: world.token(0)) == "floor"
    world.write_prereg(floor=(6, 4))
    ledger = world.ledger()
    ledger.exclude(world.families[0], "owner")  # a 2-family group with 2 eligible families: 6 sealed, 4 eligible
    assert world.token(0).excluded == {world.families[0]}
    ledger = world.ledger()
    ledger.exclude(world.families[2], "owner")  # now 4 sealed, 2 eligible: below both floors
    assert gate_of(lambda: world.token("0b")) == "floor"
    assert all(r["kind"] != "slot_claim" or r["slot"] == "0" for r in world.ledger().records())


def test_slot_zero_b_is_available_only_after_a_non_canonical_slot_zero(world):
    assert gate_of(lambda: world.token("0b")) == "claim"  # slot 0 has not ended
    assert unclaimed(world)


# --- the expansion mode must be the one the prereg declares for the slot ---------


def test_the_expansion_mode_must_equal_the_declared_one(bare, tmp_path, no_calls):
    config_hash = rewrite_identity()["config_hash"]
    artifact = tmp_path / "declared.json"
    artifact.write_text("{}")
    bare.write_prereg({
        "0": {"expansion": "live"},
        "1": {"expansion": "replay", "artifact_sha256": sha_of(artifact)},
        "2": {"expansion": "candidate", "rewrite_candidate": config_hash},
        "3": {"expansion": "candidate", "rewrite_candidate": "b" * 64},
    })
    bare.write_records()
    bare.freeze()
    # live declared: a candidate flag, a replay path or a build target are all wrong modes
    assert gate_of(lambda: bare.token(0, rewrite_candidate=config_hash)) == "expansion_mode"
    assert gate_of(lambda: bare.token(0, expansion=str(artifact))) == "expansion_mode"
    assert gate_of(lambda: bare.token(0, expansion="build:x.json")) == "expansion_mode"
    # replay declared: live is wrong, a different artifact is wrong
    other = tmp_path / "other.json"
    other.write_text("{ }")
    assert gate_of(lambda: bare.token(1, expansion="live", baseline_run="r1")) == "expansion_mode"
    assert gate_of(lambda: bare.token(1, expansion=str(other), baseline_run="r1")) == "expansion_mode"
    assert gate_of(lambda: bare.token(1, expansion=str(tmp_path / "missing.json"), baseline_run="r1")) == "expansion_mode"
    # candidate declared: live without the flag is wrong; the wrong hash is wrong; a declared hash the code does not have is wrong
    assert gate_of(lambda: bare.token(2, expansion="live", baseline_run="r1")) == "expansion_mode"
    assert gate_of(lambda: bare.token(2, rewrite_candidate="c" * 64, baseline_run="r1")) == "expansion_mode"
    assert gate_of(lambda: bare.token(3, rewrite_candidate="b" * 64, baseline_run="r1")) == "expansion_mode"
    assert unclaimed(bare) and no_calls == []


# --- the tracing gate -----------------------------------------------------------


def test_tracing_variables_are_scrubbed_to_false():
    env = {name: "true" for name in tracing_guard.TRACING_VARS}
    env["UNRELATED"] = "kept"
    tracing_guard.scrub_tracing_env(env)
    assert {env[name] for name in tracing_guard.TRACING_VARS} == {"false"} and env["UNRELATED"] == "kept"
    assert tracing_guard.TRACING_VARS == TRACING_VARS


@pytest.mark.parametrize("name", TRACING_VARS)
@pytest.mark.parametrize("value", ["true", "TRUE", "1", " yes "])
def test_a_tracing_variable_left_on_refuses_the_gate(world, monkeypatch, name, value, no_calls):
    monkeypatch.setenv(name, value)
    assert "env_enabled" in tracing_guard.tracing_problems()
    assert gate_of(lambda: world.token(0)) == "tracing"
    assert unclaimed(world) and no_calls == []


@pytest.mark.parametrize("name", TRACING_VARS)
def test_scrubbing_each_variable_clears_the_refusal(world, monkeypatch, name):
    from langsmith import utils as ls_utils

    monkeypatch.setenv(name, "true")
    ls_utils.get_env_var.cache_clear()
    tracing_guard.scrub_tracing_env()
    ls_utils.get_env_var.cache_clear()
    assert tracing_guard.tracing_problems() == []
    assert isinstance(world.token(0), sealed_run.SlotToken)


def test_dotenv_cannot_switch_tracing_back_on_after_the_scrub(tmp_path, monkeypatch):
    dotenv = pytest.importorskip("dotenv")
    env_file = tmp_path / "tracing.env"
    env_file.write_text("".join(f"{name}=true\n" for name in TRACING_VARS) + "OTHER_VAR=loaded\n")
    isolate_env(monkeypatch, *TRACING_VARS, "OTHER_VAR", "PYTHON_DOTENV_DISABLED")  # the P0 capture leaves the last set
    tracing_guard.scrub_tracing_env()
    dotenv.load_dotenv(env_file)  # the call src.generator makes at import: override=False
    assert [os.environ[name] for name in TRACING_VARS] == ["false"] * 4
    assert os.environ["OTHER_VAR"] == "loaded"  # the .env file really was read


def test_a_scrub_that_comes_too_late_is_caught_by_tracing_is_enabled(world, monkeypatch):
    """langsmith caches the environment: scrubbing after it read 'true' leaves tracing_is_enabled() true."""
    from langsmith import utils as ls_utils

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    ls_utils.get_env_var.cache_clear()
    assert ls_utils.tracing_is_enabled() is True  # langsmith now holds 'true'
    tracing_guard.scrub_tracing_env()
    assert tracing_guard.tracing_problems() == ["tracing_enabled"]  # the environment says false, the cache does not
    assert gate_of(lambda: world.token(0)) == "tracing"
    ls_utils.get_env_var.cache_clear()
    assert tracing_guard.tracing_problems() == []


def test_a_global_tracer_refuses_the_gate(world):
    from unittest import mock

    from langchain_core.tracers.context import tracing_v2_enabled

    with tracing_v2_enabled(client=mock.MagicMock()):
        problems = tracing_guard.tracing_problems()
        assert "global_tracer" in problems
        assert gate_of(lambda: world.token(0)) == "tracing"
    assert tracing_guard.tracing_problems() == []
    assert unclaimed(world)


def test_a_tracer_callback_hook_refuses_the_gate(world):
    from contextvars import ContextVar

    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.tracers import context as tracer_context

    var = ContextVar("p16_test_handler", default=None)
    tracer_context.register_configure_hook(var, True)
    try:
        assert tracing_guard.tracing_problems() == []
        token = var.set(BaseCallbackHandler())
        try:
            assert "tracer_callback" in tracing_guard.tracing_problems()
            assert gate_of(lambda: world.token(0)) == "tracing"
        finally:
            var.reset(token)
    finally:
        tracer_context._configure_hooks[:] = [h for h in tracer_context._configure_hooks if h[0] is not var]
    assert tracing_guard.tracing_problems() == [] and unclaimed(world)


def test_a_tracing_context_and_the_debug_flag_refuse_the_gate(world):
    import langsmith
    from langchain_core import globals as lc_globals

    with langsmith.tracing_context(enabled=True):
        assert "tracing_enabled" in tracing_guard.tracing_problems()
        assert gate_of(lambda: world.token(0)) == "tracing"
    lc_globals.set_debug(True)
    try:
        assert tracing_guard.tracing_problems() == ["debug"]
        assert gate_of(lambda: world.token(0)) == "tracing"
    finally:
        lc_globals.set_debug(False)
    assert unclaimed(world)


def test_tracing_that_comes_on_after_the_gate_stops_the_run_before_the_first_call(world, monkeypatch, no_calls):
    """The re-check 'just before the first call': nothing is called, and the claimed slot is spent."""
    token = world.token(0)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(tracing_guard.TracingRefused) as info:
        world.run(0, token=token)
    assert "env_enabled" in info.value.reasons
    assert world.ledger().recover(0).action is RecoveryAction.SPENT
    assert no_calls == ["run"]  # the matrix was entered, and stopped before any expansion
    assert not list((world.private / "sealed" / "runs").glob("*"))


def test_the_run_proceeds_with_tracing_scrubbed_while_a_spy_transport_sees_nothing(world, monkeypatch, socket_spy):
    """Tracing set in the environment and scrubbed: fake models run through LangChain callbacks, nothing leaves."""
    for name in TRACING_VARS:
        monkeypatch.setenv(name, "true")
    tracing_guard.scrub_tracing_env()
    from langsmith import utils as ls_utils

    ls_utils.get_env_var.cache_clear()
    fakes = make_fakes()
    meter = make_meter(fakes)
    retrieval = world.retrieval()
    base = world.generator(retrieval)

    def generate(question):  # also goes through the metered generation client (LangChain callbacks)
        meter.generation_llm().invoke(question)
        return base(question)

    outcome = world.run(0, meter=meter, generate=generate)
    assert outcome.canonical
    assert fakes["generation"].sent  # the fake model really ran, under the callbacks
    assert socket_spy == []


_SPY_SCRIPT = textwrap.dedent(
    """
    import json, os, runpy, socket, sys, time
    attempts, loads = [], []
    def spy(self, address):
        attempts.append(str(address))
        raise ConnectionRefusedError("spy transport")
    socket.socket.connect = spy
    VARS = ["LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING"]
    import dotenv
    real_load = dotenv.load_dotenv
    def load(*a, **k):  # what src.generator calls at import; record what the variables held at that moment
        loads.append([os.environ.get(n) for n in VARS])
        return real_load(dotenv_path=os.environ["P16_ENV_FILE"], override=False)
    dotenv.load_dotenv = load
    mode = sys.argv[1]
    code = None
    if mode == "entry":
        sys.argv = ["pipeline", "eval", "--sealed-slot", "0"]
        try:
            runpy.run_module("src.pipeline", run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    else:
        import src.generator  # nothing scrubbed first
    from langchain_core.language_models.fake import FakeListLLM
    from langsmith import utils as ls_utils
    from src.tracing_guard import tracing_problems
    FakeListLLM(responses=["ok"]).invoke("synthetic")
    deadline = time.time() + (6 if mode == "control" else 0)
    while mode == "control" and not attempts and time.time() < deadline:
        time.sleep(0.1)
    print(json.dumps({"code": code, "env": [os.environ.get(n) for n in VARS], "first_load": loads[0] if loads else None,
                      "enabled": bool(ls_utils.tracing_is_enabled()), "problems": tracing_problems(),
                      "attempts": attempts}), flush=True)
    os._exit(0)
    """
)


def _spy_run(tmp_path, mode, *, channel):
    """Run the spy script in a fresh interpreter. ``channel``: the four variables are 'true' in the
    process ``environment``, in a ``dotenv`` file only, or ``both``."""
    env_file = tmp_path / "dotenv.env"
    in_dotenv = channel in ("dotenv", "both")
    env_file.write_text(
        ("".join(f"{name}=true\n" for name in TRACING_VARS) if in_dotenv else "") + "LANGSMITH_API_KEY=fake-key\n"
    )
    script = tmp_path / "spy_script.py"
    script.write_text(_SPY_SCRIPT)
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(ROOT),
        "P16_ENV_FILE": str(env_file), "LANGSMITH_API_KEY": "fake-key", "LANGSMITH_ENDPOINT": "http://127.0.0.1:9",
    }
    if channel in ("environment", "both"):
        env.update({name: "true" for name in TRACING_VARS})
    proc = subprocess.run([sys.executable, str(script), mode], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_cli_entry_scrubs_before_src_loads_dotenv_and_the_spy_sees_nothing(tmp_path):
    """python -m src.pipeline: each of the four variables, set in the environment AND in .env, ends 'false'."""
    control = _spy_run(tmp_path, "control", channel="both")
    assert control["enabled"] is True and control["attempts"], "the spy must see uploads when tracing is on"
    assert control["first_load"] == ["true"] * 4  # without the scrub, src.generator loads .env with tracing on
    entry = _spy_run(tmp_path, "entry", channel="both")
    assert entry["code"] == 2  # --sealed-slot without --prereg: a usage refusal, after the scrub
    assert entry["first_load"] == ["false"] * 4  # the scrub ran BEFORE src.generator's load_dotenv()
    assert entry["env"] == ["false"] * 4  # and load_dotenv (override=False) could not switch anything back on
    assert entry["enabled"] is False and entry["problems"] == [] and entry["attempts"] == []


def test_dotenv_alone_is_neutralised_the_same_way(tmp_path):
    entry = _spy_run(tmp_path, "entry", channel="dotenv")
    assert entry["first_load"] == ["false"] * 4 and entry["env"] == ["false"] * 4 and entry["attempts"] == []


# --- more gate corners: the sealed file's own validity, the group map -------------------------


def test_a_frozen_file_that_does_not_validate_or_is_not_sealed_refuses(bare):
    bare.write_prereg()
    bare.write_records()
    rows = [dict(r) for r in bare.sealed_rows]
    rows[0]["scope"] = "nonsense"  # a schema error, in a file the freeze record matches
    bare.sealed_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    bare.freeze()
    assert gate_of(lambda: bare.token(0)) == "set_file"
    assert unclaimed(bare)


def test_a_set_outside_the_sealed_directory_without_a_marker_is_not_a_sealed_set(bare, tmp_path):
    other = tmp_path / "elsewhere" / "frozen.jsonl"
    other.parent.mkdir()
    other.write_text(bare.sealed_path.read_text())
    bare.write_prereg()
    bare.write_records()
    bare.freeze(set_path=other)
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.acquire_slot_token(
            0, ledger=bare.ledger(), set_path=other, prereg_path=bare.prereg_path, records_dir=bare.records_dir,
            expansion="live", meter=make_meter(),
        )
    assert info.value.gate == "set_file" and unclaimed(bare)


def test_groups_are_the_connected_components_of_the_twins_edges_named_by_their_smallest_sealed_family():
    groups = sealed_run.compute_groups(["fc", "fa", "fb", "fd", "fe"], [("fb", "fc"), ("fa", "fb"), ("fb", "fb")])
    assert groups == {"fa": "fa", "fb": "fa", "fc": "fa", "fd": "fd", "fe": "fe"}
    # a draft outside the sealed set (a dropped twin) still joins the families it was tied to
    bridged = sealed_run.compute_groups(["fa", "fb", "fz"], [("fa", "fx"), ("fx", "fb")])
    assert bridged == {"fa": "fa", "fb": "fa", "fz": "fz"}
    assert sealed_run.compute_groups([], [("fa", "fb")]) == {}
