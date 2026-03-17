"""Vector search retrieval module."""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document

from src.embedder import get_embedding_function, get_vector_store

logger = logging.getLogger(__name__)


def retrieve(
    query: str,
    top_k: int = 5,
    document_type: Optional[str] = None,
    persist_directory: str = "./chroma_db",
) -> List[Dict[str, Any]]:
    """Retrieve the most relevant document chunks for a query.

    Args:
        query: The search query.
        top_k: Number of results to return.
        document_type: Optional filter for document type (e.g., "legislation", "case_law").
        persist_directory: ChromaDB persistence directory.

    Returns:
        List of dicts with keys: document, score, metadata.
    """
    vector_store = get_vector_store(persist_directory=persist_directory)

    filter_dict = None
    if document_type:
        filter_dict = {"document_type": document_type}

    try:
        results = vector_store.similarity_search_with_relevance_scores(
            query, k=top_k, filter=filter_dict
        )
    except Exception as e:
        logger.error("Error during retrieval: %s", e)
        return []

    return [
        {
            "document": doc,
            "score": score,
            "metadata": doc.metadata,
        }
        for doc, score in results
    ]


def format_context(results: List[Dict[str, Any]]) -> str:
    """Format retrieved results into a context string for the LLM prompt.

    Each chunk is formatted with its source metadata for citation purposes.
    """
    if not results:
        return "No relevant documents found."

    context_parts = []
    for i, result in enumerate(results, 1):
        doc = result["document"]
        source = doc.metadata.get("source", "Unknown")
        title = doc.metadata.get("title", "Unknown")
        section = doc.metadata.get("section_number", "")

        header = f"[Source {i}: {title}"
        if section:
            header += f", Section {section}"
        header += f" | {source}]"

        context_parts.append(f"{header}\n{doc.page_content}\n---")

    return "\n\n".join(context_parts)
