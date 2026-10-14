"""Phase 16A-2a a3, acceptance (e) printer parts and (j): ``sealed_tool`` printer, ids, digest, inputs, prereg, power, return.

Synthetic data only. Every command's output is checked against the allowlist (integers, sha256s, enum words);
the families, questions and labels used here are invented and carry canaries where text could leak.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from src import eval_stats
from src.eval_privacy import public_v1_id
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    CANARY, Spec, fid_of, make_row, repos, standard_released, world, write_rows,
)

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^[a-z][a-z0-9_]* ?( [a-z][a-z0-9_]*=([0-9]+|[0-9a-f]{64}|[a-z0-9_]+|(<[a-z]+>|[a-z_]+)(\[[0-9]+\]|\.[a-z_]+)*))*$")


def assert_allowlisted(text: str, *, ids_ok: bool = False) -> None:
    """An independent check of printer output: every value is an int, a sha256 or a known enum word."""
    for line in text.splitlines():
        assert LINE.match(line), f"not an allowlisted line: {line!r}"
        for token in line.split(" ")[1:]:
            key, _, value = token.partition("=")
            if key in ("error",) or value.isdigit() or re.fullmatch(r"[0-9a-f]{64}", value):
                continue
            ok = value in st.ENUM_WORDS or (key == "field" and st._FIELD_PATH.fullmatch(value)) or (ids_ok and key == "family")
            assert ok, f"value not allowlisted: {token!r}"


# --- the printer --------------------------------------------------------------------------------------------
def test_printer_prints_ints_sha256_and_enum_words_only():
    out, err = io.StringIO(), io.StringIO()
    p = st.Printer(out, err)
    p.emit("digest", rows=3, sha256="a" * 64, kind="sealed", scope_answer=0)
    p.error("usage", errors=2)
    assert out.getvalue() == f"digest rows=3 sha256={'a' * 64} kind=sealed scope_answer=0\n"
    assert err.getvalue() == "error reason=usage errors=2\n"


@pytest.mark.parametrize(
    "value",
    [CANARY, "some free text", "f0000abcd", "A" * 64, "a" * 63, "a" * 65, "q:abcdef123456", "sealed ", "SEALED", "", " ", "answer\n", "answer answer",
     -1, 10**9 + 1, 1.5, True, False, None, b"answer", ["answer"], {"a": 1}, float("nan")],
)
def test_printer_refuses_everything_else(value):
    p = st.Printer(io.StringIO(), io.StringIO())
    with pytest.raises(st.PrintRefused):
        p.emit("digest", rows=value)
    with pytest.raises(st.PrintRefused):
        p.error("usage", errors=value)
    assert p.out.getvalue() == "" and p.err.getvalue() == ""  # nothing is half-written


@pytest.mark.parametrize("key", ["Rows", "rows ", "rows=1", "", "1rows", "ro\nws", CANARY.upper(), "a" * 41])
def test_printer_refuses_unsafe_keys_kinds_and_reasons(key):
    p = st.Printer(io.StringIO(), io.StringIO())
    with pytest.raises(st.PrintRefused):
        p.emit("digest", **{key: 1})
    with pytest.raises(st.PrintRefused):
        p.emit(key)
    with pytest.raises(st.PrintRefused):
        p.error(key)


def test_an_opaque_family_id_prints_only_in_the_two_named_places():
    p = st.Printer(io.StringIO(), io.StringIO())
    p.emit("audit_hit", target="transcript", family=fid_of(0x123))
    with pytest.raises(st.PrintRefused):
        p.emit("digest", family=fid_of(0x123))
    with pytest.raises(st.PrintRefused):
        p.emit("audit_hit", target="transcript", family="f12345678 extra")
    with pytest.raises(st.PrintRefused):
        p.emit("audit_hit", target="transcript", other=fid_of(0x123))


def test_a_field_path_prints_but_a_value_posing_as_one_does_not():
    p = st.Printer(io.StringIO(), io.StringIO())
    for ok in ("evidence[0][1]", "gaps[0].keywords[2]", "<row>", "scope", "inventory.sections"):
        p.emit("schema_error", line=1, field=ok)
    for bad in ("Evidence[0]", "evidence[x]", "evidence 0", CANARY, "a" * 70):
        with pytest.raises(st.PrintRefused):
            p.emit("schema_error", line=1, field=bad)
    with pytest.raises(st.PrintRefused):
        p.emit("digest", rows="evidence[0]")  # only the `field` key takes a path


def test_the_enum_words_are_all_plain_lowercase_words():
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", w) for w in st.ENUM_WORDS)
    assert not any(st._SHA256.fullmatch(w) or st._FAMILY_ID.fullmatch(w) for w in st.ENUM_WORDS)


# --- the CLI never echoes input ------------------------------------------------------------------------------------
def test_usage_errors_exceptions_and_unknown_commands_show_fixed_text_only(world, monkeypatch):
    for argv in (["split", "--round", CANARY], ["nosuchcommand", CANARY], ["ids", "--count"], ["digest"], ["freeze", "--split", CANARY]):
        code, out, err = world.run(*argv)
        assert code == st.EXIT_USAGE and err == "error reason=usage\n" and out == "", argv
    monkeypatch.setattr(st, "cmd_ids", lambda ctx, count, out: (_ for _ in ()).throw(RuntimeError(f"boom {CANARY}")))
    code, out, err = world.run("ids", "--count", "1")
    assert code == st.EXIT_FAIL and err == "error reason=internal\n" and out == ""
    code, out, err = world.run("digest", world.tmp / f"{CANARY}-missing.jsonl")
    assert code == st.EXIT_FAIL and CANARY not in out + err  # a path is never echoed either


def test_a_corrupt_ledger_is_a_distinct_exit_with_a_fixed_reason(world):
    std = standard_released(world)
    ledger = Path(world.ledger()._ledger)
    ledger.write_bytes(b"{not json}\n" + ledger.read_bytes())
    code, out, err = world.run("exclude", "--family", std.sealed[0], "--reason", "s_hit")
    assert code == st.EXIT_LEDGER_STOP and err == "error reason=ledger_corrupt\n"


def test_the_script_runs_as_a_program_and_prints_only_allowlisted_text(tmp_path):
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "sealed_tool.py"), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert ok.returncode == 0 and "Sealed tool" in ok.stdout
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "sealed_tool.py"), "split", "--round", CANARY], capture_output=True, text=True, cwd=tmp_path)
    assert bad.returncode == st.EXIT_USAGE and bad.stderr == "error reason=usage\n" and bad.stdout == ""


# --- ids ---------------------------------------------------------------------------------------------------------------
def test_ids_issues_fresh_unique_opaque_ids_and_prints_only_a_count_and_a_hash(world, monkeypatch):
    existing = fid_of(0xABC)
    write_rows(world.dev / "tuning_pinned.jsonl", [make_row(existing, 0, sealed=False, source="legacy")])
    queue = iter(["f00000abc", "f00000def", "f00000def", "f00000123"])  # the first is taken, the second repeats
    monkeypatch.setattr(st, "new_family_id", lambda: next(queue))
    out_path = world.tmp / "ids" / "ids.txt"
    code, out, err = world.run("ids", "--count", "2", "--out", out_path)
    assert code == 0, err
    ids = out_path.read_text().split()
    assert ids == ["f00000def", "f00000123"] and existing not in ids
    line = world.last(out, "ids")
    assert line["count"] == "2" and line["sha256"] == __import__("hashlib").sha256(out_path.read_bytes()).hexdigest()
    assert_allowlisted(out) and ids[0] not in out
    # the issued ids are remembered: the next batch never repeats them
    queue2 = iter(["f00000def", "f00000123", "f00000777"])
    monkeypatch.setattr(st, "new_family_id", lambda: next(queue2))
    code, out, err = world.run("ids", "--count", "1", "--out", world.tmp / "ids2.txt")
    assert (world.tmp / "ids2.txt").read_text().split() == ["f00000777"]
    assert (world.sealed / "work" / "issued_ids.txt").read_text().split() == ["f00000def", "f00000123", "f00000777"]
    for bad in ("0", "-1", "100001"):
        assert world.run("ids", "--count", bad)[0] == st.EXIT_USAGE


def test_ids_defaults_to_the_sealed_work_directory(world):
    code, out, err = world.run("ids", "--count", "5")
    assert code == 0, err
    ids = (world.sealed / "work" / "ids.txt").read_text().split()
    assert len(set(ids)) == 5 and all(re.fullmatch(r"f[0-9a-f]{8}", i) for i in ids)


# --- digest -------------------------------------------------------------------------------------------------------------
def test_digest_counts_by_scope_register_and_source_and_never_by_chapter_for_sealed(world):
    std = standard_released(world)
    path = world.sealed / "sealed_r1.jsonl"
    code, out, err = world.run("digest", path)
    assert code == 0, err
    line = world.last(out, "digest")
    assert line["kind"] == "sealed" and line["rows"] == "24" and line["families"] == "12"
    assert (line["scope_answer"], line["scope_partial"], line["scope_refuse"]) == ("9", "0", "3")
    assert (line["reg_modelled"], line["reg_lay"], line["reg_terse"], line["reg_none"]) == ("7", "3", "2", "0")
    assert (line["src_tutorial"], line["src_handbook"], line["src_legacy"]) == ("6", "6", "0")
    assert line["sha256"] == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    assert not [k for k in line if k.startswith("ch_")]  # no chapter for sealed
    assert_allowlisted(out) and err == ""


def test_digest_of_a_development_set_adds_chapters_and_prints_no_label_text(world):
    write_rows(world.dev / "tuning_pinned.jsonl", [
        *Spec(fid=fid_of(0x200), section="3.1").rows(sealed=False), *Spec(fid=fid_of(0x201), section="3.2").rows(sealed=False),
        *Spec(fid=fid_of(0x202), section="CANARYSEC.1").rows(sealed=False), *Spec(fid=fid_of(0x203), scope="refuse").rows(sealed=False),
    ])
    code, out, err = world.run("digest", world.dev / "tuning_pinned.jsonl")
    line = world.last(out, "digest")
    assert code == 0 and line["kind"] == "development" and line["ch_3"] == "2" and line["ch_other"] == "2"  # a non-numeric chapter (and "refuse") is not printed
    assert "CANARY" not in (out + err).upper()


def test_digest_reports_a_schema_error_by_line_and_field_only(world):
    bad = make_row(fid_of(0x300), 0)
    bad["evidence"] = [[f"{CANARY}.1"]]
    bad["question"] = f"{CANARY} leaked?"
    path = write_rows(world.sealed / "sealed_bad.jsonl", [bad])
    code, out, err = world.run("digest", path)
    assert code == st.EXIT_USAGE and "reason=schema_invalid errors=1" in err
    assert "schema_error line=1 field=evidence[0][0]" in out and CANARY not in (out + err)


# --- count-migrated -------------------------------------------------------------------------------------------------------
def test_count_migrated_counts_families_by_split_and_each_splits_refusal_deficit(world, eval_registry, tmp_path):
    def v1(name, rows):
        path = tmp_path / "public" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps({"question": q, "type": t, "expected_sections": [] if t == "refusal" else ["1.1"]}) + "\n" for q, t in rows), encoding="utf-8")
        eval_registry.add(path, privacy="public", name=name)
        return path

    gold = v1("golden.jsonl", [(f"{CANARY} g{i}", "refusal" if i < 2 else "direct") for i in range(8)])
    real = v1("real.jsonl", [(f"{CANARY} r{i}", "refusal" if i == 0 else "direct") for i in range(5)])
    pid = lambda tag, i: public_v1_id(f"{CANARY} {tag}{i}")  # noqa: E731
    pairing = tmp_path / "pairing.json"
    pairing.write_text(json.dumps({"pairs": [[pid("g", 2), pid("r", 1)], [pid("g", 1), pid("r", 2)]]}), encoding="utf-8")
    tutorial = tmp_path / "tutorial.jsonl"
    tutorial.write_text("".join(json.dumps(r) + "\n" for r in (
        {"id": "T01-Aa", "type": "direct"}, {"id": "T01-Ab", "type": "direct"}, {"id": "T01-Ba", "type": "refusal"}, {"id": "T01-Bb", "type": "refusal"})), encoding="utf-8")
    code, out, err = world.run("count-migrated", "--golden", gold, "--realistic", real, "--pairing", pairing, "--tutorial", tutorial,
                               "--quota-tuning", "16", "--quota-realistic", "8")
    assert code == 0, err
    line = world.last(out, "count_migrated")
    assert (line["pairs"], line["tutorial_exercises"]) == ("2", "2")
    assert line["pair_scope_conflicts"] == "1"  # g1 is a refusal, r2 is not: the pair counts as a refusal and is flagged
    assert (line["tuning_families"], line["tuning_refuse"]) == ("6", "1")  # golden-only families go to tuning; g0 is the refusal
    assert (line["realistic_families"], line["realistic_refuse"]) == ("7", "3")  # 5 realistic rows (a pair is ONE family) + 2 exercises; r0, r2, T01-B
    assert (line["tuning_quota"], line["tuning_deficit"]) == ("16", "3")  # 25 % of 16 is 4 refusal families, 1 present
    assert (line["realistic_quota"], line["realistic_deficit"]) == ("8", "0")
    assert_allowlisted(out)
    assert CANARY not in out + err


def test_count_migrated_quotas_default_to_the_prereg_and_a_bad_pairing_is_refused(world, eval_registry, tmp_path):
    path = tmp_path / "public" / "g.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"question": "gold one", "type": "direct", "expected_sections": ["1.1"]}) + "\n", encoding="utf-8")
    eval_registry.add(path, privacy="public", name="g")
    tutorial = tmp_path / "t.jsonl"
    tutorial.write_text(json.dumps({"id": "T01-Aa", "type": "direct"}) + "\n", encoding="utf-8")
    pairing = tmp_path / "p.json"
    pairing.write_text(json.dumps({"pairs": [["q:000000000000", "q:111111111111"]]}), encoding="utf-8")
    world.write_prereg()
    argv = ("count-migrated", "--golden", path, "--realistic", path, "--tutorial", tutorial)
    code, out, err = world.run(*argv, "--pairing", pairing)
    assert code == st.EXIT_USAGE and "reason=inputs_invalid" in err  # the pair names ids that are not in the sets
    pairing.write_text(json.dumps({"pairs": []}), encoding="utf-8")
    code, out, err = world.run(*argv, "--pairing", pairing)
    line = world.last(out, "count_migrated")
    assert code == 0 and (line["tuning_quota"], line["realistic_quota"]) == ("8", "4")  # tuning_min and realistic_min from the prereg


# --- inputs-check (wired to src/sealed_inputs.py) -----------------------------------------------------------------------------
def _inputs(tmp_path, pairs, labels, golden=("q:g1", "q:g2", "q:g3"), realistic=("q:r1", "q:r2", "q:r3")):
    (tmp_path / "pairing.json").write_text(json.dumps({"pairs": pairs}), encoding="utf-8")
    (tmp_path / "conflicts.json").write_text(json.dumps({"labels": labels}), encoding="utf-8")
    (tmp_path / "gold_ids.txt").write_text("\n".join(golden), encoding="utf-8")
    (tmp_path / "real_ids.txt").write_text("\n".join(realistic), encoding="utf-8")
    return ("inputs-check", "--pairing", tmp_path / "pairing.json", "--conflicts", tmp_path / "conflicts.json",
            "--golden-ids", tmp_path / "gold_ids.txt", "--realistic-ids", tmp_path / "real_ids.txt")


def test_inputs_check_passes_a_valid_pairing_and_inventoried_conflict_labels(world, tmp_path):
    code, out, err = world.run(*_inputs(tmp_path, [["q:g1", "q:r1"], ["q:g2", "q:r2"]], ["1.1", "2.3"]))
    assert code == 0, err
    assert out == "inputs_check status=ok pairs=2 labels=2\n" and err == ""


def test_inputs_check_refuses_a_reused_id_a_non_public_id_and_an_uninventoried_label(world, tmp_path):
    code, out, err = world.run(*_inputs(tmp_path, [["q:g1", "q:r1"], ["q:g1", "q:r2"]], ["1.1"]))
    assert code == st.EXIT_USAGE and "reason=inputs_invalid errors=1" in err
    assert "inputs_error source=pairing field=pairs[1][0] why=reused_id" in out
    code, out, err = world.run(*_inputs(tmp_path, [["q:g1", "q:r1"], [f"q:{CANARY}", "q:r2"]], ["1.1"]))
    assert code == st.EXIT_USAGE and "inputs_error source=pairing field=pairs[1][0] why=not_public_id" in out and CANARY not in out + err
    code, out, err = world.run(*_inputs(tmp_path, [["q:g1", "q:r1"]], ["1.1", f"{CANARY}.9"]))
    assert code == st.EXIT_USAGE and "inputs_error source=conflicts field=labels[1] why=uninventoried_label" in out
    assert CANARY not in out + err
    code, out, err = world.run(*_inputs(tmp_path, [["q:g1", "q:r1"], ["q:g1", "q:r1"]], ["99.99"]))  # every problem, by field
    assert out.count("inputs_error") == 3 and "why=reused_id" in out and "why=uninventoried_label" in out
    assert_allowlisted(out)


def test_inputs_check_takes_its_public_ids_from_the_registered_golden_and_realistic_sets(world, eval_registry, tmp_path):
    for name, questions in (("golden-v1", ["gold one", "gold two"]), ("realistic-v1", ["real one", "real two"])):
        path = tmp_path / "public" / f"{name}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps({"question": q, "type": "direct", "expected_sections": ["1.1"]}) + "\n" for q in questions), encoding="utf-8")
        eval_registry.add(path, privacy="public", name=name)
    pairing, conflicts = tmp_path / "pairing.json", tmp_path / "conflicts.json"
    conflicts.write_text(json.dumps({"labels": ["1.1"]}), encoding="utf-8")
    pairing.write_text(json.dumps({"pairs": [[public_v1_id("gold one"), public_v1_id("real one")]]}), encoding="utf-8")
    code, out, err = world.run("inputs-check", "--pairing", pairing, "--conflicts", conflicts)
    assert code == 0 and out == "inputs_check status=ok pairs=1 labels=1\n", err
    pairing.write_text(json.dumps({"pairs": [[public_v1_id("real two"), public_v1_id("gold two")]]}), encoding="utf-8")  # columns swapped
    code, out, err = world.run("inputs-check", "--pairing", pairing, "--conflicts", conflicts)
    assert code == st.EXIT_USAGE and out.count("why=not_public_id") == 2


def test_inputs_check_reports_a_missing_file_without_naming_it(world, tmp_path):
    argv = list(_inputs(tmp_path, [], []))
    argv[2] = tmp_path / f"{CANARY}-gone.json"
    code, out, err = world.run(*argv)
    assert code == st.EXIT_USAGE and CANARY not in out + err


# --- return --------------------------------------------------------------------------------------------------------------------
def test_return_prints_a_return_file_only_if_every_key_and_value_is_an_id_hash_count_or_enum(world, tmp_path):
    good = {"status": "ok", "kind": "draft", "families": 60, "rows": 120, "by_scope": {"answer": 40, "partial": 5, "refuse": 15},
            "by_register": {"lay": 30, "modelled": 20, "terse": 10}, "by_source": {"tutorial": 30, "handbook": 30}, "sha256": "ab" * 32}
    path = tmp_path / "return.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    code, out, err = world.run("return", path)
    assert code == 0 and err == "" and json.loads(out) == good and out.count("\n") == 1
    ids = {"status": "ok", "kind": "twins", "pairs_checked": 100, "twin_pairs": 2, "pool_families": 50, "sha256": "0" * 64, "first": "deadbeef"}
    path.write_text(json.dumps(ids), encoding="utf-8")
    assert world.run("return", path)[0] == 1  # "first" is not an enum word, and a bare id-shaped VALUE is fine only as a value
    path.write_text(json.dumps({**ids, "first": "deadbeef"}), encoding="utf-8")
    code, out, err = world.run("return", path, "--enum", "first")
    assert code == 0 and json.loads(out)["first"] == "deadbeef"
    for bad in (
        {**good, "status": f"ok {CANARY}"}, {**good, CANARY: 1}, {**good, "families": 1.5}, {**good, "families": -1}, {**good, "families": "60"},
        {**good, "families": True}, {**good, "reason": None}, {**good, "by_scope": {"answer": [{"deep": {"deeper": {"deepest": {"x": 1}}}}]}},
        {"status": "blocked", "reason": f"the question about {CANARY} was unclear"}, [1, 2], "ok", 3,
    ):
        path.write_text(bad if isinstance(bad, str) else json.dumps(bad), encoding="utf-8")
        code, out, err = world.run("return", path)
        assert code == st.EXIT_FAIL and out == "invalid\n" and err == "" and CANARY not in out, bad
    path.write_text('{"status": "ok", "status": "ok"}', encoding="utf-8")
    assert world.run("return", path)[1] == "invalid\n"  # duplicate keys
    path.write_text("not json " + CANARY, encoding="utf-8")
    assert world.run("return", path)[1] == "invalid\n"
    assert world.run("return", tmp_path / "absent.json")[1] == "invalid\n"
    path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    assert world.run("return", path, "--enum", "bad word!")[1] == "invalid\n"  # an unsafe --enum is not an escape hatch


# --- prereg-check ----------------------------------------------------------------------------------------------------------------------
def test_prereg_check_validates_the_prereg_and_recomputes_quotas_and_the_power_table(world):
    world.write_prereg()
    code, out, err = world.run("prereg-check")
    assert code == 0, err
    line = world.last(out, "prereg_check")
    assert (line["status"], line["rounds"], line["quota_examples"], line["power_cells"], line["committed"]) == ("ok", "2", "1", "4", "0")
    assert line["family_cells"] == "2"  # one per alpha (alphas = 0.05, 0.0125) at the single discordance


@pytest.mark.parametrize(
    "edit,field,why",
    [
        (lambda d: d["quota"]["examples"][0].update(tuning=d["quota"]["examples"][0]["tuning"] + 1), "quota.examples[0]", "mismatch"),
        (lambda d: d["power"]["table"][0].update(power_pm=d["power"]["table"][0]["power_pm"] + 1), "power.table", "mismatch"),
        (lambda d: d["power"]["table"][1].update(mde_hundredths=None), "power.table", "mismatch"),
        (lambda d: d["quota"].update(spare=d["quota"]["spare"] + 1), "quota.sealed", "mismatch"),
        (lambda d: d["power"].update(planned_groups=[1] * 20 + [3] * 5), "power.table", "mismatch"),
        (lambda d: d.update(extra_key=1), "<unknown>", "unknown_key"),
        (lambda d: d.pop("mwe"), "mwe", "missing"),
        (lambda d: d.update(alpha=1.5), "alpha", "out_of_range"),
        (lambda d: d.update(rounds=[3]), "rounds", "wrong_type"),
        (lambda d: d.update(band={"min_pct": 20, "max_pct": 30}), "band", "mismatch"),
        (lambda d: d["seeds"].pop("repair"), "seeds.repair", "wrong_type"),
    ],
)
def test_prereg_check_names_the_field_that_fails_and_nothing_else(world, edit, field, why):
    doc = world.prereg_doc()
    edit(doc)
    world.prereg_path.parent.mkdir(parents=True, exist_ok=True)
    world.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = world.run("prereg-check")
    assert code == st.EXIT_FAIL and "reason=prereg_invalid" in err
    assert f"prereg_error field={field} why={why}" in out, out
    assert_allowlisted(out)


def test_prereg_check_on_the_committed_prereg(world):
    world.write_prereg()
    code, out, err = world.run("prereg-check", "--committed")
    assert code == st.EXIT_REFUSED and "reason=prereg_uncommitted" in err
    world.commit_prereg(push=False)
    code, out, err = world.run("prereg-check", "--committed")
    assert code == 0 and world.last(out, "prereg_check")["committed"] == "1"
    world.prereg_path.write_text(world.prereg_path.read_text().replace('"split_r1": 11', '"split_r1": 12'), encoding="utf-8")
    code, out, err = world.run("prereg-check", "--committed")
    assert code == st.EXIT_REFUSED and "reason=prereg_modified" in err
    assert world.run("prereg-check")[0] == 0  # without --committed the working file is checked as it is


# --- power ---------------------------------------------------------------------------------------------------------------------------------
def _power_lines(out):
    rows = [{"_kind": "power", **dict(p.split("=", 1) for p in ln.split(" ")[1:])} for ln in out.splitlines() if ln.startswith("power ")]
    return {r["model"]: r for r in rows}


def test_power_with_singleton_groups_matches_the_independent_family_figures(world):
    args = ("power", "--group-sizes", ",".join(["1"] * 40), "--discordance", "0.3", "--effect", "0.1", "--alpha", "0.05", "--draws", "2000", "--seed", "5")
    code, out, err = world.run(*args)
    assert code == 0, err
    lines = _power_lines(out)
    exact = eval_stats.power(40, 0.3, 0.1, 0.05, "directional")
    assert abs(int(lines["independent"]["power_pm"]) / 1000 - exact) < 0.05  # Monte Carlo of the sign-flip test ~ exact McNemar
    assert lines["independent"]["power_pm"] == lines["correlated"]["power_pm"]  # singletons: the two models coincide
    assert lines["independent"]["mde_hundredths"] == lines["correlated"]["mde_hundredths"]
    assert abs(int(lines["independent"]["mde_hundredths"]) - round(100 * eval_stats.mde(40, 0.3, 0.05))) <= 4
    ref = world.last(out, "power_reference")
    assert ref["power_pm"] == str(round(1000 * exact)) and ref["families"] == "40"
    assert lines["independent"]["families"] == "40" and lines["independent"]["groups"] == "40" and lines["independent"]["draws"] == "2000"
    assert_allowlisted(out)
    assert world.run(*args)[1] == out  # seed-deterministic


def test_power_prints_both_correlation_models_for_grouped_designs_and_needs_its_inputs(world):
    base = ("--discordance", "0.4", "--effect", "0.1", "--alpha", "0.05", "--draws", "200", "--seed", "1")
    code, out, err = world.run("power", "--group-sizes", "4,4,4,4,1,1,1,1,1,1", *base)
    lines = _power_lines(out)
    assert code == 0 and set(lines) == {"independent", "correlated"} and lines["independent"]["groups"] == "10" and lines["independent"]["families"] == "22"
    assert lines["independent"]["power_pm"] != lines["correlated"]["power_pm"]  # the correlation model moves the figure
    for bad in (["power"], ["power", "--group-sizes", "0,1", *base], ["power", "--group-sizes", "a,b", *base],
                ["power", "--group-sizes", "1,1", "--from-freeze", *base]):
        assert world.run(*bad)[0] == st.EXIT_USAGE, bad
    world.write_prereg()  # draws, seed, alpha, effect and discordance default from the prereg
    code, out, err = world.run("power", "--group-sizes", "1,1,1,1,1,1")
    assert code == 0 and out.count("power ") == 4 and world.last(out, "power")["draws"] == "60"  # 2 models x the prereg's 2 alphas
