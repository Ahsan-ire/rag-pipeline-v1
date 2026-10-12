"""Phase 16A-2a phase gate, Area C (C1-C4): the scanner's gate fixes.

Every question, family id and word here is invented (the fixtures of
``test_p16w_scan_sealed`` plus a few more). Nothing reads a real eval set, the
corpus or ``eval/private/``; the private root is the per-test
``tmp_path/eval/private`` (conftest).

- C1 (Rev9): a short sealed question is also caught as a whole line / whole value
  after JSON string escapes are decoded (a transcript is one physical line).
- C2 (Rev10): only released or frozen development splits (``dev/tuning*``,
  ``dev/realistic*``) exempt a sealed window.
- C3 (Rev12): a window or whole question maps to the SET of sealed families.
- C4 (Rev14): crash leftovers of an atomic write are record files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import src.eval_sets as eval_sets
from scripts import scan_leaks as sl
from tests.test_p16w_scan_leaks import _write_jsonl
from tests.test_p16w_scan_sealed import (  # noqa: F401  (fixtures are used by name)
    DEV_Q,
    FAM_LONG,
    FAM_OTHER,
    FAM_SHORT,
    SHORT5,
    T,
    _gate,
    _index,
    _isolated,
    _mkdir,
    _record,
    _s_hits,
    repo,
    window,
    world,
)
from tests.test_p16w_schema import _row, _write

FAM_A, FAM_B, FAM_C = "f5555abcd", "f6666abcd", "f7777abcd"
A10 = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
B11 = "kilo lima alpha bravo charlie delta echo foxtrot golf hotel mike"  # shares A10[0:8] at B11[2:10]
SHARED = " ".join(A10.split()[0:8])
B_LATE = "kilo lima bravo charlie delta echo foxtrot golf hotel india mike"  # shares A10[1:9] only
SHARED_LATE = " ".join(A10.split()[1:9])


# --- C1: a short sealed question inside JSON-escaped text ---------------------------------------------
def test_c1_a_short_question_as_a_whole_line_inside_a_json_escaped_transcript(world):
    index = _index(world)
    assert _s_hits(f"intro\n{SHORT5}\noutro\n", index) == [("t.md:2", FAM_SHORT)]  # the plain file
    # the same file content as a transcript records it: one physical line, the newlines escaped
    transcript = json.dumps({"type": "tool_result", "content": f"intro\n{SHORT5}\noutro"}) + "\n"
    assert transcript.count("\n") == 1
    assert _s_hits(transcript, index) == [("t.md:1", FAM_SHORT)]


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"content": f"intro\n{SHORT5}\noutro"}),  # a JSON value, \n escaped
        json.dumps({"content": f"intro\n  - {SHORT5.upper()}?  \noutro"}),  # re-cased, bulleted, punctuated
        json.dumps({"content": f"intro\r\n{SHORT5}\r\noutro"}),  # CRLF
        json.dumps({"message": {"content": [{"text": f"x\n{SHORT5}"}]}}),  # nested objects and lists
        json.dumps({"content": json.dumps({"stdout": f"intro\n{SHORT5}\noutro"})}),  # a serialisation inside a string
        json.dumps({"content": json.dumps(json.dumps({"stdout": f"a\n{SHORT5}\nb"}))}),  # and inside that
        '{"content": "intro\\u000a' + SHORT5 + '\\u000aoutro"}',  # \u000a instead of \n
        "intro\\n" + SHORT5 + "\\noutro",  # not JSON at all: the raw line's \n escapes
        'INFO payload={"q": "intro\\n' + SHORT5 + '\\noutro"}',  # JSON embedded in a log line
        'INFO payload={"q": "' + SHORT5 + '\\nmore"}',  # whole piece at the start of the embedded value
        json.dumps({"content": f"intro\n“{SHORT5}”\noutro"}),  # curly quotes are punctuation
        json.dumps({SHORT5 + "\nkey": 1}),  # in a key
        json.dumps({"content": "zorbin keldwyn\nplimsy tarnock vessuvio"}),  # the whole value, wrapped (caught before C1 too)
    ],
)
def test_c1_the_escaped_forms_are_caught(world, text):
    assert _s_hits(text, _index(world)) == [("t.md:1", FAM_SHORT)]


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"content": f"the client asked {SHORT5} yesterday\nand more"}),  # in prose
        json.dumps({"content": f"1. {SHORT5}\nnext"}),  # a numbered item is prose (the documented residual)
        json.dumps({"content": f"question: {SHORT5}\nnext"}),  # unquoted key: value
        json.dumps({"content": SHORT5 + " again\nnext"}),  # a longer piece
        json.dumps({"content": "zorbin keldwyn plimsy tarnock\nvessuvio extra"}),  # a fragment on each side of a break
        "intro\\n" + SHORT5 + " again\\noutro",  # raw escapes, piece too long
    ],
)
def test_c1_prose_is_still_not_a_hit_after_decoding(world, text):
    assert sl.scan_text(text, "t.md", _index(world)) == []


def test_c1_a_transcript_file_is_a_hit_through_the_cli(world, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "content": "hello"}) + "\n"
        + json.dumps({"type": "tool_result", "content": f"intro\n{SHORT5}\noutro"}) + "\n",
        encoding="utf-8",
    )
    assert sl.main(["--output", str(transcript), "--handbook-dump", str(world["dump"])]) == 5
    assert capsys.readouterr().out.splitlines() == [f"{transcript}:2\t{FAM_SHORT}"]


# --- C2: only development splits exempt a sealed window ---------------------------------------------------
FRAG = " ".join(T[3:12])  # 9 tokens: holds the sealed LONG question's windows w3 and w4, in no exempt source


def test_c2_a_fragment_saved_in_work_does_not_hide_an_s_hit(world):
    root = world["root"]
    index = _index(world)
    assert _s_hits(f"note: {window(5)}\n", index) == [("t.md:1", FAM_LONG)]  # the control
    work = _mkdir(root / "work")
    (work / "scratch.jsonl").write_text(json.dumps({"question": " ".join(T[5:15])}) + "\n", encoding="utf-8")
    after = _index(world)
    assert _s_hits(f"note: {window(5)}\n", after) == [("t.md:1", FAM_LONG)]
    assert after.s_exempt_windows == index.s_exempt_windows  # nothing more was exempted
    # the fragment itself is still a private needle (a private hit is not an S-hit)
    assert [h for h in sl.scan_text(f"note: {' '.join(T[5:15])}\n", "t.md", after) if h.family is None]


@pytest.mark.parametrize(
    "rel",
    ["dev/tuning_r1.jsonl", "dev/realistic_r1.jsonl", "dev/tuning-v2.jsonl", "dev/realistic-v2.jsonl", "dev/tuning.jsonl"],
)
def test_c2_a_released_or_frozen_development_split_exempts_its_windows(world, rel):
    world["dev"].unlink()  # the fixture's own development file would also exempt w1
    _write(_mkdir(world["root"] / Path(rel).parent) / Path(rel).name, [_row(0, "f4444abcd", question=FRAG)])
    index = _index(world)
    assert not any(tuple(window(i).split()) in index.s_windows for i in (3, 4))
    assert [_s_hits(window(i), index) for i in (3, 4)] == [[], []]
    assert _s_hits(window(5), index) == [("t.md:1", FAM_LONG)]  # a window the split does not hold


@pytest.mark.parametrize(
    "rel",
    [
        "work/frag.jsonl",
        "dev/quarantine_r1.jsonl",
        "dev/quarantine.jsonl",
        "dev/other.jsonl",
        "dev/dev.jsonl",
        "dev/sub/tuning_r1.jsonl",  # one level only
        "tuning_r1.jsonl",  # not under dev/
        "sets/tuning_r1.jsonl",
        "dev/notes_tuning.jsonl",
    ],
)
def test_c2_any_other_private_source_exempts_nothing(world, rel):
    world["dev"].unlink()
    _write(_mkdir((world["root"] / rel).parent) / Path(rel).name, [_row(0, "f4444abcd", question=FRAG)])
    index = _index(world)
    assert all(tuple(window(i).split()) in index.s_windows for i in (3, 4))
    assert [_s_hits(window(i), index) for i in (3, 4)] == [[("t.md:1", FAM_LONG)]] * 2
    # ... and it is still a private needle source
    assert [h for h in sl.scan_text(FRAG, "t.md", index) if h.family is None]


def test_c2_a_needles_path_a_registry_private_set_and_a_run_input_exempt_nothing(world, tmp_path, eval_registry):
    world["dev"].unlink()
    outside = _write_jsonl(tmp_path / "elsewhere" / "frag.jsonl", [{"question": FRAG}])
    # --needles
    index = sl.build_needles(sl.collect_sources(needles=[outside]), handbook_dump=world["dump"])
    assert _s_hits(window(3), index) == [("t.md:1", FAM_LONG)]
    # a registry private set outside dev/
    eval_registry.add(outside, privacy="private", name="frag-private")
    index = _index(world)
    assert _s_hits(window(3), index) == [("t.md:1", FAM_LONG)]
    # a run input
    run = world["root"] / "runs" / "r-1"
    run.mkdir(parents=True)
    (run / "inputs.json").write_text(
        json.dumps({"version": 1, "inputs": [{"path": str(outside), "sha256": eval_sets.sha256_file(outside), "kind": "questions"}]}),
        encoding="utf-8",
    )
    index = sl.build_needles(sl.collect_sources(run_id="r-1"), handbook_dump=world["dump"])
    assert _s_hits(window(3), index) == [("t.md:1", FAM_LONG)]


def test_c2_a_symlink_in_dev_to_a_file_elsewhere_exempts_nothing(world, tmp_path):
    world["dev"].unlink()
    target = _write_jsonl(tmp_path / "elsewhere" / "frag.jsonl", [{"question": FRAG}])
    os.symlink(target, _mkdir(world["root"] / "dev") / "tuning_link.jsonl")
    index = sl.build_needles(sl.collect_sources(needles=[target]), handbook_dump=world["dump"])
    assert _s_hits(window(3), index) == [("t.md:1", FAM_LONG)]


def test_c2_the_development_splits_predicate(world, tmp_path):
    dev = _mkdir(world["root"] / "dev")
    for name, want in [("tuning_r2.jsonl", True), ("realistic-v2.jsonl", True), ("quarantine_r1.jsonl", False), ("x.jsonl", False)]:
        f = _write_jsonl(dev / name, [{"question": "q"}])
        assert sl._is_development_split(f) is want, name
    assert sl._is_development_split(_write_jsonl(tmp_path / "tuning_r1.jsonl", [{"question": "q"}])) is False


# --- C3: a window or whole question maps to the set of sealed families -----------------------------------
def _two_families(world, q_a=A10, q_b=B11, fam_a=FAM_A, fam_b=FAM_B):
    root = world["root"]
    _write(root / "sealed" / "two.jsonl", [_row(0, fam_a, question=q_a), _row(0, fam_b, question=q_b)])
    return _index(world)


def test_c3_a_shared_window_names_every_family_one_line_each(world):
    index = _two_families(world)
    assert sorted(_s_hits(f"leak: {SHARED}\n", index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]
    # the index keeps one family per key for existing callers and the full set beside it
    key = tuple(SHARED.split())
    assert index.s_windows[key] in (FAM_A, FAM_B) and index.s_families[key] == {FAM_A, FAM_B}


def test_c3_a_window_of_another_family_inside_a_whole_hit_is_not_folded_away(world):
    index = _two_families(world)
    hits = sl.scan_text(f"intro\n{A10}\n", "t.md", index)
    assert sorted((h.target, h.family) for h in hits) == [("t.md:2", FAM_A), ("t.md:2", FAM_B)]
    # the shared window sits at the start of the whole question, so both are reported on its first line
    hits = sl.scan_text(f"intro\n{A10.upper()}\n", "t.md", index)
    assert sorted(h.family for h in hits) == [FAM_A, FAM_B]


def test_c3_the_extra_family_of_a_folded_window_is_reported_at_that_windows_line(world):
    index = _two_families(world, q_b=B_LATE)
    assert sorted(_s_hits(f"leak: {SHARED_LATE}\n", index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]
    # A's whole question wrapped so that the shared window starts on line 2: A is reported where its whole
    # question starts, B where the window it shares with A starts
    text = "alpha\nbravo charlie delta echo foxtrot golf hotel india juliet\n"
    assert sorted((h.target, h.family) for h in sl.scan_text(text, "t.md", index)) == [("t.md:1", FAM_A), ("t.md:2", FAM_B)]


def test_c3_two_families_holding_the_same_question_are_both_named(world):
    root = world["root"]
    q = "marmoset pelican quokka rhea saola tapir urial vicuna wombat"
    _write(root / "sealed" / "one.jsonl", [_row(0, FAM_A, question=q)])
    _write(root / "sealed" / "two.jsonl", [_row(0, FAM_B, question=q)])
    index = _index(world)
    assert sorted(_s_hits(f"x\n{q}\n", index)) == [("t.md:2", FAM_A), ("t.md:2", FAM_B)]
    assert sorted(_s_hits(" ".join(q.split()[1:9]), index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]


def test_c3_two_families_holding_the_same_short_question_are_both_named(world):
    root = world["root"]
    q = "marmoset pelican quokka rhea saola"
    _write(root / "sealed" / "one.jsonl", [_row(0, FAM_A, question=q)])
    _write(root / "sealed" / "two.jsonl", [_row(0, FAM_B, question=q)])
    index = _index(world)
    assert sorted(_s_hits(json.dumps({"q": q}), index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]
    assert sorted(_s_hits(f"intro\\n{q}\\nend", index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]


def test_c3_an_eight_token_question_that_is_another_familys_window_names_both(world):
    root = world["root"]
    eight = A10.split()[:8]
    _write(root / "sealed" / "two.jsonl", [_row(0, FAM_A, question=" ".join(eight)), _row(0, FAM_B, question=B11)])
    index = _index(world)
    assert sorted(_s_hits(f"leak: {SHARED}\n", index)) == [("t.md:1", FAM_A), ("t.md:1", FAM_B)]


def test_c3_a_single_family_is_still_one_hit_and_the_cli_prints_every_family(world, tmp_path, capsys):
    _two_families(world)
    out = tmp_path / "o.txt"
    out.write_text(f"ok\n{SHARED}\n{window(5)}\n", encoding="utf-8")
    assert sl.main(["--output", str(out), "--handbook-dump", str(world["dump"])]) == 5
    cap = capsys.readouterr()
    assert sorted(cap.out.splitlines()) == sorted([f"{out}:2\t{FAM_A}", f"{out}:2\t{FAM_B}", f"{out}:3\t{FAM_LONG}"])
    assert "3 hit(s)" in cap.err
    assert all(len(line.split("\t")) == 2 for line in cap.out.splitlines())  # target and family, nothing else


# --- C4: crash leftovers of an atomic write are record files ----------------------------------------------
LEFTOVERS = [
    "sealed/ledger/payloads/.assignment-1.json.tmp",  # src.sealed_ledger._write_payload_file
    "sealed/work/.approvals.json.ab12cd.tmp",  # sealed_tool.write_atomic
    "sealed/work/.release.journal.0f0f0f.tmp",
    "sealed/work/sheets/.sheet-1.csv.ab12cd.tmp",  # nested under sealed/work/
    "sealed/work/sp_out/0a1b2c3d/.twins.jsonl.ab12cd.tmp",
    "sealed/work/.heldout.jsonl.validate.tmp",
]


@pytest.mark.parametrize("rel", LEFTOVERS)
def test_c4_a_crash_leftover_is_allowed_by_the_precheck(repo, world, capsys, rel):
    _record(world, rel, {"kind": "claim", "status": "ok", "sha256": "a" * 64})
    assert sl.precheck(_index(world).source_shas) == []
    assert _gate(repo, world) == 0
    assert "clean" in capsys.readouterr().err


@pytest.mark.parametrize("rel", LEFTOVERS)
def test_c4_a_crash_leftover_is_scanned_as_a_target(repo, world, capsys, rel):
    planted = _record(world, rel, {"kind": "claim", "note": SHORT5}, {"kind": "claim", "note": DEV_Q})
    assert _gate(repo, world) == 5
    cap = capsys.readouterr()
    lines = cap.out.splitlines()
    assert f"eval/private/{rel}:1\t{FAM_SHORT}" in lines  # a sealed question: target and family id only
    assert [x for x in lines if x.startswith(f"eval/private/{rel}:2\t")]  # a development question: a private hit
    assert SHORT5 not in cap.out + cap.err
    assert sl.main(["--output", str(planted), "--handbook-dump", str(world["dump"])]) == 5
    assert f"{planted}:1\t{FAM_SHORT}" in capsys.readouterr().out.splitlines()


def test_c4_leftovers_are_found_as_record_files_and_no_needle_source(world):
    paths = [_record(world, rel, {"kind": "claim"}) for rel in LEFTOVERS]
    assert {p for p in sl._record_files()} >= set(paths)
    assert not any(".tmp" in s.label for s in sl.collect_sources())


@pytest.mark.parametrize(
    "rel",
    [
        "sealed/ledger/payloads/assignment-1.json.tmp",  # no leading dot: not the writer's name
        "sealed/ledger/payloads/sub/.assignment-1.json.tmp",  # one level only
        "sealed/ledger/.assignment-1.json.tmp",  # not the payloads directory
        "sealed/ledger/payloads/.assignment-1.csv.tmp",  # payloads are .json
        "sealed/.sealed_r1.jsonl.ab12cd.tmp",  # not under sealed/ledger or sealed/work
        "dev/.tuning_r1.jsonl.ab12cd.tmp",
        ".sp_dispatch.jsonl.tmp",
    ],
)
def test_c4_other_tmp_files_are_still_refused(repo, world, capsys, rel):
    stray = _record(world, rel, {"kind": "claim"})
    assert _gate(repo, world) == 7
    assert str(stray) in capsys.readouterr().err


def test_c4_a_symlinked_leftover_is_not_a_record(repo, world, tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    link = _mkdir(world["root"] / "sealed" / "ledger" / "payloads") / ".assignment-1.json.tmp"
    os.symlink(outside, link)
    assert _gate(repo, world) == 7
    assert str(link) in capsys.readouterr().err
