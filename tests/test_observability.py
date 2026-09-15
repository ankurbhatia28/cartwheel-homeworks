"""Homework 2 tests.

Part A (here): the application attributes `record_tool_result` adds to a tool
span. The handout does not ask for these; instrumentation is code, and the
fields it writes are a contract two downstream consumers already depend on
(`reports/smoke.sql` counts permission denials, and
`analysis/helpers/normalization.py` reads the role and store for Module 2),
so they are worth pinning.

Part D authentication tests are added to this file when that part is done.

Everything here runs offline: no Docker, no Langfuse, no model provider key.
Spans go to an in-memory exporter.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fastapi import HTTPException

from agent.agent import prompt_version
from agent.auth import AuthContext
from observability.instrument import record_tool_result

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
MERCHANT_STORE_2 = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")

Recorder = Callable[[AuthContext, Any], ReadableSpan]


@pytest.fixture
def record_on_span() -> Recorder:
    """Run the recorder inside a real recording span; return the finished span.

    A recording span is the point: `record_tool_result` returns early when the
    active span is not recording, so asserting against the default no-op span
    would pass vacuously no matter what the body did.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    def record(ctx: AuthContext, result: Any) -> ReadableSpan:
        with tracer.start_as_current_span("execute_tool"):
            record_tool_result(ctx, result)
        return exporter.get_finished_spans()[-1]

    try:
        yield record
    finally:
        provider.shutdown()


def test_allowed_call_records_identity_and_no_denial(record_on_span: Recorder) -> None:
    span = record_on_span(SHOPPER_1, {"ok": True, "order": {"order_id": 4127}})

    assert span.attributes["cartwheel.user_role"] == "shopper"
    assert span.attributes["cartwheel.user_id"] == "1"
    assert span.attributes["cartwheel.permission_denied"] is False
    assert "cartwheel.permission_denied.reason" not in span.attributes
    assert "cartwheel.tool_error" not in span.attributes
    # Only merchants carry a store.
    assert "cartwheel.store_id" not in span.attributes


def test_permission_denied_is_a_bool_the_smoke_report_can_count(
    record_on_span: Recorder,
) -> None:
    """`reports/smoke.sql` compares JSONExtractString(...) = 'true'.

    A Python bool serializes to JSON `true`; the string "True" does not, and
    the query would silently count zero denials. `is False`/`is True` pins the
    type, not just the value.
    """
    allowed = record_on_span(SHOPPER_1, {"ok": True})
    denied = record_on_span(SHOPPER_1, {"ok": False, "error": "permission_denied", "reason": "nope"})

    assert allowed.attributes["cartwheel.permission_denied"] is False
    assert denied.attributes["cartwheel.permission_denied"] is True


def test_merchant_denial_records_store_and_reason(record_on_span: Recorder) -> None:
    result = {
        "ok": False,
        "error": "permission_denied",
        "reason": "role 'merchant' (user 9002) may not view order #4127",
    }
    span = record_on_span(MERCHANT_STORE_2, result)

    assert span.attributes["cartwheel.user_role"] == "merchant"
    assert span.attributes["cartwheel.user_id"] == "9002"
    # A string, per the HW2 contract. OTLP encodes int64 as a quoted string,
    # so an int attribute reached Langfuse as "2" anyway; sending a string
    # makes the stored value match what was sent.
    assert span.attributes["cartwheel.store_id"] == "2"
    assert isinstance(span.attributes["cartwheel.store_id"], str)
    assert span.attributes["cartwheel.permission_denied"] is True
    assert span.attributes["cartwheel.permission_denied.reason"] == result["reason"]
    assert span.attributes["cartwheel.tool_error"] == "permission_denied"


def test_real_denied_tool_result_is_attributed(world: dict, record_on_span: Recorder) -> None:
    """The hw1-session.jsonl record 4 case, end to end through the real tool.

    Merchant 9002 belongs to store 2; order 4127 belongs to store 1. Using the
    actual tool result rather than a hand-built dict keeps this test honest if
    the denial shape ever changes.
    """
    from agent.agent import get_order_logic

    result = get_order_logic(MERCHANT_STORE_2, 4127)
    assert result["error"] == "permission_denied"  # guard the premise

    span = record_on_span(MERCHANT_STORE_2, result)
    assert span.attributes["cartwheel.permission_denied"] is True
    assert "order #4127" in span.attributes["cartwheel.permission_denied.reason"]


@pytest.mark.parametrize(
    "error", ["not_found", "not_eligible", "invalid_argument", "paused", "not_implemented"]
)
def test_non_permission_failures_record_their_error_code(
    record_on_span: Recorder, error: str
) -> None:
    """Beyond the handout: every failure is attributed, not just denials.

    Without this, a trace can show that a tool refused but not why, and Module
    2 cannot group traces by failure mode.
    """
    span = record_on_span(SUPPORT, {"ok": False, "error": error, "reason": "..."})

    assert span.attributes["cartwheel.tool_error"] == error
    # A refusal is not a denial, and not a span error either.
    assert span.attributes["cartwheel.permission_denied"] is False
    assert span.status.status_code is StatusCode.UNSET


def test_expected_refusals_are_not_span_errors(record_on_span: Recorder) -> None:
    """A deliberate omission, pinned so a later change cannot flip it quietly.

    A permission denial and an ineligible refund are the system working
    correctly. Setting the span status to ERROR would make every guardrail
    look like an outage in error-rate dashboards.
    """
    for result in (
        {"ok": True},
        {"ok": False, "error": "permission_denied", "reason": "nope"},
        {"ok": False, "error": "not_eligible", "reason": "outside the window"},
    ):
        assert record_on_span(SHOPPER_1, result).status.status_code is StatusCode.UNSET


def test_recorder_never_raises_on_an_odd_result(record_on_span: Recorder) -> None:
    """This runs after the tool has already committed its writes.

    If the recorder raised, the SDK's default tool-error function would report
    a generic failure to the model for a call that actually succeeded, and the
    model could reasonably retry a refund that already went through.
    """
    for result in (None, "not a dict", 42, [], {}):
        span = record_on_span(SHOPPER_1, result)
        assert span.attributes["cartwheel.permission_denied"] is False


def test_no_op_when_tracing_is_off() -> None:
    """Outside a recording span the recorder must do nothing, quietly.

    This is what lets the whole suite run with tracing disabled.
    """
    assert trace.get_current_span().is_recording() is False
    record_tool_result(SHOPPER_1, {"ok": False, "error": "permission_denied", "reason": "x"})


# ---------------------------------------------------------------------------
# Part D: authentication. The handout requires the first two tests; the rest
# cover paths the supplied contract test leaves unchecked, and the rejection
# span added in Part B.
#
# Still offline: create_session and _authorize are called as functions, so no
# server, no Langfuse, and no model provider key is involved.
# ---------------------------------------------------------------------------


@pytest.fixture
def server_app(world: dict):
    """The server module with an empty session table."""
    from server import app as module

    module._SESSIONS.clear()
    try:
        yield module
    finally:
        module._SESSIONS.clear()


def _open_session(module, user_id: int, role: str) -> dict[str, Any]:
    return module.create_session(module.SessionCreate(user_id=user_id, role=role))


def test_session_creation_rejects_a_role_the_database_does_not_confirm(
    server_app,
) -> None:
    """Required by Part D. User 1 is a shopper in the database."""
    with pytest.raises(HTTPException) as exc:
        _open_session(server_app, 1, "merchant")

    assert exc.value.status_code == 403
    assert server_app._SESSIONS == {}  # no session was bound


def test_token_for_one_session_cannot_authorize_another(server_app) -> None:
    """Required by Part D."""
    shopper = _open_session(server_app, 1, "shopper")
    merchant = _open_session(server_app, 9002, "merchant")

    # Positive control: the token works on its own session.
    ctx = server_app._authorize(shopper["session_id"], f"Bearer {shopper['token']}")
    assert (ctx.user_id, ctx.role) == (1, "shopper")

    with pytest.raises(HTTPException) as exc:
        server_app._authorize(merchant["session_id"], f"Bearer {shopper['token']}")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "user_id,role,status",
    [
        (1, "admin", 400),  # role that does not exist
        (999_999, "shopper", 404),  # user that does not exist
        (9002, "shopper", 403),  # real user, wrong role
        (1, "support", 403),
    ],
)
def test_session_creation_status_codes(server_app, user_id, role, status) -> None:
    """Beyond the handout: the supplied contract test only covers the 200."""
    with pytest.raises(HTTPException) as exc:
        _open_session(server_app, user_id, role)

    assert exc.value.status_code == status
    assert server_app._SESSIONS == {}


def test_token_carries_the_database_identity_not_the_request(server_app) -> None:
    """The thesis of Part B: the server decides who you are.

    SessionCreate has no store_id field, so a caller cannot claim a store even
    by sending one; the store on the bound context comes from the users table.
    """
    response = _open_session(server_app, 9002, "merchant")
    payload = server_app.verify_token(response["token"])

    assert payload["user_id"] == 9002
    assert payload["role"] == "merchant"
    assert payload["store_id"] == 2  # from the database row, not the request

    ctx, _session = server_app._SESSIONS[response["session_id"]]
    assert (ctx.user_id, ctx.role, ctx.store_id) == (9002, "merchant", 2)

    claimed = server_app.SessionCreate(
        **{"user_id": 1, "role": "shopper", "store_id": 99, "user_role": "support"}
    )
    assert not hasattr(claimed, "store_id")
    assert claimed.role == "shopper"


def test_missing_and_malformed_tokens_are_unauthorized(server_app) -> None:
    session = _open_session(server_app, 1, "shopper")
    sid = session["session_id"]

    for header in (None, "", "Basic abc", "Bearer not-a-token", "Bearer a.b"):
        with pytest.raises(HTTPException) as exc:
            server_app._authorize(sid, header)
        assert exc.value.status_code == 401


def test_refused_sessions_are_recorded_on_a_span(server_app, monkeypatch) -> None:
    """Part B extra: a refused session never reaches a tool.

    Without this span the 404/403 split that lets an unauthenticated caller
    enumerate user ids and roles leaves no evidence at all.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(server_app, "_tracer", provider.get_tracer(__name__))

    try:
        for user_id, role, reason in [
            (1, "admin", "unknown_role"),
            (999_999, "shopper", "unknown_user"),
            (1, "merchant", "role_mismatch"),
        ]:
            with pytest.raises(HTTPException):
                _open_session(server_app, user_id, role)
            span = exporter.get_finished_spans()[-1]
            assert span.name == "cartwheel.session_rejected"
            assert span.attributes["cartwheel.session_rejected"] is True
            assert span.attributes["cartwheel.session_rejected.reason"] == reason
            # Unverified, caller-supplied identity must never be written to
            # cartwheel.user_id, which downstream analysis reads as the
            # authenticated caller.
            assert span.attributes["cartwheel.requested_user_id"] == str(user_id)
            assert "cartwheel.user_id" not in span.attributes
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# Part C: the traced message endpoint.
#
# The handout verifies Part C by eye in Langfuse (Part E), so nothing proves
# the root span is right without Docker. These tests do, offline: the real
# agent loop and the real endpoint, with a scripted model and an in-memory
# span exporter.
# ---------------------------------------------------------------------------


@pytest.fixture
def traced_server(server_app, monkeypatch):
    """Post a message to the real endpoint; return (response, root span).

    The scripted model keeps this offline, and monkeypatching the server's
    tracer captures the span this part is responsible for creating.
    """
    import asyncio

    from agent import agent as support
    from tests.eval.fake_model import FakeModel, text_message

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(server_app, "_tracer", provider.get_tracer(__name__))
    # Pin content capture instead of inheriting it. instrument_genai() does
    # os.environ.setdefault("TRACELOOP_TRACE_CONTENT", "false") while the
    # instrumentation's own default is "true", so whether messages are
    # captured otherwise depends on which tests ran first.
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "true")

    def post(session: dict[str, Any], reply: str = "Hi.", **body: Any):
        model = FakeModel()
        model.set_next_output([text_message(reply)])
        monkeypatch.setattr(support, "resolve_model", lambda _name: model)
        body.setdefault("message", "Show my recent orders.")
        response = asyncio.run(
            server_app.post_message(
                session["session_id"],
                server_app.MessageIn(**body),
                authorization=f"Bearer {session['token']}",
            )
        )
        spans = [s for s in exporter.get_finished_spans()
                 if s.name == "cartwheel.session_message"]
        return response, spans[-1]

    try:
        yield post, exporter
    finally:
        provider.shutdown()


def test_message_response_carries_the_keys_hw3_reads(server_app, traced_server) -> None:
    """scenarios/runner.py reads reply["reply"]; that key is a contract."""
    post, _exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    response, _span = post(session, reply="Here are your orders.")

    assert response["reply"] == "Here are your orders."
    assert response["session_id"] == session["session_id"]
    assert response["prompt_version"] == prompt_version()


def test_root_span_records_the_authenticated_identity(server_app, traced_server) -> None:
    post, _exporter = traced_server
    session = _open_session(server_app, 9002, "merchant")

    _response, span = post(session)

    assert span.attributes["cartwheel.session_id"] == session["session_id"]
    assert span.attributes["cartwheel.user_role"] == "merchant"
    assert span.attributes["cartwheel.user_id"] == "9002"
    assert span.attributes["cartwheel.prompt_version"] == prompt_version()
    assert span.status.status_code is StatusCode.UNSET


def test_scenario_id_is_recorded_only_when_supplied(server_app, traced_server) -> None:
    """The absent case is the one that is easy to get wrong."""
    post, _exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    _r, with_id = post(session, scenario_id="support-0001")
    assert with_id.attributes["cartwheel.scenario_id"] == "support-0001"

    _r, without = post(session)
    assert "cartwheel.scenario_id" not in without.attributes

    _r, empty = post(session, scenario_id="")
    assert "cartwheel.scenario_id" not in empty.attributes


def test_messages_use_the_otel_genai_shape(server_app, traced_server) -> None:
    """Parse the attributes back: valid JSON in the documented structure."""
    post, _exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    _response, span = post(session, message="Where is order 4127?", reply="Delivered.")

    sent = json.loads(span.attributes["gen_ai.input.messages"])
    got = json.loads(span.attributes["gen_ai.output.messages"])
    assert sent == [
        {"role": "user", "parts": [{"type": "text", "content": "Where is order 4127?"}]}
    ]
    assert got == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Delivered."}]}
    ]


def test_content_capture_off_keeps_identity_but_drops_messages(
    server_app, traced_server, monkeypatch
) -> None:
    """TRACELOOP_TRACE_CONTENT governs message content, not identity.

    Turning it off does not make a trace PII-free: the caller's id and role
    are still recorded, by design.
    """
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "false")
    post, _exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    _response, span = post(session)

    assert "gen_ai.input.messages" not in span.attributes
    assert "gen_ai.output.messages" not in span.attributes
    assert span.attributes["cartwheel.user_id"] == "1"


def test_requested_model_is_recorded_on_the_root_span(
    server_app, traced_server, monkeypatch
) -> None:
    """Part C extra: Part F requires the same model across two runs.

    Without this the model only appears on a child span, so a root-span query
    cannot confirm the precondition.
    """
    post, _exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    _r, asked = post(session, model="glm-5.2")
    assert asked.attributes["cartwheel.model"] == "glm-5.2"

    monkeypatch.setenv("CARTWHEEL_MODEL", "claude-opus-4-6")
    _r, defaulted = post(session)
    assert defaulted.attributes["cartwheel.model"] == "claude-opus-4-6"


def test_a_failed_run_is_attributed_and_still_raises(
    server_app, traced_server, monkeypatch
) -> None:
    """Part C extra: a looping agent and a provider outage are both a 500.

    cartwheel.run_error is what separates them in aggregate. The span keeps
    the automatic ERROR status, unlike an expected tool refusal (Part A).
    """
    import asyncio

    from agents.exceptions import MaxTurnsExceeded

    post, exporter = traced_server
    session = _open_session(server_app, 1, "shopper")

    class Looping:
        @staticmethod
        async def run(*_args: Any, **_kwargs: Any) -> Any:
            raise MaxTurnsExceeded("max turns exceeded")

    monkeypatch.setattr(server_app, "Runner", Looping)
    with pytest.raises(MaxTurnsExceeded):
        asyncio.run(
            server_app.post_message(
                session["session_id"],
                server_app.MessageIn(message="loop forever"),
                authorization=f"Bearer {session['token']}",
            )
        )

    span = [s for s in exporter.get_finished_spans()
            if s.name == "cartwheel.session_message"][-1]
    assert span.attributes["cartwheel.run_error"] == "MaxTurnsExceeded"
    assert span.status.status_code is StatusCode.ERROR
    assert "gen_ai.output.messages" not in span.attributes
    # The question was still recorded, which is the point of setting the
    # input attribute before the run.
    assert "gen_ai.input.messages" in span.attributes


def test_unauthorized_request_creates_no_span(server_app, traced_server) -> None:
    """A security property that Langfuse cannot show you.

    An absent trace is not inspectable, so the only place this can be checked
    is a test: a rejected request must not build an agent, call a model, or
    leave a span behind.
    """
    import asyncio

    post, exporter = traced_server
    session = _open_session(server_app, 1, "shopper")
    other = _open_session(server_app, 9002, "merchant")

    for sid, header in [
        (session["session_id"], None),
        (session["session_id"], "Bearer forged.signature"),
        (other["session_id"], f"Bearer {session['token']}"),
        ("nonexistent-session", f"Bearer {session['token']}"),
    ]:
        with pytest.raises(HTTPException):
            asyncio.run(
                server_app.post_message(
                    sid, server_app.MessageIn(message="hello"), authorization=header
                )
            )

    assert [s for s in exporter.get_finished_spans()
            if s.name == "cartwheel.session_message"] == []
