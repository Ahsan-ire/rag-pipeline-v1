"""Operational audit event log for the RAG pipeline.

This is an **operational event log** — a plain append-only JSONL file — not
a tamper-evident audit trail. There is no cryptographic chaining, signing,
or integrity check: anyone with filesystem access can edit or delete a
line. Its purpose is to let an operator reconstruct what the pipeline did
for a given query (what was retrieved, which grounding gate outcome fired,
what the CLI showed the user) without persisting content that must not be
logged:

- Answer text and chunk text are NEVER recorded, in any form, anywhere in
  the record. The corpus is a copyrighted, unpublished handbook; logging
  its text into an operational log file would recreate the exact copyright
  exposure the rest of the pipeline is careful to avoid.
- The raw question text is hashed (SHA-256) and only its length is kept, by
  default, because legal queries can themselves reveal a client's matter
  (e.g. "does the lease at 14 Oak Grove permit assignment"). Set the
  environment variable ``AUDIT_LOG_RAW_QUERIES=1`` to opt into logging the
  raw question text (local debugging only) — this is read at call time,
  not import time, so it can be toggled per-process without reimporting.
"""

import functools
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_LOG_PATH = Path("logs/audit_log.jsonl")

# Action vocabulary: what pipeline.query actually did with the answer. Owned
# here, next to the record schema, as constants — a typo at a call site would
# otherwise mint a new bucket that Phase 10's outcome-distribution metric
# silently miscounts (the sibling gate-outcome vocabulary is owned the same
# way in src/grounding.py).
ACTION_SHOWN = "shown"
ACTION_SHOWN_WITH_WARNING = "shown_with_warning"
ACTION_BLOCKED_UNVERIFIED = "blocked_unverified"
ACTION_REFUSAL_SHOWN = "refusal_shown"
ACTION_SHOWN_UNVERIFIED_OVERRIDE = "shown_unverified_override"
ACTION_NO_RESULTS = "no_results"


@functools.lru_cache(maxsize=1)
def _git_sha() -> Optional[str]:
    """Best-effort short git SHA of HEAD, or ``None`` on any failure.

    Factored out of ``build_event`` so tests can patch it directly instead
    of shelling out to git. Deliberately swallows everything (missing git
    binary, not-a-repo, timeout, non-zero exit): this value is diagnostic
    metadata for an operational log, never something a caller should have
    to handle a raised exception for. Cached for the process lifetime — the
    SHA cannot change mid-process, and a per-query subprocess spawn is pure
    waste in any future batch caller (same convention as the cached
    ``get_embedding_function``).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None
    except Exception:
        return None


def build_event(
    *,
    question: str,
    top_k: int,
    document_type: Optional[str],
    results: List[Dict[str, Any]],
    gate_outcome: Optional[str],
    action: str,
    citation_check: Dict[str, List[Dict[str, str]]],
    citations: List[Dict[str, str]],
    answer: str,
) -> Dict[str, Any]:
    """Build one audit record for a single query/answer cycle.

    Args:
        question: The raw user question. Never stored verbatim unless
            ``AUDIT_LOG_RAW_QUERIES=1`` — see module docstring.
        top_k: Number of chunks requested from the retriever.
        document_type: The document-type filter applied to retrieval, if any.
        results: Retriever result dicts, each ``{"document", "score",
            "metadata"}`` (``document`` is a ``langchain_core.documents.Document``).
        gate_outcome: The grounding-gate classification for this answer
            (e.g. ``CITATIONS_VERIFIED``), or ``None`` if the gate did not run.
        action: What the caller actually did with the answer — one of
            ``shown`` / ``shown_with_warning`` / ``blocked_unverified`` /
            ``refusal_shown`` / ``shown_unverified_override`` / ``no_results``.
        citation_check: ``{"grounded": [...], "ungrounded": [...]}`` citation
            dicts, as produced by the generator's citation validation.
        citations: All extracted citation dicts (each with a ``"raw"`` display
            string), grounded and ungrounded together.
        answer: The generated answer text. Used only for its length —
            never stored (see module docstring).

    Returns:
        A JSON-serializable dict with exactly the keys documented in the
        module's Phase 8 contract. Answer text and chunk text are excluded
        always; raw query text is excluded unless opted in via env var.

    This function never raises: git SHA resolution failures degrade to
    ``None`` (both inside ``_git_sha`` itself, and again here in case a
    caller has patched ``_git_sha`` to something that raises), and a
    missing per-chunk document id falls back to the same content-hash id
    the vector store itself uses (``compute_chunk_id``).
    """
    # Lazy imports: embedder pulls chromadb + sentence-transformers and
    # generator pulls the Anthropic client — a lightweight consumer of this
    # module (a log-replay script, a bare build_event test) shouldn't pay for
    # the full stack. compute_chunk_id is only a fallback: the retriever
    # attaches .id to every Document it returns, so re-hashing here means an
    # id-less Document came from somewhere else.
    from src.embedder import compute_chunk_id
    from src.generator import GENERATION_MODEL

    retrieved: List[Dict[str, Any]] = []
    for r in results:
        doc = r["document"]
        doc_id = doc.id if getattr(doc, "id", None) else compute_chunk_id(doc.page_content)
        metadata = doc.metadata or {}
        retrieved.append(
            {
                "id": doc_id,
                "section_number": metadata.get("section_number"),
                "page_start": metadata.get("page_start"),
                "page_end": metadata.get("page_end"),
                "score": r["score"],
            }
        )

    try:
        git_sha = _git_sha()
    except Exception:
        git_sha = None

    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha,
        "query_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "query_chars": len(question),
        "top_k": top_k,
        "document_type": document_type,
        "retrieved": retrieved,
        "gate_outcome": gate_outcome,
        "action": action,
        "verified_count": len(citation_check["grounded"]),
        "unverified_count": len(citation_check["ungrounded"]),
        "citation_locators": [c["raw"] for c in citations],
        "generation_model": GENERATION_MODEL,
        "answer_chars": len(answer),
    }

    if os.environ.get("AUDIT_LOG_RAW_QUERIES") == "1":
        record["query_text"] = question

    return record


def log_event(record: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append one JSON record as a single line to the audit log.

    Resolves the target path as: the explicit ``path`` argument, else the
    ``AUDIT_LOG_PATH`` environment variable, else ``DEFAULT_LOG_PATH``.
    Creates parent directories if needed. Deliberately dumb: no log
    rotation, no file locking, no concurrent-writer coordination — this is
    an operational log, not a production audit trail (see module docstring).
    """
    if path is None:
        env_path = os.environ.get("AUDIT_LOG_PATH")
        path = Path(env_path) if env_path else DEFAULT_LOG_PATH

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
