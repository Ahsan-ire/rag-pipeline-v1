"""Phase 16A-1 eval-set registry and input classifier (D65, item 1).

**Registry** (``eval/sets.json``): every eval set the instrument may treat as
public is listed with ``name``, ``path`` (repo-relative), ``privacy``
(``public``/``private``), ``role`` (``tuning``/``realistic``/``regression``/
``fixture``), ``status`` and ``sha256``. Sealed or sealed-marked files cannot
register. Tests register tmp sets by monkeypatching :func:`load_registry`
(see ``tests/conftest.py``); production code never takes a registry argument.

**classify(path)** -- resolved path, first match wins:

1. under ``eval/private/sealed/`` -> ``sealed``;
2. a ``.json``/``.jsonl`` eval input with any parsed row (or a ``.json`` file's
   top-level object) holding ``"sealed": true``, or any unparseable line
   containing ``"sealed"`` (fail closed) -> ``sealed``, wherever it lives, so
   copies, renames, reorders, subsets and reserialisations stay sealed;
3. under ``eval/private/`` -> ``private``;
4. a registered public path at its registered sha256 -> ``public``;
5. a report, cache or artifact whose recorded input sha256s are all registered
   public -> ``public`` (JSON: a top-level ``"inputs": [{"sha256": ...}, ...]``
   list; Markdown: the v5 report's ``  - sha256: <hex>`` set lines);
6. with ``legacy_public=True`` (it enables this lookup, never sets a class), a
   sha256 in ``eval/legacy_public.json`` -> ``public``;
7. else ``private``.

Only eval inputs are ever classified. The strictest class over every path a
runner opens is that runner's floor (:func:`floor`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src import eval_privacy as _privacy
from src.eval_privacy import (
    PRIVATE,
    PUBLIC,
    SEALED,
    REPO_ROOT,
    SealedInputError,
    is_under,
    strictest,
)

REGISTRY_PATH = REPO_ROOT / "eval" / "sets.json"
LEGACY_PUBLIC_PATH = REPO_ROOT / "eval" / "legacy_public.json"

ROLES = ("tuning", "realistic", "regression", "fixture")
STATUSES = ("active", "frozen", "retired")
_REGISTRY_KEYS = {"name", "path", "privacy", "role", "status", "sha256"}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MD_SET_SHA_RE = re.compile(r"^\s+- sha256: ([0-9a-f]{64})\s*$")


class RegistryError(ValueError):
    """The registry (or the legacy list) is malformed or registers a sealed file."""


@dataclass(frozen=True)
class SetEntry:
    """One registered eval set."""

    name: str
    path: str
    privacy: str
    role: str
    status: str
    sha256: str

    def resolved(self) -> Path:
        """The entry's path resolved against the repo root."""
        return (REPO_ROOT / self.path).resolve()


def sha256_file(path: Any) -> str:
    """Hex sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Sealed markers
# --------------------------------------------------------------------------
def _is_sealed_row(obj: Any) -> bool:
    """A parsed row/object carries the marker ``"sealed": true``."""
    return isinstance(obj, dict) and obj.get("sealed") is True


_NON_EVAL_SUFFIXES = (".md", ".py")


def _loads_flagging_sealed(text: str) -> Tuple[Any, bool]:
    """``json.loads`` that also reports any ``"sealed": true`` pair at any depth.

    Catches a duplicate-key bypass (``{"sealed": true, "sealed": false}``
    parses last-key-wins as not sealed) by inspecting every pair as parsed.
    """
    flagged = False

    def _hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        nonlocal flagged
        if any(k == "sealed" and v is True for k, v in pairs):
            flagged = True
        return dict(pairs)

    obj = json.loads(text, object_pairs_hook=_hook)
    return obj, flagged


def has_sealed_marker(path: Any) -> bool:
    """Rule 2: an eval input carrying a sealed marker (fail closed).

    ``.json``: the top-level object (or any element of a top-level list) holds
    ``"sealed": true`` -- any ``"sealed": true`` pair, duplicate keys
    included -- or the file does not parse and contains ``"sealed"``. Every
    other suffix except ``.md``/``.py`` (``.jsonl``, ``.txt``, ``.ndjson``,
    none, ...) is scanned line by line as JSONL, because ``load_golden_set``
    accepts any suffix: a parsed line with a ``"sealed": true`` pair, or a line
    that does not parse but contains ``"sealed"``. Lines split on ``\n`` only
    (a raw U+2028 inside a JSON string is not a line break). Marker text in
    ``.md``/``.py`` files is not a marker. An unreadable file fails closed
    (sealed); a missing file has no marker.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in _NON_EVAL_SUFFIXES or not p.exists() or p.is_dir():
        return False
    try:
        raw = p.read_bytes()
    except OSError:
        return True
    text = raw.decode("utf-8", errors="replace")
    if suffix != ".json":
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if not line.strip():
                continue
            try:
                obj, flagged = _loads_flagging_sealed(line)
            except (ValueError, RecursionError):
                if '"sealed"' in line:
                    return True
                continue
            if flagged or _is_sealed_row(obj):
                return True
        return False
    try:
        obj, flagged = _loads_flagging_sealed(text)
    except (ValueError, RecursionError):
        return '"sealed"' in text
    if flagged or _is_sealed_row(obj):
        return True
    if isinstance(obj, list):
        return any(_is_sealed_row(item) for item in obj)
    return False


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def _parse_registry(data: Any) -> List[SetEntry]:
    """Validate the registry document and return its entries."""
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sets"), list):
        raise RegistryError("eval/sets.json must be {\"version\": 1, \"sets\": [...]}")
    entries: List[SetEntry] = []
    names = set()
    for i, raw in enumerate(data["sets"]):
        where = f"sets[{i}]"
        if not isinstance(raw, dict) or set(raw) != _REGISTRY_KEYS:
            raise RegistryError(f"{where}: keys must be exactly {sorted(_REGISTRY_KEYS)}")
        if raw["privacy"] not in (PUBLIC, PRIVATE):
            raise RegistryError(f"{where}: privacy must be public or private")
        if raw["role"] not in ROLES:
            raise RegistryError(f"{where}: role must be one of {ROLES}")
        if raw["status"] not in STATUSES:
            raise RegistryError(f"{where}: status must be one of {STATUSES}")
        if not isinstance(raw["sha256"], str) or not _SHA_RE.match(raw["sha256"]):
            raise RegistryError(f"{where}: sha256 must be 64 lowercase hex")
        if raw["name"] in names:
            raise RegistryError(f"{where}: duplicate name")
        names.add(raw["name"])
        entries.append(SetEntry(**{k: raw[k] for k in _REGISTRY_KEYS}))
    return entries


def _check_entry_not_sealed(entry: SetEntry) -> None:
    """Refuse a registry entry that is sealed by location or by marker."""
    path = entry.resolved()
    if is_under(path, _privacy.private_root() / "sealed"):
        raise RegistryError(f"registry entry {entry.name!r} lies under the sealed root")
    if path.exists() and has_sealed_marker(path):
        raise RegistryError(f"registry entry {entry.name!r} carries a sealed marker")


def load_registry() -> List[SetEntry]:
    """Load and validate ``eval/sets.json`` (tests monkeypatch this function).

    Raises:
        RegistryError: malformed registry, or a sealed/marked file registered.
    """
    with open(REGISTRY_PATH, encoding="utf-8") as fh:
        entries = _parse_registry(json.load(fh))
    for entry in entries:
        _check_entry_not_sealed(entry)
    return entries


def registry_from_dict(data: Any) -> List[SetEntry]:
    """Parse and validate a registry document already in memory (tests, tooling)."""
    entries = _parse_registry(data)
    for entry in entries:
        _check_entry_not_sealed(entry)
    return entries


def load_legacy_public() -> List[Dict[str, str]]:
    """Load ``eval/legacy_public.json``: ``{"version": 1, "entries": [{"path", "sha256"}]}``.

    Raises:
        RegistryError: on a malformed document.
    """
    if not LEGACY_PUBLIC_PATH.exists():
        return []
    with open(LEGACY_PUBLIC_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
        raise RegistryError("eval/legacy_public.json must be {\"version\": 1, \"entries\": [...]}")
    out = []
    for i, entry in enumerate(data["entries"]):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256"}
            or not _SHA_RE.match(str(entry["sha256"]))
        ):
            raise RegistryError(f"legacy entries[{i}] must be {{path, sha256}}")
        out.append({"path": str(entry["path"]), "sha256": entry["sha256"]})
    return out


def public_entries() -> List[SetEntry]:
    """Registered public entries."""
    return [e for e in load_registry() if e.privacy == PUBLIC]


def public_sha256s() -> set:
    """sha256s of every registered public set."""
    return {e.sha256 for e in public_entries()}


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _recorded_input_shas(path: Path) -> Optional[List[str]]:
    """The input sha256s a derived file records (rule 5), or None if it records none."""
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if suffix == ".json":
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        inputs = obj.get("inputs") if isinstance(obj, dict) else None
        if not isinstance(inputs, list) or not inputs:
            return None
        shas = []
        for item in inputs:
            sha = item.get("sha256") if isinstance(item, dict) else None
            if not isinstance(sha, str) or not _SHA_RE.match(sha):
                return None
            shas.append(sha)
        return shas
    if suffix == ".md":
        shas = [m.group(1) for line in text.splitlines() if (m := _MD_SET_SHA_RE.match(line))]
        return shas or None
    return None


def classify(path: Any, *, legacy_public: bool = False) -> str:
    """Classify one eval input path (rules 1-7 in the module docstring).

    Args:
        path: the eval input (set, cache, report or artifact) to classify.
        legacy_public: enables rule 6, the frozen legacy-public lookup.

    Returns:
        ``"public"``, ``"private"`` or ``"sealed"``.
    """
    p = Path(path).resolve()
    root = _privacy.private_root()
    if is_under(p, root / "sealed"):
        return SEALED
    if has_sealed_marker(p):
        return SEALED
    if is_under(p, root):
        return PRIVATE
    if not p.is_file():
        return PRIVATE
    digest = sha256_file(p)
    for entry in public_entries():
        if entry.resolved() == p and entry.sha256 == digest:
            return PUBLIC
    recorded = _recorded_input_shas(p)
    if recorded is not None:
        public = public_sha256s()
        if all(sha in public for sha in recorded):
            return PUBLIC
    if legacy_public and any(e["sha256"] == digest for e in load_legacy_public()):
        return PUBLIC
    return PRIVATE


def is_sealed(path: Any) -> bool:
    """Rules 1-2 only: under the sealed root, or carrying a sealed marker."""
    p = Path(path).resolve()
    return is_under(p, _privacy.private_root() / "sealed") or has_sealed_marker(p)


def floor(paths: Iterable[Any], *, legacy_public: bool = False) -> str:
    """The strictest :func:`classify` result over ``paths`` (public if none)."""
    return strictest(classify(p, legacy_public=legacy_public) for p in paths)


def refuse_sealed(paths: Iterable[Any]) -> None:
    """Raise :class:`SealedInputError` if any path classifies sealed (CLI exit 4)."""
    for p in paths:
        if is_sealed(p):
            raise SealedInputError("sealed eval input is refused in 16A-1 (exit 4)")


def registered_public_entry(path: Any) -> Optional[SetEntry]:
    """The public registry entry whose path and sha256 match ``path``, if any."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    digest = sha256_file(p)
    for entry in public_entries():
        if entry.resolved() == p and entry.sha256 == digest:
            return entry
    return None


def private_needle_sets() -> List[SetEntry]:
    """Registered private sets (the scanner's registry needle sources)."""
    return [e for e in load_registry() if e.privacy == PRIVATE]


def entries_for(paths: Sequence[Any]) -> List[Optional[SetEntry]]:
    """Registry entry (any privacy) per path, matched by resolved path and sha256."""
    out: List[Optional[SetEntry]] = []
    registry = load_registry()
    for path in paths:
        p = Path(path).resolve()
        digest = sha256_file(p) if p.is_file() else None
        out.append(next((e for e in registry if e.resolved() == p and e.sha256 == digest), None))
    return out
