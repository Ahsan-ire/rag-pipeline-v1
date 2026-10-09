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
