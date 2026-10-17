"""16A-2b b1a pre-waiver fixes (Codex r8 B1/M3/m4, auditor r8 M1/M2/m3): what the five W-items add that the r4-r7 tests did not pin.

* W1: a slot's stored results and its rows sidecar carry ``judge_runtime_sha256`` (the judge-path package versions), which is
  outside the registered ``judge.config_hash`` (the identity tests are in ``tests/test_p16a2b_b1a_r7_config.py``).
* W2: the scanner's own ``s_hit`` exclusion after slot 0 is stored is accepted and records the slot whose report it scanned
  (``scan_slot``); the owner's command refuses every reason there (``tests/test_p16a2b_b1a_r7_tool.py``).
* W5: the prereg no longer claims that ``eval_sets`` are excluded from the incumbent's identity, and the identity's module
  docstring states what ``scan_inputs`` covers.

Synthetic data only (``tests/p16_slot.py``, ``tests/test_p16a2b_b1a_r6_draws.py``'s isolated ledger); no network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import judge, pipeline_config, sealed_run
from src.sealed_ledger import ProtocolError, SealedLedger
from tests.p16_slot import _langsmith_env_cache, bare, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r6_draws import isolated
from tests.test_p16w_slot_run_protocol import leaky

ROOT = Path(__file__).resolve().parent.parent


# =================================================================================================================================
# W1: the slot's results and sidecar record judge_runtime_sha256
# =================================================================================================================================
def test_a_slots_stored_results_and_rows_sidecar_carry_the_judge_runtime_digest(world):
    assert world.run(0).canonical is True
    doc = world.stored_doc(0)
    rows = json.loads((world.run_dir(doc["run_id"]) / "rows.json").read_text())
    identity = judge.judge_identity()
    assert doc["judge_runtime_sha256"] == rows["judge_runtime_sha256"] == identity["judge_runtime_sha256"]
    assert len(doc["judge_runtime_sha256"]) == 64
    # the registered value is what it was: the runtime is recorded beside the judge block, never inside it
    assert doc["judge"] == {"config_hash": identity["config_hash"], "sample": None}
    assert doc["facts"]["judge_config_hash"] == doc["judge"]["config_hash"]


def test_the_sidecar_carries_the_judge_runtime_digest_only_when_given():
    from src.eval_cohort import build_sidecar

    kwargs = dict(privacy="private", cohorts=[], rows=[], expansion={"kind": "none"})
    assert "judge_runtime_sha256" not in build_sidecar(**kwargs)  # a draw sidecar has no judge
    assert build_sidecar(**kwargs, judge_runtime_sha256="a" * 64)["judge_runtime_sha256"] == "a" * 64


# =================================================================================================================================
# W2: a scanner-written s_hit exclusion after slot 0 is stored is accepted and names the scanned slot
# =================================================================================================================================
def test_a_scanner_written_s_hit_after_slot_0_is_stored_is_accepted_and_carries_scan_slot(world, monkeypatch):
    victim = world.families[4]  # a singleton group with one eligible family
    render, _leaks = leaky(world, [victim])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    outcome = world.run(0)
    assert outcome.canonical and outcome.published and outcome.scan_exclusions == 1
    assert world.ledger().stored_slots() == ["0"]
    (record,) = world.ledger().records("exclusion")
    assert (record["group_id"], record["reason"]) == (victim, "s_hit")
    assert record["slots_stored"] == ["0"]  # slot 0's results were stored when the scan found the hit ...
    assert record["scan_slot"] == "0"  # ... and slot 0's report is the one that was scanned


def test_the_exclusion_record_names_exactly_the_slot_of_the_scanned_document(world, monkeypatch):
    """``render_and_publish`` reads the slot from the stored document it scans, not from anywhere the caller could vary."""
    seen = []
    real = SealedLedger.exclude

    def spy(self, group_id, reason, members=None, **kwargs):
        seen.append((reason, kwargs))
        return real(self, group_id, reason, members, **kwargs)

    monkeypatch.setattr(SealedLedger, "exclude", spy)
    render, _ = leaky(world, [world.families[4], world.families[5]])
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    assert world.run(0).scan_exclusions == 2
    assert seen == [("s_hit", {"scan_slot": "0"})] * 2
    assert {r["scan_slot"] for r in world.ledger().records("exclusion")} == {"0"}


def store(led, tmp_path, slot="0"):
    run_dir = tmp_path / f"run-{slot}"
    run_dir.mkdir()
    (run_dir / "results.json").write_text("{}", encoding="utf-8")
    led.claim(slot)
    led.store_results(slot, run_dir)


def test_the_ledger_records_scan_slot_only_for_an_s_hit_of_a_stored_slot(tmp_path):
    led = isolated(tmp_path)
    led.exclude("g1", "exposure")  # before any slot: no scan, no scan_slot
    assert "scan_slot" not in led.records("exclusion")[0]
    with pytest.raises(ProtocolError):
        led.exclude("g2", "s_hit", scan_slot="0")  # a scan runs on a stored document
    store(led, tmp_path)
    led.exclude("g3", "s_hit", scan_slot="0")
    assert [r.get("scan_slot") for r in led.records("exclusion")] == [None, "0"]
    with pytest.raises(ProtocolError):
        led.exclude("g4", "s_hit", scan_slot="1")  # slot 1's results are not stored
    assert [r["group_id"] for r in led.records("exclusion")] == ["g1", "g3"]  # the refused ones wrote nothing


@pytest.mark.parametrize("scan_slot", ["", "x", "00", "1000", "0B", 0, b"0"])
def test_a_scan_slot_that_is_not_a_slot_id_is_refused(tmp_path, scan_slot):
    led = isolated(tmp_path)
    store(led, tmp_path)
    with pytest.raises(ValueError):
        led.exclude("g1", "s_hit", scan_slot=scan_slot)
    assert led.records("exclusion") == []


@pytest.mark.parametrize("reason", ["exposure", "twin_dev_version"])
def test_only_an_s_hit_can_name_a_scanned_slot(tmp_path, reason):
    led = isolated(tmp_path)
    store(led, tmp_path)
    with pytest.raises(ValueError):
        led.exclude("g1", reason, scan_slot="0")
    assert led.records("exclusion") == []


def test_the_scanner_is_the_only_src_caller_of_exclude_and_it_always_names_the_slot():
    import ast

    for path in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "exclude" \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "ledger":
                assert path.name == "sealed_run.py" and "scan_slot" in {k.arg for k in node.keywords}, (path.name, node.lineno)


# =================================================================================================================================
# W5: the wording of what the incumbent's identity excludes and covers
# =================================================================================================================================
def test_the_prereg_does_not_claim_that_the_eval_sets_are_excluded_from_the_incumbent_identity():
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    excluded = prereg["incumbent"]["excluded"]
    assert excluded == ["timeouts_retries", "git_sha", "paths", "eval_set_contents_bound_by_freeze_records", "corpus_text"]
    assert "eval_sets" not in excluded
    assert "scan_inputs" in prereg["incumbent"]["components"]  # the public sets, registry, legacy list and handbook dump are hashed


def test_the_identity_docstring_states_what_the_scan_inputs_cover_and_what_is_left_to_the_freeze_records():
    doc = " ".join((pipeline_config.__doc__ or "").split())
    assert "``scan_inputs``" in doc
    for covered in ("registry", "legacy list", "public sets", "handbook dump"):
        assert covered in doc, covered
    assert "freeze records" in doc and "contents of the eval sets" in doc
    assert "the eval sets and any corpus text" not in doc  # the old wording: it excluded the sets wholesale
