"""Homework 9, Part E: compare the agent configurations by score and cost.

Reads the saved development results in optimize/results/ and makes no model
calls. A configuration is dominated when another one has an equal or higher
score and an equal or lower cost, and is better on at least one of the two
(the Homework 8 definition). Every other configuration is on the frontier.

Usage: uv run python profile/scripts/frontier.py

Run it by path: `profile` is also a standard-library module, so `-m` would
import that instead.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "optimize" / "results"
OUT = REPO / "profile" / "results" / "frontier.csv"

# The Homework 8 final run, then one row per run in this homework.
RUNS = {
    "final": "development-final-gpt-4o-mini-20261002-063152.json",
    "fewer-tokens": "development-fewer-tokens-gpt-4o-mini-20261006-102852.json",
    "cache-before": "development-cache-before-gpt-4o-mini-20261006-115256.json",
    "cache-after": "development-cache-after-gpt-4o-mini-20261006-115523.json",
}

COLUMNS = [
    "candidate",
    "model",
    "development_score",
    "write_pass_5",
    "cost_per_100_conversations_usd",
    "median_latency_seconds",
    "frontier_status",
]


def dominates(a: dict, b: dict) -> bool:
    score_a, cost_a = a["development_score"], a["cost_per_100_conversations_usd"]
    score_b, cost_b = b["development_score"], b["cost_per_100_conversations_usd"]
    return score_a >= score_b and cost_a <= cost_b and (score_a > score_b or cost_a < cost_b)


def main() -> None:
    rows = []
    for candidate, name in RUNS.items():
        result = json.loads((RESULTS / name).read_text())
        rows.append(
            {
                "candidate": candidate,
                "model": result["model"],
                "development_score": round(result["score"], 6),
                "write_pass_5": result["write_pass_5"],
                "cost_per_100_conversations_usd": result["cost_per_100_conversations_usd"],
                "median_latency_seconds": result["median_latency_seconds"],
            }
        )
    for row in rows:
        dominated = any(dominates(other, row) for other in rows if other is not row)
        row["frontier_status"] = "dominated" if dominated else "frontier"

    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
