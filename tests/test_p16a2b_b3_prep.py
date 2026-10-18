"""16A-2b b3: ``scripts/b3_prep.py``, the tool that builds attempt 1's ``draft`` dispatch inputs and prints the launcher command.

Synthetic fixtures only: invented questions (each carries a canary), invented section labels, a tmp repo (a real git clone from
the ``world`` fixture, so the prereg is genuinely committed), a tmp private root (``tests/conftest.py`` points
``eval_privacy.private_root`` at ``tmp_path/eval/private``) and invented stand-ins for the handbook dump and the tutorial folder.
Nothing reads ``data/``, ``Tutorial_Docs_for_review/`` or a real set, and nothing starts the SP.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from scripts import b3_prep as prep
from scripts import sealed_tool as st
from scripts import sp_launch as sp
from tests.p16w_sealed_world import CANARY, INVENTORY, repos, world  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
BRIEF = ROOT / "docs" / "sealed" / "sealed-processor.md"
MANUAL = "2025 PPC Applied Land Law Student Tutorial Manual (synthetic).pdf"
SHEETS = ("Tutorial One Exercises.pdf", "Tutorial Seven Exercises.pdf")
LINE = re.compile(r"^(?P<kind>[a-z_]+)( (?P<key>[a-z_0-9]+)=(?P<val>[A-Za-z0-9_]+))+$")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_tree(root: Path) -> Dict[str, str]:
    """Relative path -> sha256 of every file under ``root``; a symlink anywhere in the tree fails the test."""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            assert not os.path.islink(Path(dirpath, name)), "the dispatch tree holds a symlink"
        for name in filenames:
            full = Path(dirpath, name)
            out[os.path.relpath(full, root)] = sha(full)
    return out


class Rig:
    """The inputs of one prep run, written under the ``world`` fixture's repo and private root, and a ``run`` that calls ``main``."""

    def __init__(self, world: Any, eval_registry: Any) -> None:
        self.world = world
        self.registry = eval_registry
        self.repo = world.repo
        self.private = world.root
        self.out_dir = self.private / "work" / "b3" / "draft1"
        self.dump = self.repo / "data" / "research" / "handbook_sections.jsonl"
        self.tutorials = self.repo / "Tutorial_Docs_for_review"
        self.inventory = self.repo / "eval" / "section_inventory.json"
        self.brief = self.repo / "docs" / "sealed" / "sealed-processor.md"
        self.issued = self.private / "sealed" / "work" / "issued_ids.txt"
        self.prereg_commit: Optional[str] = None

    # -- inputs ------------------------------------------------------------------------
    def v1(self, name: str, types: List[str]) -> Path:
        path = self.repo / "eval" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps({"question": f"{CANARY} {name} question {i}", "type": t, "expected_sections": [] if t == "refusal" else ["1.1"]}) + "\n"
            for i, t in enumerate(types)), encoding="utf-8")
        self.registry.add(path, privacy="public", name=name)
        return path

    def build(self, gold: Optional[List[str]] = None, real: Optional[List[str]] = None, tutorial: int = 1,
              manual: bool = True, dump: bool = True, commit: bool = True) -> None:
        """The default world: 8 golden + 4 realistic + 1 tutorial exercise, all answerable (105 drafts under the test quota)."""
        self.v1("golden_set.jsonl", gold if gold is not None else ["direct"] * 8)
        self.v1("realistic_set.jsonl", real if real is not None else ["direct"] * 4)
        (self.repo / "eval" / "v2_pairing.json").write_text(json.dumps({"pairs": []}), encoding="utf-8")
        counts = self.private / "inputs" / "t1t7_count_v1.jsonl"
        counts.parent.mkdir(parents=True, exist_ok=True)
        counts.write_text("".join(json.dumps({"id": f"T01-A{chr(97 + i)}", "type": "direct"}) + "\n" for i in range(tutorial)), encoding="utf-8")
        self.inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
        self.brief.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BRIEF, self.brief)
        if dump:
            self.dump.parent.mkdir(parents=True, exist_ok=True)
            self.dump.write_text(json.dumps({"label": "1.1", "page_start": 1, "page_end": 1, "text": f"{CANARY} handbook text"}) + "\n", encoding="utf-8")
        self.tutorials.mkdir(parents=True, exist_ok=True)
        if manual:
            (self.tutorials / MANUAL).write_bytes(b"%PDF synthetic manual " + CANARY.encode() + b"\x00\xff")
        for sheet in SHEETS:
            (self.tutorials / sheet).write_bytes(b"%PDF synthetic exercise sheet " + CANARY.encode())
        self.world.write_prereg(branch="phase-16a2b-data")  # not the world's "main": the command must take the prereg's own branch
        if commit:
            self.prereg_commit = self.world.commit_prereg(push=False)

    # -- running -----------------------------------------------------------------------
    def run(self, *extra: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = prep.main(["--repo", str(self.repo), *extra], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *extra: str) -> str:
        code, out, err = self.run(*extra)
        assert code == 0, err
        return out

    @staticmethod
    def line(out: str, kind: str) -> Dict[str, str]:
        hits = [ln for ln in out.splitlines() if ln.startswith(kind + " ")]
        assert hits, f"no {kind!r} line in {out!r}"
        return dict(part.split("=", 1) for part in hits[-1].split(" ")[1:])

    @staticmethod
    def command(out: str) -> str:
        return out.splitlines()[-1]

    def task(self) -> Dict[str, Any]:
        return json.loads((self.out_dir / "task.json").read_text(encoding="utf-8"))

    def ids(self) -> List[str]:
        return (self.out_dir / "ids.txt").read_text(encoding="utf-8").split()

    def issued_ids(self) -> List[str]:
        return self.issued.read_text(encoding="utf-8").split() if self.issued.is_file() else []

    def count_migrated(self) -> Tuple[int, str, str]:
        """``sealed_tool count-migrated`` over the same inputs: ``(exit code, stdout, stderr)``."""
        return self.world.run(
            "count-migrated", "--golden", self.repo / "eval" / "golden_set.jsonl", "--realistic", self.repo / "eval" / "realistic_set.jsonl",
            "--pairing", self.repo / "eval" / "v2_pairing.json", "--tutorial", self.private / "inputs" / "t1t7_count_v1.jsonl",
        )

    def untouched(self) -> bool:
        """Nothing issued and nothing written: what every refusal before the ids step must leave."""
        return self.issued_ids() == [] and not self.out_dir.exists()


@pytest.fixture
def rig(world, eval_registry) -> Rig:
    r = Rig(world, eval_registry)
    r.build()
    return r


# --- the register policy ---------------------------------------------------------------------------------------------
def test_the_real_defaults_are_70_and_35_at_105_families():
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    share = prep.registered_share(prereg)
    assert share == Fraction(1, 2)
    assert prep.lay_policy(105, share) == (35, 70)  # (lay_min, lay_or_terse_max) at today's real count


@pytest.mark.parametrize("count,share,expected", [
    (105, Fraction(1, 2), (35, 70)),
    (106, Fraction(1, 2), (36, 71)),  # ceil(212/3) = 71, then ceil(71/2) = 36
    (3, Fraction(1, 2), (1, 2)),
    (1, Fraction(1, 2), (1, 1)),
    (105, Fraction(11, 20), (39, 70)),  # ceil(0.55 * 70) = ceil(38.5)
    (105, Fraction(1), (70, 70)),
])
def test_lay_policy_arithmetic(count, share, expected):
    assert prep.lay_policy(count, share) == expected


def test_lay_policy_override_and_its_bounds():
    assert prep.lay_policy(105, Fraction(1, 2), 60) == (30, 60)
    assert prep.lay_policy(105, Fraction(1, 2), 105) == (53, 105)  # ceil(52.5)
    assert prep.lay_policy(105, Fraction(1, 2), 1) == (1, 1)
    for bad in (0, -1, 106, True):
        with pytest.raises(st.Stop) as info:
            prep.lay_policy(105, Fraction(1, 2), bad)
        assert info.value.reason == "usage"
    for count, share in ((0, Fraction(1, 2)), (10, Fraction(0)), (10, Fraction(3, 2)), (True, Fraction(1, 2))):
        with pytest.raises(st.Stop):
            prep.lay_policy(count, share)


@pytest.mark.parametrize("share", [Fraction(1, 2), Fraction(11, 20), Fraction(1, 3), Fraction(2, 3)])
def test_the_share_holds_for_every_mix_the_sp_may_pick(share):
    """Within both bounds, lay / (lay or terse) >= share for ALL pairs (lay, lay-or-terse) the two numbers allow."""
    for count in (1, 2, 3, 7, 40, 105, 110):
        for override in (None, 1, max(1, count // 2), count):
            lay_min, lay_max = prep.lay_policy(count, share, override)
            assert 0 < lay_min <= lay_max <= count
            for lay_or_terse in range(lay_min, lay_max + 1):  # fewer lay-or-terse drafts than lay_min cannot hold lay_min lay ones
                for lay in range(lay_min, lay_or_terse + 1):
                    assert Fraction(lay, lay_or_terse) >= share, (count, override, lay, lay_or_terse)


def test_the_share_is_read_exactly_from_the_prereg():
    prereg = {"drafting_rule": {"attempt_1": {"realistic_lay_min_share": 0.55}}}
    assert prep.registered_share(prereg) == Fraction(11, 20)  # not the binary float 0.55000000000000004...
    for bad in ({}, {"drafting_rule": {}}, {"drafting_rule": {"attempt_1": {}}}):
        with pytest.raises(st.Stop) as info:
            prep.registered_share(bad)
        assert info.value.reason == "prereg_missing"
    for bad_value in (True, "0.5", 0, 1.5, float("nan")):
        with pytest.raises(st.Stop) as info:
            prep.registered_share({"drafting_rule": {"attempt_1": {"realistic_lay_min_share": bad_value}}})
        assert info.value.reason == "prereg_invalid"


# --- counts and the task ---------------------------------------------------------------------------------------------
def test_the_counts_are_the_ones_count_migrated_computes(rig):
    code, want_out, err = rig.count_migrated()
    assert code == 0, err
    want = rig.world.last(want_out, "count_migrated")
    out = rig.ok()
    counts = rig.line(out, "counts")
    assert counts["drafts"] == want["drafts"] == "105" and counts["refusal_drafts"] == want["refusal_drafts"]
    assert int(want["refusal_drafts"]) > 0
    task = rig.task()
    assert task["count"] == int(want["drafts"]) and task["refusals"] == int(want["refusal_drafts"])
    for key in prep.COUNT_FIELDS:
        assert counts[key] == want[key], key


def test_the_counts_follow_the_inputs(world, eval_registry):
    r = Rig(world, eval_registry)
    r.build(gold=["direct"] * 8 + ["refusal"] * 6, real=["direct"] * 4 + ["refusal"] * 2, tutorial=3)
    code, want_out, err = r.count_migrated()
    assert code == 0, err
    want = world.last(want_out, "count_migrated")
    r.ok()
    assert (r.task()["count"], r.task()["refusals"]) == (int(want["drafts"]), int(want["refusal_drafts"]))
    assert len(r.ids()) == int(want["drafts"])


def test_task_json_is_canonical_bytes(rig):
    code, want_out, err = rig.count_migrated()
    refusals = int(rig.world.last(want_out, "count_migrated")["refusal_drafts"])
    out = rig.ok()
    raw = (rig.out_dir / "task.json").read_bytes()
    assert raw == b'{"count":105,"kind":"draft","lay_min":35,"lay_or_terse_max":70,"refusals":%d}\n' % refusals
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert rig.line(out, "task") == {
        "count": "105", "refusals": str(refusals), "lay_min": "35", "lay_or_terse_max": "70", "sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_the_lay_override_reaches_the_task(rig):
    out = rig.ok("--lay-or-terse-max", "60")
    assert (rig.task()["lay_or_terse_max"], rig.task()["lay_min"]) == (60, 30)
    assert rig.line(out, "lay_policy") == {"share_num": "1", "share_den": "2", "lay_or_terse_max": "60", "lay_min": "30", "override": "yes"}
    again = rig.ok()  # same directory, no override: task.json is rewritten with the defaults, the ids are reused
    assert rig.line(again, "lay_policy")["override"] == "no" and (rig.task()["lay_or_terse_max"], rig.task()["lay_min"]) == (70, 35)


@pytest.mark.parametrize("bad", ["0", "-3", "106", "abc"])
def test_a_bad_lay_override_is_refused_before_anything_is_issued(rig, bad):
    code, out, err = rig.run("--lay-or-terse-max", bad)
    assert code == st.EXIT_USAGE and "reason=usage" in err
    assert rig.untouched()


def test_an_unreachable_band_stops_with_count_migrateds_reason_and_aggregate_line(world, eval_registry):
    r = Rig(world, eval_registry)
    r.build(real=["refusal"] * 30)  # the realistic split's preassigned refusals exceed what its band can hold
    want_code, want_out, want_err = r.count_migrated()
    assert want_code == st.EXIT_REFUSED and "reason=band_unreachable" in want_err
    code, out, err = r.run()
    assert code == want_code and "reason=band_unreachable" in err
    assert out == want_out  # the same aggregate line, forwarded, and nothing else printed
    assert r.untouched()


def test_a_plan_with_more_refusals_than_families_is_refused(rig, monkeypatch):
    monkeypatch.setattr(st, "cmd_count_migrated", lambda ctx, *a: ctx.printer.emit("count_migrated", drafts=10, refusal_drafts=11))
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=band_unreachable" in err and rig.untouched()


def test_a_count_migrated_line_that_is_missing_is_an_internal_error(rig, monkeypatch):
    monkeypatch.setattr(st, "cmd_count_migrated", lambda ctx, *a: 0)
    code, out, err = rig.run()
    assert code == st.EXIT_FAIL and err == "error reason=internal\n" and rig.untouched()


# --- ids ---------------------------------------------------------------------------------------------------------------
def test_ids_are_issued_once_and_recorded(rig):
    out = rig.ok()
    ids = rig.ids()
    assert len(ids) == 105 == len(set(ids)) and all(sp.FAMILY_ID_RE.fullmatch(i) for i in ids)
    assert (rig.out_dir / "ids.txt").read_text(encoding="utf-8") == "\n".join(ids) + "\n"
    assert rig.issued_ids() == ids  # recorded as issued, the way `sealed_tool ids` does it
    assert rig.line(out, "ids") == {"count": "105", "reused": "no", "sha256": sha(rig.out_dir / "ids.txt")}


def test_ids_never_collide_with_ids_the_private_root_already_holds(rig, monkeypatch):
    taken = ["f%08x" % n for n in range(1, 4)]
    (rig.private / "dev").mkdir(parents=True, exist_ok=True)
    (rig.private / "dev" / "held.jsonl").write_text("".join(json.dumps({"family_id": t}) + "\n" for t in taken), encoding="utf-8")
    seq = iter(taken + ["f%08x" % n for n in range(100, 300)])
    monkeypatch.setattr(st, "new_family_id", lambda: next(seq))
    rig.ok()
    assert not set(taken) & set(rig.ids()) and len(rig.ids()) == 105


def test_a_rerun_reuses_the_ids_and_rewrites_the_same_tree(rig):
    first = rig.ok()
    tree, issued = digest_tree(rig.out_dir), rig.issued_ids()
    second = rig.ok()
    assert digest_tree(rig.out_dir) == tree and rig.issued_ids() == issued and len(issued) == 105
    assert rig.line(first, "ids")["reused"] == "no" and rig.line(second, "ids")["reused"] == "yes"
    assert second.replace("reused=yes", "reused=no") == first  # everything else, the command included, is identical


@pytest.mark.parametrize("damage,reason", [
    (lambda ids: ids[:-1], "count_mismatch"),  # one short
    (lambda ids: ids + ["f%08x" % 0xABCDEF], "count_mismatch"),  # one extra
    (lambda ids: ids[:-1] + ["f%08x" % 0xABCDEF], "input_invalid"),  # the right count, one never issued
    (lambda ids: ids[:-1] + [ids[0]], "input_invalid"),  # a repeat
    (lambda ids: ids[:-1] + ["not-an-id"], "input_invalid"),  # malformed
    (lambda ids: [i.upper() for i in ids], "input_invalid"),
])
def test_a_mismatched_existing_ids_file_is_refused_and_nothing_is_issued_or_written(rig, damage, reason):
    rig.ok()
    ids, issued = rig.ids(), rig.issued_ids()
    (rig.out_dir / "ids.txt").write_text("\n".join(damage(ids)) + "\n", encoding="utf-8")
    (rig.out_dir / "task.json").unlink()
    before = digest_tree(rig.out_dir)
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and f"reason={reason}" in err and "sp_launch" not in out
    assert digest_tree(rig.out_dir) == before and not (rig.out_dir / "task.json").exists()
    assert rig.issued_ids() == issued


def test_ids_without_a_trailing_newline_or_as_a_symlink_are_refused(rig):
    rig.ok()
    ids, path = rig.ids(), rig.out_dir / "ids.txt"
    path.write_text("\n".join(ids), encoding="utf-8")  # no final newline: not the bytes `ids` writes
    assert rig.run()[0] == st.EXIT_REFUSED
    real = rig.out_dir.parent / "real_ids.txt"
    real.write_text("\n".join(ids) + "\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(real)
    code, _, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=input_invalid" in err


def test_ids_issued_but_not_recorded_are_refused(rig):
    rig.ok()
    rig.issued.unlink()  # a crash between writing ids.txt and recording it: never reuse ids nobody recorded
    code, _, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=input_invalid" in err


# --- the tree ----------------------------------------------------------------------------------------------------------
def test_the_tree_holds_the_task_the_ids_and_byte_copies(rig):
    rig.ok()
    assert sorted(digest_tree(rig.out_dir)) == sorted(["task.json", "ids.txt", "inventory.json", "handbook_sections.jsonl", f"tutorials/{MANUAL}"])
    assert (rig.out_dir / "inventory.json").read_bytes() == rig.inventory.read_bytes()
    assert (rig.out_dir / "handbook_sections.jsonl").read_bytes() == rig.dump.read_bytes()
    assert (rig.out_dir / "tutorials" / MANUAL).read_bytes() == (rig.tutorials / MANUAL).read_bytes()  # incl. the NUL and 0xff bytes
    assert not any(name.endswith(".tmp") for _, _, names in os.walk(rig.out_dir) for name in names)


def test_the_digests_are_those_of_the_inputs(rig):
    out = rig.ok()
    digests = {ln.split(" ")[1].split("=")[1]: ln.split(" ")[2].split("=")[1] for ln in out.splitlines() if ln.startswith("digest ")}
    assert digests == {
        "inventory": sha(rig.inventory), "handbook_sections": sha(rig.dump), "tutorial_manual": sha(rig.tutorials / MANUAL),
        "brief": sha(rig.brief), "prereg": sha(rig.world.prereg_path),
    }


def test_the_manual_is_selected_by_name_and_the_exercise_sheets_never_are(rig):
    (rig.tutorials / f"Copy of {MANUAL}").write_bytes(b"x")  # contains the prefix but does not start with it
    (rig.tutorials / (prep.MANUAL_PREFIX + " folder")).mkdir()  # starts with it but is not a file
    rig.ok()
    assert os.listdir(rig.out_dir / "tutorials") == [MANUAL]
    assert prep.select_manual(rig.repo) == rig.tutorials / MANUAL
    assert not any(sheet in os.listdir(rig.out_dir / "tutorials") for sheet in SHEETS)


def test_select_manual_refuses_none_and_two(rig):
    (rig.tutorials / MANUAL).unlink()
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_missing" in err and rig.untouched()
    (rig.tutorials / (prep.MANUAL_PREFIX + " A.pdf")).write_bytes(b"a")
    assert rig.run()[0] == 0  # exactly one again (the name is free after the prefix)
    shutil.rmtree(rig.out_dir)
    (rig.tutorials / (prep.MANUAL_PREFIX + " B.pdf")).write_bytes(b"b")
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err and not rig.out_dir.exists()
    shutil.rmtree(rig.tutorials)  # no folder at all
    assert "reason=input_missing" in rig.run()[2]


def test_only_the_exercise_sheets_in_the_folder_is_a_refusal(world, eval_registry):
    r = Rig(world, eval_registry)
    r.build(manual=False)
    code, out, err = r.run()
    assert code == st.EXIT_USAGE and "reason=input_missing" in err and r.untouched()


def test_a_missing_dump_is_refused_before_ids_are_issued(world, eval_registry):
    r = Rig(world, eval_registry)
    r.build(dump=False)
    code, out, err = r.run()
    assert code == st.EXIT_USAGE and "reason=dump_missing" in err and "sp_launch" not in out
    assert r.untouched()


def test_a_missing_inventory_or_brief_is_refused_before_ids_are_issued(rig):
    rig.inventory.unlink()
    assert "reason=input_missing" in rig.run()[2] and rig.untouched()
    rig.inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
    rig.brief.unlink()
    assert "reason=input_missing" in rig.run()[2] and rig.untouched()


def test_a_stray_file_in_tutorials_or_a_file_for_out_is_refused(rig):
    rig.ok()
    (rig.out_dir / "tutorials" / "stray.pdf").write_bytes(b"x")
    code, _, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=input_invalid" in err
    (rig.out_dir / "tutorials" / "stray.pdf").unlink()
    assert rig.run()[0] == 0
    elsewhere = rig.private / "work" / "b3" / "afile"
    elsewhere.write_text("x", encoding="utf-8")
    assert "reason=input_invalid" in rig.run("--out", str(elsewhere))[2]


def test_out_must_lie_under_the_private_root_outside_sealed(rig, tmp_path):
    for bad in (tmp_path / "elsewhere", rig.private, rig.private / "sealed" / "x", rig.private / "sealed", rig.private / ".." / "outside"):
        code, out, err = rig.run("--out", str(bad))
        assert code == st.EXIT_USAGE and "reason=path_outside" in err, bad
    assert rig.issued_ids() == [] and not (tmp_path / "elsewhere").exists() and not (rig.private / "sealed" / "x").exists()
    link = rig.private / "work" / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(tmp_path)
    assert "reason=path_outside" in rig.run("--out", str(link))[2]  # a symlink is refused, and so is a path through one
    assert "reason=path_outside" in rig.run("--out", str(link / "draft"))[2]
    custom = rig.private / "work" / "b3" / "custom"
    command = rig.command(rig.ok("--out", str(custom)))
    assert (custom / "task.json").is_file() and str(custom) in command


def test_a_case_variant_spelling_of_sealed_is_refused(rig, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("a case-sensitive file system: a differently cased path is a different directory")
    rig.ok()  # the first run creates sealed/work (the lock and the issued ids), so the directory exists
    code, out, err = rig.run("--out", str(rig.private / "Sealed" / "x"))
    assert code == st.EXIT_USAGE and "reason=path_outside" in err and not (rig.private / "sealed" / "x").exists()
    assert "reason=path_outside" in rig.run("--out", str(rig.private / "SEALED"))[2]
    assert "reason=path_outside" in rig.run("--out", str(rig.private.parent / "PRIVATE"))[2]  # the private root itself, spelled differently


# --- the prereg ----------------------------------------------------------------------------------------------------------
def test_an_uncommitted_or_modified_prereg_is_refused_before_ids_are_issued(world, eval_registry):
    r = Rig(world, eval_registry)
    r.build(commit=False)
    code, out, err = r.run()
    assert code == st.EXIT_REFUSED and "reason=prereg_uncommitted" in err and r.untouched()
    world.commit_prereg(push=False)
    assert r.run()[0] == 0
    shutil.rmtree(r.out_dir)
    with open(world.prereg_path, "a", encoding="utf-8") as fh:  # still valid JSON, no longer the committed bytes
        fh.write("\n")
    before = r.issued_ids()
    code, out, err = r.run()
    assert code == st.EXIT_REFUSED and "reason=prereg_modified" in err and r.issued_ids() == before and not r.out_dir.exists()


def test_an_invalid_or_missing_prereg_is_refused(rig):
    doc = rig.world.prereg_doc()
    doc["mwe"] = 0.99  # a registered value changed
    rig.world.prereg_path.write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = rig.run()
    assert code != 0 and "reason=prereg_invalid" in err and rig.untouched()
    rig.world.prereg_path.unlink()
    assert "reason=prereg_missing" in rig.run()[2] and rig.untouched()


def test_the_launchers_own_prereg_check_is_run(rig, monkeypatch):
    def refuse(path):
        raise sp.LaunchError(sp.EXIT_REFUSED, "prereg_invalid")

    monkeypatch.setattr(sp, "check_prereg_valid", refuse)
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=prereg_invalid" in err and rig.untouched()


# --- the command ---------------------------------------------------------------------------------------------------------
def parse_command(command: str) -> Any:
    words = shlex.split(command)
    assert words[:5] == ["env", "-u", "ANTHROPIC_API_KEY", ".venv/bin/python", "scripts/sp_launch.py"]
    return sp._parser().parse_args(words[5:])


def test_the_command_is_one_line_that_the_launchers_parser_accepts(rig):
    out = rig.ok()
    command = rig.command(out)
    assert "\n" not in command and out.endswith(command + "\n")
    args = parse_command(command)
    out_dir = str(rig.out_dir)
    assert args.kind == "data" and args.task == f"{out_dir}/task.json" and args.brief == prep.BRIEF_RELATIVE
    assert args.inputs == [f"{out_dir}/{n}" for n in ("task.json", "ids.txt", "inventory.json", "handbook_sections.jsonl", "tutorials")]
    assert args.prereg == rig.prereg_commit and sp.SHA_RE.fullmatch(args.prereg)  # the last commit that touched the prereg
    assert args.branch == json.loads(rig.world.prereg_path.read_text(encoding="utf-8"))["branch"]  # the prereg's own branch
    assert args.timeout == 21600 and args.sweep is False
    assert sorted(args.enums) == sorted(prep.DRAFT_ENUMS)
    names = [Path(p).name for p in args.inputs]  # the basenames the launcher stages: distinct, none reserved
    assert len(set(names)) == len(names) and not set(names) & sp.RESERVED_STAGING_NAMES
    assert len(sp._validate_inputs(args.inputs)) == 5  # all exist, no symlink anywhere in the tree


def test_the_command_uses_repo_relative_paths_when_out_is_under_the_repo(rig):
    private = rig.repo / "eval" / "private"  # the production layout: the private root inside the repo
    (private / "inputs").mkdir(parents=True)
    shutil.copyfile(rig.private / "inputs" / "t1t7_count_v1.jsonl", private / "inputs" / "t1t7_count_v1.jsonl")
    command = rig.command(rig.ok("--private-root", str(private)))
    assert (private / "work" / "b3" / "draft1" / "task.json").is_file()
    assert " --task eval/private/work/b3/draft1/task.json " in command and str(rig.repo) not in command
    assert parse_command(command).inputs[-1] == "eval/private/work/b3/draft1/tutorials"


def test_the_timeout_option(rig):
    for bad in ("0", "-5", str(sp.MAX_TIMEOUT + 1), "x"):
        code, out, err = rig.run("--timeout", bad)
        assert code == st.EXIT_USAGE and "reason=usage" in err
    assert rig.untouched()
    assert parse_command(rig.command(rig.ok("--timeout", "7200"))).timeout == 7200


def test_the_enum_words_cover_every_draft_and_blocked_return():
    draft = {
        "status": "ok", "kind": "draft", "families": 105, "rows": 105,
        "by_scope": {"answer": 50, "partial": 20, "refuse": 35},
        "by_register": {"lay": 40, "modelled": 35, "terse": 30},
        "by_source": {"tutorial": 50, "handbook": 55}, "sha256": "ab" * 32,
    }
    assert sp.validate_return(draft, prep.DRAFT_ENUMS)
    for reason in ("inputs_missing", "task_unclear", "quota_unreachable"):
        assert sp.validate_return({"status": "blocked", "reason": reason}, prep.DRAFT_ENUMS)
    assert not sp.validate_return({"status": "ok", "kind": "twins"}, prep.DRAFT_ENUMS)  # another task's word
    assert not sp.validate_return({"status": "ok", "note": "free text"}, prep.DRAFT_ENUMS)
    assert not sp.validate_return(draft)  # without --enum the launcher would call every return invalid
    assert set(prep.DRAFT_ENUMS) <= set(st.RETURN_ENUMS)  # `sealed_tool return` accepts the same words
    assert len(set(prep.DRAFT_ENUMS)) == len(prep.DRAFT_ENUMS)


def test_the_enum_words_are_the_ones_the_brief_names():
    text = BRIEF.read_text(encoding="utf-8")
    draft_section = text.split("### `draft`", 1)[1].split("### `twins`", 1)[0]
    block = draft_section.split("`return.json`:", 1)[1].split("```", 2)[1]
    in_example = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block))
    blocked = re.search(r'\{"status": "blocked", "reason": "<one enum: ([a-z_ |]+)>"\}', text)
    assert blocked, "the brief's blocked return changed"
    reasons = {w.strip() for w in blocked.group(1).split("|")}
    assert reasons == {"inputs_missing", "task_unclear", "quota_unreachable"}
    assert in_example | reasons | {"status", "blocked", "reason"} <= set(prep.DRAFT_ENUMS)
    assert {"lay", "modelled", "terse", "tutorial", "handbook"} <= set(prep.DRAFT_ENUMS)  # the row format's register and source words
    normalised = " ".join(text.split())
    for word in prep.DRAFT_ENUMS:
        assert word in normalised, word  # nothing in the list is a word the brief never uses


# --- the brief -----------------------------------------------------------------------------------------------------------
def test_the_brief_states_the_lay_rule_with_both_bounds():
    text = " ".join(BRIEF.read_text(encoding="utf-8").split())
    assert (
        "Among your `lay` or `terse` drafts, at least `task.json:lay_min` must be `lay`, and no more than "
        "`task.json:lay_or_terse_max` drafts may be `lay` or `terse`."
    ) in text
    keys = text.split("`task.json` carries these keys:", 1)[1].split("Follow these rules:", 1)[0]
    for key in ("`count`", "`refusals`", "`lay_min`", "`lay_or_terse_max`", "`allowed_sources`"):
        assert key in keys, key


# --- what the tool prints and does ----------------------------------------------------------------------------------------
def test_stdout_holds_only_counts_digests_enums_and_the_command(rig):
    out = rig.ok()
    command = rig.command(out)
    body = [ln for ln in out.splitlines() if ln != command]
    assert body and all(LINE.fullmatch(ln) for ln in body), body
    assert {ln.split(" ")[0] for ln in body} == {"counts", "lay_policy", "ids", "task", "digest"}
    assert CANARY not in out and MANUAL not in out and "question" not in out
    for ln in body:
        for key, value in (part.split("=", 1) for part in ln.split(" ")[1:]):
            known = value.isdigit() or re.fullmatch(r"[0-9a-f]{64}", value) or value in st.ENUM_WORDS or (key == "name" and value in prep.INPUT_NAMES)
            assert known, (ln, value)
    assert command.startswith("env -u ANTHROPIC_API_KEY .venv/bin/python scripts/sp_launch.py --kind data ")


def test_a_refusal_prints_only_a_fixed_reason_and_never_a_path_or_text(rig):
    (rig.tutorials / MANUAL).unlink()
    (rig.tutorials / (prep.MANUAL_PREFIX + " B.pdf")).write_bytes(b"b")
    (rig.tutorials / (prep.MANUAL_PREFIX + " A.pdf")).write_bytes(b"a")
    code, out, err = rig.run()
    assert code != 0 and re.fullmatch(r"error reason=[a-z_]+\n", err), err
    assert CANARY not in out + err and "Tutorial" not in out + err and str(rig.repo) not in out + err


def test_an_internal_error_prints_no_exception_text(rig, monkeypatch):
    monkeypatch.setattr(prep, "draft_counts", lambda ctx: (_ for _ in ()).throw(RuntimeError(f"secret {CANARY} path")))
    code, out, err = rig.run()
    assert code == st.EXIT_FAIL and err == "error reason=internal\n" and CANARY not in out + err


def test_a_held_state_lock_refuses_the_run(rig):
    lock_dir = rig.private / "sealed" / "work"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / st.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out, err = rig.run()
        assert code == st.EXIT_REFUSED and "reason=locked" in err and not rig.out_dir.exists()
    finally:
        os.close(fd)
    assert rig.run()[0] == 0


def test_the_prep_never_starts_the_launcher(rig, monkeypatch):
    def no(*args, **kwargs):
        raise AssertionError("the prep must not dispatch")

    for name in ("launch", "_dispatch", "_run_sp", "main", "sweep_only"):
        monkeypatch.setattr(sp, name, no)
    rig.ok()


def test_copy_bytes_refuses_a_copy_that_differs_and_leaves_no_temp(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"abc\x00")
    assert prep.copy_bytes(src, dst) == hashlib.sha256(b"abc\x00").hexdigest() and dst.read_bytes() == b"abc\x00"

    def corrupt(a, b):
        Path(b).write_bytes(b"corrupt")

    monkeypatch.setattr(shutil, "copyfile", corrupt)
    dst2 = tmp_path / "dst2.bin"
    with pytest.raises(st.Stop):
        prep.copy_bytes(src, dst2)
    assert not dst2.exists() and sorted(os.listdir(tmp_path)) == ["dst.bin", "src.bin"]


def test_copy_bytes_replaces_a_symlink_instead_of_writing_through_it(tmp_path):
    src, target, dst = tmp_path / "src.bin", tmp_path / "target.bin", tmp_path / "dst.bin"
    src.write_bytes(b"new")
    target.write_bytes(b"old")
    dst.symlink_to(target)
    prep.copy_bytes(src, dst)
    assert not dst.is_symlink() and dst.read_bytes() == b"new" and target.read_bytes() == b"old"
