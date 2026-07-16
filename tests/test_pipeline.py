"""Tests for the pipeline orchestration module."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.generator import REFUSAL_PHRASE
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    REFUSAL,
)
from src.pipeline import index_documents, query
from src.query_rewrite import Expansion, REWRITE_MODEL, STATUS_DISABLED, STATUS_LIVE


@pytest.fixture(autouse=True)
def _stub_expand_query(monkeypatch):
    """File-level autouse: every test in this module gets a disabled-stub
    ``Expansion`` from ``src.pipeline.expand_query`` unless the test itself
    overrides the patch. Without this, any pipeline test that reaches the
    real ``expand_query`` (none of them mock it, since query expansion
    predates most of these tests) would try to construct a live rewrite LLM
    — this stub is what keeps the whole file at zero API calls. Individual
    tests that need to assert on the expansion call, or exercise a non-empty
    rewrite list, re-patch ``src.pipeline.expand_query`` themselves inside
    the test body (patch context managers nest inside this fixture's
    monkeypatch and are restored before it is)."""

    def _stub(question, *, enabled=True):
        return Expansion(question, (), REWRITE_MODEL, STATUS_DISABLED)

    monkeypatch.setattr("src.pipeline.expand_query", _stub)


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
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}), \
             patch("src.pipeline.rebuild_bm25_index"):
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
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}), \
             patch("src.pipeline.rebuild_bm25_index"):
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
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}), \
             patch("src.pipeline.rebuild_bm25_index"):
            count = index_documents("/test/doc.pdf", "case_law")

        assert count == 1

    def test_no_documents_returns_zero(self):
        """Test that an empty source returns 0."""
        with patch("src.pipeline.load_directory", return_value=[]):
            count = index_documents("/empty/dir/", "legislation")

        assert count == 0

    def test_indexes_handbook_pdf_via_page_aware_route(self):
        """A handbook PDF routes through load_handbook_pdf + chunk_handbook."""
        handbook_triple = (
            "CHAPTER 1\nGENERAL\n1.1 body text.",
            [],
            {
                "source": "/data/Conveyancing_Handbook.pdf",
                "title": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "date": "",
            },
        )
        mock_chunks = [
            Document(page_content="[Conveyancing Handbook, Ch.1 General, para 1.1] body",
                     metadata={"section_number": "1.1"})
        ]
        with patch("src.pipeline.load_handbook_pdf", return_value=handbook_triple) as m_load, \
             patch("src.pipeline.chunk_handbook", return_value=mock_chunks) as m_chunk, \
             patch("src.pipeline.load_pdf") as m_load_pdf, \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}):
            count = index_documents("/data/Conveyancing_Handbook.pdf", "handbook")

        assert count == 1
        m_load.assert_called_once()
        m_chunk.assert_called_once()
        m_load_pdf.assert_not_called()  # the legislation loader is bypassed

    def test_handbook_empty_extraction_returns_zero(self):
        """An empty extraction (missing/blank PDF) returns 0 without chunking."""
        with patch("src.pipeline.load_handbook_pdf", return_value=("", [], {})), \
             patch("src.pipeline.chunk_handbook") as m_chunk, \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 0, "updated": 0, "deleted": 0}):
            count = index_documents("/data/missing.pdf", "handbook")

        assert count == 0
        m_chunk.assert_not_called()

    def test_reset_clears_store_before_indexing(self):
        """--reset clears the vector store first (positional-ID dedup guard)."""
        handbook_triple = ("CHAPTER 1\n1.1 x", [], {"source": "h.pdf"})
        with patch("src.pipeline.clear_store") as m_clear, \
             patch("src.pipeline.load_handbook_pdf", return_value=handbook_triple), \
             patch("src.pipeline.chunk_handbook", return_value=[Document(page_content="x", metadata={})]), \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}):
            index_documents("/data/handbook.pdf", "handbook", reset=True)

        m_clear.assert_called_once()

    def test_reset_does_not_clear_when_chunking_raises(self):
        """A mis-routed handbook PDF raises BEFORE clear_store, sparing the index."""
        triple = ("text with no chapter markers", [], {"source": "x.pdf"})
        with patch("src.pipeline.clear_store") as m_clear, \
             patch("src.pipeline.load_handbook_pdf", return_value=triple), \
             patch("src.pipeline.chunk_handbook", side_effect=ValueError("no CHAPTER markers")), \
             patch("src.pipeline.sync_documents") as m_sync:
            with pytest.raises(ValueError):
                index_documents("/data/notahandbook.pdf", "handbook", reset=True)

        m_clear.assert_not_called()   # store survives
        m_sync.assert_not_called()

    def test_handbook_type_rejects_non_pdf_source(self):
        """--type handbook requires a single PDF; a dir/URL would mis-tag chunks."""
        with pytest.raises(ValueError, match="PDF"):
            index_documents("/data/legislation/", "handbook")

    def test_handbook_sync_scoped_to_the_cli_source_path(self):
        """The sync scope must be the CLI path verbatim — ingest writes the same
        string into metadata["source"], and any other spelling would be a
        different source (D37)."""
        handbook_triple = ("CHAPTER 1\n1.1 x", [], {"source": "./data/h.pdf"})
        chunks = [Document(page_content="x", metadata={"source": "./data/h.pdf"})]
        with patch("src.pipeline.load_handbook_pdf", return_value=handbook_triple), \
             patch("src.pipeline.chunk_handbook", return_value=chunks), \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}) as m_sync:
            index_documents("./data/h.pdf", "handbook", persist_directory="/tmp/pd")

        m_sync.assert_called_once_with(
            "./data/h.pdf", chunks, persist_directory="/tmp/pd"
        )

    def test_multi_source_directory_syncs_each_source_separately(self):
        """A directory of documents yields chunks from several sources; each
        source syncs under its own scope so one document's re-index can never
        delete another's chunks (D37)."""
        chunks_a = [Document(page_content="a", metadata={"source": "a.html"})]
        chunks_b = [Document(page_content="b", metadata={"source": "b.html"})]
        mock_docs = [
            Document(page_content="A", metadata={"source": "a.html"}),
            Document(page_content="B", metadata={"source": "b.html"}),
        ]
        with patch("src.pipeline.load_directory", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document",
                   side_effect=[chunks_a, chunks_b]), \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}) as m_sync, \
             patch("src.pipeline.rebuild_bm25_index"):
            count = index_documents("/test/data/", "legislation")

        assert count == 2
        sources_synced = [call.args[0] for call in m_sync.call_args_list]
        assert sources_synced == ["a.html", "b.html"]
        assert m_sync.call_args_list[0].args[1] == chunks_a
        assert m_sync.call_args_list[1].args[1] == chunks_b
        with pytest.raises(ValueError, match="PDF"):
            index_documents("https://example.com/act", "handbook")

    def test_handbook_empty_chunk_list_skips_sync_and_returns_zero(self):
        """FIX 5: chunk_handbook returning [] from a real (non-blank) PDF must
        NOT reach sync_documents — sync(source, []) would silently delete every
        stored chunk for the handbook (the whole corpus). An empty chunk list is
        never an intended delete-all; the guard returns 0 without touching the
        store."""
        handbook_triple = ("CHAPTER 1\n1.1 body text.", [], {"source": "h.pdf"})
        with patch("src.pipeline.load_handbook_pdf", return_value=handbook_triple), \
             patch("src.pipeline.chunk_handbook", return_value=[]), \
             patch("src.pipeline.sync_documents") as m_sync:
            count = index_documents("/data/handbook.pdf", "handbook")

        assert count == 0
        m_sync.assert_not_called()

    def test_multi_source_chunk_missing_source_raises(self):
        """FIX 6: a chunk missing its 'source' metadata (ingest guarantees it) is
        a loader/chunker regression. Grouping by a fallback would silently mis-
        scope the per-source sync and could delete another document's chunks, so
        the grouping raises ValueError naming the offending chunk's leading text
        instead of syncing."""
        mock_docs = [Document(page_content="A", metadata={"source": "a.html"})]
        bad_chunks = [
            Document(page_content="orphan chunk with no source metadata at all", metadata={})
        ]
        with patch("src.pipeline.load_directory", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", return_value=bad_chunks), \
             patch("src.pipeline.sync_documents") as m_sync, \
             patch("src.pipeline.rebuild_bm25_index") as m_rebuild:
            with pytest.raises(ValueError, match="source"):
                index_documents("/test/data/", "legislation")

        m_sync.assert_not_called()
        m_rebuild.assert_not_called()

    def test_multi_source_rebuilds_bm25_once_after_all_syncs(self):
        """FIX 7: per-source syncs pass rebuild_bm25=False; the global BM25
        sidecar is rebuilt exactly ONCE after the loop (not once per source,
        which would be O(N x total_chunks)), and only because something
        changed."""
        chunks_a = [Document(page_content="a", metadata={"source": "a.html"})]
        chunks_b = [Document(page_content="b", metadata={"source": "b.html"})]
        mock_docs = [
            Document(page_content="A", metadata={"source": "a.html"}),
            Document(page_content="B", metadata={"source": "b.html"}),
        ]
        with patch("src.pipeline.load_directory", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", side_effect=[chunks_a, chunks_b]), \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 1, "updated": 0, "deleted": 0}) as m_sync, \
             patch("src.pipeline.rebuild_bm25_index") as m_rebuild:
            index_documents("/test/data/", "legislation", persist_directory="/tmp/pd")

        # Every per-source sync deferred its own rebuild...
        for call in m_sync.call_args_list:
            assert call.kwargs["rebuild_bm25"] is False
        # ...and the global rebuild ran exactly once, after the loop, at the dir.
        m_rebuild.assert_called_once_with(persist_directory="/tmp/pd")

    def test_multi_source_skips_bm25_rebuild_when_all_syncs_are_noops(self):
        """FIX 7: if every per-source sync reports no change, the global BM25
        rebuild is skipped entirely — the existing sidecar stays untouched."""
        chunks_a = [Document(page_content="a", metadata={"source": "a.html"})]
        chunks_b = [Document(page_content="b", metadata={"source": "b.html"})]
        mock_docs = [
            Document(page_content="A", metadata={"source": "a.html"}),
            Document(page_content="B", metadata={"source": "b.html"}),
        ]
        with patch("src.pipeline.load_directory", return_value=mock_docs), \
             patch("src.pipeline.chunk_legal_document", side_effect=[chunks_a, chunks_b]), \
             patch("src.pipeline.sync_documents",
                   return_value={"added": 0, "updated": 0, "deleted": 0}), \
             patch("src.pipeline.rebuild_bm25_index") as m_rebuild:
            index_documents("/test/data/", "legislation")

        m_rebuild.assert_not_called()


class TestQuery:
    @pytest.fixture(autouse=True)
    def _redirect_audit_log(self, tmp_path, monkeypatch):
        """Send every query's audit event to a tmp file, never ./logs/.

        The audit wiring is always-on, so any query test that does not patch
        log_event would otherwise write a real JSONL line into the repo's
        logs/ directory. Redirecting AUDIT_LOG_PATH per-test keeps the suite
        hermetic (see src/audit.py log_event path resolution). _git_sha is
        patched too — build_event would otherwise spawn a real ``git``
        subprocess on every query call, breaking the no-unmocked-IO rule.
        The load-once builder (D37) is stubbed for the same reason: query()
        builds the store + BM25 sidecar up front via load_retrieval_context and
        injects them into retrieve(), and since every test here patches
        src.pipeline.retrieve, the stub objects are never dereferenced."""
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
        monkeypatch.setattr("src.audit._git_sha", lambda: "t3st5ha")
        monkeypatch.setattr(
            "src.pipeline.load_retrieval_context", lambda *a, **k: (object(), object())
        )

    def test_query_builds_once_and_injects_into_retrieve(self, monkeypatch):
        """query() builds the store + BM25 sidecar once up front via the shared
        load_retrieval_context helper (which also runs the manifest check) and
        injects them into retrieve() along with persist_directory (load-once,
        D37)."""
        store_sentinel, bm25_sentinel = object(), object()
        calls = {"context": 0}

        def fake_context(persist_directory):
            calls["context"] += 1
            assert persist_directory == "/tmp/custom_pd"
            return store_sentinel, bm25_sentinel

        monkeypatch.setattr("src.pipeline.load_retrieval_context", fake_context)

        with patch("src.pipeline.retrieve", return_value=[]) as m_retrieve:
            query("anything", persist_directory="/tmp/custom_pd")

        assert calls == {"context": 1}
        kwargs = m_retrieve.call_args.kwargs
        assert kwargs["vector_store"] is store_sentinel
        assert kwargs["bm25_index"] is bm25_sentinel
        assert kwargs["persist_directory"] == "/tmp/custom_pd"

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
            "citations": [{"para": "1", "page": "1", "raw": "Test, Section 1"}],
            "sources": ["Test, Section 1"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "1", "page": "1", "raw": "Test, Section 1"}],
                "ungrounded": [],
            },
            "gate_outcome": CITATIONS_VERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            result = query("What is the law?")

        assert result["answer"] == "The answer is..."

    def test_no_results_message(self):
        """Empty retrieval gives a helpful message AND the same result shape as
        the normal path, so callers (e.g. the Phase 5 eval) need no key guards."""
        with patch("src.pipeline.retrieve", return_value=[]):
            result = query("Unknown question")

        assert "No relevant documents" in result["answer"]
        assert result["citations"] == []
        assert result["sources"] == []
        assert result["citation_check"] == {"grounded": [], "ungrounded": []}

    def test_verbose_prints_scores_and_flags_ungrounded(self, capsys):
        """--verbose prints per-chunk fused RRF scores before the answer; under a
        PARTIALLY_VERIFIED gate outcome the unverified-citation banner (which
        names each failed locator) prints after it, superseding the legacy
        ungrounded-citations warning."""
        mock_results = [
            {
                "document": Document(
                    page_content="Priority entry content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        "page_start": 412,
                    },
                ),
                "score": 0.03279,
                "metadata": {"section_number": "14.8.5"},
            }
        ]
        mock_generated = {
            "answer": "Answer [Handbook, para 99.9, p.5].",
            "citations": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            "sources": ["para 99.9, p.5"],
            "source_documents": [],
            "citation_check": {
                "grounded": [],
                "ungrounded": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            },
            "gate_outcome": PARTIALLY_VERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is a priority entry?", verbose=True)

        out = capsys.readouterr().out
        assert "RRF=0.03279" in out  # verbose RRF listing still prints
        assert "para 14.8.5" in out
        assert "1 of 1 citations could not be verified" in out
        assert "para 99.9, p.5" in out  # the failed locator is named

    def test_verbose_appendix_section_has_no_para_token(self, capsys):
        """The --verbose retrieved-chunks block must mirror the APPENDIX
        locator grammar (chunker._prefix / retriever._handbook_header): an
        appendix section_number prints verbatim, not as "para APPENDIX ..."."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Appendix content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "APPENDIX 14.1",
                        "page_start": 87,
                    },
                ),
                "score": 0.03279,
                "metadata": {"section_number": "APPENDIX 14.1"},
            }
        ]
        mock_generated = {
            "answer": "Answer [Handbook, APPENDIX 14.1, p.87].",
            "citations": [{"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}],
            "sources": ["APPENDIX 14.1, p.87"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}],
                "ungrounded": [],
            },
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is in the appendix?", verbose=True)

        out = capsys.readouterr().out
        assert "APPENDIX 14.1" in out
        assert "para APPENDIX" not in out

    def test_zero_citation_warning_on_uncited_non_refusal_answer(self, capsys):
        """A non-refusal answer with no extractable citations gets a prominent
        display warning — citation_check has nothing to validate here, so this
        is the only thing that can catch an uncited answer."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf"},
                ),
                "score": 0.01,
                "metadata": {},
            }
        ]
        mock_generated = {
            "answer": "The general rule is straightforward.",
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is the rule?")

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "no citations" in out

    def test_no_zero_citation_warning_for_canonical_refusal(self, capsys):
        """The canonical refusal has no citations by design — it must NOT
        trigger the zero-citation warning, which is only for answers that
        look substantive but forgot to cite anything."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Unrelated content.",
                    metadata={"source": "Conveyancing_Handbook.pdf"},
                ),
                "score": 0.01,
                "metadata": {},
            }
        ]
        mock_generated = {
            "answer": REFUSAL_PHRASE,
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("An out-of-corpus question")

        out = capsys.readouterr().out
        assert "WARNING" not in out

    def test_no_zero_citation_warning_for_normalized_refusal_shape(self, capsys):
        """The warning suppression must track is_refusal's NORMALIZATION, not a
        bare-string compare. The system prompt shows the refusal sentence
        quote-wrapped with the period outside the quotes, so the model emits
        `"<phrase>".`; that is still a refusal and must NOT trigger the
        zero-citation warning. Pins the pipeline↔is_refusal coupling so a
        regression to naive equality (== REFUSAL_PHRASE) is caught here."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Unrelated content.",
                    metadata={"source": "Conveyancing_Handbook.pdf"},
                ),
                "score": 0.01,
                "metadata": {},
            }
        ]
        mock_generated = {
            "answer": f'"{REFUSAL_PHRASE}".',  # quotes, period OUTSIDE — prompt-displayed shape
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("An out-of-corpus question")

        out = capsys.readouterr().out
        assert "WARNING" not in out

    def test_no_zero_citation_warning_when_citations_present(self, capsys):
        """An answer that carries citations never gets the zero-citation
        warning, regardless of whether those citations are grounded."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf"},
                ),
                "score": 0.01,
                "metadata": {},
            }
        ]
        mock_generated = {
            "answer": "The rule is X [Handbook, para 3.2, p.10].",
            "citations": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
            "sources": ["para 3.2, p.10"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
                "ungrounded": [],
            },
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is the rule?")

        out = capsys.readouterr().out
        assert "WARNING" not in out

    def test_ungrounded_citations_print_without_verbose(self, capsys):
        """Ungrounded-citation warnings are a correctness signal, not debug
        output — they must print even when --verbose is not passed."""
        # No gate_outcome in the mock → pins the legacy fallback display path.
        mock_results = [
            {
                "document": Document(
                    page_content="Priority entry content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        "page_start": 412,
                    },
                ),
                "score": 0.03279,
                "metadata": {"section_number": "14.8.5"},
            }
        ]
        mock_generated = {
            "answer": "Answer [Handbook, para 99.9, p.5].",
            "citations": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            "sources": ["para 99.9, p.5"],
            "source_documents": [],
            "citation_check": {
                "grounded": [],
                "ungrounded": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            },
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is a priority entry?")  # verbose defaults to False

        out = capsys.readouterr().out
        assert "Ungrounded citations" in out
        assert "RRF=" not in out  # RRF listing stays verbose-only

    # --- Grounding-gate display policy (Phase 8) --------------------------

    def test_unverified_outcome_withholds_answer_body(self, capsys):
        """CITATIONS_UNVERIFIED fails closed: the draft body is withheld, the
        block banner and the retrieved source headers (locator + pages, no
        chunk text) are shown, and the returned dict carries the block notice
        with answer_chars but not the draft. The retrieved chunk is an APPENDIX
        chunk, so its header must render via locator_label (verbatim, no
        'para' token)."""
        draft = "SENTINEL DRAFT BODY: the interest is registrable on completion."
        mock_results = [
            {
                "document": Document(
                    page_content="Appendix content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "APPENDIX 14.1",
                        "page_start": 87,
                    },
                ),
                "score": 0.02,
                "metadata": {"section_number": "APPENDIX 14.1"},
            }
        ]
        mock_generated = {
            "answer": draft,
            "citations": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            "sources": ["para 99.9, p.5"],
            "source_documents": [],
            "citation_check": {
                "grounded": [],
                "ungrounded": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            },
            "gate_outcome": CITATIONS_UNVERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            result = query("What is the appendix rule?")

        out = capsys.readouterr().out
        assert draft not in out  # body is withheld
        assert "BLOCKED — CITATIONS UNVERIFIED" in out
        assert "APPENDIX 14.1" in out  # source header line (locator)
        assert "p.87" in out  # source header line (pages)
        assert "para APPENDIX" not in out  # appendix renders via locator_label
        # Returned dict: block notice replaces the draft, but answer_chars proves
        # a draft existed and the gate outcome is preserved.
        assert result["answer"] != draft
        assert "BLOCKED — CITATIONS UNVERIFIED" in result["answer"]
        assert result["answer_chars"] == len(draft)
        assert result["gate_outcome"] == CITATIONS_UNVERIFIED

    def test_zero_citation_answer_blocks_through_the_production_path(self, capsys):
        """The P0 case routed through the code path production actually runs:
        a non-refusal draft with ZERO citations gets gate_outcome
        CITATIONS_UNVERIFIED from the real generate_with_sources, so it must
        hit the BLOCKED branch — not the legacy fallback whose '⚠ WARNING:
        this answer contains no citations' text production never emits."""
        draft = "SENTINEL DRAFT: an uncited assertion about registration."
        mock_results = [
            {
                "document": Document(
                    page_content="Chunk content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "3.2.1",
                        "page_start": 40,
                    },
                ),
                "score": 0.02,
                "metadata": {"section_number": "3.2.1"},
            }
        ]
        mock_generated = {
            "answer": draft,
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": CITATIONS_UNVERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            result = query("What does the handbook assert?")

        out = capsys.readouterr().out
        assert draft not in out
        assert "BLOCKED — CITATIONS UNVERIFIED" in out
        # No unverified-locator list (there were no citations at all) and no
        # legacy fallback warning — this is the gated branch, not v1 display.
        assert "Unverified citations in the withheld draft" not in out
        assert "this answer contains no citations" not in out
        assert result["answer_chars"] == len(draft)

    def test_show_unverified_reveals_draft_and_logs_override(self, capsys):
        """--show-unverified reveals the withheld draft under the UNVERIFIED
        DRAFT banner, returns the real answer, and logs the override action."""
        draft = "SENTINEL DRAFT BODY: the interest is registrable on completion."
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={
                        "source": "Conveyancing_Handbook.pdf",
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        "page_start": 412,
                    },
                ),
                "score": 0.02,
                "metadata": {"section_number": "14.8.5"},
            }
        ]
        mock_generated = {
            "answer": draft,
            "citations": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            "sources": ["para 99.9, p.5"],
            "source_documents": [],
            "citation_check": {
                "grounded": [],
                "ungrounded": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            },
            "gate_outcome": CITATIONS_UNVERIFIED,
        }

        spy = MagicMock()
        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", spy):
            result = query("What is the rule?", show_unverified=True)

        out = capsys.readouterr().out
        assert "UNVERIFIED DRAFT" in out
        assert draft in out  # the draft IS printed under the banner
        assert result["answer"] == draft  # real answer returned
        assert spy.call_count == 1
        assert spy.call_args.args[0]["action"] == "shown_unverified_override"

    def test_partially_verified_names_failed_citations(self, capsys):
        """PARTIALLY_VERIFIED shows the answer plus a banner that names each
        unverified locator with 'N of M' wording."""
        draft = "The rule is X [Handbook, para 3.2, p.10] and Y [para 99.9, p.5]."
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf", "section_number": "3.2"},
                ),
                "score": 0.02,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": draft,
            "citations": [
                {"para": "3.2", "page": "10", "raw": "para 3.2, p.10"},
                {"para": "99.9", "page": "5", "raw": "para 99.9, p.5"},
            ],
            "sources": ["para 3.2, p.10", "para 99.9, p.5"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
                "ungrounded": [{"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}],
            },
            "gate_outcome": PARTIALLY_VERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("What is the rule?")

        out = capsys.readouterr().out
        assert draft in out  # answer shown
        assert "1 of 2 citations could not be verified" in out
        assert "para 99.9, p.5" in out  # the failed locator is named

    def test_citations_verified_shows_note_and_logs_shown(self, capsys):
        """CITATIONS_VERIFIED shows the answer, the citations list, and the
        all-verified closing note; the audit action is 'shown'."""
        draft = "The rule is X [Handbook, para 3.2, p.10]."
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf", "section_number": "3.2"},
                ),
                "score": 0.02,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": draft,
            "citations": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
            "sources": ["para 3.2, p.10"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
                "ungrounded": [],
            },
            "gate_outcome": CITATIONS_VERIFIED,
        }

        spy = MagicMock()
        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", spy):
            query("What is the rule?")

        out = capsys.readouterr().out
        assert draft in out
        # Phase 14 WS1.3 display-honesty rewording: the note now states the gate
        # only resolves each locator to a retrieved passage, and explicitly
        # disclaims verifying that the passage supports the claim.
        assert "resolve to a retrieved passage" in out
        assert "does not verify the passage supports the claim" in out
        assert spy.call_args.args[0]["action"] == "shown"

    def test_refusal_outcome_shows_no_warnings(self, capsys):
        """A REFUSAL outcome prints only the refusal sentence — no zero-citation
        warning, no gate banner — and logs action 'refusal_shown'."""
        mock_results = [
            {
                "document": Document(
                    page_content="Unrelated content.",
                    metadata={"source": "Conveyancing_Handbook.pdf"},
                ),
                "score": 0.01,
                "metadata": {},
            }
        ]
        mock_generated = {
            "answer": REFUSAL_PHRASE,
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": REFUSAL,
        }

        spy = MagicMock()
        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", spy):
            query("An out-of-corpus question")

        out = capsys.readouterr().out
        assert REFUSAL_PHRASE in out
        assert "WARNING" not in out
        assert "BLOCKED" not in out
        assert "✓" not in out
        assert "could not be verified" not in out
        assert spy.call_args.args[0]["action"] == "refusal_shown"

    def test_audit_logs_exactly_once_including_no_results(self):
        """Auditing is always-on: exactly one log_event per query, on both the
        normal path and the empty-retrieval early-return path (where the
        recorded action is 'no_results')."""
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf", "section_number": "3.2"},
                ),
                "score": 0.02,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": "The rule is X [Handbook, para 3.2, p.10].",
            "citations": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
            "sources": ["para 3.2, p.10"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
                "ungrounded": [],
            },
            "gate_outcome": CITATIONS_VERIFIED,
        }

        normal_spy = MagicMock()
        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", normal_spy):
            query("What is the rule?")
        assert normal_spy.call_count == 1

        no_results_spy = MagicMock()
        with patch("src.pipeline.retrieve", return_value=[]), \
             patch("src.pipeline.log_event", no_results_spy):
            query("Unknown question")
        assert no_results_spy.call_count == 1
        assert no_results_spy.call_args.args[0]["action"] == "no_results"

    def test_audit_write_failure_is_non_fatal(self, capsys):
        """A log_event failure must not crash the query: it returns normally and
        prints a visible one-line warning instead of raising."""
        mock_results = [
            {
                "document": Document(
                    page_content="Some content.",
                    metadata={"source": "Conveyancing_Handbook.pdf", "section_number": "3.2"},
                ),
                "score": 0.02,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": "The rule is X [Handbook, para 3.2, p.10].",
            "citations": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
            "sources": ["para 3.2, p.10"],
            "source_documents": [],
            "citation_check": {
                "grounded": [{"para": "3.2", "page": "10", "raw": "para 3.2, p.10"}],
                "ungrounded": [],
            },
            "gate_outcome": CITATIONS_VERIFIED,
        }

        with patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", side_effect=OSError("disk full")):
            result = query("What is the rule?")

        out = capsys.readouterr().out
        assert result["answer"] == "The rule is X [Handbook, para 3.2, p.10]."
        assert "audit log write failed" in out

    # --- Query expansion wiring (Phase 13, D43) ----------------------------

    def test_no_rewrite_true_disables_expansion(self):
        """no_rewrite=True must reach expand_query as enabled=False."""
        stub = Expansion("Some question", (), REWRITE_MODEL, STATUS_DISABLED)
        with patch("src.pipeline.expand_query", return_value=stub) as m_expand, \
             patch("src.pipeline.retrieve", return_value=[]):
            query("Some question", no_rewrite=True)

        m_expand.assert_called_once_with("Some question", enabled=False)

    def test_no_rewrite_default_false_enables_expansion(self):
        """The default (no_rewrite unset) must reach expand_query as
        enabled=True — expansion is on by default."""
        stub = Expansion("Some question", (), REWRITE_MODEL, STATUS_DISABLED)
        with patch("src.pipeline.expand_query", return_value=stub) as m_expand, \
             patch("src.pipeline.retrieve", return_value=[]):
            query("Some question")

        m_expand.assert_called_once_with("Some question", enabled=True)

    def test_expansion_rewrites_forwarded_to_retrieve(self):
        """Effective rewrites from expand_query are forwarded to retrieve()
        as the rewrites= keyword, as a plain list."""
        stub = Expansion("q", ("rewrite one", "rewrite two"), REWRITE_MODEL, STATUS_LIVE)
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=[]) as m_retrieve:
            query("q")

        assert m_retrieve.call_args.kwargs["rewrites"] == ["rewrite one", "rewrite two"]

    def test_no_rewrites_forwards_none_to_retrieve(self):
        """An empty rewrites tuple forwards rewrites=None (not an empty
        list) — the retriever's own byte-identical-when-empty contract
        (D43) expects None, not []."""
        stub = Expansion("q", (), REWRITE_MODEL, STATUS_DISABLED)
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=[]) as m_retrieve:
            query("q")

        assert m_retrieve.call_args.kwargs["rewrites"] is None

    def test_verbose_prints_expansion_status_and_rewrites(self, capsys):
        """--verbose prints the expansion status/count line, then each
        rewrite indented, before the RRF chunk table."""
        stub = Expansion("q", ("rewrite one", "rewrite two"), REWRITE_MODEL, STATUS_LIVE)
        mock_results = [
            {
                "document": Document(
                    page_content="x",
                    metadata={"section_number": "3.2", "page_start": 10},
                ),
                "score": 0.01,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": "Answer text.",
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": CITATIONS_VERIFIED,
        }
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("q", verbose=True)

        out = capsys.readouterr().out
        assert f"Query expansion [{STATUS_LIVE}]: 2 rewrite(s)" in out
        assert "rewrite one" in out
        assert "rewrite two" in out
        # Expansion line precedes the RRF chunk table, per spec.
        assert out.index("Query expansion") < out.index("Retrieved chunks")

    def test_no_expansion_line_without_verbose(self, capsys):
        """The expansion line is verbose-only, like the RRF chunk table."""
        stub = Expansion("q", ("rewrite one",), REWRITE_MODEL, STATUS_LIVE)
        mock_results = [
            {
                "document": Document(
                    page_content="x",
                    metadata={"section_number": "3.2", "page_start": 10},
                ),
                "score": 0.01,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": "Answer text.",
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": CITATIONS_VERIFIED,
        }
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated):
            query("q")  # verbose defaults to False

        out = capsys.readouterr().out
        assert "Query expansion" not in out

    def _mock_results_and_generated(self):
        mock_results = [
            {
                "document": Document(
                    page_content="x",
                    metadata={"section_number": "3.2", "page_start": 10},
                ),
                "score": 0.01,
                "metadata": {"section_number": "3.2"},
            }
        ]
        mock_generated = {
            "answer": "Answer text.",
            "citations": [],
            "sources": [],
            "source_documents": [],
            "citation_check": {"grounded": [], "ungrounded": []},
            "gate_outcome": CITATIONS_VERIFIED,
        }
        return mock_results, mock_generated

    def test_audit_event_carries_rewrite_fields_without_raw_text_by_default(
        self, monkeypatch
    ):
        """The audit event carries rewrite_status/rewrite_count/
        rewrite_sha256s whenever an expansion ran, but NOT rewrite_texts
        unless AUDIT_LOG_RAW_QUERIES=1 is set."""
        monkeypatch.delenv("AUDIT_LOG_RAW_QUERIES", raising=False)
        stub = Expansion("q", ("rewrite one",), REWRITE_MODEL, STATUS_LIVE)
        mock_results, mock_generated = self._mock_results_and_generated()

        spy = MagicMock()
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", spy):
            query("q")

        event = spy.call_args.args[0]
        assert event["rewrite_status"] == STATUS_LIVE
        assert event["rewrite_count"] == 1
        assert event["rewrite_sha256s"] == [
            hashlib.sha256("rewrite one".encode("utf-8")).hexdigest()
        ]
        assert "rewrite_texts" not in event

    def test_audit_event_carries_raw_rewrite_texts_when_env_opted_in(
        self, monkeypatch
    ):
        monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        stub = Expansion("q", ("rewrite one",), REWRITE_MODEL, STATUS_LIVE)
        mock_results, mock_generated = self._mock_results_and_generated()

        spy = MagicMock()
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=mock_results), \
             patch("src.pipeline.generate_with_sources", return_value=mock_generated), \
             patch("src.pipeline.log_event", spy):
            query("q")

        event = spy.call_args.args[0]
        assert event["rewrite_texts"] == ["rewrite one"]

    def test_no_results_path_also_carries_expansion(self, monkeypatch):
        """Expansion runs before retrieval, so the no-results early-return
        path logs the rewrite fields too, not just the main path."""
        monkeypatch.delenv("AUDIT_LOG_RAW_QUERIES", raising=False)
        stub = Expansion("q", ("rewrite one",), REWRITE_MODEL, STATUS_LIVE)

        spy = MagicMock()
        with patch("src.pipeline.expand_query", return_value=stub), \
             patch("src.pipeline.retrieve", return_value=[]), \
             patch("src.pipeline.log_event", spy):
            query("q")

        event = spy.call_args.args[0]
        assert event["action"] == "no_results"
        assert event["rewrite_status"] == STATUS_LIVE
        assert event["rewrite_count"] == 1


class TestEvalCli:
    """The Phase 10 `eval` subcommand parses its flags and dispatches to
    run_eval_matrix with the right set_specs, modes, and knobs. IO-free: the
    matrix runner is replaced with a spy, so no store/API is touched. main()
    imports run_eval_matrix from src.evaluator at call time, so patching
    src.evaluator.run_eval_matrix is what the local import resolves to."""

    def _dispatch(self, monkeypatch, argv):
        import src.evaluator
        import src.pipeline

        captured = {}

        def spy(set_specs, **kwargs):
            captured["set_specs"] = set_specs
            captured["kwargs"] = kwargs

        monkeypatch.setattr("src.evaluator.run_eval_matrix", spy)
        monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
        src.pipeline.main()
        return captured

    def test_default_dispatch_shape(self, monkeypatch):
        c = self._dispatch(monkeypatch, [])
        assert c["set_specs"] == [("tuning", "eval/golden_set.jsonl")]
        assert c["kwargs"]["modes"] == ["hybrid", "vector", "bm25", "hybrid+rewrite"]
        assert c["kwargs"]["skip_refusals"] is False
        assert c["kwargs"]["skip_completeness"] is False
        assert c["kwargs"]["judge"] is False
        assert c["kwargs"]["results_path"] is None
        assert c["kwargs"]["judge_dump_path"] is None

    def test_full_flag_threading(self, monkeypatch):
        c = self._dispatch(
            monkeypatch,
            [
                "--heldout", "eval/heldout_set.jsonl",
                "--mode", "vector",
                "--judge", "--judge-sample", "15",
                "-o", "out.md",
                "--top-k", "8",
            ],
        )
        assert c["set_specs"] == [
            ("tuning", "eval/golden_set.jsonl"),
            ("held-out", "eval/heldout_set.jsonl"),
        ]
        assert c["kwargs"]["modes"] == ["vector"]
        assert c["kwargs"]["judge"] is True
        assert c["kwargs"]["judge_sample"] == 15
        assert c["kwargs"]["results_path"] == "out.md"
        assert c["kwargs"]["top_k"] == 8
        # Judge on -> a gitignored dump path is passed for local review.
        assert c["kwargs"]["judge_dump_path"] == "eval/judge_review.jsonl"

    def test_offline_ci_shape_has_no_generation_flags(self, monkeypatch):
        """Phase 11 CI shape: --skip-refusals --skip-completeness (no --judge)
        threads through as both skips True + judge False — the combination the
        matrix runner turns into zero generation calls (its own blocker test
        proves the zero-call guarantee)."""
        c = self._dispatch(monkeypatch, ["--skip-refusals", "--skip-completeness"])
        assert c["kwargs"]["skip_refusals"] is True
        assert c["kwargs"]["skip_completeness"] is True
        assert c["kwargs"]["judge"] is False
        assert c["kwargs"]["judge_dump_path"] is None

    def test_non_default_golden_labeled_golden(self, monkeypatch):
        c = self._dispatch(monkeypatch, ["--golden", "eval/custom.jsonl"])
        assert c["set_specs"] == [("golden", "eval/custom.jsonl")]

    def test_mode_all_expands_to_four(self, monkeypatch):
        c = self._dispatch(monkeypatch, ["--mode", "all"])
        assert c["kwargs"]["modes"] == ["hybrid", "vector", "bm25", "hybrid+rewrite"]

    def test_mode_hybrid_rewrite_accepted(self, monkeypatch):
        """The hybrid+rewrite mode is a valid single --mode choice (D46)."""
        c = self._dispatch(monkeypatch, ["--mode", "hybrid+rewrite"])
        assert c["kwargs"]["modes"] == ["hybrid+rewrite"]

    def test_realistic_appends_realistic_set_spec(self, monkeypatch):
        """--realistic appends a ('realistic', path) set after the held-out
        append, so a canonical run can carry both slices (D46)."""
        c = self._dispatch(
            monkeypatch,
            [
                "--heldout", "eval/heldout_set.jsonl",
                "--realistic", "eval/realistic_set.jsonl",
            ],
        )
        assert c["set_specs"] == [
            ("tuning", "eval/golden_set.jsonl"),
            ("held-out", "eval/heldout_set.jsonl"),
            ("realistic", "eval/realistic_set.jsonl"),
        ]


class TestQueryCli:
    """The `query` subcommand's --no-rewrite flag parses and dispatches by
    keyword to query(), mirroring TestEvalCli's dispatch-spy style. query()
    itself is replaced with a spy, so no store/API/expansion is touched."""

    def _dispatch(self, monkeypatch, argv):
        import src.pipeline

        captured = {}

        def spy(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        monkeypatch.setattr("src.pipeline.query", spy)
        monkeypatch.setattr("sys.argv", ["prog", "query", *argv])
        src.pipeline.main()
        return captured

    def test_no_rewrite_flag_forwarded_by_keyword(self, monkeypatch):
        c = self._dispatch(monkeypatch, ["What is a priority entry?", "--no-rewrite"])
        assert c["kwargs"]["no_rewrite"] is True

    def test_no_rewrite_defaults_false(self, monkeypatch):
        c = self._dispatch(monkeypatch, ["What is a priority entry?"])
        assert c["kwargs"]["no_rewrite"] is False
