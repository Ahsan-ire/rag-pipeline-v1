"""Tests for the embedding and vector storage module."""

import shutil
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.embedder import (
    _sanitize_metadata,
    add_documents,
    assert_embedding_model,
    clear_store,
    compute_chunk_id,
    get_vector_store,
)


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
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=persist_dir,
        )

        count = add_documents(test_documents, vector_store=store, persist_directory=persist_dir)
        assert count == 2

    def test_deduplication(self, tmp_path, test_documents):
        """Test that adding the same documents twice doesn't create duplicates."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=persist_dir,
        )

        count1 = add_documents(test_documents, vector_store=store, persist_directory=persist_dir)
        assert count1 == 2

        count2 = add_documents(test_documents, vector_store=store, persist_directory=persist_dir)
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


class TestSanitizeMetadata:
    """None-valued metadata keys are dropped before Chroma sees them (D22)."""

    def test_drops_none_keys_and_counts(self):
        docs = [
            Document(page_content="x", metadata={"section_number": "1.1", "page_start": None, "page_end": None}),
            Document(page_content="y", metadata={"section_number": "1.2", "page_start": 87, "page_end": 88}),
        ]
        dropped, affected = _sanitize_metadata(docs)
        assert dropped == 2 and affected == 1
        assert docs[0].metadata == {"section_number": "1.1"}    # None keys gone
        assert docs[1].metadata == {"section_number": "1.2", "page_start": 87, "page_end": 88}

    def test_add_documents_accepts_none_page_metadata(self, tmp_path):
        # End-to-end: a chunk with page_start=None must index cleanly (Chroma
        # would otherwise reject the None value).
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=persist_dir,
        )
        docs = [
            Document(
                page_content="[Conveyancing Handbook, Ch.1 General] intro text",
                metadata={
                    "source": "h.pdf",
                    "chapter_number": 1,
                    "section_number": "",
                    "page_start": None,
                    "page_end": None,
                },
            )
        ]
        count = add_documents(docs, vector_store=store, persist_directory=persist_dir)
        assert count == 1
        assert "page_start" not in docs[0].metadata


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


class TestComputeChunkId:
    """Content-hash chunk IDs (D7): identity means identity across re-chunking."""

    def test_deterministic_for_same_text(self):
        assert compute_chunk_id("hello world") == compute_chunk_id("hello world")

    def test_different_for_different_text(self):
        assert compute_chunk_id("hello world") != compute_chunk_id("goodbye world")

    def test_is_16_char_hex(self):
        chunk_id = compute_chunk_id("some chunk text")
        assert len(chunk_id) == 16
        int(chunk_id, 16)  # raises ValueError if not hex


class TestContentHashDedup:
    def test_dedupes_identical_text_regardless_of_source_metadata(self, tmp_path):
        """The D7 payoff: positional IDs (source::chunk_i) would not have caught
        this, since the two documents below differ only in 'source' metadata —
        content-hash identity dedupes on text alone."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        same_text = "1.1 Every conveyance of freehold land shall be by deed."
        docs = [
            Document(page_content=same_text, metadata={"source": "handbook_v1.pdf"}),
            Document(page_content=same_text, metadata={"source": "handbook_v2.pdf"}),
        ]
        count = add_documents(docs, vector_store=store, persist_directory=persist_dir)
        assert count == 1


class TestEmbeddingModelManifest:
    """Recorded at index time, asserted at query time (D5)."""

    def test_add_documents_writes_manifest(self, tmp_path):
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        add_documents(
            [Document(page_content="text", metadata={"source": "x.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        manifest = tmp_path / "chroma" / "embedding_model.txt"
        assert manifest.exists()
        assert manifest.read_text() == "sentence-transformers/all-MiniLM-L6-v2"

    def test_assert_passes_when_model_matches(self, tmp_path):
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        add_documents(
            [Document(page_content="text", metadata={"source": "x.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        assert_embedding_model(persist_dir)  # must not raise

    def test_assert_raises_on_mismatch(self, tmp_path):
        persist_dir = tmp_path / "chroma"
        persist_dir.mkdir()
        (persist_dir / "embedding_model.txt").write_text("some-other-model")

        with pytest.raises(ValueError, match="mismatch"):
            assert_embedding_model(str(persist_dir))

    def test_assert_skips_silently_when_manifest_missing(self, tmp_path):
        assert_embedding_model(str(tmp_path / "never_indexed"))  # must not raise


class TestBM25IndexSideEffect:
    """add_documents keeps a BM25 sidecar in sync with the store (D24)."""

    def test_add_documents_persists_bm25_index(self, tmp_path):
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        add_documents(
            [Document(page_content="1.1 Registration of title.", metadata={"source": "h.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        assert (tmp_path / "chroma" / "bm25_index.pkl").exists()

    def test_no_bm25_file_written_when_nothing_new(self, tmp_path):
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        count = add_documents([], vector_store=store, persist_directory=persist_dir)
        assert count == 0
        assert not (tmp_path / "chroma" / "bm25_index.pkl").exists()
