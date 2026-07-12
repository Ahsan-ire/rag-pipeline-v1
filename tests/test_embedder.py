"""Tests for the embedding and vector storage module."""

import shutil

import pytest
from langchain_core.documents import Document

from src.bm25_index import load_bm25_index, search_bm25
from src.embedder import (
    _sanitize_metadata,
    add_documents,
    assert_embedding_model,
    clear_store,
    compute_chunk_id,
    get_vector_store,
    rebuild_bm25_index,
    sync_documents,
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


class CountingFakeEmbeddings(FakeEmbeddings):
    """FakeEmbeddings that tallies how many texts it has embedded.

    Lets a test assert that an unchanged re-sync re-embeds nothing — the store
    wrapper's add/update paths both route through ``embed_documents``.
    """

    def __init__(self):
        self.embedded_texts = 0

    def embed_documents(self, texts):
        """Count the texts, then defer to the deterministic fake vectors."""
        self.embedded_texts += len(texts)
        return super().embed_documents(texts)


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

    def test_explicit_store_requires_persist_directory(self, tmp_path, test_documents):
        """An explicit vector_store without its persist_directory must raise:
        the BM25/manifest sidecars would otherwise be silently written beside
        the DEFAULT store while the vectors live elsewhere, permanently
        desyncing hybrid retrieval (D26)."""
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=str(tmp_path / "chroma"),
        )

        with pytest.raises(ValueError, match="persist_directory"):
            add_documents(test_documents, vector_store=store)


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
    """Source-scoped content-hash chunk IDs (D7, hardened per-source in Phase 9)."""

    def test_deterministic_for_same_source_and_text(self):
        assert compute_chunk_id("h.pdf", "hello world") == compute_chunk_id(
            "h.pdf", "hello world"
        )

    def test_different_for_different_text(self):
        assert compute_chunk_id("h.pdf", "hello world") != compute_chunk_id(
            "h.pdf", "goodbye world"
        )

    def test_different_for_different_source_same_text(self):
        """The Phase 9 point: identical text under two sources hashes to two
        distinct IDs, so a per-source replace of A cannot clobber B's identical
        boilerplate chunk."""
        assert compute_chunk_id("a.pdf", "same text") != compute_chunk_id(
            "b.pdf", "same text"
        )

    def test_is_16_char_hex(self):
        chunk_id = compute_chunk_id("h.pdf", "some chunk text")
        assert len(chunk_id) == 16
        int(chunk_id, 16)  # raises ValueError if not hex

    def test_nul_separator_prevents_boundary_ambiguity(self):
        """Without the NUL delimiter, ("ab","c") and ("a","bc") would both
        concatenate to "abc" and collide."""
        assert compute_chunk_id("ab", "c") != compute_chunk_id("a", "bc")


class TestContentHashDedup:
    """Phase 9 inverts the old cross-source dedup: chunk identity is now
    per-source, so text alone no longer collapses two documents into one."""

    def test_dedupes_identical_text_under_same_source(self, tmp_path):
        """Within one source, identical text is still one chunk — Chroma would
        reject the duplicate ID, so the in-batch dedup keeps a single copy."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        same_text = "1.1 Every conveyance of freehold land shall be by deed."
        docs = [
            Document(page_content=same_text, metadata={"source": "handbook.pdf"}),
            Document(page_content=same_text, metadata={"source": "handbook.pdf"}),
        ]
        count = add_documents(docs, vector_store=store, persist_directory=persist_dir)
        assert count == 1

    def test_identical_text_under_different_sources_stored_separately(self, tmp_path):
        """The Phase 9 hardening: the same boilerplate clause in two documents
        is two distinct chunks under source-scoped IDs, and BOTH are stored —
        so a later per-source replace of one cannot destroy the other's copy."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        same_text = "1.1 Every conveyance of freehold land shall be by deed."
        docs = [
            Document(page_content=same_text, metadata={"source": "handbook_v1.pdf"}),
            Document(page_content=same_text, metadata={"source": "handbook_v2.pdf"}),
        ]
        count = add_documents(docs, vector_store=store, persist_directory=persist_dir)
        assert count == 2
        assert len(store.get()["ids"]) == 2


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


class TestSyncDocuments:
    """Per-source replace (Phase 9): add + metadata-update + delete, scoped to
    one source, with the vector store and BM25 sidecar kept in lockstep."""

    def test_changed_text_purges_stale_id_from_store_and_bm25(self, tmp_path):
        """THE TRAP: an insert-only pipeline can never remove the old chunk when
        a doc's text changes. sync must drop the stale ID from BOTH the vector
        store and the BM25 sidecar — otherwise a lexical search would keep
        surfacing a chunk the vector store no longer holds."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        docs = [
            Document(page_content="alpha ZORBLE distinctive token one", metadata={"source": "A.pdf", "page_start": 1}),
            Document(page_content="beta chunk two", metadata={"source": "A.pdf", "page_start": 2}),
            Document(page_content="gamma chunk three", metadata={"source": "A.pdf", "page_start": 3}),
        ]
        sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        stale_id = compute_chunk_id("A.pdf", "alpha ZORBLE distinctive token one")
        assert stale_id in store.get()["ids"]

        # Mutate the first doc's text: its content hash — and thus its ID — changes.
        docs[0] = Document(
            page_content="alpha rewritten without the token",
            metadata={"source": "A.pdf", "page_start": 1},
        )
        result = sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        assert result == {"added": 1, "updated": 0, "deleted": 1}

        # Gone from the vector store...
        assert stale_id not in store.get()["ids"]
        # ...and the rebuilt BM25 sidecar no longer carries it or returns it for
        # the old distinctive token.
        index = load_bm25_index(persist_dir)
        assert stale_id not in index.ids
        hits = search_bm25(index, "ZORBLE", top_k=10)
        assert stale_id not in [hit_id for hit_id, _doc, _score in hits]

    def test_metadata_only_change_updates_in_place(self, tmp_path):
        """A chunker fix that moves a page number but not the text: same ID, new
        metadata. Content-hash dedup alone would keep the stale metadata; sync
        updates the surviving chunk in place."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        docs = [
            Document(page_content="stable chunk text one", metadata={"source": "A.pdf", "page_start": 10}),
            Document(page_content="stable chunk text two", metadata={"source": "A.pdf", "page_start": 20}),
        ]
        sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        chunk_id = compute_chunk_id("A.pdf", "stable chunk text one")

        docs[0] = Document(
            page_content="stable chunk text one",
            metadata={"source": "A.pdf", "page_start": 11},
        )
        result = sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        assert result == {"added": 0, "updated": 1, "deleted": 0}

        stored = store.get(ids=[chunk_id])
        assert stored["metadatas"][0]["page_start"] == 11

    def test_unchanged_resync_re_embeds_nothing(self, tmp_path):
        """An identical re-sync must add/update/delete nothing and re-embed no
        text — the store's embedding function is not called at all."""
        persist_dir = str(tmp_path / "chroma")
        embeddings = CountingFakeEmbeddings()
        store = get_vector_store(embedding_function=embeddings, persist_directory=persist_dir)
        docs = [
            Document(page_content="chunk one text", metadata={"source": "A.pdf", "page_start": 1}),
            Document(page_content="chunk two text", metadata={"source": "A.pdf", "page_start": 2}),
        ]
        first = sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        assert first["added"] == 2
        assert embeddings.embedded_texts > 0

        embeddings.embedded_texts = 0
        second = sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        assert second == {"added": 0, "updated": 0, "deleted": 0}
        assert embeddings.embedded_texts == 0

    def test_two_sources_identical_text_are_independent(self, tmp_path):
        """Source-scoped IDs in the round trip: the same clause under A and B is
        two chunks; removing it from A leaves B's copy untouched."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        shared = "shared boilerplate clause verbatim"
        sync_documents(
            "A.pdf",
            [Document(page_content=shared, metadata={"source": "A.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        sync_documents(
            "B.pdf",
            [Document(page_content=shared, metadata={"source": "B.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        id_a = compute_chunk_id("A.pdf", shared)
        id_b = compute_chunk_id("B.pdf", shared)
        assert id_a != id_b
        assert {id_a, id_b} <= set(store.get()["ids"])

        # Remove the doc from A entirely; B's identical-text chunk must survive.
        result = sync_documents("A.pdf", [], vector_store=store, persist_directory=persist_dir)
        assert result == {"added": 0, "updated": 0, "deleted": 1}
        remaining = set(store.get()["ids"])
        assert id_a not in remaining
        assert id_b in remaining

    def test_empty_documents_clears_only_that_source_and_rebuilds_bm25(self, tmp_path):
        """sync_documents(source, []) is delete-all-for-source. The stale-only
        path must STILL rebuild BM25 (the delete-rebuild trap): the rebuilt
        sidecar drops A's chunk and keeps B's, rather than lingering with A's
        deleted chunk still indexed.

        (The BM25 index's ``ids`` are asserted directly rather than via a
        keyword search: BM25 IDF goes non-positive for a term present in >= half
        the corpus, so a search over a 1-2 doc corpus is an unreliable probe —
        see search_bm25's docstring.)"""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        sync_documents(
            "A.pdf",
            [Document(page_content="alpha ZLORP unique", metadata={"source": "A.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        sync_documents(
            "B.pdf",
            [Document(page_content="beta keeps living", metadata={"source": "B.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        id_a = compute_chunk_id("A.pdf", "alpha ZLORP unique")
        id_b = compute_chunk_id("B.pdf", "beta keeps living")

        # Before the clear, the sidecar carries both chunks.
        assert set(load_bm25_index(persist_dir).ids) == {id_a, id_b}

        result = sync_documents("A.pdf", [], vector_store=store, persist_directory=persist_dir)
        assert result == {"added": 0, "updated": 0, "deleted": 1}
        assert set(store.get()["ids"]) == {id_b}  # only B remains in the store

        # Stale-only path rebuilt BM25: A's chunk is gone, B's chunk remains.
        rebuilt_ids = set(load_bm25_index(persist_dir).ids)
        assert id_a not in rebuilt_ids
        assert rebuilt_ids == {id_b}

    def test_conflicting_metadata_source_raises(self, tmp_path):
        """A doc claiming a different source is a caller bug — storing it under
        this source's ID while claiming another would corrupt identity."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        docs = [Document(page_content="x", metadata={"source": "OTHER.pdf"})]
        with pytest.raises(ValueError, match="source"):
            sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)

    def test_missing_source_is_filled_with_param(self, tmp_path):
        """A missing/empty source is filled in with the sync param, not rejected."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        docs = [Document(page_content="y", metadata={})]
        sync_documents("A.pdf", docs, vector_store=store, persist_directory=persist_dir)
        assert docs[0].metadata["source"] == "A.pdf"
        assert compute_chunk_id("A.pdf", "y") in store.get()["ids"]

    def test_explicit_store_requires_persist_directory(self, tmp_path):
        """Same guard as add_documents: an explicit store without its
        persist_directory would desync the BM25/manifest sidecars (D26)."""
        store = get_vector_store(
            embedding_function=FakeEmbeddings(),
            persist_directory=str(tmp_path / "chroma"),
        )
        with pytest.raises(ValueError, match="persist_directory"):
            sync_documents(
                "A.pdf",
                [Document(page_content="z", metadata={"source": "A.pdf"})],
                vector_store=store,
            )

    def test_clearing_the_only_source_removes_bm25_sidecar_without_crashing(self, tmp_path):
        """FIX 1: sync_documents(source, []) on the ONLY source empties the
        store. rank_bm25 cannot represent an empty corpus (BM25Okapi([]) divides
        by zero on average doc length), so the rebuild must NOT try to build —
        it deletes any existing sidecar instead, so a stale pickle can't ghost
        the just-deleted chunks. load_bm25_index then returns None and retrieval
        degrades to vector-only (also empty)."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        sync_documents(
            "A.pdf",
            [Document(page_content="alpha unique content", metadata={"source": "A.pdf"})],
            vector_store=store,
            persist_directory=persist_dir,
        )
        assert (tmp_path / "chroma" / "bm25_index.pkl").exists()

        # Clear the only source: this used to crash in the BM25 rebuild.
        result = sync_documents("A.pdf", [], vector_store=store, persist_directory=persist_dir)
        assert result == {"added": 0, "updated": 0, "deleted": 1}
        assert store.get()["ids"] == []  # store is empty
        assert not (tmp_path / "chroma" / "bm25_index.pkl").exists()  # sidecar removed
        assert load_bm25_index(persist_dir) is None

    def test_rebuild_bm25_false_defers_sidecar_but_still_writes_store(self, tmp_path):
        """FIX 7: sync_documents(..., rebuild_bm25=False) still writes the vector
        store and returns accurate counts, but does NOT touch the BM25 sidecar —
        a batch indexer defers the O(total_chunks) rebuild and does it once at
        the end via the public rebuild_bm25_index."""
        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        docs = [Document(page_content="alpha unique", metadata={"source": "A.pdf"})]

        result = sync_documents(
            "A.pdf", docs, vector_store=store, persist_directory=persist_dir, rebuild_bm25=False
        )
        assert result == {"added": 1, "updated": 0, "deleted": 0}
        assert compute_chunk_id("A.pdf", "alpha unique") in store.get()["ids"]  # store updated
        assert not (tmp_path / "chroma" / "bm25_index.pkl").exists()  # sidecar deferred

        # The public wrapper then builds the sidecar (+ manifest) exactly once.
        rebuild_bm25_index(vector_store=store, persist_directory=persist_dir)
        assert (tmp_path / "chroma" / "bm25_index.pkl").exists()
        assert set(load_bm25_index(persist_dir).ids) == {compute_chunk_id("A.pdf", "alpha unique")}
