"""Phase 16A-1 privacy primitives (D65): classes, the floor, opaque ids, private destinations.

An eval input is ``public``, ``private`` or ``sealed`` (strictest last). In
16A-1 ``sealed`` is a refusal only: every eval entry point refuses sealed input
before any retrieval, expansion or model call (16A-2 adds trusted sealed
processing).

This module owns the pieces every privacy-aware path shares:

- the class order and :func:`check_floor` (a caller may ask for a STRONGER class
  than the input floor, never a weaker one -> :class:`PrivacyFloorError`);
- opaque question ids (:func:`public_v1_id`, :func:`private_v1_id`), so private
  runs print ids instead of question text;
- :func:`safe_error`, the only rendering of an exception a private run may print
  (opaque id + exception type, never ``str(exc)``);
- the private root (:func:`private_root`, ``eval/private/``) and contained
  destinations under it (:func:`run_dir`, :func:`artifact_path`,
  :func:`contained_path`): symlink and ``..`` escapes are refused.

Nothing here reads eval rows; the registry and classifier live in
``src.eval_sets``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

PUBLIC = "public"
PRIVATE = "private"
SEALED = "sealed"
CLASSES = (PUBLIC, PRIVATE, SEALED)
_RANK = {name: rank for rank, name in enumerate(CLASSES)}

# Run ids name a directory under eval/private/runs/: a strict charset keeps them
# from carrying path separators, dots-only names or question text.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")


class PrivacyFloorError(ValueError):
    """A caller asked for a weaker privacy class than the inputs' floor."""


class SealedInputError(ValueError):
    """Sealed input reached a 16A-1 entry point (refused; CLI exit 4)."""


class PrivatePathError(ValueError):
    """A private destination escapes the private root (symlink or ``..``)."""


def rank(privacy: str) -> int:
    """Strictness rank of a class (public 0 < private 1 < sealed 2).

    Raises:
        ValueError: for anything outside ``CLASSES``.
    """
    if privacy not in _RANK:
        raise ValueError(f"unknown privacy class {privacy!r}; expected one of {CLASSES}")
    return _RANK[privacy]


def strictest(classes: Iterable[str]) -> str:
    """The strictest class in ``classes`` (``public`` for an empty iterable)."""
    best = PUBLIC
    for cls in classes:
        if rank(cls) > rank(best):
            best = cls
    return best


def check_floor(requested: str, floor: str) -> str:
    """Return the effective class: ``requested`` if it is at least ``floor``.

    A stronger request is honoured (a public set may be run as private); a
    weaker one raises before the caller does any work.

    Raises:
        PrivacyFloorError: if ``requested`` is weaker than ``floor``.
        SealedInputError: if the floor is ``sealed`` (16A-1 refuses sealed input).
    """
    if rank(floor) >= rank(SEALED):
        raise SealedInputError("sealed eval input is refused in 16A-1 (exit 4)")
    if rank(requested) < rank(floor):
        raise PrivacyFloorError(
            f"privacy {requested!r} is weaker than the inputs' floor {floor!r}"
        )
    return requested


def require_class(privacy: Any) -> str:
    """Validate a keyword-only ``privacy`` argument (no default exists anywhere)."""
    if not isinstance(privacy, str) or privacy not in _RANK:
        raise ValueError(f"privacy must be one of {CLASSES}, got {privacy!r}")
    if privacy == SEALED:
        raise SealedInputError("sealed eval input is refused in 16A-1 (exit 4)")
    return privacy


# --------------------------------------------------------------------------
# Opaque ids
# --------------------------------------------------------------------------
def public_v1_id(question: str) -> str:
    """Id of a public v1 row: ``"q:" + sha256(question)[:12]`` (D66)."""
    return "q:" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]


def private_v1_id(question: str, set_sha256: str) -> str:
    """Id of a private v1 row, salted by its file's own sha256 (D65).

    ``"q:" + sha256(set_sha256 || question)[:12]``: without the file's hash,
    candidate text cannot confirm membership. (Residual, D65: once the set's
    sha256 is registered in the committed registry, it can.)
    """
    digest = hashlib.sha256((set_sha256 + "\x00" + question).encode("utf-8"))
    return "q:" + digest.hexdigest()[:12]


def safe_error(exc: BaseException, row_id: Optional[str] = None) -> str:
    """Render an exception for a private run: id + exception type, never its text."""
    kind = type(exc).__name__
    return f"{row_id}: {kind}" if row_id else kind


# --------------------------------------------------------------------------
# Private destinations
# --------------------------------------------------------------------------
def private_root() -> Path:
    """The private root, ``eval/private/`` under the repo.

    Tests move it only by monkeypatching this function (a conftest fixture
    points it at ``tmp_path/eval/private``); production code never takes a
    root parameter.
    """
    return REPO_ROOT / "eval" / "private"


def is_under(path: os.PathLike | str, root: os.PathLike | str) -> bool:
    """True iff ``path`` resolves inside ``root`` (both resolved).

    On a case-insensitive filesystem (macOS by default) ``resolve()`` keeps the
    as-typed casing, so ``eval/Private/Sealed/x`` would miss a string prefix
    test. When the plain test fails, every existing ancestor of ``path`` is
    also compared to ``root`` by file identity (``os.path.samefile``: device +
    inode), the same identity rule ``evaluator._same_path`` uses.
    """
    p = Path(path).resolve()
    r = Path(root).resolve()
    try:
        p.relative_to(r)
        return True
    except ValueError:
        pass
    if not r.exists():
        return False
    for ancestor in (p, *p.parents):
        try:
            if ancestor.exists() and os.path.samefile(ancestor, r):
                return True
        except OSError:
            continue
    return False


def contained_path(root: Path, *parts: str) -> Path:
    """Join ``parts`` under ``root`` and refuse any escape.

    Refused: an absolute part, a ``..`` component, an existing symlink at any
    component below ``root``, and a result that does not resolve inside the
    resolved ``root``.

    Raises:
        PrivatePathError: on any escape.
    """
    root_resolved = root.resolve()
    candidate = root
    for part in parts:
        rel = Path(part)
        if rel.is_absolute() or ".." in rel.parts:
            raise PrivatePathError("private destination escapes the private root")
        for piece in rel.parts:
            candidate = candidate / piece
            if candidate.is_symlink():
                raise PrivatePathError("private destination crosses a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PrivatePathError("private destination escapes the private root") from exc
    return candidate


def _check_root(root: Path) -> None:
    """Refuse a private root (or its eval/ parent chain) that is itself a symlink."""
    if root.is_symlink():
        raise PrivatePathError("the private root is a symlink")


def run_dir(run_id: str, *, create: bool = True) -> Path:
    """``eval/private/runs/<run id>/``, contained and (by default) created.

    Raises:
        ValueError: for a run id outside ``[A-Za-z0-9_-]{3,64}``.
        PrivatePathError: on a symlink or ``..`` escape.
    """
    if not _RUN_ID_RE.match(run_id):
        raise ValueError("run id must match [A-Za-z0-9][A-Za-z0-9_-]{2,63}")
    root = private_root()
    _check_root(root)
    path = contained_path(root, "runs", run_id)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        contained_path(root, "runs", run_id)  # re-check after creation
    return path


def artifact_path(name: str) -> Path:
    """A contained file path under ``eval/private/artifacts/`` (parent created)."""
    root = private_root()
    _check_root(root)
    path = contained_path(root, "artifacts", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    contained_path(root, "artifacts", name)
    return path


def write_private(path: Path, content: str) -> None:
    """Atomically write ``content`` to a path that must be inside the private root."""
    root = private_root()
    if not is_under(path, root):
        raise PrivatePathError("private output must stay under the private root")
    rel = Path(path).resolve().relative_to(root.resolve())
    target = contained_path(root, *rel.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, target)


def write_inputs_json(directory: Path, inputs: Sequence[Mapping[str, Any]]) -> Path:
    """Write ``inputs.json`` for a private run or artifact (scanner contract, item 1).

    Each entry: ``path``, ``sha256``, ``kind`` (``questions`` for an eval set,
    ``derived`` for a cache, dump or artifact, which also lists ``sources``: the
    question sets it was built from, each ``{path, sha256}``).

    Raises:
        ValueError: for an entry with an unknown kind or missing fields.
    """
    clean: List[Dict[str, Any]] = []
    for entry in inputs:
        kind = entry.get("kind")
        if kind not in ("questions", "derived"):
            raise ValueError("inputs.json entries need kind 'questions' or 'derived'")
        if not entry.get("path") or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ValueError("inputs.json entries need a path and a sha256")
        item: Dict[str, Any] = {"path": str(entry["path"]), "sha256": entry["sha256"], "kind": kind}
        if kind == "derived":
            item["sources"] = [
                {"path": str(s["path"]), "sha256": s["sha256"]} for s in entry.get("sources", [])
            ]
        clean.append(item)
    out = Path(directory) / "inputs.json"
    write_private(out, json.dumps({"version": 1, "inputs": clean}, indent=1, sort_keys=True) + "\n")
    return out
