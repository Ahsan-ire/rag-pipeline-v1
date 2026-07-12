"""Pipeline orchestration and CLI entry point.

Usage:
    python -m src.pipeline index ./data/legislation/ --type legislation
    python -m src.pipeline query "What are the succession rights of a spouse?" --top-k 6
"""

import argparse
import logging
import sys
from typing import Any, Dict, Optional

from src.chunker import chunk_handbook, chunk_legal_document
from src.embedder import add_documents, clear_store
from src.generator import generate_with_sources, is_refusal
from src.ingest import (
    load_directory,
    load_handbook_pdf,
    load_html_from_url,
    load_pdf,
)
from src.retriever import DEFAULT_TOP_K, retrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def index_documents(
    source_path: str, document_type: str = "handbook", reset: bool = False
) -> int:
    """Ingest, chunk, embed, and store documents from a source path or URL.

    Args:
        source_path: A file path, directory path, or URL.
        document_type: Type of document (handbook, legislation, case_law,
            contracts). ``handbook`` PDFs use the page-aware loader and the
            handbook chunker (Phase 2); everything else keeps the original path.
        reset: If True, clear the vector store before indexing. Interim guard
            against the positional-ID dedup trap while re-indexing during a
            chunker-iteration loop (content-hash IDs land in Phase 3).

    Returns:
        Number of chunks indexed.
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
        if reset:
            clear_store()
        count = add_documents(all_chunks)
        logger.info("Indexed %d chunks in vector store", count)
        print(f"\nIndexed {count} chunks from 1 document")
        return count

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

    # Store in vector database (clear only after chunks are in hand — see above)
    if reset:
        clear_store()
    count = add_documents(all_chunks)
    logger.info("Indexed %d chunks in vector store", count)

    print(f"\nIndexed {count} chunks from {len(documents)} document(s)")
    return count


def query(
    question: str,
    top_k: int = DEFAULT_TOP_K,
    document_type: Optional[str] = None,
    verbose: bool = False,
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

    Returns:
        Dict with answer, citations, sources, source_documents, and
        citation_check ({grounded, ungrounded}) — the same shape on every
        path, so callers (e.g. the Phase 5 eval) never need key guards.
    """
    # Retrieve relevant chunks
    results = retrieve(question, top_k=top_k, document_type=document_type)

    if not results:
        msg = "No relevant documents found. Please index some documents first."
        print(f"\n{msg}")
        return {
            "answer": msg,
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
        }

    logger.info("Retrieved %d relevant chunks", len(results))

    if verbose:
        print("\nRetrieved chunks (by fused RRF score):")
        for rank, r in enumerate(results, 1):
            meta = r["document"].metadata
            section = meta.get("section_number") or "—"
            page = meta.get("page_start", "?")
            doc_type = meta.get("document_type", "?")
            print(
                f"  {rank:>2}. RRF={r['score']:.5f}  para {section}  "
                f"p.{page}  [{doc_type}]"
            )

    # Generate answer with citations
    result = generate_with_sources(question, results)

    # Display results
    print(f"\nAnswer:\n{result['answer']}")
    if result["sources"]:
        print("\nCitations found:")
        for source in result["sources"]:
            print(f"  - {source}")

    # A non-refusal answer with no extractable citations is invisible to
    # citation_check (there is nothing to validate), so it needs its own flag:
    # otherwise it reads exactly like a grounded answer even though a reader
    # has no locator to check it against.
    if not is_refusal(result["answer"]) and not result["citations"]:
        print(
            "\n⚠ WARNING: this answer contains no citations and could not be "
            "verified\n  against the retrieved sources — treat it as "
            "unverified."
        )

    # Ungrounded citations are a correctness signal, not a debugging aid —
    # always show them so an invented locator is never mistaken for a real
    # one just because --verbose was left off.
    ungrounded = result["citation_check"]["ungrounded"]
    if ungrounded:
        print("\n⚠ Ungrounded citations (not matched to any retrieved chunk):")
        for citation in ungrounded:
            print(f"  - {citation['raw']}")

    return result


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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "index":
        index_documents(args.source_path, args.document_type, reset=args.reset)
    elif args.command == "query":
        query(args.question, args.top_k, args.document_type, args.verbose)
    elif args.command == "eval":
        from src.evaluator import run_eval

        run_eval(args.golden, top_k=args.top_k, skip_refusals=args.skip_refusals)


if __name__ == "__main__":
    main()
