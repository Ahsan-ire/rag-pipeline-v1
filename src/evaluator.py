"""Phase 5 evaluation harness: retrieval hit@k and refusal accuracy against a
hand-written golden set (see IMPLEMENTATION_PLAN.md Phase 5).

Two metrics, scored independently against ``eval/golden_set.jsonl``:

- Retrieval hit@k (``evaluate_retrieval``): for non-refusal questions, does
  any retrieved chunk's ``section_number`` relate to an expected section?
- Refusal accuracy (``evaluate_refusals``): for refusal-type questions, does
  the generated answer match the canonical refusal (``is_refusal``)?

``run_eval`` ties both together, prints a summary, and writes the same report
to a Markdown file. Per CLAUDE.md's copyright rule, neither the stdout
summary nor the Markdown report ever includes chunk ``page_content`` or full
generated answers — only question text, section numbers, and metrics.
"""

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.generator import _sections_related, generate_with_sources, is_refusal
from src.retriever import retrieve

VALID_TYPES = {"direct", "exact_token", "refusal"}


def load_golden_set(path: str) -> List[Dict[str, Any]]:
    """Load and validate the golden question set from a JSONL file.

    Each non-blank line must be a JSON object with keys ``question`` (a
    non-empty string), ``type`` (one of ``"direct"``, ``"exact_token"``,
    ``"refusal"``), and ``expected_sections`` (a list of section-number
    strings, non-empty for the two in-corpus types and exactly ``[]`` for
    ``"refusal"``, since a refusal question has no answerable section).

    Args:
        path: Path to the golden-set JSONL file.

    Returns:
        A list of normalised ``{"question", "type", "expected_sections"}``
        dicts, one per non-blank line, in file order.

    Raises:
        ValueError: If a line violates the schema above; the message names
            the offending 1-indexed line number.
    """
    golden: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue

            entry = json.loads(line)
            question = entry.get("question")
            entry_type = entry.get("type")
            expected_sections = entry.get("expected_sections")

            if not isinstance(question, str) or not question.strip():
                raise ValueError(
                    f"Line {line_number}: 'question' must be a non-empty string"
                )
            if entry_type not in VALID_TYPES:
                raise ValueError(
                    f"Line {line_number}: 'type' must be one of "
                    f"{sorted(VALID_TYPES)}, got {entry_type!r}"
                )
            if entry_type == "refusal":
                if expected_sections not in (None, []):
                    raise ValueError(
                        f"Line {line_number}: 'expected_sections' must be [] "
                        "for type 'refusal'"
                    )
                normalised_sections: List[str] = []
            else:
                # Non-refusal rows must carry a real list of section numbers.
                # A bare truthiness test used to pass a string ("14.8") or a
                # numeric element (3.10) straight through; those later corrupt
                # the hit@k metric (char-by-char iteration, str(3.10)=='3.1'),
                # so validate the shape here and store stripped strings.
                if not isinstance(expected_sections, list) or not expected_sections:
                    raise ValueError(
                        f"Line {line_number}: 'expected_sections' must be a "
                        f"non-empty list for type {entry_type!r}"
                    )
                if not all(
                    isinstance(section, str) and section.strip()
                    for section in expected_sections
                ):
                    raise ValueError(
                        f"Line {line_number}: 'expected_sections' must contain "
                        f"only non-empty strings for type {entry_type!r}"
                    )
                normalised_sections = [section.strip() for section in expected_sections]

            golden.append(
                {
                    "question": question,
                    "type": entry_type,
                    "expected_sections": normalised_sections,
                }
            )

    return golden


def evaluate_retrieval(
    golden: List[Dict[str, Any]],
    retrieve_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    top_k: int = 6,
) -> Dict[str, Any]:
    """Score retrieval hit@k over the non-refusal questions in ``golden``.

    A question is a HIT if any retrieved result's ``section_number`` metadata
    is related (equal, or dotted-nested either direction — see
    ``src.generator._sections_related``) to any of its expected sections.
    Refusal-type questions have no expected section and are skipped here;
    they are scored separately by ``evaluate_refusals``.

    Args:
        golden: Golden-set entries, as returned by ``load_golden_set``.
        retrieve_fn: Callable ``(question, top_k=...) -> retrieved results``,
            in the shape returned by ``src.retriever.retrieve`` (a list of
            ``{"document", "score", "metadata"}`` dicts). Defaults to
            ``src.retriever.retrieve`` when omitted.
        top_k: Number of chunks to request per question.

    Returns:
        Dict with ``per_question`` (list of per-question hit/miss detail),
        ``hits``, ``total``, ``hit_rate`` (0.0 when ``total`` is 0), and
        ``by_type`` (the same three stats broken out per question ``type``).
    """
    if retrieve_fn is None:
        retrieve_fn = retrieve

    per_question: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, Any]] = {}
    hits = 0
    total = 0

    for entry in golden:
        if entry["type"] == "refusal":
            continue

        question = entry["question"]
        expected_sections = [str(s) for s in entry["expected_sections"]]
        results = retrieve_fn(question, top_k=top_k)
        retrieved_sections = [
            str(r["document"].metadata.get("section_number", "")) for r in results
        ]

        hit = any(
            _sections_related(expected, retrieved)
            for expected in expected_sections
            for retrieved in retrieved_sections
        )

        total += 1
        hits += int(hit)

        type_stats = by_type.setdefault(entry["type"], {"hits": 0, "total": 0})
        type_stats["total"] += 1
        type_stats["hits"] += int(hit)

        per_question.append(
            {
                "question": question,
                "type": entry["type"],
                "expected_sections": expected_sections,
                "retrieved_sections": retrieved_sections,
                "hit": hit,
            }
        )

    for stats in by_type.values():
        stats["hit_rate"] = stats["hits"] / stats["total"] if stats["total"] else 0.0

    return {
        "per_question": per_question,
        "hits": hits,
        "total": total,
        "hit_rate": hits / total if total else 0.0,
        "by_type": by_type,
    }


def evaluate_refusals(
    golden: List[Dict[str, Any]],
    answer_fn: Optional[Callable[[str], str]] = None,
    top_k: int = 6,
) -> Dict[str, Any]:
    """Score refusal accuracy over the refusal-type questions in ``golden``.

    Args:
        golden: Golden-set entries, as returned by ``load_golden_set``.
        answer_fn: Callable ``(question) -> answer string``. Defaults to
            retrieving ``top_k`` chunks with ``src.retriever.retrieve`` and
            generating with ``src.generator.generate_with_sources`` — this
            default makes live Claude API calls, so tests must inject a fake.
        top_k: Number of chunks the default ``answer_fn`` retrieves.

    Returns:
        Dict with ``per_question`` (list of ``{"question", "refused"}``),
        ``refused``, ``total``, and ``accuracy`` (0.0 when ``total`` is 0).
        The raw answer is deliberately not carried out of this function: it
        can echo copyrighted corpus prose, so only the refusal flag escapes.
    """
    if answer_fn is None:

        def answer_fn(question: str) -> str:
            results = retrieve(question, top_k=top_k)
            return generate_with_sources(question, results)["answer"]

    per_question: List[Dict[str, Any]] = []
    refused = 0
    total = 0

    for entry in golden:
        if entry["type"] != "refusal":
            continue

        question = entry["question"]
        answer = answer_fn(question)
        refused_flag = is_refusal(answer)

        total += 1
        refused += int(refused_flag)

        per_question.append({"question": question, "refused": refused_flag})

    return {
        "per_question": per_question,
        "refused": refused,
        "total": total,
        "accuracy": refused / total if total else 0.0,
    }


def _format_report(
    golden_path: str,
    top_k: int,
    retrieval: Dict[str, Any],
    refusals: Optional[Dict[str, Any]],
) -> str:
    """Render the retrieval + refusal results as a Markdown report.

    Copyright rule (CLAUDE.md): only question text, section numbers, and
    metrics appear here — never chunk ``page_content`` or full generated
    answer text (a refusal-type answer could itself echo corpus phrasing).
    """
    lines: List[str] = []
    lines.append("# Legal RAG Evaluation Report")
    lines.append("")
    lines.append(f"- Date: {datetime.now().isoformat()}")
    lines.append(f"- Golden set: {golden_path}")
    lines.append(f"- top_k: {top_k}")
    lines.append("")

    lines.append(f"## Retrieval (hit@{top_k})")
    lines.append("")
    lines.append(
        f"Overall hit rate: {retrieval['hits']}/{retrieval['total']} = "
        f"{retrieval['hit_rate']:.3f}"
    )
    lines.append("")
    if retrieval["by_type"]:
        lines.append("| Type | Hits | Total | Hit rate |")
        lines.append("| --- | --- | --- | --- |")
        for q_type, stats in sorted(retrieval["by_type"].items()):
            lines.append(
                f"| {q_type} | {stats['hits']} | {stats['total']} | "
                f"{stats['hit_rate']:.3f} |"
            )
        lines.append("")

    lines.append("## Refusals")
    lines.append("")
    if refusals is None:
        lines.append("Refusal accuracy: skipped")
    else:
        lines.append(
            f"Refusal accuracy: {refusals['refused']}/{refusals['total']} = "
            f"{refusals['accuracy']:.3f}"
        )
    lines.append("")

    lines.append("## Per-question detail")
    lines.append("")
    for q in retrieval["per_question"]:
        status = "HIT" if q["hit"] else "MISS"
        lines.append(
            f"- [{q['type']}] {status} expected={q['expected_sections']} "
            f"retrieved={q['retrieved_sections']} :: {q['question']}"
        )
    if refusals is not None:
        for q in refusals["per_question"]:
            status = "refused" if q["refused"] else "answered"
            lines.append(f"- [refusal] {status} :: {q['question']}")

    return "\n".join(lines) + "\n"


def run_eval(
    golden_path: str,
    top_k: int = 6,
    skip_refusals: bool = False,
    results_path: str = "eval/results.md",
    retrieve_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    answer_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Run the full Phase 5 evaluation and report the results.

    Loads the golden set, always scores retrieval hit@k, and (unless
    ``skip_refusals``) scores refusal accuracy — the latter makes live Claude
    API calls through the default ``answer_fn``. Prints a summary to stdout
    and writes the same report as Markdown to ``results_path``.

    Args:
        golden_path: Path to the golden-set JSONL file.
        top_k: Number of chunks to retrieve per question.
        skip_refusals: If True, skip the (API-calling) refusal pass.
        results_path: Where to write the Markdown report; parent directory
            is created if missing.
        retrieve_fn: Optional override for retrieval (see
            ``evaluate_retrieval``); mainly for tests.
        answer_fn: Optional override for answer generation (see
            ``evaluate_refusals``); mainly for tests.

    Returns:
        Dict with ``retrieval`` (evaluate_retrieval's return value),
        ``refusals`` (evaluate_refusals's return value, or None if skipped),
        ``golden_path``, and ``top_k``.
    """
    golden = load_golden_set(golden_path)

    retrieval = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=top_k)
    refusals = (
        None
        if skip_refusals
        else evaluate_refusals(golden, answer_fn=answer_fn, top_k=top_k)
    )

    report = _format_report(golden_path, top_k, retrieval, refusals)
    print(report)

    parent_dir = os.path.dirname(results_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        f.write(report)

    return {
        "retrieval": retrieval,
        "refusals": refusals,
        "golden_path": golden_path,
        "top_k": top_k,
    }
