"""Phase 16A-1 (d) lock: the v5 reports must equal the P0 capture on main, byte for byte.

``tests/p16_capture.py`` ran once on a scratch worktree of main (9ce4e07)
before any 16A-1 code edit and wrote ``tests/fixtures/p16_v5_projection_main.json``.
This re-runs the same driver in-process on the working tree. The driver makes
every network, embedding-model and vector-store constructor raise (and fails
the capture if one fired even when swallowed), so a pass also proves the v5
paths construct none of them.
"""

import json
from pathlib import Path

from tests.p16_capture import capture

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "p16_v5_projection_main.json"
MAIN_SHA = "9ce4e071383ee1c62b1c809363563d8efbac0aea"


def test_v5_reports_identical_to_main_capture():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert expected["main_sha"] == MAIN_SHA
    actual = capture(str(ROOT))
    assert sorted(actual["reports"]) == sorted(expected["reports"])
    for branch, text in expected["reports"].items():
        assert actual["reports"][branch] == text, f"v5 report changed: {branch}"
    assert actual["meta"] == expected["meta"]


def test_capture_covers_every_v5_branch():
    reports = json.loads(FIXTURE.read_text(encoding="utf-8"))["reports"]
    assert "Canonical run (writes the committed report): True" in reports["matrix_canonical"]
    assert "generation errors:" in reports["matrix_generation_error"]
    assert "generation incomplete" in reports["matrix_incomplete_unknown"]
    assert "unknown generation status" in reports["matrix_incomplete_unknown"]
    assert "SUPPRESSED" in reports["matrix_judge_suppressed"]
    assert "NO held-out set present" in reports["matrix_no_heldout"]
    assert "query expansion: disabled" in reports["matrix_offline"]
    assert "] excluded (" in reports["run_eval_legacy_excluded"]
