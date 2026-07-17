"""Tests for the operational audit event log (src/audit.py).

No real subprocess or filesystem-outside-tmp_path calls: ``_git_sha`` is
always patched, and every ``log_event`` call is pointed at a ``tmp_path``
either via an explicit ``path`` argument or via ``AUDIT_LOG_PATH``.
"""

import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.documents import Document

from src.audit import DEFAULT_LOG_PATH, _git_sha, build_event, log_event
from src.embedder import compute_chunk_id
from src.generator import GENERATION_MODEL

EXPECTED_KEYS = {
    "timestamp",
    "git_sha",
    "query_sha256",
    "query_chars",
    "top_k",
    "document_type",
    "retrieved",
    "gate_outcome",
    "action",
    "verified_count",
    "unverified_count",
    "citation_locators",
    "generation_model",
    "answer_chars",
}


@pytest.fixture
def with_id_doc():
    """A retrieved chunk whose Document already carries an id."""
    return Document(
        page_content="The purchaser's solicitor confirms good marketable title.",
        metadata={"section_number": "14.8.5", "page_start": 412, "page_end": 412},
        id="preset-id-0001",
    )


@pytest.fixture
def without_id_doc():
    """A retrieved chunk whose Document has no id set — exercises the
    compute_chunk_id fallback. The page content is a distinctive sentinel
    used to assert chunk text never leaks into the record."""
    return Document(
        page_content="SENTINEL_CHUNK_TEXT: requisitions on title must be raised within the specified period.",
        metadata={"section_number": "1.2", "page_start": 1, "page_end": 2},
    )


@pytest.fixture
def results(with_id_doc, without_id_doc):
    return [
        {"document": with_id_doc, "score": 0.9, "metadata": with_id_doc.metadata},
        {"document": without_id_doc, "score": 0.5, "metadata": without_id_doc.metadata},
    ]


@pytest.fixture
def citation_check():
    return {
        "grounded": [{"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"}],
        "ungrounded": [{"para": "1.2", "page": "1", "raw": "para 1.2, p.1"}],
    }


@pytest.fixture
def citations(citation_check):
    return citation_check["grounded"] + citation_check["ungrounded"]


@pytest.fixture(autouse=True)
def patched_git_sha(monkeypatch):
    """Default a deterministic, patched git SHA for every test in this
    module unless a test overrides it itself."""
    monkeypatch.setattr("src.audit._git_sha", lambda: "abc1234")


class TestGitSha:
    """Direct coverage of the helper, with subprocess.run itself patched
    (no real subprocess calls)."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        """``_git_sha`` is lru_cached for the process lifetime; clear it
        around each test so the patched ``subprocess.run`` is actually
        consulted instead of a value cached by an earlier test."""
        _git_sha.cache_clear()
        yield
        _git_sha.cache_clear()

    def test_returns_short_sha_on_success(self, monkeypatch):
        class FakeResult:
            returncode = 0
            stdout = "abc1234\n"

        monkeypatch.setattr("src.audit.subprocess.run", lambda *a, **k: FakeResult())
        assert _git_sha() == "abc1234"

    def test_returns_none_on_nonzero_returncode(self, monkeypatch):
        class FakeResult:
            returncode = 128
            stdout = ""

        monkeypatch.setattr("src.audit.subprocess.run", lambda *a, **k: FakeResult())
        assert _git_sha() is None

    def test_returns_none_when_subprocess_raises(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("git not installed")

        monkeypatch.setattr("src.audit.subprocess.run", boom)
        assert _git_sha() is None


class TestBuildEventKeys:
    def test_returns_exactly_the_expected_keys(self, results, citation_check, citations):
        record = build_event(
            question="What is the priority period?",
            top_k=6,
            document_type="handbook",
            results=results,
            gate_outcome="PARTIALLY_VERIFIED",
            action="shown_with_warning",
            citation_check=citation_check,
            citations=citations,
            answer="The priority period is 30 days [para 14.8.5, p.412].",
        )
        assert set(record.keys()) == EXPECTED_KEYS

    def test_no_query_text_key_by_default(self, monkeypatch, results, citation_check, citations):
        monkeypatch.delenv("AUDIT_LOG_RAW_QUERIES", raising=False)
        record = build_event(
            question="Does a vendor need to disclose latent defects?",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="answer text",
        )
        assert "query_text" not in record


class TestBuildEventRetrieved:
    def test_retrieved_id_uses_document_id_when_set(self, results, citation_check, citations, with_id_doc):
        record = build_event(
            question="q",
            top_k=6,
            document_type="handbook",
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        assert record["retrieved"][0]["id"] == with_id_doc.id

    def test_retrieved_id_falls_back_to_compute_chunk_id(
        self, results, citation_check, citations, without_id_doc
    ):
        record = build_event(
            question="q",
            top_k=6,
            document_type="handbook",
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        source = without_id_doc.metadata.get("source", "")
        fallback_id = record["retrieved"][1]["id"]
        assert fallback_id == compute_chunk_id(source, without_id_doc.page_content)
        assert fallback_id == hashlib.sha256(
            (source + "\0" + without_id_doc.page_content).encode("utf-8")
        ).hexdigest()[:16]

    def test_retrieved_fields_pulled_from_metadata_and_score(
        self, results, citation_check, citations
    ):
        record = build_event(
            question="q",
            top_k=6,
            document_type="handbook",
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        first = record["retrieved"][0]
        assert first["section_number"] == "14.8.5"
        assert first["page_start"] == 412
        assert first["page_end"] == 412
        assert first["score"] == 0.9

    def test_retrieved_missing_metadata_defaults_to_none(self, citation_check, citations):
        bare_doc = Document(page_content="no metadata chunk here", metadata={})
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=[{"document": bare_doc, "score": 0.1, "metadata": {}}],
            gate_outcome=None,
            action="no_results",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        entry = record["retrieved"][0]
        assert entry["section_number"] is None
        assert entry["page_start"] is None
        assert entry["page_end"] is None


class TestBuildEventPassthroughAndCounts:
    def test_scalar_passthrough_fields(self, results, citation_check, citations):
        record = build_event(
            question="q",
            top_k=9,
            document_type="handbook",
            results=results,
            gate_outcome="PARTIALLY_VERIFIED",
            action="shown_with_warning",
            citation_check=citation_check,
            citations=citations,
            answer="answer",
        )
        assert record["top_k"] == 9
        assert record["document_type"] == "handbook"
        assert record["gate_outcome"] == "PARTIALLY_VERIFIED"
        assert record["action"] == "shown_with_warning"
        assert record["generation_model"] == GENERATION_MODEL
        assert record["answer_chars"] == len("answer")

    def test_verified_and_unverified_counts(self, results, citation_check, citations):
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="PARTIALLY_VERIFIED",
            action="shown_with_warning",
            citation_check=citation_check,
            citations=citations,
            answer="answer",
        )
        assert record["verified_count"] == 1
        assert record["unverified_count"] == 1

    def test_citation_locators_are_raw_strings(self, results, citation_check, citations):
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="PARTIALLY_VERIFIED",
            action="shown_with_warning",
            citation_check=citation_check,
            citations=citations,
            answer="answer",
        )
        assert record["citation_locators"] == ["para 14.8.5, p.412", "para 1.2, p.1"]

    def test_git_sha_present_when_available(self, results, citation_check, citations):
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome=None,
            action="no_results",
            citation_check=citation_check,
            citations=citations,
            answer="",
        )
        assert record["git_sha"] == "abc1234"


class TestBuildEventGitShaFailure:
    def test_survives_git_sha_raising(self, monkeypatch, results, citation_check, citations):
        def boom():
            raise RuntimeError("git not found")

        monkeypatch.setattr("src.audit._git_sha", boom)
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome=None,
            action="no_results",
            citation_check=citation_check,
            citations=citations,
            answer="",
        )
        assert record["git_sha"] is None


class TestQueryHashing:
    def test_query_sha256_and_chars(self, results, citation_check, citations):
        question = "Must a vendor disclose latent defects in the title?"
        record = build_event(
            question=question,
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="answer",
        )
        assert record["query_sha256"] == hashlib.sha256(question.encode("utf-8")).hexdigest()
        assert record["query_chars"] == len(question)

    def test_raw_query_text_included_only_with_env_opt_in(
        self, monkeypatch, results, citation_check, citations
    ):
        question = "Does the lease at a specific address permit assignment?"
        monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        record = build_event(
            question=question,
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="answer",
        )
        assert record["query_text"] == question


class TestBuildEventRewrites:
    """Phase 13 (D43): build_event's keyword-only rewrites/rewrite_status."""

    def test_omitted_when_both_rewrite_args_are_none(
        self, results, citation_check, citations
    ):
        """Backward compat: passing neither rewrites nor rewrite_status
        produces a byte-identical event to before this feature existed — the
        three new fields are entirely absent, not present-with-None."""
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        assert set(record.keys()) == EXPECTED_KEYS
        assert "rewrite_status" not in record
        assert "rewrite_count" not in record
        assert "rewrite_sha256s" not in record
        assert "rewrite_texts" not in record

    def test_rewrite_fields_present_with_correct_hashes_no_raw_text(
        self, monkeypatch, results, citation_check, citations
    ):
        monkeypatch.delenv("AUDIT_LOG_RAW_QUERIES", raising=False)
        rewrites = ["formal rephrasing", "keyword variant"]
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
            rewrites=rewrites,
            rewrite_status="live",
        )
        assert record["rewrite_status"] == "live"
        assert record["rewrite_count"] == 2
        assert record["rewrite_sha256s"] == [
            hashlib.sha256(r.encode("utf-8")).hexdigest() for r in rewrites
        ]
        assert "rewrite_texts" not in record

    def test_rewrite_status_alone_still_populates_all_three_fields(
        self, results, citation_check, citations
    ):
        """rewrite_status given with rewrites omitted (None) still adds all
        three fields — the omit rule is "both None", not "either None"."""
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome=None,
            action="no_results",
            citation_check=citation_check,
            citations=citations,
            answer="",
            rewrite_status="disabled",
        )
        assert record["rewrite_status"] == "disabled"
        assert record["rewrite_count"] == 0
        assert record["rewrite_sha256s"] == []

    def test_raw_rewrite_texts_included_only_with_env_opt_in(
        self, monkeypatch, results, citation_check, citations
    ):
        monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        rewrites = ["formal rephrasing", "keyword variant"]
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
            rewrites=rewrites,
            rewrite_status="live",
        )
        assert record["rewrite_texts"] == rewrites


class TestBuildEventIntent:
    """Phase 14 (D50): build_event's keyword-only ``intent_rewrite``. When
    ``None`` neither intent key appears (byte-identical to pre-Phase-14); when
    present, ``intent_rewrite_sha256`` is always recorded and the raw
    ``intent_rewrite_text`` is gated behind AUDIT_LOG_RAW_QUERIES. conftest
    scrubs that env var suite-wide, so the no-leak default needs no monkeypatch."""

    def _event(self, results, citation_check, citations, **extra):
        return build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
            **extra,
        )

    def test_intent_none_adds_no_intent_keys(self, results, citation_check, citations):
        """intent_rewrite=None (alongside the Phase-13 rewrite fields) must add
        NEITHER intent key — the event shape is byte-identical to a pre-Phase-14
        rewrite-only record."""
        record = self._event(
            results, citation_check, citations,
            rewrites=["formal rephrasing"],
            rewrite_status="live",
            intent_rewrite=None,
        )
        assert "intent_rewrite_sha256" not in record
        assert "intent_rewrite_text" not in record

    def test_intent_sha256_present_no_raw_text_by_default(
        self, results, citation_check, citations
    ):
        intent = "the underlying comparison of A and B"
        record = self._event(
            results, citation_check, citations,
            rewrites=["formal rephrasing"],
            rewrite_status="live",
            intent_rewrite=intent,
        )
        assert record["intent_rewrite_sha256"] == hashlib.sha256(
            intent.encode("utf-8")
        ).hexdigest()
        assert "intent_rewrite_text" not in record  # no leak without the env flag

    def test_intent_raw_text_included_only_with_env_opt_in(
        self, monkeypatch, results, citation_check, citations
    ):
        monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        intent = "the underlying comparison of A and B"
        record = self._event(
            results, citation_check, citations,
            rewrites=["formal rephrasing"],
            rewrite_status="live",
            intent_rewrite=intent,
        )
        assert record["intent_rewrite_text"] == intent

    def test_intent_key_absent_from_default_event_keyset(
        self, results, citation_check, citations
    ):
        """With neither rewrites nor intent passed, the record carries exactly
        the pre-Phase-13 key set — no intent keys sneak in."""
        record = build_event(
            question="q",
            top_k=6,
            document_type=None,
            results=results,
            gate_outcome="CITATIONS_VERIFIED",
            action="shown",
            citation_check=citation_check,
            citations=citations,
            answer="a",
        )
        assert set(record.keys()) == EXPECTED_KEYS


class TestNoLeakedText:
    def test_answer_and_chunk_text_absent_from_serialized_record(
        self, results, citation_check, citations, without_id_doc
    ):
        answer = "SENTINEL_ANSWER_TEXT: the priority period is 30 days [para 14.8.5, p.412]."
        record = build_event(
            question="q",
            top_k=6,
            document_type="handbook",
            results=results,
            gate_outcome="PARTIALLY_VERIFIED",
            action="shown_with_warning",
            citation_check=citation_check,
            citations=citations,
            answer=answer,
        )
        serialized = json.dumps(record)
        assert answer not in serialized
        assert "SENTINEL_ANSWER_TEXT" not in serialized
        assert without_id_doc.page_content not in serialized
        assert "SENTINEL_CHUNK_TEXT" not in serialized


class TestLogEvent:
    def test_appends_two_lines_in_order_and_each_line_parses(self, tmp_path):
        path = tmp_path / "sub" / "audit_log.jsonl"
        record1 = {"a": 1}
        record2 = {"a": 2}
        log_event(record1, path=path)
        log_event(record2, path=path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == record1
        assert json.loads(lines[1]) == record2

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "audit_log.jsonl"
        assert not path.parent.exists()
        log_event({"x": 1}, path=path)
        assert path.exists()

    def test_env_path_override_used_when_no_explicit_path(self, tmp_path, monkeypatch):
        env_path = tmp_path / "env_dir" / "audit_log.jsonl"
        monkeypatch.setenv("AUDIT_LOG_PATH", str(env_path))
        log_event({"y": 2})
        assert env_path.exists()
        assert json.loads(env_path.read_text(encoding="utf-8").strip()) == {"y": 2}

    def test_explicit_path_takes_precedence_over_env(self, tmp_path, monkeypatch):
        env_path = tmp_path / "env_only" / "audit_log.jsonl"
        explicit_path = tmp_path / "explicit" / "audit_log.jsonl"
        monkeypatch.setenv("AUDIT_LOG_PATH", str(env_path))
        log_event({"z": 3}, path=explicit_path)
        assert explicit_path.exists()
        assert not env_path.exists()

    def test_default_log_path_constant(self):
        assert DEFAULT_LOG_PATH == Path("logs/audit_log.jsonl")
