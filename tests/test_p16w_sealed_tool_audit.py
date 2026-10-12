"""Phase 16A-2a a3, acceptance (k): ``sealed_tool audit`` counts S-needles per target class with the family id.

The sealed questions are invented (``zorbin``-style words); the targets are synthetic transcripts, memory
files, logs, runs and a throwaway git repository. Nothing reads ``~/.claude``, a real log or a real run: the
projects root, the SP staging root and the repository are all injected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    CANARY, fid_of, make_row, repos, world, write_rows,
)
from tests.test_p16w_sealed_tool import assert_allowlisted
from tests.test_p16w_sp_launch import git

WORDS = "zorbin keldwyn plimsy tarnock vessuvio quintoble flarnwick gostrel mardwyn pellick ostravy glombar trundle snorfle bexwick".split()


def needle(i: int) -> str:
    """A long sealed question made of invented words only this family uses (so any hit names this family)."""
    return " ".join(f"{WORDS[i % len(WORDS)]}{i}x{k}" for k in range(9))


CLASSES = ("transcript", "memory", "log", "research", "work", "run", "dispatch", "staging", "tree", "history")
SHORT = "zorbin keldwyn plimsy tarnock vessuvio"  # a 5-token sealed question: whole JSON values only


@pytest.fixture
def audit_world(world, tmp_path):
    """A sealed set with one family per target class, and the injected target locations."""
    fam = {c: fid_of(0xE00 + i) for i, c in enumerate(CLASSES)}
    fam["worktree"] = fid_of(0xE20)
    fam["elsewhere"] = fid_of(0xE21)  # planted only in another project's directory: never counted
    fam["short"] = fid_of(0xE22)
    rows = []
    for i, (name, fid) in enumerate(fam.items()):
        rows.append(make_row(fid, 0, question=SHORT if name == "short" else needle(i), evidence=[[f"{i % 9 + 1}.{i // 9 + 1}"]]))
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    q = {name: rows[i]["question"] for i, name in enumerate(fam)}
    projects = tmp_path / "claude_projects"
    mangled = st.mangle_path(world.repo)
    staging = tmp_path / "sp_staging"
    w = {
        "fam": fam, "q": q, "projects": projects, "mangled": mangled, "staging": staging,
        "opts": {"claude_projects": projects, "staging_root": staging},
    }
    return type("AW", (), w)()


def plant(aw, world, *, skip=()):
    """Plant each class's needle in its own target (escaped, as a transcript holds it)."""
    def put(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    q = aw.q
    if "transcript" not in skip:
        put(aw.projects / aw.mangled / "session.jsonl", json.dumps({"role": "user", "content": f"please check: \"{q['transcript']}\""}) + "\n")
        put(aw.projects / f"{aw.mangled}--claude-worktrees-agent-x" / "sub" / "agent.jsonl",
            json.dumps({"role": "assistant", "content": q["worktree"]}) + "\n")
    put(aw.projects / "-Users-someone-else-project" / "session.jsonl", json.dumps({"content": q["elsewhere"]}) + "\n")
    if "memory" not in skip:
        put(aw.projects / aw.mangled / "memory" / "note.md", f"- remember {q['memory']}\n")
    if "log" not in skip:
        put(world.repo / "logs" / "run.log", f"{{\"q\": \"{SHORT}\"}}\n{q['log']}\n")
    if "research" not in skip:
        put(world.repo / "data" / "research" / "notes.txt", f"he once asked {SHORT} in passing\n{q['research']}\n")
    if "work" not in skip:
        put(world.root / "work" / "scratch.txt", f"{q['work']}\n")
    if "run" not in skip:
        put(world.root / "runs" / "r20261012-aaaa" / "report.md", f"# run\n{q['run']}\n")
    if "dispatch" not in skip:
        put(world.root / "sp_dispatch.jsonl", json.dumps({"id": "d1", "note": q["dispatch"]}) + "\n")
    if "staging" not in skip:
        put(aw.staging / "abcd1234" / "stdout.log", q["staging"] + "\n")


def run_audit(world, aw, **extra):
    return world.run("audit", **aw.opts, **extra)


def lines(out, kind):
    return [{"_kind": kind, **dict(p.split("=", 1) for p in ln.split(" ")[1:])} for ln in out.splitlines() if ln.startswith(kind + " ")]


def test_a_planted_s_needle_in_each_target_class_is_counted_with_its_family_id(world, audit_world):
    aw = audit_world
    plant(aw, world)
    code, out, err = run_audit(world, aw)
    assert code == st.EXIT_HIT == 5, err
    targets = {ln["target"]: ln for ln in lines(out, "audit_target")}
    assert set(targets) == set(st.TARGET_CLASSES)
    for klass in ("transcript", "memory", "log", "research", "work", "run", "dispatch", "staging"):
        assert targets[klass]["files"] != "0", klass
    hits = {(ln["target"], ln["family"]): ln["count"] for ln in lines(out, "audit_hit")}
    fam = aw.fam
    expected = {
        ("transcript", fam["transcript"]), ("transcript", fam["worktree"]),  # a worktree's project directory counts too
        ("memory", fam["memory"]), ("log", fam["log"]), ("log", fam["short"]),  # the short question as a whole JSON value
        ("research", fam["research"]), ("work", fam["work"]), ("run", fam["run"]), ("dispatch", fam["dispatch"]), ("staging", fam["staging"]),
    }
    assert set(hits) == expected, hits
    assert not any(f == fam["elsewhere"] for _, f in hits)  # another project's directory is not ours
    assert targets["transcript"]["hits"] == "2" and targets["log"]["hits"] == "2" and targets["memory"]["hits"] == "1"
    assert targets["tree"]["hits"] == "0" and targets["history"]["hits"] == "0"
    # the short question embedded in prose (research) is the documented residual: not counted there
    assert ("research", fam["short"]) not in hits
    summary = lines(out, "audit")[0]
    assert summary["status"] == "hits" and summary["hits"] == str(len(expected)) and int(summary["s_needles"]) > 0
    # the output holds counts, target classes and family ids only: no needle word, no path, no id outside audit_hit lines
    assert_allowlisted(out, ids_ok=True)
    for word in WORDS + [str(aw.projects), str(world.repo), "claude_projects", "session", "report"]:
        assert word not in out + err
    for line in out.splitlines():
        if not line.startswith("audit_hit "):
            assert not re.search(r"f[0-9a-f]{8}", line), line
    assert err == ""


def test_audit_scans_the_tracked_tree_and_history(world, audit_world):
    aw = audit_world
    plant(aw, world, skip=("transcript", "memory", "log", "research", "work", "run", "dispatch", "staging"))
    (world.repo / "notes.md").write_text(f"notes\n{aw.q['tree']}\n", encoding="utf-8")
    git(world.repo, "add", "notes.md")
    git(world.repo, "commit", "--quiet", "-m", "add notes")
    (world.repo / "old.md").write_text(f"old\n{aw.q['history']}\n", encoding="utf-8")
    git(world.repo, "add", "old.md")
    git(world.repo, "commit", "--quiet", "-m", f"add old {aw.q['worktree']}")  # a commit message is history too
    git(world.repo, "rm", "--quiet", "old.md")
    git(world.repo, "commit", "--quiet", "-m", "remove old")
    code, out, err = run_audit(world, aw)
    assert code == st.EXIT_HIT, err
    hits = {(ln["target"], ln["family"]) for ln in lines(out, "audit_hit")}
    fam = aw.fam
    assert ("tree", fam["tree"]) in hits and ("tree", fam["history"]) not in hits  # removed from HEAD
    assert ("history", fam["history"]) in hits and ("history", fam["tree"]) in hits  # but still in history
    assert ("history", fam["worktree"]) in hits  # and a commit message is scanned too
    assert not any(t == "transcript" for t, _ in hits)
    for word in WORDS:
        assert word not in out + err


def test_a_clean_world_and_a_world_without_sealed_sources_exit_zero(world, audit_world):
    aw = audit_world
    code, out, err = run_audit(world, aw)  # sealed sources exist, nothing planted
    assert code == 0 and lines(out, "audit")[0]["status"] == "clean" and "audit_hit" not in out
    assert all(ln["hits"] == "0" for ln in lines(out, "audit_target"))
    (world.sealed / "sealed_r1.jsonl").unlink()
    plant(aw, world)
    code, out, err = run_audit(world, aw)
    assert code == 0 and lines(out, "audit")[0]["s_needles"] == "0" and "audit_hit" not in out  # nothing to look for


def test_audit_counts_each_hit_once_per_target_class_and_family_with_a_count(world, audit_world):
    aw = audit_world
    (aw.projects / aw.mangled).mkdir(parents=True)
    (aw.projects / aw.mangled / "a.jsonl").write_text((json.dumps({"c": aw.q["transcript"]}) + "\n") * 3, encoding="utf-8")
    (aw.projects / aw.mangled / "b.jsonl").write_text(json.dumps({"c": aw.q["transcript"]}) + "\n", encoding="utf-8")
    code, out, err = run_audit(world, aw)
    hit = lines(out, "audit_hit")
    assert code == 5 and len(hit) == 1 and hit[0]["target"] == "transcript" and hit[0]["count"] == "4"
    assert lines(out, "audit_target")[0]["files"] != "0"


def test_audit_refuses_an_invalid_sealed_source_with_a_fixed_reason(world, audit_world):
    aw = audit_world
    write_rows(world.sealed / "sealed_bad.jsonl", [{**make_row(fid_of(0xF00), 0), "evidence": [["99.99"]], "question": CANARY}])
    code, out, err = run_audit(world, aw)
    assert code == st.EXIT_USAGE and err == "error reason=scan_source_invalid\n" and CANARY not in out + err
