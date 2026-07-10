# Legal RAG Evaluation Report

- Date: 2026-07-09T23:53:59.891648
- Golden set: eval/golden_set.jsonl
- top_k: 6

## Retrieval (hit@6)

Overall hit rate: 27/30 = 0.900

| Type | Hits | Total | Hit rate |
| --- | --- | --- | --- |
| direct | 21 | 23 | 0.913 |
| exact_token | 6 | 7 | 0.857 |

## Refusals

Refusal accuracy: 5/5 = 1.000

## Per-question detail

- [direct] HIT expected=['1.1'] retrieved=['2.3.1.6', '14.3.2', '1.1', '11.4.2', '12.5.2.3', '4.4'] :: What is conveyancing?
- [direct] HIT expected=['1.7.2.3', '1.7.2'] retrieved=['10.2.7', '1.7.2.2', '1.7.2.3', '13.3.6', '13.4.21', '13.1.3'] :: How does searching on the Index of Names work in the Registry of Deeds?
- [direct] HIT expected=['1.8'] retrieved=['1.8', '1.7.3', '1.7.3.6', '13.1', '4.9', '8.4.3'] :: What are the essential differences between the Registry of Deeds and the Land Registry systems?
- [direct] HIT expected=['2.1', '2.2'] retrieved=['2.1', '2.3.1.6', '1.5', '11.6.3', '2.2.2.9', 'APPENDIX 6.1'] :: In what order do the steps of a typical conveyancing transaction occur?
- [direct] HIT expected=['3.4'] retrieved=['3.4', '16.9.7', '8.5.11', '3.4.3', '14.9.4', '8.2.5.13'] :: What special considerations apply to voluntary deeds transferring property between family members?
- [direct] HIT expected=['3.2.3.7', 'APPENDIX 3.1'] retrieved=['9.8', '9.8.4', '9.8', '9.8', '9.7.2.2', '3.2.3.7'] :: What rules govern a solicitor giving an undertaking to a lender in a commercial property transaction?
- [direct] HIT expected=['4.5.1'] retrieved=['4.5.1', '4.7.4', '13.3', '4.8.1', '4.7', '13.3.2.1'] :: How long must a good root of title be under an open contract?
- [direct] HIT expected=['4.9.2'] retrieved=['4.9.2', '14.2.8.3', '10.2.4', '14.2.4', '10.2.4', '14.2'] :: Which three registers does the Land Registry maintain?
- [direct] MISS expected=['5.8'] retrieved=['14.7', '5.9.11', '14.7.6', '14.6.8', '14.7.2', '5.14'] :: How does ownership held as tenants in common devolve on the death of one owner?
- [direct] HIT expected=['6.1'] retrieved=['6.1', '1.5.3', '6.1', '2.2.1.16', '2.2.1.16', '6.2.9'] :: What form must a contract for the sale of land take in order to be enforceable?
- [direct] HIT expected=['6.3.2', 'APPENDIX 6.1'] retrieved=['6.3.2.2', '6.5', '6.5', '6.6', '12.11.2', '12.3.5.16'] :: Who holds the deposit paid under a contract for sale, and in what capacity?
- [direct] HIT expected=['7.1', '7.2'] retrieved=['7.2', '7.4', '7.2.7', '14.9', '7.2.8.5', '8.2.5.13'] :: What protection does the Family Home Protection Act 1976 give to a non-owning spouse?
- [direct] HIT expected=['7.2.9', '7.1'] retrieved=['7.2.9', '7.2.3.3', '7.2.6', '7.2.8.5', '7.2.8.5', '7.2.7'] :: When is a spouse's prior written consent required for a conveyance of the family home?
- [direct] HIT expected=['8.2.5.7'] retrieved=['8.2.5.7', '5.10.6', '8.2.5.6', '5.6', '9.3', '15.3.16.4'] :: Why does a deed need a receipt clause?
- [direct] HIT expected=['9.6.1.1', '9.6.1'] retrieved=['3.2', '9.6', '9.6', '16.18.5.5', '2.2.1', '16.3.2.9'] :: What is an accountable trust receipt and when is it used?
- [direct] HIT expected=['10.1', '10.2'] retrieved=['10.4', '10.1', '10.2.4', '10.3', '10.1.2', '10.2.4'] :: What searches should a purchaser's solicitor carry out before completion, and why?
- [direct] HIT expected=['10.2.3'] retrieved=['10.2.3', '10.6.2.3', '10.6.2.4', '10.2.3', '10.2.3', '10.6.3'] :: When is a bankruptcy search appropriate?
- [direct] MISS expected=['15.1'] retrieved=['1.6.2', '13.4.21', '15.3.14.5', '15.3.31.2', '15.3', '5.8.2'] :: What is the purpose of requisitions on title?
- [direct] HIT expected=['16.13'] retrieved=['16.13', '16.13.1', '2.2.1.7', 'APPENDIX 16.1', '15.3.17', '14.6.13'] :: What is the current standard rate of Capital Gains Tax?
- [direct] HIT expected=['16.4.5'] retrieved=['16.4.5', '16.6.1', '16.4.3', '16.4.2', '16.6.2', '12.5.2.4'] :: Is stamp duty payable on house contents such as carpets and curtains?
- [direct] HIT expected=['13.4.8'] retrieved=['13.4.8', '13.4.3', '13.4.21', '14.6.12', '13.4.2', '1.9'] :: Can successive squatters add together their periods of adverse possession?
- [direct] HIT expected=['APPENDIX 14.1', '14.12'] retrieved=['APPENDIX 14.1', '14.8.3.3', 'APPENDIX 14.1', '13.2', '14.12.4.2', '14.12.4.2'] :: Which wayleaves granted under the Gas Act 1976 affect registered land without registration?
- [direct] HIT expected=['14.6.12', '14.8.3'] retrieved=['14.6.12', '14.8.3', '14.8.3.4', '14.8.3.2', '14.8.2', '14.8.3.7'] :: Why can a squatter not apply for a caution to protect their interest in registered land?
- [exact_token] HIT expected=['14.8.5'] retrieved=['14.8.5.2', '14.8.5', '10.2.9', '14.8', '14.2.11', '14.8.5.3'] :: How is a priority entry lodged in the Land Registry?
- [exact_token] HIT expected=['14.12', 'APPENDIX 14.1'] retrieved=['12.6.4', '14.12', '14.8.3.3', '14.12.4.2', '14.6.12', '13.3.7'] :: Which burdens affect registered land without registration under s.72?
- [exact_token] HIT expected=['14.15'] retrieved=['14.17.8', '14.13.2', '4.4', '14.15', '15.3.22.3', '13.4.21'] :: What is Form 60 used for?
- [exact_token] HIT expected=['13.3.4'] retrieved=['13.5', '13.4', '13.3.11', '13.3.4', '13.3.4', '13.3.12.3'] :: When is an application for first registration made on Form 1 rather than Form 2?
- [exact_token] HIT expected=['13.1'] retrieved=['16.3.2.10', '5.10.1', '1.7', '5.10.2', '10.2.7', '13.1.2'] :: What did the Registration of Deeds and Title Act 2006 change?
- [exact_token] HIT expected=['14.8.5'] retrieved=['13.4.15', '15.3.22.3', '11.4.2', '7.2.10.3', '14.8.5', '6.3.8'] :: What does paragraph 14.8.5 of the handbook deal with?
- [exact_token] MISS expected=['11.8.2.2'] retrieved=['11.8.1.14', '11.8.7.6', '10.2.4', '11.8.7.9', '11.8.7.2', '11.8.7.10'] :: What does requisition 27.5 concern?
- [refusal] refused :: What are the legal grounds for divorce in Ireland?
- [refusal] refused :: What is the current national minimum wage in Ireland?
- [refusal] refused :: What is the penalty for careless driving under the Road Traffic Acts?
- [refusal] refused :: What sentence can be imposed for burglary in Ireland?
- [refusal] refused :: How many days of statutory annual leave is an employee entitled to in Ireland?
