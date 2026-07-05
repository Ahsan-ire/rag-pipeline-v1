"""Document ingestion module for loading legal documents from PDF and HTML sources.

The PDF path is page-aware (Phase 1): ``extract_pdf`` returns the cleaned full
text together with a *page map* — a list of :class:`PageSpan` recording, for
every surviving page, its raw PDF page index, the book's own printed page
number, and the half-open ``[char_start, char_end)`` slice it occupies in the
returned text. The Phase 2 chunker uses this map to assign page citations to
chunks. ``load_pdf`` keeps its original single-``Document`` contract by wrapping
``extract_pdf`` and discarding the map at that seam.
"""

import bisect
import logging
import os
import re
from typing import List, NamedTuple, Optional, Tuple

import pdfplumber
import requests
from bs4 import BeautifulSoup
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


class PageSpan(NamedTuple):
    """One page's position in the concatenated clean text.

    Attributes:
        page_number: Raw 1-indexed pdfplumber page — always present, the
            corpus-agnostic ground truth for opening the source PDF.
        printed_page: The book's own printed page number (parsed from the
            running header, or inferred for headerless body pages via the
            modal raw-minus-printed offset). ``None`` for front matter or any
            corpus without recoverable page numbers.
        char_start: Inclusive start offset into the clean text.
        char_end: Exclusive end offset; ``clean_text[char_start:char_end]`` is
            exactly this page's cleaned text, by construction.
    """

    page_number: int
    printed_page: Optional[int]
    char_start: int
    char_end: int


# --- Running-header grammar (D14) ---------------------------------------------
# Headers are only ever the first non-empty line of a page, in two shapes
# (recto/verso alternation). Titles are all-caps (so lowercase case names like
# "AG v Blake" can never match); the page number is 1-3 digits (printed pages
# top out well under 1000, so 4-digit years fail). Patterns are line-anchored
# and case-sensitive per the CLAUDE.md convention.
_HEADER_TITLE = r"[A-Z][A-Z0-9&'(),.\- ]*[A-Z.)]"
RECTO_HEADER = re.compile(rf"^({_HEADER_TITLE})\s+(\d{{1,3}})$")  # TITLE 87
VERSO_HEADER = re.compile(rf"^(\d{{1,3}})\s+({_HEADER_TITLE})$")  # 88 TITLE
CHAPTER_MARKER = re.compile(r"^CHAPTER \d+$")  # excluded from header matching
_PAGE_NUMBER_LINE = re.compile(r"^\d{1,3}$")
_TRAILING_HYPHEN = re.compile(r"([a-z]+)-$")  # word broken at a line/page end
_LEADING_WORD = re.compile(r"([a-z]+)")
_HEADER_OFFSET_TOLERANCE = 1  # accept candidates within modal offset +/- this


def _first_nonempty_line(text: str) -> Optional[str]:
    """Return the first non-blank line of ``text``, stripped, or ``None``."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _match_header(line: str) -> Optional[int]:
    """Return the printed page number if ``line`` is a running header, else None.

    ``CHAPTER N`` markers match the recto shape but are structural content, not
    headers, so they are excluded first — deleting them would leave the Phase 2
    chunker unable to find any chapter (the highest-severity trap in this phase).
    """
    if CHAPTER_MARKER.match(line):
        return None
    recto = RECTO_HEADER.match(line)
    if recto:
        return int(recto.group(2))
    verso = VERSO_HEADER.match(line)
    if verso:
        return int(verso.group(1))
    return None


def _compute_inference_bounds(
    raw_pages: List[Tuple[int, str]]
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Self-calibrate the offset and the raw-page window over which to infer.

    Every page whose first non-empty line parses as a header votes with
    ``raw_index - printed_number``; the mode is the corpus's constant offset
    (D10 reports +44 for the handbook, but it is derived, never hardcoded, so
    the "bring your own manual" property survives).

    Returns ``(modal_offset, infer_lower_raw, infer_upper_raw)`` — the inclusive
    raw-page window in which a headerless page's printed number is inferred as
    ``raw - modal_offset`` (D18). The lower bound is the earlier of the first
    validated header and the first ``^CHAPTER N$`` marker page: chapter openers
    are headerless and sit one page *before* the first running header, so
    without the marker term Chapter 1's opener (printed page 1) would be
    mis-classified as front matter. The upper bound is the last validated header
    plus one, so at most one trailing headerless page is covered — insurance for
    corpora whose body does not run headers to the final text page (a no-op on
    this corpus). Returns ``(None, None, None)`` when no headers are found
    (e.g. legislation PDFs), disabling inference entirely.
    """
    votes: dict = {}
    validated_raws: List[int] = []
    for raw_no, text in raw_pages:
        first = _first_nonempty_line(text)
        if first is None:
            continue
        printed = _match_header(first)
        if printed is not None:
            votes[raw_no - printed] = votes.get(raw_no - printed, 0) + 1
    if not votes:
        return None, None, None
    modal_offset = max(votes, key=votes.get)

    for raw_no, text in raw_pages:
        first = _first_nonempty_line(text)
        if first is None:
            continue
        printed = _match_header(first)
        if (
            printed is not None
            and abs((raw_no - printed) - modal_offset) <= _HEADER_OFFSET_TOLERANCE
        ):
            validated_raws.append(raw_no)

    first_chapter_raw: Optional[int] = None
    for raw_no, text in raw_pages:
        first = _first_nonempty_line(text)
        if first is not None and CHAPTER_MARKER.match(first):
            first_chapter_raw = raw_no
            break

    first_body_raw = validated_raws[0] if validated_raws else None
    last_body_raw = validated_raws[-1] if validated_raws else None
    lower_candidates = [r for r in (first_body_raw, first_chapter_raw) if r is not None]
    infer_lower_raw = min(lower_candidates) if lower_candidates else None
    infer_upper_raw = last_body_raw + 1 if last_body_raw is not None else None
    return modal_offset, infer_lower_raw, infer_upper_raw


def _build_attestation(raw_pages: List[Tuple[int, str]]) -> Tuple[set, set]:
    """Build the corpus's hyphenation evidence from unbroken (intra-line) tokens.

    Returns ``(hyphenated, plain)``: lowercased words seen written with an
    internal hyphen (e.g. ``co-ownership``) and lowercased plain words. Used to
    decide, empirically, whether a line-end hyphen should be kept or dropped.
    """
    hyphenated: set = set()
    plain: set = set()
    for _, text in raw_pages:
        for line in text.split("\n"):
            for token in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)+", line):
                hyphenated.add(token.lower())
            for token in re.findall(r"[A-Za-z]+", line):
                plain.add(token.lower())
    return hyphenated, plain


def _join_hyphenated(a: str, b_word: str, hyphenated: set, plain: set) -> bool:
    """Decide whether a broken word ``a`` + ``b_word`` keeps its hyphen.

    Returns True to keep the hyphen (``a-b_word`` is attested in the corpus),
    False to fuse (``ab_word`` attested, or neither — plain join is the default,
    since a true mid-word break is far more common than a broken compound).
    """
    if f"{a}-{b_word}" in hyphenated:
        return True
    return False


def _repair_intra_page_hyphenation(page: str, hyphenated: set, plain: set) -> str:
    """Rejoin words broken by a hyphen at a line end, within a single page."""
    lines = page.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _TRAILING_HYPHEN.search(line)
        if match and i + 1 < len(lines) and lines[i + 1][:1].islower():
            a = match.group(1)
            nxt = lines[i + 1]
            b_match = _LEADING_WORD.match(nxt)
            b_word = b_match.group(1) if b_match else ""
            if _join_hyphenated(a, b_word, hyphenated, plain):
                lines[i + 1] = line + nxt  # keep hyphen: co- + ownership
            else:
                lines[i + 1] = line[:-1] + nxt  # drop hyphen: regis + tration
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _clean_page(
    text: str,
    raw_no: int,
    modal_offset: Optional[int],
    infer_lower_raw: Optional[int],
    infer_upper_raw: Optional[int],
    hyphenated: set,
    plain: set,
) -> Tuple[str, Optional[int]]:
    """Clean one raw page: strip header/page-number furniture, repair hyphens.

    Returns ``(cleaned_text, printed_page)``. ``printed_page`` comes from a
    validated header, or is inferred as ``raw_no - modal_offset`` for headerless
    body pages within ``[infer_lower_raw, infer_upper_raw]`` (D18), or is
    ``None`` for front matter / headerless corpora.
    """
    lines = [line.rstrip() for line in text.split("\n")]
    printed: Optional[int] = None

    first_idx = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_idx is not None:
        first_line = lines[first_idx].strip()
        parsed = _match_header(first_line)
        validated = (
            parsed is not None
            and modal_offset is not None
            and abs((raw_no - parsed) - modal_offset) <= _HEADER_OFFSET_TOLERANCE
        )
        if validated:
            printed = parsed
            lines.pop(first_idx)
        elif _PAGE_NUMBER_LINE.match(first_line):
            lines.pop(first_idx)

    last_idx = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None
    )
    if last_idx is not None and _PAGE_NUMBER_LINE.match(lines[last_idx].strip()):
        lines.pop(last_idx)

    page = "\n".join(lines)
    page = _repair_intra_page_hyphenation(page, hyphenated, plain)
    page = re.sub(r"\n{3,}", "\n\n", page).strip("\n")

    if (
        printed is None
        and modal_offset is not None
        and infer_lower_raw is not None
        and infer_upper_raw is not None
        and infer_lower_raw <= raw_no <= infer_upper_raw
    ):
        candidate = raw_no - modal_offset
        if candidate >= 1:  # a printed page number is never zero or negative
            printed = candidate

    return page, printed


def _extract_raw_pages(file_path: str) -> List[Tuple[int, str]]:
    """Extract raw per-page text via pdfplumber, skipping pages with no text.

    Page numbers are the true 1-indexed PDF positions, so a skipped (imageless
    or blank) page leaves a gap rather than renumbering later pages. Propagates
    exceptions to the caller (``load_pdf`` handles them; the QA script wants
    real errors).
    """
    pages: List[Tuple[int, str]] = []
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if text:
                pages.append((i, text))
    return pages


def _join_pages(
    cleaned: List[Tuple[int, Optional[int], str]], hyphenated: set, plain: set
) -> Tuple[str, List[PageSpan]]:
    """Concatenate cleaned pages, repairing cross-page hyphens, recording offsets.

    Three passes keep the page-map invariant exact *by construction*: page texts
    are already cleaned (pass 1); this decides each boundary's separator and any
    hyphen trim (pass 2); then a single pure concatenation records each page's
    span (pass 3), so ``clean_text[start:end]`` is never disturbed by a later
    edit. Pages join with ``"\n"`` (a page break is a line break, not a
    paragraph break) unless a word is repaired across the seam.
    """
    page_texts = [text for _, _, text in cleaned]
    n = len(page_texts)
    separators = ["\n"] * max(n - 1, 0)

    for i in range(n - 1):
        match = _TRAILING_HYPHEN.search(page_texts[i])
        if match and page_texts[i + 1][:1].islower():
            a = match.group(1)
            b_match = _LEADING_WORD.match(page_texts[i + 1])
            b_word = b_match.group(1) if b_match else ""
            if _join_hyphenated(a, b_word, hyphenated, plain):
                separators[i] = ""  # keep hyphen: page ends "co-", next "ownership"
            else:
                page_texts[i] = page_texts[i][:-1]  # drop hyphen: "regis" + "tration"
                separators[i] = ""

    parts: List[str] = []
    page_map: List[PageSpan] = []
    pos = 0
    for i, (raw_no, printed, _) in enumerate(cleaned):
        text = page_texts[i]
        start = pos
        parts.append(text)
        pos += len(text)
        page_map.append(PageSpan(raw_no, printed, start, pos))
        if i < n - 1:
            parts.append(separators[i])
            pos += len(separators[i])

    return "".join(parts), page_map


def extract_pdf(file_path: str) -> Tuple[str, List[PageSpan]]:
    """Extract, clean, and page-map a PDF's text layer.

    Returns ``(clean_text, page_map)``: the cleaned full text and a list of
    :class:`PageSpan` locating every surviving page within it. See D13/D14/D15/D16
    in docs/decisions.md for the cleaning rules.
    """
    raw_pages = _extract_raw_pages(file_path)
    if not raw_pages:
        return "", []

    modal_offset, infer_lower_raw, infer_upper_raw = _compute_inference_bounds(raw_pages)
    hyphenated, plain = _build_attestation(raw_pages)

    cleaned: List[Tuple[int, Optional[int], str]] = []
    for raw_no, text in raw_pages:
        page, printed = _clean_page(
            text,
            raw_no,
            modal_offset,
            infer_lower_raw,
            infer_upper_raw,
            hyphenated,
            plain,
        )
        if page.strip():  # a page that is only furniture is dropped
            cleaned.append((raw_no, printed, page))

    return _join_pages(cleaned, hyphenated, plain)


def page_range(
    page_map: List[PageSpan], start: int, end: int
) -> Tuple[PageSpan, PageSpan]:
    """Map a ``[start, end)`` slice of the clean text to its first/last pages.

    A position falling in the single-character separator gap between two pages
    is attributed to the preceding page (bisect on ``char_start``). A word
    repaired across a page break therefore yields distinct start/end pages,
    which is physically correct — the word sits on both.
    """
    if not page_map:
        raise ValueError("page_range called with an empty page map")
    starts = [span.char_start for span in page_map]

    def span_for(pos: int) -> PageSpan:
        idx = bisect.bisect_right(starts, pos) - 1
        return page_map[max(idx, 0)]

    return span_for(start), span_for(max(start, end - 1))


def load_pdf(file_path: str, document_type: str = "legislation") -> List[Document]:
    """Load a PDF file and return a list containing a single Document.

    Wraps :func:`extract_pdf`: the pages are cleaned and concatenated into one
    Document (so the chunker can split on legal boundaries rather than arbitrary
    page breaks). The page map is dropped at this seam — it must not enter
    ``Document.metadata`` because the chunker copies metadata into every chunk
    and Chroma rejects non-scalar metadata values. The Phase 2 chunker will call
    ``extract_pdf`` directly to consume the map.
    """
    try:
        clean_text, _page_map = extract_pdf(file_path)
    except FileNotFoundError:
        logger.error("PDF file not found: %s", file_path)
        return []
    except Exception as e:
        logger.error("Error loading PDF %s: %s", file_path, e)
        return []

    if not clean_text.strip():
        logger.warning("No text extracted from PDF: %s", file_path)
        return []

    return [
        Document(
            page_content=clean_text,
            metadata={
                "source": file_path,
                "title": os.path.basename(file_path),
                "document_type": document_type,
                "date": "",
            },
        )
    ]


def load_handbook_pdf(file_path: str) -> Tuple[str, List[PageSpan], dict]:
    """Load a handbook PDF for the page-aware chunker (Phase 2).

    Unlike :func:`load_pdf` — which collapses the corpus into a single
    ``Document`` and drops the page map at that seam — this returns the cleaned
    text, the page map, and a metadata block, so
    :func:`src.chunker.chunk_handbook` can assign per-chunk ``page_start`` /
    ``page_end`` citations from the map. Error semantics mirror ``load_pdf``: a
    missing or unreadable file is logged and yields an empty result
    ``("", [], {})`` rather than raising, so the pipeline degrades gracefully.
    """
    try:
        clean_text, page_map = extract_pdf(file_path)
    except FileNotFoundError:
        logger.error("PDF file not found: %s", file_path)
        return "", [], {}
    except Exception as e:
        logger.error("Error loading PDF %s: %s", file_path, e)
        return "", [], {}

    if not clean_text.strip():
        logger.warning("No text extracted from PDF: %s", file_path)
        return "", [], {}

    metadata = {
        "source": file_path,
        "title": os.path.basename(file_path),
        "document_type": "handbook",
        "date": "",
    }
    return clean_text, page_map, metadata


def load_html_from_url(
    url: str, document_type: str = "legislation"
) -> List[Document]:
    """Fetch an HTML page and return a list containing a single Document.

    Works with eISB (Irish Statute Book) and BAILII pages.
    """
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error("Error fetching URL %s: %s", url, e)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        h1_tag = soup.find("h1")
        title = h1_tag.get_text(strip=True) if h1_tag else url

    # Remove script and style elements
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # Extract body text
    body = soup.find("body")
    text = body.get_text(separator="\n") if body else soup.get_text(separator="\n")

    # Clean up excessive whitespace while preserving paragraph breaks
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if not text.strip():
        logger.warning("No text extracted from URL: %s", url)
        return []

    return [
        Document(
            page_content=text,
            metadata={
                "source": url,
                "title": title,
                "document_type": document_type,
                "date": "",
            },
        )
    ]


def load_html_file(file_path: str, document_type: str = "legislation") -> List[Document]:
    """Load a local HTML file and return a list containing a single Document."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except (FileNotFoundError, PermissionError) as e:
        logger.error("Error reading HTML file %s: %s", file_path, e)
        return []

    soup = BeautifulSoup(html_content, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else os.path.basename(file_path)

    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    body = soup.find("body")
    text = body.get_text(separator="\n") if body else soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if not text.strip():
        logger.warning("No text extracted from HTML file: %s", file_path)
        return []

    return [
        Document(
            page_content=text,
            metadata={
                "source": file_path,
                "title": title,
                "document_type": document_type,
                "date": "",
            },
        )
    ]


def load_directory(dir_path: str, document_type: str = "legislation") -> List[Document]:
    """Walk a directory and load all supported files (.pdf, .html, .htm, .txt)."""
    if not os.path.isdir(dir_path):
        logger.error("Directory not found: %s", dir_path)
        return []

    documents = []
    for root, _, files in os.walk(dir_path):
        for filename in sorted(files):
            file_path = os.path.join(root, filename)
            ext = os.path.splitext(filename)[1].lower()

            if ext == ".pdf":
                docs = load_pdf(file_path, document_type)
            elif ext in (".html", ".htm"):
                docs = load_html_file(file_path, document_type)
            elif ext == ".txt":
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    if text.strip():
                        docs = [
                            Document(
                                page_content=text,
                                metadata={
                                    "source": file_path,
                                    "title": filename,
                                    "document_type": document_type,
                                    "date": "",
                                },
                            )
                        ]
                    else:
                        docs = []
                except Exception as e:
                    logger.error("Error reading %s: %s", file_path, e)
                    docs = []
            else:
                continue

            documents.extend(docs)

    logger.info("Loaded %d documents from %s", len(documents), dir_path)
    return documents
