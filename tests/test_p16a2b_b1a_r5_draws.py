"""16A-2b b1a closure round r5, part B: the candidate-draw cap (R5, owner-chosen). A development draw counts only if
it was recorded in the ledger (a ``dev_draw`` record; since r6 by ``pipeline eval --record-draw`` inside the run, tested in
``tests/test_p16a2b_b1a_r6_draws.py``, and no longer by a ``sealed_tool`` command); at most the incumbent and four candidates
may ever have records; ``controls.py`` computes the controls from the first recorded draws; ``declare-batch`` needs the
recorded draws to cover the batch; ``prereg-check`` registers all of it.

Synthetic data only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import pipeline_config, sealed_ledger
from src.sealed_ledger import DrawCoverageRefused, LedgerCorrupt, ProtocolError, SealedLedger
from tests.p16w_sealed_world import repos, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r3_tool import put
from tests.test_p16a2b_b1a_tool import file_sha, record_controls, record_draws, sealed_frozen
from tests.test_p16w_sealed_tool import assert_allowlisted

ROOT = Path(__file__).resolve().parent.parent
D = "a" * 64
CONFIGS = [f"{n}" * 64 for n in "123456"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make(tmp_path: Path) -> SealedLedger:
    return SealedLedger(tmp_path / "copy", mirror=tmp_path / "mirror.jsonl")


def with_controls(tmp_path: Path, incumbent: str = CONFIGS[0]) -> SealedLedger:
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=incumbent)
    return led


def draw(led: SealedLedger, config: str, n: int) -> int:
    return led.record_dev_draw(config, sha(f"artifact {config} {n}"), sha(f"sidecar {config} {n}"), f"run-{config[:4]}-{n}")


# --- the ledger record ---------------------------------------------------------------------------------------------------------
def test_the_ledger_assigns_a_per_configuration_index_and_lists_draws_in_order(tmp_path):
    led = with_controls(tmp_path)
    assert [draw(led, CONFIGS[0], n) for n in range(3)] == [1, 2, 3]
    assert [draw(led, CONFIGS[1], n) for n in range(2)] == [1, 2]  # a configuration has its own count
    assert draw(led, CONFIGS[0], 3) == 4
    records = led.dev_draws(CONFIGS[0])
    assert [r["draw_index"] for r in records] == [1, 2, 3, 4]
    assert [r["rows_sidecar_sha256"] for r in records] == [sha(f"sidecar {CONFIGS[0]} {n}") for n in range(4)]
    assert [r["artifact_sha256"] for r in records] == [sha(f"artifact {CONFIGS[0]} {n}") for n in range(4)]
    assert all(r["kind"] == "dev_draw" and r["config_hash"] == CONFIGS[0] for r in records)
    assert [r["execution_receipt"] for r in records] == [f"run-{CONFIGS[0][:4]}-{n}" for n in range(4)]  # r6: one receipt per draw
    assert led.dev_draws(CONFIGS[2]) == []
    assert led.dev_draw_configs() == [CONFIGS[0], CONFIGS[1]]  # in the order they were first recorded
    # a fresh handle reads the same truth
    assert make(tmp_path).dev_draws(CONFIGS[1])[1]["draw_index"] == 2


def test_the_mirror_carries_the_three_digests_only(tmp_path):
    led = with_controls(tmp_path)
    draw(led, CONFIGS[0], 0)
    line = json.loads((tmp_path / "mirror.jsonl").read_text().splitlines()[-1])
    assert line["kind"] == "dev_draw"
    assert line["digests"] == {
        "config_hash": CONFIGS[0], "artifact_sha256": sha(f"artifact {CONFIGS[0]} 0"),
        "rows_sidecar_sha256": sha(f"sidecar {CONFIGS[0]} 0"),
    }
    assert set(line) == {"seq", "kind", "digest", "digests"}  # no index, no counts


@pytest.mark.parametrize("bad", ["", "short", "A" * 64, "g" * 64, None, 5])
def test_the_three_fields_must_be_lowercase_sha256_digests(tmp_path, bad):
    led = with_controls(tmp_path)
    for position in range(3):
        args = [CONFIGS[0], D, D, "run-x"]
        args[position] = bad
        with pytest.raises(ValueError):
            led.record_dev_draw(*args)
    assert led.records("dev_draw") == []


def test_at_most_five_distinct_configurations_may_ever_have_records(tmp_path):
    led = with_controls(tmp_path)
    assert sealed_ledger.DEV_DRAW_CONFIGS_MAX == 1 + sealed_ledger.K_MAX == 5
    for config in CONFIGS[:5]:  # the incumbent and four candidates
        draw(led, config, 0)
    with pytest.raises(ProtocolError, match="at most 5"):
        draw(led, CONFIGS[5], 0)  # a sixth distinct hash
    assert led.dev_draw_configs() == CONFIGS[:5] and led.dev_draws(CONFIGS[5]) == []
    assert draw(led, CONFIGS[4], 1) == 2 and draw(led, CONFIGS[0], 1) == 2  # a configuration already in may go on


def test_before_the_controls_exist_only_one_configuration_may_be_recorded(tmp_path):
    led = make(tmp_path)
    led.record_freeze(split="sealed", set_sha256=D, twins_sha256=D, totals={"families": 60})
    draw(led, CONFIGS[0], 0)
    assert draw(led, CONFIGS[0], 1) == 2  # the incumbent's draws go on
    with pytest.raises(ProtocolError, match="controls"):
        draw(led, CONFIGS[1], 0)
    assert led.dev_draw_configs() == [CONFIGS[0]]
    led.record_controls(controls_sha256=D, draws_sha256=D, prereg_sha256=D, incumbent_config_hash=CONFIGS[0])
    assert draw(led, CONFIGS[1], 0) == 1  # a candidate's draws may follow the controls


def test_a_rows_sidecar_cannot_be_recorded_twice(tmp_path):
    led = with_controls(tmp_path)
    led.record_dev_draw(CONFIGS[0], sha("a1"), sha("s1"), "run-a")
    for n, config in enumerate((CONFIGS[0], CONFIGS[1])):
        with pytest.raises(ProtocolError, match="already recorded"):
            led.record_dev_draw(config, sha("a2"), sha("s1"), f"run-b{n}")  # a copy is not another draw
    assert len(led.dev_draws(CONFIGS[0])) == 1 and led.dev_draws(CONFIGS[1]) == []
    led.record_dev_draw(CONFIGS[0], sha("a1"), sha("s2"), "run-c")  # the same artifact digest is not a copy of the draw
    assert len(led.dev_draws(CONFIGS[0])) == 2


def test_the_ledger_refuses_to_replay_records_that_break_the_cap_or_the_single_incumbent_rule(tmp_path):
    """A hand-edited ledger (the cap bypassed by writing the records directly) is corrupt, never read as valid."""
    led = with_controls(tmp_path)
    for config in CONFIGS[:5]:
        draw(led, config, 0)
    with led._txn() as txn:  # bypasses record_dev_draw's checks
        txn.append("dev_draw", config_hash=CONFIGS[5], draw_index=1, artifact_sha256=D, rows_sidecar_sha256=sha("x"),
                   execution_receipt="run-x")
    with pytest.raises(LedgerCorrupt):
        make(tmp_path).records()
    other = tmp_path / "other"
    other.mkdir()
    led2 = SealedLedger(other / "copy", mirror=other / "mirror.jsonl", ledger=other / "ledger.jsonl")
    draw(led2, CONFIGS[0], 0)
    with led2._txn() as txn:  # a second configuration before any controls record
        txn.append("dev_draw", config_hash=CONFIGS[1], draw_index=1, artifact_sha256=D, rows_sidecar_sha256=sha("y"),
                   execution_receipt="run-y")
    with pytest.raises(LedgerCorrupt):
        led2.records()


# --- declare-batch coverage (the ledger rule; the command passes the prereg's draws_r) ---------------------------------------------
@pytest.fixture(autouse=True)
def _embargo_off(monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)


def entries(*configs: str):
    return [{"config_hash": c, "expansion": "replay"} for c in configs]


def test_every_batch_candidate_needs_draws_r_recorded_draws(tmp_path):
    led = with_controls(tmp_path)
    for n in range(5):
        draw(led, CONFIGS[1], n)
    for n in range(4):  # one short
        draw(led, CONFIGS[2], n)
    with pytest.raises(DrawCoverageRefused, match="fewer recorded"):
        led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[2]), draws_required=5)
    with pytest.raises(DrawCoverageRefused, match="fewer recorded"):  # a candidate with none
        led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[3]), draws_required=5)
    assert led.batch_declaration() is None
    draw(led, CONFIGS[2], 4)
    led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[2]), draws_required=5)
    assert [c["config_hash"] for c in led.batch_declaration()["candidates"]] == [CONFIGS[1], CONFIGS[2]]


def test_every_recorded_candidate_must_be_in_the_batch_but_the_incumbent_need_not_be(tmp_path):
    led = with_controls(tmp_path)  # the incumbent is CONFIGS[0]
    for config in (CONFIGS[0], CONFIGS[1], CONFIGS[2]):
        for n in range(5):
            draw(led, config, n)
    with pytest.raises(DrawCoverageRefused, match="not in the batch"):  # CONFIGS[2] was tried and recorded, then left out
        led.record_batch_declaration(entries(CONFIGS[1]), draws_required=5)
    assert led.batch_declaration() is None
    led.record_batch_declaration(entries(CONFIGS[1], CONFIGS[2]), draws_required=5)  # all recorded candidates count toward K
    assert len(led.batch_declaration()["candidates"]) == 2


def test_the_coverage_rules_cannot_be_skipped(tmp_path):
    """r6 (Codex 4): ``draws_required`` has no default, so a library caller cannot declare an uncovered batch. (r5 accepted the
    bypass here; the full tests are in ``tests/test_p16a2b_b1a_r6_draws.py``.)"""
    led = with_controls(tmp_path)
    with pytest.raises(TypeError):
        led.record_batch_declaration(entries(CONFIGS[3]))
    with pytest.raises(DrawCoverageRefused):
        led.record_batch_declaration(entries(CONFIGS[3]), draws_required=5)
    assert led.batch_declaration() is None


def ready(world):
    """A frozen world with the controls recorded (the incumbent is ``f * 64``) and the epoch's prereg registered."""
    sealed_frozen(world)
    record_controls(world)


# --- declare-batch on the command line: the coverage rules --------------------------------------------------------------------------------
def test_declare_batch_needs_each_candidate_to_have_its_draws_recorded_and_every_recorded_candidate_declared(world):
    ready(world)  # the incumbent is f * 64
    a, b = "1" * 64, "2" * 64
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err and out == ""  # no draws recorded for it
    record_draws(world.ledger(), a, n=4)
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err  # four of the prereg's five
    record_draws(world.ledger(), a, n=1, start=4)
    record_draws(world.ledger(), b)  # a second candidate is tried and recorded ...
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay")
    assert code == st.EXIT_REFUSED and "reason=draws_uncovered" in err  # ... and left out of the batch
    assert world.ledger().batch_declaration() is None
    record_draws(world.ledger(), "f" * 64)  # the incumbent's own draws are not a candidate
    code, out, err = world.run("declare-batch", "--candidate", f"{a}:replay", "--candidate", f"{b}:replay")
    assert code == 0, err
    assert [c["config_hash"] for c in world.ledger().batch_declaration()["candidates"]] == [a, b]


# --- controls.py: the draws are the recorded draws ----------------------------------------------------------------------------------------
def test_the_registered_values_of_the_draw_cap_are_in_the_prereg_table_and_the_real_prereg():
    expected = {
        "adoption.development_evidence.recorded_configs_max": 4,
        "adoption.development_evidence.draws_recorded_by": "pipeline_eval_record_draw_in_run",  # r6: in the run
        "adoption.development_evidence.unrecorded_runs": "never_count_and_are_logged_per_config",
        "adoption.development_evidence.all_recorded_candidates_declared": True,
    }
    doc = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    for path, value in expected.items():
        assert st.REGISTERED[path] == value and st._eq(st._at(doc, path), value), path
        changed = (not value) if isinstance(value, bool) else (value + 1 if isinstance(value, int) else value + "_x")
        assert (path, "mismatch") in st.validate_prereg(put(doc, path, changed), recompute=False), path
        assert (path, "mismatch") in st.validate_prereg(put(doc, path, KeyError), recompute=False), path
    assert doc["adoption"]["development_evidence"]["recorded_configs_max"] == sealed_ledger.K_MAX
    assert st.validate_prereg(put(doc, "adoption.development_evidence.extra", 1), recompute=False)  # unregistered leaf


def test_the_code_excluded_list_is_registered_and_prereg_check_recomputes_it_from_the_code(monkeypatch):
    doc = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert doc["incumbent"]["code"] == "all_src_modules_except_code_excluded_and_hashed_scripts"  # r6
    assert doc["incumbent"]["code_excluded"] == list(pipeline_config.CODE_EXCLUDED) == st.REGISTERED["incumbent.code_excluded"]
    assert "scoring" not in doc["incumbent"]["excluded"]  # scoring code is hashed now
    assert st.computed_errors(doc) == []
    # a prereg that names another list is refused by prereg-check, even where the table (typed values) would pass it
    for other in (doc["incumbent"]["code_excluded"][:-1], [*doc["incumbent"]["code_excluded"], "evaluator.py"],
                  list(reversed(doc["incumbent"]["code_excluded"]))):
        assert ("incumbent.code_excluded", "mismatch") in st.computed_errors(put(doc, "incumbent.code_excluded", other))
    # ... and the code growing its list without the prereg is refused the same way
    monkeypatch.setattr(pipeline_config, "CODE_EXCLUDED", (*pipeline_config.CODE_EXCLUDED, "zzz.py"))
    assert ("incumbent.code_excluded", "mismatch") in st.computed_errors(doc)
