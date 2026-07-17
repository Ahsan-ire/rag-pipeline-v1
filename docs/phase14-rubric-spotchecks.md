# Phase 14 — WS5.2 comparison-rubric spot-checks (17 Jul 2026)

D30-safe record: rubric outcomes, tier, citation metadata, and retrieved locators only —
**no answer text** is stored here or anywhere in the repo. Live queries were run by the
orchestrating agent on 17 Jul 2026 ~05:01–05:04 against the production pipeline
(`python -m src.pipeline query "<question>" --top-k 6`), source state `f8e66a4`
(src/ identical through `928eefa`; the diff between the two is eval/results.md + README only),
generation `claude-sonnet-5`, expansion `claude-haiku-4-5` (live, sample independent of the
canonical run's). Rubric (from the gated plan WS5.2): basis of comparison stated; both sides
addressed; explicit contrast drawn; unsupported points named as gaps; correct answer tier
chosen; bracketed locator on every substantive sentence; answer checked for 2048-token
truncation.

## Check 1 — S5 phrasing: "What is the difference between a purchase and sale conveyance?"

| Rubric item | Verdict | Evidence (metadata only) |
|---|---|---|
| Basis of comparison stated | PASS | Answer frames the handbook's model — one conveyancing transaction with a vendor side and a purchaser side — as the basis, then compares roles within it |
| Both sides addressed | PASS | Vendor-side duties (title disposal, mortgage redemption) and purchaser-side duties (financing, lender undertakings, stamp duty, registration) each covered with citations |
| Explicit contrast drawn | PASS | Closing paragraph draws the contrast: same five-phase process, distinct duties per side |
| Gaps named | PASS | States the extracts do not frame "purchase conveyance" and "sale conveyance" as two distinct transaction types — the question's premise is answered rather than assumed |
| Correct tier | PASS | Caveat form (exact `CAVEAT_PREFIX` opener): correct, since no directly-answering section (2.2.1 / 2.2.2 / 2.9) was in the retrieved set |
| Citation on every sentence | PASS | 6 citations across every substantive sentence; caveat opener uncited by design (rule 3); gate outcome CITATIONS_VERIFIED, 6/6 resolved |
| 2048-token truncation | PASS (none) | Answer concludes naturally with a summary sentence; length well under the cap |

Cited locators: para 1.1 p.1 · para 1.5 pp.3–4 (×2) · para 2.2.2.1 p.27 · para 6.4.3.7 p.137 ·
APPENDIX 6.1 pp.154–155.

**Variance note:** this sample's retrieved set contains `2.2.2.1` — a dotted-nesting child of
expected `2.2.2`, i.e. a *related@6 HIT* under the eval rule — while canonical run #3's S5 row
(same code, different live expansion sample) is a related MISS. One more datapoint that S5's
retrieval outcome is expansion-sample-dependent at W=0.25; see the D50 addendum.

## Check 2 — N4 phrasing: "What's the difference between what the seller's solicitor and the buyer's solicitor actually do in a conveyance?"

| Rubric item | Verdict | Evidence (metadata only) |
|---|---|---|
| Basis of comparison stated | PASS | Opens with the three transaction stages and states each side performs distinct functions per stage [para 2.1] |
| Both sides addressed | PASS | Structured vendor's-solicitor and purchaser's-solicitor sections, each multi-sentence and cited |
| Explicit contrast drawn | PASS | Dedicated closing-stage contrast paragraph plus a summary contrast (title-standing vs title-scrutinising) |
| Gaps named | PASS | Names that the extracts contain no dedicated vendor's-solicitor-at-closing subsection comparable to 2.4.2 |
| Correct tier | PASS | Direct answer: expected sections 2.2.1 / 2.2.2 retrieved at top ranks (canonical run #3 concurs: N4 strict HIT rank 3) |
| Citation on every sentence | PASS | 14 citations, all resolved; gate outcome CITATIONS_VERIFIED |
| 2048-token truncation | PASS (none) | Ends with a complete summary paragraph |

Cited locators: para 2.1 p.20 · para 2.2.1 pp.20–21 (×5) · para 2.2.2 p.27 (×2) ·
para 2.2.2.2 p.27 · para 2.4.2 p.34 (×3) · para 6.4.3.1 pp.135–136 (×2).

## Overall

Both comparison questions **pass all seven rubric items**. The 15 Jul field-test failure form —
per-extract fragments instead of a comparison — is not reproducible on either phrasing under the
D49 synthesis rule. The retrieval-side acceptance anchor (S5 strict/related@6 in the canonical
artifact) is a separate, unmet criterion — recorded in the D50 addendum, not here.
