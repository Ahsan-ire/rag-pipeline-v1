# Design decisions log

> One short entry per meaningful choice: what we decided, why, what we rejected.
> Append-only. If a decision is reversed, add a new entry — don't edit history.
> This file goes in `docs/decisions.md`.

**Current phase: 4 — generation**

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
