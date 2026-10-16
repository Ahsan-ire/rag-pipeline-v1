"""16A-2b b1a closure round r3 (F7), the launcher part: a data dispatch runs under the prereg that is in the tree.

``sp_launch.check_prereg(..., file=...)`` requires the commit to be the last one that touched the prereg file and to
equal the working file; ``launch`` passes the file's sha256 to the ``dispatch`` record, the first record of any kind that
carries one registers the epoch's prereg, and a dispatch under another prereg is refused with the SP never started.

Synthetic repositories only (``tests/test_p16w_sp_launch.py``'s fixtures).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts import sp_launch as sp
from tests.test_p16w_sp_launch import env, git, repos  # noqa: F401  (fixtures)

PREREG_A = '{"branch": "main", "note": "a"}\n'
PREREG_B = '{"branch": "main", "note": "b"}\n'


def sha_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dispatches(env):
    from src.sealed_ledger import SealedLedger

    private = sp.eval_privacy.private_root()
    return SealedLedger(private / "sealed" / "ledger", mirror=private.parent / "sealed_ledger.jsonl").records("dispatch")


def amend(repos, text=PREREG_B) -> str:
    """Another prereg, committed and pushed (the last commit touching the file)."""
    return repos.pushed_prereg(text)


# --- check_prereg(file=...) -------------------------------------------------------------------------------------------------
def test_the_commit_must_be_the_last_one_touching_the_prereg_file_and_equal_the_working_file(repos):
    work = repos.work
    file = work / "eval" / "prereg_16a2.json"
    first = repos.pushed_prereg(PREREG_A)
    sp.check_prereg(first, "main", repo=work, file=file)  # the last commit touching it, equal to the working file
    second = repos.pushed_prereg(PREREG_B)
    sp.check_prereg(second, "main", repo=work, file=file)
    with pytest.raises(sp.LaunchError, match="last commit"):  # the amended file is another commit: the old sha is stale
        sp.check_prereg(first, "main", repo=work, file=file)
    other = repos.commit(work, "unrelated.txt")  # later commits on top that do not touch the prereg are fine
    git(work, "push", "--quiet", "origin", "main")
    assert other != second
    sp.check_prereg(second, "main", repo=work, file=file)
    file.write_text(PREREG_A, encoding="utf-8")  # an uncommitted edit of the working file
    with pytest.raises(sp.LaunchError, match="differs"):
        sp.check_prereg(second, "main", repo=work, file=file)


def test_a_prereg_file_outside_the_repository_or_absent_is_refused_and_no_file_keeps_the_old_rule(repos, tmp_path):
    work = repos.work
    sha = repos.pushed_prereg(PREREG_A)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(PREREG_A, encoding="utf-8")
    for file in (elsewhere, work / "eval" / "missing.json"):
        with pytest.raises(sp.LaunchError):
            sp.check_prereg(sha, "main", repo=work, file=file)
    sp.check_prereg(repos.first, "main", repo=work)  # without `file` any pushed commit still passes (probe/tool callers)


# --- the dispatch record and the epoch's prereg ------------------------------------------------------------------------------
def test_a_data_dispatch_registers_the_epochs_prereg_and_a_dispatch_under_another_is_refused(env, repos):
    env.make_claude(ret={"count": 1})
    first = repos.pushed_prereg(PREREG_A)
    assert env.launch("data", prereg=first, branch="main", repo=repos.work, enums=["count"]).exit_status == sp.EXIT_OK
    [rec] = dispatches(env)
    assert rec["prereg_sha256"] == sha_of_text(PREREG_A)
    from src.sealed_ledger import SealedLedger

    private = sp.eval_privacy.private_root()
    ledger = SealedLedger(private / "sealed" / "ledger", mirror=private.parent / "sealed_ledger.jsonl")
    assert ledger.epoch_prereg_sha256() == sha_of_text(PREREG_A)  # the first dispatch registered it
    assert env.launch("data", prereg=first, branch="main", repo=repos.work, enums=["count"]).exit_status == sp.EXIT_OK  # again

    second = amend(repos)  # amended and pushed after a data dispatch
    stale = env.launch("data", prereg=first, branch="main", repo=repos.work, enums=["count"])
    assert stale.exit_status == sp.EXIT_REFUSED and "last commit" in stale.message  # the old commit is no longer the prereg
    runs = len(env.records())
    changed = env.launch("data", prereg=second, branch="main", repo=repos.work, enums=["count"])
    assert changed.exit_status == sp.EXIT_REFUSED and changed.message == "sp_launch: prereg_amended"  # r4 D6: the reason token
    assert len(env.records()) == runs and env.staging_dirs() == []  # the SP never ran, nothing is left in staging
    assert len(dispatches(env)) == 2  # no third dispatch record


def test_the_prereg_binding_precedes_the_ledger_and_a_probe_needs_neither(env, repos):
    env.make_claude(ret={"count": 1})
    assert env.launch("probe", enums=["count"]).exit_status == sp.EXIT_OK
    assert dispatches(env) == []
    # a data dispatch under a commit that is not the prereg is refused before any record is written
    sha = repos.pushed_prereg(PREREG_A)
    stray = repos.commit(repos.work, "stray.txt")
    git(repos.work, "push", "--quiet", "origin", "main")
    outcome = env.launch("data", prereg=stray, branch="main", repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_REFUSED and "last commit" in outcome.message
    assert dispatches(env) == [] and len(env.records()) == 1  # only the probe ran
    assert env.launch("data", prereg=sha, branch="main", repo=repos.work, enums=["count"]).exit_status == sp.EXIT_OK
    assert json.loads((sp.eval_privacy.private_root().parent / "sealed_ledger.jsonl").read_text().splitlines()[0])["digests"] == {
        "prereg_sha256": sha_of_text(PREREG_A),
    }
