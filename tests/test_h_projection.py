"""H0 regression lock: the evaluator projection must equal main's (spec amendment 1).

Re-runs scripts/h_capture_projection.py against the working tree, in a subprocess
so the nested pytest session is isolated, and compares it with the projection
captured on main before any hotfix edit. The recorded SHA is metadata and is
excluded from the comparison. New H1c fields (status, incomplete counts) are
asserted in tests/test_evaluator.py, not here.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "h_eval_projection_main.json"


def test_evaluator_projection_identical_to_main(tmp_path):
    out = tmp_path / "projection.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "h_capture_projection.py"),
            "--repo",
            str(ROOT),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = json.loads(out.read_text(encoding="utf-8"))
    assert expected["main_sha"] == "85a42831331124f5d5667ff7b47e9e1f381737c9"
    expected.pop("main_sha")
    actual.pop("main_sha")
    assert actual == expected
