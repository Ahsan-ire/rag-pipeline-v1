"""What a sealed slot run stores and what is decided from it (16A-2b b1a r5, D74).

These functions compute or project values that the stored results document and the adoption analysis use: a row's
strict and related hit at the cut-off, the baseline's per-mode family hits, the document's sets, roles, floor and groups,
the canonical-v6 guards, the eligible-family counts that the floor is judged on, and the groups an exclusion removes
from analysis. They were in ``src.sealed_run`` (the slot protocol: gates, preflight, claim, storage and publication), which
the r5 incumbent identity left out; since r6 the protocol module is hashed too, and the computations stay here so that the
scorer's identity (``src.scoring_identity``) can cover them without covering the protocol. Editing any of them changes
``pipeline_config_hash`` and ``scorer_identity``. ``src.sealed_run`` imports them (and re-exports their old names), so no
caller changes and the behaviour is the one it had.

16A-2b b1a r6 moved the rest of the metric computations here from src.sealed_report (which now only renders): the views
over the stored document (the sealed set, the dropped families, the kept rows, the eligible families, the cut-off check),
the family-level strict hits, the paired baseline filter, the confirmatory test statistics and the strata counts. The scorer's
identity (src.scoring_identity.scorer_identity) covers this module, not the renderer.

This module imports no protocol code (no ``GateRefused``, no token, no ledger writes): the document builder reads the
token by attribute and is given the one gate it needs (``dev_sets_of``) by ``src.sealed_run.build_results_doc``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from src.eval_cohort import SCORER_VERSION
from src.eval_stats import (
    UNAVAILABLE,
    cluster_signflip_p,
    collapse_family,
    durkalski_chi2,
    family_cluster,
    mcnemar_exact,
)

RESULTS_KIND = "sealed_slot_results"
RESULTS_VERSION = 1
FAMILY_RULE = "all"
DESCRIPTIVE_SLOTS = ("0", "0b")

#: The canonical-v6 guards, in report order. Each is a pure function of the stored facts.
GUARDS = (
    "heldout_frozen_sealed_set",
    "heldout_answerable",
    "realistic_answerable",
    "all_modes",
    "distinct_sets",
    "refusals_scored",
    "completeness_scored",
    "top_k_6",
    "no_generation_errors",
    "no_incomplete_generations",
    "no_unknown_status",
    "expansion_attempted",
    "no_expansion_fallbacks",
    "bm25_loaded",
    "judge_clean",
    "slot_0_or_0b",
    "bound_records",
    "live_expansion",
    "tracing_off",
)


# ---------------------------------------------------------------------------
# Views over the stored document and the metrics computed from them (moved from src.sealed_report, r6)
# ---------------------------------------------------------------------------


def is_confirmatory(slot: str) -> bool:
    """Slots other than ``0`` and ``0b`` are confirmatory (1-4 in the prereg)."""
    return slot not in DESCRIPTIVE_SLOTS


def check_cutoff(doc: Mapping[str, Any]) -> None:
    """Refuse a stored document whose hit cut-off is unrecorded, is not its ``k``, or differs from its baseline's.

    A baseline scored at another cut-off has other hits (a section at rank 4 is a hit at 6 and a miss at
    3), so pairing it with this run would manufacture gains or losses from the cut-off alone.

    Raises:
        ValueError: any of the three.
    """
    cutoff = doc.get("cutoff")
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff != doc["k"]:
        raise ValueError("the stored run does not record the hit cut-off it was scored at")
    base = doc.get("baseline")
    if base and base.get("cutoff") != cutoff:
        raise ValueError("the baseline and the candidate were scored at different hit cut-offs")


def sealed_set(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one set of the stored document with role ``sealed``."""
    found = [s for s in doc["sets"] if s["role"] == "sealed"]
    if len(found) != 1:
        raise ValueError("a slot results document holds exactly one sealed set")
    return found[0]


def dropped_families(doc: Mapping[str, Any], excluded: FrozenSet[str]) -> FrozenSet[str]:
    """The families of the sealed set that analysis drops (those in an excluded group)."""
    return frozenset(f for f, g in doc["groups"].items() if g in excluded)


def kept_rows(set_doc: Mapping[str, Any], drop: FrozenSet[str]) -> List[Mapping[str, Any]]:
    """The set's rows whose family is not dropped."""
    return [r for r in set_doc["rows"] if r["family_id"] not in drop]


def eligible_families(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """Families whose rows are all answer or partial (retrieval-eligible), sorted."""
    fams: Dict[str, bool] = {}
    for r in rows:
        fams[r["family_id"]] = fams.get(r["family_id"], True) and r["scope"] != "refuse"
    return sorted(f for f, ok in fams.items() if ok)


def family_hits(rows: Sequence[Mapping[str, Any]], mode: str) -> Dict[str, bool]:
    """Family-level strict hit@k of ``mode`` over eligible families (``all`` collapse; a tie is a miss)."""
    by_family: Dict[str, List[bool]] = {}
    for r in rows:
        if r["scope"] != "refuse":
            by_family.setdefault(r["family_id"], []).append(bool(r["retrieval"][mode]["strict_hit"]))
    return {f: collapse_family(outcomes, FAMILY_RULE) for f, outcomes in by_family.items()}


def stored_shapes(rows: Sequence[Mapping[str, Any]], modes: Sequence[str], k: int, answers_ran: bool):
    """Rebuild the ``eval_v6.family_rates`` input shapes from stored rows."""
    plain = [{"id": r["id"], "family_id": r["family_id"], "scope": r["scope"]} for r in rows]
    retrieval: Dict[str, Dict[str, Dict[str, Any]]] = {m: {} for m in modes}
    for r in rows:
        if r["scope"] == "refuse":
            continue
        for m in modes:
            cell = r["retrieval"][m]
            retrieval[m][r["id"]] = {
                "strict_hit": {k: bool(cell["strict_hit"])},
                "related_hit": {k: bool(cell["related_hit"])},
            }
    answers: Optional[Dict[str, Dict[str, Any]]] = None
    if answers_ran:
        answers = {}
        for r in rows:
            a = r["answer"]
            if a is None:
                continue
            entry: Dict[str, Any] = {"observed": a["observed"]}
            if r["scope"] == "partial":
                entry["partial"] = {"status": a["partial"]}
            answers[r["id"]] = entry
    return plain, retrieval, answers


def row_scope_correct(r: Mapping[str, Any]) -> Optional[bool]:
    """Whether the observed scope of one stored row is its labelled one (a partial row: its PARTIAL answer is correct); None when no answer pass ran."""
    a = r["answer"]
    if a is None:
        return None
    if r["scope"] == "partial":
        return a["partial"] == "correct"
    return a["observed"] == r["scope"]


def baseline_hits_kept(baseline: Mapping[str, Any], drop: FrozenSet[str]) -> Dict[str, bool]:
    """The baseline run's stored family hits without the dropped families: the paired baseline of a confirmatory comparison."""
    return {f: bool(v) for f, v in baseline["hits"].items() if f not in drop}


def stratum_numbers(rows: Sequence[Mapping[str, Any]], primary: str, answers_ran: bool) -> Dict[str, Any]:
    """The counts behind one stratum line (row = phrasing level): rows, families, strict hits of the eligible rows and, when
    the answer pass ran, how many rows have a scope verdict and how many of those are correct."""
    eligible = [r for r in rows if r["scope"] != "refuse"]
    out: Dict[str, Any] = {
        "rows": len(rows),
        "families": len({r["family_id"] for r in rows}),
        "eligible": len(eligible),
        "hits": sum(1 for r in eligible if r["retrieval"][primary]["strict_hit"]),
        "scope_n": None,
        "scope_hits": None,
    }
    if answers_ran:
        verdicts = [v for v in (row_scope_correct(r) for r in rows) if v is not None]
        out["scope_n"], out["scope_hits"] = len(verdicts), sum(verdicts)
    return out


def confirmatory_stats(
    candidate: Mapping[str, bool], baseline: Mapping[str, bool], groups: Mapping[str, str]
) -> Dict[str, Any]:
    """The primary test and the two labelled sensitivities, from paired family outcomes.

    Args:
        candidate, baseline: family -> strict hit of the primary mode, over the
            same eligible, non-dropped families (a different roster is refused).
        groups: family -> group id.

    Returns:
        ``{"families", "groups", "groups_discordant", "gains", "losses",
        "net", "p_signflip", "durkalski", "p_mcnemar"}``: ``gains`` (``b``) are
        families the candidate hits and the baseline misses, ``losses`` (``c``)
        the reverse; ``p_signflip`` is ``cluster_signflip_p`` of the per-group
        net discordance; ``durkalski`` is ``{"chi2", "p"}`` or ``None``
        (unavailable); ``p_mcnemar`` is the exact two-sided family-level test.

    Raises:
        ValueError: the two arms cover different families, or a family has no group.
    """
    if set(candidate) != set(baseline):
        raise ValueError("the baseline and the candidate cover different families")
    per_group: Dict[str, List[Tuple[bool, bool]]] = {}
    for fam in sorted(candidate):
        if fam not in groups:
            raise ValueError("a family has no group")
        per_group.setdefault(groups[fam], []).append((bool(baseline[fam]), bool(candidate[fam])))
    d = [sum(int(c) - int(b) for b, c in pairs) for _g, pairs in sorted(per_group.items())]
    gains = sum(1 for pairs in per_group.values() for b, c in pairs if c and not b)
    losses = sum(1 for pairs in per_group.values() for b, c in pairs if b and not c)
    dur = durkalski_chi2([family_cluster(pairs) for _g, pairs in sorted(per_group.items())])
    return {
        "families": len(candidate),
        "groups": len(per_group),
        "groups_discordant": sum(1 for x in d if x != 0),
        "gains": gains,
        "losses": losses,
        "net": gains - losses,
        "p_signflip": cluster_signflip_p(d),
        "durkalski": None if dur == UNAVAILABLE else {"chi2": dur.chi2, "p": dur.p_value},
        "p_mcnemar": mcnemar_exact(gains, losses),
    }


# ---------------------------------------------------------------------------
# Groups, floor counts, the baseline
# ---------------------------------------------------------------------------


def resolve_excluded_groups(families: Iterable[str], groups: Mapping[str, str]) -> FrozenSet[str]:
    """The groups (under ``groups``, family -> group id) that the excluded ``families`` belong to.

    A family that is not in the frozen set (a member rejected after the exclusion was made) names
    no group and is skipped, so a stale exclusion excludes nothing instead of wedging the gate; a
    group renamed by a later rejection resolves through its remaining members.
    """
    return frozenset(groups[f] for f in families if f in groups)


def eligible_counts(rows: Sequence[Mapping[str, Any]], groups: Mapping[str, str], excluded: FrozenSet[str]) -> Tuple[int, int]:
    """``(sealed families, eligible answerable families)`` after exclusion."""
    scopes: Dict[str, bool] = {}
    for r in rows:
        if groups[r["family_id"]] in excluded:
            continue
        scopes[r["family_id"]] = scopes.get(r["family_id"], True) and r["scope"] != "refuse"
    return len(scopes), sum(1 for ok in scopes.values() if ok)


def baseline_hits_by_mode(rows: Sequence[Mapping[str, Any]], modes: Iterable[str]) -> Dict[str, Dict[str, bool]]:
    """``{mode: {family: strict hit}}`` over the stored set's rows: the baseline a confirmatory slot is paired with."""
    return {m: family_hits(rows, m) for m in modes}


# ---------------------------------------------------------------------------
# The canonical-v6 guards
# ---------------------------------------------------------------------------


def canonical_guards(facts: Mapping[str, Any]) -> Dict[str, bool]:
    """Every canonical-v6 guard as ``{name: met}``, from the stored facts alone.

    Canonical v6 = v5's guards (D46, D51, H1c) with the held-out clause met by
    the frozen sealed set, plus slot 0 or 0b, the bound records, live expansion
    (a live candidate arm counts as live: it is not replayed) and the tracing
    guard. The pre-publication scan is a step, not a guard. A missing fact
    fails its guard (closed).
    """
    f = facts
    return {
        "heldout_frozen_sealed_set": bool(f.get("heldout_is_frozen_set", False)),
        "heldout_answerable": bool(f.get("heldout_answerable", False)),
        "realistic_answerable": bool(f.get("realistic_answerable", False)),
        "all_modes": bool(f.get("all_modes", False)),
        "distinct_sets": bool(f.get("distinct_sets", False)),
        "refusals_scored": bool(f.get("refusals_scored", False)),
        "completeness_scored": bool(f.get("completeness_scored", False)),
        "top_k_6": f.get("top_k") == 6,
        "no_generation_errors": f.get("generation_errors", 1) == 0,
        "no_incomplete_generations": f.get("generation_incomplete", 1) == 0,
        "no_unknown_status": f.get("generation_unknown", 1) == 0,
        "expansion_attempted": f.get("rewrite_attempts", 0) > 0,
        "no_expansion_fallbacks": f.get("rewrite_fallbacks", 1) == 0,
        "bm25_loaded": bool(f.get("bm25_loaded", False)) or not bool(f.get("bm25_default_path_in_play", True)),
        "judge_clean": bool(f.get("judge_clean", False)),
        "slot_0_or_0b": f.get("slot") in DESCRIPTIVE_SLOTS,
        "bound_records": bool(f.get("bound_records", False)),
        # Expansions generated live (slot 0/0b, or a live candidate arm), not replayed from an artifact.
        "live_expansion": f.get("expansion_mode") in ("live", "candidate"),
        "tracing_off": bool(f.get("tracing_off", False)),
    }


def failed_guards(facts: Mapping[str, Any]) -> List[str]:
    """Names of the guards the stored facts do not meet (empty = canonical)."""
    return [name for name, met in canonical_guards(facts).items() if not met]


# ---------------------------------------------------------------------------
# The stored results document
# ---------------------------------------------------------------------------


def is_private_input(path: Union[str, os.PathLike]) -> bool:
    """True iff ``path`` classifies as a private (not public, not sealed) eval input: the sets a freeze record binds."""
    from src import eval_sets

    return eval_sets.classify(path) == eval_sets.PRIVATE


def row_entry(row: Mapping[str, Any], retrieval: Mapping[str, Mapping[str, Any]],
               answers: Optional[Mapping[str, Mapping[str, Any]]], judged: Mapping[str, Mapping[str, Any]],
               k: int) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": row["id"], "family_id": row["family_id"], "scope": row["scope"],
        "register": row.get("register"), "source": row.get("source"),
        "retrieval": {}, "answer": None, "judge": None,
    }
    for mode, per_row in retrieval.items():
        cell = per_row.get(row["id"])
        if cell is not None:
            entry["retrieval"][mode] = {
                "strict_rank": cell["strict_rank"], "related_rank": cell["related_rank"],
                "strict_hit": bool(cell["strict_hit"].get(k)), "related_hit": bool(cell["related_hit"].get(k)),
            }
    a = (answers or {}).get(row["id"])
    if a is not None:
        entry["answer"] = {"observed": a["observed"], "partial": a["partial"]["status"] if "partial" in a else None}
    j = judged.get(row["id"])
    if j is not None:
        entry["judge"] = {key: j[key] for key in ("ok", "error_type", "n_claims", "supported", "unsupported", "unclear")}
    return entry


def build_results_doc(
    slot_token: Any,
    result: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    dev_sets_of: Callable[[Any, Iterable[str]], Dict[str, str]],
) -> Dict[str, Any]:
    """Assemble the stored results document from a finished matrix run.

    ``slot_token`` is the run's ``src.sealed_run.SlotToken`` (read by attribute only: this module never imports the
    protocol) and ``dev_sets_of`` is ``src.sealed_run.dev_sets_of`` (the gate that maps development set hashes onto
    their freeze records; it raises ``GateRefused``, which belongs to the protocol module), passed in by
    ``src.sealed_run.build_results_doc``, the entry point the evaluator calls.

    Ids, enums, numbers and hashes only: no question, rewrite, answer, claim,
    gap or evidence text. ``result`` is the evaluator's v6 working result. It records the running pipeline's
    ``pipeline_config_hash``, the judge's ``config_hash`` (the judge always runs on every eligible answer:
    ``sample`` is ``null``) and, beside it, ``judge_runtime_sha256`` (r8: the versions of the packages a judge call runs
    through; recorded per slot and compared candidate vs slot 0, not part of the registered ``config_hash``), the
    ``scorer_version`` (the label every eval sidecar carries,
    ``src.eval_cohort.SCORER_VERSION``) and the ``scorer_identity`` (``src.scoring_identity.scorer_identity()``, computed
    from the scoring code and data: 16B compares a candidate with slot 0 only when the judge hash and the scorer
    identity are equal), a set's ``role`` is ``sealed``, ``development`` (a frozen private tuning or realistic
    version) or ``regression`` (a public set).
    """
    k, primary = result["k"], result["primary_mode"]
    if result["top_k"] != slot_token.cutoff or k != slot_token.cutoff:
        raise ValueError("the run was not scored at the prereg's hit cut-off")
    sets = []
    for s in result["sets"]:
        owned = bool(s.get("owned"))
        # the role names what the set is for: the sealed set; a frozen private development version (tuning or
        # realistic: the sets a freeze record binds); or a public set (the regression set), which no freeze binds
        role = "sealed" if owned else "development" if is_private_input(s["path"]) else "regression"
        sets.append({
            "label": s["label"], "role": role,
            "sha256": s["sha256"], "cohort": s["cohort"],
            "rows": [
                row_entry(r, s["retrieval"], s["answers"], s.get("judge_by_id") or {}, k) for r in s["rows"]
            ],
        })
    baseline = None
    if slot_token.baseline is not None:
        by_mode = slot_token.baseline["hits_by_mode"]
        if primary not in by_mode:
            raise ValueError("the baseline run lacks the primary mode")
        baseline = {
            "run_id": slot_token.baseline["run_id"],
            "results_digest": slot_token.baseline["results_digest"],
            "cutoff": slot_token.baseline["cutoff"],
            "hits": by_mode[primary],
        }
    return {
        "version": RESULTS_VERSION, "kind": RESULTS_KIND, "slot": slot_token.slot, "run_id": slot_token.run_id,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top_k": result["top_k"], "k": k, "cutoff": slot_token.cutoff,
        "modes": list(result["modes"]), "primary_mode": primary,
        "answers_ran": bool(result["answers_ran"]),
        "provenance": dict(result["provenance"]),
        "expansion": {
            "mode": result["expansion_mode"], "identity": result.get("expansion_identity"),
            "rewrite_live": result["rewrite_live"], "rewrite_replayed": result["rewrite_replayed"],
        },
        "run_cost": result.get("run_cost"),
        "floor": dict(slot_token.floor),
        "pipeline_config_hash": result.get("pipeline_config_hash"),
        "scorer_version": SCORER_VERSION,
        "scorer_identity": result.get("scorer_identity"),
        "judge": {"config_hash": result.get("judge_config_hash"), "sample": None},
        "judge_runtime_sha256": result.get("judge_runtime_sha256"),
        "prereg_sha256": slot_token.prereg_sha256, "prereg_commit": slot_token.prereg_commit,
        "candidate_config_hash": slot_token.candidate_config_hash,
        "candidate_expansion": slot_token.candidate_expansion,
        "candidate_rewrite_config_hash": slot_token.candidate_rewrite_config_hash,
        # only private development inputs are bound to a freeze record: a registered public set (the regression
        # set slot 0 may run with) is not a development version, so it names no split (the gate's own rule)
        "dev_sets": dev_sets_of(slot_token.ledger, [
            s["sha256"] for s in result["sets"] if not s.get("owned") and is_private_input(s["path"])
        ]),
        "groups": dict(slot_token.groups),
        "baseline": baseline,
        "facts": dict(facts),
        "sets": sets,
    }
