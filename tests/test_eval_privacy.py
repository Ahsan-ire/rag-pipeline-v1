"""Phase 16A-1 (b): canaries -- private content never leaves the private root.

Each canary is a run of tokens drawn from the letters g-z only, so no hex hash
(sha256s fill the reports and sidecars) can ever match one by accident. A leak
check normalises the haystack (escape-decode, casefold, keep a-z0-9 only) so a
re-cased, re-spaced, escaped or punctuation-altered copy is caught, and checks
every individual token, which is stronger than checking 8-token windows.
"""

import codecs
import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

import src.evaluator as ev
from tests.p16_capture import PROVENANCE, FakeRetrieval

ROOT = Path(__file__).resolve().parent.parent
_ALPHA = "ghijklmnopqrstuvwxyz"


def _token(rng):
    return "".join(rng.choice(_ALPHA) for _ in range(10))


class Canaries:
    """Distinct ``P16-CANARY-<field>-<tokens>`` values per field."""

    def __init__(self, seed=1610):
        rng = random.Random(seed)
        self.tokens = {}
        for field in ("qstart", "qend", "qmid", "gap", "evidence", "badid", "rowexc", "topexc",
                      "rewrite", "intent", "answer", "claim", "loader"):
            self.tokens[field] = _token(rng)
        self.mid_words = [_token(rng) for _ in range(9)]

    def value(self, field):
        return f"P16-CANARY-{field}-{self.tokens[field]}"

    def question(self, i):
        return (f"{self.value('qstart')} row{_ALPHA[i]} " + " ".join(self.mid_words) + f" {self.value('qend')}?")

    def all_tokens(self):
        return list(self.tokens.values()) + self.mid_words


def _normalise(text):
    try:
        decoded = codecs.decode(text, "unicode_escape")
    except Exception:  # noqa: BLE001 - best effort; the raw text is checked too
        decoded = text
    return re.sub(r"[^a-z0-9]", "", (text + " " + decoded).casefold())


def assert_no_leak(canaries, *texts, where=""):
    hay = _normalise("\n".join(t for t in texts if t))
    for tok in canaries.all_tokens():
        assert tok not in hay, f"canary token leaked {where}"


def _files_outside(root, private_root):
    """Files under ``root`` outside the private root, excluding the input sets."""
    out = []
    for p in Path(root).rglob("*"):
        if p.parent.name == "sets":
            continue  # the test's own input set (the private source itself)
        if p.is_file() and not str(p.resolve()).startswith(str(private_root.resolve())):
            out.append(p)
    return out


def _eval_snapshot():
    return {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}


@pytest.fixture
def canaries():
    return Canaries()


@pytest.fixture
def private_set(tmp_path, canaries):
    """An unregistered (hence private) v1 set: answerable + refusal rows."""
    rows = [
        {"question": canaries.question(0), "type": "direct", "expected_sections": [canaries.value("evidence")]},
        {"question": canaries.question(1), "type": "direct", "expected_sections": ["91.1"]},
        {"question": canaries.question(2), "type": "refusal", "expected_sections": []},
    ]
    p = tmp_path / "sets" / "private_set.jsonl"
    p.parent.mkdir()
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p, rows


def _fakes(canaries, rows):
    from langchain_core.documents import Document

    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    expected = {r["question"]: r["expected_sections"] for r in rows}
    retrieval = FakeRetrieval(expected)

    def expand(question, **kw):
        return Expansion(question, (canaries.value("rewrite") + " rewrite",), REWRITE_MODEL, STATUS_LIVE,
                         canaries.value("intent") + " intent")

    def generate_fn(question):
        if question == rows[1]["question"]:
            raise RuntimeError(canaries.value("rowexc") + " " + question)
        doc = Document(page_content=canaries.value("answer"), metadata={"section_number": "91.1"}, id="c1")
        return {
            "answer": f"{canaries.value('answer')} answer text [Handbook, para 91.1, p.1].",
            "gate_outcome": "CITATIONS_VERIFIED", "citations": [{"para": "91.1"}],
            "citation_check": {"grounded": [{"para": "91.1"}], "ungrounded": []},
            "source_documents": [doc], "generation_status": "complete",
        }

    def judge_fn(prompt_vars):
        return json.dumps({"claims": [{"claim": canaries.value("claim") + " " + prompt_vars["question"],
                                       "verdict": "supported"}]})

    return retrieval, expand, generate_fn, judge_fn


def test_private_matrix_run_leaks_nothing(tmp_path, monkeypatch, capsys, caplog, canaries, private_set,
                                          _private_root_in_tmp):
    caplog.set_level(logging.DEBUG)
    path, rows = private_set
    retrieval, expand, generate_fn, judge_fn = _fakes(canaries, rows)
    monkeypatch.setattr(ev, "expand_query", expand)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    canonical = tmp_path / "results.md"
    monkeypatch.setattr(ev, "DEFAULT_RESULTS_PATH", str(canonical))
    before = _eval_snapshot()

    result = ev.run_eval_matrix(
        [("held-out", str(path)), ("realistic", str(path))],
        judge=True, retrieve_fn_factory=retrieval.factory(6), generate_fn=generate_fn,
        judge_fn=judge_fn, provenance_fn=lambda: dict(PROVENANCE),
        judge_dump_path=str(tmp_path / "judge_dump_outside.jsonl"), privacy="private",
    )
    out = capsys.readouterr()
    assert result["is_canonical"] is False and not canonical.exists()
    assert not (tmp_path / "judge_dump_outside.jsonl").exists()
    outside = _files_outside(tmp_path, _private_root_in_tmp)
    assert_no_leak(canaries, out.out, out.err, caplog.text,
                   *[p.read_text(errors="replace") for p in outside], where="(matrix)")
    assert _eval_snapshot() == before
    # evidence groups reach the private report (and only it)
    report = Path(result["results_path"]).read_text()
    assert canaries.tokens["evidence"] in _normalise(report)
    assert canaries.tokens["qstart"] not in _normalise(report)  # ids, not question text
    run_dir = Path(result["results_path"]).parent
    assert (run_dir / "inputs.json").exists() and (run_dir / "judge_review.jsonl").exists()
    assert (run_dir / "report.md.rows.json").exists()


def test_private_run_eval_leaks_nothing(tmp_path, capsys, caplog, canaries, private_set, _private_root_in_tmp):
    caplog.set_level(logging.DEBUG)
    path, rows = private_set
    retrieval, _expand, generate_fn, _judge = _fakes(canaries, rows)

    def answer_fn(question):
        return {"answer": canaries.value("answer"), "generation_status": "truncated"}

    result = ev.run_eval(str(path), retrieve_fn=retrieval.factory(6)("hybrid"), answer_fn=answer_fn,
                         provenance_fn=lambda: dict(PROVENANCE), privacy="private")
    out = capsys.readouterr()
    outside = _files_outside(tmp_path, _private_root_in_tmp)
    assert_no_leak(canaries, out.out, out.err, caplog.text,
                   *[p.read_text(errors="replace") for p in outside], where="(run_eval)")
    assert str(result["results_path"]).startswith(str(_private_root_in_tmp))


def test_private_run_eval_through_the_actual_sdk_logs_no_question(tmp_path, monkeypatch, capsys, caplog, canaries,
                                                                   private_set, _private_root_in_tmp):
    """16A-1 merge gate, Codex #1: a private run_eval whose default generation path
    goes through the ACTUAL anthropic SDK (a spend meter over an httpx.MockTransport,
    nothing leaves the process) with SDK debug logging on (ANTHROPIC_LOG=debug) emits
    no question text into captured logging."""
    from tests.test_p16w_spend import ledger_lines, sdk_meter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")
    sdk_logger = logging.getLogger("anthropic")
    monkeypatch.setattr(sdk_logger, "level", logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    path, rows = private_set
    retrieval, *_ = _fakes(canaries, rows)
    seen = []
    ev.run_eval(str(path), retrieve_fn=retrieval.factory(6)("hybrid"), provenance_fn=lambda: dict(PROVENANCE),
                privacy="private", meter=sdk_meter(seen, reply="I cannot find that in the handbook."))
    assert seen and canaries.tokens["qstart"] in _normalise("".join(seen))  # the real SDK request carried it
    assert [l["kind"] for l in ledger_lines() if l["event"] == "settle"] == ["generation"]
    out = capsys.readouterr()
    outside = _files_outside(tmp_path, _private_root_in_tmp)
    assert_no_leak(canaries, out.out, out.err, caplog.text,
                   *[p.read_text(errors="replace") for p in outside], where="(run_eval, actual SDK)")


def test_private_cli_loader_error_prints_type_only(tmp_path, monkeypatch, capsys, canaries):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"question": canaries.question(0), "type": canaries.value("loader"),
                               "expected_sections": ["1.1"]}) + "\n")
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--golden", str(bad), "--skip-refusals", "--skip-completeness"])
    import src.pipeline

    with pytest.raises(SystemExit) as exc:
        src.pipeline.main()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert_no_leak(canaries, out.out, out.err, where="(cli loader error)")
    assert "ValueError" in out.err


def test_validate_eval_set_never_prints_text(tmp_path, canaries):
    p = tmp_path / "v2.jsonl"
    row = {"schema": 2, "id": canaries.value("badid"), "family_id": "f0000000a", "question": canaries.question(0),
           "scope": "partial", "evidence": [[canaries.value("evidence")]],
           "gaps": [{"id": "g1", "keywords": [canaries.value("gap")]}]}
    p.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"version": 1, "map_sha256": "0" * 64, "sections": ["1.1"], "aliases": []}))
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_eval_set.py"), str(p), "--inventory", str(inv)],
                          capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 1
    assert_no_leak(canaries, proc.stdout, proc.stderr, where="(validate_eval_set)")


def test_scan_output_catches_forced_injection(tmp_path, canaries, private_set):
    path, rows = private_set
    leak = tmp_path / "leaky.log"
    leak.write_text("ok\n" + json.dumps({"x": rows[0]["question"].upper()}) + "\n")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_leaks.py"), "--output", str(leak),
                           "--needles", str(path)], capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert proc.returncode == 5, proc.stderr[-500:]
    assert_no_leak(canaries, proc.stdout, proc.stderr, where="(scan_leaks hit lines)")


def test_private_escape_destinations_abort(tmp_path, canaries, private_set, _private_root_in_tmp):
    from src.eval_privacy import PrivatePathError

    path, rows = private_set
    retrieval, *_ = _fakes(canaries, rows)
    for bad in (str(_private_root_in_tmp / "runs" / ".." / ".." / "escape.md"), str(tmp_path / "outside.md")):
        with pytest.raises(PrivatePathError):
            ev.run_eval_matrix([("golden", str(path))], results_path=bad,
                               retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
                               privacy="private", skip_refusals=True, skip_completeness=True)


def _recording_runners(calls, *, offline=False):
    """``run_eval`` and ``run_eval_matrix`` callers whose every retrieval/model hook
    records a call. Default: every pass on (generation and the judge too);
    ``offline=True`` runs retrieval only, so a control run completes."""
    live = {} if offline else {"generate_fn": lambda q: calls.append("gen"),
                               "judge_fn": lambda v: calls.append("judge"), "judge": True}

    def factory(mode):
        calls.append(("factory", mode))
        return lambda q, top_k=6: calls.append(("retrieve", mode)) or []

    def matrix(sets, report, privacy):
        return ev.run_eval_matrix([("golden", str(s)) for s in sets], results_path=str(report),
                                  retrieve_fn_factory=factory, provenance_fn=lambda: dict(PROVENANCE),
                                  privacy=privacy, skip_refusals=offline, skip_completeness=offline, **live)

    def single(sets, report, privacy):
        return ev.run_eval(str(sets[0]), results_path=str(report),
                           retrieve_fn=lambda q, top_k=6: calls.append("retrieve") or [],
                           answer_fn=lambda q: calls.append("answer"), skip_refusals=offline,
                           provenance_fn=lambda: dict(PROVENANCE), privacy=privacy)

    return {"matrix": matrix, "run_eval": single}


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


# (input set's name in the run dir, report's name in the run dir): each pair
# makes one of the private run's own writes land on the input.
_OVERWRITE_CASES = {
    "report over the set": ("set.jsonl", "set.jsonl"),
    "report over the set, case variant": ("set.jsonl", "SET.jsonl"),
    "inputs.json over the set": ("inputs.json", "report.md"),
    "judge dump over the set": ("judge_review.jsonl", "report.md"),
    "rows sidecar over the set": ("report.md.rows.json", "report.md"),
}


@pytest.mark.parametrize("runner", ["matrix", "run_eval"])
@pytest.mark.parametrize("case", sorted(_OVERWRITE_CASES))
def test_private_write_over_an_input_refused_before_any_call(tmp_path, canaries, private_set,
                                                             _private_root_in_tmp, runner, case):
    """Final gate D1: a private run never writes its report, sidecar, inputs.json or
    judge dump over an input it reads. Fails on b290cea (the set was overwritten)."""
    _path, rows = private_set
    set_name, report_name = _OVERWRITE_CASES[case]
    if report_name != report_name.lower() and not os.path.exists(str(tmp_path).upper()):
        pytest.skip("case-sensitive filesystem: a case variant is a different file")
    rdir = _private_root_in_tmp / "runs" / "r20261010-d1"
    target = _write_rows(rdir / set_name, rows)
    before = target.read_bytes()
    calls = []
    with pytest.raises(ValueError):
        _recording_runners(calls)[runner]([target], rdir / report_name, "private")
    assert calls == []
    assert target.read_bytes() == before
    assert sorted(p.name for p in rdir.iterdir()) == [set_name]  # nothing else written


@pytest.mark.parametrize("runner", ["matrix", "run_eval"])
@pytest.mark.parametrize("name", ["inputs.json", "judge_review.jsonl", "Inputs.JSON"])
def test_private_report_named_as_a_run_contract_file_refused(tmp_path, canaries, private_set,
                                                            _private_root_in_tmp, runner, name):
    """Final gate D1: the report may not take inputs.json's (the scanner contract's)
    or the judge dump's name. Fails on b290cea (inputs.json was replaced by the report)."""
    path, _rows = private_set
    rdir = _private_root_in_tmp / "runs" / "r20261010-d1c"
    calls = []
    with pytest.raises(ValueError):
        _recording_runners(calls)[runner]([path], rdir / name, "private")
    assert calls == []
    assert not rdir.exists() or list(rdir.iterdir()) == []


@pytest.mark.parametrize("runner", ["matrix", "run_eval"])
def test_private_overwrite_guard_controls(tmp_path, canaries, private_set, _private_root_in_tmp,
                                          eval_registry, runner):
    """Controls: a private report beside (not over) an input set in the run dir is
    written, and a public report over its input set is still refused (D38)."""
    _path, rows = private_set
    rdir = _private_root_in_tmp / "runs" / "r20261010-d1ok"
    beside = _write_rows(rdir / "set.jsonl", rows)
    before = beside.read_bytes()
    calls = []
    _recording_runners(calls, offline=True)[runner]([beside], rdir / "report.md", "private")
    assert calls and beside.read_bytes() == before
    assert {"inputs.json", "report.md", "report.md.rows.json"} <= {p.name for p in rdir.iterdir()}

    pub = _write_rows(tmp_path / "pub" / "set.jsonl", [{"question": "Alpha bravo charlie delta echo?",
                                                         "type": "direct", "expected_sections": ["1.1"]}])
    eval_registry.add(pub)
    if runner == "matrix":  # run_eval's public path never had the D38 guard (pre-16A)
        calls.clear()
        with pytest.raises(ValueError):
            _recording_runners(calls)[runner]([pub], pub, "public")
        assert calls == []


def test_private_cli_report_over_its_own_set_refused(tmp_path, monkeypatch, capsys, canaries, private_set,
                                                     _private_root_in_tmp):
    """Final gate D1 through the CLI: ``pipeline eval --golden X -o X`` on a private X
    exits 1 with the type only, before any index open, and leaves X intact."""
    _path, rows = private_set
    target = _write_rows(_private_root_in_tmp / "runs" / "r20261010-cli" / "set.jsonl", rows)
    before = target.read_bytes()
    calls = []
    monkeypatch.setattr("src.evaluator.get_vector_store", lambda *a, **k: calls.append("store"))
    monkeypatch.setattr("src.evaluator.load_bm25_index", lambda *a, **k: calls.append("bm25"))
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--golden", str(target), "-o", str(target),
                                     "--skip-refusals", "--skip-completeness"])
    import src.pipeline

    with pytest.raises(SystemExit) as exc:
        src.pipeline.main()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "ValueError" in out.err
    assert_no_leak(canaries, out.out, out.err, where="(cli overwrite refusal)")
    assert calls == [] and target.read_bytes() == before


def test_public_privacy_on_private_input_raises_before_any_call(tmp_path, canaries, private_set):
    from src.eval_privacy import PrivacyFloorError

    path, rows = private_set
    calls = []

    def factory(mode):
        calls.append(mode)
        return lambda q, top_k=6: []

    with pytest.raises(PrivacyFloorError):
        ev.run_eval_matrix([("golden", str(path))], retrieve_fn_factory=factory,
                           generate_fn=lambda q: calls.append(q), judge_fn=lambda v: calls.append(v),
                           provenance_fn=lambda: dict(PROVENANCE), privacy="public")
    assert calls == []


def test_private_cli_top_level_exception_prints_type_only(tmp_path, monkeypatch, capsys, caplog, canaries,
                                                          private_set):
    """A top-level exception carrying canary text (and the question) reaches the CLI: type only."""
    caplog.set_level(logging.DEBUG)
    path, rows = private_set

    def boom(set_specs, **kwargs):
        raise RuntimeError(canaries.value("topexc") + " " + rows[0]["question"])

    monkeypatch.setattr("src.evaluator.run_eval_matrix", boom)
    monkeypatch.setattr("sys.argv", ["prog", "eval", "--golden", str(path), "--skip-refusals", "--skip-completeness"])
    import src.pipeline

    with pytest.raises(SystemExit) as exc:
        src.pipeline.main()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert_no_leak(canaries, out.out, out.err, caplog.text, where="(cli top-level exception)")
    assert "RuntimeError" in out.err


def test_private_copy_of_a_canonical_run_is_not_canonical(tmp_path, monkeypatch):
    """Control: the P0 canonical fakes make a PUBLIC run canonical; the same on private copies is not."""
    import shutil

    from tests.p16_capture import (
        MATRIX_SETS,
        FakeGeneration,
        _expected_index,
        _fake_expand,
        _judge_fn,
        _pick_canonical,
    )

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    canonical = tmp_path / "results.md"
    monkeypatch.setattr(ev, "DEFAULT_RESULTS_PATH", str(canonical))
    paths = [p for _l, p in MATRIX_SETS]
    expected, refusals = _expected_index(paths)
    retrieval = FakeRetrieval(expected)

    def run(specs, privacy, out):
        gen = FakeGeneration(retrieval, _pick_canonical)
        gen.refusal_qs = refusals
        return ev.run_eval_matrix(specs, judge=True, results_path=out, retrieve_fn_factory=retrieval.factory(6),
                                  generate_fn=gen, judge_fn=_judge_fn("clean"),
                                  provenance_fn=lambda: dict(PROVENANCE), privacy=privacy)

    public = run(list(MATRIX_SETS), "public", str(tmp_path / "public.md"))
    assert public["is_canonical"] is True
    copies = []
    for label, p in MATRIX_SETS:
        dst = tmp_path / "copies" / Path(p).name
        dst.parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / p, dst)
        copies.append((label, str(dst)))
    private = run(copies, "private", None)
    assert private["is_canonical"] is False
    assert not canonical.exists()
