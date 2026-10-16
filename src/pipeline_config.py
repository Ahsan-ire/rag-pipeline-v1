"""The identity of the running pipeline: ``pipeline_config_hash`` (16A-2b b1a r4, r5; D74).

A sealed slot compares a candidate with the **incumbent**, so "the incumbent" must be a value a machine can
check, not a claim. This module computes that value with **zero calls** (no model is loaded, no network is
touched, no corpus text is read): the sha256 of the canonical JSON of the configuration that shapes what the
pipeline retrieves, generates, judges and scores.

``pipeline_config(persist_directory, *, top_k)`` is a dict with exactly the keys :data:`PIPELINE_CONFIG_KEYS`:

``version``
    :data:`PIPELINE_CONFIG_VERSION` (2), bumped when this recipe changes.
``code``
    ``{module file: sha256 of its ast.dump with docstrings stripped}`` for **every** ``src`` module that is not in
    :data:`CODE_EXCLUDED`. New modules are hashed by default; a module is left out only by being added, reviewed, to
    the registered exclusion list. ``ast.dump`` carries no line numbers, so a comment, a docstring or a whitespace
    edit does not change it; any change to the code that runs does.
``runtime``
    The Python ``major.minor`` and **every** installed distribution (``importlib.metadata.distributions()``) as a
    sorted, deduplicated list of ``"normalised-name==version"`` strings.
``endpoint``
    The effective Anthropic base URL, resolved by the installed ``langchain_anthropic.ChatAnthropic`` itself (its
    ``anthropic_api_url`` field's default factory: ``ANTHROPIC_API_URL``, then ``ANTHROPIC_BASE_URL``, then the
    SDK default). Generation, the rewrite and the judge all build that client, so one value serves all three.
``embedding``
    The resolved ``embedder.EMBEDDING_MODEL`` (the environment override counts: it is read once at import), its
    :class:`~src.embedder.EmbeddingModelSpec` and the Chroma collection name.
``index``
    :func:`index_identity`: the sha256 of the sorted chunk ids, their count, the embedding-model manifest beside the
    store and the collection's effective search configuration (Chroma's ``hnsw`` / ``spann`` configuration blocks and
    any ``hnsw:*`` collection metadata); ``None`` when there is no index (and then :func:`pipeline_config_hash` is
    ``None``).
``bm25``
    The tokenizer pattern and ``BM25Okapi``'s ``k1``, ``b`` and ``epsilon`` defaults (the index is built with them).
``fusion``
    ``RRF_K``, the rewrite and intent list weights, the candidate pool and the run's ``top_k``.
``rewrite``
    ``expansion_artifact.rewrite_identity()["config_hash"]`` (rewrite model, prompt, caps) and the rewrite mode.
``generation``
    ``GENERATION_MODEL``, the client kwargs from ``generator.generation_llm_kwargs()`` without the timeout and
    retry count, the sha256 of the prompt template's ``[[role, template], ...]``, the refusal phrase and the caveat
    prefix.

**Excluded on purpose** (they change whether a call succeeds, or are about the run rather than the pipeline):
timeouts and retries (their literals live in ``src.llm_transport``, itself in :data:`CODE_EXCLUDED`, and the client
kwargs drop them), the git sha, paths (the persist directory is only read, never hashed), the API key, the modules in
:data:`CODE_EXCLUDED`, the eval sets and any corpus text. The judge has its own identity
(``src.judge.judge_identity``) but its code is part of ``code`` here too, and so is all scoring code. Chunk ids are
content hashes, so the index component moves when the indexed text does; chunk *metadata* (page and section numbers)
is not read, because a hash must never need documents.

Nothing here creates a file, a directory or a Chroma collection.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.metadata
import inspect
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.expansion_artifact import _canonical_json

PIPELINE_CONFIG_VERSION = 2
PIPELINE_CONFIG_KEYS = (
    "version", "code", "runtime", "endpoint", "embedding", "index", "bm25", "fusion", "rewrite", "generation",
)

#: The ``src`` modules whose code is NOT hashed (registered in the prereg as ``incumbent.code_excluded``; ``prereg-check``
#: recomputes the list from this constant). Every other ``src`` module, a new one included, is hashed. A module may be
#: listed only if it cannot change what is retrieved, generated, judged or scored: no value that reaches a prompt, a
#: client, a ranking, a verdict or a metric, no row filter, no default of those. Each entry's reason:
#:
#: * ``__init__.py``: empty package marker.
#: * ``audit.py``: the operational JSONL event log of ``pipeline query`` (what to record, never what to answer).
#: * ``eval_privacy.py``: privacy classes, opaque row-id spelling and private-path containment (a changed id
#:   spelling breaks a lookup; it moves no ranking, answer or verdict).
#: * ``eval_roster.py``: the bake-off arm roster for ``scripts/`` (display ids; nothing in ``src`` imports it).
#: * ``eval_sealed_read.py``: the read-only sealed-set loader (it validates and returns rows; refusing is its only effect).
#: * ``eval_sets.py``: the set registry and the public / private / sealed classifier (which file is what, not what a
#:   row scores).
#: * ``eval_split.py``: the deterministic family splitter that builds the sets (data preparation; nothing in ``src``
#:   imports it).
#: * ``llm_transport.py``: the timeout and retry literals (they change whether a call succeeds, never its result).
#: * ``render.py``: the display of a finished ``pipeline query`` result (display-only by its own contract).
#: * ``sealed_inputs.py``: ``inputs-check`` of the hand-made pairing and conflict files (a check; nothing imports it).
#: * ``sealed_ledger.py``: the append-only slot ledger and state machine (records, digests, locks and refusals). The
#:   group ids, the floor test and the excluded-family aggregation live in ``sealed_groups.py``, which is hashed.
#: * ``sealed_run.py``: the slot protocol: gates, preflight, token, claim and receipt, storage, publication. Nothing in it
#:   computes or projects a value a stored result or the adoption analysis uses: those (the row hits, the baseline,
#:   the results document, the canonical guards, the floor counts, the excluded groups) live in ``sealed_results.py``,
#:   which is hashed.
#: * ``spend.py``: the spend meter and its retry loop (it wraps the production clients; a retry changes whether a call
#:   succeeds, not what it returns).
#: * ``tracing_guard.py``: the tracing refusal (it can only stop a run).
CODE_EXCLUDED = (
    "__init__.py", "audit.py", "eval_privacy.py", "eval_roster.py", "eval_sealed_read.py", "eval_sets.py",
    "eval_split.py", "llm_transport.py", "render.py", "sealed_inputs.py", "sealed_ledger.py", "sealed_run.py",
    "spend.py", "tracing_guard.py",
)
#: Where the hashed modules live (tests point it at a copy to edit one).
CODE_DIR = Path(__file__).resolve().parent

#: Client kwargs that change whether a call succeeds, never what it returns: left out of every identity.
CLIENT_RELIABILITY_KEYS = ("default_request_timeout", "max_retries")


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace, UTF-8): the convention of ``expansion_artifact``."""
    return _canonical_json(obj)


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def prompt_messages(template: Any) -> List[List[str]]:
    """A chat prompt template as ``[[role, template text], ...]`` (the same shape the rewrite identity hashes)."""
    messages: List[List[str]] = []
    for message in template.messages:
        cls = type(message).__name__
        role = "system" if cls.startswith("System") else "human" if cls.startswith("Human") else cls
        text = getattr(getattr(message, "prompt", None), "template", None)
        if not isinstance(text, str):
            raise ValueError("a prompt message is not plain text")
        messages.append([str(role), text])
    return messages


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove the docstring statement of the module and of every class and function (an empty body keeps ``pass``)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


@functools.lru_cache(maxsize=512)
def _code_digest(path: str, mtime_ns: int, size: int) -> str:
    tree = _strip_docstrings(ast.parse(Path(path).read_text(encoding="utf-8")))
    return sha256_hex(ast.dump(tree).encode("utf-8"))


def code_digest(path: Path) -> str:
    """sha256 of ``ast.dump`` of a source file with docstrings stripped (comments never reach the tree)."""
    stat = path.stat()
    return _code_digest(str(path), stat.st_mtime_ns, stat.st_size)


def code_modules() -> List[str]:
    """The hashed modules: every ``*.py`` under :data:`CODE_DIR` (relative posix path) not in :data:`CODE_EXCLUDED`, sorted.

    Hashed by default: a module added to ``src`` joins the identity until it is registered as excluded.
    """
    return sorted(
        path.relative_to(CODE_DIR).as_posix() for path in CODE_DIR.rglob("*.py")
        if path.relative_to(CODE_DIR).as_posix() not in CODE_EXCLUDED and "__pycache__" not in path.parts
    )


def code_identity() -> Dict[str, str]:
    """``{module file: code digest}`` for :func:`code_modules` (an unreadable or unparseable file raises)."""
    return {name: code_digest(CODE_DIR / name) for name in code_modules()}


# ---------------------------------------------------------------------------
# runtime, endpoint, embedding, index
# ---------------------------------------------------------------------------


def normalise_distribution_name(name: str) -> str:
    """The PEP 503 normalised name: lower case, runs of ``-``, ``_`` and ``.`` become one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_identity() -> Dict[str, Any]:
    """The Python ``major.minor`` and EVERY installed distribution as a sorted, deduplicated ``name==version`` list.

    ``importlib.metadata.distributions()`` is read as it stands (no allow-list: a numerical or tokenizer dependency
    is as much a part of the retrieval as the packages named in a requirements file). A distribution with no name
    or no version has nothing to pin and is skipped.
    """
    found = set()
    for dist in importlib.metadata.distributions():
        name, version = dist.metadata["Name"], dist.version
        if name and version:
            found.add(f"{normalise_distribution_name(name)}=={version}")
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}", "packages": sorted(found)}


def endpoint_identity() -> Dict[str, str]:
    """The effective Anthropic base URL, exactly as the installed ``ChatAnthropic`` resolves it.

    ``langchain_anthropic.ChatAnthropic`` passes its ``anthropic_api_url`` field to the SDK client as ``base_url``
    (so the SDK's own ``ANTHROPIC_BASE_URL`` lookup never runs); the field's default factory reads
    ``ANTHROPIC_API_URL``, then ``ANTHROPIC_BASE_URL``, then falls back to the SDK default. This calls that factory,
    so the recipe follows the installed package instead of copying its rules. Generation, the rewrite and the
    judge all build that class, so one value serves all three. No client is built and nothing is sent.
    """
    from langchain_anthropic import ChatAnthropic

    factory = ChatAnthropic.model_fields["anthropic_api_url"].default_factory
    if factory is None:  # the installed package changed shape: fail closed, never guess the endpoint
        raise RuntimeError("ChatAnthropic.anthropic_api_url has no default factory")
    return {"anthropic_base_url": str(factory())}


def embedding_identity() -> Dict[str, Any]:
    """The resolved embedding model, its spec and the collection name (read as the pipeline reads them)."""
    from src import embedder

    return {
        "model": embedder.EMBEDDING_MODEL,
        "spec": asdict(embedder.get_model_spec()),
        "collection": embedder.COLLECTION_NAME,
    }


def index_identity(persist_directory: str) -> Optional[str]:
    """sha256 of the index: sorted chunk ids, their count, the embedding-model manifest and the search configuration.

    Reads the ids only (Chroma ``get(include=[])``: never documents, metadata or embeddings) and never builds an
    embedding function. The collection's effective search configuration (``configuration_json``'s ``hnsw`` and
    ``spann`` blocks, which carry ``ef_search`` and the other approximate-search settings, and any ``hnsw:*``
    collection metadata) is part of the hash: it can change the ranking while the ids stay the same. ``None`` when
    the directory, the Chroma store or the collection is missing; nothing is created in any of those cases (a
    missing directory is not even opened).
    """
    from src import embedder

    root = Path(persist_directory)
    if not (root / "chroma.sqlite3").is_file():
        return None
    import chromadb

    client = chromadb.PersistentClient(path=str(root))
    try:
        collection = client.get_collection(embedder.COLLECTION_NAME, embedding_function=None)
    except Exception:  # noqa: BLE001 - chromadb raises its own NotFound/ValueError types by version
        return None
    ids = sorted(collection.get(include=[])["ids"])
    manifest = root / embedder.EMBEDDING_MODEL_MANIFEST
    recorded = manifest.read_text(encoding="utf-8").strip() if manifest.is_file() else None
    configuration = collection.configuration_json
    metadata = collection.metadata or {}
    return sha256_hex(canonical_json({
        "ids": ids,
        "count": len(ids),
        "embedding_model_manifest": recorded,
        "search_configuration": {
            "hnsw": configuration.get("hnsw"),
            "spann": configuration.get("spann"),
            "metadata": {key: metadata[key] for key in sorted(metadata) if str(key).startswith("hnsw:")},
        },
    }))


# ---------------------------------------------------------------------------
# bm25, fusion, rewrite, generation
# ---------------------------------------------------------------------------


def bm25_identity() -> Dict[str, Any]:
    """The BM25 tokenizer pattern and ``BM25Okapi``'s ``k1``, ``b``, ``epsilon`` defaults."""
    from rank_bm25 import BM25Okapi

    from src import bm25_index

    params = inspect.signature(BM25Okapi.__init__).parameters
    return {
        "tokenizer": bm25_index._TOKEN_PATTERN.pattern,
        **{name: params[name].default for name in ("k1", "b", "epsilon")},
    }


def fusion_identity(top_k: int) -> Dict[str, Any]:
    """Reciprocal rank fusion: ``RRF_K``, the rewrite and intent list weights, the candidate pool and ``top_k``."""
    from src import retriever

    return {
        "rrf_k": retriever.RRF_K,
        "rewrite_list_weight": retriever.REWRITE_LIST_WEIGHT,
        "intent_list_weight": retriever.INTENT_LIST_WEIGHT,
        "candidate_pool": retriever.CANDIDATE_POOL,
        "top_k": top_k,
    }


def rewrite_component() -> Dict[str, Any]:
    """The rewrite step's ``config_hash`` (model, prompt, caps) and the rewrite retrieval mode."""
    from src import query_rewrite
    from src.expansion_artifact import rewrite_identity

    return {"config_hash": rewrite_identity()["config_hash"], "mode": query_rewrite.REWRITE_MODE}


def generation_identity() -> Dict[str, Any]:
    """The generation model, client kwargs (no timeout or retries), prompt hash, refusal phrase and caveat prefix."""
    from src import generator

    return {
        "model": generator.GENERATION_MODEL,
        "client": {
            k: v for k, v in generator.generation_llm_kwargs().items() if k not in CLIENT_RELIABILITY_KEYS
        },
        "prompt_sha256": sha256_hex(canonical_json(prompt_messages(generator.PROMPT_TEMPLATE))),
        "refusal_phrase": generator.REFUSAL_PHRASE,
        "caveat_prefix": generator.CAVEAT_PREFIX,
    }


# ---------------------------------------------------------------------------
# the configuration and its hash
# ---------------------------------------------------------------------------


def pipeline_config(persist_directory: str, *, top_k: int) -> Dict[str, Any]:
    """The pipeline configuration (see the module docstring): a JSON-serialisable dict, zero calls."""
    return {
        "version": PIPELINE_CONFIG_VERSION,
        "code": code_identity(),
        "runtime": runtime_identity(),
        "endpoint": endpoint_identity(),
        "embedding": embedding_identity(),
        "index": index_identity(persist_directory),
        "bm25": bm25_identity(),
        "fusion": fusion_identity(top_k),
        "rewrite": rewrite_component(),
        "generation": generation_identity(),
    }


def pipeline_config_hash(persist_directory: str, *, top_k: int) -> Optional[str]:
    """sha256 of the canonical JSON of :func:`pipeline_config`, or ``None`` when there is no index.

    A pipeline with no index has no identity: ``None`` never equals a recorded incumbent hash, so a missing or
    emptied index cannot pass for the incumbent.
    """
    config: Mapping[str, Any] = pipeline_config(persist_directory, top_k=top_k)
    if config["index"] is None:
        return None
    return sha256_hex(canonical_json(config))
