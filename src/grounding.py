"""Grounding gate: classify how well an answer's citations are verified.

The gate names ONLY what the system actually VERIFIES — the answer's citation
locators against the paragraphs/pages of the chunks that were retrieved. It says
nothing about whether the underlying legal claims are correct; that is not
something this pipeline can check. The outcome vocabulary is deliberately about
*citation verification* ("VERIFIED"), never about legal validity, so a consumer
can never read "CITATIONS_VERIFIED" as "the law is stated correctly".

The four citation outcomes are mutually exclusive; ``classify`` returns exactly
one. Three further TERMINAL outcomes (H1b) name a generation that did not finish
normally: ``ANSWER_TRUNCATED``, ``MODEL_DECLINED`` and ``GENERATION_INCOMPLETE``.
``generation_outcome`` maps the generation status to them; they are decided before
``classify`` runs and take precedence over every citation outcome (the answer is
withheld). A query that retrieves nothing ends as ``no_results``, with no model call.
"""

from typing import Dict, List, Optional

# Outcome vocabulary. These are the only strings ``classify`` returns; consumers
# (display policy lives in pipeline.py) branch on them.
REFUSAL = "REFUSAL"
CITATIONS_VERIFIED = "CITATIONS_VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
CITATIONS_UNVERIFIED = "CITATIONS_UNVERIFIED"

# Terminal outcomes (H1b): the draft is cut off, declined or abnormally ended, so
# it is never displayed or returned, whatever its citations look like.
ANSWER_TRUNCATED = "ANSWER_TRUNCATED"
MODEL_DECLINED = "MODEL_DECLINED"
GENERATION_INCOMPLETE = "GENERATION_INCOMPLETE"
TERMINAL_OUTCOMES = (ANSWER_TRUNCATED, MODEL_DECLINED, GENERATION_INCOMPLETE)

# Generation status vocabulary (H1). ``error`` is evaluator-only: a row whose
# generation raised, counted in ``generation_errors`` and never as ``unknown``.
STATUS_COMPLETE = "complete"
STATUS_TRUNCATED = "truncated"
STATUS_DECLINED = "declined"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"
# Statuses where the model's output is not a finished, accepted answer.
INCOMPLETE_STATUSES = (STATUS_TRUNCATED, STATUS_DECLINED, STATUS_INCOMPLETE)

_STOP_REASON_STATUS = {
    "end_turn": STATUS_COMPLETE,
    "stop_sequence": STATUS_COMPLETE,
    "max_tokens": STATUS_TRUNCATED,
    "model_context_window_exceeded": STATUS_TRUNCATED,
    "refusal": STATUS_DECLINED,
}

# Exact user-facing texts (spec amendment 2): used verbatim as the display text
# and as the public ``answer`` of a withheld result.
WITHHELD_NOTICES = {
    ANSWER_TRUNCATED: (
        "WITHHELD \u2014 ANSWER INCOMPLETE: the answer was cut off before it was "
        "complete and has been withheld. Try a narrower question."
    ),
    MODEL_DECLINED: (
        "WITHHELD \u2014 the model declined to answer this request. Rephrase the "
        "question or consult the handbook directly."
    ),
    GENERATION_INCOMPLETE: (
        "WITHHELD \u2014 answer generation did not complete normally and the "
        "answer has been withheld. Please retry."
    ),
}
UNKNOWN_STATUS_NOTICE = (
    "\u26a0 Completion status could not be confirmed (no stop reason returned) "
    "\u2014 check this answer with extra care."
)


def status_from_stop_reason(stop_reason: Optional[str]) -> str:
    """Map an Anthropic ``stop_reason`` to a generation status.

    ``end_turn``/``stop_sequence`` are ``complete``; ``max_tokens`` and
    ``model_context_window_exceeded`` are ``truncated``; ``refusal`` is
    ``declined``; any other value (e.g. ``pause_turn``) is ``incomplete``; an
    absent or None stop reason is ``unknown``.
    """
    if stop_reason is None:
        return STATUS_UNKNOWN
    return _STOP_REASON_STATUS.get(stop_reason, STATUS_INCOMPLETE)


def generation_outcome(status: Optional[str]) -> Optional[str]:
    """Return the terminal outcome for a generation status, else None.

    ``truncated`` -> ``ANSWER_TRUNCATED``, ``declined`` -> ``MODEL_DECLINED``,
    ``incomplete`` -> ``GENERATION_INCOMPLETE``. ``complete``, ``unknown`` and
    None (a legacy result with no status) return None: the caller falls through
    to ``classify``. Any other unrecognised string fails closed to
    ``GENERATION_INCOMPLETE``.
    """
    if status in (None, STATUS_COMPLETE, STATUS_UNKNOWN):
        return None
    if status == STATUS_TRUNCATED:
        return ANSWER_TRUNCATED
    if status == STATUS_DECLINED:
        return MODEL_DECLINED
    return GENERATION_INCOMPLETE


def classify(
    answer: str,
    citations: List[Dict[str, str]],
    citation_check: Dict[str, List[Dict[str, str]]],
) -> str:
    """Classify an answer by how well its citations were verified.

    Args:
        answer: The raw model answer text (used only to detect a refusal).
        citations: The citations extracted from ``answer`` (``extract_citations``
            output); a citation is one ``{"para", "page", "raw"}`` dict.
        citation_check: ``validate_citations`` output — ``{"grounded": [...],
            "ungrounded": [...]}``. The internal key names stay "grounded"/
            "ungrounded"; only the *outcome* vocabulary is "verified".

    Returns:
        Exactly one of ``REFUSAL``, ``CITATIONS_VERIFIED``,
        ``PARTIALLY_VERIFIED``, ``CITATIONS_UNVERIFIED``:

        * an answer that IS the refusal sentence (D32's normalized exact
          match) → ``REFUSAL``, even if a caller passes citation dicts
          alongside it. An answer that merely *contains* the refusal phrase
          plus other text — e.g. the refusal sentence with a stray citation
          bracket appended — is NOT a refusal under D32; it falls through to
          citation verification like any other malformed answer and fails
          closed to ``CITATIONS_UNVERIFIED`` unless its citations verify;
        * ≥1 citation and zero ungrounded → ``CITATIONS_VERIFIED``;
        * ≥1 grounded and ≥1 ungrounded → ``PARTIALLY_VERIFIED``;
        * a non-refusal with zero grounded (this includes the zero-citation
          case) → ``CITATIONS_UNVERIFIED``. This closes the P0: a citation-free
          answer can no longer read as valid.
    """
    # Lazy (function-level) import to break a module-level import cycle:
    # generator.py imports ``classify`` from this module at top level, so if
    # this module imported generator at top level too, importing either module
    # first would deadlock. Deferring the import to call time — by which point
    # generator is fully loaded — keeps generator → grounding a clean top-level
    # import while grounding → generator stays lazy.
    from src.generator import is_refusal

    if is_refusal(answer):
        return REFUSAL

    grounded = citation_check.get("grounded", [])
    ungrounded = citation_check.get("ungrounded", [])

    if citations and not ungrounded:
        return CITATIONS_VERIFIED
    if grounded and ungrounded:
        return PARTIALLY_VERIFIED
    return CITATIONS_UNVERIFIED
