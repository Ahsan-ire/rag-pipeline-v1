"""16A-2b b1a, the ``sealed_tool`` parts: the round-1 attempt cap, ``freeze`` (binding, minimums, source counts),
``declare-batch`` and ``power`` over the prereg's alphas. Synthetic fixtures only (``tests/p16w_sealed_world.py``).
(``calibrate`` is covered with the other review commands in ``tests/test_p16w_sealed_tool_review.py``.)
"""

from __future__ import annotations

import hashlib
import json

from scripts import sealed_tool as st
from src import sealed_ledger
from tests.p16_draws import record_draws as _record_draws
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    read_rows, repos, standard_released, world,
)


def file_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sealed_frozen(w):
    standard_released(w)
    w.approve_all("sealed")
    code, out, err = w.recheck("sealed")
    assert code == 0, err
    return out


# --- split: the round-1 attempt cap -------------------------------------------------------------------------------
def test_the_fourth_round_one_split_attempt_is_the_stop_split_infeasible(world):
    world.write_prereg()
    world.commit_prereg(push=True)
    ledger = world.ledger()
    for outcome in ("quota_short", "infeasible", "quota_short"):  # any outcomes count
        ledger.record_split_attempt(round_no=1, pool_digest="a" * 64, outcome=outcome)
    attempts = lambda: [r for r in world.ledger().records("split_attempt") if r["round"] == 1]  # noqa: E731
    assert len(attempts()) == 3
    before = world.ledger().records()
    code, out, err = world.run("split", "--round", "1")
    assert code == st.EXIT_REFUSED and "reason=split_infeasible" in err and out == ""
    assert len(attempts()) == 3 and world.ledger().records() == before  # nothing recorded
    # round 2 has its own cap (drafting_rule.round_2.max_attempts, tests/test_p16a2b_b1a_r2_tool.py); here it fails
    # for its own reason: there is no released round 1
    code, out, err = world.run("split", "--round", "2")
    assert code == st.EXIT_REFUSED and "reason=round1_missing" in err


def test_below_the_cap_a_round_one_attempt_still_proceeds(world):
    world.write_prereg()
    world.commit_prereg(push=True)
    for _ in range(2):
        world.ledger().record_split_attempt(round_no=1, pool_digest="a" * 64, outcome="infeasible")
    code, out, err = world.run("split", "--round", "1")
    assert "reason=split_infeasible" not in err  # the third attempt is allowed (here it stops on the empty pool)


# --- freeze -------------------------------------------------------------------------------------------------------
def test_every_freeze_records_the_prereg_sha_and_the_sealed_freeze_reports_source_counts(world):
    out = sealed_frozen(world)
    line = world.last(out, "freeze")
    # the standard world's sealed side: three 2-groups from tutorials (6 families), 3 refusals and 3 singletons from the handbook
    assert (line["tutorial"], line["drafted"]) == ("6", "6")
    want = file_sha(world.prereg_path)
    assert world.ledger().freeze_record("sealed")["prereg_sha256"] == want
    world.approve_all("development")
    code, out, err = world.recheck("realistic-v2")
    assert code == 0, err
    assert "tutorial" not in world.last(out, "freeze")  # development freezes report no source split
    assert world.ledger().freeze_record("realistic")["prereg_sha256"] == want
    mirror = [json.loads(ln) for ln in world.repo.joinpath("eval", "sealed_ledger.jsonl").read_text().splitlines()]
    assert all(m["digests"]["prereg_sha256"] == want for m in mirror if m["kind"] == "freeze")


def test_a_freeze_needs_the_prereg_committed_unmodified_and_pushed(world):
    standard_released(world)
    world.approve_all("sealed")
    original = world.prereg_path.read_text(encoding="utf-8")
    world.prereg_path.write_text(original.replace('"split_r1": 11', '"split_r1": 12'), encoding="utf-8")
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=prereg_modified" in err and out == ""
    world.commit_prereg(push=False)  # committed now, but not on the remote
    code, out, err = world.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=prereg_not_pushed" in err and out == ""
    assert world.ledger().freeze_record("sealed") is None and not (world.sealed / "heldout-v2.jsonl").exists()
    assert not list((world.sealed / "work").glob("freeze_*"))


def test_a_development_freeze_below_its_quota_minimum_is_refused_before_anything_is_written(world):
    std = standard_released(world)
    # the registered prereg cannot change once the epoch has one (prereg_amended), so the realistic split is brought
    # below its registered minimum (4) by the owner rejecting one of its three drafted families
    victim = next(r["family_id"] for r in read_rows(world.dev / "realistic_r1.jsonl") if r["family_id"] not in std.pinned_realistic)
    world.run("sheet", "--kind", "development", "--name", "r1", "--all")
    sheet = world.mark_sheet(world.sheet_path("development", "r1"), {victim: "R"}, default="A")
    code, out, err = world.run("apply", "--kind", "development", "--sheet", sheet)
    assert code == 0 and world.last(out, "apply")["rejected_families"] == "1", err
    code, out, err = world.recheck("realistic-v2")
    assert code == st.EXIT_FREEZE_INFEASIBLE and "reason=freeze_infeasible split=realistic families=3 minimum=4" in err and out == ""
    assert world.ledger().freeze_record("realistic") is None and not (world.dev / "realistic-v2.jsonl").exists()
    assert not list((world.sealed / "work").glob("freeze_*")) and not list(world.dev.glob("*.journal"))
    # tuning meets its minimum (8) and the sealed split has no such minimum
    assert world.recheck("tuning-v2")[0] == 0
    assert world.ledger().freeze_record("tuning")["totals"]["families"] == 8


# --- declare-batch ------------------------------------------------------------------------------------------------
def record_controls(world, content: bytes = b'{"version": 1}\n'):
    """The committed controls list and the ledger's record of it (what ``declare-batch`` requires)."""
    path = world.repo / "eval" / "controls_v1.json"
    path.write_bytes(content)
    world.ledger().record_controls(
        controls_sha256=file_sha(path), draws_sha256="e" * 64, prereg_sha256=file_sha(world.prereg_path),
        incumbent_config_hash="f" * 64,
    )
    return path


record_draws = _record_draws  # r6: the shared helper records each draw with its own execution receipt


def test_declare_batch_needs_the_freeze_the_controls_validates_and_appends_once(world, monkeypatch):
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)  # the embargo itself: tests/test_p16a2b_b1a_r2.py
    a, b = "1" * 64, "2" * 64
    rewrite = "7" * 64  # a candidate entry's rewrite identity (r3 F9): <config hash>:candidate:<rewrite config hash>
    cand = lambda h, mode="replay": ["--candidate", f"{h}:{mode}" if mode == "replay" else f"{h}:{mode}:{rewrite}"]  # noqa: E731
    code, out, err = world.run("declare-batch", *cand(a))
    assert code == st.EXIT_REFUSED and "reason=freeze_missing" in err
    sealed_frozen(world)
    code, out, err = world.run("declare-batch", *cand(a))
    assert code == st.EXIT_REFUSED and "reason=controls_missing" in err and out == ""  # no controls record yet
    record_controls(world)
    for bad in (cand(a) + cand(a), [], ["--candidate", f"short:replay"], ["--candidate", f"{'A' * 64}:replay"],
                ["--candidate", a], ["--candidate", f"{a}:live"], ["--candidate", f"{a}:"], ["--candidate", f"{a}:replay:candidate"],
                ["--candidate", f"{a}:candidate"], ["--candidate", f"{a}:candidate:short"],  # a candidate names its rewrite identity
                ["--candidate", f"{a}:replay:{rewrite}"], ["--candidate", f"{a}:candidate:{rewrite}:x"],  # a replay entry has none
                sum((cand(str(n) * 64) for n in range(1, 6)), [])):
        code, out, err = world.run("declare-batch", *bad)
        assert code == st.EXIT_REFUSED and "reason=batch_invalid" in err and out == "", bad
    assert world.ledger().batch_declaration() is None
    record_draws(world.ledger(), a, b)  # r5: the batch's candidates have their draws recorded (the coverage rules: r5 tests)
    code, out, err = world.run("declare-batch", *cand(a), *cand(b, "candidate"))
    assert code == 0, err
    line = world.last(out, "batch_declared")
    rec = world.ledger().batch_declaration()
    assert line["candidates"] == "2" and line["sha256"] == rec["candidates_sha256"]
    assert rec["candidates"] == [
        {"config_hash": a, "expansion": "replay"},
        {"config_hash": b, "expansion": "candidate", "rewrite_config_hash": rewrite},
    ]
    code, out, err = world.run("declare-batch", *cand(b))
    assert code == st.EXIT_REFUSED and "reason=batch_declared" in err
    assert a not in out + err and world.ledger().batch_declaration() == rec


# --- power ----------------------------------------------------------------------------------------------------------
def test_power_emits_one_set_per_alpha_unless_alpha_is_given(world):
    world.write_prereg()
    code, out, err = world.run("power", "--group-sizes", "1,1,1,1,1,1")
    assert code == 0, err
    lines = [dict(p.split("=", 1) for p in ln.split(" ")[1:]) for ln in out.splitlines() if ln.startswith("power ")]
    assert sorted({ln["alpha_ppm"] for ln in lines}) == ["12500", "50000"] and len(lines) == 4
    refs = [ln for ln in out.splitlines() if ln.startswith("power_reference ")]
    assert len(refs) == 2
    code, out, err = world.run("power", "--group-sizes", "1,1,1,1,1,1", "--alpha", "0.1")
    assert code == 0, err
    assert {ln.split("alpha_ppm=")[1].split(" ")[0] for ln in out.splitlines() if ln.startswith("power ")} == {"100000"}
