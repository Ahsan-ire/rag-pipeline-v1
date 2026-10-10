"""Tests for the Phase 15 bake-off instruments.

Covers the three committed instruments whose output is selection evidence:

- ``scripts/w_sweep.py`` — the offline-only cache guard (it must RAISE, never
  call the API) and the machine-readable per-question rank dump.
- ``scripts/bakeoff_report.py`` — report parsing, flip lists, per-class
  movement, both-role coverage, and the held-out refusal.
- ``scripts/embed_latency.py`` — the timing core, with an injected embedder
  and a fake clock.

No test loads a model, opens an index, or makes a network/API call: every
report is a synthetic fixture built here, and every retrieval/embedding seam is
stubbed. The fixture reproduces the *structure* of a real offline report
(``eval/results_partial.md``) with invented questions and section numbers — no
corpus or eval-set text is copied into this file.
"""
import json
import os

import re

import pytest

from scripts import bakeoff_report, embed_latency, w_sweep
from src import eval_sets
from src.eval_privacy import public_v1_id
from src.eval_roster import RoleSpec

# ---------------------------------------------------------------------------
# Synthetic report fixture (structure of eval/results_partial.md, invented data)
# ---------------------------------------------------------------------------
# Phase 16A-1 (D65): a report classifies public only when every set sha256 it
# records is registered public (classify rule 5), so the fixture records the
# registered hashes of the committed golden/realistic sets. Only the hashes
# are taken from the registry; no eval-set text enters this file.
_REGISTERED = {e.path: e.sha256 for e in eval_sets.load_registry()}
GOLDEN_SHA = _REGISTERED["eval/golden_set.jsonl"]
REALISTIC_SHA = _REGISTERED["eval/realistic_set.jsonl"]

EXPANSION_OFF = "- query expansion: disabled (offline run)"
EXPANSION_ON = "- query expansion: claude-haiku — attempted 20, live 20, fallbacks 0"

# Ablation rows: mode -> (S@1, S@3, S@6, R@1, R@3, R@6, mrr_strict, mrr_related, n)
BASELINE_GOLDEN_ABLATION = {
    "hybrid": (0.333, 0.333, 0.667, 0.333, 0.667, 0.667, 0.333, 0.400, 3),
    "vector": (0.333, 0.333, 0.333, 0.333, 0.333, 0.333, 0.333, 0.333, 3),
    "bm25": (0.000, 0.333, 0.667, 0.000, 0.333, 0.667, 0.200, 0.200, 3),
    "hybrid+rewrite": (0.333, 0.333, 0.667, 0.333, 0.667, 0.667, 0.333, 0.400, 3),
}
BASELINE_REALISTIC_ABLATION = {
    "hybrid": (0.000, 0.333, 0.333, 0.000, 0.333, 0.667, 0.111, 0.222, 3),
    "vector": (0.000, 0.000, 0.333, 0.000, 0.333, 0.333, 0.100, 0.150, 3),
    "bm25": (0.000, 0.000, 0.000, 0.000, 0.000, 0.333, 0.000, 0.050, 3),
    "hybrid+rewrite": (0.000, 0.333, 0.333, 0.000, 0.333, 0.667, 0.111, 0.222, 3),
}

# Invented questions. G* are "golden" rows, R* "realistic" rows.
G1 = "Fixture golden question one about widgets?"
G2 = "Fixture golden question two about sprockets?"
G3 = "Fixture golden question three about flanges?"
R1 = "Fixture realistic question about a widget dispute?"
R2 = "Fixture realistic question about sprocket ownership?"
R3 = "Fixture realistic question about flange registration?"


def _detail_row(qtype, strict_rank, related_rank, expected, retrieved, question):
    """Render one per-question detail line exactly as src/evaluator.py does."""
    strict = "HIT" if strict_rank is not None else "MISS"
    related = "HIT" if related_rank is not None else "MISS"
    return (
        f"- [{qtype}] strict={strict}(rank={strict_rank}) "
        f"related={related}(rank={related_rank}) "
        f"expected={expected!r} retrieved={retrieved!r} :: {question}"
    )


def _ablation_block(label, rows):
    """Render one ``## <label> — retrieval ablation`` table."""
    lines = [
        f"## {label} — retrieval ablation",
        "",
        "| Mode | S@1 | S@3 | S@6 | R@1 | R@3 | R@6 | MRR@6 strict | MRR@6 related | n |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mode, vals in rows.items():
        cells = " | ".join(f"{v:.3f}" for v in vals[:8])
        lines.append(f"| {mode} | {cells} | {vals[8]} |")
    # The evaluator emits a second, differently-shaped table in this same
    # section; the parser must stop at the mode table rather than eat it.
    lines += [
        "",
        "By type (hybrid), strict / related hit rate:",
        "",
        "| Type | Strict rate | Related rate | n |",
        "| --- | --- | --- | --- |",
        "| direct | 0.667 | 0.667 | 3 |",
        "",
    ]
    return lines


def build_report(
    *,
    model="fixture/model-a",
    expansion_line=EXPANSION_OFF,
    detail_mode="hybrid+rewrite",
    golden_ablation=None,
    realistic_ablation=None,
    golden_rows=None,
    realistic_rows=None,
):
    """Build a synthetic arm report with the real report's structure.

    Args:
        model: value of the provenance ``embedding model`` line.
        expansion_line: the expansion disclosure line (offline vs live).
        detail_mode: mode named in the per-question detail headings.
        golden_ablation/realistic_ablation: ``{mode: 9-tuple}`` metric rows.
        golden_rows/realistic_rows: pre-rendered per-question detail lines.

    Returns:
        The report text.
    """
    golden_ablation = golden_ablation or BASELINE_GOLDEN_ABLATION
    realistic_ablation = realistic_ablation or BASELINE_REALISTIC_ABLATION
    lines = [
        "# Legal RAG Evaluation Report v4 (fixture)",
        "",
        "- Date: 2026-08-05T00:00:00.000000",
        "- top_k: 6",
        "- Retrieval modes ablated: hybrid, vector, bm25, hybrid+rewrite",
        "- Canonical run (writes the committed report): False",
        "",
        "## Provenance",
        "",
        "- git sha: abc1234 (clean)",
        "- indexed chunk count: 1470",
        f"- embedding model: {model}",
        "- generation model: claude-sonnet-5",
        "- matching: strict = exact section-number equality; related = dotted-nesting",
        "- MRR@6: truncated mean reciprocal rank — no match in the top 6 scores 0.",
        "- passes: retrieval ablation, refusals SKIPPED, completeness SKIPPED, judge off",
        "- answer passes use hybrid (expansion disabled — offline run).",
        expansion_line,
        "- BM25 sidecar loaded: True",
        "",
        "Question sets:",
        "- tuning: tuning (used to select fusion constants, D31 — NOT held-out)",
        "  - path: eval/golden_set.jsonl",
        f"  - sha256: {GOLDEN_SHA}",
        "  - question counts: direct=3 (n=3)",
        "- realistic: realistic (messy staff phrasing — dev/regression slice)",
        "  - path: eval/realistic_set.jsonl",
        f"  - sha256: {REALISTIC_SHA}",
        "  - question counts: direct=3 (n=3)",
        "",
        "## Headline: strict hit@6 on the held-out set (hybrid)",
        "",
        "**strict hit@6 = 2/3 = 0.667**, set: tuning (NO held-out set present).",
        "",
    ]
    lines += _ablation_block("tuning", golden_ablation)
    lines += _ablation_block("realistic", realistic_ablation)
    lines += [f"## tuning — per-question detail ({detail_mode})", ""]
    lines += golden_rows if golden_rows is not None else [
        _detail_row("direct", 1, 1, ["9.1"], ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6"], G1),
        _detail_row("direct", None, 2, ["9.7"], ["9.8", "9.7.1", "9.3", "9.4", "9.5", "9.6"], G2),
        _detail_row("exact_token", 4, 4, ["9.9"], ["9.1", "9.2", "9.3", "9.9", "9.5", "9.6"], G3),
    ]
    lines += ["", f"## realistic — per-question detail ({detail_mode})", ""]
    lines += realistic_rows if realistic_rows is not None else [
        _detail_row("direct", 2, 2, ["8.1"], ["8.4", "8.1", "8.3", "8.5", "8.6", "8.7"], R1),
        _detail_row("direct", None, None, ["8.2"], ["8.4", "8.5", "8.6", "8.7", "8.8", "8.9"], R2),
        _detail_row("direct", None, 3, ["8.3"], ["8.4", "8.5", "8.3.1", "8.7", "8.8", "8.9"], R3),
    ]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# scripts/bakeoff_report.py — parsing
# ---------------------------------------------------------------------------
def test_parse_report_reads_provenance_ablation_and_questions():
    """The parser recovers model, set paths/shas, ablation rows and rank rows."""
    parsed = bakeoff_report.parse_report(build_report())

    assert parsed["embedding_model"] == "fixture/model-a"
    assert parsed["expansion_disabled"] is True
    assert set(parsed["sets"]) == {"tuning", "realistic"}

    golden = parsed["sets"]["tuning"]
    assert golden["path"] == "eval/golden_set.jsonl"
    assert golden["sha256"] == GOLDEN_SHA
    assert golden["ablation"]["hybrid"]["strict"][6] == 0.667
    assert golden["ablation"]["hybrid"]["related"][6] == 0.667
    assert golden["ablation"]["hybrid"]["n"] == 3
    assert golden["ablation"]["bm25"]["strict"][1] == 0.0
    assert set(golden["ablation"]) == {"hybrid", "vector", "bm25", "hybrid+rewrite"}

    rows = golden["questions"]
    assert [r["question"] for r in rows] == [G1, G2, G3]
    assert rows[0]["type"] == "direct"
    assert rows[0]["expected"] == ["9.1"]
    assert rows[0]["retrieved"] == ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6"]
    assert rows[0]["strict_rank"] == 1
    assert rows[1]["strict_rank"] is None
    assert rows[1]["related_rank"] == 2
    assert rows[2]["type"] == "exact_token"


def test_parse_report_finds_sets_by_path_not_label():
    """``golden_set.jsonl`` is labelled ``tuning`` in reports — resolve by path."""
    parsed = bakeoff_report.parse_report(build_report())
    assert bakeoff_report.set_of_kind(parsed, "golden")["path"] == "eval/golden_set.jsonl"
    assert (
        bakeoff_report.set_of_kind(parsed, "realistic")["path"]
        == "eval/realistic_set.jsonl"
    )


def test_parse_report_rejects_a_live_expansion_report():
    """Arms whose expansion state differs are not comparable — refuse them."""
    with pytest.raises(ValueError, match="disabled \\(offline run\\)"):
        bakeoff_report.parse_report(build_report(expansion_line=EXPANSION_ON))


def test_parse_report_rejects_a_detail_section_from_another_mode():
    """The offline detail section is ``hybrid+rewrite``; anything else is a
    differently-configured run, not a comparable arm (round-3 correction 3)."""
    with pytest.raises(ValueError, match="hybrid\\+rewrite"):
        bakeoff_report.parse_report(build_report(detail_mode="hybrid"))


# ---------------------------------------------------------------------------
# scripts/bakeoff_report.py — comparison
# ---------------------------------------------------------------------------
def _candidate_report():
    """A second arm: G1 regresses, G2 recovers, G3 unchanged."""
    return build_report(
        model="fixture/model-b",
        golden_rows=[
            _detail_row("direct", None, None, ["9.1"], ["9.2", "9.3", "9.4", "9.5", "9.6", "9.0"], G1),
            _detail_row("direct", 1, 1, ["9.7"], ["9.7", "9.3", "9.4", "9.5", "9.6", "9.0"], G2),
            _detail_row("exact_token", 2, 2, ["9.9"], ["9.1", "9.9", "9.3", "9.4", "9.5", "9.6"], G3),
        ],
    )


def test_compare_lists_both_flip_directions_with_full_question_text():
    """HIT->MISS and MISS->HIT are computed against the named baseline arm."""
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(build_report()),
        "candidate": bakeoff_report.parse_report(_candidate_report()),
    }
    result = bakeoff_report.compare(arms, "baseline-fixture", legacy=True)

    assert result["baseline"] == "baseline-fixture"
    assert result["flips"]["candidate"]["hit_to_miss"] == [G1]
    assert result["flips"]["candidate"]["miss_to_hit"] == [G2]
    assert result["flips"]["candidate"]["unmatched"] == []
    assert "baseline-fixture" not in result["flips"]  # never compared to itself

    table = {row["arm"]: row for row in result["table"]}
    assert table["baseline-fixture"]["golden"]["strict_at_6"] == 0.667
    assert table["baseline-fixture"]["realistic"]["related_at_6"] == 0.667
    assert table["candidate"]["embedding_model"] == "fixture/model-b"


def test_compare_flags_questions_present_in_only_one_arm():
    """A question in one arm but not the other means the sets differ — say so."""
    trimmed = build_report(
        # n=1 so the new detail-count check (C3) passes: this test is about
        # differing question sets, not a truncated report.
        golden_ablation={
            mode: vals[:8] + (1,) for mode, vals in BASELINE_GOLDEN_ABLATION.items()
        },
        golden_rows=[
            _detail_row("direct", 1, 1, ["9.1"], ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6"], G1),
        ]
    )
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(build_report()),
        "candidate": bakeoff_report.parse_report(trimmed),
    }
    result = bakeoff_report.compare(arms, "baseline-fixture", legacy=True)
    assert sorted(result["flips"]["candidate"]["unmatched"]) == sorted([G2, G3])


def test_compare_raises_when_the_baseline_arm_is_absent():
    """A typo'd baseline name must fail, not silently compare nothing."""
    arms = {"candidate": bakeoff_report.parse_report(build_report())}
    with pytest.raises(KeyError):
        bakeoff_report.compare(arms, "baseline-fixture", legacy=True)


# ---------------------------------------------------------------------------
# scripts/bakeoff_report.py — per-class movement
# ---------------------------------------------------------------------------
GONE = "Fixture question that no longer exists"
# Item 9: the roster is keyed by row id (public v1 id of the fixture question).
FIXTURE_ROSTER = (
    bakeoff_report.RosterEntry("vocabulary gap", public_v1_id(R1), ("8.1",)),
    bakeoff_report.RosterEntry("vocabulary gap", public_v1_id(R2), ("8.2",)),
    bakeoff_report.RosterEntry("near-miss", public_v1_id(R3), ("8.3",)),
    bakeoff_report.RosterEntry("near-miss", public_v1_id(GONE), ("8.9",)),
)


def test_class_movement_matches_roster_by_id_and_counts_related_hits():
    """Roster entries match on row id; a related-only HIT@6 counts."""
    arm = bakeoff_report.parse_report(build_report())
    movement = bakeoff_report.class_movement(arm, roster=FIXTURE_ROSTER)

    by_class = movement["by_class"]
    assert by_class["vocabulary gap"]["n"] == 2
    assert by_class["vocabulary gap"]["hits"] == [R1]  # strict rank 2
    assert by_class["vocabulary gap"]["misses"] == [R2]  # no rank at all
    assert by_class["near-miss"]["hits"] == [R3]  # related rank 3 only
    assert movement["unmatched"] == [public_v1_id(GONE)]

    entries = {e["row_id"]: e for e in movement["entries"]}
    assert entries[public_v1_id(R1)]["question"] == R1
    assert entries[public_v1_id(R3)]["hit_at_6"] is True
    assert entries[public_v1_id(GONE)]["question"] is None


def test_parse_report_adds_a_row_id_per_detail_row():
    """Public rows get public_v1_id(question); a shown opaque id is kept."""
    parsed = bakeoff_report.parse_report(build_report())
    assert [q["id"] for q in parsed["sets"]["tuning"]["questions"]] == [
        public_v1_id(G1), public_v1_id(G2), public_v1_id(G3)
    ]
    opaque = "q:0123456789ab"
    private_text = build_report().replace(f":: {G1}", f":: {opaque}")
    rows = bakeoff_report.parse_report(private_text)["sets"]["tuning"]["questions"]
    assert rows[0]["id"] == opaque


def test_class_movement_default_roster_is_the_briefs_table():
    """The shipped roster is the brief's 3 vocabulary-gap + 5 near-miss rows."""
    counts = {}
    for entry in bakeoff_report.ROSTER:
        counts[entry.failure_class] = counts.get(entry.failure_class, 0) + 1
    assert counts == {"vocabulary gap": 3, "near-miss": 5}


# ---------------------------------------------------------------------------
# scripts/bakeoff_report.py — both-role coverage
# ---------------------------------------------------------------------------
def _role_report(retrieved_for_role):
    """A report whose one realistic row retrieved ``retrieved_for_role``."""
    return build_report(
        realistic_rows=[
            _detail_row("direct", None, 1, ["2.2.1", "2.2.2", "2.9"], retrieved_for_role, R1)
        ]
    )


FIXTURE_ROLES = (bakeoff_report.RoleSpec("S5", public_v1_id(R1), ("2.2.1", "2.2.2")),)


def test_role_coverage_true_only_when_each_group_is_covered_separately():
    """Both roles retrieved -> both true."""
    arm = bakeoff_report.parse_report(
        _role_report(["2.2.1", "2.2.2", "7.1", "7.2", "7.3", "7.4"])
    )
    cover = bakeoff_report.role_coverage(arm, roles=FIXTURE_ROLES)["S5"]
    assert cover["found"] is True
    assert cover["groups"] == {"2.2.1": True, "2.2.2": True}
    assert cover["both"] is True


def test_role_coverage_generic_parent_satisfies_neither_group():
    """One retrieved 2.2 must NOT tick 2.2.1 and 2.2.2 (Codex C3)."""
    arm = bakeoff_report.parse_report(
        _role_report(["2.2", "7.1", "7.2", "7.3", "7.4", "7.5"])
    )
    cover = bakeoff_report.role_coverage(arm, roles=FIXTURE_ROLES)["S5"]
    assert cover["groups"] == {"2.2.1": False, "2.2.2": False}
    assert cover["both"] is False


def test_role_coverage_descendant_covers_its_group():
    """2.2.1.5 covers 2.2.1 (equal-or-descendant) but never 2.2.2."""
    arm = bakeoff_report.parse_report(
        _role_report(["2.2.1.5", "7.1", "7.2", "7.3", "7.4", "7.5"])
    )
    cover = bakeoff_report.role_coverage(arm, roles=FIXTURE_ROLES)["S5"]
    assert cover["groups"] == {"2.2.1": True, "2.2.2": False}
    assert cover["both"] is False


def test_role_coverage_reports_a_missing_question_rather_than_passing_it():
    """If the role question is absent from the arm, coverage is not 'both'."""
    arm = bakeoff_report.parse_report(build_report(realistic_rows=[]))
    cover = bakeoff_report.role_coverage(arm, roles=FIXTURE_ROLES)["S5"]
    assert cover["found"] is False
    assert cover["both"] is False


def test_shipped_role_specs_cover_2_2_1_and_2_2_2():
    """S5 and N4 are checked for exactly the two role groups (2.9 excluded)."""
    assert [r.name for r in bakeoff_report.ROLE_QUESTIONS] == ["S5", "N4"]
    for role in bakeoff_report.ROLE_QUESTIONS:
        assert role.groups == ("2.2.1", "2.2.2")


# ---------------------------------------------------------------------------
# scripts/bakeoff_report.py — CLI
# ---------------------------------------------------------------------------
def _write_arms(tmp_path):
    """Write a baseline and a candidate report; return their paths."""
    baseline = tmp_path / "baseline-fixture.md"
    candidate = tmp_path / "candidate.md"
    baseline.write_text(build_report(), encoding="utf-8")
    candidate.write_text(_candidate_report(), encoding="utf-8")
    return baseline, candidate


@pytest.mark.parametrize(
    "argv",
    [
        ["--reports", "eval/heldout_set.jsonl", "--baseline", "x"],
        ["--reports", "eval/bakeoff/a.md", "--baseline", "heldout"],
        ["--reports", "eval/bakeoff/HELDOUT-arm.md", "--baseline", "x"],
    ],
)
def test_cli_refuses_any_heldout_argument_with_exit_code_2(argv, capsys):
    """Held-out exclusion is a property of the artifact, not of discipline."""
    with pytest.raises(SystemExit) as exc:
        bakeoff_report.main(argv)
    assert exc.value.code == 2
    assert "held-out" in capsys.readouterr().err


def test_cli_refuses_a_baseline_name_that_is_not_a_report(tmp_path, capsys):
    """A baseline naming no supplied report exits 2 rather than comparing."""
    baseline, _ = _write_arms(tmp_path)
    with pytest.raises(SystemExit) as exc:
        bakeoff_report.main(["--reports", str(baseline), "--baseline", "typo"])
    assert exc.value.code == 2
    assert "not among" in capsys.readouterr().err


def test_cli_emits_all_four_sections_and_a_manifest(tmp_path, capsys):
    """End-to-end: the four evidence sections print; the manifest records shas."""
    baseline, candidate = _write_arms(tmp_path)
    manifest_path = tmp_path / "manifest.json"

    rc = bakeoff_report.main(
        [
            "--reports",
            str(baseline),
            str(candidate),
            "--baseline",
            "baseline-fixture",
            "--manifest-out",
            str(manifest_path),
            "--legacy",  # v5 reports carry no .rows.json sidecar (16A-1 C4)
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out
    assert bakeoff_report.LEGACY_NOTE in captured.err

    assert "## 1. Selection table" in out
    assert "## 2. Golden per-question strict flips" in out
    assert "## 3. Per-class movement" in out
    assert "## 4. S5 / N4 both-role coverage" in out
    assert "fixture/model-b" in out
    assert G1 in out  # the HIT->MISS flip, full question text
    assert G2 in out  # the MISS->HIT gain

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["baseline"] == "baseline-fixture"
    assert set(manifest["arms"]) == {"baseline-fixture", "candidate"}
    arm = manifest["arms"]["candidate"]
    assert arm["report_path"] == str(candidate)
    assert arm["report_sha256"] == bakeoff_report.sha256_file(str(candidate))
    assert arm["embedding_model"] == "fixture/model-b"
    assert {s["path"] for s in arm["eval_sets"]} == {
        "eval/golden_set.jsonl",
        "eval/realistic_set.jsonl",
    }
    assert {s["sha256"] for s in arm["eval_sets"]} == {GOLDEN_SHA, REALISTIC_SHA}
    assert manifest["legacy"] is True and manifest["c4"] is False


# ---------------------------------------------------------------------------
# scripts/w_sweep.py — offline-only cache guard
# ---------------------------------------------------------------------------
def _explode(*args, **kwargs):
    """Stand-in for expand_query that fails the test if it is ever called."""
    raise AssertionError("expand_query called — the sweep spent an API call")


def _sets(questions):
    """Minimal ``{label: rows}`` shaped like load_sets() output."""
    return {
        "golden": [
            {"question": q, "expected_sections": ["1.1"], "type": "direct"}
            for q in questions
        ]
    }


def test_build_cache_offline_only_raises_on_a_missing_entry(tmp_path, monkeypatch):
    """No cache entry -> RuntimeError naming the question, no API call."""
    monkeypatch.setattr(w_sweep, "CACHE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(w_sweep, "expand_query", _explode)

    question = "A fixture question that was edited after the cache was built?"
    with pytest.raises(RuntimeError) as exc:
        w_sweep.build_cache(_sets([question]), offline_only=True)
    assert question[:60] in str(exc.value)
    assert "absent" in str(exc.value)


def test_build_cache_offline_only_raises_on_a_non_live_entry(tmp_path, monkeypatch):
    """A cached fallback is not a live expansion — refuse it too."""
    cache_path = tmp_path / "cache.json"
    question = "A fixture question whose cached expansion fell back?"
    cache_path.write_text(
        json.dumps({question: {"rewrites": [], "status": "fallback", "intent": None}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(w_sweep, "CACHE", str(cache_path))
    monkeypatch.setattr(w_sweep, "expand_query", _explode)

    with pytest.raises(RuntimeError, match="status=fallback"):
        w_sweep.build_cache(_sets([question]), offline_only=True)


def test_build_cache_offline_only_returns_a_complete_live_cache(tmp_path, monkeypatch):
    """The happy path stays offline: complete live cache, no API call."""
    cache_path = tmp_path / "cache.json"
    question = "A fixture question with a live cached expansion?"
    entry = {"rewrites": ["rewrite one"], "status": w_sweep.STATUS_LIVE, "intent": "intent"}
    cache_path.write_text(json.dumps({question: entry}), encoding="utf-8")
    monkeypatch.setattr(w_sweep, "CACHE", str(cache_path))
    monkeypatch.setattr(w_sweep, "expand_query", _explode)

    cache = w_sweep.build_cache(_sets([question]), offline_only=True)
    assert cache[question]["status"] == w_sweep.STATUS_LIVE
    assert cache[question]["rewrites"] == ["rewrite one"]


# ---------------------------------------------------------------------------
# scripts/w_sweep.py — --persist-dir and --ranks-out
# ---------------------------------------------------------------------------
SWEEP_SECTIONS = ["1.1", "2.2", "3.3", "4.4", "5.5", "6.6"]


class _FakeDoc:
    """Minimal stand-in for a langchain Document (metadata only is read)."""

    def __init__(self, section):
        self.metadata = {"section_number": section}


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _stub_sweep(monkeypatch, tmp_path, calls, eval_registry, *, prefix="Fixture"):
    """Wire w_sweep's IO seams to fakes: set files, cache file, store, retrieval.

    The set files are real tmp JSONL files registered public (16A-1: the floor
    classifies every set w_sweep opens); the cache is listed in a tmp
    legacy_public.json, so ``--legacy-public`` makes the run public and its
    absence floors it to private (as with the real 0717 cache).
    """
    golden = [
        {"question": f"{prefix} golden sweep question {i}?", "expected_sections": ["1.1"],
         "type": "direct"}
        for i in range(2)
    ] + [{"question": f"{prefix} golden refusal question?", "expected_sections": [],
          "type": "refusal"}]
    realistic = [
        {"question": f"{prefix} realistic sweep question {i}?", "expected_sections": ["3.3"],
         "type": "direct"}
        for i in range(17)
    ]
    golden_path = _write_jsonl(tmp_path / "sets" / "golden_set.jsonl", golden)
    realistic_path = _write_jsonl(tmp_path / "sets" / "realistic_set.jsonl", realistic)
    eval_registry.add(golden_path)
    eval_registry.add(realistic_path)
    monkeypatch.setattr(
        w_sweep, "SET_PATHS", (("golden", str(golden_path)), ("realistic", str(realistic_path)))
    )
    # S5/N4 come from the id-keyed roster (item 9); point them at fixture rows.
    monkeypatch.setattr(
        w_sweep,
        "ROLE_BY_NAME",
        {
            "S5": RoleSpec("S5", public_v1_id(realistic[4]["question"]), ("2.2.1", "2.2.2")),
            "N4": RoleSpec("N4", public_v1_id(realistic[16]["question"]), ("2.2.1", "2.2.2")),
        },
    )

    answerable = [r for r in golden + realistic if r["type"] != "refusal"]
    cache_path = tmp_path / "expansions.json"
    cache_path.write_text(
        json.dumps(
            {
                row["question"]: {
                    "rewrites": ["a rewrite"],
                    "status": w_sweep.STATUS_LIVE,
                    "intent": "an intent",
                }
                for row in answerable
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(w_sweep, "CACHE", str(cache_path))
    _legacy_public(monkeypatch, tmp_path, cache_path)
    monkeypatch.setattr(w_sweep, "expand_query", _explode)

    def fake_load_retrieval_context(persist_directory=None):
        calls["persist_directory"] = persist_directory
        return ("vector-store", "bm25-index")

    def fake_retrieve(question, **kwargs):
        calls.setdefault("retrieve_kwargs", []).append(kwargs)
        return [{"document": _FakeDoc(s)} for s in SWEEP_SECTIONS]

    monkeypatch.setattr(w_sweep, "load_retrieval_context", fake_load_retrieval_context)
    monkeypatch.setattr(w_sweep, "retrieve", fake_retrieve)
    return [r for r in golden if r["type"] != "refusal"], realistic


def test_sweep_defaults_to_the_production_index_and_writes_no_file(
    tmp_path, monkeypatch, capsys, eval_registry
):
    """No new flag -> default (absolute) dir, printed summary only. Without
    --legacy-public the cache floors the run to private: aggregates still print."""
    calls = {}
    _stub_sweep(monkeypatch, tmp_path, calls, eval_registry)

    assert w_sweep.main([]) == 0

    assert calls["persist_directory"] == w_sweep.DEFAULT_PERSIST_DIR
    assert os.path.isabs(w_sweep.DEFAULT_PERSIST_DIR)
    out = capsys.readouterr().out
    for weight in w_sweep.WEIGHTS:
        assert f"=== W = {weight} ===" in out
    assert "  golden: strict@6 2/2 (1.000)" in out
    assert "S5: strict_rank=" in out and "N4: strict_rank=" in out
    assert not list(tmp_path.glob("*.json.out"))
    assert not (tmp_path / "eval" / "private").exists()  # nothing written


def test_sweep_ranks_out_dumps_machine_readable_per_question_ranks(
    tmp_path, monkeypatch, capsys, eval_registry
):
    """--ranks-out (public run) writes the C4 fields plus the legacy ``ranks``
    dict: question, expected, both ranks and the top-6 list."""
    calls = {}
    golden, realistic = _stub_sweep(monkeypatch, tmp_path, calls, eval_registry)
    ranks_path = tmp_path / "ranks.json"

    w_sweep.main(
        ["--persist-dir", "./chroma_db_arm_fixture", "--ranks-out", str(ranks_path),
         "--legacy-public"]
    )
    capsys.readouterr()

    assert calls["persist_directory"] == "./chroma_db_arm_fixture"
    payload = json.loads(ranks_path.read_text(encoding="utf-8"))
    assert payload["persist_dir"] == "./chroma_db_arm_fixture"
    assert payload["weights"] == list(w_sweep.WEIGHTS)
    assert len(payload["ranks"]) == len(w_sweep.WEIGHTS) * (len(golden) + len(realistic))

    row = payload["ranks"]["W=0.0|golden|0"]
    assert row["question"] == golden[0]["question"]
    assert row["expected"] == ["1.1"]
    assert row["strict_rank"] == 1
    assert row["related_rank"] == 1
    assert row["retrieved_sections"] == SWEEP_SECTIONS

    miss = payload["ranks"]["W=0.5|realistic|16"]
    assert miss["question"] == realistic[16]["question"]
    assert miss["strict_rank"] == 3  # "3.3" is third in the stubbed list
    assert calls["retrieve_kwargs"][-1]["intent_weight"] == 0.5

    # C4 fields (item 6): the same identity a rows sidecar carries.
    from src.eval_cohort import SCORER_VERSION

    assert payload["version"] == 1
    assert payload["scorer_version"] == SCORER_VERSION
    assert payload["absorbed_map_sha256"] is None
    assert payload["expansion"] == {
        "kind": "cache", "digest": eval_sets.sha256_file(w_sweep.CACHE)
    }
    assert payload["privacy"] == "public"
    assert {c["label"] for c in payload["cohorts"]} == {"golden", "realistic"}
    golden_cohort = next(c for c in payload["cohorts"] if c["label"] == "golden")
    assert golden_cohort["rows"] == 3  # the refusal row is in the cohort...
    assert len(payload["rows"]) == len(w_sweep.WEIGHTS) * (len(golden) + len(realistic))
    by_key = {(r["mode"], r["id"]): r for r in payload["rows"]}
    c4_row = by_key[("W=0.5", public_v1_id(realistic[16]["question"]))]
    assert c4_row["strict_rank"] == 3 and c4_row["completion_rank"] == 3
    assert c4_row["set_sha256"] == eval_sets.sha256_file(w_sweep.SET_PATHS[1][1])
    # ...but never scored (refusals have no expected section).
    refusal_id = public_v1_id("Fixture golden refusal question?")
    assert all(r["id"] != refusal_id for r in payload["rows"])


def test_ranks_of_matches_the_evaluator_first_rank_logic():
    """The one helper (score_evidence, one group) keeps the old semantics."""
    assert w_sweep.ranks_of(["2.2"], ["", "2.2.1", "2.2"]) == (3, 2)
    assert w_sweep.ranks_of(["9.9"], ["1.1", "2.2"]) == (None, None)
    assert w_sweep.ranks_of([], ["1.1"]) == (None, None)
    assert not hasattr(w_sweep, "first_ranks")


# ---------------------------------------------------------------------------
# scripts/embed_latency.py
# ---------------------------------------------------------------------------
class _FakeClock:
    """Deterministic perf_counter: returns each scripted value in turn."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        value = self.values[self.calls]
        self.calls += 1
        return value


def test_measure_computes_deterministic_percentiles_with_a_fake_clock(monkeypatch):
    """Timing core: warmup is untimed, percentiles are nearest-rank."""
    seen = []
    # One (start, end) pair per timed query: 10ms, 20ms, 30ms, 40ms.
    clock = _FakeClock([0.0, 0.010, 1.0, 1.020, 2.0, 2.030, 3.0, 3.040])
    monkeypatch.setattr(embed_latency, "perf_counter", clock)

    stats = embed_latency.measure(seen.append, ["q1", "q2", "q3", "q4"], warmup=3)

    assert stats["cold_load_s"] is None
    assert stats["p50_ms"] == pytest.approx(20.0)
    assert stats["p95_ms"] == pytest.approx(40.0)
    assert stats["mean_ms"] == pytest.approx(25.0)
    assert len(seen) == 7  # 3 warmup + 4 timed
    assert seen[:3] == ["q1", "q2", "q3"]  # warmup cycles the query list
    assert clock.calls == 8  # warmup calls are not clocked


def test_measure_rejects_an_empty_query_list():
    """No queries means no measurement — fail rather than divide by zero."""
    with pytest.raises(ValueError):
        embed_latency.measure(lambda q: None, [])


@pytest.mark.parametrize(
    "requested,expected",
    [(20, 3), (2, 2), (3, 3), (0, 0), (-1, 0)],
)
def test_load_queries_clamps_n_to_the_file(tmp_path, requested, expected):
    """--n is clamped to the file's size (and never negative)."""
    path = tmp_path / "questions.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"question": f"Fixture latency question {i}?", "type": "direct"})
            for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    questions = embed_latency.load_queries(str(path), requested)
    assert len(questions) == expected
    if questions:
        assert questions[0] == "Fixture latency question 0?"


def test_percentile_is_nearest_rank():
    """p50/p95 return observed samples, not interpolated ones."""
    samples = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert embed_latency._percentile(samples, 50) == 3.0
    assert embed_latency._percentile(samples, 95) == 5.0
    assert embed_latency._percentile([7.0], 95) == 7.0


def test_default_query_set_is_not_the_heldout_set():
    """The latency default points at the realistic set, never the held-out one."""
    assert os.path.basename(embed_latency.DEFAULT_QUERIES) == "realistic_set.jsonl"


class TestCompareProdRanks:
    """compare_prod_ranks — selection disqualifier #2 (production config)."""

    @staticmethod
    def _dump(rows):
        return {"ranks": rows}

    def test_flip_gain_and_clean_rows(self):
        from scripts.bakeoff_report import compare_prod_ranks

        base = self._dump({
            "W=0.25|golden|0": {"question": "q0", "strict_rank": 3},
            "W=0.25|golden|1": {"question": "q1", "strict_rank": None},
            "W=0.25|golden|2": {"question": "q2", "strict_rank": 1},
            "W=0.5|golden|0": {"question": "q0", "strict_rank": 9},
            "W=0.25|realistic|0": {"question": "r0", "strict_rank": 2},
        })
        arm = self._dump({
            "W=0.25|golden|0": {"question": "q0", "strict_rank": 9},
            "W=0.25|golden|1": {"question": "q1", "strict_rank": 4},
            "W=0.25|golden|2": {"question": "q2", "strict_rank": 2},
            "W=0.5|golden|0": {"question": "q0", "strict_rank": 1},
            "W=0.25|realistic|0": {"question": "r0", "strict_rank": None},
        })
        out = compare_prod_ranks(base, arm, legacy=True)
        assert [r["question"] for r in out["flips"]] == ["q0"]  # 3 -> 9
        assert [r["question"] for r in out["gains"]] == ["q1"]  # None -> 4
        assert out["unmatched"] == []
        # W=0.5 and realistic rows must not leak into the golden W=0.25 view.
        assert all(r["key"].startswith("W=0.25|golden|") for r in out["flips"] + out["gains"])

    def test_unmatched_keys_surface(self):
        from scripts.bakeoff_report import compare_prod_ranks

        base = self._dump({"W=0.25|golden|0": {"question": "q0", "strict_rank": 1}})
        arm = self._dump({"W=0.25|golden|1": {"question": "q1", "strict_rank": 1}})
        out = compare_prod_ranks(base, arm, legacy=True)
        assert out["flips"] == [] and out["gains"] == []
        assert out["unmatched"] == ["W=0.25|golden|0", "W=0.25|golden|1"]

    def test_rank_six_is_a_hit_seven_is_not(self):
        from scripts.bakeoff_report import compare_prod_ranks

        base = self._dump({"W=0.25|golden|0": {"question": "q", "strict_rank": 6}})
        arm = self._dump({"W=0.25|golden|0": {"question": "q", "strict_rank": 7}})
        out = compare_prod_ranks(base, arm, legacy=True)
        assert [r["question"] for r in out["flips"]] == ["q"]


# ---------------------------------------------------------------------------
# C2 — set-provenance guard
# ---------------------------------------------------------------------------
def test_parse_report_rejects_a_recorded_heldout_set_path_under_a_neutral_name():
    """The CLI filename check cannot see inside a report: a held-out run saved
    as ``arm-b.md`` records ``eval/heldout_set.jsonl`` in its provenance. The
    parser must refuse it rather than let the label fallback treat it as
    golden. (Synthetic text; the real held-out file is never read.)"""
    text = build_report().replace(
        "  - path: eval/golden_set.jsonl", "  - path: eval/heldout_set.jsonl"
    )
    assert "heldout_set.jsonl" in text
    with pytest.raises(ValueError, match="held-out"):
        bakeoff_report.parse_report(text)


def test_parse_report_rejects_a_recorded_set_with_no_path():
    text = build_report().replace("  - path: eval/golden_set.jsonl\n", "")
    with pytest.raises(ValueError, match="only"):
        bakeoff_report.parse_report(text)


def test_cli_rejects_a_neutrally_named_report_recording_heldout(tmp_path):
    bad = tmp_path / "arm-b.md"
    bad.write_text(
        build_report().replace("eval/golden_set.jsonl", "eval/heldout_set.jsonl"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="held-out"):
        bakeoff_report.main(["--reports", str(bad), "--baseline", "arm-b"])


# ---------------------------------------------------------------------------
# C3 — vacuous-pass guards
# ---------------------------------------------------------------------------
def _ablation_n(n):
    return {mode: vals[:8] + (n,) for mode, vals in BASELINE_GOLDEN_ABLATION.items()}


def test_compare_fails_on_an_empty_baseline_golden_detail():
    empty = build_report(golden_rows=[], golden_ablation=_ablation_n(0))
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(empty),
        "candidate": bakeoff_report.parse_report(build_report()),
    }
    with pytest.raises(ValueError, match="baseline-fixture.*no golden"):
        bakeoff_report.compare(arms, "baseline-fixture", legacy=True)


def test_compare_fails_on_an_empty_arm_golden_detail():
    empty = build_report(golden_rows=[], golden_ablation=_ablation_n(0))
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(build_report()),
        "candidate": bakeoff_report.parse_report(empty),
    }
    with pytest.raises(ValueError, match="candidate.*no golden"):
        bakeoff_report.compare(arms, "baseline-fixture", legacy=True)


def test_compare_fails_when_detail_count_disagrees_with_reported_n():
    """Ablation says n=3 but only one detail row parsed: truncated report."""
    short = build_report(
        golden_rows=[
            _detail_row("direct", 1, 1, ["9.1"], ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6"], G1),
        ]
    )
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(build_report()),
        "candidate": bakeoff_report.parse_report(short),
    }
    with pytest.raises(ValueError, match="n=3.*1 rows"):
        bakeoff_report.compare(arms, "baseline-fixture", legacy=True)


def test_parse_report_rejects_a_report_with_no_provenance_block():
    """Re-review C2: with the whole ``Question sets:`` block removed, sets are
    synthesised from the ablation/detail headings with path=None. They must be
    refused, not let into selection via the label fallback."""
    text = re.sub(r"Question sets:\n(?:.*\n)*?\n", "\n", build_report(), count=1)
    assert "Question sets:" not in text
    with pytest.raises(ValueError, match="only .* may enter selection"):
        bakeoff_report.parse_report(text)


def test_compare_fails_when_the_golden_ablation_section_is_missing():
    """Re-review C3: without the ablation section there is no reported n, so a
    truncated detail list cannot be detected. Missing n must fail, not pass."""
    text = re.sub(
        r"## tuning — retrieval ablation\n(?:.*\n)*?(?=## realistic — retrieval ablation)",
        "",
        build_report(),
        count=1,
    )
    assert "## tuning — retrieval ablation" not in text
    arms = {
        "baseline-fixture": bakeoff_report.parse_report(build_report()),
        "candidate": bakeoff_report.parse_report(text),
    }
    with pytest.raises(ValueError, match="no reported n"):
        bakeoff_report.compare(arms, "baseline-fixture", legacy=True)


class TestCompareProdRanksVacuous:
    ROW = {"W=0.25|golden|0": {"question": "q", "strict_rank": 1}}

    def test_a_row_without_strict_rank_is_missing_evidence_not_a_miss(self):
        """Re-review C3: a dump row lacking the field must fail; an explicit
        JSON null (a genuine miss) stays accepted, see TestCompareProdRanks."""
        bare = {"W=0.25|golden|0": {"question": "q"}}
        with pytest.raises(ValueError, match="lacks strict_rank"):
            bakeoff_report.compare_prod_ranks({"ranks": self.ROW}, {"ranks": bare}, legacy=True)
        with pytest.raises(ValueError, match="lacks strict_rank"):
            bakeoff_report.compare_prod_ranks({"ranks": bare}, {"ranks": self.ROW}, legacy=True)

    def test_an_explicit_null_strict_rank_is_a_recorded_miss(self):
        null = {"W=0.25|golden|0": {"question": "q", "strict_rank": None}}
        out = bakeoff_report.compare_prod_ranks({"ranks": self.ROW}, {"ranks": null}, legacy=True)
        assert [r["question"] for r in out["flips"]] == ["q"]

    def test_no_baseline_rows_for_the_prefix_fails(self):
        with pytest.raises(ValueError, match="baseline"):
            bakeoff_report.compare_prod_ranks({"ranks": {}}, {"ranks": self.ROW}, legacy=True)

    def test_no_arm_rows_for_the_prefix_fails(self):
        wrong_weight = {"W=0.5|golden|0": {"question": "q", "strict_rank": 1}}
        with pytest.raises(ValueError, match="arm"):
            bakeoff_report.compare_prod_ranks({"ranks": self.ROW}, {"ranks": wrong_weight}, legacy=True)


# ---------------------------------------------------------------------------
# C5 — manifest provenance fields
# ---------------------------------------------------------------------------
def _legacy_public(monkeypatch, tmp_path, *paths):
    """List ``paths`` in a tmp eval/legacy_public.json (as the frozen pre-16A
    bake-off artifacts and the 0717 cache are listed in the real one)."""
    legacy = tmp_path / "legacy_public.json"
    legacy.write_text(
        json.dumps(
            {"version": 1, "entries": [
                {"path": str(p), "sha256": eval_sets.sha256_file(p)} for p in paths
            ]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", legacy)


def test_manifest_records_prod_rank_hashes_weight_cache_and_command_line(
    tmp_path, capsys, monkeypatch
):
    baseline, candidate = _write_arms(tmp_path)
    base_dump = tmp_path / "baseline-fixture.json"
    arm_dump = tmp_path / "candidate.json"
    rows = {"W=0.25|golden|0": {"question": "q", "strict_rank": 1}}
    base_dump.write_text(json.dumps({"ranks": rows}), encoding="utf-8")
    arm_dump.write_text(json.dumps({"ranks": rows}), encoding="utf-8")
    cache = tmp_path / "expansions.json"
    cache.write_text("{}", encoding="utf-8")
    _legacy_public(monkeypatch, tmp_path, base_dump, arm_dump, cache)
    manifest_path = tmp_path / "manifest.json"
    argv = [
        "--reports", str(baseline), str(candidate),
        "--baseline", "baseline-fixture",
        "--prod-ranks", str(base_dump), str(arm_dump),
        "--expansion-cache", str(cache),
        "--manifest-out", str(manifest_path),
        "--legacy", "--legacy-public",
    ]

    assert bakeoff_report.main(argv) == 0
    capsys.readouterr()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["prod_ranks"] == [
        {"path": str(base_dump), "sha256": bakeoff_report.sha256_file(str(base_dump))},
        {"path": str(arm_dump), "sha256": bakeoff_report.sha256_file(str(arm_dump))},
    ]
    assert manifest["shipped_weight"] == bakeoff_report.SHIPPED_WEIGHT == 0.25
    assert manifest["expansion_cache"] == {
        "path": str(cache),
        "sha256": bakeoff_report.sha256_file(str(cache)),
    }
    assert manifest["command_line"][1:] == argv


def test_manifest_without_prod_ranks_has_empty_fields_not_missing_ones(tmp_path):
    baseline, _ = _write_arms(tmp_path)
    manifest = bakeoff_report.build_manifest(
        {"baseline-fixture": bakeoff_report.parse_report(build_report())},
        {"baseline-fixture": str(baseline)},
        "baseline-fixture",
        command_line=["x"],
    )
    assert manifest["prod_ranks"] == []
    assert manifest["expansion_cache"] is None
    assert manifest["command_line"] == ["x"]
