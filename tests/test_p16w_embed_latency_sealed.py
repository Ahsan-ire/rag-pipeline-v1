"""Phase 16A-2a phase gate, Area C (C5): ``scripts/embed_latency.py`` refuses sealed input.

The script reads a question file and embeds each question; a sealed question must
never be read by it (classify -> sealed -> exit 4, like every eval entry point).
Every question is invented; the private root is the per-test ``tmp_path/eval/private``
(conftest) and no model is loaded (the embedder is faked).
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from scripts import embed_latency
from src.eval_privacy import SealedInputError

CANARY = "zorbin keldwyn plimsy tarnock vessuvio"


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.queries: list = []

    def embed_query(self, text: str):
        self.queries.append(text)
        return [0.0]


@pytest.fixture
def fake_embedder(monkeypatch):
    fake = _FakeEmbeddings()
    loads: list = []

    def get_embedding_function():
        loads.append(1)
        return fake

    monkeypatch.setattr(embed_latency.embedder, "get_embedding_function", get_embedding_function)
    fake.loads = loads
    return fake


@pytest.fixture
def opened(monkeypatch):
    """Every path ``open`` is asked for while the test runs."""
    seen: list = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    return seen


def _write(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_c5_a_set_under_the_sealed_root_is_refused_before_it_is_read(_private_root_in_tmp, fake_embedder, opened, capsys):
    sealed = _write(_private_root_in_tmp / "sealed" / "heldout.jsonl", [{"question": CANARY}])
    assert embed_latency.main(["--queries", str(sealed), "--n", "5"]) == 4
    cap = capsys.readouterr()
    assert CANARY not in cap.out + cap.err and cap.out == ""
    assert not [p for p in opened if p == str(sealed)], "the sealed file was opened"
    assert fake_embedder.queries == [] and fake_embedder.loads == []  # no model was loaded either


def test_c5_a_set_carrying_a_sealed_marker_anywhere_is_refused(tmp_path, fake_embedder, capsys):
    marked = _write(tmp_path / "elsewhere" / "marked.jsonl", [{"question": CANARY, "sealed": True}])
    assert embed_latency.main(["--queries", str(marked)]) == 4
    cap = capsys.readouterr()
    assert CANARY not in cap.out + cap.err
    assert fake_embedder.queries == [] and fake_embedder.loads == []


def test_c5_load_queries_itself_refuses_a_sealed_path(_private_root_in_tmp):
    sealed = _write(_private_root_in_tmp / "sealed" / "heldout.jsonl", [{"question": CANARY}])
    with pytest.raises(SealedInputError) as exc:
        embed_latency.load_queries(str(sealed), 5)
    assert CANARY not in str(exc.value)


@pytest.mark.parametrize("where", ["private", "public"])
def test_c5_an_unsealed_set_is_still_timed(where, tmp_path, _private_root_in_tmp, fake_embedder, capsys):
    base = _private_root_in_tmp / "dev" if where == "private" else tmp_path / "public"
    qs = _write(base / "set.jsonl", [{"question": f"invented question number {i}"} for i in range(4)])
    assert embed_latency.main(["--queries", str(qs), "--n", "3", "--warmup", "0"]) == 0
    out = capsys.readouterr().out
    assert "| Model | Cold load | p50 | p95 |" in out and "n=3 queries" in out
    assert len(fake_embedder.queries) == 3 and fake_embedder.loads == [1]
