# HW4 review summary

Reviewer: Ankur. Traces: the HW3 final run (Langfuse, 2026-09-16 00:40–01:45 UTC; 250 conversations,
276 traces), model `claude-opus-4-6`, prompt version `c8d514e7b628`. Review app:
`analysis/review_app/`. State: `analysis/state/`.

## 1. Reviewed sample

**Unit of review.** One conversation, meaning one `cartwheel.session_id`. Cartwheel writes one trace
per user turn, so the review app groups each conversation's traces in order, and a conversation's
labels are written to every one of its traces. **100 conversations = 105 traces** (5 multi-turn).

| Batch | Size | How it was chosen | Seed | Roles (shopper / merchant / support) | Coverage / challenge |
|---|---|---|---|---|---|
| 1 | 30 | 15 uniform random + 15 cluster representatives (KMeans k=8 on role, intent, tools, flags, turns, latency; the ones closest to each centroid, every cluster represented) | 20260918 | 25 / 4 / 1 | 20 / 10 |
| 2 | 30 | Dimension chosen before looking at outcomes: **intent**, 3 per value across all 10 intents, uniform within each value | 20260919 | 23 / 4 / 3 | 25 / 5 |
| 3 | 25 | Depth searches for candidate modes, **likely positives and close negatives**, retrieved with signals on reply text, tool results and scenario fields; every one reviewed | — | 19 / 5 / 1 | 13 / 12 |
| 4 | 15 | Uniform random from conversations not yet sampled, drawn after the taxonomy was drafted | 20260921 | 8 / 5 / 2 | 9 / 6 |
| **Total** | **100** | No conversation is in two batches | | **75 / 18 / 7** | **67 / 33** |

Intents covered: refund 21, order status 19, product search 12, cancellation 9, return deadline 8,
policy question 7, order history 7, dispute 7, out of scope 6, account change 4.

Raindrop Workshop added 9 fresh runs (10 traces, `workshop-01`…`09`) as a source of hypotheses. They
are outside the 100 and are not labeled (see `workshop_notes.md`).

Three annotations were made on conversations outside the sample (support-0002, 0004, 0023) while
browsing; they are not counted.

## 2. Open coding

- **80 notes** on the 100 conversations: **67** have a noted failure and **33** were marked "no
  failure observed". Each note records the first failure, per the stopping rule.
- Notes were written in the reviewer's words. Twelve batch 3 notes started as accepted agent
  suggestions and were rewritten by the reviewer in his own words, with a first failure marked.
- Original wording is kept where notes were revised (for example support-0197, "made up order" became
  "made up title").

## 3. Final taxonomy (8 modes)

Counts are **sample fractions**, not prevalence estimates: batch 2 was stratified and batch 3 was
deliberately targeted, so the sample over-represents some cases. Homework 5 estimates prevalence on the
full trace store.

| Mode | Fail / 100 conversations | Trace-level Fail / Pass (of 105) | Evaluator | Requirement |
|---|---|---|---|---|
| `exposes_internal_identifiers` | 32 (32%) | 34 / 71 | code check | RESP-1 (revised) |
| `promises_unconfirmed_outcome` | 11 (11%) | 14 / 91 | LLM judge | RESP-2 |
| `surfaces_irrelevant_detail` | 10 (10%) | 10 / 95 | LLM judge | RESP-5, SCOPE-2 |
| `missed_required_escalation` | 8 (8%) | 9 / 96 | LLM judge | ESC-2, ESC-3, ESC-4 |
| `misstates_result_completeness` | 8 (8%) | 8 / 97 | code check | RESP-7 (added) |
| `incomplete_status_answer` | 6 (6%) | 6 / 99 | LLM judge | RESP-6 (added) |
| `mishandles_bad_record` | 5 (5%) | 5 / 100 | LLM judge | RESP-3 (clarified) |
| `reveals_inaccessible_record` | 3 (3%) | 3 / 102 | LLM judge | RESP-4, AUTH-1 |

66 of 100 conversations fail at least one mode, and 15 fail two or more. Each mode's binary
definition, supporting annotations, positive conversations, close negatives, boundary against its
nearest neighbor, and change history are in `analysis/state/patterns.json`. Every mode has at least
3 confirmed positives. Close negatives: 3–4 per mode, except `reveals_inaccessible_record` (0244,
0245) and `incomplete_status_answer` (0117, 0122), where the reviewed data has no further near-misses.

## 4. Stability: new modes in the final 15

**0 new consequential modes** appeared in batch 4. Four of its 15 conversations had failures, and all
fit existing modes: support-0222 and 0187 (`surfaces_irrelevant_detail`), and 0187, 0234 and 0178
(`exposes_internal_identifiers`). The other 11 had no failure. Batch 4 sharpened one boundary
(support-0178, below) without adding a mode, so no further batch was needed.

## 5. Taxonomy revisions

**Merged: `gives_off_platform_advice` into `surfaces_irrelevant_detail`.** Batch 2 produced the
off-platform candidate from support-0149 (declined a marketing question, then recommended outside
resources). The batch 3 depth search confirmed it (0103, 0104) and found clean close negatives (0101,
0105, 0106 declined without outside suggestions). With 10 candidates against a limit of 8, the reviewer
merged it into `surfaces_irrelevant_detail`, because one product change fixes both: answer what was
asked and stop. The mode's definition now names outside recommendations explicitly.

Other revisions (each recorded in the mode's history in `patterns.json`):

- `presents_missing_data_as_fact` merged with the bad-record cases of `surfaces_irrelevant_detail` (the
  negative-price explanations, 0201 and 0203) into **`mishandles_bad_record`**. One fix covers both:
  say the record is wrong and send it to a person.
- `undisclosed_result_limit` widened to **`misstates_result_completeness`** after support-0145 ("…and
  10 more (498 total)").
- `dead_end_without_escalation` widened to **`missed_required_escalation`** after support-0236 (a
  dispute handled by the agent, with escalation offered only at the end).
- Workshop run W2 (a human "may be able to intercept the shipment") folded into
  `promises_unconfirmed_outcome`. The required behavior is to offer human review without predicting
  its result.
- Boundary sharpened by support-0178: describing "the system's records" fails
  `exposes_internal_identifiers`, while stating in user terms that data is missing is what RESP-3
  requires.

**Rejected groups** (kept as observations, not modes):

- `misuses_tool_arguments` (support-0147, 0148: merchant product searches passed the store id where
  `search_products` expects the store name). Rejected by the reviewer.
- `misses_store_specific_policy` (0233, 0232 originally): demoted for insufficient support. It had 2
  positives against 3 confirmed close negatives (0223 Northwind 45-day, 0225 Juniper 14-day, 0231
  Cascade restocking fee), so the agent usually applies store rules. 0232's note was later reassigned
  to `promises_unconfirmed_outcome`.

**Failures observed but not covered by a final mode:** support-0147 and 0148 (store id passed as a
name; tool-argument group rejected), support-0233 (restocking fee ignored; store-policy group demoted),
and support-0140 (answered from `search_help_center` rather than `get_policy`; kept as an open code).
These are real failures that an evaluator built from this taxonomy would not measure.

## 6. SPEC.md revisions and their motivating annotations

| Rule | Change | Motivating annotations |
|---|---|---|
| RESP-1 | **Revised.** Cite policies in plain language; keep raw ids like `cw-returns` out of replies (in tool calls and escalation context instead). The original rule required the id itself. | support-0077, 0024, 0067, 0216, 0219, 0198, 0034, 0098 |
| RESP-3 | **Clarified.** For a visibly wrong record, say it needs review by a person; no claims about the value or its cause. | support-0201, 0203 |
| RESP-6 | **Added.** Order-status replies give the next step and the expected date when policy defines it (ship within 3 days; deliver within 7 days of shipment). | support-0005, 0010, 0003, 0006, 0001, 0120 |
| RESP-7 | **Added.** Replies built on a capped list or search say the results are partial, with counts that agree with what is shown and what exists. | support-0171, 0084, 0145, 0083, 0191 |

These revisions change the requirements, not the running agent. Its prompt still asks for policy ids,
so `exposes_internal_identifiers` measures the gap between current behavior and the revised spec.

## 7. Search for more instances and suggestions

- **Depth search (batch 3):** 14 agent suggestions posted on likely positives; **13 accepted, 1
  rejected**. The rejection is support-0174 (`promises_unconfirmed_outcome`): "This is following
  directions. It is fine. Doesn't make any promises on outcome." The escalation happened and nothing
  was promised, so it became a close negative.
- **Re-check for criteria drift:** after `presents_missing_data_as_fact` emerged in batch 2,
  support-0198 (batch 1) and 0200 (batch 2, first marked "no failure") were re-checked and accepted.
  Both presented "Portable Tray", taken from the product description, as the missing title.
- **Raindrop Workshop:** 7 suggestions, 6 accepted and 1 revised (see `workshop_notes.md`). Workshop
  surfaced `reveals_inaccessible_record`, which open coding had missed: the only shopper-facing denial
  in batches 1–2 (0244) was worded safely.
- All decisions are in `analysis/state/suggestions.json`.

## 8. Labeling method and a limitation

Every one of the 800 conversation × mode cells has an explicit human Pass/Fail, written to
`analysis/state/labels/<mode>.jsonl` (840 trace-level records) and to Langfuse as a score with one
stable id per trace and mode.

To make 800 judgments tractable, the Labels view showed a **suggested** value for each cell
(`analysis/review_app/draft_labels.py`, saved in `label_drafts.json`), with its basis:

- the reviewer's own note assigned to that mode
- a code check (raw policy id or tool name in the reply; a capped result without disclosure), which
  caught second failures the stopping rule had skipped
- "not applicable" (for example, no permission denial in the conversation)
- "no note, check for a second failure"

The reviewer confirmed each row. **The final labels agree with the suggestions in 798 of 800 cells**;
the two exceptions are 0232 and 0219, reassigned after the reviewer's decisions. That agreement is
expected where the suggestion came from the reviewer's own note. But for the "no note" cells it may
also reflect **anchoring on the drafts**. A second pass over those cells without suggestions shown, or
a second reviewer, would test that. Each saved label records the suggestion it started from, so the
comparison stays inspectable.

## 9. Readiness for Homework 5

HW5 needs at least 30 Pass and 30 Fail labels per mode to split and validate an LLM judge.

| Mode | Fail traces now | Plan |
|---|---|---|
| `exposes_internal_identifiers` | 34 | Enough Fails, but it's a code check, so no judge is needed |
| `promises_unconfirmed_outcome` | 14 | Needs about 16 more Fails: generate scenarios targeting queued refunds, escalations and account changes |
| `surfaces_irrelevant_detail` | 10 | About 20 more |
| `missed_required_escalation` | 9 | About 21 more (disputes, account changes) |
| `misstates_result_completeness` | 8 | Code check; no judge needed |
| `incomplete_status_answer` | 6 | About 24 more (shipped and placed order-status scenarios) |
| `mishandles_bad_record` | 5 | About 25 more (only 6 damaged records exist, so scenarios need variety) |
| `reveals_inaccessible_record` | 3 | About 27 more (authorization-boundary scenarios) |

Every judge mode has enough Pass labels. None except the code-check `exposes_internal_identifiers`
has 30 Fails, so HW5 starts by generating targeted scenarios with the HW3 pipeline, as the updated
handout suggests.
