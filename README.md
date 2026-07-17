# Legal RAG Pipeline — a first-line sweep of the conveyancing handbook

[![CI](https://github.com/Ahsan-ire/rag-pipeline-v1/actions/workflows/ci.yml/badge.svg)](https://github.com/Ahsan-ire/rag-pipeline-v1/actions/workflows/ci.yml)
&nbsp; **[▶ Live interactive demo](https://ahsan-ire.github.io/rag-pipeline-v1/Demo/demo.html)** — no install, runs in the browser.

Ask a procedure question in plain English. Get a grounded answer with **verified
chapter/paragraph/page citations** — or an honest refusal. Then open the handbook at the cited
page and reach your own conclusion.

That last step is the whole point. This tool is **not** built to replace reading the source: it's a
first-line sweep before diving into an ~800-page manual. The answer orients you; the
**citation is the product** — every one is machine-verified against the retrieved text before you
see it, so `[Handbook, para 6.3.2, p.214]` reliably lands you on the paragraph that actually says
it. An answer that cannot be verified is **withheld, not shown** — the system fails closed rather
than guessing confidently.

## Why I built this, and how it's used

I work with a small legal team specialising in conveyancing, and the reference for almost
everything is one ~800-page handbook. Finding the right paragraph is rarely hard law — it's paging. A general-purpose AI
answers instantly but leaves you wondering whether to trust it, which in legal work means you end
up checking the book anyway. This is the middle path: an answer that arrives already pinned to
chapter, paragraph and page, so the check takes seconds instead of a search.

It has been in real use since mid-July 2026 — me mainly, colleagues occasionally on their own
questions — always on real work questions, never as the final word. Two things in this repo came
directly out of that use: the "realistic" evaluation slice is built
from colleagues' actual phrasing, which failed badly against a system that scored perfectly on my
own polished test questions (see the evaluation section), and the Phase 14 comparison-question
work started as one colleague's complaint about one bad answer. I haven't measured time saved and
won't invent a number; what I can say is that the failures users found became the roadmap.

## The user journey

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/user-journey-dark.svg">
  <img alt="User journey: your question is retrieved against the handbook, an answer is drafted with paragraph and page citations, and a grounding gate checks every citation — leading to a verified answer, a warning, a withheld answer, or an exact refusal. You verify at the cited page." src="docs/diagrams/user-journey-light.svg">
</picture>

Four possible outcomes, never a confident unchecked guess:

| Outcome | When | What you see |
| --- | --- | --- |
| ✅ **Verified answer** | The handbook covers it | Answer + citations, each verified against a retrieved chunk |
| ⚠️ **Partial verification** | Some citations couldn't be checked | Answer + a warning **naming each unverified citation** |
| ⛔ **Withheld** | No citation could be verified | The draft is blocked; retrieved sources shown so you can still look |
| 🚫 **Refusal** | The question is outside the corpus | The exact sentence "not covered in the source material" |

(These are the grounding **gate's** four outcomes, which the demo below illustrates. The answer
*text* itself is separately graded — direct answer, partial answer naming its gaps, closest-related
guidance under an explicit caveat, or the refusal — detailed in [`ABOUT.md`](ABOUT.md).)

## How it works, step by step

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/pipeline-steps-dark.svg">
  <img alt="Pipeline steps: index once offline (extract and clean OCR text, chunk by paragraph numbering, build a BM25 plus vector dual index); then per question — Haiku produces three rewrites plus an intent reframe, hybrid retrieval fuses ranked lists, Sonnet drafts a cited answer, the citation gate checks it, and an audit log records hashes only." src="docs/diagrams/pipeline-steps-light.svg">
</picture>

Why each step exists, in one line each:

1. **Page-aware ingestion** — page numbers are preserved from the first byte, because a citation
   without a page is unverifiable.
2. **Structure-aware chunking** — chunks follow the author's own paragraph numbering, so a citation
   names a real unit of meaning, not an arbitrary text window.
3. **Dual (hybrid) retrieval** — legal questions hinge on exact tokens ("s.72 burdens", "Form 60");
   keyword search catches what semantic search fuzzes past, and vice versa.
4. **Query expansion** — staff phrase questions colloquially; the handbook doesn't. Three quick
   rewrites plus an intent-level reframe (what is the question *really* asking?) bridge the
   vocabulary gap (skippable with `--no-rewrite`).
5. **Graded answers** — direct answer, partial answer that names its gaps, closest-related guidance
   under an explicit caveat, or an exact refusal — never a shrug dressed up as an answer.
6. **The grounding gate** — the step that makes the citations trustworthy: every `(paragraph, page)`
   the model cites is checked against the chunks actually retrieved. Invented citations don't pass.
   To be precise about what that proves: the locator resolves to real retrieved text. It does not
   prove the passage legally supports the claim — that judgment is yours, which is why every answer
   ends at the book.

## Try it — interactive demo, no install

**[Open the live demo](https://ahsan-ire.github.io/rag-pipeline-v1/Demo/demo.html)** — or open
[`Demo/demo.html`](Demo/demo.html) locally in any browser. It runs the pipeline's logic as a guided
simulation over the **wholly synthetic sample handbook** (a fictional jurisdiction — no real corpus
text), and shows all four outcomes above, including watching the gate catch a fabricated citation.

## Try it — real pipeline, fresh clone (no API key needed)

The real corpus is **copyrighted and never in this repo**, so the quickstart runs against the same
synthetic sample handbook (`scripts/sample_corpus.py`) that exercises the identical chunker grammar:

```bash
python3 -m venv .venv && source .venv/bin/activate   # tested on Python 3.12
pip install -r requirements.txt          # installs torch/sentence-transformers (heavy, one-time)
python -m pytest tests/ -q               # full suite, offline, no key

python scripts/build_sample_index.py     # builds ./sample_chroma_db/ (downloads MiniLM ~90MB once)
python -m src.pipeline eval \
  --golden eval/sample_golden_set.jsonl \
  --persist-dir sample_chroma_db \
  --skip-refusals --skip-completeness     # keyless retrieval-only eval → 7/7 on the sample set
```

This is exactly what CI runs (`.github/workflows/ci.yml`) — no `ANTHROPIC_API_KEY` anywhere.

**With an API key** (`cp .env.example .env`, set `ANTHROPIC_API_KEY`) you can generate real answers
and index your own handbook:

```bash
python -m src.pipeline query "How is a Windlass Charge created?" --persist-dir sample_chroma_db --top-k 6
python -m src.pipeline query "What are the requirements for making a valid will?" --persist-dir sample_chroma_db   # → refusal (succession law, not conveyancing)
python -m src.pipeline index ./data/your-handbook.pdf --type handbook   # --reset to rebuild
```

## Does it actually work? (evaluation at a glance)

- **Shipped config, held-out: strict hit@6 = 19/20 = 0.950** — the production pipeline (hybrid
  retrieval + query expansion), measured on questions authored *after* the retrieval constants
  were frozen and never used for tuning. The raw-hybrid retrieval core scores 20/20 = 1.000
  (95% Wilson CI 0.839–1.000) on the same set; the one-question gap is a sampled-expansion flip,
  disclosed per-question in [`ABOUT.md`](ABOUT.md). With n=20, read both as indicative, not a
  benchmark.
- **Citation integrity: 519/519 citations grounded** across all three eval sets — the number that
  matters most for the "citations are the product" claim.
- **The honest number: 0.471 strict hit@6 on messy real-staff phrasing** — a deliberately hard
  "realistic" slice built from real field-test failures (up from 0.353 raw hybrid in the
  same run — the Phase 13 canonical run scored 0.412 for this config; the one target question it
  still misses is documented in D50, not hidden — token-aware chunking is the next lever).
- **Comparison questions get real comparisons** — both field-test comparison questions pass a
  seven-item manual rubric ([`docs/phase14-rubric-spotchecks.md`](docs/phase14-rubric-spotchecks.md)).

Full ablation tables, refusal accuracy, methodology, and provenance:
[`ABOUT.md`](ABOUT.md) and the canonical report [`eval/results.md`](eval/results.md).

## Data handling, in one paragraph

The handbook is copyrighted, so the PDF, the index, and all logs are gitignored and **never
committed** — this public repo ships only code, tests, the synthetic sample, and scrubbed eval
reports (questions and section numbers, never corpus text). The audit log records **SHA-256 hashes**
of queries, not their text — legal queries can reveal client matters. Full detail in
[`ABOUT.md`](ABOUT.md#data-handling).

## More detail

- [`ABOUT.md`](ABOUT.md) — architecture, full evaluation, deployment notes, limitations,
  troubleshooting, roadmap.
- [`docs/decisions.md`](docs/decisions.md) — design rationale, one entry per meaningful choice,
  append-only (D1–D52).
- [`docs/harness.md`](docs/harness.md) — the development workflow itself (gates, fresh-context
  critics, eval-judged bake-offs).
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — phase-by-phase build plan.

## License

[MIT](LICENSE) © 2026 Ahsan Malik — covers everything in this repository, **including the wholly
synthetic sample corpus**. The real conveyancing handbook is never distributed here.
