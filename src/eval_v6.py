"""Phase 16A-1 report v6: schema-2 sets scored in families (D66, item 3).

``run_eval_matrix`` switches to this path iff any input set is schema 2 (rows
carrying ``"schema": 2``); every set of that run is then loaded through
``src.eval_schema.load_any`` (v1 rows become one-OR-group, ``family_id = id``
rows) and scored here:

- **retrieval** per mode on ``answer`` + ``partial`` rows with
  ``src.eval_scoring.score_evidence`` (AND groups of OR sections; strict and
  related completion ranks; absorbed aliases share their chunk's rank);
- **scope** on every row with ``observed_scope`` (labelled 3 x observed 5) and
  ``score_partial`` for ``partial`` rows;
- **families**: a family's outcome is the ``all`` collapse of its phrasings
  (``src.eval_stats.collapse_family``; three paraphrases count once), and every
  family rate carries a Wilson 95% interval.

The report sections, in order: family counts with eligible denominators
(retrieval: ``answer`` + ``partial``; scope and refusal: all), scope
confusion, family rates, cohort blocks, expansion digest, run cost. Rows are
named only by their opaque ids. **No v6 run is canonical in 16A-1 or writes
``eval/results.md``** (P7 decides the canonical v6 run in 16A-2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.eval_scoring import (
    MODE_RELATED,
    MODE_STRICT,
    OBSERVED_SCOPES,
    PARTIAL_CORRECT,
    observed_scope,
    score_evidence,
    score_partial,
)
from src.eval_stats import UNAVAILABLE, collapse_family, wilson_interval

LABELLED_SCOPES = ("answer", "partial", "refuse")
FAMILY_RULE = "all"
REPORT_TITLE = "# Legal RAG Evaluation Report v6 (schema 2, families; not canonical in 16A-1)"


def _chunk_id(doc: Any) -> str:
    """The production chunk id of a retrieved Document (``doc.id``, else computed)."""
    if getattr(doc, "id", None):
        return str(doc.id)
    from src.embedder import compute_chunk_id

    return compute_chunk_id(doc.metadata.get("source", ""), doc.page_content)


def ranked_pairs(results: Sequence[Mapping[str, Any]], top_k: int) -> List[Tuple[str, str]]:
    """``retrieve()`` results -> ordered ``(chunk_id, section)`` pairs, clamped to top_k."""
    out = []
    for r in list(results)[:top_k]:
        doc = r["document"]
        out.append((_chunk_id(doc), str(doc.metadata.get("section_number", "")).strip()))
    return out


def pseudo_v1(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A v1-shaped entry for the shared generation pass (type drives include_types)."""
    return {"question": row["question"], "type": "refusal" if row["scope"] == "refuse" else "direct"}


def score_retrieval_v6(
    rows: Sequence[Mapping[str, Any]],
    retrieve_fn: Callable[..., List[Dict[str, Any]]],
    *,
    top_k: int,
    ks: Sequence[int],
    absorbed: Optional[Mapping[str, Sequence[str]]],
) -> Dict[str, Dict[str, Any]]:
    """Per-row strict/related evidence scores for one mode (answer + partial rows only)."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row["scope"] == "refuse":
            continue
        ranked = ranked_pairs(retrieve_fn(row["question"], top_k=top_k), top_k)
        strict = score_evidence(ranked, row["evidence"], MODE_STRICT, absorbed, ks=ks)
        related = score_evidence(ranked, row["evidence"], MODE_RELATED, absorbed, ks=ks)
        out[row["id"]] = {
            "strict_rank": strict["completion_rank"],
            "related_rank": related["completion_rank"],
            "strict_hit": strict["hit_at_k"],
            "related_hit": related["hit_at_k"],
            "groups_total": strict["groups_total"],
            "groups_covered_strict": strict["groups_covered"],
        }
    return out


def score_answers_v6(
    rows: Sequence[Mapping[str, Any]], answers: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Per-row observed scope and (partial rows) PARTIAL correctness."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cached = answers.get(row["question"])
        if cached is None:
            continue
        result = cached.get("result")
        if result is not None and "generation_status" not in result:
            result = {**result, "generation_status": cached.get("generation_status")}
        obs = observed_scope(result)
        entry: Dict[str, Any] = {"observed": obs}
        if row["scope"] == "partial":
            entry["partial"] = score_partial(result, row["evidence"], row["gaps"])
        out[row["id"]] = entry
    return out


def _families(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    fams: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        fams.setdefault(row["family_id"], []).append(row)
    return fams


def _rate(hits: int, n: int) -> Dict[str, Any]:
    ci = wilson_interval(hits, n)
    return {"hits": hits, "n": n, "ci": None if ci == UNAVAILABLE else list(ci)}


def family_rates(
    rows: Sequence[Mapping[str, Any]],
    retrieval: Mapping[str, Mapping[str, Mapping[str, Any]]],
    answers: Optional[Mapping[str, Mapping[str, Any]]],
    *,
    k: int,
) -> Dict[str, Any]:
    """Family-collapsed rates (``all`` rule) with Wilson intervals."""
    fams = _families(rows)
    out: Dict[str, Any] = {"retrieval": {}, "rule": FAMILY_RULE, "k": k}
    eligible = {f: rs for f, rs in fams.items() if all(r["scope"] != "refuse" for r in rs)}
    for mode, per_row in retrieval.items():
        out["retrieval"][mode] = {}
        for match in ("strict", "related"):
            hits = sum(
                1
                for rs in eligible.values()
                if collapse_family([bool(per_row[r["id"]][f"{match}_hit"].get(k)) for r in rs], FAMILY_RULE)
            )
            out["retrieval"][mode][match] = _rate(hits, len(eligible))
    if answers is not None:
        scored = {f: rs for f, rs in fams.items() if all(r["id"] in answers for r in rs)}
        scope_hits = 0
        for rs in scored.values():
            outcomes = []
            for r in rs:
                a = answers[r["id"]]
                if r["scope"] == "partial":
                    outcomes.append(a["partial"]["status"] == PARTIAL_CORRECT)
                else:
                    outcomes.append(a["observed"] == r["scope"])
            scope_hits += int(collapse_family(outcomes, FAMILY_RULE))
        out["scope"] = _rate(scope_hits, len(scored))
        refuse = {f: rs for f, rs in scored.items() if all(r["scope"] == "refuse" for r in rs)}
        out["refusal"] = _rate(
            sum(int(collapse_family([answers[r["id"]]["observed"] == "refuse" for r in rs], FAMILY_RULE)) for rs in refuse.values()),
            len(refuse),
        )
        partial = {f: rs for f, rs in scored.items() if all(r["scope"] == "partial" for r in rs)}
        out["partial"] = _rate(
            sum(
                int(collapse_family([answers[r["id"]]["partial"]["status"] == PARTIAL_CORRECT for r in rs], FAMILY_RULE))
                for rs in partial.values()
            ),
            len(partial),
        )
    return out


def scope_confusion(
    rows: Sequence[Mapping[str, Any]], answers: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Dict[str, int]]:
    """Row counts, labelled scope (3) x observed scope (5)."""
    table = {lab: {obs: 0 for obs in OBSERVED_SCOPES} for lab in LABELLED_SCOPES}
    for row in rows:
        a = answers.get(row["id"])
        if a is not None:
            table[row["scope"]][a["observed"]] += 1
    return table


def _fmt_rate(rate: Mapping[str, Any]) -> str:
    n = rate["n"]
    if not n:
        return "n/a (0 families)"
    ci = rate["ci"]
    return f"{rate['hits']}/{n} = {rate['hits'] / n:.3f} (95% Wilson CI {ci[0]:.3f}-{ci[1]:.3f})"


def format_v6_report(result: Mapping[str, Any]) -> str:
    """Render report v6 (ids only, never question or answer text)."""
    prov = result["provenance"]
    lines: List[str] = [REPORT_TITLE, ""]
    lines.append(f"- Date: {datetime.now().isoformat()}")
    lines.append(f"- top_k: {result['top_k']}; hit cut-off k = {result['k']}")
    lines.append(f"- Retrieval modes: {', '.join(result['modes'])}")
    lines.append(f"- Privacy: {result['privacy']}")
    lines.append("- Canonical run: False (no v6 run is canonical in 16A-1)")
    lines.append(
        f"- git sha: {prov.get('git_sha')}; embedding model: {prov.get('embedding_model')}; "
        f"generation model: {prov.get('generation_model')}"
    )
    lines.append(f"- family outcome rule: {FAMILY_RULE} (three paraphrases count once)")
    lines.append("")

    lines.append("## Family counts")
    lines.append("")
    lines.append("| Set | Rows | Families | Retrieval-eligible families (answer+partial) | Scope/refusal families (all) |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in result["sets"]:
        c = s["counts"]
        lines.append(f"| {s['label']} | {c['rows']} | {c['families']} | {c['retrieval_families']} | {c['families']} |")
    lines.append("")

    lines.append("## Scope confusion (rows; labelled x observed)")
    lines.append("")
    for s in result["sets"]:
        lines.append(f"### {s['label']}")
        lines.append("")
        if s["confusion"] is None:
            lines.append("Answer passes skipped: no scope confusion.")
            lines.append("")
            continue
        lines.append("| Labelled \\ Observed | " + " | ".join(OBSERVED_SCOPES) + " |")
        lines.append("| --- | " + " | ".join(["---"] * len(OBSERVED_SCOPES)) + " |")
        for lab in LABELLED_SCOPES:
            row = s["confusion"][lab]
            lines.append(f"| {lab} | " + " | ".join(str(row[o]) for o in OBSERVED_SCOPES) + " |")
        lines.append("")

    lines.append("## Family rates")
    lines.append("")
    for s in result["sets"]:
        fr = s["family_rates"]
        lines.append(f"### {s['label']}")
        lines.append("")
        for mode, by in fr["retrieval"].items():
            lines.append(f"- retrieval {mode} strict@{fr['k']}: {_fmt_rate(by['strict'])}")
            lines.append(f"- retrieval {mode} related@{fr['k']}: {_fmt_rate(by['related'])}")
        for key, name in (("scope", "scope correct"), ("refusal", "refuse families refused"), ("partial", "partial families correct")):
            if key in fr:
                lines.append(f"- {name}: {_fmt_rate(fr[key])}")
        lines.append("")

    lines.append("## Cohort")
    lines.append("")
    for s in result["sets"]:
        c = s["cohort"]
        lines.append(f"- {s['label']}: path {c['path']}; privacy {c['privacy']}; schema {c['schema']}; "
                     f"sha256 {c['sha256']}; rows {c['rows']}; families {c['families']}; cohort_fp {c['cohort_fp']}")
    lines.append(f"- absorbed map sha256: {result.get('absorbed_map_sha256')}")
    lines.append("")

    lines.append("## Expansion")
    lines.append("")
    ident = result.get("expansion_identity") or {}
    lines.append(
        f"- {ident.get('kind')}: model {ident.get('model')}; digest {ident.get('digest')}; "
        f"rewrite_live {result.get('rewrite_live')}, rewrite_replayed {result.get('rewrite_replayed')}"
    )
    lines.append("")

    lines.append("## Run cost")
    lines.append("")
    cost = result.get("run_cost")
    if cost is None:
        lines.append("- no spend meter (offline run)")
    else:
        lines.append(f"- run EUR {cost['run_eur']:.4f}; week EUR {cost['week_eur']:.4f}")
    lines.append("")

    lines.append("## Per-row detail (ids only)")
    lines.append("")
    for s in result["sets"]:
        lines.append(f"### {s['label']}")
        lines.append("")
        for row in s["rows"]:
            parts = [f"- {row['id']} [{row['scope']}] family={row['family_id']}"]
            for mode in result["modes"]:
                r = s["retrieval"][mode].get(row["id"])
                if r is not None:
                    parts.append(f"{mode}: strict={r['strict_rank']} related={r['related_rank']}")
            a = (s["answers"] or {}).get(row["id"])
            if a is not None:
                parts.append(f"observed={a['observed']}")
                if "partial" in a:
                    parts.append(f"partial={a['partial']['status']}")
            if result["privacy"] != "public":
                parts.append(f"evidence={row['evidence']}")
            lines.append("; ".join(parts))
        lines.append("")
    return "\n".join(lines) + "\n"
