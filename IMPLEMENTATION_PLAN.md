# Implementation Plan — Legal RAG Pipeline (v1 → submission)

**Target (superseded — see "Two-track remediation" after Phase 12):** ~~working, evaluated,
documented pipeline frozen by Fri 10 July, submitted Sun 12 July~~. Following the 11 Jul external
critique, the plan split into two tracks: v1 frozen and tagged `v1.0-baseline` on **Sat 11 July**;
v2 production-hardening (Phases 7–12) targeted for **Mon 13 July**; Claude Corps Fellowship
deadline **17 July** is buffer for the v1-vs-v2 submission decision.

**Scope discipline:** one corpus (the conveyancing handbook), one job (answer procedure questions with chapter/paragraph/page citations, refuse when out-of-corpus), one interface (CLI). Anything else is post-submission.

**How to use this file:** each phase is one or two Claude Code sessions. Open the session with:
> Read CLAUDE.md and IMPLEMENTATION_PLAN.md. We are doing Phase N. Enter plan mode and propose your approach before touching anything.
Review the plan, challenge it, approve, implement, run the acceptance checks, commit, push.

---

## Phase 0 — Repo hygiene + go/no-go gate (Thu 2 July, evening, ~1–2h)

1. Commit this file, `CLAUDE.md`, and `docs/decisions.md` to the repo.
2. Replace the 2-line README with a stub: one-paragraph description, status badge line ("under active development, submission 12 July"), quickstart placeholder.
3. Pin dependencies: `pip freeze` in a clean venv after install, write exact versions to `requirements.txt`.
4. **The gate — extraction QA script** (`scripts/extraction_qa.py`):
   - Opens the real handbook PDF with pdfplumber.
   - Prints N random pages of extracted text alongside page numbers.
   - You eyeball 10 pages against the PDF for: (a) text fidelity (OCR errors?), (b) page furniture polluting the stream (headers/footers/page numbers mid-text?), (c) do section numbers like `3.2.1` survive cleanly at line starts?
   - Also print the set of distinct line-start patterns matching `^\d+(\.\d+)*` and `^Chapter \d+` so we learn the book's real numbering grammar.

**Acceptance:** you can describe, in one paragraph in `docs/decisions.md`, exactly what the raw extracted text looks like and what cleaning it needs. Everything in Phases 1–2 depends on this evidence.

**Go/no-go:** if extraction is garbage (unlikely, since copy-paste works), we pivot to OCRmyPDF re-processing — flag it to Claude immediately.

---

## Phase 1 — Ingestion v2: page-aware, cleaned (Fri 3 evening + Sat 4 morning)

**Design change:** stop joining all pages into one Document. Instead:

1. Extract per-page, recording `(page_number, char_start, char_end)` offsets into the concatenated text — a **page map**.
2. Cleaning pass, driven by Phase 0 findings, typically:
   - strip running headers/footers (detect lines repeating on >30% of pages),
   - strip standalone page-number lines,
   - repair hyphenation across line breaks (`regis-\ntration` → `registration`),
   - normalise whitespace without destroying paragraph breaks.
3. Return `(clean_text, page_map)` so the chunker can later assign `page_start`/`page_end` to every chunk via offsets.
4. Keep HTML/eISB loaders untouched (off critical path).

**Tests:** unit tests for header/footer stripping, hyphenation repair, and page-map offset correctness on synthetic multi-page input.

**Acceptance:** re-run `extraction_qa.py` on the cleaned output — 10/10 sampled pages clean; a spot-checked sentence's offsets map back to the correct PDF page.

---

## Phase 2 — Handbook chunker (Sat 4 afternoon + Sun 5)

**Design:** chunker strategies routed by document type (keep the existing legislation strategy; add `handbook`):

1. Patterns from the *real* numbering grammar discovered in Phase 0 — expected shape: `^Chapter \d+` for chapters, `^\d+\.\d+(\.\d+)?\s` for numbered paragraphs. Anchor patterns to line starts; **no IGNORECASE** on structural markers (the `PART` false-positive lesson).
2. Chunk = one numbered paragraph. Merge runt neighbours (< ~150 tokens) within the same section; split oversized ones (> ~1,000 tokens) with the existing fallback splitter, *inheriting* the section metadata.
3. Metadata per chunk: `chapter_number`, `chapter_title`, `section_number` (e.g. "3.2.1"), `heading`, `page_start`, `page_end` (via the page map), plus existing fields.
4. Keep the contextual prefix idea but enrich it: `[Conveyancing Handbook, Ch.3 Registration of Title, para 3.2.1, p.87]`.

**Tests:** feed a realistic handbook-style fixture (chapters + decimal numbering + a prose line starting "Part I of the folio" as a false-positive trap); assert split counts, metadata values, page assignment, runt-merging.

**Acceptance (on the real book):** ≥90% of chunks carry non-empty `section_number`; chunk count is plausible (an 800-page handbook ≈ high hundreds to ~2,000 chunks); you manually verify 10 random chunks' section number **and page number** against the PDF. Record the verified hit rate in `docs/decisions.md`.

---

## Phase 3 — Retrieval v2: hybrid + storage fixes (Mon 6, evening)

1. **BM25** over the chunk store (`rank_bm25`), built at index time and persisted (pickle alongside `chroma_db/`).
2. **Reciprocal rank fusion** of BM25 and vector rankings (`score = Σ 1/(60 + rank)`); retrieve ~12 from each, fuse, return top-k (default 6).
3. **Fix IDs:** content-hash (`sha256(chunk_text)[:16]`) instead of positional index — re-chunking now correctly re-indexes changed content. Drop the private `_collection` access.
4. Record `embedding_model` in the Chroma collection metadata; assert it matches at query time.

**Tests:** RRF fusion math on synthetic rankings; an exact-token test — a query containing a term that appears verbatim in exactly one chunk must rank that chunk top-3.

**Acceptance:** on the real index, 5 exact-token queries ("priority entry", "s.72 burdens", a Form name, etc.) each retrieve the right paragraph in the top 3.

---

## Phase 4 — Generation polish (Tue 7, evening)

1. Citation format becomes `[Handbook, para 3.2.1, p.87]` sourced from chunk metadata; update the system prompt and the source-extraction regex together.
2. Add a tested **refusal path**: a question the handbook cannot answer ("What is the CGT rate?") must produce an explicit "not covered in the source material" response, not a guess.
3. Surface retrieval scores in CLI output (`--verbose`) so you can see *why* an answer cited what it cited.

**Acceptance:** 3 real questions answered with correct para+page citations verified against the PDF; 2 out-of-corpus questions correctly refused.

---

## Phase 5 — Evaluation harness (Wed 8, evening; write questions at lunch)

1. `eval/golden_set.jsonl` — ~25 questions **you write from your actual work**, each with the expected section number(s). Mix: 15 direct ("what does the handbook say about X"), 5 exact-token, 5 out-of-corpus (expected answer: refusal).
2. `python -m src.pipeline eval` — computes **retrieval hit@k** (expected section in top-k) and refusal accuracy; prints a table; writes `eval/results.md`.
3. One tuning iteration: try chunk-size and k variants, keep the winner, log the numbers.

**Acceptance:** hit@6 ≥ 80% on in-corpus questions; 5/5 refusals correct. If below, the results table tells you whether chunking or retrieval is the culprit — fix the bigger one, re-run, stop. **Do not tune past Wednesday.**

---

## Superseded (was Phase 6 — Portfolio surface): content moved to Phase 11

> This section's original scope (README rewrite, demo recording, decisions.md completeness,
> fresh-clone test) is superseded by the new Phase 6 (v1 freeze) and folded into
> **Phase 11 — portfolio surface** below. Body kept for reference only — `/phase-gate 6` must
> resolve against the new Phase 6, not this one.

1. **README rewrite** — the most-read artefact in the repo: what/why (the real workplace problem), architecture diagram (ASCII fine), quickstart that works from `git clone` in ≤5 commands, the eval results table, honest limitations, roadmap (matter-scoped deployment vision).
2. 2–3 minute screen recording: index → ask 3 questions → show citations → show a refusal → show eval output.
3. `docs/decisions.md` complete — every D-entry filled in.
4. Fresh-clone test: new venv, follow your own README verbatim. If it breaks, fix the README.

---

## Freeze + submission track

> **Superseded** by the two-track plan (Phases 6–12 below, see "Two-track remediation" after
> Phase 12): the 11 Jul external critique triggered a v1 freeze + v2 production-hardening split.
> Original single-track dates kept below for the record.

- **Fri 10:** code freeze. Hand repo + demo to PhD reviewer. Switch fully to essays.
- **Sat 11:** incorporate feedback (docs/small fixes only — no new features), essays final.
- **Sun 12:** submit.

**Parallel track (not optional):** AI Fluency + Claude 101 modules done by **Sun 5**. Essay drafts (community impact: colleagues adopting the S.150 skill; setback: your call) exist by **Wed 8** — a fellowship application is essays *and* project, weighted accordingly.

## Cut list (pre-agreed, in order, if behind schedule)
1. Tuning iteration in Phase 5 (keep the harness, skip optimisation)
2. Refusal-path *tests* (keep the behaviour)
3. BM25 persistence (rebuild index at query time — slower, works)
4. Screen recording (README screenshots instead)

**Never cut:** page-aware citations, the golden set, the README.

---

## Two-track remediation (Phases 6–12 supersede the tail above)

An external critique (11 Jul 2026) verified against the code: grounding was checked but not
enforced at the output boundary; the 90% eval headline conflated related-section matching with
exact retrieval and was tuned on its own question set; re-indexing without `--reset` leaves stale
chunks; `is_refusal` accepted hedged answers; appendix citations were invisible to the citation
extractor. Response (see `docs/decisions.md` D32–D33 onward): freeze today as v1 — honest,
fail-visible, not fail-closed — then implement the critique properly for v2 (fail-closed grounding
gate, auditability, held-out eval) by 13 Jul, and decide on the 13th which to submit.

## Phase 6 — v1 freeze: honesty + safety minimum (Sat 11 Jul, ~3.5–4.5h) — `phase-6-v1-freeze`

**Design:** scope discipline is fail-visible, not fail-closed. No gate module, no appendix work, no
lifecycle work — anything running past its timebox defers to its v2 phase.

1. **Zero-citation warning** (`src/pipeline.py` `query()`): a non-refusal answer with empty
   `citations` prints a prominent "WARNING: this answer contains no citations and could not be
   verified — treat as unverified."
2. **Always-print ungrounded warnings**: move the ungrounded block out of `if verbose:` so it
   always shows.
3. **Strict refusal matching** (`src/generator.py` `is_refusal`): normalized exact match — strip
   whitespace + surrounding quotes + trailing period, casefold, compare to `REFUSAL_PHRASE`.
   Evaluator imports it, so eval inherits the stricter definition.
4. **Eval labeling + provenance** (`src/evaluator.py`): per-question `hit_strict` (exact
   section-number equality) alongside existing `hit_related` (dotted-nesting); report shows both,
   strict first, labeled "n=30 tuning set — used to select fusion constants (D31); NOT held-out."
   `collect_provenance()` records git SHA + dirty flag, indexed chunk count, embedding model,
   generation model, and the matching definitions; `_format_report` adds top_k, golden path +
   per-type counts, and the refusals-skipped flag from its own arguments. Injectable `provenance_fn`
   keeps tests IO-free.
5. **Embedding cache** (timeboxed 20 min): `@functools.lru_cache(maxsize=1)` on
   `get_embedding_function()`. Revert and defer to Phase 9 if anything fights it.
6. **Re-run eval on the real index** (retrieval offline; refusal pass = 5 live calls under the
   strict matcher). Report the honest number even if strict matching drops a live refusal —
   do not loosen the matcher to protect the metric. Commit regenerated `eval/results.md`.
7. **README honest interim rewrite**: drop the "corpus-agnostic" overclaim; real quickstart with
   the corpus-not-distributable caveat; eval table with both strict and related hit@6, labeled;
   short honest limitations list.
8. **Extend this file** with Phases 6–12, and physically rename the old
   "Phase 6 — Portfolio surface" heading (done above) so `/phase-gate 6` locks onto this Phase 6,
   not the old one.
9. **Gate + tag:** `/phase-gate 6`, PR, merge; tag `v1.0-pre-critique` on `17d23b1` and
   `v1.0-baseline` on the merge commit; push both tags.

**Tests:** hedged-phrase answer NOT a refusal / exact-phrase-with-period IS; zero-citation warning
printed (capsys) and absent for refusals; ungrounded warning without `--verbose`; strict-vs-related
divergence fixture (expected `14.12`, retrieved `14.12.1` → related hit, strict miss); provenance
from injected fake; existing suite green.
**decisions.md:** D32 (fail-visible warnings + strict refusal semantics), D33 (dual-metric labeling
+ provenance; strict becomes the headline basis going forward).
**Acceptance:** full suite green; live citation-free non-refusal shows the warning; eval/results.md
carries strict AND related rates plus provenance (git SHA, chunk count, both model names); both
tags exist on origin.

---

## Phase 7 — appendix citations end-to-end (Sun 12 Jul am, ~2h) — `phase-7-appendix-citations`

**Design:** must precede the grounding gate — today an appendix-only-cited answer extracts zero
citations and would be wrongly blocked. 4 golden questions expect APPENDIX sections.

1. **Fix `_handbook_header`** (retriever.py:143-155): `section_number.startswith("APPENDIX")` →
   emit verbatim, no `para` token — mirrors `chunker._prefix`, one locator grammar everywhere.
2. **Extend `CITATION_RE`** (generator.py, ~the `CITATION_RE = re.compile(` line): alternation `para <digits>` OR
   `APPENDIX <d+.d+>` (case-tolerant on the token); normalize into the existing dict shape (`para`
   key holds `"3.2.1"` or canonical `"APPENDIX 14.1"`).
3. **Extend `_sections_related`**: appendix-ness must match on both sides — `"14.1"` never relates
   to `"APPENDIX 14.1"`; both-appendix → strip prefix, existing component-nesting rule; mixed →
   False. `_citation_matches_chunk` + eval hit@k inherit automatically.
4. Live spot-check: golden Q22 (Gas Act wayleaves) — appendix citation appears in sources and
   grounds.

**Tests:** header rendering (no "para", verbatim); extraction in compact + long D21 bracket forms;
appendix citation grounds against appendix chunk; para/appendix cross-match False both directions;
lowercase "Appendix" extracts; eval hit against `["APPENDIX 14.1"]`.
**decisions.md:** D34 (appendix locators first-class; never-cross-match rule).
**Acceptance:** suite green; live Q22 shows grounded appendix citation; malformed header gone.

---

## Phase 8 — grounding gate + audit trail (Sun 12 Jul pm, ~3h) — `phase-8-grounding-gate`

(Amended 11 Jul per colleague review — outcome renames, mandatory page check, gated public
return, query hashing. PARTIALLY_VERIFIED display: user chose show-with-banner over the
colleague's withhold; record both positions in D35.)

1. **New `src/grounding.py`** — `classify(answer, citations, citation_check)` → string
   constants named for what the system actually verifies (LOCATORS, not legal claims):
   `REFUSAL` / `CITATIONS_VERIFIED` (≥1 citation, zero unverified) / `PARTIALLY_VERIFIED`
   (≥1 verified AND ≥1 unverified) / `CITATIONS_UNVERIFIED` (non-refusal, zero verified —
   includes zero-citation, closing the P0). Display copy says "citations verified against
   retrieved sources", never a bare "verified". Called inside `generate_with_sources` →
   `result["gate_outcome"]` reaches every consumer; display policy stays in pipeline.py.
   **Gate matching rule:** `_citation_matches_chunk` keeps nesting-plus-page-span, but the
   page check becomes MANDATORY — a chunk with no page metadata cannot verify a citation
   (fail closed). Exact-only locator equality REJECTED (record in D35): chunks are
   section-granular (D28); correct answers cite finer sub-paragraphs inside a chunk, so
   exact equality would structurally block the best answers.
2. **Fail-closed display** (`pipeline.query`), per locked decisions:
   - CITATIONS_VERIFIED → answer + citations + "all citations verified against retrieved
     sources".
   - PARTIALLY_VERIFIED → answer shown; the warning banner NAMES each unverified citation
     ("N of M citations could not be verified — check before relying: …"). (User decision
     11 Jul; colleague preferred withholding — D35 records the trade.)
   - CITATIONS_UNVERIFIED → **answer withheld**; banner `BLOCKED — CITATIONS UNVERIFIED`
     (wording says citations *could not be verified*, never "not in the corpus");
     retrieved source headers (section + pages only, no chunk text) shown; hints to
     rephrase / `--top-k` / `--show-unverified`.
   - `--show-unverified` (new flag): reveals the draft under "UNVERIFIED DRAFT — do not
     rely on this text"; use recorded in the event log.
   - **Gated public return:** when withheld, `query()`'s returned dict does NOT carry the
     draft text — `answer` holds the block notice; `gate_outcome`, citation lists,
     sources, and `answer_chars` are present. Programmatic access to the raw draft is only
     via `generate_with_sources` (internal; eval already calls it directly) or an explicit
     `show_unverified=True` param mirroring the CLI flag — keeps any future API consumer
     safe by default.
   - `query()` reads `result.get("gate_outcome")` with a defined fallback for
     legacy/missing results; the two existing `test_pipeline.py` mocks get `gate_outcome`
     added — both, belt and braces.
   - Extend `/phase-gate`'s hygiene check to cover `logs/` alongside
     `data/`/`*.pdf`/`.env`/`chroma_db/` (`.gitignore` is the primary guard, the skill is
     the backstop).
3. **New `src/audit.py`** — an **operational event log** (append-only JSONL,
   `logs/audit_log.jsonl`, `AUDIT_LOG_PATH` env override; no tamper-evidence claims).
   Record: ISO timestamp, git SHA (best-effort), **`query_sha256` + `query_chars` — NOT
   raw query text by default** (legal queries can reveal client matters; raw logging only
   via explicit `AUDIT_LOG_RAW_QUERIES=1`), top_k, type filter, retrieved
   `[{id, section_number, page_start, page_end, score}]` (content-hash IDs —
   copyright-safe), `gate_outcome`, `action` (`shown`/`shown_with_warning`/
   `blocked_unverified`/`refusal_shown`/`shown_unverified_override`), verified/unverified
   counts, citation locator strings, generation model, `answer_chars`. **Excluded: answer
   text, chunk text, and (by default) query text.** Always-on in `pipeline.query`.
   Add `logs/` to `.gitignore`.

**Tests:** classification matrix (refusal / zero-citation / all-verified / mixed /
appendix-only / no-page-metadata chunk → fails closed); capsys: CITATIONS_UNVERIFIED
withholds body + shows sources AND the returned dict carries no draft text, override
reveals with banner + event-log flag, PARTIALLY shows banner naming the failed citations;
audit line has expected keys, no answer/chunk/raw-query text by default, raw query present
only under the env opt-in, appends (tmp_path); generation seams patched — no live calls.
**decisions.md:** D35 (gate semantics; outcome naming rationale; exact-only matching
rejected via D28; mandatory page check; show-vs-withhold for PARTIALLY — user decision
with colleague counter-position recorded), D36 (event-log contents; why answer/chunk/
raw-query text excluded; "operational log, not tamper-evident audit trail").
**Acceptance:** suite green; live in-corpus Q → CITATIONS_VERIFIED + shown;
citation-stripped answer → withheld with sources and no draft in the returned dict; one
well-formed event line per query with hashed query; `git status` clean of `logs/`.

---

## Phase 9 — index lifecycle + load-once retrieval (Sun 12 Jul eve, ~2.5h) — `phase-9-index-lifecycle`

(Amended 11 Jul per colleague review — all five hardening points adopted.)

1. **Per-source replace, hardened** — new `sync_documents(source, documents,
   vector_store=None, persist_directory=None)` in `src/embedder.py` (`add_documents` stays
   untouched/insert-only for additive callers):
   - **Source-scoped chunk IDs:** `compute_chunk_id(source, text)` =
     `sha256(source + "\0" + text)[:16]`. Pure text-hashes collide across sources
     (identical boilerplate in two documents → deleting source A's stale IDs could destroy
     source B's chunk; insert-dedup would skip B's copy) — real risk given the multi-doc
     roadmap. D7's "identity means identity" now holds per-source. **Consequence: all
     existing IDs change → one full `--reset` re-index after this lands** (BM25 sidecar
     rebuilds with it).
   - **Explicit `source` param** so an empty `documents` list expresses "this source now
     has zero chunks" (delete-all-for-source), which group-by-metadata cannot.
   - **Upsert before delete:** insert/refresh new chunks FIRST, delete stale IDs LAST — a
     crash mid-sync leaves harmless extras (re-sync converges), never missing chunks.
   - **Metadata-only updates propagate:** for surviving IDs (text unchanged), compare
     metadata; on difference, update in place (wrapper update API if available, else
     delete+re-add those IDs) — fixes the case where a chunker improvement only changes
     page/section metadata and content-hash dedup would silently keep the stale metadata.
   - **Rebuild BM25 sidecar + manifest if anything changed** (`stale or new or updated`) —
     covers the delete-only trap where a re-sync leaves deleted chunks in BM25.
   `pipeline.index_documents` switches to `sync_documents`; `--reset` retained for full
   rebuilds.
2. **Retriever injection** — `retrieve(query, ..., vector_store=None, bm25_index=None)`
   (same explicit-injection convention as `add_documents`); skip store/BM25 construction
   when injected. Evaluator's default `retrieve_fn`/`answer_fn` build once and close over
   them (eval currently reloads MiniLM 35×); `pipeline.query` passes a once-built store.
   Expose `--persist-dir` on the CLI (index/query/eval) — needed by Phase 11's isolated
   `sample_chroma_db/`. Land the Phase 6 `lru_cache` here if it was deferred.

**Tests:** index → mutate one chunk's text → re-sync → stale ID absent from store AND from
BM25 results (the trap test); metadata-only change → surviving ID's metadata updated;
unchanged chunks not re-embedded (ID set stable); two sources with IDENTICAL chunk text
don't collide or delete each other (the source-scoped-ID test); `sync_documents(source, [])`
empties exactly that source; injected store/bm25 used without disk loads (monkeypatch
counters).
**decisions.md:** D37 (per-source replace semantics; `add_documents` stays insert-only;
BM25 delete-rebuild trap; source-scoped IDs).
**Acceptance:** suite green; real corpus: re-index without `--reset` → final ID set exactly
equals a fresh `--reset` build's; retrieval-only eval wall-clock measurably down (record
before/after).

---

## Phase 10 — Eval v2: honest, held-out, ablated (Mon 13 Jul am, ~4h) — `phase-10-eval-v2`

**Prerequisite** (Sat 11 night, human, ~1h, no pipeline runs, no peeking): author
`eval/heldout_set.jsonl` — 15–20 fresh in-corpus questions (direct + exact_token, verified against
the PDF, never used in tuning) + 5–8 near-domain refusal hard negatives, each grepped against the
PDF text before locking in (the handbook has tax/family-home/lease chapters that could make a
candidate negative actually in-corpus). Same schema; `load_golden_set` reused unchanged.

1. **Ablation plumbing:** `retrieve(..., mode="hybrid"|"vector"|"bm25")` selects what feeds RRF.
2. **Metrics:** hit@1/3/6 in strict AND related bases + MRR (first strict / first related match),
   from one retrieval at k=6 per question.
   - **Carry-over from Phase 6 (pressure-tester footgun):** `run_eval` ALWAYS overwrites
     `results_path` (default `eval/results.md`), even under `--skip-refusals` — so an offline/CI run
     silently degrades the canonical report (drops the live 5/5 refusal line). Add a `--results`/`-o`
     CLI flag (and/or refuse to overwrite the default path when refusals are skipped) so CI and
     ad-hoc offline runs write elsewhere. This is the natural home for the fix (eval CLI is already
     gaining `--skip-completeness`/`--judge` here); Phase 11 CI depends on it.
3. **Runner + report:** each mode × each set (tuning, held-out) → full table; provenance extended
   with mode + set hashes; headline = strict hit@6 on held-out; ablation table; per-question
   detail; sets labeled "tuning (used for D31)" vs "held-out (never tuned)".
4. **Answer-generation pass (shared):** generate once for in-corpus questions; feed both the
   citation-completeness metrics — (a) sentence-citation coverage, (b) citation-grounded
   fraction, (c) gate-outcome distribution, **(d) false-refusal rate: fraction of answerable
   (in-corpus) questions whose answer scored `is_refusal` — and, with the gate on,
   false-block rate (answerable questions wrongly withheld)** (colleague point adopted:
   refusal quality must be measured in both directions) — and the judge below. Flag
   `--skip-completeness` mirrors `--skip-refusals`.
5. **`--judge` (experimental, off by default):** per generated answer, one judge call over the
   retrieved context → per-claim supported/unsupported/unclear + mean faithfulness, reported as
   "LLM-judged faithfulness estimate"; judge model + prompt version + config recorded in
   provenance; `--judge-sample N` if time is tight, disclosed in the report; ≥5-answer manual
   spot-review noted.
6. **Sub-chunking decision gate (decide, don't build):** write D39 from the ablation numbers — if
   vector-only related-hit@6 is within ~10 pts of hybrid, defer multi-vector post-submission; if
   materially weak, the sanctioned lever is lowering the oversize-chunk threshold + re-index, not a
   multi-vector build.
7. Full eval on real corpus; commit reports (D30 scrub rule: no corpus prose).

**Tests:** mode selection via fakes; strict/related + MRR math on synthetic rankings; hit@1 ≤ hit@3
≤ hit@6 property; splitter + completeness on fixed fake answers; both-set report rendering
(injected fakes); judge prompt construction with mocked LLM. All IO injected.
**decisions.md:** D38 (eval v2 design: held-out strict headline, never-tune-on-held-out protocol),
D39 (sub-chunking go/no-go with pasted ablation numbers).
**Acceptance:** committed report has provenance; strict+related hit@{1,3,6}+MRR for 3 modes × 2
sets; refusal accuracy incl. near-domain negatives; completeness metrics; judge estimate (or
disclosed subset); D39 recorded with evidence.

---

## Phase 11 — portfolio surface (Mon 13 Jul pm, ~3h) — `phase-11-portfolio-surface`

1. **Synthetic sample corpus:** `scripts/sample_corpus.py` — copyright-safe ~15-page synthetic
   handbook adapted from `_handbook`/`_body` in tests/test_chunker_handbook.py (standalone copy
   + cross-ref comment; don't make tests import from scripts): 2–3 chapters, nested sections,
   one APPENDIX, a false-positive trap line. `scripts/build_sample_index.py`: text →
   `chunk_handbook` → `sync_documents` → **`./sample_chroma_db/` — NEVER the real
   `./chroma_db/`** (colleague point adopted: the sample builder must not contaminate or
   replace the local real index; directory gitignored; smoke eval points at it via Phase 9's
   `--persist-dir`). Recorded decision: script-not-PDF (PDF generation = new dependency,
   rejected per CLAUDE.md; the script enters the real pipeline at the post-extraction seam).
   Plus `eval/sample_golden_set.jsonl` (5–8 questions incl. one appendix expectation).
2. **CI** (`.github/workflows/ci.yml`): job 1 — ubuntu, Python 3.12, pip cache, `pytest tests/ -q`;
   job 2 (smoke) — build sample index, `eval --golden eval/sample_golden_set.jsonl
   --skip-refusals --skip-completeness`; cache `~/.cache/huggingface`. No `ANTHROPIC_API_KEY`
   in CI — offline paths only.
3. **LICENSE:** MIT; README states it covers code only, corpus never distributed.
4. **README final:** what/why; ASCII architecture diagram (ingest → chunk → hybrid index → RRF →
   generate → grounding gate → audit log); fresh-clone quickstart via sample corpus; honest eval
   table (held-out strict headline, ablations); data-handling section; firm-deployment notes;
   limitations; troubleshooting; demo script; roadmap.
5. **Fresh-clone verification:** clone to scratch dir, new venv, follow README verbatim; fix README
   where it breaks.

**Tests:** sample builder yields expected chunks/sections/one appendix via `chunk_handbook`
(offline).
**decisions.md:** D40 (sample-corpus mechanism), D41 (MIT + scope note).
**Acceptance:** fresh clone passes quickstart offline; CI green on the PR; README complete per
above.

---

## Phase 12 — final gate + v1-vs-v2 decision (Mon 13 Jul eve, ~2h) — on main

(Amended 11 Jul per colleague review: the head-to-head must be same-set, same-index.)

1. `/phase-gate` over v2 (full suite, pressure-tester, high-effort code review, hygiene: nothing
   tracked in `data/`, `*.pdf`, `.env`, `chroma_db/`, `sample_chroma_db/`, `logs/`).
2. Re-run full Phase 10 eval at v2 HEAD (live refusals + completeness; `--judge` per user opt-in);
   commit.
3. **Valid head-to-head (replaces tag-report comparison):** evaluate BOTH versions on the SAME
   frozen held-out set: `git worktree add /tmp/v1-eval v1.0-baseline`, run v1's own evaluator
   (`python -m src.pipeline eval --golden <heldout>` — v1's evaluator already speaks dual metrics
   and `load_golden_set` is schema-compatible) against the SAME index as v2. Caveat: valid only if
   the index is unchanged between versions (expected — D39's default is defer); if Phase 10
   re-chunked, each version runs against its own freshly built index and the comparison discloses
   that retrieval differences include chunking. Comparing v1's tuning-set related score against
   v2's held-out strict score is narrative, not measurement — both appear in the doc, but only
   same-set/same-basis numbers sit in the head-to-head table.
4. **`docs/v1-v2-comparison.md`:** the same-set head-to-head table (strict + related, hit@{1,3,6},
   refusal accuracy incl. false-refusal rate, for v1 and v2) + feature table (gate, appendix
   citations, lifecycle, event log, provenance, held-out eval, CI, LICENSE, README) + the
   narrative metrics clearly labeled as such. State honestly if v2's held-out strict headline is
   lower than v1's tuning-set related 0.900 — lower-but-honest is the expected, defensible
   outcome.
5. Submission decision recorded as **D42** (recommendation: v2 if the gate passes — "critique →
   production hardening → honest metrics" is itself the portfolio story; `v1.0-baseline` stays
   the fallback). Tag `v2.0`; push.

**Acceptance:** gate PASS; comparison doc committed with the same-set table; D42 records the
decision; chosen artifact tagged + pushed.

---

## Phase 13 — query robustness: rewrite + graded answers (Tue 14 Jul, ~1 day) — `phase-13-query-robustness`

(Post-v2.0 remediation. 14 Jul field-testing showed natural staff phrasing gets the refusal
sentence from the model itself — audit log: six queries, all `gate_outcome: REFUSAL`, gate never
fired — because (a) vague phrasing retrieves disjoint BM25/vector rankings and the right chunks
miss top-6, and (b) prompt rule 3 is binary answer-or-refuse, refusing even with the correct
chunk at rank 1. The AI-generated eval sets masked this: they share the corpus vocabulary, so
held-out strict hit@6 read 20/20. Diagnosis + design: docs/decisions.md D43–D47.)

1. **Multi-query weighted retrieval (D43, D45):** `retrieve()` gains keyword-only
   `rewrites: Optional[Sequence[str]]` — each used arm runs once per sub-query (original
   question always first; casefold dedup), one ranked-ID list per (arm × sub-query),
   `id_to_doc` unioned, all lists fused by `_reciprocal_rank_fusion`, which gains optional
   per-list weights (original lists 1.0, rewrite lists 0.5 — correlated-rewrite noise cannot
   outvote original-arm agreement). `CANDIDATE_POOL`/`RRF_K`/`DEFAULT_TOP_K` unchanged
   (12/60/6 — D31 upheld, pool widening rescinded at plan gate). `rewrites=None` is
   byte-identical to today.
2. **Expansion stage (D43):** new `src/query_rewrite.py` — `REWRITE_MODEL="claude-haiku-4-5"`,
   `get_rewrite_llm()` (sibling of `get_llm`, same key guard), `parse_rewrites()` (pure,
   defensive), `expand_query(question, *, llm=None) -> Expansion` (original; effective
   rewrites deduped & original-excluded; model; status `live|no_key|api_error|parse_error|
   disabled`) — never raises; any failure → zero rewrites + status. `pipeline.query()` calls
   it between context-load and retrieve (keyword-only `no_rewrite: bool = False`, passed by
   keyword from `main()`); new `--no-rewrite` CLI flag; rewrites print under `--verbose`.
3. **Audit fields (D43):** `build_event()` gains keyword-only `rewrites=None` + expansion
   status; always logs `rewrite_count`, `rewrite_sha256s`, `rewrite_status`; raw rewrite text
   only under the existing `AUDIT_LOG_RAW_QUERIES=1` gate (same client-matter sensitivity as
   the query).
4. **Graded answer policy (D44):** `CAVEAT_PREFIX` constant; SYSTEM_PROMPT rule 3 → four-tier
   coverage policy (direct / partial-with-named-gaps / caveat-form related guidance, every
   statement cited / exact refusal, alone, for genuinely out-of-corpus); rule 4 reworded
   ("legal principle first in the substantive answer"); PROMPT_TEMPLATE human closing updated
   in lockstep (old binary-only wording must be gone). `REFUSAL_PHRASE`, `is_refusal`,
   `CITATION_RE`, the grounding gate: all byte-unchanged (byte-canary test added).
   `evaluate_completeness` strips the one literal `CAVEAT_PREFIX` sentence before
   sentence-splitting (reported metric only; gates nothing).
5. **Eval (D46):** `EVAL_MODES = (hybrid, vector, bm25, hybrid+rewrite)` — swap ALL coupled
   sites: `run_eval_matrix` default `modes` + mode guard (evaluator.py:1305, :1374), CLI
   choices AND `all`-expansion + import (pipeline.py:637-643, :716, :729); shared per-run
   expansion cache (one attempt per unique question feeds retrieval AND generation) +
   `rewrite_fallbacks`/`rewrite_attempts` counters; default `retrieve_fn_factory` handles
   `hybrid+rewrite`; default `generate_fn` expands too (answer passes measure the production
   config); per-question detail keys to the `hybrid+rewrite` row when it ran; `--realistic
   <path>` third set; **both `--skip-*` flags also disable expansion** (the documented
   "both skips = zero API calls" contract stays true on keyed boxes); canonical v3 = existing
   conditions + realistic set answerable + distinct realpaths across sets + all EVAL_MODES +
   expansion attempted with zero fallbacks; explicit `--results` at the canonical path now
   raises on a non-canonical run (was warning); legacy `run_eval` default output moves to the
   partial path; report captions + provenance line (rewrite model, live vs fallback); ci.yml
   gains a third grep asserting the `hybrid+rewrite` row exists (existing two greps
   byte-unchanged).
6. **Embedder (D47):** local-first model load; ONLY cache-miss failures trigger the one
   logged download retry; unrelated failures re-raise.
7. **Realistic set (D46):** `eval/realistic_set.jsonl` (~22 q: real failing queries as seeds,
   messy paraphrases, vocabulary-shifted new questions, ~4 near-domain negatives) +
   `eval/realistic_candidate_review.md` (curation evidence, mirrors the held-out review doc).
   Drafted → **user reviews/refines → freeze** → only then step 8. Existing sets byte-identical.
8. **Canonical re-run (live API, user-approved):** full matrix incl. realistic set + judge;
   `is_canonical=True` writes eval/results.md; README metrics + architecture + limitations
   re-synced.

**Tests:** rewrites=None identity; rewrite-doc enters top-k; weighted fusion — correlated
rewrite lists (0.5) cannot outvote original-arm agreement (adversarial test); id_to_doc union;
parse_rewrites matrix; expand_query degrade paths (no key / API error / parse failure → status,
zero rewrites); suite-wide autouse conftest fixture scrubbing ANTHROPIC_API_KEY (keyed dev box
can never leak a live call through an unpatched seam) plus autouse `expand_query` patches at
BOTH seams (`src.pipeline`, `src.evaluator`); --no-rewrite skips expansion; audit rewrite
fields present/absent + status + env-gated text; CAVEAT_PREFIX in prompt; REFUSAL_PHRASE byte
canary (`== b"not covered in the source material"`); old binary wording absent from the human
template; is_refusal(caveat answer) False; grounding caveat+cited → CITATIONS_VERIFIED;
completeness caveat-prefix exemption; evaluator canonical v3 routing (realistic absent /
duplicate realpaths / fallbacks>0 / zero attempts → partial); one-expansion-per-question cache
count across retrieval+generation; hybrid+rewrite factory passes rewrites; `--realistic` and
`--mode hybrid+rewrite` CLI dispatch; explicit-canonical-path refusal on non-canonical runs;
embedder local-first (local hit / cache-miss retry / unrelated error no-retry). All offline.
**decisions.md:** D43, D44, D45, D46, D47.
**Acceptance:** full suite green; the two named failing queries ("what does unregistered land
mean", "process for registering unregistered land") answered with verified citations in live
spot-checks; near-domain negatives still refuse — criterion amended 15 Jul at the canonical
run: the graded policy (D44) deliberately answers negatives for which the corpus holds
genuinely related transactional guidance, under the explicit caveat sentence with verified
citations. Outcome after the budgeted calibration iteration (D44 addendum): 11/14 exact
refusals (held-out 7/8, realistic 4/6; tuning 5/5); the three residuals (fees, planning,
mortgage-arrears) are all subjects the corpus covers from the transactional angle — live
spot-checks (fees, planning) show caveat-form answers with gate-verified citations, while the
committed report records only the refusal boolean for negative rows (refusal-row detail
reporting is a Phase 14 evaluator addition) — documented in D44 + README rather than
prompt-tuned away (dev-set overfit). Binary 14/14 was a pre-graded-policy criterion;
tier-choice grading is the Phase 14/15 fix that makes this measurable properly; canonical
eval/results.md written with the realistic slice present; keyless CI
green with the two existing smoke greps byte-intact plus the new hybrid+rewrite row grep;
README numbers match the new results.md; CLAUDE.md Commands block updated (canonical command
now includes `--realistic`; both-skips offline contract restated).

## Phase 14 — answer quality: synthesis + intent-level rewriting (Thu 16–Fri 17 Jul, 1.5 days) — `phase-14-answer-quality`

(Provenance: the 15 Jul field test — a comparison question ("purchase vs sale conveyance")
returned caveat-form fragments, not a comparison. Four independent read-only architecture
audits (2 opus agents, 1 sonnet agent, Codex ultra) confirmed two mechanisms: no synthesis
instruction anywhere in SYSTEM_PROMPT, and query expansion that re-words but never re-frames.
Plan gated 15 Jul: plan-auditor NO-GO on the v1 fusion algebra (corrected — the intent-weight
dominance invariant holds only for W ≤ 0.5), Codex ultra 14 findings reconciled. Full gated
plan + reconciliation log: ~/.claude/plans/phase-14-answer-quality.md. Design record:
docs/decisions.md D49–D51.)

1. **Synthesis rule (WS1, D49):** SYSTEM_PROMPT Rule 5 — organize the answer around the
   question; compare/contrast answers state the basis of comparison, address each side, draw
   the explicit contrast; every sentence still carries a bracketed locator; unsupported
   comparison points are rule-3(b) gaps. Human turn reframed to "single coherent response"
   (opener/closing lockstep). CITATIONS_VERIFIED display note made honest (resolution, not
   entailment). REFUSAL_PHRASE/CAVEAT_PREFIX/gate logic/D44 rule-3 text byte-frozen.
2. **Intent-level rewrite (WS2, D50):** rewrite prompt line 4 `INTENT: <restatement>`;
   `extract_intent` pre-pass ahead of a byte-unchanged `parse_rewrites` (no double-count by
   construction; malformed/overlong → None, never a parse error);
   `Expansion.intent_rewrite` appended after `status` with default; casefold dedup. retrieve()
   fuses the intent as its own ranked-list pair on a separate weight budget, W ∈ [0, 0.5]
   enforced by ValueError (equal-rank dominance invariant: 2/61 > (1+2W)/61 ⇔ W ≤ 0.5).
   Threaded: pipeline.query, evaluator rewrite mode, evaluator default generate_fn; legacy
   paths documented raw-query-only. Audit: intent sha256 always (when present), raw text only
   under AUDIT_LOG_RAW_QUERIES=1; intent-None events byte-identical to pre-Phase-14.
3. **Eval-integrity guards (WS3, D51 — canonical v4):** judge pass with zero API/parse errors
   required for canonical; BM25-sidecar-actually-loaded required whenever a default path was
   in play (ownership flags captured pre-mutation, provenance disclosure); retrieve() rejects
   top_k < 1 (no upper cap); refusal rows in the committed report substantiated with
   is_caveat/gate_outcome/citation counts (never answer text — D30).
4. **Quote-snapping: CUT** at the plan gate (both reviewers) — fail-open quote matching is not
   an entailment floor; moved to the Phase 15 backlog with its open design questions.
5. **W sweep (D50):** cached-expansion protocol (47 questions, zero fallbacks), offline fusion
   at W ∈ {0, 0.25, 0.5}. Result: 0.25 = zero effect (no flips, S5 not rescued); 0.5 rescues
   S5 (strict rank 5, realistic 0.529) but relegates one golden control rank 3→7 — the
   zero-golden-regression constraint disqualifies it. **Negative result: no W selected;
   INTENT_LIST_WEIGHT ships at 0.25; the gated fallback acceptance (S5 related@6 HIT + live
   comparison-rubric pass) is engaged.** The 4-line expansion prompt alone lifts golden
   controls 25/30 → 27/30 strict@6 at W=0.

Acceptance (amended per the gated fallback): held-out hybrid 20/20 strict@6 unregressed;
golden controls unregressed vs run #2; S5 related@6 HIT AND the live S5 answer passes the
comparison rubric (basis stated, both sides addressed, explicit contrast, gaps named, correct
tier, citations on every sentence — checked for 2048-token truncation); N4 stays strict HIT;
realistic strict@6 ≥ 0.471; negatives ≥ 15 Jul calibration (11/14, documented residuals);
zero expansion fallbacks; canonical v4 guards green (judge clean, bm25 loaded). Canonical
run #3 results: eval/results.md (this run also re-baselines golden/tuning under the 4-line
expansion prompt).

**Outcome (17 Jul, canonical run #3):** every criterion met EXCEPT the S5 retrieval prong —
S5 is strict+related MISS in the canonical sample (rubric prong: both comparison questions
pass all seven items, evidence in docs/phase14-rubric-spotchecks.md; the spot-check's own
sample retrieved a related section, demonstrating the outcome is expansion-sample-dependent
at W=0.25). Realistic 0.471 passes exactly at threshold. Full record and the accept-vs-W=0.5
disposition: D50 addendum; the disposition is the merge decision.

---

## Phase 15 — retrieval foundation: embedding upgrade + tokenizer-true accounting (Mon 3–Wed 5 Aug, ~2 days) — `phase-15-retrieval-foundation`

(First post-submission phase; the repo shipped as `v2.1.1` on 17 Jul and was untouched until
3 Aug. Origin: the D23 truncation finding, re-measured 3 Aug against the live index —
**67.4% of the 1,470 stored chunks exceed MiniLM's 256-token window** (median 386 BERT-tokens,
p95 713, max 2,640), and the D21 citation prefix is prepended *after* every size check, so it
is uncounted overhead on top of that. Truncation is treated here as a **measured information-
loss defect and a preregistered hypothesis, NOT a demonstrated root cause** (plan-gate finding
C6): measuring the realistic set's expected-section chunks on 3 Aug found 78.3% of the
strict-MISS chunks over the window versus **83.3% of the strict-HIT ones** — truncation does
not discriminate hits from misses, so the expected gain rests primarily on model quality
(MiniLM 42.92 vs candidates 53.9–61.8 MTEB v2 English retrieval) and the bake-off is what
settles it. A second 3 Aug finding caps the instrument itself: golden `1.7.2`, `6.3.2`,
`9.6.1` and realistic `4.8.1.1` (2 rows) expect section numbers no chunk carries — the D20
runt-merge folded each into a neighbour (parents *and* children are indexed) — so **strict@6
was — until the WS0 repair below — structurally ceilinged at golden 0.900 and realistic
0.882** regardless of
model. Also measured the same day: a 512-token window would still truncate 24–38% of chunks
and force re-splitting 36.4% of them (+554 chunks), fragmenting the D4 chunk-equals-numbered-
paragraph unit well beyond the oversize splitting D4 already contemplates;
an 8,192-token window truncates nothing, so the chunker stays byte-frozen and the fix is a
model swap plus honest accounting. Model research 3 Aug (MTEB v2 English retrieval + BEIR
recomputed from the official results repo; licence/gating/`trust_remote_code`/prefix contracts
read from the HF configs). Full gated plan: ~/.claude/plans/lucky-whistling-sunbeam.md.
Bake-off brief: docs/designs/001-bakeoff-embedding-model.md. Design record: docs/decisions.md
D53–D58. **4 Aug diagnosis (D54)** classified the realistic failures into four measured
classes — vocabulary gap (3 deep-misses; the same content retrieves at rank 1–6 under
legal-register phrasing), near-misses at rank 7–19 (5, incl. S5 at raw rank 9), absorbed
labels (the ceiling above), and expansion sampling variance (cached-expansion replay scores
0.588 realistic where the canonical draw scored 0.471, same system) — so the bake-off's job
is specific: recover deep-misses and pull near-misses inside top-6, per class, with zero
regressions. The user set acceptance to **measure-and-disclose** for the canonical metrics on
4 Aug: the recorded expansion variance exceeds any hard bar's effect size on n=17.)

0. **Instrument repair (WS0, D54 — DONE 4 Aug, before any arm build):** the four eval rows
   whose expected section was runt-merged into a neighbour gain the absorbing chunk's label as
   an additional accepted answer (realistic `4.8.1.1`→+`4.8.1` ×2, golden `6.3.2`→+`6.3`,
   `9.6.1`→+`9.6`; golden `1.7.2` deliberately left — it hits via the present `1.7.2.3`).
   Text was never lost; only labels diverged, so this repairs the *instrument*, not the system.
   Post-repair offline baselines (zero API): golden raw-hybrid strict@6 **25/30 = 0.833** (was
   0.800), realistic unchanged **6/17 = 0.353**; structural ceilings lifted to 1.000. These are
   the bake-off's baseline-arm comparison numbers.
1. **Hygiene + dependency pin (WS1, D53):** `.gitignore` gains `chroma_db_arm_*/` and
   `eval/bakeoff/` **before any arm is built** — arm indexes hold the full copyrighted corpus
   text exactly as `chroma_db/` does; the CLAUDE.md Codex do-not-read clause is extended to
   both. `transformers==5.14.1` pinned exactly in requirements.txt (previously transitive at
   5.13.0; ModernBERT-based candidates need ≥4.48, so the floor becomes load-bearing).
2. **Per-model config seam + `EMBEDDING_MODEL` env override (WS2, D55):** `src/embedder.py`
   gains `resolve_embedding_model()` (process env var, else `DEFAULT_EMBEDDING_MODEL`) and
   `MODEL_SPECS: dict[str, EmbeddingModelSpec]` (frozen dataclass: `context_window`,
   `max_seq_length`, `query_prompt`, `doc_prompt`) so bake-off arms run in one checkout.
   `EMBEDDING_MODEL` stays a module constant, so `evaluator.py` import-by-value, provenance,
   the manifest write and `assert_embedding_model`'s default arg keep working unchanged.
   Query-side prompts are **merged** into `query_encode_kwargs`, never substituted
   (langchain-huggingface 1.2.2 `embed_query` *replaces* `encode_kwargs` — a naive prompt
   config would embed queries unnormalized against normalized documents);
   `default_prompt_name` is forced to None so a model's own repo config cannot silently prompt
   documents; `max_seq_length` is set post-construction with a readback assert. All inert for
   MiniLM (byte-equal constructor kwargs, empty `query_encode_kwargs`).
3. **Tokenizer-true accounting: index-time guard + provenance (WS3, D56; lands atomically
   with the winner adoption in item 5, NOT before it — one of the 16 sample-corpus chunks is
   258 tokens, so under MiniLM's 256 window the guard would correctly reject it and break
   `build_sample_index` and CI for the whole interval):**
   `assert_chunks_fit_window()` at a single choke point in `add_documents` and
   `sync_documents`, **plus an explicit preflight in `index_documents` before `if reset:` in
   both branches** — `src/pipeline.py:118` clears the store before `:123` syncs, so a
   sync-only guard would fire after `--reset` had already destroyed the index, leaving nothing
   at all; a failing preflight must reach neither `clear_store` nor `sync_documents`.
   Indexing fails loudly, before any store write, if a stored chunk
   **including its citation prefix** exceeds the configured model's window; the error names
   `section_number`/pages/token count and never chunk text (D30 + copyright).
   `ALLOW_CHUNK_TRUNCATION=1` downgrades it to a warning so a MiniLM rollback re-index stays
   possible. Eval provenance discloses the embedding window and the stored-chunk token
   distribution (n / max / p50 / p95 / over-window). `transformers` is imported lazily behind
   a monkeypatchable seam so the chunker keeps its zero-heavy-imports property and the test
   suite never downloads a tokenizer. **`src/chunker.py` is byte-frozen this phase** —
   `CHARS_PER_TOKEN` and the char thresholds are deliberately unchanged (their blast radius is
   the char-calibrated fixtures, the 16-chunk sample-corpus freeze and the CI greps; deferred
   to the next deliberate re-chunk).
4. **Embedding bake-off (WS4, D57):** three arms + the MiniLM baseline, judged **only** by
   `eval/golden_set.jsonl` (harness rule; the held-out set appears in exactly one command in
   this phase, the WS7 canonical run). Arms: `Alibaba-NLP/gte-modernbert-base`,
   `ibm-granite/granite-embedding-small-english-r2` (cheap arm), `Qwen/Qwen3-Embedding-0.6B`
   (ceiling arm, behind a wall-clock cost gate) — plus a **freshly rebuilt**
   `chroma_db_arm_baseline_minilm` baseline under the pinned Phase 15 stack
   (`ALLOW_CHUNK_TRUNCATION=1`), never the pre-Phase-15 `./chroma_db`, whose documents were
   embedded under a different dependency stack that the model-id-only manifest cannot detect.
   Per-arm precheck asserts normalization,
   window, pooling and prompt config before its index is built; each arm is indexed with
   `--reset` into its own `--persist-dir` (content-hash chunk IDs are model-independent, so a
   non-reset rebuild is a silent no-op that leaves stale vectors under a stale manifest), and
   a 1,470-chunk count is the chunker-byte-freeze canary (arm *completeness* is checked from
   the store's own vector count — the chunker's printed number is model-independent and cannot
   detect a bad build). Offline evals only (`--skip-refusals --skip-completeness` = zero API
   calls). Before any production replay, `scripts/w_sweep.py` gains enforced offline-only cache
   loading, `--persist-dir`, and machine-readable per-question output, so the replay cannot
   spend live API calls. **Selection disqualifies on any golden HIT→MISS versus the rebuilt
   baseline in *either* raw hybrid *or* the cached production configuration** (expansion +
   intent at the shipped W — raw hybrid alone does not measure what ships); among survivors,
   highest golden strict@6, read against the post-repair 1.000 structural ceiling; realistic slice, S5 and
   N4 are diagnostics; ties break to the smaller/faster model. The selection parser is a
   committed, unit-tested script that refuses `--heldout` and emits an arm manifest of input
   sha256s and commands, so held-out exclusion is a property of an artifact rather than
   self-attestation. `scripts/bakeoff_report.py` also computes the Tier-2 per-class
   movement (deep-misses recovered, near-misses pulled into top-6) from the committed class
   roster in the brief — the roster and the computation are build steps, not assumptions
   (round-3 auditor finding #6). Download size and measured per-query embed latency are recorded per arm.
5. **Winner adoption + re-baseline, only if a non-baseline candidate survives (WS5–WS7, D57
   addendum, D58). Ordering is load-bearing and REVERSIBLE-FIRST (round-3 auditor blocker #1
   — nothing destroys the production index until CI has accepted the winner):**
   (a) `DEFAULT_EMBEDDING_MODEL` flips to the winner and the WS3 guard lands with it;
   (b) `scripts/build_sample_index.py` gains `--reset` (without it the model-independent IDs
   leave MiniLM vectors under a stale manifest), `sample_chroma_db/` is regenerated, and the
   three CI smoke greps pass locally; the CI HuggingFace cache key tracks the model name;
   (c) **push the branch and open the PR** — CI triggers only on push-to-main or pull_request,
   so without this step "CI green before the canonical run" is unachievable — and wait for CI
   green (D40: vector floats differ across platforms; a macOS-green/ubuntu-red smoke must
   surface HERE, while everything is still reversible);
   (d) only then the full `--reset` re-index of the production `./chroma_db`;
   (e) the W re-sweep, offline (cached expansions are query-side and index-independent;
   `offline_only=True` makes an incomplete cache raise rather than call the API). **The one
   binding W rule (D58) — D50's recorded rule re-run under the new embeddings:** smallest
   W ∈ {0, 0.25, 0.5} making S5 strict@6 HIT, subject to zero golden-control strict flips vs
   the W=0 control AND N4 remaining HIT; the incumbent 0.25 stands if no W qualifies; any
   change to this rule itself requires its own D58 rationale; if W changes, every surviving
   arm and the baseline are replayed at the final W, the flip lists regenerated, and the
   branch re-pushed for CI before WS7;
   (f) then **one** canonical run writes `eval/results.md`.

**Tests:** env resolution (unset / override / whitespace / unknown-model warning) and a guard
test pinning `EMBEDDING_MODEL == DEFAULT_EMBEDDING_MODEL` so a polluted shell fails legibly;
MiniLM inertness canary (constructor kwargs byte-equal to today, `query_encode_kwargs` empty);
query-prompt merge canary (normalization survives alongside the prompt); every known-model
`MODEL_SPECS` entry enforces `max_seq_length == context_window` (a mismatch raises, with a
mismatch test — provenance must not report zero over-window while the client truncates
earlier); `_apply_model_config`
set / readback-raise / `default_prompt_name` / no-`_client` no-op; context-window resolution
(spec / tokenizer / sentinel raises); tokenizer loader local-first, cache-miss retry,
unrelated-error propagation, lru reuse; token counting applies the same newline normalization
langchain applies; guard passes, raises with the prefix counted, writes nothing to the store
when it raises, honours the escape hatch, and keeps chunk text out of its message; provenance
carries the token stats and degrades to "unavailable" without losing `chunk_count`;
`build_sample_index(reset=True)` re-adds 16 chunks while the existing idempotence test stays
green. All offline — models and tokenizers mocked, no network, no API key.

**decisions.md:** D53, D54 (landed at phase start), D55, D56, D57 (+ addendum), D58.

**Acceptance (two tiers, per the 4 Aug user decision — D54):**

*Tier 1 — HARD GATES (deterministic, each fails the phase):* full suite green
(`python -m pytest tests/ -q`); the winning arm has **zero** per-question golden HIT→MISS
flips vs the rebuilt baseline arm in **both** raw-hybrid and the cached production-config
replay, with both flip lists committed to the bake-off brief; a failing preflight leaves the
index intact — neither `clear_store` nor `sync_documents` is reached (targeted pytest,
finding C1); provenance names the new model, its window, the stored-chunk token distribution
and over-window = 0; sample-corpus CI smoke still `strict hit@6 = 7/7 = 1.000` with both row
greps byte-intact, green **on CI** before the canonical run is spent; CI cache key names the
winner; `transformers==5.14.1` in requirements.txt and `pip freeze`; nothing corpus-bearing
tracked (`git check-ignore` on a probe path *inside* **each** arm dir and `eval/bakeoff/` —
a bare name gives a false negative, finding A1 — plus `git status --porcelain` showing no
untracked corpus-bearing path; a `grep -v '^??'` filter would discard exactly the
`?? chroma_db_arm_*/` line a gitignore failure produces, round-2 finding #10); D53–D58 present
and the decisions.md current-phase header reads 15; ABOUT.md discloses download size and
measured **p50** query-embed latency. *(Freshness precondition on every report-reading check:
the report's sha is reachable from HEAD, that commit contains `assert_chunks_fit_window`, and
the embedding-model line names the exact winner — a label-existence grep passes on the July
report, and so does "a sha on this branch", since the July commit is an ancestor of it,
finding A5.)*

*Tier 2 — MEASURED AND DISCLOSED (reported in eval/results.md, the brief's Outcome, and the
PR; NOT pass/fail — D50's recorded expansion variance exceeds any hard bar's effect on n=17,
and the canonical run is sampled once):* realistic strict@6 on the canonical hybrid+rewrite
row vs the 0.471 July figure (post-repair offline baseline: raw-hybrid 0.353; the
cached-expansion replay of the July system scored 0.588, bounding the sampling noise);
held-out strict@6 on both rows vs 20/20 and 0.950; negatives vs 11/14; **per-failure-class
movement from the D54 diagnosis** — vocabulary-gap deep-misses recovered (of 3), near-misses
pulled into top-6 (of 5, S5 among them); S5/N4 both-role-group coverage by equal-or-descendant
match (`s == group or s.startswith(group + ".")` for each of 2.2.1 and 2.2.2 — not the
evaluator's existential flag, finding C3, and not symmetric "related", which one generic
parent `2.2` would satisfy, round-2 finding) plus the S5 answer against the Phase 14
comparison rubric; canonical v4 guard status. **Any Tier-2 degradation vs the July headline
is presented as an explicit disposition in the PR body (the D50-addendum precedent) — the
merge decision is the user's.**

**If no non-baseline candidate survives Tier-1 selection, the outcome is "no model swap":
record the negative result and STOP for user disposition before landing the guard or
rebuilding the sample index** — under MiniLM's 256-token window the guard cannot land without
a standing truncation exception, so whether to widen the bracket or adopt such a policy is the
user's call, not the implementer's. The pin, hygiene and instrument-repair work stands either
way.

**Outcome (closed 24 Aug 2026):** **NEGATIVE per D57 — no model swap.** No candidate survived
Tier-1 selection (gte-modernbert-base and granite-small-english-r2 each flipped golden controls in
raw hybrid *and* the cached production-config replay; Qwen3-Embedding-0.6B was cost-disqualified at
the wall-clock gate), so the pre-registered no-swap branch applies and `all-MiniLM-L6-v2` remains
the production embedder. **MOOTED:** WS5–WS8 and every winner-conditional item — winner adoption,
the sample-index regeneration, the CI cache-key change, the production re-index, the D58 W
re-sweep, the canonical run, and the ABOUT.md download-size/p50-latency disclosure that belonged to
the adopted model. **WS3 guard NOT landed:** no truncation-exception policy was adopted — the user
chose accept-&-close on 24 Aug — and under MiniLM's 256-token window the guard cannot land without
one (D56/D58 record both reserved numbers as retired). **What stands:** the D53 pin and hygiene, the
D54 diagnosis and instrument repair, the D55 model-config seam, and the committed bake-off
instruments (`scripts/bakeoff_report.py` incl. its `compare_prod_ranks` production-config flip
check, `scripts/w_sweep.py`, `scripts/embed_latency.py`) with their tests. The **re-open condition**
for the embedding bracket is recorded in the
D57 addendum (revisit only after the Phase 16 question-set expansion supplies statistical power).
Full arm table, flip lists and the post-verdict mechanism probe:
docs/designs/001-bakeoff-embedding-model.md §Outcome.

---

## Integrity hotfix (H) — `integrity-hotfix` → `v2.2.1` (spec v3, 9 Oct 2026)

**Authority:** `docs/designs/003-roadmap-and-next-actions.md` §3, with the owner's answers
in §1.2 (Q1/Q2), and ledger **D61** (on this branch).
- **D62 is written during implementation**, not before it. It records the decisions below,
  and the plan-gate rounds are reconciled in 003 `## Review → Integrity hotfix plan gate`.
- **Deliberate divergences from 003 §3, superseding it:**
  - **H2 is display-only.** 003 said "downgrade-only". Display-only is chosen for three
    reasons: D38/D44 keep sentence coverage descriptive; the owner's Q2 answer keeps the
    `PARTIALLY_VERIFIED` display; and claim-level downgrades belong with the entailment pass
    in Phase 17a, where they can be measured.
  - **003's H5 "scope policy in the system prompt" is dropped.** Q1 made today's prompt *be*
    Handbook mode, so there's nothing to change in the prompt. H5 becomes a source label plus
    a disclaimer.

**Scope:** Handbook mode only. **Retrieval is untouched.**

**Evidence:**
- The CLI audit log (67 real queries; eval traffic is excluded by D36) has a maximum answer of
  5,639 chars (~1.4k tokens), under the 2,048-token cap.
- A live probe (9 Oct) through `ChatPromptTemplate | ChatAnthropic` (`claude-sonnet-5`,
  langchain-anthropic 1.4.8, langchain-core 1.4.8, anthropic 0.116.0) returns
  `response_metadata["stop_reason"]` = `max_tokens` / `end_turn`, plus `stop_details`.

### Work
1. **H1 — generation status, set inside `generate_with_sources`, so every consumer gets it
   (D35's reasoning).**
   - `generate()` invokes `PROMPT_TEMPLATE | llm`, without `StrOutputParser`.
   - **Text:** `.content` if it's a str; otherwise the joined `text` blocks (an empty list
     gives "").
   - **Status mapping:**

     | `stop_reason` | `generation_status` |
     |---|---|
     | `end_turn`, `stop_sequence` | `complete` |
     | `max_tokens`, `model_context_window_exceeded` | `truncated` |
     | `refusal` | `declined` |
     | any other value | `incomplete` |
     | absent or None in the response metadata | `unknown` |

   - The return gets two new keys, `generation_status` and `stop_reason`; the existing keys
     are unchanged.
   - **`max_tokens` stays 2048.** No answer has come near it, and raising it would hit D52's
     120 s timeout sizing and the judge, which shares `get_llm`. Revisit in Phase 19.
   - **Pin the transitive dependencies** the metadata path depends on, by the D53 precedent:
     `langchain-core==1.4.8`, `anthropic==0.116.0`. These are already installed versions,
     not new dependencies.
2. **H1b — terminal outcomes take precedence (extends D35).**
   - `generate_with_sources` calls a new pure `grounding.generation_outcome(status)` **before**
     `classify`, and sets `gate_outcome` from it:

     | Status | Outcome |
     |---|---|
     | `truncated` | `ANSWER_TRUNCATED` |
     | `declined` | `MODEL_DECLINED` |
     | `incomplete` | `GENERATION_INCOMPLETE` |
     | `complete` or `unknown` | `classify` result, unchanged for existing inputs |

   - Terminal outcomes beat every citation outcome, the legacy `outcome is None` fallback and
     `--show-unverified`.
   - An exact refusal sentence carrying a terminal status → the terminal outcome.
   - **A result dict with no `generation_status` key** (legacy mocks or fakes) is treated as
     `unknown`. The **legacy `outcome is None` display path is exempt** from the "could not
     be confirmed" line, keeping D35's exact v1 display there.
3. **H1c — evaluator accounting.**
   - **Status reaches the refusal scorer.** `evaluate_refusals`' `answer_fn` may return
     either a str (legacy, meaning status `unknown`) or `{"answer", "generation_status"}`.
     Run_eval_matrix's `_answer_fn`, `evaluate_refusals`' default and legacy `run_eval`'s
     default return the dict.
   - **One normaliser** maps a result to (text, status) for every scorer.
   - `generate_answers` records the status per row, and does **not retry** on a returned
     status.
   - Rows with `truncated`/`declined`/`incomplete` are counted per set and in aggregate as
     `generation_incomplete`, broken down by status. They are **excluded** from completeness,
     the judge and the refusal-accuracy denominators, and reported on their own line.
   - **`GATE_OUTCOMES` and the completeness distribution are unchanged**: excluded rows never
     enter them.
   - `unknown` is counted and reported.
   - **Canonical v5:** the canonical guard additionally requires
     `generation_incomplete == 0` **and** `unknown == 0`. Bump the report title constant
     (src/evaluator.py ~L1990–1995) and its test pin (tests/test_evaluator.py ~L2711) to
     **v5**, and update CLAUDE.md's canonical condition list completely (incl. D51's
     judge/BM25 guards).
   - The committed `eval/results.md` stays a v4 report until the next canonical run, which is
     stated in D62.
   - **Fixture migration:** canonical-true evaluator fixtures get explicit
     `generation_status: "complete"`. Missing metadata is never treated as complete.
4. **H2 — uncited-statement hint (display-only heuristic; evaluator code untouched).**
   - **Steps:**
     1. Strip one leading exact `CAVEAT_PREFIX`, as the evaluator does.
     2. Flag every *remaining* exact `CAVEAT_PREFIX` occurrence as "repeated caveat".
     3. Split with `evaluator.split_sentences` (imported, unmodified).
   - **A unit is flagged** when it has no locator, is ≥5 words, and doesn't end with `:`.
   - **Gap-statement exemption** (narrow): a unit is exempt only if it **starts with** one of
     "The extracts do not", "The source material does not", "This is not covered", "The
     handbook does not" **and** contains none of "but", "however", "likely", "probably",
     "generally", "usually".
   - A whole-answer refusal flags nothing.
   - **Display heading:** "These statements may not be backed by a citation (heuristic):".
   - The count is audited; the text never is. The outcome never changes.
   - **Documented misses:** lowercase starts, quotes, and the exemption's own blind spots.
5. **H3 — one rendering contract (`src/render.py`).**
   - `render(result, flags) -> Rendered(display_text, action, public_result)`.
   - **`public_result` keeps D35's uniform key set** on every path: `answer`,
     `gate_outcome`, `citations`, `sources`, `citation_check`, `source_documents`,
     `answer_chars`, plus the new `generation_status`, `stop_reason`, `uncited_count`.
   - **For blocked and terminal outcomes**, `answer` is a **synthesised safe notice**, never
     the draft. `citations`/`citation_check` follow D35's blocked allowlist. No uncited
     sentences appear.
   - **Matrix:** no_results × legacy None × the four citation outcomes × the three terminal
     outcomes × `show_unverified` on/off × uncited on/off × status `unknown`/`complete`.
   - The verbose chunk-score print stays before rendering.
6. **H4 — honest wording, on every surface that states the outcome set or the verification
   claim.**
   - README: L6, L12–14, L37 (alt text), L40–47 (outcome table → the full set), L49, L84.
   - `Demo/demo.html`: L138, L198–200, L393.
   - ABOUT.md: L36–39.
   - `docs/diagrams/user-journey*.mmd`: add the terminal outcomes, and re-render the SVGs
     with `npx -y @mermaid-js/mermaid-cli`. That's a dev-time tool, not a project dependency.
     If it's unavailable, the caption says "simplified — full outcome table in README".
   - `docs/v1-v2-comparison.md`: a dated correction note.
   - CLAUDE.md: canonical v5 (H1c).
7. **H5 — source label + disclaimer (display-only), for outcomes VERIFIED, PARTIAL and the
   override draft only.**
   - Prefix `Source:` + the sorted unique `metadata["title"]` of the chunks matched by
     **verified** citations, falling back to the retrieved chunks.
   - Suffix: "Research aid — check the cited paragraphs; not legal advice; the source edition
     may predate current law."
   - It lives in `display_text` only. Raw `answer`, `answer_chars`, refusal matching, caveat
     detection, citation extraction and H2 never see it.
   - **No mode flag in H.** No mode selection exists yet. Research mode's CLI shape is
     decided in Phase 19.
8. **H6 — audit (extends D36).**
   - New actions: `withheld_truncated`, `withheld_declined`, `withheld_incomplete`.
   - New always-present fields:
     - `stop_reason` (str|null);
     - `generation_status`, which is `"not_run"` on the no-results path;
     - `uncited_count` (int, 0 when not computed).
   - Exactly one event per query.
   - No answer text, uncited sentences or message objects, even with
     `AUDIT_LOG_RAW_QUERIES=1`.
   - Propagate through `_write_audit` / `build_event`, and update `tests/test_audit.py`
     `EXPECTED_KEYS`.
9. **Ledger:** D62, covering:
   - the decisions above;
   - the divergences from 003;
   - `max_tokens` kept at 2048;
   - canonical v5;
   - the dependency pins.

   Also update decisions.md's `Next:` and `Current phase` lines.

### Acceptance (Tier-1)
- (a) The full suite is green.
- (b) **Real-path status:** a fake `BaseChatModel` returns a `ChatResult` whose `llm_output`
  carries `stop_reason`, through the real `PROMPT_TEMPLATE | llm` seam, for:
  - `end_turn`;
  - `max_tokens`;
  - `model_context_window_exceeded`;
  - `refusal` with empty list content;
  - `pause_turn`;
  - absent.
- (c) **Leak tests:**
  - sentinel draft and sentinel uncited text never appear in stdout, in `query()`'s returned
    dict or in the audit line;
  - for every terminal outcome **and** for shown outcomes (audit line);
  - with `show_unverified` on/off and with `AUDIT_LOG_RAW_QUERIES=1`.
- (d) **Precedence:** an exact refusal sentence with each terminal status gives the terminal
  outcome; a legacy mock without status shows the v1 display unchanged.
- (e) **Evaluator regression (projection):**
  - Before any code edit, a script run on `main` writes
    `tests/fixtures/h_eval_projection_main.json`, recording `main`'s SHA.
  - It captures, per listed fixture: strict/related ranks, sentence-coverage numerators and
    denominators per row, completeness aggregates, refusal-accuracy counts and judge input
    texts.
  - The fixtures are the evaluator tests the plan names in D62's appendix: the completeness,
    refusal and matrix fixtures.
  - Afterwards the same projection must be identical. New fields (status, incomplete counts)
    are asserted separately.
  - New tests:
    - incomplete rows, on answerable and refusal-type rows;
    - per-set and aggregate counts;
    - exclusion from completeness, the judge and refusal denominators;
    - no retry;
    - partial-report routing;
    - canonical rejection when either count is non-zero;
    - the v5 title.
- (f) **Render matrix:** every combination in H3, asserting `public_result` keys and the
  notice text.
- (g) **H2 fixtures:**
  - cited, uncited, heading, list lead-in;
  - a narrow gap statement (exempt);
  - D32's hedge "This is not covered in the source material, but the likely answer is 20
    days." (**flagged**);
  - "Defects not covered by the warranty remain the vendor's risk." (**flagged**);
  - leading caveat (stripped);
  - repeated caveat (flagged as "repeated caveat");
  - refusal (nothing flagged);
  - a lowercase-start miss, documented.
- (h) **H5:**
  - the label equals the titles of the chunks behind verified citations (or of the retrieved
    chunks if none);
  - sample-index and legislation fixtures are covered;
  - the decoration is absent from `answer`/`answer_chars`;
  - refusal matching is unchanged.
- (i) **Audit:** `EXPECTED_KEYS`, values per path (incl. `not_run`) and the three new
  actions.
- (j) **Retrieval regression canary:**
  - Before implementation, the offline eval runs on `main` against `./chroma_db`, and its
    output is stored locally at `data/research/h_offline_baseline_main.md` with the SHA.
  - After implementation, the same command produces identical retrieval ablation rows.
  - This is a canary for accidental retrieval edits only; H's own coverage is (b)–(i).
  - A keyed-environment fixture asserts zero model calls offline.
- (k) D62 is written; the `Next:` and `Current phase` lines are updated; 003's `## Review`
  carries the plan-gate reconciliation.

### Tier-2
- A local-only live check of 5 questions: direct, lay-phrased, comparison, out-of-scope,
  long multi-part.
- The PR records **metadata only**: outcome, status, citation and uncited counts,
  `answer_chars`. **No answer text in the PR or the repo.**
- Forced truncation is covered by the Tier-1 fakes and the 9 Oct live probe. No new
  `max_tokens` override is added.

### Gates
1. Round-3 plan gate.
2. Implement.
3. Gate steps per `.claude/skills/phase-gate`, run against this section (invoked as "H").
4. Codex merge review.
5. PR, then CI, then the owner's "go", then tag `v2.2.1`.

### Spec v3.1 amendments (round-3 gate, 9 Oct; both legs found no BLOCKER; Codex found no MAJOR)
These amendments override v3 where they conflict.

1. **H0: pre-implementation captures (a new first step).**
   - `pytest --collect-only` of the evaluator completeness, refusal and matrix tests is written,
     together with `main`'s SHA, to `tests/fixtures/h_projection_manifest.txt`.
   - `scripts/h_capture_projection.py` (committed) writes `tests/fixtures/h_eval_projection_main.json`
     for exactly those test IDs, run on the `main` SHA.
   - The after-comparison is a suite test, `tests/test_h_projection.py`.
   - (j)'s offline baseline is captured by the orchestrator.
2. **Exact texts** (display and public `answer`):
   - `unknown` (non-legacy, shown outcomes): "⚠ Completion status could not be confirmed (no
     stop reason returned) — check this answer with extra care." The **legacy path never shows
     it** (asserted both ways).
   - `ANSWER_TRUNCATED`: "WITHHELD — ANSWER INCOMPLETE: the answer was cut off before it was
     complete and has been withheld. Try a narrower question."
   - `MODEL_DECLINED`: "WITHHELD — the model declined to answer this request. Rephrase the
     question or consult the handbook directly."
   - `GENERATION_INCOMPLETE`: "WITHHELD — answer generation did not complete normally and the
     answer has been withheld. Please retry."
   - Terminal outcomes print **no sources and no `--show-unverified` hint**.
3. **Where H2 and H5 apply:**
   - H2 computes and shows only for VERIFIED, PARTIAL and the override draft. `uncited_count`
     is an int there and **`null`** everywhere else (legacy path, refusal, blocked, terminal,
     no_results).
   - H5's `Source:` label goes on **VERIFIED and PARTIAL only**, built from the
     verified-citation chunks' titles prettified (extension stripped, `_` → space). The
     override draft keeps its existing "UNVERIFIED DRAFT" branding with no Source label; the
     disclaimer still applies.
4. **The render matrix covers reachable states only.** It's an explicit table:

   | Path | Status | Uncited |
   |---|---|---|
   | no_results | — | — |
   | legacy None | — | — |
   | REFUSAL | complete / unknown | — |
   | VERIFIED, PARTIAL | complete / unknown | uncited present / absent |
   | BLOCKED | complete / unknown | — (with override on/off) |
   | each terminal outcome | — | — (with override on/off) |

5. **H4 additions:** `docs/diagrams/pipeline-steps.mmd` (its `OUT` node) and the
   `src/grounding.py` module docstring. Re-render with the repo's
   `docs/diagrams/mmdc-config.json`, both themes, and an exact `@mermaid-js/mermaid-cli`
   version pinned in the command. The `.mmd` sources are the record. If rendering is
   unavailable, the PR leaves the SVGs unchanged and opens a follow-up issue; there's no
   caption fallback.
6. **H2 precision:**
   - Markdown heading lines (`#…`) are exempt.
   - Hedge words are matched as **whole words** (`\b…\b`).
   - The "heading" fixture expects *not flagged*.
7. **H1c precision:**
   - Generation-error rows get status `error` (counted only in `generation_errors`, never as
     `unknown`).
   - The eval report adds per-set `answer_chars` max/p95 (metadata), so the next run measures
     eval answer lengths.
   - If canonical v5 is ever blocked by truncation, a follow-up raises `max_tokens` together
     with D52 timeout sizing.
   - Correct wording: the committed `eval/results.md` is the **"Report v3"-titled 17 Jul
     run**. It stays the record until the next canonical (v5) run.
8. **(b) additions:** `stop_sequence` and an explicit `None`. The "no retry" test is kept as a
   regression lock, and acknowledged to pass today.
9. **(j) is orchestrator-verified.** The pressure-tester can't read `data/` or `chroma_db/`.
   Its gate verdict for (j) cites the orchestrator's recorded comparison.
10. **Tier-2:** live questions are **not** taken from the held-out set, and the PR omits
    question text too.
11. **003 `## Review`:** gets a per-round finding → disposition line. All findings are
    ACCEPTED; none are rebutted. Round-3 #8 (divergences from the approved 003 §3) is
    **escalated to the owner for sign-off before implementation.**
12. **Owner sign-off (9 Oct 2026):** all three divergences are approved — H2 display-only,
    H5 as source label plus disclaimer (no system-prompt change), and the (j) offline check
    narrowed to a retrieval-row canary. Implementation may start. Same date: GitHub branch
    protection is on for `main` (no force-push, no deletion, admins included; harness
    Layer 3).

## Phase 16A — eval foundations — `phase-16a1-eval-instrument` → `v2.3.0` (spec v4.1, 10 Oct 2026: rescoped after plan-gate round 3, round-4 findings fixed; 16A-1 fully specified, 16A-2 outlined for its own gate; dispositions at the end)

**Authority:** design 003 §5 16A (normative), §1.2 (Q1, Q3, Q7); astra B3, M8–M10; D54; D57 addendum; D59; D61; D62;
D64. **v4 rescope (orchestrator, binding):** three gate rounds failed, mostly on the sealed-set data protocol, which
needs owner answers not yet given. So **16A-1, the instrument, is fully specified here and reviewable alone**, with
`sealed` only a fail-closed refusal; trusted sealed processors, slots, the signed core, the slot ledger, sealed
rendering and slot replay move to **16A-2, outlined below for its own spec and plan gate**. **Ordering (003 16A.1):**
no private question enters an eval run, cache or judge call before 16A-1 merges; no new private eval data is created
before 16A-2's gate passes (the 24 existing tutorial rows are development data, 003 16A.3: P2). **Lanes:** **[C]**
Claude only (corpus, `chroma_db/`, real eval rows of any class, tutorial content, every registry sha256, judgment,
integration, canaries, commits with `Implemented-by:`). **[W]** R0 code on **synthetic fixtures only**, via
`harness-worker` once B2's escape probes pass and OS-1's `worker_allow` is applied (else Claude); `worker_allow` =
`src/{eval_sets,eval_schema,eval_privacy,eval_scoring,eval_split}.py`,
`src/{eval_stats,expansion_artifact,spend,text_utils,chunker}.py`, `scripts/{scan_leaks,validate_eval_set}.py`,
`tests/test_p16w_*.py`, `tests/fixtures/p16w_*`, plus read-only import dependencies `src/{generator,grounding,
render}.py` and `tests/conftest.py`; workers never get real eval rows (public v1 included), files quoting
them, `eval/` or report output. **D64 triggers in 16A-1:** the weekly spend cap, reached or to be lifted (item 8; a
self-imposed run-limit refusal is an ordinary failed run); a private-data hit in pushed history (item 1); the OS-1
manifest refresh (owner-only).

**OS-1 — owner questions** (sent at 16A-1's start; non-blocking for 16A-1, **blocking for 16A-2's spec**):
1. *Manifest:* "Please review the `.harness/project.toml` diff in the 16A-1 PR (`^eval/private/` → `never_commit`;
   `eval/private` → `restricted_read`; the do-not-read clause → `codex_clause`; [W] globs → `worker_allow`), then run
   `~/ClaudeCode/harness/bin/harness-init /Users/malik26/ClaudeCode/rag-pipeline-v1 --refresh` with
   `phase-16a1-eval-instrument` checked out." (Plain `harness-init` is a no-op once enrolled, `:896`; `--refresh`
   adopts the running work tree's manifest, `:935–953`, so not `main`'s; `harness-doctor` then shows no drift.)
2. *Tutorials:* "Which tutorials beyond T1 and T7 may 16A-2 draft from?"
3. *Colleague queries:* "Can colleagues' real queries (client identifiers removed) feed the realistic slice? How many, by when?"
4. *Source mix:* "Default: ≥ 25% of realistic families from tutorial or colleague sources if available, else lay
   phrasings Claude drafts, with the D46 self-retrieval caveat. Approve or set a minimum; does sealed need one?"
5. *Second reviewer:* "Who gives Q3's second opinion on refusal and ambiguous labels, and by when? If nobody can, do
   you review alone with the gap disclosed, or does 16A-2 wait?"
6. *Forwarding:* "May handbook- or tutorial-derived eval material (Q7: tutorial material is private) go to that
   colleague? It is a licensing and data-protection position (D64 hard stop); nothing leaves the project until then."

### 16A-1 — instrument (fully agentic)
**Evidence (`main` 9ce4e07; bare numbers are `src/evaluator.py`):** question text reaches both reports (`:1402–1413`,
`:2549`, `:2572`), the judge dump (`src/judge.py:271`), the expansion cache (`:1711–1724`), w_sweep prefixes
(`scripts/w_sweep.py:84`) and bake-off keys (`scripts/bakeoff_report.py:487–508`); the loader keeps three fields
(`:205–210`); matching is OR-only (`:397`); held-out branches key on the label (`:2042–2048`); no usage capture
(`src/judge.py:103`); SDK retries (`src/generator.py:167`, `src/query_rewrite.py:173`); 59 `run_eval*` test calls.

### Work
0. **P0 — captures before any code edit [C]** (H0 precedent). `tests/p16_capture.py` (an uncollected driver) sets
   `PYTHON_DOTENV_DISABLED=1` (python-dotenv 1.2.2) and drops `ANTHROPIC_API_KEY` before importing `src` from
   `--repo`, makes the `anthropic` and `ChatAnthropic` client constructors raise, fakes retrieval (fixed ranked chunk lists from a committed synthetic fixture; SentenceTransformer and Chroma
   constructors patched to raise) and every model seam (generation, judge, `expand_query` taking any keywords; a fake meter where the runner takes `meter=`) and runs both v5
   formatters over public golden, realistic, sample and the invented `tests/fixtures/p16_synthetic_heldout.jsonl`
   labelled `held-out`: an offline and a canonical-shaped run (fakes meet every v5 guard; tmp results path; clock,
   paths, provenance injected) plus one run per other branch (generation error, incomplete and unknown rows, judge
   suppressed, no held-out set, a legacy `excluded (<status>)` row), passing `privacy="public"` only where the runner
   has it; on a scratch worktree of `9ce4e07` → `tests/fixtures/p16_v5_projection_main.json` ((d)'s lock runs it
   in-process). H (j)'s offline rows go to `data/research/p16_offline_baseline_main.md`.
1. **Privacy classes and output control (D65):** `public`, `private`, and `sealed` (16A-1: refusal only).
   - **[W] Registry** `src/eval_sets.py` + `eval/sets.json` ([C]): `name`, `path`, `privacy` (`public`/`private`),
     `role` (`tuning`/`realistic`/`regression`/`fixture`), `status`, `sha256`; at merge, public v1 golden, realistic,
     sample, heldout, synthetic fixtures, Tier-2's v2 copies; no private set; sealed or marked files refused.
   - **[W] `classify(path)`**, resolved path, first match wins: (1) under `eval/private/sealed/` → sealed; (2) a
     `.json`/`.jsonl` eval input with any parsed row (or a `.json` file's top-level object) holding `"sealed": true`,
     or any unparseable line containing `"sealed"` (fail closed) → sealed, wherever it lives (copies, renames,
     reorders, subsets, reserialisations keep row markers); (3) under `eval/private/` → private; (4) a registered
     public path at its registered sha256 → public; (5) a report, cache or artifact whose recorded input sha256s are
     all registered public → public; (6) with `--legacy-public` (it enables this lookup, never sets a class), a sha256
     in `eval/legacy_public.json` ([C], frozen: `{"version": 1, "entries": [{"path", "sha256"}]}` for the committed
     `eval/w_sweep_expansions_20260717.json` and pre-16A `eval/bakeoff/` artifacts) → public; (7) else private. Only
     eval inputs are ever classified. Without the flag, `w_sweep` and `bakeoff_report --prod-ranks` (both open the
     0717 cache) floor to private and write under `eval/private/` (disclosed in D65).
   - **[C] Privacy floor:** `run_eval`, `run_eval_matrix`, both formatters and the `w_sweep` and `bakeoff_report`
     entry functions take a keyword-only `privacy`, no default. A runner derives its own floor (the strictest
     `classify` over every path it opens); a weaker caller value raises `PrivacyFloorError` before any retrieval,
     expansion or model call, a stronger one is honoured, and formatters refuse a class weaker than the result's. CLIs
     pass `classify`'s result; tests pass `privacy="public"` and register tmp sets via a `tests/conftest.py` fixture
     patching the registry loader (59 calls edited); AST tests keep that literal and any registry, classifier or
     class-setting parameter out of `src/` and `scripts/`. The private root, `src.eval_privacy.private_root()`
     (`eval/private/`), moves only when a conftest fixture monkeypatches it to `tmp_path/eval/private`.
   - **[C] Sealed refusal** (before any retrieval, expansion or model call; CLI exit 4), complete for 16A-1:
     `pipeline eval`, `run_eval`, `run_eval_matrix`, `load_golden_set`, `eval_schema.load_v2`, `validate_eval_set.py`,
     `w_sweep.py`, `bakeoff_report.py`, `scan_leaks.py` (as a source) and the expansion-artifact loader.
   - **[W] Serialiser** (`src/eval_privacy.py`, allowlist), private runs: stdout, stderr and logging carry aggregates
     and opaque ids only; the private report adds section ids, evidence and ranks; nothing outside `eval/private/`
     carries a question, gap keyword, prefix, raw malformed value, `str(exc)` or model-derived text (rewrites, intent,
     answers, judge claims). Private v1 ids are `q:` + sha256(set sha256 ‖ question)[:12], salted by the file's own
     hash (kept under `eval/private/`), so candidate text cannot confirm membership without that hash. **Residual (D65):** once a private set's sha256
     is registered in the committed `eval/sets.json`, anyone holding candidate text can test membership; 16A-2 (P4)
     decides whether private registry entries carry a separate secret salt. **[C] Destinations:** a private
     run writes reports, detail, judge dumps, caches and rank dumps only under `eval/private/runs/<run id>/` (with
     `inputs.json`: each private input's path, sha256 and `kind` — `questions` for an eval set, `derived` for a cache, dump or artifact, which also lists the question sets it was built from), artifacts under `eval/private/artifacts/` (resolved-path
     containment; symlink and `..` escapes refused; never `eval/results.md`); errors print as id + exception type.
   - **[W] Leak scanner** `scripts/scan_leaks.py`, never printing a needle. *Sources (explicit):* registry `private`
     sets, `--needles <path>` (a v1 or v2 eval JSONL; must classify private) and, in `--output` mode, the run's own
     private inputs; public and sealed sources are refused (empty until 16A-2). *Needles* (JSON and `\uXXXX` escapes
     decoded, then NFKC, casefold, punctuation → space, whitespace collapsed; targets alike): each private question
     whole and each 8-token window; a whole question is exempt only if its normalised sha256 equals a registered
     public question's (`source` grants nothing), a window only if it occurs in one; under 8 tokens only the whole
     needle exists. **Promise:** every non-exempt whole question and window is caught; shorter fragments are the
     serialiser's job ((b)). *Modes:* `--output` (a private run's stdout, stderr, captured logging and report before
     release; a hit aborts, exit 5); `--merge-gate --base <ref>` (tracked files at HEAD, **every blob version added or
     modified in `base..HEAD`**, commit messages, tags, PR body, comments, review comments), which first refuses (exit
     7, paths only) if a file under `eval/private/` outside `runs/` and `artifacts/`, or a `questions` input (or a
     `derived` input's listed question set) in any `runs/*/inputs.json`, is not a needle source; entries in
     `eval/legacy_public.json` and sha256-keyed artifacts with no question text are exempt (their sources are checked). *Hits* give target (file:line, commit or PR item), needle id (`n` +
     10 hex of its sha256), kind and source, never text; the orchestrator may open that tracked file (private, not
     sealed, content is readable by Claude) and fix it, rewriting unpushed history. With private sets present,
     `--merge-gate` runs before every push (CI cannot); a hit in pushed history is a data incident (D64 owner stop).
   - **[C] Hygiene and rules:** `.gitignore`, `check_never_commit.py` and CLAUDE.md's do-not-read clause gain
     `eval/private/` (extends D63); CLAUDE.md gains the private-output and sealed-refusal rules. **Residual (D65):** a
     stripped marker, a direct `chromadb` import, a skipped pre-push scan or a deleted `runs/<id>/` goes undetected
     (instruction-enforced).
2. **Schema v2 (D66) [W]:** `src/eval_schema.py` + `scripts/validate_eval_set.py` (errors by line and field, never
   text); `"schema": 2` on every row (JSONL has no header line), no mixed files. **Identity:** `id` unique per set;
   `family_id` = a scenario and its paraphrases; public ids `^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$`; non-public
   `family_id` `^f[0-9a-f]{8}$`, `id` `<family_id>-<n>`, nothing else. **`scope`** `answer`|`partial`|`refuse` (D61);
   **`evidence`:** AND groups of non-empty OR section lists, `[]` iff `refuse`; **`gaps`:** `partial` only, non-empty,
   each `{"id", "keywords"}` with ≥ 1 non-empty keyword. **Optional:** `type` (agrees with `scope`), `register`
   (`modelled`/`lay`/`terse`), `source` (`handbook`/`tutorial`/`colleague`/`legacy`), `ambiguous`, `owner_status`
   (`draft`/`approved`/`changed`/`rejected`), `second_status` (`not_required`/`pending`/`agree`/`disagree`/`missing`);
   when a second review is required is 16A-2's rule (P3), following 003 §5 16A.4 unnarrowed. **Inventory:** each
   evidence section must be in `eval/section_inventory.json` (item 7), else a line/field error (the D54 class). **v1
   rows load as today** (`load_golden_set` byte-equal); a public v1 `id = "q:" + sha256(question)[:12]` (a duplicate
   question makes C4 refuse the set; none in public v1), a private v1 id is salted (item 1), `family_id = id`.
3. **Scoring and report v6 (D66).** First commit: `split_sentences`, `_GAP_STARTS`, the hedge regex and a new
   `is_gap_statement` move to `src/text_utils.py`, imported by render and evaluator (no import cycle). **Gap
   statements (D62 follow-up taken):** H2's `unit.startswith(_GAP_STARTS)` (`src/render.py:148`) misses list items;
   `is_gap_statement` first strips list markers (`-`, `*`, `•`, `1.`, `1)`, `(a)`) and emphasis, hedge rule unchanged,
   and render uses it too, so list-item gap statements stop being flagged as uncited. Scorers live in
   `src/eval_scoring.py`; `observed_scope` and `score_partial` strip one leading `CAVEAT_PREFIX` exactly as
   `src/render.py:136–137` and `src/evaluator.py:898–899` do, so a D44 related-guidance opener alone is not `partial`.
   - **[W] `score_evidence(ranked, groups, mode, absorbed=None)`**, `ranked` = ordered `(chunk_id, section)` pairs:
     `completion_rank` = max over groups of the first rank matching any member (strict = equal, related = today's
     nesting), `None` if a group is unmatched; `groups_covered@k`; hit@k iff `completion_rank ≤ k`; one group =
     today's `first_*_rank`; absorbed aliases share their chunk's rank and add no entries.
   - **[W] `observed_scope(result)`** (default display, `show_unverified=False`), first match wins: `unscored`
     (status not `complete`, a terminal outcome, or no gate outcome on a non-refusal; citations are validated
     before terminal status, `src/generator.py:427`, `:433`) → `withheld` (`CITATIONS_UNVERIFIED`) → `refuse`
     (`is_refusal`) → `partial` (a unit passes `is_gap_statement`) → `answer`.
   - **[W] `score_partial(result, groups, gaps)`:** `unscored` counts, never correct; else correct iff the outcome is
     `CITATIONS_VERIFIED` or `PARTIALLY_VERIFIED`, not `is_refusal`, ≥ 1 verified citation related-matches a required
     group, and every gap is stated (a gap-statement unit holds one of its keywords as a whole word, any case); gap
     recall is separate. **[C]** v2 sets use these scorers, v1 sets the v5 path.
   - **Report v6** (iff a set is schema 2): family counts with eligible denominators (retrieval `answer` + `partial`;
     scope and refusal all), scope confusion (labelled 3 × observed 5), family rates with Wilson CIs, cohort block,
     expansion digest (a hash only: the artifact's sha256, or for live arms sha256 of the sorted
     `(row id, rewrite and intent sha256s)`), run cost, in that order (machines read the rows sidecar, item 6). **No
     v6 run is canonical in 16A-1 or writes `eval/results.md`** (P7). Registered public v1 sets stay report v5 byte
     for byte (fixed clock, paths, provenance); private v1 files get the serialiser.
4. **Pure split and statistics (D67) [W]**, data-free building blocks for 16A-2.
   - **`split_families(families, seed, quotas, strata, *, twins, preassigned=(), min_share=None)`**
     (`src/eval_split.py`): content-free records (`family_id`, scope, primary chapter, source, register, per-split
     eligibility; a question, evidence, gap or `sealed` field is refused). `twins`: `batches` (batch id → family ids)
     partitions every supplied family once, preassigned included (unknown, omitted or duplicate refused);
     `families_digest` = sha256 of the sorted supplied `family_id`s and batch ids, recomputed (mismatch refused);
     `pairs_covered` = every unordered batch pair, self-pairs included; edge endpoints and preassigned ids must be
     supplied families. Edges merge families into one unit (union-find), eligible for a split only if every member is;
     conflicting preassignments within one unit → `SplitInfeasible`. Sorted by `family_id`, seeded shuffle, each unit
     to one split; `preassigned` families never move but count toward their split's quotas, strata, band and
     `min_share` (per-split minimum share by source); strata = scope × primary chapter (`multi`; `refuse`); band
     25–30% of each split's final families; infeasible → `SplitInfeasible` with per-constraint counts, no output.
   - **`src/eval_stats.py`** (stdlib): family collapse (`all`|`majority`; a tie is a MISS); exact two-sided McNemar;
     **Durkalski's clustered χ²** (Durkalski et al. 2003; htestClust `mcnemartestClust`): d_k = (b_k − c_k)/m_k, m_k =
     the family's phrasings, concordant included, χ² = (Σd_k)²/Σd_k², 1 df (singletons give the uncorrected
     asymptotic McNemar, not the exact test; Σd_k² = 0 → `unavailable`); Wilson (z = 1.96; empty → `unavailable`);
     exact `power(N, discordance, effect, alpha, sided)`, `"two"` counting every two-sided rejection, `"directional"`
     only those favouring the candidate; MDE = the smallest effect (0.01 grid) whose **directional** power reaches
     the target (`none` if none does; not monotone in N); Holm; per-family flip frequency. Verdicts, K_max: P5, P6.
5. **Frozen expansion artifact (D68) [W]** (`src/expansion_artifact.py`): entries keyed by (row id, question sha256),
   bound to rewrite model id, prompt sha256 and rewrite config hash, with an `inputs` header and a build record
   (entries, live attempts, fallbacks). `run_eval_matrix(expansion=)`: `"live"` (default), `build:<path>` (private →
   `eval/private/artifacts/`) or an artifact replayed in every arm; a missing entry or identity mismatch fails with
   zero expansion calls; reports record the digest, `rewrite_live`, `rewrite_replayed`; replay is never v5-canonical.
6. **C4 cohort identity + item 9 (D68) [C]** ([C]: `scripts/w_sweep.py:34–35` and the roster quote real rows).
   **Cohort block** per set in report v6: path, privacy, schema, sha256, rows, families, `cohort_fp` = sha256 of the
   sorted `(id, evidence fingerprint, scope)` tuples. **Rows sidecar:** every 16A-1 run also writes
   `<report>.rows.json` (gitignored via `*.rows.json`, so `collect_provenance` never sees it; `version`, scorer version, absorbed-map hash, expansion identity, cohort blocks, rows
   `{set_sha256, id, mode, strict_rank, related_rank, completion_rank}`); `w_sweep` rank dumps gain the same fields.
   **`compare` / `compare_prod_ranks`** key rows by `(set sha256, id)`; both arms must cover the eligible ids exactly
   once with every required field (missing, extra or duplicate fails; the `:786` skip goes). Set hashes, labels,
   `cohort_fp`, scorer version and absorbed-map hash must match unconditionally; expansion identity must match too,
   except that two live arms may differ in draw digest (rewrite model, prompt and config identities equal) and
   `--rewrite-candidate <config hash>` permits only the declared candidate config difference (its artifact records
   that hash). **Legacy:** an arm without C4 fields (a v5 markdown report, a pre-16A dump) is `legacy` and accepted only with an
   explicit `--legacy` flag (printed and recorded; without it the arm is refused); then today's v5 rules apply with no new refusal and a printed `legacy: C4 not checked` note, so v5 output is
   unchanged. `--controls <file>` (`{"version": 1, "controls": [{"set_sha256", "ids"}]}`) must be non-empty and
   resolve in both arms; control flips are reported apart (choosing controls and verdicts: P6). Roster and roles move
   to ids. **Item 9:** `w_sweep.py` loses its module-level `chdir`; helpers merge; one arm roster; one answer_fn
   status wrapper (D62). D68 re-defers D62's status recompute (it touches a canonical guard) and `test_h_projection`'s
   subprocess cost (P0 adds none).
7. **Absorbed-section map and inventory (D69).** **[W]** A chunker side channel records each D20 runt merge and each
   `_merge_appendix_stubs` absorption (`src/chunker.py:380–398`; absorbing and absorbed section, absorbed span) and
   each final chunk's span through the oversize re-split, chunks unchanged. **[C]** `scripts/absorbed_map.py` runs it
   on the corpus (no re-index) and commits `eval/absorbed_sections.json` (versioned, hashed; chunk ids and section
   numbers only, keyed by production chunk id, `src/embedder.py:378–406`) and `eval/section_inventory.json`
   (`{"version": 1, "map_sha256", "sections", "aliases"}`, sorted: every `section_number` in `./chroma_db`, and every
   absorbed label; inventoried = in either list). A chunk lists an absorbed section only if the whole trimmed span
   lies in its own (oversize siblings and find-miss sub-chunks, `src/chunker.py:491–504`, get none); every key must
   exist in `./chroma_db`; v6 strict scoring credits an alias only via its chunk; the map hash binds comparisons.
   **D54's chunk-metadata route is re-deferred, not adopted:** it would change production metadata, citations, the
   index and the gate, which this phase keeps byte-stable ((d), (l)); D69 names it a Phase 18 candidate.
8. **API spend meter, eval only (D70).**
   - **[C] `config/api_prices.toml`:** per model $/MTok (input, output, cache write, cache read), `price_list_date`,
     `usd_per_eur`, `weekly_cap_eur = 40` (D64); an unpriced model is refused when the meter is built.
   - **[W] `src/spend.py`:** `SpendMeter(prices, ledger, run_limit_eur)` builds the metered generation, rewrite and
     judge clients: today's `ChatAnthropic` with `max_retries=0`, invoked in the meter's own loop (4 attempts, as D52)
     mirroring anthropic 0.116.0 (`_base_client.py:784–896`): `APIConnectionError` (timeouts included) and
     `RetryableError` retry; an `APIStatusError` (`__cause__` walked) obeys `x-should-retry: true|false`, else retries
     408, 409, 429 and ≥ 500 only; the wait is `retry-after-ms` or `retry-after` if in (0, 60] s, else min(0.5·2ⁿ, 8)
     s × (1 − 0.25·U). So **every attempt** passes the meter (a callback, `raise_error=True`). *Reserve*
     (`on_chat_model_start`, before sending, under an exclusive `fcntl.flock`): worst case = prompt UTF-8 bytes + 64
     per message as input tokens (at the higher of the input and cache-write rates) + `max_tokens` output; if the UTC
     ISO week's settled cost + open reservations + worst case exceeds the week's ceiling, or run total + worst case
     the run limit, it raises `SpendLimitReached(kind="week"|"run")` and nothing is sent; else it appends and fsyncs a
     `reserve` line. *Settle* (`on_llm_end`): the `usage_metadata` cost replaces it; an error, timeout, missing usage
     or crash leaves the worst case charged. Lines hold time, run id, project, model, kind, tokens, $, €, ceiling,
     approval reference; never text. *Location:* one per-user ledger outside every checkout,
     `~/.local/state/claudecode/anthropic_spend.jsonl` (D64's cap is per owner), so worktrees, branch switches and
     `git clean` cannot reset it; `CC_SPEND_LEDGER` is honoured only under pytest (`PYTEST_CURRENT_TEST` set; else
     refused), where `tests/conftest.py` sets it (autouse) and the default path is refused. *Ceiling:* €40 per UTC ISO
     week, hard; a run's limit is the remainder or a lower `--approved-eur`; only
     `--owner-approved-eur X --approval-ref <ref>`, after the owner approves X, lifts it (logged on every line and in
     the D-entry; a false reference is an instruction-enforced residual).
   - **[C] `SpendLimitReached`** subclasses `BaseException`, so the broad handlers (`src/judge.py:180`,
     `src/query_rewrite.py:366`, `:372`, `src/evaluator.py:1026`) cannot make it an api error, a fallback or a retried
     error row; `expand_query` (its never-raise contract gains this exception), `judge_answer` and `generate_answers`
     document it. The meter latches; `pipeline eval` prints run and week totals, writes no report and exits 3 on
     `week` (the D64 stop) or 6 on `run` (an ordinary failed run).
   - **[C] Wiring, eval only:** `get_llm()`/`get_rewrite_llm()` are unchanged, so `pipeline query` is never metered
     (production spend is outside D64's eval cap). `generate`/`generate_with_sources` gain `llm=None`; `run_eval`,
     `run_eval_matrix` and `w_sweep` gain `meter=None` and, given one, pass its clients to generation,
     `expand_query(llm=)` and the judge's `llm_fn` (without one, call shapes are today's). A default live path with a
     usable API key and no meter raises `SpendMeterRequired` before any call; the CLI builds a meter iff it is live.
9. **Ledger + docs [C]:** D65 (item 1), D66 (2–3), D67 (4), D68 (5–6), D69 (7), D70 (8); 16A-2 allocates its own.
   CLAUDE.md (privacy rules, `eval/private/`, sealed refusal, meter); `docs/harness.md`; `Current phase`, `Next:`.

### Acceptance (Tier-1)
- **(a)** Suite and CI green, `never-commit` included; the H0 lock (`tests/test_h_projection.py`) passes.
- **(b) Canaries** (`tests/test_eval_privacy.py`, models faked): distinct `P16-CANARY-<field>-<uuid>` values (question
  start and end, gap keyword, evidence group, malformed id-like value, per-row and top-level exception text,
  fake-model rewrites, intent, answers and judge claims) go through `run_eval_matrix`, `run_eval`, the judge dump,
  `w_sweep`, `bakeoff_report`, `validate_eval_set`, a loader error and a CLI exception, and are absent (also as
  8-token windows, escaped, re-cased, re-spaced) from stdout, stderr, `caplog` and every file under `tmp_path` and
  `eval/` outside the private root; evidence groups reach only the private report; a private canonical-shaped v1 run
  never writes `eval/results.md`; `--output` catches forced injections; symlink and `..` escapes abort.
- **(c) Classification and floor:** one test per `classify` rule; a marked JSONL copied, renamed, reordered, subset or
  reserialised stays sealed, as does a malformed marked line; `.md`/`.py` marker text does not; changed bytes,
  stripped headers and mixed public + unmatched runs are private; a marked file cannot register; a weaker `privacy`
  than the floor raises with zero retrieval, expansion and model calls (downgraded sealed input too), a stronger one
  honoured, formatters refuse a weaker class; both AST tests pass; item 1's refusers refuse sealed input, zero calls.
- **(d) v5 unchanged:** `tests/test_p16_projection.py` reproduces P0's `reports`, every captured branch, byte for
  byte, constructing no network-capable client, embedding model or vector store (the patched constructors never fire); `load_golden_set` is byte-equal
  on the v1 files and H (j)'s offline rows match P0 [C]; each v5 canonical guard removed alone makes a run
  non-canonical; the CI greps are unchanged; no v6 or replayed run writes `eval/results.md`; a canonical-shaped run's recorded provenance is identical with
  and without its sidecar present.
- **(e) Evidence groups** [W, synthetic]: two groups hit → the later group's first hit; one missing → a miss at every
  k; OR alternates; a parent matches under related only; an alias takes its chunk's rank; `groups_covered@k`. [C]:
  single-group ranks equal v5's on every v1 golden and realistic row (fake retriever).
- **(f) PARTIAL and scope:** a stated gap in a VERIFIED or PARTIALLY_VERIFIED answer is correct, also as a bullet,
  numbered or emphasised item; incorrect: a missing gap, a keyword outside a gap statement, a hedged one (D32), a
  whole refusal, no verified citation in a required group, a withheld draft; truncated, declined, incomplete, unknown
  and error drafts with verified citations and a stated gap are `unscored`; one fixture per `observed_scope` class; a
  D44 caveat answer with no gap is `answer`; render stops flagging list-item gap statements, not other list items.
- **(g) Schema** errors give line and field, never text: duplicate or non-opaque id, mixed versions, incoherent
  scope/evidence/gaps, empty keywords, a `type`/`scope` clash, an empty group, an unknown status, an uninventoried section.
- **(h) `split_families`** (200 seeded synthetic pools): no unit spans splits; twin-edged families, cross-batch
  included, share a split; a `twins` record with a missing batch pair, a `families_digest` mismatch, empty, partial or
  duplicate batch membership, or an unknown edge endpoint or preassigned id is refused; conflicting preassignments in
  one unit → `SplitInfeasible`; preassigned families count toward band, quotas, strata and `min_share` and never move;
  seed and permutation invariance; no ineligible placement; infeasible → counts; a content-bearing record is refused.
- **(i) Scanner** (synthetic): public and sealed sources are refused; a 9-token private question whose two windows sit
  in different public questions is caught whole; an exact-hash public question is exempt, its `source=legacy`
  paraphrase is not; a sub-8-token question has only its whole needle; escaped injections are caught; hit lines hold
  no fixture token; a clean real-shaped v5 report has zero hits; `--merge-gate` finds needles in a blob added then
  deleted within the range, a commit message, a tag annotation and a faked `gh` PR body and comment; it refuses (exit
  7, no content) an unregistered file under the private root and an `inputs.json` `questions` input not given as a source, and passes a run whose only private inputs are a
  `legacy_public.json` cache and a sha256-keyed artifact built from a registered source.
- **(j) Stats** (recomputed 10 Oct, stdlib Python 3): exact two-sided McNemar b = 10, c = 2 → p = 0.03857421875;
  singleton Durkalski, same data → χ² = 5.3333, p = 0.0209213353 (≠ exact); clusters (m, b, c) = (2,2,0), (2,1,1),
  (3,3,0), (2,0,1), (2,1,0) → d = 1, 0, 1, −0.5, 0.5, χ² = 2²/2.5 = 1.6, p = 0.2059032107; all-cancelling →
  `unavailable`. Power, α = 0.05, gain probability 0.8 given d discordant: two-sided 0.209728 (d = 7) > 0.16777472
  (d = 8), directional 0.2097152 > 0.16777216; N = 50, effect 0.10: two-sided 0.2411886, directional 0.2411428 at
  discordance 0.2; 0.0944530 and 0.0925917 at 0.8. Directional MDE, 80% power, N = 36, discordance 0.2 / 0.3 / 0.5:
  none / 0.26 / 0.34 at α = 0.05, none / 0.29 / 0.39 at α = 0.0125. Wilson 0/10 → [0, 0.27754], 10/10 → [0.72246, 1],
  0/0 → `unavailable`. Holm (0.01, 0.04, 0.03, 0.005) → (0.03, 0.06, 0.06, 0.02). Three paraphrases count once.
- **(k) C4:** a mismatched set hash, label, `cohort_fp`, scorer version, absorbed-map hash or expansion digest is
  refused, a declared `--rewrite-candidate` arm accepted; live/live and candidate pairs still fail on set-hash, scorer
  or map mismatch; a legacy arm with `--legacy`: v5 rules, the note, no refusal; without it (incl. a post-16A report whose
  `.rows.json` was deleted): refused; missing, extra or duplicate rows fail (today's
  missing-baseline-HIT case too); empty or unresolved `--controls` refused; permuted rows match by id. **(k2)** Arms
  replaying one artifact match, `rewrite_live` = 0; a missing entry fails with zero calls; a mismatch refuses.
- **(l) Absorbed map:** a synthetic runt merge and appendix stub map as expected; only the containing oversize
  sub-chunk is credited; a key absent from a fake index fails; the inventory covers every fake-index section and
  alias; the 16-chunk sample corpus is byte-identical; the 1,470-chunk canary holds [C]; v5 scoring ignores the map.
- **(m) Spend** (fake model with `usage_metadata`; tmp ledger): all three kinds settle; two transient failures then a
  success make 3 reservations (1 settled, 2 at worst case); a call crossing the ceiling or run limit is never sent; of
  two processes racing for one remaining worst case exactly one sends; an unsettled (crashed) reservation counts in a
  new process; an unpriced model is refused; ISO-week totals are right; lines hold no fixture token;
  `SpendLimitReached` passes through `generate_answers` (one call, no error row), `expand_query` (no fallback),
  `judge_answer` (no api error), `run_eval_matrix` and the CLI (no report; exit 3 on `week`, 6 on `run`); the retry
  predicate (faked SDK errors) retries connection/timeout, 408, 409, 429 and every status ≥ 500 (tested with 500,
  501, 502, 503 and 529) and `x-should-retry: true`; no status below 500 other than 408/409/429 (tested with 400,
  401, 403, 404, 413 and 422); none on `x-should-retry: false`, which takes precedence; honouring `retry-after` ≤ 60 s; a fake key with no meter raises
  `SpendMeterRequired`; `CC_SPEND_LEDGER` outside pytest is refused; `pipeline query` and the offline command build no
  meter; the suite never uses the default ledger.
- **(n) Hygiene:** `git check-ignore eval/private/probe.jsonl` passes; `check_never_commit.py` rejects
  `eval/private/x.jsonl`; `git ls-files eval/private` is empty; `w_sweep` runs from a tmp cwd without `chdir`;
  `src.render` no longer imports `src.evaluator`; the PR carries the OS-1 manifest diff. **(o)** D65–D70 present.

**Tier-2:** offline v6 on v2 copies of golden and realistic ([C], `tests/fixtures/p16_v2_{golden,realistic}.jsonl`,
registered `fixture`, one family per row) equals v5's row-level numbers with an empty absorbed map; with the real map
every changed row is a listed alias credit. A metered live smoke [C] (one generation, rewrite and judge call on a
public golden row) runs with `--approved-eur 0.25`, sized by the meter's worst case at cached prices (25 Sep: Sonnet 5
$2.50 cache write, $10 out; Haiku 4.5 $1.25, $5) and bounded prompts (generation ≤ 29 KB: 3.5 KB system, 6 chunks ≤
4.2 KB; judge ≤ 36 KB with a ≤ 9 KB answer; rewrite ≤ 1 KB; 128 tokens for 2 messages): (29,128 × 2.50 + 2,048 × 10) +
(36,128 × 2.50 + 2,048 × 10) + (1,128 × 1.25 + 300 × 5) µ$ = $0.207, ≤ €0.21 at `usd_per_eur` ≥ 1; it prints the
meter's sum for its real prompts first, sends nothing above €0.25, and settles from real usage. A worker-lane record
per [W] item. **Gates:** plan gate on v4.1 (plan-auditor + Codex `gpt-6.1-sol`, read-only; the outline only for
completeness) → implement → `/phase-gate 16A-1` → Codex merge review → PR, CI → merge, `v2.3.0`.

### 16A-2 — eval data and re-baseline, in two phases (spec v7, 12 Oct 2026; plan-gate r1–r6 reconciled)
**Authority:** design 003 §5 16A.3–16A.5 and 16A.7–16A.8 (normative), §1.2 Q1, Q3, Q7, §6 Phase 20 and 21 rows; astra
B3, M8–M10; D46, D54, D57 (+ addendum), D61, D62, D64, D65–D70; **D71 binds** (its Q5 narrows 16A.4's second opinion to
owner-escalated labels, disclosed in D75). **Two phases (S1):** **16A-2a** builds and tests the sealed tooling on
synthetic fixtures only (`v2.4.0`); **16A-2b** creates the data and runs the re-baseline (`v2.5.0`). No new private eval
data before this spec's plan gate passes (003 16A.1); no sealed row is created and no slot claimed before 16A-2a merges.
**Stance (binding):** the simplest protocol that meets 003 16A's text. The operators (one owner who labels, the
orchestrator, a fresh-context sealed processor) are cooperative, so code is added only where an accident would
*silently* corrupt a result (a sealed row edited after approval, a re-rolled split, sealed text where the orchestrator or
the public can read it, a sealed call outside a claimed slot, a crash that loses or repeats a slot, external tracing, a
non-canonical headline); the rest is procedure with recorded evidence. "Blind" = the context that tunes retrieval (the
orchestrator, then 16B) never sees a sealed row.

**Evidence (`15e4d02`; bare numbers are `src/evaluator.py`):** sealed is refused at every entry
(`src/eval_privacy.py:88–89`, `:101–102`; `src/eval_schema.py:124–135`; `src/eval_sets.py:503–507`, used at `:180`;
`src/expansion_artifact.py:512`; `scripts/scan_leaks.py:276–278`, `:317–318`; `src/pipeline.py:691`) and cannot register
(`src/eval_sets.py:227–233`); schema v2 has no `sealed` key (`src/eval_schema.py:85–87`, `:376–378`) and one scope per
family (`:469–481`); the precheck refuses non-source files under `eval/private/` outside `runs/` and `artifacts/`
(`scripts/scan_leaks.py:794–819`); public windows are exempt (`:427`); artifact keys are `<set sha256[:16]>/<id>`
(`:2654`); replay and `build:` refuse both skips (`:1983–1984`, `:2002–2003`), and the CLI builds no meter then
(`src/pipeline.py:698`); v6 refuses the judge (`:2154–2155`), is never canonical (`src/eval_v6.py:42`, `:204`) and drops
refusal families from retrieval rates (`:137`); a run writes its sidecar beside its report (`:1341`); metered calls run
LangChain callbacks (`src/spend.py:956`), so configured tracing would send prompts out (D65); `split_families` takes
exact final quotas with preassigned families pinned (`src/eval_split.py:28–30`, `:45–53`), merges `edges` by union-find
into keep-together units with no other meaning (`:54–73`, `:85–88`), and on `SplitInfeasible` returns nothing (`:907`,
`:927`); prices and cap `config/api_prices.toml:11–26`; one rewrite + generation + judge settled €0.0200 (D69).
**Tutorials (owner, 12 Oct):** `Tutorial_Docs_for_review/` (gitignored, `never_commit`, `restricted_read`): the 2025 PPC
tutorial manual (17 tutorials, pp. 22–68; 7 title-document sets, pp. 69–218) and the T1 and T7 exercise sheets. T1 and
T7 gave the 24 candidate rows; 15 tutorials and the 7 sets are unseen; 003 §7 Q9 is pending, so all stay private (Q7).
The orchestrator never opens the folder beyond the 12 Oct table-of-contents read.

**Lanes:** **[C]** Claude only: real rows, corpus, tutorials, SP dispatch, ledgers, integration. **[W]** R0 code on
synthetic fixtures in files already in `worker_allow` (a2; the `eval_schema`, `eval_privacy` and `validate_eval_set`
parts of a1); the rest is [C]. Workers stay off until the owner's `harness-init --refresh` and a drift-free
`harness-doctor` (D71 Q1); until then [W] runs as Claude. `.harness/project.toml` is not changed.
**Trusted processors (D72), the only readers of sealed rows:** (1) the owner; (2) the sealed processor (SP, a7); (3) the
processes of `sealed_tool.py`, `scan_leaks.py` and a slot-gated run, which print only counts, sha256s and enums; opaque
family ids appear only in scanner diagnostics, the private ledger, `return`'s validated line, and the sheets, forms and
forwarding log under `eval/private/sealed/work/`; (4) Anthropic's API inside a claimed slot (003 §7), tracing off (a5);
(5) the colleague, for items the owner forwards (D71 Q6). Never the orchestrator's context, Codex, a worker or any
third-party model.
**Unit (S8, R1, R7):** the **family** (one scenario, one scope) is the unit of split and counting. The SP's all-pairs
twin verdict merges (as phrasings) or drops any twin inside the new pool; a twin of a pinned development (v1, T1/T7,
earlier batches), public or regression family joins it as a phrasing or is dropped. A **relation** is a twin verdict,
the same source tutorial, or the same primary evidence section (the first section of the first evidence group). Groups
are the connected components of all relations over every draft and every pinned family, computed **before** any edge
is filtered (r4: P–A–B). A group containing a pinned development family, or a draft sharing an 8-token window with a
public question, is **sealed-ineligible as a whole** and goes to development by quota and register; its edges are never
passed to `split_families`. The remaining groups are passed as `edges`. In round 2 a draft related to any round-1
sealed group is **dropped** (never joined, so no forced placement and no overfill; r5), and the SP drafts round-2
families from tutorials and primary sections that no round-1 sealed group uses. So no pinned family is ever in a passed
group and `preassignment_conflicts` cannot arise. Demotion, exclusion and recheck act on the **whole group**; the group invariant
is checked after every move.
**Inference (R2, r5):** the confirmatory **estimand** is the family-level difference in strict hit@6 over eligible
families (answer or partial; the MWE's scale). The primary test is an **exact cluster sign-flip test**: each group g
contributes d_g = (families the candidate gains) − (families it loses), and p is the exact probability, over
independent random signs on every |d_g|, of |Σ| at least as large as |Σ d_g| (**two-sided**, enumerated by a dynamic
programme over integer sums; the sign of Σ d_g gives the direction, so a gain and a loss are each declared at α
two-sided, as v5's exact McNemar was). Groups are independent, and under the paired-randomisation null (the two arms
are exchangeable within each group, which implies a zero family-level difference) d_g is symmetric, so the size is
≤ α exactly under that null; within-group changes are never masked, and with all-singleton groups p equals
`eval_stats.mcnemar_exact` (two-sided). Power and MDE come from a seeded Monte Carlo of this test on the family scale
(rejections in the candidate's favour, as `power(..., sided="directional")`), with the planned group sizes, under
independent families (optimistic; with singletons it reproduces `eval_stats.power`/`mde`, so the figures below hold)
and under full within-group correlation (pessimistic). Both are reported. Durkalski by group (asymptotic) and family-level exact McNemar are labelled sensitivities. **Floor (families, 003):** ≥ 50 approved sealed and ≥ 35 eligible answerable
(answer or partial, not excluded); sealed 60 in the band gives 42–45; the group count is reported beside it.

### 16A-2a — sealed tooling — `phase-16a2-eval-data` → `v2.4.0`
a1. **Sealed class, narrowly (D72) [W; loader and wiring C].** Schema v2 gains optional `"sealed": true` (true only).
    Read-only access goes only through `src/eval_sealed_read.py` (one loader for validation, scanning and `sealed_tool`;
    no retrieval or model call; `validate_eval_set.py --sealed` prints line and field errors only). Every run,
    retrieval, generation or replay entry still refuses sealed (exit 4) unless given a `SlotToken` (a4, a5).
    `tests/test_p16_ast_hygiene.py`: no function in `src/` outside `src/eval_sealed_read.py` takes a parameter whose name
    contains `sealed`; in `scripts/` only `validate_eval_set`, `scan_leaks`, `sealed_tool` and `sp_launch` may (D65
    reconciled in D72). `eval_privacy` gains `sealed/runs/<id>/` and `sealed/artifacts/`. A **row hash** is sha256 of the
    canonical row without `sealed`, `owner_status` and `second_status`. **P4 salt: none:** only v2 private sets with
    random ids (`secrets.token_hex(4)`) are written, so a committed set sha256 confirms only a whole file.
a2. **Scanner (D72) [W].** Every `*.jsonl` under the private root outside any `runs/` or `artifacts/` is a needle source,
    sealed ones as **S-needles**; `sealed/runs/` and `sealed/artifacts/` are traced like `runs/` and `artifacts/`;
    non-JSONL files are allowed only under a `work/` directory, exempt from the precheck; anything else exits 7.
    **Exempt windows** = public ∪ development ∪ handbook text (the b0 dump, read in-process). A sealed question under 8
    tokens has no window needle: only an exact match of the whole normalised question against a whole JSON string value
    or a whole line counts (residual: one embedded in prose is missed). An S-hit prints target and family id only.
a3. **`scripts/sealed_tool.py` (D72) [C; synthetic tests].** Loads via a1's loader; one allowlisted printer. Commands:
    - `ids`; `digest` (sha256, rows, families, counts by scope, register and source; never by chapter for sealed);
      `count-migrated` (b2); `inputs-check` (b0).
    - `twins` records the SP's all-pairs verdict (every draft against every draft and every development, public,
      regression and quarantined family; a lexical screen only orders pairs) as 16A-1's `twins` record under the Unit
      rule: sealed-ineligible marks for drafts tied to pinned or public families, and `edges` = split-group edges among
      sealed-eligible drafts only; it refuses a verdict that leaves a twin pair inside the pool.
    - `split --round 1|2` refuses unless the pushed prereg declares the round. Each **attempt** records the pool digest
      (sha256 over every pool row's canonical JSON) and its outcome; `SplitInfeasible` releases nothing, the SP drafts
      more under the prereg's rule, and the first feasible attempt is released, so nothing is seen before release.
      `eligible` follows register: realistic takes only lay or terse drafts and tutorial-sourced families; modelled
      drafts may go only to tuning or sealed. **Round 2** (once, after validation and recheck, before freeze; R3)
      replaces every sealed rejection or demotion and every development shortfall against quota or band; its drafts are
      twin-checked, paraphrased by the SP to 2 phrasings, owner-validated on a round-2 sheet (b5) and rechecked before
      freeze; a round-2 family the owner rejects is dropped (there is no round 3), and a resulting shortfall is the stop
      "freeze infeasible after round 2"; surviving round-1 families are pinned and each split's quota = pinned +
      replacements.
      Release **moves** development families from
      `sealed/pool/` to `eval/private/dev/`, unmarked (the only sanctioned unmarking), by copy, fsync, verify and delete
      under a move journal that recovery replays; launcher outputs move the same way.
    - `sheet`/`apply`: a review sheet (CSV, one line per phrasing: ids, row hash, phrasing, scope, evidence with pages,
      gaps, `ambiguous`; owner columns A / C / R / E, corrections, the colleague's key); `apply` re-validates against the
      inventory and writes `work/approvals.json`, binding each final row hash to the sheet's sha256; changed rows bind
      once the owner ticks a confirm sheet.
    - `freeze --split <name>` (per split; sealed needs no development approval) replays the released split from the
      private ledger and requires the SP's `recheck` verdict on the **final** content, preceded by a lexical screen of
      every sealed row against every development row (a shared 8-token window or a whole short question goes to the SP;
      R6): a twin with development demotes the whole group, approvals kept; a twin inside sealed merges or drops one. It
      checks approvals, 2–3 phrasings, the 25–30% band in families (rows reported), strata, `min_share` and the group
      invariant on the post-move counts, and for sealed the floor. A sealed band breach is repaired by the fewest seeded
      demotions of over-represented sealed groups to quarantine while sealed stays ≥ 50 families; anything else still
      failing is the D64 stop "freeze infeasible after round 2". It binds the twins record into the freeze record. The
      frozen sealed file is **immutable for the epoch**.
    - `escalate`, `calibrate`, `disagree` (b5); `exclude` (a4); `audit` (a6); `power`, `prereg-check` (a8); `return`
      prints an SP return file only if every key and value matches the id, hash, count or enum patterns, else `invalid`.
a4. **Ledgers, slots, recovery, exclusions (D72) [C].** Full content-free records (attempts, assignments, freeze, slots,
    exclusions) live out of tree like D70's (`~/.local/state/claudecode/sealed_ledger.jsonl`, exclusive `flock`, fsync)
    with a private copy in `eval/private/sealed/ledger/`; the committed mirror `eval/sealed_ledger.jsonl` holds record
    kinds and digests only (no scope, chapter, stratum or `sealed` key). Losing both private copies stops the epoch. A
    sealed run: (1) **claim** the slot (refused if claimed before); (2) run; (3) write per-row results, sidecar and
    artifact under `sealed/runs/<id>/` and fsync; (4) decide canonical status; a canonical run records a **publication
    intent**, a non-canonical one prints guard names only and writes none (0b stays available once); (5) render and
    scan before publishing: a hit excludes the hit group and re-renders; (6) record **consumed**. Recovery makes no
    fresh sealed call: a claim without stored results spends the slot; stored results without an intent are rendered
    from storage, and the slot is consumed only once rendered; a durable intent disqualifies 0b even if rendering was
    interrupted, and recovery republishes the same stored result. **Exclusions:** an exposure, an S-hit, or a new
    development version whose clearance pass (b6) finds a twin appends an exclusion against the whole sealed group,
    never a move or unmark; analysis drops it from both arms while its rows still run, so set hashes, artifact keys and
    pairing survive. No top-up after freeze; below the floor the epoch ends (D64 stop).
a5. **Sealed run and canonical v6 (D73) [C].** `pipeline eval --sealed-slot <n>` gets its `SlotToken` only if a4's claim
    succeeds; the sealed file, twins record and exclusion list match their records; every sealed row is approval-bound;
    a meter is active; the expansion mode is the one the prereg declares for that slot (live for 0 and 0b; replay of the
    declared artifact; a live candidate arm under `--rewrite-candidate` where declared); and tracing is off: the process
    **scrubs** `LANGSMITH_TRACING_V2`, `LANGCHAIN_TRACING_V2`, `LANGSMITH_TRACING` and `LANGCHAIN_TRACING` to `false`
    before importing `src` (`load_dotenv` does not override), so tracing set in the environment or `.env` is
    neutralised, and it refuses if a global tracer or tracer callback is configured or
    `langsmith.utils.tracing_is_enabled()` is still true just before the first call. Else it refuses before any call.
    Report v6 gains the judge (answer + partial rows; unsupported rate with Wilson CI; dump in the run directory) and a
    **sealed rendering**: no ids, no per-family or per-chapter line; per-scope, per-register and per-source aggregates
    (tutorial versus Claude-drafted, with the D46 caveat on the drafted part); the group count. Slot 0 is descriptive
    (Wilson CIs). **Canonical v6** = v5's guards (D46, D51, H1c), the held-out clause met by the frozen sealed set, plus
    slot 0 or 0b, the bound records, live expansion and the tracing guard; a4's pre-publication scan is a step, not a
    guard.
    Only canonical v6 writes the committed **`eval/results_v6.md`** (aggregates; development sets as counts); its
    sidecar stays in `sealed/runs/<id>/`. `eval/results.md` keeps v5's untouched writer, now historical (P0, H0 hold).
a6. **CLI, runner, audit (D73) [C].** `src/pipeline.py:698` counts `--expansion build:` as live; `build:` enables live
    expansion under both skips (rewrite calls only); a replayed artifact under both skips runs retrieval only with zero
    calls; both skips alone stay zero-call. `--regression <path>` adds heldout-v1 as a fourth set. `audit` scans with
    S-needles every `~/.claude/projects/*` directory derived from this repository's path (worktrees and subagents
    included) or the SP staging root, `logs/`, `data/research/`, `eval/private/work/`, non-sealed runs, the dispatch
    logs, and the tracked tree and history; it prints counts per target and each hit's family id (residual: cloud
    sessions). Development files are covered by a3's freeze screen instead, since their windows are exempt (R6).
a7. **Sealed-processor launcher (D72) [C].** `scripts/sp_launch.py --kind probe|data <task>` creates
    `~/.local/state/claudecode/sealed_sp/<dispatch id>/`, copies in exactly the task's inputs and runs there under
    `env -i`, keeping only `HOME`, `PATH`, `USER`, `LANG` and `TMPDIR` (no `CLAUDE_CODE_*`, `CLAUDECODE` or API key):
    `claude -p --restricted --tools Read,Write,Edit,Glob,Grep --permission-mode acceptEdits --permission-prompts none
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence --model opus --append-system-prompt
    <brief text>`, as an argument vector, with `<task>` on **stdin** (`--mcp-config` is variadic and swallows a trailing
    positional prompt: orchestrator pre-probe, 12 Oct). That pre-probe (canary file, `env -i`, `--restricted`, empty
    MCP config) returned the canary, DENIED for a read in the repository, and no MCP tools, with subscription login
    working; the Tier-2 probe repeats it through the launcher. Every flag is in `claude --help` (12 Oct); no `-file`
    form of the system-prompt flag is listed, so the launcher reads `docs/sealed/sealed-processor.md` itself. The SP's
    stdout and stderr go to files in the staging directory and are never printed: the launcher's only output is
    `sealed_tool return`'s validated line and its exit status. It then runs `sealed_tool.py` (ids, lexical screen,
    validation, `return`), moves `out/` into `eval/private/sealed/work/sp_out/<id>/` (taken in by `sealed_tool
    intake`; implementation note, 12 Oct: the SP's twin verdicts there are record files, its pool a sealed source) and
    the stdout/stderr logs into `eval/private/sealed/work/` crash-safely (a3), and deletes the
    staging directory in a `finally`; each launch first sweeps stale staging directories, and `sp_launch.py --sweep`
    runs before any Codex call. A `data` dispatch first requires `git fetch` and `git
    merge-base --is-ancestor <prereg sha> origin/<branch>` and logs to `eval/private/sp_dispatch.jsonl` and the
    out-of-tree ledger; a `probe` uses a synthetic prereg and logs to `eval/private/sp_probe.jsonl`. **Enforced** (per
    `claude --help`): no Bash, MCP or user, project or local settings (managed settings and `--settings` still apply;
    none is passed); file tools confined to the staging directory, so `.env`, the repository and this project's memory
    directory are out of reach; no transcript. **Instruction-only** (D72
    residual, the trust status of Codex in the global CLAUDE.md): what the brief says to draft and return.
a8. **Builders and tooling (D73) [C].** `scripts/handbook_sections.py` (b0's dump). `inputs-check`: every id in
    `eval/v2_pairing.json` is a public v1 id used at most once; every label in `eval/conflict_sections.json` is
    inventoried. `scripts/controls.py --rule c1|c2` computes the control list from draw sidecars and refuses a differing
    committed list. `eval_stats` gains `cluster_signflip_p(d)` (exact, two-sided) and `cluster_signflip_power(group_sizes,
    discordance, effect, alpha, draws, seed, model)` ([W], synthetic); `power` prints both models; `prereg-check`
    validates a prereg and recomputes its quota formulas and power table.
a9. **Docs [C]:** D72 (processors, launcher, scanner, ledgers, slots, recovery, tracing, residuals); D73 (canonical v6,
    `results_v6.md`, CLI, builders, heldout-v1 retired to regression per 003 16A.3). CLAUDE.md: sealed rules, tutorial
    wording, the `build:` exception to the zero-call line, the held-out headline line, and the canonical Codex clause
    gains `Tutorial_Docs_for_review/` and `~/.local/state/claudecode/sealed_sp/`. `docs/harness.md`: SP lane,
    negative path, the `:43` held-out row. `pressure-tester.md`, `plan-auditor.md` and the phase-gate skill gain
    `eval/private/sealed/`, `Tutorial_Docs_for_review/` and the SP staging root as off-limits; sealed criteria are
    evidenced by `sealed_tool` counts only. **Residuals (D72, instruction-enforced):** a sealed question typed into
    `pipeline query` by its holder; an SP that ignores its brief; files outside the audited set; spend-ledger token
    counts; work files holding text not yet in a JSONL; the owner's and colleague's mail; the orchestrator's restraint
    with the tutorial folder.

**16A-2a acceptance** (Tier-1; synthetic, schema-valid fixtures; models faked; every test proves the success path ran):
- **(a)** Suite and CI green, `never-commit` included; P0 and H0 locks unchanged; `git ls-files eval/private` empty.
- **(b) Gates:** without a `SlotToken` every run, retrieval, generation and replay entry refuses sealed (exit 4, zero
  calls); the loader, validator and scanner accept sealed with zero retrieval or model calls; the AST rule fails on a
  violation planted in a temp module; a re-claimed slot, a sealed file, twins record or exclusion list off its record,
  an unbound sealed row, or an expansion mode other than the slot's declared one refuses before any call. Each tracing
  variable, in the environment or `.env`, is scrubbed and the run proceeds while a spy transport sees nothing; a global
  tracer, a tracer callback, or `tracing_is_enabled()` still true after the scrub refuses.
- **(c) Canaries** in every free-text field and in fake-model output (rewrites, intent, answers, judge claims), with
  valid ids and enums, are absent (also as windows, escaped, re-cased) from stdout, stderr, `caplog`, `results_v6.md` and
  every file outside `sealed/`; opaque ids appear only in the places the trusted-processor list names.
- **(d) Scanner:** `sealed` must be `true`; a 5-token sealed question is caught as a whole value or line, not in prose;
  ordinary conveyancing wording, development text and handbook windows raise no S-hit; a non-JSONL file outside
  `work/` exits 7; S-hits in a file, blob, commit message and faked PR body print target and family id only.
- **(e) `sealed_tool`:** printer fuzz; `twins` refuses a surviving twin, marks a draft tied to a pinned or public family
  sealed-ineligible and passes no edge for it; a pinned development family P, a draft A sharing P's primary section and a
  draft B sharing A's tutorial make the whole group P–A–B sealed-ineligible (r4); a round-2 draft related to a round-1
  sealed group is dropped, and a round-2 pool whose other drafts fit the replacement quotas releases (r5); `split` honours register eligibility, refuses an unpushed prereg or
  undeclared round, records an infeasible attempt and releases nothing, and recovers a move crash between copy and
  delete; round 2 pins round 1 and fills sealed replacements and development shortfalls, and `freeze` refuses a round-2
  family without 2 phrasings, an owner approval or a `recheck`; `freeze` catches a changed
  assignment, a missing approval or `recheck`, a sealed phrasing planted in a development row (whole group demoted,
  approvals kept, group invariant intact), a band breach (fewest seeded group demotions to quarantine) and a phrasing or
  floor breach (the D64 stop), and passes the sealed freeze with development unapproved; `return` rejects free text.
- **(f) Fault injection** at each of a4's steps: no fresh call on recovery; a crash before step 3 spends the slot; a
  crash between steps 3 and 4 renders from storage and consumes only once rendered; a non-canonical slot 0 writes no
  intent and allows 0b once; a crash after the intent, before or after the first emitted number, refuses 0b and
  republishes; a post-freeze group exclusion keeps the next slot's replay and pairing; below the floor the epoch ends;
  losing both private ledgers stops it.
- **(g) Canonical v6:** each guard removed alone → non-canonical, guard names only; a scan hit in the rendered report
  excludes the hit group and re-renders; only canonical v6 writes `results_v6.md`; no v6 run writes `eval/results.md`;
  the sidecar never lands beside the report; the judge, source split, caveat and group count render.
- **(h) CLI** (mocked): `build:` under both skips builds a meter, makes rewrite calls only, propagates exits 3 and 6;
  replay under both skips and both skips alone make zero calls; `--regression` adds a fourth set.
- **(i) Launcher** (mocked `claude`): the exact argument vector, stdin task and scrubbed environment; refusal with an
  API key, or with a prereg not reachable from `origin/<branch>` or `origin/main` (b1a; accepted when a later commit
  sits on top); a fake
  `claude` printing a canary leaves it out of the launcher's output; a failed run still deletes its staging directory
  and the next launch sweeps a stale one; the two kinds log apart.
- **(j)** `controls.py` under both rules; `cluster_signflip_p` (two-sided) matches brute-force enumeration on small
  cases, equals `mcnemar_exact` with singleton groups (e.g. b = 7, c = 1 → 0.0703), and `cluster_signflip_power` under
  independence with singletons matches `eval_stats.power(..., "directional")` within Monte Carlo error; and on Codex's r5 fixture (20 two-family groups, gain and loss types
  equally likely, true family effect 0) rejects at no more than α over exact enumeration; a group where one family
  gains and another loses contributes 0, and a refusal-only group contributes nothing; `cluster_signflip_power` is
  seed-deterministic and its MDE table equals the prereg's; Durkalski and the family test render as labelled
  sensitivities;
  `disagree` reports scope, group structure and gaps apart (missing-group, extra-group, different-gap fixtures);
  `inputs-check` refuses a reused or non-public id and an uninventoried conflict label; `handbook_sections.py` on a
  synthetic chunk store yields every inventoried section.
- **(k) Audit:** a planted S-needle in a synthetic transcript, memory file, log, run or `eval/private/work/` file is
  counted with its family id.

**16A-2a Tier-2:** one `probe` dispatch from the orchestrator's shell on canary-only data: a file read outside the
staging directory is denied, no MCP tool is present and no memory directory is created (the environment scrub is
2a-(i)'s). **Gates:** plan gate (this spec) → implement → `/phase-gate 16A-2a` → Codex merge
review → PR, CI → merge and tag **`v2.4.0`** under D64.

### 16A-2b — data and re-baseline — `phase-16a2b-data` → `v2.5.0`
**Entry:** 16A-2a merged; the owner's answer to b1's question in D74; the prereg committed and plan-gated while
**unpushed** (gate findings are fixed before the push, so they are not owner stops), then pushed: the push commits it.
b0. **Inputs [C].** The handbook dump (`scripts/handbook_sections.py`, from the corpus via the chunker: section label,
    page span, text; gitignored; check: every inventoried section present); `eval/v2_pairing.json` (the orchestrator
    pairs public golden-v1 and realistic-v1 ids that are one scenario; `inputs-check`); `eval/conflict_sections.json`
    (labels only: the pre-1975 architect's-certificate date conflict plus any the owner flags; `inputs-check`).
b1. **Pre-registration (D74) [C].** `eval/prereg_16a2.json` (content-free): split seed (`secrets`); quota formulas over
    the pool (sealed 60, 10 above the floor as spare; realistic max(42, preassigned realistic); tuning = the rest, ≥ 60,
    else the SP drafts more before the next attempt); the drafting rule for attempts and round 2; strata; band;
    `min_share` (realistic: tutorial + colleague ≥ 0.25, D71 Q4; none elsewhere); the floor; calibration seed; primary
    metric = sealed family strict hit@6, hybrid+rewrite (`all` collapse); **MWE +0.10** absolute;
    α = 0.05 family-wise over rejections in **either direction** (one two-sided test per candidate; Holm over the K
    two-sided p-values with K = 4 fixed in advance, b1a; a significant result's direction is the sign of Σ d_g); **K_max = 4**
    confirmatory candidates per epoch, declared as one non-adaptive batch before the first confirmatory slot; **primary test the exact cluster sign-flip test on the family-level
    estimand** (Inference), Durkalski by group and exact McNemar on families the sensitivities (R2, r5);
    rewrite-changing candidates expand live under `--rewrite-candidate` against the incumbent's replay; R = 5; the
    canonical v6 guards; power tables for groups and families. Slots: 0 = descriptive baseline (the canonical run),
    0b = spare, 1–4 = the batch, refused until 16B's sealed comparison passes its own gate. Later changes are amendments.
    **Adoption and controls — decided by the owner on 13 Oct: (A) with (C1) (D74).** The options were:
    - **(A) Development evidence adopts; sealed is a non-inferiority guard** (b1a: the guard is exactly its two checks). Adopted when the development family effect
      is ≥ MWE, no critical control flips, unsupported claims do not rise, *and* sealed shows no harm (point estimate
      ≥ 0, no Holm-significant loss under the sign-flip primary). Modest real gains below the sealed MDE can ship, but so
      can a change overfitted to development and neutral on sealed. It departs from 003 §5 16B (003:255).
    - **(B) Sealed confirmation only.** Adopted only on PASS (favours the candidate, sign-flip-primary Holm p ≤ α, effect
      ≥ MWE, no control flip, unsupported claims not higher). The exact sign-flip test bounds false adoption by α under the
      paired-randomisation null, but with 35–45 eligible families in fewer groups a true gain near the MWE is usually not
      adopted: for independent singleton families power at the MWE is ≈ 0.11–0.16 (`eval_stats`), group structure moves it
      either way, and the prereg's Monte Carlo table gives the figures for the planned groups. This favours the
      incumbent.
    - **Controls (C1):** public-lineage development families with strict completion rank ≤ 3 in raw hybrid and in
      hybrid+rewrite on every draw, which **reverses the D57 addendum** ("a rank-6 hit that any perturbation dislodges
      is real fragility"; D74 must say so). **(C2):** D57 as written: every incumbent strict HIT among them.

    Under any choice FAIL = a control flip or a Holm-significant loss (sign-flip primary), and **Phase 21 keeps 003's
    rule: it ships only if it beats single-pass on sealed families** (§6). D57's re-open condition is **not** met here:
    under (A) an embedding arm spends a slot only on development evidence ≥ MWE, under (B) only if its development effect
    is ≥ the sealed MDE. b9 aligns CLAUDE.md's bake-off line, `.claude/skills/bake-off/SKILL.md:20–21` and
    `docs/harness.md` principle 3 with the choice.
b1a. **Prereg completeness and binding (13–14 Oct; prereg plan gate r1: Codex REVISE 1 MAJOR, plan-auditor 1 BLOCKER /
    5 MAJOR / 10 MINOR; closure r2: Codex REVISE 4 MAJOR / 2 MINOR (F1–F6), plan-auditor FAIL 1 BLOCKER / 6 MAJOR /
    10 MINOR (A1–A17); all ACCEPTED and implemented before the prereg is pushed, so none becomes an amendment) [C].**
    This item supersedes the b1 text it contradicts.
    - **Drafting arithmetic (Au1, A2, A16).** Refusal drafts = max(round_half_up(0.275 × pool_total) − preassigned
      refusals, the sum of the development splits' deficits to 25 % of their quotas), with round_half_up(0.275 × n) =
      (11n + 20) div 40. `count-migrated` prints the plan (pool total, target, drafts, each split's open slots) and
      stops (`band_unreachable`, a b2 owner stop) when a development split cannot reach its band: its deficit exceeds
      its open slots, or its preassigned refusals exceed 30 % of its quota. The SP brief drafts exactly
      `task.json:refusals` refusals, and at least `task.json:lay_min` of its lay or terse drafts are lay. `split`
      records an attempt `register_short` (nothing released) when lay < 0.5 of the new lay-or-terse drafts. Round 1
      allows at most 3 attempts; a 4th is the stop "split infeasible" (Au11). Round 2 allows at most 3 attempts
      under the same formula over its own pool; a 4th exits `EXIT_FREEZE_INFEASIBLE` with reason `split_infeasible`,
      the stop "freeze infeasible after round 2" (A9). Round 2's lay check reuses attempt 1's registered share.
    - **Confirmatory slots (Au2, Au9, A1, A3, F3).**
      - The prereg declares slots 1–4 as `batch` slots. A `batch_declaration` ledger record names K ≤ 4 candidates in
        slot order (candidate i runs in slot i). Each candidate has a config hash and an expansion mode:
        - `replay` (retrieval-only): replays `slot0_canonical`, the sha256 of the canonical slot's (0, or 0b with an
          intent) run-directory `expansion_artifact.json`, carried by its `slot_stored` record;
        - `candidate` (rewrite-changing): live rewrite under `--rewrite-candidate <rewrite hash>`, against the
          incumbent's replay. The entry also carries `rewrite_config_hash`, which the gate checks against
          `rewrite_identity()` now; `config_hash` is the canonical pipeline configuration, which 16B checks (r3 F9).
      - The record is mirrored as a digest, written once, and needs the controls record (below). The gate refuses a
        slot whose command line does not match its entry.
      - Holm always uses K = 4, fixed in advance (Au14), and a spent confirmatory slot still counts.
      - **Embargo:** slots 1–4 and `declare-batch` are refused in code (`CONFIRMATORY_EMBARGO`) until 16B's reviewed
        code change lifts it. That change must add the registered candidate-identity check: the sha256 of the run's
        canonical pipeline configuration equals the declared hash, checked at the gate before the claim. Until then the
        gate's `check_candidate_identity` always refuses (`identity`), so flipping the boolean alone opens nothing
        (r3 F11). The candidates are bound before any confirmatory slot can be spent, but after slot 0's descriptive
        aggregates (the D74 residual in docs/decisions.md, D74 r3 addendum).
      - A confirmatory slot needs the judge on.
    - **Adoption inputs (Au3, A6, A7, A8).**
      - **Development evidence** is the family-level strict-hit@6 difference (`all` collapse) in hybrid+rewrite on
        tuning-v2 ∪ realistic-v2 at their freeze records, paired by draw index, as the mean over R = 5 draws. The
        incumbent's draws are the first 5 development draws recorded in the ledger under the incumbent's config hash
        (b7). A `replay` candidate replays the incumbent's draw-i expansion artifact; a `candidate` one uses the first
        5 draws recorded under its own config hash (r3 F10).
        Later draws never count, so re-running draws cannot move the mean.
      - **Unsupported claims "do not rise"** (D74, unchanged; the r1 draft's +0.01 tolerance is withdrawn): the
        candidate's claim-level unsupported rate on its sealed slot ≤ the canonical slot 0's judged rate.
      - A **control flip** is a strict HIT@6 → MISS in raw hybrid or hybrid+rewrite on any of the first 5 recorded
        draws (D57's rule; r3 F8 restores raw hybrid, which r2 had dropped without an owner decision).
    - **Version binding (Au4, A5, A15, F4).**
      - The controls and the development evidence bind to the tuning and realistic freeze records.
      - Every slot's private development inputs must be those freeze versions, each once, and slots 0/0b need both
        (`dev_binding`). `controls.py` needs both splits, each once.
      - `controls.py` writes a `controls` ledger record once (list sha256, draw-sidecar digest, prereg sha256). A
        later run only verifies, and deleting the file does not reopen the list.
      - The b7 draw records (mechanism landed in b1a r5–r6: `pipeline eval --record-draw`) name which draws count; each carries the
        config hash it ran under (r3 F10). `controls.py` refuses repeated draw files (distinct sha256s) and, on a
        verify run, draws other than the recorded ones (r3 F5).
      - Slots 0 and 0b also need the `controls` record and the unchanged file, so the control list exists before
        slot 0's results (r3 F6).
    - **Freeze-time minimums (Au5).** `freeze` checks tuning ≥ `tuning_min` (60) and realistic ≥ `realistic_min`
      (42); a breach is the stop "freeze infeasible after round 2".
    - **Binding (Au6, Au10, A4, F2).**
      - `split`, `freeze`, `calibrate`, `declare-batch` and the slot gate accept the prereg only if it is committed,
        unmodified and an ancestor of `origin/main` or `origin/<branch>`. Main is accepted because the branch ref
        disappears after merge. The data dispatch's launcher follows the same rule, including that the commit is the
        last one touching the prereg and its blob equals the working file (r3 F7).
      - The first ledger record carrying a prereg sha256 (normally the first data dispatch, r3 F7; else the first
        split attempt) registers the **epoch prereg**. Every later use, `controls.py`
        included, must present the same sha256 (`prereg_amended`, a D64 stop).
      - Freeze records and slot results carry the prereg's sha256; slot results also carry its commit.
    - **Checks (Au7, Cx1, Au8, A12, F5).**
      - `prereg-check` validates every registered value against one table (`REGISTERED_VALUES`, r3 F4): every leaf
        of `primary_metric`, `collapse`, `adoption`, `controls`, `slots` and `drafting_rule`, plus `mwe`, `alpha`,
        `k_max` and `draws_r` (= 5). An unregistered leaf under those roots is refused. Seeds, quota, band and
        power keep their own recomputation checks.
      - `controls.py` takes its rule, ceiling and draw count from the epoch prereg.
      - `calibrate` takes its seed only from the epoch prereg.
      - The power table has cells at α ∈ {0.05, 0.0125} for both group models, plus a singleton-family table from
        `eval_stats.power`/`mde`, all recomputed by `prereg-check`.
    - **Calibration (Au12, A14, F1).** It is drawn once, after every split is frozen, from the frozen files' own
      approved rows (their sha256 must equal the freeze records), by the epoch prereg's seed. A `calibration_draw`
      ledger record makes the draw durable: a re-run reproduces it or is refused.
    - **Visibility (Au13, F6).** The sealed freeze reports its source split as counts only: tutorial
      (`source` = tutorial) vs drafted.
    - **Closure r3 (16 Oct; Codex REVISE 1 BLOCKER / 3 MAJOR / 1 MINOR, plan-auditor FAIL 1 BLOCKER / 7 MAJOR /
      6 MINOR; dispositioned in D74's addendum).** Besides the in-place changes above (F4–F11):
      - **Results (F1).** Only private non-sealed inputs are bound to the development freezes; a registered public
        regression set (b8's `--regression`) no longer fails results assembly after the claim.
      - **Quotas (F2).** `count-migrated`'s default tuning quota is split's own (pool − sealed − realistic).
      - **No eager claim (F3).** `acquire_slot_token(claim=True)` is refused (`claim`); every claim goes through
        `run_slot`'s preflight. `SlotToken.claim()` stays public for `run_slot`; only tests call it directly.
      - **Attempt-1 size (F12).** Drafts = max(`families_approx`, sealed + tuning_min + realistic quota − migrated),
        so attempt 1 is not predictably `quota_short` (≈ 57 migrated → 105 drafts; r4's margin makes it 110).
      - **Epoch check (F13).** `sealed_tool epoch` prints the registered epoch prereg (or none) and whether the
        working file matches; b2 runs it first.
      - **Deferred to b3/b4 (Au m2).** b3's dispatch writes `task.json:refusals` from `count-migrated`'s
        `refusal_drafts` and `lay_min` = ceil(0.5 × the attempt's planned lay-or-terse drafts); b4's intake refuses a
        pool whose refusal count differs from `task.json:refusals`.
      - **Deferred to 16B (Au M4).** Holm with K = 4 and the adoption inputs (development evidence, unsupported
        claims, control flips) are 16B's analysis code. It reads the epoch prereg, which nothing can amend
        (`prereg_amended`), so 16B can implement but not choose them.
    - **Closure r4 (16 Oct; on d00a844: Codex REVISE 1 MAJOR; plan-auditor FAIL 0 BLOCKER / 3 MAJOR / 11 MINOR;
      design by an Opus pass; dispositioned in D74's r4 addendum).**
      - **Incumbent identity (Au M1).** `src/pipeline_config.pipeline_config_hash()` (v1, zero calls): the sha256 of
        the canonical JSON of the retrieval and generation code digests (`ast.dump` without docstrings of retriever,
        bm25_index, embedder, chunker, query_rewrite, generator and grounding), runtime package versions, embedding
        model and collection, index identity (sorted chunk ids, count, model manifest; ids only), BM25 and fusion
        parameters with `top_k`, the rewrite identity, and the generation model, client and prompt. Excluded:
        timeouts and retries, git sha, paths, judge, scoring, eval sets, corpus text. Every eval sidecar carries it.
        `controls.py` requires all draws to carry one hash H equal to the running pipeline's, and records H as the
        `controls` record's `incumbent_config_hash`. Slots 0/0b refuse (`incumbent`) unless the running hash is H.
        Consequence: code and environment are frozen from b7's draws to slot 0; changing the incumbent is a new epoch.
      - **Judge registration (Au M2).** `judge.judge_identity()` (model, prompt, client, verdicts, sample
        `all_eligible`). Every sealed slot refuses before the claim: judge off or None (`judge:off`), any
        `--judge-sample` (`judge:sample`), and an identity other than the prereg's `judge.config_hash`
        (`judge:identity`). Results record the judge hash; 16B compares only results with equal judge hashes; any
        sealed judge error means "unsupported claims do not rise" is not shown.
      - **Claim boundary (Codex r4).** Only `preflight_slot` mints a `PreflightReceipt` (bound to the run's argument
        digest); `SlotToken.claim` needs one; `run_slot` always preflights, and an already-claimed token must match
        the digest (`claim:preflight_mismatch`).
      - **Smaller items.** The draft margin reaches b3 (Au M3); `controls.committed` says before slot 0; quota and
        `min_share` values are pinned by `prereg-check`; the data launcher takes the branch from the prereg, validates
        the prereg in-process before any dispatch record, and names its refusal `prereg_amended`; a data dispatch
        record must carry the prereg sha256; public non-sealed sets are stored with role `regression`.
    - **Closure r5 (16 Oct; on 5e2572a: Codex REVISE 4 MAJOR / 2 MINOR; plan-auditor FAIL 0 BLOCKER / 3 MAJOR /
      7 MINOR; the owner chose the approach; dispositioned in D74's r5 addendum).** This supersedes r4's module list.
      - **Whole-src identity.** `pipeline_config_v2` hashes every `src/**/*.py` except the registered `CODE_EXCLUDED`
        list (records, gates, privacy, display, transport literals; each justified in the module), so a new module is
        hashed by default. Metric-bearing code moved out of `sealed_run.py` into the hashed `sealed_results.py`, and the analysis units, floor test and exclusion aggregation out of `sealed_ledger.py` into the hashed `sealed_groups.py`.
        Runtime = every installed distribution; `endpoint` = the resolved Anthropic base URL; the index adds Chroma's
        search configuration. Timeout and retry literals live in the excluded `llm_transport.py`.
      - **Judge identity** also covers `judge.py`, `eval_v6.py` and `eval_stats.py` (parsing, the named eligibility
        predicate `judge.is_judgeable`, scoring) and the endpoint; results record `scorer_version`; 16B compares
        only results with equal judge hash and scorer version.
      - **Recorded draws (owner-chosen cap).** A development draw counts only if `sealed_tool record-draw` recorded it
        as a `dev_draw` ledger record (per-config index). An epoch may record draws under at most the incumbent plus 4
        candidate config hashes, and only the incumbent's before the `controls` record. `controls.py` takes exactly
        the first recorded draws under the incumbent hash. `declare-batch` needs ≥ `draws_r` recorded draws per
        candidate, and every recorded candidate config must be in the batch (all count toward Holm's K = 4).
        Unrecorded runs never count. This lands the b7 `dev_draw` mechanism now.
      - **Incumbent re-checked** by the evaluator's token check and before storing results (slots 0/0b).
      - **The launcher** runs `validate_prereg` and the pinned values.
      - **Frozen period.** From b7's first recorded draw to slot 0b, any change to a hashed module, an installed
        package or the endpoint makes slots 0 and 0b refuse (`incumbent`). A guard failure on slot 0 whose fix
        touches hashed code therefore means a new epoch (a D64 stop), not 0b; 0b is for failures that need no code
        change.
      - **Drafts:** at 57 migrated families the rule gives 110 (r4 margin included; the r3 "105" is superseded).
        `task.json:count` is written by b3's dispatch code (not yet built), from `count-migrated`.
    - **Closure r6 (16 Oct; on 68182ee: Codex REVISE 5 MAJOR / 1 MINOR; plan-auditor FAIL 0 BLOCKER / 3 MAJOR /
      10 MINOR; the owner chose a consolidating round; dispositioned in D74's r6 addendum).** This supersedes r5's
      exclusion list and draw recording.
      - **Exclusions are inert only.** `CODE_EXCLUDED` keeps only modules that cannot reach a client, row set, rank,
        score, judgement, exclusion or analysis unit (`__init__`, `audit`, `eval_roster`, `eval_split`,
        `llm_transport`, `render`, `sealed_inputs`, `tracing_guard`); `spend`, `sealed_run`, `sealed_ledger`,
        `eval_privacy`, `eval_sets`, `eval_sealed_read` are hashed, and so is `scripts/scan_leaks.py`. A test fails if
        a hashed module imports an excluded one beyond a registered allow-list of inert names. `pipeline_config_v3`.
      - **Clients.** Client construction moved from `spend.py` into the hashed, judge-bound `llm_clients.py`.
      - **Scorer identity.** `scorer_identity()` is computed from the scoring modules, the grounding/caveat constants
        scoring compares with, and the scoring data files (`eval/absorbed_sections.json`,
        `eval/section_inventory.json`); the pipeline hash gains `scoring_inputs`. Results and sidecars carry it;
        16B compares only results with equal judge hash and scorer identity. `SCORER_VERSION` is a label only.
      - **Judge preparation.** `judge_prep.py` owns the refusal check, answer normalisation, `is_judgeable` (complete
        answers only), the judge's own context formatter and item assembly; the judge hash covers it and
        `llm_clients.py`.
      - **Draws are recorded inside the run.** `pipeline eval --record-draw` refuses before any call unless the inputs
        are exactly the frozen tuning and realistic versions, live `build:` expansion, the registered cutoff, both
        hybrid modes, retrieval-only, tracing off; a replay candidate's draw i must replay the incumbent's draw i.
        On completion it writes the artifact and sidecar, appends the `dev_draw` record (run id as receipt), and
        only then writes any report or prints anything. The after-the-fact `record-draw` command is removed. Any
        other private run over a frozen development version appends `dev_run_unrecorded`; `sealed_tool draws`
        reports both per config. `draws_required` is mandatory; a recorded config with too few draws is declared
        `abandoned` and still counts toward K = 4.
      - **Operator residual.** Someone with the code can always run the pipeline outside these entry points (e.g. a
        hand-written script). Such runs are not recorded, never count, and are a process violation that only an
        audit of the operator's activity can find.
    - **Closure r7 (17 Oct; on 52b74fd: Codex REVISE 1 BLOCKER / 3 MAJOR / 1 RESIDUAL; plan-auditor FAIL 0 BLOCKER /
      3 MAJOR / 6 MINOR / 3 RESIDUAL; owner-chosen targeted round; dispositioned in D74's r7 addendum).**
      - **Seams are test-only.** Every injection hook on the slot and eval entry points (scanner, renderer, judge,
        retrieval/generation adapters, draw recorder other than `sealed_run.DrawRecorder`, the meter's inner factory)
        is refused (`seam`) unless `src.seams.TEST_SEAMS` is True, which only the test suite sets (an AST test pins
        that no `src/` or `scripts/` module sets it).
      - **Judge stack.** The judge identity also covers `generator.get_llm`, the metering wrapper and `judge_runtime`
        (the installed distributions on the judge call path, by dependency closure).
      - **Scorer glue.** The absorbed-map loader, hit cut-offs and the sidecar rank projection moved into the hashed
        `scoring_glue.py`, inside the scorer identity. Counted draws carry their scorer identity; `declare-batch`
        refuses a candidate whose counted draws' scorer identity differs from the incumbent's.
      - **Recording verified.** After the recorder returns, the evaluator checks the ledger holds this run's
        `dev_draw` before releasing anything (`draw_ledger`); a `--record-draw` run that fails after its sidecar is
        written logs `dev_run_unrecorded` (`record_failed`); the pipeline hash is re-taken right before a draw is
        recorded and before any slot stores (`incumbent_changed_during_run`).
      - **Modes bound.** `dev_draw` records keep mode, `replay_of`, rewrite and scorer identities; a `replay` entry
        needs replay draws with `replay_of == index`; a `candidate` entry needs build draws whose rewrite identity is
        new and equals its declared one. A draw with any rewrite fallback is voided (`dev_draw_void`, logged, not
        counted).
      - **Minors.** Exposure/twin exclusions are refused once slot 0 or 0b is stored (`exclusion_late`; scan-driven
        `s_hit` stays); the scanner's data inputs are digested into `scan_inputs`; `pipeline eval --sealed-slot 0
        --preflight-only` runs every slot gate without claiming (controls/incumbent reported `pending`), to be run
        before b7's first recorded draw; hashed modules may import only hashed scripts (`sp_launch.py` added).
        `pipeline_config_v4`. New pinned dependency `packaging==26.2` (already installed transitively; now imported
        directly by the judge-runtime closure).
    - **Cosmetic (Au15, Au16, A10, A11, A17).**
      - The owner-hours forecast is recomputed at b5.
      - The tie key is dropped.
      - The non-inferiority wording means exactly its two checks.
      - The quota-short example is labelled.
      - Holm's K is 4 wherever it is mentioned.
b2. **Migration and quotas [C].** First `sealed_tool epoch`: it must print `prereg_sha256=none` or the pushed
    prereg's sha256 with `matches=yes` (else a D64 stop). Golden-v1, realistic-v1 and the 24 T1/T7 rows become v2 development rows (`source`
    `legacy`/`tutorial`) in `eval/private/dev/`, keeping their phrasings (T1/T7 capped at 3); the v1 files stay
    byte-frozen. **Counting** (`count-migrated`): each golden or realistic row is one family, a pair in
    `eval/v2_pairing.json` one realistic family; T1/T7 rows group by exercise (the id without its phrasing letter); a
    refusal family's migrated scope is `refuse`. Golden-only families go to tuning; paired, unpaired realistic and T1/T7
    families to realistic. The refusal drafts follow b1a's rule (pool share 0.275 including the preassigned
    refusals), placed in the registers of the splits that lack them.
b3. **Blind pool [C dispatch; SP drafts].** Data dispatches from the b0 dump and the tutorial folder (the 15 unseen
    tutorials and 7 title-document sets; never T1 or T7): one seed phrasing per family; scope, evidence (inventory
    sections), gaps, `ambiguous`; refusals by b1a's 0.275 rule; lay, modelled and terse registers; written only
    under `sealed/pool/`, every row marked. Attempt 1's family count is `count-migrated`'s `drafts` (b1a r3 F12 +
    r4 D1: max(100, sealed + tuning_min + realistic quota − migrated) + 5; ≈ 110 at 57 migrated), written into
    `task.json:count` with `refusals` and `lay_min`: about half tutorial-sourced (about 3 per unseen tutorial, at most 1
    per title-document set), the rest handbook-drafted. Families on a b0 conflict label are dev-only.
b4. **Clearance, split, paraphrase [SP; C].** The SP gives the all-pairs twin verdict; the orchestrator covers the
    development-only pairs (golden, realistic, T1/T7, heldout-v1) from the pairing map and its own reading. `split`
    makes up to 3 attempts per round (b1a) and releases the first feasible one. The SP then paraphrases every family to 2–3 phrasings (migrated ones
    keep theirs and are topped up to 2); the orchestrator receives only the development side. **Fixed order (R3):**
    round-1 split → b5 validation → SP `recheck` on final content → round 2 → freeze (b6).
b5. **Validation, escalation, calibration (D75) [owner; SP applies sealed sheets, orchestrator development ones].** The
    owner validates every sealed family, every refusal or ambiguous label anywhere, every new development label (fast
    review), every SP paraphrase of a migrated family, and the flagged items: T01-Bb; realistic-v1's planning-extension
    refusal (relabelled under D61); T07-Ab/T07-Ba secondary groups; the pre-1975 date conflict (b0). **Escalation:**
    decision E makes `escalate` write a form (Appendix A) under `eval/private/sealed/work/escalations/` (sealed items) or
    `eval/private/work/escalations/` (development items only): item ref, phrasing(s) and the owner's one-line doubt, never the owner's key, so the
    colleague answers independently. Only the owner forwards it (email or paper, never via a third-party model), logs
    date and item refs in `eval/private/sealed/work/forwarding_log.csv` (no names) and types the colleague's full key into the sheet; both
    keys are kept, the owner decides, the orchestrator sees counts. Open after 10 days → the owner decides
    (`second_status: missing`), or a still-doubted sealed family is rejected. **Disagreement** per dimension: scope;
    AND/OR group structure (a missing or extra required group, or a different alternative set, under related matching);
    partial gaps; each rate with a Wilson CI, labelled "an owner-selected sample of doubted items, not a population
    rate" (D71 Q5). The owner-hours forecast is recomputed here from the actual counts (D75). **Optional calibration**
    (if the owner confirms the colleague can do it): 15 approved families drawn once, after every split is frozen, from
    the frozen files with the epoch prereg's seed (5 refuse or partial, 10 answer; ledger-recorded, b1a), labelled from
    the question alone; agreement per dimension as a
    random n = 15 sample, or D75 says "not done". Rejected families go to `sealed/quarantine.jsonl` (marked) or
    `eval/private/dev/quarantine.jsonl`.
b6. **Freeze, clearance, audits [SP; C].** `freeze --split heldout-v2`, then tuning-v2 and realistic-v2 after their
    validation; a final development clearance pass (SP) runs before slot 0. **Development freezes at slot 0 (R4):** the
    versions slot 0 uses serve every slot of the epoch; a later correction makes a new development version (set-version
    hook, its own artifact and draws, its own clearance pass, whose twins give a4 group exclusions) and never mutates
    slot-0 inputs. `audit` runs at release, at each freeze, before slot 0 and at the phase gate; counts go into D75.
b7. **Development baseline, controls, cost probe (D76) [C].** First `pipeline eval --sealed-slot 0 --preflight-only` (b1a r7; every gate but controls/incumbent must pass, else fix before any draw). On the frozen development sets: offline v6 (zero calls)
    over tuning-v2, realistic-v2 and heldout-v1; R = 5 retrieval-only live draws (`build:`, each with its own
    `--approved-eur`): per-family flip frequency and the rate range. **Each draw over the frozen development sets is
    recorded in the ledger as it completes (a `dev_draw` record: index, config hash, artifact and rows-sidecar
    sha256s, mode, scorer identity; recorded in-run by `pipeline eval --record-draw`, b1a r6–r7); only the first 5
    recorded count** for the controls and the development evidence
    (b1a). Draw 1 is the **development artifact** (development files are frozen once, so it is never voided; a pipeline change is a new incumbent, i.e. a new epoch, b1a r5). The control list (b1's rule) is computed once from those draws on the frozen
    development versions and committed as the ledger's `controls` record (digest in the mirror) before the batch
    declaration and before slot 0 (the gate refuses slot 0 without it); it is never recomputed. **Cost probe** (cost confirmed first, 16A.8):
    one metered public run (golden-, realistic-, heldout-v1, judge on, `--results eval/results_partial.md`,
    `--approved-eur 3`), plus the meter's worst case priced in-process over the final development prompts.
b8. **Slot 0 [C].** With the week's remaining cap ≥ 2 × the forecast: `pipeline eval --sealed-slot 0` over heldout-v2,
    realistic-v2, tuning-v2 and `--regression` heldout-v1 (four modes, refusals, completeness, judge), live expansion
    (the gate refuses `build:`; the run directory's `expansion_artifact.json` is the artifact, and its sha256 in the
    `slot_stored` record is what slots 1–4 replay as `slot0_canonical`, b1a), `--approved-eur` 2 × the forecast. Canonical → `results_v6.md` and a ledger outcome; otherwise guard names,
    then 0b once (only for a failure that needs no change to hashed code, b1a r5); a second failure goes to the owner.
b9. **Docs [C]:** D74 (prereg, the owner's choice, under C1 the D57-addendum reversal), D75 (data and validation, owner
    hours, the D71 Q5 narrowing), D76 (re-baseline: cost, results, MDE at the final N); README headline with the source
    split and the D46 caveat; b1's three adoption texts; `Current phase`, `Next:`.

**Owner hours (forecast):** sealed 60 families × 7 min = 7.0 h; new development families 40 × 4 min = 2.7 h; T1/T7 rows
24 × 3.5 min + flagged items 4 × 15 min = 2.4 h; v1 refusal labels 11 × 2 min = 0.4 h; SP paraphrases of migrated
single-phrasing families ≈ 55 × 1.5 min = 1.4 h; escalations (~10%) 10 × 15 min = 2.5 h; confirm sheets ≈ 0.5 h; round-2 replacements (if needed) ≈ 10 families × 7 min = 1.2 h.
**Required ≈ 17–18 h**; optional calibration + 0.75 h; the colleague ≈ 3 h. Realistic only as ~11 sessions of 1.5 h over
3–4 weeks, so b5 is staged: sealed sheets and their escalations first (≈ 10 h, the critical path), development sheets
second (≈ 7 h). Third phrasings, a late calibration and colleague queries go to a later version (**set-version
hook**, D71 Q3: a new registry name or sealed epoch via Phase 20's candidate review, never the frozen sealed set).
**Cost (forecasts; P8):** slot 0 ≈ 360 rows (sealed ≈ 120, tuning ≈ 125, realistic ≈ 85, heldout-v1 28), one generation
and one live rewrite each, judge on ≈ 70%: at the D69 anchor (€0.0200 per row) ≈ €7, planned at €14; five draws priced
from the final development prompts by the meter's worst-case function (≈ €3.5 at a 1,128-token prompt); the probe ≤ €3.
Phase ≈ €12, planned ≈ €21; 0b waits for a new UTC week unless the remainder covers it. The only hard bounds are the
meter's run limits and the €40 UTC-week cap (exits 6 and 3); lifting the cap is a D64 stop.
**Power (P8; `eval_stats`, recomputed 12 Oct, directional power 0.8):** for families, N = eligible answerable sealed
families, 42–45 at freeze and ≥ 35 by the floor (refusal families excluded, `src/eval_v6.py:137`). The figures below
are for **independent singleton families**, where the sign-flip primary equals exact McNemar; the planned group
structure moves power either way (Codex r6 showed a grouped case above the singleton figure), so the prereg's Monte Carlo
table under both correlation models is the binding one, recomputed by `power` at freeze (R2). Family MDE at
discordance 0.2 / 0.3 / 0.5:
N = 35: – / 0.26 / 0.34 at α = 0.05, – / 0.29 / 0.40 at α = 0.0125; N = 45: 0.19 / 0.23 / 0.31 and – / 0.27 / 0.35. Power
at the MWE (0.10, discordance 0.3): 0.11–0.16 at α = 0.05, 0.04–0.05 at 0.0125. Sampling error and the control rule can
still produce FAIL. **Negative branch:** if freeze is infeasible after round 2, 16A-2b merges the content-free
records and the offline development baseline (private data stays local), writes no `results_v6.md`, heads the PR with
the shortfall, and the owner decides whether that is v2.5.0 (D64: eval protocol).

**16A-2b acceptance (Tier-2; evidence is counts and hashes only):** the owner's choice in D74 before the prereg commit;
the prereg pushed before the first `data` dispatch record; `prereg-check` passes on the **committed** prereg; attempts,
split and freeze in the private ledger, kinds and digests in the mirror; sealed ≥ 50 families and ≥ 35 eligible
answerable, tuning ≥ 60 and realistic ≥ 42 families, each with 2–3 phrasings, every split in the band (rows reported),
realistic tutorial share ≥ 25%; every sealed, refusal and ambiguous label owner-approved and hash-bound; escalation
counts and disagreement per dimension; calibration done or "not done"; every audit hit has an exclusion
record, and unexcluded hits = 0 (R5); baseline, draws and controls
recorded; probe, forecast and slot 0 in D76; `results_v6.md` from a canonical run within the week's cap; `scan_leaks
--merge-gate --base main --pr <n>` clean. **Stops (D64):** b1's owner question; the weekly cap (exit 3) or lifting it;
split infeasible (a 4th round-1 attempt) and freeze infeasible after round 2 (b1a); a development band unreachable
at b2 (`band_unreachable`); a prereg amendment (`prereg_amended`) or new epoch; the floor breached; both private ledgers lost; a sealed
hit in pushed history; the owner's `harness-init --refresh`. **Gates:** prereg plan gate → b0–b8 → `/phase-gate
16A-2b` → Codex merge review → PR, CI → merge and tag **`v2.5.0`** under D64.

**Design problems** (the 10 Oct outline, full text at `4b78843`; Cx = Codex, Au = plan-auditor, rN = round):
- **P1 Blindness** (who drafts, checks, dedups, paraphrases; "never retrieved" evidence; window-exempt and short
  questions) [Cx r3 5; Au r2 11, 12; Au r3 13, 14 (eligibility), 31]
- **P2 Pool, dedup, split** (twin dedup and the `twins` record; pool format, digest, freeze; pairing map; seed; phrasing
  counts; inventory on new labels; colleague intake; `min_share`; the 24 tutorial rows never sealed) [Cx r1 6 (pool),
  r2 10; Au r2 13, 16, 17, 24; Au r3 1 (edges), 15 (applied), 20 (values), 21, 22]
- **P3 Validation** (second review per 16A.4; return channel; approvals bound to row hashes; colleague deadline;
  forwarding; quarantine; quotas after demotion; disagreement rate) [Cx r3 9, 10; Au r2 15, 16 (demotion), 20; Au r3 2,
  3, 12]
- **P4 Sealed lifecycle** (pre-seal protection; trusted processors; diagnostics; exposure; aggregates-only rendering;
  S-needles; side channels) [Cx r1 1, r2 3, 5; Au r2 10, 19, 21; Au r3 7, 23]
- **P5 Pre-registration and slots** (core vs ledger; reservation; states; spare 0b; slot-0 expansion; declared batch;
  scan abort; out-of-tree state; MWE; Holm; re-epoch) [Cx r1 4, 9, 15 (re-epoch), r2 1, 6, 9, r3 7, 12, 13; Au r2 5,
  22, 29; Au r3 8, 18 (slots), 21 (formats)]
- **P6 Controls and artifacts** (a control rule without "zero flips anywhere"; artifacts for slots; the development
  artifact; variance draws) [Cx r1 10 (draws), r3 6; Au r2 1, 9; Au r3 4, 5]
- **P7 Canonical v6** (sealed headline; replay guard; one writer; all-or-nothing vs one spare) [Cx r2 8 (v6); Au r2 3;
  Au r3 17, 30]
- **P8 Cost, power, negative branch** [Cx r1 5, 15 (estimate), r3 11; Au r2 28; Au r3 25 (Tier-2 MDE, D57)]

### 16B — obligations registered by 16A-2b (outline; 16B gets its own spec and plan gate)
16B may implement these but not choose them: they are fixed by the epoch prereg, which nothing can amend
(`prereg_amended`).
- Replace `sealed_run.check_candidate_identity` (today it always refuses `identity`) with the registered check: the
  running `pipeline_config_hash()` equals the batch entry's `config_hash`, before the claim; only then lift
  `CONFIRMATORY_EMBARGO`, in the same reviewed change.
- Declare the batch (`declare-batch`, ≤ 4 entries, `replay` or `candidate:<rewrite hash>`) before any of slots 1–4.
- The adoption analysis reads the epoch prereg: development evidence over the first 5 draws recorded under each
  config hash (b7 `dev_draw`), unsupported claims vs slot 0's judged rate (equal judge hashes only; any sealed judge
  error means not shown), control flips in raw hybrid or hybrid+rewrite, the sealed guard's two checks, Holm with
  K = 4.
- Order: the embargo lift and the identity check land in one reviewed change BEFORE any candidate draw is recorded,
  because `sealed_run.py` and `sealed_ledger.py` are hashed and a later change makes a recorded candidate
  unreproducible. With fewer than 5 draws it can then only be declared `{"config_hash": …, "expansion": "abandoned"}`;
  with 5 or more it can be declared but its slot will refuse `identity`. Either way it spends one of K = 4. Recording
  a candidate draw before that change is not refused by code; it is a planning error the order prevents.
- 16B must not edit any module in the scorer identity (`scoring_identity.SCORING_CODE_MODULES`) or the judge identity
  (`judge.JUDGE_CODE_MODULES`, `generator.get_llm`, the metering wrapper) or a judge-path package: each makes every
  comparison with slot 0 refuse.
- Comparisons need an equal judge hash AND an equal `scorer_identity` between the candidate's slot and slot 0.
- b7's flip-frequency report and worst-case pricing tool live in `scripts/` (unhashed tools), never in `src/`, so
  building them after the draws does not change the incumbent; they must not edit `eval_stats.py`/`eval_v6.py`
  (judge-bound, registered in the prereg).
- Whether to separate the judge from the generation model (today `JUDGE_MODEL = GENERATION_MODEL`, so a candidate that changes the generation model or
  client kwargs is refused `judge:identity`; a generation-prompt change is not, because the judge has its own prompt) is an owner question for 16B.

### 16A-2 dispositions (the 47 DEFERRED-16A-2 rows of the 16A-1 table below; severities there)
a* = 16A-2a items, b* = 16A-2b items, 2a-(x) = 16A-2a criteria.

| 16A-1 rows | P | Closed by (spec v4) |
|---|---|---|
| Cx r1 1; Cx r2 3; Au r2 21; Au r3 23 | P4 | a3 allowlisted printer (sealed totals only, no chapter or stratum), a5 sealed rendering (aggregates only; rows, sidecar and artifact only in `sealed/runs/<id>/`), a6 audit; 2a-(c), (e), (g). **Residual (D72):** spend-ledger token counts |
| Cx r1 4; Cx r2 1, 9; Cx r3 7, 12, 13; Au r2 5, 22, 29; Au r3 8, 30 | P5, P7 | a4: out-of-tree ledger with a private copy and a kinds-and-digests mirror; claim → store → intent → render → consumed; recovery without fresh calls; 0b only before any intent; exclusions instead of file changes. a5: canonical decided before rendering. b1: prereg immutable once pushed (later changes are D64 amendments); 2a-(f), (g) |
| Cx r1 9; Cx r2 6 | P5 | b1: one non-adaptive batch of ≤ K_max per epoch, declared before slot 1, Holm within it. **Residual (D74):** slot 0's descriptive aggregates precede the batch |
| Cx r1 5; Cx r3 11; Au r2 28 | P8 | Cost and power blocks (forecasts, probe, in-process pricing, cap); negative branch (content-free records only, private data local, PR headline = shortfall); b7; a9 (`docs/harness.md`) |
| Cx r2 10; Au r2 16, 24; Au r3 21 | P2, P3, P5 | a3: `ids`; split attempts with the pool digest; private split records replayed by `freeze`; sealed band repair by whole groups to quarantine (R3); formats of pool, sheets and approvals; b1 seed and quota formulas; 2a-(e) |
| Cx r3 5; Au r2 11, 12 | P1 | The SP drafts, clears and paraphrases (b3, b4) in a staging directory with no Bash (a7), sealed calls only in a claimed slot (a5), `return` validation (a3), S-needle audit (a6); 2a-(b), (k), 2a Tier-2. **Residual (D72):** `pipeline query` by a text holder |
| Cx r3 6; Au r2 1 | P6 | b1 controls rule (C1 or C2, PENDING-OWNER); `controls.py` (a8) on frozen development sets after the draws, sha256 in the mirror before slot 1 (b7); 2a-(j) |
| Cx r3 9, 10; Au r2 15, 20; Au r3 2, 3, 12 | P3 | b5 (owner validation of every sealed, refusal and ambiguous label; escalation form; owner-only forwarding; 10-day deadline; disagreement per dimension; D71 Q5 narrowing disclosed) + a3 `sheet`/`apply` (approvals bound to final row hashes); 2a-(e), (j) |
| Au r2 3; Au r3 17 | P7 | a5: canonical v6 only live (slot 0/0b), never a replay; it writes only `results_v6.md`; `results.md` keeps v5's writer; 2a-(g) |
| Au r2 9; Au r3 4, 5 | P6 | b7 development artifact (draw 1; a set change means a re-draw) and five draws; b8 slot-0 artifact over every set, replayed by later slots; a6 retrieval-only replay |
| Au r2 10 | P4 | b3: pool rows born marked under `sealed/pool/`; a1 `sealed` key, which 2a-(d) checks is `true` |
| Au r2 13; Au r3 13 | P1, P2 | a3 `twins` (all-pairs SP verdict over drafts, development, heldout-v1, public, quarantine) and `freeze` (`recheck` on final content); b3 conflict labels; b4; 2a-(e) |
| Au r2 17; Au r3 22 | P2 | b1 `min_share` (D71 Q4); b2 counting, pairing map, the 24 T1/T7 rows preassigned and never sealed; b4 phrasing range; set-version hook (owner-hours block) |
| Au r3 31 | P1 | a2: short sealed questions matched whole; a draft sharing a window with a public question is sealed-ineligible (Unit, R7); development and handbook windows are exempt (S10), sealed→development leakage is caught by a3's freeze screen (R6); 2a-(d), (e) |

**FIXED rows' 16A-2 remainders:** Cx r1 6 (pool) a3; Cx r1 10 (draws) b7; Cx r1 15 a4, cost block; Cx r2 5 (S-needles)
a2; Cx r2 8 (v6) a5; Au r2 19 D72–D76 carry counts and ids only and are merge-gate scanned; Au r3 1 (edges) a3; Au r3 7
(sealed hits) a2, a4; Au r3 14 (eligibility) a3; Au r3 15 (inventory) a3, b3; Au r3 18 (slot state) a4; Au r3 20
(values) b1; Au r3 25 (MDE, D57) power block, b1.

### 16A-2 plan-gate r1 dispositions (spec v1 `2527a77`)
Codex `gpt-6.1-sol` REVISE (2 BLOCKER / 7 MAJOR / 2 MINOR); plan-auditor FAIL (2 / 10 / 20). 43 ACCEPTED, 0 REBUTTED.
S1–S7 were the r1 structural decisions; r2's S8–S12 superseded parts of S3 and S4, as noted.

| Source | # | Disposition | Where (spec v4) |
|---|---|---|---|
| Cx | 1 | ACCEPTED (S2): the frozen sealed file is immutable; exposures become family exclusions dropped from both arms | a3 `freeze`; a4; 2a-(f) |
| Cx | 2 | ACCEPTED (S3; completed in r2 by S11): all-pairs SP verdict, `recheck` on final content, post-freeze clearance | a3; a4; b6; 2a-(e) |
| Cx | 3 | ACCEPTED (S3); superseded by S8 and r3's R2: twins merged or dropped at the source, family unit, Durkalski by split group as the primary | Unit; a3; b1 |
| Cx | 4 | ACCEPTED: final quotas include preassigned families; counting procedure; refusal deficits drafted | b1; b2 |
| Cx | 5 | ACCEPTED (S7): N = eligible answerable families; "never FAIL" removed; D57 re-open not met by sealed alone | Power block; b1 |
| Cx | 6 | ACCEPTED (S2; completed in r2 by S11): out-of-tree claim → store → intent → render → consumed | a4; 2a-(f) |
| Cx | 7 | ACCEPTED (S6): no `SlotToken` while tracing is configured; spy-transport tests | a5; 2a-(b) |
| Cx | 8 | ACCEPTED: disagreement per dimension, both keys kept | b5; a3; 2a-(j) |
| Cx | 9 | ACCEPTED: `src/pipeline.py:698` counts `build:` as live; mocked CLI test with exits 3 and 6 | a6; 2a-(h) |
| Cx | 10 | ACCEPTED: costs are forecasts; in-process pricing; per-run limits; the cap is the hard bound | Cost block; b7 |
| Cx | 11 | ACCEPTED: schema-valid fixtures, free-text and model-output canaries, success path proven | 2a-(c) |
| Au | 1 | ACCEPTED (S1): 16A-2a (`v2.4.0`) and 16A-2b (`v2.5.0`) | Header; 16A-2a; 16A-2b entry |
| Au | 2 | ACCEPTED: as Cx 4 | b1; b2 |
| Au | 3 | ACCEPTED (S4; extended in r2 by S9: staging directory, `env -i`) | a7; 2a-(i); 2a Tier-2 |
| Au | 4 | ACCEPTED (S4): as Au 3 | a7 |
| Au | 5 | ACCEPTED (S5; S10 in r2: development windows exempt, whole-value short matches) | a2; 2a-(d) |
| Au | 6 | ACCEPTED (S5): as Au 5 | a2; 2a-(d) |
| Au | 7 | ACCEPTED (S2): as Cx 1 and Cx 6 | a4; 2a-(f) |
| Au | 8 | ACCEPTED: development-artifact invariance dropped (re-draw on a set change); retrieval-only replay | b7; a6; 2a-(h) |
| Au | 9 | ACCEPTED: `--regression` fourth set | a6; 2a-(h) |
| Au | 10 | ACCEPTED (S3); superseded by S8: as Cx 3 | Unit |
| Au | 11 | ACCEPTED (S7): power on eligible answerable families; adoption and controls are the owner's; D46 source split | Power block; b1; a5 |
| Au | 12 | ACCEPTED (S7): as Au 11 | b1 (PENDING-OWNER) |
| Au | 13 | ACCEPTED (S4): as Au 3 | a7 |
| Au | 14 | ACCEPTED (S4): as Au 3 | a7; 2a-(i) |
| Au | 15 | ACCEPTED (S4): as Au 3 | a7; 2a Tier-2 |
| Au | 16 | ACCEPTED: "beyond the 12 Oct table-of-contents read", in CLAUDE.md too | Tutorials; a9 |
| Au | 17 | ACCEPTED: dispatch logs outside `sealed/` plus out-of-tree records; prereg on the remote before a data dispatch | a7; 2a-(i); 16A-2b Tier-2 |
| Au | 18 | ACCEPTED: freeze per split; sealed needs no development approval | a3; b6; 2a-(e) |
| Au | 19 | ACCEPTED: round 2 before freeze only (r2: it replaces rejections) | a3; b4 |
| Au | 20 | ACCEPTED: 2–3 phrasings; migrated families topped up | b4; 16A-2b Tier-2 |
| Au | 21 | ACCEPTED: hours recounted with paraphrases of migrated families (≈ 17 h) | Owner hours |
| Au | 22 | ACCEPTED (S5): as Au 5; pool move and memory audit | a3; a6 |
| Au | 23 | ACCEPTED: read-only sealed path (r2: a separate loader module) | a1; 2a-(b) |
| Au | 24 | ACCEPTED: the sealed sidecar lives only in `sealed/runs/<id>/` | a5; 2a-(g) |
| Au | 25 | ACCEPTED (S2): as Cx 1 and Cx 6 | a4; 2a-(f) |
| Au | 26 | ACCEPTED (S3); superseded by S8: split-group edges; development and heldout-v1 paths defined | a3; b4 |
| Au | 27 | ACCEPTED: committed, content-free `eval/conflict_sections.json` | b0; b3 |
| Au | 28 | ACCEPTED: as Cx 11 | 2a-(c) |
| Au | 29 | ACCEPTED (S7): as Au 11 | b1; power block |
| Au | 30 | ACCEPTED (S7): C1 reverses the D57 addendum, in the owner question; D74 records it | b1 |
| Au | 31 | ACCEPTED (S6): as Cx 7 | a5; 2a-(b) |
| Au | 32 | ACCEPTED: `prereg-check` on the committed prereg | a8; 16A-2b Tier-2 |

### 16A-2 plan-gate r2 dispositions (spec v2 `15e4d02`, 12 Oct)
Codex REVISE (9 of 11 closed; N1–N4 MAJOR, N5 MINOR); plan-auditor FAIL (1 / 13 / 12). All ACCEPTED or closed by **S8**
family unit, **S9** SP staging under `env -i`, **S10** development windows exempt, **S11** closed post-freeze
transitions, **S12** private ledger and crash-safe moves.

| Source | # | Sev. | Disposition | Where (spec v4) |
|---|---|---|---|---|
| Cx | 2 | (r1) | ACCEPTED (S11): SP `recheck` on final content before freeze; later development corrections get a clearance pass and exclusions, never moves | a3 `freeze`; a4; b6; 2a-(e), (f) |
| Cx | 6 | (r1) | ACCEPTED (S11): a durable intent disqualifies 0b, interrupted render included; recovery republishes | a4; 2a-(f) |
| Cx | N1 | MAJOR | Closed by S8: no component map; the twins record and exclusion list are freeze-bound and required by the `SlotToken` and canonical guards | a3; a5; 2a-(b) |
| Cx | N2 | MAJOR | Closed by S8: a family has one scope (`src/eval_schema.py:469–481`), so `src/eval_v6.py:137` defines the population | Unit; power block |
| Cx | N3 | MAJOR | Closed by S8: 60 sealed in the band gives ≥ 42 answerable; attempts release only a feasible split; round 2 quota = pinned + replacements | a3 `split`; b1; 2a-(e) |
| Cx | N4 | MAJOR | ACCEPTED: `probe` dispatches use a synthetic prereg and their own log; chronology applies to the first `data` dispatch | a7; 2a-(i); 16A-2b Tier-2 |
| Cx | N5 | MINOR | ACCEPTED (Codex's diff): protected entries refuse; read-only loader, validator and scanner accept with zero calls | a1; 2a-(b) |
| Au2 | 1 | BLOCKER | Closed by S8 (as N3) | Unit; a3; b1 |
| Au2 | 2 | MAJOR | ACCEPTED: builder, source and check for the handbook dump, pairing map and conflict labels | b0; a8; 2a-(j) |
| Au2 | 3 | MAJOR | ACCEPTED: all four tracing variables set false before importing `src`; `tracing_is_enabled()` asserted | a5; 2a-(b) |
| Au2 | 4 | MAJOR | ACCEPTED: `git fetch` + `merge-base --is-ancestor`, tested with a later push | a7; 2a-(i) |
| Au2 | 5 | MAJOR | Closed by S9: staging directory outside the repository with copied inputs | a7; 2a Tier-2 |
| Au2 | 6 | MAJOR | Closed by S9: `env -i` keeps only HOME, PATH, USER, LANG, TMPDIR | a7; 2a-(i); 2a Tier-2 |
| Au2 | 7 | MAJOR | Closed by S9: file tools confined to staging; memory-write probe | a7; 2a Tier-2 |
| Au2 | 8 | MAJOR | Closed by S12: the mirror holds kinds and digests only | a4 |
| Au2 | 9 | MAJOR | Closed by S11: SP `recheck` dispatch on final content | a3 `freeze`; b6 |
| Au2 | 10 | MAJOR | Closed by S10: development windows exempt; whole-value short matches; false-positive test | a2; 2a-(d) |
| Au2 | 11 | MAJOR | ACCEPTED: audit covers `eval/private/dev/` and `work/`; escalation forms rooted under each `work/` | a6; b5; 2a-(k) |
| Au2 | 12 | MAJOR | ACCEPTED: pressure-tester, plan-auditor and phase-gate barred from sealed, tutorials and staging | a9 |
| Au2 | 13 | MAJOR | ACCEPTED: heldout-v1 retired to regression in D73; CLAUDE.md and `docs/harness.md:43` updated | a9 |
| Au2 | 14 | MAJOR | ACCEPTED: prereg gated unpushed, push = commitment; quota formulas | 16A-2b entry; b1 |
| Au2 | 15 | MINOR | Closed by S10: audit hits carry family ids for exclusions | a2; a6 |
| Au2 | 16 | MINOR | ACCEPTED: row hash excludes `sealed` and status keys; post-move counts checked | a1; a3 `freeze` |
| Au2 | 17 | MINOR | ACCEPTED: `slot0.json` / `slot0b.json`; the ledger names the replayed artifact | b8 |
| Au2 | 18 | MINOR | ACCEPTED: unpaired realistic-v1 rows go to realistic | b2 |
| Au2 | 19 | MINOR | ACCEPTED: band in families, rows reported; 2–3 phrasings checked; T1/T7 capped at 3 | a3 `freeze`; b2 |
| Au2 | 20 | MINOR | ACCEPTED: CLAUDE.md zero-call line gains the `build:` exception | a9 |
| Au2 | 21 | MINOR | ACCEPTED: bake-off skill and harness principle 3 follow the owner's choice | b1; b9 |
| Au2 | 22 | MINOR | ACCEPTED: separate read-only loader module; AST hygiene test; D65 reconciled in D72 | a1; 2a-(b) |
| Au2 | 23 | MINOR | ACCEPTED: option (A) discloses its departure from 003:255 | b1 |
| Au2 | 24 | MINOR | ACCEPTED: all project directories from this path (worktrees, subagents) and staging; cloud sessions a residual | a6 |
| Au2 | 25 | MINOR | Closed by S9: `--append-system-prompt` (listed) carries the brief's text | a7 |
| Au2 | 26 | MINOR | Closed by S12: copy, fsync, verify, delete under a move journal; tested | a3 `split`; 2a-(e) |

### 16A-2 plan-gate r3 dispositions (spec v3 `bdca417`, 12 Oct)
Codex: r2 items closed; Cx3-1, Cx3-2 MAJOR, Cx3-3 MINOR. plan-auditor: 1 conditional BLOCKER, 9 MAJOR, 8 MINOR. All
ACCEPTED under **R1** edges only among sealed-eligible drafts, **R2** clustered primary, **R3** one replacement round,
**R4** development frozen at slot 0, **R5** audit criterion, **R6** freeze leakage screen, **R7** public-window rule.

| Source | # | Sev. | Disposition | Where (spec v4) |
|---|---|---|---|---|
| Cx3 | 1 | MAJOR | ACCEPTED (R2): Durkalski by split group is the primary; family McNemar the sensitivity; power on groups and families | Unit; b1; power block; 2a-(j) |
| Cx3 | 2 | MAJOR | ACCEPTED (R1): demotion, exclusion and recheck act on whole groups; invariant checked after every move | Unit; a3 `freeze`; a4; 2a-(e) |
| Cx3 | 3 | MINOR | ACCEPTED: tracing variables are scrubbed and the run proceeds; refusal only if tracing stays on or a tracer exists | a5; 2a-(b) |
| Au3 | a1 | MAJOR | ACCEPTED: only a canonical render writes an intent; a non-canonical slot 0 leaves 0b available once | a4; 2a-(f) |
| Au3 | a2 | BLOCKER | ACCEPTED (R1): as b1 | Unit; a3 `twins` |
| Au3 | a3 | MAJOR | ACCEPTED (R3): group-level band repair restored; one replacement round; "freeze infeasible after round 2" stop | a3; b4; stops |
| Au3 | a4 | MAJOR | ACCEPTED: `handbook_sections.py` and the conflict-label check tested | a8; 2a-(j) |
| Au3 | a5 | MINOR | ACCEPTED: as Cx3-3 | a5; 2a-(b) |
| Au3 | a6 | MAJOR | ACCEPTED (R6): development files leave the needle audit; the freeze screen catches sealed→development leakage | a3 `freeze`; a6; 2a-(e), (k) |
| Au3 | a7 | MAJOR | ACCEPTED (R7): a public-window draft is sealed-ineligible; the Au r3 31 row updated | Unit; 47-row table |
| Au3 | a8 | MINOR | ACCEPTED: (d) checks `sealed` is `true` | 2a-(d) |
| Au3 | b1 | BLOCKER (cond.) | ACCEPTED (R1): only edges among sealed-eligible drafts are passed; primary evidence section defined | Unit; a3 |
| Au3 | b2 | MAJOR | ACCEPTED (R3): round 2 also fills development shortfalls; residual failures are a listed stop | a3 `split`, `freeze`; stops |
| Au3 | b3 | MAJOR | ACCEPTED: SP output only to staging files; the launcher prints `return`'s line alone | a7; 2a-(i) |
| Au3 | b4 | MAJOR | ACCEPTED: `SlotToken` checks the slot's declared expansion mode, `--rewrite-candidate` included | a5; 2a-(b) |
| Au3 | b5 | MAJOR | ACCEPTED: staging deleted in `finally`, swept on launch and before Codex; Codex clause extended | a7; a9; 2a-(i) |
| Au3 | b6 | MAJOR | ACCEPTED: register-based eligibility (realistic: lay, terse or tutorial-sourced) | a3 `split`; 2a-(e) |
| Au3 | b7 | MAJOR | ACCEPTED (R5): every hit has an exclusion or retirement record; unexcluded hits = 0 | 16A-2b Tier-2 |
| Au3 | b8 | MAJOR | ACCEPTED (R4): development frozen at slot 0; corrections make a new version; controls computed once | b6; b7 |
| Au3 | c1 | MINOR | ACCEPTED: the scan is a pre-publication step (hit → exclusion, re-render), not a canonical guard | a4; a5; 2a-(g) |
| Au3 | c2 | MINOR | ACCEPTED (R2): slot 0 descriptive; Durkalski tested on synthetic paired arms | a5; 2a-(j) |
| Au3 | c3 | MINOR | ACCEPTED: opaque-id locations listed once; every `work/` path rooted | Trusted processors; b5; 2a-(c) |
| Au3 | c4 | MINOR | ACCEPTED: AST rule defined with a script allowlist and a planted-violation test | a1; 2a-(b) |
| Au3 | c5 | MINOR | ACCEPTED: stored results without an intent are rendered from storage; consumed only once rendered | a4; 2a-(f) |
| Au3 | c6 | MINOR | ACCEPTED (R6): (k) no longer plants in development files | 2a-(k) |
| Au3 | c7 | MINOR | ACCEPTED: "every sealed row is approval-bound" | a5; 2a-(b) |
| Au3 | c8 | MINOR | ACCEPTED: the real probe checks read denial, no MCP, no memory directory; the scrub is 2a-(i)'s; managed settings disclosed | a7; 2a Tier-2 |

### 16A-2 plan-gate r4 dispositions (spec v4 `a010d32`, 12 Oct)
Codex r4 closed Cx3-1…3 and raised Cx4-1, Cx4-2 (MAJOR); the r3 plan-auditor's closure check closed all 24 Au3 rows and
raised Au4-1…3 (MAJOR) and Au4-4 (MINOR). All are accepted and fixed in v5 by the orchestrator:
| Src | # | Sev | Disposition | Where |
|---|---|---|---|---|
| Cx4 | 1 | MAJOR | ACCEPTED: groups are the connected components of all relations over drafts and pinned families, computed before filtering; ineligibility covers the whole group (P–A–B case tested) | Unit; 2a-(e) |
| Cx4 | 2 | MAJOR | ACCEPTED: the primary test is exact McNemar on group-collapsed outcomes (size ≤ α exactly; power and MDE at N = eligible groups); Durkalski is an approximate sensitivity | Inference; b1; option (B); 2a-(j) |
| Au4 | 1 | MAJOR | ACCEPTED: round-2 families are paraphrased, owner-validated and rechecked before freeze; rejection → dropped, shortfall → the listed stop; hours +1.2 h | a3; 2a-(e); owner hours |
| Au4 | 2 | MAJOR | ACCEPTED: a round-2 draft related to a round-1 sealed group joins it; related to both that group and pinned development → dropped; the round-1 straddle is closed by whole-group ineligibility | Unit; 2a-(e) |
| Au4 | 3 | MAJOR | ACCEPTED: same fix as Cx4-2 | Inference |
| Au4 | 4 | MINOR | ACCEPTED: SP stdout/stderr logs move to `eval/private/sealed/work/`, so the precheck passes | a7 |
| Au4 | — | residue | ACCEPTED: (k) plants in `eval/private/work/`; the criterion names only exclusion records; the forwarding log is rooted under `sealed/work/`; sealed escalations go only under `sealed/work/` | (k); 2b Tier-2; b5 |

### 16A-2 plan-gate r5 dispositions (spec v5 `eeba82a`, 12 Oct)
Codex r5 closed Cx4-1 and Cx4-2 and raised Cx5-1 (MAJOR); the plan-auditor's closure check closed Au4-1…4 and raised
Au5-1 (MAJOR, the same as Cx5-1) and Au5-2 (MAJOR, conditional). Fixed in v6 by the orchestrator:
| Src | # | Sev | Disposition | Where |
|---|---|---|---|---|
| Cx5 | 1 | MAJOR | ACCEPTED: the estimand is the family-level difference (the MWE's scale); the primary is an exact cluster sign-flip test on group net discordance (size ≤ α under the paired-randomisation null, no within-group masking, exact McNemar with singletons); power by seeded Monte Carlo under two correlation models; Codex's fixture is a test | Inference; a8; b1; options; 2a-(j) |
| Au5 | 1 | MAJOR | ACCEPTED: same fix as Cx5-1 | Inference |
| Au5 | 2 | MAJOR | ACCEPTED: a round-2 draft related to any round-1 sealed group is dropped, not joined; the SP drafts round 2 from sources no sealed group uses | Unit; 2a-(e) |

### 16A-2 plan-gate r6 dispositions (spec v6 `b8a24c5`, 12 Oct)
| Src | # | Sev | Disposition | Where |
|---|---|---|---|---|
| Cx6 | 1 | MAJOR | ACCEPTED: same fix as Au6-1 (two-sided; singleton equality with `mcnemar_exact`); option (B) no longer states a power bound for grouped families, and the figures come from the prereg's Monte Carlo table | Inference; option (B); 2a-(j) |
| Cx6 | 2 | MAJOR | ACCEPTED: one two-sided test per candidate, Holm over the K two-sided p-values; the family-wise α covers rejections in either direction | b1 |
| Au6 | 1 | MAJOR | ACCEPTED: the sign-flip primary is two-sided at α (the direction is read from the sign), equal to `mcnemar_exact` with singletons; the Monte Carlo power under independence with singletons reproduces `eval_stats.power(..., "directional")`, so the quoted power and MDE figures stand | Inference; a8; 2a-(j) |

**Plan-gate result (12 Oct 2026): READY.** Codex `gpt-6.1-sol` r7 READY on `1c36c4d` (Cx5-1, Cx6-1, Cx6-2 closed; no
new BLOCKER/MAJOR); plan-auditor r7 PASS (Au6-1 closed; none new). Rounds: r1 full (Codex 2/7/2, auditor 2/10/20); r2
full, fresh auditor (0/4+2 open; 1/13/12); r3 full, fresh auditor (0/2/1; 1/9/8); r4–r7 closure checks scoped to each
diff (2+3, 1+2, 2+1, 0+0 new MAJORs). 16A-2a may be built; 16A-2b's prereg waits on the PENDING-OWNER adoption rule.

### 16A-2 appendix A — note for the reviewing colleague
*(The owner sends this with each form. It names no client and quotes no Handbook text.)*

I am testing a research tool that answers conveyancing questions using only the Law Society's Conveyancing Handbook,
and that should decline when the Handbook does not answer. To test it I keep an answer key. On the enclosed questions
I am unsure of the key, and I would value your independent view. (If the form says *calibration*, the questions are a
random few, used to check the key as a whole.)

For each question:
1. Read it as if a colleague had asked you.
2. Tick one. **Answer:** the Handbook answers it. **Partly:** the Handbook answers some of it, and the rest is outside
   it. **Decline:** the Handbook does not deal with it, or it needs advice beyond the Handbook.
3. If *answer* or *partly*, write the Handbook paragraph numbers a good answer should rely on (for example 3.2.1), and
   say whether each one is needed or whether any one of them would do.
4. If *partly*, say in a few words which part the Handbook does not cover.
5. Tick *sure*, *fairly sure* or *unsure*, and add any comment. If you think I may see it differently, say so: that is
   exactly what I need.

Please keep the questions to yourself, do not put them into any AI tool or online service, and return the form only
to me. Delete or return your copy when we are done. Each question takes about 5–10 minutes. Thank you.

### Plan-gate dispositions (Codex r1–r4, plan-auditor r2–r4; 10 Oct)
Codex `gpt-6.1-sol` REVISE in r1 `425bdcc` (4 BLOCKER / 11 MAJOR / 1 MINOR), r2 `5513715` (2/7/3), r3 `16d0465`
(1/10/2), r4 `880600f` (0/2/1); plan-auditor FAIL on the same commits: r2 (2/18/9), r3 (1/17/13), r4 (0/7/16); no r1
auditor leg. Each finding was checked against plan and code; stats recomputed in stdlib Python 3 (v3's Wilson bounds
were off in the 5th decimal). Of 127 (a row may list several): **80 FIXED** (16A-2 remainders under Where),
**47 DEFERRED-16A-2** (P1–P8), **0 REBUTTED**.

| Source | # | Severity | Disposition | Where (v4.1) |
|---|---|---|---|---|
| Cx r1 | 1 | BLOCKER | DEFERRED-16A-2 | P4 sealed rendering; 16A-1 refuses sealed input, (c) |
| Cx r1 | 2 | BLOCKER | FIXED | Item 1 serialiser, destinations, scanner; (b), (i) |
| Cx r1 | 3 | BLOCKER | FIXED | Item 1 `classify` (2), (5), (7), sealed refusal; (c) |
| Cx r1 | 4, 9 | MAJOR | DEFERRED-16A-2 | P5 |
| Cx r1 | 5 | BLOCKER | DEFERRED-16A-2 | P8 (closed in r2; carried as a constraint) |
| Cx r1 | 6 | MAJOR | FIXED | Item 4 `split_families` (eligibility, units, twins); (h); pool → P2 |
| Cx r1 | 7 | MAJOR | FIXED | Item 4 stats; (j) |
| Cx r1 | 8 | MAJOR | FIXED | Item 4 power and MDE inputs; item 3 eligible denominators; (j) |
| Cx r1 | 10 | MAJOR | FIXED | Item 5 artifact; item 4 per-family flip frequency; (k2); draws → P6 |
| Cx r1 | 11 | MAJOR | FIXED | Item 6 exact coverage, `--controls`; (k) |
| Cx r1 | 12 | MAJOR | FIXED | Item 3 `observed_scope`, `score_partial`; (f) |
| Cx r1 | 13 | MAJOR | FIXED | Item 7; (l) |
| Cx r1 | 14 | MAJOR | FIXED | P0; item 3 v5 compatibility; (d) |
| Cx r1 | 15 | MAJOR | FIXED | Item 8 ceiling and D64 stop; (m); re-epoch → P5, estimate → P8 |
| Cx r1 | 16 | MINOR | FIXED | Evidence block |
| Cx r2 | 1 | BLOCKER | DEFERRED-16A-2 | P5 |
| Cx r2 | 2 | BLOCKER | FIXED | Item 1 registry, `classify`, floor; (c) |
| Cx r2 | 3 | MAJOR | DEFERRED-16A-2 | P4; 16A-1 has no sealed diagnostics, (g) |
| Cx r2 | 4 | MAJOR | FIXED | Item 1 scanner; (i) |
| Cx r2 | 5 | MAJOR | FIXED | Item 1 scanner (Q-needles only); S-needles → P4 |
| Cx r2 | 6, 9 | MAJOR | DEFERRED-16A-2 | P5 |
| Cx r2 | 7 | MAJOR | FIXED | (j) |
| Cx r2 | 8 | MAJOR | FIXED | Items 3, 5, 6 (v5 live guard kept, replay non-canonical, `--rewrite-candidate`); (d), (k), (k2); v6 → P7 |
| Cx r2 | 10 | MINOR | DEFERRED-16A-2 | P2 |
| Cx r2 | 11 | MINOR | FIXED | Items 3, 7; (e), (l) |
| Cx r2 | 12 | MINOR | FIXED | Item 3 `observed_scope` |
| Cx r3 | 1 | BLOCKER | FIXED | Item 8 locked reserve/settle, `max_retries=0` + metered retries; (m) |
| Cx r3 | 2 | MAJOR | FIXED | Item 8 `SpendLimitReached`; (m) |
| Cx r3 | 3 | MAJOR | FIXED | Item 1 privacy floor; (c) |
| Cx r3 | 4 | MAJOR | FIXED | Item 1 scanner (exact-hash exemption, decoding, promise); (i) |
| Cx r3 | 5 | MAJOR | DEFERRED-16A-2 | P1 (store-access stamp moved out of 16A-1) |
| Cx r3 | 6 | MAJOR | DEFERRED-16A-2 | P6 |
| Cx r3 | 7 | MAJOR | DEFERRED-16A-2 | P5 |
| Cx r3 | 8 | MAJOR | FIXED | OS-1 Q1 (`--refresh`; `harness-doctor` drift check) |
| Cx r3 | 9, 10 | MAJOR | DEFERRED-16A-2 | P3 |
| Cx r3 | 11 | MAJOR | DEFERRED-16A-2 | P8 |
| Cx r3 | 12 | MINOR | DEFERRED-16A-2 | P5 |
| Cx r3 | 13 | MINOR | DEFERRED-16A-2 | P5 (v3's table claim withdrawn) |
| Au r2 | 1 | BLOCKER | DEFERRED-16A-2 | P6 |
| Au r2 | 2 | BLOCKER | FIXED | Item 8; (m) |
| Au r2 | 3 | MAJOR | DEFERRED-16A-2 | P7 (v5's live guard is unchanged in 16A-1) |
| Au r2 | 4 | MAJOR | FIXED | Item 6 `--rewrite-candidate`; (k) |
| Au r2 | 5 | MAJOR | DEFERRED-16A-2 | P5 |
| Au r2 | 6 | MAJOR | FIXED | Item 1 floor, tests-only registration; (a), (c) |
| Au r2 | 7 | MAJOR | FIXED | Item 1 `classify` (4): public by registered path + sha256 |
| Au r2 | 8 | MAJOR | FIXED | P0; (d) |
| Au r2 | 9 | MAJOR | DEFERRED-16A-2 | P6 (the builder is item 5) |
| Au r2 | 10 | MAJOR | DEFERRED-16A-2 | P4 |
| Au r2 | 11, 12 | MAJOR | DEFERRED-16A-2 | P1 |
| Au r2 | 13 | MAJOR | DEFERRED-16A-2 | P2 |
| Au r2 | 14 | MAJOR | FIXED | Item 1 scanner (explicit sources, Q-needles only); (i) clean report |
| Au r2 | 15 | MAJOR | DEFERRED-16A-2 | P3; OS-1 Q5 |
| Au r2 | 16 | MAJOR | DEFERRED-16A-2 | P2, P3 |
| Au r2 | 17 | MAJOR | DEFERRED-16A-2 | P2; OS-1 Q2–Q4 |
| Au r2 | 18 | MAJOR | FIXED | OS-1 Q1; ordering rule (no private data before 16A-2's gate) |
| Au r2 | 19 | MAJOR | FIXED | Item 1 `--merge-gate` targets; (i); D-entry wording → P4 |
| Au r2 | 20 | MAJOR | DEFERRED-16A-2 | P3 |
| Au r2 | 21 | MINOR | DEFERRED-16A-2 | P4 |
| Au r2 | 22, 29 | MINOR | DEFERRED-16A-2 | P5 |
| Au r2 | 23 | MINOR | FIXED | Item 3 first commit; (n) |
| Au r2 | 24 | MINOR | DEFERRED-16A-2 | P2 |
| Au r2 | 25 | MINOR | FIXED | Item 7; (l) |
| Au r2 | 26 | MINOR | FIXED | Lanes; real-row checks and the heldout sha256 are [C] |
| Au r2 | 27 | MINOR | FIXED | Item 1 `classify` (6) |
| Au r2 | 28 | MINOR | DEFERRED-16A-2 | P8 |
| Au r3 | 1 | BLOCKER | FIXED | Item 4 `twins` contract (every batch pair covered); (h); producing edges → P2 |
| Au r3 | 2 | MAJOR | DEFERRED-16A-2 | P3 |
| Au r3 | 3 | MAJOR | DEFERRED-16A-2 | P3; OS-1 Q3, Q5 |
| Au r3 | 4, 5 | MAJOR | DEFERRED-16A-2 | P6 |
| Au r3 | 6 | MAJOR | FIXED | Item 1 marker scope, explicit needle sources, fixtures registered `fixture`; (c), (i) |
| Au r3 | 7 | MAJOR | FIXED | Item 1 hit report (private needles); sealed hits → P4 |
| Au r3 | 8 | MAJOR | DEFERRED-16A-2 | P5 |
| Au r3 | 9 | MAJOR | FIXED | Item 8 `SpendLimitReached`; (m) |
| Au r3 | 10 | MAJOR | FIXED | Item 8 wiring, eval only; (m) |
| Au r3 | 11 | MAJOR | FIXED | Item 1 range blobs, pre-push rule, incident stop; (i); residual in D65 |
| Au r3 | 12 | MAJOR | DEFERRED-16A-2 | P3 (item 2 leaves the rule unnarrowed) |
| Au r3 | 13 | MAJOR | DEFERRED-16A-2 | P1 |
| Au r3 | 14 | MAJOR | FIXED | Item 1 Q-needles (short question = whole needle only); (i); eligibility → P1 |
| Au r3 | 15 | MAJOR | FIXED | Item 2 inventory check, item 7 inventory; (g), (l); applied to new labels → P2 |
| Au r3 | 16 | MAJOR | FIXED | Item 3 `is_gap_statement`; (f) |
| Au r3 | 17 | MAJOR | DEFERRED-16A-2 | P7 (in 16A-1 no v6 run writes `eval/results.md`, (d)) |
| Au r3 | 18 | MAJOR | FIXED | Item 8 ledger redirect and guard; (m); slot state → P5 |
| Au r3 | 19 | MINOR | FIXED | P0 |
| Au r3 | 20 | MINOR | FIXED | Item 4 `preassigned`, `min_share`; (h); values → P2 |
| Au r3 | 21 | MINOR | DEFERRED-16A-2 | P2, P5 |
| Au r3 | 22 | MINOR | DEFERRED-16A-2 | P2 |
| Au r3 | 23 | MINOR | DEFERRED-16A-2 | P4 |
| Au r3 | 24 | MINOR | FIXED | Item 8 (`max_retries=0`, out-of-tree ledger); (m) |
| Au r3 | 25 | MINOR | FIXED | (j) both sidednesses, α = 0.0125; Tier-2 MDE and D57 threshold → P8 |
| Au r3 | 26 | MINOR | FIXED | No unmarking or trusted processors in 16A-1; complete refusal list; one lane per item |
| Au r3 | 27 | MINOR | FIXED | Item 6 (item 9 is [C]) |
| Au r3 | 28 | MINOR | FIXED | Item 7, D69 rationale |
| Au r3 | 29 | MINOR | FIXED | OS-1 Q6 (D64 hard stop) |
| Au r3 | 30 | MINOR | DEFERRED-16A-2 | P7 |
| Au r3 | 31 | MINOR | DEFERRED-16A-2 | P1 |
| Cx r4 | 1 | MAJOR | FIXED | Item 6: only expansion identity has the live-arm and candidate exceptions; (k) (also Au r4 17) |
| Cx r4 | 2 | MAJOR | FIXED | Item 4 `twins`: batch partition, `families_digest`, endpoints, conflicting preassignments; (h) |
| Cx r4 | 3 | MINOR | FIXED | Item 4 `mcnemartestClust` (checked against the htestClust index on rdrr.io) |
| Au r4 | 1, 2 | MAJOR | FIXED | 1: item 3 `CAVEAT_PREFIX` strip, (f); 2: P0 key drop, dotenv off, raising constructors, fake meter, (d) |
| Au r4 | 3, 4 | MAJOR | FIXED | 3: only the weekly cap is a D64 stop (exit 3 vs 6), Tier-2 €0.25 from the meter's worst case; 4: item 6 rows sidecar and `legacy` rule, item 2 one v1 id rule, (k) |
| Au r4 | 5, 6, 7 | MAJOR | FIXED | 5, 7: item 1 serialiser (stdout aggregates and ids; derived text stays private), digest a hash, (b); 6: `--merge-gate` source check (exit 7), `inputs.json`, (i) |
| Au r4 | 8, 9, 10 | MINOR | FIXED | 8: ordering wording; P2, P3, P5 own 003 16A.3–.5; 9: `worker_allow` listed, [C] computes sha256s; 10: `classify` (6) file and flag wording, D65 disclosure |
| Au r4 | 11–14 | MINOR | FIXED | 11: retry loop mirrors anthropic 0.116.0, (m); 12: `CC_SPEND_LEDGER` pytest-only, (m); 13: OS-1 Q1 on the branch; 14: formats in items 1–3, 6, 7 |
| Au r4 | 15–19 | MINOR | FIXED | 15: appendix stubs, (l); 16: Tier-2 copies registered, empty-map equality; 17: as Cx r4 1; 18: P0 branch runs, (d); 19: every stream, (b) scope, `results.md` test |
| Au r4 | 20–23 | MINOR | FIXED | 20: status recompute and H0 subprocess re-deferred (item 6, D68), P0 lock in-process; 21: salted private v1 ids; 22: malformed marked line sealed, (c); 23: Cx r1 10 row |
| Cx r5 | 1 | MINOR | FIXED | (m) retry set: 408/409/429 and every status ≥ 500 (500–503, 529 tested); 400–422 not retried |
| Au r5 | 1 | MAJOR | FIXED | merge gate: `inputs.json` `kind` (`questions`/`derived`); legacy caches and sha256-keyed artifacts exempt, their sources checked; (i) |
| Au r5 | 2 | MINOR | FIXED | as Cx r5 1 |
| Au r5 | 3 | MINOR | FIXED | P0 fakes retrieval; embedding/vector-store constructors raise; (d) |
| Au r5 | 4 | MINOR | FIXED | legacy arms need explicit `--legacy`; a post-16A arm missing its sidecar is refused; (k) |
| Au r5 | 5 | MINOR | FIXED | `*.rows.json` gitignored; (d) provenance identical with and without the sidecar |
| Au r5 | nc | — | FIXED | `worker_allow` import dependencies listed; registry-salt membership residual disclosed in D65, P4 decides |

## Cut list (v2)

**Cut order if behind (Phases 6–12 only; superseded for Phase 13 below):** judge pass →
MRR/hit@1,3 → CI smoke job → completeness metric → demo polish.
**Never cut:** fail-closed gate, appendix support, per-source replace (with BM25 rebuild), held-out
set, strict labeling + provenance, honest README, `v1.0-baseline` tag.
**Phase 13 cuts:** cross-encoder reranker (deliberately cut — no new dep needed via
sentence-transformers, but adds a second model download + latency days before the deadline);
per-sub-query pool widening (rescinded at plan gate per D31's measured regression — revisit
post-submission with eval evidence if the realistic slice shows recall still short); tier/
relevance machine-grading of graded answers (judge measures claim support only — known
limitation). Phase 13 cut order if behind: report-callout polish → D-entry prose → README demo
tweaks. Phase 13 never-cut: gate behaviour, exact REFUSAL_PHRASE, canonical guard (incl. the
judge pass in the WS8 canonical run), keyless CI.
**Phase 15 cuts:** re-expressing the chunker's char thresholds in true tokens (deliberately
deferred — with an 8k window nothing needs re-splitting, so paying the fixture +
sample-corpus-freeze blast radius now buys nothing; it belongs to the next deliberate
re-chunk); BM25 stemming, the service layer and entailment-level citation checking (each its
own measurable phase — folding them in would confound the embedding measurement). Phase 15
cut order if behind: Qwen3-0.6B arm (cost-disqualified with its measured wall-clock recorded
as a rejected alternative) → `scripts/embed_latency.py` (fall back to an inline timing snippet
recorded in the brief) → the p95 latency figure (p50 alone satisfies the ops disclosure).
**Chunk-token provenance disclosure is NOT cuttable** — it is named in the acceptance block,
and offering it as a cut made the phase simultaneously on-plan and unacceptable (plan-gate
finding A4). Phase 15
never-cut: the index-time truncation guard; `--reset` on every re-index; the arm-directory
gitignore landing before any arm is built; the held-out set staying out of arm selection; the
canonical v4 guards.

**Phase 15 disposition note (8 Oct 2026, D59 — appended; the lists above are unedited):**
- **"Never-cut: the index-time truncation guard" → MOOT, not cut.** At plan gate (brief 001,
  finding A2) the guard was resequenced to land *with* an adopted winning model. Under
  MiniLM's 256-token window it would correctly reject the 258-token sample chunk and break
  CI. No winner was adopted (D57), so its precondition never arose. No truncation-exception
  policy is adopted.
- **"Chunk-token provenance disclosure is NOT cuttable" → delivered in measured form:**
  - the 1,470-chunk token distribution in brief 001 §Problem (3 Aug);
  - the hit/miss truncation-discrimination measurement (D54).
  Automated per-run provenance is deferred to the next embedder change.
- Whether truncation is load-bearing is now a Phase 16 experiment
  (docs/designs/003-roadmap-and-next-actions.md, WS-C).

## Two-track git strategy

Tags freeze a fixed commit (`v1.0-pre-critique` on `17d23b1`, `v1.0-baseline` on the Phase 6 merge
commit, `v2.0` on main after Phase 12 if v2 is chosen); branches are movable pointers meant to keep
receiving commits, so freezing v1 for later comparison calls for a tag, not a branch — if a v1
hotfix is ever needed, a branch can still be cut from the tag afterwards. Day-to-day work continues
on the existing convention (branch `phase-N-slug` → PR → merge after user "go"), with phase
branches stacked from the previous phase's branch on Saturday so work isn't blocked waiting for
each PR's merge approval.
