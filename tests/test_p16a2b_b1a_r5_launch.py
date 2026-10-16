"""16A-2b b1a closure round r5, the launcher part (R7): a data dispatch runs the whole of ``prereg-check`` (the structural
validation AND the pinned quota numbers and minimum shares), un-mocked, so a prereg that ``prereg-check`` refuses is never
registered as the epoch's.

The full ``validate_prereg`` recomputes the power tables (about 20 s), so the two tests that must see the real, complete
check run it once each; the dispatch tests keep the real ``pinned_errors`` and the structural ``validate_prereg`` (without
the recomputation) so that they stay quick. Synthetic repositories (``tests/test_p16w_sp_launch.py``'s fixtures); the real
``eval/prereg_16a2.json`` is read, never written.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import sealed_tool as st
from scripts import sp_launch as sp
from tests.test_p16a2b_b1a_r4_launch import REAL_CHECK_PREREG_VALID, commit_and_push, dispatches, ledger_of
from tests.test_p16w_sp_launch import env, git, repos  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
REAL_VALIDATE = st.validate_prereg  # captured before any test replaces it


def real_prereg() -> dict:
    return json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))


def write(tmp_path, doc, name="prereg.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def zero_share() -> dict:
    """The real prereg with the realistic minimum share set to zero: structurally valid, not the registered number."""
    doc = real_prereg()
    assert doc["min_share"]["realistic"]["tutorial_or_colleague"] != 0
    doc["min_share"]["realistic"]["tutorial_or_colleague"] = 0
    return doc


@pytest.fixture
def structural_validation(monkeypatch):
    """``validate_prereg`` without its (slow) recomputation of the quota examples and power tables; ``pinned_errors`` stays real."""
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: REAL_VALIDATE(doc, **{**kw, "recompute": False}))


def test_the_real_prereg_passes_the_launchers_check_unmocked_and_it_runs_both_functions_of_prereg_check(tmp_path, monkeypatch):
    calls = []
    real_pinned = st.pinned_errors
    monkeypatch.setattr(st, "validate_prereg", lambda doc, **kw: calls.append("validate") or REAL_VALIDATE(doc, **kw))
    monkeypatch.setattr(st, "pinned_errors", lambda doc: calls.append("pinned") or real_pinned(doc))
    REAL_CHECK_PREREG_VALID(write(tmp_path, real_prereg()))  # raises LaunchError if prereg-check would refuse it
    assert calls == ["validate", "pinned"]  # one verdict: the two functions the command runs


def test_a_pinned_min_share_that_prereg_check_refuses_is_refused_by_the_launcher_unmocked(tmp_path):
    doc = zero_share()
    # the structure is valid (it is all the launcher used to check) ...
    assert REAL_VALIDATE(doc, recompute=False) == []
    # ... but it is not the pre-registration: the pin refuses it, and so does the launcher's real check
    assert ("min_share.realistic.tutorial_or_colleague", "mismatch") in st.pinned_errors(doc)
    with pytest.raises(sp.LaunchError) as info:
        REAL_CHECK_PREREG_VALID(write(tmp_path, doc))
    assert (info.value.status, info.value.reason) == (sp.EXIT_REFUSED, "prereg_invalid")


@pytest.mark.parametrize("path,value", [
    (("min_share", "realistic", "tutorial_or_colleague"), 0.2),
    (("quota", "tuning_min"), 59),
    (("quota", "floor_sealed"), 49),
])
def test_every_pinned_number_is_refused_by_the_launcher(tmp_path, structural_validation, path, value):
    doc = real_prereg()
    node = doc
    for key in path[:-1]:
        node = node[key]
    assert node[path[-1]] != value
    node[path[-1]] = copy.deepcopy(value)
    assert st.pinned_errors(doc)
    with pytest.raises(sp.LaunchError) as info:
        REAL_CHECK_PREREG_VALID(write(tmp_path, doc))
    assert info.value.reason == "prereg_invalid"


def test_a_data_dispatch_under_a_pinned_change_registers_nothing(env, repos, monkeypatch, structural_validation):
    env.make_claude(ret={"count": 1})
    doc = zero_share()
    sha = commit_and_push(repos, json.dumps(doc, indent=2) + "\n", branch=doc["branch"])
    monkeypatch.setattr(sp, "check_prereg_valid", REAL_CHECK_PREREG_VALID)
    outcome = env.launch("data", prereg=sha, branch=doc["branch"], repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_REFUSED and outcome.message == "sp_launch: prereg_invalid"
    assert dispatches(env) == [] and ledger_of(env).epoch_prereg_sha256() is None
    assert env.records() == [] and env.staging_dirs() == []  # the SP never started


def test_a_data_dispatch_under_the_unchanged_prereg_is_registered(env, repos, monkeypatch, structural_validation):
    env.make_claude(ret={"count": 1})
    doc = real_prereg()
    sha = commit_and_push(repos, json.dumps(doc, indent=2) + "\n", branch=doc["branch"])
    monkeypatch.setattr(sp, "check_prereg_valid", REAL_CHECK_PREREG_VALID)
    outcome = env.launch("data", prereg=sha, branch=doc["branch"], repo=repos.work, enums=["count"])
    assert outcome.exit_status == sp.EXIT_OK and len(dispatches(env)) == 1
    assert ledger_of(env).epoch_prereg_sha256() is not None
