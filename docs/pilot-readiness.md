# Pilot readiness: Phase 20 entry conditions

Source: docs/designs/003-roadmap-and-next-actions.md, Phase 20 row and §7; D71 (Q3).

The internal pilot (colleagues using the tool) starts only when **every** condition below is
met. These are entry conditions, not exit criteria. Owner items are D64 hard stops: Claude never
marks them done; the owner confirms each one and Claude records the date. Evidence that is
private (emails, contracts, policy text) stays out of this public repo; only a pointer is noted.

Colleague queries reach the eval sets only through the pilot's opt-in feedback into candidate
review, with client identifiers removed (D71 Q3). Raw queries are never logged.

| # | Condition | Owner of the action | What has to happen | Status |
|---|---|---|---|---|
| 1 | Licence position confirmed (003 §7) | Owner | Send the Law Society email (the nine questions in 003 §7: indexing a purchased copy, showing excerpts, sending excerpts to an API processor, an AI/RAG licence, tutorial-derived eval data). Get a written answer. | Open: email not sent yet |
| 2 | Anthropic DPA, and ZDR if needed, confirmed for the actual organisation, model and features | Owner; Claude researches the terms | Decide which legal entity holds the API account. Confirm the DPA covers that entity. Decide whether zero data retention is needed and request it. Check that every API feature the pilot uses is covered. | Open: Claude is checking (12 Oct) whether a company or Enterprise plan is needed |
| 3 | Firm AI-policy sign-off (Law Society GenAI guidelines v4, Dec 2025) | Claude drafts; owner adapts; firm signs | Claude drafts the policy when a pilot date is in sight, during Phase 18–19. The owner tailors it and gets it signed off. | Not started (deliberately late) |
| 4 | Q4 bars agreed and met | Agreed by owner 9 Oct; Claude measures | Handbook mode: ≤1% unsupported claims on the adjudicated set, p95 latency ≤ 20 s, ≤ €0.05 per query. Measured on the 16A eval sets before the pilot. | Agreed; not yet measured |
| 5 | Operator named | Owner | The owner is the operator (Q5, 9 Oct). A backup operator is still to be named. | Partly done |
| 6 | Named-user authentication | Claude builds (Phase 18 service, Phase 20) | Each colleague signs in as themselves; no shared login. | Open |
| 7 | Raw-query logging removed | Claude builds (Phase 18 tracing) | Queries are traced by keyed HMAC, never stored as text. Colleague questions enter eval review only when a user submits them as feedback. | Open |
| 8 | Backup and restore tested | Claude builds; owner watches one restore | Back up the index release and config, restore them on a clean machine, and record the result. | Open |

**Route to the pilot** (design 003 §9, under D64): 16A-2 eval data → 16B retrieval experiments →
17a answer integrity → 17b model migration → 18 service, index releases and tracing → 19 tools
and research mode → **20 pilot**. Items 6–8 are built along the way. Items 1–3 run in parallel
on the owner's side.
