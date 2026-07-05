"""Hybrid (BM25 + vector) retrieval module, fused by reciprocal rank fusion (D6)."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document

from src.bm25_index import load_bm25_index, search_bm25
from src.embedder import assert_embedding_model, get_vector_store

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATE_POOL = 12


def retrieve(
    query: str,
    top_k: int = 5,
    document_type: Optional[str] = None,
    persist_directory: str = "./chroma_db",
) -> List[Dict[str, Any]]:
    """Retrieve the most relevant document chunks for a query.

    Hybrid retrieval (D6): BM25 and vector search each contribute a ranked
    candidate list (~max(12, top_k) each); results are fused by reciprocal
    rank fusion (score = sum(1 / (60 + rank))) and the top_k fused results are
    returned. Each arm degrades independently — a failure in one doesn't
    block the other — and retrieval falls back to vector-only if no BM25
    index has been built yet (e.g. an index that predates Phase 3).

    Args:
        query: The search query.
        top_k: Number of results to return.
        document_type: Optional filter for document type (e.g., "legislation", "case_law").
        persist_directory: ChromaDB persistence directory.

    Returns:
        List of dicts with keys: document, score (fused RRF score), metadata.
    """
    assert_embedding_model(persist_directory)

    vector_store = get_vector_store(persist_directory=persist_directory)
    candidate_k = max(CANDIDATE_POOL, top_k)
    filter_dict = {"document_type": document_type} if document_type else None

    id_to_doc: Dict[str, Document] = {}

    vector_ranked_ids: List[str] = []
    try:
        vector_results = vector_store.similarity_search_with_relevance_scores(
            query, k=candidate_k, filter=filter_dict
        )
        for doc, _score in vector_results:
            if doc.id is None:
                continue
            vector_ranked_ids.append(doc.id)
            id_to_doc[doc.id] = doc
    except Exception as e:
        logger.error("Error during vector retrieval: %s", e)

    bm25_ranked_ids: List[str] = []
    bm25_index = load_bm25_index(persist_directory)
    if bm25_index is not None:
        try:
            for doc_id, doc, _score in search_bm25(
                bm25_index, query, candidate_k, document_type
            ):
                bm25_ranked_ids.append(doc_id)
                id_to_doc.setdefault(doc_id, doc)
        except Exception as e:
            logger.error("Error during BM25 retrieval: %s", e)
    else:
        logger.warning(
            "No BM25 index found at %s; falling back to vector-only retrieval",
            persist_directory,
        )

    if not vector_ranked_ids and not bm25_ranked_ids:
        return []

    fused = _reciprocal_rank_fusion(vector_ranked_ids, bm25_ranked_ids)

    return [
        {
            "document": id_to_doc[doc_id],
            "score": score,
            "metadata": id_to_doc[doc_id].metadata,
        }
        for doc_id, score in fused[:top_k]
    ]


def _reciprocal_rank_fusion(
    *ranked_id_lists: List[str], k: int = RRF_K
) -> List[Tuple[str, float]]:
    """Fuse ranked ID lists by reciprocal rank: score = sum(1 / (k + rank)).

    Fusing on rank rather than raw score sidesteps the two arms having
    incomparable score scales (BM25 scores are unbounded; Chroma's relevance
    scores are not reliably normalised to [0, 1] — see the fake-embeddings
    warning in the test suite).
    """
    scores: Dict[str, float] = {}
    for ranked_ids in ranked_id_lists:
        for rank, doc_id in enumerate(ranked_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


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
