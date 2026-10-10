"""Phase 16A-1 (g): schema v2 loader and validate_eval_set.py, synthetic rows only.

Every row, keyword, section label and inventory here is invented. Text-bearing
values carry a ``P16-CANARY`` marker so each error test can assert that no row
text, keyword, section or raw bad value reaches an error message or CLI output.
"""

import json

import pytest

from scripts import validate_eval_set as cli
from src import eval_schema
from src.eval_privacy import SealedInputError, private_v1_id, public_v1_id
from src.eval_schema import (
    InventoryError,
    SchemaError,
    detect_schema,
    load_any,
    load_inventory,
    load_v2,
)
from src.eval_sets import sha256_file
from src.evaluator import load_golden_set

CANARY = "P16-CANARY"
SECTIONS = ["901.1", "901.2", "902.7.3"]
ALIASES = ["901.2.4"]
INVENTORY = {"version": 1, "map_sha256": "a" * 64, "sections": SECTIONS, "aliases": ALIASES}


def _q(tag):
    return f"{CANARY} invented widget question {tag} {CANARY}"


def _row(n=0, fam="f0000abcd", **over):
    row = {
        "schema": 2,
        "id": f"{fam}-{n}",
        "family_id": fam,
        "question": _q(n),
        "scope": "answer",
        "evidence": [["901.1", "901.2"], ["902.7.3"]],
    }
    row.update(over)
    return row


def _partial(n=0, fam="f0000abcd", **over):
    base = {
        "scope": "partial",
        "gaps": [{"id": "g1", "keywords": [f"{CANARY}-kw"]}],
    }
    base.update(over)
    return _row(n, fam, **base)


def _refuse(n=0, fam="f0000abcd", **over):
    base = {"scope": "refuse", "evidence": []}
    base.update(over)
    return _row(n, fam, **base)


def _write(path, rows):
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _assert_no_text(text):
    assert CANARY not in text
    assert "901.1" not in text and "902.7.3" not in text and "999.9" not in text


def _errors(path, **kw):
    with pytest.raises(SchemaError) as info:
        load_v2(path, inventory=kw.pop("inventory", INVENTORY), **kw)
    _assert_no_text(str(info.value))
    _assert_no_text(repr(info.value.args))
    return info.value.errors


# --- valid sets ---------------------------------------------------------------
def test_valid_private_v2_set_loads_normalised(tmp_path):
    p = _write(
        tmp_path / "set.jsonl",
        [
            _row(0, type="direct", register="lay", source="tutorial", ambiguous=False,
                 owner_status="approved", second_status="not_required"),
            _row(1, evidence=[[" 901.2.4 "]]),  # an alias is inventoried; stripped
            _partial(0, fam="f1111abcd", type="exact_token"),
            _refuse(0, fam="f2222abcd", type="refusal"),
        ],
    )
    rows = load_v2(p, inventory=INVENTORY)
    assert [r["id"] for r in rows] == ["f0000abcd-0", "f0000abcd-1", "f1111abcd-0", "f2222abcd-0"]
    assert set(rows[0]) == {
        "schema", "id", "family_id", "question", "scope", "evidence", "gaps", "type",
        "register", "source", "ambiguous", "owner_status", "second_status",
    }
    assert rows[0]["evidence"] == [["901.1", "901.2"], ["902.7.3"]]
    assert rows[0]["register"] == "lay" and rows[0]["second_status"] == "not_required"
    assert rows[1]["evidence"] == [["901.2.4"]] and rows[1]["type"] is None
    assert rows[2]["gaps"] == [{"id": "g1", "keywords": [f"{CANARY}-kw"]}]
    assert rows[3]["evidence"] == [] and rows[3]["gaps"] == []
    assert load_any(p, inventory=INVENTORY) == rows
    assert detect_schema(p) == 2


def test_public_v2_set_uses_public_id_rules(tmp_path, eval_registry):
    p = _write(tmp_path / "pub.jsonl", [_row(0, id="widget.q-001", family_id="widget_fam")])
    eval_registry.add(p)
    assert load_v2(p, inventory=INVENTORY)[0]["id"] == "widget.q-001"
    # the same row in an unregistered (private) copy is non-opaque
    q = _write(tmp_path / "copy.jsonl", [_row(0, id="widget.q-001", family_id="widget_fam")])
    errs = _errors(q)
    assert (1, "family_id", "not an opaque family id") in errs
    assert (1, "id", "not an opaque row id") in errs


def test_public_id_regex_rejects_bad_ids(tmp_path, eval_registry):
    p = _write(tmp_path / "pub.jsonl", [_row(0, id="ab", family_id="-bad"), _row(1, id="x" * 65, family_id="ok_fam")])
    eval_registry.add(p)
    errs = _errors(p)
    assert (1, "id", "not a valid public id") in errs
    assert (1, "family_id", "not a valid public id") in errs
    assert (2, "id", "not a valid public id") in errs


# --- (g) error classes ------------------------------------------------------
def test_duplicate_id(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0), _row(1), _row(0)])
    assert _errors(p) == [(3, "id", "duplicate id")]


@pytest.mark.parametrize(
    "rid,fid,expected",
    [
        (f"{CANARY}-0", "f0000abcd", (1, "id", "not an opaque row id")),
        ("f0000abcd-01", "f0000abcd", (1, "id", "not an opaque row id")),
        ("f0000abcd-1", f"{CANARY}", (1, "family_id", "not an opaque family id")),
        ("f0000abcd-1", "f0000ABCD", (1, "family_id", "not an opaque family id")),
        ("f1111abcd-1", "f0000abcd", (1, "id", "id does not extend its family_id")),
        (7, "f0000abcd", (1, "id", "not an opaque row id")),
    ],
)
def test_non_opaque_id(tmp_path, rid, fid, expected):
    p = _write(tmp_path / "s.jsonl", [_row(0, id=rid, family_id=fid)])
    assert expected in _errors(p)


def test_mixed_versions(tmp_path):
    v1 = {"question": _q("v1"), "type": "direct", "expected_sections": ["901.1"]}
    p = _write(tmp_path / "s.jsonl", [_row(0), v1, _row(1)])
    errs = _errors(p)
    assert errs == [(2, "schema", "mixed schema versions")]
    with pytest.raises(SchemaError):
        detect_schema(p)
    with pytest.raises(SchemaError):
        load_any(p, inventory=INVENTORY)


def test_unknown_schema_value_and_bool(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0, schema=3), _row(1, schema=True)])
    assert _errors(p) == [(1, "schema", "unknown schema version"), (2, "schema", "unknown schema version")]


def test_v1_file_through_load_v2_refused(tmp_path):
    p = _write(tmp_path / "s.jsonl", [{"question": _q("v1"), "type": "refusal", "expected_sections": []}])
    assert _errors(p) == [(1, "schema", "expected schema 2")]


@pytest.mark.parametrize(
    "row,expected",
    [
        (_refuse(0, evidence=[["901.1"]]), (1, "evidence", "must be [] for scope refuse")),
        (_row(0, evidence=[]), (1, "evidence", "must be non-empty unless scope is refuse")),
        (_partial(0, evidence=[]), (1, "evidence", "must be non-empty unless scope is refuse")),
        (_row(0, gaps=[{"id": "g1", "keywords": ["k"]}]), (1, "gaps", "only allowed for scope partial")),
        (_refuse(0, gaps=[{"id": "g1", "keywords": ["k"]}]), (1, "gaps", "only allowed for scope partial")),
        ({k: v for k, v in _partial(0).items() if k != "gaps"}, (1, "gaps", "required for scope partial")),
        (_partial(0, gaps=[]), (1, "gaps", "must be a non-empty list")),
        (_row(0, scope=f"{CANARY}"), (1, "scope", "unknown scope")),
        (_row(0, evidence=f"{CANARY}"), (1, "evidence", "must be a list of groups")),
        (_row(0, evidence=[f"{CANARY}"]), (1, "evidence[0]", "group must be a list")),
        (_partial(0, gaps=[{"id": "g1", "keywords": ["k"], "note": CANARY}]),
         (1, "gaps[0]", "gap keys must be id and keywords")),
        (_partial(0, gaps=[{"id": "g1", "keywords": ["k"]}, {"id": "g1", "keywords": ["k"]}]),
         (1, "gaps[1].id", "duplicate gap id")),
        (_partial(0, gaps=[{"id": f"{CANARY} x", "keywords": ["k"]}]), (1, "gaps[0].id", "not a valid gap id")),
    ],
)
def test_incoherent_scope_evidence_gaps(tmp_path, row, expected):
    p = _write(tmp_path / "s.jsonl", [row])
    assert expected in _errors(p)


@pytest.mark.parametrize(
    "keywords,expected",
    [
        ([], (1, "gaps[0].keywords", "must be a non-empty list")),
        ([""], (1, "gaps[0].keywords[0]", "empty keyword")),
        ([f"{CANARY}-ok", "   "], (1, "gaps[0].keywords[1]", "empty keyword")),
        ([f"{CANARY}-ok", 3], (1, "gaps[0].keywords[1]", "empty keyword")),
    ],
)
def test_empty_keywords(tmp_path, keywords, expected):
    p = _write(tmp_path / "s.jsonl", [_partial(0, gaps=[{"id": "g1", "keywords": keywords}])])
    assert expected in _errors(p)


@pytest.mark.parametrize(
    "row,expected",
    [
        (_row(0, type="refusal"), (1, "type", "type disagrees with scope")),
        (_partial(0, type="refusal"), (1, "type", "type disagrees with scope")),
        (_refuse(0, type="direct"), (1, "type", "type disagrees with scope")),
        (_refuse(0, type="exact_token"), (1, "type", "type disagrees with scope")),
        (_row(0, type=f"{CANARY}"), (1, "type", "unknown type")),
    ],
)
def test_type_scope_clash(tmp_path, row, expected):
    p = _write(tmp_path / "s.jsonl", [row])
    assert _errors(p) == [expected]


def test_empty_group(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0, evidence=[["901.1"], []]), _row(1, evidence=[["901.1", ""]])])
    errs = _errors(p)
    assert (1, "evidence[1]", "empty group") in errs
    assert (2, "evidence[0][1]", "section must be a non-empty string") in errs


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("owner_status", f"{CANARY}", "unknown status"),
        ("second_status", "approved", "unknown status"),
        ("register", f"{CANARY}", "unknown value"),
        ("source", "web", "unknown value"),
        ("ambiguous", "yes", "must be a boolean"),
    ],
)
def test_unknown_status_and_enums(tmp_path, key, value, reason):
    p = _write(tmp_path / "s.jsonl", [_row(0, **{key: value})])
    assert _errors(p) == [(1, key, reason)]


def test_uninventoried_section(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0), _row(1, evidence=[["901.1"], ["902.7.3", "999.9"]])])
    assert _errors(p) == [(2, "evidence[1][1]", "section not in inventory")]


def test_unknown_key_reported_without_its_name(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0, **{f"{CANARY}_key": 1})])
    assert _errors(p) == [(1, "<unknown>", "unknown key")]


def test_missing_required_and_bad_question(tmp_path):
    row = _row(0, question="  ")
    del row["family_id"]
    p = _write(tmp_path / "s.jsonl", [row])
    errs = _errors(p)
    assert (1, "family_id", "missing") in errs
    assert (1, "question", "must be a non-empty string") in errs


def test_unparseable_and_non_object_lines(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0), f'{{"question": "{CANARY}" ', "", f'["{CANARY}"]'])
    assert _errors(p) == [(2, "<row>", "invalid JSON"), (4, "<row>", "row is not a JSON object")]


def test_invalid_utf8_line(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(json.dumps(_row(0)).encode() + b"\n" + b'{"q": "\xff"}\n')
    assert _errors(p) == [(2, "<row>", "invalid UTF-8")]


def test_empty_file(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n\n")
    assert _errors(p) == [(0, "<file>", "no rows")]


def test_errors_render_line_field_reason_only(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0, scope="refuse")])
    with pytest.raises(SchemaError) as info:
        load_v2(p, inventory=INVENTORY)
    for line in str(info.value).splitlines():
        assert line.startswith("line 1: ")
    assert str(info.value) == "line 1: evidence: must be [] for scope refuse"


# --- inventory ---------------------------------------------------------------
def test_inventory_default_missing_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", tmp_path / "absent.json")
    p = _write(tmp_path / "s.jsonl", [_row(0)])
    with pytest.raises(InventoryError) as info:
        load_v2(p)
    assert info.value.errors == [(0, "inventory", "inventory file missing")]


def test_inventory_default_file_is_used(tmp_path, monkeypatch):
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps(INVENTORY))
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
    p = _write(tmp_path / "s.jsonl", [_row(0)])
    assert len(load_v2(p)) == 1


@pytest.mark.parametrize(
    "bad,field",
    [
        ({**INVENTORY, "version": 2}, "inventory.version"),
        ({**INVENTORY, "map_sha256": "xyz"}, "inventory.map_sha256"),
        ({**INVENTORY, "sections": ["901.1", ""]}, "inventory.sections"),
        ({**INVENTORY, "aliases": "901.2.4"}, "inventory.aliases"),
        ({k: v for k, v in INVENTORY.items() if k != "aliases"}, "inventory"),
    ],
)
def test_malformed_inventory(tmp_path, bad, field):
    p = _write(tmp_path / "s.jsonl", [_row(0)])
    with pytest.raises(InventoryError) as info:
        load_v2(p, inventory=bad)
    assert field in {f for _, f, _ in info.value.errors}
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps(bad))
    with pytest.raises(InventoryError):
        load_inventory(inv)


def test_v1_file_needs_no_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", tmp_path / "absent.json")
    p = _write(tmp_path / "v1.jsonl", [{"question": _q("a"), "type": "direct", "expected_sections": ["x"]}])
    assert load_any(p)[0]["evidence"] == [["x"]]


# --- v1 normalisation ---------------------------------------------------------
V1_ROWS = [
    {"question": _q("a"), "type": "direct", "expected_sections": [" 901.1 ", "901.2"]},
    {"question": _q("b"), "type": "exact_token", "expected_sections": ["902.7.3"], "extra": "ignored"},
    {"question": _q("c"), "type": "refusal", "expected_sections": []},
    {"question": _q("d"), "type": "refusal"},
]


def test_v1_public_ids_and_golden_parity(tmp_path, eval_registry):
    p = _write(tmp_path / "v1.jsonl", V1_ROWS)
    eval_registry.add(p)
    rows = load_any(p)
    golden = load_golden_set(str(p))
    assert detect_schema(p) == 1
    assert [
        {"question": r["question"], "type": r["type"],
         "expected_sections": r["evidence"][0] if r["evidence"] else []}
        for r in rows
    ] == golden
    for r, src in zip(rows, V1_ROWS):
        assert r["id"] == r["family_id"] == public_v1_id(src["question"])
        assert r["schema"] == 1 and r["gaps"] == []
    assert [r["scope"] for r in rows] == ["answer", "answer", "refuse", "refuse"]


def test_v1_private_ids_salted_by_file_sha(tmp_path):
    p = _write(tmp_path / "v1.jsonl", V1_ROWS)
    rows = load_any(p)
    salt = sha256_file(p)
    assert [r["id"] for r in rows] == [private_v1_id(s["question"], salt) for s in V1_ROWS]
    assert rows[0]["id"] != public_v1_id(V1_ROWS[0]["question"])
    assert all(r["family_id"] == r["id"] for r in rows)


def test_v1_duplicate_question_is_duplicate_id(tmp_path):
    p = _write(tmp_path / "v1.jsonl", [V1_ROWS[0], V1_ROWS[2], V1_ROWS[0]])
    with pytest.raises(SchemaError) as info:
        load_any(p)
    assert info.value.errors == [(3, "id", "duplicate id")]
    _assert_no_text(str(info.value))


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"question": "", "type": "direct", "expected_sections": ["x"]}, (1, "question", "must be a non-empty string")),
        ({"question": _q("t"), "type": CANARY, "expected_sections": ["x"]}, (1, "type", "unknown type")),
        ({"question": _q("t"), "type": "refusal", "expected_sections": [CANARY]},
         (1, "expected_sections", "must be [] for type refusal")),
        ({"question": _q("t"), "type": "direct", "expected_sections": CANARY},
         (1, "expected_sections", "must be a non-empty list")),
        ({"question": _q("t"), "type": "direct", "expected_sections": [CANARY, " "]},
         (1, "expected_sections", "sections must be non-empty strings")),
    ],
)
def test_v1_invalid_rows_without_text(tmp_path, row, expected):
    p = _write(tmp_path / "v1.jsonl", [row])
    with pytest.raises(SchemaError) as info:
        load_any(p)
    assert info.value.errors == [expected]
    _assert_no_text(str(info.value))
    # load_golden_set rejects the same row (it may quote values; we do not)
    with pytest.raises(ValueError):
        load_golden_set(str(p))


# --- sealed refusal (item 1) ----------------------------------------------------
def _sealed_file(tmp_path):
    return _write(tmp_path / "s.jsonl", [_row(0), _row(1, sealed=True)])


def test_load_v2_refuses_sealed_before_inventory(tmp_path, monkeypatch):
    p = _sealed_file(tmp_path)
    monkeypatch.setattr(eval_schema, "_read_rows", lambda *_: pytest.fail("rows read"))
    monkeypatch.setattr(eval_schema, "load_inventory", lambda *_: pytest.fail("inventory read"))
    for fn in (load_v2, load_any, detect_schema):
        with pytest.raises(SealedInputError):
            fn(p)


def test_sealed_root_location_refused(tmp_path, _private_root_in_tmp):
    d = _private_root_in_tmp / "sealed"
    d.mkdir(parents=True)
    p = _write(d / "x.jsonl", [_row(0)])
    with pytest.raises(SealedInputError):
        load_v2(p, inventory=INVENTORY)
    assert cli.main([str(p)]) == 4


def test_malformed_marked_line_refused(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_row(0), '{"sealed": true, "question": '])
    with pytest.raises(SealedInputError):
        load_v2(p, inventory=INVENTORY)


# --- CLI ------------------------------------------------------------------------
def _inv_file(tmp_path):
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps(INVENTORY))
    return str(inv)


def test_cli_valid_prints_counts_only(tmp_path, capsys):
    p = _write(tmp_path / "s.jsonl", [_row(0), _row(1), _partial(0, fam="f1111abcd"), _refuse(0, fam="f2222abcd")])
    assert cli.main([str(p), "--inventory", _inv_file(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["schema: 2", "rows: 4", "families: 3", "scopes: answer=2, partial=1, refuse=1"]
    assert err == ""
    _assert_no_text(out)
    assert "f0000abcd" not in out


def test_cli_invalid_prints_line_field_reason(tmp_path, capsys):
    p = _write(
        tmp_path / "s.jsonl",
        [_row(0), _row(0), _partial(1, gaps=[{"id": "g1", "keywords": [""]}]), _row(2, evidence=[["999.9"]]),
         _row(3, owner_status=CANARY)],
    )
    assert cli.main([str(p), "--inventory", _inv_file(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "line 2: id: duplicate id",
        "line 3: gaps[0].keywords[0]: empty keyword",
        "line 4: evidence[0][0]: section not in inventory",
        "line 5: owner_status: unknown status",
    ]
    _assert_no_text(out + err)


def test_cli_sealed_exits_4_without_reading(tmp_path, capsys, monkeypatch):
    p = _sealed_file(tmp_path)
    monkeypatch.setattr(cli, "load_any", lambda *a, **k: pytest.fail("loaded"))
    monkeypatch.setattr(cli, "load_inventory", lambda *a, **k: pytest.fail("inventory read"))
    assert cli.main([str(p), "--inventory", str(tmp_path / "nope.json")]) == 4
    out, err = capsys.readouterr()
    assert out == "" and "sealed" in err
    _assert_no_text(out + err)


def test_cli_missing_inventory_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", tmp_path / "absent.json")
    p = _write(tmp_path / "s.jsonl", [_row(0)])
    assert cli.main([str(p)]) == 2
    assert "inventory file missing" in capsys.readouterr().err
    assert cli.main([str(p), "--inventory", str(tmp_path / "nope.json")]) == 2


def test_cli_missing_input_exits_2(tmp_path):
    assert cli.main([str(tmp_path / "absent.jsonl")]) == 2


def test_cli_v1_file(tmp_path, capsys):
    p = _write(tmp_path / "v1.jsonl", V1_ROWS)
    assert cli.main([str(p)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["schema: 1", "rows: 4", "families: 4", "scopes: answer=2, partial=0, refuse=2"]
