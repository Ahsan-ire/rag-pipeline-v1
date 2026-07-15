# CLAUDE.md — legal RAG pipeline

## What this project is
RAG pipeline over an OCR-scanned Irish conveyancing handbook (~800 pages, decimal
paragraph numbering: Chapter 3 → 3.2 → 3.2.1). The product's entire point is
**grounded answers with chapter/paragraph/page citations** and explicit refusal
when the answer is not in the corpus. Portfolio piece for the Claude Corps
Fellowship — two-track: v1 freeze on 11 July (to be tagged `v1.0-baseline` at
merge), v2 remediation target 13 July, fellowship deadline 17 July.

Working spec lives in IMPLEMENTATION_PLAN.md. Design rationale lives in
docs/decisions.md. Current phase is stated at the top of decisions.md.

## Harness
The workflow itself (gates, critics, bake-offs) is documented in
docs/harness.md — read it before changing how work gets done here.
Fresh-context critics live in .claude/agents/ (plan-auditor,
pressure-tester); gates and the design tournament in .claude/skills/
(plan-gate, phase-gate, bake-off). Designs too big for a decisions.md
entry, and all bake-off briefs, are artifacts in docs/designs/ (see its
README). Bake-offs are judged by eval/golden_set.jsonl — never by a
model's opinion, never on the held-out set.

## Commands
- Run tests: `python -m pytest tests/ -q`  (must pass before any commit)
- Index corpus: `python -m src.pipeline index ./data/Conveyancing_Handbook.pdf --type handbook`
- Query: `python -m src.pipeline query "..." --top-k 6`
- Extraction QA: `python scripts/extraction_qa.py ./data/Conveyancing_Handbook.pdf`
- Eval (canonical v2, held-out headline; **makes live API calls**):
  `python -m src.pipeline eval --heldout eval/heldout_set.jsonl --judge`
  — writes the committed `eval/results.md` only on a canonical run (held-out
  set, all 3 modes, refusals+completeness, top_k=6, no errors); else the
  gitignored `eval/results_partial.md`.
- Eval offline / CI (no API key, retrieval ablation only):
  `python -m src.pipeline eval --skip-refusals --skip-completeness` — both
  skips are required to make ZERO generation calls (generation would otherwise
  need `ANTHROPIC_API_KEY`).

## Hard rules
- NEVER commit anything in `data/`, any `*.pdf`, `.env`, or `chroma_db/`.
  The corpus is copyrighted; the repo is public.
- Evidence before claims: run the code/tests and show the output before
  saying something works. "Should work" is not done.
- Every design choice (chunk size, pattern, library, threshold) gets a short
  entry appended to docs/decisions.md: what / why / what was rejected.
- If implementation diverges from the approved plan, STOP and return to plan
  mode — do not improvise silently.
- Scope: one phase of IMPLEMENTATION_PLAN.md per session. Do not start the
  next phase without being asked.
- No new dependencies without stating why in the plan; pin exact versions in
  requirements.txt.

## Adversarial review (Codex)
A second-vendor reviewer (OpenAI Codex CLI, model configured by the user) is
installed: use the `codex` MCP tool when present, otherwise
`codex exec --sandbox read-only "..."` via Bash. ALWAYS pass
`--sandbox read-only` — the default sandbox is workspace-write, i.e. Codex
could otherwise edit this repo; a reviewer must not touch the working tree.
It can read files and run read-only commands (e.g. `git diff`) itself — pass
it *pointers* (file paths, branch names), not pasted content.

- Plan gate: after plan-auditor passes a phase plan, get a Codex critique of
  the plan file; reconcile both sets of findings before implementing.
- Merge gate: before requesting merge of a phase branch, get a Codex review
  of the diff vs main; fix findings forward or rebut them explicitly in the
  PR description.
- Canonical call:
  `codex exec --sandbox read-only "Adversarially review <plan file | the diff vs main> for phase N of IMPLEMENTATION_PLAN.md: real bugs, missing steps, spec divergence, weak tests. Cite file:line. Where a fix is small and mechanical, include a proposed unified diff in the finding (text only — you cannot apply it). Do NOT read data/, chroma_db/, or held-out eval files."`
- Treat Codex findings like pressure-tester findings: verify each against
  the code before acting; it can be wrong or out of scope. Proposed diffs
  are suggestions, not patches: verify and apply them yourself — Codex
  never writes to the tree.
- NEVER paste corpus text (handbook extracts, chunk contents, held-out eval
  questions) into a Codex prompt, and always include the do-not-read clause
  above — the corpus is copyrighted and must not be shipped to a third-party
  model (same reason as the `data/` commit ban; note `chroma_db/` contains
  the full corpus text too).

## Conventions
- Python 3.11+, type hints and docstrings on all public functions.
- Structural regexes anchor to line starts; no IGNORECASE on structural
  markers (learned from the PART false-positive).
- Chunk metadata keys: chapter_number, chapter_title, section_number,
  heading, page_start, page_end, source, title, document_type.
- Tests mock all IO and models (see the FakeEmbeddings pattern in
  tests/test_embedder.py, imported by the other test files that need it);
  no network or API calls in the test suite.
- Explain non-obvious diffs at the level of someone re-learning Python.

## Context that saves you time
- Corpus PDF has an OCR text layer: expect header/footer pollution,
  hyphenation breaks, occasional character errors. Cleaning happens in
  ingest, driven by scripts/extraction_qa.py findings.
- ingest returns (clean_text, page_map) — page_map carries per-page char
  offsets; the chunker uses it to assign page_start/page_end.
- Embedding model: sentence-transformers/all-MiniLM-L6-v2, recorded in the
  Chroma collection metadata; corpus and queries must use the same model.
- Retrieval is hybrid: BM25 + vector, fused with reciprocal rank fusion.
- The legislation chunker (PART/Section patterns) is retained and routed by
  document_type; the handbook strategy is the default for this corpus.
