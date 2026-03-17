"""Tests for the pipeline orchestration module."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.pipeline import index_documents, query


class TestIndexDocuments:
    def test_indexes_directory(self):
        """Test indexing documents from a directory."""
        mock_docs = [
            Document(
                page_content="Test legal text.",
                metadata={"source": "test.pdf", "title": "Test", "document_type": "legislation"},
            )
        ]
        mock_chunks = [
            Document(
                page_content="[From: Test] Test legal text.",
                metadata={
                    "source": "test.pdf",
                    "title": "Test",
                    "document_type": "legislation",
                    "section_number": "",
                    "parent_section": "",
                },
            )
        ]

        with patch("src.pipeline.load_directory", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", return_value=mock_chunks), \
             patch("src.pipeline.add_documents", return_value=1):
            count = index_documents("/test/data/", "legislation")

        assert count == 1

    def test_indexes_url(self):
        """Test indexing from a URL."""
        mock_docs = [
            Document(
                page_content="Test act text.",
                metadata={"source": "https://example.com", "title": "Test Act"},
            )
        ]

        with patch("src.pipeline.load_html_from_url", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", return_value=mock_docs), \
             patch("src.pipeline.add_documents", return_value=1):
            count = index_documents("https://example.com/act", "legislation")

        assert count == 1

    def test_indexes_pdf_file(self):
        """Test indexing a single PDF file."""
        mock_docs = [
            Document(
                page_content="Test PDF content.",
                metadata={"source": "test.pdf", "title": "test.pdf"},
            )
        ]

        with patch("src.pipeline.load_pdf", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", return_value=mock_docs), \
             patch("src.pipeline.add_documents", return_value=1):
            count = index_documents("/test/doc.pdf", "case_law")

        assert count == 1

    def test_no_documents_returns_zero(self):
        """Test that an empty source returns 0."""
        with patch("src.pipeline.load_directory", return_value=[]):
            count = index_documents("/empty/dir/", "legislation")

        assert count == 0


class TestQuery:
    def test_returns_answer(self):
        """Test that query returns an answer dict."""
        mock_results = [
            {
                "document": Document(
                    page_content="Test content.",
                    metadata={"source": "test.pdf", "title": "Test"},
                ),
                "score": 0.9,
                "metadata": {"source": "test.pdf"},
            }
        ]
        mock_generated = {
            "answer": "The answer is...",
            "sources": ["Test, Section 1"],
            "source_documents": [],
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            result = query("What is the law?")

        assert result["answer"] == "The answer is..."

    def test_no_results_message(self):
        """Test that empty retrieval gives a helpful message."""
        with patch("src.pipeline.retrieve", return_value=[]):
            result = query("Unknown question")

        assert "No relevant documents" in result["answer"]
