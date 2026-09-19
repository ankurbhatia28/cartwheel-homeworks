"""Draft (suggested) Pass/Fail labels for every reviewed conversation x mode.

Suggestions only. The reviewer confirms or flips each row in the Labels view
before anything is saved to analysis/state/labels/ or Langfuse. Each draft
records its basis so the reviewer knows what to check:

  note          one of the reviewer's annotations is assigned to this mode -> Fail
  code          a deterministic check fired (only for the two code-check modes)
  not_applicable  the mode cannot occur in this conversation (e.g. no permission
                denial, not an order-status request) -> Pass
  no_note       nothing noted; the stopping rule means a second failure could
                still be present -> Pass, verify

    uv run python analysis/review_app/draft_labels.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "analysis" / "state"
DATA = Path(__file__).resolve().parent / "data" / "conversations.json"

POLICY_ID = re.compile(r"\b(cw-[a-z-]+|store-[a-z-]+-policy)\b")
TOOL_NAMES = ("get_order", "find_order", "list_my_orders", "issue_refund", "cancel_order", "escalate_to_human",
              "search_help_center", "get_policy", "search_products", "get_product", "check_return_eligibility")
DISCLOSURE = re.compile(r"\b\d+\s*(of|out of)\s*\d+|most recent|only (see|show|pull)|showing (the )?(first|top)|and \d+ more|more than", re.I)
DQ_STATES = {"product_missing_title", "order_missing_delivery_date", "order_reversed_dates",
             "order_store_mismatch", "product_duplicate_title", "product_invalid_price"}


def _replies(conv):
    return "\n".join(t["reply"] for t in conv["turns"])


def _calls(conv):
    return [c for t in conv["turns"] for s in t["steps"] for c in s["calls"]]


def _snippet(text, match, width=70):
    start = max(0, match.start() - width // 2)
    return text[start:match.end() + width // 2].replace("\n", " ").strip()


def code_exposes(conv):
    text = _replies(conv)
    m = POLICY_ID.search(text) or re.search(r"\b(" + "|".join(TOOL_NAMES) + r")\b", text)
    return (1, f"code: reply contains '{m.group(0)}' — …{_snippet(text, m)}…") if m else (0, "code: no policy id or tool name in the reply")


def code_completeness(conv):
    capped = []
    for c in _calls(conv):
        r = c.get("result")
        if c["name"] not in ("list_my_orders", "search_products") or not isinstance(r, dict):
            continue
        shown = len(r.get("orders") or r.get("products") or [])
        limit = (c.get("arguments") or {}).get("limit") if isinstance(c.get("arguments"), dict) else None
        if r.get("truncated") or (r.get("total_count") or 0) > shown or (limit and shown >= limit):
            capped.append(f"{c['name']} returned {shown}" + (f" of {r['total_count']}" if r.get("total_count") else f" (limit {limit})"))
    if not capped:
        return 0, "code: no capped list or search result"
    if DISCLOSURE.search(_replies(conv)):
        return 0, f"code: {capped[0]}; the reply discloses it — check the counts agree"
    return 1, f"code: {capped[0]}; the reply does not say the results are partial"


def applicability(mode, conv):
    t = conv["scenario"]["tuple"]
    if mode == "reveals_inaccessible_record" and not any("permission_denied" in c["flags"] for c in _calls(conv)):
        return "no permission denial in this conversation"
    if mode == "incomplete_status_answer" and t.get("intent") != "order_status":
        return "not an order-status request"
    if mode == "mishandles_bad_record" and t.get("record_state") not in DQ_STATES:
        return "no missing, inconsistent, or visibly wrong record field"
    return None


def main() -> None:
    convs = {c["id"]: c for c in json.loads(DATA.read_text())["conversations"]}
    sample = json.loads((STATE / "sample_manifest.json").read_text())["conversations"]
    annotations = {a["id"]: a for a in json.loads((STATE / "annotations.json").read_text())["annotations"]}
    modes = json.loads((STATE / "patterns.json").read_text())["modes"]

    drafts: dict[str, dict[str, dict]] = {}
    for mode in modes:
        name = mode["name"]
        noted: dict[str, list[str]] = {}
        for aid in mode["annotation_ids"]:
            a = annotations.get(aid)
            if a:
                noted.setdefault(a["conversation_id"], []).append(a["note"])
        out = drafts.setdefault(name, {})
        for entry in sample:
            conv = convs[entry["id"]]
            if conv["id"] in noted:
                out[conv["id"]] = {"label": 1, "basis": "note", "evidence": "your note: " + " | ".join(noted[conv["id"]])}
                continue
            if name == "exposes_internal_identifiers":
                label, why = code_exposes(conv)
                out[conv["id"]] = {"label": label, "basis": "code", "evidence": why}
                continue
            if name == "misstates_result_completeness":
                label, why = code_completeness(conv)
                out[conv["id"]] = {"label": label, "basis": "code", "evidence": why}
                continue
            reason = applicability(name, conv)
            if reason:
                out[conv["id"]] = {"label": 0, "basis": "not_applicable", "evidence": reason}
            else:
                out[conv["id"]] = {"label": 0, "basis": "no_note", "evidence": "no note for this mode; check for a second failure"}

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "note": "Suggestions only; the reviewer confirms or flips each before saving.", "drafts": drafts}
    (STATE / "label_drafts.json").write_text(json.dumps(payload, indent=2) + "\n")
    for name, cells in drafts.items():
        fails = sum(c["label"] for c in cells.values())
        bases = {}
        for c in cells.values():
            bases[c["basis"]] = bases.get(c["basis"], 0) + 1
        print(f"{name:31} suggested Fail {fails:3} / {len(cells)}  {bases}")


if __name__ == "__main__":
    main()
