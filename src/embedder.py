"""Embedding generation and ChromaDB vector storage module."""

import functools
import hashlib
import logging
import shutil
from pathlib import Path
from typing import List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

from src.bm25_index import build_bm25_index, save_bm25_index

logger = logging.getLogger(__name__)

CHROMA_PERSIST_DIR = "./chroma_db"
COLLECTION_NAME = "legal_documents"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_MODEL_MANIFEST = "embedding_model.txt"


@functools.lru_cache(maxsize=1)
def get_embedding_function() -> HuggingFaceEmbeddings:
    """Create and return the HuggingFace embedding function (cached).

    ``lru_cache`` means the underlying MiniLM model loads once per process
    instead of once per ``get_vector_store()`` call — before this, every
    ``retrieve()`` reloaded the model (35+ loads across one eval run). Safe
    because the object is stateless configuration and the function takes no
    arguments; callers that need a different embedding function (tests use
    FakeEmbeddings) already pass it explicitly and never hit this path.
    """
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


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


def compute_chunk_id(text: str) -> str:
    """Content-hash chunk ID (D7): identity means identity across re-chunking,
    unlike positional IDs which collide with different content the moment
    chunking changes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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

    ids = [compute_chunk_id(doc.page_content) for doc in documents]

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


def _rebuild_bm25_index(vector_store: Chroma, persist_directory: str) -> None:
    """Rebuild the BM25 index from the store's full current contents.

    rank_bm25 has no incremental-update API, so a from-scratch rebuild against
    the vector store's own authoritative contents (rather than tracking deltas
    ourselves) is what keeps the two indexes from drifting apart.
    """
    stored = vector_store.get(include=["documents", "metadatas"])
    ids = stored["ids"]
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
