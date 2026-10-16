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

Since 16A-2b b1a r6 this module only renders. The metric computations over the stored document (the views, the family-level
hits, the paired baseline, the confirmatory statistics, the strata counts) live in ``src.sealed_results``, which the scorer's
identity (``src.scoring_identity``) covers and this module is not part of; the names are imported here, so they are still
available as ``src.sealed_report.<name>``.

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

from src.eval_v6 import _fmt_rate, _rate, family_rates, judge_lines, judge_summary
from src.sealed_results import (  # the metric computations over the stored document live in a hashed module (r6)
    DESCRIPTIVE_SLOTS,
    FAMILY_RULE,
    RESULTS_KIND,
    RESULTS_VERSION,
    baseline_hits_kept,
    check_cutoff,
    confirmatory_stats,
    dropped_families,
    eligible_families,
    family_hits,
    is_confirmatory,
    kept_rows,
    row_scope_correct,
    sealed_set,
    stored_shapes,
    stratum_numbers,
)


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


def stratum_line(label: str, rows: Sequence[Mapping[str, Any]], primary: str, answers_ran: bool) -> str:
    """One aggregate table row for a stratum (row = phrasing level); the counts are ``sealed_results.stratum_numbers``."""
    n = stratum_numbers(rows, primary, answers_ran)
    cells = [label, str(n["rows"]), str(n["families"]), _fmt_rate(_rate(n["hits"], n["eligible"]), "rows")]
    if answers_ran:
        cells.append(_fmt_rate(_rate(n["scope_hits"], n["scope_n"]), "rows"))
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
    paired_base = baseline_hits_kept(base, drop)
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
    paired_base = baseline_hits_kept(base, drop)
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

    plain, retrieval, answers = stored_shapes(rows, modes, k, answers_ran)
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
            splain, sretr, sans = stored_shapes(srows, modes, k, answers_ran)
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
