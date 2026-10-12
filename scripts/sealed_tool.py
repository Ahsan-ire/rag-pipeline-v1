"""Phase 16A-2a item a3 (D72): the sealed tool. One CLI for everything that touches sealed rows.

Sealed rows are read only through :func:`src.eval_sealed_read.load_sealed`; every other module
that handles them is a *trusted processor* (D72): this tool, ``scan_leaks.py`` and a slot-gated
run. Whatever this tool prints goes to the orchestrator's context, so **everything it prints goes
through one allowlisted printer** (:class:`Printer`): integers, sha256 hex, enum words from the
closed :data:`ENUM_WORDS` set and, in two places only, opaque ids (``audit``'s per-hit family id
and ``return``'s validated line). A value that is none of these (free text, a float, a negative
number, an unknown word, a malformed id) raises :class:`PrintRefused` instead of being printed.
Keys are code literals. Errors, exceptions and ``argparse`` complaints are rendered as fixed
``error reason=<enum>`` lines; no exception text is ever shown (it could carry a path or a row).

Usage (global options first; every path is injectable so tests use tmp dirs only)::

    sealed_tool.py [--private-root D] [--repo D] [--ledger F] [--mirror F] [--inventory F]
                   [--prereg F] [--handbook-dump F] [--claude-projects D] [--staging-root D]
                   <command> ...

Commands: ``ids``, ``digest``, ``count-migrated``, ``inputs-check``, ``twins``, ``split``,
``sheet``, ``apply``, ``freeze``, ``escalate``, ``calibrate``, ``disagree``, ``exclude``,
``audit``, ``power``, ``prereg-check``, ``return``.

Exit codes: 0 ok; 1 a checked record failed (``prereg-check``, ``return`` invalid, internal error);
2 usage or invalid input (schema errors by line and field only); 5 ``audit`` found an S-hit;
6 a split attempt was infeasible (**nothing released**; recorded); 8 refused (prereg not pushed
or round undeclared, wrong state, missing approval or recheck, changed assignment ...);
9 **freeze infeasible after round 2** (the D64 stop); 10 ledger stop (corrupt, tampered or lost
private ledgers; the D64 stop).

Layout (all under the private root; ``W`` = ``sealed/work/``, ``DW`` = ``work/``)
----------------------------------------------------------------------------------
- ``sealed/pool/**.jsonl``       the SP's drafts (marked rows). ``split`` consumes them.
- ``sealed/sealed_r<R>.jsonl``   the sealed side of round R's release (marked).
- ``dev/tuning_r<R>.jsonl``, ``dev/realistic_r<R>.jsonl``  the development side (**unmarked**).
  Pinned development families are any other ``dev/tuning*.jsonl`` / ``dev/realistic*.jsonl``
  (the file-name prefix names the split); ``dev/quarantine*.jsonl`` is neither.
- ``sealed/quarantine*.jsonl``   rejected, demoted or dropped sealed families (marked).
- ``sealed/heldout-v2.jsonl``, ``dev/tuning-v2.jsonl``, ``dev/realistic-v2.jsonl``  frozen sets.
- ``W/approvals.json`` / ``DW/approvals.json``  owner approvals bound to row hashes.
- ``W/tutorial_map.json``        ``{family id: opaque tutorial tag}`` (the "same source tutorial"
  relation; rows carry no tutorial id, so the SP's dispatch notes feed this map).
- ``W/sheets/``, ``W/escalations/``, ``W/twins_current_r<R>.json``, ``W/release.journal``.
- ledger payloads (``store_payload``): ``twins_r<R>_<pool digest>``, ``assign_r<R>``,
  ``twins_sealed`` -- content-free (ids, hashes, counts, enums).

Protocols
---------
**Groups.** The connected components of ALL relations (a twin verdict, the same source tutorial per
``tutorial_map.json``, the same primary section) over every draft AND every pinned family, computed before
anything is filtered. A component holding a pinned development family, or a draft tied to a public question (a
twin verdict naming a public id, or a shared 8-token window), is sealed-ineligible as a whole and its edges are
not passed. In round 2 a draft in a component holding a round-1 sealed family is *dropped* (to
``sealed/quarantine_r2.jsonl``), never joined.

**split.** Needs the pushed prereg (a7's ancestry rule) declaring the round, a current ``twins`` record for the
pool, and passes the exact final quotas (``compute_quotas``) to ``split_families`` with the pinned families
preassigned. ``SplitInfeasible`` (or a tuning quota under ``tuning_min``) records the attempt and releases nothing;
the first feasible attempt stores ``assign_r<R>`` and the release record, then moves the files under the journal.

**freeze.** Without a verdict it writes the lexical-screen pairs and a content digest to ``W/screen_<split>.json``
and stops (``recheck_missing``, with ``pairs_required``). With ``--recheck-verdict`` / ``--recheck-return`` (the
SP's ``twins.jsonl`` and ``return.json``) it checks, in this order and with no side effect before step 8:
(1) replay of every released round from its stored payload; (2) the files still hold the assignment; (3) the
verdict is for THIS content (digest, mtime, return ok, enough pairs); (4) twins: a sealed twin of development
demotes its whole group to quarantine (approvals kept; after the sealed freeze, an exclusion instead), a twin
inside sealed drops the later group; (5) approvals bound by row hash; (6) 2-3 phrasings (D64 stop); (7) band,
strata, min_share, group invariant, floor, with the sealed band repaired by the fewest seeded group demotions;
(8) quarantine moves, exclusions, the frozen file (mode 0444) and the ledger record.

**prereg** (``eval/prereg_16a2.json``): ``version``, ``branch``, ``rounds``, ``seeds`` (``split_r1``, ``split_r2``,
``repair``, ``calibration``), ``quota`` (``sealed``, ``spare``, ``floor_sealed``, ``floor_answerable``,
``realistic_min``, ``tuning_min``, ``examples``), ``band``, ``mwe``, ``alpha``, ``k_max``, ``draws_r``, ``power``
(``alpha``, ``discordance``, ``planned_groups``, ``draws``, ``seed``, ``table``), optional ``strata``,
``min_share`` (keyed by split; the source class ``tutorial_or_colleague`` pools those two sources) and the free keys
named in :data:`PREREG_KEYS`. ``prereg-check`` recomputes ``quota.examples`` and ``power.table``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src import eval_privacy  # noqa: E402  (module import: tests relocate private_root)
from src import eval_schema  # noqa: E402
from src import eval_sets  # noqa: E402
from src import eval_stats  # noqa: E402
from src import sealed_inputs  # noqa: E402
from src.eval_sealed_read import NotSealedInput, load_sealed  # noqa: E402  (the only sealed reader)
from src.eval_split import SplitInfeasible, SplitInputError, families_digest, split_families  # noqa: E402
from src.sealed_ledger import (  # noqa: E402
    EpochStop,
    LedgerCorrupt,
    LedgerRefused,
    MirrorTampered,
    ProtocolError,
    SealedLedger,
    SealedLedgerError,
)
from scripts import scan_leaks as sl  # noqa: E402  (tokenisation, windows, S-needles, exemptions)
from scripts import sp_launch as sp  # noqa: E402  (return validator, journaled move)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_HIT = 5
EXIT_SPLIT_INFEASIBLE = 6
EXIT_REFUSED = 8
EXIT_FREEZE_INFEASIBLE = 9
EXIT_LEDGER_STOP = 10

SCOPES = ("answer", "partial", "refuse")
REGISTERS = ("lay", "modelled", "terse")
SOURCES = ("handbook", "tutorial", "colleague", "legacy")
SPLITS = ("sealed", "tuning", "realistic")
DEV_SPLITS = ("tuning", "realistic")
SPLIT_ALIASES = {
    "sealed": "sealed", "heldout-v2": "sealed", "heldout_v2": "sealed",
    "tuning": "tuning", "tuning-v2": "tuning", "tuning_v2": "tuning",
    "realistic": "realistic", "realistic-v2": "realistic", "realistic_v2": "realistic",
}
KINDS = ("sealed", "development")
MODELS = ("independent", "correlated")
TARGET_CLASSES = ("transcript", "memory", "log", "research", "work", "run", "dispatch", "staging", "tree", "history")
EXCLUSION_REASONS = ("s_hit", "exposure", "twin_dev_version")
FROZEN_FILES = {"sealed": ("sealed", "heldout-v2.jsonl"), "tuning": ("dev", "tuning-v2.jsonl"), "realistic": ("dev", "realistic-v2.jsonl")}
BAND_MIN_NUM, BAND_MIN_DEN = 1, 4  # 25 %
BAND_MAX_NUM, BAND_MAX_DEN = 3, 10  # 30 %
OWNER_MARKS = ("A", "C", "R", "E")
SHEET_COLUMNS = (
    "row_id", "family_id", "row_hash", "phrasing_no", "phrasing", "scope", "register", "source",
    "evidence", "gaps", "ambiguous", "A", "C", "R", "E", "corrections", "doubt", "colleague_key",
)
CONFIRM_COLUMNS = ("row_id", "family_id", "row_hash", "phrasing", "scope", "evidence", "gaps", "ambiguous", "OK")
CORRECTABLE = ("question", "scope", "evidence", "gaps", "ambiguous", "register")
RETURN_ENUMS = (
    "status", "ok", "blocked", "kind", "draft", "twins", "paraphrase", "recheck", "probe", "families", "rows",
    "by_scope", "by_register", "by_source", "sha256", "pairs_checked", "twin_pairs", "pool_families", "reason",
    "answer", "partial", "refuse", "lay", "modelled", "terse", "handbook", "tutorial", "colleague", "legacy",
    "inputs_missing", "task_unclear", "quota_unreachable", "allowed", "denied", "absent", "present",
)

#: Every refusal / error reason the tool can print (enum words).
REASONS = (
    "usage", "internal", "schema_invalid", "inventory_invalid", "input_missing", "input_invalid", "not_sealed",
    "prereg_missing", "prereg_invalid", "prereg_uncommitted", "prereg_modified", "prereg_not_pushed",
    "prereg_unchecked", "round_undeclared", "round1_missing", "already_released", "already_frozen",
    "pool_empty", "pool_changed", "pool_register_missing", "twins_missing", "twins_stale", "twin_in_pool",
    "tutorial_tag_missing", "verdict_invalid", "assignment_changed", "replay_mismatch", "approval_missing",
    "sheet_invalid", "sheet_stale", "sheet_conflict", "recheck_missing", "recheck_stale", "recheck_invalid",
    "phrasing_breach", "band_breach", "strata_breach", "min_share_breach", "group_invariant", "floor_breach",
    "twin_in_sealed", "calibration_short", "colleague_key_invalid", "frozen", "move_recovery_failed",
    "ledger_refused", "ledger_corrupt", "mirror_tampered", "epoch_stop", "ledger_protocol", "no_group",
    "inputs_invalid", "scan_source_invalid", "git_failed", "split_input_invalid", "freeze_infeasible",
    "round_order", "unknown_family", "nothing_to_do",
)
#: Closed set of words the printer accepts as enum values.
ENUM_WORDS = frozenset(
    {
        *SCOPES, *REGISTERS, *SOURCES, *SPLITS, *KINDS, *MODELS, *TARGET_CLASSES, *EXCLUSION_REASONS, *REASONS,
        *RETURN_ENUMS, "none", "invalid", "released", "infeasible", "quota_short", "frozen", "ok", "clean",
        "hits", "already", "recheck_required", "owner_selected_sample", "random_sample_n15", "feasible",
        # split_families constraint names (SplitInfeasible.counts keys)
        "quota_total", "eligibility", "band", "strata", "min_share", "preassignment_conflicts",
        "preassigned_ineligible", "unit_no_allowed_split", "search", "floor", "approvals",
        # prereg-check / inputs-check enums
        "missing", "wrong_type", "out_of_range", "unknown_key", "mismatch", "reused_id", "not_public_id",
        "uninventoried_label", "pairing", "conflicts",
    }
)
ID_FIELDS = frozenset({("audit_hit", "family")})  # the only places an opaque family id is printed
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAMILY_ID = re.compile(r"f[0-9a-f]{8}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FIELD_PATH = re.compile(r"(?:<[a-z]+>|[a-z_]+)(?:\[[0-9]+\]|\.[a-z_]+)*")
_EXT_ID = re.compile(r"[A-Za-z0-9:_.-]{1,64}")  # a family id of any set (public ids are "q:<hex>")
_TAG = re.compile(r"[a-z0-9_]{1,24}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
MAX_PRINT_INT = 10**9


class PrintRefused(Exception):
    """A value the printer will not print (a bug, never data): fixed message, no value."""


class Stop(Exception):
    """A clean stop: an exit code, a reason enum and optional printable (allowlisted) fields."""

    def __init__(self, code: int, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.fields = fields


class Printer:
    """The one allowlisted printer (D72 trusted-processor rule).

    ``emit`` writes ``kind key=value ...`` to stdout; ``error`` writes ``error reason=<enum> ...`` to
    stderr. Values: non-negative ints (at most ``10**9``), sha256 hex, enum words in
    :data:`ENUM_WORDS`, and an opaque family id only for the ``(kind, key)`` pairs in
    :data:`ID_FIELDS`. A ``field`` key takes a schema field path (``evidence[0][1]``) built from
    schema key names. Anything else raises :class:`PrintRefused`.
    """

    def __init__(self, out: Any = None, err: Any = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr

    @staticmethod
    def _value(kind: str, key: str, value: Any) -> str:
        if isinstance(value, bool):
            raise PrintRefused("bool")
        if isinstance(value, int):
            if not 0 <= value <= MAX_PRINT_INT:
                raise PrintRefused("int out of range")
            return str(value)
        if isinstance(value, str):
            if _SHA256.fullmatch(value) or value in ENUM_WORDS:
                return value
            if (kind, key) in ID_FIELDS and _FAMILY_ID.fullmatch(value):
                return value
            if key == "field" and _FIELD_PATH.fullmatch(value) and len(value) <= 64:
                return value
        raise PrintRefused("value is not on the allowlist")

    def _line(self, head: str, fields: Mapping[str, Any], kind: str) -> str:
        parts = [head]
        for key, value in fields.items():
            if not _KEY.fullmatch(key):
                raise PrintRefused("key")
            parts.append(f"{key}={self._value(kind, key, value)}")
        return " ".join(parts)

    def emit(self, name: str, /, **fields: Any) -> None:
        if not _KIND.fullmatch(name):
            raise PrintRefused("kind")
        self.out.write(self._line(name, fields, name) + "\n")

    def error(self, reason: str, /, **fields: Any) -> None:
        if reason not in ENUM_WORDS:
            raise PrintRefused("reason")
        self.err.write(self._line(f"error reason={reason}", fields, "error") + "\n")

    def raw_validated(self, line: str) -> None:
        """Print a line that a validator already proved safe (``return``)."""
        self.out.write(line + "\n")


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------
def canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no spaces, ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically: temp file, fsync, rename, fsync the directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path, reason: str = "input_invalid") -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except OSError:
        raise Stop(EXIT_USAGE, "input_missing") from None
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Stop(EXIT_USAGE, reason) from None


def write_json(path: Path, obj: Any) -> str:
    """Write canonical JSON atomically; returns its sha256."""
    data = (canon(obj) + "\n").encode("ascii")
    write_atomic(path, data)
    return hashlib.sha256(data).hexdigest()


def resolve_split(name: str) -> str:
    if name not in SPLIT_ALIASES:
        raise Stop(EXIT_USAGE, "usage")
    return SPLIT_ALIASES[name]


# ---------------------------------------------------------------------------
# Context: every path is injectable
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    """Where everything lives. Tests pass tmp paths; production uses the defaults."""

    printer: Printer
    private: Path
    repo: Path
    ledger_path: Optional[Path] = None
    mirror: Optional[Path] = None
    inventory_path: Optional[Path] = None
    prereg_path: Optional[Path] = None
    handbook_dump: Optional[Path] = None
    projects_root: Optional[Path] = None
    staging_root: Optional[Path] = None
    _inventory: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # -- locations -------------------------------------------------------------
    @property
    def sealed_dir(self) -> Path:
        return self.private / "sealed"

    @property
    def pool_dir(self) -> Path:
        return self.sealed_dir / "pool"

    @property
    def sealed_work(self) -> Path:
        return self.sealed_dir / "work"

    @property
    def dev_dir(self) -> Path:
        return self.private / "dev"

    @property
    def dev_work(self) -> Path:
        return self.private / "work"

    @property
    def journal(self) -> Path:
        return self.sealed_work / "release.journal"

    def work_for(self, kind: str) -> Path:
        return self.sealed_work if kind == "sealed" else self.dev_work

    @property
    def prereg_file(self) -> Path:
        return self.prereg_path if self.prereg_path is not None else self.repo / "eval" / "prereg_16a2.json"

    @property
    def mirror_file(self) -> Path:
        return self.mirror if self.mirror is not None else self.repo / "eval" / "sealed_ledger.jsonl"

    def inventory(self) -> Dict[str, Any]:
        if self._inventory is None:
            self._inventory = eval_schema.load_inventory(self.inventory_path)
        return self._inventory

    def ledger(self) -> SealedLedger:
        return SealedLedger(self.sealed_dir / "ledger", mirror=self.mirror_file, ledger=self.ledger_path)


def default_ctx(printer: Printer, **overrides: Any) -> Ctx:
    """A context from the (possibly relocated) private root; ``overrides`` replace any field."""
    private = Path(overrides.pop("private", None) or eval_privacy.private_root())
    repo = Path(overrides.pop("repo", None) or private.parent.parent)
    values = {k: (Path(v) if v is not None else None) for k, v in overrides.items()}
    return Ctx(printer=printer, private=private, repo=repo, **values)


# ---------------------------------------------------------------------------
# Rows, families, files
# ---------------------------------------------------------------------------
def row_no(row: Mapping[str, Any]) -> int:
    return int(str(row["id"]).rsplit("-", 1)[1])


def row_key(row: Mapping[str, Any]) -> Tuple[str, int]:
    return (str(row["family_id"]), row_no(row))


@dataclass
class Fam:
    """One family (a scenario, one scope): its rows, where they live and (dev) its split."""

    fid: str
    rows: List[Dict[str, Any]]
    path: Optional[Path] = None
    split: Optional[str] = None

    @property
    def scope(self) -> str:
        return str(self.rows[0]["scope"])

    @property
    def source(self) -> Optional[str]:
        return self.rows[0].get("source")

    @property
    def register(self) -> Optional[str]:
        return self.rows[0].get("register")

    @property
    def primary(self) -> Optional[str]:
        """The primary section: the first label of the first evidence group."""
        evidence = self.rows[0]["evidence"]
        return evidence[0][0] if evidence and evidence[0] else None

    @property
    def chapter(self) -> str:
        if self.scope == "refuse":
            return "refuse"
        chapters = {label.split(".")[0] for group in self.rows[0]["evidence"] for label in group}
        return chapters.pop() if len(chapters) == 1 else "multi"


def group_families(rows: Iterable[Mapping[str, Any]], path: Optional[Path] = None, split: Optional[str] = None) -> Dict[str, Fam]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by.setdefault(str(row["family_id"]), []).append(dict(row))
    return {fid: Fam(fid, sorted(rs, key=row_no), path, split) for fid, rs in sorted(by.items())}


def dump_rows(rows: Iterable[Mapping[str, Any]], *, mark: bool) -> bytes:
    """JSONL bytes for normalised rows: ``None`` keys dropped, ``sealed: true`` iff ``mark``."""
    lines = []
    for row in sorted(rows, key=row_key):
        out = {k: v for k, v in row.items() if v is not None}
        if not out.get("gaps"):
            out.pop("gaps", None)
        if mark:
            out["sealed"] = True
        else:
            out.pop("sealed", None)
        lines.append(json.dumps(out, sort_keys=True, ensure_ascii=True))
    return ("\n".join(lines) + "\n").encode("ascii") if lines else b""


def load_sealed_rows(ctx: Ctx, path: Path) -> List[Dict[str, Any]]:
    """Sealed rows, through the one sanctioned loader."""
    try:
        return load_sealed(path, inventory=ctx.inventory())
    except NotSealedInput:
        raise Stop(EXIT_USAGE, "not_sealed") from None


def load_dev_rows(ctx: Ctx, path: Path) -> List[Dict[str, Any]]:
    return eval_schema.load_v2(path, inventory=ctx.inventory())


def pool_files(ctx: Ctx) -> List[Path]:
    if not ctx.pool_dir.is_dir():
        return []
    return sorted(
        p for p in ctx.pool_dir.rglob("*.jsonl")
        if p.is_file() and not p.is_symlink() and not p.name.startswith("twins")
    )


def sealed_components(ctx: Ctx) -> List[Path]:
    return sorted(p for p in ctx.sealed_dir.glob("sealed_r*.jsonl") if p.is_file()) if ctx.sealed_dir.is_dir() else []


def quarantine_files(ctx: Ctx, kind: str) -> List[Path]:
    base = ctx.sealed_dir if kind == "sealed" else ctx.dev_dir
    return sorted(p for p in base.glob("quarantine*.jsonl") if p.is_file()) if base.is_dir() else []


def quarantine_target(ctx: Ctx, kind: str) -> Path:
    return (ctx.sealed_dir if kind == "sealed" else ctx.dev_dir) / "quarantine.jsonl"


def dev_split_of(path: Path) -> Optional[str]:
    for name in DEV_SPLITS:
        if path.stem.startswith(name):
            return name
    return None


def dev_components(ctx: Ctx) -> List[Path]:
    """Development files that are not frozen outputs and not quarantine."""
    if not ctx.dev_dir.is_dir():
        return []
    frozen = {FROZEN_FILES[s][1] for s in DEV_SPLITS}
    return sorted(
        p for p in ctx.dev_dir.glob("*.jsonl")
        if p.is_file() and p.name not in frozen and not p.name.startswith("quarantine")
    )


def load_pool(ctx: Ctx) -> Dict[str, Fam]:
    fams: Dict[str, Fam] = {}
    for path in pool_files(ctx):
        for fid, fam in group_families(load_sealed_rows(ctx, path), path).items():
            if fid in fams:
                raise Stop(EXIT_USAGE, "input_invalid")
            fams[fid] = fam
    return fams


def quarantined_ids(ctx: Ctx, kind: str) -> Set[str]:
    ids: Set[str] = set()
    for path in quarantine_files(ctx, kind):
        rows = load_sealed_rows(ctx, path) if kind == "sealed" else load_dev_rows(ctx, path)
        ids.update(str(r["family_id"]) for r in rows)
    return ids


def load_sealed_side(ctx: Ctx) -> Dict[str, Fam]:
    """Sealed component families that are not quarantined (survivors)."""
    gone = quarantined_ids(ctx, "sealed")
    fams: Dict[str, Fam] = {}
    for path in sealed_components(ctx):
        for fid, fam in group_families(load_sealed_rows(ctx, path), path, "sealed").items():
            if fid not in gone:
                fams[fid] = fam
    return fams


def load_dev(ctx: Ctx, *, include_quarantine: bool = False) -> Dict[str, Fam]:
    """Development component families (``split`` set from the file-name prefix)."""
    gone = set() if include_quarantine else quarantined_ids(ctx, "development")
    fams: Dict[str, Fam] = {}
    for path in dev_components(ctx):
        for fid, fam in group_families(load_dev_rows(ctx, path), path, dev_split_of(path)).items():
            if fid not in fams and fid not in gone:
                fams[fid] = fam
    return fams


def final_row_hashes(fams: Iterable[Fam]) -> Dict[str, str]:
    return {str(r["id"]): eval_schema.row_hash(r) for f in fams for r in f.rows}


def pool_digest(pool: Mapping[str, Fam]) -> str:
    """sha256 over every pool row's canonical JSON (rows ordered by family and phrasing)."""
    rows = sorted((r for f in pool.values() for r in f.rows), key=row_key)
    return sha_text("\n".join(canon(r) for r in rows))


def source_class(source: Optional[str]) -> str:
    """The source class min_share counts: tutorial and colleague together (D71 Q4)."""
    return "tutorial_or_colleague" if source in ("tutorial", "colleague") else (source or "legacy")


def realistic_ok(fam: Fam) -> bool:
    """Register eligibility: realistic takes lay or terse drafts and tutorial-sourced families."""
    return fam.register in ("lay", "terse") or fam.source == "tutorial"


def family_record(fam: Fam, eligible: Sequence[str]) -> Dict[str, Any]:
    return {
        "family_id": fam.fid, "scope": fam.scope, "chapter": fam.chapter, "source": source_class(fam.source),
        "register": fam.register or "unspecified", "eligible": sorted(eligible),
    }


# ---------------------------------------------------------------------------
# Verdict files, tutorial map, lexical screen
# ---------------------------------------------------------------------------
def parse_verdict(path: Path) -> List[Tuple[str, str]]:
    """The SP's ``twins.jsonl``: one ``{"a", "b", "verdict": "twin"}`` per line; strict."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    pairs: List[Tuple[str, str]] = []
    bad = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        ok = (
            isinstance(obj, dict) and set(obj) == {"a", "b", "verdict"} and obj["verdict"] == "twin"
            and isinstance(obj["a"], str) and isinstance(obj["b"], str)
            and _EXT_ID.fullmatch(obj["a"]) and _EXT_ID.fullmatch(obj["b"]) and obj["a"] != obj["b"]
        )
        if not ok:
            bad += 1
            continue
        pairs.append((obj["a"], obj["b"]))
    if bad:
        raise Stop(EXIT_USAGE, "verdict_invalid", lines=bad)
    return sorted({tuple(sorted(p)) for p in pairs})  # type: ignore[misc]


def load_tutorial_map(ctx: Ctx, path: Optional[Path] = None) -> Dict[str, str]:
    p = path if path is not None else ctx.sealed_work / "tutorial_map.json"
    if not p.is_file():
        return {}
    doc = read_json(p)
    if not isinstance(doc, dict) or not all(isinstance(k, str) and isinstance(v, str) and _TAG.fullmatch(v) for k, v in doc.items()):
        raise Stop(EXIT_USAGE, "input_invalid")
    return dict(doc)


def tokens_of(text: str) -> List[str]:
    return sl.normalise_tokens(text)


def window_set(toks: Sequence[str]) -> Set[Tuple[str, ...]]:
    return {tuple(toks[i : i + sl.WINDOW]) for i in range(len(toks) - sl.WINDOW + 1)}


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


def lexical_pairs(sealed: Mapping[str, Fam], dev: Mapping[str, Fam]) -> List[Tuple[str, str]]:
    """Family pairs ``(sealed, development)`` sharing an 8-token window or a whole short question.

    The lexical screen only *orders* what goes to the SP (R6): it never decides a twin. Uses the
    scanner's tokenisation and window length; no exemption is applied, so the SP sees every pair.
    """
    dev_windows: Dict[Tuple[str, ...], Set[str]] = {}
    dev_short: List[Tuple[str, List[str]]] = []
    dev_all: List[Tuple[str, List[str]]] = []
    for fid, fam in dev.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            dev_all.append((fid, toks))
            if len(toks) < sl.WINDOW:
                dev_short.append((fid, toks))
            for win in window_set(toks):
                dev_windows.setdefault(win, set()).add(fid)
    pairs: Set[Tuple[str, str]] = set()
    for sid, fam in sealed.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            for win in window_set(toks):
                pairs.update((sid, d) for d in dev_windows.get(win, ()))
            if len(toks) < sl.WINDOW:
                pairs.update((sid, d) for d, dt in dev_all if _contains(dt, toks))
            pairs.update((sid, d) for d, dt in dev_short if _contains(toks, dt))
    return sorted(pairs)


def public_tied_drafts(pool: Mapping[str, Fam]) -> Set[str]:
    """Drafts sharing an 8-token window with a public question, or equal to one (R7)."""
    hashes, windows = sl._public_questions()
    tied: Set[str] = set()
    for fid, fam in pool.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            if toks and (sha_text(" ".join(toks)) in hashes or window_set(toks) & windows):
                tied.add(fid)
    return tied


# ---------------------------------------------------------------------------
# Groups: connected components of ALL relations, before any filtering (r4, r5)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    kind: str  # draft | pinned | sealed_pin | quarantine
    tag: Optional[str] = None
    primary: Optional[str] = None


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def components(self) -> List[List[str]]:
        out: Dict[str, List[str]] = {}
        for item in self.parent:
            out.setdefault(self.find(item), []).append(item)
        return sorted(sorted(c) for c in out.values())


@dataclass
class GroupPlan:
    components: List[List[str]]
    status: Dict[str, str]  # draft id -> free | ineligible | dropped
    edges: List[List[str]]
    groups: Dict[str, List[str]]  # group id (smallest member) -> members, for free groups of >= 2


def compute_groups(nodes: Mapping[str, Node], pairs: Iterable[Tuple[str, str]], public_tied: Set[str]) -> GroupPlan:
    """Components of all relations (twin verdict, same source tutorial, same primary section).

    Computed over EVERY node (drafts and pinned families) before anything is filtered. Then, per
    component: a draft in a component holding a round-1 sealed family is ``dropped`` (never joined);
    else a draft in a component holding a pinned development family, or tied to a public question, is
    ``ineligible`` (the whole group, so its edges are never passed); otherwise the drafts are ``free``
    and a chain of edges ties them (the keep-together unit for ``split_families``).
    """
    uf = _UnionFind(nodes)
    external_tied: Set[str] = set(public_tied)
    for a, b in pairs:
        if a in nodes and b in nodes:
            uf.union(a, b)
        elif a in nodes and nodes[a].kind == "draft":
            external_tied.add(a)  # a twin of a public / regression / unknown family
        elif b in nodes and nodes[b].kind == "draft":
            external_tied.add(b)
    for attr in ("tag", "primary"):
        first: Dict[str, str] = {}
        for fid in sorted(nodes):
            value = getattr(nodes[fid], attr)
            if value is None:
                continue
            if value in first:
                uf.union(first[value], fid)
            else:
                first[value] = fid
    status: Dict[str, str] = {}
    edges: List[List[str]] = []
    groups: Dict[str, List[str]] = {}
    comps = uf.components()
    for comp in comps:
        kinds = {nodes[x].kind for x in comp}
        drafts = [x for x in comp if nodes[x].kind == "draft"]
        if not drafts:
            continue
        if "sealed_pin" in kinds:
            verdict = "dropped"
        elif "pinned" in kinds or any(d in external_tied for d in drafts):
            verdict = "ineligible"
        else:
            verdict = "free"
        for d in drafts:
            status[d] = verdict
        if verdict == "free" and len(drafts) > 1:
            edges.extend([drafts[i], drafts[i + 1]] for i in range(len(drafts) - 1))
            groups[drafts[0]] = drafts
    return GroupPlan(comps, status, edges, groups)


# ---------------------------------------------------------------------------
# Pre-registration (b1): shape, quota formulas, power table, pushed gate
# ---------------------------------------------------------------------------
PREREG_KEYS = frozenset(
    {
        "version", "branch", "rounds", "seeds", "quota", "band", "strata", "min_share", "mwe", "alpha",
        "k_max", "draws_r", "power", "primary_metric", "collapse", "adoption", "controls", "slots", "drafting_rule",
    }
)
PREREG_REQUIRED = ("version", "branch", "rounds", "seeds", "quota", "band", "mwe", "alpha", "k_max", "draws_r", "power")
PREREG_ERRORS = ("missing", "wrong_type", "out_of_range", "unknown_key", "mismatch")


def _is_int(x: Any, lo: int = 0) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= lo


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def compute_quotas(quota: Mapping[str, Any], pool_total: int, pre_realistic: int) -> Dict[str, int]:
    """b1's formulas: sealed fixed; realistic = max(floor, preassigned realistic); tuning = the rest."""
    sealed = int(quota["sealed"])
    realistic = max(int(quota["realistic_min"]), int(pre_realistic))
    return {"sealed": sealed, "realistic": realistic, "tuning": int(pool_total) - sealed - realistic}


def mc_power_pm(groups: Sequence[int], disc: float, effect: float, alpha: float, draws: int, seed: int, model: str) -> int:
    return round(1000 * eval_stats.cluster_signflip_power(groups, disc, effect, alpha, draws, seed, model))  # type: ignore[arg-type]


def mc_mde_hundredths(groups: Sequence[int], disc: float, alpha: float, draws: int, seed: int, model: str, target: float = 0.8) -> Optional[int]:
    """Smallest effect k/100 (k >= 1, up to the discordance) whose Monte Carlo power reaches ``target``."""
    k = 1
    while k <= disc * 100 + 1e-9:
        effect = min(k / 100, disc)
        if eval_stats.cluster_signflip_power(groups, disc, effect, alpha, draws, seed, model) >= target:  # type: ignore[arg-type]
            return k
        k += 1
    return None


def power_cells(power: Mapping[str, Any], mwe: float) -> List[Dict[str, Any]]:
    """The prereg's power table, recomputed: model-major, one cell per discordance."""
    cells = []
    for model in MODELS:
        for disc in power["discordance"]:
            effect = min(mwe, disc)
            args = (power["planned_groups"], disc)
            cells.append(
                {
                    "model": model, "discordance": disc,
                    "power_pm": mc_power_pm(*args, effect, power["alpha"], power["draws"], power["seed"], model),
                    "mde_hundredths": mc_mde_hundredths(*args, power["alpha"], power["draws"], power["seed"], model),
                }
            )
    return cells


def validate_prereg(doc: Any, *, recompute: bool = True) -> List[Tuple[str, str]]:
    """``(field path, error enum)`` for a prereg document; empty = valid. Recomputes quota examples and power table."""
    if not isinstance(doc, dict):
        return [("<doc>", "wrong_type")]
    errors: List[Tuple[str, str]] = [("<unknown>", "unknown_key") for k in doc if k not in PREREG_KEYS]
    for key in PREREG_REQUIRED:
        if key not in doc:
            errors.append((key, "missing"))
    if errors:
        return errors

    def need(path: str, ok: bool, reason: str = "wrong_type") -> bool:
        if not ok:
            errors.append((path, reason))
        return ok

    need("version", doc["version"] == 1, "out_of_range")
    need("branch", isinstance(doc["branch"], str) and bool(_BRANCH.fullmatch(doc["branch"])) and ".." not in doc["branch"])
    rounds = doc["rounds"]
    need("rounds", isinstance(rounds, list) and bool(rounds) and all(r in (1, 2) and not isinstance(r, bool) for r in rounds)
         and len(set(rounds)) == len(rounds))
    seeds = doc["seeds"]
    if need("seeds", isinstance(seeds, dict)):
        for key in ("split_r1", "split_r2", "repair", "calibration"):
            need(f"seeds.{key}", key in seeds and isinstance(seeds[key], int) and not isinstance(seeds[key], bool))
    quota = doc["quota"]
    if need("quota", isinstance(quota, dict)):
        keys = ("sealed", "spare", "floor_sealed", "floor_answerable", "realistic_min", "tuning_min")
        if all(need(f"quota.{k}", _is_int(quota.get(k), 0)) for k in keys):
            need("quota.sealed", quota["sealed"] == quota["floor_sealed"] + quota["spare"], "mismatch")
            examples = quota.get("examples", [])
            if need("quota.examples", isinstance(examples, list)):
                for i, ex in enumerate(examples):
                    ok = isinstance(ex, dict) and set(ex) == {"pool_total", "preassigned_realistic", "sealed", "realistic", "tuning"}
                    if need(f"quota.examples[{i}]", ok) and recompute:
                        want = compute_quotas(quota, ex["pool_total"], ex["preassigned_realistic"])
                        need(f"quota.examples[{i}]", all(ex[k] == want[k] for k in want), "mismatch")
    band = doc["band"]
    need("band", band == {"min_pct": 25, "max_pct": 30}, "mismatch")
    need("mwe", _is_num(doc["mwe"]) and 0 < doc["mwe"] <= 1, "out_of_range")
    need("alpha", _is_num(doc["alpha"]) and 0 < doc["alpha"] < 1, "out_of_range")
    need("k_max", _is_int(doc["k_max"], 1), "out_of_range")
    need("draws_r", _is_int(doc["draws_r"], 1), "out_of_range")
    pw = doc["power"]
    if need("power", isinstance(pw, dict)):
        ok = (
            set(pw) <= {"alpha", "discordance", "planned_groups", "draws", "seed", "table"}
            and _is_num(pw.get("alpha")) and 0 < pw["alpha"] < 1
            and isinstance(pw.get("discordance"), list) and bool(pw["discordance"])
            and all(_is_num(d) and 0 < d <= 1 for d in pw["discordance"])
            and isinstance(pw.get("planned_groups"), list) and bool(pw["planned_groups"])
            and all(_is_int(g, 1) for g in pw["planned_groups"])
            and _is_int(pw.get("draws"), 1) and isinstance(pw.get("seed"), int) and not isinstance(pw.get("seed"), bool)
        )
        if need("power", ok) and recompute and not errors:
            want_cells = power_cells(pw, float(doc["mwe"]))
            table = pw.get("table")
            same = (
                isinstance(table, list) and len(table) == len(want_cells)
                and all(isinstance(t, dict) and all(t.get(k) == w[k] for k in w) for t, w in zip(table, want_cells))
            )
            need("power.table", same, "mismatch")
    strata, min_share = doc.get("strata", {}), doc.get("min_share", {})
    need("strata", isinstance(strata, dict) and all(s in SPLITS and isinstance(m, dict) for s, m in strata.items()))
    need("min_share", isinstance(min_share, dict) and all(s in SPLITS and isinstance(m, dict) for s, m in min_share.items()))
    return errors


def load_prereg(ctx: Ctx) -> Dict[str, Any]:
    path = ctx.prereg_file
    if not path.is_file():
        raise Stop(EXIT_USAGE, "prereg_missing")
    doc = read_json(path, "prereg_invalid")
    errors = validate_prereg(doc, recompute=False)
    if errors:
        raise Stop(EXIT_USAGE, "prereg_invalid", errors=len(errors))
    return doc


def _git_out(repo: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, env=env)
    except OSError:
        raise Stop(EXIT_REFUSED, "prereg_unchecked") from None
    if proc.returncode != 0:
        raise Stop(EXIT_REFUSED, "prereg_uncommitted")
    return proc.stdout


def prereg_commit(ctx: Ctx) -> str:
    """The commit that last changed the prereg file; the working file must equal that commit's copy."""
    try:
        rel = ctx.prereg_file.resolve().relative_to(ctx.repo.resolve()).as_posix()
    except ValueError:
        raise Stop(EXIT_REFUSED, "prereg_uncommitted") from None
    sha = _git_out(ctx.repo, "log", "-1", "--format=%H", "--", rel).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Stop(EXIT_REFUSED, "prereg_uncommitted")
    if _git_out(ctx.repo, "show", f"{sha}:{rel}") != ctx.prereg_file.read_bytes():
        raise Stop(EXIT_REFUSED, "prereg_modified")
    return sha


def gate_prereg(ctx: Ctx, prereg: Mapping[str, Any], round_no: int) -> str:
    """``split`` runs only if the prereg declares the round AND is pushed (a7's ancestry rule)."""
    if round_no not in prereg["rounds"]:
        raise Stop(EXIT_REFUSED, "round_undeclared")
    sha = prereg_commit(ctx)
    try:
        sp.check_prereg(sha, str(prereg["branch"]), repo=ctx.repo)
    except sp.LaunchError:
        raise Stop(EXIT_REFUSED, "prereg_not_pushed") from None
    return sha


# ---------------------------------------------------------------------------
# ids, digest, count-migrated, inputs-check
# ---------------------------------------------------------------------------
_FID_ANY = re.compile(r"f[0-9a-f]{8}")


def known_family_ids(ctx: Ctx) -> Set[str]:
    """Every family-id-shaped token in the private root's JSON / JSONL / text files (no loaders)."""
    found: Set[str] = set()
    if ctx.private.is_dir():
        for path in ctx.private.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".jsonl", ".json", ".txt", ".csv"):
                try:
                    found.update(_FID_ANY.findall(path.read_bytes().decode("utf-8", errors="replace")))
                except OSError:
                    continue
    return found


def new_family_id() -> str:
    """One fresh opaque family id: ``f`` + 8 random hex digits (``secrets``)."""
    return "f" + secrets.token_hex(4)


def cmd_ids(ctx: Ctx, count: int, out: Optional[str]) -> int:
    """Issue ``count`` fresh opaque family ids for the SP's ``ids.txt``; print the count and sha256 only."""
    if not 1 <= count <= 100_000:
        raise Stop(EXIT_USAGE, "usage")
    taken = known_family_ids(ctx)
    ids: List[str] = []
    seen: Set[str] = set()
    while len(ids) < count:
        cand = new_family_id()
        if cand not in taken and cand not in seen:
            seen.add(cand)
            ids.append(cand)
    target = Path(out) if out else ctx.sealed_work / "ids.txt"
    write_atomic(target, ("\n".join(ids) + "\n").encode("ascii"))
    issued = ctx.sealed_work / "issued_ids.txt"
    previous = issued.read_bytes() if issued.is_file() else b""
    write_atomic(issued, previous + ("\n".join(ids) + "\n").encode("ascii"))
    ctx.printer.emit("ids", count=len(ids), sha256=sha_file(target))
    return EXIT_OK


def cmd_digest(ctx: Ctx, path: str) -> int:
    """sha256, rows, families and counts by scope, register and source; chapters never for sealed."""
    p = Path(path)
    sealed = eval_sets.is_sealed(p)
    rows = load_sealed_rows(ctx, p) if sealed else load_dev_rows(ctx, p)
    fams = group_families(rows)
    fields: Dict[str, Any] = {"sha256": sha_file(p), "rows": len(rows), "families": len(fams)}
    for scope in SCOPES:
        fields[f"scope_{scope}"] = sum(1 for f in fams.values() if f.scope == scope)
    for reg in (*REGISTERS, None):
        fields[f"reg_{reg or 'none'}"] = sum(1 for f in fams.values() if f.register == reg)
    for src in (*SOURCES, None):
        fields[f"src_{src or 'none'}"] = sum(1 for f in fams.values() if f.source == src)
    if not sealed:
        chapters: Dict[str, int] = {}
        for fam in fams.values():
            ch = fam.chapter
            key = f"ch_{ch}" if ch.isdigit() and len(ch) <= 3 else "ch_other"
            chapters[key] = chapters.get(key, 0) + 1
        fields.update(sorted(chapters.items()))
    ctx.printer.emit("digest", kind="sealed" if sealed else "development", **fields)
    return EXIT_OK


def _v1_families(path: Path) -> Dict[str, bool]:
    """``{family id: is_refusal}`` for a golden / realistic file (v1 rows or v2 families)."""
    rows = eval_schema.load_any(path)
    out: Dict[str, bool] = {}
    for row in rows:
        out[str(row["family_id"])] = out.get(str(row["family_id"]), False) or row["scope"] == "refuse"
    return out


def _tutorial_exercises(path: Path) -> Dict[str, bool]:
    """T1 / T7 rows grouped by exercise (the id without its phrasing letter): ``{exercise: is_refusal}``."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    out: Dict[str, bool] = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise Stop(EXIT_USAGE, "input_invalid") from None
        rid = obj.get("id") if isinstance(obj, dict) else None
        if not isinstance(rid, str) or len(rid) < 2:
            raise Stop(EXIT_USAGE, "input_invalid")
        refusal = obj.get("type") == "refusal" or obj.get("scope") == "refuse"
        out[rid[:-1]] = out.get(rid[:-1], False) or refusal
    return out


def cmd_count_migrated(ctx: Ctx, golden: str, realistic: str, pairing: str, tutorial: str,
                       quota_tuning: Optional[int], quota_realistic: Optional[int]) -> int:
    """b2's counting: families per development split and each split's refusal deficit (to 25 % of its quota)."""
    gold, real, tut = _v1_families(Path(golden)), _v1_families(Path(realistic)), _tutorial_exercises(Path(tutorial))
    doc = read_json(Path(pairing))
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, list) or not all(isinstance(p, list) and len(p) == 2 for p in pairs):
        raise Stop(EXIT_USAGE, "input_invalid")
    paired_gold: Set[str] = set()
    conflicts = 0
    real_refuse = dict(real)
    for g, r in pairs:
        if g not in gold or r not in real or g in paired_gold:
            raise Stop(EXIT_USAGE, "inputs_invalid")
        paired_gold.add(g)
        if gold[g] != real[r]:
            conflicts += 1
        real_refuse[r] = real[r] or gold[g]
    tuning_refuse = sum(1 for g, refuse in gold.items() if g not in paired_gold and refuse)
    tuning_n = sum(1 for g in gold if g not in paired_gold)
    realistic_n = len(real) + len(tut)
    realistic_refuse = sum(real_refuse.values()) + sum(tut.values())
    quotas: Dict[str, int] = {}
    prereg = ctx.prereg_file
    quota_doc = read_json(prereg).get("quota", {}) if prereg.is_file() and quota_tuning is None else {}
    quotas["tuning"] = quota_tuning if quota_tuning is not None else int(quota_doc.get("tuning_min", 0))
    quotas["realistic"] = quota_realistic if quota_realistic is not None else max(int(quota_doc.get("realistic_min", 0)), realistic_n)

    def deficit(q: int, refuse: int) -> int:
        return max(0, -(-q * BAND_MIN_NUM // BAND_MIN_DEN) - refuse)

    ctx.printer.emit(
        "count_migrated", pairs=len(pairs), pair_scope_conflicts=conflicts, tutorial_exercises=len(tut),
        tuning_families=tuning_n, tuning_refuse=tuning_refuse, tuning_quota=quotas["tuning"],
        tuning_deficit=deficit(quotas["tuning"], tuning_refuse),
        realistic_families=realistic_n, realistic_refuse=realistic_refuse, realistic_quota=quotas["realistic"],
        realistic_deficit=deficit(quotas["realistic"], realistic_refuse),
    )
    return EXIT_OK


def cmd_inputs_check(ctx: Ctx, pairing: Optional[str], conflicts: Optional[str], golden_ids: Optional[str], realistic_ids: Optional[str]) -> int:
    """b0: ``eval/v2_pairing.json`` and ``eval/conflict_sections.json`` (a8's :mod:`src.sealed_inputs`)."""

    def ids_of(path: Optional[str]) -> Optional[List[str]]:
        if path is None:
            return None
        try:
            return [x for x in Path(path).read_text(encoding="utf-8").split() if x]
        except (OSError, UnicodeDecodeError):
            raise Stop(EXIT_USAGE, "input_missing") from None

    pairing_file = Path(pairing) if pairing else sealed_inputs.PAIRING_PATH
    conflict_file = Path(conflicts) if conflicts else sealed_inputs.CONFLICT_PATH
    try:
        counts = sealed_inputs.inputs_check(
            pairing_file, conflict_file, inventory=ctx.inventory(),
            golden_ids=ids_of(golden_ids), realistic_ids=ids_of(realistic_ids),
        )
    except sealed_inputs.InputsError as exc:
        for name, path, message in exc.errors:
            source = "pairing" if name == pairing_file.name else "conflicts"
            if "already used" in message:
                why = "reused_id"
            elif "not a public" in message:
                why = "not_public_id"
            elif "inventory" in message:
                why = "uninventoried_label"
            else:
                why = "invalid"
            ctx.printer.emit("inputs_error", source=source, field=path if _FIELD_PATH.fullmatch(path) else "other", why=why)
        raise Stop(EXIT_USAGE, "inputs_invalid", errors=len(exc.errors)) from None
    ctx.printer.emit("inputs_check", status="ok", pairs=counts["pairs"], labels=counts["labels"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# twins
# ---------------------------------------------------------------------------
def load_quarantine(ctx: Ctx, kind: str) -> Dict[str, Fam]:
    fams: Dict[str, Fam] = {}
    for path in quarantine_files(ctx, kind):
        rows = load_sealed_rows(ctx, path) if kind == "sealed" else load_dev_rows(ctx, path)
        fams.update(group_families(rows, path))
    return fams


def _check_tags(fams: Iterable[Fam], tags: Mapping[str, str]) -> None:
    missing = sum(1 for f in fams if f.source == "tutorial" and f.fid not in tags)
    if missing:
        raise Stop(EXIT_USAGE, "tutorial_tag_missing", families=missing)


def build_supply(ctx: Ctx, round_no: int, pool: Mapping[str, Fam], tags: Mapping[str, str], pairs: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """Nodes, groups and family records for a round: the shared core of ``twins`` and ``split``."""
    pinned = {fid: f for fid, f in load_dev(ctx).items() if f.split in DEV_SPLITS}
    sealed_pin = load_sealed_side(ctx) if round_no == 2 else {}
    quarantine = load_quarantine(ctx, "sealed")
    for fam in pool.values():
        if fam.register not in REGISTERS or fam.source not in SOURCES:
            raise Stop(EXIT_USAGE, "pool_register_missing")
    _check_tags([*pool.values(), *pinned.values(), *sealed_pin.values()], tags)
    nodes: Dict[str, Node] = {}
    for kind, fams in (("quarantine", quarantine), ("sealed_pin", sealed_pin), ("pinned", pinned), ("draft", pool)):
        for fid, fam in fams.items():
            nodes[fid] = Node(kind, tags.get(fid), fam.primary)
    plan = compute_groups(nodes, pairs, public_tied_drafts(pool))
    records: Dict[str, Dict[str, Any]] = {}
    preassigned: Dict[str, str] = {}
    for fid, fam in pool.items():
        status = plan.status[fid]
        if status == "dropped":
            continue
        eligible = ["tuning"] + (["realistic"] if realistic_ok(fam) else []) + (["sealed"] if status == "free" else [])
        records[fid] = family_record(fam, eligible)
    for fid, fam in pinned.items():
        records[fid] = family_record(fam, [fam.split])  # type: ignore[list-item]
        preassigned[fid] = str(fam.split)
    for fid, fam in sealed_pin.items():
        records[fid] = family_record(fam, ["sealed"])
        preassigned[fid] = "sealed"
    dropped = sorted(f for f, s in plan.status.items() if s == "dropped")
    ineligible = sorted(f for f, s in plan.status.items() if s == "ineligible")
    draft_ids = sorted(f for f in pool if f in records)
    pin_ids = sorted(preassigned)
    batches = {name: ids for name, ids in (("pool", draft_ids), ("pinned", pin_ids)) if ids}
    names = sorted(batches)
    twins = {
        "batches": batches,
        "families_digest": families_digest(sorted(records), names),
        "pairs_covered": [[names[i], names[j]] for i in range(len(names)) for j in range(i, len(names))],
        "edges": plan.edges,
    }
    return {
        "records": records, "preassigned": preassigned, "twins": twins, "groups": plan.groups,
        "ineligible": ineligible, "dropped": dropped, "pinned_fams": pinned, "sealed_pin_fams": sealed_pin,
    }


def cmd_twins(ctx: Ctx, round_no: int, verdict: str, dev_verdict: Optional[str], tutorials: Optional[str]) -> int:
    """Record the SP's all-pairs verdict as 16A-1's ``twins`` record under the Unit rule."""
    if round_no not in (1, 2):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    ledger = ctx.ledger()
    pool = load_pool(ctx)
    if not pool:
        raise Stop(EXIT_REFUSED, "pool_empty")
    sp_pairs = parse_verdict(Path(verdict))
    inside = [p for p in sp_pairs if p[0] in pool and p[1] in pool]
    if inside:
        raise Stop(EXIT_REFUSED, "twin_in_pool", pairs=len(inside))
    all_pairs = sorted({*sp_pairs, *(parse_verdict(Path(dev_verdict)) if dev_verdict else [])})
    tags = load_tutorial_map(ctx, Path(tutorials) if tutorials else None)
    supply = build_supply(ctx, round_no, pool, tags, all_pairs)
    digest = pool_digest(pool)
    record = {
        "version": 1, "round": round_no, "pool_digest": digest, "verdict_sha256": sha_file(Path(verdict)),
        "twin_pairs": [list(p) for p in all_pairs], **supply["twins"],
        "ineligible": supply["ineligible"], "dropped": supply["dropped"], "groups": supply["groups"],
    }
    name = f"twins_r{round_no}_{digest[:16]}"
    sha = ledger.store_payload(name, record)
    write_json(ctx.sealed_work / f"twins_current_r{round_no}.json", {"payload": name, "sha256": sha, "pool_digest": digest})
    ctx.printer.emit(
        "twins", round=round_no, pool_families=len(pool), twin_pairs=len(all_pairs), groups=len(supply["groups"]),
        ineligible=len(supply["ineligible"]), dropped=len(supply["dropped"]), edges=len(supply["twins"]["edges"]), sha256=sha,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# split and release
# ---------------------------------------------------------------------------
def assign_name(round_no: int) -> str:
    return f"assign_r{round_no}"


def release_paths(ctx: Ctx, round_no: int) -> Dict[str, Path]:
    return {
        "sealed": ctx.sealed_dir / f"sealed_r{round_no}.jsonl",
        "tuning": ctx.dev_dir / f"tuning_r{round_no}.jsonl",
        "realistic": ctx.dev_dir / f"realistic_r{round_no}.jsonl",
        "dropped": ctx.sealed_dir / f"quarantine_r{round_no}.jsonl",
    }


def execute_release(ctx: Ctx, round_no: int, payload: Mapping[str, Any]) -> None:
    """Move a released round out of ``sealed/pool/``: stage, then one journaled transaction.

    Development families go to ``dev/`` **unmarked** (the only sanctioned unmarking); sealed ones to
    ``sealed/sealed_r<R>.jsonl`` and dropped ones to ``sealed/quarantine_r<R>.jsonl`` (both marked); the
    consumed pool files are moved aside inside the same transaction. The copy, fsync, verify and delete
    run under ``sp_launch.journaled_move``; a crash is finished by :func:`recover_state`. Idempotent.
    """
    marker = ctx.sealed_work / f"release_r{round_no}.done"
    if marker.exists():
        return
    if ctx.journal.exists():
        sp.recover_moves(ctx.journal)
    dests = release_paths(ctx, round_no)
    assignment: Mapping[str, str] = payload["assignment"]
    expected = {name for name in set(assignment.values())} | ({"dropped"} if payload["dropped"] else set())
    stage = ctx.sealed_work / f"stage_r{round_no}"
    consumed = ctx.sealed_work / f"consumed_r{round_no}"
    if not (all(dests[n].exists() for n in expected) and not pool_files(ctx)):
        shutil.rmtree(stage, ignore_errors=True)
        pool = load_pool(ctx)
        if pool_digest(pool) != payload["pool_digest"]:
            raise Stop(EXIT_REFUSED, "pool_changed")
        buckets: Dict[str, List[Dict[str, Any]]] = {n: [] for n in dests}
        for fid, fam in pool.items():
            name = assignment.get(fid) or ("dropped" if fid in payload["dropped"] else None)
            if name is None:
                raise Stop(EXIT_REFUSED, "pool_changed")
            buckets[name].extend(fam.rows)
        pairs: List[Tuple[Path, Path]] = []
        for name, rows in buckets.items():
            if rows:
                staged = stage / dests[name].name
                write_atomic(staged, dump_rows(rows, mark=name in ("sealed", "dropped")))
                pairs.append((staged, dests[name]))
        pairs.extend((p, consumed / f"{i:03d}.consumed") for i, p in enumerate(pool_files(ctx)))
        sp.journaled_move(pairs, ctx.journal)
    shutil.rmtree(consumed, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    write_atomic(marker, b"done\n")


def recover_state(ctx: Ctx) -> None:
    """Finish a crashed move journal and every recorded release that was not completed."""
    try:
        if ctx.journal.exists():
            sp.recover_moves(ctx.journal)
        ledger = ctx.ledger()
        for rec in ledger.records("release"):
            execute_release(ctx, int(rec["round"]), ledger.load_payload(assign_name(int(rec["round"])), rec["payload_sha256"]))
    except (sp.MoveError, OSError):
        raise Stop(EXIT_LEDGER_STOP, "move_recovery_failed") from None


def _read_pointer(ctx: Ctx, round_no: int) -> Dict[str, Any]:
    path = ctx.sealed_work / f"twins_current_r{round_no}.json"
    if not path.is_file():
        raise Stop(EXIT_REFUSED, "twins_missing")
    ptr = read_json(path)
    if not (isinstance(ptr, dict) and set(ptr) == {"payload", "sha256", "pool_digest"}):
        raise Stop(EXIT_REFUSED, "twins_missing")
    return ptr


def _violated(node: Any) -> bool:
    """Whether a ``SplitInfeasible.counts`` subtree holds a violated constraint (counts only, no content)."""
    if isinstance(node, Mapping):
        return node.get("violated") is True or any(_violated(v) for v in node.values())
    return False


def cmd_split(ctx: Ctx, round_no: int) -> int:
    """One split attempt: infeasible releases nothing (recorded); the first feasible attempt is released."""
    if round_no not in (1, 2):
        raise Stop(EXIT_USAGE, "usage")
    prereg = load_prereg(ctx)
    gate_prereg(ctx, prereg, round_no)
    recover_state(ctx)
    ledger = ctx.ledger()
    releases = {int(r["round"]): r for r in ledger.records("release")}
    if round_no in releases:
        raise Stop(EXIT_REFUSED, "already_released")
    if round_no == 2 and 1 not in releases:
        raise Stop(EXIT_REFUSED, "round1_missing")
    if ledger.freeze_record("sealed") is not None:
        raise Stop(EXIT_REFUSED, "already_frozen")
    pool = load_pool(ctx)
    if not pool:
        raise Stop(EXIT_REFUSED, "pool_empty")
    digest = pool_digest(pool)
    ptr = _read_pointer(ctx, round_no)
    if ptr["pool_digest"] != digest:
        raise Stop(EXIT_REFUSED, "twins_stale")
    record = ledger.load_payload(ptr["payload"], ptr["sha256"])
    tags = load_tutorial_map(ctx)
    pairs = [(a, b) for a, b in record["twin_pairs"]]
    supply = build_supply(ctx, round_no, pool, tags, pairs)
    twins = supply["twins"]
    if (twins["families_digest"], twins["edges"], supply["dropped"]) != (record["families_digest"], record["edges"], record["dropped"]):
        raise Stop(EXIT_REFUSED, "twins_stale")  # development or sealed state moved since `twins`
    records = supply["records"]
    total = len(records)
    quota_doc = prereg["quota"]
    if round_no == 1:
        quotas = compute_quotas(quota_doc, total, sum(1 for s in supply["preassigned"].values() if s == "realistic"))
    else:
        prior = ledger.load_payload(assign_name(1), releases[1]["payload_sha256"])["quotas"]
        quotas = {"sealed": prior["sealed"], "realistic": prior["realistic"]}
        quotas["tuning"] = total - quotas["sealed"] - quotas["realistic"]
    attempts = sum(1 for r in ledger.records("split_attempt") if r["round"] == round_no)
    if quotas["tuning"] < int(quota_doc["tuning_min"]):
        ledger.record_split_attempt(round_no=round_no, pool_digest=digest, outcome="quota_short")
        ctx.printer.emit("split", round=round_no, outcome="quota_short", attempt=attempts + 1)
        return EXIT_SPLIT_INFEASIBLE
    seed = int(prereg["seeds"][f"split_r{round_no}"])
    strata, min_share = prereg.get("strata") or None, prereg.get("min_share") or None
    try:
        assignment = split_families(
            [records[f] for f in sorted(records)], seed, quotas, strata, twins=twins,
            preassigned=supply["preassigned"], min_share=min_share,
        )
    except SplitInfeasible as exc:
        ledger.record_split_attempt(round_no=round_no, pool_digest=digest, outcome="infeasible")
        ctx.printer.emit("split", round=round_no, outcome="infeasible", attempt=attempts + 1)
        for name, node in sorted(exc.counts.items()):
            if name in ENUM_WORDS and _violated(node):
                ctx.printer.emit("split_constraint", constraint=name)
        return EXIT_SPLIT_INFEASIBLE
    except SplitInputError:
        raise Stop(EXIT_USAGE, "split_input_invalid") from None
    payload = {
        "version": 1, "round": round_no, "seed": seed, "quotas": quotas, "strata": strata, "min_share": min_share,
        "pool_digest": digest, "families": [records[f] for f in sorted(records)], "preassigned": supply["preassigned"],
        "twins": twins, "assignment": assignment, "groups": supply["groups"], "ineligible": supply["ineligible"],
        "dropped": supply["dropped"], "twins_payload": ptr["payload"], "twins_sha256": ptr["sha256"],
    }
    sha = ledger.store_payload(assign_name(round_no), payload)
    ledger.record_split_attempt(round_no=round_no, pool_digest=digest, outcome="feasible", payload_sha256=sha)
    totals = {s: sum(1 for v in assignment.values() if v == s) for s in SPLITS}
    new_totals = {s: sum(1 for f, v in assignment.items() if v == s and f in pool) for s in SPLITS}
    ledger.record_release(
        round_no=round_no, assignment_digest=sha_text(canon(assignment)),
        totals={**new_totals, "families": total, "dropped": len(supply["dropped"])}, payload_sha256=sha,
    )
    execute_release(ctx, round_no, payload)
    ctx.printer.emit(
        "split", round=round_no, outcome="released", attempt=attempts + 1, sealed=totals["sealed"], tuning=totals["tuning"],
        realistic=totals["realistic"], groups=len(supply["groups"]), dropped=len(supply["dropped"]), sha256=sha,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Approvals, sheets, apply
# ---------------------------------------------------------------------------
def approvals_path(ctx: Ctx, kind: str) -> Path:
    return ctx.work_for(kind) / "approvals.json"


def load_approvals(ctx: Ctx, kind: str) -> Dict[str, Any]:
    path = approvals_path(ctx, kind)
    if not path.is_file():
        return {"version": 1, "approved": {}, "pending_confirm": {}, "rejected": [], "escalated": []}
    doc = read_json(path)
    if not (isinstance(doc, dict) and doc.get("version") == 1 and isinstance(doc.get("approved"), dict)):
        raise Stop(EXIT_USAGE, "input_invalid")
    for key, default in (("pending_confirm", {}), ("rejected", []), ("escalated", [])):
        doc.setdefault(key, default)
    return doc


def save_approvals(ctx: Ctx, kind: str, doc: Mapping[str, Any]) -> None:
    write_json(approvals_path(ctx, kind), doc)


def kind_components(ctx: Ctx, kind: str) -> List[Path]:
    return sealed_components(ctx) if kind == "sealed" else dev_components(ctx)


def load_kind(ctx: Ctx, kind: str) -> Tuple[Dict[Path, List[Dict[str, Any]]], Set[str]]:
    """``({component file: its rows}, quarantined family ids)`` for sealed or development."""
    gone = quarantined_ids(ctx, kind)
    files: Dict[Path, List[Dict[str, Any]]] = {}
    for path in kind_components(ctx, kind):
        loader = load_sealed_rows if kind == "sealed" else load_dev_rows
        files[path] = loader(ctx, path)
    return files, gone


def live_rows(files: Mapping[Path, List[Dict[str, Any]]], gone: Set[str]) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    return {str(r["id"]): (p, r) for p, rows in files.items() for r in rows if str(r["family_id"]) not in gone}


def needs_approval(kind: str, row: Mapping[str, Any]) -> bool:
    """Sealed rows always; development rows unless a plain legacy first phrasing (b5's validation list)."""
    if kind == "sealed":
        return True
    plain_legacy = row.get("source") == "legacy" and row_no(row) == 0 and row["scope"] != "refuse" and not row.get("ambiguous")
    return not plain_legacy


def split_of_file(kind: str, path: Path) -> Optional[str]:
    return "sealed" if kind == "sealed" else dev_split_of(path)


def assert_not_frozen(ledger: SealedLedger, kind: str, paths: Iterable[Path]) -> None:
    for path in paths:
        split = split_of_file(kind, path)
        if split is not None and ledger.freeze_record(split) is not None:
            raise Stop(EXIT_REFUSED, "frozen")


def load_pages(ctx: Ctx) -> Dict[str, str]:
    """``{section label: "p12" | "p12-14"}`` from the handbook dump (b0); empty if absent."""
    dump = ctx.handbook_dump if ctx.handbook_dump is not None else ctx.repo / sl.HANDBOOK_DUMP
    pages: Dict[str, str] = {}
    try:
        text = dump.read_bytes().decode("utf-8", errors="replace") if dump.is_file() else ""
    except OSError:
        return pages
    for line in text.split("\n"):
        try:
            obj = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        label, ps, pe = obj.get("label"), obj.get("page_start"), obj.get("page_end")
        if isinstance(label, str) and isinstance(ps, int) and isinstance(pe, int):
            pages[label] = f"p{ps}" if ps == pe else f"p{ps}-{pe}"
    return pages


def render_evidence(evidence: Sequence[Sequence[str]], pages: Mapping[str, str]) -> str:
    """AND groups joined by `` & ``, OR alternatives by `` | ``, each label with its pages."""
    return " & ".join(" | ".join(f"{lab} [{pages[lab]}]" if lab in pages else lab for lab in group) for group in evidence)


def render_gaps(gaps: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{g['id']}: {', '.join(g['keywords'])}" for g in gaps)


def csv_bytes(columns: Sequence[str], records: Iterable[Mapping[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow({c: rec.get(c, "") for c in columns})
    return buf.getvalue().encode("utf-8")


def read_sheet(path: Path, columns: Sequence[str]) -> Tuple[List[Dict[str, str]], str]:
    """The sheet's lines and its sha256; the header must be exactly ``columns``."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if list(reader.fieldnames or []) != list(columns):
        raise Stop(EXIT_USAGE, "sheet_invalid")
    lines = [{k: (v or "") for k, v in rec.items() if k is not None} for rec in reader]
    return lines, hashlib.sha256(raw).hexdigest()


def ticked(cell: str) -> bool:
    return cell.strip().lower() in ("x", "1", "y", "yes", "true", "ok")


def sheets_dir(ctx: Ctx, kind: str) -> Path:
    return ctx.work_for(kind) / "sheets"


def cmd_sheet(ctx: Ctx, kind: str, round_no: Optional[int], name: Optional[str], include_all: bool) -> int:
    """A review sheet (CSV): one line per phrasing, owner columns A / C / R / E, corrections, colleague's key."""
    recover_state(ctx)
    files, gone = load_kind(ctx, kind)
    approvals = load_approvals(ctx, kind)
    pages = load_pages(ctx)
    out: List[Dict[str, Any]] = []
    for path, rows in files.items():
        if round_no is not None and path.name not in (f"sealed_r{round_no}.jsonl", f"tuning_r{round_no}.jsonl", f"realistic_r{round_no}.jsonl"):
            continue
        for row in rows:
            rid, h = str(row["id"]), eval_schema.row_hash(row)
            if str(row["family_id"]) in gone or not needs_approval(kind, row):
                continue
            bound = approvals["approved"].get(rid, {}).get("row_hash") == h
            pending = approvals["pending_confirm"].get(rid, {}).get("row_hash") == h
            if (bound or pending) and not include_all:
                continue
            out.append(
                {
                    "row_id": rid, "family_id": row["family_id"], "row_hash": h, "phrasing_no": row_no(row),
                    "phrasing": row["question"], "scope": row["scope"], "register": row.get("register") or "",
                    "source": row.get("source") or "", "evidence": render_evidence(row["evidence"], pages),
                    "gaps": render_gaps(row["gaps"]), "ambiguous": "yes" if row.get("ambiguous") else "no",
                }
            )
    out.sort(key=lambda r: (r["family_id"], r["phrasing_no"]))
    label = name or f"sheet_{kind}_{'r' + str(round_no) if round_no else 'all'}"
    target = sheets_dir(ctx, kind) / f"{label}.csv"
    write_atomic(target, csv_bytes(SHEET_COLUMNS, out))
    ctx.printer.emit("sheet", kind=kind, rows=len(out), families=len({r["family_id"] for r in out}), sha256=sha_file(target))
    return EXIT_OK


def _validated_files(ctx: Ctx, kind: str, changed: Mapping[Path, List[Dict[str, Any]]]) -> None:
    """Re-validate every rewritten file with the one loader (schema, inventory, one scope per family)."""
    for path, rows in changed.items():
        if not rows:
            continue
        tmp = path.with_name(f".{path.name}.validate.tmp")
        try:
            write_atomic(tmp, dump_rows(rows, mark=kind == "sealed"))
            (load_sealed_rows if kind == "sealed" else load_dev_rows)(ctx, tmp)
        finally:
            tmp.unlink(missing_ok=True)


def _quarantine(ctx: Ctx, kind: str, fams: Mapping[str, Sequence[Mapping[str, Any]]], *, status: Optional[str] = "rejected") -> None:
    """Append whole families to the kind's ``quarantine.jsonl`` (marked for sealed), before they leave a component.

    ``status`` is the ``owner_status`` the rows get (``rejected``); a demotion passes ``None`` and keeps theirs.
    """
    if not fams:
        return
    target = quarantine_target(ctx, kind)
    existing = (load_sealed_rows if kind == "sealed" else load_dev_rows)(ctx, target) if target.is_file() else []
    keep = [r for r in existing if str(r["family_id"]) not in fams]
    add = [dict(r, owner_status=status) if status else dict(r) for rows in fams.values() for r in rows]
    write_atomic(target, dump_rows([*keep, *add], mark=kind == "sealed"))


def _rewrite(ctx: Ctx, kind: str, files: Mapping[Path, List[Dict[str, Any]]]) -> None:
    for path, rows in files.items():
        if rows:
            write_atomic(path, dump_rows(rows, mark=kind == "sealed"))
        else:
            path.unlink(missing_ok=True)


def cmd_apply(ctx: Ctx, kind: str, sheet: Optional[str], confirm: Optional[str]) -> int:
    """Apply the owner's marks: approvals bound to row hash + sheet sha256; changed rows need a confirm sheet."""
    if (sheet is None) == (confirm is None):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    ledger = ctx.ledger()
    files, gone = load_kind(ctx, kind)
    rows_by_id = live_rows(files, gone)
    assert_not_frozen(ledger, kind, files)
    approvals = load_approvals(ctx, kind)
    if confirm is not None:
        lines, sha = read_sheet(Path(confirm), CONFIRM_COLUMNS)
        bound = 0
        for line in lines:
            if not ticked(line["OK"]):
                continue
            rid = line["row_id"]
            pending = approvals["pending_confirm"].get(rid)
            if rid not in rows_by_id or pending is None or line["row_hash"] != pending["row_hash"] \
                    or eval_schema.row_hash(rows_by_id[rid][1]) != line["row_hash"]:
                raise Stop(EXIT_REFUSED, "sheet_stale")
            approvals["approved"][rid] = {"row_hash": line["row_hash"], "sheet_sha256": sha}
            del approvals["pending_confirm"][rid]
            bound += 1
        save_approvals(ctx, kind, approvals)
        ctx.printer.emit("apply", kind=kind, confirmed=bound, pending_confirm=len(approvals["pending_confirm"]), sha256=sha)
        return EXIT_OK

    lines, sha = read_sheet(Path(sheet), SHEET_COLUMNS)  # type: ignore[arg-type]
    approved: Dict[str, str] = {}
    changed: Dict[str, Dict[str, Any]] = {}
    rejected: Set[str] = set()
    escalated: Set[str] = set()
    pending = stale = conflict = bad = 0
    for line in lines:
        rid = line["row_id"]
        if rid not in rows_by_id:
            stale += 1
            continue
        _, row = rows_by_id[rid]
        if line["row_hash"] != eval_schema.row_hash(row):
            stale += 1
            continue
        marks = [m for m in OWNER_MARKS if ticked(line[m])]
        if len(marks) > 1:
            conflict += 1
        elif not marks:
            pending += 1
        elif marks[0] == "A":
            approved[rid] = eval_schema.row_hash(row)
        elif marks[0] == "R":
            rejected.add(str(row["family_id"]))
        elif marks[0] == "E":
            escalated.add(str(row["family_id"]))
        else:  # C: the corrections cell is a JSON object over CORRECTABLE keys
            try:
                patch = json.loads(line["corrections"])
                ok = isinstance(patch, dict) and bool(patch) and set(patch) <= set(CORRECTABLE)
            except ValueError:
                ok = False
            if not ok:
                bad += 1
                continue
            changed[rid] = dict(row, **patch)
    if stale:
        raise Stop(EXIT_REFUSED, "sheet_stale", lines=stale)
    if conflict or bad:
        raise Stop(EXIT_REFUSED, "sheet_conflict", lines=conflict + bad)
    new_files = {p: [dict(r) for r in rows] for p, rows in files.items()}
    quarantined: Dict[str, List[Dict[str, Any]]] = {}
    for path, rows in new_files.items():
        for i, row in enumerate(rows):
            rid, fid = str(row["id"]), str(row["family_id"])
            if rid in approved:
                row["owner_status"] = "approved"
            elif rid in changed:
                rows[i] = dict(changed[rid], owner_status="changed")
            elif fid in escalated:
                row["second_status"] = "pending"
        for row in [r for r in rows if str(r["family_id"]) in rejected]:
            quarantined.setdefault(str(row["family_id"]), []).append(row)
        rows[:] = [r for r in rows if str(r["family_id"]) not in rejected]
    touched = {p: rows for p, rows in new_files.items() if rows != files[p]}
    _validated_files(ctx, kind, touched)
    _quarantine(ctx, kind, quarantined)
    _rewrite(ctx, kind, touched)
    for rid, h in approved.items():
        approvals["approved"][rid] = {"row_hash": h, "sheet_sha256": sha}
        approvals["pending_confirm"].pop(rid, None)
    final = live_rows(new_files, gone | set(quarantined))
    for rid in changed:
        approvals["approved"].pop(rid, None)
        approvals["pending_confirm"][rid] = {"row_hash": eval_schema.row_hash(final[rid][1]), "sheet_sha256": sha}
    for rid, (_, row) in rows_by_id.items():
        if str(row["family_id"]) in rejected:
            approvals["approved"].pop(rid, None)
            approvals["pending_confirm"].pop(rid, None)
    approvals["rejected"] = sorted({*approvals["rejected"], *rejected})
    approvals["escalated"] = sorted({*approvals["escalated"], *escalated})
    save_approvals(ctx, kind, approvals)
    if changed:
        pages = load_pages(ctx)
        recs = []
        for rid in sorted(changed):
            row = final[rid][1]
            recs.append(
                {
                    "row_id": rid, "family_id": row["family_id"], "row_hash": eval_schema.row_hash(row), "phrasing": row["question"],
                    "scope": row["scope"], "evidence": render_evidence(row["evidence"], pages), "gaps": render_gaps(row["gaps"]),
                    "ambiguous": "yes" if row.get("ambiguous") else "no",
                }
            )
        write_atomic(sheets_dir(ctx, kind) / f"{Path(str(sheet)).stem}.confirm.csv", csv_bytes(CONFIRM_COLUMNS, recs))
    ctx.printer.emit(
        "apply", kind=kind, approved=len(approved), changed=len(changed), rejected_families=len(rejected),
        escalated_families=len(escalated), pending=pending, sha256=sha,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# escalate, calibrate, disagree
# ---------------------------------------------------------------------------
FORM_HEAD = (
    "Please give your own independent view of the answer key for each question below. Tick one: ANSWER (the\n"
    "Handbook answers it), PARTLY (it answers some of it), DECLINE (it does not deal with it, or it needs advice\n"
    "beyond it). If ANSWER or PARTLY, list the Handbook paragraph numbers a good answer relies on and say whether\n"
    "each is needed or whether any one would do. If PARTLY, say which part the Handbook does not cover. Then tick\n"
    "sure / fairly sure / unsure. Please keep these questions to yourself and return the form only to the owner.\n"
)
FORM_ITEM_TAIL = "  Your view:  [ ] answer   [ ] partly   [ ] decline\n  Paragraphs:\n  Not covered (if partly):\n  Confidence: [ ] sure  [ ] fairly sure  [ ] unsure\n  Comment:\n"


def _family_rows(ctx: Ctx, kind: str) -> Dict[str, List[Dict[str, Any]]]:
    files, gone = load_kind(ctx, kind)
    by: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in live_rows(files, gone).values():
        by.setdefault(str(row["family_id"]), []).append(row)
    return {fid: sorted(rs, key=row_no) for fid, rs in by.items()}


def _write_form(ctx: Ctx, kind: str, label: str, title: str, items: Sequence[Tuple[str, Sequence[Mapping[str, Any]], str]]) -> Path:
    """A form: item refs, the phrasings and (escalation) the owner's one-line doubt. Never the owner's key."""
    out = [title + "\n", FORM_HEAD]
    refs: Dict[str, str] = {}
    for i, (fid, rows, doubt) in enumerate(items, 1):
        ref = f"E{i:02d}"
        refs[ref] = fid
        out.append(f"\nItem {ref}\n")
        out.extend(f"  Phrasing {n}: {r['question']}\n" for n, r in enumerate(rows, 1))
        if doubt:
            out.append(f"  Owner's doubt: {doubt}\n")
        out.append(FORM_ITEM_TAIL)
    base = ctx.work_for(kind) / "escalations"
    write_atomic(base / f"{label}.txt", "".join(out).encode("utf-8"))
    write_json(base / f"{label}.refs.json", refs)
    return base / f"{label}.txt"


def cmd_escalate(ctx: Ctx, kind: str, sheet: str, name: Optional[str]) -> int:
    """Forms for the sheet's E-marked items, under ``work/escalations/`` of the kind (sealed or development)."""
    lines, _ = read_sheet(Path(sheet), SHEET_COLUMNS)
    fams = _family_rows(ctx, kind)
    doubts: Dict[str, str] = {}
    for line in lines:
        if ticked(line["E"]) and line["family_id"] in fams:
            doubts.setdefault(line["family_id"], "")
            doubts[line["family_id"]] = doubts[line["family_id"]] or line["doubt"].strip().replace("\n", " ")
    if not doubts:
        raise Stop(EXIT_REFUSED, "nothing_to_do")
    items = [(fid, fams[fid], doubts[fid]) for fid in sorted(doubts)]
    label = f"form_{name or Path(sheet).stem}"
    form = _write_form(ctx, kind, label, "Escalation form", items)
    ctx.printer.emit("escalate", kind=kind, forms=1, items=len(items), sha256=sha_file(form))
    return EXIT_OK


def cmd_calibrate(ctx: Ctx, kind: str, seed: Optional[int]) -> int:
    """Draw 15 approved families by the prereg seed (5 refuse-or-partial, 10 answer) for a calibration form."""
    use_seed = seed if seed is not None else int(load_prereg(ctx)["seeds"]["calibration"])
    fams = _family_rows(ctx, kind)
    approvals = load_approvals(ctx, kind)["approved"]
    ok = [
        fid for fid, rows in fams.items()
        if all(approvals.get(str(r["id"]), {}).get("row_hash") == eval_schema.row_hash(r) for r in rows)
    ]
    hard = sorted(f for f in ok if fams[f][0]["scope"] != "answer")
    easy = sorted(f for f in ok if fams[f][0]["scope"] == "answer")
    if len(hard) < 5 or len(easy) < 10:
        raise Stop(EXIT_REFUSED, "calibration_short", refuse_or_partial=len(hard), answer=len(easy))
    rng = random.Random(use_seed)
    drawn = sorted([*rng.sample(hard, 5), *rng.sample(easy, 10)])
    items = [(fid, fams[fid][:1], "") for fid in drawn]
    form = _write_form(ctx, kind, "calibration_form", "Calibration form (a random few questions, used to check the key as a whole)", items)
    pages = load_pages(ctx)
    recs = [
        {
            "row_id": r["id"], "family_id": fid, "row_hash": eval_schema.row_hash(r), "phrasing_no": row_no(r), "phrasing": r["question"],
            "scope": r["scope"], "evidence": render_evidence(r["evidence"], pages), "gaps": render_gaps(r["gaps"]),
            "ambiguous": "yes" if r.get("ambiguous") else "no",
        }
        for fid in drawn for r in fams[fid][:1]
    ]
    write_atomic(sheets_dir(ctx, kind) / "calibration.csv", csv_bytes(SHEET_COLUMNS, recs))
    digest = write_json(ctx.work_for(kind) / "calibration_draw.json", {"seed": use_seed, "families": drawn, "n": len(drawn)})
    ctx.printer.emit("calibrate", kind=kind, drawn=len(drawn), refuse_or_partial=5, answer=10, sha256=digest, form_sha256=sha_file(form))
    return EXIT_OK


def related_labels(a: str, b: str) -> bool:
    """Related matching: equal, or one is a dotted ancestor of the other."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def compare_groups(owner: Sequence[Sequence[str]], other: Sequence[Sequence[str]]) -> Dict[str, bool]:
    """AND/OR structure: a missing / extra required group, or a different alternative set (related matching)."""

    def linked(g: Sequence[str], h: Sequence[str]) -> bool:
        return any(related_labels(x, y) for x in g for y in h)

    extra = any(not any(linked(g, h) for h in other) for g in owner)
    missing = any(not any(linked(g, h) for g in owner) for h in other)
    alt = False
    for g in owner:
        for h in other:
            if linked(g, h) and not (all(any(related_labels(x, y) for y in h) for x in g) and all(any(related_labels(y, x) for x in g) for y in h)):
                alt = True
    return {"missing_group": missing, "extra_group": extra, "alt_set_diff": alt}


def gaps_differ(owner: Sequence[Mapping[str, Any]], other: Sequence[Mapping[str, Any]]) -> bool:
    """Partial gaps differ if the counts differ or a gap has no counterpart sharing a keyword."""

    def kw(gap: Mapping[str, Any]) -> Set[str]:
        return {w for k in gap.get("keywords", []) if isinstance(k, str) for w in tokens_of(k)}

    if len(owner) != len(other):
        return True
    free = [kw(g) for g in other]
    for gap in owner:
        mine = kw(gap)
        hit = next((i for i, theirs in enumerate(free) if mine & theirs), None)
        if hit is None:
            return True
        free.pop(hit)
    return False


def _pm(interval: Any) -> Dict[str, int]:
    return {} if isinstance(interval, str) else {"lo_pm": round(1000 * interval[0]), "hi_pm": round(1000 * interval[1])}


def parse_colleague_key(text: str) -> Optional[Dict[str, Any]]:
    try:
        key = json.loads(text)
    except ValueError:
        return None
    ok = (
        isinstance(key, dict) and set(key) <= {"scope", "evidence", "gaps"} and key.get("scope") in SCOPES
        and isinstance(key.get("evidence", []), list)
        and all(isinstance(g, list) and all(isinstance(x, str) for x in g) for g in key.get("evidence", []))
        and isinstance(key.get("gaps", []), list)
        and all(isinstance(g, dict) and isinstance(g.get("keywords", []), list) for g in key.get("gaps", []))
    )
    return key if ok else None


def cmd_disagree(ctx: Ctx, kind: str, sheet: str, calibration: bool) -> int:
    """Colleague's key vs the owner's, per dimension (scope, AND/OR groups, gaps); both keys are kept."""
    lines, _ = read_sheet(Path(sheet), SHEET_COLUMNS)
    fams = _family_rows(ctx, kind)
    keys: Dict[str, Dict[str, Any]] = {}
    invalid = 0
    for line in lines:
        text = line["colleague_key"].strip()
        if not text or line["family_id"] in keys or line["family_id"] not in fams:
            continue
        key = parse_colleague_key(text)
        if key is None:
            invalid += 1
        else:
            keys[line["family_id"]] = key
    if invalid:
        raise Stop(EXIT_USAGE, "colleague_key_invalid", keys=invalid)
    tally = {"scope_n": 0, "scope_disagree": 0, "group_n": 0, "group_disagree": 0, "missing_group": 0, "extra_group": 0,
             "alt_set_diff": 0, "gap_n": 0, "gap_disagree": 0}
    record: Dict[str, Any] = {}
    for fid, key in sorted(keys.items()):
        mine = fams[fid][0]
        dims = {"scope": key["scope"] != mine["scope"], "groups": False, "gaps": False}
        tally["scope_n"] += 1
        tally["scope_disagree"] += dims["scope"]
        if key["scope"] != "refuse" and mine["scope"] != "refuse":
            diff = compare_groups(mine["evidence"], key.get("evidence", []))
            tally["group_n"] += 1
            dims["groups"] = any(diff.values())
            tally["group_disagree"] += dims["groups"]
            for name, flag in diff.items():
                tally[name] += flag
        if key["scope"] == "partial" and mine["scope"] == "partial":
            tally["gap_n"] += 1
            dims["gaps"] = gaps_differ(mine["gaps"], key.get("gaps", []))
            tally["gap_disagree"] += dims["gaps"]
        record[fid] = {
            "owner": {"scope": mine["scope"], "evidence": mine["evidence"], "gaps": mine["gaps"]}, "colleague": key,
            "disagrees": dims,
        }
    write_json(ctx.work_for(kind) / "disagreement.json", record)
    files, gone = load_kind(ctx, kind)
    for path, rows in files.items():
        dirty = False
        for row in rows:
            fid = str(row["family_id"])
            if fid in record:
                status = "disagree" if any(record[fid]["disagrees"].values()) else "agree"
                if row.get("second_status") != status:
                    row["second_status"], dirty = status, True
        if dirty:
            write_atomic(path, dump_rows(rows, mark=kind == "sealed"))
    out: Dict[str, Any] = {"sample": "random_sample_n15" if calibration else "owner_selected_sample", "compared": len(keys), **tally}
    for dim in ("scope", "group", "gap"):
        n, k = tally[f"{dim}_n"], tally[f"{dim}_disagree"]
        out.update({f"{dim}_{key}": v for key, v in _pm(eval_stats.wilson_interval(k, n)).items()})
    ctx.printer.emit("disagree", **out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# exclude
# ---------------------------------------------------------------------------
def release_payloads(ledger: SealedLedger) -> List[Dict[str, Any]]:
    return [ledger.load_payload(assign_name(int(r["round"])), r["payload_sha256"]) for r in ledger.records("release")]


def sealed_group_index(ledger: SealedLedger) -> Dict[str, List[str]]:
    """``{group id: members}`` over every released round; a family in no group is its own group."""
    groups: Dict[str, List[str]] = {}
    for payload in release_payloads(ledger):
        groups.update(payload["groups"])
        for fid, split in payload["assignment"].items():
            if split == "sealed" and not any(fid in m for m in payload["groups"].values()):
                groups.setdefault(fid, [fid])
    return groups


def group_id_of(groups: Mapping[str, Sequence[str]], fid: str) -> Optional[str]:
    for gid, members in groups.items():
        if fid in members:
            return gid
    return None


def cmd_exclude(ctx: Ctx, family: Optional[str], group: Optional[str], reason: str) -> int:
    """Append an exclusion against the WHOLE sealed group (never a move or an unmark)."""
    if reason not in EXCLUSION_REASONS or (family is None) == (group is None):
        raise Stop(EXIT_USAGE, "usage")
    ledger = ctx.ledger()
    groups = sealed_group_index(ledger)
    if family is not None:
        if not _FAMILY_ID.fullmatch(family):
            raise Stop(EXIT_USAGE, "usage")
        gid = group_id_of(groups, family)
    else:
        gid = group if group in groups else None
    if gid is None:
        raise Stop(EXIT_REFUSED, "no_group")
    if gid in ledger.exclusions():
        ctx.printer.emit("exclude", outcome="already", members=len(groups[gid]))
        return EXIT_OK
    ledger.exclude(gid, reason)
    ctx.printer.emit("exclude", outcome="ok", reason=reason, members=len(groups[gid]))
    return EXIT_OK


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------
def frozen_path(ctx: Ctx, split: str) -> Path:
    where, name = FROZEN_FILES[split]
    return (ctx.sealed_dir if where == "sealed" else ctx.dev_dir) / name


def _ceil_share(share: Any, quota: int) -> int:
    from fractions import Fraction

    f = Fraction(repr(share)) if isinstance(share, float) else Fraction(share)
    return -((-f.numerator * quota) // f.denominator)


def evaluate_split(
    fams: Mapping[str, Fam], split: str, prereg: Mapping[str, Any], placement: Mapping[str, str],
    groups: Mapping[str, Sequence[str]], excluded: Set[str],
) -> List[str]:
    """The constraint names a final split violates (empty = ready): band, strata, min_share, floor, group_invariant."""
    q = len(fams)
    refuse = sum(1 for f in fams.values() if f.scope == "refuse")
    bad: List[str] = []
    if not (BAND_MIN_DEN * refuse >= BAND_MIN_NUM * q and BAND_MAX_DEN * refuse <= BAND_MAX_NUM * q):
        bad.append("band")
    strata = (prereg.get("strata") or {}).get(split, {})
    if any(sum(1 for f in fams.values() if f"{f.scope}|{f.chapter}" == key) < need for key, need in strata.items()):
        bad.append("strata")
    shares = (prereg.get("min_share") or {}).get(split, {})
    if any(sum(1 for f in fams.values() if source_class(f.source) == src) < _ceil_share(share, q) for src, share in shares.items()):
        bad.append("min_share")
    if split == "sealed":
        answerable = 0
        for fid, fam in fams.items():
            gid = group_id_of(groups, fid) or fid
            if fam.scope != "refuse" and gid not in excluded:
                answerable += 1
        quota = prereg["quota"]
        if q < int(quota["floor_sealed"]) or answerable < int(quota["floor_answerable"]):
            bad.append("floor")
    for members in groups.values():
        if len({placement[m] for m in members if m in placement}) > 1:
            bad.append("group_invariant")
            break
    return bad


def repair_band(units: Sequence[Tuple[str, int, int]], floor_q: int, seed: int, max_k: int = 6, budget: int = 400_000) -> Optional[List[str]]:
    """The fewest seeded group demotions that bring the sealed band right while ``floor_q`` families remain.

    ``units`` are ``(group id, families, refuse families)``. Candidates are shuffled by ``seed``; subsets
    are tried by increasing size, so the first hit is a minimum, and ties are broken by the seeded order.
    """
    order = sorted(units)
    random.Random(seed).shuffle(order)
    q, r = sum(u[1] for u in order), sum(u[2] for u in order)
    tried = 0
    for k in range(1, max_k + 1):
        for combo in itertools.combinations(order, k):
            tried += 1
            if tried > budget:
                return None
            q2, r2 = q - sum(u[1] for u in combo), r - sum(u[2] for u in combo)
            if q2 >= floor_q and BAND_MIN_DEN * r2 >= BAND_MIN_NUM * q2 and BAND_MAX_DEN * r2 <= BAND_MAX_NUM * q2:
                return [u[0] for u in combo]
    return None


def move_to_quarantine(ctx: Ctx, kind: str, fids: Set[str], *, status: Optional[str]) -> int:
    """Move whole sealed families to quarantine (demotion or rejection): quarantine first, then the components."""
    files, gone = load_kind(ctx, kind)
    quarantined: Dict[str, List[Dict[str, Any]]] = {}
    new_files: Dict[Path, List[Dict[str, Any]]] = {}
    for path, rows in files.items():
        keep = [r for r in rows if str(r["family_id"]) not in fids]
        for r in rows:
            if str(r["family_id"]) in fids:
                quarantined.setdefault(str(r["family_id"]), []).append(r)
        if len(keep) != len(rows):
            new_files[path] = keep
    _quarantine(ctx, kind, quarantined, status=status)
    _rewrite(ctx, kind, new_files)
    return len(quarantined)


def _verdict_lines(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())


def cmd_freeze(ctx: Ctx, split_name: str, verdict: Optional[str], verdict_return: Optional[str]) -> int:
    """Freeze one split (sealed needs no development approval). See the module docstring for the order of checks."""
    split = resolve_split(split_name)
    kind = "sealed" if split == "sealed" else "development"
    prereg = load_prereg(ctx)
    recover_state(ctx)
    ledger = ctx.ledger()
    if ledger.freeze_record(split) is not None:
        raise Stop(EXIT_REFUSED, "already_frozen")
    releases = ledger.records("release")
    if not releases:
        raise Stop(EXIT_REFUSED, "round1_missing")
    payloads = {int(r["round"]): ledger.load_payload(assign_name(int(r["round"])), r["payload_sha256"]) for r in releases}

    # 1. replay every released round from its stored payload
    expected: Dict[str, str] = {}
    dropped: Set[str] = set()
    for payload in payloads.values():
        try:
            replay = split_families(
                payload["families"], payload["seed"], payload["quotas"], payload["strata"], twins=payload["twins"],
                preassigned=payload["preassigned"], min_share=payload["min_share"],
            )
        except (SplitInfeasible, SplitInputError):
            raise Stop(EXIT_REFUSED, "replay_mismatch") from None
        if replay != payload["assignment"]:
            raise Stop(EXIT_REFUSED, "replay_mismatch")
        expected.update({f: s for f, s in payload["assignment"].items() if f not in payload["preassigned"]})
        dropped.update(payload["dropped"])

    # 2. the released files must still hold what the assignment says
    sealed_side, dev_side = load_sealed_side(ctx), load_dev(ctx)
    sealed_q, dev_q = quarantined_ids(ctx, "sealed"), quarantined_ids(ctx, "development")
    moved = sum(
        1 for fid, s in expected.items()
        if not ((s == "sealed" and (fid in sealed_side or fid in sealed_q)) or (s != "sealed" and (dev_side.get(fid) and dev_side[fid].split == s or fid in dev_q)))
    ) + sum(1 for fid in dropped if fid not in sealed_q)
    planted = sum(1 for fid in sealed_side if fid not in expected)
    if moved or planted:
        raise Stop(EXIT_REFUSED, "assignment_changed", families=moved + planted)

    # 3. the lexical screen, then the SP's recheck on the final content
    pairs = lexical_pairs(sealed_side, dev_side)
    required = len(sealed_side) * len(dev_side)  # the SP checks every sealed family against every development family
    content = sha_text(canon([sorted(final_row_hashes(sealed_side.values()).items()), sorted(final_row_hashes(dev_side.values()).items())]))
    screen = ctx.sealed_work / f"screen_{split}.json"
    if verdict is None or verdict_return is None:
        write_json(screen, {"content_digest": content, "pairs": [list(p) for p in pairs]})
        raise Stop(EXIT_REFUSED, "recheck_missing", pairs=len(pairs), pairs_required=required, sha256=content)
    if not screen.is_file() or read_json(screen).get("content_digest") != content:
        raise Stop(EXIT_REFUSED, "recheck_stale")
    vpath, rpath = Path(verdict), Path(verdict_return)
    line = sp.load_return(rpath, RETURN_ENUMS)
    ret = json.loads(line) if line else {}
    if not (ret.get("status") == "ok" and ret.get("kind") == "recheck" and ret.get("twin_pairs") == _verdict_lines(vpath)
            and isinstance(ret.get("pairs_checked"), int) and ret["pairs_checked"] >= required):
        raise Stop(EXIT_REFUSED, "recheck_invalid")
    if min(vpath.stat().st_mtime, rpath.stat().st_mtime) < screen.stat().st_mtime:
        raise Stop(EXIT_REFUSED, "recheck_stale")
    twin_pairs = parse_verdict(vpath)

    # 4. twins: with development the WHOLE group leaves sealed (approvals kept); inside sealed, one is dropped.
    #    Everything up to step 8 is computed in memory: a refusal leaves no side effect.
    groups = sealed_group_index(ledger)
    demote: Set[str] = set()
    exclude: Set[str] = set()
    for a, b in twin_pairs:
        sa, sb, da, db = a in sealed_side, b in sealed_side, a in dev_side, b in dev_side
        if (sa and db) or (sb and da):
            fid = a if sa else b
            (demote if (split == "sealed" or ledger.freeze_record("sealed") is None) else exclude).add(group_id_of(groups, fid) or fid)
        elif sa and sb and (group_id_of(groups, a) or a) != (group_id_of(groups, b) or b):
            demote.add(group_id_of(groups, max(a, b)) or max(a, b))
    leaving = {m for gid in demote for m in groups.get(gid, [gid])} & set(sealed_side)
    sealed_after = {f: x for f, x in sealed_side.items() if f not in leaving}
    fams: Dict[str, Fam] = dict(sealed_after) if split == "sealed" else {f: x for f, x in dev_side.items() if x.split == split}

    # 5. approvals bound by row hash
    approved = load_approvals(ctx, kind)["approved"]
    missing = sum(
        1 for fam in fams.values()
        if any(needs_approval(kind, r) and approved.get(str(r["id"]), {}).get("row_hash") != eval_schema.row_hash(r) for r in fam.rows)
    )
    if missing:
        raise Stop(EXIT_REFUSED, "approval_missing", families=missing)

    # 6. 2-3 phrasings per family (a breach is the D64 stop)
    short = sum(1 for fam in fams.values() if not 2 <= len(fam.rows) <= 3)
    if short:
        raise Stop(EXIT_FREEZE_INFEASIBLE, "phrasing_breach", families=short)

    # 7. band, strata, min_share, group invariant, floor; a sealed band breach is repaired by seeded group demotions
    def placement(sealed_ids: Iterable[str]) -> Dict[str, str]:
        place = {f: "sealed" for f in sealed_ids}
        place.update({f: str(x.split) for f, x in dev_side.items() if x.split})
        return place

    excluded = set(ledger.exclusions()) | exclude
    violated = evaluate_split(fams, split, prereg, placement(sealed_after), groups, excluded)
    repaired: List[str] = []
    if split == "sealed" and "band" in violated:
        by_group: Dict[str, List[str]] = {}
        for fid in fams:
            by_group.setdefault(group_id_of(groups, fid) or fid, []).append(fid)
        units = [(gid, len(m), sum(1 for f in m if fams[f].scope == "refuse")) for gid, m in sorted(by_group.items())]
        chosen = repair_band(units, int(prereg["quota"]["floor_sealed"]), int(prereg["seeds"]["repair"]))
        if chosen is not None:
            repaired = chosen
            leaving |= {f for gid in chosen for f in by_group[gid]}
            fams = {f: x for f, x in fams.items() if f not in leaving}
            sealed_after = fams
            violated = evaluate_split(fams, split, prereg, placement(sealed_after), groups, excluded)
    if violated:
        for name in violated:
            ctx.printer.emit("freeze_constraint", constraint=name)
        raise Stop(EXIT_FREEZE_INFEASIBLE, "freeze_infeasible", families=len(fams))

    # 8. commit. Demoted and repaired groups go to quarantine (marked, approvals kept), exclusions are appended,
    #    then the frozen file is written and the ledger records the freeze (twins record bound for sealed).
    if leaving:
        move_to_quarantine(ctx, "sealed", leaving, status=None)
    for gid in sorted(exclude - ledger.exclusions()):
        ledger.exclude(gid, "twin_dev_version")
    rows = [r for f in fams.values() for r in f.rows]
    data = dump_rows(rows, mark=split == "sealed")
    target = frozen_path(ctx, split)
    write_atomic(target, data, mode=0o444)
    set_sha = hashlib.sha256(data).hexdigest()
    answerable = sum(1 for fid, f in fams.items() if f.scope != "refuse" and (group_id_of(groups, fid) or fid) not in excluded)
    totals = {
        "families": len(fams), "rows": len(rows), "refuse_families": sum(1 for f in fams.values() if f.scope == "refuse"),
        "eligible_answerable": answerable, "groups": len({group_id_of(groups, f) or f for f in fams}),
    }
    twins_sha = None
    if split == "sealed":
        record = {
            "version": 1, "rounds": {str(r): {"payload": p["twins_payload"], "sha256": p["twins_sha256"]} for r, p in sorted(payloads.items())},
            "recheck_verdict_sha256": sha_file(vpath), "recheck_twin_pairs": [list(p) for p in twin_pairs],
            "demoted_groups": sorted(demote), "repaired_groups": sorted(repaired), "families_sha256": set_sha,
        }
        twins_sha = ledger.store_payload("twins_sealed", record)
        ledger.record_freeze(
            split="sealed", set_sha256=set_sha, totals=totals, twins_sha256=twins_sha,
            exclusions_sha256=sha_text(canon(sorted(ledger.exclusions()))),
        )
    else:
        ledger.record_freeze(split=split, set_sha256=set_sha, totals=totals)
    extra = {"twins_sha256": twins_sha} if twins_sha else {}
    ctx.printer.emit(
        "freeze", split=split, status="frozen", demoted_groups=len(demote), repaired_groups=len(repaired),
        excluded_groups=len(exclude), sha256=set_sha, **totals, **extra,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def mangle_path(path: Path) -> str:
    """Claude Code's project-directory name for a path: every non-alphanumeric character becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def _files_under(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()) if root.is_dir() else []


def _history_blobs(repo: Path) -> List[Tuple[str, str]]:
    """(blob sha, path) for every blob added or modified by any commit reachable from any ref."""
    found: List[Tuple[str, str]] = []
    for commit in sl._git(repo, "rev-list", "--all").decode().split():
        raw = sl._git(repo, "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--no-renames", "-z", commit)
        parts = raw.split(b"\0")
        i = 0
        while i + 1 < len(parts):
            meta = parts[i].decode(errors="replace")
            if not meta.startswith(":"):
                i += 1
                continue
            fields = meta[1:].split()
            path = parts[i + 1].decode(errors="replace")
            i += 2
            if len(fields) >= 5 and fields[4][0] in "AMT" and set(fields[3]) != {"0"} and fields[1] != "160000":
                found.append((fields[3], path))
    return found


def audit_targets(ctx: Ctx) -> Dict[str, List[Path]]:
    """The a6 target list as files per class: transcripts and memory, staging, logs, research, work, runs, dispatch logs."""
    root = ctx.projects_root if ctx.projects_root is not None else Path.home() / ".claude" / "projects"
    prefix = mangle_path(ctx.repo)
    targets: Dict[str, List[Path]] = {c: [] for c in TARGET_CLASSES}
    if root.is_dir():
        for directory in sorted(d for d in root.iterdir() if d.is_dir() and d.name.startswith(prefix)):
            for path in _files_under(directory):
                is_memory = "memory" in path.relative_to(directory).parts[:-1]
                targets["memory" if is_memory else "transcript"].append(path)
    try:
        staging = ctx.staging_root if ctx.staging_root is not None else sp.resolve_staging_root()
    except sp.RootRefused:
        staging = sp.default_staging_root()
    targets["staging"] = _files_under(staging)
    targets["log"] = _files_under(ctx.repo / "logs")
    targets["research"] = _files_under(ctx.repo / "data" / "research")
    targets["work"] = _files_under(ctx.dev_work)
    targets["run"] = _files_under(ctx.private / "runs")
    targets["dispatch"] = [p for p in (ctx.private / "sp_dispatch.jsonl", ctx.private / "sp_probe.jsonl") if p.is_file()]
    return targets


def cmd_audit(ctx: Ctx) -> int:
    """S-needle scan over the a6 target list; counts per target class and each hit's family id."""
    try:
        index = sl.build_needles(sl.collect_sources(), handbook_dump=ctx.handbook_dump)
    except sl.ScanRefusal:
        raise Stop(EXIT_USAGE, "scan_source_invalid") from None
    s_needles = len(index.s_whole) + len(index.s_windows) + len(index.s_short)
    hits: Dict[str, Dict[str, int]] = {c: {} for c in TARGET_CLASSES}
    scanned: Dict[str, int] = {c: 0 for c in TARGET_CLASSES}

    def scan(klass: str, text: str) -> None:
        scanned[klass] += 1
        for hit in sl.scan_text(text, klass, index):
            if hit.family is not None:
                hits[klass][hit.family] = hits[klass].get(hit.family, 0) + 1

    if s_needles:
        for klass, paths in audit_targets(ctx).items():
            for path in paths:
                try:
                    scan(klass, path.read_bytes().decode("utf-8", errors="replace"))
                except OSError:
                    continue
        if (ctx.repo / ".git").exists():
            head, history = sl._head_blobs(ctx.repo), _history_blobs(ctx.repo)
            blobs = sl._cat_blobs(ctx.repo, sorted({s for s, _ in head} | {s for s, _ in history}))
            for klass, listing in (("tree", head), ("history", history)):
                for sha in sorted({s for s, _ in listing}):
                    scan(klass, blobs.get(sha, b"").decode("utf-8", errors="replace"))
            messages = sl._git(ctx.repo, "log", "--all", "--format=%B%x1e").decode("utf-8", errors="replace")
            for message in messages.split("\x1e"):
                scan("history", message)
    total = 0
    for klass in TARGET_CLASSES:
        n = sum(hits[klass].values())
        total += n
        ctx.printer.emit("audit_target", target=klass, files=scanned[klass], hits=n)
    for klass in TARGET_CLASSES:
        for family, count in sorted(hits[klass].items()):
            ctx.printer.emit("audit_hit", target=klass, family=family, count=count)
    ctx.printer.emit("audit", status="hits" if total else "clean", s_needles=s_needles, hits=total)
    return EXIT_HIT if total else EXIT_OK


# ---------------------------------------------------------------------------
# power, prereg-check, return
# ---------------------------------------------------------------------------
def cmd_power(ctx: Ctx, group_sizes: Optional[str], from_freeze: bool, discordance: Sequence[float],
              effect: Optional[float], alpha: Optional[float], draws: Optional[int], seed: Optional[int]) -> int:
    """Power and MDE of the cluster sign-flip test under independence and full within-group correlation.

    Group sizes come from ``--group-sizes`` or from the frozen sealed set (eligible answerable families,
    exclusions dropped, grouped by released group). The singleton reference is ``eval_stats.power`` / ``mde``.
    """
    prereg = read_json(ctx.prereg_file) if ctx.prereg_file.is_file() else {}
    pw = prereg.get("power", {}) if isinstance(prereg, dict) else {}
    alpha_v = alpha if alpha is not None else pw.get("alpha")
    effect_v = effect if effect is not None else (prereg.get("mwe") if isinstance(prereg, dict) else None)
    draws_v = draws if draws is not None else pw.get("draws")
    seed_v = seed if seed is not None else pw.get("seed")
    discs = list(discordance) or list(pw.get("discordance", []))
    if None in (alpha_v, effect_v, draws_v, seed_v) or not discs:
        raise Stop(EXIT_USAGE, "usage")
    if (group_sizes is None) == (not from_freeze):
        raise Stop(EXIT_USAGE, "usage")
    if group_sizes is not None:
        try:
            sizes = [int(x) for x in group_sizes.split(",")]
        except ValueError:
            raise Stop(EXIT_USAGE, "usage") from None
        if not sizes or min(sizes) < 1:
            raise Stop(EXIT_USAGE, "usage")
    else:
        ledger = ctx.ledger()
        groups, excluded = sealed_group_index(ledger), ledger.exclusions()
        fams = group_families(load_sealed_rows(ctx, frozen_path(ctx, "sealed")))
        counts: Dict[str, int] = {}
        for fid, fam in fams.items():
            gid = group_id_of(groups, fid) or fid
            if fam.scope != "refuse" and gid not in excluded:
                counts[gid] = counts.get(gid, 0) + 1
        sizes = sorted(counts.values())
    n = sum(sizes)
    for disc in discs:
        eff = min(float(effect_v), float(disc))
        for model in MODELS:
            mde = mc_mde_hundredths(sizes, disc, float(alpha_v), int(draws_v), int(seed_v), model)
            ctx.printer.emit(
                "power", model=model, families=n, groups=len(sizes), discordance_pm=round(1000 * disc), effect_pm=round(1000 * eff),
                power_pm=mc_power_pm(sizes, disc, eff, float(alpha_v), int(draws_v), int(seed_v), model),
                mde_hundredths=mde if mde is not None else "none", draws=int(draws_v),
            )
        ref = eval_stats.mde(n, disc, float(alpha_v))
        ctx.printer.emit(
            "power_reference", model="independent", families=n, discordance_pm=round(1000 * disc),
            power_pm=round(1000 * eval_stats.power(n, disc, eff, float(alpha_v), "directional")),
            mde_hundredths=round(100 * ref) if ref is not None else "none",
        )
    return EXIT_OK


def cmd_prereg_check(ctx: Ctx, committed: bool) -> int:
    """Validate the prereg and recompute its quota formulas and power table; ``--committed`` also needs it committed unmodified."""
    if committed:
        prereg_commit(ctx)
    doc = read_json(ctx.prereg_file, "prereg_invalid") if ctx.prereg_file.is_file() else None
    if doc is None:
        raise Stop(EXIT_USAGE, "prereg_missing")
    errors = validate_prereg(doc)
    for path, why in errors:
        ctx.printer.emit("prereg_error", field=path if _FIELD_PATH.fullmatch(path) else "other", why=why)
    if errors:
        raise Stop(EXIT_FAIL, "prereg_invalid", errors=len(errors))
    ctx.printer.emit(
        "prereg_check", status="ok", rounds=len(doc["rounds"]), quota_examples=len(doc["quota"].get("examples", [])),
        power_cells=len(doc["power"].get("table", [])), committed=1 if committed else 0,
    )
    return EXIT_OK


def cmd_return(ctx: Ctx, path: str, enums: Sequence[str]) -> int:
    """An SP return file's canonical line if every key and value is an id, hash, count or enum; else ``invalid``."""
    try:
        line = sp.load_return(path, [*RETURN_ENUMS, *enums])
    except ValueError:
        line = None
    if line is None:
        ctx.printer.raw_validated("invalid")
        return EXIT_FAIL
    ctx.printer.raw_validated(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """argparse that never echoes the offending argument (it could be text)."""

    def error(self, message: str) -> Any:  # noqa: ARG002 - message deliberately dropped
        raise Stop(EXIT_USAGE, "usage")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="sealed_tool.py", description="Sealed tool (D72): prints only counts, sha256s and enums.")
    for opt in ("private-root", "repo", "ledger", "mirror", "inventory", "prereg", "handbook-dump", "claude-projects", "staging-root"):
        ap.add_argument(f"--{opt}", default=None, metavar="PATH")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def add(name: str, *flags: Tuple[str, Dict[str, Any]]) -> None:
        p = sub.add_parser(name)
        for flag, kw in flags:
            p.add_argument(flag, **kw)

    add("ids", ("--count", {"type": int, "required": True}), ("--out", {"default": None}))
    add("digest", ("path", {}))
    add("count-migrated", ("--golden", {"required": True}), ("--realistic", {"required": True}), ("--pairing", {"required": True}),
        ("--tutorial", {"required": True}), ("--quota-tuning", {"type": int, "default": None}), ("--quota-realistic", {"type": int, "default": None}))
    add("inputs-check", ("--pairing", {"default": None}), ("--conflicts", {"default": None}), ("--golden-ids", {"default": None}),
        ("--realistic-ids", {"default": None}))
    add("twins", ("--round", {"type": int, "required": True}), ("--verdict", {"required": True}), ("--dev-verdict", {"default": None}),
        ("--tutorials", {"default": None}))
    add("split", ("--round", {"type": int, "required": True}))
    add("sheet", ("--kind", {"choices": KINDS, "required": True}), ("--round", {"type": int, "default": None}), ("--name", {"default": None}),
        ("--all", {"action": "store_true"}))
    add("apply", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"default": None}), ("--confirm", {"default": None}))
    add("freeze", ("--split", {"required": True}), ("--recheck-verdict", {"default": None}), ("--recheck-return", {"default": None}))
    add("escalate", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"required": True}), ("--name", {"default": None}))
    add("calibrate", ("--kind", {"choices": KINDS, "required": True}), ("--seed", {"type": int, "default": None}))
    add("disagree", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"required": True}), ("--calibration", {"action": "store_true"}))
    add("exclude", ("--family", {"default": None}), ("--group", {"default": None}), ("--reason", {"required": True}))
    add("audit")
    add("power", ("--group-sizes", {"default": None}), ("--from-freeze", {"action": "store_true"}),
        ("--discordance", {"type": float, "action": "append", "default": []}), ("--effect", {"type": float, "default": None}),
        ("--alpha", {"type": float, "default": None}), ("--draws", {"type": int, "default": None}), ("--seed", {"type": int, "default": None}))
    add("prereg-check", ("--committed", {"action": "store_true"}))
    add("return", ("path", {}), ("--enum", {"action": "append", "default": [], "dest": "enums"}))
    return ap


def dispatch(ctx: Ctx, a: argparse.Namespace) -> int:
    c = a.command
    if c == "ids":
        return cmd_ids(ctx, a.count, a.out)
    if c == "digest":
        return cmd_digest(ctx, a.path)
    if c == "count-migrated":
        return cmd_count_migrated(ctx, a.golden, a.realistic, a.pairing, a.tutorial, a.quota_tuning, a.quota_realistic)
    if c == "inputs-check":
        return cmd_inputs_check(ctx, a.pairing, a.conflicts, a.golden_ids, a.realistic_ids)
    if c == "twins":
        return cmd_twins(ctx, a.round, a.verdict, a.dev_verdict, a.tutorials)
    if c == "split":
        return cmd_split(ctx, a.round)
    if c == "sheet":
        return cmd_sheet(ctx, a.kind, a.round, a.name, a.all)
    if c == "apply":
        return cmd_apply(ctx, a.kind, a.sheet, a.confirm)
    if c == "freeze":
        return cmd_freeze(ctx, a.split, a.recheck_verdict, a.recheck_return)
    if c == "escalate":
        return cmd_escalate(ctx, a.kind, a.sheet, a.name)
    if c == "calibrate":
        return cmd_calibrate(ctx, a.kind, a.seed)
    if c == "disagree":
        return cmd_disagree(ctx, a.kind, a.sheet, a.calibration)
    if c == "exclude":
        return cmd_exclude(ctx, a.family, a.group, a.reason)
    if c == "audit":
        return cmd_audit(ctx)
    if c == "power":
        return cmd_power(ctx, a.group_sizes, a.from_freeze, a.discordance, a.effect, a.alpha, a.draws, a.seed)
    if c == "prereg-check":
        return cmd_prereg_check(ctx, a.committed)
    return cmd_return(ctx, a.path, a.enums)


LEDGER_ERRORS = (
    (LedgerRefused, "ledger_refused", EXIT_LEDGER_STOP),
    (MirrorTampered, "mirror_tampered", EXIT_LEDGER_STOP),
    (EpochStop, "epoch_stop", EXIT_LEDGER_STOP),
    (LedgerCorrupt, "ledger_corrupt", EXIT_LEDGER_STOP),
    (ProtocolError, "ledger_protocol", EXIT_REFUSED),
)


def main(argv: Optional[Sequence[str]] = None, *, out: Any = None, err: Any = None) -> int:
    """CLI entry point; returns the exit code. Nothing but the allowlisted printer writes output."""
    printer = Printer(out, err)
    try:
        args = build_parser().parse_args(argv)
        ctx = default_ctx(
            printer, private=args.private_root, repo=args.repo, ledger_path=args.ledger, mirror=args.mirror,
            inventory_path=args.inventory, prereg_path=args.prereg, handbook_dump=args.handbook_dump,
            projects_root=args.claude_projects, staging_root=args.staging_root,
        )
        return dispatch(ctx, args)
    except Stop as stop:
        printer.error(stop.reason, **stop.fields)
        return stop.code
    except eval_schema.SchemaError as exc:
        for line, fld, _reason in exc.errors[:50]:
            printer.emit("schema_error", line=min(line, MAX_PRINT_INT), field=fld if _FIELD_PATH.fullmatch(fld) else "other")
        printer.error("inventory_invalid" if isinstance(exc, eval_schema.InventoryError) else "schema_invalid", errors=len(exc.errors))
        return EXIT_USAGE
    except SealedLedgerError as exc:
        for kind, reason, code in LEDGER_ERRORS:
            if isinstance(exc, kind):
                printer.error(reason)
                return code
        printer.error("ledger_corrupt")
        return EXIT_LEDGER_STOP
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001 - never show exception text (it could carry a path or a row)
        printer.err.write("error reason=internal\n")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
