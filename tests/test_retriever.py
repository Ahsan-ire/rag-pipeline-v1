"""Tests for the retrieval module."""

import logging
import shutil
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from src.bm25_index import load_bm25_index
from src.retriever import (
    RETRIEVAL_MODES,
    REWRITE_LIST_WEIGHT,
    RRF_K,
    _reciprocal_rank_fusion,
    format_context,
    retrieve,
)
from tests.test_embedder import FakeEmbeddings


@pytest.fixture
def seeded_store(tmp_path):
    """Create a ChromaDB store seeded with test documents."""
    from src.embedder import add_documents, get_vector_store

    persist_dir = str(tmp_path / "chroma")
    store = get_vector_store(
        embedding_function=FakeEmbeddings(),
        persist_directory=persist_dir,
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

    add_documents(docs, vector_store=store, persist_directory=persist_dir)
    yield store, persist_dir
    shutil.rmtree(persist_dir, ignore_errors=True)


@pytest.fixture
def exact_token_store(tmp_path):
    """A store with one target chunk containing a distinctive exact phrase and
    several decoys sharing no vocabulary with it — isolates BM25's
    contribution to the fused ranking from FakeEmbeddings' hash-based (i.e.
    semantically meaningless) vector similarity."""
    from src.embedder import add_documents, get_vector_store

    persist_dir = str(tmp_path / "chroma")
    store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)

    target = Document(
        page_content="The priority entry protects a purchaser pending completion of the sale.",
        metadata={"source": "handbook.pdf", "document_type": "handbook", "section_number": "9.4"},
    )
    decoys = [
        Document(
            page_content=text,
            metadata={"source": f"decoy_{i}.pdf", "document_type": "handbook", "section_number": str(i)},
        )
        for i, text in enumerate(
            [
                "Restrictive covenants bind successors in title to the burdened land.",
                "A fee simple estate is the greatest interest known to Irish land law.",
                "Rights of way must be registered as burdens on the servient tenement.",
                "Probate practitioners extract a grant before dealing with estate assets.",
                "Mortgages secured on registered land require a charge on the folio.",
                "Adverse possession claims require twelve years of continuous occupation.",
                "Planning permission conditions can run with the land in some cases.",
                "Judgment mortgages are registered against the debtor's interest in land.",
                "Easements of light protect existing windows from obstruction by neighbours.",
            ]
        )
    ]

    add_documents([target] + decoys, vector_store=store, persist_directory=persist_dir)
    yield store, persist_dir, target
    shutil.rmtree(persist_dir, ignore_errors=True)


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


class TestLoadOnceInjection:
    """Phase 9 (load-once retrieval): a caller that already holds a built
    vector store and/or BM25 index can inject them, so retrieve() skips its
    own per-call disk load / embedding-model manifest check for whichever one
    was injected. This is what lets the evaluator build both exactly once and
    reuse them across a whole golden set instead of paying that cost per
    question."""

    def test_injected_store_and_bm25_skip_all_per_call_loads(self, seeded_store):
        """Both vector_store and bm25_index injected -> get_vector_store,
        load_bm25_index, AND assert_embedding_model must never be called;
        retrieve still returns correct results using only the injected
        objects."""
        store, persist_dir = seeded_store
        # Load the BM25 sidecar ourselves, once, before the call under test —
        # mirroring what a load-once caller (the evaluator) would already
        # have on hand.
        bm25 = load_bm25_index(persist_dir)
        assert bm25 is not None  # sanity: seeded_store did write a sidecar

        def _boom(*args, **kwargs):
            raise AssertionError(
                "must not be called when both vector_store and bm25_index are injected"
            )

        with patch(
            "src.retriever.get_vector_store", side_effect=_boom
        ) as mock_get_store, patch(
            "src.retriever.load_bm25_index", side_effect=_boom
        ) as mock_load_bm25, patch(
            "src.retriever.assert_embedding_model", side_effect=_boom
        ) as mock_assert:
            results = retrieve(
                "making a will",
                top_k=2,
                persist_directory=persist_dir,
                vector_store=store,
                bm25_index=bm25,
            )

        assert len(results) > 0
        mock_get_store.assert_not_called()
        mock_load_bm25.assert_not_called()
        mock_assert.assert_not_called()

    def test_injected_store_only_still_loads_bm25_from_disk_once(self, seeded_store):
        """vector_store injected but bm25_index omitted -> get_vector_store
        and assert_embedding_model are skipped (store already built), but
        load_bm25_index still runs, exactly once, to fill the un-injected
        arm."""
        store, persist_dir = seeded_store

        with patch(
            "src.retriever.load_bm25_index", side_effect=load_bm25_index
        ) as mock_load_bm25, patch(
            "src.retriever.get_vector_store"
        ) as mock_get_store, patch(
            "src.retriever.assert_embedding_model"
        ) as mock_assert:
            results = retrieve(
                "making a will", top_k=2, persist_directory=persist_dir, vector_store=store
            )

        assert len(results) > 0
        mock_load_bm25.assert_called_once_with(persist_dir)
        mock_get_store.assert_not_called()
        mock_assert.assert_not_called()


class TestExactTokenRetrieval:
    """D6's acceptance shape: a term appearing verbatim in exactly one chunk
    must rank that chunk top-3, even against FakeEmbeddings' semantically
    meaningless vector similarity — proving BM25 is genuinely wired into the
    fused ranking, not just present in code."""

    def test_exact_phrase_ranks_top_3(self, exact_token_store):
        store, persist_dir, target = exact_token_store

        with patch("src.retriever.get_vector_store", return_value=store):
            results = retrieve("priority entry", top_k=3, persist_directory=persist_dir)

        top_3_texts = [r["document"].page_content for r in results]
        assert target.page_content in top_3_texts


class TestReciprocalRankFusion:
    """RRF fusion math on synthetic rankings (D6), independent of any store."""

    def test_top_ranked_in_both_arms_wins(self):
        vector_ranked = ["a", "b", "c"]
        bm25_ranked = ["a", "c", "b"]

        fused = _reciprocal_rank_fusion(vector_ranked, bm25_ranked)

        assert fused[0][0] == "a"

    def test_present_in_only_one_arm_still_scored(self):
        vector_ranked = ["a", "b"]
        bm25_ranked = []  # e.g. no BM25 index built yet

        fused = _reciprocal_rank_fusion(vector_ranked, bm25_ranked)

        assert dict(fused)["a"] == 1.0 / (60 + 1)
        assert dict(fused)["b"] == 1.0 / (60 + 2)

    def test_score_is_sum_of_reciprocal_ranks(self):
        # "x" is rank 1 in one arm and rank 2 in the other.
        fused = _reciprocal_rank_fusion(["x", "y"], ["y", "x"])

        assert dict(fused)["x"] == 1.0 / 61 + 1.0 / 62
        assert dict(fused)["y"] == 1.0 / 62 + 1.0 / 61

    def test_empty_lists_produce_no_results(self):
        assert _reciprocal_rank_fusion([], []) == []


class TestVectorOnlyFallback:
    def test_retrieve_works_without_a_bm25_index(self, tmp_path):
        """An index predating Phase 3 (no bm25_index.pkl sidecar) must still
        serve queries, vector-only, rather than raising."""
        from src.embedder import get_vector_store

        persist_dir = str(tmp_path / "chroma")
        store = get_vector_store(embedding_function=FakeEmbeddings(), persist_directory=persist_dir)
        # Seed directly through the raw Chroma API, bypassing add_documents,
        # so no BM25 sidecar or embedding-model manifest is ever written.
        store.add_documents(
            documents=[Document(page_content="A person may make a valid will.", metadata={})],
            ids=["doc_1"],
        )

        with patch("src.retriever.get_vector_store", return_value=store):
            results = retrieve("will", top_k=2, persist_directory=persist_dir)

        assert len(results) == 1

    def test_retrieve_survives_a_corrupt_bm25_sidecar(self, seeded_store):
        """A present-but-unloadable pickle (truncated write, dependency skew)
        must degrade to vector-only retrieval like a missing one — the query
        must still be served, not crash with an UnpicklingError."""
        from pathlib import Path

        store, persist_dir = seeded_store
        # seeded_store wrote a valid sidecar via add_documents; corrupt it.
        (Path(persist_dir) / "bm25_index.pkl").write_bytes(b"\x80truncated garbage")

        with patch("src.retriever.get_vector_store", return_value=store):
            results = retrieve("making a will", top_k=2, persist_directory=persist_dir)

        assert len(results) > 0


class TestFormatContext:
    def test_formats_results(self, mock_retrieved_results):
        """Non-handbook chunks keep the generic [Source i: ...] header."""
        context = format_context(mock_retrieved_results)

        assert "[Source 1:" in context
        assert "[Source 2:" in context
        assert "Succession Act 1965" in context
        assert "---" in context

    def test_formats_handbook_results(self, handbook_retrieved_results):
        """Handbook chunks get the compact [Handbook, para X, p.N] locator; a
        multi-page chunk renders the page range (Phase 4 / D28)."""
        context = format_context(handbook_retrieved_results)

        assert "[Handbook, para 14.8.5, p.412]" in context
        assert "[Handbook, para 1.2, pp.1–2]" in context
        assert "[Source 1:" not in context  # handbook branch, not the generic one

    def test_empty_results(self):
        """Test formatting with no results."""
        context = format_context([])
        assert "No relevant documents found" in context

    def test_formats_handbook_appendix_section(self):
        """An APPENDIX section_number renders verbatim with no "para" token,
        mirroring chunker._prefix (chunker.py:550); a normal section retrieved
        in the same call still gets the "para X" form."""
        docs = [
            Document(
                page_content="Appendix content.",
                metadata={
                    "source": "Conveyancing_Handbook.pdf",
                    "document_type": "handbook",
                    "section_number": "APPENDIX 14.1",
                    "page_start": 87,
                    "page_end": 87,
                },
            ),
            Document(
                page_content="Regular section content.",
                metadata={
                    "source": "Conveyancing_Handbook.pdf",
                    "document_type": "handbook",
                    "section_number": "3.2.1",
                    "page_start": 40,
                    "page_end": 40,
                },
            ),
        ]
        results = [
            {"document": docs[0], "score": 0.5, "metadata": docs[0].metadata},
            {"document": docs[1], "score": 0.4, "metadata": docs[1].metadata},
        ]

        context = format_context(results)

        assert "[Handbook, APPENDIX 14.1, p.87]" in context
        assert "para APPENDIX" not in context
        assert "[Handbook, para 3.2.1, p.40]" in context

    def test_formats_handbook_appendix_page_range(self):
        """A multi-page appendix chunk renders the page range while keeping
        the section verbatim (no "para" token), per D21 + the APPENDIX rule."""
        doc = Document(
            page_content="Appendix content spanning pages.",
            metadata={
                "source": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "section_number": "APPENDIX 14.1",
                "page_start": 87,
                "page_end": 89,
            },
        )
        results = [{"document": doc, "score": 0.5, "metadata": doc.metadata}]

        context = format_context(results)

        assert "[Handbook, APPENDIX 14.1, pp.87–89]" in context


class TestRetrieveModes:
    """Phase 10 ablation (D38): ``mode`` gates both backend resolution and arm
    execution, so the evaluator can measure each arm's standalone hit@k
    without paying for, or being contaminated by, the other arm."""

    def test_unknown_mode_raises_before_any_io(self):
        """An unknown mode must raise ValueError before touching any backend
        seam — validated first so a typo'd mode fails fast and cheap, never
        after opening the store or unpickling the BM25 sidecar. Patching all
        three IO seams to raise AssertionError, and getting ValueError back
        instead, proves none of them fired."""

        def _boom(*args, **kwargs):
            raise AssertionError("no IO on bad mode")

        with patch("src.retriever.get_vector_store", side_effect=_boom), patch(
            "src.retriever.load_bm25_index", side_effect=_boom
        ), patch("src.retriever.assert_embedding_model", side_effect=_boom):
            with pytest.raises(ValueError) as exc_info:
                retrieve("q", mode="banana")

        message = str(exc_info.value)
        assert "banana" in message
        # The valid set is spelled out in the error so a caller sees the fix,
        # not just that they got it wrong.
        for valid_mode in RETRIEVAL_MODES:
            assert valid_mode in message

    def test_vector_mode_never_loads_bm25_or_warns(self, seeded_store, caplog):
        """mode="vector" runs the vector arm only: it must never call
        load_bm25_index or emit the missing-sidecar warning, even though the
        seeded store has a valid BM25 sidecar sitting right beside it."""
        store, persist_dir = seeded_store

        with patch(
            "src.retriever.load_bm25_index",
            side_effect=AssertionError("must not load BM25 index in vector mode"),
        ), caplog.at_level(logging.WARNING, logger="src.retriever"):
            results = retrieve(
                "making a will",
                top_k=2,
                mode="vector",
                vector_store=store,
                persist_directory=persist_dir,
            )

        assert len(results) > 0
        assert "No BM25 index" not in caplog.text

    def test_bm25_mode_never_touches_vector_backend(self, exact_token_store):
        """mode="bm25" runs the BM25 arm only: it must never resolve a vector
        backend via get_vector_store/assert_embedding_model, and must never
        query an injected vector_store either.

        Uses exact_token_store rather than seeded_store: seeded_store has
        only 2 documents with zero shared vocabulary between them, which
        makes rank_bm25's classic idf (log((N-freq+0.5)/(freq+0.5))) come out
        to exactly 0 for every term (freq=1, N=2) — so no query can ever get
        a positive BM25 score there. exact_token_store's 10 documents (D6's
        exact-phrase fixture) don't have that degeneracy, so "priority entry"
        genuinely surfaces its target chunk.
        """
        store, persist_dir, target = exact_token_store
        bm25 = load_bm25_index(persist_dir)
        assert bm25 is not None  # sanity: exact_token_store did write a sidecar

        class ExplodingStore:
            def similarity_search_with_relevance_scores(self, *args, **kwargs):
                raise AssertionError("vector arm must not run in bm25 mode")

        stub = ExplodingStore()

        with patch(
            "src.retriever.get_vector_store",
            side_effect=AssertionError("must not resolve vector store in bm25 mode"),
        ), patch(
            "src.retriever.assert_embedding_model",
            side_effect=AssertionError("must not check embedding model in bm25 mode"),
        ):
            results = retrieve(
                "priority entry",
                top_k=2,
                mode="bm25",
                vector_store=stub,
                bm25_index=bm25,
                persist_directory=persist_dir,
            )

        # Results are non-empty (BM25 found something) and the call returning
        # normally at all proves none of the AssertionErrors above fired.
        assert len(results) > 0
        assert target.page_content in [r["document"].page_content for r in results]

    def test_bm25_mode_missing_sidecar_warns_and_returns_empty(self, tmp_path, caplog):
        """mode="bm25" with no sidecar (load_bm25_index returns None) warns
        and returns [] rather than silently falling back to a vector search
        that this mode never runs. get_vector_store/assert_embedding_model
        are patched to raise too, proving bm25 mode skips them outright."""
        with patch("src.retriever.load_bm25_index", return_value=None), patch(
            "src.retriever.get_vector_store",
            side_effect=AssertionError("must not resolve vector store in bm25 mode"),
        ), patch(
            "src.retriever.assert_embedding_model",
            side_effect=AssertionError("must not check embedding model in bm25 mode"),
        ), caplog.at_level(logging.WARNING, logger="src.retriever"):
            results = retrieve("q", mode="bm25", persist_directory=str(tmp_path))

        assert results == []
        assert "No BM25 index" in caplog.text

    def test_single_arm_preserves_candidate_order(self, tmp_path):
        """A single-arm mode is RRF over one ranked list, which is
        order-preserving: the returned results must follow the arm's
        candidate order, not be re-sorted by the arm's own score values."""
        # Deliberately NOT in score order (0.1, 0.9, 0.5) so a test that
        # passed only because results got re-sorted by score would fail here.
        docs_and_scores = [
            (Document(id="doc_c", page_content="third", metadata={"section_number": "3"}), 0.1),
            (Document(id="doc_a", page_content="first", metadata={"section_number": "1"}), 0.9),
            (Document(id="doc_b", page_content="second", metadata={"section_number": "2"}), 0.5),
        ]

        class FakeStore:
            def similarity_search_with_relevance_scores(self, *args, **kwargs):
                return docs_and_scores

        results = retrieve(
            "q",
            top_k=3,
            mode="vector",
            vector_store=FakeStore(),
            bm25_index=None,
            persist_directory=str(tmp_path),
        )

        section_numbers = [r["metadata"]["section_number"] for r in results]
        assert section_numbers == ["3", "1", "2"]

    def test_strict_errors_propagates_arm_exception_vs_default_swallow(self, tmp_path):
        """By default an arm's search exception is logged and swallowed —
        production retrieval degrades rather than 500s. strict_errors=True
        lets it propagate, so eval runs don't silently score an operational
        failure as a retrieval miss."""

        class ExplodingStore:
            def similarity_search_with_relevance_scores(self, *args, **kwargs):
                raise RuntimeError("boom")

        stub = ExplodingStore()

        # (a) default: swallowed, no other arm ran, so no candidates at all.
        results = retrieve(
            "q", mode="vector", vector_store=stub, persist_directory=str(tmp_path)
        )
        assert results == []

        # (b) strict: the same exception propagates instead.
        with pytest.raises(RuntimeError):
            retrieve(
                "q",
                mode="vector",
                vector_store=stub,
                persist_directory=str(tmp_path),
                strict_errors=True,
            )


class TestRewrites:
    """Phase 13 (D43): ``rewrites`` runs each used arm once per sub-query
    (original query first, then deduped rewrites) and fuses one ranked-ID
    list per (arm, sub-query), weighting rewrite-derived lists at
    ``REWRITE_LIST_WEIGHT``. The identity proof (rewrites=None/omitted must
    be byte-identical to pre-Phase-13 retrieve) matters as much as the new
    behavior — the whole existing test suite above re-validates it too, since
    none of those calls pass ``rewrites`` at all."""

    def test_rewrites_none_and_omitted_are_identical(self, seeded_store):
        """rewrites=None and omitting the keyword entirely must produce the
        same documents in the same order with the same fused scores."""
        store, persist_dir = seeded_store

        with patch("src.retriever.get_vector_store", return_value=store):
            omitted = retrieve("making a will", top_k=2, persist_directory=persist_dir)
            explicit_none = retrieve(
                "making a will", top_k=2, persist_directory=persist_dir, rewrites=None
            )

        assert len(omitted) == len(explicit_none) > 0
        for a, b in zip(omitted, explicit_none):
            assert a["document"].id == b["document"].id
            assert a["score"] == b["score"]

    def test_casefold_dedup_behaves_like_no_rewrites(self, seeded_store):
        """A rewrite list of [QUERY.upper(), "  ", query] must all be dropped
        (casefold-duplicate of the original, whitespace-only, exact duplicate
        of the original) — the fused result must be identical to passing no
        rewrites at all."""
        store, persist_dir = seeded_store
        query = "making a will"

        with patch("src.retriever.get_vector_store", return_value=store):
            baseline = retrieve(query, top_k=2, persist_directory=persist_dir)
            deduped = retrieve(
                query,
                top_k=2,
                persist_directory=persist_dir,
                rewrites=[query.upper(), "  ", query],
            )

        assert len(baseline) == len(deduped) > 0
        for a, b in zip(baseline, deduped):
            assert a["document"].id == b["document"].id
            assert a["score"] == b["score"]

    def test_rewrite_only_document_enters_fused_top_k(self):
        """A document the original query's own ranked list never returns at
        all (the fake store below has nothing on file for that query text)
        still enters the fused top-k once a rewrite's own ranked list finds
        it — proving the rewrite's sub-query genuinely runs its own search
        rather than being accepted and ignored. A control call without the
        rewrite never sees the document, confirming it isn't a fixture
        accident."""

        original_hit = Document(
            id="doc_original", page_content="original hit", metadata={"section_number": "1"}
        )
        rewrite_only_hit = Document(
            id="doc_rewrite_only", page_content="rewrite hit", metadata={"section_number": "2"}
        )

        class DispatchingStore:
            """Returns a canned ranked list keyed on the exact sub-query
            text, so each sub-query in the loop can be driven independently."""

            def similarity_search_with_relevance_scores(self, query, k=None, filter=None):
                if query == "vague original phrasing":
                    return [(original_hit, 0.9)]
                if query == "distinctive rewrite phrasing":
                    return [(rewrite_only_hit, 0.9)]
                return []

        results = retrieve(
            "vague original phrasing",
            top_k=2,
            mode="vector",
            vector_store=DispatchingStore(),
            rewrites=["distinctive rewrite phrasing"],
        )
        assert "doc_rewrite_only" in [r["document"].id for r in results]

        control = retrieve(
            "vague original phrasing", top_k=2, mode="vector", vector_store=DispatchingStore()
        )
        assert "doc_rewrite_only" not in [r["document"].id for r in control]

    def test_rewrite_only_doc_resolves_to_full_result_dict(self):
        """id_to_doc is one dict shared across every (arm, sub-query) list:
        a doc_id contributed ONLY by a rewrite's ranked list must still
        resolve to a full result — document (with its page_content) and
        metadata both present — not just a bare id with missing fields."""

        rewrite_only_hit = Document(
            id="doc_rw", page_content="rewrite hit content", metadata={"section_number": "9"}
        )

        class DispatchingStore:
            def similarity_search_with_relevance_scores(self, query, k=None, filter=None):
                if query == "distinctive rewrite phrasing":
                    return [(rewrite_only_hit, 0.9)]
                return []

        results = retrieve(
            "vague original phrasing",
            top_k=2,
            mode="vector",
            vector_store=DispatchingStore(),
            rewrites=["distinctive rewrite phrasing"],
        )

        match = next(r for r in results if r["document"].id == "doc_rw")
        assert match["document"].page_content == "rewrite hit content"
        assert match["metadata"] == {"section_number": "9"}

    def test_full_production_shape_rewrite_bundle_cannot_outvote_original(self, monkeypatch):
        """The exact production shape the flat-0.5 weight broke (gate fix): a
        HYBRID retrieve with 3 rewrites — 2 arms × 3 rewrites = 6 rewrite-derived
        lists — all ranking a noise doc first, while both original-query arms
        rank the right doc first. With the per-rewrite-budget weight (0.5/3 per
        list) the whole rewrite bundle contributes 6 × (0.5/3)/(k+1) = 1.0/(k+1),
        capped at half the original's 2.0/(k+1), so the RIGHT doc wins. Under the
        old flat 0.5 the bundle was 6 × 0.5 = 3.0 > 2.0 and noise would have won."""
        right = Document(id="right", page_content="right", metadata={"section_number": "1"})
        noise = Document(id="noise", page_content="noise", metadata={"section_number": "2"})

        class Store:
            def similarity_search_with_relevance_scores(self, query, k=None, filter=None):
                # Original query ranks the right doc; every rewrite ranks noise.
                return [(right, 0.9)] if query == "orig" else [(noise, 0.9)]

        def fake_search_bm25(index, query, k, document_type=None):
            return [("right", right, 5.0)] if query == "orig" else [("noise", noise, 5.0)]

        monkeypatch.setattr("src.retriever.search_bm25", fake_search_bm25)

        results = retrieve(
            "orig",
            top_k=2,
            mode="hybrid",
            vector_store=Store(),
            bm25_index=object(),  # non-None so the bm25 arm runs; search is faked
            rewrites=["rw one", "rw two", "rw three"],
        )

        assert results[0]["document"].id == "right"
        # And confirm the numbers: right = 2/(k+1); noise = 1/(k+1).
        by_id = {r["document"].id: r["score"] for r in results}
        assert by_id["right"] == pytest.approx(2.0 / (RRF_K + 1))
        assert by_id["noise"] == pytest.approx(1.0 / (RRF_K + 1))

    def test_single_rewrite_keeps_weight_half(self):
        """With exactly ONE effective rewrite, its per-list weight is
        REWRITE_LIST_WEIGHT / max(1, 1) = 0.5 — unchanged from before the gate
        fix: a rewrite-only doc scores exactly 0.5/(k+1) against the original's
        1.0/(k+1)."""
        doc_a = Document(id="a", page_content="a", metadata={})
        doc_b = Document(id="b", page_content="b", metadata={})

        class Store:
            def similarity_search_with_relevance_scores(self, query, k=None, filter=None):
                if query == "orig":
                    return [(doc_a, 0.9)]
                if query == "rw":
                    return [(doc_b, 0.9)]
                return []

        results = retrieve("orig", top_k=2, mode="vector", vector_store=Store(), rewrites=["rw"])

        by_id = {r["document"].id: r["score"] for r in results}
        assert by_id["a"] == pytest.approx(1.0 / (RRF_K + 1))
        assert by_id["b"] == pytest.approx(REWRITE_LIST_WEIGHT / (RRF_K + 1))


class TestPerSubQueryStrictErrors:
    """Phase 13 (D43): the strict_errors/swallow contract applies per
    sub-query, not just per arm — an exception on a rewrite's search must be
    isolated from the original query's own result, exactly as an exception on
    one arm is already isolated from the other arm today."""

    def test_strict_errors_per_subquery(self):
        good_doc = Document(id="doc_good", page_content="ok", metadata={})

        class RaisesOnRewriteStore:
            """Raises only when searched with the rewrite's exact text; the
            original sub-query's search succeeds normally."""

            def similarity_search_with_relevance_scores(self, query, k=None, filter=None):
                if query == "bad rewrite":
                    raise RuntimeError("boom")
                return [(good_doc, 0.9)]

        # Default: the rewrite sub-query's exception is logged and swallowed;
        # the original sub-query's result still comes through untouched.
        results = retrieve(
            "original query",
            top_k=2,
            mode="vector",
            vector_store=RaisesOnRewriteStore(),
            rewrites=["bad rewrite"],
        )
        assert [r["document"].id for r in results] == ["doc_good"]

        # strict_errors=True: the same exception now propagates instead.
        with pytest.raises(RuntimeError):
            retrieve(
                "original query",
                top_k=2,
                mode="vector",
                vector_store=RaisesOnRewriteStore(),
                rewrites=["bad rewrite"],
                strict_errors=True,
            )


class TestWeightedReciprocalRankFusion:
    """Phase 13 (D43): ``_reciprocal_rank_fusion`` gains optional per-list
    ``weights`` so rewrite-derived lists can be counted at less than an
    original-query list."""

    def test_correlated_rewrite_lists_cannot_outvote_original_arms(self):
        """The adversarial case D43 names directly, at the PRODUCTION per-list
        weight (gate fix): three rewrite-derived lists each weighted
        ``REWRITE_LIST_WEIGHT / n_rewrites`` (0.5/3) all ranking generic chunk
        "x" first (3 × (0.5/3)/(60+1) = 0.5/(60+1)) must not outrank two
        original-query lists (weight 1.0) both ranking the actually-right chunk
        "y" first (2 × 1.0/(60+1)). The whole rewrite bundle now caps at half a
        single original arm, so this holds with margin to spare."""
        rewrite_lists = [["x"], ["x"], ["x"]]
        original_lists = [["y"], ["y"]]
        per_rewrite = REWRITE_LIST_WEIGHT / 3  # the production per-list weight
        weights = [per_rewrite, per_rewrite, per_rewrite, 1.0, 1.0]

        fused = _reciprocal_rank_fusion(*rewrite_lists, *original_lists, weights=weights)

        assert fused[0][0] == "y"

    def test_weights_none_equals_explicit_all_ones(self):
        """weights=None must be exactly equivalent to passing 1.0 for every
        list — backward compatible with every existing direct caller."""
        lists = [["a", "b"], ["b", "a"]]

        assert _reciprocal_rank_fusion(*lists) == _reciprocal_rank_fusion(
            *lists, weights=[1.0, 1.0]
        )

    def test_mismatched_weights_length_raises(self):
        """A weights list whose length doesn't match the number of ranked-ID
        lists is a caller bug — raise loudly rather than silently
        mis-weighting or index-erroring."""
        with pytest.raises(ValueError):
            _reciprocal_rank_fusion(["a"], ["b"], weights=[1.0])
