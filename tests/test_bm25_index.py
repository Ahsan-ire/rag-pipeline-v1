"""Tests for the BM25 lexical index module."""

from langchain_core.documents import Document

from src.bm25_index import build_bm25_index, load_bm25_index, save_bm25_index, search_bm25


def _corpus():
    ids = ["a", "b", "c"]
    documents = [
        Document(
            page_content="A person aged 18 or over may make a valid will.",
            metadata={"document_type": "legislation"},
        ),
        Document(
            page_content="The priority entry protects a purchaser pending completion.",
            metadata={"document_type": "handbook"},
        ),
        Document(
            page_content="The defendant was found liable for negligence.",
            metadata={"document_type": "case_law"},
        ),
    ]
    return ids, documents


class TestBuildAndSearch:
    def test_exact_token_ranks_top(self):
        ids, documents = _corpus()
        index = build_bm25_index(ids, documents)

        results = search_bm25(index, "priority entry", top_k=3)

        assert results[0][0] == "b"

    def test_respects_document_type_filter(self):
        ids, documents = _corpus()
        index = build_bm25_index(ids, documents)

        results = search_bm25(index, "negligence", top_k=3, document_type="case_law")

        assert len(results) == 1
        assert results[0][1].metadata["document_type"] == "case_law"

    def test_filter_matching_nothing_returns_empty(self):
        ids, documents = _corpus()
        index = build_bm25_index(ids, documents)

        results = search_bm25(index, "anything", top_k=3, document_type="contracts")

        assert results == []

    def test_top_k_limits_results(self):
        """'person' and 'negligence' each uniquely match a different document,
        giving two positive-scoring candidates; top_k=1 must truncate to one."""
        ids, documents = _corpus()
        index = build_bm25_index(ids, documents)

        results = search_bm25(index, "person negligence", top_k=1)

        assert len(results) == 1
        assert results[0][0] in ("a", "c")


class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        ids, documents = _corpus()
        index = build_bm25_index(ids, documents)
        save_bm25_index(index, str(tmp_path))

        loaded = load_bm25_index(str(tmp_path))

        assert loaded is not None
        results = search_bm25(loaded, "priority entry", top_k=1)
        assert results[0][0] == "b"

    def test_load_returns_none_when_missing(self, tmp_path):
        assert load_bm25_index(str(tmp_path / "does_not_exist")) is None
