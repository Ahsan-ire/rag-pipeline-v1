---
name: phase-gate
description: End-of-phase quality gate for the RAG pipeline — full tests, pressure-test subagent, code review, git hygiene, decisions.md check. Produces a PASS/FAIL evidence report.
argument-hint: [phase-number]
disable-model-invocation: true
allowed-tools: Bash(python -m pytest *), Bash(python -m src.pipeline *), Bash(python scripts/extraction_qa.py *), Bash(.venv/bin/python *)
---

## Current state

!`git status --short`

## The gate

You are running the quality gate for **Phase $ARGUMENTS** of this repo. Collect evidence first; fix nothing during the gate.

1. Read IMPLEMENTATION_PLAN.md and extract Phase $ARGUMENTS's acceptance criteria **verbatim**. Read the current-phase line at the top of docs/decisions.md.
2. Run the full test suite: `.venv/bin/python -m pytest tests/ -q` (fall back to `python -m pytest tests/ -q` if no venv). The whole suite must pass. Paste the output.
3. Spawn the **pressure-tester** subagent (Agent tool), passing: the phase number, the acceptance criteria verbatim, and a reminder to return the per-criterion table with pasted evidence.
4. Invoke the built-in **code-review** skill (Skill tool) on the current diff at high effort.
5. Git hygiene: confirm `git status --porcelain` shows nothing matching `data/`, `*.pdf`, `.env`, or `chroma_db/` (their presence means .gitignore failed).
6. Confirm docs/decisions.md contains entries covering this phase's design choices. If missing, draft the entry and ask the user before appending.
7. Assemble the gate report:
   - Acceptance criterion table (criterion → PASS/FAIL → evidence)
   - Code-review findings
   - Pressure-tester defects
   - Hygiene result and decisions.md status
   - **GATE: PASS or FAIL**
   - On PASS: print a suggested commit message. On FAIL: a numbered fix list, then wait for user approval before fixing anything.
