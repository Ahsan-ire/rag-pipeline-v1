"""LLM response generation module using Claude for legal Q&A."""

import os
import re
from typing import Any, Dict, List

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from src.retriever import format_context

load_dotenv()

# Canonical refusal sentence. Defined once here and imported wherever refusal is
# produced or detected (system prompt below, and Phase 5's eval matcher) so the
# string cannot drift between the generator and the harness that scores it.
REFUSAL_PHRASE = "not covered in the source material"

SYSTEM_PROMPT = f"""You are an Irish legal research assistant answering questions about the \
Law Society of Ireland Conveyancing Handbook. Answer using ONLY the provided \
context. Follow these rules:

1. Ground every statement in the context. Do not rely on outside legal knowledge.
2. Cite the source of each claim using the exact bracketed locator shown in that \
source's header, e.g. [Handbook, para 14.8.5, p.412]. Give the paragraph and page \
for every statement you make.
3. If the context does not contain enough information to answer the question, \
reply with exactly this sentence and nothing else: "{REFUSAL_PHRASE}". Do not \
guess, speculate, or fall back on general knowledge.
4. Use precise legal terminology; state the legal principle first, then the \
authority (paragraph and page) supporting it."""

PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        (
            "human",
            """Based on the following handbook extracts, answer the question.

Context:
{context}

Question: {question}

Answer using the bracketed locators from the source headers. If the extracts do \
not cover the question, reply with the exact refusal sentence.""",
        ),
    ]
)

# Citation extractor. Anchors on the ``para`` and ``p.``/``pp.`` tokens rather than
# splitting on commas: both the compact ``[Handbook, para 3.2.1, p.87]`` header and
# the longer ``[Conveyancing Handbook, Ch.3 Some Title, para 3.2.1, p.87]`` prefix
# baked into every chunk (D21) are valid, and the chapter-title segment is OCR'd
# free text that can itself contain commas. Everything between ``[`` and ``para``
# (and between the paragraph number and the page) is treated as an opaque run that
# never crosses a closing bracket (``[^\]]``). Captures (paragraph, first page).
CITATION_RE = re.compile(
    r"\[[^\]]*?\bpara\s+(\d+(?:\.\d+)*)[^\]]*?\bpp?\.\s*(\d+)[^\]]*?\]"
)


def get_llm() -> ChatAnthropic:
    """Create and return a ChatAnthropic LLM instance.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your-api-key-here":
        raise ValueError(
            "ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key."
        )

    return ChatAnthropic(
        model="claude-sonnet-5",
        max_tokens=2048,
        # Sonnet 5 rejects a non-default temperature (400), so we omit it and
        # steer determinism through the system prompt instead. Thinking is held
        # off (it is on-by-default on Sonnet 5) to keep behaviour comparable to
        # the claude-sonnet-4-6 baseline this change was isolated from (D29).
        thinking={"type": "disabled"},
    )


def extract_citations(text: str) -> List[Dict[str, str]]:
    """Pull ``(paragraph, page)`` citations out of an answer.

    Returns one dict per bracketed locator found, with keys ``para``, ``page``,
    and a ``raw`` display string. Tolerant of both the compact and the long
    (D21-prefix) bracket forms — see ``CITATION_RE``.
    """
    return [
        {"para": para, "page": page, "raw": f"para {para}, p.{page}"}
        for para, page in CITATION_RE.findall(text)
    ]


def is_refusal(answer: str) -> bool:
    """True if the answer is the canonical refusal.

    A refusal must contain the canonical phrase (case-insensitive) AND carry
    no extractable citations: a partial answer that hedges with the phrase
    mid-sentence while still citing sources is an answer, not a refusal.
    Shared with the Phase 5 eval so refusal detection uses one definition.
    """
    return REFUSAL_PHRASE.lower() in answer.lower() and not extract_citations(answer)


def _sections_related(cited: str, chunk_section: str) -> bool:
    """True if two decimal paragraph numbers are equal or one nests the other.

    Compared component-wise on dot boundaries, so ``14.12`` relates to its
    sub-paragraph ``14.12.1`` (a more capable model often cites the finer
    sub-paragraph that lives inside a chunk whose ``section_number`` is the
    parent), while ``14.1`` does NOT relate to ``14.12``.
    """
    if not cited or not chunk_section:
        return False
    a = cited.split(".")
    b = chunk_section.split(".")
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _citation_matches_chunk(
    citation: Dict[str, str], retrieved_results: List[Dict[str, Any]]
) -> bool:
    """True if the citation's page lies in a retrieved chunk that covers its paragraph.

    A chunk covers the citation when its ``section_number`` nests-or-equals the
    cited paragraph (see ``_sections_related``) and the cited page falls within
    the chunk's ``[page_start, page_end]`` span. A related paragraph with no page
    metadata counts (nothing to contradict the page). An invented paragraph, or a
    real one cited on a page no retrieved chunk covers, stays ungrounded.
    """
    para = citation["para"]
    try:
        page = int(citation["page"])
    except (TypeError, ValueError):
        return False

    for result in retrieved_results:
        meta = result["document"].metadata
        if not _sections_related(para, str(meta.get("section_number", ""))):
            continue
        start = meta.get("page_start")
        if start is None:
            return True  # paragraph related; no page info to contradict it
        end = meta.get("page_end", start) or start
        if start <= page <= end:
            return True
    return False


def validate_citations(
    citations: List[Dict[str, str]], retrieved_results: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, str]]]:
    """Split cited (paragraph, page) pairs into grounded vs. ungrounded.

    Grounding is the real anti-hallucination guarantee — not the citation format.
    A citation is *grounded* only if it maps to a chunk that was actually
    retrieved (see ``_citation_matches_chunk``); *ungrounded* citations are the
    model inventing or misremembering a locator, which is what we surface.
    """
    grounded: List[Dict[str, str]] = []
    ungrounded: List[Dict[str, str]] = []
    for citation in citations:
        if _citation_matches_chunk(citation, retrieved_results):
            grounded.append(citation)
        else:
            ungrounded.append(citation)
    return {"grounded": grounded, "ungrounded": ungrounded}


def generate(question: str, context: str) -> Dict[str, Any]:
    """Generate an answer to a legal question using the provided context.

    Args:
        question: The user's legal question.
        context: Formatted context string from retrieved documents.

    Returns:
        Dict with 'answer', 'citations' (list of {para, page, raw}), and
        'sources' (the raw display strings, kept for backward compatibility).
    """
    llm = get_llm()
    chain = PROMPT_TEMPLATE | llm | StrOutputParser()

    answer = chain.invoke({"question": question, "context": context})

    citations = extract_citations(answer)
    return {
        "answer": answer,
        "citations": citations,
        "sources": [c["raw"] for c in citations],
    }


def generate_with_sources(
    question: str, retrieved_results: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Generate an answer using retriever output directly.

    Args:
        question: The user's legal question.
        retrieved_results: Output from retriever.retrieve().

    Returns:
        Dict with 'answer', 'citations', 'sources', 'source_documents', and
        'citation_check' ({grounded, ungrounded}) keys.
    """
    context = format_context(retrieved_results)
    result = generate(question, context)
    result["source_documents"] = [r["document"] for r in retrieved_results]
    result["citation_check"] = validate_citations(
        result["citations"], retrieved_results
    )
    return result
