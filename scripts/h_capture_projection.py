"""Capture the evaluator projection for the integrity hotfix (H0, spec amendment 1).

Runs exactly the test IDs listed in ``tests/fixtures/h_projection_manifest.txt``
in-process against the ``src/`` and ``tests/`` of ``--repo``, while wrapping the
evaluator functions those tests exercise, and writes a JSON projection keyed by
test ID: per-row strict/related ranks, sentence-coverage numerators and
denominators per row, completeness aggregates, refusal-accuracy counts, and the
judge input texts. The projection is values only (no timestamps, sorted keys),
so two runs on equal behaviour produce byte-identical files.

Typical use: run once against a checkout of ``main`` before any hotfix edit, to
write ``tests/fixtures/h_eval_projection_main.json``; ``tests/test_h_projection.py``
re-runs it on the working tree and asserts the projection is unchanged.

Usage:
    python scripts/h_capture_projection.py --repo <path> --out <json>
        [--manifest <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List

DEFAULT_MANIFEST = os.path.join("tests", "fixtures", "h_projection_manifest.txt")


def read_manifest(path: str) -> List[str]:
    """Return the test node IDs from a manifest (lines containing ``::``)."""
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if "::" in ln]


def read_manifest_sha(path: str) -> str:
    """Return the SHA recorded on the manifest's first line (``main_sha: <sha>``)."""
    with open(path, encoding="utf-8") as fh:
        first = fh.readline().strip()
    return first.split(":", 1)[1].strip() if first.startswith("main_sha:") else ""


def _project_retrieval(res: Dict[str, Any]) -> Dict[str, Any]:
    """Project an ``evaluate_retrieval`` result to ranks and totals."""
    return {
        "total": res["total"],
        "rows": [
            [q["question"], q["first_strict_rank"], q["first_related_rank"]]
            for q in res["per_question"]
        ],
    }


def _project_completeness(res: Dict[str, Any]) -> Dict[str, Any]:
    """Project an ``evaluate_completeness`` result to aggregates and row counts."""
    aggregate_keys = (
        "total", "errors", "refused", "blocked", "false_refusal_rate",
        "false_block_rate", "sentence_citation_coverage",
        "coverage_excluded_refusals", "citation_grounded_fraction",
        "sum_sentences", "sum_cited_sentences", "sum_citations", "sum_grounded",
    )
    out: Dict[str, Any] = {k: res[k] for k in aggregate_keys}
    out["gate_outcome_distribution"] = dict(res["gate_outcome_distribution"])
    out["rows"] = [
        [
            r["question"], r["error"], r["refused"], r["gate_outcome"],
            r["n_sentences"], r["n_cited_sentences"], r["n_citations"],
            r["n_grounded"], r["n_ungrounded"],
        ]
        for r in res["per_question"]
    ]
    return out


def _project_refusals(res: Dict[str, Any]) -> Dict[str, Any]:
    """Project an ``evaluate_refusals`` result to counts and per-row flags."""
    return {
        "refused": res["refused"],
        "total": res["total"],
        "accuracy": res["accuracy"],
        "rows": [[q["question"], q["refused"]] for q in res["per_question"]],
    }


class _Recorder:
    """Pytest plugin: wraps evaluator functions and records per-test values."""

    def __init__(self) -> None:
        """Start with an empty projection."""
        self.current: str | None = None
        self.projection: Dict[str, Dict[str, Any]] = {}

    def _entry(self) -> Dict[str, Any]:
        """Return the projection bucket of the running test."""
        assert self.current is not None, "wrapped function ran outside a test"
        return self.projection.setdefault(self.current, {})

    def wrap(self, fn: Callable, key: str, project: Callable) -> Callable:
        """Wrap ``fn`` so its projected return value is appended under ``key``."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **kwargs)
            self._entry().setdefault(key, []).append(project(result))
            return result

        wrapper.__name__ = getattr(fn, "__name__", key)
        return wrapper

    def install(self) -> None:
        """Patch the evaluator/judge entry points (before test collection)."""
        import src.evaluator as ev
        import src.judge as judge

        ev.evaluate_retrieval = self.wrap(
            ev.evaluate_retrieval, "retrieval", _project_retrieval
        )
        ev.evaluate_completeness = self.wrap(
            ev.evaluate_completeness, "completeness", _project_completeness
        )
        ev.evaluate_refusals = self.wrap(
            ev.evaluate_refusals, "refusals", _project_refusals
        )
        ev.run_eval_matrix = self.wrap(
            ev.run_eval_matrix,
            "matrix",
            lambda r: {
                "is_canonical": r["is_canonical"],
                "generation_errors": r["generation_errors"],
                "rewrite_attempts": r["rewrite_attempts"],
                "rewrite_fallbacks": r["rewrite_fallbacks"],
            },
        )

        original_judge = judge.judge_answers

        def judge_wrapper(items: List[Dict[str, str]], *args: Any, **kwargs: Any) -> Any:
            self._entry().setdefault("judge_inputs", []).append(
                [[i["question"], i["answer"], i["context"]] for i in items]
            )
            return original_judge(items, *args, **kwargs)

        judge.judge_answers = judge_wrapper

    # -- pytest hooks --------------------------------------------------------

    def pytest_configure(self, config: Any) -> None:
        """Install the wrappers before any test module is imported."""
        self.install()

    def pytest_runtest_setup(self, item: Any) -> None:
        """Remember which test is running."""
        self.current = item.nodeid

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Record the outcome of the call phase (or of a failed setup)."""
        if report.when == "call" or (report.when == "setup" and report.failed):
            self.projection.setdefault(report.nodeid, {})["outcome"] = report.outcome


def capture(repo: str, manifest: str) -> Dict[str, Any]:
    """Run the manifest's tests against ``repo`` and return the projection."""
    import pytest

    repo = os.path.realpath(repo)
    ids = read_manifest(manifest)
    sys.path.insert(0, repo)
    os.chdir(repo)
    recorder = _Recorder()
    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "--rootdir", repo, *ids], plugins=[recorder]
    )
    import src.evaluator as ev

    if not os.path.realpath(ev.__file__).startswith(repo + os.sep):
        raise RuntimeError(f"src imported from {ev.__file__}, not from {repo}")
    if code != 0:
        raise RuntimeError(f"manifest tests failed (pytest exit {code})")
    missing = [i for i in ids if i not in recorder.projection]
    if missing:
        raise RuntimeError(f"manifest ids not run: {missing[:3]}")
    return {
        "main_sha": read_manifest_sha(manifest),
        "tests": {k: recorder.projection[k] for k in sorted(recorder.projection)},
    }


def main(argv: List[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Capture the H0 evaluator projection for the manifest's tests."
    )
    parser.add_argument("--repo", required=True, help="repo whose src/ and tests/ to import")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument(
        "--manifest", default=None,
        help=f"manifest path (default: <this checkout>/{DEFAULT_MANIFEST})",
    )
    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    manifest = os.path.realpath(args.manifest or os.path.join(here, DEFAULT_MANIFEST))
    out = os.path.realpath(args.out)
    projection = capture(args.repo, manifest)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(projection, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
