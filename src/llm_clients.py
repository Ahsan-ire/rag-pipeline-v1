"""How the metered Anthropic clients are shaped from the production ones (16A-2b b1a r6, D74).

``src.spend.SpendMeter`` meters every live eval call through a copy of the client the production code builds. What that
copy changes about the client decides what the model is asked and how: a field set here (``max_tokens``, ``thinking``, the
model id) would change generation, the rewrite and the judge alike. It was inside ``spend.default_inner_factory``; it lives
here, in a module that ``src.pipeline_config.CODE_EXCLUDED`` does not list (so editing it changes ``pipeline_config_hash``)
and that ``src.judge.JUDGE_CODE_MODULES`` lists (so editing it changes the judge's identity). ``src.spend`` keeps the
metering (prices, the ledger, the retry loop) and calls :func:`build_metered_inner`.

The production builders (``generator.get_llm``, ``query_rewrite.get_rewrite_llm``) still own the model id, the token cap,
``thinking`` and the transport literals; this module only says which builder serves which kind and what is overridden on the
copy. It imports them lazily, so importing it loads no model client and runs no ``load_dotenv()``.
"""

from __future__ import annotations

from typing import Any, Dict

#: The client kinds the meter serves: generation and the judge call through the same production client.
KINDS = ("generation", "judge", "rewrite")

#: What the meter overrides on the production client: its own retry loop owns every retry, so the SDK must not retry.
METERED_CLIENT_UPDATE: Dict[str, Any] = {"max_retries": 0}

#: Cached SDK attributes dropped from the copy, so the overridden value is the one the SDK client is built with.
CACHED_SDK_ATTRIBUTES = ("_client_params", "_client", "_async_client")


def production_client(kind: str) -> Any:
    """Today's production client for ``kind`` (``get_llm`` for generation and judge, ``get_rewrite_llm`` for rewrite).

    Raises:
        ValueError: an unknown ``kind``, or no API key (exactly as the builders raise it).
    """
    if kind in ("generation", "judge"):
        from src.generator import get_llm

        return get_llm()
    if kind == "rewrite":
        from src.query_rewrite import get_rewrite_llm

        return get_rewrite_llm()
    raise ValueError(f"unknown client kind {kind!r}")


def shape_metered_client(base: Any) -> Any:
    """A copy of the production client ``base`` with :data:`METERED_CLIENT_UPDATE` applied and its cached SDK clients dropped."""
    inner = base.model_copy(update=dict(METERED_CLIENT_UPDATE))
    for cached in CACHED_SDK_ATTRIBUTES:
        inner.__dict__.pop(cached, None)
    return inner


def build_metered_inner(kind: str) -> Any:
    """The client the meter wraps for ``kind``: the production client, copied with ``max_retries=0``.

    Reuses the production builders, so the model id, ``max_tokens``, timeout and ``thinking`` cannot drift from what
    ``pipeline query`` uses.
    """
    return shape_metered_client(production_client(kind))
