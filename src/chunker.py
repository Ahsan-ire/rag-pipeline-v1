"""Legal-aware document chunking module.

Splits Irish legal documents by structural boundaries (Parts, Sections, Subsections)
before falling back to recursive character splitting for oversized chunks.
"""

import re
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

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
