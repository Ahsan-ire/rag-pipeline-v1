"""16A-2b b1a closure round r7, T1 (Codex BLOCKER): the injection seams are test-only.

``run_slot(scan=...)``, ``recover_slot(scan=...)``, ``finish_slot(scan=, render=)``, ``render_and_publish``, the ``matrix_kwargs``
adapters (retrieval, generation, judge, provenance), ``run_eval_matrix``'s own adapters and recorder, and ``SpendMeter(inner_factory=)``
can each change what a run returns while every identity hash stays the same, so none of them is honoured outside the suite:
``src.seams.TEST_SEAMS`` is False by default, ``tests/conftest.py`` sets it True, and each entry point refuses a non-default seam
with the flag False before any claim, ledger write or call. Nothing under ``src/`` or ``scripts/`` may assign the flag.

Synthetic data only (``tests/p16_slot.py``); every model is faked and the network is never touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import src.evaluator as ev
from src import eval_privacy, seams, sealed_run, spend
from src.sealed_ledger import SealedLedger
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    _langsmith_env_cache,
    bare,
    kinds,
    make_world,
    world,
)

ROOT = Path(__file__).resolve().parent.parent


def switch_off(monkeypatch):
    """The state of a shipped process: no test-only seam is honoured. Called after the test has built what needs a seam
    (the world's tokens and run arguments use a fake-client meter)."""
    monkeypatch.setattr(seams, "TEST_SEAMS", False)


@pytest.fixture
def seams_off(monkeypatch):
    switch_off(monkeypatch)
    return monkeypatch


# --- the flag ---------------------------------------------------------------------------------------------------------------------
def test_the_flag_is_false_in_a_fresh_interpreter_and_true_in_the_suite():
    assert seams.TEST_SEAMS is True and seams.enabled() is True  # tests/conftest.py
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from src import seams; print(seams.TEST_SEAMS, seams.enabled())"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"]


def flag_assignments(source: str) -> list:
    """Every place ``source`` writes ``TEST_SEAMS`` other than the one ``TEST_SEAMS = False`` of ``src/seams.py``: an assignment
    to a name or an attribute, an augmented or annotated assignment, ``global``, ``del``, a string naming it (``setattr``,
    ``monkeypatch.setattr("src.seams.TEST_SEAMS", True)``, ``globals()["TEST_SEAMS"]``) or a keyword of that name."""
    found = []
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            for sub in ast.walk(target):
                if (isinstance(sub, ast.Name) and sub.id == "TEST_SEAMS") or (
                    isinstance(sub, ast.Attribute) and sub.attr == "TEST_SEAMS"
                ):
                    found.append(("assign", node.lineno))
        if isinstance(node, ast.Global) and "TEST_SEAMS" in node.names:
            found.append(("global", node.lineno))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "TEST_SEAMS" in node.value:
            found.append(("string", node.lineno))
        if isinstance(node, ast.keyword) and node.arg == "TEST_SEAMS":
            found.append(("keyword", getattr(node.value, "lineno", 0)))
    return found


@pytest.mark.parametrize("source", [
    "TEST_SEAMS = True",
    "seams.TEST_SEAMS = True",
    "from src import seams\nseams.TEST_SEAMS = bool(1)",
    "TEST_SEAMS += 1",
    "TEST_SEAMS: bool = True",
    "def f():\n    global TEST_SEAMS\n    TEST_SEAMS = True",
    "setattr(seams, 'TEST_SEAMS', True)",
    "monkeypatch.setattr('src.seams.TEST_SEAMS', True)",
    "globals()['TEST_SEAMS'] = True",
    "dict(TEST_SEAMS=True)",
    "(TEST_SEAMS := True)",
    "import importlib\nm = importlib.import_module('src.seams')\nm.TEST_SEAMS = 1",
])
def test_the_detector_finds_every_way_of_switching_the_flag_on(source):
    assert flag_assignments(source), source


def test_no_module_under_src_or_scripts_assigns_the_flag_but_the_one_false_default():
    checked = 0
    for folder in ("src", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "TEST_SEAMS" not in text:
                continue
            checked += 1
            found = flag_assignments(text)
            if path == ROOT / "src" / "seams.py":
                tree = ast.parse(text)
                defaults = [n for n in tree.body if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "TEST_SEAMS" for t in n.targets)]
                assert len(defaults) == 1 and isinstance(defaults[0].value, ast.Constant) and defaults[0].value.value is False
                found = [f for f in found if f != ("assign", defaults[0].lineno)]
                found = [f for f in found if f[0] != "string"]  # (the module docstring names it)
            else:
                found = [f for f in found if f[0] != "string"]  # a docstring or message may name it; an assignment never
            assert found == [], (str(path), found)
    assert checked >= 3  # the scan is looking at the files that use the flag


def test_the_only_way_a_shipped_module_reads_the_flag_is_through_the_seams_module():
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.seams":
                assert all(alias.name != "TEST_SEAMS" for alias in node.names), path.name  # a copied flag would be stale


# --- the slot entry points ----------------------------------------------------------------------------------------------------------
def seam_of(call) -> sealed_run.GateRefused:
    with pytest.raises(sealed_run.GateRefused) as info:
        call()
    assert info.value.gate == "seam"
    return info.value


def test_a_fresh_run_with_an_injected_adapter_is_refused_before_the_claim(world, monkeypatch):
    token = world.token(0, claim=False)
    before = kinds(world)
    args = world.run_args()
    assert set(args["matrix_kwargs"]) >= {"retrieve_fn_factory", "generate_fn", "judge_fn", "provenance_fn"}
    switch_off(monkeypatch)
    exc = seam_of(lambda: sealed_run.run_slot(token, ledger=world.ledger(), **args))
    assert exc.detail == "matrix_kwargs"
    assert kinds(world) == before and "slot_claim" not in kinds(world)  # no claim, no ledger write
    assert not token.claimed[0] and not token.used[0]
    with world.ledger().lock_slot("0"):  # the refusal released the slot's execution lock
        pass


def test_a_fresh_run_with_an_injected_scanner_is_refused_before_the_claim(world, monkeypatch):
    token = world.token(0, claim=False)
    before = kinds(world)
    args = {**world.run_args(), "matrix_kwargs": None}
    switch_off(monkeypatch)
    exc = seam_of(lambda: sealed_run.run_slot(token, ledger=world.ledger(), scan=lambda text: [], **args))
    assert exc.detail == "scan" and kinds(world) == before and not token.claimed[0]
    with world.ledger().lock_slot("0"):
        pass


def test_the_preflight_refuses_injected_adapters_too(world, monkeypatch):
    token = world.token(0, claim=False)
    args = world.run_args()
    switch_off(monkeypatch)
    try:
        exc = seam_of(lambda: sealed_run.preflight_slot(token, **args))
        assert exc.detail == "matrix_kwargs" and not token.claimed[0]
    finally:
        token.release()


def test_recovery_refuses_an_injected_scanner_before_it_touches_the_slot(world, monkeypatch):
    assert world.run(0).canonical is True
    before = world.ledger().records()
    monkeypatch.setattr(seams, "TEST_SEAMS", False)
    exc = seam_of(lambda: sealed_run.recover_slot(0, ledger=world.ledger(), scan=lambda text: []))
    assert exc.detail == "scan"  # (without the scanner, recovery says what it always said: the slot is consumed)
    with pytest.raises(sealed_run.GateRefused) as consumed:
        sealed_run.recover_slot(0, ledger=world.ledger())
    assert (consumed.value.gate, consumed.value.detail) == ("claim", "consumed")
    assert world.ledger().records() == before


def test_finishing_a_stored_slot_refuses_an_injected_scanner_or_renderer(world, seams_off):
    ledger = world.ledger()
    for kwargs, detail in (({"scan": lambda text: []}, "scan"), ({"render": lambda *a, **k: "text"}, "render")):
        assert seam_of(lambda: sealed_run.finish_slot(0, ledger=ledger, **kwargs)).detail == detail
    assert ledger.records() == world.ledger().records()


def test_publishing_with_a_scanner_the_module_did_not_build_or_a_renderer_is_refused(world, seams_off):
    ledger = world.ledger()
    doc = {"groups": {}}
    for kwargs, detail in (
        ({"scan": lambda text: [], "render": None}, "scan"), ({"scan": sealed_run._DefaultScan(None, None), "render": lambda *a, **k: ""}, "render"),
    ):
        exc = seam_of(lambda: sealed_run.render_and_publish(
            doc, ledger=ledger, excluded=frozenset(), failed=(), publish_to=None, **kwargs))
        assert exc.detail == detail


def test_the_modules_own_scanner_is_not_a_seam(world, seams_off):
    """``default_scanner`` builds ``_DefaultScan``; ``render_and_publish`` honours exactly that type with no renderer."""
    class FakeLeaks:
        class Hit:
            family = "f0"

        @staticmethod
        def scan_text(text, target, index):
            return [FakeLeaks.Hit] if "needle" in text else []

    scan = sealed_run._DefaultScan(FakeLeaks, object())
    assert scan("a needle here") == ["f0"] and scan("clean") == []
    # accepted by the seam check: the call goes on to render the (empty) document and fails for its own, later, reason
    with pytest.raises(Exception) as info:
        sealed_run.render_and_publish({}, ledger=world.ledger(), excluded=frozenset(), failed=(), scan=scan, publish_to=None)
    assert not (isinstance(info.value, sealed_run.GateRefused) and info.value.gate == "seam")


# --- the matrix runner -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seam", ["retrieve_fn_factory", "generate_fn", "judge_fn", "provenance_fn", "record_draw"])
def test_run_eval_matrix_refuses_each_injected_adapter_before_any_work(world, seams_off, seam):
    fake = (lambda *a, **k: None) if seam != "retrieve_fn_factory" else (lambda mode: lambda q, top_k=6: [])
    runs_before = sorted((world.private / "runs").iterdir()) if (world.private / "runs").exists() else []
    exc = seam_of(lambda: ev.run_eval_matrix(
        [("tuning", str(world.golden_path)), ("realistic", str(world.realistic_path))], modes=["hybrid"], top_k=6,
        skip_refusals=True, skip_completeness=True, persist_directory="unused", privacy="private", **{seam: fake}))
    assert exc.detail == seam
    after = sorted((world.private / "runs").iterdir()) if (world.private / "runs").exists() else []
    assert after == runs_before  # no run directory, no report, no sidecar
    assert "dev_run_unrecorded" not in kinds(world)


def test_a_draw_recorder_of_the_modules_own_type_is_not_a_seam(world, seams_off):
    recorder = sealed_run.DrawRecorder(ledger=world.ledger(), plan=None, persist_directory="unused", top_k=6)
    # the seam check passes; the call then fails on its own first argument check (no sets), which is the proof it went past
    with pytest.raises(ValueError, match="at least one"):
        ev.run_eval_matrix([], privacy="private", record_draw=recorder)


def test_the_default_adapters_need_no_seam(seams_off, monkeypatch):
    """With every seam left at its default the entry point asks for no seam at all (it goes on to its own argument checks;
    nothing is retrieved and no model is loaded: the set list is empty)."""
    asked = []
    monkeypatch.setattr(sealed_run, "require_seam", lambda name: asked.append(name))
    with pytest.raises(ValueError, match="at least one"):
        ev.run_eval_matrix([], privacy="private")
    assert asked == []


# --- the meter ---------------------------------------------------------------------------------------------------------------------------
def test_a_meter_with_an_injected_client_factory_is_refused_outside_the_suite(seams_off):
    prices = spend.load_prices(ROOT / "config" / "api_prices.toml")
    with pytest.raises(spend.SeamRefused):
        spend.SpendMeter(prices, None, 1.0, inner_factory=lambda kind: object())
    assert isinstance(spend.SpendMeter(prices, None, 1.0), spend.SpendMeter)  # the default factory needs no seam
    assert issubclass(spend.SeamRefused, spend.SpendMeterError)  # the CLI's meter-failure exit covers it


def test_a_meter_with_an_injected_client_factory_works_in_the_suite():
    prices = spend.load_prices(ROOT / "config" / "api_prices.toml")
    assert isinstance(spend.SpendMeter(prices, None, 1.0, inner_factory=lambda kind: object()), spend.SpendMeter)
