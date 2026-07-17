# Design decisions log

> One short entry per meaningful choice: what we decided, why, what we rejected.
> Append-only. If a decision is reversed, add a new entry — don't edit history.
> This file goes in `docs/decisions.md`.

**Current phase: 14 — answer quality: synthesis + intent-level rewriting**

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

## D10 — Extraction QA findings: go/no-go verdict GO (3 Jul 2026)
**Finding (Phase 0 acceptance evidence):** pdfplumber's raw extraction of
`data/Conveyancing_Handbook.pdf` (805 pages) is clean enough to build on with
no OCR garbling observed across 10 randomly sampled pages plus full-corpus
pattern checks: sentences are well-formed, no visible character-level OCR
errors, and no re-OCR pass is warranted. It needs three kinds of cleaning
before chunking. (1) **Header pollution** is real but not literal-repeat:
707/805 pages (88%) open with a running header shaped either
`<ALL-CAPS CHAPTER TITLE> <printed page no.>` or
`<printed page no.> <ALL-CAPS CHAPTER TITLE>` (recto/verso alternation), and
the title text changes every chapter — so `extraction_qa.py`'s repeated-line
report (frequency-based, >10% threshold) found *zero* hits even though
header pollution is on the large majority of pages. Header stripping must be
a **positional regex rule** (first line of page matching the caps+number
shape), not a literal-string-repetition rule. (2) **Hyphenation breaks are
frequent**: 2,682 lines across the corpus end in a lowercase letter followed
by a hyphen, confirming the cross-line hyphen repair CLAUDE.md anticipated.
(3) **Front-matter page-number offset**: the printed page number embedded in
the header differs from pdfplumber's raw page index by a constant +44
throughout the body (e.g. raw page 107 ↔ printed "63"; raw page 762 ↔
printed "718") — front matter (title pages, table of contents, table of
statutes, table of cases, all in roman numerals) accounts for the gap.
Phase 1 must decide whether `page_start`/`page_end` citations surface the
raw PDF page or the book's own printed page number; not resolved here.

**Numbering grammar — invalidates a stated assumption:** the handbook has
**no in-body `Chapter N` (mixed-case) marker** as IMPLEMENTATION_PLAN.md
Phase 2 assumed. The real chapter-start marker is `^CHAPTER \d+$`
(**all-caps**, standalone line, chapter title on the following line) —
confirmed to appear exactly once per chapter, 16 times total, matching the
table of contents. The mixed-case `^Chapter \d+` pattern census'd in this
gate matched only 5 lines, all mid-prose false positives (e.g. "Chapter 13
dealt with the difference between..."), zero real chapter starts — a
case-sensitivity trap in the same family as the PART false-positive (D3),
just inverted: here the fix is matching the caps, not avoiding
over-matching. Decimal paragraph numbering goes **four levels deep**
(`N`, `N.N`, `N.N.N`, `N.N.N.N` — e.g. `1.7.2.1 Formalities for
registration`), deeper than the 3-level `3.2 → 3.2.1` example in CLAUDE.md.
Depth-1 bare numbers (2,035 occurrences) are overwhelmingly noise — years,
street addresses, enumerated-list items ("1. Actual where...", lettered
sub-items), table-of-contents page numbers — and are not usable alone as a
structural signal. Depth-2-and-deeper numbers reliably mark real
section/subsection headings, and the chapter number is recoverable as the
leading integer of any such number (e.g. "9.4 Content of the Mortgage Deed"
belongs to Chapter 9) — so Phase 2 does not need a separate chapter-number
extraction step once section numbers are parsed, only a chapter-*title*
lookup (from the table of contents or the `CHAPTER N` marker pages).

**Verdict:** GO. Proceed to Phase 1 on pdfplumber's existing text layer; no
OCRmyPDF pivot needed. Phase 2's chunker patterns must use `^CHAPTER \d+$`
(all-caps) instead of the mixed-case pattern named in the current plan text.

## D11 — Extraction tooling: pdfplumber retained over Marker/Docling (3 Jul 2026)
**Decision:** keep pdfplumber (extends D1); do not adopt Marker, Docling, or
similar deep-learning PDF-to-Markdown converters, conditional on the Phase 0
extraction QA gate passing (it did — see D10).
**Why:** the corpus already carries a usable OCR text layer, so extraction
means *reading* that layer, not re-OCRing it. pdfplumber does this
deterministically, with zero new dependencies, and gives the per-page
character offsets the page_map/citation design (D2) needs. Marker is a
deep-learning layout+OCR pipeline (Surya models, torch + downloaded weights)
whose throughput is heavily GPU-dependent — real-world reports range from
~0.03 pages/sec on a consumer GPU to ~25 pages/sec on an H100 — the wrong
cost/risk profile for an 800-page book on a laptop a week before freeze. It
would also discard the existing text layer in favour of new model output:
a new, non-deterministic source of error, not a fix for one that was
demonstrated.
**Rejected:** Marker / Docling (GPU-dependent throughput, new dependency
weight, replaces a working text layer with model output); OCRmyPDF
(deferred — only relevant if the gate had failed, which it didn't).
**Consequence:** if a future corpus fails its own extraction QA gate, the
escalation ladder is OCRmyPDF first (re-OCR in place, deterministic, CPU-only),
Marker/Docling only if layout structure itself — not text fidelity — is the
problem.

## D12 — requirements.txt pinned to exact resolved versions, not full freeze (3 Jul 2026)
**Decision:** pin the 13 direct dependencies to the exact versions resolved
in a clean Python 3.12.13 venv (`==`), rather than committing the full
`pip freeze` transitive closure IMPLEMENTATION_PLAN.md's Phase 0 literally
specifies.
**Why:** a full freeze on macOS-arm64 embeds platform-specific transitive
pins (torch wheels, etc.) that can fail to resolve for a reviewer cloning on
a different OS/architecture — directly hostile to the "fresh-clone
quickstart in ≤5 commands" goal in Phase 6. Pinning only the direct
dependencies still gives fully reproducible, evidenced builds (39/39 tests
passed against these exact pins, including langchain's 0.x → 1.3.11 major
version jump pulled in by the `>=0.3.0` range) without baking in one
machine's platform wheels.
**Rejected:** literal `pip freeze` (platform-fragile transitive pins);
leaving `>=` ranges as-is (the thing Phase 0 explicitly exists to fix —
non-reproducible builds).
**Consequence:** if a reviewer's fresh-clone install (Phase 6) hits a
transitive dependency conflict pip's own resolver can't solve, revisit and
pin the offending transitive package explicitly rather than freezing
everything.

---

## D13 — Page identity: page map carries raw + printed number; citations use printed (3 Jul 2026)
**Decision:** `extract_pdf` returns `(clean_text, page_map)` where each
`PageSpan` records `page_number` (raw 1-indexed pdfplumber page), `printed_page`
(the book's own number), and the `[char_start, char_end)` slice of the page in
`clean_text`. `printed_page` is parsed from the running header, or — for
headerless body pages — inferred as `raw − modal_offset`; `None` for front
matter. Phase 2 chunk `page_start`/`page_end` metadata will carry the **printed**
page; the raw index stays in the map for QA/debugging.
**Why:** the printed number is what a practitioner sees on the page and how the
book's own TOC/index cite — `[Handbook, para 3.2.1, p.87]` must read as a book
citation. Keeping the raw index too costs one field and is the corpus-agnostic
ground truth (opens the PDF at that page). The inference is essential: ~12% of
body pages are headerless (chapter openers), and every chapter's first chunk
starts there — without it those chunks would carry a null page.
**Rejected:** raw-only (not how the book is cited); printed-only (nullable,
corpus-specific); hardcoding the −44 offset D10 measured (corpus-specific — the
modal offset is self-calibrated from the headers, so "bring your own manual"
survives).
**Consequence:** the final citation display string is a Phase 4 decision that
consumes this printed page; `page_range(page_map, start, end)` (bisect on
`char_start`) resolves a text offset to its page(s).

## D14 — Positional header stripping: shape + CHAPTER exclusion + modal-offset validation (3 Jul 2026)
**Decision:** strip a running header only from the **first non-empty line** of a
page, matching one of two all-caps shapes (`TITLE 87` / `88 TITLE`), with the
page number constrained to 1–3 digits, and only when the candidate's
`raw − printed` offset is within ±1 of the corpus's modal offset. `^CHAPTER \d+$`
is excluded from header matching before any shape test. Bare page-number lines
(`^\d{1,3}$`) are stripped only as the first or last non-empty line.
**Why:** D10 proved frequency-based detection blind (the header title changes
every chapter, so no line repeats). Three guards make positional stripping safe:
(1) **CHAPTER exclusion** — `CHAPTER 3` matches the recto shape exactly, and
chapter openers are the headerless pages, so a naive rule would silently delete
all 16 chapter markers and only Phase 2 would notice (verified live: 16 markers
survive cleaning); (2) **1–3 digit** page numbers reject 4-digit years, so an
all-caps statute title like `SUCCESSION ACT 1965` first-on-page survives;
(3) **modal-offset validation** (self-calibrated, ~707 votes for +44) means a
stray all-caps-plus-small-number content line would have to arithmetically equal
`raw − 44` to be stripped. Live result: 729/805 first-line header shapes → 5/739
after cleaning.
**Rejected:** frequency/repeated-line detection (D10 showed it finds nothing);
IGNORECASE or mixed-case `Chapter` matching (the D3 case-sensitivity lesson);
unconditional `^\d+$` stripping (D10 found 2,035 mid-text bare numbers — years,
addresses, list items — that must survive).
**Consequence:** cleaning now runs on all PDFs (incl. `--type legislation` via
`load_directory`); the validated rule makes false positives on other corpora
negligible. Known, documented residue: front-matter headers with **roman-numeral**
page numbers (`TABLE OF STATUTES xxvii`) don't match the arabic rule and survive —
harmless, front matter yields no cited chunks. Header-shape lines appearing as a
page *footer* (rare, appendix forms) are not stripped by the first-line rule.

## D15 — Hyphenation repair: corpus-attestation join (3 Jul 2026)
**Decision:** rejoin a word broken by a hyphen at a line end (lowercase before
the hyphen, next line starts lowercase), within a page and across page seams.
Keep the hyphen when the reconstructed `a-b` is attested as a hyphenated word
elsewhere in the corpus (`co-ownership`); otherwise fuse (`regis-`/`tration` →
`registration`). Attestation is built once from all unbroken (intra-line) tokens.
**Why:** a plain always-fuse policy would corrupt genuine compounds
(`co-ownership` → `coownership`), which breaks Phase 3 BM25 exact-token retrieval
on real conveyancing terms. Attestation decides empirically from the book's own
usage, with no new dependency and deterministically. Live result: 2,682 raw
hyphen-break lines → 14 residual after cleaning (the residue is where the next
line did not start lowercase — correctly left alone).
**Rejected:** always-fuse (corrupts compounds); a fixed prefix list (`re-` is
wrong in both directions: `re-entry` vs `regis-tration`); dictionary lookup
(new dependency). Untouched by design: hyphen before uppercase/digit
(`1976-\n1977`).
**Consequence:** a compound repaired across a page break straddles two pages, so
`page_range` correctly returns distinct start/end pages for it.

## D16 — Ingestion seam, whitespace, and page-join policy (3 Jul 2026)
**Decision:** new public `extract_pdf(path) -> (clean_text, page_map)` is the
cleaning contract; `load_pdf` keeps its exact `List[Document]` signature and
metadata, now wrapping `extract_pdf` and dropping the map at that seam.
Whitespace normalisation is minimal — rstrip lines, collapse 3+ newlines to 2,
strip page-edge blank lines, **never unwrap line breaks into spaces**. Pages
join with a single `\n`. Offsets are recorded in a pure final concatenation pass
(clean per page → decide each boundary's separator/hyphen-trim → concatenate),
so `clean_text[char_start:char_end]` is exact by construction.
**Why:** the wrapper means today's `pipeline index` gets cleaned text with zero
changes to `pipeline.py`/`chunker.py` (all 39 existing tests pass unmodified),
while Phase 2 rewires the chunker to consume the map — the map must never enter
`Document.metadata` because the chunker copies metadata into every chunk and
Chroma rejects non-scalar values. Minimal whitespace preserves the line-start
anchors (`^CHAPTER \d+$`, `^\d+\.\d+`) that Phase 2 and BM25 depend on. A single
`\n` join matches typography (a page break is a line break); `\n\n` would
fabricate a paragraph break mid-sentence at ~800 boundaries and poison the
fallback splitter (whose first separator is `\n\n`). Recording offsets only in a
pure pass avoids the classic bug of mutating already-offset text.
**Rejected:** rewiring `index_documents`/`chunk_legal_document` now (Phase 2
scope); aggressive whitespace collap/unwrapping (destroys structural anchors);
`\n\n` page joins; repairing hyphens after concatenation (offset drift).
**Consequence:** a page that is only furniture (header-only) becomes empty and is
dropped; raw page numbering is preserved (gaps, not renumbering). Verified: page
map invariant PASS on all 739 cleaned pages of the real corpus.

## D17 — OCR-fidelity verdict: prose fully OCR'd; garble is layout, not characters (3 Jul 2026)
**Finding (answers a reviewer concern that copy-pasted citations looked
garbled):** the corpus text layer is clean at the character level — across all
805 text pages, **0 `(cid:N)` unmapped-glyph markers, 0 U+FFFD replacement
characters**, and of 16 distinct non-ASCII characters all are legitimate
(curly quotes, en/em dashes, `§`, `€`, `£`, `•`, accented names — `Éireann`,
`précis`, `Dáil`, `vis-à-vis`); zero mojibake. Body prose sampled across the 16
chapters is pristine (well-formed sentences, correct section refs, clean
hyphen breaks). The garble a reader sees on copy-paste has two **layout**
sources, not OCR corruption:
1. **Front-matter tables** (Table of Cases/Statutes, roman-numeral pages) are
   two-column; `extract_text` linearises across the full page width, splicing
   left-column and right-column citations together (`EBS Ltd v Kenehan [2017]
   IEHC 604 . . . 275 Kelly & anor v Irish Bank Resolution`). Characters are
   correct; only reading order is jumbled. These pages precede `CHAPTER 1` and
   produce no cited body chunks.
2. **Appendix form facsimiles** (Conditions of Sale, Certificate of Title,
   Requisitions on Title, Conveyancing & Taxation) — ~66–80 pages whose body is
   vector-drawn/not in the text layer; `extract_text` returns only the running
   header. `text_layer_coverage_report` now flags these as *sparse* pages
   (<100 chars). None carry a bitmap image with missing OCR (the un-OCR'd-image
   count is 0), so OCRmyPDF would not recover them — the form text simply isn't
   present as extractable content.
**Decision:** GO on pdfplumber's existing text layer for the body (no OCR
pivot). Phase 1 cleaning correctly **drops** the header-only appendix pages
(D16) rather than emitting header-noise chunks. Front-matter table garble is
accepted as a documented limitation for v1.
**Rejected:** OCRmyPDF re-processing (the D11 ladder's first rung) — warranted
only for missing/garbled text, and the prose has neither; column-aware
extraction of the front-matter tables (real churn for reference material outside
the QA scope); trying to recover the appendix forms (their content is not in the
text layer at all).
**Consequence:** Phase 2 should decide whether to **exclude pre-`CHAPTER 1`
front matter** from chunking (its garbled tables would otherwise become
low-value, section-number-less chunks). If the appendix forms' content is ever
needed, those specific page ranges require a separate OCR pass — out of scope
for the procedure-QA v1.

## D18 — Opener/bounds fix: printed-page inference window (4 Jul 2026)
**Decision:** the headerless-page printed-number inference (D13) now fires only for
raw pages inside `[infer_lower, infer_upper]` and only when the inferred number is
`≥ 1`, where `infer_lower = min(first validated-header raw, first raw page whose
first non-empty line is ^CHAPTER N$)` and `infer_upper = last validated-header raw
+ 1`. This replaces the single lower gate `raw ≥ first_body_raw`
(`_compute_header_offset` → `_compute_inference_bounds`).
**Why:** every chapter opener is headerless and sits one raw page *before* the
chapter's first running header, so the old gate mis-classified Chapter 1's opener
(printed page 1) as front matter — a null page on the first chunk of the book.
Anchoring the lower bound to the `^CHAPTER N$` marker rescues it. The `+1` upper
bound covers at most one trailing headerless page (insurance for corpora whose body
does not run headers to the final text page — a no-op here). The `≥ 1` guard refuses
to fabricate page 0 or a negative number when the offset would underflow. Live
result on the real corpus: **0 of 1,470 chunks carry a null `page_start`**; all 16
chapter openers cite their true printed page; the modal offset is still self-calibrated
(never hardcoded), preserving "bring your own manual."
**Rejected:** hardcoding the front-matter page count (corpus-specific); inferring for
every page past the first marker with no upper bound (would number trailing back
matter); dropping the `≥ 1` guard (fabricates page 0).
**Consequence:** on this corpus validated headers reach the last text page, so
`infer_upper` is never exercised, but the window makes the rule safe for other
manuals. 38/38 ingest tests hold, incl. three new bounds tests.

## D19 — Handbook segment grammar (4 Jul 2026)
**Decision:** `chunk_handbook(clean_text, page_map, metadata, chunk_size=600,
chunk_overlap=120)` segments the cleaned text at structural boundaries tracked as
**character offsets into `clean_text`** (never string splits), so `page_range` stays
exact. Chapters: `^CHAPTER (\d{1,2})$` (MULTILINE, case-sensitive). `chapter_title`
= the consecutive **ALL-CAPS** non-empty lines after the marker, stopping at the
first blank line, heading/appendix, **mixed-case line**, or a 3-line cap. Headings:
`^(\d{1,2}(?:\.\d{1,3}){1,3})[^\S\n]+(\S.*)$` (the number and title must share one
physical line) with four guards — (i) leading integer == current chapter, (ii) title
opens with capital/digit/quote or `e[A-Z]`, (iii) no leading-zero number components,
(iv) no `\.{3,}` dot-leaders. Appendices: `^APPENDIX (\d{1,2}\.\d{1,3})\b`. Body ends
at the first `^INDEX$` after the last marker; front matter (before the first marker)
is excluded. Zero markers → loud `ValueError` naming `--type`.
**Why:** these are the real markers D10 measured. The guards encode the exact traps
found probing the corpus: cross-chapter references (i), wrapped-prose false starts
(ii, with `e[A-Z]` saving the one real casualty `14.18 eRegistration`), quoted Law
Society numbering `3.01/3.02` (iii), and dot-leaders (iv). The ALL-CAPS title rule
(added after an adversarial review) stops the mixed-case epigraphs that open chapters
3 and 5 from being folded into the chapter title and polluting every chunk's citation
prefix (they stay in the chapter body instead). The horizontal-whitespace heading
separator keeps the number and title on one line, matching the single-line
`_is_heading_line` matcher and refusing to scavenge a following line onto a bare
cross-reference number. Live result: 16/16 markers, 0 sequence violations, 1,293
headings accepted, guard rejects (i)26 (ii)5 (iii)2 (iv)0 — matching the probe; 104,550
front-matter bytes and 117,835 index-tail bytes excluded (without the INDEX cut,
section 16.19 would have absorbed the entire 118 KB index into ~90 poisoned chunks).
**Rejected:** IGNORECASE / mixed-case `Chapter` (the D3 case lesson); depth-1 bare
numbers as boundaries (D10: 2,035 noise hits); "title = single line after marker"
(breaks the two 2-line titles); `\s+` heading separator (spans newlines); no INDEX
boundary (poisons citations); silent fallthrough on a mis-routed `--type`.
**Consequence:** the legislation strategy (`chunk_legal_document`) is untouched, routed
by `document_type`. **Known limitation, deferred:** `_find_body_end` takes the *first*
`^INDEX$` after the last chapter marker; a stray standalone `^INDEX$` inside the final
chapter's body, or a false `^CHAPTER N$` line inside the garbled two-column index,
would mis-bound the body. Neither occurs on this corpus (16 clean markers, a single
INDEX line), so it is recorded for a future "bring your own manual" hardening pass
rather than fixed in this phase.

## D20 — Runt & oversize policy (4 Jul 2026)
**Decision:** three reconciliation passes over the segments. (1) Title-only `APPENDIX`
stubs (<50 chars) merge **backward** into the preceding same-chapter segment (their
form facsimiles are not in the text layer — D17). (2) Runts (<600 chars) merge
**forward**: a furniture chapter intro absorbs its first section and *adopts that
section's identity* (the chapter title already lives in the prefix); any other runt
merges forward only into a *descendant* (`next.startswith(id + ".")`), keeping its own
(parent) identity; a runt followed by a sibling stays standalone; merging never crosses
a chapter seam. (3) A runt that is the *last* segment of its chapter merges **backward**
to a fixed point. Oversize segments (>4,000 chars) are re-split to `chunk_size` tokens
(2,400/480 chars) with the fallback splitter; each sub-chunk's page range is recovered
by locating it in `clean_text` (verbatim substrings) with a stride-advancing cursor,
inheriting the parent's range on a find-miss; the prefix is prepended **after** the split.
**Why:** aligns chunk boundaries with the author's meaning boundaries (D4) while
refusing to sacrifice a correct citation to a size floor — sibling runts keep their own
paragraph number rather than being glued to a neighbour. Descendant-merge keeps a short
parent heading (e.g. `3.2`) with the content that actually lives under it (`3.2.1`). The
stride cursor stops `str.find` from re-locking onto an earlier occurrence of repeated
phrasing. Live result: 1,470 chunks, body chars min 40 / median 1,694 / max 3,981 (0
remain >4,000), 207 runts deliberately kept standalone, 61 appendix-cited chunks.
**Rejected:** merging sibling runts (loses citations); forward-merging across a chapter
seam (wrong chapter); one split size for both the 4,000 trigger and the 2,400 target
(would needlessly re-split 2,400–4,000-char chunks, violating D4's band); a `+1` cursor
advance (re-locks onto duplicates in repetitive text).
**Consequence:** ~14% of chunks are sub-600 runts by design; chunk size is a Phase 5
tunable.

## D21 — Chunk metadata & contextual prefix (4 Jul 2026)
**Decision:** each chunk carries `chapter_number:int, chapter_title, section_number,
heading, page_start:int, page_end:int` plus `source, title, document_type, date` — all
scalar. The prefix prepended to `page_content`:
`[Conveyancing Handbook, Ch.3 Ethics, para 3.2.1, p.87] ` — document title = filename
stem (underscores→spaces); chapter title `string.capwords`-cased from ALL-CAPS (keeps
`Vendor's`); `para` omitted for a chapter intro; an `APPENDIX 6.1` section rendered
verbatim (no `para`); `pp.X–Y` when the chunk spans pages; the page component omitted
when the printed page is None.
**Why:** the printed page (via `page_range`, D13) is what a practitioner cites, and the
prefix front-loads chapter/para/page into the embedded text so the citation survives
MiniLM truncation (D23) and feeds BM25 (Phase 3). Live result: 95.8% of chunks carry a
dotted depth-≥2 section number; the biased 10-chunk spot-check (merged-runt, oversize
sub-chunk, chapter-intro, random) located a mid-chunk sentence on its cited printed
page(s) **10/10**, mapping printed→raw through the freshly extracted page map (never
`+44` arithmetic).
**Rejected:** raw page numbers in the citation (not how the book is cited); `str.title()`
(corrupts `Vendor'S`); prepending the prefix before oversize splitting (sub-chunks would
inherit the wrong page).
**Consequence:** the final display citation string is a Phase 4 decision; None-page keys
are dropped before Chroma (D22).

## D22 — Wiring: loader, routing, --reset, metadata sanitizer (4 Jul 2026)
**Decision:** new `load_handbook_pdf(path) -> (clean_text, page_map, metadata)` mirrors
`load_pdf`'s metadata block and graceful error semantics (missing file → logged,
`("", [], {})`, no traceback). `index_documents` routes `document_type == "handbook"`
to `load_handbook_pdf` + `chunk_handbook`, and **requires the source to be a `.pdf`**
(a directory/URL under this type raises, so legislation is never silently tagged
`handbook`); every other route is untouched. `"handbook"` is added to both `--type`
choice lists; the index default flips to `"handbook"`; epilog examples updated. New
`index --reset` flag calls `clear_store()` — but **only after** the chunks are in hand,
so a mis-routed `--reset` that trips `chunk_handbook`'s `ValueError` cannot destroy the
existing index and then crash. `add_documents` gains a metadata sanitizer that drops
None-valued keys with one aggregated warning.
**Why:** the handbook is the corpus, so it is the default; the page-aware route is the
only one that consumes the map. `--reset` neutralises the positional-ID dedup trap
(embedder.py) during this session's iterate loop — re-indexing after a chunker change
would otherwise silently no-op; content-hash IDs are Phase 3. Validating and chunking
before clearing, and requiring a PDF for `--type handbook`, close two footguns an
adversarial review surfaced (destructive `--reset`, silent type mis-tagging). The
sanitizer exists because Chroma rejects None values, which handbook chunks legitimately
carry for `page_start`/`page_end` when a printed page is unknown; one aggregated warning
avoids per-chunk noise. Live result: `index … --type handbook --reset` indexed 1,470
chunks; 0 None-pages on this corpus so the sanitizer was a no-op here, but it is proven
by unit test.
**Rejected:** letting the page map enter `Document.metadata` (Chroma rejects non-scalars
— D16); a per-chunk None warning (noise); clearing the store before validation
(destructive); content-hash IDs now (Phase 3 scope).
**Consequence:** `chroma_db/` (~31 MB) is CWD-relative and gitignored; re-index with
`--reset` until Phase 3 lands content-hash IDs.

## D23 — MiniLM truncation note + honest acceptance metrics (4 Jul 2026)
**Decision:** record that `all-MiniLM-L6-v2` truncates at 256 wordpiece tokens
(~1,000 chars), so a 2,400-char chunk embeds only its head — the citation prefix is
front-loaded so it always survives, and BM25 (Phase 3) sees the full text. Replace the
vacuous "≥90% non-empty `section_number`" gate (satisfied by construction) with measured
acceptance evidence: 2,510,174 clean chars / 739 pages; chapter markers 16/16, 0 sequence
violations; guard rejects (i)26 (ii)5 (iii)2 (iv)0; trailing-dot heading-shaped lines
censused = 7; front-matter 104,550 B and index-tail 117,835 B excluded; 1,470 chunks,
95.8% dotted depth-≥2 section number, 0 null pages, 0 oversize remaining, longest
`chapter_title` 58 chars; biased spot-check **10/10** sentences located on their cited
printed pages.
**Why:** the old gate proved nothing (empty `section_number` was impossible by
construction); these numbers are falsifiable and match the read-only corpus probe. The
truncation note pre-registers a Phase 5 tuning suspect.
**Rejected:** the by-construction gate; hiding the truncation limit.
**Consequence:** chunk-size tuning in Phase 5 will revisit the 2,400-char oversize target
against the 256-token embedding window.

---

## D24 — Phase 3 implementation: BM25 + RRF hybrid retrieval (5 Jul 2026)
**Decision:** implement the hybrid retrieval D6 already committed to. New `src/bm25_index.py`
(`rank-bm25==0.2.2`, resolved and pinned per D12's methodology) builds a `BM25Okapi` index over
the vector store's full contents, pickled beside `chroma_db/` as `bm25_index.pkl`. `retrieve()`
pulls `max(12, top_k)` candidates from each of the vector and BM25 arms, fuses by reciprocal
rank fusion (`score = Σ 1/(60 + rank)`), and returns the top-k fused results. Each arm is wrapped
in its own try/except (a failure in one doesn't block the other), and retrieval falls back to
vector-only, with a logged warning, when no BM25 sidecar exists yet (an index predating this
phase). `search_bm25` excludes non-positive-scoring documents from its candidates entirely,
rather than returning them as tied-zero "matches" — BM25 IDF goes non-positive once a term
appears in at least half the corpus (true of common words at the real corpus's ~1,470-chunk
scale), so including them would misrepresent absence of lexical evidence as a ranked signal and
dilute the fused score of a genuine exact match.
**Why:** legal queries are exact-token-heavy (section numbers, form names, statute titles); pure
semantic search fuzzes past the literal token that matters (D6). Fusing on rank rather than raw
score sidesteps the two arms having incomparable scales — Chroma's own relevance scores are not
reliably normalised to [0, 1] under the project's embedding setup (observed directly: the test
suite logs a `UserWarning` with scores like −45, on both the pre-Phase-3 code and this phase's,
so this was a latent property of the retrieval stack, not a regression).
**Rejected:** a single hard document_type filter applied only to the vector arm (would let
mismatched-type chunks leak in via BM25); tied-zero BM25 "matches" counted as ranked (dilutes
RRF for queries with no real lexical signal in most of the corpus); rebuilding BM25 incrementally
per-add (rank_bm25 has no such API; correctness from a full, from-scratch rebuild against the
store's own authoritative contents is simpler and cannot drift).
**Consequence:** `tests/test_bm25_index.py` (6 tests: exact-token ranking, document_type
filtering, empty-filter-match, top_k truncation, pickle round-trip, missing-file → None) plus
`tests/test_retriever.py` additions (RRF fusion math on synthetic rankings, an exact-token test
with 1 target + 9 vocabulary-disjoint decoys under `FakeEmbeddings` — the target ranks top-3 via
BM25's contribution alone, independent of FakeEmbeddings' hash-based, semantically meaningless
vector similarity — and a vector-only-fallback test with no BM25 sidecar present). Real-corpus
acceptance (5 exact-token queries against the actual handbook index, each hitting top-3) is
pending corpus arrival — tracked as open, not yet run.

## D25 — Phase 3 implementation: content-hash IDs + embedding-model manifest (5 Jul 2026)
**Decision:** implement D7 (content-hash chunk IDs) and the recording/assertion half of D5.
`embedder.compute_chunk_id` = `sha256(text)[:16]`, replacing `f"{source}::chunk_{i}"` in
`add_documents`. The private `vector_store._collection.get(ids=...)` dedup check is replaced
with the public `vector_store.get(ids=...)` (confirmed present on `langchain_chroma.Chroma` by
direct introspection of the pinned 1.1.0 install). The embedding model name is recorded two
ways: best-effort via `collection_metadata` at `Chroma` construction (human-discoverable by
inspecting the raw Chroma DB, but confirmed by direct testing *not* to update on reopen with a
different value, and with no public getter back — so not load-bearing), and authoritatively via
an `embedding_model.txt` sidecar written by `add_documents` and checked by
`assert_embedding_model()`, which `retrieve()` calls before querying: raises `ValueError` on a
real mismatch, warns and no-ops if the manifest doesn't exist yet (an index predating this
phase, or nothing indexed yet).
**Why:** D7's own rationale — positional IDs collide with different content the moment chunking
changes. D5 needs corpus and queries to share one coordinate system by construction; a plain
`collection_metadata` write is not sufficient on its own because it doesn't survive being reopened
with a changed value, and reading it back has no public API — an explicit sidecar manifest is
simpler, testable without spinning up real Chroma internals, and consistent with how BM25 is
already persisted as a sidecar next to `chroma_db/`.
**Rejected:** reading collection metadata back via the private `_collection.metadata` (works, but
is exactly the kind of implementation-detail dependency D7 already argued against for the dedup
path); a full freeze on `collection_metadata` alone (silently wrong once contradicted by a
reopen, per the tested behaviour above).
**Bug found and fixed by its own test:** a batch containing two documents with *identical* text
hashes to the same ID twice within one `add_documents` call — Chroma's `get()`/`add_documents()`
reject duplicate IDs outright rather than deduping, raising `DuplicateIDError`. Fixed by deduping
within the incoming batch (keep first occurrence) before touching the store at all.
**Consequence:** `tests/test_embedder.py` gains `TestComputeChunkId` (3), `TestContentHashDedup`
(1 — proves two chunks with identical text but different `source` metadata now correctly dedupe,
which positional IDs would have missed), `TestEmbeddingModelManifest` (4), and
`TestBM25IndexSideEffect` (2). Existing fixtures that pass an explicit `vector_store` now also
pass a matching `persist_directory` — BM25/manifest sidecars are written relative to that
parameter regardless of the `vector_store` object's own (unrecoverable, no public accessor)
directory. Full suite: **124 passed, 0 failed** (was 102 before Phase 3; +22 new tests across
`test_bm25_index.py`, `test_embedder.py`, `test_retriever.py`).

## D26 — Phase 3 local validation + robustness fixes from review (6 Jul 2026)
**Decision:** validate PR #5 against the real corpus locally and land three fixes its review
surfaced. (1) `load_bm25_index` now treats an *unreadable* pickle (truncated write, dependency
version skew) the same as a missing one — warn and return `None`, so retrieval degrades to
vector-only instead of crashing the query; `save_bm25_index` writes via temp file + `os.replace`
so an interrupted write can never leave a truncated pickle at the real path. (2) `add_documents`
raises `ValueError` when given an explicit `vector_store` without a `persist_directory` — the
BM25/manifest sidecars are written relative to that parameter, and defaulting it would silently
put them beside the default store while the vectors live elsewhere, permanently desyncing hybrid
retrieval. (The guard closes the *omission* case only; a deliberately wrong pairing is
undetectable because langchain-chroma exposes no accessor for a store's own directory.)
(3) `DEFAULT_TOP_K = 6` shared constant in `retriever.py`, imported by the CLI — was `5` in
three places, and the plan (line 67) specifies default 6; a shared constant follows the module's
own `RRF_K`/`CANDIDATE_POOL` pattern and prevents silent drift.
**Why:** the corrupt-pickle crash contradicted D24's advertised per-arm degradation — reproduced
live: a truncated `bm25_index.pkl` raised `UnpicklingError` out of `retrieve()` before the fix,
and degrades with a warning after it.
**Rejected:** catching pickle errors in the retriever's BM25 arm instead (wrong altitude —
`load`'s contract is already `Optional`; fixing it there serves every caller); deriving
`persist_directory` from the store object (no public API).
**Validation evidence (real corpus, fresh Python 3.12 venv):** full suite 129 passed.
`index --reset` → 1,470 chunks created by the chunker = 1,470 stored IDs — **zero
identical-text collapses**, so D25's text-only hashing cost no citations on this corpus. (The
protection is D21's citation prefix living inside `page_content`; if a later phase moves the
prefix to metadata, the collapse risk returns silently — re-run this check after any such
change.) Sidecars present inside `chroma_db/` (gitignored, wiped by `--reset`); manifest reads
the configured model; `git status` clean of forbidden paths. Exact-token acceptance: **4/5
queries hit the right paragraph in top-3** — "priority entry" (14.8.5.x #1–3), "s.72 burdens"
(14.12/14.12.7 #1–2), "Form 60" (14.15 #2), "compulsory first registration" (13.2 #1). The miss:
the full statute title "Registration of Deeds and Title Act 2006" — BM25 ranked the right chunk
(13.1.2) **#1**, but every query token is corpus-ubiquitous (of 98%, and 94%, act 49%, title
47%, registration 30%), so mid-rank chunks appearing in *both* arms outscored a single-arm #1
under RRF; it surfaces at fused rank 6 (inside the default top-6 the generator sees, outside the
top-3 bar). Registered alongside D23's MiniLM truncation as the Phase 5 tuning suspects: RRF's
consensus bias vs single-arm exact hits (candidate: weighted fusion or citation-query routing).
Robustness probes, live: pickle removed → vector-only + warning; pickle truncated → warning +
vector-only (the new path); manifest overwritten → `ValueError` naming both models.
**Process note:** per the compressed-schedule decision (6 Jul), in-session go/no-go gates with
posted acceptance evidence supersede the one-phase-per-session rule through the 10 Jul freeze.

## D27 — BM25 tokenizer keeps dotted section numbers as single tokens (6 Jul 2026)
**Decision:** `_TOKEN_PATTERN` changes from `\w+` to `\d+(?:\.\d+)+|\w+` — a dotted numeric
sequence ("14.8.5", "3.2.1") is one token. BM25 pickle rebuilt (vector store unaffected).
**Why:** observed on the real index before the change: query "14.8.5" tokenized to bare digit
groups `[14, 8, 5]`, which made **any** section sharing those digits an equal lexical match —
14.8.3.5 ranked #1 and 14.8.5 itself was absent from the fused top-3. Dotted paragraph numbers
are this corpus's citation identity and the product's flagship exact-lookup case (D6's own
motivating example). After the change: "14.8.5" → fused #2, "13.2" → fused #2, and the
suite-level test pins the behaviour (a section sharing digit groups but not the dotted token
must not match).
**Rejected:** leaving `\w+` (flagship case unserved); hierarchical prefix-matching so "14.8.5"
also matches child tokens like "14.8.5.2" (complexity; exactness is the point, and children stay
reachable via their own tokens — the trade-off is deliberate). Known residual, deliberately not
tuned tonight: for bare-citation queries the vector arm is semantic noise, and RRF's consensus
bias can still push a BM25-only exact hit below double-dip chunks (e.g. "14.12.4.2" — BM25 #2–3,
fused >3; that section is also split across 3 chunks, diluting each). Phase 5's golden set is
the instrument for that decision — n=35, not n=2.

<!-- Append new entries below. Format: ## Dn — Title (date) / Decision / Why / Rejected / Consequence -->

## D28 — Phase 4 generation: compact citation locators, refusal path, grounding validation (6 Jul 2026)
**Decision:** Handbook answers cite in the compact form `[Handbook, para 3.2.1, p.87]`, built in
`format_context()` from each chunk's `section_number` + page span (legislation/case-law keep the
generic `[Source i: …]` header, branched on `document_type`). The system prompt instructs that exact
form plus a single `REFUSAL_PHRASE = "not covered in the source material"` constant (imported by the
Phase 5 matcher so the string cannot drift). Source extraction uses one tolerant, token-anchored
regex `CITATION_RE` that captures `(para, page)` from BOTH the compact form and the longer D21 in-text
prefix `[Conveyancing Handbook, Ch.N …, para X, p.Y]` the model may echo verbatim — anchored on the
`para`/`p.` tokens, never comma-split (the chapter-title segment is OCR'd free text containing commas).
`validate_citations()` then splits cited `(para, page)` into grounded vs. ungrounded against the
*actually-retrieved* chunk metadata: grounded when the cited page lies in a retrieved chunk whose
`section_number` nests-or-equals the cited paragraph (component-wise on dot boundaries). `--verbose`
prints per-chunk fused RRF scores + section/page and flags ungrounded citations.
**Why:** the product's whole point is grounded, page-cited answers. Postel's law — prompt strictly for
the short locator (the chapter is derivable from the decimal paragraph number, and the OCR'd chapter
title is the noisiest part of the prefix), accept liberally (the model sometimes echoes the baked-in
long prefix). The real anti-hallucination guarantee is not the citation *format* but validating each
(para, page) against retrieved chunks — the tolerant regex without that check would parse invented
locators just as happily. Live acceptance: 3/3 in-corpus answers grounded (0 ungrounded), each
cross-checked against the PDF (para markers + content on the cited printed pages, e.g. 14.12/p.533,
14.6.12/p.514, 16.13/p.691); the refusal path is prompt-driven on context *sufficiency* — "CGT rate"
is answered (it is genuinely in the handbook's tax chapter 16.13) while genuinely out-of-corpus
questions (divorce grounds, minimum wage, careless driving) return the canonical phrase.
**Rejected:** adopting the long `[Conveyancing Handbook, …]` prefix as the canonical citation (more
grounded but bloats multi-paragraph answers with noisy OCR'd titles; the short form loses nothing);
comma-splitting the citation parser (breaks on commas inside chapter titles); exact `section_number`
equality in grounding (flags legitimate sub-paragraph citations — a more capable model cites `14.12.1`
where a chunk's section is the parent `14.12`; relaxed to nesting, which still catches invented
paragraphs and page-mismatches).
**Consequence:** F5's coupling stands — the D21 prefix is still baked into `page_content`; the citation
chain is ingest page-map (D13) → chunk metadata (D21) → this locator. Phase 5's eval must score both
the short and long captured forms as valid, or the tolerant regex becomes a silent metric penalty.
**Known limitation (accepted 7 Jul):** citations into APPENDIX sections (e.g. `[Handbook, para
APPENDIX 14.1, pp.565–566]`, which the model does emit) are not captured by `CITATION_RE` — the
`para`+digits anchor skips them, so they render in the answer text but are invisible to the sources
list and the grounding check. Fails safe (misses legitimate citations, never admits invented ones).
Shipped as-is per user decision; the Phase 5 golden set gets ≥1 appendix-focused question to measure
whether it matters in practice, and the fix is revisited only if it does.
**Gate hardening (7 Jul, from /phase-gate 4 code review):** `is_refusal()` now requires the canonical
phrase AND zero extracted citations (a partial answer hedging with the phrase while citing sources is
an answer, not a refusal — protects the Phase 5 refusal metric); `query()`'s empty-retrieval return
carries the same keys (`citations`, `citation_check`) as the normal path, so eval-loop callers never
need key guards.

## D29 — Generation model claude-sonnet-4-6 → claude-sonnet-5 (6 Jul 2026)
**Decision:** `get_llm()` uses `claude-sonnet-5`, drops the `temperature=0` argument, and sets
`thinking={"type": "disabled"}`.
**Why:** the portfolio piece should ship on the latest Sonnet (adopted-delta #1). IMPLEMENTATION_PLAN.md
Phase 4 lists no model change, so this is a sanctioned divergence, logged here per the CLAUDE.md
"don't improvise silently" rule. Sonnet 5 **rejects a non-default `temperature` with a 400**, so the
former `temperature=0` is removed and determinism is steered through the strict grounding system prompt
instead. Adaptive thinking is on-by-default on Sonnet 5 (it was off on 4-6); `thinking` is held disabled
to keep this a clean one-variable model swap and to avoid thinking eating into the 2048-token
`max_tokens` budget on long cited answers. Sequenced last and verified in isolation: the full
citation/refusal/grounding change landed and passed live acceptance on claude-sonnet-4-6 first, then the
model was bumped and re-verified (3/3 grounded in-corpus, 2/2 refusals) — so if Sonnet 5 regresses
before the freeze it reverts to a known-good baseline in one commit.
**Rejected:** bumping the model up front (conflates a model regression with the prompt/format change);
keeping `temperature=0` (400 on Sonnet 5); leaving adaptive thinking on (larger behavioural delta +
truncation risk at max_tokens=2048 — revisit with a higher budget if answer quality warrants).
**Consequence:** Sonnet 5's finer-grained citations (sub-paragraphs like `14.12.1`) are what motivated
the nesting-aware grounding relaxation in D28; the two changes were validated together on the live corpus.

## D30 — Phase 5 evaluation harness: reuse D28's grounding logic, scrub the report of corpus text (7 Jul 2026)
**Decision:** `src/evaluator.py` scores two metrics independently against `eval/golden_set.jsonl`
(authored in parallel, not touched by this change): retrieval hit@k (`evaluate_retrieval`) and refusal
accuracy (`evaluate_refusals`). Hit@k reuses `_sections_related` from `src.generator` unchanged — the
same equal-or-dotted-nesting rule that grounds citations in D28 also decides whether a retrieved
chunk's `section_number` counts as covering an expected section, so a golden question expecting the
parent paragraph `14.12` still counts a retrieved child `14.12.1` as a hit. Refusal accuracy reuses
`is_refusal` unchanged for the same reason D28 built it that way: a hedge that still cites a source is
scored as an answer, not a refusal. `run_eval()` prints and writes an identical Markdown report (one
`_format_report` string, not two), and both surfaces are hard-scrubbed to question text, section
numbers, and metrics only — retrieved chunks are represented by `section_number` alone (no
`page_content`) and refusal rows print only a `refused`/`answered` flag, never the generated answer
text, since a hedged answer can itself echo corpus prose.
**Why:** CLAUDE.md's copyright rule ("NEVER commit `data/`, any `*.pdf`... the corpus is copyrighted;
the repo is public") extends to `eval/results.md`, which *is* committed — the report is generated from
retrieved chunks and live LLM answers, both of which can carry verbatim handbook text, so the scrub has
to happen at report-generation time, not as an afterthought. Reusing `_sections_related`/`is_refusal`
rather than re-implementing matching logic keeps the eval honest: it measures the same notion of
"related section" and "refusal" that the product actually uses, not a laxer or stricter stand-in that
would silently inflate or deflate the hit@k the Phase 5 acceptance gate (≥80%) is judged against.
**Rejected:** a separate section-matching function for eval (drift risk — the eval's definition of
"hit" would diverge from the generator's definition of "grounded" over time); including retrieved page
numbers in the per-question report line (the return contracts for `evaluate_retrieval`/
`evaluate_refusals` carry section numbers only, and the copyright rule doesn't *require* pages be
shown — adding them would mean threading page metadata through per-question dicts for no metric
benefit); printing the raw refusal-path answer text in the report for debugging (the accuracy metric
doesn't need it, and a hedged answer is exactly the case most likely to contain quoted corpus text).
**Consequence:** `run_eval`'s default `retrieve_fn`/`answer_fn` make live vector-store and Claude API
calls (unchanged retrieval/generation paths from Phases 3–4); every test in `tests/test_evaluator.py`
injects fakes, per CLAUDE.md's no-network-in-tests rule. The one-tuning-iteration step and the ≥80%
hit@6 acceptance check are deliberately left for a follow-up run against the real index — this phase
is the harness only.
**Gate fixes (10 Jul 2026):** the Phase 5 gate review caught two holes in the above. (1)
`evaluate_refusals` was carrying the full generated answer in its per-question rows — a write-only
field nothing read, and exactly the corpus-echo risk this entry's scrub exists to exclude; the field
is dropped, so the return contract now genuinely carries only questions, section numbers, and flags.
(2) `load_golden_set` validated `expected_sections` with a bare truthiness test, so a string value
(`"14.8"` instead of `["14.8"]`) slipped through and was iterated character-by-character downstream,
silently inflating hit@k; it now requires a non-empty list of non-empty strings (stored stripped) and
raises the loader's usual line-numbered ValueError otherwise.

## D31 — Phase 5 tuning iteration: fusion constants retained after grid search (9 Jul 2026)
**Decision:** `RRF_K` stays 60 and `CANDIDATE_POOL` stays 12. A 12-config grid (RRF_K ∈ {60, 30, 20, 10} ×
CANDIDATE_POOL ∈ {12, 20, 30}) was measured with the Phase 5 harness against the real index (hit@6,
n=30 retrieval questions, offline — no API cost): baseline 27/30 = 0.900; every pool-widening config
at RRF_K ∈ {60, 30, 20} scored 26/30; at RRF_K=10, POOL=20 matched the baseline at 27/30 by swapping
misses (POOL=30 still 26/30); lowering RRF_K alone changed nothing. No configuration beat the defaults.
**Why:** widening the pool fixes the two arguably-mislabelled misses (tenants in common, purpose of
requisitions) but regresses three previously-passing questions (Form 60, Registration of Deeds and
Title Act 2006, lender undertakings) — a net loss. Mechanism: a wider candidate pool feeds RRF's
consensus bias; moderately-ranked double-dip chunks accumulate fused score and push single-arm exact
hits out of the top 6 — empirical confirmation of D27's analysis. The requisition-27.5 miss survives
every config: bare-number lookup is not addressable by fusion constants (the vector arm is semantic
noise for numerals) and stays a documented limitation.
**Rejected:** adopting RRF_K=10/POOL=20 (also 27/30 — swaps misses without improving the rate, for a
larger delta from validated Phase 3 behaviour); chunk-size variants and an embedding-model swap
(re-index + full re-validation two days before freeze; D23's MiniLM 256-token truncation remains the
registered suspect and moves to the README roadmap).
**Consequence:** eval/results.md ships the accepted numbers: hit@6 = 0.900, refusal accuracy 5/5. At
n=30, ±1 hit is within noise; the grid's value is evidence the defaults aren't sitting left of a cheap
win — tuning was *run and resolved*, not skipped.

## D32 — v1 freeze: fail-visible warnings + strict refusal matching (11 Jul 2026)
**Decision:** Two interim safety changes ahead of the v1 tag, both display/scoring only. (1) The CLI
now warns on every answer whose grounding cannot be vouched for: a non-refusal answer with zero
extractable citations prints an explicit "unverified" warning, and ungrounded-citation warnings print
unconditionally instead of only under `--verbose`. (2) `is_refusal` is tightened from
substring-plus-no-citations to normalized exact match: the whole answer, after stripping whitespace,
one layer of surrounding quotes, and trailing periods, then casefolding, must equal
`REFUSAL_PHRASE`. The old `extract_citations` guard is removed as redundant under exact matching.
**Why:** An external review (11 Jul) correctly identified that grounding was checked but never
enforced or surfaced at the output boundary — a citation-free answer produced two empty validation
lists and printed as if valid, and a hedged sentence like "This is not covered in the source
material, but the likely answer is 20 days." scored as a successful refusal. The system prompt has
always demanded exactly the refusal sentence and nothing else, so the scorer now holds the model to
the contract the prompt states. The live refusal pass was re-run under the strict matcher: still 5/5.
**Rejected:** The full fail-closed grounding gate (blocking unverified answers) for v1 — it requires
appendix-citation support first, or correct appendix-cited answers would be wrongly blocked; both
land in v2 (Phases 7–8). Keeping the substring matcher to protect the 5/5 metric — an honest 4/5
would have been reported instead.
**Consequence:** v1 is fail-visible, not fail-closed: nothing is withheld, but nothing unverified
passes silently. The evaluator imports `is_refusal`, so eval scoring inherits the strict definition
with no separate harness logic. `GENERATION_MODEL` is hoisted to a constant in `generator.py` so the
eval provenance block (D33) records it from one source of truth.

## D33 — dual-metric eval labeling + provenance block (11 Jul 2026)
**Decision:** `evaluate_retrieval` scores every question under two explicit bases and the report
carries both, strict first: *strict* = exact section-number equality; *related* = the existing
`_sections_related` dotted-nesting, which matches **either direction** — a retrieved parent OR child
of an expected section counts (e.g. expected `6.3.2` matches retrieved `6.3.2.2` or `6.3`); the
symmetry is inherent to `_sections_related`'s component-prefix comparison and predates this phase
(Phase 5's 27/30 used the same matcher). The report
also gains a Provenance section (git SHA + dirty flag, indexed chunk count, embedding model,
generation model, matching definitions, per-type question counts, refusals-skipped flag), a label
stating the 30-question set was used to tune fusion constants (D31) and is NOT held-out, and
per-question `strict=`/`related=` flags. Current real-index numbers: strict 24/30 = 0.800, related
27/30 = 0.900, refusals 5/5.
**Why:** The external review reproduced our 27/30 but showed the headline conflated related-section
matching with exact retrieval, was tuned on its own eval set, and shipped without provenance —
"the artifact itself does not establish reproducibility." Related-matching is a defensible retrieval
metric (a child chunk usually contains the parent's answer), but presenting it unlabeled overclaims.
The two bases bracket the truth; the reviewer's independently-measured 24/30 strict matches ours
exactly, which is itself evidence the harness is honest.
**Rejected:** Dropping the related metric entirely (it captures real retrieval quality the strict
basis undercounts — three of the six strict-misses retrieved a directly-nested neighbour of the
expected section); making provenance collection mandatory in tests (tests inject a fake
`provenance_fn`; git/store access inside the suite would violate the no-IO rule).
**Consequence:** From Phase 10 (eval v2), the submission headline becomes strict hit@6 on a held-out
set — expected to be lower than 0.900 and defensibly so. `run_eval`'s return and the report format
changed shape (`hits_strict`/`hits_related`); nothing outside the evaluator consumed the old keys.
**Definition correction (11 Jul 2026, same day):** the initial D33 wording described "related" as
child-counts-for-parent only. An adversarial review of this phase's own diff caught that
`_sections_related` is symmetric — a retrieved *parent* also counts — and that exactly one of the 27
related hits (the accountable-trust-receipt question, expected `9.6.1`, retrieved parent `9.6`)
exists only via that direction; under a child-only reading the related rate would be 26/30. The
matcher is unchanged (it is the same one Phase 5 shipped); the definition text here, in the report,
and in the README was corrected to state "either direction" so the published metric describes the
code exactly. Strict 24/30 remains the conservative anchor and is unaffected.
**Numbering note:** decisions entries written before 11 Jul that refer to "Phase 6" mean the OLD
Phase 6 (portfolio surface / fresh-clone quickstart), which the two-track re-plan renumbered to
**Phase 11**. See IMPLEMENTATION_PLAN.md.
**Held-out set frozen (12 Jul 2026):** `eval/heldout_set.jsonl` — 20 in-corpus questions (15
direct, 5 exact_token, incl. APPENDIX 7.1 and APPENDIX 16.3 expectations) + 8 near-domain refusal
hard negatives, authored from the handbook itself and verified against the corpus only (exact-chunk
existence, printed-page agreement, zero tuning-set overlap, negative answer-absence) with NO
retrieval, similarity search, or generation run against any question before freezing.
SHA-256 `601a81c0a3e36aa5d90afb7904fdebad7704d3fff506871c0b9032d7576dcfe6`. Selection rationale and
the full 42-candidate audit trail live in `eval/heldout_candidate_review.md`. Phase 10 scores it;
Phase 12 runs BOTH v1 (worktree at `v1.0-baseline`) and v2 against this exact file for the
head-to-head; nothing is ever tuned on its results.
**Provenance dirty-flag disambiguation (12 Jul 2026):** a generated report cannot record its own
commit's SHA, so the freshly regenerated `eval/results.md` always made the tree look dirty and the
committed artifact read `(dirty)` with no explanation visible to a reviewer (colleague review,
12 Jul). `collect_provenance` now takes `exclude_paths` (run_eval passes its own `results_path`)
and reports `git_dirty_other` — the count of dirty files beyond the report; the sha line renders
`(clean)` / `(clean apart from this generated report)` / `(dirty: N file(s) beyond this report)`,
degrading gracefully when git is unavailable. Rejected: a footnote sentence in the report (mushy,
"typically" phrasing) and amending commits to hide the two-step dance (history rewriting).

## D34 — appendix locators first-class end-to-end; never-cross-match rule (12 Jul 2026)
**Decision:** `APPENDIX N.M` becomes a first-class citation locator with one grammar on every
surface, `chunker._prefix` being the reference: an appendix section renders **verbatim, never
behind a `para` token** — in the chunk prefix (already correct), the retriever's compact header
(was emitting malformed `[Handbook, para APPENDIX 14.1, p.N]`), the CLI `--verbose` retrieval
lines (same hardcoded `para`), and the `raw` display string of extracted citations.
`CITATION_RE` gains an alternation branch for `APPENDIX \d+(?:\.\d+)*` inside the same
bracket-with-page structure; the `APPENDIX` token alone is case-tolerant (scoped `(?i:...)` group,
model output may write "Appendix") and `extract_citations` canonicalizes it to uppercase, keeping
the existing `{"para", "page", "raw"}` dict shape (`para` holds `"3.2.1"` or `"APPENDIX 14.1"`).
`_sections_related` states the matching rule explicitly: **appendix-ness must match on both
sides** — `"14.1"` never relates to `"APPENDIX 14.1"` in either direction; two appendix locators
strip the prefix and reuse the existing component-nesting rule (so `APPENDIX 14.1` relates to
`APPENDIX 14.1.2`); mixed is always False.
**Why:** An appendix-only-cited answer extracted zero citations, so it printed the D32
"unverified" warning today and would be **wrongly blocked** by Phase 8's fail-closed gate — the
critique item this phase clears before the gate lands. 4 of 30 golden questions (and 2 held-out
questions) expect APPENDIX sections. The never-cross-match rule exists because paragraph `14.1`
and `APPENDIX 14.1` are different documents in different locator namespaces: letting them relate
would let a paragraph citation verify against an appendix chunk — a grounding false positive.
**Rejected:** `re.IGNORECASE` on the whole pattern (the convention since the PART false-positive
is that case-tolerance is scoped to exactly the token that needs it); treating the formalized
`_sections_related` as a behavior fix (adversarial audit confirmed the old component-split
returned correct results for every appendix case by accident — the change makes the rule explicit
and intentional rather than emergent, and the old pin tests are reframed as the rule); widening
appendix numbers beyond the chunker's `\d{1,2}\.\d{1,3}` shape was NOT rejected — the extraction
pattern deliberately mirrors the para grammar (`\d+(?:\.\d+)*`) as a harmless superset.
**Consequence:** `extract_citations` moves from 2-tuple `findall` to `finditer` (the alternation
adds a capture group). Evaluator `hit_related` inherits the rule via its `_sections_related`
import; `hit_strict` already handled appendix strings by literal equality — no evaluator change.
The zero-citation warning and the Phase 8 gate now see appendix citations like any other locator.
**Gate-review hardening (12 Jul 2026, same day):** the phase's 8-angle adversarial review found
(with empirical repros) that the alternation's lazy scan let a locator-shaped token inside the
OCR'd chapter-title free-text run hijack the match — `[..., Ch.6 Contracts see Appendix 3,
para 6.3.2, p.220]` extracted `APPENDIX 3` instead of `para 6.3.2` (a regression: the para-only
regex was immune in this direction), and symmetrically a free-text `para N` could steal from a
real appendix locator (pre-existing). Fix: the locator segment must sit directly before the page
segment (`\s*,\s*` replaces the opaque run between number and page) — that adjacency IS the
emitted grammar in both the compact header and the D21 prefix, no test or live output ever
carried interstitial text there, and the change also collapses the reviewer-timed quadratic
backtracking on degenerate unclosed-bracket input to linear. Semantics note: if a bracket somehow
carries two locator tokens, nearest-to-page now wins (was leftmost). Same review: the
`startswith("APPENDIX")`/`para` guard, triplicated across chunker/retriever/pipeline, is
extracted into `chunker.locator_label()` (single owner, surfaces cannot drift); SYSTEM_PROMPT
rule 2 gains the appendix example and an explicit "never rewrite an APPENDIX locator as a para
number" instruction, since the never-cross-match rule would (correctly) flag such a rewrite as
ungrounded. Known and accepted, not fixed: a re-cased `Para 3.2.1` locator still does not extract
(pre-existing; case-tolerance stays deliberately scoped to the APPENDIX token, whose casing the
model must reproduce from metadata — a `para` token appears lowercase in every header the model
sees).

## D35 — fail-closed grounding gate: outcomes, display policy, mandatory page check (12 Jul 2026)
**Decision:** `src/grounding.py` classifies every generated answer into exactly one of four
outcomes, named for what the system actually verifies — citation **locators** against retrieved
sources, never legal claims: `REFUSAL` / `CITATIONS_VERIFIED` (≥1 citation, zero unverified) /
`PARTIALLY_VERIFIED` (≥1 verified and ≥1 unverified) / `CITATIONS_UNVERIFIED` (non-refusal with
zero verified, which includes the zero-citation case — closing the critique's P0). `classify` runs
inside `generate_with_sources`, so `result["gate_outcome"]` reaches every consumer; display policy
lives in `pipeline.query`. Default posture is **block-and-show-sources**: `CITATIONS_UNVERIFIED`
withholds the answer body behind a `BLOCKED — CITATIONS UNVERIFIED` banner whose wording says the
citations *could not be verified* — never that the answer "is not in the corpus" — and prints the
retrieved source headers (locator + pages only, no chunk text) so a solicitor can review manually,
plus rephrase/`--top-k`/`--show-unverified` hints. `--show-unverified` (CLI) / `show_unverified=True`
(API) reveals the draft under an "UNVERIFIED DRAFT" banner and is recorded in the event log.
**Gated public return:** when withheld, `query()`'s returned dict does not carry the draft — the
`answer` key holds the block notice and `answer_chars` records that a draft existed; programmatic
access to raw drafts is only via `generate_with_sources` or the explicit override, so any future
API consumer is safe by default. `PARTIALLY_VERIFIED` answers are **shown** with a banner naming
each failed citation. The grounding check itself is tightened: `_citation_matches_chunk`'s page
check is now **mandatory** — a section-related chunk with no page metadata can no longer verify a
citation (the old `start is None → grounded` branch, which no test pinned, fails closed).
**Why:** The external critique's P0, verified in code: grounding was computed but never enforced at
the output boundary — a citation-free answer displayed exactly like a verified one. For a legal-firm
posture the failure mode must be a visible block with a manual-review path, not silent plausibility.
The mandatory page check does the real safety work because chunks are section-granular (D28):
section-nesting alone is coarse, and page-span agreement is the strongest signal available offline.
**Rejected:** exact-locator-only gate matching (colleague proposal) — D28's section-granular chunks
mean correct answers routinely cite finer sub-paragraphs living inside a chunk, so exact equality
would structurally block the best answers. Withholding `PARTIALLY_VERIFIED` answers (colleague
preference) — the user chose show-with-banner naming the failed citations, reserving full
withholding for zero-verified; both positions recorded here. `GROUNDED`/`UNGROUNDED` outcome names
(the original plan wording) — renamed because "grounded" reads as claim-level truth, which the gate
cannot check; the vocabulary now states citation-locator verification only.
**Consequence:** `query()` reads `gate_outcome` with a defined fallback — a result lacking the key
(legacy callers, old test mocks) gets the exact v1 fail-visible display, and the D32 zero-citation
warning now lives only in that fallback branch, superseded by the gate everywhere else. Two
existing pipeline mocks were updated to carry outcomes; the remaining legacy-mock tests now pin the
fallback path deliberately. The evaluator inherits `gate_outcome` in every generated result for
Phase 10's outcome-distribution metric with no evaluator change.

## D36 — operational event log: contents, hashing, and what is deliberately excluded (12 Jul 2026)
**Decision:** `src/audit.py` writes one JSONL event per `pipeline.query` call, always-on —
including the no-results early return (`action: "no_results"`, a sixth action value added to the
plan's five: shown / shown_with_warning / blocked_unverified / refusal_shown /
shown_unverified_override). Default path `logs/audit_log.jsonl` (already gitignored), `AUDIT_LOG_PATH`
env override. Record: UTC ISO timestamp, best-effort git SHA, **`query_sha256` + `query_chars` —
not raw query text** (raw only under explicit `AUDIT_LOG_RAW_QUERIES=1`, read at call time), top_k,
document-type filter, retrieved `[{id, section_number, page_start, page_end, score}]` (content-hash
IDs — `document.id`, recomputed via `compute_chunk_id` when unset), `gate_outcome`, `action`,
verified/unverified counts, citation locator strings, generation model, `answer_chars`.
**Excluded always: answer text and chunk text.** A failed log write prints a visible one-line
warning and the query proceeds. This is an operational log, **not** a tamper-evident audit trail —
no chaining, signing, or integrity checks are claimed.
**Why:** The critique's auditability theme: a firm must be able to reconstruct what the pipeline
did (what was retrieved, what the gate decided, what the user was shown, whether the override was
used) — but the log itself must not become a new leak: chunk text is copyrighted, and legal query
text can reveal a client's matter, so identity-preserving hashes stand in for both.
**Rejected:** raw query text by default (client confidentiality); logging answer or chunk text in
any form (copyright — same rule as D30's report scrub); hooking the log into
`generate_with_sources` instead of `pipeline.query` — the evaluator calls `generate_with_sources`
directly ~30× per eval run and an operational log of eval traffic is noise that would also slow
evals (the CLI is the operational surface; eval provenance is D33's job); crash-on-log-failure
(a query must not die because `logs/` is unwritable) and silent failure (invisible audit gaps) —
the visible-warning middle ground was chosen; tamper-evidence claims without an implementation.
**Consequence:** `/phase-gate`'s hygiene step now checks `logs/` alongside the never-commit list
(the `.gitignore` entry predates this phase). Tests exercise the real write path only under
`tmp_path`/patched env, keeping the suite IO-clean; `test_pipeline.py` carries an autouse fixture
pointing `AUDIT_LOG_PATH` at `tmp_path` so no test can touch a real `logs/` directory.
**Gate-review hardening (12 Jul 2026, same day):** the phase's pressure-tester passed all five
acceptance criteria pre-hardening; the 8-angle adversarial review then produced fixes applied
in-branch: the retriever now attaches `.id` to BM25-arm Documents (the audit's content-hash
fallback was silently the *common* case — the sidecar stores Documents id-less); the six audit
`action` strings became `ACTION_*` constants in `src/audit.py` (a call-site typo would have minted
a bucket Phase 10's distribution metric silently miscounts, while the sibling gate-outcome
vocabulary already had constants); `_git_sha` is `lru_cache`d and the pipeline test fixture patches
it (the review caught, empirically, ~15 unit tests spawning a real `git` subprocess each run — a
no-unmocked-IO violation); the three copies of the answer+citations print block collapsed into
`_print_answer_and_sources`; the manual-review source listing fixed two page bugs (`p.None` for
explicit-None pages; ASCII hyphen where every other surface uses the D21 en-dash) and the blocked
path now NAMES the unverified locators (restoring v1's hallucinated-locator triage signal that the
block banner had dropped); `query()`'s return shape is now uniform — `answer_chars` and
`gate_outcome` on every path, with `no_results` recording `answer_chars: 0` since no draft ever
existed; audit's heavy imports went lazy so a log-replay script can import `src.audit` without the
chromadb/anthropic stack. **Measured, not assumed:** 0 of 1,470 chunks in the real index lack
`page_start` (and `_sanitize_metadata` drops None-valued keys at index time), so D35's mandatory
page check blocks nothing on the current corpus — the fail-closed rule's blast radius today is
zero. **Accepted as designed, documented not changed:** the broad `except` around the audit write
(a visible warning beats a crashed query; the record hole is the trade); the withheld-return dict
stays a key-by-key allowlist rather than a `{**result}` spread (a spread is fail-open the day a
future key carries draft text); `classify` keeps its spec'd three-param signature; the legacy
display fallback stays (belt-and-braces per the colleague review) with an explicit removal trigger
comment; and a refusal-sentence-plus-stray-citation hybrid intentionally falls through to citation
verification and fails closed — `classify`'s docstring now states this instead of overpromising
"regardless of any citations".

## D37 — source-scoped chunk IDs + per-source sync; load-once retrieval (12 Jul 2026)
**Decision:** Chunk identity becomes source-scoped: `compute_chunk_id(source, text)` =
`sha256(source + "\0" + text)[:16]` (NUL separator prevents `("ab","c")`/`("a","bc")` ambiguity).
New `sync_documents(source, documents, vector_store=None, persist_directory=None)` makes the
store's contents for one source exactly equal the given documents: source authority (a doc's
`metadata["source"]` is set from the param when missing and raises on a conflicting value — the
`where={"source": ...}` lookup keys off it, so a mismatch would silently duplicate the corpus);
**upsert before delete** (new chunks added first, stale ids deleted last — a crash mid-sync leaves
harmless extras that the next sync converges, never missing chunks); **metadata-only drift updates
in place** via `Chroma.update_documents` (langchain-chroma 1.1.0; only on a genuine diff, since the
wrapper re-embeds on update); BM25 sidecar + model manifest rebuilt whenever anything changed —
added, updated, OR deleted — closing the delete-only trap where a re-sync would leave deleted
chunks alive inside the BM25 pickle; `sync_documents(source, [])` empties exactly that source.
`add_documents` stays insert-only for additive callers. `pipeline.index_documents` switches to
sync (grouping chunks by `metadata["source"]` on multi-document paths; the handbook path scopes to
the CLI path verbatim, which is what ingest writes into `source`); `--reset` retained for full
rebuilds. Load-once retrieval: `retrieve(..., vector_store=None, bm25_index=None)` skips per-call
store construction, manifest re-read, and BM25 unpickling when injected (the injector owns the
manifest check); the evaluator's default `retrieve_fn`/`answer_fn` build once per run and close
over the store; `pipeline.query` builds once and injects; `--persist-dir` lands on
index/query/eval (Phase 11's isolated `sample_chroma_db/` depends on it).
**Why:** The critique verified that re-indexing without `--reset` left stale chunks (insert-only
`add_documents` had no delete path — the only removal was whole-directory `rmtree`). Pure text
hashes collide across sources: identical boilerplate in two documents would share one id, so
deleting source A's stale ids could destroy source B's chunk — a real risk on the multi-document
roadmap; D7's "identity means identity" now holds per source. `rank_bm25` has no incremental API,
so correctness demands a from-scratch sidecar rebuild on ANY change, including delete-only syncs.
Eval reloaded the Chroma wrapper, manifest, and BM25 pickle once per question.
**Rejected:** deriving the source scope by grouping `metadata["source"]` inside sync (cannot
express "this source now has zero chunks" — the explicit param can); keeping text-only hashes
(cross-source collision above); delete-before-add ordering (a mid-sync crash loses chunks; the
chosen order only ever leaves extras); unconditional `update_documents` on surviving ids (the
wrapper re-embeds on update — an unchanged re-sync must embed nothing, verified by an
embed-call-counter test); caching the store/BM25 globally in the retriever (explicit injection
matches the `add_documents` convention and keeps tests seam-friendly).
**Consequence:** All pre-Phase-9 ids changed → one full `--reset` re-index performed (1,470
chunks). Acceptance evidence: re-index WITHOUT `--reset` → `0 added, 0 updated, 0 deleted` and the
final ID set EXACTLY equals a fresh `--reset` build's (1,470 ids, set equality True);
retrieval-only eval wall-clock **4.24s → 0.37s** (11.5×, 30 questions) with identical results
(strict 24/30, related 27/30) — the injection changes cost, not behavior. The old
`TestContentHashDedup` cross-source-dedup pin was rewritten: identical text under two sources now
deliberately yields two ids.
**Gate-review hardening (12 Jul 2026, same day):** the pressure-tester passed all three
acceptance criteria (independently corroborating the timing win at 3.3× with the model pre-warmed
in both arms — isolating pure load-once effect); the 8-angle adversarial review then drove these
fixes, applied in-branch: **(1)** delete-all on a single-source store crashed —
`sync_documents(source, [])` emptied the store and `BM25Okapi([])` raises ZeroDivisionError
(reproduced end-to-end; the existing test masked it by keeping a second source alive) — an empty
store now REMOVES the sidecar pickle instead of rebuilding, since an absent sidecar is the correct
artifact (`load_bm25_index` → None → vector-only fallback); **(2)** `run_eval`'s default
provenance ignored `--persist-dir` — the report's chunk_count attested to `./chroma_db` while the
scores came from the flag's store, exactly the wrong-corpus attestation Phase 11's smoke eval
would have hit; `collect_provenance` now takes the directory; **(3)** the
assert+store+BM25 build triple lived in three files and ran TWICE per eval run (once per pass) —
a new `retriever.load_retrieval_context(persist_directory)` is the single blessed way to build
injection args (the manifest check lives inside it, so future injectors cannot forget it — closing
the review's bypassed-guard concern), and `run_eval` now builds once and threads one shared
`retrieve_fn` into both passes; **(4)** a latent full-corpus wipe: the handbook path had no
empty-chunk-list guard, so a `chunk_handbook` regression returning `[]` would have made a routine
re-index silently delete all 1,470 chunks via sync's delete-all form — guarded with a loud
warning + no-op (deliberate deletion stays available via the sync API directly); **(5)** the
multi-source loop's silent `source_path` fallback for chunks missing `source` metadata now raises
(silence would mask a loader regression and mis-scope the sync); **(6)** the multi-source loop
rebuilt the GLOBAL BM25 index once per source (O(N × corpus)) — `sync_documents` gained
`rebuild_bm25=False` and the pipeline rebuilds exactly once after the loop; **(7)** `query()`'s
missing `persist_directory` docstring entry. **Empirically ruled out by the review** (negative
results worth recording): chromadb 1.5.9 round-trips metadata byte-equal incl. int types, so the
feared re-update-forever loop cannot occur; `where={"source": ""}` matches empty strings.
**Accepted as designed, documented not changed:** `update_documents` re-embeds unchanged text on
metadata-only drift — the cheaper metadata-only path needs private `_collection` access, which D7/
D25 deliberately banned; the cost is rare and index-time (revisit if the multi-doc phase makes
metadata corrections routine); the degraded double BM25 load attempt when the sidecar is missing
(None is both "not injected" and "nothing to load" — trivial, warn-once behavior preserved);
`add_documents` retained per spec for additive callers though currently production-dead
(pipeline switched to sync; revisit at the multi-doc phase); injected `retrieve()` skipping the
manifest check stays by design — the injector owns it, and `load_retrieval_context` now makes the
safe path the easy path.

---

## D38 — eval v2: held-out headline, retrieval ablation, two-sided answer quality, experimental judge (13 Jul 2026)
**Decision:** Rebuild the eval around honesty after the 11 Jul external critique found the v1
headline (90% hit@6) was tuned on its own question set and conflated related-section nesting with
exact retrieval. New `run_eval_matrix` (the `eval` CLI now dispatches to it; `run_eval` is retained
unchanged for its Phase 5/6 callers/tests) scores every `(set, mode)` cell and renders one v2
report. Concretely:

- **Held-out headline, strict.** The report's headline is **strict hit@6 on the frozen held-out
  set** (D33), with the count and a **Wilson 95% interval** (`_wilson_ci`, stdlib math) beside it —
  n≈20 means the interval spans several questions' worth of rate, and the report says so ("single
  curated-set estimate", no "statistically-validated architecture" language). Strict = exact
  `section_number` equality; related (dotted-nesting) is reported alongside, never as the headline.
- **Retrieval-mode ablation.** `retrieve()` gained keyword-only `mode` (`RETRIEVAL_MODES =
  hybrid/vector/bm25`, single source of truth) that gates **both** backend resolution AND arm
  execution: `vector` mode never unpickles the BM25 sidecar (no missing-sidecar warning), `bm25`
  mode never opens Chroma or runs the manifest check, and an *injected* arm the mode doesn't use is
  ignored. `bm25` with no sidecar warns and returns `[]` (fail-visible), never a silent fallback.
- **`strict_errors` for benchmarks.** `retrieve(..., strict_errors=True)` (eval sets it) PROPAGATES
  an arm exception instead of swallowing it; an operational failure must abort the benchmark, not
  score as a retrieval miss. Production keeps graceful per-arm degradation (`strict_errors=False`).
- **hit@{1,3,6} + MRR@{top_k}, no re-retrieval.** `evaluate_retrieval` records the 1-indexed rank
  of the first strict/related match per question and derives hit@k for `k in (1,3,6) if k<=top_k`
  (no incoherent hit@6 at top_k=3) plus **truncated** MRR (a miss scores 0; the cutoff is labeled
  `MRR@6` everywhere). All keys are additive — the existing hit_strict/hit_related contract and
  tests are untouched.
- **One shared generation pass, gated.** The generation pass runs iff
  `(not skip_refusals) or (not skip_completeness) or judge`; `include_types` derives from the active
  passes (refusal answers iff refusals scored; in-corpus answers iff completeness scored or judge
  on). Both-skips + no-judge ⇒ **zero** `generate_fn` calls — a dedicated blocker test pins it,
  protecting the offline ablation preview AND keyless Phase 11 CI (where generation would RAISE, not
  just cost). Answers are generated once, cached by question, and reused by refusals + completeness
  + judge (a duplicate question text raises; per-question failures become error rows, disclosed).
  Answer passes always use the HYBRID production config; the ablation affects retrieval scoring only.
- **Two-sided answer quality.** `evaluate_completeness` measures the answerable questions the
  opposite way from `evaluate_refusals`: **false-refusal rate** (answerable questions that got the
  canonical refusal) and **false-block rate** (answerable drafts the gate would withhold as
  CITATIONS_UNVERIFIED — labeled *over-blocking pressure*, NOT proof the block was wrong), plus
  **syntactic sentence-citation coverage** (micro-avg cited/total sentences over non-refused
  answers; "syntactic" because a cited sentence may still carry a wrong locator — grounding is
  separate) and **citation-grounded fraction** (micro-avg grounded/citations; "n/a" when zero
  citations, never a silent 0/1) and the zero-filled gate-outcome distribution. The refusal table
  is two-sided (answerable-refusal rate + near-domain-negative refusal rate); neither is called
  "accuracy" unqualified. A heuristic `split_sentences` (masks bracketed citations, guards prose
  abbreviations, splits newlines then `.?!`+capital) feeds coverage — it gates nothing and its
  limitations are documented.
- **Experimental LLM judge (`src/judge.py`).** Optional `--judge` decomposes each non-refused
  in-corpus answer into claims and scores faithfulness = supported/n_claims (unclear counts
  against), judged **per set**, **conditional on a non-refused answer**. Model JSON is parsed
  strictly (fence-strip + outermost `{...}` + schema check); **API errors are counted separately
  from parse errors**, zero-claim answers are counted, and the mean is **suppressed** when the
  failure fraction exceeds 20%. The report states the judge is the **same model family** as the
  generator (shared blind spots) and calls the whole thing experimental/secondary. `JUDGE_MODEL ==
  GENERATION_MODEL`, `JUDGE_PROMPT_VERSION` pinned in provenance.
- **Results-path guard.** With no explicit `--results/-o`, the committed `eval/results.md` is
  written ONLY by a **canonical** run (held-out set present, all 3 modes, refusals + completeness
  scored, top_k=6, zero generation errors); anything less writes the gitignored
  `eval/results_partial.md`. An explicit path is honored (warned if it targets the canonical path on
  a partial run) but **refused if it equals an input eval set** (writing a report over the set would
  destroy it). Reports are written atomically (temp + `os.replace`), so a crash never half-clobbers
  the committed report.
- **D30 upheld.** The report carries only question text, section numbers, counts, rates, gate
  outcomes, and provenance — never chunk `page_content`, generated answer text, or judge claim text.
  Claim text lives ONLY in the gitignored `eval/judge_review.jsonl` local review dump. Canary-string
  tests plant secrets in fake chunks/answers/claims and assert they never reach the report.

**Why:** A portfolio piece whose entire point is grounded, citable answers cannot ship a headline
that was tuned on its own questions and inflated by nesting credit. Held-out + strict + Wilson makes
the number honest and honestly uncertain; the ablation shows what each retrieval arm contributes;
the two-sided answer-quality view exposes both failure directions (silent over-refusal AND
over-blocking) that a one-sided "refusal accuracy" hid; the judge is a cheap, clearly-fenced
faithfulness sniff-test, not a claim of independent validation.
**Rejected:** flipping `run_eval`'s default results path in place (zero-blast-radius to leave it;
the CLI reroute closes the real footgun); a hard-error results guard (auto-redirect to the partial
path is friendlier and equally safe); a checkpoint/resume answer cache (over-engineered for ~60
calls, a re-run is ~$2); a code-level sha-pin of the held-out path (D33 + the report's printed
sha256 is the cross-check); row-keyed/sha-keyed answer cache (question-text keying with a
duplicate-raises guard is enough); reusing the ablation's per-mode retrievals for generation
(generation is deterministic-hybrid — re-querying is equivalent and keeps `evaluate_retrieval`'s
tested contract intact); stratified judge sampling (deterministic uniform `random.Random(seed)` is
honest and simpler); a same-model judge presented as an independent oracle (disclosed as shared-
family instead).

## D39 — sub-chunking go/no-go, decided on the tuning set (13 Jul 2026)
**Decision (protocol; numbers filled in from the canonical live run):** The pre-declared go/no-go
rule for the sub-chunking experiment is evaluated **on the tuning set only** — the set sanctioned
for making decisions (D31) — NOT on the held-out set, which appears in the ablation tables
descriptively but must never feed a decision (that would burn its out-of-sample status, the whole
point of D33). Rule: proceed to sub-chunking only if vector-only related@6 lags hybrid related@6 by
more than ~10 points on the tuning set (i.e. the vector arm is leaving material recall on the table
that finer chunks might recover); within ~10 points, hybrid retrieval is already capturing the
signal and sub-chunking is not worth the added index complexity for v2.
**Caveat recorded:** a mode delta cannot *prove* sub-chunking will or won't help — it is a coarse,
pre-declared decision rule chosen to avoid post-hoc rationalization, not a causal measurement. The
held-out ablation numbers are reported next to the tuning ones purely so a reader can see whether
the two sets point the same way.
**Outcome (from the canonical `eval/results.md`, git-clean run):** On the **tuning set**,
hybrid related@6 = 0.900 vs **vector-only related@6 = 0.833** — a **6.7-point** gap, *within* the
~10-point threshold → **NO-GO: do not sub-chunk for v2.** Hybrid retrieval already captures the
recall the vector arm alone leaves on the table, so finer chunks are not worth the added index
complexity. Held-out (descriptive comparison only, NOT a decision input): hybrid related@6 = 1.000
vs vector 0.900 = a 10.0-point gap — the same direction, so both sets agree on the call.
Notably **bm25-only related@6 equals hybrid** on both sets (tuning 0.900, held-out 1.000): the
lexical arm carries most of the related-match signal on this decimal-numbered corpus, which is
consistent with the exact-token nature of paragraph citations and is itself an argument against
sub-chunking (the win, if any, would be for the vector arm, and hybrid already backstops it).

## D38 addendum — canonical live-run evidence (13 Jul 2026)
The canonical run (`eval --heldout eval/heldout_set.jsonl --judge`, all three modes, both passes,
top_k=6, **zero generation and zero judge errors**, git-clean provenance) produced
`eval/results.md`. Headline and answer quality, held-out set (frozen, sha256
`601a81c0…dcfe6`, matching D33):
- **Strict hit@6 = 20/20 = 1.000** (95% Wilson CI 0.839–1.000) — the honest, out-of-sample,
  exact-match headline (vs the v1 90% that was *related*-matched on the *tuning* set).
- False refusals 0/20; near-domain negatives correctly refused 8/8; false-block 0/20; sentence-
  citation coverage 0.884 (84/95); **citation-grounded fraction 86/86 = 1.000**; gate distribution
  20 CITATIONS_VERIFIED / 0 else. Tuning set: false refusals 2/30 (0.067), negatives 5/5, coverage
  0.770 (201/261), grounded 209/209 = 1.000, false-block 0/30.
- **Judge (experimental, same-family, conditional on non-refused answers):** mean faithfulness
  0.983 held-out (20/20 parsed) / 0.982 tuning (28/28 parsed), 0 api/parse errors, not suppressed.
  A 48-record local review dump (gitignored) was spot-checked: real atomic-claim decomposition with
  sensible per-claim verdicts. D30 verified on the committed report: zero chunk/answer/claim text
  (a programmatic check confirmed none of the 48 records' claim strings appear in `eval/results.md`).

## D40 — synthetic sample corpus enters at the text seam, not as a PDF (13 Jul 2026)
**Decision:** ship a wholly synthetic ~15-page handbook (`scripts/sample_corpus.py`) and a builder
(`scripts/build_sample_index.py`) that indexes it into a gitignored `./sample_chroma_db/`, so a fresh
clone of this public repo can run the pipeline end-to-end without the copyrighted corpus. The corpus
enters at the **post-extraction seam**: `build_sample_corpus()` hand-builds the exact
`(clean_text, page_map, metadata)` triple `ingest.extract_pdf` produces and feeds it straight to
`chunk_handbook`. It is a standalone adaptation of the `_handbook`/`_body` fixtures in
`tests/test_chunker_handbook.py` (copied, with cross-reference comments both ways, not shared — a
script must never import from the test tree; the chunker tests keep their fixtures self-contained).
The new `tests/test_sample_corpus.py` *does* import its subject under test (`scripts.sample_corpus`,
`scripts.build_sample_index`) — that is normal and resolves under `python -m pytest` because the repo
root is on `sys.path`; it is not the prohibited direction.
**Determinism mechanism (the CI smoke depends on it):** two authoring invariants hold for *every*
surviving section, not just golden-cited ones — (a) body ≥ 600 chars so no runt merges it away
(`RUNT_CHAR_THRESHOLD`; an under-600 parent absorbs its child and the section vanishes), and (b) every
post-merge segment ≤ ~3,500 chars so nothing re-splits into duplicate section metadata
(`OVERSIZE_CHAR_THRESHOLD = 4000`, measured on the whole segment incl. heading). Section 2.4.1 is a
deliberate sub-600 trailing runt (the merge-into-2.4 demonstration) and is never cited. Every golden
question embeds its section's unique fictional token (e.g. "Form VX-9", "Blackthorn Conditions") so
**BM25 alone** — pure Python, platform-deterministic — ranks the right chunk first (a unit test
asserts BM25 top-3 for all 7 questions, with the extra headroom for authoring latitude; in practice
all 7 land at rank 1). That is the floor under the CI hybrid `7/7` assertion; CI additionally asserts
the BM25-only ablation row is a clean sweep so a broken lexical sidecar can't hide behind the hybrid
headline, while the vector row is deliberately left unasserted (its floats vary across platforms). The
builder
**refuses to write into the real `./chroma_db/`** (realpath compare + a casefolded `chroma_db`
basename check, run before any model loads) so a demo can never clobber or contaminate the real index.
**Rejected:** generating a real PDF (a PDF-writing library would be a new dependency for no benefit,
per the CLAUDE.md no-new-deps rule — the text seam is the same code path the real PDF reaches after
extraction); importing the chunker-test helpers into the script; pinning the HuggingFace model
revision to harden cross-platform floats (needs a `src/embedder.py` change, out of Phase 11 scope —
the fixed-key CI HF cache freezes one snapshot and the BM25 floor covers the residual, recorded as an
accepted risk).

## D41 — MIT LICENSE, code-and-sample scope (13 Jul 2026)
**Decision:** add an MIT `LICENSE` (© 2026 Ahsan Malik). The README states the licence covers
**everything in this repository, including the wholly synthetic sample corpus**; the real,
copyrighted handbook is never distributed here and no rights over it are granted. The scope note lives
in the README licence section and in this entry; the `LICENSE` file itself is left as unmodified,
standard MIT text so licence-detection tooling recognizes it cleanly. It reconciles the Phase 11
spec's "MIT covers code only" with the fact that the synthetic sample now lives in the repo: the
sample is original synthetic text authored for this project, so MIT can and does cover it; the phrase
"code only" in the spec was shorthand for "not the corpus", which remains true.
**Rejected:** a bare "code only" note that would leave the sample corpus's licence status ambiguous;
any dual-licence or corpus-carve-out language that could be misread as granting rights over the real
handbook.

## D42 — v2 is the submission artifact; v1.0-baseline stays the fallback (13 Jul 2026)
**Decision:** v2 is selected for submission, to be tagged `v2.0` on main; `v1.0-baseline` remains the
tagged fallback (`git checkout v1.0-baseline`). Grounds: the Phase 12 readiness gate passed (400-test
suite green; pressure-tester PASS on all six checkable claims including proven keylessness; a
high-effort code review of `v1.0-baseline..HEAD` surfaced ten findings, none primary-path correctness
or data-leak, all recorded in the gate report; hygiene greps clean), and the same-set, same-index
head-to-head (`docs/v1-v2-comparison.md`; frozen held-out set sha256 `601a81c0…dcfe6`; same
1,470-chunk store, valid per D39's re-chunking no-go) measured **parity** on every shared cell —
strict/related hit@{1,3,6} of 0.900/0.950/1.000 for both, negatives 8/8 both, false refusals 0/20
both (v1's cell via a driver replaying v1's own answer path, 20/20 successful generations).
Also approved: publishing a GitHub release from the `v2.0` tag (an addition beyond the Phase 12
spec's "tag; push" — the most visible surface for reviewers browsing the repo).
**Why:** the head-to-head is measurement, not narrative — same questions, same index, each version
driven by its own evaluator (with the one disclosed exception above: v1's false-refusal cell, a
metric v1's harness lacks, came from a driver replaying v1's own answer path). Parity on retrieval was the *expected* result (D39 deliberately deferred
re-chunking), so the decision rests on what v2 adds around the answer: the fail-closed grounding gate,
appendix citations, per-source index lifecycle, audit log, report provenance, the honest held-out
strict-headline methodology, keyless CI, and the MIT licence — plus answer-quality evidence v1 cannot
measure (grounded citations 89/89, false-block 0/20, judged faithfulness 0.995 experimental). The
critique → production-hardening → honest-metrics arc is itself the portfolio story.
**Rejected:** submitting `v1.0-baseline` (it measures no worse, but ships no gate, no audit trail, no
CI, no licence, and a headline methodology the 11 Jul critique already dismantled); treating v1's
tuning-set related 0.900 as comparable to v2's held-out strict headline (different set AND basis —
kept in the comparison doc under "narrative, NOT head-to-head"); delaying the tag until after the
fellowship deadline for further hardening (the remaining review findings are recorded and none are
blocking).

## D43 — Haiku query expansion, weighted multi-query RRF inside retrieve() (14 Jul 2026)
**Decision:** add a pre-retrieval LLM expansion stage: `src/query_rewrite.py` calls
`claude-haiku-4-5` (max_tokens 300) once per query to produce ≤3 rewrites (handbook-register
rephrasing, keyword variant, plain paraphrase). `expand_query()` returns a structured
`Expansion` (original; effective rewrites — deduped, original excluded; model; and a status:
`live | no_key | api_error | parse_error | disabled`) and never raises: any failure degrades to
zero rewrites with the status recording why, so keyless CI and offline paths make zero API
calls by construction and the audit log can tell "expansion off" from "expansion broke"
(plan-gate finding). `retrieve()` gains keyword-only `rewrites`; each used arm runs once per
sub-query (original always first) and ALL ranked lists fuse via `_reciprocal_rank_fusion`,
which gains optional per-list weights: **original-query lists weight 1.0, rewrite-derived
lists 0.5** — three correlated rewrites agreeing on a generic chunk (3×0.5/61) can no longer
outvote the original's two arms agreeing on the right one (2×1.0/61); covered by an
adversarial correlated-noise test (plan-gate finding). The same expansion path is used by
`pipeline.query()` and by the evaluator (eval mode `hybrid+rewrite`), so the production config
is the measured config. **This function is also the designated future seam for chat-style
question condensation (history in, standalone question out) — decided, not built.**
**Why:** the 14 Jul failure analysis showed natural staff phrasing produces disjoint BM25/vector
rankings (all fused scores were single-arm 1/(60+r) values) while handbook-phrased eval questions
retrieve perfectly — a vocabulary-mismatch problem that neither arm can fix alone: BM25 has no
stemming and down-weights common words; MiniLM is weak on out-of-register legal paraphrase.
**Rejected:** cross-encoder reranker (→ cut list: no new dep needed but adds a second model
download, latency, and moving parts days before the deadline); HyDE (generates corpus-register
text but costs a larger generation and is harder to audit); static synonym maps (unmaintainable
against an 800-page corpus); putting expansion only in `pipeline.query()` (eval would measure a
pipeline nobody ships).

**Gate fixes (14 Jul 2026):** the diff review showed the flat 0.5 per-list weight fails in the
production two-arm shape (3 rewrites × 2 arms = 6 lists × 0.5 = 3.0 > the original's 2.0); the
weight is now REWRITE_LIST_WEIGHT / n_rewrites per list, capping the whole rewrite bundle at half
the original's full-agreement score for any arm/rewrite count. expand_query's construction-failure
handling was tightened (explicit missing-key check → no_key; any other constructor failure →
api_error) and marker-only/no-alpha model output now counts as parse_error, so a degenerate
expansion can never satisfy the canonical zero-fallback gate.

## D44 — Graded related-guidance answer policy; REFUSAL_PHRASE frozen (14 Jul 2026)
**Decision:** replace SYSTEM_PROMPT rule 3's binary answer-or-refuse with a four-tier coverage
policy: (a) direct answer; (b) partial answer that names what the extracts do not address;
(c) related-guidance answer opening with the exact new `CAVEAT_PREFIX` constant ("The source
material does not directly answer this question. The most closely related guidance is:"), every
statement cited; (d) exact `REFUSAL_PHRASE`, alone, for questions the extracts say nothing
relevant about — other jurisdictions, other areas of law, tangential-term mentions included.
Graded output is an ANSWER: it must carry locator citations and passes through the unchanged
grounding gate; `REFUSAL_PHRASE` stays byte-identical and exact-match-only, now pinned by a
literal byte-canary test (constant-vs-constant assertions can't catch a coordinated rewording —
plan-gate finding). Plan-gate adjustments: rule 4 is reworded to require the legal principle
first *in the substantive answer* (so it cannot conflict with the caveat opener); the human
message's binary closing instruction is updated in lockstep with rule 3 and a test asserts the
old binary-only wording is gone; and `evaluate_completeness` strips the one literal
`CAVEAT_PREFIX` sentence before sentence-splitting so the uncited preface does not depress
`sentence_citation_coverage` (a reported metric — it gates nothing).
**Why:** the audit log proved the model itself emitted the refusal sentence on all six failing
real-world queries (gate never fired) — including one where the correct chunk was retrieved at
rank 1 (para 13.3.11). The binary rule gave the model no permitted middle path. The gate was
exonerated and remains the **citation-grounding guarantee** (every shown citation resolves to a
retrieved passage — it certifies citation honesty, not semantic entailment): a caveat-form
answer with zero citations is correctly blocked (fail-closed working as designed).
**Rejected:** any second refusal wording (is_refusal is exact-match and coupled to grounding
classify, evaluator refusal+completeness scoring, and two test files — a softer refusal variant
would silently zero every refusal metric); loosening the gate for uncited related guidance;
keeping binary refusal and fixing only retrieval (disproven by the rank-1 refusal case);
machine-grading which tier (direct/partial/caveat) the model chose (judge measures claim
support, not tier choice — recorded as a known limitation, revisit post-submission).
**Addendum (calibration iteration, 15 Jul 2026):** the first canonical v3 run showed the
graded policy over-extending tier (c) to near-domain negatives whose subject is only
transactionally adjacent (7/14 exact refusals; failures were all property-adjacent subjects:
landlord-tenant, CPO compensation, rent review, MARP, fees, planning). Repeat probes showed
the tier choice was also unstable run-to-run — Sonnet 5 rejects a non-default temperature, so
boundary robustness must come from prompt structure, not sampling config. Two-step iteration
(budgeted in the Phase 13 plan's risk register; tuned only on tuning-set + realistic-slice
failures, never on held-out specifics): (1) rule 3 restructured subject-gate-first — classify
the question's own subject before considering the extracts; outside-subject questions refuse
regardless of adjacent material; (2) the subject test distinguishes the transactional
due-diligence angle (what a conveyancer checks — in scope) from advice on the underlying
matter (what things cost, whether to seek permissions, how disputes are resolved — refuse).
Spot-checks post-iteration: eviction/divorce/planning refuse stably (2/2 runs each); graded
positives unregressed (unregistered-land caveat answer, deposit direct answer, family-home
direct answer); vendor-died still refuses. Known residual: the solicitor-fees negative stays
caveat-form stably — the model classifies conveyancing fees as in-subject and cites the s.150
LSRA 2015 fee-disclosure duty as related guidance. Judged defensible product behaviour
(honest caveat + verified citation to genuinely fee-related guidance); forcing it would
require naming fees in the prompt (dev-set overfit). Accepted with this written
justification; proper fix is tier-choice grading (Phase 14/15 roadmap).
**Final canonical outcome (run #2, 15 Jul, merge-gate correction):** 11/14 exact refusals
(tuning 5/5, held-out 7/8, realistic 4/6). Fees stayed caveat-form as predicted; planning
and mortgage-arrears ALSO came back caveat-form in this sample despite planning refusing 2/2
in spot-checks — the boundary is sampling-sensitive (fixed default temperature; rewrites
resampled per run), so residual counts carry ±1–2 rows of run-to-run variance. All three
residual subjects have transactional-angle corpus coverage; caveat/gate detail for negative
rows is not yet recorded in the committed report (Phase 14 evaluator addition) — until then
the caveat-form characterization rests on live spot-checks (fees, planning), stated as such
wherever claimed. One cost: 1/20 held-out false refusal (a capacity-law question the
handbook covers from the conveyancing angle — the subject gate read it as underlying-matter
advice); accepted and canaried (prompt-clause pin tests) rather than re-tuned against a
held-out specific.

## D45 — CANDIDATE_POOL stays 12; recall breadth comes from expansion, not pool widening (14 Jul 2026)
**Decision:** `CANDIDATE_POOL`, `RRF_K`, and `DEFAULT_TOP_K` are all unchanged (12 / 60 / 6).
The Phase 13 draft plan proposed widening the pool to 30; the plan-gate Codex critique cited
D31 against it and the draft was rescinded before implementation.
**Why:** D31's grid search already measured pool 30 on the real index: hit@6 fell 27/30 → 26/30
at every RRF_K, because a wider pool feeds RRF's consensus bias — moderately-ranked double-dip
chunks accumulate fused score and push single-arm exact hits out of the top 6. That evidence
stands. The recall problem Phase 13 actually diagnosed is vocabulary mismatch, and the fix is
more *queries* (D43's rewrites, which put the right chunks near the top of their own ranked
lists) rather than deeper lists per query. top_k=6 untouched keeps prompt size, gate behaviour,
hit@6 comparability, and the canonical top_k=6 definition.
**Rejected:** pool 30 globally (contradicts D31's measured regression); a rewrite-only wider
pool (untested premise; adds a second fusion regime for no demonstrated need — revisit with
eval evidence if the realistic slice shows recall still short); raising top_k (prompt bloat;
breaks metric comparability).

## D46 — "realistic" eval slice as a set label; canonical definition v3 (14 Jul 2026)
**Decision:** add `eval/realistic_set.jsonl` (~22 questions: real failing staff queries as
seeds, messy paraphrases, vocabulary-shifted new questions, near-domain negatives) wired via a
new `--realistic` CLI arg as a third set labeled `realistic`; questions use only the existing
`direct`/`exact_token`/`refusal` types. Eval gains mode `hybrid+rewrite` (evaluator-level
`EVAL_MODES`; `RETRIEVAL_MODES` stays 3-valued). Canonical (v3) now ALSO requires: a
realistic-labeled set with answerable hybrid results; **distinct realpaths across all input
sets** (so `--heldout X --realistic X` cannot satisfy both clauses by relabeling — plan-gate
finding); all four `EVAL_MODES`; **expansion actually attempted** (≥1 live expansion, so an
inert/misconfigured rewrite model cannot be vacuously canonical — plan-gate finding) with zero
fallbacks. Two hardening fixes ride along (plan-gate findings): an explicit `--results` equal
to the canonical path now **raises** on a non-canonical run instead of warning, and legacy
`run_eval`'s default output moves off the canonical path. Offline contract preserved: passing
BOTH `--skip-refusals --skip-completeness` also disables expansion, keeping the documented
"both skips = zero API calls" invariant true even on a keyed dev box; the `hybrid+rewrite` row
then renders as an explicit fallback. Per-question report detail keys to the `hybrid+rewrite`
row when it ran (gate outcomes must sit beside the retrieval that produced them). Golden/
held-out sets stay byte-identical (easy slice + refusal calibration). **Honesty label:** this
slice is authored during remediation and used to iterate the prompt — the report and README
present it as dev/regression evidence, not held-out proof; an independently authored realistic
held-out slice (real staff questions) is the roadmap follow-up.
**Why:** the AI-generated sets share the corpus's vocabulary, so they measured near-self-
retrieval — held-out strict hit@6 20/20 while every natural-phrasing query failed. Refusal
accuracy rewarded refusing, tuning the operating point toward "answer handbook-phrased
questions, refuse everything else". The committed report must never again be writable without
the hard slice. `VALID_TYPES` is a hard allowlist and extra JSONL keys are silently dropped, so
a label (not a new type) is the only schema-safe shape.
**Rejected:** a new question `type` (load_golden_set would reject it; IN_CORPUS_TYPES ripple);
a separate recall@k metric (expected_sections are OR-alternatives — hit@k/MRR already carry the
signal; new columns would churn the CI greps for zero information); discarding the existing
sets; blocking Phase 13 on an independently authored second slice (deadline; roadmap instead).
**Addendum (freeze, 15 Jul 2026):** user review complete; frozen at 23 questions (17
answerable, 6 refusal), sha256 in eval/realistic_candidate_review.md. Two comparison/synthesis
rows added at user direction after a 15 Jul field test exposed an intent-reading gap (rewrites
re-word but don't re-frame: "purchase vs sale conveyance" never reached the Ch.2 vendor-vs-
purchaser material a probe showed was retrievable with the right vocabulary). S5 is expected
to strict-miss under Phase 13 behaviour — it is the deliberate measuring stick for Phase 14's
intent-level rewriting. R1 stays a refusal row with its trade-off documented in the review doc.

## D47 — Local-first embedding-model load with cache-miss-only download fallback (14 Jul 2026)
**Decision:** `get_embedding_function()` first constructs HuggingFaceEmbeddings with
`local_files_only=True`; ONLY a recognised cache-miss failure (huggingface_hub's local-entry-
not-found family / the corresponding OSError message shape) triggers one logged retry without
the flag, downloading once. Unrelated construction failures (OOM, bad kwargs, dependency
breakage) re-raise immediately — no pointless network-enabled retry (plan-gate finding).
**Why:** every CLI run made ~25 HuggingFace HEAD requests re-validating a cached model — pure
latency and an offline/demo fragility (a HF outage would take the pipeline down). Fresh clones
still work via the fallback.
**Rejected:** documenting `HF_HUB_OFFLINE=1` only (fragile for a live demo — depends on the
operator's shell); pinning revision hashes (couples requirements to HF repo internals);
retry-on-any-exception (turns unrelated failures into network attempts).

## D48 — Harness codified: fresh-context critics checked in, eval-judged bake-offs, no model scoreboard (14 Jul 2026; renumbered from D43 at the 15 Jul Phase 13 merge — D43–D47 were independently claimed by the Phase 13 entries on a parallel branch)
**Decision:** codify the development workflow as portable, checked-in files (documented in
`docs/harness.md`): the `pressure-tester` and `plan-auditor` subagents — previously referenced by the
phase/plan gates but defined nowhere in the repo — now live in `.claude/agents/`; a `plan-gate` skill
formalizes the pre-implementation gate CLAUDE.md already mandated (design becomes an artifact, then
two independent fresh-context critiques, reconciled); a `bake-off` skill encodes a design tournament
for expensive-to-reverse decisions — independent candidates from a shared brief, cross-critique, and
the **tuning set** (`eval/golden_set.jsonl`) as judge, with the losing design fed to the
pressure-tester as attack surface during implementation; `docs/designs/` holds design artifacts and
bake-off briefs. Prompted by an external multi-agent workflow (tournament + scoreboard + per-agent
containers) reviewed on 14 Jul: the adopted ideas are the ones whose active ingredient is
**context independence of the critic**, not infrastructure.
**Rejected:** a model-graded scoreboard routing work between models (n=1 grading, no ground truth, no
blinding, judge related to a contestant — a vibes ledger, and stale within weeks; the eval set is the
scoreboard); per-agent containers / broker service / multi-repo topology (subagents + git worktrees +
`--sandbox read-only` already provide the isolation at zero infrastructure); any autonomous
third-party agent layer with real-world reach on machines near client data (prompt-injection surface
and unvetted skill ecosystems are GDPR/privilege exposure, not workflow); using the held-out set to
judge bake-offs (selection on it burns the headline metric).
**Consequence:** the gates are now reproducible from a fresh clone and portable to future projects via
the checklist in `docs/harness.md`; the v3 re-chunking decision deferred by D39 is the natural first
`/bake-off` candidate.

## D49 — Generator synthesis rule: organize around the question, not the extracts (17 Jul 2026)
**Decision:** SYSTEM_PROMPT gains Rule 5: organize the answer around the QUESTION; for
compare/contrast questions state the basis of comparison, address each side, and draw the
explicit contrast the extracts support; synthesize across extracts into one coherent response;
every sentence — comparative/summary sentences included — still carries a bracketed locator
(the mandatory rule-3 openers aside); unsupported comparison points are named as gaps under
rule 3(b), never inferred. The human turn is reframed from "Based on the following handbook
extracts" to "Using the following handbook source material… answer as a single coherent
response" (opener and closing in lockstep). The CITATIONS_VERIFIED display note now states
honestly what the gate checks: citations resolve to a retrieved passage — locator and page —
not that the passage supports the claim (pipeline.py display only; gate logic unchanged).
**Why:** the 15 Jul field test returned caveat-form fragments for a comparison question; the
generation audit confirmed every SYSTEM_PROMPT cue was per-statement/per-extract with no
organizing instruction (gen-F1, Codex-arch#7); the old display string oversold the
locator-in-retrieved-set check as verification (gen-F3, Codex-arch#1).
**Rejected:** rewording the rule-3 tier tie-breaks in the same phase (the 15 Jul subject-gate
restructure just calibrated the negatives boundary; re-opening it risks re-flipping them —
plan gate); relaxing per-sentence citation for summary sentences (grounding is the product).
**Frozen:** REFUSAL_PHRASE, CAVEAT_PREFIX, gate fail-closed logic, grounding.classify, the
D44 subject-gate rule-3 text — all byte-canaried.

## D50 — Intent-level rewrite: INTENT-tag contract, corrected fusion algebra, W sweep negative result (17 Jul 2026)
**Decision:** the Haiku rewrite prompt asks for a 4th numbered line `4) INTENT: <restatement>`
re-framing the underlying information need. A dedicated pre-pass (`extract_intent`) peels the
first case-sensitive INTENT-tagged line off the response BEFORE `parse_rewrites` (which stays
byte-unchanged, MAX_REWRITES=3) — the intent can never enter the surface bundle, so no
double-counting is possible by construction; malformed/missing/overlong (> MAX_REWRITE_CHARS)
tags degrade to None, never a parse error. `Expansion.intent_rewrite: Optional[str] = None` is
appended after `status` (all positional constructions unchanged); intents casefold-equal to
the original or any surface rewrite dedup to None. A validly-extracted intent survives
STATUS_PARSE_ERROR (a good reframe is not discarded because the surface lines were
degenerate). retrieve() fuses the intent as its OWN pair of ranked lists on a separate weight
budget W: the equal-rank dominance invariant (original two-arm agreement 2/61 > correlated
noise (1+2W)/61) holds only for W ≤ 0.5, so weights outside [0, 0.5] raise ValueError — v1's
"W ≤ 1.0" claim was false and is withdrawn (plan-gate blocker). Audit events carry
intent_rewrite_sha256 (always, when an intent exists) and intent_rewrite_text (only under
AUDIT_LOG_RAW_QUERIES=1); intent-None events are byte-identical to pre-Phase-14.
**W sweep (17 Jul, cached-expansion protocol — one live expansion per golden+realistic
answerable question, zero fallbacks, 25/47 with intents; offline fusion at W ∈ {0, 0.25,
0.5}):** W=0 baseline: golden 27/30 strict@6 (the 4-line prompt alone improves controls —
committed baseline 25/30), realistic 8/17 = 0.471 (= committed), S5 strict MISS / related
rank 2, N4 HIT rank 3. W=0.25: aggregates identical to W=0, zero per-question flips, S5 not
rescued. W=0.5: S5 strict HIT rank 5, realistic 9/17 = 0.529 — but one golden control
("purpose of requisitions on title", expected 15.1) relegated rank 3→7, a HIT→MISS flip vs
the same-expansion arm. **Selection rule (smallest W rescuing S5 subject to zero
golden-control regressions and N4 HIT): NO W in the gated set qualifies — negative result;
the fallback acceptance (S5 related@6 HIT + live comparison-rubric pass) is engaged.**
INTENT_LIST_WEIGHT ships at 0.25: the smallest studied invariant-preserving weight, zero
measured cost, mechanism stays live/audited for the Phase 15 fusion revisit. Diagnostic for
the record: W=0.30 double-hits S5 AND the golden control, both at rank exactly 6/6 — a
two-knife-edge configuration, too sampling-fragile to ship and outside the gated sweep set.
**Rejected:** escalating W past 0.5 (breaks the dominance invariant); shipping W=0.5 under an
aggregate-only reading of "unregressed" (golden 0.867 ≥ committed 0.833, but a per-question
control flip is exactly what the constraint exists to catch); quote-snapping as an entailment
floor (WS4 — cut entirely at the plan gate; fail-open quote matching is not entailment; moved
to the Phase 15 backlog with its open design questions); group-level fusion normalization
(Phase 15 if intent ever needs authority beyond 0.5).

## D51 — Canonical definition v4: judge required, BM25-loaded guard, top_k boundary validation, substantiated negatives (17 Jul 2026)
**Decision:** `is_canonical` (v3: D46) additionally requires (a) the judge pass ran with zero
API/parse errors on every set — CLAUDE.md's canonical command always said `--judge`; the guard
now enforces it (Codex #13); (b) whenever any DEFAULT path (retrieve factory or generate_fn —
ownership flags captured before factory mutation, Codex #8) could have used the BM25 sidecar,
the sidecar must have actually loaded — a run silently degraded to vector-only can never
overwrite the committed report; fully-injected fixtures carry no BM25 requirement.
`bm25_loaded` + both ownership flags are disclosed in report provenance so a non-canonical run
names the guard that failed. retrieve() raises ValueError on top_k < 1 at the caller-funnel
boundary (kills the negative-slice hazard at its root); NO upper cap — top_k > pool is
deliberately supported (candidate_k widening). Negative rows in the committed report carry
is_caveat / gate_outcome / grounded-citation counts — never answer text (D30) — so
graded-negative claims are substantiated by the artifact (Phase 13 merge-gate finding #1).
**Why:** the eval-architecture audit found is_canonical never checked the sidecar or the
judge — a "canonical" run could have been vector-only or judge-free without any trace.
**Rejected:** capping top_k at CANDIDATE_POOL (v1 — withdrawn per Codex #10: the blocked-answer
UI deliberately advises raising --top-k past the pool); putting the BM25 requirement on
injected fixtures (they own their retrieval; the guard would just break the test suite).

## D52 — LLM client timeouts: dead sockets fail loudly and retry, never hang (17 Jul 2026)
**Decision:** `get_llm()` sets `default_request_timeout=120.0`, `get_rewrite_llm()` sets
`default_request_timeout=60.0`; both set `max_retries=3`. The Anthropic SDK transparently
retries timeouts/connection drops before raising, so a transient dead socket self-heals;
an exhausted retry surfaces as a normal API error (expand_query → STATUS_API_ERROR;
generation → the eval's generation-error accounting, which already blocks canonical).
**Why:** two canonical-eval runs on 17 Jul wedged for hours each — ~7s of CPU over 2h43m,
one in-flight POST never returning, no timeout configured anywhere in the client stack — an
unbounded hang on any dropped connection, in production queries as well as eval runs. Found
completing WS5; fixed forward as a minimal ops-robustness bug fix (Phase 15 backlog item 8
territory, pulled forward out of necessity), constructor-canary-tested.
**Rejected:** wrapping the eval in an external kill-and-restart watchdog only (treats the
symptom, leaves production queries hangable; a restarted canonical run re-spends the full
API budget); longer timeouts (the slowest healthy generation call is well under 60s — 120s
already carries ample headroom).
