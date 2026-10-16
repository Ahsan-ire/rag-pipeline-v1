"""Phase 16A-2a phase-gate round 2, fixes 1 and 3: the hit cut-off, and the gate's unclaimed default.

Fix 1 (Codex MAJOR). The primary metric is strict hit@6, and a hit depends on the cut-off: a section at rank 4 is a
hit at 6 and a miss at 3. A sealed slot (0, 0b, 1-4) therefore runs only at the prereg's declared cut-off
(``primary_metric.cutoff``, else 6) and is refused (``cutoff``) BEFORE the claim otherwise. The stored results
record the cut-off, and ``load_baseline`` refuses a baseline whose stored cut-off differs from the candidate's, so
a confirmatory slot can never pair a hit@6 baseline with a hit@3 candidate and read the difference as an effect.

Fix 3 (code review, MINOR). ``acquire_slot_token`` defaults to ``claim=False``: a library caller cannot claim before
the preflight unless it asks for it, and then the gate itself needs the ``top_k`` to check.
"""

import copy
import inspect
import json
from typing import Any, Dict, List

import pytest

import src.evaluator as ev
from src import sealed_report, sealed_run
from src.sealed_ledger import RecoveryAction
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    SlotRetrieval,
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    world,
)
from tests.test_p16w_fix_a_preflight import assert_refused_unclaimed, cli_fakes, fakes  # noqa: F401
from tests.test_p16w_slot_run_cli import cli, spy_run_slot  # noqa: F401
from tests.test_p16w_slot_run_protocol import candidate_prereg, run_id_of
from tests.test_p16w_spend import make_meter


class RankFour(SlotRetrieval):
    """One fixed ranking per question, whatever the cut-off: the evidence is always at rank 4 (index 3).

    ``top_k`` only truncates it, so the identical ranking is a strict hit at cut-offs 4 and above and a miss at 3.
    """

    def __init__(self, section_of: Dict[str, str]) -> None:
        super().__init__(section_of, hit=lambda mode, question: False)  # the base never places the evidence itself

    def results(self, mode: str, question: str, top_k: int) -> List[Dict[str, Any]]:
        full = super().results(mode, question, 12)
        section = self.section_of.get(question)
        if section is not None:
            meta = {**full[3]["metadata"], "section_number": section, "chapter_number": section.split(".")[0]}
            full[3]["document"].metadata = meta
            full[3]["metadata"] = meta
        return full[:top_k]


def rank_four(world):
    return RankFour(world.section_of)


def run_with(world, slot, retrieval, **kw):
    """``world.run`` with the retrieval swapped for ``retrieval`` (all else faked as usual)."""
    matrix_extra = {
        "retrieve_fn_factory": retrieval.factory(6),
        "generate_fn": world.generator(retrieval),
    }
    return world.run(slot, matrix_extra=matrix_extra, **kw)


def set_cutoff(world, cutoff, slots=None):
    """Rewrite the prereg with ``primary_metric.cutoff`` (the slot declarations stay)."""
    doc = dict(world.prereg)
    if slots is not None:
        doc["slots"] = slots
    doc["primary_metric"] = {"cutoff": cutoff}
    world.prereg = doc
    world.prereg_path.write_text(json.dumps(doc), encoding="utf-8")


# --- the premise: a rank-4 section is a hit at 6 and a miss at 3 -----------------------------------------------------


def test_the_rank_four_ranking_is_a_hit_at_six_and_a_miss_at_three(world):
    retrieval = rank_four(world)
    question = next(q for q, s in world.section_of.items() if s == "901.2")
    sections = lambda k: [r["metadata"]["section_number"] for r in retrieval.results("hybrid", question, k)]  # noqa: E731
    assert "901.2" in sections(6) and "901.2" not in sections(3) and sections(6)[3] == "901.2"
    assert sections(12)[:6] == sections(6) and sections(12)[:3] == sections(3)  # one ranking, only truncated


# --- the cut-off check, on every route, before the claim --------------------------------------------------------------


@pytest.mark.parametrize("top_k", [1, 3, 5, 7, 12])
def test_the_gate_refuses_a_top_k_that_is_not_the_cutoff_before_it_claims(world, top_k):
    assert gate_of(lambda: world.token(0, top_k=top_k)) == "cutoff"
    assert gate_of(lambda: world.token(0, claim=False, top_k=top_k)) == "cutoff"
    assert kinds(world) == ["freeze"] and world.ledger().recover(0).action is RecoveryAction.NOT_STARTED
    with world.ledger().lock_slot(0):  # the refusal did not leave the slot busy
        pass


def test_the_gate_never_claims_whatever_top_k_it_is_given(world):
    """r3 F3 (this replaces "a gate that is to claim must be told its top_k"): an eager claim is refused outright, so
    no cut-off is ever vouched for at a claim the preflight has not seen."""
    for top_k in (None, 6):
        assert gate_of(lambda: sealed_run.acquire_slot_token(
            0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path, records_dir=world.records_dir,
            expansion="live", meter=make_meter(), claim=True, top_k=top_k,
        )) == "claim"
    assert kinds(world) == ["freeze"]
    token = world.token(0, claim=False, top_k=None)  # the CLI's route: minted unclaimed, the preflight checks top_k
    assert token.cutoff == 6 and token.claimed == [False] and "slot_claim" not in kinds(world)
    token.claim(sealed_run.preflight_slot(token, **world.run_args()))  # what run_slot does: preflight, then claim (r4 C)
    assert token.claimed == [True] and "slot_claim" in kinds(world)
    token.release()


@pytest.mark.parametrize("top_k", [3, 5, 12])
def test_the_preflight_refuses_a_top_k_that_is_not_the_cutoff_and_the_slot_stays_unclaimed(world, fakes, top_k):
    """The CLI path and the library path alike: the unclaimed token was minted without ``top_k``, so the preflight
    (the evaluator's token re-check) is what refuses, with no call made and no claim."""
    calls, state = fakes
    token = world.token(0, claim=False, top_k=None)
    try:
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.preflight_slot(
                token, set_specs=world.specs, modes=list(ev.EVAL_MODES), top_k=top_k, skip_refusals=False,
                skip_completeness=False, judge=True, judge_sample=None, persist_directory="unused",
                meter=make_meter(), expansion="live", matrix_kwargs=state["kwargs"],
            )
        assert info.value.gate == "cutoff"
    finally:
        token.release()
    assert_refused_unclaimed(world, calls)


def test_the_preflight_passes_the_cutoff_itself_and_still_claims_nothing(world, fakes):
    calls, state = fakes
    token = world.token(0, claim=False, top_k=None)
    try:
        assert isinstance(sealed_run.preflight_slot(  # r4 C: it returns the receipt a claim needs
            token, set_specs=world.specs, modes=list(ev.EVAL_MODES), top_k=6, skip_refusals=False,
            skip_completeness=False, judge=True, judge_sample=None, persist_directory="unused",
            meter=make_meter(), expansion="live", matrix_kwargs=state["kwargs"],
        ), sealed_run.PreflightReceipt)
    finally:
        token.release()
    assert_refused_unclaimed(world, calls)


def test_run_slot_on_an_unclaimed_token_refuses_a_wrong_top_k_before_the_claim(world, fakes):
    calls, state = fakes
    token = world.token(0, claim=False, top_k=None)
    with pytest.raises(sealed_run.GateRefused) as info:
        world.run(0, token=token, top_k=3, matrix_extra=state["kwargs"])
    assert info.value.gate == "cutoff"
    assert_refused_unclaimed(world, calls)


def test_a_claimed_token_still_cannot_run_at_another_cutoff(world, fakes):
    """Defence in depth: the evaluator re-checks the token against ``top_k`` on every run, claimed or not."""
    calls, state = fakes
    token = world.token(0, claim=True, top_k=6)
    with pytest.raises(sealed_run.GateRefused) as info:
        sealed_run.run_slot(
            token, ledger=world.ledger(), set_specs=world.specs, modes=list(ev.EVAL_MODES), top_k=3,
            skip_refusals=False, skip_completeness=False, judge=True, judge_sample=None, persist_directory="unused",
            meter=make_meter(), expansion="live", matrix_kwargs=state["kwargs"],
        )
    assert info.value.gate == "cutoff" and calls.events == []  # nothing ran (the claim was the caller's own opt-in)


@pytest.mark.parametrize("value", ["3", "1", "12"])
def test_the_cli_refuses_a_top_k_that_is_not_the_cutoff_and_spends_no_slot(world, cli_fakes, monkeypatch, capsys, value):
    calls, _state = cli_fakes
    code, out, err = cli(monkeypatch, capsys, world, "--top-k", value)
    assert code == 4 and "gate=cutoff" in err and value not in err.replace("gate=cutoff", "")
    assert_refused_unclaimed(world, calls)
    assert kinds(world) == ["freeze"]


def test_the_cli_runs_at_the_default_top_k(world, monkeypatch, capsys, spy_run_slot):
    code, _out, _err = cli(monkeypatch, capsys, world)  # the CLI's default --top-k is the cut-off
    assert code == 0 and spy_run_slot["kwargs"]["top_k"] == 6 and spy_run_slot["token"].cutoff == 6


# --- where the cut-off comes from: the prereg's primary_metric, else 6 -------------------------------------------------


def test_the_cutoff_is_six_unless_the_prereg_declares_one(world):
    assert sealed_run.load_prereg(world.prereg_path)["cutoff"] == 6
    for declared in ({}, {"name": "strict_hit"}, "strict hit@6", None):
        doc = dict(world.prereg, primary_metric=declared)
        world.prereg_path.write_text(json.dumps(doc))
        assert sealed_run.load_prereg(world.prereg_path)["cutoff"] == 6, declared
    for cutoff in (1, 3, 6):
        set_cutoff(world, cutoff)
        assert sealed_run.load_prereg(world.prereg_path)["cutoff"] == cutoff


@pytest.mark.parametrize("bad", [0, 2, 5, 7, -6, True, "6", 6.0, None, [6]])
def test_a_declared_cutoff_the_evaluator_cannot_score_at_is_a_malformed_prereg(world, bad):
    set_cutoff(world, bad)
    assert gate_of(lambda: sealed_run.load_prereg(world.prereg_path)) == "prereg"
    assert gate_of(lambda: world.token(0)) == "prereg"
    assert kinds(world) == ["freeze"]


def test_a_prereg_declared_cutoff_replaces_the_default_for_every_slot(world):
    set_cutoff(world, 3)
    assert gate_of(lambda: world.token(0, top_k=6)) == "cutoff"
    token = world.token(0, claim=False, top_k=3)
    assert token.cutoff == 3
    token.release()


# --- the stored results record it; a baseline must have been scored at the same one ----------------------------------


def test_the_stored_results_record_the_cutoff_and_the_baseline_block_carries_it(world):
    config_hash = candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    doc0 = world.stored_doc(0)
    assert doc0["cutoff"] == doc0["k"] == doc0["top_k"] == 6 and doc0["baseline"] is None
    world.run(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    doc1 = world.stored_doc(1)
    assert doc1["cutoff"] == 6 and doc1["baseline"]["cutoff"] == 6 and doc1["baseline"]["run_id"] == r0


def test_a_run_scored_at_a_prereg_cutoff_other_than_six_stores_it(world, monkeypatch):
    set_cutoff(world, 3)
    # the pipeline hash includes the run's top_k (the fusion component): the world recorded its incumbent at the default 6,
    # so for a prereg whose cut-off is 3 the incumbent is the same pipeline hashed at 3 (r4 A)
    monkeypatch.setattr(sealed_run.SealedLedger, "incumbent_config_hash", lambda self: world.config_hash(3))
    outcome = world.run(0, top_k=3)
    doc = world.stored_doc(0)
    assert doc["cutoff"] == doc["k"] == doc["top_k"] == 3
    assert outcome.failed_guards == ("top_k_6",)  # the canonical guard is still hit@6: this run is descriptive only


def test_load_baseline_refuses_a_baseline_whose_stored_cutoff_differs(world):
    candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    ledger, set_sha = world.ledger(), sealed_run._file_sha(world.sealed_path)
    ok = sealed_run.load_baseline(r0, ledger=ledger, set_sha256=set_sha, cutoff=6)
    assert ok["cutoff"] == 6 and ok["run_id"] == r0
    for other in (1, 3, 5, 12):
        with pytest.raises(sealed_run.GateRefused) as info:
            sealed_run.load_baseline(r0, ledger=ledger, set_sha256=set_sha, cutoff=other)
        assert (info.value.gate, info.value.detail) == ("baseline", "cutoff")


def test_a_baseline_stored_without_a_cutoff_is_refused_closed(world, monkeypatch):
    candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    real = sealed_run.json.loads

    def without_cutoff(text, *a, **k):
        doc = real(text, *a, **k)
        if isinstance(doc, dict) and doc.get("kind") == sealed_run.RESULTS_KIND:
            doc.pop("cutoff", None)
        return doc

    monkeypatch.setattr(sealed_run.json, "loads", without_cutoff)
    assert gate_of(lambda: sealed_run.load_baseline(
        r0, ledger=world.ledger(), set_sha256=sealed_run._file_sha(world.sealed_path), cutoff=6)) == "baseline"


# --- the regression: identical rankings, evidence at rank 4 --------------------------------------------------------------


def test_identical_rankings_with_the_evidence_at_rank_four_never_pair_a_hit_at_6_with_a_miss_at_3(world, capsys):
    """The baseline is hit@6, so the evidence at rank 4 is a hit for every eligible family. A candidate run at the
    same ranking but cut-off 3 would miss them all (all baseline-hit / candidate-miss), purely through the cut-off. Every
    route that could produce that pairing refuses; the candidate at the baseline's own cut-off pairs with no change."""
    config_hash = candidate_prereg(world)
    retrieval = rank_four(world)
    out0 = run_with(world, 0, retrieval)
    assert out0.canonical
    doc0 = world.stored_doc(0)
    eligible = {r["family_id"] for r in next(s for s in doc0["sets"] if s["role"] == "sealed")["rows"] if r["scope"] != "refuse"}
    primary = doc0["primary_mode"]
    assert all(
        r["retrieval"][primary]["strict_hit"]
        for r in next(s for s in doc0["sets"] if s["role"] == "sealed")["rows"] if r["scope"] != "refuse"
    )  # the baseline hits every eligible row at rank 4 under cut-off 6
    r0 = run_id_of(world, 0)
    capsys.readouterr()

    # (a) the candidate at cut-off 3: refused by the gate, before the claim, with the slot untouched
    world.ensure_batch(1)
    kind_before = kinds(world)
    with pytest.raises(sealed_run.GateRefused) as info:
        run_with(world, 1, retrieval, top_k=3, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    assert info.value.gate == "cutoff"
    assert kinds(world) == kind_before and world.ledger().recover(1).action is RecoveryAction.NOT_STARTED

    # (b) the prereg is changed to cut-off 3 so that the candidate's own check passes: the baseline (hit@6) is refused
    set_cutoff(world, 3)
    with pytest.raises(sealed_run.GateRefused) as info:
        run_with(world, 1, retrieval, top_k=3, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    assert (info.value.gate, info.value.detail) == ("baseline", "cutoff")
    assert kinds(world) == kind_before and world.ledger().recover(1).action is RecoveryAction.NOT_STARTED

    # (c) the control: at the baseline's cut-off the same ranking pairs with no gain and no loss
    set_cutoff(world, 6)
    outcome = run_with(world, 1, retrieval, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    assert outcome.rendered
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[eval] slot 1 ")]
    assert any(f"families: candidate gains 0, loses 0 of {len(eligible)}" in ln for ln in lines)
    doc1 = world.stored_doc(1)
    assert doc1["baseline"]["hits"] == {f: True for f in eligible} and doc1["cutoff"] == doc1["baseline"]["cutoff"] == 6


# --- the renderer is the second line: a document that pairs two cut-offs is not rendered -----------------------------------


def test_the_renderer_refuses_a_document_that_pairs_two_cutoffs_or_records_none(world):
    config_hash = candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    world.run(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    doc = world.stored_doc(1)
    render = lambda d: sealed_report.format_sealed_report(d, excluded=frozenset(), canonical=False)  # noqa: E731
    assert render(doc)  # the genuine document renders
    assert sealed_report.confirmatory_summary(doc, frozenset())

    mixed = copy.deepcopy(doc)
    mixed["baseline"]["cutoff"] = 3
    for call in (render, lambda d: sealed_report.confirmatory_summary(d, frozenset())):
        with pytest.raises(ValueError, match="different hit cut-offs"):
            call(mixed)
    for mutate in (lambda d: d.pop("cutoff"), lambda d: d.update(cutoff=3), lambda d: d.update(cutoff=True)):
        broken = copy.deepcopy(doc)
        mutate(broken)
        for call in (render, lambda d: sealed_report.confirmatory_summary(d, frozenset())):
            with pytest.raises(ValueError, match="does not record the hit cut-off"):
                call(broken)
    lacking = copy.deepcopy(doc)
    lacking["baseline"].pop("cutoff")
    with pytest.raises(ValueError, match="different hit cut-offs"):
        render(lacking)


# --- fix 3: the gate does not claim unless asked --------------------------------------------------------------------------


def test_the_gate_mints_an_unclaimed_token_by_default(world):
    assert inspect.signature(sealed_run.acquire_slot_token).parameters["claim"].default is False
    token = sealed_run.acquire_slot_token(
        0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
        records_dir=world.records_dir, expansion="live", meter=make_meter(),
    )
    try:
        assert token.claimed == [False] and kinds(world) == ["freeze"]
        assert world.ledger().recover(0).action is RecoveryAction.NOT_STARTED
    finally:
        token.release()


def test_a_library_caller_that_never_claims_gets_the_preflight_before_the_claim(world, fakes):
    """The default path end to end: the unclaimed token is preflighted by ``run_slot`` and only then claimed."""
    calls, state = fakes
    token = sealed_run.acquire_slot_token(
        0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
        records_dir=world.records_dir, expansion="live", meter=make_meter(),
    )
    with pytest.raises(ValueError):
        world.run(0, token=token, top_k=0, matrix_extra=state["kwargs"])  # a refusal the preflight makes
    assert_refused_unclaimed(world, calls)
    token = sealed_run.acquire_slot_token(
        0, ledger=world.ledger(), set_path=world.sealed_path, prereg_path=world.prereg_path,
        records_dir=world.records_dir, expansion="live", meter=make_meter(),
    )
    assert world.run(0, token=token).canonical and "slot_claim" in kinds(world)
