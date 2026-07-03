"""Tests for the document ingestion module."""

import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.ingest import (
    PageSpan,
    _match_header,
    extract_pdf,
    load_directory,
    load_html_from_url,
    load_html_file,
    load_pdf,
    page_range,
)


def _mock_pdf(page_texts):
    """Build a mocked pdfplumber PDF whose pages yield the given texts.

    A text of "" simulates a page with no extractable text layer.
    """
    pages = []
    for text in page_texts:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)

    mock_pdf = MagicMock()
    mock_pdf.pages = pages
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    return mock_pdf


def _extract(page_texts, path="/test/handbook.pdf"):
    """Run extract_pdf over synthetic page texts with pdfplumber mocked."""
    with patch("src.ingest.pdfplumber.open", return_value=_mock_pdf(page_texts)):
        return extract_pdf(path)


class TestLoadPdf:
    def test_returns_document_with_metadata(self, tmp_path):
        """Test that load_pdf returns a Document with correct metadata."""
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Section 1. This is test legislation."

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("src.ingest.pdfplumber.open", return_value=mock_pdf):
            docs = load_pdf("/test/doc.pdf", "legislation")

        assert len(docs) == 1
        assert "Section 1" in docs[0].page_content
        assert docs[0].metadata["source"] == "/test/doc.pdf"
        assert docs[0].metadata["title"] == "doc.pdf"
        assert docs[0].metadata["document_type"] == "legislation"

    def test_multiple_pages_concatenated(self):
        """Test that multiple PDF pages are joined into one document."""
        page1 = MagicMock()
        page1.extract_text.return_value = "Page one text."
        page2 = MagicMock()
        page2.extract_text.return_value = "Page two text."

        mock_pdf = MagicMock()
        mock_pdf.pages = [page1, page2]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("src.ingest.pdfplumber.open", return_value=mock_pdf):
            docs = load_pdf("/test/doc.pdf")

        assert "Page one text." in docs[0].page_content
        assert "Page two text." in docs[0].page_content

    def test_file_not_found_returns_empty(self):
        """Test that missing files return an empty list."""
        with patch("src.ingest.pdfplumber.open", side_effect=FileNotFoundError):
            docs = load_pdf("/nonexistent/file.pdf")
        assert docs == []

    def test_empty_pdf_returns_empty(self):
        """Test that a PDF with no extractable text returns empty."""
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("src.ingest.pdfplumber.open", return_value=mock_pdf):
            docs = load_pdf("/test/empty.pdf")
        assert docs == []


class TestLoadHtmlFromUrl:
    def test_returns_document_from_url(self):
        """Test HTML fetching and parsing from a URL."""
        html = """
        <html>
        <head><title>Test Act 2024</title></head>
        <body>
        <h1>Test Act 2024</h1>
        <p>Section 1. This is a test provision.</p>
        </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("src.ingest.requests.get", return_value=mock_response):
            docs = load_html_from_url("https://example.com/act", "legislation")

        assert len(docs) == 1
        assert "Section 1" in docs[0].page_content
        assert docs[0].metadata["title"] == "Test Act 2024"
        assert docs[0].metadata["source"] == "https://example.com/act"

    def test_network_error_returns_empty(self):
        """Test that network errors return an empty list."""
        import requests

        with patch(
            "src.ingest.requests.get",
            side_effect=requests.RequestException("Connection failed"),
        ):
            docs = load_html_from_url("https://example.com/bad")
        assert docs == []


class TestLoadDirectory:
    def test_loads_multiple_file_types(self, tmp_path):
        """Test that load_directory handles PDF and text files."""
        # Create a text file
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("This is test legislation text.")

        docs = load_directory(str(tmp_path), "legislation")
        assert len(docs) == 1
        assert "test legislation" in docs[0].page_content

    def test_nonexistent_directory_returns_empty(self):
        """Test that a missing directory returns empty."""
        docs = load_directory("/nonexistent/directory/")
        assert docs == []


class TestMatchHeader:
    """The running-header grammar (D14): shape, digit width, CHAPTER exclusion."""

    def test_recto_shape_returns_printed_number(self):
        assert _match_header("REGISTRATION OF TITLE 87") == 87

    def test_verso_shape_returns_printed_number(self):
        assert _match_header("88 REGISTRATION OF TITLE") == 88

    def test_chapter_marker_is_not_a_header(self):
        # CHAPTER 3 matches the recto shape but must be excluded, or the Phase 2
        # chunker loses every chapter start.
        assert _match_header("CHAPTER 3") is None

    def test_allcaps_title_with_four_digit_year_is_not_a_header(self):
        # \d{1,3} on the page number rejects a 4-digit year, so an all-caps
        # statute title landing first-on-page survives.
        assert _match_header("SUCCESSION ACT 1965") is None

    def test_section_number_line_is_not_a_header(self):
        assert _match_header("3.2.1 Formalities for registration") is None

    def test_lowercase_case_name_is_not_a_header(self):
        # Irish citations use a lowercase "v"; the all-caps title class excludes it.
        assert _match_header("AG v Blake 87") is None


class TestExtractPdf:
    """Page-aware extraction, cleaning, and the page map (D13-D16)."""

    # A small book: 2 front-matter pages (no header), then body pages carrying
    # recto/verso running headers with printed numbers = raw index - 2, and a
    # headerless chapter-opener. Every page's cleaned form is predictable.
    BOOK = [
        "Table of Contents\nintroductory front matter.",          # raw 1, front
        "Table of Cases\nmore front matter.",                     # raw 2, front
        "REGISTRATION OF TITLE 1\n3.1 First body paragraph.",     # raw 3, printed 1
        "2 REGISTRATION OF TITLE\n3.2 Second body paragraph.",    # raw 4, printed 2
        "CHAPTER 2\nProperty Law\n2.1 Chapter two opens here.",   # raw 5, headerless
    ]

    def test_returns_clean_text_and_page_map(self):
        clean_text, page_map = _extract(self.BOOK)
        assert isinstance(clean_text, str) and clean_text
        assert all(isinstance(span, PageSpan) for span in page_map)
        assert len(page_map) == 5

    def test_headers_stripped_recto_and_verso(self):
        clean_text, _ = _extract(self.BOOK)
        assert "REGISTRATION OF TITLE 1" not in clean_text
        assert "2 REGISTRATION OF TITLE" not in clean_text
        assert "3.1 First body paragraph." in clean_text
        assert "3.2 Second body paragraph." in clean_text

    def test_printed_pages_parsed_from_both_shapes(self):
        _, page_map = _extract(self.BOOK)
        by_raw = {span.page_number: span.printed_page for span in page_map}
        assert by_raw[3] == 1  # recto
        assert by_raw[4] == 2  # verso

    def test_front_matter_printed_page_is_none(self):
        _, page_map = _extract(self.BOOK)
        by_raw = {span.page_number: span.printed_page for span in page_map}
        assert by_raw[1] is None
        assert by_raw[2] is None

    def test_chapter_marker_first_line_survives(self):
        # The killer case: CHAPTER as a page's first line, in a regime where
        # headers are being stripped.
        clean_text, _ = _extract(self.BOOK)
        assert "CHAPTER 2" in clean_text
        assert "Property Law" in clean_text

    def test_headerless_body_page_gets_inferred_printed_page(self):
        _, page_map = _extract(self.BOOK)
        by_raw = {span.page_number: span.printed_page for span in page_map}
        assert by_raw[5] == 3  # raw 5 - modal offset 2

    def test_page_map_slices_match_cleaned_page_text(self):
        # The core invariant: clean_text[start:end] is exactly the page's text.
        clean_text, page_map = _extract(self.BOOK)
        expected = {
            1: "Table of Contents\nintroductory front matter.",
            2: "Table of Cases\nmore front matter.",
            3: "3.1 First body paragraph.",
            4: "3.2 Second body paragraph.",
            5: "CHAPTER 2\nProperty Law\n2.1 Chapter two opens here.",
        }
        for span in page_map:
            assert clean_text[span.char_start:span.char_end] == expected[span.page_number]

    def test_page_map_spans_monotonic_and_nonoverlapping(self):
        clean_text, page_map = _extract(self.BOOK)
        assert page_map[0].char_start == 0
        for a, b in zip(page_map, page_map[1:]):
            assert a.char_start < b.char_start
            assert a.char_end > a.char_start
            assert a.char_end <= b.char_start
        assert page_map[-1].char_end <= len(clean_text)

    def test_allcaps_title_with_year_survives_cleaning(self):
        pages = [
            "REGISTRATION OF TITLE 40\n5.1 body one.",
            "41 REGISTRATION OF TITLE\nSUCCESSION ACT 1965\n5.2 body two.",
        ]
        clean_text, _ = _extract(pages)
        assert "SUCCESSION ACT 1965" in clean_text

    def test_same_shape_line_survives_when_not_first_line(self):
        # A caps+number line mid-page is not a header (only first lines are).
        pages = [
            "REGISTRATION OF TITLE 50\n6.1 See FORM 3 55 for the layout.",
            "51 REGISTRATION OF TITLE\n6.2 more.",
        ]
        clean_text, _ = _extract(pages)
        assert "FORM 3 55" in clean_text

    def test_standalone_page_number_stripped_first_and_last_line_only(self):
        pages = ["150\n7.1 Body paragraph.\n150"]
        clean_text, page_map = _extract(pages)
        assert clean_text == "7.1 Body paragraph."

    def test_standalone_number_mid_page_survives(self):
        pages = ["8.1 See item\n150\nin the list."]
        clean_text, _ = _extract(pages)
        assert "150" in clean_text

    def test_hyphenation_repaired_within_page_plain_join(self):
        pages = ["6.1 The regis-\ntration of title. Registration is required."]
        clean_text, _ = _extract(pages)
        assert "registration of title" in clean_text
        assert "regis-" not in clean_text

    def test_hyphenation_attested_compound_keeps_hyphen(self):
        # "co-ownership" appears unbroken earlier, so a later break keeps the hyphen.
        pages = ["5.1 Joint co-ownership exists. A co-\nownership arrangement holds."]
        clean_text, _ = _extract(pages)
        assert "co-ownership arrangement" in clean_text
        assert "coownership" not in clean_text

    def test_hyphenation_repaired_across_pages(self):
        pages = [
            "OLD TITLE 10\n4.1 The deed of convey-",
            "11 OLD TITLE\nance must be registered.",
        ]
        clean_text, page_map = _extract(pages)
        assert "conveyance must be registered" in clean_text
        assert "convey-" not in clean_text
        # The earlier page owns "convey", the later owns "ance": the word straddles.
        idx = clean_text.index("conveyance")
        start_span, end_span = page_range(page_map, idx, idx + len("conveyance"))
        assert start_span.page_number == 1
        assert end_span.page_number == 2

    def test_cross_page_hyphen_with_intervening_header(self):
        # Page 2's raw first line is a header; per-page cleaning removes it BEFORE
        # the boundary repair, so "regis-" joins "tration", not the header.
        pages = [
            "OLD TITLE 10\n4.1 The system of regis-",
            "11 OLD TITLE\ntration is central.",
        ]
        clean_text, _ = _extract(pages)
        assert "registration is central" in clean_text
        assert "OLD TITLE" not in clean_text

    def test_page_empty_after_cleaning_is_skipped_numbering_preserved(self):
        pages = [
            "REGISTRATION OF TITLE 30\n1.1 real body.",
            "REGISTRATION OF TITLE 31",  # header only -> empty after cleaning
            "REGISTRATION OF TITLE 32\n1.2 more body.",
        ]
        _, page_map = _extract(pages)
        assert [span.page_number for span in page_map] == [1, 3]

    def test_page_without_text_layer_is_skipped(self):
        clean_text, page_map = _extract(
            ["1.1 first page.", "", "1.2 third page."]
        )
        assert [span.page_number for span in page_map] == [1, 3]

    def test_no_pages_returns_empty(self):
        clean_text, page_map = _extract([""])
        assert clean_text == ""
        assert page_map == []


class TestPageRange:
    """Offset-to-page lookup, including the separator-gap boundary case."""

    def _map(self):
        # pages "AAAA" [0,4), sep at 4, "BBBB" [5,9)
        return "AAAA\nBBBB", [
            PageSpan(1, 10, 0, 4),
            PageSpan(2, 11, 5, 9),
        ]

    def test_maps_position_to_its_page(self):
        _, page_map = self._map()
        start, end = page_range(page_map, 6, 8)
        assert start.page_number == 2 and end.page_number == 2

    def test_separator_gap_maps_to_preceding_page(self):
        _, page_map = self._map()
        start, _ = page_range(page_map, 4, 5)  # position 4 is the "\n" gap
        assert start.page_number == 1

    def test_span_across_two_pages(self):
        _, page_map = self._map()
        start, end = page_range(page_map, 2, 7)
        assert start.page_number == 1 and end.page_number == 2

    def test_empty_map_raises(self):
        with pytest.raises(ValueError):
            page_range([], 0, 1)


class TestLoadPdfCleaning:
    """load_pdf keeps its single-Document contract while now cleaning text."""

    def test_returns_single_cleaned_document(self):
        with patch(
            "src.ingest.pdfplumber.open",
            return_value=_mock_pdf(["REGISTRATION OF TITLE 5\n3.1 The body text here."]),
        ):
            docs = load_pdf("/test/handbook.pdf", "handbook")
        assert len(docs) == 1
        assert "REGISTRATION OF TITLE 5" not in docs[0].page_content
        assert "3.1 The body text here." in docs[0].page_content
        assert docs[0].metadata["source"] == "/test/handbook.pdf"
        assert docs[0].metadata["title"] == "handbook.pdf"
        assert docs[0].metadata["document_type"] == "handbook"
