"""Phase 16A-2a a3, acceptance (c) for ``sealed_tool``: canaries in every free-text field never reach an output.

Free text in this world: question phrasings, gap keywords and ids, section labels (two inventory labels carry
the canary), tutorial tags, the prereg's drafting rule, pairing ids, the owner's corrections and doubts, the
colleague's key, sheet and form names, an SP return file. Every command runs; the combined stdout, stderr and log
records must hold no canary (also upper-cased), no opaque id outside the places the trusted-processor list names,
and no value that is not an integer, a sha256 or an enum word. The files outside ``eval/private/sealed/`` (the
mirror, the out-of-tree ledger and payloads, development files, the repository) must hold no sealed text.
"""

from __future__ import annotations

import json
import logging
import re
from scripts import sealed_tool as st
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    CANARY, read_rows, repos, standard_released, world,
)
from tests.test_p16w_sealed_tool import assert_allowlisted

SEALED_MARK = "SEALEDCANARY"
DEV_MARK = "DEVCANARY"
FAMILY = re.compile(r"\bf[0-9a-f]{8}\b")


def assert_clean(history, caplog):
    for argv, code, out, err in history:
        text = out + err
        assert "canary" not in text.lower(), (argv, text)
        if argv[0] == "return":
            continue  # the validated JSON line: checked on its own
        assert_allowlisted(text, ids_ok=argv[0] == "audit")
        if argv[0] != "audit":
            assert not FAMILY.search(text), (argv, text)  # opaque ids appear only in audit hits and return lines
    for record in caplog.records:
        assert "canary" not in record.getMessage().lower() and not FAMILY.search(record.getMessage())


def assert_sealed_text_stays_under_sealed(world, tmp_path):
    """No sealed question or canary in the mirror, the ledgers and payloads, development files or the repository."""
    sealed_root = world.sealed.resolve()
    questions = [r["question"] for r in read_rows(world.sealed / "sealed_r1.jsonl")]
    checked = 0
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "claude_projects" in path.parts:
            continue
        resolved = path.resolve()
        if sealed_root in resolved.parents and (sealed_root / "ledger") not in resolved.parents:
            continue  # sealed material may live here
        text = path.read_bytes().decode("utf-8", errors="replace")
        assert SEALED_MARK not in text and not any(q in text for q in questions), str(path.relative_to(tmp_path))
        checked += 1
    assert checked >= 6  # the mirror, both ledger copies, payloads, development files, the prereg ...
    for path in (sealed_root / "ledger").rglob("*"):  # the private ledger copy and payloads are content-free too
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert SEALED_MARK not in text and DEV_MARK not in text and not any(q in text for q in questions)


def test_canaries_in_every_free_text_field_never_reach_stdout_stderr_or_the_log(world, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    world.prereg_extra = {"drafting_rule": f"{CANARY} draft more families under the rule"}
    std = standard_released(world, sealed_mark=SEALED_MARK, dev_mark=DEV_MARK)
    run = world.run
    run("ids", "--count", "3", "--out", tmp_path / f"{CANARY}-ids.txt")
    run("digest", world.sealed / "sealed_r1.jsonl")
    run("digest", world.dev / "tuning_r1.jsonl")
    s0, s1, s2 = std.sealed_singletons
    name = f"{SEALED_MARK}sheet"
    run("sheet", "--kind", "sealed", "--name", name)
    corrections = {f"{s0}-{n}": {"corrections": json.dumps({"question": f"{SEALED_MARK} the owner reworded phrasing {n}"})} for n in (0, 1)}
    extra = {**corrections, **{f"{s1}-{n}": {"doubt": f"{SEALED_MARK} unsure about this one", "colleague_key": json.dumps({"scope": "refuse", "evidence": []})} for n in (0, 1)}}
    sheet = world.mark_sheet(world.sheet_path("sealed", name), {s0: "C", s1: "E", s2: "R"}, default="A", extra=extra)
    assert run("apply", "--kind", "sealed", "--sheet", sheet)[0] == 0
    confirm = world.mark_sheet(world.sealed / "work" / "sheets" / f"{name}.confirm.csv", {}, default="OK")
    assert run("apply", "--kind", "sealed", "--confirm", confirm)[0] == 0
    assert run("escalate", "--kind", "sealed", "--sheet", sheet, "--name", f"{SEALED_MARK}form")[0] == 0
    assert run("disagree", "--kind", "sealed", "--sheet", sheet)[0] == 0
    run("sheet", "--kind", "development", "--name", f"{DEV_MARK}dev")
    run("calibrate", "--kind", "sealed")
    run("exclude", "--family", std.sealed_groups[0][0], "--reason", "s_hit")
    run("prereg-check")
    run("power", "--group-sizes", "1,1,2,3", "--discordance", "0.4", "--effect", "0.1", "--alpha", "0.05", "--draws", "30", "--seed", "2")
    returns = tmp_path / "return.json"
    returns.write_text(json.dumps({"status": "blocked", "reason": f"{CANARY} could not be drafted"}), encoding="utf-8")
    run("return", returns)
    returns.write_text(json.dumps({"status": "ok", "kind": "twins", "pairs_checked": 3, "twin_pairs": 0, "pool_families": 1, "sha256": "0" * 64}), encoding="utf-8")
    run("return", returns)
    (tmp_path / "pairing.json").write_text(json.dumps({"pairs": [[f"q:{CANARY}", "q:r1"]]}), encoding="utf-8")
    (tmp_path / "conflicts.json").write_text(json.dumps({"labels": [f"{CANARY}.1"]}), encoding="utf-8")
    (tmp_path / "ids.txt").write_text("q:g1", encoding="utf-8")
    run("inputs-check", "--pairing", tmp_path / "pairing.json", "--conflicts", tmp_path / "conflicts.json",
        "--golden-ids", tmp_path / "ids.txt", "--realistic-ids", tmp_path / "ids.txt")
    run("freeze", "--split", "sealed")
    # a transcript that holds a sealed question: audit prints the target class and the family id, never the text
    projects = tmp_path / "claude_projects"
    transcript = projects / st.mangle_path(world.repo) / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    question = next(r["question"] for r in read_rows(world.sealed / "sealed_r1.jsonl") if r["family_id"] == std.sealed_groups[1][0])
    transcript.write_text(json.dumps({"content": question}) + "\n", encoding="utf-8")
    code, out, err = run("audit", claude_projects=projects, staging_root=tmp_path / "staging")
    assert code == st.EXIT_HIT and f"audit_hit target=transcript family={std.sealed_groups[1][0]} count=1" in out

    ran = {argv[0] for argv, *_ in world.history}
    assert ran >= {"ids", "digest", "twins", "split", "sheet", "apply", "escalate", "calibrate", "disagree", "exclude", "freeze", "audit",
                   "power", "prereg-check", "return", "inputs-check"}
    codes = {argv[0]: code for argv, code, *_ in world.history}
    assert codes["split"] == 0 and codes["audit"] == st.EXIT_HIT  # the success paths ran, not only refusals
    assert_clean(world.history, caplog)
    returns_out = [out for argv, code, out, err in world.history if argv[0] == "return"]
    assert returns_out[0] == "invalid\n" and json.loads(returns_out[1])["status"] == "ok"
    assert_sealed_text_stays_under_sealed(world, tmp_path)


def test_the_freeze_the_frozen_digest_and_the_power_figures_print_no_canary(world, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    std = standard_released(world, sealed_mark=SEALED_MARK, dev_mark=DEV_MARK)
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    world.run("digest", world.sealed / "heldout-v2.jsonl")
    world.run("power", "--from-freeze", "--discordance", "0.3", "--draws", "20", "--seed", "1", "--effect", "0.1", "--alpha", "0.05")
    world.approve_all("development")
    assert world.recheck("realistic-v2")[0] == 0
    assert any(argv[0] == "freeze" and code == 0 for argv, code, *_ in world.history)
    assert_clean(world.history, caplog)
    assert_sealed_text_stays_under_sealed(world, tmp_path)
    frozen = world.last(world.history[-1][2], "freeze")
    assert frozen["split"] == "realistic" and frozen["status"] == "frozen"
