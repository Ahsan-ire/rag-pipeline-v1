"""Phase 16A-1 leak scanner (item 1, D65): find private eval questions before they leave the box.

The scanner never prints or writes a needle, matched text or file content. A
hit names only *where* (target), *which* needle (an opaque id), its *kind* and
its *source*.

Usage::

    python scripts/scan_leaks.py --output <file>... [--run <run id>] [--needles <path>...]
    python scripts/scan_leaks.py --merge-gate --base <ref> [--pr <number>] [--gh <exe>]
                                 [--repo <dir>] [--needles <path>...]

Sources (explicit)
------------------
- every registry ``private`` set (:func:`src.eval_sets.private_needle_sets`);
- each ``--needles <path>``: a v1 or v2 eval ``.jsonl`` that must classify
  ``private`` (a ``public`` one is refused, exit 2; a ``sealed`` one exit 4);
- ``--output`` mode with ``--run <id>``: the run's own private inputs from
  ``eval/private/runs/<id>/inputs.json`` -- each ``questions`` input and each
  ``derived`` input's listed ``sources``. A run input whose recorded sha256 is a
  registered public set's contributes no needles (every one of its questions
  would be exempt) and is not refused; a sealed one is refused (exit 4); one
  whose file is missing or no longer matches its recorded sha256 is refused
  (exit 2: the needles it held cannot be rebuilt).

A registered private set that is missing on disk is refused (exit 2): the scan
would silently lose its needles. A source line that does not parse as JSON is
refused (exit 2, path:line only); rows without a string ``question`` are
skipped (metadata rows).

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

Needle id = ``"n"`` + the first 10 hex of sha256(normalised needle text, tokens
joined by single spaces).

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
target is folded into the whole hit. Targets: ``<file>:<line>`` (``--output``
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

Pre-check (exit 7): refused when (a) a file under the private root outside
``runs/`` and ``artifacts/`` is not a needle source (matched by sha256 against
every needle source file), or (b) a ``runs/*/inputs.json`` does not parse, or
lists a ``questions`` input that is not a needle source, or a ``derived`` input
that is not a needle source and not exempt, or a ``derived`` input's listed
source (or a sha256-keyed artifact's own ``inputs`` header) that is not a
needle source, a registered public set or a legacy-public entry. Exempt
derived inputs (their sources are still checked): a recorded sha256 listed in
``eval/legacy_public.json``; and a **sha256-keyed artifact with no question
text** -- a file under ``eval/private/artifacts/`` that still matches its
recorded sha256, parses as JSON, has a non-empty ``entries`` container whose
every item carries a 64-hex value under a key containing ``sha256``, and has no
key named ``question`` or ``questions`` anywhere in its tree. (Residual: other
string values in such an artifact are not inspected.) Refusal lines print
paths only, never content.

Exit codes: 0 clean; 2 usage error, refused public ``--needles``, unreadable or
missing source, ``gh`` failure; 4 sealed source offered; 5 hit (either mode);
7 merge-gate source pre-check refused.

With private sets present, ``--merge-gate`` runs before every push (CI cannot:
it has no private sets).

Python API: :func:`normalise_tokens`, :func:`needle_id`, :func:`collect_sources`,
:func:`build_needles`, :func:`scan_text`, :func:`scan_output`,
:func:`merge_gate`, :func:`main`.
"""

from __future__ import annotations

import argparse
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
from src.eval_privacy import PRIVATE, PUBLIC, SEALED, REPO_ROOT  # noqa: E402

WINDOW = 8
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SEALED = 4
EXIT_HIT = 5
EXIT_PRECHECK = 7

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
    """One question file whose rows become needles; ``label`` is printed on hits."""

    path: Path
    label: str


@dataclass
class NeedleIndex:
    """Non-exempt needles, keyed by token tuple."""

    whole: Dict[Tuple[str, ...], Tuple[str, str]] = field(default_factory=dict)
    windows: Dict[Tuple[str, ...], Tuple[str, str]] = field(default_factory=dict)
    # whole needles of >= WINDOW tokens, indexed by their first window
    long_by_prefix: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = field(default_factory=dict)
    short_lengths: Set[int] = field(default_factory=set)
    source_shas: Set[str] = field(default_factory=set)
    exempt_whole: int = 0
    exempt_windows: int = 0

    def __len__(self) -> int:
        return len(self.whole) + len(self.windows)


@dataclass(frozen=True)
class Hit:
    """One finding: where, which needle (opaque id), kind and source. Never text."""

    target: str
    needle: str
    kind: str
    source: str

    def line(self) -> str:
        """The printable hit line."""
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


def _check_sealed(path: Path) -> None:
    if eval_sets.classify(path) == SEALED:
        raise ScanRefusal(EXIT_SEALED, "sealed source refused (16A-1)", [str(path)])


def _run_inputs(run_id: str) -> List[Dict[str, Any]]:
    """Parse ``runs/<id>/inputs.json`` (refusal exit 2 if missing or malformed)."""
    path = eval_privacy.run_dir(run_id, create=False) / "inputs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        inputs = data["inputs"]
        if data.get("version") != 1 or not isinstance(inputs, list):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ScanRefusal(EXIT_USAGE, "run inputs.json missing or malformed", [str(path)])
    return [i for i in inputs if isinstance(i, dict)]


def collect_sources(
    needles: Sequence[Any] = (),
    *,
    run_id: Optional[str] = None,
) -> List[NeedleSource]:
    """Gather the explicit needle sources (registry private sets, ``--needles``, run inputs).

    Raises:
        ScanRefusal: exit 4 for a sealed source, exit 2 for a public ``--needles``
            path, a non-``.jsonl`` one, a missing source or a drifted run input.
    """
    sources: List[NeedleSource] = []
    for entry in eval_sets.private_needle_sets():
        path = entry.resolved()
        if not path.is_file():
            raise ScanRefusal(EXIT_USAGE, "registered private set missing", [entry.name])
        _check_sealed(path)
        sources.append(NeedleSource(path, entry.name))
    for raw in needles:
        path = Path(raw)
        if not path.is_file():
            raise ScanRefusal(EXIT_USAGE, "--needles file not found", [str(raw)])
        cls = eval_sets.classify(path)
        if cls == SEALED:
            raise ScanRefusal(EXIT_SEALED, "sealed source refused (16A-1)", [str(raw)])
        if cls == PUBLIC:
            raise ScanRefusal(EXIT_USAGE, "public source refused: --needles must classify private", [str(raw)])
        if path.suffix.lower() != ".jsonl":
            raise ScanRefusal(EXIT_USAGE, "--needles must be a v1 or v2 eval .jsonl", [str(raw)])
        sources.append(NeedleSource(path, str(raw)))
    if run_id is not None:
        no_needles = _registry_public_shas() | _legacy_shas()
        recorded: List[Dict[str, Any]] = []
        for item in _run_inputs(run_id):
            if item.get("kind") == "questions":
                recorded.append(item)
            elif item.get("kind") == "derived":
                recorded.extend(s for s in item.get("sources") or [] if isinstance(s, dict))
        for item in recorded:
            sha = str(item.get("sha256", ""))
            rec_path = str(item.get("path", ""))
            if sha in no_needles:
                continue
            path = _resolve_recorded(rec_path)
            if not path.is_file():
                raise ScanRefusal(EXIT_USAGE, "run input missing", [rec_path])
            _check_sealed(path)
            if eval_sets.sha256_file(path) != sha:
                raise ScanRefusal(EXIT_USAGE, "run input changed since the run", [rec_path])
            sources.append(NeedleSource(path, rec_path))
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


def build_needles(sources: Sequence[NeedleSource]) -> NeedleIndex:
    """Build the needle index from ``sources``, minus public exemptions."""
    index = NeedleIndex()
    if not sources:
        return index
    public_hashes, public_windows = _public_questions()
    for src in sources:
        index.source_shas.add(eval_sets.sha256_file(src.path))
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
                if win in public_windows:
                    index.exempt_windows += 1
                elif win not in index.windows:
                    index.windows[win] = (needle_id(win), src.label)
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


def scan_text(text: str, target: str, index: NeedleIndex) -> List[Hit]:
    """Scan one target's text; hits are labelled ``<target>:<line>``.

    Matching is on whole tokens over the normalised token stream of the
    whole text, so a needle wrapped across lines is still caught.
    """
    if not index or not text:
        return []
    tokens, lines = _tokens_with_lines(text)
    n = len(tokens)
    found: List[Tuple[int, int, str, str, str]] = []  # (start, end, kind, id, source)
    for i in range(n):
        if i + WINDOW <= n:
            win = tuple(tokens[i : i + WINDOW])
            for whole in index.long_by_prefix.get(win, ()):
                if tuple(tokens[i : i + len(whole)]) == whole:
                    nid, src = index.whole[whole]
                    found.append((i, i + len(whole), "whole", nid, src))
            if win in index.windows:
                nid, src = index.windows[win]
                found.append((i, i + WINDOW, "window", nid, src))
        for length in index.short_lengths:
            if i + length <= n:
                seq = tuple(tokens[i : i + length])
                if seq in index.whole:
                    nid, src = index.whole[seq]
                    found.append((i, i + length, "whole", nid, src))
    wholes = [(s, e) for s, e, k, _, _ in found if k == "whole"]
    hits: List[Hit] = []
    seen: Set[Tuple[str, str]] = set()
    for start, end, kind, nid, src in sorted(found):
        if kind == "window" and any(s <= start and end <= e for s, e in wholes):
            continue
        label = f"{target}:{lines[start]}"
        if (label, nid) in seen:
            continue
        seen.add((label, nid))
        hits.append(Hit(label, nid, kind, src))
    return hits


def scan_output(
    files: Sequence[Any],
    *,
    run_id: Optional[str] = None,
    needles: Sequence[Any] = (),
) -> List[Hit]:
    """``--output`` mode: scan a private run's files before release.

    Raises:
        ScanRefusal: as :func:`collect_sources`, or exit 2 for an unreadable file.
    """
    index = build_needles(collect_sources(needles, run_id=run_id))
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


def _is_sha_keyed_artifact(path: Path) -> bool:
    """A JSON artifact keyed by question sha256 that carries no ``question`` key anywhere."""
    if not eval_privacy.is_under(path, eval_privacy.private_root() / "artifacts") or not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False

    def has_question_key(obj: Any) -> bool:
        if isinstance(obj, dict):
            return any(k in ("question", "questions") or has_question_key(v) for k, v in obj.items())
        if isinstance(obj, list):
            return any(has_question_key(v) for v in obj)
        return False

    if not isinstance(doc, dict) or has_question_key(doc):
        return False
    entries = doc.get("entries")
    items = list(entries.values()) if isinstance(entries, dict) else entries
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, dict) or not any(
            "sha256" in k and isinstance(v, str) and _HEX64.match(v) for k, v in item.items()
        ):
            return False
    return True


def _artifact_header_shas(path: Path) -> List[Tuple[str, str]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
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
        if rel.parts[0] in ("runs", "artifacts"):
            continue
        try:
            sha = eval_sets.sha256_file(path)
        except OSError:
            sha = ""
        if sha not in source_shas:
            offenders.append(str(path))
    runs = root / "runs"
    for inputs_json in sorted(runs.glob("*/inputs.json")) if runs.is_dir() else []:
        try:
            data = json.loads(inputs_json.read_text(encoding="utf-8"))
            items = data["inputs"]
            if not isinstance(items, list):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError):
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
            listed = [
                (str(s.get("path", "")), str(s.get("sha256", "")))
                for s in item.get("sources") or []
                if isinstance(s, dict)
            ]
            resolved = _resolve_recorded(rec_path)
            exempt = sha in legacy
            if not exempt and resolved.is_file() and eval_sets.sha256_file(resolved) == sha:
                if _is_sha_keyed_artifact(resolved):
                    exempt = True
                    listed += _artifact_header_shas(resolved)
            if not exempt and sha not in source_shas:
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

    Raises:
        ScanRefusal: exit 7 when the pre-check refuses (paths only); else as
            :func:`collect_sources`, or exit 2 on a git/gh failure.
    """
    repo_path = Path(repo) if repo is not None else REPO_ROOT
    notes = notes if notes is not None else []
    sources = collect_sources(needles)
    index = build_needles(sources)
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
    log = _git(repo_path, "log", "--format=%H%x00%B%x1e", f"{base}..HEAD").decode("utf-8", errors="replace")
    for rec in log.split("\x1e"):
        sha, _, body = rec.strip("\n").partition("\x00")
        if sha:
            hits.extend(scan_text(body, f"commit {sha[:12]}", index))
    tags = _git(repo_path, "for-each-ref", "refs/tags", "--format=%(objectname)%00%(contents)%1e")
    for rec in tags.decode("utf-8", errors="replace").split("\x1e"):
        obj, _, body = rec.strip("\n").partition("\x00")
        if obj:
            hits.extend(scan_text(body, f"tag {obj[:12]}", index))
    # Branch and tag ref NAMES can carry text; scan them, label by object sha only.
    refs = _git(repo_path, "for-each-ref", "refs/heads", "refs/tags", "--format=%(objectname)%00%(refname)%1e")
    for rec in refs.decode("utf-8", errors="replace").split("\x1e"):
        obj, _, name = rec.strip("\n").partition("\x00")
        if obj:
            hits.extend(scan_text(name, f"ref {obj[:12]}", index))
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
            hits = scan_output(args.output, run_id=args.run, needles=args.needles)
        else:
            hits = merge_gate(
                args.base, repo=args.repo, pr=args.pr, needles=args.needles, gh=args.gh, notes=notes
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
