"""Phase 15 WS4 — parse bake-off arm reports and emit the selection evidence.

This script's stdout IS the phase's binding selection evidence (gate finding
A19 / Codex C7), so it is a committed, unit-tested instrument rather than a
scratchpad throwaway. It reads the offline arm reports written by
``python -m src.pipeline eval ... --skip-refusals --skip-completeness`` and
emits, in Markdown:

1. the selection table (golden + realistic strict@6 / related@6 per arm),
2. the per-question golden flip lists (HIT->MISS and MISS->HIT) vs a named
   baseline arm — the winner must have zero HIT->MISS flips,
3. per-class movement against the roster in
   ``docs/designs/001-bakeoff-embedding-model.md`` (3 vocabulary-gap +
   5 near-miss realistic questions), and
4. S5/N4 both-role coverage, checked for 2.2.1 and 2.2.2 *separately* with
   equal-or-descendant matching, so one generic retrieved ``2.2`` cannot tick
   both roles at once (Codex C3: ``_sections_related`` is symmetric and would
   falsely pass both).

Two safety properties are structural, not procedural:

- **Held-out exclusion.** Any argument containing "heldout" is refused at the
  argparse level (exit 2), so the held-out set cannot enter a selection
  artifact even by a slip of shell history (finding A25 / Codex C7).
- **Comparability.** Every report must carry
  ``query expansion: disabled (offline run)``; a live-expansion report is not
  comparable to offline ones and raises instead of being averaged in.

Note on the per-question section heading: the evaluator labels it
``## <set> — per-question detail (hybrid+rewrite)`` even on an offline run
(``src/evaluator.py:2280``). Offline that mode IS raw hybrid — expansion is
disabled — so the parser keys off that real label; there is no bare "hybrid"
per-question heading to grep for (round-3 correction 3).

Phase 16A-1 (items 1, 6, 9; D65, D68) adds:

- **C4 cohort identity.** Each arm report's rows sidecar
  (``<report>.rows.json``, ``src.eval_cohort``) is loaded when present. When
  EVERY arm has one, :func:`compare` keys rows by ``(set sha256, id)`` and
  refuses (:class:`C4Error`) any mismatch in set hashes, set labels,
  ``cohort_fp``, scorer version, absorbed-map hash or expansion identity
  (two ``live`` arms may differ in draw digest only; ``--rewrite-candidate
  <config hash>`` permits only that declared config difference), and any
  missing, extra or duplicate row. ``w_sweep --ranks-out`` dumps carry the
  same fields and :func:`compare_prod_ranks` applies the same rules.
- **Legacy.** An arm without C4 fields (a v5 report, a pre-16A dump) is
  ``legacy``: refused (:class:`LegacyArmError`) unless ``--legacy`` /
  ``legacy=True`` is given, and then today's v5 rules run unchanged with a
  ``legacy: C4 not checked`` note on stderr (stdout stays v5 byte for byte).
- **Controls.** ``--controls <file>`` (``{"version": 1, "controls":
  [{"set_sha256", "ids"}]}``) must be non-empty and resolve in both arms;
  control flips are reported apart (C4 only).
- **Privacy floor.** :func:`run` takes a keyword-only ``privacy`` (no
  default). ``main`` derives the floor = the strictest ``classify`` over every
  path it opens (reports, sidecars, prod-rank dumps, the expansion cache, the
  ``--controls`` file); sealed input exits 4 before anything else. A controls
  file has no class of its own, so classify's rule 7 makes every
  ``--controls`` run private (16A-1 merge gate, Codex #2). Without ``--legacy-public`` a
  ``--prod-ranks`` run (which opens the 0717 cache) floors to private: stdout
  then carries ids and aggregates only, and the manifest is written only under
  ``eval/private/runs/<run id>/`` with ``inputs.json``.
- **One roster.** Roster and S5/N4 roles come from ``src.eval_roster``, keyed
  by row id; ``parse_report`` adds an ``id`` to every detail row.
- **Report v6.** A v6 report (schema-2 run, ``src.eval_v6``) has no v5
  provenance, ablation or detail sections: its sets (label, path, sha256) are
  read from its ``## Cohort`` block and its per-row ranks from its rows
  sidecar, which it always needs (machines read the sidecar, item 6). The
  selection table is then the sidecar's row-level strict/related@6; S5/N4
  role coverage is ``n/a`` (no retrieved sections are recorded). 16A-1 merge
  gate, Codex #5.
- **Retrieval depth.** A v6 report must declare its ``top_k`` and hit
  cut-off ``k``, and ``k`` must be the selection's @6 (a shallower run is
  refused, never re-read at @6). C4 arms must all record the same ``top_k``
  (a v5 report's ``- top_k:`` header line counts; a pre-16A fixture without
  one is unknown, and unknown matches only unknown), and none below 6. Merge
  gate round 2, Codex #2.

Usage::

    python scripts/bakeoff_report.py \\
        --reports eval/bakeoff/baseline-minilm.md eval/bakeoff/gte.md \\
        --baseline baseline-minilm \\
        [--manifest-out eval/bakeoff/manifest.json] [--legacy] [--legacy-public] \\
        [--controls controls.json] [--rewrite-candidate <config hash>]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src import eval_privacy as _privacy  # noqa: E402
from src import eval_sets as _eval_sets  # noqa: E402
from src.eval_cohort import eligible_fp, sidecar_path  # noqa: E402
from src.eval_privacy import (  # noqa: E402
    PUBLIC,
    SEALED,
    SealedInputError,
    check_floor,
    public_v1_id,
    require_class,
    safe_error,
    write_inputs_json,
    write_private,
)
from src.eval_roster import ROLE_QUESTIONS, ROSTER, RoleSpec, RosterEntry  # noqa: E402,F401

# The offline-run disclosure every arm report must carry. Reports that ran with
# live expansion are a different experiment and are refused, not compared.
EXPANSION_DISABLED_MARKER = "query expansion: disabled (offline run)"

# The per-question detail section the evaluator emits when the hybrid+rewrite
# mode is ablated — which, with expansion disabled, is raw hybrid by identity.
DETAIL_MODE = "hybrid+rewrite"

# Which set is which, by the eval-set file the report names in its provenance.
GOLDEN_BASENAME = "golden_set.jsonl"
REALISTIC_BASENAME = "realistic_set.jsonl"

# The only question-set files a selection report may record. Anything else
# (notably the held-out set, however a report file happens to be named) is
# refused at parse time — see _assert_set_provenance.
ALLOWED_SET_BASENAMES = (GOLDEN_BASENAME, REALISTIC_BASENAME)

# The intent-fusion weight that ships; compare_prod_ranks reads the dumps at it
# and the manifest records it.
SHIPPED_WEIGHT = 0.25

# Default expansion cache the production-rank dumps were replayed from
# (scripts/w_sweep.py CACHE); duplicated so importing this script never imports
# the sweep (which loads the retrieval pipeline). Absolute: no cwd dependence.
DEFAULT_EXPANSION_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval",
    "w_sweep_expansions_20260717.json",
)

# The cutoff every "@6" figure in this report is read at.
HIT_K = 6

_ABLATION_HEADING = re.compile(r"^##\s+(?P<label>.+?)\s+—\s+retrieval ablation\s*$")
_DETAIL_HEADING = re.compile(
    r"^##\s+(?P<label>.+?)\s+—\s+per-question detail\s+\((?P<mode>[^)]+)\)\s*$"
)
_DETAIL_ROW = re.compile(
    r"^- \[(?P<type>[^\]]*)\] "
    r"strict=(?P<strict>HIT|MISS)\(rank=(?P<strict_rank>None|\d+)\) "
    r"related=(?P<related>HIT|MISS)\(rank=(?P<related_rank>None|\d+)\) "
    r"expected=(?P<expected>\[.*?\]) "
    r"retrieved=(?P<retrieved>\[.*?\])"
    r"(?P<extra>.*?) :: (?P<question>.*)$"
)
_SET_LABEL = re.compile(r"^- (?P<label>[^:]+): ")
# Report v6 (``src.eval_v6.format_v6_report``): title, model line, cohort lines.
V6_TITLE_PREFIX = "# Legal RAG Evaluation Report v6"
_V6_MODEL = re.compile(r"^- git sha: .*; embedding model: (?P<model>.+); generation model: .*$")
_V6_DEPTH = re.compile(r"^- top_k: (?P<top_k>\d+); hit cut-off k = (?P<k>\d+)$")
# The v5 header's depth line (``src.evaluator``'s v5 and v1 report headers).
_V5_TOP_K = re.compile(r"^- top_k: (?P<top_k>\d+)$")
_V6_COHORT = re.compile(
    r"^- (?P<label>.+?): path (?P<path>.+); privacy (?P<privacy>[a-z]+); schema (?P<schema>\d+); "
    r"sha256 (?P<sha256>[0-9a-f]{64}); rows \d+; families \d+; cohort_fp [0-9a-f]{64}$"
)
_SET_FIELD = re.compile(r"^\s+- (?P<key>path|sha256): (?P<value>.+)$")
_EMBEDDING_MODEL = re.compile(r"^- embedding model: (?P<model>.+)$")

# A detail row that already shows an opaque v1 id (a private report) instead
# of question text.
_OPAQUE_ID = re.compile(r"^q:[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# CLI exit codes: 2 = refused (argparse errors, C4/legacy refusals), 4 = sealed.
EXIT_REFUSED = 2
EXIT_SEALED = 4

# The fields that make a rows sidecar / rank dump a C4 document (item 6).
C4_FIELDS = ("version", "scorer_version", "absorbed_map_sha256", "expansion", "cohorts", "rows")
ROW_FIELDS = ("set_sha256", "id", "mode", "strict_rank", "related_rank", "completion_rank")
C4_VERSION = 1
CONTROLS_VERSION = 1

# Printed (stderr, so stdout stays v5 byte for byte) whenever the legacy path runs.
LEGACY_NOTE = "legacy: C4 not checked"

# Expansion-identity keys a declared --rewrite-candidate may change: the config
# hash itself, and what that hash covers (model, prompt) plus the draw digest.
_CANDIDATE_KEYS = frozenset({"config_hash", "prompt_sha256", "model", "digest"})

# The roster and S5/N4 roles (ROSTER, ROLE_QUESTIONS, RosterEntry, RoleSpec)
# are imported from src.eval_roster above (item 9: one roster, keyed by the
# public v1 row id of the realistic-set row) and re-exported from here.


class C4Error(ValueError):
    """Two arms are not comparable under C4 cohort identity (CLI exit 2).

    Messages carry arm names, set labels, hashes and opaque row ids only --
    never question text -- so they are safe to print on a private run.
    """


class LegacyArmError(C4Error):
    """An arm has no C4 fields and ``--legacy`` was not given."""


# --------------------------------------------------------------------------
# Pure core: parsing
# --------------------------------------------------------------------------
def _split_sections(text: str) -> List[Tuple[str, List[str]]]:
    """Split a report into ``(heading_line, body_lines)`` pairs on ``## ``."""
    sections: List[Tuple[str, List[str]]] = []
    heading = ""
    body: List[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((heading, body))
            heading, body = line, []
        else:
            body.append(line)
    sections.append((heading, body))
    return sections


def _parse_ablation(body: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Parse one ``retrieval ablation`` table into ``{mode: metrics}``.

    The @k columns are read from the header rather than assumed, so a report
    written at a different ``top_k`` parses instead of silently mis-labelling.
    Parsing stops at the end of the mode table, because the same section also
    carries the "By type (hybrid)" table, whose columns are not rates @k.
    """
    ks: List[Tuple[str, int]] = []  # ("strict"|"related", k), in column order
    table: Dict[str, Dict[str, Any]] = {}
    started = False
    for line in body:
        if not line.startswith("|"):
            if started:
                break  # blank line / prose: the mode table has ended
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "Mode":
            started = True
            ks = []
            for cell in cells[1:]:
                if cell.startswith("S@"):
                    ks.append(("strict", int(cell[2:])))
                elif cell.startswith("R@"):
                    ks.append(("related", int(cell[2:])))
            continue
        if not started or not ks or set(cells[0]) == {"-"}:
            continue
        mode, values = cells[0], cells[1:]
        row: Dict[str, Any] = {"strict": {}, "related": {}}
        for (kind, k), value in zip(ks, values):
            row[kind][k] = float(value)
        tail = values[len(ks):]
        if len(tail) >= 3:
            row["mrr_strict"] = float(tail[0])
            row["mrr_related"] = float(tail[1])
            row["n"] = int(tail[2])
        table[mode] = row
    return table


def _parse_detail(body: Sequence[str]) -> List[Dict[str, Any]]:
    """Parse the per-question detail rows of one set.

    Rows the evaluator emits for refusal questions have a different shape and
    are skipped: only rows carrying strict/related ranks are retrieval rows.
    """
    rows: List[Dict[str, Any]] = []
    for line in body:
        m = _DETAIL_ROW.match(line)
        if m is None:
            continue
        rows.append(
            {
                "type": m.group("type"),
                "id": row_id_of(m.group("question")),
                "question": m.group("question"),
                "expected": list(ast.literal_eval(m.group("expected"))),
                "retrieved": list(ast.literal_eval(m.group("retrieved"))),
                "strict_rank": _opt_int(m.group("strict_rank")),
                "related_rank": _opt_int(m.group("related_rank")),
            }
        )
    return rows


def row_id_of(shown: str) -> str:
    """The row id of a detail row: the opaque id it shows, else ``public_v1_id``.

    A private report renders ``q:<12 hex>`` where a public one renders the
    question; a public report's id is recomputed from its question text.
    """
    return shown if _OPAQUE_ID.match(shown) else public_v1_id(shown)


def has_text(shown: str) -> bool:
    """True when a detail row shows question text rather than an opaque id."""
    return _OPAQUE_ID.match(shown) is None


def _opt_int(raw: str) -> Optional[int]:
    """``"None"`` -> None, digits -> int (the report's rank rendering)."""
    return None if raw == "None" else int(raw)


def _parse_provenance(body: Sequence[str]) -> Tuple[Optional[str], Dict[str, Dict[str, str]]]:
    """Read the embedding-model line and the ``Question sets:`` block."""
    model: Optional[str] = None
    sets: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    in_sets = False
    for line in body:
        m = _EMBEDDING_MODEL.match(line)
        if m is not None:
            model = m.group("model").strip()
            continue
        if line.strip() == "Question sets:":
            in_sets = True
            continue
        if not in_sets:
            continue
        field = _SET_FIELD.match(line)
        if field is not None and current is not None:
            sets[current][field.group("key")] = field.group("value").strip()
            continue
        label = _SET_LABEL.match(line)
        if label is not None:
            current = label.group("label").strip()
            sets.setdefault(current, {})
    return model, sets


def _assert_set_provenance(prov_sets: Mapping[str, Mapping[str, str]]) -> None:
    """Refuse a report recording any question set other than golden/realistic.

    The held-out exclusion in ``main`` only inspects CLI filenames; a report
    file can be named anything. This checks what the report itself says it was
    run on, so a held-out run cannot enter selection under a neutral filename
    (or be mistaken for golden by the label fallback in ``set_of_kind``).

    Raises:
        ValueError: if a recorded set has no path, or its path's basename is
            not one of :data:`ALLOWED_SET_BASENAMES`.
    """
    for label, fields in prov_sets.items():
        path = fields.get("path")
        if not path or os.path.basename(path) not in ALLOWED_SET_BASENAMES:
            raise ValueError(
                f"report records question set {label!r} with path {path!r}; "
                f"only {list(ALLOWED_SET_BASENAMES)} may enter selection "
                "(the held-out set is never used for selection)"
            )


def parse_report(text: str, *, require_offline: bool = True) -> Dict[str, Any]:
    """Parse one offline arm report into a comparable dict.

    Args:
        text: the full Markdown text of an arm report.
        require_offline: refuse a report without the disabled-expansion
            marker (the v5 comparability rule). ``main`` passes False only for
            an arm that has a C4 rows sidecar: its expansion identity is then
            checked by C4 instead (stricter: kind, model, prompt, config and
            digest), which is what lets two ``live`` arms or a declared
            ``--rewrite-candidate`` be compared at all.

    Returns:
        ``{"embedding_model": str|None, "expansion_disabled": bool,
        "top_k": int|None, "sets": {label: {"path", "sha256", "ablation",
        "questions"}}}`` (``top_k`` from the header's ``- top_k:`` line) where
        ``questions`` are the ``hybrid+rewrite`` per-question rows (raw hybrid
        by identity on an offline run), each carrying its row ``id``.

    Raises:
        ValueError: if the report does not disclose disabled expansion (it is
            then not comparable to the other arms) and ``require_offline``, or
            if a set's per-question detail was rendered under a mode other than
            ``hybrid+rewrite``, or if the report records a question set whose
            path is not the golden or realistic set file.
    """
    if text.startswith(V6_TITLE_PREFIX):
        if require_offline:
            raise C4Error(
                "a v6 report is compared only through its rows sidecar "
                "(<report>.rows.json); it has none, so it is refused (v5 rules cannot read it)"
            )
        return _parse_v6_report(text)
    expansion_disabled = EXPANSION_DISABLED_MARKER in text
    if require_offline and not expansion_disabled:
        raise ValueError(
            "report does not carry "
            f"{EXPANSION_DISABLED_MARKER!r} — arms whose expansion state "
            "differs are not comparable; re-run it with --skip-refusals "
            "--skip-completeness"
        )

    model: Optional[str] = None
    top_k: Optional[int] = None
    prov_sets: Dict[str, Dict[str, str]] = {}
    ablations: Dict[str, Dict[str, Dict[str, Any]]] = {}
    details: Dict[str, List[Dict[str, Any]]] = {}

    for heading, body in _split_sections(text):
        if heading == "":
            for line in body:
                m = _V5_TOP_K.match(line)
                if m is not None:
                    top_k = int(m.group("top_k"))
            continue
        if heading.startswith("## Provenance"):
            model, prov_sets = _parse_provenance(body)
            _assert_set_provenance(prov_sets)
            continue
        m = _ABLATION_HEADING.match(heading)
        if m is not None:
            ablations[m.group("label")] = _parse_ablation(body)
            continue
        m = _DETAIL_HEADING.match(heading)
        if m is not None:
            if m.group("mode") != DETAIL_MODE:
                raise ValueError(
                    f"set {m.group('label')!r}: per-question detail rendered "
                    f"under mode {m.group('mode')!r}, expected {DETAIL_MODE!r} "
                    "— the arm was not run with the bake-off's mode ablation"
                )
            details[m.group("label")] = _parse_detail(body)

    sets: Dict[str, Dict[str, Any]] = {}
    for label in sorted(set(prov_sets) | set(ablations) | set(details)):
        sets[label] = {
            "path": prov_sets.get(label, {}).get("path"),
            "sha256": prov_sets.get(label, {}).get("sha256"),
            "ablation": ablations.get(label, {}),
            "questions": details.get(label, []),
        }
    # Validate every set that will be consumed, not only those the provenance
    # section listed: a report with no provenance block would otherwise yield
    # sets with path=None that the label fallback lets into selection (C2).
    _assert_set_provenance(sets)
    return {
        "embedding_model": model,
        "expansion_disabled": expansion_disabled,
        "top_k": top_k,
        "sets": sets,
    }


def _parse_v6_report(text: str) -> Dict[str, Any]:
    """Parse a report v6: its sets from the ``## Cohort`` block (ranks come from the sidecar).

    The header's ``- top_k: N; hit cut-off k = K`` line is required and ``K``
    must be ``HIT_K``: a run retrieved to fewer than 6 chunks has no rank
    beyond its depth, so reading it at @6 would credit or penalise depth, not
    retrieval (merge gate round 2, Codex #2).

    Raises:
        C4Error: a v6 report with no cohort lines, no depth line, or a hit
            cut-off other than ``HIT_K``.
        ValueError: a recorded set that may not enter selection
            (:func:`_assert_set_provenance`).
    """
    model: Optional[str] = None
    depth: Optional[Tuple[int, int]] = None
    sets: Dict[str, Dict[str, Any]] = {}
    for heading, body in _split_sections(text):
        if heading == "":
            for line in body:
                m = _V6_MODEL.match(line)
                if m is not None:
                    model = m.group("model").strip()
                m = _V6_DEPTH.match(line)
                if m is not None:
                    depth = (int(m.group("top_k")), int(m.group("k")))
        elif heading.strip() == "## Cohort":
            for line in body:
                m = _V6_COHORT.match(line)
                if m is not None:
                    sets[m.group("label")] = {
                        "path": m.group("path"),
                        "sha256": m.group("sha256"),
                        "ablation": {},
                        "questions": [],
                    }
    if not sets:
        raise C4Error("v6 report has no cohort lines")
    if depth is None:
        raise C4Error("v6 report declares no retrieval depth (its '- top_k: N; hit cut-off k = K' line)")
    top_k, k = depth
    if k != HIT_K:
        raise C4Error(
            f"v6 report's hit cut-off k = {k} (top_k {top_k}) is not the selection's "
            f"@{HIT_K}: refused rather than re-read at @{HIT_K}"
        )
    _assert_set_provenance(sets)
    return {
        "embedding_model": model,
        "expansion_disabled": False,
        "report_version": 6,
        "top_k": top_k,
        "k": k,
        "sets": sets,
    }


def _fill_from_sidecar(arm: Dict[str, Any], name: str) -> None:
    """Fill a v6 arm's ablation headline and detail rows from its rows sidecar.

    Per set: ``ablation`` gets the row-level strict/related@6 and ``n`` of each
    of ``hybrid`` and ``hybrid+rewrite`` the sidecar has; ``questions`` the
    ``hybrid+rewrite`` rows, shown by id (``id_only``: v6 carries no question
    text) with no retrieved sections (``retrieved`` None).
    """
    index = index_rows(arm["sidecar"], name)
    for data in arm["sets"].values():
        for mode in ("hybrid", DETAIL_MODE):
            group = index.get((data["sha256"], mode))
            if not group:
                continue
            n = len(group)
            data["ablation"][mode] = {
                "strict": {HIT_K: sum(_hit_at_k(r["strict_rank"]) for r in group.values()) / n},
                "related": {HIT_K: sum(_hit_at_k(r["related_rank"]) for r in group.values()) / n},
                "n": n,
            }
        detail = index.get((data["sha256"], DETAIL_MODE), {})
        data["questions"] = [
            {
                "type": None,
                "id": rid,
                "question": rid,
                "id_only": True,
                "expected": [],
                "retrieved": None,
                "strict_rank": detail[rid]["strict_rank"],
                "related_rank": detail[rid]["related_rank"],
            }
            for rid in sorted(detail)
        ]


# --------------------------------------------------------------------------
# Pure core: comparison
# --------------------------------------------------------------------------
def set_of_kind(arm: Mapping[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    """Return the golden or realistic set of a parsed arm, or None.

    The golden set is labelled ``tuning`` in reports (``src/evaluator.py``
    names it by role, not by filename), so it is identified by the eval-set
    path it names, with the label as a fallback.
    """
    basename = {"golden": GOLDEN_BASENAME, "realistic": REALISTIC_BASENAME}[kind]
    fallback_label = {"golden": "tuning", "realistic": "realistic"}[kind]
    for label, data in arm["sets"].items():
        if data.get("path") and os.path.basename(data["path"]) == basename:
            return data
    for label, data in arm["sets"].items():
        if fallback_label in label:
            return data
    return None


def _hit_at_k(rank: Optional[int], k: int = HIT_K) -> bool:
    """A rank hits @k when it exists and is within the cutoff."""
    return rank is not None and rank <= k


def _headline_row(data: Optional[Mapping[str, Any]]) -> Dict[str, Optional[float]]:
    """strict@6 / related@6 / n for one set, read from the hybrid row.

    Offline, ``hybrid`` and ``hybrid+rewrite`` are the same numbers (expansion
    disabled); ``hybrid`` is the row the brief names as the primary metric, and
    ``hybrid+rewrite`` is only used if a report lacks it.
    """
    if not data:
        return {"strict_at_6": None, "related_at_6": None, "n": None}
    ablation = data.get("ablation") or {}
    row = ablation.get("hybrid") or ablation.get(DETAIL_MODE) or {}
    return {
        "strict_at_6": row.get("strict", {}).get(HIT_K),
        "related_at_6": row.get("related", {}).get(HIT_K),
        "n": row.get("n"),
    }


def _check_detail_count(name: str, kind: str, data: Mapping[str, Any]) -> None:
    """Raise if a set's detail-row count differs from the ``n`` its report states."""
    n = _headline_row(data)["n"]
    if n is None:
        raise ValueError(
            f"arm {name!r}: {kind} set has no reported n (missing ablation "
            "section?) — detail completeness cannot be checked"
        )
    if n != len(data["questions"]):
        raise ValueError(
            f"arm {name!r}: {kind} set reports n={n} but its per-question "
            f"detail has {len(data['questions'])} rows — truncated or "
            "mis-parsed report"
        )


def _require_golden_detail(name: str, arm: Mapping[str, Any]) -> None:
    """Raise unless ``arm`` has a non-empty golden detail list matching its n."""
    golden = set_of_kind(arm, "golden")
    if golden is None or not golden["questions"]:
        raise ValueError(
            f"arm {name!r} has no golden per-question detail; flip lists "
            "computed from it would be vacuously empty"
        )
    _check_detail_count(name, "golden", golden)


def _table(arms: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The selection table rows (read from each report's ablation section)."""
    return [
        {
            "arm": name,
            "embedding_model": arm.get("embedding_model"),
            "golden": _headline_row(set_of_kind(arm, "golden")),
            "realistic": _headline_row(set_of_kind(arm, "realistic")),
        }
        for name, arm in arms.items()
    ]


def _check_details(arms: Mapping[str, Mapping[str, Any]]) -> None:
    """The vacuous-pass guards: non-empty golden detail, counts match ``n``."""
    for name, arm in arms.items():
        _require_golden_detail(name, arm)
        realistic = set_of_kind(arm, "realistic")
        if realistic is not None:
            _check_detail_count(name, "realistic", realistic)


def compare(
    arms: Mapping[str, Mapping[str, Any]],
    baseline: str,
    *,
    legacy: bool = False,
    rewrite_candidate: Optional[str] = None,
    controls: Optional[Sequence[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Build the selection table and the golden per-question flip lists.

    Dispatches on C4 (D68): when EVERY arm carries a rows sidecar (the
    ``"sidecar"`` key :func:`load_arm` sets), rows are keyed by ``(set sha256,
    id)`` and cohort identity is enforced (:func:`compare_c4`). Otherwise the
    arms are ``legacy`` and are compared under today's v5 rules -- but only
    with ``legacy=True``.

    Args:
        arms: ``{arm_name: parsed_report}``, in the order they should appear.
        baseline: the arm name every other arm is compared against.
        legacy: accept arms without C4 fields (v5 rules, nothing new checked).
        rewrite_candidate: a declared candidate rewrite config hash (C4 only).
        controls: ``(set_sha256, id)`` control rows (C4 only; see
            :func:`load_controls`).

    Returns:
        ``{"baseline", "table", "flips", "c4"}`` (plus ``"control_flips"`` and
        ``"controls"`` on C4). ``table`` carries golden and realistic
        strict@6/related@6 per arm; ``flips`` carries, per non-baseline arm,
        every golden row that went ``hit_to_miss`` (strict, @6) or
        ``miss_to_hit``, plus ``unmatched`` rows present in one arm but not the
        other (legacy only: C4 refuses those). Legacy flips are the rows' shown
        text (question, or opaque id on a private report); C4 flips are ids.

    Raises:
        KeyError: if ``baseline`` is not among ``arms``.
        LegacyArmError: an arm without C4 fields and ``legacy`` is False.
        C4Error: any C4 refusal, or ``controls``/``rewrite_candidate`` given
            for legacy arms.
        ValueError: if any arm (baseline included) has no golden set, an empty
            golden question-detail list, or a detail count that disagrees with
            the ``n`` its own ablation table reports — each would make the
            flip lists vacuously empty, i.e. a clean pass proving nothing.
    """
    if baseline not in arms:
        raise KeyError(f"baseline arm {baseline!r} not among reports: {list(arms)}")
    legacy_arms = [name for name, arm in arms.items() if arm.get("sidecar") is None]
    if not legacy_arms:
        return compare_c4(
            arms, baseline, rewrite_candidate=rewrite_candidate, controls=controls
        )
    if not legacy:
        raise LegacyArmError(
            f"arm(s) {legacy_arms} carry no C4 rows sidecar (<report>.rows.json): "
            "legacy arms are refused without --legacy"
        )
    if controls is not None or rewrite_candidate is not None:
        raise C4Error("--controls and --rewrite-candidate need C4 sidecars on every arm")
    return _compare_legacy(arms, baseline)


def _compare_legacy(arms: Mapping[str, Mapping[str, Any]], baseline: str) -> Dict[str, Any]:
    """Today's v5 comparison: rows matched by the text each report shows."""
    table = _table(arms)
    _check_details(arms)

    base_golden = set_of_kind(arms[baseline], "golden")
    base_hits = {
        q["question"]: _hit_at_k(q["strict_rank"]) for q in base_golden["questions"]
    }

    flips: Dict[str, Dict[str, List[str]]] = {}
    for name, arm in arms.items():
        if name == baseline:
            continue
        golden = set_of_kind(arm, "golden")
        hit_to_miss, miss_to_hit, unmatched = [], [], []
        seen = set()
        for q in golden["questions"]:
            question = q["question"]
            seen.add(question)
            if question not in base_hits:
                unmatched.append(question)
                continue
            now = _hit_at_k(q["strict_rank"])
            before = base_hits[question]
            if before and not now:
                hit_to_miss.append(question)
            elif now and not before:
                miss_to_hit.append(question)
        unmatched.extend(q for q in base_hits if q not in seen)
        flips[name] = {
            "hit_to_miss": hit_to_miss,
            "miss_to_hit": miss_to_hit,
            "unmatched": unmatched,
        }
    return {"baseline": baseline, "table": table, "flips": flips, "c4": False}


# --------------------------------------------------------------------------
# C4 cohort identity (D68)
# --------------------------------------------------------------------------
def is_c4(doc: Any) -> bool:
    """True when ``doc`` (a sidecar or rank dump) carries every C4 field."""
    return isinstance(doc, Mapping) and all(field in doc for field in C4_FIELDS)


def _is_rank(value: Any) -> bool:
    """A recorded rank: a positive int, or None (a genuine miss)."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 1)


def index_rows(doc: Mapping[str, Any], name: str) -> Dict[Tuple[str, str], Dict[str, Dict[str, Any]]]:
    """Validate a C4 document and index its rows as ``{(set sha256, mode): {id: row}}``.

    Raises:
        C4Error: a wrong version, a malformed cohort or expansion block, a row
            missing a required field (or carrying a non-rank value), a row
            whose set is not one of the document's cohorts, or a duplicate
            ``(set sha256, mode, id)`` row.
    """
    if not is_c4(doc):
        missing = [f for f in C4_FIELDS if not isinstance(doc, Mapping) or f not in doc]
        raise C4Error(f"arm {name!r}: not a C4 document (missing {missing})")
    if doc["version"] != C4_VERSION:
        raise C4Error(f"arm {name!r}: unsupported C4 version {doc['version']!r}")
    if not isinstance(doc["scorer_version"], str) or not doc["scorer_version"]:
        raise C4Error(f"arm {name!r}: scorer_version must be a non-empty string")
    if not isinstance(doc["expansion"], Mapping) or "kind" not in doc["expansion"]:
        raise C4Error(f"arm {name!r}: expansion identity must be an object with a kind")
    cohorts = doc["cohorts"]
    if not isinstance(cohorts, list) or not cohorts:
        raise C4Error(f"arm {name!r}: no cohort blocks")
    shas: Set[str] = set()
    for cohort in cohorts:
        sha = cohort.get("sha256") if isinstance(cohort, Mapping) else None
        if not isinstance(sha, str) or not _SHA256.match(sha) or "cohort_fp" not in cohort:
            raise C4Error(f"arm {name!r}: a cohort block lacks sha256 or cohort_fp")
        if not _is_count(cohort.get("eligible")) or not (
            isinstance(cohort.get("eligible_fp"), str) and _SHA256.match(cohort["eligible_fp"])
        ):
            raise C4Error(f"arm {name!r}: set {sha[:12]}: cohort block lacks its eligible roster")
        if sha in shas:
            raise C4Error(f"arm {name!r}: set {sha[:12]} has two cohort blocks")
        shas.add(sha)
    if not isinstance(doc["rows"], list):
        raise C4Error(f"arm {name!r}: rows must be a list")
    index: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for i, row in enumerate(doc["rows"]):
        if not isinstance(row, Mapping):
            raise C4Error(f"arm {name!r}: rows[{i}] is not an object")
        missing = [f for f in ROW_FIELDS if f not in row]
        if missing:
            raise C4Error(
                f"arm {name!r}: rows[{i}] lacks {missing} -- missing evidence is not a MISS"
            )
        if not isinstance(row["id"], str) or not isinstance(row["mode"], str):
            raise C4Error(f"arm {name!r}: rows[{i}] id and mode must be strings")
        if not all(_is_rank(row[f]) for f in ("strict_rank", "related_rank", "completion_rank")):
            raise C4Error(f"arm {name!r}: rows[{i}] ({row['id']}) has a non-integer rank")
        if row["set_sha256"] not in shas:
            raise C4Error(
                f"arm {name!r}: rows[{i}] ({row['id']}) names a set with no cohort block"
            )
        group = index.setdefault((row["set_sha256"], row["mode"]), {})
        if row["id"] in group:
            raise C4Error(
                f"arm {name!r}: duplicate row {row['id']} in set "
                f"{row['set_sha256'][:12]} mode {row['mode']}"
            )
        group[row["id"]] = dict(row)
    _check_roster(cohorts, index, name)
    return index


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_roster(
    cohorts: Sequence[Mapping[str, Any]],
    index: Mapping[Tuple[str, str], Mapping[str, Any]],
    name: str,
) -> None:
    """Every mode must cover each cohort's eligible roster exactly (16A-1 merge gate #4).

    Exactly once is the duplicate check in :func:`index_rows`; this adds
    "exactly the eligible ids": a document whose rows lack an eligible row, or
    carry another id in its place, is refused even when the other arm has the
    same gap (the arm-vs-arm coverage check cannot see that).

    Raises:
        C4Error: an eligible cohort with no rows at all, or a (set, mode)
            group whose ids are not the cohort's roster.
    """
    modes = sorted({mode for _sha, mode in index})
    for cohort in cohorts:
        sha = cohort["sha256"]
        if cohort["eligible"] and not modes:
            raise C4Error(f"arm {name!r}: set {sha[:12]} has eligible rows but the document has none")
        for mode in modes:
            ids = index.get((sha, mode), {})
            if len(ids) != cohort["eligible"] or eligible_fp(ids) != cohort["eligible_fp"]:
                raise C4Error(
                    f"arm {name!r}: set {sha[:12]} mode {mode}: rows cover {len(ids)} ids, not "
                    f"the cohort's {cohort['eligible']} eligible ids (roster mismatch)"
                )


def cohort_labels(doc: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """``{set sha256: label}`` recorded in a C4 document's cohort blocks (dumps)."""
    return {c["sha256"]: c.get("label") for c in doc["cohorts"]}


def report_labels(arm: Mapping[str, Any]) -> Dict[Optional[str], str]:
    """``{set sha256: label}`` from a parsed report's provenance."""
    return {data.get("sha256"): label for label, data in arm["sets"].items()}


def _check_expansion(
    base: Mapping[str, Any], arm: Mapping[str, Any], name: str, rewrite_candidate: Optional[str]
) -> None:
    """Expansion identity must match, with the two permitted exceptions.

    Two ``live`` arms may differ in draw ``digest`` only (rewrite model,
    prompt and config identities equal). With ``--rewrite-candidate H``, an arm
    recording ``config_hash == H`` may differ from the baseline in exactly the
    declared config (``config_hash`` and what it covers -- ``model``,
    ``prompt_sha256`` -- plus the draw ``digest``); its ``kind`` and every
    other field must still match.

    Raises:
        C4Error: on any other difference.
    """
    if dict(base) == dict(arm):
        return
    keys = set(base) | set(arm)
    if (
        rewrite_candidate is not None
        and arm.get("config_hash") == rewrite_candidate
        and base.get("config_hash") != rewrite_candidate
        and base.get("kind") == arm.get("kind")
        and all(base.get(k) == arm.get(k) for k in keys - _CANDIDATE_KEYS)
    ):
        return
    if base.get("kind") == "live" and arm.get("kind") == "live":
        if all(base.get(k) == arm.get(k) for k in keys - {"digest"}):
            return
        raise C4Error(
            f"arm {name!r}: live expansion identity differs beyond the draw digest "
            "(rewrite model, prompt or config)"
        )
    raise C4Error(
        f"arm {name!r}: expansion identity differs from the baseline "
        f"(kind {base.get('kind')!r} vs {arm.get('kind')!r}, digest or config)"
    )


def check_c4_identity(
    base: Mapping[str, Any],
    arm: Mapping[str, Any],
    *,
    name: str,
    base_labels: Mapping[Any, Any],
    arm_labels: Mapping[Any, Any],
    rewrite_candidate: Optional[str] = None,
) -> None:
    """Refuse two C4 documents whose cohort identity differs.

    Checked unconditionally, before expansion: scorer version, absorbed-map
    hash, set hashes, each set's label and ``cohort_fp`` (plus schema, rows,
    families and the eligible roster). Then expansion identity (:func:`_check_expansion`).

    Raises:
        C4Error: on the first mismatch.
    """
    if base["scorer_version"] != arm["scorer_version"]:
        raise C4Error(
            f"arm {name!r}: scorer version {arm['scorer_version']!r} differs from "
            f"the baseline's {base['scorer_version']!r}"
        )
    if base["absorbed_map_sha256"] != arm["absorbed_map_sha256"]:
        raise C4Error(f"arm {name!r}: absorbed-map hash differs from the baseline's")
    base_sets = {c["sha256"]: c for c in base["cohorts"]}
    arm_sets = {c["sha256"]: c for c in arm["cohorts"]}
    if set(base_sets) != set(arm_sets):
        only = sorted(s[:12] for s in set(base_sets) ^ set(arm_sets))
        raise C4Error(f"arm {name!r}: set hashes differ from the baseline's ({only})")
    for sha in sorted(base_sets):
        if base_labels.get(sha) != arm_labels.get(sha):
            raise C4Error(
                f"arm {name!r}: set {sha[:12]} is labelled {arm_labels.get(sha)!r}, "
                f"the baseline {base_labels.get(sha)!r}"
            )
        for key in ("cohort_fp", "schema", "rows", "families", "eligible", "eligible_fp"):
            if base_sets[sha].get(key) != arm_sets[sha].get(key):
                raise C4Error(
                    f"arm {name!r}: set {sha[:12]} {key} differs from the baseline's"
                )
    _check_expansion(base["expansion"], arm["expansion"], name, rewrite_candidate)


def _check_coverage(
    base_idx: Mapping[Tuple[str, str], Mapping[str, Any]],
    arm_idx: Mapping[Tuple[str, str], Mapping[str, Any]],
    name: str,
) -> None:
    """Both arms must cover the same ids, exactly once, in every (set, mode).

    (Exactly once is :func:`index_rows`' duplicate check.)

    Raises:
        C4Error: a (set, mode) group present on one side only, or any id
            missing from or extra to the arm.
    """
    if set(base_idx) != set(arm_idx):
        diff = sorted(f"{s[:12]}/{m}" for s, m in set(base_idx) ^ set(arm_idx))
        raise C4Error(f"arm {name!r}: (set, mode) groups differ from the baseline's: {diff}")
    for key in sorted(base_idx):
        missing = sorted(set(base_idx[key]) - set(arm_idx[key]))
        extra = sorted(set(arm_idx[key]) - set(base_idx[key]))
        if missing or extra:
            raise C4Error(
                f"arm {name!r}: set {key[0][:12]} mode {key[1]}: "
                f"missing rows {missing}, extra rows {extra}"
            )


def load_controls(path: str) -> List[Tuple[str, str]]:
    """Load a ``--controls`` file as ``[(set sha256, id), ...]``.

    Format: ``{"version": 1, "controls": [{"set_sha256": <hex>, "ids":
    [<id>, ...]}, ...]}``.

    Raises:
        C4Error: invalid JSON, a wrong version, an empty control list, an
            entry without a set hash or with no ids, or a duplicated control.
    """
    with open(path, encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except ValueError as exc:
            raise C4Error("controls file is not valid JSON") from exc
    return parse_controls(doc)


def parse_controls(doc: Any) -> List[Tuple[str, str]]:
    """Validate an in-memory controls document (see :func:`load_controls`)."""
    if not isinstance(doc, Mapping) or doc.get("version") != CONTROLS_VERSION:
        raise C4Error('controls must be {"version": 1, "controls": [...]}')
    entries = doc.get("controls")
    if not isinstance(entries, list) or not entries:
        raise C4Error("controls must be non-empty")
    out: List[Tuple[str, str]] = []
    for i, entry in enumerate(entries):
        sha = entry.get("set_sha256") if isinstance(entry, Mapping) else None
        ids = entry.get("ids") if isinstance(entry, Mapping) else None
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise C4Error(f"controls[{i}]: set_sha256 must be 64 lowercase hex")
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids):
            raise C4Error(f"controls[{i}]: ids must be a non-empty list of row ids")
        for rid in ids:
            if (sha, rid) in out:
                raise C4Error(f"controls[{i}]: duplicate control {rid}")
            out.append((sha, rid))
    return out


def _resolve_controls(
    controls: Sequence[Tuple[str, str]],
    idx: Mapping[Tuple[str, str], Mapping[str, Any]],
    mode: str,
    name: str,
) -> None:
    """Every control must name a row the arm scored at ``mode``.

    Raises:
        C4Error: for the first control that does not resolve.
    """
    for sha, rid in controls:
        if rid not in idx.get((sha, mode), {}):
            raise C4Error(
                f"control {rid} (set {sha[:12]}) does not resolve in arm {name!r} at mode {mode}"
            )


def _flip_lists(
    base_rows: Mapping[str, Mapping[str, Any]],
    arm_rows: Mapping[str, Mapping[str, Any]],
    top_k: int,
) -> Tuple[List[str], List[str]]:
    """Sorted ids that went strict HIT->MISS and MISS->HIT @top_k."""
    hit_to_miss, miss_to_hit = [], []
    for rid in sorted(base_rows):
        before = _hit_at_k(base_rows[rid]["strict_rank"], top_k)
        now = _hit_at_k(arm_rows[rid]["strict_rank"], top_k)
        if before and not now:
            hit_to_miss.append(rid)
        elif now and not before:
            miss_to_hit.append(rid)
    return hit_to_miss, miss_to_hit


def _control_flip_lists(
    controls: Sequence[Tuple[str, str]],
    base_idx: Mapping[Tuple[str, str], Mapping[str, Any]],
    arm_idx: Mapping[Tuple[str, str], Mapping[str, Any]],
    mode: str,
    top_k: int,
) -> Tuple[List[str], List[str]]:
    """Control ids that flipped (each resolved already), in control order."""
    h2m_all: List[str] = []
    m2h_all: List[str] = []
    for sha, rid in controls:
        group = (sha, mode)
        h2m, m2h = _flip_lists({rid: base_idx[group][rid]}, {rid: arm_idx[group][rid]}, top_k)
        h2m_all.extend(h2m)
        m2h_all.extend(m2h)
    return h2m_all, m2h_all


def _check_report_matches_sidecar(name: str, arm: Mapping[str, Any]) -> None:
    """A sidecar must belong to its report: same set hashes, same golden ids.

    Raises:
        C4Error: the report and its sidecar record different sets, the
            sidecar has no golden ``hybrid+rewrite`` rows, the report repeats a
            golden row, or (v1 sets) the report's golden detail ids differ
            from the sidecar's rows.
    """
    sidecar = arm["sidecar"]
    report_shas = {data.get("sha256") for data in arm["sets"].values()}
    sidecar_shas = {c["sha256"] for c in sidecar["cohorts"]}
    if report_shas != sidecar_shas:
        raise C4Error(f"arm {name!r}: the rows sidecar records other sets than its report")
    golden = set_of_kind(arm, "golden")
    ids = [q["id"] for q in golden["questions"]]
    if len(set(ids)) != len(ids):
        raise C4Error(f"arm {name!r}: duplicate golden row in the report's detail")
    schema = next(
        (c.get("schema") for c in sidecar["cohorts"] if c["sha256"] == golden["sha256"]), None
    )
    rows = arm["_c4_index"].get((golden["sha256"], DETAIL_MODE))
    if rows is None:
        raise C4Error(f"arm {name!r}: the rows sidecar has no golden {DETAIL_MODE} rows")
    if schema == 1 and set(rows) != set(ids):
        raise C4Error(
            f"arm {name!r}: golden rows differ between the report and its sidecar "
            f"(missing {sorted(set(ids) - set(rows))}, extra {sorted(set(rows) - set(ids))})"
        )


def _check_depth(arms: Mapping[str, Mapping[str, Any]]) -> None:
    """All C4 arms share one retrieval depth, deep enough for @``HIT_K`` (round 2, Codex #2).

    Ranks stop at the depth a run retrieved to, so arms retrieved to
    different depths -- or to fewer than ``HIT_K`` chunks -- give different
    @6 figures for identical retrieval. ``top_k`` is None only for a v5
    report without its header line; unknown then matches only unknown.

    Raises:
        C4Error: a ``top_k`` below ``HIT_K``, or ``top_k`` differing between arms.
    """
    depths = {name: arm.get("top_k") for name, arm in arms.items()}
    for name, top_k in depths.items():
        if top_k is not None and top_k < HIT_K:
            raise C4Error(f"arm {name!r}: top_k {top_k} is below the @{HIT_K} hit cut-off")
    if len(set(depths.values())) > 1:
        raise C4Error(f"retrieval depth (top_k) differs between arms: {depths}")


def compare_c4(
    arms: Mapping[str, Mapping[str, Any]],
    baseline: str,
    *,
    rewrite_candidate: Optional[str] = None,
    controls: Optional[Sequence[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """C4 comparison of arm reports that all carry rows sidecars.

    Rows are keyed by ``(set sha256, id)`` from each arm's sidecar (row order
    is irrelevant). Golden flips are read at the ``hybrid+rewrite`` mode;
    controls are reported apart in ``control_flips`` and left out of
    ``flips``.

    Raises:
        KeyError: if ``baseline`` is not among ``arms``.
        C4Error: any identity, depth, coverage, report/sidecar or control refusal.
        ValueError: the v5 vacuous-pass guards (see :func:`compare`).
    """
    if baseline not in arms:
        raise KeyError(f"baseline arm {baseline!r} not among reports: {list(arms)}")
    if controls is not None and not controls:
        raise C4Error("controls must be non-empty")
    _check_depth(arms)
    table = _table(arms)
    _check_details(arms)
    indexed: Dict[str, Dict[str, Any]] = {}
    for name, arm in arms.items():
        indexed[name] = dict(arm)
        indexed[name]["_c4_index"] = index_rows(arm["sidecar"], name)
        _check_report_matches_sidecar(name, indexed[name])

    base = indexed[baseline]
    base_idx = base["_c4_index"]
    golden_sha = set_of_kind(base, "golden")["sha256"]
    control_list = list(controls or ())
    control_set = set(control_list)
    for name, arm in indexed.items():
        if control_list:
            _resolve_controls(control_list, arm["_c4_index"], DETAIL_MODE, name)
        if name == baseline:
            continue
        check_c4_identity(
            base["sidecar"],
            arm["sidecar"],
            name=name,
            base_labels=report_labels(base),
            arm_labels=report_labels(arm),
            rewrite_candidate=rewrite_candidate,
        )
        _check_coverage(base_idx, arm["_c4_index"], name)

    flips: Dict[str, Dict[str, List[str]]] = {}
    control_flips: Dict[str, Dict[str, List[str]]] = {}
    key = (golden_sha, DETAIL_MODE)
    main_base = {r: v for r, v in base_idx[key].items() if (golden_sha, r) not in control_set}
    for name, arm in indexed.items():
        if name == baseline:
            continue
        arm_idx = arm["_c4_index"]
        hit_to_miss, miss_to_hit = _flip_lists(main_base, arm_idx[key], HIT_K)
        flips[name] = {"hit_to_miss": hit_to_miss, "miss_to_hit": miss_to_hit, "unmatched": []}
        c_h2m, c_m2h = _control_flip_lists(control_list, base_idx, arm_idx, DETAIL_MODE, HIT_K)
        control_flips[name] = {"hit_to_miss": c_h2m, "miss_to_hit": c_m2h}
    return {
        "baseline": baseline,
        "table": table,
        "flips": flips,
        "control_flips": control_flips,
        "controls": [rid for _sha, rid in control_list],
        "c4": True,
    }


def class_movement(
    arm: Mapping[str, Any], roster: Sequence[RosterEntry] = ROSTER
) -> Dict[str, Any]:
    """Which roster questions this arm now hits, per D54 failure class.

    A roster question counts as recovered when it is a strict **or** related
    HIT within the top 6 — the roster is about whether the content surfaces at
    all, which is what the vocabulary-gap and near-miss classes describe.
    Roster rows are matched by row id (``src.eval_roster``, item 9).

    Args:
        arm: a parsed arm report.
        roster: roster entries to score; defaults to ``src.eval_roster.ROSTER``.

    Returns:
        ``{"entries": [...], "by_class": {class: {"n", "hits", "misses"}},
        "unmatched": [row_id, ...]}`` — ``unmatched`` names roster ids that
        matched no row in the arm's realistic set (a roster that has drifted
        from the eval set, which must be noticed, not averaged away). ``hits``
        and ``misses`` hold the matched row's shown text; an unmatched roster
        row is listed by its id.
    """
    realistic = set_of_kind(arm, "realistic") or {"questions": []}
    entries: List[Dict[str, Any]] = []
    by_class: Dict[str, Dict[str, Any]] = {}
    unmatched: List[str] = []

    for item in roster:
        match = next(
            (q for q in realistic["questions"] if q.get("id") == item.row_id),
            None,
        )
        bucket = by_class.setdefault(
            item.failure_class, {"n": 0, "hits": [], "misses": []}
        )
        bucket["n"] += 1
        entry: Dict[str, Any] = {
            "class": item.failure_class,
            "row_id": item.row_id,
            "expected": list(item.expected),
            "question": None if match is None else match["question"],
            "strict_rank": None if match is None else match["strict_rank"],
            "related_rank": None if match is None else match["related_rank"],
            "hit_at_6": False,
        }
        if match is None:
            unmatched.append(item.row_id)
            bucket["misses"].append(item.row_id)
        else:
            entry["hit_at_6"] = _hit_at_k(match["strict_rank"]) or _hit_at_k(
                match["related_rank"]
            )
            (bucket["hits"] if entry["hit_at_6"] else bucket["misses"]).append(
                match["question"]
            )
        entries.append(entry)
    return {"entries": entries, "by_class": by_class, "unmatched": unmatched}


def _covers(retrieved: Sequence[str], group: str) -> bool:
    """Equal-or-descendant match: ``2.2.1`` or ``2.2.1.5`` cover ``2.2.1``.

    Deliberately NOT ``_sections_related``, which is symmetric prefix matching:
    a single retrieved ``2.2`` is "related" to both 2.2.1 and 2.2.2 and would
    tick both role groups at once (Codex C3). Only the group itself or a
    descendant of it counts.
    """
    return any(s == group or s.startswith(group + ".") for s in retrieved)


def role_coverage(
    arm: Mapping[str, Any], roles: Sequence[RoleSpec] = ROLE_QUESTIONS
) -> Dict[str, Any]:
    """Per-role-group coverage for the both-sides comparison questions.

    Args:
        arm: a parsed arm report.
        roles: the rows and their role groups; defaults to S5 and N4
            (``src.eval_roster``), matched by row id.

    Returns:
        ``{name: {"question", "found", "retrieved", "groups": {group: bool},
        "both": bool}}``. ``both`` is True only when every group is covered
        independently.
    """
    realistic = set_of_kind(arm, "realistic") or {"questions": []}
    out: Dict[str, Any] = {}
    for role in roles:
        match = next(
            (q for q in realistic["questions"] if q.get("id") == role.row_id),
            None,
        )
        retrieved = [] if match is None else match["retrieved"]
        available = retrieved is not None  # a v6 row records no retrieved sections
        groups = {g: available and _covers(retrieved, g) for g in role.groups}
        out[role.name] = {
            "question": None if match is None else match["question"],
            "found": match is not None,
            "available": available,
            "retrieved": retrieved if available else [],
            "groups": groups,
            "both": match is not None and available and all(groups.values()),
        }
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _fmt(value: Optional[float]) -> str:
    """Render an optional rate cell."""
    return "n/a" if value is None else f"{value:.3f}"


def _question_texts(arms: Mapping[str, Mapping[str, Any]]) -> Dict[str, str]:
    """``{row id: question text}`` from every parsed report that shows text."""
    texts: Dict[str, str] = {}
    for arm in arms.values():
        for data in arm["sets"].values():
            for q in data["questions"]:
                if not q.get("id_only") and has_text(q["question"]):
                    texts.setdefault(q["id"], q["question"])
    return texts


def render(
    arms: Mapping[str, Mapping[str, Any]],
    baseline: str,
    *,
    legacy: bool = False,
    rewrite_candidate: Optional[str] = None,
    controls: Optional[Sequence[Tuple[str, str]]] = None,
    show_text: bool = True,
) -> str:
    """Render the full selection evidence as Markdown.

    Args:
        arms: ``{arm_name: parsed_report}`` (with ``"sidecar"`` for C4 arms).
        baseline: the baseline arm name.
        legacy: accept legacy arms (v5 rules; see :func:`compare`).
        rewrite_candidate: declared candidate rewrite config hash (C4).
        controls: ``(set_sha256, id)`` control rows (C4).
        show_text: False on a private run: every row renders as its opaque id
            only. On a public legacy run (True) the output is v5 byte for
            byte; on a public C4 run a row renders as ``id — question`` when a
            report shows its text.

    Raises:
        Whatever :func:`compare` raises.
    """
    comparison = compare(
        arms, baseline, legacy=legacy, rewrite_candidate=rewrite_candidate, controls=controls
    )
    c4 = comparison["c4"]
    texts = _question_texts(arms) if (c4 and show_text) else {}

    def show(item: str, *, is_id: bool = False) -> str:
        # ``item`` is either a row's shown text (question or opaque id) or,
        # with ``is_id``, a C4 row id (v2 ids are not ``q:`` hashes, so they
        # are never re-hashed). A private run (show_text False) only ever
        # renders ids.
        rid = item if is_id else row_id_of(item)
        if not show_text:
            return rid
        if not c4:
            return item
        # an id is never its own text (a v2 id is not a ``q:`` hash)
        text = texts.get(rid) or (item if not is_id and has_text(item) else None)
        return f"{rid} — {text}" if text else rid

    out: List[str] = ["# Bake-off selection evidence", ""]
    if c4:
        out.append(
            f"Baseline arm: `{baseline}`. C4 cohort identity checked (set hashes, labels, "
            "cohort_fp, scorer version, absorbed map, expansion identity);"
        )
        out.append(
            "per-question rows are keyed by (set sha256, id) from each arm's rows "
            "sidecar, read at the `hybrid+rewrite` mode."
        )
    else:
        out.append(f"Baseline arm: `{baseline}`. All arms offline (expansion disabled);")
        out.append(
            "the per-question rows are the `hybrid+rewrite` section, which offline "
            "IS raw hybrid."
        )
    out.append("")

    out.append("## 1. Selection table")
    out.append("")
    out.append(
        "| Arm | Embedding model | Golden S@6 | Golden R@6 | Realistic S@6 | Realistic R@6 |"
    )
    out.append("| --- | --- | --- | --- | --- | --- |")
    for row in comparison["table"]:
        out.append(
            f"| {row['arm']} | {row['embedding_model'] or 'n/a'} | "
            f"{_fmt(row['golden']['strict_at_6'])} | {_fmt(row['golden']['related_at_6'])} | "
            f"{_fmt(row['realistic']['strict_at_6'])} | {_fmt(row['realistic']['related_at_6'])} |"
        )
    out.append("")

    out.append(f"## 2. Golden per-question strict flips vs `{baseline}`")
    out.append("")
    for name in arms:
        if name == baseline:
            continue
        flip = comparison["flips"][name]
        out.append(f"### {name}")
        out.append("")
        out.append(f"HIT->MISS ({len(flip['hit_to_miss'])}) — a single one disqualifies:")
        out.extend(f"- {show(q, is_id=c4)}" for q in flip["hit_to_miss"] or [])
        if not flip["hit_to_miss"]:
            out.append("- none")
        out.append("")
        out.append(f"MISS->HIT ({len(flip['miss_to_hit'])}):")
        out.extend(f"- {show(q, is_id=c4)}" for q in flip["miss_to_hit"] or [])
        if not flip["miss_to_hit"]:
            out.append("- none")
        if flip["unmatched"]:
            out.append("")
            out.append(
                f"UNMATCHED ({len(flip['unmatched'])}) — present in only one arm, "
                "so the sets differ; investigate before trusting the flips:"
            )
            out.extend(f"- {show(q, is_id=c4)}" for q in flip["unmatched"])
        if c4 and comparison["controls"]:
            control = comparison["control_flips"][name]
            out.append("")
            out.append(
                f"Controls ({len(comparison['controls'])}, reported apart): "
                f"HIT->MISS {[show(q, is_id=c4) for q in control['hit_to_miss']] or 'none'}; "
                f"MISS->HIT {[show(q, is_id=c4) for q in control['miss_to_hit']] or 'none'}"
            )
        out.append("")

    out.append("## 3. Per-class movement (roster from the brief)")
    out.append("")
    out.append("| Arm | Class | Recovered @6 (strict or related) | Questions |")
    out.append("| --- | --- | --- | --- |")
    unmatched_notes: List[str] = []
    for name, arm in arms.items():
        movement = class_movement(arm)
        for failure_class, bucket in movement["by_class"].items():
            hits = "; ".join(show(q) for q in bucket["hits"]) or "—"
            out.append(
                f"| {name} | {failure_class} | {len(bucket['hits'])}/{bucket['n']} | {hits} |"
            )
        if movement["unmatched"]:
            unmatched_notes.append(
                f"- {name}: roster ids matching no question — "
                + "; ".join(movement["unmatched"])
            )
    out.append("")
    if unmatched_notes:
        out.append("Roster drift (a roster id no longer matches the eval set):")
        out.extend(unmatched_notes)
        out.append("")

    out.append("## 4. S5 / N4 both-role coverage")
    out.append("")
    out.append(
        "Equal-or-descendant match per group (`s == g or s.startswith(g + \".\")`): "
        "one generic `2.2` satisfies neither group."
    )
    out.append("")
    groups = sorted({g for role in ROLE_QUESTIONS for g in role.groups})
    header = " | ".join(f"{g}" for g in groups)
    out.append(f"| Arm | Question | {header} | Both |")
    out.append("| --- | --- | " + " | ".join(["---"] * (len(groups) + 1)) + " |")
    for name, arm in arms.items():
        for role_name, cover in role_coverage(arm).items():
            if not cover["available"]:
                cells = " | ".join("n/a" for _g in groups)
                out.append(f"| {name} | {role_name} (v6: no retrieved sections recorded) | {cells} | n/a |")
                continue
            cells = " | ".join(
                ("yes" if cover["groups"].get(g) else "no") for g in groups
            )
            found = "" if cover["found"] else " (question not found)"
            out.append(
                f"| {name} | {role_name}{found} | {cells} | "
                f"{'yes' if cover['both'] else 'no'} |"
            )
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Production-rank dumps (w_sweep --ranks-out)
# --------------------------------------------------------------------------
def compare_prod_ranks(
    baseline: Mapping[str, Any],
    arm: Mapping[str, Any],
    weight: float = SHIPPED_WEIGHT,
    label: str = "golden",
    top_k: int = HIT_K,
    *,
    legacy: bool = False,
    rewrite_candidate: Optional[str] = None,
    controls: Optional[Sequence[Tuple[str, str]]] = None,
    name: str = "arm",
) -> Dict[str, Any]:
    """Golden strict@k flips between two ``w_sweep --ranks-out`` dumps.

    This is selection disqualifier #2 (production configuration — surface
    rewrites plus the intent arm at the shipped weight, replayed from the
    committed expansion cache): an arm can be clean on raw queries and regress
    under expansion, so raw-hybrid flips alone do not measure what ships.

    **C4** (both dumps carry the C4 fields, D68): rows are keyed by ``(set
    sha256, id)`` at mode ``W=<weight>``; cohort identity is enforced
    (:func:`check_c4_identity`, labels from the dumps' cohort blocks) and both
    dumps must cover the same ids exactly once in every (set, mode) -- the
    old silent skip of a row missing from the arm is gone. ``controls`` must
    resolve in both dumps and are reported apart.

    **Legacy** (either dump lacks C4 fields, e.g. a pre-16A dump): refused
    unless ``legacy=True``; then today's v5 rule -- rows matched by their
    ``W=<w>|<label>|<i>`` key, a one-sided key reported in ``unmatched``.

    Args:
        baseline: parsed JSON of the baseline arm's ranks dump.
        arm: parsed JSON of the candidate arm's ranks dump.
        weight: the intent weight to compare at (the shipped W).
        label: the set label to compare (selection uses the golden set only).
        top_k: strict hit cutoff.
        legacy: accept legacy dumps under v5 rules.
        rewrite_candidate: declared candidate rewrite config hash (C4).
        controls: ``(set_sha256, id)`` control rows (C4).
        name: the arm's name, for refusal messages.

    Returns:
        ``{"weight", "label", "flips": [...], "gains": [...], "unmatched":
        [...], "c4": bool}`` where flips are baseline strict-HIT@k rows that
        the arm strict-MISSes, gains the reverse. Legacy rows carry ``key`` and
        the question text; C4 rows carry ``id`` and ``set_sha256`` (never
        text), and C4 adds ``control_flips`` / ``control_gains``.

    Raises:
        ValueError: (legacy) if the baseline or the arm has no row for the
            prefix, or a row lacks ``strict_rank``.
        LegacyArmError: a legacy dump without ``legacy=True``.
        C4Error: any C4 refusal (including no rows at the weight).
    """
    if is_c4(baseline) and is_c4(arm):
        return _compare_prod_ranks_c4(
            baseline, arm, weight, label, top_k,
            rewrite_candidate=rewrite_candidate, controls=controls, name=name,
        )
    if not legacy:
        side = "baseline" if not is_c4(baseline) else "arm"
        raise LegacyArmError(
            f"{side} production-rank dump ({name!r}) has no C4 fields: legacy "
            "dumps are refused without --legacy"
        )
    if controls is not None or rewrite_candidate is not None:
        raise C4Error("--controls and --rewrite-candidate need C4 dumps on both sides")
    return _compare_prod_ranks_legacy(baseline, arm, weight, label, top_k)


def _compare_prod_ranks_legacy(
    baseline: Mapping[str, Any], arm: Mapping[str, Any], weight: float, label: str, top_k: int
) -> Dict[str, Any]:
    """Today's v5 rule for two dumps (rows keyed ``W=<w>|<label>|<i>``)."""
    prefix = f"W={weight}|{label}|"
    base_rows = {k: v for k, v in baseline.get("ranks", {}).items() if k.startswith(prefix)}
    arm_rows = {k: v for k, v in arm.get("ranks", {}).items() if k.startswith(prefix)}
    flips: List[Dict[str, Any]] = []
    gains: List[Dict[str, Any]] = []
    if not base_rows or not arm_rows:
        raise ValueError(
            f"no production-rank rows match {prefix!r} in the "
            f"{'baseline' if not base_rows else 'arm'} dump — an empty "
            "comparison would pass vacuously (wrong weight/label, or a "
            "truncated dump?)"
        )
    unmatched = sorted(set(base_rows) ^ set(arm_rows))
    # An absent strict_rank is missing evidence, not a recorded MISS (an
    # explicit JSON null is a genuine miss and stays accepted).
    for side, rows in (("baseline", base_rows), ("arm", arm_rows)):
        for key, row in rows.items():
            if "strict_rank" not in row:
                raise ValueError(
                    f"{side} production-rank row {key!r} lacks strict_rank — "
                    "missing evidence is not a recorded MISS"
                )
    for key, brow in base_rows.items():
        arow = arm_rows.get(key)
        if arow is None:
            continue  # v5 rule (legacy only): reported in ``unmatched`` above
        b_hit = _hit_at_k(brow.get("strict_rank"), top_k)
        a_hit = _hit_at_k(arow.get("strict_rank"), top_k)
        row = {
            "key": key,
            "question": brow.get("question"),
            "baseline_rank": brow.get("strict_rank"),
            "arm_rank": arow.get("strict_rank"),
        }
        if b_hit and not a_hit:
            flips.append(row)
        elif a_hit and not b_hit:
            gains.append(row)
    return {
        "weight": weight,
        "label": label,
        "flips": sorted(flips, key=lambda r: r["key"]),
        "gains": sorted(gains, key=lambda r: r["key"]),
        "unmatched": unmatched,
        "c4": False,
    }


def _compare_prod_ranks_c4(
    baseline: Mapping[str, Any],
    arm: Mapping[str, Any],
    weight: float,
    label: str,
    top_k: int,
    *,
    rewrite_candidate: Optional[str],
    controls: Optional[Sequence[Tuple[str, str]]],
    name: str,
) -> Dict[str, Any]:
    """C4 rule for two dumps (see :func:`compare_prod_ranks`)."""
    if controls is not None and not controls:
        raise C4Error("controls must be non-empty")
    base_idx = index_rows(baseline, "baseline")
    arm_idx = index_rows(arm, name)
    check_c4_identity(
        baseline, arm, name=name,
        base_labels=cohort_labels(baseline), arm_labels=cohort_labels(arm),
        rewrite_candidate=rewrite_candidate,
    )
    _check_coverage(base_idx, arm_idx, name)
    shas = [sha for sha, lab in cohort_labels(baseline).items() if lab == label]
    if len(shas) != 1:
        raise C4Error(f"no single set labelled {label!r} in the production-rank dumps")
    mode = f"W={weight}"
    key = (shas[0], mode)
    if not base_idx.get(key):
        raise C4Error(
            f"no production-rank rows for set {label!r} at {mode} — an empty "
            "comparison would pass vacuously (wrong weight/label, or a truncated dump?)"
        )
    control_list = list(controls or ())
    for side_name, idx in (("baseline", base_idx), (name, arm_idx)):
        _resolve_controls(control_list, idx, mode, side_name)
    control_set = set(control_list)

    def rows(ids: Sequence[str], sha: str) -> List[Dict[str, Any]]:
        group = (sha, mode)
        return [
            {
                "id": rid,
                "set_sha256": sha,
                "baseline_rank": base_idx[group][rid]["strict_rank"],
                "arm_rank": arm_idx[group][rid]["strict_rank"],
            }
            for rid in ids
        ]

    main_base = {r: v for r, v in base_idx[key].items() if (shas[0], r) not in control_set}
    flip_ids, gain_ids = _flip_lists(main_base, arm_idx[key], top_k)
    control_flips: List[Dict[str, Any]] = []
    control_gains: List[Dict[str, Any]] = []
    for sha, rid in control_list:
        h2m, m2h = _flip_lists(
            {rid: base_idx[(sha, mode)][rid]}, {rid: arm_idx[(sha, mode)][rid]}, top_k
        )
        control_flips.extend(rows(h2m, sha))
        control_gains.extend(rows(m2h, sha))
    return {
        "weight": weight,
        "label": label,
        "flips": rows(flip_ids, shas[0]),
        "gains": rows(gain_ids, shas[0]),
        "unmatched": [],
        "control_flips": control_flips,
        "control_gains": control_gains,
        "c4": True,
    }


# --------------------------------------------------------------------------
# Manifest + private destinations
# --------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    """SHA-256 of a file, read in chunks (reports are small; be tidy anyway)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    arms: Mapping[str, Mapping[str, Any]],
    paths: Mapping[str, str],
    baseline: str,
    *,
    prod_rank_paths: Optional[Sequence[str]] = None,
    shipped_weight: float = SHIPPED_WEIGHT,
    expansion_cache_path: Optional[str] = None,
    command_line: Optional[Sequence[str]] = None,
    legacy: Optional[bool] = None,
    c4: Optional[bool] = None,
    controls_path: Optional[str] = None,
    rewrite_candidate: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe exactly which artifacts produced this comparison.

    Args:
        arms: ``{arm_name: parsed_report}``.
        paths: ``{arm_name: report_path}``.
        baseline: the baseline arm name.
        prod_rank_paths: the production-rank dumps consumed (baseline first);
            each is recorded with its sha256.
        shipped_weight: the intent weight the production ranks were read at.
        expansion_cache_path: the expansion cache the dumps were replayed from;
            recorded with its sha256 when the file exists, else path-only with
            a null sha256, and null when not supplied.
        command_line: the exact argv of this run (``sys.argv``).
        legacy: whether ``--legacy`` was given (recorded when not None).
        c4: whether the report comparison ran under C4 (recorded when not None).
        controls_path: the ``--controls`` file (recorded with its sha256).
        rewrite_candidate: the declared ``--rewrite-candidate`` config hash.

    Returns:
        A JSON-ready dict carrying, per arm, the report path and its sha256,
        the embedding model, each eval set's path + sha256 as recorded in the
        report's provenance, and its rows sidecar (path + sha256, or null) —
        so held-out absence is checkable from the artifact rather than
        asserted from shell history. Plus ``prod_ranks`` (path + sha256 each),
        ``shipped_weight``, ``expansion_cache`` and ``command_line``, and the
        C4 keys given.
    """
    manifest: Dict[str, Any] = {"baseline": baseline, "arms": {}}
    for name, arm in arms.items():
        side = sidecar_path(paths[name])
        manifest["arms"][name] = {
            "report_path": paths[name],
            "report_sha256": sha256_file(paths[name]),
            "embedding_model": arm.get("embedding_model"),
            "eval_sets": [
                {"label": label, "path": data.get("path"), "sha256": data.get("sha256")}
                for label, data in sorted(arm["sets"].items())
            ],
            "sidecar": (
                {"path": side, "sha256": sha256_file(side)}
                if arm.get("sidecar") is not None
                else None
            ),
        }
    manifest["prod_ranks"] = [
        {"path": p, "sha256": sha256_file(p)} for p in (prod_rank_paths or [])
    ]
    manifest["shipped_weight"] = shipped_weight
    if expansion_cache_path is None:
        manifest["expansion_cache"] = None
    else:
        manifest["expansion_cache"] = {
            "path": expansion_cache_path,
            "sha256": (
                sha256_file(expansion_cache_path)
                if os.path.isfile(expansion_cache_path)
                else None
            ),
        }
    manifest["command_line"] = list(command_line or [])
    if legacy is not None:
        manifest["legacy"] = legacy
    if c4 is not None:
        manifest["c4"] = c4
    if controls_path is not None:
        manifest["controls"] = {"path": controls_path, "sha256": sha256_file(controls_path)}
    if rewrite_candidate is not None:
        manifest["rewrite_candidate"] = rewrite_candidate
    return manifest


def new_run_id() -> str:
    """A fresh private run id (``src.eval_privacy.new_run_id``)."""
    return _privacy.new_run_id()


def private_output_path(requested: str) -> Tuple[Path, Path]:
    """``(run dir, file path)`` for an output a private run asked to write.

    A path already inside ``eval/private/runs/<run id>/<file>`` is kept (its
    directory is the run directory); any other path is redirected to a fresh
    ``eval/private/runs/<run id>/<basename>``, so nothing a private run writes
    ever lands outside the private root (item 1). Symlink and ``..`` escapes
    are refused by ``src.eval_privacy``.

    Raises:
        src.eval_privacy.PrivatePathError: on a contained-path escape.
    """
    runs_root = _privacy.private_root() / "runs"
    if _privacy.is_under(requested, runs_root):
        rel = _privacy.relative_to_root(requested, runs_root)
        if len(rel.parts) == 2:
            rdir = _privacy.run_dir(rel.parts[0])
            return rdir, rdir / rel.parts[1]
    rdir = _privacy.run_dir(new_run_id())
    return rdir, rdir / os.path.basename(requested)


def _derived_input(path: str, sources: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """An ``inputs.json`` entry for a report, sidecar, dump or cache."""
    return {
        "path": os.path.abspath(path),
        "sha256": sha256_file(path),
        "kind": "derived",
        "sources": [
            {"path": str(s.get("path")), "sha256": s.get("sha256")}
            for s in sources
            if isinstance(s.get("sha256"), str) and _SHA256.match(s["sha256"])
        ],
    }


# --------------------------------------------------------------------------
# Entry function + CLI
# --------------------------------------------------------------------------
def input_paths(
    report_paths: Sequence[str],
    prod_ranks: Optional[Sequence[str]] = None,
    expansion_cache: Optional[str] = None,
    controls_path: Optional[str] = None,
) -> List[str]:
    """Every eval input a run opens: reports, their sidecars, dumps, the cache, controls.

    The expansion cache counts whenever ``--prod-ranks`` is given (defaulting
    to the committed 0717 cache) or ``--expansion-cache`` names one. The
    ``--controls`` file is opened too, so it is part of the floor (item 1).
    """
    paths = list(report_paths)
    paths += [sidecar_path(p) for p in report_paths if os.path.isfile(sidecar_path(p))]
    paths += list(prod_ranks or [])
    cache = expansion_cache or (DEFAULT_EXPANSION_CACHE if prod_ranks else None)
    if cache is not None:
        paths.append(cache)
    if controls_path is not None:
        paths.append(controls_path)
    return paths


def _arm_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def load_arm(path: str) -> Dict[str, Any]:
    """Parse one arm report and attach its rows sidecar when present.

    A report with a sidecar is a C4 arm: its sidecar must be a valid C4
    document (a malformed one is refused, never downgraded to legacy), and
    its expansion state is checked by C4 rather than the offline marker.

    Raises:
        C4Error: a malformed sidecar.
        ValueError: :func:`parse_report` refusals.
    """
    side = sidecar_path(path)
    sidecar = None
    if os.path.isfile(side):
        with open(side, encoding="utf-8") as fh:
            try:
                sidecar = json.load(fh)
            except ValueError as exc:
                raise C4Error(f"arm {_arm_name(path)!r}: rows sidecar is not valid JSON") from exc
        index_rows(sidecar, _arm_name(path))  # validate now; refuse, never downgrade
    with open(path, encoding="utf-8") as fh:
        arm = parse_report(fh.read(), require_offline=sidecar is None)
    arm["sidecar"] = sidecar
    if arm.get("report_version") == 6:
        _fill_from_sidecar(arm, _arm_name(path))
    return arm


def _print_prod_ranks(name: str, result: Mapping[str, Any], *, show_text: bool) -> None:
    """Print one arm's production-config flips (v5 lines on a public legacy run)."""
    if not result["c4"] and show_text:
        flips = result["flips"] or "none"
        gains = [r["question"][:70] for r in result["gains"]] or "none"
        print(f"- **{name}**: HIT→MISS: {flips}")
        print(f"  MISS→HIT: {gains}")
        if result["unmatched"]:
            print(f"  UNMATCHED KEYS (investigate): {result['unmatched']}")
        return

    def ident(row: Mapping[str, Any]) -> str:
        if "id" in row:
            return row["id"]
        question = row.get("question")
        return row_id_of(question) if isinstance(question, str) else row["key"]

    def label(row: Mapping[str, Any]) -> str:
        rid = ident(row)
        question = row.get("question")
        if show_text and isinstance(question, str) and has_text(question):
            return f"{rid} — {question[:70]}"
        return rid

    print(f"- **{name}**: HIT→MISS: {[label(r) for r in result['flips']] or 'none'}")
    print(f"  MISS→HIT: {[label(r) for r in result['gains']] or 'none'}")
    if result.get("control_flips") or result.get("control_gains"):
        print(
            f"  controls (reported apart): HIT→MISS: "
            f"{[label(r) for r in result['control_flips']] or 'none'}; "
            f"MISS→HIT: {[label(r) for r in result['control_gains']] or 'none'}"
        )
    if result["unmatched"]:
        print(f"  UNMATCHED KEYS (investigate): {result['unmatched']}")


def run(
    report_paths: Sequence[str],
    baseline: str,
    *,
    privacy: str,
    legacy: bool = False,
    legacy_public: bool = False,
    prod_ranks: Optional[Sequence[str]] = None,
    expansion_cache: Optional[str] = None,
    manifest_out: Optional[str] = None,
    controls_path: Optional[str] = None,
    rewrite_candidate: Optional[str] = None,
    command_line: Optional[Sequence[str]] = None,
) -> int:
    """Print the selection evidence (and optionally write a manifest).

    The entry function (item 1): ``privacy`` is keyword-only with no default.
    It re-derives the floor (the strictest ``classify`` over every input it
    opens) and refuses a weaker ``privacy`` before reading anything else. On a
    non-public run stdout carries arm names, rates and opaque ids only, and
    the manifest is written only under ``eval/private/runs/<run id>/`` beside
    an ``inputs.json``.

    Args:
        report_paths: arm reports (the arm name is the file stem).
        baseline: the baseline arm name.
        privacy: the caller's class (``main`` passes the derived floor).
        legacy: ``--legacy``: accept arms/dumps without C4 fields (v5 rules).
        legacy_public: ``--legacy-public``: enables classify's frozen
            legacy-public lookup (it never sets a class itself).
        prod_ranks: ``w_sweep --ranks-out`` dumps, baseline first.
        expansion_cache: the cache the dumps were replayed from.
        manifest_out: where to write the JSON manifest.
        controls_path: ``--controls`` file.
        rewrite_candidate: ``--rewrite-candidate`` config hash.
        command_line: argv recorded in the manifest.

    Returns:
        0 on success.

    Raises:
        SealedInputError: sealed input (``main`` maps it to exit 4).
        PrivacyFloorError: ``privacy`` weaker than the inputs' floor.
        C4Error / LegacyArmError: comparability refusals.
    """
    opened = input_paths(report_paths, prod_ranks, expansion_cache, controls_path)
    _eval_sets.refuse_sealed(opened)
    floor = _eval_sets.floor(opened, legacy_public=legacy_public)
    privacy = check_floor(require_class(privacy), floor)
    show_text = privacy == PUBLIC

    arms: Dict[str, Dict[str, Any]] = {}
    paths: Dict[str, str] = {}
    for path in report_paths:
        arms[_arm_name(path)] = load_arm(path)
        paths[_arm_name(path)] = path
    if baseline not in arms:
        raise KeyError(f"baseline arm {baseline!r} not among reports: {list(arms)}")
    controls = load_controls(controls_path) if controls_path else None

    legacy_arms = [name for name, arm in arms.items() if arm["sidecar"] is None]
    if legacy:
        print("legacy: --legacy given (recorded in the manifest)", file=sys.stderr)
    if legacy_arms and legacy:
        print(f"{LEGACY_NOTE} (legacy arms: {legacy_arms})", file=sys.stderr)

    rendered = render(
        arms, baseline, legacy=legacy, rewrite_candidate=rewrite_candidate,
        controls=controls, show_text=show_text,
    )

    prod_out: List[Tuple[str, Dict[str, Any]]] = []
    if prod_ranks:
        with open(prod_ranks[0], encoding="utf-8") as f:
            base_dump = json.load(f)
        for path in prod_ranks[1:]:
            with open(path, encoding="utf-8") as f:
                dump = json.load(f)
            if legacy and not (is_c4(base_dump) and is_c4(dump)):
                print(f"{LEGACY_NOTE} (production-rank dump {_arm_name(path)!r})", file=sys.stderr)
            prod_out.append(
                (
                    _arm_name(path),
                    compare_prod_ranks(
                        base_dump, dump, legacy=legacy, rewrite_candidate=rewrite_candidate,
                        controls=controls, name=_arm_name(path),
                    ),
                )
            )

    # Every refusal has happened by now: print only complete evidence.
    print(rendered)
    if prod_ranks:
        print("\n## Production-config golden flips (W=0.25, cached expansions)\n")
        for name, result in prod_out:
            _print_prod_ranks(name, result, show_text=show_text)

    if manifest_out:
        cache = expansion_cache or (DEFAULT_EXPANSION_CACHE if prod_ranks else None)
        manifest = build_manifest(
            arms,
            paths,
            baseline,
            prod_rank_paths=prod_ranks or [],
            shipped_weight=SHIPPED_WEIGHT,
            expansion_cache_path=cache,
            command_line=command_line,
            legacy=legacy,
            c4=not legacy_arms,
            controls_path=controls_path,
            rewrite_candidate=rewrite_candidate,
        )
        content = json.dumps(manifest, indent=2)
        if show_text:
            with open(manifest_out, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"\nManifest written: {manifest_out}")
        else:
            rdir, target = private_output_path(manifest_out)
            inputs = []
            for name, path in paths.items():
                sets = [
                    {"path": d.get("path"), "sha256": d.get("sha256")}
                    for d in arms[name]["sets"].values()
                ]
                inputs.append(_derived_input(path, sets))
                if arms[name]["sidecar"] is not None:
                    inputs.append(_derived_input(sidecar_path(path), arms[name]["sidecar"]["cohorts"]))
            for path in prod_ranks or []:
                with open(path, encoding="utf-8") as f:
                    dump = json.load(f)
                inputs.append(_derived_input(path, dump.get("cohorts", []) if is_c4(dump) else []))
            if cache is not None and os.path.isfile(cache):
                inputs.append(_derived_input(cache, []))
            if controls_path is not None:
                # built from the sets its set_sha256 values name (each resolved
                # in every arm by now, so the baseline's provenance has it)
                wanted = {sha for sha, _rid in controls or ()}
                inputs.append(_derived_input(controls_path, [
                    {"path": d.get("path"), "sha256": d.get("sha256")}
                    for d in arms[baseline]["sets"].values() if d.get("sha256") in wanted
                ]))
            # inputs.json before the manifest (16A-1 gate round 5, PT3)
            write_inputs_json(rdir, inputs)
            write_private(target, content)
            print(f"\nManifest written: {target}")
    return 0


def _refuse_heldout(parser: argparse.ArgumentParser, argv: Sequence[str]) -> None:
    """Exit 2 if any argument mentions the held-out set.

    Held-out exclusion is a property of this artifact, not of the operator's
    discipline (finding A25 / Codex C7): the set is never used for selection,
    so no argument may name it.
    """
    for arg in argv:
        if "heldout" in arg.lower():
            parser.error(
                f"refusing argument {arg!r}: the held-out set is never used for "
                "selection (eval integrity, D30/D31/D46) — bake-off arms are "
                "judged on eval/golden_set.jsonl"
            )


def _build_parser() -> argparse.ArgumentParser:
    """The CLI parser (flags documented in the module docstring)."""
    parser = argparse.ArgumentParser(
        description="Compare offline bake-off arm reports and emit selection evidence."
    )
    parser.add_argument(
        "--reports",
        nargs="+",
        required=True,
        metavar="PATH",
        help="Arm report paths; the arm name is the file stem.",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="Arm name (file stem) every other arm is compared against.",
    )
    parser.add_argument(
        "--manifest-out",
        default=None,
        metavar="PATH",
        help=(
            "Also write a JSON manifest of the inputs (paths + sha256s). On a "
            "private run it is written under eval/private/runs/<run id>/."
        ),
    )
    parser.add_argument(
        "--prod-ranks",
        nargs="+",
        default=None,
        metavar="RANKS_JSON",
        help=(
            "w_sweep --ranks-out dumps: the baseline arm's dump first, then one "
            "per candidate arm (file stem names the arm). Emits the "
            "production-config golden flip lists at the shipped W "
            "(selection disqualifier #2)."
        ),
    )
    parser.add_argument(
        "--expansion-cache",
        default=None,
        metavar="PATH",
        help=(
            "Expansion cache the --prod-ranks dumps were replayed from; its "
            "sha256 goes in the manifest. Defaults to the committed W-sweep "
            "cache when --prod-ranks is given."
        ),
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "Accept arms without C4 fields (a v5 report with no .rows.json "
            "sidecar, a pre-16A dump) under today's v5 rules; prints "
            "'legacy: C4 not checked' and is recorded in the manifest."
        ),
    )
    parser.add_argument(
        "--legacy-public",
        action="store_true",
        help=(
            "Enable classify's frozen legacy-public lookup (eval/legacy_public.json, "
            "e.g. the 0717 expansion cache). Without it a --prod-ranks run "
            "floors to private."
        ),
    )
    parser.add_argument(
        "--controls",
        default=None,
        metavar="PATH",
        help=(
            'Control rows, {"version": 1, "controls": [{"set_sha256", "ids"}]}: '
            "must be non-empty and resolve in both arms; their flips are "
            "reported apart (C4 only)."
        ),
    )
    parser.add_argument(
        "--rewrite-candidate",
        default=None,
        metavar="CONFIG_HASH",
        help=(
            "Declared candidate rewrite config hash: an arm whose expansion "
            "identity records it may differ from the baseline in that config only."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: print the selection evidence, optionally a manifest.

    Exit codes: 0 ok; 1 an error on a private run (printed as its exception
    type only); 2 argparse errors, held-out arguments, an unknown baseline and
    C4/legacy refusals; 4 sealed input (before anything else is read).
    """
    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    _refuse_heldout(parser, raw)
    args = parser.parse_args(raw)

    opened = input_paths(args.reports, args.prod_ranks, args.expansion_cache, args.controls)
    if any(_eval_sets.is_sealed(p) for p in opened):
        print("[bakeoff_report] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    privacy = _eval_sets.floor(opened, legacy_public=args.legacy_public)
    if privacy == SEALED:
        print("[bakeoff_report] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED

    names = [_arm_name(p) for p in args.reports]
    if args.baseline not in names:
        parser.error(
            f"baseline {args.baseline!r} is not among the report names: {names}"
        )

    try:
        return run(
            args.reports,
            args.baseline,
            privacy=privacy,
            legacy=args.legacy,
            legacy_public=args.legacy_public,
            prod_ranks=args.prod_ranks,
            expansion_cache=args.expansion_cache,
            manifest_out=args.manifest_out,
            controls_path=args.controls,
            rewrite_candidate=args.rewrite_candidate,
            command_line=[sys.argv[0], *raw],
        )
    except SealedInputError:
        print("[bakeoff_report] sealed input refused (16A-1)", file=sys.stderr)
        return EXIT_SEALED
    except C4Error as exc:
        # C4 messages carry names, labels, hashes and ids -- but an id read
        # from a malformed dump can carry arbitrary text, so a non-public run
        # prints the refusal type only (16A-1 gate round 2).
        if privacy != PUBLIC:
            print(f"[bakeoff_report] refused: {safe_error(exc)} (details withheld on a private run)",
                  file=sys.stderr)
        else:
            print(f"[bakeoff_report] refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - private runs must not print str(exc)
        if privacy != PUBLIC:
            print(f"[bakeoff_report] error: {safe_error(exc)}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
