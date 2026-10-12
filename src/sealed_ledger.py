"""Sealed-slot ledger and slot state machine (Phase 16A-2a item a4, D72).

What this module does
---------------------
It is the append-only, content-free record of the sealed epoch: split
attempts, release/assignment, the freeze, slot claims, publication intents,
"consumed" marks and group exclusions. It also owns the **slot protocol**, so
that a crash at any step has exactly one safe recovery and a sealed row is
never run twice. It knows nothing about the evaluator; item a5 calls it.

Three stores, one truth
-----------------------
Every record is one JSON line, written to:

1. the **out-of-tree ledger** ``~/.local/state/claudecode/sealed_ledger.jsonl``
   (keyed on the passwd home, like D70's spend ledger; ``CC_SEALED_LEDGER``
   overrides it **only under pytest** and is refused otherwise);
2. the **private copy**, ``<private_copy_dir>/sealed_ledger.jsonl`` (in
   production ``eval/private/sealed/ledger/``), byte-identical to (1);
3. optionally the **committed mirror** (``eval/sealed_ledger.jsonl``): one line
   per record holding only the record's kind, its sequence number, the sha256
   of the private line and the record's own digest fields. No scope, chapter,
   stratum or ``sealed`` key, no ids, no counts.

Appends happen under an exclusive ``fcntl.flock`` on store (1), in that order,
each fsynced (the same recipe as ``src/spend.py``). A final line without a
newline is a write cut off by a crash: it is ignored and cut off by the next
append. Any other unreadable line raises ``LedgerCorrupt`` (fail closed).

Every operation first **reconciles** the stores under the lock:

* the shorter private store is a prefix of the longer one (a crash between the
  two writes) -> it is caught up; two stores that disagree -> ``LedgerCorrupt``;
* one private store lost -> restored from the other;
* the mirror must equal the projection of the private records line for line,
  else ``MirrorTampered`` (so an edited earlier mirror line is refused); a
  mirror that lags is caught up;
* the mirror attesting more records than the private stores hold means the
  private stores were lost: ``EpochStop`` (D64 stop). Note this is detectable
  only because the committed mirror is a witness; without a mirror, two lost
  private stores look like a fresh epoch.

Freeze, per split
-----------------
``record_freeze(split=...)`` records one split's freeze (``sealed``, ``tuning``,
``realistic``...): its file sha256 and totals. Only the ``sealed`` freeze must
also bind the twins record's and the exclusion list's sha256. A second freeze of
the *same* split is refused; the mirror carries the split name (a value) and the
digests, never a count.

Payloads
--------
Freeze replays a released split from content-free per-family records (seed,
assignment...) that are too big for a ledger line. ``store_payload(name, obj)``
writes ``<name>.json`` (canonical JSON, fsynced, renamed into place) to
``sealed_payloads/`` next to the out-of-tree ledger AND to ``payloads/`` in the
private copy, and returns its sha256. ``load_payload(name, sha256)`` reads
whichever copy verifies and raises ``EpochStop`` if neither does. Split attempts
and releases may carry ``payload_sha256``; the mirror carries that digest, never
the payload.

Slot protocol (a4 steps 1-6)
----------------------------
1. ``claim(slot)``: refused if the slot was claimed before (and a spent slot
   stays claimed, so it is never re-run);
2. the caller runs the sealed call;
3. ``store_results(slot, run_dir)``: durable results exist (digest recorded);
4. the caller decides canonical status; only a canonical run calls
   ``record_intent(slot)``, a non-canonical run records nothing;
5. the caller renders (and scans);
6. ``record_consumed(slot)``.

``recover(slot)`` reads the state and returns the action. It appends nothing
and never implies a fresh sealed call: a claim without stored results is
``SPENT``; stored results without an intent are ``RENDER_FROM_STORAGE`` (the
slot is consumed only once rendered); a durable intent is ``REPUBLISH`` of the
stored result.

0b rule: ``may_use_0b()`` is true only if slot 0 was claimed and ended
non-canonical (no intent ever recorded) and 0b was not claimed yet. An intent
on slot 0, even with the render interrupted, disqualifies 0b for good. Slot 0
is not "ended" while it holds stored results that are neither rendered nor
consumed (its canonical status may still be decided), so 0b waits for that.

Execution locks (a live slot is not a spent slot)
-------------------------------------------------
The ledger alone cannot tell a slot whose process is still running from one
whose process died: both are "claimed, nothing stored". ``lock_slot(slot)``
therefore takes a non-blocking exclusive ``flock`` on a per-slot lock file
(``sealed_slot_locks/slot-<id>.lock`` next to the out-of-tree ledger); the gate
takes it before it claims and the token keeps it for the whole run, and
recovery takes it too. The kernel drops the lock with the process, so a crash
leaves a free lock and a ledger that says SPENT, while a live run keeps the lock.
``claim("0b")`` takes slot 0's lock (non-blocking) and is refused if it is held,
and once 0b is claimed ``store_results``/``record_intent`` for slot 0 are refused.

Dispatches
----------
``record_dispatch`` appends a ``dispatch`` record when ``scripts/sp_launch.py``
starts a ``data`` sealed-processor run: the 8-hex dispatch id, the launch kind
(``dispatch_kind``: the record's own ``kind`` is ``dispatch``), the argument-vector
hash and the UTC time. Nothing else. The committed mirror carries the record's
kind and line digest only (no digest field is listed for it).

Groups and exclusions
---------------------
``group_ids`` is the **one definition of a sealed group id** (the connected
components of the twins record's edges over the frozen sealed families, named by
the smallest member family): ``sealed_tool exclude`` and the slot gate both call
it. A group's *name* can change when a later rejection removes its smallest
member, so an exclusion also records its ``members`` (the group's families when it
was made) and the gate resolves ``excluded_families()`` against the freeze-bound
groups at run time instead of trusting the recorded name.
``exclusions_digest`` and ``SealedLedger.exclusions_at_freeze`` give the digest
the sealed freeze binds, so the slot gate re-checks it from the ledger itself
(no separate exclusion file).

Errors are ``BaseException`` subclasses, like D70's, so no broad
``except Exception`` can turn a refusal into a fall-through.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import sys
from contextlib import contextmanager, nullcontext as _nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEDGER_ENV = "CC_SEALED_LEDGER"
LEDGER_FILENAME = "sealed_ledger.jsonl"
SCHEMA_VERSION = 1

#: Record kinds (a4): split attempts, release/assignment, freeze, slot claim,
#: results stored (step 3), publication intent, consumed, exclusion; plus (a7)
#: the sealed-processor ``dispatch``.
KINDS = (
    "split_attempt",
    "release",
    "freeze",
    "slot_claim",
    "slot_stored",
    "publication_intent",
    "slot_consumed",
    "exclusion",
    "dispatch",
)

#: The only fields of a record the committed mirror may carry besides the
#: record's kind, sequence number and line digest (all sha256 hex digests).
MIRROR_DIGEST_FIELDS: Dict[str, tuple] = {
    "split_attempt": ("pool_digest", "payload_sha256"),
    "release": ("assignment_digest", "payload_sha256"),
    "freeze": ("set_sha256", "twins_sha256", "exclusions_sha256"),
    "slot_stored": ("results_digest",),
    "publication_intent": ("results_digest",),
}

#: Non-digest values the mirror may carry: a freeze names its split (a value, not a key).
MIRROR_VALUE_FIELDS: Dict[str, tuple] = {"freeze": ("split",)}

#: The split whose freeze also binds the twins record and the exclusion list.
SEALED_SPLIT = "sealed"

#: Payload directories: out-of-tree (next to the ledger) and inside the private copy.
PAYLOAD_DIRNAME = "sealed_payloads"
PRIVATE_PAYLOAD_DIRNAME = "payloads"

#: Files at a run directory's top level that the run-directory digest ignores (rendered output, see ``digest_run_dir``).
DIGEST_EXEMPT = ("report.md", "report.md.tmp")

#: Per-slot execution locks live next to the out-of-tree ledger (so the pytest-only override moves them too).
SLOT_LOCK_DIRNAME = "sealed_slot_locks"

#: A mirror key containing any of these is a leak (a4).
FORBIDDEN_MIRROR_KEY_PARTS = ("scope", "chapter", "stratum", "sealed")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[a-z0-9_]{1,40}")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_DISPATCH_ID = re.compile(r"[0-9a-f]{8}")
_PAYLOAD_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SLOT = re.compile(r"0b|0|[1-9][0-9]{0,2}")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SealedLedgerError(BaseException):
    """Base of every ledger failure (``BaseException``, like D70's meter errors)."""


class LedgerRefused(SealedLedgerError):
    """A ledger location the rules forbid (override outside pytest, default under it)."""


class LedgerCorrupt(SealedLedgerError):
    """An unreadable or inconsistent line, or two private stores that disagree."""


class MirrorTampered(SealedLedgerError):
    """The committed mirror no longer matches the private records."""


class EpochStop(SealedLedgerError):
    """Both private copies are lost (they hold fewer records than the mirror attests).

    A D64 owner stop: the epoch ends.
    """


class ProtocolError(SealedLedgerError):
    """A call that the slot protocol (or the freeze rule) does not allow now."""


class SlotRefused(ProtocolError):
    """``claim`` refused: the slot was claimed before, or 0b is not available."""


class SlotBusy(SlotRefused):
    """The slot's execution lock is held: another process is running or recovering it."""


# ---------------------------------------------------------------------------
# Ledger location (mirrors src/spend.py, D70)
# ---------------------------------------------------------------------------


def default_ledger_path() -> Path:
    """The one per-user ledger: ``~/.local/state/claudecode/sealed_ledger.jsonl``."""
    # Keyed on the passwd entry, not $HOME: HOME=/other would otherwise hide the ledger.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "claudecode" / LEDGER_FILENAME


def _under_pytest() -> bool:
    """True only inside a pytest process (same rule as ``src.spend._under_pytest``).

    ``PYTEST_CURRENT_TEST`` alone can be exported by any shell; requiring the
    ``pytest`` module to be loaded as well closes that. (A process that imports
    pytest on purpose to spoof it remains an instruction-enforced residual, D70.)
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and "pytest" in sys.modules


def _same_path(a: Path, b: Path) -> bool:
    return os.path.abspath(os.path.realpath(a)) == os.path.abspath(os.path.realpath(b))


def resolve_ledger_path() -> Path:
    """Return the out-of-tree ledger path, enforcing the location rules.

    * outside pytest: ``CC_SEALED_LEDGER`` set -> ``LedgerRefused``; else the
      default per-user path;
    * under pytest: ``CC_SEALED_LEDGER`` must be set and must not be the default
      path -> else ``LedgerRefused`` (a test can never touch the real ledger).
    """
    override = os.environ.get(LEDGER_ENV)
    if not _under_pytest():
        if override:
            raise LedgerRefused(
                f"{LEDGER_ENV} is honoured only under pytest; unset it to use the per-user ledger"
            )
        return default_ledger_path()
    if not override:
        raise LedgerRefused(f"under pytest the default ledger is refused and {LEDGER_ENV} is not set")
    path = Path(override)
    if _same_path(path, default_ledger_path()):
        raise LedgerRefused(f"under pytest {LEDGER_ENV} must not point at the default ledger")
    return path


def check_ledger_path(ledger: os.PathLike[str] | str | None) -> Path:
    """Validate an explicit ``ledger`` argument (``None`` -> ``resolve_ledger_path``).

    Outside pytest only the default path is accepted (an arbitrary path would
    hide the epoch's history); under pytest the default path is refused.
    """
    if ledger is None:
        return resolve_ledger_path()
    path = Path(ledger)
    is_default = _same_path(path, default_ledger_path())
    if _under_pytest():
        if is_default:
            raise LedgerRefused("under pytest the default ledger is refused")
    else:
        if os.environ.get(LEDGER_ENV):
            raise LedgerRefused(f"{LEDGER_ENV} is honoured only under pytest")
        if not is_default:
            raise LedgerRefused("outside pytest the ledger must be the per-user ledger")
    return path


# ---------------------------------------------------------------------------
# File IO (the recipe of src/spend.py: flock, single-write append, fsync, torn tail)
# ---------------------------------------------------------------------------


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    """Open (creating) the ledger and hold an exclusive flock on it for the block."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)  # closing the descriptor releases the flock


@dataclass
class _Store:
    """One file's complete lines (without newlines) and the byte length they span."""

    path: Path
    lines: List[bytes]
    complete_len: int


def _read_store(path: Path) -> _Store:
    """Read ``path``; a final line without a newline (a torn write) is ignored."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return _Store(path, [], 0)
    complete_len = data.rfind(b"\n") + 1  # 0 when there is no full line
    return _Store(path, data[:complete_len].split(b"\n")[:-1], complete_len)


def _fsync_dir(directory: os.PathLike[str] | str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(directory: os.PathLike[str] | str) -> None:
    """fsync a directory, making the entries created or renamed in it durable."""
    _fsync_dir(directory)


def fsync_ancestry(path: os.PathLike[str] | str, stop_at: os.PathLike[str] | str | None = None) -> None:
    """fsync every directory that holds an entry on the way down to ``path``.

    ``path`` itself is not synced (its own contents are the caller's business);
    its parent is, then the parent's parent and so on, so that a directory
    created on the way is reachable after a crash. The walk ends at ``stop_at``
    (inclusive) when it is an ancestor of ``path``, else at the filesystem root.
    """
    target = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop_at)) if stop_at is not None else None
    if stop is not None and stop != target and stop not in target.parents:
        stop = None
    for parent in target.parents:
        _fsync_dir(parent)
        if parent == stop:
            break


class SlotLock:
    """A held, exclusive, non-blocking ``flock`` on one slot's lock file (see ``SealedLedger.lock_slot``).

    The lock lasts until :meth:`release` (idempotent), the end of a ``with``
    block, garbage collection, or the death of the process: whichever comes
    first. Closing the descriptor is what releases an ``flock``.
    """

    def __init__(self, fd: int, slot: str) -> None:
        self._fd: Optional[int] = fd
        self.slot = slot

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "SlotLock":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()

    def __del__(self) -> None:  # a dropped token must not keep its slot busy
        try:
            self.release()
        except Exception:  # noqa: BLE001 - nothing may escape a finaliser (the interpreter may be shutting down)
            pass


def _append_line(path: Path, line: bytes, complete_len: int, *, mode: int) -> None:
    """Append one line and fsync it (the caller holds the ledger lock).

    A torn tail past ``complete_len`` is truncated first, so a new line is never
    glued onto a crashed writer's partial line.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, mode)
    try:
        if os.fstat(fd).st_size != complete_len:
            os.ftruncate(fd, complete_len)
        view = memoryview(line + b"\n")
        while view:
            view = view[os.write(fd, view):]  # O_APPEND: lands at the (new) end
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


def _dumps(obj: Any) -> bytes:
    """Canonical JSON: sorted keys, no spaces, ASCII, finite numbers only."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _write_payload_file(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file, fsync, rename, fsync dir).

    An existing file is left alone: the caller (``store_payload``) has already
    checked that it holds the same bytes (a re-run after a crash between the
    two copies).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        return
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Records: validation (content-free), mirror projection, replay
# ---------------------------------------------------------------------------


def _hex64(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _token(value: Any, name: str, pattern: "re.Pattern[str]" = _TOKEN) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} must match {pattern.pattern!r} (ids and enums only, never text)")
    return value


def _count(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _totals(value: Mapping[str, int]) -> Dict[str, int]:
    return {_token(k, "totals key"): _count(v, f"totals[{k!r}]") for k, v in dict(value).items()}


def _slot_id(slot: int | str) -> str:
    """Normalise a slot to its ledger id: ``"0"``, ``"0b"``, ``"1"``, ``"2"``..."""
    text = str(slot) if isinstance(slot, int) and not isinstance(slot, bool) else slot
    if not isinstance(text, str) or not _SLOT.fullmatch(text):
        raise ValueError("slot must be a non-negative integer or '0b'")
    return text


def _project(record: Mapping[str, Any], line: bytes) -> bytes:
    """The committed-mirror line for one private record: kind and digests only."""
    out: Dict[str, Any] = {
        "seq": record["seq"],
        "kind": record["kind"],
        "digest": hashlib.sha256(line).hexdigest(),
    }
    for key in MIRROR_VALUE_FIELDS.get(record["kind"], ()):
        if key in record:
            out[key] = record[key]
    digests = {k: record[k] for k in MIRROR_DIGEST_FIELDS.get(record["kind"], ()) if k in record}
    if digests:
        out["digests"] = digests
    for key in _all_keys(out):
        if any(part in key for part in FORBIDDEN_MIRROR_KEY_PARTS):
            raise LedgerCorrupt(f"mirror projection would carry the forbidden key {key!r}")
    return _dumps(out)


def _all_keys(obj: Any) -> Iterator[str]:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            yield str(key)
            yield from _all_keys(value)


def _parse_record(raw: bytes, index: int) -> Dict[str, Any]:
    try:
        rec = json.loads(raw)
    except ValueError:
        raise LedgerCorrupt(f"ledger line {index + 1} is not JSON") from None
    if (
        not isinstance(rec, dict)
        or rec.get("v") != SCHEMA_VERSION
        or rec.get("seq") != index
        or rec.get("kind") not in KINDS
    ):
        raise LedgerCorrupt(f"ledger line {index + 1} is not a v{SCHEMA_VERSION} record in sequence")
    return rec


@dataclass
class _SlotState:
    """What the ledger says about one slot."""

    stored: Optional[Dict[str, Any]] = None
    intent: Optional[Dict[str, Any]] = None
    consumed: bool = False


def _replay(records: List[Dict[str, Any]]) -> Dict[str, _SlotState]:
    """Fold the slot records into per-slot state; an impossible order is corruption.

    Only claimed slots appear in the result.
    """
    slots: Dict[str, _SlotState] = {}
    frozen_splits = set()
    for rec in records:
        kind = rec["kind"]
        if kind == "freeze":
            split = rec.get("split")
            if not isinstance(split, str) or not _TOKEN.fullmatch(split):
                raise LedgerCorrupt(f"record {rec['seq']}: bad split")
            if split in frozen_splits:
                raise LedgerCorrupt(f"record {rec['seq']}: a second freeze of split {split!r}")
            frozen_splits.add(split)
        if not kind.startswith(("slot_", "publication_")):
            continue
        slot = rec.get("slot")
        if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
            raise LedgerCorrupt(f"record {rec['seq']}: bad slot")
        state = slots.get(slot)
        if kind == "slot_claim":
            if state is not None:
                raise LedgerCorrupt(f"record {rec['seq']}: slot {slot} claimed twice")
            slots[slot] = _SlotState()
            continue
        if state is None:
            raise LedgerCorrupt(f"record {rec['seq']}: {kind} for unclaimed slot {slot}")
        if kind == "slot_stored":
            if state.stored is not None:
                raise LedgerCorrupt(f"record {rec['seq']}: slot {slot} stored twice")
            state.stored = rec
        elif state.stored is None or state.consumed:
            raise LedgerCorrupt(f"record {rec['seq']}: {kind} out of order for slot {slot}")
        elif kind == "publication_intent":
            if state.intent is not None:
                raise LedgerCorrupt(f"record {rec['seq']}: slot {slot} has two intents")
            state.intent = rec
        else:  # slot_consumed
            state.consumed = True
    return slots


def _slot0_ended_non_canonical(slots: Mapping[str, _SlotState]) -> bool:
    s0 = slots.get("0")
    if s0 is None or s0.intent is not None:
        return False
    # Stored but not yet consumed: still undecided/unrendered, so not ended.
    return s0.stored is None or s0.consumed


def _may_use_0b(slots: Mapping[str, _SlotState]) -> bool:
    return "0b" not in slots and _slot0_ended_non_canonical(slots)


def _check_claimable(sid: str, slots: Mapping[str, _SlotState]) -> None:
    if sid in slots:
        raise SlotRefused(f"slot {sid} was claimed before")
    if sid == "0b" and not _may_use_0b(slots):
        raise SlotRefused("slot 0b is not available")


def below_floor(eligible_after_exclusion: int, floor: int) -> bool:
    """True when the eligible answerable families fell below the floor.

    That is the epoch end (D64 stop): no top-up after freeze. Exactly at the
    floor is still enough.
    """
    return _count(eligible_after_exclusion, "eligible_after_exclusion") < _count(floor, "floor")


def group_ids(families: Iterable[str], edges: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Family -> group id: the connected components of ``edges``, each named by its smallest member family.

    This is the one definition of a sealed group id (see the module docstring):
    ``sealed_tool`` (``exclude``, ``freeze``, ``power``) and the slot gate both
    call it with the frozen sealed families and the twins record's edges. An
    edge endpoint outside ``families`` (a draft that was rejected or dropped)
    keeps its connectivity but is never a member, and a family with no edge is
    a group of its own.
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
        smallest.setdefault(find(f), f)
    return {f: smallest[find(f)] for f in members}


def exclusions_digest(groups: Iterable[str]) -> str:
    """sha256 of the canonical JSON list of the sorted, distinct group ids (what the sealed freeze binds)."""
    return hashlib.sha256(_dumps(sorted(set(groups)))).hexdigest()


def digest_run_dir(run_dir: os.PathLike[str] | str) -> str:
    """sha256 over a run directory: every regular file's relative path and sha256.

    The digest changes if any stored result, sidecar or artifact is edited,
    added or removed. Symlinks are refused (their target could change later).
    The one exception is ``DIGEST_EXEMPT``: ``report.md`` (and its temporary
    file) at the directory's top level is the rendered output of a confirmatory
    slot, written AFTER step 3 from the digested files, so it can never be part of
    what step 3 recorded.
    """
    root = Path(run_dir)
    if not root.is_dir():
        raise ProtocolError("the run directory does not exist")
    entries = sorted(
        p for p in root.rglob("*")
        if (not p.is_dir() or p.is_symlink()) and p.relative_to(root).as_posix() not in DIGEST_EXEMPT
    )
    if not entries:
        raise ProtocolError("the run directory holds no results")
    outer = hashlib.sha256()
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise ProtocolError("the run directory may hold regular files only")
        inner = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                inner.update(chunk)
            os.fsync(fh.fileno())  # step 3 means durable: make sure, whatever the caller did
        outer.update(f"{path.relative_to(root).as_posix()}\0{inner.hexdigest()}\n".encode("utf-8"))
    _fsync_dir(root)
    return outer.hexdigest()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class RecoveryAction(str, Enum):
    """What the caller does for a slot after a crash. None of them is a fresh sealed call."""

    NOT_STARTED = "not_started"  # never claimed: nothing to recover
    SPENT = "spent"  # claimed, no stored results: the slot is spent, never re-run
    RENDER_FROM_STORAGE = "render_from_storage"  # stored, no intent: render; consumed only once rendered
    REPUBLISH = "republish"  # a durable intent: republish the stored result
    CONSUMED = "consumed"  # finished: nothing to do


@dataclass(frozen=True)
class Recovery:
    """The recovery verdict for one slot, with the stored run id and digest if any."""

    slot: str
    action: RecoveryAction
    run_id: Optional[str] = None
    results_digest: Optional[str] = None


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class _Txn:
    """One locked, reconciled view of the ledger; ``append`` writes to every store."""

    def __init__(self, owner: "SealedLedger", records: List[Dict[str, Any]]) -> None:
        self._owner = owner
        self.records = records

    @property
    def slots(self) -> Dict[str, _SlotState]:
        return _replay(self.records)

    def append(self, kind: str, **fields: Any) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "seq": len(self.records),
            "kind": kind,
            "ts": self._owner._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields,
        }
        self._owner._write(rec)
        self.records.append(rec)
        return rec


class SealedLedger:
    """The sealed epoch's ledger and slot state machine (see the module docstring).

    ``private_copy_dir`` is the directory of the private copy (production:
    ``eval/private/sealed/ledger/``); ``mirror`` is the optional committed
    mirror file; ``ledger`` is the out-of-tree ledger (``None`` -> the per-user
    default, or ``CC_SEALED_LEDGER`` under pytest); ``clock`` is for tests.
    """

    def __init__(
        self,
        private_copy_dir: os.PathLike[str] | str,
        *,
        mirror: os.PathLike[str] | str | None = None,
        ledger: os.PathLike[str] | str | None = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._ledger = check_ledger_path(ledger)
        self._copy = Path(private_copy_dir) / LEDGER_FILENAME
        self._mirror = Path(mirror) if mirror is not None else None
        paths = [p for p in (self._ledger, self._copy, self._mirror) if p is not None]
        for i, a in enumerate(paths):
            if any(_same_path(a, b) for b in paths[:i]):
                raise LedgerRefused("the ledger, its private copy and the mirror must be different files")
        self._payload_dirs = (self._ledger.parent / PAYLOAD_DIRNAME, Path(private_copy_dir) / PRIVATE_PAYLOAD_DIRNAME)
        if _same_path(*self._payload_dirs):
            raise LedgerRefused("the two payload directories must be different")
        self._lock_dir = self._ledger.parent / SLOT_LOCK_DIRNAME
        self._copy_dir = Path(private_copy_dir)
        self._clock = clock or _utcnow

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _txn(self) -> Iterator[_Txn]:
        """Lock the ledger, reconcile the three stores, and yield the records."""
        with _locked(self._ledger):
            yield _Txn(self, self._reconcile())

    def _reconcile(self) -> List[Dict[str, Any]]:
        stores = [_read_store(self._ledger), _read_store(self._copy)]
        truth = max(stores, key=lambda s: len(s.lines)).lines
        for store in stores:
            if truth[: len(store.lines)] != store.lines:
                raise LedgerCorrupt("the out-of-tree ledger and its private copy disagree")
        records = [_parse_record(raw, i) for i, raw in enumerate(truth)]
        _replay(records)  # fail closed on an impossible order before anything is written
        projections = [_project(rec, raw) for rec, raw in zip(records, truth)]
        mirror = _read_store(self._mirror) if self._mirror is not None else None
        if mirror is not None:
            shared = min(len(mirror.lines), len(projections))
            if mirror.lines[:shared] != projections[:shared]:
                raise MirrorTampered("an earlier line of the committed mirror has changed")
            if len(mirror.lines) > len(projections):
                raise EpochStop(
                    f"both private copies hold {len(projections)} records but the committed "
                    f"mirror attests {len(mirror.lines)}: the private ledgers are lost"
                )
        for store in stores:  # restore a lost or lagging private store
            self._catch_up(store, truth, mode=0o600)
        if mirror is not None:
            self._catch_up(mirror, projections, mode=0o644)
        return records

    @staticmethod
    def _catch_up(store: _Store, target: List[bytes], *, mode: int) -> None:
        complete_len = store.complete_len
        for line in target[len(store.lines):]:
            _append_line(store.path, line, complete_len, mode=mode)
            complete_len += len(line) + 1

    def _write(self, record: Dict[str, Any]) -> None:
        """Append a new record to every store: ledger, private copy, then mirror."""
        raw = _dumps(record)
        line_for = {self._ledger: raw, self._copy: raw}
        if self._mirror is not None:
            line_for[self._mirror] = _project(record, raw)
        for path, line in line_for.items():
            _append_line(path, line, _read_store(path).complete_len, mode=0o644 if path == self._mirror else 0o600)

    # -- split, release, freeze ----------------------------------------------

    def record_split_attempt(
        self, *, round_no: int, pool_digest: str, outcome: str, payload_sha256: Optional[str] = None
    ) -> None:
        """Record a split attempt: the pool digest and its outcome (an enum token).

        ``payload_sha256`` optionally names the stored payload (``store_payload``)
        the attempt refers to.
        """
        fields: Dict[str, Any] = {
            "round": _count(round_no, "round_no", minimum=1),
            "pool_digest": _hex64(pool_digest, "pool_digest"),
            "outcome": _token(outcome, "outcome"),
        }
        if payload_sha256 is not None:
            fields["payload_sha256"] = _hex64(payload_sha256, "payload_sha256")
        with self._txn() as txn:
            txn.append("split_attempt", **fields)

    def record_release(
        self,
        *,
        round_no: int,
        assignment_digest: str,
        totals: Optional[Mapping[str, int]] = None,
        payload_sha256: Optional[str] = None,
    ) -> None:
        """Record a release/assignment: a digest and optional counts, never content.

        ``payload_sha256`` optionally names the stored payload (the content-free
        per-family records, seed and assignment, ``store_payload``) that freeze
        replays the released split from.
        """
        fields: Dict[str, Any] = {
            "round": _count(round_no, "round_no", minimum=1),
            "assignment_digest": _hex64(assignment_digest, "assignment_digest"),
        }
        if totals is not None:
            fields["totals"] = _totals(totals)
        if payload_sha256 is not None:
            fields["payload_sha256"] = _hex64(payload_sha256, "payload_sha256")
        with self._txn() as txn:
            txn.append("release", **fields)

    def record_freeze(
        self,
        *,
        split: str,
        set_sha256: str,
        totals: Mapping[str, int],
        twins_sha256: Optional[str] = None,
        exclusions_sha256: Optional[str] = None,
    ) -> None:
        """Record one split's freeze: its file's sha256 and totals.

        The ``sealed`` split also binds the twins record's sha256 (required) and
        the exclusion list's: ``exclusions_sha256=None`` means "the exclusions
        recorded so far", digested INSIDE this transaction (so an exclusion
        cannot slip in between the digest and the freeze record); a given value
        must equal that digest or ``ProtocolError`` is raised. The other splits
        may omit both. A split's frozen file is immutable for the epoch, so a
        second freeze of the SAME split is refused (other splits freeze
        separately).
        """
        name = _token(split, "split")
        fields: Dict[str, Any] = {
            "split": name,
            "set_sha256": _hex64(set_sha256, "set_sha256"),
            "totals": _totals(totals),
        }
        if name == SEALED_SPLIT and twins_sha256 is None:
            raise ValueError(f"the {SEALED_SPLIT} freeze binds the twins record")
        if twins_sha256 is not None:
            fields["twins_sha256"] = _hex64(twins_sha256, "twins_sha256")
        if exclusions_sha256 is not None:
            _hex64(exclusions_sha256, "exclusions_sha256")
        with self._txn() as txn:
            if any(r["kind"] == "freeze" and r["split"] == name for r in txn.records):
                raise ProtocolError(f"split {name!r} is already frozen; its frozen file is immutable")
            if name == SEALED_SPLIT or exclusions_sha256 is not None:
                current = exclusions_digest(r["group_id"] for r in txn.records if r["kind"] == "exclusion")
                if exclusions_sha256 is not None and exclusions_sha256 != current:
                    raise ProtocolError("exclusions_sha256 is not the digest of the exclusions recorded so far")
                fields["exclusions_sha256"] = current
            txn.append("freeze", **fields)

    def freeze_record(self, split: str) -> Optional[Dict[str, Any]]:
        """The named split's freeze record, or ``None`` before its freeze."""
        name = _token(split, "split")
        found = [r for r in self.records("freeze") if r["split"] == name]
        return found[0] if found else None

    # -- sealed-processor dispatches (a7) --------------------------------------------

    def record_dispatch(self, *, dispatch_id: str, dispatch_kind: str, argv_hash: str) -> None:
        """Record that a sealed-processor run was started (ids, an enum and a digest; the time is the record's ``ts``)."""
        fields: Dict[str, Any] = {
            "dispatch_id": _token(dispatch_id, "dispatch_id", _DISPATCH_ID),
            "dispatch_kind": _token(dispatch_kind, "dispatch_kind"),
            "argv_hash": _hex64(argv_hash, "argv_hash"),
        }
        with self._txn() as txn:
            txn.append("dispatch", **fields)

    # -- payloads (content-free records too big for a ledger line) -------------------

    def store_payload(self, name: str, obj: Any) -> str:
        """Write ``<name>.json`` canonically to BOTH payload directories; returns its sha256.

        The directories are ``sealed_payloads/`` next to the out-of-tree ledger
        (so the pytest-only ledger override moves it too) and ``payloads/`` in
        the private copy. Each file is fsynced and renamed into place. The
        digest is of the file's bytes (canonical JSON: sorted keys, no spaces,
        ASCII, no NaN). A payload is immutable: storing the same content again
        fills in a missing copy, different content under the name is refused.
        """
        pname = _token(name, "payload name", _PAYLOAD_NAME)
        try:
            data = _dumps(obj)
        except (TypeError, ValueError):
            raise ValueError("a payload must be JSON-serialisable (string keys, finite numbers)") from None
        with self._txn():  # serialise with every other writer, and fail closed on a broken ledger
            for directory in self._payload_dirs:
                existing = directory / f"{pname}.json"
                if existing.exists() and existing.read_bytes() != data:
                    raise ProtocolError(f"payload {pname!r} is already stored with different content")
            for directory in self._payload_dirs:
                _write_payload_file(directory / f"{pname}.json", data)
        return hashlib.sha256(data).hexdigest()

    def load_payload(self, name: str, sha256: str) -> Any:
        """Read a payload from whichever copy verifies against ``sha256``.

        Raises ``EpochStop`` if neither copy exists with that digest (both
        private copies of the payload are lost or altered).
        """
        pname = _token(name, "payload name", _PAYLOAD_NAME)
        expected = _hex64(sha256, "sha256")
        for directory in self._payload_dirs:
            try:
                data = (directory / f"{pname}.json").read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() == expected:
                return json.loads(data)
        raise EpochStop(f"payload {pname!r} verifies against its digest in neither copy")

    def records(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every record (or those of one kind), oldest first."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"unknown record kind {kind!r}")
        with self._txn() as txn:
            return [dict(r) for r in txn.records if kind is None or r["kind"] == kind]

    # -- the slot protocol ----------------------------------------------------

    @contextmanager
    def _slot_zero_idle(self) -> Iterator[None]:
        """Hold slot 0's execution lock (non-blocking) for the block, or refuse: slot 0 is still running."""
        try:
            guard = self.lock_slot("0")
        except SlotBusy:
            raise SlotBusy("slot 0 is still running; slot 0b is not available") from None
        try:
            yield
        finally:
            guard.release()

    def slot_lock_path(self, slot: int | str) -> Path:
        """The lock file of ``slot`` (what :meth:`lock_slot` flocks)."""
        return self._lock_dir / f"slot-{_slot_id(slot)}.lock"

    def lock_slot(self, slot: int | str) -> SlotLock:
        """Take the slot's execution lock without waiting (see the module docstring).

        The caller holds it for the whole run of the slot (or its recovery) and
        calls ``release()``; the OS drops it if the process dies.

        Raises:
            SlotBusy: another process (or another holder in this one) has it.
        """
        sid = _slot_id(slot)
        self._lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.slot_lock_path(sid), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SlotBusy(f"slot {sid} is held by a running process") from None
        except BaseException:
            os.close(fd)
            raise
        return SlotLock(fd, sid)

    def claim(self, slot: int | str) -> str:
        """Step 1: claim the slot. Refused if it was claimed before (spent slots included).

        Slot ``"0b"`` is also refused unless ``may_use_0b()`` AND slot 0 is not
        running right now (slot 0's execution lock is taken, non-blocking, for
        the duration of the claim). Returns the normalised slot id.
        """
        sid = _slot_id(slot)
        with self._slot_zero_idle() if sid == "0b" else _nullcontext():
            with self._txn() as txn:
                _check_claimable(sid, txn.slots)
                txn.append("slot_claim", slot=sid)
        return sid

    def check_claimable(self, slot: int | str) -> None:
        """Raise ``SlotRefused`` if ``claim(slot)`` would be refused on the ledger's state; append nothing.

        Advisory (a gate's early answer): the claim itself re-checks under the lock, and for ``"0b"`` it
        also needs slot 0 not to be running, which only the claim can establish.
        """
        sid = _slot_id(slot)
        with self._txn() as txn:
            _check_claimable(sid, txn.slots)

    def store_results(self, slot: int | str, run_dir: os.PathLike[str] | str) -> str:
        """Step 3: record that durable results exist in ``run_dir``; returns their digest.

        The caller has written the files; this fsyncs them, digests the
        directory (``digest_run_dir``), makes the directory's own ancestry
        durable (a run directory created for this run is reachable after a
        crash) and records the digest and the run id (the directory name).
        """
        sid = _slot_id(slot)
        run_id = _token(Path(run_dir).name, "run id", _ID)
        digest = digest_run_dir(run_dir)
        fsync_ancestry(run_dir, stop_at=self._common_ancestor(Path(run_dir)))
        with self._txn() as txn:
            state = txn.slots.get(sid)
            if state is None:
                raise ProtocolError(f"slot {sid} is not claimed")
            if sid == "0" and "0b" in txn.slots:
                raise ProtocolError("slot 0 was superseded by slot 0b; it can store nothing more")
            if state.stored is not None:
                raise ProtocolError(f"slot {sid} already has stored results")
            txn.append("slot_stored", slot=sid, run_id=run_id, results_digest=digest)
        return digest

    def _common_ancestor(self, run_dir: Path) -> Optional[Path]:
        """The deepest directory above both ``run_dir`` and the private copy (the sealed tree's root), if any."""
        try:
            common = os.path.commonpath([os.path.abspath(run_dir), os.path.abspath(self._copy_dir)])
        except ValueError:
            return None
        return Path(common) if common not in ("", os.sep) else None

    def record_intent(self, slot: int | str) -> None:
        """Step 4, canonical runs only: record the durable publication intent.

        A non-canonical run records nothing. An intent disqualifies 0b for good.
        """
        sid = _slot_id(slot)
        with self._txn() as txn:
            if sid == "0" and "0b" in txn.slots:
                raise ProtocolError("slot 0 was superseded by slot 0b; it can publish nothing")
            state = txn.slots.get(sid)
            if state is None or state.stored is None:
                raise ProtocolError(f"slot {sid} has no stored results to publish")
            if state.consumed:
                raise ProtocolError(f"slot {sid} is already consumed")
            if state.intent is not None:
                raise ProtocolError(f"slot {sid} already has a publication intent")
            txn.append("publication_intent", slot=sid, results_digest=state.stored["results_digest"])

    def record_consumed(self, slot: int | str) -> None:
        """Step 6: the stored results were rendered (and published, if canonical)."""
        sid = _slot_id(slot)
        with self._txn() as txn:
            state = txn.slots.get(sid)
            if state is None or state.stored is None:
                raise ProtocolError(f"slot {sid} has no stored results to consume")
            if state.consumed:
                raise ProtocolError(f"slot {sid} is already consumed")
            txn.append("slot_consumed", slot=sid)

    def recover(self, slot: int | str) -> Recovery:
        """The recovery action for a slot. Read-only; never implies a fresh sealed call."""
        sid = _slot_id(slot)
        with self._txn() as txn:
            state = txn.slots.get(sid)
        if state is None:
            return Recovery(sid, RecoveryAction.NOT_STARTED)
        if state.stored is None:
            return Recovery(sid, RecoveryAction.SPENT)
        if state.consumed:
            action = RecoveryAction.CONSUMED
        elif state.intent is not None:
            action = RecoveryAction.REPUBLISH
        else:
            action = RecoveryAction.RENDER_FROM_STORAGE
        return Recovery(sid, action, state.stored["run_id"], state.stored["results_digest"])

    def may_use_0b(self) -> bool:
        """True only if slot 0 was claimed and ended non-canonical, and 0b is unused.

        "Ended non-canonical" means no intent was ever recorded for slot 0 and,
        if it stored results, it has been consumed. Any intent on slot 0, even
        with rendering interrupted, disqualifies 0b.
        """
        with self._txn() as txn:
            return _may_use_0b(txn.slots)

    # -- exclusions -------------------------------------------------------------

    def exclude(self, group_id: str, reason: str, members: Optional[Sequence[str]] = None) -> None:
        """Append an exclusion against a whole group (``reason`` is an enum token, e.g. ``s_hit``).

        ``members`` are the group's families at the time (family ids, never text);
        ``None`` means ``[group_id]``, the group's own smallest-member name. The
        gate resolves the families (``excluded_families``) against the frozen
        groups at run time, so a group renamed by a later rejection still resolves.
        """
        fields: Dict[str, Any] = {"group_id": _token(group_id, "group_id", _ID), "reason": _token(reason, "reason")}
        if members is None:
            members = [fields["group_id"]]
        elif isinstance(members, (str, bytes)):
            raise ValueError("members must be a sequence of family ids")
        names = sorted({_token(m, "member", _ID) for m in members})
        if not names:
            raise ValueError("an exclusion names at least one member family")
        fields["members"] = names
        with self._txn() as txn:
            txn.append("exclusion", **fields)

    def exclusions(self) -> FrozenSet[str]:
        """The group ids excluded so far."""
        return frozenset(r["group_id"] for r in self.records("exclusion"))

    def excluded_families(self) -> FrozenSet[str]:
        """Every family named by an exclusion so far (the recorded ``members``; a legacy record: its group id).

        The slot gate maps these onto the freeze-bound groups, so it never
        depends on a group *name* that a later rejection may have changed.

        Raises:
            LedgerCorrupt: a record's ``members`` is not a list of family ids.
        """
        out = set()
        for rec in self.records("exclusion"):
            members = rec.get("members", [rec["group_id"]])
            if not (
                isinstance(members, list) and members
                and all(isinstance(m, str) and _ID.fullmatch(m) for m in members)
            ):
                raise LedgerCorrupt(f"record {rec['seq']}: bad exclusion members")
            out.update(members)
        return frozenset(out)

    def exclusions_at_freeze(self, split: str = SEALED_SPLIT) -> Optional[FrozenSet[str]]:
        """The groups excluded BEFORE the named split's freeze record (``None`` if it is not frozen yet).

        This is the list the freeze binds with ``exclusions_sha256``: the ledger is append-only, so the
        gate re-derives it from the ledger instead of reading a separate file.
        """
        records = self.records()
        freeze = next((r for r in records if r["kind"] == "freeze" and r["split"] == _token(split, "split")), None)
        if freeze is None:
            return None
        return frozenset(r["group_id"] for r in records if r["kind"] == "exclusion" and r["seq"] < freeze["seq"])

    below_floor = staticmethod(below_floor)
