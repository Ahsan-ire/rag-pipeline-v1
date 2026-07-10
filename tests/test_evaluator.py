"""Tests for the Phase 5 evaluation harness (src/evaluator.py).

Everything is mocked: no network, no API calls, no real vector store. Fake
``retrieve_fn``/``answer_fn`` stand in for src.retriever.retrieve and the
generation path, matching the ``[{"document": Document, "score": float,
"metadata": dict}, ...]`` shape retrieve() actually returns.
"""

import json

import pytest
from langchain_core.documents import Document

from src.evaluator import (
    evaluate_refusals,
    evaluate_retrieval,
    load_golden_set,
    run_eval,
)
from src.generator import REFUSAL_PHRASE


def _write_jsonl(path, lines):
    """Write raw text lines (already JSON-encoded, or blank) to a JSONL file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _result(section_number):
    """Build one fake retrieve() result carrying only a section_number."""
    doc = Document(page_content="", metadata={"section_number": section_number})
    return {"document": doc, "score": 1.0, "metadata": doc.metadata}


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
    def test_exact_section_match_is_a_hit(self):
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.8.5"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.8.5")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits"] == 1
        assert report["total"] == 1
        assert report["hit_rate"] == 1.0
        assert report["per_question"][0]["hit"] is True

    def test_nested_section_is_a_hit(self):
        """Expected 14.12 vs a retrieved sub-paragraph 14.12.1 still counts."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.12"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.12.1")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits"] == 1
        assert report["per_question"][0]["hit"] is True

    def test_sibling_section_is_a_miss(self):
        """Expected 14.1 vs retrieved 14.12 do not nest -> miss."""
        golden = [{"question": "Q1", "type": "direct", "expected_sections": ["14.1"]}]
        fake_retrieve = lambda q, top_k=6: [_result("14.12")]

        report = evaluate_retrieval(golden, retrieve_fn=fake_retrieve)

        assert report["hits"] == 0
        assert report["per_question"][0]["hit"] is False
        assert report["hit_rate"] == 0.0

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

        assert report["hits"] == 2
        assert report["total"] == 3
        assert report["hit_rate"] == pytest.approx(2 / 3)
        assert report["by_type"]["direct"] == {"hits": 1, "total": 2, "hit_rate": 0.5}
        assert report["by_type"]["exact_token"] == {"hits": 1, "total": 1, "hit_rate": 1.0}

    def test_hit_rate_zero_when_no_questions(self):
        """All-refusal golden set -> zero non-refusal questions -> hit_rate 0.0, no ZeroDivisionError."""
        golden = [{"question": "Q1", "type": "refusal", "expected_sections": []}]

        report = evaluate_retrieval(golden, retrieve_fn=lambda q, top_k=6: [])

        assert report["total"] == 0
        assert report["hits"] == 0
        assert report["hit_rate"] == 0.0
        assert report["by_type"] == {}


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
            doc = Document(
                page_content=secret_chunk_text,
                metadata={"section_number": "14.8.5"},
            )
            return [{"document": doc, "score": 1.0, "metadata": doc.metadata}]

        def fake_answer(question):
            return f"{secret_chunk_text} {REFUSAL_PHRASE}"

        result = run_eval(
            golden_path,
            top_k=6,
            results_path=str(results_path),
            retrieve_fn=fake_retrieve,
            answer_fn=fake_answer,
        )

        assert results_path.exists()
        content = results_path.read_text(encoding="utf-8")
        assert "hit rate" in content.lower()
        assert secret_chunk_text not in content

        captured = capsys.readouterr()
        assert secret_chunk_text not in captured.out

        assert result["retrieval"]["hits"] == 1
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
        )

        captured = capsys.readouterr()
        assert "skipped" in captured.out
        assert result["refusals"] is None
        assert "skipped" in results_path.read_text(encoding="utf-8")
