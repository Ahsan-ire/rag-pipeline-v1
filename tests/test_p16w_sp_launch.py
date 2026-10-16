"""Phase 16A-2a item a7, acceptance (i): scripts/sp_launch.py with a fake ``claude``.

The real ``claude`` is never run. A fake executable on a temporary PATH records
what it was given (argument vector, stdin, environment, working directory) and
behaves as the test scripts it (exit code, canary on stdout/stderr, files under
``out/``, a ``return.json``). Every text below is invented.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sp_launch as sp

ROOT = Path(__file__).resolve().parent.parent
CANARY = "ZQXV-canary-4417-pelvanth"
CANARY2 = "KRDL-canary-9902-ostrimane"
FIVE = ("HOME", "PATH", "USER", "LANG", "TMPDIR")
# Added to a Python child by the interpreter / macOS, never by the launcher.
INTERPRETER_ENV = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
EXPECTED_ARGV = [
    "-p", "--restricted", "--tools", "Read,Write,Edit,Glob,Grep", "--permission-mode", "acceptEdits",
    "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--no-session-persistence", "--model", "opus", "--append-system-prompt",
]

FAKE_CLAUDE = r'''#!__PY__
import json, os, sys
CFG = json.load(open("__CFG__"))
stdin = sys.stdin.read()
tree = []
for dp, dn, fn in os.walk("."):
    for n in dn + fn:
        tree.append(os.path.relpath(os.path.join(dp, n), "."))
with open(CFG["record"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "env": dict(os.environ),
                         "cwd": os.getcwd(), "tree": sorted(tree)}) + "\n")
if CFG.get("canary"):
    print(CFG["canary"])
    print(CFG["canary"], file=sys.stderr)
for rel, text in CFG.get("files", {}).items():
    path = os.path.join("out", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
for rel, target in CFG.get("symlinks", {}).items():
    os.makedirs("out", exist_ok=True)
    os.symlink(target, os.path.join("out", rel))
if CFG.get("ret") is not None:
    with open(os.path.join("out", "return.json"), "w") as fh:
        fh.write(CFG["ret"] if isinstance(CFG["ret"], str) else json.dumps(CFG["ret"]))
if CFG.get("grandchild_pid_file"):  # a helper process in the same group, to prove a timeout kills the group
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(CFG["grandchild_pid_file"], "w") as fh:
        fh.write(str(child.pid))
if CFG.get("sleep"):
    import time
    time.sleep(CFG["sleep"])
sys.exit(CFG.get("exit", 0))
'''


def twin_line(a: str, b: str) -> str:
    """One valid ``twins.jsonl`` line (an allowlisted ``out/`` file that carries recognisable text)."""
    return json.dumps({"a": a, "b": b, "verdict": "twin"}) + "\n"


class SimulatedCrash(BaseException):
    """Stands in for a process dying: not an Exception, so no handler can swallow it."""


class Env:
    """The sandbox a launcher test runs in: staging root, destination, fake claude, task and brief."""

    def __init__(self, tmp_path: Path, private: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "sealed_sp"
        self.private = private
        self.work = private / "sealed" / "work"  # the two logs land here
        self.dest = self.work / "sp_out"  # a released out/ lands in sp_out/<dispatch id>/
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.record = tmp_path / "record.jsonl"
        self.task = tmp_path / "task.txt"
        self.brief = tmp_path / "brief.md"
        self.task.write_text("TASK: do the invented thing\nsecond line\n", encoding="utf-8")
        self.brief.write_text('BRIEF "quoted" \'single\' $HOME \u2014 \u00fcn\u00efcode\nline 2\n', encoding="utf-8")
        self.make_claude()

    def make_claude(self, **cfg) -> None:
        cfg["record"] = str(self.record)
        cfg_path = self.tmp / "fake_claude.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        script = self.bin / "claude"
        script.write_text(FAKE_CLAUDE.replace("__PY__", sys.executable).replace("__CFG__", str(cfg_path)))
        script.chmod(0o755)

    def records(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def log(self, name: str) -> list:
        path = self.private / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def launch(self, kind: str = "probe", **kw):
        return sp.launch(kind=kind, task=self.task, brief=self.brief, **kw)

    def main(self, *extra: str, kind: str = "probe") -> int:
        return sp.main(["--kind", kind, "--task", str(self.task), "--brief", str(self.brief), *extra])

    def staging_dirs(self) -> list:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.name not in sp.RESERVED_ROOT_NAMES)


@pytest.fixture
def env(tmp_path, monkeypatch, _private_root_in_tmp):
    """A sandbox with a polluted launcher environment, a fake claude first on PATH, no real claude reachable."""
    home = tmp_path / "home"
    tmpdir = tmp_path / "tmpdir"
    home.mkdir()
    tmpdir.mkdir()
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not os.path.exists(os.path.join(d, "claude"))]
    sandbox = Env(tmp_path, _private_root_in_tmp)
    monkeypatch.setenv("PATH", os.pathsep.join([str(sandbox.bin), *keep]))
    monkeypatch.setenv(sp.ROOT_ENV, str(sandbox.root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "spuser")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    for name, value in {
        "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "9", "CLAUDE_CONFIG_DIR": "/x",
        "ANTHROPIC_BASE_URL": "https://example.invalid", "ANTHROPIC_AUTH_TOKEN": "tok", "OPENAI_API_KEY": "k",
        "AWS_SECRET_ACCESS_KEY": "s", "GITHUB_TOKEN": "g", "LANGSMITH_API_KEY": "l", "SP_TEST_LEAK": "leak",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return sandbox


# --- git fixture: a work clone with a bare "origin" -------------------------------
def git(cwd, *args) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


class GitRepos:
    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "--bare", str(self.origin))
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.work = self.clone(tmp_path / "work")
        self.first = self.commit(self.work, "a.txt")
        git(self.work, "push", "--quiet", "origin", "main")
        self.other = self.clone(tmp_path / "other")  # cloned after the first push: shares its history

    def clone(self, path: Path) -> Path:
        git(path.parent, "clone", "--quiet", str(self.origin), str(path))
        git(path, "symbolic-ref", "HEAD", "refs/heads/main")
        return path

    @staticmethod
    def commit(repo: Path, name: str) -> str:
        (repo / name).write_text(name, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "--quiet", "-m", f"add {name}")
        return git(repo, "rev-parse", "HEAD")

    @staticmethod
    def commit_prereg(repo: Path, text: str = '{"branch": "main"}\n') -> str:
        """Commit ``eval/prereg_16a2.json`` (the file a data dispatch is bound to) with ``text``; returns the commit."""
        path = repo / "eval" / "prereg_16a2.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(repo, "add", "eval/prereg_16a2.json")
        git(repo, "commit", "--quiet", "-m", "prereg")
        return git(repo, "rev-parse", "HEAD")

    def pushed_prereg(self, text: str = '{"branch": "main"}\n') -> str:
        """A prereg committed in the work clone and pushed to origin/main: what a data dispatch is launched under."""
        sha = self.commit_prereg(self.work, text)
        git(self.work, "push", "--quiet", "origin", "main")
        return sha


@pytest.fixture
def repos(tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (("NAME", "t"), ("EMAIL", "t@example.invalid")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    return GitRepos(tmp_path)


# --- argument vector, stdin, environment -----------------------------------------------
def test_exact_argv_stdin_and_scrubbed_env(env, tmp_path, monkeypatch):
    note = tmp_path / "notes.txt"
    note.write_text("an invented input", encoding="utf-8")
    tree = tmp_path / "bundle"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("not an input", encoding="utf-8")

    real_popen = subprocess.Popen
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sp.subprocess, "Popen", spy)
    outcome = env.launch(inputs=[note, tree])
    assert outcome.exit_status == sp.EXIT_FAILED  # the default fake writes no return.json
    assert outcome.line == sp.INVALID

    [rec] = env.records()
    brief = env.brief.read_text(encoding="utf-8")
    assert rec["argv"] == [*EXPECTED_ARGV, brief]
    assert rec["stdin"] == env.task.read_text(encoding="utf-8")
    assert env.task.read_text(encoding="utf-8") not in rec["argv"]

    seen = {k: v for k, v in rec["env"].items() if k not in INTERPRETER_ENV}
    assert seen == {
        "HOME": os.environ["HOME"], "PATH": os.environ["PATH"], "USER": "spuser",
        "LANG": "en_US.UTF-8", "TMPDIR": os.environ["TMPDIR"],
    }
    assert not any(k.startswith(("CLAUDE", "ANTHROPIC", "OPENAI", "AWS", "GITHUB", "LANGSMITH")) for k in rec["env"])
    assert "SP_TEST_LEAK" not in rec["env"]

    # cwd is the staging directory, holding exactly the inputs plus out/ and the two logs.
    assert Path(rec["cwd"]).parent == env.root.resolve()
    assert rec["tree"] == sorted(
        ["notes.txt", "bundle", "bundle/sub", "bundle/sub/deep.txt", "out", "stdout.log", "stderr.log"]
    )

    # The launcher itself passed exactly the five variables, the task on a stdin pipe (its text is in rec["stdin"]
    # above), no shell, and its own session so a timeout can kill the whole group.
    [(args, kwargs)] = [c for c in calls if "-p" in c[0][0]]
    assert kwargs["env"] == {k: os.environ[k] for k in FIVE}
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["start_new_session"] is True
    assert not kwargs.get("shell")
    assert Path(args[0][0]).name == "claude" and Path(args[0][0]).parent == env.bin


def test_build_argv_and_scrubbed_env_functions(env):
    assert sp.build_argv("B") == ["claude", *EXPECTED_ARGV, "B"]
    assert list(sp.scrubbed_env()) == list(FIVE)
    assert sp.argv_hash(["/some/abs/claude", "-p"]) == sp.argv_hash(["claude", "-p"])


# --- refusals -------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["sk-ant-fake-value", ""])
def test_refuses_with_api_key_set(env, monkeypatch, capfd, value):
    monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    assert env.main() == sp.EXIT_REFUSED
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "ANTHROPIC_API_KEY" in captured.err and "sk-ant-fake-value" not in captured.err
    assert env.records() == []  # claude never started
    assert env.staging_dirs() == []
    assert env.log("sp_probe.jsonl") == []


def test_prereg_refused_when_not_reachable_from_origin(env, repos):
    unpushed = repos.commit_prereg(repos.work)
    outcome = env.launch("data", prereg=unpushed, branch="main", repo=repos.work)
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.line is None
    assert "not reachable" in outcome.message
    assert env.records() == [] and env.staging_dirs() == []
    assert env.log("sp_dispatch.jsonl") == []


def test_prereg_accepted_with_later_commits_on_top_after_fetch(env, repos):
    prereg = repos.pushed_prereg()  # committed in the work clone and pushed
    git(repos.other, "pull", "--quiet", "origin", "main")
    tip = repos.commit(repos.other, "c.txt")  # a later commit on top, pushed from elsewhere
    git(repos.other, "push", "origin", "main")
    assert git(repos.work, "rev-parse", "origin/main") == prereg  # not fetched yet
    env.make_claude(ret={"status": "ok"})
    outcome = env.launch("data", prereg=prereg, branch="main", repo=repos.work, enums=["status", "ok"])
    assert outcome.exit_status == sp.EXIT_OK, outcome.message
    assert git(repos.work, "rev-parse", "origin/main") == tip  # the launcher ran git fetch
    assert len(env.records()) == 1
    assert [r["kind"] for r in env.log("sp_dispatch.jsonl")] == ["data"]


def test_prereg_equal_to_branch_tip_is_accepted(env, repos):
    outcome = env.launch("data", prereg=repos.pushed_prereg(), branch="main", repo=repos.work)
    assert outcome.message is None and len(env.records()) == 1


def test_prereg_on_another_branch_is_refused(env, repos):
    git(repos.other, "checkout", "--quiet", "-b", "feature")
    elsewhere = repos.commit(repos.other, "f.txt")
    git(repos.other, "push", "--quiet", "origin", "feature")
    outcome = env.launch("data", prereg=elsewhere, branch="main", repo=repos.work)
    assert outcome.exit_status == sp.EXIT_REFUSED
    assert env.records() == []


def test_prereg_unknown_to_origin_is_refused(env, repos):
    outcome = env.launch("data", prereg="0123456789abcdef0123456789abcdef01234567", branch="main", repo=repos.work)
    assert outcome.exit_status == sp.EXIT_REFUSED and env.records() == []


def test_fetch_failure_refuses(env, repos, tmp_path):
    git(repos.work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    outcome = env.launch("data", prereg=repos.first, branch="main", repo=repos.work)
    assert outcome.exit_status == sp.EXIT_REFUSED and "fetch failed" in outcome.message
    assert env.records() == []


@pytest.mark.parametrize("sha,branch", [
    ("abc1234", "main"), ("--upload-pack=x", "main"), ("0" * 40, "-main"), ("0" * 40, "a..b"), ("0" * 40, "ma in"),
])
def test_prereg_arguments_must_be_plain(env, repos, sha, branch):
    outcome = env.launch("data", prereg=sha, branch=branch, repo=repos.work)
    assert outcome.exit_status == sp.EXIT_USAGE and env.records() == []


def test_data_needs_prereg_and_probe_ignores_it(env, tmp_path):
    assert env.launch("data").exit_status == sp.EXIT_USAGE
    assert env.launch("data", prereg="0" * 40).exit_status == sp.EXIT_USAGE
    outcome = env.launch("probe", prereg="0" * 40, branch="main", repo=tmp_path / "no-such-repo")
    assert outcome.message is None and len(env.records()) == 1


def test_staging_root_override_only_under_pytest(env, monkeypatch):
    assert sp.resolve_staging_root() == env.root
    monkeypatch.setattr(sp, "_under_pytest", lambda: False)
    with pytest.raises(sp.RootRefused):
        sp.resolve_staging_root()
    assert env.launch().exit_status == sp.EXIT_REFUSED
    monkeypatch.delenv(sp.ROOT_ENV)
    assert sp.resolve_staging_root() == sp.default_staging_root()
    monkeypatch.setattr(sp, "_under_pytest", lambda: True)
    with pytest.raises(sp.RootRefused):
        sp.resolve_staging_root()  # variable unset
    monkeypatch.setenv(sp.ROOT_ENV, str(sp.default_staging_root()))
    with pytest.raises(sp.RootRefused):
        sp.resolve_staging_root()  # the real root, under pytest
    assert env.records() == []


def test_bad_inputs_are_usage_errors_before_anything_starts(env, tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(ok)
    clash = tmp_path / "sub"
    clash.mkdir()
    (clash / "ok.txt").write_text("y", encoding="utf-8")
    out_named = tmp_path / "other" / "out"
    out_named.parent.mkdir()
    out_named.write_text("z", encoding="utf-8")
    for bad in ([tmp_path / "missing.txt"], [link], [ok, clash / "ok.txt"], [out_named]):
        assert env.launch(inputs=bad).exit_status == sp.EXIT_USAGE
    env.task.write_text("   \n", encoding="utf-8")
    assert env.launch().exit_status == sp.EXIT_USAGE
    assert env.launch(enums=["bad word"]).exit_status == sp.EXIT_USAGE
    assert env.records() == [] and env.staging_dirs() == []


def test_a_held_lock_refuses_dispatch_and_sweep(env):
    env.root.mkdir()
    fd = os.open(env.root / sp.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env.launch().exit_status == sp.EXIT_REFUSED
        assert sp.main(["--sweep"]) == sp.EXIT_REFUSED
    finally:
        os.close(fd)
    assert env.records() == []
    assert sp.main(["--sweep"]) == sp.EXIT_OK


# --- the SP's words never reach the launcher's output ---------------------------------------
def test_canary_on_sp_stdout_and_stderr_is_absent_from_launcher_output(env, capfd):
    env.make_claude(canary=CANARY, files={"twins.jsonl": twin_line(CANARY2, "f0a1b2c3d")}, ret={"status": "ok", "count": 3})
    rc = env.main("--enum", "status", "--enum", "ok", "--enum", "count")
    captured = capfd.readouterr()
    assert rc == sp.EXIT_OK
    assert captured.out == '{"count":3,"status":"ok"}\n'  # the one validated line
    assert captured.err == ""
    assert CANARY not in captured.out + captured.err and CANARY2 not in captured.out + captured.err
    # The success path ran: the fake did print, and it landed in the work logs, not on the terminal.
    [line] = env.log("sp_probe.jsonl")
    work = env.work
    assert CANARY in (work / f"{line['dispatch_id']}.stdout.log").read_text()
    assert CANARY in (work / f"{line['dispatch_id']}.stderr.log").read_text()
    assert (env.dest / line["dispatch_id"] / "twins.jsonl").read_text() == twin_line(CANARY2, "f0a1b2c3d")
    assert env.staging_dirs() == []


def test_refusal_message_is_fixed_text(env, monkeypatch, capfd):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env.main()
    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == "sp_launch: ANTHROPIC_API_KEY is set; the SP must use the subscription login\n"


# --- return validation ------------------------------------------------------------------------
ID = "0a1b2c3d"
SHA = "f" * 64


@pytest.mark.parametrize("data", [
    {},
    {ID: ID},
    {SHA: 7, "status": "ok"},
    {"count": 0, "ids": [ID, SHA], "nested": {ID: {"n": 12}}},
])
def test_validate_return_accepts_ids_hashes_ints_and_enums(data):
    assert sp.validate_return(data, enums=["status", "ok", "count", "ids", "nested", "n"])


@pytest.mark.parametrize("data", [
    {"status": "the client buys a house in Lusk"},  # free-text value
    {"a free text key": "ok"},                       # free-text key
    {"status": "OK"},                                # enum is case-sensitive
    {"status": "ok\n"},                              # trailing newline must not slip past a regex
    {"status": ID.upper()},                          # ids are lowercase hex
    {"status": "0a1b2c3"},                           # 7 hex
    {"status": "0a1b2c3d4"},                         # 9 hex
    {"status": "f" * 63}, {"status": "f" * 65},
    {"count": -1}, {"count": 1.5}, {"count": True}, {"count": None}, {"count": "5"}, {"count": 10**9 + 1},
    {"status": ["ok", "free text"]}, {"status": {"ok": "free text"}},
    {"status": {"a": {"b": {"c": {"d": {"e": 1}}}}}},  # deeper than MAX_DEPTH
    ["ok"], "ok", 3, None,
])
def test_validate_return_rejects_everything_else(data):
    assert not sp.validate_return(data, enums=["status", "ok", "count", "a", "b", "c", "d", "e"])


FAMILY = "f0a1b2c3d"
ROW = f"{FAMILY}-1"


@pytest.mark.parametrize("data", [
    {"families": [FAMILY, "f00000000"], "status": "ok"},
    {"rows": [ROW, f"{FAMILY}-0", f"{FAMILY}-2"], "status": "ok"},
    {FAMILY: 3, ROW: 1, ID: 2, SHA: 4},  # every kind of id is also a legal key
])
def test_validate_return_accepts_private_family_and_row_ids_beside_dispatch_ids(data):
    """The SP brief names family ids (``f`` + 8 hex) and row ids (``f...-n``) in its return file."""
    assert sp.validate_return(data, enums=["status", "ok", "families", "rows"])
    assert sp.ID_RE.fullmatch(ID) and sp.FAMILY_ID_RE.fullmatch(FAMILY) and sp.ROW_ID_RE.fullmatch(ROW)


@pytest.mark.parametrize("bad", [
    "F0a1b2c3d", "f0a1b2c", "f0a1b2c3d4", "g0a1b2c3d",   # case, length, not hex
    f"{FAMILY}-", f"{FAMILY}-x", f"{FAMILY}--1", f"{FAMILY}-1-2", f"{FAMILY}-12345",  # a row id is the family id, "-" and 1-4 digits
    f"{FAMILY}\n", f"{ROW}\n", f" {ROW}", f"{ROW} the client", "f0a1b2c3d-1 ",
    f"{ID}-1",                                            # an 8-hex dispatch id has no row suffix
])
def test_validate_return_still_rejects_near_miss_ids(bad):
    assert not sp.validate_return({"status": bad}, enums=["status"])
    assert not sp.validate_return({bad: 1}, enums=["status"])
    assert not sp.validate_return({"status": [bad]}, enums=["status"])


def test_unsafe_enum_words_are_refused():
    for bad in ("two words", "", "x" * 33, "line\nbreak", "9start", "emoji\u2603"):
        with pytest.raises(ValueError):
            sp.validate_return({}, enums=[bad])


def _write(path: Path, content) -> Path:
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def test_load_return_gives_canonical_line_or_none(tmp_path):
    f = _write(tmp_path / "r.json", '{ "status" : "ok",\n "count": 2 }')
    assert sp.load_return(f, ["status", "ok", "count"]) == '{"count":2,"status":"ok"}'
    assert sp.load_return(tmp_path / "missing.json") is None
    for content in (
        '{"count": 1, "count": 2}', "not json", '{"count": NaN}', '{"count": 1e3}', b"\xff\xfe", "[]",
        '{"status": "free text"}', "{" * 5000, '{"count": 1}' + " " * 70000,
    ):
        assert sp.load_return(_write(tmp_path / "bad.json", content), ["status", "count"]) is None, content[:20]
    link = tmp_path / "link.json"
    link.symlink_to(f)
    assert sp.load_return(link, ["status", "ok", "count"]) is None
    assert sp.load_return(tmp_path, ["status"]) is None  # a directory


def test_free_text_return_prints_invalid_and_releases_nothing(env, capfd):
    leak = "Mrs Murphy buys 5 Elm Road"
    env.make_claude(files={"pool/draft.txt": "draft"}, canary=CANARY, ret={"summary": leak})
    rc = env.main("--enum", "summary")
    captured = capfd.readouterr()
    assert rc == sp.EXIT_FAILED
    assert captured.out == "invalid\n" and captured.err == ""
    assert leak not in captured.out + captured.err
    [line] = env.log("sp_probe.jsonl")
    assert not (env.dest / line["dispatch_id"]).exists()  # out/ is not released
    assert (env.work / f"{line['dispatch_id']}.stdout.log").exists()  # the logs are kept
    assert env.staging_dirs() == []


def test_nonzero_exit_with_valid_return_releases_nothing(env, capfd):
    env.make_claude(exit=3, files={"pool/draft.txt": "d"}, ret={"count": 1})
    rc = env.main("--enum", "count")
    assert rc == sp.EXIT_FAILED
    [line] = env.log("sp_probe.jsonl")
    assert line["exit_status"] == 3
    assert not (env.dest / line["dispatch_id"]).exists()


# --- failure, sweep, journal ----------------------------------------------------------------------
def test_failed_run_still_deletes_its_staging_directory(env, capfd):
    env.make_claude(exit=7, canary=CANARY)
    rc = env.main()
    captured = capfd.readouterr()
    assert rc == sp.EXIT_FAILED and CANARY not in captured.out + captured.err
    assert len(env.records()) == 1  # it ran
    assert env.staging_dirs() == []
    [line] = env.log("sp_probe.jsonl")
    assert line["exit_status"] == 7


def test_exception_mid_dispatch_still_deletes_staging(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail " + CANARY)

    monkeypatch.setattr(sp, "_run_sp", boom)
    outcome = env.launch()
    assert outcome.exit_status == sp.EXIT_FAILED
    assert outcome.message == "sp_launch: failed (RuntimeError)" and CANARY not in outcome.message
    assert env.staging_dirs() == []


def test_next_launch_sweeps_a_stale_staging_directory(env):
    stale = env.root / "deadbeef"
    (stale / "out").mkdir(parents=True)
    (stale / "input.txt").write_text(CANARY, encoding="utf-8")
    (env.root / "stray-file").write_text(CANARY, encoding="utf-8")
    env.make_claude(ret={"count": 1})
    outcome = env.launch(enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK
    assert not stale.exists() and not (env.root / "stray-file").exists()
    assert env.staging_dirs() == []


def test_sweep_command_removes_stale_entries_and_reports_a_count(env, capfd):
    assert sp.main(["--sweep"]) == sp.EXIT_OK  # no root yet
    assert capfd.readouterr().out == "swept 0\n"
    (env.root / "aaaa1111").mkdir(parents=True)
    (env.root / "bbbb2222").mkdir()
    assert sp.main(["--sweep"]) == sp.EXIT_OK
    assert capfd.readouterr().out == "swept 2\n"
    assert env.staging_dirs() == []
    assert sp.main(["--sweep", "--kind", "probe"]) == sp.EXIT_USAGE


def make_tree(base: Path) -> Path:
    (base / "a" / "b").mkdir(parents=True)
    (base / "a" / "b" / "x.txt").write_text("x" * 50, encoding="utf-8")
    (base / "y.bin").write_bytes(bytes(range(256)) * 8)
    (base / "empty").mkdir()
    return base


def snapshot(path: Path) -> dict:
    if path.is_file():
        return {".": path.read_bytes()}
    return {str(p.relative_to(path)): (None if p.is_dir() else p.read_bytes()) for p in sorted(path.rglob("*"))}


def crash_at(monkeypatch, point) -> None:
    def hook(name: str) -> None:
        if name == point:
            raise SimulatedCrash(name)

    monkeypatch.setattr(sp, "_crash_point", hook)


def test_journaled_move_happy_path(tmp_path):
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    journal = tmp_path / "j" / "moves.journal"
    sp.journaled_move([(src, tmp_path / "dst" / "deep" / "tree")], journal)
    assert not src.exists() and snapshot(tmp_path / "dst" / "deep" / "tree") == want
    assert sp.pending_moves(journal) == [] and sp.recover_moves(journal) == 0
    assert [r["t"] for r in sp._read_journal(journal)] == ["begin", "done"]
    assert not list((tmp_path / "dst" / "deep").glob("*.partial"))


def test_journaled_move_of_a_single_file(tmp_path):
    src = tmp_path / "one.log"
    src.write_text("log text", encoding="utf-8")
    sp.journaled_move([(src, tmp_path / "work" / "one.log")], tmp_path / "moves.journal")
    assert not src.exists() and (tmp_path / "work" / "one.log").read_text() == "log text"


@pytest.mark.parametrize("point", ["after_begin", "after_copy", "after_rename", "after_delete"])
def test_move_journal_recovers_a_crash_at_every_step(tmp_path, monkeypatch, point):
    src = make_tree(tmp_path / "src")
    other = tmp_path / "other.log"
    other.write_text("second item", encoding="utf-8")
    want, want_other = snapshot(src), other.read_bytes()
    dst, dst_other = tmp_path / "dst" / "tree", tmp_path / "dst" / "other.log"
    journal = tmp_path / "moves.journal"
    crash_at(monkeypatch, point)
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, dst), (other, dst_other)], journal)
    assert len(sp.pending_moves(journal)) == 1  # the transaction never closed
    if point == "after_copy":
        assert not dst.exists() and list(dst.parent.glob("*.partial"))  # unverified copy, source intact
    if point in ("after_begin", "after_copy", "after_rename"):
        assert src.exists()  # nothing is deleted before the copy is verified and in place

    crash_at(monkeypatch, None)  # the "restart": no crash hook any more
    assert sp.recover_moves(journal) == 1
    assert not src.exists() and not other.exists()
    assert snapshot(dst) == want and dst_other.read_bytes() == want_other
    assert not list(dst.parent.glob("*.partial"))
    assert sp.pending_moves(journal) == [] and sp.recover_moves(journal) == 0


def test_copy_that_does_not_verify_keeps_the_source(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    want = snapshot(src)
    dst, journal = tmp_path / "dst", tmp_path / "moves.journal"
    real_copy = sp._copy_durable
    top = {"next": True}

    def corrupting_copy(a, b):
        is_top, top["next"] = top["next"], False  # the copy recurses through this name; corrupt once
        real_copy(a, b)
        if is_top:
            (b / "y.bin").write_bytes(b"corrupt")

    monkeypatch.setattr(sp, "_copy_durable", corrupting_copy)
    with pytest.raises(sp.MoveError):
        sp.journaled_move([(src, dst)], journal)
    assert snapshot(src) == want and not dst.exists() and not list(tmp_path.glob("*.partial"))
    assert len(sp.pending_moves(journal)) == 1
    monkeypatch.setattr(sp, "_copy_durable", real_copy)
    assert sp.recover_moves(journal) == 1 and snapshot(dst) == want and not src.exists()


def test_recovery_stops_when_source_and_destination_are_both_gone(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    journal = tmp_path / "moves.journal"
    crash_at(monkeypatch, "after_begin")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, tmp_path / "dst")], journal)
    crash_at(monkeypatch, None)
    shutil.rmtree(src)
    with pytest.raises(sp.MoveError):
        sp.recover_moves(journal)
    assert len(sp.pending_moves(journal)) == 1  # still pending, not silently closed


def test_move_refuses_an_existing_destination_and_symlinks(tmp_path):
    src = make_tree(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    journal = tmp_path / "moves.journal"
    with pytest.raises(sp.MoveError):
        sp.journaled_move([(src, dst)], journal)
    (src / "link").symlink_to(src / "y.bin")
    with pytest.raises(sp.MoveError):
        sp.journaled_move([(src, tmp_path / "dst2")], journal)
    with pytest.raises(sp.MoveError):
        sp.journaled_move([(tmp_path / "missing", tmp_path / "dst3")], journal)
    assert not journal.exists() and src.exists()  # refused before anything was journaled or copied


def test_destination_differing_from_manifest_is_not_overwritten(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    dst, journal = tmp_path / "dst", tmp_path / "moves.journal"
    crash_at(monkeypatch, "after_begin")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, dst)], journal)
    crash_at(monkeypatch, None)
    dst.mkdir()
    (dst / "someone-elses.txt").write_text("x", encoding="utf-8")
    with pytest.raises(sp.MoveError):
        sp.recover_moves(journal)
    assert src.exists()


def test_torn_journal_tail_is_ignored_but_a_corrupt_line_is_not(tmp_path, monkeypatch):
    src = make_tree(tmp_path / "src")
    journal = tmp_path / "moves.journal"
    crash_at(monkeypatch, "after_begin")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(src, tmp_path / "dst")], journal)
    crash_at(monkeypatch, None)
    with open(journal, "ab") as fh:
        fh.write(b'{"v":1,"t":"do')  # a crash mid-append
    assert len(sp.pending_moves(journal)) == 1
    assert sp.recover_moves(journal) == 1
    with open(journal, "ab") as fh:
        fh.write(b"this is not json\n")
    with pytest.raises(sp.MoveError):
        sp.pending_moves(journal)


def test_sweep_replays_a_pending_move_before_deleting_its_source(env, monkeypatch):
    staging = env.root / "cafe0123"
    make_tree(staging / "out")
    want = snapshot(staging / "out")
    journal = env.root / sp.JOURNAL_NAME
    crash_at(monkeypatch, "after_copy")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(staging / "out", env.dest / "cafe0123")], journal)
    crash_at(monkeypatch, None)
    assert sp.main(["--sweep"]) == sp.EXIT_OK
    assert snapshot(env.dest / "cafe0123") == want  # nothing lost to the sweep
    assert env.staging_dirs() == []


def test_sweep_deletes_nothing_when_recovery_fails(env, monkeypatch):
    staging = env.root / "cafe0123"
    make_tree(staging / "out")
    journal = env.root / sp.JOURNAL_NAME
    crash_at(monkeypatch, "after_begin")
    with pytest.raises(SimulatedCrash):
        sp.journaled_move([(staging / "out", env.dest / "cafe0123")], journal)
    crash_at(monkeypatch, None)
    # Both the source and the destination vanish: recovery cannot finish.
    shutil.rmtree(staging / "out")
    (env.root / "innocent-bystander").mkdir()
    assert sp.main(["--sweep"]) == sp.EXIT_FAILED
    assert (staging).exists() and (env.root / "innocent-bystander").exists()  # fail closed
    assert env.launch().exit_status == sp.EXIT_FAILED and env.records() == []


def test_crash_after_the_copy_leaves_staging_for_recovery(env, monkeypatch, capfd):
    env.make_claude(canary=CANARY, files={"twins.jsonl": twin_line(CANARY2, "f0a1b2c3d")}, ret={"count": 1})
    crash_at(monkeypatch, "after_rename")
    with pytest.raises(SimulatedCrash):
        env.launch(enums=["count"])
    crash_at(monkeypatch, None)
    [staging] = env.staging_dirs()  # kept: a move is still pending, so deleting it could lose data
    assert (env.dest / staging / "twins.jsonl").exists()
    assert sp.main(["--sweep"]) == sp.EXIT_OK
    assert capfd.readouterr().out == "swept 1\n"
    assert env.staging_dirs() == []
    assert (env.dest / staging / "twins.jsonl").read_text() == twin_line(CANARY2, "f0a1b2c3d")
    assert CANARY in (env.work / f"{staging}.stdout.log").read_text()
    assert sp.pending_moves(env.root / sp.JOURNAL_NAME) == []


# --- dispatch logs ------------------------------------------------------------------------------------
def test_the_two_kinds_log_to_different_files_and_never_log_text(env, repos):
    env.make_claude(canary=CANARY, ret={"count": 1})
    probe = env.launch("probe", enums=["count"])
    data = env.launch("data", prereg=repos.pushed_prereg(), branch="main", repo=repos.work, enums=["count"])
    assert probe.exit_status == data.exit_status == sp.EXIT_OK
    [p] = env.log("sp_probe.jsonl")
    [d] = env.log("sp_dispatch.jsonl")
    assert (p["kind"], d["kind"]) == ("probe", "data")
    assert p["dispatch_id"] != d["dispatch_id"]
    expected_hash = sp.argv_hash(sp.build_argv(env.brief.read_text(encoding="utf-8")))
    for row in (p, d):
        assert set(row) == {"argv_hash", "dispatch_id", "exit_status", "kind", "ts"}
        assert re.fullmatch(r"[0-9a-f]{8}", row["dispatch_id"])
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["ts"])
        assert row["exit_status"] == 0 and row["argv_hash"] == expected_hash
    for name in ("sp_probe.jsonl", "sp_dispatch.jsonl"):
        text = (env.private / name).read_text(encoding="utf-8")
        assert CANARY not in text and "TASK" not in text and "BRIEF" not in text


def _ledger_records(kind=None):
    """The epoch's ledger as the launcher builds it (not via ``sealed_run``: importing that loads ``.env``)."""
    from src.sealed_ledger import SealedLedger

    private = sp.eval_privacy.private_root()
    return SealedLedger(private / "sealed" / "ledger", mirror=private.parent / "sealed_ledger.jsonl").records(kind)


def test_a_data_dispatch_is_also_recorded_in_the_out_of_tree_ledger_and_a_probe_is_not(env, repos):
    """a7: a data dispatch logs to sp_dispatch.jsonl AND the sealed ledger; the mirror gets the kind and a digest."""
    env.make_claude(canary=CANARY, ret={"count": 1})
    assert env.launch("probe", enums=["count"]).exit_status == sp.EXIT_OK
    assert _ledger_records() == []  # a probe leaves the epoch's ledger alone
    assert env.launch("data", prereg=repos.pushed_prereg(), branch="main", repo=repos.work, enums=["count"]).exit_status == sp.EXIT_OK
    [d] = env.log("sp_dispatch.jsonl")
    [rec] = _ledger_records("dispatch")
    assert set(rec) == {"v", "seq", "kind", "ts", "dispatch_id", "dispatch_kind", "argv_hash", "prereg_sha256"}
    assert rec["prereg_sha256"] == hashlib.sha256((repos.work / "eval" / "prereg_16a2.json").read_bytes()).hexdigest()
    assert rec["dispatch_id"] == d["dispatch_id"] and rec["dispatch_kind"] == "data" and rec["argv_hash"] == d["argv_hash"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["ts"])
    mirror = [json.loads(line) for line in (env.private.parent / "sealed_ledger.jsonl").read_text().splitlines()]
    assert [set(m) for m in mirror] == [{"seq", "kind", "digest", "digests"}]  # the kind and digests (line, prereg), nothing else
    assert mirror[0]["digests"] == {"prereg_sha256": rec["prereg_sha256"]}
    assert mirror[0]["kind"] == "dispatch" and d["dispatch_id"] not in json.dumps(mirror)
    for path in (Path(os.environ["CC_SEALED_LEDGER"]), env.private / "sealed" / "ledger" / "sealed_ledger.jsonl"):
        assert CANARY not in path.read_text() and "TASK" not in path.read_text()


def test_a_dispatch_the_ledger_cannot_record_is_refused_and_the_sp_never_runs(env, repos, capfd):
    from src.sealed_ledger import LedgerCorrupt

    class Broken:
        def record_dispatch(self, **kw):
            raise LedgerCorrupt("the ledger is damaged: secret text that must not be printed")

    outcome = env.launch("data", prereg=repos.pushed_prereg(), branch="main", repo=repos.work, ledger=Broken())
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.line is None
    assert outcome.message == "sp_launch: the sealed ledger could not record the dispatch"
    assert env.records() == [] and env.staging_dirs() == []  # claude never started, staging was removed
    assert env.log("sp_dispatch.jsonl") == []


def test_a_released_out_directory_lands_in_sp_out_and_the_logs_in_the_sealed_work_directory(env, repos):
    env.make_claude(files={"twins.jsonl": twin_line("f0a1b2c3d", "f1a1b2c3d")}, ret={"count": 1})
    assert env.launch("probe", enums=["count"]).exit_status == sp.EXIT_OK
    [p] = env.log("sp_probe.jsonl")
    sealed_work = env.private / "sealed" / "work"
    assert (sealed_work / "sp_out" / p["dispatch_id"] / "twins.jsonl").read_text() == twin_line("f0a1b2c3d", "f1a1b2c3d")
    assert (sealed_work / f"{p['dispatch_id']}.stdout.log").is_file()
    assert not (env.private / "sealed" / p["dispatch_id"]).exists()  # the old landing place is gone


def test_script_runs_standalone():
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sp_launch.py"), "--help"],
                          capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0 and "--kind" in done.stdout and "--sweep" in done.stdout
