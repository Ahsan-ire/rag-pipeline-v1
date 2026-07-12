"""Grounding gate: classify how well an answer's citations are verified.

The gate names ONLY what the system actually VERIFIES — the answer's citation
locators against the paragraphs/pages of the chunks that were retrieved. It says
nothing about whether the underlying legal claims are correct; that is not
something this pipeline can check. The outcome vocabulary is deliberately about
*citation verification* ("VERIFIED"), never about legal validity, so a consumer
can never read "CITATIONS_VERIFIED" as "the law is stated correctly".

The four outcomes are mutually exclusive; ``classify`` returns exactly one.
"""

from typing import Dict, List

# Outcome vocabulary. These are the only strings ``classify`` returns; consumers
# (display policy lives in pipeline.py) branch on them.
REFUSAL = "REFUSAL"
CITATIONS_VERIFIED = "CITATIONS_VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
CITATIONS_UNVERIFIED = "CITATIONS_UNVERIFIED"


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

        * a refusal answer → ``REFUSAL`` (regardless of any citations);
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
