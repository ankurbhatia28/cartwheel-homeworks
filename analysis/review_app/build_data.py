"""Build the review app's conversation cache and cluster map.

Pulls the HW3 final-run traces from Langfuse (or, offline, from the committed
export), groups them by ``cartwheel.session_id`` into conversations, and
rebuilds each turn as an ordered timeline:

    user message -> thinking -> reasoning text -> tool call -> tool result -> ... -> reply

Langfuse records every ``openai.response`` generation with ``output = null``,
so the agent's thinking and the text it writes before each tool call only
survive inside the *next* generation's input. The last generation of a turn
therefore holds the whole chain for that turn; the tool results come from the
TOOL spans, which carry structured JSON and the ``cartwheel.*`` attributes.

Usage (from the repository root):

    uv run python analysis/review_app/build_data.py                 # live Langfuse
    uv run python analysis/review_app/build_data.py --source traces/support_traces.json

Writes ``analysis/review_app/data/conversations.json`` (a cache, gitignored)
and ``analysis/state/graph.json`` (the 2D map with clusters).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

APP_DIR = Path(__file__).resolve().parent
DATA_PATH = APP_DIR / "data" / "conversations.json"
GRAPH_PATH = REPO / "analysis" / "state" / "graph.json"
# Every scenario set whose traces the app may show: HW3's 250 and HW5's
# targeted refund run. A conversation with no matching record still renders,
# but without its tuple or expected outcome.
SCENARIOS_PATHS = [REPO / "scenarios" / "support_scenarios.jsonl",
                   REPO / "scenarios" / "hw5_scenarios.jsonl"]

# Decision 1 (hw4overview.md): the HW3 final run, and nothing else in Langfuse.
WINDOW_START = "2026-09-16T00:40:00+00:00"
WINDOW_END = "2026-09-16T01:45:00+00:00"
SESSION_CONTEXT_HEADER = "## Session context"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _attrs(metadata: Any) -> dict[str, Any]:
    """OpenTelemetry attributes arrive as a dict from the API, a string from ClickHouse."""
    raw = (metadata or {}).get("attributes") if isinstance(metadata, dict) else None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw or {}


def _to_dict(record: Any) -> dict[str, Any]:
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json", by_alias=True)
    return json.loads(record.json(by_alias=True))


def load_from_langfuse(window: tuple[str, str] = (WINDOW_START, WINDOW_END), prefix: str = "support-") -> list[dict[str, Any]]:
    from observability.instrument import load_env

    load_env()
    from langfuse import Langfuse

    lf = Langfuse()
    start, end = datetime.fromisoformat(window[0]), datetime.fromisoformat(window[1])
    summaries, page = [], 1
    while True:
        resp = lf.api.trace.list(page=page, limit=100, from_timestamp=start, to_timestamp=end)
        batch = list(resp.data or [])
        summaries.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    traces = []
    for summary in summaries:
        # Langfuse can read-timeout while it is still ingesting a run, so retry
        # rather than lose the whole pull on one slow trace.
        for attempt in range(4):
            try:
                full = _to_dict(lf.api.trace.get(summary.id))
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                print(f"  retry {attempt + 1}/3 for {summary.id}: {type(exc).__name__}", file=sys.stderr)
                time.sleep(3 * (attempt + 1))
        if str(_attrs(full.get("metadata")).get("cartwheel.scenario_id", "")).startswith(prefix):
            traces.append(full)
    return traces


def load_from_export(path: Path, window: tuple[str, str] = (WINDOW_START, WINDOW_END)) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    return [t for t in payload["traces"] if window[0][:19] <= t["timestamp"][:19] < window[1][:19]]


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def _text(messages: Any) -> str:
    out = []
    for message in messages or []:
        for part in message.get("parts", []):
            if part.get("type") == "text" and isinstance(part.get("content"), str):
                out.append(part["content"])
    return "\n\n".join(out)


def _thinking_text(part: dict[str, Any]) -> str:
    content = part.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("thinking") or ""
    return part.get("thinking") or ""


def _parse_result(raw: Any) -> Any:
    """Tool responses inside generation inputs are Python reprs; fall back to the raw string."""
    if not isinstance(raw, str):
        return raw
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(raw)
        except (ValueError, SyntaxError):
            continue
    return raw


def _result_flags(result: Any, attrs: dict[str, Any]) -> list[str]:
    flags = []
    if str(attrs.get("cartwheel.permission_denied")).lower() == "true":
        flags.append("permission_denied")
    if isinstance(result, dict):
        if result.get("ok") is False and result.get("error") != "permission_denied":
            flags.append(f"error:{result.get('error', 'unknown')}")
        status = result.get("status")
        if status in ("queued_for_approval", "auto_approved", "cancelled"):
            flags.append(status)
        if "ticket_id" in result:
            flags.append("escalated")
    if str(attrs.get("cartwheel.tool_error")).lower() == "true" and not any(f.startswith("error") for f in flags):
        flags.append("tool_error")
    return flags


def build_turn(trace: dict[str, Any], index: int) -> tuple[dict[str, Any], str | None]:
    observations = sorted(trace.get("observations") or [], key=lambda o: o.get("startTime") or "")
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    tool_spans = [o for o in observations if o.get("type") == "TOOL"]
    history = (generations[-1].get("input") or []) if generations else []

    system_prompt = None
    for message in history:
        if message.get("role") == "system":
            system_prompt = _text([message])
            break

    # Everything after the last user message belongs to this turn.
    last_user = max((i for i, m in enumerate(history) if m.get("role") == "user"), default=-1)
    items: list[dict[str, Any]] = []
    for message in history[last_user + 1 :]:
        for part in message.get("parts", []):
            kind = part.get("type")
            if kind == "thinking":
                items.append({"kind": "thinking", "text": _thinking_text(part)})
            elif kind == "text" and message.get("role") == "assistant":
                items.append({"kind": "reasoning", "text": part.get("content") or ""})
            elif kind == "tool_call":
                items.append({"kind": "call", "id": part.get("id"), "name": part.get("name"),
                              "arguments": part.get("arguments")})
            elif kind == "tool_call_response":
                items.append({"kind": "response", "id": part.get("id"),
                              "result": _parse_result(part.get("response", part.get("content")))})

    # Pair calls with results: by id from the generation input, then enrich
    # from the TOOL spans (structured JSON plus cartwheel.* attributes), in order.
    responses = {i["id"]: i["result"] for i in items if i["kind"] == "response" and i.get("id")}
    calls = [i for i in items if i["kind"] == "call"]
    for position, call in enumerate(calls):
        span = tool_spans[position] if position < len(tool_spans) else None
        if span is not None and span.get("name") != call["name"]:
            span = next((s for s in tool_spans if s.get("name") == call["name"] and not s.get("_used")), None)
        if span is not None:
            span["_used"] = True
        attrs = _attrs(span.get("metadata")) if span else {}
        result = span.get("output") if span and span.get("output") is not None else responses.get(call.get("id"))
        call.update({"result": result, "span_id": span.get("id") if span else None,
                     "flags": _result_flags(result, attrs)})
    # Tool spans the history didn't mention (defensive; not seen in the inspected traces).
    for span in tool_spans:
        if not span.get("_used"):
            attrs = _attrs(span.get("metadata"))
            calls.append({"kind": "call", "id": None, "name": span.get("name"), "arguments": span.get("input"),
                          "result": span.get("output"), "span_id": span.get("id"),
                          "flags": _result_flags(span.get("output"), attrs)})
            items.append(calls[-1])

    # Group into steps: the reasoning that leads to a call, the call, its result.
    steps: list[dict[str, Any]] = []
    current: dict[str, Any] = {"thoughts": [], "calls": []}
    for item in items:
        if item["kind"] in ("thinking", "reasoning"):
            if current["calls"]:
                steps.append(current)
                current = {"thoughts": [], "calls": []}
            current["thoughts"].append(item)
        elif item["kind"] == "call":
            current["calls"].append({k: item.get(k) for k in ("name", "arguments", "result", "span_id", "flags")})
    if current["thoughts"] or current["calls"]:
        steps.append(current)
    for n, step in enumerate(steps):
        step["id"] = f"t{index}s{n}"

    turn_flags = sorted({f for step in steps for call in step["calls"] for f in call["flags"]})
    turn = {
        "index": index,
        "trace_id": trace["id"],
        "timestamp": trace["timestamp"],
        "latency": round(trace.get("latency") or 0, 2),
        "cost": round(trace.get("totalCost") or 0, 5),
        "user": _text(trace.get("input")),
        "steps": steps,
        "reply": _text(trace.get("output")),
        "flags": turn_flags,
        "html_path": trace.get("htmlPath"),
    }
    return turn, system_prompt


def split_system_prompt(prompt: str | None) -> tuple[str, str]:
    """Separate the per-session context block from the static instructions."""
    if not prompt or SESSION_CONTEXT_HEADER not in prompt:
        return "", prompt or ""
    head, rest = prompt.split(SESSION_CONTEXT_HEADER, 1)
    body, sep, tail = rest.partition("\n## ")
    context = (SESSION_CONTEXT_HEADER + body).strip()
    static = (head + (("## " + tail) if sep else "")).strip()
    return context, static


def build_conversations(traces: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    scenarios = {json.loads(line)["id"]: json.loads(line)
                 for path in SCENARIOS_PATHS if path.exists()
                 for line in path.read_text().splitlines() if line.strip()}
    sessions: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        sid = _attrs(trace.get("metadata")).get("cartwheel.session_id")
        sessions.setdefault(sid, []).append(trace)

    conversations, statics = [], Counter()
    for session_id, session_traces in sessions.items():
        session_traces.sort(key=lambda t: t["timestamp"])
        attrs = _attrs(session_traces[0].get("metadata"))
        turns, context = [], ""
        for i, trace in enumerate(session_traces):
            turn, prompt = build_turn(trace, i)
            turns.append(turn)
            if prompt and not context:
                context, static = split_system_prompt(prompt)
                statics[static] += 1
        scenario = scenarios.get(attrs.get("cartwheel.scenario_id"), {})
        tools = [c["name"] for t in turns for s in t["steps"] for c in s["calls"]]
        conversations.append({
            "id": session_id,
            "scenario_id": attrs.get("cartwheel.scenario_id"),
            "role": attrs.get("cartwheel.user_role"),
            "user_id": attrs.get("cartwheel.user_id"),
            "model": attrs.get("cartwheel.model"),
            "prompt_version": attrs.get("cartwheel.prompt_version"),
            "session_context": context,
            "scenario": {
                "group": scenario.get("scenario_group"),
                "data_quality_case_id": scenario.get("data_quality_case_id"),
                "tuple": scenario.get("tuple", {}),
                "expected": scenario.get("expected", {}),
            },
            "turns": turns,
            "trace_ids": [t["trace_id"] for t in turns],
            "tools": tools,
            "flags": sorted({f for t in turns for f in t["flags"]}),
            "latency": round(sum(t["latency"] for t in turns), 2),
            "cost": round(sum(t["cost"] for t in turns), 5),
            "started_at": turns[0]["timestamp"],
        })
    conversations.sort(key=lambda c: c["scenario_id"] or "")
    static_prompt = statics.most_common(1)[0][0] if statics else ""
    return conversations, static_prompt


# ---------------------------------------------------------------------------
# cluster map
# ---------------------------------------------------------------------------


def build_graph(conversations: list[dict[str, Any]], k: int = 8, seed: int = 7) -> dict[str, Any]:
    """Project conversations to 2D and cluster them on observable features.

    Features are what the reviewer can see before judging: role, intent,
    scenario group, turn count, which tools ran, flags raised, reply length,
    and latency. Nothing about expected outcomes, so clusters don't encode a
    prediction of failure.
    """
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    def vocab(key):
        return sorted({v for c in conversations for v in key(c) if v is not None})

    roles = vocab(lambda c: [c["role"]])
    intents = vocab(lambda c: [c["scenario"]["tuple"].get("intent")])
    tool_names = vocab(lambda c: c["tools"])
    flag_names = vocab(lambda c: [f.split(":")[0] for f in c["flags"]])

    rows = []
    for c in conversations:
        tools = Counter(c["tools"])
        flags = {f.split(":")[0] for f in c["flags"]}
        row = [float(c["role"] == r) for r in roles]
        row += [float(c["scenario"]["tuple"].get("intent") == i) for i in intents]
        row += [float(c["scenario"]["group"] == "challenge")]
        row += [min(tools[t], 3) / 3 for t in tool_names]
        row += [float(f in flags) for f in flag_names]
        row += [len(c["turns"]), len(c["tools"]), sum(len(t["reply"]) for t in c["turns"]) / 1000, c["latency"]]
        rows.append(row)
    X = StandardScaler().fit_transform(np.array(rows))
    labels = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(X)
    xy = PCA(n_components=2, random_state=seed).fit_transform(X)
    xy = (xy - xy.min(axis=0)) / (xy.max(axis=0) - xy.min(axis=0) + 1e-9)

    nodes = []
    for c, (x, y), label in zip(conversations, xy, labels):
        nodes.append({"id": c["id"], "scenario_id": c["scenario_id"], "x": round(float(x), 4), "y": round(float(y), 4),
                      "cluster": int(label), "role": c["role"], "intent": c["scenario"]["tuple"].get("intent"),
                      "group": c["scenario"]["group"], "turns": len(c["turns"]), "tools": len(c["tools"])})
    clusters = []
    for label in range(k):
        members = [i for i, lab in enumerate(labels) if lab == label]
        centre = X[members].mean(axis=0)
        order = sorted(members, key=lambda i: float(((X[i] - centre) ** 2).sum()))
        clusters.append({"id": label, "size": len(members),
                         "representatives": [conversations[i]["id"] for i in order[:10]],
                         "top_intents": [i for i, _ in Counter(nodes[m]["intent"] for m in members).most_common(3)],
                         "top_roles": [r for r, _ in Counter(nodes[m]["role"] for m in members).most_common(2)]})
    return {"features": {"roles": roles, "intents": intents, "tools": tool_names, "flags": flag_names},
            "k": k, "seed": seed, "nodes": nodes, "clusters": clusters}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=None, help="offline export instead of live Langfuse")
    parser.add_argument("--window", nargs=2, metavar=("START", "END"), default=[WINDOW_START, WINDOW_END],
                        help="ISO timestamps to pull; defaults to the HW3 final run")
    parser.add_argument("--prefix", default="support-", help="scenario id prefix to keep")
    parser.add_argument("--no-graph", action="store_true",
                        help="leave analysis/state/graph.json alone (HW4's cluster map is a committed artifact)")
    parser.add_argument("--merge", action="store_true",
                        help="keep the conversations already in the cache and add these to them "
                             "(HW5 adds a second run without rebuilding HW3's 250)")
    args = parser.parse_args()

    window = (args.window[0], args.window[1])
    traces = load_from_export(args.source, window) if args.source else load_from_langfuse(window, args.prefix)
    conversations, static_prompt = build_conversations(traces)
    graph_conversations = conversations
    if args.merge and DATA_PATH.exists():
        prior = json.loads(DATA_PATH.read_text())
        fresh = {c["id"] for c in conversations}
        conversations = [c for c in prior["conversations"] if c["id"] not in fresh] + conversations
        static_prompt = static_prompt or prior.get("static_system_prompt", "")
    graph = None if args.no_graph else build_graph(graph_conversations)

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(args.source) if args.source else "langfuse",
        "window": list(window),
        "static_system_prompt": static_prompt,
        "conversations": conversations,
    }, indent=1))
    if graph is not None:
        GRAPH_PATH.write_text(json.dumps(graph, indent=1))
    turns = sum(len(c["turns"]) for c in conversations)
    print(f"{len(conversations)} conversations, {turns} traces -> {DATA_PATH.relative_to(REPO)}")
    if graph is None:
        print(f"cluster map left unchanged ({GRAPH_PATH.relative_to(REPO)})")
    else:
        print(f"{len(graph['clusters'])} clusters -> {GRAPH_PATH.relative_to(REPO)}: "
              + ", ".join(f"c{c['id']}={c['size']}" for c in graph["clusters"]))


if __name__ == "__main__":
    main()
