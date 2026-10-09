"""One rendering contract for ``pipeline.query`` (H3), plus the H2/H5 display helpers.

``render`` decides, for a finished generation result, three things at once: the
text to print, the audit ``action``, and the dict ``query()`` returns. Keeping them
in one pure function means they cannot drift apart, and it is the one place where
the draft answer can leak or not, so the leak rules are tested here.

Everything in this module is DISPLAY-ONLY. The uncited-statement hint (H2), the
``Source:`` label and the disclaimer (H5) and the "completion status" line are
never written into ``public_result["answer"]`` or ``answer_chars``, and are never
seen by refusal matching, caveat detection or citation extraction.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.audit import (
    ACTION_BLOCKED_UNVERIFIED,
    ACTION_NO_RESULTS,
    ACTION_REFUSAL_SHOWN,
    ACTION_SHOWN,
    ACTION_SHOWN_UNVERIFIED_OVERRIDE,
    ACTION_SHOWN_WITH_WARNING,
    ACTION_WITHHELD_DECLINED,
    ACTION_WITHHELD_INCOMPLETE,
    ACTION_WITHHELD_TRUNCATED,
)
from src.chunker import locator_label
from src.evaluator import split_sentences
from src.generator import (
    CAVEAT_PREFIX,
    CITATION_RE,
    _citation_matches_chunk,
    is_refusal,
)
from src.grounding import (
    ANSWER_TRUNCATED,
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    GENERATION_INCOMPLETE,
    MODEL_DECLINED,
    PARTIALLY_VERIFIED,
    REFUSAL,
    STATUS_UNKNOWN,
    TERMINAL_OUTCOMES,
    UNKNOWN_STATUS_NOTICE,
    WITHHELD_NOTICES,
    generation_outcome,
)

NO_RESULTS_MESSAGE = "No relevant documents found. Please index some documents first."
STATUS_NOT_RUN = "not_run"

DISCLAIMER = (
    "Research aid — check the cited paragraphs; not legal advice; the source "
    "edition may predate current law."
)
UNCITED_HEADING = "These statements may not be backed by a citation (heuristic):"
REPEATED_CAVEAT_LABEL = "repeated caveat"

BLOCKED_NOTICE = (
    "BLOCKED — CITATIONS UNVERIFIED: the answer was withheld because its "
    "citations could not be verified against the retrieved sources."
)

_TERMINAL_ACTIONS = {
    ANSWER_TRUNCATED: ACTION_WITHHELD_TRUNCATED,
    MODEL_DECLINED: ACTION_WITHHELD_DECLINED,
    GENERATION_INCOMPLETE: ACTION_WITHHELD_INCOMPLETE,
}

# Gap-statement exemption (H2): a unit that merely says the handbook is silent.
_GAP_STARTS = (
    "The extracts do not",
    "The source material does not",
    "This is not covered",
    "The handbook does not",
)
# A hedge turns a gap statement back into a claim ("not covered, but likely 20
# days"), so the exemption is lost. Whole words only (amendment 6).
_HEDGE_RE = re.compile(
    r"\b(?:but|however|likely|probably|generally|usually)\b", re.IGNORECASE
)
_MIN_WORDS = 5
_TITLE_EXTENSIONS = (".pdf", ".txt", ".html", ".htm", ".md", ".docx")


@dataclass(frozen=True)
class RenderFlags:
    """Inputs to ``render`` that are not part of the generation result.

    ``show_unverified`` is the operator override. ``retrieved`` is the retriever's
    output (``{"document", "score", ...}`` dicts): the blocked-path source listing
    and the H5 ``Source:`` label read chunk metadata from it. ``no_results`` renders
    the empty-retrieval path, where there is no generation result at all.
    """

    show_unverified: bool = False
    retrieved: List[Dict[str, Any]] = field(default_factory=list)
    no_results: bool = False


@dataclass(frozen=True)
class Rendered:
    """What ``render`` decided: the text to print, the audit action, the return dict."""

    display_text: str
    action: str
    public_result: Dict[str, Any]


def uncited_statements(answer: str) -> List[str]:
    """Return statements in ``answer`` that may lack a citation (H2 heuristic).

    Display-only: the outcome never changes. Steps: strip one leading exact
    ``CAVEAT_PREFIX`` (as the evaluator does); flag every remaining exact
    occurrence as ``"repeated caveat"``; split with ``evaluator.split_sentences``.
    A unit is flagged when it has no citation locator, is at least five words, does
    not end with ``:``, is not a Markdown heading, and is not a narrow gap
    statement ("The handbook does not ...") free of hedge words. A whole-answer
    refusal flags nothing.

    Documented misses: ``split_sentences`` does not split before a lowercase
    letter, so ``"... [para 1.2, p.3]. then more claims"`` is one cited unit;
    quotes and the exemption's own blind spots are likewise not handled.
    """
    if is_refusal(answer):
        return []
    text = answer.lstrip()
    if text.startswith(CAVEAT_PREFIX):
        text = text[len(CAVEAT_PREFIX):]
    flagged: List[str] = [REPEATED_CAVEAT_LABEL] * text.count(CAVEAT_PREFIX)
    # A newline (not a space) keeps neighbouring sentences from merging.
    text = text.replace(CAVEAT_PREFIX, "\n")
    for unit in split_sentences(text):
        if CITATION_RE.search(unit):
            continue
        if unit.lstrip().startswith("#") or unit.rstrip().endswith(":"):
            continue
        if len(unit.split()) < _MIN_WORDS:
            continue
        if unit.startswith(_GAP_STARTS) and not _HEDGE_RE.search(unit):
            continue
        flagged.append(unit)
    return flagged


def prettify_title(title: str) -> str:
    """Make a ``metadata["title"]`` readable: strip a file extension, ``_`` to space."""
    lowered = title.lower()
    for ext in _TITLE_EXTENSIONS:
        if lowered.endswith(ext):
            title = title[: -len(ext)]
            break
    return title.replace("_", " ").strip()


def source_titles(
    citation_check: Dict[str, List[Dict[str, str]]],
    retrieved: List[Dict[str, Any]],
) -> List[str]:
    """Sorted unique prettified titles behind the verified citations (H5).

    Uses the chunks that match a VERIFIED citation (same rule as the gate); if
    none match, falls back to every retrieved chunk. Chunks without a title add
    nothing.
    """
    matched = [
        r
        for r in retrieved
        if any(
            _citation_matches_chunk(c, [r]) for c in citation_check.get("grounded", [])
        )
    ]
    titles = set()
    for r in matched or retrieved:
        title = (r["document"].metadata or {}).get("title")
        if title:
            pretty = prettify_title(str(title))
            if pretty:
                titles.add(pretty)
    return sorted(titles)


def _retrieved_source_lines(retrieved: List[Dict[str, Any]]) -> List[str]:
    """One locator+pages line per retrieved chunk, no chunk text.

    Reuses ``locator_label`` (D34) so the APPENDIX grammar matches every other
    surface; the page range uses the D21 en-dash. ``page_start`` can be an explicit
    ``None`` (OCR found no printed page), so the guard tests the value, not the key.
    """
    lines = ["\nRetrieved sources (for manual review):"]
    for r in retrieved:
        meta = r["document"].metadata
        section = meta.get("section_number") or "—"
        p_start = meta.get("page_start")
        p_end = meta.get("page_end")
        if p_start is None:
            pages = f"pp.?–{p_end}" if p_end is not None else "p.?"
        elif p_end and p_end != p_start:
            pages = f"pp.{p_start}–{p_end}"
        else:
            pages = f"p.{p_start}"
        lines.append(f"  - {locator_label(section)}  {pages}")
    return lines


def _answer_block(answer: str, sources: list) -> List[str]:
    """The answer body and its extracted citation list, as print-call strings."""
    lines = [f"\nAnswer:\n{answer}"]
    if sources:
        lines.append("\nCitations found:")
        lines.extend(f"  - {s}" for s in sources)
    return lines


def _uncited_block(uncited: List[str]) -> List[str]:
    """The H2 hint lines (empty when nothing is flagged)."""
    if not uncited:
        return []
    return [f"\n{UNCITED_HEADING}"] + [f"  - {u}" for u in uncited]


def render(result: Optional[Dict[str, Any]], flags: RenderFlags) -> Rendered:
    """Decide the display text, audit action and public return dict for one query.

    ``public_result`` has the same key set on every path: ``answer``,
    ``gate_outcome``, ``citations``, ``sources``, ``citation_check``,
    ``source_documents``, ``answer_chars``, ``generation_status``, ``stop_reason``
    and ``uncited_count``. It is built key by key, never by spreading ``result``,
    so a future key that carries draft text cannot leak. Blocked and terminal
    outcomes carry a synthesised notice as ``answer`` (``answer_chars`` records that
    a draft existed). ``uncited_count`` is an int only for VERIFIED, PARTIAL and the
    override draft, and ``None`` everywhere else.
    """
    if flags.no_results:
        return Rendered(
            display_text=f"\n{NO_RESULTS_MESSAGE}",
            action=ACTION_NO_RESULTS,
            public_result={
                "answer": NO_RESULTS_MESSAGE,
                "gate_outcome": None,
                "citations": [],
                "sources": [],
                "citation_check": {"grounded": [], "ungrounded": []},
                "source_documents": [],
                "answer_chars": 0,
                "generation_status": STATUS_NOT_RUN,
                "stop_reason": None,
                "uncited_count": None,
            },
        )

    assert result is not None
    draft = result["answer"]
    citations = result["citations"]
    citation_check = result["citation_check"]
    ungrounded = citation_check["ungrounded"]
    # A result with no status (legacy mocks) is `unknown`.
    status = result.get("generation_status", STATUS_UNKNOWN)
    # H1b: a terminal status wins over any gate outcome, even a supplied one.
    outcome = generation_outcome(status) or result.get("gate_outcome")

    def public(answer: str, uncited: Optional[int]) -> Dict[str, Any]:
        return {
            "answer": answer,
            "gate_outcome": outcome,
            "citations": citations,
            "sources": result["sources"],
            "citation_check": citation_check,
            "source_documents": result["source_documents"],
            "answer_chars": len(draft),
            "generation_status": status,
            "stop_reason": result.get("stop_reason"),
            "uncited_count": uncited,
        }

    unknown_line = (
        [f"\n{UNKNOWN_STATUS_NOTICE}"] if status == STATUS_UNKNOWN else []
    )

    if outcome is None:
        # Legacy fallback: no gate outcome means a pre-gate caller or mock.
        # Exact v1 display, never the `unknown` line (D35).
        lines = _answer_block(draft, result["sources"])
        if not is_refusal(draft) and not citations:
            lines.append(
                "\n⚠ WARNING: this answer contains no citations and could "
                "not be verified\n  against the retrieved sources — treat it "
                "as unverified."
            )
        if ungrounded:
            lines.append(
                "\n⚠ Ungrounded citations (not matched to any retrieved chunk):"
            )
            lines.extend(f"  - {c['raw']}" for c in ungrounded)
        return Rendered("\n".join(lines), ACTION_SHOWN, public(draft, None))

    if outcome in TERMINAL_OUTCOMES:
        # Draft never printed or returned; no sources, no override hint.
        notice = WITHHELD_NOTICES[outcome]
        return Rendered(
            f"\n{notice}", _TERMINAL_ACTIONS[outcome], public(notice, None)
        )

    if outcome == REFUSAL:
        lines = [f"\nAnswer:\n{draft}"] + unknown_line
        return Rendered("\n".join(lines), ACTION_REFUSAL_SHOWN, public(draft, None))

    if outcome in (CITATIONS_VERIFIED, PARTIALLY_VERIFIED):
        uncited = uncited_statements(draft)
        titles = source_titles(citation_check, flags.retrieved)
        lines = [f"\nSource: {'; '.join(titles)}"] if titles else []
        lines += _answer_block(draft, result["sources"]) + unknown_line
        if outcome == CITATIONS_VERIFIED:
            lines.append(
                "\n✓ All citations resolve to a retrieved passage (locator "
                "and page checked — this does not verify the passage "
                "supports the claim)."
            )
            action = ACTION_SHOWN
        else:
            lines.append(
                f"\n⚠ {len(ungrounded)} of {len(citations)} citations could "
                "not be verified against the retrieved sources — check these "
                "before relying on them:"
            )
            lines.extend(f"  - {c['raw']}" for c in ungrounded)
            action = ACTION_SHOWN_WITH_WARNING
        lines += _uncited_block(uncited)
        lines.append(f"\n{DISCLAIMER}")
        return Rendered("\n".join(lines), action, public(draft, len(uncited)))

    # CITATIONS_UNVERIFIED
    if flags.show_unverified:
        uncited = uncited_statements(draft)
        lines = [
            "\nUNVERIFIED DRAFT — do not rely on this text",
            f"\nAnswer:\n{draft}",
        ]
        lines += _retrieved_source_lines(flags.retrieved) + unknown_line
        lines += _uncited_block(uncited)
        lines.append(f"\n{DISCLAIMER}")
        return Rendered(
            "\n".join(lines), ACTION_SHOWN_UNVERIFIED_OVERRIDE, public(draft, len(uncited))
        )

    lines = [
        "\n\U0001f6ab BLOCKED — CITATIONS UNVERIFIED",
        "This answer's citations could not be verified against the retrieved "
        "sources, so it is withheld. This does NOT mean the answer is absent "
        "from the corpus.",
    ]
    # Name the locators that failed verification (locator strings only, never
    # draft text) so a reviewer can triage the block.
    if ungrounded:
        lines.append("\nUnverified citations in the withheld draft:")
        lines.extend(f"  - {c['raw']}" for c in ungrounded)
    lines += _retrieved_source_lines(flags.retrieved)
    lines.append(
        "\nTry rephrasing the question, raising --top-k, or use --show-unverified "
        "to see the unverified draft."
    )
    return Rendered(
        "\n".join(lines), ACTION_BLOCKED_UNVERIFIED, public(BLOCKED_NOTICE, None)
    )
