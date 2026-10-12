"""Phase 16A-2a (a1): the sealed schema key, the read-only sealed loader, ``--sealed`` and the row hash.

Every row, section label and inventory here is invented and schema-valid.
Text-bearing values carry a ``P16-CANARY`` marker so each error test can
assert that no row text reaches an error message or CLI output. Nothing here
retrieves, expands or calls a model.
"""

import hashlib
import json
import re
import socket
import subprocess
import sys

import pytest

from scripts import validate_eval_set as cli
from src import eval_schema
from src.eval_privacy import SealedInputError, check_floor, require_class
from src.eval_schema import (
    ROW_HASH_EXCLUDED_KEYS,
    InventoryError,
    SchemaError,
    detect_schema,
    load_any,
    load_v2,
    row_hash,
)
from src.eval_sealed_read import NotSealedInput, load_sealed
from src.eval_sets import classify
from src.evaluator import load_golden_set
from tests.test_p16_cli import _run
from tests.test_p16w_schema import CANARY, INVENTORY, _assert_no_text, _partial, _refuse, _row, _write

ERROR_LINE = re.compile(r"^line \d+: [A-Za-z_<>\[\]0-9.]+: [a-z0-9 /.]+$")


def _sealed_dir(private_root):
    d = private_root / "sealed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _good_rows():
    return [_row(0), _row(1), _partial(0, fam="f1111abcd"), _refuse(0, fam="f2222abcd")]


@pytest.fixture
def sealed_by_location(_private_root_in_tmp):
    """A valid sealed set under ``eval/private/sealed/`` whose rows carry no marker."""
    return _write(_sealed_dir(_private_root_in_tmp) / "set.jsonl", _good_rows())


@pytest.fixture
def sealed_by_marker(tmp_path):
    """A valid sealed set anywhere else: one marked row makes the whole file sealed."""
    rows = _good_rows()
    rows[2] = dict(rows[2], sealed=True)
    return _write(tmp_path / "marked.jsonl", rows)


# --- schema: the optional ``sealed`` key, true only ---------------------------------
@pytest.mark.parametrize("value", [False, 1, 0, "true", None, [], {}, 1.0])
def test_sealed_key_other_than_true_is_a_field_error(tmp_path, value):
    p = _write(tmp_path / "p.jsonl", [_row(0), _row(1, sealed=value)])
    with pytest.raises(SchemaError) as info:
        load_v2(p, inventory=INVENTORY)
    assert info.value.errors == [(2, "sealed", "must be true")]
    _assert_no_text(str(info.value))


def test_sealed_key_is_known_not_an_unknown_key(tmp_path):
    """Before 16A-2a ``sealed`` was ``<unknown>``; now it is its own field."""
    p = _write(tmp_path / "p.jsonl", [_row(0, sealed=False)])
    with pytest.raises(SchemaError) as info:
        load_v2(p, inventory=INVENTORY)
    assert [f for _, f, _ in info.value.errors] == ["sealed"]


def test_sealed_true_loads_only_through_the_sealed_loader(sealed_by_marker):
    assert len(load_sealed(sealed_by_marker, inventory=INVENTORY)) == 4
    for fn in (load_v2, load_any, detect_schema):
        with pytest.raises(SealedInputError):
            fn(sealed_by_marker, **({} if fn is detect_schema else {"inventory": INVENTORY}))


def test_duplicate_sealed_key_cannot_hide_the_marker(tmp_path):
    """``{"sealed": true, "sealed": false}`` parses as false, but the file is still sealed."""
    row = json.dumps(_row(0))[:-1] + ', "sealed": true, "sealed": false}'
    p = _write(tmp_path / "d.jsonl", [row])
    assert classify(p) == "sealed"
    with pytest.raises(SchemaError) as info:
        load_sealed(p, inventory=INVENTORY)
    assert info.value.errors == [(1, "sealed", "must be true")]


# --- the sealed loader ---------------------------------------------------------------
def test_loader_reads_a_set_sealed_by_location(sealed_by_location):
    assert classify(sealed_by_location) == "sealed"
    rows = load_sealed(sealed_by_location, inventory=INVENTORY)
    assert [r["id"] for r in rows] == ["f0000abcd-0", "f0000abcd-1", "f1111abcd-0", "f2222abcd-0"]
    assert [r["scope"] for r in rows] == ["answer", "answer", "partial", "refuse"]


def test_loader_reads_a_set_sealed_by_marker(sealed_by_marker):
    rows = load_sealed(sealed_by_marker, inventory=INVENTORY)
    assert len(rows) == 4


def test_loader_returns_the_shape_load_v2_returns(sealed_by_location, tmp_path):
    """Same validator, same normalised rows: the sealed copy equals the private copy."""
    twin = _write(tmp_path / "twin.jsonl", _good_rows())
    assert classify(twin) == "private"
    assert load_sealed(sealed_by_location, inventory=INVENTORY) == load_v2(twin, inventory=INVENTORY)


def test_loader_refuses_a_path_that_is_not_sealed(tmp_path, monkeypatch):
    p = _write(tmp_path / "plain.jsonl", _good_rows())
    monkeypatch.setattr(eval_schema, "_read_rows", lambda *_: pytest.fail("rows read"))
    with pytest.raises(NotSealedInput):
        load_sealed(p, inventory=INVENTORY)


def test_loader_never_reads_a_markdown_or_python_file(_private_root_in_tmp, monkeypatch):
    monkeypatch.setattr(eval_schema, "_read_rows", lambda *_: pytest.fail("rows read"))
    for name in ("renamed.md", "renamed.py"):
        p = _write(_sealed_dir(_private_root_in_tmp) / name, [_row(0)])
        with pytest.raises(SchemaError) as info:
            load_sealed(p, inventory=INVENTORY)
        assert info.value.errors == [(0, "<file>", "not an eval input (.md/.py)")]


def test_loader_uses_opaque_ids_never_public_ones(_private_root_in_tmp):
    rows = [_row(0, fam="public-family-1", id="public-row-1")]
    p = _write(_sealed_dir(_private_root_in_tmp) / "ids.jsonl", rows)
    with pytest.raises(SchemaError) as info:
        load_sealed(p, inventory=INVENTORY)
    assert info.value.errors == [(1, "family_id", "not an opaque family id"), (1, "id", "not an opaque row id")]


def test_loader_reports_line_and_field_errors_without_text(_private_root_in_tmp):
    rows = [
        _row(0),
        _row(0),
        _partial(1, gaps=[{"id": "g1", "keywords": [""]}]),
        _row(2, evidence=[["999.9"]]),
        _row(3, sealed=False),
    ]
    p = _write(_sealed_dir(_private_root_in_tmp) / "bad.jsonl", rows)
    with pytest.raises(SchemaError) as info:
        load_sealed(p, inventory=INVENTORY)
    _assert_no_text(str(info.value))
    _assert_no_text(repr(info.value.args))
    assert info.value.errors == [
        (2, "id", "duplicate id"),
        (2, "question", "duplicate question"),
        (3, "gaps[0].keywords[0]", "empty keyword"),
        (3, "scope", "scope differs within family"),
        (4, "evidence[0][0]", "section not in inventory"),
        (5, "sealed", "must be true"),
    ]


def test_loader_reports_an_unparseable_line_without_text(_private_root_in_tmp):
    p = _write(_sealed_dir(_private_root_in_tmp) / "junk.jsonl", [_row(0), "{not json " + CANARY])
    with pytest.raises(SchemaError) as info:
        load_sealed(p, inventory=INVENTORY)
    assert info.value.errors == [(2, "<row>", "invalid JSON")]
    _assert_no_text(str(info.value))


def test_loader_rejects_v1_and_mixed_rows(_private_root_in_tmp):
    d = _sealed_dir(_private_root_in_tmp)
    v1 = _write(d / "v1.jsonl", [{"question": _row(0)["question"], "type": "direct", "expected_sections": ["901.1"]}])
    with pytest.raises(SchemaError) as info:
        load_sealed(v1, inventory=INVENTORY)
    assert info.value.errors == [(1, "schema", "expected schema 2")]
    mixed = _write(d / "mixed.jsonl", [_row(0), {"question": "x", "type": "direct", "expected_sections": ["901.1"]}])
    with pytest.raises(SchemaError) as info:
        load_sealed(mixed, inventory=INVENTORY)
    assert info.value.errors == [(2, "schema", "mixed schema versions")]


def test_loader_never_skips_the_inventory(sealed_by_location, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", tmp_path / "absent.json")
    with pytest.raises(InventoryError):
        load_sealed(sealed_by_location)


def _loaded_banned_modules(import_line):
    """Banned retrieval/generation/SDK modules loaded by a cold process running ``import_line``."""
    code = (
        f"import sys; {import_line}\n"
        "banned = ('src.evaluator', 'src.retriever', 'src.generator', 'src.query_rewrite', 'src.embedder',\n"
        "          'src.judge', 'src.pipeline', 'src.spend', 'anthropic', 'langchain_anthropic', 'chromadb',\n"
        "          'sentence_transformers', 'httpx')\n"
        "print(sorted(m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in banned)))\n"
    )
    from tests.test_p16_ast_hygiene import ROOT

    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_loader_imports_nothing_that_retrieves_or_calls_a_model():
    """A cold process importing the loader and the validator loads no retrieval, generation or SDK module."""
    assert _loaded_banned_modules("import src.eval_sealed_read, scripts.validate_eval_set") == "[]"
    # Positive control: the same probe does see the evaluator (and the SDK it pulls in).
    assert "src.evaluator" in _loaded_banned_modules("import src.evaluator")


def test_loader_and_validator_open_no_connection(sealed_by_location, monkeypatch, capsys):
    def boom(*a, **k):
        pytest.fail("network used")

    monkeypatch.setattr(socket.socket, "connect", boom)
    assert load_sealed(sealed_by_location, inventory=INVENTORY)
    inv = sealed_by_location.parent / "inv.json"
    inv.write_text(json.dumps(INVENTORY))
    assert cli.main([str(sealed_by_location), "--sealed", "--inventory", str(inv)]) == 0
    assert capsys.readouterr() == ("", "")


# --- every other entry still refuses sealed -----------------------------------------
def test_other_loaders_and_floors_still_refuse_a_valid_sealed_set(sealed_by_location, sealed_by_marker):
    for p in (sealed_by_location, sealed_by_marker):
        for fn in (load_v2, load_any):
            with pytest.raises(SealedInputError):
                fn(p, inventory=INVENTORY)
        with pytest.raises(SealedInputError):
            detect_schema(p)
        with pytest.raises(SealedInputError):
            load_golden_set(str(p))
    with pytest.raises(SealedInputError):
        check_floor("private", "sealed")
    with pytest.raises(SealedInputError):
        require_class("sealed")


@pytest.mark.parametrize("flag", ["--golden", "--heldout", "--realistic", "--expansion"])
@pytest.mark.parametrize("which", ["sealed_by_location", "sealed_by_marker"])
def test_pipeline_eval_exits_4_for_a_valid_sealed_set(monkeypatch, request, flag, which):
    path = request.getfixturevalue(which)
    code, captured = _run(monkeypatch, [flag, str(path), "--skip-refusals", "--skip-completeness"])
    assert code == 4
    assert captured == {}  # the matrix runner was never reached


def test_validator_without_sealed_flag_still_exits_4(sealed_by_location, sealed_by_marker, capsys):
    for p in (sealed_by_location, sealed_by_marker):
        assert cli.main([str(p)]) == 4
        out, err = capsys.readouterr()
        assert out == "" and "sealed" in err


# --- validate_eval_set.py --sealed ----------------------------------------------------
def _inv_file(tmp_path):
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps(INVENTORY))
    return str(inv)


@pytest.mark.parametrize("which", ["sealed_by_location", "sealed_by_marker"])
def test_cli_sealed_valid_prints_nothing_and_exits_0(tmp_path, capsys, request, which):
    path = request.getfixturevalue(which)
    assert cli.main([str(path), "--sealed", "--inventory", _inv_file(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")


def test_cli_sealed_invalid_prints_line_and_field_errors_only(_private_root_in_tmp, tmp_path, capsys):
    p = _write(
        _sealed_dir(_private_root_in_tmp) / "bad.jsonl",
        [_row(0), _row(0), _row(1, evidence=[["999.9"]]), _row(2, sealed=0), _row(3, owner_status=CANARY)],
    )
    assert cli.main([str(p), "--sealed", "--inventory", _inv_file(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "line 2: id: duplicate id",
        "line 2: question: duplicate question",
        "line 3: evidence[0][0]: section not in inventory",
        "line 4: sealed: must be true",
        "line 5: owner_status: unknown status",
    ]
    assert all(ERROR_LINE.match(line) for line in out.splitlines())
    assert err == ""
    _assert_no_text(out + err)
    assert "f0000abcd" not in out  # not even an opaque id


def test_cli_sealed_on_a_path_that_is_not_sealed_exits_2(tmp_path, capsys):
    p = _write(tmp_path / "plain.jsonl", _good_rows())
    assert cli.main([str(p), "--sealed", "--inventory", _inv_file(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "not sealed" in err


def test_cli_sealed_missing_input_or_inventory_exits_2(sealed_by_location, tmp_path, capsys, monkeypatch):
    assert cli.main([str(tmp_path / "absent.jsonl"), "--sealed"]) == 2
    assert cli.main([str(sealed_by_location), "--sealed", "--inventory", str(tmp_path / "nope.json")]) == 2
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", tmp_path / "absent.json")
    assert cli.main([str(sealed_by_location), "--sealed"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "inventory file missing" in err
    _assert_no_text(err)


def test_cli_sealed_uses_the_sealed_loader_only(sealed_by_location, tmp_path, monkeypatch):
    """The ``--sealed`` path never touches the refusing loaders."""
    monkeypatch.setattr(cli, "load_any", lambda *a, **k: pytest.fail("refusing loader used"))
    monkeypatch.setattr(cli, "classify", lambda *a, **k: pytest.fail("refusal check used"))
    assert cli.main([str(sealed_by_location), "--sealed", "--inventory", _inv_file(tmp_path)]) == 0


# --- row hash --------------------------------------------------------------------------
def test_row_hash_known_answer():
    row = {"b": [1, 2], "a": 1, "owner_status": "approved", "second_status": "agree", "sealed": True}
    assert row_hash(row) == hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest()


def test_row_hash_excluded_keys_are_exactly_the_three():
    assert ROW_HASH_EXCLUDED_KEYS == ("sealed", "owner_status", "second_status")


def test_row_hash_ignores_sealed_and_status_keys():
    base = _row(0)
    h = row_hash(base)
    for extra in (
        {"sealed": True},
        {"owner_status": "approved"},
        {"owner_status": "changed", "second_status": "disagree"},
        {"sealed": True, "owner_status": "draft", "second_status": "pending"},
    ):
        assert row_hash(dict(base, **extra)) == h
    assert re.fullmatch(r"[0-9a-f]{64}", h)


@pytest.mark.parametrize(
    "change",
    [
        {"question": "another invented widget question"},
        {"evidence": [["901.2"]]},
        {"scope": "partial"},
        {"id": "f0000abcd-9"},
        {"family_id": "f9999abcd"},
        {"register": "lay"},
        {"source": "tutorial"},
        {"ambiguous": True},
        {"type": "direct"},
    ],
)
def test_row_hash_changes_with_content(change):
    base = _row(0)
    assert row_hash(dict(base, **change)) != row_hash(base)


def test_row_hash_is_independent_of_key_order_and_does_not_mutate():
    row = _row(0, owner_status="approved", sealed=True)
    reordered = dict(reversed(list(row.items())))
    snapshot = json.dumps(row)
    assert row_hash(row) == row_hash(reordered)
    assert json.dumps(row) == snapshot  # still holds its excluded keys


def test_row_hash_survives_non_ascii_and_lone_surrogates():
    assert row_hash({"question": "café — \ud800"}) == row_hash({"question": "café — \ud800"})
    assert row_hash({"question": "café"}) != row_hash({"question": "cafe"})


def test_row_hash_of_loaded_rows_ignores_review_status(_private_root_in_tmp):
    d = _sealed_dir(_private_root_in_tmp)
    draft = load_sealed(_write(d / "a.jsonl", [_row(0, owner_status="draft", second_status="pending")]), inventory=INVENTORY)
    approved = load_sealed(_write(d / "b.jsonl", [_row(0, owner_status="approved", second_status="agree")]), inventory=INVENTORY)
    assert draft[0]["owner_status"] != approved[0]["owner_status"]
    assert row_hash(draft[0]) == row_hash(approved[0])
    edited = load_sealed(_write(d / "c.jsonl", [_row(0, question=_row(0)["question"] + " edited")]), inventory=INVENTORY)
    assert row_hash(edited[0]) != row_hash(approved[0])
