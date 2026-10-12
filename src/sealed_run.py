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
slot. The last check before the claim is the slot's execution lock (a slot that
another process is running or recovering is ``claim: busy``). The CLI mints the
token UNCLAIMED (the default, ``claim=False``); :func:`run_slot` then runs :func:`preflight_slot`
(everything the run itself could still refuse: the numeric options, the replay
artifact, the metered clients, the absorbed map, duplicate ids, and the embedding
model, Chroma index and BM25 sidecar a default path loads) and only then claims,
so the durable claim sits right before the first model call and a refusal in
between never costs a slot. A refusal raises :class:`GateRefused` naming one gate
(a fixed token, never a path, hash or row detail) before any retrieval, expansion
or model call:

``slot``, ``prereg`` (a malformed or silent prereg), ``cutoff`` (``top_k`` is not the prereg's
declared hit cut-off, 6 unless the prereg says otherwise: see below), ``expansion_mode`` (the
mode the command line selects is not the mode the prereg declares for the
slot), ``meter`` (no active spend meter), ``tracing`` (tracing is on:
:mod:`src.tracing_guard`), ``freeze_record`` (the sealed split is not frozen),
``set_file`` / ``twins_record`` / ``exclusion_list`` (the sealed file, the
group record or the exclusions bound at the freeze differ from the freeze
record), ``approvals`` (a sealed row is not approval-bound), ``groups`` (the
group record is malformed), ``floor`` (fewer families than the floor after
exclusions: the epoch ends, a D64 stop), ``baseline`` (a confirmatory slot
without a verified baseline run that published a canonical report),
``retrieval_backend`` (the preflight could not load the index a default path
needs) and ``claim`` (a4's claim refused: claimed before, 0b not available, or
the slot's execution lock is held: ``busy``).

Where each input comes from (one source of truth each; ``scripts/sealed_tool.py`` writes them)
----------------------------------------------------------------------------------------------
**Prereg** (``--prereg``, the same ``eval/prereg_16a2.json`` that ``sealed_tool`` checks; this
module reads only the keys below, anything else is ignored)::

    {"version": 1,
     "quota": {"floor_sealed": 50, "floor_answerable": 35, ...},
     "primary_metric": {"cutoff": 6},          # optional; the hit cut-off k, one of the evaluator's HIT_KS, default 6
     "slots": {"0":  {"expansion": "live"},
               "0b": {"expansion": "live"},
               "1":  {"expansion": "replay",    "artifact_sha256": "<64 hex>"},
               "2":  {"expansion": "candidate", "rewrite_candidate": "<64 hex config hash>"}}}

The floor is ``quota.floor_sealed`` families and ``quota.floor_answerable`` eligible
answerable families: the numbers ``sealed_tool freeze`` enforced, not a second copy.
The **cut-off** is ``primary_metric.cutoff`` if the prereg declares one, else 6 (the primary
metric is strict hit@6). Every sealed slot (0, 0b, 1-4) must run at ``top_k`` equal to it, and the
check happens before the claim (:func:`check_cutoff`: the gate when it is given ``top_k``, and the
evaluator's token re-check, which :func:`preflight_slot` runs and so does every real run): a run scored at a
different cut-off is a different estimand, and a confirmatory slot compared with a baseline scored at
another cut-off would show a spurious gain or loss (a section at rank 4 is a hit at 6 and a miss at 3).
The stored results record the cut-off (``results.json``: ``cutoff``, also inside ``baseline``) and
:func:`load_baseline` refuses a baseline whose stored cut-off differs from the candidate's.
``live``: ``--expansion live`` and no ``--rewrite-candidate``. ``replay``:
``--expansion <artifact>`` whose file sha256 equals ``artifact_sha256``.
``candidate``: ``--expansion live`` with ``--rewrite-candidate <config hash>``
equal to the declared hash *and* to the code's actual rewrite ``config_hash``
(``expansion_artifact.rewrite_identity``). ``build:`` is never accepted: a live
slot freezes its own expansions into its run directory.

**Approvals** (``--slot-records`` directory, default ``eval/private/sealed/work/``): the file
``sealed_tool apply`` writes, ``approvals.json``, read as it is::

    {"version": 1, "approved": {"<row id>": {"row_hash": "<64 hex>", "sheet_sha256": "<64 hex>"}, ...},
     "pending_confirm": {...}, "rejected": [...], "escalated": [...]}

Every sealed row's ``eval_schema.row_hash`` must equal ``approved[row id]["row_hash"]`` (a
changed row is not bound until the owner confirms it, so ``pending_confirm`` binds nothing).
The file is not in the ledger, so the token remembers its sha256 and the ``bound_records``
guard re-checks it after the run.

**Group record**: the freeze-bound ``twins_sealed`` payload in the ledger
(``ledger.load_payload("twins_sealed", freeze["twins_sha256"])``, digest-checked against the
freeze record). Only its ``"edges": [[family, family], ...]`` is read.

**Exclusions**: ``SealedLedger.excluded_families()``, no file. The freeze binds the digest of the
group ids recorded before it (``exclusions_digest``); the gate recomputes it from the ledger
(``exclusions_at_freeze``), so a later exclusion (an S-hit, an exposure) simply adds to the list.
Each exclusion names its member families, and the gate maps them onto the freeze-bound groups
(``resolve_excluded_groups``), so a group renamed by a later rejection still resolves and an
exclusion whose families are no longer in the sealed set excludes nothing (never a refusal).

**Groups.** ``src.sealed_ledger.group_ids`` -- the same function ``sealed_tool exclude`` uses --
over the frozen set's families and the group record's edges: the connected components, a family
with no edge a group of its own, each named by its smallest *sealed* family id (an edge may name
a draft that is not in the sealed set: its connectivity is kept, it is not a member).

The slot protocol (:func:`run_slot`, :func:`finish_slot`)
---------------------------------------------------------
1. the slot is claimed (a4 step 1): by :func:`run_slot` after its preflight for a token minted
   with ``claim=False`` (the default, and what the CLI does), or by the gate itself for a token
   minted with ``claim=True`` (an explicit opt-in that skips the preflight, so the gate then
   insists on being given ``top_k`` to check the cut-off before it claims);
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
call. A canonical report (slots 0 and 0b only) is written to ``eval/results_v6.md``
(derived from the private root so that tests redirect it); nothing else ever
writes there. A confirmatory slot (1-4) is never canonical and never published:
after the same render-and-scan it writes ``report.md`` into its own run directory
(``eval/private/sealed/runs/<run id>/``, never committed, ignored by the run
directory digest) and prints an aggregates-only summary (the primary sign-flip
line and the labelled sensitivities, numbers only), without calling anything a
failed guard.

The slot's execution lock (``SealedLedger.lock_slot``) is taken by the gate before the
claim and carried by the token to the end of :func:`run_slot`; :func:`recover_slot`
takes it too. A live run therefore never looks spent, 0b cannot be claimed while
slot 0 runs, and two recoveries of one slot cannot both finish it.
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
    EpochStop,
    ProtocolError,
    RecoveryAction,
    SealedLedger,
    SealedLedgerError,
    SlotBusy,
    SlotRefused,
    below_floor,
    digest_run_dir,
    exclusions_digest,
    fsync_dir,
    group_ids,
)
from src.sealed_report import (
    RESULTS_KIND,
    RESULTS_VERSION,
    confirmatory_summary,
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
REPORT_FILE = "report.md"  # a confirmatory slot's rendered report, inside its run directory
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SLOT = re.compile(r"0b|0|[1-9][0-9]{0,2}")

#: Every gate name :class:`GateRefused` can carry (fixed tokens; safe to print).
GATES = (
    "slot", "prereg", "cutoff", "expansion_mode", "meter", "tracing", "freeze_record", "set_file", "twins_record",
    "exclusion_list", "approvals", "groups", "floor", "baseline", "claim", "set_binding", "stored_results",
    "retrieval_backend",
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


def backend_refused(exc: BaseException) -> GateRefused:
    """The ``retrieval_backend`` refusal for a preflight that could not load the index (detail: the class name only)."""
    return GateRefused("retrieval_backend", type(exc).__name__)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def default_ledger() -> SealedLedger:
    """The epoch's ledger: out-of-tree store, private copy under the sealed dir, committed mirror."""
    root = _privacy.private_root()
    return SealedLedger(root / "sealed" / "ledger", mirror=root.parent / "sealed_ledger.jsonl")


def default_records_dir() -> Path:
    """``eval/private/sealed/work/``: where ``sealed_tool apply`` writes ``approvals.json``."""
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
# Records: prereg, approvals, the group record, exclusions, groups
# ---------------------------------------------------------------------------

#: The ledger payload ``sealed_tool freeze --split sealed`` stores and binds: the group record. The freeze
#: stores it under its content name (``twins_payload_name``: ``twins_sealed_<first 16 hex of its sha256>``,
#: the freeze record's own ``twins_sha256``) and, for readers that predate that, under this plain alias too.
TWINS_PAYLOAD = "twins_sealed"


def twins_payload_name(sha256: str) -> str:
    """The content name of the freeze-bound group record for a freeze whose ``twins_sha256`` is ``sha256``."""
    return f"{TWINS_PAYLOAD}_{sha256[:16]}"


def _read_json(path: Union[str, os.PathLike], gate: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise GateRefused(gate) from None


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


#: The hit cut-off a sealed slot runs at when the prereg declares none: the primary metric is strict hit@6.
DEFAULT_CUTOFF = 6


def _declared_cutoff(primary_metric: Any) -> int:
    """``primary_metric.cutoff`` if the prereg declares one, else :data:`DEFAULT_CUTOFF`.

    Raises:
        GateRefused: ``prereg`` when a declared cut-off is not one of the evaluator's ``HIT_KS`` (a run
            can only be scored at those, so any other value could never be met).
    """
    if not isinstance(primary_metric, dict) or "cutoff" not in primary_metric:
        return DEFAULT_CUTOFF
    from src.evaluator import HIT_KS

    cutoff = primary_metric["cutoff"]
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff not in HIT_KS:
        raise GateRefused("prereg")
    return cutoff


def load_prereg(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    """Read and validate the prereg keys this module uses (see the module docstring).

    The returned document carries the floor as ``doc["floor"] = {"families": quota.floor_sealed,
    "eligible": quota.floor_answerable}``, derived from the prereg's quota (a top-level ``floor``
    key in the file is ignored): the gate and ``sealed_tool freeze`` use the same two numbers. It
    also carries ``doc["cutoff"]``, the hit cut-off every sealed slot must run at (the module docstring).

    Raises:
        GateRefused: ``prereg`` for a missing or malformed file, a bad quota floor or a
            malformed slot declaration.
    """
    doc = _read_json(path, "prereg")
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise GateRefused("prereg")
    quota = doc.get("quota")
    if not (
        isinstance(quota, dict)
        and all(isinstance(quota.get(k), int) and not isinstance(quota.get(k), bool) and quota[k] >= 0
                for k in ("floor_sealed", "floor_answerable"))
    ):
        raise GateRefused("prereg")
    doc = {**doc, "floor": {"families": quota["floor_sealed"], "eligible": quota["floor_answerable"]},
           "cutoff": _declared_cutoff(doc.get("primary_metric"))}
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
    """``sealed_tool``'s ``approvals.json`` -> ``{row id: bound row hash}`` (see the module docstring)."""
    doc = _read_json(path, "approvals")
    approved = doc.get("approved") if isinstance(doc, dict) and doc.get("version") == 1 else None
    if not isinstance(approved, dict):
        raise GateRefused("approvals")
    bound: Dict[str, str] = {}
    for row_id, entry in approved.items():
        if not (isinstance(entry, dict) and _is_hex(entry.get("row_hash")) and _is_hex(entry.get("sheet_sha256"))):
            raise GateRefused("approvals")
        bound[row_id] = entry["row_hash"]
    return bound


def load_group_edges(ledger: SealedLedger, freeze: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """The group edges from the freeze-bound ``twins_sealed`` payload, as family-id pairs.

    The payload is named by the freeze record's own ``twins_sha256`` (``twins_sealed_<sha16>``) and
    read from whichever ledger copy verifies against that digest; the plain ``twins_sealed`` alias
    (the same bytes) is the fallback for a freeze recorded before payloads were content-named.

    Raises:
        GateRefused: ``twins_record`` when no copy verifies (the record is off its freeze record),
            ``groups`` when the payload has no well-formed ``edges``.
    """
    try:
        digest = freeze["twins_sha256"]
        try:
            payload = ledger.load_payload(twins_payload_name(digest), digest)
        except EpochStop:
            payload = ledger.load_payload(TWINS_PAYLOAD, digest)
    except (EpochStop, KeyError, ValueError, TypeError):
        raise GateRefused("twins_record") from None
    edges = payload.get("edges") if isinstance(payload, dict) else None
    if not isinstance(edges, list):
        raise GateRefused("groups")
    out: List[Tuple[str, str]] = []
    for edge in edges:
        if not (isinstance(edge, (list, tuple)) and len(edge) == 2 and all(isinstance(x, str) and x for x in edge)):
            raise GateRefused("groups")
        out.append((edge[0], edge[1]))
    return out


def check_exclusions_bound(ledger: SealedLedger, freeze: Mapping[str, Any]) -> FrozenSet[str]:
    """Every family excluded so far, after checking the freeze-time list against the freeze record.

    The freeze binds the digest of the group *ids* recorded before it; the families returned are the
    exclusions' recorded members (``SealedLedger.excluded_families``), to be mapped onto the frozen
    groups with :func:`resolve_excluded_groups`.

    Raises:
        GateRefused: ``exclusion_list`` when the exclusions recorded before the freeze no longer
            digest to the freeze record's ``exclusions_sha256``, or a record's members are malformed.
    """
    at_freeze = ledger.exclusions_at_freeze(SEALED_SPLIT)
    if at_freeze is None or exclusions_digest(at_freeze) != freeze.get("exclusions_sha256"):
        raise GateRefused("exclusion_list")
    try:
        return ledger.excluded_families()
    except SealedLedgerError:
        raise GateRefused("exclusion_list") from None


def resolve_excluded_groups(families: Iterable[str], groups: Mapping[str, str]) -> FrozenSet[str]:
    """The groups (under ``groups``, family -> group id) that the excluded ``families`` belong to.

    A family that is not in the frozen set (a member rejected after the exclusion was made) names
    no group and is skipped, so a stale exclusion excludes nothing instead of wedging the gate; a
    group renamed by a later rejection resolves through its remaining members.
    """
    return frozenset(groups[f] for f in families if f in groups)


#: The one definition of a group id (``sealed_tool exclude`` calls the same function).
compute_groups = group_ids


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
    cutoff: int = field(repr=False)  # the prereg's hit cut-off: the only top_k this slot may run at
    ledger: Any = field(repr=False, compare=False)
    twins_sha256: str = field(repr=False)
    exclusions_sha256: str = field(repr=False)
    approvals_path: str = field(repr=False)
    approvals_sha256: str = field(repr=False)
    baseline: Optional[Mapping[str, Any]] = field(repr=False)
    mint: object = field(repr=False, compare=False, default=None)
    used: List[bool] = field(repr=False, compare=False, default_factory=lambda: [False])
    lock: Any = field(repr=False, compare=False, default=None)  # the slot's execution lock, held to the run's end
    claimed: List[bool] = field(repr=False, compare=False, default_factory=lambda: [False])

    def __post_init__(self) -> None:
        if self.mint is not _MINT:
            raise TypeError("a SlotToken is minted only by acquire_slot_token")

    def claim(self) -> None:
        """Claim the slot in the ledger (a4 step 1) if this token has not claimed it yet.

        Raises:
            GateRefused: ``claim`` when the ledger refuses (``busy`` when slot 0 still runs for 0b).
        """
        if self.claimed[0]:
            return
        try:
            self.ledger.claim(self.slot)
        except SlotBusy:
            raise GateRefused("claim", "busy") from None
        except SlotRefused:
            raise GateRefused("claim") from None
        self.claimed[0] = True

    def begin(self) -> None:
        """Mark the token used: one claim runs the matrix once (a sealed row is never run twice).

        Raises:
            GateRefused: ``claim`` (``unclaimed``) when the slot was never claimed, (``token_used``) when
                the matrix already started under this token.
        """
        if not self.claimed[0]:
            raise GateRefused("claim", "unclaimed")
        if self.used[0]:
            raise GateRefused("claim", "token_used")
        self.used[0] = True

    def release(self) -> None:
        """Let go of the slot's execution lock (idempotent). :func:`run_slot` does this when the run ends or dies."""
        if self.lock is not None:
            self.lock.release()

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
        """True iff the bound records are still the ones the gate checked.

        The sealed file and the approvals file still hash as at the gate, and the ledger still holds
        the freeze record's group record and the exclusions it bound (a later exclusion is not a
        change: the ledger is append-only and the freeze-time list is re-derived from it).
        """
        if _file_sha(self.set_path) != self.set_sha256 or _file_sha(self.approvals_path) != self.approvals_sha256:
            return False
        try:
            freeze = self.ledger.freeze_record(SEALED_SPLIT)
            if freeze is None or (freeze["twins_sha256"], freeze["exclusions_sha256"]) != (
                self.twins_sha256, self.exclusions_sha256,
            ):
                return False
            load_group_edges(self.ledger, freeze)
            check_exclusions_bound(self.ledger, freeze)
        except (GateRefused, SealedLedgerError):
            return False
        return True


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


def check_cutoff(cutoff: int, top_k: Any) -> None:
    """Refuse a run whose ``top_k`` is not the prereg's hit cut-off ``cutoff``.

    Called before the slot is claimed (the gate when it is given ``top_k``, the evaluator's token
    re-check and so :func:`preflight_slot`) and again by that re-check on every real run. A different ``top_k``
    would score the sealed rows at a different cut-off (strict hit@3 against the baseline's hit@6, say),
    which the primary comparison cannot absorb.

    Raises:
        GateRefused: ``cutoff`` when ``top_k`` is not exactly the cut-off (a bool or a non-integer never is).
    """
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k != cutoff:
        raise GateRefused("cutoff")


def load_baseline(run_id: str, *, ledger: SealedLedger, set_sha256: str, cutoff: int) -> Dict[str, Any]:
    """Verify a stored slot 0 / 0b run and return its per-mode family hits (for a confirmatory slot).

    The run must be a consumed ``0``/``0b`` slot of the same sealed set whose
    run directory still digests to what the ledger recorded, and it must have
    a publication intent: the baseline is the run that published the canonical
    report (when 0b is the canonical one, slot 0 is not a baseline). It must also have been scored
    at the candidate's hit cut-off ``cutoff``: a stored cut-off that differs (or is not recorded)
    makes its hits incomparable with the candidate's.

    Raises:
        GateRefused: ``baseline`` for anything else.
    """
    stored = [r for r in ledger.records("slot_stored") if r["run_id"] == run_id]
    if len(stored) != 1 or stored[0]["slot"] not in DESCRIPTIVE_SLOTS:
        raise GateRefused("baseline")
    if ledger.recover(stored[0]["slot"]).action is not RecoveryAction.CONSUMED:
        raise GateRefused("baseline")
    if not any(r["slot"] == stored[0]["slot"] for r in ledger.records("publication_intent")):
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
    if doc.get("cutoff") != cutoff or doc.get("k") != cutoff or isinstance(doc.get("cutoff"), bool):
        raise GateRefused("baseline", "cutoff")
    return {
        "run_id": run_id,
        "cutoff": cutoff,
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
    claim: bool = False,
    top_k: Optional[int] = None,
) -> SlotToken:
    """Run every gate for ``slot`` and, if all pass, mint the token (claiming the slot only if ``claim=True``).

    See the module docstring for the gates, their order and the formats. The
    claim is the last step, so any refusal leaves the slot unclaimed. Just
    before it the gate takes the slot's execution lock (non-blocking; the token
    keeps it until :func:`run_slot` ends). By default the token comes
    back unclaimed (the gate only checks that the slot COULD be claimed) and
    :func:`run_slot` claims it after its preflight, right before the first model
    call: that is how the CLI makes every refusal precede the claim, and why a library
    caller must opt in (``claim=True``) to claim at the gate, skipping that preflight.

    ``top_k`` is the cut-off the caller will run at. When given it is checked against the prereg's
    hit cut-off right away (``cutoff``); ``claim=True`` requires it, because the gate is then the last
    place that can refuse before the claim. Without it (the CLI path) :func:`preflight_slot` checks it.

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
    if top_k is not None or claim:
        check_cutoff(prereg["cutoff"], top_k)  # None with claim=True is refused too: the gate cannot vouch for it
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
    approvals_path = Path(records_dir) / "approvals.json"
    if _file_sha(set_path) != freeze["set_sha256"]:
        raise GateRefused("set_file")
    edges = load_group_edges(ledger, freeze)  # digest-checked against the freeze record ("twins_record")
    excluded_families = check_exclusions_bound(ledger, freeze)  # the list bound at the freeze, plus any later one
    try:
        rows = tuple(load_sealed(set_path))
    except (SchemaError, InventoryError, NotSealedInput, OSError, UnicodeDecodeError):
        raise GateRefused("set_file") from None
    if not rows:
        raise GateRefused("set_file")

    approved = load_approvals(approvals_path)
    approvals_sha = _file_sha(approvals_path)
    if approvals_sha is None or any(approved.get(r["id"]) != row_hash(r) for r in rows):
        raise GateRefused("approvals")

    families = sorted({r["family_id"] for r in rows})
    groups = compute_groups(families, edges)
    excluded = resolve_excluded_groups(excluded_families, groups)  # by member family: a renamed group still resolves

    floor = prereg["floor"]
    sealed_families, eligible = _eligible_counts(rows, groups, frozenset(excluded))
    if below_floor(sealed_families, floor["families"]) or below_floor(eligible, floor["eligible"]):
        raise GateRefused("floor")

    baseline: Optional[Dict[str, Any]] = None
    if is_confirmatory(sid):
        if not baseline_run:
            raise GateRefused("baseline")
        baseline = load_baseline(
            baseline_run, ledger=ledger, set_sha256=freeze["set_sha256"], cutoff=prereg["cutoff"],
        )
    elif baseline_run:
        raise GateRefused("baseline")

    approved_paths = [str(Path(set_path).resolve())]
    if artifact_path is not None:
        approved_paths.append(str(Path(artifact_path).resolve()))
    lock = None
    try:
        try:
            lock = ledger.lock_slot(sid)  # a slot another process is running or recovering is not ours to claim
        except SlotBusy:
            raise GateRefused("claim", "busy") from None
        token = _mint_token(
            sid, freeze, set_path, rows, groups, excluded, mode, approved_paths, artifact_sha, floor,
            prereg["cutoff"], ledger, approvals_path, approvals_sha, baseline, lock,
        )
        if claim:
            token.claim()
        else:
            try:
                ledger.check_claimable(sid)
            except SlotRefused:
                raise GateRefused("claim") from None
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    return token


def _mint_token(
    sid: str,
    freeze: Mapping[str, Any],
    set_path: Union[str, os.PathLike],
    rows: Tuple[Mapping[str, Any], ...],
    groups: Mapping[str, str],
    excluded: FrozenSet[str],
    mode: str,
    approved_paths: List[str],
    artifact_sha: Optional[str],
    floor: Mapping[str, int],
    cutoff: int,
    ledger: SealedLedger,
    approvals_path: Path,
    approvals_sha: Optional[str],
    baseline: Optional[Dict[str, Any]],
    lock: Any,
) -> SlotToken:
    """Build the (unclaimed) token: the gate's last step before the claim."""
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
        cutoff=cutoff,
        ledger=ledger,
        twins_sha256=freeze["twins_sha256"],
        exclusions_sha256=freeze["exclusions_sha256"],
        approvals_path=str(approvals_path),
        approvals_sha256=approvals_sha,
        baseline=baseline,
        mint=_MINT,
        lock=lock,
    )


def check_token_for_run(
    slot_token: Any,
    *,
    set_paths: Sequence[str],
    expansion_mode: str,
    artifact_path: Optional[str],
    meter: Any,
    top_k: int,
) -> None:
    """The evaluator's own re-check of a token before it does any work (defence in depth).

    Raises:
        GateRefused: not a genuine token, the sealed file is not among the sets
            exactly once or changed since the gate, no meter, the expansion
            the evaluator parsed is not the one the token was minted for, or ``top_k`` is not the
            prereg's hit cut-off (``cutoff``: this is also what makes :func:`preflight_slot` refuse it).
    """
    from src.spend import SpendMeter

    if not is_slot_token(slot_token):
        raise GateRefused("slot")
    check_cutoff(slot_token.cutoff, top_k)
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
    if result["top_k"] != slot_token.cutoff or k != slot_token.cutoff:
        raise ValueError("the run was not scored at the prereg's hit cut-off")
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
    """Write ``text`` to ``path`` durably: temp file, fsync, rename, then fsync the directory entries.

    A rename is a durable fact only once the directory that holds it is synced, and callers record
    "consumed" after this returns: without the sync a crash could leave a ledger that says the report was
    published beside a report that is missing or still the old one. A directory created on the way gets its
    own parent synced as well.
    """
    created = []
    probe = path.parent
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
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
    fsync_dir(path.parent)
    for directory in created:
        fsync_dir(directory.parent)


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
            ledger.exclude(group, "s_hit", members=sorted(f for f, g in doc["groups"].items() if g == group))
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
    rendered: bool = False  # a confirmatory slot's report.md was written into its run directory (never published)


def _exclusions_now(ledger: SealedLedger, groups: Mapping[str, str]) -> FrozenSet[str]:
    """Every group excluded so far, from the ledger (the freeze-time list re-checked against its record).

    ``groups`` is the stored document's family -> group map: the recorded member families are resolved
    onto it, the same way the gate resolved them for the run.
    """
    freeze = ledger.freeze_record(SEALED_SPLIT)
    if freeze is None:
        raise GateRefused("exclusion_list")
    return resolve_excluded_groups(check_exclusions_bound(ledger, freeze), groups)


def finish_slot(
    slot: Union[int, str],
    *,
    ledger: SealedLedger,
    scan: Optional[Callable[[str], Sequence[Optional[str]]]] = None,
    out: Callable[[str], None] = print,
    render: Optional[Callable[..., str]] = None,
) -> SlotOutcome:
    """Steps 4-6 from storage: decide canonical status, render, scan, publish, consume.

    Used for a fresh run (right after step 3) and for recovery alike, so both
    produce the same report. Makes no model, retrieval or expansion call.

    Slots 0 and 0b are the only canonical ones: a canonical run records the intent and
    writes ``eval/results_v6.md``; a non-canonical one prints its failed guards and renders
    nothing. A confirmatory slot (1-4) is never canonical and is not judged by the guards:
    it is rendered and scanned like a canonical report, written to ``report.md`` in its own
    run directory (never published, never committed, no intent) and summarised on ``out`` as
    numbers only (the primary sign-flip line and the labelled sensitivities).

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
    confirmatory = is_confirmatory(sid)
    if rec.action is RecoveryAction.REPUBLISH:
        if not canonical:
            raise GateRefused("stored_results")  # an intent exists only for a canonical run
    elif canonical:
        ledger.record_intent(sid)  # step 4: only a canonical run records an intent
    newly = 0
    excluded: FrozenSet[str] = frozenset()
    # A non-canonical slot 0/0b publishes nothing: there is nothing to render or scan.
    if canonical or confirmatory:
        excluded = _exclusions_now(ledger, doc["groups"])
        if scan is None:
            notes: List[str] = []
            scan = default_scanner(doc["run_id"], notes)
            for note in notes:
                print(f"[eval] scan note: {note}", file=sys.stderr)
        _text, newly = render_and_publish(
            doc, ledger=ledger, excluded=excluded, failed=failed, scan=scan,
            publish_to=(run_dir / REPORT_FILE) if confirmatory else results_v6_path(), render=render,
        )
        if newly:  # the groups the scan added count for the summary too
            excluded = _exclusions_now(ledger, doc["groups"])
    ledger.record_consumed(sid)  # step 6
    if canonical:
        out(f"[eval] sealed slot {sid}: canonical; published {results_v6_path().name}"
            + (f" ({newly} group(s) excluded by the pre-publication scan)" if newly else ""))
    elif confirmatory:
        out(f"[eval] sealed slot {sid}: confirmatory; report rendered in run {doc['run_id']} (not published)"
            + (f" ({newly} group(s) excluded by the pre-publication scan)" if newly else ""))
        for line in confirmatory_summary(doc, excluded):
            out(f"[eval] slot {sid} {line}")
    else:
        out(f"[eval] sealed slot {sid}: not canonical; failed guards: {', '.join(failed)}")
    return SlotOutcome(sid, doc["run_id"], canonical, tuple(failed), canonical, newly, rec.action.value, confirmatory)


def preflight_slot(
    slot_token: SlotToken,
    *,
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
) -> None:
    """Everything the run could still refuse, checked with the not-yet-claimed ``slot_token``.

    Same arguments as :func:`run_slot` (which calls it before claiming an unclaimed token), so that
    a refusal costs no slot. It runs the evaluator up to (not including)
    the first retrieval, expansion or model call: the numeric options, the token and set bindings
    (and ``top_k`` against the prereg's hit cut-off: a different one is a ``cutoff`` refusal here, before the claim),
    the replay artifact (loaded and preflighted against every row), the metered clients, the absorbed
    map, the per-set cohort and duplicate-id checks, and, when a default retrieval or generation path
    is in play, the embedding model, Chroma index and BM25 sidecar it would load. Makes no call.

    Raises:
        Whatever the evaluator refuses with (``ValueError``, ``GateRefused``...); never claims anything.
    """
    from src import evaluator

    evaluator.run_eval_matrix(
        list(set_specs), modes=list(modes), top_k=top_k, skip_refusals=skip_refusals,
        skip_completeness=skip_completeness, judge=judge, judge_sample=judge_sample,
        persist_directory=persist_directory, privacy=PRIVATE, meter=meter, expansion=expansion,
        slot_token=slot_token, preflight_only=True, **dict(matrix_kwargs or {}),
    )


def run_slot(
    slot_token: SlotToken,
    *,
    ledger: SealedLedger,
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
    A token minted unclaimed (``claim=False``) is preflighted first
    (:func:`preflight_slot`: every refusal the run could still make) and claimed
    only when nothing is left to refuse, right before the first model call.
    A crash anywhere before ``store_results`` leaves the slot claimed with no
    stored results, which a4 treats as spent. The token's execution lock is held
    for the whole run and released when it ends or dies (``finally``), so a live
    run is never mistaken for a spent slot.
    """
    from src import evaluator

    try:
        if not slot_token.claimed[0]:
            preflight_slot(
                slot_token, set_specs=set_specs, modes=modes, top_k=top_k, skip_refusals=skip_refusals,
                skip_completeness=skip_completeness, judge=judge, judge_sample=judge_sample,
                persist_directory=persist_directory, meter=meter, expansion=expansion, matrix_kwargs=matrix_kwargs,
            )
            slot_token.claim()
        evaluator.run_eval_matrix(
            list(set_specs), modes=list(modes), top_k=top_k, skip_refusals=skip_refusals,
            skip_completeness=skip_completeness, judge=judge, judge_sample=judge_sample,
            persist_directory=persist_directory, privacy=PRIVATE, meter=meter, expansion=expansion,
            slot_token=slot_token, **dict(matrix_kwargs or {}),
        )
        ledger.store_results(slot_token.slot, sealed_run_dir(slot_token.run_id, create=False))  # step 3
        return finish_slot(slot_token.slot, ledger=ledger, scan=scan, out=out)
    finally:
        slot_token.release()


def recover_slot(
    slot: Union[int, str],
    *,
    ledger: SealedLedger,
    scan: Optional[Callable[[str], Sequence[Optional[str]]]] = None,
    out: Callable[[str], None] = print,
) -> Optional[SlotOutcome]:
    """Recovery for a slot that is already in the ledger. Never makes a call.

    Returns ``None`` when the slot was never claimed (the caller runs it
    fresh). A spent or consumed slot is a ``claim`` refusal (never re-run);
    stored results are finished from storage. The slot's execution lock is held
    meanwhile: a slot that another process is running or recovering is a
    ``claim`` refusal (``busy``), never a spent slot or a second finish.
    """
    sid = normalise_slot(slot)
    try:
        lock = ledger.lock_slot(sid)
    except SlotBusy:
        raise GateRefused("claim", "busy") from None
    try:
        rec = ledger.recover(sid)
        if rec.action is RecoveryAction.NOT_STARTED:
            return None
        if rec.action is RecoveryAction.SPENT:
            raise GateRefused("claim", "spent")
        if rec.action is RecoveryAction.CONSUMED:
            raise GateRefused("claim", "consumed")
        return finish_slot(sid, ledger=ledger, scan=scan, out=out)
    finally:
        lock.release()
