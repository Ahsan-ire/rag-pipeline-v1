"""What a sealed slot run stores and what is decided from it (16A-2b b1a r5, D74).

These functions compute or project values that the stored results document and the adoption analysis use: a row's
strict and related hit at the cut-off, the baseline's per-mode family hits, the document's sets, roles, floor and groups,
the canonical-v6 guards, the eligible-family counts that the floor is judged on, and the groups an exclusion removes
from analysis. They were in ``src.sealed_run``, which the incumbent's code identity leaves out (it holds the slot
protocol: gates, preflight, claim, storage and publication). A value that can move a result must not sit in an
excluded module, so they live here, in a module that ``src.pipeline_config.CODE_EXCLUDED`` does **not** list: editing
any of them changes ``pipeline_config_hash``. ``src.sealed_run`` imports them (and re-exports their old names), so no
caller changes and the behaviour is the one it had.

This module imports no protocol code (no ``GateRefused``, no token, no ledger writes): the document builder reads the
token by attribute and is given the one gate it needs (``dev_sets_of``) by ``src.sealed_run.build_results_doc``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, Mapping, Sequence, Tuple, Union

from src.eval_cohort import SCORER_VERSION
from src.sealed_report import DESCRIPTIVE_SLOTS, RESULTS_KIND, RESULTS_VERSION, family_hits

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
    ``sample`` is ``null``) and the ``scorer_version`` (the value every eval sidecar carries,
    ``src.eval_cohort.SCORER_VERSION``: 16B compares a candidate with slot 0 only when the judge hash and the scorer
    version are equal), a set's ``role`` is ``sealed``, ``development`` (a frozen private tuning or realistic
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
        "judge": {"config_hash": result.get("judge_config_hash"), "sample": None},
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
