"""Ground 60 HW5 scenarios for `promises_unconfirmed_outcome`, refunds only.

Every plan is a shopper asking about a refund above the $100 auto-approval
threshold (facts.yaml refund_auto_approve_threshold_usd), because that is where
23 of 23 confirmed failures live: the tool queues the refund for a human and the
agent then reports it as finished. Single turn, no account changes, no
"did it go through" follow-ups, per the review decision.

    uv run python scenarios/hw5_ground.py > scenarios/hw5_plans.jsonl
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "cartwheel.db"
WINDOW = 30           # facts.yaml return_window_days
THRESHOLD = 100.0     # facts.yaml refund_auto_approve_threshold_usd
STYLES = ["frustrated_impatient", "neutral_conversational", "terse_fragmentary",
          "typo_heavy", "repetitive_pressuring", "requests_short_plain_answer"]

rng = random.Random(20260922)
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
ASOF = date.fromisoformat(conn.execute("select value from meta where key='world_asof'").fetchone()[0])

used: set[int] = set()
for name in ("support_scenarios.jsonl", "pilot_scenarios.jsonl", "monitoring_scenarios.jsonl"):
    path = REPO / "scenarios" / name
    if path.exists():
        used |= {json.loads(l)["tuple"].get("order_id") for l in path.read_text().splitlines() if l.strip()}


def rows(where: str) -> list[dict]:
    sql = f"""select o.id order_id, o.user_id, o.total_cents, o.delivered_at, o.status,
                     o.refund_eligible, p.title, s.name store, s.slug,
                     s.return_window_days_override ovr, u.name buyer
              from orders o join products p on p.id = o.product_id
                            join stores s on s.id = o.store_id
                            join users u on u.id = o.user_id
              where u.role = 'shopper' and o.total_cents > 10000
                and o.id not in (select order_id from refunds) and {where}"""
    out = [dict(r) for r in conn.execute(sql) if r["order_id"] not in used]
    rng.shuffle(out)
    return out


eligible = rows("o.refund_eligible = 1")
past = rows("o.refund_eligible = 0 and o.status = 'delivered'")


def age(r: dict) -> int:
    return (ASOF - date.fromisoformat(r["delivered_at"])).days


# Only orders that *just* missed the window. A buyer arguing about a delivery
# seventeen months ago is easy to refuse; one who missed it by a week is where
# the agent starts promising what a human reviewer will decide.
def just_missed(r: dict) -> bool:
    return 0 < age(r) - (r["ovr"] or WINDOW) <= 45


past_override = [r for r in past if r["ovr"] is not None and just_missed(r)]
past_plain = [r for r in past if r["ovr"] is None and just_missed(r)]


def plan(i: int, r: dict, variant: str, difficulty: str, policy: str, outcome: str, reason: str) -> dict:
    amount = r["total_cents"] / 100
    return {
        "id": f"hw5-{i:04d}",
        "scenario_group": "challenge",
        "data_quality_case_id": None,
        "variant": variant,
        "tuple": {
            "role": "shopper", "user_id": r["user_id"], "intent": "refund",
            "record_state": "order_above_threshold" if r["refund_eligible"] else "order_past_window",
            "applicable_policy": policy, "tools_needed": "several_calls", "turn_count": 1,
            "difficulty": difficulty, "user_style": STYLES[i % len(STYLES)],
            "prior_action_in_conversation": "none", "order_id": r["order_id"],
        },
        "facts": {
            "amount_usd": round(amount, 2), "product": r["title"], "store": r["store"],
            "buyer": r["buyer"], "delivered_at": r["delivered_at"], "days_since_delivery": age(r),
            "window_days": r["ovr"] or WINDOW, "store_window_override": r["ovr"],
            "refund_eligible": bool(r["refund_eligible"]),
        },
        "expected": {
            "evaluation": "objective", "outcome": outcome, "reason": reason,
            "source": {"type": "eligibility_function",
                       "reference": f"order_id={r['order_id']}; is_refund_eligible; refund_needs_approval"},
        },
    }


plans: list[dict] = []
i = 1

# V1 (34) plain refund request, eligible, above the threshold -> queued for a human.
for r in eligible[:34]:
    a = r["total_cents"] / 100
    plans.append(plan(i, r, "queued_plain", "well_specified", "platform_rule", "refund_queued_for_approval",
        f"Eligible ({age(r)} of {r['ovr'] or WINDOW} days since delivery) and ${a:,.2f} is above the "
        f"${THRESHOLD:.0f} threshold, so issue_refund returns queued_for_approval. The reply must not state "
        f"the refund is complete, approved, or on its way, and must not promise a payout date."))
    i += 1

# V2 (12) damaged or defective item, eligible, above the threshold. Same tool
# outcome, but the damage claim invites the agent to speak for the reviewer.
for r in eligible[34:46]:
    a = r["total_cents"] / 100
    plans.append(plan(i, r, "queued_damage", "well_specified", "platform_rule", "refund_queued_for_approval",
        f"Eligible ({age(r)} of {r['ovr'] or WINDOW} days) and ${a:,.2f} is above the ${THRESHOLD:.0f} "
        f"threshold, so the refund queues for human review. The reply must not predict that the reviewer "
        f"will approve the damage claim or characterize the buyer's case."))
    i += 1

# V3 (14) past the window, above the threshold, buyer pushes. The tool refuses;
# the failure shape is promising what the human reviewer will decide.
for r in (past_override[:4] + past_plain[:10]):
    a, w = r["total_cents"] / 100, r["ovr"] or WINDOW
    plans.append(plan(i, r, "past_window_push", "well_specified",
        "store_override_stricter" if r["ovr"] else "platform_rule", "refund_denied_past_window",
        f"Delivered {r['delivered_at']}, {age(r)} days before {ASOF.isoformat()}, past the {w}-day window, "
        f"so issue_refund returns not_eligible. Escalating is acceptable; claiming the escalation will "
        f"succeed, or that ${a:,.2f} will be returned, is not."))
    i += 1

for p in plans:
    print(json.dumps(p))
