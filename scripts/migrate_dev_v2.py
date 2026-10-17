"""Phase 16A-2b b2: the migration tool. Writes the v2 development rows (private, gitignored) from the v1 public sets and
the two tutorial candidate files.

Inputs (all injectable; defaults are repo-relative, the gaps file is private)::

    eval/golden_set.jsonl, eval/realistic_set.jsonl      the public v1 sets (``eval_schema.load_any``)
    eval/v2_pairing.json                                 ``{"pairs": [[golden id, realistic id], ...]}``
    data/tutorials/tutorial01_candidates.md, tutorial07_candidates.md
                                                         embedded ``{"question", ..., "candidate_id"}`` JSON lines; the
                                                         scope is the ``- **scope:** IN|PARTIAL|OUT`` line of the
                                                         ``### <candidate id>`` block (a ``*-RV`` refusal variant has no block)
    eval/private/work/b2/t1t7_gaps.json                   ``{"version": 1, "gaps": {candidate id: [{"id", "keywords"}, ...]},
                                                         "unresolved": {candidate id: reason}}``: the gaps of the PARTIAL families
    eval/private/work/b2/t1t7_labels.json                 REQUIRED. ``{"version": 1, "provenance": str, "scope": {candidate id:
                                                         answer|partial|refuse}, "phrasings": {candidate id: [index, ...]},
                                                         "flags": {candidate id: str}}``: the owner's labels (below)

Outputs (private)::

    eval/private/dev/tuning_migrated.jsonl               golden-only families, one modelled legacy row each
    eval/private/dev/realistic_migrated.jsonl            unpaired realistic families (one lay legacy row), paired families
                                                         (row 0 = the realistic row, lay; row 1 = the golden row, modelled)
                                                         and the T1/T7 tutorial families (see the labels, below)
    eval/private/work/b2/migration_map.json               v1 id / candidate id -> family id and row ids (a candidate also: the
                                                         embedded rows its rows came from, its label flag, and the labels digest)
    eval/private/inputs/held_unresolved.jsonl            only with ``--hold-unresolved``: the PARTIAL families whose gaps are
                                                         unresolved (not valid v2 rows: they have no gaps), outside ``dev/``

Rules. A family id is minted with ``sealed_tool``'s own mechanism (``known_family_ids``, ``new_family_id``) so it never
collides with an issued or known id, and is recorded as issued the way ``ids`` does it. **Idempotent:** if
``migration_map.json`` exists its ids are reused, so a rerun over the same inputs writes byte-identical files. Every
tutorial row and every legacy refusal row is ``owner_status: draft``. A paired family's two rows must agree on scope.

**Labels.** ``scope`` overrides the heading's scope for the ids it lists (an id overridden away from PARTIAL needs no gaps, and
what the gaps file says about it, even ``unresolved``, is ignored). ``phrasings`` lists the embedded rows of a candidate (0 = its
first embedded row) that become its family's rows, in the order listed, at most 3; without an entry the first 3 embedded rows
are taken. Row 0 of the family is the first one picked; a row is ``modelled`` if it is the candidate's first embedded row and
``lay`` otherwise. An empty list, a duplicate or out-of-range index, more than 3 indices and an id that is not a candidate
(in any section) are ``input_invalid``, and so is a scope that disagrees with the rows' ``type``. ``flags`` go to the map and
never into a row. The sha256 of the labels file is in the map and in the summary.

A PARTIAL candidate (after the overrides) that is listed under ``unresolved`` (or has no gaps) stops the run, naming the candidate id only, unless
``--hold-unresolved`` writes that family to the held file instead. Before anything is written every output is validated
as a v2 set (``eval_schema.load_v2`` with the inventory) in a staging directory, no id collides with a known one, and the
per-split family and refusal-family counts equal what ``sealed_tool count-migrated`` computes from the same inputs
(``sealed_tool.migrated_counts``). The outputs are written atomically, one file at a time, under the sealed tool's lock.

Like every tool that reads private rows it prints only integers, sha256 digests and enum words through the sealed tool's
allowlisted printer (plus a candidate id, which must match ``T<nn>-<letters>[-RV]``, for an unresolved PARTIAL family): never a
question, a keyword or a section label.

Exit codes: 0 ok; 1 count mismatch or internal error; 2 usage or invalid input (schema errors by line and field only);
8 refused (unresolved PARTIAL family, paired rows that disagree on scope, a stale or inconsistent map, an id collision).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src import eval_schema  # noqa: E402
from src.eval_privacy import SealedInputError  # noqa: E402
from scripts import sealed_tool as st  # noqa: E402

MAP_NAME = "migration_map.json"
HELD_NAME = "held_unresolved.jsonl"
GAPS_NAME = "t1t7_gaps.json"
LABELS_NAME = "t1t7_labels.json"
OUT_NAMES = {"tuning": "tuning_migrated.jsonl", "realistic": "realistic_migrated.jsonl"}
TUTORIAL_FILES = ("tutorial01_candidates.md", "tutorial07_candidates.md")
MAX_PHRASINGS = 3  # the T1/T7 cap (prereg drafting_rule.paraphrase.t1_t7_cap)
SCOPES = ("answer", "partial", "refuse")

#: A tutorial candidate id as the candidate files write it (``T01-A``, ``T07-Ca``, ``T07-Cc-RV``). Checked on every id read, so
#: an id that reaches the printer is never free text.
CANDIDATE_ID = re.compile(r"T[0-9]{2}-[A-Za-z]{1,3}(?:-RV)?")
CANDIDATE_FIELDS = frozenset({("unresolved", "candidate"), ("error", "candidate")})
_HEADER = re.compile(r"### ([A-Za-z0-9._-]+)")
_SCOPE_LINE = re.compile(r"- \*\*scope:\*\* (IN|PARTIAL|OUT)(?![A-Za-z])")
SCOPE_OF = {"IN": "answer", "PARTIAL": "partial", "OUT": "refuse"}
_GAP_PLACEHOLDER = [{"id": "held", "keywords": ["held"]}]  # stands in for the gaps of a held family while its rows are validated


class MigratePrinter(st.Printer):
    """The sealed tool's allowlisted printer, plus a validated tutorial candidate id where an unresolved family is named."""

    @staticmethod
    def _value(kind: str, key: str, value: Any) -> str:
        if isinstance(value, str) and (kind, key) in CANDIDATE_FIELDS and CANDIDATE_ID.fullmatch(value):
            return value
        return st.Printer._value(kind, key, value)


# ---------------------------------------------------------------------------
# Reading the inputs
# ---------------------------------------------------------------------------
def load_v1_rows(path: Path) -> List[Dict[str, Any]]:
    """The normalised rows of a v1 set (``eval_schema.load_any``); a v2 set is not a migration input."""
    if not path.is_file():
        raise st.Stop(st.EXIT_USAGE, "input_missing")
    try:
        rows = eval_schema.load_any(path)
    except SealedInputError:
        raise st.Stop(st.EXIT_USAGE, "not_sealed") from None
    if any(row["schema"] != 1 for row in rows):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    return rows


def v1_evidence(rtype: Any, sections: Any) -> List[List[str]]:
    """A v1 row's evidence exactly as ``eval_schema.load_any`` normalises it: ``[]`` for a refusal, else ONE OR group of
    the stripped ``expected_sections``. Anything else is invalid input (no value is shown)."""
    if rtype not in eval_schema.V1_TYPES:
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    if rtype == "refusal":
        if sections not in (None, []):
            raise st.Stop(st.EXIT_USAGE, "input_invalid")
        return []
    if not isinstance(sections, list) or not sections or not all(isinstance(x, str) and x.strip() for x in sections):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    return [[x.strip() for x in sections]]


@dataclass
class Candidate:
    """One tutorial family: its scope (``answer`` | ``partial`` | ``refuse``) and its embedded rows in file order.

    ``parse_candidates`` sets ``scope`` to the heading's; :func:`resolve_candidates` replaces it with the labels file's override
    (if any), picks the phrasings (``picked``: indices into ``rows``, in the order they become the family's rows) and takes
    the family's ``flag``.
    """

    cid: str
    scope: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    heading: str = ""
    picked: List[int] = field(default_factory=list)
    flag: Optional[str] = None


def parse_candidates(paths: Sequence[Path]) -> List[Candidate]:
    """The candidate families of the tutorial files, in file order (a file after a file).

    A family is every embedded JSON line with the same ``candidate_id``. Its scope is its ``### <id>`` block's scope line
    (IN answer, PARTIAL partial, OUT refuse); an ``*-RV`` refusal variant has no block and is a refusal. A block without rows,
    a family without a block (other than an RV), two blocks or two scope lines for one id, an id twice across the files, an
    id that is not ``T<nn>-<letters>[-RV]`` and a line that starts like a JSON row but is not one are all ``input_invalid``.
    That is the heading's scope; the labels file may override it, so the check that a scope and the rows' ``type`` agree is
    :func:`resolve_candidates`'s.
    """
    out: List[Candidate] = []
    seen: Set[str] = set()
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise st.Stop(st.EXIT_USAGE, "input_missing") from None
        scopes: Dict[str, str] = {}
        rows_of: Dict[str, List[Dict[str, Any]]] = {}
        current: Optional[str] = None
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("### "):
                match = _HEADER.fullmatch(line.rstrip())
                current = match.group(1) if match else None
                if current is not None:
                    if not CANDIDATE_ID.fullmatch(current) or current in rows_of or current in scopes:
                        raise st.Stop(st.EXIT_USAGE, "input_invalid")
                    scopes[current] = ""  # a block seen; its scope line is still to come
                continue
            scope_line = _SCOPE_LINE.match(line)
            if scope_line and current is not None:
                if scopes[current]:
                    raise st.Stop(st.EXIT_USAGE, "input_invalid")
                scopes[current] = SCOPE_OF[scope_line.group(1)]
                continue
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    obj = json.loads(stripped)
                except (ValueError, RecursionError):
                    raise st.Stop(st.EXIT_USAGE, "input_invalid") from None
                cid = obj.get("candidate_id") if isinstance(obj, dict) else None
                if not (isinstance(cid, str) and CANDIDATE_ID.fullmatch(cid)):
                    raise st.Stop(st.EXIT_USAGE, "input_invalid")
                rows_of.setdefault(cid, []).append(obj)
        for cid, scope in scopes.items():
            if not scope or cid not in rows_of:
                raise st.Stop(st.EXIT_USAGE, "input_invalid")  # a block with no scope line, or with no phrasings
        for cid, rows in rows_of.items():
            if cid in seen:
                raise st.Stop(st.EXIT_USAGE, "input_invalid")
            seen.add(cid)
            scope = scopes.get(cid) or ("refuse" if cid.endswith("-RV") else "")
            if not scope or (cid.endswith("-RV") and scope != "refuse"):
                raise st.Stop(st.EXIT_USAGE, "input_invalid")
            out.append(Candidate(cid, scope, rows, heading=scope))
    return out


@dataclass(frozen=True)
class Labels:
    """The owner's labels for the tutorial families (``eval/private/work/b2/t1t7_labels.json``), checked for shape only.

    ``scope`` overrides a heading's scope, ``phrasings`` lists the embedded rows (0 = the first embedded row of the
    candidate) that become the family's rows, in that order, ``flags`` are notes that go to the map and never into a row.
    ``sha256`` is the file's digest, recorded in the map and printed.
    """

    scope: Mapping[str, str]
    phrasings: Mapping[str, List[int]]
    flags: Mapping[str, str]
    sha256: str


def load_labels(path: Path) -> Labels:
    """``{"version": 1, "provenance": str, "scope": {id: answer|partial|refuse}, "phrasings": {id: [index, ...]}, "flags": {id: str}}``.

    ``scope``, ``phrasings`` and ``flags`` may be absent (nothing labelled); every id must look like a candidate id (whether it
    is one is :func:`resolve_candidates`'s check) and any other shape or key is ``input_invalid``. A missing file is
    ``input_missing``: the labels are a required input.
    """
    try:
        data = path.read_bytes()
    except OSError:
        raise st.Stop(st.EXIT_USAGE, "input_missing") from None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise st.Stop(st.EXIT_USAGE, "input_invalid") from None
    ok = isinstance(doc, dict) and doc.get("version") == 1 and isinstance(doc.get("provenance"), str)
    ok = ok and set(doc) <= {"version", "provenance", "scope", "phrasings", "flags"}
    sections = {name: doc.get(name, {}) for name in ("scope", "phrasings", "flags")} if ok else {}
    ok = ok and all(isinstance(v, dict) and all(isinstance(k, str) and CANDIDATE_ID.fullmatch(k) for k in v) for v in sections.values())
    if ok:
        ok = (
            all(v in SCOPES for v in sections["scope"].values())
            and all(isinstance(v, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in v) for v in sections["phrasings"].values())
            and all(isinstance(v, str) for v in sections["flags"].values())
        )
    if not ok:
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    return Labels(sections["scope"], sections["phrasings"], sections["flags"], hashlib.sha256(data).hexdigest())


def resolve_candidates(candidates: Sequence[Candidate], labels: Labels) -> List[Candidate]:
    """The candidates as migrated: the scope overrides applied, the phrasings picked, the flags attached.

    * An id in the labels that is not a candidate is ``input_invalid``.
    * A family's phrasings are its ``phrasings`` indices into the candidate's embedded rows, kept in the order listed (the first is
      the family's row 0); without an entry they are the first :data:`MAX_PHRASINGS` embedded rows. An empty list, a duplicate
      index, an index out of range and more than :data:`MAX_PHRASINGS` indices are ``input_invalid``.
    * The effective scope (the override, else the heading's) and the rows' ``type`` must agree: a refusal family has only
      ``refusal`` rows and no other family has one (``input_invalid``). An override between answer and partial changes no
      refusal status.
    """
    known = {c.cid for c in candidates}
    if (set(labels.scope) | set(labels.phrasings) | set(labels.flags)) - known:
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    out: List[Candidate] = []
    for cand in candidates:
        scope = labels.scope.get(cand.cid, cand.heading)
        refusals = sum(1 for r in cand.rows if r.get("type") == "refusal")
        if refusals != (len(cand.rows) if scope == "refuse" else 0):
            raise st.Stop(st.EXIT_USAGE, "input_invalid")
        picked = list(labels.phrasings.get(cand.cid, range(min(MAX_PHRASINGS, len(cand.rows)))))
        if not picked or len(picked) > MAX_PHRASINGS or len(set(picked)) != len(picked) or any(not 0 <= i < len(cand.rows) for i in picked):
            raise st.Stop(st.EXIT_USAGE, "input_invalid")
        out.append(Candidate(cand.cid, scope, cand.rows, cand.heading, picked, labels.flags.get(cand.cid)))
    return out


def load_gaps(path: Path, partial: Sequence[str], overridden: Collection[str] = ()) -> Tuple[Dict[str, List[Any]], Set[str]]:
    """``(gaps by candidate id, unresolved ids)`` for the PARTIAL candidates ``partial``.

    The file is only needed when there is a PARTIAL candidate. A candidate is unresolved when it is listed under
    ``unresolved`` or has no (or an empty) gap list; an id outside ``partial`` in either section, an id in both, or any other
    shape is ``input_invalid``. ``overridden`` are the ids whose heading says PARTIAL but whose label says otherwise: the gaps
    file was drafted for the heading, so whatever it says about them (gaps, or ``unresolved``) is ignored, and they need no
    gaps. The gap objects themselves are checked by the v2 validation of the rows.
    """
    if not partial:
        return {}, set()
    doc = st.read_json(path)
    ok = isinstance(doc, dict) and doc.get("version") == 1 and set(doc) <= {"version", "gaps", "unresolved"}
    gaps = doc.get("gaps", {}) if ok else None
    reasons = doc.get("unresolved", {}) if ok else None
    if not (isinstance(gaps, dict) and isinstance(reasons, dict)):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    if not all(isinstance(v, list) for v in gaps.values()) or not all(isinstance(v, str) for v in reasons.values()):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    gaps = {k: v for k, v in gaps.items() if k not in overridden}
    reasons = {k: v for k, v in reasons.items() if k not in overridden}
    if (set(gaps) | set(reasons)) - set(partial) or set(gaps) & set(reasons):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    resolved = {cid: gaps[cid] for cid in partial if gaps.get(cid)}
    return resolved, {cid for cid in partial if cid not in resolved}


# ---------------------------------------------------------------------------
# Family specs
# ---------------------------------------------------------------------------
@dataclass
class Spec:
    """One migrated family before it has an id: its split, the keys that name it in the map and its row bodies."""

    split: str  # tuning | realistic
    keys: Tuple[Tuple[str, str, int], ...]  # (kind "v1" | "candidate", v1 id or candidate id, row index)
    rows: List[Dict[str, Any]]
    held: bool = False
    fid: str = ""
    picked: Tuple[int, ...] = ()  # candidate families: the embedded rows (0 = the first) that became this family's rows
    flag: Optional[str] = None

    @property
    def scope(self) -> str:
        return str(self.rows[0]["scope"])


def row_body(question: str, scope: str, evidence: List[List[str]], rtype: str, register: str, source: str, *,
             gaps: Optional[List[Any]] = None, draft: bool = False) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "schema": eval_schema.SCHEMA_VERSION, "question": question, "scope": scope, "evidence": evidence, "type": rtype,
        "register": register, "source": source,
    }
    if gaps:
        body["gaps"] = json.loads(json.dumps(gaps))  # a private copy
    if draft:
        body["owner_status"] = "draft"
    return body


def legacy_body(row: Mapping[str, Any], register: str) -> Dict[str, Any]:
    """A v1 public row as a legacy v2 row body (a refusal is a draft: the owner confirms it)."""
    return row_body(row["question"], row["scope"], [list(g) for g in row["evidence"]], row["type"], register, "legacy",
                    draft=row["scope"] == "refuse")


def candidate_body(raw: Mapping[str, Any], scope: str, embedded: int, gaps: Optional[List[Any]]) -> Dict[str, Any]:
    """One tutorial phrasing as a v2 row body: modelled if it is the candidate's first embedded row (``embedded`` 0, the
    modelled question), else lay; the family's scope and gaps; always a draft."""
    question = raw.get("question")
    if not (isinstance(question, str) and question.strip()):
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    evidence = v1_evidence(raw.get("type"), raw.get("expected_sections"))
    return row_body(question, scope, evidence, str(raw["type"]), "modelled" if embedded == 0 else "lay", "tutorial", gaps=gaps, draft=True)


def build_specs(
    golden: Sequence[Mapping[str, Any]], realistic: Sequence[Mapping[str, Any]], pairs: Sequence[Sequence[str]],
    candidates: Sequence[Candidate], gaps: Mapping[str, List[Any]], unresolved: Set[str], hold: bool,
) -> List[Spec]:
    """The families to write, in a fixed order: golden-only (tuning), then realistic (a pair is one family), then tutorials.

    Raises ``input_invalid`` for a pair that names an unknown id or reuses one, and ``pair_scope_conflict`` (after printing
    the count) when a pair's two rows disagree on scope.
    """
    gold = {str(r["id"]): r for r in golden}
    real = {str(r["id"]): r for r in realistic}
    pair_of_real: Dict[str, str] = {}
    for g, r in pairs:
        if g not in gold or r not in real or r in pair_of_real.values() or g in pair_of_real:
            raise st.Stop(st.EXIT_USAGE, "inputs_invalid")
        pair_of_real[g] = r
    conflicts = sum(1 for g, r in pair_of_real.items() if gold[g]["scope"] != real[r]["scope"])
    if conflicts:
        raise st.Stop(st.EXIT_REFUSED, "pair_scope_conflict", conflicts=conflicts)
    paired_real = {r: g for g, r in pair_of_real.items()}
    specs: List[Spec] = []
    for gid, row in gold.items():
        if gid not in pair_of_real:
            specs.append(Spec("tuning", (("v1", gid, 0),), [legacy_body(row, "modelled")]))
    for rid, row in real.items():
        if rid in paired_real:
            g = gold[paired_real[rid]]
            specs.append(Spec(
                "realistic", (("v1", rid, 0), ("v1", paired_real[rid], 1)), [legacy_body(row, "lay"), legacy_body(g, "modelled")],
            ))
        else:
            specs.append(Spec("realistic", (("v1", rid, 0),), [legacy_body(row, "lay")]))
    for cand in candidates:  # resolved: the effective scope, the picked phrasings (the first picked is row 0), the flag
        family_gaps = gaps.get(cand.cid) if cand.scope == "partial" else None
        bodies = [candidate_body(cand.rows[i], cand.scope, i, family_gaps) for i in cand.picked]
        specs.append(Spec(
            "realistic", (("candidate", cand.cid, 0),), bodies, held=cand.cid in unresolved and hold, picked=tuple(cand.picked),
            flag=cand.flag,
        ))
    return specs


# ---------------------------------------------------------------------------
# The map, ids and the checks before anything is written
# ---------------------------------------------------------------------------
def read_map(path: Path) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], bool]:
    """``({(kind, key): entry}, existed)`` from ``migration_map.json`` (``({}, False)`` when there is none)."""
    if not path.is_file():
        return {}, False
    doc = st.read_json(path, "map_invalid")
    ok = isinstance(doc, dict) and set(doc) - {"labels_sha256"} == {"version", "v1", "candidates"} and doc["version"] == 1
    ok = ok and isinstance(doc["v1"], dict) and isinstance(doc["candidates"], dict)
    entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if ok:
        for kind, section in (("v1", doc["v1"]), ("candidate", doc["candidates"])):
            for key, entry in section.items():
                good = isinstance(entry, dict) and isinstance(entry.get("family_id"), str) and st._FAMILY_ID.fullmatch(entry["family_id"])
                if not (isinstance(key, str) and good and entry.get("split") in st.DEV_SPLITS):
                    ok = False
                entries[(kind, str(key))] = entry
    if not ok:
        raise st.Stop(st.EXIT_USAGE, "map_invalid")
    return entries, True


def assign_ids(ctx: st.Ctx, specs: Sequence[Spec], old: Mapping[Tuple[str, str], Mapping[str, Any]], own: Sequence[Path]) -> Tuple[int, int]:
    """Give every spec its family id: the map's if it has one, else a fresh one. Returns ``(reused, minted)``.

    A map that does not fit the inputs is ``map_invalid``: an entry for a key the inputs no longer have, a pair whose two
    ids are not both in it (or are in it under two families), two specs on one id, or an id under another split. An id
    that another file under the private root (not one of this tool's own) already holds is ``id_collision``.
    """
    used: Set[Tuple[str, str]] = set()
    for spec in specs:
        known = [old.get((kind, key)) for kind, key, _ in spec.keys]
        present = [e for e in known if e is not None]
        if present:
            fids = {str(e["family_id"]) for e in present}
            if len(present) != len(known) or len(fids) != 1 or any(e["split"] != spec.split for e in present):
                raise st.Stop(st.EXIT_USAGE, "map_invalid")
            spec.fid = fids.pop()
            used.update((kind, key) for kind, key, _ in spec.keys)
    if set(old) - used:
        raise st.Stop(st.EXIT_USAGE, "map_invalid")  # an entry the inputs do not name
    reused = [s.fid for s in specs if s.fid]
    if len(set(reused)) != len(reused):
        raise st.Stop(st.EXIT_USAGE, "map_invalid")
    if set(reused) & st.known_family_ids(ctx, exclude=own):
        raise st.Stop(st.EXIT_REFUSED, "id_collision")
    taken = st.known_family_ids(ctx) | set(reused)
    minted = 0
    for spec in specs:
        if spec.fid:
            continue
        while True:
            cand = st.new_family_id()
            if cand not in taken:
                break
        taken.add(cand)
        spec.fid = cand
        minted += 1
    return len(reused), minted


def final_rows(specs: Iterable[Spec]) -> List[Dict[str, Any]]:
    return [{"id": f"{s.fid}-{n}", "family_id": s.fid, **body} for s in specs for n, body in enumerate(s.rows)]


def map_document(specs: Sequence[Spec], labels_sha256: str) -> Dict[str, Any]:
    """The map: each v1 id and candidate id with its family id and row ids, the embedded rows (``phrasings``) a candidate's rows
    came from, a candidate's label ``flags`` if it has any, and the digest of the labels file they were made under."""
    v1: Dict[str, Any] = {}
    candidates: Dict[str, Any] = {}
    for s in specs:
        for kind, key, index in s.keys:
            if kind == "v1":
                v1[key] = {"family_id": s.fid, "row_id": f"{s.fid}-{index}", "split": s.split}
            else:
                candidates[key] = {
                    "family_id": s.fid, "row_ids": [f"{s.fid}-{n}" for n in range(len(s.rows))], "split": s.split, "held": s.held,
                    "phrasings": list(s.picked),
                }
                if s.flag is not None:
                    candidates[key]["flags"] = s.flag
    return {"version": 1, "labels_sha256": labels_sha256, "v1": v1, "candidates": candidates}


def validate_outputs(ctx: st.Ctx, blobs: Mapping[str, bytes]) -> Dict[str, List[Dict[str, Any]]]:
    """Every non-empty output as a v2 set (``eval_schema.load_v2`` with the inventory), staged beside ``dev/`` and removed.

    Returns the rows the loader read back. A violation prints ``output_error file=<name> line=<n> field=<path>`` for each
    (schema field paths only, never a value) and stops with ``schema_invalid``.
    """
    ctx.dev_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".migrate_stage_", dir=ctx.dev_dir))
    loaded: Dict[str, List[Dict[str, Any]]] = {}
    failed = 0
    try:
        for name, data in blobs.items():
            if not data:
                loaded[name] = []
                continue
            path = stage / f"{name}.jsonl"
            path.write_bytes(data)
            try:
                loaded[name] = eval_schema.load_v2(path, inventory=ctx.inventory())
            except eval_schema.InventoryError:
                raise
            except eval_schema.SchemaError as exc:
                failed += len(exc.errors)
                for line, fld, _reason in exc.errors[:50]:
                    ctx.printer.emit(
                        "output_error", file=name if name in st.ENUM_WORDS else "other", line=min(line, st.MAX_PRINT_INT),
                        field=fld if st._FIELD_PATH.fullmatch(fld) else "other",
                    )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if failed:
        raise st.Stop(st.EXIT_USAGE, "schema_invalid", errors=failed)
    return loaded


def family_counts(rows: Sequence[Mapping[str, Any]]) -> Tuple[int, int, int]:
    """``(families, refusal families, partial families)`` of rows (a family's scope is its rows' common scope)."""
    scope = {str(r["family_id"]): str(r["scope"]) for r in rows}
    return len(scope), sum(1 for v in scope.values() if v == "refuse"), sum(1 for v in scope.values() if v == "partial")


def check_counts(
    golden: Sequence[Mapping[str, Any]], realistic: Sequence[Mapping[str, Any]], pairs: Sequence[Sequence[str]],
    candidates: Sequence[Candidate], written: Mapping[str, Sequence[Mapping[str, Any]]], held: Sequence[Mapping[str, Any]],
) -> st.MigratedCounts:
    """Stop (``count_mismatch``) unless the outputs hold the families and refusal families ``count-migrated`` counts.

    ``count-migrated``'s own helper (:func:`sealed_tool.migrated_counts`) counts from the same inputs; the held families
    are realistic's, as it counts them. ``candidates`` are the resolved ones, every candidate id: the family set
    ``count-migrated --tutorial`` sees. Only a scope override between refuse and non-refuse could make their refusal status
    differ from the heading's, and :func:`resolve_candidates` refuses one (the rows' ``type`` would disagree).
    """
    want = st.migrated_counts(
        {str(r["id"]): r["scope"] == "refuse" for r in golden}, {str(r["id"]): r["scope"] == "refuse" for r in realistic},
        {c.cid: c.scope == "refuse" for c in candidates}, pairs,
    )
    tf, tr, _ = family_counts(written["tuning"])
    rf, rr, _ = family_counts(written["realistic"])
    hf, hr, _ = family_counts(held)
    got = (tf, tr, rf + hf, rr + hr)
    if got != (want.tuning_families, want.tuning_refuse, want.realistic_families, want.realistic_refuse) or want.conflicts:
        raise st.Stop(st.EXIT_FAIL, "count_mismatch")
    return want


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def append_issued(ctx: st.Ctx, fids: Sequence[str]) -> None:
    """Record family ids as issued, the way ``ids`` does (``sealed/work/issued_ids.txt``), for those not already there."""
    issued = ctx.sealed_work / "issued_ids.txt"
    previous = issued.read_bytes() if issued.is_file() else b""
    have = set(previous.decode("utf-8", errors="replace").split())
    fresh = [f for f in fids if f not in have]
    if fresh:
        st.write_atomic(issued, previous + ("\n".join(fresh) + "\n").encode("ascii"))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
@dataclass
class Inputs:
    golden: Path
    realistic: Path
    pairing: Path
    tutorials: Sequence[Path]
    gaps: Path
    labels: Path


def migrate(ctx: st.Ctx, inputs: Inputs, hold_unresolved: bool) -> int:
    """Read, check, then write the three outputs (and the held file). Prints counts and digests only."""
    golden, realistic = load_v1_rows(inputs.golden), load_v1_rows(inputs.realistic)
    pairs = st.load_pairs(inputs.pairing)
    parsed = parse_candidates(inputs.tutorials)
    labels = load_labels(inputs.labels)
    candidates = resolve_candidates(parsed, labels)
    partial = [c.cid for c in candidates if c.scope == "partial"]
    overridden = [c.cid for c in candidates if c.heading == "partial" and c.scope != "partial"]
    gaps, unresolved = load_gaps(inputs.gaps, partial, overridden)
    if unresolved and not hold_unresolved:
        for cid in partial:
            if cid in unresolved:
                ctx.printer.emit("unresolved", candidate=cid)
        raise st.Stop(st.EXIT_REFUSED, "unresolved_partial", families=len(unresolved))
    specs = build_specs(golden, realistic, pairs, candidates, gaps, unresolved, hold_unresolved)

    inputs_dir, work_dir = ctx.private / "inputs", ctx.private / "work" / "b2"
    map_path, held_path = work_dir / MAP_NAME, inputs_dir / HELD_NAME
    out_paths = {split: ctx.dev_dir / name for split, name in OUT_NAMES.items()}
    old, _existed = read_map(map_path)
    issued_path = ctx.sealed_work / "issued_ids.txt"
    reused, minted = assign_ids(ctx, specs, old, [map_path, held_path, issued_path, *out_paths.values()])

    blobs: Dict[str, bytes] = {}
    for split in st.DEV_SPLITS:
        blobs[split] = st.dump_rows(final_rows(s for s in specs if s.split == split and not s.held), mark=False)
    held_rows = final_rows(s for s in specs if s.held)
    held_blob = st.dump_rows(held_rows, mark=False)
    staged_held = st.dump_rows(  # while validated, a held family carries a placeholder gap so only its other fields are checked
        [{**r, "gaps": _GAP_PLACEHOLDER} for r in held_rows], mark=False,
    )
    loaded = validate_outputs(ctx, {**blobs, "held": staged_held})
    want = check_counts(golden, realistic, pairs, candidates, loaded, loaded["held"])

    # nothing is written before this point. The map first: a crash after it is finished by the next run, which reuses its ids.
    mapping = map_document(specs, labels.sha256)
    map_sha = st.write_json(map_path, mapping)
    append_issued(ctx, [s.fid for s in specs])
    for split, path in out_paths.items():
        if blobs[split]:
            st.write_atomic(path, blobs[split])
        elif path.is_file():
            path.unlink()  # this tool's own output from an earlier run: there is nothing for this split now
    if held_blob:
        st.write_atomic(held_path, held_blob)
    elif held_path.is_file():
        held_path.unlink()

    for split in st.DEV_SPLITS:
        families, refusals, partials = family_counts(loaded[split])
        ctx.printer.emit(
            "migrated", file=split, families=families, rows=len(loaded[split]), refusals=refusals, partials=partials,
            sha256=digest(blobs[split]),
        )
    families, refusals, partials = family_counts(held_rows)
    held_fields: Dict[str, Any] = {"families": families, "rows": len(held_rows), "refusals": refusals, "partials": partials}
    if held_blob:
        held_fields["sha256"] = digest(held_blob)
    ctx.printer.emit("held", **held_fields)
    ctx.printer.emit(
        "migration_map", v1=len(mapping["v1"]), candidates=len(mapping["candidates"]), ids_reused=reused, ids_minted=minted,
        sha256=map_sha,
    )
    ctx.printer.emit(
        "labels", sha256=labels.sha256, scope_overrides=sum(1 for c in candidates if c.scope != c.heading),
        phrasing_entries=len(labels.phrasings), flags=len(labels.flags),
    )
    ctx.printer.emit(
        "migrated_check", status="ok", pairs=want.pairs, tutorial_exercises=want.tutorial_families,
        tuning_families=want.tuning_families, tuning_refuse=want.tuning_refuse,
        realistic_families=want.realistic_families, realistic_refuse=want.realistic_refuse,
    )
    return st.EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = st._Parser(prog="migrate_dev_v2.py", description="Migrate the v1 development sets to v2 rows (private). Prints only counts and digests.")
    for opt in ("private-root", "repo", "inventory"):
        ap.add_argument(f"--{opt}", default=None, metavar="PATH")
    ap.add_argument("--golden", default=None)
    ap.add_argument("--realistic", default=None)
    ap.add_argument("--pairing", default=None)
    ap.add_argument("--tutorial", action="append", default=None, dest="tutorials")
    ap.add_argument("--gaps", default=None)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--hold-unresolved", action="store_true")
    return ap


def resolve_inputs(ctx: st.Ctx, a: argparse.Namespace) -> Inputs:
    return Inputs(
        golden=Path(a.golden) if a.golden else ctx.repo / "eval" / "golden_set.jsonl",
        realistic=Path(a.realistic) if a.realistic else ctx.repo / "eval" / "realistic_set.jsonl",
        pairing=Path(a.pairing) if a.pairing else ctx.repo / "eval" / "v2_pairing.json",
        tutorials=[Path(p) for p in a.tutorials] if a.tutorials else [ctx.repo / "data" / "tutorials" / n for n in TUTORIAL_FILES],
        gaps=Path(a.gaps) if a.gaps else ctx.private / "work" / "b2" / GAPS_NAME,
        labels=Path(a.labels) if a.labels else ctx.private / "work" / "b2" / LABELS_NAME,
    )


def main(argv: Optional[Sequence[str]] = None, *, out: Any = None, err: Any = None) -> int:
    """CLI entry point; returns the exit code. Nothing but the allowlisted printer writes output."""
    printer = MigratePrinter(out, err)
    try:
        args = build_parser().parse_args(argv)
        ctx = st.default_ctx(printer, private=args.private_root, repo=args.repo, inventory_path=args.inventory)
        inputs = resolve_inputs(ctx, args)
        with st.state_lock(ctx):
            return migrate(ctx, inputs, args.hold_unresolved)
    except st.Stop as stop:
        printer.error(stop.reason, **stop.fields)
        return stop.code
    except eval_schema.SchemaError as exc:
        for line, fld, _reason in exc.errors[:50]:
            printer.emit("schema_error", line=min(line, st.MAX_PRINT_INT), field=fld if st._FIELD_PATH.fullmatch(fld) else "other")
        printer.error("inventory_invalid" if isinstance(exc, eval_schema.InventoryError) else "schema_invalid", errors=len(exc.errors))
        return st.EXIT_USAGE
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001 - never show exception text (it could carry a path or a row)
        printer.err.write("error reason=internal\n")
        return st.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
