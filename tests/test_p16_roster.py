"""Phase 16A-1 item 9: the single roster's ids resolve to the committed realistic set ([C])."""

import json
from pathlib import Path

from src.eval_privacy import public_v1_id
from src.eval_roster import ROLE_QUESTIONS, ROSTER

REALISTIC = Path(__file__).resolve().parent.parent / "eval" / "realistic_set.jsonl"


def test_roster_and_role_ids_resolve_to_realistic_rows():
    rows = [json.loads(l) for l in REALISTIC.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {public_v1_id(r["question"]): r for r in rows}
    for entry in ROSTER:
        assert entry.row_id in by_id, entry
        assert tuple(by_id[entry.row_id]["expected_sections"]) == entry.expected
    for role in ROLE_QUESTIONS:
        assert role.row_id in by_id, role
        assert set(role.groups) <= set(by_id[role.row_id]["expected_sections"])
    counts = {}
    for entry in ROSTER:
        counts[entry.failure_class] = counts.get(entry.failure_class, 0) + 1
    assert counts == {"vocabulary gap": 3, "near-miss": 5}
