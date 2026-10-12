"""Phase 16A-2a item a8 [C]: ``inputs-check`` for the 16A-2b hand-made inputs (b0).

Two small, committed, content-free files are written by the orchestrator in
16A-2b and must be checked before anything is built from them:

- ``eval/v2_pairing.json`` -- ``{"pairs": [[golden_id, realistic_id], ...]}``:
  public golden-v1 and realistic-v1 rows that are one scenario. Every id must
  be a **public v1 id** of the right set (``"q:" + sha256(question)[:12]``,
  :func:`src.eval_privacy.public_v1_id`) and each id may be used **at most
  once** in the whole map (either column).
- ``eval/conflict_sections.json`` -- ``{"labels": [...]}``: section labels
  only. Every label must be **inventoried**: in ``eval/section_inventory.json``
  as a section, an alias or a dotted ancestor of one, which is the same test an
  evidence label passes (:func:`src.eval_schema.inventory_sections`).

:func:`inputs_check` is the function ``scripts/sealed_tool.py`` calls for its
``inputs-check`` command (item a3, later). It prints nothing and reads no
private file. Errors name the file, the field path (``pairs[3][1]``) and a
fixed reason, never a value, so a mistyped private id or question pasted into
either file cannot reach an output (the eval_schema convention: errors by
field, never text).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Collection, Dict, List, Mapping, Optional, Tuple

from src.eval_privacy import REPO_ROOT
from src.eval_schema import inventory_sections, load_any, load_inventory
from src.eval_sets import SetEntry, public_entries, registered_public_entry

PAIRING_PATH = REPO_ROOT / "eval" / "v2_pairing.json"
CONFLICT_PATH = REPO_ROOT / "eval" / "conflict_sections.json"

# Registry names of the two public v1 sets a pair joins (``eval/sets.json``).
GOLDEN_SET = "golden-v1"
REALISTIC_SET = "realistic-v1"

# (field path, fixed reason): no value ever appears in either.
FieldError = Tuple[str, str]


class InputsError(ValueError):
    """An ``inputs-check`` input is malformed (``errors`` = ``(file, field, reason)``).

    The message and ``args`` carry only file names, field paths and fixed
    reasons -- never an id, a label or any other value from the files.
    """

    def __init__(self, errors: List[Tuple[str, str, str]]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(f"{f}: {p}: {m}" for f, p, m in self.errors))


def public_v1_ids(set_name: str) -> frozenset:
    """The public v1 row ids of one registered public set (``golden-v1`` ...).

    Args:
        set_name: the registry name of a public set.

    Raises:
        InputsError: the name is not a registered public set, or the file on
            disk no longer matches its registered sha256 (its ids would then
            be salted, not public).
    """
    entry: Optional[SetEntry] = next((e for e in public_entries() if e.name == set_name), None)
    if entry is None:
        raise InputsError([(set_name, "set", "not a registered public set")])
    if registered_public_entry(entry.resolved()) is None:
        raise InputsError([(set_name, "set", "file does not match its registered sha256")])
    return frozenset(row["id"] for row in load_any(entry.resolved()))


def check_pairing(doc: Any, golden_ids: Collection[str], realistic_ids: Collection[str]) -> List[FieldError]:
    """Errors in a pairing document (pure; empty list = valid).

    Checks the shape (``{"pairs": [[golden_id, realistic_id], ...]}`` and
    nothing else), that column 0 holds golden-v1 ids and column 1 realistic-v1
    ids, and that no id is used twice anywhere in the map.

    Args:
        doc: the parsed ``eval/v2_pairing.json``.
        golden_ids / realistic_ids: the public v1 ids of the two sets.
    """
    if not isinstance(doc, dict) or set(doc) != {"pairs"}:
        return [("pairs", 'must be an object with exactly the key "pairs"')]
    pairs = doc["pairs"]
    if not isinstance(pairs, list):
        return [("pairs", "must be a list")]
    errors: List[FieldError] = []
    seen: Dict[str, int] = {}
    columns = (("golden-v1", golden_ids), ("realistic-v1", realistic_ids))
    for i, pair in enumerate(pairs):
        if not isinstance(pair, list) or len(pair) != 2:
            errors.append((f"pairs[{i}]", "must be [golden_id, realistic_id]"))
            continue
        for j, (rid, (set_name, public)) in enumerate(zip(pair, columns)):
            where = f"pairs[{i}][{j}]"
            if not isinstance(rid, str) or not rid:
                errors.append((where, "must be a non-empty string"))
                continue
            if rid not in public:
                errors.append((where, f"not a public {set_name} id"))
            if rid in seen:
                errors.append((where, f"id already used at pairs[{seen[rid]}]"))
            else:
                seen[rid] = i
    return errors


def check_conflict_labels(doc: Any, inventory: Mapping[str, Any]) -> List[FieldError]:
    """Errors in a conflict-labels document (pure; empty list = valid).

    Args:
        doc: the parsed ``eval/conflict_sections.json``: ``{"labels": [...]}``.
        inventory: a loaded section-inventory dict (``load_inventory``).

    Raises:
        src.eval_schema.InventoryError: the inventory itself is malformed.
    """
    if not isinstance(doc, dict) or set(doc) != {"labels"}:
        return [("labels", 'must be an object with exactly the key "labels"')]
    labels = doc["labels"]
    if not isinstance(labels, list):
        return [("labels", "must be a list")]
    inventoried = inventory_sections(inventory)
    errors: List[FieldError] = []
    for i, label in enumerate(labels):
        where = f"labels[{i}]"
        if not isinstance(label, str) or not label.strip():
            errors.append((where, "must be a non-empty string"))
        elif label.strip() not in inventoried:
            errors.append((where, "not in the section inventory"))
    return errors


def _read_json(path: Path, errors: List[Tuple[str, str, str]]) -> Any:
    """Parse one input file; append an error (and return ``None``) if it cannot be read."""
    name = path.name
    if not path.is_file():
        errors.append((name, "file", "missing"))
        return None
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        errors.append((name, "file", "invalid JSON"))
        return None


def inputs_check(
    pairing_path: Any = None,
    conflict_path: Any = None,
    *,
    inventory: Optional[Mapping[str, Any]] = None,
    golden_ids: Optional[Collection[str]] = None,
    realistic_ids: Optional[Collection[str]] = None,
) -> Dict[str, int]:
    """Check both b0 input files; return their counts or raise :class:`InputsError`.

    Args:
        pairing_path / conflict_path: default to the committed
            ``eval/v2_pairing.json`` and ``eval/conflict_sections.json``.
        inventory: a loaded inventory dict; default the committed
            ``eval/section_inventory.json``.
        golden_ids / realistic_ids: the public v1 ids; default the ids of the
            registered ``golden-v1`` and ``realistic-v1`` sets. Tests pass
            synthetic collections.

    Returns:
        ``{"pairs": <pair count>, "labels": <label count>}`` (nothing else is
        safe to print).

    Raises:
        InputsError: a file is missing or invalid, an id is reused or not a
            public v1 id of its set, or a label is not inventoried -- every
            problem found, by file and field, no values.
        src.eval_schema.InventoryError: the inventory is missing or malformed.
    """
    pairing_file = Path(pairing_path) if pairing_path is not None else PAIRING_PATH
    conflict_file = Path(conflict_path) if conflict_path is not None else CONFLICT_PATH
    errors: List[Tuple[str, str, str]] = []

    pairing = _read_json(pairing_file, errors)
    if pairing is not None:
        golden = public_v1_ids(GOLDEN_SET) if golden_ids is None else golden_ids
        realistic = public_v1_ids(REALISTIC_SET) if realistic_ids is None else realistic_ids
        errors.extend((pairing_file.name, f, m) for f, m in check_pairing(pairing, golden, realistic))

    conflicts = _read_json(conflict_file, errors)
    if conflicts is not None:
        data = load_inventory() if inventory is None else inventory
        errors.extend((conflict_file.name, f, m) for f, m in check_conflict_labels(conflicts, data))

    if errors:
        raise InputsError(errors)
    return {"pairs": len(pairing["pairs"]), "labels": len(conflicts["labels"])}
