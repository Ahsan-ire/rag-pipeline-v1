"""Shared helpers for tests that need recorded development draws (not a test module; 16A-2b b1a r6).

Since r6 a batch declaration enforces the draw coverage unconditionally and every ``dev_draw`` record carries an execution
receipt (the run id), so tests that declare a batch record the draws first, through these helpers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence


def record_draws(ledger: Any, *config_hashes: str, n: int = 5, start: int = 0) -> None:
    """``n`` development draws recorded in the ledger under each configuration hash. Each draw has its own artifact and
    rows-sidecar digests and its own execution receipt, as a real draw has."""
    for config in config_hashes:
        for i in range(start, start + n):
            ledger.record_dev_draw(
                config, hashlib.sha256(f"artifact:{config}:{i}".encode()).hexdigest(),
                hashlib.sha256(f"{config}:{i}".encode()).hexdigest(), f"run-{config[:8]}-{i}",
            )


def declare(ledger: Any, entries: Sequence[Mapping[str, str]], *, draws: int = 5) -> str:
    """``record_batch_declaration`` with the coverage satisfied: each runnable entry's configuration has ``draws`` recorded
    draws (an abandoned one has one), recorded first unless it has them already."""
    for entry in entries:
        have = len(ledger.dev_draws(entry["config_hash"]))
        want = 1 if entry["expansion"] == "abandoned" else draws
        if have < want:
            record_draws(ledger, entry["config_hash"], n=want - have, start=have)
    return ledger.record_batch_declaration(entries, draws_required=draws)
