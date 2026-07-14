"""Tests for the LLM response generation module."""

import os
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.generator import (
    CAVEAT_PREFIX,
    GENERATION_MODEL,
    PROMPT_TEMPLATE,
    REFUSAL_PHRASE,
    SYSTEM_PROMPT,
    _sections_related,
    extract_citations,
    generate,
    generate_with_sources,
    get_llm,
    is_refusal,
    validate_citations,
)
from src.grounding import CITATIONS_UNVERIFIED, PARTIALLY_VERIFIED


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

    def test_uses_the_generation_model_constant(self):
        """get_llm() must build its ChatAnthropic from the hoisted
        GENERATION_MODEL constant, not a separate hardcoded string, so the
        eval-report provenance block (which imports GENERATION_MODEL) can
        never drift out of sync with what actually generated the answers."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key-123"}, clear=False):
            llm = get_llm()
            assert llm.model == GENERATION_MODEL


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

    def test_extracts_compact_appendix_citation(self):
        """The verbatim appendix grammar (no 'para' token) must extract too
        (Phase 7 / D34) — chunk metadata for appendices is 'APPENDIX 14.1',
        never a 'para' locator."""
        answer = "See the form at [Handbook, APPENDIX 14.1, p.87]."
        citations = extract_citations(answer)

        assert citations == [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]

    def test_extracts_appendix_citation_from_long_prefix_form(self):
        """The D21 long prefix form applies to appendix locators too."""
        answer = (
            "See [Conveyancing Handbook, Ch.14 Registration, APPENDIX 14.1, p.87]."
        )
        citations = extract_citations(answer)

        assert citations == [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]

    def test_extracts_lowercase_appendix_and_canonicalizes(self):
        """The model may echo 'Appendix' in any case; extraction canonicalizes
        the label to uppercase regardless (case-tolerance lives only at this
        extraction boundary — see CITATION_RE)."""
        answer = "See [Handbook, Appendix 14.1, p.87]."
        citations = extract_citations(answer)

        assert citations == [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]

    def test_extracts_both_a_para_and_an_appendix_citation(self):
        """An answer mixing both locator grammars extracts one citation per
        bracket, each keeping its own grammar's raw string."""
        answer = (
            "The general rule is set out [Handbook, para 14.8.5, p.412], and "
            "the prescribed form is at [Handbook, APPENDIX 14.1, p.87]."
        )
        citations = extract_citations(answer)

        assert citations == [
            {"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"},
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"},
        ]

    def test_appendix_word_in_title_text_cannot_hijack_a_para_locator(self):
        """Gate-review regression (D34 addendum): the OCR'd chapter-title run
        is free text and may itself contain a locator-shaped token. Only the
        locator directly before the page segment is the real one — a title
        cross-reference like 'see Appendix 3' must not steal the match."""
        answer = (
            "See [Conveyancing Handbook, Ch.6 Contracts see Appendix 3, "
            "para 6.3.2, p.220]."
        )
        citations = extract_citations(answer)

        assert citations == [
            {"para": "6.3.2", "page": "220", "raw": "para 6.3.2, p.220"}
        ]

    def test_para_token_in_title_text_cannot_hijack_an_appendix_locator(self):
        """Reverse direction of the hijack guard: a 'para N' fragment in the
        free-text run must not steal the match from the real appendix locator
        adjacent to the page segment."""
        answer = (
            "See [Conveyancing Handbook, Ch.5 See para 3.2 Notes, "
            "APPENDIX 14.1, p.87]."
        )
        citations = extract_citations(answer)

        assert citations == [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]


class TestRefusal:
    def test_system_prompt_embeds_refusal_phrase(self):
        """The canonical refusal phrase must live in the system prompt, so the
        refusal instruction and the detector cannot drift apart."""
        assert REFUSAL_PHRASE in SYSTEM_PROMPT

    def test_system_prompt_embeds_caveat_prefix(self):
        """D44: the caveat-form opener must live in the system prompt too, so
        the instruction and whatever scores/detects it cannot drift apart."""
        assert CAVEAT_PREFIX in SYSTEM_PROMPT

    def test_refusal_phrase_byte_value_is_unchanged(self):
        """Plan-gate finding M14: pin the literal bytes, not constant-vs-constant
        — a change to REFUSAL_PHRASE itself would silently pass a
        self-referential assertion but must fail this one."""
        assert REFUSAL_PHRASE.encode() == b"not covered in the source material"

    def test_is_refusal_detects_the_exact_phrase(self):
        assert is_refusal(REFUSAL_PHRASE)

    def test_is_refusal_true_with_trailing_period(self):
        assert is_refusal(f"{REFUSAL_PHRASE}.")

    def test_is_refusal_true_with_multiple_trailing_periods(self):
        assert is_refusal(f"{REFUSAL_PHRASE}...")

    def test_is_refusal_true_wrapped_in_straight_quotes(self):
        assert is_refusal(f'"{REFUSAL_PHRASE}"')
        assert is_refusal(f"'{REFUSAL_PHRASE}'")

    def test_is_refusal_true_wrapped_in_curly_quotes(self):
        """The system prompt shows the refusal phrase inside quotes, so models
        sometimes echo the quote marks — including curly/smart quotes."""
        assert is_refusal(f"“{REFUSAL_PHRASE}”")
        assert is_refusal(f"‘{REFUSAL_PHRASE}’")

    def test_is_refusal_true_wrapped_in_straight_quotes_period_outside(self):
        """This is the exact shape the SYSTEM_PROMPT itself displays: reply
        with exactly this sentence and nothing else: "phrase". — straight
        quotes with the period OUTSIDE the closing quote. A model echoing the
        prompt verbatim produces this, so it must be detected as a refusal."""
        assert is_refusal(f'"{REFUSAL_PHRASE}".')

    def test_is_refusal_true_wrapped_in_curly_quotes_period_outside(self):
        """Curly-quote counterpart of the prompt-displayed shape above."""
        assert is_refusal(f"“{REFUSAL_PHRASE}”.")

    def test_is_refusal_true_prompt_displayed_shape_with_trailing_whitespace(self):
        """Prompt-displayed shape (period outside straight quotes) plus
        trailing whitespace, as a model reply might include."""
        assert is_refusal(f'  "{REFUSAL_PHRASE}".  \n')

    def test_is_refusal_true_with_different_casing(self):
        assert is_refusal(REFUSAL_PHRASE.upper())

    def test_is_refusal_true_with_surrounding_whitespace(self):
        assert is_refusal(f"  {REFUSAL_PHRASE}  \n")

    def test_is_refusal_false_for_a_real_answer(self):
        assert not is_refusal("A priority entry protects the purchaser [Handbook, para 14.8.5, p.412].")

    def test_hedged_sentence_with_phrase_is_not_a_refusal(self):
        """New contract: the answer must equal the canonical refusal sentence
        exactly (after normalization) — nothing more. A substring match is
        unsafe because a hedged partial answer can contain the phrase while
        still asserting a (possibly wrong) answer; scoring that as a refusal
        would corrupt the Phase 5 refusal-accuracy metric. This replaces the
        old citation-based guard (``and not extract_citations(answer)``),
        which is now redundant since an exact match can never contain a
        bracket citation."""
        hedged = (
            "This is not covered in the source material, but the likely "
            "answer is 20 days."
        )
        assert not is_refusal(hedged)

    def test_hedged_answer_with_citations_is_not_a_refusal(self):
        """Same hedged-answer case as above, but with a citation attached —
        pinned separately because this is the exact shape the old
        ``extract_citations``-based guard used to special-case."""
        partial = (
            "The 2025 amendment is not covered in the source material, but the "
            "general position is stated [Handbook, para 16.13, p.691]."
        )
        assert not is_refusal(partial)

    def test_phrase_embedded_mid_sentence_is_not_a_refusal(self):
        embedded = f"The board found this {REFUSAL_PHRASE} and moved on."
        assert not is_refusal(embedded)

    def test_empty_string_is_not_a_refusal(self):
        assert not is_refusal("")

    def test_caveat_form_answer_is_not_a_refusal(self):
        """A caveat-form answer (D44) is a real, if hedged, answer — not the
        canonical refusal sentence — so it must not be classified as one."""
        caveat_answer = (
            f"{CAVEAT_PREFIX} Requisitions must be raised within the agreed "
            "period [Handbook, para 15.2, p.520]."
        )
        assert not is_refusal(caveat_answer)

    def test_refusal_phrase_is_still_a_refusal_alongside_the_caveat_form(self):
        """The plain refusal sentence must keep classifying as a refusal even
        now that the caveat form exists as a separate, non-refusal outcome."""
        assert is_refusal(REFUSAL_PHRASE)


class TestPromptTemplateHumanMessage:
    def test_old_binary_closing_sentence_is_gone(self):
        """D44 replaced the old two-way (answer-or-refuse) closing instruction
        with a three-way one that also names the caveat form; the old sentence
        must not linger anywhere in the human template."""
        human_template = PROMPT_TEMPLATE.messages[1].prompt.template
        assert "If the extracts do not cover the question" not in human_template

    def test_closing_sentence_names_the_caveat_form(self):
        human_template = PROMPT_TEMPLATE.messages[1].prompt.template
        assert "caveat form" in human_template


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

    def test_appendix_citation_grounds_against_an_appendix_chunk(self):
        """An appendix citation (as produced by extract_citations, para key
        'APPENDIX 14.1') grounds when a retrieved chunk's section_number is
        the same appendix and the cited page falls within its page span."""
        results = [
            {
                "document": Document(
                    page_content="Form 14.1 — prescribed contract for sale...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "APPENDIX 14.1",
                        "page_start": 86,
                        "page_end": 88,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]
        check = validate_citations(citations, results)

        assert len(check["grounded"]) == 1
        assert check["ungrounded"] == []

    def test_appendix_citation_page_outside_span_is_ungrounded(self):
        """Same appendix chunk as above, but the cited page (99) falls
        outside its [86, 88] span — a page hallucination, stays ungrounded."""
        results = [
            {
                "document": Document(
                    page_content="Form 14.1 — prescribed contract for sale...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "APPENDIX 14.1",
                        "page_start": 86,
                        "page_end": 88,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [
            {"para": "APPENDIX 14.1", "page": "99", "raw": "APPENDIX 14.1, p.99"}
        ]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1

    def test_para_citation_does_not_ground_against_an_appendix_chunk(self):
        """A plain paragraph citation '14.1' must never match an appendix
        chunk 'APPENDIX 14.1', even on the same page — never-cross-match
        (D34)."""
        results = [
            {
                "document": Document(
                    page_content="Form 14.1 — prescribed contract for sale...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "APPENDIX 14.1",
                        "page_start": 86,
                        "page_end": 88,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [{"para": "14.1", "page": "87", "raw": "para 14.1, p.87"}]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1

    def test_appendix_citation_does_not_ground_against_a_numeric_chunk(self):
        """Reverse direction of the never-cross-match rule: an appendix
        citation must never match a plain numeric chunk '14.1'."""
        results = [
            {
                "document": Document(
                    page_content="...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.1",
                        "page_start": 86,
                        "page_end": 88,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [
            {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}
        ]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1

    def test_related_chunk_without_page_start_is_ungrounded(self):
        """Fail closed (D35 mandatory page check): a chunk whose section relates
        to the citation but carries NO page_start cannot verify the page, so the
        citation stays ungrounded. Previously such a chunk grounded the citation
        outright ('no page info to contradict it') — that hole is now closed."""
        results = [
            {
                "document": Document(
                    page_content="s.72 burdens...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        # no page_start / page_end at all
                    },
                ),
                "score": 0.03,
                "metadata": {},
            }
        ]
        citations = [{"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"}]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1

    def test_page_outside_span_not_rescued_by_a_pageless_related_chunk(self):
        """A related chunk with pages puts the cited page OUTSIDE its span, and a
        SECOND related chunk has no pages: the second no longer rescues the
        citation (it fails closed), so the citation stays ungrounded even though
        two chunks relate to the paragraph."""
        results = [
            {
                "document": Document(
                    page_content="priority entry...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        "page_start": 412,
                        "page_end": 412,
                    },
                ),
                "score": 0.03,
                "metadata": {},
            },
            {
                "document": Document(
                    page_content="priority entry (duplicate, no pages)...",
                    metadata={
                        "document_type": "handbook",
                        "section_number": "14.8.5",
                        # no page metadata
                    },
                ),
                "score": 0.02,
                "metadata": {},
            },
        ]
        citations = [{"para": "14.8.5", "page": "999", "raw": "para 14.8.5, p.999"}]
        check = validate_citations(citations, results)

        assert check["grounded"] == []
        assert len(check["ungrounded"]) == 1


class TestSectionsRelatedAppendix:
    """D34: ``_sections_related`` now formalizes appendix matching as an
    explicit never-cross-match rule — appendix-ness must match on both sides,
    and nesting applies WITHIN appendices the same way it does for plain
    paragraphs. (Previously this behavior was accidental — a side effect of
    splitting both strings on "." with no appendix-aware logic; these tests
    used to pin that accident. It is now an intentional, documented rule.)"""

    def test_identical_appendix_strings_relate(self):
        assert _sections_related("APPENDIX 14.1", "APPENDIX 14.1") is True

    def test_numeric_paragraph_never_relates_to_an_appendix(self):
        # Never-cross-match: a plain numeric paragraph never relates to an
        # appendix locator, in either direction, even when the numeric tail
        # is identical.
        assert _sections_related("14.1", "APPENDIX 14.1") is False
        assert _sections_related("APPENDIX 14.1", "14.1") is False

    def test_subitem_nests_within_an_appendix(self):
        # Nesting applies within appendices: "APPENDIX 14.1" relates to its
        # sub-item "APPENDIX 14.1.2", the same component-nesting rule used
        # for plain paragraphs, applied after stripping the shared
        # "APPENDIX " prefix from both sides.
        assert _sections_related("APPENDIX 14.1", "APPENDIX 14.1.2") is True
        assert _sections_related("APPENDIX 14.1.2", "APPENDIX 14.1") is True

    def test_sibling_appendices_do_not_relate(self):
        assert _sections_related("APPENDIX 14.1", "APPENDIX 14.2") is False


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
        # A non-refusal answer that cites nothing is unverified (P0 fix).
        assert result["gate_outcome"] == CITATIONS_UNVERIFIED

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
        # One grounded + one ungrounded citation → partially verified.
        assert result["gate_outcome"] == PARTIALLY_VERIFIED
