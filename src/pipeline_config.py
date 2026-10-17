"""The identity of the running pipeline: ``pipeline_config_hash`` (16A-2b b1a r4, r5, r6, r7; D74).

A sealed slot compares a candidate with the **incumbent**, so "the incumbent" must be a value a machine can
check, not a claim. This module computes that value with **zero calls** (no model is loaded, no network is
touched, no corpus text is read): the sha256 of the canonical JSON of the configuration that shapes what the
pipeline retrieves, generates, judges and scores.

``pipeline_config(persist_directory, *, top_k)`` is a dict with exactly the keys :data:`PIPELINE_CONFIG_KEYS`:

``version``
    :data:`PIPELINE_CONFIG_VERSION` (4), bumped when this recipe changes.
``code``
    ``{module file: sha256 of its ast.dump with docstrings stripped}`` for **every** ``src`` module that is not in
    :data:`CODE_EXCLUDED`, plus the scripts in :data:`HASHED_SCRIPTS` (keyed ``scripts/<file>``). New modules are
    hashed by default; a module is left out only by being added, reviewed, to the registered exclusion list, and only if
    nothing hashed imports it except the registered inert names (:data:`CODE_EXCLUDED_IMPORTS`; a test holds this). The
    same test holds that every script a hashed module imports (``scan_leaks.py``, ``sp_launch.py``) is itself in
    :data:`HASHED_SCRIPTS`.
    ``ast.dump`` carries no line numbers, so a comment, a docstring or a whitespace edit does not change it; any change
    to the code that runs does.
``runtime``
    The Python ``major.minor`` and **every** installed distribution (``importlib.metadata.distributions()``) as a
    sorted, deduplicated list of ``"normalised-name==version"`` strings.
``endpoint``
    The effective Anthropic base URL, resolved by the installed ``langchain_anthropic.ChatAnthropic`` itself (its
    ``anthropic_api_url`` field's default factory: ``ANTHROPIC_API_URL``, then ``ANTHROPIC_BASE_URL``, then the
    SDK default). Generation, the rewrite and the judge all build that client, so one value serves all three.
``embedding``
    The resolved ``embedder.EMBEDDING_MODEL`` (the environment override counts: it is read once at import), its
    :class:`~src.embedder.EmbeddingModelSpec` and the Chroma collection name.
``index``
    :func:`index_identity`: the sha256 of the sorted chunk ids, their count, the embedding-model manifest beside the
    store and the collection's effective search configuration (Chroma's ``hnsw`` / ``spann`` configuration blocks and
    any ``hnsw:*`` collection metadata); ``None`` when there is no index (and then :func:`pipeline_config_hash` is
    ``None``).
``bm25``
    The tokenizer pattern and ``BM25Okapi``'s ``k1``, ``b`` and ``epsilon`` defaults (the index is built with them).
``fusion``
    ``RRF_K``, the rewrite and intent list weights, the candidate pool and the run's ``top_k``.
``rewrite``
    ``expansion_artifact.rewrite_identity()["config_hash"]`` (rewrite model, prompt, caps) and the rewrite mode.
``generation``
    ``GENERATION_MODEL``, the client kwargs from ``generator.generation_llm_kwargs()`` without the timeout and
    retry count, the sha256 of the prompt template's ``[[role, template], ...]``, the refusal phrase and the caveat
    prefix.
``scoring_inputs``
    ``{data file: sha256 or None}`` for the committed data files the scoring path reads
    (:func:`src.scoring_identity.scoring_inputs`: the absorbed-section map, whose aliases decide strict ranks, and the
    section inventory the evidence labels are validated against); ``None`` for a file that is absent.
``scan_inputs``
    :func:`scan_inputs_component`: ``{"handbook_dump", "public_registry", "legacy_public": sha256 or None, "public_sets":
    {name: sha256 or None}}`` for the data the pre-publication scan (``scripts/scan_leaks.py``) reads that no freeze record
    binds: the handbook dump whose text exempts sealed windows, the public-set registry and legacy list, and each registered
    public set's current file (a drifted one exempts nothing). They decide which families an S-hit excludes, so they are part
    of the identity as well as the scanner's code. ``None`` for a file that is absent.

**Excluded on purpose** (they change whether a call succeeds, or are about the run rather than the pipeline):
timeouts and retries (their literals live in ``src.llm_transport``, itself in :data:`CODE_EXCLUDED`, and the client
kwargs drop them), the git sha, paths (the persist directory is only read, never hashed), the API key, the modules in
:data:`CODE_EXCLUDED`, the eval sets and any corpus text. The judge has its own identity
(``src.judge.judge_identity``) but its code is part of ``code`` here too, and so is all scoring code (the scorer has its
own, ``src.scoring_identity.scorer_identity``). Chunk ids are
content hashes, so the index component moves when the indexed text does; chunk *metadata* (page and section numbers)
is not read, because a hash must never need documents.

Nothing here creates a file, a directory or a Chroma collection.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.metadata
import inspect
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from dotenv import load_dotenv

from src.expansion_artifact import _canonical_json

PIPELINE_CONFIG_VERSION = 4
PIPELINE_CONFIG_KEYS = (
    "version", "code", "runtime", "endpoint", "embedding", "index", "bm25", "fusion", "rewrite", "generation",
    "scoring_inputs", "scan_inputs",
)

#: The ``src`` modules whose code is NOT hashed (registered in the prereg as ``incumbent.code_excluded``; ``prereg-check``
#: recomputes the list from this constant). Every other ``src`` module, a new one included, is hashed. A module may be
#: listed only if it cannot reach a client, a row set, a rank, a score, a judgement, an exclusion or an analysis unit: it
#: builds no client and reads no value that reaches a prompt, a ranking, a verdict or a metric, and it filters or projects
#: no row. Whether anything hashed depends on it is a test (:func:`excluded_import_violations`): a hashed module may import
#: from an excluded one only the names registered in :data:`CODE_EXCLUDED_IMPORTS`. Each entry's reason:
#:
#: * ``__init__.py``: empty package marker.
#: * ``audit.py``: the operational JSONL event log of ``pipeline query`` (what to record, never what to answer). Only
#:   ``src.pipeline`` and ``src.render`` import it, and ``pipeline.query`` swallows an audit failure.
#: * ``eval_roster.py``: the bake-off arm roster for ``scripts/bakeoff_report.py`` and ``scripts/w_sweep.py``; no ``src``
#:   module imports it.
#: * ``eval_split.py``: the family splitter that builds the sets (``scripts/sealed_tool.py``; data preparation); no ``src``
#:   module imports it, and the sets it produced are bound by sha256 at their freeze records.
#: * ``llm_transport.py``: the timeout and retry literals (they change whether a call succeeds, never its result); the client
#:   kwargs drop them from every identity (:data:`CLIENT_RELIABILITY_KEYS`).
#: * ``render.py``: the display of a finished ``pipeline query`` result (display-only by its own contract). Only
#:   ``src.pipeline`` imports it, for ``query``; the evaluator, the judge and the scorers never call it.
#: * ``sealed_inputs.py``: ``inputs-check`` of the hand-made pairing and conflict files (``scripts/sealed_tool.py``); no
#:   ``src`` module imports it.
#: * ``tracing_guard.py``: the tracing refusal and the tracing-variable scrub: it can only stop a run (``TracingRefused``)
#:   or set the tracing variables to ``"false"`` in the environment.
CODE_EXCLUDED = (
    "__init__.py", "audit.py", "eval_roster.py", "eval_split.py", "llm_transport.py", "render.py", "sealed_inputs.py",
    "tracing_guard.py",
)

#: The only names a hashed module may import from an excluded one, as ``"<excluded file>:<name>"`` (registered in the
#: prereg as ``incumbent.code_excluded_imports``). Each is inert: a transport literal that every identity drops, the audit
#: writer and the display of ``pipeline query``, and the tracing refusal. :func:`excluded_import_violations` reports any other.
CODE_EXCLUDED_IMPORTS = (
    "audit.py:build_event", "audit.py:log_event",
    "llm_transport.py:GENERATION_MAX_RETRIES", "llm_transport.py:GENERATION_REQUEST_TIMEOUT",
    "llm_transport.py:REWRITE_MAX_RETRIES", "llm_transport.py:REWRITE_REQUEST_TIMEOUT",
    "render.py:RenderFlags", "render.py:render",
    "tracing_guard.py:TracingRefused", "tracing_guard.py:assert_tracing_off", "tracing_guard.py:scrub_tracing_env",
    "tracing_guard.py:tracing_problems",
)

#: The scripts whose code is hashed with the ``src`` modules (keyed ``scripts/<file>``): ``scan_leaks.py`` decides which
#: sealed groups the pre-publication scan excludes from analysis, so a change to it can change the analysed population, and
#: ``sp_launch.py`` is imported by ``sealed_run`` (``bind_prereg`` takes its prereg-ancestry rule from it), so what the gate
#: accepts as the registered prereg depends on it. A hashed module may import only the scripts listed here
#: (:func:`excluded_import_violations`).
HASHED_SCRIPTS = ("scan_leaks.py", "sp_launch.py")

#: The only imports of an unhashed script that a hashed *script* may make, as ``"<hashed script>:<imported script>"``
#: (registered in the prereg as ``incumbent.code_scripts_inert_imports``). ``sp_launch.check_prereg_valid`` imports
#: ``scripts.sealed_tool`` lazily to run ``prereg-check`` in-process before a sealed-processor data dispatch; ``sealed_run``
#: never calls it (the gate's prereg binding uses ``sp_launch.check_prereg``, which reaches no other script), and hashing
#: ``sealed_tool.py`` would pull the operator tooling (and the data-preparation modules it imports) into the pipeline's
#: identity. A hashed *src* module has no such allowance: any script it imports is in :data:`HASHED_SCRIPTS`.
HASHED_SCRIPT_IMPORTS_INERT = ("sp_launch.py:sealed_tool.py",)
#: Where the hashed modules live (tests point it at a copy to edit one).
CODE_DIR = Path(__file__).resolve().parent
#: Where the hashed scripts live (tests point it at a copy to edit one).
SCRIPTS_DIR = CODE_DIR.parent / "scripts"
#: The repository root the scanner's module is imported from (``scripts.scan_leaks``); fixed, unlike :data:`CODE_DIR`.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Client kwargs that change whether a call succeeds, never what it returns: left out of every identity.
CLIENT_RELIABILITY_KEYS = ("default_request_timeout", "max_retries")


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace, UTF-8): the convention of ``expansion_artifact``."""
    return _canonical_json(obj)


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def prompt_messages(template: Any) -> List[List[str]]:
    """A chat prompt template as ``[[role, template text], ...]`` (the same shape the rewrite identity hashes)."""
    messages: List[List[str]] = []
    for message in template.messages:
        cls = type(message).__name__
        role = "system" if cls.startswith("System") else "human" if cls.startswith("Human") else cls
        text = getattr(getattr(message, "prompt", None), "template", None)
        if not isinstance(text, str):
            raise ValueError("a prompt message is not plain text")
        messages.append([str(role), text])
    return messages


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove the docstring statement of the module and of every class and function (an empty body keeps ``pass``)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


@functools.lru_cache(maxsize=512)
def _code_digest(path: str, mtime_ns: int, size: int) -> str:
    tree = _strip_docstrings(ast.parse(Path(path).read_text(encoding="utf-8")))
    return sha256_hex(ast.dump(tree).encode("utf-8"))


def code_digest(path: Path) -> str:
    """sha256 of ``ast.dump`` of a source file with docstrings stripped (comments never reach the tree)."""
    stat = path.stat()
    return _code_digest(str(path), stat.st_mtime_ns, stat.st_size)


def code_modules() -> List[str]:
    """The hashed modules: every ``*.py`` under :data:`CODE_DIR` (relative posix path) not in :data:`CODE_EXCLUDED`, sorted.

    Hashed by default: a module added to ``src`` joins the identity until it is registered as excluded.
    """
    return sorted(
        path.relative_to(CODE_DIR).as_posix() for path in CODE_DIR.rglob("*.py")
        if path.relative_to(CODE_DIR).as_posix() not in CODE_EXCLUDED and "__pycache__" not in path.parts
    )


def hashed_scripts() -> List[str]:
    """The hashed scripts as ``scripts/<file>`` (the keys of their entries in the ``code`` component), sorted."""
    return sorted(f"scripts/{name}" for name in HASHED_SCRIPTS)


def code_identity() -> Dict[str, str]:
    """``{module file: code digest}`` for :func:`code_modules` and :func:`hashed_scripts` (an unreadable or unparseable file raises)."""
    out = {name: code_digest(CODE_DIR / name) for name in code_modules()}
    out.update({key: code_digest(SCRIPTS_DIR / key.split("/", 1)[1]) for key in hashed_scripts()})
    return out


def _find_definition(tree: ast.AST, qualname: str) -> Optional[ast.AST]:
    """The ``def`` or ``class`` node ``qualname`` names in ``tree``: a dotted path from the module down through classes and functions.

    ``"get_llm"`` is the top-level function, ``"SpendMeter._client"`` the method, ``"MeteredChatModel"`` the class. A bare name
    that is not defined at the top level falls back to the first function of that name anywhere (the form r6 used).
    """
    def child(node: ast.AST, name: str) -> Optional[ast.AST]:
        for item in getattr(node, "body", []):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == name:
                return item
        return None

    node: Optional[ast.AST] = tree
    for part in qualname.split("."):
        node = child(node, part) if node is not None else None
    if node is None and "." not in qualname:
        node = next(
            (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == qualname), None,
        )
    return node


def function_digest(path: Path, qualname: str) -> str:
    """sha256 of ``ast.dump`` of the function, method or class ``qualname`` in ``path`` (docstrings stripped).

    ``qualname`` is a dotted path (``"get_llm"``, ``"SpendMeter._client"``, ``"MeteredChatModel"``): a class is digested whole,
    every method included. For code that lives in a module shared with something else (``src.scoring_identity`` binds
    ``generator._sections_related`` this way instead of hashing the whole generator into the scorer's identity, and
    ``src.judge`` binds ``generator.get_llm`` and the metering wrapper of ``spend`` into the judge's).

    Raises:
        ValueError: ``path`` defines no such function or class.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    node = _find_definition(tree, qualname)
    if node is None:
        raise ValueError(f"{Path(path).name} defines no function or class {qualname!r}")
    return sha256_hex(ast.dump(_strip_docstrings(node)).encode("utf-8"))


def _imports_of(tree: ast.AST) -> List[Tuple[str, str]]:
    """``(src module stem, imported name)`` for every import of a ``src`` module in ``tree``, nested imports included.

    ``from src.render import render`` gives ``("render", "render")``; ``from src import render`` and ``import src.render``
    give ``("render", "<module>")``; ``importlib.import_module("src.render")`` (a string constant naming a module) gives
    ``("render", "<dynamic>")``. A relative import (``from .render import render``) inside ``src`` counts like the absolute one.
    """
    found: List[Tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # inside src: from .x import y / from . import x
                parts = module.split(".") if module else []
                if parts:
                    found.extend((parts[0], alias.name) for alias in node.names)
                else:
                    found.extend((alias.name, "<module>") for alias in node.names)
            elif module == "src":
                found.extend((alias.name, "<module>") for alias in node.names)
            elif module.startswith("src."):
                found.extend((module.split(".")[1], alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    found.append((alias.name.split(".")[1], "<module>"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"src\.[A-Za-z0-9_]+", node.value):
            found.append((node.value.split(".")[1], "<dynamic>"))
    return found


def _script_imports_of(tree: ast.AST) -> List[str]:
    """The stem of every ``scripts`` module ``tree`` imports, statically or by a literal dynamic import, nested imports included.

    ``from scripts.scan_leaks import x``, ``from scripts import scan_leaks``, ``import scripts.scan_leaks`` and a string
    constant ``"scripts.scan_leaks"`` (``importlib.import_module("scripts.scan_leaks")``) all give ``"scan_leaks"``.
    """
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module == "scripts":
                found.extend(alias.name for alias in node.names)
            elif module.startswith("scripts."):
                found.append(module.split(".")[1])
        elif isinstance(node, ast.Import):
            found.extend(alias.name.split(".")[1] for alias in node.names if alias.name.startswith("scripts."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"scripts\.[A-Za-z0-9_]+", node.value):
            found.append(node.value.split(".")[1])
    return found


def excluded_import_violations() -> List[str]:
    """What the hashed code takes from an excluded module beyond :data:`CODE_EXCLUDED_IMPORTS`: ``"<hashed file> -> <excluded file>:<name>"``.

    Walks every module of :func:`code_modules` and every script of :func:`hashed_scripts`. A hashed module that imports an
    excluded module, a name of it that is not registered, the whole module, or names it in a string (a dynamic import),
    depends on code the identity does not cover: the exclusion's justification ("it cannot reach a client, a row set, a rank,
    a score, a judgement, an exclusion or an analysis unit") is then not what the hash assumes. Empty = the exclusions hold.

    r7: it also reports ``"<hashed file> -> scripts/<file>"`` for any ``scripts`` module a hashed module imports (statically
    or by a literal dynamic import) that is not in :data:`HASHED_SCRIPTS`: a script that ``sealed_run`` loads decides what the
    gate accepts or what the scan excludes, so it is hashed too, or it is not imported. The one exception is the registered
    :data:`HASHED_SCRIPT_IMPORTS_INERT`.
    """
    stems = {name[:-3]: name for name in CODE_EXCLUDED}
    allowed = set(CODE_EXCLUDED_IMPORTS)
    scripts = {name[:-3] for name in HASHED_SCRIPTS}
    inert_scripts = set(HASHED_SCRIPT_IMPORTS_INERT)
    sources = [(name, CODE_DIR / name) for name in code_modules()]
    sources += [(key, SCRIPTS_DIR / key.split("/", 1)[1]) for key in hashed_scripts()]
    violations: List[str] = []
    for label, path in sources:
        tree = _strip_docstrings(ast.parse(path.read_text(encoding="utf-8")))
        for stem, name in _imports_of(tree):
            if stem in stems and f"{stems[stem]}:{name}" not in allowed:
                violations.append(f"{label} -> {stems[stem]}:{name}")
        importer = label.split("/", 1)[1] if label.startswith("scripts/") else None  # a hashed script, by file name
        for stem in _script_imports_of(tree):
            if stem not in scripts and not (importer is not None and f"{importer}:{stem}.py" in inert_scripts):
                violations.append(f"{label} -> scripts/{stem}.py")
    return sorted(set(violations))


# ---------------------------------------------------------------------------
# runtime, endpoint, embedding, index
# ---------------------------------------------------------------------------


def normalise_distribution_name(name: str) -> str:
    """The PEP 503 normalised name: lower case, runs of ``-``, ``_`` and ``.`` become one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_identity() -> Dict[str, Any]:
    """The Python ``major.minor`` and EVERY installed distribution as a sorted, deduplicated ``name==version`` list.

    ``importlib.metadata.distributions()`` is read as it stands (no allow-list: a numerical or tokenizer dependency
    is as much a part of the retrieval as the packages named in a requirements file). A distribution with no name
    or no version has nothing to pin and is skipped.
    """
    found = set()
    for dist in importlib.metadata.distributions():
        name, version = dist.metadata["Name"], dist.version
        if name and version:
            found.add(f"{normalise_distribution_name(name)}=={version}")
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}", "packages": sorted(found)}


def distribution_closure(roots: Sequence[str]) -> List[str]:
    """``name==version`` of ``roots`` and every distribution they require, transitively, as a sorted list (r7).

    Walks ``importlib.metadata.requires`` from each root, normalising names (PEP 503) and following a requirement only when it
    applies here: a requirement whose marker is false on this interpreter is ignored, and so is one that belongs to an extra
    nobody asked for (``name[extra]`` in a requirement asks for that extra of ``name``; an extra requested by nothing on the path
    adds nothing). The closure is what is imported on the call path, not what could be installed. A required distribution that
    is not installed is recorded as ``name==missing`` (a broken environment must not look like a smaller one), never skipped
    silently. Unlike :func:`runtime_identity`, which lists everything installed, this is the set one call path depends on, so
    an unrelated package's version does not move an identity built from it.

    Raises:
        packaging.requirements.InvalidRequirement: a requirement string that cannot be parsed (fail closed).
    """
    from packaging.requirements import Requirement

    found: Dict[str, str] = {}
    asked: Dict[str, set] = {}  # the extras requested of each distribution so far
    pending: List[Tuple[str, frozenset]] = [(normalise_distribution_name(root), frozenset()) for root in roots]
    while pending:
        name, extras = pending.pop()
        have = asked.setdefault(name, set())
        if name in found and extras <= have:
            continue  # visited, and nothing new is asked of it
        have |= extras
        if name not in found:
            try:
                found[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                found[name] = "missing"
        if found[name] == "missing":
            continue
        environments = [{"extra": ""}, *({"extra": extra} for extra in sorted(have))]
        for text in importlib.metadata.requires(name) or []:
            requirement = Requirement(text)
            if requirement.marker is not None and not any(requirement.marker.evaluate(env) for env in environments):
                continue  # not for this interpreter, or for an extra nobody asked for: not on the call path
            pending.append((normalise_distribution_name(requirement.name), frozenset(requirement.extras)))
    return sorted(f"{name}=={version}" for name, version in found.items())


def endpoint_identity() -> Dict[str, str]:
    """The effective Anthropic base URL, exactly as the installed ``ChatAnthropic`` resolves it.

    ``langchain_anthropic.ChatAnthropic`` passes its ``anthropic_api_url`` field to the SDK client as ``base_url``
    (so the SDK's own ``ANTHROPIC_BASE_URL`` lookup never runs); the field's default factory reads
    ``ANTHROPIC_API_URL``, then ``ANTHROPIC_BASE_URL``, then falls back to the SDK default. This calls that factory,
    so the recipe follows the installed package instead of copying its rules. Generation, the rewrite and the
    judge all build that class, so one value serves all three. No client is built and nothing is sent.
    """
    from langchain_anthropic import ChatAnthropic

    load_dotenv()  # no override: what the process already has wins; a base URL set only in .env is read like the clients read it
    factory = ChatAnthropic.model_fields["anthropic_api_url"].default_factory
    if factory is None:  # the installed package changed shape: fail closed, never guess the endpoint
        raise RuntimeError("ChatAnthropic.anthropic_api_url has no default factory")
    return {"anthropic_base_url": str(factory())}


def embedding_identity() -> Dict[str, Any]:
    """The resolved embedding model, its spec and the collection name (read as the pipeline reads them)."""
    from src import embedder

    return {
        "model": embedder.EMBEDDING_MODEL,
        "spec": asdict(embedder.get_model_spec()),
        "collection": embedder.COLLECTION_NAME,
    }


def index_identity(persist_directory: str) -> Optional[str]:
    """sha256 of the index: sorted chunk ids, their count, the embedding-model manifest and the search configuration.

    Reads the ids only (Chroma ``get(include=[])``: never documents, metadata or embeddings) and never builds an
    embedding function. The collection's effective search configuration (``configuration_json``'s ``hnsw`` and
    ``spann`` blocks, which carry ``ef_search`` and the other approximate-search settings, and any ``hnsw:*``
    collection metadata) is part of the hash: it can change the ranking while the ids stay the same. ``None`` when
    the directory, the Chroma store or the collection is missing; nothing is created in any of those cases (a
    missing directory is not even opened).
    """
    from src import embedder

    root = Path(persist_directory)
    if not (root / "chroma.sqlite3").is_file():
        return None
    import chromadb

    client = chromadb.PersistentClient(path=str(root))
    try:
        collection = client.get_collection(embedder.COLLECTION_NAME, embedding_function=None)
    except Exception:  # noqa: BLE001 - chromadb raises its own NotFound/ValueError types by version
        return None
    ids = sorted(collection.get(include=[])["ids"])
    manifest = root / embedder.EMBEDDING_MODEL_MANIFEST
    recorded = manifest.read_text(encoding="utf-8").strip() if manifest.is_file() else None
    configuration = collection.configuration_json
    metadata = collection.metadata or {}
    return sha256_hex(canonical_json({
        "ids": ids,
        "count": len(ids),
        "embedding_model_manifest": recorded,
        "search_configuration": {
            "hnsw": configuration.get("hnsw"),
            "spann": configuration.get("spann"),
            "metadata": {key: metadata[key] for key in sorted(metadata) if str(key).startswith("hnsw:")},
        },
    }))


# ---------------------------------------------------------------------------
# bm25, fusion, rewrite, generation
# ---------------------------------------------------------------------------


def bm25_identity() -> Dict[str, Any]:
    """The BM25 tokenizer pattern and ``BM25Okapi``'s ``k1``, ``b``, ``epsilon`` defaults."""
    from rank_bm25 import BM25Okapi

    from src import bm25_index

    params = inspect.signature(BM25Okapi.__init__).parameters
    return {
        "tokenizer": bm25_index._TOKEN_PATTERN.pattern,
        **{name: params[name].default for name in ("k1", "b", "epsilon")},
    }


def fusion_identity(top_k: int) -> Dict[str, Any]:
    """Reciprocal rank fusion: ``RRF_K``, the rewrite and intent list weights, the candidate pool and ``top_k``."""
    from src import retriever

    return {
        "rrf_k": retriever.RRF_K,
        "rewrite_list_weight": retriever.REWRITE_LIST_WEIGHT,
        "intent_list_weight": retriever.INTENT_LIST_WEIGHT,
        "candidate_pool": retriever.CANDIDATE_POOL,
        "top_k": top_k,
    }


def rewrite_component() -> Dict[str, Any]:
    """The rewrite step's ``config_hash`` (model, prompt, caps) and the rewrite retrieval mode."""
    from src import query_rewrite
    from src.expansion_artifact import rewrite_identity

    return {"config_hash": rewrite_identity()["config_hash"], "mode": query_rewrite.REWRITE_MODE}


def generation_identity() -> Dict[str, Any]:
    """The generation model, client kwargs (no timeout or retries), prompt hash, refusal phrase and caveat prefix."""
    from src import generator

    return {
        "model": generator.GENERATION_MODEL,
        "client": {
            k: v for k, v in generator.generation_llm_kwargs().items() if k not in CLIENT_RELIABILITY_KEYS
        },
        "prompt_sha256": sha256_hex(canonical_json(prompt_messages(generator.PROMPT_TEMPLATE))),
        "refusal_phrase": generator.REFUSAL_PHRASE,
        "caveat_prefix": generator.CAVEAT_PREFIX,
    }


def scoring_inputs_component() -> Dict[str, Optional[str]]:
    """``{data file: sha256 or None}`` for the committed data files the scoring path reads (``src.scoring_identity``)."""
    from src import scoring_identity

    return scoring_identity.scoring_inputs()


@functools.lru_cache(maxsize=64)
def _file_digest(path: str, mtime_ns: int, size: int) -> str:
    return sha256_hex(Path(path).read_bytes())


def _digest_or_none(path: Any) -> Optional[str]:
    """sha256 of a file's bytes, or ``None`` when ``path`` is not a file (cached by path, mtime and size)."""
    target = Path(path)
    if not target.is_file():
        return None
    stat = target.stat()
    return _file_digest(str(target), stat.st_mtime_ns, stat.st_size)


def scan_inputs(
    *, handbook_dump: Any, sets_index: Any, legacy_list: Any, public_sets: Mapping[str, Any],
) -> Dict[str, Any]:
    """``{label: sha256 or None}`` for the data the pre-publication scan reads (see the module docstring, ``scan_inputs``).

    The four arguments are paths (tests give temporary ones): the handbook dump, the file that lists the public sets (the
    result's ``public_registry``), the legacy public list, and ``public_sets``, a mapping from a public set's name to its
    file. A path that is not a file is ``None``. Nothing outside the arguments is read.
    """
    return {
        "handbook_dump": _digest_or_none(handbook_dump),
        "public_registry": _digest_or_none(sets_index),
        "legacy_public": _digest_or_none(legacy_list),
        "public_sets": {name: _digest_or_none(public_sets[name]) for name in sorted(public_sets)},
    }


def scan_inputs_component() -> Dict[str, Any]:
    """:func:`scan_inputs` over the scanner's own path constants (``scripts.scan_leaks`` and ``src.eval_sets``).

    The handbook dump is ``scan_leaks.HANDBOOK_DUMP`` under the scanner's repo base, the registry and the legacy list are
    ``eval_sets.REGISTRY_PATH`` and ``eval_sets.LEGACY_PUBLIC_PATH`` and the public sets are the registry's public entries, so
    this follows the scanner instead of copying its paths. The suite replaces this function (``tests/conftest.py``) so that
    no test reads a real corpus-derived file through it; the paths themselves are tested with temporary files.
    """
    import importlib

    from src import eval_sets

    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    scan_leaks = importlib.import_module("scripts.scan_leaks")
    return scan_inputs(
        handbook_dump=scan_leaks._repo_base() / scan_leaks.HANDBOOK_DUMP,
        sets_index=eval_sets.REGISTRY_PATH,
        legacy_list=eval_sets.LEGACY_PUBLIC_PATH,
        public_sets={entry.name: entry.resolved() for entry in eval_sets.public_entries()},
    )


# ---------------------------------------------------------------------------
# the configuration and its hash
# ---------------------------------------------------------------------------


def pipeline_config(persist_directory: str, *, top_k: int) -> Dict[str, Any]:
    """The pipeline configuration (see the module docstring): a JSON-serialisable dict, zero calls."""
    return {
        "version": PIPELINE_CONFIG_VERSION,
        "code": code_identity(),
        "runtime": runtime_identity(),
        "endpoint": endpoint_identity(),
        "embedding": embedding_identity(),
        "index": index_identity(persist_directory),
        "bm25": bm25_identity(),
        "fusion": fusion_identity(top_k),
        "rewrite": rewrite_component(),
        "generation": generation_identity(),
        "scoring_inputs": scoring_inputs_component(),
        "scan_inputs": scan_inputs_component(),
    }


def pipeline_config_hash(persist_directory: str, *, top_k: int) -> Optional[str]:
    """sha256 of the canonical JSON of :func:`pipeline_config`, or ``None`` when there is no index.

    A pipeline with no index has no identity: ``None`` never equals a recorded incumbent hash, so a missing or
    emptied index cannot pass for the incumbent.
    """
    config: Mapping[str, Any] = pipeline_config(persist_directory, top_k=top_k)
    if config["index"] is None:
        return None
    return sha256_hex(canonical_json(config))
