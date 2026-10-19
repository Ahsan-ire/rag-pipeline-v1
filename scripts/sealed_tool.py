"""Phase 16A-2a item a3 (D72): the sealed tool. One CLI for everything that touches sealed rows.

Sealed rows are read only through :func:`src.eval_sealed_read.load_sealed`; every other module
that handles them is a *trusted processor* (D72): this tool, ``scan_leaks.py`` and a slot-gated
run. Whatever this tool prints goes to the orchestrator's context, so **everything it prints goes
through one allowlisted printer** (:class:`Printer`): integers, sha256 hex, enum words from the
closed :data:`ENUM_WORDS` set and, in two places only, opaque ids (``audit``'s per-hit family id
and ``return``'s validated line). A value that is none of these (free text, a float, a negative
number, an unknown word, a malformed id) raises :class:`PrintRefused` instead of being printed.
Keys are code literals. Errors, exceptions and ``argparse`` complaints are rendered as fixed
``error reason=<enum>`` lines; no exception text is ever shown (it could carry a path or a row).

Usage (global options first; every path is injectable so tests use tmp dirs only)::

    sealed_tool.py [--private-root D] [--repo D] [--ledger F] [--mirror F] [--inventory F]
                   [--prereg F] [--handbook-dump F] [--claude-projects D] [--staging-root D]
                   <command> ...

Commands: ``ids``, ``digest``, ``count-migrated``, ``inputs-check``, ``intake``, ``twins``, ``split``,
``paraphrase-apply``, ``sheet``, ``apply``, ``freeze``, ``escalate``, ``calibrate``, ``disagree``, ``exclude``,
``audit``, ``power``, ``prereg-check``, ``declare-batch``, ``draws``, ``epoch``, ``return``.

Exit codes: 0 ok; 1 a checked record failed (``prereg-check``, ``return`` invalid, internal error);
2 usage or invalid input (schema errors by line and field only); 5 ``audit`` found an S-hit;
6 a split attempt was infeasible (**nothing released**; recorded; ``split --diagnose`` exits 6 for every outcome but
``feasible`` and records nothing); 7 ``audit`` could not run (the handbook
dump is absent, so its exemption would silently shrink; or SP staging is live or its moves cannot be recovered);
8 refused (prereg not pushed or round undeclared, wrong state, missing approval or recheck, changed
assignment, another state-changing command holds ``sealed/work/.lock`` ...);
9 **freeze infeasible after round 2** (the D64 stop); 10 ledger stop (corrupt, tampered or lost
private ledgers; the D64 stop).

Every state-changing command (:data:`STATE_COMMANDS`) holds one exclusive, non-blocking flock on
``sealed/work/.lock`` for its whole run, so two of them can never interleave; the second is refused (``locked``).
``split --diagnose`` writes nothing, but it is a ``split`` and takes the same lock, so it never interleaves with one.

Layout (all under the private root; ``W`` = ``sealed/work/``, ``DW`` = ``work/``)
----------------------------------------------------------------------------------
- ``sealed/work/sp_out/<dispatch id>/``  where ``sp_launch`` releases an SP run's ``out/`` (``pool.jsonl``, ``dev.jsonl``,
  ``twins.jsonl``, ``tutorial_map.json``, ``return.json``); ``intake`` takes ``pool.jsonl`` and ``tutorial_map.json``
  from here, ``twins`` and ``freeze`` read ``twins.jsonl`` and ``return.json`` in place, and ``paraphrase-apply`` reads
  ``pool.jsonl`` (marked rows) and ``dev.jsonl`` (unmarked rows) in place.
- ``sealed/pool/**.jsonl``       the SP's drafts (marked rows), one ``<dispatch id>.jsonl`` per ``intake``. ``split`` consumes them.
- ``sealed/sealed_r<R>.jsonl``   the sealed side of round R's release (marked).
- ``dev/tuning_r<R>.jsonl``, ``dev/realistic_r<R>.jsonl``  the development side (**unmarked**).
  Pinned development families are any other ``dev/tuning*.jsonl`` / ``dev/realistic*.jsonl``
  (the file-name prefix names the split); ``dev/quarantine*.jsonl`` is neither.
- ``sealed/quarantine*.jsonl``   rejected, demoted or dropped sealed families (marked).
- ``sealed/heldout-v2.jsonl``, ``dev/tuning-v2.jsonl``, ``dev/realistic-v2.jsonl``  frozen sets.
- ``W/approvals.json`` / ``DW/approvals.json``  owner approvals bound to row hashes. ``W/approvals.json`` is also the
  approvals source of truth for the slot gate (``src.sealed_run`` reads this very file: ``approved[row id]["row_hash"]``).
- ``W/tutorial_map.json``        ``{family id: opaque tutorial tag}`` (the "same source tutorial"
  relation; rows carry no tutorial id, so ``intake`` merges the SP's ``out/tutorial_map.json`` into this map:
  tags ``T02``-``T06``, ``T08``-``T17`` and ``TD1``-``TD7`` there, stored lower-case; ``T01`` and ``T07`` never).
- ``W/sheets/``, ``W/escalations/``, ``W/twins_current_r<R>.json``, ``W/release.journal``.
- ledger payloads (``store_payload``): ``twins_r<R>_<pool digest>``, ``assign_r<R>``,
  ``twins_sealed_<sha16>`` (named by the first 16 hex digits of its own sha256, which is the freeze record's
  ``twins_sha256``; a plain ``twins_sealed`` alias with the same bytes is stored for readers that predate the
  naming) -- content-free (ids, hashes, counts, enums).
- ``W/freeze_<split>.journal`` and ``W/freeze_<split>.data``: the whole freeze transaction, computed first and
  journalled before anything changes (see **freeze**).
- ``W/paraphrase_applied.json``  ``{dispatch id: {"pool_sha256", "dev_sha256"}}`` (a sha256, or ``null`` for an input that
  dispatch did not have): the paraphrase dispatches ``paraphrase-apply`` has applied.
  ``W/paraphrase_<dispatch id>.journal`` and ``W/paraphrase_<dispatch id>.data/``: one ``paraphrase-apply`` transaction (see
  **paraphrase-apply**).

Protocols
---------
**Groups.** The connected components of ALL relations (a twin verdict, the same source tutorial per
``tutorial_map.json``, the same primary section) over every draft AND every pinned family, computed before
anything is filtered. A component holding a pinned development family, or a draft tied to a public question (a
twin verdict naming a public id, or a shared 8-token window), is sealed-ineligible as a whole and its edges are
not passed. In round 2 a draft in a component holding a round-1 sealed family is *dropped* (to
``sealed/quarantine_r2.jsonl``), never joined.

**Group ids (``exclude``, ``freeze``, ``power`` and the slot gate).** One definition, one function
(``src.sealed_ledger.group_ids``): the connected components of the released rounds' ``edges`` over the families
that are *in the sealed set*, each named by its smallest member. The frozen sealed freeze stores those edges in the
``twins_sealed`` payload, which is the slot gate's group record (digest-checked against the freeze record), so
``exclude <group>`` and ``pipeline eval --sealed-slot`` can never name a group differently. Before the freeze the
sealed set is the surviving ``sealed_r*.jsonl`` families, so a pre-freeze exclusion is provisional.

**twins.** ``twins --round R --verdict A [--extra-verdict B ...] [--dev-verdict D] [--tutorials T]`` records the SP's
all-pairs verdict. The SP's verdict varies from run to run, so ``--extra-verdict`` (repeatable) adds further SP runs and a
pair flagged by ANY of them counts: the pairs used are the union of ``--verdict``, every extra and ``--dev-verdict``
(unordered pairs, de-duplicated, sorted). Every SP file (main and extras) goes through the same strict ``parse_verdict``
(``verdict_invalid`` / ``input_missing``), and ``twin_in_pool`` is judged over the union of the SP files (not ``--dev-verdict``).
An id named by a pair that is in no set (not in the pool, development, public or regression) is treated alike in every file:
it ties a pool draft it is paired with to a public family (sealed-ineligible) and otherwise has no effect. The record keeps
``verdict_sha256`` as the main file's sha256 and, only when extras were given, adds ``extra_verdict_sha256s`` (the extras'
file sha256s, sorted); the printed ``twins`` line gains ``extra_verdicts=<n>`` likewise. ``split`` and ``freeze`` read
``twin_pairs`` / ``families_digest`` / ``edges`` / ``dropped`` only, so they are unaffected. A payload is immutable. Without
extras the record is stored under ``twins_r<R>_<pool digest[:16]>`` (unchanged); with extras under
``twins_r<R>_<pool digest[:16]>_u<first 16 hex of the sha256 of the canonical JSON of the sorted union of the SP pairs
(main + extras, not ``--dev-verdict``)>``, so a union record can follow a no-extras record for the same pool, and a different
union gets a different name. Either way ``twins_current_r<R>.json`` is moved to the record just stored, which is what ``split``
and ``split --diagnose`` read. An identical rerun is idempotent; the same name with different content (the same union reached
through other files, or another ``--dev-verdict``) is refused (``ledger_protocol``) and nothing moves.

**split.** Needs the pushed prereg (a7's ancestry rule) declaring the round, a current ``twins`` record for the
pool, and passes the exact final quotas (``compute_quotas``) to ``split_families`` with the pinned families
preassigned. Round 1's realistic quota is the smallest that can still end inside the refusal band over its pinned
families and refusal families (``min_feasible_quota``, ``quota.dev_quota_rule``); tuning's is the rest of the pool.
``SplitInfeasible`` (or a tuning quota under tuning's own minimum feasible quota, ``tuning_floor``) records the attempt
and releases nothing; the first feasible attempt stores ``assign_r<R>`` and the release record, then moves the files
under the journal. Round 2 reuses round 1's released quotas and recomputes none.

``split --round R --diagnose`` runs the same gates and the same attempt (one code path: :func:`plan_split`, then
:func:`attempt_split`) but shows it instead of recording it: no ``split_attempt`` record, no payload, no release, no file moves,
and the attempt cap is untouched, so it can be run as often as wanted between real attempts. It does not recover a crashed
release first (that moves files): if a recovery is pending it is refused (``recovery_pending``). Output, counts and enum words
only, through the same printer: ``diagnose round=R outcome=<feasible|infeasible|quota_short|register_short>``; a feasible one
then prints ``diagnose_release`` (the per-split totals a real split would release); an infeasible one prints a
``diagnose_constraint`` line for EVERY entry of ``SplitInfeasible.counts`` (violated or not, the entry's own integers under its
own key names, ``violated=yes|no`` last); then always ``diagnose_pool``, ``diagnose_split`` (per split), ``diagnose_register``
and ``diagnose_drafts`` (per draft class and scope).

**paraphrase-apply.** ``paraphrase-apply --dispatch <id>`` adds the SP's new phrasings (the ``paraphrase`` task) to families
that are already released. It reads ``sp_out/<id>/pool.jsonl`` (through the sealed loader) and ``sp_out/<id>/dev.jsonl`` (as
schema-2 rows; the file lies in the sealed tree, so a byte copy under ``work/`` is what ``eval_schema.load_v2`` validates), at
least one of which must exist. It is a state command: it takes the state lock and first finishes any crashed transaction
(``recover_state``).
Everything is checked before anything is written: a ``pool.jsonl`` row names a sealed-side family (a ``sealed_r*.jsonl``
family that is not quarantined), a ``dev.jsonl`` row a development family (one that sits in exactly one development component
file, not quarantined); a family of the other side is ``wrong_side``, an unknown one ``unknown_family``, one in a frozen split
``frozen``. Per family the new rows' numbers are exactly the next ones after its highest row number (``row_index``); each new
row has the scope, evidence, gaps, type and source of the family's row 0, a valid ``register`` and no review status
(``row_mismatch``) and a question that, case-folded and with whitespace collapsed, equals no phrasing of the family, old or new
(``question_repeat``). Afterwards every touched family holds at most ``drafting_rule.paraphrase.phrasings_max`` rows (a migrated
tutorial-sourced one at most ``t1_t7_cap``: ``over_cap``) and at least ``phrasings_min`` (``under_min``). A dispatch is applied
once (``paraphrase_applied.json``: ``already_applied``). Each affected file is then rewritten with its old rows plus the new
ones (sealed files marked, development files unmarked), as one transaction decided first and journalled
(``W/paraphrase_<id>.journal``, the new bytes in ``W/paraphrase_<id>.data/``): the files are replaced one by one by atomic writes
(each only if it still holds the journalled old bytes or already the new ones), then the dispatch is recorded and the
journal removed. A crash anywhere is finished from the journal by the next command (``paraphrase-apply`` itself, which then
prints the same counts, or any state-changing one), never by recomputing; a file that holds neither the old nor the new bytes
is ``paraphrase_recovery_failed`` (a D64 stop). It prints counts only: per side the families touched and the rows added, the
phrasing-count histogram of the side after applying (families with 1, 2, 3 and more than 3 rows) and the two input sha256s.
Adding rows to a family changes nothing the release or the freeze depend on: the stored payload (``assign_r<R>``) holds family
records (row 0's scope, chapter, source and register), ids, edges and the pool digest of the rows that *left* ``pool/``, and
``freeze``'s replay, its "files still hold the assignment" check and the group computations read family membership only. The
new rows do change what ``freeze`` hashes at its recheck screen (``content_digest``), by design: the SP's ``recheck`` must run
on the final content, paraphrases included, and a screen written before is stale (``recheck_stale``). They need approvals like
any other row (``sheet`` lists them, ``freeze`` stops at ``approval_missing`` until they are approved).

**freeze.** Without a verdict it writes the lexical-screen pairs and a content digest to ``W/screen_<split>.json``
and stops (``recheck_missing``, with ``pairs_required``). With ``--recheck-verdict`` / ``--recheck-return`` (the
SP's ``twins.jsonl`` and ``return.json``) it checks, in this order and with no side effect before step 8:
(1) replay of every released round from its stored payload; (2) the files still hold the assignment; (3) the
verdict is for THIS content (digest, mtime, return ok, enough pairs); (4) twins: a sealed twin of development
demotes its whole group to quarantine (approvals kept; after the sealed freeze, an exclusion instead), a twin
inside sealed drops the later group; (5) approvals bound by row hash; (6) 2-3 phrasings (D64 stop); (7) band,
strata, min_share, group invariant, floor, with the sealed band repaired by the fewest seeded group demotions;
(8) commit: the WHOLE transaction (the frozen file's bytes, the twins record, the demoted groups, the exclusions) is
computed first and journalled to ``W/freeze_<split>.journal`` (+ ``.data``); then, in this order and each step
idempotent, the frozen file (mode 0444), the ``twins_sealed_<sha16>`` payload, the ledger record, the exclusions and
LAST the quarantine moves. A crash anywhere is finished from the journal by the next command (``freeze`` itself or any
state-changing one), never by recomputing from a world the transaction had half changed. The sealed freeze binds the
payload's sha256 (the rounds' twins records, the recheck, the demotions and the group ``edges``), the frozen file's
sha256 and the exclusions recorded before the freeze (the ledger computes that digest inside the freeze transaction).
After the sealed freeze a twin found by a later development recheck never moves a sealed family: it is an
**exclusion** (naming its member families), and groups are built from the frozen file.

**prereg** (``eval/prereg_16a2.json``): ``version``, ``branch``, ``rounds``, ``seeds`` (``split_r1``, ``split_r2``,
``repair``, ``calibration``), ``quota`` (``sealed``, ``spare``, ``floor_sealed``, ``floor_answerable``,
``realistic_min``, ``tuning_min``, the text of the quota rules ``dev_quota_rule`` / ``realistic_rule`` /
``tuning_floor_rule`` / ``tuning_rule``, ``examples`` with the preassigned families and refusal families of each
development split), ``band``, ``mwe``, ``alpha``, ``k_max``, ``draws_r``, ``power``
(``alphas`` = [alpha, alpha / k_max], ``discordance``, ``planned_groups``, ``draws``, ``seed``, ``table``,
``family_table``), optional ``strata``, ``min_share`` (keyed by split; the source class ``tutorial_or_colleague``
pools those two sources) and the b1a blocks, all required and checked against their registered values:
``primary_metric`` (cut-off 6, hybrid+rewrite, strict_hit), ``collapse`` (``{"rule": "all"}``), ``adoption`` (rule a,
the confirmatory batch, the development evidence, unsupported claims and control flip definitions), ``controls``
(rule c1, rank ceiling 3), ``slots`` (0 and 0b live; 1-4 are declared ``batch``: each one's mode, replay of
``slot0_canonical`` or a rewrite-changing candidate, is its batch entry's), ``drafting_rule`` (refusal share 0.275
over the whole pool, at most 3 round-1 attempts and 3 round-2 attempts, the lay share of the new lay or terse drafts,
and attempt 1's draft count: ``max(families_approx, (sealed + tuning floor + realistic quota) - migrated) + draft_margin``),
``incumbent`` (the pipeline identity slots 0 and 0b must run under: ``pipeline_config_v4``, its components and the registered
list of ``src`` modules left out of the code hash) and
``judge`` (the registered judge every sealed slot runs). **One prereg per epoch:** the first ledger record that carries a ``prereg_sha256`` (a ``data`` dispatch, usually: the
launcher records the prereg it ran under) registers it; every dispatch, ``split``, ``freeze``, ``calibrate``,
``declare-batch``, the slot gate and ``controls.py`` refuse any other (``prereg_amended``). ``epoch`` prints the
registered sha256 and whether the working prereg is it. **Every registered value** is one entry of
:data:`REGISTERED_VALUES` (a table of dotted path and exact value) that ``prereg-check`` walks; a leaf under a
registered root that is not in the table is ``unknown_key``. ``prereg-check`` recomputes
``quota.examples`` (and their ``label``), ``power.table`` and ``power.family_table``, pins the quota numbers, the quota rule
text and the minimum shares (:data:`PINNED_VALUES`) and checks the values the code computes: ``incumbent.components`` is the code's
``PIPELINE_CONFIG_KEYS`` and ``judge.model`` / ``judge.prompt_version`` / ``judge.config_hash`` are ``src.judge``'s
(:data:`COMPUTED_VALUES`). The same file is the
slot gate's prereg (``pipeline eval --prereg``): the gate takes the floor from ``quota.floor_sealed`` /
``quota.floor_answerable`` and the expansion mode of each slot from ``slots`` (``{"0": {"expansion": "live"}, ...}``).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import hashlib
import inspect
import io
import itertools
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src import eval_privacy  # noqa: E402  (module import: tests relocate private_root)
from src import eval_schema  # noqa: E402
from src import eval_sets  # noqa: E402
from src import eval_stats  # noqa: E402
from src import sealed_inputs  # noqa: E402
from src.eval_sealed_read import NotSealedInput, load_sealed  # noqa: E402  (the only sealed reader)
from src.eval_split import SplitInfeasible, SplitInputError, families_digest, split_families  # noqa: E402
from src.sealed_ledger import (  # noqa: E402
    DrawBindingRefused,
    DrawCoverageRefused,
    EpochStop,
    LedgerCorrupt,
    LedgerRefused,
    MirrorTampered,
    ProtocolError,
    SealedLedger,
    SealedLedgerError,
    exclusions_digest,
    group_ids,
)
from scripts import scan_leaks as sl  # noqa: E402  (tokenisation, windows, S-needles, exemptions)
from scripts import sp_launch as sp  # noqa: E402  (return validator, journaled move)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_HIT = 5
EXIT_SPLIT_INFEASIBLE = 6
EXIT_AUDIT_REFUSED = 7  # audit cannot give a complete answer (not a finding): absent dump, live staging
EXIT_REFUSED = 8
EXIT_FREEZE_INFEASIBLE = 9
EXIT_LEDGER_STOP = 10

SCOPES = ("answer", "partial", "refuse")
REGISTERS = ("lay", "modelled", "terse")
SOURCES = ("handbook", "tutorial", "colleague", "legacy")
SPLITS = ("sealed", "tuning", "realistic")
DEV_SPLITS = ("tuning", "realistic")
SPLIT_ALIASES = {
    "sealed": "sealed", "heldout-v2": "sealed", "heldout_v2": "sealed",
    "tuning": "tuning", "tuning-v2": "tuning", "tuning_v2": "tuning",
    "realistic": "realistic", "realistic-v2": "realistic", "realistic_v2": "realistic",
}
KINDS = ("sealed", "development")
MODELS = ("independent", "correlated")
TARGET_CLASSES = ("transcript", "memory", "log", "research", "work", "run", "dispatch", "staging", "tree", "history")
EXCLUSION_REASONS = ("s_hit", "exposure", "twin_dev_version")
#: The reasons ``exclude`` refuses once slot 0 (or 0b) is stored (``exclusion_late``): every one (r8). Once a slot's results
#: are stored the only exclusions are the in-process publication scanner's own ``s_hit`` ones (``src.sealed_run``), which
#: record the slot whose report was scanned (``scan_slot``); the owner's command writes none of them.
LATE_EXCLUSION_REASONS = EXCLUSION_REASONS
FROZEN_FILES = {"sealed": ("sealed", "heldout-v2.jsonl"), "tuning": ("dev", "tuning-v2.jsonl"), "realistic": ("dev", "realistic-v2.jsonl")}
BAND_MIN_NUM, BAND_MIN_DEN = 1, 4  # 25 %
BAND_MAX_NUM, BAND_MAX_DEN = 3, 10  # 30 %
OWNER_MARKS = ("A", "C", "R", "E")
SHEET_COLUMNS = (
    "row_id", "family_id", "row_hash", "phrasing_no", "phrasing", "scope", "register", "source",
    "evidence", "gaps", "ambiguous", "A", "C", "R", "E", "corrections", "doubt", "colleague_key",
)
CONFIRM_COLUMNS = ("row_id", "family_id", "row_hash", "phrasing", "scope", "evidence", "gaps", "ambiguous", "OK")
CORRECTABLE = ("question", "scope", "evidence", "gaps", "ambiguous", "register")
RETURN_ENUMS = (
    "status", "ok", "blocked", "kind", "draft", "twins", "paraphrase", "recheck", "probe", "families", "rows",
    "by_scope", "by_register", "by_source", "sha256", "pairs_checked", "twin_pairs", "pool_families", "reason",
    "answer", "partial", "refuse", "lay", "modelled", "terse", "handbook", "tutorial", "colleague", "legacy",
    "inputs_missing", "task_unclear", "quota_unreachable", "allowed", "denied", "absent", "present",
)

#: Every refusal / error reason the tool can print (enum words).
REASONS = (
    "usage", "internal", "schema_invalid", "inventory_invalid", "input_missing", "input_invalid", "not_sealed",
    "prereg_missing", "prereg_invalid", "prereg_uncommitted", "prereg_modified", "prereg_not_pushed",
    "prereg_unchecked", "round_undeclared", "round1_missing", "already_released", "already_frozen",
    "pool_empty", "pool_changed", "pool_register_missing", "twins_missing", "twins_stale", "twin_in_pool",
    "tutorial_tag_missing", "verdict_invalid", "assignment_changed", "replay_mismatch", "approval_missing",
    "sheet_invalid", "sheet_stale", "sheet_conflict", "recheck_missing", "recheck_stale", "recheck_invalid",
    "phrasing_breach", "band_breach", "strata_breach", "min_share_breach", "group_invariant", "floor_breach",
    "twin_in_sealed", "calibration_short", "colleague_key_invalid", "frozen", "move_recovery_failed",
    "ledger_refused", "ledger_corrupt", "mirror_tampered", "epoch_stop", "ledger_protocol", "no_group",
    "inputs_invalid", "scan_source_invalid", "git_failed", "split_input_invalid", "freeze_infeasible",
    "round_order", "unknown_family", "nothing_to_do", "already_intaken", "tag_conflict",
    "path_outside", "locked", "freeze_recovery_failed", "dump_missing", "staging_live",
    "split_infeasible", "splits_not_frozen", "calibration_drawn", "batch_declared", "batch_invalid", "freeze_missing",
    "prereg_amended", "frozen_file_changed", "embargoed", "controls_missing", "band_unreachable", "draws_uncovered",
    "draws_mismatch", "exclusion_late",
    # split --diagnose (16A-2b)
    "recovery_pending",
    # scripts/migrate_dev_v2.py (16A-2b b2)
    "unresolved_partial", "pair_scope_conflict", "map_invalid", "id_collision", "count_mismatch",
    # paraphrase-apply (16A-2b b4)
    "wrong_side", "row_index", "row_mismatch", "question_repeat", "over_cap", "under_min", "already_applied",
    "paraphrase_recovery_failed",
)
#: Closed set of words the printer accepts as enum values.
ENUM_WORDS = frozenset(
    {
        *SCOPES, *REGISTERS, *SOURCES, *SPLITS, *KINDS, *MODELS, *TARGET_CLASSES, *EXCLUSION_REASONS, *REASONS,
        *RETURN_ENUMS, "none", "invalid", "released", "infeasible", "quota_short", "frozen", "ok", "clean",
        "hits", "already", "recheck_required", "owner_selected_sample", "random_sample_n15", "feasible",
        "register_short", "unreachable", "families_approx", "pool_floor", "explicit", "yes", "no", "held",
        # split_families constraint names (SplitInfeasible.counts keys)
        "quota_total", "eligibility", "band", "strata", "min_share", "preassignment_conflicts",
        "preassigned_ineligible", "unit_no_allowed_split", "search", "floor", "approvals",
        # prereg-check / inputs-check enums
        "missing", "wrong_type", "out_of_range", "unknown_key", "mismatch", "reused_id", "not_public_id",
        "uninventoried_label", "pairing", "conflicts",
        # split --diagnose: draft classes, the min_share source class, a stratum chapter that is not a number, a label not on the list
        "total", "sealed_eligible", "sealed_ineligible", "realistic_eligible", "dropped", "tutorial_or_colleague", "multi", "other",
    }
)
ID_FIELDS = frozenset({("audit_hit", "family")})  # the only places an opaque family id is printed
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAMILY_ID = sp.FAMILY_ID_RE  # one pattern set for opaque ids (the launcher's return validator owns it)
_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FIELD_PATH = re.compile(r"(?:<[a-z]+>|[a-z_]+)(?:\[[0-9]+\]|\.[a-z_]+)*")
_EXT_ID = re.compile(r"[A-Za-z0-9:_.-]{1,64}")  # a family id of any set (public ids are "q:<hex>")
_TAG = re.compile(r"[a-z0-9_]{1,24}")  # a stored tutorial tag (lower-case)
#: A tag as the SP writes it in ``out/tutorial_map.json`` (``intake``): tutorials 2-17 without 7 (never T01 or T07),
#: and title-document sets 1-7. Stored lower-case, which is what ``_TAG`` accepts.
_SP_TAG = re.compile(r"T0[2-689]|T1[0-7]|TD[1-7]")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
MAX_PRINT_INT = 10**9


class PrintRefused(Exception):
    """A value the printer will not print (a bug, never data): fixed message, no value."""


class Stop(Exception):
    """A clean stop: an exit code, a reason enum and optional printable (allowlisted) fields."""

    def __init__(self, code: int, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.fields = fields


class Printer:
    """The one allowlisted printer (D72 trusted-processor rule).

    ``emit`` writes ``kind key=value ...`` to stdout; ``error`` writes ``error reason=<enum> ...`` to
    stderr. Values: non-negative ints (at most ``10**9``), sha256 hex, enum words in
    :data:`ENUM_WORDS`, and an opaque family id only for the ``(kind, key)`` pairs in
    :data:`ID_FIELDS`. A ``field`` key takes a schema field path (``evidence[0][1]``) built from
    schema key names. Anything else raises :class:`PrintRefused`.
    """

    def __init__(self, out: Any = None, err: Any = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr

    @staticmethod
    def _value(kind: str, key: str, value: Any) -> str:
        if isinstance(value, bool):
            raise PrintRefused("bool")
        if isinstance(value, int):
            if not 0 <= value <= MAX_PRINT_INT:
                raise PrintRefused("int out of range")
            return str(value)
        if isinstance(value, str):
            if _SHA256.fullmatch(value) or value in ENUM_WORDS:
                return value
            if (kind, key) in ID_FIELDS and _FAMILY_ID.fullmatch(value):
                return value
            if key == "field" and _FIELD_PATH.fullmatch(value) and len(value) <= 64:
                return value
        raise PrintRefused("value is not on the allowlist")

    def _line(self, head: str, fields: Mapping[str, Any], kind: str) -> str:
        parts = [head]
        for key, value in fields.items():
            if not _KEY.fullmatch(key):
                raise PrintRefused("key")
            parts.append(f"{key}={self._value(kind, key, value)}")
        return " ".join(parts)

    def emit(self, name: str, /, **fields: Any) -> None:
        if not _KIND.fullmatch(name):
            raise PrintRefused("kind")
        self.out.write(self._line(name, fields, name) + "\n")

    def error(self, reason: str, /, **fields: Any) -> None:
        if reason not in ENUM_WORDS:
            raise PrintRefused("reason")
        self.err.write(self._line(f"error reason={reason}", fields, "error") + "\n")

    def raw_validated(self, line: str) -> None:
        """Print a line that a validator already proved safe (``return``)."""
        self.out.write(line + "\n")


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------
def canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no spaces, ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: ``write_atomic``'s temp name is ``.<name>.<2n hex digits>.tmp`` with n random bytes; the pattern that finds a
#: crash's leftover (:func:`remove_atomic_leftovers`) is built from the same constant, so the two cannot drift apart.
_ATOMIC_TMP_BYTES = 3
_ATOMIC_TMP = re.compile(r"\..+\.[0-9a-f]{%d}\.tmp" % (2 * _ATOMIC_TMP_BYTES), re.DOTALL)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically: temp file, fsync, rename, fsync the directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(_ATOMIC_TMP_BYTES)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path, reason: str = "input_invalid") -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except OSError:
        raise Stop(EXIT_USAGE, "input_missing") from None
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Stop(EXIT_USAGE, reason) from None


def write_json(path: Path, obj: Any) -> str:
    """Write canonical JSON atomically; returns its sha256."""
    data = (canon(obj) + "\n").encode("ascii")
    write_atomic(path, data)
    return hashlib.sha256(data).hexdigest()


def resolve_split(name: str) -> str:
    if name not in SPLIT_ALIASES:
        raise Stop(EXIT_USAGE, "usage")
    return SPLIT_ALIASES[name]


# ---------------------------------------------------------------------------
# Context: every path is injectable
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    """Where everything lives. Tests pass tmp paths; production uses the defaults."""

    printer: Printer
    private: Path
    repo: Path
    ledger_path: Optional[Path] = None
    mirror: Optional[Path] = None
    inventory_path: Optional[Path] = None
    prereg_path: Optional[Path] = None
    handbook_dump: Optional[Path] = None
    projects_root: Optional[Path] = None
    staging_root: Optional[Path] = None
    _inventory: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # -- locations -------------------------------------------------------------
    @property
    def sealed_dir(self) -> Path:
        return self.private / "sealed"

    @property
    def pool_dir(self) -> Path:
        return self.sealed_dir / "pool"

    @property
    def sp_out(self) -> Path:
        """Where ``sp_launch`` releases an SP run's ``out/`` (``<dispatch id>/pool.jsonl`` ...)."""
        return self.sealed_dir / "work" / "sp_out"

    @property
    def sealed_work(self) -> Path:
        return self.sealed_dir / "work"

    @property
    def dev_dir(self) -> Path:
        return self.private / "dev"

    @property
    def dev_work(self) -> Path:
        return self.private / "work"

    @property
    def journal(self) -> Path:
        return self.sealed_work / "release.journal"

    def work_for(self, kind: str) -> Path:
        return self.sealed_work if kind == "sealed" else self.dev_work

    @property
    def prereg_file(self) -> Path:
        return self.prereg_path if self.prereg_path is not None else self.repo / "eval" / "prereg_16a2.json"

    @property
    def mirror_file(self) -> Path:
        return self.mirror if self.mirror is not None else self.repo / "eval" / "sealed_ledger.jsonl"

    def inventory(self) -> Dict[str, Any]:
        if self._inventory is None:
            self._inventory = eval_schema.load_inventory(self.inventory_path)
        return self._inventory

    def ledger(self) -> SealedLedger:
        return SealedLedger(self.sealed_dir / "ledger", mirror=self.mirror_file, ledger=self.ledger_path)


def default_ctx(printer: Printer, **overrides: Any) -> Ctx:
    """A context from the (possibly relocated) private root; ``overrides`` replace any field."""
    private = Path(overrides.pop("private", None) or eval_privacy.private_root())
    repo = Path(overrides.pop("repo", None) or private.parent.parent)
    values = {k: (Path(v) if v is not None else None) for k, v in overrides.items()}
    return Ctx(printer=printer, private=private, repo=repo, **values)


# ---------------------------------------------------------------------------
# One lock for every state-changing command; names and destinations that cannot escape
# ---------------------------------------------------------------------------
LOCK_NAME = ".lock"
#: Commands that write anything (ledger, rows, approvals, sheets, forms, pool, ids). The read-only ones
#: (``digest``, ``count-migrated``, ``inputs-check``, ``power``, ``prereg-check``, ``epoch``, ``draws``, ``return``, ``audit``) take no lock.
#: ``split`` is listed whether or not it is ``--diagnose`` (which writes nothing): a diagnosis must never interleave with a split.
STATE_COMMANDS = frozenset(
    {
        "ids", "intake", "twins", "split", "paraphrase-apply", "sheet", "apply", "freeze", "escalate", "calibrate", "disagree",
        "exclude", "declare-batch",
    }
)
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


@contextlib.contextmanager
def state_lock(ctx: Ctx) -> Iterator[None]:
    """Hold ``sealed/work/.lock`` exclusively, without waiting, for a whole state-changing command.

    A second command finds it held and is refused (``locked``) before it reads or changes anything, so
    ``exclude`` cannot land between a freeze's digest and its record, nor two ``apply`` runs interleave.
    Closing the descriptor (or the process dying) releases the flock.
    """
    ctx.sealed_work.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(ctx.sealed_work / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop(EXIT_REFUSED, "locked") from None
        yield
    finally:
        os.close(fd)


def checked_name(name: Optional[str]) -> Optional[str]:
    """A ``--name``: a plain basename of ``[A-Za-z0-9_-]{1,64}`` (never a path), else a usage refusal."""
    if name is not None and not _NAME.fullmatch(name):
        raise Stop(EXIT_USAGE, "usage")
    return name


def safe_dest(root: Path, subdir: str, filename: str) -> Path:
    """``root/subdir/filename``, proved (symlink-safe) to lie inside the real ``root/subdir``.

    Refused (``path_outside``) if ``filename`` holds a separator, if ``subdir`` (or an ancestor up to ``root``)
    is a symlink that leads elsewhere, or if the destination itself is a symlink. Nothing is created here.
    """
    if not filename or "/" in filename or os.sep in filename or "\0" in filename:
        raise Stop(EXIT_USAGE, "path_outside")
    real_base = os.path.join(os.path.realpath(root), subdir)
    base = root / subdir
    dest = base / filename
    if os.path.realpath(base) != real_base or os.path.islink(dest) or os.path.realpath(dest) != os.path.join(real_base, filename):
        raise Stop(EXIT_USAGE, "path_outside")
    return dest


# ---------------------------------------------------------------------------
# Rows, families, files
# ---------------------------------------------------------------------------
def row_no(row: Mapping[str, Any]) -> int:
    return int(str(row["id"]).rsplit("-", 1)[1])


def row_key(row: Mapping[str, Any]) -> Tuple[str, int]:
    return (str(row["family_id"]), row_no(row))


@dataclass
class Fam:
    """One family (a scenario, one scope): its rows, where they live and (dev) its split."""

    fid: str
    rows: List[Dict[str, Any]]
    path: Optional[Path] = None
    split: Optional[str] = None

    @property
    def scope(self) -> str:
        return str(self.rows[0]["scope"])

    @property
    def source(self) -> Optional[str]:
        return self.rows[0].get("source")

    @property
    def register(self) -> Optional[str]:
        return self.rows[0].get("register")

    @property
    def primary(self) -> Optional[str]:
        """The primary section: the first label of the first evidence group."""
        evidence = self.rows[0]["evidence"]
        return evidence[0][0] if evidence and evidence[0] else None

    @property
    def chapter(self) -> str:
        if self.scope == "refuse":
            return "refuse"
        chapters = {label.split(".")[0] for group in self.rows[0]["evidence"] for label in group}
        return chapters.pop() if len(chapters) == 1 else "multi"


def group_families(rows: Iterable[Mapping[str, Any]], path: Optional[Path] = None, split: Optional[str] = None) -> Dict[str, Fam]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by.setdefault(str(row["family_id"]), []).append(dict(row))
    return {fid: Fam(fid, sorted(rs, key=row_no), path, split) for fid, rs in sorted(by.items())}


def dump_rows(rows: Iterable[Mapping[str, Any]], *, mark: bool) -> bytes:
    """JSONL bytes for normalised rows: ``None`` keys dropped, ``sealed: true`` iff ``mark``."""
    lines = []
    for row in sorted(rows, key=row_key):
        out = {k: v for k, v in row.items() if v is not None}
        if not out.get("gaps"):
            out.pop("gaps", None)
        if mark:
            out["sealed"] = True
        else:
            out.pop("sealed", None)
        lines.append(json.dumps(out, sort_keys=True, ensure_ascii=True))
    return ("\n".join(lines) + "\n").encode("ascii") if lines else b""


def load_sealed_rows(ctx: Ctx, path: Path) -> List[Dict[str, Any]]:
    """Sealed rows, through the one sanctioned loader."""
    try:
        return load_sealed(path, inventory=ctx.inventory())
    except NotSealedInput:
        raise Stop(EXIT_USAGE, "not_sealed") from None


def load_dev_rows(ctx: Ctx, path: Path) -> List[Dict[str, Any]]:
    return eval_schema.load_v2(path, inventory=ctx.inventory())


def pool_files(ctx: Ctx) -> List[Path]:
    if not ctx.pool_dir.is_dir():
        return []
    return sorted(
        p for p in ctx.pool_dir.rglob("*.jsonl")
        if p.is_file() and not p.is_symlink() and not p.name.startswith("twins")
    )


def sealed_components(ctx: Ctx) -> List[Path]:
    return sorted(p for p in ctx.sealed_dir.glob("sealed_r*.jsonl") if p.is_file()) if ctx.sealed_dir.is_dir() else []


def quarantine_files(ctx: Ctx, kind: str) -> List[Path]:
    base = ctx.sealed_dir if kind == "sealed" else ctx.dev_dir
    return sorted(p for p in base.glob("quarantine*.jsonl") if p.is_file()) if base.is_dir() else []


def quarantine_target(ctx: Ctx, kind: str) -> Path:
    return (ctx.sealed_dir if kind == "sealed" else ctx.dev_dir) / "quarantine.jsonl"


def dev_split_of(path: Path) -> Optional[str]:
    for name in DEV_SPLITS:
        if path.stem.startswith(name):
            return name
    return None


def dev_components(ctx: Ctx) -> List[Path]:
    """Development files that are not frozen outputs and not quarantine."""
    if not ctx.dev_dir.is_dir():
        return []
    frozen = {FROZEN_FILES[s][1] for s in DEV_SPLITS}
    return sorted(
        p for p in ctx.dev_dir.glob("*.jsonl")
        if p.is_file() and p.name not in frozen and not p.name.startswith("quarantine")
    )


def load_pool(ctx: Ctx) -> Dict[str, Fam]:
    fams: Dict[str, Fam] = {}
    for path in pool_files(ctx):
        for fid, fam in group_families(load_sealed_rows(ctx, path), path).items():
            if fid in fams:
                raise Stop(EXIT_USAGE, "input_invalid")
            fams[fid] = fam
    return fams


def quarantined_ids(ctx: Ctx, kind: str) -> Set[str]:
    ids: Set[str] = set()
    for path in quarantine_files(ctx, kind):
        rows = load_sealed_rows(ctx, path) if kind == "sealed" else load_dev_rows(ctx, path)
        ids.update(str(r["family_id"]) for r in rows)
    return ids


def load_sealed_side(ctx: Ctx) -> Dict[str, Fam]:
    """Sealed component families that are not quarantined (survivors)."""
    gone = quarantined_ids(ctx, "sealed")
    fams: Dict[str, Fam] = {}
    for path in sealed_components(ctx):
        for fid, fam in group_families(load_sealed_rows(ctx, path), path, "sealed").items():
            if fid not in gone:
                fams[fid] = fam
    return fams


def load_dev(ctx: Ctx, *, include_quarantine: bool = False) -> Dict[str, Fam]:
    """Development component families (``split`` set from the file-name prefix)."""
    gone = set() if include_quarantine else quarantined_ids(ctx, "development")
    fams: Dict[str, Fam] = {}
    for path in dev_components(ctx):
        for fid, fam in group_families(load_dev_rows(ctx, path), path, dev_split_of(path)).items():
            if fid not in fams and fid not in gone:
                fams[fid] = fam
    return fams


def final_row_hashes(fams: Iterable[Fam]) -> Dict[str, str]:
    return {str(r["id"]): eval_schema.row_hash(r) for f in fams for r in f.rows}


def pool_digest(pool: Mapping[str, Fam]) -> str:
    """sha256 over every pool row's canonical JSON (rows ordered by family and phrasing)."""
    rows = sorted((r for f in pool.values() for r in f.rows), key=row_key)
    return sha_text("\n".join(canon(r) for r in rows))


def source_class(source: Optional[str]) -> str:
    """The source class min_share counts: tutorial and colleague together (D71 Q4)."""
    return "tutorial_or_colleague" if source in ("tutorial", "colleague") else (source or "legacy")


def realistic_ok(fam: Fam) -> bool:
    """Register eligibility: realistic takes lay or terse drafts and tutorial-sourced families."""
    return fam.register in ("lay", "terse") or fam.source == "tutorial"


def family_record(fam: Fam, eligible: Sequence[str]) -> Dict[str, Any]:
    return {
        "family_id": fam.fid, "scope": fam.scope, "chapter": fam.chapter, "source": source_class(fam.source),
        "register": fam.register or "unspecified", "eligible": sorted(eligible),
    }


# ---------------------------------------------------------------------------
# Verdict files, tutorial map, lexical screen
# ---------------------------------------------------------------------------
def parse_verdict(path: Path) -> List[Tuple[str, str]]:
    """The SP's ``twins.jsonl``: one ``{"a", "b", "verdict": "twin"}`` per line; strict."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    pairs: List[Tuple[str, str]] = []
    bad = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        ok = (
            isinstance(obj, dict) and set(obj) == {"a", "b", "verdict"} and obj["verdict"] == "twin"
            and isinstance(obj["a"], str) and isinstance(obj["b"], str)
            and _EXT_ID.fullmatch(obj["a"]) and _EXT_ID.fullmatch(obj["b"]) and obj["a"] != obj["b"]
        )
        if not ok:
            bad += 1
            continue
        pairs.append((obj["a"], obj["b"]))
    if bad:
        raise Stop(EXIT_USAGE, "verdict_invalid", lines=bad)
    return sorted({tuple(sorted(p)) for p in pairs})  # type: ignore[misc]


def load_tutorial_map(ctx: Ctx, path: Optional[Path] = None) -> Dict[str, str]:
    p = path if path is not None else ctx.sealed_work / "tutorial_map.json"
    if not p.is_file():
        return {}
    doc = read_json(p)
    if not isinstance(doc, dict) or not all(isinstance(k, str) and isinstance(v, str) and _TAG.fullmatch(v) for k, v in doc.items()):
        raise Stop(EXIT_USAGE, "input_invalid")
    return dict(doc)


def tokens_of(text: str) -> List[str]:
    return sl.normalise_tokens(text)


def window_set(toks: Sequence[str]) -> Set[Tuple[str, ...]]:
    return {tuple(toks[i : i + sl.WINDOW]) for i in range(len(toks) - sl.WINDOW + 1)}


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


def lexical_pairs(sealed: Mapping[str, Fam], dev: Mapping[str, Fam]) -> List[Tuple[str, str]]:
    """Family pairs ``(sealed, development)`` sharing an 8-token window or a whole short question.

    The lexical screen only *orders* what goes to the SP (R6): it never decides a twin. Uses the
    scanner's tokenisation and window length; no exemption is applied, so the SP sees every pair.
    """
    dev_windows: Dict[Tuple[str, ...], Set[str]] = {}
    dev_short: List[Tuple[str, List[str]]] = []
    dev_all: List[Tuple[str, List[str]]] = []
    for fid, fam in dev.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            dev_all.append((fid, toks))
            if len(toks) < sl.WINDOW:
                dev_short.append((fid, toks))
            for win in window_set(toks):
                dev_windows.setdefault(win, set()).add(fid)
    pairs: Set[Tuple[str, str]] = set()
    for sid, fam in sealed.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            for win in window_set(toks):
                pairs.update((sid, d) for d in dev_windows.get(win, ()))
            if len(toks) < sl.WINDOW:
                pairs.update((sid, d) for d, dt in dev_all if _contains(dt, toks))
            pairs.update((sid, d) for d, dt in dev_short if _contains(toks, dt))
    return sorted(pairs)


def public_tied_drafts(pool: Mapping[str, Fam]) -> Set[str]:
    """Drafts sharing an 8-token window with a public question, or equal to one (R7)."""
    hashes, windows = sl._public_questions()
    tied: Set[str] = set()
    for fid, fam in pool.items():
        for row in fam.rows:
            toks = tokens_of(row["question"])
            if toks and (sha_text(" ".join(toks)) in hashes or window_set(toks) & windows):
                tied.add(fid)
    return tied


# ---------------------------------------------------------------------------
# Groups: connected components of ALL relations, before any filtering (r4, r5)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    kind: str  # draft | pinned | sealed_pin | quarantine
    tag: Optional[str] = None
    primary: Optional[str] = None


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def components(self) -> List[List[str]]:
        out: Dict[str, List[str]] = {}
        for item in self.parent:
            out.setdefault(self.find(item), []).append(item)
        return sorted(sorted(c) for c in out.values())


@dataclass
class GroupPlan:
    components: List[List[str]]
    status: Dict[str, str]  # draft id -> free | ineligible | dropped
    edges: List[List[str]]
    groups: Dict[str, List[str]]  # group id (smallest member) -> members, for free groups of >= 2


def compute_groups(nodes: Mapping[str, Node], pairs: Iterable[Tuple[str, str]], public_tied: Set[str]) -> GroupPlan:
    """Components of all relations (twin verdict, same source tutorial, same primary section).

    Computed over EVERY node (drafts and pinned families) before anything is filtered. Then, per
    component: a draft in a component holding a round-1 sealed family is ``dropped`` (never joined);
    else a draft in a component holding a pinned development family, or tied to a public question, is
    ``ineligible`` (the whole group, so its edges are never passed); otherwise the drafts are ``free``
    and a chain of edges ties them (the keep-together unit for ``split_families``).
    """
    uf = _UnionFind(nodes)
    external_tied: Set[str] = set(public_tied)
    for a, b in pairs:
        if a in nodes and b in nodes:
            uf.union(a, b)
        elif a in nodes and nodes[a].kind == "draft":
            external_tied.add(a)  # a twin of a public / regression / unknown family
        elif b in nodes and nodes[b].kind == "draft":
            external_tied.add(b)
    for attr in ("tag", "primary"):
        first: Dict[str, str] = {}
        for fid in sorted(nodes):
            value = getattr(nodes[fid], attr)
            if value is None:
                continue
            if value in first:
                uf.union(first[value], fid)
            else:
                first[value] = fid
    status: Dict[str, str] = {}
    edges: List[List[str]] = []
    groups: Dict[str, List[str]] = {}
    comps = uf.components()
    for comp in comps:
        kinds = {nodes[x].kind for x in comp}
        drafts = [x for x in comp if nodes[x].kind == "draft"]
        if not drafts:
            continue
        if "sealed_pin" in kinds:
            verdict = "dropped"
        elif "pinned" in kinds or any(d in external_tied for d in drafts):
            verdict = "ineligible"
        else:
            verdict = "free"
        for d in drafts:
            status[d] = verdict
        if verdict == "free" and len(drafts) > 1:
            edges.extend([drafts[i], drafts[i + 1]] for i in range(len(drafts) - 1))
            groups[drafts[0]] = drafts
    return GroupPlan(comps, status, edges, groups)


# ---------------------------------------------------------------------------
# Pre-registration (b1): shape, quota formulas, power table, pushed gate
# ---------------------------------------------------------------------------
PREREG_KEYS = frozenset(
    {
        "version", "branch", "rounds", "seeds", "quota", "band", "strata", "min_share", "mwe", "alpha",
        "k_max", "draws_r", "power", "primary_metric", "collapse", "adoption", "controls", "slots", "drafting_rule",
        "incumbent", "judge",
    }
)
PREREG_REQUIRED = (
    "version", "branch", "rounds", "seeds", "quota", "band", "mwe", "alpha", "k_max", "draws_r", "power",
    "primary_metric", "collapse", "adoption", "controls", "slots", "drafting_rule", "incumbent", "judge",
)
PREREG_ERRORS = ("missing", "wrong_type", "out_of_range", "unknown_key", "mismatch")


def _is_int(x: Any, lo: int = 0) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= lo


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


#: The registered text of the quota rules (``quota.dev_quota_rule`` ... in the prereg, pinned by ``prereg-check``):
#: what :func:`min_feasible_quota`, :func:`compute_quotas` and :func:`tuning_floor` implement.
DEV_QUOTA_RULE = "max(min,F,ceil(4(F-R)/3),ceil(10R/3))"
REALISTIC_QUOTA_RULE = "dev_quota_rule_at_realistic_min"
TUNING_FLOOR_RULE = "dev_quota_rule_at_tuning_min"
TUNING_QUOTA_RULE = "pool_total-sealed-realistic_quota"


def band_bounds(quota: int) -> Tuple[int, int]:
    """``(lo, hi)``: the refusal-family counts a split of ``quota`` families may hold, 25 % rounded up to 30 % rounded down.

    Integer arithmetic only, from the same constants as the splitter's band (``src.eval_split._band``: ``4r >= q`` and
    ``10r <= 3q``); a test holds the two equal.
    """
    return -(-quota * BAND_MIN_NUM // BAND_MIN_DEN), quota * BAND_MAX_NUM // BAND_MAX_DEN


def min_feasible_quota(minimum: int, families: int, refusals: int) -> int:
    """The smallest quota of a development split with ``families`` preassigned (``refusals`` of them refusal families)
    that can still end inside the refusal band: ``max(min, F, ceil(4(F-R)/3), ceil(10R/3))`` (:data:`DEV_QUOTA_RULE`).

    Why each term. The split must hold its own families (``Q >= F``) and its registered minimum. Its ``Q - F`` open slots
    must be able to bring the refusal count up to ``ceil(Q/4)``: ``ceil(Q/4) <= R + Q - F``, i.e. ``Q >= ceil(4(F-R)/3)``.
    And the refusals already in it must not pass the ceiling: ``R <= floor(3Q/10)``, i.e. ``Q >= ceil(10R/3)``; adding
    refusals cannot fix that, only a bigger quota of non-refusal families can (the dilution term).
    The three bounds come from :data:`BAND_MIN_NUM` and friends, so they follow the band if it ever changes.

    The band itself is empty for a few small quotas (``ceil(Q/4) > floor(3Q/10)`` at Q = 5, 6, 9 and 13): a quota must
    also not be one of those, so the result is bumped past them. That never binds above 13, hence never for the registered
    minimums (42 and 60); the test worlds' tiny minimums do meet it.

    Raises:
        ValueError: negative inputs, or more refusals than families.
    """
    for value in (minimum, families, refusals):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("quota inputs must be non-negative integers")
    if refusals > families:
        raise ValueError("more refusal families than families")
    quota = max(
        minimum, families,
        -(-(families - refusals) * BAND_MIN_DEN // (BAND_MIN_DEN - BAND_MIN_NUM)),
        -(-refusals * BAND_MAX_DEN // BAND_MAX_NUM),
    )
    while band_bounds(quota)[0] > band_bounds(quota)[1]:
        quota += 1
    return quota


def refusal_interval(quota: int, families: int = 0, refusals: int = 0) -> Tuple[int, int]:
    """``(lo, hi)``: the final refusal-family counts a split can reach.

    A sealed split (nothing preassigned) reaches ``[ceil(Q/4), floor(3Q/10)]``. A development split with ``families``
    preassigned, ``refusals`` of them refusal families, reaches ``[max(R, ceil(Q/4)), min(R + Q - F, floor(3Q/10))]``: it
    keeps its refusals and can add one per open slot. ``lo > hi`` means the split cannot end inside the band.
    """
    low, high = band_bounds(quota)
    return max(refusals, low), min(refusals + quota - families, high)


def compute_quotas(quota: Mapping[str, Any], pool_total: int, pre_realistic: int, pre_realistic_refuse: int) -> Dict[str, int]:
    """The quotas of a round-1 split: sealed fixed; realistic = :func:`min_feasible_quota` at ``realistic_min`` over its
    preassigned families (``pre_realistic``, ``pre_realistic_refuse`` of them refusals); tuning = the rest of the pool."""
    sealed = int(quota["sealed"])
    realistic = min_feasible_quota(int(quota["realistic_min"]), int(pre_realistic), int(pre_realistic_refuse))
    return {"sealed": sealed, "realistic": realistic, "tuning": int(pool_total) - sealed - realistic}


def tuning_floor(quota: Mapping[str, Any], pre_tuning: int, pre_tuning_refuse: int) -> int:
    """Tuning's own minimum feasible quota: :func:`min_feasible_quota` at ``tuning_min`` over its preassigned families.

    A tuning quota below it is ``quota_short`` (the split could not end inside the band); it is also the tuning term of
    attempt 1's draft count (``drafting_rule.attempt_1.families_rule``).
    """
    return min_feasible_quota(int(quota["tuning_min"]), int(pre_tuning), int(pre_tuning_refuse))


def mc_power_pm(groups: Sequence[int], disc: float, effect: float, alpha: float, draws: int, seed: int, model: str) -> int:
    return round(1000 * eval_stats.cluster_signflip_power(groups, disc, effect, alpha, draws, seed, model))  # type: ignore[arg-type]


def mc_mde_hundredths(groups: Sequence[int], disc: float, alpha: float, draws: int, seed: int, model: str, target: float = 0.8) -> Optional[int]:
    """Smallest effect k/100 (k >= 1, up to the discordance) whose Monte Carlo power reaches ``target``."""
    k = 1
    while k <= disc * 100 + 1e-9:
        effect = min(k / 100, disc)
        if eval_stats.cluster_signflip_power(groups, disc, effect, alpha, draws, seed, model) >= target:  # type: ignore[arg-type]
            return k
        k += 1
    return None


def power_cells(power: Mapping[str, Any], mwe: float) -> List[Dict[str, Any]]:
    """The prereg's power table, recomputed: model-major, then alpha, then discordance."""
    cells = []
    for model in MODELS:
        for alpha in power["alphas"]:
            for disc in power["discordance"]:
                effect = min(mwe, disc)
                args = (power["planned_groups"], disc)
                cells.append(
                    {
                        "model": model, "alpha": alpha, "discordance": disc,
                        "power_pm": mc_power_pm(*args, effect, alpha, power["draws"], power["seed"], model),
                        "mde_hundredths": mc_mde_hundredths(*args, alpha, power["draws"], power["seed"], model),
                    }
                )
    return cells


def family_cells(power: Mapping[str, Any], mwe: float) -> List[Dict[str, Any]]:
    """The singleton-family table, recomputed from ``eval_stats``: per alpha, per discordance, n = all planned groups' families."""
    n = sum(power["planned_groups"])
    cells = []
    for alpha in power["alphas"]:
        for disc in power["discordance"]:
            ref = eval_stats.mde(n, disc, alpha)
            cells.append(
                {
                    "alpha": alpha, "discordance": disc, "families": n,
                    "power_pm": round(1000 * eval_stats.power(n, disc, min(mwe, disc), alpha, "directional")),
                    "mde_hundredths": round(100 * ref) if ref is not None else None,
                }
            )
    return cells


_ABSENT = object()


def _at(doc: Any, path: str) -> Any:
    """The value at a dotted path, or ``_ABSENT``."""
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return _ABSENT
        doc = doc[part]
    return doc


def _eq(a: Any, b: Any) -> bool:
    """Strict JSON equality (``True`` is not ``1``; ``1`` equals ``1.0`` only for numbers)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if _is_num(a) and _is_num(b):
        return a == b
    return type(a) is type(b) and a == b


#: Every registered value of the prereg outside the structural blocks, as ``(dotted path, exact value)``: the one
#: table ``prereg-check`` walks (b1a r3). A leaf is a scalar or a list of scalars; the tests build their prereg from
#: this table and fail when a leaf of ``eval/prereg_16a2.json`` outside :data:`STRUCTURAL_ROOTS` is not in it.
REGISTERED_VALUES: Tuple[Tuple[str, Any], ...] = (
    ("mwe", 0.1),
    ("alpha", 0.05),
    ("k_max", 4),
    ("draws_r", 5),
    ("primary_metric.cutoff", 6),
    ("primary_metric.metric", "strict_hit"),
    ("primary_metric.mode", "hybrid+rewrite"),
    ("primary_metric.set", "sealed"),
    ("primary_metric.unit", "family"),
    ("primary_metric.eligible_scopes", ["answer", "partial"]),
    ("primary_metric.estimand", "family_level_difference"),
    ("primary_metric.mwe_scale", "absolute"),
    ("primary_metric.test", "exact_cluster_signflip"),
    ("primary_metric.sided", "two"),
    ("primary_metric.direction", "sign_of_sum_d_g"),
    ("primary_metric.alpha_scope", "family_wise"),
    ("primary_metric.multiplicity", "holm"),
    ("primary_metric.sensitivities", ["durkalski_by_group", "mcnemar_exact_families"]),
    ("collapse.rule", "all"),
    ("adoption.rule", "a"),
    ("adoption.decision", "d74"),
    ("adoption.development_family_effect_min", 0.1),
    ("adoption.critical_control_flips_max", 0),
    ("adoption.sealed_point_estimate_min", 0),
    ("adoption.sealed_holm_significant_loss", "forbidden"),
    ("adoption.label", "adopted_on_development_evidence"),
    ("adoption.fail_if", ["critical_control_flip", "holm_significant_sealed_loss"]),
    ("adoption.departs_from", "design_003_s5_16b"),
    ("adoption.phase_21", "beats_single_pass_on_sealed"),
    ("adoption.embedding_arm_slot", "development_effect_ge_mwe"),
    ("adoption.rejected", ["b"]),
    ("adoption.confirmatory_batch.slots", ["1", "2", "3", "4"]),
    ("adoption.confirmatory_batch.k_max", 4),
    ("adoption.confirmatory_batch.declared", "one_non_adaptive_batch_before_first_confirmatory_slot"),
    ("adoption.confirmatory_batch.multiplicity", "holm"),
    ("adoption.confirmatory_batch.state", "refused_until_16b_sealed_comparison_gate"),
    ("adoption.confirmatory_batch.slots_declared_by", "batch_declaration_record_before_slot_1"),
    ("adoption.confirmatory_batch.expansion", "batch"),
    ("adoption.confirmatory_batch.replays", "slot0_canonical"),
    ("adoption.confirmatory_batch.rewrite_changing_candidate.expansion", "candidate"),
    ("adoption.confirmatory_batch.rewrite_changing_candidate.flag", "rewrite_candidate"),
    ("adoption.confirmatory_batch.rewrite_changing_candidate.arm", "live"),
    ("adoption.confirmatory_batch.rewrite_changing_candidate.against", "incumbent_replay"),
    ("adoption.confirmatory_batch.holm_k", 4),
    ("adoption.confirmatory_batch.spent_slot_counts", True),
    ("adoption.confirmatory_batch.modes", ["replay", "candidate", "abandoned"]),
    ("adoption.confirmatory_batch.mode_binding",
     "replay_entries_need_replay_draws_candidate_entries_need_build_draws_and_a_new_rewrite_identity"),
    ("adoption.confirmatory_batch.mode_declared_by", "batch_declaration"),
    ("adoption.confirmatory_batch.candidate_identity", "config_hash_is_pipeline_config_v4_checked_by_16b_gate_before_claim"),
    ("adoption.confirmatory_batch.rewrite_identity", "candidate_entry_rewrite_config_hash_is_rewrite_model_prompt_caps_checked_at_gate_before_claim"),
    ("adoption.confirmatory_batch.embargo_lifted_by", "16b_reviewed_code_change_with_identity_check"),
    ("adoption.confirmatory_batch.requires", ["controls_record", "judge_on", "registered_judge"]),
    ("adoption.development_evidence.metric", "strict_hit"),
    ("adoption.development_evidence.cutoff", 6),
    ("adoption.development_evidence.collapse", "all"),
    ("adoption.development_evidence.mode", "hybrid+rewrite"),
    ("adoption.development_evidence.sets", ["tuning", "realistic"]),
    ("adoption.development_evidence.versions", "development_freeze_records"),
    ("adoption.development_evidence.draws", 5),
    ("adoption.development_evidence.statistic", "mean_over_draws_of_family_difference"),
    ("adoption.development_evidence.pairing", "by_draw_index"),
    ("adoption.development_evidence.incumbent_draws", "first_5_recorded_development_draws_under_the_incumbent_config_hash"),
    ("adoption.development_evidence.candidate_draws", "replay_mode_replays_incumbent_draw_artifacts_else_first_5_recorded_draws_under_its_config_hash"),
    ("adoption.development_evidence.recorded_configs_max", 4),
    ("adoption.development_evidence.draws_recorded_by", "pipeline_eval_record_draw_in_run"),
    ("adoption.development_evidence.unrecorded_runs", "never_count_and_are_logged_per_config"),
    ("adoption.development_evidence.abandoned", "counts_toward_k"),
    ("adoption.development_evidence.fallback_draws", "void_logged_not_counted"),
    ("adoption.development_evidence.all_recorded_candidates_declared", True),
    ("adoption.scoring.identity", "scorer_identity_v1"),
    ("adoption.scoring.compare", "candidate_and_slot0_equal_and_counted_draws_equal_incumbent_draws"),
    ("adoption.scoring.covers", [
        "eval_cohort.py", "eval_schema.py", "eval_scoring.py", "eval_sealed_read.py", "eval_stats.py", "eval_v6.py",
        "judge_prep.py", "sealed_groups.py", "sealed_results.py", "scoring_glue.py", "text_utils.py",
        "generator.py:_sections_related",
        "generator.CAVEAT_PREFIX", "grounding.CITATIONS_UNVERIFIED", "grounding.CITATIONS_VERIFIED",
        "grounding.PARTIALLY_VERIFIED", "grounding.STATUS_COMPLETE", "grounding.TERMINAL_OUTCOMES",
        "eval/absorbed_sections.json", "eval/section_inventory.json",
    ]),
    ("adoption.exclusions",
     "confirmatory_comparison_uses_exclusions_recorded_before_slot0_store_plus_each_confirmatory_slots_own_s_hit_exclusions_applied_to_both_arms"),
    ("adoption.unsupported_claims.unit", "claim"),
    ("adoption.unsupported_claims.where", "candidate_sealed_slot"),
    ("adoption.unsupported_claims.against", "incumbent"),
    ("adoption.unsupported_claims.reference", "canonical_slot_0_judged_sealed_claims"),
    ("adoption.unsupported_claims.max_excess_abs", 0),
    ("adoption.control_flip.from", "strict_hit"),
    ("adoption.control_flip.to", "miss"),
    ("adoption.control_flip.cutoff", 6),
    ("adoption.control_flip.modes", ["hybrid", "hybrid+rewrite"]),
    ("adoption.control_flip.draws", "any_of_first_5_recorded_under_the_incumbent_config_hash"),
    ("adoption.control_flip.candidate_draws", "any_of_its_first_5_recorded_paired_by_draw_index"),
    ("adoption.control_flip.versions", "development_freeze_records"),
    ("controls.rule", "c1"),
    ("controls.decision", "d74"),
    ("controls.families", "public_lineage_development"),
    ("controls.metric", "strict_completion_rank"),
    ("controls.rank_ceiling", 3),
    ("controls.modes", ["hybrid", "hybrid+rewrite"]),
    ("controls.draws", "first_5_recorded_development_draws_under_the_incumbent_config_hash"),
    ("controls.draws_r", 5),
    ("controls.collapse", "all"),
    ("controls.computed", "once_on_frozen_development_versions"),
    ("controls.committed", "ledger_controls_record_before_slot_0"),
    ("controls.recomputed_mid_batch", False),
    ("controls.flip", "fail"),
    ("controls.reverses", "d57_addendum"),
    ("controls.rejected", ["c2"]),
    ("controls.binds_to", "development_freeze_records"),
    ("slots.0.expansion", "live"),
    ("slots.0.role", "descriptive_baseline"),
    ("slots.0.builds", "run_dir_expansion_artifact"),
    ("slots.0.canonical", "v6_guards"),
    ("slots.0b.expansion", "live"),
    ("slots.0b.role", "spare"),
    ("slots.0b.builds", "run_dir_expansion_artifact"),
    ("slots.0b.canonical", "v6_guards"),
    ("slots.0b.usable", "once_after_non_canonical_or_spent_slot_0"),
    ("slots.1.expansion", "batch"),
    ("slots.1.artifact", "slot0_canonical"),
    ("slots.1.role", "confirmatory"),
    ("slots.1.requires", "batch_declaration"),
    ("slots.2.expansion", "batch"),
    ("slots.2.artifact", "slot0_canonical"),
    ("slots.2.role", "confirmatory"),
    ("slots.2.requires", "batch_declaration"),
    ("slots.3.expansion", "batch"),
    ("slots.3.artifact", "slot0_canonical"),
    ("slots.3.role", "confirmatory"),
    ("slots.3.requires", "batch_declaration"),
    ("slots.4.expansion", "batch"),
    ("slots.4.artifact", "slot0_canonical"),
    ("slots.4.role", "confirmatory"),
    ("slots.4.requires", "batch_declaration"),
    ("drafting_rule.attempt_1.families_approx", 100),
    ("drafting_rule.attempt_1.draft_margin", 5),
    ("drafting_rule.attempt_1.families_rule", "max(families_approx,(sealed_quota+tuning_floor+realistic_quota)-migrated_families)+draft_margin"),
    ("drafting_rule.attempt_1.source_mix", "tutorial_sourced_about_half_rest_handbook"),
    ("drafting_rule.attempt_1.per_unseen_tutorial_approx", 3),
    ("drafting_rule.attempt_1.unseen_tutorials", 15),
    ("drafting_rule.attempt_1.per_title_document_set_max", 1),
    ("drafting_rule.attempt_1.title_document_sets", 7),
    ("drafting_rule.attempt_1.never_drafted_from", ["t01", "t07"]),
    ("drafting_rule.attempt_1.refusal_share_target", 0.275),
    ("drafting_rule.attempt_1.refusal_share_counts", "pool_including_preassigned"),
    ("drafting_rule.attempt_1.refusal_drafts", "max(round_half_up(0.275_x_pool_total)_minus_preassigned_refusals,sum_of_split_deficits_to_25pct)"),
    ("drafting_rule.attempt_1.seed_phrasings_per_family", 1),
    ("drafting_rule.attempt_1.registers", ["lay", "modelled", "terse"]),
    ("drafting_rule.attempt_1.conflict_label_families", "development_only"),
    ("drafting_rule.attempt_1.realistic_lay_min_share", 0.5),
    ("drafting_rule.attempt_1.realistic_lay_share_of", "new_drafts_registered_lay_or_terse"),
    ("drafting_rule.attempt_1.band_unreachable", "b2_owner_stop"),
    ("drafting_rule.next_attempt.when", ["quota_short", "infeasible"]),
    ("drafting_rule.next_attempt.action", "sp_drafts_more_before_next_attempt"),
    ("drafting_rule.next_attempt.split_seed", "same_per_round"),
    ("drafting_rule.next_attempt.released", "first_feasible_attempt"),
    ("drafting_rule.next_attempt.max_round_1_attempts", 3),
    ("drafting_rule.next_attempt.after_cap", "split_infeasible_stop"),
    ("drafting_rule.round_2.purpose", "replacements"),
    ("drafting_rule.round_2.order", ["round_1_split", "b5_validation", "sp_recheck", "round_2", "freeze"]),
    ("drafting_rule.round_2.quotas", "equal_round_1"),
    ("drafting_rule.round_2.related_to_round_1_sealed_group", "dropped"),
    ("drafting_rule.round_2.sources", "tutorials_and_primary_sections_unused_by_round_1_sealed_groups"),
    ("drafting_rule.round_2.surplus", "quarantine_r2"),
    ("drafting_rule.round_2.max_attempts", 3),
    ("drafting_rule.round_2.after_cap", "freeze_infeasible_stop"),
    ("drafting_rule.round_2.refusal_drafts", "same_formula_over_round_2_pool"),
    ("drafting_rule.paraphrase.phrasings_min", 2),
    ("drafting_rule.paraphrase.phrasings_max", 3),
    ("drafting_rule.paraphrase.migrated_top_up_to", 2),
    ("drafting_rule.paraphrase.t1_t7_cap", 3),
    ("drafting_rule.infeasible_after_round_2", "d64_owner_stop"),
    ("incumbent.config_fn", "pipeline_config_v4"),
    ("incumbent.components", [
        "version", "code", "runtime", "endpoint", "embedding", "index", "bm25", "fusion", "rewrite", "generation",
        "scoring_inputs", "scan_inputs",
    ]),
    ("incumbent.code", "all_src_modules_except_code_excluded_and_hashed_scripts"),
    ("incumbent.code_excluded", [
        "__init__.py", "audit.py", "eval_roster.py", "eval_split.py", "llm_transport.py", "render.py", "sealed_inputs.py",
        "tracing_guard.py",
    ]),
    ("incumbent.code_excluded_imports", [
        "audit.py:build_event", "audit.py:log_event",
        "llm_transport.py:GENERATION_MAX_RETRIES", "llm_transport.py:GENERATION_REQUEST_TIMEOUT",
        "llm_transport.py:REWRITE_MAX_RETRIES", "llm_transport.py:REWRITE_REQUEST_TIMEOUT",
        "render.py:RenderFlags", "render.py:render",
        "tracing_guard.py:TracingRefused", "tracing_guard.py:assert_tracing_off", "tracing_guard.py:scrub_tracing_env",
        "tracing_guard.py:tracing_problems",
    ]),
    ("incumbent.code_scripts", ["scan_leaks.py", "sp_launch.py"]),
    ("incumbent.code_scripts_inert_imports", ["sp_launch.py:sealed_tool.py"]),
    ("incumbent.excluded", ["timeouts_retries", "git_sha", "paths", "eval_set_contents_bound_by_freeze_records", "corpus_text"]),
    ("incumbent.fixed_by", "controls_record_incumbent_config_hash"),
    ("incumbent.draws", "each_counted_draw_sidecar_carries_the_incumbent_config_hash"),
    ("incumbent.slots", ["0", "0b"]),
    ("incumbent.candidates", "batch_entry_config_hash_is_pipeline_config_v4_of_the_candidate"),
    ("judge.required_on", ["0", "0b", "1", "2", "3", "4"]),
    ("judge.model", "claude-sonnet-5"),
    ("judge.prompt_version", "faithfulness-judge-v1"),
    ("judge.sample", "all_eligible"),
    ("judge.eligible", "retrieval_eligible_rows_with_complete_non_refusal_answer"),
    ("judge.rate", "sum_unsupported_over_sum_claims_of_ok_judged_sealed_rows"),
    ("judge.compare", "candidate_and_slot0_canonical_results_carry_equal_judge_config_hash_and_scorer_identity"),
    ("judge.covers", [
        "model", "client", "prompt", "verdicts", "sample", "parsing", "eligibility", "scoring", "endpoint",
        "preparation", "client_construction", "client_builder", "metering_wrapper",
    ]),
    ("judge.runtime", "recorded_per_slot_compared_candidate_vs_slot0"),
    ("judge.errors", "any_sealed_judge_error_means_not_shown"),
)
REGISTERED: Dict[str, Any] = dict(REGISTERED_VALUES)
REGISTERED_ROOTS = tuple(dict.fromkeys(path.split(".")[0] for path, _ in REGISTERED_VALUES))
#: Top-level prereg keys with their own structural checks and recomputations (``validate_prereg``): not in the table.
STRUCTURAL_ROOTS = ("version", "branch", "rounds", "seeds", "quota", "band", "strata", "min_share", "power")

#: Registered leaves whose value is computed from the code, never typed: ``(dotted path, name of the computation)``.
#: The leaf must be present (and a sha256 hex digest) in every load; ``prereg-check`` (``recompute``) compares it with
#: the code, so a prereg whose judge identity is not the running code's is refused where it is authored.
COMPUTED_VALUES: Tuple[Tuple[str, str], ...] = (("judge.config_hash", "judge_identity"),)
COMPUTED_PATHS = frozenset(path for path, _ in COMPUTED_VALUES)

#: The structural blocks' exact values that ``prereg-check`` (``recompute``) pins to the real prereg: the quota
#: numbers, the text of the quota rules the code implements (:data:`DEV_QUOTA_RULE` ...) and the minimum shares (the quota
#: *examples* and the power tables keep their recomputation). Not checked on
#: every load: a test world's small quotas are valid preregs for the commands, but only the registered numbers are the
#: pre-registration.
PINNED_VALUES: Tuple[Tuple[str, Any], ...] = (
    ("quota.sealed", 60),
    ("quota.spare", 10),
    ("quota.floor_sealed", 50),
    ("quota.floor_answerable", 35),
    ("quota.realistic_min", 42),
    ("quota.tuning_min", 60),
    ("quota.dev_quota_rule", DEV_QUOTA_RULE),
    ("quota.realistic_rule", REALISTIC_QUOTA_RULE),
    ("quota.tuning_floor_rule", TUNING_FLOOR_RULE),
    ("quota.tuning_rule", TUNING_QUOTA_RULE),
    ("min_share.realistic.tutorial_or_colleague", 0.25),
)


def prereg_leaves(node: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """``(dotted path, value)`` of every leaf of a JSON value (a scalar, a list or an empty object is a leaf)."""
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from prereg_leaves(value, f"{prefix}.{key}" if prefix else str(key))
    else:
        yield prefix, node


def _check_b1a(doc: Mapping[str, Any], errors: List[Tuple[str, str]]) -> None:
    """Every registered value is present and exact, and no leaf under a registered root is unregistered.

    A missing or changed value is ``mismatch``; an unregistered leaf is ``unknown_key`` (and its parent ``mismatch``).
    """
    for path, value in REGISTERED_VALUES:
        got = _at(doc, path)
        if got is _ABSENT or not _eq(got, value):
            errors.append((path, "mismatch"))
    for root in REGISTERED_ROOTS:
        if root not in doc:
            continue
        for path, _ in prereg_leaves(doc[root], root):
            if path not in REGISTERED and path not in COMPUTED_PATHS:
                errors.append((path, "unknown_key"))
                parent = path.rpartition(".")[0]
                if parent and (parent, "mismatch") not in errors:
                    errors.append((parent, "mismatch"))
    for path in sorted(COMPUTED_PATHS):  # present and shaped like a digest on every load; compared with the code by prereg-check
        got = _at(doc, path)
        if not (isinstance(got, str) and _SHA256.fullmatch(got)):
            errors.append((path, "mismatch"))
    share = _at(doc, "drafting_rule.attempt_1.refusal_share_target")
    if _is_num(share) and not 0.25 <= share <= 0.30:
        errors.append(("drafting_rule.attempt_1.refusal_share_target", "out_of_range"))


def pinned_errors(doc: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """``(path, "mismatch")`` for each of :data:`PINNED_VALUES` that is absent or not exactly the registered number.

    Run by ``prereg-check`` only (not by :func:`validate_prereg`, which every command applies to its prereg): a command
    needs a *valid* prereg, but only ``prereg-check`` says whether it is *the* pre-registration.
    """
    return [(path, "mismatch") for path, value in PINNED_VALUES if _at(doc, path) is _ABSENT or not _eq(_at(doc, path), value)]


def computed_errors(doc: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """``(path, "mismatch")`` for each value the code computes that the prereg does not carry (``prereg-check``'s recompute step).

    ``incumbent.components`` must be the code's ``PIPELINE_CONFIG_KEYS``, ``incumbent.code_excluded`` its ``CODE_EXCLUDED``,
    ``incumbent.code_excluded_imports`` its ``CODE_EXCLUDED_IMPORTS``, ``incumbent.code_scripts`` its ``HASHED_SCRIPTS`` and
    ``incumbent.code_scripts_inert_imports`` its ``HASHED_SCRIPT_IMPORTS_INERT``;
    ``adoption.scoring.covers`` must be ``scoring_identity.covered()`` and ``judge.covers`` the judge's ``JUDGE_COVERS``;
    ``judge.model`` and ``judge.prompt_version``
    must be the judge's own and ``judge.config_hash`` must be ``judge_identity()["config_hash"]`` (the imports are lazy:
    only ``prereg-check`` needs them).
    """
    from src import judge, pipeline_config, scoring_identity

    errors: List[Tuple[str, str]] = []
    for path, want in (
        ("incumbent.components", list(pipeline_config.PIPELINE_CONFIG_KEYS)),
        ("incumbent.code_excluded", list(pipeline_config.CODE_EXCLUDED)),
        ("incumbent.code_excluded_imports", list(pipeline_config.CODE_EXCLUDED_IMPORTS)),
        ("incumbent.code_scripts", list(pipeline_config.HASHED_SCRIPTS)),
        ("incumbent.code_scripts_inert_imports", list(pipeline_config.HASHED_SCRIPT_IMPORTS_INERT)),
        ("adoption.scoring.covers", scoring_identity.covered()),
        ("judge.covers", list(judge.JUDGE_COVERS)),
    ):
        if not _eq(_at(doc, path), want):
            errors.append((path, "mismatch"))
    identity = judge.judge_identity()
    for path, want in (
        ("judge.model", judge.JUDGE_MODEL), ("judge.prompt_version", judge.JUDGE_PROMPT_VERSION),
        ("judge.config_hash", identity["config_hash"]),
    ):
        if not _eq(_at(doc, path), want):
            errors.append((path, "mismatch"))
    return errors


#: The keys of a prereg quota example (and ``label``, optional): the pool, the preassigned families and refusal families of
#: each development split, and the quotas the rules (:func:`compute_quotas`, :func:`tuning_floor`) give.
EXAMPLE_COUNT_KEYS = (
    "pool_total", "preassigned_realistic", "preassigned_realistic_refusals", "preassigned_tuning", "preassigned_tuning_refusals",
)
EXAMPLE_KEYS = frozenset({*EXAMPLE_COUNT_KEYS, "sealed", "realistic", "tuning"})


def _example_counts_ok(ex: Mapping[str, Any]) -> bool:
    """Whether a quota example's counts are non-negative integers with no more refusal families than families (so the
    rules can be recomputed from them)."""
    return (
        all(_is_int(ex.get(k), 0) for k in (*EXAMPLE_COUNT_KEYS, "sealed", "realistic"))
        and _is_int(ex.get("tuning"), -10**9)
        and ex["preassigned_realistic_refusals"] <= ex["preassigned_realistic"]
        and ex["preassigned_tuning_refusals"] <= ex["preassigned_tuning"]
    )


def _check_examples_labels(quota: Mapping[str, Any], errors: List[Tuple[str, str]]) -> None:
    """A quota example may carry ``label``: ``quota_short`` exactly when its tuning is below tuning's own minimum feasible
    quota (:func:`tuning_floor` over the example's preassigned tuning families)."""
    for i, ex in enumerate(quota.get("examples", [])):
        if not isinstance(ex, dict) or not _example_counts_ok(ex):
            continue
        short = ex["tuning"] < tuning_floor(quota, ex["preassigned_tuning"], ex["preassigned_tuning_refusals"])
        label = ex.get("label")
        if "label" in ex and label != ("quota_short" if short else "ok"):
            errors.append((f"quota.examples[{i}]", "mismatch"))
        elif short and "label" not in ex:
            errors.append((f"quota.examples[{i}]", "mismatch"))


def validate_prereg(doc: Any, *, recompute: bool = True) -> List[Tuple[str, str]]:
    """``(field path, error enum)`` for a prereg document; empty = valid. Recomputes quota examples and power table."""
    if not isinstance(doc, dict):
        return [("<doc>", "wrong_type")]
    errors: List[Tuple[str, str]] = [("<unknown>", "unknown_key") for k in doc if k not in PREREG_KEYS]
    for key in PREREG_REQUIRED:
        if key not in doc:
            errors.append((key, "missing"))
    if errors:
        return errors

    def need(path: str, ok: bool, reason: str = "wrong_type") -> bool:
        if not ok:
            errors.append((path, reason))
        return ok

    need("version", doc["version"] == 1, "out_of_range")
    need("branch", isinstance(doc["branch"], str) and bool(_BRANCH.fullmatch(doc["branch"])) and ".." not in doc["branch"])
    rounds = doc["rounds"]
    need("rounds", isinstance(rounds, list) and bool(rounds) and all(r in (1, 2) and not isinstance(r, bool) for r in rounds)
         and len(set(rounds)) == len(rounds))
    seeds = doc["seeds"]
    if need("seeds", isinstance(seeds, dict)):
        for key in ("split_r1", "split_r2", "repair", "calibration"):
            need(f"seeds.{key}", key in seeds and isinstance(seeds[key], int) and not isinstance(seeds[key], bool))
    quota = doc["quota"]
    if need("quota", isinstance(quota, dict)):
        keys = ("sealed", "spare", "floor_sealed", "floor_answerable", "realistic_min", "tuning_min")
        if all(need(f"quota.{k}", _is_int(quota.get(k), 0)) for k in keys):
            need("quota.sealed", quota["sealed"] == quota["floor_sealed"] + quota["spare"], "mismatch")
            examples = quota.get("examples", [])
            if need("quota.examples", isinstance(examples, list)):
                for i, ex in enumerate(examples):
                    ok = isinstance(ex, dict) and set(ex) - {"label"} == EXAMPLE_KEYS and _example_counts_ok(ex)
                    if need(f"quota.examples[{i}]", ok) and recompute:
                        want = compute_quotas(quota, ex["pool_total"], ex["preassigned_realistic"], ex["preassigned_realistic_refusals"])
                        need(f"quota.examples[{i}]", all(ex[k] == want[k] for k in want), "mismatch")
                if isinstance(examples, list):
                    _check_examples_labels(quota, errors)
    band = doc["band"]
    need("band", band == {"min_pct": 25, "max_pct": 30}, "mismatch")
    need("mwe", _is_num(doc["mwe"]) and 0 < doc["mwe"] <= 1, "out_of_range")
    need("alpha", _is_num(doc["alpha"]) and 0 < doc["alpha"] < 1, "out_of_range")
    need("k_max", _is_int(doc["k_max"], 1), "out_of_range")
    need("draws_r", _is_int(doc["draws_r"], 1), "out_of_range")
    pw = doc["power"]
    if need("power", isinstance(pw, dict)):
        ok = (
            set(pw) <= {"alphas", "discordance", "planned_groups", "draws", "seed", "table", "family_table"}
            and isinstance(pw.get("alphas"), list) and bool(pw["alphas"])
            and all(_is_num(a) and 0 < a < 1 for a in pw["alphas"])
            and isinstance(pw.get("discordance"), list) and bool(pw["discordance"])
            and all(_is_num(d) and 0 < d <= 1 for d in pw["discordance"])
            and isinstance(pw.get("planned_groups"), list) and bool(pw["planned_groups"])
            and all(_is_int(g, 1) for g in pw["planned_groups"])
            and _is_int(pw.get("draws"), 1) and isinstance(pw.get("seed"), int) and not isinstance(pw.get("seed"), bool)
        )
        if need("power", ok):
            ok = _is_num(doc["alpha"]) and _is_int(doc["k_max"], 1) and pw["alphas"] == [doc["alpha"], doc["alpha"] / doc["k_max"]]
            need("power.alphas", ok, "mismatch")
        if ok and recompute and not errors:
            need("power.family_table", pw.get("family_table") == family_cells(pw, float(doc["mwe"])), "mismatch")
            want_cells = power_cells(pw, float(doc["mwe"]))
            table = pw.get("table")
            same = (
                isinstance(table, list) and len(table) == len(want_cells)
                and all(isinstance(t, dict) and all(t.get(k) == w[k] for k in w) for t, w in zip(table, want_cells))
            )
            need("power.table", same, "mismatch")
    _check_b1a(doc, errors)  # whatever else is wrong: a registered value is exact on its own
    if recompute:
        errors.extend(computed_errors(doc))
    strata, min_share = doc.get("strata", {}), doc.get("min_share", {})
    need("strata", isinstance(strata, dict) and all(s in SPLITS and isinstance(m, dict) for s, m in strata.items()))
    need("min_share", isinstance(min_share, dict) and all(s in SPLITS and isinstance(m, dict) for s, m in min_share.items()))
    return errors


def load_prereg(ctx: Ctx) -> Dict[str, Any]:
    path = ctx.prereg_file
    if not path.is_file():
        raise Stop(EXIT_USAGE, "prereg_missing")
    doc = read_json(path, "prereg_invalid")
    errors = validate_prereg(doc, recompute=False)
    if errors:
        raise Stop(EXIT_USAGE, "prereg_invalid", errors=len(errors))
    return doc


def _git_out(repo: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, env=env)
    except OSError:
        raise Stop(EXIT_REFUSED, "prereg_unchecked") from None
    if proc.returncode != 0:
        raise Stop(EXIT_REFUSED, "prereg_uncommitted")
    return proc.stdout


def prereg_commit(ctx: Ctx) -> str:
    """The commit that last changed the prereg file; the working file must equal that commit's copy."""
    try:
        rel = ctx.prereg_file.resolve().relative_to(ctx.repo.resolve()).as_posix()
    except ValueError:
        raise Stop(EXIT_REFUSED, "prereg_uncommitted") from None
    sha = _git_out(ctx.repo, "log", "-1", "--format=%H", "--", rel).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Stop(EXIT_REFUSED, "prereg_uncommitted")
    if _git_out(ctx.repo, "show", f"{sha}:{rel}") != ctx.prereg_file.read_bytes():
        raise Stop(EXIT_REFUSED, "prereg_modified")
    return sha


def gate_prereg(ctx: Ctx, prereg: Mapping[str, Any], round_no: int) -> str:
    """``split`` runs only if the prereg declares the round AND is pushed (a7's ancestry rule)."""
    if round_no not in prereg["rounds"]:
        raise Stop(EXIT_REFUSED, "round_undeclared")
    sha = prereg_commit(ctx)
    try:
        sp.check_prereg(sha, str(prereg["branch"]), repo=ctx.repo)
    except sp.LaunchError:
        raise Stop(EXIT_REFUSED, "prereg_not_pushed") from None
    return sha


def bind_prereg_sha(ctx: Ctx, prereg: Mapping[str, Any]) -> str:
    """The prereg's file sha256 if it is committed, unmodified and pushed (``gate_prereg``'s rule, without a round)."""
    sha = prereg_commit(ctx)
    try:
        sp.check_prereg(sha, str(prereg["branch"]), repo=ctx.repo)
    except sp.LaunchError:
        raise Stop(EXIT_REFUSED, "prereg_not_pushed") from None
    return sha_file(ctx.prereg_file)


def check_epoch_prereg(ledger: SealedLedger, prereg_sha: str) -> None:
    """The epoch has one registered prereg (the first ledger record that carries a ``prereg_sha256``): a different one is refused."""
    registered = ledger.epoch_prereg_sha256()
    if registered is not None and registered != prereg_sha:
        raise Stop(EXIT_REFUSED, "prereg_amended")


def bind_epoch_prereg(ctx: Ctx, prereg: Mapping[str, Any], ledger: SealedLedger) -> str:
    """The prereg's file sha256 if it is committed, unmodified, pushed AND the epoch's registered prereg."""
    sha = bind_prereg_sha(ctx, prereg)
    check_epoch_prereg(ledger, sha)
    return sha


# ---------------------------------------------------------------------------
# ids, digest, count-migrated, inputs-check
# ---------------------------------------------------------------------------
_FID_ANY = sp.FAMILY_ID_RE


def known_family_ids(ctx: Ctx, exclude: Iterable[Path] = ()) -> Set[str]:
    """Every family-id-shaped token in the private root's JSON / JSONL / text files (no loaders), except in the files
    named by ``exclude`` (``migrate_dev_v2`` asks which ids the files it does not own already hold)."""
    found: Set[str] = set()
    skip = {Path(p).resolve() for p in exclude}
    if ctx.private.is_dir():
        for path in ctx.private.rglob("*"):
            readable = path.is_file() and not path.is_symlink() and path.suffix.lower() in (".jsonl", ".json", ".txt", ".csv")
            if readable and path.resolve() not in skip:
                try:
                    found.update(_FID_ANY.findall(path.read_bytes().decode("utf-8", errors="replace")))
                except OSError:
                    continue
    return found


def new_family_id() -> str:
    """One fresh opaque family id: ``f`` + 8 random hex digits (``secrets``)."""
    return "f" + secrets.token_hex(4)


def cmd_ids(ctx: Ctx, count: int, out: Optional[str]) -> int:
    """Issue ``count`` fresh opaque family ids for the SP's ``ids.txt``; print the count and sha256 only."""
    if not 1 <= count <= 100_000:
        raise Stop(EXIT_USAGE, "usage")
    taken = known_family_ids(ctx)
    ids: List[str] = []
    seen: Set[str] = set()
    while len(ids) < count:
        cand = new_family_id()
        if cand not in taken and cand not in seen:
            seen.add(cand)
            ids.append(cand)
    target = Path(out) if out else ctx.sealed_work / "ids.txt"
    write_atomic(target, ("\n".join(ids) + "\n").encode("ascii"))
    issued = ctx.sealed_work / "issued_ids.txt"
    previous = issued.read_bytes() if issued.is_file() else b""
    write_atomic(issued, previous + ("\n".join(ids) + "\n").encode("ascii"))
    ctx.printer.emit("ids", count=len(ids), sha256=sha_file(target))
    return EXIT_OK


def cmd_digest(ctx: Ctx, path: str) -> int:
    """sha256, rows, families and counts by scope, register and source; chapters never for sealed."""
    p = Path(path)
    sealed = eval_sets.is_sealed(p)
    rows = load_sealed_rows(ctx, p) if sealed else load_dev_rows(ctx, p)
    fams = group_families(rows)
    fields: Dict[str, Any] = {"sha256": sha_file(p), "rows": len(rows), "families": len(fams)}
    for scope in SCOPES:
        fields[f"scope_{scope}"] = sum(1 for f in fams.values() if f.scope == scope)
    for reg in (*REGISTERS, None):
        fields[f"reg_{reg or 'none'}"] = sum(1 for f in fams.values() if f.register == reg)
    for src in (*SOURCES, None):
        fields[f"src_{src or 'none'}"] = sum(1 for f in fams.values() if f.source == src)
    if not sealed:
        chapters: Dict[str, int] = {}
        for fam in fams.values():
            ch = fam.chapter
            key = f"ch_{ch}" if ch.isdigit() and len(ch) <= 3 else "ch_other"
            chapters[key] = chapters.get(key, 0) + 1
        fields.update(sorted(chapters.items()))
    ctx.printer.emit("digest", kind="sealed" if sealed else "development", **fields)
    return EXIT_OK


def _v1_families(path: Path) -> Dict[str, bool]:
    """``{family id: is_refusal}`` for a golden / realistic file (v1 rows or v2 families)."""
    rows = eval_schema.load_any(path)
    out: Dict[str, bool] = {}
    for row in rows:
        out[str(row["family_id"])] = out.get(str(row["family_id"]), False) or row["scope"] == "refuse"
    return out


def _tutorial_exercises(path: Path) -> Dict[str, bool]:
    """T1 / T7 rows grouped by exercise (the id without its phrasing letter): ``{exercise: is_refusal}``."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    out: Dict[str, bool] = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise Stop(EXIT_USAGE, "input_invalid") from None
        rid = obj.get("id") if isinstance(obj, dict) else None
        if not isinstance(rid, str) or len(rid) < 2:
            raise Stop(EXIT_USAGE, "input_invalid")
        refusal = obj.get("type") == "refusal" or obj.get("scope") == "refuse"
        out[rid[:-1]] = out.get(rid[:-1], False) or refusal
    return out


def load_pairs(path: Path) -> List[List[str]]:
    """``eval/v2_pairing.json``'s ``pairs``: ``[[golden id, realistic id], ...]`` (the shape only; ``inputs-check`` checks the ids)."""
    doc = read_json(path)
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, list) or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in pairs):
        raise Stop(EXIT_USAGE, "input_invalid")
    return pairs


@dataclass(frozen=True)
class MigratedCounts:
    """What ``count-migrated`` counts from the v1 sets, the pairing and the tutorial families (``migrate_dev_v2`` asserts its
    output equals it): golden-only families are tuning's, realistic ones, pairs (ONE family) and T1/T7 families are realistic's."""

    pairs: int
    conflicts: int  # pairs whose two rows disagree on refusal (a pair counts as a refusal family if either does)
    tutorial_families: int
    tuning_families: int
    tuning_refuse: int
    realistic_families: int
    realistic_refuse: int


def migrated_counts(gold: Mapping[str, bool], real: Mapping[str, bool], tut: Mapping[str, bool], pairs: Sequence[Sequence[str]]) -> MigratedCounts:
    """The migrated families and refusal families per development split (the shared core of ``count-migrated`` and ``migrate_dev_v2``).

    ``gold`` / ``real`` / ``tut`` map a family (or tutorial exercise) to ``is_refusal``; ``pairs`` is ``[[golden id, realistic
    id], ...]``. A pair naming an unknown id, or a golden id twice, is ``inputs_invalid``.
    """
    paired_gold: Set[str] = set()
    conflicts = 0
    real_refuse = dict(real)
    for g, r in pairs:
        if g not in gold or r not in real or g in paired_gold:
            raise Stop(EXIT_USAGE, "inputs_invalid")
        paired_gold.add(g)
        if gold[g] != real[r]:
            conflicts += 1
        real_refuse[r] = real[r] or gold[g]
    return MigratedCounts(
        pairs=len(pairs), conflicts=conflicts, tutorial_families=len(tut),
        tuning_families=sum(1 for g in gold if g not in paired_gold),
        tuning_refuse=sum(1 for g, refuse in gold.items() if g not in paired_gold and refuse),
        realistic_families=len(real) + len(tut),
        realistic_refuse=sum(real_refuse.values()) + sum(tut.values()),
    )


def cmd_count_migrated(ctx: Ctx, golden: str, realistic: str, pairing: str, tutorial: str,
                       quota_tuning: Optional[int], quota_realistic: Optional[int], drafts: Optional[int] = None) -> int:
    """b2's counting: families per development split, each split's refusal deficit (to 25 % of its quota), and the
    refusal drafts the pool needs.

    The realistic quota is ``cmd_split``'s (:func:`compute_quotas`): the smallest that can still end inside the refusal
    band over the migrated realistic families and refusal families (``quota.dev_quota_rule``), unless ``--quota-realistic``
    is given. ``drafts`` is the number of new drafts. Its default is the prereg's ``attempt_1.families_rule``:
    ``max(families_approx, (sealed + tuning floor + realistic quota) - migrated) + draft_margin``: the registered
    ``families_approx``, or more when the first split needs a bigger pool, plus the registered ``draft_margin`` (a
    drafted family can be rejected at the owner's review, so the pool is drafted with room). The tuning floor is tuning's
    own minimum feasible quota over the migrated tuning families (:func:`tuning_floor`), not the bare ``tuning_min``; the
    printed ``drafts_rule`` says which bound binds (``families_approx`` / ``pool_floor``), or ``explicit`` (no margin is added
    to an explicit count), and ``draft_margin`` the margin the default used. The
    tuning quota is ``cmd_split``'s (the pool less the sealed and realistic quotas) unless ``--quota-tuning`` is given.
    The pool is the migrated families plus the drafts; its refusal target is the registered share (11/40) of it,
    rounded half up,
    and the drafts must supply ``max(target - preassigned refusals, sum of the splits' deficits)`` refusals.
    A split whose deficit exceeds its open slots, or whose preassigned refusals exceed 30 % of its quota, cannot
    reach the band, and neither can a pool whose refusal target lies outside the sum of the three splits' reachable
    refusal intervals (:func:`refusal_interval`; the dilution that one split's quota needs shows up here): either way
    ``band_unreachable`` after printing every split's quota, interval and the target (b2's owner stop).
    """
    gold, real, tut = _v1_families(Path(golden)), _v1_families(Path(realistic)), _tutorial_exercises(Path(tutorial))
    pairs = load_pairs(Path(pairing))
    counts = migrated_counts(gold, real, tut, pairs)
    tuning_n, tuning_refuse = counts.tuning_families, counts.tuning_refuse
    realistic_n, realistic_refuse = counts.realistic_families, counts.realistic_refuse
    prereg = ctx.prereg_file

    def deficit(q: int, refuse: int) -> int:
        return max(0, band_bounds(q)[0] - refuse)

    from fractions import Fraction

    prereg_doc = read_json(prereg) if prereg.is_file() else None
    attempt = prereg_doc.get("drafting_rule", {}).get("attempt_1", {}) if isinstance(prereg_doc, dict) else {}
    if not isinstance(attempt, dict) or "refusal_share_target" not in attempt:
        raise Stop(EXIT_USAGE, "prereg_missing")
    share = Fraction(str(attempt["refusal_share_target"]))
    if share != Fraction(11, 40):
        raise Stop(EXIT_REFUSED, "prereg_invalid")
    # the quota block is read whether or not the quotas are overridden: the default tuning quota needs the sealed quota
    quota_doc = prereg_doc.get("quota") if isinstance(prereg_doc, dict) else None
    quota_doc = quota_doc if isinstance(quota_doc, dict) else {}
    migrated, preassigned = tuning_n + realistic_n, tuning_refuse + realistic_refuse
    sealed_q = int(quota_doc.get("sealed", 0))
    realistic_q = (
        quota_realistic if quota_realistic is not None
        else min_feasible_quota(int(quota_doc.get("realistic_min", 0)), realistic_n, realistic_refuse)
    )
    tuning_floor_q = min_feasible_quota(int(quota_doc.get("tuning_min", 0)), tuning_n, tuning_refuse)
    # the drafts rule (prereg drafting_rule.attempt_1.families_rule): the registered approximate count, raised to what the
    # first split needs (sealed + tuning floor + realistic quotas; `spare` is already inside `sealed`), else attempt 1 would
    # be quota_short and burn a capped attempt
    approx, margin = attempt.get("families_approx"), attempt.get("draft_margin")
    if drafts is not None:
        new_drafts, drafts_rule, margin_used = drafts, "explicit", 0
    else:
        if isinstance(approx, bool) or not isinstance(approx, int) or approx < 0:
            raise Stop(EXIT_USAGE, "usage")
        if isinstance(margin, bool) or not isinstance(margin, int) or margin < 0:
            raise Stop(EXIT_USAGE, "usage")
        floor_drafts = max(0, sealed_q + tuning_floor_q + realistic_q - migrated)
        new_drafts = max(approx, floor_drafts) + margin
        margin_used = margin
        drafts_rule = "pool_floor" if floor_drafts > approx else "families_approx"
    if isinstance(new_drafts, bool) or not isinstance(new_drafts, int) or new_drafts < 0:
        raise Stop(EXIT_USAGE, "usage")
    pool_total = migrated + new_drafts
    # the default tuning quota is cmd_split's (compute_quotas): the pool less the sealed and the realistic quotas, so a
    # pool large enough for the split gives a tuning quota of at least the tuning floor (never printed negative)
    tuning_q = quota_tuning if quota_tuning is not None else max(0, pool_total - sealed_q - realistic_q)
    quotas: Dict[str, int] = {"tuning": tuning_q, "realistic": realistic_q}
    deficits = {"tuning": deficit(quotas["tuning"], tuning_refuse), "realistic": deficit(quotas["realistic"], realistic_refuse)}
    families = {"tuning": tuning_n, "realistic": realistic_n}
    refuse_have = {"tuning": tuning_refuse, "realistic": realistic_refuse}
    target = (2 * share.numerator * pool_total + share.denominator) // (2 * share.denominator)  # round half up
    needed = max(target - preassigned, deficits["tuning"] + deficits["realistic"])
    bands: Dict[str, str] = {}
    open_slots: Dict[str, int] = {}
    # each split's reachable final refusal-family count; the pool's target must be coverable by the three together
    intervals = {"sealed": refusal_interval(sealed_q)}
    for split in DEV_SPLITS:
        open_slots[split] = max(0, quotas[split] - families[split])
        too_many = BAND_MAX_DEN * refuse_have[split] > BAND_MAX_NUM * quotas[split]
        intervals[split] = refusal_interval(quotas[split], families[split], refuse_have[split])
        empty = intervals[split][0] > intervals[split][1]
        bands[split] = "unreachable" if deficits[split] > open_slots[split] or too_many or empty else "ok"
    supply_lo, supply_hi = (sum(intervals[s][i] for s in SPLITS) for i in (0, 1))
    supply = "ok" if supply_lo <= target <= supply_hi and all(lo <= hi for lo, hi in intervals.values()) else "unreachable"
    ctx.printer.emit(
        "count_migrated", pairs=counts.pairs, pair_scope_conflicts=counts.conflicts, tutorial_exercises=counts.tutorial_families,
        tuning_families=tuning_n, tuning_refuse=tuning_refuse, tuning_quota=quotas["tuning"],
        tuning_deficit=deficits["tuning"], tuning_floor=tuning_floor_q,
        realistic_families=realistic_n, realistic_refuse=realistic_refuse, realistic_quota=quotas["realistic"],
        realistic_deficit=deficits["realistic"],
        migrated_families=migrated, preassigned_refusals=preassigned, drafts=new_drafts, drafts_rule=drafts_rule,
        draft_margin=margin_used,
        pool_total=pool_total, refusal_target=target,
        refusal_drafts=needed, tuning_open=open_slots["tuning"], tuning_band=bands["tuning"],
        realistic_open=open_slots["realistic"], realistic_band=bands["realistic"],
        sealed_quota=sealed_q,
        **{f"{s}_refuse_{end}": max(0, intervals[s][i]) for s in SPLITS for i, end in enumerate(("min", "max"))},
        supply_min=max(0, supply_lo), supply_max=max(0, supply_hi), supply=supply,
    )
    if "unreachable" in bands.values() or supply == "unreachable":
        raise Stop(EXIT_REFUSED, "band_unreachable")
    return EXIT_OK


def cmd_inputs_check(ctx: Ctx, pairing: Optional[str], conflicts: Optional[str], golden_ids: Optional[str], realistic_ids: Optional[str]) -> int:
    """b0: ``eval/v2_pairing.json`` and ``eval/conflict_sections.json`` (a8's :mod:`src.sealed_inputs`)."""

    def ids_of(path: Optional[str]) -> Optional[List[str]]:
        if path is None:
            return None
        try:
            return [x for x in Path(path).read_text(encoding="utf-8").split() if x]
        except (OSError, UnicodeDecodeError):
            raise Stop(EXIT_USAGE, "input_missing") from None

    pairing_file = Path(pairing) if pairing else sealed_inputs.PAIRING_PATH
    conflict_file = Path(conflicts) if conflicts else sealed_inputs.CONFLICT_PATH
    try:
        counts = sealed_inputs.inputs_check(
            pairing_file, conflict_file, inventory=ctx.inventory(),
            golden_ids=ids_of(golden_ids), realistic_ids=ids_of(realistic_ids),
        )
    except sealed_inputs.InputsError as exc:
        for name, path, message in exc.errors:
            source = "pairing" if name == pairing_file.name else "conflicts"
            if "already used" in message:
                why = "reused_id"
            elif "not a public" in message:
                why = "not_public_id"
            elif "inventory" in message:
                why = "uninventoried_label"
            else:
                why = "invalid"
            ctx.printer.emit("inputs_error", source=source, field=path if _FIELD_PATH.fullmatch(path) else "other", why=why)
        raise Stop(EXIT_USAGE, "inputs_invalid", errors=len(exc.errors)) from None
    ctx.printer.emit("inputs_check", status="ok", pairs=counts["pairs"], labels=counts["labels"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------
def cmd_intake(ctx: Ctx, dispatch: str) -> int:
    """Take one released SP dispatch into the pool: ``sp_out/<id>/pool.jsonl`` and ``tutorial_map.json``.

    Validates everything first and changes nothing if any check fails. The pool file must pass the one
    sealed loader and hold only fresh family ids (none in the pool, development, the sealed side or any
    quarantine); a tutorial-sourced family needs a tag. The tag map must be ``{family id: tag}`` over pool
    family ids with tags in :data:`_SP_TAG`; its tags are stored lower-case and may not change a tag already
    stored. Then the map is merged (idempotent) and the pool is moved to ``sealed/pool/<id>.jsonl`` under the
    journaled move, so a crash is finished by :func:`recover_state`. Prints counts only.
    """
    if not sp.ID_RE.fullmatch(dispatch):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    src_dir = ctx.sp_out / dispatch
    pool_src, map_src = src_dir / "pool.jsonl", src_dir / "tutorial_map.json"
    present = [p for p in (pool_src, map_src) if p.exists() or p.is_symlink()]
    if not present:
        raise Stop(EXIT_USAGE, "input_missing")
    if any(p.is_symlink() or not p.is_file() for p in present):
        raise Stop(EXIT_USAGE, "input_invalid")
    current = load_pool(ctx)
    rows = load_sealed_rows(ctx, pool_src) if pool_src.is_file() else []
    incoming = group_families(rows)
    taken = {
        *current, *load_dev(ctx, include_quarantine=True), *load_sealed_side(ctx),
        *quarantined_ids(ctx, "sealed"), *quarantined_ids(ctx, "development"),
    }
    if set(incoming) & taken:
        raise Stop(EXIT_USAGE, "input_invalid")
    fresh: Dict[str, str] = {}
    if map_src.is_file():
        doc = read_json(map_src)
        valid_ids = set(current) | set(incoming)
        ok = isinstance(doc, dict) and bool(doc) and all(
            isinstance(k, str) and bool(sp.FAMILY_ID_RE.fullmatch(k)) and k in valid_ids
            and isinstance(v, str) and bool(_SP_TAG.fullmatch(v))
            for k, v in doc.items()
        )
        if not ok:
            raise Stop(EXIT_USAGE, "input_invalid")
        fresh = {k: v.lower() for k, v in doc.items()}
    merged = load_tutorial_map(ctx)
    clash = sum(1 for k, v in fresh.items() if merged.get(k, v) != v)
    if clash:
        raise Stop(EXIT_REFUSED, "tag_conflict", families=clash)
    _check_tags(incoming.values(), {**merged, **fresh})
    dest = ctx.pool_dir / f"{dispatch}.jsonl"
    if pool_src.is_file() and os.path.lexists(dest):
        raise Stop(EXIT_REFUSED, "already_intaken")
    if fresh:  # first: a crash before the move leaves a re-runnable intake (the merge is idempotent)
        write_json(ctx.sealed_work / "tutorial_map.json", {**merged, **fresh})
    if pool_src.is_file():
        try:
            sp.journaled_move([(pool_src, dest)], ctx.journal)
        except (sp.MoveError, OSError):
            raise Stop(EXIT_LEDGER_STOP, "move_recovery_failed") from None
    ctx.printer.emit("intake", families=len(incoming), rows=len(rows), tags=len(fresh))
    return EXIT_OK


# ---------------------------------------------------------------------------
# twins
# ---------------------------------------------------------------------------
def load_quarantine(ctx: Ctx, kind: str) -> Dict[str, Fam]:
    fams: Dict[str, Fam] = {}
    for path in quarantine_files(ctx, kind):
        rows = load_sealed_rows(ctx, path) if kind == "sealed" else load_dev_rows(ctx, path)
        fams.update(group_families(rows, path))
    return fams


def _check_tags(fams: Iterable[Fam], tags: Mapping[str, str]) -> None:
    missing = sum(1 for f in fams if f.source == "tutorial" and f.fid not in tags)
    if missing:
        raise Stop(EXIT_USAGE, "tutorial_tag_missing", families=missing)


def build_supply(ctx: Ctx, round_no: int, pool: Mapping[str, Fam], tags: Mapping[str, str], pairs: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """Nodes, groups and family records for a round: the shared core of ``twins`` and ``split``."""
    pinned = {fid: f for fid, f in load_dev(ctx).items() if f.split in DEV_SPLITS}
    sealed_pin = load_sealed_side(ctx) if round_no == 2 else {}
    quarantine = load_quarantine(ctx, "sealed")
    for fam in pool.values():
        if fam.register not in REGISTERS or fam.source not in SOURCES:
            raise Stop(EXIT_USAGE, "pool_register_missing")
    # A tutorial tag is demanded of the pool and of the sealed families pinned in round 2. A migrated development
    # family carries none (the SP never sees it, so `intake` cannot tag it): it simply joins no tutorial group.
    _check_tags([*pool.values(), *sealed_pin.values()], tags)
    nodes: Dict[str, Node] = {}
    for kind, fams in (("quarantine", quarantine), ("sealed_pin", sealed_pin), ("pinned", pinned), ("draft", pool)):
        for fid, fam in fams.items():
            nodes[fid] = Node(kind, tags.get(fid), fam.primary)
    plan = compute_groups(nodes, pairs, public_tied_drafts(pool))
    records: Dict[str, Dict[str, Any]] = {}
    preassigned: Dict[str, str] = {}
    for fid, fam in pool.items():
        status = plan.status[fid]
        if status == "dropped":
            continue
        eligible = ["tuning"] + (["realistic"] if realistic_ok(fam) else []) + (["sealed"] if status == "free" else [])
        records[fid] = family_record(fam, eligible)
    for fid, fam in pinned.items():
        records[fid] = family_record(fam, [fam.split])  # type: ignore[list-item]
        preassigned[fid] = str(fam.split)
    for fid, fam in sealed_pin.items():
        records[fid] = family_record(fam, ["sealed"])
        preassigned[fid] = "sealed"
    dropped = sorted(f for f, s in plan.status.items() if s == "dropped")
    ineligible = sorted(f for f, s in plan.status.items() if s == "ineligible")
    draft_ids = sorted(f for f in pool if f in records)
    pin_ids = sorted(preassigned)
    batches = {name: ids for name, ids in (("pool", draft_ids), ("pinned", pin_ids)) if ids}
    names = sorted(batches)
    twins = {
        "batches": batches,
        "families_digest": families_digest(sorted(records), names),
        "pairs_covered": [[names[i], names[j]] for i in range(len(names)) for j in range(i, len(names))],
        "edges": plan.edges,
    }
    return {
        "records": records, "preassigned": preassigned, "twins": twins, "groups": plan.groups,
        "ineligible": ineligible, "dropped": dropped, "pinned_fams": pinned, "sealed_pin_fams": sealed_pin,
    }


def cmd_twins(
    ctx: Ctx, round_no: int, verdict: str, dev_verdict: Optional[str], tutorials: Optional[str],
    extra_verdicts: Sequence[str] = (),
) -> int:
    """Record the SP's all-pairs verdict as 16A-1's ``twins`` record under the Unit rule.

    The SP's verdict can differ from one run to the next, so the owner may pass further SP runs as ``extra_verdicts``
    (``--extra-verdict``, repeatable): a pair flagged by ANY of them counts. The pairs used are the union of the main
    ``verdict``, every extra and ``dev_verdict`` (unordered pairs, de-duplicated, sorted). Every SP file (main and extras) is
    read by the same strict :func:`parse_verdict`, and the in-pool refusal applies to the union of those SP files.
    """
    if round_no not in (1, 2):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    ledger = ctx.ledger()
    pool = load_pool(ctx)
    if not pool:
        raise Stop(EXIT_REFUSED, "pool_empty")
    sp_pairs = parse_verdict(Path(verdict))
    extra_pairs = [parse_verdict(Path(extra)) for extra in extra_verdicts]  # a malformed one is verdict_invalid, as for the main file
    sp_union = sorted({*sp_pairs, *(pair for pairs in extra_pairs for pair in pairs)})
    inside = [p for p in sp_union if p[0] in pool and p[1] in pool]
    if inside:
        raise Stop(EXIT_REFUSED, "twin_in_pool", pairs=len(inside))
    all_pairs = sorted({*sp_union, *(parse_verdict(Path(dev_verdict)) if dev_verdict else [])})
    tags = load_tutorial_map(ctx, Path(tutorials) if tutorials else None)
    supply = build_supply(ctx, round_no, pool, tags, all_pairs)
    digest = pool_digest(pool)
    record = {
        "version": 1, "round": round_no, "pool_digest": digest, "verdict_sha256": sha_file(Path(verdict)),
        # present only when extras were given, so a record without extras is byte-identical to one written before they existed
        **({"extra_verdict_sha256s": sorted(sha_file(Path(extra)) for extra in extra_verdicts)} if extra_verdicts else {}),
        "twin_pairs": [list(p) for p in all_pairs], **supply["twins"],
        "ineligible": supply["ineligible"], "dropped": supply["dropped"], "groups": supply["groups"],
    }
    name = f"twins_r{round_no}_{digest[:16]}"
    if extra_verdicts:
        # A payload is immutable, so a union record cannot reuse the name of the no-extras record already stored for this
        # pool: it is named by its SP union (main + extras, not the dev verdict) as well. Without extras the name is unchanged.
        name += "_u" + sha_text(canon([list(p) for p in sp_union]))[:16]
    sha = ledger.store_payload(name, record)
    write_json(ctx.sealed_work / f"twins_current_r{round_no}.json", {"payload": name, "sha256": sha, "pool_digest": digest})
    ctx.printer.emit(
        "twins", round=round_no, pool_families=len(pool), twin_pairs=len(all_pairs), groups=len(supply["groups"]),
        ineligible=len(supply["ineligible"]), dropped=len(supply["dropped"]), edges=len(supply["twins"]["edges"]), sha256=sha,
        **({"extra_verdicts": len(extra_verdicts)} if extra_verdicts else {}),
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# split and release
# ---------------------------------------------------------------------------
def assign_name(round_no: int) -> str:
    return f"assign_r{round_no}"


def release_paths(ctx: Ctx, round_no: int) -> Dict[str, Path]:
    return {
        "sealed": ctx.sealed_dir / f"sealed_r{round_no}.jsonl",
        "tuning": ctx.dev_dir / f"tuning_r{round_no}.jsonl",
        "realistic": ctx.dev_dir / f"realistic_r{round_no}.jsonl",
        "dropped": ctx.sealed_dir / f"quarantine_r{round_no}.jsonl",
    }


def quarantined_by_release(payload: Mapping[str, Any]) -> Set[str]:
    """Pool families a release sent to ``quarantine_r<R>.jsonl``: dropped (related to a round-1 sealed group) and surplus."""
    return {*payload["dropped"], *payload.get("surplus", [])}


def execute_release(ctx: Ctx, round_no: int, payload: Mapping[str, Any]) -> None:
    """Move a released round out of ``sealed/pool/``: stage, then one journaled transaction.

    Development families go to ``dev/`` **unmarked** (the only sanctioned unmarking); sealed ones to
    ``sealed/sealed_r<R>.jsonl`` and dropped ones to ``sealed/quarantine_r<R>.jsonl`` (both marked); the
    consumed pool files are moved aside inside the same transaction. The copy, fsync, verify and delete
    run under ``sp_launch.journaled_move``; a crash is finished by :func:`recover_state`. Idempotent.
    """
    marker = ctx.sealed_work / f"release_r{round_no}.done"
    if marker.exists():
        return
    if ctx.journal.exists():
        sp.recover_moves(ctx.journal)
    dests = release_paths(ctx, round_no)
    assignment: Mapping[str, str] = payload["assignment"]
    shelved = quarantined_by_release(payload)
    # Only NEW families are written by a release: pinned ones already sit in their files (a round 2 with pinned
    # realistic families and no new realistic draft never creates realistic_r2.jsonl).
    expected = {name for fid, name in assignment.items() if fid not in payload["preassigned"]} | ({"dropped"} if shelved else set())
    stage = ctx.sealed_work / f"stage_r{round_no}"
    consumed = ctx.sealed_work / f"consumed_r{round_no}"
    if not (all(dests[n].exists() for n in expected) and not pool_files(ctx)):
        shutil.rmtree(stage, ignore_errors=True)
        pool = load_pool(ctx)
        if pool_digest(pool) != payload["pool_digest"]:
            raise Stop(EXIT_REFUSED, "pool_changed")
        buckets: Dict[str, List[Dict[str, Any]]] = {n: [] for n in dests}
        for fid, fam in pool.items():
            name = assignment.get(fid) or ("dropped" if fid in shelved else None)
            if name is None:
                raise Stop(EXIT_REFUSED, "pool_changed")
            buckets[name].extend(fam.rows)
        pairs: List[Tuple[Path, Path]] = []
        for name, rows in buckets.items():
            if rows:
                staged = stage / dests[name].name
                write_atomic(staged, dump_rows(rows, mark=name in ("sealed", "dropped")))
                pairs.append((staged, dests[name]))
        pairs.extend((p, consumed / f"{i:03d}.consumed") for i, p in enumerate(pool_files(ctx)))
        sp.journaled_move(pairs, ctx.journal)
    shutil.rmtree(consumed, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    write_atomic(marker, b"done\n")


def remove_atomic_leftovers(ctx: Ctx) -> int:
    """Delete the temp files a crashed :func:`write_atomic` left under ``sealed/`` and ``dev/``; return how many.

    A crash between the write and the rename leaves ``.<name>.<6 hex>.tmp`` beside its destination. The
    destination is either still the old file or absent, and the command that was writing it is re-run from its
    stored inputs, so the leftover is worth nothing, and the scanner's pre-check refuses (exit 7) a private file it
    cannot classify. Only a regular file whose name matches :data:`_ATOMIC_TMP` exactly is removed: no symlink is
    followed or deleted, and the ledger's own directory (``sealed/ledger/``, with its own temp names) is not touched.
    The caller holds the state lock, so no live write of ours has such a file open.
    """
    removed = 0
    skip = ctx.sealed_dir / "ledger"
    for top in (ctx.sealed_dir, ctx.dev_dir):
        if top.is_symlink() or not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            here = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (here / d).is_symlink() and here / d != skip]
            for name in filenames:
                path = here / name
                if _ATOMIC_TMP.fullmatch(name) and path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed += 1
    return removed


def recover_state(ctx: Ctx) -> None:
    """Finish a crashed move journal, every recorded release that was not completed, every journalled freeze and every
    journalled ``paraphrase-apply``.

    It first removes the temp files a crashed :func:`write_atomic` left under ``sealed/`` and ``dev/``.
    """
    try:
        remove_atomic_leftovers(ctx)
        if ctx.journal.exists():
            sp.recover_moves(ctx.journal)
        ledger = ctx.ledger()
        for rec in ledger.records("release"):
            execute_release(ctx, int(rec["round"]), ledger.load_payload(assign_name(int(rec["round"])), rec["payload_sha256"]))
        for split in SPLITS:
            if freeze_journal_path(ctx, split).exists():
                finish_freeze(ctx, split)
        finish_paraphrases(ctx)
    except (sp.MoveError, OSError):
        raise Stop(EXIT_LEDGER_STOP, "move_recovery_failed") from None


def _read_pointer(ctx: Ctx, round_no: int) -> Dict[str, Any]:
    path = ctx.sealed_work / f"twins_current_r{round_no}.json"
    if not path.is_file():
        raise Stop(EXIT_REFUSED, "twins_missing")
    ptr = read_json(path)
    if not (isinstance(ptr, dict) and set(ptr) == {"payload", "sha256", "pool_digest"}):
        raise Stop(EXIT_REFUSED, "twins_missing")
    return ptr


SURPLUS_TRIES = 24  # seeded surplus choices tried before an attempt is declared infeasible


def restrict_twins(twins: Mapping[str, Any], keep: Set[str]) -> Dict[str, Any]:
    """The twins attestation of ``keep`` only: batches, digest, covered pairs and edges re-derived for what remains."""
    batches = {name: [f for f in ids if f in keep] for name, ids in twins["batches"].items()}
    batches = {name: ids for name, ids in batches.items() if ids}
    names = sorted(batches)
    return {
        "batches": batches,
        "families_digest": families_digest(sorted(keep), names),
        "pairs_covered": [[names[i], names[j]] for i in range(len(names)) for j in range(i, len(names))],
        "edges": [list(e) for e in twins["edges"] if e[0] in keep and e[1] in keep],
    }


def surplus_choices(drafts: Sequence[str], edges: Iterable[Sequence[str]], count: int, seed: int, tries: int = SURPLUS_TRIES) -> Iterator[List[str]]:
    """Seeded sets of exactly ``count`` drafts to shelve, whole twin units only; each distinct set once.

    Units are the components of the drafts' group edges. Each try shuffles the units with ``Random("surplus:<seed>:<k>")``
    and takes units, in that order, while they still fit the count; a try that cannot land exactly on ``count`` yields nothing.
    """
    uf = _UnionFind(drafts)
    wanted = set(drafts)
    for a, b in edges:
        if a in wanted and b in wanted:
            uf.union(a, b)
    units = uf.components()
    seen: Set[Tuple[str, ...]] = set()
    for k in range(tries):
        order = list(units)
        random.Random(f"surplus:{seed}:{k}").shuffle(order)
        left, chosen = count, []
        for unit in order:
            if len(unit) <= left:
                chosen.extend(unit)
                left -= len(unit)
            if not left:
                break
        key = tuple(sorted(chosen))
        if left == 0 and key not in seen:
            seen.add(key)
            yield list(key)


def assign_round(
    records: Mapping[str, Mapping[str, Any]], twins: Mapping[str, Any], preassigned: Mapping[str, str], quotas: Mapping[str, int],
    seed: int, strata: Any, min_share: Any, surplus: int,
) -> Tuple[Dict[str, str], List[str], List[Mapping[str, Any]], Mapping[str, Any]]:
    """``split_families`` over ``records``, first shelving ``surplus`` drafts (round 2: more drafts than replacement slots).

    Returns ``(assignment, shelved draft ids, the records split, the twins split)``. Raises ``SplitInfeasible`` if no
    seeded choice of shelved drafts gives a feasible split (or none can land exactly on the surplus).
    """
    drafts = sorted(f for f in records if f not in preassigned)
    choices: Iterable[List[str]] = [[]] if surplus <= 0 else surplus_choices(drafts, twins["edges"], surplus, seed)
    last: Optional[SplitInfeasible] = None
    for shelved in choices:
        keep = {f for f in records if f not in shelved}
        recs = [records[f] for f in sorted(keep)]
        tw = twins if not shelved else restrict_twins(twins, keep)
        try:
            return split_families(recs, seed, quotas, strata, twins=tw, preassigned=preassigned, min_share=min_share), shelved, recs, tw
        except SplitInfeasible as exc:
            last = exc
    raise last or SplitInfeasible("no surplus choice fits", {"quota_total": {"violated": True}})


def _violated(node: Any) -> bool:
    """Whether a ``SplitInfeasible.counts`` subtree holds a violated constraint (counts only, no content)."""
    if isinstance(node, Mapping):
        return node.get("violated") is True or any(_violated(v) for v in node.values())
    return False


def recovery_pending(ctx: Ctx, ledger: SealedLedger) -> bool:
    """Whether :func:`recover_state` would have something to finish (read-only: it changes nothing).

    True for a move journal with an open transaction (or one that cannot be read), a recorded release whose
    ``release_r<R>.done`` marker is missing, a freeze journal or a ``paraphrase-apply`` journal. ``split --diagnose`` refuses in that case instead of
    recovering: a diagnosis moves nothing, and a world in the middle of a transaction is not one to diagnose.
    """
    try:
        if ctx.journal.exists() and sp.pending_moves(ctx.journal):
            return True
    except (sp.MoveError, OSError):
        return True
    if any(not (ctx.sealed_work / f"release_r{int(rec['round'])}.done").exists() for rec in ledger.records("release")):
        return True
    return any(freeze_journal_path(ctx, split).exists() for split in SPLITS) or bool(paraphrase_journals(ctx))


@dataclass
class SplitPlan:
    """What one split attempt is decided from: every gate passed, every number computed, nothing recorded or moved.

    :func:`plan_split` builds it; the real attempt (:func:`conclude_split`) and ``--diagnose`` (:func:`report_diagnosis`) both
    read it, so the two can never compute a different attempt.
    """

    round_no: int
    prereg: Mapping[str, Any]
    prereg_sha: str
    ledger: SealedLedger
    pool: Dict[str, Fam]
    digest: str
    pointer: Mapping[str, Any]
    supply: Dict[str, Any]
    quotas: Dict[str, int]
    surplus: int  # round 2: drafts beyond the replacement slots, shelved rather than forced into a split
    tuning_needed: int  # tuning's own minimum feasible quota (a lower tuning quota is ``quota_short``)
    pinned_n: Dict[str, int]
    pinned_refuse: Dict[str, int]
    attempts: int  # recorded attempts of this round so far
    cap: int  # the registered cap on this round's attempts
    new_register: int  # new (not legacy) drafts that are lay or terse: what realistic can take
    new_lay: int  # ... of which lay
    lay_min: Any  # the registered minimum lay share of those (a Fraction)

    def register_short(self) -> bool:
        """Whether the new drafts hold too few lay ones (below the registered share): a short pool, like a short quota."""
        from fractions import Fraction

        return bool(self.new_register) and Fraction(self.new_lay, self.new_register) < self.lay_min

    def lay_needed(self) -> int:
        """The fewest lay drafts, among the new lay or terse ones, that reach the registered share."""
        return math.ceil(self.lay_min * self.new_register)


@dataclass
class SplitAttempt:
    """What one attempt came to: ``quota_short``, ``register_short``, ``infeasible`` (with ``counts``) or ``feasible``."""

    outcome: str
    counts: Optional[Mapping[str, Any]] = None  # SplitInfeasible.counts (infeasible only)
    assignment: Optional[Dict[str, str]] = None  # the rest: feasible only
    shelved: Optional[List[str]] = None
    split_records: Optional[List[Mapping[str, Any]]] = None
    split_twins: Optional[Mapping[str, Any]] = None
    seed: int = 0
    strata: Any = None
    min_share: Any = None


def plan_split(ctx: Ctx, round_no: int, *, diagnose: bool = False) -> SplitPlan:
    """Every check and computation of a split attempt, up to (not including) the attempt itself.

    Raises :class:`Stop` for the prereg gate, the epoch, a released, frozen or out-of-order round, the attempt cap, an empty
    pool, a missing or stale ``twins`` record and every input problem. Unless ``diagnose``, it first finishes whatever a crash
    left half done (:func:`recover_state`); a diagnosis moves nothing, so it refuses (``recovery_pending``) instead.
    """
    if round_no not in (1, 2):
        raise Stop(EXIT_USAGE, "usage")
    prereg = load_prereg(ctx)
    gate_prereg(ctx, prereg, round_no)
    if not diagnose:
        recover_state(ctx)
    ledger = ctx.ledger()
    if diagnose and recovery_pending(ctx, ledger):
        raise Stop(EXIT_REFUSED, "recovery_pending")
    prereg_sha = sha_file(ctx.prereg_file)  # gate_prereg proved it committed, unmodified and pushed
    check_epoch_prereg(ledger, prereg_sha)  # before anything is recorded: the epoch has one registered prereg
    releases = {int(r["round"]): r for r in ledger.records("release")}
    if round_no in releases:
        raise Stop(EXIT_REFUSED, "already_released")
    if round_no == 2 and 1 not in releases:
        raise Stop(EXIT_REFUSED, "round1_missing")
    if ledger.freeze_record("sealed") is not None:
        raise Stop(EXIT_REFUSED, "already_frozen")
    if round_no == 1:  # the cap on round-1 attempts: past it the split is infeasible, a stop (b1a, Au11)
        cap = int(prereg["drafting_rule"]["next_attempt"]["max_round_1_attempts"])
        if sum(1 for r in ledger.records("split_attempt") if r["round"] == 1) >= cap:
            raise Stop(EXIT_REFUSED, "split_infeasible")
    else:  # round 2 is capped too: past it the freeze is infeasible, a stop with nothing recorded
        cap = int(prereg["drafting_rule"]["round_2"]["max_attempts"])
        if sum(1 for r in ledger.records("split_attempt") if r["round"] == 2) >= cap:
            raise Stop(EXIT_FREEZE_INFEASIBLE, "split_infeasible")
    pool = load_pool(ctx)
    if not pool:
        raise Stop(EXIT_REFUSED, "pool_empty")
    digest = pool_digest(pool)
    ptr = _read_pointer(ctx, round_no)
    if ptr["pool_digest"] != digest:
        raise Stop(EXIT_REFUSED, "twins_stale")
    record = ledger.load_payload(ptr["payload"], ptr["sha256"])
    tags = load_tutorial_map(ctx)
    pairs = [(a, b) for a, b in record["twin_pairs"]]
    supply = build_supply(ctx, round_no, pool, tags, pairs)
    twins = supply["twins"]
    if (twins["families_digest"], twins["edges"], supply["dropped"]) != (record["families_digest"], record["edges"], record["dropped"]):
        raise Stop(EXIT_REFUSED, "twins_stale")  # development or sealed state moved since `twins`
    records = supply["records"]
    total = len(records)
    quota_doc = prereg["quota"]
    pinned_n = {s: sum(1 for v in supply["preassigned"].values() if v == s) for s in SPLITS}
    pinned_refuse = {
        s: sum(1 for f, v in supply["preassigned"].items() if v == s and records[f]["scope"] == "refuse") for s in SPLITS
    }
    surplus = 0
    if round_no == 1:
        # the realistic quota is the smallest that can still end inside the refusal band over its pinned families and
        # refusal families (quota.dev_quota_rule); tuning is the rest and must reach its own minimum feasible quota
        quotas = compute_quotas(quota_doc, total, pinned_n["realistic"], pinned_refuse["realistic"])
        tuning_needed = tuning_floor(quota_doc, pinned_n["tuning"], pinned_refuse["tuning"])
    else:
        # Each split's quota is its pinned families plus its replacements, i.e. round 1's quota (a split can only be
        # larger if more families were pinned into it since). Tuning is NOT "the rest": drafts beyond the replacement
        # slots are surplus, shelved in quarantine_r2 rather than forced into a split.
        prior = ledger.load_payload(assign_name(1), releases[1]["payload_sha256"])["quotas"]
        quotas = {s: max(int(prior[s]), pinned_n[s]) for s in SPLITS}
        surplus = max(0, (total - sum(pinned_n.values())) - sum(quotas[s] - pinned_n[s] for s in SPLITS))
        # round 1's released quotas are reused, never recomputed from the survivors: the minimum is the registered one
        tuning_needed = int(quota_doc["tuning_min"])
    attempts = sum(1 for r in ledger.records("split_attempt") if r["round"] == round_no)
    # the registered share of lay among the new drafts that are lay or terse (what realistic can take): a short pool
    # releases nothing and counts toward the cap, like a short quota
    from fractions import Fraction

    new_reg = [f.register for f in pool.values() if f.source != "legacy" and f.register in ("lay", "terse")]
    lay_min = Fraction(str(prereg["drafting_rule"]["attempt_1"]["realistic_lay_min_share"]))
    return SplitPlan(
        round_no=round_no, prereg=prereg, prereg_sha=prereg_sha, ledger=ledger, pool=pool, digest=digest, pointer=ptr,
        supply=supply, quotas=quotas, surplus=surplus, tuning_needed=tuning_needed, pinned_n=pinned_n,
        pinned_refuse=pinned_refuse, attempts=attempts, cap=cap, new_register=len(new_reg),
        new_lay=sum(1 for r in new_reg if r == "lay"), lay_min=lay_min,
    )


def attempt_split(plan: SplitPlan) -> SplitAttempt:
    """Decide the attempt: quota short, too few lay drafts, or ``assign_round``. Records nothing, moves nothing."""
    if plan.quotas["tuning"] < plan.tuning_needed:
        return SplitAttempt("quota_short")
    if plan.register_short():
        return SplitAttempt("register_short")
    prereg, supply = plan.prereg, plan.supply
    seed = int(prereg["seeds"][f"split_r{plan.round_no}"])
    strata, min_share = prereg.get("strata") or None, prereg.get("min_share") or None
    try:
        assignment, shelved, split_records, split_twins = assign_round(
            supply["records"], supply["twins"], supply["preassigned"], plan.quotas, seed, strata, min_share, plan.surplus,
        )
    except SplitInfeasible as exc:
        return SplitAttempt("infeasible", counts=exc.counts)
    except SplitInputError:
        raise Stop(EXIT_USAGE, "split_input_invalid") from None
    return SplitAttempt(
        "feasible", assignment=assignment, shelved=shelved, split_records=split_records, split_twins=split_twins,
        seed=seed, strata=strata, min_share=min_share,
    )


def release_totals(plan: SplitPlan, attempt: SplitAttempt) -> Tuple[Dict[str, int], Dict[str, int]]:
    """A feasible attempt's per-split family counts: ``(all, new)``, i.e. with and without the pinned ones (``new`` is what
    the release moves out of the pool)."""
    assignment = attempt.assignment or {}
    totals = {s: sum(1 for v in assignment.values() if v == s) for s in SPLITS}
    new_totals = {s: sum(1 for f, v in assignment.items() if v == s and f in plan.pool) for s in SPLITS}
    return totals, new_totals


def conclude_split(ctx: Ctx, plan: SplitPlan, attempt: SplitAttempt) -> int:
    """Record the attempt; if it is feasible, store its payload, record and execute the release."""
    ledger, round_no, supply = plan.ledger, plan.round_no, plan.supply
    if attempt.outcome != "feasible":
        ledger.record_split_attempt(round_no=round_no, pool_digest=plan.digest, outcome=attempt.outcome, prereg_sha256=plan.prereg_sha)
        ctx.printer.emit("split", round=round_no, outcome=attempt.outcome, attempt=plan.attempts + 1)
        for name, node in sorted((attempt.counts or {}).items()):
            if name in ENUM_WORDS and _violated(node):
                ctx.printer.emit("split_constraint", constraint=name)
        return EXIT_SPLIT_INFEASIBLE
    assignment, shelved = attempt.assignment or {}, attempt.shelved or []
    payload = {
        "version": 1, "round": round_no, "seed": attempt.seed, "quotas": plan.quotas, "strata": attempt.strata,
        "min_share": attempt.min_share, "pool_digest": plan.digest, "families": attempt.split_records,
        "preassigned": supply["preassigned"], "twins": attempt.split_twins, "assignment": assignment,
        "groups": supply["groups"], "ineligible": supply["ineligible"], "dropped": supply["dropped"], "surplus": shelved,
        "twins_payload": plan.pointer["payload"], "twins_sha256": plan.pointer["sha256"],
    }
    sha = ledger.store_payload(assign_name(round_no), payload)
    ledger.record_split_attempt(
        round_no=round_no, pool_digest=plan.digest, outcome="feasible", payload_sha256=sha, prereg_sha256=plan.prereg_sha,
    )
    totals, new_totals = release_totals(plan, attempt)
    ledger.record_release(
        round_no=round_no, assignment_digest=sha_text(canon(assignment)),
        totals={**new_totals, "families": len(supply["records"]) - len(shelved), "dropped": len(supply["dropped"]), "surplus": len(shelved)},
        payload_sha256=sha,
    )
    execute_release(ctx, round_no, payload)
    ctx.printer.emit(
        "split", round=round_no, outcome="released", attempt=plan.attempts + 1, sealed=totals["sealed"], tuning=totals["tuning"],
        realistic=totals["realistic"], groups=len(supply["groups"]), dropped=len(supply["dropped"]), surplus=len(shelved),
        sha256=sha,
    )
    return EXIT_OK


# -- split --diagnose: the same attempt, shown instead of recorded ----------------------------------------------------------
def _word(value: Any) -> str:
    """``value`` if it is an enum word the printer accepts, else ``other``: a diagnosis never prints a label it cannot vouch for."""
    return value if isinstance(value, str) and value in ENUM_WORDS else "other"


def _constraint_fields(path: Sequence[str], name: str) -> Dict[str, Any]:
    """The identifying fields of one ``SplitInfeasible.counts`` record from the keys above it: the split, then a stratum
    (``scope|chapter``, printed as a scope word and a chapter number or word), a source class, or ``total`` (``_total``)."""
    fields: Dict[str, Any] = {}
    for depth, key in enumerate(path):
        if depth == 0:
            fields["split"] = _word(key)
        elif depth == 1 and key == "_total":
            fields["total"] = "yes"
        elif depth == 1 and name == "strata":
            scope, _, chapter = key.partition("|")
            fields["stratum_scope"] = _word(scope)
            fields["stratum_chapter"] = int(chapter) if re.fullmatch(r"[0-9]{1,9}", chapter) else _word(chapter)
        elif depth == 1:
            fields["source"] = _word(key)
        else:
            fields[f"level_{depth + 1}"] = _word(key)
    return fields


def _flat_numbers(node: Mapping[str, Any], prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """The fields of one counts record, ``violated`` last: ints as they are, flags as yes / no, a list as its length, a nested
    mapping flattened with ``_``. Anything else is passed on to the printer, which refuses what it does not know."""
    for key, value in node.items():
        if key == "violated" and not prefix:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from _flat_numbers(value, f"{name}_")
        elif isinstance(value, bool):
            yield name, "yes" if value else "no"
        elif isinstance(value, (list, tuple, set, frozenset)):
            yield name, len(value)
        else:
            yield name, value
    if not prefix and "violated" in node:
        yield "violated", "yes" if node["violated"] else "no"


def _constraint_records(node: Mapping[str, Any], path: Tuple[str, ...] = ()) -> Iterator[Tuple[Tuple[str, ...], Mapping[str, Any]]]:
    """The records of one counts entry: a mapping with a plain value in it is a record, a mapping of mappings is a level (split,
    then stratum or source) whose keys label the records below it."""
    if any(not isinstance(v, Mapping) for v in node.values()):
        yield path, node
        return
    for key, sub in node.items():
        if isinstance(sub, Mapping):
            yield from _constraint_records(sub, (*path, str(key)))


def emit_constraints(ctx: Ctx, counts: Mapping[str, Any]) -> None:
    """One ``diagnose_constraint`` line per constraint record of ``SplitInfeasible.counts`` (violated or not), counts only."""
    for name, node in counts.items():
        if not isinstance(node, Mapping):
            continue
        records = list(_constraint_records(node))
        if not records:  # a constraint with nothing registered under it (no strata, no min_share): say so
            ctx.printer.emit("diagnose_constraint", constraint=_word(name), entries=0, violated="no")
        for path, record in records:
            ctx.printer.emit("diagnose_constraint", constraint=_word(name), **_constraint_fields(path, name), **dict(_flat_numbers(record)))


def _scope_counts(scopes: Iterable[str]) -> Dict[str, int]:
    counts = {scope: 0 for scope in SCOPES}
    for scope in scopes:
        counts[scope] += 1
    return counts


def emit_supply(ctx: Ctx, plan: SplitPlan) -> None:
    """The round's supply, counts only: quotas and pins per split, the register share, the drafts by class and scope."""
    pr, supply, pool = ctx.printer, plan.supply, plan.pool
    records, dropped = supply["records"], set(supply["dropped"])
    pr.emit(
        "diagnose_pool", round=plan.round_no, records=len(records), pinned=len(supply["preassigned"]), groups=len(supply["groups"]),
        surplus=plan.surplus, tuning_floor=plan.tuning_needed, attempts=plan.attempts, attempts_cap=plan.cap,
    )
    for split in SPLITS:
        quota = plan.quotas[split]
        pr.emit(
            "diagnose_split", split=split, quota=max(quota, 0), **({"quota_negative": "yes"} if quota < 0 else {}),
            pinned_families=plan.pinned_n[split], pinned_refusals=plan.pinned_refuse[split],
        )
    pr.emit(
        "diagnose_register", lay_or_terse=plan.new_register, lay=plan.new_lay, lay_needed=plan.lay_needed(),
        short="yes" if plan.register_short() else "no",
    )
    classes = {
        "total": list(pool),
        "sealed_eligible": [f for f in pool if f in records and "sealed" in records[f]["eligible"]],
        "sealed_ineligible": [f for f in pool if f in records and "sealed" not in records[f]["eligible"]],
        "dropped": [f for f in pool if f in dropped],
        "realistic_eligible": [f for f in pool if f in records and "realistic" in records[f]["eligible"]],
    }
    for name, fids in classes.items():
        pr.emit("diagnose_drafts", **{"class": name}, families=len(fids), **_scope_counts(pool[f].scope for f in fids))


def report_diagnosis(ctx: Ctx, plan: SplitPlan, attempt: SplitAttempt) -> int:
    """Show the attempt instead of recording it: outcome, what it would release or every constraint, then the supply."""
    pr = ctx.printer
    pr.emit("diagnose", round=plan.round_no, outcome=attempt.outcome)
    if attempt.outcome == "feasible":
        totals, new_totals = release_totals(plan, attempt)
        pr.emit(
            "diagnose_release", round=plan.round_no, sealed=totals["sealed"], tuning=totals["tuning"], realistic=totals["realistic"],
            new_sealed=new_totals["sealed"], new_tuning=new_totals["tuning"], new_realistic=new_totals["realistic"],
            groups=len(plan.supply["groups"]), dropped=len(plan.supply["dropped"]), surplus=len(attempt.shelved or []),
        )
    elif attempt.counts is not None:
        emit_constraints(ctx, attempt.counts)
    emit_supply(ctx, plan)
    return EXIT_OK if attempt.outcome == "feasible" else EXIT_SPLIT_INFEASIBLE


def cmd_split(ctx: Ctx, round_no: int, diagnose: bool = False) -> int:
    """One split attempt: infeasible releases nothing (recorded); the first feasible attempt is released.

    With ``diagnose`` the same gates and computations run, and the attempt is shown (:func:`report_diagnosis`) instead of
    recorded or released: nothing is written, no payload is stored, no file moves, and the attempt cap is untouched. The
    caller holds the state lock either way (``split`` is a state command), so a diagnosis never interleaves with a split.
    """
    plan = plan_split(ctx, round_no, diagnose=diagnose)
    attempt = attempt_split(plan)
    return report_diagnosis(ctx, plan, attempt) if diagnose else conclude_split(ctx, plan, attempt)


# ---------------------------------------------------------------------------
# paraphrase-apply: the SP's new phrasings for released families (16A-2b b4)
# ---------------------------------------------------------------------------
PARAPHRASE_STATUS_KEYS = ("owner_status", "second_status")
#: The fields a new phrasing shares with its family's row 0 (what makes it the same scenario, scored the same way).
PARAPHRASE_SAME_AS_ROW_0 = ("scope", "evidence", "gaps", "type", "source")
#: Order in which the checks that run on every touched family report their refusal (the first one that fails).
PARAPHRASE_ROW_CHECKS = ("row_index", "row_mismatch", "question_repeat", "over_cap", "under_min")


def _paraphrase_crash(name: str) -> None:
    """No-op seam between a ``paraphrase-apply`` transaction's steps (``after_journal``, ``after_file``, ``after_record``);
    tests replace it with a function that raises, to inject a crash there."""


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paraphrase_applied_path(ctx: Ctx) -> Path:
    return ctx.sealed_work / "paraphrase_applied.json"


def paraphrase_journal_path(ctx: Ctx, dispatch: str) -> Path:
    return ctx.sealed_work / f"paraphrase_{dispatch}.journal"


def paraphrase_data_dir(ctx: Ctx, dispatch: str) -> Path:
    return ctx.sealed_work / f"paraphrase_{dispatch}.data"


def paraphrase_journals(ctx: Ctx) -> List[Path]:
    """The journals of ``paraphrase-apply`` transactions not yet finished (read-only)."""
    if not ctx.sealed_work.is_dir():
        return []
    return sorted(p for p in ctx.sealed_work.glob("paraphrase_*.journal") if p.is_file() or p.is_symlink())


def load_paraphrase_applied(ctx: Ctx) -> Dict[str, Dict[str, Optional[str]]]:
    """``{dispatch id: {"pool_sha256", "dev_sha256"}}`` (a sha256 or ``None`` each), ``{}`` if nothing was applied yet."""
    path = paraphrase_applied_path(ctx)
    if not path.is_file():
        return {}
    doc = read_json(path)
    ok = isinstance(doc, dict) and all(
        isinstance(key, str) and bool(sp.ID_RE.fullmatch(key)) and isinstance(rec, dict)
        and set(rec) == {"pool_sha256", "dev_sha256"}
        and all(v is None or (isinstance(v, str) and bool(_SHA256.fullmatch(v))) for v in rec.values())
        for key, rec in doc.items()
    )
    if not ok:
        raise Stop(EXIT_USAGE, "input_invalid")
    return doc


def _paraphrase_tx(ctx: Ctx, path: Path) -> Dict[str, Any]:
    """The journalled transaction at ``path``, its shape checked (``paraphrase_recovery_failed`` otherwise: a D64 stop).

    Shape: ``dispatch`` (the journal's own id), ``pool_sha256`` / ``dev_sha256`` (a sha256 or null), ``files`` (``path`` relative
    to the private root and under ``sealed/`` or ``dev/``, ``kind``, ``old_sha256``, ``new_sha256``) and ``report`` (per side
    the families and rows added and the four histogram buckets: integers only).
    """
    fail = Stop(EXIT_LEDGER_STOP, "paraphrase_recovery_failed")

    def sha_or_none(value: Any) -> bool:
        return value is None or (isinstance(value, str) and bool(_SHA256.fullmatch(value)))

    try:
        tx = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        raise fail from None
    match = re.fullmatch(r"paraphrase_([0-9a-f]{8})\.journal", path.name)
    ok = (
        isinstance(tx, dict) and tx.get("version") == 1 and match is not None and tx.get("dispatch") == match.group(1)
        and sha_or_none(tx.get("pool_sha256")) and sha_or_none(tx.get("dev_sha256"))
        and isinstance(tx.get("files"), list) and bool(tx["files"]) and isinstance(tx.get("report"), dict)
        and set(tx["report"]) == set(KINDS)
    )
    if ok:
        for item in tx["files"]:
            parts = Path(str(item.get("path", ""))).parts if isinstance(item, dict) else ()
            ok = ok and (
                isinstance(item, dict) and set(item) == {"path", "kind", "old_sha256", "new_sha256"}
                and isinstance(item["path"], str) and len(parts) >= 2 and ".." not in parts and not item["path"].startswith("/")
                and parts[0] == ("sealed" if item["kind"] == "sealed" else "dev") and item["kind"] in KINDS
                and isinstance(item["old_sha256"], str) and bool(_SHA256.fullmatch(item["old_sha256"]))
                and isinstance(item["new_sha256"], str) and bool(_SHA256.fullmatch(item["new_sha256"]))
            )
        for side in KINDS:
            rep = tx["report"][side]
            ok = ok and isinstance(rep, dict) and set(rep) == {"families", "rows", "phrasings"} and _is_int(rep["families"]) \
                and _is_int(rep["rows"]) and isinstance(rep["phrasings"], list) and len(rep["phrasings"]) == 4 \
                and all(_is_int(n) for n in rep["phrasings"])
    if not ok:
        raise fail
    return tx


def finish_paraphrase(ctx: Ctx, journal: Path) -> Dict[str, Any]:
    """Run (or resume) the journalled ``paraphrase-apply`` transaction at ``journal``; returns it. Every step is idempotent.

    Order: each affected file in turn (replaced by an atomic write of the staged bytes only if it still holds the journalled
    old bytes; a file that already holds the new ones is left alone, and one that holds neither is a stop), then the dispatch
    in ``paraphrase_applied.json``, then the journal and the staged bytes are removed. Nothing is recomputed: a half-done
    transaction is finished, never re-derived from the half-changed world.
    """
    tx = _paraphrase_tx(ctx, journal)
    dispatch = tx["dispatch"]
    data_dir = paraphrase_data_dir(ctx, dispatch)
    for index, item in enumerate(tx["files"]):
        target = ctx.private / item["path"]
        try:
            data = (data_dir / f"{index}.stage").read_bytes()
        except OSError:
            raise Stop(EXIT_LEDGER_STOP, "paraphrase_recovery_failed") from None
        if sha_bytes(data) != item["new_sha256"]:
            raise Stop(EXIT_LEDGER_STOP, "paraphrase_recovery_failed")
        current = sha_file(target) if target.is_file() and not target.is_symlink() else None
        if current == item["old_sha256"]:
            write_atomic(target, data)
        elif current != item["new_sha256"]:
            raise Stop(EXIT_LEDGER_STOP, "paraphrase_recovery_failed")
        _paraphrase_crash("after_file")
    record = {"pool_sha256": tx["pool_sha256"], "dev_sha256": tx["dev_sha256"]}
    applied = load_paraphrase_applied(ctx)
    if applied.get(dispatch, record) != record:
        raise Stop(EXIT_LEDGER_STOP, "paraphrase_recovery_failed")
    applied[dispatch] = record
    write_json(paraphrase_applied_path(ctx), applied)
    _paraphrase_crash("after_record")
    journal.unlink(missing_ok=True)
    _fsync_dir(ctx.sealed_work)
    shutil.rmtree(data_dir, ignore_errors=True)
    return tx


def finish_paraphrases(ctx: Ctx) -> None:
    """Finish every journalled ``paraphrase-apply`` transaction, then drop staged bytes that no journal owns (a crash between
    the journal's removal and the staged bytes')."""
    for journal in paraphrase_journals(ctx):
        finish_paraphrase(ctx, journal)
    if ctx.sealed_work.is_dir():
        for data_dir in ctx.sealed_work.glob("paraphrase_*.data"):
            if data_dir.is_dir() and not data_dir.is_symlink() and not paraphrase_journal_path(ctx, data_dir.name[len("paraphrase_"):-len(".data")]).exists():
                shutil.rmtree(data_dir, ignore_errors=True)


def emit_paraphrase(ctx: Ctx, tx: Mapping[str, Any]) -> None:
    """The counts of an applied dispatch: per side the families touched and rows added, then the side's phrasing-count histogram
    (families with 1, 2, 3 and more than 3 rows after applying), then the two input digests (``none`` for an absent input)."""
    for side in KINDS:
        rep = tx["report"][side]
        ctx.printer.emit("paraphrase_apply", side=side, families=rep["families"], rows=rep["rows"])
        one, two, three, over = rep["phrasings"]
        ctx.printer.emit("paraphrase_phrasings", side=side, families_1=one, families_2=two, families_3=three, families_over_3=over)
    ctx.printer.emit("paraphrase_inputs", pool_sha256=tx["pool_sha256"] or "none", dev_sha256=tx["dev_sha256"] or "none")


@dataclass
class ParaphraseSide:
    """One side's world as ``paraphrase-apply`` sees it: its live families, every component file's rows and the new rows."""

    kind: str  # "sealed" | "development"
    fams: Dict[str, Fam]
    files: Dict[Path, List[Dict[str, Any]]]
    new: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # new rows by family id


def load_paraphrase_side(ctx: Ctx, kind: str) -> ParaphraseSide:
    """The live families of a side, each in exactly one component file (a family spread over two files is ``input_invalid``:
    which rows would a rewrite keep?). Families in quarantine are not live; their rows stay in whatever file holds them."""
    gone = quarantined_ids(ctx, kind)
    loader = load_sealed_rows if kind == "sealed" else load_dev_rows
    fams: Dict[str, Fam] = {}
    files: Dict[Path, List[Dict[str, Any]]] = {}
    for path in kind_components(ctx, kind):
        rows = loader(ctx, path)
        files[path] = rows
        for fid, fam in group_families(rows, path, split_of_file(kind, path)).items():
            if fid in gone:
                continue
            if fid in fams:
                raise Stop(EXIT_USAGE, "input_invalid")
            fams[fid] = fam
    return ParaphraseSide(kind, fams, files)


def load_sp_dev_rows(ctx: Ctx, path: Path) -> List[Dict[str, Any]]:
    """The SP's ``dev.jsonl`` as development rows.

    The file lies under ``sealed/work/sp_out/``, which ``classify`` calls sealed by location, so ``eval_schema.load_v2`` would
    refuse it. A byte copy under ``work/`` (private, not sealed) is validated instead, by the loader every development file
    goes through. A copy that still classifies sealed holds a ``sealed`` marker: development rows are never marked
    (``wrong_side``). The copy is always removed.
    """
    tmp = ctx.dev_work / f".sp_dev.{secrets.token_hex(_ATOMIC_TMP_BYTES)}.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp.write_bytes(path.read_bytes())
    try:
        return load_dev_rows(ctx, tmp)
    except eval_privacy.SealedInputError:
        raise Stop(EXIT_REFUSED, "wrong_side") from None
    finally:
        tmp.unlink(missing_ok=True)


def normalised_question(text: Any) -> str:
    """A question as the duplicate check compares it: case-folded, every run of whitespace one space, ends trimmed."""
    return " ".join(str(text).casefold().split())


def migrated_dev_file(ctx: Ctx, path: Optional[Path]) -> bool:
    """Whether a development component file holds pinned (migrated) families: it is not a round's release output."""
    released = {release_paths(ctx, r)[split].name for r in (1, 2) for split in DEV_SPLITS}
    return path is not None and path.name not in released


def check_new_rows(ctx: Ctx, side: ParaphraseSide, lo: int, hi: int, tutorial_cap: int) -> Dict[str, int]:
    """Per refusal reason of :data:`PARAPHRASE_ROW_CHECKS`, how many of the side's touched families fail it (counts only).

    A new row must be the family's next row number (the numbers after its highest, in order, none missing); equal row 0 in
    :data:`PARAPHRASE_SAME_AS_ROW_0`, hold a valid register and no review status; and its question differs (normalised) from every
    other phrasing of the family. After applying, the family holds at most ``hi`` rows (``tutorial_cap`` for a migrated
    tutorial-sourced one) and at least ``lo``.
    """
    failed: Dict[str, Set[str]] = {name: set() for name in PARAPHRASE_ROW_CHECKS}
    for fid, new in side.new.items():
        fam = side.fams[fid]
        top = max(row_no(r) for r in fam.rows)
        if sorted(row_no(r) for r in new) != list(range(top + 1, top + 1 + len(new))):
            failed["row_index"].add(fid)
        ref = fam.rows[0]
        for row in new:
            if (
                any(row[key] != ref[key] for key in PARAPHRASE_SAME_AS_ROW_0)
                or row.get("register") not in REGISTERS
                or any(row.get(key) is not None for key in PARAPHRASE_STATUS_KEYS)
            ):
                failed["row_mismatch"].add(fid)
        seen = {normalised_question(r["question"]) for r in fam.rows}
        for row in new:
            text = normalised_question(row["question"])
            if text in seen:
                failed["question_repeat"].add(fid)
            seen.add(text)
        limit = min(hi, tutorial_cap) if side.kind == "development" and fam.source == "tutorial" and migrated_dev_file(ctx, fam.path) else hi
        total = len(fam.rows) + len(new)
        if total > limit:
            failed["over_cap"].add(fid)
        if total < lo:
            failed["under_min"].add(fid)
    return {name: len(fids) for name, fids in failed.items()}


def cmd_paraphrase_apply(ctx: Ctx, dispatch: str) -> int:
    """Add the SP's new phrasings from one released ``paraphrase`` dispatch to the released families (see the module docstring).

    Validates everything before writing anything (the schema, the family's side, frozen splits, row numbers, row content, the
    question, the phrasing counts); refuses a dispatch already applied; then writes every affected file under one journalled
    transaction and records the dispatch. Prints counts and digests only.
    """
    if not sp.ID_RE.fullmatch(dispatch):
        raise Stop(EXIT_USAGE, "usage")
    journal = paraphrase_journal_path(ctx, dispatch)
    if journal.exists() or journal.is_symlink():  # a crash left this dispatch half applied: finish it, nothing more
        tx = _paraphrase_tx(ctx, journal)
        recover_state(ctx)  # finishes this journal and every other pending transaction
        emit_paraphrase(ctx, tx)
        return EXIT_OK
    recover_state(ctx)
    prereg = load_prereg(ctx)
    ledger = ctx.ledger()
    check_epoch_prereg(ledger, sha_file(ctx.prereg_file))  # the phrasing bounds are the epoch's registered ones
    rule = prereg["drafting_rule"]["paraphrase"]
    lo, hi, tutorial_cap = int(rule["phrasings_min"]), int(rule["phrasings_max"]), int(rule["t1_t7_cap"])
    if dispatch in load_paraphrase_applied(ctx):
        raise Stop(EXIT_REFUSED, "already_applied")

    src_dir = ctx.sp_out / dispatch
    pool_src, dev_src = src_dir / "pool.jsonl", src_dir / "dev.jsonl"
    present = [p for p in (pool_src, dev_src) if p.exists() or p.is_symlink()]
    if not present:
        raise Stop(EXIT_USAGE, "input_missing")
    if any(p.is_symlink() or not p.is_file() for p in present):
        raise Stop(EXIT_USAGE, "input_invalid")
    pool_rows = load_sealed_rows(ctx, pool_src) if pool_src.is_file() else []
    dev_rows = load_sp_dev_rows(ctx, dev_src) if dev_src.is_file() else []
    pool_sha = sha_file(pool_src) if pool_src.is_file() else None
    dev_sha = sha_file(dev_src) if dev_src.is_file() else None

    sides = {"sealed": load_paraphrase_side(ctx, "sealed"), "development": load_paraphrase_side(ctx, "development")}
    if set(sides["sealed"].fams) & set(sides["development"].fams):
        raise Stop(EXIT_USAGE, "input_invalid")
    wrong: Set[str] = set()
    unknown: Set[str] = set()
    for kind, rows in (("sealed", pool_rows), ("development", dev_rows)):
        side, other = sides[kind], sides["development" if kind == "sealed" else "sealed"]
        for row in rows:
            fid = str(row["family_id"])
            if fid in side.fams:
                side.new.setdefault(fid, []).append(row)
            elif fid in other.fams:
                wrong.add(fid)
            else:
                unknown.add(fid)
    if wrong:
        raise Stop(EXIT_REFUSED, "wrong_side", families=len(wrong))
    if unknown:
        raise Stop(EXIT_REFUSED, "unknown_family", families=len(unknown))
    for kind, side in sides.items():
        if frozen_files(ledger, kind, {side.fams[fid].path for fid in side.new if side.fams[fid].path is not None}):
            raise Stop(EXIT_REFUSED, "frozen")
    failures = [check_new_rows(ctx, side, lo, hi, tutorial_cap) for side in sides.values()]
    for name in PARAPHRASE_ROW_CHECKS:
        count = sum(f[name] for f in failures)
        if count:
            raise Stop(EXIT_REFUSED, name, families=count)

    changed: Dict[str, Dict[Path, List[Dict[str, Any]]]] = {kind: {} for kind in KINDS}
    for kind, side in sides.items():
        for fid in sorted(side.new):
            path = side.fams[fid].path
            if path is None:
                raise Stop(EXIT_USAGE, "input_invalid")
            rows = changed[kind].setdefault(path, [dict(r) for r in side.files[path]])
            rows.extend(dict(r) for r in side.new[fid])
        _validated_files(ctx, kind, changed[kind])  # schema, inventory, one scope per family, no repeated question
    files: List[Dict[str, Any]] = []
    staged: List[bytes] = []
    for kind in KINDS:
        for path in sorted(changed[kind]):
            blob = dump_rows(changed[kind][path], mark=kind == "sealed")
            files.append({"path": path.relative_to(ctx.private).as_posix(), "kind": kind, "old_sha256": sha_file(path), "new_sha256": sha_bytes(blob)})
            staged.append(blob)
    report: Dict[str, Any] = {}
    for kind, side in sides.items():
        totals = [len(fam.rows) + len(side.new.get(fid, [])) for fid, fam in side.fams.items()]
        report[kind] = {
            "families": len(side.new), "rows": sum(len(v) for v in side.new.values()),
            "phrasings": [sum(1 for n in totals if n == 1), sum(1 for n in totals if n == 2), sum(1 for n in totals if n == 3), sum(1 for n in totals if n > 3)],
        }
    tx = {"version": 1, "dispatch": dispatch, "pool_sha256": pool_sha, "dev_sha256": dev_sha, "files": files, "report": report}

    # commit: the whole transaction is decided HERE and journalled (the new bytes first, the journal last); finish_paraphrase runs it
    data_dir = paraphrase_data_dir(ctx, dispatch)
    shutil.rmtree(data_dir, ignore_errors=True)
    for index, blob in enumerate(staged):
        write_atomic(data_dir / f"{index}.stage", blob)
    write_json(journal, tx)
    _paraphrase_crash("after_journal")
    emit_paraphrase(ctx, finish_paraphrase(ctx, journal))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Approvals, sheets, apply
# ---------------------------------------------------------------------------
def approvals_path(ctx: Ctx, kind: str) -> Path:
    return ctx.work_for(kind) / "approvals.json"


def load_approvals(ctx: Ctx, kind: str) -> Dict[str, Any]:
    path = approvals_path(ctx, kind)
    if not path.is_file():
        return {"version": 1, "approved": {}, "pending_confirm": {}, "rejected": [], "escalated": []}
    doc = read_json(path)
    if not (isinstance(doc, dict) and doc.get("version") == 1 and isinstance(doc.get("approved"), dict)):
        raise Stop(EXIT_USAGE, "input_invalid")
    for key, default in (("pending_confirm", {}), ("rejected", []), ("escalated", [])):
        doc.setdefault(key, default)
    return doc


def save_approvals(ctx: Ctx, kind: str, doc: Mapping[str, Any]) -> None:
    write_json(approvals_path(ctx, kind), doc)


def kind_components(ctx: Ctx, kind: str) -> List[Path]:
    return sealed_components(ctx) if kind == "sealed" else dev_components(ctx)


def load_kind(ctx: Ctx, kind: str) -> Tuple[Dict[Path, List[Dict[str, Any]]], Set[str]]:
    """``({component file: its rows}, quarantined family ids)`` for sealed or development."""
    gone = quarantined_ids(ctx, kind)
    files: Dict[Path, List[Dict[str, Any]]] = {}
    for path in kind_components(ctx, kind):
        loader = load_sealed_rows if kind == "sealed" else load_dev_rows
        files[path] = loader(ctx, path)
    return files, gone


def live_rows(files: Mapping[Path, List[Dict[str, Any]]], gone: Set[str]) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    return {str(r["id"]): (p, r) for p, rows in files.items() for r in rows if str(r["family_id"]) not in gone}


def needs_approval(kind: str, row: Mapping[str, Any]) -> bool:
    """Sealed rows always; development rows unless a plain legacy first phrasing (b5's validation list)."""
    if kind == "sealed":
        return True
    plain_legacy = row.get("source") == "legacy" and row_no(row) == 0 and row["scope"] != "refuse" and not row.get("ambiguous")
    return not plain_legacy


def split_of_file(kind: str, path: Path) -> Optional[str]:
    return "sealed" if kind == "sealed" else dev_split_of(path)


def frozen_files(ledger: SealedLedger, kind: str, paths: Iterable[Path]) -> Set[Path]:
    """The component files whose own split is frozen (a development kind has several splits; only those count)."""
    out: Set[Path] = set()
    for path in paths:
        split = split_of_file(kind, path)
        if split is not None and ledger.freeze_record(split) is not None:
            out.add(path)
    return out


def load_pages(ctx: Ctx) -> Dict[str, str]:
    """``{section label: "p12" | "p12-14"}`` from the handbook dump (b0); empty if absent."""
    dump = ctx.handbook_dump if ctx.handbook_dump is not None else ctx.repo / sl.HANDBOOK_DUMP
    pages: Dict[str, str] = {}
    try:
        text = dump.read_bytes().decode("utf-8", errors="replace") if dump.is_file() else ""
    except OSError:
        return pages
    for line in text.split("\n"):
        try:
            obj = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        label, ps, pe = obj.get("label"), obj.get("page_start"), obj.get("page_end")
        if isinstance(label, str) and isinstance(ps, int) and isinstance(pe, int):
            pages[label] = f"p{ps}" if ps == pe else f"p{ps}-{pe}"
    return pages


def render_evidence(evidence: Sequence[Sequence[str]], pages: Mapping[str, str]) -> str:
    """AND groups joined by `` & ``, OR alternatives by `` | ``, each label with its pages."""
    return " & ".join(" | ".join(f"{lab} [{pages[lab]}]" if lab in pages else lab for lab in group) for group in evidence)


def render_gaps(gaps: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{g['id']}: {', '.join(g['keywords'])}" for g in gaps)


def csv_bytes(columns: Sequence[str], records: Iterable[Mapping[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow({c: rec.get(c, "") for c in columns})
    return buf.getvalue().encode("utf-8")


def read_sheet(path: Path, columns: Sequence[str]) -> Tuple[List[Dict[str, str]], str]:
    """The sheet's lines and its sha256; the header must be exactly ``columns``."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Stop(EXIT_USAGE, "input_missing") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if list(reader.fieldnames or []) != list(columns):
        raise Stop(EXIT_USAGE, "sheet_invalid")
    lines = [{k: (v or "") for k, v in rec.items() if k is not None} for rec in reader]
    return lines, hashlib.sha256(raw).hexdigest()


def ticked(cell: str) -> bool:
    return cell.strip().lower() in ("x", "1", "y", "yes", "true", "ok")


def sheets_dir(ctx: Ctx, kind: str) -> Path:
    return ctx.work_for(kind) / "sheets"


def sheet_dest(ctx: Ctx, kind: str, filename: str) -> Path:
    """A file under ``<work>/sheets/``, contained (symlink-safe) there."""
    return safe_dest(ctx.work_for(kind), "sheets", filename)


def cmd_sheet(ctx: Ctx, kind: str, round_no: Optional[int], name: Optional[str], include_all: bool) -> int:
    """A review sheet (CSV): one line per phrasing, owner columns A / C / R / E, corrections, colleague's key."""
    checked_name(name)
    recover_state(ctx)
    files, gone = load_kind(ctx, kind)
    approvals = load_approvals(ctx, kind)
    pages = load_pages(ctx)
    out: List[Dict[str, Any]] = []
    for path, rows in files.items():
        if round_no is not None and path.name not in (f"sealed_r{round_no}.jsonl", f"tuning_r{round_no}.jsonl", f"realistic_r{round_no}.jsonl"):
            continue
        for row in rows:
            rid, h = str(row["id"]), eval_schema.row_hash(row)
            if str(row["family_id"]) in gone or not needs_approval(kind, row):
                continue
            bound = approvals["approved"].get(rid, {}).get("row_hash") == h
            pending = approvals["pending_confirm"].get(rid, {}).get("row_hash") == h
            if (bound or pending) and not include_all:
                continue
            out.append(
                {
                    "row_id": rid, "family_id": row["family_id"], "row_hash": h, "phrasing_no": row_no(row),
                    "phrasing": row["question"], "scope": row["scope"], "register": row.get("register") or "",
                    "source": row.get("source") or "", "evidence": render_evidence(row["evidence"], pages),
                    "gaps": render_gaps(row["gaps"]), "ambiguous": "yes" if row.get("ambiguous") else "no",
                }
            )
    out.sort(key=lambda r: (r["family_id"], r["phrasing_no"]))
    label = name or f"sheet_{kind}_{'r' + str(round_no) if round_no else 'all'}"
    target = sheet_dest(ctx, kind, f"{label}.csv")
    write_atomic(target, csv_bytes(SHEET_COLUMNS, out))
    ctx.printer.emit("sheet", kind=kind, rows=len(out), families=len({r["family_id"] for r in out}), sha256=sha_file(target))
    return EXIT_OK


def _validated_files(ctx: Ctx, kind: str, changed: Mapping[Path, List[Dict[str, Any]]]) -> None:
    """Re-validate every rewritten file with the one loader (schema, inventory, one scope per family)."""
    for path, rows in changed.items():
        if not rows:
            continue
        tmp = path.with_name(f".{path.name}.validate.tmp")
        try:
            write_atomic(tmp, dump_rows(rows, mark=kind == "sealed"))
            (load_sealed_rows if kind == "sealed" else load_dev_rows)(ctx, tmp)
        finally:
            tmp.unlink(missing_ok=True)


def _quarantine(ctx: Ctx, kind: str, fams: Mapping[str, Sequence[Mapping[str, Any]]], *, status: Optional[str] = "rejected") -> None:
    """Append whole families to the kind's ``quarantine.jsonl`` (marked for sealed), before they leave a component.

    ``status`` is the ``owner_status`` the rows get (``rejected``); a demotion passes ``None`` and keeps theirs.
    """
    if not fams:
        return
    target = quarantine_target(ctx, kind)
    existing = (load_sealed_rows if kind == "sealed" else load_dev_rows)(ctx, target) if target.is_file() else []
    keep = [r for r in existing if str(r["family_id"]) not in fams]
    add = [dict(r, owner_status=status) if status else dict(r) for rows in fams.values() for r in rows]
    write_atomic(target, dump_rows([*keep, *add], mark=kind == "sealed"))


def _rewrite(ctx: Ctx, kind: str, files: Mapping[Path, List[Dict[str, Any]]]) -> None:
    for path, rows in files.items():
        if rows:
            write_atomic(path, dump_rows(rows, mark=kind == "sealed"))
        else:
            path.unlink(missing_ok=True)


def cmd_apply(ctx: Ctx, kind: str, sheet: Optional[str], confirm: Optional[str]) -> int:
    """Apply the owner's marks: approvals bound to row hash + sheet sha256; changed rows need a confirm sheet."""
    if (sheet is None) == (confirm is None):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    ledger = ctx.ledger()
    files, gone = load_kind(ctx, kind)
    rows_by_id = live_rows(files, gone)
    frozen = frozen_files(ledger, kind, files)

    def refuse_frozen(rid: str) -> None:
        """A line naming a row of a frozen split is refused (rows of the other splits are unaffected)."""
        if rid in rows_by_id and rows_by_id[rid][0] in frozen:
            raise Stop(EXIT_REFUSED, "frozen")

    approvals = load_approvals(ctx, kind)
    if confirm is not None:
        lines, sha = read_sheet(Path(confirm), CONFIRM_COLUMNS)
        bound = 0
        for line in lines:
            refuse_frozen(line["row_id"])
            if not ticked(line["OK"]):
                continue
            rid = line["row_id"]
            pending = approvals["pending_confirm"].get(rid)
            if rid not in rows_by_id or pending is None or line["row_hash"] != pending["row_hash"] \
                    or eval_schema.row_hash(rows_by_id[rid][1]) != line["row_hash"]:
                raise Stop(EXIT_REFUSED, "sheet_stale")
            approvals["approved"][rid] = {"row_hash": line["row_hash"], "sheet_sha256": sha}
            del approvals["pending_confirm"][rid]
            bound += 1
        save_approvals(ctx, kind, approvals)
        ctx.printer.emit("apply", kind=kind, confirmed=bound, pending_confirm=len(approvals["pending_confirm"]), sha256=sha)
        return EXIT_OK

    lines, sha = read_sheet(Path(sheet), SHEET_COLUMNS)  # type: ignore[arg-type]
    approved: Dict[str, str] = {}
    changed: Dict[str, Dict[str, Any]] = {}
    rejected: Set[str] = set()
    escalated: Set[str] = set()
    pending = stale = conflict = bad = 0
    for line in lines:
        rid = line["row_id"]
        refuse_frozen(rid)
        if rid not in rows_by_id:
            stale += 1
            continue
        _, row = rows_by_id[rid]
        if line["row_hash"] != eval_schema.row_hash(row):
            stale += 1
            continue
        marks = [m for m in OWNER_MARKS if ticked(line[m])]
        if len(marks) > 1:
            conflict += 1
        elif not marks:
            pending += 1
        elif marks[0] == "A":
            approved[rid] = eval_schema.row_hash(row)
        elif marks[0] == "R":
            rejected.add(str(row["family_id"]))
        elif marks[0] == "E":
            escalated.add(str(row["family_id"]))
        else:  # C: the corrections cell is a JSON object over CORRECTABLE keys
            try:
                patch = json.loads(line["corrections"])
                ok = isinstance(patch, dict) and bool(patch) and set(patch) <= set(CORRECTABLE)
            except ValueError:
                ok = False
            if not ok:
                bad += 1
                continue
            changed[rid] = dict(row, **patch)
    if stale:
        raise Stop(EXIT_REFUSED, "sheet_stale", lines=stale)
    if conflict or bad:
        raise Stop(EXIT_REFUSED, "sheet_conflict", lines=conflict + bad)
    new_files = {p: [dict(r) for r in rows] for p, rows in files.items()}
    quarantined: Dict[str, List[Dict[str, Any]]] = {}
    for path, rows in new_files.items():
        for i, row in enumerate(rows):
            rid, fid = str(row["id"]), str(row["family_id"])
            if rid in approved:
                row["owner_status"] = "approved"
            elif rid in changed:
                rows[i] = dict(changed[rid], owner_status="changed")
            elif fid in escalated:
                row["second_status"] = "pending"
        for row in [r for r in rows if str(r["family_id"]) in rejected]:
            quarantined.setdefault(str(row["family_id"]), []).append(row)
        rows[:] = [r for r in rows if str(r["family_id"]) not in rejected]
    touched = {p: rows for p, rows in new_files.items() if rows != files[p]}
    _validated_files(ctx, kind, touched)
    _quarantine(ctx, kind, quarantined)
    _rewrite(ctx, kind, touched)
    for rid, h in approved.items():
        approvals["approved"][rid] = {"row_hash": h, "sheet_sha256": sha}
        approvals["pending_confirm"].pop(rid, None)
    final = live_rows(new_files, gone | set(quarantined))
    for rid in changed:
        approvals["approved"].pop(rid, None)
        approvals["pending_confirm"][rid] = {"row_hash": eval_schema.row_hash(final[rid][1]), "sheet_sha256": sha}
    for rid, (_, row) in rows_by_id.items():
        if str(row["family_id"]) in rejected:
            approvals["approved"].pop(rid, None)
            approvals["pending_confirm"].pop(rid, None)
    approvals["rejected"] = sorted({*approvals["rejected"], *rejected})
    approvals["escalated"] = sorted({*approvals["escalated"], *escalated})
    save_approvals(ctx, kind, approvals)
    if changed:
        pages = load_pages(ctx)
        recs = []
        for rid in sorted(changed):
            row = final[rid][1]
            recs.append(
                {
                    "row_id": rid, "family_id": row["family_id"], "row_hash": eval_schema.row_hash(row), "phrasing": row["question"],
                    "scope": row["scope"], "evidence": render_evidence(row["evidence"], pages), "gaps": render_gaps(row["gaps"]),
                    "ambiguous": "yes" if row.get("ambiguous") else "no",
                }
            )
        write_atomic(sheet_dest(ctx, kind, f"{Path(str(sheet)).stem}.confirm.csv"), csv_bytes(CONFIRM_COLUMNS, recs))
    ctx.printer.emit(
        "apply", kind=kind, approved=len(approved), changed=len(changed), rejected_families=len(rejected),
        escalated_families=len(escalated), pending=pending, sha256=sha,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# escalate, calibrate, disagree
# ---------------------------------------------------------------------------
FORM_HEAD = (
    "Please give your own independent view of the answer key for each question below. Tick one: ANSWER (the\n"
    "Handbook answers it), PARTLY (it answers some of it), DECLINE (it does not deal with it, or it needs advice\n"
    "beyond it). If ANSWER or PARTLY, list the Handbook paragraph numbers a good answer relies on and say whether\n"
    "each is needed or whether any one would do. If PARTLY, say which part the Handbook does not cover. Then tick\n"
    "sure / fairly sure / unsure. Please keep these questions to yourself and return the form only to the owner.\n"
)
FORM_ITEM_TAIL = "  Your view:  [ ] answer   [ ] partly   [ ] decline\n  Paragraphs:\n  Not covered (if partly):\n  Confidence: [ ] sure  [ ] fairly sure  [ ] unsure\n  Comment:\n"


def _family_rows(ctx: Ctx, kind: str) -> Dict[str, List[Dict[str, Any]]]:
    files, gone = load_kind(ctx, kind)
    by: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in live_rows(files, gone).values():
        by.setdefault(str(row["family_id"]), []).append(row)
    return {fid: sorted(rs, key=row_no) for fid, rs in by.items()}


def _write_form(ctx: Ctx, kind: str, label: str, title: str, items: Sequence[Tuple[str, Sequence[Mapping[str, Any]], str]]) -> Path:
    """A form: item refs, the phrasings and (escalation) the owner's one-line doubt. Never the owner's key."""
    out = [title + "\n", FORM_HEAD]
    refs: Dict[str, str] = {}
    for i, (fid, rows, doubt) in enumerate(items, 1):
        ref = f"E{i:02d}"
        refs[ref] = fid
        out.append(f"\nItem {ref}\n")
        out.extend(f"  Phrasing {n}: {r['question']}\n" for n, r in enumerate(rows, 1))
        if doubt:
            out.append(f"  Owner's doubt: {doubt}\n")
        out.append(FORM_ITEM_TAIL)
    form, refs_file = (safe_dest(ctx.work_for(kind), "escalations", f"{label}{ext}") for ext in (".txt", ".refs.json"))
    write_atomic(form, "".join(out).encode("utf-8"))
    write_json(refs_file, refs)
    return form


def cmd_escalate(ctx: Ctx, kind: str, sheet: str, name: Optional[str]) -> int:
    """Forms for the sheet's E-marked items, under ``work/escalations/`` of the kind (sealed or development)."""
    checked_name(name)
    lines, _ = read_sheet(Path(sheet), SHEET_COLUMNS)
    fams = _family_rows(ctx, kind)
    doubts: Dict[str, str] = {}
    for line in lines:
        if ticked(line["E"]) and line["family_id"] in fams:
            doubts.setdefault(line["family_id"], "")
            doubts[line["family_id"]] = doubts[line["family_id"]] or line["doubt"].strip().replace("\n", " ")
    if not doubts:
        raise Stop(EXIT_REFUSED, "nothing_to_do")
    items = [(fid, fams[fid], doubts[fid]) for fid in sorted(doubts)]
    label = f"form_{name or Path(sheet).stem}"
    form = _write_form(ctx, kind, label, "Escalation form", items)
    ctx.printer.emit("escalate", kind=kind, forms=1, items=len(items), sha256=sha_file(form))
    return EXIT_OK


def _frozen_family_rows(ctx: Ctx, ledger: SealedLedger, kind: str) -> Dict[str, List[Dict[str, Any]]]:
    """The families of the frozen split file(s) of ``kind`` (sealed: the sealed file; development: tuning and realistic).

    Every file must still be the frozen one (its sha256 is its freeze record's ``set_sha256``), else
    ``frozen_file_changed``; the rows are the file's own, not the component files'.
    """
    by: Dict[str, List[Dict[str, Any]]] = {}
    for name in ("sealed",) if kind == "sealed" else DEV_SPLITS:
        path = frozen_path(ctx, name)
        if not path.is_file():
            raise Stop(EXIT_REFUSED, "splits_not_frozen")
        if sha_file(path) != ledger.freeze_record(name)["set_sha256"]:
            raise Stop(EXIT_REFUSED, "frozen_file_changed")
        rows = load_sealed_rows(ctx, path) if name == "sealed" else load_dev_rows(ctx, path)
        for row in rows:
            by.setdefault(str(row["family_id"]), []).append(row)
    return {fid: sorted(rs, key=row_no) for fid, rs in by.items()}


def cmd_calibrate(ctx: Ctx, kind: str) -> int:
    """Draw 15 approved families by the prereg seed (5 refuse-or-partial, 10 answer) for a calibration form.

    Only under the epoch's registered prereg (committed, unmodified, pushed), only after every split is frozen,
    and only from the rows of the frozen split file(s) of ``kind`` (sealed: the sealed file; development: tuning
    and realistic), each of which must still be the frozen one. Once per kind, durably: the ledger's
    ``calibration_draw`` record is written before the form and sheet files; a later run recomputes the draw and,
    if it is the recorded one, re-emits it (finishing any file a crash left out) or else refuses
    ``calibration_drawn``.
    """
    prereg = load_prereg(ctx)
    use_seed = int(prereg["seeds"]["calibration"])
    ledger = ctx.ledger()
    bind_epoch_prereg(ctx, prereg, ledger)
    if any(ledger.freeze_record(sp_name) is None for sp_name in SPLITS):
        raise Stop(EXIT_REFUSED, "splits_not_frozen")
    fams = _frozen_family_rows(ctx, ledger, kind)
    approvals = load_approvals(ctx, kind)["approved"]
    ok = [
        fid for fid, rows in fams.items()
        if all(approvals.get(str(r["id"]), {}).get("row_hash") == eval_schema.row_hash(r) for r in rows)
    ]
    hard = sorted(f for f in ok if fams[f][0]["scope"] != "answer")
    easy = sorted(f for f in ok if fams[f][0]["scope"] == "answer")
    if len(hard) < 5 or len(easy) < 10:
        raise Stop(EXIT_REFUSED, "calibration_short", refuse_or_partial=len(hard), answer=len(easy))
    rng = random.Random(use_seed)
    drawn = sorted([*rng.sample(hard, 5), *rng.sample(easy, 10)])
    draw_doc = {"seed": use_seed, "families": drawn, "n": len(drawn)}
    draw_sha = sha_text(canon(draw_doc))
    recorded = ledger.calibration_draw(kind)
    if recorded is not None and recorded["draw_sha256"] != draw_sha:
        raise Stop(EXIT_REFUSED, "calibration_drawn")
    draw_path = ctx.work_for(kind) / "calibration_draw.json"
    if draw_path.is_file() and read_json(draw_path, "input_invalid") != draw_doc:
        raise Stop(EXIT_REFUSED, "calibration_drawn")
    if recorded is None:
        ledger.record_calibration_draw(kind, draw_sha)  # durable before any file: a crash re-run finishes the files
    form_path = safe_dest(ctx.work_for(kind), "escalations", "calibration_form.txt")
    if not form_path.is_file():
        items = [(fid, fams[fid][:1], "") for fid in drawn]
        _write_form(ctx, kind, "calibration_form", "Calibration form (a random few questions, used to check the key as a whole)", items)
    sheet_path = sheet_dest(ctx, kind, "calibration.csv")
    if not sheet_path.is_file():
        pages = load_pages(ctx)
        recs = [
            {
                "row_id": r["id"], "family_id": fid, "row_hash": eval_schema.row_hash(r), "phrasing_no": row_no(r), "phrasing": r["question"],
                "scope": r["scope"], "evidence": render_evidence(r["evidence"], pages), "gaps": render_gaps(r["gaps"]),
                "ambiguous": "yes" if r.get("ambiguous") else "no",
            }
            for fid in drawn for r in fams[fid][:1]
        ]
        write_atomic(sheet_path, csv_bytes(SHEET_COLUMNS, recs))
    digest = sha_file(draw_path) if draw_path.is_file() else write_json(draw_path, draw_doc)
    ctx.printer.emit("calibrate", kind=kind, drawn=len(drawn), refuse_or_partial=5, answer=10, sha256=digest, form_sha256=sha_file(form_path))
    return EXIT_OK


def related_labels(a: str, b: str) -> bool:
    """Related matching: equal, or one is a dotted ancestor of the other."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def compare_groups(owner: Sequence[Sequence[str]], other: Sequence[Sequence[str]]) -> Dict[str, bool]:
    """AND/OR structure: a missing / extra required group, or a different alternative set (related matching)."""

    def linked(g: Sequence[str], h: Sequence[str]) -> bool:
        return any(related_labels(x, y) for x in g for y in h)

    extra = any(not any(linked(g, h) for h in other) for g in owner)
    missing = any(not any(linked(g, h) for g in owner) for h in other)
    alt = False
    for g in owner:
        for h in other:
            if linked(g, h) and not (all(any(related_labels(x, y) for y in h) for x in g) and all(any(related_labels(y, x) for x in g) for y in h)):
                alt = True
    return {"missing_group": missing, "extra_group": extra, "alt_set_diff": alt}


def gaps_differ(owner: Sequence[Mapping[str, Any]], other: Sequence[Mapping[str, Any]]) -> bool:
    """Partial gaps differ if the counts differ or a gap has no counterpart sharing a keyword."""

    def kw(gap: Mapping[str, Any]) -> Set[str]:
        return {w for k in gap.get("keywords", []) if isinstance(k, str) for w in tokens_of(k)}

    if len(owner) != len(other):
        return True
    free = [kw(g) for g in other]
    for gap in owner:
        mine = kw(gap)
        hit = next((i for i, theirs in enumerate(free) if mine & theirs), None)
        if hit is None:
            return True
        free.pop(hit)
    return False


def _pm(interval: Any) -> Dict[str, int]:
    return {} if isinstance(interval, str) else {"lo_pm": round(1000 * interval[0]), "hi_pm": round(1000 * interval[1])}


def parse_colleague_key(text: str) -> Optional[Dict[str, Any]]:
    try:
        key = json.loads(text)
    except ValueError:
        return None
    ok = (
        isinstance(key, dict) and set(key) <= {"scope", "evidence", "gaps"} and key.get("scope") in SCOPES
        and isinstance(key.get("evidence", []), list)
        and all(isinstance(g, list) and all(isinstance(x, str) for x in g) for g in key.get("evidence", []))
        and isinstance(key.get("gaps", []), list)
        and all(isinstance(g, dict) and isinstance(g.get("keywords", []), list) for g in key.get("gaps", []))
    )
    return key if ok else None


def cmd_disagree(ctx: Ctx, kind: str, sheet: str, calibration: bool) -> int:
    """Colleague's key vs the owner's, per dimension (scope, AND/OR groups, gaps); both keys are kept."""
    lines, _ = read_sheet(Path(sheet), SHEET_COLUMNS)
    fams = _family_rows(ctx, kind)
    keys: Dict[str, Dict[str, Any]] = {}
    invalid = 0
    for line in lines:
        text = line["colleague_key"].strip()
        if not text or line["family_id"] in keys or line["family_id"] not in fams:
            continue
        key = parse_colleague_key(text)
        if key is None:
            invalid += 1
        else:
            keys[line["family_id"]] = key
    if invalid:
        raise Stop(EXIT_USAGE, "colleague_key_invalid", keys=invalid)
    tally = {"scope_n": 0, "scope_disagree": 0, "group_n": 0, "group_disagree": 0, "missing_group": 0, "extra_group": 0,
             "alt_set_diff": 0, "gap_n": 0, "gap_disagree": 0}
    record: Dict[str, Any] = {}
    for fid, key in sorted(keys.items()):
        mine = fams[fid][0]
        dims = {"scope": key["scope"] != mine["scope"], "groups": False, "gaps": False}
        tally["scope_n"] += 1
        tally["scope_disagree"] += dims["scope"]
        if key["scope"] != "refuse" and mine["scope"] != "refuse":
            diff = compare_groups(mine["evidence"], key.get("evidence", []))
            tally["group_n"] += 1
            dims["groups"] = any(diff.values())
            tally["group_disagree"] += dims["groups"]
            for name, flag in diff.items():
                tally[name] += flag
        if key["scope"] == "partial" and mine["scope"] == "partial":
            tally["gap_n"] += 1
            dims["gaps"] = gaps_differ(mine["gaps"], key.get("gaps", []))
            tally["gap_disagree"] += dims["gaps"]
        record[fid] = {
            "owner": {"scope": mine["scope"], "evidence": mine["evidence"], "gaps": mine["gaps"]}, "colleague": key,
            "disagrees": dims,
        }
    write_json(ctx.work_for(kind) / "disagreement.json", record)
    files, gone = load_kind(ctx, kind)
    for path, rows in files.items():
        dirty = False
        for row in rows:
            fid = str(row["family_id"])
            if fid in record:
                status = "disagree" if any(record[fid]["disagrees"].values()) else "agree"
                if row.get("second_status") != status:
                    row["second_status"], dirty = status, True
        if dirty:
            write_atomic(path, dump_rows(rows, mark=kind == "sealed"))
    out: Dict[str, Any] = {"sample": "random_sample_n15" if calibration else "owner_selected_sample", "compared": len(keys), **tally}
    for dim in ("scope", "group", "gap"):
        n, k = tally[f"{dim}_n"], tally[f"{dim}_disagree"]
        out.update({f"{dim}_{key}": v for key, v in _pm(eval_stats.wilson_interval(k, n)).items()})
    ctx.printer.emit("disagree", **out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# exclude
# ---------------------------------------------------------------------------
def release_payloads(ledger: SealedLedger) -> List[Dict[str, Any]]:
    return [ledger.load_payload(assign_name(int(r["round"])), r["payload_sha256"]) for r in ledger.records("release")]


def release_edges(payloads: Iterable[Mapping[str, Any]]) -> List[List[str]]:
    """The group edges of every released round, de-duplicated and sorted (what ``twins_sealed`` stores)."""
    return [list(e) for e in sorted({(str(a), str(b)) for p in payloads for a, b in p["twins"]["edges"]})]


def sealed_group_index(ledger: SealedLedger, live: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """``{group id: members}`` over every released round; a family in no group is its own group.

    The ids come from :func:`src.sealed_ledger.group_ids` (the one definition the slot gate shares): the
    components of the released edges over ``live``, the families that are in the sealed set now (callers pass
    the surviving ``sealed_r*.jsonl`` families, which after the sealed freeze are exactly the frozen set).
    ``live=None`` means every family assigned to sealed at release.
    """
    payloads = release_payloads(ledger)
    families = sorted(set(live)) if live is not None else sorted(
        {fid for p in payloads for fid, split in p["assignment"].items() if split == "sealed"}
    )
    index: Dict[str, List[str]] = {}
    for fid, gid in group_ids(families, [tuple(e) for e in release_edges(payloads)]).items():
        index.setdefault(gid, []).append(fid)
    return {gid: sorted(members) for gid, members in sorted(index.items())}


def group_id_of(groups: Mapping[str, Sequence[str]], fid: str) -> Optional[str]:
    for gid, members in groups.items():
        if fid in members:
            return gid
    return None


def ledger_exclude(ledger: SealedLedger, group: str, reason: str, members: Iterable[str]) -> None:
    """Append an exclusion naming its member families (``SealedLedger.exclude(..., members=...)``).

    Adapter: a ledger whose ``exclude`` predates ``members`` records the group id alone.
    """
    if "members" in inspect.signature(ledger.exclude).parameters:
        ledger.exclude(group, reason, members=sorted(set(members)))
    else:
        ledger.exclude(group, reason)


def excluded_group_ids(ledger: SealedLedger, groups: Mapping[str, Sequence[str]]) -> Set[str]:
    """The ids in ``groups`` that are excluded: named by the recorded id, or holding any family an exclusion named.

    A group renamed since the exclusion (its smallest member left the sealed set) still resolves, by members.
    Every recorded id is kept too, so a caller can test ``gid in result`` for ids that are not in ``groups``.
    """
    found: Set[str] = set(ledger.exclusions())
    named_fn = getattr(ledger, "excluded_families", None)
    if callable(named_fn):
        named = named_fn()
        if isinstance(named, Mapping):
            named = {f for members in named.values() for f in members}
        found |= {gid for gid, members in groups.items() if set(named) & set(members)}
    return found


def current_sealed_side(ctx: Ctx, ledger: SealedLedger) -> Dict[str, Fam]:
    """The sealed families as they stand now.

    Once the sealed set is frozen that is the frozen file (its sha256 checked against the freeze record), so
    groups and twin handling never depend on component files a later command could touch; before it, the
    surviving ``sealed_r*.jsonl`` families.
    """
    record = ledger.freeze_record("sealed")
    if record is None:
        return load_sealed_side(ctx)
    path = frozen_path(ctx, "sealed")
    if not path.is_file() or sha_file(path) != record["set_sha256"]:
        raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed")
    return group_families(load_sealed_rows(ctx, path), path, "sealed")


def cmd_exclude(ctx: Ctx, families: Optional[Sequence[str]], group: Optional[str], reason: str) -> int:
    """Append an exclusion against the WHOLE sealed group of each named family (never a move or an unmark).

    ``--family`` may repeat (an S-hit can name several sealed families: ``audit`` prints one line each); every
    family is resolved to its group BEFORE anything is written, so one unknown family changes nothing. Families of
    one group give one exclusion. Each exclusion names its member families and records which slots were stored when it was made.
    Once slot 0 (or 0b) is stored EVERY reason is refused (``exclusion_late``, r8): the confirmatory comparison uses the
    exclusions recorded before slot 0 was stored plus each confirmatory slot's own ``s_hit`` ones, and those are written only
    by the in-process publication scanner (``src.sealed_run.render_and_publish``, which records ``scan_slot``), never by this
    command (a hand-made ``s_hit`` after slot 0 would change the population with no scan receipt behind it).
    """
    if reason not in EXCLUSION_REASONS or (not families) == (group is None):
        raise Stop(EXIT_USAGE, "usage")
    recover_state(ctx)
    ledger = ctx.ledger()
    if reason in LATE_EXCLUSION_REASONS and any(slot in ("0", "0b") for slot in ledger.stored_slots()):
        # the confirmatory comparison uses the exclusions recorded before slot 0 was stored (prereg ``adoption.exclusions``):
        # whatever is found after that point (an exposure, a twin of a development version, a hit) is not a new exclusion made
        # by hand; a slot's own S-hits are written by the publication scanner, with the slot they came from
        raise Stop(EXIT_REFUSED, "exclusion_late")
    groups = sealed_group_index(ledger, current_sealed_side(ctx, ledger))
    wanted: List[str] = []
    if families:
        for family in families:
            if not _FAMILY_ID.fullmatch(family):
                raise Stop(EXIT_USAGE, "usage")
            gid = group_id_of(groups, family)
            if gid is None:
                raise Stop(EXIT_REFUSED, "no_group")
            if gid not in wanted:
                wanted.append(gid)
    else:
        if group not in groups:
            raise Stop(EXIT_REFUSED, "no_group")
        wanted.append(str(group))
    already = excluded_group_ids(ledger, groups)
    for gid in wanted:
        if gid in already:
            ctx.printer.emit("exclude", outcome="already", members=len(groups[gid]))
            continue
        ledger_exclude(ledger, gid, reason, groups[gid])
        ctx.printer.emit("exclude", outcome="ok", reason=reason, members=len(groups[gid]))
    return EXIT_OK


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------
def frozen_path(ctx: Ctx, split: str) -> Path:
    where, name = FROZEN_FILES[split]
    return (ctx.sealed_dir if where == "sealed" else ctx.dev_dir) / name


def _ceil_share(share: Any, quota: int) -> int:
    from fractions import Fraction

    f = Fraction(repr(share)) if isinstance(share, float) else Fraction(share)
    return -((-f.numerator * quota) // f.denominator)


def evaluate_split(
    fams: Mapping[str, Fam], split: str, prereg: Mapping[str, Any], placement: Mapping[str, str],
    groups: Mapping[str, Sequence[str]], excluded: Set[str], all_groups: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[str]:
    """The constraint names a final split violates (empty = ready): band, strata, min_share, floor, group_invariant.

    ``groups`` (the sealed set's groups) and ``excluded`` (group ids) decide the floor. The group invariant is
    checked over ``all_groups`` -- the groups of EVERY released family, development included (default: ``groups``) --
    so a group split across two splits is seen even when only some of its members are sealed.
    """
    q = len(fams)
    refuse = sum(1 for f in fams.values() if f.scope == "refuse")
    bad: List[str] = []
    if not (BAND_MIN_DEN * refuse >= BAND_MIN_NUM * q and BAND_MAX_DEN * refuse <= BAND_MAX_NUM * q):
        bad.append("band")
    strata = (prereg.get("strata") or {}).get(split, {})
    if any(sum(1 for f in fams.values() if f"{f.scope}|{f.chapter}" == key) < need for key, need in strata.items()):
        bad.append("strata")
    shares = (prereg.get("min_share") or {}).get(split, {})
    if any(sum(1 for f in fams.values() if source_class(f.source) == src) < _ceil_share(share, q) for src, share in shares.items()):
        bad.append("min_share")
    if split == "sealed":
        answerable = 0
        for fid, fam in fams.items():
            gid = group_id_of(groups, fid) or fid
            if fam.scope != "refuse" and gid not in excluded:
                answerable += 1
        quota = prereg["quota"]
        if q < int(quota["floor_sealed"]) or answerable < int(quota["floor_answerable"]):
            bad.append("floor")
    for members in (all_groups if all_groups is not None else groups).values():
        if len({placement[m] for m in members if m in placement}) > 1:
            bad.append("group_invariant")
            break
    return bad


def repair_band(
    units: Sequence[Tuple[Any, ...]], floor_q: int, seed: int, max_k: int = 6, budget: int = 400_000, floor_answerable: int = 0,
) -> Optional[List[str]]:
    """The fewest seeded group demotions that bring the sealed band right while the floors still hold.

    ``units`` are ``(group id, families, refuse families[, answerable families])``; ``answerable`` is the number of
    that group's families that count toward the floor (non-refusal and not excluded; default: families minus refuse
    ones). Candidates are shuffled by ``seed``; subsets are tried by increasing size, so the first hit is a minimum,
    and ties are broken by the seeded order. A subset is a hit only if ``floor_q`` families AND ``floor_answerable``
    answerable families remain, so a repair never ends in a floor breach another subset would have avoided.
    """
    order = sorted(units)
    random.Random(seed).shuffle(order)

    def answerable(u: Tuple[Any, ...]) -> int:
        return int(u[3]) if len(u) > 3 else int(u[1]) - int(u[2])

    q, r, a = sum(u[1] for u in order), sum(u[2] for u in order), sum(answerable(u) for u in order)
    tried = 0
    for k in range(1, max_k + 1):
        for combo in itertools.combinations(order, k):
            tried += 1
            if tried > budget:
                return None
            q2, r2 = q - sum(u[1] for u in combo), r - sum(u[2] for u in combo)
            a2 = a - sum(answerable(u) for u in combo)
            if q2 >= floor_q and a2 >= floor_answerable and BAND_MIN_DEN * r2 >= BAND_MIN_NUM * q2 and BAND_MAX_DEN * r2 <= BAND_MAX_NUM * q2:
                return [u[0] for u in combo]
    return None


def move_to_quarantine(ctx: Ctx, kind: str, fids: Set[str], *, status: Optional[str]) -> int:
    """Move whole sealed families to quarantine (demotion or rejection): quarantine first, then the components.

    Idempotent: a family already moved is in no component, so a repeat (a crash recovery) finds nothing more to do.
    """
    files, gone = load_kind(ctx, kind)
    quarantined: Dict[str, List[Dict[str, Any]]] = {}
    new_files: Dict[Path, List[Dict[str, Any]]] = {}
    for path, rows in files.items():
        keep = [r for r in rows if str(r["family_id"]) not in fids]
        for r in rows:
            if str(r["family_id"]) in fids:
                quarantined.setdefault(str(r["family_id"]), []).append(r)
        if len(keep) != len(rows):
            new_files[path] = keep
    _quarantine(ctx, kind, quarantined, status=status)
    _rewrite(ctx, kind, new_files)
    return len(quarantined)


def _verdict_lines(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())


# -- the freeze transaction: computed first, journalled, then run step by idempotent step --------------------------
def twins_payload_name(sha256: str) -> str:
    """The content name of a sealed freeze's twins record: ``twins_sealed_<first 16 hex of its own sha256>``."""
    return f"twins_sealed_{sha256[:16]}"


def freeze_journal_path(ctx: Ctx, split: str) -> Path:
    return ctx.sealed_work / f"freeze_{split}.journal"


def freeze_data_path(ctx: Ctx, split: str) -> Path:
    return ctx.sealed_work / f"freeze_{split}.data"


def _freeze_crash(name: str) -> None:
    """No-op seam between the freeze transaction's steps (``after_journal``, ``after_frozen``, ``after_payload``,
    ``after_record``, ``after_exclude``, ``after_quarantine``); tests replace it with a function that raises."""


def record_sealed_freeze(ledger: SealedLedger, **fields: Any) -> None:
    """``record_freeze`` for the sealed split: the exclusion digest is computed inside the ledger's own transaction.

    Adapter: a ledger that predates that (it requires the digest, and has no ``excluded_families``) is handed
    the digest of the exclusions seen now instead.
    """
    if hasattr(ledger, "excluded_families"):
        ledger.record_freeze(**fields, exclusions_sha256=None)
    else:
        ledger.record_freeze(**fields, exclusions_sha256=exclusions_digest(ledger.exclusions()))


def _freeze_tx(ctx: Ctx, split: str) -> Dict[str, Any]:
    path = freeze_journal_path(ctx, split)
    try:
        tx = json.loads(path.read_bytes().decode("utf-8"))
        ok = (
            isinstance(tx, dict) and tx.get("version") == 1 and tx.get("split") == split
            and isinstance(tx.get("set_sha256"), str) and isinstance(tx.get("totals"), dict)
            and isinstance(tx.get("leaving"), list) and isinstance(tx.get("exclude"), list) and isinstance(tx.get("emit"), dict)
        )
    except (OSError, ValueError, RecursionError):
        ok = False
    if not ok:
        raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed")
    return tx


def finish_freeze(ctx: Ctx, split: str) -> Dict[str, Any]:
    """Run (or resume) the journalled freeze transaction of ``split``; returns it. Every step is idempotent.

    Order: the frozen file (from the staged bytes), the twins payload, the ledger record, the exclusions and,
    last, the quarantine moves. Nothing is recomputed: a half-done transaction is finished, never re-derived
    from the half-changed world. The journal is removed only when every step is done.
    """
    tx = _freeze_tx(ctx, split)
    ledger = ctx.ledger()
    data_path = freeze_data_path(ctx, split)
    try:
        data = data_path.read_bytes()
    except OSError:
        raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed") from None
    if hashlib.sha256(data).hexdigest() != tx["set_sha256"]:
        raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed")
    target = frozen_path(ctx, split)
    if not target.is_file() or sha_file(target) != tx["set_sha256"]:
        write_atomic(target, data, mode=0o444)
    _freeze_crash("after_frozen")
    twins = tx.get("twins")
    if twins:
        for name in (twins["name"], "twins_sealed"):  # the content name, and the plain alias older readers load
            if ledger.store_payload(name, twins["record"]) != tx["twins_sha256"]:
                raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed")
    _freeze_crash("after_payload")
    existing = ledger.freeze_record(split)
    if existing is None:
        fields: Dict[str, Any] = {"split": split, "set_sha256": tx["set_sha256"], "totals": tx["totals"]}
        if tx.get("prereg_sha256"):
            fields["prereg_sha256"] = tx["prereg_sha256"]
        if split == "sealed":
            record_sealed_freeze(ledger, twins_sha256=tx["twins_sha256"], **fields)
        else:
            ledger.record_freeze(**fields)
    elif existing["set_sha256"] != tx["set_sha256"]:
        raise Stop(EXIT_LEDGER_STOP, "freeze_recovery_failed")
    _freeze_crash("after_record")
    for item in tx["exclude"]:
        if item["group"] not in ledger.exclusions():
            ledger_exclude(ledger, item["group"], "twin_dev_version", item["members"])
    _freeze_crash("after_exclude")
    if tx["leaving"]:
        move_to_quarantine(ctx, "sealed", set(tx["leaving"]), status=None)
    _freeze_crash("after_quarantine")
    freeze_journal_path(ctx, split).unlink(missing_ok=True)
    _fsync_dir(ctx.sealed_work)
    data_path.unlink(missing_ok=True)
    return tx


def emit_freeze(ctx: Ctx, tx: Mapping[str, Any]) -> None:
    extra = {"twins_sha256": tx["twins_sha256"]} if tx.get("twins_sha256") else {}
    ctx.printer.emit(
        "freeze", split=tx["split"], status="frozen", **tx["emit"], sha256=tx["set_sha256"], **tx["totals"], **extra,
    )


def cmd_freeze(ctx: Ctx, split_name: str, verdict: Optional[str], verdict_return: Optional[str]) -> int:
    """Freeze one split (sealed needs no development approval). See the module docstring for the order of checks."""
    split = resolve_split(split_name)
    kind = "sealed" if split == "sealed" else "development"
    prereg = load_prereg(ctx)
    if freeze_journal_path(ctx, split).exists():  # a crash left this split's transaction half done: finish it, nothing more
        tx = _freeze_tx(ctx, split)
        recover_state(ctx)  # finishes the move journal, the releases and every pending freeze, this one included
        emit_freeze(ctx, tx)
        return EXIT_OK
    prereg_sha = bind_prereg_sha(ctx, prereg)  # every freeze is made under a committed, unmodified, pushed prereg
    recover_state(ctx)
    ledger = ctx.ledger()
    check_epoch_prereg(ledger, prereg_sha)
    if ledger.freeze_record(split) is not None:
        raise Stop(EXIT_REFUSED, "already_frozen")
    releases = ledger.records("release")
    if not releases:
        raise Stop(EXIT_REFUSED, "round1_missing")
    payloads = {int(r["round"]): ledger.load_payload(assign_name(int(r["round"])), r["payload_sha256"]) for r in releases}

    # 1. replay every released round from its stored payload
    expected: Dict[str, str] = {}
    dropped: Set[str] = set()
    for payload in payloads.values():
        try:
            replay = split_families(
                payload["families"], payload["seed"], payload["quotas"], payload["strata"], twins=payload["twins"],
                preassigned=payload["preassigned"], min_share=payload["min_share"],
            )
        except (SplitInfeasible, SplitInputError):
            raise Stop(EXIT_REFUSED, "replay_mismatch") from None
        if replay != payload["assignment"]:
            raise Stop(EXIT_REFUSED, "replay_mismatch")
        expected.update({f: s for f, s in payload["assignment"].items() if f not in payload["preassigned"]})
        dropped.update(quarantined_by_release(payload))

    # 2. the released files must still hold what the assignment says
    sealed_frozen = ledger.freeze_record("sealed") is not None
    sealed_side, dev_side = current_sealed_side(ctx, ledger), load_dev(ctx)
    sealed_q, dev_q = quarantined_ids(ctx, "sealed"), quarantined_ids(ctx, "development")
    moved = sum(
        1 for fid, s in expected.items()
        if not ((s == "sealed" and (fid in sealed_side or fid in sealed_q)) or (s != "sealed" and (dev_side.get(fid) and dev_side[fid].split == s or fid in dev_q)))
    ) + sum(1 for fid in dropped if fid not in sealed_q)
    planted = sum(1 for fid in sealed_side if fid not in expected)
    if moved or planted:
        raise Stop(EXIT_REFUSED, "assignment_changed", families=moved + planted)

    # 3. the lexical screen, then the SP's recheck on the final content
    pairs = lexical_pairs(sealed_side, dev_side)
    required = len(sealed_side) * len(dev_side)  # the SP checks every sealed family against every development family
    content = sha_text(canon([sorted(final_row_hashes(sealed_side.values()).items()), sorted(final_row_hashes(dev_side.values()).items())]))
    screen = ctx.sealed_work / f"screen_{split}.json"
    if verdict is None or verdict_return is None:
        write_json(screen, {"content_digest": content, "pairs": [list(p) for p in pairs]})
        raise Stop(EXIT_REFUSED, "recheck_missing", pairs=len(pairs), pairs_required=required, sha256=content)
    if not screen.is_file() or read_json(screen).get("content_digest") != content:
        raise Stop(EXIT_REFUSED, "recheck_stale")
    vpath, rpath = Path(verdict), Path(verdict_return)
    line = sp.load_return(rpath, RETURN_ENUMS)
    ret = json.loads(line) if line else {}
    if not (ret.get("status") == "ok" and ret.get("kind") == "recheck" and ret.get("twin_pairs") == _verdict_lines(vpath)
            and isinstance(ret.get("pairs_checked"), int) and ret["pairs_checked"] >= required):
        raise Stop(EXIT_REFUSED, "recheck_invalid")
    if min(vpath.stat().st_mtime, rpath.stat().st_mtime) < screen.stat().st_mtime:
        raise Stop(EXIT_REFUSED, "recheck_stale")
    twin_pairs = parse_verdict(vpath)

    # 4. twins: before the sealed freeze, a twin with development demotes the WHOLE group (approvals kept) and a twin
    #    inside sealed drops the later group. After it, nothing sealed ever moves: either becomes an exclusion
    #    (naming its members). Groups are built from the frozen file once there is one.
    #    Everything up to step 8 is computed in memory: a refusal leaves no side effect.
    groups = sealed_group_index(ledger, sealed_side)
    released = {fid for p in payloads.values() for fid in p["assignment"]}
    all_groups = sealed_group_index(ledger, released)  # every released family, development included
    demote: Set[str] = set()
    exclude: Set[str] = set()
    leave_or_exclude = exclude if sealed_frozen else demote
    for a, b in twin_pairs:
        sa, sb, da, db = a in sealed_side, b in sealed_side, a in dev_side, b in dev_side
        if (sa and db) or (sb and da):
            fid = a if sa else b
            leave_or_exclude.add(group_id_of(groups, fid) or fid)
        elif sa and sb and (group_id_of(groups, a) or a) != (group_id_of(groups, b) or b):
            leave_or_exclude.add(group_id_of(groups, max(a, b)) or max(a, b))
    leaving = {m for gid in demote for m in groups.get(gid, [gid])} & set(sealed_side)
    sealed_after = {f: x for f, x in sealed_side.items() if f not in leaving}
    fams: Dict[str, Fam] = dict(sealed_after) if split == "sealed" else {f: x for f, x in dev_side.items() if x.split == split}

    # 5. approvals bound by row hash
    approved = load_approvals(ctx, kind)["approved"]
    missing = sum(
        1 for fam in fams.values()
        if any(needs_approval(kind, r) and approved.get(str(r["id"]), {}).get("row_hash") != eval_schema.row_hash(r) for r in fam.rows)
    )
    if missing:
        raise Stop(EXIT_REFUSED, "approval_missing", families=missing)

    # 6. 2-3 phrasings per family (a breach is the D64 stop)
    short = sum(1 for fam in fams.values() if not 2 <= len(fam.rows) <= 3)
    if short:
        raise Stop(EXIT_FREEZE_INFEASIBLE, "phrasing_breach", families=short)

    # 6b. the development splits' freeze-time minimums (nothing is written before this)
    minimum = {"tuning": int(prereg["quota"]["tuning_min"]), "realistic": int(prereg["quota"]["realistic_min"])}.get(split)
    if minimum is not None and len(fams) < minimum:
        raise Stop(EXIT_FREEZE_INFEASIBLE, "freeze_infeasible", split=split, families=len(fams), minimum=minimum)

    # 7. band, strata, min_share, group invariant, floor; a sealed band breach is repaired by seeded group demotions
    def placement(sealed_ids: Iterable[str]) -> Dict[str, str]:
        place = {f: "sealed" for f in sealed_ids}
        place.update({f: str(x.split) for f, x in dev_side.items() if x.split})
        return place

    excluded = excluded_group_ids(ledger, groups) | exclude
    violated = evaluate_split(fams, split, prereg, placement(sealed_after), groups, excluded, all_groups)
    repaired: List[str] = []
    if split == "sealed" and "band" in violated:
        by_group: Dict[str, List[str]] = {}
        for fid in fams:
            by_group.setdefault(group_id_of(groups, fid) or fid, []).append(fid)
        units = [
            (gid, len(m), sum(1 for f in m if fams[f].scope == "refuse"),
             0 if gid in excluded else sum(1 for f in m if fams[f].scope != "refuse"))
            for gid, m in sorted(by_group.items())
        ]
        chosen = repair_band(
            units, int(prereg["quota"]["floor_sealed"]), int(prereg["seeds"]["repair"]),
            floor_answerable=int(prereg["quota"]["floor_answerable"]),
        )
        if chosen is not None:
            repaired = chosen
            leaving |= {f for gid in chosen for f in by_group[gid]}
            fams = {f: x for f, x in fams.items() if f not in leaving}
            sealed_after = fams
            violated = evaluate_split(fams, split, prereg, placement(sealed_after), groups, excluded, all_groups)
    if violated:
        for name in violated:
            ctx.printer.emit("freeze_constraint", constraint=name)
        raise Stop(EXIT_FREEZE_INFEASIBLE, "freeze_infeasible", families=len(fams))

    # 8. commit. The whole transaction is decided HERE and journalled; the steps then run in finish_freeze.
    rows = [r for f in fams.values() for r in f.rows]
    data = dump_rows(rows, mark=split == "sealed")
    set_sha = hashlib.sha256(data).hexdigest()
    answerable = sum(1 for fid, f in fams.items() if f.scope != "refuse" and (group_id_of(groups, fid) or fid) not in excluded)
    totals = {
        "families": len(fams), "rows": len(rows), "refuse_families": sum(1 for f in fams.values() if f.scope == "refuse"),
        "eligible_answerable": answerable, "groups": len({group_id_of(groups, f) or f for f in fams}),
    }
    twins_tx: Optional[Dict[str, Any]] = None
    twins_sha: Optional[str] = None
    if split == "sealed":
        record = {
            "version": 1, "rounds": {str(r): {"payload": p["twins_payload"], "sha256": p["twins_sha256"]} for r, p in sorted(payloads.items())},
            "recheck_verdict_sha256": sha_file(vpath), "recheck_twin_pairs": [list(p) for p in twin_pairs],
            "demoted_groups": sorted(demote), "repaired_groups": sorted(repaired), "families_sha256": set_sha,
            # the slot gate's group record: its ids are group_ids(frozen families, these edges), as exclude's are
            "edges": release_edges(payloads.values()),
        }
        twins_sha = sha_text(canon(record))
        twins_tx = {"name": twins_payload_name(twins_sha), "record": record}
    already = excluded_group_ids(ledger, groups)
    tx = {
        "version": 1, "split": split, "set_sha256": set_sha, "totals": totals, "twins": twins_tx, "twins_sha256": twins_sha,
        "leaving": sorted(leaving),
        "exclude": [{"group": g, "members": sorted(groups.get(g, [g]))} for g in sorted(exclude - already)],
        "emit": {"demoted_groups": len(demote), "repaired_groups": len(repaired), "excluded_groups": len(exclude)},
        "prereg_sha256": prereg_sha,
    }
    if split == "sealed":  # source split of the frozen families, counts only
        n_tutorial = sum(1 for f in fams.values() if f.source == "tutorial")
        tx["emit"].update(tutorial=n_tutorial, drafted=len(fams) - n_tutorial)
    write_atomic(freeze_data_path(ctx, split), data)
    write_json(freeze_journal_path(ctx, split), tx)
    _freeze_crash("after_journal")
    emit_freeze(ctx, finish_freeze(ctx, split))
    return EXIT_OK


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def mangle_path(path: Path) -> str:
    """Claude Code's project-directory name for a path: every non-alphanumeric character becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def _files_under(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()) if root.is_dir() else []


def _history_blobs(repo: Path) -> List[Tuple[str, str]]:
    """(blob sha, path) for every blob added or modified by any commit reachable from any ref."""
    found: List[Tuple[str, str]] = []
    for commit in sl._git(repo, "rev-list", "--all").decode().split():
        raw = sl._git(repo, "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--no-renames", "-z", commit)
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
            if len(fields) >= 5 and fields[4][0] in "AMT" and set(fields[3]) != {"0"} and fields[1] != "160000":
                found.append((fields[3], path))
    return found


def staging_root_of(ctx: Ctx) -> Path:
    try:
        return ctx.staging_root if ctx.staging_root is not None else sp.resolve_staging_root()
    except sp.RootRefused:
        return sp.default_staging_root()


def worktree_paths(repo: Path) -> List[Path]:
    """Every worktree of the repository (``git worktree list``), the main one included; empty if git cannot say."""
    try:
        raw = sl._git(repo, "worktree", "list", "--porcelain").decode("utf-8", errors="replace")
    except (sl.ScanRefusal, OSError):
        return []
    return [Path(line[len("worktree "):]) for line in raw.splitlines() if line.startswith("worktree ")]


def project_dir_markers(ctx: Ctx) -> Tuple[List[str], str]:
    """``(names a project directory CONTAINS, a prefix it STARTS with)`` for the sessions that may hold sealed text.

    A session's directory name is its working directory with every non-alphanumeric character turned into ``-``.
    The repository's and every worktree's mangled path can sit anywhere in the name (a scratchpad session is
    ``-private-tmp-...-<mangled repo>-<id>-scratchpad``); the SP staging root's mangled path starts the name of a
    session run from a staging directory.
    """
    contains = sorted({mangle_path(ctx.repo), *(mangle_path(p) for p in worktree_paths(ctx.repo))})
    return contains, mangle_path(staging_root_of(ctx))


def settle_staging(ctx: Ctx) -> None:
    """Make SP staging scannable: no live dispatch or sweep, stale entries swept, every half-done move replayed first.

    The lock is the launcher's own (``sp_launch``): held means a dispatch or sweep is running, so files are in
    flux and the audit refuses (``staging_live``). Holding it proves no dispatch is live, so every staging
    directory left is a dead one, and the launcher's own sweep (``sp_launch.sweep_stale``) removes them: the inputs a
    dead dispatch copied in would otherwise read as an exposure of the very text they were staged from. The sweep
    replays pending moves first (never deletes before the destination is complete), so no output is half in, half out.
    """
    root = staging_root_of(ctx)
    if not root.is_dir():
        return
    try:
        with sp._exclusive_lock(root):
            sp.sweep_stale(root)
    except sp.LaunchError:
        raise Stop(EXIT_AUDIT_REFUSED, "staging_live") from None
    except (sp.MoveError, OSError):
        raise Stop(EXIT_AUDIT_REFUSED, "move_recovery_failed") from None


def audit_targets(ctx: Ctx) -> Dict[str, List[Path]]:
    """The a6 target list as files per class: transcripts and memory, staging, logs, research, work, runs, dispatch logs."""
    root = ctx.projects_root if ctx.projects_root is not None else Path.home() / ".claude" / "projects"
    contains, staging_prefix = project_dir_markers(ctx)
    targets: Dict[str, List[Path]] = {c: [] for c in TARGET_CLASSES}
    if root.is_dir():
        mine = sorted(
            d for d in root.iterdir()
            if d.is_dir() and (d.name.startswith(staging_prefix) or any(marker in d.name for marker in contains))
        )
        for directory in mine:
            for path in _files_under(directory):
                is_memory = "memory" in path.relative_to(directory).parts[:-1]
                targets["memory" if is_memory else "transcript"].append(path)
    targets["staging"] = _files_under(staging_root_of(ctx))
    targets["log"] = _files_under(ctx.repo / "logs")
    targets["research"] = _files_under(ctx.repo / "data" / "research")
    targets["work"] = _files_under(ctx.dev_work)
    targets["run"] = _files_under(ctx.private / "runs")
    targets["dispatch"] = [p for p in (ctx.private / "sp_dispatch.jsonl", ctx.private / "sp_probe.jsonl") if p.is_file()]
    return targets


def cmd_audit(ctx: Ctx) -> int:
    """S-needle scan over the a6 target list; counts per target class and each hit's family id."""
    try:
        sources = sl.collect_sources()
        if any(src.sealed for src in sources):
            # The handbook text is exempt from the S-needles. With the dump absent that exemption would silently
            # shrink to nothing and every handbook phrase in a transcript would read as a leak: say so instead.
            dump = Path(ctx.handbook_dump) if ctx.handbook_dump is not None else sl._repo_base() / sl.HANDBOOK_DUMP
            if not (dump.is_file() and os.access(dump, os.R_OK)):
                raise Stop(EXIT_AUDIT_REFUSED, "dump_missing")
        index = sl.build_needles(sources, handbook_dump=ctx.handbook_dump)
    except sl.ScanRefusal:
        raise Stop(EXIT_USAGE, "scan_source_invalid") from None
    s_needles = len(index.s_whole) + len(index.s_windows) + len(index.s_short)
    hits: Dict[str, Dict[str, int]] = {c: {} for c in TARGET_CLASSES}
    scanned: Dict[str, int] = {c: 0 for c in TARGET_CLASSES}

    def scan(klass: str, text: str) -> None:
        scanned[klass] += 1
        for hit in sl.scan_text(text, klass, index):
            if hit.family is not None:
                hits[klass][hit.family] = hits[klass].get(hit.family, 0) + 1

    if s_needles:
        settle_staging(ctx)
        for klass, paths in audit_targets(ctx).items():
            for path in paths:
                try:
                    scan(klass, path.read_bytes().decode("utf-8", errors="replace"))
                except OSError:
                    continue
        if (ctx.repo / ".git").exists():
            head, history = sl._head_blobs(ctx.repo), _history_blobs(ctx.repo)
            blobs = sl._cat_blobs(ctx.repo, sorted({s for s, _ in head} | {s for s, _ in history}))
            for klass, listing in (("tree", head), ("history", history)):
                for sha in sorted({s for s, _ in listing}):
                    scan(klass, blobs.get(sha, b"").decode("utf-8", errors="replace"))
            # Messages of every commit AND annotated tag, read object by object (the merge gate's own walk):
            # `git log --format=%B` shows no tag message, and splitting one stream on a delimiter byte can hide a needle.
            commits = sl._git(ctx.repo, "rev-list", "--all").decode().split()
            tag_objs = sl._git(ctx.repo, "for-each-ref", "refs/tags", "--format=%(objectname)").decode().split()
            objects = sl._cat_blobs(ctx.repo, sorted(set(commits) | set(tag_objs)))
            for sha in [*commits, *tag_objs]:
                scanned["history"] += 1
                for hit in sl._scan_message(objects.get(sha, b""), "history", index):
                    if hit.family is not None:
                        hits["history"][hit.family] = hits["history"].get(hit.family, 0) + 1
    total = 0
    for klass in TARGET_CLASSES:
        n = sum(hits[klass].values())
        total += n
        ctx.printer.emit("audit_target", target=klass, files=scanned[klass], hits=n)
    for klass in TARGET_CLASSES:
        for family, count in sorted(hits[klass].items()):
            ctx.printer.emit("audit_hit", target=klass, family=family, count=count)
    ctx.printer.emit("audit", status="hits" if total else "clean", s_needles=s_needles, hits=total)
    return EXIT_HIT if total else EXIT_OK


# ---------------------------------------------------------------------------
# power, prereg-check, return
# ---------------------------------------------------------------------------
def cmd_power(ctx: Ctx, group_sizes: Optional[str], from_freeze: bool, discordance: Sequence[float],
              effect: Optional[float], alpha: Optional[float], draws: Optional[int], seed: Optional[int]) -> int:
    """Power and MDE of the cluster sign-flip test under independence and full within-group correlation.

    Group sizes come from ``--group-sizes`` or from the frozen sealed set (eligible answerable families,
    exclusions dropped, grouped by released group). The singleton reference is ``eval_stats.power`` / ``mde``.
    """
    prereg = read_json(ctx.prereg_file) if ctx.prereg_file.is_file() else {}
    pw = prereg.get("power", {}) if isinstance(prereg, dict) else {}
    alphas = [alpha] if alpha is not None else list(pw.get("alphas", []))
    effect_v = effect if effect is not None else (prereg.get("mwe") if isinstance(prereg, dict) else None)
    draws_v = draws if draws is not None else pw.get("draws")
    seed_v = seed if seed is not None else pw.get("seed")
    discs = list(discordance) or list(pw.get("discordance", []))
    if not alphas or None in (effect_v, draws_v, seed_v) or not discs:
        raise Stop(EXIT_USAGE, "usage")
    if (group_sizes is None) == (not from_freeze):
        raise Stop(EXIT_USAGE, "usage")
    if group_sizes is not None:
        try:
            sizes = [int(x) for x in group_sizes.split(",")]
        except ValueError:
            raise Stop(EXIT_USAGE, "usage") from None
        if not sizes or min(sizes) < 1:
            raise Stop(EXIT_USAGE, "usage")
    else:
        ledger = ctx.ledger()
        fams = group_families(load_sealed_rows(ctx, frozen_path(ctx, "sealed")))
        groups = sealed_group_index(ledger, fams)
        excluded = excluded_group_ids(ledger, groups)
        counts: Dict[str, int] = {}
        for fid, fam in fams.items():
            gid = group_id_of(groups, fid) or fid
            if fam.scope != "refuse" and gid not in excluded:
                counts[gid] = counts.get(gid, 0) + 1
        sizes = sorted(counts.values())
    n = sum(sizes)
    for alpha_v in alphas:
        for disc in discs:
            eff = min(float(effect_v), float(disc))
            for model in MODELS:
                mde = mc_mde_hundredths(sizes, disc, float(alpha_v), int(draws_v), int(seed_v), model)
                ctx.printer.emit(
                    "power", model=model, families=n, groups=len(sizes), alpha_ppm=round(1e6 * float(alpha_v)),
                    discordance_pm=round(1000 * disc), effect_pm=round(1000 * eff),
                    power_pm=mc_power_pm(sizes, disc, eff, float(alpha_v), int(draws_v), int(seed_v), model),
                    mde_hundredths=mde if mde is not None else "none", draws=int(draws_v),
                )
            ref = eval_stats.mde(n, disc, float(alpha_v))
            ctx.printer.emit(
                "power_reference", model="independent", families=n, alpha_ppm=round(1e6 * float(alpha_v)),
                discordance_pm=round(1000 * disc),
                power_pm=round(1000 * eval_stats.power(n, disc, eff, float(alpha_v), "directional")),
                mde_hundredths=round(100 * ref) if ref is not None else "none",
            )
    return EXIT_OK


def cmd_prereg_check(ctx: Ctx, committed: bool) -> int:
    """Validate the prereg, recompute its quota formulas and power table, pin its quota numbers and minimum shares
    (:data:`PINNED_VALUES`) and check its computed values (:func:`computed_errors`); ``--committed`` also needs it
    committed unmodified."""
    if committed:
        prereg_commit(ctx)
    doc = read_json(ctx.prereg_file, "prereg_invalid") if ctx.prereg_file.is_file() else None
    if doc is None:
        raise Stop(EXIT_USAGE, "prereg_missing")
    errors = [*validate_prereg(doc), *pinned_errors(doc)]
    for path, why in errors:
        ctx.printer.emit("prereg_error", field=path if _FIELD_PATH.fullmatch(path) else "other", why=why)
    if errors:
        raise Stop(EXIT_FAIL, "prereg_invalid", errors=len(errors))
    ctx.printer.emit(
        "prereg_check", status="ok", rounds=len(doc["rounds"]), quota_examples=len(doc["quota"].get("examples", [])),
        power_cells=len(doc["power"].get("table", [])), family_cells=len(doc["power"].get("family_table", [])),
        committed=1 if committed else 0,
    )
    return EXIT_OK


def controls_file(ctx: Ctx) -> Path:
    """``eval/controls_v1.json`` of the repo: the committed critical-controls list."""
    return ctx.repo / "eval" / "controls_v1.json"


def cmd_declare_batch(ctx: Ctx, candidates: Sequence[str]) -> int:
    """Declare the confirmatory batch once: 1-4 candidates in slot order, each ``<config hash>:replay``,
    ``<config hash>:candidate:<rewrite config hash>`` or ``<config hash>:abandoned``.

    ``config hash`` is the candidate's PIPELINE configuration hash (16B's identity check); a ``candidate`` entry also
    names its ``rewrite config hash`` (the rewrite model, prompt and caps: what the slot gate compares with the code's
    ``rewrite_identity()`` and the run's ``--rewrite-candidate``). An ``abandoned`` entry names a configuration with
    recorded development draws but fewer than the prereg's ``draws_r``: it counts toward Holm's K = 4 and the slot gate
    refuses to run it (a recorded candidate cannot be left out of the batch, nor can its draws be abandoned without
    counting).

    Refused while the confirmatory embargo holds (``embargoed``), before the sealed freeze, under any prereg but the
    epoch's registered one (committed, unmodified, pushed), and before the controls are recorded in the ledger and
    match ``eval/controls_v1.json`` (``controls_missing``). It also needs the recorded development draws to cover the
    batch (``draws_uncovered``): every ``replay`` or ``candidate`` entry's ``config hash`` has at least the prereg's
    ``draws_r`` draws recorded (``pipeline eval --record-draw``), every ``abandoned`` entry's has at least one and fewer, and
    every recorded configuration other than the incumbent's is in the batch (all of them count toward Holm's K). Last, the
    entries must be bound to their draws (``draws_mismatch``, r7): the counted draws of every runnable entry were scored by
    the scorer identity the incumbent's counted draws carry; a ``replay`` entry's counted draws are replay draws of the
    incumbent's same-index draws; a ``candidate`` entry's are build draws under its own rewrite identity, which is not the
    incumbent's.
    """
    from src import sealed_ledger

    if sealed_ledger.CONFIRMATORY_EMBARGO:
        raise Stop(EXIT_REFUSED, "embargoed")
    entries: List[Dict[str, str]] = []
    for text in candidates:
        parts = text.split(":")
        config_hash, mode = parts[0], parts[1] if len(parts) > 1 else ""
        if not _SHA256.fullmatch(config_hash) or mode not in sealed_ledger.BATCH_ENTRY_EXPANSIONS:
            raise Stop(EXIT_REFUSED, "batch_invalid")
        entry = {"config_hash": config_hash, "expansion": mode}
        if mode == "candidate":
            if len(parts) != 3 or not _SHA256.fullmatch(parts[2]):
                raise Stop(EXIT_REFUSED, "batch_invalid")
            entry["rewrite_config_hash"] = parts[2]
        elif len(parts) != 2:
            raise Stop(EXIT_REFUSED, "batch_invalid")
        entries.append(entry)
    ledger = ctx.ledger()
    if ledger.freeze_record("sealed") is None:
        raise Stop(EXIT_REFUSED, "freeze_missing")
    prereg = load_prereg(ctx)
    bind_epoch_prereg(ctx, prereg, ledger)
    controls = ledger.controls_record()
    path = controls_file(ctx)
    if controls is None or not path.is_file() or sha_file(path) != controls["controls_sha256"]:
        raise Stop(EXIT_REFUSED, "controls_missing")
    if ledger.batch_declaration() is not None:
        raise Stop(EXIT_REFUSED, "batch_declared")
    try:
        digest = ledger.record_batch_declaration(entries, draws_required=prereg["draws_r"])
    except ValueError:
        raise Stop(EXIT_REFUSED, "batch_invalid") from None
    except DrawCoverageRefused:
        raise Stop(EXIT_REFUSED, "draws_uncovered") from None
    except DrawBindingRefused:
        raise Stop(EXIT_REFUSED, "draws_mismatch") from None
    except ProtocolError:
        raise Stop(EXIT_REFUSED, "batch_declared") from None
    ctx.printer.emit("batch_declared", candidates=len(entries), sha256=digest)
    return EXIT_OK


def cmd_draws(ctx: Ctx) -> int:
    """Read-only: the recorded development draws and the logged unrecorded runs, per pipeline configuration.

    Prints one ``draw_config config=<hash|none> recorded=<n> unrecorded=<n>`` line per configuration (a configuration
    only ever run unrecorded counts ``recorded=0``) and a ``draws configs=<n> recorded=<n> unrecorded=<n>`` summary. Draws are
    recorded only by ``pipeline eval --record-draw``, inside the run (there is no command that records one afterwards), and a
    ``pipeline eval`` run over a frozen tuning or realistic version that was not a ``--record-draw`` run is logged as
    unrecorded before it prints; neither the unrecorded runs nor any draw outside the first ``draws_r`` count.
    """
    ledger = ctx.ledger()
    recorded = {config: len(ledger.dev_draws(config)) for config in ledger.dev_draw_configs()}
    unrecorded = ledger.dev_unrecorded_counts()
    for config in [*recorded, *(c for c in unrecorded if c not in recorded)]:
        ctx.printer.emit("draw_config", config=config, recorded=recorded.get(config, 0), unrecorded=unrecorded.get(config, 0))
    ctx.printer.emit(
        "draws", configs=len(set(recorded) | set(unrecorded)), recorded=sum(recorded.values()), unrecorded=sum(unrecorded.values()),
    )
    return EXIT_OK


def cmd_epoch(ctx: Ctx) -> int:
    """Read-only: the epoch's registered prereg from the ledger, and whether the working prereg file is it.

    Prints ``epoch prereg_sha256=<sha256 or none> records=<ledger records> matches=<yes|no>`` (counts and a digest
    only). ``matches`` is ``yes`` exactly when a prereg is registered and the working prereg file hashes to it;
    ``no`` when none is registered yet, the file differs, or the file is absent. Appends no record.
    """
    ledger = ctx.ledger()
    registered = ledger.epoch_prereg_sha256()
    working = sha_file(ctx.prereg_file) if ctx.prereg_file.is_file() else None
    ctx.printer.emit(
        "epoch", prereg_sha256=registered if registered is not None else "none", records=len(ledger.records()),
        matches="yes" if registered is not None and registered == working else "no",
    )
    return EXIT_OK


def cmd_return(ctx: Ctx, path: str, enums: Sequence[str]) -> int:
    """An SP return file's canonical line if every key and value is an id, hash, count or enum; else ``invalid``."""
    try:
        line = sp.load_return(path, [*RETURN_ENUMS, *enums])
    except ValueError:
        line = None
    if line is None:
        ctx.printer.raw_validated("invalid")
        return EXIT_FAIL
    ctx.printer.raw_validated(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """argparse that never echoes the offending argument (it could be text)."""

    def error(self, message: str) -> Any:  # noqa: ARG002 - message deliberately dropped
        raise Stop(EXIT_USAGE, "usage")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="sealed_tool.py", description="Sealed tool (D72): prints only counts, sha256s and enums.")
    for opt in ("private-root", "repo", "ledger", "mirror", "inventory", "prereg", "handbook-dump", "claude-projects", "staging-root"):
        ap.add_argument(f"--{opt}", default=None, metavar="PATH")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def add(name: str, *flags: Tuple[str, Dict[str, Any]]) -> None:
        p = sub.add_parser(name)
        for flag, kw in flags:
            p.add_argument(flag, **kw)

    add("ids", ("--count", {"type": int, "required": True}), ("--out", {"default": None}))
    add("digest", ("path", {}))
    add("count-migrated", ("--golden", {"required": True}), ("--realistic", {"required": True}), ("--pairing", {"required": True}),
        ("--tutorial", {"required": True}), ("--quota-tuning", {"type": int, "default": None}), ("--quota-realistic", {"type": int, "default": None}),
        ("--drafts", {"type": int, "default": None}))
    add("inputs-check", ("--pairing", {"default": None}), ("--conflicts", {"default": None}), ("--golden-ids", {"default": None}),
        ("--realistic-ids", {"default": None}))
    add("intake", ("--dispatch", {"required": True}))
    add("twins", ("--round", {"type": int, "required": True}), ("--verdict", {"required": True}), ("--dev-verdict", {"default": None}),
        ("--tutorials", {"default": None}), ("--extra-verdict", {"action": "append", "default": [], "dest": "extra_verdicts"}))
    add("split", ("--round", {"type": int, "required": True}), ("--diagnose", {"action": "store_true"}))
    add("paraphrase-apply", ("--dispatch", {"required": True}))
    add("sheet", ("--kind", {"choices": KINDS, "required": True}), ("--round", {"type": int, "default": None}), ("--name", {"default": None}),
        ("--all", {"action": "store_true"}))
    add("apply", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"default": None}), ("--confirm", {"default": None}))
    add("freeze", ("--split", {"required": True}), ("--recheck-verdict", {"default": None}), ("--recheck-return", {"default": None}))
    add("escalate", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"required": True}), ("--name", {"default": None}))
    add("calibrate", ("--kind", {"choices": KINDS, "required": True}))
    add("disagree", ("--kind", {"choices": KINDS, "required": True}), ("--sheet", {"required": True}), ("--calibration", {"action": "store_true"}))
    add("exclude", ("--family", {"action": "append", "default": None}), ("--group", {"default": None}), ("--reason", {"required": True}))
    add("audit")
    add("power", ("--group-sizes", {"default": None}), ("--from-freeze", {"action": "store_true"}),
        ("--discordance", {"type": float, "action": "append", "default": []}), ("--effect", {"type": float, "default": None}),
        ("--alpha", {"type": float, "default": None}), ("--draws", {"type": int, "default": None}), ("--seed", {"type": int, "default": None}))
    add("prereg-check", ("--committed", {"action": "store_true"}))
    add("declare-batch", ("--candidate", {"action": "append", "default": [], "dest": "candidates"}))
    add("draws")
    add("epoch")
    add("return", ("path", {}), ("--enum", {"action": "append", "default": [], "dest": "enums"}))
    return ap


def dispatch(ctx: Ctx, a: argparse.Namespace) -> int:
    """Run the command; a state-changing one holds ``sealed/work/.lock`` (non-blocking) for its whole run."""
    if a.command in STATE_COMMANDS:
        with state_lock(ctx):
            return _run_command(ctx, a)
    return _run_command(ctx, a)


def _run_command(ctx: Ctx, a: argparse.Namespace) -> int:
    c = a.command
    if c == "ids":
        return cmd_ids(ctx, a.count, a.out)
    if c == "digest":
        return cmd_digest(ctx, a.path)
    if c == "count-migrated":
        return cmd_count_migrated(ctx, a.golden, a.realistic, a.pairing, a.tutorial, a.quota_tuning, a.quota_realistic, a.drafts)
    if c == "inputs-check":
        return cmd_inputs_check(ctx, a.pairing, a.conflicts, a.golden_ids, a.realistic_ids)
    if c == "intake":
        return cmd_intake(ctx, a.dispatch)
    if c == "twins":
        return cmd_twins(ctx, a.round, a.verdict, a.dev_verdict, a.tutorials, a.extra_verdicts)
    if c == "split":
        return cmd_split(ctx, a.round, a.diagnose)
    if c == "paraphrase-apply":
        return cmd_paraphrase_apply(ctx, a.dispatch)
    if c == "sheet":
        return cmd_sheet(ctx, a.kind, a.round, a.name, a.all)
    if c == "apply":
        return cmd_apply(ctx, a.kind, a.sheet, a.confirm)
    if c == "freeze":
        return cmd_freeze(ctx, a.split, a.recheck_verdict, a.recheck_return)
    if c == "escalate":
        return cmd_escalate(ctx, a.kind, a.sheet, a.name)
    if c == "calibrate":
        return cmd_calibrate(ctx, a.kind)
    if c == "disagree":
        return cmd_disagree(ctx, a.kind, a.sheet, a.calibration)
    if c == "exclude":
        return cmd_exclude(ctx, a.family, a.group, a.reason)
    if c == "audit":
        return cmd_audit(ctx)
    if c == "power":
        return cmd_power(ctx, a.group_sizes, a.from_freeze, a.discordance, a.effect, a.alpha, a.draws, a.seed)
    if c == "prereg-check":
        return cmd_prereg_check(ctx, a.committed)
    if c == "declare-batch":
        return cmd_declare_batch(ctx, a.candidates)
    if c == "draws":
        return cmd_draws(ctx)
    if c == "epoch":
        return cmd_epoch(ctx)
    return cmd_return(ctx, a.path, a.enums)


LEDGER_ERRORS = (
    (LedgerRefused, "ledger_refused", EXIT_LEDGER_STOP),
    (MirrorTampered, "mirror_tampered", EXIT_LEDGER_STOP),
    (EpochStop, "epoch_stop", EXIT_LEDGER_STOP),
    (LedgerCorrupt, "ledger_corrupt", EXIT_LEDGER_STOP),
    (ProtocolError, "ledger_protocol", EXIT_REFUSED),
)


def main(argv: Optional[Sequence[str]] = None, *, out: Any = None, err: Any = None) -> int:
    """CLI entry point; returns the exit code. Nothing but the allowlisted printer writes output."""
    printer = Printer(out, err)
    try:
        args = build_parser().parse_args(argv)
        ctx = default_ctx(
            printer, private=args.private_root, repo=args.repo, ledger_path=args.ledger, mirror=args.mirror,
            inventory_path=args.inventory, prereg_path=args.prereg, handbook_dump=args.handbook_dump,
            projects_root=args.claude_projects, staging_root=args.staging_root,
        )
        return dispatch(ctx, args)
    except Stop as stop:
        printer.error(stop.reason, **stop.fields)
        return stop.code
    except eval_schema.SchemaError as exc:
        for line, fld, _reason in exc.errors[:50]:
            printer.emit("schema_error", line=min(line, MAX_PRINT_INT), field=fld if _FIELD_PATH.fullmatch(fld) else "other")
        printer.error("inventory_invalid" if isinstance(exc, eval_schema.InventoryError) else "schema_invalid", errors=len(exc.errors))
        return EXIT_USAGE
    except SealedLedgerError as exc:
        for kind, reason, code in LEDGER_ERRORS:
            if isinstance(exc, kind):
                printer.error(reason)
                return code
        printer.error("ledger_corrupt")
        return EXIT_LEDGER_STOP
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001 - never show exception text (it could carry a path or a row)
        printer.err.write("error reason=internal\n")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
