# 16A-2b b5: owner guide to the validation sheets

Written 2 Nov 2026. This page explains how to validate the eval questions for 16A-2b. Keep it open while you work on a
sheet. Plan step: `IMPLEMENTATION_PLAN.md` §16A-2b, b5. Decisions: D75.

## 1. Where things stand

- The new eval data is built and split (b1a to b4). It has two sides:
  - **Sealed exam:** 60 families, 120 rows (38 answer, 4 partial, 18 refuse; 2 phrasings each). Claude never sees it.
  - **Development:** 136 families. 255 of its rows need your review. Claude may read these.
- **Nothing has been validated yet.** Your sheets are the blocker. Everything after them waits: recheck, round 2,
  freeze, the baseline draws, slot 0, and the `v2.5.0` tag.
- A **family** is one question scenario. Each sheet line is one **phrasing** of it, i.e. one way of asking the same
  question.

## 2. What you need to do, in order

| # | Task | Time | Who runs the tool |
|---|---|---|---|
| 1 | Fill the **sealed** sheet (the critical path) | ≈ 7 h | You fill it; Claude applies it (counts only) |
| 2 | Forward any escalation forms to your colleague and log them | 15 min each | Claude writes the form; you send it |
| 3 | Tick the confirm sheet for the rows you corrected | ≈ 0.5 h | Claude makes it and applies it |
| 4 | Fill the **development** sheet, starting from Claude's pre-check | ≈ 7 h | Claude applies it |
| 5 | Validate round-2 replacement families, if any | ≈ 1.2 h | as in 1 |
| 6 | Approve each € amount for the live runs (probe ≤ €3, draws ≈ €3.5, slot 0 planned €14) | minutes | Claude runs them |

Work in sessions of about 1.5 h. After each session, tell Claude "sealed batch done" (or "development batch done").

## 3. The sealed sheet: rules

File: `eval/private/sealed/work/sheets/sheet_sealed_all.csv`

- **Only you see it.**
  - Open it yourself, outside any Claude session.
  - Never paste, attach, upload, screenshot or email it, or any line of it, to Claude, Codex or any other AI or chat
    tool.
- **Edit it where it is.** Don't copy it to Desktop, Documents, Downloads or a cloud folder: iCloud or OneDrive may sync
  those.
- **Edit only these columns:** `A`, `C`, `R`, `E`, `corrections`, `doubt`, `colleague_key`.
  - Don't sort, reorder, add or delete rows or columns.
  - Don't touch `row_hash`: every approval is bound to it.
- **Save as CSV (UTF-8)** under the same name.
  - Excel: *File → Save As → CSV UTF-8*.
  - Numbers: *File → Export To → CSV*, Text Encoding *Unicode (UTF-8)*, then replace the original file.
- **Turn off smart quotes before you type JSON.**
  - macOS: *System Settings → Keyboard → Text Input → Edit… → Use smart quotes and dashes*, off.
  - Excel: *Tools → AutoCorrect → AutoFormat As You Type*, untick *"Straight quotes" with "smart quotes"*.
  - Curly quotes break the JSON, and the tool then refuses the whole sheet.

## 4. Reading a line

| Column | Meaning |
|---|---|
| `phrasing_no`, `phrasing` | Which phrasing (0, 1, …) and its text |
| `scope` | `answer`: the Handbook answers it. `partial`: the Handbook answers part of it (`gaps` names the rest). `refuse`: the Handbook doesn't deal with it, or it needs advice beyond the Handbook |
| `register` | `modelled` = formal, well-formed question. `lay` = a non-lawyer's wording. `terse` = keyword-style search |
| `source` | Where the family came from: `handbook`, `tutorial`, `colleague` or `legacy` |
| `evidence` | The Handbook paragraphs a correct answer relies on, with page numbers to look them up. `&` separates groups that are **all needed**. `\|` separates alternatives where **any one will do**. Example: `4.5.1 [p62] \| 4.5.2 [p63] & 5.1 [p70]` means (4.5.1 or 4.5.2) and 5.1 |
| `gaps` | Partial only: the parts the Handbook does **not** cover, as `id: keyword, keyword` |
| `ambiguous` | `yes` if two reasonable readings lead to different answers |

## 5. How to review one family, step by step (about 7 min)

### Before you start

1. **Open the Handbook PDF** (`data/Conveyancing_Handbook.pdf`) next to the sheet.
   - The page numbers in the sheet are the Handbook's printed page numbers.
   - In the PDF, the matching page is the **printed page + 44**. For example, `[p62]` is PDF page 106.
   - Or search (Cmd+F) for the paragraph number.
2. **The rows are sorted by family**, so the phrasings of a family sit together. Review a whole family at a time.
3. **The labels were drafted by a model.** Treat them as a suggestion to check, not as an answer to confirm.

### For each family

1. **Read the phrasings first, and cover the labels.** Work out what is asked:
   - the facts given;
   - the legal question;
   - how many distinct parts ("limbs") it has.

   Then form your own view of where the Handbook deals with it. Looking at the labels second stops you anchoring on
   them.
2. **Check the phrasings.**
   - Each phrasing asks the same question, with the same facts and the same limbs. Look for a phrasing that adds or
     drops a condition, a fact or a limb.
   - Each one reads naturally for its register:
     - `lay`: how a client or another non-lawyer would ask;
     - `modelled`: how a practitioner or trainee would write it;
     - `terse`: search-box keywords.
   - No phrasing gives the answer away.
   - A phrasing that drifts gets `C` with `{"question": "..."}` on that line. If the whole family is muddled, `R`.
3. **Read the cited paragraphs** in the Handbook. Do they answer the question as asked? Then search the Handbook
   (Cmd+F on the key terms, and the chapter contents) for any other place that answers it.
4. **Decide the scope.** Judge by **what this Handbook says**, not by current law or your own knowledge. Handbook mode
   (D61) answers from the Handbook alone, and these labels measure that mode. Ask:

   | Can you write a complete, correct answer citing only Handbook paragraphs? | Scope |
   |---|---|
   | Yes, every part of the question | `answer` |
   | Only part of it, and you can name the missing part | `partial` (the missing part becomes a gap) |
   | No: the Handbook doesn't deal with it, or answering needs something it doesn't give (a specific figure or fee, a judgment on the client's particular facts, another area of law) | `refuse` |

   - A wrong `refuse` is the costliest error, so search properly before you accept one.
   - A question that touches the Handbook's topic only in passing is still `refuse` if the Handbook gives nothing a
     good answer could rely on.
5. **Check the evidence.** It lists the paragraphs a good answer must rely on.
   - **Be specific.** Cite the paragraph that actually states the answer, not the chapter or section introduction.
   - **Alternatives (`|`).** Add one only where another paragraph states the same answer, for example an appendix
     that quotes the provision.
   - **Separate groups (`&`).** Use one only when the question has two limbs answered in different places, a good
     answer needs both, and **every** phrasing asks both.
   - **Don't pad.** A paragraph that only mentions the topic doesn't belong.
   - **Why it matters.** The system counts as "found" only when it retrieves at least one paragraph from every group.
     A missing alternative penalises a right answer, and a padded group lets a weak one through.
6. **Check the gaps** (partial only).
   - Search the Handbook to confirm the gap really isn't covered.
   - Check the keywords. The scorer looks for any one of them, as a whole word in any case, in the sentence where the
     answer says what the Handbook doesn't cover.
   - Good keywords are words such a sentence would naturally use: a few variants and synonyms (`qualify`,
     `qualifies`, `eligible`). Avoid very general words (`why`, `better`) and long exact phrases nobody would write
     word for word.
7. **Check ambiguous and register.**
   - **Ambiguous:** could two reasonable readers take the question differently and reach different answers? The flag
     doesn't change how the row is scored, so an ambiguous question is still marked against your one answer. Prefer
     to reword it (`C`), or reject it (`R`).
   - **Register:** fix it only if it is clearly wrong.
8. **Mark the family** (§6): `A`, `C` with JSON, `R`, or `E` with a doubt.
   - If a family is taking more than about 15 minutes, `E` it.
   - If it's broken beyond a quick fix, `R` it. Rejected families are replaced in round 2.

## 6. Your decision: exactly one `x` per line

| Mark | Use it when | Also fill |
|---|---|---|
| `A` approve | Everything on the line is right | — |
| `C` correct | Something is wrong and you know the fix | `corrections` (JSON, §7) |
| `R` reject | The family is unusable: unfixable, or a near-duplicate of another question | — |
| `E` escalate | You're unsure and want your colleague's independent view | `doubt`: one line on what you're unsure about. Never write your own answer there; the colleague sees the doubt |

- One `R` rejects the **whole family**, and any corrections in it are dropped. One `E` escalates the whole family. For
  `R` and `E`, mark every phrasing of the family the same way, for clarity.
- A line with no mark stays pending and comes back on the next sheet. A line with two marks makes the tool refuse the
  sheet.

## 7. The `corrections` cell (only with `C`)

Write a JSON object with only the keys you are changing. The allowed keys are `question`, `scope`, `evidence`, `gaps`,
`ambiguous` and `register`. Use straight double quotes.

| You want to | Type |
|---|---|
| Change the evidence to (4.5.1 or 4.5.2) and 5.1 | `{"evidence": [["4.5.1", "4.5.2"], ["5.1"]]}` |
| Make a family partial, with a gap | `{"scope": "partial", "gaps": [{"id": "g1", "keywords": ["stamp duty", "rate"]}]}` |
| Make a family refuse | `{"scope": "refuse", "evidence": [], "gaps": []}` |
| Change a partial to answer | `{"scope": "answer", "gaps": []}` |
| Flag it ambiguous | `{"ambiguous": true}` |
| Reword this phrasing | `{"question": "the new wording"}` |
| Fix the register | `{"register": "lay"}` |

- **Evidence format.** Each inner `[...]` is one needed group. The labels inside a group are the alternatives. Write
  labels as strings (`"4.5.1"`), and use only labels that exist in the Handbook, or the tool refuses the sheet.
- **Family-wide fields.** `scope`, `evidence` and `gaps` are shared by every phrasing of a family. Write the correction
  **once**, on any one phrasing, and the tool applies it to every phrasing of the family. That includes a phrasing the
  sheet doesn't list (on the development sheet, an original legacy wording). You may mark the other phrasings `A`. Each
  phrasing the correction changes comes back on your confirm sheet.
  - If you do write the correction on several phrasings, it must be identical on each, or the tool refuses the sheet.
  - A `question`, `register` or `ambiguous` correction applies to its own line only.
- **No need to touch `type`.** Rows also carry an older `type` field. When you change the scope, the tool updates it to
  match.
- **Confirm step.** A corrected row doesn't count straight away. It comes back on a short confirm sheet showing the new
  values, and you tick `OK` once it looks right.

## 8. What happens when you say a batch is done

1. Claude runs `sealed_tool apply --kind sealed --sheet <file>` and reports counts only: approved, changed, rejected,
   escalated and pending.
2. If the tool refuses the sheet, nothing is written. The reasons are:
   - `sheet_conflict lines=N`: N lines have two marks, or `C` with invalid JSON, or two phrasings of one family carry
     different scope, evidence or gaps corrections. Claude can't see which lines on a sealed sheet, so check your marks
     and quotes.
   - `schema_invalid`: a correction made a row invalid, for example an unknown paragraph label, a `refuse` that still
     has evidence, or phrasings of one family with different scopes. The tool names the field (such as `evidence`) but
     not the row.
   - `sheet_invalid`: the header changed, or the file isn't a CSV.
   - `sheet_stale`: a row changed after the sheet was made.
3. Claude then makes a **fresh sheet** with only the rows still open, in the same folder. **Continue on the fresh
   sheet.** The old one is stale once a correction or rejection is applied.
4. Claude makes a confirm sheet for your `C` rows, and an escalation form for your `E` rows (§9).

## 9. Escalation to your colleague

1. Claude runs `escalate`. It writes a form at `eval/private/sealed/work/escalations/form_<name>.txt`. Items are labelled
   `E01`, `E02`, … Each shows the phrasings and your doubt, never your answer, so the colleague answers independently.
2. You send the form to the colleague by email or on paper. Never send it through an AI tool.
3. Log each one in `eval/private/sealed/work/forwarding_log.csv`, with no names. Use the columns
   `date_sent,form,item_refs,date_returned`.
4. When the colleague replies, type their key into `colleague_key` on that family's line of the current sheet, for
   example `{"scope": "answer", "evidence": [["4.5.1"]], "gaps": []}`. Then make your final mark (A, C or R). Both
   keys are kept, and you decide.
5. **Deadline: 10 days.** If there is no reply by then, you decide alone, or a sealed family you still doubt is
   rejected.
6. Claude runs `disagree`, which counts disagreements per dimension.
7. **Calibration** (optional; your colleague said yes). After the freeze, the tool draws 15 approved families at random.
   Your colleague labels them from the questions alone, and the tool measures agreement.

## 10. The development sheet

File: `eval/private/work/sheets/sheet_development_all.csv`. It has 255 lines in 136 families: 61 new families, 26 T1/T7
families, 49 legacy paraphrases and 11 legacy refusal labels.

- The same marks and rules apply. Claude may read this sheet.
- Claude's pre-check, `eval/private/work/sheets/precheck_development.md`, lists the suspect families and suggests
  corrections. It also lists the clean families, which you can approve quickly.
- The plan also flags these items for you:
  - the 10 originally-partial T1/T7 families;
  - T01-Bb;
  - the realistic-v1 planning-extension refusal (D61);
  - the T07-Ab/T07-Ba secondary groups;
  - the pre-1975 date conflict;
  - the Family Home Protection Act near-miss pair `f3b70a075`/`fe5e51596`;
  - your three reworded rows from 20 Oct.

## 11. After your sheets

Claude runs the rest:

1. The SP recheck, then round 2 if needed.
2. The freeze and audits (b6).
3. Preflight, a dry draw, 5 recorded draws, the controls and the cost probe (b7).
4. Slot 0 (b8).
5. The docs and `v2.5.0` (b9).

You approve each € amount before its run.
