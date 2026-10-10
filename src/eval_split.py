"""Deterministic family split with twin units, quotas, strata, band and min_share.

Phase 16A-1 item 4 (D67): a pure, data-free building block for 16A-2. It takes
content-free family records and returns ``{family_id: split}``. Stdlib only; no
file IO, no network. Every input problem is refused with ``SplitInputError``
(a ``ValueError``) whose message names keys and ids, never record values beyond
them; every unsatisfiable constraint set raises ``SplitInfeasible`` carrying
per-constraint counts, and nothing is returned.

Shapes (pinned here because the spec leaves them open)
------------------------------------------------------
**Family record** — a mapping with EXACTLY these keys (any other key is refused;
content-bearing keys such as ``question``, ``evidence``, ``gaps`` or ``sealed``
get a dedicated "content-bearing" refusal)::

    {"family_id": "f001",          # non-empty str, unique across the pool
     "scope": "answer",            # "answer" | "partial" | "refuse"
     "chapter": "3",               # primary chapter: a chapter string, "multi",
                                   # or "refuse" (required iff scope == "refuse")
     "source": "drafted",          # non-empty str (used by min_share)
     "register": "lay",            # non-empty str (validated, not constrained)
     "eligible": ["dev", "test"]}  # list/tuple of split names, each a quotas key,
                                   # no duplicates (may be empty -> infeasible)

**Stratum key** — ``"<scope>|<chapter>"``, e.g. ``"answer|3"``,
``"partial|multi"``, ``"refuse|refuse"`` (``stratum_of`` computes it).

**quotas** — ``{split: exact final family count}`` (int >= 0). Every supplied
family is placed, so the quotas must sum to the number of families; a mismatch
is ``SplitInfeasible`` (constraint ``quota_total``).

**strata** — per-split per-stratum MINIMUM counts:
``{split: {stratum_key: min_count}}`` (absolute ints >= 0, not proportions).
Unlisted strata have no minimum. Unknown split names are refused.

**band** — fixed by the spec, not a parameter: the number of ``refuse``-scope
families in each split's FINAL families (preassigned included) must lie in
[25%, 30%] of that split's quota, evaluated exactly in integers
(``4*r >= q`` and ``10*r <= 3*q``).

**min_share** — ``{split: {source: minimum share in [0, 1]}}``: the split's
families from that source must be at least ``ceil(share * quota)``. Shares are
converted with ``Fraction(repr(x))`` for floats so ``0.1 * 30`` needs 3, not 4.

**preassigned** — a mapping ``{family_id: split}`` or an iterable of
``(family_id, split)`` pairs. Ids must be supplied families (else refused),
splits must be quotas keys (else refused), a repeated id is refused. A
preassigned family never moves and counts toward its split's quota, strata,
band and min_share. Preassigning to a split the family (or its unit) is not
eligible for is ``SplitInfeasible`` (constraint ``preassigned_ineligible``);
two different preassigned splits inside one twin unit is ``SplitInfeasible``
(constraint ``preassignment_conflicts``).

**twins** — a mapping with EXACTLY these keys::

    {"batches": {batch_id: [family_id, ...], ...},
     "families_digest": "<sha256 hex>",
     "pairs_covered": [[batch_a, batch_b], ...],
     "edges": [[family_a, family_b], ...]}

* ``batches`` partitions the supplied families: every supplied family
  (preassigned included) appears in exactly one batch. An empty ``batches``,
  an empty batch, an unknown id, an omitted family or a duplicate (within or
  across batches) is refused.
* ``families_digest`` must equal ``families_digest(supplied_ids, batch_ids)``
  (recomputed here); a mismatch is refused.
* ``pairs_covered`` must contain every unordered batch pair, self-pairs
  included, i.e. exactly ``n*(n+1)/2`` distinct pairs; a missing pair, an
  unknown batch id, a malformed pair or a repeated pair (in either order) is
  refused. This attests that the twin dedup compared every batch against every
  batch (itself included); producing it is 16A-2's job (P2).
* ``edges`` endpoints must be supplied families (else refused). A self-edge is
  allowed and has no effect; repeated edges are harmless.

Digest canonical serialisation
------------------------------
``families_digest(family_ids, batch_ids)`` =
``sha256(json.dumps({"batch_ids": sorted(batch_ids), "family_ids":
sorted(family_ids)}, sort_keys=True, separators=(",", ":"),
ensure_ascii=True).encode("utf-8")).hexdigest()`` — lowercase hex.

Algorithm
---------
1. Validate everything above (refusals before any search).
2. Union-find over the edges (families visited in ``family_id`` order) forms
   units. A unit's allowed splits = the intersection of its members'
   ``eligible`` lists, narrowed to the pinned split when any member is
   preassigned.
3. Static necessary conditions are counted per constraint (``counts``); any
   violation raises ``SplitInfeasible`` without searching.
4. Pinned units are placed. Free units are sorted by their smallest
   ``family_id``, then shuffled with ``random.Random(seed)``; each unit also
   gets a seeded split preference order from the same generator. Input order
   therefore never matters (permutation invariance), only ``seed`` does.
5. Iterative depth-first search assigns units. At each step it takes the
   unplaced unit with the fewest viable splits (minimum remaining values),
   ties broken by the seeded shuffle position, so the shuffle fixes the order
   among equally constrained units; a unit with no viable split is an
   immediate dead end. Candidate splits
   are those the unit is allowed in with room for it (and no band ceiling
   overrun), tried first where the
   unit closes a positive deficit (band floor, stratum or source minimum),
   then in the unit's seeded preference order. After every placement the
   affected splits are checked for necessary feasibility (quota room, band
   floor/ceiling for both refuse and non-refuse families, each stratum/source
   deficit against what is still placeable there, and summed deficits against
   remaining room), plus an aggregate check that the unplaced refuse and
   non-refuse families fit all splits' band floors and ceilings together; a
   failure backtracks.
6. A complete assignment is re-verified by an independent checker before it
   is returned.

Incompleteness: the search is bounded by ``SEARCH_NODE_BUDGET`` placements. If
the budget runs out the call raises ``SplitInfeasible`` (``search.exhausted``
true) even though a split might exist; it never returns a partial or
unverified split. ``register`` is validated but not constrained (the spec names
no register constraint).
"""

from __future__ import annotations

import hashlib
import json
import random
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

SCOPES = ("answer", "partial", "refuse")
RECORD_KEYS = frozenset({"family_id", "scope", "chapter", "source", "register", "eligible"})
CONTENT_KEYS = frozenset(
    {
        "question",
        "questions",
        "phrasing",
        "phrasings",
        "evidence",
        "gap",
        "gaps",
        "sealed",
        "text",
        "answer",
        "keywords",
        "groups",
    }
)
TWINS_KEYS = frozenset({"batches", "families_digest", "pairs_covered", "edges"})
BAND_MIN = Fraction(1, 4)
BAND_MAX = Fraction(3, 10)
SEARCH_NODE_BUDGET = 200_000

Preassigned = Union[Mapping[str, str], Iterable[Tuple[str, str]]]


class SplitInputError(ValueError):
    """A refused input: malformed, content-bearing or inconsistent records/twins."""


class SplitInfeasible(Exception):
    """No split satisfies the constraints; ``counts`` holds per-constraint counts.

    ``counts`` is a dict keyed by constraint name (``quota_total``,
    ``eligibility``, ``band``, ``strata``, ``min_share``,
    ``preassignment_conflicts``, ``preassigned_ineligible``,
    ``unit_no_allowed_split``, ``search``); each entry carries the required and
    available numbers and a ``violated`` flag. It never contains record
    content.
    """

    def __init__(self, message: str, counts: Dict[str, Any]) -> None:
        super().__init__(message)
        self.counts = counts


def families_digest(family_ids: Iterable[str], batch_ids: Iterable[str]) -> str:
    """Return the canonical sha256 hex digest of sorted family ids and batch ids.

    See the module docstring for the exact serialisation.
    """
    payload = {"batch_ids": sorted(batch_ids), "family_ids": sorted(family_ids)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stratum_of(record: Mapping[str, Any]) -> str:
    """Return the stratum key ``"<scope>|<chapter>"`` of a family record."""
    return f"{record['scope']}|{record['chapter']}"


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_stratum_key(key: Any, where: str) -> None:
    if not isinstance(key, str) or key.count("|") != 1:
        raise SplitInputError(f"{where}: stratum key must be 'scope|chapter'")
    scope, chapter = key.split("|")
    if scope not in SCOPES or chapter == "":
        raise SplitInputError(f"{where}: stratum key {key!r} is not a valid scope|chapter")
    if (scope == "refuse") != (chapter == "refuse"):
        raise SplitInputError(f"{where}: stratum key {key!r}: chapter 'refuse' iff scope 'refuse'")


def _validate_quotas(quotas: Any) -> Dict[str, int]:
    if not isinstance(quotas, Mapping) or not quotas:
        raise SplitInputError("quotas must be a non-empty mapping {split: count}")
    out: Dict[str, int] = {}
    for split, q in quotas.items():
        if not _is_nonempty_str(split):
            raise SplitInputError("quotas: split names must be non-empty strings")
        if not _is_count(q):
            raise SplitInputError(f"quotas[{split!r}] must be an int >= 0")
        out[split] = q
    return out


def _validate_records(families: Any, splits: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if isinstance(families, (str, bytes, Mapping)) or not isinstance(families, Iterable):
        raise SplitInputError("families must be a sequence of records")
    records: Dict[str, Dict[str, Any]] = {}
    for idx, rec in enumerate(families):
        where = f"families[{idx}]"
        if not isinstance(rec, Mapping):
            raise SplitInputError(f"{where}: record must be a mapping")
        keys = set(rec.keys())
        content = sorted(k for k in keys if k in CONTENT_KEYS)
        if content:
            raise SplitInputError(f"{where}: content-bearing field(s) refused: {content}")
        extra = sorted(str(k) for k in keys - RECORD_KEYS)
        if extra:
            raise SplitInputError(f"{where}: unknown field(s) refused: {extra}")
        missing = sorted(RECORD_KEYS - keys)
        if missing:
            raise SplitInputError(f"{where}: missing field(s): {missing}")
        fid = rec["family_id"]
        if not _is_nonempty_str(fid):
            raise SplitInputError(f"{where}: family_id must be a non-empty string")
        if fid in records:
            raise SplitInputError(f"{where}: duplicate family_id {fid!r}")
        scope = rec["scope"]
        if scope not in SCOPES:
            raise SplitInputError(f"{where}: scope must be one of {list(SCOPES)}")
        chapter = rec["chapter"]
        if not _is_nonempty_str(chapter):
            raise SplitInputError(f"{where}: chapter must be a non-empty string")
        if (scope == "refuse") != (chapter == "refuse"):
            raise SplitInputError(f"{where}: chapter 'refuse' iff scope 'refuse'")
        for field in ("source", "register"):
            if not _is_nonempty_str(rec[field]):
                raise SplitInputError(f"{where}: {field} must be a non-empty string")
        elig = rec["eligible"]
        if isinstance(elig, (str, bytes)) or not isinstance(elig, (list, tuple)):
            raise SplitInputError(f"{where}: eligible must be a list of split names")
        if len(set(elig)) != len(elig):
            raise SplitInputError(f"{where}: eligible has duplicate split names")
        for s in elig:
            if s not in splits:
                raise SplitInputError(f"{where}: eligible names unknown split {s!r}")
        records[fid] = {
            "family_id": fid,
            "scope": scope,
            "chapter": chapter,
            "source": rec["source"],
            "register": rec["register"],
            "eligible": frozenset(elig),
        }
    if not records:
        raise SplitInputError("families is empty")
    return records


def _pair(value: Any, where: str) -> Tuple[Any, Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SplitInputError(f"{where}: must be a 2-element pair")
    return value[0], value[1]


def _validate_twins(twins: Any, ids: Sequence[str]) -> List[Tuple[str, str]]:
    if not isinstance(twins, Mapping):
        raise SplitInputError("twins must be a mapping")
    keys = set(twins.keys())
    if keys != TWINS_KEYS:
        raise SplitInputError(
            f"twins keys must be exactly {sorted(TWINS_KEYS)}; "
            f"missing {sorted(TWINS_KEYS - keys)}, unknown {sorted(str(k) for k in keys - TWINS_KEYS)}"
        )
    id_set = set(ids)
    batches = twins["batches"]
    if not isinstance(batches, Mapping) or not batches:
        raise SplitInputError("twins.batches must be a non-empty mapping")
    seen: Dict[str, str] = {}
    for bid, members in batches.items():
        if not _is_nonempty_str(bid):
            raise SplitInputError("twins.batches: batch ids must be non-empty strings")
        if isinstance(members, (str, bytes)) or not isinstance(members, (list, tuple)):
            raise SplitInputError(f"twins.batches[{bid!r}] must be a list of family ids")
        if not members:
            raise SplitInputError(f"twins.batches[{bid!r}] is empty")
        for fid in members:
            if fid not in id_set:
                raise SplitInputError(f"twins.batches[{bid!r}]: unknown family id {fid!r}")
            if fid in seen:
                raise SplitInputError(
                    f"twins.batches: family {fid!r} appears more than once "
                    f"(batches {seen[fid]!r} and {bid!r})"
                )
            seen[fid] = bid
    omitted = sorted(id_set - set(seen))
    if omitted:
        raise SplitInputError(f"twins.batches omits {len(omitted)} supplied family id(s): {omitted}")

    expected = families_digest(ids, batches.keys())
    if twins["families_digest"] != expected:
        raise SplitInputError("twins.families_digest does not match the recomputed digest")

    batch_ids = set(batches.keys())
    pairs = twins["pairs_covered"]
    if isinstance(pairs, (str, bytes)) or not isinstance(pairs, (list, tuple)):
        raise SplitInputError("twins.pairs_covered must be a list of batch pairs")
    covered = set()
    for i, p in enumerate(pairs):
        a, b = _pair(p, f"twins.pairs_covered[{i}]")
        if a not in batch_ids or b not in batch_ids:
            raise SplitInputError(f"twins.pairs_covered[{i}] names an unknown batch id")
        key = (a, b) if a <= b else (b, a)
        if key in covered:
            raise SplitInputError(f"twins.pairs_covered[{i}] repeats pair {list(key)}")
        covered.add(key)
    ordered = sorted(batch_ids)
    missing = [
        [ordered[i], ordered[j]]
        for i in range(len(ordered))
        for j in range(i, len(ordered))
        if (ordered[i], ordered[j]) not in covered
    ]
    if missing:
        raise SplitInputError(f"twins.pairs_covered is missing {len(missing)} batch pair(s): {missing}")

    edges = twins["edges"]
    if isinstance(edges, (str, bytes)) or not isinstance(edges, (list, tuple)):
        raise SplitInputError("twins.edges must be a list of family-id pairs")
    out: List[Tuple[str, str]] = []
    for i, e in enumerate(edges):
        a, b = _pair(e, f"twins.edges[{i}]")
        for end in (a, b):
            if end not in id_set:
                raise SplitInputError(f"twins.edges[{i}]: unknown family id endpoint {end!r}")
        out.append((a, b))
    return out


def _validate_preassigned(pre: Any, ids: Sequence[str], splits: Sequence[str]) -> Dict[str, str]:
    if isinstance(pre, (str, bytes)):
        raise SplitInputError("preassigned must be a mapping or (family_id, split) pairs")
    items: Iterable[Any] = pre.items() if isinstance(pre, Mapping) else pre
    id_set = set(ids)
    out: Dict[str, str] = {}
    for i, item in enumerate(items):
        fid, split = _pair(item, f"preassigned[{i}]")
        if fid not in id_set:
            raise SplitInputError(f"preassigned[{i}]: unknown family id {fid!r}")
        if split not in splits:
            raise SplitInputError(f"preassigned[{i}]: unknown split {split!r}")
        if fid in out:
            raise SplitInputError(f"preassigned[{i}]: family {fid!r} preassigned more than once")
        out[fid] = split
    return out


def _to_fraction(x: Any, where: str) -> Fraction:
    if isinstance(x, bool) or not isinstance(x, (int, float, Fraction)):
        raise SplitInputError(f"{where} must be a number in [0, 1]")
    f = Fraction(repr(x)) if isinstance(x, float) else Fraction(x)
    if f < 0 or f > 1:
        raise SplitInputError(f"{where} must be in [0, 1]")
    return f


def _ceil_frac(f: Fraction) -> int:
    return -((-f.numerator) // f.denominator)


def _validate_strata(strata: Any, splits: Sequence[str]) -> Dict[str, Dict[str, int]]:
    if strata is None:
        return {}
    if not isinstance(strata, Mapping):
        raise SplitInputError("strata must be a mapping {split: {stratum_key: min_count}}")
    out: Dict[str, Dict[str, int]] = {}
    for split, mins in strata.items():
        if split not in splits:
            raise SplitInputError(f"strata names unknown split {split!r}")
        if not isinstance(mins, Mapping):
            raise SplitInputError(f"strata[{split!r}] must be a mapping")
        inner: Dict[str, int] = {}
        for key, m in mins.items():
            _validate_stratum_key(key, f"strata[{split!r}]")
            if not _is_count(m):
                raise SplitInputError(f"strata[{split!r}][{key!r}] must be an int >= 0")
            inner[key] = m
        out[split] = inner
    return out


def _validate_min_share(
    min_share: Any, splits: Sequence[str], quotas: Mapping[str, int]
) -> Dict[str, Dict[str, int]]:
    """Return required absolute counts {split: {source: ceil(share * quota)}}."""
    if min_share is None:
        return {}
    if not isinstance(min_share, Mapping):
        raise SplitInputError("min_share must be a mapping {split: {source: share}}")
    out: Dict[str, Dict[str, int]] = {}
    for split, shares in min_share.items():
        if split not in splits:
            raise SplitInputError(f"min_share names unknown split {split!r}")
        if not isinstance(shares, Mapping):
            raise SplitInputError(f"min_share[{split!r}] must be a mapping")
        inner: Dict[str, int] = {}
        for source, share in shares.items():
            if not _is_nonempty_str(source):
                raise SplitInputError(f"min_share[{split!r}]: source must be a non-empty string")
            f = _to_fraction(share, f"min_share[{split!r}][{source!r}]")
            inner[source] = _ceil_frac(f * quotas[split])
        out[split] = inner
    return out


def _band(q: int) -> Tuple[int, int]:
    """Return (lo, hi) refuse-family counts allowed for quota ``q`` (lo > hi = empty)."""
    return _ceil_frac(BAND_MIN * q), (BAND_MAX * q).numerator // (BAND_MAX * q).denominator


class _Unit:
    __slots__ = ("key", "members", "size", "refuse", "strata", "sources", "allowed", "pin")

    def __init__(self, key: str, members: List[str]) -> None:
        self.key = key
        self.members = members
        self.size = len(members)
        self.refuse = 0
        self.strata: Dict[str, int] = {}
        self.sources: Dict[str, int] = {}
        self.allowed: Tuple[str, ...] = ()
        self.pin: Optional[str] = None


def _build_units(
    records: Mapping[str, Mapping[str, Any]],
    edges: Sequence[Tuple[str, str]],
    pre: Mapping[str, str],
    splits: Sequence[str],
) -> Tuple[List[_Unit], int, int]:
    """Union-find units; return (units, conflicting_units, pinned_ineligible_units)."""
    parent = {fid: fid for fid in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted((min(a, b), max(a, b)) for a, b in edges):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Keep the lexicographically smaller root: deterministic, order-free.
            if rb < ra:
                ra, rb = rb, ra
            parent[rb] = ra

    groups: Dict[str, List[str]] = {}
    for fid in sorted(records):
        groups.setdefault(find(fid), []).append(fid)

    units: List[_Unit] = []
    conflicts = 0
    pin_inelig = 0
    for members in groups.values():
        u = _Unit(members[0], members)
        allowed = set(splits)
        for fid in members:
            rec = records[fid]
            allowed &= rec["eligible"]
            if rec["scope"] == "refuse":
                u.refuse += 1
            sk = stratum_of(rec)
            u.strata[sk] = u.strata.get(sk, 0) + 1
            u.sources[rec["source"]] = u.sources.get(rec["source"], 0) + 1
        pins = {pre[fid] for fid in members if fid in pre}
        if len(pins) > 1:
            conflicts += 1
            allowed = set()
        elif pins:
            (pin,) = pins
            if pin not in allowed:
                pin_inelig += 1
                allowed = set()
            else:
                allowed = {pin}
                u.pin = pin
        u.allowed = tuple(s for s in splits if s in allowed)
        units.append(u)
    return units, conflicts, pin_inelig


def _static_counts(
    units: Sequence[_Unit],
    records: Mapping[str, Mapping[str, Any]],
    splits: Sequence[str],
    quotas: Mapping[str, int],
    strata: Mapping[str, Mapping[str, int]],
    src_req: Mapping[str, Mapping[str, int]],
    conflicts: int,
    pin_inelig: int,
) -> Dict[str, Any]:
    """Compute per-constraint necessary-condition counts (``violated`` flags)."""
    total = len(records)
    qsum = sum(quotas.values())
    counts: Dict[str, Any] = {
        "quota_total": {"families": total, "quota_sum": qsum, "violated": total != qsum},
        "preassignment_conflicts": {"units": conflicts, "violated": conflicts > 0},
        "preassigned_ineligible": {"units": pin_inelig, "violated": pin_inelig > 0},
    }
    no_split = sum(1 for u in units if not u.allowed)
    counts["unit_no_allowed_split"] = {"units": no_split, "violated": no_split > 0}

    elig: Dict[str, Any] = {}
    band: Dict[str, Any] = {}
    strat: Dict[str, Any] = {}
    share: Dict[str, Any] = {}
    for s in splits:
        q = quotas[s]
        avail = [u for u in units if s in u.allowed]
        pinned_here = sum(u.size for u in units if u.pin == s)
        size = sum(u.size for u in avail)
        ref = sum(u.refuse for u in avail)
        elig[s] = {
            "quota": q,
            "eligible_families": size,
            "preassigned": pinned_here,
            "violated": size < q or pinned_here > q,
        }
        lo, hi = _band(q)
        pinned_ref = sum(u.refuse for u in units if u.pin == s)
        pinned_non = pinned_here - pinned_ref
        band[s] = {
            "quota": q,
            "refuse_min": lo,
            "refuse_max": hi,
            "refuse_available": ref,
            "non_refuse_available": size - ref,
            "refuse_preassigned": pinned_ref,
            "violated": lo > hi
            or ref < lo
            or (size - ref) < q - hi
            or pinned_ref > hi
            or pinned_non > q - lo,
        }
        sreq = strata.get(s, {})
        if sreq:
            per: Dict[str, Any] = {}
            for key, m in sorted(sreq.items()):
                a = sum(u.strata.get(key, 0) for u in avail)
                per[key] = {"required": m, "available": a, "violated": a < m}
            tot = sum(sreq.values())
            per["_total"] = {"required": tot, "quota": q, "violated": tot > q}
            strat[s] = per
        oreq = src_req.get(s, {})
        if oreq:
            per = {}
            for src, m in sorted(oreq.items()):
                a = sum(u.sources.get(src, 0) for u in avail)
                per[src] = {"required": m, "available": a, "violated": a < m}
            tot = sum(oreq.values())
            per["_total"] = {"required": tot, "quota": q, "violated": tot > q}
            share[s] = per
    counts["eligibility"] = elig
    counts["band"] = band
    counts["strata"] = strat
    counts["min_share"] = share
    return counts


def _any_violated(node: Any) -> bool:
    if isinstance(node, Mapping):
        if node.get("violated") is True:
            return True
        return any(_any_violated(v) for v in node.values())
    return False


def _verify(
    assignment: Mapping[str, str],
    records: Mapping[str, Mapping[str, Any]],
    units: Sequence[_Unit],
    quotas: Mapping[str, int],
    strata: Mapping[str, Mapping[str, int]],
    src_req: Mapping[str, Mapping[str, int]],
    pre: Mapping[str, str],
) -> List[str]:
    """Independent final check; return the names of violated constraints."""
    bad: List[str] = []
    if set(assignment) != set(records):
        bad.append("coverage")
    for fid, s in assignment.items():
        if s not in records[fid]["eligible"]:
            bad.append("eligibility")
            break
    for fid, s in pre.items():
        if assignment.get(fid) != s:
            bad.append("preassigned")
            break
    for u in units:
        if len({assignment.get(f) for f in u.members}) != 1:
            bad.append("unit")
            break
    for s, q in quotas.items():
        fams = [f for f, t in assignment.items() if t == s]
        if len(fams) != q:
            bad.append(f"quota:{s}")
        r = sum(1 for f in fams if records[f]["scope"] == "refuse")
        if not (4 * r >= q and 10 * r <= 3 * q):
            bad.append(f"band:{s}")
        for key, m in strata.get(s, {}).items():
            if sum(1 for f in fams if stratum_of(records[f]) == key) < m:
                bad.append(f"strata:{s}")
        for src, m in src_req.get(s, {}).items():
            if sum(1 for f in fams if records[f]["source"] == src) < m:
                bad.append(f"min_share:{s}")
    return bad


class _Search:
    """Iterative bounded DFS over free units with incremental feasibility state."""

    def __init__(
        self,
        units: Sequence[_Unit],
        splits: Sequence[str],
        quotas: Mapping[str, int],
        strata: Mapping[str, Mapping[str, int]],
        src_req: Mapping[str, Mapping[str, int]],
    ) -> None:
        self.splits = list(splits)
        self.quotas = dict(quotas)
        self.strata = {s: dict(strata.get(s, {})) for s in splits}
        self.src_req = {s: dict(src_req.get(s, {})) for s in splits}
        self.band = {s: _band(quotas[s]) for s in splits}
        self.n = {s: 0 for s in splits}
        self.ref = {s: 0 for s in splits}
        self.c_strat = {s: {k: 0 for k in self.strata[s]} for s in splits}
        self.c_src = {s: {k: 0 for k in self.src_req[s]} for s in splits}
        self.a_size = {s: 0 for s in splits}
        self.a_ref = {s: 0 for s in splits}
        self.a_strat = {s: {k: 0 for k in self.strata[s]} for s in splits}
        self.a_src = {s: {k: 0 for k in self.src_req[s]} for s in splits}
        for u in units:
            for s in u.allowed:
                self._avail(u, s, +1)
        self.free_size = sum(u.size for u in units)
        self.free_ref = sum(u.refuse for u in units)
        self.nodes = 0

    def _avail(self, u: _Unit, s: str, sign: int) -> None:
        self.a_size[s] += sign * u.size
        self.a_ref[s] += sign * u.refuse
        for k in self.a_strat[s]:
            self.a_strat[s][k] += sign * u.strata.get(k, 0)
        for k in self.a_src[s]:
            self.a_src[s][k] += sign * u.sources.get(k, 0)

    def place(self, u: _Unit, s: str) -> None:
        """Assign unit ``u`` to split ``s`` (removing it from every availability pool)."""
        for t in u.allowed:
            self._avail(u, t, -1)
        self.free_size -= u.size
        self.free_ref -= u.refuse
        self.n[s] += u.size
        self.ref[s] += u.refuse
        for k in self.c_strat[s]:
            self.c_strat[s][k] += u.strata.get(k, 0)
        for k in self.c_src[s]:
            self.c_src[s][k] += u.sources.get(k, 0)

    def unplace(self, u: _Unit, s: str) -> None:
        """Undo ``place(u, s)``."""
        for t in u.allowed:
            self._avail(u, t, +1)
        self.free_size += u.size
        self.free_ref += u.refuse
        self.n[s] -= u.size
        self.ref[s] -= u.refuse
        for k in self.c_strat[s]:
            self.c_strat[s][k] -= u.strata.get(k, 0)
        for k in self.c_src[s]:
            self.c_src[s][k] -= u.sources.get(k, 0)

    def ok(self, s: str) -> bool:
        """Necessary feasibility of split ``s`` given placed and still-available units."""
        q = self.quotas[s]
        cap = q - self.n[s]
        if cap < 0 or self.a_size[s] < cap:
            return False
        lo, hi = self.band[s]
        ref = self.ref[s]
        non = self.n[s] - ref
        if ref > hi or non > q - lo:
            return False
        if ref + min(self.a_ref[s], cap) < lo:
            return False
        if non + min(self.a_size[s] - self.a_ref[s], cap) < q - hi:
            return False
        # Strata are disjoint, and refuse strata are disjoint from the
        # non-refuse ones, so non-refuse deficits plus the refuse deficit
        # (band floor or refuse stratum, whichever is larger) must fit.
        deficit_non = 0
        deficit_ref = max(0, lo - ref)
        for k, m in self.strata[s].items():
            d = m - self.c_strat[s][k]
            if d > 0:
                if d > self.a_strat[s][k]:
                    return False
                if k.startswith("refuse|"):
                    deficit_ref = max(deficit_ref, d)
                else:
                    deficit_non += d
        if deficit_non + deficit_ref > cap or deficit_non > (q - lo) - non:
            return False
        if ref + deficit_ref > hi:
            return False
        deficit = 0
        for k, m in self.src_req[s].items():
            d = m - self.c_src[s][k]
            if d > 0:
                if d > self.a_src[s][k]:
                    return False
                deficit += d
        return deficit <= cap

    def ok_global(self) -> bool:
        """Aggregate band check: unplaced refuse/non-refuse families must fit the
        splits' remaining band floors and ceilings taken together."""
        need_ref = room_ref = need_non = room_non = 0
        for s in self.splits:
            q = self.quotas[s]
            lo, hi = self.band[s]
            ref = self.ref[s]
            non = self.n[s] - ref
            need_ref += max(0, lo - ref)
            room_ref += max(0, hi - ref)
            need_non += max(0, (q - hi) - non)
            room_non += max(0, (q - lo) - non)
        free_non = self.free_size - self.free_ref
        return need_ref <= self.free_ref <= room_ref and need_non <= free_non <= room_non

    def helps(self, u: _Unit, s: str) -> int:
        """How many open deficits in ``s`` placing ``u`` would reduce."""
        score = 0
        lo, _ = self.band[s]
        if u.refuse and self.ref[s] < lo:
            score += 1
        for k, m in self.strata[s].items():
            if u.strata.get(k) and self.c_strat[s][k] < m:
                score += 1
        for k, m in self.src_req[s].items():
            if u.sources.get(k) and self.c_src[s][k] < m:
                score += 1
        return score

    def fits(self, u: _Unit, s: str) -> bool:
        """Cheap viability: room for ``u`` in ``s`` and no band ceiling overrun."""
        q = self.quotas[s]
        if q - self.n[s] < u.size:
            return False
        lo, hi = self.band[s]
        if self.ref[s] + u.refuse > hi:
            return False
        return (self.n[s] - self.ref[s]) + (u.size - u.refuse) <= q - lo

    def candidates(self, u: _Unit, pref: Sequence[str]) -> List[str]:
        """Viable splits for ``u``, deficit-closing first, then seeded order."""
        rank = {s: i for i, s in enumerate(pref)}
        cands = [s for s in u.allowed if self.fits(u, s)]
        cands.sort(key=lambda s: (-self.helps(u, s), rank[s]))
        return cands

    def run(self, order: Sequence[_Unit], prefs: Sequence[Sequence[str]], budget: int) -> Optional[List[str]]:
        """Return the chosen split per unit of ``order`` (by position), or None.

        Variable order is dynamic: at each step the unplaced unit with the
        fewest viable splits is taken (minimum remaining values), ties broken
        by its position in the seeded ``order``. A unit with no viable split
        is a dead end (forward checking). None means no split exists or the
        ``budget`` of placements ran out.
        """
        if not (self.ok_global() and all(self.ok(s) for s in self.splits)):
            return None
        n_units = len(order)
        if n_units == 0:
            return []
        remaining = set(range(n_units))
        chosen: List[Optional[str]] = [None] * n_units

        def pick() -> Tuple[int, List[str]]:
            best = -1
            best_c: List[str] = []
            best_n = None
            for pos in sorted(remaining):
                u = order[pos]
                n = sum(1 for s in u.allowed if self.fits(u, s))
                if best_n is None or n < best_n:
                    best, best_n = pos, n
                    if n == 0:
                        break
            if best_n:
                best_c = self.candidates(order[best], prefs[best])
            return best, best_c

        # Each frame: [position, candidates, next index, placed split or None].
        pos, cands = pick()
        remaining.discard(pos)
        stack: List[List[Any]] = [[pos, cands, 0, None]]
        while stack:
            frame = stack[-1]
            pos, cands, i, placed = frame
            u = order[pos]
            if placed is not None:
                self.unplace(u, placed)
                frame[3] = None
                chosen[pos] = None
            if i >= len(cands):
                stack.pop()
                remaining.add(pos)
                continue
            s = cands[i]
            frame[2] = i + 1
            self.nodes += 1
            if self.nodes > budget:
                return None
            self.place(u, s)
            frame[3] = s
            chosen[pos] = s
            if self.ok_global() and all(self.ok(t) for t in u.allowed):
                if not remaining:
                    return [c for c in chosen]  # type: ignore[misc]
                npos, ncands = pick()
                remaining.discard(npos)
                stack.append([npos, ncands, 0, None])
            # else: loop back; the frame top undoes this placement.
        return None


def split_families(
    families: Sequence[Mapping[str, Any]],
    seed: int,
    quotas: Mapping[str, int],
    strata: Optional[Mapping[str, Mapping[str, int]]],
    *,
    twins: Mapping[str, Any],
    preassigned: Preassigned = (),
    min_share: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Assign every family to one split, keeping twin units together.

    Args:
        families: content-free family records (shape in the module docstring).
        seed: int seed for the unit shuffle and per-unit split preference.
        quotas: ``{split: exact final family count}``; must sum to len(families).
        strata: ``{split: {"scope|chapter": min_count}}`` minimums, or None/{}.
        twins: the twin-dedup attestation (batches, families_digest,
            pairs_covered, edges); validated, then edges form units.
        preassigned: ``{family_id: split}`` or ``(family_id, split)`` pairs that
            never move and count toward their split's constraints.
        min_share: ``{split: {source: minimum share in [0, 1]}}`` or None.

    Returns:
        ``{family_id: split}`` for every supplied family, keys in sorted order.

    Raises:
        SplitInputError: a refused input (content-bearing or unknown record
            field, malformed twins, digest mismatch, missing batch pair,
            unknown edge endpoint or preassigned id, ...).
        SplitInfeasible: no split satisfies the constraints (or the bounded
            search found none); ``.counts`` carries per-constraint counts.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SplitInputError("seed must be an int")
    q = _validate_quotas(quotas)
    splits = sorted(q)
    records = _validate_records(families, splits)
    ids = sorted(records)
    edges = _validate_twins(twins, ids)
    pre = _validate_preassigned(preassigned, ids, splits)
    strat = _validate_strata(strata, splits)
    src_req = _validate_min_share(min_share, splits, q)

    units, conflicts, pin_inelig = _build_units(records, edges, pre, splits)
    counts = _static_counts(units, records, splits, q, strat, src_req, conflicts, pin_inelig)
    counts["search"] = {"nodes": 0, "budget": SEARCH_NODE_BUDGET, "exhausted": False, "violated": False}
    if _any_violated(counts):
        raise SplitInfeasible("split infeasible: static constraint check failed", counts)

    rng = random.Random(seed)
    free = [u for u in units if u.pin is None]  # already sorted by smallest family_id
    rng.shuffle(free)
    prefs = []
    for _ in free:
        p = list(splits)
        rng.shuffle(p)
        prefs.append(p)

    search = _Search(units, splits, q, strat, src_req)
    for u in units:
        if u.pin is not None:
            search.place(u, u.pin)
    choice = search.run(free, prefs, SEARCH_NODE_BUDGET)
    counts["search"]["nodes"] = search.nodes
    if choice is None:
        counts["search"]["exhausted"] = search.nodes > SEARCH_NODE_BUDGET
        counts["search"]["violated"] = True
        raise SplitInfeasible("split infeasible: no assignment satisfies every constraint", counts)

    assignment: Dict[str, str] = {}
    for u in units:
        if u.pin is not None:
            for f in u.members:
                assignment[f] = u.pin
    for u, s in zip(free, choice):
        for f in u.members:
            assignment[f] = s
    bad = _verify(assignment, records, units, q, strat, src_req, pre)
    if bad:  # defence in depth: never return an unverified split
        counts["search"]["violated"] = True
        counts["search"]["final_check_failed"] = sorted(set(bad))
        raise SplitInfeasible("split infeasible: final verification failed", counts)
    return {fid: assignment[fid] for fid in ids}
