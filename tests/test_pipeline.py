"""Tests for the pipeline orchestration module."""

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
                   return_value={"added": 1, "updated": 0, "deleted": 0}):
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
                   return_value={"added": 1, "updated": 0, "deleted": 0}):
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
                   return_value={"added": 1, "updated": 0, "deleted": 0}):
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
                   return_value={"added": 1, "updated": 0, "deleted": 0}) as m_sync:
            count = index_documents("/test/data/", "legislation")

        assert count == 2
        sources_synced = [call.args[0] for call in m_sync.call_args_list]
        assert sources_synced == ["a.html", "b.html"]
        assert m_sync.call_args_list[0].args[1] == chunks_a
        assert m_sync.call_args_list[1].args[1] == chunks_b
        with pytest.raises(ValueError, match="PDF"):
            index_documents("https://example.com/act", "handbook")


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
        The load-once builders (D37) are stubbed for the same reason: query()
        builds the store + BM25 sidecar up front and injects them into
        retrieve(), and since every test here patches src.pipeline.retrieve,
        the stub objects are never dereferenced."""
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
        monkeypatch.setattr("src.audit._git_sha", lambda: "t3st5ha")
        monkeypatch.setattr("src.pipeline.assert_embedding_model", lambda *a, **k: None)
        monkeypatch.setattr("src.pipeline.get_vector_store", lambda **k: object())
        monkeypatch.setattr("src.pipeline.load_bm25_index", lambda *a, **k: object())

    def test_query_builds_once_and_injects_into_retrieve(self, monkeypatch):
        """query() builds the store + BM25 sidecar once up front and injects
        them into retrieve() along with persist_directory (load-once, D37)."""
        store_sentinel, bm25_sentinel = object(), object()
        calls = {"assert": 0, "store": 0, "bm25": 0}

        def fake_assert(persist_directory):
            calls["assert"] += 1
            assert persist_directory == "/tmp/custom_pd"

        def fake_store(**kwargs):
            calls["store"] += 1
            assert kwargs["persist_directory"] == "/tmp/custom_pd"
            return store_sentinel

        def fake_bm25(persist_directory):
            calls["bm25"] += 1
            assert persist_directory == "/tmp/custom_pd"
            return bm25_sentinel

        monkeypatch.setattr("src.pipeline.assert_embedding_model", fake_assert)
        monkeypatch.setattr("src.pipeline.get_vector_store", fake_store)
        monkeypatch.setattr("src.pipeline.load_bm25_index", fake_bm25)

        with patch("src.pipeline.retrieve", return_value=[]) as m_retrieve:
            query("anything", persist_directory="/tmp/custom_pd")

        assert calls == {"assert": 1, "store": 1, "bm25": 1}
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
        assert "verified against the retrieved sources" in out
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
