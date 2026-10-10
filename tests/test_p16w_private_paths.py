"""Phase 16A-1 item 1: private destinations stay under the private root (symlink/.. refused)."""

import json
import os

import pytest

from src.eval_privacy import (
    PrivatePathError,
    artifact_path,
    contained_path,
    run_dir,
    safe_error,
    write_inputs_json,
    write_private,
)


def test_run_dir_created_under_private_root(_private_root_in_tmp):
    d = run_dir("run-001")
    assert d == _private_root_in_tmp / "runs" / "run-001" and d.is_dir()


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "", "x", "has space", ".hidden"])
def test_run_id_charset(bad):
    with pytest.raises((ValueError, PrivatePathError)):
        run_dir(bad)


def test_dotdot_escape_refused(_private_root_in_tmp):
    with pytest.raises(PrivatePathError):
        contained_path(_private_root_in_tmp, "runs", "../../outside")
    with pytest.raises(PrivatePathError):
        artifact_path("../escape.json")
    with pytest.raises(PrivatePathError):
        contained_path(_private_root_in_tmp, "/etc/passwd")


def test_symlink_escape_refused(tmp_path, _private_root_in_tmp):
    outside = tmp_path / "outside"
    outside.mkdir()
    (_private_root_in_tmp / "runs").mkdir(parents=True)
    os.symlink(outside, _private_root_in_tmp / "runs" / "evil")
    with pytest.raises(PrivatePathError):
        run_dir("evil")
    os.symlink(outside, _private_root_in_tmp / "artifacts")
    with pytest.raises(PrivatePathError):
        artifact_path("x.json")


def test_symlinked_private_root_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linkroot"
    os.symlink(real, link)
    monkeypatch.setattr("src.eval_privacy.private_root", lambda: link)
    with pytest.raises(PrivatePathError):
        run_dir("run-001")


def test_write_private_refuses_outside(tmp_path):
    with pytest.raises(PrivatePathError):
        write_private(tmp_path / "not_private.md", "x")


def test_write_inputs_json(_private_root_in_tmp):
    d = run_dir("run-002")
    out = write_inputs_json(d, [
        {"path": "eval/private/a.jsonl", "sha256": "a" * 64, "kind": "questions"},
        {"path": "eval/private/artifacts/e.json", "sha256": "b" * 64, "kind": "derived",
         "sources": [{"path": "eval/private/a.jsonl", "sha256": "a" * 64}]},
    ])
    data = json.loads(out.read_text())
    assert [i["kind"] for i in data["inputs"]] == ["questions", "derived"]
    with pytest.raises(ValueError):
        write_inputs_json(d, [{"path": "x", "sha256": "a" * 64, "kind": "other"}])


def test_safe_error_never_carries_message():
    exc = RuntimeError("P16-CANARY-exc-secret question text")
    assert safe_error(exc) == "RuntimeError"
    assert safe_error(exc, "q:abc") == "q:abc: RuntimeError"


def test_relative_to_root_accepts_a_samefile_alias(tmp_path, monkeypatch, _private_root_in_tmp):
    """A case-variant spelling (samefile on macOS) resolves to the same relative path."""
    import os

    from src.eval_privacy import relative_to_root

    (_private_root_in_tmp / "runs" / "r1").mkdir(parents=True)
    alias_root = tmp_path / "eval" / "PRIVATE"
    real = os.path.samefile
    monkeypatch.setattr(os.path, "samefile", lambda a, b: real(str(a).replace("PRIVATE", "private"), b))
    alias_root.mkdir(parents=True)
    assert relative_to_root(alias_root / "runs" / "r1" / "report.md", _private_root_in_tmp) == \
        __import__("pathlib").Path("runs/r1/report.md")
    with pytest.raises(PrivatePathError):
        relative_to_root(tmp_path / "elsewhere.md", _private_root_in_tmp)


def test_symlink_planted_at_temp_path_is_refused(tmp_path, _private_root_in_tmp):
    """Gate round 2: <report>.tmp symlinked outside must not redirect a private write."""
    d = run_dir("r-probe-1")
    outside = tmp_path / "outside_leak.md"
    os.symlink(outside, d / "report.md.tmp")
    with pytest.raises(PrivatePathError):
        write_private(d / "report.md", "PRIVATE QUESTION TEXT\n")
    assert not outside.exists()


def test_symlink_planted_at_artifact_temp_path_is_refused(tmp_path, _private_root_in_tmp):
    from src.expansion_artifact import save_artifact

    target = artifact_path("x.json")
    outside = tmp_path / "outside.json"
    os.symlink(outside, target.with_name("x.json.tmp"))
    from src.expansion_artifact import build_artifact
    from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion

    art = build_artifact([("row-1", "synthetic q")], lambda q: Expansion(q, ("r",), REWRITE_MODEL, STATUS_LIVE),
                         [{"path": "x.jsonl", "sha256": "a" * 64, "kind": "questions"}])
    with pytest.raises(PrivatePathError):
        save_artifact(target, art)
    assert not outside.exists()
