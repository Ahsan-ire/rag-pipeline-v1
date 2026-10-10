"""Frozen query-expansion artifact (Phase 16A-1 item 5, D68).

A live eval run asks the rewrite model (``src.query_rewrite.expand_query``) for
fresh rewrites on every run, so two runs differ in their expansion draw as well
as in whatever change is under test. A *frozen expansion artifact* records one
draw so that every arm of a comparison can replay exactly the same expansions,
with zero expansion calls.

Artifact JSON shape (``version`` 1; written with ``sort_keys`` and fixed
separators, so the same content always gives the same bytes)::

    {
      "version": 1,
      "kind": "expansion_artifact",
      "inputs": [{"path": str, "sha256": <64 hex>, "kind": "questions"}, ...],
      "identity": {"model": str, "prompt_sha256": <64 hex>, "config_hash": <64 hex>},
      "build": {"entries": int, "live_attempts": int, "fallbacks": int},
      "entries": {
        "<row id>": {
          "question_sha256": <64 hex>,
          "rewrites": [str, ...],
          "intent": str | null,
          "status": one of src.query_rewrite.EXPANSION_STATUSES
        }, ...
      }
    }

* ``inputs`` sits at the top level so ``src.eval_sets.classify`` rule 5 can read
  it: an artifact whose recorded input sha256s are all registered-public sets
  classifies public, anything else private.
* Entries are keyed by row id and bound to the sha256 of the row's question
  text; the question text itself is never stored.
* ``build`` is the build record: ``entries`` = rows recorded; ``live_attempts``
  = expand calls actually made (rows with byte-identical question text share
  one call, like the evaluator's per-question expansion cache);
  ``fallbacks`` = attempts whose status is not ``live`` or that produced zero
  rewrites (the evaluator's ``rewrite_fallbacks`` rule).

Error messages name row ids and field names only, never question text or
model-derived text (item 1's serialiser rule).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src import query_rewrite as qr
from src.eval_privacy import SEALED, SealedInputError
from src.eval_sets import classify, sha256_file
from src.query_rewrite import EXPANSION_STATUSES, STATUS_LIVE, Expansion

ARTIFACT_VERSION = 1
ARTIFACT_KIND = "expansion_artifact"

_HEX64 = frozenset("0123456789abcdef")
_TOP_KEYS = {"version", "kind", "inputs", "identity", "build", "entries"}
_IDENTITY_KEYS = ("model", "prompt_sha256", "config_hash")
_BUILD_KEYS = ("entries", "live_attempts", "fallbacks")
_ENTRY_KEYS = {"question_sha256", "rewrites", "intent", "status"}
_INPUT_KEYS = {"path", "sha256", "kind"}


class ExpansionArtifactError(ValueError):
    """An artifact is malformed, lacks an entry, or does not match its use."""


# --------------------------------------------------------------------------
# Hashing helpers
# --------------------------------------------------------------------------
def _canonical_json(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8, non-ASCII kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_text(text: str) -> str:
    """Hex sha256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_sha256(question: str) -> str:
    """Hex sha256 of a question's exact text (UTF-8, no normalisation)."""
    return _sha256_text(question)


def _is_sha(value: Any) -> bool:
    """True for a 64-character lowercase hex string."""
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


# --------------------------------------------------------------------------
# Rewrite identity
# --------------------------------------------------------------------------
def _prompt_messages() -> List[List[str]]:
    """The rewrite chat template as ``[[role, template text], ...]``.

    Read from ``REWRITE_PROMPT_TEMPLATE`` itself, so an edit to either the
    system prompt or the human turn changes the prompt hash.
    """
    messages: List[List[str]] = []
    for message in qr.REWRITE_PROMPT_TEMPLATE.messages:
        cls = type(message).__name__
        role = "system" if cls.startswith("System") else "human" if cls.startswith("Human") else cls
        prompt = getattr(message, "prompt", None)
        template = getattr(prompt, "template", None)
        if not isinstance(template, str):  # pragma: no cover - defensive
            raise ExpansionArtifactError("rewrite prompt template is not plain text")
        messages.append([str(role), template])
    return messages


def _client_params() -> Dict[str, Any]:
    """``max_tokens``, ``temperature`` and ``thinking`` of the rewrite client.

    Read from ``src.query_rewrite.rewrite_llm_kwargs()``, the same kwargs
    ``get_rewrite_llm()`` builds its client with, so a change to the client
    config changes the config hash and cannot drift silently. An absent
    ``temperature`` is ``None`` (API default) and an absent ``thinking`` is
    ``"off"`` (Haiku 4.5's default).
    """
    kwargs = qr.rewrite_llm_kwargs()
    return {
        "max_tokens": kwargs["max_tokens"],
        "temperature": kwargs.get("temperature"),
        "thinking": kwargs.get("thinking", "off"),
    }


def rewrite_identity() -> Dict[str, str]:
    """The identity of the live rewrite step, as recorded in an artifact.

    Returns ``{"model", "prompt_sha256", "config_hash"}``:

    * ``model``: ``src.query_rewrite.REWRITE_MODEL``.
    * ``prompt_sha256``: sha256 of the canonical JSON (sorted keys, no
      whitespace, UTF-8) of ``[[role, template], ...]`` for every message in
      ``REWRITE_PROMPT_TEMPLATE`` (the system prompt
      ``REWRITE_SYSTEM_PROMPT`` and the ``"Question: {question}"`` human turn).
    * ``config_hash``: sha256 of the canonical JSON of the config object
      ``{"model", "max_tokens", "temperature" and "thinking" (all read from
      the client ``get_rewrite_llm()`` builds; unset thinking is ``"off"``), "max_rewrites",
      "max_rewrite_chars", "max_intent_chars", "intent_tag",
      "prompt_sha256"}``. The parse caps are included because they shape the
      recorded rewrites. Timeout and retry count are excluded: they change
      whether a call succeeds, not what a successful call returns.
    """
    prompt_sha = hashlib.sha256(_canonical_json(_prompt_messages())).hexdigest()
    config = {
        "model": qr.REWRITE_MODEL,
        **_client_params(),
        "max_rewrites": qr.MAX_REWRITES,
        "max_rewrite_chars": qr.MAX_REWRITE_CHARS,
        "max_intent_chars": qr.MAX_INTENT_CHARS,
        "intent_tag": qr.INTENT_TAG,
        "prompt_sha256": prompt_sha,
    }
    return {
        "model": qr.REWRITE_MODEL,
        "prompt_sha256": prompt_sha,
        "config_hash": hashlib.sha256(_canonical_json(config)).hexdigest(),
    }


def _check_identity_shape(identity: Any) -> Dict[str, str]:
    """Validate an identity mapping and return a plain dict copy."""
    if not isinstance(identity, Mapping) or set(identity) != set(_IDENTITY_KEYS):
        raise ExpansionArtifactError("identity must have exactly model, prompt_sha256, config_hash")
    if not isinstance(identity["model"], str) or not identity["model"]:
        raise ExpansionArtifactError("identity.model must be a non-empty string")
    for key in ("prompt_sha256", "config_hash"):
        if not _is_sha(identity[key]):
            raise ExpansionArtifactError(f"identity.{key} must be a sha256 hex digest")
    return {k: identity[k] for k in _IDENTITY_KEYS}


# --------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------
def live_digest(entries: Mapping[str, Expansion]) -> str:
    """Expansion digest of a live arm: a hash only, order-independent.

    Canonical encoding: the list, sorted by row id, of
    ``[row_id, [sha256(rewrite) for rewrite in rewrites], sha256(intent) or ""]``
    (rewrite order within a row is kept, since it is part of the draw; each
    sha256 is over the UTF-8 text), serialised as canonical JSON (sorted keys,
    no whitespace, UTF-8); the digest is the hex sha256 of those bytes.
    Status is not part of the digest.

    Args:
        entries: row id -> the ``Expansion`` used for that row.
    """
    records = []
    for row_id in sorted(entries):
        exp = entries[row_id]
        intent = exp.intent_rewrite
        records.append(
            [
                row_id,
                [_sha256_text(r) for r in exp.rewrites],
                _sha256_text(intent) if intent is not None else "",
            ]
        )
    return hashlib.sha256(_canonical_json(records)).hexdigest()


def artifact_digest(path: Any) -> str:
    """Expansion digest of a replayed arm: the artifact file's sha256."""
    return sha256_file(path)


# --------------------------------------------------------------------------
# Build and save
# --------------------------------------------------------------------------
def _check_inputs(inputs: Any) -> List[Dict[str, str]]:
    """Validate the ``inputs`` header; returns a plain list of dicts."""
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)) or not inputs:
        raise ExpansionArtifactError("inputs must be a non-empty list")
    clean = []
    for i, item in enumerate(inputs):
        if not isinstance(item, Mapping) or set(item) != _INPUT_KEYS:
            raise ExpansionArtifactError(f"inputs[{i}] must have exactly path, sha256, kind")
        if not isinstance(item["path"], str) or not item["path"]:
            raise ExpansionArtifactError(f"inputs[{i}].path must be a non-empty string")
        if not _is_sha(item["sha256"]):
            raise ExpansionArtifactError(f"inputs[{i}].sha256 must be a sha256 hex digest")
        if item["kind"] != "questions":
            raise ExpansionArtifactError(f"inputs[{i}].kind must be 'questions'")
        clean.append({"path": item["path"], "sha256": item["sha256"], "kind": "questions"})
    return clean


def build_artifact(
    rows: Iterable[Tuple[str, str]],
    expand_fn: Callable[[str], Expansion],
    inputs: Sequence[Mapping[str, str]],
    identity: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Expand every row once and return the artifact as a JSON-ready dict.

    Args:
        rows: ``(row_id, question)`` pairs; row ids must be unique non-empty strings.
        expand_fn: called as ``expand_fn(question)``; must return an ``Expansion``
            whose ``model`` equals the identity's model. Rows with byte-identical
            question text share one call; each row still gets its own entry.
        inputs: the question sets the rows came from, each
            ``{"path", "sha256", "kind": "questions"}``.
        identity: the rewrite identity to bind; defaults to :func:`rewrite_identity`.

    Returns:
        The artifact dict (see the module docstring). No question text is kept.

    Raises:
        ExpansionArtifactError: on a duplicate or empty row id, a bad header, or
            an ``expand_fn`` result that is not a valid ``Expansion``.
    """
    ident = _check_identity_shape(identity if identity is not None else rewrite_identity())
    header = _check_inputs(inputs)
    row_list = list(rows)
    seen_ids = set()
    for row_id, question in row_list:
        if not isinstance(row_id, str) or not row_id:
            raise ExpansionArtifactError("row ids must be non-empty strings")
        if row_id in seen_ids:
            raise ExpansionArtifactError(f"duplicate row id: {row_id}")
        if not isinstance(question, str):
            raise ExpansionArtifactError(f"row {row_id}: question must be a string")
        seen_ids.add(row_id)

    by_question: Dict[str, Expansion] = {}
    fallbacks = 0
    entries: Dict[str, Dict[str, Any]] = {}
    for row_id, question in row_list:
        q_sha = question_sha256(question)
        exp = by_question.get(q_sha)
        if exp is None:
            exp = expand_fn(question)
            if not isinstance(exp, Expansion):
                raise ExpansionArtifactError(f"row {row_id}: expand_fn did not return an Expansion")
            if exp.status not in EXPANSION_STATUSES:
                raise ExpansionArtifactError(f"row {row_id}: unknown expansion status")
            if exp.model != ident["model"]:
                raise ExpansionArtifactError(f"row {row_id}: expansion model differs from identity model")
            if not all(isinstance(r, str) for r in exp.rewrites) or not (
                exp.intent_rewrite is None or isinstance(exp.intent_rewrite, str)
            ):
                raise ExpansionArtifactError(f"row {row_id}: rewrites/intent must be strings")
            by_question[q_sha] = exp
            if exp.status != STATUS_LIVE or len(exp.rewrites) == 0:
                fallbacks += 1
        entries[row_id] = {
            "question_sha256": q_sha,
            "rewrites": list(exp.rewrites),
            "intent": exp.intent_rewrite,
            "status": exp.status,
        }

    return {
        "version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        "inputs": header,
        "identity": ident,
        "build": {
            "entries": len(entries),
            "live_attempts": len(by_question),
            "fallbacks": fallbacks,
        },
        "entries": entries,
    }


def artifact_bytes(artifact: Mapping[str, Any]) -> bytes:
    """Deterministic serialisation: sorted keys, 2-space indent, UTF-8, trailing newline."""
    text = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def save_artifact(path: Any, artifact: Mapping[str, Any]) -> str:
    """Validate and atomically write ``artifact`` to ``path``; return its digest.

    The artifact is validated first (a malformed one is never written). The
    bytes go to a sibling ``.tmp`` file that then replaces ``path`` with
    ``os.replace``, so a reader never sees a half-written artifact. Where the
    file may live (private builds under ``eval/private/artifacts/``) is the
    caller's decision.

    Returns:
        The written file's sha256 (equal to :func:`artifact_digest`).
    """
    _validate(artifact)
    data = artifact_bytes(artifact)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ExpansionArtifactError("artifact destination is a symlink")
    # Exclusive, no-follow temp file: a planted symlink at <target>.tmp cannot
    # redirect the write (16A-1 gate round 2).
    from src.eval_privacy import open_exclusive_tmp

    tmp, fh = open_exclusive_tmp(target, "wb")
    try:
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists() and not tmp.is_symlink():
            tmp.unlink()
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Validation and load
# --------------------------------------------------------------------------
def _validate(obj: Any) -> None:
    """Schema-check an artifact dict; raise ExpansionArtifactError on any defect."""
    if not isinstance(obj, Mapping) or set(obj) != _TOP_KEYS:
        raise ExpansionArtifactError(f"artifact must have exactly the keys {sorted(_TOP_KEYS)}")
    if obj["version"] != ARTIFACT_VERSION or isinstance(obj["version"], bool):
        raise ExpansionArtifactError("unsupported artifact version")
    if obj["kind"] != ARTIFACT_KIND:
        raise ExpansionArtifactError("artifact kind must be 'expansion_artifact'")
    _check_inputs(obj["inputs"])
    _check_identity_shape(obj["identity"])
    build = obj["build"]
    if not isinstance(build, Mapping) or set(build) != set(_BUILD_KEYS):
        raise ExpansionArtifactError("build must have exactly entries, live_attempts, fallbacks")
    for key in _BUILD_KEYS:
        value = build[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ExpansionArtifactError(f"build.{key} must be a non-negative integer")
    entries = obj["entries"]
    if not isinstance(entries, Mapping):
        raise ExpansionArtifactError("entries must be an object")
    if build["entries"] != len(entries):
        raise ExpansionArtifactError("build.entries does not match the number of entries")
    if build["live_attempts"] > len(entries) or build["fallbacks"] > build["live_attempts"]:
        raise ExpansionArtifactError("build counts are inconsistent")
    for row_id, entry in entries.items():
        if not isinstance(row_id, str) or not row_id:
            raise ExpansionArtifactError("entry keys must be non-empty row ids")
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS:
            raise ExpansionArtifactError(f"entry {row_id}: must have exactly {sorted(_ENTRY_KEYS)}")
        if not _is_sha(entry["question_sha256"]):
            raise ExpansionArtifactError(f"entry {row_id}: question_sha256 must be a sha256 hex digest")
        rewrites = entry["rewrites"]
        if not isinstance(rewrites, list) or not all(isinstance(r, str) for r in rewrites):
            raise ExpansionArtifactError(f"entry {row_id}: rewrites must be a list of strings")
        if entry["intent"] is not None and not isinstance(entry["intent"], str):
            raise ExpansionArtifactError(f"entry {row_id}: intent must be a string or null")
        if entry["status"] not in EXPANSION_STATUSES:
            raise ExpansionArtifactError(f"entry {row_id}: unknown status")


@dataclass(frozen=True)
class ExpansionArtifact:
    """A loaded, validated expansion artifact, ready to replay.

    Attributes:
        path: where it was loaded from.
        digest: sha256 of the exact bytes loaded (the replay expansion digest).
        privacy: ``classify(path)`` at load time (``public`` or ``private``).
        identity: ``{"model", "prompt_sha256", "config_hash"}``.
        inputs: the ``inputs`` header entries.
        build: the build record ``{"entries", "live_attempts", "fallbacks"}``.
        entries: row id -> recorded entry.
    """

    path: Path
    digest: str
    privacy: str
    identity: Mapping[str, str]
    inputs: Tuple[Mapping[str, str], ...]
    build: Mapping[str, int]
    entries: Mapping[str, Mapping[str, Any]]

    def check_identity(self, identity: Optional[Mapping[str, str]] = None) -> None:
        """Refuse unless the artifact's identity equals ``identity``.

        Args:
            identity: the identity the run would use; defaults to
                :func:`rewrite_identity` (the code's current rewrite step).

        Raises:
            ExpansionArtifactError: naming each mismatched field.
        """
        want = _check_identity_shape(identity if identity is not None else rewrite_identity())
        bad = [k for k in _IDENTITY_KEYS if self.identity[k] != want[k]]
        if bad:
            raise ExpansionArtifactError(f"artifact identity mismatch: {', '.join(bad)}")

    def _row_error(self, row_id: str, question: str) -> Optional[str]:
        """The reason ``(row_id, question)`` cannot be replayed, or None."""
        entry = self.entries.get(row_id)
        if entry is None:
            return f"missing entry for row {row_id}"
        if entry["question_sha256"] != question_sha256(question):
            return f"question sha256 mismatch for row {row_id}"
        return None

    def preflight(self, rows: Iterable[Tuple[str, str]], identity: Optional[Mapping[str, str]] = None) -> None:
        """Check identity and every row before any use, so a run fails with zero expansion calls.

        Args:
            rows: every ``(row_id, question)`` the run will replay.
            identity: as for :meth:`check_identity`.

        Raises:
            ExpansionArtifactError: on an identity mismatch, or listing every
                missing / mismatched row id (ids only, never text).
        """
        self.check_identity(identity)
        problems = [msg for row_id, question in rows if (msg := self._row_error(row_id, question))]
        if problems:
            shown = "; ".join(problems[:20])
            more = f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""
            raise ExpansionArtifactError(f"artifact cannot replay {len(problems)} row(s): {shown}{more}")

    def replay(self, row_id: str, question: str) -> Expansion:
        """The recorded ``Expansion`` for one row.

        Raises:
            ExpansionArtifactError: on a missing entry or a question sha256 mismatch.
        """
        problem = self._row_error(row_id, question)
        if problem:
            raise ExpansionArtifactError(problem)
        entry = self.entries[row_id]
        return Expansion(
            question,
            tuple(entry["rewrites"]),
            self.identity["model"],
            entry["status"],
            entry["intent"],
        )


def load_artifact(path: Any) -> ExpansionArtifact:
    """Load an artifact: sealed refusal first, then schema validation.

    Raises:
        SealedInputError: if ``path`` classifies sealed (checked before reading).
        ExpansionArtifactError: if the file is missing, not JSON, or malformed.
    """
    p = Path(path)
    privacy = classify(p)
    if privacy == SEALED:
        raise SealedInputError("sealed eval input is refused in 16A-1 (exit 4)")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ExpansionArtifactError(f"cannot read artifact ({type(exc).__name__})") from None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ExpansionArtifactError("artifact is not valid UTF-8 JSON") from None
    _validate(obj)
    entries = {
        row_id: {
            "question_sha256": e["question_sha256"],
            "rewrites": tuple(e["rewrites"]),
            "intent": e["intent"],
            "status": e["status"],
        }
        for row_id, e in obj["entries"].items()
    }
    return ExpansionArtifact(
        path=p,
        digest=hashlib.sha256(data).hexdigest(),
        privacy=privacy,
        identity=dict(obj["identity"]),
        inputs=tuple(dict(i) for i in obj["inputs"]),
        build=dict(obj["build"]),
        entries=entries,
    )
