"""Tests for the Phase 5 evaluation harness (src/evaluator.py).

Everything is mocked: no network, no API calls, no real vector store. Fake
``retrieve_fn``/``answer_fn`` stand in for src.retriever.retrieve and the
generation path, matching the ``[{"document": Document, "score": float,
"metadata": dict}, ...]`` shape retrieve() actually returns.
"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.evaluator import (
    collect_provenance,
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


def _fake_provenance():
    """A canned provenance dict for run_eval tests.

    The real ``collect_provenance`` shells out to git and opens the Chroma
    store; injecting this keeps the test suite IO-free (CLAUDE.md: no network,
    no real vector store). Shape matches ``collect_provenance``'s return.
    """
    return {
        "git_sha": "deadbee",
        "git_dirty": False,
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
        assert prov["chunk_count"] == 3
        # Static fields still present alongside the populated dynamic ones.
        assert prov["embedding_model"] and prov["embedding_model"] != "unavailable"
        assert prov["generation_model"] and prov["generation_model"] != "unavailable"
        assert prov["matching"] and prov["matching"] != "unavailable"
