"""Phase 14 WS2.4 — W sweep over the intent-fusion weight.

Protocol (plan-gated): ONE cached expansion per golden+realistic answerable
question (live Haiku, cached to JSON so the fusion sweep is offline and
reproducible), then sweep W in {0, 0.25, 0.5} where W=0 is the same-expansion
baseline. Scoring replicates evaluate_retrieval exactly: first-rank strict
(literal section equality) and related (_sections_related dotted nesting),
hit@6. Selection rule: smallest W making S5 strict@6 HIT, subject to zero
golden-control regressions vs the W=0 arm and N4 staying HIT.

Phase 15 (WS4.6) adds three things and changes nothing else: ``main`` builds
the cache with ``offline_only=True`` (the "zero API" claim becomes structural
rather than conditional on cache completeness — Codex C5), ``--persist-dir``
so a bake-off arm index can be swept instead of the default one, and
``--ranks-out`` so the per-question ranks can be diffed arm-to-arm by
``scripts/bakeoff_report.py`` instead of only eyeballed as printed text.

Phase 16A-1 (items 1, 6, 9; D65, D68):

- **No ``chdir``.** Every repo path (the eval sets, the cache, the default
  index) is resolved absolutely from this file, so ``python
  scripts/w_sweep.py`` works from any cwd; user-given relative paths
  (``--persist-dir``, ``--ranks-out``) resolve against the caller's cwd.
- **One scorer, one roster.** Ranks come from ``src.eval_scoring
  .score_evidence`` (one group == the evaluator's first-rank logic); S5 and N4
  come from ``src.eval_roster.ROLE_BY_NAME`` by row id, not list index.
- **C4 rank dumps.** ``--ranks-out`` writes the rows-sidecar fields
  (``version``, ``scorer_version``, ``absorbed_map_sha256``, ``expansion``
  identity ``{"kind": "cache", "digest": <cache sha256>}``, ``cohorts`` with a
  ``label``, ``inputs``, ``rows`` keyed by (set sha256, id) with mode
  ``W=<w>``). A public run also keeps the legacy ``ranks`` dict (question
  text) so a ``--legacy`` comparison against a pre-16A dump still works.
- **Privacy floor.** :func:`run_sweep` takes a keyword-only ``privacy`` (no
  default); ``main`` derives it as the strictest ``classify`` over the set
  files and the cache. The cache is the legacy-public 0717 cache, so without
  ``--legacy-public`` the sweep floors to private: stdout then carries
  aggregates and opaque ids only, and ``--ranks-out`` is written only under
  ``eval/private/runs/<run id>/`` beside ``inputs.json``. Sealed input exits
  4 before anything is loaded.
"""
import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.bakeoff_report import private_output_path  # noqa: E402
from src import eval_sets as _eval_sets  # noqa: E402
from src.embedder import CHROMA_PERSIST_DIR  # noqa: E402
from src.eval_cohort import SCORER_VERSION, SIDECAR_VERSION, v1_cohort  # noqa: E402
from src.eval_privacy import (  # noqa: E402
    PUBLIC,
    SEALED,
    PrivacyFloorError,
    SealedInputError,
    check_floor,
    public_v1_id,
    require_class,
    safe_error,
    write_inputs_json,
    write_private,
)
from src.eval_roster import ROLE_BY_NAME  # noqa: E402
from src.eval_scoring import score_evidence  # noqa: E402
from src.query_rewrite import STATUS_LIVE, expand_query  # noqa: E402
from src.retriever import load_retrieval_context, retrieve  # noqa: E402

CACHE = os.path.join(REPO, "eval", "w_sweep_expansions_20260717.json")
SET_PATHS: Tuple[Tuple[str, str], ...] = (
    ("golden", os.path.join(REPO, "eval", "golden_set.jsonl")),
    ("realistic", os.path.join(REPO, "eval", "realistic_set.jsonl")),
)
DEFAULT_PERSIST_DIR = os.path.normpath(os.path.join(REPO, CHROMA_PERSIST_DIR))
# S5 and N4 (D50) by name; their row ids live in src.eval_roster.
ROLE_NAMES = ("S5", "N4")
WEIGHTS = [0.0, 0.25, 0.5]
TOP_K = 6
EXIT_SEALED = 4


def load_set_files(set_paths: Sequence[Tuple[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Load the sweep's eval sets.

    Returns:
        ``{label: {"path", "sha256", "all": [every row], "rows": [answerable
        rows]}}`` (answerable = ``type != "refusal"``, as before).
    """
    sets: Dict[str, Dict[str, Any]] = {}
    for label, path in set_paths:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        sets[label] = {
            "path": path,
            "sha256": _eval_sets.sha256_file(path),
            "all": rows,
            "rows": [r for r in rows if r["type"] != "refusal"],
        }
    return sets


def build_cache(
    sets: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    offline_only: bool = False,
    name_question: Optional[Callable[[str], str]] = None,
    meter: Any = None,
    privacy: str,
) -> Dict[str, Dict[str, Any]]:
    """Load the expansion cache, filling any gap with a live Haiku call.

    Args:
        sets: ``{label: [row, ...]}``; every row's ``question`` needs a
            cached expansion for the sweep to run.
        offline_only: when True, refuse to call the API: a question with no
            ``STATUS_LIVE`` cache entry raises :class:`RuntimeError` instead of
            being expanded live. This is what makes the sweep's "zero API
            calls" property structural rather than conditional on the cache
            happening to be complete (Codex C5) — a question edited in the eval
            set now fails loudly instead of silently spending budget.
        name_question: how the error names the question; defaults to its
            opening 60 characters. A private run passes the row's opaque id.
        meter: a ``src.spend.SpendMeter`` (D70). A live fill expands through
            its metered rewrite client (one attempt per question: the meter's
            own loop owns retries); with a usable API key and no meter a live
            fill raises ``SpendMeterRequired`` before any call.
        privacy: the class of ``sets`` -- REQUIRED, and must be ``public``,
            for a live fill: the fill writes question text as JSON keys into
            ``CACHE``, a tracked file outside the private root (gate round 4).
            Ignored when ``offline_only`` (nothing is written).

    Returns:
        ``{question: {"rewrites": [...], "status": str, "intent": str|None}}``.

    Raises:
        RuntimeError: under ``offline_only``, for the first question lacking a
            live cache entry (named by ``name_question``).
    """
    describe = name_question or (lambda q: repr(q[:60]))
    rewrite_kwargs: Dict[str, Any] = {}
    attempts = 3  # zero-fallback requirement: retry twice (unmetered legacy path)
    if not offline_only:
        if privacy != PUBLIC:
            raise PrivacyFloorError(
                "a live cache fill writes the tracked public cache; only public sets may fill it"
            )
        from src.evaluator import _require_meter

        _require_meter(meter, live_paths=(True,))
        if meter is not None:
            rewrite_kwargs = {"llm": meter.rewrite_llm()}
            attempts = 1
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            cache = json.load(f)
    changed = False
    for rows in sets.values():
        for r in rows:
            q = r["question"]
            if q in cache and cache[q]["status"] == STATUS_LIVE:
                continue
            if offline_only:
                status = cache[q]["status"] if q in cache else "absent"
                raise RuntimeError(
                    "offline_only: no live cached expansion "
                    f"(status={status}) for question: {describe(q)} — "
                    f"refusing to call the API; refresh {CACHE} deliberately"
                )
            for _attempt in range(attempts):
                exp = expand_query(q, enabled=True, **rewrite_kwargs)
                if exp.status == STATUS_LIVE:
                    break
            cache[q] = {
                "rewrites": list(exp.rewrites),
                "status": exp.status,
                "intent": exp.intent_rewrite,
            }
            changed = True
    if changed:
        with open(CACHE, "w") as f:
            json.dump(cache, f, indent=1)
    return cache


def ranks_of(expected: Sequence[str], retrieved_sections: Sequence[str]) -> Tuple[Optional[int], Optional[int]]:
    """``(first strict rank, first related rank)`` via ``score_evidence``.

    One group (the v1 expected sections as OR alternatives) is exactly the
    evaluator's ``first_strict_rank`` / ``first_related_rank`` logic
    (``tests/test_p16w_scoring.py``); a row with no expected sections ranks
    nowhere.
    """
    if not expected:
        return None, None
    ranked = [(str(i), sec) for i, sec in enumerate(retrieved_sections)]
    strict = score_evidence(ranked, [list(expected)], "strict")["completion_rank"]
    related = score_evidence(ranked, [list(expected)], "related")["completion_rank"]
    return strict, related


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the sweep's CLI arguments (no argument reproduces Phase 14)."""
    parser = argparse.ArgumentParser(
        description=(
            "Sweep the intent-fusion weight W over the golden + realistic sets "
            "using cached expansions (zero API calls)."
        )
    )
    parser.add_argument(
        "--persist-dir",
        default=DEFAULT_PERSIST_DIR,
        help=(
            "ChromaDB directory to sweep; defaults to the production index "
            f"({CHROMA_PERSIST_DIR} under the repo). Point it at a bake-off arm "
            "index to replay that arm under the shipped production config."
        ),
    )
    parser.add_argument(
        "--ranks-out",
        default=None,
        metavar="PATH",
        help=(
            "Also write the per-question ranks as JSON (C4 fields), so arms can "
            "be diffed machine-readably (scripts/bakeoff_report.py). Printed "
            "output is unchanged with or without this flag. On a private run "
            "it is written under eval/private/runs/<run id>/."
        ),
    )
    parser.add_argument(
        "--legacy-public",
        action="store_true",
        help=(
            "Enable classify's frozen legacy-public lookup (eval/legacy_public.json "
            "lists the 0717 expansion cache). Without it the sweep floors to private."
        ),
    )
    return parser.parse_args(argv)


def input_paths() -> List[str]:
    """Every eval input the sweep opens: its set files and the expansion cache."""
    return [path for _label, path in SET_PATHS] + [CACHE]


def _find_role_rows(sets: Dict[str, Dict[str, Any]]) -> Dict[str, Optional[Tuple[str, int]]]:
    """``{role name: (label, index)}`` of S5/N4 by public v1 id (roster key)."""
    found: Dict[str, Optional[Tuple[str, int]]] = {}
    for name in ROLE_NAMES:
        spec = ROLE_BY_NAME.get(name)
        found[name] = None
        if spec is None:
            continue
        for label, data in sets.items():
            for i, row in enumerate(data["rows"]):
                if public_v1_id(row["question"]) == spec.row_id:
                    found[name] = (label, i)
                    break
            if found[name] is not None:
                break
    return found


def run_sweep(
    *,
    persist_dir: str,
    ranks_out: Optional[str],
    privacy: str,
    legacy_public: bool = False,
    meter: Any = None,
) -> int:
    """Run the sweep, print the per-W summary, optionally dump the ranks.

    The entry function (item 1): ``privacy`` is keyword-only with no default.
    The floor (strictest ``classify`` over the set files and the cache) is
    re-derived here, and a weaker ``privacy`` raises before anything is
    loaded, retrieved or expanded.

    Args:
        persist_dir: ChromaDB directory to sweep.
        ranks_out: where to write the C4 rank dump (None: no file).
        privacy: the caller's class (``main`` passes the derived floor).
        legacy_public: enables classify's legacy-public lookup (rule 6).
        meter: forwarded to ``build_cache`` (D70). The sweep itself runs
            ``offline_only`` (zero API calls), so it is never used today.

    Returns:
        0 on success, 1 when cached fallbacks make the sweep non-canonical.

    Raises:
        SealedInputError: a sealed set or cache.
        PrivacyFloorError: ``privacy`` weaker than the floor.
    """
    paths = input_paths()
    _eval_sets.refuse_sealed(paths)
    floor = _eval_sets.floor(paths, legacy_public=legacy_public)
    privacy = check_floor(require_class(privacy), floor)
    public = privacy == PUBLIC

    sets = load_set_files(SET_PATHS)
    cohorts: List[Dict[str, Any]] = []
    ids: Dict[str, Dict[str, str]] = {}
    for label, data in sets.items():
        block, set_ids = v1_cohort(
            data["all"], path=data["path"], privacy=privacy, sha256=data["sha256"]
        )
        block["label"] = label
        cohorts.append(block)
        ids[label] = set_ids
    any_id = {q: rid for set_ids in ids.values() for q, rid in set_ids.items()}

    rows_by_label = {label: data["rows"] for label, data in sets.items()}
    cache = build_cache(
        rows_by_label,
        offline_only=True,
        name_question=None if public else (lambda q: any_id.get(q, "<unlisted cache entry>")),
        meter=meter,
        privacy=privacy,
    )
    non_live = {q: c["status"] for q, c in cache.items() if c["status"] != STATUS_LIVE}
    n_intent = sum(1 for c in cache.values() if c["intent"])
    if public:
        shown_non_live = {q[:60]: s for q, s in non_live.items()}
    else:
        # A cache entry outside the sets has no id; count it, never show it.
        shown_non_live = {any_id[q]: s for q, s in non_live.items() if q in any_id}
        if len(shown_non_live) != len(non_live):
            shown_non_live["<unlisted entries>"] = str(len(non_live) - len(shown_non_live))
    print(f"expansions cached: {len(cache)}  non-live: {shown_non_live or 0}  with-intent: {n_intent}")
    if non_live:
        print("FATAL: fallbacks present — sweep would not match canonical conditions")
        return 1

    vs, bm = load_retrieval_context(persist_directory=persist_dir)
    ranks: Dict[Tuple[float, str, int], Tuple[Optional[int], Optional[int]]] = {}
    detail: Dict[str, Dict[str, Any]] = {}  # "W=<w>|<label>|<i>" -> full row (public only)
    dump_rows: List[Dict[str, Any]] = []
    for W in WEIGHTS:
        for label, data in sets.items():
            for i, r in enumerate(data["rows"]):
                c = cache[r["question"]]
                res = retrieve(
                    r["question"], top_k=TOP_K, vector_store=vs, bm25_index=bm,
                    mode="hybrid", strict_errors=True,
                    rewrites=c["rewrites"] or None,
                    intent_rewrite=c["intent"], intent_weight=W,
                )
                secs = [
                    str(x["document"].metadata.get("section_number", "")).strip()
                    for x in res
                ]
                expected = [str(s).strip() for s in r["expected_sections"]]
                strict, related = ranks_of(expected, secs)
                ranks[(W, label, i)] = (strict, related)
                dump_rows.append(
                    {
                        "set_sha256": data["sha256"],
                        "id": ids[label][r["question"]],
                        "mode": f"W={W}",
                        "strict_rank": strict,
                        "related_rank": related,
                        # v1: one group, completion == first strict rank
                        # (src.eval_cohort.rank_rows does the same).
                        "completion_rank": strict,
                    }
                )
                if public:
                    detail[f"W={W}|{label}|{i}"] = {
                        "question": r["question"],
                        "expected": expected,
                        "strict_rank": strict,
                        "related_rank": related,
                        "retrieved_sections": secs,
                    }

    roles = _find_role_rows(sets)
    for W in WEIGHTS:
        print(f"\n=== W = {W} ===")
        for label, data in sets.items():
            n = len(data["rows"])
            s6 = sum(1 for i in range(n) if (ranks[(W, label, i)][0] or 99) <= 6)
            r6 = sum(1 for i in range(n) if (ranks[(W, label, i)][1] or 99) <= 6)
            print(f"  {label}: strict@6 {s6}/{n} ({s6/n:.3f})  related@6 {r6}/{n} ({r6/n:.3f})")
        parts = []
        for name in ROLE_NAMES:
            where = roles[name]
            if where is None:
                parts.append(f"{name}: not found in the sets")
            else:
                s, rr = ranks[(W, where[0], where[1])]
                parts.append(f"{name}: strict_rank={s} related_rank={rr}")
        print("  " + "   ".join(parts))
        if W > 0.0:
            def _flip(label: str, i: int) -> Tuple[Any, ...]:
                row = sets[label]["rows"][i]
                if public:
                    return (label, i, row["question"][:70])
                return (label, ids[label][row["question"]])

            def _gain(label: str, i: int) -> Tuple[Any, ...]:
                if public:
                    return (label, i)
                return (label, ids[label][sets[label]["rows"][i]["question"]])

            flips = [
                _flip(label, i)
                for label, data in sets.items()
                for i in range(len(data["rows"]))
                if (ranks[(0.0, label, i)][0] or 99) <= 6 and (ranks[(W, label, i)][0] or 99) > 6
            ]
            gains = [
                _gain(label, i)
                for label, data in sets.items()
                for i in range(len(data["rows"]))
                if (ranks[(0.0, label, i)][0] or 99) > 6 and (ranks[(W, label, i)][0] or 99) <= 6
            ]
            print(f"  strict flips HIT->MISS vs W=0: {flips or 'none'}")
            print(f"  strict gains MISS->HIT vs W=0: {gains or 'none'}")

    if ranks_out:
        cache_sha = _eval_sets.sha256_file(CACHE)
        set_inputs = [
            {"path": data["path"], "sha256": data["sha256"], "kind": "questions"}
            for data in sets.values()
        ]
        payload: Dict[str, Any] = {
            "version": SIDECAR_VERSION,
            "scorer_version": SCORER_VERSION,
            "absorbed_map_sha256": None,
            "expansion": {"kind": "cache", "digest": cache_sha},
            "privacy": privacy,
            "persist_dir": persist_dir,
            "weights": list(WEIGHTS),
            "top_k": TOP_K,
            "cohorts": cohorts,
            "inputs": set_inputs,
            "rows": sorted(dump_rows, key=lambda r: (r["set_sha256"], r["mode"], r["id"])),
        }
        if public:
            payload["ranks"] = detail
            with open(ranks_out, "w") as f:
                json.dump(payload, f, indent=1)
        else:
            rdir, target = private_output_path(ranks_out)
            write_private(target, json.dumps(payload, indent=1))
            write_inputs_json(
                rdir,
                [
                    *set_inputs,
                    {
                        "path": CACHE,
                        "sha256": cache_sha,
                        "kind": "derived",
                        "sources": [{"path": s["path"], "sha256": s["sha256"]} for s in set_inputs],
                    },
                ],
            )
            print(f"\n[w_sweep] private run: ranks written to {target}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point (see the module docstring).

    Exit codes: 0 ok; 1 fallbacks present, or an error on a private run
    (printed as its exception type only); 4 sealed input (before anything is
    loaded).
    """
    args = _parse_args(argv)
    paths = input_paths()
    if any(_eval_sets.is_sealed(p) for p in paths):
        print("[w_sweep] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    privacy = _eval_sets.floor(paths, legacy_public=args.legacy_public)
    if privacy == SEALED:
        print("[w_sweep] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    try:
        return run_sweep(
            persist_dir=args.persist_dir,
            ranks_out=args.ranks_out,
            privacy=privacy,
            legacy_public=args.legacy_public,
        )
    except SealedInputError:
        print("[w_sweep] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    except Exception as exc:  # noqa: BLE001 - private runs must not print str(exc)
        if privacy != PUBLIC:
            print(f"[w_sweep] error: {safe_error(exc)}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
