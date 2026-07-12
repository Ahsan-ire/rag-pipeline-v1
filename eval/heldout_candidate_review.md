# Held-Out Evaluation Candidate Review

**Status:** Draft candidate pool - not selected, not frozen, and not used in any retrieval or generation run.

**Source:** *Conveyancing Handbook*. Page references below are the handbook's printed page numbers, not raw PDF page indexes.

## Method

- Parsed the handbook using the existing page-aware ingestion and structural chunking code only. No project vector store, BM25 retrieval, pipeline answer-generation call, or eval runner was used.
- Excluded exact and dotted-related locators already represented in `eval/golden_set.jsonl`.
- Read the underlying passage for every positive candidate and kept questions with a localized, independently identifiable answer.
- Searched every negative topic with several terms across a separate text extraction of all 808 PDF pages. Relevant hits were inspected and are disclosed below.
- Visually checked representative source pages across the PDF, including both appendix tables, to confirm printed-page mapping and layout.

## Positive candidate pool

The suggested type is provisional. Prefer questions with one expected locator in the final set because the current evaluator treats a list of expected sections as alternatives rather than requiring all of them.

| ID | Suggested type | Candidate question | Expected locator | Printed page(s) | Evidence target |
| --- | --- | --- | --- | --- | --- |
| P01 | direct | In a residential purchase using the certificate-of-title system, what responsibilities does the purchaser's solicitor assume on completion for the mortgage, registration, and delivery of title documents to the lender? | `1.5.2` | 4 | Purchaser's solicitor investigates title, completes and registers the borrower's title and mortgage, and certifies title to the lender. |
| P02 | direct | Why should the Law Society's standard family law declarations be reviewed for the particular transaction instead of being used without amendment? | `1.6.3` | 7 | The standard declarations may need transaction-specific amendment and evolve with family-law legislation. |
| P03 | direct | For how long does the handbook recommend retaining a conveyancing file, and how is that period calculated? | `2.6` | 35 | Recommended minimum is derived from the liability period for sealed documents plus time for service of proceedings. |
| P04 | direct | Which tax-related checks appear in the notes to the handbook's sample financial memorandum for a property purchase? | `2.8` | 36-37 | Notes address VAT, possible stamp-duty relief or exemption, and CGT clearance or deduction. |
| P05 | direct | Does the exception to the conflict-of-interest prohibition allow one solicitor to act on a transfer of a family home from joint names into one spouse's sole name? | `3.3` | 48 | The limited joint-name exception does not extend to a transfer from joint ownership into one sole name. |
| P06 | direct | How is responsibility shared under a co-decision-making agreement, and who supervises the co-decision-maker? | `3.5.2` | 52 | Decisions are made jointly and the co-decision-maker is supervised by the Decision Support Service. |
| P07 | direct | Does a contract term restricting investigation of part of the vendor's title remove the vendor's obligation to disclose a latent title defect? | `4.6` | 63 | A restriction on investigation does not relieve the vendor of the duty to disclose known or discoverable latent defects. |
| P08 | direct | Under the certificate-of-title guidelines, what minimum unexpired lease term is generally required, and what classes of Land Registry leasehold title are acceptable? | `4.4.2` | 60-61 | The passage specifies the general minimum term and acceptable Land Registry leasehold title classes. |
| P09 | direct | What is the distinction between actual, constructive, and imputed notice when investigating title? | `5.2.1` | 73 | The passage separately defines knowledge, knowledge discoverable by proper inquiry, and knowledge attributed through an agent. |
| P10 | direct | When property is being sold by a receiver appointed under a debenture, what should be checked to establish that the receiver was validly appointed? | `5.9.4` | 85-86 | The default event and appointment must comply with the wording and formal requirements of the debenture. |
| P11 | direct | What BER certificate and advisory-report information must be produced when a dwelling is offered for sale or letting? | `6.2.11` | 114 | The passage addresses production to an interested purchaser or tenant and provisional/final certificates for plan-based sales. |
| P12 | direct | Who bears the risk of loss or damage to the property between the date of sale and completion, and what limits that liability? | `6.7.4` | 142-143 | The vendor remains liable under the general conditions, subject to a purchase-price cap and further limitations. |
| P13 | direct | What minimum cohabitation periods apply when deciding whether a person is a qualified cohabitant, both with and without dependent children? | `7.5` | 202-203 | The definition uses different minimum periods depending on whether the couple are parents of a dependent child. |
| P14 | exact_token | Which family law declaration form applies where property is a shared home owned by only one civil partner? | `APPENDIX 7.1` | 205-206 | The appendix table maps that civil-partner scenario to a specific numbered form. |
| P15 | direct | How is priority between deeds now determined in the Registry of Deeds, and what must an application for registration contain? | `8.4.1` | 221 | Priority follows the application's serial number; the passage lists the application form, original deed, and fee. |
| P16 | direct | In the specimen voluntary Land Registry transfer, how is a transferor's lifetime right of residence, maintenance, and support preserved? | `8.5.10` | 231 | The transfer is expressed to be subject to and charged with that lifetime right, registered as a burden. |
| P17 | exact_token | When is the eDischarge system available for a mortgage, and in which cases is a paper discharge still required? | `9.3` | 239-240 | eDischarge applies to qualifying entire discharges of registered charges; the passage identifies excluded cases. |
| P18 | direct | What does a purchaser's solicitor typically undertake to a bridging lender about the title deeds and the use and repayment of the bridging funds? | `9.12` | 273 | The solicitor holds title documents to the lender's order, applies funds to the purchase, and clears bridging from the later loan. |
| P19 | direct | If an act found on a closing search does not affect the property being sold, what explanation must the vendor's solicitor provide beyond simply saying that it does not affect the property? | `10.5` | 311 | The solicitor should identify what property the act does affect or show reasonable grounds for the conclusion, then sign and date the explanation. |
| P20 | direct | What is the standard duration of a planning permission, and from which event does that period run? | `11.11.2` | 377-378 | Standard duration is measured from the grant rather than the notification of the decision, subject to stated exceptions. |
| P21 | direct | When may an invalid planning condition be severed without causing the entire planning permission to fail? | `11.6.8` | 333 | The answer turns on whether the condition is peripheral or insignificant versus an essential planning feature. |
| P22 | direct | Which categories of professional does the handbook say may currently provide an acceptable certificate or opinion on planning compliance? | `11.10.3` | 370-371 | The passage lists registered architects and specified experienced or qualified architects, engineers, surveyors, and recognized EU professionals. |
| P23 | direct | When closing the purchase of a new house on registered land, how should a pending Land Registry dealing be handled instead of requesting a blanket omnibus letter? | `12.7.2` | 420 | The vendor's solicitor should explain whether and how the pending dealing affects the property; blanket certification is not generally required. |
| P24 | direct | What time obligation does the purchaser protection pledge place on the purchaser's solicitor after the builder issues the contract? | `12.11.3` | 428-429 | The completed contract and balance deposit are to be returned within the stated period. |
| P25 | direct | What is the effect if a conveyance requiring compulsory first registration is not registered within the prescribed period? | `13.2.5` | 439 | The passage states the registration period, absence of vested title before registration, and the purchaser's intervening equitable position. |
| P26 | exact_token | What are Forms 7 and 8 used for in a caution against first registration, and what protection does the caution give the cautioner? | `13.6` | 481-482 | Form 7 is the application, Form 8 supplies affidavit evidence, and the cautioner receives notice of a first-registration application. |
| P27 | direct | Does a Land Registry map normally establish a property's exact legal boundary, and how can a boundary be made conclusive? | `14.2.11` | 492-493 | Registry maps usually show general boundaries; conclusiveness requires the prescribed agreement/order and folio entry. |
| P28 | exact_token | What information and supporting evidence must accompany Form 57B when a paid charge cannot be released because its owner has died and no representation has been raised? | `14.14.5` | 548-549 | The application covers death, lack of representation, payment evidence or corroboration, and successors' details. |
| P29 | exact_token | What information does Requisition 44 seek about a property's entry on the vacant-sites register and liability for the vacant-site levy? | `15.3.35` | 600 | It asks about registration, levy and discharge, intended entry, notices or demands, and appeals. |
| P30 | exact_token | On the Record of Clients' Instructions on Stamp Duty, what valuation must be supplied when a transfer is voluntary or partly voluntary and the full purchase price is not paid? | `APPENDIX 16.3` | 737 | The appendix requires an open-market value supported by professional valuation in that situation. |

## Near-domain negative candidate pool

These questions are intended to test refusal when the handbook contains tempting adjacent vocabulary but not enough information to answer. "High" confidence means the targeted procedure or rule was not found after multiple searches. "Medium" means the handbook mentions the subject and points elsewhere, making it a harder but slightly less clean negative.

| ID | Candidate refusal question | Full-corpus searches and inspected hits | Confidence | Review recommendation |
| --- | --- | --- | --- | --- |
| N01 | What minimum notice period must a landlord give under the Residential Tenancies Acts when terminating a tenancy because the property is being sold? | Searched `Residential Tenancies Board`, `notice of termination`, `Part 4 tenancy`, `rent pressure zone`, `RTB`, `notice period`, and termination-period variants. Sections `10.2.11` and `15.3.10` mention RTB searches, notices, disputes, and requisitions but expressly defer detailed tenancy law elsewhere; no sale-termination period was found. | Medium | Good adversarial candidate, but retain only if you want negatives with partial topical mentions. |
| N02 | What documents and procedural steps must an executor complete to apply to the Probate Office for a grant of probate? | Searched `Probate Office`, `grant of probate`, `probate application`, `statement of affairs`, form and executor-oath variants. The handbook discusses the conveyancing effect of grants and proving executors (`14.7.8.3`) but does not set out the Probate Office application process. | Medium | Useful near-domain hard negative; source mentions grants frequently, so manual review is important. |
| N03 | What time limit applies to an application for judicial review of a planning decision, and what leave requirements must the applicant satisfy? | Searched `judicial review`, planning/JR combinations, `Order 84`, `leave`, `eight weeks`, and `8 weeks`. The only Order 84/JR passage concerns review of PRA decisions (`14.1.5`); planning passages do not give the requested JR deadline or leave test. | High | Strong lexical-confusion negative. |
| N04 | How is compensation assessed, and what appeal or arbitration procedure applies, when land is acquired under a compulsory purchase order? | Searched `compulsory purchase order`, `CPO`, `property arbitrator`, compensation-assessment, disturbance-compensation, and arbitration variants. The handbook says a planning search may reveal a CPO or compensation payment (`10.2.4`) but does not explain valuation or dispute procedure. | High | Strong near-domain negative. |
| N05 | How is open-market rent determined under a commercial lease rent-review clause, including the usual assumptions and disregards? | Searched `rent review`, `open market rent`, `upward only`, formula/mechanism, assumptions, and disregards. Sections `15.3.9` and `16.6.1` mention rent-review documentation and stamp duty but not valuation mechanics. | High | Strong lexical-confusion negative. |
| N06 | When must a solicitor submit a suspicious transaction report, through which reporting system, and what tipping-off restrictions apply? | Searched `money laundering`, `suspicious transaction report`, `goAML`, `FIU`, `tipping off`, and reporting variants. Section `2.2` states that AML procedures are essential and refers readers to separate Law Society guidance; no reporting rules were found. | High | Strong negative because the handbook deliberately delegates the detailed answer elsewhere. |
| N07 | What are the stages of the Mortgage Arrears Resolution Process, and how may a borrower appeal a proposed alternative repayment arrangement? | Searched `MARP`, `Mortgage Arrears Resolution Process`, `CCMA`, standard financial statement, not-co-operating borrower, arrears appeal, and alternative-repayment terms. Section `9.14` mentions the CCMA and enforcement moratorium at a high level but does not describe MARP stages or appeals. | Medium-High | Good hard negative; inspect wording to keep it beyond the high-level CCMA material actually present. |
| N08 | How can a consumer complain to the Property Services Regulatory Authority about an auctioneer or estate agent, and when can compensation be claimed from its compensation fund? | Searched the authority's full name, `PSRA`, licensed property-service-provider terms, complaints, and compensation-fund terms. No substantive matches were found. | High | Clean near-domain negative. |
| N09 | What inspections and documents must a landlord complete before a property can qualify for Housing Assistance Payment? | Searched `Housing Assistance Payment`, `HAP`, HAP landlord/tenancy/inspection variants, and related qualification wording. No substantive matches were found. | High | Clean property-adjacent negative. |
| N10 | How should a solicitor respond to a data-subject access request for a conveyancing file, including the response deadline and applicable exemptions? | Searched `GDPR`, `Data Protection Act 2018`, `data subject access`, `subject access request`, Article 15, deadlines, and exemptions. Data-protection wording appears in the 2023 Conditions of Sale appendix, but the requested file-access procedure is absent. | High | Strong lexical-confusion negative. |
| N11 | How is a construction-contract payment dispute referred to statutory adjudication, and within what period must the adjudicator decide it? | Searched `Construction Contracts Act`, adjudicator/adjudication, construction payment dispute, referral, and decision-period variants. No substantive matches were found. | High | Clean new-house/construction-adjacent negative. |
| N12 | What eligibility, discount, and clawback rules apply when a local-authority tenant buys a dwelling under the incremental tenant-purchase scheme? | Searched `tenant purchase scheme`, `incremental tenant purchase`, eligibility, housing allocation, discount, and clawback variants. No substantive matches were found. | High | Clean housing/conveyancing-adjacent negative. |

## Suggested selection process

1. Select 15-20 positives, keeping broad chapter coverage and at least two appendix/exact-token cases.
2. Prefer single-locator questions. Reword or reject anything that feels compound, overly leading, or unlike a question you would naturally ask.
3. Select 5-8 negatives. A balanced set would use mostly high-confidence negatives plus one or two harder partial-mention cases such as N01, N02, or N07.
4. Do not run retrieval while reviewing. Once wording and expected locators are final, convert only the selected rows to JSONL, schema-check them, record the SHA-256, and freeze the set before its first eval run.

## Frozen selection (12 Jul 2026)

Selected after corpus-only verification (metadata lookups + text grep; NO retrieval, similarity
search, or generation touched any candidate before freezing):

- **Positives (20):** P01, P03, P06, P08*, P09, P10, P11, P13, P14, P16, P18, P19, P20, P21, P24,
  P25, P26*, P28, P29, P30. (*Reworded to single-part: P08 keeps only the minimum-term question;
  P26 asks only which forms apply.) Dropped for phrasing quality, not content: P02/P23 (leading),
  P05/P07 (yes-no framing), P04 (OCR-jumbled memo table), P12/P27 (mild compounds, chapters already
  covered), P15 (compound), P17/P22 (chapter coverage duplicates).
- **Negatives (8):** N01, N03, N04, N05, N07, N08, N10, N12 — six high-confidence + two harder
  partial-mention cases (N01, N07), every absence claim independently re-verified against a fresh
  full-text extraction before selection.
- Verification found: all 30 positive locators exist as exact `section_number` chunks; printed-page
  agreement exact on all 30; zero overlap (equality or dotted nesting, both directions) with the 37
  tuning-set locators in `eval/golden_set.jsonl`.

**Frozen file:** `eval/heldout_set.jsonl` — 28 lines, schema-validated via `load_golden_set`.
**SHA-256:** `601a81c0a3e36aa5d90afb7904fdebad7704d3fff506871c0b9032d7576dcfe6`
No retrieval or generation run may touch this file's questions until Phase 10, and nothing is ever
tuned on its results.
