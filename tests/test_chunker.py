"""Tests for the legal-aware chunking module."""

import pytest
from langchain_core.documents import Document

from src.chunker import (
    _apply_fallback_splitter,
    _prepend_summary,
    _split_by_legal_structure,
    chunk_legal_document,
)


class TestSplitByLegalStructure:
    def test_splits_by_part(self):
        """Test splitting on PART boundaries."""
        text = """PART I
PRELIMINARY
Some preliminary text here.

PART II
MAIN PROVISIONS
Main content here."""
        metadata = {"title": "Test Act", "source": "test.pdf", "document_type": "legislation"}

        chunks = _split_by_legal_structure(text, metadata)
        assert len(chunks) == 2
        assert "PRELIMINARY" in chunks[0].page_content
        assert "MAIN PROVISIONS" in chunks[1].page_content

    def test_splits_by_section(self):
        """Test splitting on Section boundaries."""
        text = """Section 1.
First section content.

Section 2.
Second section content.

Section 3.
Third section content."""
        metadata = {"title": "Test Act", "source": "test.pdf", "document_type": "legislation"}

        chunks = _split_by_legal_structure(text, metadata)
        assert len(chunks) == 3
        assert chunks[0].metadata["section_number"] == "1"
        assert chunks[1].metadata["section_number"] == "2"
        assert chunks[2].metadata["section_number"] == "3"

    def test_preserves_metadata(self):
        """Test that original document metadata is carried forward."""
        text = "Section 1.\nSome content."
        metadata = {
            "title": "Succession Act 1965",
            "source": "/data/succession.pdf",
            "document_type": "legislation",
            "date": "1965",
        }

        chunks = _split_by_legal_structure(text, metadata)
        assert chunks[0].metadata["title"] == "Succession Act 1965"
        assert chunks[0].metadata["source"] == "/data/succession.pdf"
        assert chunks[0].metadata["document_type"] == "legislation"

    def test_no_structure_returns_single_chunk(self):
        """Test that unstructured text returns as a single chunk."""
        text = "This is plain legal text with no section markers."
        metadata = {"title": "Test", "source": "test.pdf", "document_type": "legislation"}

        chunks = _split_by_legal_structure(text, metadata)
        assert len(chunks) == 1
        assert chunks[0].page_content == text


class TestApplyFallbackSplitter:
    def test_splits_oversized_chunks(self):
        """Test that chunks exceeding the size limit are re-split."""
        # Create a chunk that's definitely too large (> 600 tokens * 4 chars = 2400 chars)
        long_text = "This is a sentence. " * 200  # ~4000 chars
        chunks = [
            Document(
                page_content=long_text,
                metadata={"title": "Test", "section_number": "1"},
            )
        ]

        result = _apply_fallback_splitter(chunks, chunk_size=600, chunk_overlap=120)
        assert len(result) > 1

    def test_preserves_small_chunks(self):
        """Test that chunks within the size limit are not re-split."""
        chunks = [
            Document(
                page_content="Short content.",
                metadata={"title": "Test", "section_number": "1"},
            )
        ]

        result = _apply_fallback_splitter(chunks, chunk_size=600, chunk_overlap=120)
        assert len(result) == 1
        assert result[0].page_content == "Short content."


class TestPrependSummary:
    def test_adds_prefix_with_title_and_section(self):
        """Test SAC prefix with full metadata."""
        chunks = [
            Document(
                page_content="Content here.",
                metadata={
                    "title": "Succession Act 1965",
                    "section_number": "77",
                    "parent_section": "PART II",
                },
            )
        ]

        result = _prepend_summary(chunks)
        assert result[0].page_content.startswith("[From: Succession Act 1965, PART II, Section 77]")

    def test_handles_missing_section(self):
        """Test SAC prefix when section info is missing."""
        chunks = [
            Document(
                page_content="Content here.",
                metadata={"title": "Test Doc", "section_number": "", "parent_section": ""},
            )
        ]

        result = _prepend_summary(chunks)
        assert result[0].page_content.startswith("[From: Test Doc]")


class TestChunkLegalDocument:
    def test_end_to_end_chunking(self, sample_document):
        """Test complete chunking pipeline on a sample document."""
        chunks = chunk_legal_document(sample_document)
        assert len(chunks) > 1

        # Each chunk should have the SAC prefix
        for chunk in chunks:
            assert chunk.page_content.startswith("[From:")

        # Metadata should be preserved
        for chunk in chunks:
            assert "title" in chunk.metadata
            assert "source" in chunk.metadata
