"""Phase 5/6 evaluation harness: retrieval hit@k and refusal accuracy against a
hand-written golden set (see IMPLEMENTATION_PLAN.md Phase 5).

Metrics, scored against ``eval/golden_set.jsonl``:

- Retrieval hit@k (``evaluate_retrieval``): for non-refusal questions, scored
  two ways side by side — ``strict`` (exact ``section_number`` equality with an
  expected section) and ``related`` (dotted-nesting, so a retrieved child like
  ``6.3.2.2`` counts for expected ``6.3.2``; see ``_sections_related``).
- Refusal accuracy (``evaluate_refusals``): for refusal-type questions, does
  the generated answer match the canonical refusal (``is_refusal``)?

``run_eval`` ties both together, prints a summary, and writes the same report
to a Markdown file. The report opens with a ``## Provenance`` block
(``collect_provenance``): git sha/dirty flag, indexed chunk count, embedding and
generation models, and the strict-vs-related matching definition — so a report
is self-describing about the exact code and index that produced it.

Per CLAUDE.md's copyright rule (D30), neither the stdout summary nor the
Markdown report ever includes chunk ``page_content`` or full generated
answers — only question text, section numbers, metrics, and provenance.
"""

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.embedder import CHROMA_PERSIST_DIR, EMBEDDING_MODEL, get_vector_store
from src.generator import (
    GENERATION_MODEL,
    _sections_related,
    generate_with_sources,
    is_refusal,
)
from src.retriever import retrieve

VALID_TYPES = {"direct", "exact_token", "refusal"}

# Short, fixed definition of the two retrieval-matching modes, surfaced both in
# the provenance block and (spelled out) in the retrieval report header so a
# reader never has to guess what "strict" vs "related" mean.
MATCHING_DEFINITION = (
    "strict = exact section-number equality; "
    "related = dotted-nesting either direction (a retrieved parent OR child "
    "of an expected section also counts, e.g. expected 6.3.2 matches "
    "retrieved 6.3.2.2 or 6.3)"
)


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

    Each non-refusal question is scored two independent ways against the
    retrieved chunks' ``section_number`` metadata:

    - ``hit_strict``: True iff some expected section equals some retrieved
      section exactly (both ``.strip()``ed). This is the harsh metric — a
      retrieved parent or child that merely *nests* the expected one does not
      count.
    - ``hit_related``: True iff some expected section is equal-or-dotted-nested
      to some retrieved section (see ``src.generator._sections_related``), so a
      retrieved child like ``14.12.1`` counts for expected ``14.12``.

    Refusal-type questions have no expected section and are skipped here; they
    are scored separately by ``evaluate_refusals``.

    Args:
        golden: Golden-set entries, as returned by ``load_golden_set``.
        retrieve_fn: Callable ``(question, top_k=...) -> retrieved results``,
            in the shape returned by ``src.retriever.retrieve`` (a list of
            ``{"document", "score", "metadata"}`` dicts). Defaults to
            ``src.retriever.retrieve`` when omitted.
        top_k: Number of chunks to request per question.

    Returns:
        Dict with ``per_question`` (each carrying ``hit_strict`` and
        ``hit_related`` flags), ``hits_strict``/``hit_rate_strict``,
        ``hits_related``/``hit_rate_related``, ``total`` (rates are 0.0 when
        ``total`` is 0), and ``by_type`` (both hit counts and rates plus the
        total, broken out per question ``type``).
    """
    if retrieve_fn is None:
        retrieve_fn = retrieve

    per_question: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, Any]] = {}
    hits_strict = 0
    hits_related = 0
    total = 0

    for entry in golden:
        if entry["type"] == "refusal":
            continue

        question = entry["question"]
        expected_sections = [str(s).strip() for s in entry["expected_sections"]]
        results = retrieve_fn(question, top_k=top_k)
        retrieved_sections = [
            str(r["document"].metadata.get("section_number", "")).strip()
            for r in results
        ]

        # strict: literal string equality (skip empty retrieved sections so a
        # missing section_number can never equality-match an expected one).
        hit_strict = any(
            expected == retrieved
            for expected in expected_sections
            for retrieved in retrieved_sections
            if retrieved
        )
        # related: the pre-existing dotted-nesting rule, unchanged.
        hit_related = any(
            _sections_related(expected, retrieved)
            for expected in expected_sections
            for retrieved in retrieved_sections
        )

        total += 1
        hits_strict += int(hit_strict)
        hits_related += int(hit_related)

        type_stats = by_type.setdefault(
            entry["type"], {"hits_strict": 0, "hits_related": 0, "total": 0}
        )
        type_stats["total"] += 1
        type_stats["hits_strict"] += int(hit_strict)
        type_stats["hits_related"] += int(hit_related)

        per_question.append(
            {
                "question": question,
                "type": entry["type"],
                "expected_sections": expected_sections,
                "retrieved_sections": retrieved_sections,
                "hit_strict": hit_strict,
                "hit_related": hit_related,
            }
        )

    for stats in by_type.values():
        denom = stats["total"]
        stats["hit_rate_strict"] = stats["hits_strict"] / denom if denom else 0.0
        stats["hit_rate_related"] = stats["hits_related"] / denom if denom else 0.0

    return {
        "per_question": per_question,
        "hits_strict": hits_strict,
        "hits_related": hits_related,
        "total": total,
        "hit_rate_strict": hits_strict / total if total else 0.0,
        "hit_rate_related": hits_related / total if total else 0.0,
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


def _porcelain_dirty_paths(porcelain_output: str) -> List[str]:
    """Parse ``git status --porcelain`` stdout into a list of dirty paths.

    Porcelain (v1) format is two status-code characters, one separator space,
    then the path: ``"XY path/to/file"``. For a rename or copy, the path part
    is instead ``"old/path -> new/path"``; the file's current location is the
    right-hand side, so that is what gets returned for those lines.

    Args:
        porcelain_output: Raw stdout from ``git status --porcelain``.

    Returns:
        A list of paths (repo-root-relative, as git reports them), one per
        non-blank line, in file order.
    """
    paths: List[str] = []
    for line in porcelain_output.splitlines():
        if not line:
            continue
        path_part = line[3:]  # past the 2-char status code + 1 separator space
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        paths.append(path_part)
    return paths


def collect_provenance(
    persist_directory: str = CHROMA_PERSIST_DIR,
    exclude_paths: tuple = (),
) -> Dict[str, Any]:
    """Gather the code + index + model facts that produced an eval run.

    Every field degrades to the string ``"unavailable"`` rather than raising,
    so a report can still be produced from a checkout with no git, or before
    any corpus has been indexed. Concretely:

    - ``git_sha``: short commit hash (``git rev-parse --short HEAD``).
    - ``git_dirty``: True if ``git status --porcelain`` reports any change,
      else False (or ``"unavailable"`` if git is not usable here).
    - ``git_dirty_other``: count of dirty paths whose ``os.path.normpath`` is
      NOT among the normpath'd ``exclude_paths`` (or ``"unavailable"`` on the
      same subprocess failure that takes down ``git_dirty``). This exists so
      a report can say "the only dirty file is the report you're reading"
      instead of a bare, unhelpful "dirty" — see ``exclude_paths`` below.
    - ``chunk_count``: number of vectors currently in the Chroma store; asks
      for ``include=[]`` so only the ``ids`` come back (no documents/embeddings
      loaded just to be counted).
    - ``embedding_model`` / ``generation_model``: the configured model strings,
      imported from their owning modules so this block cannot drift from them.
    - ``matching``: the fixed strict-vs-related definition (``MATCHING_DEFINITION``).

    Args:
        persist_directory: Chroma persistence directory to count chunks in.
        exclude_paths: Paths to exclude when counting ``git_dirty_other`` —
            typically the eval report file about to be (re)written, which is
            expected to show up dirty and shouldn't count as a surprise.
            Matching is by ``os.path.normpath`` equality. Caveat: porcelain
            paths from git are always repo-root-relative (relative to the
            repo containing this file), while ``exclude_paths`` is whatever
            the caller passed — typically relative to the caller's own
            process cwd. If the caller's process runs from outside the repo
            root, a path here may fail to match the porcelain path even
            though they name the same file. The failure mode is harmlessly
            conservative: the unmatched file is counted as one "other" dirty
            file, i.e. the report says "dirty: 1 file(s) beyond this report"
            instead of the fully-clean phrasing — never a false "clean".

    Returns:
        A dict with the keys described above; string values are plain strings
        and any unobtainable field is the literal ``"unavailable"``.
    """
    # Anchor git to THIS repo (the directory holding evaluator.py), not the
    # caller's process cwd — otherwise provenance would record whatever repo
    # `python -m src.pipeline eval` happened to be launched from.
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_dir,
        ).stdout.strip()
    except Exception:
        git_sha = "unavailable"

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_dir,
        ).stdout
        dirty_paths = _porcelain_dirty_paths(status)
        git_dirty: Any = bool(dirty_paths)
        excluded = {os.path.normpath(p) for p in exclude_paths}
        git_dirty_other: Any = sum(
            1 for p in dirty_paths if os.path.normpath(p) not in excluded
        )
    except Exception:
        git_dirty = "unavailable"
        git_dirty_other = "unavailable"

    try:
        store = get_vector_store(persist_directory=persist_directory)
        chunk_count: Any = len(store.get(include=[])["ids"])
    except Exception:
        chunk_count = "unavailable"

    return {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "git_dirty_other": git_dirty_other,
        "chunk_count": chunk_count,
        "embedding_model": EMBEDDING_MODEL,
        "generation_model": GENERATION_MODEL,
        "matching": MATCHING_DEFINITION,
    }


def _format_report(
    golden_path: str,
    top_k: int,
    retrieval: Dict[str, Any],
    refusals: Optional[Dict[str, Any]],
    provenance: Dict[str, Any],
    golden: List[Dict[str, Any]],
) -> str:
    """Render the retrieval + refusal results as a Markdown report.

    Copyright rule (CLAUDE.md D30): only question text, section numbers,
    metrics, and provenance appear here — never chunk ``page_content`` or full
    generated answer text (a refusal-type answer could itself echo corpus
    phrasing).

    Args:
        golden_path: Path the golden set was loaded from.
        top_k: Chunks retrieved per question.
        retrieval: ``evaluate_retrieval``'s dual-metric return value.
        refusals: ``evaluate_refusals``'s return value, or None if skipped.
        provenance: ``collect_provenance``'s return value (git/index/models).
        golden: The loaded golden set, used only to report per-type question
            counts (not any answer or chunk text).
    """
    # Per-type counts of the loaded golden set (e.g. direct=8, exact_token=5,
    # refusal=5), so the report states the shape of the set it scored.
    type_counts: Dict[str, int] = {}
    for entry in golden:
        type_counts[entry["type"]] = type_counts.get(entry["type"], 0) + 1
    counts_str = ", ".join(f"{t}={type_counts[t]}" for t in sorted(type_counts))

    # git_dirty is a bool on success but the string "unavailable" on failure.
    # When dirty, git_dirty_other (also bool-guarded against "unavailable")
    # disambiguates "only the report we're about to overwrite is dirty" from
    # "something else changed too" — see collect_provenance's exclude_paths.
    dirty = provenance.get("git_dirty")
    dirty_other = provenance.get("git_dirty_other")
    if not isinstance(dirty, bool):
        # git_dirty itself is "unavailable" (or some other non-bool) — degrade
        # gracefully: render whatever string we have, never raise.
        dirty_str = str(dirty)
    elif not dirty:
        dirty_str = "clean"
    elif isinstance(dirty_other, int) and dirty_other == 0:
        dirty_str = "clean apart from this generated report"
    elif isinstance(dirty_other, int):
        dirty_str = f"dirty: {dirty_other} file(s) beyond this report"
    else:
        # dirty is True but git_dirty_other didn't come back as an int (e.g.
        # "unavailable") — fall back to the old plain "dirty", never raise.
        dirty_str = "dirty"

    lines: List[str] = []
    lines.append("# Legal RAG Evaluation Report")
    lines.append("")
    lines.append(f"- Date: {datetime.now().isoformat()}")
    lines.append(f"- Golden set: {golden_path}")
    lines.append(f"- top_k: {top_k}")
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- git sha: {provenance.get('git_sha')} ({dirty_str})")
    lines.append(f"- indexed chunk count: {provenance.get('chunk_count')}")
    lines.append(f"- embedding model: {provenance.get('embedding_model')}")
    lines.append(f"- generation model: {provenance.get('generation_model')}")
    lines.append(f"- matching: {provenance.get('matching')}")
    lines.append(f"- golden set question counts: {counts_str}")
    lines.append(
        f"- refusals: {'skipped' if refusals is None else 'scored'}"
    )
    lines.append("")

    lines.append(f"## Retrieval (hit@{top_k})")
    lines.append("")
    lines.append(
        "Two metrics: strict = exact section-number equality; related = "
        "dotted-nesting either direction (a retrieved parent OR child of an "
        "expected section counts, e.g. expected 6.3.2 matches retrieved "
        "6.3.2.2 or 6.3)."
    )
    lines.append("")
    # Strict is the headline (harsher, no nesting credit); related second.
    lines.append(
        f"Strict hit rate: {retrieval['hits_strict']}/{retrieval['total']} = "
        f"{retrieval['hit_rate_strict']:.3f}"
    )
    lines.append(
        f"Related hit rate: {retrieval['hits_related']}/{retrieval['total']} = "
        f"{retrieval['hit_rate_related']:.3f}"
    )
    lines.append("")
    lines.append(
        f"Question set: n={retrieval['total']} tuning set — used to select "
        "fusion constants (D31); NOT held-out."
    )
    lines.append("")
    if retrieval["by_type"]:
        lines.append(
            "| Type | Strict hits | Strict rate | Related hits | "
            "Related rate | Total |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for q_type, stats in sorted(retrieval["by_type"].items()):
            lines.append(
                f"| {q_type} | {stats['hits_strict']} | "
                f"{stats['hit_rate_strict']:.3f} | {stats['hits_related']} | "
                f"{stats['hit_rate_related']:.3f} | {stats['total']} |"
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
        strict = "HIT" if q["hit_strict"] else "MISS"
        related = "HIT" if q["hit_related"] else "MISS"
        lines.append(
            f"- [{q['type']}] strict={strict} related={related} "
            f"expected={q['expected_sections']} "
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
    provenance_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the full Phase 5/6 evaluation and report the results.

    Loads the golden set, always scores retrieval hit@k (strict and related),
    and (unless ``skip_refusals``) scores refusal accuracy — the latter makes
    live Claude API calls through the default ``answer_fn``. Prints a summary
    to stdout and writes the same report as Markdown to ``results_path``.

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
        provenance_fn: Optional override for the provenance block; called with
            no arguments to return the ``collect_provenance`` shape. Defaults
            to ``collect_provenance`` with ``results_path`` passed as its
            ``exclude_paths`` (the report about to be written is expected to
            be dirty and shouldn't count as a surprise) — this default shells
            out to git and opens the Chroma store, so tests MUST inject a
            fake to stay IO-free.

    Returns:
        Dict with ``retrieval`` (evaluate_retrieval's return value),
        ``refusals`` (evaluate_refusals's return value, or None if skipped),
        ``provenance`` (the provenance dict), ``golden_path``, and ``top_k``.
    """
    golden = load_golden_set(golden_path)

    retrieval = evaluate_retrieval(golden, retrieve_fn=retrieve_fn, top_k=top_k)
    refusals = (
        None
        if skip_refusals
        else evaluate_refusals(golden, answer_fn=answer_fn, top_k=top_k)
    )

    if provenance_fn is None:
        provenance_fn = lambda: collect_provenance(exclude_paths=(results_path,))
    provenance = provenance_fn()

    report = _format_report(
        golden_path, top_k, retrieval, refusals, provenance, golden
    )
    print(report)

    parent_dir = os.path.dirname(results_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        f.write(report)

    return {
        "retrieval": retrieval,
        "refusals": refusals,
        "provenance": provenance,
        "golden_path": golden_path,
        "top_k": top_k,
    }
