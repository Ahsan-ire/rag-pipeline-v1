"""Tests for the pre-retrieval query-expansion module (src/query_rewrite.py).

conftest.py's autouse `_no_live_api_key` fixture scrubs ANTHROPIC_API_KEY
before every test, so the no-key path needs no explicit monkeypatch — and
any test that forgets to patch `_invoke_rewrite`/`get_rewrite_llm` fails
loudly (ValueError) instead of silently making a real API call.
"""

import dataclasses
from unittest.mock import patch

import pytest

from src.query_rewrite import (
    Expansion,
    MAX_REWRITE_CHARS,
    MAX_REWRITES,
    REWRITE_MODEL,
    STATUS_API_ERROR,
    STATUS_DISABLED,
    STATUS_LIVE,
    STATUS_NO_KEY,
    STATUS_PARSE_ERROR,
    expand_query,
    parse_rewrites,
)


class TestParseRewrites:
    def test_numbered_with_periods(self):
        text = "1. formal rephrasing\n2. keyword variant\n3. plain paraphrase"
        assert parse_rewrites(text) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_numbered_with_parens(self):
        text = "1) formal rephrasing\n2) keyword variant\n3) plain paraphrase"
        assert parse_rewrites(text) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_bulleted(self):
        text = "- formal rephrasing\n* keyword variant\n• plain paraphrase"
        assert parse_rewrites(text) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_preamble_junk_before_numbers_is_ignored(self):
        """A non-marked line ahead of a numbered list is discarded, not
        harvested — the defensive plain-line fallback only applies when NO
        line in the whole response carries a marker."""
        text = (
            "Here are three alternative search queries:\n"
            "1. formal rephrasing\n"
            "2. keyword variant\n"
            "3. plain paraphrase"
        )
        result = parse_rewrites(text)
        assert result == ["formal rephrasing", "keyword variant", "plain paraphrase"]
        assert not any("Here are three" in r for r in result)

    def test_wrapping_quotes_stripped(self):
        text = '1. "formal rephrasing"\n2. \'keyword variant\'\n3. plain paraphrase'
        assert parse_rewrites(text) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_casefold_duplicates_dropped(self):
        text = "1. Registration Of Title\n2. registration of title\n3. a distinct one"
        assert parse_rewrites(text) == ["Registration Of Title", "a distinct one"]

    def test_overlong_line_dropped(self):
        long_line = "x" * (MAX_REWRITE_CHARS + 1)
        text = f"1. {long_line}\n2. a normal length rewrite"
        assert parse_rewrites(text) == ["a normal length rewrite"]

    def test_empty_text_returns_empty_list(self):
        assert parse_rewrites("") == []

    def test_no_numbered_lines_falls_back_to_plain_lines(self):
        """Defensive path: the model skipped numbering entirely."""
        text = "a plain rewrite\nanother plain rewrite"
        assert parse_rewrites(text) == ["a plain rewrite", "another plain rewrite"]

    def test_caps_at_max_rewrites(self):
        text = "\n".join(
            f"{i}. rewrite number {i}" for i in range(1, MAX_REWRITES + 3)
        )
        assert len(parse_rewrites(text)) == MAX_REWRITES

    def test_marker_only_lines_yield_no_rewrites(self):
        """Fallback path (gate fix): a degenerate "1.\\n2.\\n3." response is
        markers with no content — no line matches the marker+content regex, so
        the fallback strips each leading marker to "" and drops it. The result
        is ZERO rewrites, NOT the three literal "1." strings the old
        no-marked-line fallback harvested."""
        assert parse_rewrites("1.\n2.\n3.") == []

    def test_bullet_only_lines_yield_no_rewrites(self):
        """Same fallback path with bulleted markers: "-\\n*\\n•" → zero rewrites."""
        assert parse_rewrites("-\n*\n•") == []

    def test_no_alpha_numbered_candidate_dropped(self):
        """Numbered path (gate fix): a numbered line whose content carries NO
        alphabetic character (bare digits) is dropped — it is not a usable
        search query — while a real rewrite alongside it survives."""
        text = "1. 2024\n2. registration of unregistered land"
        assert parse_rewrites(text) == ["registration of unregistered land"]


class TestExpandQuery:
    def test_disabled_skips_llm_entirely(self, monkeypatch):
        """enabled=False must never call get_rewrite_llm."""

        def boom(*args, **kwargs):
            raise AssertionError("get_rewrite_llm must not be called when disabled")

        monkeypatch.setattr("src.query_rewrite.get_rewrite_llm", boom)
        result = expand_query("What is a priority entry?", enabled=False)

        assert result == Expansion(
            "What is a priority entry?", (), REWRITE_MODEL, STATUS_DISABLED
        )

    def test_no_key_path(self):
        """No API key (conftest's autouse fixture scrubs it) -> STATUS_NO_KEY,
        empty rewrites, and get_rewrite_llm's own ValueError is swallowed."""
        result = expand_query("What is a priority entry?", llm=None)

        assert result.status == STATUS_NO_KEY
        assert result.rewrites == ()
        assert result.original == "What is a priority entry?"
        assert result.model == REWRITE_MODEL

    def test_happy_path_live(self):
        canned = "1. formal rephrasing\n2. keyword variant\n3. plain paraphrase"
        with patch("src.query_rewrite._invoke_rewrite", return_value=canned):
            result = expand_query("What is a priority entry?", llm=object())

        assert result.status == STATUS_LIVE
        assert result.rewrites == (
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        )

    def test_rewrite_matching_question_is_excluded(self):
        """A rewrite that is a casefold-duplicate of the original question is
        dropped from the effective rewrites."""
        question = "What is a priority entry?"
        canned = f"1. {question.upper()}\n2. keyword variant\n3. plain paraphrase"
        with patch("src.query_rewrite._invoke_rewrite", return_value=canned):
            result = expand_query(question, llm=object())

        assert result.status == STATUS_LIVE
        assert result.rewrites == ("keyword variant", "plain paraphrase")
        assert all(r.casefold() != question.casefold() for r in result.rewrites)

    def test_api_error(self):
        with patch(
            "src.query_rewrite._invoke_rewrite", side_effect=RuntimeError("boom")
        ):
            result = expand_query("What is a priority entry?", llm=object())

        assert result.status == STATUS_API_ERROR
        assert result.rewrites == ()

    def test_parse_error_on_unparseable_prose(self):
        """A single unmarked paragraph with no newlines: no numbered/bulleted
        line exists (so the defensive plain-line fallback is tried), but the
        one "line" is over MAX_REWRITE_CHARS and is dropped, leaving zero
        effective rewrites from a non-empty response."""
        unparseable = "word " * 60  # well over MAX_REWRITE_CHARS, single line
        assert len(unparseable) > MAX_REWRITE_CHARS
        with patch("src.query_rewrite._invoke_rewrite", return_value=unparseable):
            result = expand_query("What is a priority entry?", llm=object())

        assert result.status == STATUS_PARSE_ERROR
        assert result.rewrites == ()

    def test_empty_response_is_parse_error_not_live(self):
        """An empty/whitespace-only LLM response must degrade to
        STATUS_PARSE_ERROR (zero rewrites), never STATUS_LIVE — a degenerate
        expansion that reads as "live" would let an inert rewrite model be
        counted as a successful expansion in the canonical eval accounting."""
        with patch("src.query_rewrite._invoke_rewrite", return_value="   \n\t  "):
            result = expand_query("What is a priority entry?", llm=object())

        assert result.status == STATUS_PARSE_ERROR
        assert result.rewrites == ()

    def test_marker_only_response_is_parse_error(self):
        """Marker-only model output ("1.\\n2.\\n3.") yields zero effective
        rewrites, so expand_query degrades to STATUS_PARSE_ERROR — never
        STATUS_LIVE. A degenerate expansion that read as "live" would satisfy
        the canonical eval's zero-fallback gate with no real rewrites (D46)."""
        with patch("src.query_rewrite._invoke_rewrite", return_value="1.\n2.\n3."):
            result = expand_query("What is a priority entry?", llm=object())

        assert result.status == STATUS_PARSE_ERROR
        assert result.rewrites == ()

    def test_constructor_runtimeerror_is_api_error(self, monkeypatch):
        """A RuntimeError raised while BUILDING the client (llm=None path) must
        degrade to STATUS_API_ERROR, never escape — the never-raises contract
        covers construction failures, not only ValueError. A key is set so the
        explicit missing-key check passes and control reaches the constructor."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        def boom():
            raise RuntimeError("client construction blew up")

        monkeypatch.setattr("src.query_rewrite.get_rewrite_llm", boom)
        result = expand_query("What is a priority entry?", llm=None)

        assert result.status == STATUS_API_ERROR
        assert result.rewrites == ()

    def test_key_present_but_constructor_valueerror_is_api_error(self, monkeypatch):
        """With a key set, an UNRELATED ValueError from the constructor must be
        STATUS_API_ERROR, not STATUS_NO_KEY: the no-key case is decided by an
        explicit env check BEFORE construction, so a constructor ValueError can
        no longer be mislabeled as a missing key."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        def boom():
            raise ValueError("some unrelated config problem, not the key")

        monkeypatch.setattr("src.query_rewrite.get_rewrite_llm", boom)
        result = expand_query("What is a priority entry?", llm=None)

        assert result.status == STATUS_API_ERROR
        assert result.rewrites == ()

    def test_expansion_is_frozen(self):
        exp = Expansion("q", (), REWRITE_MODEL, STATUS_DISABLED)
        with pytest.raises(dataclasses.FrozenInstanceError):
            exp.status = STATUS_LIVE
