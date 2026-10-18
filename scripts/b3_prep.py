"""Phase 16A-2b b3 prep: build attempt 1's ``draft`` dispatch inputs and print the launcher command (no dispatch).

b3 asks the sealed processor (SP) to draft the blind pool. Before that dispatch the orchestrator needs a directory with the
task, the ids the SP will use and the three source inputs, plus the exact ``sp_launch.py`` command. This tool builds them
and prints the command. It never starts the SP, never calls ``sp_launch``, and makes no network or API call.

Usage (from the repo root)::

    .venv/bin/python scripts/b3_prep.py [--out DIR] [--lay-or-terse-max N] [--timeout SECONDS]
                                        [--private-root D] [--repo D] [--prereg F] [--inventory F] [--handbook-dump F]

What it does, in order (it stops at the first refusal, and issues ids only after every other check has passed)
---------------------------------------------------------------------------------------------------------------
1. **Prereg.** ``eval/prereg_16a2.json`` must load, pass the launcher's own ``check_prereg_valid`` and be committed unmodified
   (``sealed_tool.prereg_commit``). The printed ``--prereg`` is that last commit; ``--branch`` is the prereg's own ``branch``
   (the launcher refuses any other).
2. **Counts.** It runs ``sealed_tool``'s ``cmd_count_migrated`` in-process over the same inputs b2 used
   (``eval/golden_set.jsonl``, ``eval/realistic_set.jsonl``, ``eval/v2_pairing.json``, the private
   ``eval/private/inputs/t1t7_count_v1.jsonl`` and the prereg) and reads two numbers from its printed line:
   ``drafts`` becomes ``task.json:count`` and ``refusal_drafts`` becomes ``task.json:refusals``. If ``count-migrated`` would
   stop (``band_unreachable``: a split's band or the pool's refusal supply cannot be met) this tool stops with the same
   reason. The counts are never recomputed here, so they cannot drift from ``count-migrated``'s.
3. **Register policy.** The registered rule (``drafting_rule.attempt_1.realistic_lay_min_share``) is a share of the new
   drafts that are ``lay`` or ``terse``. The SP chooses the registers, so the task bounds them with two numbers:
   ``lay_or_terse_max`` (default ``ceil(2 * count / 3)``, or ``--lay-or-terse-max``) and
   ``lay_min = ceil(share * lay_or_terse_max)`` (exact ``Fraction`` arithmetic on the prereg value). If the SP writes at least
   ``lay_min`` ``lay`` drafts and at most ``lay_or_terse_max`` ``lay``-or-``terse`` drafts, then ``lay >= lay_min >=
   share * lay_or_terse_max >= share * (lay or terse)``: the registered share holds whatever mix it picks.
4. **Inputs.** ``eval/section_inventory.json``, the handbook dump (``data/research/handbook_sections.jsonl``) and the PPC land
   law tutorial manual must exist. The manual is the one file in ``Tutorial_Docs_for_review/`` whose name starts with
   ``2025 PPC Applied Land Law Student Tutorial Manual`` (selected by name, never the Tutorial One / Seven sheets; zero or
   several matches are refused). The brief ``docs/sealed/sealed-processor.md`` must exist.
5. **ids.** ``count`` fresh opaque family ids come from ``sealed_tool``'s ``cmd_ids`` (so they are recorded as issued and never
   collide), written to ``<out>/ids.txt``. Idempotent: if ``<out>/ids.txt`` already holds exactly ``count`` valid, distinct,
   already-issued ids it is reused; any other existing ``ids.txt`` is refused (ids are never issued twice silently).
6. **Tree.** ``<out>/task.json`` (canonical JSON, sorted keys, trailing newline), ``ids.txt``, ``inventory.json``,
   ``handbook_sections.jsonl`` and ``tutorials/<the manual>``. The three sources are byte copies (``shutil.copyfile``, never
   opened for reading as text), written beside the destination and renamed into place, and their sha256 is checked equal to the
   source's. No symlinks. ``<out>/tutorials/`` must hold nothing but the manual.

``<out>`` defaults to ``<private root>/work/b3/draft1`` and must lie under the private root (and not under its ``sealed/``
directory): the copies hold corpus and copyrighted text, so they may only go where git ignores them.

Output (stdout) is the sealed tool's allowlisted printer: integers, sha256 digests and enum words only, then the launcher
command as the last line. No question, section text or file name is ever printed. Errors are ``error reason=<enum>`` on stderr.

Exit codes: 0 ok; 1 internal error; 2 usage or missing/invalid input (including an invalid prereg file); 8 refused (the
prereg is not committed or not valid, ``band_unreachable``, an existing ids.txt that does not fit).
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import math
import os
import secrets
import shlex
import shutil
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src import eval_privacy  # noqa: E402
from src import eval_schema  # noqa: E402
from scripts import scan_leaks as sl  # noqa: E402  (the handbook dump's repo-relative path)
from scripts import sealed_tool as st  # noqa: E402
from scripts import sp_launch as sp  # noqa: E402

#: The brief the SP receives as its system prompt (repo-relative; the command is run from the repo root).
BRIEF_RELATIVE = "docs/sealed/sealed-processor.md"
#: Default ``--out`` under the private root.
DEFAULT_OUT_PARTS = ("work", "b3", "draft1")
#: Where the manual lives and how it is recognised: by name prefix (the T1 / T7 exercise sheets never match).
TUTORIAL_DIR = "Tutorial_Docs_for_review"
MANUAL_PREFIX = "2025 PPC Applied Land Law Student Tutorial Manual"
#: The private count input b2 used for the T1 / T7 exercise families (``--tutorial`` of ``count-migrated``).
COUNT_INPUT_PARTS = ("inputs", "t1t7_count_v1.jsonl")
DEFAULT_TIMEOUT = 21600  # seconds the SP may run: 6 h, generous for ~105 families
INTERPRETER = ".venv/bin/python"

#: Every word a ``draft`` ``return.json`` or a ``blocked`` return may hold (keys and values), as the brief names them. The
#: launcher's return validator drops anything else, so the command passes each one as ``--enum``.
DRAFT_ENUMS: Tuple[str, ...] = (
    "status", "ok", "blocked", "kind", "draft", "families", "rows",
    "by_scope", "answer", "partial", "refuse",
    "by_register", "lay", "modelled", "terse",
    "by_source", "tutorial", "handbook",
    "sha256", "reason", "inputs_missing", "task_unclear", "quota_unreachable",
)
#: The ``name=`` values of ``digest`` lines.
INPUT_NAMES = frozenset({"inventory", "handbook_sections", "tutorial_manual", "brief", "prereg"})
#: The ``count-migrated`` fields echoed on the ``counts`` line (integers and two enum words).
COUNT_FIELDS = (
    "drafts", "refusal_drafts", "migrated_families", "preassigned_refusals", "pool_total", "refusal_target",
    "drafts_rule", "draft_margin", "supply",
)


class PrepPrinter(st.Printer):
    """The sealed tool's allowlisted printer, plus the closed set of input names on ``digest`` lines."""

    @staticmethod
    def _value(kind: str, key: str, value: Any) -> str:
        if kind == "digest" and key == "name" and isinstance(value, str) and value in INPUT_NAMES:
            return value
        return st.Printer._value(kind, key, value)


# ---------------------------------------------------------------------------
# The register policy
# ---------------------------------------------------------------------------
def lay_policy(count: int, share: Fraction, lay_or_terse_max: Optional[int] = None) -> Tuple[int, int]:
    """``(lay_min, lay_or_terse_max)`` for a draft of ``count`` families under the registered ``share``.

    ``lay_or_terse_max`` defaults to ``ceil(2 * count / 3)``; an explicit one must lie in ``1..count``. ``lay_min`` is
    ``ceil(share * lay_or_terse_max)``. An SP that writes at least ``lay_min`` ``lay`` drafts and at most ``lay_or_terse_max``
    ``lay``-or-``terse`` drafts has ``lay >= share * (lay or terse)``, whatever mix it picks.

    Raises:
        Stop: ``usage`` for a count below 1, a ``share`` outside ``(0, 1]`` or a ``lay_or_terse_max`` outside ``1..count``.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or not 0 < share <= 1:
        raise st.Stop(st.EXIT_USAGE, "usage")
    if lay_or_terse_max is None:
        lay_or_terse_max = -(-2 * count // 3)
    elif isinstance(lay_or_terse_max, bool) or not isinstance(lay_or_terse_max, int) or not 1 <= lay_or_terse_max <= count:
        raise st.Stop(st.EXIT_USAGE, "usage")
    lay_min = -(-share.numerator * lay_or_terse_max // share.denominator)
    return lay_min, lay_or_terse_max


def registered_share(prereg: Mapping[str, Any]) -> Fraction:
    """``drafting_rule.attempt_1.realistic_lay_min_share`` as an exact fraction (``Fraction(str(value))``, as ``split`` reads it)."""
    try:
        raw = prereg["drafting_rule"]["attempt_1"]["realistic_lay_min_share"]
    except (KeyError, TypeError):
        raise st.Stop(st.EXIT_USAGE, "prereg_missing") from None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise st.Stop(st.EXIT_USAGE, "prereg_invalid")
    share = Fraction(str(raw))
    if not 0 < share <= 1:
        raise st.Stop(st.EXIT_USAGE, "prereg_invalid")
    return share


# ---------------------------------------------------------------------------
# Counts: count-migrated, in-process
# ---------------------------------------------------------------------------
def draft_counts(ctx: st.Ctx) -> Dict[str, Any]:
    """Run ``count-migrated`` over b2's inputs and return its printed ``count_migrated`` fields (ints, or enum words).

    The numbers come from ``sealed_tool.cmd_count_migrated`` itself, with its printer captured, so there is no second
    implementation to drift. When it stops (``band_unreachable``) the line it printed first is forwarded to this tool's
    stdout (it is the same allowlisted aggregate) and the stop is re-raised.
    """
    capture = st.Printer(io.StringIO(), io.StringIO())
    inner = dataclasses.replace(ctx, printer=capture)
    try:
        st.cmd_count_migrated(
            inner,
            str(ctx.repo / "eval" / "golden_set.jsonl"), str(ctx.repo / "eval" / "realistic_set.jsonl"),
            str(ctx.repo / "eval" / "v2_pairing.json"), str(ctx.private.joinpath(*COUNT_INPUT_PARTS)),
            None, None, None,
        )
    except st.Stop:
        for line in capture.out.getvalue().splitlines():  # type: ignore[attr-defined]
            ctx.printer.raw_validated(line)
        raise
    fields: Optional[Dict[str, Any]] = None
    for line in capture.out.getvalue().splitlines():  # type: ignore[attr-defined]
        if line.startswith("count_migrated "):
            pairs = (part.split("=", 1) for part in line.split(" ")[1:])
            fields = {key: int(val) if val.isdigit() else val for key, val in pairs}
    if fields is None or not all(isinstance(fields.get(k), int) for k in ("drafts", "refusal_drafts")):
        raise st.Stop(st.EXIT_FAIL, "internal")
    count, refusals = fields["drafts"], fields["refusal_drafts"]
    if count < 1 or refusals < 0 or refusals > count:
        raise st.Stop(st.EXIT_REFUSED, "band_unreachable")  # more refusal families than families: the plan cannot be drafted
    return fields


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Sources:
    """The files the dispatch needs, all checked present before anything is issued or written."""

    inventory: Path
    dump: Path
    manual: Path
    brief: Path


def select_manual(repo: Path) -> Path:
    """The one file in ``<repo>/Tutorial_Docs_for_review/`` whose name starts with :data:`MANUAL_PREFIX`.

    Selected by name only (the file is not opened). Anything else in the folder, including the Tutorial One and Tutorial
    Seven exercise sheets, is ignored. A missing folder or no match is ``input_missing``; two or more matches is ``input_invalid``.
    """
    try:
        with os.scandir(repo / TUTORIAL_DIR) as entries:
            matches = sorted(e.path for e in entries if e.name.startswith(MANUAL_PREFIX) and e.is_file())
    except OSError:
        raise st.Stop(st.EXIT_USAGE, "input_missing") from None
    if not matches:
        raise st.Stop(st.EXIT_USAGE, "input_missing")
    if len(matches) > 1:
        raise st.Stop(st.EXIT_USAGE, "input_invalid")
    return Path(matches[0])


def resolve_sources(ctx: st.Ctx) -> Sources:
    """The inventory, the handbook dump, the manual and the brief, each required to exist (``dump_missing`` / ``input_missing``)."""
    inventory = ctx.inventory_path if ctx.inventory_path is not None else ctx.repo / "eval" / "section_inventory.json"
    dump = ctx.handbook_dump if ctx.handbook_dump is not None else ctx.repo / sl.HANDBOOK_DUMP
    brief = ctx.repo / BRIEF_RELATIVE
    if not inventory.is_file() or not brief.is_file():
        raise st.Stop(st.EXIT_USAGE, "input_missing")
    if not dump.is_file():
        raise st.Stop(st.EXIT_USAGE, "dump_missing")
    return Sources(inventory, dump, select_manual(ctx.repo), brief)


def resolve_out(ctx: st.Ctx, given: Optional[str]) -> Path:
    """The output directory: the default, or ``--out``, which must be a real path under the private root and outside ``sealed/``."""
    default = ctx.private.joinpath(*DEFAULT_OUT_PARTS)
    raw = Path(given) if given else default
    if raw.is_symlink():
        raise st.Stop(st.EXIT_USAGE, "path_outside")
    out = raw.resolve()
    # `is_under` also compares by file identity, so a case-variant spelling (``eval/private/Sealed/x`` on macOS) is caught too
    strictly_inside = eval_privacy.is_under(out, ctx.private) and not eval_privacy.is_under(ctx.private, out)
    if not strictly_inside or eval_privacy.is_under(out, ctx.sealed_dir):
        raise st.Stop(st.EXIT_USAGE, "path_outside")
    return out


def check_tree(out: Path, manual_name: str) -> None:
    """Refuse, before anything is written, an ``out`` that is not a plain directory or a ``tutorials/`` that holds anything but the manual."""
    if os.path.lexists(out) and (out.is_symlink() or not out.is_dir()):
        raise st.Stop(st.EXIT_REFUSED, "input_invalid")
    tutorials = out / "tutorials"
    if os.path.lexists(tutorials):
        if tutorials.is_symlink() or not tutorials.is_dir() or any(name != manual_name for name in os.listdir(tutorials)):
            raise st.Stop(st.EXIT_REFUSED, "input_invalid")


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------
def reuse_ids(ctx: st.Ctx, path: Path, count: int) -> None:
    """An existing ``ids.txt`` is reusable only if it is exactly ``count`` valid, distinct ids that ``ids`` already issued.

    Raises:
        Stop: ``count_mismatch`` for another number of ids; ``input_invalid`` for a symlink, a malformed or repeated id,
            or an id that is not in ``sealed/work/issued_ids.txt`` (both refused with exit 8: never issue twice silently).
    """
    if path.is_symlink() or not path.is_file():
        raise st.Stop(st.EXIT_REFUSED, "input_invalid")
    try:
        text = path.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError):
        raise st.Stop(st.EXIT_REFUSED, "input_invalid") from None
    ids = text.split("\n")
    if ids and ids[-1] == "":
        ids.pop()
    if len(ids) != count:
        raise st.Stop(st.EXIT_REFUSED, "count_mismatch")
    if len(set(ids)) != len(ids) or not all(sp.FAMILY_ID_RE.fullmatch(i) for i in ids) or text != "\n".join(ids) + "\n":
        raise st.Stop(st.EXIT_REFUSED, "input_invalid")
    issued_path = ctx.sealed_work / "issued_ids.txt"
    try:
        issued = set(issued_path.read_bytes().decode("ascii", errors="replace").split()) if issued_path.is_file() else set()
    except OSError:
        raise st.Stop(st.EXIT_REFUSED, "input_invalid") from None
    if not set(ids) <= issued:
        raise st.Stop(st.EXIT_REFUSED, "input_invalid")


def resolve_ids(ctx: st.Ctx, path: Path, count: int) -> bool:
    """Make ``path`` hold ``count`` issued ids; True if an existing file was reused, False if they were just issued."""
    if os.path.lexists(path):
        reuse_ids(ctx, path, count)
        return True
    capture = st.Printer(io.StringIO(), io.StringIO())
    st.cmd_ids(dataclasses.replace(ctx, printer=capture), count, str(path))  # issues, writes the file, records them as issued
    return False


# ---------------------------------------------------------------------------
# Writing the tree
# ---------------------------------------------------------------------------
def copy_bytes(src: Path, dst: Path) -> str:
    """Byte-copy ``src`` to ``dst`` (written beside it, then renamed into place); return the sha256, checked equal to the source's.

    The content is never decoded or printed. A symlinked source is followed (the copy is a regular file); an existing ``dst``
    (even a symlink) is replaced, never written through.
    """
    want = st.sha_file(src)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(3)}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        if st.sha_file(tmp) != want:
            raise st.Stop(st.EXIT_FAIL, "internal")
        os.replace(tmp, dst)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return want


def display_path(repo: Path, path: Path) -> str:
    """``path`` relative to the repo root if it lies under it (the command runs from there), else absolute."""
    try:
        return path.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(path)


def launcher_command(out_display: str, prereg_commit: str, branch: str, timeout: int) -> str:
    """The exact ``sp_launch.py`` command for the dispatch, one line, every word shell-quoted."""
    task, ids = f"{out_display}/task.json", f"{out_display}/ids.txt"
    inputs = [task, ids, f"{out_display}/inventory.json", f"{out_display}/handbook_sections.jsonl", f"{out_display}/tutorials"]
    argv = [
        "env", "-u", "ANTHROPIC_API_KEY", INTERPRETER, "scripts/sp_launch.py", "--kind", "data", "--task", task,
        "--brief", BRIEF_RELATIVE, "--inputs", *inputs, "--prereg", prereg_commit, "--branch", branch, "--timeout", str(timeout),
    ]
    for word in DRAFT_ENUMS:
        argv += ["--enum", word]
    return " ".join(shlex.quote(part) for part in argv)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def prepare(ctx: st.Ctx, args: argparse.Namespace) -> int:
    """Check, then build the dispatch tree and print the summary and the launcher command. See the module docstring."""
    out = resolve_out(ctx, args.out)
    prereg = st.load_prereg(ctx)
    try:
        sp.check_prereg_valid(ctx.prereg_file)  # the check the launcher runs: an invalid prereg would be refused at dispatch
    except sp.LaunchError:
        raise st.Stop(st.EXIT_REFUSED, "prereg_invalid") from None
    commit = st.prereg_commit(ctx)  # the last commit that touched the prereg, and the working file equals it
    branch = prereg.get("branch")
    if not isinstance(branch, str) or not sp.BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise st.Stop(st.EXIT_USAGE, "prereg_invalid")
    share = registered_share(prereg)

    fields = draft_counts(ctx)
    ctx.printer.emit("counts", **{key: fields[key] for key in COUNT_FIELDS if key in fields})
    count, refusals = int(fields["drafts"]), int(fields["refusal_drafts"])
    lay_min, lay_max = lay_policy(count, share, args.lay_or_terse_max)
    ctx.printer.emit(
        "lay_policy", share_num=share.numerator, share_den=share.denominator, lay_or_terse_max=lay_max, lay_min=lay_min,
        override="yes" if args.lay_or_terse_max is not None else "no",
    )

    sources = resolve_sources(ctx)
    check_tree(out, sources.manual.name)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)  # nothing above wrote anything; ids are issued only now
    reused = resolve_ids(ctx, out / "ids.txt", count)
    ctx.printer.emit("ids", count=count, reused="yes" if reused else "no", sha256=st.sha_file(out / "ids.txt"))

    task = {"kind": "draft", "count": count, "refusals": refusals, "lay_min": lay_min, "lay_or_terse_max": lay_max}
    st.write_atomic(out / "task.json", (st.canon(task) + "\n").encode("ascii"))
    ctx.printer.emit("task", count=count, refusals=refusals, lay_min=lay_min, lay_or_terse_max=lay_max, sha256=st.sha_file(out / "task.json"))

    (out / "tutorials").mkdir(exist_ok=True, mode=0o700)
    digests = {
        "inventory": copy_bytes(sources.inventory, out / "inventory.json"),
        "handbook_sections": copy_bytes(sources.dump, out / "handbook_sections.jsonl"),
        "tutorial_manual": copy_bytes(sources.manual, out / "tutorials" / sources.manual.name),
        "brief": st.sha_file(sources.brief),
        "prereg": st.sha_file(ctx.prereg_file),
    }
    for name, digest in digests.items():
        ctx.printer.emit("digest", name=name, sha256=digest)
    ctx.printer.raw_validated(launcher_command(display_path(ctx.repo, out), commit, branch, args.timeout))
    return st.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = st._Parser(prog="b3_prep.py", description="Prepare the b3 draft dispatch inputs and print the launcher command (no dispatch).")
    for opt in ("private-root", "repo", "prereg", "inventory", "handbook-dump", "out"):
        ap.add_argument(f"--{opt}", default=None, metavar="PATH")
    ap.add_argument("--lay-or-terse-max", type=int, default=None, dest="lay_or_terse_max", metavar="N")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, out: Any = None, err: Any = None) -> int:
    """CLI entry point; returns the exit code. Nothing but the allowlisted printer writes output."""
    printer = PrepPrinter(out, err)
    try:
        args = build_parser().parse_args(argv)
        if not 0 < args.timeout <= sp.MAX_TIMEOUT:
            raise st.Stop(st.EXIT_USAGE, "usage")
        ctx = st.default_ctx(
            printer, private=args.private_root, repo=args.repo, prereg_path=args.prereg, inventory_path=args.inventory,
            handbook_dump=args.handbook_dump,
        )
        with st.state_lock(ctx):
            return prepare(ctx, args)
    except st.Stop as stop:
        printer.error(stop.reason, **stop.fields)
        return stop.code
    except eval_schema.SchemaError as exc:
        for line, fld, _reason in exc.errors[:50]:
            printer.emit("schema_error", line=min(line, st.MAX_PRINT_INT), field=fld if st._FIELD_PATH.fullmatch(fld) else "other")
        printer.error("inventory_invalid" if isinstance(exc, eval_schema.InventoryError) else "schema_invalid", errors=len(exc.errors))
        return st.EXIT_USAGE
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001 - never show exception text (it could carry a path or a row)
        printer.err.write("error reason=internal\n")
        return st.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
