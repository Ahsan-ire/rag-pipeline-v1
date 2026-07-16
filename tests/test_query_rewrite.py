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
    MAX_INTENT_CHARS,
    MAX_REWRITE_CHARS,
    MAX_REWRITES,
    REWRITE_MODEL,
    STATUS_API_ERROR,
    STATUS_DISABLED,
    STATUS_LIVE,
    STATUS_NO_KEY,
    STATUS_PARSE_ERROR,
    expand_query,
    extract_intent,
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

    def test_positional_construction_without_intent_defaults_none(self):
        """Phase 14 (D50): the 4-field positional construction every failure
        return in expand_query and the evaluator use stays valid and defaults
        intent_rewrite to None."""
        exp = Expansion("q", (), REWRITE_MODEL, STATUS_DISABLED)
        assert exp.intent_rewrite is None


class TestExtractIntent:
    """Phase 14 (D50) pre-pass: peel the INTENT-tagged line off BEFORE
    parse_rewrites. First match wins; the matched line is removed; a
    missing/malformed/overlong tag yields None."""

    def test_intent_on_the_fourth_line(self):
        text = (
            "1) formal rephrasing\n"
            "2) keyword variant\n"
            "3) plain paraphrase\n"
            "4) INTENT: the underlying comparison of A and B"
        )
        intent, remaining = extract_intent(text)
        assert intent == "the underlying comparison of A and B"
        assert "INTENT" not in remaining
        # The three surface lines are untouched and still parse to three.
        assert parse_rewrites(remaining) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_intent_on_a_reordered_line_still_extracted(self):
        """The tag need not be on line 4: a reordered response (intent first)
        is still extracted, and that line — wherever it sits — is removed."""
        text = (
            "INTENT: the underlying information need\n"
            "1) formal rephrasing\n"
            "2) keyword variant\n"
            "3) plain paraphrase"
        )
        intent, remaining = extract_intent(text)
        assert intent == "the underlying information need"
        assert "INTENT" not in remaining
        assert parse_rewrites(remaining) == [
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        ]

    def test_duplicate_intent_lines_first_wins(self):
        text = (
            "1) formal rephrasing\n"
            "INTENT: first intent wins\n"
            "INTENT: second intent ignored"
        )
        intent, remaining = extract_intent(text)
        assert intent == "first intent wins"
        # Only the FIRST tagged line is removed; the second stays in the text.
        assert "second intent ignored" in remaining
        assert "first intent wins" not in remaining

    def test_unknown_tag_is_not_intent(self):
        """A different tag (PURPOSE:) is not the case-sensitive INTENT: tag, so
        no intent is extracted and NO line is removed."""
        text = (
            "1) formal rephrasing\n"
            "2) keyword variant\n"
            "3) plain paraphrase\n"
            "4) PURPOSE: not the intent tag"
        )
        intent, remaining = extract_intent(text)
        assert intent is None
        assert remaining == text  # nothing removed

    def test_lowercase_tag_is_not_matched(self):
        """The tag is case-sensitive: a lowercase 'intent:' is not the tag."""
        text = "1) formal rephrasing\n4) intent: lowercase is not the tag"
        intent, remaining = extract_intent(text)
        assert intent is None
        assert remaining == text

    def test_missing_tag_returns_none_and_unchanged_text(self):
        text = "1) formal rephrasing\n2) keyword variant\n3) plain paraphrase"
        intent, remaining = extract_intent(text)
        assert intent is None
        assert remaining == text

    def test_empty_restatement_is_malformed_but_line_removed(self):
        """A bare 'INTENT:' with nothing after it is malformed → None, but the
        line is still REMOVED so it can never fall through to parse_rewrites as
        a surface rewrite."""
        text = "1) formal rephrasing\n4) INTENT:   "
        intent, remaining = extract_intent(text)
        assert intent is None
        assert "INTENT" not in remaining
        assert remaining == "1) formal rephrasing"

    def test_overlong_intent_is_none_but_line_removed(self):
        long_intent = "x" * (MAX_INTENT_CHARS + 1)
        text = f"1) formal rephrasing\nINTENT: {long_intent}"
        intent, remaining = extract_intent(text)
        assert intent is None
        assert "INTENT" not in remaining

    def test_only_intent_line_removed_leaves_empty_text(self):
        """An only-intent response leaves parse_rewrites the empty string it
        already handles — mirroring parse_rewrites' empty-input behavior."""
        intent, remaining = extract_intent("INTENT: just the reframe")
        assert intent == "just the reframe"
        assert remaining == ""
        assert parse_rewrites(remaining) == []

    def test_empty_text_returns_none(self):
        assert extract_intent("") == (None, "")


class TestExpandQueryIntent:
    """Phase 14 (D50): expand_query wires the pre-pass in, dedups the intent,
    and carries it on the returned Expansion."""

    def _run(self, question, canned):
        with patch("src.query_rewrite._invoke_rewrite", return_value=canned):
            return expand_query(question, llm=object())

    def test_missing_intent_is_live_with_three_rewrites_and_none_intent(self):
        """A 3-line (no INTENT) output stays STATUS_LIVE with intent None —
        never a parse error."""
        canned = "1) formal rephrasing\n2) keyword variant\n3) plain paraphrase"
        result = self._run("What is a priority entry?", canned)
        assert result.status == STATUS_LIVE
        assert result.intent_rewrite is None
        assert len(result.rewrites) == 3

    def test_untagged_fourth_line_keeps_surface_cap_at_three(self):
        """An untagged 4th line is NOT an intent — it stays in the surface text,
        where parse_rewrites' MAX_REWRITES=3 cap drops it. Intent is None."""
        canned = (
            "1) formal rephrasing\n2) keyword variant\n"
            "3) plain paraphrase\n4) an untagged fourth rewrite"
        )
        result = self._run("What is a priority entry?", canned)
        assert result.intent_rewrite is None
        assert len(result.rewrites) == MAX_REWRITES  # still capped at 3

    def test_intent_carried_on_live_expansion(self):
        canned = (
            "1) formal rephrasing\n2) keyword variant\n"
            "3) plain paraphrase\n4) INTENT: comparing procedure A with procedure B"
        )
        result = self._run("A vs B?", canned)
        assert result.status == STATUS_LIVE
        assert result.rewrites == (
            "formal rephrasing",
            "keyword variant",
            "plain paraphrase",
        )
        assert result.intent_rewrite == "comparing procedure A with procedure B"

    def test_only_intent_output_mirrors_empty_parse_but_keeps_intent(self):
        """No surface lines → parse_rewrites empty-input behavior (zero
        rewrites, STATUS_PARSE_ERROR), but the validly extracted intent is
        still carried — it is an independent signal."""
        result = self._run("A vs B?", "INTENT: the underlying comparison")
        assert result.status == STATUS_PARSE_ERROR
        assert result.rewrites == ()
        assert result.intent_rewrite == "the underlying comparison"

    def test_intent_equal_to_original_deduped_to_none(self):
        question = "What is a priority entry?"
        canned = (
            "1) formal rephrasing\n2) keyword variant\n"
            f"3) plain paraphrase\n4) INTENT: {question.upper()}"
        )
        result = self._run(question, canned)
        assert result.status == STATUS_LIVE
        assert result.intent_rewrite is None  # casefold-equal to the original

    def test_intent_equal_to_surface_rewrite_deduped_to_none(self):
        canned = (
            "1) formal rephrasing\n2) keyword variant\n"
            "3) plain paraphrase\n4) INTENT: FORMAL REPHRASING"
        )
        result = self._run("What is a priority entry?", canned)
        assert result.status == STATUS_LIVE
        assert result.intent_rewrite is None  # casefold-equal to a surface rewrite

    def test_overlong_intent_expands_to_none_intent(self):
        long_intent = "y " * 200  # well over MAX_INTENT_CHARS
        canned = (
            "1) formal rephrasing\n2) keyword variant\n"
            f"3) plain paraphrase\n4) INTENT: {long_intent}"
        )
        result = self._run("What is a priority entry?", canned)
        assert result.status == STATUS_LIVE
        assert result.intent_rewrite is None

    def test_strict_paths_disabled_and_no_key_carry_no_intent(self, monkeypatch):
        """The pre-parse failure returns (disabled / no_key) never ran
        extraction, so they carry intent None — verifying intent handling under
        the degrade paths too."""
        disabled = expand_query("q", enabled=False)
        assert disabled.status == STATUS_DISABLED
        assert disabled.intent_rewrite is None

        no_key = expand_query("q", llm=None)  # conftest scrubs the key
        assert no_key.status == STATUS_NO_KEY
        assert no_key.intent_rewrite is None
