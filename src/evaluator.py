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

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.bm25_index import load_bm25_index
from src.embedder import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_MODEL,
    assert_embedding_model,
    get_vector_store,
)
from src.generator import (
    CITATION_RE,
    GENERATION_MODEL,
    _sections_related,
    generate_with_sources,
    is_refusal,
)
from src.grounding import (
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    REFUSAL,
)
from src.retriever import (
    RETRIEVAL_MODES,
    format_context,
    load_retrieval_context,
    retrieve,
)

# Canonical vs. partial report destinations (Phase 10 results-path guard, D38).
# ``eval/results.md`` is committed and is ONLY written by a fully canonical run
# (held-out set present, all three modes, refusals + completeness scored,
# top_k==6, zero generation errors). Anything less writes the gitignored
# ``eval/results_partial.md`` so a partial/exploratory run can never silently
# overwrite the trustworthy committed report.
DEFAULT_RESULTS_PATH = "eval/results.md"
PARTIAL_RESULTS_PATH = "eval/results_partial.md"

# The four grounding-gate outcomes, in report order, so the gate-outcome
# distribution is always zero-filled over the full vocabulary (a missing
# outcome reads as "0", never as absent).
GATE_OUTCOMES = (
    REFUSAL,
    CITATIONS_VERIFIED,
    PARTIALLY_VERIFIED,
    CITATIONS_UNVERIFIED,
)

VALID_TYPES = {"direct", "exact_token", "refusal"}
# The answerable (in-corpus) types — the ones the generator is expected to
# answer and cite. Used to derive the generation ``include_types`` and to pick
# which questions the completeness + judge passes score.
IN_CORPUS_TYPES = ("direct", "exact_token")
# A set whose label contains this token is treated as the frozen held-out set
# for the canonical-run check and the report headline (D38 / D33).
HELDOUT_LABEL_TOKEN = "held-out"

# The retrieval cut-offs reported by ``evaluate_retrieval``: hit@1, hit@3, hit@6.
# Only the ks with ``k <= top_k`` are actually reported (a hit@6 cell is
# meaningless when only 3 chunks were retrieved), so the effective ks are
# derived per run — see ``evaluate_retrieval``. The headline metric is strict
# hit@6 on the held-out set (D38).
HIT_KS = (1, 3, 6)

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


def _build_default_retrieve_fn(
    top_k: int, persist_directory: str
) -> Callable[..., List[Dict[str, Any]]]:
    """Build a load-once ``retrieve_fn`` (Phase 9): one store + one BM25 index,
    reused across every question instead of rebuilt per call.

    ``src.retriever.retrieve`` re-opens the Chroma wrapper, re-unpickles the
    BM25 index, and re-checks the embedding-model manifest on every single
    call. Over a golden set of dozens of questions that per-question cost
    dominates; :func:`src.retriever.load_retrieval_context` builds each of
    those three things exactly ONCE and this closes over the result, so only
    the query itself varies per call.

    Args:
        top_k: Default ``top_k`` baked into the returned callable (still
            overridable per call via its own ``top_k`` keyword argument).
        persist_directory: Where to open the vector store and load the BM25
            index from.

    Returns:
        A callable ``(question, top_k=top_k) -> retrieved results`` — the
        same shape ``src.retriever.retrieve`` returns — that delegates to
        ``retrieve`` with the pre-built store/index injected, so ``retrieve``
        itself skips its own disk load and manifest check for this call.
    """
    store, bm25 = load_retrieval_context(persist_directory)

    def _retrieve_fn(question: str, top_k: int = top_k) -> List[Dict[str, Any]]:
        """Retrieve ``question`` using the store/BM25 index built once above."""
        return retrieve(
            question,
            top_k=top_k,
            persist_directory=persist_directory,
            vector_store=store,
            bm25_index=bm25,
        )

    return _retrieve_fn


def _wilson_ci(hits: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95%-by-default Wilson score interval for a binomial proportion.

    The Wilson interval (not the naive ``p ± z·sqrt(p(1-p)/n)`` normal
    approximation) is used because the eval's headline rates are measured on
    small, curated sets — the held-out set has only ~20 in-corpus questions —
    where the normal approximation misbehaves near 0 and 1 (it can even fall
    outside ``[0, 1]``). Wilson stays inside ``[0, 1]`` and is well-behaved at
    the extremes, so the report can honestly show "0.80 (95% CI 0.58–0.92,
    n=20)" instead of a point estimate that reads as more precise than 20
    questions can support.

    Args:
        hits: Number of successes (``0 <= hits <= n``).
        n: Number of trials.
        z: Standard-normal quantile for the desired two-sided level (1.96 ≈
            95%).

    Returns:
        ``(low, high)`` bounds, each clamped to ``[0.0, 1.0]``. ``n == 0``
        returns ``(0.0, 0.0)`` — no data, no interval — rather than dividing
        by zero.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    low = center - half
    high = center + half
    return (max(0.0, low), min(1.0, high))


def evaluate_retrieval(
    golden: List[Dict[str, Any]],
    retrieve_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    top_k: int = 6,
    persist_directory: str = CHROMA_PERSIST_DIR,
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
            ``{"document", "score", "metadata"}`` dicts). Defaults to None, in
            which case the vector store and BM25 index are built ONCE here
            (Phase 9 load-once retrieval) and reused across every question via
            ``_build_default_retrieve_fn``, instead of ``src.retriever.retrieve``
            re-opening the Chroma store and re-unpickling the BM25 index on
            every single call.
        top_k: Number of chunks to request per question.
        persist_directory: ChromaDB persistence directory, used only to build
            the default ``retrieve_fn`` (ignored when ``retrieve_fn`` is
            given explicitly).

    Returns:
        Dict with ``per_question`` (each carrying ``hit_strict``/``hit_related``
        flags and their 1-indexed ``first_strict_rank``/``first_related_rank``,
        ``None`` when unmatched), ``hits_strict``/``hit_rate_strict``,
        ``hits_related``/``hit_rate_related``, ``total`` (rates are 0.0 when
        ``total`` is 0), and ``by_type`` (both hit counts and rates plus the
        total, broken out per question ``type``).

        Phase 10 additive keys (D38): ``top_k``; ``ks`` (the reported cut-offs,
        ``[k for k in HIT_KS if k <= top_k]``); ``hit_at_k`` and
        ``hit_rate_at_k`` (each ``{"strict": {k: ...}, "related": {k: ...}}``
        over ``ks``); and ``mrr_strict``/``mrr_related`` (mean reciprocal rank
        truncated at ``top_k`` — a question with no match in the top_k scores 0).
        All existing keys and per-question keys are unchanged.
    """
    if retrieve_fn is None:
        retrieve_fn = _build_default_retrieve_fn(top_k, persist_directory)

    # Only report cut-offs we actually retrieved deep enough to measure: a
    # hit@6 cell is incoherent when top_k==3. ks preserves HIT_KS order.
    ks = [k for k in HIT_KS if k <= top_k]

    per_question: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, Any]] = {}
    hits_strict = 0
    hits_related = 0
    total = 0
    # Truncated (MRR@top_k) reciprocal-rank sums: a question with no match in
    # the top_k retrieved chunks contributes 0, with the cut-off disclosed in
    # the report so this is never mistaken for an untruncated MRR.
    mrr_strict_sum = 0.0
    mrr_related_sum = 0.0
    # hit@k counters, one running count per k, for strict and related.
    strict_at_k = {k: 0 for k in ks}
    related_at_k = {k: 0 for k in ks}

    for entry in golden:
        if entry["type"] == "refusal":
            continue

        question = entry["question"]
        expected_sections = [str(s).strip() for s in entry["expected_sections"]]
        # Clamp to top_k so a hit@k / MRR@k figure can never count a match that
        # sits below the declared cutoff — production retrieve() already returns
        # <= top_k, but an injected/custom retrieve_fn might over-return.
        results = retrieve_fn(question, top_k=top_k)[:top_k]
        retrieved_sections = [
            str(r["document"].metadata.get("section_number", "")).strip()
            for r in results
        ]

        # Walk the ranked list once and record the 1-indexed rank of the FIRST
        # strict match and the FIRST related match (None if never matched).
        # Everything downstream — hit_strict/hit_related, hit@k, MRR — derives
        # from these two ranks, so hit@k is free (no re-retrieval at smaller k)
        # and hit_strict stays exactly the old any()-based flag.
        first_strict_rank: Optional[int] = None
        first_related_rank: Optional[int] = None
        for rank, retrieved in enumerate(retrieved_sections, start=1):
            # strict: literal string equality (skip empty retrieved sections so
            # a missing section_number can never equality-match an expected one).
            if (
                first_strict_rank is None
                and retrieved
                and any(expected == retrieved for expected in expected_sections)
            ):
                first_strict_rank = rank
            # related: the pre-existing dotted-nesting rule, unchanged.
            if first_related_rank is None and any(
                _sections_related(expected, retrieved)
                for expected in expected_sections
            ):
                first_related_rank = rank
            if first_strict_rank is not None and first_related_rank is not None:
                break

        hit_strict = first_strict_rank is not None
        hit_related = first_related_rank is not None

        total += 1
        hits_strict += int(hit_strict)
        hits_related += int(hit_related)
        if first_strict_rank is not None:
            mrr_strict_sum += 1.0 / first_strict_rank
        if first_related_rank is not None:
            mrr_related_sum += 1.0 / first_related_rank
        for k in ks:
            if first_strict_rank is not None and first_strict_rank <= k:
                strict_at_k[k] += 1
            if first_related_rank is not None and first_related_rank <= k:
                related_at_k[k] += 1

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
                "first_strict_rank": first_strict_rank,
                "first_related_rank": first_related_rank,
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
        # Phase 10 additive metrics (D38). ``ks`` are the reported cut-offs
        # (k <= top_k); ``hit_at_k``/``hit_rate_at_k`` are keyed by those ks for
        # both strict and related; MRR is truncated at top_k (misses score 0).
        "top_k": top_k,
        "ks": ks,
        "hit_at_k": {"strict": dict(strict_at_k), "related": dict(related_at_k)},
        "hit_rate_at_k": {
            "strict": {k: (strict_at_k[k] / total if total else 0.0) for k in ks},
            "related": {k: (related_at_k[k] / total if total else 0.0) for k in ks},
        },
        "mrr_strict": mrr_strict_sum / total if total else 0.0,
        "mrr_related": mrr_related_sum / total if total else 0.0,
    }


def evaluate_refusals(
    golden: List[Dict[str, Any]],
    answer_fn: Optional[Callable[[str], str]] = None,
    top_k: int = 6,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Dict[str, Any]:
    """Score refusal accuracy over the refusal-type questions in ``golden``.

    Args:
        golden: Golden-set entries, as returned by ``load_golden_set``.
        answer_fn: Callable ``(question) -> answer string``. Defaults to None,
            in which case the default retrieves ``top_k`` chunks via a
            load-once ``retrieve_fn`` (``_build_default_retrieve_fn`` — the
            same Phase 9 store/BM25-index-built-once pattern used by
            ``evaluate_retrieval``) and generates with
            ``src.generator.generate_with_sources`` — this default makes live
            Claude API calls, so tests must inject a fake.
        top_k: Number of chunks the default ``answer_fn`` retrieves.
        persist_directory: ChromaDB persistence directory, used only to build
            the default ``answer_fn`` (ignored when ``answer_fn`` is given
            explicitly).

    Returns:
        Dict with ``per_question`` (list of ``{"question", "refused"}``),
        ``refused``, ``total``, and ``accuracy`` (0.0 when ``total`` is 0).
        The raw answer is deliberately not carried out of this function: it
        can echo copyrighted corpus prose, so only the refusal flag escapes.
    """
    if answer_fn is None:
        default_retrieve_fn = _build_default_retrieve_fn(top_k, persist_directory)

        def answer_fn(question: str) -> str:
            """Retrieve via the once-built store/BM25 index, then generate."""
            results = default_retrieve_fn(question)
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


# Prose abbreviations whose trailing period must NOT be read as a sentence end.
# Ordered longest-first so a shorter member ("p.") can never pre-empt a longer
# one ("pp.", "paras.") during protection. Deliberately small and legal-prose
# focused (the handbook's own citation style: paragraphs, sections, pages).
_SENTENCE_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "etc.",
    "cf.",
    "viz.",
    "approx.",
    "vs.",
    "paras.",
    "para.",
    "pp.",
    "p.",
    "ss.",
    "s.",
    "no.",
    "art.",
    "ch.",
    "sec.",
)

# A whole bracketed span — a citation locator like ``[Handbook, para 14.8.5,
# p.412]`` — masked as one opaque token before splitting so the periods inside
# it (``p.412``, ``14.8.5``) can never be read as sentence boundaries.
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
# The mask token: two NUL bytes around the index. Contains no ``. ! ?`` or
# whitespace, so it always survives sentence splitting as a single unit and
# can never itself look like a sentence boundary.
_MASK_RE = re.compile("\x00(\\d+)\x00")
# Sentence boundary: a ``.?!`` immediately before whitespace that is followed by
# a capital, an opening quote, or a masked citation (a sentence may open with a
# quotation or, rarely, a citation). Heuristic — see ``split_sentences``.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“\x00])")


def split_sentences(text: str) -> List[str]:
    """Split an answer into sentences for the completeness metric (heuristic).

    This gates nothing — it only counts sentences and locates where citations
    fall, feeding the *syntactic* sentence-citation-coverage figure (D38). It
    is deliberately simple and its limitations are documented, not hidden:

    1. Bracketed citation spans are masked to an opaque token first, so the
       periods inside ``[Handbook, para 14.8.5, p.412]`` cannot be mistaken for
       sentence ends.
    2. A small set of prose abbreviations (``p. pp. para. paras. s. ss. no.
       art. ch. sec. e.g. i.e. etc. cf. viz. approx. vs.``) have their periods
       protected so ``see para. 3`` or ``e.g. a lease`` do not split there.
    3. The text is split on newlines first (so bullet / numbered lists split),
       then within each line on ``.?!`` + whitespace + a capital/quote/citation.
    4. Masks and protected periods are restored, so each returned sentence
       carries its original citation brackets verbatim (needed for the
       downstream ``CITATION_RE`` check).

    Known limitations (accepted — this is a coarse coverage proxy): a sentence
    that genuinely ends in a listed abbreviation (e.g. an answer ending "...the
    answer is no.") will not split after it; a sentence whose terminal period
    sits inside a closing quote (``'... yes.' The next...``) will not split; and
    lower-case sentence starts are not detected. None of these can cause a
    false refusal or block — the metric is descriptive only.

    Args:
        text: The answer text (may be empty, whitespace, or multi-line).

    Returns:
        A list of non-empty, stripped sentence strings in order; ``[]`` for
        empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []

    # 1. Mask bracketed citation spans to opaque tokens.
    masked_citations: List[str] = []

    def _mask(match: "re.Match[str]") -> str:
        masked_citations.append(match.group(0))
        return f"\x00{len(masked_citations) - 1}\x00"

    masked = _BRACKET_RE.sub(_mask, text)

    # 2. Protect abbreviation periods (longest-first; case-insensitive but the
    #    matched casing is preserved — only the periods become the sentinel).
    for abbr in _SENTENCE_ABBREVIATIONS:
        pattern = r"\b" + re.escape(abbr)
        masked = re.sub(
            pattern,
            lambda m: m.group(0).replace(".", "\x01"),
            masked,
            flags=re.IGNORECASE,
        )

    # 3. Newline split first (lists), then sentence split within each line.
    sentences: List[str] = []
    for line in masked.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT_RE.split(line):
            part = part.strip()
            if part:
                sentences.append(part)

    # 4. Unmask: restore protected periods, then the citation brackets.
    restored: List[str] = []
    for sentence in sentences:
        sentence = sentence.replace("\x01", ".")
        sentence = _MASK_RE.sub(
            lambda m: masked_citations[int(m.group(1))], sentence
        )
        restored.append(sentence)
    return restored


def evaluate_completeness(
    golden: List[Dict[str, Any]],
    answers: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Score answer quality on the ANSWERABLE (in-corpus) questions (D38).

    This measures the pipeline in the *other* direction from ``evaluate_refusals``
    (which measures whether near-domain negatives are correctly refused): here we
    ask whether questions the corpus CAN answer are being answered, cited, and
    grounded — and, symmetrically, whether the pipeline over-refuses or the
    grounding gate over-blocks them.

    The ``answers`` cache (built once by ``generate_answers``) is keyed by
    question text; each value is ``{"result": <generate_with_sources dict>,
    "error": <str|None>}``. A question whose generation errored has
    ``result=None`` and is counted under ``errors`` but excluded from every
    rate (an operational failure must not be scored as a refusal or a block).

    Metrics (all honest-by-construction; see D38 for the naming rationale):

    - ``false_refusal_rate``: answerable questions whose answer IS the canonical
      refusal, over the answerable questions we got a result for. High = the
      pipeline is refusing questions it should answer.
    - ``false_block_rate``: answerable questions whose gate outcome is
      ``CITATIONS_UNVERIFIED`` (the draft would be withheld). This is
      *over-blocking PRESSURE* on answerable questions, NOT proof the block was
      wrong — the draft may genuinely deserve blocking.
    - ``sentence_citation_coverage``: micro-averaged Σ cited-sentences / Σ
      sentences over the NON-REFUSED answers (a refusal legitimately has no
      citations, so including it would deflate the figure); ``None`` when there
      are no non-refused sentences. "Syntactic" because a cited sentence may
      still carry a wrong locator — grounding is measured separately.
    - ``citation_grounded_fraction``: micro-averaged Σ grounded / Σ citations
      over non-refused answers; ``None`` ("n/a") when there are zero citations,
      never a silent 0 or 1.
    - ``gate_outcome_distribution``: counts over the answerable questions with a
      result, zero-filled across all four ``GATE_OUTCOMES``.

    Args:
        golden: Golden-set entries (``load_golden_set`` output).
        answers: The shared answer cache described above.

    Returns:
        A dict of the aggregates above plus ``per_question`` rows carrying only
        the question, type, flags, gate outcome, and counts — never answer text
        (D30). ``total`` is the number of answerable questions with a result;
        ``errors`` the number whose generation failed.
    """
    per_question: List[Dict[str, Any]] = []
    total = 0
    errors = 0
    refused = 0
    blocked = 0
    sum_sentences = 0
    sum_cited_sentences = 0
    sum_citations = 0
    sum_grounded = 0
    coverage_excluded_refusals = 0
    gate_distribution = {outcome: 0 for outcome in GATE_OUTCOMES}

    for entry in golden:
        if entry["type"] == "refusal":
            continue  # near-domain negatives are scored by evaluate_refusals

        question = entry["question"]
        cached = answers.get(question)
        if cached is None or cached.get("result") is None:
            # No answer (never generated, or generation errored): count it as an
            # error and skip every metric — scoring a crash as a refusal/block
            # would silently corrupt the rates.
            errors += 1
            per_question.append(
                {
                    "question": question,
                    "type": entry["type"],
                    "error": (cached or {}).get("error", "no answer generated"),
                    "refused": None,
                    "gate_outcome": None,
                    "n_sentences": 0,
                    "n_cited_sentences": 0,
                    "n_citations": 0,
                    "n_grounded": 0,
                    "n_ungrounded": 0,
                }
            )
            continue

        result = cached["result"]
        answer = result["answer"]
        gate_outcome = result["gate_outcome"]
        citation_check = result.get("citation_check", {})
        n_grounded = len(citation_check.get("grounded", []))
        n_ungrounded = len(citation_check.get("ungrounded", []))
        n_citations = len(result.get("citations", []))

        sentences = split_sentences(answer)
        n_sentences = len(sentences)
        n_cited_sentences = sum(1 for s in sentences if CITATION_RE.search(s))

        refused_flag = is_refusal(answer)

        total += 1
        gate_distribution[gate_outcome] = gate_distribution.get(gate_outcome, 0) + 1
        if refused_flag:
            refused += 1
            coverage_excluded_refusals += 1
        else:
            # Coverage + grounding are measured only over non-refused answers.
            sum_sentences += n_sentences
            sum_cited_sentences += n_cited_sentences
            sum_citations += n_citations
            sum_grounded += n_grounded
            if gate_outcome == CITATIONS_UNVERIFIED:
                blocked += 1

        per_question.append(
            {
                "question": question,
                "type": entry["type"],
                "error": None,
                "refused": refused_flag,
                "gate_outcome": gate_outcome,
                "n_sentences": n_sentences,
                "n_cited_sentences": n_cited_sentences,
                "n_citations": n_citations,
                "n_grounded": n_grounded,
                "n_ungrounded": n_ungrounded,
            }
        )

    coverage = (
        sum_cited_sentences / sum_sentences if sum_sentences else None
    )
    grounded_fraction = sum_grounded / sum_citations if sum_citations else None

    return {
        "per_question": per_question,
        "total": total,
        "errors": errors,
        "refused": refused,
        "false_refusal_rate": refused / total if total else 0.0,
        "blocked": blocked,
        "false_block_rate": blocked / total if total else 0.0,
        "sentence_citation_coverage": coverage,
        "coverage_excluded_refusals": coverage_excluded_refusals,
        "citation_grounded_fraction": grounded_fraction,
        "gate_outcome_distribution": gate_distribution,
        "sum_sentences": sum_sentences,
        "sum_cited_sentences": sum_cited_sentences,
        "sum_citations": sum_citations,
        "sum_grounded": sum_grounded,
    }


def generate_answers(
    golden: List[Dict[str, Any]],
    include_types: Sequence[str],
    generate_fn: Callable[[str], Dict[str, Any]],
    retries: int = 2,
    retry_backoff: float = 2.0,
) -> Dict[str, Dict[str, Any]]:
    """Generate (and cache) one answer per in-scope question, ONCE (D38).

    A single generation pass feeds three downstream metrics — refusal accuracy,
    completeness, and the judge — so every answer is produced exactly once and
    cached, keyed by question text. Which questions are generated is decided by
    the caller via ``include_types`` (the gating contract, Design 2): this
    function makes NO API calls for a question whose type is not included, which
    is what keeps a both-passes-skipped, no-judge run (and keyless CI)
    generation-free.

    Args:
        golden: Golden-set entries (``load_golden_set`` output).
        include_types: The question types to generate for (subset of
            ``VALID_TYPES``). A question outside this set is skipped entirely.
        generate_fn: ``Callable[[question], generate_with_sources dict]`` — the
            single seam that does retrieve+generate. Tests inject a fake; the
            live default (built by ``run_eval_matrix``) retrieves hybrid and
            calls ``generate_with_sources``.
        retries: Extra attempts after the first on a ``generate_fn`` exception
            (so ``retries=2`` means up to 3 tries). A question that still fails
            is recorded as an error row (``result=None``), never dropped.
        retry_backoff: Seconds multiplied by the attempt index between retries
            (``0`` disables the sleep — used by tests). Only the live path waits.

    Returns:
        ``{question: {"result": <generate_with_sources dict | None>, "error":
        <str | None>}}``. ``result`` is None exactly when every attempt raised;
        ``error`` then carries the last exception's ``"Type: message"``.

    Raises:
        ValueError: If two in-scope questions share identical text (they would
            collide in the cache — fail visibly rather than silently drop one).
    """
    answers: Dict[str, Dict[str, Any]] = {}
    for entry in golden:
        if entry["type"] not in include_types:
            continue
        question = entry["question"]
        if question in answers:
            raise ValueError(
                f"Duplicate question text in golden set (would collide in the "
                f"answer cache): {question!r}"
            )

        result: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        for attempt in range(retries + 1):
            try:
                result = generate_fn(question)
                error = None
                break
            except Exception as e:  # noqa: BLE001 — record and (maybe) retry any failure
                error = f"{type(e).__name__}: {e}"
                if attempt < retries and retry_backoff:
                    time.sleep(retry_backoff * (attempt + 1))
        answers[question] = {"result": result, "error": error}

    return answers


def _sha256_file(path: str) -> str:
    """Return the hex SHA-256 of a file's bytes (for set provenance).

    The report prints each set's sha256 so a reader can confirm the held-out
    set that produced the numbers is exactly the frozen one recorded in D33 —
    honesty-by-cross-check rather than by code-pinning the path.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_results_path(
    explicit_path: Optional[str],
    is_canonical: bool,
    set_paths: Sequence[str],
) -> Tuple[str, List[str]]:
    """Decide where the report is written, guarding the committed report (D38).

    Rules (Design 5):

    - An explicit ``--results/-o`` is always honored, EXCEPT it is refused
      (``ValueError``) if it equals any input set path — writing a report over
      an eval set would destroy the set. If it targets the canonical
      ``eval/results.md`` on a non-canonical run, it is honored but a warning is
      returned (the caller prints it).
    - With no explicit path, a fully canonical run writes ``eval/results.md``;
      anything less writes the gitignored ``eval/results_partial.md`` (with a
      note), so a partial run can never silently clobber the committed report.

    Args:
        explicit_path: The ``--results`` value, or None.
        is_canonical: Whether this run met every canonical condition.
        set_paths: The input set file paths, to refuse overwriting.

    Returns:
        ``(resolved_path, warnings)`` — warnings is a possibly-empty list of
        strings for the caller to surface on stderr.

    Raises:
        ValueError: If ``explicit_path`` equals an input set path.
    """
    warnings: List[str] = []
    if explicit_path is not None:
        # Compare by realpath, not normpath, so an absolute/relative spelling or
        # a symlink that names the SAME file as an input set is still caught
        # (a normpath string-compare would miss those aliases).
        real = os.path.realpath(explicit_path)
        for p in set_paths:
            if real == os.path.realpath(p):
                raise ValueError(
                    f"--results path {explicit_path!r} resolves to an input "
                    f"eval-set path; refusing to overwrite the eval set with a report"
                )
        if not is_canonical and real == os.path.realpath(DEFAULT_RESULTS_PATH):
            warnings.append(
                f"Writing a NON-canonical run to the canonical path "
                f"{DEFAULT_RESULTS_PATH} (explicitly requested via --results)."
            )
        return explicit_path, warnings

    if is_canonical:
        return DEFAULT_RESULTS_PATH, warnings
    warnings.append(
        f"Run is not canonical; writing to {PARTIAL_RESULTS_PATH} instead of "
        f"{DEFAULT_RESULTS_PATH} (the committed report is written only by a "
        f"held-out, all-modes, refusals+completeness, top_k=6, error-free run)."
    )
    return PARTIAL_RESULTS_PATH, warnings


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
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Dict[str, Any]:
    """Run the full Phase 5/6 evaluation and report the results.

    Superseded at the CLI by ``run_eval_matrix`` (Phase 10): the ``eval``
    subcommand now dispatches to the matrix runner, which adds the retrieval-
    mode ablation, hit@{1,3,6}+MRR, held-out headline, completeness, judge, and
    the results-path guard. ``run_eval`` is retained unchanged (its default
    ``results_path`` still ALWAYS overwrites — that footgun is exactly why the
    CLI no longer calls it) for any programmatic caller relying on the Phase 5/6
    single-set contract and its existing test coverage.

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
            to ``collect_provenance`` with this run's ``persist_directory`` (so
            the chunk count is read from the index under test, not the hard-coded
            default dir) and ``results_path`` passed as its ``exclude_paths``
            (the report about to be written is expected to be dirty and
            shouldn't count as a surprise) — this default shells out to git and
            opens the Chroma store, so tests MUST inject a fake to stay IO-free.
        persist_directory: ChromaDB persistence directory, threaded into both
            ``evaluate_retrieval`` and ``evaluate_refusals`` (Phase 9
            load-once retrieval); ignored by either pass whose ``retrieve_fn``
            / ``answer_fn`` was given explicitly, since only their own default
            builders consult it.

    Returns:
        Dict with ``retrieval`` (evaluate_retrieval's return value),
        ``refusals`` (evaluate_refusals's return value, or None if skipped),
        ``provenance`` (the provenance dict), ``golden_path``, and ``top_k``.
    """
    golden = load_golden_set(golden_path)

    # Build the load-once retrieve_fn a SINGLE time for the whole run (Phase 9 /
    # D37): one store open + one BM25 unpickle, shared by the retrieval pass AND
    # (when it runs) the refusal pass's default answer_fn. Previously each pass
    # built its own default, so a full run opened the store and unpickled the
    # sidecar twice. An explicitly injected retrieve_fn/answer_fn bypasses this.
    if retrieve_fn is None:
        retrieve_fn = _build_default_retrieve_fn(top_k, persist_directory)

    retrieval = evaluate_retrieval(
        golden, retrieve_fn=retrieve_fn, top_k=top_k, persist_directory=persist_directory
    )

    if skip_refusals:
        refusals = None
    else:
        if answer_fn is None:

            def answer_fn(question: str) -> str:
                """Answer via the run's single load-once retrieve_fn, then generate.

                Derived from the SAME retrieve_fn as the retrieval pass so the
                whole run opens the store and unpickles the BM25 index exactly
                once, not once per evaluate_* pass.
                """
                return generate_with_sources(
                    question, retrieve_fn(question, top_k=top_k)
                )["answer"]

        refusals = evaluate_refusals(
            golden, answer_fn=answer_fn, top_k=top_k, persist_directory=persist_directory
        )

    if provenance_fn is None:
        provenance_fn = lambda: collect_provenance(
            persist_directory=persist_directory, exclude_paths=(results_path,)
        )
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


def _atomic_write(path: str, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    Mirrors the BM25-sidecar write convention: the destination is only ever
    replaced by a fully-written temp file, so an interrupted or crashing write
    leaves any pre-existing file (e.g. the committed ``eval/results.md``) byte-
    identical rather than half-overwritten.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run_eval_matrix(
    set_specs: List[Tuple[str, str]],
    modes: Sequence[str] = RETRIEVAL_MODES,
    top_k: int = 6,
    skip_refusals: bool = False,
    skip_completeness: bool = False,
    judge: bool = False,
    judge_sample: Optional[int] = None,
    results_path: Optional[str] = None,
    retrieve_fn_factory: Optional[Callable[[str], Callable[..., List[Dict[str, Any]]]]] = None,
    generate_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    judge_fn: Optional[Callable[[Dict[str, str]], str]] = None,
    provenance_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    judge_dump_path: Optional[str] = None,
    persist_directory: str = CHROMA_PERSIST_DIR,
) -> Dict[str, Any]:
    """Run the Phase 10 eval matrix (sets × retrieval modes) and write the v2 report.

    Supersedes ``run_eval`` for the CLI: it scores every ``(set, mode)`` cell of
    retrieval hit@{1,3,6}+MRR, runs ONE shared generation pass (hybrid, the
    production config) feeding refusal accuracy, completeness (false-refusal AND
    false-block), and — optionally — the experimental LLM judge, then renders one
    honest, held-out-headline report.

    Gating (Design 2, blocker fix): the generation pass runs iff
    ``(not skip_refusals) or (not skip_completeness) or judge``. When it does
    not run, ``generate_fn`` is never called — which is what keeps the offline
    ablation preview (and keyless CI, where generation would RAISE) API-free.
    ``include_types`` is derived from the active passes: refusal-type answers are
    generated iff refusals are scored; in-corpus answers iff completeness is
    scored or the judge is on.

    Canonical guard (Design 5): with no explicit ``results_path``, the committed
    ``eval/results.md`` is written ONLY by a fully canonical run — a held-out set
    present, all three modes, refusals AND completeness scored, ``top_k == 6``,
    and zero generation errors. Anything less writes the gitignored
    ``eval/results_partial.md``. The report is written atomically.

    Args:
        set_specs: ``[(label, path), ...]`` — one entry per question set. A label
            containing ``"held-out"`` marks the frozen out-of-sample set (used
            for the headline and the canonical check).
        modes: Retrieval modes to ablate (subset/permutation of
            ``RETRIEVAL_MODES``); each is scored independently per set.
        top_k, skip_refusals, skip_completeness, judge, judge_sample: run knobs.
        results_path: Explicit report destination (``--results/-o``); None auto-
            resolves per the canonical guard.
        retrieve_fn_factory: ``mode -> retrieve_fn`` builder (tests inject
            fakes). The default builds the store+BM25 index ONCE and threads
            ``mode=`` and ``strict_errors=True`` so a single-arm eval never pays
            the other arm's IO and an operational failure aborts rather than
            scoring as a miss.
        generate_fn: ``question -> generate_with_sources dict`` (tests inject).
            The default retrieves hybrid and calls ``generate_with_sources``.
        judge_fn: judge ``llm_fn`` forwarded to ``judge_answers`` (tests inject).
        provenance_fn: override for the provenance block (tests inject a fake to
            stay IO-free).
        judge_dump_path: if set and the judge runs, per-item judge records
            (including claim text) are written here as JSONL for local review —
            gitignored; never committed, never in the report (D30).
        persist_directory: index directory for the default retrieval/generation.

    Returns:
        A dict with ``sets`` (per-set results), ``modes``, ``top_k``,
        ``provenance``, ``results_path``, ``is_canonical``, the run knobs,
        ``generation_ran``, and ``include_types``.
    """
    if not set_specs:
        raise ValueError("run_eval_matrix requires at least one (label, path) set")
    modes = list(modes)
    for mode in modes:
        if mode not in RETRIEVAL_MODES:
            raise ValueError(
                f"Unknown retrieval mode {mode!r}; expected from {RETRIEVAL_MODES}"
            )
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    if judge_sample is not None and judge_sample < 0:
        raise ValueError(f"judge_sample must be >= 0, got {judge_sample}")

    set_paths = [path for _label, path in set_specs]
    # Fail fast on the dangerous footgun (report over an eval set) BEFORE any
    # expensive generation, even though _resolve_results_path re-guards at write.
    # Compare by realpath so abs/rel/symlink aliases are caught too.
    if results_path is not None:
        real = os.path.realpath(results_path)
        for p in set_paths:
            if real == os.path.realpath(p):
                raise ValueError(
                    f"--results path {results_path!r} resolves to an input "
                    f"eval-set path; refusing to overwrite the eval set with a report"
                )

    # Which passes run, and therefore which answers to generate (Design 2).
    generation_ran = (not skip_refusals) or (not skip_completeness) or judge
    include_types: List[str] = []
    if not skip_refusals:
        include_types.append("refusal")
    if (not skip_completeness) or judge:
        include_types.extend(IN_CORPUS_TYPES)

    # Load each backend at most once, and ONLY when a mode that uses it actually
    # runs — so a vector-only matrix never unpickles the BM25 sidecar and a
    # bm25-only matrix never opens Chroma or runs the embedding-model manifest
    # check (the isolation retrieve() itself provides, preserved end-to-end
    # rather than defeated by pre-loading both). The vector loader keeps the
    # blessed manifest check that load_retrieval_context bundles.
    vector_cache: List[Any] = []
    bm25_cache: List[Any] = []

    def _get_vector() -> Any:
        if not vector_cache:
            assert_embedding_model(persist_directory)
            vector_cache.append(get_vector_store(persist_directory=persist_directory))
        return vector_cache[0]

    def _get_bm25() -> Any:
        if not bm25_cache:
            bm25_cache.append(load_bm25_index(persist_directory))
        return bm25_cache[0]

    if retrieve_fn_factory is None:

        def retrieve_fn_factory(mode: str) -> Callable[..., List[Dict[str, Any]]]:
            def _fn(question: str, top_k: int = top_k) -> List[Dict[str, Any]]:
                # Inject only the arm this mode uses; retrieve()'s mode gating
                # then never touches (or loads) the other arm.
                vs = _get_vector() if mode in ("hybrid", "vector") else None
                bm = _get_bm25() if mode in ("hybrid", "bm25") else None
                return retrieve(
                    question,
                    top_k=top_k,
                    persist_directory=persist_directory,
                    vector_store=vs,
                    bm25_index=bm,
                    mode=mode,
                    strict_errors=True,
                )

            return _fn

    if generate_fn is None:

        def generate_fn(question: str) -> Dict[str, Any]:
            results = retrieve(
                question,
                top_k=top_k,
                persist_directory=persist_directory,
                vector_store=_get_vector(),
                bm25_index=_get_bm25(),
                mode="hybrid",
                strict_errors=True,
            )
            return generate_with_sources(question, results)

    sets: List[Dict[str, Any]] = []
    total_generation_errors = 0
    judge_dump_records: List[Dict[str, Any]] = []

    for label, path in set_specs:
        golden = load_golden_set(path)
        counts: Dict[str, int] = {}
        for entry in golden:
            counts[entry["type"]] = counts.get(entry["type"], 0) + 1

        # Retrieval ablation: one evaluate_retrieval per mode, each with its
        # mode-specific retrieve_fn (single-arm modes never touch the other arm).
        retrieval_by_mode: Dict[str, Dict[str, Any]] = {}
        for mode in modes:
            retrieval_by_mode[mode] = evaluate_retrieval(
                golden,
                retrieve_fn=retrieve_fn_factory(mode),
                top_k=top_k,
                persist_directory=persist_directory,
            )

        # One shared generation pass, only if a consuming pass is active.
        answers: Dict[str, Dict[str, Any]] = {}
        generation_errors = 0
        if generation_ran:
            answers = generate_answers(golden, include_types, generate_fn)
            generation_errors = sum(
                1 for a in answers.values() if a["result"] is None
            )
            total_generation_errors += generation_errors

        refusals = None
        if not skip_refusals:
            def _answer_fn(question: str, _a: Dict[str, Any] = answers) -> str:
                # A generation error yields "" -> is_refusal("")=False -> scored
                # as "not refused". That is the CONSERVATIVE direction (it can
                # only DEFLATE refusal accuracy, never inflate it) and a run with
                # any generation error is already flagged non-canonical and
                # discloses the error count, so the deflation is never silent.
                cached = _a.get(question) or {}
                result = cached.get("result") or {}
                return result.get("answer", "")

            refusals = evaluate_refusals(golden, answer_fn=_answer_fn)

        completeness = None
        if not skip_completeness:
            completeness = evaluate_completeness(golden, answers)

        judge_result = None
        if judge:
            from src.judge import judge_answers  # lazy: only import when judging

            items: List[Dict[str, str]] = []
            for entry in golden:
                if entry["type"] not in IN_CORPUS_TYPES:
                    continue
                cached = answers.get(entry["question"]) or {}
                result = cached.get("result")
                if result is None:
                    continue  # generation error — nothing to judge
                if is_refusal(result["answer"]):
                    continue  # judge is conditional on a non-refused answer
                context = format_context(
                    [
                        {"document": d, "score": 0.0, "metadata": d.metadata}
                        for d in result.get("source_documents", [])
                    ]
                )
                items.append(
                    {
                        "question": entry["question"],
                        "answer": result["answer"],
                        "context": context,
                    }
                )
            judge_result = judge_answers(
                items, llm_fn=judge_fn, sample_n=judge_sample
            )
            for record in judge_result.get("per_item", []):
                judge_dump_records.append({"set": label, **record})

        sets.append(
            {
                "label": label,
                "path": path,
                "sha256": _sha256_file(path),
                "counts": counts,
                "n_questions": len(golden),
                "retrieval": retrieval_by_mode,
                "refusals": refusals,
                "completeness": completeness,
                "judge": judge_result,
                "generation_errors": generation_errors,
            }
        )

    # Canonical only if EVERY condition holds (Design 5).
    has_heldout = any(HELDOUT_LABEL_TOKEN in label for label, _ in set_specs)
    all_modes = set(modes) == set(RETRIEVAL_MODES)
    is_canonical = (
        has_heldout
        and all_modes
        and (not skip_refusals)
        and (not skip_completeness)
        and top_k == 6
        and total_generation_errors == 0
    )

    resolved_path, warnings = _resolve_results_path(
        results_path, is_canonical, set_paths
    )
    for warning in warnings:
        print(f"[eval] {warning}", file=sys.stderr)

    if provenance_fn is None:
        provenance_fn = lambda: collect_provenance(
            persist_directory=persist_directory, exclude_paths=(resolved_path,)
        )
    provenance = provenance_fn()

    result = {
        "sets": sets,
        "modes": modes,
        "top_k": top_k,
        "provenance": provenance,
        "results_path": resolved_path,
        "is_canonical": is_canonical,
        "skip_refusals": skip_refusals,
        "skip_completeness": skip_completeness,
        "judge": judge,
        "judge_sample": judge_sample,
        "generation_ran": generation_ran,
        "include_types": include_types,
        "generation_errors": total_generation_errors,
    }

    report = _format_matrix_report(result)
    print(report)
    _atomic_write(resolved_path, report)

    # Write the gitignored judge review dump (claim text lives ONLY here).
    if judge and judge_dump_path and judge_dump_records:
        dump_lines = [json.dumps(r, ensure_ascii=False) for r in judge_dump_records]
        _atomic_write(judge_dump_path, "\n".join(dump_lines) + "\n")

    return result


def _mrr_label(top_k: int) -> str:
    """The truncated-MRR label, e.g. 'MRR@6' — cutoff always disclosed."""
    return f"MRR@{top_k}"


def _fmt_opt_rate(value: Optional[float]) -> str:
    """Render an optional rate: a 3-dp number, or 'n/a' when None."""
    return "n/a" if value is None else f"{value:.3f}"


def _format_matrix_report(result: Dict[str, Any]) -> str:
    """Render the Phase 10 v2 report (D38); honors D30 (no chunk/answer/claim text).

    Only question text, section numbers, counts, rates, gate outcomes, and
    provenance appear. The headline is strict hit@6 on the held-out set, with a
    count and a Wilson 95% interval so its small-n uncertainty is explicit.
    """
    sets = result["sets"]
    modes = result["modes"]
    top_k = result["top_k"]
    provenance = result["provenance"]
    prov_get = provenance.get
    lines: List[str] = []

    lines.append("# Legal RAG Evaluation Report v2 (held-out, ablated)")
    lines.append("")
    lines.append(f"- Date: {datetime.now().isoformat()}")
    lines.append(f"- top_k: {top_k}")
    lines.append(f"- Retrieval modes ablated: {', '.join(modes)}")
    lines.append(
        f"- Canonical run (writes the committed report): {result['is_canonical']}"
    )
    lines.append("")

    # ---- Provenance ---------------------------------------------------------
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- git sha: {prov_get('git_sha')} (dirty: {prov_get('git_dirty')})")
    lines.append(f"- indexed chunk count: {prov_get('chunk_count')}")
    lines.append(f"- embedding model: {prov_get('embedding_model')}")
    lines.append(f"- generation model: {prov_get('generation_model')}")
    lines.append(f"- matching: {prov_get('matching')}")
    lines.append(
        f"- {_mrr_label(top_k)}: truncated mean reciprocal rank — a question "
        f"with no match in the top {top_k} scores 0 (cutoff disclosed)."
    )
    passes = []
    passes.append("retrieval ablation")
    passes.append("refusals" if not result["skip_refusals"] else "refusals SKIPPED")
    passes.append(
        "completeness" if not result["skip_completeness"] else "completeness SKIPPED"
    )
    passes.append("judge" if result["judge"] else "judge off")
    lines.append(f"- passes: {', '.join(passes)}")
    lines.append(
        "- answer passes (refusals/completeness/judge) use the HYBRID production "
        "retrieval config; the mode ablation affects retrieval scoring only."
    )
    if result["generation_errors"]:
        lines.append(
            f"- generation errors: {result['generation_errors']} "
            "(run is NON-canonical)"
        )
    lines.append("")
    lines.append("Question sets:")
    for s in sets:
        counts_str = ", ".join(f"{t}={s['counts'][t]}" for t in sorted(s["counts"]))
        if HELDOUT_LABEL_TOKEN in s["label"]:
            note = "held-out (never tuned — out-of-sample)"
        elif "tuning" in s["label"]:
            note = "tuning (used to select fusion constants, D31 — NOT held-out)"
        else:
            note = "supplementary set"
        lines.append(f"- {s['label']}: {note}")
        lines.append(f"  - path: {s['path']}")
        lines.append(f"  - sha256: {s['sha256']}")
        lines.append(f"  - question counts: {counts_str} (n={s['n_questions']})")
    lines.append("")

    # ---- Headline -----------------------------------------------------------
    lines.append("## Headline: strict hit@6 on the held-out set (hybrid)")
    lines.append("")
    heldout = next((s for s in sets if HELDOUT_LABEL_TOKEN in s["label"]), None)
    headline_set = heldout or (sets[0] if sets else None)
    if (
        headline_set
        and "hybrid" in headline_set["retrieval"]
        and 6 in headline_set["retrieval"]["hybrid"]["ks"]
    ):
        r = headline_set["retrieval"]["hybrid"]
        hits = r["hit_at_k"]["strict"][6]
        n = r["total"]
        rate = r["hit_rate_at_k"]["strict"][6]
        low, high = _wilson_ci(hits, n)
        qualifier = "" if heldout else " (NO held-out set present — first set shown)"
        lines.append(
            f"**strict hit@6 = {hits}/{n} = {rate:.3f}** "
            f"(95% Wilson CI {low:.3f}–{high:.3f}), set: {headline_set['label']}{qualifier}."
        )
        lines.append("")
        lines.append(
            "This is a single curated-set estimate: with n≈20 the interval spans "
            "several questions' worth of rate, so treat it as indicative, not a "
            "statistically-validated architecture claim."
        )
    else:
        lines.append(
            "Headline unavailable: needs hybrid mode scored at top_k>=6."
        )
    lines.append("")

    # ---- Per-set ablation tables -------------------------------------------
    for s in sets:
        lines.append(f"## {s['label']} — retrieval ablation")
        lines.append("")
        # Union of ks actually present (they match across modes for a given
        # top_k, but read defensively).
        ks = s["retrieval"][modes[0]]["ks"] if modes else []
        strict_hdr = " | ".join(f"S@{k}" for k in ks)
        related_hdr = " | ".join(f"R@{k}" for k in ks)
        lines.append(
            f"| Mode | {strict_hdr} | {related_hdr} | {_mrr_label(top_k)} strict "
            f"| {_mrr_label(top_k)} related | n |"
        )
        lines.append(
            "| --- | " + " | ".join(["---"] * (2 * len(ks) + 3)) + " |"
        )
        for mode in modes:
            r = s["retrieval"][mode]
            strict_cells = " | ".join(
                f"{r['hit_rate_at_k']['strict'][k]:.3f}" for k in r["ks"]
            )
            related_cells = " | ".join(
                f"{r['hit_rate_at_k']['related'][k]:.3f}" for k in r["ks"]
            )
            lines.append(
                f"| {mode} | {strict_cells} | {related_cells} | "
                f"{r['mrr_strict']:.3f} | {r['mrr_related']:.3f} | {r['total']} |"
            )
        lines.append("")
        # By-type breakdown for the hybrid mode (the production config).
        if "hybrid" in s["retrieval"] and s["retrieval"]["hybrid"]["by_type"]:
            lines.append(f"By type (hybrid), strict / related hit rate:")
            lines.append("")
            lines.append("| Type | Strict rate | Related rate | n |")
            lines.append("| --- | --- | --- | --- |")
            for q_type, stats in sorted(
                s["retrieval"]["hybrid"]["by_type"].items()
            ):
                lines.append(
                    f"| {q_type} | {stats['hit_rate_strict']:.3f} | "
                    f"{stats['hit_rate_related']:.3f} | {stats['total']} |"
                )
            lines.append("")

    # ---- Refusal (two-sided) + answer quality ------------------------------
    for s in sets:
        comp = s["completeness"]
        ref = s["refusals"]
        if comp is None and ref is None:
            continue
        lines.append(f"## {s['label']} — refusals & answer quality")
        lines.append("")
        lines.append(
            "Two-sided refusal view — a system can fail by refusing answerable "
            "questions OR by answering questions it should refuse:"
        )
        lines.append("")
        lines.append("| Direction | Rate | Count |")
        lines.append("| --- | --- | --- |")
        if comp is not None:
            low, high = _wilson_ci(comp["refused"], comp["total"])
            lines.append(
                f"| answerable questions REFUSED (false refusals) | "
                f"{comp['false_refusal_rate']:.3f} (95% CI {low:.3f}–{high:.3f}) "
                f"| {comp['refused']}/{comp['total']} |"
            )
        if ref is not None:
            low, high = _wilson_ci(ref["refused"], ref["total"])
            lines.append(
                f"| near-domain NEGATIVES refused (correct refusals) | "
                f"{ref['accuracy']:.3f} (95% CI {low:.3f}–{high:.3f}) "
                f"| {ref['refused']}/{ref['total']} |"
            )
        lines.append("")
        if comp is not None:
            lines.append("Answer quality on the answerable questions:")
            lines.append("")
            lines.append(
                f"- Syntactic sentence-citation coverage (micro-avg over "
                f"non-refused answers): {_fmt_opt_rate(comp['sentence_citation_coverage'])} "
                f"({comp['sum_cited_sentences']}/{comp['sum_sentences']} sentences; "
                f"{comp['coverage_excluded_refusals']} refusal(s) excluded). "
                "\"Syntactic\" = has a bracket citation; a cited sentence may still "
                "carry a wrong locator (grounding measured separately)."
            )
            lines.append(
                f"- Citation-grounded fraction (micro-avg Σ grounded / Σ "
                f"citations): {_fmt_opt_rate(comp['citation_grounded_fraction'])} "
                f"({comp['sum_grounded']}/{comp['sum_citations']} citations)."
            )
            low, high = _wilson_ci(comp["blocked"], comp["total"])
            lines.append(
                f"- False-block rate (answerable drafts the gate would WITHHOLD "
                f"as CITATIONS_UNVERIFIED): {comp['false_block_rate']:.3f} "
                f"(95% CI {low:.3f}–{high:.3f}; {comp['blocked']}/{comp['total']}). "
                "This is over-blocking PRESSURE, not proof each block was wrong."
            )
            if comp["errors"]:
                lines.append(
                    f"- generation errors on answerable questions: {comp['errors']}"
                )
            dist = comp["gate_outcome_distribution"]
            dist_str = ", ".join(f"{o}={dist.get(o, 0)}" for o in GATE_OUTCOMES)
            lines.append(f"- Gate-outcome distribution: {dist_str}")
            lines.append("")

    # ---- Judge --------------------------------------------------------------
    lines.append("## LLM judge (experimental faithfulness estimate)")
    lines.append("")
    if not result["judge"]:
        lines.append("Judge: not run.")
        lines.append("")
    else:
        lines.append(
            "Experimental and secondary — gates nothing. Conditional on a "
            "non-refused answer. The judge is the SAME model family as the "
            "generator, so it can share its blind spots; read as a rough estimate."
        )
        lines.append("")
        for s in sets:
            j = s["judge"]
            if j is None:
                continue
            mean_str = (
                "SUPPRESSED (too many judge failures — unreliable)"
                if j["suppressed"]
                else _fmt_opt_rate(j["mean_faithfulness"])
            )
            lines.append(
                f"- {s['label']}: mean faithfulness = {mean_str} "
                f"(over {j['scored_n']} scored; attempted {j['attempted']}, "
                f"parsed {j['successful']}, api-errors {j['api_errors']}, "
                f"parse-errors {j['parse_errors']}, zero-claim {j['zero_claim']}; "
                f"judge={j['judge_model']} {j['prompt_version']})."
            )
        lines.append("")

    # ---- Per-question detail (D30-safe: no answer/chunk/claim text) ----------
    for s in sets:
        lines.append(f"## {s['label']} — per-question detail (hybrid)")
        lines.append("")
        hybrid = s["retrieval"].get("hybrid")
        # Gate outcome per question from the completeness pass, if it ran.
        gate_by_q: Dict[str, Any] = {}
        if s["completeness"] is not None:
            for row in s["completeness"]["per_question"]:
                gate_by_q[row["question"]] = row
        if hybrid is not None:
            for q in hybrid["per_question"]:
                strict = "HIT" if q["hit_strict"] else "MISS"
                related = "HIT" if q["hit_related"] else "MISS"
                extra = ""
                grow = gate_by_q.get(q["question"])
                if grow is not None:
                    extra = f" gate={grow['gate_outcome']}"
                lines.append(
                    f"- [{q['type']}] strict={strict}(rank={q['first_strict_rank']}) "
                    f"related={related}(rank={q['first_related_rank']}) "
                    f"expected={q['expected_sections']} "
                    f"retrieved={q['retrieved_sections']}{extra} :: {q['question']}"
                )
        # Refusal-type per-question rows (from the refusal pass).
        if s["refusals"] is not None:
            for q in s["refusals"]["per_question"]:
                status = "refused" if q["refused"] else "answered"
                lines.append(f"- [refusal] {status} :: {q['question']}")
        lines.append("")

    return "\n".join(lines) + "\n"
