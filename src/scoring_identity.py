"""The identity of the scorer, computed from the code and data that score (16A-2b b1a r6, D74).

Two results are comparable (a candidate's sealed slot against slot 0's canonical run; a candidate's development draws
against the incumbent's) only if the same scorer produced them. ``src.eval_cohort.SCORER_VERSION`` was a string someone had
to remember to bump; this module computes the identity instead: :func:`scorer_identity` is the sha256 of the canonical
JSON of

* ``code``: the ``ast.dump`` digest (docstrings stripped, as in ``src.pipeline_config``) of each module in
  :data:`SCORING_CODE_MODULES`, the modules that turn rows and ranked results into hits, ranks, rates, analysis units and
  the stored results document: the row loaders and the schema that fixes a row's evidence groups and scope, the evidence
  scoring, the observed-scope and partial-correctness scoring, the family collapse, the interval and the tests, the cohort
  roster, the metric computations over the stored document, the analysis units and the exclusion aggregation, the refusal
  test and status normalisation, the sentence splitter and gap-statement test;
* ``functions``: the digest of each function in :data:`SCORING_FUNCTIONS`, scoring code that lives in a module shared with
  the pipeline (``generator._sections_related`` decides related matches; hashing all of ``generator.py`` here would make
  every generation change a scorer change);
* ``values``: the literals the scorer reads from shared modules (the caveat opener it strips, the grounding vocabulary it
  compares a result with);
* ``inputs``: :func:`scoring_inputs`, the sha256 of each committed data file the scoring path reads (``None`` for an absent
  file): the absorbed-section map (its aliases count as hits at their chunk's rank) and the section inventory.

``SCORER_VERSION`` stays as the human label every sidecar and results document carries; it is no longer the comparison key.
The prereg registers the comparison (``adoption.scoring``: the identity name, ``candidate_and_slot0_equal`` and
:func:`covered`) and ``judge.compare`` (equal judge hash AND equal ``scorer_identity``).

Rendering is not in the list: ``src.sealed_report`` holds the text of a report, and the metric computations it used to hold
live in ``src.sealed_results``. Presentation arithmetic left there (a rate printed as ``hits/n``) derives from values the
scored modules computed.

The module reads files and nothing else: no model is loaded, nothing is called. Tests point :data:`CODE_DIR` (through
``src.pipeline_config``) and :data:`DATA_ROOT` at copies.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

SCORER_IDENTITY_NAME = "scorer_identity_v1"

#: The ``src`` modules whose code is part of the scorer's identity (see the module docstring for what each is for).
SCORING_CODE_MODULES = (
    "eval_cohort.py", "eval_schema.py", "eval_scoring.py", "eval_sealed_read.py", "eval_stats.py", "eval_v6.py",
    "judge_prep.py", "sealed_groups.py", "sealed_results.py", "text_utils.py",
)

#: Scoring functions that live in a shared module, as ``(file, function)``.
SCORING_FUNCTIONS = (("generator.py", "_sections_related"),)

#: The literals the scorer reads from shared modules, as ``(module, name)``; their values are in the identity.
SCORING_VALUES = (
    ("generator", "CAVEAT_PREFIX"),
    ("grounding", "CITATIONS_UNVERIFIED"), ("grounding", "CITATIONS_VERIFIED"), ("grounding", "PARTIALLY_VERIFIED"),
    ("grounding", "STATUS_COMPLETE"), ("grounding", "TERMINAL_OUTCOMES"),
)

#: The committed data files the scoring path reads, relative to :data:`DATA_ROOT`: ``eval/absorbed_sections.json``
#: (``evaluator._load_absorbed_map``: aliases that count as hits at their chunk's rank, so strict ranks depend on it) and
#: ``eval/section_inventory.json`` (``eval_schema.load_inventory``: the section labels an evidence group may name).
SCORING_DATA_FILES = ("eval/absorbed_sections.json", "eval/section_inventory.json")

#: The repository root the data files are read from (tests point it at a copy).
DATA_ROOT = Path(__file__).resolve().parent.parent


def covered() -> List[str]:
    """What the identity covers, as the prereg registers it (``adoption.scoring.covers``): modules, functions, values, data files."""
    return [
        *SCORING_CODE_MODULES,
        *(f"{file}:{name}" for file, name in SCORING_FUNCTIONS),
        *(f"{module}.{name}" for module, name in SCORING_VALUES),
        *SCORING_DATA_FILES,
    ]


def scoring_inputs() -> Dict[str, Optional[str]]:
    """``{data file: sha256 of its bytes, or None when the file is absent}`` for :data:`SCORING_DATA_FILES`."""
    out: Dict[str, Optional[str]] = {}
    for relative in SCORING_DATA_FILES:
        path = DATA_ROOT / relative
        out[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return out


def scorer_identity() -> str:
    """The scorer's identity: sha256 (hex) of the canonical JSON of the code, function, value and data-file digests.

    Raises:
        OSError, SyntaxError: a scoring module cannot be read or parsed (never a silent partial identity).
    """
    import importlib

    from src import pipeline_config

    values: Dict[str, Any] = {}
    for module, name in SCORING_VALUES:
        value = getattr(importlib.import_module(f"src.{module}"), name)
        values[f"{module}.{name}"] = sorted(value) if isinstance(value, (tuple, list, set, frozenset)) else value
    doc = {
        "version": SCORER_IDENTITY_NAME,
        "code": {name: pipeline_config.code_digest(pipeline_config.CODE_DIR / name) for name in SCORING_CODE_MODULES},
        "functions": {
            f"{file}:{name}": pipeline_config.function_digest(pipeline_config.CODE_DIR / file, name)
            for file, name in SCORING_FUNCTIONS
        },
        "values": values,
        "inputs": scoring_inputs(),
    }
    return pipeline_config.sha256_hex(pipeline_config.canonical_json(doc))
