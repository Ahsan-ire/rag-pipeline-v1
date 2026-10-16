"""16A-2b b1a closure round r4, part A: ``src.pipeline_config`` (the incumbent's identity) and the judge identity.

Zero calls: every test runs with the model loaders and the network refused. The real ``index_identity`` is tested
against a temporary Chroma store written with explicit vectors (no embedding model); the suite's autouse fixture
otherwise replaces it with "no index", so no test reads a real ``chroma_db``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import bm25_index, embedder, generator, judge, pipeline_config as pc, query_rewrite, retriever
from src.eval_cohort import build_sidecar

REAL_INDEX_IDENTITY = pc.index_identity  # captured at import, before the autouse fixture replaces it
ROOT = Path(__file__).resolve().parent.parent
INDEX = "a" * 64


@pytest.fixture
def with_index(monkeypatch):
    """A pipeline with an index of a fixed identity (so the hash exists), the model loaders and the network refused."""
    monkeypatch.setattr(pc, "index_identity", lambda persist_directory: INDEX)

    def refuse(*args, **kwargs):
        raise AssertionError("pipeline_config must not load a model or open a client")

    for owner, name in (
        (embedder, "get_embedding_function"), (embedder, "get_vector_store"), (retriever, "load_retrieval_context"),
        (generator, "get_llm"), (generator, "ChatAnthropic"), (query_rewrite, "get_rewrite_llm"),
        (embedder, "HuggingFaceEmbeddings"),
    ):
        monkeypatch.setattr(owner, name, refuse)
    attempts = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: attempts.append(address) or (_ for _ in ()).throw(OSError))
    return attempts


def h(top_k: int = 6):
    return pc.pipeline_config_hash("unused", top_k=top_k)


# --- the hash: shape, determinism, zero calls --------------------------------------------------------------------------------
def test_the_config_has_exactly_the_registered_components_and_the_hash_is_a_deterministic_digest(with_index):
    config = pc.pipeline_config("unused", top_k=6)
    assert tuple(config) == pc.PIPELINE_CONFIG_KEYS == (
        "version", "code", "runtime", "embedding", "index", "bm25", "fusion", "rewrite", "generation")
    assert config["version"] == pc.PIPELINE_CONFIG_VERSION == 1
    assert set(config["code"]) == set(pc.CODE_MODULES)
    value = h()
    assert value == h() == pc.pipeline_config_hash("elsewhere", top_k=6)  # the directory is read, never hashed
    assert len(value) == 64 and set(value) <= set("0123456789abcdef")
    assert json.loads(json.dumps(config)) == config  # JSON-serialisable as it is


def test_computing_the_hash_loads_no_model_makes_no_client_and_no_network_attempt(with_index):
    h()
    pc.pipeline_config("unused", top_k=6)
    assert with_index == []  # no connection attempt (the loaders raise if touched)


def test_no_index_means_no_hash(monkeypatch):
    monkeypatch.setattr(pc, "index_identity", lambda persist_directory: None)
    assert pc.pipeline_config("unused", top_k=6)["index"] is None
    assert h() is None  # a missing index can never equal a recorded incumbent


def test_the_hashed_modules_are_real_files_that_shape_retrieval_and_generation():
    names = {"retriever.py", "bm25_index.py", "embedder.py", "chunker.py", "query_rewrite.py", "generator.py", "grounding.py"}
    assert set(pc.CODE_MODULES) == names and all((pc.CODE_DIR / n).is_file() for n in names)


# --- every component moves the hash --------------------------------------------------------------------------------------------
def _prompt(text):
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages([("system", text), ("human", "Question: {question}")])


@pytest.mark.parametrize("name,change", [
    ("RRF_K", lambda m: m.setattr(retriever, "RRF_K", retriever.RRF_K + 1)),
    ("rewrite weight", lambda m: m.setattr(retriever, "REWRITE_LIST_WEIGHT", 0.4)),
    ("intent weight", lambda m: m.setattr(retriever, "INTENT_LIST_WEIGHT", 0.1)),
    ("candidate pool", lambda m: m.setattr(retriever, "CANDIDATE_POOL", 13)),
    ("rewrite prompt", lambda m: m.setattr(query_rewrite, "REWRITE_PROMPT_TEMPLATE", _prompt("another rewrite prompt"))),
    ("rewrite model", lambda m: m.setattr(query_rewrite, "REWRITE_MODEL", "another-rewrite-model")),
    ("rewrite cap", lambda m: m.setattr(query_rewrite, "MAX_REWRITES", query_rewrite.MAX_REWRITES + 1)),
    ("GENERATION_MODEL", lambda m: m.setattr(generator, "GENERATION_MODEL", "another-model")),
    ("generation prompt", lambda m: m.setattr(generator, "PROMPT_TEMPLATE", _prompt("another generation prompt"))),
    ("refusal phrase", lambda m: m.setattr(generator, "REFUSAL_PHRASE", "another phrase")),
    ("caveat prefix", lambda m: m.setattr(generator, "CAVEAT_PREFIX", "another prefix")),
    ("generation client", lambda m: m.setattr(
        generator, "generation_llm_kwargs", lambda: {**ORIGINAL_KWARGS(), "max_tokens": 4096})),
    ("EMBEDDING_MODEL", lambda m: m.setattr(embedder, "EMBEDDING_MODEL", "Alibaba-NLP/gte-modernbert-base")),
    ("collection name", lambda m: m.setattr(embedder, "COLLECTION_NAME", "another_collection")),
    ("index ids", lambda m: m.setattr(pc, "index_identity", lambda persist_directory: "b" * 64)),
    ("bm25 tokenizer", lambda m: m.setattr(bm25_index, "_TOKEN_PATTERN", __import__("re").compile(r"\w+"))),
    ("python", lambda m: m.setattr(pc, "sys", SimpleNamespace(version_info=SimpleNamespace(major=3, minor=99)))),
    ("package version", lambda m: m.setattr(
        pc.importlib.metadata, "version", lambda name: "9.9.9" if name == "rank-bm25" else REAL_VERSION(name))),
])
def test_each_component_changes_the_hash(with_index, monkeypatch, name, change):
    before = h()
    change(monkeypatch)
    assert h() != before, name


ORIGINAL_KWARGS = generator.generation_llm_kwargs
REAL_VERSION = pc.importlib.metadata.version


def test_top_k_is_part_of_the_fusion_component(with_index):
    assert h(6) != h(7)


def test_a_timeout_or_retry_change_does_not_change_the_hash(with_index, monkeypatch):
    before = h()
    original = generator.generation_llm_kwargs
    monkeypatch.setattr(generator, "generation_llm_kwargs",
                        lambda: {**original(), "default_request_timeout": 5.0, "max_retries": 9})
    assert h() == before
    # ... and the rewrite client's timeout and retries are left out by the rewrite identity itself
    original_rewrite = query_rewrite.rewrite_llm_kwargs
    monkeypatch.setattr(query_rewrite, "rewrite_llm_kwargs",
                        lambda: {**original_rewrite(), "default_request_timeout": 1.0, "max_retries": 0})
    assert h() == before


def test_the_embedding_environment_override_reaches_the_resolved_model():
    """``EMBEDDING_MODEL`` is read once at import (``embedder.EMBEDDING_MODEL``); the identity reports it as resolved."""
    code = ("from src import pipeline_config as pc; import json; "
            "print(json.dumps(pc.embedding_identity()))")

    def identity(**env):
        base = {k: v for k, v in os.environ.items() if k != "EMBEDDING_MODEL"}
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **env}, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])

    default, overridden = identity(), identity(EMBEDDING_MODEL="Alibaba-NLP/gte-modernbert-base")
    assert default["model"] == embedder.DEFAULT_EMBEDDING_MODEL
    assert overridden["model"] == "Alibaba-NLP/gte-modernbert-base" and overridden != default
    assert overridden["spec"]["max_seq_length"] == 8192  # the spec of the resolved model, not the default's


# --- the code component ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def code_copy(tmp_path, monkeypatch):
    """A private copy of the hashed modules that ``pipeline_config`` reads instead of ``src``."""
    for name in pc.CODE_MODULES:
        shutil.copyfile(pc.CODE_DIR / name, tmp_path / name)
    monkeypatch.setattr(pc, "CODE_DIR", tmp_path)
    return tmp_path


def test_a_code_edit_changes_the_hash_and_a_comment_or_docstring_edit_does_not(with_index, code_copy):
    target = code_copy / "retriever.py"
    before = h()
    original = target.read_text(encoding="utf-8")
    # comments, blank lines and docstrings are not code
    edited = original.replace('"""Hybrid (BM25 + vector) retrieval module, fused by reciprocal rank fusion (D6)."""',
                              '"""Another module docstring."""', 1)
    edited = "# a new comment\n\n\n" + edited.replace("RRF_K = 60", "RRF_K = 60  # trailing comment", 1)
    assert edited != original
    target.write_text(edited, encoding="utf-8")
    assert h() == before
    target.write_text(original.replace("CANDIDATE_POOL = 12", "CANDIDATE_POOL = 13", 1), encoding="utf-8")  # code
    assert h() != before
    target.write_text(original.replace("def format_context(", "def format_context_x(", 1), encoding="utf-8")  # a rename
    assert h() != before
    target.write_text(original, encoding="utf-8")
    assert h() == before


def test_every_hashed_module_counts(with_index, code_copy):
    before = h()
    for name in pc.CODE_MODULES:
        path = code_copy / name
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\n_R4_MARK = 1\n", encoding="utf-8")
        assert h() != before, name
        path.write_text(original, encoding="utf-8")
        assert h() == before


# --- index_identity on a real (temporary) Chroma store --------------------------------------------------------------------------
def _store(path: Path, ids, *, manifest=None, collection=True):
    import chromadb

    client = chromadb.PersistentClient(path=str(path))
    if collection:
        col = client.create_collection(embedder.COLLECTION_NAME, embedding_function=None)
        if ids:
            col.add(ids=list(ids), embeddings=[[float(i), 1.0] for i, _ in enumerate(ids)], documents=[f"text {i}" for i in ids])
    if manifest is not None:
        (path / embedder.EMBEDDING_MODEL_MANIFEST).write_text(manifest, encoding="utf-8")


def test_index_identity_of_a_missing_or_empty_directory_is_none_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope"
    assert REAL_INDEX_IDENTITY(str(missing)) is None and not missing.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert REAL_INDEX_IDENTITY(str(empty)) is None and list(empty.iterdir()) == []


def test_index_identity_of_a_store_without_the_collection_is_none_and_adds_none(tmp_path):
    _store(tmp_path / "s", [], collection=False)
    import chromadb

    assert REAL_INDEX_IDENTITY(str(tmp_path / "s")) is None
    assert chromadb.PersistentClient(path=str(tmp_path / "s")).list_collections() == []  # none was created


def test_index_identity_hashes_sorted_ids_count_and_manifest_and_reads_ids_only(tmp_path, monkeypatch):
    attempts = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: attempts.append(address) or (_ for _ in ()).throw(OSError))
    _store(tmp_path / "a", ["c3", "a1", "b2"], manifest="model-x\n")
    _store(tmp_path / "b", ["a1", "b2", "c3"], manifest="model-x")  # another insertion order, the manifest stripped
    first = REAL_INDEX_IDENTITY(str(tmp_path / "a"))
    assert first is not None and len(first) == 64
    assert first == REAL_INDEX_IDENTITY(str(tmp_path / "b"))
    _store(tmp_path / "c", ["a1", "b2", "c3", "d4"], manifest="model-x")
    _store(tmp_path / "d", ["a1", "b2", "c3"], manifest="model-y")
    _store(tmp_path / "e", ["a1", "b2", "c3"])  # no manifest at all
    assert len({first, REAL_INDEX_IDENTITY(str(tmp_path / "c")), REAL_INDEX_IDENTITY(str(tmp_path / "d")),
                REAL_INDEX_IDENTITY(str(tmp_path / "e"))}) == 4
    # ids only: the collection is read with include=[] (never documents, metadata or embeddings) and no embedding
    # function is built
    import chromadb
    from chromadb.api.models.Collection import Collection

    seen = []
    real_get = Collection.get
    monkeypatch.setattr(Collection, "get", lambda self, *a, **k: seen.append(k) or real_get(self, *a, **k))
    monkeypatch.setattr(embedder, "get_embedding_function", lambda: (_ for _ in ()).throw(AssertionError("embedding")))
    assert REAL_INDEX_IDENTITY(str(tmp_path / "a")) == first
    assert seen and all(k.get("include") == [] for k in seen)
    assert attempts == []  # no connection attempt (no telemetry, no model download)
    assert chromadb is not None


def test_a_store_written_through_langchain_is_identified_without_loading_a_model(tmp_path):
    """The production writer is ``langchain_chroma.Chroma`` with the embedding function injected by the caller."""
    from langchain_core.documents import Document

    from tests.test_embedder import FakeEmbeddings

    from langchain_chroma import Chroma

    store = Chroma(collection_name=embedder.COLLECTION_NAME, embedding_function=FakeEmbeddings(),
                   persist_directory=str(tmp_path / "lc"))
    store.add_documents([Document(page_content="alpha", metadata={"k": 1}), Document(page_content="beta", metadata={"k": 2})],
                        ids=["id-1", "id-2"])
    del store
    identity = REAL_INDEX_IDENTITY(str(tmp_path / "lc"))
    assert identity is not None
    assert identity == REAL_INDEX_IDENTITY(str(tmp_path / "lc"))


# --- the sidecar carries the hash --------------------------------------------------------------------------------------------------
def test_build_sidecar_carries_the_pipeline_config_hash_and_none_without_an_index():
    kwargs = dict(privacy="private", cohorts=[], rows=[], expansion={"kind": "none"})
    assert build_sidecar(**kwargs, pipeline_config_hash=INDEX)["pipeline_config_hash"] == INDEX
    assert build_sidecar(**kwargs)["pipeline_config_hash"] is None


# --- the judge identity ------------------------------------------------------------------------------------------------------------
def test_the_judge_identity_is_deterministic_and_moves_with_model_prompt_version_client_and_verdicts(monkeypatch):
    base = judge.judge_identity()
    assert base == judge.judge_identity() and set(base) == {"model", "prompt_sha256", "config_hash"}
    assert base["model"] == judge.JUDGE_MODEL and len(base["config_hash"]) == 64
    for owner, name, value in (
        (judge, "JUDGE_MODEL", "another-model"), (judge, "JUDGE_PROMPT_VERSION", "faithfulness-judge-v2"),
        (judge, "_VALID_VERDICTS", ("supported", "unsupported")), (judge, "JUDGE_PROMPT", _prompt("another judge prompt")),
        (judge, "JUDGE_SAMPLE", "sampled"), (judge, "JUDGE_SEED", 1),
    ):
        with pytest.MonkeyPatch.context() as m:
            m.setattr(owner, name, value)
            assert judge.judge_identity()["config_hash"] != base["config_hash"], name
    with pytest.MonkeyPatch.context() as m:  # the client the judge calls through
        m.setattr(generator, "generation_llm_kwargs", lambda: {**ORIGINAL_KWARGS(), "max_tokens": 1})
        assert judge.judge_identity()["config_hash"] != base["config_hash"]
    with pytest.MonkeyPatch.context() as m:  # ... but not its timeout or retries
        m.setattr(generator, "generation_llm_kwargs", lambda: {**ORIGINAL_KWARGS(), "default_request_timeout": 1.0, "max_retries": 0})
        assert judge.judge_identity() == base


def test_the_judge_identity_makes_no_call_and_needs_no_key(monkeypatch):
    monkeypatch.setattr(generator, "get_llm", lambda: (_ for _ in ()).throw(AssertionError("get_llm")))
    monkeypatch.setattr(judge, "get_llm", lambda: (_ for _ in ()).throw(AssertionError("get_llm")))
    assert judge.judge_identity()["config_hash"]


def test_get_llm_still_builds_the_same_client_from_the_shared_kwargs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    built = []
    monkeypatch.setattr(generator, "ChatAnthropic", lambda **kwargs: built.append(kwargs) or object())
    generator.get_llm()
    assert built == [generator.generation_llm_kwargs()] and built[0] == {
        "model": "claude-sonnet-5", "max_tokens": 2048, "thinking": {"type": "disabled"},
        "default_request_timeout": 120.0, "max_retries": 3,
    }
