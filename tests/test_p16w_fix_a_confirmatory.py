"""Phase 16A-2a gate fixes A3, A4 and A5: confirmatory slots, the baseline's intent, durable publication.

A3: a confirmatory slot (1-4) is rendered after the scan to ``sealed/runs/<id>/report.md`` and prints
an aggregates-only summary; it is not labelled "failed guards" and never writes ``results_v6.md``.
A4: a baseline must have a publication intent (the canonical one of slots 0 and 0b).
A5: the report's parent directory is fsynced after the rename and before "consumed"; a new run
directory's ancestry is fsynced before "slot_stored".
"""

import errno
import os
import stat

import pytest

from src import sealed_report, sealed_run
from src.sealed_ledger import RecoveryAction, SealedLedger, digest_run_dir
from tests.p16_canary import assert_no_leak
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    Crash,
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    world,
)
from tests.test_p16w_slot_run_protocol import candidate_prereg, crash_at, recover, run_id_of


# --- A3 -----------------------------------------------------------------------------------------------------------


def test_a_confirmatory_slot_is_rendered_to_its_run_directory_and_summarised_in_numbers(world, capsys):
    config_hash = candidate_prereg(world)
    world.run(0)
    canonical_before = sealed_run.results_v6_path().read_text()
    capsys.readouterr()
    outcome = world.run(1, expansion="live", rewrite_candidate=config_hash, baseline_run=run_id_of(world, 0))
    out = capsys.readouterr().out

    assert not outcome.canonical and not outcome.published and outcome.rendered
    assert outcome.failed_guards == ("slot_0_or_0b",)  # the data keeps the slot guard; nothing calls it a failure
    assert sealed_run.results_v6_path().read_text() == canonical_before  # only slots 0 and 0b write results_v6.md
    report_path = world.run_dir(outcome.run_id) / "report.md"
    report = report_path.read_text()
    assert report.splitlines()[0].endswith("(sealed slot 1; confirmatory)")
    assert "Canonical run" not in report and "Guards not met" not in report and "not canonical" not in report
    assert "**Primary test**: exact two-sided cluster sign-flip test" in report
    assert "Sensitivity (labelled, not the test): Durkalski" in report

    assert "failed guards" not in out and "not canonical" not in out and "canonical; published" not in out
    lines = [ln for ln in out.splitlines() if ln.startswith("[eval] slot 1 ")]
    assert lines[0].startswith("[eval] slot 1 primary (hybrid+rewrite, family strict hit@6): cluster sign-flip p = 1.0000")
    assert "0 of 4 groups discordant; net +0 families" in lines[0]
    assert any("families: candidate gains 0, loses 0 of 6" in ln for ln in lines)
    assert sum("sensitivity (labelled, not the test)" in ln for ln in lines) == 2
    assert "confirmatory; report rendered in run" in out and "(not published)" in out

    # no intent (only slots 0/0b publish), consumed once rendered, and nothing sensitive anywhere it was printed
    assert [r["slot"] for r in world.ledger().records("publication_intent")] == ["0"]
    assert world.ledger().recover(1).action is RecoveryAction.CONSUMED
    assert_no_leak(world.canaries.secrets(), report, out)
    assert not [i for i in world.ids() if i in report + out]
    assert not (world.run_dir(outcome.run_id) / "report.md.tmp").exists()


def test_the_run_directory_digest_ignores_the_rendered_report_and_nothing_else(tmp_path):
    from tests.test_p16w_sealed_ledger import make_run

    run = make_run(tmp_path, "r1")
    before = digest_run_dir(run)
    (run / "report.md").write_text("rendered later\n")
    (run / "report.md.tmp").write_text("a crashed write\n")
    assert digest_run_dir(run) == before
    (run / "nested" / "report.md").write_text("not the top-level report\n")
    assert digest_run_dir(run) != before  # only the top-level file is exempt
    (run / "nested" / "report.md").unlink()
    (run / "other.md").write_text("an extra file\n")
    assert digest_run_dir(run) != before


def test_a_confirmatory_slot_recovers_after_a_crash_following_its_report(world, capsys):
    config_hash = candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    with pytest.MonkeyPatch.context() as m:
        crash_at(m, SealedLedger, "record_consumed")
        with pytest.raises(Crash):
            world.run(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    run_dir = world.run_dir(run_id_of(world, 1))
    assert (run_dir / "report.md").is_file()  # written before the crash
    assert world.ledger().recover(1).action is RecoveryAction.RENDER_FROM_STORAGE  # not consumed, no intent
    capsys.readouterr()
    outcome = recover(world, 1)  # the stored directory still digests to the ledger's record
    assert outcome.rendered and not outcome.canonical and outcome.action == "render_from_storage"
    assert world.ledger().recover(1).action is RecoveryAction.CONSUMED
    assert "[eval] slot 1 primary" in capsys.readouterr().out


def test_only_slots_zero_and_zero_b_can_be_canonical(world):
    candidate_prereg(world)
    assert world.run(0, skip_refusals=True).canonical is False
    assert world.run("0b").canonical is True
    assert sealed_run.results_v6_path().is_file()


def test_a_confirmatory_summary_is_numbers_only_and_empty_for_a_descriptive_slot(world):
    world.run(0)
    doc0 = world.stored_doc(0)
    assert sealed_report.confirmatory_summary(doc0, frozenset()) == []
    doc = dict(doc0, slot="1", baseline={"run_id": "r", "results_digest": "0" * 64, "hits": {}})
    assert sealed_report.confirmatory_summary(dict(doc, baseline=None), frozenset()) == [
        "no baseline in the stored run; no comparison"
    ]


# --- A4 -----------------------------------------------------------------------------------------------------------


def test_a_baseline_must_have_published_a_canonical_report(world):
    config_hash = candidate_prereg(world)
    assert world.run(0, skip_refusals=True).canonical is False  # slot 0: consumed, but no intent
    assert world.run("0b").canonical is True  # 0b is the canonical one
    r0, r0b = run_id_of(world, 0), run_id_of(world, "0b")

    def token(baseline):
        return world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=baseline)

    assert gate_of(lambda: token(r0)) == "baseline"  # a consumed slot 0 run without an intent is no baseline
    assert kinds(world).count("slot_claim") == 2  # and the refusal claimed nothing
    good = token(r0b)
    assert good.baseline["run_id"] == r0b


def test_a_baseline_with_no_canonical_run_at_all_is_refused(world):
    config_hash = candidate_prereg(world)
    assert world.run(0, skip_refusals=True).canonical is False
    assert world.run("0b", skip_refusals=True).canonical is False
    for run in (run_id_of(world, 0), run_id_of(world, "0b")):
        assert gate_of(lambda: world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=run)) == "baseline"


# --- A5 -----------------------------------------------------------------------------------------------------------


class IoLog:
    """Ordered log of renames, directory fsyncs (by name) and ledger appends during a block."""

    def __init__(self, monkeypatch, dirs):
        self.events = []
        self.dirs = dirs
        self.fail_after_rename_of = None  # (file name renamed, directory name whose fsync then fails)
        real_fsync, real_replace, real_write = os.fsync, os.replace, SealedLedger._write
        log = self

        def dir_name(fd):
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                return None
            for name, path in log.dirs.items():
                try:
                    if os.stat(path).st_ino == st.st_ino:
                        return name
                except FileNotFoundError:
                    continue
            return "other"

        def fsync(fd):
            name = dir_name(fd)
            if name is not None:
                fail = log.fail_after_rename_of
                if fail and ("replace", fail[0]) in log.events and name == fail[1]:
                    raise OSError(errno.EIO, "injected fsync failure")
                log.events.append(("fsync_dir", name))
            return real_fsync(fd)

        def replace(src, dst):
            log.events.append(("replace", os.path.basename(str(dst))))
            return real_replace(src, dst)

        def write(self, record):
            log.events.append(("append", record["kind"]))
            return real_write(self, record)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(SealedLedger, "_write", write)

    def index(self, event, after=0):
        return next(i for i, e in enumerate(self.events) if e == event and i >= after)


def test_the_reports_directory_is_fsynced_after_the_rename_and_before_consumed(world, monkeypatch):
    eval_dir = world.private.parent
    log = IoLog(monkeypatch, {"eval": eval_dir})
    world.run(0)
    renamed = log.index(("replace", "results_v6.md"))
    synced = log.index(("fsync_dir", "eval"), after=renamed)
    consumed = log.index(("append", "slot_consumed"))
    assert renamed < synced < consumed


def test_a_failed_directory_fsync_stops_the_slot_before_it_is_consumed(world, monkeypatch):
    log = IoLog(monkeypatch, {"eval": world.private.parent})
    log.fail_after_rename_of = ("results_v6.md", "eval")
    with pytest.raises(OSError):
        world.run(0)
    assert kinds(world)[-1] == "publication_intent" and "slot_consumed" not in kinds(world)
    assert world.ledger().recover(0).action is RecoveryAction.REPUBLISH  # the intent stands: recovery republishes
    log.fail_after_rename_of = None  # the disk is healthy again
    assert recover(world, 0).canonical and world.ledger().recover(0).action is RecoveryAction.CONSUMED


def test_a_confirmatory_reports_directory_is_fsynced_before_consumed_too(world, monkeypatch):
    config_hash = candidate_prereg(world)
    world.run(0)
    r0 = run_id_of(world, 0)
    token = world.token(1, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    log = IoLog(monkeypatch, {"rundir": world.run_dir(token.run_id)})
    world.run(1, token=token, expansion="live", rewrite_candidate=config_hash, baseline_run=r0)
    renamed = log.index(("replace", "report.md"))
    assert renamed < log.index(("fsync_dir", "rundir"), after=renamed) < log.index(("append", "slot_consumed"))


def test_a_new_run_directorys_ancestry_is_fsynced_before_the_results_are_recorded(world, monkeypatch):
    sealed_dir = world.private / "sealed"
    log = IoLog(monkeypatch, {"runs": sealed_dir / "runs", "sealed": sealed_dir})
    world.run(0)
    stored = log.index(("append", "slot_stored"))
    assert ("fsync_dir", "runs") in log.events[:stored]  # the run directory's entry in runs/
    assert ("fsync_dir", "sealed") in log.events[:stored]  # and runs/ itself in sealed/ (it was created for this run)
