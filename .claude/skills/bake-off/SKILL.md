---
name: bake-off
description: Design tournament for decisions that are expensive to reverse (chunking strategy, index schema, retrieval architecture). Independent fresh-context candidates, cross-critique, and — wherever the decision is measurable — the tuning eval set picks the winner. No model-as-judge scoreboards, ever.
argument-hint: [one-sentence decision statement]
disable-model-invocation: true
---

You are running a design bake-off for: **$ARGUMENTS**

Use this only for decisions that are genuinely expensive to reverse — a
bake-off costs several agent runs and possibly an eval cycle. For anything
cheaply reversible, decide normally and log it in docs/decisions.md.

## Hard rules for every bake-off

- **The judge is the eval set, not a model.** A model grading model output at
  n=1, with no ground truth and no blinding, is a vibes ledger — this harness
  rejected that pattern deliberately (see docs/harness.md). If no metric can
  decide, the human decides; a model never does.
- **Candidate selection uses the tuning/golden set ONLY** (for this repo:
  `eval/golden_set.jsonl`). The held-out set is never an input to selection —
  using it to pick a design burns it as a headline metric.
- **Candidates get the brief and nothing else.** Independence of context is
  the active ingredient: a candidate that saw another candidate's reasoning,
  or the conversation that framed the problem, is contaminated.

## Procedure

1. **Write the brief.** Create `docs/designs/NNN-bakeoff-<slug>.md` (next NNN;
   start from `docs/designs/TEMPLATE.md`). It contains ONLY: the problem
   statement, the constraints (from CLAUDE.md + relevant decisions.md
   entries, cited by number), the decision criteria, and the exact eval
   command + metric that will judge (or the explicit statement that the
   decision is unmeasurable and the human judges). Confirm the brief with the
   user before spawning anything — the brief is the whole contest.

2. **Generate candidates independently.** Spawn two design agents in
   parallel (Agent tool, general-purpose), each receiving only the brief's
   file path and an output path (`## Candidate A` / `## Candidate B` sections
   of the brief, or sibling files for long designs). Fresh context each — do
   not share this conversation. If a second vendor is available
   (`codex exec --sandbox read-only`), it may author one candidate from the
   same brief — pointers only, never pasted corpus text. Vendor diversity
   adds variety of failure modes; context independence is what actually
   matters.

3. **Cross-critique.** For each candidate, spawn a fresh **plan-auditor**
   pointed at that candidate and the brief's criteria. Append each
   failure-mode list under the candidate it attacks. Verify findings before
   trusting them — critics can be wrong.

4. **Judge.**
   - *Measurable*: implement each candidate minimally, isolated from the
     working tree and from each other (git worktrees; separate persist
     directories for anything indexed — never the real `chroma_db/`). Run the
     brief's exact eval command per candidate. The metric table picks the
     winner. If generation-stage API calls are needed, state the cost and get
     the user's go-ahead first.
   - *Not measurable*: present both candidates plus critiques to the user
     with a recommendation and the reasoning. The user picks.

5. **Record.** Winner, numbers (or the human's stated grounds), and the
   loser-as-rejected-alternative go into a docs/decisions.md entry, linking
   the brief. The losing candidate stays in the design doc — decisions.md
   demands "what was rejected", and the bake-off already wrote it down.

6. **Loser becomes the watchdog.** When the winning design is implemented,
   hand the losing candidate's critique list (and the loser design itself) to
   the phase-gate pressure-tester as extra attack surface. That is the one
   part of the "losing model tears it down at checkpoints" idea worth
   keeping: the loser's design encodes a different set of assumptions, which
   is exactly where the winner's blind spots live.
