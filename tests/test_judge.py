"""Tests for the Phase 10 LLM-as-judge faithfulness estimate (src/judge.py).

Everything is IO-free: a fake ``llm_fn`` (``Callable[[prompt_vars], raw_text]``)
stands in for the real Claude call in every test except the two default-adapter
seam tests, which monkeypatch ``src.judge.get_llm`` to prove the default path
routes through that seam and never touches ChatAnthropic directly.
"""

import random

import pytest

from src.judge import (
    FAILURE_SUPPRESSION_THRESHOLD,
    GENERATION_MODEL,
    JUDGE_MODEL,
    JUDGE_PROMPT,
    JUDGE_PROMPT_VERSION,
    judge_answer,
    judge_answers,
)


class TestModuleConstants:
    """Sanity-check the constants the report's provenance block relies on."""

    def test_judge_model_is_generation_model(self):
        """D38: the judge must be the same model family as the generator."""
        assert JUDGE_MODEL == GENERATION_MODEL

    def test_prompt_version_string(self):
        assert JUDGE_PROMPT_VERSION == "faithfulness-judge-v1"

    def test_failure_suppression_threshold(self):
        assert FAILURE_SUPPRESSION_THRESHOLD == 0.20

    def test_judge_prompt_vars(self):
        """The prompt template must declare exactly the three vars judge_answer
        feeds it: question, answer, context."""
        assert set(JUDGE_PROMPT.input_variables) == {"question", "answer", "context"}


class TestJudgeAnswerPromptVars:
    """Verify judge_answer feeds llm_fn exactly the vars it promises."""

    def test_captures_question_answer_context(self):
        """A fake llm_fn that records its prompt_vars should see the exact
        question/answer/context strings passed to judge_answer, under the
        exact keys the docstring promises."""
        captured = []

        def fake_llm(prompt_vars):
            captured.append(prompt_vars)
            return '{"claims": []}'

        judge_answer("the question", "the answer", "the context", llm_fn=fake_llm)

        assert len(captured) == 1
        assert captured[0] == {
            "question": "the question",
            "answer": "the answer",
            "context": "the context",
        }


class TestJudgeAnswerScoring:
    """Valid-JSON scoring, fenced/prose-wrapped JSON, and verdict handling."""

    def test_valid_json_scoring(self):
        """Three claims (supported/unsupported/unclear) -> counts and mean."""

        def fake_llm(prompt_vars):
            return (
                '{"claims": ['
                '{"claim": "c1", "verdict": "supported"}, '
                '{"claim": "c2", "verdict": "unsupported"}, '
                '{"claim": "c3", "verdict": "unclear"}'
                "]}"
            )

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is True
        assert result["error_type"] is None
        assert result["n_claims"] == 3
        assert result["supported"] == 1
        assert result["unsupported"] == 1
        assert result["unclear"] == 1
        assert result["faithfulness"] == pytest.approx(1 / 3)
        assert len(result["verdicts"]) == 3

    def test_fenced_json(self):
        """A ```json ... ``` fence (with a trailing newline) must be stripped."""

        def fake_llm(prompt_vars):
            return '```json\n{"claims":[{"claim":"c","verdict":"supported"}]}\n```\n'

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is True
        assert result["faithfulness"] == 1.0

    def test_prose_wrapped_json(self):
        """Surrounding prose around the JSON object must be stripped."""

        def fake_llm(prompt_vars):
            return 'Here is my judgment: {"claims": [{"claim":"c","verdict":"supported"}]} — done.'

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is True
        assert result["n_claims"] == 1

    def test_malformed_json_is_parse_error(self):
        """Text with no JSON object at all -> parse error, not a crash."""

        def fake_llm(prompt_vars):
            return "not json at all"

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is False
        assert result["error_type"] == "parse"
        assert result["faithfulness"] is None
        assert result["n_claims"] == 0

    def test_schema_invalid_claims_not_a_list(self):
        """Valid JSON, but 'claims' is not a list -> parse error (schema check)."""

        def fake_llm(prompt_vars):
            return '{"claims": "notalist"}'

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is False
        assert result["error_type"] == "parse"

    def test_malformed_claim_elements_are_parse_error(self):
        """A claims list whose elements are not verdict-bearing objects (null, a
        bare string, or an object with no verdict) is a schema violation -> parse
        error, NOT a zero-faithfulness 'success' that would drag the mean down
        and dodge the failure counter (codex finding 5)."""
        for bad in (
            '{"claims": [null, null]}',
            '{"claims": ["just a string claim"]}',
            '{"claims": [{"claim": "c"}]}',            # no verdict key
            '{"claims": [{"claim": "c", "verdict": 3}]}',  # verdict not a string
        ):
            result = judge_answer("q", "a", "c", llm_fn=lambda v, _b=bad: _b)
            assert result["ok"] is False, bad
            assert result["error_type"] == "parse", bad

    def test_verdict_only_object_is_accepted(self):
        """A well-formed record with a string verdict but no claim text is still
        a valid claim (text defaults to empty) — only structure is enforced."""
        result = judge_answer(
            "q", "a", "c",
            llm_fn=lambda v: '{"claims": [{"verdict": "supported"}]}',
        )
        assert result["ok"] is True
        assert result["n_claims"] == 1
        assert result["supported"] == 1

    def test_llm_fn_exception_is_api_error(self):
        """An llm_fn that raises (network/auth/rate-limit) is an API error,
        held apart from parse errors, with everything zeroed out."""

        def fake_llm(prompt_vars):
            raise RuntimeError("boom")

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is False
        assert result["error_type"] == "api"
        assert result["faithfulness"] is None
        assert result["verdicts"] == []

    def test_zero_claims_faithfulness_is_none_not_zero(self):
        """An answer with no checkable claims parses OK but must report
        faithfulness=None, never 0.0 (0.0 would misleadingly read as 'fully
        unfaithful')."""

        def fake_llm(prompt_vars):
            return '{"claims": []}'

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["ok"] is True
        assert result["n_claims"] == 0
        assert result["faithfulness"] is None
        assert result["error_type"] is None

    def test_unknown_verdict_normalises_to_unclear(self):
        """A verdict the model invents outside the three valid values must
        fail safe to 'unclear', never silently count as 'supported'."""

        def fake_llm(prompt_vars):
            return '{"claims":[{"claim":"c","verdict":"maybe"}]}'

        result = judge_answer("q", "a", "c", llm_fn=fake_llm)

        assert result["unclear"] == 1
        assert result["supported"] == 0
        assert result["verdicts"][0]["verdict"] == "unclear"


class TestJudgeAnswersAggregation:
    """judge_answers: aggregation math, sampling, and failure suppression."""

    def test_aggregation_math_over_all_items(self):
        """Three items: fully supported (1.0), half supported (0.5), and
        zero-claim (None). The mean must average only the two scored items,
        and per_item must carry the question through."""

        def fake_llm(prompt_vars):
            answer = prompt_vars["answer"]
            if answer == "full":
                return '{"claims": [{"claim": "c1", "verdict": "supported"}]}'
            if answer == "half":
                return (
                    '{"claims": ['
                    '{"claim": "c1", "verdict": "supported"}, '
                    '{"claim": "c2", "verdict": "unsupported"}'
                    "]}"
                )
            return '{"claims": []}'

        items = [
            {"question": "q1", "answer": "full", "context": "ctx"},
            {"question": "q2", "answer": "half", "context": "ctx"},
            {"question": "q3", "answer": "zero", "context": "ctx"},
        ]

        result = judge_answers(items, llm_fn=fake_llm)

        assert result["attempted"] == 3
        assert result["successful"] == 3
        assert result["zero_claim"] == 1
        assert result["scored_n"] == 2
        assert result["mean_faithfulness"] == pytest.approx((1.0 + 0.5) / 2)
        assert result["suppressed"] is False
        assert len(result["per_item"]) == 3
        assert all("question" in r for r in result["per_item"])

    def test_deterministic_sampling(self):
        """sample_n < len(items) must judge exactly random.Random(seed).sample
        of items — same items, same order."""
        items = [
            {"question": f"q{i}", "answer": "a", "context": "c"} for i in range(5)
        ]

        def fake_llm(prompt_vars):
            return '{"claims": [{"claim": "c", "verdict": "supported"}]}'

        result = judge_answers(items, llm_fn=fake_llm, sample_n=2, seed=0)

        expected = random.Random(0).sample(items, 2)
        judged_questions = [r["question"] for r in result["per_item"]]
        assert judged_questions == [it["question"] for it in expected]
        assert result["attempted"] == 2
        assert result["sample_n"] == 2

    def test_sample_n_gte_len_judges_all(self):
        """sample_n >= len(items) is not a valid sub-sample: judge everything."""
        items = [
            {"question": f"q{i}", "answer": "a", "context": "c"} for i in range(3)
        ]

        def fake_llm(prompt_vars):
            return '{"claims": [{"claim": "c", "verdict": "supported"}]}'

        result = judge_answers(items, llm_fn=fake_llm, sample_n=10)

        assert result["attempted"] == 3

    def test_mean_suppressed_over_failure_threshold(self):
        """5 items, 2 raise (api errors) -> failure_rate 0.4 > 0.20 ->
        suppressed True."""
        items = [
            {"question": f"q{i}", "answer": str(i), "context": "c"} for i in range(5)
        ]

        def fake_llm(prompt_vars):
            if prompt_vars["answer"] in ("0", "1"):
                raise RuntimeError("boom")
            return '{"claims": [{"claim": "c", "verdict": "supported"}]}'

        result = judge_answers(items, llm_fn=fake_llm)

        assert result["api_errors"] == 2
        assert result["failure_rate"] == pytest.approx(0.4)
        assert result["suppressed"] is True

    def test_failure_just_under_threshold_not_suppressed(self):
        """5 items, exactly 1 failure -> failure_rate 0.20, NOT > 0.20 ->
        suppressed False (the threshold is a strict '>')."""
        items = [
            {"question": f"q{i}", "answer": str(i), "context": "c"} for i in range(5)
        ]

        def fake_llm(prompt_vars):
            if prompt_vars["answer"] == "0":
                raise RuntimeError("boom")
            return '{"claims": [{"claim": "c", "verdict": "supported"}]}'

        result = judge_answers(items, llm_fn=fake_llm)

        assert result["failure_rate"] == pytest.approx(0.2)
        assert result["suppressed"] is False


class TestDefaultAdapterSeam:
    """The default llm_fn must route through src.judge.get_llm — never
    ChatAnthropic directly — so tests can monkeypatch that one seam."""

    def test_default_path_routes_through_get_llm(self, monkeypatch):
        """With no llm_fn given, judge_answer must call src.judge.get_llm().
        Patch it to record the call and then raise, proving the default
        adapter reached it and that the raise is caught as an API error."""
        calls = []

        def fake_get_llm():
            calls.append(True)
            raise RuntimeError("get_llm reached")

        monkeypatch.setattr("src.judge.get_llm", fake_get_llm)

        result = judge_answer("q", "a", "c")

        assert calls, "default adapter never called src.judge.get_llm"
        assert result["error_type"] == "api"

    def test_injected_llm_fn_bypasses_get_llm(self, monkeypatch):
        """When llm_fn is injected, get_llm must never be touched."""

        def fake_get_llm():
            raise AssertionError("get_llm should not be called when llm_fn is injected")

        monkeypatch.setattr("src.judge.get_llm", fake_get_llm)

        result = judge_answer(
            "q", "a", "c",
            llm_fn=lambda v: '{"claims":[{"claim":"c","verdict":"supported"}]}',
        )

        assert result["ok"] is True
