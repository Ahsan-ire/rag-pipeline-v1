"""Pre-retrieval query expansion via a small, fast LLM (Phase 13, D43).

Before the pipeline retrieves chunks for a user question, this module asks
``claude-haiku-4-5`` for up to three alternative phrasings — a handbook-
register rephrasing, a keyword-only variant, and a plain-language paraphrase
— so retrieval sees several vocabulary shapes of the same question instead
of only the user's own wording. This is the fix for the 14 Jul field failure
where natural staff phrasing produced disjoint BM25/vector rankings while
handbook-phrased eval questions retrieved perfectly (see docs/decisions.md
D43): neither retrieval arm can fix a vocabulary mismatch on its own, but a
rewrite that speaks the handbook's own register can land in either arm's top
ranks.

Degrade contract: ``expand_query`` NEVER raises. Any failure along the way —
no API key, an API error, or a response that fails to parse into usable
rewrites — degrades to a zero-rewrite ``Expansion`` whose ``status`` records
why. This is what keeps the keyless CI suite and the offline eval path
(``--skip-refusals --skip-completeness``) at zero API calls: nothing here
raises in a way that would force a caller to add its own generation-call
guard, and ``enabled=False`` skips calling the rewrite LLM at all.

Future seam: ``expand_query``'s signature and degrade contract are designed
to also serve chat-style question condensation later (conversation history
in, a single standalone question out) — that seam is decided here, not yet
built.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

load_dotenv()

logger = logging.getLogger(__name__)

# Rewrite model: a small, fast model is enough for search-query paraphrasing
# and keeps the added per-query latency/cost low relative to the Sonnet 5
# generation call. Alias verified live 14 Jul 2026.
REWRITE_MODEL = "claude-haiku-4-5"
REWRITE_MAX_TOKENS = 300
MAX_REWRITES = 3
MAX_REWRITE_CHARS = 200

# Case-sensitive tag the rewrite model puts on its 4th line to carry the
# INTENT reframe (Phase 14, D50). Matched exactly — no IGNORECASE — like every
# other structural marker in this module (project convention).
INTENT_TAG = "INTENT:"
# Per-line cap for the intent restatement. ``parse_rewrites`` HAS a per-line
# length cap (``MAX_REWRITE_CHARS``), so the intent line MIRRORS it: a
# restatement longer than this is treated as malformed and extracted as None.
# (Had parse_rewrites carried no cap, this would instead be a standalone
# 500-char guard — it does, so we mirror rather than invent a second limit.)
MAX_INTENT_CHARS = MAX_REWRITE_CHARS

# Eval mode label for this feature (Phase 13, D43/D46). Defined here as the
# single source of truth so the evaluator can import it instead of
# hardcoding a second copy of the string.
REWRITE_MODE = "hybrid+rewrite"

# Status vocabulary: WHY `Expansion.rewrites` may be empty. Owned here as
# constants (same convention as src/audit.py's ACTION_* and
# src/grounding.py's outcome constants) so a typo at a call site can't mint
# an untracked bucket.
STATUS_LIVE = "live"
STATUS_NO_KEY = "no_key"
STATUS_API_ERROR = "api_error"
STATUS_PARSE_ERROR = "parse_error"
STATUS_DISABLED = "disabled"
EXPANSION_STATUSES = (
    STATUS_LIVE,
    STATUS_NO_KEY,
    STATUS_API_ERROR,
    STATUS_PARSE_ERROR,
    STATUS_DISABLED,
)


@dataclass(frozen=True)
class Expansion:
    """Result of one query-expansion attempt.

    ``rewrites`` are the EFFECTIVE rewrites — deduped (casefold) and never
    containing the original question — actually usable by the retriever.
    ``status`` records WHY ``rewrites`` may be empty, so the audit log (and
    eval provenance) can tell "expansion off" (``disabled``/``no_key``) apart
    from "expansion broke" (``api_error``/``parse_error``), rather than
    seeing an opaque empty tuple in every case.

    ``intent_rewrite`` (Phase 14, D50) is the model's INTENT reframe — a
    restatement of what the question is fundamentally asking — or ``None`` when
    the model produced none, produced a malformed/overlong one, or produced one
    that merely duplicates the original question or a surface rewrite (deduped
    away, adds no new retrieval signal). It is appended AFTER ``status`` with a
    default so every existing positional ``Expansion(...)`` construction stays
    valid unchanged. It travels on its OWN weight budget in the retriever (it
    is NOT one of ``rewrites``), so it can never enter the surface bundle.
    """

    original: str
    rewrites: Tuple[str, ...]
    model: str
    status: str
    intent_rewrite: Optional[str] = None


REWRITE_SYSTEM_PROMPT = """You expand search queries for a retrieval system over the Law \
Society of Ireland Conveyancing Handbook. Given a user question, output exactly 4 \
numbered lines — three alternative search queries, then an intent restatement:

1) the question rephrased in formal Irish conveyancing / Land Registry terminology, as \
the handbook would phrase it
2) a keyword-only variant (terms of art and key nouns, no filler words)
3) a close plain-language paraphrase
4) INTENT: a restatement that re-frames what the question is FUNDAMENTALLY asking — the \
underlying information need, NOT another rephrasing. For example, re-frame a comparison \
question to name both compared procedures explicitly.

Output ONLY the 4 numbered lines, no preamble."""

REWRITE_PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", REWRITE_SYSTEM_PROMPT),
        ("human", "Question: {question}"),
    ]
)

# Matches one numbered ("1." / "1)") or bulleted ("-"/"*"/"•") line, capturing
# the content after the marker. Anchored to the line start (project
# convention: structural markers anchor to line starts, no IGNORECASE).
_REWRITE_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*(.+)$")

# The leading list-marker alone (no captured content) — stripped from a
# FALLBACK candidate (the no-marked-line-in-response path) so a marker-only
# line like "1." or "-" reduces to "" and is dropped rather than harvested
# verbatim as a rewrite. Same marker alternation as _REWRITE_LINE_RE.
_LEADING_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")

# One layer of wrapping quotes to strip from a parsed rewrite (straight and
# curly pairs) — mirrors the quote-stripping list in generator.is_refusal.
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def get_rewrite_llm() -> ChatAnthropic:
    """Create and return a ChatAnthropic LLM instance for query rewriting.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your-api-key-here":
        raise ValueError(
            "ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key."
        )

    return ChatAnthropic(
        model=REWRITE_MODEL,
        max_tokens=REWRITE_MAX_TOKENS,
        # Haiku 4.5 runs with thinking OFF by default — unlike get_llm()'s
        # Sonnet 5, which runs adaptive thinking on by default — so no
        # `thinking` kwarg is needed here to keep it off.
    )


def _invoke_rewrite(llm: Any, question: str) -> str:
    """Invoke the rewrite chain once for a single question.

    Isolated as its own function so tests patch this seam directly instead
    of fighting the LCEL ``TEMPLATE | llm | StrOutputParser()`` chain.
    """
    chain = REWRITE_PROMPT_TEMPLATE | llm | StrOutputParser()
    return chain.invoke({"question": question})


def _strip_wrapping_quotes(text: str) -> str:
    """Strip one layer of matching wrapping quotes (straight or curly)."""
    if len(text) >= 2:
        for open_q, close_q in _QUOTE_PAIRS:
            if text[0] == open_q and text[-1] == close_q:
                return text[1:-1]
    return text


def extract_intent(text: str) -> Tuple[Optional[str], str]:
    """Split the INTENT-tagged line off a rewrite response BEFORE parse_rewrites.

    Pure function — no LLM calls, no I/O. Runs as a dedicated pre-pass so the
    intent reframe can never leak into the surface-rewrite bundle
    (``parse_rewrites`` stays byte-unchanged and never sees the intent line):
    no double-counting is possible by construction.

    Contract (Phase 14, D50): scan ``text`` line by line; for each line strip
    any leading list-marker / numbering (the same ``1.``/``1)``/``-``/``*``/``•``
    markers ``parse_rewrites`` recognises) and test whether the remainder begins
    with the case-sensitive tag ``INTENT:``. The FIRST such line wins. Its tag
    is stripped and the remainder whitespace-trimmed to yield the restatement.

    Failure modes all yield an intent of ``None``:
      - no line carries the tag (missing) — and NO line is removed;
      - the restatement is empty after trimming (malformed);
      - the restatement exceeds ``MAX_INTENT_CHARS`` (overlong — this cap
        mirrors ``parse_rewrites``' own per-line ``MAX_REWRITE_CHARS`` cap).
    Whenever a tagged line IS present it is removed from the returned text
    REGARDLESS of whether its restatement was usable, so a malformed INTENT
    line can never fall through into ``parse_rewrites`` as a surface rewrite.

    Args:
        text: The raw LLM rewrite response.

    Returns:
        ``(intent, remaining_text)``. ``intent`` is the trimmed restatement or
        ``None`` (missing/malformed/overlong). ``remaining_text`` is ``text``
        with the first tagged line removed (identical to ``text`` when no tagged
        line exists), ready to hand to ``parse_rewrites``.
    """
    if not text:
        return None, text

    lines = text.splitlines()
    for i, line in enumerate(lines):
        # Anchor the tag at the (post-marker) line start — case-sensitive, like
        # every structural marker here. A stray "the INTENT: is unclear" mid-line
        # is NOT a tag line (it does not start with the tag after marker strip).
        after_marker = _LEADING_MARKER_RE.sub("", line)
        if not after_marker.startswith(INTENT_TAG):
            continue
        # First tagged line wins and is removed unconditionally (see docstring).
        remaining = "\n".join(lines[:i] + lines[i + 1 :])
        restatement = after_marker[len(INTENT_TAG) :].strip()
        if not restatement or len(restatement) > MAX_INTENT_CHARS:
            return None, remaining
        return restatement, remaining

    return None, text


def parse_rewrites(text: str) -> List[str]:
    """Parse an LLM rewrite response into a list of candidate rewrites.

    Pure function — no LLM calls, no I/O. Each line is matched against
    ``_REWRITE_LINE_RE`` (a numbered ``1.``/``1)`` or bulleted ``-``/``*``/``•``
    marker). If NO line in the whole response carries such a marker, every
    non-blank line is instead accepted as a plain-text candidate (defensive:
    the model sometimes skips numbering entirely) — but this fallback never
    kicks in when at least one marked line exists, so unmarked preamble text
    ahead of a numbered list is correctly discarded rather than harvested.

    Each candidate is stripped, has one layer of wrapping quotes removed,
    and is then dropped if it is empty, longer than ``MAX_REWRITE_CHARS``,
    carries NO alphabetic character (marker-only noise like ``1.`` or ``-`` is
    not a usable search query), or is a casefold-duplicate of an earlier
    surviving candidate. Fallback (no-marked-line) candidates additionally have
    any leading list-marker stripped first, so a degenerate ``1.\\n2.\\n3.``
    response reduces to zero rewrites rather than the literal markers. The
    result is capped at ``MAX_REWRITES``.
    """
    if not text:
        return []

    lines = text.splitlines()
    matches = [_REWRITE_LINE_RE.match(line) for line in lines]
    has_marker = any(m is not None for m in matches)

    if has_marker:
        candidates = [m.group(1) for m in matches if m is not None]
    else:
        # Defensive fallback: no line carries a marker+content, so every
        # non-blank line is a candidate — but strip any leading list-marker it
        # DOES carry (a lone "1." / "-" that _REWRITE_LINE_RE rejected because
        # nothing followed the marker) so a marker-only line becomes "" and is
        # dropped below, not harvested verbatim.
        candidates = [
            _LEADING_MARKER_RE.sub("", line) for line in lines if line.strip()
        ]

    rewrites: List[str] = []
    seen = set()
    for candidate in candidates:
        cleaned = _strip_wrapping_quotes(candidate.strip()).strip()
        if not cleaned or len(cleaned) > MAX_REWRITE_CHARS:
            continue
        # Drop any candidate with NO alphabetic character (all digits/
        # punctuation): a marker-only "1." or a numbered line whose content is
        # bare digits is not a usable search query, and letting it through would
        # count a degenerate response as live rewrites.
        if not any(ch.isalpha() for ch in cleaned):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        rewrites.append(cleaned)

    return rewrites[:MAX_REWRITES]


def expand_query(question: str, *, llm: Any = None, enabled: bool = True) -> Expansion:
    """Ask the rewrite LLM for alternative phrasings of ``question``.

    Never raises — see the module's degrade contract. ``enabled=False`` skips
    calling the rewrite LLM entirely (including ``get_rewrite_llm``), which is
    what lets a caller disable expansion without needing an API key at all.

    Args:
        question: The user's original question.
        llm: An already-constructed chat model to use instead of building one
            via ``get_rewrite_llm()`` — the seam tests use to avoid a real
            client, and that a future caller could use to share one client
            across many calls.
        enabled: If False, expansion is skipped and ``get_rewrite_llm`` is
            never called.

    Returns:
        An ``Expansion``. ``status`` is one of ``EXPANSION_STATUSES``.
    """
    if not enabled:
        return Expansion(question, (), REWRITE_MODEL, STATUS_DISABLED)

    if llm is None:
        # Decide the missing-key case EXPLICITLY, reading the env var exactly as
        # get_rewrite_llm's own guard does, BEFORE touching the constructor. This
        # keeps the two failure modes cleanly separated: a genuinely absent or
        # placeholder key degrades to STATUS_NO_KEY, while ANY other failure
        # raised while building the client — an unrelated ValueError, a
        # RuntimeError from the SDK, a dependency breakage — degrades to
        # STATUS_API_ERROR. Previously only ValueError was caught, so a
        # RuntimeError escaped (breaking the never-raises contract) and an
        # unrelated constructor ValueError was mislabeled STATUS_NO_KEY.
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or api_key == "your-api-key-here":
            logger.warning("Query expansion skipped: no ANTHROPIC_API_KEY set.")
            return Expansion(question, (), REWRITE_MODEL, STATUS_NO_KEY)
        try:
            llm = get_rewrite_llm()
        except Exception:  # noqa: BLE001 — any construction failure degrades, never raises
            logger.warning("Query expansion failed: the rewrite LLM could not be built.")
            return Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR)

    try:
        raw = _invoke_rewrite(llm, question)
    except Exception:  # noqa: BLE001 — any rewrite-call failure degrades, never raises
        logger.warning("Query expansion failed: the rewrite LLM call raised.")
        return Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR)

    # Pre-pass (Phase 14, D50): peel the INTENT-tagged line off FIRST, then hand
    # the remaining text — never the intent line — to the byte-unchanged
    # parse_rewrites. The intent can therefore never enter the surface bundle.
    intent, remaining = extract_intent(raw)
    parsed = parse_rewrites(remaining)
    question_key = question.casefold()
    effective = tuple(r for r in parsed if r.casefold() != question_key)

    # Dedup the intent reframe: a restatement casefold-equal to the original
    # question, or to any surviving surface rewrite, adds no new retrieval
    # signal (the fusion already has that list), so it collapses to None.
    if intent is not None:
        intent_key = intent.casefold()
        if intent_key == question_key or any(
            intent_key == r.casefold() for r in effective
        ):
            intent = None

    # Any degenerate expansion — zero effective rewrites, whether from an
    # unparseable non-empty response OR an empty/whitespace-only one — must
    # record STATUS_PARSE_ERROR, never STATUS_LIVE. Canonical eval accounting
    # treats a "live" expansion that carries no rewrites as a silent fallback,
    # so a live-but-empty status here would let an inert rewrite model read as
    # a successful expansion (D46). A validly extracted intent is still carried
    # on the returned Expansion — it is an independent signal, and only the
    # surface bundle degraded here.
    if not effective:
        logger.warning("Query expansion produced no usable rewrites (parse failure).")
        return Expansion(question, (), REWRITE_MODEL, STATUS_PARSE_ERROR, intent)

    return Expansion(question, effective, REWRITE_MODEL, STATUS_LIVE, intent)
