"""Legal-aware document chunking module.

Two strategies, routed by ``document_type`` (D3):

* ``chunk_legal_document`` — the original legislation strategy (PART / Section),
  retained untouched for that document type.
* ``chunk_handbook`` — the handbook strategy (Phase 2 / D19-D21): CHAPTER markers,
  decimal-numbered headings, per-chapter appendices, and page-mapped citations.
"""

import logging
import re
import string
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.ingest import PageSpan, page_range

logger = logging.getLogger(__name__)

# Irish legislative structure patterns
PART_PATTERN = re.compile(r"\n(?=PART\s+[IVXLCDM]+)", re.IGNORECASE)
SECTION_PATTERN = re.compile(r"\n(?=(?:Section\s+\d+\.?|\d+\.[\u2014\u2013\-]))", re.IGNORECASE)
SUBSECTION_PATTERN = re.compile(r"\n(?=\(\d+\)\s)")

# Approximate chars per token for English text
CHARS_PER_TOKEN = 4


def chunk_legal_document(
    doc: Document, chunk_size: int = 600, chunk_overlap: int = 120
) -> List[Document]:
    """Split a legal document into chunks, respecting legal structure.

    Args:
        doc: A LangChain Document containing the full text.
        chunk_size: Target chunk size in tokens.
        chunk_overlap: Overlap between chunks in tokens.

    Returns:
        List of Document chunks with enriched metadata.
    """
    chunks = _split_by_legal_structure(doc.page_content, doc.metadata)

    # Apply fallback splitter to any oversized chunks
    chunks = _apply_fallback_splitter(chunks, chunk_size, chunk_overlap)

    # Prepend summary context to each chunk (SAC technique)
    chunks = _prepend_summary(chunks)

    return chunks


def _split_by_legal_structure(text: str, metadata: dict) -> List[Document]:
    """Split text by legal structural boundaries hierarchically.

    Splits first by PART, then by Section within each part.
    """
    parts = PART_PATTERN.split(text)

    chunks = []
    for part in parts:
        if not part.strip():
            continue

        # Extract part number if present
        part_match = re.match(r"(PART\s+[IVXLCDM]+)", part, re.IGNORECASE)
        parent_section = part_match.group(1) if part_match else ""

        # Split by section within this part
        sections = SECTION_PATTERN.split(part)

        for section in sections:
            if not section.strip():
                continue

            # Extract section number
            section_match = re.match(
                r"(?:Section\s+(\d+)\.?|(\d+)\.[\u2014\u2013\-])", section, re.IGNORECASE
            )
            if section_match:
                section_number = section_match.group(1) or section_match.group(2)
            else:
                section_number = ""

            chunk_metadata = {
                **metadata,
                "section_number": section_number,
                "parent_section": parent_section,
            }

            chunks.append(
                Document(page_content=section.strip(), metadata=chunk_metadata)
            )

    # If no structural splits were found, return the whole text as one chunk
    if not chunks:
        chunks = [
            Document(
                page_content=text.strip(),
                metadata={**metadata, "section_number": "", "parent_section": ""},
            )
        ]

    return chunks


def _apply_fallback_splitter(
    chunks: List[Document], chunk_size: int, chunk_overlap: int
) -> List[Document]:
    """Re-split any chunks that exceed the target size using RecursiveCharacterTextSplitter."""
    char_size = chunk_size * CHARS_PER_TOKEN
    char_overlap = chunk_overlap * CHARS_PER_TOKEN

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=char_size,
        chunk_overlap=char_overlap,
        separators=["\n\n", "\n", ". ", " "],
        length_function=len,
    )

    result = []
    for chunk in chunks:
        if len(chunk.page_content) > char_size:
            sub_chunks = splitter.split_documents([chunk])
            result.extend(sub_chunks)
        else:
            result.append(chunk)

    return result


def _prepend_summary(chunks: List[Document]) -> List[Document]:
    """Prepend a contextual prefix to each chunk (lightweight SAC technique).

    This helps disambiguate chunks from different documents that may have
    similar boilerplate text (common in legal documents like contracts/NDAs).
    """
    for chunk in chunks:
        title = chunk.metadata.get("title", "Unknown")
        section = chunk.metadata.get("section_number", "")
        parent = chunk.metadata.get("parent_section", "")

        parts = [f"From: {title}"]
        if parent:
            parts.append(parent)
        if section:
            parts.append(f"Section {section}")

        prefix = "[" + ", ".join(parts) + "] "
        chunk.page_content = prefix + chunk.page_content

    return chunks


# ============================================================================
# Handbook chunking strategy (Phase 2 / D19-D21)
# ============================================================================
#
# The handbook is structured as ``CHAPTER N`` markers (all-caps, standalone
# lines — D10), decimal-numbered headings (``N.M`` down to ``N.M.O.P``),
# per-chapter in-body ``APPENDIX N.M`` lines, and a trailing ``INDEX``. Front
# matter precedes the first CHAPTER marker and is excluded. ``chunk_handbook``
# segments the *cleaned* text at those structural boundaries, tracking every
# boundary as a character offset into ``clean_text`` so ``page_range`` (from the
# page map) can attach exact printed-page citations. Runt and oversize segments
# are then reconciled (D20) and each surviving segment becomes a Document with a
# contextual citation prefix (D21).

HANDBOOK_CHAPTER = re.compile(r"^CHAPTER (\d{1,2})$", re.MULTILINE)
# The number and title must sit on the SAME physical line: [^\S\n]+ is horizontal
# whitespace only. A plain \s+ would span a newline, letting a bare cross-reference
# number ("see 3.2\n...") scavenge the next line as a spurious heading — and it would
# disagree with the single-line _is_heading_line matcher used in title extraction.
HANDBOOK_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){1,3})[^\S\n]+(\S.*)$", re.MULTILINE)
HANDBOOK_APPENDIX = re.compile(r"^APPENDIX (\d{1,2}\.\d{1,3})\b\s*(.*)$", re.MULTILINE)
HANDBOOK_INDEX = re.compile(r"^INDEX$", re.MULTILINE)

# Guard (ii): a genuine heading title opens with a capital, a digit, a straight
# or curly quote, or the lowercase-e product prefix (``eRegistration``).
_HEADING_START_OK = re.compile(r"[A-Z0-9\"'“”‘’]|e[A-Z]")

RUNT_CHAR_THRESHOLD = 600          # segments shorter than this are runts (D20)
OVERSIZE_CHAR_THRESHOLD = 4000     # segments longer than this are re-split (D20)
APPENDIX_STUB_CHAR_THRESHOLD = 50  # title-only appendix lines merge backward (D19)


@dataclass
class _Segment:
    """A structural segment of the handbook, addressed by char offsets.

    ``start`` / ``end`` are a half-open ``[start, end)`` slice of ``clean_text``;
    adjacent segments are contiguous, so merging two is just extending ``end``
    and ``clean_text[start:end]`` remains the exact segment text.
    """

    chap: int
    chap_title: str
    secnum: str          # "" for a chapter intro; "3.2.1"; or "APPENDIX 6.1"
    heading: str
    is_intro: bool
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def chunk_handbook(
    clean_text: str,
    page_map: List[PageSpan],
    metadata: dict,
    chunk_size: int = 600,
    chunk_overlap: int = 120,
) -> List[Document]:
    """Chunk the cleaned handbook text into citation-ready Documents (D19-D21).

    Args:
        clean_text: The cleaned full text from ``ingest.extract_pdf``.
        page_map: The page map from the same extraction — used to attach printed
            ``page_start`` / ``page_end`` to each chunk.
        metadata: Base document metadata (source, title, document_type, date).
        chunk_size: Target size, in tokens, used when re-splitting oversize
            segments (``chunk_size * 4`` chars). The runt (600 chars) and
            oversize-trigger (4000 chars) thresholds are fixed (D20).
        chunk_overlap: Overlap, in tokens, for oversize re-splitting.

    Returns:
        A list of chunk Documents, each carrying a contextual citation prefix
        and the D21 metadata keys.

    Raises:
        ValueError: if no ``CHAPTER N`` markers are present — a loud failure so a
            mis-routed ``--type`` cannot silently fall through (the D3 lesson).
    """
    return _chunk_handbook_impl(
        clean_text, page_map, metadata, chunk_size, chunk_overlap, None
    )


def _chunk_handbook_impl(
    clean_text: str,
    page_map: List[PageSpan],
    metadata: dict,
    chunk_size: int,
    chunk_overlap: int,
    log: Optional["AbsorptionLog"],
) -> List[Document]:
    """The one handbook code path, shared by :func:`chunk_handbook` and
    :func:`chunk_handbook_with_absorption`.

    ``log`` is the optional absorption side channel (Phase 16A-1 item 7). When it
    is ``None`` every helper takes exactly its pre-16A path; when it is given,
    the helpers only *append observations* to it — they never read it back, so
    the segments and Documents produced are identical either way.
    """
    markers = _find_chapter_markers(clean_text)
    body_end = _find_body_end(clean_text, markers)
    segments = _segment_body(clean_text, markers, body_end)
    if log is not None:
        log._begin(clean_text, segments)
    segments = _merge_appendix_stubs(segments, log)
    segments = _merge_runts(segments, log)
    segments = _merge_trailing_runts(segments, log)
    if log is not None:
        log._resolve(segments)
    return _build_documents(
        segments, clean_text, page_map, metadata, chunk_size, chunk_overlap, log
    )


# --- Structural discovery -----------------------------------------------------

def _find_chapter_markers(clean_text: str) -> List[Tuple[int, int, int]]:
    """Return ``(chapter_number, marker_start, marker_end)`` for each CHAPTER.

    Raises ValueError when none are found (D19 sanity gate); logs a warning if
    the numbers are not strictly ascending (a sequence violation reported by
    the D23 acceptance metrics, not a hard failure).
    """
    markers = [
        (int(m.group(1)), m.start(), m.end())
        for m in HANDBOOK_CHAPTER.finditer(clean_text)
    ]
    if not markers:
        raise ValueError(
            "chunk_handbook found no 'CHAPTER N' markers. This strategy is for "
            "the handbook corpus; check the --type flag (use --type legislation "
            "for PART/Section documents)."
        )
    numbers = [n for n, _, _ in markers]
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        logger.warning("Chapter markers are not strictly ascending: %s", numbers)
    logger.info("chunk_handbook: %d chapter markers %s", len(markers), numbers)
    return markers


def _find_body_end(clean_text: str, markers: List[Tuple[int, int, int]]) -> int:
    """Chunkable body ends at the first ``INDEX`` line after the last chapter."""
    match = HANDBOOK_INDEX.search(clean_text, markers[-1][1])
    return match.start() if match else len(clean_text)


def _segment_body(
    clean_text: str, markers: List[Tuple[int, int, int]], body_end: int
) -> List[_Segment]:
    """Cut each chapter region into an intro segment plus one per heading/appendix."""
    segments: List[_Segment] = []
    for idx, (chapter, marker_start, marker_end) in enumerate(markers):
        region_end = markers[idx + 1][1] if idx + 1 < len(markers) else body_end
        region_end = min(region_end, body_end)
        if region_end <= marker_start:
            continue  # last chapter fully inside the index tail — nothing to chunk

        title_search = marker_end + 1 if clean_text[marker_end:marker_end + 1] == "\n" else marker_end
        chap_title, content_start = _extract_chapter_title(
            clean_text, title_search, region_end, chapter
        )

        boundaries: List[Tuple[int, str, str]] = []  # (offset, section_number, heading)
        for m in HANDBOOK_HEADING.finditer(clean_text, content_start):
            if m.start() >= region_end:
                break
            if _heading_passes_guards(m.group(1), m.group(2), m.group(0), chapter):
                boundaries.append((m.start(), m.group(1), m.group(2).strip()))
        for m in HANDBOOK_APPENDIX.finditer(clean_text, content_start):
            if m.start() >= region_end:
                break
            same_line = m.group(2).strip()
            heading = same_line or _first_nonempty_after(clean_text, m.end(), region_end)
            boundaries.append((m.start(), "APPENDIX " + m.group(1), heading))
        boundaries.sort(key=lambda b: b[0])

        first_boundary = boundaries[0][0] if boundaries else region_end
        segments.append(
            _Segment(chapter, chap_title, "", chap_title, True, marker_start, first_boundary)
        )
        for bi, (b_start, secnum, heading) in enumerate(boundaries):
            b_end = boundaries[bi + 1][0] if bi + 1 < len(boundaries) else region_end
            segments.append(
                _Segment(chapter, chap_title, secnum, heading, False, b_start, b_end)
            )
    return segments


def _extract_chapter_title(
    clean_text: str, search_start: int, region_end: int, chapter: int
) -> Tuple[str, int]:
    """Return ``(title, content_start)`` for a chapter.

    The title is the consecutive ALL-CAPS non-empty lines after the CHAPTER
    marker, up to the first blank line, heading/appendix, mixed-case line, or a
    3-line cap. Chapter titles in this corpus are ALL-CAPS, so a line containing
    a lowercase letter marks the start of body prose — several chapters (e.g. 3,
    5) open with a mixed-case epigraph directly under the title with no blank
    line, and without this guard that epigraph would be folded into the title
    and pollute every chunk's citation prefix. ``content_start`` is where section
    scanning begins (the line the title stopped at); any epigraph therefore stays
    in the chapter body, not the citation.
    """
    title_lines: List[str] = []
    content_start = region_end
    for line_start, line in _iter_lines(clean_text, search_start, region_end):
        stripped = line.strip()
        stop = (
            not stripped
            or _is_heading_line(line, chapter)
            or bool(HANDBOOK_APPENDIX.match(line))
            or any(c.islower() for c in stripped)  # mixed-case → epigraph/prose, not a title
            or len(title_lines) >= 3
        )
        if stop:
            content_start = line_start
            break
        title_lines.append(stripped)
    return _title_case(" ".join(title_lines)), content_start


def _heading_passes_guards(number: str, title: str, line: str, chapter: int) -> bool:
    """Apply D19's four heading guards; return True if the line is a real heading."""
    components = number.split(".")
    if int(components[0]) != chapter:                       # (i) belongs here
        return False
    if any(len(c) > 1 and c[0] == "0" for c in components):  # (iii) no leading zeros
        return False
    if not _HEADING_START_OK.match(title):                  # (ii) valid opener
        return False
    if re.search(r"\.{3,}", line):                          # (iv) not a dot-leader
        return False
    return True


def _is_heading_line(line: str, chapter: int) -> bool:
    """True if ``line`` (a single physical line) is a guard-passing heading."""
    m = HANDBOOK_HEADING.match(line)
    return bool(m and _heading_passes_guards(m.group(1), m.group(2), line, chapter))


# --- Segment reconciliation (D20) ---------------------------------------------

def _merge_appendix_stubs(
    segments: List[_Segment], log: Optional["AbsorptionLog"] = None
) -> List[_Segment]:
    """Fold title-only ``APPENDIX`` lines backward into the preceding segment.

    The appendix form facsimiles are not in the text layer (D17), so an appendix
    boundary is usually just its title line — a citation-less stub. Merging it
    backward (never across a chapter seam) attaches it to the last real section
    rather than emitting an orphan chunk.
    """
    result: List[_Segment] = []
    for seg in segments:
        if (
            result
            and seg.secnum.startswith("APPENDIX")
            and seg.length < APPENDIX_STUB_CHAR_THRESHOLD
            and result[-1].chap == seg.chap
        ):
            if log is not None:
                log._absorbed(KIND_APPENDIX_STUB, "appendix_backward", seg)
            result[-1].end = seg.end
        else:
            result.append(replace(seg))
    return result


def _merge_runts(
    segments: List[_Segment], log: Optional["AbsorptionLog"] = None
) -> List[_Segment]:
    """Merge runt (<600 char) segments forward (D20).

    A furniture chapter intro absorbs its first real section and *adopts that
    section's identity* (the chapter title already lives in the prefix). Any
    other runt merges forward only into a *descendant* section (keeping its own,
    parent, identity — hierarchically true); a runt followed by a sibling stays
    standalone, because a correct citation beats a size floor. Merging never
    crosses a chapter seam.
    """
    result: List[_Segment] = []
    n = len(segments)
    i = 0
    while i < n:
        cur = replace(segments[i])
        while cur.length < RUNT_CHAR_THRESHOLD and i + 1 < n:
            nxt = segments[i + 1]
            if nxt.chap != cur.chap:
                break  # never merge forward across a chapter seam
            if cur.is_intro:
                if log is not None:
                    # The intro's (empty) label is the one that disappears.
                    log._absorbed(KIND_RUNT_MERGE, "intro_adopt", cur)
                cur.secnum, cur.heading, cur.is_intro = nxt.secnum, nxt.heading, nxt.is_intro
                cur.end = nxt.end
                i += 1
                continue
            if cur.secnum and nxt.secnum.startswith(cur.secnum + "."):
                if log is not None:
                    log._absorbed(KIND_RUNT_MERGE, "descendant_forward", nxt)
                cur.end = nxt.end  # descendant merge: keep the parent's identity
                i += 1
                continue
            break  # sibling / unrelated: this runt stays standalone
        result.append(cur)
        i += 1
    return result


def _merge_trailing_runts(
    segments: List[_Segment], log: Optional["AbsorptionLog"] = None
) -> List[_Segment]:
    """Merge a runt that is the *last* segment of its chapter backward (D20).

    Runs to a fixed point so a chain of trailing runts collapses into the last
    substantial section of the chapter.
    """
    result = [replace(s) for s in segments]
    changed = True
    while changed:
        changed = False
        for i in range(len(result) - 1, 0, -1):
            seg = result[i]
            last_in_chapter = i == len(result) - 1 or result[i + 1].chap != seg.chap
            prev = result[i - 1]
            if seg.length < RUNT_CHAR_THRESHOLD and last_in_chapter and prev.chap == seg.chap:
                if log is not None:
                    log._absorbed(KIND_RUNT_MERGE, "trailing_backward", seg)
                prev.end = seg.end
                result.pop(i)
                changed = True
                break
    return result


# --- Document assembly (D21) --------------------------------------------------

def _build_documents(
    segments: List[_Segment],
    clean_text: str,
    page_map: List[PageSpan],
    metadata: dict,
    chunk_size: int,
    chunk_overlap: int,
    log: Optional["AbsorptionLog"] = None,
) -> List[Document]:
    """Turn reconciled segments into prefixed, page-cited Document chunks.

    Oversize segments (>4000 chars) are re-split to ``chunk_size`` tokens with
    the fallback splitter; each sub-chunk's page range is recovered by locating
    it in ``clean_text`` (sub-chunks are verbatim substrings), falling back to
    the parent segment's range on a find-miss. The prefix is prepended *after*
    splitting so each sub-chunk cites its own pages.
    """
    doc_title = _display_title(metadata.get("title", ""))
    char_size = chunk_size * CHARS_PER_TOKEN
    char_overlap = chunk_overlap * CHARS_PER_TOKEN
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=char_size,
        chunk_overlap=char_overlap,
        separators=["\n\n", "\n", ". ", " "],
        length_function=len,
    )
    base = {k: metadata.get(k) for k in ("source", "title", "document_type", "date")}

    docs: List[Document] = []
    for seg_index, seg in enumerate(segments):
        seg_text = clean_text[seg.start:seg.end]
        seg_pages = _page_citation(page_map, seg.start, seg.end)
        if len(seg_text) > OVERSIZE_CHAR_THRESHOLD:
            cursor = seg.start
            for piece in splitter.split_text(seg_text):
                pos = clean_text.find(piece, cursor)
                if pos == -1:
                    pos = clean_text.find(piece, seg.start)
                if pos != -1:
                    pages = _page_citation(page_map, pos, pos + len(piece))
                    # Advance the cursor to near the next piece's true start (they
                    # overlap by at most char_overlap). Advancing by a full stride
                    # rather than +1 stops find() from re-locking onto an earlier
                    # occurrence of repeated phrasing.
                    cursor = max(pos + 1, pos + len(piece) - char_overlap)
                else:
                    pages = seg_pages  # inherit the parent's range on a find-miss
                if log is not None:
                    # The side channel records only a hit inside this segment:
                    # an unbounded find() can land in a later segment with the
                    # same text, which would credit the wrong sections. Pages
                    # (production metadata) are unchanged (gate round 5, CR7).
                    if pos != -1 and seg.start <= pos and pos + len(piece) <= seg.end:
                        log._chunk(seg_index, seg, ORIGIN_OVERSIZE_SPLIT, pos, pos + len(piece))
                    else:
                        log._chunk(seg_index, seg, ORIGIN_FIND_MISS, None, None)
                docs.append(_make_doc(piece.strip(), base, seg, doc_title, pages))
        else:
            if log is not None:
                log._chunk(seg_index, seg, ORIGIN_SEGMENT, seg.start, seg.end)
            docs.append(_make_doc(seg_text.strip(), base, seg, doc_title, seg_pages))
    return docs


def _make_doc(
    body: str,
    base: dict,
    seg: _Segment,
    doc_title: str,
    pages: Tuple[Optional[int], Optional[int]],
) -> Document:
    """Build one chunk Document with the D21 metadata keys and citation prefix."""
    p_start, p_end = pages
    prefix = _prefix(doc_title, seg.chap, seg.chap_title, seg.secnum, p_start, p_end)
    meta = {
        **base,
        "chapter_number": seg.chap,
        "chapter_title": seg.chap_title,
        "section_number": seg.secnum,
        "heading": seg.heading,
        "page_start": p_start,
        "page_end": p_end,
    }
    return Document(page_content=prefix + body, metadata=meta)


def locator_label(secnum: str) -> str:
    """Render a section number as its in-citation locator segment.

    The single locator grammar every display surface follows (D34): a numbered
    paragraph becomes ``para 3.2.1``; an ``APPENDIX`` section renders verbatim
    (``APPENDIX 14.1``, never ``para APPENDIX 14.1``). The retriever's compact
    header and the CLI's ``--verbose`` listing call this instead of restating
    the rule, so the three surfaces cannot drift apart.
    """
    return secnum if secnum.startswith("APPENDIX") else f"para {secnum}"


def _prefix(
    doc_title: str,
    chapter: int,
    chap_title: str,
    secnum: str,
    p_start: Optional[int],
    p_end: Optional[int],
) -> str:
    """Build the contextual citation prefix, e.g.
    ``[Conveyancing Handbook, Ch.3 Registration Of Title, para 3.2.1, p.87] ``.

    Omits the ``para`` component for a chapter intro, renders an ``APPENDIX``
    section verbatim, and omits the page component when the printed page is None.
    """
    parts = [doc_title, f"Ch.{chapter} {chap_title}".rstrip()]
    if secnum:
        parts.append(locator_label(secnum))
    page = _page_string(p_start, p_end)
    if page:
        parts.append(page)
    return "[" + ", ".join(parts) + "] "


# --- Absorption side channel (Phase 16A-1 item 7, D69) -------------------------
#
# D20's reconciliation passes fold some sections' text into a neighbouring chunk
# whose ``section_number`` is a different label (D54's "absorbed labels"). The
# text is retrievable; only the label is gone. Rather than change chunk metadata
# (D54's route, re-deferred: it would move production metadata, citations, the
# index and the gate), the eval instrument gets a SIDE CHANNEL: an opt-in log of
# every absorption and of every final chunk's span, built by observing the one
# real chunking code path. Chunks, their order, text, metadata and ids are
# byte-identical with or without it (tests/test_p16w_absorbed.py proves it).
#
# Semantics relied on (src/chunker.py reconciliation passes, in run order):
#
# * ``_merge_appendix_stubs`` — an ``APPENDIX`` segment shorter than 50 chars is
#   folded backward into the preceding same-chapter segment, which keeps its
#   label. Absorbed label: the ``APPENDIX N.M`` stub.        rule "appendix_backward"
# * ``_merge_runts`` — a runt (<600 chars) merges forward:
#   - a chapter intro (label ``""``) absorbs the next section and ADOPTS its
#     label, so the label that disappears is the intro's empty one. Recorded for
#     completeness; never an alias (an intro has no section number). "intro_adopt"
#   - any other runt absorbs a *descendant* (``next.startswith(cur + ".")``) and
#     keeps its own label. Absorbed label: the descendant.  "descendant_forward"
# * ``_merge_trailing_runts`` — a runt that is the last segment of its chapter
#   merges backward into the preceding same-chapter segment (to a fixed point).
#   Absorbed label: the trailing runt.                     "trailing_backward"
#
# The ABSORBED SPAN is the absorbed label's OWN text — its segment exactly as
# ``_segment_body`` cut it, before any reconciliation (so a stub that an absorbed
# section had itself absorbed is not part of that section's span) — trimmed of
# surrounding whitespace, as ``[start, end)`` offsets into ``clean_text``. The
# ABSORBING section is the label of the FINAL reconciled segment that holds the
# span (merges chain: 3.2.1 → 3.2 → 3.1 resolves 3.2.1's absorber to 3.1).
#
# Each final chunk's span is recorded through the D20 oversize re-split: a
# whole segment's trimmed span; an oversize sub-chunk's located span (the same
# ``clean_text.find`` position production uses for its page range); and, on a
# find-miss, no span at all.

KIND_RUNT_MERGE = "runt_merge"
KIND_APPENDIX_STUB = "appendix_stub"

ORIGIN_SEGMENT = "segment"                # the whole reconciled segment, unsplit
ORIGIN_OVERSIZE_SPLIT = "oversize_split"  # a located sub-chunk of an oversize segment
ORIGIN_FIND_MISS = "find_miss"            # a sub-chunk find() could not locate


@dataclass(frozen=True)
class Absorption:
    """One D20 absorption: a section whose label no chunk carries any more.

    Attributes:
        kind: ``"runt_merge"`` or ``"appendix_stub"``.
        rule: which D20 rule fired — ``"appendix_backward"``, ``"intro_adopt"``,
            ``"descendant_forward"`` or ``"trailing_backward"``.
        absorbed_section: the label that disappeared (``""`` for a chapter
            intro, ``"APPENDIX 6.1"`` for a stub, else a decimal number).
        absorbing_section: the label of the final segment that now holds it.
        chapter: the chapter number (merges never cross a chapter seam).
        start: trimmed span start (offset into ``clean_text``).
        end: trimmed span end (exclusive).
        segment_index: index of the final reconciled segment holding the span.
    """

    kind: str
    rule: str
    absorbed_section: str
    absorbing_section: str
    chapter: int
    start: int
    end: int
    segment_index: int


@dataclass(frozen=True)
class ChunkSpan:
    """Where one final chunk's body sits in ``clean_text``.

    Attributes:
        chunk_index: position in the returned chunk list.
        segment_index: the final reconciled segment the chunk came from.
        section: the chunk's ``section_number`` (the segment's label).
        origin: ``"segment"``, ``"oversize_split"`` or ``"find_miss"``.
        start: trimmed body span start, or ``None`` on a find-miss.
        end: trimmed body span end (exclusive), or ``None`` on a find-miss.
    """

    chunk_index: int
    segment_index: int
    section: str
    origin: str
    start: Optional[int]
    end: Optional[int]


@dataclass
class AbsorptionLog:
    """The side channel filled by :func:`chunk_handbook_with_absorption`.

    ``absorptions`` is in the order the passes fired; ``chunks`` is aligned
    one-to-one with the returned chunk list. The underscore methods are the
    chunker's write hooks; nothing in the chunker ever reads the log back.
    """

    absorptions: List[Absorption] = field(default_factory=list)
    chunks: List[ChunkSpan] = field(default_factory=list)
    _text: str = field(default="", repr=False)
    _originals: List[Tuple[int, int, str, bool]] = field(default_factory=list, repr=False)
    _pending: List[Tuple[str, str, str, int, int, int]] = field(
        default_factory=list, repr=False
    )

    # -- write hooks (called from the chunking path) --------------------------

    def _begin(self, clean_text: str, segments: Sequence[_Segment]) -> None:
        """Snapshot the pre-reconciliation segments (each label's own text)."""
        if self._originals or self.absorptions or self.chunks:
            raise RuntimeError("an AbsorptionLog records exactly one chunking run")
        self._text = clean_text
        self._originals = [(s.start, s.end, s.secnum, s.is_intro) for s in segments]

    def _absorbed(self, kind: str, rule: str, seg: _Segment) -> None:
        """Record that ``seg``'s label is being absorbed (span resolved later)."""
        own_start, own_end = self._own_span(seg)
        start, end = _trimmed_span(self._text, own_start, own_end)
        self._pending.append((kind, rule, seg.secnum, seg.chap, start, end))

    def _resolve(self, final_segments: Sequence[_Segment]) -> None:
        """Attach each absorption to the final segment that holds its span."""
        for kind, rule, absorbed, chap, start, end in self._pending:
            index = next(
                (
                    i for i, s in enumerate(final_segments)
                    if s.start <= start and end <= s.end
                ),
                None,
            )
            if index is None:
                raise RuntimeError(
                    f"absorbed span [{start}, {end}) of {absorbed!r} is in no "
                    "final segment — the D20 passes are no longer contiguous"
                )
            self.absorptions.append(
                Absorption(
                    kind, rule, absorbed, final_segments[index].secnum, chap,
                    start, end, index,
                )
            )
        self._pending = []

    def _chunk(
        self,
        segment_index: int,
        seg: _Segment,
        origin: str,
        raw_start: Optional[int],
        raw_end: Optional[int],
    ) -> None:
        """Record the next chunk's span (trimmed exactly as its body is)."""
        if raw_start is None or raw_end is None:
            start: Optional[int] = None
            end: Optional[int] = None
        else:
            start, end = _trimmed_span(self._text, raw_start, raw_end)
        self.chunks.append(
            ChunkSpan(len(self.chunks), segment_index, seg.secnum, origin, start, end)
        )

    def _own_span(self, seg: _Segment) -> Tuple[int, int]:
        """The original (pre-merge) slice of the segment that carries ``seg``'s label.

        A merged segment's label always comes from one original segment inside
        its range: its own first one, or — after an intro adopts a section's
        label — the adopted section, which is the first original carrying that
        label after the intro. So the first original in range with the same
        ``(secnum, is_intro)`` is the label's own text.
        """
        for o_start, o_end, secnum, is_intro in self._originals:
            if seg.start <= o_start < seg.end and secnum == seg.secnum and is_intro == seg.is_intro:
                return o_start, o_end
        raise RuntimeError(f"no original segment carries label {seg.secnum!r}")


def _trimmed_span(text: str, start: int, end: int) -> Tuple[int, int]:
    """Shrink ``[start, end)`` past leading/trailing whitespace (``str.strip`` rules)."""
    piece = text[start:end]
    stripped = piece.strip()
    if not stripped:
        return start, start
    lead = len(piece) - len(piece.lstrip())
    return start + lead, start + lead + len(stripped)


def chunk_handbook_with_absorption(
    clean_text: str,
    page_map: List[PageSpan],
    metadata: dict,
    chunk_size: int = 600,
    chunk_overlap: int = 120,
) -> Tuple[List[Document], AbsorptionLog]:
    """Chunk exactly as :func:`chunk_handbook` and also return the absorption log.

    The chunks are byte-identical to ``chunk_handbook(clean_text, page_map,
    metadata, chunk_size, chunk_overlap)`` — same code path, the log only
    observes. Feed the result to :func:`absorbed_sections_by_chunk` together
    with :func:`production_chunk_ids` to get the per-chunk alias map.

    Returns:
        ``(chunks, log)``; ``log.chunks[i]`` describes ``chunks[i]``.
    """
    log = AbsorptionLog()
    docs = _chunk_handbook_impl(
        clean_text, page_map, metadata, chunk_size, chunk_overlap, log
    )
    if len(log.chunks) != len(docs):  # defensive: the alignment is the contract
        raise RuntimeError("absorption log is not aligned with the chunk list")
    return docs, log


def production_chunk_ids(chunks: Iterable[Document]) -> List[str]:
    """The ids the index stores these chunks under (``src/embedder.py``).

    Delegates to :func:`src.embedder.compute_chunk_id` with the chunk's own
    ``metadata["source"]`` — the same call ``add_documents`` makes, and the same
    value ``sync_documents`` uses (it refuses a chunk whose source differs from
    its scope). Imported lazily: ``src.embedder`` pulls in Chroma and
    HuggingFace, which plain chunking must not need.
    """
    from src.embedder import compute_chunk_id

    return [compute_chunk_id(c.metadata.get("source", ""), c.page_content) for c in chunks]


def absorbed_sections_by_chunk(
    log: AbsorptionLog, chunk_ids: Sequence[str]
) -> Dict[str, List[str]]:
    """Map each chunk id to the absorbed section labels it may be credited with.

    A chunk lists an absorbed section only if the WHOLE trimmed absorbed span
    lies inside the chunk's own trimmed span, within the same final segment.
    Consequences: an unsplit absorbing chunk lists all its segment's absorbed
    labels; of an oversize segment's sub-chunks only those containing the whole
    span are credited (a span split across sub-chunks credits none); a
    find-miss sub-chunk (no located span) is credited nothing. An intro's empty
    label and a label equal to the absorbing chunk's own are never listed.

    Args:
        log: the log from :func:`chunk_handbook_with_absorption`.
        chunk_ids: one id per chunk, aligned with ``log.chunks`` — normally
            ``production_chunk_ids(chunks)``.

    Returns:
        ``{chunk_id: sorted unique labels}``, only for chunks with at least one
        label. Chunks sharing an id (identical text) pool their labels.

    Raises:
        ValueError: if ``chunk_ids`` is not aligned with ``log.chunks``.
    """
    if len(chunk_ids) != len(log.chunks):
        raise ValueError(
            f"{len(chunk_ids)} chunk ids for {len(log.chunks)} logged chunks"
        )
    found: Dict[str, set] = {}
    for span, chunk_id in zip(log.chunks, chunk_ids):
        if span.start is None or span.end is None:
            continue  # find-miss sub-chunk: its position is unknown, no credit
        for a in log.absorptions:
            if (
                a.segment_index == span.segment_index
                and a.absorbed_section
                and a.absorbed_section != span.section
                and a.start < a.end
                and span.start <= a.start
                and a.end <= span.end
            ):
                found.setdefault(chunk_id, set()).add(a.absorbed_section)
    return {cid: sorted(labels) for cid, labels in sorted(found.items())}


# --- Small helpers ------------------------------------------------------------

def _page_citation(
    page_map: List[PageSpan], start: int, end: int
) -> Tuple[Optional[int], Optional[int]]:
    """Return the printed ``(page_start, page_end)`` for a ``[start, end)`` slice."""
    start_span, end_span = page_range(page_map, start, end)
    return start_span.printed_page, end_span.printed_page


def _page_string(p_start: Optional[int], p_end: Optional[int]) -> str:
    """Render ``p.87`` / ``pp.87–89`` / ``""`` (when no printed page is known)."""
    pages = [p for p in (p_start, p_end) if p is not None]
    if not pages:
        return ""
    lo, hi = min(pages), max(pages)
    return f"p.{lo}" if lo == hi else f"pp.{lo}–{hi}"


def _display_title(title: str) -> str:
    """Filename stem with underscores as spaces, e.g. ``Conveyancing Handbook``."""
    stem = title.rsplit(".", 1)[0] if "." in title else title
    return stem.replace("_", " ").strip() or "Document"


def _title_case(text: str) -> str:
    """Title-case an ALL-CAPS chapter title (``string.capwords`` keeps ``'s``)."""
    return string.capwords(text)


def _iter_lines(text: str, start: int, end: int):
    """Yield ``(line_start_offset, line_text)`` for each line in ``text[start:end]``."""
    i = start
    while i < end:
        j = text.find("\n", i, end)
        if j == -1:
            yield i, text[i:end]
            return
        yield i, text[i:j]
        i = j + 1


def _first_nonempty_after(text: str, start: int, end: int) -> str:
    """Return the first non-blank line (stripped) in ``text[start:end]``, or ""."""
    for _, line in _iter_lines(text, start, end):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
