"""A wholly synthetic, copyright-safe sample handbook (Phase 11 / D40).

Why this file exists
--------------------
The real corpus is a copyrighted conveyancing handbook that is never committed
to this public repo, so a fresh clone has nothing to index and no way to
demonstrate the pipeline end to end. This module invents a ~15-page handbook —
a fictional jurisdiction ("Erewhon"), fictional registers, forms and covenants,
zero real text — that exercises the real grammar the chunker keys on: ``CHAPTER
N`` markers, decimal section numbering nested to four levels, an ``APPENDIX``
locator, and a ``Part I of the folio`` false-positive trap line (the
legislation-vs-handbook routing bug class).

How it enters the pipeline (D40)
--------------------------------
It enters at the *post-extraction* seam, not as a generated PDF. ``chunk_handbook``
consumes ``(clean_text, page_map, metadata)`` — none of which is PDF-specific —
so building that triple by hand skips pdfplumber entirely and needs no new
dependency. The construction below (page strings -> concatenated ``clean_text``
plus a contiguous ``PageSpan`` map) is a STANDALONE adaptation of the
``_handbook`` / ``_body`` fixtures in tests/test_chunker_handbook.py:18-54. It is
deliberately a copy, not a shared import: the chunker tests keep their fixtures
self-contained, and a script must never import from the test tree.

Determinism contract (the CI smoke depends on it)
-------------------------------------------------
Every section body that a golden question cites is >= 600 chars (below that a
runt merges away and the section vanishes from the index), and every post-merge
segment stays well under 4000 chars (above that it re-splits into duplicate
section metadata). Section 2.4.1 is intentionally a sub-600 runt: it is the
designed trailing-runt-merge demonstration and is never cited by the golden set.
Each cited section carries a unique fictional token (e.g. "Meridian Folio",
"Form VX-9", "Blackthorn Conditions") so BM25 pins the right chunk regardless of
embedding-float jitter across platforms. Change the prose freely, but keep those
size floors and unique tokens or tests/test_sample_corpus.py will fail loudly.
"""

import os
import sys

# Repo root on sys.path so ``python scripts/sample_corpus.py`` works even though
# sys.path[0] is scripts/ (same bootstrap as scripts/extraction_qa.py:409-414).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ingest import PageSpan  # noqa: E402  (import after the sys.path bootstrap)

# A non-/data/ identity so this synthetic corpus can never be confused with the
# real handbook in the vector store or in provenance.
SOURCE = "sample://synthetic-handbook-v1"
# No dot in the stem -> _display_title renders "Sample Conveyancing Handbook".
TITLE = "Sample_Conveyancing_Handbook"


def _filler(n: int, tag: str) -> str:
    """``n`` distinct ~100-char clauses (standalone adaptation of the chunker
    test's ``_body``). Distinct per ``i`` so nothing repeats, and per ``tag`` so
    clauses stay globally unique across sections and pages."""
    return "".join(
        f"Clause {tag}{i} of this part states a rule of fictional conveyancing "
        f"practice in plain and careful terms. "
        for i in range(n)
    )


# --- Section bodies -----------------------------------------------------------
# Each is a couple of distinctive sentences (carrying the section's unique token)
# followed by filler that lifts it clear of the 600-char runt floor. Chapter 1.

_S_1_1_HEAD = (
    "Section 1.1 establishes the Verdigris Register, the central record of "
    "freehold title maintained by the Land Registry of Erewhon. Every parcel of "
    "registered land is identified by a unique Verdigris folio number. "
)
_S_1_1_TAIL = _filler(6, "aa")  # continues onto the next page (a page-straddle demo)

_S_1_2 = (
    "Section 1.2 governs the Meridian Folio, the master title sheet opened for "
    "each registered estate. Part I of the folio describes the property comprised "
    "in it, Part II names the registered owner, and Part III lists burdens. A "
    "solicitor must inspect the Meridian Folio before advising on any purchase. "
) + _filler(6, "ab")

_S_1_3 = (
    "Section 1.3 provides for the Sallowfield Caution, a protective entry that "
    "warns of an unregistered interest and freezes dealings until it is resolved. "
) + _filler(6, "ac")

_S_1_3_1 = (
    "Section 1.3.1 sets out the Renewal Endorsement, by which a Sallowfield "
    "Caution is extended for a further term before it would otherwise lapse. "
) + _filler(6, "ad")

_S_1_4 = (
    "Section 1.4 imposes the Kestrel Undertaking, the personal assurance a "
    "solicitor gives that outstanding title documents will be lodged promptly. "
) + _filler(6, "ae")

# Chapter 2.
_S_2_1 = (
    "Section 2.1 describes the Thornwood Contract, the standard form agreement for "
    "the sale of registered land in Erewhon and the source of the parties' core "
    "obligations. "
) + _filler(6, "ba")

_S_2_2 = (
    "Section 2.2 concerns the Bramblewick Deposit, the sum a purchaser pays on "
    "exchange as security for completion of the Thornwood Contract. "
) + _filler(6, "bb")

_S_2_2_1 = (
    "Section 2.2.1 explains the Bramblewick Escrow, the stakeholder account in "
    "which the deposit is held pending completion or lawful forfeiture. "
) + _filler(6, "bc")

_S_2_2_1_1 = (
    "Section 2.2.1.1 states the Halcyon Release condition, the single trigger on "
    "which escrow funds may be paid out to the vendor without further authority. "
) + _filler(6, "bd")

_S_2_3 = (
    "Section 2.3 requires Form VX-9, the requisition schedule a purchaser serves "
    "to raise queries on title before completion of the sale. "
) + _filler(6, "be")

_S_2_4 = (
    "Section 2.4 governs Peppercorn Completion, the final exchange of purchase "
    "money for the executed transfer and the keys to the property. "
) + _filler(6, "bf")

# Intentional sub-600 runt: the designed trailing-runt-merge demo. Never cited.
_S_2_4_1 = (
    "Section 2.4.1 allows a Peppercorn Adjustment for apportioned outgoings. "
) + _filler(2, "bg")

# Chapter 3.
_S_3_1 = (
    "Section 3.1 introduces the Larkspur Mortgage, the standard charge by which a "
    "lender takes security over a registered estate in Erewhon. "
) + _filler(6, "ca")

_S_3_2 = (
    "Section 3.2 explains how a Windlass Charge is created and registered against "
    "the folio so that it binds a later purchaser of the land. "
) + _filler(6, "cb")

_S_3_2_1 = (
    "Section 3.2.1 defines Fenwick Priority, the rule that ranks competing "
    "Windlass Charges by the order in which they are entered on the folio. "
) + _filler(6, "cc")

_S_3_3 = (
    "Section 3.3 provides for the Cobbleworth Discharge, the entry that removes a "
    "satisfied charge from the folio once the secured debt is repaid. "
) + _filler(6, "cd")

_APPENDIX_3_1 = (
    "This appendix reproduces the Blackthorn Conditions of Sale, the standard "
    "terms incorporated into every Thornwood Contract unless expressly excluded. "
    "The Blackthorn Conditions cover risk, insurance, and the Blackthorn Covenant "
    "levy payable on completion. "
) + _filler(6, "da")


# --- Page assembly ------------------------------------------------------------
# Each entry is either a page string (printed page = its 1-index) or a
# ``(text, printed_page)`` tuple. Front matter carries no printed page (None) and
# sits before CHAPTER 1, so the chunker never emits it. The INDEX line on the
# last page ends the chunkable body, so everything after it is excluded too.
FRONT_MATTER_SENTINEL = "FRONTMATTERSENTINEL_ZZZ"
POST_INDEX_SENTINEL = "POSTINDEXSENTINEL_ZZZ"

SAMPLE_PAGES = [
    # Page 1 — front matter (no printed page).
    (
        "SAMPLE CONVEYANCING HANDBOOK\n"
        "A fictional teaching corpus for the Erewhon conveyancing pipeline. "
        f"{FRONT_MATTER_SENTINEL}. This page is front matter and is not part of "
        "any chapter, so nothing on it should ever appear in a chunk.\n",
        None,
    ),
    # Page 2 — CHAPTER 1, its title, the intro (a sub-600 runt that merges into
    # 1.1), the 1.1 heading, and the first half of 1.1's body.
    (
        "CHAPTER 1\n"
        "REGISTRATION OF TITLE\n"
        "The registration of title system in this fictional jurisdiction is "
        "administered by the Land Registry of Erewhon and introduced here.\n"
        "1.1 The Verdigris Register\n"
        + _S_1_1_HEAD
    ),
    # Page 3 — the rest of 1.1's body (so 1.1 straddles pages 2-3), then 1.2.
    (
        _S_1_1_TAIL + "\n"
        "1.2 The Meridian Folio\n"
        + _S_1_2
    ),
    # Page 4 — 1.3 and its child 1.3.1.
    (
        "1.3 The Sallowfield Caution\n"
        + _S_1_3 + "\n"
        "1.3.1 Renewal Endorsement\n"
        + _S_1_3_1
    ),
    # Page 5 — 1.4.
    (
        "1.4 The Kestrel Undertaking\n"
        + _S_1_4
    ),
    # Page 6 — CHAPTER 2, title, intro (merges into 2.1), 2.1.
    (
        "CHAPTER 2\n"
        "CONTRACTS FOR SALE\n"
        "This chapter states the fictional rules that govern a contract for the "
        "sale of registered land from exchange to completion.\n"
        "2.1 The Thornwood Contract\n"
        + _S_2_1
    ),
    # Page 7 — 2.2.
    (
        "2.2 The Bramblewick Deposit\n"
        + _S_2_2
    ),
    # Page 8 — 2.2.1 and its child 2.2.1.1 (four-level numbering).
    (
        "2.2.1 The Bramblewick Escrow\n"
        + _S_2_2_1 + "\n"
        "2.2.1.1 The Halcyon Release Condition\n"
        + _S_2_2_1_1
    ),
    # Page 9 — 2.3.
    (
        "2.3 Form VX-9 and Requisitions\n"
        + _S_2_3
    ),
    # Page 10 — 2.4 and its sub-600 runt child 2.4.1 (merges back into 2.4).
    (
        "2.4 Peppercorn Completion\n"
        + _S_2_4 + "\n"
        "2.4.1 Peppercorn Adjustment\n"
        + _S_2_4_1
    ),
    # Page 11 — CHAPTER 3, title, intro (merges into 3.1), 3.1.
    (
        "CHAPTER 3\n"
        "MORTGAGES AND CHARGES\n"
        "This chapter states the fictional rules for taking and discharging "
        "security over a registered estate in Erewhon.\n"
        "3.1 The Larkspur Mortgage\n"
        + _S_3_1
    ),
    # Page 12 — 3.2.
    (
        "3.2 The Windlass Charge\n"
        + _S_3_2
    ),
    # Page 13 — 3.2.1 and 3.3.
    (
        "3.2.1 Fenwick Priority Between Charges\n"
        + _S_3_2_1 + "\n"
        "3.3 The Cobbleworth Discharge\n"
        + _S_3_3
    ),
    # Page 14 — the appendix (>= 600 chars, so it survives both merge passes).
    (
        "APPENDIX 3.1 Blackthorn Conditions of Sale\n"
        + _APPENDIX_3_1
    ),
    # Page 15 — the INDEX line ends the chunkable body; the sentinel after it is
    # excluded from every chunk.
    (
        "INDEX\n"
        f"{POST_INDEX_SENTINEL} everything from the index onward is excluded from "
        "chunking, including these words.\n"
    ),
]


def sample_pages() -> list:
    """Return the list of page entries (str or ``(text, printed_page)``)."""
    return SAMPLE_PAGES


def build_sample_corpus() -> tuple:
    """Build the ``(clean_text, page_map, metadata)`` triple.

    This is exactly the contract ``ingest.extract_pdf`` produces for the real
    PDF, so the returned triple can be fed straight to ``chunk_handbook``. Pages
    join with a single ``\\n`` and each ``PageSpan`` records the precise
    ``[char_start, char_end)`` slice it occupies, so ``page_range`` resolves
    offsets exactly as it does on a live extraction.
    """
    parts: list = []
    page_map: list = []
    pos = 0
    pages = sample_pages()
    for i, page in enumerate(pages, start=1):
        text, printed = page if isinstance(page, tuple) else (page, i)
        start = pos
        parts.append(text)
        pos += len(text)
        page_map.append(PageSpan(i, printed, start, pos))
        if i < len(pages):
            parts.append("\n")
            pos += 1
    metadata = {
        "source": SOURCE,
        "title": TITLE,
        "document_type": "handbook",
        "date": "",
    }
    return "".join(parts), page_map, metadata


if __name__ == "__main__":
    # A quick, dependency-free sanity print: chunk the corpus and show what the
    # chunker made of it (no embeddings, no Chroma, no network).
    from src.chunker import chunk_handbook

    clean_text, page_map, metadata = build_sample_corpus()
    docs = chunk_handbook(clean_text, page_map, metadata)
    print(f"pages       : {len(page_map)}")
    print(f"clean chars : {len(clean_text)}")
    print(f"chunks      : {len(docs)}")
    print("sections    :")
    for d in docs:
        m = d.metadata
        pages = (
            f"p.{m['page_start']}"
            if m["page_start"] == m["page_end"]
            else f"pp.{m['page_start']}-{m['page_end']}"
        )
        print(
            f"  {m['section_number']:<14} {pages:<9} "
            f"{len(d.page_content):>5} chars  {m['heading']}"
        )
