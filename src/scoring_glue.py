"""The evaluator's scoring-facing glue, in a module the scorer's identity covers (16A-2b b1a r7, T3; D74).

``src.scoring_identity.scorer_identity`` is the value two results are compared on (a candidate's sealed slot against slot 0's
canonical run; a candidate's development draws against the incumbent's), so every piece of code that decides a hit, a rank, a
roster or a denominator must be inside it. r6 covered the scoring modules, but five small things that decide what the scorer
is handed or what a rank becomes still lived in ``src.evaluator`` (which is not a scoring module: it orchestrates, and one edit
there would otherwise change a rank with the identity unmoved). They live here now and ``src.evaluator`` imports them:

* :data:`HIT_KS` and :func:`hit_cutoffs`: the hit cut-offs reported for a run (``k <= top_k``) and the one headline cut-off ``k``;
* :data:`ABSORBED_MAP_PATH` and :func:`load_absorbed_map`: the committed absorbed-section map whose aliases count as hits at their
  chunk's rank (the file's bytes are in the identity's inputs; how its content is read into the mapping is code, and is here);
* :func:`sidecar_rows`: the projection of a mode's per-row scores into the rows sidecar (``strict_rank``, ``related_rank`` and the
  ``completion_rank`` that ``scripts/controls.py`` and the bake-off comparison read);
* :func:`retrieval_family_count`: how many families are retrieval-eligible (none of a family's rows is a refusal), the same
  predicate ``eval_v6.family_rates`` applies.

Nothing here makes a call, loads a model or reads anything but the absorbed map. ``tests`` point ``ABSORBED_MAP_PATH`` through
:data:`REPO_ROOT` at a copy.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The retrieval cut-offs reported by ``evaluate_retrieval`` and the v6 scorer: hit@1, hit@3, hit@6. Only the ks with
#: ``k <= top_k`` are reported (a hit@6 cell is meaningless when only 3 chunks were retrieved), so the effective ks are derived
#: per run (:func:`hit_cutoffs`). The headline metric is strict hit@6 on the sealed set.
HIT_KS = (1, 3, 6)

#: The committed absorbed-section map, relative to :data:`REPO_ROOT` (item 7): aliases that count as hits at their chunk's rank.
ABSORBED_MAP_PATH = os.path.join("eval", "absorbed_sections.json")

#: The repository root the map is read from.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hit_cutoffs(top_k: int) -> Tuple[List[int], int]:
    """``(ks, k)`` for a run at ``top_k``: the reported cut-offs ``[k for k in HIT_KS if k <= top_k]`` and the largest of them.

    ``k`` is ``top_k`` itself when no cut-off fits (``top_k`` below the smallest). ``ks`` keeps :data:`HIT_KS` order.
    """
    ks = [k for k in HIT_KS if k <= top_k]
    return ks, (max(ks) if ks else top_k)


def load_absorbed_map() -> Tuple[Optional[Dict[str, List[str]]], Optional[str]]:
    """The committed absorbed-section map (item 7) and its sha256, or ``(None, None)`` when the file is absent.

    The mapping is the document's ``map`` object (an empty one when it has none); the digest is of the file's bytes.
    """
    path = os.path.join(REPO_ROOT, ABSORBED_MAP_PATH)
    if not os.path.isfile(path):
        return None, None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return dict(doc.get("map") or {}), digest.hexdigest()


def sidecar_rows(set_sha256: str, mode: str, scored: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The rows-sidecar rows of one mode of one set: ``{set_sha256, id, mode, strict_rank, related_rank, completion_rank}``.

    ``scored`` is ``eval_v6.score_retrieval_v6``'s ``{row id: scores}``. ``completion_rank`` is the strict completion rank (the
    rank at which every evidence group is first covered), the value ``scripts/controls.py`` reads.
    """
    return [
        {
            "set_sha256": set_sha256, "id": rid, "mode": mode,
            "strict_rank": score["strict_rank"], "related_rank": score["related_rank"],
            "completion_rank": score["strict_rank"],
        }
        for rid, score in scored.items()
    ]


def retrieval_family_count(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many families are retrieval-eligible: those none of whose rows has scope ``refuse`` (the schema gives a family one scope)."""
    families = {r["family_id"] for r in rows}
    refusing = {r["family_id"] for r in rows if r["scope"] == "refuse"}
    return len(families - refusing)
