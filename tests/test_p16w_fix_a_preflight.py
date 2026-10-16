"""Phase 16A-2a gate fix A2: every refusal happens before the claim ("a refusal never spends a slot").

The CLI mints the token unclaimed and ``run_slot`` preflights, then claims right before the first
model call. One test per class of refusal the run itself used to make AFTER the claim: the numeric
options, the replay artifact's load and per-row preflight, the embedding model / Chroma index /
BM25 sidecar a default path loads, the absorbed map, and the duplicate checks. Each runs through the
real CLI with the models faked at the evaluator's seams and asserts that the ledger holds no claim
for the slot, that no model, retrieval or expansion call was made, and that the slot's lock is free.
"""

import json

import pytest

import src.evaluator as ev
from src import sealed_run
from src.sealed_ledger import RecoveryAction
from tests.p16_capture import PROVENANCE
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    world,
)
from tests.test_p16w_slot_run_cli import cli
from tests.test_p16w_slot_run_protocol import run_id_of
from tests.test_p16w_spend import make_meter


class Calls:
    """Records any model, expansion or retrieval call the run makes."""

    def __init__(self):
        self.events = []

    def __call__(self, name):
        def record(*args, **kwargs):
            self.events.append(name)
            raise AssertionError(f"a call was made before the claim: {name}")

        return record


def make_call_fakes(world, monkeypatch):
    """The matrix's injection seams (``state["kwargs"]``) and a record of every call made through them."""
    calls = Calls()
    retrieval = world.retrieval()
    spied = {}
    base_gen = world.generator(retrieval)

    def watched(name, fn):
        def inner(*args, **kwargs):
            calls.events.append(name)
            return fn(*args, **kwargs)

        return inner

    spied["generate_fn"] = watched("generate", base_gen)
    spied["judge_fn"] = watched("judge", world.judge())
    spied["provenance_fn"] = lambda: dict(PROVENANCE)
    spied["retrieve_fn_factory"] = lambda mode: watched("retrieve", retrieval.factory(6)(mode))
    monkeypatch.setattr(ev, "expand_query", watched("expand", world.expand()))
    return calls, {"kwargs": spied}


def inject_into_run_slot(fakes, monkeypatch):
    """``fakes``, injected into the CLI's ``run_slot`` the way the e2e test does."""
    _calls, state = fakes
    real_run_slot = sealed_run.run_slot
    monkeypatch.setattr(
        sealed_run, "run_slot", lambda token, **kw: real_run_slot(token, **{"matrix_kwargs": state["kwargs"], **kw})
    )
    return fakes


@pytest.fixture
def fakes(world, monkeypatch):
    """The matrix's injection seams (``state["kwargs"]``) and a record of every call made through them."""
    return make_call_fakes(world, monkeypatch)


@pytest.fixture
def cli_fakes(fakes, monkeypatch):
    """``fakes``, injected into the CLI's ``run_slot`` the way the e2e test does."""
    return inject_into_run_slot(fakes, monkeypatch)


def assert_refused_unclaimed(world, calls, slot=0):
    assert not any(r["kind"] == "slot_claim" and r["slot"] == str(slot) for r in world.ledger().records())
    assert world.ledger().recover(slot).action is RecoveryAction.NOT_STARTED
    assert calls.events == [], "nothing may be called before the claim"
    with world.ledger().lock_slot(slot):  # the failed attempt does not keep the slot busy
        pass


# --- numeric options ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("flag,value", [("--top-k", "0"), ("--top-k", "-3"), ("--judge-sample", "-1")])
def test_invalid_numeric_options_are_a_usage_error_and_spend_no_slot(world, cli_fakes, monkeypatch, capsys, flag, value):
    calls, _state = cli_fakes
    code, out, err = cli(monkeypatch, capsys, world, flag, value)
    assert code == 2 and "no slot was touched" in err
    assert_refused_unclaimed(world, calls)
    assert kinds(world) == ["freeze"]


def test_the_library_path_refuses_invalid_options_before_the_claim_too(world, fakes):
    """run_slot on an unclaimed token (no CLI in front): the preflight is what refuses, before the claim."""
    calls, state = fakes
    for kw in ({"top_k": 0}, {"judge_sample": -1}):
        token = world.token(0, claim=False)
        with pytest.raises(ValueError):
            world.run(0, token=token, matrix_extra=state["kwargs"], **kw)
        assert_refused_unclaimed(world, calls)
    assert kinds(world) == ["freeze"]


# --- the replay artifact -------------------------------------------------------------------------------------


def test_a_replay_artifact_that_does_not_cover_the_sets_spends_no_slot(
    world, cli_fakes, tmp_path, monkeypatch, capsys, eval_registry
):
    """The artifact was frozen by slot 0; a (public) regression set given now has a row it never saw.

    A private development set must be its frozen version (``dev_binding``), which slot 0 covered, so the
    uncovered set here is a registered public one, which the binding leaves alone."""
    calls, _state = cli_fakes
    world.run(0)
    capsys.readouterr()
    calls.events.clear()
    r0 = run_id_of(world, 0)
    artifact = world.run_dir(r0) / "expansion_artifact.json"
    assert sealed_run._file_sha(artifact) == world.ledger().canonical_artifact_sha256()  # slot 1 (a replay entry) replays it
    rows = [json.loads(line) for line in world.realistic_path.read_text().splitlines()]
    extra = dict(rows[0], id="f30000009-0", family_id="f30000009", question=rows[0]["question"] + " again")
    newer = tmp_path / "dev" / "realistic_v2.jsonl"
    newer.write_text("".join(json.dumps(r) + "\n" for r in [*rows, extra]))
    eval_registry.add(newer, privacy="public", name="regression-newer")
    calls.events.clear()
    claims_before = kinds(world).count("slot_claim")
    code, out, err = cli(
        monkeypatch, capsys, world, slot="1", base=[
            "--golden", str(world.golden_path), "--heldout", str(world.sealed_path), "--realistic", str(newer),
            "--sealed-slot", "1", "--prereg", str(world.prereg_path), "--slot-records", str(world.records_dir),
            "--judge", "--expansion", str(artifact), "--baseline-run", r0,
        ],
    )
    assert code == 1 and "ExpansionArtifactError" in err
    assert kinds(world).count("slot_claim") == claims_before  # slot 1 was not claimed
    assert world.ledger().recover(1).action is RecoveryAction.NOT_STARTED
    assert calls.events == []
    with world.ledger().lock_slot(1):
        pass


# --- the index a default path loads ---------------------------------------------------------------------------


class _Store:
    def __init__(self, ids):
        self._ids = ids

    def get(self, limit=None):
        return {"ids": list(self._ids)}


@pytest.fixture
def backends(monkeypatch, fakes):
    """Default retrieval (no injected factory), with the embedding model, Chroma store and BM25 sidecar faked."""
    _calls, state = fakes
    state["kwargs"] = {k: v for k, v in state["kwargs"].items() if k != "retrieve_fn_factory"}
    cfg = {"manifest": None, "ids": ["chunk-1"], "bm25": object(), "loaded": []}

    def assert_model(persist_directory, *a, **k):
        cfg["loaded"].append("manifest")
        if cfg["manifest"] is not None:
            raise cfg["manifest"]

    def vector_store(persist_directory=None, **k):
        cfg["loaded"].append("vector")
        return _Store(cfg["ids"])

    def bm25(persist_directory):
        cfg["loaded"].append("bm25")
        return cfg["bm25"]

    monkeypatch.setattr(ev, "assert_embedding_model", assert_model)
    monkeypatch.setattr(ev, "get_vector_store", vector_store)
    monkeypatch.setattr(ev, "load_bm25_index", bm25)
    return cfg


@pytest.mark.parametrize(
    "break_it,detail",
    [
        pytest.param(lambda cfg: cfg.update(manifest=ValueError("embedding model mismatch")), "ValueError", id="embedding-model"),
        pytest.param(lambda cfg: cfg.update(ids=[]), "LookupError", id="empty-chroma-index"),
        pytest.param(lambda cfg: cfg.update(bm25=None), "LookupError", id="missing-bm25-sidecar"),
    ],
)
def test_a_missing_or_mismatched_index_spends_no_slot(world, cli_fakes, backends, monkeypatch, capsys, break_it, detail):
    calls, _state = cli_fakes
    break_it(backends)
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 4 and f"gate=retrieval_backend ({detail})" in err
    assert "mismatch" not in err  # the class name is the only detail printed, never the message
    assert_refused_unclaimed(world, calls)


def preflight(world, state, modes):
    token = world.token(0, claim=False)
    try:
        return sealed_run.preflight_slot(
            token, set_specs=world.specs, modes=modes, top_k=6, skip_refusals=False, skip_completeness=False,
            judge=True, judge_sample=None, persist_directory="unused", meter=make_meter(), expansion="live",
            matrix_kwargs=state["kwargs"],
        )
    finally:
        token.release()


def test_the_preflight_with_a_healthy_index_loads_it_and_claims_nothing(world, fakes, backends):
    calls, state = fakes
    assert preflight(world, state, list(ev.EVAL_MODES)) is None
    assert backends["loaded"] == ["manifest", "vector", "bm25"]  # the positive control: it really was probed
    assert kinds(world) == ["freeze"] and calls.events == []


def test_only_the_arms_in_play_are_probed(world, fakes, backends):
    """A vector-only matrix needs no BM25 sidecar; a bm25-only one needs no Chroma index. (Answer passes injected.)"""
    calls, state = fakes
    backends.update(bm25=None)
    assert preflight(world, state, ["vector"]) is None
    assert backends["loaded"] == ["manifest", "vector"]
    with pytest.raises(sealed_run.GateRefused) as info:
        preflight(world, state, ["bm25"])
    assert (info.value.gate, info.value.detail) == ("retrieval_backend", "LookupError")
    backends.update(ids=[], bm25=object())
    assert preflight(world, state, ["bm25"]) is None  # an empty Chroma index does not matter to a bm25-only run


# --- the absorbed map and the duplicate checks -------------------------------------------------------------------


def test_a_broken_absorbed_map_spends_no_slot(world, cli_fakes, monkeypatch, capsys):
    calls, _state = cli_fakes

    def corrupt():
        raise ValueError("corrupt absorbed map")

    monkeypatch.setattr(ev, "_load_absorbed_map", corrupt)
    code, out, err = cli(monkeypatch, capsys, world)
    assert code == 1 and "error: ValueError" in err
    assert_refused_unclaimed(world, calls)


def test_a_development_set_that_repeats_a_question_spends_no_slot(bare, monkeypatch, capsys):
    """A v1 golden set with one question twice passes the CLI's load check and is refused by the cohort check.

    The set is the frozen tuning version (a development set must be, ``dev_binding``): it is written before the freeze.
    """
    row = {"question": "A synthetic question that appears twice?", "type": "direct", "expected_sections": ["901.1"]}
    bare.golden_path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    bare.write_prereg()
    bare.write_records()
    bare.freeze()
    calls, _state = inject_into_run_slot(make_call_fakes(bare, monkeypatch), monkeypatch)
    code, out, err = cli(monkeypatch, capsys, bare)
    assert code == 1 and "error: CohortError" in err
    assert_refused_unclaimed(bare, calls)


def test_a_duplicate_row_id_inside_a_set_is_refused_by_the_preflight(world, fakes, monkeypatch):
    """The C4 duplicate-id check runs on the token's rows before the claim (the sealed loader forbids it earlier,
    so the check is exercised with a doubled row on the token)."""
    calls, state = fakes
    token = world.token(0, claim=False)
    doubled = tuple([*token.rows, token.rows[0]])
    object.__setattr__(token, "rows", doubled)  # a frozen dataclass: this is the only way to model a bad token
    with pytest.raises(ValueError, match="duplicate row id"):
        world.run(0, token=token, matrix_extra=state["kwargs"])
    assert_refused_unclaimed(world, calls)


# --- the order: preflight, then the claim, then the first call ---------------------------------------------------------


def test_the_claim_comes_after_the_preflight_and_before_the_first_model_call(world, monkeypatch):
    events = []
    real_claim = sealed_run.SealedLedger.claim

    def claim(self, slot):
        events.append("claim")
        return real_claim(self, slot)

    monkeypatch.setattr(sealed_run.SealedLedger, "claim", claim)
    real_preflight = sealed_run.preflight_slot
    monkeypatch.setattr(
        sealed_run, "preflight_slot", lambda *a, **k: (events.append("preflight"), real_preflight(*a, **k))[1]
    )
    expand = world.expand()
    monkeypatch.setattr(ev, "expand_query", lambda *a, **k: (events.append("expand"), expand(*a, **k))[1])
    outcome = world.run(0, token=world.token(0, claim=False))
    assert outcome.canonical
    assert events[:3] == ["preflight", "claim", "expand"] and events.count("claim") == 1


def test_a_matrix_run_on_an_unclaimed_token_is_refused_before_any_call(world, fakes):
    calls, state = fakes
    token = world.token(0, claim=False)
    with pytest.raises(sealed_run.GateRefused) as info:
        ev.run_eval_matrix(
            world.specs, privacy="private", meter=make_meter(), slot_token=token, expansion="live", judge=True,
            **state["kwargs"],
        )
    assert (info.value.gate, info.value.detail) == ("claim", "unclaimed")
    assert calls.events == [] and kinds(world) == ["freeze"]
