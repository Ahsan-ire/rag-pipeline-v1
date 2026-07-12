"""Tests for the retrieval module."""

import shutil
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from src.retriever import _reciprocal_rank_fusion, format_context, retrieve
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
