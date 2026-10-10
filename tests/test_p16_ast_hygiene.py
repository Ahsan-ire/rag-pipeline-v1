"""Phase 16A-1 (c) AST rules and (n) hygiene.

(c): the literal ``privacy="public"`` and any registry, classifier or
class-setting parameter stay out of ``src/`` and ``scripts/`` (only tests
register sets or pass a public class); every ``privacy`` parameter of a
runner or formatter is keyword-only with no default.
(n): ``eval/private/`` is ignored, refused by the never-commit check and
untracked; ``src.render`` no longer imports ``src.evaluator``; ``w_sweep``
does not chdir at import.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE = sorted([*ROOT.glob("src/*.py"), *ROOT.glob("scripts/*.py")])
BANNED_PARAM_PARTS = ("registry", "classif", "privacy_class", "set_class", "floor_override")
ENTRY_FUNCS = {
    "src/evaluator.py": ("run_eval", "run_eval_matrix", "_format_report", "_format_matrix_report"),
}


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.mark.parametrize("path", CODE, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_privacy_literal_keyword(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "privacy" and isinstance(node.value, ast.Constant):
            pytest.fail(f"{path.name}:{node.value.lineno}: literal privacy= keyword in production code")


@pytest.mark.parametrize("path", CODE, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_registry_or_classifier_parameters(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in _functions(tree):
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            if any(part in arg.arg for part in BANNED_PARAM_PARTS):
                pytest.fail(f"{path.name}:{fn.name}: parameter {arg.arg!r} sets a registry/class")


@pytest.mark.parametrize("rel,names", sorted(ENTRY_FUNCS.items()))
def test_entry_privacy_is_keyword_only_without_default(rel, names):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found = {fn.name: fn for fn in _functions(tree) if fn.name in names}
    assert set(found) == set(names)
    for name, fn in found.items():
        kw = [a.arg for a in fn.args.kwonlyargs]
        assert "privacy" in kw, name
        default = fn.args.kw_defaults[kw.index("privacy")]
        assert default is None, f"{name}: privacy has a default"
        assert "privacy" not in [a.arg for a in fn.args.args], name


@pytest.mark.xfail(strict=True, reason="w_sweep/bakeoff_report privacy floor and chdir removal land with the C4/item-9 commit; strict, so this flips to a failure once they do")
def test_scripts_entry_functions_take_privacy_keyword_only():
    for rel in ("scripts/w_sweep.py", "scripts/bakeoff_report.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        privacy_fns = [
            fn for fn in _functions(tree)
            if "privacy" in [a.arg for a in fn.args.kwonlyargs]
        ]
        assert privacy_fns, f"{rel}: no entry function takes keyword-only privacy"
        for fn in privacy_fns:
            kw = [a.arg for a in fn.args.kwonlyargs]
            assert fn.args.kw_defaults[kw.index("privacy")] is None, f"{rel}:{fn.name}"


# --- (n) hygiene ---------------------------------------------------------------
def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_private_probe_is_git_ignored():
    assert _git("check-ignore", "eval/private/probe.jsonl").returncode == 0
    assert _git("check-ignore", "eval/results_partial.md.rows.json").returncode == 0


def test_never_commit_rejects_private():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_never_commit

    assert check_never_commit.offenders(["eval/private/x.jsonl"]) == ["eval/private/x.jsonl"]
    assert check_never_commit.offenders(["eval/sets.json"]) == []


def test_nothing_tracked_under_eval_private():
    assert _git("ls-files", "eval/private").stdout.strip() == ""


def test_render_does_not_import_evaluator():
    code = "import sys, src.render; print('src.evaluator' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.xfail(strict=True, reason="w_sweep/bakeoff_report privacy floor and chdir removal land with the C4/item-9 commit; strict, so this flips to a failure once they do")
def test_w_sweep_has_no_module_level_chdir():
    tree = ast.parse((ROOT / "scripts" / "w_sweep.py").read_text(encoding="utf-8"))
    for node in tree.body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) == "chdir":
                if not any(isinstance(node, t) for t in (ast.FunctionDef, ast.ClassDef)):
                    pytest.fail("w_sweep.py calls chdir at module level")
