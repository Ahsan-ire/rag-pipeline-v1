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

## Phase 16A — eval foundations — `phase-16a1-eval-instrument` → `v2.3.0`, then `phase-16a2-eval-data` → `v2.4.0` (spec v3, 9 Oct 2026: Codex round 2 and plan-auditor round 2 reconciled; combined dispositions table at the end)

**Authority:** `docs/designs/003-roadmap-and-next-actions.md` §5 16A (normative) and §1.2 (Q1 scope classes;
Q3 owner labels, colleague second opinion; Q7 tutorial material private); astra B3, M8–M10; D54; D57
addendum (re-open condition); D59 (C4, item 9); D61; D62 follow-ups; D64 (standing go, hard stops).

**v3 design rules** (v2's row index, needle sources and slot/hash coupling are gone): privacy by location +
marker (item 1); honest blindness (16A-2); signed rules, computed lists, a separate slot ledger (item 5);
canonical replay (items 3, 5); a metered API (item 8). **Ordering (003 16A.1):** no private question
enters any eval run, cache or judge call until 16A-1 is merged; no private data exists before OS-1 is applied.

**Sealed-output rule (whole phase):** anything printed, committed or returned to the orchestrator about a
sealed set is pre-registered aggregates or counts only: no row or family ids, sections, ranks, strata or
per-row outcomes. Per-row sealed data lives only under `eval/private/sealed/`; only the trusted processors
(item 1) and the subagents named in 16A-2 open it, never the orchestrator.

**Lanes:** **[C]** Claude only (corpus, `chroma_db/`, tutorial or private eval content, judgment). **[W]** R0
code on **synthetic fixtures only**, via `harness-worker` once B2's escape probes pass and OS-1's
`worker_allow` is applied (else Claude implements it); workers never receive real eval rows (public v1
included), `eval/private/`, held-out files or report output. Integration, real-row checks, canaries and
commits stay [C] (`Implemented-by:`).

**Owner touchpoints (D64): two pings.**
- **OS-1** (sent at the start of 16A-1; 16A-1 proceeds; **16A-2 step 1 waits for it**). Exact questions:
  1. *Manifest:* "Please review the `.harness/project.toml` diff (adds `^eval/private/` to `never_commit`,
     `eval/private` to `restricted_read` and `codex_clause`, the [W] globs to `worker_allow`) and run
     `~/ClaudeCode/harness/bin/harness-init` on this repo to apply it."
  2. *Tutorials:* "Which tutorials beyond T1 and T7 may 16A-2 subagents use as blind drafting sources?"
  3. *Colleague queries:* "Can colleagues' real queries be collected for the realistic slice? If so, about
     how many, by what date? Please remove client identifiers before dropping them in."
  4. *Source mix:* "Plan default: at least 25% of realistic families from tutorial or colleague sources if
     available; otherwise Claude-drafted lay phrasings, with the D46 self-retrieval caveat disclosed in
     D71. Approve, or set another minimum (and say whether the sealed split needs one too)."
  5. *Second reviewer:* "Will a colleague review the sealed refusal and ambiguous labels you forward at
     OS-2 within 7 days (or name a deadline)? Families without that review leave sealed for development."
- **OS-2** (16A-2 step 7; blocks sealing): the single validation session.
- **D64 triggers named here:** a spend-meter stop or an unapproved estimate above the remaining weekly cap;
  a re-epoch, replacement sealing, an amendment to the signed core or canonical v6 guard list, or raising
  K_max; a target or criterion not met as approved, other than the pre-approved negative branch.

### 16A-1 — instrument (fully agentic)

**Evidence (checked on `main` 9ce4e07; bare line numbers are `src/evaluator.py`):**
- Question text reaches both reports (`:1402–1413`, `:2549`, `:2572`; printed `:1550`, `:2167`); the matrix
  detail line adds expected/retrieved sections and ranks (`:2546–2549`); per-row state is keyed by text
  (`:1899–1965`, `:2533–2559`). The judge record carries the question (`src/judge.py:271`; configured
  `src/pipeline.py:642`, written `:2171–2173`). The live expansion cache is keyed by text (`:1711–1724`).
- `scripts/w_sweep.py` keys by text and prints 60/70-character prefixes (`:84`, `:150`, `:196`);
  `scripts/bakeoff_report.py` parses `:: question` (`:95`), keys flips by text (`:487–508`), matches roster
  and role coverage by prefix (`:544`, `:603`) and skips unmatched rows (`:786`).
- `load_golden_set` validates (`:200`) but projects three fields (`:205–210`); matching is OR-only
  (`:397`); `retrieved_sections` drops chunk ids (`:384–387`); both formatters print `datetime.now()`
  (`:1326`, `:2210`); the matrix report prints each set's sha256 (`:2328`); held-out branches key on the
  `held-out` label (`:2042–2048`, `:2341–2353`). Citations are validated (`src/generator.py:427`) before a
  terminal status overrides the outcome (`:433`); `src/render.py:350` can show an unverified draft.
- **No usage capture:** judge and rewrite chains end in `StrOutputParser` (`src/judge.py:103`,
  `src/query_rewrite.py:183`); the generator reads only `stop_reason` (`src/generator.py:391–394`).
- **Only `pipeline query` leaves a retrieval trace** (`src/pipeline.py:196–255` → `src/audit.py`
  `log_event`); other paths open the store via `get_vector_store` (`src/embedder.py:353`; callers
  `src/retriever.py:84`, `:270`; evaluator `:1246`, `:1750`) and `load_bm25_index` (`src/bm25_index.py:78`).
- `src/render.py:30` imports `split_sentences` from the evaluator; `_GAP_STARTS` is private (`:79`).
  `.harness/project.toml`: `restricted_read` lacks `eval/private`; `codex_clause = ""`. Dev sets: golden 35
  rows (23 direct / 7 exact_token / 5 refusal; 11 multi-section), realistic 23 (16 / 1 / 6; 11
  multi-section), no `id`; `tests/test_evaluator.py` has 59 `run_eval*` calls.

### Work
0. **P0 — pre-implementation captures [C]** (before any code edit; H0 precedent).
   `scripts/p16_capture_projection.py` (committed) runs both v5 formatters on `main`, models faked, over the
   public v1 sets (golden, realistic, sample) **plus a synthetic set labelled `held-out`**
   (`tests/fixtures/p16_synthetic_heldout.jsonl`, invented rows; no real held-out row is read): one offline
   run, and one canonical-shaped run whose fakes satisfy every v5 guard (results path redirected to tmp).
   Clock, paths and provenance are injected; output `tests/fixtures/p16_v5_projection_main.json` (`reports`
   compared, `main_sha` provenance). The orchestrator records H (j)'s offline retrieval-ablation rows on
   `./chroma_db` at `data/research/p16_offline_baseline_main.md`, with the SHA.
1. **Privacy classes and output control (D65).**
   - **[W] Registry** `eval/sets.json` + `src/eval_sets.py`: per set `path`, `privacy`, `role`
     (`tuning`/`realistic`/`sealed`/`regression`), `status` (`active`/`retired`), `sha256`; sealed sets add
     `epoch` and `commitment` (file sha256 + sha256 of the sorted family ids). Public: v1 golden, realistic,
     sample and heldout (heldout sha256 filled in at integration [C]). A marked file cannot register public.
   - **[W] `classify(path)`** on the resolved path (`os.path.realpath`), first match wins: (1) under
     `eval/private/sealed/` → sealed; (2) bytes match `"sealed"\s*:\s*true` → sealed, wherever the file is
     (a copy, rename, reorder, subset or reserialisation keeps its rows' markers); (3) under `eval/private/`
     → private; (4) a registered public path whose current sha256 matches → public; (5) a v5/v6 report or
     eval-tool cache whose recorded input-set sha256s (the report's `sha256:` lines; a cache's `inputs`
     header) are all registered public sets → public; (6) with `--legacy-public`, a sha256 listed in
     `eval/legacy_public.json` (pre-16A artifacts such as `eval/w_sweep_expansions_20260717.json` and the
     Phase 15 reports; written in 16A-1, frozen at its merge) → public; (7) else private (fail closed: a
     stripped header lands here). A run's class is the strictest input's; nothing is promoted by id or text.
   - **[C] Explicit privacy.** `run_eval`, `run_eval_matrix`, both formatters and the `w_sweep` and
     `bakeoff_report` entry functions take a keyword-only `privacy` with **no default**. CLIs pass
     `classify`'s result; tests pass `privacy="public"` explicitly (the 59 `run_eval*` calls and the
     bake-off instrument tests are edited mechanically; the H0 projection must still match). An AST test
     forbids that literal outside `tests/`.
   - **[W] Allowlisted serialiser** (`src/eval_privacy.py`), by run class: public as today; private →
     opaque ids + metrics, never a question, gap keyword or prefix (w_sweep's prefix prints become ids);
     sealed → pre-registered aggregates and counts only, for **every** set in the run, reals to 4 decimals.
   - **[C] Destinations.** A private run writes detail, judge dumps, caches and rank dumps only under
     `eval/private/`; a sealed run only under `eval/private/sealed/`, with `"sealed": true` on every row and
     file header; its only writes outside are the aggregate report, ledger line and slot record. Containment
     uses the resolved path (a symlink or `..` escape is refused). Non-public runs print warnings and
     exceptions as id (sealed: a count) + exception type, never `str(exc)` or a traceback with row values.
   - **[W] Leak scanner** `scripts/scan_leaks.py` (trusted): loads its own needles at scan time from every
     private and sealed file (`classify`); prints counts and needle classes, never a needle.
     - **Q-needles:** for each non-public, non-`legacy` question, the whole question and every 8-token
       window (NFKC, casefold, punctuation → space, whitespace collapsed). A window also found in a
       registered public question is exempt; the whole question never is. A question with every window
       exempt is flagged: sealed-eligible → ineligible (item 4 `pool`); private → counted for review.
     - **S-needles (sealed only):** evidence section ids as intact tokens (`4.8.1` is never split) and gap
       keywords as whole tokens, minus the aggregate template's fixed vocabulary; they scan the templated
       sealed-run outputs only (stdout, the report's sealed block, ledger line, slot record). Section ids and
       legal terms occur legitimately in tracked code (`3.2.1` at `src/chunker.py:202`), so repo-wide scans
       use Q-needles only.
     - **Promise:** any verbatim (normalised) run of ≥ 8 tokens of a non-public question, and any whole one,
       is detected; shorter fragments are prevented by the serialiser (tested in (b)), not promised here.
     - **Targets:** run outputs before printing or writing outside `eval/private/`; at the merge gate, every
       tracked file at HEAD, commit messages and tag annotations in the PR range, the PR body, comments and
       review comments (`gh pr view --json body,comments`; `gh api .../pulls/N/comments`). A hit aborts that
       output; a sealed slot is never re-run for it (per-row results are already stored, item 4): after a
       fix, `scripts/render_sealed.py --slot N` re-renders the aggregate with zero model calls.
   - **[W] Store-access stamp.** `get_vector_store` and `load_bm25_index` append `{ts, pid, argv0}` to
     `logs/store_access.jsonl` (`STORE_ACCESS_LOG` overrides; a write failure warns, never fails the call;
     `tests/conftest.py` redirects it). `scripts/check_store_access.py --since T0 --until T1` prints a
     count. With the audit log's line count, it is the evidence for "never retrieved" (16A-2 step 2).
   - **[C] Trusted sealed processors** (the only readers of sealed files; counts and hashes out): the
     evaluator under a reserved slot; `src/eval_sets.py`; `scripts/validate_eval_set.py` (sealed line/field
     detail goes to `eval/private/sealed/diagnostics/`); `scripts/eval_split.py`; `scripts/scan_leaks.py`;
     `scripts/render_sealed.py`; `scripts/apply_validation.py`. Every other tool refuses a sealed input.
   - **[C] Hygiene and rules.** `.gitignore` and `scripts/check_never_commit.py` gain `eval/private/`
     (extends D63); CLAUDE.md's do-not-read clause gains `eval/private/`; new hard rules: the sealed-output
     rule and "the orchestrator never opens `eval/private/sealed/`"; the OS-1 manifest diff is prepared.
     **Residual (D65):** the never-open rule, the marker and the subagents' no-retrieval rule are
     instruction-enforced misuse guards, not a boundary (a deliberately stripped marker or a direct
     `chromadb` import goes undetected); a Read-deny hook for `eval/private/sealed/` is left to the harness.
2. **Schema v2 (D66) [W].** `src/eval_schema.py` + `scripts/validate_eval_set.py` (line and field, never
   text; sealed: counts only). Rows carry `"schema": 2`; mixed v1/v2 files are rejected; sealed sets are v2,
   every row marked.
   - **Identity:** `id` unique per set; `family_id` names a scenario and its paraphrases. Public v2 ids match
     `^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$`; non-public `family_id` matches `^f[0-9a-f]{8}$` (issued by
     `eval_split.py`) and `id` is `<family_id>-<n>`; any other pattern is rejected.
   - **`scope`** `answer` | `partial` | `refuse` (Handbook mode, D61). **`evidence`:** AND groups, each a
     non-empty OR list of sections; `[]` iff `refuse`. **`gaps`:** `partial` only and non-empty there:
     `[{"id", "keywords": [≥ 1 non-empty string]}]`.
   - **Optional:** `type` (agrees with `scope`); `register` `modelled`/`lay`/`terse`; `source`
     `handbook`/`tutorial`/`colleague`/`legacy`; `ambiguous`; `owner_status`
     `draft`/`approved`/`changed`/`rejected`; `second_status` `not_required`/`pending`/`agree`/`disagree`/
     `missing`. A second review is **required** iff the row is sealed and `refuse` or `ambiguous` (Q3).
   - **v1 rows load exactly as today** (`load_golden_set` byte-equal); internally a v1 row gets
     `id = "q:" + sha256(question)[:12]` (public file) or `L<line>` (non-public), `family_id = id`, one OR
     group, scope from `type`.
3. **Scoring, report v6, canonical v6 (D66).** First commit: `split_sentences`, `_GAP_STARTS` and the hedge
   regex move to `src/text_utils.py` (render and evaluator import it; `src.evaluator.split_sentences` stays
   as a re-export), so the new scorers create no import cycle.
   - **[W] `score_evidence(ranked, groups, mode, absorbed=None)`**, `ranked` = ordered `(chunk_id, section)`
     pairs. `completion_rank` = max over groups of the first rank matching any member (strict = equal;
     related = today's nesting rule); `None` if a group is unmatched; also `groups_covered@k`. hit@k iff
     `completion_rank ≤ k`; one group equals today's `first_*_rank`. Absorbed aliases (item 7) share their
     chunk's rank and add no ranked entries.
   - **[W] `observed_scope(result)`** under the default display policy (`show_unverified=False`; the
     `render.py:350` override is outside this metric), first match wins: `unscored` (status not `complete`,
     a terminal outcome, or no gate outcome on a non-refusal) → `withheld` (`CITATIONS_UNVERIFIED`) →
     `refuse` (`is_refusal`, `src/generator.py:201`) → `partial` (a sentence passes H2's gap-statement
     test) → `answer`.
   - **[W] `score_partial(result, groups, gaps)`:** `unscored` rows count, never correct; otherwise correct
     iff the outcome is `CITATIONS_VERIFIED` or `PARTIALLY_VERIFIED`, not `is_refusal`, ≥ 1 verified
     citation related-matches a required group, and every gap is stated (a gap-statement sentence holds one
     of its keywords as a whole word, case-insensitively). Gap recall is reported separately.
   - **[C] Integration:** v2 sets use the new scorers; v1 sets use the untouched v5 path.
   - **Report v6** (iff a loaded set is schema 2) adds family counts with eligible denominators; a scope
     confusion table (labelled 3 × observed 5); family rates with Wilson CIs (`unavailable` when empty); the
     cohort block (item 6); expansion mode and digest (item 5); run cost (item 8).
   - **Canonical v6** = every v5 guard, with two replacements: the headline is the registry's active
     `role = sealed` set (not the `held-out` label); the rewrite guard is met **live** (attempts > 0,
     fallbacks 0) **or by replay** (complete, digest-verified coverage from an artifact whose build record
     met it, item 5). It adds: the signed core matches; the slot is declared and reserved by this process;
     the commitment verifies, `status == active`; per-row results stored before rendering; a clean leak
     scan; an active spend meter.
   - **v5 compatibility:** runs over registered public v1 sets stay report v5 byte for byte (fixed clock,
     paths, provenance); a v1 file classed private follows the serialiser, deliberately not byte-compatible.
4. **Pool, split, seal and slots (D67).** `scripts/eval_split.py` ([W] tool on synthetic data; [C] runs):
   - `migrate`: v1 golden + realistic, the 24 T1/T7 rows and a pairing map → schema-2 development rows in
     `eval/private/dev/` (`source` `legacy` for v1, `tutorial` for T1/T7; ids issued here). A
     golden↔realistic pair is one family in realistic; golden-only and T1/T7 families go to tuning.
   - `pool`: input `eval/private/sealed/pool/drafts.jsonl` (schema-2 rows with a content-free `draft_key`
     `d<batch>-<n>` and `draft_family`, no ids) + the dedup verdict file (step 3). It issues ids; sets
     `eligible_sealed` (false if dedup-flagged, fully window-exempt, or from a batch that failed its window
     check); writes `pool.jsonl` beside it, every row marked; prints totals by source, scope and
     eligibility. **Pool digest** = sha256 of the complete family records (every row and field) sorted by
     `family_id`, one `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` line each.
   - **Seed before split:** `eval/split_seed.json` (seed from `secrets`, pool digest, quotas) must be
     committed at HEAD; `split` refuses an uncommitted seed, a differing digest or an existing assignment
     file. `--new-epoch` needs recorded owner approval and fresh validation (D64).
   - **`split_families(families, seed, quotas, strata)`** (pure): sort by `family_id`, seeded shuffle; each
     family to exactly one split with all its rows; sealed draws only `eligible_sealed`, realistic only
     lay-register seeds; strata = scope × primary chapter (first section of the first group; `multi` if
     groups span chapters; `refuse`); refusal band 25–30% of each split's families; an infeasible quota
     fails with counts and writes nothing. Assignments go to `eval/private/sealed/split_assignments.json`;
     development-assigned rows move to `eval/private/dev/` unmarked (the only sanctioned unmarking).
   - `demote --reason <enum>` (called only by `apply_validation` and `seal`, never by hand with a sealed id)
     appends to `eval/private/sealed/demotions.jsonl` and moves the family to tuning, unmarked.
   - `seal` demotes families with any fully window-exempt phrasing, applies the band fallback (step 8),
     refuses below 50 families, writes the committed `eval/split_manifest.json` (seed, pool digest,
     per-split family and row **totals** with no sealed strata, split-file sha256s, assignment and
     demotion-log sha256s, sealed commitment) and registers the set. `verify` replays `split(seed, pool)`
     then the demotion log, checks membership and every hash, and prints pass/fail and counts.
   - **[C] Slot guards.** Before any sealed retrieval, expansion or model call: the signed core matches
     (item 5); the commitment verifies and `status == active`; `--sealed-slot N` is declared in
     `eval/slots.toml`; this process reserved it.
     - **Reservation:** exclusive create (`O_CREAT|O_EXCL`) in an out-of-tree state directory
       (`~/.local/state/rag-pipeline-v1/sealed_slots/<epoch>-<N>.json`, untouched by checkouts and
       `git clean`), mirrored to committed `eval/sealed_slots/<epoch>-<N>.json`; either existing refuses.
     - **States:** `reserved` → `stored` (per-row results fsynced under `eval/private/sealed/runs/`) →
       `publishing` (fsynced **before** any performance aggregate is printed, written or returned) →
       `completed`; or `failed`. Canonical status is decided before `publishing`; a non-canonical run ends
       `failed`, unpublished. A reserved slot never returns to unused.
     - `eval/sealed_ledger.md`: one line per finished slot (date, commit, slot, state, purpose, aggregates).
     - **Exposure** (a sealed file opened outside the trusted list, sealed retrieval outside a slot) retires
       the set (`status = retired`, role `regression`); replacement sealing is a D64 hard stop.
5. **Pre-registration, statistics, frozen expansions (D68).**
   - **[W] `src/eval_stats.py`** (stdlib only): family collapse (`all` | `majority`; a tie is a MISS); exact
     two-sided McNemar on family outcomes (primary); **Durkalski's clustered χ²** (sensitivity; Durkalski
     et al. 2003, as htestClust `mcnemarClust`): cluster k = a family with m_k phrasings (concordant
     included), d_k = (b_k − c_k)/m_k, χ² = (Σd_k)² / Σd_k², 1 df; singletons reduce it to the
     uncorrected asymptotic McNemar, not the exact test; Σd_k² = 0 is `unavailable`. Also Wilson CIs
     (`unavailable` when empty, never `[0, 0]`); exact power and MDE vs N, (p10, p01) or a discordance grid,
     the adjusted α and target power (not monotone in N: MDE = the smallest effect reaching target power at
     that N); Holm over K_max; `critical_verdict` kept apart from `exploratory_verdict`; variance summaries.
   - **Eligible populations:** retrieval metrics count `answer` + `partial` families, scope and refusal
     metrics all families; each rate, CI and MDE uses the printed eligible denominator.
   - **Verdict per confirmatory slot** (candidate vs incumbent on the primary metric): **PASS** iff it
     favours the candidate, Holm-adjusted p ≤ α, the point estimate ≥ MWE, and `critical_verdict` is not
     FAIL; **FAIL** iff `critical_verdict` FAILs or adjusted p ≤ α favours the incumbent; else
     **INCONCLUSIVE**. Interim adjusted p = K_max·p; the final Holm table can upgrade an INCONCLUSIVE, never
     reverse a PASS or FAIL. Unused and failed slots enter with p = 1; K never shrinks. Every slot run also
     carries the development sets, so `critical_verdict` is an aggregate of the same run.
   - **Candidate rule (one non-adaptive batch per epoch).** Candidates are chosen on development evidence
     only. All confirmatory candidates of an epoch are declared in **one batch** before its first
     confirmatory slot is reserved; `scripts/eval_slots.py declare` refuses once any is reserved. A
     candidate conceived after a confirmatory result is exploratory or waits for a new epoch (fresh sealed
     families; D64 hard stop). The only sealed information before the batch is slot 0's descriptive
     aggregates (no rows, families or sections); D68 discloses this residual adaptivity.
   - **Signed core + slot ledger.** `eval/preregistration.toml` (the core) is immutable once signed:
     the owner approves it at OS-2; the orchestrator commits it with `eval/preregistration.sig.json` (core
     sha256, signing commit, date) and records both in the D68 addendum; any later change is an amendment
     (D64 hard stop). `eval/slots.toml` is append-only; each `[[slot]]` records epoch, slot, kind
     (`baseline`/`spare`/`confirmatory`/`exploratory`), `core_sha256`, candidate config hash, commit, the
     development report hashes that selected it, and `expansion` = `fixed:<artifact digest>` or
     `candidate:<rewrite config hash>`. A sealed run checks sha256(core) == sig == its slot's `core_sha256`;
     appending a slot never touches the core.
   - **Core contents:** unit (family), outcome and tie rules; primary metric (family strict@6,
     hybrid+rewrite); eligible populations; MWE (draft 0.10); α = 0.05 family-wise, Holm over K_max
     (draft 4), target power 0.8, MDE over discordance 0.2/0.3/0.5; the schedule (slot 0 incumbent
     baseline, descriptive; spare 0b only if slot 0 ended `failed` before `publishing` and within the
     approved spend; one confirmatory batch ≤ K_max per epoch); the critical-control rule; R (draft 5);
     the verdict rules; the canonical v6 guard list.
   - **Critical-control rule** (signed as a rule; the list is computed): the retrieval-eligible development
     families, on final labels, that are a family strict HIT@6 in **both** raw hybrid (offline) and
     hybrid+rewrite replaying the development artifact, in the step 10 baseline. `scripts/critical_controls.py`
     computes the list; it is committed with its sha256 (in D72) before any 16B slot.
   - **[W] Frozen expansion artifact** (`src/expansion_artifact.py`): entries keyed by (row id, question
     sha256), as an expansion depends only on the question (label changes never invalidate it); bound to the
     rewrite model id, prompt sha256 and rewrite config hash; an `inputs` header and a **build record**
     (entries, live attempts, fallbacks, building run). `run_eval_matrix` accepts an injected artifact and
     replays it in every arm; a missing entry or identity mismatch fails (never regenerates); the report
     records the digest and separate `rewrite_live` / `rewrite_replayed` counts; v5 runs keep today's live
     expansion. The *development artifact* is built live (zero fallbacks) on the final tuning and realistic
     v2 in step 10 (`eval/private/artifacts/`); the *sealed artifact* is slot 0's live expansions, stored at
     `stored` under `eval/private/sealed/artifacts/` (0b rebuilds it live). *Retrieval-only candidates*
     declare `fixed:<sealed artifact digest>`; both arms replay it. *Rewrite-changing candidates* declare
     `candidate:<config hash>` plus a development artifact built with that config before the batch; in the
     slot the candidate arm expands live with exactly that config and the incumbent arm replays.
   - **Deployment variance:** R draws on the development sets, each re-expanding live and re-running
     retrieval and family scoring: per-family flip frequency against the development-artifact replay, and
     the family-rate range.
6. **C4 cohort identity + item-9 backlog (D69).**
   - **[C] Cohort block** per set in report v6: path, privacy, schema, sha256 (sealed: commitment), rows,
     families, `cohort_fp` = sha256 over the sorted `(id, evidence fingerprint, scope)` tuples.
   - **[C] `compare` / `compare_prod_ranks`** key rows by `(set sha256, id)`, never position or prefix.
     Both arms must cover the eligible ids exactly once with every required field (a missing, extra or
     duplicate row fails; the `bakeoff_report.py:786` skip goes). They refuse differing set hashes, labels,
     `cohort_fp`, scorer version or absorbed-map hash; expansion identity is checked as the slot declares
     (development comparisons: identical digests unless `candidate:`). Every critical control must resolve
     and be a strict HIT in the baseline arm, else the comparison is refused as non-reproducing (an
     instrument error, not a verdict). The D54 roster and role coverage move to ids; v5 output is unchanged.
   - **[W] Item 9:** `w_sweep.py` loses its module-level `chdir` and cwd-relative paths; duplicated helpers
     merge; the arm roster is single-sourced. **D62 follow-up:** one answer_fn status wrapper.
7. **Absorbed-section metadata (D70).**
   - **[W] Chunker side channel:** each D20 runt merge as (absorbing section, absorbed section, absorbed span
     in `clean_text`); each final chunk's own span carried through the oversize re-split; chunk text, ids
     and metadata unchanged.
   - **[C] `scripts/absorbed_map.py`** runs it on the corpus (no re-index) and commits
     `eval/absorbed_sections.json` (versioned, hashed; chunk ids and section numbers only), keyed by
     production chunk id (source-scoped content hashes, `src/embedder.py:378–406`; oversize merges re-split
     into chunks sharing one `section_number`, `src/chunker.py:491–504`). A chunk lists an absorbed section
     only if the whole absorbed span (whitespace-trimmed, as emitted chunk bodies are) lies inside its own
     span; siblings and find-miss sub-chunks (`chunker.py:494–504`) get none. **Every key must exist in
     `./chroma_db`**, or the build fails (e.g. a differing `source` string). v6 strict scoring credits an
     absorbed section only through that chunk's entry; the map hash binds comparisons. D54's repairs,
     production metadata and the gate are untouched.
8. **API spend meter (D69).**
   - **[C] `config/api_prices.toml`** (committed): per model id, $/MTok for input, output, cache write and
     cache read; `price_list_date`; `usd_per_eur`. An unknown model id refuses the call.
   - **[W] `src/spend.py`:** `SpendMeter(prices, limit_eur, ledger)` as a LangChain callback handler
     (`raise_error=True`). **Before** each call (`on_chat_model_start`): worst case = the prompt's UTF-8
     bytes as input tokens + `max_tokens` output; if spent + worst case > limit, it raises
     `SpendLimitReached` and nothing is sent. **After** each call: the cost from `usage_metadata` (missing
     usage counts as the worst case) goes to `logs/spend_ledger.jsonl` (time, run id, model, tokens, cost;
     no text). Each evaluator-level retry is a separate metered call.
   - **[C] Wiring:** attached in `get_llm()` (generator, judge) and `get_rewrite_llm()` on every live run.
     Limit = the remaining D64 weekly cap (€40 minus this ISO week's ledger total) or a lower
     `--approved-eur`; a higher figure needs `--owner-approved-eur`, as approved by the owner (OS-2 or a D64
     ping; recorded in D72). A stop aborts the run (a sealed slot ends `failed`, unpublished) and is the D64
     spend hard stop. Report v6 prints the run cost; v5 report text is unchanged.
9. **Ledger + docs [C].** D65–D70; CLAUDE.md (canonical v5 and v6, the privacy and sealed rules,
   `eval/private/`, the meter); `docs/harness.md` changelog and the negative-result phase path (16A-2 Stop
   paths); the `Current phase` and `Next:` lines.

### Acceptance (Tier-1)
- **(a) Suite and CI** green, including `never-commit`; `tests/test_h_projection.py` (H0 lock) passes.
- **(b) Canary leak test** (`tests/test_eval_privacy.py`, models faked): synthetic private and sealed sets
  carry a distinct `P16-CANARY-<field>-<uuid>` in the question start and end, a gap keyword, an evidence
  group, a malformed row's id-like value, a per-row and a top-level exception message; run through
  `run_eval_matrix`, `run_eval`, the judge dump, `w_sweep` (prefix prints too), `bakeoff_report`,
  `eval_split`, `validate_eval_set`, a loader error and a CLI exception. Each sentinel is absent (also as an
  8-token window, JSON- or `\u`-escaped, re-cased or re-spaced) from stdout, stderr, `caplog` and every file
  outside `tmp/eval/private/`, present in the private detail report; forced injections (whole question,
  8-token prefix, reformatted copy) are caught and write nothing; a symlink escape aborts.
- **(b2) Sealed aggregates only.** A sealed fixture run's stdout, report, ledger line, slot record and
  returned result contain no fixture row or family id, section, stratum or per-row outcome (regex over the
  fixture's ids and sections); public sets in the same run render as aggregates.
- **(c) Classification:** one test per `classify` rule; a sealed fixture copied, renamed, reordered, subset
  or reserialised outside `eval/private/` stays sealed; an unregistered file, a registered path with changed
  bytes, a report with a stripped or non-public input header, and a mixed public + unmatched run are
  private; a marked file cannot register public; the CLI resolves an unregistered file to private; the AST
  test passes; every non-trusted tool refuses a sealed input with zero retrieval and model calls.
- **(d) v5 unchanged:** `tests/test_p16_projection.py` reproduces P0's `reports` byte for byte (synthetic
  held-out and canonical-branch runs included); `load_golden_set` is byte-equal on the v1 files [C]; H (j)'s
  offline rows reproduce P0 (orchestrator-verified); each v5 canonical guard, removed alone, makes the run
  non-canonical; the CI greps are unchanged.
- **(e) Evidence groups** [W, synthetic]: two groups both hit → the later group's first hit; one missing →
  a miss at every k; OR alternates; a parent matches under related, not strict; an absorbed alias takes its
  chunk's rank; `groups_covered@k`. [C]: on every v1 golden and realistic row, single-group ranks equal the
  v5 ranks (fake retriever).
- **(f) PARTIAL and observed scope:** a stated gap in a VERIFIED or PARTIALLY_VERIFIED answer is correct;
  incorrect: a missing gap, a keyword outside a gap statement, a whole refusal (quoted and trailing-period
  variants), no verified citation in a required group, a withheld draft; truncated, declined, incomplete,
  unknown and error drafts with verified citations and a stated gap are `unscored`; D32's hedge is not a
  stated gap; one `observed_scope` fixture per class.
- **(g) Schema rejections** name line and field, never text (sealed: counts, detail under
  `eval/private/sealed/diagnostics/`): duplicate id; bad or non-opaque id; sealed below schema 2 or
  unmarked; mixed versions; scope/evidence/gaps incoherence; empty keyword list or keyword; `type`/`scope`
  disagreement; empty group; unknown `owner_status`/`second_status`.
- **(h) Pool and split:** over 200 seeded synthetic pools no family is in two splits and phrasings follow
  their family; same seed → same split; permuted input → identical split; ineligible families are never
  sealed; quotas, strata and band hold; an infeasible quota fails writing nothing; an uncommitted seed, a
  digest mismatch or existing assignments are refused; a label or eligibility change preserving every count
  changes the digest; `verify` replays demotions and fails on a one-byte change to any split file; the
  committed manifest carries no sealed id or stratum.
- **(i) Sealed guards** (fake call counters): an unsigned core, core ≠ sig, an undeclared, used or
  unscheduled slot, a commitment mismatch or `retired` status each refuse with zero retrieval, expansion
  and model calls; of two processes reserving one slot exactly one proceeds; deleting the committed slot
  file does not free the slot; an exception after reservation → `failed`; a crash before `publishing`
  allows 0b, after it refuses 0b; a non-canonical run ends `failed` unpublished; a scanner abort after
  `stored` re-renders via `render_sealed.py` with zero model calls; `declare` is refused after a
  confirmatory reservation in the epoch; appending a slot leaves the core check passing.
- **(j) Stats** (reference values recomputed 9 Oct, stdlib Python): exact McNemar b = 10, c = 2:
  p = 0.03857421875; singleton Durkalski on the same data: χ² = 5.3333, p = 0.0209213353 (≠ exact); unequal
  clusters (m_k, b_k, c_k) = (2,2,0), (2,1,1), (3,3,0), (2,0,1), (2,1,0): d = 1, 0, 1, −0.5, 0.5,
  χ² = 2²/2.5 = 1.6, p = 0.2059032107; all-cancelling clusters → `unavailable`; exact power at gain
  probability 0.8, α = 0.05: 0.2097280 (7 discordant) > 0.1677747 (8); N = 50, effect 0.10: 0.2411886 at
  discordance 0.2, 0.0944530 at 0.8; Wilson 0/10 → [0, 0.27753], 10/10 → [0.72247, 1], 0/0 →
  `unavailable`; three paraphrases count once; a tie is a MISS; Holm on a known vector; unused and failed
  slots enter as p = 1, K unchanged; verdicts (below-MWE with p ≤ α is INCONCLUSIVE; a control flip FAILs
  at p > 0.5; exploratory flips alone never FAIL; the final table never reverses).
- **(k) C4:** a mismatched set hash, label, `cohort_fp`, scorer version or absorbed-map hash is refused;
  `fixed:` arms with different digests are refused, a `candidate:` arm with the declared config accepted;
  a missing, extra or duplicate row fails (today's missing-baseline-HIT case included); an unresolved or
  non-HIT control is refused as non-reproducing; permuted production-rank rows match by id.
- **(k2) Frozen expansions:** two arms replaying one artifact see identical expansions, `rewrite_live` = 0;
  a full replay of an artifact built with zero fallbacks is canonical-eligible, one built with a fallback is
  not; a missing entry fails with zero expansion calls; an identity mismatch refuses; each variance draw
  re-expands and re-retrieves every development row.
- **(l) Absorbed:** a synthetic runt merge yields the expected map; in an oversize merge only the containing
  sub-chunk is credited (not its sibling or a find-miss sub-chunk); a key absent from a fake index fails the
  build; the 16-chunk sample corpus is byte-identical; the 1,470-chunk canary holds (orchestrator-verified);
  v5 scoring ignores the map.
- **(m) Spend meter** (fake chat model with usage): usage is captured from generator, judge and rewrite; a
  call whose worst case would cross the limit is never sent; missing usage counts as worst case; an unknown
  model is refused; the weekly remainder comes from the ledger; ledger lines carry no text; the offline
  command makes zero metered calls.
- **(n) Hygiene:** `git check-ignore eval/private/probe.jsonl` passes; `check_never_commit.py` rejects
  `eval/private/x.jsonl`; `git ls-files eval/private` is empty; `w_sweep` imports without changing cwd and
  runs from a tmp cwd; both store openers stamp (redirected in tests, failure-tolerant); `src.render` no
  longer imports `src.evaluator`.
- **(o) Ledger:** D65–D70 present; `Current phase` and `Next:` updated.

**Tier-2:** offline v6 on v2 copies of golden and realistic (one family per row) equals the v5 row-level
numbers (PR records metadata only); a worker-lane record per [W] item.

### 16A-2 — data + re-baseline
**Preconditions:** 16A-1 merged; OS-1 applied (checked by (a)). **Layout:** `eval/private/{dev,validation,
reports,artifacts}/` and `eval/private/sealed/` (pool, set, assignments, demotions, validation, diagnostics,
runs, artifacts). **Committed:** counts, hashes, seed and split manifests, registry, slot records, ledger,
core + sig, `slots.toml`, control list; development ids may appear in reports, sealed ids never.

**Blindness, stated honestly (D71).** Every new family, sealed included, is written by Claude-family
subagents reading the handbook and approved tutorials, so seeds may sit closer to the corpus wording than
real queries do. "Blind" means never retrieved outside a slot, never used for tuning, never read by the
orchestrator. Every sealed-touching dispatch prompt forbids running `src.pipeline`, importing `src` or any
eval command, and allows a reply of counts only (dedup: plus ineligible keys).

### Work
1. **Migrate development data [C]** (`eval_split.py migrate`). Proposed AND splits are flagged for OS-2. The
   24 T1/T7 rows are development (already retrieved). The v1 files stay byte-frozen. Held-out v1 becomes
   role `regression` once sealed v2 lands; still run and reported, never the headline.
2. **Draft new families blind [C dispatch].**
   - Fresh `sonnet` subagents, one batch per source, write schema-2 seed rows (`draft_key`, `draft_family`,
     seed, `evidence`, `scope`, `gaps`, `source`, `register`, `ambiguous`, `"sealed": true`) straight into
     `eval/private/sealed/pool/drafts.jsonl`, returning counts only. Sources per OS-1: the handbook, approved
     tutorials, colleague queries the owner drops into `eval/private/sealed/pool/intake/` (orchestrator-unread).
   - **Window check:** the orchestrator records T0/T1 around each batch and runs nothing that opens the
     store in between; afterwards `check_store_access.py` and the audit log's line count (count only) must
     show zero new entries in the window, else the batch is development-only.
   - **Targets** (families, ~20% surplus): ≥ 60 sealed-eligible; enough lay families for realistic ≥ 40 and
     others for tuning ≥ 60 after migration; refusals 25–30%; partials present; realistic mix per OS-1 Q4.
3. **Label check and dedup [C dispatch].** A fresh `opus` subagent checks every draft's labels against the
   handbook, writes notes beside the drafts, returns counts. Another compares each draft family's scenario
   with **all** development families, held-out v1 and any regression set (same scenario → ineligible,
   `dev_sibling`; exact duplicate → dropped), writes the verdict file, and returns counts plus the
   ineligible `draft_family` keys (they can only become development). `pool` then issues ids, applies
   eligibility (fully window-exempt questions included) and prints the digest.
4. **Commit seed, split, paraphrase [C].** Commit `eval/split_seed.json`, run `split`; development-assigned
   new families move to `eval/private/dev/`, readable by the orchestrator from then on. Development
   paraphrases: the orchestrator or subagents. Sealed paraphrases: one fresh subagent writes 2–3 phrasings
   (`modelled`/`lay`/`terse`) per sealed family into `eval/private/sealed/` under the same window check.
5. **Development baseline [C]** (no sealed access): the zero-API offline ablation on tuning v2 and realistic
   v2 (partial v6 report; the 16A-1 (d) v1 canary holds), and a metered 3-row live dry run (rewrite,
   generation, judge) on development rows to measure tokens per call.
6. **Cost estimate [C]:** planned calls × measured tokens × `config/api_prices.toml`, covering slot 0 (all
   modes, generation, judge, live sealed expansion), retries at the retry limit, spare 0b, the development
   artifact, the R variance draws and spend already incurred; with the price-list date and €/$ rate.
7. **OS-2 — the batched owner session (one ping).** `apply_validation.py build` writes the packet (dev items
   in `eval/private/validation/`, sealed in `eval/private/sealed/validation/`, every phrasing included) and
   a one-page count summary. The owner (a) validates every sealed label, every refusal, partial and
   ambiguous label, each phrasing's fit to its family's label, and the proposed AND splits; (b) rules on the
   four flagged items: T01-Bb, the realistic "planning permission for an extension" refusal label (Q1), the
   missing secondary labels on T07-Ab and T07-Ba, and the conflicting handbook dates for the pre-1975
   architect's-certificate rule; (c) signs the core (item 5) and the canonical v6 guard list; (d) approves
   the estimate if it exceeds the remaining weekly cap (estimate + 20%, never above the remaining cap
   unless the owner approves a higher figure); (e) forwards the sealed refusal + ambiguous sub-packet to the
   colleague (Q3) with a reply deadline (default 7 days), and drops the reply into
   `eval/private/sealed/validation/`.
8. **Apply validation [C].** `apply_validation.py apply` (counts only) sets `owner_status` and
   `second_status`, then demotes to development owner-rejected families and families whose required second
   review is `disagree`, or `missing` at the deadline (pre-registered: no resolver, no further ping).
   **Band fallback:** if the sealed refusal share leaves 25–30%, `seal` demotes families of the
   over-represented class in seeded order until it holds. D71 records raw agreement, Cohen's κ and PABAK over
   answer/partial/refuse (κ is unstable at extreme prevalence), and the owner's change rate.
9. **Seal [C].** `seal` (below 50 families → Stop paths); commit the manifest, the registry entry (`sealed`,
   `active`) and the ledger header; `verify` passes.
10. **Slot 0 and development measures [C].** Before reserving, the meter limit must cover the estimate (else
    a D64 spend ping). One canonical v6 run on slot 0 (sealed + realistic v2; tuning v2 and held-out v1 as
    regression), expanding live and storing the sealed artifact; it writes `eval/results.md` (sealed:
    aggregates). Slot 0b only under item 5's rule. On the final development sets: build the development
    artifact, run the offline and replay baselines, compute and commit the critical-control list with its
    sha256, and run the R variance draws.
11. **Ledger [C].** **D71:** data, sources and mix, counts, validation and agreement, the blindness and
    authorship caveat, the flagged items' dispositions by item id and outcome class only (no row content).
    **D72:** re-baseline, cost, variance, achieved MDE, the control-list hash; the D57 re-open condition
    becomes testable. Update the `Current phase` and `Next:` lines.

### Acceptance (Tier-1)
- **(a) Hygiene:** suite and CI green; `git ls-files eval/private` empty; `verify` passes;
  `.harness/project.toml` has `eval/private` in `restricted_read` and `codex_clause`, `^eval/private/` in
  `never_commit`.
- **(b) Counts:** ≥ 60 tuning, ≥ 40 realistic, ≥ 50 sealed families (retrieval-eligible sealed count
  beside it); 2–3 phrasings per family; refusals 25–30% of each split's families; zero families across
  splits; zero sealed families that are migrated, dedup-ineligible, fully window-exempt or from a failed
  window.
- **(c) Labels:** every sealed row has `owner_status` `approved` or `changed`; every sealed refusal or
  ambiguous row has `second_status = agree`; no sealed row is `draft`, `pending`, `disagree` or `missing`;
  every development refusal, partial and ambiguous row is owner-validated; D71 records agreement.
- **(d) Flagged items:** all four have recorded dispositions.
- **(e) Core signed:** the sig's sha256 equals the core's and appears in the D68 addendum and the report.
- **(f) Canonical run:** every v6 guard passes; slot records and ledger show slot 0 `completed` (or 0
  `failed` unpublished, then 0b `completed`) and nothing else.
- **(g) Leak scan:** `scan_leaks.py` reports 0 over the merge-gate targets, run by the orchestrator once the
  PR body and Codex dispositions are final and again after any later PR edit; a clean real-shaped report
  passes.
- **(h) Spend:** the 16A-2 ledger total ≤ the approved amount; no call crossed the weekly cap without the
  owner's approval of that figure.
- **(i) Ledger:** D71 and D72 present; the non-empty critical-control list committed with its hash.

### Tier-2
- **Baseline:** family strict@6 (hybrid+rewrite, raw hybrid) on sealed, realistic and tuning v2 with Wilson
  CIs and eligible denominators; scope confusion, gap recall; held-out v1 as regression; D54 class counts
  (development). **Variance and power:** variance over R draws; achieved MDE at the retrieval-eligible
  sealed count. Expectation (9 Oct, exact test, 80% power, α = 0.05, K = 1): N = 36 → MDE ≈ 0.26 at
  discordance 0.3, 0.34 at 0.5, none at 0.2; N = 50 → 0.22, 0.29 (≥ 50 sealed families ≈ 35–37
  eligible), stated in D68 as the honest power constraint. **PR:** aggregates and hashes, never sealed ids.

### Stop paths
- **Negative branch (pre-approved; D57 and `docs/work-state.md` precedent).** If sealed v2 stays under 50
  families, or another sealing target cannot be met as approved: do not seal; (e), (f) and the sealed parts
  of (b), (c) are MOOT; commit tooling and non-private metadata only; D71 is the negative result (what fell
  short, why); phase-gate with MOOT-marked criteria, Codex merge gate, PR headline = the result; merge and
  tag `v2.4.0` under D64; then STOP and ping before 16B, whose confirmatory plan needs a sealed set.
- **Spend:** a meter stop, or an estimate above the remaining cap not approved at OS-2, is a D64 spend ping;
  meanwhile only offline work runs, and a deferred slot 0 leaves (f) unmet (no merge, no tag).
- **Exposure and protocol:** sealed exposure retires the set (item 4); re-sealing, a change to the signed
  core, the canonical v6 guard list or K_max, or an empty control list (16B's rule would be vacuous) is a D64
  hard stop.

### Gates
1. **Plan gate:** plan-auditor + Codex `gpt-6.1-sol`, read-only, re-gate stopping rule.
2. **16A-1:** implement → `/phase-gate 16A-1` → Codex merge review → PR, CI → merge, tag `v2.3.0` (D64).
3. **16A-2:** steps 1–6 → OS-2 → steps 8–11 → `/phase-gate 16A-2` → Codex merge review on code and
   committed metadata only (`eval/private/` in `restricted_read` and the do-not-read clause) → PR, CI →
   merge, tag `v2.4.0` (D64), only if every Tier-1 criterion passed or the negative branch applies.

### Ledger (from D65)
D65 privacy and output control · D66 schema v2, scoring, report/canonical v6 · D67 pool, split, sealing,
slots · D68 signed core, slot ledger, statistics, rules, frozen expansions (OS-2 addendum: core sha256 and
signing commit) · D69 C4, item 9, spend meter · D70 absorbed map · D71 data v2 · D72 re-baseline.

### Plan-gate dispositions (Codex rounds 1–2, plan-auditor round 2; 9 Oct)
Codex `gpt-6.1-sol` r1 on `425bdcc`: REVISE (4 BLOCKER / 11 MAJOR / 1 MINOR); r2 on `5513715`: REVISE (2 /
7 / 3, and 5 r1 findings not closed); plan-auditor on `5513715`: FAIL (2 / 18 / 9). Each finding was checked
against the plan and cited code; Durkalski was recomputed in stdlib Python. "Item" = 16A-1 work item,
"step" = 16A-2 step, a letter = a Tier-1 criterion of the half named.

| Source | # | Sev. | Disposition | Where (v3) |
|---|---|---|---|---|
| Codex r1 | 1, 8, 12, 16 | BLOCKER, MAJOR ×2, MINOR | Closed in r2; kept | Sealed-output rule, item 1 serialiser, 16A-1 (b2); item 5 populations; item 3, (f); evidence block |
| Codex r1 | 2, 3, 7, 9, 10 | BLOCKER ×2, MAJOR ×3 | Not closed in r2 → ACCEPTED as r2 #4, #2, #7, #6 (modified), #8 | See those rows |
| Codex r1 | 4, 5, 6, 11, 13, 14, 15 | BLOCKER, MAJOR ×6 | Closed in r2; each extended in v3 by, respectively, auditor #22 (out-of-tree reservation), #28 (merged negative branch, private data still local), Codex r2 #10 (digest), auditor #1 (control rule), r2 #11 (interface), auditor #8 (held-out branch), auditor #2 (meter) | See those rows |
| Codex r2 | 1 | BLOCKER | ACCEPTED: immutable signed core + sig; separate append-only `eval/slots.toml` citing the core hash | Item 5; 16A-1 (i) |
| Codex r2 | 2 | BLOCKER | ACCEPTED by redesign: no row index; public only by registered path + sha256 or all-public input hashes; marker → sealed anywhere; stripped header → private | Item 1 `classify`; 16A-1 (c) |
| Codex r2 | 3 | MAJOR | ACCEPTED: no sealed index exists; sealed diagnostics only under `eval/private/sealed/diagnostics/`, counts out | Items 1, 2; 16A-1 (g) |
| Codex r2 | 4 | MAJOR | ACCEPTED: whole-question needles never exempt; fully exempt questions flagged or made ineligible; promise stated (≥ 8 tokens or whole question; shorter fragments prevented by the serialiser) | Item 1 scanner; step 3; 16A-1 (b) |
| Codex r2 | 5 | MAJOR | ACCEPTED, modified: S-needles scan templated sealed outputs only; repo-wide scans use Q-needles (`3.2.1` at `chunker.py:202`) | Item 1 scanner; 16A-2 (g) |
| Codex r2 | 6 | MAJOR | ACCEPTED, modified: one confirmatory batch per epoch, declared before any confirmatory slot; later ideas exploratory or a new epoch; slot-0 aggregates disclosed as residual | Item 5 candidate rule; 16A-1 (i) |
| Codex r2 | 7 | MAJOR | ACCEPTED: d_k = (b_k − c_k)/m_k; χ² = 1.6, p = 0.2059032107 (recomputed) | Item 5; 16A-1 (j) |
| Codex r2 | 8 | MAJOR | ACCEPTED: v6 rewrite guard met live or by verified replay of an artifact whose build met it; expansion identity per slot declaration; v5 unchanged | Items 3, 5, 6; 16A-1 (k), (k2) |
| Codex r2 | 9 | MAJOR | ACCEPTED: 0b rebuilds the artifact; fsynced `stored`/`publishing` states; canonical decided before publishing; spend checked | Items 4, 5; step 10; 16A-1 (i) |
| Codex r2 | 10, 11, 12 | MINOR ×3 | ACCEPTED: (10) digest over the complete canonical family records; (11) ordered `(chunk_id, section)`, aliases share rank, trimmed spans; (12) `show_unverified=False` stated | Item 4, 16A-1 (h); items 3, 7; item 3 `observed_scope` |
| Auditor r2 | 1 | BLOCKER | ACCEPTED: signed selection rule; list computed on final labels after the step 10 baseline, committed with its hash before 16B | Item 5; step 10; 16A-2 (i) |
| Auditor r2 | 2 | BLOCKER | ACCEPTED: spend meter built and tested in 16A-1; a stop is a declared D64 trigger | Item 8; 16A-1 (m); header |
| Auditor r2 | 3, 5 | MAJOR | ACCEPTED (= Codex r2 #8 and #1) | Items 3, 5 |
| Auditor r2 | 4 | MAJOR | ACCEPTED: `candidate:` expansion declaration; candidate live vs incumbent replay | Items 5, 6; 16A-1 (k) |
| Auditor r2 | 6 | MAJOR | ACCEPTED: explicit `privacy` in the library; tests pass `"public"`; H0 lock must pass; AST test | Item 1; 16A-1 (a), (c) |
| Auditor r2 | 7 | MAJOR | ACCEPTED: public by registered path + sha256, unaffected by migrated copies; migrated rows `source: legacy`, not needles | Item 1; item 4 `migrate` |
| Auditor r2 | 8 | MAJOR | ACCEPTED: synthetic `held-out`-labelled set and a canonical-shaped run in P0 | P0; 16A-1 (d) |
| Auditor r2 | 9 | MAJOR | ACCEPTED: development artifact built live on final labels in step 10, keyed so label changes never invalidate it | Item 5; step 10 |
| Auditor r2 | 10 | MAJOR | ACCEPTED: new families are born marked in `eval/private/sealed/pool/` | Steps 2–4; item 4 |
| Auditor r2 | 11 | MAJOR | ACCEPTED: subagents draft, check, dedup and paraphrase sealed data with counts-only replies; the orchestrator never reads it; caveat in D71 | 16A-2 blindness; steps 2–4 |
| Auditor r2 | 12 | MAJOR | ACCEPTED: dispatch prohibition; store-access stamp + audit-log window check as the data source; instruction-enforced residual disclosed | Item 1 stamp; step 2; D65 |
| Auditor r2 | 13 | MAJOR | ACCEPTED: dedup against all development, held-out v1 and regression scenarios | Step 3 |
| Auditor r2 | 14 | MAJOR | ACCEPTED: trusted scanner loads its own needles; S-needle scope; clean-report criterion; an abort never consumes a slot | Item 1 scanner; 16A-1 (i); 16A-2 (g) |
| Auditor r2 | 15 | MAJOR | ACCEPTED: colleague round-trip inside OS-2, 7-day default deadline, demotion fallback (no resolver needed) | OS-1 Q5; steps 7–8 |
| Auditor r2 | 16 | MAJOR | ACCEPTED: seed committed before split; private assignments; demotion log replayed by `verify`; band fallback after attrition | Item 4; step 8; 16A-1 (h) |
| Auditor r2 | 17 | MAJOR | ACCEPTED: source mix is an owner question with a stated default; "settled at the plan gate" removed | OS-1 Q2–Q4; D71 |
| Auditor r2 | 18 | MAJOR | ACCEPTED: OS-1 blocks 16A-2; manifest incl. `codex_clause` checked | Header; 16A-2 (a) |
| Auditor r2 | 19 | MAJOR | ACCEPTED: commit messages, tags, PR body, comments and review comments scanned, re-run after edits; D71 cites flagged items by id and outcome only | Item 1 scanner; step 11; 16A-2 (g) |
| Auditor r2 | 20 | MAJOR | ACCEPTED: `owner_status` + `second_status`; "required" defined; κ with raw agreement and PABAK; phrasings validated | Item 2; steps 7–8; 16A-2 (c) |
| Auditor r2 | 21, 22, 24 | MINOR ×3 | ACCEPTED: (21) sealed totals only, no strata committed; (22) out-of-tree `O_EXCL` reservation; (24) drafts carry `draft_key`/`draft_family`, `migrate` and `pool` issue ids | Item 4; 16A-1 (h), (i) |
| Auditor r2 | 23, 25 | MINOR ×2 | ACCEPTED: (23) the `src/text_utils.py` move is item 3's first commit; (25) every absorbed-map key must exist in the production index | Items 3, 7; 16A-1 (l), (n) |
| Auditor r2 | 26, 27 | MINOR ×2 | ACCEPTED: (26) [W] work uses synthetic data only, real-row checks and the heldout sha256 are [C]; (27) `--legacy-public` for hashes in `eval/legacy_public.json`, frozen at the 16A-1 merge | Lanes; item 1 `classify` (6); 16A-1 (d), (e) |
| Auditor r2 | 28, 29 | MINOR ×2 | ACCEPTED: (28) the negative result is a merged deliverable, PR headline = result; (29) canonical decided before `publishing`, non-canonical → `failed` unpublished → 0b eligible | Stop paths; item 4 states; 16A-1 (i) |

No finding was rebutted. Two directions were adjusted on evidence: only `pipeline query` writes the audit
log (`src/pipeline.py:196–255`), hence the store-access stamp; and section ids and legal terms occur in
tracked code (`src/chunker.py:202`), hence S-needles scan templated sealed outputs, not the repository.

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
