# Realistic-slice candidate review — Phase 13 (D46)

**STATUS: DRAFT — pending user review. Do not run the canonical eval against this set until
the freeze block at the bottom is filled in.**

## What this slice is (and is not)

21 questions in natural staff phrasing: vague wording, colloquial register, embedded
misconceptions, and near-domain negatives. It exists because the golden/held-out sets are
AI-generated from the corpus and share its vocabulary — they measured near-self-retrieval
(held-out strict hit@6 20/20) while every real-world query failed on 14 Jul.

**Honesty label (D46):** this slice was authored *during* the Phase 13 remediation and is used
to iterate on it. It is **dev/regression evidence, not held-out proof**. An independently
authored realistic slice (real staff questions, collected blind) is the roadmap follow-up.

## Method

- Candidate questions drafted from three sources: (1) the four real failing queries from the
  14 Jul field test, kept verbatim including natural wording slips; (2) messy paraphrases of
  **golden (tuning) set questions only — never held-out questions**, so prompt iteration on
  this slice cannot contaminate the held-out headline's independence; (3) new
  vocabulary-shifted questions and near-domain negatives.
- Every expected section verified against the indexed corpus (1,470 chunks) by keyword sweeps
  plus hybrid retrieval, reading the candidate paragraphs. Evidence below is paraphrased —
  no verbatim handbook text is committed (same rule as data/).
- Negatives verified absent with ≥3 distinct keyword searches each plus a retrieval call;
  search terms recorded.
- Schema: `load_golden_set` types only (`direct`/`exact_token`/`refusal`); `expected_sections`
  are OR-alternatives. No question duplicates any golden/held-out question text
  (`generate_answers` raises on duplicates within a run).

## Answerable questions (15)

| # | Question (abridged) | Expected | Pages | Conf. | Evidence (paraphrased) |
|---|---|---|---|---|---|
| S1 | Transfer via deed of assignment (lease), "15 years of consideration", unregistered — verbatim seed | 4.8.1.1, 4.7.4.3 | 65–68 | High | For leasehold title the vendor may deduce from a conveyance/assignment for value ≥15 years old (skip document); 4.7.4.3 states the same rule in the age-of-roots discussion. The question's "15 years of consideration" is a garbled reference to exactly this. |
| S2 | Same, plus "what does 15 years of consideration refer to?" — verbatim seed | 4.8.1.1, 4.5.1 | 62–68 | High | As S1; 4.5.1 supplies the general 15-year open-contract root period (s.56, LCLRA 2009) the phrase garbles. |
| S3 | "progress for registering unregistered land" — verbatim seed (sic) | 13.3, 13.1 | 434–442 | High | 13.3 = first-registration procedure (statement of title, Form 1/2, root rules); 13.1 = chapter scope/intro. |
| S4 | "what unregistered land means" — verbatim seed | 1.7, 1.8 | 7–15 | High | 1.7 explains the two registration systems; 1.8 is the direct side-by-side contrast. |
| P1 | How far back must title documents go; "15 years is enough?" | 4.5.1 | 62–63 | High | 15-year open-contract root period. Paraphrase of golden Q7. |
| P2 | Which office to check for ownership — "Land Registry or the other one?" | 1.8, 1.7 | 7–15 | High | Registry contrast. Paraphrase of golden Q3/Q8 territory. |
| P3 | Husband sole owner, wife not on deeds — sell without telling her? | 7.2, 7.2.9 | 178–191 | High | FHPA 1976 s.3(1): conveyance without the non-owning spouse's prior written consent is void; 7.2.9 = the practice verification (statutory declaration). Paraphrase of golden Q12/Q13. |
| P4 | Two brothers own a farm, one died — half pass automatically? | 5.8 | 79–80 | High | Joint tenancy survivorship vs tenants-in-common devolution — never names either term. Paraphrase of golden Q9. |
| P5 | Seller leaving appliances/furniture — tax on that too? | 16.4.5 | 672 | High | Stamp duty generally not charged on contents passing by delivery. Paraphrase of golden Q20. |
| P6 | Neighbour using field for years, previous fella before him — times count together? | 13.4.8 | 471 | High | Successive squatters can aggregate adverse-possession periods passed by deed. Paraphrase of golden Q21. |
| P7 | Why do deeds say the seller received the money? | 8.2.5.7 | 213 | High | Receipt clause = statutory discharge protecting later bona fide purchasers. Paraphrase of golden Q14. |
| P8 | Judgment against client — how does creditor register it against his land? "Is there a form?" | 14.15 | 550–551 | High | Form 60 = application to register a judgment mortgage. (Original draft wrongly assumed Form 60 was a status query — corpus verification corrected it; see log below.) Paraphrase of golden Q26. `exact_token`. |
| N1 | Buyer's deposit if the sale falls through? | 6.9.2, 6.9.4 | 147–148 | High | Forfeiture-and-resale on purchaser default vs full refund on rescission — outcome depends on why it fell through; both listed as alternatives. |
| N2 | Acting for both buyer and their bank — allowed? | 9.7.2, 9.8 | 258–267 | High | Certificate-of-title system: solicitor acts solely for the borrower while giving the lender an undertaking; 9.8 = when lenders instruct their own solicitor. Not a flat yes/no — good graded-answer test. |
| N3 | What happens in what order after sale agreed? | 2.9, 2.1 | 20, 37–38 | High | 2.9 = side-by-side vendor/purchaser step flow; 2.1 = three-stage framework. Deliberately drops "how long does it take" — the corpus contains no typical-duration guidance (verified absent), so a duration question would be unanswerable. |

## Near-domain negatives (6, `type: refusal`)

All verified absent. Search terms recorded per question.

| # | Question | Conf. absent | Searches run | Notes |
|---|---|---|---|---|
| R1 | Vendor died after contracts signed, before completion — procedure? | High | death on title; personal representative; grant of probate/administration; transmission; death of vendor/purchaser; risk | The corpus covers death only in the historical chain of title or a PR-as-vendor from the outset — never mid-transaction death. **The hardest rule-(d) test in the set**: probate-adjacent chunks exist, so a weak policy would emit related-guidance instead of refusing. See review flag below. |
| R2 | Conveyancing steps for buying a house in Northern Ireland? | High | Northern Ireland; Land Registry of Northern Ireland; UK conveyancing; England and Wales | One incidental NI mention in a stamp-duty charity clause; nothing on NI process. |
| R3 | How much does a solicitor typically charge for a purchase? | High | solicitor fees; legal fees typically; scale fee; costs for conveyancing | Only the s.150 LSRA 2015 duty to disclose the fee basis — no figures or ranges. |
| R4 | Can a landlord evict a tenant behind on rent? | High | eviction; evict; arrears of rent; Residential Tenancies Board; RTB | RTB content appears only as purchaser due-diligence requisitions, never dispute procedure. |
| R5 | Requirements for making a valid will in Ireland? | High | valid will; execution of a will; testamentary capacity; s.78 Succession Act; two witnesses; attestation | Corpus is dense with probate/assent/devolution language but never covers will-validity formalities — a strong adversarial negative. |
| R6 | Planning permission for a house extension? | Med-High | planning permission for an extension; apply for planning permission; planning application process | Ch.11 covers exempted-development checks retrospectively for a sale (and expressly delegates the exemption call to an architect/engineer); no forward-looking application guidance. |

## Flags for user review

1. **R1 (vendor dies mid-transaction) is deliberately borderline.** Under the new graded
   policy, is the *desired* behaviour the exact refusal, or a caveat-form answer pointing at
   the closest probate/PR guidance? The eval schema forces a binary choice and it is currently
   `refusal`. If the canonical run shows the model giving (arguably more useful) caveat-form
   answers here, the options are: accept the lower refusal score with a written justification,
   or reclassify/drop the row. Your call at review.
2. **S1/S2 expected sections are specific** (4.8.1.1/4.7.4.3): the seed questions are garbled
   enough that "related" hits on 4.5.x will score under the related metric but miss strict.
   That is intended — these are the hardest answerable rows — but confirm you're happy for the
   headline realistic strict number to carry that difficulty.
3. Question wording is deliberately informal (typos in seeds kept verbatim, e.g. "progress"
   for "process"). Edit freely — wording changes before freeze are exactly what this review
   is for. Adding more real staff questions is very welcome; they are the highest-value rows.

## Correction log

- P8: initial draft assumed Form 60 was a pending-application status query; corpus
  verification showed Form 60 is the judgment-mortgage registration form (14.15) and there is
  no status-query form in the corpus (status checks are described via landdirect.ie dealing
  numbers, 14.1.9.6–7). Question rewritten around the verified purpose.
- A "how long does a purchase take" candidate was cut: step ordering is covered (2.9) but
  typical duration is verified absent — a half-answerable question makes a bad eval row.

## Freeze block (fill at freeze — after user review)

- Frozen: <date>
- Line count: <n> (answerable <n>, refusal <n>)
- sha256: <hash of eval/realistic_set.jsonl at freeze>
- Post-freeze rule: same as the held-out set — wording and expected sections do not change
  after freeze; corrections require a new dated entry here and a re-run.
