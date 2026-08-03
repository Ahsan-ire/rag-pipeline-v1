# 001 — Embedding-model bake-off (Phase 15)

**Status:** reviewed (plan gate READY — round 5, 4 Aug 2026; see `## Review`)
**Date:** 3 Aug 2026
**Decision ledger entry:** docs/decisions.md D57 (bake-off outcome, added when decided); diagnosis + instrument repair recorded as D54

## Problem

The corpus is embedded with `sentence-transformers/all-MiniLM-L6-v2`, whose input window is
256 wordpiece tokens (D5, D23). Measured against the live 1,470-chunk index on 3 Aug 2026:

| Quantity | Value |
|---|---|
| Stored chunks | 1,470 |
| Median chunk length | 386 BERT-tokens |
| p90 / p95 / p99 | 588 / 713 / 868 |
| Max | 2,640 |
| Chunks > 256 tokens (truncated today) | **991 (67.4%)** |
| Chunks > 512 tokens | 354 (24.1%) |

Everything past the first 256 tokens of two chunks in three is invisible to the vector arm:
those chunks are represented by their opening text alone. The D21 citation prefix is prepended
*after* every size check (`src/chunker.py:512-531`), so it consumes part of that window
without ever being counted against it.

This is an information-loss defect on its own terms: a paragraph's tail is invisible to the
vector arm, and retrieval sets the ceiling on a grounded-citation product.

**What truncation is NOT known to explain (measured 3 Aug, before the bake-off).** The
tempting story — "the realistic misses and the unmet S5 anchor are caused by truncation" — is
*not* supported by measurement and is recorded here as an open hypothesis, not a root cause.
Taking every realistic-set question's expected sections and measuring the stored chunks that
carry them:

| Bucket | Expected-section chunks over the 256-token window |
|---|---|
| Questions that MISS strictly | 18/23 = 78.3% |
| Questions that MISS both strictly and related | 8/11 = 72.7% |
| Questions that strict-HIT | 20/24 = **83.3%** |

Chunks behind *successful* retrievals are truncated at least as often as chunks behind failed
ones, so truncation does not discriminate between them. The expected gain from this phase
therefore rests primarily on **model quality** (MiniLM 42.92 vs the candidates' 53.9–61.8
MTEB v2 English retrieval), with the window removing a real but not-yet-implicated defect.
The bake-off is the instrument that settles it; nothing here presumes the answer.

**The full failure-class diagnosis (4 Aug, per-question, offline — D54).** Every answerable
realistic question was re-run at top_k=20 across hybrid/vector/bm25 plus a cached-expansion
production replay. The 12 non-hits decompose into:

| Class | Count | Evidence | What would fix it |
|---|---|---|---|
| Vocabulary gap | 3 | Same content retrieves at rank 1–6 under legal phrasing ("successive squatters…" → rank 1) and is absent from the top 20 under lay phrasing ("neighbour has been using our client's field…") | Better semantic embeddings — this bake-off's central thesis |
| Near-miss | 5 | Expected section at rank 7–19 (S5 at raw rank 9; the spouse-consent question ranks 17 raw → 1 with expansion) | A stronger model tightens ranking; a cross-encoder reranker (Phase 16) is the dedicated tool |
| Absorbed label | 2 | Expected `4.8.1.1` carried by the chunk labelled `4.8.1` — scoring artefact, text present | Instrument repair (done, below) |
| Expansion variance | cross-cutting | Cached-expansion replay: realistic 10/17 = **0.588** vs the canonical draw's 8/17 = 0.471, identical system | Measure-and-disclose acceptance (user decision, 4 Aug) |

**Instrument repair (D54, applied 4 Aug before any arm build):** the four absorbed-label rows
gained the absorbing chunk's label as an additional accepted answer (realistic
`4.8.1.1`→+`4.8.1` ×2; golden `6.3.2`→+`6.3`, `9.6.1`→+`9.6`; golden `1.7.2` left as-is — it
already hits via the present `1.7.2.3`). Post-repair offline baselines, which supersede the
committed-report figures as the baseline-arm comparison: **golden raw-hybrid strict@6
25/30 = 0.833** (was 0.800), **realistic raw-hybrid 6/17 = 0.353** (unchanged — those rows
need ranking, not labels). Structural ceilings lifted to 1.000. The arm table reports, per
arm, the per-class movement: deep-misses recovered (of 3), near-misses pulled into top-6
(of 5).

**Historical note — the absorbed-label finding (3 Aug), now repaired.** Before the D54 repair,
golden `1.7.2`, `6.3.2`, `9.6.1` and realistic `4.8.1.1` (2 rows) expected section numbers no
chunk carries — the D20 runt-merge folded each section's text into a neighbour (parents *and*
children indexed), so those rows could never strict-HIT and strict@6 was structurally
ceilinged at golden 0.900 / realistic 0.882. The repair above lifted both ceilings to 1.000
**before any arm was built**, so the instrument is consistent for the whole bake-off. The
deeper fix — chunks carrying the section numbers they absorbed, restoring citation precision —
is Phase 16 work (owner decision, 4 Aug), bundled with the refined practitioner golden set.

Two structural facts constrain any fix (both measured 3 Aug, same index):

- Cutting chunks to fit a 512-token window would re-split **36.4%** of them (+554 chunks),
  fragmenting the numbered-paragraph chunk unit that D4 chose and that the citations rest on.
- A model with a ≥1,024-token window leaves 11 chunks needing a split; at 8,192 tokens, none
  do (largest chunk = 1,256 tokens under a ModernBERT tokenizer).

## Constraints

- **The corpus never leaves this machine.** Embedding APIs (Voyage, OpenAI, Cohere, Gemini)
  are excluded by the same rule that bans committing `data/` and bans sending corpus text to
  Codex: the handbook is copyrighted and the repo is public. Candidates must be local models
  runnable through `sentence-transformers` on CPU.
- **One coordinate system, asserted, not assumed (D5, D47).** The model name is recorded in
  `<persist_dir>/embedding_model.txt` and asserted at query time; corpus and queries must be
  embedded by the same model. Loading stays local-first with a single cache-miss download
  fallback.
- **The chunker is byte-frozen this phase.** Chunk text and count must be identical across
  arms (1,470) — content-hash chunk IDs (D7, per-source since Phase 9) make this checkable and
  make the arms a clean A/B. Any other count means the chunker moved; stop.
- **Fresh-clone demo must keep working keyless** (D40, D47): no gated Hugging Face repos (a
  login wall breaks `pip install && python scripts/build_sample_index.py`), no
  `trust_remote_code=True`, and a download size a reviewer will tolerate. Licence must permit
  commercial use in an MIT repo — CC-BY-NC candidates are excluded regardless of quality.
- **Dependency policy (CLAUDE.md):** no new dependency without a stated reason and an exact
  pin. `transformers` is promoted from transitive to pinned in this phase (D53).
- **Eval integrity (D30, D31, D38, D46, D51):** the held-out set is never used for selection
  and appears in no bake-off command. Arm reports carry no chunk or answer text. Arm indexes
  and arm reports are gitignored before the first arm is built.

## Decision criteria

Judged by `eval/golden_set.jsonl` only — the tuning set, which is what fusion constants were
already selected on (D31, D46). Never the held-out set. No model's opinion is consulted.

**The exact command, run once per arm (zero API calls — both skips are required, they suppress
generation *and* disable Haiku expansion):**

```
EMBEDDING_MODEL=<model-id> .venv/bin/python -m src.pipeline eval \
  --golden eval/golden_set.jsonl \
  --realistic eval/realistic_set.jsonl \
  --persist-dir ./chroma_db_arm_<arm> \
  --skip-refusals --skip-completeness \
  -o eval/bakeoff/<arm>.md
```

Each arm's index is built beforehand with `--reset` into its own `--persist-dir` (a non-reset
rebuild is a silent no-op: chunk IDs are content hashes and therefore model-independent, so
nothing is added, the manifest is not rewritten, and stale vectors survive).

The `--golden` path must be the byte-exact string `eval/golden_set.jsonl`: `src/pipeline.py`
labels the set `tuning` only on exact equality, and a `./` prefix silently relabels it
`golden`, breaking any parser keyed to the section heading.

**Primary metric — golden strict hit@6, hybrid row** (`## tuning — retrieval ablation`),
read against the post-repair 30/30 = 1.000 structural ceiling.

**Selection rule:** the single binding statement lives in **IMPLEMENTATION_PLAN.md §Phase 15,
item 4** (disqualifiers on raw-hybrid AND production-config golden flips vs the rebuilt
baseline; among survivors highest golden strict@6 against the post-repair 1.000 ceiling; ties
to the smaller/faster model; the no-swap STOP branch) — stated once, applied mechanically, and
on any divergence the phase section wins (round-3 rule: no rule lives in two places).

**Per-class roster (the Tier-2 instrument's ground truth — `scripts/bakeoff_report.py`
computes per-arm class movement against exactly these questions):**

| Class | Realistic-set question (identifying prefix) | Expected | Raw rank today |
|---|---|---|---|
| Vocabulary gap | "The neighbour has been using our client's field…" | 13.4.8 | absent @20 |
| Vocabulary gap | "Two brothers own a farm together and one of them died…" | 5.8 | absent @20 |
| Vocabulary gap | "Can you explain what unregistered land means?" | 1.7, 1.8 | absent @20 |
| Near-miss | "What is the difference between a purchase and sale conveyance?" (S5) | 2.2.1/2.2.2/2.9 | 9 |
| Near-miss | "How far back do the title documents need to go…" | 4.5.1 | 12 |
| Near-miss | "Husband owns the house and the wife isn't on the deeds…" | 7.2, 7.2.9 | 17 |
| Near-miss | "Client is buying a house and the seller is leaving the appliances…" | 16.4.5 | 7 |
| Near-miss | "We're acting for both the buyer and their bank…" | 9.7.2, 9.8 | 19 |

(Two further rows — "registering unregistered land" and "which office do I check…" — are
single-arm gaps rescued differently per retrieval arm; tracked in the arm table as
diagnostics but not classed.)

**Diagnostics recorded but not binding:** realistic strict@6 and related@6 (compare against
the offline raw-hybrid baseline **0.353**, not the canonical 0.471 — expansion is disabled in
these runs); S5 and N4 rank movement; per-arm download size, cold model-load time, and p50/p95
per-query embed latency.

**The baseline arm is rebuilt, not reused.** Phase 15 changes the embedding stack
(`transformers` 5.13.0 → 5.14.1 pinned), and the manifest records only the model id — not
dependency versions, pooling, normalization or prompt config — so evaluating the pre-Phase-15
`./chroma_db` would compare documents embedded under one stack against queries embedded under
another and could hand a candidate an undeserved win. The baseline arm is therefore built in
this checkout like every other arm, which under MiniLM's 256-token window requires the
documented escape hatch:
`ALLOW_CHUNK_TRUNCATION=1 EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2 … --reset --persist-dir ./chroma_db_arm_baseline_minilm`.

## Design

Empty by construction — this is a bake-off brief.

**Deliberate deviation from `.claude/skills/bake-off/SKILL.md` (recorded, not accidental).**
The skill's steps 2–3 (two fresh-context agents each *authoring* a candidate design, then
cross-critiquing) do not apply here: the candidates are not designs to be written, they are
published third-party model artifacts whose properties are fixed and externally verifiable.
Generating them by agent would invent nothing and risk hallucinated model attributes. What the
skill exists to protect — *the judge is an eval set, never a model's opinion, and never the
held-out set* — is honoured exactly. The candidate sections below are therefore descriptive
inventory, and no arm is described as a favourite: the ordering is alphabetical-by-vendor and
the selection rule above is mechanical.

### Candidate A — Alibaba-NLP/gte-modernbert-base

149M params, ~298 MB, **8,192-token window**, 768 dims, Apache-2.0, ungated, no
`trust_remote_code`, **no query/passage instruction prefixes** (the same symmetric contract the
pipeline already has with MiniLM, so the query path is unchanged). MTEB(eng, v2) retrieval mean
57.0 (IBM's independently-run table), BEIR 15-task 55.19, versus MiniLM's 42.92 / 43.76.
Known gotcha: its `tokenizer_config.json` `model_max_length` is an int64 sentinel, so
`max_seq_length` is set explicitly and read back.

### Candidate B — ibm-granite/granite-embedding-small-english-r2

47M params, ~95 MB — the same download weight as MiniLM today — 8,192-token window, 384 dims,
Apache-2.0, ungated, no `trust_remote_code`, no prefixes. MTEB(eng, v2) retrieval 53.93, BEIR
50.87. Emits unnormalized vectors; the pipeline's existing `normalize_embeddings=True` covers
this, and the precheck asserts ‖v‖≈1. In the bracket because if it wins on this corpus the
fresh-clone story stays as light as it is today.

### Candidate C — Qwen/Qwen3-Embedding-0.6B

596M params, ~1.2 GB, 32k window, 1024 dims, Apache-2.0, ungated, no `trust_remote_code`.
MTEB(eng, v2) retrieval 61.83. In the bracket for one specific reason: on **AILAStatutes**
(retrieve the statutory provision governing a query — the closest public analogue to "find the
handbook paragraph that governs this"), instruction-tuned decoders score ~79 where every
encoder here scores 17–28. If that transfers to Irish conveyancing procedure it is worth
knowing. Costs: ~36× MiniLM's per-token compute, a mandatory query-side instruction prefix,
and last-token pooling (requires left padding). Behind a wall-clock cost gate — time 20 chunks
before committing to the full build; a projection over 90 minutes disqualifies it on cost with
the measured number recorded.

## Rejected alternatives

**Candidate C — Qwen/Qwen3-Embedding-0.6B: COST-DISQUALIFIED at the WS4.2 wall-clock gate
(4 Aug, measured).** 20 real chunks took **269.0 s** on this machine's CPU → projected full
1,470-chunk build **~330 minutes**, 3.7× over the 90-minute gate (and implying multi-second
single-query embed latency in interactive use). Disqualified before its arm index was built,
per the pre-approved first cut-list item. For the record, its precheck facts under the
complete snapshot: bfloat16 weights (norm drift ~2e-3 — benign numerics), true module stack
`Transformer + lasttoken-Pooling + Normalize`, repo query instruction verified and
version-pinned into `MODEL_SPECS`, semantic separation healthy (cos related 0.68 vs
unrelated 0.12). The AILAStatutes hypothesis (instruction-tuned decoders dominating
statute-style retrieval) remains untested on this corpus — a Phase 16+ candidate only if
GPU/ONNX acceleration changes the cost calculus.

**Operational finding worth keeping (4 Aug):** two of the three candidate downloads produced
**partial HuggingFace snapshots** (weights + tokenizer but no `modules.json` /
`config_sentence_transformers.json` / `1_Pooling/config.json`), under which
sentence-transformers silently falls back to its default module stack — mean pooling, no
prompts — i.e. a *different model* that loads without error. The gte arm's first index build
ran under this degradation and was killed and rebuilt; the precheck's module listing is what
caught it (gte cosines changed from 0.76/0.29 to 0.80/0.38 once the true CLS-pooling stack
loaded). The precheck now doubles as the completeness check: module stack, prompts, window,
norms, and separation are asserted per arm before any build.

Ruled out before the bracket was set (3 Aug research pass; MTEB figures recomputed from the
official results repo, attributes read from the HF configs):

- **google/embeddinggemma-300m** — HF `gated: manual`; an unauthenticated config fetch returns
  "Access to model ... is restricted ... Please log in." A reviewer's fresh keyless clone gets
  a 401. Ungated mirrors redistribute under the same non-OSI Gemma licence.
- **jinaai/jina-embeddings-v3, v5-text-nano, v5-text-small** — CC-BY-NC-4.0; v4 is
  research-only (Qwen RESEARCH LICENSE). v5-nano posts the best small-model legal-subset score
  measured (3-task AILA/LegalSummarization mean 52.33) and is excluded on licence alone.
- **Snowflake/snowflake-arctic-embed-m-v2.0** — best licence-clean legal-subset score (43.73)
  but requires `trust_remote_code=True` (its `gte` arch is not native in transformers) and is
  1.22 GB. **arctic-embed-l-v2.0** needs no remote code but is 2.27 GB.
- **BAAI/bge-\*-v1.5, intfloat/e5-\*, MongoDB/mdbr-leaf-ir** — 512-token windows: they leave
  24–38% of chunks truncated and would force the 36.4% re-split this design exists to avoid.
  e5 is additionally superseded (e5-large-v2 at 49.31 loses to bge-base at 54.75 for 3.6× the
  compute).
- **nomic-embed-text-v1.5** — 8k window and Apache-2.0, but 47.97 retrieval mean, below
  bge-base and below the 47M granite-small in this bracket.
- **IEITYuan/Yuan-embedding-2.0-en** — highest measured sub-1.5B score (70.69, above models 13×
  its size) but ~7 likes and no independent scrutiny; benchmark-overfit risk not worth a
  portfolio piece's retrieval floor.
- **Larger models generally** (Qwen3-4B/8B, gte-Qwen2-1.5B) — multi-GB weights, outside a
  laptop-CPU budget.

## Review

Plan gate, 3 Aug 2026. Both legs ran: fresh-context **plan-auditor** (31 findings: 6 blockers,
15 degrades-result, 10 cosmetic) and second-vendor **Codex** read-only critique (8 findings:
1 destructive-bug, 4 further P1, 2 P2, 1 P3). Verdict: **REVISE → re-gate**. Every finding was
verified against the code before disposition; the ones that changed the plan are below.

### Blockers accepted (plan changed)

| # | Finding | Verified how | Disposition |
|---|---|---|---|
| C1 | The guard would fire *after* `--reset` had already wiped the index: `clear_store` is `src/pipeline.py:118`, `sync_documents` is `:123`. A failing guard would leave **no index at all**. | Read `src/pipeline.py:111-125`. | **ACCEPT.** Guard becomes a *preflight* in `index_documents`, before `if reset:`, in both branches; `add_documents`/`sync_documents` keep their own guard for callers that bypass the CLI. Tests must prove `clear_store` and `sync_documents` are both uncalled when the preflight fails. |
| A2 | Landing the guard while the default model is still MiniLM breaks `scripts/build_sample_index.py` and therefore CI, for the whole WS3→WS5 window. | Measured the 16 sample chunks with the real MiniLM tokenizer: `[184…234, 258]` — one chunk at **258 > 256**. | **ACCEPT.** Resequenced: the guard lands *with* the winner adoption, after the default flips. Under MiniLM the guard failing is correct behaviour, not a bug — `ALLOW_CHUNK_TRUNCATION=1` is the documented path for any MiniLM rollback re-index. |
| A1 | `git check-ignore -v chroma_db_arm_gte` reports "not ignored" for a directory-only pattern when the directory does not exist yet — the corpus-safety proof returns a false negative every time. | Reproduced: exit 1 on the bare name; `chroma_db_arm_gte/probe.txt` correctly reports `.gitignore:29`. | **ACCEPT.** Verification command becomes a probe path inside a real directory. |
| A3 / brief §5 | No defined outcome when no arm beats the baseline cleanly — WS5–WS8 were written unconditionally around "the winner". | Golden hybrid strict@6 is 24/30 today; D50 is the recorded precedent of a single control flip disqualifying an aggregate-superior change. | **ACCEPT.** Selection rule gains step 5 (baseline survives → "no model swap" negative result; user decides whether to widen the bracket). |
| A4 | The cut list offered "chunk-token provenance disclosure" as cut #3 while the acceptance block *required* it — the phase could be simultaneously on-plan and unacceptable. | Both statements are in `IMPLEMENTATION_PLAN.md`. | **ACCEPT.** Provenance disclosure removed from the cut order; the cut list now offers only genuinely optional work. |
| A5 | Acceptance criteria 4/5/7 grep `eval/results.md`, which a non-canonical run never rewrites — `grep "strict hit@6 = 20/20"` passes **today**, before any Phase 15 work exists. | Ran the grep on the committed July report: matches `eval/results.md:38`. | **ACCEPT.** Every report-reading criterion is now gated on a freshness precondition: the report's provenance must name the winning model *and* a git sha from this branch. |
| A6 | Implementation was ordered *before* the gate that authorises it (WS1 items 1–5 ahead of the gate at item 6), against `.claude/skills/plan-gate/SKILL.md:48-50` and `docs/harness.md:51`. | `git status` confirmed `.gitignore`, `CLAUDE.md`, `requirements.txt` already modified. | **ACCEPT (process).** Correct ordering for the remainder: no further code lands until this gate reads READY. The three landed edits stay — all reversible, no arm built, no model swapped, and the `.gitignore` entry is a safety precondition that must precede any arm build — but the ordering error is recorded rather than argued away. |

### Codex P1s accepted (plan changed)

| # | Finding | Disposition |
|---|---|---|
| C2 | The baseline arm reused the pre-Phase-15 `./chroma_db`, mixing documents embedded under `transformers` 5.13.0 with queries under 5.14.1; the manifest records only the model id, so nothing would catch it. | **ACCEPT.** Baseline arm is rebuilt in this checkout under the new stack (see *Decision criteria*), with `ALLOW_CHUNK_TRUNCATION=1`. |
| C3 | S5/N4 use existential matching (`any(expected == retrieved)`, `src/evaluator.py:394,401`), so a comparison question can "HIT" having retrieved only one side of the contrast — contradicting the Phase 14 rubric that requires both sides. | **ACCEPT.** The S5/N4 criteria now require a strict-or-related match for *each* role group (2.2.1 and 2.2.2), read from `retrieved_sections` rather than the evaluator's existential flag. |
| C4 | Selection measured raw hybrid only; production runs expansion + the intent arm, so an arm could be clean on raw queries and regress under what actually ships. | **ACCEPT.** Added as binding selection step 2, replayed deterministically from the committed expansion cache (no API calls). |
| C5 | `scripts/w_sweep.py:40-53` calls `expand_query` live for any question lacking a `STATUS_LIVE` cache entry — the "zero-API" W sweep is conditional, not guaranteed. | **ACCEPT.** `build_cache(..., offline_only=True)` raises instead of calling out; the WS6 decision rule also restores D50's requirement that N4 stay HIT. |
| C6 | "Recorded root cause" overstated the record: `docs/decisions.md:485` lists truncation as a tuning *suspect*, and D50 attributes S5's behaviour to expansion sampling. Also "breaks D4" overstated — D4 explicitly permits splitting oversized paragraphs. | **ACCEPT, and strengthened with measurement.** Reframed as a preregistered hypothesis; the 3 Aug truncation-discrimination measurement (see *Problem*) shows it does **not** discriminate hits from misses, and that is now stated in the brief before the bake-off rather than discovered after it. |

### Accepted, smaller (folded into the plan)

`ALLOW_CHUNK_TRUNCATION` joins `EMBEDDING_MODEL` in the `tests/conftest.py` autouse env scrub
(A16 — an exported flag would silently turn the guard's raise-tests into warning-tests);
`MODEL_SPECS` gains an asserted invariant that `max_seq_length` agrees with `context_window`,
since the guard checks one and truncation happens at the other (A17); the report parser becomes
a committed, unit-tested script because its output is the phase's binding evidence (A19, C7);
the arm-completeness check reads the **store's** vector count, not the chunker's printed chunk
count, which is model-independent and cannot fail (A23); held-out absence becomes a property of
a checked-in runner that refuses `--heldout` rather than "shell-history discipline" (A25, C7);
criteria 5/14 gain real commands; the brief's `Status` follows the documented lifecycle (A21);
the phase-gate hygiene list gains the arm directories (A22); the `transformers` cold-cache
message assumption gets an explicit re-verification step under 5.14.1, and the version note in
`src/embedder.py:41-44` is updated with it (A14); CI runs on the pushed branch *before* the
live canonical run, so an ubuntu-vs-macOS smoke flip is caught before API budget is spent (A18);
the exact-string requirement for `--golden` is documented (A27); "the suite never imports
transformers" is corrected to "never instantiates a model or downloads a tokenizer", since
`src/embedder.py:12` imports `langchain_huggingface` at module scope (C8).

### Rebutted, with reason

- **A7 — "acceptance criterion 2 is a tautology."** Correct as stated, and deliberately so: it
  restates the selection rule, so it can only be satisfied by construction. It is retained as a
  *disclosure* requirement (the per-question flip list must be committed to this brief), and the
  falsifiable criteria are the realistic bar, the S5/N4 coverage checks and the held-out guard.
- **A15 — ".env import order."** Real (`scripts/w_sweep.py:19-21` imports `src.generator`, hence
  `load_dotenv`, before `src.embedder`), but the correct response is a documented prohibition —
  `EMBEDDING_MODEL` is a process-environment variable and must never be placed in `.env` — not a
  refactor of import order in a phase whose premise is measurement stability.
- **A20 — "the bake-off skips skill steps 2–3."** Rebutted with rationale recorded in `## Design`
  above: the candidates are external artifacts, not authored designs.
- **A24 / A31 — irreversibility and schedule.** Partially accepted: the rollback path is now
  documented as requiring `ALLOW_CHUNK_TRUNCATION=1`, and re-index wall-clock is measured for
  every arm rather than only Qwen. The date range in the phase header is indicative, not a
  commitment; this is post-deadline work.
- **A29 — "pin `torch` too."** OUT-OF-SCOPE. Real dependency-hygiene point, unrelated to the
  embedding swap; goes to the follow-on backlog rather than widening this phase.
- **A13's premise** was accepted and *measured* rather than argued (see C6).

### Round 2 (revised plan re-gated, 3 Aug)

Fresh critics were re-run on the revised plan, as the gate requires. Codex round 2 confirmed
the truncation-premise reframing, the rebuilt-baseline rationale and the guard-ordering fix as
correctly revised, and found five further defects — all **accepted** and applied:

1. **The offline-only W-sweep hardening was scheduled after the step that needs it.** The
   revision added the production-config replay to selection (WS4) but deferred
   `offline_only=True` to WS6, leaving the *binding* selection step able to spend live API
   calls. The hardening now lands before the first replay.
2. **The freshness precondition still passed on the July report.** A label-existence grep
   proves nothing (the renderer always emits those labels), and "a sha on this branch" is
   equally weak because the July commit is an *ancestor* of this branch. The check now requires
   the report's sha to be reachable from HEAD, that commit to contain `assert_chunks_fit_window`,
   and the embedding-model line to name the exact winner.
3. **The both-role coverage check could be satisfied by a single generic parent.**
   `_sections_related` (`src/generator.py:292-295`) is *symmetric* prefix matching, so one
   retrieved `2.2` is "related" to both 2.2.1 and 2.2.2 and would tick both roles at once. The
   criterion is now equal-or-descendant (`s == group or s.startswith(group + ".")`), which
   excludes the generic ancestor; the WS6 W-rule inherits it.
4. **The no-swap branch must stop, not proceed.** Under MiniLM the guard cannot land without a
   standing truncation exception, so a no-swap outcome now halts for user disposition before
   the guard or the sample-index rebuild — rather than landing them anyway.
5. **Cross-document inconsistencies** (provenance still offered as a cut in one file, "favourite"
   framing surviving in two, the corrected transformers-import claim not propagated, scratchpad-
   vs-committed parser, and unconditional "solved by window" doc instructions that contradict a
   no-swap outcome) — all reconciled across the three documents.

### Round 3 (verdict: still REVISE — consolidation, 3 Aug)

A second re-gate found the v2 revision failing in nine further blocking ways. The pattern
mattered more than any single item: the same rules were restated in three documents and drifted
apart between edits, and several round-1 dispositions were recorded as "folded into the plan"
without ever becoming a plan step. Two structural corrections:

- **One normative source.** `IMPLEMENTATION_PLAN.md` §Phase 15 now owns the selection rule and
  the acceptance block (CLAUDE.md already designates it the working spec); the workstream plan
  carries execution mechanics, and this brief carries the bake-off contract and these logs. No
  rule appears in two places.
- **Instrument gaps are build steps, not assumptions.** The production-config disqualifier and
  the both-role-group coverage test were written as if instruments existed for them; neither
  did (`scripts/w_sweep.py` compares W arms within one index and matches existentially). They
  are now explicit work in `scripts/bakeoff_report.py` and `scripts/w_sweep.py`, landing before
  the selection step that depends on them.

Also fixed: the no-swap branch no longer lands a guard that cannot land under MiniLM; the
negatives count is scoped to held-out + realistic (the naive latch returned 16 on any report);
the hygiene check no longer filters out the untracked line that a gitignore failure produces;
p95 latency is required by neither acceptance nor cut list simultaneously; the offline report's
per-question section is keyed to its real `hybrid+rewrite` label; the branch is pushed and the
PR opened before the canonical run, so "CI green first" is achievable; the production index is
rebuilt only after the sample index and CI pass; and a held-out regression gets the same
STOP-for-user-disposition branch as a missed realistic bar.

**Open question deliberately escalated rather than decided:** whether criteria 3, 6 and 7
should be hard gates at all. Each is measured once, on a canonical run whose expansion sampling
D50's addendum records as moving golden 27/30 → 26/30 and S5 from related-rank-2 to MISS — on
a slice where one question is 5.9 points. The gate can be reformulated as "measure and
disclose" rather than "pass or fail", which is what the evidence supports; that is a product
decision for the owner.

### Round 4 (owner decisions + diagnosis, 4 Aug — gate unblocked)

The owner reviewed the diagnosis and decided all open questions: **(1)** acceptance for the
canonical metrics is **measure-and-disclose** (Tier 2 of the acceptance block in
IMPLEMENTATION_PLAN.md §Phase 15; deterministic bake-off rules stay hard gates in Tier 1);
**(2)** the absorbed-label rows are repaired **now** by accepting the absorbing label
(applied — see *Problem*), with the metadata-level fix (chunks recording the section numbers
they absorbed) deferred to Phase 16; **(3)** re-chunking the corpus for four labels is
rejected; **(4)** Phase 16 carries the refined practitioner golden set (designed around the
vocabulary-gap findings), the absorbed-sections metadata fix, and the cross-encoder reranker
targeting the near-miss class. Diagnosis and repair recorded as D54; the transformers pin as
D53; remaining entries renumbered D55–D58 in the phase section.

### Round 5 (verdict: READY, 4 Aug)

The round-3 re-gate ran both legs fresh on the consolidated plan. **Both critics' substantive
checks came back clean**: no data-handling violations (verified live against the working
tree), no unexecutable Tier-1 gates, no new cannot-fail gates, no missing steps blocking
WS2–WS4, and the pin verified installed. Every remaining finding was document-consistency —
places where an appendix recorded a correction that the body text it corrected still
contradicted. All were fixed at the source: the normative item 5 now carries the
reversible-first ordering explicitly (sample index → local greps → **push + PR + CI green** →
only then the production `--reset` → W re-sweep → canonical run) and the single binding
W rule (D50's recorded rule re-run, N4-both-groups); the execution checklist's criteria 3–7
are marked Tier-2 measured-and-disclosed and its resolved-question note updated; the stale
pre-repair ceilings are past-tensed everywhere; the WS8 decision map matches the committed
ledger (D53 pin, D54 diagnosis, D55 seam, D56 guard, D57 bake-off, D58 W); the per-class
roster above and its computation in `scripts/bakeoff_report.py` are explicit build steps; the
`ALLOW_CHUNK_TRUNCATION` conftest scrub and the `max_seq_length == context_window` invariant
are named in the phase Tests block; the phase-gate skill's hygiene list now includes the arm
directories. **Verdict: READY — implementation may begin.** Basis for closing without a
fourth fresh-critic round: three rounds ran with fresh critics each time, severity declined
monotonically (destructive code bug → cannot-fail criteria → document drift), and this
round's fixes are mechanically verifiable text reconciliations, each checked by grep after
application.

## Outcome

**NEGATIVE RESULT (4 Aug 2026, D57): no candidate survives selection — the baseline wins by
default and the phase outcome is "no model swap."** Full evidence: `eval/bakeoff/` (gitignored
artifacts; manifest with sha256s) and the tables below.

| Arm | Golden S@6 | Golden R@6 | Realistic S@6 | Realistic R@6 | p50 embed | Disposition |
|---|---|---|---|---|---|---|
| baseline (MiniLM, rebuilt) | 0.833 | 0.900 | 0.353 | 0.588 | 11.7 ms | **survives by default** |
| gte-modernbert-base | 0.833 | 0.933 | 0.294 | 0.529 | 26.1 ms | DISQUALIFIED — 2 golden HIT→MISS flips (raw AND production config) |
| granite-small-english-r2 | 0.900 | 1.000 | **0.118** | 0.412 | 26.7 ms | DISQUALIFIED — 1 golden HIT→MISS flip (raw AND production config) |
| Qwen3-Embedding-0.6B | — | — | — | — | — | COST-DISQUALIFIED (measured ~330 min projected build vs 90-min gate) |

The disqualifying flips (verified in the underlying reports, not just the parser): both
candidates lose *"What rules govern a solicitor giving an undertaking to a lender…"* (a
baseline rank-6 HIT, absent from both candidates' top-20); gte additionally loses *"What
searches should a purchaser's solicitor carry out before completion…"* (baseline rank 3).
Both persist under the production-config replay, so this is not an expansion artefact.

What the aggregate numbers would have hidden — the reason the per-question rule exists (D50):
granite's +0.067 golden aggregate came with a **realistic-slice collapse to 0.118** (2/17,
vs baseline 0.353), a per-class regression (2/5 near-misses recovered vs baseline's 3/5,
losing the stamp-duty question), and N4 both-role coverage regressing from yes/yes to no/no.
gte was baseline-equal on golden aggregate while regressing realistic and N4 coverage.
**Neither candidate recovered a single vocabulary-gap question (0/3 for every arm)** — the
lay-phrasing failure class is untouched by stronger general-purpose embedders on this corpus.

**Post-verdict mechanism probe (4 Aug, per-arm vector/bm25/hybrid ranks on the flipped
questions):** the undertaking flip is NOT an embedding failure — **all three models rank the
expected chunk #1 in vector-only mode**, and BM25 misses it entirely in every arm; the loss
happens in rank fusion, where BM25's pile of confident wrong candidates demotes the vector's
top pick to the top-6 boundary (baseline lands exactly at 6; the candidates' slightly
different candidate tails push it just past). gte's second flip is a genuine semantic
regression (expected section absent from its top-20 vector list where baseline ranks it 5).
So the disqualifications decompose into: one real semantic regression (gte), one
fusion-boundary knife-edge that punishes any perturbation of a rank-6 hit (both), and the
realistic-slice collapse (granite, the substantive failure). Two implications recorded for
Phase 16: the fusion stack (RRF constants, pool 12, W) was tuned under MiniLM across Phases
3–14, so any candidate faces a co-adapted incumbent — a future bake-off could compare
candidate+retuned-fusion as systems; and BM25 contributing nothing on several golden
questions strengthens the reranker case independently of embeddings.

Interpretation, stated plainly: the MTEB quality story (MiniLM 42.9 vs candidates 53.9–57.0)
**did not transfer to this corpus as system-level wins**. The D54 diagnosis anticipated this possibility —
truncation was already shown not to discriminate hits from misses, and the vocabulary-gap
class was always a hypothesis. The per-question controls did exactly what they were built
for: an aggregate-only comparison would have shipped granite and silently traded two field-
test failure classes for a leaderboard number.

Consequences per the pre-registered no-swap branch: `DEFAULT_EMBEDDING_MODEL` stays MiniLM;
the WS3 truncation guard does NOT land (it cannot, under MiniLM's 256-token window, without a
standing truncation-exception policy — the owner's call); no production re-index, no sample
regeneration, no CI cache-key change, and **no canonical API run is warranted** (production
config is unchanged, so the committed eval/results.md remains accurate). What stands: the
instrument repair (D54), the model-config seam (D55) making any future bake-off a
one-command-per-arm exercise, the bake-off instruments themselves, the transformers pin (D53)
and hygiene, and this negative result. Widening the bracket (arctic-l-v2.0 at 2.3 GB is the
remaining licence-clean 8k-window candidate; Qwen under GPU/ONNX), adopting a truncation
exception to land the guard anyway, or redirecting Phase 16 at the failure classes directly
(reranker for near-misses; expansion/vocabulary work for the gap class) are the owner's
disposition options.
