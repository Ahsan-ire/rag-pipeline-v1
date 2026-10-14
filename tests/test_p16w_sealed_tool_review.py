"""Phase 16A-2a a3, acceptance (e) and (j): sheets, approvals, escalation, calibration and disagreement.

Synthetic, schema-valid fixtures only. Sealed sheets and forms live under ``eval/private/sealed/work/``,
development ones under ``eval/private/work/``; nothing here prints a question, label or keyword.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from scripts import sealed_tool as st
from src import eval_schema, eval_stats
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    CANARY, Spec, fid_of, read_rows, repos, standard_released, world, write_rows,
)


def read_csv(path: Path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approvals(w, kind: str = "sealed"):
    base = w.sealed / "work" if kind == "sealed" else w.root / "work"
    path = base / "approvals.json"
    return json.loads(path.read_text()) if path.is_file() else None


def sealed_component(w, specs, name: str = "sealed_r1.jsonl") -> Path:
    """Hand-written sealed component (no split): the review commands only need the component files."""
    return write_rows(w.sealed / name, [r for s in specs for r in s.rows(sealed=True)])


# --- sheet -------------------------------------------------------------------------------------------
def test_sheet_has_a_line_per_phrasing_with_row_hash_pages_and_blank_owner_columns(world, tmp_path):
    std = standard_released(world)
    dump = tmp_path / "dump.jsonl"
    dump.write_text("".join(json.dumps({"label": s.section, "page_start": 10, "page_end": 12 if i % 2 else 10, "text": "x"}) + "\n"
                            for i, s in enumerate(world.specs.values())), encoding="utf-8")
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", "s1", handbook_dump=dump)
    assert code == 0, err
    path = world.sheet_path("sealed", "s1")
    header, lines = read_csv(path)
    assert header == list(st.SHEET_COLUMNS) and len(lines) == 24
    line = world.last(out, "sheet")
    assert (line["rows"], line["families"], line["sha256"]) == ("24", "12", sha(path))
    by_id = {r["row_id"]: r for r in lines}
    from src.eval_sealed_read import load_sealed
    for row in load_sealed(world.sealed / "sealed_r1.jsonl", inventory=eval_schema.load_inventory()):
        entry = by_id[row["id"]]
        assert entry["row_hash"] == eval_schema.row_hash(row) and entry["phrasing"] == row["question"]
        assert entry["scope"] == row["scope"] and all(entry[c] == "" for c in st.OWNER_MARKS)
    assert any("[p10-12]" in r["evidence"] for r in lines) and any("[p10]" in r["evidence"] for r in lines)
    assert CANARY not in out


def test_development_sheet_lists_rows_that_need_validation(world):
    std = standard_released(world)
    code, out, err = world.run("sheet", "--kind", "development", "--name", "d1")
    assert code == 0, err
    header, lines = read_csv(world.sheet_path("development", "d1"))
    # 3 legacy paraphrases + 2 tutorial-sourced rows + 8 new drafts x 2 phrasings; plain legacy first phrasings are exempt
    assert len(lines) == 21 and (world.root / "work" / "sheets" / "d1.csv").is_file()
    assert not (world.sealed / "work" / "sheets" / "d1.csv").exists()
    assert not [r for r in lines if r["source"] == "legacy" and r["phrasing_no"] == "0"]  # plain legacy first phrasings are exempt


# --- apply -------------------------------------------------------------------------------------------
def test_apply_binds_each_approved_row_to_its_hash_and_the_sheet_sha256(world):
    std = standard_released(world)
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {}, default="A")
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
    assert code == 0, err
    line = world.last(out, "apply")
    assert (line["approved"], line["changed"], line["pending"], line["sha256"]) == ("24", "0", "0", sha(path))
    doc = approvals(world)
    assert len(doc["approved"]) == 24 and doc["pending_confirm"] == {} and doc["rejected"] == []
    from src.eval_sealed_read import load_sealed
    for row in load_sealed(world.sealed / "sealed_r1.jsonl", inventory=eval_schema.load_inventory()):
        assert doc["approved"][row["id"]] == {"row_hash": eval_schema.row_hash(row), "sheet_sha256": sha(path)}
        assert row["owner_status"] == "approved"  # the row hash ignores it, so the approval still binds
    assert (world.sealed / "sealed_r1.jsonl").read_text().count('"sealed": true') == 24  # still marked
    code, out, err = world.run("sheet", "--kind", "sealed", "--name", "s2")
    assert world.last(out, "sheet")["rows"] == "0"  # nothing left to review


def test_apply_refuses_a_stale_sheet_and_a_double_mark_and_writes_nothing(world):
    std = standard_released(world)
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    path = world.sheet_path("sealed", "s1")
    target = world.sealed / "sealed_r1.jsonl"
    before = target.read_bytes()
    marked = world.mark_sheet(path, {}, default="A")
    rows = read_rows(target)
    rows[0]["question"] = "an edited question that changes the row hash"
    write_rows(target, rows)
    edited = target.read_bytes()
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", marked)
    assert code == st.EXIT_REFUSED and "reason=sheet_stale lines=1" in err
    assert target.read_bytes() == edited and approvals(world) is None  # nothing written
    target.write_bytes(before)
    world.run("sheet", "--kind", "sealed", "--name", "s2")
    world.mark_sheet(world.sheet_path("sealed", "s2"), {}, default="A", extra={rows[1]["id"]: {"R": "x"}})
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", world.sheet_path("sealed", "s2"))
    assert code == st.EXIT_REFUSED and "reason=sheet_conflict lines=1" in err
    assert target.read_bytes() == before and approvals(world) is None


def test_a_changed_row_binds_only_after_the_owner_ticks_a_confirm_sheet(world):
    std = standard_released(world)
    fam = std.sealed_singletons[0]
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    corrections = {r: {"corrections": json.dumps({"question": f"{CANARY} the owner reworded this phrasing {n}"})}
                   for n, r in enumerate((f"{fam}-0", f"{fam}-1"))}
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {fam: "C"}, default="A", extra=corrections)
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
    assert code == 0, err
    assert world.last(out, "apply")["changed"] == "2" and CANARY not in out + err
    doc = approvals(world)
    assert f"{fam}-0" not in doc["approved"] and set(doc["pending_confirm"]) == {f"{fam}-0", f"{fam}-1"}  # not bound yet
    from src.eval_sealed_read import load_sealed
    rows = {r["id"]: r for r in load_sealed(world.sealed / "sealed_r1.jsonl", inventory=eval_schema.load_inventory())}
    assert rows[f"{fam}-0"]["owner_status"] == "changed" and "reworded" in rows[f"{fam}-0"]["question"]
    for rid, pending in doc["pending_confirm"].items():
        assert pending["row_hash"] == eval_schema.row_hash(rows[rid])  # the FINAL row hash
    confirm = world.sealed / "work" / "sheets" / "s1.confirm.csv"
    header, lines = read_csv(confirm)
    assert header == list(st.CONFIRM_COLUMNS) and len(lines) == 2 and all(r["OK"] == "" for r in lines)
    # unticked: still pending
    code, out, err = world.run("apply", "--kind", "sealed", "--confirm", confirm)
    assert code == 0 and world.last(out, "apply")["confirmed"] == "0"
    assert set(approvals(world)["pending_confirm"]) == {f"{fam}-0", f"{fam}-1"}
    # ticked: bound to the final hash and the confirm sheet's sha256
    world.mark_sheet(confirm, {}, default="OK")
    code, out, err = world.run("apply", "--kind", "sealed", "--confirm", confirm)
    assert code == 0, err
    doc = approvals(world)
    assert world.last(out, "apply")["confirmed"] == "2" and doc["pending_confirm"] == {}
    assert doc["approved"][f"{fam}-0"] == {"row_hash": eval_schema.row_hash(rows[f"{fam}-0"]), "sheet_sha256": sha(confirm)}


def test_a_confirm_sheet_for_a_row_that_changed_again_is_refused(world):
    std = standard_released(world)
    fam = std.sealed_singletons[1]
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    patch = {r: {"corrections": json.dumps({"ambiguous": True})} for r in (f"{fam}-0", f"{fam}-1")}
    world.mark_sheet(world.sheet_path("sealed", "s1"), {fam: "C"}, default="A", extra=patch)
    assert world.run("apply", "--kind", "sealed", "--sheet", world.sheet_path("sealed", "s1"))[0] == 0
    confirm = world.mark_sheet(world.sealed / "work" / "sheets" / "s1.confirm.csv", {}, default="OK")
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    next(r for r in rows if r["id"] == f"{fam}-0")["question"] = "changed again after the confirm sheet was written"
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    code, out, err = world.run("apply", "--kind", "sealed", "--confirm", confirm)
    assert code == st.EXIT_REFUSED and "reason=sheet_stale" in err
    assert f"{fam}-0" not in approvals(world)["approved"]


def test_an_invalid_correction_is_rejected_by_the_one_validator_and_nothing_is_written(world):
    std = standard_released(world)
    fam = std.sealed_singletons[2]
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    bad = json.dumps({"evidence": [["99.99"]]})  # not in the inventory
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {fam: "C"}, default="A", extra={f"{fam}-0": {"corrections": bad}, f"{fam}-1": {"corrections": bad}})
    before = (world.sealed / "sealed_r1.jsonl").read_bytes()
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
    assert code == st.EXIT_USAGE and "reason=schema_invalid" in err and "schema_error line=" in out
    assert "99.99" not in out + err  # errors name line and field, never a value
    assert (world.sealed / "sealed_r1.jsonl").read_bytes() == before and approvals(world) is None
    world.mark_sheet(world.sheet_path("sealed", "s1"), {}, default=None)
    for cell in ("not json", json.dumps({"id": "f00000000-9"}), json.dumps({})):
        path = world.mark_sheet(world.sheet_path("sealed", "s1"), {fam: "C"}, default="A", extra={f"{fam}-0": {"corrections": cell}, f"{fam}-1": {"corrections": cell}})
        code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
        assert code == st.EXIT_REFUSED and "reason=sheet_conflict" in err


def test_reject_quarantines_the_family_and_escalate_marks_it_pending(world):
    std = standard_released(world)
    reject, escalate = std.sealed_singletons[0], std.sealed_singletons[1]
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {reject: "R", escalate: "E"}, default="A")
    code, out, err = world.run("apply", "--kind", "sealed", "--sheet", path)
    assert code == 0, err
    line = world.last(out, "apply")
    assert (line["rejected_families"], line["escalated_families"], line["approved"]) == ("1", "1", "20")
    survivors = {r["family_id"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}
    quarantined = read_rows(world.sealed / "quarantine.jsonl")
    assert reject not in survivors and {r["family_id"] for r in quarantined} == {reject}
    assert all(r["sealed"] is True and r["owner_status"] == "rejected" for r in quarantined)  # marked, still sealed
    doc = approvals(world)
    assert doc["rejected"] == [reject] and doc["escalated"] == [escalate]
    assert not any(rid.startswith(reject) for rid in doc["approved"]) and not any(rid.startswith(escalate) for rid in doc["approved"])
    assert {r["second_status"] for r in read_rows(world.sealed / "sealed_r1.jsonl") if r["family_id"] == escalate} == {"pending"}


# --- escalate ----------------------------------------------------------------------------------------
def test_escalation_forms_hold_phrasings_and_doubt_but_never_the_owners_key(world):
    sealed_fam = Spec(fid=fid_of(0x700), scope="partial", section="3.4", gaps=[{"id": "g1", "keywords": [f"{CANARY}gapword"]}], extra_groups=[["5.6"]])
    plain = Spec(fid=fid_of(0x701), section="2.2")
    sealed_component(world, [sealed_fam, plain])
    dev_fam = Spec(fid=fid_of(0x702), scope="refuse", section="")
    write_rows(world.dev / "tuning_r1.jsonl", dev_fam.rows(sealed=False))
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    path = world.mark_sheet(world.sheet_path("sealed", "s1"), {sealed_fam.fid: "E"}, default="A",
                            extra={f"{sealed_fam.fid}-1": {"doubt": "unsure whether the second part is covered"}})
    assert world.run("apply", "--kind", "sealed", "--sheet", path)[0] == 0
    code, out, err = world.run("escalate", "--kind", "sealed", "--sheet", path)
    assert code == 0, err
    assert world.last(out, "escalate")["items"] == "1" and CANARY not in out + err
    form = world.sealed / "work" / "escalations" / "form_s1.txt"
    text = form.read_text()
    questions = [r["question"] for r in read_rows(world.sealed / "sealed_r1.jsonl") if r["family_id"] == sealed_fam.fid]
    assert all(qn in text for qn in questions) and "unsure whether the second part is covered" in text
    assert plain.fid not in text and "Item E01" in text  # only the escalated item, under a short ref
    for key in ("3.4", "5.6", "gapword", '"scope"', "answer\": ", "partial\"", "evidence", "keywords"):  # the owner's key never goes out
        assert key not in text
    assert json.loads((world.sealed / "work" / "escalations" / "form_s1.refs.json").read_text()) == {"E01": sealed_fam.fid}
    assert not (world.root / "work" / "escalations").exists()
    # a development item goes under eval/private/work/escalations/, never under the sealed root
    world.run("sheet", "--kind", "development", "--name", "d1")
    dpath = world.mark_sheet(world.sheet_path("development", "d1"), {dev_fam.fid: "E"}, default="A")
    assert world.run("escalate", "--kind", "development", "--sheet", dpath)[0] == 0
    assert (world.root / "work" / "escalations" / "form_d1.txt").is_file()
    assert not (world.sealed / "work" / "escalations" / "form_d1.txt").exists()
    code, out, err = world.run("escalate", "--kind", "sealed", "--sheet", path)  # re-running rewrites the same form
    assert code == 0 and world.last(out, "escalate")["items"] == "1"


# --- calibrate ---------------------------------------------------------------------------------------
def _calibration_world(world, hard: int = 8, easy: int = 12):
    specs = [Spec(fid=fid_of(0x800 + i), scope="refuse" if i % 2 else "partial") for i in range(hard)]
    specs += [Spec(fid=fid_of(0x900 + i), scope="answer") for i in range(easy)]
    for i, spec in enumerate(specs):  # hand-built specs: give each a unique primary section
        spec.section = f"{i % 9 + 1}.{i // 9 + 1}"
    sealed_component(world, specs)
    world.run("sheet", "--kind", "sealed", "--name", "all")
    skip = specs[1].fid  # one family stays unapproved
    path = world.mark_sheet(world.sheet_path("sealed", "all"), {}, default="A")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names, lines = list(reader.fieldnames or []), list(reader)
    for line in lines:
        if line["family_id"] == skip:
            line["A"] = ""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
    assert world.run("apply", "--kind", "sealed", "--sheet", path)[0] == 0
    return specs, skip


def _freeze_records(world, *, only=("sealed", "tuning", "realistic")):
    """Plant the frozen sealed file (a copy of the component's rows) and the freeze records the calibration needs."""
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    frozen = write_rows(world.sealed / "heldout-v2.jsonl", rows)
    ledger = world.ledger()
    for split in only:
        extra = {"twins_sha256": "a" * 64} if split == "sealed" else {}
        ledger.record_freeze(split=split, set_sha256=sha(frozen) if split == "sealed" else "b" * 64, totals={}, **extra)
    return rows


def test_calibrate_draws_five_refuse_or_partial_and_ten_answer_approved_frozen_families_by_the_prereg_seed(world):
    specs, unapproved = _calibration_world(world)
    scope = {s.fid: s.scope for s in specs}
    world.write_prereg()
    _freeze_records(world)
    draw_path = world.sealed / "work" / "calibration_draw.json"
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == 0, err
    line = world.last(out, "calibrate")
    assert (line["drawn"], line["refuse_or_partial"], line["answer"]) == ("15", "5", "10")
    record = json.loads(draw_path.read_text())
    assert record["seed"] == 14 and len(set(record["families"])) == 15  # the prereg's calibration seed, nothing else
    assert sorted(scope[f] == "answer" for f in record["families"]).count(True) == 10
    assert unapproved not in record["families"]  # only approved families are drawn
    assert line["sha256"] == sha(draw_path)
    form = (world.sealed / "work" / "escalations" / "calibration_form.txt").read_text()
    rows = read_rows(world.sealed / "sealed_r1.jsonl")
    first = {r["family_id"]: r["question"] for r in rows if r["id"].endswith("-0")}
    second = {r["family_id"]: r["question"] for r in rows if r["id"].endswith("-1")}
    assert all(first[f] in form and second[f] not in form for f in record["families"])  # labelled from one phrasing, no key
    header, lines = read_csv(world.sheet_path("sealed", "calibration"))
    assert len(lines) == 15 and header == list(st.SHEET_COLUMNS)
    assert world.run("calibrate", "--kind", "sealed", "--seed", "3")[0] == st.EXIT_USAGE  # the --seed override is gone


def test_calibrate_is_drawn_once_and_a_second_run_only_reemits_or_refuses(world):
    _calibration_world(world)
    world.write_prereg()
    _freeze_records(world)
    draw_path = world.sealed / "work" / "calibration_draw.json"
    form_path = world.sealed / "work" / "escalations" / "calibration_form.txt"
    code, first, err = world.run("calibrate", "--kind", "sealed")
    assert code == 0, err
    before = (draw_path.read_bytes(), form_path.read_bytes(), world.sheet_path("sealed", "calibration").read_bytes())
    stamp = draw_path.stat().st_mtime_ns
    code, again, err = world.run("calibrate", "--kind", "sealed")
    assert code == 0 and again == first  # an identical recomputation: re-emitted
    assert (draw_path.read_bytes(), form_path.read_bytes(), world.sheet_path("sealed", "calibration").read_bytes()) == before
    assert draw_path.stat().st_mtime_ns == stamp  # nothing written
    draw = json.loads(draw_path.read_text())
    draw["seed"] += 1  # a different draw is on disk (or the prereg seed changed)
    draw_path.write_text(json.dumps(draw), encoding="utf-8")
    stale = draw_path.read_bytes()
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == st.EXIT_REFUSED and "reason=calibration_drawn" in err and out == ""
    assert draw_path.read_bytes() == stale


def test_calibrate_needs_every_split_frozen_and_draws_only_from_the_frozen_file(world):
    specs, _ = _calibration_world(world)
    world.write_prereg()
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == st.EXIT_REFUSED and "reason=splits_not_frozen" in err  # nothing frozen yet
    rows = _freeze_records(world, only=("sealed", "tuning"))
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == st.EXIT_REFUSED and "reason=splits_not_frozen" in err  # realistic is still open
    assert not (world.sealed / "work" / "calibration_draw.json").exists()
    world.ledger().record_freeze(split="realistic", set_sha256="b" * 64, totals={})
    # two approved answer families sit in a component but not in the frozen file: exactly 10 answer families remain,
    # so a draw that ignored the frozen file would (with probability 65/66) include one of them
    gone = [s.fid for s in specs if s.scope == "answer"][:2]
    write_rows(world.sealed / "heldout-v2.jsonl", [r for r in rows if r["family_id"] not in gone])
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == 0, err
    drawn = set(json.loads((world.sealed / "work" / "calibration_draw.json").read_text())["families"])
    assert not drawn & set(gone) and sum(scope == "answer" for scope in (next(s.scope for s in specs if s.fid == f) for f in drawn)) == 10
    code, out, err = world.run("calibrate", "--kind", "development")
    assert code == st.EXIT_REFUSED and "reason=splits_not_frozen" in err  # the frozen tuning / realistic files are absent


def test_calibrate_is_refused_when_too_few_families_are_approved(world):
    _calibration_world(world, hard=4, easy=12)
    world.write_prereg()
    _freeze_records(world)
    code, out, err = world.run("calibrate", "--kind", "sealed")
    assert code == st.EXIT_REFUSED and "reason=calibration_short" in err
    assert not (world.sealed / "work" / "calibration_draw.json").exists()


# --- disagree ----------------------------------------------------------------------------------------
def _disagreement_world(world):
    spec = lambda i, scope="answer", sec="1.1", extra=None, gaps=None: Spec(  # noqa: E731
        fid=fid_of(0xA00 + i), scope=scope, section=sec, extra_groups=extra or [], gaps=gaps)
    s = {
        "same": spec(1, sec="1.1"),
        "scope": spec(2, sec="2.1"),
        "missing": spec(3, sec="3.1"),
        "extra": spec(4, sec="4.1", extra=[["4.2"]]),
        "alt": spec(5, sec="5.1"),
        "related": spec(6, sec="6.1"),
        "gap_diff": spec(7, "partial", sec="7.1", gaps=[{"id": "g1", "keywords": ["alpha"]}]),
        "gap_same": spec(8, "partial", sec="8.1", gaps=[{"id": "g1", "keywords": ["Beta", "gamma"]}]),
    }
    rows = [r for sp_ in s.values() for r in sp_.rows(sealed=True)]
    for r in rows:
        if r["family_id"] == s["alt"].fid:
            r["evidence"] = [["5.1", "5.2"]]  # the owner's group offers two alternatives, the colleague's one
    write_rows(world.sealed / "sealed_r1.jsonl", rows)
    world.run("sheet", "--kind", "sealed", "--name", "s1")
    keys = {
        "same": {"scope": "answer", "evidence": [["1.1"]]},
        "scope": {"scope": "refuse", "evidence": []},
        "missing": {"scope": "answer", "evidence": [["3.1"], ["3.2"]]},
        "extra": {"scope": "answer", "evidence": [["4.1"]]},
        "alt": {"scope": "answer", "evidence": [["5.1"]]},
        "related": {"scope": "answer", "evidence": [["6.1.3"]]},  # a dotted child: related matching, not a difference
        "gap_diff": {"scope": "partial", "evidence": [["7.1"]], "gaps": [{"id": "g1", "keywords": ["omega"]}]},
        "gap_same": {"scope": "partial", "evidence": [["8.1"]], "gaps": [{"id": "g9", "keywords": ["gamma"]}]},
    }
    extra = {s[k].fid: {"colleague_key": json.dumps(v)} for k, v in keys.items()}
    return s, world.mark_sheet(world.sheet_path("sealed", "s1"), {}, default=None, extra=extra)


def test_disagree_reports_scope_groups_and_gaps_apart_with_wilson_intervals(world):
    s, sheet = _disagreement_world(world)
    code, out, err = world.run("disagree", "--kind", "sealed", "--sheet", sheet)
    assert code == 0, err
    line = world.last(out, "disagree")
    assert line["sample"] == "owner_selected_sample" and line["compared"] == "8"
    assert (line["scope_n"], line["scope_disagree"]) == ("8", "1")
    assert (line["group_n"], line["group_disagree"]) == ("7", "3")
    assert (line["missing_group"], line["extra_group"], line["alt_set_diff"]) == ("1", "1", "1")
    assert (line["gap_n"], line["gap_disagree"]) == ("2", "1")
    for dim, (k, n) in {"scope": (1, 8), "group": (3, 7), "gap": (1, 2)}.items():
        lo, hi = eval_stats.wilson_interval(k, n)
        assert (int(line[f"{dim}_lo_pm"]), int(line[f"{dim}_hi_pm"])) == (round(1000 * lo), round(1000 * hi))
    record = json.loads((world.sealed / "work" / "disagreement.json").read_text())
    assert set(record) == {sp_.fid for sp_ in s.values()}
    entry = record[s["missing"].fid]
    assert entry["owner"]["evidence"] == [["3.1"]] and entry["colleague"]["evidence"] == [["3.1"], ["3.2"]]  # both keys are kept
    assert entry["disagrees"] == {"scope": False, "groups": True, "gaps": False}
    assert record[s["related"].fid]["disagrees"]["groups"] is False and record[s["gap_same"].fid]["disagrees"]["gaps"] is False
    status = {r["family_id"]: r["second_status"] for r in read_rows(world.sealed / "sealed_r1.jsonl")}
    assert status[s["same"].fid] == "agree" and status[s["scope"].fid] == "disagree"
    assert "owner_selected_sample" in out and CANARY not in out + err
    code, out, err = world.run("disagree", "--kind", "sealed", "--sheet", sheet, "--calibration")
    assert world.last(out, "disagree")["sample"] == "random_sample_n15"


def test_disagree_refuses_an_unreadable_colleague_key(world):
    s, sheet = _disagreement_world(world)
    world.mark_sheet(sheet, {}, default=None, extra={s["same"].fid: {"colleague_key": "answer, probably"}})
    code, out, err = world.run("disagree", "--kind", "sealed", "--sheet", sheet)
    assert code == st.EXIT_USAGE and "reason=colleague_key_invalid" in err and "probably" not in out + err
    assert not (world.sealed / "work" / "disagreement.json").exists()


def test_group_and_gap_comparison_functions():
    assert st.compare_groups([["1.1"]], [["1.1"]]) == {"missing_group": False, "extra_group": False, "alt_set_diff": False}
    assert st.compare_groups([["1.1"]], [["1.1", "1.2"]])["alt_set_diff"] is True
    assert st.compare_groups([["1.1"]], [["1.1.2"]])["alt_set_diff"] is False  # related matching
    assert st.compare_groups([["1.1"], ["2.1"]], [["1.1"]])["extra_group"] is True
    assert st.compare_groups([["1.1"]], [["1.1"], ["2.1"]])["missing_group"] is True
    assert st.gaps_differ([{"id": "a", "keywords": ["x y"]}], [{"id": "b", "keywords": ["Y"]}]) is False
    assert st.gaps_differ([{"id": "a", "keywords": ["x"]}], [{"id": "a", "keywords": ["z"]}]) is True
    assert st.gaps_differ([{"id": "a", "keywords": ["x"]}], []) is True
