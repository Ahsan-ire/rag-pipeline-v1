"""The identity of the running pipeline: ``pipeline_config_hash`` (16A-2b b1a r4, D74).

A sealed slot compares a candidate with the **incumbent**, so "the incumbent" must be a value a machine can
check, not a claim. This module computes that value with **zero calls** (no model is loaded, no network is
touched, no corpus text is read): the sha256 of the canonical JSON of the configuration that shapes what the
pipeline retrieves and generates.

``pipeline_config(persist_directory, *, top_k)`` is a dict with exactly the keys :data:`PIPELINE_CONFIG_KEYS`:

``version``
    :data:`PIPELINE_CONFIG_VERSION`, bumped when this recipe changes.
``code``
    ``{module file: sha256 of its ast.dump with docstrings stripped}`` for :data:`CODE_MODULES`, the modules that
    shape retrieval, query rewriting and generation. ``ast.dump`` carries no line numbers, so a comment, a
    docstring or a whitespace edit does not change it; any change to the code that runs does.
``runtime``
    The Python ``major.minor`` and the installed versions (``importlib.metadata``) of the retrieval and
    generation packages the modules above import.
``embedding``
    The resolved ``embedder.EMBEDDING_MODEL`` (the environment override counts: it is read once at import), its
    :class:`~src.embedder.EmbeddingModelSpec` and the Chroma collection name.
``index``
    :func:`index_identity`: the sha256 of the sorted chunk ids, their count and the embedding-model manifest
    beside the store; ``None`` when there is no index (and then :func:`pipeline_config_hash` is ``None``).
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
timeouts and retries, the git sha, paths (the persist directory is only read, never hashed), the API key, the
spend meter, the judge (it has its own identity: ``src.judge.judge_identity``), scoring, the eval sets and any
corpus text. Chunk ids are content hashes, so the index component moves when the indexed text does; chunk
*metadata* (page and section numbers) is not read, because a hash must never need documents.

Nothing here creates a file, a directory or a Chroma collection.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.metadata
import inspect
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.expansion_artifact import _canonical_json

PIPELINE_CONFIG_VERSION = 1
PIPELINE_CONFIG_KEYS = (
    "version", "code", "runtime", "embedding", "index", "bm25", "fusion", "rewrite", "generation",
)

#: The ``src`` modules whose code is hashed: the ones that shape what is retrieved, how a question is rewritten
#: and how an answer is generated and graded for grounding. The files must all exist.
CODE_MODULES = (
    "retriever.py", "bm25_index.py", "embedder.py", "chunker.py", "query_rewrite.py", "generator.py", "grounding.py",
)
#: Where :data:`CODE_MODULES` live (tests point it at a copy to edit one).
CODE_DIR = Path(__file__).resolve().parent

#: The distributions the modules above import at retrieval or generation time (``importlib.metadata`` names).
RUNTIME_PACKAGES = (
    "rank-bm25", "chromadb", "sentence-transformers", "transformers", "langchain-core", "langchain-anthropic",
    "langchain-chroma", "langchain-huggingface", "langchain-text-splitters", "anthropic",
)

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


@functools.lru_cache(maxsize=64)
def _code_digest(path: str, mtime_ns: int, size: int) -> str:
    tree = _strip_docstrings(ast.parse(Path(path).read_text(encoding="utf-8")))
    return sha256_hex(ast.dump(tree).encode("utf-8"))


def code_digest(path: Path) -> str:
    """sha256 of ``ast.dump`` of a source file with docstrings stripped (comments never reach the tree)."""
    stat = path.stat()
    return _code_digest(str(path), stat.st_mtime_ns, stat.st_size)


def code_identity() -> Dict[str, str]:
    """``{module file: code digest}`` for :data:`CODE_MODULES` (a missing file raises ``OSError``)."""
    return {name: code_digest(CODE_DIR / name) for name in CODE_MODULES}


# ---------------------------------------------------------------------------
# runtime, embedding, index
# ---------------------------------------------------------------------------


def runtime_identity() -> Dict[str, Any]:
    """The Python ``major.minor`` and the installed version of each package in :data:`RUNTIME_PACKAGES`."""
    packages: Dict[str, Optional[str]] = {}
    for name in RUNTIME_PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}", "packages": packages}


def embedding_identity() -> Dict[str, Any]:
    """The resolved embedding model, its spec and the collection name (read as the pipeline reads them)."""
    from src import embedder

    return {
        "model": embedder.EMBEDDING_MODEL,
        "spec": asdict(embedder.get_model_spec()),
        "collection": embedder.COLLECTION_NAME,
    }


def index_identity(persist_directory: str) -> Optional[str]:
    """sha256 of the index: sorted chunk ids, their count and the embedding-model manifest, or ``None``.

    Reads the ids only (Chroma ``get(include=[])``: never documents, metadata or embeddings) and never builds an
    embedding function. ``None`` when the directory, the Chroma store or the collection is missing; nothing is
    created in any of those cases (a missing directory is not even opened).
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
    return sha256_hex(canonical_json({"ids": ids, "count": len(ids), "embedding_model_manifest": recorded}))


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
