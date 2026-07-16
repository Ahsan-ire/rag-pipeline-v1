"""Hybrid (BM25 + vector) retrieval module, fused by reciprocal rank fusion (D6)."""

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.bm25_index import BM25Index, load_bm25_index, search_bm25
from src.chunker import locator_label
from src.embedder import CHROMA_PERSIST_DIR, assert_embedding_model, get_vector_store

logger = logging.getLogger(__name__)

RRF_K = 60
# Total weight BUDGET for the whole rewrite bundle, relative to one original
# arm (Phase 13, D43; gate fix 14 Jul 2026). Each rewrite-derived ranked list
# is weighted REWRITE_LIST_WEIGHT / n_rewrites (n_rewrites = number of
# effective rewrite sub-queries), so across ANY arm count A and ANY rewrite
# count N the whole rewrite bundle contributes at most REWRITE_LIST_WEIGHT ×
# the original query's full-agreement score: A arms × N rewrites ×
# (0.5/N)/(60+r) = 0.5 × A/(60+r), i.e. half of the original's A×1.0/(60+r).
# A FLAT 0.5 per list (the pre-fix value) failed the real production shape:
# 3 rewrites × 2 arms = 6 lists × 0.5 = 3.0 outvoted the original's 2.0 (D43's
# stated invariant). The per-budget form closes that hole for every A and N.
REWRITE_LIST_WEIGHT = 0.5
# Per-list weight for the INTENT reframe (Phase 14, D50). PROVISIONAL — the
# offline W sweep (WS5) picks the final value; retrieve() uses this constant
# whenever the caller passes no explicit ``intent_weight``.
#
# Dominance invariant (equal-rank, hybrid): the original query's two-arm
# agreement scores 2/(RRF_K+1); the worst-case CORRELATED noise a rewrite
# bundle (at most 1/(RRF_K+1) across all arms) plus the intent's own pair of
# lists (2W/(RRF_K+1)) can pile onto one generic chunk is (1+2W)/(RRF_K+1). For
# the original to keep winning, 2/(RRF_K+1) > (1+2W)/(RRF_K+1), i.e. 2 > 1+2W,
# i.e. **W ≤ 0.5** (at exactly 0.5 it is a tie, broken in the original's favour
# because the original lists are fused — inserted into the score dict — first).
# So intent weights are capped at 0.5; anything above is rejected, never
# silently applied. (The rank-asymmetry caveat — noise at rank 1 can still beat
# original agreement at rank 12, (1+2W)/61 vs 2/72 — is inherent to RRF and
# true even at W=0 for the surface bundle; it is not introduced by the intent.)
INTENT_LIST_WEIGHT = 0.5
CANDIDATE_POOL = 12
DEFAULT_TOP_K = 6  # plan line 67: fuse ~12 per arm, return top-k (default 6)

# The three retrieval modes, single source of truth (Phase 10 ablation, D38):
# "hybrid" runs both arms and fuses; "vector"/"bm25" run one arm only, for the
# eval ablation that measures each arm's standalone contribution. The evaluator
# imports this so the CLI, the matrix runner, and retrieve() can never disagree
# on the valid mode set.
RETRIEVAL_MODES = ("hybrid", "vector", "bm25")


def load_retrieval_context(
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Tuple[Chroma, Optional[BM25Index]]:
    """Build the injection args for :func:`retrieve` exactly once.

    The one blessed way to construct ``(vector_store, bm25_index)`` for
    injection into ``retrieve``: it runs :func:`assert_embedding_model` (so an
    injector can never forget the corpus/query model-match check — ``retrieve``
    itself skips that check when a store is injected), opens the Chroma store,
    and loads the BM25 sidecar. Callers that score many queries against one
    index (the evaluator over a golden set, the CLI query path) call this once
    and reuse the pair, instead of paying a fresh store open + BM25 unpickle +
    manifest check on every single ``retrieve`` call.

    Args:
        persist_directory: ChromaDB persistence directory; the BM25 sidecar and
            embedding-model manifest live beside it.

    Returns:
        ``(vector_store, bm25_index)``; ``bm25_index`` is ``None`` when no
        sidecar has been built yet (an index predating Phase 3, or an empty
        store), in which case ``retrieve`` degrades to vector-only.
    """
    assert_embedding_model(persist_directory)
    vector_store = get_vector_store(persist_directory=persist_directory)
    bm25_index = load_bm25_index(persist_directory)
    return vector_store, bm25_index


def retrieve(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    document_type: Optional[str] = None,
    persist_directory: str = CHROMA_PERSIST_DIR,
    vector_store: Optional[Chroma] = None,
    bm25_index: Optional[BM25Index] = None,
    *,
    mode: str = "hybrid",
    strict_errors: bool = False,
    rewrites: Optional[Sequence[str]] = None,
    intent_rewrite: Optional[str] = None,
    intent_weight: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Retrieve the most relevant document chunks for a query.

    Hybrid retrieval (D6): BM25 and vector search each contribute a ranked
    candidate list (~max(12, top_k) each); results are fused by reciprocal
    rank fusion (score = sum(weight / (60 + rank))) and the top_k fused
    results are returned. Each arm degrades independently — a failure in one
    doesn't block the other — and retrieval falls back to vector-only if no
    BM25 index has been built yet (e.g. an index that predates Phase 3).

    Multi-query weighted retrieval (Phase 13, D43/D45): ``rewrites`` is an
    optional list of LLM-generated alternative phrasings of ``query``. Each
    used arm runs once per sub-query — ``query`` itself, then every rewrite
    that survives dedup — instead of once per arm, so a vocabulary mismatch
    between staff phrasing and the corpus's register can be caught by a
    rewrite even when the original phrasing's own vector/BM25 rankings miss
    the right chunk entirely. Every (arm, sub-query) pair contributes its own
    ranked-ID list to the fusion, weighted 1.0 for lists derived from the
    original query and ``REWRITE_LIST_WEIGHT / n_rewrites`` for each
    rewrite-derived list, so the WHOLE rewrite bundle — N rewrites across every
    arm — contributes at most ``REWRITE_LIST_WEIGHT`` (0.5) × the original
    query's full-agreement score for ANY arm count and ANY N: N correlated
    rewrites agreeing on a generic chunk cannot outvote the original query's
    arms agreeing on the right one (D43).
    ``CANDIDATE_POOL``/``RRF_K``/``DEFAULT_TOP_K`` are unchanged — D45
    rescinded pool widening; recall breadth comes from more queries, not
    deeper per-query candidate lists. Dedup: a rewrite is dropped if it is
    empty after ``str.strip()``, or is a casefold-duplicate of ``query`` or
    of an earlier surviving rewrite. ``rewrites=None`` or empty ⇒
    ``sub_queries == [query]``, so every arm runs exactly the single call it
    always did — byte-identical to retrieval before Phase 13.

    Intent reframe (Phase 14, D50): ``intent_rewrite`` is an optional
    intent-level restatement of ``query`` (see ``query_rewrite.Expansion``). It
    is fused as its OWN pair of ranked lists — one per arm the mode runs (hybrid
    contributes bm25+vector, single-arm modes contribute that one arm),
    mirroring the surface bundle's structure but on a SEPARATE weight budget:
    each intent-derived list is weighted ``W`` (``intent_weight`` if given, else
    the module constant ``INTENT_LIST_WEIGHT``). ``W`` is bounded to ``[0, 0.5]``
    by the equal-rank dominance invariant documented on ``INTENT_LIST_WEIGHT`` —
    a value above 0.5 (or below 0) raises ``ValueError`` rather than silently
    letting a correlated intent pair outvote the original query. The intent is
    deduped like a rewrite (dropped if empty after strip, or a casefold-dup of
    ``query`` or of a surviving rewrite). ``intent_rewrite=None`` (or empty) adds
    zero extra lists, so behavior is byte-identical to retrieval before Phase 14.

    Retrieval mode (Phase 10 ablation, D38): ``mode`` is one of
    ``RETRIEVAL_MODES``. ``"hybrid"`` (default) runs both arms and fuses;
    ``"vector"`` runs the vector arm only and never touches the BM25 sidecar
    (no missing-sidecar warning); ``"bm25"`` runs the BM25 arm only and never
    opens Chroma or runs the embedding-model manifest check. The single-arm
    modes exist so the evaluator can measure each arm's standalone hit@k. Mode
    gates BOTH which backend is resolved (a non-injected arm the mode doesn't
    use is never loaded) AND which arm executes (an *injected* arm the mode
    doesn't use is ignored). ``bm25`` mode with no sidecar warns and returns
    ``[]`` (fail-visible), rather than silently degrading to the other arm.

    Error handling (``strict_errors``): by default an arm that raises is logged
    and treated as contributing no candidates for that sub-query — production
    retrieval degrades rather than 500s. Eval runs pass ``strict_errors=True``
    so an operational failure (a corrupt store, an OOM) on ANY sub-query
    PROPAGATES instead of being silently scored as a retrieval miss, which
    would corrupt the benchmark. A *missing* BM25 sidecar is not an error and
    is unaffected by this flag (bm25 mode still warns + returns []; hybrid
    still degrades to vector-only).

    Phase 9 (load-once retrieval): ``vector_store`` and ``bm25_index`` can be
    injected by a caller that builds them once and reuses them across many
    ``retrieve`` calls (e.g. the evaluator, scoring dozens of golden
    questions), avoiding a fresh Chroma wrapper + BM25 unpickle per call.

    - ``vector_store`` given: it is used as-is and ``assert_embedding_model``
      is SKIPPED — the caller that built the store already owns that check
      (it ran it, if at all, when it first opened the store). ``vector_store``
      omitted (None): current behavior — ``assert_embedding_model`` runs and
      the store is opened fresh via ``get_vector_store(persist_directory=...)``.
    - ``bm25_index`` given: it is used as-is and ``load_bm25_index`` is
      SKIPPED. ``bm25_index`` omitted (None): current behavior — the index is
      loaded from disk, falling back to vector-only with a warning if none is
      found (or the pickle is unreadable).

    Args:
        query: The search query.
        top_k: Number of results to return.
        document_type: Optional filter for document type (e.g., "legislation", "case_law").
        persist_directory: ChromaDB persistence directory. Only consulted for
            whichever of ``vector_store``/``bm25_index`` was not injected.
        vector_store: Optional pre-built Chroma store to search directly,
            instead of opening one at ``persist_directory``.
        bm25_index: Optional pre-loaded BM25 index to search directly, instead
            of loading one from ``persist_directory``.
        mode: Retrieval mode, one of ``RETRIEVAL_MODES``
            (``"hybrid"``/``"vector"``/``"bm25"``); see the mode note above.
            An unknown mode raises ``ValueError`` before any IO.
        strict_errors: When True, an arm exception on any sub-query propagates
            instead of being swallowed and scored as no candidates; see the
            error-handling note above.
        rewrites: Optional alternative phrasings of ``query`` (Phase 13,
            D43); see the multi-query weighted retrieval note above.
        intent_rewrite: Optional intent-level restatement of ``query`` (Phase
            14, D50); see the intent-reframe note above. Keyword-only.
        intent_weight: Optional per-list weight ``W`` for the intent's ranked
            lists; defaults to ``INTENT_LIST_WEIGHT`` when None. Must be in
            ``[0, 0.5]`` (the dominance invariant). Keyword-only.

    Returns:
        List of dicts with keys: document, score (fused RRF score), metadata.

    Raises:
        ValueError: If ``mode`` is not one of ``RETRIEVAL_MODES``, if ``top_k``
            is less than 1, or if the intent weight in effect (``intent_weight``
            when given, else ``INTENT_LIST_WEIGHT``) falls outside ``[0, 0.5]``.
    """
    if mode not in RETRIEVAL_MODES:
        # Validate before any IO so a typo'd mode fails fast and cheap, never
        # after opening the store or unpickling the sidecar.
        raise ValueError(
            f"Unknown retrieval mode {mode!r}; expected one of {RETRIEVAL_MODES}"
        )
    if top_k < 1:
        # top_k < 1 is meaningless (a fused[:0] would silently return nothing,
        # and top_k=0 is the negative-slice hazard the eval matrix guards
        # against too). Validate at THIS boundary — the single point all callers
        # funnel through — so a bad top_k fails fast and cheap here, before any
        # IO, rather than degrading downstream. No UPPER bound: top_k > the
        # candidate pool is deliberately supported (candidate_k = max(
        # CANDIDATE_POOL, top_k) widens both arms), so the blocked-answer UI can
        # advise raising --top-k.
        raise ValueError(f"top_k must be >= 1, got {top_k}")

    # Intent reframe (Phase 14, D50): resolve the intent sub-query and its
    # per-list weight BEFORE any IO so a bad weight fails fast and cheap, like a
    # bad mode or top_k. An empty/None intent is a no-op (byte-identical to
    # pre-Phase-14 retrieval); only a non-empty intent triggers the weight
    # validation, so passing a stray ``intent_weight`` with no intent stays
    # inert. The bound is the equal-rank dominance invariant on
    # ``INTENT_LIST_WEIGHT``: W in [0, 0.5], never above 0.5 silently.
    intent_query = (intent_rewrite or "").strip()
    intent_list_weight = (
        INTENT_LIST_WEIGHT if intent_weight is None else intent_weight
    )
    if intent_query and not (0.0 <= intent_list_weight <= 0.5):
        raise ValueError(
            f"intent weight must be in [0, 0.5], got {intent_list_weight}: "
            f"W > 0.5 would let a correlated intent pair (2W/{RRF_K + 1}) plus "
            f"rewrite noise (1/{RRF_K + 1}) outvote the original query's two-arm "
            f"agreement (2/{RRF_K + 1}) at equal rank (see INTENT_LIST_WEIGHT)."
        )

    # Which arms this mode uses. Mode gates resolution AND execution: a
    # non-injected arm the mode doesn't use is never loaded (vector mode never
    # unpickles the BM25 sidecar; bm25 mode never opens Chroma or runs the
    # manifest check), and an *injected* arm the mode doesn't use is ignored.
    use_vector = mode in ("hybrid", "vector")
    use_bm25 = mode in ("hybrid", "bm25")

    # Resolve the backing objects for only the arm(s) this mode uses (load-once
    # injection, D37). Fill whichever used arm was left un-injected, preserving
    # the per-arm skip semantics injecting callers rely on (a store injected
    # without a bm25_index still loads the sidecar from disk exactly once, and
    # never re-opens the store). Threading mode through here is what keeps
    # single-arm eval runs from paying — or crashing on — the other arm's IO.
    if use_vector and vector_store is None:
        assert_embedding_model(persist_directory)
        vector_store = get_vector_store(persist_directory=persist_directory)
    if use_bm25 and bm25_index is None:
        bm25_index = load_bm25_index(persist_directory)

    candidate_k = max(CANDIDATE_POOL, top_k)
    filter_dict = {"document_type": document_type} if document_type else None

    # Sub-queries actually searched (Phase 13, D43): the original query
    # first, then each rewrite that is non-empty after strip() and not a
    # casefold-duplicate of the original or of an earlier surviving rewrite.
    # rewrites=None/empty leaves sub_queries == [query], so every arm below
    # makes exactly the one call it always made — byte-identical to
    # pre-Phase-13 retrieve().
    sub_queries: List[str] = [query]
    # A parallel per-sub-query weight, one entry per sub_queries entry: 1.0 for
    # the original, per_rewrite_weight for each surface rewrite, and W for the
    # intent reframe (appended last). ``seen`` dedups every sub-query — the
    # original, the rewrites, AND the intent — against each other by casefold.
    sub_query_weights: List[float] = [1.0]
    seen = {query.casefold()}
    if rewrites:
        for rewrite in rewrites:
            candidate = rewrite.strip()
            if not candidate or candidate.casefold() in seen:
                continue
            seen.add(candidate.casefold())
            sub_queries.append(candidate)

    # Per-rewrite-list weight (Phase 13, D43; gate fix): split the fixed
    # REWRITE_LIST_WEIGHT budget evenly across the N EFFECTIVE rewrite
    # sub-queries, so the whole rewrite bundle (N rewrites × every arm) can
    # contribute at most REWRITE_LIST_WEIGHT × an original arm's full agreement
    # for ANY arm count and ANY N — see REWRITE_LIST_WEIGHT's comment.
    # ``max(1, ...)`` guards the no-rewrite case (n_rewrites == 0), though the
    # weight is then never consulted (only sub_queries[0] runs).
    n_rewrites = len(sub_queries) - 1
    per_rewrite_weight = REWRITE_LIST_WEIGHT / max(1, n_rewrites)
    sub_query_weights.extend([per_rewrite_weight] * n_rewrites)

    # Intent reframe (Phase 14, D50): append it as ONE MORE sub-query weighted W
    # on its own budget — NOT part of the rewrite bundle, so per_rewrite_weight
    # above is computed from n_rewrites alone and is unaffected. Deduped against
    # every existing sub-query (mirroring the rewrite dedup); dropped when it
    # collapses to a duplicate or is empty. An intent that survives runs on each
    # used arm exactly like a rewrite, so single-arm modes contribute one intent
    # list and hybrid contributes two — mirroring the surface bundle.
    if intent_query and intent_query.casefold() not in seen:
        seen.add(intent_query.casefold())
        sub_queries.append(intent_query)
        sub_query_weights.append(intent_list_weight)

    id_to_doc: Dict[str, Document] = {}
    # One ranked-ID list per (arm × sub-query), with a parallel per-list
    # weight (1.0 for the original query's lists, REWRITE_LIST_WEIGHT for a
    # rewrite's) — fused together below instead of one list per arm.
    ranked_lists: List[List[str]] = []
    list_weights: List[float] = []

    if use_vector:
        for i, sub_q in enumerate(sub_queries):
            vector_ranked_ids: List[str] = []
            try:
                vector_results = vector_store.similarity_search_with_relevance_scores(
                    sub_q, k=candidate_k, filter=filter_dict
                )
                for doc, _score in vector_results:
                    if doc.id is None:
                        continue
                    vector_ranked_ids.append(doc.id)
                    id_to_doc[doc.id] = doc
            except Exception as e:
                if strict_errors:
                    raise
                logger.error("Error during vector retrieval: %s", e)
            ranked_lists.append(vector_ranked_ids)
            list_weights.append(sub_query_weights[i])

    if use_bm25:
        if bm25_index is not None:
            for i, sub_q in enumerate(sub_queries):
                bm25_ranked_ids: List[str] = []
                try:
                    for doc_id, doc, _score in search_bm25(
                        bm25_index, sub_q, candidate_k, document_type
                    ):
                        # BM25-sidecar Documents are stored without .id; attach the
                        # store id here so downstream consumers (audit log) never
                        # have to re-derive it by re-hashing the chunk text.
                        if doc.id is None:
                            doc.id = doc_id
                        bm25_ranked_ids.append(doc_id)
                        id_to_doc.setdefault(doc_id, doc)
                except Exception as e:
                    if strict_errors:
                        raise
                    logger.error("Error during BM25 retrieval: %s", e)
                ranked_lists.append(bm25_ranked_ids)
                list_weights.append(sub_query_weights[i])
        else:
            # A missing sidecar is not an operational error (unaffected by
            # strict_errors): hybrid degrades to vector-only, bm25 mode returns
            # [] below. Either way, say so rather than failing silently.
            logger.warning(
                "No BM25 index found at %s; %s",
                persist_directory,
                "returning no results for bm25-only mode"
                if mode == "bm25"
                else "falling back to vector-only retrieval",
            )

    if not any(ranked_lists):
        return []

    fused = _reciprocal_rank_fusion(*ranked_lists, weights=list_weights)

    return [
        {
            "document": id_to_doc[doc_id],
            "score": score,
            "metadata": id_to_doc[doc_id].metadata,
        }
        for doc_id, score in fused[:top_k]
    ]


def _reciprocal_rank_fusion(
    *ranked_id_lists: List[str],
    k: int = RRF_K,
    weights: Optional[Sequence[float]] = None,
) -> List[Tuple[str, float]]:
    """Fuse ranked ID lists by weighted reciprocal rank: score = sum(weight / (k + rank)).

    Fusing on rank rather than raw score sidesteps the two arms having
    incomparable score scales (BM25 scores are unbounded; Chroma's relevance
    scores are not reliably normalised to [0, 1] — see the fake-embeddings
    warning in the test suite).

    ``weights`` (Phase 13, D43) assigns a per-list multiplier — e.g. 1.0 for
    an original-query list and ``REWRITE_LIST_WEIGHT`` for a rewrite-derived
    one, so several correlated rewrite lists agreeing on a generic chunk
    cannot outvote two original-query arms agreeing on the right one.
    ``weights=None`` weights every list 1.0 (today's behavior, unchanged).
    ``weights``, when given, must have exactly one entry per entry in
    ``ranked_id_lists``.

    Raises:
        ValueError: If ``weights`` is given and its length does not match
            the number of ``ranked_id_lists``.
    """
    if weights is not None and len(weights) != len(ranked_id_lists):
        raise ValueError(
            f"weights must have exactly one entry per ranked_id_lists "
            f"({len(ranked_id_lists)}); got {len(weights)}"
        )
    scores: Dict[str, float] = {}
    for i, ranked_ids in enumerate(ranked_id_lists):
        weight = 1.0 if weights is None else weights[i]
        for rank, doc_id in enumerate(ranked_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def format_context(results: List[Dict[str, Any]]) -> str:
    """Format retrieved results into a context string for the LLM prompt.

    Each chunk gets a citation header the model is asked to echo. Handbook chunks
    use the compact ``[Handbook, para 3.2.1, p.87]`` locator (Phase 4 / D28), built
    from the chunk's ``section_number`` and page span; other document types keep
    the generic ``[Source i: title | source]`` header so legislation/case-law
    routing is unaffected.
    """
    if not results:
        return "No relevant documents found."

    context_parts = []
    for i, result in enumerate(results, 1):
        doc = result["document"]
        if doc.metadata.get("document_type") == "handbook":
            header = _handbook_header(doc.metadata)
        else:
            header = _generic_header(i, doc.metadata)
        context_parts.append(f"{header}\n{doc.page_content}\n---")

    return "\n\n".join(context_parts)


def _handbook_header(metadata: Dict[str, Any]) -> str:
    """Compact citation header for a handbook chunk: ``[Handbook, para X, p.N]``.

    ``para`` is omitted for chapter-intro chunks that carry no ``section_number``;
    an ``APPENDIX`` section renders verbatim with no ``para`` token instead
    (e.g. ``[Handbook, APPENDIX 14.1, p.87]``), mirroring the chunker's
    contextual prefix (``chunker._prefix``); the page renders as a range
    (``pp.1–2``) when the chunk spans pages, matching the D21 in-text prefix
    convention.
    """
    section = metadata.get("section_number", "")
    page_start = metadata.get("page_start")
    page_end = metadata.get("page_end")

    parts = ["Handbook"]
    if section:
        parts.append(locator_label(section))
    if page_start is not None:
        if page_end is not None and page_end != page_start:
            parts.append(f"pp.{page_start}–{page_end}")
        else:
            parts.append(f"p.{page_start}")
    return "[" + ", ".join(parts) + "]"


def _generic_header(index: int, metadata: Dict[str, Any]) -> str:
    """Fallback header for non-handbook document types (legislation, case law)."""
    title = metadata.get("title", "Unknown")
    source = metadata.get("source", "Unknown")
    section = metadata.get("section_number", "")

    header = f"[Source {index}: {title}"
    if section:
        header += f", Section {section}"
    header += f" | {source}]"
    return header
