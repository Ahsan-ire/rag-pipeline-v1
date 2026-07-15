# Legal RAG Pipeline

A retrieval-augmented question-answering pipeline over an ~800-page, OCR-scanned Irish conveyancing
handbook. The entire point is **grounded answers with chapter/paragraph/page citations**, a **graded
answer policy** — a direct answer when the corpus covers the question, a partial answer that names
its gaps, closest-related guidance under an explicit caveat sentence, and an exact **refusal** when
the question is genuinely outside the corpus — and a **fail-closed grounding gate**: an answer with
no verifiable citation is withheld, and a partially-verified answer is shown with a warning that
names each citation it could not verify — never a confident, unchecked guess.

## What it is, and why

Conveyancing solicitors work from a large, densely cross-referenced handbook. The value of an answer
is inseparable from *where it comes from*: "the deposit is held as stakeholder" is worth nothing
without "[Handbook, para 6.3.2, p.214]" so it can be checked. So this pipeline is built to cite, and
to fail visibly rather than fabricate.

It was built and validated against **one handbook**, and the chunker assumes that book's own
structural grammar: `CHAPTER N` markers, decimal-numbered paragraphs (`3.2`, `3.2.1`, up to four
levels deep), and an `APPENDIX N.N` scheme. A similarly-formatted manual is a plausible target for
this chunker; an arbitrary PDF is not — there is no "corpus-agnostic, bring your own manual" claim
here.

## Architecture

```
  your handbook PDF  (local only — copyrighted, never committed)
        │
        ▼  src/ingest.py      extract + clean the OCR text layer   →  (clean_text, page_map)
        │                     strip running headers/footers, repair hyphenation, keep offsets
        ▼  src/chunker.py     structural grammar: CHAPTER / decimal / APPENDIX
        │                     ~one chunk per numbered paragraph (runts merged, oversize split)
        ▼  src/embedder.py    MiniLM vectors in Chroma  +  a BM25 sidecar   (per-source sync)
        │
        ▼  src/query_rewrite.py   Haiku expands the staff question into up to 3 handbook-vocabulary
        │                     rewrites (skippable via --no-rewrite; degrades to the raw query with
        │                     no API call when keyless — CI and offline runs stay zero-call)
        ▼  src/retriever.py   hybrid retrieval: BM25 ⊕ vector per sub-query, fused by weighted
        │                     reciprocal rank fusion (rewrites capped below the original's vote)
        ▼  src/generator.py   Claude drafts a graded answer citing [Handbook, para 3.2.1, p.87]
        │
        ▼  src/grounding.py   fail-closed gate: every citation checked against a retrieved chunk.
        │                     none verified → answer WITHHELD (sources shown, draft on request);
        │                     some verified → shown with a warning naming the unverified ones
        ▼  src/audit.py       append-only event log (query + rewrite HASHES + retrieval + gate
                              outcome; no text)
```

`src/pipeline.py` is the CLI that wires these together; `src/evaluator.py` is the held-out evaluation
harness; `src/judge.py` is the experimental LLM-faithfulness estimate.

## Quickstart (fresh clone, no API key needed)

The real corpus is **copyrighted and never in this repo**, so the quickstart runs against a **wholly
synthetic sample handbook** (`scripts/sample_corpus.py`) — a fictional jurisdiction, invented
registers and forms, zero real text — that exercises the same chunker grammar.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # installs torch/sentence-transformers (heavy, one-time)
python -m pytest tests/ -q               # full suite, offline, no key

python scripts/build_sample_index.py     # builds ./sample_chroma_db/ (downloads MiniLM ~90MB once)
python -m src.pipeline eval \
  --golden eval/sample_golden_set.jsonl \
  --persist-dir sample_chroma_db \
  --skip-refusals --skip-completeness     # keyless retrieval-only eval → 7/7 on the sample set
```

No `ANTHROPIC_API_KEY` is needed for any of the above — only network access, once, to pull the
embedding model. This is exactly what CI runs (`.github/workflows/ci.yml`).

**With an API key** (`cp .env.example .env`, set `ANTHROPIC_API_KEY`) you can generate answers against
the sample index, and index/query your own handbook:

```bash
python -m src.pipeline query "How is a Windlass Charge created?" --persist-dir sample_chroma_db --top-k 6
python -m src.pipeline index ./data/your-handbook.pdf --type handbook   # --reset to rebuild
python -m src.pipeline query "What are the requirements for first registration of title?"
python -m src.pipeline eval --heldout eval/heldout_set.jsonl \
  --realistic eval/realistic_set.jsonl --judge                # canonical, makes live calls
```

## Evaluation

The headline is measured on a **held-out set** authored after the retrieval constants were frozen —
never used for tuning — with **strict** matching (the retrieved section number must *exactly* equal
the expected one). Full detail, provenance, and per-question results are in
[`eval/results.md`](eval/results.md).

> **Headline — strict hit@6 = 20/20 = 1.000** (95% Wilson CI 0.839–1.000), held-out, hybrid.
> With n≈20 the interval spans several questions' worth of rate: read it as indicative, not a
> statistically-validated architecture claim.

Since Phase 13 the eval also carries a **realistic slice** (`eval/realistic_set.jsonl`, n=23):
real staff queries kept verbatim from field testing, messy colloquial paraphrases, and near-domain
negatives. It exists because the AI-authored sets share the corpus's vocabulary and measured
near-self-retrieval — 20/20 on held-out while natural-phrasing queries failed in the field. It is
honestly labeled **dev/regression evidence, not held-out proof**: it was authored during the Phase
13 remediation and is used to iterate on it.

Retrieval ablation, strict / related hit@6 (related = a retrieved parent *or* child of the expected
section also counts). `hybrid+rewrite` = hybrid plus Haiku query expansion — the production config:

| Mode | Held-out S@6 / R@6 | Tuning S@6 / R@6 | Realistic S@6 / R@6 |
| --- | --- | --- | --- |
| **hybrid+rewrite** (production) | **1.000 / 1.000** | 0.833 / 0.933 | **0.412 / 0.588** |
| hybrid (no expansion) | 1.000 / 1.000 | 0.800 / 0.900 | 0.353 / 0.588 |
| vector only | 0.900 / 0.900 | 0.767 / 0.833 | 0.353 / 0.588 |
| bm25 only | 1.000 / 1.000 | 0.800 / 0.900 | 0.118 / 0.412 |

Two honest readings of that table. First, expansion is doing real work exactly where it was built
to: on the realistic slice it doubles strict hit@1 (0.118 → 0.235) and lifts strict hit@6
(0.353 → 0.412) over raw hybrid, with no cost to the held-out headline. Second, the realistic
numbers are **low in absolute terms** — that is the point of the slice. Messy real-world phrasing
is far harder than handbook-vocabulary questions, and this is the published baseline the Phase 14
work (intent-level rewriting, synthesis) is measured against. BM25 collapsing on this slice
(0.118) is the vocabulary-mismatch failure made visible.

The tuning set (`eval/golden_set.jsonl`, n=35) is labeled and reported separately because it *was*
used to select the fusion constants (D31); it is not the headline.

Answer quality (generation through the production hybrid+rewrite config):

| Measure | Tuning | Held-out | Realistic |
| --- | --- | --- | --- |
| False refusals (answerable questions wrongly refused) | 0/30 | 1/20 | 2/17 |
| Near-domain negatives correctly refused | 5/5 | 7/8 | 4/6 |
| Citation-grounded fraction (Σ grounded / Σ citations) | 242/242 | 108/108 | 123/123 |
| Sentence-citation coverage | 0.880 | 0.946 | 0.715 |
| False-block rate (answerable drafts the gate would withhold) | 0/30 | 0/20 | 0/17 |
| LLM-judged mean faithfulness *(experimental, same-family judge)* | 1.000 | 1.000 | 0.983 |

The refusal rows changed with the graded policy and are reported with their reasoning, not hidden:
the three negatives that answer instead of refusing (solicitor fees, planning permission, mortgage
arrears process) are questions where the corpus genuinely contains transactionally related guidance
— the model returns it under the explicit "does not directly answer this question" caveat with
verified citations, which is defensible behaviour under the related-guidance policy even though the
binary metric scores it as a miss (D44 addendum records the calibration and this residual). The
three false refusals are retrieval misses or subject-boundary edge cases on the hardest questions;
per-question detail is in the report. Tier choice is not yet machine-graded — that is a recorded
limitation with a Phase 14/15 roadmap fix.

Provenance for these numbers (from `eval/results.md`): git `b627ff2`, 1470 indexed chunks, embedding
`all-MiniLM-L6-v2`, generation `claude-sonnet-5`, query expansion `claude-haiku-4-5` (86/86 live,
zero fallbacks). Generation is sampled at the API's fixed default temperature and expansion rewrites
vary between runs, so boundary rows can flip run to run — the report is one canonical sample, not an
average.

The CI smoke number (`7/7` on the synthetic sample) is a **plumbing check that the retrieval path
works end-to-end**, not an accuracy claim — the sample corpus is tiny and authored to be
deterministic.

## Data handling

- The corpus PDF, the Chroma index, and any `.pdf`/`.env`/`logs/` are **gitignored and never
  committed** — the handbook is copyrighted and the repo is public.
- The **sample corpus is wholly synthetic** original text authored for this project.
- Committed eval reports carry **no chunk, answer, or claim prose** (D30) — no corpus text. They do
  include the eval *questions* (authored, not corpus) and the retrieved section numbers and page
  ranges, alongside the aggregate metrics.
- The audit log (`logs/`, `src/audit.py`) records **SHA-256 hashes of the query and of each
  expansion rewrite**, not their text (legal queries can reveal client matters), plus retrieval
  IDs, gate outcome, and counts — never the answer or chunk text. Raw query/rewrite logging is
  opt-in via `AUDIT_LOG_RAW_QUERIES=1` and belongs only on a single-user dev machine — remove it
  before anyone else can query the system.
- **Where corpus text does leave the machine:** a keyed `query` or a full `eval` sends the *retrieved
  chunk context* to the Anthropic API as part of the generation prompt. The keyless paths above
  (indexing, retrieval-only eval, tests) send nothing anywhere.

## Deployment notes (firm setting)

- The index is a local directory; no corpus text is stored in any cloud service by this pipeline.
- Retrieval is **keyless**; only answer generation needs an API key — retrieval and evaluation of
  retrieval can run entirely offline.
- The embedding model is recorded in a manifest beside the index and asserted at query time, so a
  corpus indexed under one model can't be silently queried under another.
- Dependencies are pinned to exact versions in `requirements.txt`.

## Limitations

- **Single-corpus grammar.** The chunker keys on this handbook's `CHAPTER`/decimal/`APPENDIX`
  structure; a differently-structured document needs a different strategy (the legislation strategy
  is retained and routed by `--type`).
- **Small-n eval.** The held-out set is n=20 answerable + 8 negatives; the 1.000 headline has a wide
  confidence interval (0.839–1.000). It's an honest out-of-sample estimate, not a large-scale
  benchmark.
- **Embedding truncation.** `all-MiniLM-L6-v2` embeds only the first ~256 tokens of a chunk;
  a 15 Jul measurement found 71% of chunks exceed that window, so the vector arm never sees the
  back half of a median chunk. BM25 sees the full text (D23). Token-aware chunking is the top
  post-submission retrieval fix.
- **Realistic-slice recall is the honest frontier.** Strict hit@6 on messy real-staff phrasing is
  0.412 — far below the handbook-vocabulary sets. Query expansion re-words but does not yet
  re-frame intent (a question phrased around a misconception won't reach the material a
  differently-framed question would); intent-level rewriting is the Phase 14 centrepiece.
- **Run-to-run variance.** The generation API runs at a fixed default temperature and expansion
  rewrites are sampled, so borderline rows (refusal boundary, rank-6 hits) can flip between eval
  runs; committed numbers are one canonical sample.
- **Rewrite latency.** Query expansion adds one Haiku call (~0.5–1.5s) per query; `--no-rewrite`
  skips it.
- **Bare-number lookups.** A query like "requisition 27.5" with no surrounding words is not reliably
  retrieved (D31).

## Troubleshooting

- **`Embedding model mismatch` ValueError** — the index was built under a different model than the
  one configured. Re-index with `--reset`.
- **`chunk_handbook found no 'CHAPTER N' markers`** — the document isn't the handbook grammar; check
  the `--type` flag (use `--type legislation` for PART/Section documents).
- **`Relevance scores must be between 0 and 1` / "requested N results, only M elements"** — harmless
  warnings from Chroma on the tiny sample index (fewer chunks than the retrieval fan-out).
- **`eval` wrote `eval/results_partial.md`, not `results.md`** — expected for any non-canonical run
  (custom `--golden`, no held-out set, or `--skip-*`). The committed `eval/results.md` is only
  overwritten by a full canonical run.
- **No API key** — affects only `query` and the live passes of `eval`; indexing, retrieval-only eval,
  and the whole test suite run without one. On a fresh clone with no `ANTHROPIC_API_KEY` set, a run
  that *needs* a key fails cleanly with a "copy `.env.example` to `.env`" message, not a crash.
- **Guaranteeing zero API calls** — pass **both** `--skip-refusals` *and* `--skip-completeness` to
  `eval`; either alone still runs a generation pass. Note that `eval` (and `query`) load a local
  `.env` via `load_dotenv()`, so on a machine that has one, an unqualified `env -u ANTHROPIC_API_KEY`
  does *not* make the run keyless — the twin `--skip` flags do.

## Demo (≈2 minutes, needs an API key)

```bash
python scripts/build_sample_index.py                                            # 1. build the sample index
python -m src.pipeline query "What is the Meridian Folio?" --persist-dir sample_chroma_db  # 2. grounded answer + citation
python -m src.pipeline query "What is the capital gains tax rate?" --persist-dir sample_chroma_db  # 3. refusal (out of corpus)
python -m src.pipeline query "How is a Windlass Charge created?" --persist-dir sample_chroma_db --verbose  # 4. show retrieval scores + gate
python -m src.pipeline eval --golden eval/sample_golden_set.jsonl --persist-dir sample_chroma_db --skip-refusals --skip-completeness  # 5. eval
```

## Tests

```bash
python -m pytest tests/ -q
```

484 tests. All IO and models are mocked (see the `FakeEmbeddings` pattern in `tests/test_embedder.py`)
— no network access, no API key required (the suite scrubs any ambient `ANTHROPIC_API_KEY` so an
unpatched seam fails loudly rather than making a live call).

## Roadmap

- **Now (v2):** fail-closed grounding gate, appendix citations, per-source re-indexing, audit log,
  held-out evaluation, keyless CI, synthetic sample corpus — all landed (Phases 7–11).
- **Done (Phase 12):** final gate passed and the same-set, same-index v1-vs-v2 head-to-head ran —
  measurement parity on retrieval and refusals; v2 selected on the feature/verifiability record
  ([`docs/v1-v2-comparison.md`](docs/v1-v2-comparison.md), decision D42).
- **Done (Phase 13, post-v2.0 remediation):** field testing showed natural staff phrasing was
  blanket-refused; root causes were retrieval vocabulary mismatch plus a binary refusal rule, masked
  by AI-authored eval sets. Landed: Haiku query expansion with a zero-API-call degrade path, weighted
  multi-query fusion, the graded four-tier answer policy, the realistic eval slice, canonical-report
  hardening, local-first embedding load (D43–D47).
- **Next (Phase 14):** intent-level query rewriting (re-frame, not just re-word) and a synthesis
  instruction so comparison questions get organized comparative answers — measured against the
  realistic slice baselined above.
- **Beyond submission:** token-aware chunking (the 71% truncation fix), BM25 stemming, a service
  layer for a staff-facing front end, entailment-level citation checking, matter-scoped deployment,
  and multi-document indexing (the per-source sync already supports it).

## License

[MIT](LICENSE) © 2026 Ahsan Malik. The licence covers everything in this repository, **including the
wholly synthetic sample corpus**. The real, copyrighted conveyancing handbook is never distributed
here and no rights over it are granted.

## More detail

- `IMPLEMENTATION_PLAN.md` — phase-by-phase build plan and acceptance criteria.
- `docs/decisions.md` — design rationale, one entry per meaningful choice, append-only (D1–D47).
- `eval/results.md` — the canonical held-out evaluation report with full provenance.
