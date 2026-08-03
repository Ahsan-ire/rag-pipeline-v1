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

Usage::

    python scripts/bakeoff_report.py \\
        --reports eval/bakeoff/baseline-minilm.md eval/bakeoff/gte.md \\
        --baseline baseline-minilm \\
        [--manifest-out eval/bakeoff/manifest.json]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# The offline-run disclosure every arm report must carry. Reports that ran with
# live expansion are a different experiment and are refused, not compared.
EXPANSION_DISABLED_MARKER = "query expansion: disabled (offline run)"

# The per-question detail section the evaluator emits when the hybrid+rewrite
# mode is ablated — which, with expansion disabled, is raw hybrid by identity.
DETAIL_MODE = "hybrid+rewrite"

# Which set is which, by the eval-set file the report names in its provenance.
GOLDEN_BASENAME = "golden_set.jsonl"
REALISTIC_BASENAME = "realistic_set.jsonl"

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
_SET_FIELD = re.compile(r"^\s+- (?P<key>path|sha256): (?P<value>.+)$")
_EMBEDDING_MODEL = re.compile(r"^- embedding model: (?P<model>.+)$")


@dataclass(frozen=True)
class RosterEntry:
    """One question of the per-class roster (the Tier-2 ground truth).

    Attributes:
        failure_class: the D54 failure class ("vocabulary gap" / "near-miss").
        prefix: identifying prefix of the realistic-set question, exactly as
            the brief's roster table records it.
        expected: the sections the eval set expects, for display only —
            hit/miss is read from the report, never recomputed here.
    """

    failure_class: str
    prefix: str
    expected: Tuple[str, ...]


@dataclass(frozen=True)
class RoleSpec:
    """A comparison question whose answer needs BOTH sides retrieved.

    Attributes:
        name: short handle used in the brief and in D50 ("S5", "N4").
        prefix: identifying prefix of the question.
        groups: the role groups that must each be covered *separately*.
    """

    name: str
    prefix: str
    groups: Tuple[str, ...]


# Hardcoded from the roster table in docs/designs/001-bakeoff-embedding-model.md
# ("Per-class roster (the Tier-2 instrument's ground truth)"). Questions are
# matched by prefix, so the brief's "…" truncation is simply dropped.
ROSTER: Tuple[RosterEntry, ...] = (
    RosterEntry("vocabulary gap", "The neighbour has been using our client's field", ("13.4.8",)),
    RosterEntry("vocabulary gap", "Two brothers own a farm together and one of them died", ("5.8",)),
    RosterEntry("vocabulary gap", "Can you explain what unregistered land means?", ("1.7", "1.8")),
    RosterEntry(
        "near-miss",
        "What is the difference between a purchase and sale conveyance?",
        ("2.2.1", "2.2.2", "2.9"),
    ),
    RosterEntry("near-miss", "How far back do the title documents need to go", ("4.5.1",)),
    RosterEntry("near-miss", "Husband owns the house and the wife isn't on the deeds", ("7.2", "7.2.9")),
    RosterEntry(
        "near-miss",
        "Client is buying a house and the seller is leaving the appliances",
        ("16.4.5",),
    ),
    RosterEntry("near-miss", "We're acting for both the buyer and their bank", ("9.7.2", "9.8")),
)

# S5 and N4 (D50): both are purchase-vs-sale comparisons whose expected set is
# 2.2.1 / 2.2.2 / 2.9. The coverage requirement deliberately covers the two role
# groups only — 2.9 (the process overview) is excluded on purpose, recorded in
# D57 rather than left implicit (round-3 correction 2).
ROLE_QUESTIONS: Tuple[RoleSpec, ...] = (
    RoleSpec("S5", "What is the difference between a purchase and sale conveyance?", ("2.2.1", "2.2.2")),
    RoleSpec(
        "N4",
        "What's the difference between what the seller's solicitor and the buyer's solicitor",
        ("2.2.1", "2.2.2"),
    ),
)


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
                "question": m.group("question"),
                "expected": list(ast.literal_eval(m.group("expected"))),
                "retrieved": list(ast.literal_eval(m.group("retrieved"))),
                "strict_rank": _opt_int(m.group("strict_rank")),
                "related_rank": _opt_int(m.group("related_rank")),
            }
        )
    return rows


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


def parse_report(text: str) -> Dict[str, Any]:
    """Parse one offline arm report into a comparable dict.

    Args:
        text: the full Markdown text of an arm report.

    Returns:
        ``{"embedding_model": str|None, "expansion_disabled": True,
        "sets": {label: {"path", "sha256", "ablation", "questions"}}}`` where
        ``questions`` are the ``hybrid+rewrite`` per-question rows (raw hybrid
        by identity on an offline run).

    Raises:
        ValueError: if the report does not disclose disabled expansion (it is
            then not comparable to the other arms), or if a set's per-question
            detail was rendered under a mode other than ``hybrid+rewrite``.
    """
    if EXPANSION_DISABLED_MARKER not in text:
        raise ValueError(
            "report does not carry "
            f"{EXPANSION_DISABLED_MARKER!r} — arms whose expansion state "
            "differs are not comparable; re-run it with --skip-refusals "
            "--skip-completeness"
        )

    model: Optional[str] = None
    prov_sets: Dict[str, Dict[str, str]] = {}
    ablations: Dict[str, Dict[str, Dict[str, Any]]] = {}
    details: Dict[str, List[Dict[str, Any]]] = {}

    for heading, body in _split_sections(text):
        if heading.startswith("## Provenance"):
            model, prov_sets = _parse_provenance(body)
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
    return {"embedding_model": model, "expansion_disabled": True, "sets": sets}


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


def compare(arms: Mapping[str, Mapping[str, Any]], baseline: str) -> Dict[str, Any]:
    """Build the selection table and the golden per-question flip lists.

    Args:
        arms: ``{arm_name: parsed_report}``, in the order they should appear.
        baseline: the arm name every other arm is compared against.

    Returns:
        ``{"baseline", "table", "flips"}``. ``table`` carries golden and
        realistic strict@6/related@6 per arm; ``flips`` carries, per non-
        baseline arm, the full text of every golden question that went
        ``hit_to_miss`` (strict, @6) or ``miss_to_hit``, plus ``unmatched``
        questions present in one arm but not the other.

    Raises:
        KeyError: if ``baseline`` is not among ``arms``.
    """
    if baseline not in arms:
        raise KeyError(f"baseline arm {baseline!r} not among reports: {list(arms)}")

    table = []
    for name, arm in arms.items():
        table.append(
            {
                "arm": name,
                "embedding_model": arm.get("embedding_model"),
                "golden": _headline_row(set_of_kind(arm, "golden")),
                "realistic": _headline_row(set_of_kind(arm, "realistic")),
            }
        )

    base_golden = set_of_kind(arms[baseline], "golden") or {"questions": []}
    base_hits = {
        q["question"]: _hit_at_k(q["strict_rank"]) for q in base_golden["questions"]
    }

    flips: Dict[str, Dict[str, List[str]]] = {}
    for name, arm in arms.items():
        if name == baseline:
            continue
        golden = set_of_kind(arm, "golden") or {"questions": []}
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
    return {"baseline": baseline, "table": table, "flips": flips}


def class_movement(
    arm: Mapping[str, Any], roster: Sequence[RosterEntry] = ROSTER
) -> Dict[str, Any]:
    """Which roster questions this arm now hits, per D54 failure class.

    A roster question counts as recovered when it is a strict **or** related
    HIT within the top 6 — the roster is about whether the content surfaces at
    all, which is what the vocabulary-gap and near-miss classes describe.

    Args:
        arm: a parsed arm report.
        roster: roster entries to score; defaults to the brief's table.

    Returns:
        ``{"entries": [...], "by_class": {class: {"n", "hits", "misses"}},
        "unmatched": [prefix, ...]}`` — ``unmatched`` names roster prefixes
        that matched no question in the arm's realistic set (a roster that has
        drifted from the eval set, which must be noticed, not averaged away).
    """
    realistic = set_of_kind(arm, "realistic") or {"questions": []}
    entries: List[Dict[str, Any]] = []
    by_class: Dict[str, Dict[str, Any]] = {}
    unmatched: List[str] = []

    for item in roster:
        match = next(
            (q for q in realistic["questions"] if q["question"].startswith(item.prefix)),
            None,
        )
        bucket = by_class.setdefault(
            item.failure_class, {"n": 0, "hits": [], "misses": []}
        )
        bucket["n"] += 1
        entry: Dict[str, Any] = {
            "class": item.failure_class,
            "prefix": item.prefix,
            "expected": list(item.expected),
            "question": None if match is None else match["question"],
            "strict_rank": None if match is None else match["strict_rank"],
            "related_rank": None if match is None else match["related_rank"],
            "hit_at_6": False,
        }
        if match is None:
            unmatched.append(item.prefix)
            bucket["misses"].append(item.prefix)
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
        roles: the questions and their role groups; defaults to S5 and N4.

    Returns:
        ``{name: {"question", "found", "retrieved", "groups": {group: bool},
        "both": bool}}``. ``both`` is True only when every group is covered
        independently.
    """
    realistic = set_of_kind(arm, "realistic") or {"questions": []}
    out: Dict[str, Any] = {}
    for role in roles:
        match = next(
            (q for q in realistic["questions"] if q["question"].startswith(role.prefix)),
            None,
        )
        retrieved = [] if match is None else match["retrieved"]
        groups = {g: _covers(retrieved, g) for g in role.groups}
        out[role.name] = {
            "question": None if match is None else match["question"],
            "found": match is not None,
            "retrieved": retrieved,
            "groups": groups,
            "both": match is not None and all(groups.values()),
        }
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _fmt(value: Optional[float]) -> str:
    """Render an optional rate cell."""
    return "n/a" if value is None else f"{value:.3f}"


def render(arms: Mapping[str, Mapping[str, Any]], baseline: str) -> str:
    """Render the full selection evidence as Markdown."""
    comparison = compare(arms, baseline)
    out: List[str] = ["# Bake-off selection evidence", ""]
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
        out.extend(f"- {q}" for q in flip["hit_to_miss"] or [])
        if not flip["hit_to_miss"]:
            out.append("- none")
        out.append("")
        out.append(f"MISS->HIT ({len(flip['miss_to_hit'])}):")
        out.extend(f"- {q}" for q in flip["miss_to_hit"] or [])
        if not flip["miss_to_hit"]:
            out.append("- none")
        if flip["unmatched"]:
            out.append("")
            out.append(
                f"UNMATCHED ({len(flip['unmatched'])}) — present in only one arm, "
                "so the sets differ; investigate before trusting the flips:"
            )
            out.extend(f"- {q}" for q in flip["unmatched"])
        out.append("")

    out.append("## 3. Per-class movement (roster from the brief)")
    out.append("")
    out.append("| Arm | Class | Recovered @6 (strict or related) | Questions |")
    out.append("| --- | --- | --- | --- |")
    unmatched_notes: List[str] = []
    for name, arm in arms.items():
        movement = class_movement(arm)
        for failure_class, bucket in movement["by_class"].items():
            hits = "; ".join(bucket["hits"]) or "—"
            out.append(
                f"| {name} | {failure_class} | {len(bucket['hits'])}/{bucket['n']} | {hits} |"
            )
        if movement["unmatched"]:
            unmatched_notes.append(
                f"- {name}: roster prefixes matching no question — "
                + "; ".join(movement["unmatched"])
            )
    out.append("")
    if unmatched_notes:
        out.append("Roster drift (a prefix no longer matches the eval set):")
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
# Manifest + CLI
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
) -> Dict[str, Any]:
    """Describe exactly which artifacts produced this comparison.

    Args:
        arms: ``{arm_name: parsed_report}``.
        paths: ``{arm_name: report_path}``.
        baseline: the baseline arm name.

    Returns:
        A JSON-ready dict carrying, per arm, the report path and its sha256,
        the embedding model, and each eval set's path + sha256 as recorded in
        the report's provenance — so held-out absence is checkable from the
        artifact rather than asserted from shell history.
    """
    manifest: Dict[str, Any] = {"baseline": baseline, "arms": {}}
    for name, arm in arms.items():
        manifest["arms"][name] = {
            "report_path": paths[name],
            "report_sha256": sha256_file(paths[name]),
            "embedding_model": arm.get("embedding_model"),
            "eval_sets": [
                {"label": label, "path": data.get("path"), "sha256": data.get("sha256")}
                for label, data in sorted(arm["sets"].items())
            ],
        }
    return manifest


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: print the selection evidence, optionally a manifest."""
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
        help="Also write a JSON manifest of the inputs (paths + sha256s).",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    _refuse_heldout(parser, raw)
    args = parser.parse_args(raw)

    arms: Dict[str, Dict[str, Any]] = {}
    paths: Dict[str, str] = {}
    for path in args.reports:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            arms[name] = parse_report(f.read())
        paths[name] = path
    if args.baseline not in arms:
        parser.error(
            f"baseline {args.baseline!r} is not among the report names: {list(arms)}"
        )

    print(render(arms, args.baseline))

    if args.manifest_out:
        manifest = build_manifest(arms, paths, args.baseline)
        with open(args.manifest_out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"\nManifest written: {args.manifest_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
