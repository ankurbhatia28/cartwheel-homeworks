"""Build the HW5 candidate list for one failure mode.

Enrichment search (the handout's `next_to_label`) plus a complementary
deterministic signal, over conversations not already reviewed in HW4. These are
retrieval signals, not labels: the reviewer labels every candidate.

    uv run python analysis/review_app/hw5_candidates.py --mode promises_unconfirmed_outcome --k 60
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DATA = Path(__file__).resolve().parent / "data" / "conversations.json"
OUT = REPO / "analysis" / "state" / "hw5_candidates.json"
MANIFEST = REPO / "analysis" / "state" / "sample_manifest.json"
TRACE_SOURCE = "traces/support_traces.json"

PROMISE = re.compile(
    r"\b(done|all set|in progress|will be (approved|refunded|processed)|should be approved"
    r"|no reason it|you'?ll (get|receive)|has been (submitted|processed)|is (being )?processed)\b",
    re.I,
)
PENDING_FLAGS = {"queued_for_approval", "escalated"}


def main() -> None:
    from analysis.helpers import next_to_label

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True)
    parser.add_argument("--k", type=int, default=60)
    args = parser.parse_args()

    conversations = json.loads(DATA.read_text())["conversations"]
    by_trace = {t: c for c in conversations for t in c["trace_ids"]}
    reviewed = {x["id"] for x in json.loads(MANIFEST.read_text())["conversations"]}

    candidates: list[dict] = []
    seen: set[str] = set()

    for pick in next_to_label(mode=args.mode, k=args.k, strategy="enrich", trace_source=TRACE_SOURCE):
        conv = by_trace.get(pick["trace_id"])
        if conv is None or conv["id"] in reviewed or conv["id"] in seen:
            continue
        seen.add(conv["id"])
        candidates.append({"conversation_id": conv["id"], "scenario_id": conv["scenario_id"],
                           "source": "enrich", "signal": pick["signal"]})

    for conv in conversations:
        if conv["id"] in reviewed or conv["id"] in seen:
            continue
        replies = " ".join(t["reply"] for t in conv["turns"])
        if set(conv["flags"]) & PENDING_FLAGS and PROMISE.search(replies):
            seen.add(conv["id"])
            candidates.append({"conversation_id": conv["id"], "scenario_id": conv["scenario_id"],
                               "source": "signal", "signal": "pending action (queued/escalated) + completion wording"})

    OUT.write_text(json.dumps({"mode": args.mode, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "trace_source": TRACE_SOURCE, "candidates": candidates}, indent=2) + "\n")
    by_source: dict[str, int] = {}
    for c in candidates:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    print(f"{len(candidates)} candidates -> {OUT.relative_to(REPO)}  {by_source}")


if __name__ == "__main__":
    main()
