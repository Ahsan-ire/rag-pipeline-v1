"""16A-2b b1a closure round r6, part B: development draws are recorded INSIDE the run (auditor M1, Codex 5, auditor m5).

``pipeline eval --record-draw`` checks every precondition before any call, makes the draw, writes its expansion artifact and rows
sidecar, appends the ``dev_draw`` ledger record (the run id is the execution receipt) and only THEN writes the report or prints
anything. A crash before the record leaves no countable draw. A run without ``--record-draw`` over a frozen tuning or realistic
version appends a ``dev_run_unrecorded`` record. ``record_batch_declaration`` needs its ``draws_required``; an abandoned
configuration counts toward K and is never run.

Synthetic data only (``tests/p16_slot.py``); every model is faked and the network is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import src.evaluator as ev
import src.pipeline
from src import eval_privacy, pipeline_config, sealed_ledger, sealed_run
from src.sealed_ledger import DrawCoverageRefused, ProtocolError, SealedLedger
from tests.p16_capture import PROVENANCE
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    BOUND_PREREG_SHA,
    WORLD_INDEX,
    Crash,
    _langsmith_env_cache,
    bare,
    kinds,
    make_world,
    world,
)

D = "a" * 64
CONFIGS = [f"{n}" * 64 for n in "123456"]
OTHER_INDEX = "9" * 64


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _embargo_off(monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)


@pytest.fixture
def drawable(bare):
    """The synthetic world frozen (sealed and both development splits) but with NO controls record yet, and a prereg that
    carries ``draws_r``: the state in which the incumbent's draws are made."""
    bare.write_prereg()
    doc = json.loads(bare.prereg_path.read_text(encoding="utf-8"))
    doc["draws_r"] = 5
    bare.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
    bare.write_records()
    bare.freeze(controls=False)
    return bare


@pytest.fixture
def fakes(drawable, monkeypatch):
    """Every ``run_eval_matrix`` call of the CLI gets the world's fake retrieval and provenance (the rewrite is already faked
    by the world); the list records the calls, so a refusal before any call leaves it empty."""
    real = ev.run_eval_matrix
    retrieval = drawable.retrieval()
    calls = []

    def wrapper(set_specs, **kwargs):
        calls.append(kwargs)
        kwargs.setdefault("retrieve_fn_factory", retrieval.factory(6))
        kwargs.setdefault("provenance_fn", lambda: dict(PROVENANCE))
        return real(set_specs, **kwargs)

    monkeypatch.setattr(ev, "run_eval_matrix", wrapper)
    return calls


_counter = [0]


def argv_for(w, *, name=None, expansion=None, replay=None, drop=(), extra=()):
    """The ``--record-draw`` command line (a fresh artifact name each time); ``drop`` removes options by name."""
    _counter[0] += 1
    argv = [
        "--golden", str(w.golden_path), "--realistic", str(w.realistic_path), "--record-draw", "--prereg", str(w.prereg_path),
        "--skip-refusals", "--skip-completeness", "--expansion", expansion or f"build:{name or f'draw{_counter[0]}'}.json",
        "--top-k", "6", "--persist-dir", "unused",
    ]
    if replay is not None:
        argv += ["--replay-incumbent-draw", str(replay)]
    for option in drop:
        i = argv.index(option)
        del argv[i:i + (1 if option in ("--record-draw", "--skip-refusals", "--skip-completeness") else 2)]
    return [*argv, *extra]


def cli(monkeypatch, capsys, argv, *, key=True):
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


def draw_records(w, config=None):
    records = w.ledger().records("dev_draw")
    return [r for r in records if config is None or r["config_hash"] == config]


def run_dir_of(run_id):
    return eval_privacy.run_dir(run_id, create=False)


# --- the draw is recorded inside the run, before anything is printed or written -----------------------------------------------------
def test_a_draw_is_recorded_before_the_report_is_written_or_anything_is_printed(drawable, fakes, monkeypatch, capsys):
    seen = {}
    real = SealedLedger.record_dev_draw

    def spy(self, config, artifact, sidecar, receipt, **kwargs):
        run_dir = run_dir_of(receipt)
        seen.update(
            printed=capsys.readouterr().out, report=(run_dir / "report.md").exists(),
            sidecar=(run_dir / "report.md.rows.json").exists(), artifact=eval_privacy.artifact_path("first.json").exists(),
            sidecar_sha=file_sha(run_dir / "report.md.rows.json"), artifact_sha=file_sha(eval_privacy.artifact_path("first.json")),
            receipt=receipt, config=config,
        )
        return real(self, config, artifact, sidecar, receipt, **kwargs)

    monkeypatch.setattr(SealedLedger, "record_dev_draw", spy)
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable, name="first"))
    assert code == 0, err
    # at the moment of the record: the artifact and the sidecar exist, no report, and nothing had been printed
    assert seen["printed"] == "" and seen["report"] is False and seen["sidecar"] is True and seen["artifact"] is True
    [record] = draw_records(drawable)
    assert (record["config_hash"], record["draw_index"]) == (drawable.config_hash(), 1) == (seen["config"], 1)
    assert record["artifact_sha256"] == seen["artifact_sha"] and record["rows_sidecar_sha256"] == seen["sidecar_sha"]
    assert record["execution_receipt"] == seen["receipt"] and (run_dir_of(seen["receipt"]) / "report.md").is_file()
    lines = out.splitlines()
    recorded_at = next(i for i, line in enumerate(lines) if line.startswith("[eval] draw recorded:"))
    reported_at = next(i for i, line in enumerate(lines) if "private v6 run" in line)
    assert recorded_at < reported_at and f"index=1 draws=1 receipt={seen['receipt']}" in lines[recorded_at]
    sidecar = json.loads((run_dir_of(seen["receipt"]) / "report.md.rows.json").read_text(encoding="utf-8"))
    assert sidecar["pipeline_config_hash"] == record["config_hash"] and sidecar["expansion"]["kind"] == "live"


def test_each_draw_has_its_own_receipt_and_index_and_the_mirror_carries_digests_only(drawable, fakes, monkeypatch, capsys):
    for n in range(3):
        code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
        assert code == 0, err
    records = draw_records(drawable)
    assert [r["draw_index"] for r in records] == [1, 2, 3]
    assert len({r["execution_receipt"] for r in records}) == 3 and len({r["rows_sidecar_sha256"] for r in records}) == 3
    # the fake rewrite returns the same expansions every time, so the three artifacts are byte-identical: independent draws
    # that happen to agree are still three draws (Codex 5), told apart by the run id their sidecars carry
    assert len({r["artifact_sha256"] for r in records}) == 1
    # the committed mirror: digests only (no receipt, no index)
    mirror = [json.loads(line) for line in sealed_run.default_ledger()._mirror.read_text().splitlines()]
    last = [m for m in mirror if m["kind"] == "dev_draw"][-1]
    assert set(last) == {"seq", "kind", "digest", "digests"}
    assert set(last["digests"]) == {"config_hash", "artifact_sha256", "rows_sidecar_sha256"}


def test_a_crash_before_the_record_leaves_no_countable_draw_and_no_result(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(SealedLedger, "record_dev_draw", lambda self, *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        cli(monkeypatch, capsys, argv_for(drawable, name="crashed"))
    out = capsys.readouterr().out
    assert "private v6 run" not in out and "draw recorded" not in out
    assert draw_records(drawable) == [] and "dev_run_unrecorded" not in kinds(drawable)
    runs = list((drawable.private / "runs").iterdir())
    assert len(runs) == 1 and not (runs[0] / "report.md").exists()  # the sidecar may exist; the report and the record do not


def test_a_sidecar_that_is_not_the_planned_draw_is_not_recorded_and_nothing_is_reported(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(sealed_run, "validate_draw_sidecar", lambda *a, **k: (_ for _ in ()).throw(sealed_run.GateRefused("draw_sidecar")))
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_sidecar" in err and "nothing was recorded" in err
    assert draw_records(drawable) == [] and "private v6 run" not in out
    [run] = list((drawable.private / "runs").iterdir())
    assert not (run / "report.md").exists()


def test_a_ledger_refusal_at_the_record_is_the_same_clean_stop(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(SealedLedger, "record_dev_draw", lambda self, *a, **k: (_ for _ in ()).throw(ProtocolError("no")))
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_ledger" in err and "private v6 run" not in out


# --- validate_draw_sidecar ----------------------------------------------------------------------------------------------------------
def make_plan(w, mode="build", replay_of=None):
    ledger = w.ledger()
    return sealed_run.DrawPlan(
        config_hash=w.config_hash(), expansion_mode=mode, replay_of=replay_of,
        dev_sets={"tuning": file_sha(w.golden_path), "realistic": file_sha(w.realistic_path)},
        modes=tuple(ev.EVAL_MODES), prereg_sha256=BOUND_PREREG_SHA,
    )


def one_draw(drawable, fakes, monkeypatch, capsys):
    assert cli(monkeypatch, capsys, argv_for(drawable, name="v"))[0] == 0
    [record] = draw_records(drawable)
    run_dir = run_dir_of(record["execution_receipt"])
    sidecar = json.loads((run_dir / "report.md.rows.json").read_text(encoding="utf-8"))
    return sidecar, eval_privacy.artifact_path("v.json"), record


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(pipeline_config_hash="b" * 64),  # another pipeline
    lambda s: s.update(pipeline_config_hash=None),
    lambda s: s.update(cohorts=s["cohorts"][:1]),  # one split only
    lambda s: s["cohorts"][0].update(sha256="c" * 64),  # not a frozen version
    lambda s: s.update(rows=s["rows"][:-1]),  # a row of a mode is missing
    lambda s: s.update(rows=[r for r in s["rows"] if r["mode"] != "hybrid+rewrite"]),  # a whole mode is missing
    lambda s: s.update(rows=s["rows"] + [dict(s["rows"][0], id="invented")]),  # a row too many
    lambda s: s.update(rows=s["rows"] + [dict(s["rows"][0])]),  # a row twice
    lambda s: s["expansion"].update(kind="replay"),  # the wrong expansion kind
    lambda s: s["expansion"].update(config_hash="d" * 64),  # not this artifact's rewrite identity
    lambda s: s.update(run_id="run-of-another-draw"),  # the sidecar is another run's: the receipt is bound to it
    lambda s: s.pop("run_id"),
])
def test_validate_draw_sidecar_refuses_a_sidecar_that_is_not_the_planned_draw(drawable, fakes, monkeypatch, capsys, mutate):
    sidecar, artifact, record = one_draw(drawable, fakes, monkeypatch, capsys)
    plan = make_plan(drawable)
    receipt = record["execution_receipt"]
    sealed_run.validate_draw_sidecar(sidecar, plan=plan, artifact_path=artifact, artifact_sha256=record["artifact_sha256"], run_id=receipt)
    bad = json.loads(json.dumps(sidecar))
    mutate(bad)
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.validate_draw_sidecar(bad, plan=plan, artifact_path=artifact, artifact_sha256=record["artifact_sha256"], run_id=receipt)
    assert info.value.gate == "draw_sidecar"


def test_validate_draw_sidecar_refuses_another_artifact_or_a_missing_one(drawable, fakes, monkeypatch, capsys):
    sidecar, artifact, record = one_draw(drawable, fakes, monkeypatch, capsys)
    plan = make_plan(drawable)
    for path, digest in ((artifact, "e" * 64), (artifact.with_name("missing.json"), record["artifact_sha256"])):
        with pytest.raises(sealed_run.GateRefused):
            sealed_run.validate_draw_sidecar(sidecar, plan=plan, artifact_path=path, artifact_sha256=digest, run_id=record["execution_receipt"])
    # a replay plan needs the sidecar's replay digest to be the artifact's
    with pytest.raises(sealed_run.GateRefused):
        sealed_run.validate_draw_sidecar(sidecar, plan=make_plan(drawable, "replay", 1), artifact_path=artifact,
                                         artifact_sha256=record["artifact_sha256"], run_id=record["execution_receipt"])


# --- every precondition is refused before any call ---------------------------------------------------------------------------------------
def refused(monkeypatch, capsys, w, calls, argv, gate, code=4, key=True):
    before = [r["kind"] for r in w.ledger().records()]
    runs_before = sorted((w.private / "runs").iterdir()) if (w.private / "runs").exists() else []
    got, out, err = cli(monkeypatch, capsys, argv, key=key)
    assert got == code, (got, err)
    assert gate in err
    assert calls == []  # run_eval_matrix was never called: no retrieval, expansion or model call
    assert [r["kind"] for r in w.ledger().records()] == before  # and nothing was recorded or logged
    assert (sorted((w.private / "runs").iterdir()) if (w.private / "runs").exists() else []) == runs_before  # no run directory


def test_record_draw_needs_the_prereg(drawable, fakes, monkeypatch, capsys):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, drop=("--prereg",)), "gate=prereg")


def test_the_prereg_must_be_the_epochs_registered_one(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(sealed_run, "bind_prereg", lambda path: ("c" * 40, "f" * 64))
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=prereg_binding")


def test_the_prereg_must_carry_draws_r(drawable, fakes, monkeypatch, capsys):
    doc = json.loads(drawable.prereg_path.read_text(encoding="utf-8"))
    del doc["draws_r"]
    drawable.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=prereg")


def test_top_k_must_be_the_preregs_cutoff(drawable, fakes, monkeypatch, capsys):
    argv = argv_for(drawable)
    argv[argv.index("--top-k") + 1] = "3"
    refused(monkeypatch, capsys, drawable, fakes, argv, "gate=cutoff")


@pytest.mark.parametrize("extra", [["--heldout", "x"], ["--regression", "x"], ["--results", "report.md"]])
def test_the_inputs_are_the_frozen_development_versions_and_nothing_else(drawable, fakes, monkeypatch, capsys, extra):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, extra=extra), "gate=draw_inputs")


def test_one_set_two_files_of_one_split_a_foreign_or_a_changed_file_are_refused(drawable, fakes, monkeypatch, capsys, tmp_path):
    w = drawable
    refused(monkeypatch, capsys, w, fakes, argv_for(w, drop=("--realistic",)), "gate=draw_inputs")  # tuning only
    argv = argv_for(w)
    argv[argv.index("--realistic") + 1] = str(w.golden_path)  # the tuning version twice
    refused(monkeypatch, capsys, w, fakes, argv, "gate=dev_binding")
    other = w.root / "dev" / "other.jsonl"  # a private development file that is no freeze record's version
    other.write_text(w.realistic_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    argv = argv_for(w)
    argv[argv.index("--realistic") + 1] = str(other)
    refused(monkeypatch, capsys, w, fakes, argv, "gate=dev_binding")
    original = w.golden_path.read_text(encoding="utf-8")
    w.golden_path.write_text(original + "\n", encoding="utf-8")  # a changed file is not its frozen version
    try:
        refused(monkeypatch, capsys, w, fakes, argv_for(w), "gate=dev_binding")
    finally:
        w.golden_path.write_text(original, encoding="utf-8")


def test_a_sealed_set_is_refused_at_the_door(drawable, fakes, monkeypatch, capsys):
    argv = argv_for(drawable)
    argv[argv.index("--golden") + 1] = str(drawable.sealed_path)
    refused(monkeypatch, capsys, drawable, fakes, argv, "sealed input refused")


def test_the_expansion_must_be_a_build_or_the_incumbents_artifact(drawable, fakes, monkeypatch, capsys):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, expansion="live"), "gate=draw_expansion")
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, replay=1), "gate=draw_replay")  # a build with a replay flag


@pytest.mark.parametrize("mode", ["hybrid", "bm25", "vector", "hybrid+rewrite"])
def test_the_modes_must_include_hybrid_and_hybrid_plus_rewrite(drawable, fakes, monkeypatch, capsys, mode):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, extra=["--mode", mode]), "gate=draw_modes")


@pytest.mark.parametrize("drop,extra", [
    (("--skip-completeness",), ()), (("--skip-refusals",), ()), ((), ("--judge",)), ((), ("--judge-sample", "2")),
])
def test_a_draw_is_retrieval_only(drawable, fakes, monkeypatch, capsys, drop, extra):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, drop=drop, extra=extra), "gate=draw_retrieval_only")


def test_a_pipeline_without_an_index_identity_has_nothing_to_record(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: None)
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=draw_index")


def test_tracing_must_be_off_and_a_build_needs_a_meter(drawable, fakes, monkeypatch, capsys):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=meter", key=False)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=tracing")


def test_the_ledgers_cap_and_the_controls_rule_are_checked_before_the_run(drawable, fakes, monkeypatch, capsys):
    ledger = drawable.ledger()
    ledger.record_dev_draw(CONFIGS[0], sha("a"), sha("s"), "run-first")  # some other configuration is the first recorded
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable), "gate=draw_ledger")  # a second one before the controls


def test_record_draw_and_sealed_slot_are_different_runs_and_the_replay_flag_needs_record_draw(drawable, fakes, monkeypatch, capsys):
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, extra=["--sealed-slot", "0"]), "different runs", code=2)
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, drop=("--record-draw",), extra=["--replay-incumbent-draw", "1"]),
            "needs --record-draw", code=2)


# --- a replay candidate's draw ---------------------------------------------------------------------------------------------------------------
def test_a_replay_candidate_replays_the_incumbents_draw_with_the_same_index(drawable, fakes, monkeypatch, capsys):
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    w = drawable
    draw_no = [0]
    monkeypatch.setattr(ev, "expand_query", lambda question, *a, **k: Expansion(
        question, (f"rewrite of draw {draw_no[0]}",), REWRITE_MODEL, STATUS_LIVE, f"intent of draw {draw_no[0]}"))
    for name in ("inc1", "inc2"):  # two independent live draws that return different expansions: two different artifacts
        draw_no[0] += 1
        code, out, err = cli(monkeypatch, capsys, argv_for(w, name=name))
        assert code == 0, err
    incumbent = w.config_hash()
    assert [r["draw_index"] for r in draw_records(w, incumbent)] == [1, 2]
    art1, art2 = (eval_privacy.artifact_path(n + ".json") for n in ("inc1", "inc2"))
    w.record_controls()
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)  # the candidate's pipeline
    candidate = w.config_hash()
    assert candidate != incumbent
    fakes.clear()
    for argv, why in (
        (argv_for(w, expansion=str(art1)), "no flag"), (argv_for(w, expansion=str(art1), replay=0), "zero"),
        (argv_for(w, expansion=str(art1), replay=6), "past draws_r"), (argv_for(w, expansion=str(art1), replay=3), "no such draw"),
        (argv_for(w, expansion=str(art2), replay=1), "the wrong artifact for that draw"),
        (argv_for(w, expansion=str(art2), replay=2), "not this candidate's next index"),  # its first draw must replay draw 1
    ):
        before = len(w.ledger().records())
        code, out, err = cli(monkeypatch, capsys, argv)
        assert code == 4 and "gate=draw_replay" in err and fakes == [] and len(w.ledger().records()) == before, why
    code, out, err = cli(monkeypatch, capsys, argv_for(w, expansion=str(art1), replay=1), key=False)  # a replay needs no meter or key
    assert code == 0, err
    code, out, err = cli(monkeypatch, capsys, argv_for(w, expansion=str(art2), replay=2), key=False)
    assert code == 0, err
    records = draw_records(w, candidate)
    assert [(r["draw_index"], r["replay_of"], r["artifact_sha256"]) for r in records] == [
        (1, 1, file_sha(art1)), (2, 2, file_sha(art2))]
    assert len({r["execution_receipt"] for r in records}) == 2
    sidecar = json.loads((run_dir_of(records[0]["execution_receipt"]) / "report.md.rows.json").read_text(encoding="utf-8"))
    assert sidecar["expansion"]["kind"] == "replay" and sidecar["expansion"]["digest"] == file_sha(art1)
    assert sidecar["pipeline_config_hash"] == candidate


def test_a_replay_needs_the_controls_record_so_that_the_incumbent_is_known(drawable, fakes, monkeypatch, capsys):
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable, name="only"))
    assert code == 0, err
    fakes.clear()
    refused(monkeypatch, capsys, drawable, fakes, argv_for(drawable, expansion=str(eval_privacy.artifact_path("only.json")), replay=1),
            "gate=draw_replay")


# --- the ledger records -------------------------------------------------------------------------------------------------------------------------
def make(tmp_path) -> SealedLedger:
    return SealedLedger(tmp_path / "copy", mirror=tmp_path / "mirror.jsonl")


def isolated(path) -> SealedLedger:
    """A ledger of its own (its own out-of-tree store, not the test's shared one), frozen and with the controls recorded."""
    led = SealedLedger(path / "copy", mirror=path / "mirror.jsonl", ledger=path / "ledger.jsonl")
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=CONFIGS[0])
    return led


def with_controls(tmp_path, incumbent=CONFIGS[0]) -> SealedLedger:
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=incumbent)
    return led


def draw(led, config, n, receipt=None):
    return led.record_dev_draw(config, sha(f"artifact {config} {n}"), sha(f"sidecar {config} {n}"), receipt or f"run-{config[:4]}-{n}")


def test_the_execution_receipt_is_required_distinct_and_an_id(tmp_path):
    led = with_controls(tmp_path)
    with pytest.raises(TypeError):
        led.record_dev_draw(CONFIGS[0], D, D)  # no receipt: not a draw
    draw(led, CONFIGS[0], 0, "run-one")
    with pytest.raises(ProtocolError, match="receipt"):
        led.record_dev_draw(CONFIGS[0], sha("x"), sha("y"), "run-one")  # one run is one draw
    with pytest.raises(ProtocolError, match="sidecar"):
        led.record_dev_draw(CONFIGS[0], sha("x"), sha("sidecar " + CONFIGS[0] + " 0"), "run-two")  # a copy of a sidecar
    for bad in ("", "ab", "has space", "x" * 65, None, 5, "slash/id"):
        with pytest.raises(ValueError):
            led.record_dev_draw(CONFIGS[0], sha("p"), sha("q"), bad)
    assert len(led.dev_draws(CONFIGS[0])) == 1 and led.dev_draws(CONFIGS[0])[0]["execution_receipt"] == "run-one"


def test_check_dev_draw_config_is_the_caps_and_controls_rule_without_recording(tmp_path):
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.check_dev_draw_config(CONFIGS[0])
    draw(led, CONFIGS[0], 0)
    led.check_dev_draw_config(CONFIGS[0])  # the recorded configuration goes on
    with pytest.raises(ProtocolError, match="controls"):
        led.check_dev_draw_config(CONFIGS[1])
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=CONFIGS[0])
    led.check_dev_draw_config(CONFIGS[1])
    for config in CONFIGS[1:5]:
        draw(led, config, 0)
    with pytest.raises(ProtocolError, match="at most 5"):
        led.check_dev_draw_config(CONFIGS[5])
    assert len(led.records("dev_draw")) == 5  # checking recorded nothing


def test_replay_of_is_checked_in_the_ledger_transaction(tmp_path):
    led = with_controls(tmp_path)
    for n in range(3):
        draw(led, CONFIGS[0], n)
    art = lambda n: sha(f"artifact {CONFIGS[0]} {n}")  # noqa: E731
    cand = CONFIGS[1]
    with pytest.raises(ProtocolError, match="not the incumbent draw's artifact"):
        led.record_dev_draw(cand, art(1), sha("c0"), "run-c0", replay_of=1)  # draw 1 is index 0's artifact
    with pytest.raises(ProtocolError, match="no such draw"):
        led.record_dev_draw(cand, art(0), sha("c0"), "run-c0", replay_of=4)
    with pytest.raises(ProtocolError, match="index"):
        led.record_dev_draw(cand, art(1), sha("c0"), "run-c0", replay_of=2)  # its first draw can only replay draw 1
    for bad in (0, -1, True, "1", 1.0):
        with pytest.raises(ValueError):
            led.record_dev_draw(cand, art(0), sha("c0"), "run-c0", replay_of=bad)
    assert led.record_dev_draw(cand, art(0), sha("c0"), "run-c0", replay_of=1) == 1
    assert led.record_dev_draw(cand, art(1), sha("c1"), "run-c1", replay_of=2) == 2
    assert [r["replay_of"] for r in led.dev_draws(cand)] == [1, 2]
    assert all("replay_of" not in r for r in led.dev_draws(CONFIGS[0]))
    (tmp_path / "x").mkdir()
    no_controls = SealedLedger(tmp_path / "x" / "copy", mirror=tmp_path / "x" / "mirror.jsonl", ledger=tmp_path / "x" / "ledger.jsonl")
    with pytest.raises(ProtocolError, match="controls"):
        no_controls.record_dev_draw(cand, D, D, "run-z", replay_of=1)


def test_unrecorded_runs_are_logged_counted_per_configuration_and_mirrored_as_a_digest_only(tmp_path):
    led = with_controls(tmp_path)
    assert led.dev_unrecorded_counts() == {} and led.exists()
    led.record_dev_run_unrecorded(CONFIGS[0], "run-a")
    led.record_dev_run_unrecorded(CONFIGS[0], "run-b")
    led.record_dev_run_unrecorded(CONFIGS[1], "run-c")
    led.record_dev_run_unrecorded(None, "run-d")  # a run with no index identity
    assert led.dev_unrecorded_counts() == {CONFIGS[0]: 2, CONFIGS[1]: 1, "none": 1}
    line = [json.loads(x) for x in (tmp_path / "mirror.jsonl").read_text().splitlines() if '"dev_run_unrecorded"' in x][0]
    assert line["digests"] == {"config_hash": CONFIGS[0]} and set(line) == {"seq", "kind", "digest", "digests"}
    for bad in ("short", 5):
        with pytest.raises(ValueError):
            led.record_dev_run_unrecorded(bad, "run-e")
    with pytest.raises(ValueError):
        led.record_dev_run_unrecorded(CONFIGS[0], "bad id")
    assert led.dev_draw_configs() == [] and led.records("dev_draw") == []  # an unrecorded run is not a draw


def test_a_ledger_that_does_not_exist_is_not_created_by_asking(tmp_path):
    led = make(tmp_path)
    assert led.exists() is False
    assert not (tmp_path / "copy").exists()


# --- an unrecorded run over a frozen version is logged before it prints -----------------------------------------------------------------------------
def offline_run(w, specs=None, **kwargs):
    retrieval = w.retrieval()
    return ev.run_eval_matrix(
        specs or [("tuning", str(w.golden_path)), ("realistic", str(w.realistic_path))], modes=["hybrid", "hybrid+rewrite"], top_k=6,
        skip_refusals=True, skip_completeness=True, persist_directory="unused", privacy="private",
        retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE), **kwargs,
    )


def test_a_run_over_a_frozen_version_without_record_draw_is_logged_before_its_report(drawable, monkeypatch, capsys):
    w = drawable
    seen = {}
    real = ev.write_private

    def spy(path, content):
        if Path(path).name == "report.md":
            seen["logged_before_report"] = [r["run_id"] for r in w.ledger().records("dev_run_unrecorded")]
            seen["printed_before_report"] = capsys.readouterr().out
        return real(path, content)

    monkeypatch.setattr(ev, "write_private", spy)
    result = offline_run(w)
    assert seen["logged_before_report"] == [result["run_id"]] and seen["printed_before_report"] == ""
    [record] = w.ledger().records("dev_run_unrecorded")
    assert (record["config_hash"], record["run_id"]) == (w.config_hash(), result["run_id"])
    assert w.ledger().dev_unrecorded_counts() == {w.config_hash(): 1} and draw_records(w) == []
    offline_run(w)
    assert w.ledger().dev_unrecorded_counts() == {w.config_hash(): 2}


def test_a_run_over_one_frozen_version_among_others_is_logged_and_a_run_over_other_sets_is_not(drawable, monkeypatch):
    w = drawable
    other = w.root / "dev" / "scratch.jsonl"
    rows = [json.loads(line) for line in w.golden_path.read_text(encoding="utf-8").splitlines()]
    other.write_text("".join(json.dumps({**r, "id": r["id"].replace("f2", "f9", 1), "family_id": r["family_id"].replace("f2", "f9", 1)}) + "\n"
                             for r in rows), encoding="utf-8")
    offline_run(w, [("scratch", str(other))])
    assert w.ledger().records("dev_run_unrecorded") == []  # no frozen version among the inputs
    offline_run(w, [("scratch", str(other)), ("tuning", str(w.golden_path))])
    assert len(w.ledger().records("dev_run_unrecorded")) == 1


def test_a_run_before_the_epoch_touches_no_ledger(bare, monkeypatch):
    path = Path(os.environ["CC_SEALED_LEDGER"])
    offline_run(bare)
    assert not path.exists()  # asking whether the epoch has frozen versions did not create its ledger


def test_a_sealed_slot_run_is_not_logged_as_an_unrecorded_development_run(world):
    world.run(0)
    assert "dev_run_unrecorded" not in kinds(world)


def test_a_ledger_stop_while_logging_is_a_defined_exit_and_prints_no_report(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(SealedLedger, "record_dev_run_unrecorded", lambda self, *a, **k: (_ for _ in ()).throw(sealed_ledger.LedgerCorrupt("x")))
    argv = ["--golden", str(drawable.golden_path), "--realistic", str(drawable.realistic_path), "--skip-refusals", "--skip-completeness",
            "--persist-dir", "unused"]
    code, out, err = cli(monkeypatch, capsys, argv)
    assert code == 8 and "ledger stop" in err and "private v6 run" not in out


# --- the batch declaration: draws_required, abandoned entries ----------------------------------------------------------------------------------------
def entries(*configs, mode="replay"):
    return [{"config_hash": c, "expansion": mode} for c in configs]


def test_draws_required_is_a_required_argument_and_the_coverage_rules_always_apply(tmp_path):
    led = with_controls(tmp_path)
    with pytest.raises(TypeError):
        led.record_batch_declaration(entries(CONFIGS[1]))  # the r5 bypass: omitting it skipped both checks
    for bad in (0, -1, True, None, "5", 1.5):
        with pytest.raises(ValueError):
            led.record_batch_declaration(entries(CONFIGS[1]), draws_required=bad)
    with pytest.raises(DrawCoverageRefused, match="fewer recorded"):
        led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)  # an unrecorded candidate
    assert led.batch_declaration() is None


def test_an_omitted_recorded_candidate_is_refused_whatever_the_caller_passes(tmp_path):
    led = with_controls(tmp_path)
    for config in (CONFIGS[1], CONFIGS[2]):
        for n in range(5):
            draw(led, config, n)
    with pytest.raises(DrawCoverageRefused, match="not in the batch"):
        led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
    led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[2]), draws_required=5)


def test_an_abandoned_entry_needs_some_but_fewer_than_the_required_draws_and_counts_toward_k(tmp_path):
    led = with_controls(tmp_path)
    for n in range(5):
        draw(led, CONFIGS[1], n)
    for n in range(2):
        draw(led, CONFIGS[2], n)
    for n in range(5):
        draw(led, CONFIGS[3], n)
    full = {"config_hash": CONFIGS[1], "expansion": "abandoned"}  # all its draws are there: it is not abandoned
    with pytest.raises(DrawCoverageRefused, match="abandoned"):
        led.record_batch_declaration([full, {"config_hash": CONFIGS[2], "expansion": "abandoned"}, *entries(CONFIGS[3])], draws_required=5)
    with pytest.raises(DrawCoverageRefused, match="abandoned"):  # an abandoned entry with no draw at all
        led.record_batch_declaration([*entries(CONFIGS[1], CONFIGS[3]), {"config_hash": CONFIGS[4], "expansion": "abandoned"}], draws_required=5)
    with pytest.raises(DrawCoverageRefused, match="not in the batch"):  # leaving the partly drawn one out is refused too
        led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[3]), draws_required=5)
    with pytest.raises(ValueError):  # an abandoned entry carries nothing else
        led.record_batch_declaration([{"config_hash": CONFIGS[2], "expansion": "abandoned", "rewrite_config_hash": D}], draws_required=5)
    digest = led.record_batch_declaration(
        [*entries(CONFIGS[1]), {"config_hash": CONFIGS[2], "expansion": "abandoned"}, *entries(CONFIGS[3])], draws_required=5)
    assert len(digest) == 64
    assert [c["expansion"] for c in led.batch_declaration()["candidates"]] == ["replay", "abandoned", "replay"]
    # K = 4 at most, abandoned or not
    (tmp_path / "k").mkdir()
    other = isolated(tmp_path / "k")
    with pytest.raises(ValueError, match="1 to 4"):
        other.record_batch_declaration([{"config_hash": c, "expansion": "abandoned"} for c in CONFIGS[1:6]], draws_required=5)


def test_the_slot_gate_never_runs_an_abandoned_entry(world):
    # slot 1's batch entry is abandoned: draws recorded for its configuration, fewer than draws_r
    ledger = world.ledger()
    world.record_controls()
    for n in range(2):
        draw(ledger, CONFIGS[0], n)
    ledger.record_batch_declaration([{"config_hash": CONFIGS[0], "expansion": "abandoned"}], draws_required=5)
    with pytest.raises(sealed_run.GateRefused) as info:
        world.token(1, declare=False, claim=False)
    assert (info.value.gate, info.value.detail) == ("batch", "abandoned")
