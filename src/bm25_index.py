"""Lexical (BM25) index: built at index time, persisted beside the vector
store, and fused with vector search via reciprocal rank fusion (D6, D24).

Legal queries are exact-token-heavy (section numbers, form names, statute
titles); pure semantic search fuzzes past the literal token that matters.
Rebuilding from the vector store's full authoritative contents on every add
(see embedder.add_documents) keeps the two indexes in sync — rank_bm25 has
no incremental-update API, so a from-scratch rebuild is the simple, correct
option at this corpus size (~1,500 chunks).
"""

import logging
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

BM25_FILENAME = "bm25_index.pkl"
_TOKEN_PATTERN = re.compile(r"\w+")


def _tokenize(text: str) -> List[str]:
    """Lowercase word tokens. Deliberately simple: exact tokens are the point."""
    return _TOKEN_PATTERN.findall(text.lower())


@dataclass
class BM25Index:
    """A BM25 index paired with the chunk IDs/Documents it was built from.

    ``ids``/``documents`` share corpus order with the tokenized corpus inside
    ``bm25``, so a score at position i belongs to ``ids[i]``.
    """

    bm25: BM25Okapi
    ids: List[str]
    documents: List[Document]


def build_bm25_index(ids: List[str], documents: List[Document]) -> BM25Index:
    """Build a BM25 index over ``documents``, aligned 1:1 with ``ids``."""
    corpus = [_tokenize(doc.page_content) for doc in documents]
    return BM25Index(bm25=BM25Okapi(corpus), ids=ids, documents=documents)


def save_bm25_index(index: BM25Index, persist_directory: str) -> None:
    """Pickle the index next to the Chroma store."""
    path = Path(persist_directory) / BM25_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(index, f)


def load_bm25_index(persist_directory: str) -> Optional[BM25Index]:
    """Load the persisted BM25 index, or None if it hasn't been built yet."""
    path = Path(persist_directory) / BM25_FILENAME
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def search_bm25(
    index: BM25Index,
    query: str,
    top_k: int,
    document_type: Optional[str] = None,
) -> List[Tuple[str, Document, float]]:
    """Return up to ``top_k`` ``(id, document, score)`` triples for ``query``,
    best score first, optionally restricted to one ``document_type``.

    Only positive-scoring documents are returned. BM25 IDF goes non-positive
    for a term that appears in at least half the corpus, so a document with
    no real lexical overlap can otherwise tie at a "score" of ~0 alongside
    genuinely common terms — treating that as a ranked lexical match would
    misrepresent absence of evidence as weak evidence, and would dilute this
    arm's contribution to RRF fusion (retriever.py) for a query that has no
    real lexical signal in most of the corpus.
    """
    candidate_positions = range(len(index.ids))
    if document_type:
        candidate_positions = [
            i
            for i in candidate_positions
            if index.documents[i].metadata.get("document_type") == document_type
        ]
    if not candidate_positions:
        return []

    scores = index.bm25.get_scores(_tokenize(query))
    scored = [(i, scores[i]) for i in candidate_positions if scores[i] > 0]
    ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)[:top_k]
    return [(index.ids[i], index.documents[i], float(score)) for i, score in ranked]
