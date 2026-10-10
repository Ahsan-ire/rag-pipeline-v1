"""Phase 16A-1 item 7 [C]: build the absorbed-section map and the section inventory (D69).

Runs the chunker's absorption side channel (``chunk_handbook_with_absorption``,
chunks unchanged) over the corpus -- no re-index -- and writes two files:

- ``eval/absorbed_sections.json``:
  ``{"version": 1, "source", "chunk_count", "map": {chunk_id: [absorbed section, ...]},
  "map_sha256"}`` -- chunk ids and section numbers only, keyed by the production
  chunk id (``src.embedder.compute_chunk_id``); ``map_sha256`` is the sha256 of
  the canonical JSON of ``map``.
- ``eval/section_inventory.json``:
  ``{"version": 1, "map_sha256", "sections", "aliases"}`` (sorted): every
  ``section_number`` stored in the index, and every absorbed label. A section is
  inventoried if it is in either list (``src.eval_schema``).

Every map key must exist in the index (else the run fails, nothing written): the
map is only meaningful for the exact chunking the index holds. Run it after any
re-index; the map hash binds C4 comparisons and v6 strict alias credit.

Usage:
    python scripts/absorbed_map.py ./data/Conveyancing_Handbook.pdf [--persist-dir ./chroma_db] [--out-dir eval]
    python scripts/absorbed_map.py --sample --persist-dir sample_chroma_db --out-dir <dir>   # synthetic corpus
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class AbsorbedMapError(RuntimeError):
    """The map cannot be trusted (a key absent from the index, mismatched source)."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def map_sha256(mapping: Dict[str, List[str]]) -> str:
    """sha256 of the canonical JSON of the map."""
    return hashlib.sha256(_canon(mapping).encode("utf-8")).hexdigest()


def build(
    clean_text: str,
    page_map: Sequence[Any],
    metadata: Dict[str, Any],
    index_ids: Sequence[str],
    index_sections: Sequence[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build ``(absorbed_sections doc, section_inventory doc)`` (pure; no IO).

    Args:
        clean_text, page_map, metadata: the ingest triple the index was built from.
        index_ids: every chunk id stored in the index.
        index_sections: every ``section_number`` stored in the index.

    Raises:
        AbsorbedMapError: if a mapped chunk id is absent from the index.
    """
    from src.chunker import (
        absorbed_sections_by_chunk,
        chunk_handbook_with_absorption,
        production_chunk_ids,
    )

    chunks, log = chunk_handbook_with_absorption(clean_text, list(page_map), metadata)
    ids = production_chunk_ids(chunks)
    mapping = absorbed_sections_by_chunk(log, ids)
    known = set(index_ids)
    missing = sorted(k for k in mapping if k not in known)
    if missing:
        raise AbsorbedMapError(
            f"{len(missing)} mapped chunk id(s) are absent from the index; the map "
            "does not describe the indexed chunking (re-index or check the source path)"
        )
    mapping = {k: sorted(set(v)) for k, v in sorted(mapping.items())}
    digest = map_sha256(mapping)
    absorbed = {
        "version": 1,
        "source": metadata.get("source"),
        "chunk_count": len(chunks),
        "map": mapping,
        "map_sha256": digest,
    }
    aliases = sorted({s for v in mapping.values() for s in v})
    sections = sorted({str(s).strip() for s in index_sections if str(s).strip()})
    inventory = {"version": 1, "map_sha256": digest, "sections": sections, "aliases": aliases}
    return absorbed, inventory


def _index_contents(persist_dir: str) -> Tuple[List[str], List[str], List[str]]:
    """``(ids, section_numbers, sources)`` stored in a Chroma index (no embeddings)."""
    from src.embedder import get_vector_store

    store = get_vector_store(persist_directory=persist_dir)
    got = store.get(include=["metadatas"])
    metas = got.get("metadatas") or []
    return (
        list(got["ids"]),
        [str((m or {}).get("section_number", "")) for m in metas],
        sorted({str((m or {}).get("source", "")) for m in metas}),
    )


def _write(path: str, doc: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: build both files from the corpus and the index they describe."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", nargs="?", help="the handbook PDF the index was built from")
    parser.add_argument("--sample", action="store_true", help="use the synthetic sample corpus")
    parser.add_argument("--persist-dir", default=None, help="index directory (default ./chroma_db)")
    parser.add_argument("--out-dir", default=os.path.join(REPO, "eval"))
    args = parser.parse_args(argv)
    if bool(args.sample) == bool(args.pdf):
        parser.error("give exactly one of a PDF path or --sample")

    if args.sample:
        from scripts.sample_corpus import build_sample_corpus

        clean_text, page_map, metadata = build_sample_corpus()
        persist = args.persist_dir or os.path.join(REPO, "sample_chroma_db")
    else:
        from src.ingest import load_handbook_pdf

        clean_text, page_map, metadata = load_handbook_pdf(args.pdf)
        from src.embedder import CHROMA_PERSIST_DIR

        persist = args.persist_dir or CHROMA_PERSIST_DIR

    ids, sections, sources = _index_contents(persist)
    if metadata.get("source") not in sources:
        print(
            "[absorbed_map] the corpus source path differs from the index's; pass the "
            "PDF path exactly as it was indexed",
            file=sys.stderr,
        )
        return 2
    try:
        absorbed, inventory = build(clean_text, page_map, metadata, ids, sections)
    except AbsorbedMapError as exc:
        print(f"[absorbed_map] {exc}", file=sys.stderr)
        return 1
    _write(os.path.join(args.out_dir, "absorbed_sections.json"), absorbed)
    _write(os.path.join(args.out_dir, "section_inventory.json"), inventory)
    print(
        f"[absorbed_map] chunks {absorbed['chunk_count']}, mapped chunks {len(absorbed['map'])}, "
        f"aliases {len(inventory['aliases'])}, sections {len(inventory['sections'])}, "
        f"map_sha256 {absorbed['map_sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
