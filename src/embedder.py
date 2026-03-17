"""Embedding generation and ChromaDB vector storage module."""

import logging
import shutil
from typing import List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

CHROMA_PERSIST_DIR = "./chroma_db"
COLLECTION_NAME = "legal_documents"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def get_embedding_function() -> HuggingFaceEmbeddings:
    """Create and return a HuggingFace embedding function."""
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
    )


def add_documents(
    documents: List[Document],
    vector_store: Optional[Chroma] = None,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> int:
    """Add documents to the vector store with deduplication.

    Uses deterministic IDs based on source and chunk index to prevent duplicates.

    Returns:
        Number of documents added.
    """
    if not documents:
        return 0

    if vector_store is None:
        vector_store = get_vector_store(persist_directory=persist_directory)

    # Generate deterministic IDs
    ids = []
    for i, doc in enumerate(documents):
        source = doc.metadata.get("source", "unknown")
        doc_id = f"{source}::chunk_{i}"
        ids.append(doc_id)

    # Check for existing IDs and filter out duplicates
    try:
        existing = vector_store._collection.get(ids=ids)
        existing_ids = set(existing["ids"]) if existing["ids"] else set()
    except Exception:
        existing_ids = set()

    new_docs = []
    new_ids = []
    for doc, doc_id in zip(documents, ids):
        if doc_id not in existing_ids:
            new_docs.append(doc)
            new_ids.append(doc_id)

    if new_docs:
        vector_store.add_documents(documents=new_docs, ids=new_ids)
        logger.info("Added %d new documents to vector store", len(new_docs))
    else:
        logger.info("No new documents to add (all duplicates)")

    return len(new_docs)


def clear_store(persist_directory: str = CHROMA_PERSIST_DIR) -> None:
    """Delete the vector store directory to reset it."""
    try:
        shutil.rmtree(persist_directory)
        logger.info("Cleared vector store at %s", persist_directory)
    except FileNotFoundError:
        logger.info("Vector store directory does not exist: %s", persist_directory)
