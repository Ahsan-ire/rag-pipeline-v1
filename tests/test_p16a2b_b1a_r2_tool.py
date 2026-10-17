"""16A-2b b1a closure round r2, the ``sealed_tool`` parts: the epoch's one registered prereg at split, freeze and
declare-batch, the embargo on the command line, ``count-migrated``'s refusal formula and band check, the round-2 cap,
the register share at split, and the small items (source counts, texts).

Synthetic fixtures only (``tests/p16w_sealed_world.py``). ``calibrate`` and ``controls.py`` are covered in
``tests/test_p16w_sealed_tool_review.py`` and ``tests/test_p16a2_controls.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src import sealed_ledger
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    read_rows, repos, standard_released, world, write_rows, build_standard,
)
from tests.test_p16a2b_b1a_tool import file_sha, record_controls, record_draws, sealed_frozen

ROOT = Path(__file__).resolve().parent.parent
SEEDS = {"split_r1": 99, "split_r2": 12, "repair": 13, "calibration": 14}


def amend_prereg(world) -> None:
    """Replace the prereg by another one (another split seed), committed and pushed: the gates' own checks pass."""
    world.write_prereg(seeds=SEEDS)
    world.commit_prereg(push=True)


def restore_prereg(world) -> None:
    """Put the original bytes back (a new commit, pushed)."""
    world.write_prereg()
    world.commit_prereg(push=True)


# --- R1: the epoch's one registered prereg -----------------------------------------------------------------------
def test_every_split_attempt_records_the_prereg_sha_and_the_first_one_registers_the_epoch(world):
    standard_released(world)
    want = file_sha(world.prereg_path)
    attempts = world.ledger().records("split_attempt")
    assert attempts and all(a["prereg_sha256"] == want for a in attempts)
    assert world.ledger().epoch_prereg_sha256() == want
    mirror = [json.loads(ln) for ln in world.repo.joinpath("eval", "sealed_ledger.jsonl").read_text().splitlines()]
    assert all(m["digests"]["prereg_sha256"] == want for m in mirror if m["kind"] == "split_attempt")


def test_a_pushed_replacement_prereg_is_refused_at_split_after_a_first_attempt(world):
    world.write_prereg()
    world.commit_prereg(push=True)
    world.ledger().record_split_attempt(round_no=1, pool_digest="a" * 64, outcome="infeasible", prereg_sha256=file_sha(world.prereg_path))
    amend_prereg(world)
    before = world.ledger().records()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=prereg_amended" in err and out == ""
    assert world.ledger().records() == before  # nothing recorded
    restore_prereg(world)  # the original bytes again: the split goes on to its own next check
    code, out, err = world.run("split", "--round", "1")
    assert "prereg_amended" not in err and "reason=pool_empty" in err


def test_a_pushed_replacement_prereg_is_refused_at_freeze(world):
    standard_released(world)
    world.approve_all("sealed")
    amend_prereg(world)
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=prereg_amended" in err and out == ""
    assert world.ledger().freeze_record("sealed") is None and not list((world.sealed / "work").glob("screen_*"))
    restore_prereg(world)
    code, out, err = world.run("freeze", "--split", "sealed")
    assert "reason=recheck_missing" in err  # the original passes the binding and reaches the screen


def test_a_pushed_replacement_prereg_is_refused_at_declare_batch(world, monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    sealed_frozen(world)
    record_controls(world)
    amend_prereg(world)
    code, out, err = world.run("declare-batch", "--candidate", f"{'1' * 64}:replay")
    assert code == st.EXIT_REFUSED and "reason=prereg_amended" in err and out == ""
    assert world.ledger().batch_declaration() is None
    restore_prereg(world)
    record_draws(world.ledger(), "1" * 64, mode="replay")  # r5: the candidate's draws are recorded (r7: replay draws, as its entry says)
    code, out, err = world.run("declare-batch", "--candidate", f"{'1' * 64}:replay")
    assert code == 0, err


# --- R3: the embargo on the command line -------------------------------------------------------------------------------
def test_declare_batch_is_refused_while_the_embargo_holds_whatever_else_is_in_order(world):
    assert sealed_ledger.CONFIRMATORY_EMBARGO is True
    code, out, err = world.run("declare-batch", "--candidate", f"{'1' * 64}:replay")  # before any freeze
    assert code == st.EXIT_REFUSED and "reason=embargoed" in err and out == ""
    sealed_frozen(world)
    record_controls(world)  # frozen, controls recorded, prereg bound: still embargoed
    code, out, err = world.run("declare-batch", "--candidate", f"{'1' * 64}:replay")
    assert code == st.EXIT_REFUSED and "reason=embargoed" in err and out == ""
    assert world.ledger().batch_declaration() is None


# --- R5: count-migrated -------------------------------------------------------------------------------------------------
def _v1(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"question": q, "type": t, "expected_sections": [] if t == "refusal" else ["1.1"]}) + "\n"
                            for q, t in rows), encoding="utf-8")
    return path


def _migrated(world, eval_registry, tmp_path, *, gold, real, tutorial=1):
    """Run ``count-migrated`` over invented v1 sets (``gold``, ``real`` are lists of types) and one tutorial exercise."""
    g = _v1(tmp_path / "public" / "g.jsonl", [(f"gold question {i}", t) for i, t in enumerate(gold)])
    r = _v1(tmp_path / "public" / "r.jsonl", [(f"real question {i}", t) for i, t in enumerate(real)])
    for path in (g, r):
        eval_registry.add(path, privacy="public", name=path.name)
    pairing = tmp_path / "pairing.json"
    pairing.write_text(json.dumps({"pairs": []}), encoding="utf-8")
    tut = tmp_path / "tutorial.jsonl"
    tut.write_text("".join(json.dumps({"id": f"T01-A{chr(97 + i)}", "type": "direct"}) + "\n" for i in range(tutorial)), encoding="utf-8")
    return lambda *extra: world.run("count-migrated", "--golden", g, "--realistic", r, "--pairing", pairing, "--tutorial", tut, *extra)


def test_count_migrated_rounds_the_refusal_target_half_up(world, eval_registry, tmp_path):
    world.write_prereg()
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 8, real=["direct"] * 4)  # 8 + (4 + 1 exercise) = 13 migrated
    # b2 (quota amendment): the quotas add up to the pool (sealed 12 + tuning + realistic), or the pool's refusal target is not
    # comparable with what the three splits can reach together. The half-way cases are the pools of 20 mod 40 (11 x pool / 40 = x.5):
    # 16.5 -> 17, 38.5 -> 39; 11 is exact. (The old pool of 20, 5.5 -> 6, cannot hold 13 migrated families beside a sealed quota of 12.)
    for drafts, tuning, realistic, pool, target in ((47, 28, 20, 60, 17), (127, 68, 60, 140, 39), (27, 18, 10, 40, 11)):
        code, out, err = run("--quota-tuning", str(tuning), "--quota-realistic", str(realistic), "--drafts", str(drafts))
        assert code == 0, err
        line = world.last(out, "count_migrated")
        assert (line["migrated_families"], line["pool_total"], line["refusal_target"]) == ("13", str(pool), str(target)), pool
        assert line["preassigned_refusals"] == "0" and line["supply"] == "ok"
    # the drafts cover the larger of the share and the splits' deficits (4 + 3): pool 20 needs 7, not 6. That pool is deliberately
    # not the quotas' sum, so the run stops at band_unreachable (after printing), but the refusal_drafts formula is what is tested
    code, out, err = run("--quota-tuning", "16", "--quota-realistic", "12", "--drafts", "7")
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    assert world.last(out, "count_migrated")["refusal_drafts"] == "7"
    code, out, err = run("--quota-tuning", "16", "--quota-realistic", "12", "--drafts", "127")
    assert world.last(out, "count_migrated")["refusal_drafts"] == "39"
    assert (world.last(out, "count_migrated")["tuning_open"], world.last(out, "count_migrated")["tuning_band"]) == ("8", "ok")


def test_count_migrated_drafts_default_to_the_preregs_families_approx(world, eval_registry, tmp_path):
    doc = world.prereg_doc()
    doc["drafting_rule"]["attempt_1"]["families_approx"] = 27
    world.write_prereg(drafting_rule=doc["drafting_rule"])
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 8, real=["direct"] * 4)
    code, out, err = run("--quota-tuning", "21", "--quota-realistic", "12")  # b2: 12 + 21 + 12 = the pool of 45
    # r4 D1: the default is max(families_approx, pool floor) + the registered draft_margin (5): 13 migrated + (27 + 5)
    line = world.last(out, "count_migrated")
    assert code == 0 and line["pool_total"] == "45" and (line["drafts"], line["draft_margin"]) == ("32", "5"), err
    rule = world.prereg_doc()["drafting_rule"]
    del rule["attempt_1"]["families_approx"]
    world.write_prereg(drafting_rule=rule)  # no families_approx and no --drafts
    code, out, err = run("--quota-tuning", "21", "--quota-realistic", "12")
    assert code == st.EXIT_USAGE and "reason=usage" in err


def test_count_migrated_stops_on_an_unreachable_band_after_printing(world, eval_registry, tmp_path):
    world.write_prereg()
    # tuning: 8 families fill its quota of 8 (no open slot) but 2 refusal families are missing
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 8, real=["direct"] * 4)
    code, out, err = run("--quota-tuning", "8", "--quota-realistic", "12", "--drafts", "30")
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    line = world.last(out, "count_migrated")  # printed first
    assert (line["tuning_open"], line["tuning_deficit"], line["tuning_band"], line["realistic_band"]) == ("0", "2", "unreachable", "ok")


def test_count_migrated_stops_when_preassigned_refusals_exceed_the_upper_band(world, eval_registry, tmp_path):
    world.write_prereg()
    # 5 families, 3 refusals (60 %), quota 8: 3 is over floor(0.30 x 8) = 2 although the deficit (0) fits the open slots
    run = _migrated(world, eval_registry, tmp_path, gold=["refusal"] * 3 + ["direct"] * 2, real=["direct"] * 4)
    code, out, err = run("--quota-tuning", "8", "--quota-realistic", "12", "--drafts", "30")
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err
    line = world.last(out, "count_migrated")
    assert (line["tuning_deficit"], line["tuning_band"]) == ("0", "unreachable")


def test_count_migrated_needs_the_registered_refusal_share(world, eval_registry, tmp_path):
    run = _migrated(world, eval_registry, tmp_path, gold=["direct"] * 8, real=["direct"] * 4)
    code, out, err = run("--quota-tuning", "16", "--quota-realistic", "12", "--drafts", "7")
    assert code == st.EXIT_USAGE and "reason=prereg_missing" in err  # no prereg at all
    rule = world.prereg_doc()["drafting_rule"]
    rule["attempt_1"]["refusal_share_target"] = 0.3
    world.write_prereg(drafting_rule=rule)
    code, out, err = run("--quota-tuning", "16", "--quota-realistic", "12", "--drafts", "7")
    assert code == st.EXIT_REFUSED and "reason=prereg_invalid" in err and out == ""


# --- R5: the round-2 cap and the register share -------------------------------------------------------------------------
def test_round_two_is_capped_at_three_attempts_and_the_stop_records_nothing(world):
    standard_released(world)
    sha = file_sha(world.prereg_path)
    for outcome in ("quota_short", "infeasible"):
        world.ledger().record_split_attempt(round_no=2, pool_digest="a" * 64, outcome=outcome, prereg_sha256=sha)
    code, out, err = world.run("split", "--round", "2")
    assert "reason=split_infeasible" not in err  # two attempts: below the cap
    world.ledger().record_split_attempt(round_no=2, pool_digest="a" * 64, outcome="quota_short", prereg_sha256=sha)
    before = world.ledger().records()
    assert len([r for r in before if r["kind"] == "split_attempt" and r["round"] == 2]) >= 3
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_FREEZE_INFEASIBLE == 9 and "reason=split_infeasible" in err and out == ""
    assert world.ledger().records() == before


def _terse_pool(world):
    """The standard world with every lay draft made terse (before the twins record binds the pool)."""
    std = build_standard(world)
    path = world.sealed / "pool" / "pool.jsonl"
    rows = read_rows(path)
    for row in rows:
        if row["register"] == "lay":
            row["register"] = "terse"
    write_rows(path, rows)
    world.write_prereg()
    world.commit_prereg(push=True)
    code, out, err = world.run("twins", "--round", "1", "--verdict", world.verdict(std.verdict_pairs))
    assert code == 0, err


def test_a_pool_whose_new_drafts_are_too_few_lay_releases_nothing_and_counts_toward_the_cap(world):
    _terse_pool(world)
    for attempt in (1, 2, 3):
        code, out, err = world.run("split", "--round", "1")
        assert code == st.EXIT_SPLIT_INFEASIBLE, err
        line = world.last(out, "split")
        assert (line["outcome"], line["attempt"]) == ("register_short", str(attempt))
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == ["register_short"] * 3
    assert world.ledger().records("release") == [] and not (world.sealed / "sealed_r1.jsonl").exists()
    code, out, err = world.run("split", "--round", "1")  # the fourth attempt: the cap
    assert code == st.EXIT_REFUSED and "reason=split_infeasible" in err


def test_a_pool_with_enough_lay_among_its_new_drafts_is_released(world):
    standard_released(world)  # 9 lay and 4 terse new drafts: 69 % lay, over the registered 50 %
    assert [r["outcome"] for r in world.ledger().records("split_attempt")] == ["feasible"]


# --- R6: small items -----------------------------------------------------------------------------------------------------
def test_the_sealed_freeze_counts_tutorial_families_as_tutorial_and_the_rest_as_drafted(world):
    std = build_standard(world)
    path = world.sealed / "pool" / "pool.jsonl"
    rows = read_rows(path)
    for row in rows:
        if row["family_id"] == std.sealed_singletons[0]:
            row["source"] = "colleague"  # neither a tutorial nor drafted from the handbook
    write_rows(path, rows)
    world.write_prereg()
    world.commit_prereg(push=True)
    assert world.run("twins", "--round", "1", "--verdict", world.verdict(std.verdict_pairs))[0] == 0
    assert world.run("split", "--round", "1")[0] == 0
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    line = world.last(out, "freeze")
    assert (line["tutorial"], line["drafted"]) == ("6", "6")  # the colleague family is not a tutorial one


def test_the_sealed_report_says_where_holm_is_applied():
    from src import sealed_report

    text = (ROOT / "src" / "sealed_report.py").read_text(encoding="utf-8")
    assert "Holm with K = 4 fixed in advance (prereg `holm_k`), applied by the prereg analysis, not here." in text
    assert "Holm across the declared candidate batch" not in text
    assert hasattr(sealed_report, "format_sealed_report")


def test_the_launcher_says_origin_branch_or_origin_main():
    assert "origin/main" in sp.__doc__ and "origin/<branch>" in sp.__doc__
    helps = " ".join(a.help or "" for a in sp._parser()._actions)
    assert "origin/<branch> or origin/main" in helps and "or else main" in helps


def test_the_processors_brief_names_the_refusal_and_lay_keys():
    text = (ROOT / "docs" / "sealed" / "sealed-processor.md").read_text(encoding="utf-8")
    assert "About 30%" not in text
    assert "`task.json:refusals`" in text and "`task.json:lay_min`" in text
    assert "`count`" in text and "`allowed_sources`" in text
