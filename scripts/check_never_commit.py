"""Server-side backstop for CLAUDE.md's hard rule: never commit the copyrighted corpus or secrets.

Run in CI on every push and pull request. Fails (exit 1) if the checked-out tree, or any commit in the
pushed/PR range, ADDS a path that must never be committed: anything under ``data/``, any PDF, ``.env``
files (``.env.example`` allowed), the Chroma indexes, bake-off artefacts, logs, the tutorial documents or
the judge review dump. Client-side git hooks can be bypassed; this check runs on GitHub and is a required
status check on ``main``.

Usage:
    python scripts/check_never_commit.py [--range BASE..HEAD]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Iterable, List

NEVER_COMMIT = [
    r"^data/",
    r"\.pdf$",
    r"(^|/)\.env$",
    r"(^|/)\.env\.(?!example$)[^/]+$",
    r"^chroma_db/",
    r"^chroma_db_arm_[^/]*/",
    r"^sample_chroma_db/",
    r"^eval/bakeoff/",
    r"^logs/",
    r"^Tutorial_Docs_for_review/",
    r"^eval/judge_review\.jsonl$",
]
_PATTERNS = [re.compile(p, re.IGNORECASE) for p in NEVER_COMMIT]


def offenders(paths: Iterable[str]) -> List[str]:
    """Return the sorted unique paths that match a never-commit pattern."""
    return sorted({p for p in paths if p and any(rx.search(p) for rx in _PATTERNS)})


def _git(*args: str) -> List[str]:
    out = subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def tree_paths() -> List[str]:
    """Every path tracked at HEAD."""
    return _git("ls-files")


def added_in_range(rev_range: str) -> List[str]:
    """Paths added (or renamed into place) by any commit in ``rev_range``, including ones later removed."""
    return _git("log", "--no-renames", "--diff-filter=A", "--name-only", "--format=", rev_range)


def main(argv: List[str] | None = None) -> int:
    """Check the tree (and optionally a commit range); print offenders and return 1 if any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--range", dest="rev_range", default=None,
                        help="also check every commit in BASE..HEAD (e.g. the pushed range)")
    args = parser.parse_args(argv)
    bad = offenders(tree_paths())
    if args.rev_range:
        bad = sorted(set(bad) | set(offenders(added_in_range(args.rev_range))))
    if bad:
        print("never-commit paths found (corpus/secrets must stay out of this public repo):")
        for p in bad:
            print(f"  - {p}")
        return 1
    print("never-commit check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
