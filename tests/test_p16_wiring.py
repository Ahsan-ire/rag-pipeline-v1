"""Phase 16A-1 runner wiring: (m) meter, (k2) artifact replay, (d) sidecar/provenance.

The meter core is tested in tests/test_p16w_spend.py; here a real SpendMeter
with fake inner chat models runs through run_eval_matrix's DEFAULT generation,
expansion and judge paths (retrieval and the index faked), so the wiring
itself is what is exercised.
"""

import ast
import json
from pathlib import Path

import pytest
from langchain_core.documents import Document

import src.evaluator as ev
from src.spend import SpendLimitReached, SpendMeterRequired
from tests.p16_capture import PROVENANCE, FakeRetrieval, _fake_expand
from tests.test_p16w_spend import FakeChat, _usage, ledger_lines, make_meter

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = "eval/sample_golden_set.jsonl"


@pytest.fixture
def fake_index(monkeypatch):
    """Default retrieval paths without an index: store/BM25 loaders and retrieve() faked."""
    calls = []
    monkeypatch.setattr(ev, "assert_embedding_model", lambda *a, **k: None)
    monkeypatch.setattr(ev, "get_vector_store", lambda **k: object())
    monkeypatch.setattr(ev, "load_bm25_index", lambda *a, **k: object())

    def fake_retrieve(question, top_k=6, **kw):
        calls.append(question)
        doc = Document(page_content="Synthetic chunk text.", id="c1",
                       metadata={"section_number": "1.1", "page_start": 1, "page_end": 1,
                                 "source": "synthetic.pdf", "chapter_number": "1"})
        return [{"document": doc, "score": 1.0, "metadata": doc.metadata}]

    monkeypatch.setattr(ev, "retrieve", fake_retrieve)
    return calls


def _fakes():
    gen = FakeChat(model="fake-gen", usage=_usage(), reply="The rule applies [Handbook, para 1.1, p.1].")
    rew = FakeChat(model="fake-rewrite", usage=_usage(),
                   reply="1) formal synthetic rewrite\n2) synthetic keywords\n3) plain synthetic\n4) INTENT: synthetic intent")
    jud = FakeChat(model="fake-judge", usage=_usage(),
                   reply=json.dumps({"claims": [{"claim": "c", "verdict": "supported"}]}))
    return {"generation": gen, "rewrite": rew, "judge": jud}


def test_metered_default_paths_settle_all_three_kinds(monkeypatch, tmp_path, fake_index):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    fakes = _fakes()
    meter = make_meter(fakes)
    result = ev.run_eval_matrix(
        [("golden", SAMPLE)], modes=["hybrid+rewrite"], judge=True, results_path=str(tmp_path / "r.md"),
        provenance_fn=lambda: dict(PROVENANCE), privacy="public", meter=meter,
    )
    kinds = {l["kind"] for l in ledger_lines() if l["event"] == "settle"}
    assert kinds == {"generation", "rewrite", "judge"}
    assert all(f.sent for f in fakes.values())
    assert result["rewrite_fallbacks"] == 0 and result["generation_errors"] == 0


def test_live_default_path_with_key_and_no_meter_raises_before_any_call(monkeypatch, tmp_path, fake_index):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    called = []
    monkeypatch.setattr(ev, "expand_query", lambda q, **k: called.append(q))
    with pytest.raises(SpendMeterRequired):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "r.md"),
                           provenance_fn=lambda: dict(PROVENANCE), privacy="public")
    assert called == [] and fake_index == []


def test_no_key_no_meter_is_allowed_offline(tmp_path):
    retrieval = FakeRetrieval({})
    ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "r.md"),
                       retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
                       privacy="public", skip_refusals=True, skip_completeness=True)


def test_spend_limit_passes_through_generate_answers_once():
    calls = []

    def gen(q):
        calls.append(q)
        raise SpendLimitReached("week")

    with pytest.raises(SpendLimitReached):
        ev.generate_answers([{"question": "q one", "type": "direct"}, {"question": "q two", "type": "direct"}],
                            ["direct"], gen, retry_backoff=0)
    assert calls == ["q one"]


def test_spend_limit_passes_through_expand_query_and_judge():
    from src.judge import judge_answer
    from src.query_rewrite import expand_query

    boom = FakeChat(model="fake-rewrite", script=[SpendLimitReached("run")])
    with pytest.raises(SpendLimitReached):
        expand_query("synthetic question", llm=boom)

    def llm_fn(v):
        raise SpendLimitReached("week")

    with pytest.raises(SpendLimitReached):
        judge_answer("q", "a", "c", llm_fn=llm_fn)


def test_spend_limit_passes_through_run_eval_matrix_and_writes_no_report(tmp_path):
    retrieval = FakeRetrieval({})

    def gen(q):
        raise SpendLimitReached("run")

    out = tmp_path / "r.md"
    with pytest.raises(SpendLimitReached):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(out), retrieve_fn_factory=retrieval.factory(6),
                           generate_fn=gen, provenance_fn=lambda: dict(PROVENANCE), privacy="public",
                           skip_refusals=True)
    assert not out.exists()


def test_pipeline_query_never_builds_a_meter():
    tree = ast.parse((ROOT / "src" / "pipeline.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "query":
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            assert not {"SpendMeter", "meter", "load_prices"} & names


# --- (k2) replay at the runner ------------------------------------------------------
def _counting_expand(calls):
    def expand(question, **kw):
        calls.append(question)
        return _fake_expand(question, **kw)

    return expand


def test_build_then_replay_matches_with_zero_live_calls(monkeypatch, tmp_path):
    retrieval = FakeRetrieval({})
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    art = tmp_path / "exp.json"
    common = dict(retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x", "generation_status": "complete", "gate_outcome": "REFUSAL"})
    live = ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}", **common)
    n_live = len(calls)
    assert n_live > 0 and art.exists()
    replay = ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "b.md"), expansion=str(art), **common)
    assert len(calls) == n_live  # zero expansion calls on replay
    assert replay["rewrite_live"] == 0 and replay["rewrite_replayed"] == n_live
    assert replay["is_canonical"] is False
    a = json.loads(Path(str(tmp_path / "a.md") + ".rows.json").read_text())
    b = json.loads(Path(str(tmp_path / "b.md") + ".rows.json").read_text())
    assert a["rows"] == b["rows"]
    assert b["expansion"]["kind"] == "replay"
    assert "expansion artifact (replay)" in (tmp_path / "b.md").read_text()


def test_replay_missing_entry_fails_with_zero_calls(monkeypatch, tmp_path, eval_registry):
    retrieval = FakeRetrieval({})
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    small = tmp_path / "small.jsonl"
    small.write_text(Path(ROOT / SAMPLE).read_text().splitlines()[0] + "\n")
    eval_registry.add(small)
    art = tmp_path / "exp.json"
    ev.run_eval_matrix([("golden", str(small))], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}",
                       retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
                       privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    calls.clear()
    factory_calls = []

    def factory(mode):
        factory_calls.append(mode)
        return retrieval.factory(6)(mode)

    from src.expansion_artifact import ExpansionArtifactError

    with pytest.raises(ExpansionArtifactError):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "b.md"), expansion=str(art),
                           retrieve_fn_factory=factory, provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    assert calls == [] and factory_calls == []


def test_replay_identity_mismatch_refuses(monkeypatch, tmp_path):
    retrieval = FakeRetrieval({})
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    art = tmp_path / "exp.json"
    common = dict(retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}", **common)
    doc = json.loads(art.read_text())
    doc["identity"]["config_hash"] = "0" * 64
    art.write_text(json.dumps(doc))
    from src.expansion_artifact import ExpansionArtifactError

    with pytest.raises(ExpansionArtifactError):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "b.md"), expansion=str(art), **common)


# --- (d) the sidecar never moves provenance ------------------------------------------
def test_provenance_identical_with_and_without_sidecar():
    """collect_provenance reads `git status --porcelain`, which omits ignored files:
    every sidecar name is git-ignored, so its presence cannot change provenance.
    Checked without writing into the repo (gate round 2)."""
    import subprocess

    for rel in ("eval/results.md.rows.json", "eval/results_partial.md.rows.json", "x/y/report.md.rows.json"):
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
        assert proc.returncode == 0, rel
    porcelain = " M src/x.py\n"
    assert ev._porcelain_dirty_paths(porcelain) == ["src/x.py"]


# --- review fixes: refusals happen before any work ---------------------------------
def test_build_over_an_input_set_refused_before_any_call(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    with pytest.raises(ValueError, match="build:"):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{SAMPLE}",
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_duplicate_question_in_second_set_refused_before_any_call(monkeypatch, tmp_path, eval_registry):
    import json as _json

    dup = tmp_path / "dup.jsonl"
    row = {"question": "synthetic duplicated question", "type": "direct", "expected_sections": ["1.1"]}
    dup.write_text(_json.dumps(row) + "\n" + _json.dumps(row) + "\n")
    eval_registry.add(dup)
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))

    def factory(mode):
        calls.append(mode)
        return FakeRetrieval({}).factory(6)(mode)

    from src.eval_cohort import CohortError

    with pytest.raises(CohortError):
        ev.run_eval_matrix([("golden", SAMPLE), ("realistic", str(dup))], results_path=str(tmp_path / "a.md"),
                           retrieve_fn_factory=factory, provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_matrix_formatter_fails_closed_without_privacy():
    with pytest.raises(KeyError):
        ev._format_matrix_report({"sets": []}, privacy="private")


def test_generate_answers_duplicate_error_has_no_text():
    with pytest.raises(ValueError) as exc:
        ev.generate_answers([{"question": "P16-CANARY-dupe", "type": "direct"}] * 2, ["direct"], lambda q: {"answer": "x"})
    assert "CANARY" not in str(exc.value)


# --- code-review fixes (phase gate) -------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_spend_limits_refused(bad):
    with pytest.raises(ValueError):
        make_meter(run_limit_eur=bad)
    with pytest.raises(ValueError):
        make_meter(owner_approved_eur=bad, approval_ref="x")


def test_v6_and_v5_live_identity_carry_rewrite_config(monkeypatch, tmp_path):
    from src.expansion_artifact import rewrite_identity

    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    r = ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"),
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    ident = r["expansion_identity"]
    assert ident["kind"] == "live" and ident["config_hash"] == rewrite_identity()["config_hash"]
    assert ident["prompt_sha256"] == rewrite_identity()["prompt_sha256"] and len(ident["digest"]) == 64


def test_private_build_with_empty_basename_refused_before_any_call(monkeypatch, tmp_path, eval_registry):
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    priv = tmp_path / "p.jsonl"
    priv.write_text(Path(ROOT / SAMPLE).read_text())
    with pytest.raises(ValueError, match="file name"):
        ev.run_eval_matrix([("golden", str(priv))], expansion="build:out/",
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="private", skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_public_built_artifact_replays_at_a_stronger_privacy(monkeypatch, tmp_path, _private_root_in_tmp):
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    art = tmp_path / "exp.json"
    common = dict(retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}",
                       privacy="public", **common)
    n = len(calls)
    r = ev.run_eval_matrix([("golden", SAMPLE)], expansion=str(art), privacy="private", **common)
    assert len(calls) == n and r["rewrite_live"] == 0


def test_private_replay_records_the_artifact_in_inputs_json(monkeypatch, tmp_path, _private_root_in_tmp):
    import json as _json

    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    art = tmp_path / "exp.json"
    common = dict(retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}",
                       privacy="public", **common)
    r = ev.run_eval_matrix([("golden", SAMPLE)], expansion=str(art), privacy="private", **common)
    inputs = _json.loads((Path(r["results_path"]).parent / "inputs.json").read_text())["inputs"]
    kinds = [(i["kind"], Path(i["path"]).name) for i in inputs]
    assert ("derived", "exp.json") in kinds and any(k == "questions" for k, _ in kinds)
    derived = next(i for i in inputs if i["kind"] == "derived")
    assert derived["sources"] and all(len(s["sha256"]) == 64 for s in derived["sources"])


def test_public_eval_functions_refuse_unmetered_live_defaults(monkeypatch):
    """Gate round 2: evaluate_refusals' default answer_fn and judge_answers' default llm_fn."""
    from src.judge import judge_answers

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    calls = []
    monkeypatch.setattr(ev, "generate_with_sources", lambda *a, **k: calls.append("gen"))
    monkeypatch.setattr(ev, "_build_default_retrieve_fn", lambda *a, **k: (lambda q, top_k=6: []))
    with pytest.raises(SpendMeterRequired):
        ev.evaluate_refusals([{"question": "q", "type": "refusal", "expected_sections": []}])
    with pytest.raises(SpendMeterRequired):
        judge_answers([{"question": "q", "answer": "a", "context": "c"}])
    assert calls == []



# --- gate round 2 (code review) ------------------------------------------------------
@pytest.mark.parametrize("dest", ["eval/results_partial.md", "judge.jsonl", "x.md"])
def test_build_target_collisions_and_non_json_refused(monkeypatch, tmp_path, dest):
    monkeypatch.chdir(ROOT)
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    target = dest if dest.startswith("eval/") else str(tmp_path / dest)
    with pytest.raises(ValueError, match="build:"):
        ev.run_eval_matrix([("golden", SAMPLE)], expansion=f"build:{target}", judge_dump_path=str(tmp_path / "judge.jsonl"),
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_run_eval_refuses_repeated_question_before_any_call(tmp_path, eval_registry):
    import json as _json

    from src.eval_cohort import CohortError

    p = tmp_path / "dup.jsonl"
    row = {"question": "synthetic repeated refusal", "type": "refusal", "expected_sections": []}
    p.write_text(_json.dumps(row) + "\n" + _json.dumps(row) + "\n")
    eval_registry.add(p)
    calls = []
    with pytest.raises(CohortError):
        ev.run_eval(str(p), retrieve_fn=lambda q, top_k=6: calls.append(q) or [],
                    answer_fn=lambda q: calls.append(q), provenance_fn=lambda: dict(PROVENANCE),
                    results_path=str(tmp_path / "r.md"), privacy="public")
    assert calls == []


def test_v6_bad_second_set_refused_before_any_call(monkeypatch, tmp_path, eval_registry):
    import json as _json

    import src.eval_schema as eval_schema
    from src.eval_schema import SchemaError

    inv = tmp_path / "inv.json"
    inv.write_text(_json.dumps({"version": 1, "map_sha256": "0" * 64, "sections": ["1.1"], "aliases": []}))
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
    good = tmp_path / "good.jsonl"
    good.write_text(_json.dumps({"schema": 2, "id": "goodrow-1", "family_id": "goodfam", "question": "synthetic good",
                                 "scope": "answer", "evidence": [["1.1"]]}) + "\n")
    bad = tmp_path / "bad.jsonl"
    bad.write_text(_json.dumps({"schema": 2, "id": "badrow-1", "family_id": "badfam", "question": "synthetic bad",
                                "scope": "answer", "evidence": [["9.9"]]}) + "\n")
    eval_registry.add(good)
    eval_registry.add(bad)
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    with pytest.raises(SchemaError):
        ev.run_eval_matrix([("golden", str(good)), ("realistic", str(bad))],
                           retrieve_fn_factory=lambda m: (lambda q, top_k=6: calls.append(q) or []),
                           provenance_fn=lambda: dict(PROVENANCE), privacy="public", results_path=str(tmp_path / "r.md"),
                           skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_private_failure_leaves_no_orphan_run_dir(tmp_path, eval_registry, _private_root_in_tmp):
    from src.eval_cohort import CohortError

    p = tmp_path / "dup.jsonl"
    p.write_text('{"question": "q dup", "type": "direct", "expected_sections": ["1.1"]}\n' * 2)
    with pytest.raises(CohortError):
        ev.run_eval_matrix([("golden", str(p))], retrieve_fn_factory=FakeRetrieval({}).factory(6),
                           provenance_fn=lambda: dict(PROVENANCE), privacy="private",
                           skip_refusals=True, skip_completeness=True)
    runs = _private_root_in_tmp / "runs"
    assert not runs.exists() or not any(runs.iterdir())


def test_formatters_refuse_a_weaker_class():
    from src.eval_privacy import PrivacyFloorError

    with pytest.raises(PrivacyFloorError):
        ev._format_matrix_report({"privacy": "private", "sets": []}, privacy="public")
    with pytest.raises(PrivacyFloorError):
        ev._format_report("g.jsonl", 6, {}, None, {}, [], privacy="public", data_privacy="private")


def test_bakeoff_rank_must_be_positive():
    from scripts.bakeoff_report import _is_rank

    assert _is_rank(None) and _is_rank(1) and _is_rank(6)
    assert not _is_rank(0) and not _is_rank(-1) and not _is_rank(True)


# --- gate round 3 ----------------------------------------------------------------------
def test_retriever_error_logs_carry_no_query_text(caplog):
    """Default (non-strict) retrieval logs the exception TYPE only (str(e) can echo the query)."""
    import logging

    from src.retriever import retrieve

    class Boom:
        def similarity_search_with_relevance_scores(self, q, **k):
            raise RuntimeError("P16-CANARY-retrieval " + q)

    caplog.set_level(logging.DEBUG)
    retrieve("zqxj private question about widgets", top_k=3, vector_store=Boom(), bm25_index=None,
             mode="vector")
    assert "zqxj" not in caplog.text and "CANARY" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_relevance_score_warning_is_silenced(recwarn):
    from langchain_core.documents import Document

    from src.retriever import retrieve

    class Store:
        def similarity_search_with_relevance_scores(self, q, **k):
            import warnings

            warnings.warn("Relevance scores must be between 0 and 1, got [chunk text]", UserWarning)
            return [(Document(page_content="chunk", metadata={"section_number": "1.1"}, id="c1"), 1.5)]

    retrieve("q", top_k=1, vector_store=Store(), bm25_index=None, mode="vector")
    assert not [w for w in recwarn if "Relevance scores" in str(w.message)]


@pytest.mark.parametrize("dest", ["eval/sets.json", "eval/legacy_public.json"])
def test_build_never_overwrites_committed_json(monkeypatch, dest):
    monkeypatch.chdir(ROOT)
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    with pytest.raises(ValueError, match="not an expansion artifact"):
        ev.run_eval_matrix([("golden", SAMPLE)], expansion=f"build:{dest}",
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: calls.append(q))
    assert calls == []


def test_replay_or_v6_targeting_results_md_refused_before_any_call(monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    art = tmp_path / "exp.json"
    common = dict(retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  privacy="public", skip_completeness=True)
    ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}",
                       generate_fn=lambda q: {"answer": "x"}, **common)
    canonical = tmp_path / "results.md"
    monkeypatch.setattr(ev, "DEFAULT_RESULTS_PATH", str(canonical))
    calls = []
    with pytest.raises(ValueError, match="not canonical"):
        ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(canonical), expansion=str(art),
                           generate_fn=lambda q: calls.append(q), **common)
    assert calls == [] and not canonical.exists()


def test_build_with_duplicate_sets_refused_before_any_call(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    with pytest.raises(ValueError, match="distinct input sets"):
        ev.run_eval_matrix([("golden", SAMPLE), ("realistic", SAMPLE)], expansion=f"build:{tmp_path / 'a.json'}",
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: calls.append(q),
                           results_path=str(tmp_path / "r.md"))
    assert calls == []


def test_meter_errors_stop_the_run_not_degrade_it():
    from src.judge import judge_answer
    from src.query_rewrite import expand_query
    from src.spend import LedgerCorrupt

    boom = FakeChat(model="fake-rewrite", script=[LedgerCorrupt("x")])
    with pytest.raises(LedgerCorrupt):
        expand_query("synthetic question", llm=boom)

    def llm_fn(v):
        raise LedgerCorrupt("x")

    with pytest.raises(LedgerCorrupt):
        judge_answer("q", "a", "c", llm_fn=llm_fn)

    def gen(q):
        raise LedgerCorrupt("x")

    with pytest.raises(LedgerCorrupt):
        ev.generate_answers([{"question": "q", "type": "direct"}], ["direct"], gen, retry_backoff=0)


# --- gate round 4 ----------------------------------------------------------------------
def test_judge_answer_default_is_refused_with_a_key(monkeypatch):
    from src.judge import judge_answer

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    with pytest.raises(SpendMeterRequired):
        judge_answer("q", "a", "c")


def test_metered_generation_is_not_retried_by_generate_answers(monkeypatch, tmp_path, fake_index):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    seen = {}
    real = ev.generate_answers

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(ev, "generate_answers", spy)
    ev.run_eval_matrix([("golden", SAMPLE)], modes=["hybrid"], results_path=str(tmp_path / "r.md"),
                       provenance_fn=lambda: dict(PROVENANCE), privacy="public", meter=make_meter(_fakes()),
                       skip_refusals=True)
    assert seen.get("retries") == 0


def test_public_build_under_private_root_is_not_redirected(monkeypatch, tmp_path, _private_root_in_tmp):
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    dest = _private_root_in_tmp / "runs" / "x" / "exp.json"
    r = ev.run_eval_matrix([("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{dest}",
                           retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                           privacy="public", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    assert Path(r["expansion_artifact"]["path"]) == dest and dest.exists()


def test_spend_totals_failure_keeps_the_exit_code(monkeypatch, capsys):
    import src.pipeline
    from src.spend import LedgerCorrupt, SpendLimitReached, SpendMeter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")

    def boom(set_specs, **kwargs):
        raise SpendLimitReached("week")

    monkeypatch.setattr("src.evaluator.run_eval_matrix", boom)
    monkeypatch.setattr(SpendMeter, "run_total_eur", property(lambda self: (_ for _ in ()).throw(LedgerCorrupt("x"))))
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--skip-refusals"])
    with pytest.raises(SystemExit) as exc:
        src.pipeline.main()
    assert exc.value.code == 3
    assert "Traceback" not in capsys.readouterr().err


# --- gate round 5 ---------------------------------------------------------------
def test_private_build_writes_inputs_json_before_the_artifact_and_never_overwrites(
    monkeypatch, tmp_path, _private_root_in_tmp
):
    """PT3: inputs.json exists before the artifact lands; CR1: an existing private
    artifact is refused before any call."""
    import json as _json

    import src.expansion_artifact as ea

    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    priv = tmp_path / "p.jsonl"
    priv.write_text(Path(ROOT / SAMPLE).read_text())
    seen = []
    real_save = ea.save_artifact

    def spy(target, artifact, **kw):
        runs = list((_private_root_in_tmp / "runs").glob("*/inputs.json"))
        seen.append((len(runs), kw.get("exclusive")))
        return real_save(target, artifact, **kw)

    monkeypatch.setattr(ea, "save_artifact", spy)
    common = dict(retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  privacy="private", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    r = ev.run_eval_matrix([("golden", str(priv))], expansion="build:exp.json", **common)
    assert seen == [(1, True)]  # inputs.json first; private writes are exclusive
    inputs = _json.loads((Path(r["results_path"]).parent / "inputs.json").read_text())["inputs"]
    assert any(i["kind"] == "derived" and Path(i["path"]).name == "exp.json" for i in inputs)
    calls = []
    monkeypatch.setattr(ev, "expand_query", _counting_expand(calls))
    with pytest.raises(ValueError, match="never overwritten"):
        ev.run_eval_matrix([("golden", str(priv))], expansion="build:exp.json", **common)
    assert calls == []


def test_expand_query_uses_the_single_key_rule(monkeypatch):
    """CR8: expand_query and get_rewrite_llm defer to generator.api_key_usable."""
    import src.generator as gen
    import src.query_rewrite as qr

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    monkeypatch.setattr(gen, "api_key_usable", lambda: False)
    assert qr.expand_query("synthetic widget question").status == qr.STATUS_NO_KEY
    with pytest.raises(ValueError):
        qr.get_rewrite_llm()


def test_honest_private_build_and_replay_pass_the_merge_gate_precheck(monkeypatch, tmp_path, _private_root_in_tmp):
    """Gate round 7: real build/replay runs (entries bound to real rows) pass the
    precheck once their question set is a needle source."""
    from scripts import scan_leaks
    from src import eval_sets

    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    priv = tmp_path / "p.jsonl"
    priv.write_text(Path(ROOT / SAMPLE).read_text())
    common = dict(retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
                  privacy="private", skip_completeness=True, generate_fn=lambda q: {"answer": "x"})
    ev.run_eval_matrix([("golden", str(priv))], expansion="build:exp.json", **common)
    art = _private_root_in_tmp / "artifacts" / "exp.json"
    ev.run_eval_matrix([("golden", str(priv))], expansion=str(art), **common)
    assert len(list((_private_root_in_tmp / "runs").glob("*/inputs.json"))) == 2
    assert scan_leaks.precheck({eval_sets.sha256_file(priv)}) == []
    # one tampered rewrite key -> the artifact no longer binds
    doc = json.loads(art.read_text())
    key = next(iter(doc["entries"]))
    doc["entries"][key.split("/")[0] + "/tampered"] = doc["entries"].pop(key)
    doc["build"]["entries"] = len(doc["entries"])
    art.write_text(json.dumps(doc))
    assert scan_leaks.precheck({eval_sets.sha256_file(priv)}) != []
