# Work state — where this project stands and what happens next

**Living document.** Updated at the end of a working session, before the session's
context is lost. It exists because the 24 Aug session's Track 2–4 drafts lived only in a
temp scratchpad and were gone by 10 Sep. Anything worth resuming from goes in the repo,
not in a chat.

**Last updated:** 8 Oct 2026
**Branch:** `phase-15-retrieval-foundation`, **pushed 8 Oct** (tracks origin). Gate blockers 1–6
and the Codex merge-review findings were fixed on 8 Oct (D59). The PR is not open yet: it
waits for a scoped Codex re-review of the fixed diff.
**Suite:** 629 passed (8 Oct).
**Forward plan:** `docs/designs/003-roadmap-and-next-actions.md` (draft; owner decisions in;
Codex astra review pending). It supersedes §2–§3 below as the plan of record. Those sections
are kept as history.
**Correction:** an earlier version said a `phase-16-harness` branch "already exists". It never
existed anywhere. Earlier commit counts were also wrong: the branch was 14 commits ahead at
`fd6c936`, not 11.

---

## 1. Where work stands

Phase 15 (retrieval foundation / embedding bake-off) reached a **documented negative
result** — D57: no candidate (gte-modernbert-base, granite-small-english-r2,
Qwen3-Embedding-0.6B) beat baseline `all-MiniLM-L6-v2`; granite collapsed the realistic
set 0.353 → 0.118; the "undertaking" flip is a BM25/RRF fusion knife-edge rather than a
semantic miss; 0/3 vocabulary-gap questions were recovered by any arm. The user accepted
and closed it on 24 Aug (accept-&-close): MiniLM stays, WS5–WS8 mooted, re-open only
after Phase 16 gives the eval statistical power.

Close-out commits landed (`802b0eb` ledger, `f01281d` doc sync, `fd6c936` results.md
annotation). `/phase-gate 15` then ran and **FAILED — fixable forward**, with six
blockers. Full evidence and the fix list: **[docs/phase15-gate-report.md](phase15-gate-report.md)**.

The harness itself was audited on 24 Aug (13 findings, theme: rules duplicated as prose
in N places and enforced by memory). The remediation work — Tracks 2–4 below — was
drafted and then lost.

---

## 2. Next actions, in order

**A. Fix the six Phase 15 blockers** (user-approved 10 Sep). Detail in the gate report.
   1. cross-model manifest assert on the index path, `src/embedder.py` + tests — the only
      code fix;
   2. D59 ledger entry (gate disposition + third-party-lane deviation);
   3. brief 001 §Outcome disclosure repairs (S5 rank line, separate production-config
      flip list, inline auditability evidence);
   4. `IMPLEMENTATION_PLAN.md` L790 vs L836 contradiction + the L833 non-cuttable item's
      disposition;
   5. truth the docstrings describing the absent truncation guard
      (`tests/conftest.py:23–33`, `src/embedder.py:43,91,107–114`);
   6. extend the inertness canary to cover `default_prompt_name = None`
      (`src/embedder.py:339` / `tests/test_embedder.py:760`).

   Suite green before each commit.

**B. Codex merge gate** on `git diff main...phase-15-retrieval-foundation` —
   `--sandbox read-only`, do-not-read clause verbatim. Dispositions (ACCEPT / REBUT /
   OUT-OF-SCOPE) appended to `docs/designs/001-bakeoff-embedding-model.md`
   `## Review → ### Merge gate`.

**C. Push + open the PR.** Headline is the negative result. Body carries gate outcomes,
   the Tier-2 "unchanged — no canonical run", the deferral dispositions, and the CI
   checklist. **STOP — merge waits for the user's "go".** CI has never run on this
   branch; CI green is a merge precondition.

**D. Post-merge:** tag `v2.2.0` (user-confirmed) — minor bump for the `EMBEDDING_MODEL`
   seam and the D54 eval-instrument comparability boundary; production behaviour is
   byte-identical.

**E. Then Tracks 2–4** on `phase-16-harness` (this branch already exists — see §3).

---

## 3. Re-draft list — Track 2–4 work lost with the 24 Aug scratchpad

Rebuild these as files. What each needs to contain is recorded here so the drafting does
not start from nothing.

| # | Artifact | What it must contain |
|---|---|---|
| 1 | `docs/designs/002-third-party-implementer-lane.md` | **DONE (10 Sep)** — re-drafted, `Status: draft`, needs `/plan-gate` before the first dispatch. |
| 2 | Do-not-read clause normalisation | The full clause lives in **exactly one** place: `CLAUDE.md` §Adversarial review, extended to: `data/`, any `*.pdf`, `.env`/`.env.*` (except `.env.example`), `chroma_db/`, `chroma_db_arm_*/`, `eval/bakeoff/`, held-out eval files (`eval/heldout_set.jsonl`, `eval/heldout_candidate_review.md`), `.claude/worktrees/`. Gate skills carry a `<clause>` template slot and instruct "copy it verbatim from CLAUDE.md"; both agents replace their stale embedded lists with "the caller passes the clause; if absent, STOP and return it as a blocking finding — hard floor regardless: never read `.env*` or `data/`". Verify: `grep -rn "Do NOT read" CLAUDE.md .claude/` → the full list exactly once. |
| 3 | `docs/harness.md` additions | Pieces-table rows (realistic_set, settings.json, merge-gate skill, pre-commit hook, ROADMAP, work-state); a **Changelog** section so drift becomes visible; §Re-gate stopping rule (≥3 fresh rounds + monotonically declining severity + only grep-verifiable residue ⇒ may close, basis recorded); §Tier-1/Tier-2 acceptance vocabulary; promote "one normative source" and "instrument gaps are build steps" to principles; §Negative-result phase path (a negative result is a deliverable: ledger incl. MOOTED numbers → artifact Outcome/Status → doc sync → phase-gate with MOOT-marked criteria → merge gate → PR headline IS the negative result → STOP for merge, within one session of the verdict). |
| 4 | `.claude/skills/merge-gate/SKILL.md` | `disable-model-invocation: true`; allowed-tools: codex read-only, `git diff/log/status/check-ignore`, `gh pr`. Steps: preconditions (phase-gate PASS or explicit waiver, clean tree) → diff scope → Codex review with the clause slot (unavailable ⇒ record "second-vendor leg SKIPPED") → disposition table into the design artifact's `## Review` → hygiene re-check incl. `git log main..HEAD --name-only` never touched a forbidden path → PR body template → STOP, merge is the user's. |
| 5 | Pre-commit guard | `.claude/hooks/precommit_guard.sh` + `tests/test_precommit_guard.py` + the `settings.json` hook JSON, **all in one commit** so the hook never points at a missing script. Reads hook JSON from stdin, extracts `tool_input.command`, and if it is a `git … commit`, checks `git diff --cached --name-only` against `^(data/\|chroma_db\|eval/bakeoff/\|logs/\|\.claude/worktrees/\|\.env)\|\.pdf$` (allowing `.env.example`); exit 2 with the offending paths on stderr to block, else exit 0. Deliberately does **not** run pytest: the hook exists for the one irreversible failure (corpus entering public history, <100 ms check); "tests green" stays gate-enforced. A natural first grok task. |
| 6 | `docs/ROADMAP.md` | Header contract: roadmap = direction (revisable, not gated); `IMPLEMENTATION_PLAN.md` = per-phase gated spec. Status vocabulary: `planned \| active \| done \| negative-result \| dropped`. Phases: 15 done (negative result, D57) · 16 golden-set program + cross-encoder reranker · 17 `search_result` content blocks + claim-level entailment · 18 FastAPI service layer, versioned index artifact, hash-chained audit, two-phase stream/gate contract · 19 React + TS front end · 20 pilot hardening / ZDR / feedback→golden promotion. |
| 7 | Phase 16 section in `IMPLEMENTATION_PLAN.md` | `## Phase 16 — golden-set program + cross-encoder reranker — phase-16-golden-reranker`, origin citing D54 + D57/addendum. **WS-A golden-set program:** expand 35 → 120–150 intents; paired per-question McNemar in `scripts/bakeoff_report.py` (~40 lines, b/c counts fall out of the existing flip lists, exact binomial via `math.comb`, no scipy — Tier-2 context, never replacing the Tier-1 zero-flip rule); a synthetic coverage backstop generator (**Claude-authored, never third-party — it must read corpus chunks**); CIT-interview + diary-study protocol templates; promotion workflow following the existing `eval/*_candidate_review.md` pattern; the absorbed-sections metadata fix (D54/Round-4 commitment, own D-entry). **WS-B reranker:** `sentence_transformers.cross_encoder.CrossEncoder` under the existing `sentence-transformers==5.6.0` pin (no new dependency — verify the import at plan time); slot in `src/retriever.py` between `_reciprocal_rank_fusion` (~L395) and the `fused[:top_k]` cut (~L403), reranking the `CANDIDATE_POOL=12` fused list; flag-gated, default off until a bake-off verdict; target class = the five near-misses (rank 7–19) plus the fusion knife-edge; latency disclosed. **WS-C third-party pilots** (first live use of design 002 after its plan gate): the pre-commit guard, the merge-gate skill draft, a mechanical doc sync. **Acceptance sketch — Tier-1:** suite green; reranker-off ⇒ byte-identical retrieval (canary); zero golden HIT→MISS flips rerank-on vs off in raw hybrid *and* cached-production replay; nothing corpus-bearing tracked; third-party diffs ⊆ files-in-scope. **Tier-2:** near-miss recovery /5, vocab-gap movement /3, strict@6 movement, McNemar p, rerank p50. |
| 8 | Small fixes | `settings.json` allows `Bash(codex exec --sandbox read-only *)`; plan-gate REVISE rule gains the re-gate stopping rule; `bake-off/SKILL.md` gains an `allowed-tools` line; harness changelog entry covering all of it. |

Also carried from the 24 Aug agent findings, not yet applied: sparse-checkout to keep
`eval/` out of a third-party worktree (folded into design 002), a CLAUDE.md amendment
stating the read-only rule applies to every third-party vendor, and a `RERANK_POOL`
constant for Phase 16.

---

## 4. Open decisions waiting on the user

1. **Sandbox profile location** — `.grok/sandbox.toml` tracked in-repo (auditable) vs
   `~/.grok/sandbox.toml` (invisible to the repo). Draft recommends tracked in-repo.
   (Design 002, open question 1.)
2. **Governance deviation** — the approved plan required design 002 to pass `/plan-gate`
   before any third-party write task. 002 is a fresh draft. Either plan-gate it first, or
   run the first tasks ad hoc and record the deviation in D59. The user's 10 Sep
   direction leans to the second; either way it gets recorded, not glossed.
3. **Push timing** — the branch is local-only. Pushing it (without merging) is the
   cheapest insurance against losing 11 commits of work.

---

## 5. Standing context

- **Golden set:** still 35 questions, untouched. It is the judge for every bake-off, and
  the Phase 15 negative result is partly a statement about its size. Expanding it is
  WS-A of Phase 16 and the single highest-leverage item on the roadmap.
- **Vendor rules:** the corpus ban and the read-only/containment rules apply to **every**
  third-party model, not just Codex. The xAI account has
  `coding_data_retention_opt_out = True` set (verified 10 Sep); that reduces exposure but
  is not a substitute for never sending corpus text.
- **Model roles as of 10 Sep:** Claude orchestrates, reviews and commits; grok implements
  grunt work in a sandboxed worktree; Codex co-develops judgment-bearing work and reviews
  at the gates. See `docs/designs/002-third-party-implementer-lane.md`.
