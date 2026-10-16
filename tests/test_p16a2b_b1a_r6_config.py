"""16A-2b b1a closure round r6, part A: the incumbent's, the judge's and the scorer's identities.

C1 the exclusion list is the inert modules and a test (not a name grep) holds that nothing hashed depends on them; scan_leaks is
hashed; the recipe is version 3. C2 client construction lives in a hashed, judge-bound module. C3 the scorer identity is computed
from the scoring code and data. C4 the judge's identity covers the whole preparation path. C6 the small items.

Zero calls: the model loaders and the network are refused (``with_index``), and every source or data edit is made in a private
copy that ``pipeline_config.CODE_DIR``, ``pipeline_config.SCRIPTS_DIR`` or ``scoring_identity.DATA_ROOT`` is pointed at.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import shutil
from pathlib import Path

import pytest
from langchain_core.documents import Document

from src import (
    embedder, evaluator, generator, grounding, judge, judge_prep, llm_clients, pipeline_config as pc, retriever, sealed_report,
    sealed_results, scoring_identity as si, spend,
)
from tests.test_p16a2b_b1a_r4_config import (  # noqa: F401  (fixtures and helpers)
    REAL_INDEX_IDENTITY,
    ROOT,
    _store,
    code_copy,
    h,
    with_index,
)


def edit(copy: Path, name: str, old: str, new: str) -> str:
    """Replace ``old`` by ``new`` once in the copy of ``name`` (it must be there); returns the original text."""
    path = copy / name
    original = path.read_text(encoding="utf-8")
    assert old in original, (name, old)
    path.write_text(original.replace(old, new, 1), encoding="utf-8")
    return original


def append(copy: Path, name: str, text: str) -> str:
    path = copy / name
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n" + text + "\n", encoding="utf-8")
    return original


@pytest.fixture
def scripts_copy(tmp_path_factory, monkeypatch):
    """A private copy of the hashed scripts that ``pipeline_config.SCRIPTS_DIR`` is pointed at."""
    target = tmp_path_factory.mktemp("scripts_copy")
    for name in pc.HASHED_SCRIPTS:
        shutil.copyfile(pc.SCRIPTS_DIR / name, target / name)
    monkeypatch.setattr(pc, "SCRIPTS_DIR", target)
    return target


@pytest.fixture
def data_copy(tmp_path_factory, monkeypatch):
    """A private copy of the scoring data files that ``scoring_identity.DATA_ROOT`` is pointed at."""
    target = tmp_path_factory.mktemp("data_copy")
    for relative in si.SCORING_DATA_FILES:
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(si.DATA_ROOT / relative, target / relative)
    monkeypatch.setattr(si, "DATA_ROOT", target)
    return target


# --- C1: the exclusion list holds only inert modules --------------------------------------------------------------------------------
def test_the_exclusion_list_is_the_inert_modules_and_everything_that_reaches_a_client_a_row_a_rank_or_a_verdict_is_hashed():
    assert pc.CODE_EXCLUDED == (
        "__init__.py", "audit.py", "eval_roster.py", "eval_split.py", "llm_transport.py", "render.py", "sealed_inputs.py",
        "tracing_guard.py")
    on_disk = {p.name for p in pc.CODE_DIR.glob("*.py")}
    assert set(pc.CODE_EXCLUDED) <= on_disk and list(pc.CODE_EXCLUDED) == sorted(pc.CODE_EXCLUDED)
    for name in ("spend.py", "sealed_run.py", "sealed_ledger.py", "eval_privacy.py", "eval_sets.py", "eval_sealed_read.py",
                 "llm_clients.py", "judge_prep.py", "scoring_identity.py"):
        assert name in pc.code_modules(), name  # r5 excluded some of these; r6 hashes them
    assert pc.PIPELINE_CONFIG_VERSION == 3 and pc.HASHED_SCRIPTS == ("scan_leaks.py",)


def test_nothing_hashed_imports_an_excluded_module_except_the_registered_inert_names():
    assert pc.excluded_import_violations() == []
    # the allow-list is not stale: every registered name is really imported by something hashed, and names an excluded module
    used = set()
    for name in [*pc.code_modules()]:
        for stem, imported in pc._imports_of(pc._strip_docstrings(ast.parse((pc.CODE_DIR / name).read_text(encoding="utf-8")))):
            if f"{stem}.py" in pc.CODE_EXCLUDED:
                used.add(f"{stem}.py:{imported}")
    assert used == set(pc.CODE_EXCLUDED_IMPORTS)
    assert all(entry.split(":")[0] in pc.CODE_EXCLUDED for entry in pc.CODE_EXCLUDED_IMPORTS)
    # ... and each registered name is defined in (or imported by) its module
    for entry in pc.CODE_EXCLUDED_IMPORTS:
        module, name = entry.split(":")
        assert hasattr(__import__(f"src.{module[:-3]}", fromlist=[name]), name), entry


@pytest.mark.parametrize("line,expected", [
    ("from src.eval_roster import ROSTER", "evaluator.py -> eval_roster.py:ROSTER"),
    ("from src import render", "evaluator.py -> render.py:<module>"),
    ("import src.audit", "evaluator.py -> audit.py:<module>"),
    ("import importlib; importlib.import_module('src.eval_split')", "evaluator.py -> eval_split.py:<dynamic>"),
    ("from src.render import uncited_statements", "evaluator.py -> render.py:uncited_statements"),  # not a registered name
    ("from .tracing_guard import TracingRefused, brand_new_name", "evaluator.py -> tracing_guard.py:brand_new_name"),
    ("from src.render import *", "evaluator.py -> render.py:*"),
    ("from src.sealed_inputs import check_pairing", "evaluator.py -> sealed_inputs.py:check_pairing"),
])
def test_a_hashed_module_that_depends_on_an_excluded_one_beyond_the_registered_names_is_reported(code_copy, line, expected):
    assert pc.excluded_import_violations() == []
    original = append(code_copy, "evaluator.py", line)
    assert expected in pc.excluded_import_violations()
    (code_copy / "evaluator.py").write_text(original, encoding="utf-8")
    assert pc.excluded_import_violations() == []


def test_the_registered_names_themselves_do_not_trip_the_scan_and_a_docstring_naming_a_module_is_not_an_import(code_copy):
    append(code_copy, "evaluator.py", "from src.render import RenderFlags, render\nfrom src.audit import build_event, log_event")
    assert pc.excluded_import_violations() == []
    path = code_copy / "retriever.py"
    path.write_text(path.read_text(encoding="utf-8").replace('"""', '"""See src.eval_roster and src.render. ', 1), encoding="utf-8")
    assert pc.excluded_import_violations() == []


@pytest.mark.parametrize("name", ["spend.py", "sealed_run.py", "sealed_ledger.py", "eval_privacy.py", "eval_sets.py",
                                  "eval_sealed_read.py", "llm_clients.py", "judge_prep.py"])
def test_editing_a_module_the_exclusion_list_used_to_hold_changes_the_hash(with_index, code_copy, name):
    before = h()
    path = code_copy / name
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n_R6_MARK = 1\n", encoding="utf-8")
    assert h() != before
    path.write_text(original, encoding="utf-8")
    assert h() == before


# --- C1: scan_leaks is hashed ---------------------------------------------------------------------------------------------------------
def test_scan_leaks_is_part_of_the_code_component_keyed_by_its_script_path(with_index, scripts_copy):
    config = pc.pipeline_config("unused", top_k=6)
    assert "scripts/scan_leaks.py" in config["code"] and set(config["code"]) == {*pc.code_modules(), "scripts/scan_leaks.py"}
    assert config["code"]["scripts/scan_leaks.py"] == pc.code_digest(scripts_copy / "scan_leaks.py")
    before = h()
    path = scripts_copy / "scan_leaks.py"
    original = path.read_text(encoding="utf-8")
    path.write_text("# a comment\n" + original, encoding="utf-8")
    assert h() == before  # a comment is not code
    path.write_text(original + "\n_R6_MARK = 1\n", encoding="utf-8")
    assert h() != before
    path.write_text(original, encoding="utf-8")
    assert h() == before


def test_a_hashed_script_that_imports_an_excluded_module_is_reported(scripts_copy):
    assert pc.excluded_import_violations() == []
    with open(scripts_copy / "scan_leaks.py", "a", encoding="utf-8") as fh:
        fh.write("\nfrom src.eval_roster import ROSTER\n")
    assert pc.excluded_import_violations() == ["scripts/scan_leaks.py -> eval_roster.py:ROSTER"]


def test_the_recipe_is_version_three_and_has_the_scoring_inputs_component(with_index):
    config = pc.pipeline_config("unused", top_k=6)
    assert config["version"] == pc.PIPELINE_CONFIG_VERSION == 3 and tuple(config) == pc.PIPELINE_CONFIG_KEYS
    assert pc.PIPELINE_CONFIG_KEYS[-1] == "scoring_inputs" and config["scoring_inputs"] == si.scoring_inputs()


# --- C2: client construction ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["generation", "judge", "rewrite"])
def test_the_metered_client_is_what_default_inner_factory_built_before_r6(monkeypatch, kind):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    from src import query_rewrite

    base = query_rewrite.get_rewrite_llm() if kind == "rewrite" else generator.get_llm()
    old = base.model_copy(update={"max_retries": 0})  # the body of the r5 spend.default_inner_factory, verbatim
    for cached in ("_client_params", "_client", "_async_client"):
        old.__dict__.pop(cached, None)
    new = spend.default_inner_factory(kind)
    assert new.model_dump() == old.model_dump() and type(new) is type(old)
    assert (new.model, new.max_retries, new.max_tokens) == (old.model, 0, old.max_tokens)
    assert new.thinking == old.thinking and new.default_request_timeout == old.default_request_timeout
    assert llm_clients.build_metered_inner(kind).model_dump() == new.model_dump()


def test_spend_delegates_the_client_shaping_and_keeps_only_the_metering(monkeypatch):
    seen = []
    monkeypatch.setattr(llm_clients, "build_metered_inner", lambda kind: seen.append(kind) or "client")
    assert spend.default_inner_factory("judge") == "client" and seen == ["judge"]
    text = (pc.CODE_DIR / "spend.py").read_text(encoding="utf-8")
    assert "model_copy" not in text and "_client_params" not in text  # the shaping is not here any more
    with pytest.raises(ValueError):
        llm_clients.production_client("nonsense")


def test_the_client_module_is_hashed_with_the_pipeline_and_with_the_judge(with_index, code_copy):
    assert "llm_clients.py" in pc.code_modules() and "llm_clients.py" in judge.JUDGE_CODE_MODULES
    pipeline_before, judge_before = h(), judge.judge_identity()["config_hash"]
    original = edit(code_copy, "llm_clients.py", 'METERED_CLIENT_UPDATE: Dict[str, Any] = {"max_retries": 0}',
                    'METERED_CLIENT_UPDATE: Dict[str, Any] = {"max_retries": 0, "max_tokens": 9}')
    assert h() != pipeline_before and judge.judge_identity()["config_hash"] != judge_before
    (code_copy / "llm_clients.py").write_text(original, encoding="utf-8")
    spend_text = append(code_copy, "spend.py", "_R6_MARK = 1")  # the metering is hashed with the pipeline, not with the judge
    assert h() != pipeline_before and judge.judge_identity()["config_hash"] == judge_before
    (code_copy / "spend.py").write_text(spend_text, encoding="utf-8")


# --- C3: the scorer identity ---------------------------------------------------------------------------------------------------------
def test_the_scorer_identity_is_a_deterministic_digest_of_the_scoring_code_functions_values_and_data():
    value = si.scorer_identity()
    assert value == si.scorer_identity() and len(value) == 64 and set(value) <= set("0123456789abcdef")
    assert si.SCORER_IDENTITY_NAME == "scorer_identity_v1"
    for name in si.SCORING_CODE_MODULES:
        assert (pc.CODE_DIR / name).is_file() and name in pc.code_modules(), name
    assert "sealed_report.py" not in si.SCORING_CODE_MODULES  # rendering is not scoring
    assert all((si.DATA_ROOT / relative).is_file() for relative in si.SCORING_DATA_FILES)
    assert si.covered()[0] == si.SCORING_CODE_MODULES[0] and "generator.py:_sections_related" in si.covered()


@pytest.mark.parametrize("name", si.SCORING_CODE_MODULES)
def test_editing_any_scoring_module_changes_the_scorer_identity_and_the_pipeline_hash(with_index, code_copy, name):
    before_scorer, before_pipeline = si.scorer_identity(), h()
    path = code_copy / name
    original = path.read_text(encoding="utf-8")
    path.write_text("# a comment\n" + original, encoding="utf-8")
    assert si.scorer_identity() == before_scorer  # a comment is not code
    path.write_text(original + "\n_R6_MARK = 1\n", encoding="utf-8")
    assert si.scorer_identity() != before_scorer and h() != before_pipeline
    path.write_text(original, encoding="utf-8")
    assert si.scorer_identity() == before_scorer and h() == before_pipeline


def test_the_scorer_identity_ignores_the_rendering_and_generation_code_it_does_not_score_with(with_index, code_copy):
    before = si.scorer_identity()
    for name in ("sealed_report.py", "render.py", "retriever.py", "evaluator.py", "spend.py"):
        append(code_copy, name, "_R6_MARK = 1")
    assert si.scorer_identity() == before


def test_a_shared_scoring_function_and_the_literals_the_scorer_reads_are_bound_without_hashing_the_whole_generator(code_copy, monkeypatch):
    before = si.scorer_identity()
    append(code_copy, "generator.py", "_R6_MARK = 1")  # unrelated generation code
    assert si.scorer_identity() == before
    original = edit(code_copy, "generator.py", "    return a[:n] == b[:n]\n", "    return a[:n] == b[:n] and n > 1\n")
    assert si.scorer_identity() != before  # the related-match rule changed
    (code_copy / "generator.py").write_text(original, encoding="utf-8")
    assert si.scorer_identity() == before
    monkeypatch.setattr(generator, "CAVEAT_PREFIX", "another opener")
    changed = si.scorer_identity()
    assert changed != before
    monkeypatch.setattr(grounding, "STATUS_COMPLETE", "done")
    assert si.scorer_identity() != changed


@pytest.mark.parametrize("relative", si.SCORING_DATA_FILES)
def test_changing_adding_or_removing_a_scoring_data_file_changes_the_scorer_identity_and_the_pipeline_hash(with_index, data_copy, relative):
    before_scorer, before_pipeline = si.scorer_identity(), h()
    path = data_copy / relative
    original = path.read_bytes()
    path.write_bytes(original + b"\n")  # a different byte is a different map
    assert si.scorer_identity() != before_scorer and h() != before_pipeline
    assert pc.pipeline_config("unused", top_k=6)["scoring_inputs"][relative] == hashlib.sha256(original + b"\n").hexdigest()
    path.unlink()  # removed: the file is absent, which is recorded as such
    assert si.scoring_inputs()[relative] is None
    assert si.scorer_identity() != before_scorer and h() != before_pipeline
    path.write_bytes(original)
    assert si.scorer_identity() == before_scorer and h() == before_pipeline


def test_the_registered_data_files_are_the_ones_the_scoring_path_reads():
    assert evaluator.ABSORBED_MAP_PATH.replace("\\", "/") in si.SCORING_DATA_FILES
    from src import eval_schema

    assert eval_schema.INVENTORY_PATH.relative_to(si.DATA_ROOT).as_posix() in si.SCORING_DATA_FILES


def test_the_metric_computations_live_in_the_scored_module_and_the_renderer_only_imports_them():
    moved = ("family_hits", "confirmatory_stats", "eligible_families", "dropped_families", "kept_rows", "sealed_set",
             "check_cutoff", "is_confirmatory", "stored_shapes", "row_scope_correct", "stratum_numbers", "baseline_hits_kept")
    report_tree = ast.parse(Path(sealed_report.__file__).read_text(encoding="utf-8"))
    defined_in_report = {n.name for n in report_tree.body if isinstance(n, ast.FunctionDef)}
    results_tree = ast.parse(Path(sealed_results.__file__).read_text(encoding="utf-8"))
    defined_in_results = {n.name for n in results_tree.body if isinstance(n, ast.FunctionDef)}
    for name in moved:
        assert name in defined_in_results and name not in defined_in_report, name
        assert getattr(sealed_report, name) is getattr(sealed_results, name), name  # the old import path still works
    assert "sealed_results.py" in si.SCORING_CODE_MODULES and "sealed_report.py" not in si.SCORING_CODE_MODULES
    assert sealed_report.RESULTS_KIND == sealed_results.RESULTS_KIND and sealed_report.DESCRIPTIVE_SLOTS == ("0", "0b")
    imports = {n.module for n in ast.walk(results_tree) if isinstance(n, ast.ImportFrom)}
    assert "src.sealed_report" not in imports  # no cycle: the scored module does not import its renderer


def test_the_sidecar_carries_the_scorer_identity_and_a_given_one_wins():
    from src.eval_cohort import SCORER_VERSION, build_sidecar

    kwargs = dict(privacy="private", cohorts=[], rows=[], expansion={"kind": "none"})
    doc = build_sidecar(**kwargs)
    assert doc["scorer_identity"] == si.scorer_identity() and doc["scorer_version"] == SCORER_VERSION
    assert build_sidecar(**kwargs, scorer_identity="a" * 64)["scorer_identity"] == "a" * 64


# --- C4: the judge's preparation path --------------------------------------------------------------------------------------------------
def test_the_judge_identity_covers_the_preparation_path_and_the_client_construction():
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert judge.JUDGE_CODE_MODULES == ("judge.py", "judge_prep.py", "llm_clients.py", "eval_v6.py", "eval_stats.py")
    assert list(judge.JUDGE_COVERS) == prereg["judge"]["covers"] == [
        "model", "client", "prompt", "verdicts", "sample", "parsing", "eligibility", "scoring", "endpoint",
        "preparation", "client_construction"]
    assert all((pc.CODE_DIR / name).is_file() and name in pc.code_modules() for name in judge.JUDGE_CODE_MODULES)
    # eval_v6 and eval_stats stay because they compute the unsupported rate (judge_summary) and its Wilson interval
    from src import eval_v6

    assert hasattr(eval_v6, "judge_summary")


@pytest.mark.parametrize("name,old,new", [
    ("judge_prep.py", 'normalized = normalized.rstrip(".")', 'normalized = normalized.rstrip("")'),  # the refusal test
    ("judge_prep.py", "normalized.casefold() == REFUSAL_PHRASE.casefold()", "normalized == REFUSAL_PHRASE"),
    ("judge_prep.py", "status = STATUS_INCOMPLETE", "status = STATUS_COMPLETE"),  # the status normalisation
    ("judge_prep.py", 'STATUS_COMPLETE = "complete"', 'STATUS_COMPLETE = "done"'),  # the vocabulary
    ("judge_prep.py", "if status != STATUS_COMPLETE:", "if status == 'never':"),  # the eligibility rule
    ("judge_prep.py", 'return "No relevant documents found."', 'return "Nothing."'),  # the judge's context formatter
    ("judge_prep.py", '"\\n\\n".join(context_parts)', '"\\n".join(context_parts)'),
    ("judge_prep.py", 'f"para {secnum}"', 'f"para. {secnum}"'),
    ("judge_prep.py", '"score": 0.0', '"score": 1.0'),  # the item assembly
    ("judge.py", "(JUDGE_PROMPT | client | StrOutputParser()).invoke", "(JUDGE_PROMPT | client).invoke"),
    ("llm_clients.py", "CACHED_SDK_ATTRIBUTES = (", "CACHED_SDK_ATTRIBUTES = ('x', "),
])
def test_editing_any_part_of_the_judge_preparation_path_changes_the_judge_identity(code_copy, name, old, new):
    before = judge.judge_identity()["config_hash"]
    original = (code_copy / name).read_text(encoding="utf-8")
    assert old in original, (name, old)
    (code_copy / name).write_text(original.replace(old, new, 1), encoding="utf-8")
    assert judge.judge_identity()["config_hash"] != before
    (code_copy / name).write_text(original, encoding="utf-8")
    assert judge.judge_identity()["config_hash"] == before


def test_the_judge_keeps_its_own_context_formatter_so_a_generation_context_change_does_not_move_its_identity(with_index, code_copy):
    judge_before, pipeline_before = judge.judge_identity()["config_hash"], h()
    original = edit(code_copy, "retriever.py", "def format_context(", "def format_context_changed(")
    assert judge.judge_identity()["config_hash"] == judge_before  # intended: a candidate may change what generation sees
    assert h() != pipeline_before
    (code_copy / "retriever.py").write_text(original, encoding="utf-8")
    edit(code_copy, "grounding.py", 'STATUS_COMPLETE = "complete"', 'STATUS_COMPLETE = "done"')  # a candidate's vocabulary
    assert judge.judge_identity()["config_hash"] == judge_before


def test_the_refusal_phrase_the_judge_compares_with_is_part_of_its_identity(monkeypatch):
    before = judge.judge_identity()["config_hash"]
    monkeypatch.setattr(generator, "REFUSAL_PHRASE", "another phrase")
    assert judge.judge_identity()["config_hash"] != before
    assert judge_prep.is_refusal("another phrase.") and not judge_prep.is_refusal("not covered in the source material")


def _handbook_chunk(section, start=None, end=None, text="The priority entry protects a purchaser."):
    meta = {"document_type": "handbook", "section_number": section, "source": "h.pdf", "title": "Handbook"}
    if start is not None:
        meta["page_start"] = start
    if end is not None:
        meta["page_end"] = end
    return {"document": Document(page_content=text, metadata=meta), "score": 0.5, "metadata": meta}


FIXTURES = {
    "empty": [],
    "paragraph": [_handbook_chunk("14.8.5", 412, 412)],
    "page range": [_handbook_chunk("3.2.1", 87, 88), _handbook_chunk("3.2.2", 89, 89, "Second.")],
    "appendix": [_handbook_chunk("APPENDIX 14.1", 566, 566)],
    "no section": [_handbook_chunk("", 12, 12)],
    "no pages": [_handbook_chunk("5.1")],
    "legislation": [{"document": Document(page_content="Section text.", metadata={
        "document_type": "legislation", "title": "Act", "source": "act.pdf", "section_number": "77"}), "score": 0.1, "metadata": {}},
        {"document": Document(page_content="Bare.", metadata={"document_type": "legislation"}), "score": 0.1, "metadata": {}}],
}


@pytest.mark.parametrize("case", sorted(FIXTURES))
def test_the_judge_context_is_today_byte_for_byte_what_generation_is_shown(case):
    assert judge_prep.format_judge_context(FIXTURES[case]) == retriever.format_context(FIXTURES[case])


@pytest.mark.parametrize("status,eligible", [
    ("complete", True), ("truncated", False), ("declined", False), ("incomplete", False),
    ("unknown", False), ("error", False), ("not_run", False), ("anything else", False), (None, False),
])
def test_only_a_complete_answer_is_judgeable(status, eligible):
    result = {"answer": "An answer. [Handbook, para 1.1, p.1]"}
    assert judge_prep.is_judgeable("answer", result, status) is eligible
    assert judge.is_judgeable is judge_prep.is_judgeable  # one definition, still importable from the judge


def test_the_rest_of_the_eligibility_rule_is_unchanged():
    ok = {"answer": "An answer."}
    assert judge_prep.is_judgeable("partial", ok, "complete") and not judge_prep.is_judgeable("refuse", ok, "complete")
    assert not judge_prep.is_judgeable("answer", None, "error")
    assert not judge_prep.is_judgeable("answer", {"answer": f"{generator.REFUSAL_PHRASE}."}, "complete")
    assert judge_prep.is_judgeable("answer", {"answer": f"{generator.REFUSAL_PHRASE}, but the likely answer is 20 days."}, "complete")


def test_the_status_literals_are_the_groundings_today_and_there_is_one_definition_of_each_helper():
    assert (judge_prep.STATUS_COMPLETE, judge_prep.STATUS_TRUNCATED, judge_prep.STATUS_DECLINED, judge_prep.STATUS_INCOMPLETE,
            judge_prep.STATUS_UNKNOWN, judge_prep.STATUS_ERROR) == (
        grounding.STATUS_COMPLETE, grounding.STATUS_TRUNCATED, grounding.STATUS_DECLINED, grounding.STATUS_INCOMPLETE,
        grounding.STATUS_UNKNOWN, grounding.STATUS_ERROR)
    assert judge_prep.INCOMPLETE_STATUSES == grounding.INCOMPLETE_STATUSES
    assert generator.is_refusal is judge_prep.is_refusal and evaluator._normalise_answer is judge_prep.normalise_answer
    assert inspect.getsourcefile(generator.is_refusal).endswith("judge_prep.py")
    assert "def is_refusal" not in (pc.CODE_DIR / "generator.py").read_text(encoding="utf-8")
    assert "def _normalise_answer" not in (pc.CODE_DIR / "evaluator.py").read_text(encoding="utf-8")


def test_judge_items_are_assembled_from_the_cache_by_the_one_eligibility_rule_and_the_judges_own_context():
    rows = [
        {"id": "a", "question": "qa", "scope": "answer"}, {"id": "b", "question": "qb", "scope": "partial"},
        {"id": "c", "question": "qc", "scope": "refuse"}, {"id": "d", "question": "qd", "scope": "answer"},
        {"id": "e", "question": "qe", "scope": "answer"}, {"id": "f", "question": "qf", "scope": "answer"},
        {"id": "g", "question": "qg", "scope": "answer"},
    ]
    docs = [c["document"] for c in FIXTURES["page range"]]
    complete = {"answer": "Yes. [Handbook, para 3.2.1, p.87]", "generation_status": "complete", "source_documents": docs}
    cache = {
        "qa": {"result": complete},
        "qb": {"result": {**complete, "answer": "Part."}},
        "qc": {"result": complete},  # a refuse row is never judged
        "qd": {"result": {**complete, "generation_status": "unknown"}},  # r6: unknown is not judged
        "qe": {"result": None},
        "qf": {"result": {**complete, "answer": generator.REFUSAL_PHRASE}},
        # qg missing from the cache
    }
    items = judge_prep.assemble_judge_items(rows, cache)
    assert [i["question"] for i in items] == ["qa", "qb"]
    assert items[0] == {"question": "qa", "answer": complete["answer"], "context": judge_prep.format_judge_context(
        [{"document": d, "score": 0.0, "metadata": d.metadata} for d in docs])}


def test_judge_v6_hands_the_judge_what_judge_prep_assembled(monkeypatch):
    seen = {}
    monkeypatch.setattr(judge_prep, "assemble_judge_items", lambda rows, cache: seen.update(rows=rows, cache=cache) or [
        {"question": "q1", "answer": "a", "context": "c"}])
    monkeypatch.setattr("src.judge.judge_answers", lambda items, llm_fn=None, sample_n=None: seen.update(items=items) or {"per_item": []})
    records = []
    block, by_id = evaluator._judge_v6("set", [{"id": "r1", "question": "q1", "scope": "answer"}], {"q1": {}},
                                       judge_fn=None, judge_sample=None, records_out=records)
    assert seen["items"] == [{"question": "q1", "answer": "a", "context": "c"}] and by_id == {}


def test_llm_fn_over_builds_the_prompt_client_parser_chain():
    from langchain_core.runnables import RunnableLambda

    got = {}

    def client(prompt_value):
        got["text"] = prompt_value.to_string()
        return "{\"claims\": []}"

    fn = judge.llm_fn_over(RunnableLambda(client))
    assert fn({"question": "Q?", "answer": "A.", "context": "C."}) == "{\"claims\": []}"
    assert "Q?" in got["text"] and "A." in got["text"] and "C." in got["text"]


# --- C6: the small items ------------------------------------------------------------------------------------------------------------
def test_sealed_results_imports_the_typing_names_its_annotations_use():
    namespace = vars(sealed_results)
    assert "List" in namespace and "Optional" in namespace
    import typing

    for name in ("row_entry", "failed_guards", "stored_shapes", "row_scope_correct"):
        typing.get_type_hints(getattr(sealed_results, name), vars(sealed_results))  # every annotation resolves


def test_the_endpoint_is_read_after_load_dotenv_without_overriding_the_environment(monkeypatch):
    calls = []

    def fake_load_dotenv(*args, **kwargs):
        calls.append(kwargs)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://dotenv.example")
        return True

    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(pc, "load_dotenv", fake_load_dotenv)
    assert pc.endpoint_identity() == {"anthropic_base_url": "https://dotenv.example"}
    assert len(calls) == 1 and not calls[0].get("override", False)  # never override what the process already has


def test_the_real_load_dotenv_does_not_override_a_variable_that_is_already_set(monkeypatch, tmp_path):
    from dotenv import load_dotenv

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_BASE_URL=https://from-the-file.example\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://already-set.example")
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    monkeypatch.setattr(pc, "load_dotenv", lambda *a, **k: load_dotenv(env, *a, **k))
    assert pc.endpoint_identity() == {"anthropic_base_url": "https://already-set.example"}


def test_the_index_identity_and_hash_of_a_store_already_open_in_this_process_through_langchain_chroma(tmp_path, monkeypatch):
    """Auditor m12: the production writer holds the store open; the identity is computed beside it, without a model."""
    from langchain_chroma import Chroma

    from tests.test_embedder import FakeEmbeddings

    store = Chroma(collection_name=embedder.COLLECTION_NAME, embedding_function=FakeEmbeddings(),
                   persist_directory=str(tmp_path / "open"))
    store.add_documents([Document(page_content="alpha", metadata={"k": 1}), Document(page_content="beta", metadata={"k": 2})],
                        ids=["id-1", "id-2"])
    monkeypatch.setattr(pc, "index_identity", REAL_INDEX_IDENTITY)
    monkeypatch.setattr(embedder, "get_embedding_function", lambda: (_ for _ in ()).throw(AssertionError("embedding")))
    first = REAL_INDEX_IDENTITY(str(tmp_path / "open"))  # the handle `store` is still alive here
    assert first is not None and first == REAL_INDEX_IDENTITY(str(tmp_path / "open"))
    whole = pc.pipeline_config_hash(str(tmp_path / "open"), top_k=6)
    assert whole is not None and whole == pc.pipeline_config_hash(str(tmp_path / "open"), top_k=6)
    store.add_documents([Document(page_content="gamma", metadata={"k": 3})], ids=["id-3"])  # written through the open handle
    assert REAL_INDEX_IDENTITY(str(tmp_path / "open")) != first
    assert pc.pipeline_config_hash(str(tmp_path / "open"), top_k=6) != whole
    del store


def test_the_prereg_no_longer_lists_the_judge_among_the_excluded_things():
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert "judge" not in prereg["incumbent"]["excluded"]
    assert prereg["incumbent"]["code_excluded"] == list(pc.CODE_EXCLUDED)
    assert prereg["incumbent"]["code_excluded_imports"] == list(pc.CODE_EXCLUDED_IMPORTS)
    assert prereg["incumbent"]["code_scripts"] == list(pc.HASHED_SCRIPTS)
    assert prereg["incumbent"]["config_fn"] == "pipeline_config_v3"
    assert prereg["adoption"]["scoring"] == {
        "identity": "scorer_identity_v1", "compare": "candidate_and_slot0_equal", "covers": si.covered()}
