"""16A-2b b1a closure round r7, T3-T5: the draw recorder is verified, failures are logged, the pipeline hash is taken at both ends,
batch modes are bound to draw modes, a draw with a rewrite fallback is void, and the draws carry the scorer identity.

T3  a ``dev_draw`` carries the sidecar's ``scorer_identity``; ``record_batch_declaration`` refuses a candidate whose counted draws'
    identity is not the incumbent's counted draws'.
T4  after the recorder returns, the run checks the ledger holds the draw (a no-op recorder is refused, nothing is released); a
    ``--record-draw`` run that fails or is refused at or after the sidecar write leaves ``dev_run_unrecorded`` (``record_failed``);
    the pipeline hash is compared at the start of a run and again right before the record (draws) or the storage (slots).
T5  a ``dev_draw`` stores its mode, ``replay_of`` and rewrite identity; one configuration's draws are all one mode; a replay entry's
    counted draws are replay draws, a candidate entry's are build draws under a rewrite identity that is its own and not the
    incumbent's; a draw with a rewrite fallback is voided (``dev_draw_void``), uses no index and releases nothing.

Synthetic data only (``tests/p16_slot.py``); every model is faked and the network is never touched.
"""

from __future__ import annotations

import json

import pytest

import src.evaluator as ev
from src import eval_privacy, pipeline_config, scoring_identity, sealed_ledger, sealed_run
from src.query_rewrite import REWRITE_MODEL, STATUS_API_ERROR, STATUS_LIVE, Expansion
from src.sealed_ledger import DrawBindingRefused, DrawCoverageRefused, LedgerCorrupt, ProtocolError, RecoveryAction, SealedLedger
from tests.p16_capture import PROVENANCE
from tests.p16_draws import REWRITE, SCORER, declare, draw_ids, record_draws
from tests.p16_slot import Crash, _langsmith_env_cache, bare, kinds, world  # noqa: F401 - the fixtures are used by name
from tests.test_p16a2b_b1a_r6_draws import (  # noqa: F401 - fixtures and helpers shared with the r6 draw tests
    CONFIGS,
    OTHER_INDEX,
    isolated,
    argv_for,
    cli,
    draw_records,
    drawable,
    entries,
    fakes,
    file_sha,
    make_plan,
    run_dir_of,
    sha,
    with_controls,
)

D = "a" * 64


@pytest.fixture(autouse=True)
def _embargo_off(monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)


def sidecar_of(record):
    return json.loads((run_dir_of(record["execution_receipt"]) / "report.md.rows.json").read_text(encoding="utf-8"))


# =================================================================================================================================
# T4: the recorder is verified
# =================================================================================================================================
def direct(w, record_draw, name="direct", **extra):
    """One private retrieval-only v6 run of the world's development sets with ``record_draw`` and a live (faked) expansion."""
    retrieval = w.retrieval()
    return ev.run_eval_matrix(
        [("tuning", str(w.golden_path)), ("realistic", str(w.realistic_path))], modes=list(ev.EVAL_MODES), top_k=6,
        skip_refusals=True, skip_completeness=True, persist_directory="unused", privacy="private",
        retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE), expansion=f"build:{name}.json",
        record_draw=record_draw, **extra,
    )


def recorder_for(w):
    return sealed_run.DrawRecorder(ledger=w.ledger(), plan=make_plan(w), persist_directory="unused", top_k=6)


def assert_nothing_released(w, capsys):
    assert capsys.readouterr().out == ""  # nothing printed
    runs = list((w.private / "runs").iterdir())
    assert len(runs) == 1 and not (runs[0] / "report.md").exists()  # the sidecar may exist; the report does not
    return runs[0]


def test_a_recorder_that_does_nothing_is_refused_and_nothing_is_released(drawable, capsys):
    calls = []
    with pytest.raises(sealed_run.GateRefused) as info:
        direct(drawable, lambda **kwargs: calls.append(kwargs["run_id"]))  # satisfies the old branch: returned, recorded nothing
    assert info.value.gate == "draw_ledger" and len(calls) == 1
    run = assert_nothing_released(drawable, capsys)
    assert draw_records(drawable) == []
    [logged] = drawable.ledger().records("dev_run_unrecorded")  # the run is visible: neither record nor silence
    assert (logged["run_id"], logged["config_hash"], logged["reason"]) == (run.name, drawable.config_hash(), "record_failed")


def test_a_recorder_that_records_another_draw_is_refused_too(drawable, capsys):
    def other(*, run_id, artifact_path, artifact_sha256, sidecar_path, sidecar_sha256, sidecar):
        drawable.ledger().record_dev_draw(sidecar["pipeline_config_hash"], "c" * 64, sidecar_sha256, run_id, **draw_ids())  # not this artifact

    with pytest.raises(sealed_run.GateRefused) as info:
        direct(drawable, other)
    assert info.value.gate == "draw_ledger"
    assert_nothing_released(drawable, capsys)
    assert len(draw_records(drawable)) == 1 and len(drawable.ledger().records("dev_run_unrecorded")) == 1  # a record, but not this run's


def test_a_recorder_that_records_under_another_receipt_is_refused(drawable, capsys):
    def other(*, run_id, artifact_path, artifact_sha256, sidecar_path, sidecar_sha256, sidecar):
        drawable.ledger().record_dev_draw(
            sidecar["pipeline_config_hash"], artifact_sha256, sidecar_sha256, "run-of-another", **draw_ids())

    with pytest.raises(sealed_run.GateRefused) as info:
        direct(drawable, other)
    assert info.value.gate == "draw_ledger"
    assert_nothing_released(drawable, capsys)


def test_a_recorder_that_records_the_draw_is_believed_and_the_run_goes_on(drawable, capsys):
    result = direct(drawable, recorder_for(drawable))
    out = capsys.readouterr().out
    assert result["run_id"] and "draw recorded:" in out and "private v6 run" in out
    [record] = draw_records(drawable)
    assert record["execution_receipt"] == result["run_id"]
    assert drawable.ledger().records("dev_run_unrecorded") == []  # a recorded draw leaves no unrecorded record


def test_a_ledger_that_does_not_exist_is_not_a_record(bare, capsys):
    """The verification never believes a callback when the epoch's ledger is not there."""
    assert not bare.ledger().exists()
    with pytest.raises(sealed_run.GateRefused) as info:
        direct(bare, lambda **kwargs: None)
    assert info.value.gate == "draw_ledger"


# --- a failed or refused record is logged --------------------------------------------------------------------------------------------
def test_a_crash_at_the_record_is_logged_as_unrecorded_with_its_reason(drawable, capsys):
    def crash(**kwargs):
        raise Crash()

    with pytest.raises(Crash):
        direct(drawable, crash)
    run = assert_nothing_released(drawable, capsys)
    [logged] = drawable.ledger().records("dev_run_unrecorded")
    assert (logged["run_id"], logged["reason"]) == (run.name, "record_failed")
    assert drawable.ledger().dev_unrecorded_counts() == {drawable.config_hash(): 1} and draw_records(drawable) == []


def test_a_refused_sidecar_validation_is_logged_through_the_cli(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(sealed_run, "validate_draw_sidecar", lambda *a, **k: (_ for _ in ()).throw(sealed_run.GateRefused("draw_sidecar")))
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_sidecar" in err and "nothing was recorded" in err and "private v6 run" not in out
    [logged] = drawable.ledger().records("dev_run_unrecorded")
    assert logged["reason"] == "record_failed" and drawable.ledger().records("dev_draw_void") == [] and draw_records(drawable) == []


def test_a_ledger_refusal_at_the_record_is_logged_too(drawable, fakes, monkeypatch, capsys):
    monkeypatch.setattr(SealedLedger, "record_dev_draw", lambda self, *a, **k: (_ for _ in ()).throw(ProtocolError("no")))
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_ledger" in err
    assert [r["reason"] for r in drawable.ledger().records("dev_run_unrecorded")] == ["record_failed"]


def test_a_failure_before_the_sidecar_write_leaves_no_trace(drawable, capsys):
    """Nothing existed to choose from yet: a retrieval that dies is not an unrecorded draw."""
    retrieval = drawable.retrieval()

    def dying(mode):
        raise Crash()

    with pytest.raises(Crash):
        ev.run_eval_matrix(
            [("tuning", str(drawable.golden_path)), ("realistic", str(drawable.realistic_path))], modes=["hybrid", "hybrid+rewrite"],
            top_k=6, skip_refusals=True, skip_completeness=True, persist_directory="unused", privacy="private",
            retrieve_fn_factory=dying, provenance_fn=lambda: dict(PROVENANCE), expansion="build:early.json",
            record_draw=recorder_for(drawable))
    assert drawable.ledger().records("dev_run_unrecorded") == [] and draw_records(drawable) == []


def test_logging_the_failure_is_best_effort_and_never_hides_the_failure_itself(drawable, monkeypatch, capsys):
    def crash(**kwargs):
        raise Crash()

    monkeypatch.setattr(SealedLedger, "record_dev_run_unrecorded", lambda self, *a, **k: (_ for _ in ()).throw(LedgerCorrupt("x")))
    with pytest.raises(Crash):  # not LedgerCorrupt: the original failure is the one the caller sees
        direct(drawable, crash)
    assert capsys.readouterr().out == ""


def test_a_run_without_a_recorder_is_still_logged_without_a_reason(drawable):
    from tests.test_p16a2b_b1a_r6_draws import offline_run

    offline_run(drawable)
    [logged] = drawable.ledger().records("dev_run_unrecorded")
    assert "reason" not in logged  # r6's record, unchanged


def test_the_unrecorded_record_takes_only_its_registered_reason(tmp_path):
    led = with_controls(tmp_path)
    led.record_dev_run_unrecorded(CONFIGS[0], "run-a", "record_failed")
    for bad in ("", "other", 5):
        with pytest.raises(ValueError):
            led.record_dev_run_unrecorded(CONFIGS[0], "run-b", bad)
    assert [r.get("reason") for r in led.records("dev_run_unrecorded")] == ["record_failed"]
    assert sealed_ledger.UNRECORDED_REASONS == ("record_failed",)


# --- the pipeline hash at both ends -----------------------------------------------------------------------------------------------------
def edited_pipeline(monkeypatch):
    """A source edit made mid-run: the pipeline hash is no longer the one the run started with."""
    monkeypatch.setattr(pipeline_config, "endpoint_identity", lambda: {"anthropic_base_url": "https://edited-mid-run.example"})


def test_a_source_edit_made_while_a_draw_was_running_is_refused_right_before_the_record(drawable, fakes, monkeypatch, capsys):
    real_validate = sealed_run.validate_draw_sidecar

    def validate_then_edit(*args, **kwargs):
        real_validate(*args, **kwargs)  # the sidecar was built before the edit: it still carries the plan's hash and passes
        edited_pipeline(monkeypatch)

    monkeypatch.setattr(sealed_run, "validate_draw_sidecar", validate_then_edit)
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=incumbent_changed_during_run" in err and "nothing was recorded" in err
    assert draw_records(drawable) == [] and "private v6 run" not in out and "draw recorded" not in out
    assert [r["reason"] for r in drawable.ledger().records("dev_run_unrecorded")] == ["record_failed"]


def test_a_slot_run_whose_pipeline_changed_during_the_run_stores_nothing(world, monkeypatch):
    real = ev.run_eval_matrix

    def run_then_edit(*args, **kwargs):
        out = real(*args, **kwargs)
        if not kwargs.get("preflight_only"):
            edited_pipeline(monkeypatch)
        return out

    monkeypatch.setattr(ev, "run_eval_matrix", run_then_edit)
    with pytest.raises(sealed_run.GateRefused) as info:
        world.run(0)
    assert info.value.gate == "incumbent_changed_during_run"
    assert world.ledger().recover(0).action is RecoveryAction.SPENT  # claimed; no results stored, so none can be published
    assert "slot_stored" not in kinds(world) and not sealed_run.results_v6_path().exists()


def test_the_preflight_receipt_carries_the_hash_the_run_is_compared_with(world):
    token = world.token(0, claim=False)
    try:
        receipt = sealed_run.preflight_slot(token, **world.run_args())
        assert receipt.config_hash == world.config_hash() == world.ledger().incumbent_config_hash()
    finally:
        token.release()


# =================================================================================================================================
# T5 / T3: the ledger record
# =================================================================================================================================
def test_a_draw_record_stores_its_mode_replay_of_and_the_two_identities(tmp_path):
    led = with_controls(tmp_path)
    record_draws(led, CONFIGS[0], n=1)
    [record] = led.dev_draws(CONFIGS[0])
    assert (record["mode"], record["replay_of"], record["scorer_identity"], record["rewrite_config_hash"]) == ("build", None, SCORER, REWRITE)
    record_draws(led, CONFIGS[1], n=1, mode="replay")
    assert led.dev_draws(CONFIGS[1])[0]["replay_of"] == 1 and led.dev_draws(CONFIGS[1])[0]["mode"] == "replay"
    for bad in ("", "short", "G" * 64, None, 5):
        with pytest.raises(ValueError):
            led.record_dev_draw(CONFIGS[0], sha("x"), sha("y"), "run-z", scorer_identity=bad, rewrite_config_hash=REWRITE)
        with pytest.raises(ValueError):
            led.record_dev_draw(CONFIGS[0], sha("x"), sha("y"), "run-z", scorer_identity=SCORER, rewrite_config_hash=bad)
    for missing in ({"scorer_identity": SCORER}, {"rewrite_config_hash": REWRITE}):
        with pytest.raises(TypeError):  # both identities are required: a record without them could not be compared
            led.record_dev_draw(CONFIGS[0], sha("x"), sha("y"), "run-z", **missing)
    assert len(led.dev_draws(CONFIGS[0])) == 1


def test_one_configurations_draws_are_all_build_draws_or_all_replay_draws(tmp_path):
    led = with_controls(tmp_path)
    record_draws(led, CONFIGS[0])  # the incumbent's five build draws
    record_draws(led, CONFIGS[1], n=2, mode="replay")
    with pytest.raises(ProtocolError, match="all build draws or all replay draws"):
        record_draws(led, CONFIGS[1], n=1)  # a build draw among replay draws
    record_draws(led, CONFIGS[2], n=1)
    with pytest.raises(ProtocolError, match="all build draws or all replay draws"):
        record_draws(led, CONFIGS[2], n=1, mode="replay")  # a replay draw among build draws
    assert [r["mode"] for r in led.dev_draws(CONFIGS[1])] == ["replay", "replay"]
    assert [r["mode"] for r in led.dev_draws(CONFIGS[2])] == ["build"] and len(led.dev_draws(CONFIGS[1])) == 2


def test_the_mirror_still_carries_the_three_digests_only(tmp_path):
    led = with_controls(tmp_path)
    record_draws(led, CONFIGS[0], n=1)
    line = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[-1])
    assert set(line) == {"seq", "kind", "digest", "digests"} and set(line["digests"]) == {"config_hash", "artifact_sha256", "rows_sidecar_sha256"}


# --- the void record --------------------------------------------------------------------------------------------------------------------------------
def test_a_void_is_logged_without_consuming_an_index_and_the_mirror_carries_the_configuration_only(tmp_path):
    led = with_controls(tmp_path)
    record_draws(led, CONFIGS[0], n=2)
    led.record_dev_draw_void(CONFIGS[0], "run-voided", "rewrite_fallback")
    assert led.dev_draws(CONFIGS[0])[-1]["draw_index"] == 2 and len(led.dev_draws(CONFIGS[0])) == 2
    record_draws(led, CONFIGS[0], n=1)
    assert [r["draw_index"] for r in led.dev_draws(CONFIGS[0])] == [1, 2, 3]  # the next draw takes the number the void would have had
    [void] = led.dev_draw_voids(CONFIGS[0])
    assert (void["kind"], void["config_hash"], void["run_id"], void["reason"]) == ("dev_draw_void", CONFIGS[0], "run-voided", "rewrite_fallback")
    assert led.dev_draw_voids(CONFIGS[1]) == []
    line = [json.loads(x) for x in (tmp_path / "mirror.jsonl").read_text().splitlines() if '"dev_draw_void"' in x][0]
    assert line["digests"] == {"config_hash": CONFIGS[0]} and set(line) == {"seq", "kind", "digest", "digests"}
    assert led.dev_draw_configs() == [CONFIGS[0]]  # a void is not a draw and does not count toward the five configurations


def test_a_void_run_id_is_not_a_draw_receipt_or_a_second_void_and_the_inputs_are_checked(tmp_path):
    led = with_controls(tmp_path)
    record_draws(led, CONFIGS[0], n=1)
    receipt = led.dev_draws(CONFIGS[0])[0]["execution_receipt"]
    with pytest.raises(ProtocolError):
        led.record_dev_draw_void(CONFIGS[0], receipt, "rewrite_fallback")  # that run was recorded as a draw
    led.record_dev_draw_void(CONFIGS[1], "run-void", "rewrite_fallback")
    with pytest.raises(ProtocolError):
        led.record_dev_draw_void(CONFIGS[1], "run-void", "rewrite_fallback")
    for config, run, reason in (("short", "run-a", "rewrite_fallback"), (CONFIGS[0], "bad id", "rewrite_fallback"),
                                (CONFIGS[0], "run-a", "other"), (None, "run-a", "rewrite_fallback")):
        with pytest.raises(ValueError):
            led.record_dev_draw_void(config, run, reason)
    assert sealed_ledger.DRAW_VOID_REASONS == ("rewrite_fallback",)


# =================================================================================================================================
# T5: the draw with a rewrite fallback, end to end
# =================================================================================================================================
def fallback_expansion(monkeypatch):
    monkeypatch.setattr(ev, "expand_query", lambda question, *a, **k: Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR))


def test_a_draw_with_a_rewrite_fallback_is_voided_and_releases_nothing(drawable, fakes, monkeypatch, capsys):
    good_expand = ev.expand_query
    fallback_expansion(monkeypatch)  # a rewrite timeout: every question fell back to the raw query
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable, name="voided"))
    assert code == 4 and "gate=draw_void (rewrite_fallback)" in err and "voided and logged" in err
    assert "private v6 run" not in out and "draw recorded" not in out
    [run] = list((drawable.private / "runs").iterdir())
    assert not (run / "report.md").exists()  # the void record is written first, and the run still releases nothing
    [void] = drawable.ledger().records("dev_draw_void")
    assert (void["config_hash"], void["run_id"], void["reason"]) == (drawable.config_hash(), run.name, "rewrite_fallback")
    assert draw_records(drawable) == [] and drawable.ledger().records("dev_run_unrecorded") == []  # a void is its own record
    sidecar = json.loads((run / "report.md.rows.json").read_text(encoding="utf-8"))
    assert sidecar["rewrite_accounting"]["fallbacks"] > 0 and sidecar["rewrite_accounting"]["fallbacks"] == sidecar["rewrite_accounting"]["attempts"]
    # the voided draw used no index: the next draw is the configuration's first
    monkeypatch.setattr(ev, "expand_query", good_expand)
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable, name="after"))
    assert code == 0, err
    assert [r["draw_index"] for r in draw_records(drawable)] == [1] and len(drawable.ledger().records("dev_draw_void")) == 1


def test_a_single_fallback_among_many_expansions_voids_the_draw(drawable, fakes, monkeypatch, capsys):
    good = ev.expand_query
    first = []

    def one_fails(question, *a, **k):
        if not first:
            first.append(question)
            return Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR)
        return good(question, *a, **k)

    monkeypatch.setattr(ev, "expand_query", one_fails)
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_void" in err and len(drawable.ledger().records("dev_draw_void")) == 1 and draw_records(drawable) == []


def test_the_void_record_failing_still_releases_nothing(drawable, fakes, monkeypatch, capsys):
    fallback_expansion(monkeypatch)
    monkeypatch.setattr(SealedLedger, "record_dev_draw_void", lambda self, *a, **k: (_ for _ in ()).throw(ProtocolError("no")))
    code, out, err = cli(monkeypatch, capsys, argv_for(drawable))
    assert code == 4 and "gate=draw_ledger" in err and "private v6 run" not in out
    assert draw_records(drawable) == [] and [r["reason"] for r in drawable.ledger().records("dev_run_unrecorded")] == ["record_failed"]


def test_validate_draw_sidecar_voids_a_fallback_only_after_every_other_check_passes(drawable, fakes, monkeypatch, capsys):
    assert cli(monkeypatch, capsys, argv_for(drawable, name="v"))[0] == 0
    [record] = draw_records(drawable)
    sidecar = sidecar_of(record)
    artifact = eval_privacy.artifact_path("v.json")
    plan = make_plan(drawable)

    def validate(doc):
        sealed_run.validate_draw_sidecar(doc, plan=plan, artifact_path=artifact, artifact_sha256=record["artifact_sha256"], run_id=record["execution_receipt"])

    validate(sidecar)
    assert sidecar["rewrite_accounting"] == {"attempts": sidecar["rewrite_accounting"]["attempts"], "fallbacks": 0,
                                             "live": sidecar["rewrite_accounting"]["attempts"], "replayed": 0}
    fell_back = json.loads(json.dumps(sidecar))
    fell_back["rewrite_accounting"]["fallbacks"] = 1
    with pytest.raises(sealed_run.GateRefused) as info:
        validate(fell_back)
    assert (info.value.gate, info.value.detail) == ("draw_void", "rewrite_fallback")
    broken = json.loads(json.dumps(fell_back))  # a sidecar that is also malformed is malformed, not void
    broken["rows"] = broken["rows"][:-1]
    with pytest.raises(sealed_run.GateRefused) as info:
        validate(broken)
    assert info.value.gate == "draw_sidecar"
    for mutate in (
        lambda s: s.pop("rewrite_accounting"), lambda s: s.update(rewrite_accounting=None), lambda s: s.update(rewrite_accounting=[]),
        lambda s: s["rewrite_accounting"].pop("fallbacks"), lambda s: s["rewrite_accounting"].update(fallbacks=True),
        lambda s: s["rewrite_accounting"].update(fallbacks=-1), lambda s: s["rewrite_accounting"].update(fallbacks="0"),
        lambda s: s.pop("scorer_identity"), lambda s: s.update(scorer_identity="short"),
        lambda s: s["expansion"].pop("config_hash"),
    ):
        bad = json.loads(json.dumps(sidecar))
        mutate(bad)
        with pytest.raises(sealed_run.GateRefused) as info:
            validate(bad)
        assert info.value.gate == "draw_sidecar"


# =================================================================================================================================
# T3 / T5: what a recorded draw carries, from the run
# =================================================================================================================================
def test_a_recorded_draw_carries_the_scorer_identity_the_mode_and_the_rewrite_identity_from_its_sidecar(drawable, fakes, monkeypatch, capsys):
    assert cli(monkeypatch, capsys, argv_for(drawable, name="ids"))[0] == 0
    [record] = draw_records(drawable)
    sidecar = sidecar_of(record)
    assert record["scorer_identity"] == sidecar["scorer_identity"] == scoring_identity.scorer_identity()
    assert (record["mode"], record["replay_of"]) == ("build", None)
    assert record["rewrite_config_hash"] == sidecar["expansion"]["config_hash"] and len(record["rewrite_config_hash"]) == 64
    # every row of both sets is expanded once (the refusal rows too): all live, none fell back
    assert sidecar["rewrite_accounting"] == {"attempts": len(drawable.dev_rows), "fallbacks": 0, "live": len(drawable.dev_rows), "replayed": 0}


def test_a_replay_draw_records_its_mode_its_index_and_the_artifacts_rewrite_identity(drawable, fakes, monkeypatch, capsys):
    w = drawable
    for name in ("inc1", "inc2"):
        assert cli(monkeypatch, capsys, argv_for(w, name=name))[0] == 0
    incumbent = w.config_hash()
    w.record_controls()
    art = [eval_privacy.artifact_path(n + ".json") for n in ("inc1", "inc2")]
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: OTHER_INDEX)  # the candidate's pipeline
    for index, path in enumerate(art, start=1):
        assert cli(monkeypatch, capsys, argv_for(w, expansion=str(path), replay=index), key=False)[0] == 0
    candidate = w.config_hash()
    replays = draw_records(w, candidate)
    assert [(r["mode"], r["replay_of"], r["draw_index"]) for r in replays] == [("replay", 1, 1), ("replay", 2, 2)]
    inc = draw_records(w, incumbent)
    assert [r["rewrite_config_hash"] for r in replays] == [r["rewrite_config_hash"] for r in inc]  # the artifacts' identity
    assert {r["scorer_identity"] for r in replays} == {r["scorer_identity"] for r in inc} == {scoring_identity.scorer_identity()}
    assert sidecar_of(replays[0])["rewrite_accounting"]["replayed"] == sidecar_of(replays[0])["rewrite_accounting"]["attempts"] > 0
    assert sidecar_of(replays[0])["rewrite_accounting"]["live"] == 0
    # a build draw cannot follow a candidate's replay draws, whatever the command line says
    assert cli(monkeypatch, capsys, argv_for(w, name="late-build"))[0] == 4


# =================================================================================================================================
# T5 / T3: the batch is bound to the draws
# =================================================================================================================================
def incumbent_ledger(tmp_path, name="ledger", **kwargs):
    """A ledger of its own (its own out-of-tree store), frozen, with the controls recorded (the incumbent is CONFIGS[0]) and the
    incumbent's five build draws."""
    (tmp_path / name).mkdir()
    led = isolated(tmp_path / name)
    record_draws(led, CONFIGS[0], **kwargs)
    return led


def test_a_replay_entry_needs_replay_draws_of_the_incumbents_same_index_draws(tmp_path):
    led = incumbent_ledger(tmp_path)
    record_draws(led, CONFIGS[1], mode="build")  # build draws under a replay entry
    with pytest.raises(DrawBindingRefused, match="replay"):
        led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
    assert led.batch_declaration() is None
    ok = incumbent_ledger(tmp_path, "ok")
    record_draws(ok, CONFIGS[1], mode="replay")
    assert len(ok.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)) == 64


def test_a_replay_entrys_counted_draws_must_each_replay_the_draw_of_their_own_index(tmp_path):
    """The ledger refuses to record a replay draw whose ``replay_of`` is not its index; a record that was written wrong anyway
    (an edited ledger) is refused again where the batch is declared."""
    raw = incumbent_ledger(tmp_path)
    record_draws(raw, CONFIGS[1], n=4, mode="replay")
    with raw._txn() as txn:
        txn.append("dev_draw", config_hash=CONFIGS[1], draw_index=5, artifact_sha256=sha("x"), rows_sidecar_sha256=sha("y"),
                   execution_receipt="run-raw", mode="replay", replay_of=2, rewrite_config_hash=REWRITE, scorer_identity=SCORER)
    with pytest.raises(DrawBindingRefused, match="replay"):
        raw.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)


def candidate_entries(*configs, rewrite):
    return [{"config_hash": c, "expansion": "candidate", "rewrite_config_hash": rewrite} for c in configs]


def test_a_candidate_entry_needs_build_draws_under_its_own_new_rewrite_identity(tmp_path):
    new = "7" * 64
    led = incumbent_ledger(tmp_path)
    record_draws(led, CONFIGS[1], mode="replay")  # replay draws under a candidate entry
    with pytest.raises(DrawBindingRefused, match="build draws"):
        led.record_batch_declaration(candidate_entries(CONFIGS[1], rewrite=new), draws_required=5)
    for n, (kwargs, match) in enumerate((
        ({"rewrite": REWRITE}, "must differ from the incumbent"),  # the rewrite is not new: this is not a rewrite-changing candidate
        ({"rewrite": "8" * 64}, "rewrite identity is not the one"),  # the draws were made under another rewrite than the entry names
    )):
        other = incumbent_ledger(tmp_path, f"case{n}")
        record_draws(other, CONFIGS[1], **kwargs)
        declared = REWRITE if n == 0 else new
        with pytest.raises(DrawBindingRefused, match=match):
            other.record_batch_declaration(candidate_entries(CONFIGS[1], rewrite=declared), draws_required=5)
    ok = incumbent_ledger(tmp_path, "ok")
    record_draws(ok, CONFIGS[1], rewrite=new)
    assert len(ok.record_batch_declaration(candidate_entries(CONFIGS[1], rewrite=new), draws_required=5)) == 64


def test_the_counted_draws_must_carry_the_incumbents_scorer_identity(tmp_path):
    for n, (incumbent_scorer, candidate_scorer, ok) in enumerate((
        (SCORER, "9" * 64, False),  # the candidate's draws were scored by another scorer
        ("9" * 64, SCORER, False),  # ... or the incumbent's were
        (SCORER, SCORER, True),
    )):
        (tmp_path / f"s{n}").mkdir()
        led = isolated(tmp_path / f"s{n}")
        record_draws(led, CONFIGS[0], scorer=incumbent_scorer)
        record_draws(led, CONFIGS[1], mode="replay", scorer=candidate_scorer)
        if ok:
            led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
        else:
            with pytest.raises(DrawBindingRefused, match="scorer"):
                led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
            assert led.batch_declaration() is None


def test_only_the_counted_draws_are_compared_and_an_incumbent_without_draws_cannot_be_compared_with(tmp_path):
    led = incumbent_ledger(tmp_path)
    record_draws(led, CONFIGS[1], mode="replay")
    record_draws(led, CONFIGS[1], n=1, mode="replay", scorer="9" * 64)  # a sixth draw, outside the first five, by another scorer
    led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)  # the first five are what counts
    (tmp_path / "none").mkdir()
    bare_led = isolated(tmp_path / "none")
    record_draws(bare_led, CONFIGS[1])  # build draws; the incumbent has none at all
    with pytest.raises(DrawBindingRefused, match="scorer"):
        bare_led.record_batch_declaration([{"config_hash": CONFIGS[1], "expansion": "replay"}], draws_required=5)


def test_an_abandoned_entry_is_not_compared(tmp_path):
    led = incumbent_ledger(tmp_path)
    record_draws(led, CONFIGS[1], n=2, scorer="9" * 64)  # partly drawn, by another scorer, in build mode: never run, never compared
    record_draws(led, CONFIGS[2], mode="replay")
    led.record_batch_declaration([{"config_hash": CONFIGS[1], "expansion": "abandoned"}, *entries(CONFIGS[2])], draws_required=5)
    assert [c["expansion"] for c in led.batch_declaration()["candidates"]] == ["abandoned", "replay"]


def test_the_coverage_rules_come_before_the_binding_rules(tmp_path):
    led = incumbent_ledger(tmp_path)
    record_draws(led, CONFIGS[1], n=4)  # too few AND build draws under a replay entry: the coverage refusal is the one raised
    with pytest.raises(DrawCoverageRefused):
        led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
    assert issubclass(DrawBindingRefused, ProtocolError) and not issubclass(DrawBindingRefused, DrawCoverageRefused)


def test_the_helper_declare_satisfies_every_rule_for_each_kind_of_entry(tmp_path):
    led = with_controls(tmp_path)
    digest = declare(led, [
        {"config_hash": CONFIGS[1], "expansion": "replay"},
        {"config_hash": CONFIGS[2], "expansion": "candidate", "rewrite_config_hash": "7" * 64},
        {"config_hash": CONFIGS[3], "expansion": "abandoned"},
    ])
    assert len(digest) == 64 and len(led.dev_draws(CONFIGS[0])) == 5 and len(led.dev_draws(CONFIGS[3])) == 1
