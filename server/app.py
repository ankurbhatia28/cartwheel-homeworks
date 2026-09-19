"""The Cartwheel endpoint, with the session routes completed in Homework 2.

A thin FastAPI wrapper with three routes (Lecture 2.3):

  - POST /sessions            binds a user + role, returns a signed dev token
  - POST /sessions/{id}/messages   one conversation turn
  - GET  /health              liveness

Why an endpoint at all: one choke point to authenticate, log, sample,
rate-limit, and replay. Modules 3 and 4 need a surface to monitor and attack.

The token is dev-only auth: a base64 JSON payload signed with an HMAC over a
shared secret (CARTWHEEL_DEV_SECRET). It is not real auth; the *shape* (a
server-issued credential carrying user id + role that tools trust) is what
Module 4 attacks. In production you would stream responses and assemble the
final message in middleware; this server does not stream.

Run with:
    uv run uvicorn server.app:app --port 8010
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, NoReturn

from agents import Runner, SQLiteSession
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.openai_agents.utils import should_send_prompts
from pydantic import BaseModel

from agent import db
from agent.agent import DEFAULT_MODEL, build_agent, prompt_version
from agent.auth import ROLES, AuthContext
from agent.config import REPO_ROOT, db_path
from observability.instrument import load_env, setup_raindrop, setup_tracing

MAX_TURNS = 12  # cap runaway loops; keeps conversations bounded
SESSIONS_DB = REPO_ROOT / ".sessions.db"
# Cap on live sessions. Each SQLiteSession holds its own connection to
# SESSIONS_DB, so an uncapped dict leaks a file descriptor (several, once the
# session is used from more than one worker thread) per conversation. A
# 250-scenario evaluation run exhausted macOS's default 256-descriptor limit
# after 47 conversations, and every later request failed with
# "unable to open database file". Evicting the least recently used session
# keeps the descriptor count flat for a run of any length.
MAX_ACTIVE_SESSIONS = 50

_tracer = trace.get_tracer("cartwheel.server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_env()
    setup_tracing()  # no-op with a warning if LANGFUSE_PUBLIC_KEY is unset
    setup_raindrop()  # HW4 Part C: opt-in local Workshop mirror (RAINDROP_LOCAL_DEBUGGER)
    yield


app = FastAPI(title="Cartwheel support agent", lifespan=lifespan)

# session_id -> (AuthContext, SQLiteSession). In-memory on purpose: the trace
# store is the durable record, not this dict. Ordered by least recently used:
# insertion order is the eviction order, and _touch_session moves an active
# session back to the end.
_SESSIONS: dict[str, tuple[AuthContext, SQLiteSession]] = {}


def _evict_sessions(limit: int = MAX_ACTIVE_SESSIONS) -> None:
    """Close and drop the least recently used sessions above `limit`.

    Closing releases the session's SQLite connections; the conversation itself
    survives in the trace store. An evicted session id then fails _authorize
    the same way an unknown one does.
    """
    while len(_SESSIONS) > limit:
        _session_id, (_ctx, session) = next(iter(_SESSIONS.items()))
        del _SESSIONS[_session_id]
        session.close()


def _touch_session(session_id: str) -> None:
    """Mark a session as most recently used, so traffic keeps it alive."""
    _SESSIONS[session_id] = _SESSIONS.pop(session_id)


# ---------------------------------------------------------------------------
# Signed dev token: base64url(JSON payload) + "." + HMAC-SHA256 signature.
# ---------------------------------------------------------------------------


def _secret() -> bytes:
    return os.environ.get("CARTWHEEL_DEV_SECRET", "cartwheel-dev-secret").encode()


def create_token(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True).encode()
    ).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Return the payload if the signature checks out, else None."""
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode()))
    except (binascii.Error, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class SessionCreate(BaseModel):
    user_id: int
    role: str


class MessageIn(BaseModel):
    message: str
    model: str | None = None
    # Set by the scenario runner (Lecture 3) so a trace links back to its
    # ground truth. Manual sessions leave it null.
    scenario_id: str | None = None


@app.post("/sessions")
def create_session(body: SessionCreate) -> dict[str, Any]:
    """Bind a verified database user to a new server-side session.

    Validate the requested role, load the user from the database, and reject
    a request whose claimed role differs from the stored role. Create an
    AuthContext and SQLiteSession, save them in _SESSIONS, then return the
    session id and a signed token. The token payload must contain session_id,
    user_id, role, store_id, and issued_at.
    """
    # The claimed role is decidable from the request alone, so reject it before
    # touching the database: an unknown role is a client error, not a lookup
    # failure, and the 400 stays deterministic whatever the database holds.
    if body.role not in ROLES:
        _reject_session(
            status_code=400,
            reason="unknown_role",
            detail=f"unknown role: {body.role!r}",
            user_id=body.user_id,
            role=body.role,
        )
    with db.connection() as conn:
        user = db.get_user(conn, body.user_id)
    if user is None:
        _reject_session(
            status_code=404,
            reason="unknown_user",
            detail=f"no user {body.user_id}",
            user_id=body.user_id,
            role=body.role,
        )
    if user.role != body.role:
        _reject_session(
            status_code=403,
            reason="role_mismatch",
            detail=f"user {user.id} is not a {body.role}",
            user_id=body.user_id,
            role=body.role,
        )

    # Identity comes from the database row, never from the request body and
    # never from a later chat message. The request only selects which user to
    # bind; note that SessionCreate has no store_id field at all, so a caller
    # cannot claim a store even in principle.
    ctx = AuthContext(user_id=user.id, role=user.role, store_id=user.store_id)
    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = (ctx, SQLiteSession(session_id, str(SESSIONS_DB)))
    _evict_sessions()
    token = create_token(
        {
            "session_id": session_id,
            "user_id": ctx.user_id,
            "role": ctx.role,
            "store_id": ctx.store_id,
            "issued_at": int(time.time()),
        }
    )
    return {"session_id": session_id, "token": token}


def _reject_session(
    *, status_code: int, reason: str, detail: str, user_id: int, role: str
) -> NoReturn:
    """Record a refused session attempt on its own span, then raise.

    A refused session never reaches a tool, so the cartwheel.permission_denied
    attribute that Homework 2 Part A puts on tool spans cannot see it. Without
    this span, probing POST /sessions to enumerate user identifiers and roles
    (404 for an absent user, 403 for a real user with another role) leaves no
    evidence anywhere.

    The claimed identity is recorded under `requested_*` names on purpose. It
    is caller-supplied and unverified, so it must never be written to
    cartwheel.user_id or cartwheel.user_role, which downstream analysis reads
    as the authenticated caller.
    """
    with _tracer.start_as_current_span("cartwheel.session_rejected") as span:
        if span.is_recording():
            span.set_attribute("cartwheel.session_rejected", True)
            span.set_attribute("cartwheel.session_rejected.reason", reason)
            span.set_attribute("cartwheel.requested_user_id", str(user_id))
            span.set_attribute("cartwheel.requested_role", role)
    raise HTTPException(status_code=status_code, detail=detail)


def _authorize(session_id: str, authorization: str | None) -> AuthContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = verify_token(authorization.removeprefix("Bearer "))
    if payload is None:
        raise HTTPException(status_code=401, detail="bad token signature")
    if payload.get("session_id") != session_id:
        raise HTTPException(status_code=403, detail="token is for another session")
    if session_id not in _SESSIONS:
        raise HTTPException(
            status_code=404,
            detail="unknown session (server restarted, or the session was evicted?)",
        )
    _touch_session(session_id)
    return _SESSIONS[session_id][0]


@app.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    body: MessageIn,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Run one authenticated conversation turn inside a root trace span.

    Authorize the token, recover the server-side session, and build the agent
    for the authenticated context. Hash only the system prompt template.
    The cartwheel.session_message span must record the session id, user role,
    user id, prompt version, and a nonempty scenario id when one is supplied. Run the
    agent inside that span, then return the session id, final reply, and
    prompt version.
    When TRACELOOP_TRACE_CONTENT is true, record gen_ai.input.messages and
    gen_ai.output.messages on the root span as JSON arrays of OTel GenAI
    messages with role and parts fields.
    """
    # Authorize before any other work: an unauthorized request must not build
    # an agent, call a model, or leave a span behind. _authorize returns the
    # AuthContext the server bound at session creation; nothing about identity
    # comes from this request or from the conversation.
    ctx = _authorize(session_id, authorization)
    _bound_ctx, session = _SESSIONS[session_id]
    agent = build_agent(ctx, model=body.model)
    version = prompt_version()
    # The course-level model name, resolved the way agent.resolve_model
    # resolves it. The provider's own identifier lands on the automatic model
    # span as gen_ai.request.model / gen_ai.response.model; this is the name
    # the caller asked for, which is what Part F holds constant.
    model_name = body.model or os.environ.get("CARTWHEEL_MODEL") or DEFAULT_MODEL

    with _tracer.start_as_current_span("cartwheel.session_message") as span:
        recording = span.is_recording()
        if recording:
            span.set_attribute("cartwheel.session_id", session_id)
            span.set_attribute("cartwheel.user_role", ctx.role)
            span.set_attribute("cartwheel.user_id", str(ctx.user_id))
            span.set_attribute("cartwheel.prompt_version", version)
            span.set_attribute("cartwheel.model", model_name)
            if body.scenario_id:
                span.set_attribute("cartwheel.scenario_id", body.scenario_id)
            # should_send_prompts() is the instrumentation's own predicate, so
            # the root span and the automatic model spans can never disagree
            # about whether content was captured. Set the input before the run
            # so a failed request still records what was asked.
            if should_send_prompts():
                span.set_attribute(
                    "gen_ai.input.messages", _genai_messages("user", body.message)
                )
        try:
            result = await Runner.run(
                agent,
                body.message,
                session=session,
                context=ctx,
                max_turns=MAX_TURNS,
            )
        except Exception as exc:
            # The span context manager already records the exception and sets
            # ERROR status. The class name is what makes the failure mode
            # queryable: a looping agent (MaxTurnsExceeded) and a provider
            # outage are the same 500 to the caller.
            if recording:
                span.set_attribute("cartwheel.run_error", type(exc).__name__)
            raise
        reply = str(result.final_output)
        if recording and should_send_prompts():
            span.set_attribute(
                "gen_ai.output.messages", _genai_messages("assistant", reply)
            )
    return {"session_id": session_id, "reply": reply, "prompt_version": version}


def _genai_messages(role: str, content: str) -> str:
    """One OTel GenAI message, serialized for a span attribute."""
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": content}]}])


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "db_exists": db_path().exists(),
        "active_sessions": len(_SESSIONS),
    }
