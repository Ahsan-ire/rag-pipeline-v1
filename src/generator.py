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

# Generation model, hoisted to a constant so the eval-report provenance block
# can import it instead of hardcoding a second copy of the model string.
GENERATION_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = f"""You are an Irish legal research assistant answering questions about the \
Law Society of Ireland Conveyancing Handbook. Answer using ONLY the provided \
context. Follow these rules:

1. Ground every statement in the context. Do not rely on outside legal knowledge.
2. Cite the source of each claim using the exact bracketed locator shown in that \
source's header, e.g. [Handbook, para 14.8.5, p.412] or, for an appendix source, \
[Handbook, APPENDIX 14.1, p.566]. Copy the locator exactly as the header shows it \
— never rewrite an APPENDIX locator as a para number. Give the locator and page \
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

# Citation extractor. Anchors on the locator token and requires the page segment
# to follow it immediately (``, p.``/``, pp.``): both the compact
# ``[Handbook, para 3.2.1, p.87]`` header and the longer ``[Conveyancing
# Handbook, Ch.3 Some Title, para 3.2.1, p.87]`` prefix baked into every chunk
# (D21) are valid, and the chapter-title segment is OCR'd free text that can
# itself contain commas. Everything between ``[`` and the locator token is an
# opaque run that never crosses a closing bracket (``[^\]]``) — but between the
# locator number and the page only ``,`` plus whitespace is allowed, because in
# both emitted grammars the locator segment sits directly before the page
# segment. That adjacency is load-bearing: a locator-shaped token inside the
# free-text title run (e.g. a chapter title containing "see Appendix 3") is not
# followed by ``, p.N``, so it can no longer hijack the match away from the real
# locator (gate-review regression fix, D34 addendum). It also bounds regex
# backtracking on degenerate unclosed-bracket input to linear.
#
# Two mutually exclusive locator forms, per the chunker's two-grammar prefix
# (chunker._prefix / chunker.locator_label): a numbered paragraph,
# ``para <digits>`` (named group ``para``), or a verbatim appendix locator,
# ``APPENDIX <digits>`` (named group ``appendix``) — chunk metadata renders the
# appendix form with no "para" token, and the model may echo it in any case
# ("Appendix", "APPENDIX"), so only that token is wrapped in a scoped
# case-insensitive group ``(?i:...)``; the rest of the pattern (including the
# numbered-paragraph branch) stays case-sensitive per project convention. When
# a bracket somehow contains two locator tokens, the one adjacent to the page
# segment wins — deterministic, and a plain paragraph can never be captured as
# an appendix or vice versa. Captures (paragraph number OR appendix number,
# first page).
CITATION_RE = re.compile(
    r"\[[^\]]*?(?:\bpara\s+(?P<para>\d+(?:\.\d+)*)"
    r"|\b(?i:appendix)\s+(?P<appendix>\d+(?:\.\d+)*))"
    r"\s*,\s*pp?\.\s*(?P<page>\d+)[^\]]*?\]"
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
        model=GENERATION_MODEL,
        max_tokens=2048,
        # Sonnet 5 rejects a non-default temperature (400), so we omit it and
        # steer determinism through the system prompt instead. Thinking is held
        # off (it is on-by-default on Sonnet 5) to keep behaviour comparable to
        # the claude-sonnet-4-6 baseline this change was isolated from (D29).
        thinking={"type": "disabled"},
    )


def extract_citations(text: str) -> List[Dict[str, str]]:
    """Pull ``(paragraph-or-appendix, page)`` citations out of an answer.

    Returns one dict per bracketed locator found, with keys ``para``, ``page``,
    and a ``raw`` display string. Tolerant of both the compact and the long
    (D21-prefix) bracket forms, and of either locator grammar — a numbered
    paragraph (``para 3.2.1``) or a verbatim appendix (``APPENDIX 14.1``,
    matched case-tolerantly since the model may write "Appendix") — see
    ``CITATION_RE``.

    For a paragraph match, ``para`` holds the bare number (e.g. ``"3.2.1"``)
    and ``raw`` is ``"para 3.2.1, p.87"``, unchanged from before. For an
    appendix match, ``para`` holds the canonical uppercase label (e.g.
    ``"APPENDIX 14.1"``, normalized regardless of how the model cased it) and
    ``raw`` is ``"APPENDIX 14.1, p.87"`` — no "para" token, since the appendix
    grammar never uses one (one-grammar rule; this string is what the CLI
    prints under "Citations found:").
    """
    citations: List[Dict[str, str]] = []
    for match in CITATION_RE.finditer(text):
        page = match.group("page")
        para = match.group("para")
        if para is not None:
            citations.append({"para": para, "page": page, "raw": f"para {para}, p.{page}"})
        else:
            label = f"APPENDIX {match.group('appendix')}"
            citations.append({"para": label, "page": page, "raw": f"{label}, p.{page}"})
    return citations


def is_refusal(answer: str) -> bool:
    """True if the answer IS the canonical refusal sentence, and nothing more.

    Contract: the answer must equal ``REFUSAL_PHRASE`` after normalizing:
    (a) strip surrounding whitespace; (b) strip one layer of surrounding
    matching quotes (straight ``"``/``'`` or curly ``""``/``''`` — the system
    prompt shows the phrase inside quotes, so models sometimes echo the
    quote marks); (c) strip trailing period(s); (d) strip whitespace again;
    (e) casefold. Only then is it compared, case-insensitively, to
    ``REFUSAL_PHRASE``.

    A plain substring check is unsafe: a hedged partial answer like "This is
    not covered in the source material, but the likely answer is 20 days."
    contains the phrase yet is a real (if evasive) answer, not a refusal —
    substring matching would score it as a refusal. Requiring an exact match
    after normalization closes that hole. Because an exact match can never
    also contain a bracket citation, the previous
    ``and not extract_citations(answer)`` guard is now redundant and has been
    removed.

    Steps (b) and (c) are order-*independent*: the system prompt itself
    displays the refusal sentence as ``"phrase".`` — straight quotes with the
    period OUTSIDE the closing quote — so a model can echo either
    ``"phrase".`` (period outside) or ``"phrase."`` (period inside). A single
    fixed-order pass (quotes-then-period) only strips one of those shapes: for
    ``"phrase".`` the trailing character is ``.``, not a closing quote, so the
    quote-stripping check never fires, the string is left as ``"phrase".``,
    and the match fails. Looping the whole normalization pass to a fixed
    point handles both interleavings: each pass strips whichever layer
    (quote or period) is currently outermost, so the quote eventually becomes
    outermost and gets stripped regardless of which layer started outside.

    The Phase 5 eval imports this function directly, so eval refusal scoring
    inherits this same strict definition automatically.
    """
    quote_pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]
    normalized = answer

    # Iterate to a fixed point instead of a fixed sequence: whichever layer
    # (quotes or trailing period) currently sits outermost gets peeled first,
    # so the fix works no matter which one the model put on the outside. Each
    # pass strictly shortens the string or leaves it unchanged, so this
    # always terminates; the small cap is just a defensive belt-and-braces
    # bound, not something normal input should ever reach.
    for _ in range(5):
        before = normalized
        normalized = normalized.strip()
        if len(normalized) >= 2 and any(
            normalized[0] == open_q and normalized[-1] == close_q
            for open_q, close_q in quote_pairs
        ):
            normalized = normalized[1:-1]
        normalized = normalized.rstrip(".")
        normalized = normalized.strip()
        if normalized == before:
            break

    return normalized.casefold() == REFUSAL_PHRASE.casefold()


def _sections_related(cited: str, chunk_section: str) -> bool:
    """True if two section locators are equal or one nests the other.

    D34: appendix-ness must match on both sides — never-cross-match. A plain
    numbered paragraph like ``"14.1"`` never relates to an appendix locator
    like ``"APPENDIX 14.1"``, in either direction, even though the numeric
    tail is identical; they are different grammars (see ``CITATION_RE``'s
    ``para`` vs ``appendix`` branches) naming different structures in the
    handbook. When both sides ARE appendix locators, the ``"APPENDIX "``
    prefix is stripped from each before applying the same nesting rule used
    for plain paragraphs, so ``"APPENDIX 14.1"`` relates to its sub-item
    ``"APPENDIX 14.1.2"`` the same way ``"14.12"`` relates to ``"14.12.1"``.

    Otherwise (both plain paragraphs), compared component-wise on dot
    boundaries: ``14.12`` relates to its sub-paragraph ``14.12.1`` (a more
    capable model often cites the finer sub-paragraph that lives inside a
    chunk whose ``section_number`` is the parent), while ``14.1`` does NOT
    relate to ``14.12``.

    No case-tolerance here: metadata, canonical extraction output (see
    ``extract_citations``), and golden-set expectations are all uppercase
    already — case handling lives only at the extraction boundary.
    """
    if not cited or not chunk_section:
        return False

    a_is_appendix = cited.startswith("APPENDIX")
    b_is_appendix = chunk_section.startswith("APPENDIX")
    if a_is_appendix != b_is_appendix:
        return False

    if a_is_appendix:
        cited = cited.removeprefix("APPENDIX ")
        chunk_section = chunk_section.removeprefix("APPENDIX ")

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
