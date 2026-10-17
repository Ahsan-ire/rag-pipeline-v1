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
hash, the sha256 of the pre-registration it ran under (the first record of any kind
that carries one registers the epoch's prereg) and the UTC time. Nothing else. The
committed mirror carries the record's kind, line digest and that prereg digest.

Development draws (b1a r5, r6)
------------------------------
``record_dev_draw`` appends a ``dev_draw`` record for each development draw (b7) that may count: the pipeline
configuration hash it ran under, its expansion artifact's sha256 and its rows sidecar's sha256 (all digests, which the
committed mirror carries), the run id that produced it (its **execution receipt**: distinct per draw, so a copy of a
sidecar or a second recording of one run is not another draw), plus a per-configuration index (1, 2, ...) that the ledger
assigns. Since r6 the only caller is ``pipeline eval --record-draw``, **inside the run, before the run prints or writes
any result**: a draw cannot be chosen after its numbers are seen, and a crash before the record leaves no countable draw.
A replay candidate's draw carries ``replay_of`` (the incumbent's draw it replayed): the ledger checks, inside its
transaction, that the artifact is that draw's artifact and that the candidate's draw index equals ``replay_of`` (paired by
draw index). A draw that is not recorded never counts: the cap is on what may ever be recorded, so a re-roll by a
non-semantic edit (a new config hash, hence a new "first five") cannot hide among unrecorded runs. At most
``DEV_DRAW_CONFIGS_MAX`` (5: the incumbent and four candidates) distinct configuration hashes may have records; before the
``controls`` record exists only ONE (the incumbent's). ``dev_draws(config_hash)`` lists a configuration's records in
order and ``dev_draw_configs()`` lists the configurations in the order they were first recorded. ``check_dev_draw_config``
is the same cap and controls rule as a pre-check (``pipeline eval --record-draw`` runs it before any call).

``record_dev_run_unrecorded`` appends a ``dev_run_unrecorded`` record (the run's configuration hash or ``None``, and its
run id) for every ``pipeline eval`` run over a frozen tuning or realistic version that was not a ``--record-draw`` run,
before the run prints its results. Such a run never counts, but it is visible: ``dev_unrecorded_counts()`` counts them per
configuration (``sealed_tool draws`` prints the counts). Since r7 a ``--record-draw`` run whose record failed or was refused
at or after the sidecar write is logged the same way, with ``reason`` ``record_failed``.

Since r7 a ``dev_draw`` also stores the draw's ``mode`` (``build`` or ``replay``; one configuration's draws are all one mode),
``replay_of`` (the incumbent draw number, or null), the ``rewrite_config_hash`` of its expansions and the run's
``scorer_identity`` (the last two from the rows sidecar). A draw whose rewrite fell back for any question is not recorded:
``record_dev_draw_void`` logs a ``dev_draw_void`` (configuration hash, run id, reason ``rewrite_fallback``) instead, and the
draw consumes no index. ``record_batch_declaration`` binds the batch to those fields (``DrawBindingRefused``): the counted
draws of every runnable entry carry the incumbent's scorer identity; a ``replay`` entry's are replay draws of the incumbent's
same-index draws; a ``candidate`` entry's are build draws under a rewrite identity that is the entry's and is not the
incumbent's.

``record_batch_declaration(candidates, draws_required=...)`` enforces the draw coverage unconditionally (``draws_required`` is
the prereg's ``draws_r`` and has no default): every runnable candidate has at least that many recorded draws, every recorded
configuration other than the incumbent's is in the batch, and a recorded configuration with fewer draws than required may be
declared ``abandoned`` (it counts toward Holm's K, and the slot gate refuses to run it).

Groups and exclusions
---------------------
``group_ids`` is the **one definition of a sealed group id** (the connected
components of the twins record's edges over the frozen sealed families, named by
the smallest member family): ``sealed_tool exclude`` and the slot gate both call
it. It, ``below_floor`` and the aggregation behind ``excluded_families`` live in
``src.sealed_groups`` (covered by the scorer's identity, which the ledger is not: they decide what the analysis is made of)
and are re-exported here. A group's *name* can change when a later rejection removes its smallest
member, so an exclusion also records its ``members`` (the group's families when it
was made) and the gate resolves ``excluded_families()`` against the freeze-bound
groups at run time instead of trusting the recorded name.
``exclusions_digest`` and ``SealedLedger.exclusions_at_freeze`` give the digest
the sealed freeze binds, so the slot gate re-checks it from the ledger itself
(no separate exclusion file). Since r7 an exclusion also records ``slots_stored``, the slots whose results were stored when it
was made, so the confirmatory comparison can use the exclusions recorded before slot 0 was stored plus each confirmatory
slot's own ``s_hit`` ones (prereg ``adoption.exclusions``). Since r8 an ``s_hit`` that the publication scanner wrote also
records ``scan_slot``, the slot whose report was scanned (``sealed_tool exclude`` writes none after slot 0 is stored).

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

from src.sealed_groups import below_floor, excluded_member_families, group_ids  # hashed: the units, the floor, the exclusions

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
    "batch_declaration",
    "calibration_draw",
    "controls",
    "dev_draw",
    "dev_run_unrecorded",
    "dev_draw_void",
)

#: The only fields of a record the committed mirror may carry besides the
#: record's kind, sequence number and line digest (all sha256 hex digests).
MIRROR_DIGEST_FIELDS: Dict[str, tuple] = {
    "split_attempt": ("pool_digest", "payload_sha256", "prereg_sha256"),
    "release": ("assignment_digest", "payload_sha256"),
    "freeze": ("set_sha256", "twins_sha256", "exclusions_sha256", "prereg_sha256"),
    "slot_stored": ("results_digest", "expansion_artifact_sha256"),
    "batch_declaration": ("candidates_sha256",),
    "dispatch": ("prereg_sha256",),
    "calibration_draw": ("draw_sha256",),
    "controls": ("controls_sha256", "draws_sha256", "prereg_sha256", "incumbent_config_hash"),
    "dev_draw": ("config_hash", "artifact_sha256", "rows_sidecar_sha256"),
    "dev_run_unrecorded": ("config_hash",),
    "dev_draw_void": ("config_hash",),
    "publication_intent": ("results_digest",),
}

#: The confirmatory batch holds at most this many candidates (slots 1..K_MAX).
K_MAX = 4

#: How a development draw was made: ``build`` (live expansion, frozen into a new artifact) or ``replay`` (a candidate's draw
#: replays the incumbent's draw of the same index). One configuration's draws are all one mode.
DRAW_MODES = ("build", "replay")

#: Why a draw was voided instead of recorded (``record_dev_draw_void``): a rewrite fell back to the raw query during the draw.
DRAW_VOID_REASONS = ("rewrite_fallback",)

#: Why a ``--record-draw`` run left only an ``dev_run_unrecorded`` record: its record failed, or was refused, at or after the
#: sidecar write.
UNRECORDED_REASONS = ("record_failed",)

#: At most this many distinct pipeline configurations may ever have ``dev_draw`` records in an epoch: the incumbent's
#: and up to ``K_MAX`` candidates' (prereg ``adoption.development_evidence.recorded_configs_max`` counts the candidates).
DEV_DRAW_CONFIGS_MAX = 1 + K_MAX

#: The 16A-2b confirmatory embargo: while True no batch can be declared and slots 1-4 cannot be claimed.
#: Lifted only by 16B's reviewed code change, which must also add the candidate-identity check (the run's
#: computed configuration hash equals the declared one): prereg ``adoption.confirmatory_batch.state``.
#: Always read as ``sealed_ledger.CONFIRMATORY_EMBARGO`` at call time, so a test can monkeypatch it.
CONFIRMATORY_EMBARGO = True

#: The expansion modes a batch entry may declare: retrieval-only (replay slot 0's canonical artifact) or a
#: rewrite-changing candidate (live rewrite under its configuration hash).
BATCH_EXPANSIONS = ("replay", "candidate")

#: A batch entry may also be ``abandoned``: a recorded configuration with fewer than ``draws_r`` recorded draws. It counts toward
#: Holm's K (the batch holds at most ``K_MAX`` entries, whatever their mode) and the slot gate refuses to run it.
ABANDONED = "abandoned"
BATCH_ENTRY_EXPANSIONS = (*BATCH_EXPANSIONS, ABANDONED)

#: The run-directory file a slot-0/0b run builds; its sha256 is recorded on ``slot_stored``.
EXPANSION_ARTIFACT_NAME = "expansion_artifact.json"

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
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{3,64}")  # a private run id (eval_privacy.new_run_id): the execution receipt of a draw
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


class DrawCoverageRefused(ProtocolError):
    """``record_batch_declaration`` refused: the recorded development draws do not cover the batch (b1a r5)."""


class DrawBindingRefused(ProtocolError):
    """``record_batch_declaration`` refused: the counted draws are not bound to the batch entry's mode, or to the
    incumbent's scorer, as the entry requires (b1a r7)."""


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


def _canonical(obj: Any) -> bytes:
    """Canonical JSON bytes of a value (what ``candidates_sha256`` digests)."""
    return _dumps(obj)


def _batch_of(records: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The batch declaration among ``records``, if any."""
    return next((r for r in records if r["kind"] == "batch_declaration"), None)


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
    batch_seen = False
    controls_seen = False
    draw_configs: List[str] = []
    for rec in records:
        kind = rec["kind"]
        if kind == "controls":
            controls_seen = True
        if kind == "dev_draw":
            config = rec.get("config_hash")
            if not isinstance(config, str) or not _HEX64.fullmatch(config):
                raise LedgerCorrupt(f"record {rec['seq']}: bad dev_draw config hash")
            if config not in draw_configs:
                if draw_configs and not controls_seen:
                    raise LedgerCorrupt(f"record {rec['seq']}: a second draw configuration before the controls record")
                if len(draw_configs) >= DEV_DRAW_CONFIGS_MAX:
                    raise LedgerCorrupt(f"record {rec['seq']}: more than {DEV_DRAW_CONFIGS_MAX} draw configurations")
                draw_configs.append(config)
        if kind == "freeze":
            split = rec.get("split")
            if not isinstance(split, str) or not _TOKEN.fullmatch(split):
                raise LedgerCorrupt(f"record {rec['seq']}: bad split")
            if split in frozen_splits:
                raise LedgerCorrupt(f"record {rec['seq']}: a second freeze of split {split!r}")
            frozen_splits.add(split)
        if kind == "batch_declaration":
            if batch_seen:
                raise LedgerCorrupt(f"record {rec['seq']}: a second batch declaration")
            if any(s in slots for s in ("1", "2", "3", "4")):
                raise LedgerCorrupt(f"record {rec['seq']}: batch declared after a confirmatory slot was claimed")
            batch_seen = True
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


def _check_claimable(sid: str, slots: Mapping[str, _SlotState], batch: Optional[Mapping[str, Any]] = None) -> None:
    if sid in slots:
        raise SlotRefused(f"slot {sid} was claimed before")
    if sid not in ("0", "0b") and CONFIRMATORY_EMBARGO:
        raise SlotRefused(f"slot {sid} is embargoed until 16B's sealed comparison gate")
    if sid not in ("0", "0b") and (batch is None or int(sid) > len(batch["candidates"])):
        raise SlotRefused(f"slot {sid} needs a batch declaration with a candidate for it")
    if sid == "0b" and not _may_use_0b(slots):
        raise SlotRefused("slot 0b is not available")


def _check_draw_config(records: Sequence[Mapping[str, Any]], config: str) -> None:
    """The cap and the controls rule for a ``dev_draw`` under ``config`` (``ProtocolError``); shared by the pre-check and the record."""
    configs = {r["config_hash"] for r in records if r["kind"] == "dev_draw"}
    if config not in configs:
        if configs and not any(r["kind"] == "controls" for r in records):
            raise ProtocolError("before the controls are recorded only the incumbent's draws may be recorded")
        if len(configs) >= DEV_DRAW_CONFIGS_MAX:
            raise ProtocolError(f"at most {DEV_DRAW_CONFIGS_MAX} pipeline configurations may have recorded draws")


def _draw_mode(record: Mapping[str, Any]) -> str:
    """A ``dev_draw`` record's mode (``build`` or ``replay``); a record without one is read as its ``replay_of`` says."""
    return record.get("mode") or ("replay" if record.get("replay_of") is not None else "build")


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
        self, *, round_no: int, pool_digest: str, outcome: str, payload_sha256: Optional[str] = None,
        prereg_sha256: Optional[str] = None,
    ) -> None:
        """Record a split attempt: the pool digest and its outcome (an enum token).

        ``payload_sha256`` optionally names the stored payload (``store_payload``)
        the attempt refers to; ``prereg_sha256`` the pre-registration the attempt was made under
        (the first record that carries one registers the epoch's prereg: ``epoch_prereg_sha256``).
        """
        fields: Dict[str, Any] = {
            "round": _count(round_no, "round_no", minimum=1),
            "pool_digest": _hex64(pool_digest, "pool_digest"),
            "outcome": _token(outcome, "outcome"),
        }
        if payload_sha256 is not None:
            fields["payload_sha256"] = _hex64(payload_sha256, "payload_sha256")
        if prereg_sha256 is not None:
            fields["prereg_sha256"] = _hex64(prereg_sha256, "prereg_sha256")
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
        prereg_sha256: Optional[str] = None,
    ) -> None:
        """Record one split's freeze: its file's sha256 and totals.

        The ``sealed`` split also binds the twins record's sha256 (required) and
        the exclusion list's: ``exclusions_sha256=None`` means "the exclusions
        recorded so far", digested INSIDE this transaction (so an exclusion
        cannot slip in between the digest and the freeze record); a given value
        must equal that digest or ``ProtocolError`` is raised. The other splits
        may omit both. A split's frozen file is immutable for the epoch, so a
        second freeze of the SAME split is refused (other splits freeze
        separately). ``prereg_sha256`` (optional) is the sha256 of the pre-registration
        the freeze was made under.
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
        if prereg_sha256 is not None:
            fields["prereg_sha256"] = _hex64(prereg_sha256, "prereg_sha256")
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

    # -- the confirmatory batch declaration (b1a) ---------------------------------------

    def record_batch_declaration(
        self, candidates: Sequence[Mapping[str, str]], *, draws_required: int,
    ) -> str:
        """Declare the confirmatory batch: 1..4 candidates in slot order, each ``{"config_hash", "expansion"}``.

        ``config_hash`` is a distinct 64-hex candidate PIPELINE configuration hash (what 16B's identity check
        compares with the run's canonical pipeline configuration); ``expansion`` is ``"replay"``
        (retrieval-only: the slot replays slot 0's canonical artifact), ``"candidate"`` (a rewrite-changing
        candidate: live rewrite) or ``"abandoned"`` (a configuration whose recorded development draws are fewer than
        ``draws_required``: it counts toward Holm's K, the slot gate refuses to run it, and it carries nothing else).
        A ``candidate`` entry also carries ``rewrite_config_hash`` (64 hex): the hash of
        the rewrite model, prompt and caps (``expansion_artifact.rewrite_identity``) that the slot gate checks the
        run's ``--rewrite-candidate`` and code against; a ``replay`` entry carries none. Candidate i runs in slot i. Needs the sealed freeze and
        the ``controls`` record; appendable once, and only before any of slots 1-4 was claimed
        (``ProtocolError`` otherwise, and always while ``CONFIRMATORY_EMBARGO`` holds). Returns the sha256 of
        the canonical JSON of the list (the mirror carries only that digest).

        ``draws_required`` (the prereg's ``draws_r``; REQUIRED: the coverage rules cannot be skipped by omitting it)
        drives the development-draw coverage rules, checked inside the transaction: every ``replay`` or ``candidate``
        entry's ``config_hash`` has at least that many recorded draws, every ``abandoned`` entry's has at least one and
        fewer than that many, and every recorded configuration other than the incumbent's (the ``controls`` record's
        ``incumbent_config_hash``) is declared, so every recorded candidate counts toward Holm's K (a candidate cannot be
        tried, recorded and left out of the batch); any failure is a ``DrawCoverageRefused``.
        """
        if isinstance(draws_required, bool) or not isinstance(draws_required, int) or draws_required < 1:
            raise ValueError("draws_required is the prereg's draws_r, an integer >= 1")
        if isinstance(candidates, (str, bytes)) or not 1 <= len(candidates) <= K_MAX:
            raise ValueError(f"a batch has 1 to {K_MAX} candidates")
        cands: List[Dict[str, str]] = []
        for entry in candidates:
            if not isinstance(entry, Mapping) or not {"config_hash", "expansion"} <= set(entry):
                raise ValueError("a batch candidate is {config_hash, expansion[, rewrite_config_hash]}")
            if entry["expansion"] not in BATCH_ENTRY_EXPANSIONS:
                raise ValueError(f"a batch candidate's expansion is one of {BATCH_ENTRY_EXPANSIONS}")
            want_keys = {"config_hash", "expansion"} | ({"rewrite_config_hash"} if entry["expansion"] == "candidate" else set())
            if set(entry) != want_keys:  # only a rewrite-changing candidate has a rewrite identity
                raise ValueError("a batch candidate carries rewrite_config_hash exactly when its expansion is candidate")
            cand = {"config_hash": _hex64(entry["config_hash"], "candidate"), "expansion": entry["expansion"]}
            if entry["expansion"] == "candidate":
                cand["rewrite_config_hash"] = _hex64(entry["rewrite_config_hash"], "rewrite_config_hash")
            cands.append(cand)
        if len({c["config_hash"] for c in cands}) != len(cands):
            raise ValueError("batch candidates must be distinct")
        digest = hashlib.sha256(_canonical(cands)).hexdigest()
        with self._txn() as txn:
            if CONFIRMATORY_EMBARGO:
                raise ProtocolError("the confirmatory batch is embargoed until 16B's sealed comparison gate")
            if not any(r["kind"] == "freeze" and r["split"] == SEALED_SPLIT for r in txn.records):
                raise ProtocolError("the sealed split is not frozen")
            if not any(r["kind"] == "controls" for r in txn.records):
                raise ProtocolError("the controls are not recorded")
            if any(r["kind"] == "batch_declaration" for r in txn.records):
                raise ProtocolError("a batch was already declared")
            if any(s in txn.slots for s in ("1", "2", "3", "4")):
                raise ProtocolError("a confirmatory slot was already claimed")
            drawn: Dict[str, int] = {}
            for r in txn.records:
                if r["kind"] == "dev_draw":
                    drawn[r["config_hash"]] = drawn.get(r["config_hash"], 0) + 1
            controls = next(r for r in txn.records if r["kind"] == "controls")
            for c in cands:
                have = drawn.get(c["config_hash"], 0)
                if c["expansion"] == ABANDONED:
                    if not 1 <= have < draws_required:
                        raise DrawCoverageRefused("an abandoned candidate has no recorded draw, or all the draws required")
                elif have < draws_required:
                    raise DrawCoverageRefused("a batch candidate has fewer recorded development draws than required")
            undeclared = set(drawn) - {controls["incumbent_config_hash"]} - {c["config_hash"] for c in cands}
            if undeclared:
                raise DrawCoverageRefused("a configuration with recorded development draws is not in the batch")
            self._check_draw_binding(txn.records, cands, controls["incumbent_config_hash"], draws_required)
            txn.append("batch_declaration", candidates=cands, candidates_sha256=digest)
        return digest

    @staticmethod
    def _check_draw_binding(
        records: Sequence[Mapping[str, Any]], cands: Sequence[Mapping[str, str]], incumbent: str, draws_required: int,
    ) -> None:
        """The counted draws (the first ``draws_required`` per configuration) are the evidence the entry's mode claims (r7).

        * every runnable entry's counted draws were scored by the incumbent's scorer: one ``scorer_identity``, equal to the one
          the incumbent's counted draws carry (an incumbent with no counted draw, or counted draws that disagree among
          themselves, cannot be compared with);
        * a ``replay`` entry's counted draws are all ``replay`` draws and draw ``i`` replays the incumbent's draw ``i``;
        * a ``candidate`` entry's counted draws are all ``build`` draws, their rewrite identity is the entry's
          ``rewrite_config_hash`` and that is not the identity of the incumbent's counted draws (a candidate declared
          rewrite-changing must change the rewrite).

        An ``abandoned`` entry is never run and is not compared.

        Raises:
            DrawBindingRefused: any rule above.
        """
        def counted(config: str) -> List[Mapping[str, Any]]:
            return [r for r in records if r["kind"] == "dev_draw" and r["config_hash"] == config][:draws_required]

        incumbent_draws = counted(incumbent)
        incumbent_scorers = {r.get("scorer_identity") for r in incumbent_draws}
        incumbent_rewrites = {r.get("rewrite_config_hash") for r in incumbent_draws}
        for c in cands:
            if c["expansion"] == ABANDONED:
                continue
            mine = counted(c["config_hash"])
            scorers = {r.get("scorer_identity") for r in mine}
            if len(incumbent_scorers) != 1 or None in incumbent_scorers or scorers != incumbent_scorers:
                raise DrawBindingRefused("a batch candidate's counted draws were not scored by the incumbent's scorer identity")
            if c["expansion"] == "replay":
                if any(_draw_mode(r) != "replay" or r.get("replay_of") != r["draw_index"] for r in mine):
                    raise DrawBindingRefused("a replay entry's counted draws must be replay draws of the incumbent's same-index draws")
            else:
                rewrite = c["rewrite_config_hash"]
                if any(_draw_mode(r) != "build" for r in mine):
                    raise DrawBindingRefused("a candidate entry's counted draws must be build draws")
                if {r.get("rewrite_config_hash") for r in mine} != {rewrite}:
                    raise DrawBindingRefused("a candidate entry's rewrite identity is not the one its counted draws were made under")
                if rewrite in incumbent_rewrites:
                    raise DrawBindingRefused("a candidate entry's rewrite identity must differ from the incumbent's")

    def batch_declaration(self) -> Optional[Dict[str, Any]]:
        """The batch declaration record, or ``None`` before it."""
        found = self.records("batch_declaration")
        return found[0] if found else None

    # -- the epoch's registered prereg, the calibration draw, the controls (b1a r2) ------------

    def epoch_prereg_sha256(self) -> Optional[str]:
        """The ``prereg_sha256`` of the first record (of any kind) that carries one: the epoch's registered prereg."""
        for rec in self.records():
            if rec.get("prereg_sha256") is not None:
                return rec["prereg_sha256"]
        return None

    def record_calibration_draw(self, kind: str, draw_sha256: str) -> None:
        """Record that the calibration draw of ``kind`` (``sealed`` or ``development``) was made: once per kind.

        Stored as ``draw_kind`` (the record's own ``kind`` is its type). Raises ``ProtocolError`` for a second
        record of the same kind; ``draw_sha256`` is the sha256 of the canonical draw JSON.
        """
        name = _token(kind, "kind")
        digest = _hex64(draw_sha256, "draw_sha256")
        with self._txn() as txn:
            if any(r["kind"] == "calibration_draw" and r["draw_kind"] == name for r in txn.records):
                raise ProtocolError(f"the {name} calibration draw was already recorded")
            txn.append("calibration_draw", draw_kind=name, draw_sha256=digest)

    def calibration_draw(self, kind: str) -> Optional[Dict[str, Any]]:
        """The calibration-draw record of ``kind``, or ``None`` before it."""
        name = _token(kind, "kind")
        found = [r for r in self.records("calibration_draw") if r["draw_kind"] == name]
        return found[0] if found else None

    def record_controls(
        self, *, controls_sha256: str, draws_sha256: str, prereg_sha256: str, incumbent_config_hash: str,
    ) -> None:
        """Record the critical-controls list once: the file's sha256, the draw files' digest, the prereg's sha256 and
        the incumbent's pipeline configuration hash (the ``pipeline_config_hash`` every counted draw carried: slots 0
        and 0b must run under exactly this configuration). All four are 64-hex digests and the mirror carries them."""
        fields = {
            "controls_sha256": _hex64(controls_sha256, "controls_sha256"),
            "draws_sha256": _hex64(draws_sha256, "draws_sha256"),
            "prereg_sha256": _hex64(prereg_sha256, "prereg_sha256"),
            "incumbent_config_hash": _hex64(incumbent_config_hash, "incumbent_config_hash"),
        }
        with self._txn() as txn:
            if any(r["kind"] == "controls" for r in txn.records):
                raise ProtocolError("the controls were already recorded")
            txn.append("controls", **fields)

    def controls_record(self) -> Optional[Dict[str, Any]]:
        """The controls record, or ``None`` before it."""
        found = self.records("controls")
        return found[0] if found else None

    def incumbent_config_hash(self) -> Optional[str]:
        """The incumbent's pipeline configuration hash from the ``controls`` record, or ``None`` before it."""
        record = self.controls_record()
        return None if record is None else record.get("incumbent_config_hash")

    def check_dev_draw_config(self, config_hash: str) -> None:
        """Whether a draw under ``config_hash`` may be recorded now (the cap and the controls rule), without recording it.

        ``pipeline eval --record-draw`` runs this before any call, so a draw that the ledger would refuse is never run.

        Raises:
            ProtocolError: a sixth distinct configuration, or a second configuration before the ``controls`` record.
        """
        config = _hex64(config_hash, "config_hash")
        with self._txn() as txn:
            _check_draw_config(txn.records, config)

    def record_dev_draw(
        self, config_hash: str, artifact_sha256: str, rows_sidecar_sha256: str, execution_receipt: str,
        *, scorer_identity: str, rewrite_config_hash: str, replay_of: Optional[int] = None,
    ) -> int:
        """Record a development draw (b7) that may count: its pipeline configuration, expansion artifact, rows sidecar and run.

        The three digests are 64-hex; ``execution_receipt`` is the run id of the run that produced the draw
        (``[A-Za-z0-9_-]{3,64}``). ``scorer_identity`` (the sidecar's: ``src.scoring_identity``) and ``rewrite_config_hash``
        (the sidecar's expansion ``config_hash``: the rewrite model, prompt and caps the draw's expansions came from) are
        64-hex too and are what ``record_batch_declaration`` binds the batch entries to (r7). The record also stores the draw's
        ``mode`` (``replay`` iff ``replay_of`` is given, else ``build``) and ``replay_of`` (the incumbent draw number or null).
        The ledger assigns the draw's per-configuration index (1 for the first record
        under ``config_hash``, 2 for the next...) and returns it. Raises ``ProtocolError`` for:

        * a mode that differs from the mode of the configuration's earlier draws (one configuration is all ``build`` or all
          ``replay``);

        * a configuration beyond the cap: at most ``DEV_DRAW_CONFIGS_MAX`` (5) distinct configuration hashes may ever
          have records (the incumbent's and four candidates');
        * a second configuration before the ``controls`` record exists (until then only the incumbent's may be
          recorded: the controls are computed from the incumbent's draws);
        * a rows sidecar or an execution receipt that is already recorded (a draw is a distinct run and a distinct file; a
          copy is not another draw);
        * ``replay_of`` (a replay candidate's draw: the number of the incumbent's draw it replayed) unless the controls
          record exists, the incumbent has that draw, the artifact digest is that draw's artifact digest, and this
          draw's index is ``replay_of`` (the candidate's i-th draw replays the incumbent's i-th: paired by draw index).
        """
        config = _hex64(config_hash, "config_hash")
        artifact = _hex64(artifact_sha256, "artifact_sha256")
        sidecar = _hex64(rows_sidecar_sha256, "rows_sidecar_sha256")
        receipt = _token(execution_receipt, "execution_receipt", _RUN_ID)
        scorer = _hex64(scorer_identity, "scorer_identity")
        rewrite = _hex64(rewrite_config_hash, "rewrite_config_hash")
        if replay_of is not None and (isinstance(replay_of, bool) or not isinstance(replay_of, int) or replay_of < 1):
            raise ValueError("replay_of is the number (>= 1) of the incumbent's draw that was replayed")
        mode = "build" if replay_of is None else "replay"
        with self._txn() as txn:
            draws = [r for r in txn.records if r["kind"] == "dev_draw"]
            _check_draw_config(txn.records, config)
            if any(r["config_hash"] == config and _draw_mode(r) != mode for r in draws):
                raise ProtocolError("a configuration's draws are all build draws or all replay draws")
            if any(r["rows_sidecar_sha256"] == sidecar for r in draws):
                raise ProtocolError("this draw's rows sidecar is already recorded")
            if any(r.get("execution_receipt") == receipt for r in draws):
                raise ProtocolError("this draw's execution receipt is already recorded")
            index = 1 + sum(1 for r in draws if r["config_hash"] == config)
            if replay_of is not None:
                controls = next((r for r in txn.records if r["kind"] == "controls"), None)
                if controls is None:
                    raise ProtocolError("a replay draw needs the controls record (the incumbent is not known before it)")
                incumbent_draws = [r for r in draws if r["config_hash"] == controls["incumbent_config_hash"]]
                if replay_of > len(incumbent_draws):
                    raise ProtocolError("the incumbent has no such draw to replay")
                if incumbent_draws[replay_of - 1]["artifact_sha256"] != artifact:
                    raise ProtocolError("a replay draw's artifact is not the incumbent draw's artifact")
                if index != replay_of:
                    raise ProtocolError("a replay draw's index must equal the incumbent draw it replays")
            txn.append(
                "dev_draw", config_hash=config, draw_index=index, artifact_sha256=artifact, rows_sidecar_sha256=sidecar,
                execution_receipt=receipt, mode=mode, replay_of=replay_of, rewrite_config_hash=rewrite,
                scorer_identity=scorer,
            )
        return index

    def record_dev_draw_void(self, config_hash: str, run_id: str, reason: str) -> None:
        """Log a development draw that was voided instead of recorded (r7): its run had a rewrite fallback.

        A draw whose expansion fell back to the raw query for any question (a rewrite timeout, say) is not a draw of the
        configuration: its ranks depend on a transport failure. ``pipeline eval --record-draw`` appends this record in place
        of a ``dev_draw``; the run releases nothing, the draw does not count and **consumes no index**, so the configuration's
        next draw takes the number the voided one would have had. The record is digests and an enum only (the mirror carries
        the configuration hash). A run id that is already a recorded draw's receipt, or already voided, is refused.

        Raises:
            ProtocolError: the run id is already recorded as a draw or as a void.
            ValueError: a malformed hash or run id, or a reason that is not in :data:`DRAW_VOID_REASONS`.
        """
        config = _hex64(config_hash, "config_hash")
        rid = _token(run_id, "run_id", _RUN_ID)
        if reason not in DRAW_VOID_REASONS:
            raise ValueError(f"reason must be one of {DRAW_VOID_REASONS}")
        with self._txn() as txn:
            for r in txn.records:
                if (r["kind"] == "dev_draw" and r.get("execution_receipt") == rid) or (
                    r["kind"] == "dev_draw_void" and r.get("run_id") == rid
                ):
                    raise ProtocolError("this run is already recorded as a draw or as a void")
            txn.append("dev_draw_void", config_hash=config, run_id=rid, reason=reason)

    def dev_draw_voids(self, config_hash: str) -> List[Dict[str, Any]]:
        """The ``dev_draw_void`` records of one pipeline configuration, in the order they were logged."""
        config = _hex64(config_hash, "config_hash")
        return [r for r in self.records("dev_draw_void") if r["config_hash"] == config]

    def dev_draws(self, config_hash: str) -> List[Dict[str, Any]]:
        """The ``dev_draw`` records of one pipeline configuration, in the order they were recorded (index 1, 2, ...)."""
        config = _hex64(config_hash, "config_hash")
        return [r for r in self.records("dev_draw") if r["config_hash"] == config]

    def dev_draw_configs(self) -> List[str]:
        """The distinct configuration hashes with recorded draws, in the order they were first recorded."""
        seen: List[str] = []
        for r in self.records("dev_draw"):
            if r["config_hash"] not in seen:
                seen.append(r["config_hash"])
        return seen

    def record_dev_run_unrecorded(self, config_hash: Optional[str], run_id: str, reason: Optional[str] = None) -> None:
        """Log a ``pipeline eval`` run over a frozen development version that was not a ``--record-draw`` run.

        ``config_hash`` is the run's pipeline configuration hash (``None`` when the run had no index to identify) and
        ``run_id`` its private run id. The run never counts; the record makes it visible (``dev_unrecorded_counts``).
        ``reason`` (r7) is ``record_failed`` for a ``--record-draw`` run whose record failed or was refused at or after the
        sidecar write (:data:`UNRECORDED_REASONS`), stored only when given: a run without ``--record-draw`` has none.
        """
        config = None if config_hash is None else _hex64(config_hash, "config_hash")
        rid = _token(run_id, "run_id", _RUN_ID)
        fields: Dict[str, Any] = {}
        if reason is not None:
            if reason not in UNRECORDED_REASONS:
                raise ValueError(f"reason must be one of {UNRECORDED_REASONS}")
            fields["reason"] = reason
        with self._txn() as txn:
            txn.append("dev_run_unrecorded", config_hash=config, run_id=rid, **fields)

    def dev_unrecorded_counts(self) -> Dict[str, int]:
        """``{configuration hash (or "none"): number of unrecorded development runs}`` in the order first logged."""
        counts: Dict[str, int] = {}
        for r in self.records("dev_run_unrecorded"):
            key = r["config_hash"] if r["config_hash"] is not None else "none"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def canonical_artifact_sha256(self) -> Optional[str]:
        """The expansion artifact's sha256 on the stored record of the slot (0 or 0b) that has a publication intent."""
        with self._txn() as txn:
            for sid in ("0", "0b"):
                state = txn.slots.get(sid)
                if state is not None and state.intent is not None and state.stored is not None:
                    return state.stored.get("expansion_artifact_sha256")
        return None

    # -- sealed-processor dispatches (a7) --------------------------------------------

    def record_dispatch(
        self, *, dispatch_id: str, dispatch_kind: str, argv_hash: str, prereg_sha256: Optional[str] = None,
    ) -> None:
        """Record that a sealed-processor run was started (ids, an enum and digests; the time is the record's ``ts``).

        ``prereg_sha256`` (the launcher always gives it) is the sha256 of the pre-registration the dispatch was
        made under; the first record of any kind that carries one registers the epoch's prereg
        (``epoch_prereg_sha256``), so a data dispatch registers it and therefore REQUIRES it (a ``data`` dispatch with
        no prereg would be a dispatch under no registered prereg). Raises ``ProtocolError`` for that, and when the
        epoch's prereg is already registered and is another file's (checked inside the transaction).
        """
        fields: Dict[str, Any] = {
            "dispatch_id": _token(dispatch_id, "dispatch_id", _DISPATCH_ID),
            "dispatch_kind": _token(dispatch_kind, "dispatch_kind"),
            "argv_hash": _hex64(argv_hash, "argv_hash"),
        }
        if fields["dispatch_kind"] == "data" and prereg_sha256 is None:
            raise ProtocolError("a data dispatch is recorded under a prereg")
        if prereg_sha256 is not None:
            fields["prereg_sha256"] = _hex64(prereg_sha256, "prereg_sha256")
        with self._txn() as txn:
            registered = next((r["prereg_sha256"] for r in txn.records if r.get("prereg_sha256") is not None), None)
            if prereg_sha256 is not None and registered is not None and registered != prereg_sha256:
                raise ProtocolError("the prereg is not this epoch's registered prereg")
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

    def exists(self) -> bool:
        """True once a store of this ledger exists (the out-of-tree ledger or its private copy): before the epoch's first
        record there is nothing to read, and a read would create the files."""
        return self._ledger.exists() or self._copy.exists()

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
                _check_claimable(sid, txn.slots, _batch_of(txn.records))
                txn.append("slot_claim", slot=sid)
        return sid

    def check_claimable(self, slot: int | str) -> None:
        """Raise ``SlotRefused`` if ``claim(slot)`` would be refused on the ledger's state; append nothing.

        Advisory (a gate's early answer): the claim itself re-checks under the lock, and for ``"0b"`` it
        also needs slot 0 not to be running, which only the claim can establish.
        """
        sid = _slot_id(slot)
        with self._txn() as txn:
            _check_claimable(sid, txn.slots, _batch_of(txn.records))

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
            extra: Dict[str, Any] = {}
            artifact = Path(run_dir) / EXPANSION_ARTIFACT_NAME
            if sid in ("0", "0b") and artifact.is_file():
                extra["expansion_artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            txn.append("slot_stored", slot=sid, run_id=run_id, results_digest=digest, **extra)
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

    def exclude(
        self, group_id: str, reason: str, members: Optional[Sequence[str]] = None, *, scan_slot: Optional[str] = None,
    ) -> None:
        """Append an exclusion against a whole group (``reason`` is an enum token, e.g. ``s_hit``).

        ``members`` are the group's families at the time (family ids, never text);
        ``None`` means ``[group_id]``, the group's own smallest-member name. The
        gate resolves the families (``excluded_families``) against the frozen
        groups at run time, so a group renamed by a later rejection still resolves.

        The record also stores ``slots_stored`` (r7): the slots whose results were stored when the exclusion was made. The
        confirmatory comparison uses the exclusions recorded before slot 0 was stored, plus each confirmatory slot's own
        ``s_hit`` exclusions applied to both arms (prereg ``adoption.exclusions``), and this field is what tells them apart.

        ``scan_slot`` (r8) is the slot whose report the publication scanner scanned when it found the hit
        (``src.sealed_run.render_and_publish`` passes it; the owner's command never does). It is recorded as ``scan_slot``
        and must be a slot id whose results are stored (the scan runs on a stored document) and the reason must be
        ``s_hit``: an exclusion that names a scanned slot is a scan's, nothing else is.

        Raises:
            ValueError: ``scan_slot`` is not a slot id, or is given with a reason other than ``s_hit``.
            ProtocolError: ``scan_slot`` names a slot whose results are not stored.
        """
        fields: Dict[str, Any] = {"group_id": _token(group_id, "group_id", _ID), "reason": _token(reason, "reason")}
        if scan_slot is not None:
            fields["scan_slot"] = _token(scan_slot, "scan_slot", _SLOT)
            if fields["reason"] != "s_hit":
                raise ValueError("only an s_hit exclusion names the slot whose report was scanned")
        if members is None:
            members = [fields["group_id"]]
        elif isinstance(members, (str, bytes)):
            raise ValueError("members must be a sequence of family ids")
        names = sorted({_token(m, "member", _ID) for m in members})
        if not names:
            raise ValueError("an exclusion names at least one member family")
        fields["members"] = names
        with self._txn() as txn:
            fields["slots_stored"] = sorted(sid for sid, state in txn.slots.items() if state.stored is not None)
            if scan_slot is not None and scan_slot not in fields["slots_stored"]:
                raise ProtocolError("the scanned slot's results are not stored")
            txn.append("exclusion", **fields)

    def stored_slots(self) -> List[str]:
        """The slots whose results are stored so far (a4 step 3), sorted: the slot state an exclusion is made in."""
        with self._txn() as txn:
            return sorted(sid for sid, state in txn.slots.items() if state.stored is not None)

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
        try:
            return excluded_member_families(self.records("exclusion"))
        except ValueError as exc:
            raise LedgerCorrupt(str(exc)) from None

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
