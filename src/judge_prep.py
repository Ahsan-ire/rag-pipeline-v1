"""Everything between a generated answer and the judge's prompt (16A-2b b1a r6, D74).

The judge (``src.judge``) scores the answers it is given. *Which* answers it is given and *what evidence it sees* are
decided here, so this module is part of the judge's identity (``src.judge.JUDGE_CODE_MODULES``) as well as of the pipeline
hash: a candidate that changes generation's context (``src.retriever.format_context``) must not change what the judge
reads, and one that changes what counts as a complete or a refused answer must not change which answers are judged
without the judge's registered hash moving. It owns:

* :func:`is_refusal`: the refusal test (``src.generator`` imports it: there is one definition);
* :func:`normalise_answer`: a generation result to ``(text, status)`` (``src.evaluator._normalise_answer`` is this function);
* :func:`is_judgeable`: whether one row's answer is judged (a complete, non-refused answer of a retrieval-eligible row);
* :func:`format_judge_context`: the context text the judge reads, a verbatim copy of ``retriever.format_context`` that
  the judge keeps for itself (a test holds the two equal today);
* :func:`assemble_judge_items`: the rows the judge is handed, from the evaluator's answer cache;
* :func:`judgments_by_row`: the judge's per-answer records attached to the rows they judged, and the lines of the judge dump
  (r7: this mapping was evaluator code outside the judge's identity).

The status words are literals here, not imports from ``src.grounding``: the judge's hash must bind the definition it
uses, and a candidate that renames a generation status then judges nothing (the sealed protocol's ``judge_clean`` guard
refuses a run that judged no answer) instead of judging by a vocabulary the registered hash did not cover. A test holds
them equal to ``src.grounding``'s.

This module imports nothing from ``src`` at import time (``src.generator`` imports it, and the refusal phrase is read from
``src.generator`` when a refusal is tested), so it can sit below both.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The generation statuses (``src.grounding``), as the judge's eligibility rule reads them.
STATUS_COMPLETE = "complete"
STATUS_TRUNCATED = "truncated"
STATUS_DECLINED = "declined"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"  # evaluator-only: a row whose generation raised
INCOMPLETE_STATUSES = (STATUS_TRUNCATED, STATUS_DECLINED, STATUS_INCOMPLETE)
KNOWN_STATUSES = (STATUS_COMPLETE, *INCOMPLETE_STATUSES, STATUS_UNKNOWN, STATUS_ERROR)


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

    The Phase 5 eval imports this function directly (through ``src.generator``),
    so eval refusal scoring inherits this same strict definition automatically.
    The phrase is ``src.generator.REFUSAL_PHRASE`` (the system prompt is built from it);
    ``src.judge.judge_identity`` records its value.
    """
    from src.generator import REFUSAL_PHRASE

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


def normalise_answer(res: Any) -> Tuple[str, str]:
    """Map one answer result to ``(text, generation_status)`` for every scorer.

    A str (a legacy ``answer_fn``) is text with status ``unknown``; a dict
    carries ``answer`` and an optional ``generation_status`` (absent means
    ``unknown`` — missing metadata is never treated as complete); None (a
    generation that raised) is empty text with status ``error``.

    Fails closed, consistent with ``grounding.generation_outcome``: a present
    but None status is ``unknown``; a string outside the known vocabulary is
    ``incomplete`` (so the row is excluded and counted, and blocks a canonical
    run). A dict with no ``answer`` key raises ``KeyError`` (fail loud, as before
    H): a malformed result must never be scored as an empty answer.
    """
    if res is None:
        return "", STATUS_ERROR
    if isinstance(res, str):
        return res, STATUS_UNKNOWN
    status = res.get("generation_status", STATUS_UNKNOWN)
    if status is None:
        status = STATUS_UNKNOWN
    elif status not in KNOWN_STATUSES:
        status = STATUS_INCOMPLETE
    return res["answer"], status


def is_judgeable(scope: str, result: Optional[Mapping[str, Any]], status: str) -> bool:
    """Whether one row's generated answer is judged: the judge's eligibility rule (one definition, hashed with the judge).

    ``scope`` is the row's scope, ``result`` its generation result (``None`` when generation raised) and ``status`` the
    normalised generation status (:func:`normalise_answer`). A row is judged when it is retrieval-eligible (not
    ``refuse``), its generation produced a result whose status is ``complete`` (and nothing else: not ``truncated``,
    ``declined``, ``incomplete``, ``unknown``, ``error`` or ``not_run``), and the answer is not a refusal.
    """
    if scope == "refuse":
        return False
    if result is None:
        return False  # generation error: nothing to judge
    if status != STATUS_COMPLETE:
        return False  # only a complete answer is judged: a cut-off, declined or unconfirmed draft is not
    if is_refusal(result["answer"]):
        return False  # the judge is conditional on a non-refused answer
    return True


# ---------------------------------------------------------------------------
# The judge's own context formatter (a verbatim copy of ``retriever.format_context`` and its helpers)
# ---------------------------------------------------------------------------


def _locator_label(secnum: str) -> str:
    """A section number as its in-citation locator segment (``para 3.2.1``; an ``APPENDIX`` section verbatim)."""
    return secnum if secnum.startswith("APPENDIX") else f"para {secnum}"


def _handbook_header(metadata: Dict[str, Any]) -> str:
    """Compact citation header for a handbook chunk: ``[Handbook, para X, p.N]``."""
    section = metadata.get("section_number", "")
    page_start = metadata.get("page_start")
    page_end = metadata.get("page_end")

    parts = ["Handbook"]
    if section:
        parts.append(_locator_label(section))
    if page_start is not None:
        if page_end is not None and page_end != page_start:
            parts.append(f"pp.{page_start}–{page_end}")
        else:
            parts.append(f"p.{page_start}")
    return "[" + ", ".join(parts) + "]"


def _generic_header(index: int, metadata: Dict[str, Any]) -> str:
    """Fallback header for non-handbook document types (legislation, case law)."""
    title = metadata.get("title", "Unknown")
    source = metadata.get("source", "Unknown")
    section = metadata.get("section_number", "")

    header = f"[Source {index}: {title}"
    if section:
        header += f", Section {section}"
    header += f" | {source}]"
    return header


def format_judge_context(results: List[Dict[str, Any]]) -> str:
    """The context text the judge reads for one answer: one cited block per retrieved chunk.

    A verbatim copy of ``retriever.format_context`` (generation's context formatter), kept apart on purpose: a candidate may
    change what generation is shown, and the judge must still read the evidence in the form its registered hash covers.
    ``tests`` hold the two outputs equal on fixtures today, so a drift of one from the other is a decision, not an accident.
    """
    if not results:
        return "No relevant documents found."

    context_parts = []
    for i, result in enumerate(results, 1):
        doc = result["document"]
        if doc.metadata.get("document_type") == "handbook":
            header = _handbook_header(doc.metadata)
        else:
            header = _generic_header(i, doc.metadata)
        context_parts.append(f"{header}\n{doc.page_content}\n---")

    return "\n\n".join(context_parts)


def assemble_judge_items(
    rows: Sequence[Mapping[str, Any]], cache: Mapping[str, Mapping[str, Any]]
) -> List[Dict[str, str]]:
    """The items the judge is handed for one set: ``{"question", "answer", "context"}`` per judgeable row, in row order.

    ``cache`` is the evaluator's answer cache (question text -> ``{"result": generation result or None}``); a row is
    included iff :func:`is_judgeable` says so for its scope, its result and the result's normalised status, and its
    context is :func:`format_judge_context` over the result's ``source_documents``.
    """
    items: List[Dict[str, str]] = []
    for row in rows:
        result = (cache.get(row["question"]) or {}).get("result")
        if not is_judgeable(row["scope"], result, normalise_answer(result)[1]):
            continue
        context = format_judge_context(
            [{"document": d, "score": 0.0, "metadata": d.metadata} for d in result.get("source_documents", [])]
        )
        items.append({"question": row["question"], "answer": result["answer"], "context": context})
    return items


def judgments_by_row(
    rows: Sequence[Mapping[str, Any]], per_item: Sequence[Mapping[str, Any]], label: str,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Attach the judge's per-answer records to the rows they judged (r7: it was evaluator code the judge's identity did not cover).

    ``per_item`` is ``judge_answers(...)["per_item"]``: one record per judged item, carrying the item's ``question``. Returns
    ``({row id: record}, dump records)``: the first is what ``eval_v6.judge_summary`` aggregates and the report's per-row
    judgments, the second the lines of the judge dump (``{"set": label, "id": row id, ...}`` plus the record without its
    question text: claim text is the point of the dump and lives only in the run directory or the gitignored review file).
    """
    by_question = {row["question"]: row["id"] for row in rows}
    by_id: Dict[str, Dict[str, Any]] = {}
    dump: List[Dict[str, Any]] = []
    for record in per_item:
        rid = by_question[record["question"]]
        by_id[rid] = record  # (the same object the judge returned: the summary and the dump read it)
        dump.append({"set": label, "id": rid, **{k: v for k, v in record.items() if k != "question"}})
    return by_id, dump
