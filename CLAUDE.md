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

## Commands
- Run tests: `python -m pytest tests/ -q`  (must pass before any commit)
- Index corpus: `python -m src.pipeline index ./data/Conveyancing_Handbook.pdf --type handbook`
- Query: `python -m src.pipeline query "..." --top-k 6`
- Extraction QA: `python scripts/extraction_qa.py ./data/Conveyancing_Handbook.pdf`
- Eval: `python -m src.pipeline eval`

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
