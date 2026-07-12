"""Tests for the grounding gate (src.grounding.classify).

Pure-function tests: ``classify`` takes an answer string plus already-computed
citation structures, so no IO or model mocking is needed. The refusal detector
it imports lazily (``src.generator.is_refusal``) is exercised through the real
string, not a mock.
"""

from src.generator import REFUSAL_PHRASE
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    REFUSAL,
    classify,
)

# A grounded/ungrounded citation dict, shaped like extract_citations output.
_PARA = {"para": "14.8.5", "page": "412", "raw": "para 14.8.5, p.412"}
_PARA2 = {"para": "1.2", "page": "1", "raw": "para 1.2, p.1"}
_BAD = {"para": "99.9", "page": "5", "raw": "para 99.9, p.5"}
_APPENDIX = {"para": "APPENDIX 14.1", "page": "87", "raw": "APPENDIX 14.1, p.87"}

# A plain non-refusal answer.
_REAL_ANSWER = "A priority entry protects the purchaser [Handbook, para 14.8.5, p.412]."


class TestClassifyRefusal:
    def test_exact_refusal_phrase_with_empty_citations_is_refusal(self):
        """A refusal answer classifies as REFUSAL even with no citations —
        refusal is decided by the answer text alone."""
        outcome = classify(
            f"{REFUSAL_PHRASE}.", [], {"grounded": [], "ungrounded": []}
        )
        assert outcome == REFUSAL

    def test_refusal_wins_over_a_stray_citation(self):
        """If the answer IS the refusal sentence, it classifies as REFUSAL even
        when a (stray) grounded citation is passed alongside — refusal wins."""
        outcome = classify(
            f"{REFUSAL_PHRASE}.",
            [_PARA],
            {"grounded": [_PARA], "ungrounded": []},
        )
        assert outcome == REFUSAL


class TestClassifyVerification:
    def test_non_refusal_zero_citations_is_unverified(self):
        """The P0 fix: a non-refusal answer that cites nothing is
        CITATIONS_UNVERIFIED, never valid."""
        outcome = classify(
            "The deposit is usually 10 percent.",
            [],
            {"grounded": [], "ungrounded": []},
        )
        assert outcome == CITATIONS_UNVERIFIED

    def test_single_grounded_citation_is_verified(self):
        outcome = classify(
            _REAL_ANSWER, [_PARA], {"grounded": [_PARA], "ungrounded": []}
        )
        assert outcome == CITATIONS_VERIFIED

    def test_two_grounded_citations_is_verified(self):
        outcome = classify(
            _REAL_ANSWER,
            [_PARA, _PARA2],
            {"grounded": [_PARA, _PARA2], "ungrounded": []},
        )
        assert outcome == CITATIONS_VERIFIED

    def test_one_grounded_one_ungrounded_is_partial(self):
        outcome = classify(
            _REAL_ANSWER,
            [_PARA, _BAD],
            {"grounded": [_PARA], "ungrounded": [_BAD]},
        )
        assert outcome == PARTIALLY_VERIFIED

    def test_all_ungrounded_is_unverified(self):
        outcome = classify(
            _REAL_ANSWER, [_BAD], {"grounded": [], "ungrounded": [_BAD]}
        )
        assert outcome == CITATIONS_UNVERIFIED

    def test_appendix_only_grounded_citation_is_verified(self):
        """Appendix locators are first-class citations (D34): an answer whose
        only citation is a grounded appendix locator is CITATIONS_VERIFIED."""
        outcome = classify(
            "See the prescribed form [Handbook, APPENDIX 14.1, p.87].",
            [_APPENDIX],
            {"grounded": [_APPENDIX], "ungrounded": []},
        )
        assert outcome == CITATIONS_VERIFIED
