"""Phase 16A-2a gate fixes A6 and A8: what else carries a sealed prompt out of the process, and the CLI's `.env` path.

A6: the tracing guard also refuses on callbacks inherited through ``var_child_runnable_config``
(every chain the run invokes would inherit them) and on a global LLM cache (a chat model looks up and
stores serialised prompts and answers in it). Each case is shown with a recording spy to be a REAL
way out for sealed text first (the control), and then to be refused.
A8: a tracing variable that exists only in ``.env`` is scrubbed by the CLI entry and the slot run
proceeds with nothing leaving the process.
"""

import os
from unittest import mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
from langchain_core.caches import InMemoryCache
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables.config import set_config_context

import src.evaluator as ev
from src import sealed_run, tracing_guard
from tests.p16_capture import PROVENANCE
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    TRACING_VARS,
    _langsmith_env_cache,
    bare,
    gate_of,
    isolate_env,
    kinds,
    no_calls,
    socket_spy,
    world,
)
from tests.test_p16w_slot_run_cli import cli

PROMPT = "P16-CANARY-q-sealed-prompt?"


class Spy(BaseCallbackHandler):
    """A callback handler that records every prompt it is shown (what a tracer or console handler would send)."""

    def __init__(self):
        self.prompts = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.prompts.append(str(messages))

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.prompts.append(str(prompts))


class RecordingCache(InMemoryCache):
    """A global LLM cache that records what it is asked to look up and to store."""

    def __init__(self):
        super().__init__()
        self.lookups, self.updates = [], []

    def lookup(self, prompt, llm_string):
        self.lookups.append(prompt)
        return super().lookup(prompt, llm_string)

    def update(self, prompt, llm_string, return_val):
        self.updates.append(prompt)
        return super().update(prompt, llm_string, return_val)


def unclaimed(world):
    return all(r["kind"] != "slot_claim" for r in world.ledger().records())


# --- inherited callbacks -----------------------------------------------------------------------------------------------


def _inheriting(config, check):
    """Run ``check`` inside a context that carries ``config`` as the inherited runnable config."""
    with set_config_context(config) as ctx:
        return ctx.run(check)


@pytest.mark.parametrize(
    "make_callbacks",
    [
        pytest.param(lambda spy: [spy], id="list-of-handlers"),
        pytest.param(lambda spy: CallbackManager([spy], inheritable_handlers=[spy]), id="callback-manager"),
        pytest.param(
            lambda spy: [__import__("langchain_core.tracers.langchain", fromlist=["x"]).LangChainTracer(client=mock.MagicMock())],
            id="langchain-tracer",
        ),
    ],
)
def test_callbacks_inherited_through_the_runnable_config_refuse_the_gate(world, make_callbacks):
    spy = Spy()
    config = {"callbacks": make_callbacks(spy)}

    def inside():
        # control: a chain invoked here inherits the handler, so a sealed prompt would reach it
        if isinstance(config["callbacks"], list) and config["callbacks"][0] is spy:
            FakeListChatModel(responses=["ok"]).invoke(PROMPT)
            assert any(PROMPT in p for p in spy.prompts), "the control: an inherited handler sees the prompt"
        assert tracing_guard.tracing_problems() == ["tracer_callback"]
        assert gate_of(lambda: world.token(0)) == "tracing"
        with pytest.raises(tracing_guard.TracingRefused) as info:
            tracing_guard.assert_tracing_off()
        assert info.value.reasons == ["tracer_callback"]

    _inheriting(config, inside)
    assert tracing_guard.tracing_problems() == [] and unclaimed(world)


@pytest.mark.parametrize("callbacks", [None, [], CallbackManager([])])
def test_an_empty_inherited_config_is_not_a_problem(callbacks):
    assert _inheriting({"callbacks": callbacks}, tracing_guard.tracing_problems) == []
    assert _inheriting({}, tracing_guard.tracing_problems) == []


def test_inherited_callbacks_that_appear_after_the_gate_stop_the_run_before_the_first_call(world, no_calls):
    token = world.token(0)
    spy = Spy()

    def run_inside():
        with pytest.raises(tracing_guard.TracingRefused):
            world.run(0, token=token)

    _inheriting({"callbacks": [spy]}, run_inside)
    assert spy.prompts == [] and "expand" not in no_calls
    assert world.ledger().recover(0).action.value == "spent"


# --- a global LLM cache -------------------------------------------------------------------------------------------------


def test_a_global_llm_cache_refuses_the_gate(world):
    cache = RecordingCache()
    set_llm_cache(cache)
    try:
        FakeListChatModel(responses=["ok"]).invoke(PROMPT)
        assert cache.lookups and cache.updates, "the control: a configured cache is consulted with the prompt"
        assert tracing_guard.tracing_problems() == ["llm_cache"]
        assert gate_of(lambda: world.token(0)) == "tracing"
        with pytest.raises(tracing_guard.TracingRefused) as info:
            tracing_guard.assert_tracing_off()
        assert info.value.reasons == ["llm_cache"]
    finally:
        set_llm_cache(None)
    assert get_llm_cache() is None and tracing_guard.tracing_problems() == [] and unclaimed(world)


def test_a_cache_configured_after_the_gate_stops_the_run_before_the_first_call(world, no_calls):
    token = world.token(0)
    cache = RecordingCache()
    set_llm_cache(cache)
    try:
        with pytest.raises(tracing_guard.TracingRefused):
            world.run(0, token=token)
    finally:
        set_llm_cache(None)
    assert cache.lookups == [] and cache.updates == [] and "expand" not in no_calls
    assert world.ledger().recover(0).action.value == "spent"


def test_the_guard_reports_every_reason_it_finds_in_a_fixed_order():
    spy = Spy()
    set_llm_cache(RecordingCache())
    try:
        reasons = _inheriting({"callbacks": [spy]}, tracing_guard.tracing_problems)
    finally:
        set_llm_cache(None)
    assert reasons == ["tracer_callback", "llm_cache"]
    assert os.environ.get("LANGCHAIN_TRACING_V2") != "true"


# --- A8: a tracing variable that exists only in .env -----------------------------------------------------------------


def test_a_tracing_variable_only_in_dotenv_is_scrubbed_by_the_entry_and_the_cli_slot_run_proceeds(
    world, tmp_path, monkeypatch, capsys, socket_spy
):
    """The CLI entry scrubs before ``src.generator`` calls ``load_dotenv()``: a ``true`` that exists only in
    ``.env`` ends ``false``, the gate passes, the slot runs end to end, and no connection is attempted."""
    from langsmith import utils as ls_utils

    dotenv = pytest.importorskip("dotenv")
    env_file = tmp_path / "tracing_only.env"
    env_file.write_text("".join(f"{name}=true\n" for name in TRACING_VARS) + "UNRELATED_VAR=loaded\n")
    isolate_env(monkeypatch, *TRACING_VARS, "UNRELATED_VAR", "PYTHON_DOTENV_DISABLED")  # (the P0 capture leaves the last set)

    # control: .env alone (no scrub first) switches tracing on, and the CLI refuses the slot
    dotenv.load_dotenv(env_file)
    ls_utils.get_env_var.cache_clear()
    assert [os.environ[n] for n in TRACING_VARS] == ["true"] * 4
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and "gate=tracing" in err
    assert "slot_claim" not in kinds(world)

    # the entry's order: scrub first, then the .env load
    isolate_env(monkeypatch, *TRACING_VARS, "UNRELATED_VAR", "PYTHON_DOTENV_DISABLED")  # (the P0 capture leaves the last set)
    tracing_guard.scrub_tracing_env()
    dotenv.load_dotenv(env_file)
    ls_utils.get_env_var.cache_clear()
    assert [os.environ[n] for n in TRACING_VARS] == ["false"] * 4 and os.environ["UNRELATED_VAR"] == "loaded"
    assert tracing_guard.tracing_problems() == []

    retrieval = world.retrieval()
    matrix_kwargs = {
        "retrieve_fn_factory": retrieval.factory(6), "generate_fn": world.generator(retrieval),
        "judge_fn": world.judge(), "provenance_fn": lambda: dict(PROVENANCE),
    }
    real_run_slot = sealed_run.run_slot
    monkeypatch.setattr(sealed_run, "run_slot", lambda token, **kw: real_run_slot(token, matrix_kwargs=matrix_kwargs, **kw))
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 0, err
    assert "[eval] sealed slot 0: canonical; published results_v6.md" in out
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored", "publication_intent", "slot_consumed"]
    assert socket_spy == []  # nothing tried to leave the process
