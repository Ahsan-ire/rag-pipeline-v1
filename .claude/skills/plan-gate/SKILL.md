---
name: plan-gate
description: Pre-implementation quality gate — makes the design an artifact, then runs two independent fresh-context critiques (plan-auditor subagent + Codex second-vendor) and reconciles the findings. Nothing gets implemented until this gate says READY.
argument-hint: [path-to-plan-file]
disable-model-invocation: true
allowed-tools: Bash(codex exec --sandbox read-only *), Bash(git log *), Bash(git diff *)
---

You are running the plan gate on **$ARGUMENTS**. The point of this gate is
that the critics never saw the reasoning that produced the plan — do not
brief them on intent, history, or "what we meant". Pointers only.

1. **Make the design an artifact.** The plan must exist as a file before
   review: either a phase section of IMPLEMENTATION_PLAN.md or a
   `docs/designs/NNN-<slug>.md` document (copy `docs/designs/TEMPLATE.md`;
   next NNN in sequence). If the plan so far lives only in conversation,
   write the artifact first and confirm the file path with the user before
   proceeding — the artifact, not the chat, is what implementation will be
   checked against.

2. **First critique — plan-auditor subagent.** Spawn the **plan-auditor**
   subagent (Agent tool). Pass: the plan file path, the acceptance criteria
   location, and nothing else. Its contract is a numbered failure-mode list
   with severities.

3. **Second critique — Codex, second vendor.** Use the `codex` MCP tool if
   present, otherwise Bash:
   `codex exec --sandbox read-only "Adversarially review <plan file> against IMPLEMENTATION_PLAN.md and CLAUDE.md: real bugs, missing steps, spec divergence, weak acceptance criteria. Cite file:line. Do NOT read data/, chroma_db/, or held-out eval files."`
   Pointers only — NEVER paste corpus text, chunk contents, or held-out
   questions into the prompt (CLAUDE.md hard rule). If the codex CLI is not
   installed, record "second-vendor leg SKIPPED — codex unavailable" in the
   gate report; a silently missing leg is a gate failure mode of its own.

4. **Reconcile.** Merge the two finding lists, dedupe, then verify each
   finding against the actual plan/code before accepting it — critics can be
   wrong or out of scope. Classify each: ACCEPT (revise the plan), REBUT
   (state the evidence), or OUT-OF-SCOPE (say where it goes instead, e.g. the
   cut list). Append the table to the design artifact under a `## Review`
   heading — the dispositions are part of the design record.

5. **Verdict.**
   - **READY**: no unresolved blockers; every finding has a disposition.
     Implementation may begin, against the artifact.
   - **REVISE**: numbered revision list. Apply revisions to the artifact
     (with user approval if they change scope), then re-run from step 2 —
     revised plans get fresh critics, not a sympathetic re-read.

Do not begin implementing anything during this gate, including "quick fixes"
the critics suggest. The gate produces a verdict and an updated artifact,
nothing else.
