# The harness — how work gets done in this repo, and how to port it

This document is the "spine": the one place that records how the development
workflow itself is built, why each piece exists, what was deliberately
rejected, and how to carry the whole thing into a new project. Project
*content* rules live in CLAUDE.md; this file is about the *workflow*.

## Principles

1. **Design is an artifact, implementation is checked against it.** Plans are
   files (IMPLEMENTATION_PLAN.md phase sections, or docs/designs/NNN-*.md),
   reviewed before code exists, and referenced after. Chat is not a record.
2. **Critics get fresh context.** The active ingredient in adversarial review
   is that the critic never saw the reasoning that produced the work, so it
   cannot be captured by it. Every reviewer in this harness — plan-auditor,
   pressure-tester, Codex — is briefed with pointers and criteria only, never
   with intent or history. A second vendor adds variety of failure modes;
   context independence is what does the real work.
3. **The judge is an eval set, not a model's opinion.** Where a decision is
   measurable, the tuning/golden set picks winners. Where it is not, a human
   does. A model never grades a contest between models.
4. **Every choice leaves a record.** docs/decisions.md is append-only:
   what / why / what was rejected. Reversals are new entries, not edits.
5. **Evidence before claims.** Nothing is "done" until the command ran and
   the output is pasted. Gates enforce this; so does the pressure-tester's
   output contract.

## The pieces

| File | Role |
|---|---|
| `CLAUDE.md` | Project rules loaded every session: commands, hard rules, conventions, context that saves time |
| `IMPLEMENTATION_PLAN.md` | Phase-scoped working spec; one phase per session |
| `docs/decisions.md` | Append-only decision ledger (what/why/rejected) |
| `docs/designs/` | Design artifacts + bake-off briefs (see its README) |
| `docs/work-state.md` | Living handoff: where work stands, next actions in order, open decisions. Updated before a session's context is lost |
| `.claude/agents/plan-auditor.md` | Fresh-context critic for plans, pre-implementation |
| `.claude/agents/pressure-tester.md` | Fresh-context verifier of "done" claims, evidence-only |
| `.claude/skills/plan-gate/` | Pre-implementation gate: artifact → two independent critiques → reconcile → READY/REVISE |
| `.claude/skills/phase-gate/` | End-of-phase gate: full tests, pressure-tester, code review, hygiene, decisions.md check |
| `.claude/skills/bake-off/` | Design tournament for expensive-to-reverse decisions, judged by the tuning eval set |
| `eval/golden_set.jsonl` | Tuning set — the judge for bake-offs and iteration |
| `eval/heldout_set.jsonl` | Headline set — never used for tuning or candidate selection |
| `.github/workflows/ci.yml` | Keyless CI: full suite + offline smoke eval with hard assertions |
| `eval/sets.json`, `src/eval_sets.py` | Eval-set registry and `classify()` → the privacy floor every eval entry point checks (D65) |
| `eval/private/` (gitignored, never committed) | Private and sealed eval material; private runs write only under `runs/<id>/` |
| `scripts/scan_leaks.py` | Needle scanner: `--output` before releasing a private run, `--merge-gate` before a push when private sets exist (D65) |
| `src/spend.py`, `config/api_prices.toml` | Eval-only spend meter; the €40/UTC-week cap is a D64 owner stop (D70) |
| `tests/p16_capture.py` + `tests/test_p16_projection.py` | P0 lock: v5 reports byte-identical to main's capture (the H0 pattern, in-process) |

## The workflow

For a normal phase:

1. Plan the phase (plan mode) → the plan is or becomes an artifact.
2. `/plan-gate <plan file>` — plan-auditor + Codex critique, findings
   reconciled into the artifact. Implement only on READY.
3. Implement on the phase branch, one phase per session, tests green before
   every commit.
4. `/phase-gate <N>` — full suite, pressure-tester versus the acceptance
   criteria verbatim, code review, git hygiene, decisions.md coverage.
5. Codex merge gate on the diff vs main (CLAUDE.md, "Adversarial review");
   findings fixed forward or rebutted in the PR description.

For a decision that is expensive to reverse (chunking strategy, index schema,
retrieval architecture): `/bake-off <decision>` — independent fresh-context
candidates from a shared brief, cross-critique, eval-set verdict on the
tuning set, ledger entry, and the losing design handed to the pressure-tester
as extra attack surface when the winner is implemented.

## Third-party models (revised 10 Sep 2026)

The harness used to run exactly one third-party model in exactly one shape: Codex
as a read-only critic at the plan and merge gates. It now runs two, in two shapes:

- **grok** — implementer for grunt work (mechanical, fully specced, test-verified),
  writing only inside a sandboxed sibling worktree, never committing.
- **Codex** — co-developer on design and judgment-bearing work *as well as* the
  adversarial reviewer at the gates. Still always `--sandbox read-only`.
- **Claude** — orchestrator: writes the specs, reviews every third-party diff, runs
  the gates, and is the only actor that commits.

Claude reviews grok's diffs, which inverts Principle 2's usual direction — that is
the point: the second-vendor leg runs both ways. Where Codex co-authored a design it
cannot also be that design's fresh-context critic; the artifact records which critique
legs actually ran.

The normative source for task classes, containment and the dispatch contract is
`docs/designs/002-third-party-implementer-lane.md` (status: draft — plan-gate before
first dispatch). The corpus ban in CLAUDE.md binds every vendor, not just Codex.

*This section is a summary; the fuller harness update (changelog, re-gate stopping
rule, Tier-1/Tier-2 vocabulary, negative-result phase path) is item 3 of the re-draft
list in `docs/work-state.md`.*

### Changelog
- **10 Oct 2026 — Phase 16A-1 (D65–D70).** Eval privacy is structural rather than
  procedural: classes, a no-default `privacy` keyword with a computed floor, AST
  tests keeping registry/classifier parameters out of production code, a leak
  scanner wired into the pre-push rule, and `eval/private/` in the never-commit
  check, `.gitignore`, the do-not-read clause and `.harness/project.toml`
  (`never_commit`, `restricted_read`, `codex_clause`, `worker_allow`; the owner
  adopts it with `harness-init --refresh`). Live eval calls are metered against
  the D64 cap. The [W] lane ran on Claude subagents in isolated worktrees
  (harness-worker unavailable), each on synthetic fixtures with a file allowlist
  and an `Implemented-by:` trailer; [C] integration and every real-row check stayed
  with the orchestrator.

## Deliberately rejected (do not rebuild these)

- **A model-graded scoreboard routing work between models.** One model
  grading two others at n=1 per task, with no ground truth, no blinding, and
  the judge related to a contestant, is not measurement — and its conclusions
  go stale with every model release. The eval set already is the scoreboard,
  with ground truth. (D48)
- **Per-agent containers, a multi-plane broker service, one repo per
  concern.** For a solo operator this is a second project with no portfolio
  value. Claude Code subagents already give fresh-context isolation;
  git worktrees give filesystem isolation; `--sandbox read-only` gives the
  second vendor read-only access. Zero infrastructure. (D48)
- **Autonomous third-party agents with real-world reach near client data.**
  Prompt-injection surface plus unvetted skill/plugin ecosystems is not a
  risk profile that belongs on a machine holding live conveyancing files —
  that is privilege and GDPR exposure, not workflow optimization. Anything
  in that class stays off machines with client data, full stop. (D48)

## Porting this harness to a new project

The harness is deliberately plain files — no infrastructure to stand up.

1. Copy `.claude/agents/`, `.claude/skills/plan-gate/`,
   `.claude/skills/bake-off/`, `docs/designs/README.md` + `TEMPLATE.md`, and
   this file. `phase-gate` needs its command allowlist and hygiene list
   re-pointed at the new project's test/eval commands before it is useful.
2. Write the new project's CLAUDE.md from the same skeleton: what this is /
   commands / hard rules / conventions / context that saves time. Keep it
   under ~two screens; it is loaded every session.
3. Start `docs/decisions.md` with the same header block (append-only,
   what/why/rejected) on day one — its value is the accumulation.
4. Build the tuning eval set **before** tuning anything, and split off a
   held-out set the moment there is a headline metric to protect. Without
   this, bake-offs degrade into model-as-judge — the exact pattern this
   harness rejects.
5. Wire CI to run keyless/offline from the first week; a gate that needs a
   secret to run is a gate that gets skipped.

What stays project-specific by design: the eval sets and their metrics, the
phase plan, the hygiene lists (what must never be committed), and the
`allowed-tools` lines in each skill. What ports untouched: the two agents,
the gate/bake-off shapes, the designs convention, and the principles above.
