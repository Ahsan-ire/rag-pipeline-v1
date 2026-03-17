"""Document ingestion module for loading legal documents from PDF and HTML sources."""

import logging
import os
from typing import List, Optional

import pdfplumber
import requests
from bs4 import BeautifulSoup
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


def load_pdf(file_path: str, document_type: str = "legislation") -> List[Document]:
    """Load a PDF file and return a list containing a single Document.

    Concatenates all pages into one Document so the chunker can split
    on legal boundaries rather than arbitrary page breaks.
    """
    try:
        with pdfplumber.open(file_path) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)

            full_text = "\n".join(pages_text)
            if not full_text.strip():
                logger.warning("No text extracted from PDF: %s", file_path)
                return []

            return [
                Document(
                    page_content=full_text,
                    metadata={
                        "source": file_path,
                        "title": os.path.basename(file_path),
                        "document_type": document_type,
                        "date": "",
                    },
                )
            ]
    except FileNotFoundError:
        logger.error("PDF file not found: %s", file_path)
        return []
    except Exception as e:
        logger.error("Error loading PDF %s: %s", file_path, e)
        return []


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
