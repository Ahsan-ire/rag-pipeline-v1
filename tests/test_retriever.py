"""Tests for the retrieval module."""

import shutil
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from src.retriever import format_context, retrieve
from tests.test_embedder import FakeEmbeddings


@pytest.fixture
def seeded_store(tmp_path):
    """Create a ChromaDB store seeded with test documents."""
    from src.embedder import add_documents, get_vector_store

    store = get_vector_store(
        embedding_function=FakeEmbeddings(),
        persist_directory=str(tmp_path / "chroma"),
    )

    docs = [
        Document(
            page_content="A person aged 18 or over may make a valid will under Irish law.",
            metadata={
                "source": "succession_act.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "77",
            },
        ),
        Document(
            page_content="The defendant was found liable for negligence in this case.",
            metadata={
                "source": "case_001.pdf",
                "title": "Smith v Jones [2020] IEHC 123",
                "document_type": "case_law",
                "section_number": "",
            },
        ),
    ]

    add_documents(docs, vector_store=store)
    yield store, str(tmp_path / "chroma")
    shutil.rmtree(str(tmp_path / "chroma"), ignore_errors=True)


class TestRetrieve:
    def test_returns_results(self, seeded_store):
        """Test that retrieve returns results from a seeded store."""
        store, persist_dir = seeded_store

        with patch("src.retriever.get_vector_store", return_value=store):
            results = retrieve("making a will", top_k=2, persist_directory=persist_dir)

        assert len(results) > 0
        assert "document" in results[0]
        assert "score" in results[0]
        assert "metadata" in results[0]

    def test_filter_by_document_type(self, seeded_store):
        """Test metadata filtering by document type."""
        store, persist_dir = seeded_store

        with patch("src.retriever.get_vector_store", return_value=store):
            results = retrieve(
                "negligence", top_k=5, document_type="case_law", persist_directory=persist_dir
            )

        # All results should be case_law type
        for result in results:
            assert result["metadata"]["document_type"] == "case_law"


class TestFormatContext:
    def test_formats_results(self, mock_retrieved_results):
        """Test context formatting for LLM prompt."""
        context = format_context(mock_retrieved_results)

        assert "[Source 1:" in context
        assert "[Source 2:" in context
        assert "Succession Act 1965" in context
        assert "---" in context

    def test_empty_results(self):
        """Test formatting with no results."""
        context = format_context([])
        assert "No relevant documents found" in context
