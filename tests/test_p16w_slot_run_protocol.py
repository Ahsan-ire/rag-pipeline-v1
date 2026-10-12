"""Phase 16A-2a a5: the slot protocol around the run, canaries, fault injection and canonical v6.

Acceptance (c) canaries, (f) fault injection at each of a4's steps, and (g)
canonical v6. Synthetic fixtures from ``tests/p16_slot.py``; every model is a
fake; the epoch's ledger, run directories and committed report all live under
``tmp_path`` (the suite's private root moves ``eval/results_v6.md`` with it).
"""

import json
import logging
import os
import time
from pathlib import Path

import pytest

import src.evaluator as ev
from src import query_rewrite, sealed_report, sealed_run
from src.expansion_artifact import rewrite_identity
from src.query_rewrite import REWRITE_MODEL, STATUS_API_ERROR, Expansion
from src.sealed_ledger import EpochStop, RecoveryAction, SealedLedger
from tests.p16_canary import assert_no_leak
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    HELD,
    Crash,
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    socket_spy,
    world,
)
from tests.test_p16w_spend import ledger_lines, make_fakes, make_meter

ROOT = Path(__file__).resolve().parent.parent


def crash_at(monkeypatch, owner, name, *, after=False):
    """Make ``owner.name`` die (before it runs, or just after it returns): a simulated process death."""
    real = getattr(owner, name)

    def wrapper(*args, **kwargs):
        if after:
            real(*args, **kwargs)
        raise Crash(name)

    monkeypatch.setattr(owner, name, wrapper)


def recover(world, slot, **kw):
    return sealed_run.recover_slot(slot, ledger=world.ledger(), **kw)


def run_id_of(world, slot):
    return world.ledger().recover(slot).run_id


# ---------------------------------------------------------------------------
# (c) Canaries: nothing free-text or fake-model leaves eval/private/sealed/
# ---------------------------------------------------------------------------


def outside_without_ledger_payloads(world):
    """The text of every file outside ``sealed/`` except the out-of-tree ledger payloads (``sealed_payloads/``).

    A payload is content-free but holds opaque ids by design: the freeze-bound group record (``twins_sealed``)
    stores the twins edges, as the assignment payloads store every family.
    """
    return [p.read_text(errors="replace") for p in world.files_outside_sealed() if "sealed_payloads" not in p.parts]


def test_canaries_in_every_free_text_field_and_fake_model_output_never_leave_sealed(
    world, monkeypatch, capsys, caplog, socket_spy
):
    caplog.set_level(logging.DEBUG)
    c = world.canaries
    # Real rewrite, generation and judge clients (metered fakes, LangChain callbacks), canary replies.
    monkeypatch.setattr(ev, "expand_query", query_rewrite.expand_query)
    fakes = make_fakes()
    fakes["rewrite"].reply = (
        f"1) {c.fields['rewrite']} alpha\n2) {c.fields['rewrite']} beta\n3) {c.fields['rewrite']} gamma\n"
        f"4) INTENT: {c.fields['intent']} delta"
    )
    fakes["generation"].reply = f"{c.fields['answer']} generated through the metered client"
    fakes["judge"].reply = json.dumps({"claims": [
        {"claim": f"{c.fields['claim']} one", "verdict": "supported"},
        {"claim": f"{c.fields['claim']} two", "verdict": "unsupported"},
    ]})
    meter = make_meter(fakes)
    retrieval = world.retrieval()
    base = world.generator(retrieval)

    def generate(question):
        meter.generation_llm().invoke(question)
        return base(question)

    before = {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}
    outcome = world.run(0, meter=meter, generate=generate, matrix_extra={"judge_fn": None})
    assert outcome.canonical and outcome.published
    assert all(fakes[k].sent for k in ("rewrite", "generation", "judge")), "the fake models must really have run"

    out = capsys.readouterr()
    report = sealed_run.results_v6_path().read_text()
    outside = [p.read_text(errors="replace") for p in world.files_outside_sealed()]
    assert len(outside) >= 5 and any("slot_claim" in text for text in outside)  # the ledgers are among them
    secrets = c.secrets()
    assert_no_leak(secrets, out.out, out.err, caplog.text, report, *outside, where="(canonical slot 0)")
    # Opaque ids appear only inside sealed/ and in the ledger's records and payloads (exclusions, the group
    # record's edges): not in any output, and not in any other file.
    for text in (out.out, out.err, caplog.text, report, *outside_without_ledger_payloads(world)):
        assert not [i for i in world.ids() if i in text]
    # Positive controls: the data really was there, inside sealed/runs/<id>/.
    run_dir = world.run_dir(run_id_of(world, 0))
    inside = {p.name: p.read_text() for p in run_dir.iterdir()}
    assert set(inside) == {
        "results.json", "rows.json", "inputs.json", "expansion_artifact.json", "judge_review.jsonl"
    }
    assert c.fields["claim"] in inside["judge_review.jsonl"]
    assert c.fields["rewrite"] in inside["expansion_artifact.json"] and c.fields["intent"] in inside["expansion_artifact.json"]
    assert world.families[0] in inside["results.json"]
    for secret in secrets:
        assert secret not in inside["results.json"], "the stored results hold ids and enums, never text"
    assert socket_spy == []
    assert before == {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}
    assert ledger_lines(), "the spend meter really metered the fake calls"
    assert_no_leak(secrets, *[json.dumps(line) for line in ledger_lines()])


def test_the_run_directory_satisfies_the_scanners_contract(world):
    """The merge gate's precheck accepts the layout (inputs.json, a bound artifact) and the report scans clean."""
    import importlib

    scan_leaks = importlib.import_module("scripts.scan_leaks")
    world.run(0)
    run_id = run_id_of(world, 0)
    index = scan_leaks.build_needles(scan_leaks.collect_sources(run_id=run_id))
    assert index.s_whole, "the sealed set must be an S-needle source"
    assert scan_leaks.precheck(index.source_shas) == []
    assert scan_leaks.scan_output([sealed_run.results_v6_path()], run_id=run_id) == []
    # and the scanner does catch a sealed question if one is pasted into a report
    leaked = world.root / "leaked.md"
    leaked.write_text(world.sealed_rows[0]["question"])
    assert [h.family for h in scan_leaks.scan_output([leaked], run_id=run_id)] == [world.families[0]]


def test_a_non_canonical_run_and_a_refusal_leak_nothing_either(world, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    world.run(0, skip_refusals=True)
    gate_of(lambda: world.token(0))
    out = capsys.readouterr()
    outside = [p.read_text(errors="replace") for p in world.files_outside_sealed()]
    assert_no_leak(world.canaries.secrets(), out.out, out.err, caplog.text, *outside)
    for text in (out.out, out.err, caplog.text, *outside_without_ledger_payloads(world)):
        assert not [i for i in world.ids() if i in text]
    assert "failed guards: refusals_scored" in out.out


def test_the_committed_mirror_carries_kinds_and_digests_only_even_after_an_exclusion(world):
    world.run(0)
    world.ledger().exclude(world.families[4], "owner")
    mirror = (world.private.parent / "sealed_ledger.jsonl").read_text()
    assert mirror and not [i for i in world.ids() if i in mirror]
    private_ledger = Path(os.environ["CC_SEALED_LEDGER"]).read_text()
    assert world.families[4] in private_ledger  # the private ledger may name the group; the mirror may not


# ---------------------------------------------------------------------------
# (f) Fault injection at each of a4's steps
# ---------------------------------------------------------------------------


def test_a_crash_during_the_run_spends_the_slot_and_allows_zero_b_once(world):
    def die(question):
        raise Crash("mid-run")

    with pytest.raises(Crash):
        world.run(0, generate=die)
    assert world.ledger().recover(0).action is RecoveryAction.SPENT
    assert kinds(world) == ["freeze", "slot_claim"]
    with pytest.raises(sealed_run.GateRefused) as info:
        recover(world, 0)
    assert (info.value.gate, info.value.detail) == ("claim", "spent")
    assert gate_of(lambda: world.token(0)) == "claim"  # never re-run
    assert world.ledger().may_use_0b()  # slot 0 ended without an intent
    assert world.run("0b").canonical
    assert gate_of(lambda: world.token("0b")) == "claim"  # once


def test_a_crash_before_step_three_spends_the_slot_even_with_files_on_disk(world, monkeypatch):
    crash_at(monkeypatch, SealedLedger, "store_results")
    with pytest.raises(Crash):
        world.run(0)
    runs = list((world.private / "sealed" / "runs").iterdir())
    assert len(runs) == 1 and (runs[0] / "results.json").is_file()  # written, but never recorded
    assert world.ledger().recover(0).action is RecoveryAction.SPENT
    monkeypatch.undo()


def test_a_crash_between_steps_three_and_four_renders_from_storage_and_consumes_once_rendered(world):
    with pytest.MonkeyPatch.context() as m:
        crash_at(m, sealed_run, "finish_slot")
        with pytest.raises(Crash):
            world.run(0)
    ledger = world.ledger()
    assert ledger.recover(0).action is RecoveryAction.RENDER_FROM_STORAGE
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored"]
    assert not ledger.may_use_0b()  # stored and not yet consumed: slot 0 has not ended
    assert not sealed_run.results_v6_path().exists()
    with pytest.MonkeyPatch.context() as m:
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a fresh call was made during recovery"))  # noqa: E731
        for owner, name in ((ev, "run_eval_matrix"), (ev, "expand_query"), (sealed_run, "acquire_slot_token"),
                            (sealed_run, "run_slot")):
            m.setattr(owner, name, boom)
        outcome = recover(world, 0)
    assert outcome.canonical and outcome.published and outcome.action == "render_from_storage"
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored", "publication_intent", "slot_consumed"]
    report = sealed_run.results_v6_path().read_text()  # the recovery really rendered and published it
    assert report.splitlines()[0].endswith("(sealed slot 0; canonical)") and "Canonical run: True" in report
    assert "## Sealed set" in report and "## Family rates (sealed)" in report
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED


def test_a_non_canonical_slot_zero_writes_no_intent_and_allows_zero_b_once(world, capsys):
    outcome = world.run(0, skip_refusals=True)
    assert not outcome.canonical and outcome.failed_guards == ("refusals_scored",) and not outcome.published
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored", "slot_consumed"]  # no intent
    assert not sealed_run.results_v6_path().exists()
    assert world.ledger().may_use_0b()
    assert world.run("0b").canonical
    assert kinds(world).count("publication_intent") == 1
    assert gate_of(lambda: world.token("0b")) == "claim"
    assert not world.ledger().may_use_0b()


@pytest.mark.parametrize("crash_after_publish", [False, True])
def test_a_crash_after_the_intent_refuses_zero_b_and_republishes(world, monkeypatch, crash_after_publish):
    """Before the first emitted number (crash_after_publish=False) or after it (True)."""
    with pytest.MonkeyPatch.context() as m:
        if crash_after_publish:
            crash_at(m, SealedLedger, "record_consumed")
        else:
            crash_at(m, sealed_run, "render_and_publish")
        with pytest.raises(Crash):
            world.run(0)
    report = sealed_run.results_v6_path()
    assert report.exists() is crash_after_publish
    first = report.read_text() if crash_after_publish else None
    ledger = world.ledger()
    assert ledger.recover(0).action is RecoveryAction.REPUBLISH
    assert not ledger.may_use_0b()  # an intent disqualifies 0b for good, rendering interrupted or not
    assert gate_of(lambda: world.token("0b")) == "claim"
    outcome = recover(world, 0)
    assert outcome.canonical and outcome.action == "republish"
    assert kinds(world) == ["freeze", "slot_claim", "slot_stored", "publication_intent", "slot_consumed"]
    if crash_after_publish:
        assert report.read_text() == first  # the same stored result, republished byte for byte
    assert not world.ledger().may_use_0b()
    with pytest.raises(sealed_run.GateRefused) as info:
        recover(world, 0)
    assert (info.value.gate, info.value.detail) == ("claim", "consumed")


def test_a_post_freeze_exclusion_keeps_the_next_slots_replay_and_pairing(world, tmp_path):
    world.run(0)
    r0 = run_id_of(world, 0)
    artifact = world.run_dir(r0) / "expansion_artifact.json"
    world.write_prereg({
        "0": {"expansion": "live"},
        "1": {"expansion": "replay", "artifact_sha256": sealed_run._file_sha(artifact)},
    })
    group = world.families[2]  # the {2, 3} group: two eligible families
    world.ledger().exclude(group, "owner")
    miss = {r["question"] for r in world.sealed_rows if r["family_id"] in (world.families[0], world.families[4])}
    outcome = world.run(1, expansion=str(artifact), baseline_run=r0, hit=lambda mode, q: q not in miss)
    assert not outcome.canonical
    assert outcome.failed_guards == ("slot_0_or_0b", "live_expansion")  # confirmatory, and replayed
    d0, d1 = world.stored_doc(0), world.stored_doc(1)
    assert d1["expansion"]["rewrite_replayed"] == 24 and d1["expansion"]["rewrite_live"] == 0
    s0 = next(s for s in d0["sets"] if s["role"] == "sealed")
    s1 = next(s for s in d1["sets"] if s["role"] == "sealed")
    assert s0["sha256"] == s1["sha256"] and s0["cohort"]["cohort_fp"] == s1["cohort"]["cohort_fp"]
    assert len(s1["rows"]) == 16  # the excluded group's rows still ran: set hashes and artifact keys survive
    assert d1["baseline"]["run_id"] == r0
    # Analysis drops the group from both arms; the pairing is on the six remaining families.
    excluded = frozenset(world.ledger().exclusions())
    assert excluded == {group}
    text = sealed_report.format_sealed_report(d1, excluded=excluded, canonical=False, failed_guards=outcome.failed_guards)
    assert "excluded groups: 1 of 6 (2 families dropped" in text
    assert "**Primary test**: exact two-sided cluster sign-flip test" in text and "p = 0.5000" in text
    assert "Sensitivity (labelled, not the test): Durkalski clustered McNemar by group, asymptotic: chi2 = 1.800, p = 0.1797" in text
    assert "exact two-sided McNemar on families: b = 0, c = 2, p = 0.5000" in text
    assert "the candidate gains 0 and loses 2 of 4" in text


def test_below_the_floor_the_epoch_ends(world):
    ledger = world.ledger()
    ledger.exclude(world.families[0], "owner")
    ledger.exclude(world.families[2], "owner")  # 4 sealed families, 2 eligible: below both floors
    assert gate_of(lambda: world.token(0)) == "floor"
    assert all(r["kind"] != "slot_claim" for r in world.ledger().records())


def test_losing_both_private_ledgers_stops_the_epoch(world, monkeypatch):
    world.run(0)
    Path(os.environ["CC_SEALED_LEDGER"]).unlink()
    (world.private / "sealed" / "ledger" / "sealed_ledger.jsonl").unlink()
    assert (world.private.parent / "sealed_ledger.jsonl").is_file()  # the committed mirror is the witness
    with pytest.raises(EpochStop):
        world.token("0b")
    with pytest.raises(EpochStop):
        recover(world, 0)
    with pytest.raises(EpochStop):
        world.ledger().may_use_0b()


# ---------------------------------------------------------------------------
# (g) Canonical v6: each guard removed alone, only canonical writes results_v6.md
# ---------------------------------------------------------------------------

#: guard -> a mutation of the canonical run's stored facts that fails that guard (and only that one)
FACT_MUTATIONS = {
    "heldout_frozen_sealed_set": ("heldout_is_frozen_set", False),
    "heldout_answerable": ("heldout_answerable", False),
    "realistic_answerable": ("realistic_answerable", False),
    "all_modes": ("all_modes", False),
    "distinct_sets": ("distinct_sets", False),
    "refusals_scored": ("refusals_scored", False),
    "completeness_scored": ("completeness_scored", False),
    "top_k_6": ("top_k", 5),
    "no_generation_errors": ("generation_errors", 1),
    "no_incomplete_generations": ("generation_incomplete", 1),
    "no_unknown_status": ("generation_unknown", 1),
    "expansion_attempted": ("rewrite_attempts", 0),
    "no_expansion_fallbacks": ("rewrite_fallbacks", 1),
    "bm25_loaded": ("bm25_default_path_in_play", True),  # together with bm25_loaded False below
    "judge_clean": ("judge_clean", False),
    "slot_0_or_0b": ("slot", "1"),
    "bound_records": ("bound_records", False),
    "live_expansion": ("expansion_mode", "replay"),
    "tracing_off": ("tracing_off", False),
}


def test_every_guard_has_exactly_one_stored_fact_that_fails_it(world):
    assert tuple(FACT_MUTATIONS) == sealed_run.GUARDS == tuple(sealed_run.canonical_guards({}))
    world.run(0)
    facts = world.stored_doc(0)["facts"]
    assert sealed_run.failed_guards(facts) == []
    for guard, (key, bad) in FACT_MUTATIONS.items():
        mutated = dict(facts, **{key: bad})
        if guard == "bm25_loaded":
            mutated["bm25_loaded"] = False
        assert sealed_run.failed_guards(mutated) == [guard], guard


def test_a_missing_fact_fails_its_guard_closed():
    assert sealed_run.failed_guards({}) == list(sealed_run.GUARDS)


def test_a_candidate_arm_counts_as_live_expansion_and_a_replay_does_not():
    base = {"expansion_mode": "candidate"}
    assert sealed_run.canonical_guards(base)["live_expansion"] is True
    assert sealed_run.canonical_guards({"expansion_mode": "replay"})["live_expansion"] is False


def _expansion_with_one_fallback(world):
    first = world.sealed_rows[0]["question"]
    real = world.expand()

    def fn(question, *a, **k):
        if question == first:
            return Expansion(question, (), REWRITE_MODEL, STATUS_API_ERROR)
        return real(question)

    return fn


def _first_answer_question(world):
    return next(r["question"] for r in world.sealed_rows if r["scope"] == "answer")


#: ``top_k_6`` is not here: a sealed slot refuses any ``top_k`` other than the prereg's cut-off (6) before the claim
#: (``cutoff``, tests/test_p16w_fix_r2_cutoff.py), so that guard can fail only through its stored fact (FACT_MUTATIONS).
E2E_GUARDS = {
    "refusals_scored": ({"skip_refusals": True}, "refusals_scored"),
    "completeness_scored": ({"skip_completeness": True}, "completeness_scored"),
    "all_modes": ({"modes": ["hybrid"]}, "all_modes"),
    "judge_clean": ({"judge": False}, "judge_clean"),
    "realistic_answerable": ({"specs": "no_realistic"}, "realistic_answerable"),
}


@pytest.mark.parametrize("name", sorted(E2E_GUARDS))
def test_each_guard_removed_alone_makes_the_run_non_canonical_and_prints_guard_names_only(world, capsys, name):
    kwargs, expected = E2E_GUARDS[name]
    kwargs = dict(kwargs)
    if kwargs.get("specs") == "no_realistic":
        kwargs["specs"] = [s for s in world.specs if s[0] != "realistic"]
    outcome = world.run(0, **kwargs)
    out = capsys.readouterr()
    assert not outcome.canonical and outcome.failed_guards == (expected,)
    assert out.out.strip().splitlines()[-1] == f"[eval] sealed slot 0: not canonical; failed guards: {expected}"
    assert not sealed_run.results_v6_path().exists()
    assert "publication_intent" not in kinds(world) and world.ledger().recover(0).action is RecoveryAction.CONSUMED
    assert_no_leak(world.canaries.secrets(), out.out, out.err)
    assert not [i for i in world.ids() if i in out.out + out.err]


def test_a_generation_error_removes_only_the_no_generation_errors_guard(world, monkeypatch, capsys):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    victim = _first_answer_question(world)

    def fail_once(question):
        if question == victim:
            raise RuntimeError(f"synthetic failure for {question}")

    retrieval = world.retrieval()
    outcome = world.run(0, generate=world.generator(retrieval, on_call=fail_once))
    assert outcome.failed_guards == ("no_generation_errors",)
    assert victim not in capsys.readouterr().out


@pytest.mark.parametrize("status,guard", [("truncated", "no_incomplete_generations"), (None, "no_unknown_status")])
def test_an_incomplete_or_unknown_status_removes_only_its_guard(world, status, guard):
    victim = _first_answer_question(world)
    retrieval = world.retrieval()
    base = world.generator(retrieval)

    def generate(question):
        out = base(question)
        if question == victim:
            out.pop("generation_status", None)
            if status is not None:
                out["generation_status"] = status
        return out

    assert world.run(0, generate=generate).failed_guards == (guard,)


def test_an_expansion_fallback_removes_only_the_no_fallbacks_guard(world, monkeypatch):
    monkeypatch.setattr(ev, "expand_query", _expansion_with_one_fallback(world))
    assert world.run(0).failed_guards == ("no_expansion_fallbacks",)


def test_records_edited_during_the_run_remove_only_the_bound_records_guard(world):
    approvals = world.records_dir / "approvals.json"  # the one bound record that lives in a file, not in the ledger
    retrieval = world.retrieval()
    state = {"done": False}

    def edit_once(question):
        if not state["done"]:
            state["done"] = True
            approvals.write_text(approvals.read_text() + " ")

    assert world.run(0, generate=world.generator(retrieval, on_call=edit_once)).failed_guards == ("bound_records",)


def test_tracing_switched_on_during_the_run_removes_only_the_tracing_guard(world, monkeypatch):
    retrieval = world.retrieval()
    state = {"done": False}

    def flip_once(question):
        if not state["done"]:
            state["done"] = True
            monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    assert world.run(0, generate=world.generator(retrieval, on_call=flip_once)).failed_guards == ("tracing_off",)


def test_a_sealed_set_not_passed_as_the_held_out_set_fails_the_held_out_clause(world):
    specs = [(label if label != HELD else "sealed", path) for label, path in world.specs]
    assert world.run(0, specs=specs).failed_guards == ("heldout_frozen_sealed_set", "heldout_answerable")


@pytest.fixture
def default_retrieval_path(world, monkeypatch):
    """The evaluator's DEFAULT retrieval path (store and BM25 loaders, retrieve) over an invented index."""
    state = {"bm25": object()}
    retrieval = world.retrieval()
    monkeypatch.setattr(ev, "assert_embedding_model", lambda *a, **k: None)
    monkeypatch.setattr(ev, "get_vector_store", lambda **k: object())
    monkeypatch.setattr(ev, "load_bm25_index", lambda *a, **k: state["bm25"])
    monkeypatch.setattr(ev, "retrieve", lambda question, top_k=6, mode="hybrid", **kw: retrieval.results(mode, question, top_k))
    return state


@pytest.mark.parametrize("loaded", [True, False])
def test_the_bm25_sidecar_must_actually_load_on_a_default_retrieval_path(world, default_retrieval_path, loaded):
    default_retrieval_path["bm25"] = object() if loaded else None
    outcome = world.run(0, matrix_extra={"retrieve_fn_factory": None})
    assert outcome.canonical is loaded
    assert outcome.failed_guards == (() if loaded else ("bm25_loaded",))


def test_only_a_canonical_run_writes_results_v6_and_no_v6_run_writes_results_md(world, tmp_path, monkeypatch):
    canonical_md, partial_md = tmp_path / "results.md", tmp_path / "results_partial.md"
    monkeypatch.setattr(ev, "DEFAULT_RESULTS_PATH", str(canonical_md))
    monkeypatch.setattr(ev, "PARTIAL_RESULTS_PATH", str(partial_md))
    before = {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}
    world.run(0, skip_completeness=True)  # non-canonical
    assert not sealed_run.results_v6_path().exists()
    assert world.run("0b").canonical
    report = sealed_run.results_v6_path()
    assert report.read_text().startswith("# Legal RAG Evaluation Report v6 (sealed slot 0b; canonical)")
    assert not canonical_md.exists() and not partial_md.exists()
    assert before == {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}
    # The sidecar never lands beside the report: it stays in sealed/runs/<id>/.
    assert sorted(p.name for p in report.parent.iterdir()) == ["private", "results_v6.md", "sealed_ledger.jsonl"]
    assert (world.run_dir(run_id_of(world, "0b")) / "rows.json").is_file()
    assert not list(world.root.rglob("*.rows.json")) or all(
        "sealed" in p.parts for p in world.root.rglob("*.rows.json")
    )


def test_every_gate_name_in_the_code_is_a_listed_fixed_token():
    """Gate names are printed on refusals, so they must be a closed set of plain tokens."""
    import ast

    tree = ast.parse((ROOT / "src" / "sealed_run.py").read_text())
    used = set()
    for node in ast.walk(tree):
        name = getattr(getattr(node, "func", None), "id", None)
        if name in ("GateRefused", "_read_json"):
            arg = node.args[0 if name == "GateRefused" else 1]
            if isinstance(arg, ast.Constant):  # (a Name is _read_json's own pass-through parameter)
                used.add(arg.value)
    assert used == set(sealed_run.GATES)
    assert all(g.isidentifier() for g in sealed_run.GATES)


def test_a_token_runs_the_matrix_once(world):
    token = world.token(0)
    world.run(0, token=token)
    with pytest.raises(sealed_run.GateRefused) as info:
        world.run(0, token=token)
    assert (info.value.gate, info.value.detail) == ("claim", "token_used")
    assert kinds(world).count("slot_stored") == 1


def test_a_slot_run_refuses_a_results_path_a_build_target_and_a_token_for_another_mode(world):
    token = world.token(0)
    with pytest.raises(ValueError, match="slot protocol"):
        ev.run_eval_matrix(world.specs, privacy="private", slot_token=token, meter=make_meter(),
                           results_path=str(world.root / "r.md"))
    for expansion in ("build:artifact.json", str(world.root / "declared.json")):
        with pytest.raises(sealed_run.GateRefused) as info:
            ev.run_eval_matrix(world.specs, privacy="private", slot_token=token, meter=make_meter(),
                               expansion=expansion)
        assert info.value.gate == "expansion_mode"
    assert not list((world.private / "artifacts").glob("*")) if (world.private / "artifacts").exists() else True


# --- the pre-publication scan -----------------------------------------------------------------


def leaky(world, families, *, always=False, extra=()):
    """A renderer that leaks the first question of each family unless its group is excluded."""
    real = sealed_report.format_sealed_report
    leaks = {fam: next(r["question"] for r in world.sealed_rows if r["family_id"] == fam) for fam in families}

    def render(doc, *, excluded, canonical, failed_guards=()):
        text = real(doc, excluded=excluded, canonical=canonical, failed_guards=failed_guards)
        for fam, question in leaks.items():
            if always or world.group_of[fam] not in excluded:
                text += f"\n{question}\n"
        return text + "".join(f"\n{line}\n" for line in extra)

    return render, leaks


def test_a_scan_hit_excludes_the_hit_group_and_re_renders(world, monkeypatch, capsys):
    victim = world.families[4]  # a singleton group with one eligible family
    render, leaks = leaky(world, [victim])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    outcome = world.run(0)
    out = capsys.readouterr()
    assert outcome.canonical and outcome.published and outcome.scan_exclusions == 1
    assert "(1 group(s) excluded by the pre-publication scan)" in out.out
    text = sealed_run.results_v6_path().read_text()
    assert leaks[victim] not in text
    assert "excluded groups: 1 of 6 (1 families dropped" in text and "eligible answerable families (answer or partial, not excluded): 5" in text
    exclusions = world.ledger().records("exclusion")
    assert [(r["group_id"], r["reason"]) for r in exclusions] == [(victim, "s_hit")]
    assert world.ledger().recover(0).action is RecoveryAction.CONSUMED
    mirror = (world.private.parent / "sealed_ledger.jsonl").read_text()
    assert not [i for i in world.ids() if i in text + out.out + out.err + mirror]
    assert_no_leak(world.canaries.secrets(), text, out.out, out.err)


def test_several_hit_groups_are_excluded_together_in_one_pass(world, monkeypatch):
    render, _ = leaky(world, [world.families[4], world.families[5]])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    outcome = world.run(0)
    assert outcome.canonical and outcome.scan_exclusions == 2
    assert {r["group_id"] for r in world.ledger().records("exclusion")} == {world.families[4], world.families[5]}


def test_a_hit_that_pushes_the_report_below_the_floor_ends_the_epoch(world, monkeypatch):
    render, _ = leaky(world, [world.families[0], world.families[4]])  # 3 eligible families lost: 6 -> 3 < 4
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    with pytest.raises(sealed_run.EpochEnded):
        world.run(0)
    assert not sealed_run.results_v6_path().exists()
    assert world.ledger().recover(0).action is RecoveryAction.REPUBLISH  # the intent stands
    with pytest.raises(sealed_run.EpochEnded):  # the exclusions are durable: recovery ends the epoch again
        recover(world, 0)


def test_a_hit_no_sealed_group_explains_blocks_publication(world, monkeypatch):
    dev_question = world.dev_rows[0]["question"]
    render, _ = leaky(world, [], extra=[dev_question])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    with pytest.raises(sealed_run.PublicationBlocked) as info:
        world.run(0)
    assert info.value.reason == "unattributed_hit" and not sealed_run.results_v6_path().exists()
    assert world.ledger().records("exclusion") == []


def test_a_hit_that_persists_after_the_exclusion_blocks_publication(world, monkeypatch):
    render, _ = leaky(world, [world.families[4]], always=True)
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    with pytest.raises(sealed_run.PublicationBlocked) as info:
        world.run(0)
    assert info.value.reason == "persistent_hit" and not sealed_run.results_v6_path().exists()
    assert [r["group_id"] for r in world.ledger().records("exclusion")] == [world.families[4]]


def test_the_scan_is_a_step_not_a_guard(world, monkeypatch):
    render, _ = leaky(world, [world.families[4]])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    assert world.run(0).failed_guards == () and "scan" not in " ".join(sealed_run.GUARDS)


# --- baselines, the candidate arm, and runs with the answer passes off ----------------------


def candidate_prereg(world):
    config_hash = rewrite_identity()["config_hash"]
    world.write_prereg({"0": {"expansion": "live"}, "0b": {"expansion": "live"},
                        "1": {"expansion": "candidate", "rewrite_candidate": config_hash}})
    return config_hash


def test_a_confirmatory_slot_needs_a_verified_consumed_baseline_of_the_same_set(world):
    config_hash = candidate_prereg(world)

    def token(**kw):
        return world.token(1, expansion="live", rewrite_candidate=config_hash, **kw)

    assert gate_of(lambda: token()) == "baseline"  # no baseline named
    assert gate_of(lambda: token(baseline_run="r-unknown")) == "baseline"
    assert gate_of(lambda: world.token(0, baseline_run="r-any")) == "baseline"  # slot 0 has no baseline
    with pytest.MonkeyPatch.context() as m:  # a stored but unconsumed slot 0 is not a baseline
        crash_at(m, sealed_run, "finish_slot")
        with pytest.raises(Crash):
            world.run(0)
    r0 = run_id_of(world, 0)
    assert gate_of(lambda: token(baseline_run=r0)) == "baseline"
    recover(world, 0)
    tampered = world.run_dir(r0) / "rows.json"
    original = tampered.read_text()
    tampered.write_text(original + " ")  # the run directory no longer digests to the ledger's record
    assert gate_of(lambda: token(baseline_run=r0)) == "baseline"
    tampered.write_text(original)
    good = token(baseline_run=r0)
    assert good.baseline["run_id"] == r0 and set(good.baseline["hits_by_mode"]) == set(ev.EVAL_MODES)
    assert len(good.baseline["hits_by_mode"]["hybrid+rewrite"]) == 6  # the six eligible families
    assert kinds(world).count("slot_claim") == 2


def test_a_live_candidate_arm_is_a_confirmatory_run_whose_only_missed_guard_is_the_slot(world):
    config_hash = candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    outcome = world.run(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    assert outcome.failed_guards == ("slot_0_or_0b",) and not outcome.canonical
    doc = world.stored_doc(1)
    assert doc["expansion"]["mode"] == "candidate" and doc["expansion"]["rewrite_live"] == 24
    assert doc["baseline"]["run_id"] == r0 and len(doc["baseline"]["hits"]) == 6
    text = sealed_report.format_sealed_report(doc, excluded=frozenset(), canonical=False, failed_guards=outcome.failed_guards)
    assert "p = 1.0000" in text and "the candidate gains 0 and loses 0 of 6" in text  # identical outcomes: no discordance
    assert not [i for i in world.ids() if i in text]


def test_a_run_with_the_answer_passes_and_expansion_off_is_stored_and_renders(world):
    outcome = world.run(0, skip_refusals=True, skip_completeness=True, judge=False)
    assert set(outcome.failed_guards) == {"refusals_scored", "completeness_scored", "expansion_attempted", "judge_clean"}
    doc = world.stored_doc(0)
    assert doc["answers_ran"] is False and doc["expansion"]["identity"]["kind"] == "disabled"
    assert not (world.run_dir(doc["run_id"]) / "expansion_artifact.json").exists()
    text = sealed_report.format_sealed_report(doc, excluded=frozenset(), canonical=False, failed_guards=outcome.failed_guards)
    assert "scope correct" not in text and "## Judge" not in text and "Scope correct (rows)" not in text
    assert "retrieval hybrid+rewrite strict@6: 6/6" in text


def test_a_judge_sample_keeps_the_run_canonical_and_judges_a_subset(world):
    outcome = world.run(0, judge_sample=2)
    assert outcome.canonical
    rows = next(s for s in world.stored_doc(0)["sets"] if s["role"] == "sealed")["rows"]
    assert sum(r["judge"] is not None for r in rows) == 2
