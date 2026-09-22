"""HW4 review app: a file-backed server for reviewing Cartwheel conversations.

Adapted from the reference ``analysis/server.py`` and the error-discovery
skill. The unit of review is a conversation (one ``cartwheel.session_id``),
not a single trace, because Cartwheel writes one trace per user turn.

Run from the repository root, after building the cache:

    uv run python analysis/review_app/build_data.py
    uv run python analysis/review_app/server.py            # http://localhost:8020
    uv run python analysis/review_app/server.py --offline  # never writes Langfuse scores

API:

    GET  /                          the review app
    GET  /api/conversations         conversation summaries (for lists and navigation)
    GET  /api/conversation/<id>     one full conversation
    GET  /api/meta                  static system prompt, cache details
    GET  /api/graph                 2D map with clusters        (state/graph.json)
    GET  POST /api/samples          the reviewed sample          (state/sample_manifest.json)
    GET  POST /api/annotations      human open codes             (state/annotations.json)
    GET  POST /api/patterns         the taxonomy                 (state/patterns.json)
    GET  POST /api/suggestions      AI suggestions and decisions (state/suggestions.json)
    GET  /api/label_drafts          suggested labels (draft_labels.py); never saved as labels
    GET  /api/hw5_candidates        HW5 candidate conversations for one mode
    GET  POST /api/hw5_labels/<mode> HW5 labels (1 = Pass, 0 = Fail), one record per trace
    GET  /api/labels                live Pass/Fail per conversation and mode
    POST /api/labels                save labels: local files + Langfuse scores

Labels are written per trace, because the handout labels traces: a
conversation's judgment for a mode is recorded on each of its turn traces.
Saves are idempotent: unchanged cells are skipped and each Langfuse score has a
stable id per (trace, mode). Label files are append-only; a changed label marks the old record
``superseded_by`` (the convention ``analysis/helpers/tools._load_labels`` reads).
Stored labels use the helpers' convention: 1 = failure present, 0 = absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = REPO / "analysis" / "state"
LABELS_DIR = STATE_DIR / "labels"
HW5_LABELS_DIR = STATE_DIR / "hw5_labels"  # HW5 convention: 1 = Pass, 0 = Fail
DATA_PATH = APP_DIR / "data" / "conversations.json"

FILES = {
    "/api/samples": (STATE_DIR / "sample_manifest.json", {"batches": [], "conversations": []}),
    "/api/annotations": (STATE_DIR / "annotations.json", {"annotations": []}),
    "/api/patterns": (STATE_DIR / "patterns.json", {"modes": []}),
    "/api/suggestions": (STATE_DIR / "suggestions.json", []),
    "/api/graph": (STATE_DIR / "graph.json", {"nodes": [], "clusters": []}),
    "/api/label_drafts": (STATE_DIR / "label_drafts.json", {"drafts": {}}),
    "/api/hw5_candidates": (STATE_DIR / "hw5_candidates.json", {"mode": None, "candidates": []}),
}
WRITABLE = {"/api/samples", "/api/annotations", "/api/patterns", "/api/suggestions"}
# Labels from the course demo fixture; not part of this review.
IGNORED_LABEL_FILES = {"unsupported_policy_claim"}

_lock = threading.Lock()
OFFLINE = False
LANGFUSE_HOST = "http://localhost:3000"  # replaced from .env at startup; only the URL, never keys


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text()
    return json.loads(text) if text.strip() else default


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# conversation cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}


def _conversations() -> dict[str, Any]:
    """The conversation cache, reloaded whenever build_data.py rewrites it.

    Keyed on the file's mtime: a rebuild (HW5 merged its 60 targeted runs into
    HW3's 250) must reach a server that is already running, or the new
    conversations are invisible until someone restarts it.
    """
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"{DATA_PATH} missing: run analysis/review_app/build_data.py first")
    mtime = DATA_PATH.stat().st_mtime_ns
    if _cache.get("mtime") != mtime:
        payload = json.loads(DATA_PATH.read_text())
        _cache.clear()
        _cache["mtime"] = mtime
        _cache["payload"] = payload
        _cache["by_id"] = {c["id"]: c for c in payload["conversations"]}
    return _cache


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    t = c["scenario"]["tuple"]
    return {
        "id": c["id"], "scenario_id": c["scenario_id"], "role": c["role"], "user_id": c["user_id"],
        "group": c["scenario"]["group"], "intent": t.get("intent"), "difficulty": t.get("difficulty"),
        "style": t.get("user_style"), "turns": len(c["turns"]), "tools": len(c["tools"]),
        "flags": c["flags"], "latency": c["latency"], "trace_ids": c["trace_ids"],
        "opening": (c["turns"][0]["user"] if c["turns"] else "")[:140],
    }


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def _label_rows(mode: str) -> list[dict[str, Any]]:
    path = LABELS_DIR / f"{mode}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _live_labels() -> dict[str, dict[str, Any]]:
    """{mode: {conversation_id: {label, evidence, ts}}} from the append-only files."""
    conversations = _conversations()["by_id"]
    trace_to_conv = {tid: cid for cid, c in conversations.items() for tid in c["trace_ids"]}
    out: dict[str, dict[str, Any]] = {}
    if not LABELS_DIR.exists():
        return out
    for path in sorted(LABELS_DIR.glob("*.jsonl")):
        mode = path.stem
        if mode in IGNORED_LABEL_FILES:
            continue
        live = {row["trace_id"]: row for row in _label_rows(mode) if not row.get("superseded_by")}
        for trace_id, row in live.items():
            if trace_id not in trace_to_conv:
                continue
            out.setdefault(mode, {})[trace_to_conv[trace_id]] = {
                "label": row["label"], "evidence": row.get("evidence", ""), "ts": row.get("ts"),
                "langfuse": row.get("langfuse_score", False)}
    return out


def label_score_id(trace_id: str, mode: str) -> str:
    """One stable Langfuse score id per (trace, mode), so re-saving overwrites instead of duplicating."""
    return hashlib.sha1(f"hw4-label:{trace_id}:{mode}".encode()).hexdigest()[:32]


def _hw5_live(mode: str) -> dict[str, dict[str, Any]]:
    path = HW5_LABELS_DIR / f"{mode}.jsonl"
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {r["trace_id"]: r for r in rows if not r.get("superseded_by")}


def _hw5_labels(mode: str) -> dict[str, Any]:
    """{conversation_id: {label, evidence, origin}} for the HW5 mode (1 = Pass)."""
    conversations = _conversations()["by_id"]
    trace_to_conv = {tid: cid for cid, c in conversations.items() for tid in c["trace_ids"]}
    out: dict[str, Any] = {}
    for trace_id, row in _hw5_live(mode).items():
        cid = trace_to_conv.get(trace_id)
        if cid:
            out[cid] = {"label": row["label"], "evidence": row.get("evidence", ""), "origin": row.get("origin", "human")}
    return out


def _save_hw5_labels(mode: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Append HW5 labels (1 = Pass, 0 = Fail), one record per trace."""
    conversations = _conversations()["by_id"]
    path = HW5_LABELS_DIR / f"{mode}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    live = {r["trace_id"]: r for r in rows if not r.get("superseded_by")}
    written, errors = 0, []
    for item in items:
        conv = conversations.get(item["conversation_id"])
        label = int(item["label"])
        if conv is None or label not in (0, 1):
            errors.append(f"{item.get('conversation_id')}: unknown conversation or bad label")
            continue
        for trace_id in conv["trace_ids"]:
            label_id = f"{trace_id}#{uuid.uuid4().hex[:8]}"
            prior = live.get(trace_id)
            if prior:
                if prior["label"] == label and prior.get("evidence", "") == item.get("evidence", ""):
                    continue
                prior["superseded_by"] = label_id
            record = {"trace_id": trace_id, "label": label, "source": "human", "ts": _now(),
                      "label_id": label_id, "conversation_id": conv["id"], "scenario_id": conv["scenario_id"],
                      "evidence": item.get("evidence", ""), "origin": "hw5_review"}
            rows.append(record)
            live[trace_id] = record
            written += 1
    HW5_LABELS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    counts = {"pass": sum(r["label"] == 1 for r in live.values()), "fail": sum(r["label"] == 0 for r in live.values())}
    return {"written": written, "errors": errors, "traces": counts}


def _save_labels(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Save (conversation, mode) judgments: one record per trace, one pass per mode file.

    Idempotent: a cell whose live record already has the same label and evidence
    is skipped, and each Langfuse score uses a stable id, so a repeated save
    neither duplicates records nor duplicates scores. Langfuse is flushed once.
    """
    conversations = _conversations()["by_id"]
    written, skipped, scored, errors = 0, 0, 0, []
    client = None
    if not OFFLINE:
        try:
            from observability.instrument import load_env

            load_env()
            from langfuse import Langfuse

            client = Langfuse()
        except Exception as exc:  # the local mirror still saves
            errors.append(f"Langfuse unavailable, saved locally only: {exc}")

    by_mode: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_mode.setdefault(item["mode"], []).append(item)

    for mode, mode_items in by_mode.items():
        path = LABELS_DIR / f"{mode}.jsonl"
        rows = _label_rows(mode)
        live = {r["trace_id"]: r for r in rows if not r.get("superseded_by")}
        changed = False
        for item in mode_items:
            conv_id, label = item["conversation_id"], int(item["label"])
            conv = conversations.get(conv_id)
            if label not in (0, 1) or conv is None:
                errors.append(f"{conv_id}/{mode}: invalid label or unknown conversation")
                continue
            evidence = item.get("evidence", "")
            for trace_id in conv["trace_ids"]:
                prior = live.get(trace_id)
                if prior and prior["label"] == label and prior.get("evidence", "") == evidence and bool(prior.get("langfuse_score")) == (client is not None):
                    skipped += 1
                    continue
                label_id = f"{trace_id}#{uuid.uuid4().hex[:8]}"
                if prior:
                    prior["superseded_by"] = label_id
                record = {"trace_id": trace_id, "label": label, "source": "human", "ts": _now(),
                          "label_id": label_id, "conversation_id": conv_id, "scenario_id": conv["scenario_id"],
                          "evidence": evidence}
                if item.get("suggested") is not None:
                    # What the draft suggested, so agreement with the drafts stays inspectable.
                    record["suggested_label"] = item["suggested"].get("label")
                    record["suggestion_basis"] = item["suggested"].get("basis")
                if client is not None:
                    try:
                        client.create_score(name=mode, value=label, trace_id=trace_id, data_type="NUMERIC",
                                            score_id=label_score_id(trace_id, mode), comment=evidence or None)
                        record["langfuse_score"] = True
                        scored += 1
                    except Exception as exc:
                        errors.append(f"{trace_id}/{mode}: Langfuse score failed: {exc}")
                rows.append(record)
                live[trace_id] = record
                written += 1
                changed = True
        if changed:
            LABELS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    if client is not None:
        client.flush()
    return {"written": written, "skipped_unchanged": skipped, "langfuse_scores": scored, "errors": errors}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/conversations":
                return self._send(200, [_summary(c) for c in _conversations()["payload"]["conversations"]])
            if path.startswith("/api/conversation/"):
                conv = _conversations()["by_id"].get(path.rsplit("/", 1)[1])
                return self._send(200, conv) if conv else self._send(404, {"error": "unknown conversation"})
            if path == "/api/meta":
                p = _conversations()["payload"]
                return self._send(200, {"static_system_prompt": p["static_system_prompt"], "built_at": p["built_at"],
                                        "langfuse_host": LANGFUSE_HOST,
                                        "source": p["source"], "window": p["window"], "offline": OFFLINE,
                                        "count": len(p["conversations"])})
            if path == "/api/labels":
                return self._send(200, _live_labels())
            if path.startswith("/api/hw5_labels/"):
                return self._send(200, _hw5_labels(path.rsplit("/", 1)[1]))
            if path in FILES:
                file, default = FILES[path]
                return self._send(200, _read(file, default))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        with _lock:
            try:
                if path == "/api/labels":
                    return self._send(200, _save_labels(body if isinstance(body, list) else [body]))
                if path.startswith("/api/hw5_labels/"):
                    return self._send(200, _save_hw5_labels(path.rsplit("/", 1)[1], body if isinstance(body, list) else [body]))
                if path in WRITABLE:
                    _write(FILES[path][0], body)
                    return self._send(200, {"saved": True})
                return self._send(404, {"error": "not found"})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

    def log_message(self, *args: Any) -> None:
        pass


def main() -> None:
    global OFFLINE
    parser = argparse.ArgumentParser(description="HW4 conversation review app")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--offline", action="store_true", help="save labels locally only, no Langfuse scores")
    args = parser.parse_args()
    OFFLINE = args.offline
    global LANGFUSE_HOST
    from observability.instrument import load_env

    load_env()
    import os

    LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", LANGFUSE_HOST).rstrip("/")
    _conversations()
    print(f"Review app on http://localhost:{args.port}  ({len(_cache['by_id'])} conversations"
          f"{', offline' if OFFLINE else ''})")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
