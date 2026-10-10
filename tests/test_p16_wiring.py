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
    probe = ROOT / "eval" / "p16_probe_report.md.rows.json"
    assert not probe.exists()
    before = ev.collect_provenance(persist_directory="/nonexistent-index")
    try:
        probe.write_text("{}\n")
        after = ev.collect_provenance(persist_directory="/nonexistent-index")
    finally:
        probe.unlink()
    assert before == after


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
