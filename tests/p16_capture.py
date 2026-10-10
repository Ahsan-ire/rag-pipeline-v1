"""P0 capture driver for Phase 16A-1: the v5 report lock (spec item 0, acceptance (d)).

Not a test module (no ``test_`` prefix, so pytest never collects it). It runs
both v5 report formatters -- ``run_eval`` (the Phase 5/6 single-set report) and
``run_eval_matrix`` (the v5 matrix report) -- over the public v1 eval sets plus
the invented ``tests/fixtures/p16_synthetic_heldout.jsonl`` (labelled
``held-out``), once per report branch, and returns the report texts.

Everything that could touch the network, a model or the index is faked or made
to raise, BEFORE ``src`` is imported:

- ``PYTHON_DOTENV_DISABLED=1`` and ``ANTHROPIC_API_KEY`` dropped, so no ``.env``
  key can leak into the run;
- the ``anthropic`` and ``ChatAnthropic`` client constructors, the
  ``SentenceTransformer`` constructor and the Chroma constructors raise (and
  every firing is recorded, so a swallowed one still fails the capture);
- retrieval is a fixed, hash-derived ranked list of synthetic section numbers
  (``tests/fixtures/p16_fake_retrieval.json``);
- generation, the judge and ``expand_query`` (accepting any keywords) are fakes;
  a fake meter is passed only where the runner accepts ``meter=``, and
  ``privacy="public"`` only where it accepts ``privacy=``.

The clock, set paths and provenance are fixed, and reports go to a temporary
directory, so two runs on equal behaviour produce byte-identical output.

Typical use: run once against a scratch worktree of ``main`` before any 16A-1
code edit, writing ``tests/fixtures/p16_v5_projection_main.json``;
``tests/test_p16_projection.py`` re-runs :func:`capture` in-process on the
working tree and asserts the reports are unchanged.

Usage:
    python tests/p16_capture.py --repo <path-to-checkout> --out <json>
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

# The checkout this driver lives in. Set files are opened relative to it (cwd),
# so a scratch worktree of main (which predates the synthetic fixture) can be
# captured with its own ``src`` but this checkout's set files -- the public v1
# sets are byte-identical in both.
DATA_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
FAKE_RETRIEVAL = os.path.join("tests", "fixtures", "p16_fake_retrieval.json")

GOLDEN = "eval/golden_set.jsonl"
REALISTIC = "eval/realistic_set.jsonl"
SAMPLE = "eval/sample_golden_set.jsonl"
SYNTH_HELDOUT = "tests/fixtures/p16_synthetic_heldout.jsonl"

FIXED_NOW = datetime(2026, 10, 10, 12, 0, 0)
PROVENANCE = {
    "git_sha": "p16fixed",
    "git_dirty": False,
    "git_dirty_other": 0,
    "chunk_count": 1470,
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "generation_model": "claude-sonnet-5",
    "matching": (
        "strict = exact section-number equality; related = dotted-nesting "
        "either direction (a retrieved parent OR child of an expected section "
        "also counts, e.g. expected 6.3.2 matches retrieved 6.3.2.2 or 6.3)"
    ),
}


class CaptureError(RuntimeError):
    """The capture could not be trusted (a guarded constructor fired, wrong src)."""


def _h(*parts: str) -> bytes:
    """sha256 digest of the parts joined by ``|`` (the driver's only randomness)."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).digest()


# --------------------------------------------------------------------------
# Environment guards
# --------------------------------------------------------------------------
def _prepare_env() -> None:
    """Disable .env loading and drop the API key before ``src`` is imported."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ.pop("ANTHROPIC_API_KEY", None)


@contextlib.contextmanager
def _raising_constructors(fired: List[str]) -> Iterator[None]:
    """Patch every network/model/index constructor to record and raise."""

    def _raiser(name: str) -> Callable[..., Any]:
        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            fired.append(name)
            raise CaptureError(f"p16_capture: {name} was constructed")

        return _boom

    import anthropic
    import chromadb
    import langchain_anthropic
    import langchain_chroma
    import sentence_transformers

    targets = [
        (anthropic.Anthropic, "__init__", "anthropic.Anthropic"),
        (anthropic.AsyncAnthropic, "__init__", "anthropic.AsyncAnthropic"),
        (langchain_anthropic.ChatAnthropic, "__init__", "ChatAnthropic"),
        (sentence_transformers.SentenceTransformer, "__init__", "SentenceTransformer"),
        (langchain_chroma.Chroma, "__init__", "langchain_chroma.Chroma"),
        (chromadb, "PersistentClient", "chromadb.PersistentClient"),
        (chromadb, "Client", "chromadb.Client"),
    ]
    with contextlib.ExitStack() as stack:
        for owner, attr, name in targets:
            stack.enter_context(mock.patch.object(owner, attr, _raiser(name)))
        yield


class _FixedDatetime(datetime):
    """``datetime`` whose ``now()`` is the fixed capture clock."""

    @classmethod
    def now(cls, tz: Any = None) -> "datetime":  # noqa: D102 - fixed clock
        return FIXED_NOW


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
def _load_rows(path: str) -> List[Dict[str, Any]]:
    """Read a v1 JSONL set (the driver's own copy, for the fakes' lookups)."""
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class FakeRetrieval:
    """Fixed ranked chunk lists: hash-placed expected section + synthetic filler."""

    def __init__(self, expected_by_q: Dict[str, List[str]]) -> None:
        """Load the filler fixture; ``expected_by_q`` maps question -> sections."""
        with open(FAKE_RETRIEVAL, encoding="utf-8") as fh:
            spec = json.load(fh)
        self.filler: List[str] = spec["filler"]
        self.first_page: int = spec["pages"]["first"]
        self.span: int = spec["pages"]["span"]
        self.expected_by_q = expected_by_q

    def sections(self, mode: str, question: str, top_k: int) -> List[str]:
        """Ranked section numbers for ``(mode, question)``; deterministic."""
        h = _h(mode, question)
        expected = self.expected_by_q.get(question, [])
        target: Optional[str] = None
        slot = h[0] % 9  # 0..8: ranks 1..6 place a hit, 7..9 leave a miss at 6
        if expected:
            target = expected[h[1] % len(expected)]
            if h[2] % 4 == 0 and "." in target:
                target = target.rsplit(".", 1)[0]  # parent: a related-only hit
        start = h[3] % len(self.filler)
        out: List[str] = []
        for rank in range(top_k):
            if target is not None and rank == slot:
                out.append(target)
            else:
                out.append(self.filler[(start + rank) % len(self.filler)])
        return out

    def results(self, mode: str, question: str, top_k: int) -> List[Dict[str, Any]]:
        """``retrieve()``-shaped results for ``(mode, question)``."""
        from langchain_core.documents import Document

        out = []
        for rank, sec in enumerate(self.sections(mode, question, top_k)):
            page = self.first_page + (int(_h(sec)[0]) % 50)
            meta = {
                "section_number": sec,
                "chapter_number": sec.split(".")[0],
                "chapter_title": "Synthetic chapter",
                "heading": "Synthetic heading",
                "page_start": page,
                "page_end": page + self.span,
                "source": "synthetic.pdf",
                "title": "Synthetic Handbook",
                "document_type": "handbook",
            }
            doc = Document(page_content=f"Synthetic chunk {sec}.", metadata=meta, id=f"syn-{sec}")
            out.append({"document": doc, "score": 1.0 - 0.05 * rank, "metadata": meta})
        return out

    def factory(self, top_k_default: int) -> Callable[[str], Callable[..., List[Dict[str, Any]]]]:
        """A ``retrieve_fn_factory`` for ``run_eval_matrix``."""

        def _factory(mode: str) -> Callable[..., List[Dict[str, Any]]]:
            def _fn(question: str, top_k: int = top_k_default) -> List[Dict[str, Any]]:
                return self.results(mode, question, top_k)

            return _fn

        return _factory


def _fake_expand(question: str, *args: Any, **kwargs: Any) -> Any:
    """``expand_query`` stand-in: one live rewrite, an intent on most rows."""
    from src.query_rewrite import REWRITE_MODEL, STATUS_DISABLED, STATUS_LIVE, Expansion

    if kwargs.get("enabled", True) is False:
        return Expansion(question, (), REWRITE_MODEL, STATUS_DISABLED)
    h = _h("expand", question)
    intent = None if h[0] % 5 == 0 else f"synthetic intent {h.hex()[:8]}"
    return Expansion(question, (f"synthetic rewrite {h.hex()[:8]}",), REWRITE_MODEL, STATUS_LIVE, intent)


# Generation behaviours a scenario can assign to a question.
ANSWER, PARTIAL, UNVERIFIED, CAVEAT, REFUSE, ERROR = (
    "answer", "partial", "unverified", "caveat", "refuse", "error",
)
TRUNCATED, DECLINED, INCOMPLETE, UNKNOWN = "truncated", "declined", "incomplete", "unknown"


class FakeGeneration:
    """``generate_fn`` stand-in; the behaviour per question comes from a picker."""

    def __init__(self, retrieval: FakeRetrieval, pick: Callable[[str, bool], str]) -> None:
        """``pick(question, is_refusal_row) -> behaviour``."""
        self.retrieval = retrieval
        self.pick = pick
        self.refusal_qs: set = set()

    def __call__(self, question: str) -> Dict[str, Any]:
        """Return a ``generate_with_sources``-shaped dict (or raise for ERROR)."""
        from src.generator import CAVEAT_PREFIX, REFUSAL_PHRASE
        from src.grounding import (
            CITATIONS_UNVERIFIED,
            CITATIONS_VERIFIED,
            PARTIALLY_VERIFIED,
            REFUSAL,
        )

        behaviour = self.pick(question, question in self.refusal_qs)
        if behaviour == ERROR:
            raise RuntimeError("synthetic generation failure")
        results = self.retrieval.results("hybrid", question, 6)
        docs = [r["document"] for r in results]
        sec = docs[0].metadata["section_number"]
        page = docs[0].metadata["page_start"]
        cite = f"[Handbook, para {sec}, p.{page}]"
        status: Optional[str] = "complete"
        if behaviour in (TRUNCATED, DECLINED, INCOMPLETE):
            status = behaviour
        if behaviour == REFUSE:
            answer, gate, citations, grounded, ungrounded = REFUSAL_PHRASE + ".", REFUSAL, [], [], []
        elif behaviour == UNVERIFIED:
            answer = f"The synthetic rule applies {cite}. It also requires a form."
            gate, citations, grounded, ungrounded = CITATIONS_UNVERIFIED, [sec], [], [sec]
        elif behaviour == PARTIAL:
            answer = (
                f"The synthetic rule applies {cite}. A second point follows "
                f"[Handbook, para 99.9, p.1]. The material does not say how long it takes."
            )
            gate, citations, grounded, ungrounded = PARTIALLY_VERIFIED, [sec, "99.9"], [sec], ["99.9"]
        elif behaviour == CAVEAT:
            answer = f"{CAVEAT_PREFIX} The synthetic guidance is related {cite}."
            gate, citations, grounded, ungrounded = CITATIONS_VERIFIED, [sec], [sec], []
        else:
            answer = f"The synthetic rule applies {cite}. Another sentence {cite}."
            gate, citations, grounded, ungrounded = CITATIONS_VERIFIED, [sec, sec], [sec, sec], []
        out: Dict[str, Any] = {
            "answer": answer,
            "gate_outcome": gate,
            "citations": [{"paragraph": c} for c in citations],
            "citation_check": {"grounded": grounded, "ungrounded": ungrounded},
            "source_documents": docs,
        }
        if behaviour != UNKNOWN:
            out["generation_status"] = status
        return out


def _judge_fn(mode: str) -> Callable[[Dict[str, str]], str]:
    """Judge ``llm_fn`` stand-in: ``clean`` parses; ``failing`` mostly fails."""

    def _fn(prompt_vars: Dict[str, str]) -> str:
        h = _h("judge", prompt_vars["question"])
        if mode == "failing":
            if h[0] % 3 == 0:
                return "not json at all"
            if h[0] % 3 == 1:
                raise RuntimeError("synthetic judge api failure")
        if h[1] % 7 == 0:
            return '{"claims": []}'
        verdicts = ["supported", "supported", "unclear", "unsupported"]
        claims = [
            {"claim": f"synthetic claim {i}", "verdict": verdicts[(h[2] + i) % 4]}
            for i in range(1 + h[3] % 3)
        ]
        return json.dumps({"claims": claims})

    return _fn


class _InertClient:
    """A metered-client stand-in that fails the capture if it is ever invoked."""

    def __init__(self, name: str) -> None:
        self.name = name

    def invoke(self, *_a: Any, **_k: Any) -> Any:
        raise CaptureError(f"p16_capture: fake meter client {self.name!r} was invoked")

    __call__ = invoke


class _FakeMeter:
    """Meter stand-in for runners that accept ``meter=`` (16A-1 item 8).

    Hands out inert clients for generation, rewrite and judge; with every model
    seam faked, none may ever be invoked (an invocation fails the capture).
    """

    run_total_eur = 0.0
    week_total_eur = 0.0

    def generation_llm(self) -> _InertClient:
        return _InertClient("generation")

    def rewrite_llm(self) -> _InertClient:
        return _InertClient("rewrite")

    def judge_llm(self) -> _InertClient:
        return _InertClient("judge")


def _runner_kwargs(fn: Callable[..., Any]) -> Dict[str, Any]:
    """``privacy="public"`` / a fake meter, only where ``fn`` accepts them."""
    params = inspect.signature(fn).parameters
    extra: Dict[str, Any] = {}
    if "privacy" in params:
        extra["privacy"] = "public"
    if "meter" in params:
        extra["meter"] = _FakeMeter()
    return extra


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------
def _pick_canonical(question: str, is_refusal_row: bool) -> str:
    """Mixed, all-complete behaviours: a canonical-shaped run."""
    h = _h("gen", question)
    if is_refusal_row:
        return REFUSE if h[0] % 4 else CAVEAT
    return (ANSWER, ANSWER, PARTIAL, UNVERIFIED, CAVEAT, REFUSE, ANSWER)[h[0] % 7]


def _pick_with(overrides: Dict[int, str]) -> Callable[[str, bool], str]:
    """Canonical picks, except rows whose hash bucket (mod 11) is overridden."""

    def _pick(question: str, is_refusal_row: bool) -> str:
        bucket = _h("branch", question)[0] % 11
        return overrides.get(bucket, _pick_canonical(question, is_refusal_row))

    return _pick


MATRIX_SETS = [("tuning", GOLDEN), ("held-out", SYNTH_HELDOUT), ("realistic", REALISTIC)]

# name -> (set_specs, run kwargs, generation picker, judge mode)
MATRIX_BRANCHES: Dict[str, Tuple[List[Tuple[str, str]], Dict[str, Any], Callable[[str, bool], str], str]] = {
    "matrix_offline": (
        MATRIX_SETS, {"skip_refusals": True, "skip_completeness": True}, _pick_canonical, "clean",
    ),
    "matrix_canonical": (MATRIX_SETS, {"judge": True}, _pick_canonical, "clean"),
    "matrix_generation_error": (MATRIX_SETS, {"judge": True}, _pick_with({0: ERROR, 5: ERROR}), "clean"),
    "matrix_incomplete_unknown": (
        MATRIX_SETS,
        {"judge": True},
        _pick_with({1: TRUNCATED, 3: DECLINED, 6: INCOMPLETE, 8: UNKNOWN}),
        "clean",
    ),
    "matrix_judge_suppressed": (MATRIX_SETS, {"judge": True}, _pick_canonical, "failing"),
    "matrix_no_heldout": ([("tuning", GOLDEN), ("realistic", REALISTIC)], {"judge": True}, _pick_canonical, "clean"),
    "matrix_sample_hybrid_k3": (
        [("golden", SAMPLE)],
        {"modes": ["hybrid"], "top_k": 3, "skip_completeness": True},
        _pick_canonical,
        "clean",
    ),
}

# name -> (set path, skip_refusals, picker)
RUN_EVAL_BRANCHES: Dict[str, Tuple[str, bool, Callable[[str, bool], str]]] = {
    # Refusal rows carry a truncated draft on some buckets: the legacy
    # ``- [refusal] excluded (<status>) :: ...`` row of the v5 single-set report.
    "run_eval_legacy_excluded": (GOLDEN, False, _pick_with({0: TRUNCATED, 2: TRUNCATED, 4: DECLINED, 7: UNKNOWN})),
    "run_eval_skip_refusals": (SYNTH_HELDOUT, True, _pick_canonical),
}


def _expected_index(paths: Sequence[str]) -> Tuple[Dict[str, List[str]], set]:
    """Question -> expected sections, and the refusal-type questions, over ``paths``."""
    expected: Dict[str, List[str]] = {}
    refusals: set = set()
    for path in paths:
        for row in _load_rows(path):
            expected[row["question"]] = [str(s).strip() for s in row.get("expected_sections") or []]
            if row["type"] == "refusal":
                refusals.add(row["question"])
    return expected, refusals


def _run_branches(tmp: str) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Run every branch; return ``(reports, meta)`` keyed by branch name."""
    import src.evaluator as ev

    expected, refusal_qs = _expected_index([GOLDEN, REALISTIC, SAMPLE, SYNTH_HELDOUT])
    retrieval = FakeRetrieval(expected)
    reports: Dict[str, str] = {}
    meta: Dict[str, Any] = {}

    for name, (set_specs, knobs, pick, judge_mode) in MATRIX_BRANCHES.items():
        gen = FakeGeneration(retrieval, pick)
        gen.refusal_qs = refusal_qs
        out_path = os.path.join(tmp, f"{name}.md")
        kwargs = dict(knobs)
        top_k = kwargs.pop("top_k", 6)
        result = ev.run_eval_matrix(
            list(set_specs),
            top_k=top_k,
            results_path=out_path,
            retrieve_fn_factory=retrieval.factory(top_k),
            generate_fn=gen,
            judge_fn=_judge_fn(judge_mode),
            provenance_fn=lambda: dict(PROVENANCE),
            judge_dump_path=os.path.join(tmp, f"{name}.judge.jsonl"),
            **kwargs,
            **_runner_kwargs(ev.run_eval_matrix),
        )
        with open(out_path, encoding="utf-8") as fh:
            reports[name] = fh.read()
        meta[name] = {"is_canonical": result["is_canonical"]}

    for name, (path, skip_refusals, pick) in RUN_EVAL_BRANCHES.items():
        gen = FakeGeneration(retrieval, pick)
        gen.refusal_qs = refusal_qs

        def _answer_fn(question: str, _gen: FakeGeneration = gen) -> Dict[str, Any]:
            res = _gen(question)
            out = {"answer": res["answer"]}
            if "generation_status" in res:
                out["generation_status"] = res["generation_status"]
            return out

        out_path = os.path.join(tmp, f"{name}.md")
        retrieve_fn = retrieval.factory(6)("hybrid")
        ev.run_eval(
            path,
            top_k=6,
            skip_refusals=skip_refusals,
            results_path=out_path,
            retrieve_fn=retrieve_fn,
            answer_fn=_answer_fn,
            provenance_fn=lambda: dict(PROVENANCE),
            **_runner_kwargs(ev.run_eval),
        )
        with open(out_path, encoding="utf-8") as fh:
            reports[name] = fh.read()
        meta[name] = {"is_canonical": None}
    return reports, meta


def capture(repo: str) -> Dict[str, Any]:
    """Run every branch against ``repo``'s ``src`` and return the projection.

    Args:
        repo: checkout whose ``src`` package is imported (``sys.path`` head).
            When ``src`` is already imported (the in-process lock), it must
            come from ``repo``.

    Returns:
        ``{"main_sha", "reports": {branch: text}, "meta": {branch: {...}}}``.

    Raises:
        CaptureError: if ``src`` resolves outside ``repo`` or a guarded
            constructor fired during the run.
    """
    repo = os.path.realpath(repo)
    _prepare_env()
    if repo not in sys.path:
        sys.path.insert(0, repo)
    import src.evaluator as ev

    if not os.path.realpath(ev.__file__).startswith(repo + os.sep):
        raise CaptureError(f"src imported from {ev.__file__}, not from {repo}")

    fired: List[str] = []
    old_cwd = os.getcwd()
    sink_out, sink_err = io.StringIO(), io.StringIO()
    try:
        os.chdir(DATA_ROOT)
        with tempfile.TemporaryDirectory(prefix="p16cap-") as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(_raising_constructors(fired))
            stack.enter_context(mock.patch.object(ev, "datetime", _FixedDatetime))
            stack.enter_context(mock.patch.object(ev, "expand_query", _fake_expand))
            stack.enter_context(mock.patch("src.query_rewrite.expand_query", _fake_expand))
            stack.enter_context(mock.patch("time.sleep", lambda *_a, **_k: None))
            stack.enter_context(contextlib.redirect_stdout(sink_out))
            stack.enter_context(contextlib.redirect_stderr(sink_err))
            reports, meta = _run_branches(tmp)
    finally:
        os.chdir(old_cwd)
    if fired:
        raise CaptureError(f"guarded constructors fired: {sorted(set(fired))}")

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - metadata only
        sha = "unavailable"
    return {"main_sha": sha, "reports": reports, "meta": meta}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: capture against ``--repo`` and write the projection JSON to ``--out``."""
    parser = argparse.ArgumentParser(description="Capture the P0 v5 report projection.")
    parser.add_argument("--repo", required=True, help="checkout whose src/ to import")
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args(argv)
    out = os.path.realpath(args.out)
    projection = capture(args.repo)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(projection, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
