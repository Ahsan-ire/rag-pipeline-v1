"""Phase 16A-2a (j), `inputs-check` (`src/sealed_inputs.py`, a8, b0). Synthetic ids and labels.

The public v1 ids and the inventory are passed in (or read from the committed
public sets); the pairing and conflict files are tmp files. Every error is
asserted to name a field, never the offending value.
"""

import json

import pytest

from src import eval_sets, sealed_inputs
from src.eval_privacy import public_v1_id
from src.eval_schema import InventoryError, load_inventory
from src.sealed_inputs import InputsError, check_conflict_labels, check_pairing, inputs_check, public_v1_ids

CANARY = "P16-CANARY"
GOLDEN = frozenset(f"q:{i:012x}" for i in range(1, 6))
REALISTIC = frozenset(f"q:{i:012x}" for i in range(101, 106))
G = sorted(GOLDEN)
R = sorted(REALISTIC)
INVENTORY = {"version": 1, "map_sha256": "a" * 64, "sections": ["901.1", "902.7.3"], "aliases": ["901.2.4"]}


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def _check(tmp_path, pairs=None, labels=None, **kw):
    pairing = _write(tmp_path, "v2_pairing.json", {"pairs": [[G[0], R[0]], [G[1], R[1]]] if pairs is None else pairs})
    conflict = _write(tmp_path, "conflict_sections.json", {"labels": ["901.1"] if labels is None else labels})
    return inputs_check(pairing, conflict, inventory=INVENTORY, golden_ids=GOLDEN, realistic_ids=REALISTIC, **kw)


def _errors(tmp_path, **kw):
    with pytest.raises(InputsError) as info:
        _check(tmp_path, **kw)
    assert CANARY not in str(info.value) and "q:" not in str(info.value) and "901" not in str(info.value)
    return info.value.errors


# --- the success path -------------------------------------------------------------
def test_valid_inputs_return_counts_only(tmp_path):
    assert _check(tmp_path) == {"pairs": 2, "labels": 1}
    assert _check(tmp_path, labels=["901.1", "901.2.4", "901"]) == {"pairs": 2, "labels": 3}  # alias, ancestor
    assert _check(tmp_path, pairs=[], labels=[]) == {"pairs": 0, "labels": 0}


# --- pairing ------------------------------------------------------------------------
def test_a_reused_id_is_refused_in_either_column_or_both(tmp_path):
    same_golden = _errors(tmp_path, pairs=[[G[0], R[0]], [G[0], R[1]]])
    assert same_golden == [("v2_pairing.json", "pairs[1][0]", "id already used at pairs[0]")]
    same_realistic = _errors(tmp_path, pairs=[[G[0], R[0]], [G[1], R[0]]])
    assert same_realistic == [("v2_pairing.json", "pairs[1][1]", "id already used at pairs[0]")]
    repeated_pair = _errors(tmp_path, pairs=[[G[0], R[0]], [G[0], R[0]]])
    assert [(f, p) for f, p, _ in repeated_pair] == [("v2_pairing.json", "pairs[1][0]"), ("v2_pairing.json", "pairs[1][1]")]
    same_in_one_pair = check_pairing({"pairs": [["q:" + "0" * 12] * 2]}, {"q:" + "0" * 12}, {"q:" + "0" * 12})
    assert same_in_one_pair == [("pairs[0][1]", "id already used at pairs[0]")]


def test_a_non_public_id_is_refused_and_each_column_has_its_own_set(tmp_path):
    private_style = "q:" + "f" * 12
    errs = _errors(tmp_path, pairs=[[private_style, R[0]], [G[1], f"{CANARY}-text"]])
    assert errs == [
        ("v2_pairing.json", "pairs[0][0]", "not a public golden-v1 id"),
        ("v2_pairing.json", "pairs[1][1]", "not a public realistic-v1 id"),
    ]
    swapped = _errors(tmp_path, pairs=[[R[0], G[0]]])  # realistic id in the golden column and vice versa
    assert [p for _, p, _ in swapped] == ["pairs[0][0]", "pairs[0][1]"]


def test_pairing_shape_errors(tmp_path):
    assert check_pairing([], GOLDEN, REALISTIC)[0][0] == "pairs"
    assert check_pairing({"pairs": [], "note": "x"}, GOLDEN, REALISTIC)[0][0] == "pairs"
    assert check_pairing({"pairs": "x"}, GOLDEN, REALISTIC) == [("pairs", "must be a list")]
    errs = check_pairing({"pairs": [[G[0]], "x", [G[0], 7], [G[1], ""]]}, GOLDEN, REALISTIC)
    assert [p for p, _ in errs] == ["pairs[0]", "pairs[1]", "pairs[2][1]", "pairs[3][1]"]


# --- conflict labels ----------------------------------------------------------------
def test_an_uninventoried_label_is_refused(tmp_path):
    errs = _errors(tmp_path, labels=["901.1", "909.9", f"{CANARY} 1.1"])
    assert errs == [
        ("conflict_sections.json", "labels[1]", "not in the section inventory"),
        ("conflict_sections.json", "labels[2]", "not in the section inventory"),
    ]


def test_conflict_shape_errors():
    assert check_conflict_labels({"labels": "x"}, INVENTORY) == [("labels", "must be a list")]
    assert check_conflict_labels({"labels": [], "x": 1}, INVENTORY)[0][0] == "labels"
    assert [p for p, _ in check_conflict_labels({"labels": ["", 3, None]}, INVENTORY)] == ["labels[0]", "labels[1]", "labels[2]"]
    with pytest.raises(InventoryError):
        check_conflict_labels({"labels": ["901.1"]}, {"sections": []})


# --- files ----------------------------------------------------------------------------
def test_every_problem_is_reported_together_and_files_must_exist_and_parse(tmp_path):
    errs = _errors(tmp_path, pairs=[[G[0], R[0]], [G[0], R[0]]], labels=["nope"])
    assert {f for f, _, _ in errs} == {"v2_pairing.json", "conflict_sections.json"}
    assert len(errs) == 3

    bad = _write(tmp_path, "v2_pairing.json", "{not json")
    ok = _write(tmp_path, "conflict_sections.json", {"labels": []})
    with pytest.raises(InputsError) as info:
        inputs_check(bad, ok, inventory=INVENTORY, golden_ids=GOLDEN, realistic_ids=REALISTIC)
    assert info.value.errors == [("v2_pairing.json", "file", "invalid JSON")]
    with pytest.raises(InputsError) as info:
        inputs_check(tmp_path / "absent_pairing.json", ok, inventory=INVENTORY, golden_ids=GOLDEN, realistic_ids=REALISTIC)
    assert info.value.errors == [("absent_pairing.json", "file", "missing")]


# --- the committed public sets and inventory -------------------------------------------
def test_public_v1_ids_come_from_the_registered_public_sets():
    golden, realistic = public_v1_ids("golden-v1"), public_v1_ids("realistic-v1")
    assert golden and realistic and not (golden & realistic)
    assert all(i.startswith("q:") and len(i) == 14 for i in golden | realistic)
    with open(eval_sets.REPO_ROOT / "eval" / "golden_set.jsonl", encoding="utf-8") as fh:
        questions = [json.loads(line)["question"] for line in fh if line.strip()]
    assert golden == {public_v1_id(q) for q in questions}


def test_default_ids_and_inventory_check_the_committed_files(tmp_path):
    g, r = sorted(public_v1_ids("golden-v1")), sorted(public_v1_ids("realistic-v1"))
    label = load_inventory()["sections"][0]
    pairing = _write(tmp_path, "v2_pairing.json", {"pairs": [[g[0], r[0]]]})
    conflict = _write(tmp_path, "conflict_sections.json", {"labels": [label]})
    assert inputs_check(pairing, conflict) == {"pairs": 1, "labels": 1}
    pairing = _write(tmp_path, "v2_pairing.json", {"pairs": [[r[0], g[0]]]})  # columns swapped: not public in its column
    with pytest.raises(InputsError):
        inputs_check(pairing, conflict)


def test_public_v1_ids_refuse_an_unknown_or_drifted_set(monkeypatch, tmp_path):
    with pytest.raises(InputsError, match="not a registered public set"):
        public_v1_ids("no-such-set")
    drifted = tmp_path / "golden.jsonl"
    drifted.write_text(json.dumps({"question": f"{CANARY} q", "type": "direct", "expected_sections": ["1.1"]}) + "\n")
    entry = eval_sets.SetEntry("golden-v1", str(drifted), "public", "tuning", "active", "0" * 64)
    monkeypatch.setattr(sealed_inputs, "public_entries", lambda: [entry])
    with pytest.raises(InputsError, match="registered sha256") as info:
        public_v1_ids("golden-v1")
    assert CANARY not in str(info.value)
