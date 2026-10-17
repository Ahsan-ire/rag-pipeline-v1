"""16A-2b b1a closure round r5, part A: the incumbent's and the judge's identities (R1 whole-src code, R2 runtime, endpoint and
index search configuration, R3 transport literals, R4 judge parsing, eligibility and scoring).

Zero calls: the model loaders and the network are refused (``with_index``), the Chroma tests use a temporary store written
with explicit vectors, and every source edit is made in a private copy of ``src`` that ``pipeline_config.CODE_DIR`` is pointed at.
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from src import embedder, generator, judge, llm_transport, pipeline_config as pc, query_rewrite
from tests.test_p16a2b_b1a_r4_config import (  # noqa: F401  (fixtures and helpers)
    REAL_INDEX_IDENTITY,
    ROOT,
    _dist,
    _real_versions,
    _store,
    code_copy,
    h,
    with_index,
)

#: modules that shape what is retrieved, generated, judged or scored and so must stay hashed (the brief's list)
MUST_BE_HASHED = (
    "pipeline.py", "evaluator.py", "expansion_artifact.py", "judge.py", "pipeline_config.py", "eval_stats.py", "eval_v6.py",
    "eval_scoring.py", "eval_cohort.py", "eval_schema.py", "sealed_report.py", "sealed_results.py", "sealed_groups.py",
    "retriever.py", "generator.py", "grounding.py",
)


def edit(copy: Path, name: str, old: str, new: str, count: int = 1) -> str:
    """Replace ``old`` by ``new`` in the copy of ``name`` (it must be there); returns the original text."""
    path = copy / name
    original = path.read_text(encoding="utf-8")
    assert original.count(old) >= count, (name, old)
    path.write_text(original.replace(old, new, count), encoding="utf-8")
    return original


# --- R1: the code component is the whole of src, minus a registered list --------------------------------------------------------
def test_every_src_module_is_hashed_or_registered_as_excluded():
    on_disk = {p.name for p in pc.CODE_DIR.glob("*.py")}
    assert set(pc.code_modules()) | set(pc.CODE_EXCLUDED) == on_disk
    assert not set(pc.code_modules()) & set(pc.CODE_EXCLUDED)
    assert set(pc.CODE_EXCLUDED) <= on_disk  # a typo in the list would be a silent no-op
    assert list(pc.CODE_EXCLUDED) == sorted(pc.CODE_EXCLUDED) and len(set(pc.CODE_EXCLUDED)) == len(pc.CODE_EXCLUDED)
    assert "llm_transport.py" in pc.CODE_EXCLUDED
    for name in MUST_BE_HASHED:
        assert name in pc.code_modules(), name  # code that retrieves, generates, judges or scores is never excluded
    assert set(pc.code_identity()) == {*pc.code_modules(), "scripts/scan_leaks.py", "scripts/sp_launch.py"}  # r6: the hashed scripts are keyed scripts/<file>; r7: + sp_launch


def test_the_registered_exclusions_are_modules_nothing_hashed_depends_on_beyond_inert_names():
    """r6 replaces the r5 name grep (which did not establish the property): for every excluded module, no hashed module
    imports it, except the names registered as inert (``CODE_EXCLUDED_IMPORTS``). The full set of checks is in
    ``tests/test_p16a2b_b1a_r6_config.py``."""
    assert pc.excluded_import_violations() == []


def test_a_new_module_is_hashed_by_default_and_an_excluded_one_is_not(with_index, code_copy):
    before = h()
    (code_copy / "brand_new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert "brand_new_module.py" in pc.code_modules() and h() != before
    (code_copy / "brand_new_module.py").unlink()
    assert h() == before
    # a module in a subpackage is not missed
    (code_copy / "sub").mkdir()
    (code_copy / "sub" / "inner.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert "sub/inner.py" in pc.code_modules() and h() != before


@pytest.mark.parametrize("name", pc.CODE_EXCLUDED)
def test_editing_an_excluded_module_does_not_change_the_hash(with_index, code_copy, name):
    before = h()
    path = code_copy / name
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n_R5_MARK = 1\n", encoding="utf-8")
    assert h() == before
    path.write_text(original, encoding="utf-8")


@pytest.mark.parametrize("name", MUST_BE_HASHED)
def test_editing_a_hashed_module_changes_the_hash(with_index, code_copy, name):
    before = h()
    path = code_copy / name
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n_R5_MARK = 1\n", encoding="utf-8")
    assert h() != before
    path.write_text(original, encoding="utf-8")
    assert h() == before


def test_editing_an_evaluator_adapter_changes_the_hash(with_index, code_copy):
    """Codex 1: the adapters that thread the rewrites and the intent into retrieval and generation are evaluator code."""
    before = h()
    original = (code_copy / "evaluator.py").read_text(encoding="utf-8")
    assert original.count("intent_rewrite=exp.intent_rewrite,") == 2  # the retrieval adapter and the answer adapter
    for occurrence in (1, 2):  # drop the intent argument from each adapter in turn
        text = original
        index = -1
        for _ in range(occurrence):
            index = text.index("intent_rewrite=exp.intent_rewrite,", index + 1)
        (code_copy / "evaluator.py").write_text(
            text[:index] + text[index + len("intent_rewrite=exp.intent_rewrite,"):], encoding="utf-8")
        ast.parse((code_copy / "evaluator.py").read_text(encoding="utf-8"))  # still a program
        assert h() != before, occurrence
    (code_copy / "evaluator.py").write_text(original, encoding="utf-8")
    assert h() == before


def test_editing_the_expansion_artifact_module_changes_the_hash(with_index, code_copy):
    before = h()
    edit(code_copy, "expansion_artifact.py", '"max_rewrites": qr.MAX_REWRITES,', '"max_rewrites": 0,')
    assert h() != before


def test_a_comment_or_docstring_edit_of_any_hashed_module_does_not_change_the_hash(with_index, code_copy):
    before = h()
    path = code_copy / "evaluator.py"
    path.write_text("# a comment\n" + path.read_text(encoding="utf-8").replace('"""', '"""Edited. ', 1), encoding="utf-8")
    assert h() == before


# --- R3: the transport literals are really excluded ------------------------------------------------------------------------------
def test_editing_a_timeout_literal_in_the_transport_module_leaves_the_hash_and_the_judge_identity_unchanged(with_index, code_copy):
    before, judge_before = h(), judge.judge_identity()["config_hash"]
    original = edit(code_copy, "llm_transport.py", "GENERATION_REQUEST_TIMEOUT = 120.0", "GENERATION_REQUEST_TIMEOUT = 5.0")
    edit(code_copy, "llm_transport.py", "REWRITE_REQUEST_TIMEOUT = 60.0", "REWRITE_REQUEST_TIMEOUT = 1.0")
    edit(code_copy, "llm_transport.py", "GENERATION_MAX_RETRIES = 3", "GENERATION_MAX_RETRIES = 9")
    edit(code_copy, "llm_transport.py", "REWRITE_MAX_RETRIES = 3", "REWRITE_MAX_RETRIES = 0")
    assert (code_copy / "llm_transport.py").read_text(encoding="utf-8") != original
    assert h() == before and judge.judge_identity()["config_hash"] == judge_before
    # ... while the same kind of edit in a hashed module (the generator's token cap) does change both
    edit(code_copy, "generator.py", '"max_tokens": 2048,', '"max_tokens": 4096,')
    assert h() != before


def test_the_clients_take_their_timeouts_and_retries_from_the_transport_module_only():
    for module, function, pairs in (
        (generator, "generation_llm_kwargs", {"default_request_timeout": "GENERATION_REQUEST_TIMEOUT", "max_retries": "GENERATION_MAX_RETRIES"}),
        (query_rewrite, "rewrite_llm_kwargs", {"default_request_timeout": "REWRITE_REQUEST_TIMEOUT", "max_retries": "REWRITE_MAX_RETRIES"}),
    ):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
        found = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in pairs:
                        found[key.value] = value
        assert set(found) == set(pairs), function
        for key, value in found.items():  # a Name, not a literal: the literal lives in llm_transport
            assert isinstance(value, ast.Name) and value.id == pairs[key], (function, key)
    assert generator.generation_llm_kwargs()["default_request_timeout"] == llm_transport.GENERATION_REQUEST_TIMEOUT == 120.0
    assert generator.generation_llm_kwargs()["max_retries"] == llm_transport.GENERATION_MAX_RETRIES == 3
    assert query_rewrite.rewrite_llm_kwargs()["default_request_timeout"] == llm_transport.REWRITE_REQUEST_TIMEOUT == 60.0
    assert query_rewrite.rewrite_llm_kwargs()["max_retries"] == llm_transport.REWRITE_MAX_RETRIES == 3


def test_the_transport_module_holds_literals_and_nothing_behavioural():
    tree = ast.parse((pc.CODE_DIR / "llm_transport.py").read_text(encoding="utf-8"))
    body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]  # docstring aside
    assert all(isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) for n in body)
    assert {t.id for n in body for t in n.targets} == {
        "GENERATION_REQUEST_TIMEOUT", "GENERATION_MAX_RETRIES", "REWRITE_REQUEST_TIMEOUT", "REWRITE_MAX_RETRIES"}


# --- R2: every installed distribution --------------------------------------------------------------------------------------------
def test_the_runtime_lists_every_installed_distribution_normalised_sorted_and_deduplicated(monkeypatch):
    fake = [_dist("Zeta_Pkg", "3.0"), _dist("alpha", "1.0"), _dist("Foo.Bar", "2.0"), _dist("foo-bar", "2.0"),  # one package twice
            _dist(None, "9.9"), _dist("noversion", None)]  # nothing to pin
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: fake)
    assert pc.runtime_identity()["packages"] == ["alpha==1.0", "foo-bar==2.0", "zeta-pkg==3.0"]
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: list(reversed(fake)))
    assert pc.runtime_identity()["packages"] == ["alpha==1.0", "foo-bar==2.0", "zeta-pkg==3.0"]  # discovery order is irrelevant


def test_the_real_runtime_includes_the_numerical_and_tokenizer_dependencies():
    names = {p.split("==")[0] for p in pc.runtime_identity()["packages"]}
    for dependency in ("numpy", "torch", "tokenizers", "huggingface-hub", "rank-bm25", "chromadb", "anthropic", "langchain-anthropic"):
        assert dependency in names, dependency
    assert len(pc.runtime_identity()["packages"]) == len(set(pc.runtime_identity()["packages"]))


@pytest.mark.parametrize("name", ["numpy", "torch", "tokenizers", "huggingface-hub", "pip"])
def test_a_changed_version_of_any_installed_distribution_changes_the_hash(with_index, monkeypatch, name):
    before = h()
    versions = _real_versions()
    assert any(pc.normalise_distribution_name(n) == name for n, _ in versions), name
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [
        _dist(n, "99.99" if pc.normalise_distribution_name(n) == name else v) for n, v in versions])
    assert h() != before


def test_an_added_or_removed_distribution_changes_the_hash_and_their_order_does_not(with_index, monkeypatch):
    versions = _real_versions()
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [_dist(n, v) for n, v in versions])
    base = h()
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [_dist(n, v) for n, v in [*versions, ("brand-new-pkg", "1")]])
    assert h() != base
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [_dist(n, v) for n, v in versions[1:]])
    assert h() != base
    shuffled = list(versions)
    random.Random(1).shuffle(shuffled)
    monkeypatch.setattr(pc.importlib.metadata, "distributions", lambda: [_dist(n, v) for n, v in shuffled])
    assert h() == base


# --- R2: the endpoint ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def no_endpoint_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


def test_the_endpoint_is_what_the_installed_client_resolves(no_endpoint_env, monkeypatch):
    """Both ``ANTHROPIC_API_URL`` (first) and ``ANTHROPIC_BASE_URL`` (second) are read by ``ChatAnthropic`` itself; the
    identity must agree with a client built under the same environment, whatever the installed package does."""
    from langchain_anthropic import ChatAnthropic

    def built() -> str:
        return ChatAnthropic(model="claude-sonnet-5", api_key="sk-ant-test-not-real").anthropic_api_url

    assert pc.endpoint_identity() == {"anthropic_base_url": built()} == {"anthropic_base_url": "https://api.anthropic.com"}
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/base")
    assert pc.endpoint_identity() == {"anthropic_base_url": built()} == {"anthropic_base_url": "https://proxy.example/base"}
    monkeypatch.setenv("ANTHROPIC_API_URL", "https://other.example/api")  # the first of the two wins
    assert pc.endpoint_identity() == {"anthropic_base_url": built()} == {"anthropic_base_url": "https://other.example/api"}
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    assert pc.endpoint_identity() == {"anthropic_base_url": "https://other.example/api"}


@pytest.mark.parametrize("variable", ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"])
def test_an_endpoint_change_changes_the_pipeline_hash_and_the_judge_identity(with_index, no_endpoint_env, monkeypatch, variable):
    before, judge_before = h(), judge.judge_identity()["config_hash"]
    monkeypatch.setenv(variable, "https://proxy.example")
    assert h() != before and judge.judge_identity()["config_hash"] != judge_before
    after, judge_after = h(), judge.judge_identity()["config_hash"]
    monkeypatch.setenv(variable, "https://proxy.example/v2")
    assert h() != after and judge.judge_identity()["config_hash"] != judge_after
    monkeypatch.delenv(variable)
    assert h() == before and judge.judge_identity()["config_hash"] == judge_before


def test_the_endpoint_is_a_registered_component_and_the_default_endpoint_is_in_the_config(with_index, no_endpoint_env):
    assert "endpoint" in pc.PIPELINE_CONFIG_KEYS
    assert pc.pipeline_config("unused", top_k=6)["endpoint"] == {"anthropic_base_url": "https://api.anthropic.com"}


def test_an_installed_package_that_no_longer_resolves_the_endpoint_the_same_way_fails_closed(monkeypatch):
    from langchain_anthropic import ChatAnthropic

    field = ChatAnthropic.model_fields["anthropic_api_url"]
    monkeypatch.setattr(field, "default_factory", None)
    with pytest.raises(RuntimeError):
        pc.endpoint_identity()


# --- R2: the index's search configuration ------------------------------------------------------------------------------------------
def _identity_of(path) -> str:
    identity = REAL_INDEX_IDENTITY(str(path))
    assert identity is not None
    return identity


def test_a_search_configuration_only_change_changes_the_index_identity(tmp_path):
    """Codex 2: ``ef_search`` can change an approximate ranking while the ids, their count and the manifest stay the same."""
    import chromadb

    _store(tmp_path / "s", ["a1", "b2", "c3"], manifest="model-x")
    before = _identity_of(tmp_path / "s")
    collection = chromadb.PersistentClient(path=str(tmp_path / "s")).get_collection(embedder.COLLECTION_NAME, embedding_function=None)
    ids_before = sorted(collection.get(include=[])["ids"])
    configuration = collection.configuration_json
    assert configuration["hnsw"]["ef_search"] != 7
    collection.modify(configuration={"hnsw": {"ef_search": 7}})
    assert sorted(collection.get(include=[])["ids"]) == ids_before  # nothing else moved
    after = _identity_of(tmp_path / "s")
    assert after != before
    collection.modify(configuration={"hnsw": {"ef_search": configuration["hnsw"]["ef_search"]}})
    assert _identity_of(tmp_path / "s") == before  # and back


def test_the_collection_metadata_that_configures_hnsw_is_part_of_the_index_identity(tmp_path):
    import chromadb

    def build(path, metadata):
        client = chromadb.PersistentClient(path=str(path))
        col = client.create_collection(embedder.COLLECTION_NAME, embedding_function=None, metadata=metadata)
        col.add(ids=["a1", "b2"], embeddings=[[0.0, 1.0], [1.0, 1.0]])

    build(tmp_path / "plain", None)
    build(tmp_path / "ef", {"hnsw:search_ef": 11})
    build(tmp_path / "space", {"hnsw:space": "cosine"})
    build(tmp_path / "label", {"label": "not hnsw"})
    identities = {name: _identity_of(tmp_path / name) for name in ("plain", "ef", "space", "label")}
    assert len({identities["plain"], identities["ef"], identities["space"]}) == 3  # each search setting moves it
    assert identities["label"] == identities["plain"]  # a label that is not an `hnsw:*` setting is not a search setting
    # ... the ids-only reading is unchanged: the same ids in a store with the default configuration agree
    build(tmp_path / "plain2", None)
    assert _identity_of(tmp_path / "plain2") == identities["plain"]


# --- the version ---------------------------------------------------------------------------------------------------------------------
def test_the_recipe_is_version_four(with_index):
    assert pc.PIPELINE_CONFIG_VERSION == 4 and pc.pipeline_config("unused", top_k=6)["version"] == 4  # r6: v3; r7: v4 (+ scan_inputs)


# --- R4: the judge's identity covers parsing, eligibility, scoring and the endpoint ---------------------------------------------------
def test_the_judge_identity_names_what_it_covers_and_the_prereg_registers_the_same_list():
    prereg = json.loads((ROOT / "eval" / "prereg_16a2.json").read_text(encoding="utf-8"))
    assert list(judge.JUDGE_COVERS) == prereg["judge"]["covers"] == [
        "model", "client", "prompt", "verdicts", "sample", "parsing", "eligibility", "scoring", "endpoint",
        "preparation", "client_construction", "client_builder", "metering_wrapper"]  # r6: + the preparation path and the client construction; r7: + the execution stack; r8: judge_runtime leaves the hash
    assert set(judge.JUDGE_CODE_MODULES) == {"judge.py", "judge_prep.py", "llm_clients.py", "eval_v6.py", "eval_stats.py"}
    assert all((pc.CODE_DIR / name).is_file() and name in pc.code_modules() for name in judge.JUDGE_CODE_MODULES)


@pytest.mark.parametrize("name,old,new", [
    ("judge.py", "v = verdict.strip().lower()", "v = verdict.strip()"),  # the verdict normalisation
    ("judge.py", 'start = stripped.find("{")', 'start = stripped.find("[")'),  # the JSON extraction
    ("judge.py", 'if not isinstance(claim, dict) or not isinstance(claim.get("verdict"), str):',
     'if not isinstance(claim, dict):'),  # the schema check (parse errors)
    ("judge.py", "FAILURE_SUPPRESSION_THRESHOLD = 0.20", "FAILURE_SUPPRESSION_THRESHOLD = 0.90"),
    ("judge_prep.py", 'if scope == "refuse":', 'if scope == "never":'),  # the eligibility predicate (r6: in judge_prep)
    ("judge_prep.py", "if is_refusal(result[\"answer\"]):", "if False:"),
    ("eval_v6.py", 'scored = [j for j in ok if j["n_claims"] > 0]', 'scored = [j for j in ok if j["n_claims"] >= 0]'),  # the rate
    ("eval_v6.py", 'unsupported = sum(j["unsupported"] for j in ok)', 'unsupported = sum(j["unclear"] for j in ok)'),
    ("eval_stats.py", "WILSON_Z = 1.96", "WILSON_Z = 1.95"),  # the interval that rate carries
])
def test_editing_the_judges_parsing_eligibility_or_scoring_code_changes_the_judge_identity(code_copy, name, old, new):
    before = judge.judge_identity()["config_hash"]
    original = (code_copy / name).read_text(encoding="utf-8")
    assert old in original, (name, old)
    (code_copy / name).write_text(original.replace(old, new, 1), encoding="utf-8")
    assert judge.judge_identity()["config_hash"] != before
    (code_copy / name).write_text(original, encoding="utf-8")
    assert judge.judge_identity()["config_hash"] == before


def test_a_comment_or_docstring_edit_of_the_judge_code_or_an_unrelated_module_does_not_change_the_judge_identity(code_copy):
    before = judge.judge_identity()["config_hash"]
    path = code_copy / "judge.py"
    path.write_text("# a comment\n" + path.read_text(encoding="utf-8").replace('"""', '"""Edited. ', 1), encoding="utf-8")
    assert judge.judge_identity()["config_hash"] == before
    for name in ("evaluator.py", "retriever.py", "llm_transport.py", "sealed_run.py"):  # not the judge's code
        path = code_copy / name
        path.write_text(path.read_text(encoding="utf-8") + "\n_R5_MARK = 1\n", encoding="utf-8")
    assert judge.judge_identity()["config_hash"] == before


def test_the_judge_identity_still_ignores_timeouts_and_retries_and_still_moves_with_the_registered_fields(monkeypatch):
    base = judge.judge_identity()["config_hash"]
    original = generator.generation_llm_kwargs
    monkeypatch.setattr(generator, "generation_llm_kwargs", lambda: {**original(), "default_request_timeout": 1.0, "max_retries": 0})
    assert judge.judge_identity()["config_hash"] == base
    monkeypatch.setattr(judge, "JUDGE_SEED", 1)
    assert judge.judge_identity()["config_hash"] != base


@pytest.mark.parametrize("scope,result,status,eligible", [
    ("answer", {"answer": "An answer. [Handbook, para 1.1, p.1]"}, "complete", True),
    ("partial", {"answer": "An answer. [Handbook, para 1.1, p.1]"}, "complete", True),
    ("refuse", {"answer": "An answer."}, "complete", False),  # not retrieval-eligible
    ("answer", None, "error", False),  # generation raised
    ("answer", {"answer": "An answer."}, "truncated", False),
    ("answer", {"answer": "An answer."}, "declined", False),
    ("answer", {"answer": "An answer."}, "incomplete", False),
    ("answer", {"answer": f"{generator.REFUSAL_PHRASE}."}, "complete", False),  # a refusal is not judged
    ("answer", {"answer": f"{generator.REFUSAL_PHRASE}, but the likely answer is 20 days."}, "complete", True),  # a hedged answer is
])
def test_the_eligibility_predicate(scope, result, status, eligible):
    assert judge.is_judgeable(scope, result, status) is eligible


# --- the values a stored result is made of are not in an excluded module (sealed_results) ----------------------------------------------
MOVED = ("GUARDS", "canonical_guards", "failed_guards", "resolve_excluded_groups", "eligible_counts", "baseline_hits_by_mode",
         "is_private_input", "row_entry", "build_results_doc")


def test_the_results_projection_lives_in_a_hashed_module_and_the_protocol_module_keeps_none_of_it():
    from src import sealed_results, sealed_run

    assert "sealed_results.py" in pc.code_modules() and "sealed_results.py" not in pc.CODE_EXCLUDED
    assert "sealed_run.py" in pc.code_modules() and "sealed_run.py" not in pc.CODE_EXCLUDED  # r6: hashed
    tree = ast.parse(Path(sealed_run.__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    for name in ("canonical_guards", "failed_guards", "resolve_excluded_groups", "_eligible_counts", "_row_entry",
                 "_is_private_input", "baseline_hits_by_mode", "GUARDS"):
        assert name not in defined | assigned, name  # not defined here: imported
    for name in MOVED:
        assert hasattr(sealed_results, name), name
    # the old names still work and are the moved objects (no caller changed)
    assert sealed_run.canonical_guards is sealed_results.canonical_guards and sealed_run.failed_guards is sealed_results.failed_guards
    assert sealed_run.GUARDS is sealed_results.GUARDS and sealed_run.resolve_excluded_groups is sealed_results.resolve_excluded_groups
    assert sealed_run._eligible_counts is sealed_results.eligible_counts and sealed_run._is_private_input is sealed_results.is_private_input
    # the protocol module computes no hit: the words that name one are not in its source
    text = Path(sealed_run.__file__).read_text(encoding="utf-8")
    assert 'cell["strict_hit"]' not in text and "family_hits(" not in text


@pytest.mark.parametrize("old,new", [
    ('"strict_hit": bool(cell["strict_hit"].get(k))', '"strict_hit": bool(cell["related_hit"].get(k))'),  # the row's hit projection
    ('"hits": by_mode[primary],', '"hits": by_mode[list(by_mode)[0]],'),  # the baseline a confirmatory slot is paired with
    ('return {m: family_hits(rows, m) for m in modes}', 'return {m: family_hits(rows, "hybrid") for m in modes}'),
    ('"top_k_6": f.get("top_k") == 6,', '"top_k_6": f.get("top_k") == 7,'),  # a canonical guard
    ('and r["scope"] != "refuse"', 'and r["scope"] != "answer"'),  # the floor counts
    ('frozenset(groups[f] for f in families if f in groups)', 'frozenset(f for f in families if f in groups)'),  # excluded groups
    ('"cutoff": slot_token.baseline["cutoff"],', '"cutoff": slot_token.baseline["cutoff"] + 1,'),
    ('"scorer_version": SCORER_VERSION,', '"scorer_version": "other",'),
])
def test_editing_the_results_projection_changes_the_pipeline_hash(with_index, code_copy, old, new):
    before = h()
    original = (code_copy / "sealed_results.py").read_text(encoding="utf-8")
    assert old in original, old
    (code_copy / "sealed_results.py").write_text(original.replace(old, new, 1), encoding="utf-8")
    ast.parse((code_copy / "sealed_results.py").read_text(encoding="utf-8"))
    assert h() != before
    (code_copy / "sealed_results.py").write_text(original, encoding="utf-8")
    assert h() == before


def test_a_comment_or_docstring_edit_of_the_results_module_does_not_change_the_hash(with_index, code_copy):
    before = h()
    path = code_copy / "sealed_results.py"
    path.write_text("# a comment\n" + path.read_text(encoding="utf-8").replace('"""', '"""Edited. ', 1), encoding="utf-8")
    assert h() == before


# --- the analysis units, the floor and the excluded families are not in the ledger module either (sealed_groups) --------------------------
def test_the_units_the_floor_and_the_exclusions_live_in_a_hashed_module_and_the_ledger_module_keeps_none_of_it():
    import subprocess
    import sys

    from src import sealed_groups, sealed_ledger

    assert "sealed_groups.py" in pc.code_modules() and "sealed_groups.py" not in pc.CODE_EXCLUDED
    assert "sealed_ledger.py" in pc.code_modules() and "sealed_ledger.py" not in pc.CODE_EXCLUDED  # r6: hashed
    tree = ast.parse(Path(sealed_ledger.__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not defined & {"group_ids", "below_floor", "excluded_member_families"}
    # the old names are the moved objects, as module names and as the ledger's own attributes
    assert sealed_ledger.group_ids is sealed_groups.group_ids and sealed_ledger.below_floor is sealed_groups.below_floor
    assert sealed_ledger.SealedLedger.below_floor is sealed_groups.below_floor
    assert sealed_groups._ID.pattern == sealed_ledger._ID.pattern  # one family-id pattern
    # the ledger (imported by the launcher) still loads no model client: the new module is stdlib-only
    code = ("import sys; import src.sealed_ledger; "
            "print([m for m in ('dotenv', 'langchain_anthropic', 'src.generator', 'src.sealed_report', 'src.sealed_results') "
            "if m in sys.modules])")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stderr
    imports = {n.module for n in ast.walk(ast.parse(Path(sealed_groups.__file__).read_text(encoding="utf-8")))
               if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(ast.parse(Path(sealed_groups.__file__).read_text(encoding="utf-8")))
                                                    if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {"__future__", "re", "typing"}, imports


@pytest.mark.parametrize("old,new", [
    ("parent[max(ra, rb)] = min(ra, rb)", "parent[min(ra, rb)] = max(ra, rb)"),  # which member names a group
    ("smallest.setdefault(find(f), f)", "smallest[find(f)] = f"),
    ("for a, b in sorted(edges):", "for a, b in sorted(edges)[1:]:"),  # a dropped edge changes the units
    ("return _count(eligible_after_exclusion, \"eligible_after_exclusion\") < _count(floor, \"floor\")",
     "return _count(eligible_after_exclusion, \"eligible_after_exclusion\") <= _count(floor, \"floor\")"),  # the floor test
    ("out.update(members)", "out.update(members[:1])"),  # which families an exclusion takes out
    ('members = rec.get("members", [rec["group_id"]])', 'members = rec.get("members", [])'),
])
def test_editing_the_units_the_floor_or_the_exclusions_changes_the_pipeline_hash(with_index, code_copy, old, new):
    before = h()
    original = (code_copy / "sealed_groups.py").read_text(encoding="utf-8")
    assert old in original, old
    (code_copy / "sealed_groups.py").write_text(original.replace(old, new, 1), encoding="utf-8")
    ast.parse((code_copy / "sealed_groups.py").read_text(encoding="utf-8"))
    assert h() != before
    (code_copy / "sealed_groups.py").write_text(original, encoding="utf-8")
    assert h() == before


def test_the_moved_computations_behave_as_before():
    from src import sealed_groups as g

    assert g.group_ids(["f1", "f2", "f3", "f4"], [("f2", "f1"), ("f3", "x9"), ("x9", "f4")]) == {
        "f1": "f1", "f2": "f1", "f3": "f3", "f4": "f3"}  # an edge to a rejected draft keeps its connectivity
    assert g.below_floor(34, 35) is True and g.below_floor(35, 35) is False  # exactly at the floor is enough
    with pytest.raises(ValueError):
        g.below_floor(True, 3)
    records = [{"seq": 1, "group_id": "ga", "members": ["fa", "fb"]}, {"seq": 2, "group_id": "gold"}]  # the second is legacy
    assert g.excluded_member_families(records) == frozenset({"fa", "fb", "gold"})
    for bad in ({"seq": 3, "group_id": "g", "members": []}, {"seq": 3, "group_id": "g", "members": "fa"},
                {"seq": 3, "group_id": "g", "members": ["has space"]}, {"seq": 3, "group_id": "g", "members": [5]}):
        with pytest.raises(ValueError, match="record 3: bad exclusion members"):
            g.excluded_member_families([bad])
