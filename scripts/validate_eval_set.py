"""Validate an eval set (schema v1 or v2) without ever printing row text (D66, item 2).

Usage::

    python scripts/validate_eval_set.py <path> [--inventory <path>]

Exit codes:

- ``0`` valid: prints counts only (schema version, rows, families, scope counts);
- ``1`` invalid: prints ``line N: field: reason`` lines (no question, keyword,
  section or raw value);
- ``2`` the input or the section inventory is missing or malformed (a v2 file
  is never validated without its inventory);
- ``4`` sealed input, refused before anything else is read (item 1).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.eval_privacy import SEALED, SealedInputError  # noqa: E402
from src.eval_schema import (  # noqa: E402
    InventoryError,
    SchemaError,
    load_any,
    load_inventory,
    render_errors,
)
from src.eval_sets import classify  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_CONFIG = 2
EXIT_SEALED = 4
SEALED_MESSAGE = "refused: sealed eval input (16A-1 refuses sealed sets)"


def summarise(rows: Sequence[dict]) -> List[str]:
    """Aggregate-only summary lines for a valid set (no ids, no text)."""
    scopes = Counter(row["scope"] for row in rows)
    versions = sorted({row["schema"] for row in rows})
    return [
        f"schema: {versions[0] if versions else '-'}",
        f"rows: {len(rows)}",
        f"families: {len({row['family_id'] for row in rows})}",
        "scopes: " + ", ".join(f"{s}={scopes.get(s, 0)}" for s in ("answer", "partial", "refuse")),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the validator; return the process exit code."""
    parser = argparse.ArgumentParser(description="Validate an eval set (v1 or v2); never prints row text.")
    parser.add_argument("path", help="eval set JSONL")
    parser.add_argument("--inventory", default=None, help="section inventory JSON (default: eval/section_inventory.json)")
    args = parser.parse_args(argv)

    # Item 1: the sealed check is the first thing done with the input.
    try:
        if classify(args.path) == SEALED:
            print(SEALED_MESSAGE, file=sys.stderr)
            return EXIT_SEALED
    except SealedInputError:
        print(SEALED_MESSAGE, file=sys.stderr)
        return EXIT_SEALED
    if not os.path.isfile(args.path):
        print("error: input file missing", file=sys.stderr)
        return EXIT_CONFIG

    try:
        inventory = load_inventory(args.inventory) if args.inventory is not None else None
        rows = load_any(args.path, inventory=inventory)
    except SealedInputError:
        print(SEALED_MESSAGE, file=sys.stderr)
        return EXIT_SEALED
    except InventoryError as exc:
        print(render_errors(exc.errors), file=sys.stderr)
        return EXIT_CONFIG
    except SchemaError as exc:
        print(render_errors(exc.errors))
        return EXIT_INVALID
    for line in summarise(rows):
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
