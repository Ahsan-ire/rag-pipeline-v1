"""16A-2b b1a closure round r3, the ``sealed_tool`` parts: ``count-migrated``'s default tuning quota and drafts rule (F2,
F12), ``prereg-check`` over every registered value (F4, F8, F9, F10, F12 wording), the epoch's prereg after a data dispatch
(F7) and the read-only ``epoch`` command (F13).

Synthetic fixtures only (``tests/p16w_sealed_world.py``); the real ``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from tests.p16w_sealed_world import b1a_blocks, read_rows, repos, standard_released, world  # noqa: F401  (fixtures)
from tests.test_p16a2b_b1a_r2_tool import _migrated  # the v1-set builder for count-migrated
from tests.test_p16w_sealed_tool import assert_allowlisted

ROOT = Path(__file__).resolve().parent.parent
REAL_PREREG = ROOT / "eval" / "prereg_16a2.json"

#: the registered quota of eval/prereg_16a2.json (sealed already holds the spare: sealed = floor_sealed + spare)
REAL_QUOTA = {"sealed": 60, "spare": 10, "floor_sealed": 50, "floor_answerable": 35, "realistic_min": 42, "tuning_min": 60}


def real_prereg() -> dict:
    return json.loads(REAL_PREREG.read_text(encoding="utf-8"))


def file_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- F2 / F12: count-migrated -----------------------------------------------------------------------------------------------
def _real_quota_world(world) -> None:
    world.write_prereg(quota=dict(REAL_QUOTA))


def test_fifty_migrated_tuning_families_with_nineteen_refusals_are_in_band_once_the_pool_is_the_splits(
    world, eval_registry, tmp_path,
):
    """Codex's example: 50 migrated tuning families (19 refusals), 20 realistic, 100 drafts. The tuning quota is what
    cmd_split would give (pool 170 - sealed 60 - realistic 42 = 68), not the bare tuning_min (60): 19 refusals are under
    30 % of 68 (20), over 30 % of 60 (18)."""
    _real_quota_world(world)
    run = _migrated(world, eval_registry, tmp_path, gold=["refusal"] * 19 + ["direct"] * 31, real=["direct"] * 20, tutorial=0)
    code, out, err = run("--drafts", "100")
    assert code == 0, err
    line = world.last(out, "count_migrated")
    assert (line["migrated_families"], line["pool_total"], line["drafts_rule"]) == ("70", "170", "explicit")
    assert (line["tuning_quota"], line["tuning_open"], line["tuning_band"]) == ("68", "18", "ok")
    assert line["tuning_quota"] == str(st.compute_quotas(REAL_QUOTA, 170, 20)["tuning"])  # the split's own number
    assert (line["realistic_quota"], line["realistic_band"]) == ("42", "ok")
    assert_allowlisted(out)


def test_an_explicit_tuning_quota_still_overrides_the_default_and_the_default_is_the_rest_after_sealed_and_realistic(
    world, eval_registry, tmp_path,
):
    _real_quota_world(world)
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 30, real=["direct"] * 10, tutorial=0)
    code, out, err = run("--drafts", "80")  # 40 migrated + 80 = 120: tuning 120 - 60 - 42 = 18, fewer than the 30 present
    line = world.last(out, "count_migrated")
    assert (line["pool_total"], line["tuning_quota"], line["tuning_open"]) == ("120", "18", "0")
    code, out, err = run("--drafts", "80", "--quota-tuning", "70", "--quota-realistic", "50")
    line = world.last(out, "count_migrated")
    assert (line["tuning_quota"], line["realistic_quota"]) == ("70", "50")


def test_the_default_drafts_make_the_first_split_feasible_with_57_migrated_families(world, eval_registry, tmp_path):
    """Au M7: 57 migrated families and families_approx 100 give a pool of 157, below sealed 60 + tuning_min 60 + realistic
    42 = 162, so attempt 1 would be quota_short and burn a capped attempt. The default drafts follow the registered rule
    (prereg ``drafting_rule.attempt_1.families_rule``): max(families_approx, sealed + tuning_min + realistic - migrated)."""
    _real_quota_world(world)
    assert world.prereg_doc()["drafting_rule"]["attempt_1"]["families_approx"] == 100
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 40, real=["direct"] * 16, tutorial=1)  # 40 + 17 = 57
    code, out, err = run()
    assert code == 0, err
    line = world.last(out, "count_migrated")
    assert line["migrated_families"] == "57"
    # r4 D1: and the registered draft_margin (5) on top, so a few rejected drafts at the owner's review do not leave it short
    assert (line["drafts"], line["drafts_rule"], line["pool_total"]) == ("110", "pool_floor", "167")  # not 100 / 157
    assert line["draft_margin"] == "5"
    assert (line["tuning_quota"], line["realistic_quota"]) == ("65", "42") and "tuning_band=ok" in out
    # what cmd_split does with that pool: its tuning quota reaches tuning_min (no quota_short), where 100 drafts would not,
    # and keeps the margin as headroom (the 105 drafts of the bare pool floor made exactly tuning_min)
    assert st.compute_quotas(REAL_QUOTA, 167, 17)["tuning"] == 65 >= REAL_QUOTA["tuning_min"]
    assert st.compute_quotas(REAL_QUOTA, 162, 17)["tuning"] >= REAL_QUOTA["tuning_min"]
    assert st.compute_quotas(REAL_QUOTA, 157, 17)["tuning"] < REAL_QUOTA["tuning_min"]
    # `spare` is already inside `sealed` (sealed = floor_sealed + spare), so the rule adds nothing for it
    assert REAL_QUOTA["sealed"] == REAL_QUOTA["floor_sealed"] + REAL_QUOTA["spare"]
    assert_allowlisted(out)


def test_the_default_drafts_are_the_larger_of_families_approx_and_the_pool_floor(world, eval_registry, tmp_path):
    _real_quota_world(world)
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 30, real=["direct"] * 5, tutorial=1)  # 36 migrated
    code, out, err = run()
    line = world.last(out, "count_migrated")  # 60 + 60 + 42 - 36 = 126 > 100: the pool floor binds here too; + margin 5
    assert (line["drafts"], line["drafts_rule"]) == ("131", "pool_floor")
    code, out, err = run("--drafts", "400")
    assert world.last(out, "count_migrated")["drafts_rule"] == "explicit"
    # a small quota (sealed 12, tuning_min 8, realistic_min 4): 12 + 8 + 5 - 36 < 0, so the registered 100 binds (+ margin 5)
    world.write_prereg(quota={**REAL_QUOTA, "sealed": 12, "spare": 4, "floor_sealed": 8, "floor_answerable": 5,
                              "realistic_min": 4, "tuning_min": 8})
    code, out, err = run()
    assert (world.last(out, "count_migrated")["drafts"], world.last(out, "count_migrated")["drafts_rule"]) == ("105", "families_approx")


# --- F4: prereg-check validates every registered value ---------------------------------------------------------------------
def changed(value):
    """A different value of the same JSON type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.125
    if isinstance(value, str):
        return value + "_x"
    return [*value, "x"]


def put(doc, path, value):
    out = copy.deepcopy(doc)
    node = out
    *parents, last = path.split(".")
    for part in parents:
        node = node[part]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return out


def test_the_real_prereg_passes_and_equals_the_registered_table():
    doc = real_prereg()
    assert st.validate_prereg(doc, recompute=False) == []
    for path, value in st.REGISTERED_VALUES:
        assert st._eq(st._at(doc, path), value), path
    # nothing in the file is unregistered: every leaf outside the structural blocks is in the table
    unregistered = [
        path for key, node in doc.items() if key not in st.STRUCTURAL_ROOTS for path, _ in st.prereg_leaves(node, key)
        if path not in st.REGISTERED and path not in st.COMPUTED_PATHS  # r4: judge.config_hash is computed from the code
    ]
    assert unregistered == []
    assert {p.split(".")[0] for p in st.REGISTERED} == set(doc) - set(st.STRUCTURAL_ROOTS)
    assert len(st.REGISTERED) == len(st.REGISTERED_VALUES)  # no path twice


@pytest.mark.parametrize("path", [p for p, _ in st.REGISTERED_VALUES])
def test_every_registered_value_is_checked_changed_and_removed(path):
    doc = real_prereg()
    errors = st.validate_prereg(put(doc, path, changed(st.REGISTERED[path])), recompute=False)
    assert (path, "mismatch") in errors, (path, errors)
    errors = st.validate_prereg(put(doc, path, KeyError), recompute=False)
    assert (path, "mismatch") in errors or (path, "missing") in errors, (path, errors)  # a top-level key: "missing" first


@pytest.mark.parametrize("root", sorted({p.split(".")[0] for p, _ in st.REGISTERED_VALUES}))
def test_a_leaf_nobody_registered_is_refused_in_every_registered_block(root):
    doc = real_prereg()
    node = doc[root]
    if isinstance(node, dict):
        node["unregistered_extra"] = 1
        path = f"{root}.unregistered_extra"
    else:  # a top-level scalar: another top-level key is the unknown one
        doc["unregistered_extra"] = 1
        path = "<unknown>"
    errors = st.validate_prereg(doc, recompute=False)
    assert errors and (path, "unknown_key") in errors


def test_the_named_gaps_of_the_audit_are_pinned():
    for path, value in (
        ("draws_r", 5), ("primary_metric.unit", "family"), ("primary_metric.set", "sealed"),
        ("primary_metric.eligible_scopes", ["answer", "partial"]), ("primary_metric.direction", "sign_of_sum_d_g"),
        ("primary_metric.alpha_scope", "family_wise"), ("primary_metric.sensitivities", ["durkalski_by_group", "mcnemar_exact_families"]),
        ("primary_metric.estimand", "family_level_difference"), ("primary_metric.mwe_scale", "absolute"),
        ("controls.families", "public_lineage_development"),
        ("adoption.confirmatory_batch.expansion", "batch"),  # was the stale "replay"
    ):
        assert st.REGISTERED[path] == value, path
    # draws_r and the fields that depend on it agree
    for path in ("adoption.development_evidence.draws", "controls.draws_r"):
        assert st.REGISTERED[path] == st.REGISTERED["draws_r"] == 5
    for path in ("adoption.development_evidence.incumbent_draws", "controls.draws", "adoption.control_flip.draws"):
        assert "first_5_" in st.REGISTERED[path]
    # every key of the confirmatory batch is in the table
    batch = real_prereg()["adoption"]["confirmatory_batch"]
    assert {f"adoption.confirmatory_batch.{k}" for k in batch if not isinstance(batch[k], dict)} <= set(st.REGISTERED)
    assert "adoption.confirmatory_batch.rewrite_changing_candidate.flag" in st.REGISTERED


def test_the_control_flip_counts_a_flip_in_raw_hybrid_or_in_the_production_config():
    """F8 (D57: a flip in raw hybrid AND the cached-expansion production configuration): ``modes``, not ``mode``."""
    assert st.REGISTERED["adoption.control_flip.modes"] == ["hybrid", "hybrid+rewrite"]
    assert "adoption.control_flip.mode" not in st.REGISTERED
    old = put(real_prereg(), "adoption.control_flip", {**real_prereg()["adoption"]["control_flip"], "mode": "hybrid+rewrite"})
    assert ("adoption.control_flip.mode", "unknown_key") in st.validate_prereg(old, recompute=False)
    from scripts import controls

    assert tuple(controls.MODES) == ("hybrid", "hybrid+rewrite")  # controls.py's membership rule already judges both modes


def test_the_two_identities_and_the_draw_attribution_are_registered():
    """F9 / F10 wording: a candidate entry has a pipeline config hash AND a rewrite identity; draws name the incumbent config."""
    assert st.REGISTERED["adoption.confirmatory_batch.candidate_identity"].startswith("config_hash_is_pipeline_config_v2")  # r4 A; r5: v2
    assert "rewrite_model_prompt_caps" in st.REGISTERED["adoption.confirmatory_batch.rewrite_identity"]
    assert st.REGISTERED["adoption.development_evidence.incumbent_draws"] == (
        "first_5_recorded_development_draws_under_the_incumbent_config_hash")
    assert st.REGISTERED["adoption.development_evidence.candidate_draws"] == (
        "replay_mode_replays_incumbent_draw_artifacts_else_first_5_recorded_draws_under_its_config_hash")
    for path in ("adoption.control_flip.draws", "controls.draws"):
        assert st.REGISTERED[path].endswith("_under_the_incumbent_config_hash"), path
    assert "families_rule" in "".join(p for p, _ in st.REGISTERED_VALUES)


def test_the_test_worlds_prereg_is_the_whole_table_and_prereg_check_passes_it(world):
    blocks = b1a_blocks()
    flat = dict(st.prereg_leaves(blocks))
    assert set(flat) == set(st.REGISTERED) | st.COMPUTED_PATHS and all(st._eq(flat[p], v) for p, v in st.REGISTERED.items())
    world.write_prereg()
    code, out, err = world.run("prereg-check")
    assert code == 0 and "status=ok" in out, (out, err)
    world.write_prereg(drafting_rule={**blocks["drafting_rule"], "note": "free text"})
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and "why=unknown_key" in out


# --- F7: the epoch's prereg is registered by the first data dispatch ----------------------------------------------------------
def test_a_prereg_amended_and_pushed_after_a_data_dispatch_is_refused_at_the_first_split(world):
    world.write_prereg()
    world.commit_prereg(push=True)
    # what sp_launch records for a data dispatch: the prereg's sha256 on the dispatch record
    world.ledger().record_dispatch(dispatch_id="0a1b2c3d", dispatch_kind="data", argv_hash="d" * 64, prereg_sha256=file_sha(world.prereg_path))
    assert world.ledger().epoch_prereg_sha256() == file_sha(world.prereg_path)
    world.write_prereg(seeds={"split_r1": 99, "split_r2": 12, "repair": 13, "calibration": 14})  # amended ...
    world.commit_prereg(push=True)  # ... and pushed: every binding check of the tool passes
    before = world.ledger().records()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=prereg_amended" in err and out == ""
    assert world.ledger().records() == before
    # and the ledger itself refuses a second dispatch under it
    from src.sealed_ledger import ProtocolError

    with pytest.raises(ProtocolError):
        world.ledger().record_dispatch(dispatch_id="1a1b2c3d", dispatch_kind="data", argv_hash="d" * 64, prereg_sha256=file_sha(world.prereg_path))
    assert world.ledger().records() == before


# --- F13: epoch ---------------------------------------------------------------------------------------------------------------
def test_epoch_reports_the_registered_prereg_the_record_count_and_whether_the_working_file_matches(world):
    world.write_prereg()
    code, out, err = world.run("epoch")
    assert code == 0 and out == "epoch prereg_sha256=none records=0 matches=no\n" and err == ""
    # a record that carries no prereg (r4 D7: a `data` dispatch must carry one, so this is a non-data dispatch)
    world.ledger().record_dispatch(dispatch_id="0a1b2c3d", dispatch_kind="probe", argv_hash="d" * 64)
    code, out, err = world.run("epoch")
    assert out == "epoch prereg_sha256=none records=1 matches=no\n"
    sha = file_sha(world.prereg_path)
    world.ledger().record_dispatch(dispatch_id="1a1b2c3d", dispatch_kind="data", argv_hash="d" * 64, prereg_sha256=sha)
    before = world.ledger().records()
    code, out, err = world.run("epoch")
    assert code == 0 and out == f"epoch prereg_sha256={sha} records=2 matches=yes\n"
    assert_allowlisted(out)
    world.write_prereg(seeds={"split_r1": 99, "split_r2": 12, "repair": 13, "calibration": 14})  # the working file moved
    code, out, err = world.run("epoch")
    assert out == f"epoch prereg_sha256={sha} records=2 matches=no\n"
    world.prereg_path.unlink()
    code, out, err = world.run("epoch")
    assert code == 0 and out == f"epoch prereg_sha256={sha} records=2 matches=no\n"
    assert world.ledger().records() == before  # read-only: it appended nothing
    assert "epoch" not in st.STATE_COMMANDS


def test_epoch_on_a_split_attempt_registered_prereg(world):
    world.write_prereg()
    sha = file_sha(world.prereg_path)
    world.ledger().record_split_attempt(round_no=1, pool_digest="a" * 64, outcome="infeasible", prereg_sha256=sha)
    code, out, err = world.run("epoch")
    assert out == f"epoch prereg_sha256={sha} records=1 matches=yes\n"
