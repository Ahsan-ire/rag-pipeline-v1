"""Tests for the Phase 5 evaluation harness (src/evaluator.py).

Everything is mocked: no network, no API calls, no real vector store. Fake
``retrieve_fn``/``answer_fn`` stand in for src.retriever.retrieve and the
generation path, matching the ``[{"document": Document, "score": float,
"metadata": dict}, ...]`` shape retrieve() actually returns.
"""

import json
import os
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.evaluator import (
    CHROMA_PERSIST_DIR,
    DEFAULT_RESULTS_PATH,
    HIT_KS,
    PARTIAL_RESULTS_PATH,
    _resolve_results_path,
    _sha256_file,
    _wilson_ci,
    collect_provenance,
    evaluate_completeness,
    evaluate_refusals,
    evaluate_retrieval,
    generate_answers,
    load_golden_set,
    run_eval,
    run_eval_matrix,
    split_sentences,
)
from src.generator import REFUSAL_PHRASE
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    REFUSAL,
)


def _cite(para, page):
    """One extracted-citation dict in generate_with_sources' shape."""
    return {"para": para, "page": str(page), "raw": f"para {para}, p.{page}"}


def _matrix_doc(section, page=5, text="chunk-text"):
    """A handbook Document for matrix fakes (carries a page span for grounding)."""
    return Document(
        page_content=text,
        metadata={
            "section_number": section,
            "page_start": page,
            "page_end": page,
            "document_type": "handbook",
        },
    )


def _matrix_golden(tmp_path, name, entries):
    """Write a golden JSONL and return its path."""
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return str(path)


def _answer_entry(
    answer, citations=None, grounded=None, ungrounded=None, gate_outcome=None,
    error=None, has_result=True,
):
    """Build one answers-cache entry (the shape generate_answers produces).

    ``has_result=False`` models a generation error row: result is None and an
    error string is carried, so evaluate_completeness counts it under errors.
    """
    if not has_result:
        return {"result": None, "error": error or "boom"}
    return {
        "result": {
            "answer": answer,
            "citations": citations or [],
            "citation_check": {
                "grounded": grounded or [],
                "ungrounded": ungrounded or [],
            },
            "gate_outcome": gate_outcome,
        },
        "error": None,
    }


def _write_jsonl(path, lines):
    """Write raw text lines (already JSON-encoded, or blank) to a JSONL file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _result(section_number):
    """Build one fake retrieve() result carrying only a section_number."""
    doc = Document(page_content="", metadata={"section_number": section_number})
    return {"document": doc, "score": 1.0, "metadata": doc.metadata}


def _fake_provenance():
    """A canned provenance dict for run_eval tests.

    The real ``collect_provenance`` shells out to git and opens the Chroma
    store; injecting this keeps the test suite IO-free (CLAUDE.md: no network,
    no real vector store). Shape matches ``collect_provenance``'s return.
    """
    return {
        "git_sha": "deadbee",
        "git_dirty": False,
        "git_dirty_other": 0,
        "chunk_count": 42,
        "embedding_model": "fake-embed-model",
        "generation_model": "fake-gen-model",
        "matching": "strict = exact; related = nested",
    }


class TestLoadGoldenSet:
    def test_loads_valid_file(self, tmp_path):
        """A well-formed golden set loads with all three entries intact."""
        lines = [
            json.dumps(
                {"question": "What is a will?", "expected_sections": ["14.8"], "type": "direct"}
            ),
            json.dumps(
                {"question": "Cite 3.2.1", "expected_sections": ["3.2.1"], "type": "exact_token"}
            ),
            json.dumps(
                {"question": "What is the CGT rate?", "expected_sections": [], "type": "refusal"}
            ),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        golden = load_golden_set(path)

        assert len(golden) == 3
        assert golden[0] == {
            "question": "What is a will?",
            "type": "direct",
            "expected_sections": ["14.8"],
        }
        assert golden[2]["type"] == "refusal"
        assert golden[2]["expected_sections"] == []

    def test_skips_blank_lines(self, tmp_path):
        """Blank lines between entries are ignored, not treated as records."""
        lines = [
            json.dumps({"question": "Q1", "expected_sections": ["1.1"], "type": "direct"}),
            "",
            "   ",
            json.dumps({"question": "Q2", "expected_sections": ["2.2"], "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        golden = load_golden_set(path)

        assert len(golden) == 2

    def test_bad_type_raises_with_line_number(self, tmp_path):
        """An unrecognised 'type' value raises ValueError naming its line."""
        lines = [
            json.dumps({"question": "Q1", "expected_sections": ["1.1"], "type": "direct"}),
            json.dumps({"question": "Q2", "expected_sections": ["2.2"], "type": "bogus"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 2"):
            load_golden_set(path)

    def test_refusal_with_nonempty_expected_sections_raises(self, tmp_path):
        """A refusal-type row must carry an empty expected_sections list."""
        lines = [
            json.dumps(
                {"question": "What is the CGT rate?", "expected_sections": ["16.13"], "type": "refusal"}
            ),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 1"):
            load_golden_set(path)

    def test_nonrefusal_with_empty_expected_sections_raises(self, tmp_path):
        """A direct/exact_token row must carry at least one expected section."""
        lines = [
            json.dumps({"question": "What is a will?", "expected_sections": [], "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 1"):
            load_golden_set(path)

    def test_empty_question_raises(self, tmp_path):
        """A blank 'question' string is rejected, not silently accepted."""
        lines = [
            json.dumps({"question": "  ", "expected_sections": ["1.1"], "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 1"):
            load_golden_set(path)

    def test_string_expected_sections_raises_naming_line(self, tmp_path):
        """A bare string (not a list) is rejected: iterating it char-by-char
        would silently inflate hit@k, so it must fail loudly at load."""
        lines = [
            json.dumps({"question": "Q1", "expected_sections": ["1.1"], "type": "direct"}),
            json.dumps({"question": "Q2", "expected_sections": "14.8", "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 2"):
            load_golden_set(path)

    def test_non_string_element_raises_naming_line(self, tmp_path):
        """A numeric element (3.1 unquoted -> float) is rejected: str(3.10)
        collapses to '3.1', so non-string members must fail at load."""
        lines = [
            json.dumps({"question": "Q1", "expected_sections": [3.1], "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        with pytest.raises(ValueError, match="Line 1"):
            load_golden_set(path)

    def test_whitespace_padded_element_is_stripped(self, tmp_path):
        """A padded ' 14.8.5 ' loads with the stripped value; unstripped it
        could never match a chunk's section_number and would deflate hit@k."""
        lines = [
            json.dumps({"question": "Q1", "expected_sections": [" 14.8.5 "], "type": "direct"}),
        ]
        path = _write_jsonl(tmp_path / "golden.jsonl", lines)

        golden = load_golden_set(path)

        assert golden[0]["expected_sections"] == ["14.8.5"]


class TestEvaluateRetrieval:
    def test_exact_section_match_is_a_hit_both_ways(self):
        """An exact equality scores under BOTH strict and related."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.8.5"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.8.5")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["total"] == 1
        assert report["hits_strict"] == 1
        assert report["hits_related"] == 1
        assert report["hit_rate_strict"] == 1.0
        assert report["hit_rate_related"] == 1.0
        assert report["per_question"][0]["hit_strict"] is True
        assert report["per_question"][0]["hit_related"] is True

    def test_nested_section_is_related_hit_but_strict_miss(self):
        """Expected 14.12 vs a retrieved sub-paragraph 14.12.1: nests (related
        HIT) but is not an exact match (strict MISS). This is the whole point
        of splitting the metric."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.12"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.12.1")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits_strict"] == 0
        assert report["hits_related"] == 1
        assert report["per_question"][0]["hit_strict"] is False
        assert report["per_question"][0]["hit_related"] is True

        # by_type must split the SAME way on this diverging case. With exactly
        # one diverging 'direct' question the numbers are unambiguous, so this
        # catches the strict/related accumulators being swapped (they coincide
        # on exact-match cases and only diverge here).
        direct_stats = report["by_type"]["direct"]
        assert direct_stats["hits_strict"] == 0
        assert direct_stats["hits_related"] == 1
        assert direct_stats["hit_rate_strict"] == 0.0
        assert direct_stats["hit_rate_related"] == 1.0

    def test_sibling_section_is_a_miss_both_ways(self):
        """Expected 14.1 vs retrieved 14.12 do not nest -> miss under both."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.1"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.12")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits_strict"] == 0
        assert report["hits_related"] == 0
        assert report["per_question"][0]["hit_strict"] is False
        assert report["per_question"][0]["hit_related"] is False
        assert report["hit_rate_strict"] == 0.0
        assert report["hit_rate_related"] == 0.0

    def test_appendix_expected_section_scores_both_metrics_against_an_appendix_chunk(self):
        """Phase 7 / D34: a golden entry expecting an appendix locator scores
        a hit under BOTH strict (exact string equality) and related (which
        delegates to _sections_related's never-cross-match rule) when the
        retrieved chunk's section_number is the same appendix."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["APPENDIX 14.1"]}
        ]
        fake_retrieve = lambda q, top_k=6: [_result("APPENDIX 14.1")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits_strict"] == 1
        assert report["hits_related"] == 1
        assert report["per_question"][0]["hit_strict"] is True
        assert report["per_question"][0]["hit_related"] is True

    def test_appendix_expected_section_misses_against_a_numeric_chunk(self):
        """Never-cross-match: a numeric chunk '14.1' must not satisfy an
        expected 'APPENDIX 14.1' under either metric, even though the digits
        are identical."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["APPENDIX 14.1"]}
        ]
        fake_retrieve = lambda q, top_k=6: [_result("14.1")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits_strict"] == 0
        assert report["hits_related"] == 0
        assert report["per_question"][0]["hit_strict"] is False
        assert report["per_question"][0]["hit_related"] is False

    def test_refusal_questions_are_excluded_from_retrieval_scoring(self):
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "Q2", "type": "refusal", "expected_sections": []},
        ]
        fake_retrieve = lambda q, top_k=6: [_result("1.1")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["total"] == 1
        assert len(report["per_question"]) == 1

    def test_by_type_split_and_hit_rate_math(self):
        golden = [
            {"question": "d1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "d2", "type": "direct", "expected_sections": ["2.2"]},
            {"question": "e1", "type": "exact_token", "expected_sections": ["3.3"]},
        ]

        def fake_retrieve(question, top_k=6):
            # d1 hits, d2 misses, e1 hits
            if question == "d1":
                return [_result("1.1")]
            if question == "d2":
                return [_result("9.9")]
            return [_result("3.3")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        # Every match here is exact, so strict and related counts coincide.
        assert report["hits_strict"] == 2
        assert report["hits_related"] == 2
        assert report["total"] == 3
        assert report["hit_rate_strict"] == pytest.approx(2 / 3)
        assert report["hit_rate_related"] == pytest.approx(2 / 3)
        assert report["by_type"]["direct"] == {
            "hits_strict": 1,
            "hits_related": 1,
            "total": 2,
            "hit_rate_strict": 0.5,
            "hit_rate_related": 0.5,
        }
        assert report["by_type"]["exact_token"] == {
            "hits_strict": 1,
            "hits_related": 1,
            "total": 1,
            "hit_rate_strict": 1.0,
            "hit_rate_related": 1.0,
        }

    def test_hit_rate_zero_when_no_questions(self):
        """All-refusal golden set -> zero non-refusal questions -> both hit
        rates 0.0, no ZeroDivisionError."""
        golden = [{"question": "Q1", "type": "refusal", "expected_sections": []}]

        report = evaluate_retrieval(golden, retrieve_fn=lambda q, top_k=6: [])

        assert report["total"] == 0
        assert report["hits_strict"] == 0
        assert report["hits_related"] == 0
        assert report["hit_rate_strict"] == 0.0
        assert report["hit_rate_related"] == 0.0
        assert report["by_type"] == {}


class TestHitAtKAndMrr:
    """Phase 10 (D38): evaluate_retrieval additionally records the 1-indexed
    rank of the first strict/related match per question, and derives hit@{1,3,6}
    and truncated MRR@top_k from those ranks — no extra retrievals, and the
    old hit_strict/hit_related flags stay exactly the any()-based booleans."""

    def _retrieve_in_order(self, sections):
        """A fake retrieve_fn that returns chunks with the given section_numbers
        in exactly that rank order (rank 1 first)."""
        return lambda q, top_k=6: [_result(s) for s in sections][:top_k]

    def test_records_first_ranks_strict4_related2(self):
        """Expected 14.12 against retrieved [9.9, 14.12.1, 8.8, 14.12, ...]:
        the first RELATED match (nested 14.12.1) is at rank 2, the first STRICT
        (exact 14.12) is at rank 4 — the ranks the whole metric derives from."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.12"]}]
        retrieve_fn = self._retrieve_in_order(
            ["9.9", "14.12.1", "8.8", "14.12", "1.1", "2.2"]
        )

        report = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=6)

        q = report["per_question"][0]
        assert q["first_related_rank"] == 2
        assert q["first_strict_rank"] == 4
        # The legacy flags still say "matched at all", derived from the ranks.
        assert q["hit_strict"] is True
        assert q["hit_related"] is True

    def test_hit_at_k_derivation_and_mrr_fractions(self):
        """Same single question (strict rank 4, related rank 2, top_k=6):
        hit@k counts a match only when its first rank <= k, and MRR is the
        exact reciprocal of the first rank."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.12"]}]
        retrieve_fn = self._retrieve_in_order(
            ["9.9", "14.12.1", "8.8", "14.12", "1.1", "2.2"]
        )

        report = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=6)

        assert report["ks"] == [1, 3, 6]
        # strict first at rank 4: misses @1 and @3, hits @6.
        assert report["hit_at_k"]["strict"] == {1: 0, 3: 0, 6: 1}
        # related first at rank 2: misses @1, hits @3 and @6.
        assert report["hit_at_k"]["related"] == {1: 0, 3: 1, 6: 1}
        assert report["hit_rate_at_k"]["strict"][6] == 1.0
        assert report["hit_rate_at_k"]["related"][1] == 0.0
        assert report["mrr_strict"] == pytest.approx(1 / 4)
        assert report["mrr_related"] == pytest.approx(1 / 2)

    def test_mrr_averages_over_all_questions_including_misses(self):
        """A missed question contributes 0 to the truncated MRR sum (it is NOT
        dropped from the denominator): Q1 strict rank 2, Q2 never matched ->
        mrr_strict = (1/2 + 0) / 2 = 0.25."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "Q2", "type": "direct", "expected_sections": ["2.2"]},
        ]

        def retrieve_fn(question, top_k=6):
            if question == "Q1":
                return [_result("9.9"), _result("1.1")]  # strict rank 2
            return [_result("8.8"), _result("7.7")]  # Q2 never matches

        report = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=6)

        assert report["total"] == 2
        assert report["mrr_strict"] == pytest.approx((1 / 2 + 0) / 2)

    def test_monotone_and_related_dominates_strict(self):
        """Invariants that must hold for any run: hit@1 <= hit@3 <= hit@6 (a
        deeper cut-off can only add hits), and related >= strict at every k (an
        exact match is always also a related match)."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["14.12"]},
            {"question": "Q2", "type": "direct", "expected_sections": ["3.3"]},
            {"question": "Q3", "type": "direct", "expected_sections": ["5.5"]},
        ]

        def retrieve_fn(question, top_k=6):
            if question == "Q1":  # related @1 (nested), strict @4
                return [_result("14.12.1"), _result("a"), _result("b"), _result("14.12"), _result("c"), _result("d")]
            if question == "Q2":  # strict & related @2
                return [_result("z"), _result("3.3"), _result("y"), _result("x"), _result("w"), _result("v")]
            return [_result("no"), _result("match"), _result("here"), _result("at"), _result("all"), _result("q")]

        report = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=6)

        for mode in ("strict", "related"):
            at = report["hit_at_k"][mode]
            assert at[1] <= at[3] <= at[6]
        for k in report["ks"]:
            assert report["hit_at_k"]["related"][k] >= report["hit_at_k"]["strict"][k]

    def test_top_k_3_limits_ks_to_1_and_3(self):
        """top_k=3 must yield ks=[1,3] only — a hit@6 cell is incoherent when
        just 3 chunks were retrieved, so it is never emitted."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]
        retrieve_fn = self._retrieve_in_order(["a", "b", "1.1"])

        report = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=3)

        assert report["ks"] == [1, 3]
        assert set(report["hit_at_k"]["strict"]) == {1, 3}
        assert 6 not in report["hit_at_k"]["strict"]
        assert report["hit_at_k"]["strict"][3] == 1  # matched at rank 3
        assert report["hit_at_k"]["strict"][1] == 0

    def test_over_returning_retrieve_fn_is_truncated_to_top_k(self):
        """An injected retrieve_fn that returns MORE than top_k results must not
        let a match beyond the cutoff count: top_k=3, match at position 5 -> the
        result is truncated to 3, so it is a MISS (codex finding 7)."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["9.9"]}]
        # 6 results, the only match at rank 5 — but top_k=3.
        over = lambda q, top_k=3: [
            _result("1.1"), _result("2.2"), _result("3.3"),
            _result("4.4"), _result("9.9"), _result("6.6"),
        ]

        report = evaluate_retrieval(golden, retrieve_fn=over, top_k=3)

        assert report["per_question"][0]["first_strict_rank"] is None
        assert report["hit_at_k"]["strict"][3] == 0
        assert report["mrr_strict"] == 0.0

    def test_zero_questions_gives_zero_mrr_and_empty_counts(self):
        """All-refusal set -> no non-refusal questions -> MRR 0.0 and every
        hit@k count 0, with no ZeroDivisionError."""
        golden = [{"question": "Q1", "type": "refusal", "expected_sections": []}]

        report = evaluate_retrieval(golden, retrieve_fn=lambda q, top_k=6: [])

        assert report["total"] == 0
        assert report["mrr_strict"] == 0.0
        assert report["mrr_related"] == 0.0
        assert report["hit_at_k"]["strict"] == {1: 0, 3: 0, 6: 0}
        assert report["hit_rate_at_k"]["related"] == {1: 0.0, 3: 0.0, 6: 0.0}


class TestEvaluateCompleteness:
    """Answer-quality scoring on the ANSWERABLE questions (D38): false-refusal
    and false-block rates, syntactic sentence-citation coverage, citation-
    grounded fraction, and the gate-outcome distribution — all micro-averaged,
    all carrying only counts (never answer text) into per_question rows."""

    def _golden(self):
        return [
            {"question": "Q1", "type": "direct", "expected_sections": ["3.2"]},
            {"question": "Q2", "type": "direct", "expected_sections": ["4.1"]},
            {"question": "Q3", "type": "exact_token", "expected_sections": ["5.1"]},
            # A near-domain negative: must be ignored here (evaluate_refusals'
            # job), never counted as an answerable question.
            {"question": "R1", "type": "refusal", "expected_sections": []},
        ]

    def _answers(self):
        return {
            # Q1: fully cited + grounded, 2 sentences both cited, 2 citations.
            "Q1": _answer_entry(
                "Rule A applies [Handbook, para 3.2, p.5]. "
                "Rule B follows [Handbook, para 3.3, p.6].",
                citations=[_cite("3.2", 5), _cite("3.3", 6)],
                grounded=[_cite("3.2", 5), _cite("3.3", 6)],
                ungrounded=[],
                gate_outcome=CITATIONS_VERIFIED,
            ),
            # Q2: the canonical refusal -> a FALSE refusal of an answerable Q.
            "Q2": _answer_entry(REFUSAL_PHRASE, gate_outcome=REFUSAL),
            # Q3: non-refused but its one citation is ungrounded -> gate blocks
            # it (CITATIONS_UNVERIFIED = false block). 2 sentences, 1 cited.
            "Q3": _answer_entry(
                "Some claim is made without any citation. "
                "Another claim follows [Handbook, para 5.1, p.10].",
                citations=[_cite("5.1", 10)],
                grounded=[],
                ungrounded=[_cite("5.1", 10)],
                gate_outcome=CITATIONS_UNVERIFIED,
            ),
            # The refusal-type question also has an answer in the cache, but
            # evaluate_completeness must skip it entirely.
            "R1": _answer_entry(REFUSAL_PHRASE, gate_outcome=REFUSAL),
        }

    def test_rates_and_distribution(self):
        report = evaluate_completeness(self._golden(), self._answers())

        assert report["total"] == 3  # 3 answerable; R1 excluded
        assert report["errors"] == 0
        # Q2 refused -> 1/3 false refusals.
        assert report["refused"] == 1
        assert report["false_refusal_rate"] == pytest.approx(1 / 3)
        # Q3 blocked (CITATIONS_UNVERIFIED, non-refused) -> 1/3 false blocks.
        assert report["blocked"] == 1
        assert report["false_block_rate"] == pytest.approx(1 / 3)
        # Zero-filled over ALL four outcomes.
        assert report["gate_outcome_distribution"] == {
            REFUSAL: 1,
            CITATIONS_VERIFIED: 1,
            PARTIALLY_VERIFIED: 0,
            CITATIONS_UNVERIFIED: 1,
        }

    def test_micro_averaged_coverage_and_grounding(self):
        report = evaluate_completeness(self._golden(), self._answers())

        # Coverage over NON-refused answers (Q1: 2/2, Q3: 1/2) -> 3/4.
        assert report["sum_sentences"] == 4
        assert report["sum_cited_sentences"] == 3
        assert report["sentence_citation_coverage"] == pytest.approx(3 / 4)
        # The refused answer (Q2) is excluded and disclosed as such.
        assert report["coverage_excluded_refusals"] == 1
        # Grounded fraction over non-refused (Q1: 2 grounded/2, Q3: 0/1) -> 2/3.
        assert report["sum_grounded"] == 2
        assert report["sum_citations"] == 3
        assert report["citation_grounded_fraction"] == pytest.approx(2 / 3)

    def test_per_question_carries_counts_not_answer_text(self):
        """D30 canary: a secret planted in every answer must never surface in
        the per_question rows (they carry counts + flags only)."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["3.2"]}]
        secret = "TOP-SECRET-COPYRIGHTED-ANSWER-PROSE"
        answers = {
            "Q1": _answer_entry(
                f"{secret} [Handbook, para 3.2, p.5].",
                citations=[_cite("3.2", 5)],
                grounded=[_cite("3.2", 5)],
                gate_outcome=CITATIONS_VERIFIED,
            )
        }

        report = evaluate_completeness(golden, answers)

        row = report["per_question"][0]
        assert row["n_sentences"] == 1
        assert row["n_cited_sentences"] == 1
        assert row["n_citations"] == 1
        assert row["n_grounded"] == 1
        assert row["gate_outcome"] == CITATIONS_VERIFIED
        assert row["refused"] is False
        # The answer text (and thus the secret) is nowhere in the rows.
        assert secret not in json.dumps(report["per_question"])

    def test_all_refusal_answerable_set(self):
        """Every answerable question refuses -> false_refusal_rate 1.0, no
        non-refused answers so coverage/grounding are None (not 0), and no
        false blocks (a refusal's outcome is REFUSAL, not CITATIONS_UNVERIFIED)."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "Q2", "type": "direct", "expected_sections": ["2.2"]},
        ]
        answers = {
            "Q1": _answer_entry(REFUSAL_PHRASE, gate_outcome=REFUSAL),
            "Q2": _answer_entry(REFUSAL_PHRASE, gate_outcome=REFUSAL),
        }

        report = evaluate_completeness(golden, answers)

        assert report["false_refusal_rate"] == 1.0
        assert report["blocked"] == 0
        assert report["sentence_citation_coverage"] is None
        assert report["citation_grounded_fraction"] is None
        assert report["coverage_excluded_refusals"] == 2

    def test_zero_citation_answer_gives_na_grounding(self):
        """A non-refused answer with no citations at all -> grounded fraction is
        None ('n/a'), never a silent 0 or 1; coverage still counts its
        sentences (0 cited)."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]
        answers = {
            "Q1": _answer_entry(
                "A claim with no citation at all. And a second such claim.",
                citations=[],
                grounded=[],
                ungrounded=[],
                gate_outcome=CITATIONS_UNVERIFIED,
            )
        }

        report = evaluate_completeness(golden, answers)

        assert report["citation_grounded_fraction"] is None
        assert report["sentence_citation_coverage"] == pytest.approx(0.0)
        assert report["sum_sentences"] == 2
        assert report["sum_cited_sentences"] == 0
        # A citation-free non-refused answer is blocked by the gate.
        assert report["blocked"] == 1

    def test_generation_error_row_counted_not_scored(self):
        """A question whose generation errored (result is None) is counted under
        errors and excluded from every rate denominator."""
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "Q2", "type": "direct", "expected_sections": ["2.2"]},
        ]
        answers = {
            "Q1": _answer_entry(
                "Good answer [Handbook, para 1.1, p.2].",
                citations=[_cite("1.1", 2)],
                grounded=[_cite("1.1", 2)],
                gate_outcome=CITATIONS_VERIFIED,
            ),
            "Q2": _answer_entry(None, has_result=False, error="api boom"),
        }

        report = evaluate_completeness(golden, answers)

        assert report["total"] == 1  # only Q1 scored
        assert report["errors"] == 1  # Q2 errored
        assert report["false_refusal_rate"] == 0.0  # 0 refused / 1 scored
        # The error row is present but carries no gate outcome / flags.
        err_row = next(r for r in report["per_question"] if r["question"] == "Q2")
        assert err_row["error"] == "api boom"
        assert err_row["gate_outcome"] is None


class TestGenerateAnswers:
    """One generation per in-scope question, cached and keyed by question text,
    with bounded retries and error rows — the shared answer source for the
    refusal, completeness, and judge passes (D38)."""

    def _golden(self):
        return [
            {"question": "D1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "E1", "type": "exact_token", "expected_sections": ["2.2"]},
            {"question": "R1", "type": "refusal", "expected_sections": []},
        ]

    def test_include_types_filters_generation(self):
        """Only questions whose type is in include_types are generated; the
        rest never reach generate_fn (this is what keeps a skipped pass — and
        keyless CI — from making any API call)."""
        seen = []

        def generate_fn(question):
            seen.append(question)
            return {"answer": "a"}

        answers = generate_answers(
            self._golden(), include_types=["direct", "exact_token"], generate_fn=generate_fn
        )

        assert seen == ["D1", "E1"]  # R1 (refusal) skipped
        assert set(answers) == {"D1", "E1"}
        assert answers["D1"]["result"] == {"answer": "a"}
        assert answers["D1"]["error"] is None

    def test_refusal_only_include_types(self):
        """include_types=['refusal'] generates ONLY the refusal question."""
        seen = []

        def generate_fn(question):
            seen.append(question)
            return {"answer": "x"}

        generate_answers(self._golden(), include_types=["refusal"], generate_fn=generate_fn)

        assert seen == ["R1"]

    def test_duplicate_question_raises(self):
        """Two in-scope questions with identical text would collide in the
        cache -> ValueError (fail visible, never silently drop one)."""
        golden = [
            {"question": "same", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "same", "type": "direct", "expected_sections": ["2.2"]},
        ]

        with pytest.raises(ValueError, match="Duplicate question"):
            generate_answers(golden, include_types=["direct"], generate_fn=lambda q: {"answer": "a"})

    def test_retry_then_succeed(self):
        """generate_fn failing twice then succeeding -> 3 attempts, a cached
        result, and no error. retry_backoff=0 keeps the test instant."""
        calls = {"n": 0}

        def flaky(question):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return {"answer": "recovered"}

        answers = generate_answers(
            [{"question": "D1", "type": "direct", "expected_sections": ["1.1"]}],
            include_types=["direct"],
            generate_fn=flaky,
            retries=2,
            retry_backoff=0,
        )

        assert calls["n"] == 3
        assert answers["D1"]["result"] == {"answer": "recovered"}
        assert answers["D1"]["error"] is None

    def test_exhausted_retries_records_error_row(self):
        """A question that fails every attempt is recorded as an error row
        (result None, error message), never dropped."""

        def always_fail(question):
            raise RuntimeError("permanent boom")

        answers = generate_answers(
            [{"question": "D1", "type": "direct", "expected_sections": ["1.1"]}],
            include_types=["direct"],
            generate_fn=always_fail,
            retries=2,
            retry_backoff=0,
        )

        assert answers["D1"]["result"] is None
        assert "permanent boom" in answers["D1"]["error"]


class TestSha256File:
    def test_matches_hashlib(self, tmp_path):
        import hashlib

        content = b"held-out set line 1\nheld-out set line 2\n"
        path = tmp_path / "heldout.jsonl"
        path.write_bytes(content)

        assert _sha256_file(str(path)) == hashlib.sha256(content).hexdigest()


class TestResolveResultsPath:
    """The results-path guard that protects the committed eval/results.md (D38)."""

    def test_canonical_no_explicit_writes_default(self):
        path, warnings = _resolve_results_path(None, is_canonical=True, set_paths=["eval/golden_set.jsonl"])
        assert path == DEFAULT_RESULTS_PATH
        assert warnings == []

    def test_noncanonical_no_explicit_writes_partial_with_note(self):
        path, warnings = _resolve_results_path(None, is_canonical=False, set_paths=["eval/golden_set.jsonl"])
        assert path == PARTIAL_RESULTS_PATH
        assert warnings and "not canonical" in warnings[0].lower()

    def test_explicit_equal_to_input_set_is_refused(self):
        with pytest.raises(ValueError, match="eval-set"):
            _resolve_results_path(
                "eval/golden_set.jsonl", is_canonical=True, set_paths=["eval/golden_set.jsonl"]
            )

    def test_explicit_default_on_noncanonical_honored_with_warning(self):
        path, warnings = _resolve_results_path(
            DEFAULT_RESULTS_PATH, is_canonical=False, set_paths=["eval/golden_set.jsonl"]
        )
        assert path == DEFAULT_RESULTS_PATH
        assert warnings and "NON-canonical" in warnings[0]

    def test_explicit_other_path_honored_no_warning(self):
        path, warnings = _resolve_results_path(
            "eval/scratch.md", is_canonical=False, set_paths=["eval/golden_set.jsonl"]
        )
        assert path == "eval/scratch.md"
        assert warnings == []


class TestSplitSentences:
    """The heuristic splitter for the completeness metric (D38). It gates
    nothing, but must (a) never split inside a bracketed citation, (b) respect
    the prose-abbreviation guards, (c) split bullets and normal prose, and
    (d) hand back each sentence with its citation brackets intact."""

    def test_no_split_inside_bracketed_citation(self):
        """A period inside [Handbook, para 3.2, p.5] must not end a sentence;
        the two real sentences split on the period AFTER each bracket, and each
        citation is returned verbatim."""
        text = (
            "The rule is X [Handbook, para 3.2, p.5]. "
            "The next point follows [Handbook, para 3.3, p.6]."
        )
        out = split_sentences(text)
        assert out == [
            "The rule is X [Handbook, para 3.2, p.5].",
            "The next point follows [Handbook, para 3.3, p.6].",
        ]

    def test_abbreviation_guards_do_not_split(self):
        """'para.' and 'e.g.' keep their periods, so neither ends a sentence."""
        assert split_sentences("See para. 3 for details. It applies here.") == [
            "See para. 3 for details.",
            "It applies here.",
        ]
        assert split_sentences(
            "A lease may be granted, e.g. a 99-year term. That is common."
        ) == [
            "A lease may be granted, e.g. a 99-year term.",
            "That is common.",
        ]

    def test_prose_splits_on_terminal_punctuation_and_capital(self):
        """A '.' (and '?'/'!') followed by whitespace and a capital starts a
        new sentence."""
        assert split_sentences("First. Second? Third! Fourth.") == [
            "First.",
            "Second?",
            "Third!",
            "Fourth.",
        ]

    def test_newlines_split_bullets(self):
        """Bullet / list items on their own lines each become a sentence, even
        without terminal punctuation on every line."""
        out = split_sentences("First point.\n- Second bullet\n- Third bullet.")
        assert out == ["First point.", "- Second bullet", "- Third bullet."]

    def test_empty_and_whitespace_return_empty_list(self):
        assert split_sentences("") == []
        assert split_sentences("   \n  \t ") == []

    def test_single_unterminated_sentence_preserved(self):
        """A lone clause with no terminal punctuation is still one sentence."""
        assert split_sentences("Only one clause here") == ["Only one clause here"]

    def test_trailing_citation_with_internal_period_is_one_sentence(self):
        """A sentence ending in a citation whose page has a period (p.412) must
        not split on that internal period — the bracket is restored intact."""
        assert split_sentences("Cited at end [Handbook, para 14.8.5, p.412]") == [
            "Cited at end [Handbook, para 14.8.5, p.412]"
        ]


class TestWilsonCi:
    """_wilson_ci is a pure-math helper (stdlib only) for the small-n honesty
    intervals in the v2 report; these pin known values and its invariants."""

    def test_zero_n_returns_zero_interval(self):
        """n=0 has no data: return (0.0, 0.0) rather than dividing by zero."""
        assert _wilson_ci(0, 0) == (0.0, 0.0)

    def test_full_success_upper_bound_is_exactly_one(self):
        """p=1 -> the Wilson upper bound is exactly 1.0 (algebraically, before
        clamping), and the lower bound sits strictly below 1."""
        low, high = _wilson_ci(10, 10)
        assert high == pytest.approx(1.0)
        assert low == pytest.approx(0.7225, abs=1e-3)
        assert 0.0 < low < 1.0

    def test_symmetry_between_zero_and_full(self):
        """The interval is symmetric under success<->failure: the upper bound
        of (0 of n) equals 1 minus the lower bound of (n of n)."""
        low_full, _ = _wilson_ci(10, 10)
        low_zero, high_zero = _wilson_ci(0, 10)
        assert low_zero == 0.0
        assert high_zero == pytest.approx(1.0 - low_full)

    def test_bounds_always_within_unit_interval(self):
        """For a spread of (hits, n), both bounds stay clamped to [0, 1]."""
        for hits, n in [(0, 1), (1, 1), (3, 20), (17, 20), (20, 20), (50, 100)]:
            low, high = _wilson_ci(hits, n)
            assert 0.0 <= low <= high <= 1.0

    def test_hit_ks_constant_is_1_3_6(self):
        """The reported cut-offs are fixed at 1, 3, 6 (headline is strict@6)."""
        assert HIT_KS == (1, 3, 6)


class TestLoadOnceDefaultRetrieveFn:
    """Phase 9 (load-once retrieval): when evaluate_retrieval is NOT given a
    retrieve_fn, its default builds the vector store + BM25 index + embedding-
    model check exactly ONCE per evaluate_retrieval call (not once per question)
    via src.retriever.load_retrieval_context, and reuses them through
    src.retriever.retrieve's injection params. Fully IO-free:
    src.evaluator.load_retrieval_context and src.evaluator.retrieve are replaced
    with fakes, so no real Chroma store, BM25 pickle, or embedding model is ever
    touched."""

    def _patch_builders(self, monkeypatch, fake_store, fake_bm25):
        """Patch the load-once helper and the retrieve seam evaluate_retrieval's
        default path touches, recording every call, and return the call logs."""
        context_calls = []
        retrieve_calls = []

        def fake_load_retrieval_context(persist_directory):
            context_calls.append(persist_directory)
            return fake_store, fake_bm25

        def fake_retrieve(
            question, top_k=6, persist_directory=None, vector_store=None, bm25_index=None
        ):
            retrieve_calls.append(
                {
                    "question": question,
                    "top_k": top_k,
                    "persist_directory": persist_directory,
                    "vector_store": vector_store,
                    "bm25_index": bm25_index,
                }
            )
            return [_result("1.1")]

        monkeypatch.setattr("src.evaluator.load_retrieval_context", fake_load_retrieval_context)
        monkeypatch.setattr("src.evaluator.retrieve", fake_retrieve)

        return context_calls, retrieve_calls

    def test_builders_called_once_across_three_questions(self, monkeypatch):
        """3 non-refusal questions -> retrieve is called 3 times, but
        load_retrieval_context (which builds the store + BM25 index + runs the
        model check) runs exactly once, and every retrieve call received the
        SAME injected store/bm25 objects (proof they were built once and reused,
        not rebuilt per question)."""
        golden = [
            {"question": f"Q{i}", "type": "direct", "expected_sections": ["1.1"]}
            for i in range(3)
        ]
        fake_store = object()
        fake_bm25 = object()
        context_calls, retrieve_calls = self._patch_builders(
            monkeypatch, fake_store, fake_bm25
        )

        report = evaluate_retrieval(golden, top_k=6)

        assert report["total"] == 3
        assert len(context_calls) == 1
        assert len(retrieve_calls) == 3
        for call in retrieve_calls:
            assert call["vector_store"] is fake_store
            assert call["bm25_index"] is fake_bm25

    def test_persist_directory_threaded_to_every_builder(self, monkeypatch):
        """A non-default persist_directory reaches load_retrieval_context AND
        the retrieve call itself."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]
        context_calls, retrieve_calls = self._patch_builders(
            monkeypatch, object(), object()
        )

        evaluate_retrieval(golden, persist_directory="/tmp/custom")

        assert context_calls == ["/tmp/custom"]
        assert retrieve_calls[0]["persist_directory"] == "/tmp/custom"

    def test_explicit_retrieve_fn_bypasses_all_building(self, monkeypatch):
        """Passing retrieve_fn explicitly must never touch
        load_retrieval_context at all."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]

        def _boom(*args, **kwargs):
            raise AssertionError("must not be called when retrieve_fn is given explicitly")

        monkeypatch.setattr("src.evaluator.load_retrieval_context", _boom)

        report = evaluate_retrieval(golden, retrieve_fn=lambda q, top_k=6: [_result("1.1")])

        assert report["hits_strict"] == 1


class TestEvaluateRefusals:
    def test_canonical_phrase_alone_is_refused(self):
        golden = [{"question": "What is the CGT rate?", "type": "refusal", "expected_sections": []}]
        fake_answer = lambda q: REFUSAL_PHRASE

        report = evaluate_refusals(golden, answer_fn=fake_answer)

        assert report["refused"] == 1
        assert report["total"] == 1
        assert report["accuracy"] == 1.0
        assert report["per_question"][0]["refused"] is True
        # The raw answer must not leak out of the result contract (D30): the
        # per-question row carries only the question and the refusal flag.
        assert "answer" not in report["per_question"][0]
        assert set(report["per_question"][0]) == {"question", "refused"}

    def test_phrase_with_citation_is_not_refused(self):
        """A hedge that still cites a source is an answer, not a refusal (is_refusal
        requires zero extractable citations)."""
        golden = [{"question": "What is the CGT rate?", "type": "refusal", "expected_sections": []}]
        answer = (
            f"This is {REFUSAL_PHRASE} in general, but see [Handbook, para 16.13, p.691]."
        )
        fake_answer = lambda q: answer

        report = evaluate_refusals(golden, answer_fn=fake_answer)

        assert report["refused"] == 0
        assert report["accuracy"] == 0.0
        assert report["per_question"][0]["refused"] is False

    def test_non_refusal_questions_are_excluded(self):
        golden = [
            {"question": "Q1", "type": "direct", "expected_sections": ["1.1"]},
            {"question": "Q2", "type": "refusal", "expected_sections": []},
        ]
        fake_answer = lambda q: REFUSAL_PHRASE

        report = evaluate_refusals(golden, answer_fn=fake_answer)

        assert report["total"] == 1

    def test_accuracy_zero_when_no_refusal_questions(self):
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]

        report = evaluate_refusals(golden, answer_fn=lambda q: REFUSAL_PHRASE)

        assert report["total"] == 0
        assert report["refused"] == 0
        assert report["accuracy"] == 0.0


class TestRunEval:
    def _golden_path(self, tmp_path):
        lines = [
            json.dumps(
                {"question": "What does the handbook say about X?", "expected_sections": ["14.8.5"], "type": "direct"}
            ),
            json.dumps(
                {"question": "What is the CGT rate?", "expected_sections": [], "type": "refusal"}
            ),
        ]
        return _write_jsonl(tmp_path / "golden.jsonl", lines)

    def test_writes_results_md_with_hit_rate_and_no_chunk_content(self, tmp_path, capsys):
        golden_path = self._golden_path(tmp_path)
        results_path = tmp_path / "out" / "results.md"
        secret_chunk_text = "TOP-SECRET-COPYRIGHTED-HANDBOOK-PROSE"

        def fake_retrieve(question, top_k=6):
            # The retrieved chunk carries copyrighted prose in page_content;
            # the report must never echo it (D30). This is the leak source
            # that keeps the assertions below meaningful.
            doc = Document(
                page_content=secret_chunk_text,
                metadata={"section_number": "14.8.5"},
            )
            return [{"document": doc, "score": 1.0, "metadata": doc.metadata}]

        def fake_answer(question):
            # The refusal question must score as a refusal under the tightened
            # (exact-match) is_refusal, so return EXACTLY the canonical phrase.
            # Any other question defensively returns corpus prose, so if the
            # harness ever generated an answer for a non-refusal question and a
            # report path leaked it, the leak assertions below would fire.
            if "CGT" in question:
                return REFUSAL_PHRASE
            return f"Per the handbook, {secret_chunk_text}."

        result = run_eval(
            golden_path,
            top_k=6,
            results_path=str(results_path),
            retrieve_fn=fake_retrieve,
            answer_fn=fake_answer,
            provenance_fn=_fake_provenance,
        )

        assert results_path.exists()
        content = results_path.read_text(encoding="utf-8")
        assert "hit rate" in content.lower()
        assert secret_chunk_text not in content

        captured = capsys.readouterr()
        assert secret_chunk_text not in captured.out

        # Dual metric: the retrieved 14.8.5 matches expected 14.8.5 exactly.
        assert result["retrieval"]["hits_strict"] == 1
        assert result["retrieval"]["hits_related"] == 1
        assert result["refusals"]["refused"] == 1

    def test_skip_refusals_prints_skipped(self, tmp_path, capsys):
        golden_path = self._golden_path(tmp_path)
        results_path = tmp_path / "results.md"

        def fake_retrieve(question, top_k=6):
            return [_result("14.8.5")]

        result = run_eval(
            golden_path,
            top_k=6,
            skip_refusals=True,
            results_path=str(results_path),
            retrieve_fn=fake_retrieve,
            provenance_fn=_fake_provenance,
        )

        captured = capsys.readouterr()
        assert "skipped" in captured.out
        assert result["refusals"] is None
        assert "skipped" in results_path.read_text(encoding="utf-8")

    def test_load_retrieval_context_built_once_for_the_whole_run(self, tmp_path, monkeypatch):
        """FIX 2: a full run_eval (retrieval pass + refusal pass, both defaults)
        opens the store and unpickles the BM25 index EXACTLY ONCE for the whole
        run, not once per evaluate_* pass. The refusal pass's default answer_fn
        is derived from the same load-once retrieve_fn as the retrieval pass, so
        load_retrieval_context fires a single time. Fully IO-free: the helper,
        retrieve, and generate_with_sources are all faked."""
        golden_path = self._golden_path(tmp_path)  # 1 direct + 1 refusal
        results_path = tmp_path / "results.md"

        context_calls = []

        def fake_load_retrieval_context(persist_directory):
            context_calls.append(persist_directory)
            return object(), object()  # sentinel store, sentinel bm25

        def fake_retrieve(
            question, top_k=6, persist_directory=None, vector_store=None, bm25_index=None
        ):
            return [_result("14.8.5")]

        def fake_generate_with_sources(question, results):
            # Only ["answer"] is read by the derived answer_fn; a refusal answer
            # makes the refusal question score as refused.
            return {"answer": REFUSAL_PHRASE}

        monkeypatch.setattr("src.evaluator.load_retrieval_context", fake_load_retrieval_context)
        monkeypatch.setattr("src.evaluator.retrieve", fake_retrieve)
        monkeypatch.setattr("src.evaluator.generate_with_sources", fake_generate_with_sources)

        result = run_eval(
            golden_path,
            top_k=6,
            results_path=str(results_path),
            provenance_fn=_fake_provenance,
        )

        assert context_calls == [CHROMA_PERSIST_DIR]  # built once, default dir
        assert result["retrieval"]["hits_strict"] == 1
        assert result["refusals"]["refused"] == 1


class TestTopKForwarding:
    """Pin: top_k must reach the retrieval and refusal passes, not silently
    default to 6 (agreed in an earlier gate review). IO-free — all injected."""

    def test_evaluate_retrieval_forwards_top_k(self):
        """evaluate_retrieval(..., top_k=4) calls retrieve_fn with top_k=4."""
        seen = {}

        def spy_retrieve(question, top_k=6):
            seen["top_k"] = top_k
            return [_result("1.1")]

        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["1.1"]}]

        evaluate_retrieval(golden, retrieve_fn=spy_retrieve, top_k=4)

        assert seen["top_k"] == 4

    def test_run_eval_forwards_top_k_to_both_passes(self, tmp_path):
        """run_eval(top_k=4) forwards top_k to the retrieval pass (observed on
        the retrieve spy) and routes the refusal pass through the injected
        answer_fn (observed by it being called) rather than the live default —
        so neither pass silently reverts to top_k=6 or hits real IO."""
        golden_path = TestRunEval()._golden_path(tmp_path)
        results_path = tmp_path / "results.md"

        retrieve_top_ks = []
        answer_questions = []

        def spy_retrieve(question, top_k=6):
            retrieve_top_ks.append(top_k)
            return [_result("14.8.5")]

        def spy_answer(question):
            answer_questions.append(question)
            return REFUSAL_PHRASE

        run_eval(
            golden_path,
            top_k=4,
            results_path=str(results_path),
            retrieve_fn=spy_retrieve,
            answer_fn=spy_answer,
            provenance_fn=_fake_provenance,
        )

        # One non-refusal question in the golden set -> exactly one retrieval
        # call, and it carried the forwarded top_k=4.
        assert retrieve_top_ks == [4]
        # The refusal pass ran via our injected spy (no live-API default path).
        assert answer_questions == ["What is the CGT rate?"]


class TestProvenanceReport:
    def test_collect_provenance_injected_into_report(self, tmp_path, capsys):
        """run_eval renders the injected provenance fields into the report."""
        golden_path = TestRunEval()._golden_path(tmp_path)
        results_path = tmp_path / "results.md"

        run_eval(
            golden_path,
            top_k=6,
            skip_refusals=True,
            results_path=str(results_path),
            retrieve_fn=lambda q, top_k=6: [_result("14.8.5")],
            provenance_fn=_fake_provenance,
        )

        content = results_path.read_text(encoding="utf-8")
        assert "## Provenance" in content
        assert "deadbee" in content  # git sha
        assert "clean" in content  # git_dirty False -> "clean"
        assert "42" in content  # chunk count
        assert "fake-embed-model" in content
        assert "fake-gen-model" in content
        # Per-type counts of the loaded golden set (1 direct, 1 refusal).
        assert "direct=1" in content
        assert "refusal=1" in content
        # Honesty label: the report must state the tuning set is NOT held-out,
        # so a reader never mistakes it for an out-of-sample benchmark.
        assert "NOT held-out" in content
        # The strict-vs-related definition sentence must spell out that nesting
        # counts in EITHER direction (parent or child), matching the symmetric
        # _sections_related; "either direction" is the load-bearing phrase.
        assert "either direction" in content

    def test_run_eval_threads_persist_directory_into_provenance(self, tmp_path, monkeypatch):
        """FIX 3: run_eval's DEFAULT provenance_fn (provenance_fn=None) must pass
        run_eval's persist_directory through to collect_provenance, so the chunk
        count is read from the index under test — not the hard-coded default
        dir. The store-opening seam (get_vector_store) must receive that
        directory. IO-free: git and the store are faked."""
        golden_path = TestRunEval()._golden_path(tmp_path)
        results_path = tmp_path / "results.md"
        seen_dirs = []

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(stdout="")
            raise AssertionError(f"unexpected git call: {cmd}")

        def fake_get_vector_store(persist_directory=None):
            seen_dirs.append(persist_directory)
            return SimpleNamespace(get=lambda include=None: {"ids": []})

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr("src.evaluator.get_vector_store", fake_get_vector_store)

        run_eval(
            golden_path,
            top_k=6,
            skip_refusals=True,
            results_path=str(results_path),
            retrieve_fn=lambda q, top_k=6: [_result("14.8.5")],
            persist_directory="/tmp/custom_pd",
        )

        # collect_provenance opened the store exactly once, at the run's dir.
        assert seen_dirs == ["/tmp/custom_pd"]


class TestCollectProvenance:
    """collect_provenance must NEVER raise — a real ``python -m src.pipeline
    eval`` would otherwise crash mid-report — and must populate its dynamic
    fields on the happy path. Both cases are fully IO-free: ``subprocess.run``
    and ``get_vector_store`` are patched, so no real git, store, or network is
    touched (CLAUDE.md test rule)."""

    def test_never_raises_when_all_io_fails(self, monkeypatch):
        """Git shelling out AND opening the store both blow up -> every dynamic
        field degrades to the literal "unavailable" and no exception escapes."""

        def boom(*args, **kwargs):
            raise RuntimeError("io down")

        monkeypatch.setattr("src.evaluator.subprocess.run", boom)
        monkeypatch.setattr("src.evaluator.get_vector_store", boom)

        # Must return normally (never raise), even pointed at a missing dir.
        prov = collect_provenance(persist_directory="/nonexistent")

        assert prov["git_sha"] == "unavailable"
        assert prov["git_dirty"] == "unavailable"
        assert prov["git_dirty_other"] == "unavailable"
        assert prov["chunk_count"] == "unavailable"
        # Static (import-sourced) fields are present regardless of IO failure.
        assert prov["embedding_model"] and prov["embedding_model"] != "unavailable"
        assert prov["generation_model"] and prov["generation_model"] != "unavailable"
        assert prov["matching"] and prov["matching"] != "unavailable"

    def test_happy_path_populates_dynamic_fields(self, monkeypatch):
        """Git returns a sha + clean status and the store returns three ids, so
        the dynamic fields carry those values (sha, git_dirty False, count 3)."""

        def fake_run(cmd, *args, **kwargs):
            # cmd is ["git", "rev-parse", "--short", "HEAD"] or
            # ["git", "status", "--porcelain"]; branch on the subcommand.
            # SimpleNamespace stands in for the CompletedProcess (only .stdout
            # is read); an empty status string means a clean tree.
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(stdout="")
            raise AssertionError(f"unexpected git call: {cmd}")

        class FakeStore:
            def get(self, include=None):
                # Count-only fetch: no documents/embeddings are loaded.
                assert include == []
                return {"ids": ["id0", "id1", "id2"]}

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: FakeStore(),
        )

        prov = collect_provenance(persist_directory="/nonexistent")

        assert prov["git_sha"] == "abc1234"
        assert prov["git_dirty"] is False
        assert prov["git_dirty_other"] == 0
        assert prov["chunk_count"] == 3
        # Static fields still present alongside the populated dynamic ones.
        assert prov["embedding_model"] and prov["embedding_model"] != "unavailable"
        assert prov["generation_model"] and prov["generation_model"] != "unavailable"
        assert prov["matching"] and prov["matching"] != "unavailable"

    def test_dirty_other_zero_when_only_excluded_path_dirty(self, monkeypatch):
        """Porcelain reports exactly one dirty file, the caller's own
        excluded (about-to-be-overwritten) report -> git_dirty True but
        git_dirty_other 0 (nothing dirty BESIDES the excluded path)."""

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(stdout=" M eval/results.md\n")
            raise AssertionError(f"unexpected git call: {cmd}")

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: SimpleNamespace(get=lambda include=None: {"ids": []}),
        )

        prov = collect_provenance(
            persist_directory="/nonexistent",
            exclude_paths=("eval/results.md",),
        )

        assert prov["git_dirty"] is True
        assert prov["git_dirty_other"] == 0

    def test_dirty_other_counts_non_excluded_dirty_files(self, monkeypatch):
        """Two dirty files, only one excluded -> git_dirty_other counts just
        the remaining (non-excluded) one."""

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(
                    stdout=" M eval/results.md\n M src/foo.py\n"
                )
            raise AssertionError(f"unexpected git call: {cmd}")

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: SimpleNamespace(get=lambda include=None: {"ids": []}),
        )

        prov = collect_provenance(
            persist_directory="/nonexistent",
            exclude_paths=("eval/results.md",),
        )

        assert prov["git_dirty"] is True
        assert prov["git_dirty_other"] == 1

    def test_empty_porcelain_is_clean_and_zero_other(self, monkeypatch):
        """No dirty paths at all -> git_dirty False, git_dirty_other 0, no
        ZeroDivisionError-style edge case (just an empty set to check)."""

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(stdout="")
            raise AssertionError(f"unexpected git call: {cmd}")

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: SimpleNamespace(get=lambda include=None: {"ids": []}),
        )

        prov = collect_provenance(persist_directory="/nonexistent")

        assert prov["git_dirty"] is False
        assert prov["git_dirty_other"] == 0

    def test_rename_line_parsed_by_new_path(self, monkeypatch):
        """A rename porcelain line ('R  old -> new') is parsed by its
        right-hand (new/current) path, not the raw line or the old path --
        excluding the new path zeroes out git_dirty_other."""

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(stdout="abc1234\n")
            if "status" in cmd:
                return SimpleNamespace(
                    stdout="R  old_name.py -> eval/results.md\n"
                )
            raise AssertionError(f"unexpected git call: {cmd}")

        monkeypatch.setattr("src.evaluator.subprocess.run", fake_run)
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: SimpleNamespace(get=lambda include=None: {"ids": []}),
        )

        prov = collect_provenance(
            persist_directory="/nonexistent",
            exclude_paths=("eval/results.md",),
        )

        assert prov["git_dirty"] is True
        assert prov["git_dirty_other"] == 0


class TestShaLineRendering:
    """The report's sha line must disambiguate "clean", "dirty only because
    of the report we're about to overwrite", and "dirty for other reasons
    too" -- driven end-to-end through run_eval with an injected fake
    provenance dict (IO-free: no real git, no real store)."""

    def _provenance(self, **overrides):
        prov = _fake_provenance()
        prov.update(overrides)
        return prov

    def _report_text(self, tmp_path, provenance):
        golden_path = TestRunEval()._golden_path(tmp_path)
        results_path = tmp_path / "results.md"

        run_eval(
            golden_path,
            top_k=6,
            skip_refusals=True,
            results_path=str(results_path),
            retrieve_fn=lambda q, top_k=6: [_result("14.8.5")],
            provenance_fn=lambda: provenance,
        )

        return results_path.read_text(encoding="utf-8")

    def test_sha_line_clean_when_not_dirty(self, tmp_path):
        provenance = self._provenance(git_dirty=False, git_dirty_other=0)

        content = self._report_text(tmp_path, provenance)

        assert "git sha: deadbee (clean)" in content
        assert "apart" not in content

    def test_sha_line_clean_apart_from_report_when_only_report_dirty(self, tmp_path):
        provenance = self._provenance(git_dirty=True, git_dirty_other=0)

        content = self._report_text(tmp_path, provenance)

        assert (
            "git sha: deadbee (clean apart from this generated report)" in content
        )

    def test_sha_line_dirty_with_other_files_when_others_dirty(self, tmp_path):
        provenance = self._provenance(git_dirty=True, git_dirty_other=3)

        content = self._report_text(tmp_path, provenance)

        assert "git sha: deadbee (dirty: 3 file(s) beyond this report)" in content

    def test_sha_line_degrades_when_git_dirty_unavailable(self, tmp_path):
        """git_dirty itself is "unavailable" (git not usable at all) -> the
        sha line renders that string as-is and run_eval does not raise,
        regardless of what git_dirty_other holds."""
        provenance = self._provenance(
            git_sha="unavailable", git_dirty="unavailable", git_dirty_other="unavailable"
        )

        content = self._report_text(tmp_path, provenance)

        assert "git sha: unavailable (unavailable)" in content

    def test_sha_line_falls_back_to_plain_dirty_when_other_not_an_int(self, tmp_path):
        """git_dirty is True but git_dirty_other is unavailable (the
        subprocess call that would populate it raised) -- degrade to the
        old plain "(dirty)" rather than crash on a non-int count."""
        provenance = self._provenance(git_dirty=True, git_dirty_other="unavailable")

        content = self._report_text(tmp_path, provenance)

        assert "git sha: deadbee (dirty)" in content
        assert "apart" not in content
        assert "beyond this report" not in content


class TestRunEvalMatrix:
    """The Phase 10 matrix runner (D38): sets × modes retrieval, ONE shared
    generation pass under the gating contract, refusals + completeness + judge
    off the cache, canonical guard, atomic write, D30-safe report. Every test is
    IO-free: retrieve_fn_factory / generate_fn / judge_fn / provenance_fn are
    injected, and the results-path constants are monkeypatched to tmp files so a
    test can NEVER touch the real committed eval/results.md."""

    def _prov(self):
        return {
            "git_sha": "abc1234",
            "git_dirty": False,
            "git_dirty_other": 0,
            "chunk_count": 10,
            "embedding_model": "em",
            "generation_model": "gm",
            "matching": "strict = exact; related = nested",
        }

    def _golden_entries(self):
        return [
            {"question": "D1", "type": "direct", "expected_sections": ["3.2"]},
            {"question": "E1", "type": "exact_token", "expected_sections": ["5.1"]},
            {"question": "R1", "type": "refusal", "expected_sections": []},
        ]

    def _retrieve_factory(self):
        """A mode->retrieve_fn factory that hits D1 (3.2) and E1 (5.1)."""

        def factory(mode):
            def _fn(question, top_k=6):
                if question == "D1":
                    return [{"document": _matrix_doc("3.2"), "score": 1.0, "metadata": _matrix_doc("3.2").metadata}]
                if question == "E1":
                    return [{"document": _matrix_doc("5.1"), "score": 1.0, "metadata": _matrix_doc("5.1").metadata}]
                return []

            return _fn

        return factory

    def _generate_fn(self, counter):
        """A generate_fn recording each call; verified answers for D1/E1, a
        refusal for R1."""

        def gen(question):
            counter.append(question)
            if question == "R1":
                return {
                    "answer": REFUSAL_PHRASE,
                    "citations": [],
                    "sources": [],
                    "source_documents": [],
                    "citation_check": {"grounded": [], "ungrounded": []},
                    "gate_outcome": REFUSAL,
                }
            sec = "3.2" if question == "D1" else "5.1"
            doc = _matrix_doc(sec)
            cite = _cite(sec, 5)
            return {
                "answer": f"A claim [Handbook, para {sec}, p.5].",
                "citations": [cite],
                "sources": [cite["raw"]],
                "source_documents": [doc],
                "citation_check": {"grounded": [cite], "ungrounded": []},
                "gate_outcome": CITATIONS_VERIFIED,
            }

        return gen

    def _patch_paths(self, monkeypatch, tmp_path):
        default = str(tmp_path / "results.md")
        partial = str(tmp_path / "results_partial.md")
        monkeypatch.setattr("src.evaluator.DEFAULT_RESULTS_PATH", default)
        monkeypatch.setattr("src.evaluator.PARTIAL_RESULTS_PATH", partial)
        return default, partial

    # --- the blocker test --------------------------------------------------
    def test_blocker_zero_generate_calls_when_both_skipped_no_judge(self, tmp_path, monkeypatch):
        """Both passes skipped + judge off => the generation pass does not run,
        so generate_fn is NEVER called (protects the offline ablation preview
        AND keyless CI, where a generation call would RAISE, not just cost)."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        calls = []

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn(calls),
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=True,
            judge=False,
        )

        assert calls == []  # zero generation calls
        assert result["generation_ran"] is False
        assert result["include_types"] == []
        # Not canonical (skipped passes) -> partial path.
        assert result["results_path"].endswith("results_partial.md")

    # --- include_types gating ---------------------------------------------
    def test_refusals_only_generates_refusal_type(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "s.jsonl", self._golden_entries())
        calls = []

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn(calls),
            provenance_fn=self._prov,
            skip_refusals=False,
            skip_completeness=True,
            judge=False,
        )

        assert result["include_types"] == ["refusal"]
        assert calls == ["R1"]
        assert result["sets"][0]["refusals"]["refused"] == 1
        assert result["sets"][0]["completeness"] is None

    def test_completeness_only_generates_in_corpus(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "s.jsonl", self._golden_entries())
        calls = []

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn(calls),
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=False,
            judge=False,
        )

        assert result["include_types"] == ["direct", "exact_token"]
        assert sorted(calls) == ["D1", "E1"]
        assert result["sets"][0]["refusals"] is None
        assert result["sets"][0]["completeness"]["total"] == 2

    def test_judge_forces_in_corpus_generation(self, tmp_path, monkeypatch):
        """judge on with both passes skipped still generates in-corpus answers
        (the judge needs them), but not refusal answers."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "s.jsonl", self._golden_entries())
        calls = []

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn(calls),
            judge_fn=lambda v: json.dumps({"claims": [{"claim": "c", "verdict": "supported"}]}),
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=True,
            judge=True,
        )

        assert result["include_types"] == ["direct", "exact_token"]
        assert sorted(calls) == ["D1", "E1"]
        assert result["sets"][0]["judge"]["mean_faithfulness"] == pytest.approx(1.0)

    def test_generation_runs_once_per_included_question(self, tmp_path, monkeypatch):
        """A full run generates each included question exactly once; the refusal
        AND completeness passes both read that single cache (no re-generation)."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        calls = []

        run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn(calls),
            provenance_fn=self._prov,
        )

        # 3 questions (D1, E1, R1), each generated once — not once per pass.
        assert sorted(calls) == ["D1", "E1", "R1"]

    # --- canonical truth table --------------------------------------------
    def test_canonical_full_run_writes_default(self, tmp_path, monkeypatch):
        default, _partial = self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
        )

        assert result["is_canonical"] is True
        assert result["results_path"] == default
        assert os.path.exists(default)

    @pytest.mark.parametrize(
        "kwargs,label",
        [
            ({"modes": ["hybrid"]}, "not all 3 modes"),
            ({"skip_refusals": True}, "refusals skipped"),
            ({"skip_completeness": True}, "completeness skipped"),
            ({"top_k": 3}, "top_k != 6"),
        ],
    )
    def test_noncanonical_conditions_route_to_partial(self, tmp_path, monkeypatch, kwargs, label):
        _default, partial = self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
            **kwargs,
        )

        assert result["is_canonical"] is False, label
        assert result["results_path"] == partial, label

    def test_missing_heldout_label_is_noncanonical(self, tmp_path, monkeypatch):
        _default, partial = self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "golden_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("tuning", gp)],  # no held-out set
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
        )

        assert result["is_canonical"] is False
        assert result["results_path"] == partial

    def test_generation_error_makes_run_noncanonical(self, tmp_path, monkeypatch):
        _default, partial = self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        def flaky_gen(question):
            if question == "D1":
                raise RuntimeError("boom")  # persistent -> error row
            if question == "R1":
                return {"answer": REFUSAL_PHRASE, "citations": [], "sources": [],
                        "source_documents": [], "citation_check": {"grounded": [], "ungrounded": []},
                        "gate_outcome": REFUSAL}
            doc = _matrix_doc("5.1")
            cite = _cite("5.1", 5)
            return {"answer": "A [Handbook, para 5.1, p.5].", "citations": [cite], "sources": [],
                    "source_documents": [doc], "citation_check": {"grounded": [cite], "ungrounded": []},
                    "gate_outcome": CITATIONS_VERIFIED}

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=flaky_gen,
            provenance_fn=self._prov,
        )

        assert result["generation_errors"] == 1
        assert result["is_canonical"] is False
        assert result["results_path"] == partial

    # --- results-path guard integration -----------------------------------
    def test_partial_run_leaves_existing_default_byte_identical(self, tmp_path, monkeypatch):
        default, _partial = self._patch_paths(monkeypatch, tmp_path)
        # A prior canonical report already sits at the default path.
        original = "PRIOR CANONICAL REPORT — DO NOT CLOBBER\n"
        with open(default, "w", encoding="utf-8") as f:
            f.write(original)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
            skip_completeness=True,  # -> non-canonical -> partial path
        )

        assert open(default, encoding="utf-8").read() == original

    def test_explicit_default_on_partial_run_warns(self, tmp_path, monkeypatch, capsys):
        default, _partial = self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("held-out", gp)],
            results_path=default,  # explicit -> honored even though non-canonical
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
            skip_completeness=True,
        )

        assert result["results_path"] == default
        err = capsys.readouterr().err
        assert "NON-canonical" in err

    def test_results_path_equal_to_set_path_refused(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        with pytest.raises(ValueError, match="eval-set"):
            run_eval_matrix(
                [("held-out", gp)],
                results_path=gp,  # would overwrite the eval set with a report
                retrieve_fn_factory=self._retrieve_factory(),
                generate_fn=self._generate_fn([]),
                provenance_fn=self._prov,
            )

    def test_atomic_write_failure_preserves_original(self, tmp_path, monkeypatch):
        """os.replace failing mid-write leaves the prior committed report intact
        (temp file written, but the destination never swapped)."""
        default, _partial = self._patch_paths(monkeypatch, tmp_path)
        original = "PRIOR CANONICAL — MUST SURVIVE\n"
        with open(default, "w", encoding="utf-8") as f:
            f.write(original)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        def boom_replace(src, dst):
            raise RuntimeError("disk full during replace")

        monkeypatch.setattr("src.evaluator.os.replace", boom_replace)

        with pytest.raises(RuntimeError, match="disk full"):
            run_eval_matrix(
                [("held-out", gp)],
                retrieve_fn_factory=self._retrieve_factory(),
                generate_fn=self._generate_fn([]),
                provenance_fn=self._prov,
            )

        assert open(default, encoding="utf-8").read() == original

    # --- default factory: per-backend load-once + mode isolation ----------
    def _patch_backends(self, monkeypatch):
        """Patch the three backend loaders + retrieve; return call logs."""
        vector_calls, assert_calls, bm25_calls, retrieve_calls = [], [], [], []
        fake_store, fake_bm25 = object(), object()

        monkeypatch.setattr(
            "src.evaluator.assert_embedding_model",
            lambda pd: assert_calls.append(pd),
        )
        monkeypatch.setattr(
            "src.evaluator.get_vector_store",
            lambda persist_directory=None: (vector_calls.append(persist_directory) or fake_store),
        )
        monkeypatch.setattr(
            "src.evaluator.load_bm25_index",
            lambda pd: (bm25_calls.append(pd) or fake_bm25),
        )

        def fake_retrieve(question, top_k=6, persist_directory=None, vector_store=None,
                          bm25_index=None, mode="hybrid", strict_errors=False):
            retrieve_calls.append({"mode": mode, "store": vector_store, "bm25": bm25_index,
                                   "strict": strict_errors})
            return [{"document": _matrix_doc("3.2"), "score": 1.0, "metadata": _matrix_doc("3.2").metadata}]

        monkeypatch.setattr("src.evaluator.retrieve", fake_retrieve)
        return {
            "vector": vector_calls, "assert": assert_calls, "bm25": bm25_calls,
            "retrieve": retrieve_calls, "fake_store": fake_store, "fake_bm25": fake_bm25,
        }

    def test_default_factory_loads_each_backend_once_and_threads_mode(self, tmp_path, monkeypatch):
        """With no injected factory over all three modes: the vector store loads
        ONCE (with the manifest check) and the BM25 index loads ONCE, reused
        across the modes that need them, and every retrieve call carries mode= +
        strict_errors=True with only that mode's arm(s) injected."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        logs = self._patch_backends(monkeypatch)

        run_eval_matrix(
            [("held-out", gp)],
            generate_fn=self._generate_fn([]),  # inject gen so no real generation
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=True,
            judge=False,
        )

        assert len(logs["vector"]) == 1  # store built once, reused
        assert len(logs["assert"]) == 1  # manifest checked once
        assert len(logs["bm25"]) == 1    # sidecar loaded once
        by_mode = {c["mode"]: c for c in logs["retrieve"]}
        assert set(by_mode) == {"hybrid", "vector", "bm25"}
        assert by_mode["hybrid"]["store"] is logs["fake_store"]
        assert by_mode["hybrid"]["bm25"] is logs["fake_bm25"]
        # vector mode: only the vector arm injected.
        assert by_mode["vector"]["store"] is logs["fake_store"]
        assert by_mode["vector"]["bm25"] is None
        # bm25 mode: only the bm25 arm injected.
        assert by_mode["bm25"]["store"] is None
        assert by_mode["bm25"]["bm25"] is logs["fake_bm25"]
        for c in logs["retrieve"]:
            assert c["strict"] is True

    def test_bm25_only_matrix_never_opens_chroma_or_checks_manifest(self, tmp_path, monkeypatch):
        """A bm25-only matrix run (offline) must not open Chroma or run the
        embedding-model manifest check — the isolation retrieve() provides,
        preserved by the runner (codex finding 2)."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        logs = self._patch_backends(monkeypatch)

        run_eval_matrix(
            [("held-out", gp)],
            modes=["bm25"],
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=True,
            judge=False,
        )

        assert logs["vector"] == []  # get_vector_store never called
        assert logs["assert"] == []  # assert_embedding_model never called
        assert len(logs["bm25"]) == 1

    def test_vector_only_matrix_never_loads_bm25(self, tmp_path, monkeypatch):
        """A vector-only matrix run must not unpickle the BM25 sidecar."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        logs = self._patch_backends(monkeypatch)

        run_eval_matrix(
            [("held-out", gp)],
            modes=["vector"],
            provenance_fn=self._prov,
            skip_refusals=True,
            skip_completeness=True,
            judge=False,
        )

        assert logs["bm25"] == []  # load_bm25_index never called
        assert len(logs["vector"]) == 1
        assert len(logs["assert"]) == 1

    def test_results_path_alias_of_set_refused(self, tmp_path, monkeypatch):
        """An absolute/relative alias of an input set path is refused too, not
        just an identical spelling (codex finding 3)."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        # gp is absolute; pass a path that realpath-resolves to the same file
        # via a redundant '.' segment — a different string, same file.
        alias = os.path.join(os.path.dirname(gp), ".", os.path.basename(gp))
        assert alias != gp

        with pytest.raises(ValueError, match="eval-set"):
            run_eval_matrix(
                [("held-out", gp)],
                results_path=alias,
                retrieve_fn_factory=self._retrieve_factory(),
                generate_fn=self._generate_fn([]),
                provenance_fn=self._prov,
            )

    def test_negative_top_k_and_judge_sample_rejected(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        with pytest.raises(ValueError, match="top_k"):
            run_eval_matrix(
                [("held-out", gp)], top_k=-1,
                retrieve_fn_factory=self._retrieve_factory(),
                generate_fn=self._generate_fn([]), provenance_fn=self._prov,
            )
        with pytest.raises(ValueError, match="judge_sample"):
            run_eval_matrix(
                [("held-out", gp)], judge_sample=-1,
                retrieve_fn_factory=self._retrieve_factory(),
                generate_fn=self._generate_fn([]), provenance_fn=self._prov,
            )

    # --- report rendering --------------------------------------------------
    def test_two_set_report_rendering(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        tuning = _matrix_golden(tmp_path, "golden_set.jsonl", self._golden_entries())
        heldout = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("tuning", tuning), ("held-out", heldout)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            judge_fn=lambda v: json.dumps({"claims": [{"claim": "c", "verdict": "supported"}]}),
            provenance_fn=self._prov,
            judge=True,
        )

        report = open(result["results_path"], encoding="utf-8").read()
        # Both sets captioned, with their sha256s and honesty labels.
        assert "tuning (used to select fusion constants" in report
        assert "held-out (never tuned" in report
        for s in result["sets"]:
            assert s["sha256"] in report
        # Headline drawn from the held-out set.
        assert "Headline: strict hit@6 on the held-out set" in report
        assert "strict hit@6 =" in report
        # 3-mode ablation rows present for each set.
        assert report.count("| hybrid |") == 2
        assert report.count("| vector |") == 2
        assert report.count("| bm25 |") == 2
        # Judge section rendered (not the "not run" variant).
        assert "mean faithfulness" in report

    def test_judge_off_renders_not_run(self, tmp_path, monkeypatch):
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=self._retrieve_factory(),
            generate_fn=self._generate_fn([]),
            provenance_fn=self._prov,
            judge=False,
        )

        report = open(result["results_path"], encoding="utf-8").read()
        assert "Judge: not run." in report

    def test_d30_canaries_absent_from_report_present_in_dump(self, tmp_path, monkeypatch):
        """Secrets planted in chunk text, answer text, and judge claim text must
        never reach the committed report; the claim text lives ONLY in the
        gitignored judge dump."""
        self._patch_paths(monkeypatch, tmp_path)
        gp = _matrix_golden(tmp_path, "heldout_set.jsonl", self._golden_entries())
        dump = str(tmp_path / "judge_dump.jsonl")

        def factory(mode):
            def _fn(question, top_k=6):
                doc = _matrix_doc("3.2", text="SECRET-CHUNK-PROSE")
                if question in ("D1", "E1"):
                    return [{"document": doc, "score": 1.0, "metadata": doc.metadata}]
                return []
            return _fn

        def gen(question):
            if question == "R1":
                return {"answer": REFUSAL_PHRASE, "citations": [], "sources": [],
                        "source_documents": [], "citation_check": {"grounded": [], "ungrounded": []},
                        "gate_outcome": REFUSAL}
            sec = "3.2" if question == "D1" else "5.1"
            doc = _matrix_doc(sec, text="SECRET-CHUNK-PROSE")
            cite = _cite(sec, 5)
            return {"answer": f"SECRET-ANSWER-PROSE [Handbook, para {sec}, p.5].",
                    "citations": [cite], "sources": [], "source_documents": [doc],
                    "citation_check": {"grounded": [cite], "ungrounded": []},
                    "gate_outcome": CITATIONS_VERIFIED}

        result = run_eval_matrix(
            [("held-out", gp)],
            retrieve_fn_factory=factory,
            generate_fn=gen,
            judge_fn=lambda v: json.dumps({"claims": [{"claim": "SECRET-CLAIM-TEXT", "verdict": "supported"}]}),
            provenance_fn=self._prov,
            judge=True,
            judge_dump_path=dump,
        )

        report = open(result["results_path"], encoding="utf-8").read()
        assert "SECRET-CHUNK-PROSE" not in report
        assert "SECRET-ANSWER-PROSE" not in report
        assert "SECRET-CLAIM-TEXT" not in report
        # The claim text is captured in the gitignored dump for local review.
        assert "SECRET-CLAIM-TEXT" in open(dump, encoding="utf-8").read()
