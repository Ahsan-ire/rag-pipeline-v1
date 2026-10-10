"""Phase 16A-1 (c): eval-set registry and classify() rules, synthetic inputs only."""

import json
import shutil

import pytest

import src.eval_sets as eval_sets
from src.eval_privacy import (
    PrivacyFloorError,
    SealedInputError,
    check_floor,
    private_v1_id,
    public_v1_id,
    require_class,
    strictest,
)
from src.eval_sets import RegistryError, classify, floor, has_sealed_marker, registry_from_dict


def _rows(*questions, sealed_index=None):
    rows = []
    for i, q in enumerate(questions):
        row = {"question": q, "type": "direct", "expected_sections": ["91.1"]}
        if i == sealed_index:
            row["sealed"] = True
        rows.append(row)
    return rows


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# --- rule 1: location under eval/private/sealed ------------------------------
def test_rule1_under_sealed_root(_private_root_in_tmp):
    sealed_dir = _private_root_in_tmp / "sealed"
    sealed_dir.mkdir(parents=True)
    p = _write_jsonl(sealed_dir / "anything.jsonl", _rows("synthetic widget question one"))
    assert classify(p) == "sealed"


# --- rule 2: sealed marker survives copies, renames, reorders, subsets -------
def test_rule2_marker_anywhere(tmp_path):
    p = _write_jsonl(tmp_path / "x.jsonl", _rows("q a", "q b", "q c", sealed_index=1))
    assert classify(p) == "sealed"


def test_rule2_marker_survives_copy_rename_reorder_subset_reserialise(tmp_path):
    rows = _rows("q a", "q b", "q c", sealed_index=1)
    original = _write_jsonl(tmp_path / "orig.jsonl", rows)
    copy = tmp_path / "copy.jsonl"
    shutil.copy(original, copy)
    renamed = tmp_path / "renamed_set.jsonl"
    original.rename(renamed)
    reordered = _write_jsonl(tmp_path / "reordered.jsonl", list(reversed(rows)))
    subset = _write_jsonl(tmp_path / "subset.jsonl", [rows[1]])
    reser = tmp_path / "reser.jsonl"
    reser.write_text("".join(json.dumps(r, indent=None, separators=(",", ":"), sort_keys=True) + "\n" for r in rows))
    as_json = tmp_path / "as_list.json"
    as_json.write_text(json.dumps(rows))
    for p in (copy, renamed, reordered, subset, reser, as_json):
        assert classify(p) == "sealed", p.name


def test_rule2_json_top_level_object(tmp_path):
    p = tmp_path / "artifact.json"
    p.write_text(json.dumps({"sealed": True, "entries": []}))
    assert classify(p) == "sealed"


def test_rule2_malformed_marked_line_fails_closed(tmp_path):
    p = tmp_path / "broken.jsonl"
    p.write_text('{"question": "q a"}\n{"sealed": true, "question": \n')
    assert classify(p) == "sealed"
    j = tmp_path / "broken.json"
    j.write_text('{"sealed": tru')
    assert classify(j) == "sealed"


def test_rule2_marker_text_in_md_or_py_is_not_a_marker(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text('{"sealed": true}\n')
    py = tmp_path / "mod.py"
    py.write_text('X = {"sealed": True}\n')
    assert not has_sealed_marker(md) and not has_sealed_marker(py)
    assert classify(md) == "private" and classify(py) == "private"


def test_sealed_false_is_not_a_marker(tmp_path):
    p = _write_jsonl(tmp_path / "x.jsonl", [{"question": "q", "sealed": False}])
    assert classify(p) == "private"


# --- rule 3: under eval/private ---------------------------------------------
def test_rule3_under_private_root(_private_root_in_tmp, eval_registry):
    _private_root_in_tmp.mkdir(parents=True)
    p = _write_jsonl(_private_root_in_tmp / "set.jsonl", _rows("q a"))
    eval_registry.add(p)  # even a public registration cannot lift rule 3
    assert classify(p) == "private"


# --- rule 4: registered public path at its sha256 ---------------------------
def test_rule4_registered_public(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    eval_registry.add(p)
    assert classify(p) == "public"


def test_rule4_changed_bytes_are_private(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    eval_registry.add(p)
    p.write_text(p.read_text() + json.dumps({"question": "q b", "type": "refusal", "expected_sections": []}) + "\n")
    assert classify(p) == "private"


def test_rule4_same_bytes_other_path_is_private(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    eval_registry.add(p)
    other = tmp_path / "copy.jsonl"
    shutil.copy(p, other)
    assert classify(other) == "private"


def test_committed_public_sets_classify_public():
    for entry in eval_sets.load_registry():
        assert classify(entry.resolved()) == "public", entry.name


# --- rule 5: derived file whose recorded inputs are all public ---------------
def test_rule5_json_inputs_all_public(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    entry = eval_registry.add(p)
    art = tmp_path / "art.json"
    art.write_text(json.dumps({"inputs": [{"path": str(p), "sha256": entry.sha256}], "entries": []}))
    assert classify(art) == "public"


def test_rule5_mixed_public_and_unmatched_is_private(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    entry = eval_registry.add(p)
    art = tmp_path / "art.json"
    art.write_text(json.dumps({"inputs": [{"sha256": entry.sha256}, {"sha256": "0" * 64}]}))
    assert classify(art) == "private"


def test_rule5_stripped_header_is_private(tmp_path):
    art = tmp_path / "art.json"
    art.write_text(json.dumps({"entries": [{"id": "x"}]}))
    assert classify(art) == "private"


def test_rule5_markdown_report_set_lines(tmp_path, eval_registry):
    p = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    entry = eval_registry.add(p)
    rep = tmp_path / "report.md"
    rep.write_text(f"# r\n- tuning: x\n  - path: {p}\n  - sha256: {entry.sha256}\n")
    assert classify(rep) == "public"
    rep.write_text(rep.read_text() + f"  - sha256: {'1' * 64}\n")
    assert classify(rep) == "private"


# --- rule 6: legacy lookup only with the flag ---------------------------------
def test_rule6_legacy_only_with_flag(tmp_path, monkeypatch):
    cache = tmp_path / "old_cache.json"
    cache.write_text(json.dumps({"some question": {"rewrites": []}}))
    legacy = tmp_path / "legacy_public.json"
    legacy.write_text(json.dumps({"version": 1, "entries": [{"path": str(cache), "sha256": eval_sets.sha256_file(cache)}]}))
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", legacy)
    assert classify(cache) == "private"
    assert classify(cache, legacy_public=True) == "public"


def test_rule6_flag_never_lifts_sealed_or_private_root(tmp_path, monkeypatch, _private_root_in_tmp):
    _private_root_in_tmp.mkdir(parents=True)
    cache = _private_root_in_tmp / "c.json"
    cache.write_text("{}")
    legacy = tmp_path / "legacy_public.json"
    legacy.write_text(json.dumps({"version": 1, "entries": [{"path": str(cache), "sha256": eval_sets.sha256_file(cache)}]}))
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", legacy)
    assert classify(cache, legacy_public=True) == "private"


# --- rule 7: default ----------------------------------------------------------
def test_rule7_default_private(tmp_path):
    assert classify(_write_jsonl(tmp_path / "x.jsonl", _rows("q a"))) == "private"
    assert classify(tmp_path / "missing.jsonl") == "private"


# --- registry -----------------------------------------------------------------
def test_marked_file_cannot_register(tmp_path):
    p = _write_jsonl(tmp_path / "x.jsonl", _rows("q a", sealed_index=0))
    doc = {"version": 1, "sets": [{"name": "n", "path": str(p), "privacy": "public", "role": "fixture",
                                   "status": "active", "sha256": eval_sets.sha256_file(p)}]}
    with pytest.raises(RegistryError):
        registry_from_dict(doc)


def test_sealed_location_cannot_register(_private_root_in_tmp):
    d = _private_root_in_tmp / "sealed"
    d.mkdir(parents=True)
    p = _write_jsonl(d / "x.jsonl", _rows("q a"))
    doc = {"version": 1, "sets": [{"name": "n", "path": str(p), "privacy": "private", "role": "fixture",
                                   "status": "active", "sha256": eval_sets.sha256_file(p)}]}
    with pytest.raises(RegistryError):
        registry_from_dict(doc)


@pytest.mark.parametrize("bad", [
    {"privacy": "sealed"}, {"role": "headline"}, {"status": "live"}, {"sha256": "abc"}, {"extra": 1},
])
def test_registry_validation(tmp_path, bad):
    p = _write_jsonl(tmp_path / "x.jsonl", _rows("q a"))
    entry = {"name": "n", "path": str(p), "privacy": "public", "role": "fixture", "status": "active",
             "sha256": eval_sets.sha256_file(p)}
    entry.update(bad)
    with pytest.raises(RegistryError):
        registry_from_dict({"version": 1, "sets": [entry]})


def test_committed_registry_has_no_private_set():
    assert all(e.privacy == "public" for e in eval_sets.load_registry())


# --- floor ---------------------------------------------------------------------
def test_floor_is_strictest(tmp_path, eval_registry):
    pub = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    eval_registry.add(pub)
    priv = _write_jsonl(tmp_path / "priv.jsonl", _rows("q b"))
    sealed = _write_jsonl(tmp_path / "s.jsonl", _rows("q c", sealed_index=0))
    assert floor([pub]) == "public"
    assert floor([pub, priv]) == "private"
    assert floor([pub, priv, sealed]) == "sealed"
    assert floor([]) == "public"


def test_check_floor_weaker_raises_stronger_honoured():
    with pytest.raises(PrivacyFloorError):
        check_floor("public", "private")
    assert check_floor("private", "public") == "private"
    assert check_floor("private", "private") == "private"
    with pytest.raises(SealedInputError):
        check_floor("private", "sealed")


def test_require_class():
    assert require_class("public") == "public"
    with pytest.raises(SealedInputError):
        require_class("sealed")
    for bad in (None, "", "Public", 1):
        with pytest.raises(ValueError):
            require_class(bad)


def test_strictest():
    assert strictest(["public", "private"]) == "private"
    assert strictest([]) == "public"


# --- opaque ids ------------------------------------------------------------------
def test_ids():
    assert public_v1_id("abc").startswith("q:") and len(public_v1_id("abc")) == 14
    assert private_v1_id("abc", "0" * 64) != private_v1_id("abc", "1" * 64)
    assert private_v1_id("abc", "0" * 64) != public_v1_id("abc")


# --- review fixes ---------------------------------------------------------------
@pytest.mark.parametrize("name", ["copy.txt", "copy.ndjson", "copy", "copy.bak"])
def test_marker_survives_any_eval_suffix(tmp_path, name):
    rows = _rows("q a", "q b", sealed_index=1)
    p = _write_jsonl(tmp_path / name, rows)
    assert classify(p) == "sealed"


def test_duplicate_key_marker_bypass_is_sealed(tmp_path):
    p = tmp_path / "dup.jsonl"
    p.write_text('{"question": "q", "sealed": true, "sealed": false}\n')
    assert classify(p) == "sealed"
    j = tmp_path / "dup.json"
    j.write_text('{"sealed": true, "sealed": false}')
    assert classify(j) == "sealed"


def test_u2028_inside_a_string_is_not_a_line_break(tmp_path):
    p = tmp_path / "u.jsonl"
    p.write_text(json.dumps({"question": "alpha beta", "sealed": True}, ensure_ascii=False) + "\n")
    assert classify(p) == "sealed"


def test_case_variant_of_sealed_root_is_sealed(tmp_path, monkeypatch, _private_root_in_tmp):
    """On a case-insensitive FS a case variant names the same directory (samefile)."""
    sealed = _private_root_in_tmp / "sealed"
    sealed.mkdir(parents=True)
    variant = tmp_path / "eval" / "private" / "SEALED"
    if not variant.exists():
        # case-sensitive FS (Linux CI): emulate the alias with a symlink-free bind via samefile patch
        import os

        real_samefile = os.path.samefile
        monkeypatch.setattr(os.path, "samefile",
                            lambda a, b: real_samefile(str(a).replace("SEALED", "sealed"), b))
        variant.mkdir(parents=True)
        (variant / "x.jsonl").write_text('{"question": "q"}\n')
        from src.eval_privacy import is_under

        assert is_under(variant / "x.jsonl", sealed)
    else:
        p = variant / "x.jsonl"
        p.write_text('{"question": "q"}\n')
        assert classify(p) == "sealed"


@pytest.mark.parametrize("name", ["renamed.md", "renamed.MD", "renamed.py", "x.jsonl.md"])
def test_sealed_set_renamed_md_or_py_is_never_loaded(tmp_path, name):
    """Gate finding: .md/.py marker text is exempt from rule 2, so every loader refuses those suffixes."""
    from src.eval_schema import SchemaError, detect_schema, load_any
    from src.eval_sets import NotAnEvalInput
    from src.evaluator import load_golden_set

    p = _write_jsonl(tmp_path / name, _rows("q a", sealed_index=0))
    with pytest.raises(NotAnEvalInput):
        load_golden_set(str(p))
    with pytest.raises(SchemaError):
        detect_schema(p)
    with pytest.raises(SchemaError):
        load_any(p)


def test_question_set_with_self_declared_public_inputs_is_private(tmp_path, eval_registry):
    """Gate round 2: rule 5 never applies to a file holding question text."""
    pub = _write_jsonl(tmp_path / "pub.jsonl", _rows("q a"))
    entry = eval_registry.add(pub)
    fake = tmp_path / "set.json"
    fake.write_text(json.dumps({"inputs": [{"sha256": entry.sha256}], "question": "private q",
                                "type": "direct", "expected_sections": ["1.1"]}))
    assert classify(fake) == "private"
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps({"inputs": [{"sha256": entry.sha256}], "rows": [{"question": "private q"}]}))
    assert classify(nested) == "private"


def test_escaped_sealed_key_on_a_malformed_line_is_sealed(tmp_path):
    p = tmp_path / "esc.jsonl"
    p.write_text('{"question": "q a"}\n{"\\u0073ealed": true, "question": \n')
    assert classify(p) == "sealed"
