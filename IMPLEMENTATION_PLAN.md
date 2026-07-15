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

---

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

## Two-track git strategy

Tags freeze a fixed commit (`v1.0-pre-critique` on `17d23b1`, `v1.0-baseline` on the Phase 6 merge
commit, `v2.0` on main after Phase 12 if v2 is chosen); branches are movable pointers meant to keep
receiving commits, so freezing v1 for later comparison calls for a tag, not a branch — if a v1
hotfix is ever needed, a branch can still be cut from the tag afterwards. Day-to-day work continues
on the existing convention (branch `phase-N-slug` → PR → merge after user "go"), with phase
branches stacked from the previous phase's branch on Saturday so work isn't blocked waiting for
each PR's merge approval.
