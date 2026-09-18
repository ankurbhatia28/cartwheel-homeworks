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
    GET  /api/labels                live Pass/Fail per conversation and mode
    POST /api/labels                save labels: local files + Langfuse scores

Labels are written per trace, because the handout labels traces: a
conversation's judgment for a mode is recorded on each of its turn traces.
Label files are append-only; a changed label marks the old record
``superseded_by`` (the convention ``analysis/helpers/tools._load_labels`` reads).
Stored labels use the helpers' convention: 1 = failure present, 0 = absent.
"""

from __future__ import annotations

import argparse
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
DATA_PATH = APP_DIR / "data" / "conversations.json"

FILES = {
    "/api/samples": (STATE_DIR / "sample_manifest.json", {"batches": [], "conversations": []}),
    "/api/annotations": (STATE_DIR / "annotations.json", {"annotations": []}),
    "/api/patterns": (STATE_DIR / "patterns.json", {"modes": []}),
    "/api/suggestions": (STATE_DIR / "suggestions.json", []),
    "/api/graph": (STATE_DIR / "graph.json", {"nodes": [], "clusters": []}),
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
    if not _cache:
        if not DATA_PATH.exists():
            raise FileNotFoundError(f"{DATA_PATH} missing: run analysis/review_app/build_data.py first")
        payload = json.loads(DATA_PATH.read_text())
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


def _save_labels(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Append one record per trace for each (conversation, mode) judgment."""
    conversations = _conversations()["by_id"]
    written, scored, errors = 0, 0, []
    client = None
    if not OFFLINE:
        try:
            from observability.instrument import load_env

            load_env()
            from langfuse import Langfuse

            client = Langfuse()
        except Exception as exc:  # the local mirror still saves
            errors.append(f"Langfuse unavailable, saved locally only: {exc}")
    from analysis.helpers.langfuse_io import write_label_score

    for item in items:
        mode, conv_id, label = item["mode"], item["conversation_id"], int(item["label"])
        if label not in (0, 1):
            errors.append(f"{conv_id}/{mode}: label must be 0 or 1")
            continue
        conv = conversations.get(conv_id)
        if conv is None:
            errors.append(f"unknown conversation {conv_id}")
            continue
        path = LABELS_DIR / f"{mode}.jsonl"
        rows = _label_rows(mode)
        for trace_id in conv["trace_ids"]:
            label_id = f"{trace_id}#{uuid.uuid4().hex[:8]}"
            for row in rows:
                if row["trace_id"] == trace_id and not row.get("superseded_by"):
                    row["superseded_by"] = label_id
            record = {"trace_id": trace_id, "label": label, "source": "human", "ts": _now(),
                      "label_id": label_id, "conversation_id": conv_id, "scenario_id": conv["scenario_id"],
                      "evidence": item.get("evidence", "")}
            if client is not None:
                try:
                    write_label_score(trace_id, mode, label, comment=item.get("evidence") or None, client=client)
                    record["langfuse_score"] = True
                    scored += 1
                except Exception as exc:
                    errors.append(f"{trace_id}/{mode}: Langfuse score failed: {exc}")
            rows.append(record)
            written += 1
        LABELS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return {"written": written, "langfuse_scores": scored, "errors": errors}


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
