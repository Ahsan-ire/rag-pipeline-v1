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
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

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
    markers = _find_chapter_markers(clean_text)
    body_end = _find_body_end(clean_text, markers)
    segments = _segment_body(clean_text, markers, body_end)
    segments = _merge_appendix_stubs(segments)
    segments = _merge_runts(segments)
    segments = _merge_trailing_runts(segments)
    return _build_documents(
        segments, clean_text, page_map, metadata, chunk_size, chunk_overlap
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

def _merge_appendix_stubs(segments: List[_Segment]) -> List[_Segment]:
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
            result[-1].end = seg.end
        else:
            result.append(replace(seg))
    return result


def _merge_runts(segments: List[_Segment]) -> List[_Segment]:
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
                cur.secnum, cur.heading, cur.is_intro = nxt.secnum, nxt.heading, nxt.is_intro
                cur.end = nxt.end
                i += 1
                continue
            if cur.secnum and nxt.secnum.startswith(cur.secnum + "."):
                cur.end = nxt.end  # descendant merge: keep the parent's identity
                i += 1
                continue
            break  # sibling / unrelated: this runt stays standalone
        result.append(cur)
        i += 1
    return result


def _merge_trailing_runts(segments: List[_Segment]) -> List[_Segment]:
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
    for seg in segments:
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
                docs.append(_make_doc(piece.strip(), base, seg, doc_title, pages))
        else:
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
        parts.append(secnum if secnum.startswith("APPENDIX") else f"para {secnum}")
    page = _page_string(p_start, p_end)
    if page:
        parts.append(page)
    return "[" + ", ".join(parts) + "] "


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
