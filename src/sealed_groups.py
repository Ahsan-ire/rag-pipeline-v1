"""The analysis units, the floor and the excluded families of a sealed epoch (16A-2b b1a r5, D74).

Three small pure computations that decide what the sealed analysis is made of:

* :func:`group_ids`: the group of every sealed family (the cluster the confirmatory test treats as one unit);
* :func:`below_floor`: whether the families left after exclusion are fewer than the floor (the epoch's D64 stop);
* :func:`excluded_member_families`: which families the ledger's exclusion records take out of the analysis.

They were in ``src.sealed_ledger`` (records, digests, locks and the slot state machine), which the r5 incumbent identity left
out; since r6 the ledger is hashed too, and they stay here so that the scorer's identity (``src.scoring_identity``) can cover
the analysis units without covering the ledger. Editing any of them changes ``pipeline_config_hash`` and
``scorer_identity``. ``src.sealed_ledger`` imports them and re-exports their old names (``group_ids``, ``below_floor``,
``SealedLedger.below_floor``, ``SealedLedger.excluded_families``), so no caller changes and the behaviour is the one they had.

This module is deliberately a sibling of ``src.sealed_results`` and imports nothing but the standard library:
``src.sealed_ledger`` is imported by the launcher, which must not load the model clients (importing them runs
``load_dotenv()``), and ``src.sealed_results`` reaches them through ``src.sealed_report``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Sequence, Tuple

#: A family id as the ledger stores it in an exclusion's ``members`` (the same pattern as ``sealed_ledger._ID``;
#: a test holds the two equal).
_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _count(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def below_floor(eligible_after_exclusion: int, floor: int) -> bool:
    """True when the eligible answerable families fell below the floor.

    That is the epoch end (D64 stop): no top-up after freeze. Exactly at the
    floor is still enough.
    """
    return _count(eligible_after_exclusion, "eligible_after_exclusion") < _count(floor, "floor")


def group_ids(families: Iterable[str], edges: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Family -> group id: the connected components of ``edges``, each named by its smallest member family.

    This is the one definition of a sealed group id (see the module docstring):
    ``sealed_tool`` (``exclude``, ``freeze``, ``power``) and the slot gate both
    call it with the frozen sealed families and the twins record's edges. An
    edge endpoint outside ``families`` (a draft that was rejected or dropped)
    keeps its connectivity but is never a member, and a family with no edge is
    a group of its own.
    """
    members = set(families)
    parent: Dict[str, str] = {f: f for f in members}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(edges):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    smallest: Dict[str, str] = {}
    for f in sorted(members):
        smallest.setdefault(find(f), f)
    return {f: smallest[find(f)] for f in members}


def excluded_member_families(exclusion_records: Iterable[Mapping[str, Any]]) -> FrozenSet[str]:
    """Every family named by the ``exclusion`` records: each record's ``members`` (a legacy record: its group id).

    The slot gate maps these families onto the freeze-bound groups (``src.sealed_results.resolve_excluded_groups``), so
    it never depends on a group *name* that a later rejection may have changed.

    Raises:
        ValueError: a record's ``members`` is not a non-empty list of family ids (the ledger reports it as corrupt).
    """
    out = set()
    for rec in exclusion_records:
        members = rec.get("members", [rec["group_id"]])
        if not (
            isinstance(members, list) and members
            and all(isinstance(m, str) and _ID.fullmatch(m) for m in members)
        ):
            raise ValueError(f"record {rec['seq']}: bad exclusion members")
        out.update(members)
    return frozenset(out)

