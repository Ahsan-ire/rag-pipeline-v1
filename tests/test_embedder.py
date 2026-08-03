"""Tests for the embedding and vector storage module."""

import logging
import shutil

import pytest
from langchain_core.documents import Document

from src.bm25_index import load_bm25_index, search_bm25
from src.embedder import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_MODEL,
    MODEL_SPECS,
    EmbeddingModelSpec,
    _apply_model_config,
    _sanitize_metadata,
    _validate_model_specs,
    add_documents,
    assert_embedding_model,
    clear_store,
    compute_chunk_id,
    get_embedding_function,
    get_model_spec,
    get_vector_store,
    rebuild_bm25_index,
    resolve_embedding_model,
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


class TestGetEmbeddingFunction:
    """D47: local-first construction. A warm HuggingFace cache must not make
    the ~25 network HEAD requests per CLI run that re-validate an
    already-cached model, but an unrelated failure must never trigger a
    network-enabled retry (plan-gate finding M12)."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        """get_embedding_function is lru_cached for the process lifetime;
        clear it around each test (same pattern as src.audit._git_sha in
        test_audit.py) so the patched HuggingFaceEmbeddings recorder below is
        actually consulted instead of a value cached by an earlier test, and
        so this module's own cached instance doesn't leak into later tests."""
        get_embedding_function.cache_clear()
        yield
        get_embedding_function.cache_clear()

    def test_local_hit_constructs_once_with_local_files_only(self, monkeypatch):
        """A cache-warm construction succeeds first try: exactly one
        HuggingFaceEmbeddings call, with local_files_only=True."""
        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        result = get_embedding_function()

        assert len(calls) == 1
        assert calls[0]["model_kwargs"]["local_files_only"] is True
        assert isinstance(result, RecorderEmbeddings)

    def test_cache_miss_os_error_retries_without_local_files_only(self, monkeypatch):
        """The failure mode actually verified against the installed stack
        (huggingface_hub 1.22.0 / transformers 5.14.1 / sentence_transformers
        5.6.0): transformers' cached_file catches huggingface_hub's
        LocalEntryNotFoundError and re-raises a plain OSError whose message
        mentions the cached-files / offline-mode fallback. That must trigger
        exactly one retry, without local_files_only, and return its result."""
        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise OSError(
                        "We couldn't connect to 'https://huggingface.co' to load "
                        "the files, and couldn't find them in the cached files. "
                        "Check your internet connection or see how to run the "
                        "library in offline mode."
                    )

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        result = get_embedding_function()

        assert len(calls) == 2
        assert calls[0]["model_kwargs"].get("local_files_only") is True
        assert "local_files_only" not in calls[1]["model_kwargs"]
        assert isinstance(result, RecorderEmbeddings)

    def test_cache_miss_local_entry_not_found_error_retries(self, monkeypatch):
        """Defensive isinstance path: some huggingface_hub/sentence_transformers
        call sites raise LocalEntryNotFoundError directly rather than
        wrapping it in an OSError. A message with none of the OSError
        substring markers isolates this to the isinstance branch."""
        from huggingface_hub.errors import LocalEntryNotFoundError

        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise LocalEntryNotFoundError("no matching entry found")

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        result = get_embedding_function()

        assert len(calls) == 2
        assert calls[0]["model_kwargs"].get("local_files_only") is True
        assert "local_files_only" not in calls[1]["model_kwargs"]
        assert isinstance(result, RecorderEmbeddings)

    def test_unrelated_failure_propagates_without_retry(self, monkeypatch):
        """M12: a failure unrelated to the local cache (auth error, bad
        argument, ...) must propagate immediately rather than silently
        falling back to a network-enabled retry."""
        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                raise ValueError("boom")

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        with pytest.raises(ValueError, match="boom"):
            get_embedding_function()

        assert len(calls) == 1

    def test_permission_error_mentioning_cache_is_not_a_miss(self, monkeypatch):
        """Gate fix: a PermissionError is a filesystem/config fault, NOT a cold
        cache — even when its message mentions "cache" (e.g. a read-only cache
        dir). It must propagate on the FIRST construction, never triggering a
        network-enabled download retry (the old broad substring match treated
        any "cache"-mentioning OSError as a miss). PermissionError is an OSError
        subclass, so this pins the explicit exclusion."""
        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                raise PermissionError("Permission denied writing cache directory")

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        with pytest.raises(PermissionError, match="Permission denied"):
            get_embedding_function()

        assert len(calls) == 1  # single construction, no download retry


class TestPerModelConfigSeam:
    """D55: one env var selects the embedding model, and its MODEL_SPECS entry
    carries everything that differs per model (window, max_seq_length,
    prompts). The bake-off drives arms through this seam, so the seam must be
    provably inert for MiniLM and provably correct for a prompted model."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        """Same reason as TestGetEmbeddingFunction._fresh_cache: the embedding
        function is lru_cached for the process lifetime, so a value cached by
        an earlier test would mask the patched recorder / patched spec here,
        and this class's own cached instance must not leak into later tests."""
        get_embedding_function.cache_clear()
        yield
        get_embedding_function.cache_clear()

    # --- env resolution -------------------------------------------------

    def test_unset_env_resolves_to_default_model(self):
        """No EMBEDDING_MODEL in the environment (conftest scrubs it) means the
        shipped default, not a KeyError and not an empty model id."""
        assert resolve_embedding_model() == DEFAULT_EMBEDDING_MODEL

    def test_env_override_is_returned(self, monkeypatch):
        """A known arm id set in the process environment wins over the
        default — this is how a bake-off arm is selected without a code edit."""
        monkeypatch.setenv("EMBEDDING_MODEL", "Alibaba-NLP/gte-modernbert-base")

        assert resolve_embedding_model() == "Alibaba-NLP/gte-modernbert-base"

    def test_whitespace_only_env_falls_back_to_default(self, monkeypatch):
        """`EMBEDDING_MODEL=" "` (or an empty export) is a typo, not a request
        to embed under a model named " ". Strip-then-or sends it to the
        default rather than to a download of a nonexistent repo id."""
        monkeypatch.setenv("EMBEDDING_MODEL", "   ")

        assert resolve_embedding_model() == DEFAULT_EMBEDDING_MODEL

    def test_unknown_model_id_is_returned_with_a_warning(self, monkeypatch, caplog):
        """An id with no MODEL_SPECS entry still runs (so a new arm can be
        tried immediately), but it must say so: generic defaults mean no known
        context window and no prompts, which the operator has to know about."""
        monkeypatch.setenv("EMBEDDING_MODEL", "some-vendor/unknown-embedder")

        with caplog.at_level(logging.WARNING, logger="src.embedder"):
            resolved = resolve_embedding_model()

        assert resolved == "some-vendor/unknown-embedder"
        assert "MODEL_SPECS" in caplog.text
        assert "some-vendor/unknown-embedder" in caplog.text

    def test_unknown_model_gets_the_generic_spec(self):
        """The generic spec is all-None: no window claim, no max_seq_length
        override, no prompts."""
        assert get_model_spec("some-vendor/unknown-embedder") == EmbeddingModelSpec()

    def test_module_constant_is_the_default_model(self):
        """Guard for the rest of this suite. EMBEDDING_MODEL is bound ONCE, at
        import, from the process environment — so an EMBEDDING_MODEL exported
        in the shell before pytest started would rebind it, and the conftest
        scrub (which runs per test, long after import) cannot unbind it. Every
        assertion below that names MiniLM would then be testing the wrong
        model."""
        assert EMBEDDING_MODEL == DEFAULT_EMBEDDING_MODEL, (
            "EMBEDDING_MODEL is set in the environment this pytest run "
            "inherited, and it is baked into src.embedder at import time. "
            "Run `unset EMBEDDING_MODEL` and re-run the suite."
        )

    # --- constructor kwargs ---------------------------------------------

    def test_minilm_construction_is_byte_identical_to_before(self, monkeypatch):
        """Inertness canary. The seam must change NOTHING about how the shipped
        baseline is built — same model_kwargs, same encode_kwargs, and an EMPTY
        query_encode_kwargs so langchain keeps using encode_kwargs for queries
        exactly as it did before Phase 15."""
        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        get_embedding_function()

        assert calls == [
            {
                "model_name": DEFAULT_EMBEDDING_MODEL,
                "model_kwargs": {"device": "cpu", "local_files_only": True},
                "encode_kwargs": {"normalize_embeddings": True},
                "query_encode_kwargs": {},
            }
        ]

    def test_query_prompt_merges_the_base_encode_kwargs(self, monkeypatch):
        """Merge canary — the unnormalized-query hazard. langchain-huggingface
        1.2.2's embed_query REPLACES encode_kwargs with query_encode_kwargs
        when the latter is non-empty; it does not merge. A query_encode_kwargs
        of just {"prompt": ...} would therefore drop normalize_embeddings and
        embed queries unnormalized against normalized documents. This pins the
        explicit merge."""
        monkeypatch.setitem(
            MODEL_SPECS, DEFAULT_EMBEDDING_MODEL, EmbeddingModelSpec(query_prompt="Q: ")
        )
        get_embedding_function.cache_clear()

        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        get_embedding_function()

        assert calls[0]["query_encode_kwargs"] == {
            "normalize_embeddings": True,
            "prompt": "Q: ",
        }
        assert calls[0]["encode_kwargs"] == {"normalize_embeddings": True}

    def test_doc_prompt_applies_to_documents_only(self, monkeypatch):
        """A doc prompt goes into encode_kwargs (documents); with no query
        prompt configured, query_encode_kwargs stays empty so queries inherit
        encode_kwargs — including the doc prompt is langchain's behaviour, not
        something this seam invents, and there is no doc-prompt-only model in
        MODEL_SPECS today. This pins where the prompt lands."""
        monkeypatch.setitem(
            MODEL_SPECS, DEFAULT_EMBEDDING_MODEL, EmbeddingModelSpec(doc_prompt="D: ")
        )
        get_embedding_function.cache_clear()

        calls = []

        class RecorderEmbeddings:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr("src.embedder.HuggingFaceEmbeddings", RecorderEmbeddings)

        get_embedding_function()

        assert calls[0]["encode_kwargs"] == {
            "normalize_embeddings": True,
            "prompt": "D: ",
        }
        assert calls[0]["query_encode_kwargs"] == {}

    # --- client configuration -------------------------------------------

    def test_apply_model_config_sets_max_seq_length_and_clears_prompt_name(self):
        """max_seq_length is forced onto the loaded client (repos ship
        sentinels), and default_prompt_name is neutralized so a repo config
        cannot silently prefix DOCUMENTS."""

        class StubClient:
            max_seq_length = 512
            default_prompt_name = "query"

        class StubEmb:
            _client = StubClient()

        emb = StubEmb()
        _apply_model_config(emb, EmbeddingModelSpec(context_window=8192, max_seq_length=8192))

        assert emb._client.max_seq_length == 8192
        assert emb._client.default_prompt_name is None

    def test_apply_model_config_raises_when_readback_disagrees(self):
        """A client that accepts the assignment but keeps its own value would
        truncate at a length the guard and provenance never see. The readback
        turns that into a loud failure at construction."""

        class IgnoringClient:
            default_prompt_name = None

            @property
            def max_seq_length(self):
                return 512

            @max_seq_length.setter
            def max_seq_length(self, value):
                pass  # silently ignores the assignment

        class StubEmb:
            model_name = "vendor/stubborn-model"
            _client = IgnoringClient()

        with pytest.raises(ValueError, match="stubborn-model"):
            _apply_model_config(
                StubEmb(), EmbeddingModelSpec(context_window=8192, max_seq_length=8192)
            )

    def test_apply_model_config_is_a_noop_without_a_client(self):
        """Test doubles (RecorderEmbeddings, FakeEmbeddings) have no
        underlying SentenceTransformer. Configuring one must not be a
        precondition for constructing an embedding function."""

        class NoClientEmb:
            pass

        _apply_model_config(
            NoClientEmb(), EmbeddingModelSpec(context_window=8192, max_seq_length=8192)
        )  # must not raise

    # --- spec invariant --------------------------------------------------

    def test_validate_model_specs_rejects_a_window_mismatch(self):
        """The invariant (plan-gate finding A17): the guard checks chunks
        against context_window while the client truncates at max_seq_length, so
        a divergence would let provenance report over-window=0 while the model
        truncated anyway. The error must name the offending model."""
        bad = {
            "vendor/mismatched": EmbeddingModelSpec(context_window=8192, max_seq_length=512)
        }

        with pytest.raises(ValueError, match="vendor/mismatched"):
            _validate_model_specs(bad)

    def test_validate_model_specs_skips_partial_specs(self):
        """A spec that sets only one of the two makes no claim about the other
        (MiniLM: window known, repo max_seq_length left alone) — not a
        violation."""
        _validate_model_specs(
            {
                "vendor/window-only": EmbeddingModelSpec(context_window=256),
                "vendor/nothing": EmbeddingModelSpec(),
            }
        )  # must not raise

    def test_shipped_specs_satisfy_the_invariant(self):
        """The committed MODEL_SPECS — the baseline plus all three bake-off
        arms — pass the same check that runs at import."""
        arms = [
            "Alibaba-NLP/gte-modernbert-base",
            "ibm-granite/granite-embedding-small-english-r2",
            "Qwen/Qwen3-Embedding-0.6B",
        ]
        for arm in arms:
            spec = MODEL_SPECS[arm]
            assert spec.context_window == 8192
            assert spec.max_seq_length == 8192

        _validate_model_specs(MODEL_SPECS)  # must not raise
