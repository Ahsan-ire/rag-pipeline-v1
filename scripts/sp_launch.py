"""Phase 16A-2a sealed-processor launcher (item a7, D72): run the SP in a throwaway directory.

The sealed processor (SP) is a ``claude -p`` run that may read sealed material.
This launcher is the only sanctioned way to start one. It exists to keep the SP
boxed in and to keep everything the SP says out of the orchestrator's view.

Usage::

    python scripts/sp_launch.py --kind probe|data --task <task file> --brief <brief file>
                                [--inputs <path> ...] [--enum <word> ...]
                                [--prereg <sha> --branch <name>]
    python scripts/sp_launch.py --sweep

What a dispatch does, in order
------------------------------
1. Takes an exclusive lock on the staging root, replays any pending move
   journal (so no half-moved output is lost) and **sweeps** every stale entry
   in ``~/.local/state/claudecode/sealed_sp/``. ``--sweep`` does only this and
   runs before any Codex call.
2. Refuses if ``ANTHROPIC_API_KEY`` is set (the SP must use the owner's
   subscription login, never metered API spend).
3. ``--kind data`` only: ``git fetch origin`` and then
   ``git merge-base --is-ancestor <prereg sha> origin/<branch>``; refused unless
   the pre-registration commit is reachable from the pushed branch (later
   commits on top are fine; ``ls-remote`` is not a substitute).
4. Creates ``<root>/<dispatch id>/``, copies in exactly the ``--inputs`` and
   creates ``out/``.
5. Runs ``claude`` with that directory as cwd, under an environment holding only
   ``HOME``, ``PATH``, ``USER``, ``LANG`` and ``TMPDIR``, with the argument
   vector in :func:`build_argv`. The task text goes on **stdin**:
   ``--mcp-config`` is variadic and would swallow a trailing prompt.
6. The SP's stdout and stderr go to files in the staging directory and are
   **never read or printed** here. The launcher's only output is one line: the
   SP's ``out/return.json`` if (and only if) every key and value matches
   :func:`validate_return`, else ``invalid``. A refusal prints one fixed reason
   on stderr (never text from the SP, git or an exception).
7. If the SP exited 0 and the return validated, ``out/`` is moved to
   ``<dest>/<dispatch id>/`` (production ``dest``:
   ``eval/private/sealed/work/sp_out/``, which ``sealed_tool intake`` reads); the
   two logs are always moved to ``<work>/<dispatch id>.stdout.log`` / ``.stderr.log``
   (production ``work``: ``eval/private/sealed/work/``). A failed or invalid run
   releases nothing but its logs. Moves are crash-safe (:func:`journaled_move`).
8. The staging directory is deleted in a ``finally`` (kept only while a move is
   still pending, because deleting it then would lose the data; the next launch
   or ``--sweep`` replays the journal first and deletes afterwards).
9. A line with the dispatch id, kind, UTC time, argv hash and the SP's exit
   status (never any text) is appended to ``eval/private/sp_dispatch.jsonl``
   (``data``) or ``eval/private/sp_probe.jsonl`` (``probe``). A ``data`` dispatch
   also appends a ``dispatch`` record (dispatch id, kind, argv hash; the ledger adds
   the UTC time) to the out-of-tree sealed ledger **before the SP starts**, so a
   dispatch that dies mid-run is still on the record; if the ledger cannot record
   it the dispatch is refused (exit ``8``) and the SP never runs.

Exit codes: ``0`` ok; ``1`` the SP failed, its return was invalid, or cleanup
failed; ``2`` usage or input error; ``8`` refused (API key set, prereg not
reachable, root override outside pytest, another dispatch holds the lock, the sealed ledger
cannot record a ``data`` dispatch).

The staging root can be moved **only under pytest** (``CC_SP_STAGING_ROOT``,
same rules as ``CC_SPEND_LEDGER`` in :mod:`src.spend`).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Dict, Iterator, List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Imported as a module (not ``from ... import private_root``) so a test that
# monkeypatches ``src.eval_privacy.private_root`` is honoured at call time.
import src.eval_privacy as eval_privacy  # noqa: E402
from src.sealed_ledger import SealedLedger, SealedLedgerError  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 8

KINDS = ("probe", "data")
ROOT_ENV = "CC_SP_STAGING_ROOT"
JOURNAL_NAME = "moves.journal"  # not .jsonl: the scanner treats private *.jsonl as needle sources
LOCK_NAME = ".lock"
RESERVED_ROOT_NAMES = frozenset({JOURNAL_NAME, LOCK_NAME})
OUT_DIR = "out"
RETURN_NAME = "return.json"
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
RESERVED_STAGING_NAMES = frozenset({OUT_DIR, STDOUT_LOG, STDERR_LOG})
ENV_KEYS = ("HOME", "PATH", "USER", "LANG", "TMPDIR")
INVALID = "invalid"

# The exact argument vector (a7). The task is NOT in it: it goes on stdin.
CLAUDE_ARGV_PREFIX: Tuple[str, ...] = (
    "claude",
    "-p",
    "--restricted",
    "--tools", "Read,Write,Edit,Glob,Grep",
    "--permission-mode", "acceptEdits",
    "--permission-prompts", "none",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--no-session-persistence",
    "--model", "opus",
    "--append-system-prompt",
)

# --- return-file allowlist -----------------------------------------------------
# One pattern set for every opaque id a return file may carry; ``sealed_tool``'s ``return`` command and its
# printer reuse these names (``sp.FAMILY_ID_RE`` ...), so there is no second copy to drift.
ID_RE = re.compile(r"[0-9a-f]{8}")  # a dispatch id
FAMILY_ID_RE = re.compile(r"f[0-9a-f]{8}")  # a private family id
ROW_ID_RE = re.compile(r"f[0-9a-f]{8}-[0-9]{1,4}")  # a row id: the family id and the phrasing number
ID_PATTERNS = (ID_RE, FAMILY_ID_RE, ROW_ID_RE)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ENUM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
MAX_INT = 10**9  # a count, not a payload: bounds what an integer can smuggle
MAX_RETURN_BYTES = 64 * 1024
MAX_DEPTH = 4
MAX_NODES = 4096

SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class LaunchError(Exception):
    """A launcher stop. ``reason`` is a fixed string, never SP, git or exception text."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class RootRefused(Exception):
    """The staging-root override was used where it is not allowed."""


class MoveError(Exception):
    """A journaled move could not be completed or verified (the source is kept)."""


@dataclass(frozen=True)
class Outcome:
    """What a launch returns: the exit status, the one stdout line, a stderr reason."""

    exit_status: int
    line: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Staging root (pytest-only override, as src/spend.py's CC_SPEND_LEDGER)
# ---------------------------------------------------------------------------
def default_staging_root() -> Path:
    """``~/.local/state/claudecode/sealed_sp``, keyed on the passwd entry, not ``$HOME``."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "claudecode" / "sealed_sp"


def _under_pytest() -> bool:
    """True only inside a pytest process (``PYTEST_CURRENT_TEST`` alone can be exported by a shell)."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and "pytest" in sys.modules


def _same_path(a: Path, b: Path) -> bool:
    return os.path.abspath(os.path.realpath(a)) == os.path.abspath(os.path.realpath(b))


def resolve_staging_root() -> Path:
    """The staging root to use.

    Outside pytest: ``CC_SP_STAGING_ROOT`` set -> :class:`RootRefused`, else the
    default. Under pytest: the variable must be set and must not be the default
    (so a test can never touch the owner's real staging area).
    """
    override = os.environ.get(ROOT_ENV)
    if not _under_pytest():
        if override:
            raise RootRefused(f"{ROOT_ENV} is honoured only under pytest")
        return default_staging_root()
    if not override:
        raise RootRefused(f"under pytest the default staging root is refused and {ROOT_ENV} is not set")
    path = Path(override)
    if _same_path(path, default_staging_root()):
        raise RootRefused(f"under pytest {ROOT_ENV} must not point at the default staging root")
    return path


# ---------------------------------------------------------------------------
# Return-file validation (reusable by sealed_tool's ``return``)
# ---------------------------------------------------------------------------
def _check_enums(enums: Collection[str]) -> frozenset:
    """The caller's enum words, each of a safe shape (``ValueError`` otherwise)."""
    words = frozenset(enums)
    for word in words:
        if not isinstance(word, str) or not ENUM_RE.fullmatch(word):
            raise ValueError("enum words must match [A-Za-z][A-Za-z0-9_-]{0,31}")
    return words


def _allowed_word(text: str, enums: frozenset) -> bool:
    """An opaque id (dispatch, family or row), a sha256, or one of the caller's enum words.

    ``fullmatch`` throughout: a trailing newline never passes.
    """
    return any(pattern.fullmatch(text) for pattern in ID_PATTERNS) or bool(SHA256_RE.fullmatch(text)) or text in enums


def _valid_node(node: Any, enums: frozenset, depth: int, budget: List[int]) -> bool:
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        return False
    if isinstance(node, bool):  # bool is an int subclass: reject it explicitly
        return False
    if isinstance(node, int):
        return 0 <= node <= MAX_INT
    if isinstance(node, str):
        return _allowed_word(node, enums)
    if isinstance(node, list):
        return all(_valid_node(item, enums, depth + 1, budget) for item in node)
    if isinstance(node, dict):
        return all(
            isinstance(key, str) and _allowed_word(key, enums) and _valid_node(value, enums, depth + 1, budget)
            for key, value in node.items()
        )
    return False  # None, floats, anything else


def validate_return(data: Any, enums: Collection[str] = ()) -> bool:
    """True iff ``data`` is an object whose every key and value is on the allowlist.

    Allowed: opaque ids (a dispatch id ``[0-9a-f]{8}``, a private family id
    ``f[0-9a-f]{8}`` or a row id ``f[0-9a-f]{8}-<n>``), sha256 hex, non-negative integers
    (JSON ints up to ``MAX_INT``, not bools, not digit strings) and the words in
    ``enums``; keys are strings of the id, sha256 or enum kinds. Lists and
    objects may nest to ``MAX_DEPTH``. Free text, floats and null never pass.
    """
    allowed = _check_enums(enums)
    return isinstance(data, dict) and _valid_node(data, allowed, 1, [MAX_NODES])


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def load_return(path: os.PathLike | str, enums: Collection[str] = ()) -> Optional[str]:
    """The single validated line for an SP return file, or ``None`` if it is invalid.

    The line is the file's JSON in canonical form (sorted keys, compact), safe to
    print because every key and value passed :func:`validate_return`. A missing
    file, a symlink, a non-regular file, one over ``MAX_RETURN_BYTES``, bad UTF-8
    or JSON, a duplicate key or any non-allowlisted content is ``None``.
    """
    allowed = _check_enums(enums)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RETURN_BYTES:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(MAX_RETURN_BYTES + 1)
        if len(raw) > MAX_RETURN_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError):  # ValueError covers JSON and Unicode errors
        return None
    if not (isinstance(data, dict) and _valid_node(data, allowed, 1, [MAX_NODES])):
        return None
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# Durable file helpers and the move journal
# ---------------------------------------------------------------------------
def _fsync_fd(fd: int) -> None:
    """Flush to stable storage (macOS ``F_FULLFSYNC`` when available, else ``fsync``)."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_dir(path: os.PathLike | str) -> None:
    """fsync a directory so a rename or create inside it survives a crash."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _trim_torn_tail(fd: int) -> None:
    """Cut an unterminated last line (a crash mid-append) so the next append starts on a fresh line."""
    size = os.fstat(fd).st_size
    if size and os.pread(fd, 1, size - 1) != b"\n":
        os.ftruncate(fd, os.pread(fd, size, 0).rfind(b"\n") + 1)


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append one JSON line and fsync it (and the directory entry if the file is new)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _trim_torn_tail(fd)
        os.write(fd, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        _fsync_fd(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(path: Path) -> List[List[Any]]:
    """Sorted ``[relpath, kind, sha256, size]`` rows for a file or directory tree.

    Symlinks and special files are refused: a move must copy exactly what it
    records. ``"."`` is the root itself.
    """
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode):
        return [[".", "file", _sha256_file(path), st.st_size]]
    if not stat.S_ISDIR(st.st_mode):
        raise MoveError("only regular files and directories can be moved")
    rows: List[List[Any]] = [[".", "dir", "", 0]]
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = Path(dirpath, name)
            rel = os.path.relpath(full, path)
            mode = os.lstat(full).st_mode
            if stat.S_ISDIR(mode):
                rows.append([rel, "dir", "", 0])
            elif stat.S_ISREG(mode):
                rows.append([rel, "file", _sha256_file(full), os.lstat(full).st_size])
            else:
                raise MoveError("only regular files and directories can be moved")
    return sorted(rows)


def _copy_durable(src: Path, dst: Path) -> None:
    """Copy a file or tree to a NEW path, fsyncing every file and directory written."""
    if src.is_dir():
        dst.mkdir(mode=0o700)
        for entry in sorted(src.iterdir()):
            _copy_durable(entry, dst / entry.name)
        _fsync_dir(dst)
        return
    out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(src, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                os.write(out, block)
        _fsync_fd(out)
    finally:
        os.close(out)


def _remove_path(path: Path) -> None:
    """Delete a file, symlink or tree (a symlink is unlinked, never followed)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _delete_src(src: Path) -> None:
    """Delete a moved source and make the deletion durable."""
    _remove_path(src)
    _fsync_dir(src.parent)


def _crash_point(name: str) -> None:
    """No-op seam called between move steps (``after_begin``, ``after_copy``, ``after_rename``,
    ``after_delete``); tests replace it with a function that raises, to inject a crash there."""


def _read_journal(journal: Path) -> List[Dict[str, Any]]:
    """Parse the journal. A torn final line (a crash mid-append) is ignored; any other bad line raises."""
    try:
        raw = journal.read_bytes()
    except FileNotFoundError:
        return []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # file ended with a newline: every line is complete
    elif lines:
        lines.pop()  # unterminated tail: torn write, never acted on
    records = []
    for line in lines:
        try:
            records.append(json.loads(line.decode("utf-8")))
        except ValueError as exc:
            raise MoveError("move journal is corrupt") from exc
    return records


def pending_moves(journal: Path) -> List[Dict[str, Any]]:
    """Journal ``begin`` records that have no ``done`` yet."""
    records = _read_journal(journal)
    done = {r.get("tid") for r in records if r.get("t") == "done"}
    return [r for r in records if r.get("t") == "begin" and r.get("tid") not in done]


def _complete_item(tid: str, item: Dict[str, Any]) -> None:
    """Bring one journaled item to 'copied, verified, source deleted'. Idempotent.

    * destination present: it must equal the journaled manifest, else stop;
    * else the source must still exist: copy to ``<dst>.<tid>.partial``, fsync,
      verify against the manifest, atomically rename into place;
    * then delete the source if it is still there.
    """
    src, dst, manifest = Path(item["src"]), Path(item["dst"]), item["manifest"]
    if os.path.lexists(dst):
        if _manifest(dst) != manifest:
            raise MoveError("destination exists and differs from the journaled manifest")
    else:
        if not os.path.lexists(src):
            raise MoveError("source and destination are both missing")
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = dst.with_name(f"{dst.name}.{tid}.partial")
        if os.path.lexists(partial):
            _remove_path(partial)
        _copy_durable(src, partial)
        _fsync_dir(dst.parent)
        _crash_point("after_copy")
        if _manifest(partial) != manifest:
            _remove_path(partial)
            raise MoveError("copy did not verify against the journaled manifest; source kept")
        os.rename(partial, dst)
        _fsync_dir(dst.parent)
        _crash_point("after_rename")
    if os.path.lexists(src):
        _delete_src(src)
    _crash_point("after_delete")


def journaled_move(pairs: Sequence[Tuple[os.PathLike | str, os.PathLike | str]], journal: os.PathLike | str) -> None:
    """Move files or trees crash-safely: journal intent, copy, fsync, verify, delete.

    All pairs are one transaction. The ``begin`` record (with each source's
    sha256 manifest) is fsynced before anything is copied; a destination appears
    only by an atomic rename of a verified copy; the source is deleted last; a
    ``done`` record closes the transaction. A crash anywhere leaves the journal
    pending, and :func:`recover_moves` finishes the job from the manifest. A
    destination that already exists is refused.
    """
    journal = Path(journal)
    items = []
    for src, dst in pairs:
        src, dst = Path(src), Path(dst)
        if os.path.lexists(dst):
            raise MoveError("destination already exists")
        if not os.path.lexists(src):
            raise MoveError("source is missing")
        items.append({"src": str(src), "dst": str(dst), "manifest": _manifest(src)})
    tid = secrets.token_hex(4)
    _append_jsonl(journal, {"v": 1, "t": "begin", "tid": tid, "items": items})
    _crash_point("after_begin")
    for item in items:
        _complete_item(tid, item)
    _append_jsonl(journal, {"v": 1, "t": "done", "tid": tid})


def recover_moves(journal: os.PathLike | str) -> int:
    """Replay every pending transaction in ``journal``; return how many were finished.

    Raises :class:`MoveError` (leaving the journal pending) if one cannot be
    completed, e.g. the source and the destination are both gone.
    """
    journal = Path(journal)
    finished = 0
    for record in pending_moves(journal):
        for item in record["items"]:
            _complete_item(record["tid"], item)
        _append_jsonl(journal, {"v": 1, "t": "done", "tid": record["tid"]})
        finished += 1
    return finished


# ---------------------------------------------------------------------------
# Staging: lock, sweep, inputs
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    """Hold a non-blocking exclusive lock on the root, so dispatches and sweeps never overlap."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaunchError(EXIT_REFUSED, "another dispatch or sweep holds the staging lock") from None
        yield
    finally:
        os.close(fd)  # closing releases the flock


def sweep_stale(root: Path) -> int:
    """Delete every stale entry under ``root`` (caller holds the lock); return the count.

    Pending moves are replayed FIRST, so a source awaiting deletion is only
    removed after its destination is complete. If recovery fails, nothing is
    deleted (:class:`MoveError` propagates).
    """
    if not root.is_dir():
        return 0
    recover_moves(root / JOURNAL_NAME)
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.name in RESERVED_ROOT_NAMES:
            continue
        _remove_path(entry)
        removed += 1
    return removed


def _new_staging(root: Path) -> Tuple[str, Path]:
    """Create ``<root>/<dispatch id>/`` (id = 8 random hex chars) and return both."""
    for _ in range(16):
        dispatch_id = secrets.token_hex(4)
        staging = root / dispatch_id
        try:
            staging.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return dispatch_id, staging
    raise LaunchError(EXIT_FAILED, "could not allocate a dispatch id")


def _validate_inputs(inputs: Sequence[os.PathLike | str]) -> List[Path]:
    """Existing, non-symlink files or trees with distinct, non-reserved basenames."""
    paths: List[Path] = []
    names = set(RESERVED_STAGING_NAMES)
    for raw in inputs:
        path = Path(raw)
        if path.is_symlink() or not path.exists():
            raise LaunchError(EXIT_USAGE, "an --inputs path is missing or a symlink")
        if path.name in ("", ".", "..") or path.name in names:
            raise LaunchError(EXIT_USAGE, "an --inputs name is reserved or repeated")
        try:
            _manifest(path)  # refuses symlinks and special files anywhere in the tree
        except MoveError:
            raise LaunchError(EXIT_USAGE, "an --inputs tree holds a symlink or special file") from None
        names.add(path.name)
        paths.append(path)
    return paths


def _read_text(path: os.PathLike | str, what: str) -> str:
    try:
        text = Path(path).read_bytes().decode("utf-8")  # bytes, not read_text: no newline translation
    except (OSError, UnicodeDecodeError):
        raise LaunchError(EXIT_USAGE, f"cannot read the {what} file") from None
    if not text.strip() or "\0" in text:
        raise LaunchError(EXIT_USAGE, f"the {what} file is empty or holds a NUL")
    return text


# ---------------------------------------------------------------------------
# Refusals and the claude invocation
# ---------------------------------------------------------------------------
def check_prereg(sha: str, branch: str, *, repo: os.PathLike | str) -> None:
    """Require ``git fetch`` then ``merge-base --is-ancestor <sha> origin/<branch>``.

    Raises :class:`LaunchError` (refused) unless the commit is an ancestor of
    (or equal to) the fetched branch tip. A prereg with later commits on top
    passes. ``ls-remote`` would only prove a ref exists, not that the commit is
    in its history.
    """
    if not SHA_RE.fullmatch(sha or ""):
        raise LaunchError(EXIT_USAGE, "--prereg must be a full lowercase commit sha")
    if not BRANCH_RE.fullmatch(branch or "") or ".." in branch:
        raise LaunchError(EXIT_USAGE, "--branch is not a plain branch name")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
    try:
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"], check=False, **quiet)
        reachable = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, f"origin/{branch}"], check=False, **quiet
        )
    except OSError:
        raise LaunchError(EXIT_REFUSED, "git could not be run; the prereg cannot be checked") from None
    if fetched.returncode != 0:
        raise LaunchError(EXIT_REFUSED, "git fetch failed; the prereg cannot be checked")
    if reachable.returncode != 0:
        raise LaunchError(EXIT_REFUSED, "the prereg commit is not reachable from origin/<branch>")


def build_argv(brief_text: str) -> List[str]:
    """The exact ``claude`` argument vector, with the brief as the system-prompt text."""
    return [*CLAUDE_ARGV_PREFIX, brief_text]


def argv_hash(argv: Sequence[str]) -> str:
    """sha256 of the argument vector (argv[0] normalised to ``claude``): identifies a run, shows no text."""
    canonical = json.dumps(["claude", *argv[1:]], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scrubbed_env() -> Dict[str, str]:
    """Only ``HOME``, ``PATH``, ``USER``, ``LANG`` and ``TMPDIR`` (those set here), nothing else."""
    return {key: os.environ[key] for key in ENV_KEYS if key in os.environ}


def _run_sp(argv: Sequence[str], env: Dict[str, str], task: bytes, staging: Path) -> int:
    """Run claude in ``staging``; its stdout/stderr go to files there and are not read. -1 if it cannot start."""
    exe = shutil.which("claude", path=env.get("PATH", ""))
    if exe is None:
        return -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    out_fd = os.open(staging / STDOUT_LOG, flags, 0o600)
    err_fd = os.open(staging / STDERR_LOG, flags, 0o600)
    try:
        proc = subprocess.run(
            [exe, *argv[1:]], input=task, stdout=out_fd, stderr=err_fd, cwd=staging, env=env, check=False
        )
        return proc.returncode
    except OSError:
        return -1
    finally:
        os.close(out_fd)
        os.close(err_fd)


def _cleanup_staging(staging: Path, journal: Path) -> bool:
    """Delete the staging directory unless a move is still pending. True when it is gone."""
    try:
        if pending_moves(journal):
            return False  # data may live only here until recovery finishes
        _remove_path(staging)
        _fsync_dir(staging.parent)
    except (OSError, MoveError):
        return False
    return not os.path.lexists(staging)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_dispatch(ledger: SealedLedger, dispatch_id: str, kind: str, argv: Sequence[str]) -> None:
    """Append the ``dispatch`` record to the out-of-tree ledger; a ledger failure refuses the dispatch (no SP runs)."""
    try:
        ledger.record_dispatch(dispatch_id=dispatch_id, dispatch_kind=kind, argv_hash=argv_hash(argv))
    except (SealedLedgerError, OSError, ValueError):
        raise LaunchError(EXIT_REFUSED, "the sealed ledger could not record the dispatch") from None


def _dispatch(
    *, kind: str, root: Path, dest: Path, work: Path, log_path: Path, brief_text: str, task: bytes,
    inputs: Sequence[Path], enums: Collection[str], ledger: Optional[SealedLedger] = None,
) -> Outcome:
    dispatch_id, staging = _new_staging(root)
    journal = root / JOURNAL_NAME
    result = Outcome(EXIT_FAILED, INVALID, "sp_launch: dispatch failed")
    try:
        for src in inputs:
            _copy_durable(src, staging / src.name)
        out_dir = staging / OUT_DIR
        out_dir.mkdir(mode=0o700)
        argv = build_argv(brief_text)
        if ledger is not None:  # a data dispatch: on the record before the SP can run
            _record_dispatch(ledger, dispatch_id, kind, argv)
        returncode = _run_sp(argv, scrubbed_env(), task, staging)
        _append_jsonl(log_path, {
            "argv_hash": argv_hash(argv), "dispatch_id": dispatch_id, "exit_status": returncode,
            "kind": kind, "ts": _utc_now(),
        })
        line = load_return(out_dir / RETURN_NAME, enums)
        released = returncode == 0 and line is not None
        pairs: List[Tuple[Path, Path]] = [
            (staging / STDOUT_LOG, work / f"{dispatch_id}.stdout.log"),
            (staging / STDERR_LOG, work / f"{dispatch_id}.stderr.log"),
        ]
        if released:
            pairs.insert(0, (out_dir, dest / dispatch_id))
        pairs = [(s, d) for s, d in pairs if os.path.lexists(s)]
        if pairs:
            journaled_move(pairs, journal)
        result = Outcome(EXIT_OK if released else EXIT_FAILED, line if line is not None else INVALID)
    finally:
        cleaned = _cleanup_staging(staging, journal)
    if not cleaned:
        result = Outcome(EXIT_FAILED, result.line, "sp_launch: staging directory kept; run --sweep")
    return result


def launch(
    *,
    kind: str,
    task: os.PathLike | str,
    brief: os.PathLike | str,
    inputs: Sequence[os.PathLike | str] = (),
    prereg: Optional[str] = None,
    branch: Optional[str] = None,
    enums: Collection[str] = (),
    repo: Optional[os.PathLike | str] = None,
    dest: Optional[os.PathLike | str] = None,
    work: Optional[os.PathLike | str] = None,
    dispatch_log: Optional[os.PathLike | str] = None,
    ledger: Optional[SealedLedger] = None,
) -> Outcome:
    """Run one SP dispatch (see the module docstring); never raises for an expected stop.

    ``work`` defaults to ``<private root>/sealed/work`` (the two logs), ``dest`` to
    ``<work>/sp_out`` (the released ``out/`` directories, where ``sealed_tool intake``
    reads them), ``dispatch_log`` to ``<private root>/sp_dispatch.jsonl`` (``data``)
    or ``sp_probe.jsonl`` (``probe``), ``repo`` to this checkout. ``prereg`` and
    ``branch`` are required for ``data`` and ignored for ``probe`` (its prereg is
    synthetic). ``ledger`` is the sealed ledger a ``data`` dispatch records itself in;
    ``None`` builds the epoch's ledger exactly as ``src.sealed_run.default_ledger`` does
    (private copy under ``<private root>/sealed/ledger``, mirror ``<eval dir>/sealed_ledger.jsonl``).
    """
    try:
        if kind not in KINDS:
            raise LaunchError(EXIT_USAGE, "--kind must be probe or data")
        try:
            allowed = _check_enums(enums)
        except ValueError:
            raise LaunchError(EXIT_USAGE, "an --enum word has an unsafe shape") from None
        brief_text = _read_text(brief, "brief")
        task_bytes = _read_text(task, "task").encode("utf-8")
        staged = _validate_inputs(inputs)
        if kind == "data" and not (prereg and branch):
            raise LaunchError(EXIT_USAGE, "--kind data needs --prereg and --branch")
        try:
            root = resolve_staging_root()
        except RootRefused:
            raise LaunchError(EXIT_REFUSED, "the staging root override is refused here") from None
        private = eval_privacy.private_root()
        work_dir = Path(work) if work is not None else private / "sealed" / "work"
        dest_dir = Path(dest) if dest is not None else work_dir / "sp_out"
        log_path = Path(dispatch_log) if dispatch_log is not None else private / (
            "sp_dispatch.jsonl" if kind == "data" else "sp_probe.jsonl"
        )
        dispatch_ledger: Optional[SealedLedger] = None
        if kind == "data":
            try:
                dispatch_ledger = ledger if ledger is not None else SealedLedger(
                    private / "sealed" / "ledger", mirror=private.parent / "sealed_ledger.jsonl"
                )
            except (SealedLedgerError, OSError, ValueError):
                raise LaunchError(EXIT_REFUSED, "the sealed ledger could not be opened") from None
        with _exclusive_lock(root):
            try:
                sweep_stale(root)
            except (MoveError, OSError):
                raise LaunchError(EXIT_FAILED, "sweep failed; a pending move was not recovered") from None
            if "ANTHROPIC_API_KEY" in os.environ:
                raise LaunchError(EXIT_REFUSED, "ANTHROPIC_API_KEY is set; the SP must use the subscription login")
            if kind == "data":
                check_prereg(prereg, branch, repo=repo if repo is not None else REPO)
            if shutil.which("claude", path=os.environ.get("PATH", "")) is None:
                raise LaunchError(EXIT_USAGE, "claude was not found on PATH")
            return _dispatch(
                kind=kind, root=root, dest=dest_dir, work=work_dir, log_path=log_path, brief_text=brief_text,
                task=task_bytes, inputs=staged, enums=allowed, ledger=dispatch_ledger,
            )
    except LaunchError as exc:
        return Outcome(exc.status, None, f"sp_launch: {exc.reason}")
    except Exception as exc:  # type name only: never str(exc), which could carry SP or path text
        return Outcome(EXIT_FAILED, None, f"sp_launch: failed ({type(exc).__name__})")


def sweep_only() -> Outcome:
    """``--sweep``: replay pending moves and delete stale staging entries."""
    try:
        root = resolve_staging_root()
    except RootRefused:
        return Outcome(EXIT_REFUSED, None, "sp_launch: the staging root override is refused here")
    if not root.is_dir():
        return Outcome(EXIT_OK, "swept 0")
    try:
        with _exclusive_lock(root):
            return Outcome(EXIT_OK, f"swept {sweep_stale(root)}")
    except LaunchError as exc:
        return Outcome(exc.status, None, f"sp_launch: {exc.reason}")
    except (MoveError, OSError):
        return Outcome(EXIT_FAILED, None, "sp_launch: sweep failed; a pending move was not recovered")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Launch the sealed processor in a throwaway directory; prints one validated line, never SP text."
    )
    ap.add_argument("--kind", choices=KINDS, help="probe (synthetic prereg) or data (prereg must be pushed)")
    ap.add_argument("--task", help="task file; its text goes to the SP on stdin")
    ap.add_argument("--brief", help="brief file; its text is the appended system prompt")
    ap.add_argument("--inputs", nargs="*", default=[], metavar="PATH", help="files or trees copied into staging")
    ap.add_argument("--enum", action="append", default=[], dest="enums", metavar="WORD",
                    help="a word the SP's return file may use as a key or value (repeatable)")
    ap.add_argument("--prereg", help="pre-registration commit sha (data; ignored for probe)")
    ap.add_argument("--branch", help="branch the prereg must be pushed to (data; ignored for probe)")
    ap.add_argument("--sweep", action="store_true", help="replay pending moves and delete stale staging, then exit")
    return ap


def _emit(outcome: Outcome) -> int:
    if outcome.message:
        print(outcome.message, file=sys.stderr)
    if outcome.line is not None:
        print(outcome.line)
    return outcome.exit_status


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns the exit code (see the module docstring)."""
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.sweep:
        if any((args.kind, args.task, args.brief, args.inputs, args.enums, args.prereg, args.branch)):
            print("sp_launch: --sweep takes no other option", file=sys.stderr)
            return EXIT_USAGE
        return _emit(sweep_only())
    if not (args.kind and args.task and args.brief):
        print("sp_launch: --kind, --task and --brief are required (or use --sweep)", file=sys.stderr)
        return EXIT_USAGE
    return _emit(launch(
        kind=args.kind, task=args.task, brief=args.brief, inputs=args.inputs, prereg=args.prereg,
        branch=args.branch, enums=args.enums,
    ))


if __name__ == "__main__":
    sys.exit(main())
