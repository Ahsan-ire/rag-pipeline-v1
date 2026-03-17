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

SYSTEM_PROMPT = """You are an Irish legal research assistant. Answer the user's question \
based ONLY on the provided legal context. Follow these rules:

1. Cite specific section numbers, case names, and neutral citations.
2. Use the format [Source: document_name, Section: X] for citations.
3. If the context doesn't contain enough information to answer fully, \
say so explicitly rather than speculating.
4. Where relevant, note whether the Irish position differs from the \
English law position.
5. Use precise legal terminology.
6. Structure your answer with the legal principle first, then the \
authority supporting it."""

PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        (
            "human",
            """Based on the following legal documents, answer the question.

Context:
{context}

Question: {question}

Provide a thorough answer with citations to specific sections.""",
        ),
    ]
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
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=2048,
    )


def generate(question: str, context: str) -> Dict[str, Any]:
    """Generate an answer to a legal question using the provided context.

    Args:
        question: The user's legal question.
        context: Formatted context string from retrieved documents.

    Returns:
        Dict with 'answer' and 'sources' keys.
    """
    llm = get_llm()
    chain = PROMPT_TEMPLATE | llm | StrOutputParser()

    answer = chain.invoke({"question": question, "context": context})

    # Extract source citations from the answer
    source_pattern = re.compile(r"\[Source:\s*([^\]]+)\]")
    sources = source_pattern.findall(answer)

    return {"answer": answer, "sources": sources}


def generate_with_sources(
    question: str, retrieved_results: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Generate an answer using retriever output directly.

    Args:
        question: The user's legal question.
        retrieved_results: Output from retriever.retrieve().

    Returns:
        Dict with 'answer', 'sources', and 'source_documents' keys.
    """
    context = format_context(retrieved_results)
    result = generate(question, context)
    result["source_documents"] = [r["document"] for r in retrieved_results]
    return result
