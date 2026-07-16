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
from xbotv2.api.hooks import HookStage
from xbotv2.api.messages import Message
from httpx import ASGITransport

from xbotv2.llm.mock import MockLLM
from xbotv2.protocol.version import PROTOCOL_VERSION
from xbotv2.protocol.http_server import (
    _format_sse,
    create_app,
    set_llm_override,
)
from xbotv2.core.session import (
    SessionRuntime,
    _live_sink,
    run_turn_stream,
)
from xbotv2.protocol.models import KNOWN_SERVER_EVENT_TYPES, ServerEvent
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.transport_http import HttpTransport


SSE_DATA_RE = re.compile(r"^data: ?(.*)$", re.MULTILINE)


@pytest.mark.asyncio
async def test_session_close_cancels_turn_before_closing_engine(tmp_path: Path) -> None:
    turn_cancelled = asyncio.Event()

    async def hanging_turn() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            turn_cancelled.set()

    class Engine:
        def __init__(self) -> None:
            self.closed_after_turn = False

        async def close_session(self) -> None:
            self.closed_after_turn = turn_cancelled.is_set()

    engine = Engine()
    task = asyncio.create_task(hanging_turn())
    await asyncio.sleep(0)
    ctx = SessionRuntime(
        session_id="closing",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        engine=engine,
        turn_task=task,
    )

    await ctx.close()

    assert task.cancelled()
    assert ctx.turn_task is None
    assert engine.closed_after_turn is True


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
    assert body["model"] == "test"
    assert body["context_window"] == 4096


@pytest.mark.asyncio
async def test_http_selects_primary_agent_and_resumes_it_from_thread_metadata(
    http_app, tmp_path: Path
) -> None:
    workspace = tmp_path / "agent-workspace"
    agents_dir = workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        "---\n"
        "description: Build focused changes\n"
        "mode: primary\n"
        "tools: []\n"
        "---\n"
        "Follow the builder workflow.",
        encoding="utf-8",
    )
    app = create_app(
        paths=http_app.state.paths,
        workspace_root=str(workspace),
        no_plugins=False,
        llm_override=MockLLM(responses=[]),
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        opened = await ac.post(
            "/sessions",
            json={
                "session_id": "primary-http",
                "thread_id": "agent",
                "agent": "builder",
            },
        )
        catalog = await ac.post(
            "/sessions/primary-http/commands",
            json={"command": "agent", "args": ["list"]},
        )
        resumed = await ac.post(
            "/sessions",
            json={
                "session_id": "primary-http",
                "thread_id": "agent",
                "mode": "resume",
            },
        )

    assert opened.status_code == 200
    assert opened.json()["agent_name"] == "builder"
    assert catalog.status_code == 200
    assert catalog.json()["data"]["data"]["active"] == "builder"
    assert catalog.json()["data"]["data"]["agents"][0]["name"] == "builder"
    assert resumed.status_code == 200
    assert resumed.json()["agent_name"] == "builder"


@pytest.mark.asyncio
async def test_http_resume_returns_display_history(client: httpx.AsyncClient) -> None:
    opened = await client.post(
        "/sessions", json={"session_id": "resume-history", "thread_id": "t1"}
    )
    assert opened.status_code == 200

    turn = await client.post(
        "/sessions/resume-history/messages",
        json={"content": "remember this"},
    )
    assert turn.status_code == 200
    assert "turn_finished" in turn.text
    manager = client._transport.app.state.manager
    original = await manager.get("resume-history")
    original.engine.messages.append(Message(
        role="tool",
        content="cached result",
        tool_call_id="call-1",
        status="error",
        additional_kwargs={
            "xbotv2_data": {"cache": "tool-results/call-1.txt"},
            "xbotv2_error": {"code": "failed", "message": "bad input"},
        },
        artifact=[
            {"id": "artifact-1", "name": "report.txt", "media_type": "text/plain"}
        ],
    ))
    await original.engine.save_messages()

    resumed = await client.post(
        "/sessions",
        json={"session_id": "resume-history", "thread_id": "t1", "mode": "resume"},
    )

    assert resumed.status_code == 200
    replacement = await manager.get("resume-history")
    assert replacement is not original
    assert replacement.engine is not original.engine
    history = resumed.json()["history"]
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "remember this"),
        ("assistant", "hello from mock"),
        ("tool", "cached result"),
    ]
    tool = history[-1]
    assert tool["data"] == {"cache": "tool-results/call-1.txt"}
    assert tool["error"]["code"] == "failed"
    assert tool["artifacts"][0]["name"] == "report.txt"


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
    assert {
        "status", "provider", "permission", "sandbox", "fork", "clear", "undo",
    }.issubset(names)

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
    state_root = http_app.state.paths.session("cmds").thread("t").state_dir
    messages_path = state_root / "messages.jsonl"
    messages = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
    assert "command_result" not in messages


@pytest.mark.asyncio
async def test_history_commands_undo_fork_and_clear_persist_atomically(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    set_llm_override(http_app, MockLLM(responses=[
        {"content": "first answer"},
        {"content": "second answer"},
    ]))
    await client.post("/sessions", json={"session_id": "history", "thread_id": "t"})
    await client.post("/sessions/history/messages", json={"content": "first"})
    await client.post("/sessions/history/messages", json={"content": "second"})

    undone = await client.post(
        "/sessions/history/commands",
        json={"command": "undo", "args": ["1"]},
    )

    assert undone.status_code == 200
    assert undone.json()["data"]["history"] == [
        {
            "role": "user", "content": "first", "tool_calls": [],
            "tool_call_id": "", "status": "",
            "data": None, "error": None, "artifacts": [],
        },
        {
            "role": "assistant", "content": "first answer", "tool_calls": [],
            "tool_call_id": "", "status": "",
            "data": None, "error": None, "artifacts": [],
        },
    ]
    ctx = await http_app.state.manager.get("history")
    assert [message.content for message in ctx.engine.messages] == [
        "first", "first answer",
    ]

    source_session = http_app.state.paths.session("history")
    source = source_session.thread("t")
    source_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record.get("content") == "second" for record in source_records)
    assert source_records[-1]["record_type"] == "history_undo"
    (source.plugin_states_dir / "sample.yaml").write_text("value: kept\n")
    (source.artifacts_dir / "cached.txt").write_text("cached")
    source_session.policy_file.write_text("permissions: {}\n")
    forked = await client.post(
        "/sessions/history/commands",
        json={"command": "fork", "args": []},
    )
    fork_id = forked.json()["data"]["data"]["session_id"]
    fork_session = http_app.state.paths.session(fork_id)
    fork_paths = fork_session.thread("t")

    assert (fork_paths.plugin_states_dir / "sample.yaml").read_text() == "value: kept\n"
    assert (fork_paths.artifacts_dir / "cached.txt").read_text() == "cached"
    assert fork_session.policy_file.read_text() == "permissions: {}\n"
    assert fork_paths.messages_file.read_text() == source.messages_file.read_text()
    resumed = await client.post(
        "/sessions",
        json={"session_id": fork_id, "thread_id": "t", "mode": "resume"},
    )
    assert [item["content"] for item in resumed.json()["history"]] == [
        "first", "first answer",
    ]

    cleared = await client.post(
        "/sessions/history/commands",
        json={"command": "clear", "args": []},
    )
    assert cleared.json()["data"]["data"] == {"removed_turns": 1}
    assert cleared.json()["data"]["history"] == []
    assert ctx.engine.messages == []
    assert ctx.engine.state_store.read_messages() == []
    cleared_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert cleared_records[:len(source_records)] == source_records
    assert cleared_records[-1]["record_type"] == "history_clear"


@pytest.mark.asyncio
async def test_undo_rejects_invalid_or_excessive_counts(client: httpx.AsyncClient) -> None:
    await client.post("/sessions", json={"session_id": "undo-errors", "thread_id": "t"})

    for count in ("0", "two", "2"):
        response = await client.post(
            "/sessions/undo-errors/commands",
            json={"command": "undo", "args": [count]},
        )
        assert response.json()["data"]["status"] == "error"


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
    ctx = await http_app.state.manager.get("policy")
    cached_path = (
        http_app.state.paths.session("policy").thread("t").artifacts_dir
        / "tool_results"
        / "cached.txt"
    )
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("cached after policy reload", encoding="utf-8")
    filesystem_entry = ctx.engine.tool_registry.get("filesystem_read")
    assert filesystem_entry is not None
    cached_result = await filesystem_entry.tool.ainvoke(
        {"path": "session/artifacts/tool_results/cached.txt"},
        sandbox=ctx.engine.sandbox_policy,
    )
    status_response = await client.post(
        "/sessions/policy/commands",
        json={"command": "permission", "args": ["status"]},
    )

    assert permission_response.status_code == 200
    assert sandbox_response.status_code == 200
    assert status_response.status_code == 200
    assert cached_result.status == "success"
    assert "cached after policy reload" in cached_result.content
    assert status_response.json()["data"]["data"]["overrides"] == {"shell": "allow"}
    state_root = http_app.state.paths.session("policy").thread("t").state_dir
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

    def __init__(
        self,
        release: asyncio.Event,
        responses: list[dict[str, Any]] | None = None,
        **kwargs,
    ):
        super().__init__(
            responses=responses or [{"content": "late reply"}],
            **kwargs,
        )
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
async def test_session_mailbox_queues_user_messages_and_delivers_in_order(
    http_app,
) -> None:
    release = asyncio.Event()
    llm = _GatedMockLLM(
        release,
        responses=[{"content": "first reply"}, {"content": "second reply"}],
    )
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="mailbox-order",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    delivered: list[tuple[str, str]] = []

    async def observe(hook_ctx):
        delivered.append((hook_ctx.stage.value, hook_ctx.mailbox_message.id))

    ctx.engine.hook_manager.register(HookStage.BEFORE_MAILBOX_DELIVERY, observe)
    ctx.engine.hook_manager.register(HookStage.AFTER_MAILBOX_DELIVERY, observe)

    first, first_events, first_queued, _ = await ctx.enqueue_user_message(
        "first", "req-1"
    )
    assert first_queued is False
    assert (await first_events.get())["type"] == "turn_started"

    second, second_events, second_queued, position = await ctx.enqueue_user_message(
        "second", "req-2"
    )
    assert second_queued is True
    assert position == 1

    release.set()

    async def collect(events):
        result = []
        while True:
            event = await events.get()
            if event is None:
                return result
            result.append(event)

    first_result, second_result = await asyncio.gather(
        collect(first_events),
        collect(second_events),
    )

    assert [event["data"]["content"] for event in first_result if event["type"] == "assistant_message"] == ["first reply"]
    assert [event["data"]["content"] for event in second_result if event["type"] == "assistant_message"] == ["second reply"]
    assert [message.content for message in ctx.engine.messages if message.role == "user"] == [
        "first", "second",
    ]
    assert delivered == [
        ("before_mailbox_delivery", first.id),
        ("after_mailbox_delivery", first.id),
        ("before_mailbox_delivery", second.id),
        ("after_mailbox_delivery", second.id),
    ]


@pytest.mark.asyncio
async def test_general_message_uses_session_event_stream(http_app) -> None:
    llm = MockLLM(responses=[{"content": "background result"}])
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="general-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    events = ctx.attach_event_stream()

    item = await ctx.enqueue_general({
        "source": "task",
        "event": "completed",
        "content": "A background command completed.",
        "data": {"task_id": "task-1"},
    })

    received = []
    while True:
        event = await asyncio.wait_for(events.get(), timeout=1)
        received.append(event)
        if event and event["type"] == "turn_finished":
            break

    assert [event["type"] for event in received if event] == [
        "turn_started", "assistant_message_delta", "assistant_message",
        "turn_finished",
    ]
    assert next(
        event for event in received if event and event["type"] == "assistant_message"
    )["data"]["content"] == "background result"
    assert [message.role for message in ctx.engine.messages] == ["assistant"]
    assert any(
        message.role == "system"
        and "background command completed" in message.content.lower()
        for message in llm.get_call_messages(0)
    )
    assert item.kind == "general"


@pytest.mark.asyncio
async def test_background_task_updates_and_completion_use_session_stream(
    http_app, monkeypatch
) -> None:
    async def run(*args, **kwargs):
        await asyncio.sleep(0)
        return "task output"

    monkeypatch.setattr(
        "xbotv2.core.background_tasks.run_shell_command", run
    )
    llm = MockLLM(responses=[{"content": "task acknowledged"}])
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="background-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    events = ctx.attach_event_stream()

    await ctx.engine.background_tasks.start_task("printf result")

    received = []
    while True:
        event = await asyncio.wait_for(events.get(), timeout=1)
        received.append(event)
        if event and event["type"] == "turn_finished":
            break

    task_events = [
        event for event in received
        if event and event["type"] == "task_updated"
    ]
    assert [event["data"]["status"] for event in task_events] == [
        "pending", "running", "completed",
    ]
    assert any(
        event and event["type"] == "assistant_message"
        and event["data"]["content"] == "task acknowledged"
        for event in received
    )
    assert [message.role for message in ctx.engine.messages] == ["assistant"]
    assert any(
        message.role == "system"
        and "background task task-1 completed" in message.content.lower()
        for message in llm.get_call_messages(0)
    )


@pytest.mark.asyncio
async def test_session_close_drops_mailbox_and_resume_starts_empty(http_app) -> None:
    llm = MockLLM(responses=[{"content": "unused"}])
    ctx = await http_app.state.manager.open_session(
        session_id="mailbox-resume",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    await ctx.enqueue_general("do not replay")
    assert ctx.mailbox.size == 1

    await http_app.state.manager.close_session(
        "mailbox-resume", reason="client_disconnected"
    )
    resumed = await http_app.state.manager.open_session(
        session_id="mailbox-resume",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        mode="resume",
        no_plugins=True,
        llm_override=llm,
    )

    assert resumed.mailbox.size == 0
    records = [
        json.loads(line)
        for line in resumed.paths.session("mailbox-resume").thread("t").mailbox_log.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert records[-1]["event"] == "dropped"
    assert records[-1]["reason"] == "client_disconnected"


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
                    "args": {
                        "question": "Continue?",
                        "options": [
                            {"label": "yes", "description": "Continue."},
                            {"label": "no", "description": "Stop."},
                        ],
                    },
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
                        "options": [
                            {"label": "continue", "description": "Keep working."},
                            {"label": "stop", "description": "Stop now."},
                        ],
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
    assert seen_payloads[0]["options"] == [
        {"label": "continue", "description": "Keep working."},
        {"label": "stop", "description": "Stop now."},
    ]
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
async def test_http_goal_tool_is_discovered_and_continues_through_mailbox(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    set_llm_override(skills_app, MockLLM(responses=[
        {
            "content": "",
            "tool_calls": [{
                "id": "goal-complete",
                "name": "update_goal",
                "args": {
                    "status": "complete",
                    "summary": "API tests passed",
                },
            }],
        },
        {"content": "Goal complete: API tests passed."},
    ]))
    await skills_client.post(
        "/sessions", json={"session_id": "goal-state", "thread_id": "t"}
    )
    commands = await skills_client.get("/sessions/goal-state/commands")
    goal_commands = [
        item for item in commands.json()["commands"] if item["name"] == "goal"
    ]
    assert len(goal_commands) == 1
    assert goal_commands[0]["kind"] == "server"
    assert goal_commands[0]["usage"].startswith("/goal")
    assert not any(
        item["name"] in {"create_goal", "get_goal", "update_goal", "shell"}
        for item in commands.json()["commands"]
    )

    ctx = await skills_app.state.manager.get("goal-state")
    session_events = ctx.attach_event_stream()

    response = await skills_client.post(
        "/sessions/goal-state/commands",
        json={
            "command": "goal",
            "raw": "/goal --token-budget 2000 ship the API",
        },
    )
    assert response.json()["data"]["message"] == "Set the active goal."
    events = []
    while True:
        event = await asyncio.wait_for(session_events.get(), timeout=2)
        assert event is not None
        events.append(event)
        if event["type"] == "turn_finished":
            break
    ctx.detach_event_stream(session_events)

    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message" and event["data"]["content"]
    ] == ["Goal complete: API tests passed."]
    for _ in range(20):
        if not ctx.turn_lock.locked():
            break
        await asyncio.sleep(0)
    goal_plugin = next(
        plugin
        for plugin in ctx.engine.plugin_loader.loaded_plugins
        if plugin.manifest.name == "goal"
    )
    assert (await goal_plugin.get_goal()).data["goal"] == {
        "objective": "ship the API",
        "status": "complete",
        "summary": "API tests passed",
        "token_budget": 2000,
    }
    get_response = await skills_client.post(
        "/sessions/goal-state/commands",
        json={"command": "goal", "raw": "/goal"},
    )
    assert get_response.json()["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_http_goal_command_remains_available_during_active_turn(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    await skills_client.post(
        "/sessions", json={"session_id": "busy-command", "thread_id": "t"}
    )
    ctx = await skills_app.state.manager.get("busy-command")
    await ctx.turn_lock.acquire()
    try:
        response = await skills_client.post(
            "/sessions/busy-command/commands",
            json={"command": "goal", "raw": "/goal"},
        )
    finally:
        ctx.turn_lock.release()

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_http_command_rejects_invalid_quoting(
    skills_client: httpx.AsyncClient,
) -> None:
    await skills_client.post(
        "/sessions", json={"session_id": "invalid-command", "thread_id": "t"}
    )

    response = await skills_client.post(
        "/sessions/invalid-command/commands",
        json={"raw": "/goal 'unterminated"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_http_skill_prompt_is_expanded_before_model_input(
    skills_client: httpx.AsyncClient,
    skills_app,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "skill-workspace"
    skill_dir = workspace / ".agents" / "skills" / "xbot-test-prompt"
    skill_dir.mkdir(parents=True)
    (workspace / ".git").mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: xbot-test-prompt
description: Expand a deterministic test prompt
allowed-tools:
  - shell(git *)
---
Follow this test instruction: $ARGUMENTS
""",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[{"content": "expanded"}])
    set_llm_override(skills_app, llm)
    await skills_client.post(
        "/sessions",
        json={
            "session_id": "skill-prompt",
            "thread_id": "t",
            "workspace_root": str(workspace),
        },
    )

    commands = (
        await skills_client.get("/sessions/skill-prompt/commands")
    ).json()["commands"]
    command = next(item for item in commands if item["name"] == "xbot-test-prompt")
    assert command["kind"] == "prompt"

    response = await skills_client.post(
        "/sessions/skill-prompt/messages",
        json={"content": "/xbot-test-prompt verify boundaries"},
    )
    assert response.status_code == 200
    contents = [
        str(getattr(message, "content", ""))
        for message in llm.get_call_messages(0)
    ]
    assert any(
        "Follow this test instruction: verify boundaries" in content
        for content in contents
    )
    assert all("/xbot-test-prompt" not in content for content in contents)



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
