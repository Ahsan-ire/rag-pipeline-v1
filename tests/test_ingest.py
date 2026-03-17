"""Tests for the document ingestion module."""

import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.ingest import load_directory, load_html_from_url, load_html_file, load_pdf


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
