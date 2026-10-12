"""Phase 16A-2a item a8 [C]: the critical-controls list from draw sidecars (16A-2b b1 rule, b7).

A **control** is a development family that a candidate must not make worse.
The rule is the owner's choice (D74); this script computes either:

- **C1** -- public-lineage development families whose *strict completion rank*
  is <= 3 in raw hybrid **and** in hybrid+rewrite on **every draw**. A rank-6 hit
  that a re-draw can dislodge is not a control (this reverses the D57 addendum,
  which D74 must say).
- **C2** -- D57 as written: every incumbent strict HIT (rank <= 6, the retrieval
  depth) among those families, in both modes, judged on the incumbent's own run:
  draw 1, the development artifact (b7). Later draws are not consulted, so a
  draw-unstable hit stays a control, which is what D57's zero-flip rule means.

Definitions the plan leaves to the implementation (flagged in the 16A-2a report):

- A **family** is judged as a whole (the ``all`` collapse of ``eval_stats``): it
  qualifies only if every one of its phrasings passes in both modes on every
  draw. A phrasing that is missing from a draw, or has no rank (a miss), fails.
- **Public lineage** = the family has a row with ``source == "legacy"`` (a
  migrated golden-v1 / realistic-v1 family, paraphrases included). Refusal
  families have no retrieval rank and are never controls.
- A **draw** is one ``<report>.rows.json`` sidecar (``src.eval_cohort``). Ranks
  are read from ``completion_rank`` in the modes ``hybrid`` and
  ``hybrid+rewrite``; other modes are ignored. Sidecars are validated by
  ``scripts/bakeoff_report.index_rows`` (a missing row is an error, never a
  MISS) and must agree on scorer version and absorbed-map hash.

Output ``eval/controls_v1.json`` is ``bakeoff_report --controls`` format plus
provenance: ``{"version": 1, "rule", "rank_ceiling", "draws", "families",
"controls": [{"set_sha256", "ids"}]}`` -- ids and set hashes only, no text. The
list is computed once on the frozen development sets (b7). If the file already
exists the run only **verifies**: a differing computation exits 1 and writes
nothing. To recompute (a new development version), a human deletes the file.

Usage:
    python scripts/controls.py --rule c1 --draw d1.rows.json --draw d2.rows.json ... \\
        --dev-set eval/private/dev/<set>.jsonl [--dev-set ...] [--list eval/controls_v1.json]

Prints counts only. Exit codes: 0 written or matching; 1 differs from the
committed list; 2 unusable input; 4 a development set is sealed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.bakeoff_report import (  # noqa: E402
    CONTROLS_VERSION,
    DETAIL_MODE,
    PRIMARY_MODE,
    C4Error,
    index_rows,
)

# Rule -> the completion rank a family must stay within in every mode and draw.
RANK_CEILING = {"c1": 3, "c2": 6}
# Raw hybrid and hybrid+rewrite (D57's two configurations).
MODES = (PRIMARY_MODE, DETAIL_MODE)
PUBLIC_LINEAGE_SOURCE = "legacy"
DEFAULT_LIST = os.path.join(REPO, "eval", "controls_v1.json")

EXIT_DIFFERS = 1
EXIT_UNUSABLE = 2
EXIT_SEALED = 4


class ControlsError(ValueError):
    """The draws or sets cannot yield a trustworthy control list (ids and hashes only)."""


def _within(rank: Any, ceiling: int) -> bool:
    """A rank passes when it exists (``None`` is a miss) and is within the ceiling."""
    return rank is not None and rank <= ceiling


def compute_controls(
    draws: Sequence[Mapping[str, Any]],
    families: Mapping[str, Mapping[str, str]],
    rule: str,
) -> Dict[str, Any]:
    """The controls document for ``rule`` over ``draws`` (pure; no IO).

    Args:
        draws: parsed ``<report>.rows.json`` sidecars, one per draw.
        families: ``{set_sha256: {row_id: family_id}}`` -- the public-lineage,
            retrieval-eligible rows of each development set.
        rule: ``"c1"`` or ``"c2"``.

    Returns:
        ``{"version", "rule", "rank_ceiling", "draws", "families", "controls"}``
        with ``controls`` sorted by set hash and ids sorted.

    Raises:
        ControlsError: unknown rule, no draw or no family, a draw that does not
            cover a family's set, mode or row, draws that disagree on scorer
            version or absorbed-map hash, or no family qualifying.
        scripts.bakeoff_report.C4Error: a draw is not a valid C4 sidecar.
    """
    if rule not in RANK_CEILING:
        raise ControlsError(f"unknown rule {rule!r}; expected one of {sorted(RANK_CEILING)}")
    if not draws:
        raise ControlsError("at least one draw sidecar is required")
    if not families or not all(families.values()):
        raise ControlsError("no public-lineage development family to judge")
    ceiling = RANK_CEILING[rule]

    indexes = [index_rows(doc, f"draw {i}") for i, doc in enumerate(draws, 1)]
    for key in ("scorer_version", "absorbed_map_sha256"):
        if len({doc.get(key) for doc in draws}) != 1:
            raise ControlsError(f"draws disagree on {key}")

    controls: List[Dict[str, Any]] = []
    kept_families = 0
    for sha in sorted(families):
        by_family: Dict[str, List[str]] = {}
        for rid, fid in families[sha].items():
            by_family.setdefault(fid, []).append(rid)
        passing: List[str] = []
        for fid in sorted(by_family):
            rids = sorted(by_family[fid])
            ok = True
            judged = list(zip(draws, indexes))
            if rule == "c2":
                judged = judged[:1]  # D57 as written: the incumbent's run (draw 1) only
            for n, (doc, idx) in enumerate(judged, 1):
                if not any(c.get("sha256") == sha for c in doc["cohorts"]):
                    raise ControlsError(f"draw {n}: set {sha[:12]} has no cohort block")
                for mode in MODES:
                    scored = idx.get((sha, mode))
                    if scored is None:
                        raise ControlsError(f"draw {n}: set {sha[:12]} has no rows for mode {mode}")
                    absent = [r for r in rids if r not in scored]
                    if absent:
                        raise ControlsError(
                            f"draw {n}: set {sha[:12]} mode {mode}: row {absent[0]} is not scored "
                            "(missing evidence is not a MISS)"
                        )
                    if not all(_within(scored[r]["completion_rank"], ceiling) for r in rids):
                        ok = False
            if ok:
                passing.extend(rids)
                kept_families += 1
        if passing:
            controls.append({"set_sha256": sha, "ids": sorted(passing)})
    if not controls:
        raise ControlsError(f"no family qualifies under {rule}; an empty control list is not written")
    return {
        "version": CONTROLS_VERSION,
        "rule": rule,
        "rank_ceiling": ceiling,
        "draws": len(draws),
        "families": kept_families,
        "controls": controls,
    }


def load_lineage(
    paths: Sequence[str], *, inventory: Optional[Mapping[str, Any]] = None
) -> Dict[str, Dict[str, str]]:
    """``{set_sha256: {row_id: family_id}}`` for the public-lineage rows of v2 development sets.

    A family is public lineage if any of its rows has ``source == "legacy"``;
    all of its retrieval-eligible rows (scope not ``refuse``) are returned.

    Raises:
        ControlsError: two files with the same hash.
        src.eval_privacy.SealedInputError: a file is sealed.
        src.eval_schema.SchemaError, InventoryError: a file or the inventory is invalid.
    """
    from src.eval_schema import load_v2
    from src.eval_sets import sha256_file

    out: Dict[str, Dict[str, str]] = {}
    for path in paths:
        sha = sha256_file(path)
        if sha in out:
            raise ControlsError(f"set {sha[:12]} given twice")
        rows = load_v2(path, inventory=inventory)
        lineage = {r["family_id"] for r in rows if r.get("source") == PUBLIC_LINEAGE_SOURCE}
        out[sha] = {
            r["id"]: r["family_id"]
            for r in rows
            if r["family_id"] in lineage and r["scope"] != "refuse"
        }
    return {sha: ids for sha, ids in out.items() if ids}


def _dump(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: compute the list, then write it (absent) or verify it (present)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rule", required=True, choices=sorted(RANK_CEILING))
    parser.add_argument("--draw", action="append", default=[], metavar="ROWS_JSON", help="a draw's rows sidecar (repeat)")
    parser.add_argument("--dev-set", action="append", default=[], metavar="V2_JSONL", help="a frozen development set (repeat)")
    parser.add_argument("--list", dest="list_path", default=DEFAULT_LIST, help="the controls file (default eval/controls_v1.json)")
    args = parser.parse_args(argv)
    if not args.draw or not args.dev_set:
        parser.error("give at least one --draw and one --dev-set")

    from src.eval_privacy import SealedInputError
    from src.eval_schema import SchemaError

    try:
        draws = []
        for path in args.draw:
            with open(path, encoding="utf-8") as fh:
                draws.append(json.load(fh))
        doc = compute_controls(draws, load_lineage(args.dev_set), args.rule)
    except SealedInputError:
        print("[controls] a development set is sealed: refused", file=sys.stderr)
        return EXIT_SEALED
    except (ControlsError, C4Error, SchemaError) as exc:
        print(f"[controls] {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except (OSError, ValueError):
        print("[controls] a draw sidecar is unreadable or not JSON", file=sys.stderr)
        return EXIT_UNUSABLE

    rows = sum(len(c["ids"]) for c in doc["controls"])
    summary = f"rule {doc['rule']}, draws {doc['draws']}, families {doc['families']}, rows {rows}"
    if os.path.exists(args.list_path):
        try:
            with open(args.list_path, encoding="utf-8") as fh:
                committed = json.load(fh)
        except (OSError, ValueError):
            committed = None
        if committed != doc:
            kept = committed.get("controls") if isinstance(committed, dict) else None
            was = sum(len(c.get("ids", [])) for c in kept) if isinstance(kept, list) else "unreadable"
            print(
                f"[controls] computed list differs from the committed list ({summary}; "
                f"committed rows {was}); nothing written",
                file=sys.stderr,
            )
            return EXIT_DIFFERS
        print(f"[controls] matches the committed list ({summary})")
        return 0
    _write(args.list_path, _dump(doc))
    print(f"[controls] wrote {args.list_path} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
