"""Phase 14 WS2.4 — W sweep over the intent-fusion weight.

Protocol (plan-gated): ONE cached expansion per golden+realistic answerable
question (live Haiku, cached to JSON so the fusion sweep is offline and
reproducible), then sweep W in {0, 0.25, 0.5} where W=0 is the same-expansion
baseline. Scoring replicates evaluate_retrieval exactly: first-rank strict
(literal section equality) and related (_sections_related dotted nesting),
hit@6. Selection rule: smallest W making S5 strict@6 HIT, subject to zero
golden-control regressions vs the W=0 arm and N4 staying HIT.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from src.generator import _sections_related  # noqa: E402
from src.query_rewrite import STATUS_LIVE, expand_query  # noqa: E402
from src.retriever import load_retrieval_context, retrieve  # noqa: E402

CACHE = os.path.join(REPO, "eval", "w_sweep_expansions_20260717.json")
S5_IDX = ("realistic", 4)   # "difference between a purchase and sale conveyance"
N4_IDX = ("realistic", 16)  # seller's-vs-buyer's-solicitor comparison (control)
WEIGHTS = [0.0, 0.25, 0.5]


def load_sets():
    sets = {}
    for label, path in [
        ("golden", "eval/golden_set.jsonl"),
        ("realistic", "eval/realistic_set.jsonl"),
    ]:
        rows = [json.loads(l) for l in open(path) if l.strip()]
        sets[label] = [r for r in rows if r["type"] != "refusal"]
    return sets


def build_cache(sets):
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
            for _attempt in range(3):  # zero-fallback requirement: retry twice
                exp = expand_query(q, enabled=True)
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


def first_ranks(expected, retrieved_sections):
    fs = fr = None
    for rank, sec in enumerate(retrieved_sections, 1):
        if fs is None and sec and any(e == sec for e in expected):
            fs = rank
        if fr is None and any(_sections_related(e, sec) for e in expected):
            fr = rank
        if fs is not None and fr is not None:
            break
    return fs, fr


def main():
    sets = load_sets()
    cache = build_cache(sets)
    non_live = {q[:60]: c["status"] for q, c in cache.items() if c["status"] != STATUS_LIVE}
    n_intent = sum(1 for c in cache.values() if c["intent"])
    print(f"expansions cached: {len(cache)}  non-live: {non_live or 0}  with-intent: {n_intent}")
    if non_live:
        print("FATAL: fallbacks present — sweep would not match canonical conditions")
        sys.exit(1)

    vs, bm = load_retrieval_context()
    ranks = {}  # (W, label, i) -> (first_strict, first_related)
    for W in WEIGHTS:
        for label, rows in sets.items():
            for i, r in enumerate(rows):
                c = cache[r["question"]]
                res = retrieve(
                    r["question"], top_k=6, vector_store=vs, bm25_index=bm,
                    mode="hybrid", strict_errors=True,
                    rewrites=c["rewrites"] or None,
                    intent_rewrite=c["intent"], intent_weight=W,
                )
                secs = [
                    str(x["document"].metadata.get("section_number", "")).strip()
                    for x in res
                ]
                expected = [str(s).strip() for s in r["expected_sections"]]
                ranks[(W, label, i)] = first_ranks(expected, secs)

    for W in WEIGHTS:
        print(f"\n=== W = {W} ===")
        for label, rows in sets.items():
            n = len(rows)
            s6 = sum(1 for i in range(n) if (ranks[(W, label, i)][0] or 99) <= 6)
            r6 = sum(1 for i in range(n) if (ranks[(W, label, i)][1] or 99) <= 6)
            print(f"  {label}: strict@6 {s6}/{n} ({s6/n:.3f})  related@6 {r6}/{n} ({r6/n:.3f})")
        s5 = ranks[(W, S5_IDX[0], S5_IDX[1])]
        n4 = ranks[(W, N4_IDX[0], N4_IDX[1])]
        print(f"  S5: strict_rank={s5[0]} related_rank={s5[1]}   N4: strict_rank={n4[0]} related_rank={n4[1]}")
        if W > 0.0:
            flips = [
                (label, i, rows[i]["question"][:70])
                for label, rows in sets.items()
                for i in range(len(rows))
                if (ranks[(0.0, label, i)][0] or 99) <= 6 and (ranks[(W, label, i)][0] or 99) > 6
            ]
            gains = [
                (label, i)
                for label, rows in sets.items()
                for i in range(len(rows))
                if (ranks[(0.0, label, i)][0] or 99) > 6 and (ranks[(W, label, i)][0] or 99) <= 6
            ]
            print(f"  strict flips HIT->MISS vs W=0: {flips or 'none'}")
            print(f"  strict gains MISS->HIT vs W=0: {gains or 'none'}")


if __name__ == "__main__":
    main()
