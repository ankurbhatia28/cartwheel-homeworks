"""Run the monitor over one period, or over the last N hours (Homework 7).

    uv run python -m monitoring.run --period before
    uv run python -m monitoring.run --period after --yes
    uv run python -m monitoring.run --last-hours 24 --yes

Two selections come out of every period and stay separate for good:

  - a uniform random sample, which is the ONLY thing allowed to estimate the
    failure rate, and
  - the configured risk groups, which are there to be read by a person.

Both are judged in one batch by the frozen Homework 5 judge, with its prompt,
model, inputs and verdict parser untouched. The judge's verdicts arrive
failure-positive (1 = the failure is present), which is the opposite of the
Homework 5 label files and the same as everything else in monitoring/.

A run stops before the judge unless ``--yes`` is passed: judge calls cost
money, so the plan is printed and approved first.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analysis.helpers.normalization import normalize_trace
from monitoring.correct import corrected_mode_prevalence
from monitoring.run_judges import judge_sample, judge_test_data
from monitoring.sample import DEFAULT_RISK_GROUPS, select_traces
from monitoring.write_scores import build_score_records, post_scores

REPO = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO / "monitoring" / "config.json"
HISTORY_PATH = REPO / "monitoring" / "history.jsonl"
SCENARIOS_PATH = REPO / "scenarios" / "monitoring_scenarios.jsonl"


# ---------------------------------------------------------------------------
# config and Langfuse
# ---------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = json.loads(path.read_text())
    unknown = set(config["risk_groups"]) - set(DEFAULT_RISK_GROUPS)
    if unknown:
        raise ValueError(f"unknown risk groups: {sorted(unknown)}")
    return config


def period_window(config: dict[str, Any], label: str) -> tuple[datetime, datetime]:
    for period in config["periods"]:
        if period["label"] == label:
            start, end = period["from"], period["to"]
            if "FILL" in start or "FILL" in end:
                raise ValueError(f"period {label!r} has no recorded window yet")
            return (
                datetime.fromisoformat(start.replace("Z", "+00:00")),
                datetime.fromisoformat(end.replace("Z", "+00:00")),
            )
    raise ValueError(f"no period labelled {label!r} in the config")


def fetch_window(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every Cartwheel trace Langfuse holds in [start, end), normalized.

    Langfuse read-times out for a while after ingesting a run, so each page is
    retried rather than losing the whole pull to one slow request; the
    scheduled job in Part D has no one watching it.
    """
    from langfuse import Langfuse

    from observability.instrument import load_env

    load_env()
    # The SDK's default read timeout is short. A window holding a whole
    # scenario replay needs one trace.get per trace, and a ClickHouse query on
    # a cold cache regularly runs past the default, which failed the first
    # scheduled run after five retries.
    client = Langfuse(timeout=60)
    traces: list[dict[str, Any]] = []
    page = 1
    while True:
        for attempt in range(5):
            try:
                response = client.api.trace.list(
                    page=page, limit=50, from_timestamp=start, to_timestamp=end
                )
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        batch = list(response.data or [])
        for summary in batch:
            for attempt in range(5):
                try:
                    full = client.api.trace.get(summary.id)
                    break
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(5 * (attempt + 1))
            traces.append(normalize_trace(full))
        if len(batch) < 50:
            break
        page += 1
    return traces


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


def build_conversations(
    traces: list[dict[str, Any]], group_by: str = "scenario_id"
) -> list[dict[str, Any]]:
    """Collapse traces into one record per conversation.

    A multi-turn scenario writes one trace per turn, so the period's trace
    count and conversation count differ. Traces are ordered by timestamp and
    their text concatenated; **the last trace's id becomes the record id**, so
    a score written for the record lands on a trace Langfuse actually holds.

    The risk evidence (`tools`, `turn_count`) is read from the tool
    observations, never from the agent's reply: a reply that mentions a refund
    is not evidence that issue_refund ran.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        key = trace.get("meta", {}).get(group_by)
        if key:
            grouped.setdefault(str(key), []).append(trace)

    conversations = []
    for key, group in grouped.items():
        group.sort(key=lambda t: t.get("timestamp") or "")
        tools = sorted(
            {
                str(obs.get("name"))
                for trace in group
                for obs in trace.get("observations", [])
                if str(obs.get("type", "")).upper() == "TOOL" and obs.get("name")
            }
        )
        turn_count = sum(
            int(trace.get("features", {}).get("turn_count", 0)) for trace in group
        )
        conversations.append(
            {
                "id": group[-1]["id"],
                "key": key,
                "trace_ids": [trace["id"] for trace in group],
                "text": "\n".join(trace["text"] for trace in group if trace.get("text")),
                "tools": tools,
                # one user message per turn; the normalizer counts user and
                # assistant messages together, so halve it, floor 1.
                "turn_count": max(1, turn_count // 2),
                "models": sorted({m for trace in group for m in trace.get("models", [])}),
                "timestamp": group[0].get("timestamp"),
            }
        )
    conversations.sort(key=lambda c: (c.get("timestamp") or "", c["key"]))
    return conversations


def monitored_scenarios() -> set[str]:
    """The 50 scenario ids that define eligibility for a named period."""
    return {
        json.loads(line)["id"]
        for line in SCENARIOS_PATH.read_text().splitlines()
        if line.strip()
    }


def eligible_conversations(
    conversations: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Keep the monitored 50 and refuse a period that is not comparable.

    The HW3 window holds the whole 250-scenario run, so a period is not
    "everything in the time range": eligibility is membership in
    monitoring_scenarios.jsonl. Within that set the handout's two rules are
    refusals, not warnings, because a period missing a scenario or mixing
    models is not the same experiment as the other period.
    """
    expected = monitored_scenarios()
    eligible = [c for c in conversations if c["key"] in expected]
    missing = sorted(expected - {c["key"] for c in eligible})
    if missing:
        raise ValueError(
            f"period is incomplete: {len(missing)} scenario id(s) missing, "
            f"first few {missing[:5]}"
        )
    # Langfuse records the provider-routable id ("anthropic/claude-opus-4-6")
    # while the config names the course model. Compare on the bare name so a
    # genuine model change is still caught.
    def bare(name: str) -> str:
        return name.rsplit("/", 1)[-1]

    wanted = bare(config["model"])
    wrong = sorted(
        {m for c in eligible for m in c["models"] if m and bare(m) != wanted}
    )
    if wrong:
        raise ValueError(
            f"period mixes models: expected {config['model']!r}, also saw {wrong}"
        )
    return eligible


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def run_period(
    label: str | None,
    last_hours: int | None,
    approved: bool,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    config = load_config(config_path)
    scheduled = last_hours is not None
    if scheduled:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=last_hours)
        label = label or f"last-{last_hours}h"
    else:
        start, end = period_window(config, label or "")

    traces = fetch_window(start, end)
    # The scheduled job watches live traffic, which has no scenario ids, so it
    # groups by session. A named period replays the 50 scenarios and groups by
    # scenario id, which is what makes the two periods comparable.
    conversations = build_conversations(
        traces, group_by="session_id" if scheduled else "scenario_id"
    )

    print(f"period {label}: {start.isoformat()} -> {end.isoformat()}")
    print(f"  traces fetched: {len(traces)}  conversations: {len(conversations)}")

    if scheduled and not conversations:
        # An empty window is not a failure: the monitor records a zero and the
        # scheduled job exits green without spending anything on the judge.
        record = {
            "label": label,
            "judge_id": config["judge_id"],
            "model": config["model"],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "traces": 0,
            "conversations": 0,
            "random_sample": 0,
            "risk_sample": 0,
            "judged": 0,
            "note": "no eligible conversations in the window",
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        append_history(record)
        print("  no eligible conversations; recorded a zero count and stopped")
        return record

    if not scheduled:
        seen = len(conversations)
        conversations = eligible_conversations(conversations, config)
        print(f"  eligible: {len(conversations)} of {seen} conversations are monitored "
              f"scenarios; all 50 present, model {config['model']}")

    groups = {name: DEFAULT_RISK_GROUPS[name] for name in config["risk_groups"]}
    plan = select_traces(conversations, config["random_rate"], groups, seed=7)

    print(f"  random sample: {len(plan['random'])} "
          f"({config['random_rate']:.0%} of {len(conversations)})")
    for name, members in plan["risk_groups"].items():
        print(f"  risk group {name}: {len(members)}")
    print(f"  judge: {config['judge_id']}  ->  {len(plan['to_judge'])} judge calls")

    if not approved:
        print("\n  stopping before the judge. Re-run with --yes to spend.")
        return {"plan_only": True, "judge_calls": len(plan["to_judge"])}

    verdicts = judge_sample(config["judge_id"], plan["to_judge"])
    random_verdicts = {c["id"]: verdicts[c["id"]] for c in plan["random"]}
    risk_verdicts = {
        name: {c["id"]: verdicts[c["id"]] for c in members}
        for name, members in plan["risk_groups"].items()
    }

    labels, preds = judge_test_data(config["judge_id"])
    # Only the uniform sample estimates the rate; the risk groups are biased
    # toward failure by construction and would inflate it.
    estimate = corrected_mode_prevalence(list(random_verdicts.values()), labels, preds)

    # build_score_records takes one flat mapping for the risk side; a trace in
    # two groups is still one risk verdict.
    flat_risk = {tid: v for group in risk_verdicts.values() for tid, v in group.items()}
    records = build_score_records(
        config["judge_mode"], random_verdicts, flat_risk, estimate, label
    )
    # The period-level prevalence score has no trace, so it is anchored to a
    # session naming this monitoring period.
    written = post_scores(records, session_id=f"monitor-{config['judge_mode']}-{label}")

    record = {
        "label": label,
        "judge_id": config["judge_id"],
        "model": config["model"],
        "from": start.isoformat(),
        "to": end.isoformat(),
        "traces": len(traces),
        "conversations": len(conversations),
        "random_sample": len(random_verdicts),
        "risk_sample": sum(len(v) for v in risk_verdicts.values()),
        "judged": len(verdicts),
        "raw_rate": estimate["raw"],
        "corrected_rate": estimate["corrected"],
        "interval": [estimate["ci_low"], estimate["ci_high"]],
        "confidence": estimate["confidence"],
        "failure_sensitivity": estimate["failure_sensitivity"],
        "pass_specificity": estimate["pass_specificity"],
        "threshold": config["threshold"],
        "crossed_threshold": estimate["corrected"] > config["threshold"],
        "scores_written": written,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    append_history(record)
    print(f"\n  raw {record['raw_rate']:.3f} -> corrected {record['corrected_rate']:.3f} "
          f"{tuple(round(x, 3) for x in record['interval'])}")
    print(f"  threshold {config['threshold']}: "
          f"{'CROSSED' if record['crossed_threshold'] else 'not crossed'}")
    return record


def append_history(record: dict[str, Any], path: Path = HISTORY_PATH) -> None:
    """One line per monitor run; a repeat of a label replaces its line."""
    rows = []
    if path.exists():
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("label") != record["label"]]
    rows.append(record)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--period", help="a label from monitoring/config.json")
    group.add_argument("--last-hours", type=int, help="scheduled mode: the last N hours")
    parser.add_argument("--yes", action="store_true", help="approve the judge calls")
    args = parser.parse_args()
    run_period(args.period, args.last_hours, args.yes)


if __name__ == "__main__":
    main()
