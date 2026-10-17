"""Shared helpers for tests that need recorded development draws (not a test module; 16A-2b b1a r6, r7).

Since r6 a batch declaration enforces the draw coverage unconditionally and every ``dev_draw`` record carries an execution
receipt (the run id). Since r7 every record also carries the draw's ``mode``, the sidecar's scorer identity and the rewrite
identity of its expansions, and ``record_batch_declaration`` binds the batch entries to them: the counted draws of a ``replay``
entry are replay draws of the incumbent's same-index draws, those of a ``candidate`` entry are build draws under its own
rewrite identity, and all carry the incumbent's scorer identity. Tests that declare a batch record the draws first, through
these helpers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional, Sequence

SCORER = "5" * 64  # the scorer identity the tests' draws carry (one scorer for the incumbent and every candidate)
REWRITE = "6" * 64  # the rewrite identity of the incumbent's draws (a replay draw replays its artifact, so shares it)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def draw_ids(scorer: str = SCORER, rewrite: str = REWRITE) -> dict:
    """The keyword arguments every ``record_dev_draw`` call needs besides the config, digests and receipt."""
    return {"scorer_identity": scorer, "rewrite_config_hash": rewrite}


def record_draws(
    ledger: Any, *config_hashes: str, n: int = 5, start: Optional[int] = None, mode: str = "build",
    rewrite: str = REWRITE, scorer: str = SCORER,
) -> None:
    """``n`` development draws recorded in the ledger under each configuration hash. Each draw has its own artifact and
    rows-sidecar digests and its own execution receipt, as a real draw has; numbering continues from the configuration's
    recorded draws unless ``start`` is given.

    ``mode="replay"`` records replay draws: draw ``i`` replays the incumbent's draw ``i`` (the controls record names the
    incumbent; its missing draws up to ``i`` are recorded first), with the incumbent's artifact.
    """
    for config in config_hashes:
        first = len(ledger.dev_draws(config)) if start is None else start
        for i in range(first, first + n):
            kwargs: dict = {"scorer_identity": scorer}
            if mode == "replay":
                incumbent = ledger.incumbent_config_hash()
                if len(ledger.dev_draws(incumbent)) <= i:
                    record_draws(ledger, incumbent, n=i + 1 - len(ledger.dev_draws(incumbent)), scorer=scorer)
                artifact = ledger.dev_draws(incumbent)[i]["artifact_sha256"]
                kwargs.update(replay_of=i + 1, rewrite_config_hash=REWRITE)
            else:
                artifact = sha(f"artifact:{config}:{i}")
                kwargs.update(rewrite_config_hash=rewrite)
            ledger.record_dev_draw(config, artifact, sha(f"{config}:{i}"), f"run-{config[:8]}-{i}", **kwargs)


def declare(ledger: Any, entries: Sequence[Mapping[str, str]], *, draws: int = 5) -> str:
    """``record_batch_declaration`` with the coverage and the binding satisfied: each runnable entry's configuration has
    ``draws`` recorded draws (an abandoned one has one), recorded first unless it has them already, in the mode its entry says
    (``replay``: replay draws of the incumbent's; ``candidate``: build draws under the entry's own rewrite identity; ``abandoned``:
    a build draw), and the incumbent has the ``draws`` draws the comparison needs."""
    incumbent = ledger.incumbent_config_hash()
    if incumbent is not None and len(ledger.dev_draws(incumbent)) < draws:
        record_draws(ledger, incumbent, n=draws - len(ledger.dev_draws(incumbent)))
    for entry in entries:
        have = len(ledger.dev_draws(entry["config_hash"]))
        want = 1 if entry["expansion"] == "abandoned" else draws
        if have < want:
            if entry["expansion"] == "replay":
                record_draws(ledger, entry["config_hash"], n=want - have, mode="replay")
            else:
                record_draws(
                    ledger, entry["config_hash"], n=want - have, rewrite=entry.get("rewrite_config_hash", REWRITE),
                )
    return ledger.record_batch_declaration(entries, draws_required=draws)
