"""16A-2b b1a closure round r7, the identity parts: T2 (the judge's identity covers its execution stack), T3 (the scorer's identity
covers the evaluator's scoring glue) and the identity items of T6 (the scan's data inputs, the scripts the hashed modules import).

Zero calls: the model loaders and the network are refused (``with_index``), and every source or data edit is made in a private
copy that ``pipeline_config.CODE_DIR`` or ``pipeline_config.SCRIPTS_DIR`` is pointed at.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import evaluator, generator, judge, judge_prep, pipeline_config as pc, scoring_glue, scoring_identity as si, spend
from tests.test_p16a2b_b1a_r4_config import (  # noqa: F401  (fixtures and helpers)
    REAL_INDEX_IDENTITY,
    ROOT,
    _dist,
    _real_versions,
    code_copy,
    h,
    with_index,
)
from tests.test_p16a2b_b1a_r6_config import append, edit, scripts_copy  # noqa: F401  (fixtures and helpers)

REAL_SCAN_INPUTS_COMPONENT = pc.scan_inputs_component  # captured at import, before the autouse fixture replaces it


def mutate(copy: Path, name: str, qualname: str) -> None:
    """Insert one statement as the first line of the body of ``qualname`` in the copy of ``name`` (a real code change)."""
    path = copy / name
    text = path.read_text(encoding="utf-8")
    node = pc._find_definition(ast.parse(text), qualname)
    assert node is not None, (name, qualname)
    body = node.body
    first = body[1] if len(body) > 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else body[0]
    lines = text.splitlines(keepends=True)
    lines.insert(first.lineno - 1, " " * first.col_offset + "_R7_MARK = 1\n")
    path.write_text("".join(lines), encoding="utf-8")


# =================================================================================================================================
# T2: the judge's identity covers the code that builds and wraps its client, and the runtime it sends the request through
# =================================================================================================================================
def test_function_digest_names_functions_methods_and_classes():
    spend_py = pc.CODE_DIR / "spend.py"
    digests = {q: pc.function_digest(spend_py, q) for q in (
        "MeteredChatModel", "MeteredChatModel.invoke", "_MeterCallback", "_with_handler", "retry_decision",
        "SpendMeter._client", "SpendMeter.judge_llm")}
    assert len(set(digests.values())) == len(digests) and all(len(v) == 64 for v in digests.values())
    assert pc.function_digest(pc.CODE_DIR / "generator.py", "get_llm") == pc.function_digest(pc.CODE_DIR / "generator.py", "get_llm")
    assert pc.function_digest(pc.CODE_DIR / "generator.py", "_sections_related")  # r6's form: a bare top-level name
    for missing in ("nope", "SpendMeter.nope", "Nope.invoke", "MeteredChatModel.invoke.inner", "SpendMeter._client.x"):
        with pytest.raises(ValueError):
            pc.function_digest(spend_py, missing)


def test_function_digest_ignores_docstrings_and_comments_but_not_code(code_copy):
    path = code_copy / "generator.py"
    before = pc.function_digest(path, "get_llm")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"""Create and return a ChatAnthropic LLM instance.', '"""# A reworded docstring.\n\n    Create a ChatAnthropic LLM instance.', 1)
                    .replace("    return ChatAnthropic(**generation_llm_kwargs())", "    # a comment\n    return ChatAnthropic(**generation_llm_kwargs())", 1),
                    encoding="utf-8")
    assert pc.function_digest(path, "get_llm") == before
    mutate(code_copy, "generator.py", "get_llm")
    assert pc.function_digest(path, "get_llm") != before


def test_the_judge_identity_has_the_execution_stack_in_its_hash_and_the_runtime_beside_it():
    assert judge.JUDGE_RUNTIME_ROOTS == ("anthropic", "langchain-anthropic", "langchain-core")
    assert judge.JUDGE_EXECUTION_STACK["client_builder"] == (("generator.py", "get_llm"),)
    assert ("spend.py", "MeteredChatModel") in judge.JUDGE_EXECUTION_STACK["metering_wrapper"]
    # r8 (Codex M3): the default client factory and the constructor that selects it are on the stack too
    assert {("spend.py", "default_inner_factory"), ("spend.py", "SpendMeter.__init__")} <= set(judge.JUDGE_EXECUTION_STACK["metering_wrapper"])
    for members in judge.JUDGE_EXECUTION_STACK.values():  # every member exists (a typo would be a refusal, never a silent gap)
        for file, qualname in members:
            assert pc.function_digest(pc.CODE_DIR / file, qualname)
    assert {"client_builder", "metering_wrapper"} <= set(judge.JUDGE_COVERS)
    assert "judge_runtime" not in judge.JUDGE_COVERS  # r8 (auditor M1): the runtime is recorded per slot, not registered in the hash
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert prereg["judge"]["covers"] == list(judge.JUDGE_COVERS)
    assert prereg["judge"]["runtime"] == judge.JUDGE_RUNTIME_RULE == "recorded_per_slot_compared_candidate_vs_slot0"
    assert prereg["judge"]["config_hash"] == judge.judge_identity()["config_hash"]  # recomputed, not typed


@pytest.mark.parametrize("file,qualname", [
    ("generator.py", "get_llm"),
    ("spend.py", "default_inner_factory"), ("spend.py", "SpendMeter.__init__"),  # r8 (Codex M3)
    ("spend.py", "MeteredChatModel"), ("spend.py", "MeteredChatModel.invoke"), ("spend.py", "_MeterCallback"),
    ("spend.py", "_MeterCallback.on_chat_model_start"), ("spend.py", "_with_handler"), ("spend.py", "retry_decision"),
    ("spend.py", "SpendMeter._client"), ("spend.py", "SpendMeter.judge_llm"),
])
def test_editing_the_code_a_judge_call_passes_through_moves_the_judge_identity(code_copy, file, qualname):
    before = judge.judge_identity()["config_hash"]
    mutate(code_copy, file, qualname)
    assert judge.judge_identity()["config_hash"] != before, (file, qualname)


def test_the_builder_override_the_nominal_kwargs_could_not_see_is_now_seen(code_copy):
    """Codex 3: ``get_llm`` could override ``max_tokens`` after ``generation_llm_kwargs()`` and the judge hash stayed put."""
    before = judge.judge_identity()["config_hash"]
    path = code_copy / "generator.py"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "    return ChatAnthropic(**generation_llm_kwargs())", '    return ChatAnthropic(**{**generation_llm_kwargs(), "max_tokens": 64})', 1), encoding="utf-8")
    assert judge.judge_identity()["config_hash"] != before


def test_the_rest_of_the_generator_and_the_meter_is_not_the_judges_business(code_copy):
    """Function-level binding: the judge hash does not move with unrelated code of the modules it borrows from."""
    before_judge, before_pipeline = judge.judge_identity()["config_hash"], None
    for file, qualname in (("generator.py", "extract_citations"), ("generator.py", "generation_llm_kwargs"),
                           ("spend.py", "load_prices"), ("spend.py", "SpendMeter.generation_llm"), ("spend.py", "worst_case_usd")):
        mutate(code_copy, file, qualname)
    assert judge.judge_identity()["config_hash"] == before_judge
    del before_pipeline


def test_a_comment_or_docstring_in_the_borrowed_code_does_not_move_the_judge_identity(code_copy):
    before = judge.judge_identity()["config_hash"]
    path = code_copy / "spend.py"
    path.write_text("# a comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert judge.judge_identity()["config_hash"] == before


def versions(monkeypatch, overrides):
    """``importlib.metadata.version`` as installed, except for ``overrides`` ({normalised name: version})."""
    real = importlib.metadata.version

    def fake(name):
        return overrides.get(pc.normalise_distribution_name(name), real(name))

    monkeypatch.setattr(importlib.metadata, "version", fake)


def test_the_judge_runtime_is_the_closure_of_the_three_packages_and_their_requirements():
    closure = pc.distribution_closure(judge.JUDGE_RUNTIME_ROOTS)
    names = {entry.split("==")[0] for entry in closure}
    assert {"anthropic", "langchain-anthropic", "langchain-core"} <= names
    assert {"httpx", "pydantic", "typing-extensions", "tenacity", "langsmith", "packaging"} <= names  # transitive requirements
    assert not names & {"pytest", "chromadb", "sentence-transformers", "rank-bm25", "pdfplumber", "pytest-mock"}  # not on the call path
    assert closure == sorted(closure) and len(set(closure)) == len(closure)
    assert all(entry.count("==") == 1 and not entry.endswith("missing") for entry in closure)  # nothing the environment lacks
    for entry in closure:
        name, version = entry.split("==")
        assert importlib.metadata.version(name) == version


def installed_as(monkeypatch, overrides):
    """``importlib.metadata`` as installed, except that ``overrides`` ({normalised name: version}) are installed at those versions.

    Both readers are changed: ``version`` (the judge's closure) and ``distributions`` (the pipeline hash's runtime).
    """
    versions(monkeypatch, overrides)
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [
        _dist(n, overrides.get(pc.normalise_distribution_name(n), v)) for n, v in _real_versions()])


@pytest.mark.parametrize("name", ["anthropic", "langchain-anthropic", "langchain-core", "httpx", "pydantic", "packaging"])
def test_a_changed_version_of_a_judge_path_distribution_moves_the_runtime_and_the_pipeline_hash_not_the_registered_judge_hash(
        with_index, monkeypatch, name):
    """r8 (auditor M1): the registered ``judge.config_hash`` survives an install; the per-slot ``judge_runtime_sha256`` and the
    pipeline hash (its ``runtime`` component) still see it, so a candidate with another SDK cannot be compared with slot 0 unseen."""
    before, before_pipeline = judge.judge_identity(), h()
    installed_as(monkeypatch, {name: "999.0.0"})
    after = judge.judge_identity()
    assert after["config_hash"] == before["config_hash"]
    assert after["judge_runtime_sha256"] != before["judge_runtime_sha256"] and after["judge_runtime"] != before["judge_runtime"]
    assert f"{name}==999.0.0" in after["judge_runtime"]
    assert h() != before_pipeline


@pytest.mark.parametrize("name", ["pytest", "chromadb", "rank-bm25", "sentence-transformers"])
def test_a_changed_version_of_an_unrelated_distribution_does_not_move_the_judge_identity(monkeypatch, name):
    before = judge.judge_identity()
    versions(monkeypatch, {name: "999.0.0"})
    assert judge.judge_identity() == before  # the hash, the runtime list and its digest alike


def test_the_judge_runtime_is_reported_beside_the_hash_and_is_not_an_input_of_it(monkeypatch):
    identity = judge.judge_identity()
    assert set(identity) == {"model", "prompt_sha256", "config_hash", "judge_runtime", "judge_runtime_sha256"}
    assert identity["judge_runtime"] == pc.distribution_closure(judge.JUDGE_RUNTIME_ROOTS)  # the sorted name==version list
    assert identity["judge_runtime_sha256"] == pc.sha256_hex(pc.canonical_json(identity["judge_runtime"]))
    monkeypatch.setattr(pc, "distribution_closure", lambda roots: ["anthropic==0.0.1", "langchain-core==0.0.2"])
    other = judge.judge_identity()
    assert other["judge_runtime"] == ["anthropic==0.0.1", "langchain-core==0.0.2"]
    assert other["judge_runtime_sha256"] != identity["judge_runtime_sha256"]
    assert other["config_hash"] == identity["config_hash"]  # whatever the runtime is, the registered value is the same


def test_the_closure_follows_applicable_requirements_and_ignores_extras_and_false_markers(monkeypatch):
    graph = {
        "root": ("1.0", ["dep-a>=1", "Dep_B[extra1]>=2; python_version >= '3.0'", "opt-c; extra == 'speed'",
                         "old-d; python_version < '3.0'", "win-e; sys_platform == 'nonexistent-os'", "gone-f"]),
        "dep-a": ("2.0", ["dep-b"]),
        "dep-b": ("3.0", None),
        "opt-c": ("9.0", []),
        "old-d": ("9.0", []),
        "win-e": ("9.0", []),
    }

    def fake_version(name):
        if name not in graph:
            raise importlib.metadata.PackageNotFoundError(name)
        return graph[name][0]

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: graph[name][1])
    assert pc.distribution_closure(["Root"]) == ["dep-a==2.0", "dep-b==3.0", "gone-f==missing", "root==1.0"]
    assert pc.distribution_closure(["root", "dep-a"]) == pc.distribution_closure(["root"])  # roots may overlap


def test_the_closure_follows_an_extra_when_a_requirement_on_the_path_asks_for_it(monkeypatch):
    graph = {
        "root": ("1.0", ["lib[fast]>=1"]),
        "lib": ("2.0", ["core", "fast-lib; extra == 'fast'", "slow-lib; extra == 'slow'", "other[fast]; extra == 'fast'"]),
        "core": ("3.0", []),
        "fast-lib": ("4.0", []),
        "slow-lib": ("5.0", []),
        "other": ("6.0", ["other-fast; extra == 'fast'", "other-slow; extra == 'slow'"]),
        "other-fast": ("7.0", []),
        "other-slow": ("8.0", []),
    }
    monkeypatch.setattr(importlib.metadata, "version", lambda name: graph[name][0])
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: graph[name][1])
    # lib's fast extra is asked for (and, through it, other's fast extra); nobody asks for slow
    assert pc.distribution_closure(["root"]) == [
        "core==3.0", "fast-lib==4.0", "lib==2.0", "other-fast==7.0", "other==6.0", "root==1.0"]
    # the same distribution reached first without the extra and later with it is walked again for the extra
    assert pc.distribution_closure(["lib", "root"]) == pc.distribution_closure(["root", "lib"]) == [
        "core==3.0", "fast-lib==4.0", "lib==2.0", "other-fast==7.0", "other==6.0", "root==1.0"]
    assert "fast-lib==4.0" not in pc.distribution_closure(["lib"])  # alone, lib's extras are not asked for


def test_a_requirement_that_cannot_be_parsed_fails_closed(monkeypatch):
    from packaging.requirements import InvalidRequirement

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0")
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: ["not a valid requirement ("])
    with pytest.raises(InvalidRequirement):
        pc.distribution_closure(["anything"])


# =================================================================================================================================
# T3: the scorer's identity covers the evaluator's scoring glue
# =================================================================================================================================
MOVED_FUNCTIONS = ("hit_cutoffs", "load_absorbed_map", "sidecar_rows", "retrieval_family_count")


def test_the_scoring_glue_is_in_the_scorer_identity_and_the_prereg_registers_it():
    assert "scoring_glue.py" in si.SCORING_CODE_MODULES and "scoring_glue.py" in pc.code_modules()
    assert (pc.CODE_DIR / "scoring_glue.py").is_file() and "scoring_glue.py" in si.covered()
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert prereg["adoption"]["scoring"]["covers"] == si.covered()
    assert prereg["adoption"]["scoring"]["compare"] == "candidate_and_slot0_equal_and_counted_draws_equal_incumbent_draws"


@pytest.mark.parametrize("qualname", MOVED_FUNCTIONS)
def test_editing_each_moved_scoring_function_changes_the_scorer_identity_and_the_pipeline_hash(with_index, code_copy, qualname):
    before_scorer, before_pipeline = si.scorer_identity(), h()
    mutate(code_copy, "scoring_glue.py", qualname)
    assert si.scorer_identity() != before_scorer and h() != before_pipeline


@pytest.mark.parametrize("old,new", [
    ("HIT_KS = (1, 3, 6)", "HIT_KS = (1, 3, 5)"),
    ('ABSORBED_MAP_PATH = os.path.join("eval", "absorbed_sections.json")', 'ABSORBED_MAP_PATH = os.path.join("eval", "other.json")'),
    ('return dict(doc.get("map") or {}), digest.hexdigest()', 'return {}, digest.hexdigest()'),  # what the aliases decode to
    ('"completion_rank": score["strict_rank"],', '"completion_rank": score["related_rank"],'),  # the rank the controls read
    ("return len(families - refusing)", "return len(families)"),
    ("return ks, (max(ks) if ks else top_k)", "return ks, (min(ks) if ks else top_k)"),
])
def test_editing_the_values_and_expressions_of_the_glue_changes_the_scorer_identity(code_copy, old, new):
    before = si.scorer_identity()
    original = edit(code_copy, "scoring_glue.py", old, new)
    assert si.scorer_identity() != before
    (code_copy / "scoring_glue.py").write_text(original, encoding="utf-8")
    assert si.scorer_identity() == before


def test_the_judge_to_row_mapping_is_covered_by_both_the_judge_and_the_scorer(code_copy):
    before_scorer, before_judge = si.scorer_identity(), judge.judge_identity()["config_hash"]
    mutate(code_copy, "judge_prep.py", "judgments_by_row")
    assert si.scorer_identity() != before_scorer and judge.judge_identity()["config_hash"] != before_judge


def test_the_evaluator_defines_none_of_the_scoring_glue_and_imports_it_from_the_covered_module():
    tree = ast.parse((pc.CODE_DIR / "evaluator.py").read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    moved = {"HIT_KS", "ABSORBED_MAP_PATH", "_load_absorbed_map", "load_absorbed_map", *MOVED_FUNCTIONS}
    assert not moved & (defined | assigned)
    assert evaluator.HIT_KS is scoring_glue.HIT_KS and evaluator.ABSORBED_MAP_PATH is scoring_glue.ABSORBED_MAP_PATH
    assert evaluator._load_absorbed_map is scoring_glue.load_absorbed_map
    # the v6 runner does not recompute a cut-off, a rank projection or an eligibility count by hand
    v6 = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_matrix_v6")
    source = ast.unparse(v6)
    for forbidden in ("HIT_KS", "completion_rank", "max(ks)", "refuse_fams", "fam_eligible"):
        assert forbidden not in source, forbidden
    assert "_scoring_glue.hit_cutoffs" in source and "_scoring_glue.sidecar_rows" in source
    assert "_scoring_glue.retrieval_family_count" in source


def test_the_moved_functions_behave_as_the_evaluators_inline_code_did():
    assert scoring_glue.HIT_KS == (1, 3, 6)
    assert [scoring_glue.hit_cutoffs(k) for k in (6, 7, 5, 3, 2, 1, 0)] == [
        ([1, 3, 6], 6), ([1, 3, 6], 6), ([1, 3], 3), ([1, 3], 3), ([1], 1), ([1], 1), ([], 0)]
    scored = {"r1": {"strict_rank": 2, "related_rank": 1, "strict_hit": {6: True}}, "r2": {"strict_rank": None, "related_rank": 4}}
    assert scoring_glue.sidecar_rows("s" * 64, "hybrid", scored) == [
        {"set_sha256": "s" * 64, "id": "r1", "mode": "hybrid", "strict_rank": 2, "related_rank": 1, "completion_rank": 2},
        {"set_sha256": "s" * 64, "id": "r2", "mode": "hybrid", "strict_rank": None, "related_rank": 4, "completion_rank": None}]
    rows = [{"family_id": "a", "scope": "answer"}, {"family_id": "a", "scope": "answer"}, {"family_id": "b", "scope": "refuse"},
            {"family_id": "c", "scope": "partial"}, {"family_id": "d", "scope": "refuse"}]
    assert scoring_glue.retrieval_family_count(rows) == 2 and scoring_glue.retrieval_family_count([]) == 0


def test_the_absorbed_map_is_read_from_the_repo_root_and_hashed_by_its_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring_glue, "REPO_ROOT", str(tmp_path))
    assert scoring_glue.load_absorbed_map() == (None, None)  # absent: nothing absorbed, nothing hashed
    target = tmp_path / scoring_glue.ABSORBED_MAP_PATH
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"map": {"3.1": ["3.1.1", "3.1.2"]}, "notes": "x"}), encoding="utf-8")
    mapping, digest = scoring_glue.load_absorbed_map()
    import hashlib

    assert mapping == {"3.1": ["3.1.1", "3.1.2"]} and digest == hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert scoring_glue.load_absorbed_map()[0] == {}  # a document with no map absorbs nothing
    # the real committed map loads through the same function
    monkeypatch.setattr(scoring_glue, "REPO_ROOT", str(ROOT))
    real = scoring_glue.load_absorbed_map()
    assert real[1] == si.scoring_inputs()["eval/absorbed_sections.json"]


# =================================================================================================================================
# T6: the scan's data inputs and the scripts the hashed modules import
# =================================================================================================================================
def test_scan_inputs_digests_the_files_the_scan_reads_and_none_for_an_absent_one(tmp_path):
    files = {}
    for name in ("dump.jsonl", "sets.json", "legacy.json", "set-a.jsonl"):
        files[name] = tmp_path / name
        files[name].write_bytes(name.encode())
    got = pc.scan_inputs(handbook_dump=files["dump.jsonl"], sets_index=files["sets.json"], legacy_list=files["legacy.json"],
                         public_sets={"b-missing": tmp_path / "missing.jsonl", "a": files["set-a.jsonl"]})
    assert list(got) == ["handbook_dump", "public_registry", "legacy_public", "public_sets"]
    assert got["handbook_dump"] == pc.sha256_hex(b"dump.jsonl") and got["public_registry"] == pc.sha256_hex(b"sets.json")
    assert got["legacy_public"] == pc.sha256_hex(b"legacy.json")
    assert got["public_sets"] == {"a": pc.sha256_hex(b"set-a.jsonl"), "b-missing": None}
    files["dump.jsonl"].write_bytes(b"changed")
    assert pc.scan_inputs(handbook_dump=files["dump.jsonl"], sets_index=files["sets.json"], legacy_list=files["legacy.json"],
                          public_sets={})["handbook_dump"] == pc.sha256_hex(b"changed")  # (the cache is by mtime and size)
    absent = pc.scan_inputs(handbook_dump=tmp_path / "no", sets_index=tmp_path / "no", legacy_list=tmp_path, public_sets={})
    assert absent == {"handbook_dump": None, "public_registry": None, "legacy_public": None, "public_sets": {}}  # a directory is no file


def test_the_component_follows_the_scanners_own_path_constants(tmp_path, monkeypatch):
    from scripts import scan_leaks
    from src import eval_sets

    base = tmp_path / "repo"
    (base / "data" / "research").mkdir(parents=True)
    dump = base / scan_leaks.HANDBOOK_DUMP
    dump.write_bytes(b"handbook text")
    registry, legacy, public = tmp_path / "sets.json", tmp_path / "legacy.json", tmp_path / "public.jsonl"
    registry.write_bytes(b"{}")
    public.write_bytes(b"q")
    monkeypatch.setattr(scan_leaks, "_repo_base", lambda: base)
    monkeypatch.setattr(eval_sets, "REGISTRY_PATH", registry)
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", legacy)  # absent
    monkeypatch.setattr(eval_sets, "public_entries", lambda: [SimpleNamespace(name="tmp-public", resolved=lambda: public)])
    got = REAL_SCAN_INPUTS_COMPONENT()
    assert got == {
        "handbook_dump": pc.sha256_hex(b"handbook text"), "public_registry": pc.sha256_hex(b"{}"), "legacy_public": None,
        "public_sets": {"tmp-public": pc.sha256_hex(b"q")}}
    monkeypatch.setattr(scan_leaks, "HANDBOOK_DUMP", Path("elsewhere") / "dump.jsonl")
    assert REAL_SCAN_INPUTS_COMPONENT()["handbook_dump"] is None  # it follows the constant, it does not copy the path


def test_the_pipeline_hash_moves_with_each_data_input_of_the_scan(with_index, tmp_path, monkeypatch):
    """The suite's autouse fixture replaces the component so that no test reads a real corpus-derived file; here it is wired
    to temporary files, and the pipeline configuration and its hash follow them."""
    dump, registry = tmp_path / "dump.jsonl", tmp_path / "sets.json"
    dump.write_bytes(b"one")
    registry.write_bytes(b"{}")
    monkeypatch.setattr(pc, "scan_inputs_component", lambda: pc.scan_inputs(
        handbook_dump=dump, sets_index=registry, legacy_list=tmp_path / "none.json", public_sets={}))
    before = h()
    assert pc.pipeline_config("unused", top_k=6)["scan_inputs"]["handbook_dump"] == pc.sha256_hex(b"one")
    assert tuple(pc.pipeline_config("unused", top_k=6)) == pc.PIPELINE_CONFIG_KEYS and "scan_inputs" in pc.PIPELINE_CONFIG_KEYS
    dump.write_bytes(b"two!")
    assert h() != before  # the handbook text that exempts sealed windows changed
    dump.write_bytes(b"one")
    assert h() == before
    dump.unlink()
    assert h() != before  # absent is recorded as absent
    dump.write_bytes(b"one")
    registry.write_bytes(b'{"sets": []}')
    assert h() != before  # the registry of public sets changed


def test_the_suite_never_reads_the_real_scan_inputs_through_the_pipeline_configuration(with_index):
    assert pc.pipeline_config("unused", top_k=6)["scan_inputs"] == {
        "handbook_dump": None, "public_registry": None, "legacy_public": None, "public_sets": {}}


def test_the_scan_leaks_and_sp_launch_scripts_are_hashed_and_the_one_inert_script_import_is_registered():
    assert pc.HASHED_SCRIPTS == ("scan_leaks.py", "sp_launch.py") and pc.hashed_scripts() == [
        "scripts/scan_leaks.py", "scripts/sp_launch.py"]
    assert pc.HASHED_SCRIPT_IMPORTS_INERT == ("sp_launch.py:sealed_tool.py",)
    assert pc.excluded_import_violations() == []
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert prereg["incumbent"]["code_scripts"] == list(pc.HASHED_SCRIPTS)
    assert prereg["incumbent"]["code_scripts_inert_imports"] == list(pc.HASHED_SCRIPT_IMPORTS_INERT)


def test_the_inert_script_import_is_one_the_run_path_never_reaches():
    """``sp_launch.check_prereg_valid`` imports ``sealed_tool`` (the launcher's prereg-check before a data dispatch). The slot
    gate's prereg binding (``sealed_run.bind_prereg``) calls ``sp_launch.check_prereg`` only, and nothing hashed calls the other."""
    launcher = ast.parse((pc.SCRIPTS_DIR / "sp_launch.py").read_text(encoding="utf-8"))
    importers = [n.name for n in ast.walk(launcher) if isinstance(n, ast.FunctionDef) and "sealed_tool" in {
        s for s in pc._script_imports_of(n)}]
    assert importers == ["check_prereg_valid"]
    check_prereg = next(n for n in ast.walk(launcher) if isinstance(n, ast.FunctionDef) and n.name == "check_prereg")
    called = {c.func.id for c in ast.walk(check_prereg) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "check_prereg_valid" not in called and "_check_prereg_is_last_commit" in called
    assert pc._script_imports_of(check_prereg) == []
    for name in pc.code_modules():  # (no code reference: a comment may name it)
        tree = ast.parse((pc.CODE_DIR / name).read_text(encoding="utf-8"))
        assert not any(
            (isinstance(n, ast.Name) and n.id == "check_prereg_valid") or (isinstance(n, ast.Attribute) and n.attr == "check_prereg_valid")
            for n in ast.walk(tree)), name


@pytest.mark.parametrize("line,expected", [
    ("from scripts import sealed_tool", "evaluator.py -> scripts/sealed_tool.py"),
    ("from scripts.sealed_tool import cmd_exclude", "evaluator.py -> scripts/sealed_tool.py"),
    ("import scripts.sealed_tool", "evaluator.py -> scripts/sealed_tool.py"),
    ("import importlib; importlib.import_module('scripts.controls')", "evaluator.py -> scripts/controls.py"),
    ("def f():\n    from scripts import bakeoff_report", "evaluator.py -> scripts/bakeoff_report.py"),
])
def test_a_hashed_module_that_imports_an_unhashed_script_is_reported(code_copy, line, expected):
    assert pc.excluded_import_violations() == []
    original = append(code_copy, "evaluator.py", line)
    assert expected in pc.excluded_import_violations()
    (code_copy / "evaluator.py").write_text(original, encoding="utf-8")
    assert pc.excluded_import_violations() == []


def test_the_hashed_scripts_themselves_may_import_only_hashed_scripts_and_the_registered_inert_one(code_copy, scripts_copy):
    assert pc.excluded_import_violations() == []
    append(code_copy, "evaluator.py", "from scripts import scan_leaks, sp_launch")  # hashed scripts are fine
    assert pc.excluded_import_violations() == []
    with open(scripts_copy / "scan_leaks.py", "a", encoding="utf-8") as fh:
        fh.write("\nfrom scripts import sealed_tool\n")  # scan_leaks has no allowance for it
    with open(scripts_copy / "sp_launch.py", "a", encoding="utf-8") as fh:
        fh.write("\nfrom scripts import sealed_tool\nfrom scripts import controls\n")  # sp_launch has one: sealed_tool, not controls
    assert pc.excluded_import_violations() == [
        "scripts/scan_leaks.py -> scripts/sealed_tool.py", "scripts/sp_launch.py -> scripts/controls.py"]


def test_the_hashed_script_sp_launch_is_part_of_the_code_component_and_an_edit_moves_the_hash(with_index, scripts_copy):
    config = pc.pipeline_config("unused", top_k=6)
    assert config["code"]["scripts/sp_launch.py"] == pc.code_digest(scripts_copy / "sp_launch.py")
    before = h()
    path = scripts_copy / "sp_launch.py"
    original = path.read_text(encoding="utf-8")
    path.write_text("# a comment\n" + original, encoding="utf-8")
    assert h() == before
    path.write_text(original + "\n_R7_MARK = 1\n", encoding="utf-8")
    assert h() != before


def test_the_ledger_docstring_names_the_command_that_prints_the_counts():
    text = (pc.CODE_DIR / "sealed_ledger.py").read_text(encoding="utf-8")
    assert "``sealed_tool draws`` prints the counts" in text and "sealed_tool audit" not in text
