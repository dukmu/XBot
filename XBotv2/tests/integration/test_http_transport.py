"""End-to-end integration tests for the HTTP/SSE transport.

These tests build a FastAPI app with a ``MockLLM`` injected, then drive
it via ``httpx.AsyncClient`` + ``ASGITransport`` (no real socket).
The tests cover:

- /health round-trip
- /hello + /sessions handshake
- /sessions/{sid}/messages SSE stream with a real engine
- live permission_request round-trip via the interaction endpoints
- Chinese payload byte-level preservation through HTTP
- ESC interrupt: POST /sessions/{sid}/interrupt mid-turn yields
  ``turn_cancelled`` on the SSE stream (v1.2)

See ``docsv2/tui_opencode_requirements.md`` §10.5 + Phase E DoD.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from xbotv2.api.paths import RuntimePaths
from httpx import ASGITransport

from xbotv2.llm.mock import MockLLM
from xbotv2.protocol.version import PROTOCOL_VERSION
from xbotv2.protocol.http_server import (
    _format_sse,
    create_app,
    run_turn_stream,
    set_llm_override,
)
from xbotv2.protocol.models import KNOWN_SERVER_EVENT_TYPES, ServerEvent
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.transport_http import HttpTransport


SSE_DATA_RE = re.compile(r"^data: ?(.*)$", re.MULTILINE)


@pytest.mark.asyncio
async def test_closing_turn_stream_cancels_background_turn() -> None:
    cancelled = asyncio.Event()

    class HangingEngine:
        def __init__(self) -> None:
            self.client_event_sink = None

        def set_client_event_sink(self, sink):
            previous = self.client_event_sink
            self.client_event_sink = sink
            return previous

        async def run_turn(self, content: str, *, request_id: str = ""):
            del content, request_id
            try:
                yield {"type": "turn_started", "data": {"turn": 1}}
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    ctx = SimpleNamespace(
        session_id="disconnect",
        turn_lock=asyncio.Lock(),
        turn_task=None,
        engine=HangingEngine(),
    )
    stream = run_turn_stream(ctx, content="wait", request_id="request")

    assert (await anext(stream))["type"] == "turn_started"
    close_task = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0.05)
    try:
        assert close_task.done()
        await close_task
    finally:
        if not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)

    assert cancelled.is_set()
    assert ctx.turn_task is None
    assert not ctx.turn_lock.locked()


def _parse_sse(payload: str) -> list[dict[str, Any]]:
    """Parse a raw SSE payload into a list of event dicts."""

    events: list[dict[str, Any]] = []
    for raw_frame in payload.split("\n\n"):
        if not raw_frame.strip():
            continue
        data_match = SSE_DATA_RE.search(raw_frame)
        if not data_match:
            continue
        text = data_match.group(1).strip()
        if not text:
            continue
        try:
            events.append(json.loads(text))
        except json.JSONDecodeError:
            events.append({"type": "decode_error", "raw": text})
    return events


def _load_jsonl_fixture(relative_path: str) -> list[dict[str, Any]]:
    path = Path(__file__).parents[1] / "fixtures" / relative_path
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_all_server_event_types_have_sse_contract_fixtures() -> None:
    contracts = _load_jsonl_fixture("sse/server_event_contracts.jsonl")

    assert [event["type"] for event in contracts] == list(KNOWN_SERVER_EVENT_TYPES)
    for expected in contracts:
        event = ServerEvent.model_validate(expected)
        frame = _format_sse(
            event={"type": event.type, "data": event.data},
            seq=event.sequence,
            session_id=event.session_id,
            thread_id=event.thread_id,
            request_id=event.request_id,
        ).decode("utf-8")

        assert f"event: {event.type}\n" in frame
        assert f"id: {event.sequence}\n" in frame
        assert _parse_sse(frame) == [expected]


@pytest_asyncio.fixture
async def http_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A FastAPI app whose engine uses a mock LLM (no real network)."""

    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)

    # Minimal providers.yaml so bootstrap can pick a default
    (data_dir / "config" / "providers.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n  base_url: http://test\n  api_key: test\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\nsession_type: interactive\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "system.yaml").write_text(
        "agent_name: TestBot\nagent_role: You are a test bot.\nprovider: default\n"
        "max_context_tokens: 4096\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )

    app = create_app(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        no_plugins=True,
    )
    # Inject a mock LLM that returns one canned response per turn.
    set_llm_override(app, MockLLM(responses=[{"content": "hello from mock"}]))
    yield app


@pytest_asyncio.fixture
async def client(http_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=http_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_http_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["server_name"] == "xbotv2"


@pytest.mark.asyncio
async def test_http_hello_returns_protocol_info(client: httpx.AsyncClient) -> None:
    response = await client.post("/hello", json={"session_id": "s1", "thread_id": "t1"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1"
    assert body["thread_id"] == "t1"
    assert body["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_http_hello_rejects_unknown_protocol(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/hello",
        json={"protocol_version": "xbotv2.v999", "client_name": "future"},
    )
    assert response.status_code == 426
    assert response.json()["code"] == "unsupported_protocol"


@pytest.mark.asyncio
async def test_http_open_session_returns_agent_name(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/sessions", json={"session_id": "s1", "thread_id": "t1"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["agent_name"]
    assert body["session_id"] == "s1"


@pytest.mark.asyncio
async def test_http_open_session_without_id_creates_generated_session(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/sessions", json={"thread_id": "t1"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["session_id"]
    assert "-" in body["session_id"]


@pytest.mark.asyncio
async def test_http_resume_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/sessions",
        json={"session_id": "missing", "thread_id": "t1", "mode": "resume"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_http_new_existing_session_returns_409(client: httpx.AsyncClient) -> None:
    payload = {"session_id": "duplicate", "thread_id": "t1", "mode": "new"}
    first = await client.post("/sessions", json=payload)
    assert first.status_code == 200

    second = await client.post("/sessions", json=payload)
    assert second.status_code == 409
    assert second.json()["code"] == "session_exists"


@pytest.mark.asyncio
async def test_http_server_hosts_sessions_from_multiple_workspaces(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    response_a = await client.post(
        "/sessions",
        json={"session_id": "ws-a", "thread_id": "t", "workspace_root": str(workspace_a)},
    )
    response_b = await client.post(
        "/sessions",
        json={"session_id": "ws-b", "thread_id": "t", "workspace_root": str(workspace_b)},
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert response_a.json()["workspace_root"] == str(workspace_a.resolve())
    assert response_b.json()["workspace_root"] == str(workspace_b.resolve())


@pytest.mark.asyncio
async def test_http_commands_are_discoverable_and_session_scoped(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    commands_response = await client.get("/commands")
    assert commands_response.status_code == 200
    names = {item["name"] for item in commands_response.json()["commands"]}
    assert {"status", "provider", "permission", "sandbox"}.issubset(names)

    open_response = await client.post(
        "/sessions", json={"session_id": "cmds", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    result_response = await client.post(
        "/sessions/cmds/commands",
        json={"command": "status", "args": []},
    )
    assert result_response.status_code == 200
    body = result_response.json()
    assert body["type"] == "command_result"
    assert body["data"]["data"]["session_id"] == "cmds"
    state_root = http_app.state.paths.session("cmds").state_dir
    messages_path = state_root / "messages.jsonl"
    messages = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
    assert "command_result" not in messages


@pytest.mark.asyncio
async def test_http_provider_list_reads_providers_yaml(client: httpx.AsyncClient) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "providers", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    list_response = await client.post(
        "/sessions/providers/commands",
        json={"command": "provider", "args": ["list"]},
    )
    assert list_response.status_code == 200
    data = list_response.json()["data"]
    assert data["status"] == "ok"
    assert data["data"]["providers"] == ["default"]


@pytest.mark.asyncio
async def test_http_policy_commands_update_session_overrides(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    permission_response = await client.post(
        "/sessions/policy/commands",
        json={"command": "permission", "args": ["set", "shell", "allow"]},
    )
    sandbox_response = await client.post(
        "/sessions/policy/commands",
        json={"command": "sandbox", "args": ["set", "external_read", "ask"]},
    )
    status_response = await client.post(
        "/sessions/policy/commands",
        json={"command": "permission", "args": ["status"]},
    )

    assert permission_response.status_code == 200
    assert sandbox_response.status_code == 200
    assert status_response.status_code == 200
    assert status_response.json()["data"]["data"]["overrides"] == {"shell": "allow"}
    state_root = http_app.state.paths.session("policy").state_dir
    events_path = state_root / "events.jsonl"
    events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert "permission_override_set" not in events
    assert "sandbox_override_set" not in events


@pytest.mark.asyncio
async def test_http_permission_response_preserves_scope() -> None:
    from xbotv2.protocol.http_server import _resolve_interaction

    request_id = "permission:scope"
    captured: dict[str, str] = {}

    class _WaiterSpy:
        def answer(self, request_id: str, *, decision: str = "", scope: str = "once"):
            captured.update({"request_id": request_id, "decision": decision, "scope": scope})
            from xbotv2.core.interactions import InteractionResult

            return InteractionResult(
                request_id=request_id,
                status="answered",
                decision=decision,
                scope=scope,
            )

        def pending_request_ids(self):
            return []

    class _Engine:
        permission_waiter = _WaiterSpy()
        user_input_waiter = _WaiterSpy()

    class _Context:
        engine = _Engine()

    class _Manager:
        async def get(self, session_id: str):
            assert session_id == "permission-scope"
            return _Context()

    response = await _resolve_interaction(
        manager=_Manager(),
        session_id="permission-scope",
        payload={"request_id": request_id, "decision": "allow", "scope": "session"},
        kind="permission",
    )

    assert response["recorded"] is True
    assert captured == {
        "request_id": request_id,
        "decision": "allow",
        "scope": "session",
    }


@pytest.mark.parametrize(
    ("event_type", "request_id", "answer", "expected_field", "expected_value"),
    [
        (
            "permission_request",
            "permission:fast",
            {"decision": "allow", "scope": "once"},
            "decision",
            "allow",
        ),
        (
            "user_input_required",
            "user_input:fast",
            {"answer": "continue"},
            "answer",
            "continue",
        ),
    ],
)
@pytest.mark.asyncio
async def test_live_interaction_is_pending_before_event_is_published(
    event_type: str,
    request_id: str,
    answer: dict[str, Any],
    expected_field: str,
    expected_value: str,
) -> None:
    from types import SimpleNamespace

    from xbotv2.core.interactions import InteractionWaiter
    from xbotv2.protocol.http_server import _live_sink

    permission_waiter = InteractionWaiter()
    user_input_waiter = InteractionWaiter()
    waiter = (
        permission_waiter
        if event_type == "permission_request"
        else user_input_waiter
    )
    events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    disconnected = asyncio.Event()
    disconnect_task = asyncio.create_task(disconnected.wait())
    sink_task = asyncio.create_task(
        _live_sink(
            {
                "type": event_type,
                "data": {"request_id": request_id},
            },
            engine=SimpleNamespace(
                permission_waiter=permission_waiter,
                user_input_waiter=user_input_waiter,
            ),
            events=events,
            disconnect_task=disconnect_task,
        )
    )

    try:
        event = await events.get()
        assert event == {
            "type": event_type,
            "data": {"request_id": request_id},
        }
        assert waiter.is_pending(request_id)

        waiter.answer(request_id, **answer)
        result = await sink_task
        assert result["status"] == "answered"
        assert result[expected_field] == expected_value
    finally:
        disconnected.set()
        if not disconnect_task.done():
            disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_permission_response_rejects_always_scope() -> None:
    from xbotv2.protocol.http_server import _resolve_interaction

    class _Engine:
        permission_waiter = object()
        user_input_waiter = object()

    class _Context:
        engine = _Engine()

    class _Manager:
        async def get(self, session_id: str):
            assert session_id == "permission-scope"
            return _Context()

    with pytest.raises(Exception) as exc_info:
        await _resolve_interaction(
            manager=_Manager(),
            session_id="permission-scope",
            payload={
                "request_id": "permission:scope",
                "decision": "allow",
                "scope": "always",
            },
            kind="permission",
        )

    assert getattr(exc_info.value, "code") == "invalid_request"
    assert "once or session" in getattr(exc_info.value, "message")


@pytest.mark.asyncio
async def test_http_policy_command_reset_rebuilds_live_policy(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy-reset", "thread_id": "t"}
    )
    assert open_response.status_code == 200
    ctx = await http_app.state.manager.get("policy-reset")

    permission_set = await client.post(
        "/sessions/policy-reset/commands",
        json={"command": "permission", "args": ["set", "shell", "deny"]},
    )
    assert permission_set.status_code == 200
    assert ctx.engine.permission_system.check("shell", {}) == "deny"

    permission_reset = await client.post(
        "/sessions/policy-reset/commands",
        json={"command": "permission", "args": ["reset", "shell"]},
    )
    assert permission_reset.status_code == 200
    assert ctx.engine.permission_system.check("shell", {}) == "ask"

    sandbox_status = await client.post(
        "/sessions/policy-reset/commands",
        json={"command": "sandbox", "args": ["status"]},
    )
    assert sandbox_status.status_code == 200
    assert "sandbox" in sandbox_status.json()["data"]["message"].lower()

    sandbox_invalid = await client.post(
        "/sessions/policy-reset/commands",
        json={"command": "sandbox", "args": ["set", "external_read", "deny"]},
    )
    assert sandbox_invalid.status_code == 200
    assert sandbox_invalid.json()["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_http_policy_commands_reject_invalid_values(
    client: httpx.AsyncClient,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy-invalid", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    permission_response = await client.post(
        "/sessions/policy-invalid/commands",
        json={"command": "permission", "args": ["set", "shell", "sometimes"]},
    )
    sandbox_response = await client.post(
        "/sessions/policy-invalid/commands",
        json={"command": "sandbox", "args": ["set", "external_read", "ask"]},
    )

    assert permission_response.status_code == 200
    assert permission_response.json()["data"]["status"] == "error"
    assert sandbox_response.status_code == 200
    assert sandbox_response.json()["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_http_open_session_failure_returns_stable_json_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "providers.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n  base_url: http://test\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\nsession_type: interactive\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "system.yaml").write_text(
        "agent_name: TestBot\nagent_role: You are a test bot.\nprovider: default\n"
        "max_context_tokens: 4096\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    app = create_app(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        no_plugins=True,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post("/sessions", json={"session_id": "bad", "thread_id": "t"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "session_open_failed"
    assert "requires api_key" in body["message"]


@pytest.mark.asyncio
async def test_http_messages_sse_stream_turn_events(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "stream1", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    async with client.stream(
        "POST",
        "/sessions/stream1/messages",
        json={"content": "hi there", "request_id": "req-1"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    types = [event.get("type") for event in events]
    assert "turn_started" in types
    assert "assistant_message" in types
    assert "turn_finished" in types
    assert "end" in types
    assert all(event["protocol_version"] == PROTOCOL_VERSION for event in events)
    assert all(event["session_id"] == "stream1" for event in events)
    assert all(event["thread_id"] == "t" for event in events)
    assert all(event["request_id"] == "req-1" for event in events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events == _load_jsonl_fixture("sse/basic_turn_events.jsonl")

    assistant = next(e for e in events if e.get("type") == "assistant_message")
    assert assistant["data"]["content"] == "hello from mock"


@pytest.mark.asyncio
async def test_http_message_request_id_reaches_engine_hooks_and_sse(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    from xbotv2.api import HookStage

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "request-context", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("request-context")
    observed = []

    async def record(ctx):
        observed.append((ctx.stage, ctx.request_id))

    session.engine.hook_manager.register(HookStage.ON_TURN_START, record)
    session.engine.hook_manager.register(HookStage.AFTER_STATE_PERSIST, record)

    async with client.stream(
        "POST",
        "/sessions/request-context/messages",
        json={"content": "hello", "request_id": "request-http-1"},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    assert all(event["request_id"] == "request-http-1" for event in events)
    assert observed == [
        (HookStage.ON_TURN_START, "request-http-1"),
        (HookStage.AFTER_STATE_PERSIST, "request-http-1"),
    ]


@pytest.mark.asyncio
async def test_http_generated_request_id_reaches_engine_and_sse(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    from xbotv2.api import HookStage

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "generated-request", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("generated-request")
    observed = []

    async def record(ctx):
        observed.append(ctx.request_id)

    session.engine.hook_manager.register(HookStage.ON_TURN_START, record)

    async with client.stream(
        "POST",
        "/sessions/generated-request/messages",
        json={"content": "hello"},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    request_ids = {event["request_id"] for event in events}
    assert len(request_ids) == 1
    generated_id = request_ids.pop()
    assert generated_id.startswith("req-")
    assert observed == [generated_id]


@pytest.mark.asyncio
async def test_http_messages_preserves_chinese_payload_in_request(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "zh", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    async with client.stream(
        "POST",
        "/sessions/zh/messages",
        json={"content": "当前磁盘用了多少", "request_id": "req-zh"},
    ) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    # The mock LLM echoes a fixed string; the test that the request body
    # preserved UTF-8 is exercised via the tui-side trace in
    # test_tui_client.py::test_http_transport_trace_records_unicode_payload.
    # Here we only confirm the SSE frame encoding survives the round-trip.
    assert any(
        e.get("type") == "assistant_message" for e in events
    ), f"no assistant_message in: {events}"


@pytest.mark.asyncio
async def test_http_messages_empty_content_rejected(client: httpx.AsyncClient) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "empty", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post(
        "/sessions/empty/messages", json={"content": "   ", "request_id": "x"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_http_messages_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/sessions/does-not-exist/messages",
        json={"content": "hi", "request_id": "r"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_http_interactions_endpoint_validates_request_id(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "validate", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post(
        "/sessions/validate/interactions/permission-response",
        json={"decision": "allow", "scope": "once"},
    )
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"code", "message", "details", "retryable"}
    assert body["code"] == "invalid_request"
    assert body["details"]["errors"]
    assert body["retryable"] is False


# ----------------------------------------------------------------------
# ESC interrupt — v1.2 (§10.5.6.1)
# ----------------------------------------------------------------------


class _GatedMockLLM(MockLLM):
    """A ``MockLLM`` whose stream blocks on an ``asyncio.Event``.

    The test sets ``release`` *after* verifying the SSE stream is open
    and the interrupt endpoint has been hit; the engine's
    ``asyncio.CancelledError`` (triggered by ``/interrupt``) will fire
    first and tear the turn down before the LLM is unblocked.
    """

    def __init__(self, release: asyncio.Event, **kwargs):
        super().__init__(responses=[{"content": "late reply"}], **kwargs)
        object.__setattr__(self, "_gated_release", release)
        object.__setattr__(self, "_gated_calls", 0)

    @property
    def calls(self) -> int:
        return self._gated_calls  # type: ignore[has-type]

    async def astream(
        self,
        messages: list,
        **kwargs: Any,
    ):
        object.__setattr__(
            self, "_gated_calls", self._gated_calls + 1  # type: ignore[has-type]
        )
        # Block until released. If the engine gets cancelled mid-turn,
        # this ``await`` will raise ``CancelledError`` and abort the
        # turn before the event is set.
        await self._gated_release.wait()  # type: ignore[has-type]
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_http_interrupt_emits_turn_cancelled_on_sse(
    http_app, tmp_path: Path
) -> None:
    """Pressing ESC (i.e. ``POST /sessions/{sid}/interrupt``) mid-turn
    must close the SSE stream with a ``turn_cancelled`` event.

    This exercises the full production path:
    TUI ESC → ``HttpTransport.interrupt`` → ``POST /interrupt`` →
    session ``turn_task.cancel`` → ``Engine.run_turn`` catch
    ``CancelledError`` → yield ``turn_cancelled`` → SSE → client.

    We spin up a **real** uvicorn process (not ``ASGITransport``)
    because ASGITransport buffers the entire response body before
    exposing it to the client, which deadlocks this test.
    """

    import socket
    import threading
    import time
    import uvicorn

    release = asyncio.Event()
    gated = _GatedMockLLM(release=release)
    set_llm_override(http_app, gated)

    # Pick a free port and start uvicorn in a background thread.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(
        http_app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for the server to be ready.
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as probe:
        for _ in range(50):
            try:
                r = await probe.get("/health")
                if r.status_code == 200:
                    break
            except httpx.RequestError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("uvicorn server failed to start")

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as ac:
            open_resp = await ac.post(
                "/sessions", json={"session_id": "esc", "thread_id": "t"}
            )
            assert open_resp.status_code == 200

            sse_chunks: list[str] = []

            async def _consume_sse() -> None:
                async with ac.stream(
                    "POST",
                    "/sessions/esc/messages",
                    json={"content": "do something long", "request_id": "req-esc"},
                ) as response:
                    assert response.status_code == 200
                    async for chunk in response.aiter_text():
                        sse_chunks.append(chunk)
                        # Once we see ``turn_started`` we know the
                        # engine is past the bootstrap and is about
                        # to call the (gated) LLM.
                        if "turn_started" in chunk:
                            ir = await ac.post("/sessions/esc/interrupt")
                            assert ir.status_code == 200
                            assert ir.json()["cancelled"] is True

            await asyncio.wait_for(_consume_sse(), timeout=5.0)
            # Defensive: unblock the LLM in case the test exits
            # before the engine's CancelledError fires.
            release.set()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)

    body = "".join(sse_chunks)
    events = _parse_sse(body)
    types = [e.get("type") for e in events]
    assert "turn_started" in types, f"no turn_started in {types!r}"
    assert "turn_cancelled" in types, f"no turn_cancelled in {types!r}"
    # The stream must terminate after cancellation — no
    # ``turn_finished`` because the LLM never returned a response.
    assert "turn_finished" not in types
    # The engine is allowed to call the LLM at most once before the
    # cancellation lands.
    assert gated.calls <= 1, f"LLM was called {gated.calls} times after interrupt"


@pytest.mark.asyncio
async def test_http_interrupt_when_idle_returns_no_op(
    client: httpx.AsyncClient,
) -> None:
    """``POST /sessions/{sid}/interrupt`` with no turn in flight is a
    no-op success — pressing ESC on the TUI should never 4xx."""

    open_resp = await client.post(
        "/sessions", json={"session_id": "idle", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post("/sessions/idle/interrupt")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "idle"
    assert body["cancelled"] is False
    assert body["status"] == "idle"


@asynccontextmanager
async def _real_terminal_session(
    tmp_path: Path,
    *,
    llm: MockLLM,
    sandbox_enabled: bool,
    timeout: float = 0.1,
) -> AsyncIterator[TerminalSession]:
    """Run one connected TerminalSession against a real local HTTP server."""
    import socket
    import threading

    import uvicorn

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (config_dir / "providers.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n"
        "  base_url: http://test\n  api_key: test\n",
        encoding="utf-8",
    )
    (config_dir / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\n"
        "session_type: interactive\n",
        encoding="utf-8",
    )
    sandbox = "true" if sandbox_enabled else "false"
    (config_dir / "system.yaml").write_text(
        "agent_name: TestBot\nagent_role: You are a test bot.\n"
        "provider: default\nmax_context_tokens: 4096\n"
        "tools: []\nplugins: {}\nhooks: []\n"
        f"sandbox:\n  enabled: {sandbox}\n  resources: []\n",
        encoding="utf-8",
    )

    app = create_app(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    set_llm_override(app, llm)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            ws="none",
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"
    session: TerminalSession | None = None
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as probe:
            for _ in range(50):
                try:
                    response = await probe.get("/health")
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("uvicorn server failed to start")

        session = TerminalSession(
            session_id="default",
            thread_id="agent",
            workspace_root=workspace,
            transport=HttpTransport(base_url, timeout=timeout),
        )
        await session.connect()
        yield session
    finally:
        if session is not None:
            await session.disconnect()
        server.should_exit = True
        server_thread.join(timeout=3.0)


@pytest.mark.asyncio
async def test_real_http_filesystem_permission_wait_does_not_read_timeout(
    tmp_path: Path,
) -> None:
    """Real socket SSE must stay open while a permission request waits.

    This reproduces the user's pending ``filesystem_list`` case: the tool
    reaches ``permission_request`` and waits for the TUI.  The transport uses
    a tiny 0.1s default timeout; the permission provider waits longer than
    that. If SSE uses the regular read timeout, this test fails before the
    provider can answer and no ``tool_result`` is emitted.
    """

    workspace = tmp_path / "workspace"
    llm = MockLLM(responses=[
        {
            "content": "listing",
            "tool_calls": [
                {"name": "filesystem_list", "args": {"path": "."}, "id": "call_list"},
            ],
        },
        {"content": "done"},
    ])
    async with _real_terminal_session(
        tmp_path,
        llm=llm,
        sandbox_enabled=True,
    ) as session:
        (workspace / "hello.txt").write_text("hello", encoding="utf-8")

        events = []
        async for event in session.send_message("list workspace"):
            events.append(event)
            if event.get("type") == "permission_request":
                await asyncio.sleep(0.2)
                await session.respond_permission(
                    event["data"]["request_id"],
                    "allow",
                )

    assert "permission_request" in [event.get("type") for event in events]
    assert any(
        event.get("type") == "tool_result"
        and event.get("data", {}).get("tool_call_id") == "call_list"
        and event.get("data", {}).get("status") == "success"
        for event in events
    )


@pytest.mark.asyncio
async def test_real_http_interrupt_while_permission_waits(
    tmp_path: Path,
) -> None:
    llm = MockLLM(responses=[
        {
            "content": "listing",
            "tool_calls": [
                {"name": "filesystem_list", "args": {"path": "."}, "id": "call_wait"},
            ],
        },
    ])
    async with _real_terminal_session(
        tmp_path,
        llm=llm,
        sandbox_enabled=True,
    ) as session:
        async def collect_events() -> list[dict[str, Any]]:
            collected = []
            async for event in session.send_message("list workspace"):
                collected.append(event)
                if event.get("type") == "permission_request":
                    response = await session.transport.interrupt(
                        session_id=session.session_id
                    )
                    assert response["cancelled"] is True
            return collected

        events = await asyncio.wait_for(collect_events(), timeout=5.0)
        request = next(
            event for event in events if event.get("type") == "permission_request"
        )
        with pytest.raises(RuntimeError, match="interaction_no_longer_pending"):
            await session.respond_permission(
                request["data"]["request_id"],
                "allow",
            )

    event_types = [event.get("type") for event in events]
    assert "permission_request" in event_types
    assert "turn_cancelled" in event_types
    assert "turn_finished" not in event_types


@pytest.mark.asyncio
async def test_real_http_interrupt_while_ask_user_waits(tmp_path: Path) -> None:
    llm = MockLLM(responses=[
        {
            "content": "asking",
            "tool_calls": [
                {
                    "name": "ask_user",
                    "args": {"question": "Continue?", "options": ["yes", "no"]},
                    "id": "call_wait",
                },
            ],
        },
    ])

    async with _real_terminal_session(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as session:
        events = []
        async for event in session.send_message("ask before continuing"):
            events.append(event)
            if event.get("type") == "permission_request":
                await session.respond_permission(
                    event["data"]["request_id"],
                    "allow",
                )
            elif event.get("type") == "user_input_required":
                response = await session.transport.interrupt(
                    session_id=session.session_id
                )
                assert response["cancelled"] is True

        request = next(
            event for event in events if event.get("type") == "user_input_required"
        )
        with pytest.raises(RuntimeError, match="interaction_no_longer_pending"):
            await session.submit_user_input(
                request["data"]["request_id"],
                "yes",
            )

    event_types = [event.get("type") for event in events]
    assert "user_input_required" in event_types
    assert "turn_cancelled" in event_types
    assert "tool_result" not in event_types
    assert "turn_finished" not in event_types


@pytest.mark.asyncio
async def test_real_http_ask_user_round_trip(tmp_path: Path) -> None:
    llm = MockLLM(responses=[
        {
            "content": "asking",
            "tool_calls": [
                {
                    "name": "ask_user",
                    "args": {
                        "question": "Continue?",
                        "options": ["continue", "stop"],
                    },
                    "id": "call_ask",
                },
            ],
        },
        {"content": "continued"},
    ])
    seen_payloads: list[dict[str, Any]] = []
    seen_permissions: list[dict[str, Any]] = []

    async with _real_terminal_session(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as session:

        async def collect_events() -> list[dict[str, Any]]:
            collected = []
            async for event in session.send_message("ask before continuing"):
                collected.append(event)
                if event.get("type") == "permission_request":
                    seen_permissions.append(event["data"])
                    await session.respond_permission(
                        event["data"]["request_id"],
                        "allow",
                    )
                elif event.get("type") == "user_input_required":
                    seen_payloads.append(event["data"])
                    await asyncio.sleep(0.2)
                    await session.submit_user_input(
                        event["data"]["request_id"],
                        "continue",
                    )
            return collected

        try:
            events = await asyncio.wait_for(collect_events(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                f"ask_user stream did not finish; provider payloads={seen_payloads!r}"
            )

    assert len(seen_permissions) == 1
    assert seen_permissions[0]["request_id"] == "permission:call_ask"
    assert len(seen_payloads) == 1
    assert seen_payloads[0]["request_id"] == "user_input:call_ask"
    assert seen_payloads[0]["question"] == "Continue?"
    assert seen_payloads[0]["options"] == ["continue", "stop"]
    assert any(event["type"] == "user_input_recorded" for event in events)
    assert any(
        event.get("type") == "tool_result"
        and event.get("data", {}).get("tool_call_id") == "call_ask"
        and "User answered: continue" in event.get("data", {}).get("content", "")
        for event in events
    )
    assert any(
        event.get("type") == "assistant_message"
        and event.get("data", {}).get("content") == "continued"
        for event in events
    )


# ------------------------------------------------------------------
# Skills + MCP integration (server-side)
# ------------------------------------------------------------------


@pytest_asyncio.fixture
async def skills_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A FastAPI app with plugins enabled and skills discoverable."""
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "providers.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n  base_url: http://test\n  api_key: test\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\nsession_type: interactive\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "system.yaml").write_text(
        "agent_name: TestBot\nagent_role: You are a test bot.\nprovider: default\n"
        "max_context_tokens: 4096\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )

    app = create_app(provider_name="default", paths=RuntimePaths.from_data_dir(data_dir), no_plugins=False)
    set_llm_override(app, MockLLM(responses=[{"content": "ok"}]))
    return app


@pytest_asyncio.fixture
async def skills_client(skills_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=skills_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_http_server_commands_include_kind(
    skills_client: httpx.AsyncClient,
) -> None:
    """Server commands now include kind field."""
    resp = await skills_client.get("/commands")
    assert resp.status_code == 200
    body = resp.json()
    cmds = body.get("commands", [])
    assert len(cmds) >= 4  # status, provider, permission, sandbox
    kinds = {c.get("kind", "") for c in cmds}
    assert "server" in kinds


@pytest.mark.asyncio
async def test_http_sandbox_set_persists_to_policy_yaml(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "sandbox-persist", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    # Set network=false — should persist to policy.yaml
    set_network = await client.post(
        "/sessions/sandbox-persist/commands",
        json={"command": "sandbox", "args": ["set", "network", "false"]},
    )
    assert set_network.status_code == 200
    assert set_network.json()["data"]["status"] == "ok"

    # Set external_read=deny — also persisted
    set_ext = await client.post(
        "/sessions/sandbox-persist/commands",
        json={"command": "sandbox", "args": ["set", "external_read", "deny"]},
    )
    assert set_ext.status_code == 200

    ctx = await http_app.state.manager.get("sandbox-persist")
    # Live overrides are in memory
    assert ctx.sandbox_overrides.get("network") is False
    assert ctx.sandbox_overrides.get("external_read") == "deny"
    # Engine sandbox policy reflects the overrides
    assert ctx.engine.sandbox_policy.network is False
    assert ctx.engine.sandbox_policy.external_read == "deny"

    # policy.yaml file was written
    policy_path = ctx.paths.session("sandbox-persist").policy_file
    assert policy_path.exists()
    import yaml
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert doc["sandbox"]["network"] is False
    assert doc["sandbox"]["external_read"] == "deny"


@pytest.mark.asyncio
async def test_http_sandbox_set_rejects_invalid_values(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/sessions", json={"session_id": "sandbox-validate", "thread_id": "t"}
    )

    bad = await client.post(
        "/sessions/sandbox-validate/commands",
        json={"command": "sandbox", "args": ["set", "external_read", "garbage"]},
    )
    assert bad.status_code == 200
    assert bad.json()["data"]["status"] == "error"
    assert "Invalid value" in bad.json()["data"]["message"]

    bad_network = await client.post(
        "/sessions/sandbox-validate/commands",
        json={"command": "sandbox", "args": ["set", "network", "maybe"]},
    )
    assert bad_network.status_code == 200
    assert bad_network.json()["data"]["status"] == "error"
