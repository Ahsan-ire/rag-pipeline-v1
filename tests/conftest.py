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
