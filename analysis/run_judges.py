"""HW5: build, run, and evaluate an LLM judge for one failure mode.

Steps, in the handout's order (`homework/module-2/hw5.md`):

    uv run python analysis/run_judges.py seed        # HW4 labels -> HW5 labels (1 = Pass)
    uv run python analysis/run_judges.py inputs      # analysis/state/hw5_trace_inputs.json
    uv run python analysis/run_judges.py split       # 20/40/40 train/dev/test
    uv run python analysis/run_judges.py dev  --prompt analysis/prompts/<mode>-v0.txt
    uv run python analysis/run_judges.py test --judge <judge_id>

**Label conventions differ between homeworks.** HW4 labels (analysis/state/labels/)
use 1 = failure present. HW5 labels (analysis/state/hw5_labels/) use **1 = Pass
(failure absent), 0 = Fail**, which is what the helpers expect here. `seed`
converts; the review app writes HW5 labels directly in HW5 form.

The judge never sees human labels, review notes, or scenario metadata: only the
conversation and its tool activity (see `prepare_inputs`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

STATE = REPO / "analysis" / "state"
HW4_LABELS = STATE / "labels"
HW5_LABELS = STATE / "hw5_labels"
INPUTS = STATE / "hw5_trace_inputs.json"
CONVERSATIONS = REPO / "analysis" / "review_app" / "data" / "conversations.json"
MODE = "promises_unconfirmed_outcome"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conversations() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(CONVERSATIONS.read_text())["conversations"]}


def _live(path: Path) -> dict[str, dict[str, Any]]:
    """Current record per trace from an append-only label file."""
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {r["trace_id"]: r for r in rows if not r.get("superseded_by")}


# ---------------------------------------------------------------------------
# seed: HW4 labels (1 = failure) -> HW5 labels (1 = Pass)
# ---------------------------------------------------------------------------


def seed_hw5_labels(mode: str = MODE) -> dict[str, int]:
    hw4 = _live(HW4_LABELS / f"{mode}.jsonl")
    existing = _live(HW5_LABELS / f"{mode}.jsonl")
    HW5_LABELS.mkdir(parents=True, exist_ok=True)
    path = HW5_LABELS / f"{mode}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    added = 0
    for trace_id, r in hw4.items():
        if trace_id in existing:
            continue
        rows.append({"trace_id": trace_id, "label": 1 - int(r["label"]), "source": "human",
                     "ts": _now(), "label_id": f"{trace_id}#hw5seed", "conversation_id": r.get("conversation_id"),
                     "scenario_id": r.get("scenario_id"), "evidence": r.get("evidence", ""),
                     "origin": "hw4_label_converted"})
        added += 1
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    live = _live(path)
    return {"added": added, "pass": sum(r["label"] == 1 for r in live.values()),
            "fail": sum(r["label"] == 0 for r in live.values()), "traces": len(live)}


# ---------------------------------------------------------------------------
# inputs: what the judge reads
# ---------------------------------------------------------------------------


def prepare_inputs(mode: str = MODE) -> dict[str, int]:
    """One record per labeled conversation: the messages, tool calls and results.

    No labels, notes, expected outcomes, or scenario metadata: those would leak
    the answer. The scenario id is kept only as `trace_id` bookkeeping via the
    conversation's first trace id, which is what the label files key on.
    """
    conversations = _conversations()
    labels = _live(HW5_LABELS / f"{mode}.jsonl")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace_id, row in labels.items():
        conv = conversations.get(row.get("conversation_id") or "")
        if conv is None or conv["id"] in seen:
            continue
        seen.add(conv["id"])
        messages: list[dict[str, Any]] = []
        for turn in conv["turns"]:
            messages.append({"role": "user", "content": turn["user"]})
            for step in turn["steps"]:
                for call in step["calls"]:
                    # The normalizer flattens a tool_call to its arguments and a
                    # tool_result to its content, and drops "name", so the tool
                    # name has to travel inside the payload or the judge cannot
                    # tell issue_refund from check_return_eligibility.
                    messages.append({"role": "tool_call", "name": call["name"],
                                     "arguments": {"tool": call["name"], **call["arguments"]}})
                    messages.append({"role": "tool_result", "name": call["name"],
                                     "content": {"tool": call["name"], "result": call["result"]}})
            messages.append({"role": "assistant", "content": turn["reply"]})
        records.append({"trace_id": conv["trace_ids"][0], "trace": messages})
    INPUTS.write_text(json.dumps(records, indent=1) + "\n")
    return {"records": len(records), "labeled_conversations": len({r.get("conversation_id") for r in labels.values()})}


# ---------------------------------------------------------------------------
# split / dev / test (validate-evaluator)
# ---------------------------------------------------------------------------


def split_data(mode: str = MODE) -> dict[str, Any]:
    from analysis.helpers import split_labels

    records = json.loads(INPUTS.read_text())
    return split_labels(mode, fractions=(0.20, 0.40, 0.40), seed=7, min_per_class=10,
                        eligible_trace_ids=[r["trace_id"] for r in records])


def _judge_env() -> None:
    """Point the judge at our inputs, and load the API key from .env.

    ``load_store_traces`` reads CARTWHEEL_JUDGE_TRACE_SOURCE; without it the
    helper would fall back to the Langfuse slice or the committed demo export,
    and the judge would score traces that are not the ones we labeled.
    """
    from observability.instrument import load_env

    load_env()
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(INPUTS)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; add it to .env (never pass it on the command line)")


def run_development(mode: str, prompt_path: Path, judge_model: str = "gpt-4o-mini") -> dict[str, Any]:
    from analysis.helpers import judge_alignment, register_judge, run_judge

    _judge_env()

    prompt_path = prompt_path.resolve()
    record = register_judge(mode=mode, prompt_text=prompt_path.read_text(), judge_model=judge_model)
    judge_id = record["judge_id"]
    run_judge(judge_id, split="dev", batch_size=10)
    metrics = judge_alignment(judge_id, split="dev")
    out = REPO / "analysis" / "report" / f"dev-{judge_id}.json"
    out.write_text(json.dumps({"judge_id": judge_id, "prompt": str(prompt_path.relative_to(REPO)),
                               "model": judge_model, "metrics": metrics}, indent=2) + "\n")
    return {"judge_id": judge_id, "metrics": metrics, "report": str(out.relative_to(REPO))}


def run_test(judge_id: str) -> dict[str, Any]:
    from analysis.helpers import freeze_judge, judge_alignment, run_judge

    _judge_env()

    freeze_judge(judge_id)
    run_judge(judge_id, split="test", batch_size=10)
    metrics = judge_alignment(judge_id, split="test")
    out = REPO / "analysis" / "report" / f"test-{judge_id}.json"
    out.write_text(json.dumps({"judge_id": judge_id, "metrics": metrics}, indent=2) + "\n")
    return {"judge_id": judge_id, "metrics": metrics, "report": str(out.relative_to(REPO))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["seed", "inputs", "split", "dev", "test"])
    parser.add_argument("--mode", default=MODE)
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--judge", type=str)
    args = parser.parse_args()

    if args.step == "seed":
        print(json.dumps(seed_hw5_labels(args.mode), indent=2))
    elif args.step == "inputs":
        print(json.dumps(prepare_inputs(args.mode), indent=2))
    elif args.step == "split":
        print(json.dumps(split_data(args.mode), indent=2, default=str))
    elif args.step == "dev":
        if not args.prompt:
            parser.error("dev needs --prompt")
        print(json.dumps(run_development(args.mode, args.prompt), indent=2, default=str))
    else:
        if not args.judge:
            parser.error("test needs --judge")
        print(json.dumps(run_test(args.judge), indent=2, default=str))


if __name__ == "__main__":
    main()
