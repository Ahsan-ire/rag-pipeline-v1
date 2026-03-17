"""Pipeline orchestration and CLI entry point.

Usage:
    python -m src.pipeline index ./data/legislation/ --type legislation
    python -m src.pipeline query "What are the succession rights of a spouse?" --top-k 5
"""

import argparse
import logging
import sys
from typing import Any, Dict, Optional

from src.chunker import chunk_legal_document
from src.embedder import add_documents
from src.generator import generate_with_sources
from src.ingest import load_directory, load_html_from_url, load_pdf
from src.retriever import retrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def index_documents(source_path: str, document_type: str = "legislation") -> int:
    """Ingest, chunk, embed, and store documents from a source path or URL.

    Args:
        source_path: A file path, directory path, or URL.
        document_type: Type of document (legislation, case_law, contracts).

    Returns:
        Number of chunks indexed.
    """
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

    # Store in vector database
    count = add_documents(all_chunks)
    logger.info("Indexed %d chunks in vector store", count)

    print(f"\nIndexed {count} chunks from {len(documents)} document(s)")
    return count


def query(
    question: str,
    top_k: int = 5,
    document_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Query the RAG pipeline with a legal question.

    Args:
        question: The natural-language legal question.
        top_k: Number of relevant chunks to retrieve.
        document_type: Optional filter for document type.

    Returns:
        Dict with answer, sources, and source_documents.
    """
    # Retrieve relevant chunks
    results = retrieve(question, top_k=top_k, document_type=document_type)

    if not results:
        msg = "No relevant documents found. Please index some documents first."
        print(f"\n{msg}")
        return {"answer": msg, "sources": [], "source_documents": []}

    logger.info("Retrieved %d relevant chunks", len(results))

    # Generate answer with citations
    result = generate_with_sources(question, results)

    # Display results
    print(f"\nAnswer:\n{result['answer']}")
    if result["sources"]:
        print("\nCitations found:")
        for source in result["sources"]:
            print(f"  - {source}")

    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Legal Document RAG Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Index legislation:
    python -m src.pipeline index ./data/legislation/ --type legislation

  Index a specific PDF:
    python -m src.pipeline index ./data/case_law/judgment.pdf --type case_law

  Index from URL:
    python -m src.pipeline index https://www.irishstatutebook.ie/eli/1965/act/27/enacted/en/html

  Query the pipeline:
    python -m src.pipeline query "What are the succession rights of a spouse under Irish law?"
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Index subcommand
    index_parser = subparsers.add_parser("index", help="Index documents into the vector store")
    index_parser.add_argument("source_path", help="File path, directory, or URL to index")
    index_parser.add_argument(
        "--type",
        dest="document_type",
        default="legislation",
        choices=["legislation", "case_law", "contracts"],
        help="Type of document (default: legislation)",
    )

    # Query subcommand
    query_parser = subparsers.add_parser("query", help="Query the RAG pipeline")
    query_parser.add_argument("question", help="Legal question to answer")
    query_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of chunks to retrieve (default: 5)",
    )
    query_parser.add_argument(
        "--type",
        dest="document_type",
        default=None,
        choices=["legislation", "case_law", "contracts"],
        help="Filter by document type",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "index":
        index_documents(args.source_path, args.document_type)
    elif args.command == "query":
        query(args.question, args.top_k, args.document_type)


if __name__ == "__main__":
    main()
