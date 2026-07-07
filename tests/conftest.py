"""Shared test fixtures for the legal RAG pipeline."""

import pytest
from langchain_core.documents import Document


@pytest.fixture
def sample_document():
    """A sample legal document for testing."""
    return Document(
        page_content="""PART I
PRELIMINARY

Section 1.
This Act may be cited as the Succession Act, 1965.

Section 2.
In this Act, unless the context otherwise requires—
"the court" means the High Court;
"personal representative" means an executor or administrator for the time being of a deceased person.

PART II
WILLS

Section 77.
(1) Every person who has attained the age of eighteen years or is or has been married may make a valid will.
(2) No will made by a person who has not attained the age of eighteen years shall be valid.

Section 78.
A will shall be in writing and shall be executed in the following manner.""",
        metadata={
            "source": "/test/succession_act_1965.pdf",
            "title": "Succession Act 1965",
            "document_type": "legislation",
            "date": "1965",
        },
    )


@pytest.fixture
def sample_chunks():
    """Pre-chunked documents for testing."""
    return [
        Document(
            page_content="[From: Succession Act 1965, Section 77] Every person who has attained the age of eighteen years may make a valid will.",
            metadata={
                "source": "/test/succession_act_1965.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "77",
                "parent_section": "PART II",
            },
        ),
        Document(
            page_content="[From: Succession Act 1965, Section 78] A will shall be in writing and shall be executed in the following manner.",
            metadata={
                "source": "/test/succession_act_1965.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "78",
                "parent_section": "PART II",
            },
        ),
    ]


@pytest.fixture
def mock_retrieved_results(sample_chunks):
    """Mock retriever output."""
    return [
        {"document": sample_chunks[0], "score": 0.85, "metadata": sample_chunks[0].metadata},
        {"document": sample_chunks[1], "score": 0.72, "metadata": sample_chunks[1].metadata},
    ]


@pytest.fixture
def handbook_chunks():
    """Pre-chunked handbook documents, carrying the D21 in-text prefix and the
    Phase 2 metadata keys (section_number, page_start, page_end). The second
    chunk spans two pages to exercise the range-rendering path."""
    return [
        Document(
            page_content=(
                "[Conveyancing Handbook, Ch.14 Registration Of Title, para 14.8.5, p.412] "
                "The priority entry protects a purchaser pending completion of the sale."
            ),
            metadata={
                "source": "Conveyancing_Handbook.pdf",
                "title": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "section_number": "14.8.5",
                "page_start": 412,
                "page_end": 412,
            },
        ),
        Document(
            page_content=(
                "[Conveyancing Handbook, Ch.1 Introduction, para 1.2, pp.1–2] "
                "The objectives of the manual are set out for the practitioner."
            ),
            metadata={
                "source": "Conveyancing_Handbook.pdf",
                "title": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "section_number": "1.2",
                "page_start": 1,
                "page_end": 2,
            },
        ),
    ]


@pytest.fixture
def handbook_retrieved_results(handbook_chunks):
    """Mock retriever output for handbook chunks (fused RRF scores)."""
    return [
        {"document": handbook_chunks[0], "score": 0.03279, "metadata": handbook_chunks[0].metadata},
        {"document": handbook_chunks[1], "score": 0.01639, "metadata": handbook_chunks[1].metadata},
    ]
