"""The slot-gated sealed run and canonical report v6 (Phase 16A-2a item a5, D73).

``pipeline eval --sealed-slot <n>`` is the only way a run accepts a sealed set.
This module owns the gate that mints the :class:`SlotToken`, the a4 slot
protocol around the run, the canonical-v6 decision and the pre-publication
scan. ``src.sealed_ledger`` (a4) keeps the durable state; ``src.evaluator``
runs the matrix when handed a token; ``src.sealed_report`` renders.

The gate (:func:`acquire_slot_token`)
-------------------------------------
A token is minted only if **every** check passes; the checks that cannot spend
the slot run first and the claim is the last one, so a refusal never costs a
slot. A refusal raises :class:`GateRefused` naming one gate (a fixed token,
never a path, hash or row detail) before any retrieval, expansion or model call:

``slot``, ``prereg`` (a malformed or silent prereg), ``expansion_mode`` (the
mode the command line selects is not the mode the prereg declares for the
slot), ``meter`` (no active spend meter), ``tracing`` (tracing is on:
:mod:`src.tracing_guard`), ``freeze_record`` (the sealed split is not frozen),
``set_file`` / ``twins_record`` / ``exclusion_list`` (a file's sha256 differs
from the freeze record), ``approvals`` (a sealed row's ``row_hash`` is not
approval-bound), ``groups`` (the twins record is malformed), ``floor`` (fewer
families than the floor after exclusions: the epoch ends, a D64 stop),
``baseline`` (a confirmatory slot without a verified baseline run), and
``claim`` (a4's claim refused: claimed before, or 0b not available).

Formats this module defines (the plan leaves them to a5)
-------------------------------------------------------
**Prereg** (``--prereg``; the keys this module reads, anything else is ignored)::

    {"version": 1,
     "floor": {"families": 50, "eligible": 35},
     "slots": {"0":  {"expansion": "live"},
               "0b": {"expansion": "live"},
               "1":  {"expansion": "replay",    "artifact_sha256": "<64 hex>"},
               "2":  {"expansion": "candidate", "rewrite_candidate": "<64 hex config hash>"}}}

``live``: ``--expansion live`` and no ``--rewrite-candidate``. ``replay``:
``--expansion <artifact>`` whose file sha256 equals ``artifact_sha256``.
``candidate``: ``--expansion live`` with ``--rewrite-candidate <config hash>``
equal to the declared hash *and* to the code's actual rewrite ``config_hash``
(``expansion_artifact.rewrite_identity``). ``build:`` is never accepted: a live
slot freezes its own expansions into its run directory.

**Records directory** (``--slot-records``; default ``eval/private/sealed/work/``)::

    approvals.json   {"version": 1, "approved": {"<row hash>": "<sheet sha256>"}}
    twins.json       the 16A-1 twins record; only ``"edges": [[family, family], ...]`` is read
    exclusions.json  {"version": 1, "groups": ["<group id>", ...]}   (the freeze-time list)

The freeze record binds ``twins.json`` and ``exclusions.json`` by file sha256.
Every sealed row's ``eval_schema.row_hash`` must be a key of ``approved`` (its
value, the owner's sheet digest, must be a sha256).

**Groups.** The group map is the connected components of the twins record's
``edges`` over families; a family with no edge is a group of its own. A group's
id is its lexicographically smallest *sealed* family id (an edge may name a
draft that is not in the sealed set: its connectivity is kept, it is not a
member). Exclusions (``exclusions.json`` and the ledger's ``exclusion``
records) name groups by that id; an unknown id refuses the gate.

The slot protocol (:func:`run_slot`, :func:`finish_slot`)
---------------------------------------------------------
1. the gate claims the slot (a4 step 1);
2. ``run_eval_matrix(..., slot_token=token)`` runs the matrix and writes, under
   ``eval/private/sealed/runs/<run id>/`` and never beside any report:
   ``results.json`` (the stored results document, ids and enums only),
   ``rows.json`` (the C4 sidecar), ``inputs.json`` (the scanner contract),
   ``expansion_artifact.json`` (a live run's frozen expansions) and
   ``judge_review.jsonl`` (the judge dump, with claim text);
3. ``ledger.store_results`` fsyncs and digests the directory (a4 step 3);
4. canonical status is a pure function of the stored document
   (:func:`canonical_guards`). A canonical run records the publication intent;
   a non-canonical run records nothing and prints guard names only;
5. the stored document is rendered (``sealed_report``) and scanned in-process
   by ``scripts/scan_leaks.py`` before anything is published. An S-hit excludes
   the hit group (``ledger.exclude``) and re-renders; the same render-and-scan
   loop runs again until the text is clean. A hit that no group explains blocks
   publication (:class:`PublicationBlocked`);
6. ``ledger.record_consumed``.

Steps 4-6 read only the stored directory, whether the run is fresh or a
recovery, so :func:`recover_slot` renders from storage and can never make a
call. A canonical report is written to ``eval/results_v6.md`` (derived from the
private root so that tests redirect it); nothing else ever writes there.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from src import eval_privacy as _privacy
from src.eval_privacy import PRIVATE, REPO_ROOT, new_run_id, sealed_run_dir, write_private
from src.eval_schema import InventoryError, SchemaError, row_hash
from src.eval_sealed_read import NotSealedInput, load_sealed
from src.eval_sets import sha256_file
from src.sealed_ledger import (
    ProtocolError,
    RecoveryAction,
    SealedLedger,
    SlotRefused,
    below_floor,
    digest_run_dir,
)
from src.sealed_report import (
    RESULTS_KIND,
    RESULTS_VERSION,
    family_hits,
    format_sealed_report,
    sealed_set,
    is_confirmatory,
    DESCRIPTIVE_SLOTS,
)
from src.tracing_guard import TracingRefused, assert_tracing_off

SEALED_SPLIT = "sealed"
EXPANSION_MODES = ("live", "replay", "candidate")
RESULTS_FILE = "results.json"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SLOT = re.compile(r"0b|0|[1-9][0-9]{0,2}")

#: Every gate name :class:`GateRefused` can carry (fixed tokens; safe to print).
GATES = (
    "slot", "prereg", "expansion_mode", "meter", "tracing", "freeze_record", "set_file", "twins_record",
    "exclusion_list", "approvals", "groups", "floor", "baseline", "claim", "set_binding", "stored_results",
)

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
# Exceptions (BaseException, like the ledger's and the meter's)
# ---------------------------------------------------------------------------


class GateRefused(BaseException):
    """A slot gate refused. ``gate`` is one of :data:`GATES`; ``detail`` an optional fixed token."""

    def __init__(self, gate: str, detail: Optional[str] = None) -> None:
        super().__init__(gate if detail is None else f"{gate}: {detail}")
        self.gate = gate
        self.detail = detail


class EpochEnded(BaseException):
    """The sealed families (or the eligible answerable ones) fell below the floor: a D64 owner stop."""


class PublicationBlocked(BaseException):
    """The rendered report cannot be published. ``reason`` is a fixed token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def default_ledger() -> SealedLedger:
    """The epoch's ledger: out-of-tree store, private copy under the sealed dir, committed mirror."""
    root = _privacy.private_root()
    return SealedLedger(root / "sealed" / "ledger", mirror=root.parent / "sealed_ledger.jsonl")


def default_records_dir() -> Path:
    """``eval/private/sealed/work/``: approvals, twins and exclusions."""
    return _privacy.private_root() / "sealed" / "work"


def results_v6_path() -> Path:
    """The committed canonical report, ``eval/results_v6.md``.

    Derived from the private root (``<eval dir>/results_v6.md``) so that the
    suite's private-root fixture moves it too: no test can write the real one.
    """
    return _privacy.private_root().parent / "results_v6.md"


def normalise_slot(slot: Union[int, str]) -> str:
    """``0``, ``"0b"``, ``1``... -> the ledger's slot id.

    Raises:
        GateRefused: ``slot`` is not a non-negative integer or ``0b``.
    """
    text = str(slot) if isinstance(slot, int) and not isinstance(slot, bool) else slot
    if not isinstance(text, str) or not _SLOT.fullmatch(text):
        raise GateRefused("slot")
    return text


# ---------------------------------------------------------------------------
# Records: prereg, approvals, twins, exclusions, groups
# ---------------------------------------------------------------------------


def _read_json(path: Union[str, os.PathLike], gate: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise GateRefused(gate) from None


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def load_prereg(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    """Read and validate the prereg keys this module uses (see the module docstring).

    Raises:
        GateRefused: ``prereg`` for a missing or malformed file, a bad floor or a
            malformed slot declaration.
    """
    doc = _read_json(path, "prereg")
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise GateRefused("prereg")
    floor = doc.get("floor")
    if not (
        isinstance(floor, dict)
        and all(isinstance(floor.get(k), int) and not isinstance(floor.get(k), bool) and floor[k] >= 0
                for k in ("families", "eligible"))
    ):
        raise GateRefused("prereg")
    slots = doc.get("slots")
    if not isinstance(slots, dict):
        raise GateRefused("prereg")
    for sid, decl in slots.items():
        if not _SLOT.fullmatch(str(sid)) or not isinstance(decl, dict) or decl.get("expansion") not in EXPANSION_MODES:
            raise GateRefused("prereg")
        if decl["expansion"] == "replay" and not _is_hex(decl.get("artifact_sha256")):
            raise GateRefused("prereg")
        if decl["expansion"] == "candidate" and not _is_hex(decl.get("rewrite_candidate")):
            raise GateRefused("prereg")
    return doc


def load_approvals(path: Union[str, os.PathLike]) -> Dict[str, str]:
    """``approvals.json`` -> ``{row hash: sheet sha256}`` (see the module docstring)."""
    doc = _read_json(path, "approvals")
    approved = doc.get("approved") if isinstance(doc, dict) and doc.get("version") == 1 else None
    if not isinstance(approved, dict) or not all(_is_hex(k) and _is_hex(v) for k, v in approved.items()):
        raise GateRefused("approvals")
    return dict(approved)


def load_twins_edges(path: Union[str, os.PathLike]) -> List[Tuple[str, str]]:
    """The ``edges`` of the twins record as family-id pairs."""
    doc = _read_json(path, "twins_record")
    edges = doc.get("edges") if isinstance(doc, dict) else None
    if not isinstance(edges, list):
        raise GateRefused("groups")
    out: List[Tuple[str, str]] = []
    for edge in edges:
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2 and all(isinstance(x, str) and x for x in edge)):
            raise GateRefused("groups")
        out.append((edge[0], edge[1]))
    return out


def load_exclusion_list(path: Union[str, os.PathLike]) -> FrozenSet[str]:
    """``exclusions.json`` -> the freeze-time excluded group ids."""
    doc = _read_json(path, "exclusion_list")
    groups = doc.get("groups") if isinstance(doc, dict) and doc.get("version") == 1 else None
    if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
        raise GateRefused("exclusion_list")
    return frozenset(groups)


def compute_groups(families: Iterable[str], edges: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Family -> group id: connected components of ``edges``, id = smallest member family.

    Edge endpoints outside ``families`` keep their connectivity but are never
    members (see the module docstring).
    """
    members = set(families)
    parent: Dict[str, str] = {f: f for f in members}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(edges):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    smallest: Dict[str, str] = {}
    for f in sorted(members):
        root = find(f)
        smallest.setdefault(root, f)
    return {f: smallest[find(f)] for f in members}


def _eligible_counts(rows: Sequence[Mapping[str, Any]], groups: Mapping[str, str], excluded: FrozenSet[str]) -> Tuple[int, int]:
    """``(sealed families, eligible answerable families)`` after exclusion."""
    scopes: Dict[str, bool] = {}
    for r in rows:
        if groups[r["family_id"]] in excluded:
            continue
        scopes[r["family_id"]] = scopes.get(r["family_id"], True) and r["scope"] != "refuse"
    return len(scopes), sum(1 for ok in scopes.values() if ok)


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------

_MINT = object()


@dataclass(frozen=True)
class SlotToken:
    """Proof that a sealed slot was claimed and every gate passed (minted only by the gate).

    The token carries the sealed rows themselves (read once through the one
    sealed loader), so the run never re-opens the sealed file through a general
    loader, and the paths it lets a run open (the set, and a declared replay
    artifact). A run entry given no token refuses sealed input; a forged token
    is refused at construction.
    """

    slot: str
    run_id: str
    set_path: str
    set_sha256: str
    rows: Tuple[Mapping[str, Any], ...] = field(repr=False)
    groups: Mapping[str, str] = field(repr=False)
    excluded: FrozenSet[str] = field(repr=False)
    expansion_mode: str
    approved_paths: Tuple[str, ...] = field(repr=False)
    artifact_sha256: Optional[str]
    floor: Mapping[str, int] = field(repr=False)
    twins_path: str = field(repr=False)
    twins_sha256: str = field(repr=False)
    exclusions_path: str = field(repr=False)
    exclusions_sha256: str = field(repr=False)
    baseline: Optional[Mapping[str, Any]] = field(repr=False)
    mint: object = field(repr=False, compare=False, default=None)
    used: List[bool] = field(repr=False, compare=False, default_factory=lambda: [False])

    def __post_init__(self) -> None:
        if self.mint is not _MINT:
            raise TypeError("a SlotToken is minted only by acquire_slot_token")

    def begin(self) -> None:
        """Mark the token used: one claim runs the matrix once (a sealed row is never run twice).

        Raises:
            GateRefused: ``claim`` (``token_used``) when the matrix already started under this token.
        """
        if self.used[0]:
            raise GateRefused("claim", "token_used")
        self.used[0] = True

    def __repr__(self) -> str:
        return f"SlotToken(slot={self.slot!r}, run_id={self.run_id!r})"

    def approves(self, path: Union[str, os.PathLike]) -> bool:
        """True iff ``path`` is a file this token lets the run open (by file identity)."""
        for p in self.approved_paths:
            try:
                if os.path.samefile(p, path):
                    return True
            except OSError:
                if os.path.realpath(p) == os.path.realpath(path):
                    return True
        return False

    def records_still_bound(self) -> bool:
        """True iff the sealed file, twins record and exclusion list still match the freeze record."""
        return all(
            _file_sha(p) == sha
            for p, sha in (
                (self.set_path, self.set_sha256),
                (self.twins_path, self.twins_sha256),
                (self.exclusions_path, self.exclusions_sha256),
            )
        )


def _file_sha(path: Union[str, os.PathLike]) -> Optional[str]:
    try:
        return sha256_file(path)
    except OSError:
        return None


def is_slot_token(obj: Any) -> bool:
    """True iff ``obj`` is a genuine :class:`SlotToken`."""
    return isinstance(obj, SlotToken)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _expansion_mode(expansion: str, rewrite_candidate: Optional[str]) -> str:
    """The mode a command line selects: ``live``, ``candidate`` or ``replay`` (``build:`` refused)."""
    if not isinstance(expansion, str) or not expansion or expansion.startswith("build:"):
        raise GateRefused("expansion_mode")
    if expansion == "live":
        return "candidate" if rewrite_candidate else "live"
    if rewrite_candidate:
        raise GateRefused("expansion_mode")
    return "replay"


def _check_expansion(
    decl: Mapping[str, Any], expansion: str, rewrite_candidate: Optional[str]
) -> Tuple[str, Optional[str], Optional[str]]:
    """Match the command line against the prereg's declaration.

    Returns:
        ``(mode, artifact sha256 or None, artifact path or None)``.
    """
    mode = _expansion_mode(expansion, rewrite_candidate)
    if mode != decl["expansion"]:
        raise GateRefused("expansion_mode")
    if mode == "replay":
        sha = _file_sha(expansion)
        if sha is None or sha != decl["artifact_sha256"]:
            raise GateRefused("expansion_mode")
        return mode, sha, expansion
    if mode == "candidate":
        from src.expansion_artifact import rewrite_identity

        if rewrite_candidate != decl["rewrite_candidate"] or rewrite_identity()["config_hash"] != rewrite_candidate:
            raise GateRefused("expansion_mode")
    return mode, None, None


def load_baseline(run_id: str, *, ledger: SealedLedger, set_sha256: str) -> Dict[str, Any]:
    """Verify a stored slot 0 / 0b run and return its per-mode family hits (for a confirmatory slot).

    The run must be a consumed ``0``/``0b`` slot of the same sealed set whose
    run directory still digests to what the ledger recorded.

    Raises:
        GateRefused: ``baseline`` for anything else.
    """
    stored = [r for r in ledger.records("slot_stored") if r["run_id"] == run_id]
    if len(stored) != 1 or stored[0]["slot"] not in DESCRIPTIVE_SLOTS:
        raise GateRefused("baseline")
    if ledger.recover(stored[0]["slot"]).action is not RecoveryAction.CONSUMED:
        raise GateRefused("baseline")
    try:
        run_dir = sealed_run_dir(run_id, create=False)
        if digest_run_dir(run_dir) != stored[0]["results_digest"]:
            raise GateRefused("baseline")
        doc = json.loads((run_dir / RESULTS_FILE).read_text(encoding="utf-8"))
        sealed = sealed_set(doc)
    except (OSError, ValueError, KeyError, ProtocolError):
        raise GateRefused("baseline") from None
    if doc.get("kind") != RESULTS_KIND or sealed["sha256"] != set_sha256:
        raise GateRefused("baseline")
    return {
        "run_id": run_id,
        "results_digest": stored[0]["results_digest"],
        "hits_by_mode": {m: family_hits(sealed["rows"], m) for m in doc["modes"]},
    }


def acquire_slot_token(
    slot: Union[int, str],
    *,
    ledger: SealedLedger,
    set_path: Union[str, os.PathLike],
    prereg_path: Union[str, os.PathLike],
    records_dir: Union[str, os.PathLike],
    expansion: str,
    rewrite_candidate: Optional[str] = None,
    meter: Any = None,
    baseline_run: Optional[str] = None,
) -> SlotToken:
    """Run every gate for ``slot`` and, if all pass, claim it and mint the token.

    See the module docstring for the gates, their order and the formats. The
    claim is the last step, so any refusal leaves the slot unclaimed.

    Raises:
        GateRefused: a gate refused (its name is on the exception).
        src.sealed_ledger.SealedLedgerError: the ledger itself failed (a lost,
            corrupt or tampered ledger is a D64 stop).
    """
    sid = normalise_slot(slot)
    prereg = load_prereg(prereg_path)
    decl = prereg["slots"].get(sid)
    if decl is None:
        raise GateRefused("prereg")
    mode, artifact_sha, artifact_path = _check_expansion(decl, expansion, rewrite_candidate)

    from src.spend import SpendMeter

    if not isinstance(meter, SpendMeter):
        raise GateRefused("meter")
    try:
        assert_tracing_off()
    except TracingRefused:
        raise GateRefused("tracing") from None

    freeze = ledger.freeze_record(SEALED_SPLIT)
    if freeze is None:
        raise GateRefused("freeze_record")
    records = Path(records_dir)
    twins_path, exclusions_path = records / "twins.json", records / "exclusions.json"
    if _file_sha(set_path) != freeze["set_sha256"]:
        raise GateRefused("set_file")
    if _file_sha(twins_path) != freeze["twins_sha256"]:
        raise GateRefused("twins_record")
    if _file_sha(exclusions_path) != freeze["exclusions_sha256"]:
        raise GateRefused("exclusion_list")
    try:
        rows = tuple(load_sealed(set_path))
    except (SchemaError, InventoryError, NotSealedInput, OSError, UnicodeDecodeError):
        raise GateRefused("set_file") from None
    if not rows:
        raise GateRefused("set_file")

    approved = load_approvals(records / "approvals.json")
    if any(row_hash(r) not in approved for r in rows):
        raise GateRefused("approvals")

    families = sorted({r["family_id"] for r in rows})
    groups = compute_groups(families, load_twins_edges(twins_path))
    known_groups = set(groups.values())
    excluded = load_exclusion_list(exclusions_path) | ledger.exclusions()
    if not excluded <= known_groups:
        raise GateRefused("exclusion_list")

    floor = prereg["floor"]
    sealed_families, eligible = _eligible_counts(rows, groups, frozenset(excluded))
    if below_floor(sealed_families, floor["families"]) or below_floor(eligible, floor["eligible"]):
        raise GateRefused("floor")

    baseline: Optional[Dict[str, Any]] = None
    if is_confirmatory(sid):
        if not baseline_run:
            raise GateRefused("baseline")
        baseline = load_baseline(baseline_run, ledger=ledger, set_sha256=freeze["set_sha256"])
    elif baseline_run:
        raise GateRefused("baseline")

    try:
        ledger.claim(sid)
    except SlotRefused:
        raise GateRefused("claim") from None

    approved_paths = [str(Path(set_path).resolve())]
    if artifact_path is not None:
        approved_paths.append(str(Path(artifact_path).resolve()))
    return SlotToken(
        slot=sid,
        run_id=new_run_id(),
        set_path=str(Path(set_path).resolve()),
        set_sha256=freeze["set_sha256"],
        rows=rows,
        groups=groups,
        excluded=frozenset(excluded),
        expansion_mode=mode,
        approved_paths=tuple(approved_paths),
        artifact_sha256=artifact_sha,
        floor=dict(floor),
        twins_path=str(twins_path),
        twins_sha256=freeze["twins_sha256"],
        exclusions_path=str(exclusions_path),
        exclusions_sha256=freeze["exclusions_sha256"],
        baseline=baseline,
        mint=_MINT,
    )


def check_token_for_run(
    slot_token: Any,
    *,
    set_paths: Sequence[str],
    expansion_mode: str,
    artifact_path: Optional[str],
    meter: Any,
) -> None:
    """The evaluator's own re-check of a token before it does any work (defence in depth).

    Raises:
        GateRefused: not a genuine token, the sealed file is not among the sets
            exactly once or changed since the gate, no meter, or the expansion
            the evaluator parsed is not the one the token was minted for.
    """
    from src.spend import SpendMeter

    if not is_slot_token(slot_token):
        raise GateRefused("slot")
    owned = [p for p in set_paths if slot_token.approves(p)]
    if len(owned) != 1:
        raise GateRefused("set_binding")
    if _file_sha(owned[0]) != slot_token.set_sha256:
        raise GateRefused("set_file")
    if not isinstance(meter, SpendMeter):
        raise GateRefused("meter")
    if expansion_mode == "build":  # a slot freezes its own expansions inside its run directory, nowhere else
        raise GateRefused("expansion_mode")
    evaluator_mode = "replay" if expansion_mode == "replay" else "live"
    token_mode = "replay" if slot_token.expansion_mode == "replay" else "live"
    if evaluator_mode != token_mode:
        raise GateRefused("expansion_mode")
    if expansion_mode == "replay" and (
        artifact_path is None or _file_sha(artifact_path) != slot_token.artifact_sha256
        or not slot_token.approves(artifact_path)
    ):
        raise GateRefused("expansion_mode")


# ---------------------------------------------------------------------------
# Canonical v6
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


def _row_entry(row: Mapping[str, Any], retrieval: Mapping[str, Mapping[str, Any]],
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


def build_results_doc(slot_token: SlotToken, result: Mapping[str, Any], facts: Mapping[str, Any]) -> Dict[str, Any]:
    """Assemble the stored results document from a finished matrix run.

    Ids, enums, numbers and hashes only: no question, rewrite, answer, claim,
    gap or evidence text. ``result`` is the evaluator's v6 working result.
    """
    k, primary = result["k"], result["primary_mode"]
    sets = []
    for s in result["sets"]:
        owned = bool(s.get("owned"))
        sets.append({
            "label": s["label"], "role": "sealed" if owned else "development",
            "sha256": s["sha256"], "cohort": s["cohort"],
            "rows": [
                _row_entry(r, s["retrieval"], s["answers"], s.get("judge_by_id") or {}, k) for r in s["rows"]
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
            "hits": by_mode[primary],
        }
    return {
        "version": RESULTS_VERSION, "kind": RESULTS_KIND, "slot": slot_token.slot, "run_id": slot_token.run_id,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top_k": result["top_k"], "k": k, "modes": list(result["modes"]), "primary_mode": primary,
        "answers_ran": bool(result["answers_ran"]),
        "provenance": dict(result["provenance"]),
        "expansion": {
            "mode": result["expansion_mode"], "identity": result.get("expansion_identity"),
            "rewrite_live": result["rewrite_live"], "rewrite_replayed": result["rewrite_replayed"],
        },
        "run_cost": result.get("run_cost"),
        "floor": dict(slot_token.floor),
        "groups": dict(slot_token.groups),
        "baseline": baseline,
        "facts": dict(facts),
        "sets": sets,
    }


def write_results_doc(run_dir: Union[str, os.PathLike], doc: Mapping[str, Any]) -> Path:
    """Write ``results.json`` into the run directory (contained, atomic)."""
    path = Path(run_dir) / RESULTS_FILE
    write_private(path, json.dumps(doc, sort_keys=True, indent=1) + "\n")
    return path


def load_results_doc(run_dir: Union[str, os.PathLike]) -> Dict[str, Any]:
    """Read ``results.json`` back (``stored_results`` gate on a missing or malformed one)."""
    try:
        doc = json.loads((Path(run_dir) / RESULTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GateRefused("stored_results") from None
    if not isinstance(doc, dict) or doc.get("kind") != RESULTS_KIND or doc.get("version") != RESULTS_VERSION:
        raise GateRefused("stored_results")
    return doc


# ---------------------------------------------------------------------------
# Publication: render, scan in-process, exclude on a hit, re-render
# ---------------------------------------------------------------------------


def _load_scanner() -> Any:
    """``scripts/scan_leaks.py`` as a module (the pre-publication scan runs it in-process)."""
    import importlib

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("scripts.scan_leaks")


def default_scanner(run_id: str, notes: Optional[List[str]] = None) -> Callable[[str], List[Optional[str]]]:
    """A ``scan(text) -> [family id or None, ...]`` over the run's needles, built once.

    Each element is one hit: the hit family's opaque id for an S-hit (a sealed
    needle), ``None`` for any other private needle. Building the needles reads
    the sealed set through the sealed loader and the handbook dump in-process.

    Raises:
        PublicationBlocked: ``scan_refused`` when the scanner cannot run.
    """
    scan_leaks = _load_scanner()
    try:
        index = scan_leaks.build_needles(scan_leaks.collect_sources(run_id=run_id), notes=notes)
    except scan_leaks.ScanRefusal:
        raise PublicationBlocked("scan_refused") from None

    def scan(text: str) -> List[Optional[str]]:
        return [hit.family for hit in scan_leaks.scan_text(text, "report", index)]

    return scan


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _check_floor_doc(doc: Mapping[str, Any], excluded: FrozenSet[str]) -> None:
    rows = sealed_set(doc)["rows"]
    n, eligible = _eligible_counts(rows, doc["groups"], excluded)
    if below_floor(n, doc["floor"]["families"]) or below_floor(eligible, doc["floor"]["eligible"]):
        raise EpochEnded("the sealed families fell below the floor after exclusion")


def render_and_publish(
    doc: Mapping[str, Any],
    *,
    ledger: SealedLedger,
    excluded: FrozenSet[str],
    failed: Sequence[str],
    scan: Callable[[str], Sequence[Optional[str]]],
    publish_to: Optional[Path],
    render: Optional[Callable[..., str]] = None,
) -> Tuple[str, int]:
    """Step 5: render, scan, and on an S-hit exclude the hit group and render again.

    Args:
        doc: the stored results document.
        excluded: groups already excluded.
        failed: failed guard names (empty = canonical).
        scan: ``text -> [family id | None, ...]`` (one element per hit).
        publish_to: where a clean canonical report is written, or ``None`` (a
            non-canonical run is rendered and scanned but not published).
        render: the renderer; ``None`` means ``format_sealed_report`` (looked up at
            call time, so a test can substitute one).

    Returns:
        ``(text, groups excluded by the scan)``.

    Raises:
        EpochEnded: an exclusion left fewer families than the floor.
        PublicationBlocked: a hit no group explains, or one that persists.
    """
    excluded = frozenset(excluded)
    newly = 0
    while True:
        _check_floor_doc(doc, excluded)
        text = (render or format_sealed_report)(
            doc, excluded=excluded, canonical=not failed, failed_guards=tuple(failed)
        )
        hits = list(scan(text))
        if not hits:
            break
        if any(h is None or h not in doc["groups"] for h in hits):
            raise PublicationBlocked("unattributed_hit")
        fresh = sorted({doc["groups"][h] for h in hits} - excluded)
        if not fresh:
            raise PublicationBlocked("persistent_hit")
        for group in fresh:
            ledger.exclude(group, "s_hit")
        excluded = excluded | frozenset(fresh)
        newly += len(fresh)
    if publish_to is not None:
        _atomic_write_text(publish_to, text)
    return text, newly


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotOutcome:
    """What a slot run or recovery ended with (all fixed tokens and counts)."""

    slot: str
    run_id: str
    canonical: bool
    failed_guards: Tuple[str, ...]
    published: bool
    scan_exclusions: int
    action: str


def _exclusions_now(records_dir: Union[str, os.PathLike], ledger: SealedLedger) -> FrozenSet[str]:
    freeze = ledger.freeze_record(SEALED_SPLIT)
    path = Path(records_dir) / "exclusions.json"
    if freeze is None or _file_sha(path) != freeze["exclusions_sha256"]:
        raise GateRefused("exclusion_list")
    return load_exclusion_list(path) | ledger.exclusions()


def finish_slot(
    slot: Union[int, str],
    *,
    ledger: SealedLedger,
    records_dir: Union[str, os.PathLike],
    scan: Optional[Callable[[str], Sequence[Optional[str]]]] = None,
    out: Callable[[str], None] = print,
    render: Optional[Callable[..., str]] = None,
) -> SlotOutcome:
    """Steps 4-6 from storage: decide canonical status, render, scan, publish, consume.

    Used for a fresh run (right after step 3) and for recovery alike, so both
    produce the same report. Makes no model, retrieval or expansion call.

    Raises:
        GateRefused: ``stored_results`` when the slot has nothing to finish, or
            the run directory no longer digests to the recorded value.
    """
    sid = normalise_slot(slot)
    rec = ledger.recover(sid)
    if rec.action not in (RecoveryAction.RENDER_FROM_STORAGE, RecoveryAction.REPUBLISH):
        raise GateRefused("stored_results")
    run_dir = sealed_run_dir(rec.run_id, create=False)
    try:
        if digest_run_dir(run_dir) != rec.results_digest:
            raise GateRefused("stored_results")
    except ProtocolError:
        raise GateRefused("stored_results") from None
    doc = load_results_doc(run_dir)
    failed = failed_guards(doc["facts"])
    canonical = not failed
    if rec.action is RecoveryAction.REPUBLISH:
        if not canonical:
            raise GateRefused("stored_results")  # an intent exists only for a canonical run
    elif canonical:
        ledger.record_intent(sid)  # step 4: only a canonical run records an intent
    newly = 0
    if canonical:  # a non-canonical run publishes nothing: there is nothing to render or scan
        excluded = _exclusions_now(records_dir, ledger)
        if scan is None:
            notes: List[str] = []
            scan = default_scanner(doc["run_id"], notes)
            for note in notes:
                print(f"[eval] scan note: {note}", file=sys.stderr)
        _text, newly = render_and_publish(
            doc, ledger=ledger, excluded=excluded, failed=failed, scan=scan,
            publish_to=results_v6_path(), render=render,
        )
    ledger.record_consumed(sid)  # step 6
    if canonical:
        out(f"[eval] sealed slot {sid}: canonical; published {results_v6_path().name}"
            + (f" ({newly} group(s) excluded by the pre-publication scan)" if newly else ""))
    else:
        out(f"[eval] sealed slot {sid}: not canonical; failed guards: {', '.join(failed)}")
    return SlotOutcome(sid, doc["run_id"], canonical, tuple(failed), canonical, newly, rec.action.value)


def run_slot(
    slot_token: SlotToken,
    *,
    ledger: SealedLedger,
    records_dir: Union[str, os.PathLike],
    set_specs: Sequence[Tuple[str, str]],
    modes: Sequence[str],
    top_k: int,
    skip_refusals: bool,
    skip_completeness: bool,
    judge: bool,
    judge_sample: Optional[int],
    persist_directory: str,
    meter: Any,
    expansion: str,
    matrix_kwargs: Optional[Mapping[str, Any]] = None,
    scan: Optional[Callable[[str], Sequence[Optional[str]]]] = None,
    out: Callable[[str], None] = print,
) -> SlotOutcome:
    """Steps 2-6 for a freshly claimed slot (step 1 was the gate's claim).

    ``matrix_kwargs`` passes the evaluator's injection seams (fakes) through.
    A crash anywhere before ``store_results`` leaves the slot claimed with no
    stored results, which a4 treats as spent.
    """
    from src import evaluator

    evaluator.run_eval_matrix(
        list(set_specs), modes=list(modes), top_k=top_k, skip_refusals=skip_refusals,
        skip_completeness=skip_completeness, judge=judge, judge_sample=judge_sample,
        persist_directory=persist_directory, privacy=PRIVATE, meter=meter, expansion=expansion,
        slot_token=slot_token, **dict(matrix_kwargs or {}),
    )
    ledger.store_results(slot_token.slot, sealed_run_dir(slot_token.run_id, create=False))  # step 3
    return finish_slot(slot_token.slot, ledger=ledger, records_dir=records_dir, scan=scan, out=out)


def recover_slot(
    slot: Union[int, str],
    *,
    ledger: SealedLedger,
    records_dir: Union[str, os.PathLike],
    scan: Optional[Callable[[str], Sequence[Optional[str]]]] = None,
    out: Callable[[str], None] = print,
) -> Optional[SlotOutcome]:
    """Recovery for a slot that is already in the ledger. Never makes a call.

    Returns ``None`` when the slot was never claimed (the caller runs it
    fresh). A spent or consumed slot is a ``claim`` refusal (never re-run);
    stored results are finished from storage.
    """
    sid = normalise_slot(slot)
    rec = ledger.recover(sid)
    if rec.action is RecoveryAction.NOT_STARTED:
        return None
    if rec.action is RecoveryAction.SPENT:
        raise GateRefused("claim", "spent")
    if rec.action is RecoveryAction.CONSUMED:
        raise GateRefused("claim", "consumed")
    return finish_slot(sid, ledger=ledger, records_dir=records_dir, scan=scan, out=out)
