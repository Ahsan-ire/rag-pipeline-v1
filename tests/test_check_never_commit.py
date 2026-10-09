"""Tests for scripts/check_never_commit.py (pure pattern logic; git calls mocked)."""

import pytest

from scripts import check_never_commit as cnc


@pytest.mark.parametrize("path", [
    "data/Conveyancing_Handbook.pdf", "data/tutorials/x.md", "Demo/handbook.PDF", ".env", "sub/.env",
    ".env.local", "chroma_db/chroma.sqlite3", "chroma_db_arm_gte/x", "sample_chroma_db/x",
    "eval/bakeoff/run.json", "logs/audit_log.jsonl", "Tutorial_Docs_for_review/a.txt",
    "eval/judge_review.jsonl",
])
def test_blocked_paths(path):
    assert cnc.offenders([path]) == [path]


@pytest.mark.parametrize("path", [
    ".env.example", "scripts/bakeoff_report.py", "docs/designs/001-bakeoff-embedding-model.md",
    "eval/results.md", "src/ingest.py", "tests/fixtures/h_projection_manifest.txt", "README.md",
    "eval/heldout_set.jsonl",
])
def test_allowed_paths(path):
    assert cnc.offenders([path]) == []


def test_main_fails_on_tree_offender(monkeypatch, capsys):
    monkeypatch.setattr(cnc, "tree_paths", lambda: ["README.md", "data/x.pdf"])
    assert cnc.main([]) == 1
    assert "data/x.pdf" in capsys.readouterr().out


def test_main_checks_the_range_for_added_then_removed_files(monkeypatch):
    monkeypatch.setattr(cnc, "tree_paths", lambda: ["README.md"])
    monkeypatch.setattr(cnc, "added_in_range", lambda r: ["logs/audit_log.jsonl"])
    assert cnc.main(["--range", "a..b"]) == 1


def test_main_clean(monkeypatch):
    monkeypatch.setattr(cnc, "tree_paths", lambda: ["README.md", ".env.example"])
    monkeypatch.setattr(cnc, "added_in_range", lambda r: [])
    assert cnc.main(["--range", "a..b"]) == 0


def test_the_real_tree_is_clean():
    """The committed tree itself must pass (runs real `git ls-files`; read-only, no network)."""
    assert cnc.offenders(cnc.tree_paths()) == []
