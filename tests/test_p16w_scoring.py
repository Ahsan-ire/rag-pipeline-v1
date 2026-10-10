"""Tests for src/eval_scoring.py (16A-1 item 3, acceptance (e) [W] and (f)).

Synthetic fixtures only: invented section numbers (``3.x``, ``7.x``, ``9.x``)
and invented answers about "widgets". No IO, no models, no eval rows.
"""

import random
from types import SimpleNamespace

import pytest

from src.eval_scoring import (
    OBSERVED_SCOPES,
    observed_scope,
    score_evidence,
    score_partial,
)
from src.evaluator import evaluate_retrieval
from src.generator import CAVEAT_PREFIX, REFUSAL_PHRASE
from src.grounding import (
    ANSWER_TRUNCATED,
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    GENERATION_INCOMPLETE,
    MODEL_DECLINED,
    PARTIALLY_VERIFIED,
    REFUSAL,
)


def ranked(*sections):
    """``(chunk_id, section)`` pairs with ids c1, c2, ... in order."""
    return [(f"c{i}", s) for i, s in enumerate(sections, start=1)]


# --------------------------------------------------------------------------
# (e) score_evidence
# --------------------------------------------------------------------------


class TestScoreEvidence:
    def test_two_groups_hit_completion_is_later_groups_first_hit(self):
        r = ranked("9.9", "3.2", "5.5", "7.1", "3.2", "7.1")
        out = score_evidence(r, [["3.2"], ["7.1"]], "strict")
        assert out["group_ranks"] == [2, 4]
        assert out["completion_rank"] == 4
        assert out["hit_at_k"] == {1: False, 3: False, 6: True}
        assert out["groups_covered"] == {1: 0, 3: 1, 6: 2}
        assert out["groups_total"] == 2

    def test_one_group_missing_is_a_miss_at_every_k(self):
        r = ranked("3.2", "9.9", "9.8", "9.7", "9.6", "9.5")
        out = score_evidence(r, [["3.2"], ["7.1"]], "related")
        assert out["completion_rank"] is None
        assert out["group_ranks"] == [1, None]
        assert out["hit_at_k"] == {1: False, 3: False, 6: False}
        assert out["groups_covered"] == {1: 1, 3: 1, 6: 1}

    def test_or_alternates_first_alternative_wins(self):
        r = ranked("9.9", "3.4", "3.2")
        out = score_evidence(r, [["3.2", "3.4"]], "strict")
        assert out["completion_rank"] == 2

    def test_parent_matches_under_related_only(self):
        # Expected 3.2.1, retrieved parent 3.2; and expected 7.1, retrieved child 7.1.4.
        r = ranked("3.2", "7.1.4")
        assert score_evidence(r, [["3.2.1"]], "related")["completion_rank"] == 1
        assert score_evidence(r, [["3.2.1"]], "strict")["completion_rank"] is None
        assert score_evidence(r, [["7.1"]], "related")["completion_rank"] == 2
        assert score_evidence(r, [["7.1"]], "strict")["completion_rank"] is None
        # Sibling prefix is not nesting.
        assert score_evidence(ranked("3.21"), [["3.2"]], "related")["completion_rank"] is None

    def test_alias_takes_its_chunks_rank_and_adds_no_entries(self):
        r = ranked("9.9", "3.2", "7.1")
        absorbed = {"c2": ["3.5"]}
        out = score_evidence(r, [["3.5"], ["7.1"]], "strict", absorbed=absorbed)
        assert out["group_ranks"] == [2, 3]  # 7.1 still at rank 3: no shift
        assert out["completion_rank"] == 3
        without = score_evidence(r, [["3.5"], ["7.1"]], "strict")
        assert without["completion_rank"] is None
        # An alias on a chunk not in the ranked list contributes nothing.
        out2 = score_evidence(r, [["3.6"]], "strict", absorbed={"c99": ["3.6"]})
        assert out2["completion_rank"] is None

    def test_alias_matches_under_related_too(self):
        r = ranked("9.9", "3.2")
        out = score_evidence(r, [["3.5.1"]], "related", absorbed={"c2": ["3.5"]})
        assert out["completion_rank"] == 2

    def test_groups_covered_custom_ks(self):
        r = ranked("3.2", "9.9", "7.1", "8.8")
        out = score_evidence(r, [["3.2"], ["7.1"], ["8.8"]], "strict", ks=(1, 2, 4))
        assert out["groups_covered"] == {1: 1, 2: 1, 4: 3}
        assert out["hit_at_k"] == {1: False, 2: False, 4: True}

    def test_whitespace_stripped_and_empty_never_strict(self):
        r = ranked("  ", " 3.2 ")
        assert score_evidence(r, [["3.2 "]], "strict")["completion_rank"] == 2
        assert score_evidence(ranked(""), [[""]], "strict")["completion_rank"] is None

    def test_appendix_never_cross_matches(self):
        r = ranked("14.1", "APPENDIX 14.1")
        assert score_evidence(r, [["APPENDIX 14.1"]], "related")["completion_rank"] == 2

    @pytest.mark.parametrize(
        "groups, mode",
        [([], "strict"), ([["3.2"], []], "strict"), ([["3.2"]], "loose")],
    )
    def test_invalid_input_raises(self, groups, mode):
        with pytest.raises(ValueError):
            score_evidence(ranked("3.2"), groups, mode)


def _doc(section):
    return SimpleNamespace(metadata={"section_number": section})


class TestSingleGroupEqualsEvaluator:
    """One group = today's first_strict_rank / first_related_rank, checked
    against the real ``evaluate_retrieval`` with a fake retriever."""

    POOL = ["3", "3.2", "3.2.1", "3.21", "7.1", "7.1.4", "9", "", "APPENDIX 3.2", "  3.2 "]

    def _cases(self):
        rng = random.Random(16)
        cases = [
            (["3.2"], ["", "3.2.1", "3.2"]),  # empty section never strict-matches
            ([""], ["", "3.2"]),  # empty expected vs empty retrieved: strict skip
            (["3.2", "7.1"], ["9", "7.1.4", "3.2"]),
            (["3.2"], []),
        ]
        for _ in range(200):
            expected = rng.sample(self.POOL, rng.randint(1, 3))
            retrieved = [rng.choice(self.POOL) for _ in range(rng.randint(0, 6))]
            cases.append((expected, retrieved))
        return cases

    def test_matches_evaluate_retrieval(self):
        cases = self._cases()
        golden = [
            {"question": f"q{i}", "type": "factual", "expected_sections": exp}
            for i, (exp, _) in enumerate(cases)
        ]
        by_q = {f"q{i}": ret for i, (_, ret) in enumerate(cases)}

        def fake_retrieve(question, top_k=6):
            return [{"document": _doc(s), "score": 0.0} for s in by_q[question]]

        result = evaluate_retrieval(golden, retrieve_fn=fake_retrieve, top_k=6)
        for (expected, retrieved), pq in zip(cases, result["per_question"]):
            r = ranked(*retrieved)
            strict = score_evidence(r, [expected], "strict")
            related = score_evidence(r, [expected], "related")
            assert strict["completion_rank"] == pq["first_strict_rank"], (expected, retrieved)
            assert related["completion_rank"] == pq["first_related_rank"], (expected, retrieved)


# --------------------------------------------------------------------------
# (f) observed_scope and score_partial
# --------------------------------------------------------------------------

GROUPS = [["3.2", "3.4"], ["7.1"]]
GAPS = [{"id": "g1", "keywords": ["widget levy", "levy"]}]
CLAIM = "Widgets must be registered before completion [para 3.2.1, p.10]."
GAP_TEXT = "The handbook does not address the widget levy."


def make_result(
    answer,
    outcome=CITATIONS_VERIFIED,
    status="complete",
    grounded=({"para": "3.2.1", "page": "10", "raw": "para 3.2.1, p.10"},),
    ungrounded=(),
):
    return {
        "answer": answer,
        "citations": list(grounded) + list(ungrounded),
        "citation_check": {"grounded": list(grounded), "ungrounded": list(ungrounded)},
        "generation_status": status,
        "gate_outcome": outcome,
    }


class TestObservedScope:
    def test_one_fixture_per_class(self):
        fixtures = {
            "unscored": make_result(f"{CLAIM} {GAP_TEXT}", outcome=ANSWER_TRUNCATED, status="truncated"),
            "withheld": make_result(f"{CLAIM} {GAP_TEXT}", outcome=CITATIONS_UNVERIFIED, grounded=()),
            "refuse": make_result(REFUSAL_PHRASE, outcome=REFUSAL, grounded=()),
            "partial": make_result(f"{CLAIM}\n- {GAP_TEXT}"),
            "answer": make_result(CLAIM),
        }
        assert set(fixtures) == set(OBSERVED_SCOPES)
        for expected, result in fixtures.items():
            assert observed_scope(result) == expected

    @pytest.mark.parametrize(
        "status, outcome",
        [
            ("truncated", ANSWER_TRUNCATED),
            ("declined", MODEL_DECLINED),
            ("incomplete", GENERATION_INCOMPLETE),
            ("unknown", CITATIONS_VERIFIED),
            ("error", CITATIONS_VERIFIED),
            ("complete", ANSWER_TRUNCATED),  # terminal outcome alone
            ("complete", None),  # no gate outcome on a non-refusal
        ],
    )
    def test_unscored(self, status, outcome):
        assert observed_scope(make_result(f"{CLAIM} {GAP_TEXT}", outcome, status)) == "unscored"

    def test_none_and_missing_status_unscored(self):
        assert observed_scope(None) == "unscored"
        r = make_result(CLAIM)
        del r["generation_status"]
        assert observed_scope(r) == "unscored"

    def test_refusal_without_gate_outcome_is_refuse(self):
        assert observed_scope(make_result(REFUSAL_PHRASE, outcome=None, grounded=())) == "refuse"

    def test_caveat_answer_without_gap_is_answer(self):
        r = make_result(f"{CAVEAT_PREFIX} {CLAIM}")
        assert observed_scope(r) == "answer"

    def test_caveat_answer_with_gap_is_partial(self):
        r = make_result(f"{CAVEAT_PREFIX} {CLAIM} {GAP_TEXT}")
        assert observed_scope(r) == "partial"

    def test_hedged_gap_is_answer(self):
        r = make_result(f"{CLAIM} The handbook does not address the levy, but it is likely 2%.")
        assert observed_scope(r) == "answer"


class TestScorePartial:
    @pytest.mark.parametrize("outcome", [CITATIONS_VERIFIED, PARTIALLY_VERIFIED])
    @pytest.mark.parametrize(
        "gap_unit",
        [
            GAP_TEXT,
            f"- {GAP_TEXT}",
            f"* {GAP_TEXT}",
            f"1) {GAP_TEXT}",
            f"2. {GAP_TEXT}",
            f"(a) {GAP_TEXT}",
            f"**{GAP_TEXT}**",
            "The extracts do not mention any WIDGET LEVY.",  # any case
        ],
    )
    def test_stated_gap_is_correct(self, outcome, gap_unit):
        r = make_result(f"{CLAIM}\n{gap_unit}", outcome=outcome)
        out = score_partial(r, GROUPS, GAPS)
        assert out["status"] == "correct", out
        assert out["gap_recall"] == 1.0
        assert out["reasons"] == []
        assert out["gaps_stated"] == ["g1"]

    def test_missing_gap_incorrect(self):
        out = score_partial(make_result(f"{CLAIM} The handbook does not address fees."), GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert out["reasons"] == ["gap_not_stated"]
        assert out["gap_recall"] == 0.0
        assert out["gaps_missing"] == ["g1"]

    def test_keyword_outside_gap_statement_incorrect(self):
        answer = f"The widget levy is payable [para 3.2.1, p.10]. The handbook does not address fees."
        out = score_partial(make_result(answer), GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert "gap_not_stated" in out["reasons"]

    def test_keyword_must_be_whole_word(self):
        gaps = [{"id": "g1", "keywords": ["levy"]}]
        answer = f"{CLAIM} The handbook does not address levying fees."
        assert score_partial(make_result(answer), GROUPS, gaps)["status"] == "incorrect"

    def test_hedged_gap_incorrect(self):
        answer = f"{CLAIM} The handbook does not address the widget levy, but it is probably 2%."
        out = score_partial(make_result(answer), GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert out["reasons"] == ["gap_not_stated"]

    def test_whole_refusal_incorrect(self):
        out = score_partial(make_result(REFUSAL_PHRASE, outcome=REFUSAL, grounded=()), GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert "refusal" in out["reasons"]
        assert "outcome_not_verified" in out["reasons"]

    def test_no_verified_citation_in_required_group_incorrect(self):
        grounded = ({"para": "9.4", "page": "50", "raw": "para 9.4, p.50"},)
        answer = f"Widgets need a licence [para 9.4, p.50].\n- {GAP_TEXT}"
        out = score_partial(make_result(answer, grounded=grounded), GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert out["reasons"] == ["no_verified_citation_in_group"]

    def test_group_citation_only_ungrounded_incorrect(self):
        grounded = ({"para": "9.4", "page": "50", "raw": "para 9.4, p.50"},)
        ungrounded = ({"para": "7.1", "page": "99", "raw": "para 7.1, p.99"},)
        answer = f"A [para 9.4, p.50]. B [para 7.1, p.99].\n- {GAP_TEXT}"
        r = make_result(answer, outcome=PARTIALLY_VERIFIED, grounded=grounded, ungrounded=ungrounded)
        assert score_partial(r, GROUPS, GAPS)["reasons"] == ["no_verified_citation_in_group"]

    def test_any_group_suffices(self):
        grounded = ({"para": "7.1.2", "page": "60", "raw": "para 7.1.2, p.60"},)
        answer = f"Widgets [para 7.1.2, p.60].\n- {GAP_TEXT}"
        assert score_partial(make_result(answer, grounded=grounded), GROUPS, GAPS)["status"] == "correct"

    def test_withheld_draft_incorrect(self):
        r = make_result(f"{CLAIM}\n- {GAP_TEXT}", outcome=CITATIONS_UNVERIFIED)
        out = score_partial(r, GROUPS, GAPS)
        assert out["status"] == "incorrect"
        assert out["observed_scope"] == "withheld"
        assert out["reasons"] == ["outcome_not_verified"]

    @pytest.mark.parametrize(
        "status, outcome",
        [
            ("truncated", ANSWER_TRUNCATED),
            ("declined", MODEL_DECLINED),
            ("incomplete", GENERATION_INCOMPLETE),
            ("unknown", CITATIONS_VERIFIED),
            ("error", CITATIONS_VERIFIED),
        ],
    )
    def test_unfinished_drafts_unscored(self, status, outcome):
        r = make_result(f"{CLAIM}\n- {GAP_TEXT}", outcome=outcome, status=status)
        out = score_partial(r, GROUPS, GAPS)
        assert out["status"] == "unscored"
        assert out["gap_recall"] is None

    def test_none_result_unscored(self):
        assert score_partial(None, GROUPS, GAPS)["status"] == "unscored"

    def test_every_gap_must_be_stated_recall_separate(self):
        gaps = GAPS + [{"id": "g2", "keywords": ["easement"]}]
        out = score_partial(make_result(f"{CLAIM}\n- {GAP_TEXT}"), GROUPS, gaps)
        assert out["status"] == "incorrect"
        assert out["gap_recall"] == 0.5
        assert out["gaps_stated"] == ["g1"]
        assert out["gaps_missing"] == ["g2"]

    def test_caveat_opener_is_not_a_gap_statement(self):
        # The caveat sentence itself starts "The source material does not ...";
        # stripped once, it cannot state a gap whose keyword is "question".
        gaps = [{"id": "g1", "keywords": ["question"]}]
        r = make_result(f"{CAVEAT_PREFIX} {CLAIM}")
        out = score_partial(r, GROUPS, gaps)
        assert out["observed_scope"] == "answer"
        assert out["status"] == "incorrect"
        assert out["reasons"] == ["gap_not_stated"]
