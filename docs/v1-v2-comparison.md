# v1 vs v2 — same-set, same-index head-to-head

The submission decision between `v1.0-baseline` and v2 (D42) is grounded in this comparison.
Everything in the two **Head-to-head** tables is a same-set, same-basis measurement; everything
else is labeled for what it is.

## Why this comparison is valid (method)

- **Same frozen held-out set for both versions:** `eval/heldout_set.jsonl`, sha256
  `601a81c0a3e36aa5d90afb7904fdebad7704d3fff506871c0b9032d7576dcfe6`, n=28 (20 answerable +
  8 near-domain negatives), authored after v1's retrieval constants froze and never used for
  tuning (D33). Verified byte-identical in the `v1.0-baseline` checkout (same git blob; sha256
  re-checked at run time in both trees).
- **Same physical index for both versions:** the current 1,470-chunk store
  (`all-MiniLM-L6-v2`, BM25 sidecar). One disclosure: this store was *rebuilt by v2 code* in
  Phase 9, which changed chunk **IDs** to source-scoped IDs — but the chunking, chunk text, and
  embedding model are unchanged from v1 (D39 recorded a no-go on re-chunking), and both
  versions' own eval reports record the same 1,470-chunk count. The comparison is "same index"
  in the logical sense: identical chunks, identical vectors, identical BM25 sidecar
  (`src/bm25_index.py` is byte-identical between the two versions).
- **v1 measured by v1's own evaluator**, run from a clean `git worktree` of tag
  `v1.0-baseline` (commit `5748643`) against the shared store via a symlink. v1 reports a
  single hit@k cutoff per run, so hit@{1,3,6} took three runs (`--top-k 1/3/6`); the k=1 and
  k=3 runs were retrieval-only (offline), and the k=6 run included v1's live refusal pass.
- **Both retrieval arms were verified alive before any v1 run** (v1's retriever silently falls
  back to one-arm retrieval on backend errors): the BM25 sidecar loaded under v1 code and
  returned probe candidates, the vector store opened with exactly 1,470 chunks and a matching
  model manifest, and every run's stderr was scanned for error/fallback lines (none occurred).
- **False refusals (v1):** v1's harness has no false-refusal metric, so the cell was measured
  by a throwaway driver replaying v1's *own* default answer path — `retrieve(q, top_k=6)` →
  `generate_with_sources(q, results)["answer"]` → `is_refusal(answer)`, exactly the closure
  inside v1's `evaluate_refusals` — over the 20 answerable held-out questions. This is the same
  definition v2's completeness pass uses. Policy: one retry per question, errors excluded from
  the denominator, run valid only with 20/20 successful generations. Outcome: 20/20 successful,
  0 errors.
- **Read the rows accordingly:** retrieval rows are deterministic given the frozen index;
  refusal rows are single-run samples of a nondeterministic generator.
- One cosmetic note for anyone re-running this: v1's report formatter hardcodes a
  "tuning set" description string in its set line, but the scored file is recorded (and was
  verified) as `eval/heldout_set.jsonl` in the same report's provenance block.

## Head-to-head: retrieval (held-out, hybrid, same index, n=20 per cell)

| Metric | v1.0-baseline | v2 |
| --- | --- | --- |
| strict hit@1 | 18/20 = 0.900 | 18/20 = 0.900 |
| strict hit@3 | 19/20 = 0.950 | 19/20 = 0.950 |
| strict hit@6 | 20/20 = 1.000 | 20/20 = 1.000 |
| related hit@1 | 18/20 = 0.900 | 18/20 = 0.900 |
| related hit@3 | 19/20 = 0.950 | 19/20 = 0.950 |
| related hit@6 | 20/20 = 1.000 | 20/20 = 1.000 |

## Head-to-head: refusal behaviour (held-out, live generation)

| Metric | v1.0-baseline | v2 |
| --- | --- | --- |
| Near-domain negatives correctly refused (n=8) | 8/8 = 1.000 | 8/8 = 1.000 |
| False refusals on answerable questions (n=20) | 0/20 | 0/20 |

False-refusal rate 0.000 for both (v2's report adds the 95% CI 0.000–0.161; with n=20 a zero
count is consistent with a true rate as high as ~16%).

**The measured result is parity.** On the frozen held-out set against the identical index, v2
retrieves and refuses exactly as well as v1 — which is what D39 predicted when it deferred
re-chunking: the v2 track deliberately did not touch retrieval quality. What v2 changed is
everything *around* the answer, below.

## Feature comparison

| Capability | v1.0-baseline | v2 |
| --- | --- | --- |
| Grounding gate | absent — ungrounded answers shown with a warning | fail-closed gate; answers withheld unless every citation verifies (`src/grounding.py`, D35/D36) |
| Appendix citations | absent — appendix locators unciteable | first-class citation grammar across chunker/generator/validator (D34) |
| Index lifecycle | full-reset re-index only | source-scoped IDs + per-source `sync_documents` add/update/delete (D37) |
| Audit trail | none | JSONL event log with per-query provenance (`src/audit.py`, D36) |
| Eval-report provenance | git sha + chunk count | + set sha256s, dirty flag, model records, matching definition, canonical-run guard (D38) |
| Headline methodology | related-match rate on the tuning set | strict hit@6 on a frozen held-out set, Wilson CI, ablation (D33/D38) |
| Answer-quality measurement | refusal accuracy only | two-sided refusals, citation-grounded fraction, sentence coverage, false-block rate, experimental judge (D38) |
| CI | none | keyless GitHub Actions: 400-test suite + synthetic-sample retrieval smoke (D40) |
| License | none | MIT, covering code and the synthetic sample corpus (D41) |
| Sample corpus / fresh-clone demo | none — corpus required | wholly synthetic sample handbook + keyless quickstart (D40) |

v2 also measures answer quality that v1's harness cannot (held-out, from `eval/results.md`,
**not comparable** — v1 has no completeness or judge pass): citation-grounded fraction
89/89 = 1.000, sentence-citation coverage 89/101 = 0.881, false-block rate 0/20, LLM-judged
faithfulness 0.995 *(experimental, same-family judge)*.

## Narrative metrics — NOT head-to-head

These numbers use a different set or basis and sit here for history, not comparison:

- v1's advertised headline was **0.900** — a *related*-match rate on the *tuning* set (n=30),
  i.e. the set used to select the fusion constants. The 11 Jul external critique called this
  out, and rebuilding the eval around it is what the v2 track was for (D38).
- v2's tuning-set numbers under the honest metric: strict hit@6 0.800, related 0.900 (n=30).
- Only the two tables above are same-set, same-basis measurements. Comparing v1's tuning-set
  related 0.900 against v2's held-out strict 1.000 would repeat the original sin in reverse;
  with n=20 the held-out headline's CI (0.839–1.000) spans several questions' worth of rate.

## Recommendation

**Submit v2** — recorded as D42 in `docs/decisions.md`. The measurement shows v2 gave up
nothing on retrieval or refusals; the feature table is the value: verifiable grounded answers,
auditability, honest evaluation, and a reproducible keyless surface. `v1.0-baseline` remains
the tagged fallback (`git checkout v1.0-baseline`).

## Provenance

- **v2 numbers:** `eval/results.md`, canonical run at git `d0c93e1` (clean tree), judge on,
  zero generation and zero judge errors; committed in `8b70e76`.
- **v1 numbers:** worktree of tag `v1.0-baseline` (peeled commit `5748643`); the only
  deviations from the tagged tree were the store symlink and two untracked throwaway scripts
  (arm check + false-refusal driver) — no v1 source file was modified. Run with the main
  repo's venv (`requirements.txt` is unchanged between the two versions).
- **Held-out integrity:** sha256 verified identical in both checkouts before any run; the
  per-run report copies live outside the repo and are not committed.
