"""The sealed rendering of report v6 (Phase 16A-2a item a5, D73).

Pure functions from a slot run's **stored results document** (``results.json``
in ``eval/private/sealed/runs/<run id>/``, written by ``src.sealed_run``) to
Markdown. Nothing here reads a file, calls a model or knows a path, so a
recovery can re-render a stored run and get the same text.

What the rendering never contains: a row id, a family id, a group id, question
text, a chapter or section, a per-family line or a per-chapter line. It shows
**aggregates**: per-scope, per-register and per-source tables, the family
counts, the group count and the rates. The stored document is the only input,
plus the set of excluded groups (the freeze-time exclusion list and the
ledger's exclusions, and any group the pre-publication scan adds), which are
dropped from every aggregate of the sealed set: their rows still ran, so the
stored document is whole and a run's pairing survives, but analysis never sees
them.

Stored document (version 1), the parts this module reads::

    {"version": 1, "kind": "sealed_slot_results", "slot", "run_id", "created",
     "top_k", "k", "cutoff", "modes", "primary_mode", "answers_ran", "provenance",
     "expansion": {"mode", "identity", "rewrite_live", "rewrite_replayed"},
     "run_cost": {"run_eur", "week_eur"} | null,
     "floor": {"families", "eligible"},
     "groups": {family_id: group_id},          # the sealed set's group map
     "baseline": null | {"cutoff", "hits": {family_id: bool}},   # confirmatory slots
     "sets": [{"label", "role": "sealed" | "development" | "regression", "sha256",
               "cohort": {...},
               "rows": [{"id", "family_id", "scope", "register", "source",
                         "retrieval": {mode: {"strict_hit", "related_hit",
                                              "strict_rank", "related_rank"}},
                         "answer": null | {"observed", "partial"},
                         "judge": null | {"ok", "error_type", "n_claims",
                                          "supported", "unsupported",
                                          "unclear"}}]}]}

The statistics: slot 0 and 0b are **descriptive** (Wilson intervals). In a
confirmatory slot the **primary** statistic is the exact two-sided cluster
sign-flip test (``eval_stats.cluster_signflip_p``) on each group's net
discordance (families the candidate gains minus families it loses, on the
family-level strict hit@k of the primary mode under the ``all`` collapse);
Durkalski's clustered McNemar by group and the exact family-level McNemar are
printed as **labelled sensitivities**, never as the test.

``cutoff`` is the prereg's hit cut-off the run was scored at (equal to ``k``); a confirmatory slot's
baseline must carry the same one, and the renderer refuses a document where it does not (:func:`check_cutoff`).

A confirmatory slot is never canonical and is not judged by the canonical guards: its report
says "confirmatory" instead of "canonical"/"not canonical" and carries no "guards not met" line,
and :func:`confirmatory_summary` gives the numbers-only lines the CLI prints for it.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src.eval_stats import (
    UNAVAILABLE,
    cluster_signflip_p,
    collapse_family,
    durkalski_chi2,
    family_cluster,
    mcnemar_exact,
)
from src.eval_v6 import _fmt_rate, _rate, family_rates, judge_lines, judge_summary

RESULTS_KIND = "sealed_slot_results"
RESULTS_VERSION = 1
FAMILY_RULE = "all"
DESCRIPTIVE_SLOTS = ("0", "0b")

#: Source value -> printed label. ``handbook`` rows are the ones Claude drafted from
#: the handbook itself; they carry the D46 caveat. Tutorial rows are kept apart.
SOURCE_LABELS = {
    "tutorial": "tutorial",
    "handbook": "Claude-drafted (source: handbook) †",
    "colleague": "colleague",
    "legacy": "legacy",
    None: "unlabelled",
}
DRAFTED_SOURCE = "handbook"
D46_CAVEAT = (
    "† D46 caveat: questions Claude drafts from the handbook share the corpus's own "
    "vocabulary, so retrieval on them is close to self-retrieval and overstates what a "
    "natural-phrasing query would achieve. Read this row as development-style evidence, "
    "not as independent proof."
)
SCOPES = ("answer", "partial", "refuse")
REGISTERS = ("modelled", "lay", "terse", None)
SOURCES_ORDER = ("tutorial", "handbook", "colleague", "legacy", None)


# ---------------------------------------------------------------------------
# Views over the stored document
# ---------------------------------------------------------------------------


def is_confirmatory(slot: str) -> bool:
    """Slots other than ``0`` and ``0b`` are confirmatory (1-4 in the prereg)."""
    return slot not in DESCRIPTIVE_SLOTS


def check_cutoff(doc: Mapping[str, Any]) -> None:
    """Refuse a stored document whose hit cut-off is unrecorded, is not its ``k``, or differs from its baseline's.

    A baseline scored at another cut-off has other hits (a section at rank 4 is a hit at 6 and a miss at
    3), so pairing it with this run would manufacture gains or losses from the cut-off alone.

    Raises:
        ValueError: any of the three.
    """
    cutoff = doc.get("cutoff")
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff != doc["k"]:
        raise ValueError("the stored run does not record the hit cut-off it was scored at")
    base = doc.get("baseline")
    if base and base.get("cutoff") != cutoff:
        raise ValueError("the baseline and the candidate were scored at different hit cut-offs")


def sealed_set(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one set of the stored document with role ``sealed``."""
    found = [s for s in doc["sets"] if s["role"] == "sealed"]
    if len(found) != 1:
        raise ValueError("a slot results document holds exactly one sealed set")
    return found[0]


def dropped_families(doc: Mapping[str, Any], excluded: FrozenSet[str]) -> FrozenSet[str]:
    """The families of the sealed set that analysis drops (those in an excluded group)."""
    return frozenset(f for f, g in doc["groups"].items() if g in excluded)


def kept_rows(set_doc: Mapping[str, Any], drop: FrozenSet[str]) -> List[Mapping[str, Any]]:
    """The set's rows whose family is not dropped."""
    return [r for r in set_doc["rows"] if r["family_id"] not in drop]


def eligible_families(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """Families whose rows are all answer or partial (retrieval-eligible), sorted."""
    fams: Dict[str, bool] = {}
    for r in rows:
        fams[r["family_id"]] = fams.get(r["family_id"], True) and r["scope"] != "refuse"
    return sorted(f for f, ok in fams.items() if ok)


def family_hits(rows: Sequence[Mapping[str, Any]], mode: str) -> Dict[str, bool]:
    """Family-level strict hit@k of ``mode`` over eligible families (``all`` collapse; a tie is a miss)."""
    by_family: Dict[str, List[bool]] = {}
    for r in rows:
        if r["scope"] != "refuse":
            by_family.setdefault(r["family_id"], []).append(bool(r["retrieval"][mode]["strict_hit"]))
    return {f: collapse_family(outcomes, FAMILY_RULE) for f, outcomes in by_family.items()}


def _shapes(rows: Sequence[Mapping[str, Any]], modes: Sequence[str], k: int, answers_ran: bool):
    """Rebuild the ``eval_v6.family_rates`` input shapes from stored rows."""
    plain = [{"id": r["id"], "family_id": r["family_id"], "scope": r["scope"]} for r in rows]
    retrieval: Dict[str, Dict[str, Dict[str, Any]]] = {m: {} for m in modes}
    for r in rows:
        if r["scope"] == "refuse":
            continue
        for m in modes:
            cell = r["retrieval"][m]
            retrieval[m][r["id"]] = {
                "strict_hit": {k: bool(cell["strict_hit"])},
                "related_hit": {k: bool(cell["related_hit"])},
            }
    answers: Optional[Dict[str, Dict[str, Any]]] = None
    if answers_ran:
        answers = {}
        for r in rows:
            a = r["answer"]
            if a is None:
                continue
            entry: Dict[str, Any] = {"observed": a["observed"]}
            if r["scope"] == "partial":
                entry["partial"] = {"status": a["partial"]}
            answers[r["id"]] = entry
    return plain, retrieval, answers


def _row_scope_correct(r: Mapping[str, Any]) -> Optional[bool]:
    a = r["answer"]
    if a is None:
        return None
    if r["scope"] == "partial":
        return a["partial"] == "correct"
    return a["observed"] == r["scope"]


def stratum_line(label: str, rows: Sequence[Mapping[str, Any]], primary: str, answers_ran: bool) -> str:
    """One aggregate table row for a stratum (row = phrasing level)."""
    families = len({r["family_id"] for r in rows})
    eligible = [r for r in rows if r["scope"] != "refuse"]
    hits = sum(1 for r in eligible if r["retrieval"][primary]["strict_hit"])
    cells = [label, str(len(rows)), str(families), _fmt_rate(_rate(hits, len(eligible)), "rows")]
    if answers_ran:
        verdicts = [v for v in (_row_scope_correct(r) for r in rows) if v is not None]
        cells.append(_fmt_rate(_rate(sum(verdicts), len(verdicts)), "rows"))
    return "| " + " | ".join(cells) + " |"


def _strata_table(
    title: str,
    keys: Sequence[Any],
    label_of: Mapping[Any, str],
    attr: str,
    rows: Sequence[Mapping[str, Any]],
    primary: str,
    k: int,
    answers_ran: bool,
) -> List[str]:
    head = ["Stratum", "Rows", "Families", f"Strict hit@{k} ({primary}; eligible rows)"]
    if answers_ran:
        head.append("Scope correct (rows)")
    out = [f"### {title}", "", "| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * len(head)) + " |"]
    for key in keys:
        subset = [r for r in rows if r.get(attr) == key]
        if subset:
            out.append(stratum_line(label_of[key], subset, primary, answers_ran))
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Inference (confirmatory slots)
# ---------------------------------------------------------------------------


def confirmatory_stats(
    candidate: Mapping[str, bool], baseline: Mapping[str, bool], groups: Mapping[str, str]
) -> Dict[str, Any]:
    """The primary test and the two labelled sensitivities, from paired family outcomes.

    Args:
        candidate, baseline: family -> strict hit of the primary mode, over the
            same eligible, non-dropped families (a different roster is refused).
        groups: family -> group id.

    Returns:
        ``{"families", "groups", "groups_discordant", "gains", "losses",
        "net", "p_signflip", "durkalski", "p_mcnemar"}``: ``gains`` (``b``) are
        families the candidate hits and the baseline misses, ``losses`` (``c``)
        the reverse; ``p_signflip`` is ``cluster_signflip_p`` of the per-group
        net discordance; ``durkalski`` is ``{"chi2", "p"}`` or ``None``
        (unavailable); ``p_mcnemar`` is the exact two-sided family-level test.

    Raises:
        ValueError: the two arms cover different families, or a family has no group.
    """
    if set(candidate) != set(baseline):
        raise ValueError("the baseline and the candidate cover different families")
    per_group: Dict[str, List[Tuple[bool, bool]]] = {}
    for fam in sorted(candidate):
        if fam not in groups:
            raise ValueError("a family has no group")
        per_group.setdefault(groups[fam], []).append((bool(baseline[fam]), bool(candidate[fam])))
    d = [sum(int(c) - int(b) for b, c in pairs) for _g, pairs in sorted(per_group.items())]
    gains = sum(1 for pairs in per_group.values() for b, c in pairs if c and not b)
    losses = sum(1 for pairs in per_group.values() for b, c in pairs if b and not c)
    dur = durkalski_chi2([family_cluster(pairs) for _g, pairs in sorted(per_group.items())])
    return {
        "families": len(candidate),
        "groups": len(per_group),
        "groups_discordant": sum(1 for x in d if x != 0),
        "gains": gains,
        "losses": losses,
        "net": gains - losses,
        "p_signflip": cluster_signflip_p(d),
        "durkalski": None if dur == UNAVAILABLE else {"chi2": dur.chi2, "p": dur.p_value},
        "p_mcnemar": mcnemar_exact(gains, losses),
    }


def confirmatory_summary(doc: Mapping[str, Any], excluded: FrozenSet[str]) -> List[str]:
    """The aggregates-only lines printed for a confirmatory slot: numbers only, no ids, no text.

    The primary cluster sign-flip line first, then the families line and the two labelled
    sensitivities (Durkalski, exact McNemar). Empty for a descriptive slot (0, 0b).
    """
    check_cutoff(doc)
    if not is_confirmatory(doc["slot"]):
        return []
    drop = dropped_families(doc, excluded)
    rows = kept_rows(sealed_set(doc), drop)
    base = doc.get("baseline")
    if not base:
        return ["no baseline in the stored run; no comparison"]
    primary, k = doc["primary_mode"], doc["k"]
    mine = family_hits(rows, primary)
    paired_base = {f: bool(v) for f, v in base["hits"].items() if f not in drop}
    stats = confirmatory_stats(mine, paired_base, doc["groups"])
    n = stats["families"]
    dur = stats["durkalski"]
    return [
        f"primary ({primary}, family strict hit@{k}): cluster sign-flip p = {stats['p_signflip']:.4f} "
        f"({stats['groups_discordant']} of {stats['groups']} groups discordant; net {stats['net']:+d} families)",
        f"families: candidate gains {stats['gains']}, loses {stats['losses']} of {n}; "
        f"hit rate baseline {sum(paired_base.values())}/{n} -> candidate {sum(mine.values())}/{n}",
        "sensitivity (labelled, not the test): Durkalski clustered McNemar by group: "
        + ("unavailable" if dur is None else f"chi2 = {dur['chi2']:.3f}, p = {dur['p']:.4f}"),
        f"sensitivity (labelled, not the test): exact McNemar on families: b = {stats['gains']}, "
        f"c = {stats['losses']}, p = {stats['p_mcnemar']:.4f}",
    ]


def _inference_lines(doc: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], drop: FrozenSet[str]) -> List[str]:
    primary = doc["primary_mode"]
    k = doc["k"]
    lines: List[str] = ["## Inference", ""]
    mine = family_hits(rows, primary)
    if not is_confirmatory(doc["slot"]):
        lines.append(
            f"- Slot {doc['slot']} is the **descriptive baseline**: Wilson 95% intervals only, no test, "
            "no comparison."
        )
        lines.append(f"- Primary metric ({primary}, family strict hit@{k}, `all` collapse): "
                     f"{_fmt_rate(_rate(sum(mine.values()), len(mine)))}")
        lines.append("")
        return lines
    base = doc.get("baseline")
    if not base:
        lines.append(f"- Slot {doc['slot']} is confirmatory but the stored run carries no baseline.")
        lines.append("")
        return lines
    paired_base = {f: bool(v) for f, v in base["hits"].items() if f not in drop}
    stats = confirmatory_stats(mine, paired_base, doc["groups"])
    lines.append(
        f"- Slot {doc['slot']} is **confirmatory**. Primary metric: {primary}, family strict hit@{k} "
        f"(`all` collapse; a tie is a miss), candidate against the baseline run's frozen outcomes."
    )
    lines.append(
        f"- **Primary test**: exact two-sided cluster sign-flip test on group net discordance: "
        f"p = {stats['p_signflip']:.4f} ({stats['groups_discordant']} of {stats['groups']} groups discordant; "
        f"net {stats['net']:+d} families; the sign of the net gives the direction)."
    )
    n = stats["families"]
    base_rate, cand_rate = sum(paired_base.values()), sum(mine.values())
    lines.append(
        f"- Families: the candidate gains {stats['gains']} and loses {stats['losses']} of {n}; "
        f"family hit rate baseline {base_rate}/{n} -> candidate {cand_rate}/{n} "
        f"(effect {(cand_rate - base_rate) / n:+.3f})."
    )
    dur = stats["durkalski"]
    lines.append(
        "- Sensitivity (labelled, not the test): Durkalski clustered McNemar by group, asymptotic: "
        + ("unavailable" if dur is None else f"chi2 = {dur['chi2']:.3f}, p = {dur['p']:.4f}")
        + "."
    )
    lines.append(
        f"- Sensitivity (labelled, not the test): exact two-sided McNemar on families: "
        f"b = {stats['gains']}, c = {stats['losses']}, p = {stats['p_mcnemar']:.4f}."
    )
    lines.append("- Holm with K = 4 fixed in advance (prereg `holm_k`), applied by the prereg analysis, not here.")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def format_sealed_report(
    doc: Mapping[str, Any],
    *,
    excluded: FrozenSet[str],
    canonical: bool,
    failed_guards: Sequence[str] = (),
) -> str:
    """Render the sealed report v6 from a stored results document.

    Args:
        doc: the stored results document.
        excluded: group ids dropped from the sealed set's analysis.
        canonical: whether the run met every canonical guard.
        failed_guards: names of the guards it missed (a non-canonical rendering).
            Both are ignored for a confirmatory slot, which is neither.

    Returns:
        Markdown with no ids, no question text and no per-family or
        per-chapter line. Deterministic in its arguments (the date is the
        stored run's creation time), so a republish reproduces it exactly.
    """
    check_cutoff(doc)
    k, modes, primary, answers_ran = doc["k"], list(doc["modes"]), doc["primary_mode"], doc["answers_ran"]
    prov = doc["provenance"]
    sealed = sealed_set(doc)
    drop = dropped_families(doc, excluded)
    rows = kept_rows(sealed, drop)
    all_families = {r["family_id"] for r in sealed["rows"]}
    kept_families = {r["family_id"] for r in rows}
    elig = eligible_families(rows)
    groups_all = set(doc["groups"].values())
    groups_kept = {doc["groups"][f] for f in kept_families}
    groups_elig = {doc["groups"][f] for f in elig}
    floor = doc["floor"]
    confirmatory = is_confirmatory(doc["slot"])
    title_state = "confirmatory" if confirmatory else ("canonical" if canonical else "not canonical")
    lines: List[str] = [f"# Legal RAG Evaluation Report v6 (sealed slot {doc['slot']}; {title_state})", ""]
    lines.append(f"- Date: {doc['created']}")
    lines.append(f"- Slot: {doc['slot']} ({'descriptive baseline' if not is_confirmatory(doc['slot']) else 'confirmatory'})")
    lines.append(f"- top_k: {doc['top_k']}; hit cut-off k = {k}")
    lines.append(f"- Retrieval modes: {', '.join(modes)}; primary mode: {primary}")
    lines.append("- Privacy: sealed (aggregates only: no ids, no per-family or per-chapter lines)")
    if not confirmatory:  # the canonical guards judge slots 0 and 0b only
        lines.append(f"- Canonical run: {canonical}")
        if failed_guards:
            lines.append(f"- Guards not met: {', '.join(failed_guards)}")
    lines.append(
        f"- git sha: {prov.get('git_sha')}; embedding model: {prov.get('embedding_model')}; "
        f"generation model: {prov.get('generation_model')}"
    )
    lines.append(f"- family outcome rule: {FAMILY_RULE} (three paraphrases count once)")
    lines.append("")

    lines.append("## Sealed set")
    lines.append("")
    n_scope = {s: len({r["family_id"] for r in rows if r["scope"] == s}) for s in SCOPES}
    lines.append(f"- sha256: {sealed['sha256']}")
    lines.append(f"- rows: {len(rows)} analysed of {len(sealed['rows'])}; families: {len(kept_families)} analysed of {len(all_families)}")
    lines.append(
        f"- excluded groups: {len(groups_all) - len(groups_kept)} of {len(groups_all)} "
        f"({len(all_families) - len(kept_families)} families dropped from analysis; their rows still ran)"
    )
    lines.append(
        f"- sealed families after exclusion: {len(kept_families)} (floor {floor['families']}); "
        f"eligible answerable families (answer or partial, not excluded): {len(elig)} (floor {floor['eligible']})"
    )
    lines.append(
        f"- groups: {len(groups_kept)} analysed ({len(groups_elig)} with at least one eligible family)"
    )
    lines.append(
        f"- families by scope: answer {n_scope['answer']}, partial {n_scope['partial']}, refuse {n_scope['refuse']}"
    )
    lines.append("")

    plain, retrieval, answers = _shapes(rows, modes, k, answers_ran)
    fr = family_rates(plain, retrieval, answers, k=k)
    lines.append("## Family rates (sealed)")
    lines.append("")
    for mode, by in fr["retrieval"].items():
        lines.append(f"- retrieval {mode} strict@{k}: {_fmt_rate(by['strict'])}")
        lines.append(f"- retrieval {mode} related@{k}: {_fmt_rate(by['related'])}")
    for key, name in (("scope", "scope correct"), ("refusal", "refuse families refused"), ("partial", "partial families correct")):
        if key in fr:
            lines.append(f"- {name}: {_fmt_rate(fr[key])}")
    lines.append("")

    lines.append("## Strata (sealed; rows are phrasings, intervals descriptive)")
    lines.append("")
    lines.extend(_strata_table("By scope", SCOPES, {s: s for s in SCOPES}, "scope", rows, primary, k, answers_ran))
    lines.extend(_strata_table(
        "By register", REGISTERS, {r: (r or "unlabelled") for r in REGISTERS}, "register", rows, primary, k, answers_ran))
    lines.extend(_strata_table(
        "By source (tutorial and Claude-drafted kept apart)", SOURCES_ORDER, SOURCE_LABELS, "source",
        rows, primary, k, answers_ran))
    if any(r.get("source") == DRAFTED_SOURCE for r in rows):
        lines.append(D46_CAVEAT)
        lines.append("")

    judgments = [r["judge"] for r in rows if r["judge"] is not None]
    if judgments:
        lines.append("## Judge (sealed; answer + partial rows; same model family as the generator)")
        lines.append("")
        lines.extend(judge_lines(judge_summary(judgments)))
        lines.append("")

    lines.extend(_inference_lines(doc, rows, drop))

    dev = [s for s in doc["sets"] if s["role"] != "sealed"]
    if dev:
        lines.append("## Development sets (counts and rates only)")
        lines.append("")
        for s in dev:
            srows = list(s["rows"])
            sfams = {r["family_id"] for r in srows}
            selig = eligible_families(srows)
            lines.append(f"### {s['label']}")
            lines.append("")
            lines.append(f"- sha256: {s['sha256']}; rows {len(srows)}; families {len(sfams)}; "
                         f"retrieval-eligible families {len(selig)}")
            splain, sretr, sans = _shapes(srows, modes, k, answers_ran)
            sfr = family_rates(splain, sretr, sans, k=k)
            lines.append(f"- retrieval {primary} strict@{k}: {_fmt_rate(sfr['retrieval'][primary]['strict'])}")
            if "scope" in sfr:
                lines.append(f"- scope correct: {_fmt_rate(sfr['scope'])}")
            sj = [r["judge"] for r in srows if r["judge"] is not None]
            if sj:
                lines.extend(judge_lines(judge_summary(sj)))
            lines.append("")

    exp = doc["expansion"]
    ident = exp.get("identity") or {}
    lines.append("## Expansion")
    lines.append("")
    lines.append(
        f"- {ident.get('kind')}: model {ident.get('model')}; digest {ident.get('digest')}; "
        f"rewrite_live {exp.get('rewrite_live')}, rewrite_replayed {exp.get('rewrite_replayed')}"
    )
    lines.append("")
    lines.append("## Run cost")
    lines.append("")
    cost = doc.get("run_cost")
    if cost is None:
        lines.append("- no spend meter")
    else:
        lines.append(f"- run EUR {cost['run_eur']:.4f}; week EUR {cost['week_eur']:.4f}")
    lines.append("")
    return "\n".join(lines) + "\n"
