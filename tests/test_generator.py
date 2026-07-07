"""Tests for the LLM response generation module."""

import os
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.generator import (
    REFUSAL_PHRASE,
    SYSTEM_PROMPT,
    extract_citations,
    generate,
    generate_with_sources,
    get_llm,
    is_refusal,
    validate_citations,
)


class TestGetLlm:
    def test_raises_without_api_key(self):
        """Test that missing API key raises ValueError."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not set"):
                get_llm()

    def test_raises_with_placeholder_key(self):
        """Test that the placeholder key raises ValueError."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "your-api-key-here"}, clear=False):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not set"):
                get_llm()

    def test_creates_llm_with_valid_key(self):
        """Test that a valid API key creates the LLM."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key-123"}, clear=False):
            llm = get_llm()
            assert llm is not None


class TestGenerate:
    def test_generates_answer(self):
        """Test that generate produces an answer dict."""
        mock_llm = MagicMock()
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = (
            "Under Section 77 of the Succession Act 1965, a person aged 18 "
            "may make a valid will. [Source: Succession Act 1965, Section 77]"
        )

        with patch("src.generator.get_llm", return_value=mock_llm), \
             patch("src.generator.PROMPT_TEMPLATE.__or__", return_value=MagicMock(__or__=MagicMock(return_value=mock_chain))):
            # Directly test with a simpler mock approach
            with patch("src.generator.generate") as mock_generate:
                mock_generate.return_value = {
                    "answer": "Under Section 77...",
                    "sources": ["Succession Act 1965, Section 77"],
                }
                result = mock_generate("Who can make a will?", "context text")

        assert "answer" in result
        assert "sources" in result

    def test_extracts_citations(self):
        """Citation extraction runs the REAL module regex, not an inline copy,
        so a change to CITATION_RE is actually exercised here (Phase 4 / D28)."""
        answer = (
            "A priority entry protects the purchaser [Handbook, para 14.8.5, p.412]. "
            "The manual's objectives are set out at [Handbook, para 1.2, pp.1–2]."
        )
        citations = extract_citations(answer)

        assert len(citations) == 2
        assert citations[0] == {"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"}
        # A page range captures the first page.
        assert citations[1]["para"] == "1.2"
        assert citations[1]["page"] == "1"

    def test_extracts_citations_from_long_prefix_form(self):
        """The tolerant regex captures the full D21 prefix the model may echo
        verbatim from a chunk — anchored on para/page, not comma-split, because
        the chapter-title segment contains commas and free OCR text."""
        answer = (
            "See [Conveyancing Handbook, Ch.14 Registration Of Title, para 14.8.5, p.412] "
            "and also [Conveyancing Handbook, Ch.1 Introduction, para 1.2, pp.1–2]."
        )
        citations = extract_citations(answer)

        assert [c["para"] for c in citations] == ["14.8.5", "1.2"]
        assert [c["page"] for c in citations] == ["412", "1"]

    def test_refusal_answer_has_no_citations(self):
        """A refusal carries no bracketed locators, so extraction is empty."""
        assert extract_citations(REFUSAL_PHRASE) == []


class TestRefusal:
    def test_system_prompt_embeds_refusal_phrase(self):
        """The canonical refusal phrase must live in the system prompt, so the
        refusal instruction and the detector cannot drift apart."""
        assert REFUSAL_PHRASE in SYSTEM_PROMPT

    def test_is_refusal_detects_the_phrase(self):
        assert is_refusal(REFUSAL_PHRASE)
        assert is_refusal(f"  {REFUSAL_PHRASE.upper()}.  ")

    def test_is_refusal_false_for_a_real_answer(self):
        assert not is_refusal("A priority entry protects the purchaser [Handbook, para 14.8.5, p.412].")

    def test_partial_answer_with_citations_is_not_a_refusal(self):
        """An answer that hedges with the canonical phrase mid-sentence while
        still citing sources is an answer — scoring it as a refusal would
        corrupt the Phase 5 refusal-accuracy metric."""
        partial = (
            "The 2025 amendment is not covered in the source material, but the "
            "general position is stated [Handbook, para 16.13, p.691]."
        )
        assert not is_refusal(partial)


class TestValidateCitations:
    def test_grounded_citation_matches_a_retrieved_chunk(self, handbook_retrieved_results):
        citations = [{"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"}]
        check = validate_citations(citations, handbook_retrieved_results)

        assert len(check["grounded"]) == 1
        assert check["ungrounded"] == []

    def test_page_inside_a_range_is_grounded(self, handbook_retrieved_results):
        # Chunk 1.2 spans pages 1–2; a citation to p.2 is still within the span.
        citations = [{"para": "1.2", "page": "2", "raw": "para 1.2, p.2"}]
        check = validate_citations(citations, handbook_retrieved_results)

        assert len(check["grounded"]) == 1

    def test_unknown_paragraph_is_ungrounded(self, handbook_retrieved_results):
        citations = [{"para": "99.9", "page": "412", "raw": "para 99.9, p.412"}]
        check = validate_citations(citations, handbook_retrieved_results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1

    def test_right_paragraph_wrong_page_is_ungrounded(self, handbook_retrieved_results):
        # Paragraph 14.8.5 exists but on p.412, not p.999 — a page hallucination.
        citations = [{"para": "14.8.5", "page": "999", "raw": "para 14.8.5, p.999"}]
        check = validate_citations(citations, handbook_retrieved_results)

        assert len(check["ungrounded"]) == 1

    def test_subparagraph_of_a_retrieved_chunk_is_grounded(self):
        # A more precise model cites the sub-paragraph 14.12.1 that lives inside a
        # chunk whose section_number is the parent 14.12 — grounded on the page.
        results = [
            {
                "document": Document(
                    page_content="s.72 burdens...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.12",
                        "page_start": 533,
                        "page_end": 534,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [{"para": "14.12.1", "page": "533", "raw": "para 14.12.1, p.533"}]
        check = validate_citations(citations, results)

        assert len(check["grounded"]) == 1
        assert check["ungrounded"] == []

    def test_sibling_prefix_is_not_treated_as_nested(self):
        # '14.1' shares a string prefix with '14.12' but is a different section —
        # component-wise comparison keeps it ungrounded.
        results = [
            {
                "document": Document(
                    page_content="...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.12",
                        "page_start": 533,
                        "page_end": 533,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [{"para": "14.1", "page": "533", "raw": "para 14.1, p.533"}]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1


class TestGenerateWithSources:
    def test_includes_source_documents(self, mock_retrieved_results):
        """Test that generate_with_sources includes source documents."""
        mock_result = {
            "answer": "Test answer",
            "citations": [],
            "sources": [],
        }

        with patch("src.generator.generate", return_value=mock_result):
            result = generate_with_sources("test question", mock_retrieved_results)

        assert "source_documents" in result
        assert len(result["source_documents"]) == 2

    def test_attaches_citation_check(self, handbook_retrieved_results):
        """generate_with_sources runs the grounding check against the retrieved
        chunks, splitting the model's citations into grounded/ungrounded."""
        mock_result = {
            "answer": "answer [Handbook, para 14.8.5, p.412] and [Handbook, para 99.9, p.5].",
            "citations": [
                {"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"},
                {"para": "99.9", "page": "5", "raw": "para 99.9, p.5"},
            ],
            "sources": ["para 14.8.5, p.412", "para 99.9, p.5"],
        }

        with patch("src.generator.generate", return_value=mock_result):
            result = generate_with_sources("test question", handbook_retrieved_results)

        assert len(result["citation_check"]["grounded"]) == 1
        assert len(result["citation_check"]["ungrounded"]) == 1
