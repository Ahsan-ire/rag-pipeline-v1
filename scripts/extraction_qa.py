"""Phase 0 go/no-go gate: eyeball raw pdfplumber text extraction quality.

Inspects the corpus PDF's raw per-page text extraction before any cleaning
or chunking design happens: a random page sample for manual comparison
against the source PDF, a census of line-start numbering patterns to learn
the book's real numbering grammar, and a repeated-line report to surface
header/footer candidates. Uses pdfplumber only and imports nothing from
src/, since it must observe raw extraction independent of any cleaning
assumptions made later.

See IMPLEMENTATION_PLAN.md Phase 0.
"""

import argparse
import random
import re
from collections import Counter, defaultdict
from typing import DefaultDict, List, Tuple

import pdfplumber

CHAPTER_PATTERN = re.compile(r"^Chapter \d+")
DECIMAL_PATTERN = re.compile(r"^(\d+(?:\.\d+)*)")
REPEATED_LINE_THRESHOLD = 0.10
DEFAULT_SAMPLE_PAGES = 10
DEFAULT_SEED = 42
MAX_EXAMPLES_PER_DEPTH = 8
MAX_LINE_PREVIEW = 100


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


def main() -> None:
    """Run all three extraction QA reports against a PDF."""
    parser = argparse.ArgumentParser(
        description="Phase 0 extraction QA gate: inspect raw pdfplumber "
        "text extraction quality before designing the cleaning/chunking "
        "pipeline."
    )
    parser.add_argument("pdf_path", help="Path to the corpus PDF")
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

    pages = extract_pages(args.pdf_path)
    print(f"Extracted text from {len(pages)} pages.")

    print_random_page_sample(pages, args.pages, args.seed)
    numbering_grammar_census(pages)
    repeated_line_report(pages, REPEATED_LINE_THRESHOLD)


if __name__ == "__main__":
    main()
