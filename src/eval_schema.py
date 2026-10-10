"""Phase 16A-1 eval-set schema v2 (D66, item 2): loader, detector and validator core.

A **v2** eval set is JSONL with ``"schema": 2`` on every row (no header line)
and no v1 rows mixed in. Each row::

    {"schema": 2, "id": ..., "family_id": ..., "question": ...,
     "scope": "answer" | "partial" | "refuse",
     "evidence": [[sec, ...], ...],          # AND groups of OR lists; [] iff refuse
     "gaps": [{"id": ..., "keywords": [...]}, ...],   # partial only, non-empty
     # optional:
     "type", "register", "source", "ambiguous", "owner_status", "second_status"}

**Identity.** ``id`` is unique per set; ``family_id`` groups a scenario and its
paraphrases. In a *public* set (``classify(path) == "public"``) both match
``^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$``. In any other set ``family_id`` matches
``^f[0-9a-f]{8}$`` and ``id`` is ``<family_id>-<n>`` (``n`` a decimal integer
without leading zeros), nothing else -- so a non-public id cannot carry text.

**Inventory.** Every evidence section must be inventoried: present in the
``sections`` or the ``aliases`` list of ``eval/section_inventory.json`` (item
7, ``{"version": 1, "map_sha256", "sections", "aliases"}``). An uninventoried
section is a line/field error (the D54 class). The check is never skipped: if
no inventory is passed and the committed file is absent, :class:`InventoryError`
is raised.

**v1 rows** (``question``/``type``/``expected_sections``, no ``schema`` key)
still load through ``src.evaluator.load_golden_set`` unchanged; :func:`load_any`
additionally normalises them to the v2 row shape with ``id`` = ``public_v1_id``
(public set) or ``private_v1_id`` salted by the file's sha256 (any other set),
and ``family_id = id``.

**Never text.** Every error is a ``(line, field, reason)`` triple: a 1-indexed
physical line number (0 = the file or the inventory as a whole), a field path
built only from schema key names and list indices (an unknown key is reported
as ``<unknown>``, since a key name could itself be text), and a fixed reason
phrase. No question, keyword, section value, id or raw bad value ever appears
in an error, so errors are safe to print for private sets.

**Sealed refusal (item 1).** :func:`load_v2`, :func:`load_any` and
:func:`detect_schema` classify the path first and raise
:class:`~src.eval_privacy.SealedInputError` for sealed input before reading a
row (the validator CLI exits 4).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.eval_privacy import (
    PUBLIC,
    REPO_ROOT,
    SEALED,
    SealedInputError,
    private_v1_id,
    public_v1_id,
)
from src.eval_sets import classify, sha256_file

SCHEMA_VERSION = 2
INVENTORY_PATH = REPO_ROOT / "eval" / "section_inventory.json"

PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
PRIVATE_FAMILY_RE = re.compile(r"^f[0-9a-f]{8}$")
PRIVATE_ID_RE = re.compile(r"^(f[0-9a-f]{8})-(0|[1-9][0-9]*)$")
GAP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

SCOPES = ("answer", "partial", "refuse")
V1_TYPES = ("direct", "exact_token", "refusal")
REGISTERS = ("modelled", "lay", "terse")
SOURCES = ("handbook", "tutorial", "colleague", "legacy")
OWNER_STATUSES = ("draft", "approved", "changed", "rejected")
SECOND_STATUSES = ("not_required", "pending", "agree", "disagree", "missing")

# ``type`` (v1 vocabulary) must agree with ``scope``.
TYPE_SCOPES: Dict[str, Tuple[str, ...]] = {
    "refusal": ("refuse",),
    "direct": ("answer", "partial"),
    "exact_token": ("answer", "partial"),
}

REQUIRED_KEYS = ("schema", "id", "family_id", "question", "scope", "evidence")
OPTIONAL_KEYS = ("gaps", "type", "register", "source", "ambiguous", "owner_status", "second_status")
_ALLOWED_KEYS = frozenset(REQUIRED_KEYS + OPTIONAL_KEYS)
_ENUMS: Dict[str, Tuple[str, ...]] = {
    "register": REGISTERS,
    "source": SOURCES,
    "owner_status": OWNER_STATUSES,
    "second_status": SECOND_STATUSES,
}
_INVENTORY_KEYS = frozenset({"version", "map_sha256", "sections", "aliases"})

ErrorTuple = Tuple[int, str, str]


class SchemaError(ValueError):
    """An eval set (or inventory) violates the schema.

    ``errors`` is a list of ``(line, field, reason)`` triples; ``str()`` renders
    only those (``line N: field: reason``), never row content.
    """

    def __init__(self, errors: Sequence[ErrorTuple]) -> None:
        """Store the error triples and render them as the exception message."""
        self.errors: List[ErrorTuple] = [(int(ln), str(f), str(r)) for ln, f, r in errors]
        super().__init__(render_errors(self.errors))


class InventoryError(SchemaError):
    """The section inventory is missing or malformed (reported at line 0)."""


def render_errors(errors: Iterable[ErrorTuple]) -> str:
    """Render error triples as ``line N: field: reason`` lines (no row text)."""
    return "\n".join(f"line {line}: {field}: {reason}" for line, field, reason in errors)


# --------------------------------------------------------------------------
# Low-level reading (no text in errors)
# --------------------------------------------------------------------------
def _refuse_if_sealed(path: Path) -> str:
    """Classify ``path``; raise for sealed input, else return its class."""
    privacy = classify(path)
    if privacy == SEALED:
        raise SealedInputError("sealed eval input is refused in 16A-1 (exit 4)")
    return privacy


def _read_rows(path: Path) -> Tuple[List[Tuple[int, Any]], List[ErrorTuple]]:
    """Parse a JSONL file into ``(line, object)`` pairs, skipping blank lines.

    Undecodable or unparseable lines become ``(line, "<row>", reason)`` errors
    with a fixed reason (the JSON decoder's message is never surfaced).
    """
    rows: List[Tuple[int, Any]] = []
    errors: List[ErrorTuple] = []
    raw = path.read_bytes()
    for number, raw_line in enumerate(raw.split(b"\n"), start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            errors.append((number, "<row>", "invalid UTF-8"))
            continue
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            errors.append((number, "<row>", "invalid JSON"))
            continue
        if not isinstance(obj, dict):
            errors.append((number, "<row>", "row is not a JSON object"))
            continue
        rows.append((number, obj))
    return rows, errors


def _row_version(row: Mapping[str, Any]) -> Optional[int]:
    """1 for a row with no ``schema`` key, 2 for ``"schema": 2``, else ``None``."""
    if "schema" not in row:
        return 1
    value = row["schema"]
    if type(value) is int and value == SCHEMA_VERSION:
        return SCHEMA_VERSION
    return None


def _detect(rows: Sequence[Tuple[int, Any]], parse_errors: List[ErrorTuple]) -> int:
    """Version of parsed rows; raises :class:`SchemaError` on mixed/unknown/empty."""
    errors = list(parse_errors)
    first: Optional[int] = None
    for number, row in rows:
        version = _row_version(row)
        if version is None:
            errors.append((number, "schema", "unknown schema version"))
            continue
        if first is None:
            first = version
        elif version != first:
            errors.append((number, "schema", "mixed schema versions"))
    if errors:
        raise SchemaError(errors)
    if first is None:
        raise SchemaError([(0, "<file>", "no rows")])
    return first


def detect_schema(path: Any) -> int:
    """Return the schema version (1 or 2) of an eval JSONL file.

    Refuses sealed input first. A file mixing v1 and v2 rows, a row with an
    unknown ``schema`` value, an unparseable line or an empty file raises
    :class:`SchemaError`.
    """
    p = Path(path)
    _refuse_if_sealed(p)
    rows, parse_errors = _read_rows(p)
    return _detect(rows, parse_errors)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def inventory_sections(inventory: Any) -> frozenset:
    """Validate an inventory dict and return its inventoried labels.

    Inventoried = in either ``sections`` or ``aliases`` (item 7). Ordering is
    item 7's concern and is not enforced here.

    Raises:
        InventoryError: wrong top-level keys, ``version`` != 1, a non-hex
            ``map_sha256``, or a list that is not all non-empty strings.
    """
    if not isinstance(inventory, dict):
        raise InventoryError([(0, "inventory", "not a JSON object")])
    errors: List[ErrorTuple] = []
    if set(inventory) != _INVENTORY_KEYS:
        errors.append((0, "inventory", "keys must be version, map_sha256, sections, aliases"))
    version = inventory.get("version")
    if not (type(version) is int and version == 1):
        errors.append((0, "inventory.version", "unsupported version"))
    sha = inventory.get("map_sha256")
    if not (isinstance(sha, str) and _SHA_RE.match(sha)):
        errors.append((0, "inventory.map_sha256", "must be 64 lowercase hex"))
    labels: set = set()
    for key in ("sections", "aliases"):
        items = inventory.get(key)
        if not isinstance(items, list) or not all(isinstance(s, str) and s.strip() for s in items):
            errors.append((0, f"inventory.{key}", "must be a list of non-empty strings"))
            continue
        labels.update(s.strip() for s in items)
    if errors:
        raise InventoryError(errors)
    return frozenset(labels)


def load_inventory(path: Any = None) -> Dict[str, Any]:
    """Load the section inventory (default: the committed ``eval/section_inventory.json``).

    Raises:
        InventoryError: the file is absent, is not valid JSON, or is malformed.
    """
    p = Path(path) if path is not None else INVENTORY_PATH
    if not p.is_file():
        raise InventoryError([(0, "inventory", "inventory file missing")])
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise InventoryError([(0, "inventory", "invalid JSON")]) from None
    inventory_sections(data)
    return data


# --------------------------------------------------------------------------
# v2 row validation
# --------------------------------------------------------------------------
def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_identity(row: Mapping[str, Any], line: int, public: bool, errors: List[ErrorTuple]) -> None:
    """Append id/family_id errors for one row."""
    rid = row.get("id")
    fid = row.get("family_id")
    if public:
        if not (isinstance(fid, str) and PUBLIC_ID_RE.match(fid)):
            errors.append((line, "family_id", "not a valid public id"))
        if not (isinstance(rid, str) and PUBLIC_ID_RE.match(rid)):
            errors.append((line, "id", "not a valid public id"))
        return
    fid_ok = isinstance(fid, str) and bool(PRIVATE_FAMILY_RE.match(fid))
    if not fid_ok:
        errors.append((line, "family_id", "not an opaque family id"))
    match = PRIVATE_ID_RE.match(rid) if isinstance(rid, str) else None
    if match is None:
        errors.append((line, "id", "not an opaque row id"))
    elif fid_ok and match.group(1) != fid:
        errors.append((line, "id", "id does not extend its family_id"))


def _check_evidence(row: Mapping[str, Any], line: int, scope: Any, errors: List[ErrorTuple]) -> List[List[str]]:
    """Validate ``evidence``; return the stripped groups (``[]`` when invalid)."""
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        errors.append((line, "evidence", "must be a list of groups"))
        return []
    groups: List[List[str]] = []
    ok = True
    for gi, group in enumerate(evidence):
        if not isinstance(group, list):
            errors.append((line, f"evidence[{gi}]", "group must be a list"))
            ok = False
            continue
        if not group:
            errors.append((line, f"evidence[{gi}]", "empty group"))
            ok = False
            continue
        cleaned: List[str] = []
        for si, section in enumerate(group):
            if not _nonempty_str(section):
                errors.append((line, f"evidence[{gi}][{si}]", "section must be a non-empty string"))
                ok = False
                continue
            cleaned.append(section.strip())
        groups.append(cleaned)
    if scope == "refuse" and evidence:
        errors.append((line, "evidence", "must be [] for scope refuse"))
    if scope in ("answer", "partial") and not evidence:
        errors.append((line, "evidence", "must be non-empty unless scope is refuse"))
    return groups if ok else []


def _check_gaps(row: Mapping[str, Any], line: int, scope: Any, errors: List[ErrorTuple]) -> List[Dict[str, Any]]:
    """Validate ``gaps`` (partial only); return normalised gaps."""
    if "gaps" not in row:
        if scope == "partial":
            errors.append((line, "gaps", "required for scope partial"))
        return []
    gaps = row["gaps"]
    if scope != "partial":
        errors.append((line, "gaps", "only allowed for scope partial"))
        return []
    if not isinstance(gaps, list) or not gaps:
        errors.append((line, "gaps", "must be a non-empty list"))
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for gi, gap in enumerate(gaps):
        where = f"gaps[{gi}]"
        if not isinstance(gap, dict):
            errors.append((line, where, "gap must be an object"))
            continue
        if set(gap) != {"id", "keywords"}:
            errors.append((line, where, "gap keys must be id and keywords"))
        gid = gap.get("id")
        if not (isinstance(gid, str) and GAP_ID_RE.match(gid)):
            errors.append((line, f"{where}.id", "not a valid gap id"))
        elif gid in seen:
            errors.append((line, f"{where}.id", "duplicate gap id"))
        else:
            seen.add(gid)
        keywords = gap.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            errors.append((line, f"{where}.keywords", "must be a non-empty list"))
            continue
        kept: List[str] = []
        for ki, kw in enumerate(keywords):
            if not _nonempty_str(kw):
                errors.append((line, f"{where}.keywords[{ki}]", "empty keyword"))
            else:
                kept.append(kw.strip())
        out.append({"id": gid, "keywords": kept})
    return out


def _validate_v2_row(
    row: Mapping[str, Any], line: int, public: bool, inventoried: frozenset, errors: List[ErrorTuple]
) -> Dict[str, Any]:
    """Validate one v2 row, appending errors; return its normalised form."""
    for key in row:
        if key not in _ALLOWED_KEYS:
            errors.append((line, "<unknown>", "unknown key"))
    for key in REQUIRED_KEYS:
        if key not in row:
            errors.append((line, key, "missing"))
    _check_identity(row, line, public, errors)
    if not _nonempty_str(row.get("question")):
        errors.append((line, "question", "must be a non-empty string"))
    scope = row.get("scope")
    if scope not in SCOPES:
        errors.append((line, "scope", "unknown scope"))
        scope_ok = None
    else:
        scope_ok = scope
    groups = _check_evidence(row, line, scope_ok, errors) if "evidence" in row else []
    gaps = _check_gaps(row, line, scope_ok, errors)
    for gi, group in enumerate(groups):
        for si, section in enumerate(group):
            if section not in inventoried:
                errors.append((line, f"evidence[{gi}][{si}]", "section not in inventory"))
    if "type" in row:
        rtype = row["type"]
        if rtype not in TYPE_SCOPES:
            errors.append((line, "type", "unknown type"))
        elif scope_ok is not None and scope_ok not in TYPE_SCOPES[rtype]:
            errors.append((line, "type", "type disagrees with scope"))
    for key, allowed in _ENUMS.items():
        if key in row and row[key] not in allowed:
            reason = "unknown status" if key.endswith("_status") else "unknown value"
            errors.append((line, key, reason))
    if "ambiguous" in row and not isinstance(row["ambiguous"], bool):
        errors.append((line, "ambiguous", "must be a boolean"))
    return _normalised(
        schema=SCHEMA_VERSION,
        rid=row.get("id"),
        fid=row.get("family_id"),
        question=row.get("question"),
        scope=scope_ok,
        evidence=groups,
        gaps=gaps,
        rtype=row.get("type"),
        extras={k: row.get(k) for k in ("register", "source", "ambiguous", "owner_status", "second_status")},
    )


def _normalised(
    *,
    schema: int,
    rid: Any,
    fid: Any,
    question: Any,
    scope: Any,
    evidence: List[List[str]],
    gaps: List[Dict[str, Any]],
    rtype: Any,
    extras: Mapping[str, Any],
) -> Dict[str, Any]:
    """The common normalised row shape returned by :func:`load_v2` and :func:`load_any`."""
    return {
        "schema": schema,
        "id": rid,
        "family_id": fid,
        "question": question,
        "scope": scope,
        "evidence": evidence,
        "gaps": gaps,
        "type": rtype,
        "register": extras.get("register"),
        "source": extras.get("source"),
        "ambiguous": extras.get("ambiguous"),
        "owner_status": extras.get("owner_status"),
        "second_status": extras.get("second_status"),
    }


def _check_unique_ids(rows: Sequence[Tuple[int, Dict[str, Any]]], errors: List[ErrorTuple]) -> None:
    """A repeated ``id`` is an error at every line after its first use."""
    seen: set = set()
    for line, row in rows:
        rid = row.get("id")
        if not isinstance(rid, str):
            continue
        if rid in seen:
            errors.append((line, "id", "duplicate id"))
        seen.add(rid)


def _resolve_inventory(inventory: Optional[Mapping[str, Any]]) -> frozenset:
    """Inventoried labels from a passed dict, else the committed file (never skipped)."""
    data = load_inventory() if inventory is None else inventory
    return inventory_sections(data)


def _load_v2_rows(
    path: Path, privacy: str, rows: Sequence[Tuple[int, Any]], inventory: Optional[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    inventoried = _resolve_inventory(inventory)
    errors: List[ErrorTuple] = []
    out: List[Tuple[int, Dict[str, Any]]] = []
    public = privacy == PUBLIC
    for line, row in rows:
        out.append((line, _validate_v2_row(row, line, public, inventoried, errors)))
    _check_unique_ids(out, errors)
    if errors:
        errors.sort(key=lambda e: e[0])
        raise SchemaError(errors)
    return [row for _, row in out]


def load_v2(path: Any, *, inventory: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Load and validate a schema-2 eval set.

    Args:
        path: the v2 JSONL file. Its privacy class (``classify``) picks the id
            rules: public ids for a public set, opaque ids otherwise.
        inventory: a loaded section-inventory dict; ``None`` loads the
            committed ``eval/section_inventory.json`` (absent -> error).

    Returns:
        Normalised rows (see :func:`load_any`), in file order.

    Raises:
        SealedInputError: the input classifies sealed (checked first).
        SchemaError: any line/field violation, including v1 or mixed rows.
        InventoryError: the inventory is missing or malformed.
    """
    p = Path(path)
    privacy = _refuse_if_sealed(p)
    rows, parse_errors = _read_rows(p)
    version = _detect(rows, parse_errors)
    if version != SCHEMA_VERSION:
        raise SchemaError([(rows[0][0], "schema", "expected schema 2")])
    return _load_v2_rows(p, privacy, rows, inventory)


# --------------------------------------------------------------------------
# v1 rows, normalised
# --------------------------------------------------------------------------
def _load_v1_rows(path: Path, privacy: str, rows: Sequence[Tuple[int, Any]]) -> List[Dict[str, Any]]:
    """Validate v1 rows with ``load_golden_set``'s rules, errors without text.

    Each row normalises to ``scope`` ``refuse`` (type ``refusal``, evidence
    ``[]``) or ``answer`` with one OR group (v1 matching is OR-only), and gets
    ``id``/``family_id`` per D65/D66. Extra keys are ignored, as v1 does.
    """
    errors: List[ErrorTuple] = []
    out: List[Tuple[int, Dict[str, Any]]] = []
    salt = sha256_file(path) if privacy != PUBLIC else None
    for line, row in rows:
        question = row.get("question")
        rtype = row.get("type")
        sections = row.get("expected_sections")
        bad = False
        if not _nonempty_str(question):
            errors.append((line, "question", "must be a non-empty string"))
            bad = True
        if rtype not in V1_TYPES:
            errors.append((line, "type", "unknown type"))
            bad = True
        groups: List[List[str]] = []
        if rtype == "refusal":
            if sections not in (None, []):
                errors.append((line, "expected_sections", "must be [] for type refusal"))
                bad = True
        elif rtype in V1_TYPES:
            if not isinstance(sections, list) or not sections:
                errors.append((line, "expected_sections", "must be a non-empty list"))
                bad = True
            elif not all(_nonempty_str(s) for s in sections):
                errors.append((line, "expected_sections", "sections must be non-empty strings"))
                bad = True
            else:
                groups = [[s.strip() for s in sections]]
        if bad:
            continue
        rid = public_v1_id(question) if salt is None else private_v1_id(question, salt)
        out.append(
            (
                line,
                _normalised(
                    schema=1,
                    rid=rid,
                    fid=rid,
                    question=question,
                    scope="refuse" if rtype == "refusal" else "answer",
                    evidence=groups,
                    gaps=[],
                    rtype=rtype,
                    extras={},
                ),
            )
        )
    _check_unique_ids(out, errors)
    if errors:
        errors.sort(key=lambda e: e[0])
        raise SchemaError(errors)
    return [row for _, row in out]


def load_any(path: Any, *, inventory: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Load a v1 or v2 eval set as normalised rows (sealed refused first).

    Every row has the keys ``schema`` (1|2), ``id``, ``family_id``,
    ``question``, ``scope``, ``evidence`` (list of OR groups), ``gaps`` (list
    of ``{"id", "keywords"}``), ``type`` (``None`` if a v2 row omits it),
    ``register``, ``source``, ``ambiguous``, ``owner_status`` and
    ``second_status`` (``None`` when absent; always ``None`` for v1).

    v1 rows: ``id = family_id`` = ``public_v1_id(question)`` in a public set,
    else ``private_v1_id(question, sha256(file))``; a duplicate question is a
    ``duplicate id`` error. The inventory is consulted for v2 files only.
    ``src.evaluator.load_golden_set`` is untouched and remains the v5 loader.

    Raises:
        SealedInputError, SchemaError, InventoryError: as :func:`load_v2`.
    """
    p = Path(path)
    privacy = _refuse_if_sealed(p)
    rows, parse_errors = _read_rows(p)
    version = _detect(rows, parse_errors)
    if version == SCHEMA_VERSION:
        return _load_v2_rows(p, privacy, rows, inventory)
    return _load_v1_rows(p, privacy, rows)
