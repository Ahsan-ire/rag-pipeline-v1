"""The single arm roster for bake-off instruments (D68, item 9).

``scripts/bakeoff_report.py`` (per-class movement, S5/N4 role coverage) and
``scripts/w_sweep.py`` (the S5/N4 printout) used to carry their own copies --
one by question prefix, one by list index. Both now read this module, keyed by
the public v1 row id (``"q:" + sha256(question)[:12]``, ``src.eval_privacy``)
of the realistic-set row, so no question text is quoted here.

The ids were resolved [C] from ``eval/realistic_set.jsonl`` (sha256
``ec488b57...``) against the prefixes in
``docs/designs/001-bakeoff-embedding-model.md``'s per-class roster table; each
prefix matched exactly one row, and w_sweep's former index pair
(``realistic``, 4) / (``realistic``, 16) resolves to the S5 / N4 ids below.
``tests/test_p16_roster.py`` re-checks the ids against the committed set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class RosterEntry:
    """One row of the per-class roster (the Tier-2 ground truth).

    Attributes:
        failure_class: the D54 failure class ("vocabulary gap" / "near-miss").
        row_id: the public v1 id of the realistic-set row.
        expected: the sections the eval set expects (display only).
    """

    failure_class: str
    row_id: str
    expected: Tuple[str, ...]


@dataclass(frozen=True)
class RoleSpec:
    """A comparison row whose answer needs BOTH role groups retrieved.

    Attributes:
        name: short handle used in the brief and in D50 ("S5", "N4").
        row_id: the public v1 id of the realistic-set row.
        groups: the role groups that must each be covered separately.
    """

    name: str
    row_id: str
    groups: Tuple[str, ...]


ROSTER: Tuple[RosterEntry, ...] = (
    RosterEntry("vocabulary gap", "q:08b7099eb4b2", ("13.4.8",)),
    RosterEntry("vocabulary gap", "q:24c2197cce0c", ("5.8",)),
    RosterEntry("vocabulary gap", "q:c36cd42b4b54", ("1.7", "1.8")),
    RosterEntry("near-miss", "q:1abcf0d43e43", ("2.2.1", "2.2.2", "2.9")),
    RosterEntry("near-miss", "q:725d4c771c87", ("4.5.1",)),
    RosterEntry("near-miss", "q:fc7adba18362", ("7.2", "7.2.9")),
    RosterEntry("near-miss", "q:f4132fc160c7", ("16.4.5",)),
    RosterEntry("near-miss", "q:36f56149fc71", ("9.7.2", "9.8")),
)

# S5 and N4 (D50): both purchase-vs-sale comparisons. Coverage deliberately
# checks the two role groups only -- 2.9 (the process overview) is excluded on
# purpose, recorded in D57.
ROLE_QUESTIONS: Tuple[RoleSpec, ...] = (
    RoleSpec("S5", "q:1abcf0d43e43", ("2.2.1", "2.2.2")),
    RoleSpec("N4", "q:79b8e364817a", ("2.2.1", "2.2.2")),
)

ROLE_BY_NAME = {r.name: r for r in ROLE_QUESTIONS}
