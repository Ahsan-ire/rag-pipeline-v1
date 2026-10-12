"""Phase 16A-2a item a8 [C]: build the handbook dump (16A-2b item b0) from the chunker's output.

One JSON line per inventoried section::

    {"label": <section label>, "page_start": int, "page_end": int, "text": <the section's text>}

- **Labels** are the ``section_number`` values the chunker assigns, the same
  labels as ``sections`` in ``eval/section_inventory.json`` (item 7, written by
  ``scripts/absorbed_map.py`` from the same chunker). A section's chunks are
  grouped by label and their text concatenated in chunk (document) order, each
  chunk as the chunker returns it, citation prefix included.
- **Page span**: the smallest ``page_start`` and the largest ``page_end`` over
  the section's chunks (``null`` only if no chunk of the section has a printed
  page).
- **Check**: every label in the inventory's ``sections`` list must be present;
  otherwise the build fails (exit 1) and prints the missing labels, labels only,
  never text. Aliases (labels folded into another chunk by the D20 runt merges)
  and dotted ancestors have no chunk of their own, so they get no line; their
  text sits inside the section that absorbed them. Chunks whose label is not
  inventoried (chapter introductions carry ``""``) are not dumped.
- **Output** (production): ``data/research/handbook_sections.jsonl``. The file
  holds copyrighted corpus text: it is gitignored with ``data/``, the leak
  scanner reads its ``text`` key as the exempt handbook windows, and the CLI
  refuses an ``--out`` inside the repository but outside ``data/``.

The builder takes a chunk list (:func:`build_rows`), so tests pass a synthetic
in-memory one and never touch ``data/`` or ``chroma_db``. Production re-chunks
the corpus PDF exactly as ``scripts/absorbed_map.py`` does (no index needed).

Usage:
    python scripts/handbook_sections.py ./data/Conveyancing_Handbook.pdf [--inventory eval/section_inventory.json] [--out data/research/handbook_sections.jsonl]
    python scripts/handbook_sections.py --sample --inventory <sample inventory> --out <path>   # synthetic corpus
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_OUT = os.path.join(REPO, "data", "research", "handbook_sections.jsonl")


class MissingSectionsError(RuntimeError):
    """Inventoried sections absent from the chunk list (``missing`` = labels only)."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = sorted(missing)
        super().__init__(f"{len(self.missing)} inventoried section(s) missing: {', '.join(self.missing)}")


def build_rows(chunks: Sequence[Any], inventory: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Group ``chunks`` into one dump row per inventoried section (pure; no IO).

    Args:
        chunks: the chunker's output, in document order: objects with
            ``page_content`` and a ``metadata`` mapping holding
            ``section_number``, ``page_start`` and ``page_end``
            (``langchain_core.documents.Document``).
        inventory: a loaded ``eval/section_inventory.json`` dict; its
            ``sections`` list is the set of labels that must all be present.

    Returns:
        ``[{"label", "page_start", "page_end", "text"}, ...]`` in the order each
        section first appears in ``chunks``.

    Raises:
        MissingSectionsError: an inventoried section has no chunk.
        src.eval_schema.InventoryError: the inventory is malformed.
    """
    from src.eval_schema import inventory_sections

    inventory_sections(inventory)  # validates the whole document before use
    wanted = {str(s).strip() for s in inventory["sections"] if str(s).strip()}

    texts: Dict[str, List[str]] = {}
    firsts: Dict[str, List[int]] = {}
    lasts: Dict[str, List[int]] = {}
    for chunk in chunks:
        meta = chunk.metadata or {}
        label = str(meta.get("section_number") or "").strip()
        if label not in wanted:
            continue
        texts.setdefault(label, []).append(chunk.page_content)
        for store, key in ((firsts, "page_start"), (lasts, "page_end")):
            page = meta.get(key)
            if isinstance(page, int) and not isinstance(page, bool):
                store.setdefault(label, []).append(page)

    missing = sorted(wanted - set(texts))
    if missing:
        raise MissingSectionsError(missing)
    return [
        {
            "label": label,
            "page_start": min(firsts[label]) if label in firsts else None,
            "page_end": max(lasts[label]) if label in lasts else None,
            "text": "\n\n".join(parts),
        }
        for label, parts in texts.items()
    ]


def dump_rows(rows: Sequence[Dict[str, Any]]) -> str:
    """Serialise rows as JSONL (one line each, keys sorted, trailing newline)."""
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def _out_allowed(path: str) -> bool:
    """True unless ``path`` is inside the repository but outside its gitignored ``data/``."""
    real = os.path.realpath(path)
    repo = os.path.realpath(REPO)
    if os.path.commonpath([real, repo]) != repo:
        return True
    return os.path.commonpath([real, os.path.join(repo, "data")]) == os.path.join(repo, "data")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: re-chunk the corpus, check the inventory is covered, write the dump."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", nargs="?", help="the handbook PDF (as it was indexed)")
    parser.add_argument("--sample", action="store_true", help="use the synthetic sample corpus")
    parser.add_argument("--inventory", default=None, help="section inventory (default eval/section_inventory.json)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output JSONL (default data/research/handbook_sections.jsonl)")
    args = parser.parse_args(argv)
    if bool(args.sample) == bool(args.pdf):
        parser.error("give exactly one of a PDF path or --sample")
    if not _out_allowed(args.out):
        print("[handbook_sections] refusing --out: inside the repository but outside data/ (corpus text)", file=sys.stderr)
        return 2

    from src.eval_schema import InventoryError, load_inventory

    try:
        inventory = load_inventory(args.inventory)
    except InventoryError as exc:
        print(f"[handbook_sections] inventory unusable ({len(exc.errors)} error(s))", file=sys.stderr)
        return 2

    from src.chunker import chunk_handbook

    if args.sample:
        from scripts.sample_corpus import build_sample_corpus

        clean_text, page_map, metadata = build_sample_corpus()
    else:
        from src.ingest import load_handbook_pdf

        clean_text, page_map, metadata = load_handbook_pdf(args.pdf)
        if not clean_text:
            print("[handbook_sections] no text could be loaded from the PDF", file=sys.stderr)
            return 2
    chunks = chunk_handbook(clean_text, page_map, metadata)

    try:
        rows = build_rows(chunks, inventory)
    except MissingSectionsError as exc:
        print(f"[handbook_sections] {exc}", file=sys.stderr)
        return 1
    _write(args.out, dump_rows(rows))
    print(f"[handbook_sections] chunks {len(chunks)}, sections {len(rows)}, written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
