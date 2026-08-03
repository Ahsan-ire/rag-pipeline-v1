"""Phase 15 WS4.5 — cold-load and per-query embedding latency for one arm.

A bake-off diagnostic (never a selection criterion — the brief's decision rule
is golden strict@6): how long the model takes to load once, and how long a
single query embedding takes, over ~20 realistic questions.

Run it once per arm, with the arm's model in the environment::

    EMBEDDING_MODEL=<model-id> .venv/bin/python scripts/embed_latency.py

It prints a paste-ready Markdown row for the brief's arm table.

The timing core (:func:`measure`) is a pure function over an injected
``embed_fn``, so the unit tests exercise it with a fake embedder and a fake
clock and never load a model.
"""
import argparse
import json
import os
import statistics
import sys
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src import embedder  # noqa: E402

DEFAULT_QUERIES = os.path.join("eval", "realistic_set.jsonl")
DEFAULT_N = 20
DEFAULT_WARMUP = 3


def _percentile(samples: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of ``samples`` (1-indexed rank, no averaging).

    Nearest-rank rather than an interpolating estimator so the number is a
    latency that was actually observed, and so the same input always yields
    the same output regardless of platform float behaviour. With n=20, p95 is
    the 19th slowest sample.
    """
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("no samples to take a percentile of")
    rank = max(1, -(-int(pct) * len(ordered) // 100))  # ceil(pct/100 * n)
    return ordered[min(rank, len(ordered)) - 1]


def measure(
    embed_fn: Callable[[str], Any],
    queries: Sequence[str],
    warmup: int = DEFAULT_WARMUP,
) -> Dict[str, Optional[float]]:
    """Time one embedding call per query and summarise the distribution.

    Args:
        embed_fn: single-query embedding callable (``embeddings.embed_query``).
        queries: the queries to time, one timed call each.
        warmup: untimed calls made first (cycling through ``queries``), so the
            first timed sample does not carry lazy per-process setup that the
            model load already paid for.

    Returns:
        ``{"cold_load_s": None, "p50_ms", "p95_ms", "mean_ms"}``.
        ``cold_load_s`` is None here by design: this function is handed an
        already-loaded ``embed_fn``, so only the caller that performed the load
        can fill it in.

    Raises:
        ValueError: if ``queries`` is empty.
    """
    if not queries:
        raise ValueError("measure() needs at least one query")
    for i in range(max(0, warmup)):
        embed_fn(queries[i % len(queries)])

    samples_ms: List[float] = []
    for query in queries:
        start = perf_counter()
        embed_fn(query)
        samples_ms.append((perf_counter() - start) * 1000.0)

    return {
        "cold_load_s": None,
        "p50_ms": _percentile(samples_ms, 50),
        "p95_ms": _percentile(samples_ms, 95),
        "mean_ms": statistics.fmean(samples_ms),
    }


def load_queries(path: str, n: int = DEFAULT_N) -> List[str]:
    """Read up to ``n`` questions from a JSONL eval set.

    ``n`` is clamped to the file's size, so asking for 20 questions from a
    17-question set measures 17 rather than failing.

    Args:
        path: JSONL file whose rows carry a ``question`` field.
        n: maximum number of questions to return (clamped, never negative).

    Returns:
        The first ``min(n, len(rows))`` questions, in file order.
    """
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    questions = [r["question"] for r in rows]
    return questions[: max(0, min(n, len(questions)))]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: load the model, time it, print the arm-table row."""
    parser = argparse.ArgumentParser(
        description=(
            "Measure cold model-load time and per-query embed latency for the "
            "model named by EMBEDDING_MODEL."
        )
    )
    parser.add_argument(
        "--queries",
        default=DEFAULT_QUERIES,
        metavar="PATH",
        help=f"JSONL question set to time against (default: {DEFAULT_QUERIES}).",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_N,
        help=f"How many questions to time, clamped to the file (default: {DEFAULT_N}).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP,
        help=f"Untimed calls before timing starts (default: {DEFAULT_WARMUP}).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    queries = load_queries(args.queries, args.n)

    start = perf_counter()
    embeddings = embedder.get_embedding_function()
    cold_load_s = perf_counter() - start

    stats = measure(embeddings.embed_query, queries, warmup=args.warmup)
    stats["cold_load_s"] = cold_load_s

    print("| Model | Cold load | p50 | p95 |")
    print("| --- | --- | --- | --- |")
    print(
        f"| {embedder.EMBEDDING_MODEL} | {cold_load_s:.1f}s | "
        f"{stats['p50_ms']:.1f}ms | {stats['p95_ms']:.1f}ms |"
    )
    print(
        f"\n(mean {stats['mean_ms']:.1f}ms over n={len(queries)} queries from "
        f"{args.queries}, warmup {args.warmup})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
