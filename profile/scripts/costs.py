"""Homework 9, Part A: add up model cost by call site.

Reads only files already in the repository and makes no model calls:

  - traces/support_traces.json      HW3 customer conversations (Langfuse export)
  - optimize/results/*.json         HW8 development and test replays
  - optimize/state/final_version.json  HW8 GEPA search budget reservations
  - analysis/state/judges/          HW5 judge predictions and critiques
  - analysis/state/hw5_trace_inputs.json  the conversations the HW5 judge read
  - monitoring/history.jsonl        HW7 monitoring runs

Token counts come from the LLM API wherever they were saved. Where they were
not, the script counts the text that was sent or returned with the o200k
tokenizer (gpt-4o-mini's own), and the row's purpose says "estimated".

Usage: uv run python profile/scripts/costs.py

Run it by path: `profile` is also a standard-library module, so `-m` would
import that instead.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import tiktoken

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
OUT = REPO / "profile" / "results" / "costs.csv"
ENC = tiktoken.get_encoding("o200k_base")

# The wrapper text the two DocETL judges add around the frozen prompt
# (analysis/helpers/scale.py:231 and monitoring/run_judges.py:96).
DOCETL_WRAPPER = (
    "\n\n--- Trace to evaluate ---\n\n\n"
    "First write a critique of the trace against the criterion. "
    "Use specific evidence from the provided trace. Then return result "
    "as exactly Pass when the named failure is absent, or Fail when present."
)
POLICY_TOOLS = {"search_help_center", "get_policy"}


def tokens(text: Any) -> int:
    return len(ENC.encode(text if isinstance(text, str) else json.dumps(text)))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def prices() -> dict[str, dict[str, float]]:
    return read_json(REPO / "optimize" / "config.json")["prices_per_million_tokens_usd"]


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = prices()[model]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def row(category, call_site, purpose, model, calls, input_tokens, output_tokens, cost_usd):
    return {
        "cost_category": category,
        "call_site": call_site,
        "purpose": purpose,
        "model": model,
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 6),
    }


def frozen_judge() -> dict[str, Any]:
    return read_json(REPO / "analysis" / "state" / "judges" / "promises_unconfirmed_outcome-v3.json")


# ---------------------------------------------------------------------------
# Customer conversation calls
# ---------------------------------------------------------------------------


def hw3_conversations() -> dict[str, Any]:
    """HW3 server traces. Input tokens are the API's; output tokens are estimated.

    The export recorded zero output tokens and no output text on every
    generation, so output is counted from the final replies plus the tool-call
    arguments. That is a lower bound: Claude's tokenizer gives more tokens than
    o200k for the same text, and text written before a tool call was not kept.
    """
    data = read_json(REPO / "traces" / "support_traces.json")
    generations = [o for t in data["traces"] for o in t["observations"] if o["type"] == "GENERATION"]
    models = {o["model"] for o in generations}
    assert models == {"anthropic/claude-opus-4-6"}, models
    assert not any(o["usage"].get("output") for o in generations), "output tokens now present"
    input_tokens = sum(o["usage"]["input"] for o in generations)
    output_tokens = 0
    for trace in data["traces"]:
        for message in trace.get("output") or []:
            output_tokens += sum(tokens(str(part.get("content", ""))) for part in message.get("parts", []))
        output_tokens += sum(tokens(o.get("input")) for o in trace["observations"] if o["type"] == "TOOL")
    langfuse_input_cost = sum(o["calculatedTotalCost"] or 0 for o in generations)
    model = "claude-opus-4-6"
    total = langfuse_input_cost + output_tokens * prices()[model]["output"] / 1_000_000
    return row(
        "customer_conversation",
        "server/app.py:299",
        f"HW3: {data['scenario_count']} scenario conversations ({data['trace_count']} turns) "
        "through the server; input tokens and input cost from the API via Langfuse; "
        "output tokens estimated from final replies and tool-call arguments because the "
        "export recorded 0 output tokens (lower bound)",
        model, len(generations), input_tokens, output_tokens, total,
    )


def hw8_result_files() -> list[dict[str, Any]]:
    paths = sorted((REPO / "optimize" / "results").glob("development-*.json"))
    paths += sorted((REPO / "optimize" / "results").glob("test-configuration-*.json"))
    return [read_json(p) for p in paths]


def hw8_replays() -> list[dict[str, Any]]:
    """HW8 runner replays. Token counts and cost are the runner's, from the API.

    The runner saves conversations, not model requests, so `calls` counts
    evaluated conversations here.
    """
    rows = []
    results = hw8_result_files()
    for model in sorted({r["model"] for r in results}):
        mine = [r for r in results if r["model"] == model]
        splits = sorted({r["split"] for r in mine})
        rows.append(row(
            "customer_conversation",
            "replay/rollout.py:210",
            f"HW8 evaluation replays ({', '.join(splits)}; {len(mine)} runner {'run' if len(mine) == 1 else 'runs'}); tokens and cost "
            "from the API via optimize.runner; calls counts evaluated conversations because "
            "the runner does not save per-request counts; cost grows with how often you "
            "evaluate rather than with users",
            model,
            sum(r["evaluated_case_runs"] for r in mine),
            sum(r["input_tokens"] for r in mine),
            sum(r["output_tokens"] for r in mine),
            sum(r["cost_usd"] for r in mine),
        ))
    return rows


def gepa_reservations() -> list[str]:
    final = read_json(REPO / "optimize" / "state" / "final_version.json")
    return [r["label"] for r in final["search_budget"]["reservations"]]


def hw8_gepa_replays() -> dict[str, Any]:
    """GEPA's search replays on gpt-4o-mini, estimated from per-conversation averages.

    GEPA did not save token counts, so each reserved replay is priced at the
    mean of the HW8 gpt-4o-mini development runs.
    """
    dev = [r for r in hw8_result_files() if r["model"] == "gpt-4o-mini" and r["split"] == "development"]
    runs = sum(r["evaluated_case_runs"] for r in dev)
    per_in = sum(r["input_tokens"] for r in dev) / runs
    per_out = sum(r["output_tokens"] for r in dev) / runs
    calls = len(gepa_reservations())
    i, o = round(per_in * calls), round(per_out * calls)
    return row(
        "customer_conversation",
        "replay/rollout.py:210",
        f"HW8 GEPA search replays; estimated at the mean tokens per conversation of the "
        f"{len(dev)} gpt-4o-mini development runs because GEPA saved no token counts; "
        "calls counts evaluated conversations",
        "gpt-4o-mini", calls, i, o, cost("gpt-4o-mini", i, o),
    )


# ---------------------------------------------------------------------------
# Judge calls
# ---------------------------------------------------------------------------


def hw5_trace_texts() -> dict[str, str]:
    """The conversation text the HW5 judge read, normalized the same way."""
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(REPO / "analysis" / "state" / "hw5_trace_inputs.json")
    from analysis.helpers.scale import load_store_traces

    return {t["trace_id"]: str(t["text"]) for t in load_store_traces()}


def hw5_judge() -> dict[str, Any]:
    """HW5 DocETL judge calls, estimated from the saved prompts, inputs, and critiques.

    Predictions are cached by (prompt hash, trace id), so a version that reused
    an earlier prompt hash made no new calls. Counting distinct pairs gives the
    calls that reached the model. Reruns that overwrote a cache entry and
    DocETL's own schema instructions are not counted, so this is a lower bound.
    """
    texts = hw5_trace_texts()
    judges_dir = REPO / "analysis" / "state" / "judges"
    seen: set[tuple[str, str]] = set()
    calls = input_tokens = output_tokens = 0
    for path in sorted(judges_dir.glob("promises_unconfirmed_outcome-v*.json")):
        judge = read_json(path)
        prompt = tokens(judge["prompt_text"] + DOCETL_WRAPPER)
        for prompt_hash, predictions in judge["predictions"].items():
            critiques = judge["critiques"].get(prompt_hash, {})
            for trace_id in predictions:
                if (prompt_hash, trace_id) in seen:
                    continue
                seen.add((prompt_hash, trace_id))
                calls += 1
                input_tokens += prompt + tokens(texts[trace_id])
                output_tokens += tokens(json.dumps({"critique": critiques.get(trace_id, ""), "result": "Pass"}))
    model = frozen_judge()["model"]
    return row(
        "judge",
        "analysis/helpers/scale.py:231",
        "HW5: promises_unconfirmed_outcome judge, v0 to v3 on development and test labels; "
        "estimated from saved prompts, conversation inputs, and critiques because DocETL "
        "saved no token counts; distinct (prompt hash, trace) pairs (lower bound)",
        model, calls, input_tokens, output_tokens, cost(model, input_tokens, output_tokens),
    )


def hw5_means() -> tuple[float, float]:
    """Mean input and output tokens per call of the frozen v3 judge on HW5 data."""
    texts = hw5_trace_texts()
    judge = frozen_judge()
    prompt = tokens(judge["prompt_text"] + DOCETL_WRAPPER)
    [(prompt_hash, predictions)] = judge["predictions"].items()
    critiques = judge["critiques"][prompt_hash]
    ins = [prompt + tokens(texts[t]) for t in predictions]
    outs = [tokens(json.dumps({"critique": critiques[t], "result": "Pass"})) for t in predictions]
    return statistics.mean(ins), statistics.mean(outs)


def hw7_monitoring() -> dict[str, Any]:
    """HW7 monitoring judge calls. Only aggregates were saved, so tokens are estimated.

    Same frozen judge, prompt, and conversation format as HW5, on Opus
    conversations from the same scenarios, so each call is priced at the HW5
    v3 mean. Scheduled monitor workflow runs are not in the history and are not
    counted.
    """
    history = [json.loads(line) for line in (REPO / "monitoring" / "history.jsonl").read_text().splitlines() if line.strip()]
    calls = sum(r["judged"] for r in history)
    mean_in, mean_out = hw5_means()
    i, o = round(mean_in * calls), round(mean_out * calls)
    model = frozen_judge()["model"]
    return row(
        "judge",
        "monitoring/run_judges.py:96",
        f"HW7: {len(history)} monitoring periods ({', '.join(r['label'] for r in history)}), "
        "promises_unconfirmed_outcome-v3; estimated at the HW5 v3 mean tokens per call "
        "because only aggregates were saved",
        model, calls, i, o, cost(model, i, o),
    )


def policy_docs_mean() -> float:
    """Mean tokens of the judge's policy-document context per HW3 conversation.

    Formats retrieved documents the way replay/rollout.py:371
    (`retrieved_docs_text`) does: snippets for search hits, bodies for
    get_policy. A conversation that retrieved nothing sends a one-line
    placeholder, so it counts too.
    """
    data = read_json(REPO / "traces" / "support_traces.json")
    chunks: dict[str, list[str]] = {}
    for trace in data["traces"]:
        session = chunks.setdefault(trace["cartwheel_scenario_id"], [])
        for o in trace["observations"]:
            result = o.get("output")
            if o["type"] != "TOOL" or o["name"] not in POLICY_TOOLS:
                continue
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            if o["name"] == "search_help_center":
                session += [f"[{h['policy_id']}] {h.get('title', '')}: {h['snippet']}" for h in result.get("results", [])]
            else:
                session.append(f"[{result.get('policy_id')}] {result.get('body', '')}")
    return statistics.mean(
        tokens("\n\n".join(c) if c else "(no policy documents were retrieved)") for c in chunks.values()
    )


def hw8_case_judge() -> dict[str, Any]:
    """The per-reply judge on judged evaluation cases during HW8.

    Counted runs: those in the saved runner results that returned judge text,
    plus GEPA reservations on judged cases. The prompt, reply, and judge text are
    counted where saved; the retrieved policy documents were not saved, so each
    call adds the HW3 mean. HW6 baselines and CI runs saved no results locally
    and are not counted.
    """
    cases = [json.loads(line) for line in (REPO / "eval_cases" / "cases.jsonl").read_text().splitlines() if line.strip()]
    judged = {c["id"] for c in cases if c["expected"].get("judges")}
    judge = frozen_judge()
    system = tokens(judge["prompt_text"])
    docs = policy_docs_mean()
    calls = input_tokens = output_tokens = 0
    replies_in: list[int] = []
    replies_out: list[int] = []
    for result in hw8_result_files():
        for case in result["cases"]:
            for run in case.get("run_details", []):
                reasons = run.get("judge_reasons") or {}
                for text in reasons.values():
                    i = system + tokens(f"Agent reply:\n{run.get('final_reply', '')}\n\nPolicy documents retrieved in the trace:\n") + round(docs)
                    o = tokens(text)
                    calls += 1
                    input_tokens += i
                    output_tokens += o
                    replies_in.append(i)
                    replies_out.append(o)
    gepa_calls = sum(1 for label in gepa_reservations() if label.split(":")[1] in judged)
    gepa_in = round(statistics.mean(replies_in) * gepa_calls)
    gepa_out = round(statistics.mean(replies_out) * gepa_calls)
    model = judge["model"]
    total_in, total_out = input_tokens + gepa_in, output_tokens + gepa_out
    return row(
        "judge",
        "replay/rollout.py:426",
        f"HW8: frozen judge on judged evaluation cases ({', '.join(sorted(judged))}); "
        f"{calls} calls from saved runner results plus {gepa_calls} GEPA replays; "
        "estimated from saved replies and judge text plus the HW3 mean retrieved-policy size, "
        "because no token counts were saved; HW6 baseline and CI runs not counted",
        model, calls + gepa_calls, total_in, total_out, cost(model, total_in, total_out),
    )


def main() -> None:
    rows = [hw3_conversations(), *hw8_replays(), hw8_gepa_replays(), hw5_judge(), hw7_monitoring(), hw8_case_judge()]
    rows.sort(key=lambda r: (r["cost_category"], -r["cost_usd"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for r in rows:
        print(f"{r['cost_category']:22} {r['call_site']:32} {r['model']:16} "
              f"{r['calls']:>5} {r['input_tokens']:>10} {r['output_tokens']:>8} ${r['cost_usd']:.4f}")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
