"""Phase 16A-1 (c) AST rules and (n) hygiene.

(c): the literal ``privacy="public"`` and any registry, classifier or
class-setting parameter stay out of ``src/`` and ``scripts/`` (only tests
register sets or pass a public class); every ``privacy`` parameter of a
runner or formatter is keyword-only with no default.
(n): ``eval/private/`` is ignored, refused by the never-commit check and
untracked; ``src.render`` no longer imports ``src.evaluator``; ``w_sweep``
does not chdir at import.
16A-2a (a1): no function takes a parameter whose name contains ``sealed``,
except in ``src/eval_sealed_read.py`` and the four scripts that handle sealed
rows (D65 reconciled in D72).
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
SEALED_SRC_ALLOWED = ("src/eval_sealed_read.py",)
SEALED_SCRIPTS_ALLOWED = ("validate_eval_set", "scan_leaks", "sealed_tool", "sp_launch")


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


def _sealed_param_violations(path, root):
    """``(function, parameter)`` pairs breaking the sealed-parameter rule in one file.

    The rule: outside ``src/eval_sealed_read.py`` (in ``src/``) and outside
    ``scripts/{validate_eval_set,scan_leaks,sealed_tool,sp_launch}.py`` (in
    ``scripts/``), no function or lambda has a parameter, of any kind, whose
    name contains ``sealed`` in any casing. A file outside both directories is
    held to the strict ``src/`` rule.
    """
    rel = path.resolve().relative_to(root.resolve())
    if rel.as_posix() in SEALED_SRC_ALLOWED:
        return []
    if rel.parts[0] == "scripts" and rel.stem in SEALED_SCRIPTS_ALLOWED:
        return []
    bad = []
    for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        a = fn.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *filter(None, [a.vararg, a.kwarg])]
        bad += [(getattr(fn, "name", "<lambda>"), p.arg) for p in params if "sealed" in p.arg.lower()]
    return bad


@pytest.mark.parametrize("path", CODE, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_sealed_parameters_outside_the_sealed_modules(path):
    bad = _sealed_param_violations(path, ROOT)
    assert not bad, f"{path.relative_to(ROOT)}: parameter(s) containing 'sealed': {bad}"


def test_sealed_parameter_rule_has_its_allowed_files():
    """The allowlist names real files (the two not yet written are listed by D72)."""
    assert (ROOT / "src" / "eval_sealed_read.py").is_file()
    for name in ("validate_eval_set", "scan_leaks"):
        assert (ROOT / "scripts" / f"{name}.py").is_file()


@pytest.mark.parametrize(
    "rel,source",
    [
        ("src/other.py", "def f(sealed): pass\n"),
        ("src/other.py", "def f(x, *, allow_sealed=False): pass\n"),
        ("src/other.py", "def f(a, /, SealedInput): pass\n"),
        ("src/other.py", "def f(*sealed_args): pass\n"),
        ("src/other.py", "def f(**sealed_kw): pass\n"),
        ("src/other.py", "class C:\n    async def m(self, sealed): pass\n"),
        ("src/other.py", "g = lambda sealed: sealed\n"),
        ("scripts/other_tool.py", "def f(sealed): pass\n"),
        ("src/sealed_tool.py", "def f(sealed): pass\n"),  # only scripts/ has the script allowlist
        ("scripts/eval_sealed_read.py", "def f(sealed): pass\n"),  # only src/ has the src allowlist
    ],
)
def test_sealed_parameter_rule_fails_on_a_planted_violation(tmp_path, rel, source):
    planted = tmp_path / rel
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(source, encoding="utf-8")
    assert _sealed_param_violations(planted, tmp_path), source


@pytest.mark.parametrize(
    "rel",
    [
        "src/eval_sealed_read.py",
        "scripts/validate_eval_set.py",
        "scripts/scan_leaks.py",
        "scripts/sealed_tool.py",
        "scripts/sp_launch.py",
    ],
)
def test_sealed_parameter_rule_allows_the_named_files(tmp_path, rel):
    planted = tmp_path / rel
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("def f(sealed, *, allow_sealed=False): pass\n", encoding="utf-8")
    assert _sealed_param_violations(planted, tmp_path) == []


def test_sealed_parameter_rule_ignores_names_and_attributes(tmp_path):
    """Only parameters count: a function, attribute or local named ``sealed`` is fine."""
    planted = tmp_path / "src" / "ok.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "def is_sealed(path):\n    sealed = path.sealed\n    return sealed\n", encoding="utf-8"
    )
    assert _sealed_param_violations(planted, tmp_path) == []


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


def test_w_sweep_has_no_module_level_chdir():
    tree = ast.parse((ROOT / "scripts" / "w_sweep.py").read_text(encoding="utf-8"))
    for node in tree.body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) == "chdir":
                if not any(isinstance(node, t) for t in (ast.FunctionDef, ast.ClassDef)):
                    pytest.fail("w_sweep.py calls chdir at module level")
