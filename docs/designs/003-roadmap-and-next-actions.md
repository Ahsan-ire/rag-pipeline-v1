# 003 — Post-Phase-15 plan: close-out, integrity hotfix, global harness, eval foundations, agentic production path

**Status:** **v3 APPROVED by the owner on 9 Oct 2026.** Approved items: the PR #19 "go" and the global harness (Track B). Q1–Q8 answers are pending.
- v1 (morning, 8 Oct) went to the owner; v2 recorded the owner's decisions.
- v3 reconciles the Codex `gpt-6-astra` @ xhigh co-developer + adversarial pass (verdict
  REVISE: 4 BLOCKER, 11 MAJOR, 2 MINOR; all accepted). Raw review:
  `docs/designs/003-review-astra-2026-10-08.md`.
- Nothing past Track A is actioned until the owner approves v3 and answers §1.2.

**Date:** 8 Oct 2026 (snapshot below at 18:15 IST)
**Decision ledger entry:** D59 (landed, plus addendum); D60 is reserved for the
third-party lane / global harness (002-v2); D61+ come from this plan.
**Supersedes:** `docs/work-state.md` §2–§3 as the forward plan.

---

## 0. Snapshot — 8 Oct 2026, 18:15 IST

| Item | State |
|---|---|
| `main` | `51be633` (18 Jul), `v2.1.1`, CI green |
| Phase 15 | **Merged 9 Oct** (PR #19 → `85a4283`), tagged **`v2.2.0`**. Suite 633 passed on main. |
| Production retrieval | Unchanged: offline ablation rows byte-identical to 5 Oct |
| Real use | Owner since mid-July, as a "first-line sweep" with citations checked. Colleagues have seen it but not used it. |
| Machine config (done 8 Oct) | `cleanupPeriodDays: 36500` (sessions kept). GitHub PAT moved from plaintext into the Keychain (`github-mcp-pat`), exported only in interactive, non-worker shells (`~/.zshrc`); both MCP clients verified; plaintext backup copies redacted. **Rotation still required** (owner action, §1.1). |
| Harness | Multi-vendor workers and hooks exist only inside `fe1-companion` / `jobradar`. Nothing global yet → Track B. |
| Golden-set prep | 24 candidate rows from Tutorials 1 and 7 (gitignored `data/tutorials/`). Natural phrasings miss the top 6 in 20/66 cases; modelled questions miss in 1/22. |

## 1. Decisions

### 1.1 Already decided (8 Oct)
- **D59 / D60 numbering.**
- **Truncation guard MOOT.**
- **Track A dispositions.**
- **Licence:** proceed on the assumption that internal use is covered; the owner is emailing
  the Law Society (questions in §7).
- **Tutorial material:** use it as an eval source; the manual is **not** indexed (it serves
  as an answer-key aid).
- **No fine-tuning in Phase 16.**
- **Model routing:**
  - dev subagents: `sonnet` = Sonnet 5.5 (implementation; alias verified 9 Oct); `haiku` =
    **Haiku 5.5** (`claude-haiku-5-5`, released 7 Oct; the alias defaulted to 4.5, so it is
    remapped via `ANTHROPIC_DEFAULT_HAIKU_MODEL` in `~/.claude/settings.json` from the next
    session); `opus` (specialist judgment);
  - research: Sonnet fetches and reads **full** sources (not WebFetch summaries); Opus
    judges (owner rule, 9 Oct);
  - third-party workers: Grok primary; DeepSeek / GLM as fallbacks;
  - Codex: `gpt-6.1-sol` for gates; `gpt-6-astra` for architecture, at most 2 calls per 5 h.
- **Product model:** stays on `claude-sonnet-5` until a dedicated, separately measured
  migration (Phase 17b).
- **Owner action outstanding:** **rotate the GitHub PAT.**
  1. GitHub → Settings → Developer settings → Fine-grained tokens → *Regenerate*.
  2. Run `! security add-generic-password -U -s github-mcp-pat -a "$USER" -w` (it prompts with
     hidden input).
  3. Open a new terminal.

  No config change is needed.

### 1.2 Owner answers (9 Oct 2026)
| # | Question | Answer, and what it changes |
|---|---|---|
| Q1 | Answer scope | **Substantive answers, built as two modes.** The scope grows over time: Tailte Éireann registration guidance, courts.ie, statutes, other Law Society manuals. (a) **Handbook mode** (default, today's behaviour): grounded in the Conveyancing Handbook only; the gate and refusal stay as they are. (b) **Research mode** (opt-in toggle): handbook plus the model's general knowledge plus **up-to-date authoritative online sources**, as two-way verification. It flags where the handbook may be outdated, or where the law has moved, and ends with a protective disclaimer. Every claim carries a **provenance label**: `[Handbook ¶x, p.y]` (gate-verified) / `[Source: URL, date accessed]` (web, allow-listed authoritative domains) / `[General knowledge — not verified]`. Labels are never blended. New roadmap item: Phase 19, research mode. Ledger: **D61**. |
| Q2 | `PARTIALLY_VERIFIED` display | Applies to **Handbook mode**: keep showing it with the unverified locators named, and add the uncited-statement flag and the truncation withhold (H1–H3). Research mode uses the per-claim labels from Q1. |
| Q3 | Who labels and seals | The **owner** labels. A colleague gives a second opinion on refusal and ambiguous rows. ("Labelling" = writing the answer key: which handbook paragraphs answer each test question, or whether it should be refused. "Sealing" = locking a share of those questions away, unseen and never used for tuning, so they work as an honest final exam.) |
| Q4 | Bars | **Accepted for Handbook mode:** ≤1% unsupported claims on the adjudicated set; p95 ≤ 20 s; ≤ €0.05/query. Research mode will be slower and costlier, so it gets its own bars in Phase 19. |
| Q5 | Operator | The **owner**. Backup to be named later. |
| Q6 | Non-agentic end state? | **No:** the owner wants agentic development to proceed at Phase 21. It stays evidence-gated for *production use* (it ships when it beats single-pass on sealed families), but it is planned, not optional. Research mode (Phase 19) is its natural first host: handbook retrieval, statute lookup and allow-listed web search as tools. |
| Q7 | Tutorial-derived paraphrases public? | **Private for now.** Commit them later once the Law Society replies. A research subagent maps the legal landscape and the Law Society's own terms before the email is sent. |
| Q8 | Harness as its own repo | **Yes:** `~/ClaudeCode/harness` (in progress). |

Also decided 9 Oct:
- the integrity hotfix (§3) **proceeds**, and Track B **proceeds**;
- a future **legal-data partnership** (a provider of verified Irish legal data, to strengthen verification beyond the open web) goes on the 23+ track; the owner will share the business-plan research.


---

## 2. Track A — close Phase 15 (**COMPLETE: merged and tagged `v2.2.0` on 9 Oct**)

A0–A9 are done (push; blockers 1–6; Codex C1–C7 dispositions; re-review residuals in
`f1a0644`; work-state corrected; PR #19 opened; CI green).
**DONE 9 Oct:** merged and tagged `v2.2.0`. This plan, the astra review and `AGENTS.md` are
committed on `plan-003-roadmap`. Deleting the merged branches (O10) still awaits an owner OK.

---

## 3. Integrity hotfix (H) — **next, before Track B** (astra B1)

The tool is in real use, so known answer-integrity defects come before tooling. Branch
`integrity-hotfix`, release `v2.2.1`. It is small and keyless-testable.

| # | Change |
|---|---|
| H1 | Keep the full response object instead of discarding it (`src/generator.py:365`). Record `stop_reason`; **withhold** any answer that stopped on `max_tokens` (fail closed with a clear message); audit it. |
| H2 | Runtime **uncited-statement detection**: substantive sentences with no locator are listed under the answer as "not backed by a citation". The gate outcome gains that signal (it can downgrade only, never upgrade). |
| H3 | **One display-policy function**: every gate outcome maps to exactly one rendering, used by the CLI now and the API later (`src/pipeline.py:466–492`). |
| H4 | Wording: README/ABOUT stop claiming the citation lands on "the paragraph that actually says it" (`README.md:12–13`). State that the locator and page are verified, **not** that the passage supports the claim. |
| H5 | Answer-scope policy (Q1) written into the system prompt, with refusal examples. |

**Acceptance:**
- suite green;
- new tests for each outcome × truncation × uncited prose;
- the offline eval is byte-identical (retrieval untouched);
- a live spot check of 5 questions, including one forced truncation (low `max_tokens`
  in a test double).

---

## 4. Track B — global multi-vendor harness (separate project; astra B2, M5–M7, m17)

Owner request: apply it globally so every project under `~/ClaudeCode` gets it. **Approved 9 Oct.**
**Design of record:** `~/ClaudeCode/harness/docs/DESIGN.md` (this is design 002-v2). Status 9 Oct:
B1 (hooks, enrolment, installer, tests) is being built; astra is reviewing the design; nothing is
enabled until tests pass and the owner says go.
Implemented as a **versioned tooling repo** (`~/ClaudeCode/harness`, Q8) with an
installer. It stays off the product's critical path.

**Principles (normative; these become design 002-v2):**
1. **Explicit enrolment.**
   - A project is managed only if it carries a registered, versioned
     `.harness/project.toml`.
   - The hooks resolve the *actual* target repository: `git -C`, worktrees, nested repos.
   - The hooks are no-ops everywhere else, including non-coding sessions.
   - An unknown or invalid manifest means dispatch is denied.
2. **Containment is OS-enforced, not prompt-enforced, for every vendor.** For third-party
   workers:
   - Use a **sanitised snapshot** of the allow-listed files (`git archive` export,
     **no `.git`**). A sparse checkout still shares the object store and history, held-out
     files included.
   - Run under a macOS Seatbelt profile that denies reads outside the snapshot, denies
     network access except the vendor endpoint, and **denies Keychain/credential-helper
     access**.
   - Inject the vendor credential into that one process only.
   - Set `HARNESS_WORKER=1`, so shell startup never exports owner credentials.
   - `--bare` skips CLAUDE.md and hooks, so pass policy explicitly (`--settings`,
     appended system prompt).
   - Synthetic **escape probes** must pass before the first real dispatch.
3. **Hooks supplement containment and never replace it.** Hook errors and timeouts can
   fail open.
4. **Return path:**
   - Compare the *complete* returned tree against the dispatch snapshot: additions,
     deletions, modes, symlinks. `git diff --name-only` misses staged and untracked
     changes.
   - Reject anything outside the file allowlist.
   - Claude integrates and commits with an `Implemented-by:` trailer.
5. **Gate states:** COMPLETE / INCOMPLETE / WAIVED. A rate-limited second-vendor leg is
   INCOMPLETE, and a merge needs either a later COMPLETE or an owner-recorded WAIVED.
   SKIPPED never counts as PASS.
6. **Rate limits:**
   - account-scoped lock files (one lane per prepaid balance or account);
   - bounded attempts;
   - classify the failure (quota vs. timeout vs. crash);
   - terminate the process tree before falling back;
   - any fallback vendor runs under identical containment.
7. **Installable and reversible:** dry-run, install, uninstall, rollback. Existing
   project hooks are preserved. Malformed-config, alternate-git-form, nested-repo and
   concurrency tests run before enabling.

**Components:**
- **G1** `~/ClaudeCode/CLAUDE.md`: delegation constitution, model routing, the universal
  third-party data rule, the gate states.
- **G2** generic `worker-reviewer` agent.
- **G3** `guard_git`: no `--no-verify`, `add -f`, inline `hooksPath`, or force-push to
  main.
- **G4** `never_commit_guard` (universal secrets list + project list).
- **G5** opt-in lint hook.
- **G6** worker launchers (Grok, DeepSeek, GLM) following principle 2.
- **G7** `harness-init`.
- **G8** rate-limit protocol (principles 5–6).
- **G9** test suite.
- **G10** migrate fe1-companion and jobradar after a week.
- **G11** secrets hygiene:
  - real API keys out of project `.env` files into the Keychain;
  - Codex and workers run with scrubbed environments;
  - honest wording: this reduces exposure, it does not make a leak impossible.

**rag-pipeline enrolment:**
- `never_commit`: `data/`, `*.pdf`, `.env*`, `chroma_db*/`, `sample_chroma_db/`,
  `eval/bakeoff/`, `eval/private/`, `logs/`, `Tutorial_Docs_for_review/`.
- The do-not-read clause is single-sourced in CLAUDE.md.
- `merge-gate` skill.
- `docs/harness.md` changelog.
- `docs/ROADMAP.md`.
- **First dispatch:** synthetic-fixture tests for G4, after the escape probes pass.

---

## 5. Phase 16 — eval foundations, then retrieval

### 16A — eval foundations (astra B3, M8–M10)
1. **Privacy-aware reporting first.** Today the evaluator writes question text into
   reports and stdout (`src/evaluator.py:2297–2301`).
   - Private sets need opaque IDs in any committed artifact.
   - Detail goes to a gitignored private report only.
   - Sanitise stdout and errors.
   - The same applies to judge dumps and caches.
   - **No private question enters an eval run until this lands.**
2. **Schema:**
   - stable `id` and `family_id` (a scenario and all its paraphrases);
   - **evidence groups** (required AND groups of interchangeable OR passages). Today the
     evaluator drops extra fields and treats all expected sections as OR
     (`src/evaluator.py:200`, `:397`);
   - scope class (answer / partial / refuse) per Q1;
   - PARTIAL answers scored on their stated gaps.
3. **Split by scenario family *before* paraphrasing.** Siblings never straddle the split.
   - The 24 tutorial rows were already run through retrieval, so they are **development
     data**, not sealed.
   - Sealed held-out v2 is built blind: split first; retrieval runs only on the
     development side.
   - Any inspected hold-out retires to regression use.
4. **Validation:** the owner (plus the second reviewer per Q3) validates every sealed,
   refusal and ambiguous label. The disagreement rate is recorded.
   - Flagged so far: T01-Bb;
   - the realistic-set "planning permission for an extension" refusal label (the handbook
     covers exemption limits; resolve under Q1);
   - missing secondary labels on T07-Ab and T07-Ba;
   - conflicting handbook dates for the pre-1975 architect's-certificate rule (see the
     source registry, Phase 18).
5. **Pre-registered analysis:**
   - power is counted in **independent scenario families**, not paraphrases;
   - clustered or paired analysis (McNemar over families);
   - a minimum worthwhile effect stated up front;
   - a bounded comparison schedule;
   - **critical controls** (a zero-regression set) kept separate from the exploratory
     set, so "zero flips anywhere" doesn't permanently entrench the incumbent;
   - cached expansions for controlled comparisons, plus repeated live draws to measure
     deployment variance.
6. **Instrument:** C4 cohort identity (set hashes, expected sections, question identity);
   the item-9 backlog; absorbed-section metadata (D54; its own D-entry).
7. **Sources:** more tutorials, processed with the blind protocol; colleagues' real
   queries for the realistic slice; refusal rows at 25–30%.
   - Targets in **families**: tuning ≥60, realistic ≥40, sealed held-out ≥50, each with
     2–3 phrasings.
8. Re-baseline: offline + one canonical run (cost confirmed first).

### 16B — retrieval experiments (judged on 16A sets; astra M11, M12)
- **Candidate pool first.**
  - Expose candidate generation separately and measure recall@N.
  - The near-misses sit at ranks 7–19 and `CANDIDATE_POOL=12` (`src/retriever.py:49`) cuts
    some off.
  - Choose the pool size from the measurement.
- **Cross-encoder reranker.**
  - Specify the model, revision, licence, pair-token truncation, and p50/p95 cost.
  - The disabled path must stay genuinely unchanged (canary).
  - LegalBench-RAG found general rerankers can *hurt* legal retrieval; the critical-control
    rule decides.
- **WS-C truncation mechanism** (is MiniLM's 256-token cut-off load-bearing?).
  - First, version the representation in the index manifest: preprocessing, tokenizer and
    model revision, window and stride, aggregation, prompts, chunk-map hash. Today it
    records only the model name (`src/embedder.py:409`).
  - Isolate each arm's index.
  - "Max-pool" = maximum passage similarity across windows.
  - Mechanism experiments are kept separate from deployment selection.
- **WS-D vocabulary gap:** glossary expansion, intent-rewrite tuning, HyDE, BM25 stemming.
  The development phrasings are the tuning data; sealed families are the judge.

---

## 6. Roadmap — Phases 17–23 (astra B4, M13, M15; research 8 Oct)

**Maturity today:** advanced RAG with modular seams. **Guiding rule:** add autonomy only for
measured failures. An LLM may *propose* more retrieval but never approve an unsupported claim.

| Phase | Scope |
|---|---|
| **17a — answer integrity (full)** | Claim-level entailment pass (downgrade-only); `search_result` blocks for native citations, with our gate as a second check; prompt-injection delimiters; tightened locator matching; quote-snapping; conflict surfacing. |
| **17b — model migration** (kept separate so regressions stay attributable) | Sonnet 5 → 5.5 (`between_tools` or adaptive at low effort; no `disabled`); canonical run; model IDs to config. |
| **18 — service + index lifecycle + source registry** | One application package: a typed query service with thin CLI and FastAPI adapters. Explicit states: answer / refusal / insufficient-evidence / operational-error, plus evidence refs and degradation flags. Disclosure policy is enforced before serialisation and streaming. Ingestion is an admin job. **Immutable index releases:** Chroma + BM25 + **paragraph/source registry** (source, edition, para, page span, absorbed sections) + full manifest; built and validated offline; one atomic release id; requests pinned to a release; rollback retained; desync repaired by rebuilding, never by mutating the live index; the pickle loader is trusted-local-only. **Tracing:** request/step ids; release, config, model and prompt versions; evidence ids; budgets; errors; verification decisions. Keyed HMAC (not a salted hash) for queries. Hash chain with an independent checkpoint. Edition/currency shown in answers. |
| **19 — deterministic tools + routing + RESEARCH MODE** (needs the 18 registry; Q1) | **Research mode:** an opt-in toggle; Anthropic web search with `allowed_domains` limited to authoritative sources (irishstatutebook.ie, revisedacts.lawreform.ie, courts.ie, tailte.ie, lawsociety.ie, gov.ie); a currency check comparing handbook statements with current sources; per-claim provenance labels; protective disclaimer; its own eval (claim support against sources, currency-flag accuracy) and its own Q4-style bars. **Data-flow note:** research-mode questions go to the search provider, so no client-identifying facts in queries (UI warning plus a redaction check). |
| 19 (cont.) | `get_paragraph`, `get_chapter_toc`, `follow_cross_reference`, `define`; an Adaptive-RAG router (direct lookup / single pass / decomposition / refuse); one-shot decomposition. Measured on the non-agentic path first. |
| **20 — internal pilot** | **Entry requirements** (not exit): named-user auth; licence position confirmed (§7); Anthropic ZDR/DPA **confirmed for the actual org, model and features** (it is not automatic); raw-query logging removed; firm AI-policy sign-off (Law Society GenAI guidelines v4); Q4 bars agreed; operator named (Q5); backup and restore tested. Then a React + TS front end with citation-first UI, side-by-side passages, distinct refusal / insufficient states, and feedback into candidate review. |
| **21 — bounded agentic loop (planned, per Q6; ships to production only if it wins)** | A deterministic state machine with typed read-only tools; a sufficiency model proposes retrieval; request-wide budgets covering retries; on exhaustion, return an already-verified answer or abstain; trajectory eval. Kept only if it beats the frozen single-pass on sealed families without more unsupported claims. |
| **22 — operational exit assessment** | Pre-registered adjudicated scenario counts, severity categories, claim-support / completeness / refusal metrics, incident thresholds and rollback criteria. "Four weeks, zero ungrounded" becomes supplementary only: 0 failures in 25 still leaves an ~11% upper bound. |
| **23+ — multi-source corpora / partnership / per-firm packaging** | Tailte Éireann guidance, courts.ie, statutes, other Law Society manuals, each with source authority and edition/currency metadata; a legal-data provider partnership as a verification layer (owner's business-plan research). |
| 23+ (cont.) | Only with source authority, temporal metadata and document access controls. Self-hosted single-tenant per firm, each firm ingesting its own licensed copy. Needs a named operator and support model before any external offer. |

**Not planned:** open-ended or multi-agent orchestration; GraphRAG; tuning on <50 families;
fine-tuning on eval data.

---

## 7. Production target, data flow, licensing (astra M14)

**End state:** a self-hosted, single-tenant verifiable-answers service per firm, ingesting
the firm's own lawfully purchased copy. The vendor ships software only.

**Complete data flow (to be documented and approved in Phase 18):**

| Outbound to Anthropic | Contents |
|---|---|
| Expansion call (Haiku) | the **question** |
| Generation call | the question plus **retrieved excerpts** |
| Future chat history | condensed prior turns |
| Eval judge (dev only) | answers plus excerpts |

Nothing else leaves the machine.

**Rights basis needed in writing for:**
- scanning or extracting the purchased copy;
- indexing (likely CRRA s.53B TDM unless reserved);
- **displaying excerpts** (probably outside TDM);
- sending excerpts to an API processor;
- tutorial-derived eval datasets;
- any redistribution.

The Law Society GenAI guidelines (v4, Dec 2025) expressly don't cover copyright.

**Email questions:**
1. TDM/AI reservation (s.53B(3))?
2. Internal indexing of a purchased copy?
3. Excerpt display and maximum length?
4. Print vs e-book?
5. Sending excerpts to an LLM API?
6. Is an AI/RAG licence available?
7. Bring-your-own-copy software that ships no text?
8. Who is the rights contact?
9. **Use of PPC tutorial materials to build evaluation questions?**

---

## 8. Execution lanes

As decided in §1.1. Hard floor for every third-party lane:
- no corpus, chunk, held-out, private, tutorial or client material;
- OS-enforced containment (§4);
- Codex always `--sandbox read-only` and never executes project code (importing `src`
  loads `.env`).

## 9. Sequencing

1. Owner reviews v3 → answers Q1–Q8 → rotates the PAT.
2. "Go" → merge PR #19 → `v2.2.0`.
3. **H (integrity hotfix)** → `v2.2.1`.
4. **Track B** (harness repo), then **16A**, can run in parallel (different repos). 16A's
   privacy reporting lands before any private data runs.
5. 16B → 17a → 17b → 18 → 19 → 20 → (21) → 22 → 23+, each plan-gated.
6. A second astra pass is optional: on design 002-v2 once drafted from §4, or on the
   Phase 18 architecture.

## Risks
- Eval power remains the binding constraint until 16A produces ≥50 sealed families.
- Labels can be confidently wrong; second-reviewer validation is the control.
- A global hook bug → enrolment-only scope, dry-run and rollback.
- Licence (§7) can constrain Phases 20 and 23+.
- Quota exhaustion → INCOMPLETE gate states, never silent skips.

## Ledger numbering
D59 (+ addendum) landed · D60 third-party lane / global harness (harness DESIGN v2) · **D61 answer-scope modes (Q1, owner 9 Oct)** · D62 integrity hotfix · D63+ Phase 16A/B decisions. D56/D58 stay retired.


## Review

### Codex merge review — pre-fix Phase 15 diff (8 Oct, `gpt-6.1-sol` @ xhigh, read-only)

Verdict: **request changes**, 1 BLOCKER / 4 MAJOR / 2 MINOR. **Dispositions owner-approved 8 Oct; fixes landed in `bdd7622`, `33ec020`, `832eb2f`.** Codex read no excluded path and
edited nothing; its own run could not collect the full suite (PyTorch needed a writable tmp in
its sandbox) — the suite is 609/609 green locally. Proposed dispositions (final on owner
approval; copied into `docs/designs/001-…` `## Review → ### Merge gate` when actioned):

| # | Sev | Finding | Disposition |
|---|---|---|---|
| C1 | BLOCKER | Model override can mix two models' vectors in one store and rewrite the manifest so the read-path assert passes (`src/embedder.py:175` → unchecked writes at :531, :700, :735). MiniLM and granite are both 384-d, so dimension checks don't catch it. Also `rebuild_bm25_index` relabels without re-embedding; legacy stores with no manifest need defined handling. | **ACCEPT** — this *is* gate blocker 1; A1 scope widened: assert on all three write paths, defined behaviour for a non-empty store with no manifest (refuse unless `--reset`), tests prove a cross-model call changes neither vectors nor sidecars incl. the deferred-BM25 path. |
| C2 | MAJOR | `scripts/bakeoff_report.py:747` enforces held-out exclusion on CLI filenames, not on the report's recorded question-set paths; the label fallback (:341) can treat a mislabelled set as golden. | **ACCEPT, fix in Track A** (6-line provenance guard + a neutrally-named-report test) — it guards a hard rule. Verified 8 Oct that the real Phase 15 artifacts reference only `eval/golden_set.jsonl` and `eval/realistic_set.jsonl` (no held-out path), so the D57 verdict is unaffected. |
| C3 | MAJOR | Missing evidence yields clean flip lists (`:401`, `:675`) — the vacuous pass the gate deferred (item 8). | **ACCEPT, fix in Track A** (the three guards are mechanical) rather than defer: the instrument is binding for every future bake-off. Verdict unaffected: both candidates were disqualified by flips the instrument *found*; the baseline survived by default, not by a clean comparison. |
| C4 | MAJOR | Raw comparison matches by question text without checking set hashes/expected sections; production rows match by positional key only. | **DEFER to Phase 16 WS-A6** (instrument rework alongside McNemar, before the instrument is next used). Verdict unaffected: all arms ran from one checkout against identical set hashes (manifest). |
| C5 | MAJOR | Manifest omits production-rank dumps, expansion-cache identity, shipped W and commands (plan L701 requires hashes **and** commands); Outcome collapses raw/production flips and omits S5 ranks. | **Split:** Outcome part = gate blocker 3 → **ACCEPT** (A3). Manifest completeness → **ACCEPT, Track A** if the existing local artifacts can be hashed retroactively (they can: the dumps are in `eval/bakeoff/`); commands recorded from the brief's run log. |
| C6 | MINOR | Doc-prompt-only spec leaks the document prompt to queries (`src/embedder.py:269`), and `tests/test_embedder.py:811` blesses it. Dormant (no shipped spec uses it). | **ACCEPT, fix in Track A** — small, and a test asserting wrong behaviour should not merge. |
| C7 | MINOR | Docs advertise a truncation guard/escape hatch that doesn't exist; L833 provenance reconciliation missing. | **ACCEPT** — gate blockers 4 and 5 (A4, A5), wording per O1. |

### Codex scoped re-review of the fix commits (`gpt-6.1-sol`, high, 8 Oct)

**No BLOCKER.** C1, C6 and C7 closed. Two MAJOR residuals and one MINOR, each **ACCEPTED
and fixed** in the follow-up commit (D59 addendum):
- C2: a report without a provenance block bypassed the guard.
- C3: a missing `n`, or a missing `strict_rank`, read as clean.
- C5 historical: full hashes inlined; the unlogged command lines are stated as a gap.

The new tests were verified to fail on the old code. Suite: 633 passed.

**Harness finding:** Codex's sandboxed `import src` triggered `load_dotenv()`, which read
`.env` (no value displayed). Remedy in Track B: G11 below.

### Codex astra pass on this plan + design 002 (`gpt-6-astra` @ xhigh, 8 Oct, read-only; raw text: `003-review-astra-2026-10-08.md`)

Verdict **REVISE** (4 BLOCKER / 11 MAJOR / 2 MINOR). Static review; no project code executed.
Its code citations were spot-checked by Claude and confirmed: `README.md:12–13`, `src/pipeline.py:466–492`, `src/generator.py:365`, `src/evaluator.py:200/397/2297–2301`, `src/retriever.py:49`, `CLAUDE.md:30`.
**All findings ACCEPTED** and folded into v3:

| # | Sev | Finding | Where in v3 |
|---|---|---|---|
| 1 | BLOCKER | Integrity defects in real use were sequenced after tooling | §3 hotfix H, before Track B |
| 2 | BLOCKER | Worker containment: sparse checkout shares git objects; `--bare` skips CLAUDE.md/hooks; read-only ≠ confidential | §4 principle 2 (sanitised snapshot, Seatbelt, credential isolation, escape probes) |
| 3 | BLOCKER | Evaluator writes question text to reports and stdout, so private sets would leak | §5 16A.1 (privacy-aware reporting first) |
| 4 | BLOCKER | Colleague access came before auth, ZDR, licence and logging gates | §6 Phase 20 entry requirements |
| 5 | MAJOR | Global hooks had uncontrolled scope | §4 principles 1, 3, 7 |
| 6 | MAJOR | "Can never load a secret" was false; `~/.zshenv` re-exported the PAT into any shell | **Fixed 8 Oct**: export moved to interactive, non-worker shells; §4 principle 2 / G11 wording; PAT rotation is an owner action |
| 7 | MAJOR | Rate-limit SKIPPED silently weakened gates | §4 principles 5–6 |
| 8 | MAJOR | Hold-out contradiction; inspected rows aren't sealed | §5 16A.3 (family split before paraphrase; tutorial rows → development) |
| 9 | MAJOR | Paraphrases inflate apparent power | §5 16A.5 |
| 10 | MAJOR | Label schema can't express evidence groups or scope | §5 16A.2, Q1 |
| 11 | MAJOR | Pool of 12 can't hold rank 13–19 near-misses | §5 16B (measure pool recall first) |
| 12 | MAJOR | WS-C needs representation versioning | §5 16B |
| 13 | MAJOR | Source truth and legal currency missing | §6 Phase 18 source registry, conflicts, edition display |
| 14 | MAJOR | Data flow and licence assumptions incomplete | §7 |
| 15 | MAJOR | "Four weeks, zero ungrounded" isn't a measurable gate | §6 Phase 22 |
| 16 | MINOR | Inconsistent snapshot; CLAUDE.md says canonical v3, D51 says v4 | §0 single snapshot; CLAUDE.md fix in Track B doc sync |
| 17 | MINOR | 002's return-path check misses staged and untracked changes | §4 principle 4 |

Co-developer proposals adopted:
- roadmap split: H, then Track B as a separate project, then 16A/16B, then 17a/17b;
- service shape and index lifecycle;
- tracing with HMAC and an independent checkpoint;
- the agentic boundary;
- deployment needs a named operator.

The proposed 002-v2 outline is adopted as the skeleton (§4 principles). Astra's open questions are merged into §1.2 (Q1–Q6).

## Outcome

*(Filled in as tracks complete.)*
