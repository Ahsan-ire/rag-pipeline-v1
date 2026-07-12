"""Tests for the pipeline orchestration module."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.generator import REFUSAL_PHRASE
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
             patch("src.pipeline.add_documents", return_value=1):
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
             patch("src.pipeline.add_documents", return_value=1):
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
             patch("src.pipeline.add_documents", return_value=1):
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
             patch("src.pipeline.add_documents", return_value=1):
            count = index_documents("/data/Conveyancing_Handbook.pdf", "handbook")

        assert count == 1
        m_load.assert_called_once()
        m_chunk.assert_called_once()
        m_load_pdf.assert_not_called()  # the legislation loader is bypassed

    def test_handbook_empty_extraction_returns_zero(self):
        """An empty extraction (missing/blank PDF) returns 0 without chunking."""
        with patch("src.pipeline.load_handbook_pdf", return_value=("", [], {})), \
             patch("src.pipeline.chunk_handbook") as m_chunk, \
             patch("src.pipeline.add_documents", return_value=0):
            count = index_documents("/data/missing.pdf", "handbook")

        assert count == 0
        m_chunk.assert_not_called()

    def test_reset_clears_store_before_indexing(self):
        """--reset clears the vector store first (positional-ID dedup guard)."""
        handbook_triple = ("CHAPTER 1\n1.1 x", [], {"source": "h.pdf"})
        with patch("src.pipeline.clear_store") as m_clear, \
             patch("src.pipeline.load_handbook_pdf", return_value=handbook_triple), \
             patch("src.pipeline.chunk_handbook", return_value=[Document(page_content="x", metadata={})]), \
             patch("src.pipeline.add_documents", return_value=1):
            index_documents("/data/handbook.pdf", "handbook", reset=True)

        m_clear.assert_called_once()

    def test_reset_does_not_clear_when_chunking_raises(self):
        """A mis-routed handbook PDF raises BEFORE clear_store, sparing the index."""
        triple = ("text with no chapter markers", [], {"source": "x.pdf"})
        with patch("src.pipeline.clear_store") as m_clear, \
             patch("src.pipeline.load_handbook_pdf", return_value=triple), \
             patch("src.pipeline.chunk_handbook", side_effect=ValueError("no CHAPTER markers")), \
             patch("src.pipeline.add_documents") as m_add:
            with pytest.raises(ValueError):
                index_documents("/data/notahandbook.pdf", "handbook", reset=True)

        m_clear.assert_not_called()   # store survives
        m_add.assert_not_called()

    def test_handbook_type_rejects_non_pdf_source(self):
        """--type handbook requires a single PDF; a dir/URL would mis-tag chunks."""
        with pytest.raises(ValueError, match="PDF"):
            index_documents("/data/legislation/", "handbook")
        with pytest.raises(ValueError, match="PDF"):
            index_documents("https://example.com/act", "handbook")


class TestQuery:
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
        """--verbose prints per-chunk fused RRF scores before the answer and
        flags citations that don't match any retrieved chunk after it."""
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
            query("What is a priority entry?", verbose=True)

        out = capsys.readouterr().out
        assert "RRF=0.03279" in out
        assert "para 14.8.5" in out
        assert "Ungrounded citations" in out

    def test_verbose_appendix_section_has_no_para_token(self, capsys):
        """The --verbose retrieved-chunks block must mirror the APPENDIX
        locator grammar (chunker._prefix / retriever._handbook_header): an
        appendix section_number prints verbatim, not as "para APPENDIX ..."."""
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
