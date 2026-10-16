"""Phase 16A-1: the `pipeline eval` CLI contract (privacy from classify, sealed exit 4, meter, spend exits)."""

import json
from pathlib import Path

import pytest

import src.evaluator as ev
import src.pipeline
from src.spend import SpendLimitReached, SpendMeter
from tests.p16_capture import PROVENANCE, FakeRetrieval, _fake_expand
from tests.test_p16_wiring import _fakes
from tests.test_p16w_spend import ledger_lines, make_meter, seed

SAMPLE = "eval/sample_golden_set.jsonl"


def _run(monkeypatch, argv, runner=None):
    captured = {}

    def spy(set_specs, **kwargs):
        captured["set_specs"] = set_specs
        captured["kwargs"] = kwargs
        if runner is not None:
            runner(set_specs, **kwargs)

    monkeypatch.setattr("src.evaluator.run_eval_matrix", spy)
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code
    return code, captured


def _set(path, sealed=False):
    row = {"question": "synthetic widget question", "type": "direct", "expected_sections": ["91.1"]}
    if sealed:
        row["sealed"] = True
    path.write_text(json.dumps(row) + "\n")
    return str(path)


def test_public_sets_pass_public(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--skip-completeness"])
    assert code == 0
    assert c["kwargs"]["privacy"] == "public"


def test_unregistered_set_passes_private(monkeypatch, tmp_path):
    code, c = _run(monkeypatch, ["--golden", _set(tmp_path / "g.jsonl"), "--skip-refusals", "--skip-completeness"])
    assert code == 0
    assert c["kwargs"]["privacy"] == "private"


def test_sealed_exits_4_before_runner(monkeypatch, tmp_path):
    code, c = _run(monkeypatch, ["--golden", _set(tmp_path / "s.jsonl", sealed=True), "--skip-refusals", "--skip-completeness"])
    assert code == 4
    assert c == {}


def test_offline_builds_no_meter(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--skip-completeness"])
    assert c["kwargs"]["meter"] is None


def test_live_with_key_builds_meter(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    code, c = _run(monkeypatch, ["--skip-refusals"])
    assert isinstance(c["kwargs"]["meter"], SpendMeter)


def test_live_without_key_builds_no_meter_and_does_not_crash(monkeypatch):
    """Keyless: no Claude call is possible, so the run degrades as before (no meter)."""
    code, c = _run(monkeypatch, ["--skip-refusals"])
    assert code == 0 and c["kwargs"]["meter"] is None


@pytest.mark.parametrize("kind,expected", [("week", 3), ("run", 6)])
def test_spend_limit_exit_codes(monkeypatch, kind, expected):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")

    def boom(set_specs, **kwargs):
        raise SpendLimitReached(kind)

    code, _ = _run(monkeypatch, ["--skip-refusals"], runner=boom)
    assert code == expected


def test_private_error_prints_type_only(monkeypatch, tmp_path, capsys):
    def boom(set_specs, **kwargs):
        raise RuntimeError("P16-CANARY-cli-exception text")

    code, _ = _run(monkeypatch, ["--golden", _set(tmp_path / "g.jsonl"), "--skip-refusals", "--skip-completeness"], runner=boom)
    out = capsys.readouterr()
    assert code == 1
    from tests.p16_canary import assert_no_leak

    assert_no_leak(["P16-CANARY-cli-exception text", "synthetic widget question"], out.out, out.err)
    assert "RuntimeError" in out.err


def test_public_error_still_raises(monkeypatch):
    def boom(set_specs, **kwargs):
        raise RuntimeError("public failure")

    with pytest.raises(RuntimeError):
        _run(monkeypatch, ["--skip-refusals", "--skip-completeness"], runner=boom)


def test_expansion_flag_threads(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--expansion", "build:x.json"])
    assert c["kwargs"]["expansion"] == "build:x.json"


@pytest.mark.parametrize("argv", [["--approved-eur", "nan"], ["--approved-eur", "0"],
                                  ["--owner-approved-eur", "100"]])
def test_bad_meter_arguments_exit_2_without_traceback(monkeypatch, capsys, argv):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    code, c = _run(monkeypatch, ["--skip-refusals", *argv])
    assert code == 2 and c == {}
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("golden", [False, True])
def test_meter_error_mid_run_exits_2_type_only_when_private(monkeypatch, tmp_path, capsys, golden):
    """Gate round 5 (CR6): a SpendMeterError (a BaseException) maps to exit 2."""
    from src.spend import LedgerCorrupt

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")

    def boom(set_specs, **kwargs):
        raise LedgerCorrupt("P16-CANARY-ledger synthetic widget question")

    argv = ["--skip-refusals"] + (["--golden", _set(tmp_path / "g.jsonl")] if golden else [])
    code, _ = _run(monkeypatch, argv, runner=boom)
    err = capsys.readouterr().err
    assert code == 2 and "LedgerCorrupt" in err and "Traceback" not in err
    if golden:
        assert "P16-CANARY" not in err


# --- 16A-2a (a6), acceptance (h): build:/replay under both skips, --regression -------
# These drive the REAL run_eval_matrix through the CLI (retrieval and provenance
# faked, the meter's chat models faked), so the classification in
# src/pipeline.py and the gating in src/evaluator.py are exercised together.
def _run_real(monkeypatch, argv):
    """`pipeline eval` through the real runner; returns (exit code, captured)."""
    real_matrix = ev.run_eval_matrix
    captured = {}

    def wrapper(set_specs, **kwargs):
        captured["set_specs"] = set_specs
        captured["kwargs"] = kwargs
        kwargs.setdefault("retrieve_fn_factory", FakeRetrieval({}).factory(6))
        kwargs.setdefault("provenance_fn", lambda: dict(PROVENANCE))
        captured["result"] = real_matrix(set_specs, **kwargs)
        return captured["result"]

    monkeypatch.setattr("src.evaluator.run_eval_matrix", wrapper)
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code
    return code, captured


def _keyed_no_real_clients(monkeypatch):
    """A usable (fake) key, and any real Anthropic client construction fails loudly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")

    def spy(*a, **k):
        raise AssertionError("a real model client was constructed")

    monkeypatch.setattr("src.generator.ChatAnthropic", spy)
    monkeypatch.setattr("src.query_rewrite.ChatAnthropic", spy)


def _meter_factory(monkeypatch, fakes):
    """The CLI's SpendMeter(...) becomes a meter over fake chat models; returns the built list."""
    built = []

    def factory(prices, ledger, run_limit, **kw):
        meter = make_meter(fakes, run_limit_eur=run_limit, **kw)
        built.append(meter)
        return meter

    monkeypatch.setattr("src.spend.SpendMeter", factory)
    return built


def _no_meter_allowed(monkeypatch):
    """Building a SpendMeter fails the test."""

    def boom(*a, **k):
        raise AssertionError("a spend meter was built")

    monkeypatch.setattr("src.spend.SpendMeter", boom)


def _forbid_expand_query(monkeypatch):
    """Any expand_query call fails the test; returns the (empty) call list."""
    calls = []

    def spy(question, **kw):
        calls.append(question)
        raise AssertionError("expand_query was called")

    monkeypatch.setattr(ev, "expand_query", spy)
    return calls


def _unique_questions(path):
    return len({e["question"] for e in ev.load_golden_set(path)})


BOTH_SKIPS = ["--skip-refusals", "--skip-completeness"]


@pytest.mark.parametrize(
    "argv,metered",
    [
        ([*BOTH_SKIPS, "--expansion", "build:x.json"], True),
        ([*BOTH_SKIPS, "--expansion", "x.json"], False),  # replay: local, zero calls
        ([*BOTH_SKIPS], False),
        ([*BOTH_SKIPS, "--expansion", "live"], False),
        ([*BOTH_SKIPS, "--judge"], True),
        (["--skip-refusals", "--expansion", "x.json"], True),  # one skip: still generates
    ],
)
def test_live_classification_builds_a_meter_only_for_live_paths(monkeypatch, argv, metered):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    code, c = _run(monkeypatch, argv)
    assert code == 0
    assert isinstance(c["kwargs"]["meter"], SpendMeter) is metered


def test_build_under_both_skips_builds_a_meter_and_makes_rewrite_calls_only(monkeypatch, tmp_path):
    _keyed_no_real_clients(monkeypatch)
    fakes = _fakes()
    built = _meter_factory(monkeypatch, fakes)
    art = tmp_path / "exp.json"
    code, c = _run_real(
        monkeypatch,
        ["--golden", SAMPLE, *BOTH_SKIPS, "--expansion", f"build:{art}", "-o", str(tmp_path / "r.md")],
    )
    assert code == 0
    meter = c["kwargs"]["meter"]
    assert isinstance(meter, SpendMeter) and built == [meter]
    n = _unique_questions(SAMPLE)
    # Haiku rewrite calls only: one per unique question, no generation, no judge.
    assert len(fakes["rewrite"].sent) == n
    assert fakes["generation"].sent == [] and fakes["judge"].sent == []
    assert {l["kind"] for l in ledger_lines() if l["event"] == "settle"} == {"rewrite"}
    r = c["result"]
    assert r["generation_ran"] is False and r["expansion_enabled"] is True
    assert (r["rewrite_attempts"], r["rewrite_live"], r["rewrite_fallbacks"]) == (n, n, 0)
    assert art.exists() and r["is_canonical"] is False


@pytest.mark.parametrize("kind,expected,extra", [("week", 3, []), ("run", 6, ["--approved-eur", "0.001"])])
def test_build_under_both_skips_propagates_the_spend_exits(monkeypatch, tmp_path, capsys, kind, expected, extra):
    _keyed_no_real_clients(monkeypatch)
    if kind == "week":
        seed(39.9995)  # a rewrite call's worst case no longer fits the 40 EUR week
    fakes = _fakes()
    _meter_factory(monkeypatch, fakes)
    art, report = tmp_path / "exp.json", tmp_path / "r.md"
    code, _ = _run_real(
        monkeypatch,
        ["--golden", SAMPLE, *BOTH_SKIPS, "--expansion", f"build:{art}", "-o", str(report), *extra],
    )
    assert code == expected
    assert f"spend limit reached ({kind})" in capsys.readouterr().err
    # Refused before anything was sent, and nothing was written.
    assert fakes["rewrite"].sent == [] and fakes["generation"].sent == [] and fakes["judge"].sent == []
    assert not report.exists() and not art.exists()


def test_replay_under_both_skips_runs_retrieval_only_with_zero_calls(monkeypatch, tmp_path):
    # Build an artifact (runner level, both skips, no key: allowed since a6) ...
    monkeypatch.setattr(ev, "expand_query", _fake_expand)
    art = tmp_path / "exp.json"
    ev.run_eval_matrix(
        [("golden", SAMPLE)], results_path=str(tmp_path / "a.md"), expansion=f"build:{art}",
        retrieve_fn_factory=FakeRetrieval({}).factory(6), provenance_fn=lambda: dict(PROVENANCE),
        privacy="public", skip_refusals=True, skip_completeness=True,
    )
    assert art.exists()
    # ... then replay it through the CLI on a keyed box: no model, no meter, no expansion call.
    _keyed_no_real_clients(monkeypatch)
    _no_meter_allowed(monkeypatch)
    calls = _forbid_expand_query(monkeypatch)
    code, c = _run_real(
        monkeypatch,
        ["--golden", SAMPLE, *BOTH_SKIPS, "--expansion", str(art), "-o", str(tmp_path / "b.md")],
    )
    assert code == 0 and calls == []
    assert c["kwargs"]["meter"] is None
    r, n = c["result"], _unique_questions(SAMPLE)
    assert r["generation_ran"] is False and r["expansion_enabled"] is True
    assert (r["rewrite_attempts"], r["rewrite_replayed"], r["rewrite_live"]) == (n, n, 0)
    assert r["expansion_identity"]["kind"] == "replay" and r["is_canonical"] is False


@pytest.mark.parametrize("expansion_args", [[], ["--expansion", "live"]])
def test_both_skips_alone_make_zero_calls(monkeypatch, tmp_path, expansion_args):
    _keyed_no_real_clients(monkeypatch)
    _no_meter_allowed(monkeypatch)
    calls = _forbid_expand_query(monkeypatch)
    code, c = _run_real(
        monkeypatch, ["--golden", SAMPLE, *BOTH_SKIPS, *expansion_args, "-o", str(tmp_path / "r.md")]
    )
    assert code == 0 and calls == []
    assert c["kwargs"]["meter"] is None
    r = c["result"]
    assert r["expansion_enabled"] is False and r["generation_ran"] is False
    assert r["rewrite_attempts"] == 0 and r["expansion_identity"]["kind"] == "disabled"


def _write_set(path, questions):
    path.write_text(
        "".join(
            json.dumps({"question": q, "type": "direct", "expected_sections": ["91.1"]}) + "\n"
            for q in questions
        )
    )
    return str(path)


def test_regression_adds_a_fourth_set(monkeypatch, tmp_path):
    g, h, r, x = (_set(tmp_path / f"{n}.jsonl") for n in ("g", "h", "r", "x"))
    code, c = _run(monkeypatch, ["--golden", g, "--heldout", h, "--realistic", r, "--regression", x, *BOTH_SKIPS])
    assert code == 0
    assert c["set_specs"] == [("golden", g), ("held-out", h), ("realistic", r), ("regression", x)]
    # Without the flag there is no fourth set.
    code, c = _run(monkeypatch, ["--golden", g, *BOTH_SKIPS])
    assert [label for label, _p in c["set_specs"]] == ["golden"]


def test_regression_set_joins_the_privacy_floor(monkeypatch, tmp_path, eval_registry):
    x = _set(tmp_path / "x.jsonl")
    # Everything else public; the unregistered regression set pulls the class to private ...
    code, c = _run(monkeypatch, ["--regression", x, *BOTH_SKIPS])
    assert code == 0 and c["kwargs"]["privacy"] == "private"
    # ... and registered public it stays public.
    eval_registry.add(x)
    code, c = _run(monkeypatch, ["--regression", x, *BOTH_SKIPS])
    assert code == 0 and c["kwargs"]["privacy"] == "public"
    # A sealed regression set is refused with exit 4 before the runner.
    code, c = _run(monkeypatch, ["--regression", _set(tmp_path / "s.jsonl", sealed=True), *BOTH_SKIPS])
    assert code == 4 and c == {}


def test_regression_set_reaches_the_report_and_the_cohort_sidecar(monkeypatch, tmp_path, eval_registry):
    paths = {
        flag: _write_set(tmp_path / f"{flag}.jsonl", [f"synthetic {flag} question {i}" for i in range(3)])
        for flag in ("golden", "heldout", "realistic", "regression")
    }
    for p in paths.values():
        eval_registry.add(p)
    report = tmp_path / "r.md"
    code, c = _run_real(
        monkeypatch,
        ["--golden", paths["golden"], "--heldout", paths["heldout"], "--realistic", paths["realistic"],
         "--regression", paths["regression"], *BOTH_SKIPS, "-o", str(report)],
    )
    assert code == 0
    result = c["result"]
    assert [s["label"] for s in result["sets"]] == ["golden", "held-out", "realistic", "regression"]
    text = report.read_text()
    assert "## regression — retrieval ablation" in text and "- regression: supplementary set" in text
    reg = result["sets"][3]
    assert reg["path"] == paths["regression"] and reg["sha256"] in text
    # C4: every set carries its cohort block, and the sidecar lists all four.
    sidecar = json.loads(Path(str(report) + ".rows.json").read_text())
    assert [co["sha256"] for co in sidecar["cohorts"]] == [s["sha256"] for s in result["sets"]]
    assert len(sidecar["cohorts"]) == len(sidecar["inputs"]) == 4
    assert sidecar["privacy"] == "public"


def test_every_run_sidecar_carries_the_pipeline_config_hash_or_none_without_an_index(monkeypatch, tmp_path, eval_registry):
    """16A-2b b1a r4 A: the sidecar names the pipeline that produced the draw (what ``controls.py`` reads)."""
    from src import pipeline_config

    paths = {flag: _write_set(tmp_path / f"{flag}.jsonl", [f"synthetic {flag} question {i}" for i in range(3)])
             for flag in ("golden", "heldout")}
    for p in paths.values():
        eval_registry.add(p)

    def sidecar_of(name):
        report = tmp_path / f"{name}.md"
        code, _c = _run_real(monkeypatch, ["--golden", paths["golden"], "--heldout", paths["heldout"], *BOTH_SKIPS, "-o", str(report)])
        assert code == 0
        return json.loads(Path(str(report) + ".rows.json").read_text())

    assert sidecar_of("noindex")["pipeline_config_hash"] is None  # the suite's stand-in for "no index at the directory"
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: "1" * 64)
    with_index = sidecar_of("index")["pipeline_config_hash"]
    assert with_index == pipeline_config.pipeline_config_hash("anywhere", top_k=6) and len(with_index) == 64


def test_repeated_question_in_the_regression_set_is_refused_before_any_call(monkeypatch, tmp_path, eval_registry):
    from src.eval_cohort import CohortError

    dup = _write_set(tmp_path / "dup.jsonl", ["synthetic repeated question"] * 2)
    eval_registry.add(dup)
    calls = _forbid_expand_query(monkeypatch)
    with pytest.raises(CohortError):
        _run_real(monkeypatch, ["--golden", SAMPLE, "--regression", dup, *BOTH_SKIPS, "-o", str(tmp_path / "r.md")])
    assert calls == []


# --- 16A-2a gate fix A7: no run but a sealed slot writes eval/results_v6.md ------------------------------------


def test_the_canonical_sealed_report_is_refused_as_a_results_path_however_it_is_spelled(monkeypatch, tmp_path):
    import os

    from src import sealed_run

    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval").mkdir()
    os.symlink(tmp_path / "eval" / "results_v6.md", tmp_path / "alias.md")  # a dangling alias of the report
    spellings = [
        "eval/results_v6.md", "./eval/../eval/results_v6.md", str(tmp_path / "eval" / "results_v6.md"),
        str(tmp_path / "alias.md"), str(sealed_run.results_v6_path()),
    ]
    for canonical in (False, True):  # even a run that is otherwise canonical cannot take it
        for spelling in spellings:
            with pytest.raises(ValueError, match="canonical sealed report"):
                ev._resolve_results_path(spelling, canonical, [])
    assert ev._resolve_results_path("eval/results_v6_other.md", False, [])[0] == "eval/results_v6_other.md"
    assert ev._resolve_results_path("eval/results_v6.md.bak", True, [])[0] == "eval/results_v6.md.bak"


def test_a_non_slot_run_with_o_pointing_at_results_v6_is_refused_before_any_work(monkeypatch, tmp_path):
    from src import sealed_run

    target = sealed_run.results_v6_path()  # under the suite's private-root fixture: eval/results_v6.md of the tmp tree
    calls = _forbid_expand_query(monkeypatch)
    with pytest.raises(ValueError, match="canonical sealed report"):
        _run_real(monkeypatch, ["--golden", SAMPLE, *BOTH_SKIPS, "-o", str(target)])
    assert not target.exists() and calls == []
    assert not Path(str(target) + ".rows.json").exists()


def test_run_eval_matrix_itself_refuses_results_v6_for_every_privacy_class(monkeypatch, tmp_path):
    from src import sealed_run

    target = str(sealed_run.results_v6_path())
    for privacy in ("public", "private"):
        with pytest.raises(ValueError, match="canonical sealed report"):
            ev.run_eval_matrix(
                [("golden", SAMPLE)], skip_refusals=True, skip_completeness=True, results_path=target,
                privacy=privacy, retrieve_fn_factory=FakeRetrieval({}).factory(6),
                provenance_fn=lambda: dict(PROVENANCE),
            )
        assert not Path(target).exists()
