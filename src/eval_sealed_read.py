"""Phase 16A-2a read-only loader for sealed eval input (D72, item a1).

Sealed input is refused at every run, retrieval, generation and replay entry
(``classify`` -> ``sealed`` -> exit 4). This module is the one place that
*reads* a sealed set: validation (``validate_eval_set.py --sealed``), the leak
scanner and ``sealed_tool`` all load through :func:`load_sealed`, so there is
one validator and one definition of a sealed row.

It makes no retrieval, expansion or model call and imports none of the code
that could (``src.evaluator``, ``src.retriever``, ``src.generator``, ...); a
test imports it in a cold process and checks that. It reads one file and
returns rows. What a caller may then print is the caller's rule (D72: counts,
sha256s and enums; opaque ids only where the trusted-processor list names
them). Errors follow the schema's rule: ``(line, field, reason)`` triples with
fixed reasons, never a row value.

A parameter whose name contains ``sealed`` may appear in ``src/`` only in this
file (``tests/test_p16_ast_hygiene.py``), so no other module grows its own
sealed switch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src import eval_schema as _schema
from src.eval_privacy import SEALED
from src.eval_sets import is_sealed


class NotSealedInput(ValueError):
    """The path is neither under ``eval/private/sealed/`` nor carries a sealed marker."""


def load_sealed(path: Any, *, inventory: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Load and validate a sealed schema-2 eval set (read-only).

    The path must already be sealed by ``classify``'s rules 1-2: under
    ``eval/private/sealed/`` or holding a row with ``"sealed": true``. A
    path that is not is refused rather than read, so this is not a general
    loader. The rows are validated by the same validator as every v2 set,
    with opaque-id rules (a sealed set is never public). A row may omit the
    ``sealed`` key (the file is sealed by location or by another row); a row
    that has it must hold ``true``.

    Args:
        path: the sealed JSONL file.
        inventory: a loaded section-inventory dict; ``None`` loads the
            committed ``eval/section_inventory.json`` (absent -> error).

    Returns:
        Normalised rows (the shape ``load_v2`` returns), in file order. Hash
        one with :func:`src.eval_schema.row_hash`.

    Raises:
        NotSealedInput: the path is not sealed.
        SchemaError: a ``.md``/``.py`` file, a v1 or mixed file, or any
            line/field violation (including ``sealed`` not ``true``).
        InventoryError: the inventory is missing or malformed.
    """
    p = Path(path)
    _schema._refuse_non_eval_suffix(p)
    if not is_sealed(p):
        raise NotSealedInput("not a sealed eval input")
    rows, parse_errors = _schema._read_rows(p)
    version = _schema._detect(rows, parse_errors)
    if version != _schema.SCHEMA_VERSION:
        raise _schema.SchemaError([(rows[0][0], "schema", "expected schema 2")])
    return _schema._load_v2_rows(p, SEALED, rows, inventory)
