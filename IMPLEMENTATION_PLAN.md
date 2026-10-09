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

## Phase 16A — eval foundations — `phase-16a1-eval-instrument` → `v2.3.0`, then `phase-16a2-eval-data` → `v2.4.0` (spec v2, 9 Oct 2026: Codex plan-gate round 1 reconciled, see the dispositions table at the end)

**Authority:** `docs/designs/003-roadmap-and-next-actions.md` §5 16A (normative) and §1.2 (Q1 scope
classes, Q3 labeller + second reviewer, Q7 tutorial material private); astra B3, M8–M10; D54 (absorbed
labels), D57 addendum (re-open condition), D59 (C4 and item 9 deferred here), D61, D64.
- Runs under the standing go (D64). 16A is split so the instrument (16A-1) never waits on the owner;
  only the data (16A-2) needs owner time, in **one** batched session.
- **Ordering rule (003 16A.1):** no private question enters any eval run, cache or judge call until
  16A-1 is merged.
- D65–D70 are written during 16A-1, D71–D72 during 16A-2 (see Ledger).
- **Sealed-output rule (whole phase):** anything printed, committed or returned to the orchestrator
  about a sealed set is pre-registered aggregates only: no row or family ids, sections, ranks or
  per-row outcomes. Per-row sealed detail exists only under `eval/private/sealed/`, and Claude never
  opens it.

**Settled at the plan gate (orchestrator, 9 Oct; each is applied in the item named):**
- the pre-registration is signed at OS-2 (16A-2 step 6);
- the absorbed map is committed in `eval/` with no re-index, and credits a chunk only when that
  chunk's own text span covers the absorbed section (item 7);
- realistic v2 (≥40 families) may use Claude-drafted lay phrasings; colleague queries are additive
  (16A-2 step 2);
- blind drafting from the handbook is acceptable, with its caveat recorded in D71 (16A-2 step 2);
- a Read-deny hook for `eval/private/sealed/` is deferred: until the harness adds one, the
  never-open rule is instruction-enforced only, recorded as a D65 residual (item 1);
- held-out v1 moves to role `regression` once sealed v2 lands, and is still reported (16A-2 step 1);
- tags: `v2.3.0` (16A-1), `v2.4.0` (16A-2).

**Lanes** (every work item carries one):
- **[C] Claude only:** touches corpus, `chroma_db/`, tutorial or private eval content (R1), or is
  judgment-bearing integration.
- **[W] worker-eligible (R0):** code that never needs corpus or eval content, built and tested on
  synthetic fixtures only.
  - Dispatched through `harness-worker` (Grok primary, DeepSeek/GLM fallback), but only after Track B's
    B2 escape probes pass and OS-1's `worker_allow` is registered. Otherwise Claude implements it.
  - Workers never receive real eval rows, `eval/private/`, held-out files or report output.
  - Integration, the R1 canaries and the commit stay with Claude (`Implemented-by:` trailer).

**Owner hard stops (D64), batched into two pings:**
- **OS-1 (start of 16A-1; non-blocking):**
  1. Review the `.harness/project.toml` diff and run `harness-init --refresh` (owner-only). The diff adds
     `^eval/private/` to `never_commit`, adds `eval/private` to `restricted_read`, and puts the [W] globs in
     `worker_allow`.
  2. Say which further tutorials exist for 16A-2 sourcing, and whether colleagues' real queries can be
     collected (optional).

  If OS-1 goes unanswered, Claude does the [W] items. `eval/private/` is still protected by `.gitignore`,
  `allow_ignored_adds = false` and the D63 check.
- **OS-2 (16A-2 step 6; blocks sealing):** the single validation session.
- Nothing else in 16A pings, except a D64 trigger (see Stop paths). The D64 triggers named in this
  phase are:
  - spend that would cross the weekly cap without approval;
  - a re-epoch, a replacement sealing, or any amendment to the signed pre-registration or the
    canonical v6 guard list (each changes the eval protocol);
  - a 16A-2 target or criterion that cannot be met as approved.

### 16A-1 — instrument (fully agentic)

**Evidence (checked on `main` 9ce4e07):**
- **Question text in reports.** It reaches reports at `src/evaluator.py:1402–1413` (legacy
  `_format_report`) and `:2549`, `:2572` (`_format_matrix_report`; 003's `:2297–2301` drifted after H).
  Both reports are printed (`:1550`, `:2167`).
- **Per-row detail beyond text.** The matrix detail line also prints expected sections, retrieved
  sections and ranks (`:2546–2549`), so id-only rendering would still expose sealed rows.
- **Per-row state keyed by text.** Rows are keyed by question text at `:1899–1965` and `:2533–2559`.
- **Judge dump.** The record carries the question (`src/judge.py:271`); the destination is configured
  at `src/pipeline.py:642` and the file is written at `src/evaluator.py:2171–2173`.
- **Expansion caches.**
  - `run_eval_matrix` builds a live, in-memory expansion cache keyed by question text
    (`src/evaluator.py:1711–1724`); nothing can inject a frozen one.
  - `scripts/w_sweep.py` caches expansions keyed by question text, and prints 60/70-character question
    prefixes (`:84`, `:150`, `:196`).
- **Bake-off report.** `scripts/bakeoff_report.py`:
  - parses `:: question` (`:95`) and keys flips by text (`:487–508`);
  - matches the D54 roster by question prefix (`:544`), and role coverage likewise (`:603`);
  - `compare_prod_ranks` skips a row missing from either arm (`:786`), so a missing baseline HIT
    yields zero flips plus an "unmatched" note, not a failure.
- **Schema loss.** `load_golden_set` validates at `:200` but projects only `question`, `type` and
  `expected_sections` (`:205–210`); section matching is OR-only (`:397`).
- **Non-determinism in report text.** Both formatters print `datetime.now()` (`:1326`, `:2210`) and
  git provenance.
- **Held-out by label.** Canonical v5 finds the held-out set by the `held-out` label token
  (`HELDOUT_LABEL_TOKEN`, `:2043`, `:2341`).
- **Terminal drafts keep citations.** `generate_with_sources` validates citations (`src/generator.py:427`)
  before a terminal status overrides the gate outcome (`:433`).
- **Absorbed sections can split.** An oversize merged segment is re-split into several chunks that share
  one `section_number` (`src/chunker.py:491–504`); chunk ids are content hashes
  (`src/embedder.py:378–406`).
- **Dev sets today:**
  - golden: 35 rows (23 direct / 7 exact_token / 5 refusal; 11 multi-section);
  - realistic: 23 rows (16 / 1 / 6; 11 multi-section);
  - no row in either set has an `id`.

### Work
0. **P0 — pre-implementation captures [C]** (H0 precedent; before any code edit).
   - `scripts/p16_capture_projection.py` (committed) runs both v5 report formatters on `main` over
     fixture-driven runs (faked retrieve, generate, expand and judge) of the three registered public v1
     sets (golden, realistic, sample).
     - The clock, the set paths and the provenance are injected fixed values, so the text is
       deterministic.
   - It writes `tests/fixtures/p16_v5_projection_main.json`: `{"main_sha": ..., "reports": {...}}`.
     Only `reports` is compared; `main_sha` is provenance.
   - The orchestrator stores the retrieval-ablation rows of H (j)'s offline command on `./chroma_db`
     (golden + realistic) at `data/research/p16_offline_baseline_main.md`, with the SHA.
1. **Set registry, content identity + privacy classes (D65).**
   - **[W] Registry.** `eval/sets.json` (committed) plus `src/eval_sets.py` record per set:
     - `path`;
     - `privacy`: `public` / `private` / `sealed`;
     - `role`: `tuning` / `realistic` / `sealed` / `regression`;
     - `status`: `active` / `retired`;
     - `sha256` of the approved content (every set), so a registration is bound to its bytes;
     - for sealed sets: `commitment` (file sha256 + sha256 of the sorted `family_id` list) and `epoch`.

     The public sets are v1 `golden_set`, `realistic_set`, `sample_golden_set` and `heldout_set`.
   - **[W] Row index.** `eval/private/row_index.json` (gitignored: a public hash of a short question is
     guessable) maps, for every registered row, sha256 of its `id` and of its normalised question (NFKC,
     casefold, punctuation → space, whitespace collapsed) to its set, class and status. `eval_sets.py`
     rebuilds it from the registered files.
   - **[W] Classification follows content, not path.** Before any evaluation, every loaded file is
     resolved row by row:
     - any row matching a sealed row makes the whole file `sealed`, whatever its path, name, row order,
       subset or serialisation;
     - a row matching any other registered row takes that set's class and status; a file mixing classes
       takes the strictest;
     - an unregistered file with no match is `private` (fail closed); if the row index is missing or
       unreadable, every unregistered file is refused (registered files still resolve by path +
       content sha256);
     - registering a sealed set is refused if any row matches a development, retired or regression row,
       so retired content cannot regain sealed status by renaming.
   - **[W] Derived artifacts inherit.** Caches, judge dumps, rank dumps, split files and reports carry a
     `source_commitments` header (registry ids and content hashes of their inputs) and take the strictest
     class of those inputs. `bakeoff_report`, `w_sweep` and every selection tool classify an input by
     that header and its row membership, never by filename. A derived artifact with no header is
     `private`.
   - **[W] Sanitiser.** `src/eval_privacy.py`:
     - **Primary: an allowlisted serialiser.** Output whose resolved destination is outside
       `eval/private/` is built only from metric fields and issued ids:
       - public sets: as today (question text allowed);
       - private sets: opaque ids + metrics;
       - sealed sets: pre-registered aggregates only (no row or family ids, sections, ranks or per-row
         outcomes). A run that loads a sealed set emits aggregates only for **every** set it loads.
     - **Secondary: `leak_scan(text, needles)`.** It returns row ids (or "sealed"), never the needle.
       - It decodes JSON and `\u` escapes, then normalises as the row index does.
       - Needles are every non-public question as any 8-token window (the whole question if shorter),
         plus, for sealed rows, each gap keyword (whole word) and each serialised evidence group.
       - A window that also occurs in a registered public question is dropped, so public text (e.g. a
         migrated public row inside a private file) can always print.
     - **Opaque ids.** Non-public ids are content-free (item 2), so an id carries no content.
   - **[C] Wiring:**
     - Both formatters go through the serialiser.
     - Full private detail goes to `eval/private/reports/<run>.md`; sealed per-row detail goes to
       `eval/private/sealed/reports/` only.
     - stdout prints serialiser output only.
     - With any non-public set loaded, `[eval]` warnings, per-row exceptions and top-level CLI exceptions
       are logged as id (sealed: a count) + exception type, never `str(exc)` or a traceback carrying row
       values.
     - For non-public sets, the judge dump, expansion caches and `bakeoff_report` / `w_sweep` output key
       by id (truncated prefixes included) and live under `eval/private/` (sealed: `eval/private/sealed/`).
     - **Containment is by resolved destination** (`os.path.realpath`): a symlink or `..` path leaving
       `eval/private/` counts as outside.
     - **Defence in depth:** `leak_scan` runs before every print and every write outside `eval/private/`.
       A hit aborts, names the row id (sealed: "sealed") and writes nothing.
   - **[C] Trusted sealed processors.** Only these open sealed files, and each prints counts and hashes
     only:
     - the evaluator, under a reserved slot (item 4);
     - `src/eval_sets.py` (hashing, registration, row index);
     - `scripts/validate_eval_set.py`;
     - `scripts/eval_split.py pool|split|seal|verify`;
     - `scripts/scan_leaks.py`;
     - `scripts/apply_validation.py` (OS-2 packet build and apply).

     Any other tool given sealed content refuses.
   - **[C] Hygiene and rules:**
     - `.gitignore` gains `eval/private/`.
     - `scripts/check_never_commit.py` gains `^eval/private/` (extends D63).
     - The CLAUDE.md do-not-read clause gains `eval/private/`.
     - New CLAUDE.md hard rules: the sealed-output rule above, and "Claude never opens
       `eval/private/sealed/`; only the trusted processors read it".
     - **Residual (D65):** a Read-deny hook for `eval/private/sealed/` is deferred to the harness; until
       then the never-open rule is instruction-enforced only.
2. **Schema v2 (D66) [W].** `src/eval_schema.py` plus `scripts/validate_eval_set.py`, which prints counts
   and line numbers only, never text.
   - **Version marker:** rows carry `"schema": 2`, and a file mixing v1 and v2 rows is rejected.
   - **Identity:**
     - `id` is unique per set; `family_id` names a scenario and all its paraphrases; `question` is the
       text.
     - Public v2 sets: `id` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$`.
     - Non-public sets: ids are content-free. `family_id` is issued by `eval_split.py pool` (item 4)
       and matches `^f[0-9a-f]{8}$`; `id` is `<family_id>-<n>`, n a phrasing counter. Any other
       pattern is rejected.
     - A sealed set must be schema 2.
   - **`scope`:** `answer` | `partial` | `refuse`. This is the Handbook-mode scope (D61); Research-mode
     labels belong to Phase 19.
   - **`evidence`:** a list of required groups (AND). Each group is a non-empty list of interchangeable
     sections (OR). It is `[]` if and only if `scope == refuse`.
   - **`gaps`:** for `partial` rows only, and non-empty there: `[{"id", "keywords": [...]}]`, where each
     `keywords` list is non-empty and every keyword is a non-empty string.
   - **Optional fields:**
     - `type` (must agree with `scope`);
     - `register`: `modelled` / `lay` / `terse`;
     - `source`: `handbook` / `tutorial` / `colleague` / `legacy`;
     - `label_status`: `draft` / `owner` / `second_reviewed` / `disputed`;
     - `ambiguous` (bool).
   - **v1 rows load exactly as today** (`load_golden_set` output is byte-equal). Internally each v1 row
     gets:
     - `id = "q:" + sha256(question)[:12]` in a public file, and the content-free `L<line number>` in a
       non-public file;
     - `family_id = id`;
     - one OR group;
     - a scope derived from `type`.
3. **Evidence-group and PARTIAL scoring (D66).**
   - **[W] `score_evidence(groups, retrieved_sections, mode)`.**
     - `completion_rank` is the maximum, over groups, of the first rank matching any member of that
       group. Strict means equal; related uses today's nesting rule.
     - It is `None` if any group is unmatched. It also reports `groups_covered@k`.
     - hit@k holds iff `completion_rank ≤ k`. With one group this is identical to today's
       `first_*_rank`.
   - **[W] `observed_scope(result)`**, defined independently of citation-gate names, first match wins:
     1. `unscored`: `generation_status` is not `complete` (truncated, declined, incomplete, unknown,
        error, not_run), the gate outcome is terminal, or the gate outcome is missing on a non-refusal;
     2. `withheld`: gate outcome `CITATIONS_UNVERIFIED` (the user sees a block notice, not the draft);
     3. `refuse`: `is_refusal(answer)` (`src/generator.py:201`, the existing normalisation);
     4. `partial`: some sentence passes H2's narrow gap-statement test (the prefix list, imported from
        `render.py`, and no hedge words);
     5. `answer`: everything else.
   - **[W] `score_partial(result, groups, gaps)`.**
     - H's generation-status exclusions apply first: an `unscored` row is counted and never correct
       (canonical v6 requires zero of them, as v5 does).
     - Otherwise the row is correct iff all of these hold:
       - the gate outcome is a displayed answer: `CITATIONS_VERIFIED` or `PARTIALLY_VERIFIED`;
       - `is_refusal(answer)` is false;
       - at least one verified citation related-matches a required group;
       - every gap is *stated*: some sentence passes the gap-statement test and contains one of that
         gap's keywords as a whole word, case-insensitively.

     Gap recall is reported separately. Misses are documented, as in H2.
   - **[C] Integration.** v2 sets go through the new scorers; v1 sets go through the untouched v5 path.
   - **Report v6** is used iff any loaded set is schema v2. It adds:
     - family counts, with the eligible denominator of each metric (item 5);
     - a scope confusion table: labelled answer/partial/refuse × observed
       answer/partial/refuse/withheld/unscored;
     - family-level rates with Wilson CIs on families (`unavailable` on an empty denominator);
     - the cohort block (item 6) and the expansion-artifact identity (item 5).
   - **Canonical v6** keeps every v5 guard, and adds:
     - the headline set is the registry's active `role = sealed` set (not the `held-out` label token);
       its commitment verifies, on a reserved, declared slot (items 4–5);
     - `eval/preregistration.toml` is signed, and its hash is in the report;
     - the expansion artifact is complete (item 5);
     - `leak_scan` is clean.
   - **v5 compatibility.** Runs over registered **public** v1 sets only stay report v5, byte for byte,
     given a fixed clock, paths and provenance. A v1 file that resolves `private` follows the sanitiser
     and is deliberately not byte-compatible.
4. **Family pool, family-first split and sealing (D67).**
   - **[W] `scripts/eval_split.py pool`.** Builds **one** deduplicated family pool from migrated and
     newly drafted families, before any assignment:
     - it issues the opaque `family_id`s and row ids (item 2);
     - it freezes `eligible_sealed` per family. **Ineligible:** every migrated family, every T1/T7
       family, any family ever retrieved, and any new family marked `dev_sibling` (same scenario as a
       development family; 16A-2 step 2);
     - it writes `eval/pool_manifest.json` (committed): counts by source, scope and eligibility, and the
       sha256 of the sorted family list. `split` refuses a pool that differs from the manifest.
   - **[W] `split_families(pool, seed, quotas, strata)`** (pure core):
     - It sorts families by `family_id` before the seeded shuffle, so input order cannot change the
       result.
     - Each family goes to exactly one split, and all its phrasings follow it.
     - The sealed split draws only from `eligible_sealed` families.
     - **Strata:** scope × primary chapter. The primary chapter is that of the first evidence group's
       first section; a family whose groups span chapters is stratum `multi`; refusal families are
       stratum `refuse`.
     - **Refusal band:** 25–30% of the **families** in each split.
     - An infeasible quota or band fails with counts and writes nothing.
   - **[W] `seal` and `verify`.**
     - `seal` writes `eval/split_manifest.json` (committed): the seed, the pool-manifest hash, per-split
       and per-stratum family/row counts, each split file's sha256, and the sha256 of each split's
       sorted `family_id` list. It also registers the sealed set (registry + row index, item 1).
     - `verify` recomputes all of it.
     - Re-splitting while a manifest exists is refused (no seed shopping).
     - `--new-epoch` overrides this only with recorded owner approval (a D64 hard stop: it changes the
       eval protocol) plus fresh validation of the new sealed split, under its own D-entry.
   - **[C] Sealed-access guards:**
     - **Before any sealed retrieval, expansion or model call,** the evaluator requires all of:
       - `eval/preregistration.toml` is `signed` and its hash equals the signed hash;
       - the commitment verifies and `status == active`;
       - `--sealed-slot N` names a scheduled slot whose candidate is declared (item 5);
       - the slot has just been reserved by this process.
     - **Reservation is atomic and first:** an exclusive create (`O_CREAT|O_EXCL`) of
       `eval/sealed_slots/<epoch>-<N>.json` (committed; state and aggregates only).
       - States: `reserved` → `completed` | `failed`.
       - A failed, interrupted or non-canonical attempt **consumes** the slot. A reserved slot never
         returns to unused.
     - Each completed or failed slot appends a line to `eval/sealed_ledger.md`: date, commit, slot,
       state, purpose, pre-registered aggregates. No ids, sections or text.
     - `bakeoff_report` and `w_sweep` refuse sealed content by content identity and `source_commitments`
       (item 1), whatever the filename (extends the C2 guard).
     - Any other exposure (a sealed detail report opened, sealed retrieval outside a slot) sets
       `status = retired` and role `regression`. Replacement sealing is a D64 hard stop (owner
       approval, fresh validation).
5. **Pre-registered analysis + frozen expansions (D68).**
   - **[W] `src/eval_stats.py`** (stdlib only: `math.comb`, `math.erfc`, no scipy). It provides:
     - family collapse (`family_outcome = all | majority`; a majority tie is a MISS);
     - an exact two-sided McNemar test on family outcomes (the primary test);
     - Durkalski's clustered McNemar χ² as the sensitivity analysis, on phrasing outcomes clustered by
       family. With singleton clusters it reduces to the **uncorrected asymptotic** McNemar χ², not the
       exact test; a zero denominator (every family's gains equal its losses) is `unavailable`;
     - Wilson CIs on families; an empty denominator is `unavailable`, never `[0, 0]`;
     - exact power and MDE as a function of the eligible family count N, the gain/loss probabilities
       (p10, p01) or a discordance grid, the adjusted α and the target power. Exact power is not
       monotone in N (discrete test), so MDE is the smallest effect reaching target power at that N;
     - the Holm correction over the full schedule K (verdict rule below);
     - `critical_verdict`: any strict HIT→MISS on a critical-control family FAILs, whatever the
       p-value. It is kept apart from `exploratory_verdict`;
     - deployment variance over R draws (below): per-family flip frequency and the range of the family
       rate.
   - **Eligible populations.** Retrieval metrics count `answer` + `partial` families only (refusal
     families have no evidence); scope and refusal metrics count all families. Every rate, its Wilson CI
     and its MDE use the same eligible denominator, which the report prints. (With ≥50 sealed families
     at 25–30% refusals, about 35–37 are retrieval-eligible.)
   - **Verdict per confirmatory slot** (one hypothesis: the slot's candidate vs the incumbent on the
     primary metric):
     - **PASS** iff the difference favours the candidate, the adjusted p ≤ α, the point estimate ≥ MWE
       (the MWE gates the point estimate; the CI is reported, not gated), and `critical_verdict` is not
       FAIL;
     - **FAIL** iff `critical_verdict` FAILs, or the adjusted p ≤ α favouring the incumbent;
     - **INCONCLUSIVE** otherwise.
     - The interim adjusted p is K·p, the Holm upper bound, valid before later slots exist. The final
       Holm table at schedule close is the reported verdict; it can upgrade an INCONCLUSIVE, never
       reverse a PASS or FAIL.
     - Unused and failed slots enter Holm with p = 1, and K is never reduced to the slots that ran.
   - **Candidate rule (adaptive selection).**
     - Candidates are chosen on development evidence only.
     - Before its slot is reserved, a candidate is declared in an append-only `[[slot]]` entry of the
       pre-registration: config hash, commit, and the hashes of the development reports that selected
       it. A sealed run on an undeclared slot is refused.
     - A candidate conceived after any sealed result gets its own new slot within K, declared before
       its sealed run; it is never re-run in a spent slot. Anything beyond K is exploratory only, and
       raising K is a protocol change (D64 hard stop).
   - **[W] Frozen expansion artifact** (`src/expansion_artifact.py`), the "cached expansions" of 003
     16A.5:
     - keyed by (`cohort_fp`, row id); bound to the rewrite model id, the prompt sha256 and the rewrite
       config hash; it carries a `source_commitments` header (item 1);
     - `run_eval_matrix` accepts an injected artifact and replays it identically in every arm;
     - a missing entry or an identity mismatch fails the run; it never regenerates;
     - the report records the artifact identity and separate `rewrite_live` / `rewrite_replayed`
       counters. v5 runs keep today's live expansion and its accounting untouched;
     - a candidate that changes the rewrite itself builds its own artifact inside its own slot;
     - the sealed artifact is built live inside slot 0, from entries that are all `live` (zero
       fallbacks), and is stored under `eval/private/sealed/`.
   - **Deployment variance.** Each of R draws regenerates expansions live on the **development** sets,
     then reruns retrieval and family scoring. Flip frequency (against the frozen-artifact run on the
     same sets) and the family-rate range are computed from those R full runs.
   - **[C] `eval/preregistration.toml`** (draft). It fixes:
     - the unit (the family) and the family-outcome and tie rules;
     - the primary metric: family strict@6, hybrid+rewrite, on the frozen expansion artifact;
     - the eligible population of each metric;
     - the minimum worthwhile effect (MWE);
     - family-wise α = 0.05, Holm over K; target power 0.8, with the MDE reported over a discordance
       grid (0.2 / 0.3 / 0.5);
     - the slot schedule: slot 0 is the incumbent baseline (descriptive only), with one descriptive
       spare 0b usable only if slot 0 ended `failed` before emitting any aggregate; plus K confirmatory
       slots for 16B under the candidate rule;
     - the critical-control family ids: from the development side, non-empty, each an incumbent strict
       HIT when signed;
     - R, the number of variance draws;
     - the verdict rules above.

     It is signed at OS-2. Any later change other than a `[[slot]]` declaration is a protocol amendment
     (D64 hard stop).
6. **C4 cohort identity + item-9 backlog (D69).**
   - **[C] Cohort block and comparisons:**
     - Report v6 carries a cohort block per set: path, privacy, schema, sha256 (commitment only for
       sealed sets), rows, families, and `cohort_fp` = sha256 over the sorted `(id, evidence fingerprint,
       scope)` tuples.
     - `compare` and `compare_prod_ranks` key rows by `(set sha256, id)`, and stop matching production
       rows by position.
     - **Before comparing,** they require in both arms exact, unique coverage of the eligible ids, with
       every required outcome field present. A missing, extra or duplicate row **fails** (today's
       "unmatched" skip at `bakeoff_report.py:786` goes).
     - They refuse arms whose set hashes, expected sections, `cohort_fp`, scorer version, absorbed-map
       hash (item 7) or expansion-artifact identity (item 5) differ.
     - The critical-control set must be non-empty, resolve completely, and be all incumbent strict HITs
       in the baseline arm; otherwise the comparison fails rather than passing vacuously.
     - The D54 roster and role coverage move from question prefixes to ids.
     - For v5 reports this is a compare-time check only; the report output is unchanged.
   - **[W] Item 9:**
     - `w_sweep.py` loses its module-level `chdir` and cwd-relative paths;
     - duplicated helpers are merged, and the arm roster is single-sourced.
   - **[W] D62 follow-ups:**
     - one answer_fn status wrapper replaces today's three copies;
     - `split_sentences` moves to a shared text module.
7. **Absorbed-section metadata (D70).**
   - **[W] Chunker side channel.** The chunker exposes, as a pure side channel, each D20 runt merge as
     (absorbing section, absorbed section, the absorbed span in `clean_text`), and carries each final
     chunk's own `clean_text` span through the oversize re-split. Chunk text, ids and metadata are
     unchanged.
   - **[C] `scripts/absorbed_map.py`.** It runs the side channel on the corpus, with no re-index, and
     commits `eval/absorbed_sections.json` (versioned and hashed; chunk ids and section numbers only):
     - keyed by production chunk id (`compute_chunk_id`, a content hash, so the index is untouched);
     - a chunk lists an absorbed section only if that section's whole span lies inside the chunk's own
       span. A sibling sub-chunk with the same `section_number` but without the passage gets no entry,
       and a find-miss sub-chunk (span unknown, `chunker.py:494–504`) gets none.
   - **Scoring:** v2 strict scoring may credit a retrieved chunk for an absorbed section only through
     that chunk's own map entry (`absorbed_match`, v6 only). The map's version and hash are bound into
     comparisons (item 6).
   - **Unchanged:**
     - D54's manual repairs stay in the v1 files;
     - production metadata and the grounding gate are untouched (that is the Phase 18 registry).
8. **Ledger + docs [C].**
   - D65–D70.
   - CLAUDE.md: the canonical v5 and v6 conditions, the privacy and sealed rules, and `eval/private/`.
   - `docs/harness.md`: the changelog, plus the negative-result phase path as defined in 16A-2's Stop
     paths (it is not defined there today).
   - The `Current phase` and `Next:` lines.

### Acceptance (Tier-1)
- **(a) Suite and CI.** The suite is green, and CI is green, including the `never-commit` check.
- **(b) Canary leak test** (`tests/test_eval_privacy.py`).
  - **Setup:** synthetic private and sealed sets carry a **distinct** `P16-CANARY-<field>-<uuid>`
    sentinel in each sensitive field: question start, question end, a gap keyword, an evidence group,
    an id-like value in a malformed row, a per-row exception message and a top-level CLI exception.
    All models are faked.
  - **It runs through:**
    - `run_eval_matrix` and legacy `run_eval`;
    - the judge dump;
    - `w_sweep` (including its 60/70-character prefix prints) and `bakeoff_report`;
    - `eval_split pool|seal` and `validate_eval_set`;
    - a malformed-row loader error and a top-level CLI exception.
  - **Each sentinel must be absent**, also as any 8-token window and as a JSON- or `\u`-escaped,
    re-cased or re-spaced copy, from stdout, stderr, `caplog`, and every file whose resolved path is
    outside `tmp/eval/private/`.
  - **It must be present** in the private detail report.
  - **Failure cases:** a formatter forced to inject (i) a whole question, (ii) a truncated prefix,
    (iii) a reformatted copy or (iv) a sealed gap keyword or evidence group makes `leak_scan` abort the
    write; a symlink in `tmp/eval/private/` pointing outside counts as outside and aborts.
- **(b2) Sealed aggregates only** (tested separately from (b)). A sealed fixture run's stdout,
  committed report, ledger line, slot file and returned result contain no row or family id, section,
  rank or per-row outcome: a regex over the fixture's ids and sections finds nothing. Public sets
  loaded in the same run also render as aggregates.
- **(c) Classification by content.**
  - An unregistered file with no match is private and rendered by ids only.
  - A copy, rename, reorder, subset, pretty-printed reserialisation, and a derived cache or dump of a
    sealed fixture are each classified sealed, and refused without a slot.
  - A retired fixture copied under a new name cannot be registered as sealed.
  - A missing row index refuses unregistered files.
- **(d) v5 unchanged:**
  - `tests/test_p16_projection.py` reproduces `p16_v5_projection_main.json`'s `reports` byte for byte,
    with the clock, paths and provenance injected as in P0;
  - `load_golden_set` output is byte-equal on the three v1 files;
  - the H (j) offline command reproduces the P0 retrieval-ablation rows exactly (orchestrator-verified);
  - each v5 canonical guard, removed alone, makes the run non-canonical (one test per guard);
  - the CI greps are unchanged.
- **(e) Evidence groups:**
  - two groups both hit: the rank is the later group's first hit;
  - one group missing: a miss at every k;
  - OR alternates within a group;
  - a parent matches under related but not under strict;
  - a property check: on every v1 golden and realistic row, single-group ranks equal the v5 ranks (fake
    retriever);
  - `groups_covered@k` is reported.
- **(f) PARTIAL and observed scope:**
  - a stated gap in a `CITATIONS_VERIFIED` or `PARTIALLY_VERIFIED` answer scores correct;
  - each of these scores incorrect: a missing gap; a gap keyword in a sentence that is not a gap
    statement; a whole refusal, including a quoted or trailing-period variant caught by `is_refusal`;
    no verified citation in any required group; a `CITATIONS_UNVERIFIED` (withheld) draft;
  - truncated, declined, incomplete, unknown and error drafts that keep verified citations and a
    stated gap are `unscored`, never correct;
  - D32's hedge sentence is not counted as a stated gap;
  - `observed_scope` maps one fixture per class (answer, partial, refuse, withheld, unscored).
- **(g) Schema rejections.** Each rejection names the line and field, never the question. Cases:
  duplicate id, bad id pattern, a non-opaque id in a non-public set, a sealed set below schema 2,
  mixed schema versions, scope/evidence/gaps incoherence, an empty gap-keyword list or keyword,
  `type`/`scope` disagreement, an empty group.
- **(h) Pool and split:**
  - over 200 seeded random synthetic pools, no `family_id` appears in two splits, and every phrasing
    sits in its family's split;
  - the same seed gives the same split, and a permuted pool gives the identical split;
  - an ineligible (migrated, T1/T7, retrieved or `dev_sibling`) family is never sealed;
  - stratum quotas (including `multi` and `refuse`) and the family-level refusal band hold;
  - an infeasible quota fails and writes nothing;
  - a pool differing from its manifest is refused; a re-split with a manifest present is refused;
  - `verify` fails after a one-byte change to any split file.
- **(i) Sealed guards** (fake retriever and model with call counters):
  - with an unsigned or hash-changed pre-registration, no slot, an undeclared, used or unscheduled
    slot, a commitment mismatch or `retired` status, the evaluator refuses with **zero** retrieval,
    expansion and model calls;
  - two processes reserving the same slot: exactly one proceeds;
  - an exception after reservation leaves the slot `failed` and consumed;
  - every tool outside the trusted-processor list refuses sealed content under a neutral name;
  - ledger lines and slot files carry no id, section or text.
- **(j) Stats** (reference values computed 9 Oct, stored in the test):
  - exact McNemar with b=10, c=2: p = 0.0385742;
  - singleton-cluster Durkalski on the same data: χ² = 5.3333, p = 0.0209213, equal to the uncorrected
    asymptotic McNemar and **not** to the exact p;
  - Durkalski on unequal clusters (b_k, c_k) = (2,0), (1,1), (3,0), (0,1), (1,0): χ² = 25/15 = 1.6667,
    p = 0.196706; all-cancelling clusters give `unavailable`;
  - exact conditional power at gain probability 0.8, α = 0.05: 0.2097280 with 7 discordant families and
    0.1677747 with 8 (a discrete **decrease**);
  - N = 50, effect 0.10: power 0.241189 at discordance 0.20 and 0.094453 at 0.80;
  - Wilson: 0/10 → [0, 0.27753], 10/10 → [0.72247, 1], 0/0 → `unavailable`;
  - three paraphrases of one family count once; a majority tie is a MISS;
  - Holm gives the expected result on a known vector; unused and failed slots enter as p = 1 and K is
    unchanged;
  - verdict table: below-MWE with p ≤ α is INCONCLUSIVE; a critical-control flip FAILs even with
    p > 0.5; exploratory flips alone do not FAIL; the final Holm table never reverses an interim PASS
    or FAIL;
  - a confirmatory call is refused while the pre-registration is a draft or the slot is undeclared.
- **(k) C4:**
  - compare refuses a mismatched set hash, expected sections, `cohort_fp`, scorer version, absorbed-map
    hash or expansion-artifact identity;
  - a missing, extra or duplicate outcome row fails (including a missing baseline-HIT row, which today
    yields zero flips and one "unmatched");
  - an empty, non-resolving or non-HIT critical-control set fails;
  - a production-rank fixture with permuted row order still matches by id.
- **(k2) Frozen expansions:**
  - two arms replaying one artifact see identical expansions, with `rewrite_live` = 0;
  - a missing entry fails with zero expansion calls; a model, prompt or config hash mismatch refuses;
  - each of the R variance draws re-expands and re-retrieves every development row (fake expander and
    retriever call counters).
- **(l) Absorbed:**
  - a synthetic runt-merge fixture yields the expected map;
  - a merged **oversize** segment: only the sub-chunk containing the absorbed passage is credited; its
    sibling with the same `section_number` and a find-miss sub-chunk are not;
  - the 16-chunk sample corpus is byte-identical;
  - the 1,470-chunk canary holds (orchestrator-verified);
  - v5 scoring ignores the map.
- **(m) Hygiene:**
  - `git check-ignore eval/private/probe.jsonl` passes;
  - `check_never_commit.py` rejects `eval/private/x.jsonl`;
  - `git ls-files eval/private` is empty;
  - `w_sweep` imports without changing cwd, and runs from a tmp cwd.
- **(n) Ledger.** D65–D70 are present, and the `Current phase` and `Next:` lines are updated.

### Tier-2
- **Offline v6 report on v2 copies.** Run the offline v6 report over v2 copies of the public golden and
  realistic sets (single-group migration). With one family per row, the family-level numbers should equal
  the v5 row-level numbers. The PR records metadata only.
- **Worker lane record.** For each [W] item, record the vendor, attempts and reviewer verdict, or
  "Claude-implemented: workers unavailable". This is B2's first live use on this repo.

### 16A-2 — data + re-baseline
Starts once 16A-1 is merged.
- All content lands in `eval/private/` (Q7).
- **Committed:** counts, hashes, the pool and split manifests, the registry, slot files and the ledger.
  Development opaque ids may appear in committed reports; sealed ids never do.
- Tuning v2 and realistic v2 are one private file each. They mix public and tutorial-derived rows, so
  the stricter privacy class wins.

### Work
1. **Migrate development data [C].**
   - Make v2 copies of golden and realistic: each golden↔realistic pair becomes **one** family. At
     assignment (step 3) the family goes wholly to one development split, and all its phrasings move
     with it. OR groups are kept, except that proposed AND splits are flagged for OS-2.
   - The 24 tutorial rows (T1, T7) are development data, because they have already been through
     retrieval.
   - Every migrated family is `eligible_sealed = false`.
   - The v1 files stay byte-frozen.
   - Held-out v1 moves to role `regression` once sealed v2 lands. It is still run and reported, as a
     regression set, never as the headline.
2. **Draft candidate families [C].**
   - **Who:** fresh-context subagents. `sonnet` drafts; `opus` checks labels against the handbook.
   - **Blind protocol:** the drafters have no retrieval tool and see no rank output.
     - **Caveat (recorded in D71):** drafting reads the handbook, so seeds may sit closer to the
       corpus's own wording than real user queries do. The lay and terse phrasings (step 4) and the
       realistic split partly offset this; the caveat is reported with the baseline.
   - **Sources:** the handbook, plus the further tutorials from OS-1.
   - **What each family gets:** one seed phrasing, plus `evidence`, `scope`, `gaps`, `source` and
     `ambiguous`.
   - **Dedup pass.** A fresh-context `opus` subagent compares each new family's scenario with the
     development families. A same-scenario match is marked `dev_sibling` (ineligible for sealing,
     16A-1 item 4); exact duplicates are dropped. It returns counts only.
   - **Targets, including about 20% surplus for attrition:**
     - tuning: at least 60 families (migrated families count);
     - realistic: at least 40 families in lay register. Claude-drafted lay phrasings are acceptable;
       colleague queries are additive and never blocking;
     - sealed: at least 50 families, so draft at least 60 that are sealed-eligible.
   - **Mix:** refusals are 25–30% of each split's families, and every split has some partial families.
   - **Sealed-eligible** means: never retrieved, not from T1 or T7, not migrated, and not `dev_sibling`.
3. **Pool, then split before paraphrasing [C].**
   - `eval_split.py pool` freezes the pool, ids and eligibility, and commits the pool manifest.
   - `eval_split.py split` runs on the family seeds with the committed seed. The realistic quota comes
     from lay-register families.
4. **Paraphrase [C].**
   - Each family gets 2–3 phrasings (`modelled` / `lay` / `terse`).
   - Sealed paraphrases are written by one separate, fresh-context subagent straight into
     `eval/private/sealed/`. It has no retrieval tool and returns counts only. It is the only sanctioned
     reader of sealed text before sealing, other than the trusted processors.
   - No retrieval runs on the sealed side.
5. **Cost estimate [C]** (before OS-2, so the owner approves a real number).
   - Measure tokens per call with a 3-row dry run on **development** rows. Its spend is logged against
     the D64 cap.
   - Estimate = planned calls × tokens × current prices, covering:
     - the slot-0 canonical run (all modes, generation, the judge, and building the sealed expansion
       artifact);
     - retries at the evaluator's retry limit;
     - the R variance draws on the development sets;
     - the dry-run spend already incurred.
   - Record the price list date and the €/$ rate used.
6. **OS-2 — the batched owner session (hard stop, one ping).** The owner gets a private packet
   (development items in `eval/private/validation/`, sealed items in `eval/private/sealed/validation/`)
   plus a one-page summary of counts, and is asked to:
   1. **Validate labels:** every sealed, refusal, partial and `ambiguous` label (evidence groups, scope,
      gaps), plus the proposed AND splits.
   2. **Rule on the four flagged items:**
      - T01-Bb;
      - the realistic-set refusal label on "planning permission for an extension" (Q1);
      - the missing secondary labels on T07-Ab and T07-Ba;
      - the conflicting handbook dates for the pre-1975 architect's-certificate rule.
   3. **Sign** `eval/preregistration.toml` (16A-1 item 5's full list: MWE, family-outcome and tie rules,
      eligible populations, slots and the candidate rule, verdict rules, critical controls, R) and the
      canonical v6 guard list.
   4. **Approve the cost estimate** (step 5), if it exceeds the remaining weekly €40 cap (D64). The
      approved amount is the estimate + 20% but never above the remaining cap, unless the owner
      approves a higher figure here.
   5. **Forward** the refusal + ambiguous sub-packet to the colleague (Q3). The reply comes back as a
      file.

   `scripts/apply_validation.py` builds the packet, applies the owner's and colleague's verdicts, and
   prints counts only. The orchestrator never opens `eval/private/sealed/`.
7. **Second opinion [C].**
   - D71 records owner–colleague agreement (raw % and Cohen's κ over answer/partial/refuse), plus the
     owner's change rate against the drafts.
   - **Pre-registered fallback (no further ping):** a family whose disagreement isn't resolved, or which
     lacks a required second opinion, leaves the sealed split for development and is marked `disputed`.
8. **Seal [C].**
   - Run `eval_split.py seal` (registers the set and its row index).
   - Commit the manifests, the registry entry (`sealed`, `active`) and the ledger header.
   - If fewer than 50 sealed families survive attrition, do not seal (see Stop paths).
9. **Offline re-baseline [C].**
   - Run the zero-API ablation on tuning v2 and realistic v2 (a partial v6 report). No sealed retrieval
     runs outside a slot.
   - The 16A-1 (d) v1 canary still holds.
10. **One canonical run [C].**
    - **Before reserving slot 0,** check that the approved amount covers the estimate and that the
      weekly D64 cap is not crossed (or the owner approved crossing it).
    - **Run:** one canonical v6 run on sealed slot 0 (the incumbent baseline). It builds the sealed
      expansion artifact and writes `eval/results.md` (aggregates only for the sealed set).
    - **Spend meter:** API usage is metered during the run, and the run stops **before** any call that
      would cross the approved amount. A stopped run consumes slot 0 (`failed`), and goes to the owner.
    - **Spare:** the pre-registration holds one descriptive spare, slot 0b. It is usable only if slot 0
      ended `failed` before emitting any aggregate.
    - Then the R development variance draws.
11. **Ledger [C].**
    - **D71:** the data, sources, counts, validation, the disagreement rate, the drafting caveat and the
      dispositions of the flagged labels.
    - **D72:** the re-baseline outcome, cost, variance and the achieved MDE. The D57 re-open condition
      becomes testable.
    - Update the `Current phase` and `Next:` lines.

### Acceptance (Tier-1)
- **(a) Hygiene.** Suite and CI are green; `git ls-files eval/private` is empty; `eval_split.py verify`
  passes.
- **(b) Manifest counts:**
  - at least 60 tuning, 40 realistic and 50 sealed families; the retrieval-eligible sealed count is
    reported beside the total;
  - 2–3 phrasings per family;
  - refusals at 25–30% of each split's families;
  - zero families straddling splits (migrated golden↔realistic pairs included), and zero sealed
    families that are migrated, T1/T7 or `dev_sibling`.
- **(c) Labels.** Every sealed, refusal, partial and ambiguous row is `owner` or `second_reviewed`, as
  required. There are no `draft` rows in the sealed split, and D71 records the disagreement rate.
- **(d) Flagged items.** The four flagged items each have a recorded disposition.
- **(e) Pre-registration.** It is `signed`, and its hash appears in the canonical report.
- **(f) Canonical run.** The v6 run passes every guard; the slot files and ledger show slot 0
  `completed` (or 0 `failed` with no aggregate, then 0b `completed`) and nothing else.
- **(g) Leak scan.** `scripts/scan_leaks.py` reports 0. The orchestrator runs it: it uses 16A-1 item 1's
  needles (8-token windows, plus sealed gap keywords and evidence groups) over every tracked file,
  the committed reports and the PR body, and prints counts only.
- **(h) Spend.** Actual spend, dry run included, is at most the approved amount (step 6, item 4),
  logged against the D64 cap. The +20% tolerance never overrides the cap: no call crossed the weekly
  cap without the owner's approval of that amount.
- **(i) Ledger.** D71 and D72 are present.

### Tier-2
- **Incumbent baseline.** Family-level strict@6 (hybrid+rewrite and raw hybrid) on sealed v2, realistic
  v2 and tuning v2, with Wilson CIs on families and the eligible denominators. Also the scope confusion
  table and partial gap recall. Held-out v1 is reported as a regression set.
- **Variance and power.** Deployment variance across the R draws, and the achieved MDE at the
  retrieval-eligible sealed family count over the discordance grid. Expectation (computed 9 Oct, exact
  test, 80% power, α = 0.05, K = 1): at N = 36, MDE ≈ 0.26 at discordance 0.3 and 0.34 at 0.5, and none
  reaches 80% at 0.2; at N = 50, 0.22 and 0.29. D68 states this as the honest power constraint.
- **D54 classes.** D54 class counts on the development side.
- **PR content.** The PR carries aggregates and hashes; development opaque ids at most; never sealed
  ids.

### Stop paths
- **Sealed target missed (the negative-result path; `docs/harness.md` gains it in 16A-1 item 8).** If
  sealed v2 stays under 50 families, or any target or criterion cannot be met as approved:
  - commit the tooling and permitted non-private metadata only (counts, hashes, manifests). Private
    development data stays local in `eval/private/`;
  - do not seal;
  - write D71 as a negative result: what fell short and why;
  - STOP and ping (D64: a criterion that cannot pass as approved). 16A-2 is not merged and `v2.4.0`
    is not cut unless its approved criteria pass, or the owner explicitly approves revised criteria.
- **Cost over cap.** If the estimate exceeds the remaining cap and wasn't approved at OS-2, that is a
  D64 spend ping.
  - Meanwhile run the offline re-baseline only, and record the deferral in D72.
  - A deferred canonical run leaves acceptance (f) unmet, so 16A-2 stays incomplete: no merge, no tag.
- **Sealed exposure.** If a sealed detail report is opened, or sealed retrieval runs outside a slot:
  - retire the set to regression (D67) and record it;
  - re-sealing (a re-epoch or replacement set) is a D64 hard stop: owner approval plus fresh
    validation, before 16B's confirmatory slots.
- **Protocol amendment.** Any change to the signed pre-registration (other than a `[[slot]]`
  declaration) or to the canonical v6 guard list is a D64 hard stop.

### Gates
1. **Plan gate** on this section: plan-auditor + Codex `gpt-6.1-sol`, both read-only, with the re-gate
   stopping rule.
2. **16A-1:**
   1. Implement ([W] items go through `harness-worker` where eligible).
   2. `/phase-gate 16A-1`.
   3. Codex merge review.
   4. PR and CI.
   5. Merge and tag `v2.3.0` under D64.
3. **16A-2:**
   1. Steps 1–5.
   2. OS-2 (step 6).
   3. Steps 7–11.
   4. `/phase-gate 16A-2`.
   5. Codex merge review, on code and committed metadata only (`eval/private/` is in `restricted_read`).
   6. PR and CI.
   7. Merge and tag `v2.4.0` under D64, only if every 16A-2 Tier-1 criterion passed (see Stop paths).

### Ledger (from D65)
- **D65:** privacy-aware eval reporting.
- **D66:** eval-set schema v2, evidence groups, PARTIAL scoring and observed scope, report/canonical v6.
- **D67:** family pool, family-first split, sealing, content-identity classification, sealed slots and
  retirement.
- **D68:** pre-registered analysis, verdict and candidate rules, frozen expansions (addendum at OS-2 with
  the signed parameters).
- **D69:** cohort identity (C4) + item-9 closure.
- **D70:** absorbed-section metadata (D54 follow-through).
- **D71:** eval data v2 and label validation.
- **D72:** re-baseline outcome.

### Plan-gate dispositions (Codex round 1, 9 Oct)
Codex `gpt-6.1-sol`, read-only, reviewed `425bdcc` vs `main` `9ce4e07`: **REVISE** (4 BLOCKER / 11
MAJOR / 1 MINOR). Each finding was checked against the plan text and the cited code; the statistical
claims were recomputed independently (stdlib Python, exact enumeration). "Item" means a 16A-1 work
item, "step" a 16A-2 step, and a bare letter a Tier-1 acceptance criterion.

| # | Sev. | Finding (short) | Disposition | Where fixed / evidence |
|---|---|---|---|---|
| 1 | BLOCKER | Id-only sealed reports still expose expected/retrieved sections, ranks, per-row outcomes | ACCEPTED | Sealed-output rule (header); item 1 serialiser (sealed = aggregates only, for every set in a sealed run); 16A-1 (b2). Verified at `evaluator.py:2546–2549` |
| 2 | BLOCKER | Leak scan misses partial, truncated, reformatted and field-specific leaks; one shared sentinel | ACCEPTED | Item 1: allowlisted serialiser primary; `leak_scan` on 8-token windows + sealed gap keywords and evidence groups, after unescaping; opaque ids (item 2); containment by resolved path; 16A-1 (b) distinct per-field sentinels, truncation, escaping, symlink. Verified at `w_sweep.py:84`, `:150`, `:196` |
| 3 | BLOCKER | Sealed class can be shed by copying, renaming, reordering, subsetting or deriving | ACCEPTED | Item 1: row index + classification by content; derived artifacts inherit via `source_commitments`; retired content can't re-seal; registrations bound to content sha256; 16A-1 (c) |
| 4 | MAJOR | Slot check-then-append races; crashes don't spend slots; draft pre-reg only blocks verdicts | ACCEPTED | Item 4: signed pre-reg required before any sealed retrieval/model call; atomic `O_EXCL` reservation first; failed/interrupted attempts consume; trusted-processor list (item 1); 16A-1 (i) |
| 5 | BLOCKER | Negative path commits private development data; path not defined in `harness.md` | ACCEPTED | Stop paths: tooling + non-private metadata only, no merge/tag unless approved criteria pass or owner revises; path defined inline and added to `harness.md` (item 8). Confirmed `harness.md:88` lists it as a TODO |
| 6 | MAJOR | Split has no sealed-eligibility constraint; migrated pairs straddle splits; siblings of dev scenarios sealable | ACCEPTED | Item 4 pool (frozen eligibility, `dev_sibling`), order-invariant split, phrasings follow family, strata and family-level band defined, infeasible quotas fail; 16A-2 steps 1–3; (h), 16A-2 (b) |
| 7 | MAJOR | Singleton Durkalski ≠ exact McNemar; exact power not monotone in n | ACCEPTED | Recomputed: exact p = 0.0385742, singleton Durkalski p = 0.0209213; power 0.2097280 (7) → 0.1677747 (8). Item 5 text; (j) reference values incl. unequal-cluster and zero-variance cases |
| 8 | MAJOR | Power/MDE needs discordance and target power; refusal families lack retrieval evidence | ACCEPTED | Recomputed: N = 50, effect 0.10 → 0.241189 (d = 0.2) vs 0.094453 (d = 0.8). Item 5 eligible populations, power inputs, Wilson `unavailable`; pre-reg fields; Tier-2 MDE expectation |
| 9 | MAJOR | Bounded K alone doesn't make Holm valid under adaptive candidate choice | ACCEPTED | Item 5 candidate rule (development-only selection, per-slot declaration before reservation, new slot within K, beyond K exploratory) and verdict rule (K retained, unused/failed p = 1, interim K·p, MWE gates the point estimate) |
| 10 | MAJOR | "Cached expansions" has no executable contract; expansion-only draws can't give retrieval flips | ACCEPTED | Item 5 frozen expansion artifact (keyed, bound, injected, replay-only, separate counters) and variance draws rerunning retrieval; (k2). Verified at `evaluator.py:1711–1724` |
| 11 | MAJOR | C4 can pass with missing outcome rows or vacuous critical controls | ACCEPTED | Item 6: exact unique id coverage, required fields, non-empty resolving incumbent-HIT controls, bindings; (k). Verified: `bakeoff_report.py:786` skips unmatched rows |
| 12 | MAJOR | PARTIAL ignores `gate_outcome`, can credit terminal drafts; observed scope undefined | ACCEPTED | Item 3 `observed_scope` (5 classes) and `score_partial` (H exclusions first, displayed gate outcomes only, `is_refusal`); schema keyword validation; (f). Verified `generator.py:427` before `:433` |
| 13 | MAJOR | Section-level absorbed map credits sub-chunks lacking the absorbed passage | ACCEPTED | Item 7: map keyed by chunk id, credit only where the chunk's own span covers the absorbed span, no re-index (orchestrator decision); (l). Verified `chunker.py:491–504` |
| 14 | MAJOR | v1 byte-compatibility contradicts privacy rules; timestamps and provenance non-deterministic; (j) over-scoped | ACCEPTED | P0 (injected clock, paths, provenance; SHA outside `reports`); item 3 v5 compatibility limited to registered public sets; headline from registry role; (d) per-guard tests and retrieval-ablation rows only. Verified `evaluator.py:1326`, `:2210`, `:2043`, `:2341` |
| 15 | MAJOR | Cost approval precedes the estimate; +20% could cross the cap; re-epoch/re-seal lack owner stops | ACCEPTED | 16A-2 step 5 (estimate before OS-2), step 10 (pre-reservation check, spend meter), (h); D64 triggers listed in the header; `--new-epoch`, re-sealing and amendments are hard stops; deferred canonical leaves 16A-2 incomplete |
| 16 | MINOR | Evidence pointers conflate configuration, serialisation and matching sites | ACCEPTED | Evidence block: `:200` vs `:205–210`; dump configured `pipeline.py:642`, written `evaluator.py:2171–2173`; roster `:544` vs role coverage `:603` |

No finding was rebutted. One consequence is flagged for the orchestrator, not changed here: 003's
"≥50 sealed families" yields only about 35–37 retrieval-eligible families, so the achievable MDE at
80% power is about 26–34 points (Tier-2). Raising the sealed target would cost more owner validation
time at OS-2.

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
