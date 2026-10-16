"""16A-2b b1a closure round r4, the launcher part: a data dispatch runs under the prereg's own branch (D4), only after the
prereg passes ``prereg-check`` in-process (D5), refuses another prereg with the reason ``prereg_amended`` (D6) and is
recorded under a prereg or not at all (D7).

Synthetic repositories only (``tests/test_p16w_sp_launch.py``'s fixtures), which also replace ``check_prereg_valid`` with
a no-op: the real one is captured here at import and put back by the tests that test it.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src.sealed_ledger import ProtocolError, SealedLedger
from tests.test_p16w_sp_launch import env, git, repos  # noqa: F401  (fixtures)

REAL_CHECK_PREREG_VALID = sp.check_prereg_valid  # captured before the env fixture replaces it


def ledger_of(env):
    private = sp.eval_privacy.private_root()
    return SealedLedger(private / "sealed" / "ledger", mirror=private.parent / "sealed_ledger.jsonl")


def dispatches(env):
    return ledger_of(env).records("dispatch")


def commit_and_push(repos, text, *, branch="main"):
    """A prereg committed in the work clone and pushed to ``origin/<branch>`` only."""
    sha = repos.commit_prereg(repos.work, text)
    git(repos.work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    return sha


# --- D4: the prereg's own branch -----------------------------------------------------------------------------------------------
def test_the_ancestry_check_uses_the_preregs_own_branch(env, repos):
    env.make_claude(ret={"count": 1})
    own = '{"branch": "feature", "note": "a"}\n'
    sha = commit_and_push(repos, own, branch="feature")  # on origin/feature only: main does not carry it
    outcome = env.launch("data", prereg=sha, branch="feature", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK and len(dispatches(env)) == 1
    assert sp.prereg_branch(repos.work / "eval" / "prereg_16a2.json") == "feature"


def test_a_branch_that_differs_from_the_preregs_own_is_refused_before_anything_runs(env, repos):
    env.make_claude(ret={"count": 1})
    sha = commit_and_push(repos, '{"branch": "feature", "note": "a"}\n', branch="feature")
    git(repos.work, "push", "--quiet", "origin", "HEAD:refs/heads/main")  # main carries it too: the CLI branch would pass
    for cli_branch in ("main", "other", "feature2"):
        outcome = env.launch("data", prereg=sha, branch=cli_branch, repo=repos.work, enums=["count"])
        assert outcome.exit_status == sp.EXIT_REFUSED and outcome.message == "sp_launch: prereg_branch", cli_branch
    assert env.records() == [] and dispatches(env) == [] and env.staging_dirs() == []


def test_an_unreadable_prereg_falls_back_to_the_cli_branch_and_is_then_invalid(env, repos, monkeypatch):
    env.make_claude(ret={"count": 1})
    sha = commit_and_push(repos, "this is not json\n")
    assert sp.prereg_branch(repos.work / "eval" / "prereg_16a2.json") is None
    monkeypatch.setattr(sp, "check_prereg_valid", REAL_CHECK_PREREG_VALID)
    outcome = env.launch("data", prereg=sha, branch="main", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.message == "sp_launch: prereg_invalid"
    assert dispatches(env) == [] and env.records() == []


# --- D5: the prereg must pass prereg-check, in-process, before any dispatch record -------------------------------------------
def test_an_invalid_prereg_is_refused_before_any_dispatch_record_and_registers_nothing(env, repos, monkeypatch):
    env.make_claude(ret={"count": 1})
    sha = commit_and_push(repos, '{"branch": "main", "note": "synthetic"}\n')
    monkeypatch.setattr(sp, "check_prereg_valid", REAL_CHECK_PREREG_VALID)

    real_run = subprocess.run
    calls = []

    def watching_run(cmd, *a, **k):  # git is allowed (the ancestry check ran first); a validation subprocess is not
        calls.append(cmd[0])
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(sp.subprocess, "run", watching_run)
    outcome = env.launch("data", prereg=sha, branch="main", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.message == "sp_launch: prereg_invalid"
    assert set(calls) == {"git"}  # only git ran; no `sealed_tool prereg-check` subprocess
    assert dispatches(env) == [] and ledger_of(env).epoch_prereg_sha256() is None  # nothing was registered
    assert env.records() == [] and env.staging_dirs() == []  # the SP never started


def test_check_prereg_valid_is_the_prereg_check_function_called_in_process(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: seen.append((doc, kw)) or [])
    path = tmp_path / "prereg.json"
    path.write_text('{"branch": "main"}', encoding="utf-8")
    REAL_CHECK_PREREG_VALID(path)  # no error: nothing raised
    assert seen == [({"branch": "main"}, {})]  # the full check (recompute on): what `prereg-check` runs
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: [("quota", "mismatch")])
    with pytest.raises(sp.LaunchError) as info:
        REAL_CHECK_PREREG_VALID(path)
    assert (info.value.status, info.value.reason) == (sp.EXIT_REFUSED, "prereg_invalid")
    for text in ("not json", ""):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(sp.LaunchError):
            REAL_CHECK_PREREG_VALID(path)
    with pytest.raises(sp.LaunchError):
        REAL_CHECK_PREREG_VALID(tmp_path / "missing.json")


def test_the_check_leaves_no_api_key_behind_in_the_launcher_process(tmp_path, monkeypatch):
    """Importing ``src.judge`` runs ``load_dotenv()``; the launcher found no key and must still have none after the check."""
    path = tmp_path / "prereg.json"
    path.write_text('{"branch": "main"}', encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: os.environ.update(ANTHROPIC_API_KEY="from-dotenv") or [])
    REAL_CHECK_PREREG_VALID(path)
    assert "ANTHROPIC_API_KEY" not in os.environ
    # ... also when the check refuses
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: os.environ.update(ANTHROPIC_API_KEY="k") or [("x", "mismatch")])
    with pytest.raises(sp.LaunchError):
        REAL_CHECK_PREREG_VALID(path)
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_valid_prereg_is_dispatched_and_registered(env, repos, monkeypatch):
    env.make_claude(ret={"count": 1})
    sha = commit_and_push(repos, '{"branch": "main", "note": "a"}\n')
    monkeypatch.setattr(sp, "check_prereg_valid", REAL_CHECK_PREREG_VALID)
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: [])  # (the real check's cost is tested in test_p16a2b_b1a.py)
    outcome = env.launch("data", prereg=sha, branch="main", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK and len(dispatches(env)) == 1
    assert ledger_of(env).epoch_prereg_sha256() is not None


# --- D6: another prereg is `prereg_amended` ----------------------------------------------------------------------------------
def test_a_dispatch_under_another_prereg_than_the_epochs_is_prereg_amended(env, repos):
    env.make_claude(ret={"count": 1})
    first = commit_and_push(repos, '{"branch": "main", "note": "a"}\n')
    assert env.launch("data", prereg=first, branch="main", repo=repos.work, enums=["count"]).exit_status == sp.EXIT_OK
    second = commit_and_push(repos, '{"branch": "main", "note": "b"}\n')
    outcome = env.launch("data", prereg=second, branch="main", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.message == "sp_launch: prereg_amended"
    assert len(dispatches(env)) == 1 and len(env.records()) == 1


# --- D7: a data dispatch is recorded under a prereg ---------------------------------------------------------------------------
def test_the_launcher_refuses_a_data_dispatch_record_without_a_prereg(env):
    led = ledger_of(env)
    with pytest.raises(sp.LaunchError) as info:
        sp._record_dispatch(led, "0a1b2c3d", "data", ["claude"], None)
    assert (info.value.status, info.value.reason) == (sp.EXIT_REFUSED, "prereg_invalid")
    assert dispatches(env) == []
    sp._record_dispatch(led, "0a1b2c3d", "data", ["claude"], "a" * 64)
    assert [r["prereg_sha256"] for r in dispatches(env)] == ["a" * 64]


def test_the_ledger_requires_the_prereg_for_a_data_dispatch_and_only_for_it(env):
    led = ledger_of(env)
    with pytest.raises(ProtocolError):
        led.record_dispatch(dispatch_id="0a1b2c3d", dispatch_kind="data", argv_hash="d" * 64)
    assert dispatches(env) == []
    led.record_dispatch(dispatch_id="0a1b2c3d", dispatch_kind="probe", argv_hash="d" * 64)  # a probe carries none
    assert len(dispatches(env)) == 1 and led.epoch_prereg_sha256() is None
    assert json.loads((sp.eval_privacy.private_root().parent / "sealed_ledger.jsonl").read_text().splitlines()[0])["kind"] == "dispatch"
