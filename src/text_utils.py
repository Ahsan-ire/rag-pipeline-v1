"""Pure text helpers shared by the renderer, the evaluator and the eval scorers.

Moved here (Phase 16A-1 item 3) from ``src.evaluator`` (``split_sentences`` and
its helpers) and ``src.render`` (the H2 gap-statement starts and hedge regex) so
that ``src.render`` no longer imports ``src.evaluator``. This module imports
nothing from the project, so it can never take part in an import cycle.
"""

import re
from typing import List

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

# One leading list marker: a bullet (``-``, ``*``, ``•``), a number with ``.`` or
# ``)`` (``1.``, ``12)``), or a parenthesised letter, roman numeral or number
# (``(a)``, ``(iv)``, ``(2)``). The marker (plus any closing emphasis, as in
# ``**1.** ``) must be followed by whitespace, so a word that merely starts with
# one of these characters is never stripped.
_LIST_MARKER_RE = re.compile(r"^(?:[-*\u2022]|\d+[.)]|\((?:[A-Za-z]|[ivxlcIVXLC]+|\d+)\))[*_]*\s+")
# Leading Markdown emphasis: any run of ``*`` / ``_`` (``**bold**``, ``_em_``).
_EMPHASIS_RE = re.compile(r"^[*_]+")


def _strip_list_and_emphasis(unit: str) -> str:
    """Strip leading list markers and Markdown emphasis from ``unit``.

    Repeats to a fixed point (bounded) so nested forms such as
    ``"- **The handbook does not ..."`` or ``"**1.** The handbook ..."`` both
    reduce to the bare sentence. Only the START of the unit is touched.
    """
    text = unit.strip()
    for _ in range(4):
        before = text
        text = _LIST_MARKER_RE.sub("", text)
        text = _EMPHASIS_RE.sub("", text).lstrip()
        if text == before:
            break
    return text


def is_gap_statement(unit: str) -> bool:
    """True if ``unit`` is a narrow gap statement ("the handbook is silent").

    The H2 exemption, shared by render's uncited-statement hint and the eval
    scorers (``src.eval_scoring``). A unit qualifies when, after stripping one
    or more leading list markers (``-``, ``*``, ``•``, ``1.``, ``1)``, ``(a)``)
    and leading Markdown emphasis (``**``, ``*``, ``_``), it starts with one of
    ``_GAP_STARTS`` (case-sensitive, as before) AND the whole unit is free of
    hedge words (``_HEDGE_RE``: but / however / likely / probably / generally /
    usually, whole words, any case). The hedge rule is unchanged from H2: a
    hedge turns a gap statement back into a claim.

    Args:
        unit: One sentence or line, typically from ``split_sentences``.

    Returns:
        Whether the unit is an unhedged gap statement.
    """
    stripped = _strip_list_and_emphasis(unit)
    return stripped.startswith(_GAP_STARTS) and not _HEDGE_RE.search(unit)

# Prose abbreviations whose trailing period must NOT be read as a sentence end.
# Ordered longest-first so a shorter member ("p.") can never pre-empt a longer
# one ("pp.", "paras.") during protection. Deliberately small and legal-prose
# focused (the handbook's own citation style: paragraphs, sections, pages).
_SENTENCE_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "etc.",
    "cf.",
    "viz.",
    "approx.",
    "vs.",
    "paras.",
    "para.",
    "pp.",
    "p.",
    "ss.",
    "s.",
    "no.",
    "art.",
    "ch.",
    "sec.",
)

# A whole bracketed span — a citation locator like ``[Handbook, para 14.8.5,
# p.412]`` — masked as one opaque token before splitting so the periods inside
# it (``p.412``, ``14.8.5``) can never be read as sentence boundaries.
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
# The mask token: two NUL bytes around the index. Contains no ``. ! ?`` or
# whitespace, so it always survives sentence splitting as a single unit and
# can never itself look like a sentence boundary.
_MASK_RE = re.compile("\x00(\\d+)\x00")
# Sentence boundary: a ``.?!`` immediately before whitespace that is followed by
# a capital, an opening quote, or a masked citation (a sentence may open with a
# quotation or, rarely, a citation). Heuristic — see ``split_sentences``.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“\x00])")


def split_sentences(text: str) -> List[str]:
    """Split an answer into sentences for the completeness metric (heuristic).

    This gates nothing — it only counts sentences and locates where citations
    fall, feeding the *syntactic* sentence-citation-coverage figure (D38). It
    is deliberately simple and its limitations are documented, not hidden:

    1. Bracketed citation spans are masked to an opaque token first, so the
       periods inside ``[Handbook, para 14.8.5, p.412]`` cannot be mistaken for
       sentence ends.
    2. A small set of prose abbreviations (``p. pp. para. paras. s. ss. no.
       art. ch. sec. e.g. i.e. etc. cf. viz. approx. vs.``) have their periods
       protected so ``see para. 3`` or ``e.g. a lease`` do not split there.
    3. The text is split on newlines first (so bullet / numbered lists split),
       then within each line on ``.?!`` + whitespace + a capital/quote/citation.
    4. Masks and protected periods are restored, so each returned sentence
       carries its original citation brackets verbatim (needed for the
       downstream ``CITATION_RE`` check).

    Known limitations (accepted — this is a coarse coverage proxy): a sentence
    that genuinely ends in a listed abbreviation (e.g. an answer ending "...the
    answer is no.") will not split after it; a sentence whose terminal period
    sits inside a closing quote (``'... yes.' The next...``) will not split; and
    lower-case sentence starts are not detected. None of these can cause a
    false refusal or block — the metric is descriptive only.

    Args:
        text: The answer text (may be empty, whitespace, or multi-line).

    Returns:
        A list of non-empty, stripped sentence strings in order; ``[]`` for
        empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []

    # 1. Mask bracketed citation spans to opaque tokens.
    masked_citations: List[str] = []

    def _mask(match: "re.Match[str]") -> str:
        masked_citations.append(match.group(0))
        return f"\x00{len(masked_citations) - 1}\x00"

    masked = _BRACKET_RE.sub(_mask, text)

    # 2. Protect abbreviation periods (longest-first; case-insensitive but the
    #    matched casing is preserved — only the periods become the sentinel).
    for abbr in _SENTENCE_ABBREVIATIONS:
        pattern = r"\b" + re.escape(abbr)
        masked = re.sub(
            pattern,
            lambda m: m.group(0).replace(".", "\x01"),
            masked,
            flags=re.IGNORECASE,
        )

    # 3. Newline split first (lists), then sentence split within each line.
    sentences: List[str] = []
    for line in masked.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT_RE.split(line):
            part = part.strip()
            if part:
                sentences.append(part)

    # 4. Unmask: restore protected periods, then the citation brackets.
    restored: List[str] = []
    for sentence in sentences:
        sentence = sentence.replace("\x01", ".")
        sentence = _MASK_RE.sub(
            lambda m: masked_citations[int(m.group(1))], sentence
        )
        restored.append(sentence)
    return restored
