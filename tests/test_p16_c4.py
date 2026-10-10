"""Phase 16A-1 items 1, 6, 9: C4 cohort identity, the privacy floor and w_sweep hygiene.

Acceptance (k) for ``scripts/bakeoff_report.py`` and ``scripts/w_sweep.py``
dumps, the w_sweep/bakeoff parts of (b) (private floor, canaries), (c)
(sealed refusal, weaker privacy) and (n) (w_sweep runs from a tmp cwd
without ``chdir``).

Everything is synthetic and built in ``tmp_path``: invented
``P16-CANARY-...`` questions, tmp set files registered through the
``eval_registry`` fixture, reports in the evaluator's v5 Markdown shape, rows
sidecars built with ``src.eval_cohort``. No model, index or network is
touched; w_sweep's retrieval seams are faked as in
``tests/test_bakeoff_instruments.py``.
"""
import ast
import copy
import importlib
import json
import logging
import os
import uuid
from pathlib import Path

import pytest

from scripts import bakeoff_report, w_sweep
from src import eval_privacy, eval_sets
from src.eval_cohort import (
    SCORER_VERSION,
    build_sidecar,
    dump_sidecar,
    sidecar_path,
    v1_cohort,
)
from src.eval_privacy import PrivacyFloorError, SealedInputError, public_v1_id
from src.eval_roster import RoleSpec

C4Error = bakeoff_report.C4Error
LegacyArmError = bakeoff_report.LegacyArmError

TAG = uuid.uuid4().hex[:8]
CANARY = "P16-CANARY"
EXPANSION_OFF = "- query expansion: disabled (offline run)"
EXPANSION_LIVE = "- query expansion: claude-haiku — attempted 3, live 3, fallbacks 0"
DISABLED = {"kind": "disabled", "model": "fixture-rewrite-model"}


# ---------------------------------------------------------------------------
# Synthetic sets, reports and sidecars
# ---------------------------------------------------------------------------
def _q(kind, i):
    return f"{CANARY}-question-{kind}-{i}-{TAG} about fixture widgets?"


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def sets(tmp_path, eval_registry):
    """Two registered public v1 sets: golden (3 answerable + 1 refusal), realistic (3)."""
    golden = [
        {"question": _q("golden", i), "expected_sections": [f"9.{i + 1}"], "type": "direct"}
        for i in range(3)
    ] + [{"question": _q("golden-refusal", 0), "expected_sections": [], "type": "refusal"}]
    realistic = [
        {"question": _q("realistic", i), "expected_sections": [f"8.{i + 1}"], "type": "direct"}
        for i in range(3)
    ]
    out = {}
    for label, rows in (("tuning", golden), ("realistic", realistic)):
        name = "golden_set.jsonl" if label == "tuning" else "realistic_set.jsonl"
        path = _write_jsonl(tmp_path / "sets" / name, rows)
        eval_registry.add(path)
        out[label] = {"path": str(path), "rows": rows, "sha256": eval_sets.sha256_file(path)}
    return out


def _answerable(data):
    return [r for r in data["rows"] if r["type"] != "refusal"]


def _detail_line(strict_rank, expected, question):
    strict = "HIT" if strict_rank is not None else "MISS"
    return (
        f"- [direct] strict={strict}(rank={strict_rank}) "
        f"related={strict}(rank={strict_rank}) "
        f"expected={expected!r} retrieved={['1.1'] * 6!r} :: {question}"
    )


def _ablation(label, n):
    return [
        f"## {label} — retrieval ablation",
        "",
        "| Mode | S@1 | S@3 | S@6 | R@1 | R@3 | R@6 | MRR@6 strict | MRR@6 related | n |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        f"| hybrid | 0.333 | 0.333 | 0.667 | 0.333 | 0.333 | 0.667 | 0.333 | 0.333 | {n} |",
        f"| hybrid+rewrite | 0.333 | 0.333 | 0.667 | 0.333 | 0.333 | 0.667 | 0.333 | 0.333 | {n} |",
        "",
    ]


def build_report(sets, ranks, *, labels=None, expansion_line=EXPANSION_OFF, shown=None):
    """A v5-shaped arm report. ``ranks``: {set label: [strict rank per answerable row]}.

    ``labels`` renames set labels in the report (label-mismatch tests);
    ``shown`` maps a question to what the detail row shows (an opaque id on a
    private report).
    """
    labels = labels or {}
    shown = shown or {}
    lines = [
        "# Legal RAG Evaluation Report v4 (fixture)",
        "",
        "## Provenance",
        "",
        "- embedding model: fixture/model",
        expansion_line,
        "",
        "Question sets:",
    ]
    for label, data in sets.items():
        lines += [
            f"- {labels.get(label, label)}: fixture",
            f"  - path: {data['path']}",
            f"  - sha256: {data['sha256']}",
        ]
    lines.append("")
    for label, data in sets.items():
        lines += _ablation(labels.get(label, label), len(_answerable(data)))
    for label, data in sets.items():
        lines += [f"## {labels.get(label, label)} — per-question detail (hybrid+rewrite)", ""]
        for row, rank in zip(_answerable(data), ranks[label]):
            lines.append(
                _detail_line(rank, row["expected_sections"], shown.get(row["question"], row["question"]))
            )
        lines.append("")
    return "\n".join(lines)


def build_c4(sets, ranks, *, privacy="public", expansion=None, modes=("hybrid", "hybrid+rewrite")):
    """A rows sidecar for ``ranks`` (every mode gets the same ranks)."""
    cohorts, rows = [], []
    for label, data in sets.items():
        block, ids = v1_cohort(data["rows"], path=data["path"], privacy=privacy, sha256=data["sha256"])
        cohorts.append(block)
        for mode in modes:
            for row, rank in zip(_answerable(data), ranks[label]):
                rows.append(
                    {
                        "set_sha256": data["sha256"],
                        "id": ids[row["question"]],
                        "mode": mode,
                        "strict_rank": rank,
                        "related_rank": rank,
                        "completion_rank": rank,
                    }
                )
    return build_sidecar(
        privacy=privacy, cohorts=cohorts, rows=rows, expansion=expansion or DISABLED
    )


BASE_RANKS = {"tuning": [1, None, 4], "realistic": [2, None, 3]}
# golden row 0 HIT->MISS, row 1 MISS->HIT, row 2 unchanged HIT.
CAND_RANKS = {"tuning": [None, 1, 2], "realistic": [2, None, 3]}


def write_arm(directory, name, sets, ranks, *, sidecar=True, sidecar_doc=None, **report_kw):
    """Write ``<name>.md`` (+ ``<name>.md.rows.json``); return the report path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(build_report(sets, ranks, **report_kw), encoding="utf-8")
    if sidecar:
        doc = sidecar_doc if sidecar_doc is not None else build_c4(sets, ranks)
        Path(sidecar_path(str(path))).write_text(dump_sidecar(doc), encoding="utf-8")
    return str(path)


def load_arms(*paths):
    return {Path(p).stem: bakeoff_report.load_arm(p) for p in paths}


def gid(sets, i):
    return public_v1_id(_answerable(sets["tuning"])[i]["question"])


def _cli(argv, capsys):
    rc = bakeoff_report.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


# ---------------------------------------------------------------------------
# C4: matching arms, permuted rows, render
# ---------------------------------------------------------------------------
def test_c4_matching_arms_compare_by_id(tmp_path, sets):
    base = write_arm(tmp_path / "arms", "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path / "arms", "cand", sets, CAND_RANKS)
    result = bakeoff_report.compare(load_arms(base, cand), "base")
    assert result["c4"] is True
    assert result["flips"]["cand"] == {
        "hit_to_miss": [gid(sets, 0)],
        "miss_to_hit": [gid(sets, 1)],
        "unmatched": [],
    }


def test_c4_permuted_rows_match_by_id(tmp_path, sets):
    """Row order in a sidecar is irrelevant: rows are keyed by (set sha256, id)."""
    base = write_arm(tmp_path / "a", "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path / "a", "cand", sets, CAND_RANKS)
    doc = build_c4(sets, CAND_RANKS)
    doc["rows"] = list(reversed(doc["rows"]))
    doc["cohorts"] = list(reversed(doc["cohorts"]))
    cand_perm = write_arm(tmp_path / "b", "cand", sets, CAND_RANKS, sidecar_doc=doc)
    straight = bakeoff_report.compare(load_arms(base, cand), "base")
    permuted = bakeoff_report.compare(load_arms(base, cand_perm), "base")
    assert straight["flips"] == permuted["flips"]


def test_c4_public_cli_renders_ids_with_question_text(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 0, err
    assert "C4 cohort identity checked" in out
    q0 = _answerable(sets["tuning"])[0]["question"]
    assert f"- {gid(sets, 0)} — {q0}" in out
    assert bakeoff_report.LEGACY_NOTE not in err


# ---------------------------------------------------------------------------
# (k) identity mismatches
# ---------------------------------------------------------------------------
def _pair(sets):
    base = build_c4(sets, BASE_RANKS)
    return base, copy.deepcopy(base)


def _labels(sets):
    return {d["sha256"]: label for label, d in sets.items()}


def _check(base, arm, *, base_labels, arm_labels=None, rewrite_candidate=None):
    bakeoff_report.check_c4_identity(
        base, arm, name="arm", base_labels=base_labels,
        arm_labels=arm_labels if arm_labels is not None else base_labels,
        rewrite_candidate=rewrite_candidate,
    )


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d["cohorts"][0].update(sha256="f" * 64), "set hashes differ"),
        (lambda d: d["cohorts"][0].update(cohort_fp="0" * 64), "cohort_fp differs"),
        (lambda d: d.update(scorer_version="other-scorer"), "scorer version"),
        (lambda d: d.update(absorbed_map_sha256="e" * 64), "absorbed-map hash"),
        (lambda d: d.update(expansion={"kind": "replay", "model": "m", "digest": "d1"}),
         "expansion identity differs"),
    ],
)
def test_c4_identity_mismatch_is_refused(sets, mutate, match):
    base, arm = _pair(sets)
    mutate(arm)
    with pytest.raises(C4Error, match=match):
        _check(base, arm, base_labels=_labels(sets))


def test_c4_label_mismatch_is_refused(sets):
    base, arm = _pair(sets)
    other = {sha: ("golden" if lab == "tuning" else lab) for sha, lab in _labels(sets).items()}
    with pytest.raises(C4Error, match="labelled"):
        _check(base, arm, base_labels=_labels(sets), arm_labels=other)


def test_c4_expansion_digest_mismatch_is_refused(sets):
    base, arm = _pair(sets)
    base["expansion"] = {"kind": "cache", "digest": "a" * 64}
    arm["expansion"] = {"kind": "cache", "digest": "b" * 64}
    with pytest.raises(C4Error, match="expansion identity differs"):
        _check(base, arm, base_labels=_labels(sets))
    replay = {"kind": "replay", "model": "m", "prompt_sha256": "p", "config_hash": "c"}
    base["expansion"] = dict(replay, digest="d1")
    arm["expansion"] = dict(replay, digest="d2")
    with pytest.raises(C4Error, match="expansion identity differs"):
        _check(base, arm, base_labels=_labels(sets))


def test_c4_label_mismatch_end_to_end(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, labels={"tuning": "golden"})
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 2
    assert "labelled" in err and out == ""


def test_c4_scorer_mismatch_end_to_end(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    doc = build_c4(sets, CAND_RANKS)
    doc["scorer_version"] = "p16a1-v0"
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar_doc=doc)
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 2 and "scorer version" in err and out == ""


LIVE = {"kind": "live", "model": "m", "prompt_sha256": "p" * 64, "config_hash": "c" * 64}


def test_two_live_arms_may_differ_in_draw_digest_only(sets):
    base, arm = _pair(sets)
    base["expansion"] = dict(LIVE, digest="1" * 64)
    arm["expansion"] = dict(LIVE, digest="2" * 64)
    _check(base, arm, base_labels=_labels(sets))  # accepted
    arm["expansion"] = dict(LIVE, digest="2" * 64, prompt_sha256="q" * 64)
    with pytest.raises(C4Error, match="beyond the draw digest"):
        _check(base, arm, base_labels=_labels(sets))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda d: d["cohorts"][0].update(sha256="f" * 64), "set hashes differ"),
        (lambda d: d.update(scorer_version="other"), "scorer version"),
        (lambda d: d.update(absorbed_map_sha256="e" * 64), "absorbed-map hash"),
    ],
)
def test_live_live_and_candidate_pairs_still_fail_on_set_scorer_or_map(sets, mutate, match):
    base, arm = _pair(sets)
    base["expansion"] = dict(LIVE, digest="1" * 64)
    arm["expansion"] = dict(LIVE, digest="2" * 64)
    mutate(arm)
    with pytest.raises(C4Error, match=match):
        _check(base, arm, base_labels=_labels(sets))
    base, arm = _pair(sets)
    base["expansion"] = dict(LIVE, kind="replay", digest="1" * 64)
    arm["expansion"] = dict(LIVE, kind="replay", digest="2" * 64, config_hash="9" * 64)
    mutate(arm)
    with pytest.raises(C4Error, match=match):
        _check(base, arm, base_labels=_labels(sets), rewrite_candidate="9" * 64)


def test_declared_rewrite_candidate_is_accepted_only_for_its_config(sets):
    base, arm = _pair(sets)
    base["expansion"] = dict(LIVE, kind="replay", digest="1" * 64)
    arm["expansion"] = dict(
        LIVE, kind="replay", digest="2" * 64, config_hash="9" * 64, prompt_sha256="q" * 64
    )
    with pytest.raises(C4Error, match="expansion identity differs"):
        _check(base, arm, base_labels=_labels(sets))  # undeclared
    _check(base, arm, base_labels=_labels(sets), rewrite_candidate="9" * 64)  # declared
    with pytest.raises(C4Error, match="expansion identity differs"):
        _check(base, arm, base_labels=_labels(sets), rewrite_candidate="8" * 64)  # wrong hash
    arm["expansion"]["kind"] = "live"  # a kind change is not a config difference
    with pytest.raises(C4Error):
        _check(base, arm, base_labels=_labels(sets), rewrite_candidate="9" * 64)
    arm["expansion"] = dict(base["expansion"], config_hash="9" * 64, extra_field="x")
    with pytest.raises(C4Error, match="expansion identity differs"):
        _check(base, arm, base_labels=_labels(sets), rewrite_candidate="9" * 64)


def test_rewrite_candidate_cli_accepts_live_reports_with_sidecars(tmp_path, sets, capsys):
    """C4 arms are judged by their expansion identity, not the offline marker."""
    base_doc = build_c4(sets, BASE_RANKS, expansion=dict(LIVE, digest="1" * 64))
    cand_doc = build_c4(
        sets, CAND_RANKS, expansion=dict(LIVE, digest="2" * 64, config_hash="9" * 64)
    )
    base = write_arm(tmp_path, "base", sets, BASE_RANKS, sidecar_doc=base_doc,
                     expansion_line=EXPANSION_LIVE)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar_doc=cand_doc,
                     expansion_line=EXPANSION_LIVE)
    rc, _out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 2 and "expansion identity" in err
    rc, out, err = _cli(
        ["--reports", base, cand, "--baseline", "base", "--rewrite-candidate", "9" * 64], capsys
    )
    assert rc == 0, err
    assert gid(sets, 0) in out


# ---------------------------------------------------------------------------
# (k) legacy
# ---------------------------------------------------------------------------
def test_legacy_arm_is_refused_without_the_flag(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar=False)
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 2
    assert "refused without --legacy" in err and out == ""
    with pytest.raises(LegacyArmError):
        bakeoff_report.compare(load_arms(base, cand), "base")


def test_post_16a_report_whose_sidecar_was_deleted_is_refused(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    os.remove(sidecar_path(cand))
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 2 and "refused without --legacy" in err and out == ""


def test_legacy_flag_applies_v5_rules_with_the_note_and_no_refusal(tmp_path, sets, capsys):
    """With --legacy, a mixed C4/legacy pair renders exactly what two plain v5
    reports render (v5 output unchanged), and the note is printed."""
    mixed_base = write_arm(tmp_path / "mixed", "base", sets, BASE_RANKS)
    mixed_cand = write_arm(tmp_path / "mixed", "cand", sets, CAND_RANKS, sidecar=False)
    plain_base = write_arm(tmp_path / "plain", "base", sets, BASE_RANKS, sidecar=False)
    plain_cand = write_arm(tmp_path / "plain", "cand", sets, CAND_RANKS, sidecar=False)
    rc1, out_mixed, err_mixed = _cli(
        ["--reports", mixed_base, mixed_cand, "--baseline", "base", "--legacy"], capsys
    )
    rc2, out_plain, err_plain = _cli(
        ["--reports", plain_base, plain_cand, "--baseline", "base", "--legacy"], capsys
    )
    assert rc1 == rc2 == 0
    assert out_mixed == out_plain
    assert bakeoff_report.LEGACY_NOTE in err_mixed and bakeoff_report.LEGACY_NOTE in err_plain
    assert "All arms offline (expansion disabled)" in out_plain
    q0 = _answerable(sets["tuning"])[0]["question"]
    assert f"- {q0}" in out_plain  # v5: full question text


def test_legacy_flag_is_recorded_in_the_manifest(tmp_path, sets, capsys):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS, sidecar=False)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar=False)
    manifest = tmp_path / "manifest.json"
    rc, _out, _err = _cli(
        ["--reports", base, cand, "--baseline", "base", "--legacy", "--manifest-out", str(manifest)],
        capsys,
    )
    assert rc == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["legacy"] is True and data["c4"] is False
    assert data["arms"]["base"]["sidecar"] is None


# ---------------------------------------------------------------------------
# (k) row coverage
# ---------------------------------------------------------------------------
def _with_rows(sets, ranks, edit):
    doc = build_c4(sets, ranks)
    edit(doc)
    return doc


def _compare_docs(tmp_path, sets, cand_doc):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar_doc=cand_doc)
    return bakeoff_report.compare(load_arms(base, cand), "base")


def _hybrid_rows(doc):
    return [r for r in doc["rows"] if r["mode"] == "hybrid"]


def test_missing_row_fails(tmp_path, sets):
    doc = _with_rows(sets, CAND_RANKS, lambda d: d["rows"].remove(_hybrid_rows(d)[0]))
    with pytest.raises(C4Error, match="missing rows|roster mismatch"):
        _compare_docs(tmp_path, sets, doc)


def test_extra_row_fails(tmp_path, sets):
    def add(d):
        d["rows"].append(dict(_hybrid_rows(d)[0], id="q:ffffffffffff"))

    with pytest.raises(C4Error, match="extra rows|roster mismatch"):
        _compare_docs(tmp_path, sets, _with_rows(sets, CAND_RANKS, add))


# 16A-1 merge gate, Codex #4: the SAME gap in both arms (arm-vs-arm agreement
# holds) must still fail against the cohort's eligible roster.
def _drop_golden_hit(doc, sets):
    """Remove golden row 0 (a baseline HIT -> MISS flip) from every mode."""
    doc["rows"] = [r for r in doc["rows"] if r["id"] != gid(sets, 0)]


def _swap_golden_hit(doc, sets):
    """Replace golden row 0's id with an id outside the set, in every mode."""
    for r in doc["rows"]:
        if r["id"] == gid(sets, 0):
            r["id"] = "q:ffffffffffff"


@pytest.mark.parametrize("edit", [_drop_golden_hit, _swap_golden_hit], ids=["deletion", "substitution"])
def test_symmetric_roster_gap_in_sidecars_fails(tmp_path, sets, edit):
    base_doc, cand_doc = build_c4(sets, BASE_RANKS), build_c4(sets, CAND_RANKS)
    edit(base_doc, sets)
    edit(cand_doc, sets)
    def keys(doc):
        return sorted((r["set_sha256"], r["mode"], r["id"]) for r in doc["rows"])

    assert keys(base_doc) == keys(cand_doc)  # the two arms still agree with each other
    base = write_arm(tmp_path, "base", sets, BASE_RANKS, sidecar_doc=base_doc)
    with pytest.raises(C4Error, match="roster mismatch"):
        cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar_doc=cand_doc)
        bakeoff_report.compare(load_arms(base, cand), "base")


@pytest.mark.parametrize("edit", [_drop_golden_hit, _swap_golden_hit], ids=["deletion", "substitution"])
def test_symmetric_roster_gap_in_prod_rank_dumps_fails(sets, edit):
    base, arm = _dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS)
    edit(base, sets)
    edit(arm, sets)
    with pytest.raises(C4Error, match="roster mismatch"):
        bakeoff_report.compare_prod_ranks(base, arm)


def test_symmetric_roster_gap_in_v1_cohort_from_two_answerable_rows_fails(tmp_path, eval_registry):
    """Codex's probe: a v1_cohort over two answerable rows, one eligible row
    removed from BOTH production dumps."""
    rows = [{"question": _q("pair", i), "expected_sections": ["9.1"], "type": "direct"} for i in range(2)]
    path = _write_jsonl(tmp_path / "pair" / "golden_set.jsonl", rows)
    sha = eval_sets.sha256_file(path)
    block, ids = v1_cohort(rows, path=str(path), privacy="public", sha256=sha)
    block["label"] = "golden"

    def dump(rank):
        return build_sidecar(privacy="public", cohorts=[block], expansion={"kind": "cache", "digest": "a" * 64},
                             rows=[{"set_sha256": sha, "id": ids[rows[0]["question"]], "mode": "W=0.25",
                                    "strict_rank": rank, "related_rank": rank, "completion_rank": rank}])

    with pytest.raises(C4Error, match="roster mismatch"):
        bakeoff_report.compare_prod_ranks(dump(1), dump(1))


def test_cohort_block_without_its_roster_is_refused(tmp_path, sets):
    doc = build_c4(sets, CAND_RANKS)
    del doc["cohorts"][0]["eligible_fp"]
    with pytest.raises(C4Error, match="eligible roster"):
        bakeoff_report.index_rows(doc, "cand")


def test_duplicate_row_fails(tmp_path, sets):
    doc = _with_rows(sets, CAND_RANKS, lambda d: d["rows"].append(dict(d["rows"][0])))
    with pytest.raises(C4Error, match="duplicate row"):
        _compare_docs(tmp_path, sets, doc)


def test_row_missing_a_required_field_fails(tmp_path, sets):
    doc = _with_rows(sets, CAND_RANKS, lambda d: d["rows"][0].pop("completion_rank"))
    with pytest.raises(C4Error, match="lacks"):
        _compare_docs(tmp_path, sets, doc)


def test_sidecar_disagreeing_with_its_report_fails(tmp_path, sets):
    """The golden hybrid+rewrite rows must be exactly the report's detail rows."""
    def drop(d):
        row = next(r for r in d["rows"] if r["mode"] == "hybrid+rewrite")
        d["rows"].remove(row)

    with pytest.raises(C4Error, match="golden rows differ|missing rows|roster mismatch"):
        _compare_docs(tmp_path, sets, _with_rows(sets, CAND_RANKS, drop))


def test_malformed_sidecar_is_refused_not_downgraded(tmp_path, sets):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    Path(sidecar_path(base)).write_text('{"version": 1}', encoding="utf-8")
    with pytest.raises(C4Error, match="not a C4 document"):
        bakeoff_report.load_arm(base)


# ---------------------------------------------------------------------------
# (k) production-rank dumps
# ---------------------------------------------------------------------------
def _dump(sets, ranks, *, digest="a" * 64, weight="W=0.25"):
    doc = build_c4(sets, ranks, modes=("W=0.0", weight), expansion={"kind": "cache", "digest": digest})
    for cohort, (label, _d) in zip(doc["cohorts"], sets.items()):
        cohort["label"] = "golden" if label == "tuning" else label
    return doc


def test_prod_ranks_c4_flips_by_id(sets):
    out = bakeoff_report.compare_prod_ranks(_dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS))
    assert out["c4"] is True
    assert [r["id"] for r in out["flips"]] == [gid(sets, 0)]
    assert [r["id"] for r in out["gains"]] == [gid(sets, 1)]
    assert all("question" not in r for r in out["flips"] + out["gains"])


def test_prod_ranks_missing_baseline_hit_row_fails(sets):
    """Today's skip (``if arow is None: continue``) is gone: a baseline HIT the
    arm does not cover fails instead of vanishing."""
    arm = _dump(sets, CAND_RANKS)
    hit_id = gid(sets, 2)  # strict rank 4 in the baseline: a HIT
    arm["rows"] = [r for r in arm["rows"] if not (r["id"] == hit_id and r["mode"] == "W=0.25")]
    with pytest.raises(C4Error, match="missing rows|roster mismatch"):
        bakeoff_report.compare_prod_ranks(_dump(sets, BASE_RANKS), arm)


def test_prod_ranks_cache_digest_mismatch_is_refused(sets):
    with pytest.raises(C4Error, match="expansion identity differs"):
        bakeoff_report.compare_prod_ranks(
            _dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS, digest="b" * 64)
        )


def test_prod_ranks_permuted_rows_match_by_id(sets):
    arm = _dump(sets, CAND_RANKS)
    arm["rows"] = list(reversed(arm["rows"]))
    assert (
        bakeoff_report.compare_prod_ranks(_dump(sets, BASE_RANKS), arm)
        == bakeoff_report.compare_prod_ranks(_dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS))
    )


def test_prod_ranks_legacy_dump_needs_the_flag(sets):
    legacy = {"ranks": {"W=0.25|golden|0": {"question": "q", "strict_rank": 1}}}
    with pytest.raises(LegacyArmError):
        bakeoff_report.compare_prod_ranks(legacy, legacy)
    with pytest.raises(LegacyArmError):
        bakeoff_report.compare_prod_ranks(_dump(sets, BASE_RANKS), legacy)
    out = bakeoff_report.compare_prod_ranks(legacy, legacy, legacy=True)
    assert out["c4"] is False and out["flips"] == []


def test_prod_ranks_no_rows_at_the_weight_fails(sets):
    with pytest.raises(C4Error, match="no production-rank rows"):
        bakeoff_report.compare_prod_ranks(
            _dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS), weight=0.5
        )


# ---------------------------------------------------------------------------
# (k) controls
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "doc",
    [
        {"version": 1, "controls": []},
        {"version": 1},
        {"version": 1, "controls": [{"set_sha256": "a" * 64, "ids": []}]},
        {"version": 2, "controls": [{"set_sha256": "a" * 64, "ids": ["q:000000000000"]}]},
    ],
)
def test_empty_or_malformed_controls_are_refused(doc):
    with pytest.raises(C4Error):
        bakeoff_report.parse_controls(doc)


def test_unresolved_controls_are_refused(tmp_path, sets, capsys):
    golden_sha = sets["tuning"]["sha256"]
    controls = [(golden_sha, "q:ffffffffffff")]
    with pytest.raises(C4Error, match="does not resolve"):
        bakeoff_report.compare_prod_ranks(
            _dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS), controls=controls
        )
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    with pytest.raises(C4Error, match="does not resolve"):
        bakeoff_report.compare(load_arms(base, cand), "base", controls=controls)
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"version": 1, "controls": []}), encoding="utf-8")
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base", "--controls", str(path)], capsys)
    # a controls file floors the run to private (merge gate #2): refusal type only
    assert rc == 2 and "C4Error" in err and out == ""


def test_control_flips_are_reported_apart(tmp_path, sets, capsys):
    golden_sha = sets["tuning"]["sha256"]
    controls = [(golden_sha, gid(sets, 0))]  # the row that flips HIT->MISS
    out = bakeoff_report.compare_prod_ranks(
        _dump(sets, BASE_RANKS), _dump(sets, CAND_RANKS), controls=controls
    )
    assert out["flips"] == []
    assert [r["id"] for r in out["control_flips"]] == [gid(sets, 0)]
    assert [r["id"] for r in out["gains"]] == [gid(sets, 1)]

    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    result = bakeoff_report.compare(load_arms(base, cand), "base", controls=controls)
    assert result["flips"]["cand"]["hit_to_miss"] == []
    assert result["control_flips"]["cand"]["hit_to_miss"] == [gid(sets, 0)]
    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps({"version": 1, "controls": [{"set_sha256": golden_sha, "ids": [gid(sets, 0)]}]}),
        encoding="utf-8",
    )
    rc, text, err = _cli(
        ["--reports", base, cand, "--baseline", "base", "--controls", str(path)], capsys
    )
    assert rc == 0, err
    assert "Controls (1, reported apart)" in text


def _controls_file(tmp_path, golden_sha, ids):
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"version": 1, "controls": [{"set_sha256": golden_sha, "ids": ids}]}),
                    encoding="utf-8")
    return path


def test_controls_file_is_in_the_floor(tmp_path, sets):
    """16A-1 merge gate, Codex #2: the --controls file is an opened input, so a
    public caller value over public reports plus a controls file is weaker than
    the floor (classify rule 7: a controls file is private)."""
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    controls = _controls_file(tmp_path, sets["tuning"]["sha256"], [gid(sets, 0)])
    with pytest.raises(PrivacyFloorError):
        bakeoff_report.run([base, cand], "base", privacy="public", controls_path=str(controls))


def test_malformed_control_value_never_reaches_stderr(tmp_path, sets, capsys, caplog):
    """A duplicated control carrying private text is refused with its type only."""
    caplog.set_level(logging.DEBUG)
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    bad = f"{CANARY}-control-value-{TAG} vexing fixture widget text"
    controls = _controls_file(tmp_path, sets["tuning"]["sha256"], [bad, bad])
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base", "--controls", str(controls)], capsys)
    assert rc == 2 and out == ""
    assert "C4Error" in err
    _assert_no_canary(out, err, caplog.text)


def test_private_controls_run_records_controls_in_inputs_json_and_passes_precheck(
    tmp_path, monkeypatch, sets, capsys, _private_root_in_tmp
):
    from scripts import scan_leaks

    base = write_arm(tmp_path / "arms", "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path / "arms", "cand", sets, CAND_RANKS)
    golden_sha = sets["tuning"]["sha256"]
    controls = _controls_file(tmp_path, golden_sha, [gid(sets, 0)])
    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base", "--controls", str(controls),
                         "--manifest-out", str(tmp_path / "m.json")], capsys)
    assert rc == 0, err
    assert "Controls (1, reported apart)" in out and gid(sets, 0) in out
    assert not (tmp_path / "m.json").exists()
    (inputs_json,) = list((_private_root_in_tmp / "runs").glob("*/inputs.json"))
    items = json.loads(inputs_json.read_text(encoding="utf-8"))["inputs"]
    (entry,) = [i for i in items if i["path"] == os.path.abspath(controls)]
    assert entry["kind"] == "derived" and entry["sha256"] == eval_sets.sha256_file(controls)
    assert [s["sha256"] for s in entry["sources"]] == [golden_sha]
    assert scan_leaks.precheck(set()) == []


def test_controls_with_legacy_arms_are_refused(tmp_path, sets):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS, sidecar=False)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar=False)
    with pytest.raises(C4Error, match="need C4"):
        bakeoff_report.compare(
            load_arms(base, cand), "base", legacy=True,
            controls=[(sets["tuning"]["sha256"], gid(sets, 0))],
        )


# ---------------------------------------------------------------------------
# w_sweep: no chdir, tmp cwd, C4 dump round trip
# ---------------------------------------------------------------------------
class _FakeDoc:
    def __init__(self, section):
        self.metadata = {"section_number": section}


def _explode(*_a, **_k):
    raise AssertionError("expand_query called — the sweep spent an API call")


def _legacy_public(monkeypatch, tmp_path, *paths):
    legacy = tmp_path / "legacy_public.json"
    legacy.write_text(
        json.dumps({"version": 1, "entries": [
            {"path": str(p), "sha256": eval_sets.sha256_file(p)} for p in paths
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", legacy)


def stub_sweep(
    monkeypatch, tmp_path, sets, calls, *, sections=("9.1", "8.1", "9.3", "8.3", "9.2", "8.2"),
    rewrites=("rw",), intent="it", retrieve_exc=None, ctx_exc=None,
):
    """Fake w_sweep's seams over the ``sets`` fixture; the cache is legacy-public.

    ``rewrites``/``intent`` fill every cache entry; ``retrieve_exc``/``ctx_exc``
    make the per-row retrieval / the top-level context load raise.
    """
    monkeypatch.setattr(
        w_sweep, "SET_PATHS",
        (("golden", sets["tuning"]["path"]), ("realistic", sets["realistic"]["path"])),
    )
    realistic = _answerable(sets["realistic"])
    monkeypatch.setattr(
        w_sweep, "ROLE_BY_NAME",
        {
            "S5": RoleSpec("S5", public_v1_id(realistic[0]["question"]), ("8.1",)),
            "N4": RoleSpec("N4", public_v1_id(realistic[1]["question"]), ("8.2",)),
        },
    )
    cache = tmp_path / "cache" / "expansions.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({
            r["question"]: {"rewrites": list(rewrites), "status": w_sweep.STATUS_LIVE, "intent": intent}
            for data in sets.values() for r in _answerable(data)
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(w_sweep, "CACHE", str(cache))
    _legacy_public(monkeypatch, tmp_path, cache)
    monkeypatch.setattr(w_sweep, "expand_query", _explode)

    def fake_ctx(persist_directory=None):
        calls["persist_directory"] = persist_directory
        if ctx_exc is not None:
            raise RuntimeError(ctx_exc)
        return ("vs", "bm")

    def fake_retrieve(question, **kwargs):
        if retrieve_exc is not None:
            raise RuntimeError(retrieve_exc)
        calls.setdefault("retrieve", 0)
        calls["retrieve"] += 1
        return [{"document": _FakeDoc(s)} for s in sections]

    monkeypatch.setattr(w_sweep, "load_retrieval_context", fake_ctx)
    monkeypatch.setattr(w_sweep, "retrieve", fake_retrieve)
    return cache


def test_w_sweep_has_no_chdir_anywhere():
    tree = ast.parse(Path(w_sweep.__file__).read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "chdir"
    ]
    assert calls == []


def test_w_sweep_import_does_not_change_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    importlib.reload(w_sweep)
    assert os.getcwd() == str(tmp_path)
    assert os.path.isabs(w_sweep.CACHE) and os.path.isabs(w_sweep.DEFAULT_PERSIST_DIR)
    assert all(os.path.isabs(p) for _l, p in w_sweep.SET_PATHS)


def test_w_sweep_runs_from_a_tmp_cwd_and_its_dumps_compare_under_c4(
    tmp_path, monkeypatch, sets, capsys
):
    """(n): from a tmp cwd, with no chdir, two arms' dumps round-trip into C4."""
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    calls = {}
    stub_sweep(monkeypatch, tmp_path, sets, calls)
    assert w_sweep.main(["--ranks-out", "base.json", "--legacy-public"]) == 0
    stub_sweep(monkeypatch, tmp_path, sets, calls, sections=("1.1",) * 6)
    assert w_sweep.main(["--ranks-out", "arm.json", "--legacy-public"]) == 0
    capsys.readouterr()
    assert os.getcwd() == str(work)
    base = json.loads((work / "base.json").read_text(encoding="utf-8"))
    arm = json.loads((work / "arm.json").read_text(encoding="utf-8"))
    out = bakeoff_report.compare_prod_ranks(base, arm)
    assert out["c4"] is True
    # Every golden row the baseline hit is lost by the all-"1.1" arm.
    assert sorted(r["id"] for r in out["flips"]) == sorted(gid(sets, i) for i in range(3))


# ---------------------------------------------------------------------------
# Privacy floor: private runs, canaries, sealed input, weaker privacy
# ---------------------------------------------------------------------------
def _files_outside_private_root(root, private_root, exclude):
    exclude = {Path(p).resolve() for p in exclude}
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.resolve() in exclude:
            continue
        if eval_privacy.is_under(path, private_root):
            continue
        yield path


def _assert_no_canary(*texts):
    """No canary prefix, and no question or 8-token window of one, in any form (acceptance (b))."""
    from tests.p16_canary import assert_no_leak

    for text in texts:
        assert CANARY not in text
    secrets = [_q(kind, i) for kind in ("golden", "golden-refusal", "realistic", "refusal") for i in range(10)]
    assert_no_leak([*secrets, TAG], *texts, where="(w_sweep/bakeoff)")


def test_private_w_sweep_writes_only_under_the_private_root_and_prints_ids(
    tmp_path, monkeypatch, sets, capsys, caplog, _private_root_in_tmp
):
    caplog.set_level(logging.DEBUG)
    calls = {}
    cache = stub_sweep(monkeypatch, tmp_path, sets, calls)
    requested = tmp_path / "out" / "ranks.json"
    assert w_sweep.main(["--ranks-out", str(requested)]) == 0  # no --legacy-public: private
    captured = capsys.readouterr()
    _assert_no_canary(captured.out, captured.err, caplog.text)
    assert not requested.exists()
    dumps = list((_private_root_in_tmp / "runs").glob("*/ranks.json"))
    assert len(dumps) == 1
    inputs = json.loads((dumps[0].parent / "inputs.json").read_text(encoding="utf-8"))
    kinds = {Path(e["path"]).name: e["kind"] for e in inputs["inputs"]}
    assert kinds == {"golden_set.jsonl": "questions", "realistic_set.jsonl": "questions",
                     "expansions.json": "derived"}
    dump = json.loads(dumps[0].read_text(encoding="utf-8"))
    assert dump["privacy"] == "private" and "ranks" not in dump
    _assert_no_canary(dumps[0].read_text(encoding="utf-8"))
    inputs_written = [sets["tuning"]["path"], sets["realistic"]["path"], cache]
    for path in _files_outside_private_root(tmp_path, _private_root_in_tmp, inputs_written):
        _assert_no_canary(path.read_text(encoding="utf-8", errors="replace"))
    # Salted (private) ids, not the public ones.
    assert gid(sets, 0) not in {r["id"] for r in dump["rows"]}


def test_private_w_sweep_error_prints_only_the_exception_type(
    tmp_path, monkeypatch, sets, capsys
):
    calls = {}
    cache = stub_sweep(monkeypatch, tmp_path, sets, calls)
    data = json.loads(cache.read_text(encoding="utf-8"))
    data[_answerable(sets["tuning"])[0]["question"]]["status"] = "fallback"
    cache.write_text(json.dumps(data), encoding="utf-8")
    assert w_sweep.main([]) == 1
    captured = capsys.readouterr()
    _assert_no_canary(captured.out, captured.err)
    assert "RuntimeError" in captured.err
    assert calls.get("retrieve") is None


def test_private_bakeoff_prints_ids_only_and_writes_under_the_private_root(
    tmp_path, monkeypatch, sets, capsys, caplog, _private_root_in_tmp
):
    caplog.set_level(logging.DEBUG)
    arms_dir = tmp_path / "arms"
    base = write_arm(arms_dir, "base", sets, BASE_RANKS)
    cand = write_arm(arms_dir, "cand", sets, CAND_RANKS)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "base.json").write_text(json.dumps(_dump(sets, BASE_RANKS)), encoding="utf-8")
    (dumps / "cand.json").write_text(json.dumps(_dump(sets, CAND_RANKS)), encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text("{}", encoding="utf-8")  # not legacy-listed -> private floor
    requested = tmp_path / "manifest.json"
    rc, out, err = _cli(
        ["--reports", base, cand, "--baseline", "base",
         "--prod-ranks", str(dumps / "base.json"), str(dumps / "cand.json"),
         "--expansion-cache", str(cache), "--manifest-out", str(requested)],
        capsys,
    )
    assert rc == 0, err
    _assert_no_canary(out, err, caplog.text)
    assert gid(sets, 0) in out  # flips render as ids
    assert not requested.exists()
    manifests = list((_private_root_in_tmp / "runs").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert (manifests[0].parent / "inputs.json").is_file()
    inputs_written = [base, cand, sidecar_path(base), sidecar_path(cand),
                      sets["tuning"]["path"], sets["realistic"]["path"]]
    for path in _files_outside_private_root(tmp_path, _private_root_in_tmp, inputs_written):
        _assert_no_canary(path.read_text(encoding="utf-8", errors="replace"))


# --- acceptance (b): distinct canaries through the cache, evidence, ids and exceptions ----
def _canary_sets(tmp_path, eval_registry, c):
    """Like ``sets`` but start/end canary questions and canary evidence sections."""
    ev = c.value("evidence")
    golden = [{"question": c.question(i), "expected_sections": [ev], "type": "direct"} for i in range(3)]
    golden.append({"question": c.question(3), "expected_sections": [], "type": "refusal"})
    realistic = [{"question": c.question(4 + i), "expected_sections": [ev], "type": "direct"} for i in range(3)]
    out = {}
    for label, rows, name in (("tuning", golden, "golden_set.jsonl"), ("realistic", realistic, "realistic_set.jsonl")):
        path = _write_jsonl(tmp_path / "sets" / name, rows)
        eval_registry.add(path)
        out[label] = {"path": str(path), "rows": rows, "sha256": eval_sets.sha256_file(path)}
    return out


def _canary_secrets(c):
    fields = ("qstart", "qend", "gap", "evidence", "badid", "rowexc", "topexc", "rewrite", "intent",
              "answer", "claim", "loader")
    return [*(c.value(f) for f in fields), *(c.question(i) for i in range(7)), *c.all_tokens()]


def _assert_canary_clean(c, tmp_path, private_root, exclude, *texts, where):
    """Canaries absent from the texts, every non-input file outside the private root, and repo eval/."""
    from tests.p16_canary import assert_no_leak

    files = [p.read_text(encoding="utf-8", errors="replace")
             for p in _files_outside_private_root(tmp_path, private_root, exclude)]
    assert_no_leak(_canary_secrets(c), *texts, *files, where=where)


@pytest.fixture
def canaries():
    from tests.test_eval_privacy import Canaries

    return Canaries(seed=1611)


def test_private_w_sweep_canary_cache_evidence_and_exceptions_stay_private(
    tmp_path, monkeypatch, eval_registry, capsys, caplog, canaries, _private_root_in_tmp
):
    from tests.test_eval_privacy import _eval_snapshot

    c = canaries
    caplog.set_level(logging.DEBUG)
    sets_c = _canary_sets(tmp_path, eval_registry, c)
    before = _eval_snapshot()
    ev = c.value("evidence")
    cache = stub_sweep(
        monkeypatch, tmp_path, sets_c, {}, sections=(ev, c.value("badid")) + (ev,) * 4,
        rewrites=(c.value("rewrite"), f"{c.value('rewrite')} {c.value('gap')}"), intent=c.value("intent"),
    )
    requested = tmp_path / "out" / "ranks.json"
    assert w_sweep.main(["--ranks-out", str(requested)]) == 0
    captured = capsys.readouterr()
    dumps = list((_private_root_in_tmp / "runs").glob("*/ranks.json"))
    assert len(dumps) == 1
    inputs = [sets_c["tuning"]["path"], sets_c["realistic"]["path"], cache]
    _assert_canary_clean(c, tmp_path, _private_root_in_tmp, inputs, captured.out, captured.err,
                         caplog.text, dumps[0].read_text(encoding="utf-8"), where="(w_sweep canaries)")
    assert _eval_snapshot() == before


@pytest.mark.parametrize("which", ["rowexc", "topexc"])
def test_private_w_sweep_exception_text_is_never_printed(
    tmp_path, monkeypatch, eval_registry, capsys, caplog, canaries, _private_root_in_tmp, which
):
    c = canaries
    caplog.set_level(logging.DEBUG)
    sets_c = _canary_sets(tmp_path, eval_registry, c)
    kw = {"retrieve_exc": c.value(which)} if which == "rowexc" else {"ctx_exc": c.value(which)}
    cache = stub_sweep(monkeypatch, tmp_path, sets_c, {}, rewrites=(c.value("rewrite"),),
                       intent=c.value("intent"), **kw)
    assert w_sweep.main([]) == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    inputs = [sets_c["tuning"]["path"], sets_c["realistic"]["path"], cache]
    _assert_canary_clean(c, tmp_path, _private_root_in_tmp, inputs, captured.out, captured.err,
                         caplog.text, where=f"(w_sweep {which})")


def test_private_bakeoff_canary_cache_and_exception_stay_private(
    tmp_path, monkeypatch, eval_registry, capsys, caplog, canaries, _private_root_in_tmp
):
    from tests.test_eval_privacy import _eval_snapshot

    c = canaries
    caplog.set_level(logging.DEBUG)
    sets_c = _canary_sets(tmp_path, eval_registry, c)
    before = _eval_snapshot()
    base = write_arm(tmp_path / "arms", "base", sets_c, BASE_RANKS)
    cand = write_arm(tmp_path / "arms", "cand", sets_c, CAND_RANKS)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "base.json").write_text(json.dumps(_dump(sets_c, BASE_RANKS)), encoding="utf-8")
    (dumps / "cand.json").write_text(json.dumps(_dump(sets_c, CAND_RANKS)), encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        r["question"]: {"rewrites": [c.value("rewrite")], "status": "live", "intent": c.value("intent")}
        for d in sets_c.values() for r in _answerable(d)
    }), encoding="utf-8")
    inputs = [base, cand, sidecar_path(base), sidecar_path(cand), cache, dumps / "base.json",
              dumps / "cand.json", sets_c["tuning"]["path"], sets_c["realistic"]["path"]]
    argv = ["--reports", base, cand, "--baseline", "base",
            "--prod-ranks", str(dumps / "base.json"), str(dumps / "cand.json"),
            "--expansion-cache", str(cache), "--manifest-out", str(tmp_path / "manifest.json")]
    rc, out, err = _cli(argv, capsys)
    assert rc == 0, err
    _assert_canary_clean(c, tmp_path, _private_root_in_tmp, inputs, out, err, caplog.text,
                         where="(bakeoff canaries)")

    # A malformed id-like value in an arm dump must not leak through a C4
    # refusal on a private floor (gate round 2: refusals print the type only).
    bad = _dump(sets_c, CAND_RANKS)
    extra = dict(bad["rows"][0])
    extra["id"] = "q:" + c.value("badid")
    bad["rows"].append(extra)
    (dumps / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    bad_argv = [a if a != str(dumps / "cand.json") else str(dumps / "bad.json") for a in argv]
    rc2, out2, err2 = _cli(bad_argv, capsys)
    assert rc2 == 2, err2
    _assert_canary_clean(c, tmp_path, _private_root_in_tmp, [*inputs, dumps / "bad.json"], out2, err2,
                         caplog.text, where="(bakeoff C4 refusal)")
    inputs.append(dumps / "bad.json")

    def boom(*_a, **_k):
        raise RuntimeError(f"{c.value('rowexc')} {c.value('topexc')}")

    monkeypatch.setattr(bakeoff_report, "compare_prod_ranks", boom)
    rc3, out3, err3 = _cli(argv, capsys)
    assert rc3 == 1 and "RuntimeError" in err3
    _assert_canary_clean(c, tmp_path, _private_root_in_tmp, inputs, out3, err3,
                         caplog.text, where="(bakeoff exception)")
    assert _eval_snapshot() == before


def test_private_bakeoff_legacy_path_prints_ids_only(tmp_path, sets, capsys):
    """A legacy (v5) comparison on a private floor renders ids, not text."""
    base = write_arm(tmp_path, "base", sets, BASE_RANKS, sidecar=False)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS, sidecar=False)
    legacy_dump = {"ranks": {
        "W=0.25|golden|0": {"question": _answerable(sets["tuning"])[0]["question"], "strict_rank": 1}
    }}
    arm_dump = {"ranks": {
        "W=0.25|golden|0": {"question": _answerable(sets["tuning"])[0]["question"], "strict_rank": 9}
    }}
    (tmp_path / "b.json").write_text(json.dumps(legacy_dump), encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps(arm_dump), encoding="utf-8")
    rc, out, err = _cli(
        ["--reports", base, cand, "--baseline", "base", "--legacy",
         "--prod-ranks", str(tmp_path / "b.json"), str(tmp_path / "c.json"),
         "--expansion-cache", str(tmp_path / "b.json")],
        capsys,
    )
    assert rc == 0, err
    _assert_no_canary(out, err)
    assert gid(sets, 0) in out and bakeoff_report.LEGACY_NOTE in err


def test_sealed_w_sweep_input_exits_4_with_zero_work(tmp_path, monkeypatch, sets, capsys):
    calls = {}
    stub_sweep(monkeypatch, tmp_path, sets, calls)
    sealed = _write_jsonl(tmp_path / "sealed" / "golden_set.jsonl",
                          [dict(r, sealed=True) for r in sets["tuning"]["rows"]])
    monkeypatch.setattr(w_sweep, "SET_PATHS", (("golden", str(sealed)),))
    monkeypatch.setattr(w_sweep, "load_set_files", _explode)
    assert w_sweep.main(["--ranks-out", str(tmp_path / "r.json"), "--legacy-public"]) == 4
    captured = capsys.readouterr()
    assert captured.out == "" and "sealed" in captured.err
    assert calls == {} and not (tmp_path / "r.json").exists()
    with pytest.raises(SealedInputError):
        w_sweep.run_sweep(persist_dir="x", ranks_out=None, privacy="private")


def test_sealed_bakeoff_input_exits_4_with_zero_work(
    tmp_path, monkeypatch, sets, capsys, _private_root_in_tmp
):
    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path, "cand", sets, CAND_RANKS)
    monkeypatch.setattr(bakeoff_report, "load_arm", _explode)
    sealed_dump = tmp_path / "sealed.json"
    sealed_dump.write_text(json.dumps({"sealed": True, "ranks": {}}), encoding="utf-8")
    rc, out, err = _cli(
        ["--reports", base, cand, "--baseline", "base",
         "--prod-ranks", str(sealed_dump), str(sealed_dump), "--legacy-public"],
        capsys,
    )
    assert rc == 4 and out == "" and "sealed" in err
    sealed_report = _private_root_in_tmp / "sealed" / "base.md"
    sealed_report.parent.mkdir(parents=True)
    sealed_report.write_text(Path(base).read_text(encoding="utf-8"), encoding="utf-8")
    rc, out, err = _cli(["--reports", str(sealed_report), "--baseline", "base"], capsys)
    assert rc == 4 and out == ""


def test_weaker_privacy_raises_before_any_work(tmp_path, monkeypatch, sets):
    calls = {}
    stub_sweep(monkeypatch, tmp_path, sets, calls)
    monkeypatch.setattr(w_sweep, "load_set_files", _explode)
    with pytest.raises(PrivacyFloorError):
        w_sweep.run_sweep(persist_dir="x", ranks_out=None, privacy="public")  # cache: private
    assert calls == {}

    base = write_arm(tmp_path, "base", sets, BASE_RANKS)
    cache = tmp_path / "cache.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bakeoff_report, "load_arm", _explode)
    with pytest.raises(PrivacyFloorError):
        bakeoff_report.run([base], "base", privacy="public", expansion_cache=str(cache))


def test_stronger_privacy_is_honoured(tmp_path, monkeypatch, sets, capsys, _private_root_in_tmp):
    calls = {}
    stub_sweep(monkeypatch, tmp_path, sets, calls)
    requested = tmp_path / "ranks.json"
    assert w_sweep.run_sweep(
        persist_dir="x", ranks_out=str(requested), privacy="private", legacy_public=True
    ) == 0
    capsys.readouterr()
    assert not requested.exists()
    assert list((_private_root_in_tmp / "runs").glob("*/ranks.json"))


def test_entry_functions_take_privacy_keyword_only_without_default():
    import inspect

    for fn in (w_sweep.run_sweep, bakeoff_report.run):
        param = inspect.signature(fn).parameters["privacy"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


def test_scorer_version_is_the_sidecar_one(sets):
    assert build_c4(sets, BASE_RANKS)["scorer_version"] == SCORER_VERSION


# --- gate finding: w_sweep meter (D70) --------------------------------------------
def test_w_sweep_live_fill_needs_a_meter_with_a_key(monkeypatch, tmp_path):
    from scripts import w_sweep
    from src.spend import SpendMeterRequired

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    monkeypatch.setattr(w_sweep, "CACHE", str(tmp_path / "absent.json"))
    called = []
    monkeypatch.setattr(w_sweep, "expand_query", lambda q, **k: called.append(q))
    with pytest.raises(SpendMeterRequired):
        w_sweep.build_cache({"golden": [{"question": "synthetic q"}]}, offline_only=False, privacy="public")
    assert called == []


def test_w_sweep_live_fill_uses_the_meter_rewrite_client(monkeypatch, tmp_path):
    from scripts import w_sweep
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    monkeypatch.setattr(w_sweep, "CACHE", str(tmp_path / "c.json"))
    seen = []

    def expand(q, **k):
        seen.append(k.get("llm"))
        return Expansion(q, ("r",), REWRITE_MODEL, STATUS_LIVE, None)

    class Meter:
        def rewrite_llm(self):
            return "metered-rewrite-client"

    monkeypatch.setattr(w_sweep, "expand_query", expand)
    w_sweep.build_cache({"golden": [{"question": "synthetic q"}]}, offline_only=False, meter=Meter(), privacy="public")
    assert seen == ["metered-rewrite-client"]



def test_w_sweep_live_fill_refuses_non_public_sets(monkeypatch, tmp_path):
    """Gate round 4: a live fill writes the tracked public cache, so private sets never fill it."""
    from scripts import w_sweep
    from src.eval_privacy import PrivacyFloorError

    monkeypatch.setattr(w_sweep, "CACHE", str(tmp_path / "c.json"))
    called = []
    monkeypatch.setattr(w_sweep, "expand_query", lambda q, **k: called.append(q))
    with pytest.raises(PrivacyFloorError):
        w_sweep.build_cache({"golden": [{"question": "private q"}]}, offline_only=False, privacy="private")
    assert called == [] and not (tmp_path / "c.json").exists()


# --- gate round 5 (PT3): inputs.json is written before any other run file -----
def _order_spy(monkeypatch, module):
    seen = []
    real = module.write_private

    def spy(target, content, *a, **k):
        seen.append((Path(target).name, (Path(target).parent / "inputs.json").is_file()))
        return real(target, content, *a, **k)

    monkeypatch.setattr(module, "write_private", spy)
    return seen


def test_private_w_sweep_writes_inputs_json_before_the_dump(tmp_path, monkeypatch, sets, _private_root_in_tmp):
    stub_sweep(monkeypatch, tmp_path, sets, {})
    seen = _order_spy(monkeypatch, w_sweep)
    assert w_sweep.main(["--ranks-out", str(tmp_path / "ranks.json")]) == 0
    assert ("ranks.json", True) in seen and all(ok for _, ok in seen)


def test_private_bakeoff_writes_inputs_json_before_the_manifest(tmp_path, monkeypatch, sets, capsys,
                                                                 _private_root_in_tmp):
    base = write_arm(tmp_path / "arms", "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path / "arms", "cand", sets, CAND_RANKS)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "base.json").write_text(json.dumps(_dump(sets, BASE_RANKS)), encoding="utf-8")
    (dumps / "cand.json").write_text(json.dumps(_dump(sets, CAND_RANKS)), encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text("{}", encoding="utf-8")
    seen = _order_spy(monkeypatch, bakeoff_report)
    rc, _, err = _cli(["--reports", base, cand, "--baseline", "base",
                       "--prod-ranks", str(dumps / "base.json"), str(dumps / "cand.json"),
                       "--expansion-cache", str(cache), "--manifest-out", str(tmp_path / "m.json")], capsys)
    assert rc == 0, err
    assert ("m.json", True) in seen and all(ok for _, ok in seen)


def test_private_bakeoff_run_passes_the_merge_gate_precheck(tmp_path, monkeypatch, sets, capsys,
                                                            _private_root_in_tmp):
    """Round 5 PT1 end to end: a real private --prod-ranks run over registered
    public sets, with the (legacy-listed) expansion cache, passes the precheck."""
    from scripts import scan_leaks

    base = write_arm(tmp_path / "arms", "base", sets, BASE_RANKS)
    cand = write_arm(tmp_path / "arms", "cand", sets, CAND_RANKS)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "base.json").write_text(json.dumps(_dump(sets, BASE_RANKS)), encoding="utf-8")
    (dumps / "cand.json").write_text(json.dumps(_dump(sets, CAND_RANKS)), encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text("{}", encoding="utf-8")
    rc, _, err = _cli(["--reports", base, cand, "--baseline", "base",
                       "--prod-ranks", str(dumps / "base.json"), str(dumps / "cand.json"),
                       "--expansion-cache", str(cache), "--manifest-out", str(tmp_path / "m.json")], capsys)
    assert rc == 0, err
    assert list((_private_root_in_tmp / "runs").glob("*/inputs.json"))
    monkeypatch.setattr(scan_leaks, "_legacy_shas", lambda: {eval_sets.sha256_file(cache)})
    assert scan_leaks.precheck(set()) == []


# ---------------------------------------------------------------------------
# 16A-1 merge gate, Codex #5: ACTUAL report v6 output (run_eval_matrix's v6
# path on synthetic schema-2 sets, fake retrieval) and its rows sidecar go
# through the C4 report comparison end to end.
# ---------------------------------------------------------------------------
V6_FILLER = ["7.1", "7.2", "7.3", "7.4", "7.5", "7.6"]


@pytest.fixture
def v2_sets(tmp_path, eval_registry, monkeypatch):
    """Registered public schema-2 golden (3 answer + 1 refuse) and realistic (2) sets."""
    import src.eval_schema as eval_schema
    import src.evaluator as ev
    from tests.p16_capture import _fake_expand

    golden = [{"schema": 2, "id": f"gold-{i}", "family_id": f"gfam-{i}", "question": _q("v2golden", i),
               "scope": "answer", "evidence": [[f"9.{i + 1}"]]} for i in range(3)]
    golden.append({"schema": 2, "id": "gold-r0", "family_id": "gfam-r0", "question": _q("v2refuse", 0),
                   "scope": "refuse", "evidence": []})
    realistic = [{"schema": 2, "id": f"real-{i}", "family_id": f"rfam-{i}", "question": _q("v2realistic", i),
                  "scope": "answer", "evidence": [[f"8.{i + 1}"]]} for i in range(2)]
    out = {}
    for label, rows, name in (("tuning", golden, "golden_set.jsonl"), ("realistic", realistic, "realistic_set.jsonl")):
        path = _write_jsonl(tmp_path / "v2" / name, rows)
        eval_registry.add(path)
        out[label] = {"path": str(path), "rows": rows, "sha256": eval_sets.sha256_file(path)}
    inv = tmp_path / "inventory.json"
    inv.write_text(json.dumps({"version": 1, "map_sha256": "0" * 64,
                               "sections": sorted({"8.1", "8.2", "9.1", "9.2", "9.3", *V6_FILLER}), "aliases": []}))
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
    monkeypatch.setattr(ev, "_load_absorbed_map", lambda: (None, None))
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    return out


def _v6_arm(directory, name, v2, ranks):
    """Run the real v6 path; ``ranks``: {row id: strict rank of its section, or None}."""
    from langchain_core.documents import Document

    import src.evaluator as ev
    from tests.p16_capture import PROVENANCE

    by_q = {r["question"]: r for data in v2.values() for r in data["rows"]}

    def factory(mode):
        def retrieve(question, top_k=6):
            row = by_q[question]
            target = row["evidence"][0][0] if row["evidence"] else None
            rank = ranks.get(row["id"])
            secs = [target if (rank is not None and i + 1 == rank) else V6_FILLER[i] for i in range(top_k)]
            return [{"document": Document(page_content=f"Synthetic {s}.", id=f"syn-{s}",
                                          metadata={"section_number": s, "source": "synthetic.pdf"}),
                     "score": 1.0, "metadata": {"section_number": s}} for s in secs]
        return retrieve

    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{name}.md"
    ev.run_eval_matrix([("tuning", v2["tuning"]["path"]), ("realistic", v2["realistic"]["path"])],
                       skip_refusals=True, skip_completeness=True, retrieve_fn_factory=factory,
                       provenance_fn=lambda: dict(PROVENANCE), privacy="public", results_path=str(report))
    assert Path(sidecar_path(str(report))).is_file()
    return str(report)


V6_BASE = {"gold-0": 1, "gold-1": None, "gold-2": 4, "real-0": 2, "real-1": None}
V6_CAND = {"gold-0": None, "gold-1": 1, "gold-2": 2, "real-0": 2, "real-1": None}


def test_actual_v6_reports_and_sidecars_compare_under_c4(tmp_path, v2_sets, capsys):
    base = _v6_arm(tmp_path / "arms", "base", v2_sets, V6_BASE)
    cand = _v6_arm(tmp_path / "arms", "cand", v2_sets, V6_CAND)
    capsys.readouterr()
    assert Path(base).read_text().startswith(bakeoff_report.V6_TITLE_PREFIX)
    assert eval_sets.classify(base) == "public"  # rule 5 reads the v6 cohort block

    result = bakeoff_report.compare(load_arms(base, cand), "base")
    assert result["c4"] is True
    assert result["flips"]["cand"]["hit_to_miss"] == ["gold-0"]
    assert result["flips"]["cand"]["miss_to_hit"] == ["gold-1"]
    base_row = next(r for r in result["table"] if r["arm"] == "base")
    assert base_row["golden"] == {"strict_at_6": 2 / 3, "related_at_6": 2 / 3, "n": 3}
    assert base_row["realistic"]["n"] == 2

    rc, out, err = _cli(["--reports", base, cand, "--baseline", "base"], capsys)
    assert rc == 0, err
    assert "C4 cohort identity checked" in out
    assert "- gold-0\n" in out and "- gold-1\n" in out  # ids, never "id — id"
    _assert_no_canary(out, err)
    # a v6 row records no retrieved sections: role coverage is n/a, never "no"
    cover = bakeoff_report.role_coverage(load_arms(cand)["cand"], roles=(RoleSpec("S5", "real-0", ("8.1", "8.2")),))
    assert cover["S5"]["found"] is True and cover["S5"]["available"] is False and cover["S5"]["both"] is False


def test_v6_report_without_its_sidecar_is_refused(tmp_path, v2_sets, capsys):
    base = _v6_arm(tmp_path / "arms", "base", v2_sets, V6_BASE)
    cand = _v6_arm(tmp_path / "arms", "cand", v2_sets, V6_CAND)
    os.remove(sidecar_path(cand))
    capsys.readouterr()
    for extra in ([], ["--legacy"]):
        rc, out, err = _cli(["--reports", base, cand, "--baseline", "base", *extra], capsys)
        assert rc == 2 and out == "" and "rows sidecar" in err


def test_v6_arm_with_a_symmetric_roster_gap_is_refused(tmp_path, v2_sets):
    """#4 and #5 together: the same eligible v2 row dropped from both actual v6 sidecars."""
    arms = []
    for name, ranks in (("base", V6_BASE), ("cand", V6_CAND)):
        report = _v6_arm(tmp_path / "arms", name, v2_sets, ranks)
        side = Path(sidecar_path(report))
        doc = json.loads(side.read_text())
        doc["rows"] = [r for r in doc["rows"] if r["id"] != "gold-0"]
        side.write_text(dump_sidecar(doc))
        arms.append(report)
    with pytest.raises(C4Error, match="roster mismatch"):
        bakeoff_report.compare(load_arms(*arms), "base")


def test_v6_report_naming_an_unregistered_set_stays_private(tmp_path, v2_sets):
    base = _v6_arm(tmp_path / "arms", "base", v2_sets, V6_BASE)
    text = Path(base).read_text().replace(v2_sets["realistic"]["sha256"], "e" * 64)
    forged = tmp_path / "other" / "forged.md"
    forged.parent.mkdir()
    forged.write_text(text)
    assert eval_sets.classify(base) == "public"
    assert eval_sets.classify(str(forged)) == "private"
