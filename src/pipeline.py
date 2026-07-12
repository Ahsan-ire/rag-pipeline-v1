"""Pipeline orchestration and CLI entry point.

Usage:
    python -m src.pipeline index ./data/legislation/ --type legislation
    python -m src.pipeline query "What are the succession rights of a spouse?" --top-k 6
"""

import argparse
import logging
import sys
from typing import Any, Dict, Optional

from src.audit import (
    ACTION_BLOCKED_UNVERIFIED,
    ACTION_NO_RESULTS,
    ACTION_REFUSAL_SHOWN,
    ACTION_SHOWN,
    ACTION_SHOWN_UNVERIFIED_OVERRIDE,
    ACTION_SHOWN_WITH_WARNING,
    build_event,
    log_event,
)
from src.chunker import chunk_handbook, chunk_legal_document, locator_label
from src.embedder import (
    CHROMA_PERSIST_DIR,
    clear_store,
    rebuild_bm25_index,
    sync_documents,
)
from src.generator import generate_with_sources, is_refusal
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    REFUSAL,
)
from src.ingest import (
    load_directory,
    load_handbook_pdf,
    load_html_from_url,
    load_pdf,
)
from src.retriever import DEFAULT_TOP_K, load_retrieval_context, retrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def index_documents(
    source_path: str,
    document_type: str = "handbook",
    reset: bool = False,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> int:
    """Ingest, chunk, embed, and store documents from a source path or URL.

    Indexing is a per-source SYNC (D37): the store's contents for each source
    are made to exactly match the freshly chunked documents — new chunks are
    added, metadata-only drift is updated in place, and stale chunks (text that
    no longer exists after a chunker or corpus change) are deleted, with the
    BM25 sidecar rebuilt whenever anything changed. ``--reset`` is retained for
    full rebuilds (e.g. after an ID-scheme or embedding-model change).

    Args:
        source_path: A file path, directory path, or URL.
        document_type: Type of document (handbook, legislation, case_law,
            contracts). ``handbook`` PDFs use the page-aware loader and the
            handbook chunker (Phase 2); everything else keeps the original path.
        reset: If True, clear the vector store before indexing.
        persist_directory: Vector-store directory (BM25 sidecar and model
            manifest live beside it).

    Returns:
        Number of newly added chunks (0 on a no-op re-sync).
    """
    # Handbook PDFs take the page-aware route: extract_pdf's (clean_text,
    # page_map) feeds chunk_handbook so chunks carry printed-page citations. The
    # `handbook` type is inseparable from a single PDF — a directory or URL under
    # this type would otherwise fall through and mis-tag legislation chunks as
    # `handbook`, so reject that combination loudly instead.
    if document_type == "handbook":
        if not source_path.endswith(".pdf"):
            raise ValueError(
                "--type handbook expects a single PDF file, but got a directory "
                f"or URL: {source_path}. Use --type legislation (or case_law / "
                "contracts) for those sources."
            )
        clean_text, page_map, metadata = load_handbook_pdf(source_path)
        if not clean_text.strip():
            logger.warning("No text extracted from %s", source_path)
            return 0
        # chunk_handbook raises a loud ValueError on a non-handbook PDF — do this
        # BEFORE clearing the store so a mis-routed --reset cannot destroy the
        # existing index and then crash.
        all_chunks = chunk_handbook(clean_text, page_map, metadata)
        logger.info("Created %d handbook chunks from %s", len(all_chunks), source_path)
        # An empty chunk list from a real PDF is never an intended delete-all:
        # sync_documents(source, []) would silently delete every stored chunk
        # for this source — and the handbook is one source, so that is the whole
        # corpus. Guard it out here; deliberate deletion stays available via the
        # sync API directly.
        if not all_chunks:
            logger.warning(
                "chunk_handbook produced 0 chunks from %s; skipping sync to "
                "avoid a silent full-corpus wipe. Returning 0.",
                source_path,
            )
            return 0
        if reset:
            clear_store(persist_directory)
        # ingest writes the CLI path verbatim into metadata["source"], so the
        # same string is the sync scope — a different spelling of the path is a
        # different source and would duplicate the corpus.
        counts = sync_documents(
            source_path, all_chunks, persist_directory=persist_directory
        )
        logger.info(
            "Synced %s: %d added, %d updated, %d deleted",
            source_path, counts["added"], counts["updated"], counts["deleted"],
        )
        print(
            f"\nSynced 1 document ({len(all_chunks)} chunks): "
            f"{counts['added']} added, {counts['updated']} updated, "
            f"{counts['deleted']} deleted"
        )
        return counts["added"]

    # Load documents based on source type
    if source_path.startswith(("http://", "https://")):
        documents = load_html_from_url(source_path, document_type)
    elif source_path.endswith(".pdf"):
        documents = load_pdf(source_path, document_type)
    else:
        documents = load_directory(source_path, document_type)

    if not documents:
        logger.warning("No documents loaded from %s", source_path)
        return 0

    logger.info("Loaded %d document(s) from %s", len(documents), source_path)

    # Chunk all documents
    all_chunks = []
    for doc in documents:
        chunks = chunk_legal_document(doc)
        all_chunks.extend(chunks)

    logger.info("Created %d chunks from %d document(s)", len(all_chunks), len(documents))

    # Store in vector database (clear only after chunks are in hand — see above).
    if reset:
        clear_store(persist_directory)
    # Sync per source: a directory of documents yields chunks from several
    # sources, and each source's chunks must be synced under its own scope so
    # one document's re-index can never delete another's chunks (D37).
    by_source: Dict[str, list] = {}
    for chunk in all_chunks:
        # ingest guarantees every chunk carries its "source"; a missing one is a
        # loader/chunker regression. Grouping by a fallback would silently mis-
        # scope the per-source sync and could delete another document's chunks,
        # so fail loudly instead — naming the offending chunk's leading text.
        chunk_source = chunk.metadata.get("source")
        if not chunk_source:
            raise ValueError(
                "Chunk is missing its 'source' metadata (ingest guarantees it); "
                "refusing to group it under a fallback source, which would mis-"
                "scope the per-source sync. Offending chunk starts: "
                f"{chunk.page_content[:60]!r}"
            )
        by_source.setdefault(chunk_source, []).append(chunk)

    # Defer the BM25 rebuild: each per-source sync scans the whole store to
    # rebuild the global lexical index, so rebuilding once per source is
    # O(N x total_chunks). Sync all sources with rebuild_bm25=False, then rebuild
    # the global sidecar + manifest exactly once below.
    added = updated = deleted = 0
    for src, chunks in by_source.items():
        counts = sync_documents(
            src, chunks, persist_directory=persist_directory, rebuild_bm25=False
        )
        added += counts["added"]
        updated += counts["updated"]
        deleted += counts["deleted"]
    # Only when something actually changed; an all-no-op re-sync leaves the
    # existing sidecar untouched.
    if added or updated or deleted:
        rebuild_bm25_index(persist_directory=persist_directory)
    logger.info(
        "Synced %d source(s): %d added, %d updated, %d deleted",
        len(by_source), added, updated, deleted,
    )

    print(
        f"\nSynced {len(documents)} document(s) across {len(by_source)} "
        f"source(s): {added} added, {updated} updated, {deleted} deleted"
    )
    return added


def _write_audit(
    *,
    question: str,
    top_k: int,
    document_type: Optional[str],
    results: list,
    gate_outcome: Optional[str],
    action: str,
    citation_check: Dict[str, Any],
    citations: list,
    answer: str,
) -> None:
    """Build and append exactly one audit event, tolerating log failures.

    A query must never crash because ``logs/`` is unwritable, so the whole
    build-and-append is wrapped in a broad ``except``. The failure is not
    swallowed silently, though: a one-line warning is printed so an operator
    knows the audit trail has a hole. ``build_event`` records only
    ``len(answer)`` (see src/audit.py), so passing the real draft answer here
    never persists corpus text.
    """
    try:
        log_event(
            build_event(
                question=question,
                top_k=top_k,
                document_type=document_type,
                results=results,
                gate_outcome=gate_outcome,
                action=action,
                citation_check=citation_check,
                citations=citations,
                answer=answer,
            )
        )
    except Exception as e:  # noqa: BLE001 — an audit hiccup must not fail a query
        print(f"⚠ audit log write failed: {e}")


def _print_answer_and_sources(answer: str, sources: list) -> None:
    """Print the answer body and its extracted citation list.

    The shared opening of every branch that shows the draft (legacy fallback,
    CITATIONS_VERIFIED, PARTIALLY_VERIFIED) — one owner so the branches
    cannot drift apart on wording or format.
    """
    print(f"\nAnswer:\n{answer}")
    if sources:
        print("\nCitations found:")
        for source in sources:
            print(f"  - {source}")


def _print_retrieved_sources(results: list) -> None:
    """Print one locator+pages line per retrieved chunk, no chunk text.

    Used by the CITATIONS_UNVERIFIED paths (both the block notice and the
    ``--show-unverified`` draft) so a reviewer can see what the answer was
    matched against. Reuses ``locator_label`` (D34) so the APPENDIX grammar
    stays identical to every other display surface; the page range uses the
    same en-dash as the chunk prefix and retriever header. ``page_start`` is
    stored as an explicit ``None`` when OCR found no printed page number, so
    the missing-page guard must test the value, not the key.
    """
    print("\nRetrieved sources (for manual review):")
    for r in results:
        meta = r["document"].metadata
        section = meta.get("section_number") or "—"
        p_start = meta.get("page_start")
        p_end = meta.get("page_end")
        if p_start is None:
            pages = f"pp.?–{p_end}" if p_end is not None else "p.?"
        elif p_end and p_end != p_start:
            pages = f"pp.{p_start}–{p_end}"
        else:
            pages = f"p.{p_start}"
        print(f"  - {locator_label(section)}  {pages}")


def query(
    question: str,
    top_k: int = DEFAULT_TOP_K,
    document_type: Optional[str] = None,
    verbose: bool = False,
    show_unverified: bool = False,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Dict[str, Any]:
    """Query the RAG pipeline with a legal question.

    Args:
        question: The natural-language legal question.
        top_k: Number of relevant chunks to retrieve.
        document_type: Optional filter for document type.
        verbose: If True, print per-chunk fused RRF scores and page/section
            before the answer. Citation-honesty warnings (zero citations on a
            non-refusal answer; ungrounded citations) always print, regardless
            of this flag — they are correctness signals, not debug output.
        show_unverified: If True and the grounding gate returns
            CITATIONS_UNVERIFIED, print the withheld draft under an explicit
            "UNVERIFIED DRAFT" banner and return the real answer instead of the
            block notice. Has no effect on any other gate outcome.
        persist_directory: Vector-store directory to query; the BM25 sidecar and
            embedding-model manifest are read from beside it.

    Returns:
        Dict with answer, citations, sources, source_documents,
        citation_check ({grounded, ungrounded}), gate_outcome, and
        answer_chars — the same key set on every path, so callers (e.g. the
        Phase 5 eval) never need key guards. ``answer_chars`` is the generated
        draft's length (0 when retrieval was empty and no draft ever existed);
        ``gate_outcome`` is None for the no-results path and legacy/ungated
        results. When the gate blocks an answer (CITATIONS_UNVERIFIED without
        ``show_unverified``), ``answer`` is a block notice and the draft text
        is withheld — ``answer_chars`` is then the only trace of its size.
    """
    # Build the store and BM25 sidecar once and inject them (load-once, D37);
    # retrieve() skips its per-call construction when injected, and the shared
    # helper owns the embedding-model manifest check.
    vector_store, bm25_index = load_retrieval_context(persist_directory)
    results = retrieve(
        question,
        top_k=top_k,
        document_type=document_type,
        persist_directory=persist_directory,
        vector_store=vector_store,
        bm25_index=bm25_index,
    )

    if not results:
        msg = "No relevant documents found. Please index some documents first."
        print(f"\n{msg}")
        # The gate never ran (there was nothing to ground against), so the audit
        # record carries gate_outcome=None and the no_results action.
        _write_audit(
            question=question,
            top_k=top_k,
            document_type=document_type,
            results=[],
            gate_outcome=None,
            action=ACTION_NO_RESULTS,
            citation_check={"grounded": [], "ungrounded": []},
            citations=[],
            # Generation never ran — record a zero-length draft, not the
            # length of this UI notice, so log analysis can't mistake
            # no_results rows for real answers.
            answer="",
        )
        return {
            "answer": msg,
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": None,
            "answer_chars": 0,
        }

    logger.info("Retrieved %d relevant chunks", len(results))

    if verbose:
        print("\nRetrieved chunks (by fused RRF score):")
        for rank, r in enumerate(results, 1):
            meta = r["document"].metadata
            section = meta.get("section_number") or "—"
            # page_start can be an explicit None (no printed page found by
            # OCR), which .get's default would let through as "p.None".
            page = meta.get("page_start")
            page = "?" if page is None else page
            doc_type = meta.get("document_type", "?")
            locator = locator_label(section)
            print(
                f"  {rank:>2}. RRF={r['score']:.5f}  {locator}  "
                f"p.{page}  [{doc_type}]"
            )

    # Generate answer with citations
    result = generate_with_sources(question, results)
    # Direct indexing on purpose: generate_with_sources always sets these
    # keys, and a defensive default here would mask an upstream regression as
    # a fully-unverified answer instead of raising at the real bug.
    draft_answer = result["answer"]
    citations = result["citations"]
    citation_check = result["citation_check"]
    ungrounded = citation_check["ungrounded"]
    outcome = result.get("gate_outcome")

    # Default: pass generate_with_sources' dict through unchanged. Only the
    # gated-block branch replaces it with a withheld-draft dict; every other
    # outcome (and the legacy fallback) returns the real result untouched.
    return_value: Dict[str, Any] = result

    if outcome is None:
        # FALLBACK (belt and braces): no gate outcome means a legacy caller or a
        # mock that predates the grounding gate. Reproduce the exact v1 display —
        # answer + citations + zero-citation warning + ungrounded warning — and
        # log it as a plain "shown". Real generate_with_sources always sets
        # gate_outcome, so production never reaches this branch. (Remove it when
        # the legacy-mock fixtures in tests/test_pipeline.py migrate to
        # gate_outcome-carrying mocks — it exists for them, not for production.)
        _print_answer_and_sources(draft_answer, result["sources"])
        # A non-refusal answer with no extractable citations is invisible to
        # citation_check (there is nothing to validate), so it needs its own
        # flag: otherwise it reads exactly like a grounded answer even though a
        # reader has no locator to check it against.
        if not is_refusal(draft_answer) and not citations:
            print(
                "\n⚠ WARNING: this answer contains no citations and could not be "
                "verified\n  against the retrieved sources — treat it as "
                "unverified."
            )
        # Ungrounded citations are a correctness signal, not a debugging aid —
        # always show them so an invented locator is never mistaken for a real
        # one just because --verbose was left off.
        if ungrounded:
            print("\n⚠ Ungrounded citations (not matched to any retrieved chunk):")
            for citation in ungrounded:
                print(f"  - {citation['raw']}")
        action = ACTION_SHOWN

    elif outcome == REFUSAL:
        # The answer IS the refusal sentence — print it as-is, no warnings.
        print(f"\nAnswer:\n{draft_answer}")
        action = ACTION_REFUSAL_SHOWN

    elif outcome == CITATIONS_VERIFIED:
        _print_answer_and_sources(draft_answer, result["sources"])
        print("\n✓ All citations verified against the retrieved sources.")
        action = ACTION_SHOWN

    elif outcome == PARTIALLY_VERIFIED:
        _print_answer_and_sources(draft_answer, result["sources"])
        # Name every unverified citation so a reader knows exactly which
        # locators to double-check before relying on them.
        print(
            f"\n⚠ {len(ungrounded)} of {len(citations)} citations could not be "
            "verified against the retrieved sources — check these before relying "
            "on them:"
        )
        for citation in ungrounded:
            print(f"  - {citation['raw']}")
        action = ACTION_SHOWN_WITH_WARNING

    else:  # CITATIONS_UNVERIFIED
        if show_unverified:
            # Explicit operator override: show the draft, clearly branded as
            # unverified, plus the retrieved sources it was matched against.
            print("\nUNVERIFIED DRAFT — do not rely on this text")
            print(f"\nAnswer:\n{draft_answer}")
            _print_retrieved_sources(results)
            action = ACTION_SHOWN_UNVERIFIED_OVERRIDE
            # return_value stays as the real result (draft included).
        else:
            # Fail closed: the draft's citations could not be verified, so the
            # answer body is withheld. Show only the retrieved source headers
            # (no chunk text) so a reader can review the ground manually.
            print("\n🚫 BLOCKED — CITATIONS UNVERIFIED")
            print(
                "This answer's citations could not be verified against the "
                "retrieved sources, so it is withheld. This does NOT mean the "
                "answer is absent from the corpus."
            )
            # Name the locators that failed verification — an operator triaging
            # a block needs to see WHICH citations the draft tried to rely on
            # (locator strings only, never draft text).
            if ungrounded:
                print("\nUnverified citations in the withheld draft:")
                for citation in ungrounded:
                    print(f"  - {citation['raw']}")
            _print_retrieved_sources(results)
            print(
                "\nTry rephrasing the question, raising --top-k, or use "
                "--show-unverified to see the unverified draft."
            )
            action = ACTION_BLOCKED_UNVERIFIED
            # Build a NEW dict so the draft text never leaves this function.
            # Deliberate key-by-key allowlist, NOT {**result, ...}: a spread
            # would silently forward any future key that carries draft text —
            # fail-open. New keys must be consciously added here.
            # answer_chars lets a caller see a draft existed without seeing it.
            return_value = {
                "answer": (
                    "BLOCKED — CITATIONS UNVERIFIED: the answer was withheld "
                    "because its citations could not be verified against the "
                    "retrieved sources."
                ),
                "gate_outcome": outcome,
                "citations": citations,
                "sources": result["sources"],
                "citation_check": citation_check,
                "source_documents": result["source_documents"],
                "answer_chars": len(draft_answer),
            }

    # One audit event per query, after the display decision so `action` is
    # final. The REAL draft answer goes to build_event (it records only the
    # length, never the text — see src/audit.py).
    _write_audit(
        question=question,
        top_k=top_k,
        document_type=document_type,
        results=results,
        gate_outcome=outcome,
        action=action,
        citation_check=citation_check,
        citations=citations,
        answer=draft_answer,
    )

    # Uniform return shape: every path carries answer_chars (the generated
    # draft's length — 0 on the no-results path, where no draft ever existed)
    # and gate_outcome (None for legacy/ungated results). On shown paths
    # answer_chars equals len(answer); on the blocked path it is the only
    # trace of the withheld draft's size.
    return_value.setdefault("answer_chars", len(draft_answer))
    return_value.setdefault("gate_outcome", None)
    return return_value


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Legal Document RAG Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Index the handbook (page-aware, cited):
    python -m src.pipeline index ./data/Conveyancing_Handbook.pdf --type handbook

  Re-index from scratch (clear the store first):
    python -m src.pipeline index ./data/Conveyancing_Handbook.pdf --type handbook --reset

  Index legislation:
    python -m src.pipeline index ./data/legislation/ --type legislation

  Index from URL:
    python -m src.pipeline index https://www.irishstatutebook.ie/eli/1965/act/27/enacted/en/html --type legislation

  Query the pipeline:
    python -m src.pipeline query "What are the requirements for first registration of title?"
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Index subcommand
    index_parser = subparsers.add_parser("index", help="Index documents into the vector store")
    index_parser.add_argument("source_path", help="File path, directory, or URL to index")
    index_parser.add_argument(
        "--type",
        dest="document_type",
        default="handbook",
        choices=["handbook", "legislation", "case_law", "contracts"],
        help="Type of document (default: handbook)",
    )
    index_parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the vector store before indexing (avoids the positional-ID dedup trap)",
    )
    index_parser.add_argument(
        "--persist-dir",
        dest="persist_directory",
        default=CHROMA_PERSIST_DIR,
        help=f"Vector-store directory (default: {CHROMA_PERSIST_DIR}); the BM25 "
        "sidecar and model manifest live beside it",
    )

    # Query subcommand
    query_parser = subparsers.add_parser("query", help="Query the RAG pipeline")
    query_parser.add_argument("question", help="Legal question to answer")
    query_parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Number of chunks to retrieve (default: {DEFAULT_TOP_K})",
    )
    query_parser.add_argument(
        "--type",
        dest="document_type",
        default=None,
        choices=["handbook", "legislation", "case_law", "contracts"],
        help="Filter by document type",
    )
    query_parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Show per-chunk fused RRF scores before the answer "
            "(citation warnings always print, with or without this flag)"
        ),
    )
    query_parser.add_argument(
        "--show-unverified",
        dest="show_unverified",
        action="store_true",
        help=(
            "Reveal the withheld draft when the grounding gate blocks an answer "
            "as CITATIONS_UNVERIFIED (clearly branded as unverified)"
        ),
    )
    query_parser.add_argument(
        "--persist-dir",
        dest="persist_directory",
        default=CHROMA_PERSIST_DIR,
        help=f"Vector-store directory to query (default: {CHROMA_PERSIST_DIR})",
    )

    # Eval subcommand
    eval_parser = subparsers.add_parser(
        "eval", help="Evaluate retrieval hit@k and refusal accuracy against a golden set"
    )
    eval_parser.add_argument(
        "--golden",
        default="eval/golden_set.jsonl",
        help="Path to the golden-set JSONL file (default: eval/golden_set.jsonl)",
    )
    eval_parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Number of chunks to retrieve per question (default: {DEFAULT_TOP_K})",
    )
    eval_parser.add_argument(
        "--skip-refusals",
        action="store_true",
        help="Skip the refusal-accuracy pass (avoids live API calls)",
    )
    eval_parser.add_argument(
        "--persist-dir",
        dest="persist_directory",
        default=CHROMA_PERSIST_DIR,
        help=f"Vector-store directory to evaluate against (default: "
        f"{CHROMA_PERSIST_DIR}); Phase 11's sample-index smoke eval points "
        "this at sample_chroma_db/",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "index":
        index_documents(
            args.source_path,
            args.document_type,
            reset=args.reset,
            persist_directory=args.persist_directory,
        )
    elif args.command == "query":
        query(
            args.question,
            args.top_k,
            args.document_type,
            args.verbose,
            args.show_unverified,
            persist_directory=args.persist_directory,
        )
    elif args.command == "eval":
        from src.evaluator import run_eval

        run_eval(
            args.golden,
            top_k=args.top_k,
            skip_refusals=args.skip_refusals,
            persist_directory=args.persist_directory,
        )


if __name__ == "__main__":
    main()
