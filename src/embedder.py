"""Embedding generation and ChromaDB vector storage module."""

import functools
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

from src.bm25_index import BM25_FILENAME, build_bm25_index, save_bm25_index

try:
    from huggingface_hub.errors import LocalEntryNotFoundError
except ImportError:  # pragma: no cover - defensive against older huggingface_hub
    LocalEntryNotFoundError = None

logger = logging.getLogger(__name__)

CHROMA_PERSIST_DIR = "./chroma_db"
COLLECTION_NAME = "legal_documents"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_MODEL_MANIFEST = "embedding_model.txt"


@dataclass(frozen=True)
class EmbeddingModelSpec:
    """Per-model retrieval configuration (D55).

    Every embedding model this pipeline can run under differs in ways that are
    invisible at the ``HuggingFaceEmbeddings`` call site: how many tokens it
    reads before truncating, and whether it expects an instruction prefix on
    queries and/or documents. Keeping those facts in one frozen record — rather
    than as scattered ``if model == ...`` branches — is what lets the model be
    swapped by an environment variable during the bake-off.

    Attributes:
        context_window: Tokens the model actually reads. Recorded per model and
            validated against ``max_seq_length`` (see
            :func:`_validate_model_specs`). ``None`` means "unknown". No
            index-time truncation guard consumes it today: that guard (Phase
            15 WS3, reserved D56) was designed but NOT landed — mooted by the
            D57 no-swap outcome, disposition recorded in D59.
        max_seq_length: Value forced onto the loaded SentenceTransformer client.
            Some repos ship a sentinel/placeholder here, so it is set
            explicitly and read back. ``None`` leaves the repo's own value.
        query_prompt: Instruction prefix applied to QUERIES only. ``None`` means
            the model takes no prefix; prompts are never inherited implicitly
            (see :func:`_apply_model_config`).
        doc_prompt: Instruction prefix applied to DOCUMENTS only.
    """

    context_window: Optional[int] = None
    max_seq_length: Optional[int] = None
    query_prompt: Optional[str] = None
    doc_prompt: Optional[str] = None


MODEL_SPECS: Dict[str, EmbeddingModelSpec] = {
    # MiniLM: the shipped baseline. No max_seq_length override (its repo value
    # is already correct) and no prompts — this entry must stay behaviourally
    # inert, which the MiniLM canary test pins.
    DEFAULT_EMBEDDING_MODEL: EmbeddingModelSpec(context_window=256),
    "Alibaba-NLP/gte-modernbert-base": EmbeddingModelSpec(
        context_window=8192, max_seq_length=8192
    ),
    "ibm-granite/granite-embedding-small-english-r2": EmbeddingModelSpec(
        context_window=8192, max_seq_length=8192
    ),
    "Qwen/Qwen3-Embedding-0.6B": EmbeddingModelSpec(
        context_window=8192,
        max_seq_length=8192,
        # Verbatim from the repo's config_sentence_transformers.json
        # prompts["query"] (WS4 precheck, 4 Aug 2026) — version-pinned here so
        # a repo-side prompt change cannot silently alter the query contract.
        # The document prompt in that config is the empty string: no doc prompt.
        query_prompt=(
            "Instruct: Given a web search query, retrieve relevant passages "
            "that answer the query\nQuery:"
        ),
    ),
}


def _validate_model_specs(specs: Dict[str, EmbeddingModelSpec]) -> None:
    """Raise if any spec's ``max_seq_length`` diverges from its window.

    The two numbers would be read by different consumers: a chunk-vs-window
    check uses ``context_window``, while the embedding client truncates at
    ``max_seq_length``. If they diverged, provenance could report "over-window
    = 0" while the model silently truncated at the shorter length (plan-gate
    finding A17). The index-time truncation guard that would consume
    ``context_window`` was designed (Phase 15 WS3, reserved D56) but NOT
    landed — mooted by the D57 no-swap outcome (disposition in D59) — so this
    validation is the only enforcement today.

    Only specs that set BOTH are checked; a spec with one side ``None`` is
    making no claim about the other.

    Args:
        specs: Mapping of model id to spec, e.g. :data:`MODEL_SPECS`.

    Raises:
        ValueError: If a spec sets both fields to different values.
    """
    for name, spec in specs.items():
        if spec.context_window is None or spec.max_seq_length is None:
            continue
        if spec.max_seq_length != spec.context_window:
            raise ValueError(
                f"EmbeddingModelSpec for {name!r} is inconsistent: "
                f"max_seq_length={spec.max_seq_length} but "
                f"context_window={spec.context_window}. context_window is what "
                "provenance reasons about while the embedding client truncates "
                "at max_seq_length, so a divergence would let provenance claim "
                "zero over-window chunks while the model truncated them "
                "anyway. Set both to the same number."
            )


# Runs at import: a bad spec is a configuration bug, and the only safe time to
# find it is before anything has been embedded under it.
_validate_model_specs(MODEL_SPECS)


def resolve_embedding_model() -> str:
    """Return the embedding model id this process should use.

    Reads the ``EMBEDDING_MODEL`` **process environment variable ONLY, never
    ``.env``**. This module is imported before ``src.generator`` calls
    ``load_dotenv()``, so a value living in a developer's ``.env`` would take
    effect or not depending on which module the entry point happened to import
    first — i.e. non-deterministically per entry point, which is exactly the
    kind of silent index/query model mismatch the D5 manifest exists to catch.
    Export it in the shell instead::

        EMBEDDING_MODEL=<model-id> python -m src.pipeline index ...

    An id with no :data:`MODEL_SPECS` entry is still returned (so a bake-off
    arm can be driven without a code change), but warns: generic defaults then
    apply — no known context window, no ``max_seq_length`` override, no prompts.

    Returns:
        The configured model id, or :data:`DEFAULT_EMBEDDING_MODEL`.
    """
    name = os.environ.get("EMBEDDING_MODEL", "").strip() or DEFAULT_EMBEDDING_MODEL
    if name not in MODEL_SPECS:
        logger.warning(
            "Embedding model %r has no MODEL_SPECS entry; generic defaults "
            "apply (no known context window, no max_seq_length override, no "
            "query/document prompts). Add a MODEL_SPECS entry to configure it.",
            name,
        )
    return name


def get_model_spec(model_name: Optional[str] = None) -> EmbeddingModelSpec:
    """Return the :class:`EmbeddingModelSpec` for ``model_name``.

    Args:
        model_name: Model id to look up. Defaults to the configured
            :data:`EMBEDDING_MODEL`.

    Returns:
        The registered spec, or an all-``None`` generic spec for a model id
        :data:`MODEL_SPECS` does not know about.
    """
    return MODEL_SPECS.get(model_name or EMBEDDING_MODEL, EmbeddingModelSpec())


# Module-level constant by design: evaluator.py imports it BY VALUE, and the
# manifest write / assert_embedding_model default read it. Rebinding it after
# import would not reach those consumers, so the env var is read exactly once,
# here, at import time.
EMBEDDING_MODEL = resolve_embedding_model()

# A "can't find it" phrase must appear TOGETHER WITH "cache" in an OSError
# message for it to read as a cold-cache miss (D47; gate fix 14 Jul 2026) —
# requiring both keeps an unrelated cache-mentioning failure (e.g. "Permission
# denied writing cache directory") from being mistaken for a miss.
_NOT_FOUND_PHRASES = ("cannot find", "couldn't find", "not found")


def _is_local_cache_miss(exc: Exception) -> bool:
    """True iff ``exc`` is the local-cache-miss family, not an unrelated failure.

    Verified against the installed versions (huggingface_hub 1.22.0,
    transformers 5.14.1, sentence_transformers 5.6.0). The message shape below
    was re-verified under the 5.14.1 pin (Phase 15 WS2, 3 Aug 2026) and is
    unchanged from the 5.13.0 wording this heuristic was written against —
    ``transformers/utils/hub.py:513-517``. A cold cache with
    ``local_files_only=True`` actually surfaces from ``SentenceTransformer``
    as a plain ``OSError`` — ``transformers.utils.hub.cached_file`` catches
    huggingface_hub's own ``LocalEntryNotFoundError`` and re-raises it as an
    ``OSError`` whose message pairs a "couldn't find them" phrase with the
    word "cache" (the installed transformers text is: "We couldn't connect to
    '<endpoint>' to load the files, and couldn't find them in the cached
    files. Check your internet connection or see how to run the library in
    offline mode ..."). The direct ``isinstance(exc, LocalEntryNotFoundError)``
    check is kept too, since other huggingface_hub/sentence_transformers call
    paths (e.g. ``sentence_transformers.util.file_io.load_file_path``) raise it
    directly rather than wrapping it.

    Narrowness matters (gate fix 14 Jul 2026): a ``PermissionError`` or
    ``IsADirectoryError`` is a filesystem/config fault, NOT a cold cache — a
    network-enabled retry would neither help nor be safe — so those are
    excluded even though they are ``OSError`` subclasses whose messages may
    mention "cache" (e.g. "Permission denied writing cache directory"). For any
    other ``OSError`` we require message evidence of the actual cold-cache
    shape: the literal ``local_files_only`` flag, OR a "can't find it" phrase
    together with "cache". Anything else (a plain ``ValueError``, an auth
    error, ...) is NOT a cache miss and must propagate immediately (D47,
    plan-gate finding M12) instead of silently triggering a download retry.
    """
    if LocalEntryNotFoundError is not None and isinstance(exc, LocalEntryNotFoundError):
        return True
    # PermissionError / IsADirectoryError are OSError subclasses but are never a
    # cold-cache miss — exclude them BEFORE the generic OSError branch so a
    # "cache"-mentioning permission failure can't trigger a network retry.
    if isinstance(exc, (PermissionError, IsADirectoryError)):
        return False
    if isinstance(exc, OSError):
        message = str(exc).lower()
        if "local_files_only" in message:
            return True
        not_found = any(phrase in message for phrase in _NOT_FOUND_PHRASES)
        return not_found and "cache" in message
    return False


@functools.lru_cache(maxsize=1)
def get_embedding_function() -> HuggingFaceEmbeddings:
    """Create and return the HuggingFace embedding function (cached).

    ``lru_cache`` means the underlying MiniLM model loads once per process
    instead of once per ``get_vector_store()`` call — before this, every
    ``retrieve()`` reloaded the model (35+ loads across one eval run). Safe
    because the object is stateless configuration and the function takes no
    arguments; callers that need a different embedding function (tests use
    FakeEmbeddings) already pass it explicitly and never hit this path.

    Local-first (D47): tries ``local_files_only=True`` first, so a warm
    cache never makes the ~25 HuggingFace HEAD requests per CLI run that
    re-validate an already-cached model. Only a recognised cache-miss
    failure (:func:`_is_local_cache_miss`) falls back to one network-enabled
    retry, logged once; any other exception propagates immediately instead
    of silently falling back to the network.

    Encode kwargs come from the model's :class:`EmbeddingModelSpec` (D55), and
    :func:`_apply_model_config` finishes the setup on the loaded client. For
    MiniLM the spec carries neither prompts nor a ``max_seq_length`` override,
    so both are inert and construction is byte-identical to before.
    """
    spec = get_model_spec(EMBEDDING_MODEL)

    encode_kwargs = {"normalize_embeddings": True}
    if spec.doc_prompt:
        encode_kwargs["prompt"] = spec.doc_prompt

    # The merge is load-bearing, not stylistic. langchain-huggingface 1.2.2's
    # embed_query REPLACES encode_kwargs with query_encode_kwargs whenever the
    # latter is non-empty (huggingface.py:167-171 — it picks one dict, it does
    # not merge them). So a query_encode_kwargs of just {"prompt": ...} would
    # drop normalize_embeddings and embed QUERIES UNNORMALIZED against
    # NORMALIZED documents — under Chroma's default L2 space that silently
    # skews every distance by the query's magnitude. Copying the base kwargs in
    # keeps normalization on both sides. The same replace-not-merge rule bites
    # from the other side: with a doc prompt but no query prompt, an EMPTY
    # query_encode_kwargs makes queries fall back to encode_kwargs and so
    # inherit the DOCUMENT prompt (C6). That case gets explicit
    # normalization-only kwargs. Empty dict only when neither prompt exists
    # (MiniLM), keeping that path byte-identical to before.
    if spec.query_prompt:
        query_encode_kwargs = {**encode_kwargs, "prompt": spec.query_prompt}
    elif spec.doc_prompt:
        query_encode_kwargs = {"normalize_embeddings": True}
    else:
        query_encode_kwargs = {}

    try:
        emb = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu", "local_files_only": True},
            encode_kwargs=encode_kwargs,
            query_encode_kwargs=query_encode_kwargs,
        )
    except Exception as exc:
        if not _is_local_cache_miss(exc):
            raise
        logger.info("Local model cache miss for %s; downloading once", EMBEDDING_MODEL)
        emb = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs=encode_kwargs,
            query_encode_kwargs=query_encode_kwargs,
        )

    _apply_model_config(emb, spec)
    return emb


def _apply_model_config(emb: HuggingFaceEmbeddings, spec: EmbeddingModelSpec) -> None:
    """Force per-model settings onto the loaded SentenceTransformer client.

    Two things cannot be configured through ``HuggingFaceEmbeddings``' own
    constructor and must be set on the client after it exists:

    * ``max_seq_length`` — some repos ship a placeholder/sentinel value, so it
      is set explicitly and **read back**; a client that silently ignores the
      assignment would truncate at its own length while provenance believed
      the spec's window. (sentence-transformers 5.6 renamed
      ``tokenizer_kwargs`` to ``processor_kwargs``, so threading it through
      construction is version-fragile — post-construction assignment is not.)
    * ``default_prompt_name`` — sentence-transformers 5.6 merges a repo's
      ``config_sentence_transformers.json`` ``default_prompt_name`` into the
      model when the caller did not set one (``base/model.py:1240-1250``). That
      prompt is then applied to *every* encode call, i.e. it would silently
      prefix DOCUMENTS too. Prompts in this pipeline are explicit in
      :data:`MODEL_SPECS` or absent, so the inherited default is neutralized.

    Args:
        emb: The constructed embedding wrapper.
        spec: The model's spec.

    Raises:
        ValueError: If ``max_seq_length`` does not read back as it was set.
    """
    client = getattr(emb, "_client", None)
    if client is None:
        # Test doubles (and anything not backed by a real SentenceTransformer)
        # have no client to configure — nothing to do.
        return

    if spec.max_seq_length is not None:
        client.max_seq_length = spec.max_seq_length
        actual = getattr(client, "max_seq_length", None)
        if actual != spec.max_seq_length:
            model_name = getattr(emb, "model_name", EMBEDDING_MODEL)
            raise ValueError(
                f"Could not set max_seq_length on {model_name!r}: asked for "
                f"{spec.max_seq_length}, got {actual!r} back. The model would "
                "truncate at a length provenance does not know about."
            )

    client.default_prompt_name = None


def get_vector_store(
    embedding_function: Optional[HuggingFaceEmbeddings] = None,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Chroma:
    """Get or create a ChromaDB vector store.

    Args:
        embedding_function: Optional embedding function. Creates one if not provided.
        persist_directory: Directory for ChromaDB persistence.
    """
    if embedding_function is None:
        embedding_function = get_embedding_function()

    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embedding_function,
        persist_directory=persist_directory,
        # Best-effort, human-discoverable record (D5). Not load-bearing: Chroma
        # only honours this on first creation, and langchain-chroma exposes no
        # public getter for it back — see EMBEDDING_MODEL_MANIFEST below for the
        # authoritative, asserted record.
        collection_metadata={"embedding_model": EMBEDDING_MODEL},
    )


def compute_chunk_id(source: str, text: str) -> str:
    """Source-scoped content-hash chunk ID.

    D7's principle — "identity means identity across re-chunking, unlike
    positional IDs which collide with different content the moment chunking
    changes" — still holds, but now holds PER SOURCE. A pure text hash collides
    across documents: identical boilerplate in two files (a standard warranty
    clause, a shared statutory header) would hash to one shared ID, so a
    per-source replace deleting source A's stale IDs could destroy source B's
    live chunk, and insert-dedup would silently skip B's copy as a "duplicate"
    of A's. That is a real hazard on the multi-document roadmap, so ``source``
    is folded into the hash.

    The NUL (``\\0``) separator removes the concatenation ambiguity a bare
    ``source + text`` would carry — without it ``("ab", "c")`` and
    ``("a", "bc")`` hash identically. NUL cannot appear in a filesystem path
    and is vanishingly rare in extracted corpus text, so it is an unambiguous
    delimiter.

    Consequence: every pre-Phase-9 ID (a bare text hash) changes under this
    scheme — old and new IDs never coincide — so the existing index must be
    rebuilt once with a full ``--reset`` re-index.

    Args:
        source: The document identity (``metadata["source"]``) the chunk
            belongs to.
        text: The chunk's page content.
    """
    return hashlib.sha256((source + "\0" + text).encode("utf-8")).hexdigest()[:16]


def _write_embedding_model_manifest(persist_directory: str) -> None:
    """Record the configured embedding model beside the vector store (D5)."""
    path = Path(persist_directory) / EMBEDDING_MODEL_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EMBEDDING_MODEL)


def assert_embedding_model(
    persist_directory: str = CHROMA_PERSIST_DIR, expected: str = EMBEDDING_MODEL
) -> None:
    """Raise if the persisted index was built with a different embedding model
    than the one currently configured (D5) — corpus and queries must share one
    coordinate system. Silently no-ops if the index predates this manifest."""
    path = Path(persist_directory) / EMBEDDING_MODEL_MANIFEST
    if not path.exists():
        logger.warning(
            "No embedding-model manifest at %s; skipping model-match check "
            "(index predates this check, or nothing has been indexed yet).",
            persist_directory,
        )
        return

    recorded = path.read_text().strip()
    if recorded != expected:
        raise ValueError(
            f"Embedding model mismatch: the index at {persist_directory!r} was "
            f"built with {recorded!r}, but the pipeline is currently configured "
            f"for {expected!r}. Re-index with --reset to rebuild under the "
            "current model."
        )


def _recorded_collection_model(vector_store: Chroma) -> Optional[str]:
    """Best-effort read of the model recorded in the Chroma collection metadata.

    ``get_vector_store`` passes ``collection_metadata={"embedding_model": ...}``,
    which Chroma keeps only from the collection's first creation. langchain-chroma
    has no public getter, so this reads the private ``_collection`` and returns
    ``None`` (no claim) if it is unavailable, e.g. for a test double.
    """
    metadata = getattr(getattr(vector_store, "_collection", None), "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("embedding_model")
        if isinstance(value, str) and value:
            return value
    return None


def _assert_index_write_allowed(vector_store: Chroma, persist_directory: str) -> bool:
    """Refuse an index WRITE under a different embedding model than the store's.

    MiniLM and granite are both 384-d, so a dimension check cannot catch a
    cross-model write; mixing them would put two coordinate systems in one
    index. ``assert_embedding_model`` only guards read paths, so every write
    path calls this BEFORE touching vectors or the manifest.

    Rules: a manifest naming another model raises; a collection-metadata model
    differing from the current one raises; no manifest on a non-empty store
    (legacy) raises; a fresh/empty store is allowed. After ``--reset`` the
    store is cleared first, so it is fresh and a model change passes.

    Args:
        vector_store: The store about to be written.
        persist_directory: Where the manifest lives.

    Returns:
        True if the manifest is already present (and matches), False for a
        fresh store whose manifest the caller will create.

    Raises:
        ValueError: On any cross-model or unprovenanced non-empty store.
    """
    path = Path(persist_directory) / EMBEDDING_MODEL_MANIFEST
    remedy = "Re-index with --reset to rebuild the store under the current model."
    if path.exists():
        recorded = path.read_text().strip()
        if recorded != EMBEDDING_MODEL:
            raise ValueError(
                f"Refusing to write: the index at {persist_directory!r} was "
                f"built with {recorded!r}, but the pipeline is configured for "
                f"{EMBEDDING_MODEL!r}. {remedy}"
            )
    collection_model = _recorded_collection_model(vector_store)
    if collection_model is not None and collection_model != EMBEDDING_MODEL:
        raise ValueError(
            f"Refusing to write: the vector store at {persist_directory!r} "
            f"records collection model {collection_model!r}, but the pipeline "
            f"is configured for {EMBEDDING_MODEL!r}. {remedy}"
        )
    if path.exists():
        return True
    if vector_store.get(limit=1)["ids"]:
        raise ValueError(
            f"Refusing to write: the store at {persist_directory!r} already "
            f"holds vectors but has no {EMBEDDING_MODEL_MANIFEST} recording "
            f"which model made them (legacy index). {remedy}"
        )
    return False


def _sanitize_metadata(documents: List[Document]) -> tuple:
    """Drop None-valued metadata keys in place; return ``(dropped, affected)``.

    Chroma rejects ``None`` metadata values, but handbook chunks legitimately
    carry ``page_start`` / ``page_end`` of None when a printed page could not be
    recovered (front-matter residue, headerless pages outside the inference
    window). Rather than warn per chunk, the caller reports one aggregated line.
    """
    dropped = 0
    affected = 0
    for doc in documents:
        clean = {k: v for k, v in doc.metadata.items() if v is not None}
        removed = len(doc.metadata) - len(clean)
        if removed:
            dropped += removed
            affected += 1
            doc.metadata = clean
    return dropped, affected


def add_documents(
    documents: List[Document],
    vector_store: Optional[Chroma] = None,
    persist_directory: Optional[str] = None,
) -> int:
    """Add documents to the vector store with deduplication.

    Uses content-hash IDs (D7) so identity means identity: re-chunking the
    same text always resolves to the same ID, regardless of position.

    On any new documents, also rebuilds the BM25 lexical index (D6, D24) from
    the store's full authoritative contents and (re)records the embedding
    model manifest (D5) beside it.

    Passing an explicit ``vector_store`` REQUIRES the matching
    ``persist_directory`` — that's where the BM25/manifest sidecars are
    written, independent of the vector_store object itself (langchain-chroma
    exposes no public way to recover a store's own directory). Omitting both
    uses the default store at ``CHROMA_PERSIST_DIR``.

    Returns:
        Number of documents added.
    """
    if not documents:
        return 0

    if vector_store is not None and persist_directory is None:
        raise ValueError(
            "add_documents was given an explicit vector_store but no "
            "persist_directory. The BM25 index and embedding-model manifest "
            "are written to persist_directory; defaulting it would silently "
            f"put them beside the default store ({CHROMA_PERSIST_DIR!r}) "
            "while the vectors live elsewhere, permanently desyncing hybrid "
            "retrieval. Pass the directory the vector_store persists to."
        )
    if persist_directory is None:
        persist_directory = CHROMA_PERSIST_DIR

    dropped, affected = _sanitize_metadata(documents)
    if dropped:
        logger.warning(
            "Dropped %d None-valued metadata field(s) across %d chunk(s)",
            dropped,
            affected,
        )

    if vector_store is None:
        vector_store = get_vector_store(persist_directory=persist_directory)

    _assert_index_write_allowed(vector_store, persist_directory)

    ids = [
        compute_chunk_id(doc.metadata.get("source", ""), doc.page_content)
        for doc in documents
    ]

    # Dedupe WITHIN this batch first: two chunks with identical text hash to
    # the same ID (D7 — identity means identity), but Chroma's get()/
    # add_documents() reject duplicate IDs in a single call outright rather
    # than deduping, so an un-deduped batch would raise DuplicateIDError.
    seen_in_batch = set()
    batch_docs = []
    batch_ids = []
    for doc, doc_id in zip(documents, ids):
        if doc_id in seen_in_batch:
            continue
        seen_in_batch.add(doc_id)
        batch_docs.append(doc)
        batch_ids.append(doc_id)

    existing = vector_store.get(ids=batch_ids)
    existing_ids = set(existing["ids"]) if existing["ids"] else set()

    new_docs = []
    new_ids = []
    for doc, doc_id in zip(batch_docs, batch_ids):
        if doc_id not in existing_ids:
            new_docs.append(doc)
            new_ids.append(doc_id)

    if new_docs:
        vector_store.add_documents(documents=new_docs, ids=new_ids)
        logger.info("Added %d new documents to vector store", len(new_docs))
        _write_embedding_model_manifest(persist_directory)
        _rebuild_bm25_index(vector_store, persist_directory)
    else:
        logger.info("No new documents to add (all duplicates)")

    return len(new_docs)


def sync_documents(
    source: str,
    documents: List[Document],
    vector_store: Optional[Chroma] = None,
    persist_directory: Optional[str] = None,
    rebuild_bm25: bool = True,
) -> Dict[str, int]:
    """Make the store's chunks for ``source`` exactly equal ``documents``.

    Unlike :func:`add_documents` — insert-only, which can never remove or
    correct a chunk already in the store — this is a per-source *replace*: it
    adds new chunks, corrects the metadata of chunks whose text is unchanged,
    and deletes chunks the source no longer produces. It is scoped strictly to
    one ``source``; chunks belonging to other documents are never touched.

    IDs are source-scoped content hashes (:func:`compute_chunk_id`), so the
    notion of "the same chunk" here is "the same ``(source, text)``". A chunk
    whose *text* changed is therefore not an in-place update but a delete of
    the old ID plus an add of the new one; only a *metadata-only* drift (e.g. a
    chunker fix that moved a page or section number while leaving the text
    identical) is handled as an in-place update — content-hash identity alone
    would otherwise silently keep the stale metadata.

    Write ordering is deliberate and must not be reordered — **add, then
    update, then delete**:

    * **Add before delete (converge-up).** If the process dies between the add
      and the delete, the store holds the *union* of old and new chunks:
      harmless extras that the next sync deletes. The reverse order could die
      with chunks already deleted and their replacements not yet added, leaving
      the source under-represented — silent missing evidence, the one failure
      this pipeline must never have.
    * **The BM25 delete-rebuild trap.** ``_rebuild_bm25_index`` rebuilds the
      lexical sidecar from the vector store's *full* current contents. A sync
      that only *deletes* (a source shrank, or ``sync_documents(source, [])``
      clears it) must still rebuild: a BM25 index left untouched would keep
      returning deleted chunks as lexical hits that no longer exist in the
      vector store, desyncing hybrid retrieval. Hence the rebuild fires on
      added *or* updated *or* deleted — not on adds alone like
      :func:`add_documents`.

    ``sync_documents(source, [])`` is the delete-all-for-``source`` form: every
    stored chunk for that source is removed and the BM25 index rebuilt.

    Source authority: each doc's ``metadata["source"]`` must equal ``source``
    or be absent/empty. Absent/empty is filled in with ``source``; a
    *different*, non-empty value raises ``ValueError`` — silently overwriting
    it would let a chunk be stored under one source's ID while claiming
    another, corrupting chunk identity.

    Passing an explicit ``vector_store`` REQUIRES the matching
    ``persist_directory`` (same rule and rationale as :func:`add_documents` —
    the BM25/manifest sidecars are written there).

    Args:
        source: The document identity whose chunks are being (re)synced.
        documents: The complete, current set of chunks for ``source``. Chunks
            for other sources must not be included.
        vector_store: Optional explicit store; if given, ``persist_directory``
            is required.
        persist_directory: Where the BM25 index and embedding-model manifest
            live. Defaults to ``CHROMA_PERSIST_DIR`` only when no explicit
            ``vector_store`` was passed.
        rebuild_bm25: When True (default), a sync that changed anything also
            rewrites the embedding-model manifest and rebuilds the BM25 sidecar
            from the store's full contents. Set False by a *batch* indexer
            syncing many sources in a loop: each rebuild scans the whole store,
            so rebuilding per source is O(N x total_chunks); the batch caller
            instead defers and calls :func:`rebuild_bm25_index` exactly once
            after the loop. The vector-store writes and the returned counts are
            unaffected either way.

    Returns:
        ``{"added": n, "updated": n, "deleted": n}``.
    """
    if vector_store is not None and persist_directory is None:
        raise ValueError(
            "sync_documents was given an explicit vector_store but no "
            "persist_directory. The BM25 index and embedding-model manifest "
            "are written to persist_directory; defaulting it would silently "
            f"put them beside the default store ({CHROMA_PERSIST_DIR!r}) "
            "while the vectors live elsewhere, permanently desyncing hybrid "
            "retrieval. Pass the directory the vector_store persists to."
        )
    if persist_directory is None:
        persist_directory = CHROMA_PERSIST_DIR

    dropped, affected = _sanitize_metadata(documents)
    if dropped:
        logger.warning(
            "Dropped %d None-valued metadata field(s) across %d chunk(s)",
            dropped,
            affected,
        )

    # Source authority: fill a missing/empty source, reject a conflicting one.
    for doc in documents:
        doc_source = doc.metadata.get("source")
        if not doc_source:
            doc.metadata["source"] = source
        elif doc_source != source:
            raise ValueError(
                f"sync_documents(source={source!r}) received a document whose "
                f"metadata['source'] is {doc_source!r}. Refusing to overwrite "
                "it: a chunk stored under one source's ID while claiming "
                "another would corrupt chunk identity. Sync each source "
                "separately."
            )

    if vector_store is None:
        vector_store = get_vector_store(persist_directory=persist_directory)

    manifest_present = _assert_index_write_allowed(vector_store, persist_directory)

    # desired = target state, deduped within this batch. Identical text under
    # one source hashes to one ID, and Chroma rejects duplicate IDs in a single
    # add call, so keep the first occurrence (matching add_documents).
    desired_ids: List[str] = []
    desired_docs: List[Document] = []
    seen_in_batch = set()
    for doc in documents:
        doc_id = compute_chunk_id(source, doc.page_content)
        if doc_id in seen_in_batch:
            continue
        seen_in_batch.add(doc_id)
        desired_ids.append(doc_id)
        desired_docs.append(doc)
    desired_by_id = dict(zip(desired_ids, desired_docs))

    # existing = everything currently stored for this source (ids + metadata).
    existing = vector_store.get(where={"source": source}, include=["metadatas"])
    existing_ids = existing["ids"] or []
    existing_metadatas = existing["metadatas"] or [{} for _ in existing_ids]
    existing_meta_by_id = dict(zip(existing_ids, existing_metadatas))
    existing_id_set = set(existing_ids)

    # 1) ADD new chunks FIRST (converge-up crash safety — see docstring).
    new_ids = [i for i in desired_ids if i not in existing_id_set]
    if new_ids and not manifest_present and not rebuild_bm25:
        # Deferred-BM25 batch on a fresh store: no per-source manifest write
        # follows, so record the model before the first vectors land. Without
        # it the next source's guard would see a non-empty store with no
        # manifest and refuse it as a legacy index.
        _write_embedding_model_manifest(persist_directory)
    if new_ids:
        new_docs = [desired_by_id[i] for i in new_ids]
        vector_store.add_documents(documents=new_docs, ids=new_ids)

    # 2) UPDATE surviving chunks whose metadata drifted, in place. update_docs
    #    is re-embedded by the wrapper, so restrict it to genuine metadata
    #    diffs — an unchanged chunk must not be re-embedded.
    update_ids: List[str] = []
    update_docs: List[Document] = []
    for doc_id in desired_ids:
        if doc_id not in existing_id_set:
            continue
        if desired_by_id[doc_id].metadata != existing_meta_by_id.get(doc_id, {}):
            update_ids.append(doc_id)
            update_docs.append(desired_by_id[doc_id])
    if update_ids:
        vector_store.update_documents(ids=update_ids, documents=update_docs)

    # 3) DELETE stale chunks LAST (chunks this source no longer produces).
    stale_ids = [i for i in existing_ids if i not in seen_in_batch]
    if stale_ids:
        vector_store.delete(ids=stale_ids)

    added, updated, deleted = len(new_ids), len(update_ids), len(stale_ids)
    if (added or updated or deleted) and rebuild_bm25:
        _write_embedding_model_manifest(persist_directory)
        _rebuild_bm25_index(vector_store, persist_directory)
    logger.info(
        "sync_documents(source=%r): added=%d updated=%d deleted=%d",
        source,
        added,
        updated,
        deleted,
    )
    return {"added": added, "updated": updated, "deleted": deleted}


def rebuild_bm25_index(
    vector_store: Optional[Chroma] = None,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> None:
    """Rewrite the embedding-model manifest and BM25 sidecar from the store's
    full current contents (public entry point).

    Factored out so a batch indexer can sync many sources with
    ``sync_documents(..., rebuild_bm25=False)`` and then rebuild the global
    lexical index exactly ONCE at the end, instead of paying an
    O(total_chunks) rebuild per source (O(N x total_chunks) across N sources —
    each rebuild scans the whole store). The empty-store case is handled inside
    :func:`_rebuild_bm25_index`: an empty store leaves no sidecar rather than
    trying to build a BM25 index over an empty corpus.

    Args:
        vector_store: The store to rebuild from; opened at ``persist_directory``
            if omitted.
        persist_directory: Where the BM25 sidecar and embedding-model manifest
            live.
    """
    if vector_store is None:
        vector_store = get_vector_store(persist_directory=persist_directory)
    _assert_index_write_allowed(vector_store, persist_directory)
    _write_embedding_model_manifest(persist_directory)
    _rebuild_bm25_index(vector_store, persist_directory)


def _rebuild_bm25_index(vector_store: Chroma, persist_directory: str) -> None:
    """Rebuild the BM25 index from the store's full current contents.

    rank_bm25 has no incremental-update API, so a from-scratch rebuild against
    the vector store's own authoritative contents (rather than tracking deltas
    ourselves) is what keeps the two indexes from drifting apart.
    """
    stored = vector_store.get(include=["documents", "metadatas"])
    ids = stored["ids"]
    if not ids:
        # rank_bm25 cannot represent an empty corpus — BM25Okapi([]) divides by
        # zero computing the average document length. An ABSENT sidecar is the
        # correct artifact for an empty store: load_bm25_index returns None, so
        # retrieval falls back to vector-only, which also returns nothing on an
        # empty store. Delete any stale pickle so it can't ghost the chunks that
        # were just deleted (returning them as lexical hits the store no longer
        # holds).
        (Path(persist_directory) / BM25_FILENAME).unlink(missing_ok=True)
        return
    texts = stored["documents"]
    metadatas = stored["metadatas"] or [{} for _ in ids]
    documents = [
        Document(page_content=text, metadata=metadata or {})
        for text, metadata in zip(texts, metadatas)
    ]
    index = build_bm25_index(ids, documents)
    save_bm25_index(index, persist_directory)


def clear_store(persist_directory: str = CHROMA_PERSIST_DIR) -> None:
    """Delete the vector store directory to reset it."""
    try:
        shutil.rmtree(persist_directory)
        logger.info("Cleared vector store at %s", persist_directory)
    except FileNotFoundError:
        logger.info("Vector store directory does not exist: %s", persist_directory)
