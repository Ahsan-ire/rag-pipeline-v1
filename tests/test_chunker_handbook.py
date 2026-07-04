"""Tests for the handbook chunking strategy (Phase 2 / D19-D21).

Kept separate from ``test_chunker.py`` (which covers the legislation strategy)
so each strategy's fixtures and intent stay legible. All IO is synthetic: a
handbook is built from a list of page strings plus a hand-made page map, exactly
the ``(clean_text, page_map)`` contract ``ingest.extract_pdf`` produces.
"""

import pytest

from src.ingest import PageSpan
from src.chunker import (
    OVERSIZE_CHAR_THRESHOLD,
    RUNT_CHAR_THRESHOLD,
    chunk_handbook,
)

def _body(n: int, tag: str = "x") -> str:
    """A paragraph of ``n`` distinct ~74-char clauses (so runt/oversize sizing is
    predictable and, crucially, the text is *non-repeating* — repeated phrasing
    would make sub-chunk offset recovery ambiguous, which is a fixture artefact,
    not a chunker property to test). Use a distinct ``tag`` per page to keep
    clauses globally unique when a segment must span pages."""
    return "".join(
        f"Clause {tag}{i} in this part sets out a rule of conveyancing practice in detail. "
        for i in range(n)
    )


def _handbook(pages, title="Conveyancing_Handbook.pdf"):
    """Build ``(clean_text, page_map, metadata)`` from page strings.

    Each element is either a page's text (printed page = its raw 1-index) or a
    ``(text, printed_page)`` tuple. Pages join with a single ``\\n``, matching
    ingest's page-join policy, and each PageSpan records the exact slice it
    occupies — so ``page_range`` resolves offsets the same way it does live.
    """
    parts, page_map, pos = [], [], 0
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
        "source": "/data/" + title,
        "title": title,
        "document_type": "handbook",
        "date": "",
    }
    return "".join(parts), page_map, metadata


def _by_section(docs):
    """Map section_number -> chunk Document (assumes unique section numbers)."""
    return {d.metadata["section_number"]: d for d in docs}


def _sections(docs):
    return {d.metadata["section_number"] for d in docs}


class TestChapterWalk:
    def test_multi_chapter_metadata_and_pages(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 1\nGENERAL PRINCIPLES\n1.1 Overview\n" + _body(10),
            "1.2 Duties\n" + _body(10),
            "CHAPTER 2\nCONTRACTS\n2.1 Formation\n" + _body(10),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        by_sec = _by_section(docs)

        assert {"1.1", "1.2", "2.1"} <= set(by_sec)
        assert by_sec["1.1"].metadata["chapter_number"] == 1
        assert by_sec["1.1"].metadata["chapter_title"] == "General Principles"
        assert by_sec["2.1"].metadata["chapter_number"] == 2
        # 1.2 lives on printed page 2; 2.1's chapter opener on page 3.
        assert by_sec["1.2"].metadata["page_start"] == 2
        assert by_sec["2.1"].metadata["page_start"] == 3

    def test_four_level_numbering_accepted(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 A\n" + _body(10)
            + "\n3.1.1 B\n" + _body(10)
            + "\n3.1.1.1 C\n" + _body(10),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert {"3.1", "3.1.1", "3.1.1.1"} <= _sections(docs)

    def test_front_matter_before_first_marker_excluded(self):
        clean_text, page_map, meta = _handbook([
            "Table of Cases\nSmith v Jones garbled front-matter column splice. " + _body(5),
            "CHAPTER 1\nGENERAL\n1.1 Start\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        joined = " ".join(d.page_content for d in docs)
        assert "Table of Cases" not in joined
        assert "Smith v Jones" not in joined
        assert "1.1" in _sections(docs)


class TestHeadingGuards:
    def test_part_of_the_folio_is_not_a_boundary(self):
        # IMPLEMENTATION_PLAN.md:58 — a prose line opening "Part I of the folio"
        # must not be mistaken for a structural marker.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 5\nMORTGAGES\n5.1 Overview\n"
            "Part I of the folio describes the property comprised in it. " + _body(10),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert "5.1" in _sections(docs)
        assert not any("Part" in s for s in _sections(docs))
        assert any("Part I of the folio" in d.page_content for d in docs)

    def test_quoted_leading_zero_number_rejected(self):
        # "3.01" is Law Society list numbering, not a heading (guard iii).
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Real Heading\n" + _body(10)
            + "\n3.01 Quoted numbering from an older edition of the rules. " + _body(5),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert "3.01" not in _sections(docs)
        assert any("3.01 Quoted numbering" in d.page_content for d in docs)

    def test_eregistration_lowercase_e_heading_accepted(self):
        # "14.18 eRegistration" is the one real casualty guard (ii)'s e[A-Z] saves.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 14\nELECTRONIC CONVEYANCING\n14.1 Intro\n" + _body(10)
            + "\n14.18 eRegistration\n" + _body(10),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        by_sec = _by_section(docs)
        assert "14.18" in by_sec
        assert by_sec["14.18"].metadata["heading"] == "eRegistration"

    def test_dot_leader_line_rejected(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Real\n" + _body(10)
            + "\n3.5 Fake dot leader entry............ 90\n" + _body(5),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert "3.5" not in _sections(docs)
        assert "3.1" in _sections(docs)

    def test_bare_number_does_not_scavenge_next_line(self):
        # A bare decimal alone on a line (a wrapped cross-reference) must not pair
        # with the following physical line to form a spurious heading — the
        # number and title must be on the SAME line.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Real Heading\n" + _body(15)
            + "\nas discussed earlier in\n3.2\nSome Following Prose Line\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert "3.2" not in _sections(docs)  # bare "3.2\n" is not a heading
        assert "3.1" in _sections(docs)

    def test_cross_chapter_reference_rejected(self):
        # A "9.4 ..." line inside chapter 3 is a cross-reference, not a heading.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Real\n" + _body(10)
            + "\n9.4 Reference to a section in another chapter entirely. " + _body(5),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert "9.4" not in _sections(docs)
        assert any("9.4 Reference to a section" in d.page_content for d in docs)


class TestChapterTitle:
    def test_two_line_title(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 1\nTHE STRUCTURE OF\nTHE LEGAL PROFESSION\n1.1 Intro\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert docs[0].metadata["chapter_title"] == "The Structure Of The Legal Profession"

    def test_epigraph_under_title_not_folded_into_title(self):
        # Chapters 3 and 5 open with a mixed-case epigraph directly under the
        # ALL-CAPS title with no blank line. The epigraph must stay in the body,
        # not pollute chapter_title / the citation prefix of every chunk.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nETHICS\n"
            "There is nothing which so generally strikes the imagination of mankind. "
            "Solicitor's Guide to Professional Conduct (Law Society of Ireland, 2022)\n"
            "3.1 Professional Conduct\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        chunk = _by_section(docs)["3.1"]
        assert chunk.metadata["chapter_title"] == "Ethics"
        prefix = chunk.page_content.split("] ", 1)[0]
        assert "strikes" not in prefix          # epigraph is NOT in the citation
        assert "strikes the imagination" in chunk.page_content  # ...but stays in the body


class TestRuntPolicy:
    def test_descendant_runt_merges_keeping_parent_identity(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Big Section\n" + _body(15)
            + "\n3.2 Short\nBrief.\n3.2.1 Detail\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        sections = _sections(docs)
        assert "3.2" in sections
        assert "3.2.1" not in sections  # absorbed into its parent
        parent = _by_section(docs)["3.2"]
        assert "3.2.1 Detail" in parent.page_content

    def test_sibling_runt_stays_standalone(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nTITLES\n3.1 Alpha\nShort alpha paragraph.\n3.2 Beta\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        sections = _sections(docs)
        assert "3.1" in sections and "3.2" in sections  # a correct citation beats a size floor
        assert "Short alpha paragraph." in _by_section(docs)["3.1"].page_content

    def test_chained_runts_collapse_to_ancestor(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 3\nT\n3.1 A\nx.\n3.1.1 B\ny.\n3.1.1.1 C\nz.\n3.2 Sibling\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        sections = _sections(docs)
        assert "3.1" in sections and "3.2" in sections
        assert "3.1.1" not in sections and "3.1.1.1" not in sections
        chunk = _by_section(docs)["3.1"]
        assert "3.1.1 B" in chunk.page_content and "3.1.1.1 C" in chunk.page_content

    def test_furniture_intro_adopts_next_identity(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 7\nCONVEYANCING\n7.1 First\n" + _body(15),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        assert len(docs) == 1
        chunk = docs[0]
        assert chunk.metadata["section_number"] == "7.1"
        assert chunk.metadata["chapter_title"] == "Conveyancing"
        # The intro text is folded in, but the citation is the first section's.
        body = chunk.page_content.split("] ", 1)[1]
        assert body.startswith("CHAPTER 7")
        assert "" not in _sections(docs) or len(docs) == 1  # no orphan intro chunk

    def test_trailing_runt_merges_backward(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 4\nDEEDS\n4.1 Main\n" + _body(15) + "\n4.2 Tiny trailing\nEnd.\n",
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        sections = _sections(docs)
        assert "4.1" in sections
        assert "4.2" not in sections  # trailing runt folded into the previous section
        assert "4.2 Tiny trailing" in _by_section(docs)["4.1"].page_content


class TestAppendixAndIndex:
    def test_appendix_stub_merges_backward_and_index_tail_excluded(self):
        clean_text, page_map, meta = _handbook([
            "CHAPTER 6\nSALES\n6.1 Overview\n" + _body(10),
            "6.2 Completion\n" + _body(10)
            + "\nAPPENDIX 6.1 Conditions of Sale\n"
            + "INDEX\nabsolutely everything past the index must be excluded " + _body(30),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        joined = " ".join(d.page_content for d in docs)

        assert {"6.1", "6.2"} <= _sections(docs)
        assert "everything past the index" not in joined  # INDEX tail dropped
        assert "APPENDIX 6.1" not in _sections(docs)       # stub not its own chunk
        assert "Conditions of Sale" in _by_section(docs)["6.2"].page_content


class TestOversizeSplit:
    def test_oversize_segment_split_with_inherited_metadata_and_pages(self):
        # 8.1 spans two printed pages and is well over the 4000-char trigger.
        # Distinct tags keep the two pages' clauses globally unique.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 8\nBIG CHAPTER\n8.1 Huge\n" + _body(40, "a"),
            _body(45, "b"),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)

        assert len(docs) > 1                               # it split
        assert all(d.metadata["section_number"] == "8.1" for d in docs)   # inheritance
        assert all(d.metadata["chapter_number"] == 8 for d in docs)
        # Per-sub-chunk page recovery: at least one sub-chunk lands on page 2.
        starts = {d.metadata["page_start"] for d in docs}
        assert 1 in starts and 2 in starts
        # Each sub-chunk stays within the oversize band, none is empty.
        for d in docs:
            body = d.page_content.split("] ", 1)[1]
            assert 0 < len(body) <= OVERSIZE_CHAR_THRESHOLD


class TestPrefixAndEdgeCases:
    def test_none_printed_page_omits_page_component(self):
        clean_text, page_map, meta = _handbook([
            ("CHAPTER 9\nEQUITY\n9.1 Trusts\n" + _body(15), None),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        prefix = docs[0].page_content.split("] ", 1)[0] + "] "
        assert prefix == "[Conveyancing Handbook, Ch.9 Equity, para 9.1] "
        assert docs[0].metadata["page_start"] is None

    def test_spanning_pages_render_as_range(self):
        # A section whose text straddles two printed pages cites pp.X–Y.
        clean_text, page_map, meta = _handbook([
            "CHAPTER 2\nCONTRACTS\n2.1 Long\n" + _body(20),
            _body(20) + "\n2.2 Next\n" + _body(10),
        ])
        docs = chunk_handbook(clean_text, page_map, meta)
        chunk = _by_section(docs)["2.1"]
        assert chunk.metadata["page_start"] == 1
        assert chunk.metadata["page_end"] == 2
        assert "pp.1–2" in chunk.page_content

    def test_zero_markers_raises_valueerror(self):
        clean_text, page_map, meta = _handbook([
            "Just front matter with no chapter markers whatsoever. " + _body(5),
        ])
        with pytest.raises(ValueError, match="CHAPTER"):
            chunk_handbook(clean_text, page_map, meta)
