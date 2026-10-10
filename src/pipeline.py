"""Pipeline orchestration and CLI entry point.

Usage:
    python -m src.pipeline index ./data/legislation/ --type legislation
    python -m src.pipeline query "What are the succession rights of a spouse?" --top-k 6
"""

import argparse
import logging
import os
import sys
from typing import Any, Dict, Optional

from src import grounding
from src.audit import build_event, log_event
from src.chunker import chunk_handbook, chunk_legal_document, locator_label
from src.embedder import (
    CHROMA_PERSIST_DIR,
    clear_store,
    rebuild_bm25_index,
    sync_documents,
)
from src.evaluator import EVAL_MODES
from src.generator import generate_with_sources
from src.ingest import (
    load_directory,
    load_handbook_pdf,
    load_html_from_url,
    load_pdf,
)
from src.query_rewrite import Expansion, expand_query
from src.render import RenderFlags, render
from src.retriever import (
    DEFAULT_TOP_K,
    load_retrieval_context,
    retrieve,
)

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
    expansion: Optional[Expansion] = None,
    stop_reason: Optional[str] = None,
    generation_status: str = grounding.STATUS_UNKNOWN,
    uncited_count: Optional[int] = None,
) -> None:
    """Build and append exactly one audit event, tolerating log failures.

    A query must never crash because ``logs/`` is unwritable, so the whole
    build-and-append is wrapped in a broad ``except``. The failure is not
    swallowed silently, though: a one-line warning is printed so an operator
    knows the audit trail has a hole. ``build_event`` records only
    ``len(answer)`` (see src/audit.py), so passing the real draft answer here
    never persists corpus text.

    ``expansion`` (Phase 13, D43), when given, forwards its effective
    rewrites and status to ``build_event`` so the record carries
    ``rewrite_status``/``rewrite_count``/``rewrite_sha256s``, plus its
    ``intent_rewrite`` (Phase 14, D50) so the record carries
    ``intent_rewrite_sha256`` (and, under AUDIT_LOG_RAW_QUERIES=1,
    ``intent_rewrite_text``) whenever an intent exists. Omitted
    (``None``) reproduces today's event shape exactly — see
    ``build_event``'s own omit contracts.
    """
    try:
        event_kwargs: Dict[str, Any] = dict(
            question=question,
            top_k=top_k,
            document_type=document_type,
            results=results,
            gate_outcome=gate_outcome,
            action=action,
            citation_check=citation_check,
            citations=citations,
            answer=answer,
            stop_reason=stop_reason,
            generation_status=generation_status,
            uncited_count=uncited_count,
        )
        if expansion is not None:
            event_kwargs["rewrites"] = list(expansion.rewrites)
            event_kwargs["rewrite_status"] = expansion.status
            # Phase 14 (D50): forward the intent reframe. build_event adds the
            # intent audit keys ONLY when it is not None, so a None intent keeps
            # the event shape byte-identical to a pre-Phase-14 (rewrite-only)
            # record.
            event_kwargs["intent_rewrite"] = expansion.intent_rewrite
        log_event(build_event(**event_kwargs))
    except Exception as e:  # noqa: BLE001 — an audit hiccup must not fail a query
        print(f"⚠ audit log write failed: {e}")


def query(
    question: str,
    top_k: int = DEFAULT_TOP_K,
    document_type: Optional[str] = None,
    verbose: bool = False,
    show_unverified: bool = False,
    persist_directory: str = CHROMA_PERSIST_DIR,
    *,
    no_rewrite: bool = False,
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
        no_rewrite: If True, skip the LLM query-expansion stage (Phase 13,
            D43) — retrieval sees only the raw question. Keyword-only (after the
            bare ``*``) so it can never be set by a positional-argument shift;
            ``main()`` already passes it by keyword.

    Returns:
        ``render().public_result`` (src/render.py): the same key set on every
        path — answer, gate_outcome, citations, sources, citation_check,
        source_documents, answer_chars, generation_status, stop_reason and
        uncited_count — so callers (e.g. the Phase 5 eval) never need key
        guards. ``answer_chars`` is the generated draft's length (0 when
        retrieval was empty). ``gate_outcome`` is None for no-results and
        legacy/ungated results. When the answer is withheld (CITATIONS_UNVERIFIED
        without ``show_unverified``, or a terminal generation outcome),
        ``answer`` is a synthesised notice and the draft is never returned;
        ``answer_chars`` is then the only trace of its size. ``uncited_count`` is
        an int only for shown verified/partial answers and the override draft,
        else None.
    """
    # Build the store and BM25 sidecar once and inject them (load-once, D37);
    # retrieve() skips its per-call construction when injected, and the shared
    # helper owns the embedding-model manifest check.
    vector_store, bm25_index = load_retrieval_context(persist_directory)
    # Pre-retrieval query expansion (Phase 13, D43) runs before retrieve() so
    # both the no-results early-return and the main path below can log it.
    expansion = expand_query(question, enabled=not no_rewrite)
    results = retrieve(
        question,
        top_k=top_k,
        document_type=document_type,
        persist_directory=persist_directory,
        vector_store=vector_store,
        bm25_index=bm25_index,
        rewrites=list(expansion.rewrites) or None,
        # Phase 14 (D50): the intent reframe flows into retrieve() on its own
        # weight budget. None when the model produced no usable intent, so this
        # is byte-identical to pre-Phase-14 when there is nothing to reframe.
        intent_rewrite=expansion.intent_rewrite,
    )

    if not results:
        rendered = render(None, RenderFlags(no_results=True))
        print(rendered.display_text)
        # The gate never ran (there was nothing to ground against), so the audit
        # record carries gate_outcome=None and the no_results action.
        _write_audit(
            question=question,
            top_k=top_k,
            document_type=document_type,
            results=[],
            gate_outcome=None,
            action=rendered.action,
            citation_check=rendered.public_result["citation_check"],
            citations=[],
            # Generation never ran — record a zero-length draft, not the
            # length of this UI notice, so log analysis can't mistake
            # no_results rows for real answers.
            answer="",
            expansion=expansion,
            generation_status=rendered.public_result["generation_status"],
        )
        return rendered.public_result

    logger.info("Retrieved %d relevant chunks", len(results))

    if verbose:
        print(
            f"\nQuery expansion [{expansion.status}]: "
            f"{len(expansion.rewrites)} rewrite(s)"
        )
        for rewrite in expansion.rewrites:
            print(f"  - {rewrite}")
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
    # The verbose chunk-score print above stays before rendering; everything the
    # user sees about the answer, the audit action and the returned dict come
    # from one pure function (src/render.py) so they cannot drift apart.
    rendered = render(
        result, RenderFlags(show_unverified=show_unverified, retrieved=results)
    )
    print(rendered.display_text)
    public = rendered.public_result

    # One audit event per query, after the display decision so `action` is
    # final. The REAL draft answer goes to build_event (it records only the
    # length, never the text — see src/audit.py). uncited_count is None in the
    # public result when the hint was not computed; the audit records null too.
    _write_audit(
        question=question,
        top_k=top_k,
        document_type=document_type,
        results=results,
        gate_outcome=public["gate_outcome"],
        action=rendered.action,
        citation_check=public["citation_check"],
        citations=public["citations"],
        answer=result["answer"],
        expansion=expansion,
        stop_reason=public["stop_reason"],
        generation_status=public["generation_status"],
        uncited_count=public["uncited_count"],
    )
    return public


def main() -> None:
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
        "--no-rewrite",
        dest="no_rewrite",
        action="store_true",
        help=(
            "Skip the LLM query-expansion stage (debug/offline; retrieval sees "
            "the raw question only)"
        ),
    )
    query_parser.add_argument(
        "--persist-dir",
        dest="persist_directory",
        default=CHROMA_PERSIST_DIR,
        help=f"Vector-store directory to query (default: {CHROMA_PERSIST_DIR})",
    )

    # Eval subcommand (Phase 10 matrix: sets × retrieval modes, held-out
    # headline, completeness + optional judge). NOTE: with neither
    # --skip-refusals nor --skip-completeness, this makes LIVE Claude API calls
    # (one generation per answerable + refusal question); --judge adds one call
    # per non-refused in-corpus answer. Offline/CI runs must pass BOTH
    # --skip-refusals and --skip-completeness (and omit --judge) — that pair
    # ALSO disables query expansion outright, so it stays the documented
    # zero-API-call contract even on a keyed dev box (Phase 13, D46). A
    # canonical committed run now additionally requires --realistic, all four
    # EVAL_MODES, and zero query-expansion fallbacks (see run_eval_matrix).
    eval_parser = subparsers.add_parser(
        "eval",
        help="Evaluate retrieval ablation, refusal accuracy, and completeness "
        "against a golden set (Phase 10 matrix)",
    )
    eval_parser.add_argument(
        "--golden",
        default="eval/golden_set.jsonl",
        help="Path to the tuning golden-set JSONL (default: eval/golden_set.jsonl)",
    )
    eval_parser.add_argument(
        "--heldout",
        default=None,
        help="Path to the frozen held-out set JSONL; add it for the canonical, "
        "held-out-headline run (e.g. eval/heldout_set.jsonl)",
    )
    eval_parser.add_argument(
        "--realistic",
        default=None,
        help="Path to the realistic-slice JSONL (messy staff phrasing + "
        "near-domain negatives); required for a canonical run "
        "(e.g. eval/realistic_set.jsonl)",
    )
    eval_parser.add_argument(
        "--mode",
        choices=[*EVAL_MODES, "all"],
        default="all",
        help="Retrieval mode(s) to ablate: hybrid/vector/bm25/hybrid+rewrite, "
        "or 'all' four (default: all). Answer passes use the hybrid+rewrite "
        "production config (raw hybrid when expansion is disabled offline).",
    )
    eval_parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Number of chunks to retrieve per question (default: {DEFAULT_TOP_K}); "
        "the canonical committed report requires top_k=6",
    )
    eval_parser.add_argument(
        "--skip-refusals",
        action="store_true",
        help="Skip the refusal-accuracy pass (part of going offline/API-free)",
    )
    eval_parser.add_argument(
        "--skip-completeness",
        action="store_true",
        help="Skip the answer-quality (completeness) pass (part of going "
        "offline/API-free)",
    )
    eval_parser.add_argument(
        "--judge",
        action="store_true",
        help="Run the experimental LLM-as-judge faithfulness pass (extra live "
        "API calls; conditional on non-refused in-corpus answers)",
    )
    eval_parser.add_argument(
        "--judge-sample",
        type=int,
        default=None,
        help="Judge only a deterministic random sample of this many answers per "
        "set (default: judge all)",
    )
    eval_parser.add_argument(
        "--results",
        "-o",
        dest="results_path",
        default=None,
        help="Explicit report path; refused if it equals an eval set. Without "
        "it, only a canonical run writes eval/results.md (else the gitignored "
        "eval/results_partial.md)",
    )
    eval_parser.add_argument(
        "--persist-dir",
        dest="persist_directory",
        default=CHROMA_PERSIST_DIR,
        help=f"Vector-store directory to evaluate against (default: "
        f"{CHROMA_PERSIST_DIR}); Phase 11's sample-index smoke eval points "
        "this at sample_chroma_db/",
    )
    # Phase 16A-1 (D68, D70): frozen expansion artifacts and the spend meter.
    eval_parser.add_argument(
        "--expansion",
        default="live",
        help="'live' (default), 'build:<path>' to freeze this run's expansions, "
        "or a frozen artifact path to replay in every arm (never canonical)",
    )
    eval_parser.add_argument(
        "--approved-eur",
        type=float,
        default=None,
        help="Run spend limit in EUR (default: what is left of the EUR 40 weekly "
        "cap); a live run is always metered",
    )
    eval_parser.add_argument(
        "--owner-approved-eur",
        type=float,
        default=None,
        help="Owner-approved weekly ceiling in EUR (lifts the cap); requires "
        "--approval-ref and the owner's prior approval (D64)",
    )
    eval_parser.add_argument(
        "--approval-ref",
        default=None,
        help="Reference to the owner's approval of --owner-approved-eur",
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
            no_rewrite=args.no_rewrite,
        )
    elif args.command == "eval":
        code = _eval_command(args)
        if code:
            sys.exit(code)



# Exit codes of `pipeline eval` (16A-1): 3 = the weekly spend cap was reached
# (a D64 owner stop), 4 = sealed input refused, 6 = this run's own spend limit.
EXIT_SPEND_WEEK = 3
EXIT_SEALED = 4
EXIT_SPEND_RUN = 6


def _eval_command(args: argparse.Namespace) -> int:
    """Run ``pipeline eval``: classify inputs, meter live runs, map exit codes.

    The privacy class passed to the runner is ``classify``'s floor over the set
    paths (the CLI never chooses a class itself). Sealed input exits 4 before
    anything else runs. A live run (any pass other than the both-skips offline
    form, or --judge) builds a spend meter from ``config/api_prices.toml``; the
    offline command builds none. On a private run an unexpected error prints
    only its exception type.
    """
    from src.eval_privacy import PUBLIC, SealedInputError, safe_error
    from src.eval_sets import floor
    from src.evaluator import run_eval_matrix
    from src.spend import SpendLimitReached

    # Build the (label, path) set list. The default golden path is the
    # tuning set (used to select D31 fusion constants) — label it so the
    # report can honestly flag it as NOT held-out; a non-default --golden
    # is just "golden". The held-out set, then the realistic slice, are
    # appended when given — a canonical run needs BOTH (D46).
    set_specs = [
        ("tuning" if args.golden == "eval/golden_set.jsonl" else "golden", args.golden)
    ]
    if args.heldout:
        set_specs.append(("held-out", args.heldout))
    if args.realistic:
        set_specs.append(("realistic", args.realistic))
    modes = list(EVAL_MODES) if args.mode == "all" else [args.mode]

    paths = [p for _l, p in set_specs]
    if args.expansion != "live" and not args.expansion.startswith("build:"):
        paths.append(args.expansion)
    try:
        privacy = floor(paths)
    except SealedInputError:
        print("[eval] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    if privacy == "sealed":
        print("[eval] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED

    live = (not (args.skip_refusals and args.skip_completeness)) or args.judge
    meter = None
    if live:
        from src.spend import SpendMeter, load_prices

        meter = SpendMeter(
            load_prices(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "api_prices.toml")),
            None,
            args.approved_eur,
            owner_approved_eur=args.owner_approved_eur,
            approval_ref=args.approval_ref,
        )

    def _totals() -> None:
        if meter is not None:
            print(
                f"[eval] spend: run EUR {meter.run_total_eur:.4f}, "
                f"week EUR {meter.week_total_eur:.4f} (ceiling EUR {meter.ceiling_eur:.2f})",
                file=sys.stderr,
            )

    try:
        run_eval_matrix(
            set_specs,
            modes=modes,
            top_k=args.top_k,
            skip_refusals=args.skip_refusals,
            skip_completeness=args.skip_completeness,
            judge=args.judge,
            judge_sample=args.judge_sample,
            results_path=args.results_path,
            judge_dump_path="eval/judge_review.jsonl" if args.judge else None,
            persist_directory=args.persist_directory,
            privacy=privacy,
            meter=meter,
            expansion=args.expansion,
        )
    except SpendLimitReached as exc:
        _totals()
        print(f"[eval] spend limit reached ({exc.kind}); no report written", file=sys.stderr)
        return EXIT_SPEND_WEEK if exc.kind == "week" else EXIT_SPEND_RUN
    except SealedInputError:
        print("[eval] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    except Exception as exc:  # noqa: BLE001 - private runs must not print str(exc)
        if privacy != PUBLIC:
            print(f"[eval] error: {safe_error(exc)}", file=sys.stderr)
            return 1
        raise
    _totals()
    return 0


if __name__ == "__main__":
    main()
