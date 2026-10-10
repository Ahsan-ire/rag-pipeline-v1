"""Acceptance (h) for ``src.eval_split.split_families`` (Phase 16A-1 item 4).

Synthetic data only: every pool is generated in code from a seed. A pool is
built from a hidden "true" assignment that satisfies every constraint, so the
solver always has at least one feasible answer; the tests then check the
solver's own output with an independent checker written here.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Tuple

import pytest

from src.eval_split import (
    CONTENT_KEYS,
    SplitInfeasible,
    SplitInputError,
    families_digest,
    split_families,
    stratum_of,
)

CHAPTERS = ["1", "2", "3", "4", "5", "multi"]
SOURCES = ["drafted", "tutorial", "colleague"]
REGISTERS = ["lay", "expert"]


# --------------------------------------------------------------------------- helpers


def _band_ok(r: int, q: int) -> bool:
    return 4 * r >= q and 10 * r <= 3 * q


def _band_range(q: int) -> Tuple[int, int]:
    lo = -(-q // 4)
    hi = (3 * q) // 10
    return lo, hi


def make_twins(
    ids: List[str], batches: Dict[str, List[str]], edges: List[List[str]]
) -> Dict[str, Any]:
    """Build a valid twins record for ``batches`` and ``edges``."""
    bids = sorted(batches)
    pairs = [[bids[i], bids[j]] for i in range(len(bids)) for j in range(i, len(bids))]
    return {
        "batches": {b: list(v) for b, v in batches.items()},
        "families_digest": families_digest(ids, bids),
        "pairs_covered": pairs,
        "edges": [list(e) for e in edges],
    }


def make_pool(seed: int) -> Dict[str, Any]:
    """Generate a feasible synthetic pool plus its arguments from ``seed``."""
    rng = random.Random(seed)
    splits = ["dev", "test", "sealed_x"][: rng.choice([2, 3])]
    quotas: Dict[str, int] = {}
    for s in splits:
        while True:
            q = rng.randint(14, 40)
            lo, hi = _band_range(q)
            if lo <= hi:
                quotas[s] = q
                break
    families: List[Dict[str, Any]] = []
    truth: Dict[str, str] = {}
    n = 0
    for s in splits:
        q = quotas[s]
        lo, hi = _band_range(q)
        r = rng.randint(lo, hi)
        for k in range(q):
            n += 1
            fid = f"f{rng.randint(0, 10**6):07d}_{n:04d}"
            scope = "refuse" if k < r else rng.choice(["answer", "answer", "partial"])
            chapter = "refuse" if scope == "refuse" else rng.choice(CHAPTERS)
            eligible = [s] + [t for t in splits if t != s and rng.random() < 0.6]
            rng.shuffle(eligible)
            families.append(
                {
                    "family_id": fid,
                    "scope": scope,
                    "chapter": chapter,
                    "source": rng.choice(SOURCES),
                    "register": rng.choice(REGISTERS),
                    "eligible": eligible,
                }
            )
            truth[fid] = s
    ids = sorted(truth)
    nb = rng.randint(1, 4)
    bids = [f"b{i}" for i in range(nb)]
    batches: Dict[str, List[str]] = {b: [] for b in bids}
    for i, fid in enumerate(rng.sample(ids, len(ids))):
        batches[bids[i % nb]].append(fid)
    batch_of = {f: b for b, fs in batches.items() for f in fs}
    # Edges only between truly co-split families (so the truth stays feasible);
    # include cross-batch edges whenever there is more than one batch.
    edges: List[List[str]] = []
    by_split: Dict[str, List[str]] = {}
    for f, s in truth.items():
        by_split.setdefault(s, []).append(f)
    for s, fs in by_split.items():
        fs = sorted(fs)
        for _ in range(rng.randint(1, max(1, len(fs) // 6))):
            a, b = rng.sample(fs, 2)
            edges.append([a, b])
        if nb > 1:
            cross = [(a, b) for a in fs for b in fs if batch_of[a] != batch_of[b]]
            if cross:
                a, b = rng.choice(cross)
                edges.append([a, b])
    pre_ids = rng.sample(ids, rng.randint(0, max(1, len(ids) // 15)))
    preassigned = {f: truth[f] for f in pre_ids}
    # Strata and min_share at about half of what the truth achieves.
    strata: Dict[str, Dict[str, int]] = {}
    min_share: Dict[str, Dict[str, float]] = {}
    for s in splits:
        fams = [f for f in ids if truth[f] == s]
        rec = {r["family_id"]: r for r in families}
        sc: Dict[str, int] = {}
        oc: Dict[str, int] = {}
        for f in fams:
            sc[stratum_of(rec[f])] = sc.get(stratum_of(rec[f]), 0) + 1
            oc[rec[f]["source"]] = oc.get(rec[f]["source"], 0) + 1
        keys = rng.sample(sorted(sc), min(3, len(sc)))
        strata[s] = {k: sc[k] // 2 for k in keys}
        src = rng.choice(sorted(oc))
        min_share[s] = {src: int(50 * oc[src] / quotas[s]) / 100}
    rng.shuffle(families)
    return {
        "families": families,
        "quotas": quotas,
        "strata": strata,
        "twins": make_twins(ids, batches, edges),
        "preassigned": preassigned,
        "min_share": min_share,
        "truth": truth,
    }


def call(pool: Dict[str, Any], seed: int = 7, **over: Any) -> Dict[str, str]:
    """Call ``split_families`` with a pool's arguments, allowing overrides."""
    args = {
        "families": pool["families"],
        "quotas": pool["quotas"],
        "strata": pool["strata"],
        "twins": pool["twins"],
        "preassigned": pool["preassigned"],
        "min_share": pool["min_share"],
    }
    args.update(over)
    return split_families(
        args["families"],
        seed,
        args["quotas"],
        args["strata"],
        twins=args["twins"],
        preassigned=args["preassigned"],
        min_share=args["min_share"],
    )


def components(ids: List[str], edges: List[List[str]]) -> List[List[str]]:
    """Connected components of the twin graph (independent of the module)."""
    adj: Dict[str, set] = {f: set() for f in ids}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    seen: set = set()
    out = []
    for f in ids:
        if f in seen:
            continue
        stack, comp = [f], []
        seen.add(f)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        out.append(comp)
    return out


def check(pool: Dict[str, Any], out: Dict[str, str]) -> None:
    """Assert every (h) property of ``out`` independently of the module."""
    rec = {r["family_id"]: r for r in pool["families"]}
    assert set(out) == set(rec)
    # no ineligible placement
    for f, s in out.items():
        assert s in rec[f]["eligible"]
    # twin edges (cross-batch included) share a split; no unit spans splits
    for a, b in pool["twins"]["edges"]:
        assert out[a] == out[b]
    for comp in components(sorted(rec), pool["twins"]["edges"]):
        assert len({out[f] for f in comp}) == 1
    # preassigned never move
    for f, s in dict(pool["preassigned"]).items():
        assert out[f] == s
    for s, q in pool["quotas"].items():
        fams = [f for f in out if out[f] == s]
        assert len(fams) == q
        r = sum(1 for f in fams if rec[f]["scope"] == "refuse")
        assert _band_ok(r, q), (s, r, q)
        for key, m in pool["strata"].get(s, {}).items():
            assert sum(1 for f in fams if stratum_of(rec[f]) == key) >= m
        for src, share in (pool["min_share"] or {}).get(s, {}).items():
            got = sum(1 for f in fams if rec[f]["source"] == src)
            assert got * 100 >= round(share * 100) * q, (s, src, got, q, share)


def rec(fid: str, scope: str = "answer", chapter: str = "1", eligible=("a", "b"), source: str = "drafted"):
    """A minimal valid family record."""
    if scope == "refuse":
        chapter = "refuse"
    return {
        "family_id": fid,
        "scope": scope,
        "chapter": chapter,
        "source": source,
        "register": "lay",
        "eligible": list(eligible),
    }


def small_pool() -> Dict[str, Any]:
    """Eight families, quotas 4/4, two refusals; feasible."""
    fams = [
        rec("f1", "refuse"),
        rec("f2", "refuse"),
        rec("f3"),
        rec("f4"),
        rec("f5", "partial"),
        rec("f6"),
        rec("f7"),
        rec("f8", chapter="multi"),
    ]
    ids = [f["family_id"] for f in fams]
    return {
        "families": fams,
        "quotas": {"a": 4, "b": 4},
        "strata": {},
        "twins": make_twins(ids, {"x": ids[:4], "y": ids[4:]}, [["f3", "f6"]]),
        "preassigned": {},
        "min_share": None,
    }


# --------------------------------------------------------------------------- property loop

POOL_SEEDS = list(range(200))


@pytest.mark.parametrize("pool_seed", POOL_SEEDS)
def test_property_pool(pool_seed: int) -> None:
    """200 seeded pools: every (h) property holds, plus seed/permutation invariance."""
    pool = make_pool(pool_seed)
    out = call(pool, seed=pool_seed)
    check(pool, out)

    # Same seed + permuted input order (families, eligible lists, batches,
    # edges, pairs, preassigned) -> identical output.
    rng = random.Random(10_000 + pool_seed)
    fams = [dict(f, eligible=rng.sample(f["eligible"], len(f["eligible"]))) for f in pool["families"]]
    rng.shuffle(fams)
    tw = pool["twins"]
    bitems = list(tw["batches"].items())
    rng.shuffle(bitems)
    batches = {b: rng.sample(v, len(v)) for b, v in bitems}
    edges = [list(reversed(e)) if rng.random() < 0.5 else list(e) for e in tw["edges"]]
    rng.shuffle(edges)
    pairs = [list(reversed(p)) if rng.random() < 0.5 else list(p) for p in tw["pairs_covered"]]
    rng.shuffle(pairs)
    twins2 = {"batches": batches, "families_digest": tw["families_digest"], "pairs_covered": pairs, "edges": edges}
    pre2 = list(pool["preassigned"].items())
    rng.shuffle(pre2)
    quotas2 = dict(reversed(list(pool["quotas"].items())))
    out2 = call(pool, seed=pool_seed, families=fams, twins=twins2, preassigned=pre2, quotas=quotas2)
    assert out2 == out
    assert call(pool, seed=pool_seed) == out


def test_seed_changes_output_somewhere() -> None:
    """The seed actually drives the shuffle (different seeds differ on some pool)."""
    pool = make_pool(3)
    outs = {tuple(sorted(call(pool, seed=s).items())) for s in range(8)}
    assert len(outs) > 1


# --------------------------------------------------------------------------- units / twins


def test_cross_batch_twins_share_split() -> None:
    """A cross-batch edge chain merges families into one unit in one split."""
    p = small_pool()
    ids = [f["family_id"] for f in p["families"]]
    p["twins"] = make_twins(ids, {"x": ids[:4], "y": ids[4:]}, [["f1", "f5"], ["f5", "f3"]])
    for seed in range(20):
        out = call(p, seed=seed)
        assert out["f1"] == out["f5"] == out["f3"]


def test_unit_eligibility_is_intersection() -> None:
    """A unit may go only where every member is eligible."""
    p = small_pool()
    p["families"][2] = rec("f3", eligible=("a",))
    for seed in range(20):
        out = call(p, seed=seed)
        assert out["f3"] == out["f6"] == "a"


def _bad_twins(p: Dict[str, Any], **patch: Any) -> Dict[str, Any]:
    t = dict(p["twins"])
    t.update(patch)
    return t


@pytest.mark.parametrize(
    "case",
    [
        "missing_pair",
        "missing_self_pair",
        "digest_mismatch",
        "empty_batches",
        "empty_batch",
        "partial_membership",
        "duplicate_across",
        "duplicate_within",
        "unknown_in_batch",
        "unknown_edge_endpoint",
        "unknown_pair_batch",
        "repeated_pair",
        "extra_key",
        "missing_key",
    ],
)
def test_twins_refusals(case: str) -> None:
    """Every malformed twins record is refused with SplitInputError."""
    p = small_pool()
    ids = [f["family_id"] for f in p["families"]]
    t = p["twins"]
    if case == "missing_pair":
        tw = _bad_twins(p, pairs_covered=[pp for pp in t["pairs_covered"] if pp != ["x", "y"]])
    elif case == "missing_self_pair":
        tw = _bad_twins(p, pairs_covered=[pp for pp in t["pairs_covered"] if pp != ["y", "y"]])
    elif case == "digest_mismatch":
        tw = _bad_twins(p, families_digest=families_digest(ids + ["f9"], ["x", "y"]))
    elif case == "empty_batches":
        tw = _bad_twins(p, batches={})
    elif case == "empty_batch":
        tw = make_twins(ids, {"x": ids[:4], "y": ids[4:], "z": []}, [])
    elif case == "partial_membership":
        tw = make_twins(ids, {"x": ids[:4], "y": ids[4:7]}, [])
    elif case == "duplicate_across":
        tw = make_twins(ids, {"x": ids[:5], "y": ids[4:]}, [])
    elif case == "duplicate_within":
        tw = make_twins(ids, {"x": ids[:4] + ["f1"], "y": ids[4:]}, [])
    elif case == "unknown_in_batch":
        tw = make_twins(ids, {"x": ids[:4] + ["f9"], "y": ids[4:]}, [])
    elif case == "unknown_edge_endpoint":
        tw = _bad_twins(p, edges=[["f1", "f9"]])
    elif case == "unknown_pair_batch":
        tw = _bad_twins(p, pairs_covered=t["pairs_covered"] + [["x", "q"]])
    elif case == "repeated_pair":
        tw = _bad_twins(p, pairs_covered=t["pairs_covered"] + [["y", "x"]])
    elif case == "extra_key":
        tw = _bad_twins(p, notes="x")
    else:
        tw = {k: v for k, v in t.items() if k != "edges"}
    with pytest.raises(SplitInputError):
        call(p, twins=tw)


def test_families_digest_is_order_free_and_canonical() -> None:
    """The digest sorts both id lists; it is lowercase sha256 hex."""
    d = families_digest(["b", "a"], ["y", "x"])
    assert d == families_digest(["a", "b"], ["x", "y"])
    assert len(d) == 64 and d == d.lower()
    assert d != families_digest(["a", "b"], ["x"])


# --------------------------------------------------------------------------- preassigned


def test_unknown_preassigned_id_refused() -> None:
    p = small_pool()
    with pytest.raises(SplitInputError):
        call(p, preassigned={"f9": "a"})


def test_unknown_preassigned_split_and_repeat_refused() -> None:
    p = small_pool()
    with pytest.raises(SplitInputError):
        call(p, preassigned={"f1": "zz"})
    with pytest.raises(SplitInputError):
        call(p, preassigned=[("f1", "a"), ("f1", "a")])


def test_conflicting_preassignments_in_unit_infeasible() -> None:
    p = small_pool()
    with pytest.raises(SplitInfeasible) as ei:
        call(p, preassigned={"f3": "a", "f6": "b"})
    c = ei.value.counts
    assert c["preassignment_conflicts"] == {"units": 1, "violated": True}


def test_preassigned_never_move_and_pull_unit() -> None:
    p = small_pool()
    for seed in range(20):
        out = call(p, seed=seed, preassigned={"f6": "b", "f1": "a"})
        assert out["f6"] == "b" and out["f3"] == "b" and out["f1"] == "a"


def test_preassigned_count_toward_band() -> None:
    """Quota 4 allows exactly one refusal: a preassigned refusal fills it."""
    p = small_pool()
    for seed in range(20):
        out = call(p, seed=seed, preassigned={"f1": "a"})
        assert out["f1"] == "a" and out["f2"] == "b"
    with pytest.raises(SplitInfeasible) as ei:
        call(p, preassigned={"f1": "a", "f2": "a"})
    band = ei.value.counts["band"]["a"]
    assert band["refuse_preassigned"] == 2 and band["refuse_max"] == 1 and band["violated"]


def test_preassigned_count_toward_quota() -> None:
    p = small_pool()
    pre = {f: "a" for f in ["f3", "f4", "f5", "f7", "f8"]}
    with pytest.raises(SplitInfeasible) as ei:
        call(p, preassigned=pre)
    e = ei.value.counts["eligibility"]["a"]
    assert e["preassigned"] == 6 and e["quota"] == 4 and e["violated"]  # f6 rides with f3


def test_preassigned_count_toward_strata_and_min_share() -> None:
    """Only preassigned families can satisfy a's minimums; they do, and still count."""
    p = small_pool()
    fams = [dict(f) for f in p["families"]]
    fams[7] = rec("f8", chapter="5", source="colleague")  # the only 'answer|5' / colleague
    out = call(
        p,
        families=fams,
        strata={"a": {"answer|5": 1}},
        min_share={"a": {"colleague": 0.25}},
        preassigned={"f8": "a"},
    )
    assert out["f8"] == "a"
    # Pinned elsewhere, the same minimum is infeasible: the counts show it.
    with pytest.raises(SplitInfeasible) as ei:
        call(p, families=fams, strata={"a": {"answer|5": 1}}, preassigned={"f8": "b"})
    assert ei.value.counts["strata"]["a"]["answer|5"] == {"required": 1, "available": 0, "violated": True}
    with pytest.raises(SplitInfeasible) as ei:
        call(p, families=fams, min_share={"a": {"colleague": 0.25}}, preassigned={"f8": "b"})
    assert ei.value.counts["min_share"]["a"]["colleague"] == {"required": 1, "available": 0, "violated": True}


def test_preassigned_to_ineligible_split_infeasible() -> None:
    p = small_pool()
    p["families"][0] = rec("f1", "refuse", eligible=("b",))
    with pytest.raises(SplitInfeasible) as ei:
        call(p, preassigned={"f1": "a"})
    assert ei.value.counts["preassigned_ineligible"]["units"] == 1


# --------------------------------------------------------------------------- infeasible -> counts


def test_infeasible_quota_total() -> None:
    p = small_pool()
    with pytest.raises(SplitInfeasible) as ei:
        call(p, quotas={"a": 4, "b": 5})
    assert ei.value.counts["quota_total"] == {"families": 8, "quota_sum": 9, "violated": True}


def test_infeasible_band_no_refusals() -> None:
    p = small_pool()
    fams = [rec(f["family_id"]) for f in p["families"]]
    with pytest.raises(SplitInfeasible) as ei:
        call(p, families=fams)
    b = ei.value.counts["band"]["a"]
    assert b["refuse_available"] == 0 and b["refuse_min"] == 1 and b["violated"]


def test_infeasible_eligibility_counts() -> None:
    p = small_pool()
    fams = [rec(f["family_id"], f["scope"], f["chapter"], eligible=("b",)) for f in p["families"]]
    with pytest.raises(SplitInfeasible) as ei:
        call(p, families=fams)
    assert ei.value.counts["eligibility"]["a"]["eligible_families"] == 0
    assert ei.value.counts["eligibility"]["a"]["violated"]


def test_infeasible_by_search_reports_counts_and_no_output() -> None:
    """Statically fine, but one 8-family unit cannot fit a quota of 4."""
    p = small_pool()
    ids = [f["family_id"] for f in p["families"]]
    chain = [[ids[i], ids[i + 1]] for i in range(len(ids) - 1)]
    with pytest.raises(SplitInfeasible) as ei:
        call(p, twins=make_twins(ids, {"x": ids[:4], "y": ids[4:]}, chain))
    c = ei.value.counts
    assert c["search"]["violated"] and not c["search"]["exhausted"]
    assert not c["band"]["a"]["violated"] and not c["eligibility"]["a"]["violated"]


def test_infeasible_counts_on_generated_pool() -> None:
    """Raising a stratum minimum beyond supply gives that stratum's counts."""
    pool = make_pool(11)
    s = sorted(pool["quotas"])[0]
    strata = {k: dict(v) for k, v in pool["strata"].items()}
    strata[s]["answer|nope"] = 1
    with pytest.raises(SplitInfeasible) as ei:
        call(pool, strata=strata)
    assert ei.value.counts["strata"][s]["answer|nope"] == {"required": 1, "available": 0, "violated": True}


def test_min_share_float_is_exact() -> None:
    """0.25 of a quota of 4 needs exactly 1 family, not 2 from float noise."""
    p = small_pool()
    fams = [dict(f) for f in p["families"]]
    fams[7] = rec("f8", chapter="multi", source="colleague")
    out = call(p, families=fams, min_share={"a": {"colleague": 0.25}, "b": {"drafted": 0.1}})
    assert out["f8"] == "a"


# --------------------------------------------------------------------------- record refusals


@pytest.mark.parametrize("key", sorted(CONTENT_KEYS))
def test_content_bearing_record_refused(key: str) -> None:
    p = small_pool()
    fams = [dict(f) for f in p["families"]]
    fams[3][key] = "SECRET-CONTENT"
    with pytest.raises(SplitInputError) as ei:
        call(p, families=fams)
    assert "content-bearing" in str(ei.value)
    assert "SECRET-CONTENT" not in str(ei.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(notes="x"),  # unknown key
        lambda r: r.pop("register"),  # missing key
        lambda r: r.update(scope="maybe"),
        lambda r: r.update(scope="refuse", chapter="3"),
        lambda r: r.update(chapter="refuse"),
        lambda r: r.update(eligible=["a", "a"]),
        lambda r: r.update(eligible=["zz"]),
        lambda r: r.update(eligible="a"),
        lambda r: r.update(family_id=""),
        lambda r: r.update(family_id="f1"),  # duplicate id
        lambda r: r.update(source=""),
    ],
)
def test_malformed_record_refused(mutate) -> None:
    p = small_pool()
    fams = [dict(f) for f in p["families"]]
    mutate(fams[3])
    with pytest.raises(SplitInputError):
        call(p, families=fams)


def test_bad_parameters_refused() -> None:
    p = small_pool()
    with pytest.raises(SplitInputError):
        call(p, families=[])
    with pytest.raises(SplitInputError):
        call(p, strata={"zz": {}})
    with pytest.raises(SplitInputError):
        call(p, strata={"a": {"answer": 1}})
    with pytest.raises(SplitInputError):
        call(p, strata={"a": {"refuse|3": 1}})
    with pytest.raises(SplitInputError):
        call(p, min_share={"a": {"drafted": 1.5}})
    with pytest.raises(SplitInputError):
        call(p, min_share={"zz": {"drafted": 0.1}})
    with pytest.raises(SplitInputError):
        call(p, quotas={})
    with pytest.raises(SplitInputError):
        split_families(p["families"], "7", p["quotas"], {}, twins=p["twins"])  # type: ignore[arg-type]


def test_returns_only_family_ids_and_split_names() -> None:
    p = small_pool()
    out = call(p)
    assert list(out) == sorted(out)
    assert set(out.values()) <= {"a", "b"}


def test_budget_exhaustion_raises_not_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running out of search budget is SplitInfeasible (exhausted), never a partial split."""
    import src.eval_split as es

    monkeypatch.setattr(es, "SEARCH_NODE_BUDGET", 1)
    pool = make_pool(5)
    with pytest.raises(SplitInfeasible) as ei:
        call(pool)
    assert ei.value.counts["search"]["exhausted"] is True
