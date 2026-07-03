# Design decisions log

> One short entry per meaningful choice: what we decided, why, what we rejected.
> Append-only. If a decision is reversed, add a new entry — don't edit history.
> This file goes in `docs/decisions.md`.

**Current phase: 0 — repo hygiene + extraction QA gate**

---

## D1 — pdfplumber retained for extraction (2 Jul 2026)
**Decision:** keep pdfplumber rather than switching to PyMuPDF.
**Why:** it already works, the corpus has a usable OCR text layer, and switching
libraries mid-deadline buys marginal speed for real churn.
**Rejected:** PyMuPDF (faster, but migration risk); re-OCR with OCRmyPDF
(only if Phase 0 QA shows the existing text layer is bad).
**Consequence:** revisit only if extraction QA fails.

## D2 — Page-aware ingestion replaces one-Document-per-PDF (2 Jul 2026)
**Decision:** ingest returns (clean_text, page_map) with per-page character
offsets instead of joining all pages into a single Document.
**Why:** the old design destroyed page numbers at birth, making page-level
citations permanently impossible — and citations are the product.
**Rejected:** one Document per page (fragments paragraphs at page breaks —
the original scaffold was right to avoid this, wrong to lose the page info).
**Consequence:** chunker gains page_start/page_end via offset lookup.

## D3 — Chunker strategies routed by document type (2 Jul 2026)
**Decision:** add a `handbook` chunking strategy (Chapter N / decimal-numbered
paragraphs); retain the `legislation` strategy (PART / Section); route on
document_type.
**Why:** experiment on 2 Jul proved the legislation patterns match nothing in
handbook-style text — the whole book fell through to naive fixed-size splitting
with empty section metadata, silently.
**Rejected:** one universal regex set (false positives; untestable);
deleting the legislation strategy (it's correct for its document type and
already tested).
**Consequence:** each strategy gets its own fixture-based tests; structural
patterns anchor to line starts, case-sensitive.

## D4 — Chunk unit: numbered paragraph, merged/split to 150–1,000 tokens (2 Jul 2026)
**Decision:** one chunk per numbered handbook paragraph; merge runts within a
section, split oversized via the existing fallback splitter with metadata
inheritance.
**Why:** aligns chunk boundaries with the author's own meaning boundaries —
avoids both diluted mega-chunks and orphaned fragments.
**Rejected:** fixed-size splitting (loses structure); whole-section chunks
(diluted vectors on long sections).
**Consequence:** chunk size becomes a tunable verified in Phase 5 eval.

## D5 — Embeddings: all-MiniLM-L6-v2 baseline, recorded in collection (2 Jul 2026)
**Decision:** keep the existing local model; write its name into Chroma
collection metadata and assert it at query time.
**Why:** free, fast on CPU, good enough for a v1 whose quality lever is
chunking+hybrid retrieval, and it keeps the demo runnable by anyone.
**Rejected:** API embedders (cost + key friction for reviewers cloning the
repo); larger local models (slower, marginal gain unproven — eval first).
**Consequence:** corpus and queries share one coordinate system by
construction, not by convention.

## D6 — Hybrid retrieval: BM25 + vector with reciprocal rank fusion (2 Jul 2026)
**Decision:** add BM25 alongside vector search; fuse rankings with RRF.
**Why:** legal queries are exact-token-heavy (section numbers, form names,
statute titles); pure semantic search fuzzes past the literal token that
matters.
**Rejected:** vector-only (the s.68 problem); cross-encoder reranking
(genuine upgrade, but post-submission — see roadmap).
**Consequence:** BM25 index built at index time and persisted next to
chroma_db/.

## D7 — Content-hash chunk IDs (2 Jul 2026)
**Decision:** chunk ID = sha256(chunk_text)[:16], replacing source+index IDs.
**Why:** positional IDs collide with *different* content the moment chunking
changes, so "deduplication" silently preserved stale chunks. Content hashes
make identity mean identity.
**Rejected:** positional IDs (the bug); UUIDs (no dedup at all).
**Consequence:** re-indexing after chunker changes behaves correctly; drops
the private `_collection` API access as a side effect.

## D8 — Thin LangChain layer retained for v1 (2 Jul 2026)
**Decision:** keep langchain-core Documents, the fallback splitter, and the
ChatAnthropic wrapper as-is.
**Why:** it works, it's shallow enough to explain line-by-line, and
de-LangChaining costs days that citations and evals need more.
**Rejected:** rewrite on raw anthropic + chromadb clients (cleaner, genuinely
tempting, not worth 2 days this week).
**Consequence:** revisit post-submission if the framework fights us.

## D9 — Corpus stays out of the public repo (2 Jul 2026)
**Decision:** data/ and *.pdf remain gitignored; repo ships pipeline + tests +
eval harness, framed "bring your own manual." Verified 2 Jul: no sensitive
blob exists anywhere in git history.
**Why:** the handbook is copyrighted; and corpus-agnostic framing makes the
project generalisable — a stronger portfolio claim than a single-book tool.
**Consequence:** README quickstart uses any user-supplied PDF; demo recording
uses the real corpus locally.

---

<!-- Append new entries below. Format: ## Dn — Title (date) / Decision / Why / Rejected / Consequence -->
