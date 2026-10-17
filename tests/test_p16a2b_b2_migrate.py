"""16A-2b b2: ``scripts/migrate_dev_v2.py``, the tool that writes the v2 development rows (private, gitignored).

Synthetic fixtures only: invented questions (each carries a canary), invented section labels, a tmp private root (``tests/conftest.py``
points ``eval_privacy.private_root`` at ``tmp_path/eval/private``) and tmp tutorial candidate files. Nothing reads ``data/`` or a real set.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from scripts import migrate_dev_v2 as mig
from scripts import sealed_tool as st
from src import eval_schema
from src.eval_privacy import public_v1_id
from tests.p16w_sealed_world import CANARY, INVENTORY, read_rows
from tests.test_p16w_sealed_tool import assert_allowlisted

V1Row = Tuple[str, str, Optional[List[str]]]  # question, type, expected_sections
Block = Tuple[str, Optional[str], List[V1Row]]  # candidate id, scope word (None: no ### block, like a *-RV variant), phrasings
CAND_LINE = re.compile(r"^unresolved candidate=T[0-9]{2}-[A-Za-z]{1,3}(-RV)?$")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Rig:
    """The inputs of one migration, written under ``tmp_path``, and a ``run`` that calls ``migrate_dev_v2.main``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eval_registry: Any) -> None:
        self.tmp = tmp_path
        self.registry = eval_registry
        self.private = tmp_path / "eval" / "private"
        self.dev = self.private / "dev"
        self.inputs = self.private / "inputs"
        self.work = self.private / "work" / "b2"
        self.inventory = tmp_path / "_registry" / "section_inventory.json"
        self.inventory.parent.mkdir()
        self.inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
        monkeypatch.setattr(eval_schema, "INVENTORY_PATH", self.inventory)
        self.argv: List[str] = []

    # -- inputs ----------------------------------------------------------------------------
    def v1(self, name: str, rows: Sequence[V1Row]) -> Path:
        path = self.tmp / "public" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(
            json.dumps({"question": q, "type": t, "expected_sections": [] if t == "refusal" else sections}) + "\n"
            for q, t, sections in rows), encoding="utf-8")
        self.registry.add(path, privacy="public", name=name)
        return path

    @staticmethod
    def pid(question: str) -> str:
        return public_v1_id(question)

    def pairing(self, pairs: Sequence[Tuple[str, str]]) -> Path:
        path = self.tmp / "pairing.json"
        path.write_text(json.dumps({"pairs": [[self.pid(g), self.pid(r)] for g, r in pairs]}), encoding="utf-8")
        return path

    def candidates(self, name: str, blocks: Sequence[Block]) -> Path:
        """A tutorial candidate file: a ``### <id>`` block with a ``- **scope:**`` line per block, then the embedded JSON lines."""
        head, lines = ["# synthetic candidates", ""], ["```jsonl"]
        for cid, scope, rows in blocks:
            if scope is not None:
                head += [f"### {cid}", f"- **scope:** {scope}  ·  **source:** synthetic", ""]
            for q, t, sections in rows:
                lines.append(json.dumps({"question": q, "expected_sections": [] if t == "refusal" else sections, "type": t,
                                         "candidate_id": cid, "split": "tuning"}))
        path = self.tmp / "tutorials" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(head + lines + ["```", ""]), encoding="utf-8")
        return path

    def gaps(self, gaps: Dict[str, Any], unresolved: Optional[Dict[str, str]] = None, **extra: Any) -> Path:
        self.work.mkdir(parents=True, exist_ok=True)
        path = self.work / "t1t7_gaps.json"
        path.write_text(json.dumps({"version": 1, "gaps": gaps, "unresolved": unresolved or {}, **extra}), encoding="utf-8")
        return path

    def labels(self, *, scope: Optional[Dict[str, str]] = None, phrasings: Optional[Dict[str, List[int]]] = None,
               flags: Optional[Dict[str, str]] = None, **extra: Any) -> Path:
        """The owner's labels file at its default path (nothing labelled by default)."""
        self.work.mkdir(parents=True, exist_ok=True)
        path = self.work / "t1t7_labels.json"
        doc = {"version": 1, "provenance": f"{CANARY} synthetic", "scope": scope or {}, "phrasings": phrasings or {}, "flags": flags or {}, **extra}
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    # -- running ----------------------------------------------------------------------------
    def configure(self, golden: Path, realistic: Path, pairing: Path, tutorials: Sequence[Path]) -> None:
        self.argv = ["--golden", str(golden), "--realistic", str(realistic), "--pairing", str(pairing), "--inventory", str(self.inventory)]
        for t in tutorials:
            self.argv += ["--tutorial", str(t)]

    def run(self, *extra: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = mig.main([*self.argv, *extra], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def fields(self, text: str, kind: str) -> Dict[str, str]:
        hits = [line for line in text.splitlines() if line.startswith(kind + " ") or line == kind]
        assert hits, f"no {kind!r} line in {text!r}"
        return dict(p.split("=", 1) for p in hits[-1].split(" ")[1:])

    def files(self) -> Dict[str, bytes]:
        """Every file under the private root (relative path -> bytes), except the lock file every state-changing command makes."""
        return {str(p.relative_to(self.private)): p.read_bytes() for p in sorted(self.private.rglob("*")) if p.is_file() and p.name != st.LOCK_NAME}


Q = lambda tag: f"{CANARY} question {tag}"  # noqa: E731  (every question carries the canary and a unique tag)

GOLDEN: List[V1Row] = [
    (Q("g0"), "refusal", None), (Q("g1"), "direct", ["1.1", "1.2"]), (Q("g2"), "direct", ["2.1"]),
    (Q("g3"), "direct", ["3.1"]), (Q("g4"), "exact_token", ["4.1"]),
]
REALISTIC: List[V1Row] = [
    (Q("r0"), "direct", ["5.1"]), (Q("r1"), "direct", ["1.3"]), (Q("r2"), "direct", ["2.2"]), (Q("r3"), "refusal", None),
]
PAIRS = [(GOLDEN[1][0], REALISTIC[1][0]), (GOLDEN[2][0], REALISTIC[2][0])]
T01: List[Block] = [
    ("T01-A", "PARTIAL", [(Q(f"a{i}"), "direct", ["6.1", "6.2"]) for i in range(4)]),
    ("T01-B", "IN", [(Q(f"b{i}"), "direct", ["7.1"]) for i in range(3)]),
    ("T01-C", "OUT", [(Q(f"c{i}"), "refusal", None) for i in range(3)]),
]
T07: List[Block] = [
    ("T07-Aa", "IN", [(Q(f"x{i}"), "direct", ["9.1", "9.2", "9.3"]) for i in range(2)]),
    ("T07-Bb", "PARTIAL", [(Q(f"y{i}"), "direct", ["8.1"]) for i in range(3)]),
    ("T07-Bb-RV", None, [(Q("rv"), "refusal", None)]),
]
GAPS = {"T01-A": [{"id": "ga", "keywords": ["alpha kw", "alpha alt"]}, {"id": "gb", "keywords": ["beta kw"]}],
        "T07-Bb": [{"id": "gc", "keywords": ["gamma kw"]}]}


@pytest.fixture
def rig(tmp_path, monkeypatch, eval_registry) -> Rig:
    r = Rig(tmp_path, monkeypatch, eval_registry)
    r.configure(r.v1("golden.jsonl", GOLDEN), r.v1("realistic.jsonl", REALISTIC), r.pairing(PAIRS),
                [r.candidates("tutorial01_candidates.md", T01), r.candidates("tutorial07_candidates.md", T07)])
    r.gaps(GAPS)
    r.labels()
    return r


def by_family(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in sorted(rows, key=st.row_key):
        out.setdefault(row["family_id"], []).append(row)
    return out


def family_of(rows: Sequence[Dict[str, Any]], first_question: str) -> List[Dict[str, Any]]:
    """The family whose row 0 is ``first_question``."""
    hits = [f for f in by_family(rows).values() if f[0]["question"] == first_question]
    assert len(hits) == 1
    return hits[0]


def migrate(rig: Rig, *extra: str):
    code, out, err = rig.run(*extra)
    assert code == 0, (out, err)
    return read_rows(rig.dev / "tuning_migrated.jsonl"), read_rows(rig.dev / "realistic_migrated.jsonl"), out


# --- the families ------------------------------------------------------------------------------------------------------
def test_golden_only_families_go_to_tuning_and_unpaired_realistic_ones_to_realistic(rig):
    tuning, realistic, out = migrate(rig)
    fams = by_family(tuning)
    assert len(fams) == 3 and all(len(f) == 1 for f in fams.values())  # g0, g3, g4: golden rows that are in no pair
    assert {f[0]["question"] for f in fams.values()} == {GOLDEN[0][0], GOLDEN[3][0], GOLDEN[4][0]}
    for row in tuning:
        assert row["schema"] == 2 and row["register"] == "modelled" and row["source"] == "legacy" and row["id"] == row["family_id"] + "-0"
        assert re.fullmatch(r"f[0-9a-f]{8}", row["family_id"])
    g4 = family_of(tuning, GOLDEN[4][0])[0]
    assert (g4["scope"], g4["type"], g4["evidence"]) == ("answer", "exact_token", [["4.1"]]) and "owner_status" not in g4 and "gaps" not in g4
    g0 = family_of(tuning, GOLDEN[0][0])[0]
    assert (g0["scope"], g0["evidence"], g0["type"], g0["owner_status"]) == ("refuse", [], "refusal", "draft")  # a legacy refusal is a draft
    r0 = family_of(realistic, REALISTIC[0][0])
    assert len(r0) == 1 and (r0[0]["register"], r0[0]["source"], r0[0]["evidence"]) == ("lay", "legacy", [["5.1"]]) and "owner_status" not in r0[0]
    r3 = family_of(realistic, REALISTIC[3][0])[0]
    assert (r3["scope"], r3["owner_status"], r3["register"], r3["source"]) == ("refuse", "draft", "lay", "legacy")


def test_a_pair_is_one_realistic_family_with_the_realistic_row_first_and_each_row_keeps_its_own_evidence(rig):
    tuning, realistic, _ = migrate(rig)
    pair = family_of(realistic, REALISTIC[1][0])
    assert [r["id"] for r in pair] == [pair[0]["family_id"] + "-0", pair[0]["family_id"] + "-1"]
    assert [(r["question"], r["register"], r["source"]) for r in pair] == [(REALISTIC[1][0], "lay", "legacy"), (GOLDEN[1][0], "modelled", "legacy")]
    assert [r["evidence"] for r in pair] == [[["1.3"]], [["1.1", "1.2"]]]  # the realistic row's own, then the golden row's own
    assert {r["scope"] for r in pair} == {"answer"} and all("owner_status" not in r for r in pair)
    assert GOLDEN[1][0] not in {r["question"] for r in tuning} and GOLDEN[2][0] not in {r["question"] for r in tuning}


def test_a_tutorial_candidate_is_one_family_with_at_most_three_phrasings_in_file_order(rig):
    _, realistic, _ = migrate(rig)
    fam = family_of(realistic, Q("a0"))  # T01-A has four phrasings: the first three
    assert [r["question"] for r in fam] == [Q("a0"), Q("a1"), Q("a2")]
    assert [r["register"] for r in fam] == ["modelled", "lay", "lay"]
    assert {r["source"] for r in fam} == {"tutorial"} and {r["owner_status"] for r in fam} == {"draft"}
    assert all(Q("a3") not in r["question"] for r in realistic)
    two = family_of(realistic, Q("x0"))  # a candidate with two phrasings keeps two
    assert [r["register"] for r in two] == ["modelled", "lay"] and {r["scope"] for r in two} == {"answer"}


def test_in_out_and_partial_scopes_and_the_partial_gaps(rig):
    _, realistic, _ = migrate(rig)
    partial = family_of(realistic, Q("a0"))
    assert {r["scope"] for r in partial} == {"partial"} and {r["type"] for r in partial} == {"direct"}
    assert all(r["gaps"] == GAPS["T01-A"] and r["evidence"] == [["6.1", "6.2"]] for r in partial)  # every phrasing carries the family's gaps
    assert all(r["gaps"] == GAPS["T07-Bb"] for r in family_of(realistic, Q("y0")))
    answer = family_of(realistic, Q("b0"))
    assert {r["scope"] for r in answer} == {"answer"} and all("gaps" not in r and r["evidence"] == [["7.1"]] for r in answer)
    refuse = family_of(realistic, Q("c0"))
    assert len(refuse) == 3 and {r["scope"] for r in refuse} == {"refuse"} and all(r["evidence"] == [] and r["type"] == "refusal" for r in refuse)


def test_a_refusal_variant_is_its_own_refusal_family_of_one_modelled_row(rig):
    _, realistic, _ = migrate(rig)
    rv = family_of(realistic, Q("rv"))
    assert len(rv) == 1
    assert (rv[0]["scope"], rv[0]["register"], rv[0]["source"], rv[0]["owner_status"], rv[0]["evidence"]) == ("refuse", "modelled", "tutorial", "draft", [])
    assert rv[0]["family_id"] != family_of(realistic, Q("y0"))[0]["family_id"]  # not the family it varies


def test_the_evidence_is_what_load_any_makes_of_a_v1_row(rig, tmp_path):
    """The evidence of a migrated row is the one OR group ``load_any`` builds (stripped), and ``[]`` for a refusal. For the tutorial
    rows the same rows are written as a v1 set and read back by ``load_any``."""
    rows: List[V1Row] = [(Q("e0"), "direct", [" 1.1 ", "1.2"]), (Q("e1"), "refusal", None), (Q("e2"), "exact_token", ["3.1"])]
    path = tmp_path / "evidence_v1.jsonl"
    path.write_text("".join(json.dumps({"question": q, "type": t, "expected_sections": [] if t == "refusal" else s}) + "\n" for q, t, s in rows),
                    encoding="utf-8")
    loaded = eval_schema.load_any(path)
    rig.configure(rig.v1("g2.jsonl", rows), rig.v1("r2.jsonl", [(Q("rr"), "direct", ["1.1"])]), rig.pairing([]),
                  [rig.candidates("tutorial01_candidates.md", [("T01-A", "IN", rows[:1] + rows[2:]), ("T01-B", "OUT", [rows[1]])])])
    tuning, realistic, _ = migrate(rig)
    assert [r["evidence"] for r in sorted(tuning, key=lambda r: r["question"])] == [r["evidence"] for r in sorted(loaded, key=lambda r: r["question"])]
    cand = {r["question"]: r for r in realistic}
    for row in loaded:
        assert cand[row["question"]]["evidence"] == row["evidence"] and cand[row["question"]]["type"] == row["type"]


# --- unresolved partial families ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["listed_unresolved", "no_entry", "empty_list"])
def test_an_unresolved_partial_family_stops_the_run_naming_the_id_only_and_writes_nothing(rig, how):
    gaps = {"T07-Bb": GAPS["T07-Bb"]}
    unresolved: Dict[str, str] = {}
    if how == "listed_unresolved":
        unresolved = {"T01-A": f"{CANARY} the notes do not say what is missing"}
    elif how == "empty_list":
        gaps["T01-A"] = []
    rig.gaps(gaps, unresolved)
    before = rig.files()
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED == 8 and "reason=unresolved_partial families=1" in err
    assert [ln for ln in out.splitlines()] == ["unresolved candidate=T01-A"] and CAND_LINE.match(out.strip())
    assert CANARY not in out + err
    assert rig.files() == before and not (rig.dev.exists() and list(rig.dev.iterdir()))  # nothing written, nothing staged


def test_hold_unresolved_writes_the_family_outside_dev_and_still_counts_it(rig):
    rig.gaps({"T07-Bb": GAPS["T07-Bb"]}, {"T01-A": "unclear"})
    tuning, realistic, out = migrate(rig, "--hold-unresolved")
    assert {Q("a0"), Q("a1"), Q("a2")}.isdisjoint({r["question"] for r in realistic})  # not in dev/
    held = read_rows(rig.inputs / "held_unresolved.jsonl")
    assert [r["question"] for r in held] == [Q("a0"), Q("a1"), Q("a2")]
    assert {r["scope"] for r in held} == {"partial"} and all("gaps" not in r and r["owner_status"] == "draft" for r in held)
    assert len({r["family_id"] for r in held}) == 1 and not list(rig.dev.glob("*held*"))
    held_line = rig.fields(out, "held")
    assert (held_line["families"], held_line["rows"], held_line["partials"]) == ("1", "3", "1") and re.fullmatch(r"[0-9a-f]{64}", held_line["sha256"])
    check = rig.fields(out, "migrated_check")  # the held family is realistic's: the counts are count-migrated's, held or not
    assert (check["realistic_families"], check["realistic_refuse"]) == ("10", "3")
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))
    assert mapping["candidates"]["T01-A"]["held"] is True and mapping["candidates"]["T07-Bb"]["held"] is False
    # resolving it later (no flag) moves the same family into dev/ under the same id, and the held file goes away
    held_fid = held[0]["family_id"]
    rig.gaps(GAPS)
    _, realistic2, _ = migrate(rig)
    assert family_of(realistic2, Q("a0"))[0]["family_id"] == held_fid and not (rig.inputs / "held_unresolved.jsonl").exists()


def test_gaps_file_problems_are_invalid_input(rig):
    for bad in (
        dict(gaps={**GAPS, "T01-B": [{"id": "x", "keywords": ["k"]}]}),  # gaps for a candidate that is not PARTIAL
        dict(gaps={**GAPS, "T99-Z": [{"id": "x", "keywords": ["k"]}]}),  # an id that is not a candidate
        dict(gaps=GAPS, unresolved={"T01-A": "both"}),  # in both sections
        dict(gaps=GAPS, extra_key=1),
        dict(gaps={"T01-A": "not a list", "T07-Bb": GAPS["T07-Bb"]}),
    ):
        rig.gaps(bad.pop("gaps"), bad.pop("unresolved", None), **bad)
        code, out, err = rig.run()
        assert code == st.EXIT_USAGE and "reason=input_invalid" in err, bad
    (rig.work / "t1t7_gaps.json").write_text("[1]", encoding="utf-8")
    assert rig.run()[0] == st.EXIT_USAGE
    (rig.work / "t1t7_gaps.json").unlink()  # PARTIAL candidates but no gaps file
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_missing" in err
    assert not (rig.dev / "tuning_migrated.jsonl").exists()


def test_a_gap_that_is_not_a_schema_gap_is_a_validation_failure(rig):
    rig.gaps({**GAPS, "T01-A": [{"id": "ga", "keywords": []}]})
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=schema_invalid" in err
    assert "output_error file=realistic" in out and "field=gaps[0].keywords" in out
    assert CANARY not in out + err and not list(rig.dev.glob("*"))


# --- ids, the map, idempotence -------------------------------------------------------------------------------------------------------
def test_a_rerun_reuses_the_ids_and_writes_byte_identical_files(rig):
    migrate(rig)
    first = rig.files()
    assert {"dev/tuning_migrated.jsonl", "dev/realistic_migrated.jsonl", "work/b2/migration_map.json"} <= set(first)
    code, out, err = rig.run()
    assert code == 0, err
    second = rig.files()
    assert second == first  # every file under the private root, byte for byte (the issued list included)
    line = rig.fields(out, "migration_map")
    assert (line["ids_minted"], line["ids_reused"]) == ("0", "13")  # 3 tuning + 10 realistic families
    assert not list(rig.dev.glob(".migrate_stage_*")) and not [p for p in rig.private.rglob("*.tmp")]


def test_the_map_names_every_v1_id_and_candidate_with_its_family_and_row_ids(rig):
    tuning, realistic, out = migrate(rig)
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))
    assert mapping["version"] == 1 and len(mapping["v1"]) == 5 + 4 and len(mapping["candidates"]) == 6  # golden + realistic ids; 6 candidates
    assert mapping["labels_sha256"] == sha(rig.work / "t1t7_labels.json") and set(mapping) == {"version", "labels_sha256", "v1", "candidates"}
    g3 = family_of(tuning, GOLDEN[3][0])[0]
    assert mapping["v1"][rig.pid(GOLDEN[3][0])] == {"family_id": g3["family_id"], "row_id": g3["id"], "split": "tuning"}
    pair = family_of(realistic, REALISTIC[1][0])
    assert mapping["v1"][rig.pid(REALISTIC[1][0])] == {"family_id": pair[0]["family_id"], "row_id": pair[0]["id"], "split": "realistic"}
    assert mapping["v1"][rig.pid(GOLDEN[1][0])] == {"family_id": pair[0]["family_id"], "row_id": pair[1]["id"], "split": "realistic"}
    a = family_of(realistic, Q("a0"))
    assert mapping["candidates"]["T01-A"] == {
        "family_id": a[0]["family_id"], "row_ids": [r["id"] for r in a], "split": "realistic", "held": False, "phrasings": [0, 1, 2],
    }
    assert mapping["candidates"]["T07-Bb-RV"]["row_ids"] == [family_of(realistic, Q("rv"))[0]["id"]]
    ids = {e["family_id"] for e in [*mapping["v1"].values(), *mapping["candidates"].values()]}
    assert len(ids) == 3 + 10  # a pair's two v1 ids share one family
    assert rig.fields(out, "migration_map")["sha256"] == sha(rig.work / mig.MAP_NAME)


def test_minted_ids_avoid_known_and_issued_ids_and_are_recorded_as_issued(rig, monkeypatch):
    stray = rig.private / "sealed" / "work" / "ids.txt"  # an id issued earlier (anywhere under the private root counts)
    stray.parent.mkdir(parents=True)
    stray.write_text("fdeadbee0\nfcafe0001\n", encoding="ascii")
    other = rig.dev / "tuning_pinned.jsonl"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('{"family_id": "f12345678"}\n', encoding="utf-8")
    draws = iter(["fdeadbee0", "f12345678", "fcafe0001", "fdeadbee0"])  # three collisions, one repeat of a taken id
    real_new = st.new_family_id
    taken = []

    def scripted() -> str:
        try:
            return next(draws)
        except StopIteration:
            value = real_new()
            taken.append(value)
            return value

    monkeypatch.setattr(st, "new_family_id", scripted)
    tuning, realistic, _ = migrate(rig)
    minted = {r["family_id"] for r in [*tuning, *realistic]}
    assert len(minted) == 3 + 10 and minted.isdisjoint({"fdeadbee0", "fcafe0001", "f12345678"})
    issued = (rig.private / "sealed" / "work" / "issued_ids.txt").read_text(encoding="ascii").split()
    assert sorted(issued) == sorted(minted)  # recorded the way `ids` does it: one id per line
    # a later `ids` call never re-issues them
    assert minted <= st.known_family_ids(st.default_ctx(st.Printer(io.StringIO(), io.StringIO())))


def test_a_reused_id_that_another_file_already_holds_is_refused(rig):
    migrate(rig)
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))
    taken = mapping["candidates"]["T01-B"]["family_id"]
    (rig.dev / "tuning_other.jsonl").write_text(json.dumps({"family_id": taken}) + "\n", encoding="utf-8")
    before = rig.files()
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=id_collision" in err and taken not in out + err
    assert rig.files() == before


def test_a_map_that_does_not_fit_the_inputs_is_refused(rig):
    migrate(rig)
    path = rig.work / mig.MAP_NAME
    good = json.loads(path.read_text(encoding="utf-8"))

    def broken(edit) -> Tuple[int, str]:
        doc = json.loads(json.dumps(good))
        edit(doc)
        path.write_text(json.dumps(doc), encoding="utf-8")
        code, out, err = rig.run()
        return code, err

    cases = [
        lambda d: d["candidates"].update({"T99-Z": {**d["candidates"]["T01-A"]}}),  # an entry the inputs do not name
        lambda d: d["v1"].pop(rig.pid(GOLDEN[1][0])),  # one id of a pair is missing
        lambda d: d["candidates"]["T01-B"].update(split="tuning"),  # the family is in another split
        lambda d: d["candidates"]["T01-B"].update(family_id=d["candidates"]["T01-C"]["family_id"]),  # two families on one id
        lambda d: d["candidates"]["T01-B"].update(family_id="not-an-id"),
        lambda d: d.update(version=2),
        lambda d: d.pop("v1"),
    ]
    for edit in cases:
        code, err = broken(edit)
        assert code == st.EXIT_USAGE and "reason=map_invalid" in err
    path.write_text("not json", encoding="utf-8")
    assert rig.run()[0] == st.EXIT_USAGE
    path.write_text(json.dumps(good), encoding="utf-8")
    assert rig.run()[0] == 0  # the real map is fine again


def test_a_new_golden_family_gets_a_new_id_and_the_others_keep_theirs(rig):
    tuning, _, _ = migrate(rig)
    ids = {r["question"]: r["family_id"] for r in tuning}
    rig.configure(rig.v1("golden2.jsonl", [*GOLDEN, (Q("g5"), "direct", ["1.4"])]), rig.v1("realistic.jsonl", REALISTIC), rig.pairing(PAIRS),
                  [rig.tmp / "tutorials" / "tutorial01_candidates.md", rig.tmp / "tutorials" / "tutorial07_candidates.md"])
    code, out, err = rig.run()
    assert code == 0, err
    again = {r["question"]: r["family_id"] for r in read_rows(rig.dev / "tuning_migrated.jsonl")}
    assert {q: f for q, f in again.items() if q in ids} == ids and len(again) == 4
    assert rig.fields(out, "migration_map")["ids_minted"] == "1"


# --- validation, scope agreement and the counts ------------------------------------------------------------------------------------------
def test_a_row_that_fails_the_v2_schema_stops_the_run_and_writes_nothing(rig):
    rig.configure(rig.v1("golden3.jsonl", [*GOLDEN, (Q("g6"), "direct", ["CANARYSEC.9"])]), rig.v1("realistic.jsonl", REALISTIC), rig.pairing(PAIRS),
                  [rig.tmp / "tutorials" / "tutorial01_candidates.md", rig.tmp / "tutorials" / "tutorial07_candidates.md"])
    before = rig.files()
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=schema_invalid errors=1" in err
    assert "output_error file=tuning line=" in out and "field=evidence[0][0]" in out
    assert CANARY not in out + err and "CANARYSEC" not in out + err
    assert rig.files() == before and not list(rig.dev.iterdir())


def test_paired_rows_that_disagree_on_scope_are_refused_loudly(rig):
    rig.gaps({"T01-A": GAPS["T01-A"]})  # only the first tutorial file is read here
    rig.configure(rig.v1("golden4.jsonl", [(Q("g0"), "refusal", None), (Q("g1"), "direct", ["1.1"])]),
                  rig.v1("realistic4.jsonl", [(Q("r0"), "direct", ["5.1"]), (Q("r1"), "direct", ["1.3"])]),
                  rig.pairing([(Q("g0"), Q("r0"))]), [rig.tmp / "tutorials" / "tutorial01_candidates.md"])
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and "reason=pair_scope_conflict conflicts=1" in err
    assert CANARY not in out + err and not rig.dev.exists()


@pytest.mark.parametrize("pairs", [[("nope", "nope")], "reuse_golden", "reuse_realistic"])
def test_a_pairing_that_names_unknown_or_reused_ids_is_refused(rig, pairs):
    if pairs == "reuse_golden":
        pairs = [(GOLDEN[1][0], REALISTIC[1][0]), (GOLDEN[1][0], REALISTIC[2][0])]
    elif pairs == "reuse_realistic":
        pairs = [(GOLDEN[1][0], REALISTIC[1][0]), (GOLDEN[2][0], REALISTIC[1][0])]
    doc = {"pairs": [[rig.pid(g) if g != "nope" else "q:000000000000", rig.pid(r) if r != "nope" else "q:111111111111"] for g, r in pairs]}
    path = rig.tmp / "pairing.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=inputs_invalid" in err and not rig.dev.exists()


def count_migrated_fields(rig, tmp_path) -> Dict[str, str]:
    """``count-migrated``'s line over the same v1 sets and pairing, with every candidate as its own tutorial input (one row per
    phrasing, id = candidate + letter): the labels do not reach it."""
    tutorial = tmp_path / "tutorial_count.jsonl"
    tutorial.write_text("".join(
        json.dumps({"id": f"{cid}{letter}", "type": rows[0][1]}) + "\n" for cid, _, rows in [*T01, *T07] for letter in "ab"), encoding="utf-8")
    prereg = tmp_path / "prereg_count.json"
    prereg.write_text(json.dumps({"drafting_rule": {"attempt_1": {"refusal_share_target": 0.275, "families_approx": 100, "draft_margin": 5}},
                                  "quota": {"sealed": 12, "realistic_min": 4, "tuning_min": 8}}), encoding="utf-8")
    cm_out = io.StringIO()
    st.main(["--prereg", str(prereg), "count-migrated", "--golden", rig.argv[1], "--realistic", rig.argv[3], "--pairing", rig.argv[5],
             "--tutorial", str(tutorial)], out=cm_out, err=io.StringIO())
    return dict(p.split("=", 1) for p in cm_out.getvalue().splitlines()[0].split(" ")[1:])


def test_the_counts_are_the_ones_count_migrated_computes_from_the_same_inputs(rig, tmp_path):
    _, _, out = migrate(rig)
    check = rig.fields(out, "migrated_check")
    cm = count_migrated_fields(rig, tmp_path)
    for key in ("pairs", "tutorial_exercises", "tuning_families", "tuning_refuse", "realistic_families", "realistic_refuse"):
        assert check[key] == cm[key], key
    assert (check["tuning_families"], check["tuning_refuse"], check["realistic_families"], check["realistic_refuse"]) == ("3", "1", "10", "3")


def test_a_disagreement_with_count_migrated_is_a_count_mismatch(rig, monkeypatch):
    real_counts = st.migrated_counts

    def off_by_one(gold, real, tut, pairs):
        c = real_counts(gold, real, tut, pairs)
        return st.MigratedCounts(**{**c.__dict__, "tuning_families": c.tuning_families + 1})

    monkeypatch.setattr(st, "migrated_counts", off_by_one)
    code, out, err = rig.run()
    assert code == st.EXIT_FAIL and "reason=count_mismatch" in err
    assert not list(rig.dev.iterdir()) and not (rig.work / mig.MAP_NAME).exists()  # the check comes before the first write


# --- the candidate files ----------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("blocks", [
    [("T01-A", "IN", [(Q("a"), "direct", ["1.1"])]), ("T01-A", "IN", [(Q("b"), "direct", ["1.1"])])],  # one id twice
    [("T01-A", "OUT", [(Q("a"), "direct", ["1.1"])])],  # OUT but a direct row
    [("T01-A", "IN", [(Q("a"), "refusal", None)])],  # IN but a refusal row
    [("T01-A", "IN", [(Q("a"), "direct", ["1.1"])]), ("T01-B", None, [(Q("b"), "direct", ["1.1"])])],  # a family with no block, not an RV
    [("T01-A-RV", "IN", [(Q("a"), "direct", ["1.1"])])],  # an RV that claims to be answerable
    [("T01-A", "IN", [(Q("a"), "direct", [])])],  # a direct row with no sections
    [("T01-A", "IN", [(Q("a"), "weird", ["1.1"])])],  # an unknown v1 type
    [("not an id", "IN", [(Q("a"), "direct", ["1.1"])])],
])
def test_candidate_file_problems_are_invalid_input(rig, blocks):
    path = rig.candidates("tutorial01_candidates.md", blocks)
    rig.configure(Path(rig.argv[1]), Path(rig.argv[3]), Path(rig.argv[5]), [path])
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err, (code, err)
    assert CANARY not in out + err and not rig.dev.exists()


def test_a_block_without_phrasings_or_a_scope_and_a_broken_json_line_are_invalid_input(rig):
    path = rig.tmp / "tutorials" / "tutorial01_candidates.md"
    for text in ("### T01-A\n- **scope:** IN\n", "### T01-A\n```\n" + json.dumps({"question": "q", "type": "direct", "expected_sections": ["1.1"],
                                                                                   "candidate_id": "T01-A"}) + "\n```\n",
                 "### T01-A\n- **scope:** IN\n{not json}\n"):
        path.write_text(text, encoding="utf-8")
        rig.configure(Path(rig.argv[1]), Path(rig.argv[3]), Path(rig.argv[5]), [path])
        code, out, err = rig.run()
        assert code == st.EXIT_USAGE and "reason=input_invalid" in err


def test_a_v2_set_is_not_a_migration_input(rig):
    v2 = rig.tmp / "public" / "v2.jsonl"
    v2.write_text(json.dumps({"schema": 2, "id": "abc-1", "family_id": "abc-1", "question": Q("v2"), "scope": "answer", "evidence": [["1.1"]]}) + "\n",
                  encoding="utf-8")
    rig.registry.add(v2, privacy="public", name="v2.jsonl")
    rig.configure(v2, Path(rig.argv[3]), Path(rig.argv[5]), [rig.tmp / "tutorials" / "tutorial01_candidates.md"])
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err


def test_a_missing_input_is_reported_without_naming_it(rig):
    rig.configure(rig.tmp / f"{CANARY}-gone.jsonl", Path(rig.argv[3]), Path(rig.argv[5]), [rig.tmp / "tutorials" / "tutorial01_candidates.md"])
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_missing" in err and CANARY not in out + err


# --- what it prints and how it writes ----------------------------------------------------------------------------------------------------------
def test_it_prints_counts_and_digests_only_and_the_files_are_private_and_valid(rig):
    tuning, realistic, out = migrate(rig)
    assert CANARY not in out and "T01" not in out
    assert_allowlisted(out)
    lines = {ln.split(" ")[1].split("=")[1]: dict(p.split("=", 1) for p in ln.split(" ")[1:]) for ln in out.splitlines() if ln.startswith("migrated ")}
    assert (lines["tuning"]["families"], lines["tuning"]["rows"], lines["tuning"]["refusals"], lines["tuning"]["partials"]) == ("3", "3", "1", "0")
    # realistic: 10 families = 2 unpaired (2 rows) + 2 pairs (4 rows) + 6 candidates (3 + 3 + 3 + 2 + 3 + 1 rows); 3 refusals; 2 partial
    assert (lines["realistic"]["families"], lines["realistic"]["rows"], lines["realistic"]["refusals"], lines["realistic"]["partials"]) == ("10", "21", "3", "2")
    assert lines["tuning"]["sha256"] == sha(rig.dev / "tuning_migrated.jsonl") and lines["realistic"]["sha256"] == sha(rig.dev / "realistic_migrated.jsonl")
    for name in ("tuning_migrated.jsonl", "realistic_migrated.jsonl"):
        path = rig.dev / name
        assert (path.stat().st_mode & 0o777) == 0o600
        rows = eval_schema.load_v2(path, inventory=INVENTORY)  # the dev loaders take them as they are
        assert rows and all("sealed" not in row for row in read_rows(path))  # development rows are unmarked
    ctx = st.default_ctx(st.Printer(io.StringIO(), io.StringIO()), repo=rig.tmp, inventory_path=rig.inventory)
    fams = st.load_dev(ctx)
    assert len(fams) == 13 and {f.split for f in fams.values()} == {"tuning", "realistic"}  # pinned families, as `split` will read them


def test_the_three_outputs_are_the_only_things_written(rig):
    migrate(rig)
    assert sorted(rig.files()) == [
        "dev/realistic_migrated.jsonl", "dev/tuning_migrated.jsonl", "sealed/work/issued_ids.txt", "work/b2/migration_map.json",
        "work/b2/t1t7_gaps.json", "work/b2/t1t7_labels.json",  # the gaps and labels files were there before
    ]


def test_the_default_inputs_are_the_repos(rig, tmp_path):
    """Without flags the tool reads ``eval/golden_set.jsonl`` ... and ``data/tutorials/tutorial0{1,7}_candidates.md`` under the repo."""
    golden = rig.v1("golden.jsonl", GOLDEN)
    (tmp_path / "eval").mkdir(exist_ok=True)
    (tmp_path / "eval" / "golden_set.jsonl").write_bytes(golden.read_bytes())
    (tmp_path / "eval" / "realistic_set.jsonl").write_bytes((tmp_path / "public" / "realistic.jsonl").read_bytes())
    (tmp_path / "eval" / "v2_pairing.json").write_bytes((tmp_path / "pairing.json").read_bytes())
    (tmp_path / "data" / "tutorials").mkdir(parents=True)
    for name in mig.TUTORIAL_FILES:
        (tmp_path / "data" / "tutorials" / name).write_bytes((tmp_path / "tutorials" / name).read_bytes())
    for name in ("golden_set.jsonl", "realistic_set.jsonl"):
        rig.registry.add(tmp_path / "eval" / name, privacy="public", name=f"default-{name}")
    out, err = io.StringIO(), io.StringIO()
    code = mig.main(["--repo", str(tmp_path), "--inventory", str(rig.inventory)], out=out, err=err)  # gaps default: private/work/b2/t1t7_gaps.json
    assert code == 0, err.getvalue()
    assert rig.fields(out.getvalue(), "migrated_check")["realistic_families"] == "10"


def test_a_second_state_changing_command_is_refused_while_it_runs(rig):
    """It holds ``sealed/work/.lock`` like every state-changing command."""
    import fcntl
    import os

    (rig.private / "sealed" / "work").mkdir(parents=True)
    fd = os.open(rig.private / "sealed" / "work" / st.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out, err = rig.run()
        assert code == st.EXIT_REFUSED and "reason=locked" in err and not rig.dev.exists()
    finally:
        os.close(fd)


def test_an_empty_split_leaves_no_file_and_removes_its_own_stale_one(rig):
    migrate(rig)
    assert (rig.dev / "tuning_migrated.jsonl").is_file()
    only_paired = [(GOLDEN[1][0], "direct", ["1.1", "1.2"]), (GOLDEN[2][0], "direct", ["2.1"])]
    rig.configure(rig.v1("golden5.jsonl", only_paired), rig.v1("realistic.jsonl", REALISTIC), rig.pairing(PAIRS),
                  [rig.tmp / "tutorials" / "tutorial01_candidates.md", rig.tmp / "tutorials" / "tutorial07_candidates.md"])
    path = rig.work / mig.MAP_NAME
    mapping = json.loads(path.read_text(encoding="utf-8"))
    for q in (GOLDEN[0][0], GOLDEN[3][0], GOLDEN[4][0]):  # the golden-only families are gone from the inputs: their map entries too
        mapping["v1"].pop(rig.pid(q))
    path.write_text(json.dumps(mapping), encoding="utf-8")
    code, out, err = rig.run()
    assert code == 0, err
    assert not (rig.dev / "tuning_migrated.jsonl").exists() and (rig.dev / "realistic_migrated.jsonl").is_file()
    assert rig.fields(out, "migrated")["families"] == "10"  # the last `migrated` line is realistic's
    assert "file=tuning families=0" in out


# --- the owner's labels: scope overrides, phrasing selection, flags ------------------------------------------------------------------------------
GAP_B = [{"id": "gb1", "keywords": ["bee kw"]}]


def test_scope_overrides_replace_the_headings_scope(rig):
    """T01-A is PARTIAL in its heading and answered by the label (no gaps for it); T01-B is IN and made PARTIAL by the label (gaps given)."""
    rig.labels(scope={"T01-A": "answer", "T01-B": "partial"})
    rig.gaps({"T01-B": GAP_B, "T07-Bb": GAPS["T07-Bb"]})
    _, realistic, out = migrate(rig)
    a = family_of(realistic, Q("a0"))
    assert {r["scope"] for r in a} == {"answer"} and all("gaps" not in r for r in a) and len(a) == 3
    b = family_of(realistic, Q("b0"))
    assert {r["scope"] for r in b} == {"partial"} and all(r["gaps"] == GAP_B for r in b)
    assert family_of(realistic, Q("y0"))[0]["gaps"] == GAPS["T07-Bb"]  # unlabelled ids keep their headings
    assert {r["scope"] for r in family_of(realistic, Q("x0"))} == {"answer"} and {r["scope"] for r in family_of(realistic, Q("c0"))} == {"refuse"}
    assert rig.fields(out, "labels")["scope_overrides"] == "2"
    assert rig.fields(out, "migrated")["partials"] == "2"  # the last `migrated` line is realistic's: T01-B and T07-Bb
    assert (rig.fields(out, "migrated_check")["realistic_families"], rig.fields(out, "migrated_check")["realistic_refuse"]) == ("10", "3")


@pytest.mark.parametrize("gaps_file", ["unresolved", "gaps", "absent"])
def test_an_id_overridden_away_from_partial_needs_no_gaps_and_is_no_longer_unresolved(rig, gaps_file):
    """The gaps file was drafted for the heading: whatever it says about T01-A (unresolved, or gaps, or nothing) is ignored once
    the label makes it an answer, and the run needs no ``--hold-unresolved``."""
    rig.labels(scope={"T01-A": "answer"})
    if gaps_file == "unresolved":
        rig.gaps({"T07-Bb": GAPS["T07-Bb"]}, {"T01-A": f"{CANARY} the notes do not say"})
    elif gaps_file == "gaps":
        rig.gaps(GAPS)
    else:
        rig.gaps({"T07-Bb": GAPS["T07-Bb"]})
    _, realistic, out = migrate(rig)
    assert {r["scope"] for r in family_of(realistic, Q("a0"))} == {"answer"}
    assert not (rig.inputs / "held_unresolved.jsonl").exists() and rig.fields(out, "held")["families"] == "0"
    assert CANARY not in out
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))
    assert mapping["candidates"]["T01-A"]["held"] is False


def test_an_override_to_partial_still_needs_a_gaps_entry(rig):
    rig.labels(scope={"T01-B": "partial"})  # the gaps file has T01-A and T07-Bb only
    code, out, err = rig.run()
    assert code == st.EXIT_REFUSED and out == "unresolved candidate=T01-B\n" and "reason=unresolved_partial families=1" in err
    assert not rig.dev.exists() or not list(rig.dev.iterdir())
    rig.gaps(GAPS, {"T01-B": "unclear"})  # listed unresolved: the same refusal; with the flag it is held
    assert rig.run()[0] == st.EXIT_REFUSED
    code, out, err = rig.run("--hold-unresolved")
    assert code == 0, err
    assert [r["question"] for r in read_rows(rig.inputs / "held_unresolved.jsonl")] == [Q("b0"), Q("b1"), Q("b2")]


@pytest.mark.parametrize("scope", [{"T01-C": "answer"}, {"T01-C": "partial"}, {"T01-B": "refuse"}, {"T07-Bb-RV": "answer"}])
def test_an_override_that_disagrees_with_the_rows_type_is_invalid_input(rig, scope):
    """Only an override between answer and partial changes no refusal status; one between refuse and the others contradicts the rows."""
    rig.labels(scope=scope)
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err and not rig.dev.exists()


def test_the_phrasings_are_the_listed_embedded_rows_in_the_listed_order_and_row_0_is_the_first(rig):
    rig.labels(phrasings={"T01-A": [2, 0], "T01-B": [1], "T07-Aa": [1, 0], "T07-Bb-RV": [0]})
    _, realistic, out = migrate(rig)
    a = family_of(realistic, Q("a2"))  # row 0 is the first picked: embedded row 2, which is not the modelled question, so lay
    assert [(r["id"].rsplit("-", 1)[1], r["question"], r["register"]) for r in a] == [("0", Q("a2"), "lay"), ("1", Q("a0"), "modelled")]
    b = family_of(realistic, Q("b1"))  # a single picked row that is not the first embedded one
    assert [(r["question"], r["register"]) for r in b] == [(Q("b1"), "lay")]
    x = family_of(realistic, Q("x1"))
    assert [(r["question"], r["register"]) for r in x] == [(Q("x1"), "lay"), (Q("x0"), "modelled")]
    c = family_of(realistic, Q("c0"))  # no entry: the first 3 embedded rows, the first modelled
    assert [r["register"] for r in c] == ["modelled", "lay", "lay"]
    y = family_of(realistic, Q("y0"))
    assert [r["question"] for r in y] == [Q("y0"), Q("y1"), Q("y2")] and [r["register"] for r in y] == ["modelled", "lay", "lay"]
    assert [(r["register"], r["scope"]) for r in family_of(realistic, Q("rv"))] == [("modelled", "refuse")]
    assert all(Q("a1") != r["question"] and Q("a3") != r["question"] for r in realistic)  # unlisted embedded rows are not migrated
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))["candidates"]
    assert [mapping[c]["phrasings"] for c in ("T01-A", "T01-B", "T07-Aa", "T01-C", "T07-Bb-RV")] == [[2, 0], [1], [1, 0], [0, 1, 2], [0]]
    assert mapping["T01-A"]["row_ids"] == [r["id"] for r in a]
    assert rig.fields(out, "labels")["phrasing_entries"] == "4"


def test_the_listed_rows_carry_the_families_scope_gaps_and_draft_status(rig):
    rig.labels(phrasings={"T01-A": [3, 1]})
    _, realistic, _ = migrate(rig)
    a = family_of(realistic, Q("a3"))
    assert all(r["scope"] == "partial" and r["gaps"] == GAPS["T01-A"] and r["owner_status"] == "draft" and r["source"] == "tutorial" for r in a)


@pytest.mark.parametrize("phrasings", [
    {"T01-A": [4]},  # out of range: T01-A has four embedded rows (0 to 3)
    {"T01-A": [-1]},
    {"T07-Bb-RV": [1]},  # the variant has one embedded row
    {"T01-A": [1, 1]},  # a duplicate
    {"T01-A": [0, 1, 2, 3]},  # more than 3
    {"T01-A": []},  # empty
    {"T99-Z": [0]},  # not a candidate
    {"T01-A": [0, "1"]}, {"T01-A": [True]}, {"T01-A": [1.0]}, {"T01-A": 1}, {"T01-A": None},
])
def test_each_phrasing_problem_is_invalid_input_and_writes_nothing(rig, phrasings):
    rig.labels(phrasings=phrasings)
    before = rig.files()
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err and out == "" and rig.files() == before
    assert CANARY not in out + err and not (rig.dev.exists() and list(rig.dev.iterdir()))


@pytest.mark.parametrize("edit", [
    lambda: dict(scope={"T99-Z": "answer"}), lambda: dict(flags={"T99-Z": "x"}),  # an unknown candidate id in the other sections
    lambda: dict(scope={"nope": "answer"}), lambda: dict(phrasings={"nope": [0]}), lambda: dict(flags={"nope": "x"}),
    lambda: dict(scope={"T01-A": "partially"}), lambda: dict(scope={"T01-A": None}), lambda: dict(flags={"T01-A": 1}),
    lambda: dict(scope=[]), lambda: dict(phrasings="all"), lambda: dict(extra_key=1), lambda: dict(version=2), lambda: dict(provenance=1),
])
def test_a_labels_file_of_the_wrong_shape_is_invalid_input(rig, edit):
    kwargs = edit()
    version = kwargs.pop("version", 1)
    provenance = kwargs.pop("provenance", "synthetic")
    doc = {"version": version, "provenance": provenance, "scope": {}, "phrasings": {}, "flags": {}, **kwargs}
    (rig.work / "t1t7_labels.json").write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_invalid" in err and not (rig.dev.exists() and list(rig.dev.iterdir()))


def test_the_labels_file_is_required_and_must_be_json(rig):
    path = rig.work / "t1t7_labels.json"
    for text in ("[]", "not json", '{"scope": {}}', '{"version": 1}'):
        path.write_text(text, encoding="utf-8")
        code, out, err = rig.run()
        assert code == st.EXIT_USAGE and "reason=input_invalid" in err, text
    path.unlink()
    code, out, err = rig.run()
    assert code == st.EXIT_USAGE and "reason=input_missing" in err and not rig.dev.exists()
    other = rig.labels(scope={"T01-A": "answer"})  # the flag points elsewhere; the default is only the default
    moved = rig.tmp / "elsewhere_labels.json"
    moved.write_bytes(other.read_bytes())
    other.unlink()
    code, out, err = rig.run("--labels", str(moved))
    assert code == 0, err


def test_a_labels_file_with_only_the_required_keys_labels_nothing(rig):
    (rig.work / "t1t7_labels.json").write_text(json.dumps({"version": 1, "provenance": "p"}), encoding="utf-8")
    _, realistic, out = migrate(rig)
    assert [r["register"] for r in family_of(realistic, Q("a0"))] == ["modelled", "lay", "lay"]
    assert rig.fields(out, "labels")["scope_overrides"] == "0"


def test_flags_are_recorded_in_the_map_per_family_and_never_in_a_row_or_the_summary(rig):
    note = f"{CANARY} twin_candidate_of_golden_for_b4"
    rig.labels(flags={"T01-B": note, "T07-Aa": "second_note"})
    tuning, realistic, out = migrate(rig)
    mapping = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))["candidates"]
    assert mapping["T01-B"]["flags"] == note and mapping["T07-Aa"]["flags"] == "second_note"
    assert all("flags" not in e for c, e in mapping.items() if c not in ("T01-B", "T07-Aa"))
    assert all("flags" not in r and note not in json.dumps(r) for r in [*tuning, *realistic])
    assert CANARY not in out and rig.fields(out, "labels")["flags"] == "2"


def test_the_labels_digest_is_in_the_map_and_the_summary(rig):
    labels = rig.labels(scope={"T01-A": "answer"}, phrasings={"T01-B": [2]})
    rig.gaps({"T07-Bb": GAPS["T07-Bb"]})
    _, _, out = migrate(rig)
    digest = sha(labels)
    assert rig.fields(out, "labels")["sha256"] == digest
    assert json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))["labels_sha256"] == digest
    assert_allowlisted(out)
    # a changed labels file changes the digest in the map, and the ids stay
    ids = {c: e["family_id"] for c, e in json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))["candidates"].items()}
    labels = rig.labels(scope={"T01-A": "answer"}, phrasings={"T01-B": [2, 1]})
    _, _, out2 = migrate(rig)
    again = json.loads((rig.work / mig.MAP_NAME).read_text(encoding="utf-8"))
    assert again["labels_sha256"] == sha(labels) != digest and {c: e["family_id"] for c, e in again["candidates"].items()} == ids
    assert rig.fields(out2, "migration_map")["ids_minted"] == "0"


def test_a_rerun_with_labels_writes_byte_identical_files(rig):
    rig.labels(scope={"T01-A": "answer", "T01-B": "partial"}, phrasings={"T01-A": [3, 0], "T01-C": [2], "T07-Aa": [1]},
               flags={"T01-A": "first", "T07-Bb": "second"})
    rig.gaps({"T01-B": GAP_B, "T07-Bb": GAPS["T07-Bb"]}, {"T01-A": "was unresolved, now answered"})
    migrate(rig)
    first = rig.files()
    code, out, err = rig.run()
    assert code == 0, err
    assert rig.files() == first  # the dev files, the map (ids, phrasings, flags, labels digest) and the issued list
    assert rig.fields(out, "migration_map")["ids_minted"] == "0" and not list(rig.dev.glob(".migrate_stage_*"))


def test_the_counts_with_labels_still_equal_count_migrateds(rig, tmp_path):
    """An override between answer and partial and a different pick of phrasings change no family and no refusal: the tutorial
    input of ``count-migrated`` (every candidate id) and the migration count the same set."""
    rig.labels(scope={"T01-A": "answer", "T01-B": "partial"}, phrasings={"T01-C": [2, 1], "T07-Aa": [1]})
    rig.gaps({"T01-B": GAP_B, "T07-Bb": GAPS["T07-Bb"]})
    _, _, out = migrate(rig)
    check, cm = rig.fields(out, "migrated_check"), count_migrated_fields(rig, tmp_path)
    for key in ("pairs", "tutorial_exercises", "tuning_families", "tuning_refuse", "realistic_families", "realistic_refuse"):
        assert check[key] == cm[key], key
    assert check["tutorial_exercises"] == "6"
