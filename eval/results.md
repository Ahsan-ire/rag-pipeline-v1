# Legal RAG Evaluation Report v2 (held-out, ablated)

- Date: 2026-07-13T20:01:22.821669
- top_k: 6
- Retrieval modes ablated: hybrid, vector, bm25
- Canonical run (writes the committed report): True

## Provenance

- git sha: d0c93e1 (dirty: False)
- indexed chunk count: 1470
- embedding model: sentence-transformers/all-MiniLM-L6-v2
- generation model: claude-sonnet-5
- matching: strict = exact section-number equality; related = dotted-nesting either direction (a retrieved parent OR child of an expected section also counts, e.g. expected 6.3.2 matches retrieved 6.3.2.2 or 6.3)
- MRR@6: truncated mean reciprocal rank — a question with no match in the top 6 scores 0 (cutoff disclosed).
- passes: retrieval ablation, refusals, completeness, judge
- answer passes (refusals/completeness/judge) use the HYBRID production retrieval config; the mode ablation affects retrieval scoring only.

Question sets:
- tuning: tuning (used to select fusion constants, D31 — NOT held-out)
  - path: eval/golden_set.jsonl
  - sha256: 13bfe8cf696737d564e7cf446ee11b79b357cd9250e1fe232a5d95b7f41b4164
  - question counts: direct=23, exact_token=7, refusal=5 (n=35)
- held-out: held-out (never tuned — out-of-sample)
  - path: eval/heldout_set.jsonl
  - sha256: 601a81c0a3e36aa5d90afb7904fdebad7704d3fff506871c0b9032d7576dcfe6
  - question counts: direct=15, exact_token=5, refusal=8 (n=28)

## Headline: strict hit@6 on the held-out set (hybrid)

**strict hit@6 = 20/20 = 1.000** (95% Wilson CI 0.839–1.000), set: held-out.

This is a single curated-set estimate: with n≈20 the interval spans several questions' worth of rate, so treat it as indicative, not a statistically-validated architecture claim.

## tuning — retrieval ablation

| Mode | S@1 | S@3 | S@6 | R@1 | R@3 | R@6 | MRR@6 strict | MRR@6 related | n |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hybrid | 0.500 | 0.667 | 0.800 | 0.567 | 0.733 | 0.900 | 0.601 | 0.679 | 30 |
| vector | 0.533 | 0.700 | 0.767 | 0.567 | 0.767 | 0.833 | 0.623 | 0.664 | 30 |
| bm25 | 0.400 | 0.733 | 0.800 | 0.567 | 0.900 | 0.900 | 0.558 | 0.717 | 30 |

By type (hybrid), strict / related hit rate:

| Type | Strict rate | Related rate | n |
| --- | --- | --- | --- |
| direct | 0.826 | 0.913 | 23 |
| exact_token | 0.714 | 0.857 | 7 |

## held-out — retrieval ablation

| Mode | S@1 | S@3 | S@6 | R@1 | R@3 | R@6 | MRR@6 strict | MRR@6 related | n |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hybrid | 0.900 | 0.950 | 1.000 | 0.900 | 0.950 | 1.000 | 0.925 | 0.925 | 20 |
| vector | 0.800 | 0.900 | 0.900 | 0.850 | 0.900 | 0.900 | 0.842 | 0.875 | 20 |
| bm25 | 0.800 | 1.000 | 1.000 | 0.800 | 1.000 | 1.000 | 0.875 | 0.875 | 20 |

By type (hybrid), strict / related hit rate:

| Type | Strict rate | Related rate | n |
| --- | --- | --- | --- |
| direct | 1.000 | 1.000 | 15 |
| exact_token | 1.000 | 1.000 | 5 |

## tuning — refusals & answer quality

Two-sided refusal view — a system can fail by refusing answerable questions OR by answering questions it should refuse:

| Direction | Rate | Count |
| --- | --- | --- |
| answerable questions REFUSED (false refusals) | 0.067 (95% CI 0.018–0.213) | 2/30 |
| near-domain NEGATIVES refused (correct refusals) | 1.000 (95% CI 0.566–1.000) | 5/5 |

Answer quality on the answerable questions:

- Syntactic sentence-citation coverage (micro-avg over non-refused answers): 0.754 (202/268 sentences; 2 refusal(s) excluded). "Syntactic" = has a bracket citation; a cited sentence may still carry a wrong locator (grounding measured separately).
- Citation-grounded fraction (micro-avg Σ grounded / Σ citations): 1.000 (215/215 citations).
- False-block rate (answerable drafts the gate would WITHHOLD as CITATIONS_UNVERIFIED): 0.000 (95% CI 0.000–0.114; 0/30). This is over-blocking PRESSURE, not proof each block was wrong.
- Gate-outcome distribution: REFUSAL=2, CITATIONS_VERIFIED=28, PARTIALLY_VERIFIED=0, CITATIONS_UNVERIFIED=0

## held-out — refusals & answer quality

Two-sided refusal view — a system can fail by refusing answerable questions OR by answering questions it should refuse:

| Direction | Rate | Count |
| --- | --- | --- |
| answerable questions REFUSED (false refusals) | 0.000 (95% CI 0.000–0.161) | 0/20 |
| near-domain NEGATIVES refused (correct refusals) | 1.000 (95% CI 0.676–1.000) | 8/8 |

Answer quality on the answerable questions:

- Syntactic sentence-citation coverage (micro-avg over non-refused answers): 0.881 (89/101 sentences; 0 refusal(s) excluded). "Syntactic" = has a bracket citation; a cited sentence may still carry a wrong locator (grounding measured separately).
- Citation-grounded fraction (micro-avg Σ grounded / Σ citations): 1.000 (89/89 citations).
- False-block rate (answerable drafts the gate would WITHHOLD as CITATIONS_UNVERIFIED): 0.000 (95% CI 0.000–0.161; 0/20). This is over-blocking PRESSURE, not proof each block was wrong.
- Gate-outcome distribution: REFUSAL=0, CITATIONS_VERIFIED=20, PARTIALLY_VERIFIED=0, CITATIONS_UNVERIFIED=0

## LLM judge (experimental faithfulness estimate)

Experimental and secondary — gates nothing. Conditional on a non-refused answer. The judge is the SAME model family as the generator, so it can share its blind spots; read as a rough estimate.

- tuning: mean faithfulness = 0.994 (over 28 scored; attempted 28, parsed 28, api-errors 0, parse-errors 0, zero-claim 0; judge=claude-sonnet-5 faithfulness-judge-v1).
- held-out: mean faithfulness = 0.995 (over 20 scored; attempted 20, parsed 20, api-errors 0, parse-errors 0, zero-claim 0; judge=claude-sonnet-5 faithfulness-judge-v1).

## tuning — per-question detail (hybrid)

- [direct] strict=HIT(rank=3) related=HIT(rank=3) expected=['1.1'] retrieved=['2.3.1.6', '14.3.2', '1.1', '11.4.2', '12.5.2.3', '4.4'] gate=CITATIONS_VERIFIED :: What is conveyancing?
- [direct] strict=HIT(rank=3) related=HIT(rank=2) expected=['1.7.2.3', '1.7.2'] retrieved=['10.2.7', '1.7.2.2', '1.7.2.3', '13.3.6', '13.4.21', '13.1.3'] gate=CITATIONS_VERIFIED :: How does searching on the Index of Names work in the Registry of Deeds?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['1.8'] retrieved=['1.8', '1.7.3', '1.7.3.6', '13.1', '4.9', '8.4.3'] gate=CITATIONS_VERIFIED :: What are the essential differences between the Registry of Deeds and the Land Registry systems?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['2.1', '2.2'] retrieved=['2.1', '2.3.1.6', '1.5', '11.6.3', '2.2.2.9', 'APPENDIX 6.1'] gate=CITATIONS_VERIFIED :: In what order do the steps of a typical conveyancing transaction occur?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['3.4'] retrieved=['3.4', '16.9.7', '8.5.11', '3.4.3', '14.9.4', '8.2.5.13'] gate=CITATIONS_VERIFIED :: What special considerations apply to voluntary deeds transferring property between family members?
- [direct] strict=HIT(rank=6) related=HIT(rank=6) expected=['3.2.3.7', 'APPENDIX 3.1'] retrieved=['9.8', '9.8.4', '9.8', '9.8', '9.7.2.2', '3.2.3.7'] gate=CITATIONS_VERIFIED :: What rules govern a solicitor giving an undertaking to a lender in a commercial property transaction?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['4.5.1'] retrieved=['4.5.1', '4.7.4', '13.3', '4.8.1', '4.7', '13.3.2.1'] gate=CITATIONS_VERIFIED :: How long must a good root of title be under an open contract?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['4.9.2'] retrieved=['4.9.2', '14.2.8.3', '10.2.4', '14.2.4', '10.2.4', '14.2'] gate=CITATIONS_VERIFIED :: Which three registers does the Land Registry maintain?
- [direct] strict=MISS(rank=None) related=MISS(rank=None) expected=['5.8'] retrieved=['14.7', '5.9.11', '14.7.6', '14.6.8', '14.7.2', '5.14'] gate=REFUSAL :: How does ownership held as tenants in common devolve on the death of one owner?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['6.1'] retrieved=['6.1', '1.5.3', '6.1', '2.2.1.16', '2.2.1.16', '6.2.9'] gate=CITATIONS_VERIFIED :: What form must a contract for the sale of land take in order to be enforceable?
- [direct] strict=MISS(rank=None) related=HIT(rank=1) expected=['6.3.2', 'APPENDIX 6.1'] retrieved=['6.3.2.2', '6.5', '6.5', '6.6', '12.11.2', '12.3.5.16'] gate=CITATIONS_VERIFIED :: Who holds the deposit paid under a contract for sale, and in what capacity?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['7.1', '7.2'] retrieved=['7.2', '7.4', '7.2.7', '14.9', '7.2.8.5', '8.2.5.13'] gate=CITATIONS_VERIFIED :: What protection does the Family Home Protection Act 1976 give to a non-owning spouse?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['7.2.9', '7.1'] retrieved=['7.2.9', '7.2.3.3', '7.2.6', '7.2.8.5', '7.2.8.5', '7.2.7'] gate=CITATIONS_VERIFIED :: When is a spouse's prior written consent required for a conveyance of the family home?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['8.2.5.7'] retrieved=['8.2.5.7', '5.10.6', '8.2.5.6', '5.6', '9.3', '15.3.16.4'] gate=CITATIONS_VERIFIED :: Why does a deed need a receipt clause?
- [direct] strict=MISS(rank=None) related=HIT(rank=2) expected=['9.6.1.1', '9.6.1'] retrieved=['3.2', '9.6', '9.6', '16.18.5.5', '2.2.1', '16.3.2.9'] gate=CITATIONS_VERIFIED :: What is an accountable trust receipt and when is it used?
- [direct] strict=HIT(rank=2) related=HIT(rank=2) expected=['10.1', '10.2'] retrieved=['10.4', '10.1', '10.2.4', '10.3', '10.1.2', '10.2.4'] gate=CITATIONS_VERIFIED :: What searches should a purchaser's solicitor carry out before completion, and why?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['10.2.3'] retrieved=['10.2.3', '10.6.2.3', '10.6.2.4', '10.2.3', '10.2.3', '10.6.3'] gate=CITATIONS_VERIFIED :: When is a bankruptcy search appropriate?
- [direct] strict=MISS(rank=None) related=MISS(rank=None) expected=['15.1'] retrieved=['1.6.2', '13.4.21', '15.3.14.5', '15.3.31.2', '15.3', '5.8.2'] gate=CITATIONS_VERIFIED :: What is the purpose of requisitions on title?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['16.13'] retrieved=['16.13', '16.13.1', '2.2.1.7', 'APPENDIX 16.1', '15.3.17', '14.6.13'] gate=CITATIONS_VERIFIED :: What is the current standard rate of Capital Gains Tax?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['16.4.5'] retrieved=['16.4.5', '16.6.1', '16.4.3', '16.4.2', '16.6.2', '12.5.2.4'] gate=CITATIONS_VERIFIED :: Is stamp duty payable on house contents such as carpets and curtains?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['13.4.8'] retrieved=['13.4.8', '13.4.3', '13.4.21', '14.6.12', '13.4.2', '1.9'] gate=CITATIONS_VERIFIED :: Can successive squatters add together their periods of adverse possession?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['APPENDIX 14.1', '14.12'] retrieved=['APPENDIX 14.1', '14.8.3.3', 'APPENDIX 14.1', '13.2', '14.12.4.2', '14.12.4.2'] gate=CITATIONS_VERIFIED :: Which wayleaves granted under the Gas Act 1976 affect registered land without registration?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['14.6.12', '14.8.3'] retrieved=['14.6.12', '14.8.3', '14.8.3.4', '14.8.3.2', '14.8.2', '14.8.3.7'] gate=CITATIONS_VERIFIED :: Why can a squatter not apply for a caution to protect their interest in registered land?
- [exact_token] strict=HIT(rank=2) related=HIT(rank=1) expected=['14.8.5'] retrieved=['14.8.5.2', '14.8.5', '10.2.9', '14.8', '14.2.11', '14.8.5.3'] gate=CITATIONS_VERIFIED :: How is a priority entry lodged in the Land Registry?
- [exact_token] strict=HIT(rank=2) related=HIT(rank=2) expected=['14.12', 'APPENDIX 14.1'] retrieved=['12.6.4', '14.12', '14.8.3.3', '14.12.4.2', '14.6.12', '13.3.7'] gate=CITATIONS_VERIFIED :: Which burdens affect registered land without registration under s.72?
- [exact_token] strict=HIT(rank=4) related=HIT(rank=4) expected=['14.15'] retrieved=['14.17.8', '14.13.2', '4.4', '14.15', '15.3.22.3', '13.4.21'] gate=CITATIONS_VERIFIED :: What is Form 60 used for?
- [exact_token] strict=HIT(rank=4) related=HIT(rank=4) expected=['13.3.4'] retrieved=['13.5', '13.4', '13.3.11', '13.3.4', '13.3.4', '13.3.12.3'] gate=CITATIONS_VERIFIED :: When is an application for first registration made on Form 1 rather than Form 2?
- [exact_token] strict=MISS(rank=None) related=HIT(rank=6) expected=['13.1'] retrieved=['16.3.2.10', '5.10.1', '1.7', '5.10.2', '10.2.7', '13.1.2'] gate=CITATIONS_VERIFIED :: What did the Registration of Deeds and Title Act 2006 change?
- [exact_token] strict=HIT(rank=5) related=HIT(rank=5) expected=['14.8.5'] retrieved=['13.4.15', '15.3.22.3', '11.4.2', '7.2.10.3', '14.8.5', '6.3.8'] gate=CITATIONS_VERIFIED :: What does paragraph 14.8.5 of the handbook deal with?
- [exact_token] strict=MISS(rank=None) related=MISS(rank=None) expected=['11.8.2.2'] retrieved=['11.8.1.14', '11.8.7.6', '10.2.4', '11.8.7.9', '11.8.7.2', '11.8.7.10'] gate=REFUSAL :: What does requisition 27.5 concern?
- [refusal] refused :: What are the legal grounds for divorce in Ireland?
- [refusal] refused :: What is the current national minimum wage in Ireland?
- [refusal] refused :: What is the penalty for careless driving under the Road Traffic Acts?
- [refusal] refused :: What sentence can be imposed for burglary in Ireland?
- [refusal] refused :: How many days of statutory annual leave is an employee entitled to in Ireland?

## held-out — per-question detail (hybrid)

- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['1.5.2'] retrieved=['1.5.2', '9.8.2', '9.7.2.5', '3.2.2', '2.4.2', '9.7.2.2'] gate=CITATIONS_VERIFIED :: In a residential purchase using the certificate-of-title system, what responsibilities does the purchaser's solicitor assume on completion for the mortgage, registration, and delivery of title documents to the lender?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['2.6'] retrieved=['2.6', '16.13.2.5', '16.2.6', '12.3.2.6', '13.4.21', '15.3.22'] gate=CITATIONS_VERIFIED :: For how long does the handbook recommend retaining a conveyancing file, and how is that period calculated?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['3.5.2'] retrieved=['3.5.2', '3.5.3', '3.5.1', '11.10.3', '5.9.2', '7.5'] gate=CITATIONS_VERIFIED :: How is responsibility shared under a co-decision-making agreement, and who supervises the co-decision-maker?
- [direct] strict=HIT(rank=6) related=HIT(rank=6) expected=['4.4.2'] retrieved=['13.3.15', '13.2.2', '4.9.4.3', '9.7.2.4', '4.5.1', '4.4.2'] gate=CITATIONS_VERIFIED :: Under the certificate-of-title guidelines, what minimum unexpired lease term is generally required for leasehold property?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['5.2.1'] retrieved=['5.2.1', '5.2', '5.10', '1.7', '5.2.2.1', '15.3.12'] gate=CITATIONS_VERIFIED :: What is the distinction between actual, constructive, and imputed notice when investigating title?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['5.9.4'] retrieved=['5.9.4', '5.14', '14.17.5', '14.17.4', '10.2.2', '15.3.12'] gate=CITATIONS_VERIFIED :: When property is being sold by a receiver appointed under a debenture, what should be checked to establish that the receiver was validly appointed?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['6.2.11'] retrieved=['6.2.11', '11.13', '6.4.3.6', '11.13', '15.3.1', '12.6.2'] gate=CITATIONS_VERIFIED :: What BER certificate and advisory-report information must be produced when a dwelling is offered for sale or letting?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['7.5'] retrieved=['7.5', '7.5', '7.5', '7.2.8.4', '7.2.4', '7.2.5'] gate=CITATIONS_VERIFIED :: What minimum cohabitation periods apply when deciding whether a person is a qualified cohabitant, both with and without dependent children?
- [exact_token] strict=HIT(rank=1) related=HIT(rank=1) expected=['APPENDIX 7.1'] retrieved=['APPENDIX 7.1', '7.2.7', '7.2.9', '7.2.9', '7.2.4', '7.2.8'] gate=CITATIONS_VERIFIED :: Which family law declaration form applies where property is a shared home owned by only one civil partner?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['8.5.10'] retrieved=['8.5.10', '8.3', '5.8.2', '14.7', '15.3.14.1', '8.5.11'] gate=CITATIONS_VERIFIED :: In the specimen voluntary Land Registry transfer, how is a transferor's lifetime right of residence, maintenance, and support preserved?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['9.12'] retrieved=['9.12', '9.7.2', '9.7.2', '9.4.3', '1.5.2', '9.6.2'] gate=CITATIONS_VERIFIED :: What does a purchaser's solicitor typically undertake to a bridging lender about the title deeds and the use and repayment of the bridging funds?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['10.5'] retrieved=['10.5', '15.3.22.4', '15.3.13.2', '10.2.8', '10.1.2.2', '12.7.2'] gate=CITATIONS_VERIFIED :: If an act found on a closing search does not affect the property being sold, what explanation must the vendor's solicitor provide beyond simply saying that it does not affect the property?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['11.11.2'] retrieved=['11.11.2', '11.5', '11.8.1.13', '11.5.1.6', '11.11.2', '11.11.2'] gate=CITATIONS_VERIFIED :: What is the standard duration of a planning permission, and from which event does that period run?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['11.6.8'] retrieved=['11.6.8', '11.11.2', '11.5.1.6', '11.5', '11.5', '11.9.7'] gate=CITATIONS_VERIFIED :: When may an invalid planning condition be severed without causing the entire planning permission to fail?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['12.11.3'] retrieved=['12.11.3', '12.4', '12.3.2.15', '12.3.4.4', '12.3.2.9', '12.11.6'] gate=CITATIONS_VERIFIED :: What time obligation does the purchaser protection pledge place on the purchaser's solicitor after the builder issues the contract?
- [direct] strict=HIT(rank=1) related=HIT(rank=1) expected=['13.2.5'] retrieved=['13.2.5', '1.7.3.4', '14.8.5.2', '13.2.2', '4.9.5', '13.2.2'] gate=CITATIONS_VERIFIED :: What is the effect if a conveyance requiring compulsory first registration is not registered within the prescribed period?
- [exact_token] strict=HIT(rank=1) related=HIT(rank=1) expected=['13.6'] retrieved=['13.6', '14.8', '14.8.3', '14.8.2.3', '14.8.3.7', '14.1.9.6'] gate=CITATIONS_VERIFIED :: Which Land Registry forms are used to apply for and support a caution against first registration?
- [exact_token] strict=HIT(rank=1) related=HIT(rank=1) expected=['14.14.5'] retrieved=['14.14.5', '14.14.7', '9.9', '14.14.4', '14.14.6', '14.14'] gate=CITATIONS_VERIFIED :: What information and supporting evidence must accompany Form 57B when a paid charge cannot be released because its owner has died and no representation has been raised?
- [exact_token] strict=HIT(rank=1) related=HIT(rank=1) expected=['15.3.35'] retrieved=['15.3.35', '16.2.7', '16.4.5.1', '15.3.9', '15.3.10.3', '16.4.5.1'] gate=CITATIONS_VERIFIED :: What information does Requisition 44 seek about a property's entry on the vacant-sites register and liability for the vacant-site levy?
- [exact_token] strict=HIT(rank=3) related=HIT(rank=3) expected=['APPENDIX 16.3'] retrieved=['16.8.1', '16.8.3', 'APPENDIX 16.3', '16.9.3', '16.2.2', '5.8.2'] gate=CITATIONS_VERIFIED :: On the Record of Clients' Instructions on Stamp Duty, what valuation must be supplied when a transfer is voluntary or partly voluntary and the full purchase price is not paid?
- [refusal] refused :: What minimum notice period must a landlord give under the Residential Tenancies Acts when terminating a tenancy because the property is being sold?
- [refusal] refused :: What time limit applies to an application for judicial review of a planning decision, and what leave requirements must the applicant satisfy?
- [refusal] refused :: How is compensation assessed, and what appeal or arbitration procedure applies, when land is acquired under a compulsory purchase order?
- [refusal] refused :: How is open-market rent determined under a commercial lease rent-review clause, including the usual assumptions and disregards?
- [refusal] refused :: What are the stages of the Mortgage Arrears Resolution Process, and how may a borrower appeal a proposed alternative repayment arrangement?
- [refusal] refused :: How can a consumer complain to the Property Services Regulatory Authority about an auctioneer or estate agent, and when can compensation be claimed from its compensation fund?
- [refusal] refused :: How should a solicitor respond to a data-subject access request for a conveyancing file, including the response deadline and applicable exemptions?
- [refusal] refused :: What eligibility, discount, and clawback rules apply when a local-authority tenant buys a dwelling under the incremental tenant-purchase scheme?

