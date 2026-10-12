"""Phase 16A-2a a2 (D72), acceptance (d): the scanner's sealed needles (S-needles).

Every question, section label, inventory and handbook line here is invented and
schema-valid. The fixtures use made-up words (``zorbin``, ``quintoble`` ...) so a
leak of a sealed question into stdout or stderr is a plain substring test. Nothing
reads a real eval set, the corpus or ``eval/private/``; the private root is the
per-test ``tmp_path/eval/private`` (conftest).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import src.eval_sets as eval_sets
from scripts import scan_leaks as sl
from src import eval_schema
from tests.test_p16w_scan_leaks import _gh_fake, _write_jsonl, git
from tests.test_p16w_schema import INVENTORY, _refuse, _row, _write

# --- invented fixtures ----------------------------------------------------------
FAM_SHORT, FAM_LONG, FAM_OTHER, FAM_DEV = "f1111abcd", "f2222abcd", "f3333abcd", "f4444abcd"

SHORT5 = "zorbin keldwyn plimsy tarnock vessuvio"  # a sealed question of 5 tokens

# 16 tokens -> 9 eight-token windows w0..w8. w0 is handbook text, w1 a development question,
# w2 a public question; w3..w8 are not exempt.
T = (
    "the vendor must deliver good marketable title to the purchaser "
    "quintoble flarnwick gostrel mardwyn pellick ostravy"
).split()
LONG = " ".join(T)
LONG_PARAPHRASE = "whenever vendor ordained blorptag nimbrel yarrowtine skeld cormack unweth dasgrave pilloxen"
HANDBOOK_LINE = "General law: the vendor must deliver good marketable title to whoever buys."  # holds w0 only
DEV_Q = "can any vendor must deliver good marketable title to the buyer"  # holds w1 only
PUB_Q = "everyone knows must deliver good marketable title to the purchaser eventually"  # holds w2 only
OTHER_Q = "where did the glombar trundle snorfle bexwick"

INVENTED = (
    "zorbin keldwyn plimsy tarnock vessuvio quintoble flarnwick gostrel mardwyn pellick ostravy "
    "blorptag nimbrel yarrowtine skeld cormack unweth dasgrave pilloxen glombar trundle snorfle bexwick"
).split()


def window(i: int) -> str:
    return " ".join(T[i : i + 8])


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Empty committed registry and legacy list, and a tmp section inventory for ``load_sealed``."""
    reg = tmp_path / "_registry" / "sets.json"
    reg.parent.mkdir()
    reg.write_text(json.dumps({"version": 1, "sets": []}), encoding="utf-8")
    monkeypatch.setattr(eval_sets, "REGISTRY_PATH", reg)
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", tmp_path / "eval" / "legacy_public.json")
    inv = tmp_path / "_registry" / "section_inventory.json"
    inv.write_text(json.dumps(INVENTORY), encoding="utf-8")
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)


def _sealed_rows():
    return [
        _row(0, FAM_SHORT, question=SHORT5),
        _row(0, FAM_LONG, question=LONG),
        _row(1, FAM_LONG, question=LONG_PARAPHRASE),
        _refuse(0, FAM_OTHER, question=OTHER_Q),
    ]


@pytest.fixture
def world(tmp_path, eval_registry, _private_root_in_tmp):
    """A sealed set, a development set, a public set, a handbook dump and a repo-less scan setup."""
    root = _private_root_in_tmp
    sealed = _write(_mkdir(root / "sealed") / "set.jsonl", _sealed_rows())
    dev = _write(_mkdir(root / "dev") / "dev.jsonl", [_row(0, FAM_DEV, question=DEV_Q)])
    pub = _write_jsonl(tmp_path / "public" / "golden.jsonl", [{"question": PUB_Q, "type": "direct", "expected_sections": ["1.1"]}])
    eval_registry.add(pub, privacy="public", name="golden-public")
    dump = tmp_path / "handbook.jsonl"
    dump.write_text(
        json.dumps({"section": "3.2.1", "pages": [10, 11], "text": HANDBOOK_LINE})
        + "\n\nnot json at all\n"
        + json.dumps({"section": "3.2.2", "pages": [12, 12], "text": "A different paragraph, wholly unrelated."})
        + "\n",
        encoding="utf-8",
    )
    return {"sealed": sealed, "dev": dev, "public": pub, "dump": dump, "root": root}


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index(world, **kw):
    kw.setdefault("handbook_dump", world["dump"])
    return sl.build_needles(sl.collect_sources(), **kw)


def _s_hits(text: str, index, target: str = "t.md"):
    return [(h.target, h.family) for h in sl.scan_text(text, target, index) if h.family is not None]


def _all_forbidden(world):
    """Everything that must never reach stdout or stderr: invented words, whole questions, needle ids."""
    words = set(INVENTED)
    ids = set()
    for q in (SHORT5, LONG, LONG_PARAPHRASE, OTHER_Q):
        toks = sl.normalise_tokens(q)
        ids.add(sl.needle_id(toks))
        ids.update(sl.needle_id(toks[i : i + 8]) for i in range(len(toks) - 7))
    return words, ids


def _assert_clean(world, text: str) -> None:
    words, ids = _all_forbidden(world)
    low = text.lower()
    assert not [w for w in words if w in low], "a sealed word reached the output"
    assert not [i for i in ids if i in text], "a needle id reached the output"
    assert "p16-canary" not in low


# --- the loader, and `sealed` must be true ----------------------------------------
def test_sealed_sources_are_read_only_through_load_sealed(world, monkeypatch):
    loaded, lenient = [], []
    real_load, real_iter = sl.load_sealed, sl._iter_questions
    monkeypatch.setattr(sl, "load_sealed", lambda p, **kw: (loaded.append(Path(p)), real_load(p, **kw))[1])
    monkeypatch.setattr(sl, "_iter_questions", lambda p: (lenient.append(Path(p)), real_iter(p))[1])
    index = _index(world)
    assert loaded == [world["sealed"]]
    assert world["sealed"] not in lenient and world["dev"] in lenient  # private sets keep the lenient reader
    assert set(index.s_short.values()) == {FAM_SHORT, FAM_OTHER} and FAM_LONG in set(index.s_whole.values())


def test_sealed_key_must_be_true_via_the_loader(world, tmp_path, capsys):
    rows = _sealed_rows()
    rows[1] = dict(rows[1], sealed=False)
    _write(world["sealed"], rows)
    with pytest.raises(sl.ScanRefusal) as exc:
        _index(world)
    assert exc.value.code == 2 and exc.value.reason == "sealed source does not validate"
    assert any("sealed: must be true" in p for p in exc.value.paths)
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")
    assert sl.main(["--output", str(out), "--handbook-dump", str(world["dump"])]) == 2
    cap = capsys.readouterr()
    assert "line 2: sealed: must be true" in cap.err
    _assert_clean(world, cap.out + cap.err)


def test_a_true_marker_is_what_makes_a_file_sealed_and_a_stray_false_one_refuses(tmp_path, world, capsys):
    marked = _write(_mkdir(tmp_path / "elsewhere") / "marked.jsonl", [_row(0, FAM_SHORT, question=SHORT5, sealed=True)])
    out = tmp_path / "o.txt"
    out.write_text(f"x\n{SHORT5}\n", encoding="utf-8")
    assert sl.main(["--output", str(out), "--needles", str(marked), "--handbook-dump", str(world["dump"])]) == 5
    assert capsys.readouterr().out.splitlines() == [f"{out}:2\t{FAM_SHORT}"]
    mixed = _write(_mkdir(tmp_path / "elsewhere") / "mixed.jsonl", [_row(0, FAM_SHORT, question=SHORT5, sealed=True), _row(1, FAM_SHORT, question=OTHER_Q, sealed=False)])
    assert sl.main(["--output", str(out), "--needles", str(mixed), "--handbook-dump", str(world["dump"])]) == 2
    _assert_clean(world, "".join(capsys.readouterr()))


def test_an_invalid_sealed_set_is_exit_2_not_scanned_half_way(world, capsys):
    _write(world["sealed"], [_row(0, FAM_SHORT, question=SHORT5), _row(1, FAM_SHORT, question=SHORT5)])  # duplicate question
    with pytest.raises(sl.ScanRefusal) as exc:
        _index(world)
    assert exc.value.code == 2
    assert not any(w in " ".join(exc.value.paths).lower() for w in INVENTED)


# --- a 5-token sealed question -----------------------------------------------------
def test_a_short_sealed_question_has_no_window_needle(world):
    index = _index(world)
    short = tuple(sl.normalise_tokens(SHORT5))
    assert index.s_short[short] == FAM_SHORT
    assert short not in index.s_whole and not index.s_windows.keys() & {short}
    assert all(set(w).isdisjoint(short) for w in index.s_windows)  # no needle built from it
    assert all(set(w).isdisjoint(short) for w in index.windows)
    # and it is not a private whole needle either: running text does not match it
    assert short not in index.whole


@pytest.mark.parametrize(
    "text,line",
    [
        (json.dumps({"note": SHORT5}), 1),  # a whole JSON string value
        ('{\n  "note": "' + SHORT5.upper() + '"\n}', 2),  # pretty-printed, re-cased
        (json.dumps(["x", SHORT5]), 1),  # an array element
        (json.dumps({SHORT5: 1}), 1),  # a key
        (json.dumps(json.dumps({"q": SHORT5})), 1),  # nested serialisation
        ('INFO payload={"q": "' + SHORT5 + '", "n": 3}', 1),  # JSON embedded in a log line
        ('{"q": "' + "".join(f"\\u{ord(c):04x}" for c in SHORT5) + '"}', 1),  # every character \uXXXX
        ("intro\n" + SHORT5, 2),  # a whole line
        ("intro\n  " + SHORT5.capitalize() + "?  ", 2),  # whole line, re-cased, punctuated
        ("intro\n- " + SHORT5, 2),  # a bullet is punctuation
    ],
)
def test_a_5_token_sealed_question_is_caught_as_a_whole_value_or_a_whole_line(world, text, line):
    assert _s_hits(text, _index(world)) == [(f"t.md:{line}", FAM_SHORT)]


@pytest.mark.parametrize(
    "text",
    [
        f"the client asked {SHORT5} yesterday",  # in prose
        json.dumps({"note": f"asked {SHORT5} today"}),  # in prose inside a JSON string
        SHORT5 + " again",  # a longer line
        "zorbin keldwyn plimsy tarnock",  # a fragment
        "zorbin keldwyn\nplimsy tarnock vessuvio",  # wrapped over two lines
        "1. " + SHORT5,  # a numbered item is prose (the documented residual)
        "question: " + SHORT5,  # unquoted key: value (the documented residual)
    ],
)
def test_a_5_token_sealed_question_is_not_caught_inside_prose(world, text):
    assert sl.scan_text(text, "t.md", _index(world)) == []


# --- a long sealed question, and what is exempt --------------------------------------
def test_a_long_sealed_question_is_caught_whole_and_by_family(world):
    index = _index(world)
    assert _s_hits(f"preamble\n{LONG}\n", index) == [("t.md:2", FAM_LONG)]  # windows fold into the whole
    assert _s_hits(json.dumps({"x": LONG}), index) == [("t.md:1", FAM_LONG)]
    assert _s_hits(LONG_PARAPHRASE.upper(), index) == [("t.md:1", FAM_LONG)]  # a paraphrase: same family
    assert _s_hits(" ".join(T[:5]) + "\n" + " ".join(T[5:]), index) == [("t.md:1", FAM_LONG)]  # wrapped


def test_handbook_development_and_public_windows_are_exempt_and_the_rest_are_not(world):
    index = _index(world)
    exempt = [window(0), window(1), window(2)]  # handbook, development question, public question
    assert not any(tuple(w.split()) in index.s_windows for w in exempt)
    assert all(tuple(window(i).split()) in index.s_windows for i in range(3, 9))
    assert index.s_exempt_windows == 3
    for w, name in zip(exempt, ("handbook", "development", "public")):
        assert _s_hits(w, index) == [], name
    # ordinary conveyancing wording, as the three sources phrase it, raises no S-hit
    assert _s_hits("\n".join([HANDBOOK_LINE, DEV_Q, PUB_Q, " ".join(T[:10])]), index) == []
    # a positive control: the first non-exempt window does
    assert _s_hits(window(3), index) == [("t.md:1", FAM_LONG)]
    assert _s_hits(" ".join(T[:11]), index) == [("t.md:1", FAM_LONG)]


def test_without_the_handbook_dump_its_window_is_a_hit_and_the_scan_says_so(world, tmp_path):
    notes = []
    index = sl.build_needles(sl.collect_sources(), handbook_dump=tmp_path / "no-such-dump.jsonl", notes=notes)
    assert [n for n in notes if "handbook dump absent" in n]
    assert tuple(window(0).split()) in index.s_windows and index.s_exempt_windows == 2
    assert _s_hits(window(0), index) == [("t.md:1", FAM_LONG)]
    # the default path (data/research/handbook_sections.jsonl under the repo base) is absent here: no crash, a note
    notes.clear()
    sl.build_needles(sl.collect_sources(), notes=notes)
    assert [n for n in notes if "handbook dump absent" in n]


def test_a_dump_that_is_a_directory_or_has_bad_lines_does_not_crash(world, tmp_path):
    as_dir = tmp_path / "adir"
    as_dir.mkdir()
    for dump in (as_dir, world["dump"]):
        assert sl.build_needles(sl.collect_sources(), handbook_dump=dump)


def test_a_short_question_equal_to_a_public_question_is_exempt(world, tmp_path, eval_registry):
    pub2 = _write_jsonl(tmp_path / "public" / "more.jsonl", [{"question": SHORT5, "type": "direct", "expected_sections": ["1.1"]}])
    eval_registry.add(pub2, privacy="public", name="more-public")
    index = _index(world)
    assert tuple(sl.normalise_tokens(SHORT5)) not in index.s_short and index.s_exempt_whole == 1
    assert _s_hits(SHORT5, index) == []


def test_a_private_needle_still_carries_its_needle_id_and_an_s_hit_never_does(world):
    index = _index(world)
    hits = sl.scan_text(f"{DEV_Q}\n{SHORT5}\n", "t.md", index)
    private = [h for h in hits if h.family is None]
    sealed = [h for h in hits if h.family is not None]
    assert private and all(h.needle.startswith("n") for h in private)
    assert [h.line() for h in sealed] == [f"t.md:2\t{FAM_SHORT}"]
    assert (sealed[0].needle, sealed[0].source, sealed[0].kind) == ("", "", "S")


# --- sources: found automatically ----------------------------------------------------
def test_every_jsonl_under_the_private_root_is_a_needle_source_outside_runs_and_artifacts(world):
    root = world["root"]
    _write(_mkdir(root / "sealed" / "pool") / "draft.jsonl", [_row(0, "f5555abcd", question="pooled sealed draft quark")])
    _write(_mkdir(root / "sealed" / "work") / "w.jsonl", [_row(0, "f6666abcd", question="worked sealed draft quark")])
    _write_jsonl(root / "work" / "scratch.jsonl", [{"question": "scratch private quark"}])
    # inside the traced areas a .jsonl is not a source (and need not validate)
    (root / "runs" / "r-1").mkdir(parents=True)
    (root / "runs" / "r-1" / "rows.jsonl").write_text("not json\n", encoding="utf-8")
    (root / "sealed" / "runs" / "r-2").mkdir(parents=True)
    (root / "sealed" / "runs" / "r-2" / "rows.jsonl").write_text("not json\n", encoding="utf-8")
    sources = sl.collect_sources()
    by_label = {s.label: s.sealed for s in sources}
    assert by_label == {
        "eval/private/dev/dev.jsonl": False,
        "eval/private/sealed/set.jsonl": True,
        "eval/private/sealed/pool/draft.jsonl": True,
        "eval/private/sealed/work/w.jsonl": True,
        "eval/private/work/scratch.jsonl": False,
    }
    index = sl.build_needles(sources, handbook_dump=world["dump"])
    assert {"f5555abcd", "f6666abcd"} <= set(index.s_short.values())


def test_a_sealed_needles_path_is_accepted_and_a_file_reached_twice_is_one_source(world, tmp_path):
    sources = sl.collect_sources(needles=[world["sealed"]])
    assert [s.label for s in sources if s.sealed] == [str(world["sealed"])]  # the explicit label wins


# --- pre-check: non-JSONL files and `work/` --------------------------------------------
GIT_IGNORE = "eval/private/\n_registry/\n"


@pytest.fixture
def repo(tmp_path, world):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".gitignore").write_text(GIT_IGNORE, encoding="utf-8")
    (tmp_path / "README.md").write_text("synthetic repo\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore", "README.md")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "branch", "base")
    return tmp_path


def _gate(repo, world, *extra):
    return sl.main(["--merge-gate", "--base", "base", "--repo", str(repo), "--handbook-dump", str(world["dump"]), *extra])


@pytest.mark.parametrize(
    "rel",
    ["notes.txt", "dev/notes.md", "sealed/notes.txt", "sealed/pool/readme.md", "approvals.json",
     "dev/work/notes.txt", "sealed/dev/work/notes.txt", "workbench/notes.txt"],
)
def test_a_non_jsonl_file_outside_work_exits_7(repo, world, capsys, rel):
    stray = _mkdir((world["root"] / rel).parent) / Path(rel).name
    stray.write_text(f"{SHORT5}\n", encoding="utf-8")
    assert _gate(repo, world) == 7
    cap = capsys.readouterr()
    assert str(stray) in cap.err and cap.out == ""
    _assert_clean(world, cap.out + cap.err)


@pytest.mark.parametrize("rel", ["work/notes.txt", "work/sub/draft.md", "sealed/work/notes.txt", "sealed/work/approvals.json"])
def test_a_non_jsonl_file_inside_work_passes(repo, world, capsys, rel):
    f = _mkdir((world["root"] / rel).parent) / Path(rel).name
    f.write_text(f"{SHORT5}\n", encoding="utf-8")
    assert _gate(repo, world) == 0
    assert "clean" in capsys.readouterr().err


def test_a_symlink_to_a_file_that_is_not_a_source_exits_7(repo, world, tmp_path, capsys):
    outside = _write_jsonl(tmp_path / "outside" / "x.jsonl", [{"question": "some other quark"}])
    os.symlink(outside, world["root"] / "link.jsonl")
    assert _gate(repo, world) == 7
    assert str(world["root"] / "link.jsonl") in capsys.readouterr().err


def test_a_jsonl_inside_work_is_still_a_source_and_must_validate_if_sealed(repo, world, capsys):
    _mkdir(world["root"] / "sealed" / "work")
    (world["root"] / "sealed" / "work" / "w.jsonl").write_text('{"not": "a sealed row"}\n', encoding="utf-8")
    assert _gate(repo, world) == 2
    cap = capsys.readouterr()
    assert "sealed source does not validate" in cap.err and cap.out == ""


# --- the sealed run and artifact areas are traced like runs/ and artifacts/ ------------
def _inputs(root: Path, area: str, run_id: str, items) -> Path:
    path = root / area / run_id / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "inputs": items}), encoding="utf-8")
    return path


def test_sealed_run_inputs_are_checked_like_run_inputs(repo, world, capsys):
    q = _write(_mkdir(world["root"] / "sealed" / "runs" / "r-sealed") / "q.jsonl", [_row(0, "f7777abcd", question="a run local sealed quark")])
    _inputs(world["root"], "sealed/runs", "r-sealed", [{"path": str(q), "sha256": eval_sets.sha256_file(q), "kind": "questions"}])
    assert _gate(repo, world) == 7  # a sealed/runs file is not an automatic source, so the input is not one
    cap = capsys.readouterr()
    assert str(q) in cap.err and cap.out == ""
    _assert_clean(world, cap.err)
    assert _gate(repo, world, "--needles", str(q)) == 0  # offered as a source (a sealed one), it passes


def test_output_run_finds_a_sealed_run_dir_and_scans_its_inputs_as_s_needles(world, tmp_path, capsys):
    q = _write(_mkdir(world["root"] / "sealed" / "runs" / "r-sealed") / "q.jsonl", [_row(0, "f7777abcd", question="a run local sealed quark")])
    _inputs(world["root"], "sealed/runs", "r-sealed", [{"path": str(q), "sha256": eval_sets.sha256_file(q), "kind": "questions"}])
    out = tmp_path / "report.md"
    out.write_text("x\nA run-local sealed quark!\n", encoding="utf-8")
    assert sl.scan_output([out], handbook_dump=world["dump"]) == []  # not an automatic source without --run
    assert sl.main(["--output", str(out), "--run", "r-sealed", "--handbook-dump", str(world["dump"])]) == 5
    assert capsys.readouterr().out.splitlines() == [f"{out}:2\tf7777abcd"]
    q.write_text(q.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # drifted since the run
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.scan_output([out], run_id="r-sealed", handbook_dump=world["dump"])
    assert exc.value.code == 2
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.scan_output([out], run_id="no-such-run", handbook_dump=world["dump"])
    assert exc.value.code == 2


def _sealed_artifact(path: Path, source: Path, rows) -> Path:
    from src.expansion_artifact import build_artifact, save_artifact
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    prefix = eval_sets.sha256_file(source)[:16]
    keyed = [(f"{prefix}/{r['id']}", r["question"]) for r in rows]
    art = build_artifact(keyed, lambda q: Expansion(q, ("alpha beta",), REWRITE_MODEL, STATUS_LIVE),
                         [{"path": str(source), "sha256": eval_sets.sha256_file(source), "kind": "questions"}])
    path.parent.mkdir(parents=True, exist_ok=True)
    save_artifact(path, art)
    return path


def test_sealed_artifacts_pass_when_bound_to_real_sealed_rows_and_fail_when_not(repo, world):
    art = _sealed_artifact(world["root"] / "sealed" / "artifacts" / "exp.json", world["sealed"], _sealed_rows())
    assert sl.precheck(_index(world).source_shas) == []
    assert _gate(repo, world) == 0
    doc = json.loads(art.read_text(encoding="utf-8"))
    key = next(iter(doc["entries"]))
    doc["entries"][key.split("/")[0] + "/f9999abcd-0"] = doc["entries"].pop(key)  # no such sealed row
    art.write_text(json.dumps(doc), encoding="utf-8")
    assert [Path(p).name for p in sl.precheck(_index(world).source_shas)] == ["exp.json"]
    assert _gate(repo, world) == 7


# --- S-hits print the target and the family id only ----------------------------------------
def _fake_gh(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir(exist_ok=True)
    payload = json.dumps({"title": "t", "body": body, "comments": [], "reviews": []})
    fake.write_text(
        f"#!{sys.executable}\nimport sys\nprint({payload!r} if sys.argv[1:3] == ['pr', 'view'] else '[]')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def test_s_hits_in_a_file_a_blob_a_commit_message_and_a_pr_body_print_target_and_family_only(repo, world, tmp_path, capsys):
    world["dev"].unlink()  # a development question's window inside LONG would add a private (n-id) hit
    (repo / "leak.txt").write_text(f"intro\n{SHORT5}\n", encoding="utf-8")  # a whole line, in a tracked file
    (repo / "gone.txt").write_text(json.dumps({"q": SHORT5}) + "\n", encoding="utf-8")  # a whole JSON value
    git(repo, "add", "leak.txt", "gone.txt")
    git(repo, "commit", "-q", "-m", "add notes")
    added = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "gone.txt")  # only a historic blob in the range now
    git(repo, "commit", "-q", "-m", f"subject\n\nwhy: {LONG}")  # a whole long question in a commit message
    msg_commit = git(repo, "rev-parse", "HEAD")
    fake = _fake_gh(tmp_path, json.dumps(LONG.upper()))  # and in a PR body
    assert _gate(repo, world, "--pr", "7", "--gh", str(fake)) == 5
    cap = capsys.readouterr()
    assert sorted(cap.out.splitlines()) == sorted(
        [
            f"leak.txt:2\t{FAM_SHORT}",
            f"gone.txt@{added[:12]}:1\t{FAM_SHORT}",
            f"commit {msg_commit[:12]}:3\t{FAM_LONG}",
            f"PR#7 body:1\t{FAM_LONG}",
        ]
    )
    assert all(len(line.split("\t")) == 2 for line in cap.out.splitlines())
    _assert_clean(world, cap.out + cap.err)


def test_s_hits_with_a_faked_pr_runner_in_a_file_via_output_mode(repo, world, tmp_path, capsys):
    world["dev"].unlink()
    gh = _gh_fake(f"see {LONG}", [SHORT5, "fine"], ["fine", f"{window(4)}"])
    hits = sl.merge_gate("base", repo=repo, pr=3, gh_runner=gh, handbook_dump=world["dump"])
    assert sorted((h.target, h.family or "") for h in hits) == [
        ("PR#3 body:1", FAM_LONG),
        ("PR#3 comment[0]:1", FAM_SHORT),
        ("PR#3 review-comment[1]:1", FAM_LONG),
    ]
    out = tmp_path / "stdout.txt"
    out.write_text(f"ok\n{window(5)}\n{SHORT5}\n", encoding="utf-8")
    assert sl.main(["--output", str(out), "--handbook-dump", str(world["dump"])]) == 5
    cap = capsys.readouterr()
    assert cap.out.splitlines() == [f"{out}:2\t{FAM_LONG}", f"{out}:3\t{FAM_SHORT}"]
    assert "2 hit(s)" in cap.err
    _assert_clean(world, cap.out + cap.err)


def test_a_clean_tree_with_sealed_sources_present_is_exit_0(repo, world, capsys):
    prose = f"the client asked {SHORT5} yesterday, and a fragment: zorbin keldwyn plimsy"
    (repo / "notes.md").write_text("\n".join([HANDBOOK_LINE, PUB_Q, window(0), window(2), prose]) + "\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "docs")
    assert _gate(repo, world) == 0
    cap = capsys.readouterr()
    assert cap.out == "" and "clean" in cap.err


def test_development_question_text_raises_private_hits_but_no_s_hit(repo, world, capsys):
    (repo / "notes.md").write_text(f"{HANDBOOK_LINE}\n{DEV_Q.upper()}\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "docs")
    assert _gate(repo, world) == 5  # the development set is itself a (private) needle source
    out = capsys.readouterr().out.splitlines()
    assert out and all(len(line.split("\t")) == 4 for line in out)  # private hits only: id, kind, source
    assert not [line for line in out if FAM_LONG in line or FAM_SHORT in line]


# --- record files: a4's ledger copy and payloads, a7's dispatch logs --------------------------
RECORDS = [
    "sealed/ledger/sealed_ledger.jsonl",
    "sealed/ledger/payloads/assignment-1.json",
    "sealed/work/sp_out/0a1b2c3d/twins.jsonl",  # the SP's released twins verdict (ids only), a7
    "sp_dispatch.jsonl",
    "sp_probe.jsonl",
]


def _record(world, rel: str, *rows) -> Path:
    path = _mkdir((world["root"] / rel).parent) / Path(rel).name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_record_files_are_no_needle_source_and_raise_no_exit_2_or_7(repo, world, monkeypatch, capsys):
    clean = {"kind": "claim", "slot": "s-1", "sha256": "a" * 64, "count": 3, "status": "ok"}
    for rel in RECORDS:
        _record(world, rel, clean)
    loaded, lenient = [], []
    real_load, real_iter = sl.load_sealed, sl._iter_questions
    monkeypatch.setattr(sl, "load_sealed", lambda p, **kw: (loaded.append(Path(p)), real_load(p, **kw))[1])
    monkeypatch.setattr(sl, "_iter_questions", lambda p: (lenient.append(Path(p)), real_iter(p))[1])
    labels = {s.label for s in sl.collect_sources()}
    assert not any(rel in label for rel in RECORDS for label in labels)  # none is a source
    assert _gate(repo, world) == 0
    assert "clean" in capsys.readouterr().err
    records = {world["root"] / rel for rel in RECORDS}
    assert not records & set(loaded + lenient)  # never handed to load_sealed or the set loader
    assert loaded == [world["sealed"]]  # the sealed set itself was still read, through the loader


@pytest.mark.parametrize(
    "rel",
    [
        "sealed/ledger/notes.txt",  # not a record
        "sealed/ledger/payloads/x.txt",  # a payload is .json
        "sealed/ledger/payloads/sub/x.json",  # one level only
        "sealed/ledger/other.json",
        "sp_dispatch.json",
        "dev/sp_dispatch.jsonl.bak",
    ],
)
def test_only_the_named_record_files_are_allowed(repo, world, capsys, rel):
    stray = _record(world, rel, {"kind": "claim"})
    assert _gate(repo, world) == 7
    assert str(stray) in capsys.readouterr().err


def test_a_symlinked_record_file_is_not_allowed(repo, world, tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    os.symlink(outside, world["root"] / "sp_dispatch.jsonl")
    assert _gate(repo, world) == 7
    assert str(world["root"] / "sp_dispatch.jsonl") in capsys.readouterr().err


@pytest.mark.parametrize("rel", RECORDS)
def test_a_canary_question_planted_in_a_record_file_is_a_hit(repo, world, capsys, rel):
    planted = _record(world, rel, {"kind": "claim", "note": SHORT5}, {"kind": "claim", "note": DEV_Q})
    assert _gate(repo, world) == 5  # no exit 2 or 7 first: the file is allowed, then scanned as a target
    cap = capsys.readouterr()
    lines = cap.out.splitlines()
    target = f"eval/private/{rel}"
    assert f"{target}:1\t{FAM_SHORT}" in lines  # a sealed question: target and family id only
    private = [line.split("\t") for line in lines if line.startswith(f"{target}:2\t")]
    assert private and all(len(p) == 4 and p[1].startswith("n") for p in private)  # a development question
    _assert_clean(world, cap.out + cap.err)
    # --output mode scans the same file as a target
    assert sl.main(["--output", str(planted), "--handbook-dump", str(world["dump"])]) == 5
    out = capsys.readouterr().out.splitlines()
    assert f"{planted}:1\t{FAM_SHORT}" in out
    assert [line for line in out if line.startswith(f"{planted}:2\t")]


# --- a7's sp_out directory: twins.jsonl is a record, pool.jsonl a sealed needle source ------------
POOL_Q = "unique plimsy tarnock quintoble gostrel vessuvio mardwyn pellick ostravy"


def test_an_sp_twins_verdict_is_no_source_and_the_pool_beside_it_stays_a_sealed_source(repo, world, capsys):
    out = _mkdir(world["root"] / "sealed" / "work" / "sp_out" / "0a1b2c3d")
    (out / "twins.jsonl").write_text(
        json.dumps({"a": FAM_LONG, "b": "q:0123456789ab", "verdict": "twin"}) + "\n", encoding="utf-8"
    )
    (out / "return.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")  # non-JSONL under work/: allowed
    (out / "tutorial_map.json").write_text(json.dumps({FAM_LONG: "T05"}), encoding="utf-8")
    assert not any("twins.jsonl" in s.label for s in sl.collect_sources())  # ids only: not a needle source
    assert _gate(repo, world) == 0
    capsys.readouterr()
    # the pool the SP drafted into the same directory holds questions: a sealed source, hit anywhere else
    _write(out / "pool.jsonl", [_row(0, "f5555abcd", question=POOL_Q)])
    pools = [s for s in sl.collect_sources() if s.label.endswith("sp_out/0a1b2c3d/pool.jsonl")]
    assert len(pools) == 1 and pools[0].sealed
    (repo / "notes.md").write_text(POOL_Q + "\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "notes")
    assert _gate(repo, world) == 5
    assert "f5555abcd" in capsys.readouterr().out


def test_a_twins_file_below_the_one_sp_out_level_is_not_a_record(repo, world, capsys):
    deeper = _mkdir(world["root"] / "sealed" / "work" / "sp_out" / "0a1b2c3d" / "more")
    (deeper / "twins.jsonl").write_text(
        json.dumps({"a": FAM_LONG, "b": FAM_OTHER, "verdict": "twin"}) + "\n", encoding="utf-8"
    )
    assert _gate(repo, world) == 2  # a sealed source that fails the loader: no record exemption this deep
