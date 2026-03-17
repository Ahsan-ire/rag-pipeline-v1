"""Tests for the embedding and vector storage module."""

import shutil
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.embedder import add_documents, clear_store, get_vector_store


class FakeEmbeddings:
    """Fake embedding function that returns fixed-dimension vectors."""

    def embed_documents(self, texts):
        """Return a list of 384-dim vectors (one per text)."""
        import hashlib

        results = []
        for text in texts:
            # Deterministic pseudo-random vector based on text content
            seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
            vector = [(seed * (i + 1) % 1000) / 1000.0 for i in range(384)]
            results.append(vector)
        return results

    def embed_query(self, text):
        """Return a single 384-dim vector for a query."""
        return self.embed_documents([text])[0]


@pytest.fixture
def test_store(tmp_path):
    """Create a temporary ChromaDB store for testing."""
    store = get_vector_store(
        embedding_function=FakeEmbeddings(),
        persist_directory=str(tmp_path / "test_chroma"),
    )
    yield store
    # Cleanup
    shutil.rmtree(str(tmp_path / "test_chroma"), ignore_errors=True)


@pytest.fixture
def test_documents():
    """Sample documents for embedding tests."""
    return [
        Document(
            page_content="Section 77 allows persons aged 18 to make a will.",
            metadata={
                "source": "succession_act.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "77",
            },
        ),
        Document(
            page_content="Section 78 requires wills to be in writing.",
            metadata={
                "source": "succession_act.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "78",
            },
        ),
    ]


class TestAddDocuments:
    def test_adds_documents_to_store(self, tmp_path, test_documents):
        """Test that documents are added to ChromaDB."""
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=str(tmp_path / "chroma"),
        )

        count = add_documents(test_documents, vector_store=store)
        assert count == 2

    def test_deduplication(self, tmp_path, test_documents):
        """Test that adding the same documents twice doesn't create duplicates."""
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=str(tmp_path / "chroma"),
        )

        count1 = add_documents(test_documents, vector_store=store)
        assert count1 == 2

        count2 = add_documents(test_documents, vector_store=store)
        assert count2 == 0

    def test_empty_list_returns_zero(self, test_store):
        """Test that an empty document list returns 0."""
        count = add_documents([], vector_store=test_store)
        assert count == 0


class TestGetVectorStore:
    def test_creates_store(self, tmp_path):
        """Test that get_vector_store returns a Chroma instance."""
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=str(tmp_path / "chroma"),
        )
        assert store is not None


class TestClearStore:
    def test_clears_existing_store(self, tmp_path):
        """Test that clear_store removes the directory."""
        store_dir = tmp_path / "chroma"
        store_dir.mkdir()
        (store_dir / "test.txt").write_text("test")

        clear_store(str(store_dir))
        assert not store_dir.exists()

    def test_clear_nonexistent_store(self, tmp_path):
        """Test that clearing a nonexistent store doesn't raise."""
        clear_store(str(tmp_path / "nonexistent"))
