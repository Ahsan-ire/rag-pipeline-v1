# 002 — Third-party model lane: grok as implementer, Codex as co-developer

**Status:** draft — `/plan-gate` before the first dispatch
**Date:** 10 Sep 2026
**Decision ledger entry:** docs/decisions.md D59 (to be added when decided)
**Supersedes:** the unlanded "Codex implementer lane" draft of 24 Aug (lost with the
session scratchpad; its content is folded in here)

## Problem

Until now the harness used exactly one third-party model in exactly one shape: Codex CLI
as a read-only adversarial reviewer at the plan gate and the merge gate. Two things are
wrong with that as a standing arrangement.

1. **Throughput.** Every mechanical edit — test scaffolding to a stated contract, doc
   sync from stated values, standalone scripts — is executed by Claude, in the session's
   own context window, at Claude's cost. That is grunt work occupying the scarcest
   resource in the loop.
2. **Review shape, not review substance.** Codex is briefed as a critic *after* the
   design exists. Adversarial review of a finished artifact catches defects; it does not
   catch the framing error made before the artifact was written. Different models miss
   different things in both directions — the value is in the disagreement, and the
   current lane only harvests it at the end.

## Constraints

- **CLAUDE.md hard rule, extended to every third-party vendor:** never send corpus text
  (handbook extracts, chunk contents) or held-out eval questions to a third-party model.
  The corpus is copyrighted and the repo is public. This was written for Codex; it binds
  grok/xAI identically.
- Tests green before any commit; **only Claude commits.** No third-party model runs
  `git commit` or `git push`.
- Fresh-context critique (harness Principle 2) must survive: a model that co-authored a
  design cannot be the fresh-context critic of that same design.
- D48 stands: no per-agent containers, no broker service, no model-graded scoreboard.
  This lane is plain CLI invocations and git worktrees, no infrastructure.
- No new repo dependencies. Both CLIs are already installed on the dev box.

## Decision criteria

Unmeasurable by eval set — this is a workflow decision, judged by the human. The bar:
after one phase run through this lane, (a) no corpus-bearing or secret path was read or
written by a third-party process, verifiable from the sandbox profile and the diff;
(b) every third-party diff was reviewed by Claude and passed the normal gates before
landing; (c) the wall-clock and context cost of mechanical work visibly dropped.

## Design

### Roles

| Actor | Role | Writes to the tree? |
|---|---|---|
| **Claude** | Orchestrator. Writes specs, reviews every third-party diff, runs the gates, commits, owns all user-facing checkpoints (PR, merge STOP). | Yes — and is the only actor that commits. |
| **grok** (xAI CLI, `grok-4.6`) | **Implementer for grunt work** — anything that would otherwise go to a Claude subagent and is mechanical, fully specced and test-verified. | Yes, inside a sandboxed worktree only. |
| **Codex** (OpenAI CLI, GPT-5.6) | **Co-developer** on design and judgment-bearing work, *and* adversarial reviewer at the plan and merge gates. | No — always `--sandbox read-only`. |
| Claude subagents | Still available and still used where a Claude-context implementer or a fresh-context critic is the right tool. | Yes (in-tree, as today). |

**The 10 Sep revision to the previous draft:** the earlier lane split work as
"mechanical → third party, judgment-bearing prose → Claude alone". The second half is
withdrawn. Judgment-bearing work — design decisions, dispositions, ledger entries,
acceptance criteria — is **co-developed with Codex**: Claude drafts or Codex drafts, the
other critiques, and the disagreement is reconciled in the artifact before it lands.
Rationale (user, 10 Sep): different models catch different angles, and a review that only
runs one direction harvests half the value. Claude still holds the pen and the commit.

**Guard rail this creates:** where Codex co-authored a design, it cannot also be that
design's fresh-context critic. The plan gate's second leg then falls to the
plan-auditor plus a Claude subagent briefed with pointers only, and the artifact records
which legs ran. Fresh context, not vendor count, is the active ingredient (Principle 2).

### Task classes for grok

**ALLOWED** — mechanical, fully specified, verifiable by a command:
- test scaffolding to a stated contract;
- doc sync from values stated in the spec;
- standalone scripts with tests (hooks, helpers, report utilities);
- mechanical refactors: renames, extractions, de-duplication with the behaviour pinned by
  existing tests.

**EXCLUDED**:
- design decisions and acceptance criteria;
- eval-set contents, and anything whose spec would need corpus text to be written;
- chunker / retriever / evaluator semantics;
- dependency changes;
- anything touching `docs/decisions.md` (append-only ledger, Claude's pen).

### Containment — verified, not asserted

Two independent layers, plus the prompt clause as a third:

1. **Worktree.** A sibling worktree `../rag-pipeline-grok-wt` (sibling, *not* under
   `.claude/worktrees/` — in-repo worktrees are the mistake cleaned up on 24 Aug).
   `data/`, `*.pdf`, `.env`, `chroma_db*/`, `eval/bakeoff/` are gitignored, so git
   materializes none of them into a fresh worktree: **the workspace physically holds no
   corpus and no keys.** `eval/` *is* tracked, so held-out question files would
   materialize — suppress them with
   `git sparse-checkout set --no-cone '/*' '!/eval/'` at worktree creation.
2. **Kernel sandbox.** grok's `--sandbox <profile>` is enforced by Seatbelt on macOS. A
   custom profile adds a `deny` list enforced for read *and* write, glob-capable.
   Verified 10 Sep 2026 by probe: with `deny = ["**/secret.txt", ...]` the worker
   reported `Permission denied: …/secret.txt` and could not read the file, while writing
   its output file normally in the same run.
3. **Prompt clause.** The do-not-read clause, pasted verbatim from CLAUDE.md, as today.

Proposed profile (`.grok/sandbox.toml`, tracked so the containment is auditable —
**open question 1** below):

```toml
[profiles.ragproj]
extends = "workspace"          # read everywhere, write CWD + temp + ~/.grok
deny = [
  "**/*.pdf", "**/.env", "**/.env.*",     # (.env.example is not matched by these)
  "**/data/**", "**/chroma_db*/**",
  "**/eval/bakeoff/**",
  "**/eval/heldout_set.jsonl", "**/eval/heldout_candidate_review.md",
]
```

This is **stronger** than the containment Codex gets (prompt-level only), which is the
right asymmetry: grok writes, Codex reads.

### Dispatch contract

Spec committed at `docs/tasks/third-party/NNN-<slug>.md` before dispatch:

- **Goal** — one paragraph, no ambiguity.
- **Files in scope** — an allowlist. After the run, `git diff --name-only` must be a
  subset; checked mechanically, not by eye.
- **Out of scope** — named explicitly.
- **Acceptance criteria** — each paired with the command that verifies it.
- **Test command** — the project venv path (a fresh worktree has no `.venv/`; run
  `/Users/malik26/ClaudeCode/rag-pipeline-v1/.venv/bin/python -m pytest tests/ -q`).
- **Constraints** — CLAUDE.md conventions, the do-not-read clause verbatim, and:
  *ambiguity ⇒ stop and return numbered questions, make no edits.*

Verified invocation shape (10 Sep 2026, grok 1.0.13):

```
grok -p "<spec text or 'read docs/tasks/third-party/NNN-<slug>.md and execute it'>" \
     --cwd ../rag-pipeline-grok-wt \
     --sandbox ragproj \
     --permission-mode acceptEdits \
     --max-turns <N> \
     --output-format plain
```

Notes from the probe: `grok inspect` shows the CLI auto-loads the project `CLAUDE.md`,
`.claude/agents/` and `.claude/skills/` through its Claude-compat loader, so a worker
inherits the hard rules without being re-briefed — but the do-not-read clause still goes
in the prompt (belt and braces, and the clause is the one thing that must not depend on a
loader). `grok models` prints "You are not authenticated" even when the agent is
authenticated — a subcommand quirk, not a real auth failure. Workers are literal: the
probe wrote `OK.` when asked for a file "containing exactly OK", so specs must state
exact strings unambiguously.

### Return path

1. grok leaves **uncommitted** changes in the worktree.
2. Claude checks `git diff --name-only` ⊆ files-in-scope allowlist. Out-of-scope file ⇒
   reject the run, do not cherry-pick.
3. Adversarial review of the diff: built-in `code-review` at high effort; the
   pressure-tester with criteria verbatim for contract-bearing work. **Claude is the
   second vendor on grok-authored diffs — the roles invert.**
4. Full suite green in the worktree.
5. Claude commits on `grok/<slug>` with an `Implemented-by: grok-4.6 from
   docs/tasks/third-party/NNN-<slug>.md` trailer, then `git merge --no-ff` into the phase
   branch. Normal gates apply to the result; nothing skips a gate because a third party
   wrote it.

## Rejected alternatives

- **Third-party model commits directly.** Removes the review step that is the entire
  safety argument, and puts an unreviewed diff in public history one `git push` away from
  the corpus rule.
- **In-repo worktree under `.claude/worktrees/`.** Exactly what was removed on 24 Aug:
  two stale worktrees each holding a full `chroma_db/` (i.e. the whole corpus) inside the
  repo directory, covered by no hygiene clause.
- **`--permission-mode bypassPermissions` / `--always-approve` outside a sandbox.** The
  sandbox is what makes auto-approval acceptable; without it, auto-approval is the
  failure mode.
- **Routing work between models by a scoreboard.** Still rejected (D48). Task class is
  decided by the nature of the work — mechanical vs judgment-bearing — not by a ranking.
- **Dropping Codex to reviewer-only now that grok implements.** Discards the
  cross-vendor disagreement on exactly the work where it is most valuable (design), which
  is the opposite of the 10 Sep revision.

## Review

*(To be appended by `/plan-gate`. Note the guard rail: if Codex co-authors this
artifact's revisions, the plan gate's second critique leg must come from a critic that
did not.)*

## Open questions for the plan gate

1. **Sandbox profile location.** `.grok/sandbox.toml` tracked in-repo (auditable,
   portable, but adds a vendor-specific dotfile to a public repo) vs
   `~/.grok/sandbox.toml` (invisible to the repo, so the containment claim is
   unverifiable from the artifact). Draft recommends tracked in-repo.
2. **Ledger reach.** Does a grok-authored change ever justify its own D-entry, or does it
   always land under the phase's entry with the `Implemented-by:` trailer as the record?
3. **Codex co-development mechanics.** Round-trip via `codex exec --sandbox read-only`
   with pointers only, or a single reconciliation pass per artifact? Cap the rounds —
   the plan gate's existing re-gate stopping rule (≥3 fresh rounds, monotonically
   declining severity, only grep-verifiable residue) should apply here too.
4. **Where the `Implemented-by:` trailer is enforced** — convention, or the pre-commit
   guard (Track 2 item E) that is itself still unbuilt.

## Outcome

*(Filled in after the first phase run through this lane.)*
