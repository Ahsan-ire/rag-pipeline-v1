"""Phase 16A-2b b4 (owner-approved 19 Oct): the paraphrase tooling.

Part 1, ``scripts/sp_launch.py``: ``out/dev.jsonl`` joins the ``out/`` allowlist (valid schema-2 rows over private ids, no
``sealed`` key, else the run is ``invalid``). Part 2, ``scripts/sealed_tool.py``: ``paraphrase-apply --dispatch <id>`` adds the
SP's new phrasings to released families (sealed rows from ``pool.jsonl``, development rows from ``dev.jsonl``), validates
everything before it writes, writes through one journalled transaction and prints counts only.

The SP and the real ``claude`` are never run. Every row, question and id is invented; the private root is the per-test
``tmp_path/eval/private`` (``tests/conftest.py``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from tests.p16w_sealed_world import (  # noqa: F401  (fixtures)
    build_standard, fid_of, make_row, q, read_rows, repos, standard_released, world, write_rows,
)
from tests.test_p16w_schema import _partial, _refuse, _row
from tests.test_p16w_sealed_tool_freeze import sealed_ready
from tests.test_p16w_sealed_tool_split import _round2_world
from tests.test_p16w_sp_launch import env  # noqa: F401  (fixture)
from tests.test_p16w_sp_launch_gate import GOOD_OUT, bad_run_checks, dispatch_id_of, inventory, write_out  # noqa: F401

DISPATCH = "ab12cd34"
SECOND = "cd34ef56"
NEW_CANARY = "zqxnewphrase"
SHA = re.compile(r"[0-9a-f]{64}")
ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================================================================
# Part 1: out/dev.jsonl in the launcher
# ===========================================================================================================================
DEV_A, DEV_B, DEV_C = "f3a1b2c3d", "f4a1b2c3d", "f5a1b2c3d"


def dev_text(**first: Any) -> str:
    """A valid development ``dev.jsonl``: three families, no ``sealed`` key anywhere; ``first`` overrides row 0's keys."""
    rows = [_row(1, fam=DEV_A), _row(2, fam=DEV_A), _partial(1, fam=DEV_B), _refuse(1, fam=DEV_C)]
    rows[0] = {**rows[0], **first}
    return "".join(json.dumps(row) + "\n" for row in rows)


def pool_text() -> str:
    """A valid sealed ``pool.jsonl`` (every row marked)."""
    return "".join(json.dumps(r) + "\n" for r in (_row(1, fam=FAM_P, sealed=True), _refuse(1, fam=FAM_Q, sealed=True)))


FAM_P, FAM_Q = "f6a1b2c3d", "f7a1b2c3d"


def test_dev_jsonl_is_on_the_allowlist_and_valid_rows_pass(tmp_path, inventory):
    assert "dev.jsonl" in sp.OUT_ALLOWED
    assert sp.validate_out(write_out(tmp_path / "only", {"dev.jsonl": dev_text()}))
    # next to every other allowlisted file (a paraphrase run writes pool.jsonl and dev.jsonl together)
    assert sp.validate_out(write_out(tmp_path / "all", {**GOOD_OUT, "dev.jsonl": dev_text(), "return.json": '{"count":1}'}))
    assert sp.validate_out(write_out(tmp_path / "pair", {"pool.jsonl": pool_text(), "dev.jsonl": dev_text()}))


@pytest.mark.parametrize("text", [
    dev_text(sealed=True),                                           # the development side is never marked ...
    dev_text(sealed=False),                                          # ... not even with a false marker (a marker is not a flag)
    dev_text() + json.dumps({**_row(3, fam=DEV_A), "sealed": True}) + "\n",   # one marked row among valid ones
    dev_text(evidence=[["999.9"]]),                                  # a label not in the inventory
    dev_text(scope="surprise"),                                      # not a schema scope
    dev_text(question=""),                                           # a schema error
    dev_text(id="x-0"),                                              # an id that is not private (and not the family's)
    dev_text(family_id="public-family-1", id="public-family-1-0"),   # a public-style family id
    dev_text(id=f"{DEV_A}-01"),                                      # a leading zero is not a row number
    dev_text(id=f"{DEV_B}-1"),                                       # the id names another family
    "this is not json\n",
    json.dumps({"schema": 1, "question": "q"}) + "\n",               # not schema 2
    "",                                                              # an empty file holds no rows
    dev_text() + '{"truncated": \n',
    '{"schema": 2, "schema": 2, "id": "f3a1b2c3d-0"}\n',             # a duplicate key
    "[]\n",
])
def test_a_bad_dev_jsonl_makes_the_out_directory_invalid(tmp_path, inventory, text):
    assert not sp.validate_out(write_out(tmp_path / "out", {"dev.jsonl": text}))


def test_a_repeated_question_or_a_split_scope_in_dev_jsonl_is_invalid(tmp_path, inventory):
    same = _row(1, fam=DEV_A)
    twin = {**same, "id": f"{DEV_A}-2"}  # the same question again in one set
    assert not sp.validate_out(write_out(tmp_path / "dup", {"dev.jsonl": json.dumps(same) + "\n" + json.dumps(twin) + "\n"}))
    scope = _refuse(2, fam=DEV_A)  # one family, two scopes
    assert not sp.validate_out(write_out(tmp_path / "scope", {"dev.jsonl": json.dumps(same) + "\n" + json.dumps(scope) + "\n"}))


def test_dev_jsonl_must_be_a_regular_file_and_other_entries_still_make_it_invalid(tmp_path, inventory):
    out = write_out(tmp_path / "out", {"return.json": '{"count":1}'})
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(dev_text(), encoding="utf-8")
    (out / "dev.jsonl").symlink_to(target)
    assert not sp.validate_out(out)
    (out / "dev.jsonl").unlink()
    (out / "dev.jsonl").mkdir()
    assert not sp.validate_out(out)
    assert not sp.validate_out(write_out(tmp_path / "stray", {"dev.jsonl": dev_text(), "dev.jsonl.bak": dev_text()}))
    assert not sp.validate_out(write_out(tmp_path / "case", {"Dev.jsonl": dev_text()}))  # the name is matched exactly


def test_dev_jsonl_is_validated_by_load_v2_and_never_by_the_sealed_loader(tmp_path, inventory, monkeypatch):
    seen: List[str] = []
    real = sp.eval_schema.load_v2

    def spy(path, **kw):
        seen.append(Path(path).name)
        return real(path, **kw)

    def refuse(path, **kw):
        raise AssertionError("dev.jsonl must not go through load_sealed")

    monkeypatch.setattr(sp.eval_schema, "load_v2", spy)
    monkeypatch.setattr(sp, "load_sealed", refuse)
    assert sp.validate_out(write_out(tmp_path / "out", {"dev.jsonl": dev_text()}))
    assert seen == ["dev.jsonl"]


def test_a_run_with_a_valid_dev_jsonl_is_released_with_the_rest_of_out(env, inventory):
    env.make_claude(files={"pool.jsonl": pool_text(), "dev.jsonl": dev_text()}, ret={"count": 1})
    outcome = env.launch(enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK and outcome.line == '{"count":1}'
    released = env.dest / dispatch_id_of(env)
    assert sorted(p.name for p in released.iterdir()) == ["dev.jsonl", "pool.jsonl", "return.json"]
    assert (released / "dev.jsonl").read_text(encoding="utf-8") == dev_text()  # byte for byte
    assert env.staging_dirs() == []


@pytest.mark.parametrize("text", [
    dev_text(sealed=True),
    dev_text(sealed=False),
    dev_text(evidence=[["999.9"]]),
    dev_text(family_id="public-family-1", id="public-family-1-0"),
])
def test_a_run_with_a_bad_dev_jsonl_is_invalid_and_releases_only_its_logs(env, inventory, text):
    env.make_claude(files={"dev.jsonl": text}, ret={"count": 1})
    bad_run_checks(env, env.launch(enums=["count"]), dispatch_id_of(env))


def test_a_run_with_an_unknown_extra_file_next_to_dev_jsonl_is_invalid(env, inventory):
    env.make_claude(files={"dev.jsonl": dev_text(), "notes.txt": "x"}, ret={"count": 1})
    bad_run_checks(env, env.launch(enums=["count"]), dispatch_id_of(env))


def test_the_launcher_documents_dev_jsonl():
    doc = " ".join(sp.__doc__.split())
    assert "``pool.jsonl``, ``dev.jsonl``, ``twins.jsonl``, ``return.json`` and ``tutorial_map.json``" in doc
    assert "of whose rows carries a ``sealed`` key" in doc and "sealed_tool paraphrase-apply" in doc


# ===========================================================================================================================
# Part 2: sealed_tool paraphrase-apply
# ===========================================================================================================================
def sealed_files(w) -> List[Path]:
    return sorted(w.sealed.glob("sealed_r*.jsonl"))


def dev_files(w) -> List[Path]:
    skip = {"tuning-v2.jsonl", "realistic-v2.jsonl"}
    return sorted(p for p in w.dev.glob("*.jsonl") if p.name not in skip and not p.name.startswith("quarantine"))


def file_of(w, fid: str) -> Path:
    for path in (*sealed_files(w), *dev_files(w)):
        if any(r["family_id"] == fid for r in read_rows(path)):
            return path
    raise AssertionError("no such family")


def number(row: Dict[str, Any]) -> int:
    return int(row["id"].rsplit("-", 1)[1])


def family_rows(w, fid: str) -> List[Dict[str, Any]]:
    return sorted((r for r in read_rows(file_of(w, fid)) if r["family_id"] == fid), key=number)


def is_sealed_family(w, fid: str) -> bool:
    return file_of(w, fid).parent == w.sealed


def shrink(w, fid: str) -> None:
    """Setup: leave the family one phrasing (row 0), as a migrated family that has not been topped up yet."""
    path = file_of(w, fid)
    write_rows(path, [r for r in read_rows(path) if not (r["family_id"] == fid and number(r) > 0)])


def new_phrasing(w, fid: str, n: Optional[int] = None, **over: Any) -> Dict[str, Any]:
    """A valid new row for ``fid``: row 0's scope, evidence, gaps, type and source, the next number, a fresh question;
    marked ``"sealed": true`` iff the family is a sealed-side one. ``over`` overrides any key (``None`` drops it)."""
    rows = family_rows(w, fid)
    n = max(number(r) for r in rows) + 1 if n is None else n
    row = {k: v for k, v in rows[0].items() if k not in ("sealed", "owner_status", "second_status")}
    row.update(id=f"{fid}-{n}", question=f"{NEW_CANARY} {q(fid[-4:] + 'x' + str(n))}", register="terse")
    if is_sealed_family(w, fid):
        row["sealed"] = True
    row.update(over)
    return {k: v for k, v in row.items() if v is not None}


def grow(w, fid: str, count: int) -> None:
    """Setup: the family holds ``count`` phrasings (new rows appended straight to its file)."""
    path = file_of(w, fid)
    while len(family_rows(w, fid)) < count:
        write_rows(path, [*read_rows(path), new_phrasing(w, fid)])


def submit(w, pool: Optional[Sequence[Dict[str, Any]]] = None, dev: Optional[Sequence[Dict[str, Any]]] = None,
           dispatch: str = DISPATCH) -> Path:
    """What ``sp_launch`` leaves behind for a paraphrase run: ``sealed/work/sp_out/<dispatch>/{pool,dev}.jsonl``."""
    out = w.sealed / "work" / "sp_out" / dispatch
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("pool.jsonl", pool), ("dev.jsonl", dev)):
        (out / name).unlink(missing_ok=True)
        if rows is not None:
            write_rows(out / name, rows)
    return out


def apply_dispatch(w, dispatch: str = DISPATCH):
    return w.run("paraphrase-apply", "--dispatch", dispatch)


def snapshot(w) -> Dict[str, bytes]:
    """Every file the tool could touch: the private root, the ledger's state and the repo's ledger mirror."""
    snap: Dict[str, bytes] = {}
    for base in (w.tmp / "eval", w.tmp / "state", w.repo / "eval"):
        for path in sorted(base.rglob("*")):
            if path.is_file():
                snap[str(path.relative_to(w.tmp))] = path.read_bytes()
    return snap


def counts(out: str) -> Dict[str, Dict[str, str]]:
    """The printed lines by kind (and side): ``{"paraphrase_apply:sealed": {...}, "paraphrase_inputs": {...}}``."""
    found: Dict[str, Dict[str, str]] = {}
    for ln in out.splitlines():
        kind, *parts = ln.split()
        fields = dict(p.split("=", 1) for p in parts)
        found[f"{kind}:{fields['side']}" if "side" in fields else kind] = fields
    return found


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Rig:
    """The released standard world with marks, and six families shrunk to one phrasing (three on each side)."""

    def __init__(self, w) -> None:
        self.w = w
        self.std = standard_released(w, sealed_mark="SMK", dev_mark="DMK")
        self.s_partial = self.std.sealed_singletons[0]  # sealed, partial (it has gaps), handbook
        self.s_group = self.std.sealed_groups[0][0]  # sealed, answer, tutorial
        self.s_refuse = self.std.sealed_refuse[0]  # sealed, refuse
        self.d_pin_t = self.std.pinned_tuning[0]  # development, pinned (migrated), legacy, answer
        self.d_pin_r = self.std.pinned_realistic[0]  # development, pinned (migrated), tutorial, answer
        self.d_draft = self.std.dev_drafts[3]  # development, released from the pool, answer
        self.d_other = self.std.dev_drafts[4]  # development, released, answer, left at two phrasings
        for fid in (self.s_partial, self.s_group, self.s_refuse, self.d_pin_t, self.d_pin_r, self.d_draft):
            shrink(w, fid)

    def good(self) -> Dict[str, List[Dict[str, Any]]]:
        """One new phrasing each for ``s_partial``, ``s_group``, ``d_pin_t`` and ``d_pin_r`` and two for ``s_refuse``: 3 sealed
        families (4 rows) and 2 development families (2 rows). ``d_draft`` is left at one phrasing."""
        w = self.w
        first = new_phrasing(w, self.s_refuse)
        return {
            "pool": [new_phrasing(w, self.s_partial), new_phrasing(w, self.s_group), first, new_phrasing(w, self.s_refuse, number(first) + 1)],
            "dev": [new_phrasing(w, self.d_pin_t), new_phrasing(w, self.d_pin_r)],
        }


@pytest.fixture
def rig(world) -> Rig:
    return Rig(world)


def assert_refused(w, code_expected: int, reason: str, dispatch: str = DISPATCH) -> None:
    """The command stops with ``reason``, prints no text or id, and changes no file (not even a journal or a record)."""
    before = snapshot(w)  # after the test staged the SP's files
    code, out, err = apply_dispatch(w, dispatch)
    assert code == code_expected and f"reason={reason}" in err, (code, err)
    assert all(ln.startswith("schema_error ") for ln in out.splitlines()), out  # at most schema line numbers
    text = out + err
    assert NEW_CANARY not in text and "SMK" not in text and "DMK" not in text and not re.search(r"\bf[0-9a-f]{8}\b", text)
    assert snapshot(w) == before, "a refusal changed a file"
    assert not list((w.sealed / "work").glob("paraphrase_*.journal")) and not list((w.sealed / "work").glob("paraphrase_*.data"))
    assert not list((w.root / "work").glob(".sp_dev.*"))  # the validation copy of dev.jsonl is always removed


# --- the happy paths ------------------------------------------------------------------------------------------------------
def test_apply_adds_sealed_rows_marked_and_development_rows_unmarked_and_prints_counts_only(rig):
    w = rig.w
    good = rig.good()
    out_dir = submit(w, **good)
    code, out, err = apply_dispatch(w)
    assert code == 0 and err == "", err
    lines = counts(out)
    assert lines["paraphrase_apply:sealed"] == {"side": "sealed", "families": "3", "rows": "4"}
    assert lines["paraphrase_apply:development"] == {"side": "development", "families": "2", "rows": "2"}
    # the histogram after applying: the sealed side has 12 families (one at 3 rows, 11 at 2); the development side 12 (one at 1)
    assert lines["paraphrase_phrasings:sealed"] == {"side": "sealed", "families_1": "0", "families_2": "11", "families_3": "1", "families_over_3": "0"}
    assert lines["paraphrase_phrasings:development"] == {"side": "development", "families_1": "1", "families_2": "11", "families_3": "0", "families_over_3": "0"}
    assert lines["paraphrase_inputs"] == {"pool_sha256": sha_of((out_dir / "pool.jsonl").read_bytes()), "dev_sha256": sha_of((out_dir / "dev.jsonl").read_bytes())}
    assert len(out.splitlines()) == 5
    # the rows are in their families' files, in order, every row of a sealed file marked and none of a development file
    assert [r["id"] for r in family_rows(w, rig.s_refuse)] == [f"{rig.s_refuse}-{n}" for n in range(3)]
    assert [number(r) for r in family_rows(w, rig.s_partial)] == [0, 1] and [number(r) for r in family_rows(w, rig.d_pin_r)] == [0, 1]
    for path in sealed_files(w):
        assert all(r.get("sealed") is True for r in read_rows(path))
    for path in dev_files(w):
        assert all("sealed" not in r for r in read_rows(path))
    for row in [*good["pool"], *good["dev"]]:  # each new row is stored exactly as the SP wrote it
        assert next(r for r in family_rows(w, row["family_id"]) if r["id"] == row["id"]) == row
    work = w.sealed / "work"
    assert not list(work.glob("paraphrase_*.journal")) and not list(work.glob("paraphrase_*.data"))
    assert json.loads((work / "paraphrase_applied.json").read_text()) == {
        DISPATCH: {"pool_sha256": lines["paraphrase_inputs"]["pool_sha256"], "dev_sha256": lines["paraphrase_inputs"]["dev_sha256"]}
    }
    assert not list((w.root / "work").glob(".sp_dev.*"))
    assert NEW_CANARY not in out and "SMK" not in out and "DMK" not in out and not re.search(r"\bf[0-9a-f]{8}\b", out)


def test_every_printed_value_is_on_the_allowlist(rig):
    submit(rig.w, **rig.good())
    code, out, err = apply_dispatch(rig.w)
    assert code == 0
    for ln in out.splitlines():
        assert re.fullmatch(r"paraphrase_(apply|phrasings|inputs)( [a-z][a-z0-9_]*=\S+)+", ln), ln
        for part in ln.split()[1:]:
            value = part.split("=", 1)[1]
            assert value.isdigit() or SHA.fullmatch(value) or value in st.ENUM_WORDS, ln


def test_apply_with_only_a_pool_file_or_only_a_dev_file(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    code, out, err = apply_dispatch(w)
    assert code == 0, err
    lines = counts(out)
    assert lines["paraphrase_apply:sealed"]["families"] == "1"
    assert lines["paraphrase_apply:development"] == {"side": "development", "families": "0", "rows": "0"}
    assert lines["paraphrase_inputs"]["dev_sha256"] == "none" and SHA.fullmatch(lines["paraphrase_inputs"]["pool_sha256"])
    submit(w, dev=[new_phrasing(w, rig.d_draft)], dispatch=SECOND)
    code, out, err = apply_dispatch(w, SECOND)
    assert code == 0, err
    lines = counts(out)
    assert lines["paraphrase_inputs"]["pool_sha256"] == "none" and lines["paraphrase_apply:development"]["rows"] == "1"
    applied = json.loads((w.sealed / "work" / "paraphrase_applied.json").read_text())
    assert set(applied) == {DISPATCH, SECOND} and applied[SECOND]["pool_sha256"] is None and applied[DISPATCH]["dev_sha256"] is None


def test_a_released_family_of_either_side_can_get_a_third_phrasing(world):
    std = standard_released(world)
    sealed, dev = std.sealed_singletons[0], std.dev_drafts[0]  # two phrasings each
    submit(world, pool=[new_phrasing(world, sealed)], dev=[new_phrasing(world, dev)])
    assert apply_dispatch(world)[0] == 0
    assert len(family_rows(world, sealed)) == 3 and len(family_rows(world, dev)) == 3


def test_a_family_may_gain_two_phrasings_at_once_up_to_three(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group), new_phrasing(w, rig.s_group, 2)])
    code, out, err = apply_dispatch(w)
    assert code == 0, err
    assert len(family_rows(w, rig.s_group)) == 3 and counts(out)["paraphrase_apply:sealed"]["rows"] == "2"


def test_old_rows_and_their_approvals_survive_and_only_the_new_rows_need_one(rig):
    w = rig.w
    w.approve_all("sealed")
    w.approve_all("development")
    before = {r["id"]: r for p in sealed_files(w) for r in read_rows(p)}
    submit(w, **rig.good())
    assert apply_dispatch(w)[0] == 0
    after = {r["id"]: r for p in sealed_files(w) for r in read_rows(p)}
    assert all(after[rid] == row for rid, row in before.items()) and len(after) == len(before) + 4  # the old rows, unchanged
    code, out, err = w.run("sheet", "--kind", "sealed", "--name", "after")  # the sheet lists what still needs an approval
    assert code == 0 and w.last(out, "sheet")["rows"] == "4" and w.last(out, "sheet")["families"] == "3"
    code, out, err = w.run("sheet", "--kind", "development", "--name", "afterdev")
    assert code == 0 and w.last(out, "sheet")["rows"] == "2"


# --- every refusal changes nothing -----------------------------------------------------------------------------------------
def test_a_row_naming_a_family_of_the_other_side_is_refused(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.d_draft, sealed=True)])  # a development family, in the sealed file
    assert_refused(w, st.EXIT_REFUSED, "wrong_side")
    submit(w, dev=[new_phrasing(w, rig.s_group)])  # a sealed family, in the development file
    assert_refused(w, st.EXIT_REFUSED, "wrong_side")
    submit(w, pool=[new_phrasing(w, rig.s_group)], dev=[new_phrasing(w, rig.d_draft), new_phrasing(w, rig.s_partial)])  # one bad row
    assert_refused(w, st.EXIT_REFUSED, "wrong_side")


def test_a_row_naming_an_unknown_family_is_refused(rig):
    w = rig.w
    stranger = make_row(fid_of(0xDEAD), 1, evidence=[["1.1"]], sealed=True)
    submit(w, pool=[stranger])
    assert_refused(w, st.EXIT_REFUSED, "unknown_family")
    submit(w, dev=[{k: v for k, v in stranger.items() if k != "sealed"}])
    assert_refused(w, st.EXIT_REFUSED, "unknown_family")


def test_a_quarantined_family_is_unknown(rig):
    w = rig.w
    row = new_phrasing(w, rig.s_group)
    path = file_of(w, rig.s_group)
    rows = read_rows(path)
    write_rows(path, [r for r in rows if r["family_id"] != rig.s_group])
    write_rows(w.sealed / "quarantine.jsonl", [r for r in rows if r["family_id"] == rig.s_group])  # a rejected family
    submit(w, pool=[row])
    assert_refused(w, st.EXIT_REFUSED, "unknown_family")


def test_a_family_still_in_the_pool_is_unknown(world):
    std = build_standard(world)  # drafted, not released: the families sit in sealed/pool/
    world.write_prereg()
    world.commit_prereg()
    (world.sealed / "work" / ".lock").touch()  # the two empty files every state command creates on first use
    world.ledger().records()
    submit(world, pool=[make_row(std.sealed[0], 5, evidence=[["1.1"]], sealed=True)])
    before = snapshot(world)
    assert_refused(world, st.EXIT_REFUSED, "unknown_family")


@pytest.mark.parametrize("ids", [[3], [2], [0], [1, 3], [2, 3]])
def test_new_row_numbers_must_be_exactly_the_next_ones(rig, ids):
    """The next number is 1 for a one-phrasing family: 3 skips, 2 leaves a gap, 0 repeats a row, [1, 3] and [2, 3] break a run."""
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group, n) for n in ids])
    assert_refused(w, st.EXIT_REFUSED, "row_index")
    submit(w, dev=[new_phrasing(w, rig.d_pin_t, n) for n in ids])
    assert_refused(w, st.EXIT_REFUSED, "row_index")


def test_a_repeated_row_id_is_a_schema_error_and_changes_nothing(rig):
    w = rig.w
    one, two = new_phrasing(w, rig.s_group), new_phrasing(w, rig.s_group)
    two["question"] = f"{NEW_CANARY} another way to ask it"
    submit(w, pool=[one, two])  # the same id twice
    assert_refused(w, st.EXIT_USAGE, "schema_invalid")


def test_next_numbers_follow_the_highest_existing_number_not_the_count(rig):
    w = rig.w
    path = file_of(w, rig.s_group)
    write_rows(path, [*read_rows(path), new_phrasing(w, rig.s_group, 4)])  # rows 0 and 4: two rows, highest number 4
    submit(w, pool=[new_phrasing(w, rig.s_group, 2)])
    assert_refused(w, st.EXIT_REFUSED, "row_index")
    submit(w, pool=[new_phrasing(w, rig.s_group, 5)])
    assert apply_dispatch(w)[0] == 0 and [number(r) for r in family_rows(w, rig.s_group)] == [0, 4, 5]


CHANGES = {
    "scope": lambda w, fid: {"scope": "refuse", "evidence": []},
    "evidence": lambda w, fid: {"evidence": [["9.20"]]},
    "type": lambda w, fid: {"type": "direct"},
    "source": lambda w, fid: {"source": "handbook" if family_rows(w, fid)[0].get("source") == "tutorial" else "tutorial"},
}


@pytest.mark.parametrize("field", sorted(CHANGES))
@pytest.mark.parametrize("side", ["sealed", "development"])
def test_a_new_row_must_keep_row_zeros_scope_evidence_type_and_source(rig, field, side):
    w = rig.w
    fid, key = (rig.s_group, "pool") if side == "sealed" else (rig.d_draft, "dev")
    submit(w, **{key: [new_phrasing(w, fid, **CHANGES[field](w, fid))]})
    assert_refused(w, st.EXIT_REFUSED, "row_mismatch")


def test_a_new_row_must_keep_row_zeros_gaps(rig):
    w = rig.w
    ref = family_rows(w, rig.s_partial)[0]
    assert ref["gaps"]  # the sealed partial family
    for gaps in ([{"id": "g1", "keywords": ["somethingelse"]}], [*ref["gaps"], {"id": "g2", "keywords": ["extra"]}],
                 [{"id": "g9", "keywords": ref["gaps"][0]["keywords"]}]):
        submit(w, pool=[new_phrasing(w, rig.s_partial, gaps=gaps)])
        assert_refused(w, st.EXIT_REFUSED, "row_mismatch")
    submit(w, pool=[new_phrasing(w, rig.s_partial)])  # the same gaps as row 0 pass
    assert apply_dispatch(w)[0] == 0


@pytest.mark.parametrize("over", [{"register": None}, {"owner_status": "approved"}, {"second_status": "agree"}])
def test_a_new_row_needs_a_register_and_no_review_status(rig, over):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group, **over)])
    assert_refused(w, st.EXIT_REFUSED, "row_mismatch")
    submit(w, dev=[new_phrasing(w, rig.d_draft, **over)])
    assert_refused(w, st.EXIT_REFUSED, "row_mismatch")


def test_every_valid_register_is_accepted_and_an_unknown_one_is_a_schema_error(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group, register="chatty")])
    assert_refused(w, st.EXIT_USAGE, "schema_invalid")
    assert set(st.REGISTERS) == {"lay", "modelled", "terse"}
    fams = (rig.s_group, rig.s_partial, rig.s_refuse)
    submit(w, pool=[new_phrasing(w, fid, register=reg) for fid, reg in zip(fams, st.REGISTERS)])
    assert apply_dispatch(w)[0] == 0
    assert sorted(family_rows(w, fid)[1]["register"] for fid in fams) == sorted(st.REGISTERS)


@pytest.mark.parametrize("side", ["sealed", "development"])
def test_a_new_question_must_differ_from_every_phrasing_of_its_family(rig, side):
    w = rig.w
    fid, key = (rig.s_group, "pool") if side == "sealed" else (rig.d_draft, "dev")
    existing = family_rows(w, fid)[0]["question"]
    for text in (existing.upper(), "  " + existing.replace(" ", "   ") + " ", existing.title(), existing.replace(" ", "\t")):
        assert text != existing
        submit(w, **{key: [new_phrasing(w, fid, question=text)]})
        assert_refused(w, st.EXIT_REFUSED, "question_repeat")
    one = new_phrasing(w, fid)  # two new phrasings that are the same question in different case
    two = new_phrasing(w, fid, number(one) + 1, question=one["question"].upper())
    submit(w, **{key: [one, two]})
    assert_refused(w, st.EXIT_REFUSED, "question_repeat")


def test_the_same_question_in_another_family_is_not_a_repeat(rig):
    w = rig.w
    other = family_rows(w, rig.d_other)[0]["question"]  # another family's question: the loader's exact-duplicate rule is per set
    submit(w, pool=[new_phrasing(w, rig.s_group, question=other.upper())])
    assert apply_dispatch(w)[0] == 0


@pytest.mark.parametrize("side", ["sealed", "development"])
def test_a_family_may_not_go_past_the_registered_maximum(rig, side):
    w = rig.w
    fid, fine, key = (rig.s_group, rig.s_refuse, "pool") if side == "sealed" else (rig.d_pin_t, rig.d_pin_r, "dev")
    grow(w, fid, 3)  # three rows already: a fourth is over the maximum of 3
    submit(w, **{key: [new_phrasing(w, fid)]})
    assert_refused(w, st.EXIT_REFUSED, "over_cap")
    submit(w, **{key: [new_phrasing(w, fine), new_phrasing(w, fid)]})  # one family fine, one over: nothing is applied
    assert_refused(w, st.EXIT_REFUSED, "over_cap")


def patched_prereg(monkeypatch, **paraphrase: int) -> None:
    """``phrasings_min`` / ``phrasings_max`` / ``t1_t7_cap`` are registered values, so the real prereg cannot be edited in a
    test world: the loader is wrapped to hand ``paraphrase-apply`` a copy with other numbers."""
    real = st.load_prereg

    def load(ctx):
        doc = copy.deepcopy(real(ctx))
        doc["drafting_rule"]["paraphrase"].update(paraphrase)
        return doc

    monkeypatch.setattr(st, "load_prereg", load)


def test_a_touched_family_must_end_with_at_least_the_registered_minimum(rig, monkeypatch):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group)])  # one phrasing topped up to 2: fine under the registered minimum of 2
    before = snapshot(w)
    with monkeypatch.context() as patch:
        patched_prereg(patch, phrasings_min=3)
        assert_refused(w, st.EXIT_REFUSED, "under_min")
    assert apply_dispatch(w)[0] == 0


def test_a_migrated_tutorial_family_is_held_to_the_t1_t7_cap_and_other_families_are_not(rig, monkeypatch):
    w = rig.w
    patched_prereg(monkeypatch, t1_t7_cap=2)  # (the registered cap is 3, the same as the maximum: nothing differs there)
    grow(w, rig.d_pin_r, 2)  # pinned tutorial family: migrated
    grow(w, rig.s_group, 2)  # sealed tutorial family: drafted, never migrated
    grow(w, rig.d_pin_t, 2)  # pinned legacy family: migrated but not tutorial-sourced
    submit(w, dev=[new_phrasing(w, rig.d_pin_r)])
    assert_refused(w, st.EXIT_REFUSED, "over_cap")
    submit(w, dev=[new_phrasing(w, rig.d_pin_t)], pool=[new_phrasing(w, rig.s_group)])
    assert apply_dispatch(w)[0] == 0  # three rows each (the maximum): only the migrated tutorial family had the lower cap


def test_a_released_tutorial_family_of_the_development_side_is_not_migrated(world, monkeypatch):
    std = standard_released(world)
    fid = std.dev_drafts[0]
    path = file_of(world, fid)
    assert path.name in ("tuning_r1.jsonl", "realistic_r1.jsonl")  # a release output, not a pinned file
    write_rows(path, [dict(r, source="tutorial") if r["family_id"] == fid else r for r in read_rows(path)])
    patched_prereg(monkeypatch, t1_t7_cap=2)
    submit(world, dev=[new_phrasing(world, fid)])
    assert apply_dispatch(world)[0] == 0  # 3 rows: the cap of 2 is for migrated families only


def test_a_dispatch_is_applied_once(rig):
    w = rig.w
    submit(w, **rig.good())
    assert apply_dispatch(w)[0] == 0
    assert_refused(w, st.EXIT_REFUSED, "already_applied")
    path = file_of(w, rig.s_group)  # the record, not the rows, decides: put the family back to one row and it is still refused
    write_rows(path, [r for r in read_rows(path) if not (r["family_id"] == rig.s_group and number(r) > 0)])
    assert_refused(w, st.EXIT_REFUSED, "already_applied")


def test_a_recorded_dispatch_is_refused_before_its_files_are_read(rig):
    w = rig.w
    (w.sealed / "work" / "paraphrase_applied.json").write_text(json.dumps({DISPATCH: {"pool_sha256": None, "dev_sha256": "a" * 64}}), encoding="utf-8")
    assert_refused(w, st.EXIT_REFUSED, "already_applied")  # no sp_out directory exists at all


@pytest.mark.parametrize("doc", ["[]", '{"notanid": {}}', '{"ab12cd34": {"pool_sha256": 1, "dev_sha256": null}}', "not json"])
def test_a_damaged_applied_record_stops_the_command(rig, doc):
    w = rig.w
    (w.sealed / "work" / "paraphrase_applied.json").write_text(doc, encoding="utf-8")
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    assert_refused(w, st.EXIT_USAGE, "input_invalid")


def test_missing_and_malformed_inputs_and_a_bad_dispatch_id(rig):
    w = rig.w
    assert_refused(w, st.EXIT_USAGE, "input_missing")  # no sp_out/<id>/ at all
    out = submit(w)
    assert_refused(w, st.EXIT_USAGE, "input_missing")  # a directory with neither file
    real = w.tmp / "real.jsonl"
    write_rows(real, [new_phrasing(w, rig.s_group)])
    (out / "pool.jsonl").symlink_to(real)
    assert_refused(w, st.EXIT_USAGE, "input_invalid")
    (out / "pool.jsonl").unlink()
    (out / "dev.jsonl").mkdir()
    assert_refused(w, st.EXIT_USAGE, "input_invalid")
    for bad in ("", "xyz", "a" * 9, "AB12CD34", "../x"):
        code, o, e = w.run("paraphrase-apply", "--dispatch", bad)
        assert code == st.EXIT_USAGE and "reason=usage" in e and o == ""
    assert w.run("paraphrase-apply")[0] == st.EXIT_USAGE


def test_a_dev_jsonl_that_is_not_valid_is_a_schema_error_without_text(rig):
    w = rig.w
    submit(w, dev=[new_phrasing(w, rig.d_draft, evidence=[["999.9"]])])
    before = snapshot(w)
    code, out, err = apply_dispatch(w)
    assert code == st.EXIT_USAGE and "reason=schema_invalid" in err and "schema_error line=1 field=evidence[0][0]" in out
    assert NEW_CANARY not in out + err and "999.9" not in out + err
    assert snapshot(w) == before and not list((w.root / "work").glob(".sp_dev.*"))
    submit(w, dev=[])  # an empty file holds no rows
    assert_refused(w, st.EXIT_USAGE, "schema_invalid")


def test_a_dev_jsonl_row_with_a_sealed_marker_is_refused(rig):
    w = rig.w
    submit(w, dev=[new_phrasing(w, rig.d_draft, sealed=True)])  # the development side is never marked
    assert_refused(w, st.EXIT_REFUSED, "wrong_side")
    submit(w, dev=[new_phrasing(w, rig.d_draft, sealed=False)])  # a marker is not a flag
    assert_refused(w, st.EXIT_USAGE, "schema_invalid")


def test_a_pool_jsonl_that_is_not_valid_is_a_schema_error(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group, scope="surprise")])
    assert_refused(w, st.EXIT_USAGE, "schema_invalid")


def test_a_prereg_other_than_the_registered_one_is_refused(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    w.prereg_path.write_text(w.prereg_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # still valid, no longer registered
    assert_refused(w, st.EXIT_REFUSED, "prereg_amended")


def test_a_family_in_two_component_files_is_refused(rig):
    w = rig.w
    write_rows(w.dev / "tuning_extra.jsonl", [make_row(rig.d_pin_t, 7, evidence=[["1.1"]], sealed=False, source="legacy", register="modelled")])
    submit(w, dev=[new_phrasing(w, rig.d_draft)])
    assert_refused(w, st.EXIT_USAGE, "input_invalid")


def test_a_dispatch_is_refused_while_another_process_holds_the_lock(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    holder = "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\nfcntl.flock(fd, fcntl.LOCK_EX)\nprint('ready', flush=True)\nsys.stdin.readline()\n"
    proc = subprocess.Popen([sys.executable, "-c", holder, str(w.sealed / "work" / ".lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        before = snapshot(w)
        assert apply_dispatch(w) == (st.EXIT_REFUSED, "", "error reason=locked\n")
        assert snapshot(w) == before
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert apply_dispatch(w)[0] == 0
    assert "paraphrase-apply" in st.STATE_COMMANDS


# --- frozen splits ------------------------------------------------------------------------------------------------------
def test_a_family_of_a_frozen_split_is_refused_and_one_of_an_open_split_is_not(world):
    std = sealed_ready(world)
    world.approve_all("development")
    assert world.recheck("sealed")[0] == 0
    assert world.recheck("realistic-v2")[0] == 0
    sealed, realistic, tuning = std.sealed_singletons[0], read_rows(world.dev / "realistic_r1.jsonl")[0]["family_id"], read_rows(world.dev / "tuning_r1.jsonl")[0]["family_id"]
    submit(world, pool=[new_phrasing(world, sealed)])
    assert_refused(world, st.EXIT_REFUSED, "frozen")
    submit(world, dev=[new_phrasing(world, realistic)])
    assert_refused(world, st.EXIT_REFUSED, "frozen")
    submit(world, pool=[new_phrasing(world, sealed)], dev=[new_phrasing(world, tuning)])  # one open, one frozen: nothing is applied
    assert_refused(world, st.EXIT_REFUSED, "frozen")
    submit(world, dev=[new_phrasing(world, tuning)])  # tuning is not frozen: its families may still be topped up
    assert apply_dispatch(world)[0] == 0 and len(family_rows(world, tuning)) == 3


# --- crashes ------------------------------------------------------------------------------------------------------------
def crash_at(monkeypatch, point: str, occurrence: int = 1) -> None:
    seen = {"n": 0}

    def boom(name: str) -> None:
        if name == point:
            seen["n"] += 1
            if seen["n"] == occurrence:
                raise RuntimeError("simulated crash")

    monkeypatch.setattr(st, "_paraphrase_crash", boom)


@pytest.mark.parametrize("point,occurrence", [("after_journal", 1), ("after_file", 1), ("after_file", 2), ("after_file", 3), ("after_record", 1)])
def test_a_crash_at_any_step_is_finished_from_the_journal(rig, monkeypatch, point, occurrence):
    w = rig.w
    good = rig.good()
    submit(w, **good)
    targets = {file_of(w, f) for f in (rig.s_partial, rig.d_pin_t, rig.d_pin_r)}  # the sealed file and two pinned development ones
    assert len(targets) == 3
    old = {p: p.read_bytes() for p in targets}
    crash_at(monkeypatch, point, occurrence)
    code, out, err = apply_dispatch(w)
    assert code == st.EXIT_FAIL and "reason=internal" in err and out == ""  # the crash: no exception text, no counts yet
    work = w.sealed / "work"
    assert (work / f"paraphrase_{DISPATCH}.journal").is_file() and (work / f"paraphrase_{DISPATCH}.data").is_dir()
    changed = sum(1 for p, data in old.items() if p.read_bytes() != data)
    assert changed == (0 if point in ("after_journal",) else occurrence if point == "after_file" else 3)
    assert not (work / "paraphrase_applied.json").exists() or point == "after_record"
    code, _, e2 = w.run("split", "--round", "1", "--diagnose")  # a diagnosis refuses a world in the middle of a transaction
    assert code == st.EXIT_REFUSED and "reason=recovery_pending" in e2
    monkeypatch.setattr(st, "_paraphrase_crash", lambda name: None)
    code, out, err = apply_dispatch(w)  # the same command resumes it and prints the same counts
    assert code == 0, err
    lines = counts(out)
    assert lines["paraphrase_apply:sealed"] == {"side": "sealed", "families": "3", "rows": "4"}
    assert lines["paraphrase_apply:development"] == {"side": "development", "families": "2", "rows": "2"}
    for row in [*good["pool"], *good["dev"]]:
        assert next(r for r in family_rows(w, row["family_id"]) if r["id"] == row["id"]) == row
    assert len(family_rows(w, rig.s_refuse)) == 3 and len(family_rows(w, rig.s_group)) == 2  # nothing lost or repeated
    assert not list(work.glob("paraphrase_*.journal")) and not list(work.glob("paraphrase_*.data"))
    assert json.loads((work / "paraphrase_applied.json").read_text())[DISPATCH]["pool_sha256"] == lines["paraphrase_inputs"]["pool_sha256"]
    assert_refused(w, st.EXIT_REFUSED, "already_applied")  # and it is applied exactly once
    assert "recovery_pending" not in w.run("split", "--round", "1", "--diagnose")[2]


def test_any_state_changing_command_finishes_a_journalled_paraphrase_first(rig, monkeypatch):
    w = rig.w
    good = rig.good()
    submit(w, **good)
    crash_at(monkeypatch, "after_file", 1)
    assert apply_dispatch(w)[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "_paraphrase_crash", lambda name: None)
    assert w.run("sheet", "--kind", "sealed", "--name", "after")[0] == 0
    work = w.sealed / "work"
    assert not list(work.glob("paraphrase_*.journal")) and not list(work.glob("paraphrase_*.data"))
    for row in [*good["pool"], *good["dev"]]:
        assert next(r for r in family_rows(w, row["family_id"]) if r["id"] == row["id"]) == row
    assert DISPATCH in json.loads((work / "paraphrase_applied.json").read_text())


def test_a_file_that_changed_after_the_journal_is_a_ledger_stop_and_nothing_more_is_written(rig, monkeypatch):
    w = rig.w
    submit(w, **rig.good())
    crash_at(monkeypatch, "after_journal")
    assert apply_dispatch(w)[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "_paraphrase_crash", lambda name: None)
    sealed_file = file_of(w, rig.s_group)  # written first
    sealed_file.write_bytes(sealed_file.read_bytes() + b"\n")  # neither the journalled old bytes nor the new ones
    dev_before = {p: p.read_bytes() for p in dev_files(w)}
    code, out, err = apply_dispatch(w)
    assert code == st.EXIT_LEDGER_STOP and "reason=paraphrase_recovery_failed" in err and out == ""
    assert {p: p.read_bytes() for p in dev_files(w)} == dev_before  # the files after it were not touched
    assert (w.sealed / "work" / f"paraphrase_{DISPATCH}.journal").is_file() and not (w.sealed / "work" / "paraphrase_applied.json").exists()
    assert w.run("sheet", "--kind", "sealed", "--name", "x")[0] == st.EXIT_LEDGER_STOP  # every state command stops


def test_tampered_staged_bytes_or_journal_are_a_ledger_stop(rig, monkeypatch):
    w = rig.w
    submit(w, **rig.good())
    crash_at(monkeypatch, "after_journal")
    assert apply_dispatch(w)[0] == st.EXIT_FAIL
    monkeypatch.setattr(st, "_paraphrase_crash", lambda name: None)
    work = w.sealed / "work"
    before = {k: v for k, v in snapshot(w).items() if "paraphrase_" not in k}
    staged = work / f"paraphrase_{DISPATCH}.data" / "0.stage"
    staged.write_bytes(staged.read_bytes() + b"\n")  # the staged bytes no longer match the journalled sha256
    code, out, err = apply_dispatch(w)
    assert code == st.EXIT_LEDGER_STOP and "reason=paraphrase_recovery_failed" in err
    assert {k: v for k, v in snapshot(w).items() if "paraphrase_" not in k} == before  # no file of the world was written
    journal = work / f"paraphrase_{DISPATCH}.journal"
    good_journal = journal.read_bytes()
    for damage in (b"not json", json.dumps({"version": 2}).encode(), good_journal.replace(b"sealed_r1", b"../x"), good_journal.replace(b"sealed/", b"/etc/")):
        journal.write_bytes(damage)
        code, out, err = apply_dispatch(w)
        assert code == st.EXIT_LEDGER_STOP and "reason=paraphrase_recovery_failed" in err, damage[:20]


def test_staged_bytes_without_a_journal_are_removed(rig):
    w = rig.w
    stale = w.sealed / "work" / f"paraphrase_{DISPATCH}.data"
    stale.mkdir(parents=True)
    (stale / "0.stage").write_text("junk", encoding="utf-8")
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    assert apply_dispatch(w)[0] == 0 and not stale.exists()  # a crash before the journal left it: it is replaced and removed
    leftover = w.sealed / "work" / "paraphrase_00000000.data"
    leftover.mkdir()
    assert w.run("sheet", "--kind", "sealed", "--name", "x")[0] == 0 and not leftover.exists()


# --- what the release and the freeze depend on ----------------------------------------------------------------------------
def test_the_release_record_and_payload_do_not_depend_on_row_counts_or_hashes(rig):
    """The stored ``assign_r1`` payload holds family records (row 0's scope, chapter, source, register), ids, edges and the pool
    digest of the rows that left ``pool/``: adding phrasings to released families changes none of it, and the command records
    nothing in the ledger."""
    w = rig.w
    ledger = w.ledger()
    payload_before = ledger.load_payload("assign_r1", ledger.records("release")[0]["payload_sha256"])
    records_before = copy.deepcopy(ledger.records())
    submit(w, **rig.good())
    assert apply_dispatch(w)[0] == 0
    ledger = w.ledger()
    assert ledger.records() == records_before
    assert ledger.load_payload("assign_r1", ledger.records("release")[0]["payload_sha256"]) == payload_before
    assert (w.sealed / "work" / "release_r1.done").is_file()
    assert w.run("split", "--round", "1")[2].count("already_released") == 1  # recovery after a paraphrase replays nothing


def test_freeze_replays_the_release_and_freezes_after_paraphrases(world):
    """A world that splits, paraphrases and freezes: the replay, the "files still hold the assignment" check and the group
    computations see the same families, so adding phrasings never makes ``freeze`` refuse."""
    rig = Rig(world)
    submit(world, **rig.good())
    submit(world, dev=[new_phrasing(world, rig.d_draft)], dispatch=SECOND)  # the last development family still at one phrasing
    assert apply_dispatch(world)[0] == 0 and apply_dispatch(world, SECOND)[0] == 0
    world.approve_all("sealed")
    world.approve_all("development")
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    line = world.last(out, "freeze")
    assert line["status"] == "frozen" and line["rows"] == "25"  # 12 families: 11 at two rows and one (s_refuse) at three
    frozen = read_rows(world.sealed / "heldout-v2.jsonl")
    assert {r["family_id"] for r in frozen} == set(rig.std.sealed) and len([r for r in frozen if r["family_id"] == rig.s_refuse]) == 3
    assert all(r.get("sealed") is True for r in frozen)
    for split in ("realistic-v2", "tuning-v2"):
        code, out, err = world.recheck(split)
        assert code == 0, err
        assert all("sealed" not in r for r in read_rows(world.dev / f"{split}.jsonl"))
    assert {r["id"] for r in read_rows(world.dev / "tuning-v2.jsonl")} >= {r["id"] for r in family_rows(world, rig.d_pin_t)}


def test_a_recheck_screen_written_before_a_paraphrase_is_stale_by_design(rig):
    w = rig.w
    submit(w, pool=[new_phrasing(w, rig.s_group)])
    w.approve_all("sealed")
    code, out, err = w.run("freeze", "--split", "sealed")  # the screen of the content as it is now
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err
    required = int(dict(p.split("=") for p in err.split()[1:] if "=" in p)["pairs_required"])
    assert apply_dispatch(w)[0] == 0  # the SP's recheck would come back for content a paraphrase has since changed
    verdict = w.verdict(name="late.jsonl")
    ret = verdict.with_name("late.return.json")
    ret.write_text(json.dumps({"status": "ok", "kind": "recheck", "pairs_checked": required, "twin_pairs": 0}), encoding="utf-8")
    later = time.time() + 5
    for p in (verdict, ret):
        os.utime(p, (later, later))
    code, out, err = w.run("freeze", "--split", "sealed", "--recheck-verdict", verdict, "--recheck-return", ret)
    assert code == st.EXIT_REFUSED and "reason=recheck_stale" in err


def test_a_round_two_family_topped_up_by_paraphrase_apply_freezes_with_both_rounds_bound(world):
    """The path the existing freeze test walks by hand (a replacement left at one phrasing), taken with the tool: round 1,
    a rejection, round 2, one more phrasing for the round-2 family, approvals, recheck, freeze."""
    std, rejected, replacement, *_ = _round2_world(world, replacement_phrasings=1)
    shrunk = std.sealed_groups[1][0]  # a paraphrase between the round-2 twins record and the round-2 split leaves the record valid
    shrink(world, shrunk)
    submit(world, pool=[new_phrasing(world, shrunk)], dispatch=SECOND)
    assert apply_dispatch(world, SECOND)[0] == 0
    assert world.run("split", "--round", "2")[0] == 0
    assert len(family_rows(world, replacement)) == 1  # released into sealed_r2.jsonl with one phrasing
    submit(world, pool=[new_phrasing(world, replacement)])
    assert apply_dispatch(world)[0] == 0
    world.approve_all("sealed")
    code, out, err = world.recheck("sealed")
    assert code == 0, err
    assert {r["family_id"] for r in read_rows(world.sealed / "heldout-v2.jsonl")} == (set(std.sealed) - {rejected}) | {replacement}
    twins = world.ledger().load_payload("twins_sealed", world.ledger().freeze_record("sealed")["twins_sha256"])
    assert set(twins["rounds"]) == {"1", "2"}  # both rounds' twins records are bound: both replays passed


# --- bookkeeping ----------------------------------------------------------------------------------------------------------
NEW_REASONS = ("wrong_side", "row_index", "row_mismatch", "question_repeat", "over_cap", "under_min", "already_applied", "paraphrase_recovery_failed")


def test_the_refusal_reasons_are_enum_words_and_every_reason_the_command_uses_is_one():
    for reason in NEW_REASONS:
        assert reason in st.REASONS and reason in st.ENUM_WORDS
    assert len(set(st.REASONS)) == len(st.REASONS)
    source = Path(st.__file__).read_text(encoding="utf-8")
    body = source[source.index("# paraphrase-apply: the SP's new phrasings"):source.index("# Approvals, sheets, apply")]
    used = set(re.findall(r"Stop\(EXIT_[A-Z_]+, \"([a-z_]+)\"", body))
    assert used <= set(st.REASONS) and set(st.PARAPHRASE_ROW_CHECKS) <= set(st.REASONS)
    assert set(NEW_REASONS) <= used | set(st.PARAPHRASE_ROW_CHECKS)  # the row checks name theirs from PARAPHRASE_ROW_CHECKS


def test_the_command_is_documented_and_registered():
    doc = " ".join(st.__doc__.split())
    assert "``paraphrase-apply``" in doc and "**paraphrase-apply.**" in doc and "paraphrase_applied.json" in doc
    assert "family membership only" in doc and "``dev.jsonl``" in doc
    args = st.build_parser().parse_args(["paraphrase-apply", "--dispatch", DISPATCH])
    assert args.command == "paraphrase-apply" and args.dispatch == DISPATCH
    assert "paraphrase-apply" in st.STATE_COMMANDS


def test_the_sp_brief_describes_the_two_output_files():
    text = " ".join((ROOT / "docs" / "sealed" / "sealed-processor.md").read_text(encoding="utf-8").split())
    section = text.split("### `paraphrase`", 1)[1].split("### `recheck`", 1)[0]
    assert 'A new row of a family in `pool/` goes to `out/pool.jsonl` and is marked `"sealed": true`.' in section
    assert "A new row of a family in `dev/` goes to `out/dev.jsonl` and carries **no** `sealed` key" in section
    assert "Write **only the new rows**; never copy an existing row into `out/`." in section
    assert "one more than the highest row number the family already has" in section
    assert "scope, evidence, gaps, type and source" in section
    assert "`pool/` holds sealed-side families, `dev/` development ones" in text


def test_the_phrasing_bounds_are_the_registered_ones(world):
    rule = world.prereg_doc()["drafting_rule"]["paraphrase"]
    assert (rule["phrasings_min"], rule["phrasings_max"], rule["t1_t7_cap"]) == (2, 3, 3)
