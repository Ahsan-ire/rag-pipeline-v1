"""Build the synthetic sample index into ``./sample_chroma_db/`` (Phase 11 / D40).

This is the fresh-clone demonstrator: it turns the wholly synthetic corpus in
:mod:`scripts.sample_corpus` into a real hybrid index (Chroma vectors + a BM25
sidecar) that ``python -m src.pipeline query`` and the offline sample eval can
run against, without ever touching the real, copyrighted corpus.

Two safety properties matter here:

* **It must never write into the real ``./chroma_db/``.** That directory holds
  the full copyrighted corpus text; clobbering it would destroy the local index
  and (per the colleague review) let a demo script contaminate production data.
  The guard below refuses any persist directory that resolves to the real store,
  and it runs *before* any embedding model is loaded so the guard is testable
  offline.
* **It is idempotent.** ``sync_documents`` reconciles the store to exactly the
  corpus's chunks, so re-running the build is a no-op (added=updated=deleted=0).
"""

import argparse
import os
import sys
from typing import Optional

# Repo root on sys.path (sys.path[0] is scripts/ under direct execution).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.sample_corpus import SOURCE, build_sample_corpus  # noqa: E402
from src.chunker import chunk_handbook  # noqa: E402
from src.embedder import CHROMA_PERSIST_DIR, sync_documents  # noqa: E402

DEFAULT_SAMPLE_DIR = "./sample_chroma_db"


def _assert_not_real_index(persist_dir: str) -> None:
    """Refuse to build the sample index into the real corpus store.

    Compares real (symlink-resolved) paths and, as a second net, rejects any
    directory whose basename casefolds to ``chroma_db`` — this catches
    ``--persist-dir /some/other/chroma_db`` and macOS case-insensitive aliases
    such as ``CHROMA_DB`` that a plain string compare would miss.
    """
    resolved = os.path.realpath(persist_dir)
    if resolved == os.path.realpath(CHROMA_PERSIST_DIR):
        raise ValueError(
            f"Refusing to build the sample index into the real corpus store "
            f"({CHROMA_PERSIST_DIR!r}). Use {DEFAULT_SAMPLE_DIR!r} (the default) "
            "or another directory."
        )
    if os.path.basename(resolved).casefold() == "chroma_db":
        raise ValueError(
            f"Refusing to build the sample index into a directory named "
            f"'chroma_db' ({persist_dir!r}); that name is reserved for the real "
            "corpus store. Pick a different directory."
        )


def build_sample_index(
    persist_dir: str = DEFAULT_SAMPLE_DIR,
    vector_store: Optional[object] = None,
) -> dict:
    """Chunk the synthetic corpus and sync it into ``persist_dir``.

    Args:
        persist_dir: Where to persist the sample index. Guarded against the real
            ``./chroma_db/``.
        vector_store: Optional explicit Chroma store, forwarded to
            ``sync_documents``. Tests inject one over ``FakeEmbeddings`` so the
            build runs with no model download; production passes ``None`` and the
            real MiniLM store is built from ``persist_dir``.

    Returns:
        The ``sync_documents`` counts, ``{"added", "updated", "deleted"}``.
    """
    # Guard FIRST — before build_sample_corpus/chunk_handbook/sync_documents can
    # construct a store or load an embedding model.
    _assert_not_real_index(persist_dir)

    clean_text, page_map, metadata = build_sample_corpus()
    chunks = chunk_handbook(clean_text, page_map, metadata)
    counts = sync_documents(
        SOURCE,
        chunks,
        vector_store=vector_store,
        persist_directory=persist_dir,
    )
    return counts | {"chunks": len(chunks)}


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "Build the synthetic sample index into a local Chroma store "
            "(never the real ./chroma_db)."
        )
    )
    parser.add_argument(
        "--persist-dir",
        "--persist_directory",
        dest="persist_dir",
        default=DEFAULT_SAMPLE_DIR,
        help=f"Where to persist the sample index (default: {DEFAULT_SAMPLE_DIR}).",
    )
    args = parser.parse_args(argv)

    try:
        counts = build_sample_index(args.persist_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Sample index built at {args.persist_dir}")
    print(
        f"  chunks: {counts['chunks']}  "
        f"(added={counts['added']} updated={counts['updated']} "
        f"deleted={counts['deleted']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
