"""Tracing guard for slot-gated sealed runs (Phase 16A-2a item a5, D73).

A metered call runs LangChain callbacks (``src/spend.py``), so a configured
LangSmith tracer would send prompts (sealed question text, rewrites, answers,
judge claims) to a third party. A sealed run therefore needs tracing off, and
two defences:

1. **Scrub, before ``src`` is imported.** :func:`scrub_tracing_env` sets
   ``LANGSMITH_TRACING_V2``, ``LANGCHAIN_TRACING_V2``, ``LANGSMITH_TRACING`` and
   ``LANGCHAIN_TRACING`` to ``"false"``. ``src.pipeline`` calls it as the very
   first thing when it runs as the CLI entry (``python -m src.pipeline``), before
   it imports any other ``src`` module. ``src.generator`` calls
   ``load_dotenv()`` at import, and ``load_dotenv`` does not override a variable
   that is already set, so a value in the environment *or* in ``.env`` is
   neutralised. This module imports only the standard library at module level,
   so importing it first changes nothing that reads the environment.
2. **Refuse, just before the first call.** :func:`assert_tracing_off` raises
   :class:`TracingRefused` if any of the following still holds:

   * ``env_enabled``: a tracing variable holds a true-like value;
   * ``tracing_enabled``: ``langsmith.utils.tracing_is_enabled()`` is true
     (it reads a cached copy of the environment, so a value read before the
     scrub is still visible here: this is the check that catches a late scrub);
   * ``global_tracer``: a tracing context variable holds a tracer (for
     instance ``tracing_v2_enabled()`` is active) or a run collector;
   * ``tracer_callback``: a LangChain configure hook would attach a handler
     (a context variable set, or its enabling environment variable set);
   * ``debug``: LangChain's global debug flag is on, which prints every
     prompt to stdout through the console callback.

The reasons are fixed tokens; :class:`TracingRefused` never carries any other
text, so it is safe to print.
"""

from __future__ import annotations

import os
from typing import List, MutableMapping, Optional

#: The four variables that switch LangChain/LangSmith tracing on (a5).
TRACING_VARS = (
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING",
)

_TRUE_LIKE = frozenset({"true", "1", "yes", "on"})


class TracingRefused(BaseException):
    """Tracing is on, or cannot be shown to be off. ``reasons`` are fixed tokens.

    A ``BaseException`` (like the ledger and meter errors) so that no broad
    ``except Exception`` can turn the refusal into a fall-through.
    """

    def __init__(self, reasons: List[str]) -> None:
        super().__init__("tracing is on: " + ", ".join(reasons))
        self.reasons = list(reasons)


def scrub_tracing_env(environ: Optional[MutableMapping[str, str]] = None) -> None:
    """Set every tracing variable to ``"false"`` (in ``os.environ`` unless given one).

    Idempotent. Setting (not deleting) matters: ``load_dotenv`` skips a variable
    that is already present, so a ``true`` in ``.env`` cannot come back.
    """
    env = os.environ if environ is None else environ
    for name in TRACING_VARS:
        env[name] = "false"


def tracing_problems() -> List[str]:
    """The reasons tracing is, or may be, on right now (empty = off).

    Pure inspection: it sends nothing and changes nothing. The langsmith and
    langchain_core checks are skipped when those packages are not installed
    (then there is nothing to trace with); the environment check always runs.
    """
    problems: List[str] = []
    if any(os.environ.get(name, "").strip().lower() in _TRUE_LIKE for name in TRACING_VARS):
        problems.append("env_enabled")
    try:
        from langsmith import utils as ls_utils
    except ImportError:
        ls_utils = None
    if ls_utils is not None and ls_utils.tracing_is_enabled():
        problems.append("tracing_enabled")
    try:
        from langchain_core import globals as lc_globals
        from langchain_core.tracers import context as tracer_context
        from langchain_core.utils.env import env_var_is_set
    except ImportError:
        return problems
    if tracer_context.tracing_v2_callback_var.get() is not None or tracer_context.run_collector_var.get() is not None:
        problems.append("global_tracer")
    for var, _inheritable, handler_class, env_var in tracer_context._configure_hooks:
        if var.get() is not None or (env_var is not None and handler_class is not None and env_var_is_set(env_var)):
            if "tracer_callback" not in problems:
                problems.append("tracer_callback")
    if lc_globals.get_debug():
        problems.append("debug")
    return problems


def assert_tracing_off() -> None:
    """Raise :class:`TracingRefused` unless :func:`tracing_problems` is empty."""
    problems = tracing_problems()
    if problems:
        raise TracingRefused(problems)
