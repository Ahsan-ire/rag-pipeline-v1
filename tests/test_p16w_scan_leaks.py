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
    # a valid single-schema private set an artifact can be keyed from (the
    # mixed `dev` fixture is a scanner fixture, not a loadable eval set)
    priv_v1 = _write_jsonl(_private_root_in_tmp / "sets" / "dev_v1.jsonl",
                           [{"question": q, "type": "direct", "expected_sections": ["1.1"]} for q in (PRIV9, SHORT)])
    # and a valid public one (a public-built artifact, round 5 PT2)
    pub_v1 = _write_jsonl(tmp_path / "public" / "golden_v1.jsonl",
                          [{"question": PUB_EXACT, "type": "direct", "expected_sections": ["1.1"]}])
    eval_registry.add(priv, privacy="private", name="dev-private")
    eval_registry.add(priv_v1, privacy="private", name="dev-private-v1")
    eval_registry.add(pub, privacy="public", name="golden-public")
    eval_registry.add(pub_v1, privacy="public", name="golden-public-v1")
    return {"private": priv, "public": pub, "private_v1": priv_v1, "public_v1": pub_v1}


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


def test_merge_gate_reads_each_message_whole_record_separators_included(repo):
    """16A-1 merge gate, Codex #3: a commit or tag message holding the old
    record separator (\\x1e, whitespace to the normaliser) between a needle's
    tokens is still one message, so the needle is caught."""
    rs_joined = "\x1e".join(PRIV9.split())
    git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-m", f"subject\n\nwhy: {rs_joined}\n")
    msg_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "--cleanup=verbatim", "v0.0.2", "-m", f"release {rs_joined}\n")
    tag_obj = git(repo, "rev-parse", "v0.0.2")
    assert "\x1e" in git(repo, "cat-file", "-p", msg_commit)  # the separator really is in the message
    assert len(sl.scan_text(f"why: {rs_joined}", "probe", sl.build_needles(sl.collect_sources(())))) == 1
    got = {(h.target.split(":")[0], h.needle) for h in sl.merge_gate("base", repo=repo)}
    assert (f"commit {msg_commit[:12]}", _ids(PRIV9)) in got
    assert (f"tag {tag_obj[:12]}", _ids(PRIV9)) in got


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


def _real_artifact(path: Path, inputs) -> Path:
    """A schema-valid expansion artifact (synthetic rewrites) built from ``inputs``."""
    from src.expansion_artifact import build_artifact, save_artifact
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    from src.evaluator import _artifact_keys

    # Rows keyed exactly as a real build keys them (the precheck binds every
    # entry to a real row of its header sets, gate round 7); a header set that
    # is not on disk gets an invented row.
    rows = []
    for p, _h in inputs:
        rows += [(key, q) for q, key in _artifact_keys(p)] if Path(p).is_file() else [("r-5", "synthetic widget")]
    art = build_artifact(rows, lambda q: Expansion(q, ("alpha beta",), REWRITE_MODEL, STATUS_LIVE),
                         [{"path": p, "sha256": h, "kind": "questions"} for p, h in inputs])
    path.parent.mkdir(parents=True, exist_ok=True)
    save_artifact(path, art)
    return path


def _legacy_and_artifact(repo: Path, root: Path, sets, *, artifact_doc=None, artifact_inputs=None):
    cache = repo / "eval" / "w_cache.json"
    cache.write_text(json.dumps({"entries": {"k": {"rewrites": ["zz"]}}}), encoding="utf-8")
    (repo / "eval" / "legacy_public.json").write_text(
        json.dumps({"version": 1, "entries": [{"path": "eval/w_cache.json", "sha256": eval_sets.sha256_file(cache)}]}),
        encoding="utf-8",
    )
    priv_sha = eval_sets.sha256_file(sets["private"])
    art = root / "artifacts" / "expansion.json"
    if artifact_doc is None:
        v1 = sets["private_v1"]
        _real_artifact(art, artifact_inputs or [(str(v1), eval_sets.sha256_file(v1))])
    else:
        art.parent.mkdir(parents=True)
        art.write_text(json.dumps(artifact_doc), encoding="utf-8")
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
    _legacy_and_artifact(repo, _private_root_in_tmp, sets, artifact_inputs=[("eval/private/x.jsonl", "b" * 64)])
    with pytest.raises(sl.ScanRefusal) as exc:
        sl.merge_gate("base", repo=repo)
    # the artifact itself is refused (its header names no approved, findable set)
    assert exc.value.code == 7 and any(p.endswith("expansion.json") for p in exc.value.paths)


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


# --- gate round 5 -----------------------------------------------------------------
def test_precheck_passes_derived_inputs_traced_to_approved_sources(repo, _private_root_in_tmp, sets):
    """PT1: a bakeoff-shaped private run (arm reports, sidecars, w_sweep dumps
    whose recorded sources are registered public or needle sets) passes."""
    pub_sha = eval_sets.sha256_file(sets["public"])
    priv_sha = eval_sets.sha256_file(sets["private"])
    rdir = _private_root_in_tmp / "runs" / "run-bake"
    rdir.mkdir(parents=True)
    report = rdir / "A.md"
    report.write_text("# arm A\n", encoding="utf-8")
    dump = rdir / "dA.json"
    dump.write_text(json.dumps({"cohorts": [], "rows": []}), encoding="utf-8")
    _inputs(_private_root_in_tmp, "run-bake", [
        {"path": str(report), "sha256": eval_sets.sha256_file(report), "kind": "derived",
         "sources": [{"path": str(sets["public"]), "sha256": pub_sha}]},
        {"path": str(dump), "sha256": eval_sets.sha256_file(dump), "kind": "derived",
         "sources": [{"path": str(sets["private"]), "sha256": priv_sha}]},
    ])
    assert sl.precheck(sl.build_needles(sl.collect_sources()).source_shas) == []
    assert sl.merge_gate("base", repo=repo) == []
    # a derived input with no recorded source, or one unapproved source, is still refused
    _inputs(_private_root_in_tmp, "run-bake", [
        {"path": str(dump), "sha256": eval_sets.sha256_file(dump), "kind": "derived", "sources": []},
        {"path": str(report), "sha256": eval_sets.sha256_file(report), "kind": "derived",
         "sources": [{"path": str(sets["public"]), "sha256": pub_sha}, {"path": "x.jsonl", "sha256": "c" * 64}]},
    ])
    offenders = sl.precheck(sl.build_needles(sl.collect_sources()).source_shas)
    assert any(o.endswith("dA.json") for o in offenders)
    assert any(o.endswith("-> x.jsonl") for o in offenders)


def test_precheck_accepts_sha_keyed_artifact_outside_the_artifacts_dir(repo, tmp_path, _private_root_in_tmp, sets):
    """PT2: a public-built, content-free artifact replayed in a private run passes."""
    pub_sha = eval_sets.sha256_file(sets["public_v1"])
    art = _real_artifact(tmp_path / "probes" / "art_pub.json", [(str(sets["public_v1"]), pub_sha)])
    _inputs(_private_root_in_tmp, "run-art", [
        {"path": str(art), "sha256": eval_sets.sha256_file(art), "kind": "derived", "sources": []},
    ])
    assert sl.precheck(sl.build_needles(sl.collect_sources()).source_shas) == []


def test_precheck_refuses_question_bearing_artifact_even_when_traced(repo, _private_root_in_tmp, sets):
    """An artifact-shaped derived input is never accepted by source tracing alone."""
    priv_sha = eval_sets.sha256_file(sets["private"])
    art = _private_root_in_tmp / "runs" / "run-qa" / "exp.json"
    art.parent.mkdir(parents=True)
    art.write_text(json.dumps({"entries": [{"id": "r-5", "question_sha256": "a" * 64, "question": SHORT}]}),
                   encoding="utf-8")
    _inputs(_private_root_in_tmp, "run-qa", [
        {"path": str(art), "sha256": eval_sets.sha256_file(art), "kind": "derived",
         "sources": [{"path": str(sets["private"]), "sha256": priv_sha}]},
    ])
    assert any(o.endswith("exp.json") for o in sl.precheck(sl.build_needles(sl.collect_sources()).source_shas))


# --- gate round 6: tracing trusts no unverified metadata --------------------------
UNREG = "vorsk tallimer quentish obbleby carrowen fintle spadgett murrable"


def _offenders(sets):
    return sl.precheck(sl.build_needles(sl.collect_sources()).source_shas)


def _derived(root: Path, run: str, path: Path, sources, sha=None):
    _inputs(root, run, [{"path": str(path), "sha256": sha or eval_sets.sha256_file(path), "kind": "derived",
                         "sources": [{"path": p, "sha256": h} for p, h in sources]}])


@pytest.mark.parametrize("doc", [
    {"rows": [{"id": "x", "question": UNREG}]},          # probe A: question key, claims a public source
    [{"question": UNREG}],                                 # probe F: a JSON list
])
def test_traced_file_with_question_text_must_classify_public(repo, _private_root_in_tmp, sets, doc):
    path = _private_root_in_tmp / "runs" / "run-a" / "dump.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    _derived(_private_root_in_tmp, "run-a", path, [(str(sets["public"]), eval_sets.sha256_file(sets["public"]))])
    assert any(o.endswith("dump.json") for o in _offenders(sets))


def test_traced_source_path_must_hash_to_its_recorded_sha(repo, tmp_path, _private_root_in_tmp, sets):
    """Probe B: a source path naming another file, carrying the public sha."""
    other = tmp_path / "other.jsonl"
    other.write_text(json.dumps({"question": UNREG}) + "\n", encoding="utf-8")
    report = _private_root_in_tmp / "runs" / "run-b" / "r.md"
    report.parent.mkdir(parents=True)
    report.write_text("# arm\n", encoding="utf-8")
    _derived(_private_root_in_tmp, "run-b", report, [(str(other), eval_sets.sha256_file(sets["public"]))])
    assert any(o.endswith("r.md") for o in _offenders(sets))


def test_missing_or_drifted_derived_file_is_never_traced(repo, _private_root_in_tmp, sets):
    """Probes C and D: a missing file, and a question-bearing artifact whose recorded sha drifted."""
    pub = [(str(sets["public"]), eval_sets.sha256_file(sets["public"]))]
    _derived(_private_root_in_tmp, "run-c", Path("/nonexistent/exp.json"), pub, sha="d" * 64)
    assert any(o.endswith("exp.json") for o in _offenders(sets))
    art = _private_root_in_tmp / "runs" / "run-d" / "exp.json"
    art.parent.mkdir(parents=True)
    art.write_text(json.dumps({"entries": [{"question_sha256": "a" * 64, "question": UNREG}]}), encoding="utf-8")
    _inputs(_private_root_in_tmp, "run-c", [])
    _derived(_private_root_in_tmp, "run-d", art, pub, sha="e" * 64)
    assert any(o.endswith("exp.json") for o in _offenders(sets))


def test_artifact_exemption_needs_the_exact_schema(repo, tmp_path, _private_root_in_tmp, sets):
    """Probe E: text under keys other than question/questions is not content-free."""
    a = tmp_path / "anywhere" / "a.json"
    a.parent.mkdir()
    a.write_text(json.dumps({"entries": [{"question_sha256": "a" * 64, "q": UNREG, "prompt": UNREG}]}),
                 encoding="utf-8")
    _inputs(_private_root_in_tmp, "run-e", [{"path": str(a), "sha256": eval_sets.sha256_file(a),
                                              "kind": "derived", "sources": []}])
    assert any(o.endswith("a.json") for o in _offenders(sets))


def test_unreferenced_files_under_artifacts_are_checked(repo, _private_root_in_tmp, sets):
    """Probe G (artifacts/): an interrupted build's orphan must still be a valid
    artifact from approved sources; anything else there is refused."""
    v1 = sets["private_v1"]
    _real_artifact(_private_root_in_tmp / "artifacts" / "ok.json", [(str(v1), eval_sets.sha256_file(v1))])
    assert _offenders(sets) == []
    (_private_root_in_tmp / "artifacts" / "stray.jsonl").write_text(json.dumps({"question": UNREG}) + "\n")
    _real_artifact(_private_root_in_tmp / "artifacts" / "bad.json", [("x.jsonl", "b" * 64)])
    offenders = _offenders(sets)
    assert any(o.endswith("stray.jsonl") for o in offenders) and any(o.endswith("bad.json") for o in offenders)
    assert not any(o.endswith("ok.json") for o in offenders)


def test_artifact_entries_must_bind_to_real_rows(repo, _private_root_in_tmp, sets):
    """Round 7 (N6): a schema-valid artifact from an approved header whose entry
    keys or question hashes match no real row is refused."""
    from src.expansion_artifact import build_artifact, save_artifact
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    pub_sha = eval_sets.sha256_file(sets["public"])
    art = build_artifact([(f"{pub_sha[:16]}/{UNREG}", UNREG)],
                         lambda q: Expansion(q, (UNREG,), REWRITE_MODEL, STATUS_LIVE),
                         [{"path": "nonexistent/elsewhere.jsonl", "sha256": pub_sha, "kind": "questions"}])
    save_artifact(_private_root_in_tmp / "artifacts" / "x.json", art)
    assert any(o.endswith("x.json") for o in _offenders(sets))


@pytest.mark.parametrize("body", [
    "\n".join(json.dumps({"question": UNREG}) for _ in range(2)),           # N1 .jsonl-shaped
    "\ufeff" + json.dumps({"question": UNREG}),                              # N2 BOM
    json.dumps({"question": UNREG})[:-3],                                     # N2b truncated
    '{"cohorts": [], "rows": [], "scorer_version": "x", "question": "%s", "question": "y"}' % UNREG,  # N3
    "[" * 600 + json.dumps({"question": UNREG}) + "]" * 600,                 # N4 deep nesting
])
def test_unparseable_or_duplicate_keyed_traced_file_fails_closed(repo, _private_root_in_tmp, sets, body):
    """Round 7: a file the checker cannot parse cleanly is never traced, and
    nothing crashes the precheck (exit 7, not a traceback)."""
    path = _private_root_in_tmp / "runs" / "run-n" / "dump.json"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    _derived(_private_root_in_tmp, "run-n", path, [(str(sets["public"]), eval_sets.sha256_file(sets["public"]))])
    assert any(o.endswith("dump.json") for o in _offenders(sets))
    assert sl.main(["--merge-gate", "--base", "base", "--repo", str(repo)]) == 7


def test_non_list_sources_is_refused_not_a_crash(repo, _private_root_in_tmp, sets):
    """Round 7 (N5): ``"sources": 5`` is an offender, not a TypeError."""
    path = _private_root_in_tmp / "runs" / "run-s" / "r.md"
    path.parent.mkdir(parents=True)
    path.write_text("# r\n", encoding="utf-8")
    _inputs(_private_root_in_tmp, "run-s", [{"path": str(path), "sha256": eval_sets.sha256_file(path),
                                              "kind": "derived", "sources": 5}])
    assert sl.main(["--merge-gate", "--base", "base", "--repo", str(repo)]) == 7


def test_rule5_refuses_a_duplicate_question_key(tmp_path, sets):
    """Round 7 (N3): json.loads keeps the last duplicate key; rule 5 must not."""
    pub_sha = eval_sets.sha256_file(sets["public"])
    path = tmp_path / "outside" / "dump.json"
    path.parent.mkdir()
    pub_q = json.loads(Path(sets["public"]).read_text().splitlines()[0])["question"]
    path.write_text('{"cohorts": [], "rows": [], "scorer_version": "x", "inputs": [{"path": "p", "sha256": "%s"}],'
                    ' "question": "%s", "question": "%s"}' % (pub_sha, UNREG, pub_q), encoding="utf-8")
    assert eval_sets.classify(path) != "public"
