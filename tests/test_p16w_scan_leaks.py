"""Phase 16A-1 item 1, acceptance (i): scripts/scan_leaks.py on synthetic fixtures only.

Every question below is invented vocabulary; no real eval row is read. The
committed registry and legacy list are swapped for empty tmp copies so the
scanner sees only the sets each test registers.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import src.eval_sets as eval_sets
from scripts import scan_leaks as sl

# --- invented fixture questions --------------------------------------------
PRIV9 = "vexmor talquin brisset oomlark fendrow quazzle mirthop selvane drubbik"
PUB_W1 = "glimmet vexmor talquin brisset oomlark fendrow quazzle mirthop selvane"  # holds window 1
PUB_W2 = "talquin brisset oomlark fendrow quazzle mirthop selvane drubbik yarrowin"  # holds window 2
PUB_EXACT = "Kesslor varnith ploddy quarrent obstin melvary torrisk?"
PARAPHRASE = "kesslor varnith ploddy quarrent obstin melvary torrisk zembleton"
SHORT = "plinkra dossivel umbrath"
ESCAPEE = "crandle wispen torvald migrane hoblet sarrow plimbit dazzock fenwhistle"

PRIVATE_ROWS = [
    {"question": PRIV9, "type": "definition"},
    {"question": PUB_EXACT, "type": "definition"},
    {"question": PARAPHRASE, "type": "definition", "source": "legacy"},
    {"question": SHORT, "type": "definition"},
    {"schema": 2, "id": "r-5", "family_id": "f-5", "question": ESCAPEE, "type": "procedure"},
]
PUBLIC_ROWS = [{"question": q, "type": "definition"} for q in (PUB_W1, PUB_W2, PUB_EXACT)]

FIXTURE_TOKENS = {
    t
    for q in (PRIV9, PUB_W1, PUB_W2, PUB_EXACT, PARAPHRASE, SHORT, ESCAPEE)
    for t in sl.normalise_tokens(q)
    if len(t) >= 5
}


def _ids(q: str) -> str:
    return sl.needle_id(sl.normalise_tokens(q))


def _write_jsonl(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Empty committed registry and legacy list: only test-registered sets exist."""
    reg = tmp_path / "_registry" / "sets.json"
    reg.parent.mkdir()
    reg.write_text(json.dumps({"version": 1, "sets": []}), encoding="utf-8")
    monkeypatch.setattr(eval_sets, "REGISTRY_PATH", reg)
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", tmp_path / "eval" / "legacy_public.json")


@pytest.fixture
def sets(tmp_path, eval_registry, _private_root_in_tmp):
    """A registered private set (under the private root) and a registered public set."""
    priv = _write_jsonl(_private_root_in_tmp / "sets" / "dev.jsonl", PRIVATE_ROWS)
    pub = _write_jsonl(tmp_path / "public" / "golden.jsonl", PUBLIC_ROWS)
    eval_registry.add(priv, privacy="private", name="dev-private")
    eval_registry.add(pub, privacy="public", name="golden-public")
    return {"private": priv, "public": pub}


def _scan(text: str):
    index = sl.build_needles(sl.collect_sources())
    return sl.scan_text(text, "t.md", index)


def _assert_no_fixture_tokens(text: str) -> None:
    low = text.lower()
    leaked = [t for t in FIXTURE_TOKENS if t in low]
    assert not leaked, "a fixture token reached the scanner's output"


# --- normalisation and needles ---------------------------------------------
def test_needle_id_shape():
    nid = _ids(PRIV9)
    assert nid.startswith("n") and len(nid) == 11 and int(nid[1:], 16) >= 0


def test_nine_token_question_caught_whole_when_windows_are_public(sets):
    index = sl.build_needles(sl.collect_sources())
    toks = tuple(sl.normalise_tokens(PRIV9))
    assert toks[:8] not in index.windows and toks[1:] not in index.windows  # both exempt
    hits = sl.scan_text(f"intro\n{PRIV9}\n", "t.md", index)
    assert [(h.target, h.needle, h.kind, h.source) for h in hits] == [
        ("t.md:2", _ids(PRIV9), "whole", "dev-private")
    ]
    # each public question alone carries only an exempt window: no hit
    assert sl.scan_text(f"{PUB_W1}\n{PUB_W2}\n", "t.md", index) == []


def test_exact_public_question_exempt_but_legacy_paraphrase_is_not(sets):
    assert _scan(f"x {PUB_EXACT} y") == []
    hits = _scan(f"x {PARAPHRASE} y")
    assert {(h.needle, h.kind) for h in hits} == {(_ids(PARAPHRASE), "whole")}


def test_short_question_has_only_its_whole_needle(sets):
    index = sl.build_needles(sl.collect_sources())
    short = tuple(sl.normalise_tokens(SHORT))
    assert short in index.whole
    assert not any(set(w) & set(short) for w in index.windows)
    hits = sl.scan_text(f"note: {SHORT.upper()}!", "t.md", index)
    assert [(h.kind, h.needle) for h in hits] == [("whole", _ids(SHORT))]
    # a fragment shorter than the whole short needle is not a needle
    assert sl.scan_text("plinkra dossivel", "t.md", index) == []


def test_window_of_long_question_caught(sets):
    toks = ESCAPEE.split()
    hits = _scan("prefix " + " ".join(toks[1:]) + " suffix")
    assert [(h.kind, h.needle) for h in hits] == [("window", sl.needle_id(toks[1:]))]


def _fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else "　" for c in s)


@pytest.mark.parametrize(
    "inject",
    [
        lambda q: json.dumps({"q": q}),  # JSON-serialised
        lambda q: json.dumps(json.dumps(q)),  # double-escaped
        lambda q: "".join(f"\\u{ord(c):04x}" for c in q),  # every char \uXXXX
        lambda q: json.dumps(q.replace(" ", "\n")),  # \n escapes
        lambda q: "".join(c.upper() if i % 2 else c for i, c in enumerate(q)),  # re-cased
        lambda q: "  \t ".join(q.split()),  # re-spaced
        lambda q: "\n".join(q.split()),  # wrapped across lines
        lambda q: "-".join(q.split()[:3]) + ", " + "; ".join(q.split()[3:]) + "?!",  # punctuation
        lambda q: "&quot;" + "&#32;".join(q.split()) + "&quot;",  # HTML entities
        _fullwidth,  # NFKC
    ],
)
def test_escaped_injections_caught(sets, inject):
    hits = _scan("before " + inject(ESCAPEE) + " after")
    assert (_ids(ESCAPEE), "whole") in {(h.needle, h.kind) for h in hits}


def _v5_report(public_path: Path, sha: str) -> str:
    """A synthetic report in the v5 shape (src/evaluator.py _format_matrix_report) with public questions only."""
    lines = [
        "# Legal RAG Evaluation Report v5 (held-out + realistic, ablated)",
        "",
        "- Date: 2026-10-10T00:00:00",
        "- top_k: 6",
        "- Retrieval modes ablated: bm25, vector, hybrid, hybrid+rewrite",
        "- Canonical run (writes the committed report): False",
        "",
        "## Provenance",
        "",
        "- git sha: 0123abc (clean)",
        "- passes: retrieval ablation, refusals, completeness, judge",
        "- query expansion: disabled (offline run)",
        "",
        "Question sets:",
        "- golden (tuning): tuning (used to select fusion constants, D31 — NOT held-out)",
        f"  - path: {public_path}",
        f"  - sha256: {sha}",
        "  - question counts: definition=3 (n=3)",
        "",
        "## Headline: strict hit@6 on the held-out set (hybrid)",
        "",
        "**strict hit@6 = 2/3 = 0.667** (95% Wilson CI 0.208–0.939), set: golden (tuning).",
        "",
        "## golden (tuning) — retrieval ablation",
        "",
        "| Mode | S@1 | S@6 | R@1 | R@6 | MRR@6 strict | MRR@6 related | n |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        "| hybrid | 0.333 | 0.667 | 0.333 | 0.667 | 0.444 | 0.444 | 3 |",
        "",
        "## golden (tuning) — per-question detail (hybrid+rewrite)",
        "",
    ]
    for q in (PUB_W1, PUB_W2, PUB_EXACT):
        lines.append(
            "- [definition] strict=HIT(rank=1) related=HIT(rank=1) expected=['3.2'] "
            f"retrieved=['3.2', '4.1'] gate=supported :: {q}"
        )
    lines.append(f"- [refusal] refused caveat=False gate=refused grounded=0/0 :: {PUB_EXACT}")
    return "\n".join(lines) + "\n"


def test_clean_v5_report_has_zero_hits(sets, tmp_path, capsys):
    report = tmp_path / "out" / "report.md"
    report.parent.mkdir()
    report.write_text(_v5_report(sets["public"], eval_sets.sha256_file(sets["public"])), encoding="utf-8")
    assert sl.scan_output([report]) == []
    assert sl.main(["--output", str(report)]) == 0


def test_output_mode_hit_exit5_and_no_fixture_tokens(sets, tmp_path, capsys):
    out = tmp_path / "out" / "stdout.txt"
    out.parent.mkdir()
    out.write_text(f"ok\nleak: {PRIV9}\n{json.dumps(ESCAPEE)}\n{SHORT}\n", encoding="utf-8")
    assert sl.main(["--output", str(out)]) == 5
    cap = capsys.readouterr()
    rows = [line.split("\t") for line in cap.out.splitlines()]
    assert {(r[0], r[1], r[2], r[3]) for r in rows} == {
        (f"{out}:2", _ids(PRIV9), "whole", "dev-private"),
        (f"{out}:3", _ids(ESCAPEE), "whole", "dev-private"),
        (f"{out}:4", _ids(SHORT), "whole", "dev-private"),
    }
    _assert_no_fixture_tokens(cap.out + cap.err)


# --- source refusals ----------------------------------------------------------
def test_public_needles_source_refused(sets, tmp_path, capsys):
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")
    assert sl.main(["--output", str(out), "--needles", str(sets["public"])]) == 2
    _assert_no_fixture_tokens("".join(capsys.readouterr()))


def test_sealed_needles_source_refused(sets, tmp_path, _private_root_in_tmp, capsys):
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")
    marked = _write_jsonl(tmp_path / "elsewhere" / "s.jsonl", [{"question": SHORT, "sealed": True}])
    assert sl.main(["--output", str(out), "--needles", str(marked)]) == 4
    under = _write_jsonl(_private_root_in_tmp / "sealed" / "s.jsonl", [{"question": SHORT}])
    assert sl.main(["--output", str(out), "--needles", str(under)]) == 4
    _assert_no_fixture_tokens("".join(capsys.readouterr()))


def test_sealed_run_input_refused(tmp_path, _private_root_in_tmp):
    q = _write_jsonl(_private_root_in_tmp / "runs" / "run-s" / "q.jsonl", [{"question": SHORT, "sealed": True}])
    _inputs(_private_root_in_tmp, "run-s", [{"path": str(q), "sha256": eval_sets.sha256_file(q), "kind": "questions"}])
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.scan_output([], run_id="run-s")
    assert exc.value.code == 4


def test_unregistered_private_needles_accepted(tmp_path, eval_registry):
    extra = _write_jsonl(tmp_path / "elsewhere" / "extra.jsonl", [{"question": SHORT}])
    out = tmp_path / "o.txt"
    out.write_text(SHORT, encoding="utf-8")
    hits = sl.scan_output([out], needles=[extra])
    assert [(h.needle, h.source) for h in hits] == [(_ids(SHORT), str(extra))]


def test_unparseable_needle_source_refused_without_content(tmp_path, capsys):
    bad = tmp_path / "elsewhere" / "bad.jsonl"
    bad.parent.mkdir()
    bad.write_text(json.dumps({"question": SHORT}) + "\n{oops " + SHORT + "\n", encoding="utf-8")
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")
    assert sl.main(["--output", str(out), "--needles", str(bad)]) == 2
    err = capsys.readouterr().err
    assert f"{bad}:2" in err
    _assert_no_fixture_tokens(err)


def _inputs(root: Path, run_id: str, items) -> Path:
    path = root / "runs" / run_id / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "inputs": items}), encoding="utf-8")
    return path


def test_output_run_inputs_are_needle_sources(tmp_path, _private_root_in_tmp):
    q = _write_jsonl(_private_root_in_tmp / "runs" / "run-a" / "q.jsonl", [{"question": SHORT}])
    _inputs(_private_root_in_tmp, "run-a", [{"path": str(q), "sha256": eval_sets.sha256_file(q), "kind": "questions"}])
    out = tmp_path / "o.txt"
    out.write_text(f"x {SHORT} y", encoding="utf-8")
    assert sl.scan_output([out]) == []  # not registered: no needles without --run
    assert [h.needle for h in sl.scan_output([out], run_id="run-a")] == [_ids(SHORT)]
    q.write_text(json.dumps({"question": PRIV9}) + "\n", encoding="utf-8")  # drifted since the run
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.scan_output([out], run_id="run-a")
    assert exc.value.code == 2


# --- merge gate ---------------------------------------------------------------
GIT_CFG = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_CFG, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, sets):
    """A tmp git repo at tmp_path (the private root's repo base), base commit tagged."""
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".gitignore").write_text("eval/private/\n_registry/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("synthetic repo\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore", "README.md", "public/golden.jsonl")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "branch", "base")
    return tmp_path


def _gh_fake(body: str, comments, review_comments, title: str = "t"):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps({"title": title, "body": body, "comments": [{"body": c} for c in comments], "reviews": []})
        assert args[0] == "api" and args[-1].endswith("/comments")
        half = len(review_comments) // 2
        return json.dumps([{"body": c} for c in review_comments[:half]]) + "\n" + json.dumps(
            [{"body": c} for c in review_comments[half:]]
        )

    run.calls = calls
    return run


def test_merge_gate_finds_needles_everywhere(repo):
    (repo / "leak.txt").write_text(f"draft\n{PRIV9}\n", encoding="utf-8")
    git(repo, "add", "leak.txt")
    git(repo, "commit", "-q", "-m", "add notes")
    added = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "leak.txt")
    git(repo, "commit", "-q", "-m", "remove notes")
    git(repo, "commit", "-q", "--allow-empty", "-m", f"subject\n\nwhy: {SHORT}")
    msg_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "v0.0.1", "-m", f"release {PARAPHRASE}")
    tag_obj = git(repo, "rev-parse", "v0.0.1")
    gh = _gh_fake(json.dumps(ESCAPEE), ["clean comment", f"see {PRIV9}"], ["fine", "also fine"])
    hits = sl.merge_gate("base", repo=repo, pr=12, gh_runner=gh)
    got = {(h.target, h.needle, h.kind) for h in hits}
    assert got == {
        (f"leak.txt@{added[:12]}:2", _ids(PRIV9), "whole"),
        (f"commit {msg_commit[:12]}:3", _ids(SHORT), "whole"),
        (f"tag {tag_obj[:12]}:1", _ids(PARAPHRASE), "whole"),
        ("PR#12 body:1", _ids(ESCAPEE), "whole"),
        ("PR#12 comment[1]:1", _ids(PRIV9), "whole"),
    }
    assert [c[:2] for c in gh.calls] == [["pr", "view"], ["api", "--paginate"]]


def test_merge_gate_clean_and_pr_skipped_note(repo):
    (repo / "notes.md").write_text(f"public: {PUB_EXACT}\n{PUB_W1}\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "docs")
    notes = []
    assert sl.merge_gate("base", repo=repo, notes=notes) == []
    assert any("PR items skipped" in n for n in notes)


def test_merge_gate_cli_with_fake_gh_executable(repo, tmp_path, capsys):
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    payload = json.dumps({"body": "ok", "comments": [], "reviews": [{"body": SHORT}]})
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({payload!r} if sys.argv[1:3] == ['pr', 'view'] else '[]')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    code = sl.main(["--merge-gate", "--base", "base", "--repo", str(repo), "--pr", "3", "--gh", str(fake)])
    cap = capsys.readouterr()
    assert code == 5
    assert cap.out.splitlines() == [f"PR#3 review[0]:1\t{_ids(SHORT)}\twhole\tdev-private"]
    _assert_no_fixture_tokens(cap.out + cap.err)


def test_merge_gate_gh_failure_is_exit2(repo, capsys):
    def broken(args):
        raise sl.ScanRefusal(2, "gh failed; PR items could not be read")

    with pytest.raises(sl.ScanRefusal) as exc:
        sl.merge_gate("base", repo=repo, pr=1, gh_runner=broken)
    assert exc.value.code == 2


def test_precheck_refuses_unregistered_private_file(repo, _private_root_in_tmp, capsys):
    stray = _write_jsonl(_private_root_in_tmp / "scratch.jsonl", [{"question": SHORT}])
    assert sl.main(["--merge-gate", "--base", "base", "--repo", str(repo)]) == 7
    cap = capsys.readouterr()
    assert str(stray) in cap.err and cap.out == ""
    _assert_no_fixture_tokens(cap.out + cap.err)


def test_precheck_refuses_questions_input_not_a_source(repo, _private_root_in_tmp, capsys):
    q = _write_jsonl(_private_root_in_tmp / "runs" / "run-q" / "q.jsonl", [{"question": SHORT}])
    _inputs(_private_root_in_tmp, "run-q", [{"path": str(q), "sha256": eval_sets.sha256_file(q), "kind": "questions"}])
    assert sl.main(["--merge-gate", "--base", "base", "--repo", str(repo)]) == 7
    cap = capsys.readouterr()
    assert str(q) in cap.err and cap.out == ""
    _assert_no_fixture_tokens(cap.out + cap.err)
    # given as a source, the same run passes
    assert sl.main(["--merge-gate", "--base", "base", "--repo", str(repo), "--needles", str(q)]) == 0


def _legacy_and_artifact(repo: Path, root: Path, sets, *, artifact_doc=None):
    cache = repo / "eval" / "w_cache.json"
    cache.write_text(json.dumps({"entries": {"k": {"rewrites": ["zz"]}}}), encoding="utf-8")
    (repo / "eval" / "legacy_public.json").write_text(
        json.dumps({"version": 1, "entries": [{"path": "eval/w_cache.json", "sha256": eval_sets.sha256_file(cache)}]}),
        encoding="utf-8",
    )
    priv_sha = eval_sets.sha256_file(sets["private"])
    art = root / "artifacts" / "expansion.json"
    art.parent.mkdir(parents=True)
    doc = artifact_doc or {
        "inputs": [{"path": "eval/private/sets/dev.jsonl", "sha256": priv_sha}],
        "entries": [{"id": "r-5", "question_sha256": "a" * 64, "rewrites": ["alpha beta"]}],
    }
    art.write_text(json.dumps(doc), encoding="utf-8")
    _inputs(root, "run-ok", [
        {"path": "eval/w_cache.json", "sha256": eval_sets.sha256_file(cache), "kind": "derived", "sources": []},
        {"path": "eval/private/artifacts/expansion.json", "sha256": eval_sets.sha256_file(art), "kind": "derived",
         "sources": [{"path": "eval/private/sets/dev.jsonl", "sha256": priv_sha}]},
    ])
    return art


def test_precheck_passes_legacy_cache_and_sha_keyed_artifact(repo, _private_root_in_tmp, sets):
    _legacy_and_artifact(repo, _private_root_in_tmp, sets)
    assert sl.precheck(sl.build_needles(sl.collect_sources()).source_shas) == []
    assert sl.merge_gate("base", repo=repo) == []


def test_precheck_refuses_artifact_with_question_text(repo, _private_root_in_tmp, sets):
    doc = {"entries": [{"id": "r-5", "question_sha256": "a" * 64, "question": SHORT}]}
    _legacy_and_artifact(repo, _private_root_in_tmp, sets, artifact_doc=doc)
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.merge_gate("base", repo=repo)
    assert exc.value.code == 7
    assert any("expansion.json" in p for p in exc.value.paths)


def test_precheck_refuses_artifact_from_unregistered_source(repo, _private_root_in_tmp, sets):
    doc = {"inputs": [{"path": "eval/private/x.jsonl", "sha256": "b" * 64}],
           "entries": [{"id": "r-5", "question_sha256": "a" * 64}]}
    _legacy_and_artifact(repo, _private_root_in_tmp, sets, artifact_doc=doc)
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.merge_gate("base", repo=repo)
    assert exc.value.code == 7 and any(p.endswith("eval/private/x.jsonl") for p in exc.value.paths)


def test_cli_usage_errors():
    assert sl.main(["--merge-gate"]) == 2
    assert sl.main([]) == 2
    assert sl.main(["--output", "x", "--base", "main"]) == 2


# --- review fixes ---------------------------------------------------------------
def test_jsonl_with_unicode_line_separators_inside_string_is_read(tmp_path):
    src = tmp_path / "elsewhere" / "sep.jsonl"
    src.parent.mkdir()
    rows = [{"question": "alpha beta\x0b\x0c\x1c\x85 gamma"}, {"question": SHORT}]
    src.write_text("".join(json.dumps(r, ensure_ascii=False) + "\r\n" for r in rows), encoding="utf-8")
    got = list(sl._iter_questions(src))
    assert len(got) == 2 and got[1] == SHORT


@pytest.mark.parametrize("zw", ["​", "‍", "﻿", "­"])
def test_format_characters_do_not_split_tokens(zw):
    assert sl.normalise_tokens(f"ef{zw}fect") == ["effect"]
    assert sl.normalise_tokens(SHORT.replace("dossivel", f"dos{zw}sivel")) == sl.normalise_tokens(SHORT)


def test_merge_gate_scans_pr_title_and_ref_names(repo):
    branch = "feat-" + "-".join(sl.normalise_tokens(SHORT))
    git(repo, "branch", branch)
    git(repo, "tag", "tag-" + "-".join(sl.normalise_tokens(PRIV9)))
    gh = _gh_fake("ok", [], [], title=f"fix {SHORT}")
    hits = sl.merge_gate("base", repo=repo, pr=4, gh_runner=gh)
    targets = {h.target for h in hits}
    assert "PR#4 title:1" in targets
    assert any(t.startswith("ref ") for t in targets)
    assert not any(branch in t for t in targets)


def test_precheck_accepts_registered_public_question_inputs(tmp_path, monkeypatch, _private_root_in_tmp):
    """Gate round 4: a private run over PUBLIC sets (w_sweep's default) passes the merge-gate precheck."""
    import json as _json

    import src.eval_sets as eval_sets
    from scripts import scan_leaks

    pub = tmp_path / "pub.jsonl"
    pub.write_text(_json.dumps({"question": "synthetic public widget question", "type": "direct",
                                "expected_sections": ["1.1"]}) + "\n")
    sha = eval_sets.sha256_file(pub)
    entry = eval_sets.SetEntry(name="pubset", path=str(pub), privacy="public", role="fixture",
                               status="active", sha256=sha)
    monkeypatch.setattr(eval_sets, "load_registry", lambda: [entry])
    run = _private_root_in_tmp / "runs" / "r-pub-1"
    run.mkdir(parents=True)
    (run / "inputs.json").write_text(_json.dumps({"version": 1, "inputs": [
        {"path": str(pub), "sha256": sha, "kind": "questions"}]}))
    assert scan_leaks.precheck(set()) == []
    # an unregistered (private) questions input is still refused
    (run / "inputs.json").write_text(_json.dumps({"version": 1, "inputs": [
        {"path": str(pub), "sha256": "0" * 64, "kind": "questions"}]}))
    assert scan_leaks.precheck(set()) != []
