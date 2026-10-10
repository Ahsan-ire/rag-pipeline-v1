"""Phase 16A-1 report v6 (item 3) and the Tier-2 offline equality, on fake retrieval.

Tier-2 (spec): offline v6 on the v2 copies of golden and realistic equals v5's
row-level numbers with an empty absorbed map. Here that is proved on the P0
fake retriever (the real-index run is a [C] step on the owner's machine).
"""

import json
from pathlib import Path

import pytest

import src.eval_schema as eval_schema
import src.evaluator as ev
from tests.p16_capture import (
    PROVENANCE,
    FakeGeneration,
    FakeRetrieval,
    _expected_index,
    _fake_expand,
    _pick_canonical,
)

ROOT = Path(__file__).resolve().parent.parent
V1 = {"golden": "eval/golden_set.jsonl", "realistic": "eval/realistic_set.jsonl"}
V2 = {"golden": "tests/fixtures/p16_v2_golden.jsonl", "realistic": "tests/fixtures/p16_v2_realistic.jsonl"}


@pytest.fixture
def v6_env(monkeypatch, tmp_path):
    """cwd = repo; inventory = every section of the two v2 fixtures; fakes for expansion."""
    monkeypatch.chdir(ROOT)
    sections = sorted({s for p in V2.values() for l in open(p) for g in json.loads(l)["evidence"] for s in g})
    inv = tmp_path / "inventory.json"
    inv.write_text(json.dumps({"version": 1, "map_sha256": "0" * 64, "sections": sections, "aliases": []}))
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    monkeypatch.setattr(ev, "_load_absorbed_map", lambda: (None, None))
    expected, refusals = _expected_index(list(V1.values()))
    return FakeRetrieval(expected), refusals


def _run(retrieval, specs, tmp_path, *, refusals=frozenset(), **kw):
    gen = FakeGeneration(retrieval, _pick_canonical)
    gen.refusal_qs = set(refusals)
    out = tmp_path / f"r{len(list(tmp_path.iterdir()))}.md"
    kw.setdefault("skip_refusals", True)
    kw.setdefault("skip_completeness", True)
    result = ev.run_eval_matrix(
        specs, results_path=str(out), retrieve_fn_factory=retrieval.factory(6),
        generate_fn=gen, provenance_fn=lambda: dict(PROVENANCE), privacy="public", **kw,
    )
    side = json.loads(Path(str(out) + ".rows.json").read_text())
    return result, out.read_text(), side


def test_tier2_v6_ranks_equal_v5_rows(v6_env, tmp_path):
    retrieval, _ = v6_env
    for name in ("golden", "realistic"):
        _r1, _t1, s1 = _run(retrieval, [(name, V1[name])], tmp_path)
        _r2, _t2, s2 = _run(retrieval, [(name, V2[name])], tmp_path)
        v1_rows = [json.loads(l) for l in open(ROOT / V1[name]) if l.strip()]
        v2_rows = [json.loads(l) for l in open(ROOT / V2[name]) if l.strip()]
        from src.eval_privacy import public_v1_id

        id_map = {public_v1_id(a["question"]): b["id"] for a, b in zip(v1_rows, v2_rows)}
        key = lambda r: (r["mode"], r["id"])  # noqa: E731
        v1 = {(r["mode"], id_map[r["id"]]): (r["strict_rank"], r["related_rank"]) for r in s1["rows"]}
        v2 = {key(r): (r["strict_rank"], r["related_rank"]) for r in s2["rows"]}
        assert v1 == v2, name
        assert len(v2) == 4 * sum(1 for r in v2_rows if r["scope"] != "refuse")


def test_v6_report_sections_in_order_and_never_canonical(v6_env, tmp_path):
    retrieval, refusals = v6_env
    result, text, side = _run(
        retrieval, [("golden", V2["golden"])], tmp_path, refusals=refusals,
        skip_refusals=False, skip_completeness=False,
    )
    assert result["is_canonical"] is False
    order = ["## Family counts", "## Scope confusion", "## Family rates", "## Cohort", "## Expansion", "## Run cost"]
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)
    assert "v6" in text.splitlines()[0]
    # ids only: no question text in a public v6 report either
    for line in open(ROOT / V2["golden"]):
        assert json.loads(line)["question"] not in text
    assert side["cohorts"][0]["schema"] == 2 and side["cohorts"][0]["families"] == side["cohorts"][0]["rows"]


def test_v6_scope_confusion_counts_every_row(v6_env, tmp_path):
    retrieval, refusals = v6_env
    result, _t, _s = _run(
        retrieval, [("golden", V2["golden"])], tmp_path, refusals=refusals,
        skip_refusals=False, skip_completeness=False,
    )
    conf = result["sets"][0]["confusion"]
    assert sum(sum(r.values()) for r in conf.values()) == result["sets"][0]["counts"]["rows"]


def test_v6_refuses_results_md(v6_env, tmp_path, monkeypatch):
    retrieval, _ = v6_env
    canonical = str(tmp_path / "results.md")
    monkeypatch.setattr(ev, "DEFAULT_RESULTS_PATH", canonical)
    with pytest.raises(ValueError, match="not canonical"):
        ev.run_eval_matrix(
            [("golden", V2["golden"])], results_path=canonical, retrieve_fn_factory=retrieval.factory(6),
            provenance_fn=lambda: dict(PROVENANCE), privacy="public",
            skip_refusals=True, skip_completeness=True,
        )
    assert not Path(canonical).exists()


def test_v6_refuses_judge(v6_env, tmp_path):
    retrieval, _ = v6_env
    with pytest.raises(ValueError, match="judge"):
        _run(retrieval, [("golden", V2["golden"])], tmp_path, judge=True, judge_fn=lambda v: "{}")


def test_v6_family_collapse_counts_paraphrases_once(v6_env, tmp_path, eval_registry):
    retrieval, _ = v6_env
    p = tmp_path / "fam.jsonl"
    rows = [
        {"schema": 2, "id": f"fam-a-{i}", "family_id": "fam-a", "question": f"synthetic widget phrasing {i}",
         "scope": "answer", "evidence": [["1.1"]]}
        for i in range(3)
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    eval_registry.add(p)
    result, _t, side = _run(retrieval, [("golden", str(p))], tmp_path)
    assert result["sets"][0]["counts"] == {"rows": 3, "families": 1, "retrieval_families": 1}
    assert result["sets"][0]["family_rates"]["retrieval"]["hybrid"]["strict"]["n"] == 1


def test_private_v6_writes_only_under_private_root(v6_env, tmp_path, _private_root_in_tmp, capsys):
    retrieval, _ = v6_env
    p = tmp_path / "priv.jsonl"
    p.write_text(json.dumps({"schema": 2, "id": "f0000000a-0", "family_id": "f0000000a",
                             "question": "P16-CANARY-v6 private question text here", "scope": "answer",
                             "evidence": [["1.1"]]}) + "\n")
    before = set(Path(tmp_path).rglob("*"))
    result = ev.run_eval_matrix(
        [("golden", str(p))], retrieve_fn_factory=retrieval.factory(6),
        provenance_fn=lambda: dict(PROVENANCE), privacy="private",
        skip_refusals=True, skip_completeness=True,
    )
    new = {f for f in set(Path(tmp_path).rglob("*")) - before if f.is_file()}
    assert new and all(str(f).startswith(str(_private_root_in_tmp)) for f in new)
    out = capsys.readouterr()
    from tests.p16_canary import assert_no_leak

    assert_no_leak(["P16-CANARY-v6 private question text here"], out.out, out.err,
                   *[f.read_text(errors="replace") for f in Path(tmp_path).rglob("*")
                     if f.is_file() and not str(f).startswith(str(_private_root_in_tmp)) and f != p])
    assert result["privacy"] == "private"


def test_private_v6_canary_gap_evidence_rewrites_intent_and_answers_stay_private(
    v6_env, tmp_path, monkeypatch, _private_root_in_tmp, capsys, caplog
):
    """(b): a private partial row with canary question, gap keyword and evidence; canary
    rewrites/intent from the expansion fake and canary answers from the generation fake."""
    import logging

    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion
    from tests.p16_canary import assert_no_leak
    from tests.test_eval_privacy import Canaries, _eval_snapshot

    caplog.set_level(logging.DEBUG)
    c = Canaries(seed=1612)
    retrieval, _ = v6_env
    section, gap_kw = c.value("evidence"), c.value("gap")
    # The inventory holds the canary evidence section, as the v6_env fixture does for v2 sections.
    inv = tmp_path / "inventory_canary.json"
    inv.write_text(json.dumps({"version": 1, "map_sha256": "0" * 64, "sections": [section], "aliases": []}))
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
    question = c.question(0)
    p = tmp_path / "priv_partial.jsonl"
    p.write_text(json.dumps({
        "schema": 2, "id": "f0000000b-0", "family_id": "f0000000b", "question": question,
        "scope": "partial", "evidence": [[section]], "gaps": [{"id": "g1", "keywords": [gap_kw]}],
    }) + "\n")
    retrieval.expected_by_q[question] = [section]

    def canary_expand(q, *args, **kwargs):
        return Expansion(q, (c.value("rewrite"), f"{c.value('rewrite')} {gap_kw}"), REWRITE_MODEL,
                         STATUS_LIVE, c.value("intent"))

    monkeypatch.setattr(ev, "expand_query", canary_expand)
    gen = FakeGeneration(retrieval, lambda q, is_refusal: "partial")

    def canary_generate(q):
        out = gen(q)
        # score_partial reads grounded citations as {"para": ...} dicts (the real shape).
        out["citation_check"] = {k: [{"para": x} for x in v] for k, v in out["citation_check"].items()}
        out["answer"] = f"{c.value('answer')} {out['answer']} {c.value('claim')} {gap_kw}"
        return out

    before = _eval_snapshot()
    before_tmp = set(tmp_path.rglob("*"))
    result = ev.run_eval_matrix(
        [("golden", str(p))], retrieve_fn_factory=retrieval.factory(6), generate_fn=canary_generate,
        provenance_fn=lambda: dict(PROVENANCE), privacy="private", skip_refusals=True,
        skip_completeness=False,
    )
    assert result["privacy"] == "private"
    out = capsys.readouterr()
    new = {f for f in set(tmp_path.rglob("*")) - before_tmp if f.is_file()}
    assert new and all(str(f).startswith(str(_private_root_in_tmp)) for f in new)
    # Positive control: the run really scored the partial row.
    assert result["sets"][0]["counts"]["rows"] == 1
    outside = [f.read_text(errors="replace") for f in tmp_path.rglob("*")
               if f.is_file() and not str(f).startswith(str(_private_root_in_tmp))
               and f not in (p, inv)]
    secrets = [*(c.value(f) for f in ("qstart", "qend", "gap", "evidence", "rewrite", "intent",
                                      "answer", "claim")), question, *c.all_tokens()]
    assert_no_leak(secrets, out.out, out.err, caplog.text, *outside, where="(private v6 canaries)")
    assert _eval_snapshot() == before
