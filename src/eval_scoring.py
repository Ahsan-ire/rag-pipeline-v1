"""Schema-2 eval scorers: evidence groups, observed scope and PARTIAL correctness.

Phase 16A-1 item 3 (D66). Pure functions over plain data, no IO, so they can be
tested on synthetic fixtures alone. They are not yet wired into the evaluator's
runners (v1 sets keep the v5 path); report v6 will call them.

Shapes used throughout:

* A **section** is a locator string as stored in chunk metadata
  (``section_number``) or extracted from a citation (``"3.2.1"``,
  ``"APPENDIX 14.1"``). Sections are ``.strip()``ed before comparison, as in
  ``evaluator.evaluate_retrieval``.
* **groups** (evidence groups) is a list of AND groups, each a non-empty list of
  OR alternative sections: ``[["3.2", "3.4"], ["7.1"]]`` means "(3.2 or 3.4)
  and 7.1".
* A generation **result** is the dict ``generator.generate_with_sources``
  returns: ``answer`` (str), ``generation_status`` (``complete`` / ``truncated``
  / ``declined`` / ``incomplete`` / ``unknown``, or the evaluator-only
  ``error``), ``gate_outcome`` (a ``src.grounding`` outcome) and
  ``citation_check`` (``{"grounded": [citation, ...], "ungrounded": [...]}``,
  each citation a ``{"para", "page", "raw"}`` dict from
  ``generator.extract_citations``). A **verified citation** is an entry of
  ``citation_check["grounded"]``; its section is its ``para`` value.
  ``None`` stands for a generation that raised (status ``error``).
* A **gap** is ``{"id": str, "keywords": [str, ...]}``.
"""

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.generator import CAVEAT_PREFIX, _sections_related, is_refusal
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    STATUS_COMPLETE,
    TERMINAL_OUTCOMES,
)
from src.text_utils import is_gap_statement, split_sentences

MODE_STRICT = "strict"
MODE_RELATED = "related"
EVIDENCE_MODES = (MODE_STRICT, MODE_RELATED)
DEFAULT_KS: Tuple[int, ...] = (1, 3, 6)

SCOPE_UNSCORED = "unscored"
SCOPE_WITHHELD = "withheld"
SCOPE_REFUSE = "refuse"
SCOPE_PARTIAL = "partial"
SCOPE_ANSWER = "answer"
OBSERVED_SCOPES = (
    SCOPE_UNSCORED,
    SCOPE_WITHHELD,
    SCOPE_REFUSE,
    SCOPE_PARTIAL,
    SCOPE_ANSWER,
)

PARTIAL_CORRECT = "correct"
PARTIAL_INCORRECT = "incorrect"
PARTIAL_UNSCORED = "unscored"

# Outcomes under which a PARTIAL answer can be correct.
_SCOREABLE_OUTCOMES = (CITATIONS_VERIFIED, PARTIALLY_VERIFIED)


def _section_matches(member: str, candidate: str, mode: str) -> bool:
    """True if ``candidate`` (retrieved/cited) matches ``member`` (expected) under ``mode``.

    strict: literal equality, and an empty candidate never matches (the
    evaluator's "skip empty retrieved sections" rule). related: today's
    dotted-nesting rule, ``generator._sections_related(expected, retrieved)``.
    """
    if mode == MODE_STRICT:
        return bool(candidate) and member == candidate
    return _sections_related(member, candidate)


def score_evidence(
    ranked: Sequence[Tuple[str, str]],
    groups: Sequence[Sequence[str]],
    mode: str,
    absorbed: Optional[Mapping[str, Iterable[str]]] = None,
    *,
    ks: Sequence[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    """Score a ranked retrieval list against AND-of-OR evidence groups.

    For each group, its rank is the 1-indexed position of the first ranked
    entry matching ANY of its members (``strict`` = equal after strip;
    ``related`` = equal-or-dotted-nested). ``completion_rank`` is the max of
    those ranks: the rank by which every group is covered; ``None`` if any group
    is never matched. With one group this is exactly the evaluator's
    ``first_strict_rank`` / ``first_related_rank``.

    ``absorbed`` maps a chunk id to the alias sections it absorbed (the item-7
    map, keyed by production chunk id). An alias is treated as present at its
    chunk's rank: it can match a member there, but it adds no ranked entries,
    so it never shifts another chunk's rank.

    The caller clamps ``ranked`` to its ``top_k`` (as ``evaluate_retrieval``
    does); this function scores whatever it is given.

    Args:
        ranked: Ordered ``(chunk_id, section)`` pairs, best first.
        groups: Non-empty list of non-empty lists of expected sections.
        mode: ``"strict"`` or ``"related"``.
        absorbed: Optional ``{chunk_id: [alias section, ...]}``.
        ks: The hit@k cut-offs to report (keyword-only, default ``(1, 3, 6)``).

    Returns:
        ``{"mode", "completion_rank": int | None, "group_ranks": [int | None,
        ...] (one per group, in order), "groups_total": int, "groups_covered":
        {k: int} (groups whose rank <= k), "hit_at_k": {k: bool}
        (``completion_rank <= k``)}``.

    Raises:
        ValueError: on an unknown ``mode``, an empty ``groups`` or an empty group.
    """
    if mode not in EVIDENCE_MODES:
        raise ValueError(f"unknown evidence mode: {mode!r}")
    if not groups:
        raise ValueError("groups must be non-empty")
    norm_groups: List[List[str]] = []
    for index, group in enumerate(groups):
        members = [str(m).strip() for m in group]
        if not members:
            raise ValueError(f"evidence group {index} is empty")
        norm_groups.append(members)

    absorbed = absorbed or {}
    group_ranks: List[Optional[int]] = [None] * len(norm_groups)
    for rank, (chunk_id, section) in enumerate(ranked, start=1):
        candidates = [str(section).strip()] + [
            str(a).strip() for a in absorbed.get(chunk_id, ())
        ]
        for index, members in enumerate(norm_groups):
            if group_ranks[index] is not None:
                continue
            if any(
                _section_matches(member, candidate, mode)
                for member in members
                for candidate in candidates
            ):
                group_ranks[index] = rank
        if all(r is not None for r in group_ranks):
            break

    completion_rank: Optional[int] = (
        None if any(r is None for r in group_ranks) else max(group_ranks)  # type: ignore[type-var]
    )
    return {
        "mode": mode,
        "completion_rank": completion_rank,
        "group_ranks": group_ranks,
        "groups_total": len(norm_groups),
        "groups_covered": {
            k: sum(1 for r in group_ranks if r is not None and r <= k) for k in ks
        },
        "hit_at_k": {
            k: completion_rank is not None and completion_rank <= k for k in ks
        },
    }


def answer_units(answer: str) -> List[str]:
    """Split an answer into units for scope and gap scoring.

    Strips exactly ONE leading ``CAVEAT_PREFIX`` (after ``lstrip``), exactly as
    ``render.uncited_statements`` and ``evaluator.evaluate_completeness`` do, so a
    D44 related-guidance opener (itself a "The source material does not ..."
    sentence) is never read as a gap statement. A second or mid-answer
    occurrence is left in place. The rest is split with ``split_sentences``.
    """
    text = answer.lstrip()
    if text.startswith(CAVEAT_PREFIX):
        text = text[len(CAVEAT_PREFIX):]
    return split_sentences(text)


def _answer_text(result: Optional[Mapping[str, Any]]) -> str:
    """The answer text of a result (``""`` for None or a missing answer)."""
    if result is None:
        return ""
    return str(result.get("answer") or "")


def observed_scope(result: Optional[Mapping[str, Any]]) -> str:
    """Classify what a generation result did, as the default display shows it.

    Default display means ``show_unverified=False``: an unverified draft is
    withheld. First match wins:

    1. ``unscored``: ``result`` is None (a generation error), its
       ``generation_status`` is not ``complete`` (missing counts as
       ``unknown``), its ``gate_outcome`` is terminal, or it has no gate outcome
       and is not a refusal. Citations are validated before the terminal status
       is applied (``generator.generate_with_sources``), so a terminal draft may
       carry verified citations; it is still unscored.
    2. ``withheld``: ``gate_outcome == CITATIONS_UNVERIFIED``.
    3. ``refuse``: the answer IS the refusal sentence (``is_refusal``, D32).
    4. ``partial``: some unit (``answer_units``) passes ``is_gap_statement``.
    5. ``answer``.

    Returns:
        One of ``OBSERVED_SCOPES``.
    """
    if result is None:
        return SCOPE_UNSCORED
    answer = _answer_text(result)
    refused = is_refusal(answer)
    gate_outcome = result.get("gate_outcome")
    if result.get("generation_status") != STATUS_COMPLETE:
        return SCOPE_UNSCORED
    if gate_outcome in TERMINAL_OUTCOMES:
        return SCOPE_UNSCORED
    if gate_outcome is None and not refused:
        return SCOPE_UNSCORED
    if gate_outcome == CITATIONS_UNVERIFIED:
        return SCOPE_WITHHELD
    if refused:
        return SCOPE_REFUSE
    if any(is_gap_statement(unit) for unit in answer_units(answer)):
        return SCOPE_PARTIAL
    return SCOPE_ANSWER


def _keyword_re(keyword: str) -> "re.Pattern[str]":
    """Whole-word, case-insensitive pattern for ``keyword``.

    Lookarounds rather than ``\\b`` so a keyword that begins or ends with a
    non-word character (``"s.12"``, ``"(a)"``) still needs a word boundary
    outside it.
    """
    return re.compile(
        r"(?<!\w)" + re.escape(keyword.strip()) + r"(?!\w)", re.IGNORECASE
    )


def _gap_stated(gap: Mapping[str, Any], gap_units: Sequence[str]) -> bool:
    """True if some gap-statement unit holds one of the gap's keywords."""
    patterns = [_keyword_re(str(k)) for k in gap.get("keywords", []) if str(k).strip()]
    return any(p.search(unit) for p in patterns for unit in gap_units)


def score_partial(
    result: Optional[Mapping[str, Any]],
    groups: Sequence[Sequence[str]],
    gaps: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Score a PARTIAL-labelled row: a cited answer that also names what is missing.

    ``unscored`` when ``observed_scope(result)`` is ``unscored`` (counted, never
    correct). Otherwise ``correct`` iff ALL of:

    * ``gate_outcome`` is ``CITATIONS_VERIFIED`` or ``PARTIALLY_VERIFIED``;
    * the answer is not a whole refusal (``is_refusal``);
    * at least one verified citation (``citation_check["grounded"]``)
      related-matches a member of some evidence group;
    * every gap is stated: some unit passing ``is_gap_statement`` contains one
      of the gap's keywords as a whole word, any case. A keyword elsewhere in
      the answer, or in a hedged gap statement, does not count.

    Gap recall (stated gaps / all gaps) is reported separately and does not
    depend on the other conditions; it is ``None`` for an unscored result or
    when there are no gaps.

    Args:
        result: A generation result dict (see module docstring), or None.
        groups: The row's evidence groups (AND of OR lists of sections).
        gaps: The row's gaps, ``[{"id", "keywords": [...]}, ...]``.

    Returns:
        ``{"status": "correct" | "incorrect" | "unscored", "observed_scope",
        "gap_recall": float | None, "gaps_stated": [gap id, ...],
        "gaps_missing": [gap id, ...], "reasons": [str, ...]}``. ``reasons``
        names every failed condition (empty when correct; ``["unscored"]`` when
        unscored), drawn from ``"outcome_not_verified"``, ``"refusal"``,
        ``"no_verified_citation_in_group"`` and ``"gap_not_stated"``.
    """
    scope = observed_scope(result)
    if scope == SCOPE_UNSCORED:
        return {
            "status": PARTIAL_UNSCORED,
            "observed_scope": scope,
            "gap_recall": None,
            "gaps_stated": [],
            "gaps_missing": [],
            "reasons": [PARTIAL_UNSCORED],
        }
    assert result is not None  # observed_scope returns unscored for None
    answer = _answer_text(result)
    reasons: List[str] = []

    if result.get("gate_outcome") not in _SCOREABLE_OUTCOMES:
        reasons.append("outcome_not_verified")
    if is_refusal(answer):
        reasons.append("refusal")

    members = [str(m).strip() for group in groups for m in group]
    verified = (result.get("citation_check") or {}).get("grounded", []) or []
    if not any(
        _sections_related(member, str(c.get("para", "")).strip())
        for c in verified
        for member in members
    ):
        reasons.append("no_verified_citation_in_group")

    gap_units = [u for u in answer_units(answer) if is_gap_statement(u)]
    flags = [_gap_stated(g, gap_units) for g in gaps]
    stated_ids = [g.get("id") for g, ok in zip(gaps, flags) if ok]
    missing_ids = [g.get("id") for g, ok in zip(gaps, flags) if not ok]
    if missing_ids:
        reasons.append("gap_not_stated")

    return {
        "status": PARTIAL_INCORRECT if reasons else PARTIAL_CORRECT,
        "observed_scope": scope,
        "gap_recall": (sum(flags) / len(gaps)) if gaps else None,
        "gaps_stated": stated_ids,
        "gaps_missing": missing_ids,
        "reasons": reasons,
    }
