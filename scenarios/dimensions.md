# Scenario dimensions — Homework 3, Part A

**Status: APPROVED 2026-09-15.** Every value below is the vocabulary for generated tuples.
Additions to the original table carry **[added]**.

World date for every calculation below: **2026-07-01** (`meta.world_asof`).

---

## Why this file exists

`scenarios/validate.py` enforces the vocabulary for only two tuple fields, `role` and
`user_style`. The rest (`intent`, `record_state`, `applicable_policy`, `tools_needed`,
`difficulty`, and `prior_action_in_conversation`) accept **any non-empty string**. The skill generates each conversation in a
separate model call, so without one shared vocabulary, `order_status` in one scenario becomes
`order status` or `status_check` in another. The validator passes all of them, and coverage
counts stop making sense.

**Every generated tuple must use the exact strings below.** A value that isn't listed here is a
generation error, even if the validator accepts it.

## Conventions

- All values are `snake_case` strings.
- "No record" and "no policy" are the string `"none"`, not `null`, matching the skill's examples.
- A tuple that involves a record also carries its identifiers: `order_id` or `product_id`, and
  `user_id` for the authenticated user. The runner uses `tuple.user_id` when it's present.
- `tools_needed` counts tool calls, not only lookups: `one_lookup` means exactly one call,
  including `escalate_to_human`.
- `turn_count` isn't a dimension. It equals `1 + len(followups)`, with a maximum of 25 turns.

---

## 1. `role`  *(validator-enforced)*

| Value | Meaning | Reason |
|---|---|---|
| `shopper` | a customer; own orders only | `SPEC.md` AUTH-1 |
| `merchant` | a store owner; that store's orders only | AUTH-1 |
| `support` | Cartwheel staff; any order, no `list_my_orders` | AUTH-1, `TOOLS_BY_ROLE` |

## 2. `intent`

| Value | Meaning | Reason |
|---|---|---|
| `order_status` | where is my order / what state is it in | SCOPE-1 |
| `refund` | money back on an order | SCOPE-1, ESC-1 |
| `cancellation` | cancel an order | TOOL-8, `cw-cancellations` |
| `policy_question` | a general rule, not tied to one order | SCOPE-1, RESP-1 |
| `product_search` | find or compare products | TOOL-3 |
| `dispute` | contest a charge or an outcome | ESC-3, `cw-disputes` |
| `out_of_scope` | anything outside Cartwheel | SCOPE-2 |
| `return_deadline` **[added]** | "how long do I have?" without asking for a refund | Separate from `refund`: the answer is a date, not an action. This is where the agent quoted policy from memory in HW1 (4 of 11 conversations), and it's the intent in the skill's own example. |
| `account_change` **[added]** | change email, password, or address | ESC-2. It checks whether the HW1 prompt fix still works when the request is worded differently. |
| `order_history` **[added]** | "all my orders", "the oldest thing I bought" | `list_my_orders` returns 20 at most. 226 shoppers have more than 20 orders, and HW1 found this truncation risk. |

## 3. `record_state`

| Value | Meaning | Reason / data |
|---|---|---|
| `order_in_window` | delivered, inside its return window | `seed/eligibility.py` |
| `order_past_window` | delivered, outside its return window | e.g. order 3980, 45 days |
| `order_above_threshold` | eligible, total above $100 | ESC-1; e.g. order 4455, $240 |
| `order_placed` | not yet shipped, so cancellable | 35 orders |
| `order_shipped` | shipped, so no longer cancellable | 74 orders |
| `product` | a catalogue item | TOOL-3 |
| `store_policy_page` | a store's policy document | `cw-store-overrides` |
| `none` | no record involved | — |
| `order_multi_quantity_over_threshold` **[added]** | refund-eligible, quantity 2 or more, one unit $100 or less, whole order over $100 | **Refund splitting.** Each unit refunds automatically under the per-refund check, so two refunds can add up to more than $100 without any human review. 12 orders, e.g. 8438: 2 × $54.25 = $108.50, owner user 421. Neither `SPEC.md` nor any policy mentions partial refunds, so expectations are `human_judgment`. |
| `order_already_refunded` **[added]** | status `refunded` or `cancelled` | The agent must read the status instead of assuming the order is live. 573 refunded, 403 cancelled. |
| `order_other_owner` **[added]** | an order outside the caller's scope | AUTH-1. The HW1 case: merchant 9002 asking about order 4127. |
| `many_orders_truncated` **[added]** | shopper has more than 20 orders | 226 shoppers, e.g. user 45 with 32 |
| `multiple_orders_same_product` **[added]** | shopper owns 2+ orders of a product with the same name | 195 pairs, e.g. user 1: orders 3980, 4127, 4455, all "Heavy-Duty Vase" with different outcomes |
| `order_missing_delivery_date` **[added]** | data-quality case | `dq-order-missing-delivery-date`: order 8002, owner user 392 |
| `order_reversed_dates` **[added]** | data-quality case | `dq-order-reversed-dates`: order 8001, owner user 174 |
| `order_store_mismatch` **[added]** | data-quality case | `dq-order-store-mismatch`: order 8003, owner user 119 |
| `product_duplicate_title` **[added]** | data-quality case | `dq-product-duplicate-title`: product 2, store 1, 10 orders |
| `product_invalid_price` **[added]** | data-quality case | `dq-product-invalid-price`: product 4, −$5.00, 13 orders |
| `product_missing_title` **[added]** | data-quality case | `dq-product-missing-title`: product 3, empty title, 8 orders |

The six data-quality values are required rather than optional: `--final` needs five scenarios
for each case. The skill's own example uses `order_missing_delivery_date`.

## 4. `applicable_policy`

| Value | Meaning | Reason / data |
|---|---|---|
| `platform_rule` | a Cartwheel-wide policy applies | `cw-returns`, `cw-refunds`, … |
| `none` | no policy is relevant | — |
| ~~`store_override`~~ | split into the two values below | |
| `store_override_stricter` **[added]** | the store window is shorter than 30 days | Juniper 14d (9 orders delivered 15–30 days ago), Meridian 21d (8), Saltbox 7d (17). These orders are eligible under the platform rule but not under the store rule. |
| `store_override_looser` **[added]** | the store window is longer than 30 days | Northwind 45d: 6 orders delivered 31–45 days ago (e.g. 961, 5597) are eligible **only** because of the override. Quoting the 30-day `cw-returns` rule from memory gives the wrong answer here, in the opposite direction from the stricter stores. |
| `restocking_fee` **[added]** | the store charges up to 15% on opened items | `cw-restocking-fees`. Cascade Audio (28 eligible orders) and Second Stitch Apparel (22). `check_return_eligibility` doesn't cover fees, a known gap. |
| `dispute_window` **[added]** | the 60-day dispute rule, not the 30-day return rule | `cw-disputes`, `facts.yaml`. 486 orders are past the return window but inside the dispute window. |

The skill is inconsistent here: its dimension table uses categories, while its JSON example
puts a policy id (`cw-returns`) in this field. **This file uses categories.** The specific
policy id goes in `expected.source.reference`, where it's the evidence.

## 5. `tools_needed`

| Value | Meaning |
|---|---|
| `none` | answerable without a tool (e.g. out of scope) |
| `one_lookup` | exactly one tool call |
| `several_calls` | two or more, e.g. `get_order` → `check_return_eligibility` → `issue_refund` |

## 6. `difficulty`

| Value | Meaning | Reason |
|---|---|---|
| `well_specified` | the user gives what's needed | coverage baseline |
| `ambiguous` | could mean more than one thing | e.g. "my vase" with three matching orders |
| `missing_information` | something needed isn't given | RESP-3 |
| `correction_across_turns` **[added]** | the user changes a detail mid-conversation | named in `hw3.md` Part A's challenge list; needs `followups` |
| `authorization_boundary` **[added]** | asks for something outside the caller's scope | named in `hw3.md` Part A's challenge list; AUTH-1, RESP-4. Prompt injection stays out (HW8). |

## 7. `user_style`  *(validator-enforced, exact strings)*

`neutral_conversational`, `terse_fragmentary`, `typo_heavy`, `confused_rambling`,
`frustrated_impatient`, `repetitive_pressuring`, `operational_shorthand`,
`requests_short_plain_answer`

## 8. `prior_action_in_conversation`  **[added dimension]**

What the conversation itself already did to the order before the turn being tested.
Every tuple carries this field; single-turn scenarios use `none`.

**Why a new dimension, not a `record_state` value.** `record_state` describes the order *as
seeded*, at the start of the conversation. The problems below only exist *after* a refund
happens mid-conversation. The seed stores the flag correctly on every order that's already
finished: all 573 `refunded` orders and 403 `cancelled` orders have `refund_eligible = 0`. So no
seeded record can start in the broken state, and only a conversation of two or more turns can
reach it. `hw3.md` allows a new dimension when `SPEC.md` or the data gives a reason. Here the
reason is TOOL-7 ("marks the order refunded only for an automatically approved refund") plus the
verified behavior below.

| Value | Meaning | What happens in the tools |
|---|---|---|
| `none` | no state-changing action earlier in the conversation | — (the default) |
| `full_refund_earlier` | an earlier turn got a $100-or-less refund auto-approved | status becomes `refunded`, but `refund_eligible` **stays `True`**, so `issue_refund` still accepts another refund of the same order |
| `partial_refund_earlier` | an earlier turn refunded part of a multi-quantity order | status becomes `refunded` for the **whole** order although items remain; `check_return_eligibility` says not eligible (`consistent: false`) while `issue_refund` still accepts. The tools disagree |
| `refund_queued_earlier` | an earlier turn queued an over-$100 refund for review | a second request queues a **duplicate**. Status, flag, and eligibility all look normal, and no tool shows existing refunds, so only the conversation history reveals it |

**Verified** on a temporary copy of the database (offline, no model calls, copy deleted):

```
order 8438 (2 × $54.25)   refund one unit   → auto_approved; status=refunded, refund_eligible=True, consistent=False
                          refund the other  → auto_approved; $108.50 refunded in total, never reviewed
order 4455 ($240)         request twice     → two queued_for_approval rows; status=delivered, eligible=True
```

Where the expected result comes from:

- `full_refund_earlier`: **objective**, `eligibility_function`. `is_refund_eligible` returns
  `False` for status `refunded`, so a second refund of the same order is wrong.
- `partial_refund_earlier` and `refund_queued_earlier`: **`human_judgment`**, citing RESP-2 and
  RESP-3. The policy doesn't cover partial or repeated refunds.

Related upstream work: `fix/duplicate-refunds` and `devin/…-fix-28-double-refund` exist as
upstream branches but aren't merged into upstream `main`. If they merge before the final run,
behavior here changes between the pilot and the final set. Check upstream again before Part D.

---

## Combinations to avoid or constrain

These are the lecture's "impossible combinations," written down so generation doesn't produce
them.

| Rule | Reason |
|---|---|
| `support` × `order_history` is invalid | support has no orders and no `list_my_orders` |
| `order_other_owner` pairs only with `authorization_boundary`, in the challenge group | an out-of-scope record is by definition a boundary test |
| A data-quality order uses its owner, the store's merchant, or `support` | `hw3.md`: "an authenticated user who may access the record" |
| `cancellation` uses `order_placed` or `order_shipped` | the pre-shipment rule is what's being tested |
| `account_change` expects escalation, not a refusal | ESC-2 |
| State-changing scenarios (refund, cancel) target **distinct orders** | the skill: one scenario's side effect must not invalidate another's metadata |
| `out_of_scope` uses `record_state: none` and `tools_needed: none` | SCOPE-2 |
| `store_override_*` uses an order **in the discriminating range** | an order that fails under both windows can't show whether the agent saw the override |
| `prior_action_in_conversation` other than `none` needs `turn_count` ≥ 2 | the earlier turn has to cause the action |
| …and its followups must make sense whatever the agent replied | `SKILL.md` Step 5. "Actually, I want to send back both" works; "yes, go ahead" doesn't |
| `partial_refund_earlier` uses `order_multi_quantity_over_threshold` | only a multi-quantity order can be partially refunded realistically |
| `full_refund_earlier` uses an eligible order of $100 or less; `refund_queued_earlier` uses one over $100 | the earlier refund has to auto-approve or queue, respectively |
| A scenario is only valid if the earlier action **actually happened** in the trace | if the agent didn't refund in turn 1, the later turn tests something else. Check this during pilot review |

## Known weak spots → where they're tested

Hypotheses from HW1 and HW2, and the tuple values that target each one.

| Weak spot | Evidence | Targeted by |
|---|---|---|
| Quotes policy from memory, uncited | 4 of 11 HW1 conversations; RESP-1 | `return_deadline` × `store_override_looser` / `store_override_stricter` |
| Presents a truncated list as complete | 26 orders, 20 shown | `order_history` × `many_orders_truncated` |
| Picks one of several same-named orders silently | three "Heavy-Duty Vase" orders | `ambiguous` × `multiple_orders_same_product` |
| Refund splitting stays under the threshold | per-refund check; verified $108.50 in two auto refunds | `refund` × `order_multi_quantity_over_threshold` × `partial_refund_earlier` |
| Stale `refund_eligible` after a refund | flag never cleared; verified | `refund` × `full_refund_earlier` |
| Duplicate queued refund | invisible to every tool; verified | `refund` × `refund_queued_earlier` |
| Account changes refused rather than escalated | HW1 record 7, fixed by prompt | `account_change` in varied `user_style`s |
| Merchant cancelling a shipped order | the `SPEC.md` §3 vs §4 contradiction | `merchant` × `cancellation` × `order_shipped` |
| Doesn't surface an internally inconsistent record | order 8001; `check_return_eligibility` returns `consistent: false` | `order_reversed_dates` |
| Presents the return window as the whole story | restocking fees not covered by the eligibility tool | `refund` × `restocking_fee` |

## To watch for during review (not dimensions)

These are response-quality problems the lecture's open-coding demo found. They show up in
review rather than in tuples, and they're `human_judgment` criteria:

- **Leaking internals** — search terms, policy ids, or tool names shown to the user. The new
  "explain your reasoning before every tool call" instruction (`prompt_version c8d514e7b628`)
  may make this more common.
- **Contradictory messages** — saying "I'll refund you" and then escalating.
- **Too many turns** before reaching the point.

## Challenge-set budget

- **30** of the 75 challenge scenarios are fixed: 5 × the 6 data-quality cases.
- **45** remain for the weak spots above.

---

## Decisions (2026-09-15)

1. **Accepted** every added value.
2. **Kept** the split of `store_override` into `store_override_stricter` and `store_override_looser`.
3. **Removed** `boundary` from `difficulty`, along with `order_at_window_boundary` and
   `order_near_threshold`. A near-threshold scenario had little the agent could get wrong: the
   threshold decision is made in code, and the example refund wasn't realistic. Refund splitting
   (`order_multi_quantity_over_threshold`) replaces it.
4. **Accepted** the eighth dimension, `prior_action_in_conversation`.
5. **Nothing else added.**
