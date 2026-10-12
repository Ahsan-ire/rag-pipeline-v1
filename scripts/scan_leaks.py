"""Phase 16A-1 leak scanner (item 1, D65; sealed needles 16A-2a a2, D72): find private eval questions before they leave the box.

The scanner never prints or writes a needle, matched text or file content. A
hit names only *where* (target), *which* needle (an opaque id), its *kind* and
its *source*; an S-hit (a sealed needle) names only the target and the
family's opaque id.

Usage::

    python scripts/scan_leaks.py --output <file>... [--run <run id>] [--needles <path>...]
                                 [--handbook-dump <path>]
    python scripts/scan_leaks.py --merge-gate --base <ref> [--pr <number>] [--gh <exe>]
                                 [--repo <dir>] [--needles <path>...] [--handbook-dump <path>]

Sources
-------
- every registry ``private`` set (:func:`src.eval_sets.private_needle_sets`);
- **every ``*.jsonl`` under the private root** outside the traced areas
  (``runs/``, ``artifacts/``, ``sealed/runs/``, ``sealed/artifacts/``), found
  automatically, so a new private or sealed file needs no registration (the
  content-free **record files** in :data:`RECORD_FILES` -- the sealed ledger
  copy, its payloads and the SP dispatch logs -- are not sources; the pre-check
  allows them and ``--merge-gate`` scans them as targets);
- each ``--needles <path>``: a v1 or v2 eval ``.jsonl`` that must classify
  ``private`` or ``sealed`` (a ``public`` one is refused, exit 2);
- ``--output`` mode with ``--run <id>``: the run's own inputs from
  ``eval/private/runs/<id>/inputs.json`` (or ``eval/private/sealed/runs/<id>/``)
  -- each ``questions`` input and each ``derived`` input's listed ``sources``. A
  run input whose recorded sha256 is a registered public set's contributes no
  needles (every one of its questions would be exempt) and is not refused; one
  whose file is missing or no longer matches its recorded sha256 is refused
  (exit 2: the needles it held cannot be rebuilt).

A source that is sealed (under ``eval/private/sealed/`` or carrying a sealed
marker) is no longer refused: its questions become **S-needles**, loaded only
through :func:`src.eval_sealed_read.load_sealed` (one validator; a row whose
``sealed`` key is not ``true``, or any other schema error, is exit 2 with
line/field errors only). A registered private set that is missing on disk is
refused (exit 2): the scan would silently lose its needles. A source line that
does not parse as JSON is refused (exit 2, path:line only); rows without a
string ``question`` are skipped (metadata rows).

Needles
-------
Text, needle and target alike, is normalised by :func:`normalise_tokens`:
escapes decoded (``\\uXXXX``/``\\UXXXXXXXX``/``\\xXX`` and JSON's ``\\n \\t \\"
\\\\ \\/`` ..., repeated up to four times so double-escaped text decodes too,
then HTML entities), NFKC, casefold, every character that is neither
alphanumeric nor whitespace becomes a space, whitespace collapsed, split into
tokens. Each private question yields its whole needle and, at 8 or more
tokens, every 8-token window (under 8 tokens only the whole needle exists).

- A whole question is exempt only if its normalised sha256 equals a registered
  public question's (a row's ``source`` field grants nothing).
- A window is exempt only if it occurs (as 8 consecutive tokens) in a
  registered public question.
- Registered public sets are read only when their file still matches the
  registered sha256; a drifted public file grants no exemptions (fail safe).

**S-needles (sealed questions).** A sealed question of 8 or more tokens yields
its whole needle and every 8-token window, matched in the same token stream.
Its **exempt windows** are those that occur in a registered public question, in
a development question (any non-sealed source above) or in handbook text (the
dump, below). A sealed question of **fewer than 8 tokens has no window needle**
and is not matched inside running text: only an exact match of the whole
normalised question against a whole JSON string value (a string between two
quote marks, a key included) or against a whole line counts. Residual: such a
question embedded in prose, a numbered list item or an unquoted ``key: value``
line is missed. A whole short question equal to a registered public question is
exempt, as above.

**Handbook dump.** Handbook text is read in-process from a JSONL dump (one JSON
object per line, the section text under ``text``): ``--handbook-dump``, default
``data/research/handbook_sections.jsonl`` under the repo base. It is never
printed or copied. If it is absent or unreadable the scan says so on stderr and
the handbook exemption is empty (more S-hits, never fewer); it does not crash.

Needle id = ``"n"`` + the first 10 hex of sha256(normalised needle text, tokens
joined by single spaces). An S-hit never prints a needle id (a hash of a sealed
question would confirm candidate text).

**Promise:** every non-exempt whole question and window is caught, including
across line breaks (targets are tokenised as one stream; the hit reports the
line where the match starts). Shorter fragments are the serialiser's job.
"Re-spaced" means any change of whitespace (runs, tabs, newlines); deleting
the space *between* two words is not caught (the tokens change), nor is
URL-encoding.

Hits
----
One line per hit on stdout: ``target<TAB>needle_id<TAB>kind<TAB>source`` where
kind is ``whole`` or ``window`` and source is the registry set name or the
source path. A window hit that lies inside a whole hit's span on the same
target is folded into the whole hit. **An S-hit prints ``target<TAB>family_id``
and nothing else** (the family's opaque ``f`` + 8 hex id). Targets: ``<file>:<line>`` (``--output``
and tracked files at HEAD), ``<path>@<commit12>:<line>`` (a blob version from
the range), ``commit <sha12>:<line>``, ``tag <object sha12>:<line>``, ``ref <object sha12>:<line>``
(branch and tag ref names are scanned but never printed: a name could carry text), ``PR#<n> title:<line>``, ``PR#<n> body:<line>``,
``PR#<n> comment[i]:<line>``, ``PR#<n> review[i]:<line>`` and
``PR#<n> review-comment[i]:<line>``.

Modes
-----
``--output`` scans the given files (a private run's stdout, stderr, captured
logging and report) before release; any hit aborts with exit 5.

``--merge-gate --base <ref>`` first runs the source pre-check (refusal exit 7,
paths only) and then scans: every tracked file at HEAD; every blob version
added or modified by any commit in ``base..HEAD`` (merges diffed against each
parent, so a blob added and deleted inside the range is still scanned); every
commit message in the range; every tag (annotation, all tags in the repo); every branch and tag ref name; and
with ``--pr <n>`` the PR title, body, comments, review bodies and review comments via
``gh`` (``gh pr view <n> --json title,body,comments,reviews`` and
``gh api --paginate repos/{owner}/{repo}/pulls/<n>/comments``). Without
``--pr`` PR items are skipped with a note on stderr. A ``gh`` failure is exit 2
(the gate cannot vouch for what it did not read). Any hit is exit 5.

Pre-check (exit 7): refused when (a) a file under the private root, outside
the traced areas (``runs/``, ``artifacts/``, ``sealed/runs/``,
``sealed/artifacts/``) and outside a ``work/`` directory (``eval/private/work/``
and ``eval/private/sealed/work/``, which the pre-check does not look into), is
not a ``.jsonl`` needle source (matched by sha256 against every needle source
file; every non-symlink ``*.jsonl`` there is one automatically, so this refuses
any other file type and any symlink to a file that is not a source), or (b) a
``runs/*/inputs.json`` (or ``sealed/runs/*/inputs.json``) does not parse, or
lists a ``questions`` input that is not a needle source, or a ``derived`` input
that is not a needle source and not exempt, or a ``derived`` input's listed
source (or a sha256-keyed artifact's own ``inputs`` header) that is not a
needle source, a registered public set or a legacy-public entry. Exempt
derived inputs (their sources are still checked): a recorded sha256 listed in
``eval/legacy_public.json``; and a **sha256-keyed artifact with no question
text** -- a file under ``eval/private/artifacts/`` (or ``sealed/artifacts/``)
that still matches its recorded sha256, parses as JSON, has a non-empty
``entries`` container whose every item carries a 64-hex value under a key
containing ``sha256``, and has no key named ``question`` or ``questions``
anywhere in its tree. (Residual: other string values in such an artifact are
not inspected; a ``work/`` file's text is not inspected unless it is a
``.jsonl``, which is a needle source like any other.) Refusal lines print
paths only, never content.

Exit codes: 0 clean; 2 usage error, refused public ``--needles``, unreadable,
missing or invalid source (a sealed source that fails the loader included),
``gh`` failure; 5 hit (either mode); 7 merge-gate source pre-check refused.
(Exit 4, "sealed source offered", is retired: sealed sources are scanned.)

With private sets present, ``--merge-gate`` runs before every push (CI cannot:
it has no private sets).

Python API: :func:`normalise_tokens`, :func:`needle_id`, :func:`collect_sources`,
:func:`build_needles`, :func:`scan_text`, :func:`scan_output`,
:func:`merge_gate`, :func:`main`.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src import eval_privacy  # noqa: E402  (module import: tests relocate private_root)
from src import eval_sets  # noqa: E402
from src.eval_privacy import PRIVATE, PUBLIC, REPO_ROOT  # noqa: E402
from src.eval_schema import SchemaError, render_errors  # noqa: E402
from src.eval_sealed_read import NotSealedInput, load_sealed  # noqa: E402  (the only sealed reader)

WINDOW = 8
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_HIT = 5
EXIT_PRECHECK = 7

# Production location of the handbook dump (b0), relative to the repo base.
HANDBOOK_DUMP = Path("data") / "research" / "handbook_sections.jsonl"

# Content-free record files the sealed tooling writes under the private root (paths relative to it, one `*` = one
# path segment): a4 (src/sealed_ledger.py) the private ledger copy and its payloads/ directory (one <name>.json each),
# a7 (scripts/sp_launch.py) the data and probe dispatch logs. They hold ids, hashes, counts and enums only, so they are
# NOT needle sources (never passed to load_sealed or the set loaders) and the pre-check allows them, .json payloads
# included; the merge gate and --output still scan them as TARGETS, so question text that leaks into one is a hit.
RECORD_FILES = (
    "sealed/ledger/sealed_ledger.jsonl",
    "sealed/ledger/payloads/*.json",
    "sp_dispatch.jsonl",
    "sp_probe.jsonl",
)

# A JSON string's delimiters: a quote mark, with or without the backslashes of a
# nested (escaped) serialisation in front of it.
_QUOTE_SPLIT = re.compile(r'\\*"')

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ESCAPE_RE = re.compile(
    r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2})|([nrtbf\"\\/']))"
)
# Anything that is not a letter or digit (punctuation, symbols, underscore,
# whitespace) separates tokens.
_NON_WORD = re.compile(r"[\W_]+")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": " ", "f": " ", '"': '"', "\\": "\\", "/": "/", "'": "'"}

GhRunner = Callable[[List[str]], str]


class ScanRefusal(Exception):
    """The scan cannot run as asked. ``code`` is the CLI exit; ``paths`` are safe to print."""

    def __init__(self, code: int, reason: str, paths: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.paths = list(paths)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def _decode_once(text: str) -> str:
    """Decode one level of backslash escapes (JSON, ``\\uXXXX``, ``\\xXX``)."""

    def repl(m: "re.Match[str]") -> str:
        if m.group(1) is not None:
            return chr(int(m.group(1), 16))
        if m.group(2) is not None:
            code = int(m.group(2), 16)
            return chr(code) if code <= 0x10FFFF else " "
        if m.group(3) is not None:
            return chr(int(m.group(3), 16))
        return _SIMPLE_ESCAPES[m.group(4)]

    out = _ESCAPE_RE.sub(repl, text)
    # Join UTF-16 surrogate pairs produced by \uD83D\uDE00-style escapes.
    return out.encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")


def decode_escapes(text: str) -> str:
    """Decode backslash escapes up to four levels deep, then HTML entities."""
    for _ in range(4):
        if "\\" not in text:
            break
        decoded = _decode_once(text)
        if decoded == text:
            break
        text = decoded
    return html.unescape(text) if "&" in text else text


def normalise_tokens(text: str) -> List[str]:
    """Normalise ``text`` (escapes, NFKC, casefold, punctuation -> space) into tokens."""
    text = decode_escapes(text)
    text = unicodedata.normalize("NFKC", text)
    text = unicodedata.normalize("NFKC", text.casefold())
    # Format characters (zero-width space/joiner, BOM, soft hyphen) are
    # invisible but would split a token; delete them so a needle cannot slip by.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return _NON_WORD.sub(" ", text).split()


def needle_id(tokens: Sequence[str]) -> str:
    """Opaque needle id: ``n`` + 10 hex of sha256 of the normalised needle text."""
    return "n" + hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class NeedleSource:
    """One question file whose rows become needles; ``label`` is printed on hits.

    ``sealed`` sources are read only through ``load_sealed`` and become
    S-needles; their ``label`` is never printed (an S-hit names a family).
    """

    path: Path
    label: str
    sealed: bool = False


@dataclass
class NeedleIndex:
    """Non-exempt needles, keyed by token tuple.

    ``whole``/``windows``/``long_by_prefix``/``short_lengths`` hold the private
    needles. The ``s_*`` fields hold the S-needles (sealed questions); their
    values are family ids, never needle ids.
    """

    whole: Dict[Tuple[str, ...], Tuple[str, str]] = field(default_factory=dict)
    windows: Dict[Tuple[str, ...], Tuple[str, str]] = field(default_factory=dict)
    # whole needles of >= WINDOW tokens, indexed by their first window
    long_by_prefix: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = field(default_factory=dict)
    short_lengths: Set[int] = field(default_factory=set)
    source_shas: Set[str] = field(default_factory=set)
    exempt_whole: int = 0
    exempt_windows: int = 0
    # S-needles: sealed questions of >= WINDOW tokens (whole + windows, matched in the token stream) ...
    s_whole: Dict[Tuple[str, ...], str] = field(default_factory=dict)
    s_windows: Dict[Tuple[str, ...], str] = field(default_factory=dict)
    s_long_by_prefix: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = field(default_factory=dict)
    # ... and sealed questions under WINDOW tokens: no window needle, and no match inside running text; only a
    # whole JSON string value or a whole line equal to the question counts (:func:`_short_sealed_hits`).
    s_short: Dict[Tuple[str, ...], str] = field(default_factory=dict)
    s_exempt_whole: int = 0
    s_exempt_windows: int = 0

    def __len__(self) -> int:
        return len(self.whole) + len(self.windows) + len(self.s_whole) + len(self.s_windows) + len(self.s_short)


@dataclass(frozen=True)
class Hit:
    """One finding: where, which needle (opaque id), kind and source. Never text.

    An S-hit (a sealed needle) has ``kind == "S"`` and carries the family's
    opaque id in ``family``; it has no needle id and no source, and
    :meth:`line` prints the target and the family id only.
    """

    target: str
    needle: str
    kind: str
    source: str
    family: Optional[str] = None

    def line(self) -> str:
        """The printable hit line."""
        if self.family is not None:
            return f"{self.target}\t{self.family}"
        return f"{self.target}\t{self.needle}\t{self.kind}\t{self.source}"


def _repo_base() -> Path:
    """The directory that holds ``eval/private`` (relative inputs.json paths resolve here)."""
    return eval_privacy.private_root().parent.parent


def _resolve_recorded(path_str: str) -> Path:
    """Resolve a path recorded in an ``inputs.json`` (relative -> against the repo base)."""
    p = Path(path_str)
    return p if p.is_absolute() else _repo_base() / p


def _registry_public_shas() -> Set[str]:
    return {e.sha256 for e in eval_sets.public_entries()}


def _legacy_shas() -> Set[str]:
    return {e["sha256"] for e in eval_sets.load_legacy_public()}


def _area(rel: Path) -> Optional[str]:
    """The traced area a path (relative to the private root) sits in, if any.

    ``runs``/``artifacts`` (also under ``sealed/``) are traced through
    ``inputs.json`` rather than scanned as sources; ``work`` (also under
    ``sealed/``) holds scratch files the pre-check does not look into.
    """
    parts = rel.parts
    for name in ("runs", "artifacts", "work"):
        if parts[:1] == (name,) or parts[:2] == ("sealed", name):
            return name
    return None


def _is_record(rel: Path) -> bool:
    """True when ``rel`` (relative to the private root) is one of the :data:`RECORD_FILES`."""
    posix = rel.as_posix()
    return any(len(pat.split("/")) == len(rel.parts) and fnmatch.fnmatchcase(posix, pat) for pat in RECORD_FILES)


def _record_files() -> List[Path]:
    """Every regular (non-symlink) record file under the private root."""
    root = eval_privacy.private_root()
    if not root.is_dir():
        return []
    return [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink() and _is_record(p.relative_to(root))
    ]


def _private_jsonl_files() -> List[Path]:
    """Every regular ``*.jsonl`` under the private root outside ``runs/`` and ``artifacts/``.

    Record files (:data:`RECORD_FILES`) are not sources. A symlink is skipped
    here and left to the pre-check (a link to a file that is not itself a
    source is refused there).
    """
    root = eval_privacy.private_root()
    if not root.is_dir():
        return []
    found: List[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() != ".jsonl" or path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if _area(rel) in ("runs", "artifacts") or _is_record(rel):
            continue
        found.append(path)
    return found


def _run_inputs(run_id: str) -> List[Dict[str, Any]]:
    """Parse ``runs/<id>/inputs.json`` and ``sealed/runs/<id>/inputs.json`` (refusal exit 2 if neither
    exists or one is malformed)."""
    candidates = [
        eval_privacy.run_dir(run_id, create=False) / "inputs.json",
        eval_privacy.sealed_run_dir(run_id, create=False) / "inputs.json",
    ]
    present = [p for p in candidates if p.is_file()] or candidates[:1]
    items: List[Dict[str, Any]] = []
    for path in present:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            inputs = data["inputs"]
            if data.get("version") != 1 or not isinstance(inputs, list):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ScanRefusal(EXIT_USAGE, "run inputs.json missing or malformed", [str(path)])
        items.extend(i for i in inputs if isinstance(i, dict))
    return items


def collect_sources(
    needles: Sequence[Any] = (),
    *,
    run_id: Optional[str] = None,
) -> List[NeedleSource]:
    """Gather the needle sources.

    In order: the registry's private sets, each ``--needles`` path, every
    ``*.jsonl`` under the private root outside ``runs/`` and ``artifacts/``
    (automatic, sealed ones included), then the run's inputs. A file reached
    twice is one source (first label wins). A source that is sealed is flagged
    so :func:`build_needles` reads it through ``load_sealed`` only.

    Raises:
        ScanRefusal: exit 2 for a public ``--needles`` path, a non-``.jsonl``
            one, a missing source or a drifted run input.
    """
    sources: List[NeedleSource] = []
    seen: Set[Path] = set()

    def add(path: Path, label: str) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            sources.append(NeedleSource(path, label, eval_sets.is_sealed(path)))

    for entry in eval_sets.private_needle_sets():
        path = entry.resolved()
        if not path.is_file():
            raise ScanRefusal(EXIT_USAGE, "registered private set missing", [entry.name])
        add(path, entry.name)
    for raw in needles:
        path = Path(raw)
        if not path.is_file():
            raise ScanRefusal(EXIT_USAGE, "--needles file not found", [str(raw)])
        if eval_sets.classify(path) == PUBLIC:
            raise ScanRefusal(EXIT_USAGE, "public source refused: --needles must classify private", [str(raw)])
        if path.suffix.lower() != ".jsonl":
            raise ScanRefusal(EXIT_USAGE, "--needles must be a v1 or v2 eval .jsonl", [str(raw)])
        add(path, str(raw))
    root = eval_privacy.private_root()
    for path in _private_jsonl_files():
        add(path, "eval/private/" + path.relative_to(root).as_posix())
    if run_id is not None:
        no_needles = _registry_public_shas() | _legacy_shas()
        recorded: List[Dict[str, Any]] = []
        for item in _run_inputs(run_id):
            if item.get("kind") == "questions":
                recorded.append(item)
            elif item.get("kind") == "derived":
                srcs = item.get("sources")
                recorded.extend(s for s in (srcs if isinstance(srcs, list) else []) if isinstance(s, dict))
        for item in recorded:
            sha = str(item.get("sha256", ""))
            rec_path = str(item.get("path", ""))
            if sha in no_needles:
                continue
            path = _resolve_recorded(rec_path)
            if not path.is_file():
                raise ScanRefusal(EXIT_USAGE, "run input missing", [rec_path])
            if eval_sets.sha256_file(path) != sha:
                raise ScanRefusal(EXIT_USAGE, "run input changed since the run", [rec_path])
            add(path, rec_path)
    return sources


def _iter_questions(path: Path) -> Iterator[str]:
    """Yield every row's ``question`` from a ``.jsonl`` (or a ``.json`` list) source.

    Raises:
        ScanRefusal: exit 2 for an unreadable file or an unparseable line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ScanRefusal(EXIT_USAGE, "needle source unreadable", [str(path)])
    if path.suffix.lower() == ".json":
        try:
            rows = json.loads(text)
        except ValueError:
            raise ScanRefusal(EXIT_USAGE, "needle source does not parse", [str(path)])
        rows = rows if isinstance(rows, list) else [rows]
    else:
        rows = []
        # Split on "\n" only: str.splitlines() also breaks on U+2028, U+0085,
        # \x0b, \x0c and \x1c-\x1e, which may sit raw inside a JSON string.
        for lineno, line in enumerate(text.split("\n"), 1):
            if line.endswith("\r"):
                line = line[:-1]
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise ScanRefusal(EXIT_USAGE, "needle source line does not parse", [f"{path}:{lineno}"])
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("question"), str):
            yield row["question"]


def _public_questions() -> Tuple[Set[str], Set[Tuple[str, ...]]]:
    """Normalised-text sha256s and 8-token windows of every registered public question."""
    hashes: Set[str] = set()
    windows: Set[Tuple[str, ...]] = set()
    for entry in eval_sets.public_entries():
        path = entry.resolved()
        if not path.is_file() or eval_sets.sha256_file(path) != entry.sha256:
            continue  # drifted or absent: grants nothing
        try:
            questions = list(_iter_questions(path))
        except ScanRefusal:
            continue
        for q in questions:
            toks = normalise_tokens(q)
            if not toks:
                continue
            hashes.add(hashlib.sha256(" ".join(toks).encode("utf-8")).hexdigest())
            for i in range(len(toks) - WINDOW + 1):
                windows.add(tuple(toks[i : i + WINDOW]))
    return hashes, windows


def _iter_sealed(path: Path) -> Iterator[Tuple[str, str]]:
    """Yield ``(family_id, question)`` for every row of a sealed source.

    The one read goes through :func:`src.eval_sealed_read.load_sealed` (full
    schema validation: ``sealed`` must be ``true``; a sealed set that does not
    validate is not scanned half-way). Errors are ``(line, field, reason)``
    triples with fixed reasons, safe to print.

    Raises:
        ScanRefusal: exit 2 for an unreadable file, a schema or inventory error,
            or a path the loader does not accept as sealed.
    """
    try:
        rows = load_sealed(path)
    except SchemaError as exc:
        raise ScanRefusal(
            EXIT_USAGE,
            "sealed source does not validate",
            [str(path)] + [f"  {line}" for line in render_errors(exc.errors).splitlines()],
        )
    except NotSealedInput:
        raise ScanRefusal(EXIT_USAGE, "source is not sealed", [str(path)])
    except (OSError, UnicodeDecodeError):
        raise ScanRefusal(EXIT_USAGE, "needle source unreadable", [str(path)])
    for row in rows:
        yield str(row["family_id"]), str(row["question"])


def _handbook_windows(dump: Path, wanted: Set[Tuple[str, ...]]) -> Optional[Set[Tuple[str, ...]]]:
    """The members of ``wanted`` that occur as 8 consecutive tokens in the handbook dump.

    The dump is a JSONL file (one JSON object per line, the section text under
    ``text``). Streaming it against the sealed windows keeps the handbook out of
    memory beyond one section and out of every output. A line that does not
    parse, or a row without a string ``text``, contributes nothing (a smaller
    exemption only means more S-hits).

    Returns:
        ``None`` if the dump is absent or unreadable, else the matching windows.
    """
    try:
        raw = dump.read_bytes().decode("utf-8", errors="replace") if dump.is_file() else None
    except OSError:
        raw = None
    if raw is None:
        return None
    found: Set[Tuple[str, ...]] = set()
    if not wanted:
        return found
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        text = row.get("text") if isinstance(row, dict) else None
        if not isinstance(text, str):
            continue
        toks = normalise_tokens(text)
        for i in range(len(toks) - WINDOW + 1):
            win = tuple(toks[i : i + WINDOW])
            if win in wanted:
                found.add(win)
    return found


def _add_sealed_needles(
    index: NeedleIndex,
    rows: Sequence[Tuple[str, Tuple[str, ...]]],
    public_hashes: Set[str],
    exempt: Set[Tuple[str, ...]],
    handbook_dump: Optional[Path],
    notes: Optional[List[str]],
) -> None:
    """Add the S-needles of ``rows`` (``(family id, tokens)``) to ``index``.

    ``exempt`` holds the public and development windows; the handbook's are
    added here from ``handbook_dump``.
    """
    candidates = {
        tuple(toks[i : i + WINDOW]) for _, toks in rows for i in range(len(toks) - WINDOW + 1)
    }
    exempt = set(exempt)
    dump = handbook_dump if handbook_dump is not None else _repo_base() / HANDBOOK_DUMP
    from_handbook = _handbook_windows(dump, candidates - exempt)
    if from_handbook is None:
        if notes is not None:
            notes.append("handbook dump absent or unreadable: handbook exemption is empty")
    else:
        exempt |= from_handbook
    for family, toks in rows:
        whole_exempt = hashlib.sha256(" ".join(toks).encode("utf-8")).hexdigest() in public_hashes
        if len(toks) < WINDOW:
            # No window needle. Only a whole JSON string value or a whole line counts.
            if whole_exempt:
                index.s_exempt_whole += 1
            else:
                index.s_short.setdefault(toks, family)
            continue
        if whole_exempt:
            index.s_exempt_whole += 1
        elif toks not in index.s_whole:
            index.s_whole[toks] = family
            index.s_long_by_prefix.setdefault(toks[:WINDOW], []).append(toks)
        for i in range(len(toks) - WINDOW + 1):
            win = toks[i : i + WINDOW]
            if win in exempt:
                index.s_exempt_windows += 1
            else:
                index.s_windows.setdefault(win, family)


def build_needles(
    sources: Sequence[NeedleSource],
    *,
    handbook_dump: Optional[Any] = None,
    notes: Optional[List[str]] = None,
) -> NeedleIndex:
    """Build the needle index from ``sources``, minus exemptions.

    Private sources are exempt against registered public questions only (as in
    16A-1). Sealed sources become S-needles whose windows are exempt against
    public questions, development questions (every non-sealed source) and the
    handbook text in ``handbook_dump`` (default: ``data/research/handbook_sections.jsonl``
    under the repo base). An absent dump adds a note and an empty handbook
    exemption.

    Raises:
        ScanRefusal: exit 2 for an unreadable or invalid source.
    """
    index = NeedleIndex()
    if not sources:
        return index
    public_hashes, public_windows = _public_questions()
    development: Set[Tuple[str, ...]] = set()
    sealed_rows: List[Tuple[str, Tuple[str, ...]]] = []
    for src in sources:
        index.source_shas.add(eval_sets.sha256_file(src.path))
        if src.sealed:
            for family, q in _iter_sealed(src.path):
                toks = tuple(normalise_tokens(q))
                if toks:
                    sealed_rows.append((family, toks))
            continue
        for q in _iter_questions(src.path):
            toks = tuple(normalise_tokens(q))
            if not toks:
                continue
            nid = needle_id(toks)
            if hashlib.sha256(" ".join(toks).encode("utf-8")).hexdigest() in public_hashes:
                index.exempt_whole += 1
            elif toks not in index.whole:
                index.whole[toks] = (nid, src.label)
                if len(toks) >= WINDOW:
                    index.long_by_prefix.setdefault(toks[:WINDOW], []).append(toks)
                else:
                    index.short_lengths.add(len(toks))
            for i in range(len(toks) - WINDOW + 1):
                win = toks[i : i + WINDOW]
                development.add(win)
                if win in public_windows:
                    index.exempt_windows += 1
                elif win not in index.windows:
                    index.windows[win] = (needle_id(win), src.label)
    if sealed_rows:
        _add_sealed_needles(
            index,
            sealed_rows,
            public_hashes,
            public_windows | development,
            Path(handbook_dump) if handbook_dump is not None else None,
            notes,
        )
    return index


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------
def _tokens_with_lines(text: str) -> Tuple[List[str], List[int]]:
    tokens: List[str] = []
    lines: List[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for tok in normalise_tokens(line):
            tokens.append(tok)
            lines.append(lineno)
    return tokens, lines


def _stream_matches(
    tokens: Sequence[str],
    whole: Dict[Tuple[str, ...], Any],
    windows: Dict[Tuple[str, ...], Any],
    long_by_prefix: Dict[Tuple[str, ...], List[Tuple[str, ...]]],
    short_lengths: Iterable[int],
) -> List[Tuple[int, int, str, Any]]:
    """Folded matches ``(start, end, kind, value)`` of one needle family in a token stream.

    ``value`` is whatever the needle maps carry (``(id, source)`` for private
    needles, a family id for S-needles). A window inside a whole match's span
    is folded into the whole match.
    """
    n = len(tokens)
    found: List[Tuple[int, int, str, Any]] = []
    for i in range(n):
        if i + WINDOW <= n:
            win = tuple(tokens[i : i + WINDOW])
            for needle in long_by_prefix.get(win, ()):
                if tuple(tokens[i : i + len(needle)]) == needle:
                    found.append((i, i + len(needle), "whole", whole[needle]))
            if win in windows:
                found.append((i, i + WINDOW, "window", windows[win]))
        for length in short_lengths:
            if i + length <= n:
                seq = tuple(tokens[i : i + length])
                if seq in whole:
                    found.append((i, i + length, "whole", whole[seq]))
    spans = [(s, e) for s, e, k, _ in found if k == "whole"]
    return [m for m in sorted(found) if not (m[2] == "window" and any(s <= m[0] and m[1] <= e for s, e in spans))]


def _short_sealed_hits(text: str, target: str, index: NeedleIndex) -> List[Hit]:
    """S-hits for sealed questions under 8 tokens: a whole JSON string value or a whole line.

    A line matches when its whole normalised token list equals the question's.
    A JSON string value matches when the text between two adjacent quote marks
    (an escaped quote, ``\\"``, included, so a nested serialisation counts) does.
    Residual: such a question embedded in prose, a numbered item or an unquoted
    ``key: value`` line is missed; with no window needle there is nothing to
    match a fragment against.
    """
    if not index.s_short:
        return []
    hits: List[Hit] = []
    seen: Set[Tuple[int, str]] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        candidates = [line]
        parts = _QUOTE_SPLIT.split(line)
        candidates.extend(parts[1:-1])  # only text with a quote on both sides is a string value
        for candidate in candidates:
            family = index.s_short.get(tuple(normalise_tokens(candidate)))
            if family is not None and (lineno, family) not in seen:
                seen.add((lineno, family))
                hits.append(Hit(f"{target}:{lineno}", "", "S", "", family))
    return hits


def scan_text(text: str, target: str, index: NeedleIndex) -> List[Hit]:
    """Scan one target's text; hits are labelled ``<target>:<line>``.

    Matching is on whole tokens over the normalised token stream of the
    whole text, so a needle wrapped across lines is still caught. S-needles
    (sealed questions) of 8 or more tokens match the same way and yield S-hits
    (family id, no needle id or source); under 8 tokens see
    :func:`_short_sealed_hits`.
    """
    if not index or not text:
        return []
    tokens, lines = _tokens_with_lines(text)
    hits: List[Hit] = []
    seen: Set[Tuple[str, Any]] = set()
    if index.whole or index.windows:
        for start, _end, kind, (nid, src) in _stream_matches(
            tokens, index.whole, index.windows, index.long_by_prefix, index.short_lengths
        ):
            label = f"{target}:{lines[start]}"
            if (label, nid) not in seen:
                seen.add((label, nid))
                hits.append(Hit(label, nid, kind, src))
    if index.s_whole or index.s_windows:
        for start, _end, _kind, family in _stream_matches(
            tokens, index.s_whole, index.s_windows, index.s_long_by_prefix, ()
        ):
            label = f"{target}:{lines[start]}"
            if (label, family) not in seen:
                seen.add((label, family))
                hits.append(Hit(label, "", "S", "", family))
    for hit in _short_sealed_hits(text, target, index):
        if (hit.target, hit.family) not in seen:
            seen.add((hit.target, hit.family))
            hits.append(hit)
    return hits


def scan_output(
    files: Sequence[Any],
    *,
    run_id: Optional[str] = None,
    needles: Sequence[Any] = (),
    handbook_dump: Optional[Any] = None,
    notes: Optional[List[str]] = None,
) -> List[Hit]:
    """``--output`` mode: scan a private run's files before release.

    Raises:
        ScanRefusal: as :func:`collect_sources`, or exit 2 for an unreadable file.
    """
    index = build_needles(collect_sources(needles, run_id=run_id), handbook_dump=handbook_dump, notes=notes)
    hits: List[Hit] = []
    for f in files:
        try:
            text = Path(f).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            raise ScanRefusal(EXIT_USAGE, "output file unreadable", [str(f)])
        hits.extend(scan_text(text, str(f), index))
    return hits


# --------------------------------------------------------------------------
# Merge gate
# --------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ScanRefusal(EXIT_USAGE, "git command failed: git " + " ".join(args[:2]))
    return proc.stdout


def _cat_blobs(repo: Path, shas: Sequence[str]) -> Dict[str, bytes]:
    """Read many blobs through one ``git cat-file --batch`` process."""
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ScanRefusal(EXIT_USAGE, "git cat-file failed")
    out: Dict[str, bytes] = {}
    data, pos = proc.stdout, 0
    for sha in shas:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[sha] = data[pos : pos + size]
        pos += size + 1
    return out


def _object_messages(raw: bytes) -> List[str]:
    """Every decoding of a raw commit or tag object's message (the bytes after its header block).

    Git never transcodes a message to match its ``encoding`` header, so the
    header is only a claim: a UTF-16 or punycode header on UTF-8 bytes would
    turn a needle into noise (final gate, D2). The raw bytes are therefore
    always decoded as UTF-8; when the header names a codec that can decode
    them, that decoding is scanned too, and a hit in either counts.
    Undecodable bytes are replaced, never dropped.
    """
    header, _, body = raw.partition(b"\n\n")
    texts = [body.decode("utf-8", errors="replace")]
    for line in header.split(b"\n"):
        if line.startswith(b"encoding "):
            encoding = line[len(b"encoding "):].decode("ascii", errors="replace").strip()
            try:
                declared = body.decode(encoding, errors="replace")
            except (LookupError, ValueError):  # unknown, non-text or strict-only codec
                continue
            if declared not in texts:
                texts.append(declared)
    return texts


def _scan_message(raw: bytes, target: str, index: NeedleIndex) -> List[Hit]:
    """:func:`scan_text` over every decoding of one message; a hit is listed once."""
    hits: List[Hit] = []
    for text in _object_messages(raw):
        hits.extend(h for h in scan_text(text, target, index) if h not in hits)
    return hits


def _range_blobs(repo: Path, base: str) -> List[Tuple[str, str, str]]:
    """(blob sha, path, commit) for every blob added or modified in ``base..HEAD``."""
    commits = _git(repo, "rev-list", f"{base}..HEAD").decode().split()
    found: List[Tuple[str, str, str]] = []
    for commit in commits:
        raw = _git(repo, "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--no-renames", "-z", commit)
        parts = raw.split(b"\0")
        i = 0
        while i + 1 < len(parts):
            meta = parts[i].decode(errors="replace")
            if not meta.startswith(":"):
                i += 1
                continue
            fields = meta[1:].split()
            path = parts[i + 1].decode(errors="replace")
            i += 2
            if len(fields) < 5:
                continue
            new_mode, new_sha, status = fields[1], fields[3], fields[4]
            if status[0] in "AMT" and set(new_sha) != {"0"} and new_mode != "160000":
                found.append((new_sha, path, commit))
    return found


def _head_blobs(repo: Path) -> List[Tuple[str, str]]:
    """(blob sha, path) for every tracked file at HEAD."""
    raw = _git(repo, "ls-tree", "-r", "-z", "HEAD")
    out = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        if kind == "blob":
            out.append((sha, path.decode(errors="replace")))
    return out


def _default_gh_runner(gh: str, repo: Path) -> GhRunner:
    def run(args: List[str]) -> str:
        proc = subprocess.run([gh, *args], cwd=str(repo), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ScanRefusal(EXIT_USAGE, "gh failed; PR items could not be read")
        return proc.stdout

    return run


def _json_values(text: str) -> List[Any]:
    """Parse one or more concatenated JSON documents (``gh api --paginate`` output)."""
    dec = json.JSONDecoder()
    out, pos, text = [], 0, text.strip()
    while pos < len(text):
        obj, end = dec.raw_decode(text, pos)
        out.append(obj)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


def _pr_items(pr: int, gh_runner: GhRunner) -> List[Tuple[str, str]]:
    """(label, text) for the PR body, comments, review bodies and review comments."""
    items: List[Tuple[str, str]] = []
    try:
        view = json.loads(gh_runner(["pr", "view", str(pr), "--json", "title,body,comments,reviews"]))
        review_comments: List[Any] = []
        for page in _json_values(gh_runner(["api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{pr}/comments"])):
            review_comments.extend(page if isinstance(page, list) else [page])
    except ScanRefusal:
        raise
    except (ValueError, TypeError, OSError):
        raise ScanRefusal(EXIT_USAGE, "gh output could not be parsed; PR items not read")
    items.append((f"PR#{pr} title", str(view.get("title") or "")))
    items.append((f"PR#{pr} body", str(view.get("body") or "")))
    for i, c in enumerate(view.get("comments") or []):
        items.append((f"PR#{pr} comment[{i}]", str((c or {}).get("body") or "")))
    for i, r in enumerate(view.get("reviews") or []):
        items.append((f"PR#{pr} review[{i}]", str((r or {}).get("body") or "")))
    for i, c in enumerate(review_comments):
        body = c.get("body") if isinstance(c, dict) else None
        items.append((f"PR#{pr} review-comment[{i}]", str(body or "")))
    return items


def _load_json(path: Path) -> Any:
    """Parsed JSON at ``path``, or ``None`` if unreadable, not JSON, or carrying
    a duplicate key (``eval_sets.loads_no_duplicates``). Callers treat ``None``
    as "cannot be checked", i.e. fail closed (16A-1 gate round 7)."""
    try:
        return eval_sets.loads_no_duplicates(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None


def _sha_or_empty(path: Path) -> str:
    """sha256 of a regular file, or ``""`` if it is missing or unreadable."""
    try:
        return eval_sets.sha256_file(path) if path.is_file() else ""
    except OSError:
        return ""


def _is_sha_keyed_artifact(path: Path) -> bool:
    """A schema-valid expansion artifact (``src.expansion_artifact``).

    The artifact schema is exact (no extra keys at any level; entries carry
    only ``question_sha256``, ``rewrites``, ``intent`` and ``status``), so a
    file cannot hide question text under another key and still pass (16A-1
    gate round 6). Where it lives does not matter (round 5): a public-built
    artifact replayed in a private run is as content-free as one under
    ``eval/private/artifacts/``.
    """
    from src.expansion_artifact import ExpansionArtifactError, _validate

    if not path.is_file():
        return False
    doc = _load_json(path)
    try:
        _validate(doc)
    except ExpansionArtifactError:
        return False
    return True


def _has_entries(path: Path) -> bool:
    """True when ``path`` is a JSON object with an ``entries`` key (artifact-shaped)."""
    doc = _load_json(path)
    return isinstance(doc, dict) and "entries" in doc


def _sources_consistent(listed: Sequence[Tuple[str, str]]) -> bool:
    """Every recorded source that exists on disk hashes to its recorded sha256."""
    for s_path, s_sha in listed:
        resolved = _resolve_recorded(s_path)
        if resolved.exists() and _sha_or_empty(resolved) != s_sha:
            return False
    return True


def _content_checkable(path: Path) -> bool:
    """A traced derived file's content can be trusted to its sources.

    A ``.md`` report has no structure to check (its text is trusted to its
    recorded sources: a disclosed residual, D65). Anything else must parse as
    duplicate-free JSON (a ``.jsonl``, a BOM, a truncated or duplicate-keyed
    file fails closed), and if it holds a ``question``/``questions`` key it must
    classify public (rule 5 checks every question string). Gate round 7.
    """
    if path.suffix.lower() == ".md":
        return True
    doc = _load_json(path)
    if doc is None:
        return False
    return not eval_sets._has_question_key(doc) or eval_sets.classify(path) == PUBLIC


def _source_file(s_path: str, s_sha: str) -> Optional[Path]:
    """The question set an artifact header names: its recorded path, else any
    registered set at that sha256; ``None`` if no file with that sha is found."""
    candidates = [_resolve_recorded(s_path)]
    try:
        candidates += [e.resolved() for e in eval_sets.load_registry() if e.sha256 == s_sha]
    except Exception:  # noqa: BLE001 - an unreadable registry just means fewer candidates
        pass
    for cand in candidates:
        if _sha_or_empty(cand) == s_sha:
            return cand
    return None


def _bound_keys(src: Path) -> List[Tuple[str, str]]:
    """``(question, artifact row key)`` for every row of one artifact source.

    A sealed source is read through ``load_sealed`` with the schema-2 key of
    ``evaluator._artifact_keys`` (``<set sha256[:16]>/<row id>``), since the
    evaluator's own loaders refuse sealed input; anything else uses
    ``evaluator._artifact_keys``.
    """
    if eval_sets.is_sealed(src):
        prefix = eval_sets.sha256_file(src)[:16]
        return [(r["question"], f"{prefix}/{r['id']}") for r in load_sealed(src)]
    from src.evaluator import _artifact_keys

    return _artifact_keys(str(src))


def _artifact_ok(path: Path, ok_source: Set[str]) -> bool:
    """A schema-valid artifact whose every entry is bound to a real row.

    Every header source must be approved and found on disk at its sha256, and
    every entry key and ``question_sha256`` must be exactly the key and hash
    of a row of those sets (``evaluator._artifact_keys``; sealed sets through
    ``load_sealed``). Free text can then sit only in the rewrites, intent and
    model of real rows (gate round 7).
    """
    if not _is_sha_keyed_artifact(path):
        return False
    header = _artifact_header_shas(path)
    if not header or not all(h_sha in ok_source for _, h_sha in header):
        return False
    from src.expansion_artifact import question_sha256

    expected: Dict[str, str] = {}
    for h_path, h_sha in header:
        src = _source_file(h_path, h_sha)
        if src is None:
            return False
        try:
            for question, key in _bound_keys(src):
                expected[key] = question_sha256(question)
        except Exception:  # noqa: BLE001 - an unloadable source cannot bind entries
            return False
    doc = _load_json(path)
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        return False
    return all(
        isinstance(e, dict) and expected.get(key) == e.get("question_sha256")
        for key, e in entries.items()
    )


def _artifact_header_shas(path: Path) -> List[Tuple[str, str]]:
    doc = _load_json(path)
    out = []
    for item in (doc.get("inputs") or []) if isinstance(doc, dict) else []:
        if isinstance(item, dict):
            out.append((str(item.get("path", "")), str(item.get("sha256", ""))))
    return out


def precheck(source_shas: Set[str]) -> List[str]:
    """The merge gate's source pre-check: the offending paths (empty = pass)."""
    root = eval_privacy.private_root()
    offenders: List[str] = []
    if not root.exists():
        return offenders
    public, legacy = _registry_public_shas(), _legacy_shas()
    ok_source = source_shas | public | legacy
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        rel = path.relative_to(root)
        area = _area(rel)
        if area in ("runs", "work"):
            continue  # runs are traced through inputs.json below; work/ is exempt (16A-2a a2)
        if _is_record(rel) and not path.is_symlink():
            continue  # a content-free record file (RECORD_FILES): not a source, not an offender
        if area == "artifacts":
            # Every file here must be a schema-valid artifact built only from
            # approved sources, referenced by a run or not (an interrupted
            # build leaves one unreferenced; 16A-1 gate round 6).
            if not _artifact_ok(path, ok_source):
                offenders.append(str(path))
            continue
        # Anywhere else only a .jsonl needle source is allowed (16A-2a a2): every
        # regular *.jsonl here is one automatically, so this refuses other file
        # types and a symlink to a file that is not itself a source.
        try:
            sha = eval_sets.sha256_file(path)
        except OSError:
            sha = ""
        if path.suffix.lower() != ".jsonl" or sha not in source_shas:
            offenders.append(str(path))
    run_inputs = [
        p
        for runs in (root / "runs", root / "sealed" / "runs")
        if runs.is_dir()
        for p in sorted(runs.glob("*/inputs.json"))
    ]
    for inputs_json in run_inputs:
        try:
            data = json.loads(inputs_json.read_text(encoding="utf-8"))
            items = data["inputs"]
            if not isinstance(items, list):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            offenders.append(str(inputs_json))
            continue
        for item in items:
            if not isinstance(item, dict):
                offenders.append(str(inputs_json))
                continue
            kind, sha, rec_path = item.get("kind"), str(item.get("sha256", "")), str(item.get("path", ""))
            if kind == "questions":
                # A registered PUBLIC set is not private material: it needs no
                # needle source (and cannot be one). Same rule as
                # collect_sources(run_id=...) (16A-1 gate round 4).
                if sha not in source_shas and sha not in public:
                    offenders.append(f"{inputs_json} -> {rec_path}")
                continue
            if kind != "derived":
                offenders.append(f"{inputs_json} -> {rec_path}")
                continue
            raw_sources = item.get("sources")
            if raw_sources is not None and not isinstance(raw_sources, list):
                offenders.append(f"{inputs_json} -> {rec_path}")
                continue
            listed = [
                (str(s.get("path", "")), str(s.get("sha256", "")))
                for s in raw_sources or []
                if isinstance(s, dict)
            ]
            resolved = _resolve_recorded(rec_path)
            exempt = sha in legacy
            # The recorded file must exist with the recorded sha256 for any
            # check of its content (and so any acceptance by tracing) to mean
            # anything; a missing or drifted file is accepted only as a needle
            # source or a legacy entry (16A-1 gate round 6).
            present = _sha_or_empty(resolved) == sha
            traced = False
            if not exempt and present:
                if _is_sha_keyed_artifact(resolved):
                    if not _artifact_ok(resolved, ok_source):
                        offenders.append(f"{inputs_json} -> {rec_path}")
                        continue
                    exempt = True
                    listed += _artifact_header_shas(resolved)
                elif _has_entries(resolved):
                    # artifact-shaped but not a valid artifact: never accepted,
                    # however its sources trace
                    offenders.append(f"{inputs_json} -> {rec_path}")
                    continue
                else:
                    # A derived input whose every recorded source is approved
                    # (needle, registered public or legacy) and consistent on
                    # disk holds only text from those sources, which the needle
                    # scan covers (round 5: bakeoff arm reports, sidecars and
                    # w_sweep dumps). A file with question keys must also
                    # classify public (rule 5 checks every question string).
                    traced = (
                        bool(listed)
                        and all(s_sha in ok_source for _, s_sha in listed)
                        and _sources_consistent(listed)
                        and _content_checkable(resolved)
                    )
            if not exempt and not traced and sha not in source_shas:
                offenders.append(f"{inputs_json} -> {rec_path}")
            for s_path, s_sha in listed:
                if s_sha not in ok_source:
                    offenders.append(f"{inputs_json} -> {rec_path} -> {s_path}")
    return offenders


def merge_gate(
    base: str,
    *,
    repo: Optional[Any] = None,
    pr: Optional[int] = None,
    needles: Sequence[Any] = (),
    gh_runner: Optional[GhRunner] = None,
    gh: str = "gh",
    notes: Optional[List[str]] = None,
    handbook_dump: Optional[Any] = None,
) -> List[Hit]:
    """``--merge-gate`` mode: pre-check the sources, then scan everything a push would publish.

    Args:
        base: the base ref; ``base..HEAD`` is the range.
        repo: the git work tree (default: this repo).
        pr: the PR number whose body, comments and review comments to scan.
        needles: extra ``--needles`` sources.
        gh_runner: callable taking ``gh`` arguments and returning stdout (tests fake it).
        gh: the ``gh`` executable used when ``gh_runner`` is not given.
        notes: if given, receives human notes (never content).
        handbook_dump: the handbook JSONL dump for the S-needle exemption
            (default ``data/research/handbook_sections.jsonl`` under the repo base).

    Raises:
        ScanRefusal: exit 7 when the pre-check refuses (paths only); else as
            :func:`collect_sources`, or exit 2 on a git/gh failure.
    """
    repo_path = Path(repo) if repo is not None else REPO_ROOT
    notes = notes if notes is not None else []
    sources = collect_sources(needles)
    index = build_needles(sources, handbook_dump=handbook_dump, notes=notes)
    offenders = precheck(index.source_shas)
    if offenders:
        raise ScanRefusal(EXIT_PRECHECK, "private file or run input is not a needle source", offenders)
    _git(repo_path, "rev-parse", "--verify", f"{base}^{{commit}}")
    if not index:
        notes.append("no needles (no private sources): nothing to scan")
        if pr is None:
            notes.append("PR items skipped: no --pr given")
        return []
    hits: List[Hit] = []
    head = _head_blobs(repo_path)
    ranged = _range_blobs(repo_path, base)
    blobs = _cat_blobs(repo_path, sorted({s for s, _ in head} | {s for s, _, _ in ranged}))
    scanned: Set[str] = set()
    for sha, path in head:
        if sha in scanned:
            continue
        scanned.add(sha)
        hits.extend(scan_text(blobs.get(sha, b"").decode("utf-8", errors="replace"), path, index))
    for sha, path, commit in ranged:
        if sha in scanned:
            continue
        scanned.add(sha)
        text = blobs.get(sha, b"").decode("utf-8", errors="replace")
        hits.extend(scan_text(text, f"{path}@{commit[:12]}", index))
    # Messages are read object by object (size-delimited ``cat-file --batch``),
    # never split out of one stream on a delimiter: a message can hold any
    # delimiter byte, and a split one hides its needles (merge gate, Codex #3).
    commits = _git(repo_path, "rev-list", f"{base}..HEAD").decode().split()
    tag_objs = _git(repo_path, "for-each-ref", "refs/tags", "--format=%(objectname)").decode().split()
    objects = _cat_blobs(repo_path, sorted(set(commits) | set(tag_objs)))
    for sha in commits:
        hits.extend(_scan_message(objects.get(sha, b""), f"commit {sha[:12]}", index))
    for obj in tag_objs:
        hits.extend(_scan_message(objects.get(obj, b""), f"tag {obj[:12]}", index))
    # Branch and tag ref NAMES can carry text; scan them, label by object sha only.
    refs = _git(repo_path, "for-each-ref", "refs/heads", "refs/tags", "--format=%(objectname)%00%(refname)%1e")
    for rec in refs.decode("utf-8", errors="replace").split("\x1e"):
        obj, _, name = rec.strip("\n").partition("\x00")
        if obj:
            hits.extend(scan_text(name, f"ref {obj[:12]}", index))
    # Record files (ledger copy, payloads, dispatch logs) are content-free: question text in one is a leak.
    private = eval_privacy.private_root()
    for path in _record_files():
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            raise ScanRefusal(EXIT_USAGE, "record file unreadable", [str(path)])
        hits.extend(scan_text(text, "eval/private/" + path.relative_to(private).as_posix(), index))
    if pr is None:
        notes.append("PR items skipped: no --pr given")
    else:
        runner = gh_runner or _default_gh_runner(gh, repo_path)
        for label, text in _pr_items(pr, runner):
            hits.extend(scan_text(text, label, index))
    return hits


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Leak scanner for private eval questions (never prints a needle).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", nargs="+", metavar="FILE", help="scan a private run's output files")
    mode.add_argument("--merge-gate", action="store_true", help="scan what a push would publish")
    ap.add_argument("--run", help="private run id whose inputs.json adds needle sources (--output)")
    ap.add_argument("--needles", nargs="+", default=[], metavar="PATH", help="extra private eval .jsonl sources")
    ap.add_argument("--base", help="base ref for --merge-gate (range base..HEAD)")
    ap.add_argument("--pr", type=int, help="PR number whose items to scan (--merge-gate)")
    ap.add_argument("--gh", default="gh", help="gh executable (default: gh)")
    ap.add_argument("--repo", default=None, help="git work tree for --merge-gate (default: this repo)")
    ap.add_argument(
        "--handbook-dump",
        default=None,
        metavar="PATH",
        help="handbook JSONL dump whose windows are exempt for sealed needles "
        "(default: data/research/handbook_sections.jsonl)",
    )
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns the exit code (see the module docstring)."""
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.merge_gate and not args.base:
        print("scan_leaks: --merge-gate needs --base <ref>", file=sys.stderr)
        return EXIT_USAGE
    if args.output and (args.base or args.pr is not None):
        print("scan_leaks: --base/--pr apply to --merge-gate only", file=sys.stderr)
        return EXIT_USAGE
    if args.merge_gate and args.run:
        print("scan_leaks: --run applies to --output only", file=sys.stderr)
        return EXIT_USAGE
    notes: List[str] = []
    try:
        if args.output:
            hits = scan_output(
                args.output, run_id=args.run, needles=args.needles, handbook_dump=args.handbook_dump, notes=notes
            )
        else:
            hits = merge_gate(
                args.base,
                repo=args.repo,
                pr=args.pr,
                needles=args.needles,
                gh=args.gh,
                notes=notes,
                handbook_dump=args.handbook_dump,
            )
    except ScanRefusal as exc:
        print(f"scan_leaks: refused (exit {exc.code}): {exc.reason}", file=sys.stderr)
        for p in exc.paths:
            print(f"  {p}", file=sys.stderr)
        return exc.code
    except ValueError as exc:  # e.g. a bad run id: print the type only
        print(f"scan_leaks: refused (exit {EXIT_USAGE}): {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE
    for note in notes:
        print(f"scan_leaks: {note}", file=sys.stderr)
    for hit in hits:
        print(hit.line())
    if hits:
        print(f"scan_leaks: {len(hits)} hit(s); release blocked (exit {EXIT_HIT})", file=sys.stderr)
        return EXIT_HIT
    print("scan_leaks: clean", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
