"""Homework 9, Part D: a proxy-then-oracle cascade on the HW5 judge.

The oracle is the frozen HW5 judge (promises_unconfirmed_outcome-v3). Its
verdicts on the HW5 dev and test splits are already saved, so it is never
called here. The proxy is a less expensive model given the oracle's exact
prompt and DocETL wrapper text, plus one instruction asking for its
confidence in the verdict.

Subcommands, in the order the handout requires:

  run --split dev        call the proxy on the 62 dev conversations
  sweep                  offline: thresholds.csv from the saved dev output
  choose --threshold T   record the chosen threshold (before any test output)
  run --split test       call the proxy on the 62 test conversations
  check                  offline: check.json at the chosen threshold, once

`run` prints the model and call count and stops unless --yes is passed.
Proxy output is saved per conversation and a rerun skips finished ones.

Usage: uv run python -m cascade.run_proxy <subcommand> [...]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "cascade" / "results"
REQUIREMENT = REPO / "cascade" / "requirement.json"
CHOSEN = RESULTS / "chosen_threshold.json"
MODE = "promises_unconfirmed_outcome"
ORACLE_ID = f"{MODE}-v3"
THRESHOLDS = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99, 1.0]
ENC = tiktoken.get_encoding("o200k_base")

# The oracle's DocETL wrapper, verbatim from analysis/helpers/scale.py:231.
WRAPPER_BEFORE = "\n\n--- Trace to evaluate ---\n"
WRAPPER_AFTER = (
    "\n\n"
    "First write a critique of the trace against the criterion. "
    "Use specific evidence from the provided trace. Then return result "
    "as exactly Pass when the named failure is absent, or Fail when present."
)
# The one added instruction. DocETL enforced the {critique, result} schema on
# the oracle; the proxy is asked for the same keys plus confidence as JSON.
CONFIDENCE_INSTRUCTION = (
    "\n\nAlso state your confidence that your result is correct, as a number "
    "from 0 to 1. Respond with a JSON object with the keys critique, result, "
    "and confidence."
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokens(text: str) -> int:
    return len(ENC.encode(text))


def requirement() -> dict[str, Any]:
    return json.loads(REQUIREMENT.read_text())


def prices(model: str) -> dict[str, float]:
    config = json.loads((REPO / "optimize" / "config.json").read_text())
    return config["prices_per_million_tokens_usd"][model]


def hw5_data() -> dict[str, Any]:
    """Oracle prompt, oracle verdicts, human labels, splits, and conversation text.

    Uses the HW5 loaders read-only, so labels and verdicts follow the same
    conventions as judge_alignment: 1 means Pass for both below.
    """
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(REPO / "analysis" / "state" / "hw5_trace_inputs.json")
    sys.path.insert(0, str(REPO))
    from analysis.helpers import _state
    from analysis.helpers import tools as hw5
    from analysis.helpers.scale import load_store_traces

    oracle = hw5._load_judge(ORACLE_ID)
    assert oracle["status"] == "frozen", oracle["status"]
    failure_preds = hw5._cached_preds(oracle)
    failure_labels = {r["trace_id"]: r["label"] for r in hw5._load_labels(MODE)}
    return {
        "oracle": oracle,
        "oracle_pass": {t: 1 - int(p) for t, p in failure_preds.items()},
        "human_pass": {t: 1 - int(l) for t, l in failure_labels.items()},
        "critiques": oracle["critiques"][oracle["prompt_hash"]],
        "splits": _state.read_json(_state.state_path("splits.json"), default={})[MODE],
        "texts": {t["trace_id"]: str(t["text"]) for t in load_store_traces()},
    }


def proxy_prompt(oracle_prompt: str, text: str) -> str:
    return oracle_prompt + WRAPPER_BEFORE + text + WRAPPER_AFTER + CONFIDENCE_INSTRUCTION


def oracle_cost(data: dict[str, Any], trace_id: str) -> float:
    """Estimated cost of one oracle verdict, counted as in Part A.

    The oracle never runs here and DocETL saved no token counts, so the input
    is the prompt, wrapper, and conversation, and the output is the saved
    critique, all counted with gpt-4o-mini's o200k tokenizer.
    """
    p = prices(data["oracle"]["model"])
    sent = data["oracle"]["prompt_text"] + WRAPPER_BEFORE + data["texts"][trace_id] + WRAPPER_AFTER
    returned = json.dumps({"critique": data["critiques"].get(trace_id, ""), "result": "Pass"})
    return (tokens(sent) * p["input"] + tokens(returned) * p["output"]) / 1_000_000


def parse(text: str) -> tuple[int | None, float | None, str]:
    """Return (pass 1/0, confidence, critique) from the proxy's JSON reply."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        body = json.loads(match.group(0)) if match else {}
    result = str(body.get("result", "")).strip().lower()
    verdict = 1 if result == "pass" else 0 if result == "fail" else None
    try:
        confidence = min(1.0, max(0.0, float(body.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    return verdict, confidence, str(body.get("critique", ""))


def output_path(split: str) -> Path:
    return RESULTS / f"proxy_{split}.jsonl"


def read_output(split: str) -> list[dict[str, Any]]:
    path = output_path(split)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def run(args: argparse.Namespace) -> None:
    if args.split == "test" and not CHOSEN.exists():
        sys.exit("choose a threshold on dev first: the test split runs only after `choose`")
    req = requirement()
    model, effort = req["proxy_model"], args.reasoning_effort
    data = hw5_data()
    ids = data["splits"][args.split]
    done = {r["trace_id"] for r in read_output(args.split)}
    todo = [t for t in ids if t not in done][: args.limit]
    print(f"proxy {model} (reasoning_effort={effort}) on {args.split}: "
          f"{len(todo)} calls ({len(done)} of {len(ids)} already saved), 0 oracle calls")
    if not args.yes:
        print("stopping before any model call; pass --yes to run")
        return

    import litellm
    from observability.instrument import load_env

    load_env()
    p = prices(model)
    with output_path(args.split).open("a", encoding="utf-8") as out:
        for i, trace_id in enumerate(todo, 1):
            response = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": proxy_prompt(data["oracle"]["prompt_text"], data["texts"][trace_id])}],
                response_format={"type": "json_object"},
                reasoning_effort=effort,
                num_retries=3,
                timeout=120,
            )
            text = response.choices[0].message.content or ""
            usage = response.usage
            details = getattr(usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            completion_details = getattr(usage, "completion_tokens_details", None)
            reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0) if completion_details else 0
            verdict, confidence, critique = parse(text)
            cost = ((usage.prompt_tokens - cached) * p["input"] + cached * p["cached_input"]
                    + usage.completion_tokens * p["output"]) / 1_000_000
            record = {
                "trace_id": trace_id,
                "split": args.split,
                "model": model,
                "reasoning_effort": effort,
                "proxy_pass": verdict,
                "confidence": confidence,
                "critique": critique,
                "input_tokens": usage.prompt_tokens,
                "cached_input_tokens": cached,
                "output_tokens": usage.completion_tokens,
                "reasoning_tokens": reasoning,
                "cost_usd": round(cost, 8),
                "raw": text if verdict is None or confidence is None else None,
                "created_at": now(),
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(f"  {i}/{len(todo)} {trace_id[:8]} pass={verdict} conf={confidence} "
                  f"in={usage.prompt_tokens} out={usage.completion_tokens} (reasoning {reasoning})")


def evaluate(records: list[dict[str, Any]], data: dict[str, Any], threshold: float) -> dict[str, Any]:
    """Cascade verdicts at one threshold, compared with the oracle and the labels.

    A proxy reply without a usable verdict or confidence is sent to the
    oracle, as low confidence would be.
    """
    agree_oracle = agree_human = kept = 0
    cost = 0.0
    disagreements = []
    for r in records:
        t = r["trace_id"]
        keep = r["proxy_pass"] is not None and r["confidence"] is not None and r["confidence"] >= threshold
        verdict = r["proxy_pass"] if keep else data["oracle_pass"][t]
        kept += keep
        cost += r["cost_usd"] + (0.0 if keep else oracle_cost(data, t))
        agree_oracle += verdict == data["oracle_pass"][t]
        agree_human += verdict == data["human_pass"][t]
        if verdict != data["oracle_pass"][t]:
            disagreements.append(t)
    n = len(records)
    return {
        "threshold": threshold,
        "agreement_with_oracle": round(agree_oracle / n, 4),
        "agreement_with_human_labels": round(agree_human / n, 4),
        "proxy_share": round(kept / n, 4),
        "cost_per_1000_verdicts_usd": round(1000 * cost / n, 4),
        "n": n,
        "disagreements_with_oracle": len(disagreements),
        "disagreement_trace_ids": disagreements,
    }


def require_complete(split: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    records = read_output(split)
    missing = set(data["splits"][split]) - {r["trace_id"] for r in records}
    if missing:
        sys.exit(f"{len(missing)} {split} conversations have no proxy output yet; finish `run --split {split}`")
    return records


def sweep(args: argparse.Namespace) -> None:
    data = hw5_data()
    records = require_complete("dev", data)
    rows = [evaluate(records, data, t) for t in THRESHOLDS]
    oracle_only = statistics.mean(oracle_cost(data, r["trace_id"]) for r in records) * 1000
    fields = ["threshold", "agreement_with_oracle", "agreement_with_human_labels", "proxy_share", "cost_per_1000_verdicts_usd"]
    with (RESULTS / "thresholds.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    bar = requirement()["minimum_agreement_with_oracle"]
    print(f"oracle alone: ${oracle_only:.4f} per 1,000 verdicts (estimated); bar {bar}")
    for r in rows:
        mark = "meets bar" if r["agreement_with_oracle"] >= bar else ""
        print(f"  t={r['threshold']:<5} oracle {r['agreement_with_oracle']:.3f} ({r['disagreements_with_oracle']} off)  "
              f"human {r['agreement_with_human_labels']:.3f}  proxy share {r['proxy_share']:.3f}  "
              f"${r['cost_per_1000_verdicts_usd']:.4f}/1k  {mark}")
    confidences = [r["confidence"] for r in records]
    print("proxy confidences seen:", sorted({c for c in confidences if c is not None}),
          "| unusable replies:", sum(r["proxy_pass"] is None or r["confidence"] is None for r in records))


def choose(args: argparse.Namespace) -> None:
    if CHOSEN.exists():
        sys.exit(f"a threshold is already chosen in {CHOSEN.name}; it does not change after the test run")
    if read_output("test"):
        sys.exit("test output already exists; the threshold must be chosen before it")
    CHOSEN.write_text(json.dumps({"threshold": args.threshold, "chosen_at": now(),
                                  "reason": args.reason}, indent=2) + "\n")
    print(f"recorded threshold {args.threshold} in {CHOSEN.relative_to(REPO)}")


def check(args: argparse.Namespace) -> None:
    out = RESULTS / "check.json"
    if out.exists():
        sys.exit("check.json already exists; the test check runs once")
    data = hw5_data()
    records = require_complete("test", data)
    threshold = json.loads(CHOSEN.read_text())["threshold"]
    result = evaluate(records, data, threshold)
    bar = requirement()["minimum_agreement_with_oracle"]
    oracle_only = statistics.mean(oracle_cost(data, r["trace_id"]) for r in records) * 1000
    result.update({
        "split": "test",
        "proxy_model": requirement()["proxy_model"],
        "oracle": ORACLE_ID,
        "minimum_agreement_with_oracle": bar,
        "meets_requirement": result["agreement_with_oracle"] >= bar,
        "oracle_only_cost_per_1000_verdicts_usd": round(oracle_only, 4),
        "oracle_cost_note": "estimated by counting the oracle's prompt, conversation, and saved critique with o200k; proxy cost uses the API's token counts",
        "created_at": now(),
    })
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "disagreement_trace_ids"}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--split", choices=["dev", "test"], required=True)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--reasoning-effort", default="minimal", choices=["minimal", "low", "medium", "high"])
    p_run.add_argument("--yes", action="store_true")
    p_run.set_defaults(func=run)
    sub.add_parser("sweep").set_defaults(func=sweep)
    p_choose = sub.add_parser("choose")
    p_choose.add_argument("--threshold", type=float, required=True)
    p_choose.add_argument("--reason", required=True)
    p_choose.set_defaults(func=choose)
    sub.add_parser("check").set_defaults(func=check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
