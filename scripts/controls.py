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
list is computed once on the frozen development sets (b7) and committed to the
ledger: on the first run the file is written and a ``controls`` record is added
(the file's sha256, the digest of the draw files' sha256s and the prereg's sha256).
Once recorded the run only **verifies**: the computation must equal the file and
the file must still be the recorded one, and the supplied draw files must be the recorded draws (their
``draws_sha256`` equals the record's), else exit 1 and nothing is written. A file
with no record ("controls file not recorded") or a record with no file ("controls
already recorded") exits 1: deleting the file never allows a recompute.

The rule, rank ceiling and draw count come from the prereg's ``controls`` block
(``--prereg``, default ``eval/prereg_16a2.json``), not from the command line: the
ceiling must be the rule's (``RANK_CEILING``) and, for C1, the number of ``--draw``
files must be ``draws_r``. On every run the ``--draw`` files must have distinct sha256s (a repeated draw is not
another draw; exit 2). The prereg file's sha256 must be the ledger's
``epoch_prereg_sha256()`` (the epoch's one registered prereg). The ``--dev-set``
files must be the frozen versions of BOTH development splits, each exactly once:
their sha256s are the ``set_sha256`` of the ledger's ``freeze`` records for
``tuning`` and ``realistic`` (a later development version never changes this
epoch's controls).

**The incumbent (16A-2b b1a r4).** Every ``--draw`` sidecar carries the ``pipeline_config_hash`` of the pipeline
that produced it (``src.pipeline_config``); the draws must carry one and the same 64-hex value H (else exit 2), and
the pipeline running here (``--persist-dir``, default ``./chroma_db``; the hit cut-off is the prereg's) must hash to
H (exit 2, "the running pipeline is not the draws' pipeline"). The first run records H in the ledger with the
controls (``incumbent_config_hash``); slots 0 and 0b then run only under exactly that pipeline. A verifying run
needs H to be the recorded one (exit 1 otherwise).

Usage:
    python scripts/controls.py [--prereg eval/prereg_16a2.json] --draw d1.rows.json --draw d2.rows.json ... \\
        --dev-set eval/private/dev/<set>.jsonl [--dev-set ...] [--list eval/controls_v1.json] [--persist-dir ./chroma_db]

Prints counts only. Exit codes: 0 written or matching; 1 differs from the
committed list; 2 unusable input; 4 a development set is sealed.
"""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_PREREG = os.path.join(REPO, "eval", "prereg_16a2.json")
DEFAULT_PERSIST_DIR = "./chroma_db"
DEFAULT_CUTOFF = 6  # the hit cut-off when the prereg declares none (sealed_run.DEFAULT_CUTOFF)
FROZEN_DEV_SPLITS = ("tuning", "realistic")

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


def read_controls_prereg(path: str, n_draws: int) -> str:
    """The prereg's controls rule, after checking its ceiling and (rule c1) that ``n_draws`` is ``draws_r``.

    Raises:
        ControlsError: the file or its ``controls`` block is unusable, the ceiling is not the rule's, or the
            number of draw files is not ``draws_r``.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            block = json.load(fh).get("controls")
    except (OSError, ValueError, AttributeError):
        raise ControlsError("the prereg is unreadable or has no controls block") from None
    if not isinstance(block, dict):
        raise ControlsError("the prereg is unreadable or has no controls block")
    rule, ceiling, draws_r = block.get("rule"), block.get("rank_ceiling"), block.get("draws_r")
    if rule not in RANK_CEILING:
        raise ControlsError(f"unknown rule {rule!r}; expected one of {sorted(RANK_CEILING)}")
    if isinstance(ceiling, bool) or ceiling != RANK_CEILING[rule]:
        raise ControlsError("the prereg's rank ceiling is not its rule's")
    if rule == "c1" and (isinstance(draws_r, bool) or draws_r != n_draws):
        raise ControlsError("the number of draw files is not the prereg's draws_r")
    return rule


def read_prereg_cutoff(path: str) -> int:
    """The prereg's ``primary_metric.cutoff`` (the ``top_k`` every counted draw and sealed slot runs at), default 6."""
    try:
        with open(path, encoding="utf-8") as fh:
            metric = json.load(fh).get("primary_metric")
    except (OSError, ValueError, AttributeError):
        raise ControlsError("the prereg is unreadable or has no controls block") from None
    cutoff = metric.get("cutoff", DEFAULT_CUTOFF) if isinstance(metric, dict) else DEFAULT_CUTOFF
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 1:
        raise ControlsError("the prereg's cut-off is not a positive integer")
    return cutoff


def draws_config_hash(draws: Sequence[Mapping[str, Any]]) -> str:
    """The one ``pipeline_config_hash`` (64 hex) every draw sidecar carries: the incumbent's pipeline.

    Raises:
        ControlsError: a draw carries none or a malformed one (a run with no index), or the draws differ.
    """
    found = {doc.get("pipeline_config_hash") if isinstance(doc, dict) else None for doc in draws}
    only = next(iter(found)) if len(found) == 1 else None
    if only is None or not (isinstance(only, str) and len(only) == 64 and set(only) <= set("0123456789abcdef")):
        raise ControlsError("the draws do not all carry one 64-hex pipeline_config_hash")
    return only


def check_running_pipeline(draws_hash: str, persist_dir: str, top_k: int) -> None:
    """The pipeline running here must be the draws' pipeline (``pipeline_config_hash`` equal to theirs).

    Raises:
        ControlsError: it is not (or there is no index at ``persist_dir``).
    """
    from src import pipeline_config

    if pipeline_config.pipeline_config_hash(persist_dir, top_k=top_k) != draws_hash:
        raise ControlsError("the running pipeline is not the draws' pipeline")


def check_distinct_draws(paths: Sequence[str]) -> List[str]:
    """The sha256 of each draw file; the draws must be distinct files by content (a repeat is not another draw).

    Raises:
        ControlsError: two ``--draw`` files hash alike.
    """
    shas = [_file_sha256(p) for p in paths]
    if len(set(shas)) != len(shas):
        raise ControlsError("a draw file is given twice (the draws must be distinct)")
    return shas


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return _sha256_bytes(fh.read())


def _canonical_sha256(obj: Any) -> str:
    """sha256 of the canonical JSON (sorted keys, no spaces, ASCII) of a value."""
    return _sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))


def check_epoch_prereg(ledger: Any, prereg_path: str) -> str:
    """The prereg file's sha256, which must be the ledger's registered epoch prereg.

    Raises:
        ControlsError: the ledger has no registered prereg yet, or it is another file's.
    """
    try:
        sha = _file_sha256(prereg_path)
    except OSError:
        raise ControlsError("the prereg is unreadable or has no controls block") from None
    if ledger.epoch_prereg_sha256() != sha:  # None (nothing registered) refuses too
        raise ControlsError("the prereg is not this epoch's registered prereg")
    return sha


def check_frozen_versions(ledger: Any, paths: Sequence[str]) -> None:
    """The development sets must be the ``tuning`` and the ``realistic`` freeze records' versions, each once.

    Raises:
        ControlsError: a development set is not a frozen version, a split is given twice, or one is omitted.
    """
    from src.eval_sets import sha256_file

    frozen = {}
    for split in FROZEN_DEV_SPLITS:
        rec = ledger.freeze_record(split)
        if rec is not None:
            frozen[rec["set_sha256"]] = split
    seen = set()
    for path in paths:
        split = frozen.get(sha256_file(path))
        if split is None:
            raise ControlsError("a development set is not its frozen version")
        if split in seen:
            raise ControlsError("a development split is given twice")
        seen.add(split)
    if seen != set(FROZEN_DEV_SPLITS):
        raise ControlsError("the controls cover both development splits: a split is missing")


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
    parser.add_argument("--prereg", default=DEFAULT_PREREG, metavar="PATH", help="the prereg (default eval/prereg_16a2.json)")
    parser.add_argument("--draw", action="append", default=[], metavar="ROWS_JSON", help="a draw's rows sidecar (repeat)")
    parser.add_argument("--dev-set", action="append", default=[], metavar="V2_JSONL", help="a frozen development set (repeat)")
    parser.add_argument("--list", dest="list_path", default=DEFAULT_LIST, help="the controls file (default eval/controls_v1.json)")
    parser.add_argument("--persist-dir", default=DEFAULT_PERSIST_DIR, metavar="DIR",
                        help="the index of the pipeline running here (default ./chroma_db); it must be the draws' pipeline")
    args = parser.parse_args(argv)
    if not args.draw or not args.dev_set:
        parser.error("give at least one --draw and one --dev-set")

    from src.eval_privacy import SealedInputError
    from src.eval_schema import SchemaError
    from src.sealed_ledger import ProtocolError
    from src.sealed_run import default_ledger

    try:
        ledger = default_ledger()
        draws = []
        for path in args.draw:
            with open(path, encoding="utf-8") as fh:
                draws.append(json.load(fh))
        rule = read_controls_prereg(args.prereg, len(args.draw))
        draw_shas = check_distinct_draws(args.draw)
        incumbent = draws_config_hash(draws)
        check_running_pipeline(incumbent, args.persist_dir, read_prereg_cutoff(args.prereg))
        lineage = load_lineage(args.dev_set)  # a sealed set is refused (exit 4) before the version check
        check_frozen_versions(ledger, args.dev_set)
        prereg_sha = check_epoch_prereg(ledger, args.prereg)
        doc = compute_controls(draws, lineage, rule)
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
    record = ledger.controls_record()
    if os.path.exists(args.list_path):
        if record is None:
            print("[controls] controls file not recorded: nothing written", file=sys.stderr)
            return EXIT_DIFFERS
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
        if _file_sha256(args.list_path) != record["controls_sha256"]:
            print("[controls] the controls file is not the recorded one; nothing written", file=sys.stderr)
            return EXIT_DIFFERS
        if _canonical_sha256(sorted(draw_shas)) != record["draws_sha256"]:
            print("[controls] the supplied draws are not the recorded draws; nothing written", file=sys.stderr)
            return EXIT_DIFFERS
        if record.get("incumbent_config_hash") != incumbent:
            print("[controls] the draws' pipeline is not the recorded incumbent; nothing written", file=sys.stderr)
            return EXIT_DIFFERS
        print(f"[controls] matches the committed list ({summary})")
        return 0
    if record is not None:
        print("[controls] controls already recorded; a deleted file is never recomputed", file=sys.stderr)
        return EXIT_DIFFERS
    _write(args.list_path, _dump(doc))
    try:
        ledger.record_controls(
            controls_sha256=_file_sha256(args.list_path),
            draws_sha256=_canonical_sha256(sorted(draw_shas)),
            prereg_sha256=prereg_sha,
            incumbent_config_hash=incumbent,
        )
    except ProtocolError:
        print("[controls] controls already recorded", file=sys.stderr)
        return EXIT_DIFFERS
    print(f"[controls] wrote {args.list_path} ({summary}) and recorded it in the ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
