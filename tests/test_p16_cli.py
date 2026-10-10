"""Phase 16A-1: the `pipeline eval` CLI contract (privacy from classify, sealed exit 4, meter, spend exits)."""

import json

import pytest

import src.pipeline
from src.spend import SpendLimitReached, SpendMeter


def _run(monkeypatch, argv, runner=None):
    captured = {}

    def spy(set_specs, **kwargs):
        captured["set_specs"] = set_specs
        captured["kwargs"] = kwargs
        if runner is not None:
            runner(set_specs, **kwargs)

    monkeypatch.setattr("src.evaluator.run_eval_matrix", spy)
    monkeypatch.setattr("sys.argv", ["prog", "eval", *argv])
    code = 0
    try:
        src.pipeline.main()
    except SystemExit as exc:
        code = exc.code
    return code, captured


def _set(path, sealed=False):
    row = {"question": "synthetic widget question", "type": "direct", "expected_sections": ["91.1"]}
    if sealed:
        row["sealed"] = True
    path.write_text(json.dumps(row) + "\n")
    return str(path)


def test_public_sets_pass_public(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--skip-completeness"])
    assert code == 0
    assert c["kwargs"]["privacy"] == "public"


def test_unregistered_set_passes_private(monkeypatch, tmp_path):
    code, c = _run(monkeypatch, ["--golden", _set(tmp_path / "g.jsonl"), "--skip-refusals", "--skip-completeness"])
    assert code == 0
    assert c["kwargs"]["privacy"] == "private"


def test_sealed_exits_4_before_runner(monkeypatch, tmp_path):
    code, c = _run(monkeypatch, ["--golden", _set(tmp_path / "s.jsonl", sealed=True), "--skip-refusals", "--skip-completeness"])
    assert code == 4
    assert c == {}


def test_offline_builds_no_meter(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--skip-completeness"])
    assert c["kwargs"]["meter"] is None


def test_live_builds_meter(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals"])
    assert isinstance(c["kwargs"]["meter"], SpendMeter)


@pytest.mark.parametrize("kind,expected", [("week", 3), ("run", 6)])
def test_spend_limit_exit_codes(monkeypatch, kind, expected):
    def boom(set_specs, **kwargs):
        raise SpendLimitReached(kind)

    code, _ = _run(monkeypatch, ["--skip-refusals"], runner=boom)
    assert code == expected


def test_private_error_prints_type_only(monkeypatch, tmp_path, capsys):
    def boom(set_specs, **kwargs):
        raise RuntimeError("P16-CANARY-cli-exception text")

    code, _ = _run(monkeypatch, ["--golden", _set(tmp_path / "g.jsonl"), "--skip-refusals", "--skip-completeness"], runner=boom)
    out = capsys.readouterr()
    assert code == 1
    assert "P16-CANARY" not in out.out + out.err
    assert "RuntimeError" in out.err


def test_public_error_still_raises(monkeypatch):
    def boom(set_specs, **kwargs):
        raise RuntimeError("public failure")

    with pytest.raises(RuntimeError):
        _run(monkeypatch, ["--skip-refusals", "--skip-completeness"], runner=boom)


def test_expansion_flag_threads(monkeypatch):
    code, c = _run(monkeypatch, ["--skip-refusals", "--expansion", "build:x.json"])
    assert c["kwargs"]["expansion"] == "build:x.json"
