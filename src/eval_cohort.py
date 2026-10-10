"""Phase 16A-1 C4 cohort identity and the rows sidecar (D68, item 6).

Every 16A-1 eval run writes ``<report>.rows.json`` next to its report
(gitignored via ``*.rows.json``, so ``collect_provenance``'s ``git status``
never sees it). Machines read the sidecar, not the Markdown:

``{"version": 1, "scorer_version", "absorbed_map_sha256", "expansion",
"privacy", "cohorts": [cohort block, ...], "inputs": [{"path", "sha256",
"kind": "questions"}, ...], "rows": [{"set_sha256", "id", "mode",
"strict_rank", "related_rank", "completion_rank"}, ...]}``

A **cohort block** identifies one input set: ``path``, ``privacy``,
``schema``, ``sha256``, ``rows``, ``families`` and ``cohort_fp`` = sha256 of
the sorted ``(id, evidence fingerprint, scope)`` tuples. Two arms are
comparable only when their cohorts match (``scripts/bakeoff_report.py``).

Row ids: a public v1 row is ``"q:" + sha256(question)[:12]``; a private v1 row
is salted by its file's sha256 (``src.eval_privacy``); a v2 row carries its own
``id``. A set whose ids collide (a duplicated v1 question) is refused.

The sidecar never carries question text, so its ``inputs`` header makes a
sidecar built from registered public sets classify public (classify rule 5).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.eval_privacy import PUBLIC, private_v1_id, public_v1_id

SIDECAR_VERSION = 1
# Bump when ranks/completion are computed differently (C4 refuses a mismatch).
SCORER_VERSION = "p16a1-v1"
SIDECAR_SUFFIX = ".rows.json"


class CohortError(ValueError):
    """A set cannot form a cohort (colliding ids)."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def v1_row_id(question: str, *, privacy: str, set_sha256: str) -> str:
    """The v1 row id for ``privacy`` (public unsalted, otherwise salted)."""
    if privacy == PUBLIC:
        return public_v1_id(question)
    return private_v1_id(question, set_sha256)


def v1_scope(entry: Mapping[str, Any]) -> str:
    """v1 rows map to scope ``refuse`` (type refusal) or ``answer``."""
    return "refuse" if entry["type"] == "refusal" else "answer"


def v1_evidence(entry: Mapping[str, Any]) -> List[List[str]]:
    """v1 expected sections are ONE OR-group (``[]`` for refusals)."""
    sections = [str(s).strip() for s in entry.get("expected_sections") or []]
    return [sorted(sections)] if sections else []


def evidence_fingerprint(groups: Sequence[Sequence[str]]) -> str:
    """sha256 of the canonical (sorted groups of sorted members) evidence."""
    canon = sorted(sorted(str(s).strip() for s in g) for g in groups)
    return _sha256_text(json.dumps(canon, separators=(",", ":")))


def v1_ids(golden: Sequence[Mapping[str, Any]], *, privacy: str, set_sha256: str) -> Dict[str, str]:
    """``{question: id}`` for a v1 set.

    Raises:
        CohortError: if two rows share an id (a duplicated question).
    """
    ids: Dict[str, str] = {}
    seen: set = set()
    for entry in golden:
        rid = v1_row_id(entry["question"], privacy=privacy, set_sha256=set_sha256)
        if rid in seen:
            raise CohortError("duplicate row id in set (a repeated question); C4 refuses the set")
        seen.add(rid)
        ids[entry["question"]] = rid
    return ids


def cohort_block(
    *,
    path: str,
    privacy: str,
    schema: int,
    sha256: str,
    rows: Iterable[Tuple[str, str, Sequence[Sequence[str]], str]],
) -> Dict[str, Any]:
    """Build one cohort block.

    Args:
        rows: ``(id, family_id, evidence groups, scope)`` per row.
    """
    tuples = []
    families = set()
    n = 0
    for rid, family_id, groups, scope in rows:
        n += 1
        families.add(family_id)
        tuples.append([rid, evidence_fingerprint(groups), scope])
    tuples.sort()
    return {
        "path": str(path),
        "privacy": privacy,
        "schema": schema,
        "sha256": sha256,
        "rows": n,
        "families": len(families),
        "cohort_fp": _sha256_text(json.dumps(tuples, separators=(",", ":"))),
    }


def v1_cohort(
    golden: Sequence[Mapping[str, Any]], *, path: str, privacy: str, sha256: str
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Cohort block and ``{question: id}`` for a v1 set (``family_id = id``)."""
    ids = v1_ids(golden, privacy=privacy, set_sha256=sha256)
    block = cohort_block(
        path=path,
        privacy=privacy,
        schema=1,
        sha256=sha256,
        rows=((ids[e["question"]], ids[e["question"]], v1_evidence(e), v1_scope(e)) for e in golden),
    )
    return block, ids


def rank_rows(
    retrieval: Mapping[str, Any], *, mode: str, set_sha256: str, ids: Mapping[str, str]
) -> List[Dict[str, Any]]:
    """Sidecar rows for one ``evaluate_retrieval`` result (v1: one group)."""
    out = []
    for q in retrieval["per_question"]:
        completion = q.get("completion_rank", q["first_strict_rank"])
        out.append(
            {
                "set_sha256": set_sha256,
                "id": ids[q["question"]],
                "mode": mode,
                "strict_rank": q["first_strict_rank"],
                "related_rank": q["first_related_rank"],
                "completion_rank": completion,
            }
        )
    return out


def sidecar_path(report_path: str) -> str:
    """``<report>.rows.json``."""
    return str(report_path) + SIDECAR_SUFFIX


def build_sidecar(
    *,
    privacy: str,
    cohorts: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    expansion: Mapping[str, Any],
    absorbed_map_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the sidecar document (deterministic key order on dump)."""
    return {
        "version": SIDECAR_VERSION,
        "scorer_version": SCORER_VERSION,
        "absorbed_map_sha256": absorbed_map_sha256,
        "expansion": dict(expansion),
        "privacy": privacy,
        "cohorts": [dict(c) for c in cohorts],
        "inputs": [{"path": c["path"], "sha256": c["sha256"], "kind": "questions"} for c in cohorts],
        "rows": sorted((dict(r) for r in rows), key=lambda r: (r["set_sha256"], r["mode"], r["id"])),
    }


def dump_sidecar(doc: Mapping[str, Any]) -> str:
    """Serialise a sidecar deterministically."""
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"
