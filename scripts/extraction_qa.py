"""Extraction QA gate: inspect pdfplumber text extraction quality.

Default (raw) mode inspects the corpus PDF's raw per-page extraction before any
cleaning: a random page sample for manual comparison against the source PDF, a
census of line-start numbering patterns to learn the book's numbering grammar,
an OCR-fidelity report (un-OCR'd pages, garble signatures, non-ASCII census),
and header/footer/hyphenation censuses that make the Phase 0 findings (D10)
reproducible. Raw mode imports nothing from src/ — it must observe raw
extraction, and cross-checking the cleaner's input with the cleaner's own
regexes would let a bug hide itself; the header/hyphen patterns are duplicated
here deliberately.

`--cleaned` mode routes the same PDF through ``src.ingest.extract_pdf`` and runs
the identical reports on the cleaned output — this is how Phase 1 acceptance is
checked (headers gone, hyphens repaired, CHAPTER markers intact), plus a
page-map invariant check on the real corpus.

See IMPLEMENTATION_PLAN.md Phases 0-1 and docs/decisions.md D10, D13-D16.
"""

import argparse
import os
import random
import re
import sys
from collections import Counter, defaultdict
from typing import DefaultDict, List, Optional, Tuple

import pdfplumber

CHAPTER_PATTERN = re.compile(r"^Chapter \d+")  # mixed-case: the disproven D10 pattern
CHAPTER_MARKER = re.compile(r"^CHAPTER \d+$")  # all-caps: the real chapter marker
DECIMAL_PATTERN = re.compile(r"^(\d+(?:\.\d+)*)")

# Running-header grammar, duplicated from src.ingest (D14) so raw-mode QA stays
# independent of the cleaner it exists to check.
_HEADER_TITLE = r"[A-Z][A-Z0-9&'(),.\- ]*[A-Z.)]"
RECTO_HEADER = re.compile(rf"^({_HEADER_TITLE})\s+(\d{{1,3}})$")
VERSO_HEADER = re.compile(rf"^(\d{{1,3}})\s+({_HEADER_TITLE})$")
TRAILING_HYPHEN = re.compile(r"[a-z]-$")
PAGE_NUMBER_LINE = re.compile(r"^\d{1,3}$")
CID_PATTERN = re.compile(r"\(cid:\d+\)")
REPLACEMENT_CHAR = "�"

# Non-ASCII characters expected in an Irish legal text (section signs, curly
# quotes, dashes, accented names). Anything non-ASCII outside this set is a
# garble candidate worth eyeballing.
EXPECTED_NON_ASCII = set(
    "§°£€‘’“”–—…•●©"
    "àáâäéèêëíìîï"
    "óòôöúùûüñç"
    "ÀÁÉÍÓÚÑ"
)

REPEATED_LINE_THRESHOLD = 0.10
DEFAULT_SAMPLE_PAGES = 10
DEFAULT_SEED = 42
MAX_EXAMPLES_PER_DEPTH = 8
MAX_LINE_PREVIEW = 100
MAX_SAMPLES = 12
# A page whose entire extractable text is below this many characters is almost
# certainly furniture-only (a running header, ~25-52 chars) — its body did not
# reach the text layer (reproduced form, vector-drawn, or un-OCR'd).
SPARSE_TEXT_THRESHOLD = 100


def _first_nonempty(text: str) -> Optional[str]:
    """Return the first non-blank line of `text`, stripped, or None."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _last_nonempty(text: str) -> Optional[str]:
    """Return the last non-blank line of `text`, stripped, or None."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def _is_header_shape(line: str) -> bool:
    """True if `line` matches a running-header shape (and is not a CHAPTER marker)."""
    if CHAPTER_MARKER.match(line):
        return False
    return bool(RECTO_HEADER.match(line) or VERSO_HEADER.match(line))


def extract_pages(pdf_path: str) -> List[Tuple[int, str]]:
    """Extract raw per-page text from a PDF using pdfplumber.

    Returns a list of (page_number, text) tuples, 1-indexed, skipping pages
    with no extractable text.
    """
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if text:
                pages.append((i, text))
    return pages


def print_random_page_sample(pages: List[Tuple[int, str]], n: int, seed: int) -> None:
    """Print n randomly sampled pages, delimited by page number.

    Intended for manual comparison against the source PDF for OCR fidelity
    and page-furniture pollution.
    """
    print(f"\n{'=' * 70}")
    print(f"RANDOM PAGE SAMPLE (n={n}, seed={seed})")
    print(f"{'=' * 70}")
    rng = random.Random(seed)
    sample = sorted(rng.sample(pages, min(n, len(pages))), key=lambda p: p[0])
    for page_num, text in sample:
        print(f"\n--- Page {page_num} ---")
        print(text)


def numbering_grammar_census(pages: List[Tuple[int, str]]) -> None:
    """Scan all pages for line-start numbering patterns and print a census.

    Examines the two structural markers named in IMPLEMENTATION_PLAN.md
    Phase 2: `^Chapter \\d+` and `^\\d+(\\.\\d+)*`. The decimal pattern is
    grouped by "depth" (count of dot-separated numeric groups) so bare
    numbers that are really page-number furniture (depth 1, e.g. a
    standalone "87") stay visually distinguishable from real paragraph
    numbers (depth 2-3, e.g. "3.2.1").
    """
    print(f"\n{'=' * 70}")
    print("NUMBERING GRAMMAR CENSUS")
    print(f"{'=' * 70}")

    chapter_lines: Counter = Counter()
    depth_counts: Counter = Counter()
    depth_examples: DefaultDict[int, List[str]] = defaultdict(list)

    for _, text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if CHAPTER_PATTERN.match(stripped):
                chapter_lines[stripped[:MAX_LINE_PREVIEW]] += 1
                continue
            decimal_match = DECIMAL_PATTERN.match(stripped)
            if decimal_match:
                depth = decimal_match.group(1).count(".") + 1
                depth_counts[depth] += 1
                examples = depth_examples[depth]
                preview = stripped[:MAX_LINE_PREVIEW]
                if preview not in examples and len(examples) < MAX_EXAMPLES_PER_DEPTH:
                    examples.append(preview)

    print(
        f"\n'^Chapter \\d+' -- {sum(chapter_lines.values())} matching lines, "
        f"{len(chapter_lines)} distinct:"
    )
    for line, count in sorted(chapter_lines.items()):
        print(f"  [{count:>4}x] {line}")

    print("\n'^\\d+(\\.\\d+)*' by depth (depth = count of dot-separated groups):")
    for depth in sorted(depth_counts):
        print(f"  depth {depth}: {depth_counts[depth]} matching lines. Examples:")
        for example in depth_examples[depth]:
            print(f"    {example}")


def repeated_line_report(pages: List[Tuple[int, str]], threshold: float) -> None:
    """Print lines recurring on more than `threshold` fraction of pages.

    These are candidates for running headers/footers that Phase 1 cleaning
    needs to strip.
    """
    print(f"\n{'=' * 70}")
    print(f"REPEATED LINE REPORT (threshold: >{threshold:.0%} of pages)")
    print(f"{'=' * 70}")

    page_counts: Counter = Counter()
    total_pages = len(pages)
    for _, text in pages:
        seen_this_page = set()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and stripped not in seen_this_page:
                seen_this_page.add(stripped)
                page_counts[stripped] += 1

    repeated = [
        (line, count)
        for line, count in page_counts.items()
        if count / total_pages > threshold
    ]
    repeated.sort(key=lambda item: item[1], reverse=True)

    if not repeated:
        print(f"\nNo lines recurred on more than {threshold:.0%} of pages.")
        return

    for line, count in repeated:
        pct = count / total_pages
        print(f"  [{count:>4}/{total_pages} pages, {pct:.0%}] {line[:MAX_LINE_PREVIEW]}")


def chapter_marker_census(pages: List[Tuple[int, str]]) -> None:
    """Census the real all-caps `^CHAPTER \\d+$` marker (expect 16 in the handbook).

    Cleaning must not destroy these; in `--cleaned` mode the count must be
    unchanged from raw.
    """
    print(f"\n{'=' * 70}")
    print("CHAPTER MARKER CENSUS  ('^CHAPTER \\d+$', all-caps standalone)")
    print(f"{'=' * 70}")
    hits = []
    for page_num, text in pages:
        for line in text.splitlines():
            if CHAPTER_MARKER.match(line.strip()):
                hits.append((page_num, line.strip()))
    print(f"\n{len(hits)} matching lines:")
    for page_num, line in hits:
        print(f"  [page {page_num:>4}] {line}")


def header_footer_census(pages: List[Tuple[int, str]]) -> None:
    """Count pages whose first/last line is a running-header/footer shape.

    Reproduces D10's ~707 header pages in raw mode; the target in `--cleaned`
    mode is 0. Footers (last-line shape or bare number) confirm D10's "none".
    """
    print(f"\n{'=' * 70}")
    print("HEADER / FOOTER CENSUS  (positional: first/last non-empty line)")
    print(f"{'=' * 70}")

    total = len(pages)
    header_pages = []
    footer_pages = []
    for page_num, text in pages:
        first = _first_nonempty(text)
        if first is not None and _is_header_shape(first):
            header_pages.append((page_num, first))
        last = _last_nonempty(text)
        if last is not None and (_is_header_shape(last) or PAGE_NUMBER_LINE.match(last)):
            footer_pages.append((page_num, last))

    print(
        f"\nFirst-line header shape: {len(header_pages)}/{total} pages "
        f"({(len(header_pages) / total if total else 0):.0%})"
    )
    for page_num, line in header_pages[:MAX_SAMPLES]:
        print(f"  [page {page_num:>4}] {line[:MAX_LINE_PREVIEW]}")
    print(
        f"\nLast-line footer shape/bare-number: {len(footer_pages)}/{total} pages "
        f"({(len(footer_pages) / total if total else 0):.0%})"
    )
    for page_num, line in footer_pages[:MAX_SAMPLES]:
        print(f"  [page {page_num:>4}] {line[:MAX_LINE_PREVIEW]}")


def hyphenation_census(pages: List[Tuple[int, str]]) -> None:
    """Count lines ending in a lowercase letter + hyphen (word broken at line end).

    Reproduces D10's 2,682 raw hyphen-break lines; target in `--cleaned` mode is
    ~0 (a residue is fine where the following line did not start lowercase).
    """
    print(f"\n{'=' * 70}")
    print("HYPHENATION CENSUS  (lines ending [a-z]-)")
    print(f"{'=' * 70}")
    hits = []
    for page_num, text in pages:
        for line in text.splitlines():
            if TRAILING_HYPHEN.search(line.rstrip()):
                hits.append((page_num, line.strip()))
    print(f"\n{len(hits)} lines end in lowercase+hyphen.")
    for page_num, line in hits[:MAX_SAMPLES]:
        print(f"  [page {page_num:>4}] ...{line[-MAX_LINE_PREVIEW:]}")


def ocr_fidelity_report(pages: List[Tuple[int, str]]) -> None:
    """Surface OCR garble: (cid:N) markers, U+FFFD, and unexpected non-ASCII chars.

    Answers the "is the text layer actually clean?" question with corpus-wide
    counts instead of a 10-page eyeball. cid markers and replacement chars are
    definitive garble; the non-ASCII table separates legitimate characters
    (section signs, accented names) from mojibake.
    """
    print(f"\n{'=' * 70}")
    print("OCR FIDELITY REPORT")
    print(f"{'=' * 70}")

    cid_hits = []
    replacement_hits = []
    non_ascii: Counter = Counter()
    non_ascii_sample = {}
    for page_num, text in pages:
        for line in text.splitlines():
            if CID_PATTERN.search(line):
                cid_hits.append((page_num, line.strip()))
            if REPLACEMENT_CHAR in line:
                replacement_hits.append((page_num, line.strip()))
            for ch in line:
                if ord(ch) > 127:
                    non_ascii[ch] += 1
                    if ch not in non_ascii_sample:
                        non_ascii_sample[ch] = (page_num, line.strip()[:MAX_LINE_PREVIEW])

    print(f"\n(cid:N) unmapped-glyph markers: {len(cid_hits)} lines")
    for page_num, line in cid_hits[:MAX_SAMPLES]:
        print(f"  [page {page_num:>4}] {line[:MAX_LINE_PREVIEW]}")
    print(f"\nU+FFFD replacement characters: {len(replacement_hits)} lines")
    for page_num, line in replacement_hits[:MAX_SAMPLES]:
        print(f"  [page {page_num:>4}] {line[:MAX_LINE_PREVIEW]}")

    unexpected = {ch: n for ch, n in non_ascii.items() if ch not in EXPECTED_NON_ASCII}
    print(
        f"\nNon-ASCII characters: {len(non_ascii)} distinct "
        f"({len(unexpected)} unexpected). Table (U+xxxx, count, sample):"
    )
    for ch, count in sorted(non_ascii.items(), key=lambda kv: kv[1], reverse=True):
        flag = "  <-- UNEXPECTED" if ch not in EXPECTED_NON_ASCII else ""
        page_num, sample = non_ascii_sample[ch]
        print(f"  U+{ord(ch):04X} {ch!r:>6} x{count:<6} [page {page_num}] {sample}{flag}")


def text_layer_coverage_report(pdf_path: str) -> None:
    """Report pages that carry an image but no extractable text (un-OCR'd pages).

    A page with images and no text layer is un-OCR'd content — invisible to the
    other reports because both this script and load_pdf silently drop textless
    pages. This makes such loss explicit for the go/no-go decision.
    """
    print(f"\n{'=' * 70}")
    print("TEXT-LAYER COVERAGE  (un-OCR'd page detector)")
    print(f"{'=' * 70}")
    no_text = []
    no_text_with_images = []
    sparse = []  # header-only pages: body content never reached the text layer
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            stripped = (page.extract_text() or "").strip()
            if not stripped:
                no_text.append(i)
                if page.images:
                    no_text_with_images.append(i)
            elif len(stripped) < SPARSE_TEXT_THRESHOLD:
                sparse.append(i)
    print(f"\nTotal pages: {total}")
    print(f"Pages with no extractable text: {len(no_text)} {no_text[:MAX_SAMPLES]}")
    print(
        f"Of those, pages carrying images (likely un-OCR'd): "
        f"{len(no_text_with_images)} {no_text_with_images[:MAX_SAMPLES]}"
    )
    print(
        f"Sparse pages (<{SPARSE_TEXT_THRESHOLD} chars, ~header only): "
        f"{len(sparse)} {sparse[:MAX_SAMPLES]}"
    )
    if no_text_with_images:
        print("  ^ ESCALATE: image-only pages indicate missing OCR (see D11 ladder).")
    if sparse:
        print(
            "  ^ NOTE: sparse pages are reproduced forms/appendices whose body is "
            "not in the text layer; cleaning drops them (see D17)."
        )


def page_map_invariant_report(clean_text: str, page_map: list) -> None:
    """Verify the page map is well-formed against the real corpus (cleaned mode).

    Checks spans are ordered, non-overlapping, newline-trimmed, and that no
    cleaned page is empty — properties unit tests assert on synthetic input,
    exercised here at 800-page scale.
    """
    print(f"\n{'=' * 70}")
    print("PAGE-MAP INVARIANT CHECK")
    print(f"{'=' * 70}")
    problems = []
    if page_map and page_map[0].char_start != 0:
        problems.append(f"first span starts at {page_map[0].char_start}, not 0")
    for a, b in zip(page_map, page_map[1:]):
        if not a.char_start < b.char_start:
            problems.append(f"non-increasing start at page {a.page_number}")
        if a.char_end > b.char_start:
            problems.append(f"overlap after page {a.page_number}")
    for span in page_map:
        slice_ = clean_text[span.char_start:span.char_end]
        if not slice_.strip():
            problems.append(f"empty slice for page {span.page_number}")
        if slice_ != slice_.strip("\n"):
            problems.append(f"page {span.page_number} slice has edge newlines")
    if page_map and page_map[-1].char_end > len(clean_text):
        problems.append("last span exceeds clean_text length")

    if problems:
        print(f"\nFAIL ({len(problems)} problems):")
        for problem in problems[:MAX_SAMPLES]:
            print(f"  - {problem}")
    else:
        print(f"\nPASS: {len(page_map)} spans, all ordered/non-overlapping/trimmed.")


def _load_pages(pdf_path: str, cleaned: bool):
    """Return (pages, clean_text, page_map). Raw mode leaves the latter two None."""
    if not cleaned:
        pages = extract_pages(pdf_path)
        print(f"Extracted raw text from {len(pages)} pages.")
        return pages, None, None

    # Lazy import: keep raw mode src-independent. Add the project root to the
    # path so the script works when run as a file (sys.path[0] is scripts/).
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src.ingest import extract_pdf

    clean_text, page_map = extract_pdf(pdf_path)
    pages = [
        (span.page_number, clean_text[span.char_start:span.char_end])
        for span in page_map
    ]
    print(f"Reconstructed {len(pages)} cleaned pages from the page map.")
    return pages, clean_text, page_map


def main() -> None:
    """Run the extraction QA reports against a PDF (raw or cleaned)."""
    parser = argparse.ArgumentParser(
        description="Extraction QA gate: inspect pdfplumber text extraction "
        "quality (raw), or the Phase 1 cleaner's output (--cleaned)."
    )
    parser.add_argument("pdf_path", help="Path to the corpus PDF")
    parser.add_argument(
        "--cleaned",
        action="store_true",
        help="Route the PDF through src.ingest.extract_pdf and report on the "
        "cleaned output (Phase 1 acceptance check).",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_SAMPLE_PAGES,
        help=f"Number of random pages to sample (default: {DEFAULT_SAMPLE_PAGES})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Random seed for reproducible sampling (default: {DEFAULT_SEED})",
    )
    args = parser.parse_args()

    mode = "CLEANED" if args.cleaned else "RAW"
    print(f"Extraction QA — {mode} mode — {args.pdf_path}")

    pages, clean_text, page_map = _load_pages(args.pdf_path, args.cleaned)

    text_layer_coverage_report(args.pdf_path)  # always on the source PDF
    print_random_page_sample(pages, args.pages, args.seed)
    chapter_marker_census(pages)
    numbering_grammar_census(pages)
    header_footer_census(pages)
    hyphenation_census(pages)
    ocr_fidelity_report(pages)
    repeated_line_report(pages, REPEATED_LINE_THRESHOLD)
    if args.cleaned:
        page_map_invariant_report(clean_text, page_map)


if __name__ == "__main__":
    main()
