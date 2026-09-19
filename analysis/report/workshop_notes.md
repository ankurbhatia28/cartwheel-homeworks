# Raindrop Workshop notes (HW4 Part C)

Written by the coding agent from Workshop's MCP tools (`query_traces`, `get_run_outline`),
cross-checked against Langfuse for tool results. Reviewer decisions were recorded on 2026-09-18 in the last column: 6 accepted, 1 revised, 0
rejected. The agent proposed; the reviewer decided. Everything below
is a hypothesis, not a label.

## Setup

- Workshop 0.1.21, installed locally with the official installer; `/instrument-agent` run in
  Claude Code. No Raindrop Cloud account (the handout: "a local Workshop installation is
  sufficient").
- Instrumentation: `raindrop-openai-agents` 0.0.11, added as a second Agents SDK trace processor
  next to OpenLLMetry's (`observability/instrument.py: setup_raindrop()`, called from the server
  at startup, opt-in via `RAINDROP_LOCAL_DEBUGGER`). `tracing_enabled=False`, so Raindrop does
  not start a second OpenTelemetry pipeline.
- Checked that the existing instrumentation is preserved: the global OTel provider is unchanged,
  Langfuse still receives the full trace (session span, agent, generations, tool spans with
  `cartwheel.*` attributes), and model, agent and workflow spans have the same attribute sets as
  the HW3 traces.
- **Limitation.** In local-only mode this Raindrop version records each run's input, final
  output, model and timing, but **not tool spans**: tool tracking goes through its cloud OTel
  export, which switches off without a write key (`tracing_enabled=True requires api_key for OTEL
  export`). Each Workshop run has the **same id as its Langfuse trace**, so tool calls and results
  were read from Langfuse for the same id. Also, every run shows user `cartwheel-dev`, because
  this version sets the user once per process.

## Inspected runs

Nine fresh conversations (10 runs; workshop-09 has two turns), run on `claude-opus-4-6` through
the server on 2026-09-18. Messages are reused from existing HW3 scenarios under new `workshop-`
ids, chosen to cover all three roles and nine tools without refunding or cancelling anything.
Scenario file: `scenarios/results/workshop_scenarios.jsonl` (not committed).

| Run | Workshop run / Langfuse trace id | Role | Source scenario | Tools (from Langfuse) |
|---|---|---|---|---|
| workshop-01 | `e09c13dba94277a09d059ca29b50811c` | shopper | support-0001 | find_order |
| workshop-02 | `b719925da3b4ec426f3583262d2ec017` | shopper | support-0220 | find_order, check_return_eligibility |
| workshop-03 | `d3f30de5aac5e7d4c19b2183410a59c3` | merchant | support-0133 | get_order |
| workshop-04 | `b2eab56c287f810daa72a9dcbd556db2` | merchant | support-0143 | list_my_orders |
| workshop-05 | `c2e6583ba50d03c0ca301ad258660832` | support | support-0175 | get_order, escalate_to_human |
| workshop-06 | `1ba201cfbc3e775d292b6f21b42338f5` | support | support-0168 | search_help_center, get_policy |
| workshop-07 | `4c92bfed8f928351fec1e22fa1d27aad` | shopper | support-0202 | search_products, get_product ×2 |
| workshop-08 | `836972da825183df52ecfc966d3b0e12` | merchant | support-0242 | get_order (permission_denied) |
| workshop-09 t1 | `49cd74d65a5df9001aecc6902c964dfd` | shopper | support-0249 | find_order |
| workshop-09 t2 | `458501b6c566f27e69101e5d8830e8fd` | shopper | support-0249 | find_order |

The error runs from before the API balance was topped up (for example `fcda2888…`, `ba592ed3…`)
and the setup probes (`probe-user-1`) are also in Workshop. They are not part of this inspection.

## Candidate failures and unusual behaviors

| # | Run | What the agent observed | Evidence | Closest mode | Reviewer decision |
|---|---|---|---|---|---|
| W1 | workshop-08 | **The denial reveals that another store's order exists.** The reply says order #6330 "doesn't belong to your store (Store 11)". The tool returned `permission_denied`; an order that didn't exist would return `not_found`, so the wording confirms #6330 exists in another store. The model's own thinking, visible in Workshop, reads "it likely belongs to a different store". The reply also shows the merchant's internal store id. | reply text; Langfuse `get_order` → `{"ok": false, "error": "permission_denied"}`; SPEC RESP-4 | **Not in the current taxonomy.** Candidate `reveals_inaccessible_record` (RESP-4). The store id also fits `exposes_internal_identifiers`. | **accepted** as new mode `reveals_inaccessible_record` |
| W2 | workshop-03 | **Offers a remedy no policy supports.** A merchant asks to cancel a shipped order. The agent correctly refuses, then offers to escalate so a human "may be able to help intercept the shipment", and says cancellations must happen "before an order leaves the warehouse". cw-cancellations says only: wait for delivery, then return. | reply text; Langfuse `get_order` → status `shipped`; cw-cancellations | Near `promises_unconfirmed_outcome` (implies an outcome no tool supports). Possibly its own candidate, `offers_unsupported_remedy`. | **revised**: folded into `promises_unconfirmed_outcome`; required behavior is to offer a human review without promising what it will do |
| W3 | workshop-01 | **No delivery estimate.** The order shipped June 30 and isn't delivered; the reply says "You should receive it soon" (cw-shipping allows up to 7 days, so by July 7). It also tells the user a tracking number was "usually sent via email", which no tool or policy states. | reply; Langfuse `find_order` (`shipped_at` 2026-06-30) | `incomplete_status_answer` (reinforces); the email claim is near `surfaces_irrelevant_detail` | **accepted** (reinforces `incomplete_status_answer`) |
| W4 | workshop-02 | **Raw policy id in the reply.** "(Northwind Books store policy, **store-northwind-books-policy**)". The deadline itself is correct (July 5, the 45-day store override). | reply; Langfuse `check_return_eligibility` (`window_source: store_override`) | `exposes_internal_identifiers` (reinforces). Close negative for `misses_store_specific_policy`. | **accepted** (reinforces `exposes_internal_identifiers`) |
| W5 | workshop-06 | **Raw policy id in the reply**: "(**cw-restocking-fees**)". Content is accurate. | reply | `exposes_internal_identifiers` (reinforces) | **accepted** (reinforces `exposes_internal_identifiers`) |
| W6 | workshop-05 | **Hints at an outcome.** It escalated promptly (ticket #170, 24-hour follow-up), which is correct, but adds that the resolution "may include a refund, since the order is still refund-eligible". | reply; Langfuse `escalate_to_human` → `ticket_id: 170` | `promises_unconfirmed_outcome` (borderline). Close negative for `missed_required_escalation`; also a close negative for the "Done" pattern, since here the ticket really was created. | **accepted** (borderline positive for `promises_unconfirmed_outcome`) |
| W7 | workshop-07 | **Negative-price product handled partly per RESP-3.** It doesn't show the −$5 value, calls it "a pricing error on our end", and says "I wouldn't recommend ordering that one until it's corrected". It doesn't say a person needs to review it. | reply; Langfuse `search_products` → Rustic Pitcher `price_usd: -5.0` | `surfaces_irrelevant_detail` / RESP-3 (as clarified in HW4). See the uncertainty case below. | **accepted** as a failure under revised RESP-3 (`surfaces_irrelevant_detail`): no human review offered |

### Behaviors that look correct (possible close negatives)

- **workshop-04**: "20 shown out of 488 total" for a merchant asking for *all* orders. This
  discloses the partial list accurately, so it's a close negative for `misstates_result_completeness`.
- **workshop-09, turn 1**: two orders really do share the title "Walnut Poetry Collection" (Paper
  Lantern Press #87, Northwind Books #9509). The agent listed both and asked which, instead of
  picking one. Turn 2's correction to the Compact Desk Lamp was handled correctly (#5571,
  delivered May 20).

## Case with uncertainty or an alternative explanation

**W7 (workshop-07, negative price).** I'm unsure whether this is a failure.

- *As a failure:* revised RESP-3 says to state that a visibly wrong record needs review by a person
  and to make no claims about the value or its cause. "A pricing error **on our end**" claims a
  cause, "I wouldn't recommend ordering" is advice, and it never mentions a human review.
- *Alternative explanation:* it met the original damaged-record expectation ("do not present the
  negative price as a valid offer"), didn't reveal the value, and steered the shopper to a valid
  product under budget. A reviewer could reasonably read "pricing error" as a neutral statement
  that the data is wrong, not a claim about its cause. Whether this passes depends on how strictly
  the new RESP-3 wording is applied.

A second, weaker uncertain case is **W2**. Suggesting escalation might be justified by ESC-4
("unsure whether policy allows an action"). What's less defensible is the specific "intercept the
shipment" suggestion.

## What Workshop added beyond open coding

- **W1 surfaced something the reviewed batches hadn't turned into a mode.** In the 60 reviewed
  conversations, the one shopper-facing denial (support-0244) was worded safely and marked "no
  failure", so revealing denials never appeared in the notes. The pilot had confirmed it
  (pilot-023), and the merchant wording here repeats it. Workshop's view of the model's thinking
  ("it likely belongs to a different store") shows where the leak starts.
- **W2 is a behavior not seen in the notes**: inventing a remedy outside policy.
- W3–W6 reinforce existing candidate modes, and workshop-04 and workshop-09 give close negatives.
