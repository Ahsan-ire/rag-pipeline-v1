# Phase 15 — `/phase-gate 15` report

**Run:** 24–25 Aug 2026 (assembled 25 Aug; interrupted once by a session limit and
re-run from the top — the pressure-tester and all ten code-review angles completed on
the second pass).
**Branch:** `phase-15-retrieval-foundation` @ `fd6c936`, tree clean, 11 commits ahead of
`main`, **never pushed**.
**Verdict: FAIL — fixable forward.**
**Disposition (user, 10 Sep 2026): fix all six blockers on this branch, then push and
open the PR.** Merge still waits for an explicit "go".

This file exists because gate reports previously lived only in a chat session. The
gate is a step in `.claude/skills/phase-gate/SKILL.md`; this is its record.

---

## Gate steps and evidence

| Step | Result |
|---|---|
| 1. Acceptance criteria extracted verbatim | `IMPLEMENTATION_PLAN.md` L747–790 (Tier-1 / Tier-2 block + the terminal no-swap clause). Winner-conditional criteria marked `MOOT — D57 no-swap`, cited. |
| 2. Full suite | `609 passed` (`.venv/bin/python -m pytest tests/ -q`), green before each of the three close-out commits. |
| 3. pressure-tester (fresh context) | Completed. Notable: **T1.4 MOOT** — the C1 manifest guard was resequenced to land "with a winner" and, since no winner was adopted, never landed. First run reported the criterion as unverifiable because it named a `preflight` symbol that does not exist; the re-run was told to locate the behaviour by `grep -n "clear_store\|sync_documents" tests/test_embedder.py` and treat absence as REFUTED. It is REFUTED — see blocker 1. |
| 4. code-review (high, 10 finder angles) | Completed, strong cross-angle convergence on the manifest write path and on documentation describing guards that do not exist. |
| 5. Git hygiene | `git status --porcelain` clean; every probe for `data/`, `*.pdf`, `.env`, `chroma_db/`, `chroma_db_arm_*/`, `eval/bakeoff/`, `logs/` returned IGNORED. `transformers` exact pin confirmed (D53). |
| 6. decisions.md coverage | D53, D54, D55, D56/D58 (mooted), D57 + addendum present; header reads `Current phase: 15 — retrieval foundation (negative result recorded, closing)`; guard line `**Next: D59**`. **The gate's own disposition is not yet recorded — that is blocker 2.** |

**Tier-2:** no canonical run warranted (no model swap ⇒ nothing to re-measure); the July
`eval/results.md` stands, annotated at `fd6c936` with recomputed set hashes.

---

## Blockers (must land before the PR)

### 1. The manifest is written on the index path without a cross-model assert — *the only code fix*

`src/embedder.py:450 add_documents` and `:539 sync_documents` write and overwrite
`embedding_model.txt` (`EMBEDDING_MODEL_MANIFEST`, `:28`) with no comparison against the
manifest already beside the store. `assert_embedding_model` (`:405`) is called **only**
from read paths — `src/retriever.py:83`, `src/retriever.py:269`, `src/evaluator.py:1583`.

Consequence: re-indexing with a different `EMBEDDING_MODEL` (the D55 seam, which is a
user-facing override) silently rewrites the manifest to match the new model, so the
read-path assert that exists to catch a mixed-model store passes against a store whose
vectors are now from two models. The failure is silent and corrupts retrieval quality
rather than erroring.

Fix: assert model identity against the existing manifest/collection metadata before
writing on the index path; require an explicit reset (`--reset` already exists) to change
models. Tests for: same-model re-index (allowed), cross-model re-index (raises),
fresh-store first write (allowed, manifest created).

### 2. No D59 ledger entry for the gate's disposition

The ledger stops at the D57 addendum. Nothing records that the gate ran, what it found,
what was fixed forward, what was deferred to Phase 16 with what justification, and the
third-party-lane deviation (see `docs/designs/002-third-party-implementer-lane.md`).
Append-only, one entry, header `Next:` guard bumped in the same commit.

### 3. Brief 001 §Outcome — disclosure repairs

`docs/designs/001-bakeoff-embedding-model.md` §Outcome (L429–500):
- the S5 rank line is missing;
- the production-config flip list is not presented separately from the raw-hybrid flip
  list (the two arms of the Tier-1 rule read as one);
- the auditability evidence is referenced rather than shown inline.

Source numbers live in `eval/bakeoff/` (gitignored, corpus-bearing) — this repair is
authored from the local run artifacts, not by a third-party model.

### 4. `IMPLEMENTATION_PLAN.md` contradicts its own Phase 15 outcome

`IMPLEMENTATION_PLAN.md` L790 (Outcome, closed 24 Aug) states **"WS3 guard NOT landed:
no truncation-exception policy was adopted."** The cut list at L836 still reads
**"Phase 15 never-cut: the index-time truncation guard"**. A phase cannot be closed as
on-plan while its never-cut list names something that did not land.

Also at L833: *"Chunk-token provenance disclosure is NOT cuttable"* (plan-gate finding
A4) needs an explicit delivered / deferred disposition rather than silence.

Fix: a disposition note reconciling both against the accept-&-close decision — not a
silent edit of the never-cut list (the ledger convention is that reversals are recorded,
not erased).

### 5. Docstrings describe a truncation guard that does not exist

- `tests/conftest.py:23–33` explains that `ALLOW_CHUNK_TRUNCATION` "downgrades the
  index-time truncation guard from raise to warning" and scrubs the variable.
- `src/embedder.py:43`, `:91`, `:107–114` document `context_window` and a guard that
  "checks chunks against `context_window`".

Neither guard landed (see blocker 4). The env scrub can stay (harmless, and the variable
may return in a later phase), but the prose must say what is true today.

### 6. The inertness canary does not cover the mutation it exists to protect

`src/embedder.py:339` unconditionally sets `client.default_prompt_name = None`. The
inertness canary at `tests/test_embedder.py:760` asserts the seam changes nothing about
the shipped path but does not assert this mutation, so a regression that stops clearing
the prompt name (re-introducing the doc-prompt-on-queries hazard the merge canary at
`:784` documents) would go green.

---

## 7. PR precondition (not a fix)

The branch has never been pushed, so `.github/workflows/ci.yml` has never run against it.
**CI green on the PR is a merge precondition.**

---

## Deferred to Phase 16, with disposition

**8. Instrument vacuous-pass hardening.** `scripts/bakeoff_report.py:644–702`
`compare_prod_ranks` returns a clean result when its `W=0.25|golden|` prefix match finds
nothing — a passing check that checked nothing. Same family: positional dump-order
fragility (`:818`), arm identity taken from the file stem (`:805`), a `None[:70]`
`TypeError` path (`:825`). Deferred because the instruments are not on the production
path and Phase 16 re-enters them for the McNemar work; hardening lands there with tests
that assert the vacuous case fails.

**9. Quality backlog.** Doc-prompt-only spec leaking the document prompt to queries
(`src/embedder.py:269`); module-level `os.chdir(REPO)` in `scripts/w_sweep.py:26` firing
at pytest collection via `tests/test_bakeoff_instruments.py:23`; cwd-relative paths;
duplicated helpers; dead branches; the arm roster not single-sourced; the 3-weight sweep's
efficiency. None affect the negative result or the shipped pipeline.

---

## Re-open condition (carried from the D57 addendum)

The embedding bracket is revisited **only** after the Phase 16 question-set expansion
gives the eval statistical power. The Phase 15 verdict is not "MiniLM is best"; it is
"no candidate cleared the bar on a 35-question tuning set".
