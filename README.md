# Legal RAG Pipeline

## What it is, and why

A retrieval-augmented question-answering pipeline over an ~800-page, OCR-scanned Irish conveyancing
handbook. The product's entire point is **grounded answers with chapter/paragraph/page citations**,
and an explicit refusal when a question isn't answered in the corpus — never a guess.

This was built and validated against **one handbook**, and the chunker assumes that book's own
structural grammar: `CHAPTER N` markers, decimal-numbered paragraphs (`3.2`, `3.2.1`, up to four
levels deep), and an `APPENDIX N.N` numbering scheme. A similarly-formatted manual (chapter +
decimal-paragraph numbering) is a plausible target for this chunker; an arbitrary PDF is not —
there is no "corpus-agnostic, bring your own manual" claim here.

## Status: two tracks

An external critique (11 Jul 2026) found real gaps in how grounding was enforced and how the eval
headline was presented — see `docs/decisions.md` D32/D33 for the full account. The response splits
into two tracks:

- **v1** (this state, tagged `v1.0-baseline` at freeze) — the honest baseline. Grounding is checked
  and **warned on**, but not enforced: an ungrounded or citation-free answer still prints, with a
  prominent warning, rather than being blocked.
- **v2** (in progress, target **13 Jul**, Phases 7–12 of `IMPLEMENTATION_PLAN.md`) — adds a
  fail-closed grounding gate (unverified answers withheld, not shown), appendix citation support,
  per-source re-indexing, an audit log, a held-out evaluation set, CI, and a copyright-safe sample
  corpus.

Fellowship submission deadline: **17 Jul**. 14–17 Jul is buffer to compare v1 vs. v2 and decide
which to submit.

## Quickstart

```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

The corpus PDF is **copyrighted and not included in this repo** (see the Hard rules in
`CLAUDE.md`), so a fresh clone cannot run the pipeline end-to-end yet — there is nothing to index.
A copyright-safe synthetic sample corpus is planned for Phase 11. Until then, the commands below
work against a PDF you supply locally:

```
python -m src.pipeline index ./data/your-handbook.pdf --type handbook   # --reset to rebuild from scratch
python -m src.pipeline query "What are the requirements for first registration of title?" --top-k 6
python -m src.pipeline eval
```

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` — needed only for `query` and the
refusal-accuracy pass of `eval`, not for indexing or the test suite.

## Evaluation

Real-index results against `eval/golden_set.jsonl` (35 questions: 30 in-corpus retrieval + 5
out-of-corpus refusal). Retrieval hit@6 is scored over the 30 in-corpus questions; refusal accuracy
over the 5 refusal questions:

| Metric | Result | Definition |
| --- | --- | --- |
| Strict hit@6 | 24/30 = 0.800 | retrieved section number exactly equals the expected one |
| Related hit@6 | 27/30 = 0.900 | dotted-nesting, either direction: a retrieved parent OR child of an expected section counts (e.g. expected `6.3.2` matches retrieved `6.3.2.2` or `6.3`) |
| Refusal accuracy | 5/5 = 1.000 | strict, exact-sentence match against the canonical refusal phrase |

This 30-question set was also used to tune the hybrid-retrieval fusion constants (D31) — it is
**not held-out**, and the related metric is a real but looser measure of retrieval quality than the
strict one. A held-out question set, with the strict rate as the headline number, arrives in
Phase 10. Full per-question detail and a provenance block (git SHA, indexed chunk count, embedding
+ generation model) are in `eval/results.md`.

## Limitations (v1)

- **Grounding is warn-only.** An answer with no citations, or with a citation that doesn't match a
  retrieved chunk, still prints — with a warning — instead of being blocked. The fail-closed gate
  is Phase 8.
- **Appendix citations render but aren't checked.** The model does cite `APPENDIX 14.1`-style
  sections in its answer text, but the citation extractor and grounding check don't recognize that
  form yet, so it's invisible to both (Phase 7).
- **Embedding truncation.** `all-MiniLM-L6-v2` embeds only the first ~1,000 characters of a chunk;
  BM25 sees the full text (D23).
- **Stale chunks on re-index.** Re-indexing a changed source without `--reset` can leave old chunks
  behind (Phase 9).
- **Bare-number lookups miss.** A query like "requisition 27.5" (no surrounding words) is not
  reliably retrieved (D31).

## Tests

```
python -m pytest tests/ -q
```

190 tests. All IO and models are mocked — no network access, no API key required.

## More detail

- `IMPLEMENTATION_PLAN.md` — phase-by-phase build plan and acceptance criteria.
- `docs/decisions.md` — design rationale, one entry per meaningful choice, append-only.
