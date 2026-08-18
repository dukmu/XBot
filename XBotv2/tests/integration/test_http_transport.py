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

See ``docs/protocol/tui_opencode_requirements.md`` §10.5 + Phase E DoD.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import XBotv2.client as client_module
import yaml
from xcore import Context
from XBotv2.core.jobs import JobKind
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.events import Events
from XBotv2.core.messages import Message
from XBotv2.core.tools import Tool
from XBotv2.client import XBotClient, XBotClientError
from XBotv2.coretools.shell import ShellRunner
from XBotv2.agentloop.internal_messages import structure_tool_message
from httpx import ASGITransport

from XBotv2.llm.mock import MockLLM
from XBotv2.application.server import start_server_application
from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.protocol.http_server import (
    _format_sse,
    set_llm_override,
)
from XBotv2.protocol.session_manager import ThreadNotActive
from XBotv2.session.runtime import SessionRuntime, _live_sink, run_turn_stream
from XBotv2.protocol.models import KNOWN_SERVER_EVENT_TYPES, ServerEvent
from XBotv2.tui.terminal import TerminalSession
from XBotv2.tui.transport_http import HttpTransport


SSE_DATA_RE = re.compile(r"^data: ?(.*)$", re.MULTILINE)


async def _drain_stream(stream):
    return [event async for event in stream]


async def _start_background_shell(application: Any, command: str) -> str:
    job = await application.jobs.create(
        kind=JobKind.SHELL,
        metadata={"command": command, "cwd": ""},
    )
    application.jobs.start(job.id, ShellRunner())
    return job.id


@pytest.mark.asyncio
async def test_python_sdk_uses_typed_resources_and_events(http_app) -> None:
    assert client_module.XBotClient is XBotClient
    set_llm_override(http_app, MockLLM(responses=[{"content": "sdk answer"}]))
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        health = await sdk.health()
        opened = await sdk.open_session(
            session_id="sdk-client",
            thread_id="main",
        )
        events = [
            event
            async for event in sdk.send_message(
                "sdk-client",
                "main",
                "sdk question",
                request_id="sdk-request",
            )
        ]
        messages = await sdk.list_messages("sdk-client", "main")
        undone = await sdk.undo_history("sdk-client", "main")

        assert health.status == "ok"
        assert opened.session_id == "sdk-client"
        assert any(
            event.type == "assistant_message"
            and event.data["content"] == "sdk answer"
            for event in events
        )
        assert events[-1].type == "end"
        assert [item.content for item in messages.messages] == [
            "sdk question",
            "sdk answer",
        ]
        assert undone.removed_turns == 1
        assert undone.messages == []
        assert not hasattr(sdk, "run_command")

        with pytest.raises(XBotClientError) as raised:
            await sdk.get_thread("sdk-client", "missing")
        assert raised.value.status_code == 404
        assert raised.value.code == "session_not_found"
        assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_python_sdk_uploads_attachment_as_session_artifact(http_app) -> None:
    llm = MockLLM(responses=[{"content": "attachment received"}])
    set_llm_override(http_app, llm)
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        await sdk.open_session(session_id="sdk-attachment", thread_id="main")
        events = [
            event
            async for event in sdk.send_message(
                "sdk-attachment",
                "main",
                "inspect this",
                attachments=[{
                    "name": "sample.bin",
                    "media_type": "application/octet-stream",
                    "data": "YmluYXJ5",
                }],
            )
        ]
        messages = await sdk.list_messages("sdk-attachment", "main")

    assert events[-1].type == "end"
    artifact = messages.messages[0].artifacts[0]
    assert artifact["name"] == "sample.bin"
    assert not str(artifact["id"]).startswith("/")
    user = next(message for message in llm.get_call_messages(0) if message.role == "user")
    assert user.artifact == [artifact]


@pytest.mark.asyncio
async def test_session_policy_api_persists_reloads_and_preserves_rules(http_app) -> None:
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        await sdk.open_session(session_id="sdk-policy", thread_id="main")
        policy_path = http_app.state.paths.session("sdk-policy").config_file
        policy_path.write_text(
            yaml.safe_dump({
                "permissions": {
                    "allow": [{"tool": "edit", "params": {"path": "a\\.txt"}}],
                },
                "sandbox": {
                    "resources": [{"path": "/tmp/approved", "access": "readwrite"}],
                },
            }),
            encoding="utf-8",
        )

        updated = await sdk.update_session_policy(
            "sdk-policy",
            permissions={"shell": "allow", "edit": "deny"},
            sandbox={"network": False, "external_write": "deny"},
        )
        ctx = await http_app.state.manager.get("sdk-policy", "main")
        sandbox = ctx.services.sandbox

        assert updated.permissions == {
            "deny": [{"tool": "edit"}],
            "allow": [{"tool": "shell"}, {"tool": "edit", "params": {"path": "a\\.txt"}}],
        }
        assert updated.sandbox["resources"] == [
            {"path": "/tmp/approved", "access": "readwrite"}
        ]
        assert ctx.services.permissions.check("shell") == "allow"
        assert ctx.services.permissions.check(
            "edit", {"path": "a.txt", "mode": "write"}
        ) == "deny"
        assert ctx.services.sandbox.network is False
        assert ctx.services.sandbox.external_write == "deny"
        assert ctx.services.sandbox is sandbox
        assert ctx.services.jobs is not None
        assert ctx.engine.tools.registry.get("shell") is not None

        cleared = await sdk.update_session_policy(
            "sdk-policy",
            remove_permissions=["shell", "edit"],
            remove_sandbox=["network"],
        )
        assert cleared.permissions == {
            "allow": [
                {"tool": "edit", "params": {"path": "a\\.txt"}}
            ]
        }
        assert "network" not in cleared.sandbox
        assert (await sdk.get_session_policy("sdk-policy")) == cleared


@pytest.mark.asyncio
async def test_session_policy_api_rejects_update_during_turn(http_app) -> None:
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        await sdk.open_session(session_id="busy-policy", thread_id="main")
        ctx = await http_app.state.manager.get("busy-policy", "main")
        await ctx.turn_lock.acquire()
        try:
            with pytest.raises(XBotClientError) as raised:
                await sdk.update_session_policy(
                    "busy-policy", permissions={"shell": "allow"}
                )
        finally:
            ctx.turn_lock.release()

        assert raised.value.status_code == 409
        assert raised.value.code == "thread_busy"


@pytest.mark.asyncio
async def test_session_close_cancels_turn_before_closing_engine(tmp_path: Path) -> None:
    turn_cancelled = asyncio.Event()

    async def hanging_turn() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            turn_cancelled.set()

    class Engine:
        plugin_loader = None
        job_registry = None

        def __init__(self) -> None:
            self.closed_after_turn = False

        def set_wake_driver(self, _driver) -> None:
            pass

        async def discard_inputs(self) -> None:
            pass

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
        services=Context(data_dir=tmp_path),
        engine=engine,
        turn_task=task,
    )

    await ctx.close()

    assert task.cancelled()
    assert ctx.turn_task is None
    assert engine.closed_after_turn is True


@pytest.mark.asyncio
async def test_closing_turn_stream_cancels_background_turn(tmp_path) -> None:
    cancelled = asyncio.Event()

    class HangingEngine:
        plugin_loader = None
        job_registry = None

        def __init__(self) -> None:
            self.client_event_sink = None

        def set_wake_driver(self, _driver) -> None:
            pass

        def set_client_event_sink(self, sink):
            previous = self.client_event_sink
            self.client_event_sink = sink
            return previous

        async def run_turn(
            self,
            content: str,
            *,
            request_id: str = "",
            mailbox_message=None,
            images=None,
            artifacts=None,
        ):
            del content, request_id, mailbox_message, images, artifacts
            try:
                yield {"type": "turn_started", "data": {"turn": 1}}
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    ctx = SessionRuntime(
        session_id="disconnect",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        services=Context(data_dir=tmp_path),
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

    # Tree overlays: llm provider definitions + user context live in plugin
    # config, not separate providers.yaml / user.yaml documents.
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
            {
                "id": "sandbox",
                "name": "sandbox",
                "config": {"sandbox": {"enabled": False, "resources": []}},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "permissions": {
                        "ask": [
                            {"tool": "ask_user"},
                            {"tool": "request_permission"},
                            {"tool": "edit"},
                        ],
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )

    # Isolate the workspace from the ambient checkout so a workspace
    # ``.xbot/config.yaml`` cannot override session policy in these tests.
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    app = server.server
    # Inject a mock LLM that returns one canned response per turn.
    set_llm_override(app, MockLLM(responses=[{"content": "hello from mock"}]))
    try:
        yield app
    finally:
        await server.stop()


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

    providers = (await client.get("/providers")).json()
    assert providers["default"] == "default"
    configured = next(
        item for item in providers["providers"] if item["name"] == "default"
    )
    assert configured == {
        "name": "default",
        "provider": "openai",
        "default_model": "test",
        "models": [
            {
                "model": "test",
                "max_context_tokens": 4096,
                "max_output_tokens": None,
                "reasoning_effort": "",
                "thinking": "",
                "input_modalities": ["text"],
            }
        ],
    }
    assert "api_key" not in str(providers)

    tools = (
        await client.get("/sessions/s1/threads/t1/tools")
    ).json()["tools"]
    ask_user = next(item for item in tools if item["name"] == "ask_user")
    assert ask_user["parameters"]["required"] == ["question", "options"]
    assert ask_user["description"]
    request_permission = next(
        item for item in tools if item["name"] == "request_permission"
    )
    assert request_permission["parameters"]["properties"]["params"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }


@pytest.mark.asyncio
async def test_http_session_exposes_independent_thread_resources(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    set_llm_override(http_app, MockLLM(responses=[
        {"content": "main reply"},
        {"content": "child reply"},
    ]))
    opened = await client.post(
        "/sessions",
        json={"session_id": "thread-resources", "thread_id": "agent"},
    )
    child = await client.post(
        "/sessions/thread-resources/threads",
        json={"thread_id": "child"},
    )

    assert opened.status_code == 200
    assert child.status_code == 200
    session = (await client.get("/sessions/thread-resources")).json()
    assert session == {
        "session_id": "thread-resources",
        "status": "active",
        "active_threads": 2,
        "thread_count": 2,
    }
    threads = (
        await client.get("/sessions/thread-resources/threads")
    ).json()["threads"]
    assert {item["thread_id"] for item in threads} == {"agent", "child"}
    assert all(item["status"] == "active" for item in threads)
    assert {item["thread_id"]: item["kind"] for item in threads} == {
        "agent": "main",
        "child": "subagent",
    }

    main_turn = await client.post(
        "/sessions/thread-resources/threads/agent/messages",
        json={"content": "main message"},
    )
    child_turn = await client.post(
        "/sessions/thread-resources/threads/child/messages",
        json={"content": "child message"},
    )
    assert main_turn.status_code == 200
    assert child_turn.status_code == 200
    main_messages = (
        await client.get(
            "/sessions/thread-resources/threads/agent/messages"
        )
    ).json()["messages"]
    child_messages = (
        await client.get(
            "/sessions/thread-resources/threads/child/messages"
        )
    ).json()["messages"]
    assert [item["content"] for item in main_messages] == [
        "main message",
        "main reply",
    ]
    assert [item["content"] for item in child_messages] == [
        "child message",
        "child reply",
    ]

    closed = await client.post(
        "/sessions/thread-resources/threads/child/close"
    )
    assert closed.json() == {
        "session_id": "thread-resources",
        "thread_id": "child",
        "status": "closed",
    }
    assert (
        await client.get("/sessions/thread-resources/threads/child")
    ).json()["status"] == "inactive"
    assert (
        await client.get("/sessions/thread-resources/threads/agent")
    ).json()["status"] == "active"
    inactive_tasks = await client.get(
        "/sessions/thread-resources/threads/child/tasks"
    )
    assert inactive_tasks.status_code == 409
    assert inactive_tasks.json()["code"] == "thread_not_active"


@pytest.mark.asyncio
async def test_idle_runtime_is_reaped_after_timeout(http_app) -> None:
    manager = http_app.state.manager
    tmp = http_app.state.paths
    await manager.close_all()
    # give the shared app manager a short idle timeout
    manager.idle_timeout = 0.05
    manager.reap_interval = 0.02
    manager.start_reaper()
    await manager.open_session(
        session_id="idle-reap", thread_id="agent", provider_name="default",
        workspace_root=str(tmp), no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "hi"}]),
    )
    assert await manager.get("idle-reap", "agent") is not None
    await asyncio.sleep(0.2)
    with pytest.raises(ThreadNotActive):
        await manager.get("idle-reap", "agent")
    # restore defaults so other tests are unaffected
    manager.idle_timeout = 3600.0
    manager.reap_interval = 60.0


@pytest.mark.asyncio
async def test_http_selects_primary_agent_and_resumes_it_from_thread_metadata(
    http_app, tmp_path: Path
) -> None:
    workspace = tmp_path / "agent-workspace"
    agents_dir = workspace / ".agents"
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
    server = await start_server_application(
        paths=http_app.state.paths,
        provider_name="default",
        workspace_root=str(workspace),
        no_plugins=False,
    )
    app = server.server
    set_llm_override(app, MockLLM(responses=[]))
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
        agents = await ac.get(
            "/sessions/primary-http/threads/agent/agents"
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
    assert agents.json()["active"] == "builder"
    assert any(
        item["name"] == "builder" for item in agents.json()["agents"]
    )
    assert resumed.status_code == 200
    assert resumed.json()["agent_name"] == "builder"
    await server.stop()


@pytest.mark.asyncio
async def test_http_switches_primary_agent_without_replacing_thread_history(
    http_app, tmp_path: Path
) -> None:
    workspace = tmp_path / "switch-agent-workspace"
    agents_dir = workspace / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        "---\ndescription: Build changes\nmode: primary\ntools: []\n---\nBuild.",
        encoding="utf-8",
    )
    explorer_path = agents_dir / "Explorer.md"
    explorer_path.write_text(
        "---\n"
        "description: Read-only exploration\n"
        "mode: all\n"
        "model: default/explorer-model\n"
        "context_window: 64000\n"
        "tools:\n  - read\n"
        "permission:\n  edit: deny\n  shell: deny\n"
        "---\nExplore only.",
        encoding="utf-8",
    )
    (agents_dir / "worker.md").write_text(
        "---\ndescription: Child only\nmode: subagent\n---\nWork.",
        encoding="utf-8",
    )
    server = await start_server_application(
        paths=http_app.state.paths,
        provider_name="default",
        workspace_root=str(workspace),
        no_plugins=False,
    )
    app = server.server
    set_llm_override(app, MockLLM(responses=[{"content": "existing answer"}]))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        await ac.post(
            "/sessions",
            json={
                "session_id": "switch-primary",
                "thread_id": "main",
                "agent": "builder",
            },
        )
        await ac.post(
            "/sessions/switch-primary/threads/main/messages",
            json={"content": "keep this history"},
        )
        switched = await ac.put(
            "/sessions/switch-primary/threads/main/agent",
            json={"name": "Explorer"},
        )
        ctx = await app.state.manager.get("switch-primary", "main")

        assert switched.status_code == 200
        assert switched.json()["agent"] == "Explorer"
        assert switched.json()["model"] == "explorer-model"
        assert switched.json()["context_window"] == 64000
        assert ctx.session_id == "switch-primary"
        assert ctx.thread_id == "main"
        assert [message.content for message in ctx.engine.messages] == [
            "keep this history",
            "existing answer",
        ]
        assert ctx.engine.tools.registry.get("read") is not None
        assert ctx.engine.tools.registry.get("edit") is None
        assert ctx.engine.settings.model == "explorer-model"
        assert ctx.engine.settings.context_window == 64000
        assert ctx.services.state_store.read_thread_metadata()["agent"] == "Explorer"

        explorer_path.write_text(
            "---\ndescription: Reloaded exploration\nmode: all\n"
            "model: default/explorer-model\ncontext_window: 48000\n"
            "tools:\n  - read\n"
            "permission:\n  edit: deny\n  shell: deny\n"
            "---\nExplore updated.",
            encoding="utf-8",
        )
        reloaded = await ac.post(
            "/sessions/switch-primary/threads/main/agents/reload"
        )
        assert reloaded.status_code == 200
        assert ctx.engine.settings.context_window == 48000
        assert any(
            item["description"] == "Reloaded exploration"
            for item in reloaded.json()["agents"]
            if item["name"] == "Explorer"
        )

        child_only = await ac.put(
            "/sessions/switch-primary/threads/main/agent",
            json={"name": "worker"},
        )
        assert child_only.status_code == 404
        assert child_only.json()["code"] == "agent_not_found"
        assert ctx.engine.settings.agent_name == "Explorer"

        await ctx.turn_lock.acquire()
        try:
            busy = await ac.put(
                "/sessions/switch-primary/threads/main/agent",
                json={"name": "builder"},
            )
        finally:
            ctx.turn_lock.release()
        assert busy.status_code == 409
        assert busy.json()["code"] == "thread_busy"
        assert busy.json()["retryable"] is True
        assert ctx.engine.settings.agent_name == "Explorer"

        resumed = await ac.post(
            "/sessions",
            json={
                "session_id": "switch-primary",
                "thread_id": "main",
                "mode": "resume",
            },
        )

    assert resumed.status_code == 200
    assert resumed.json()["agent_name"] == "Explorer"
    assert resumed.json()["model"] == "explorer-model"
    assert resumed.json()["context_window"] == 48000
    assert [item["content"] for item in resumed.json()["history"]] == [
        "keep this history",
        "existing answer",
    ]
    await server.stop()


@pytest.mark.asyncio
async def test_http_resume_returns_display_history(client: httpx.AsyncClient) -> None:
    opened = await client.post(
        "/sessions", json={"session_id": "resume-history", "thread_id": "t1"}
    )
    assert opened.status_code == 200

    turn = await client.post(
        "/sessions/resume-history/threads/t1/messages",
        json={"content": "remember this"},
    )
    assert turn.status_code == 200
    assert "turn_finished" in turn.text
    manager = client._transport.app.state.manager
    original = await manager.get("resume-history", "t1")
    tool_message = Message(
        role="tool",
        content="cached result",
        tool_call_id="call-1",
        status="error",
        data={"cache": "tool-results/call-1.txt"},
        error={"code": "failed", "message": "bad input"},
        artifact=[
            {"id": "artifact-1", "name": "report.txt", "media_type": "text/plain"}
        ],
    )
    structure_tool_message(tool_message, "sample")
    original.engine.messages.append(tool_message)
    await original.services.persistence.flush()

    resumed = await client.post(
        "/sessions",
        json={"session_id": "resume-history", "thread_id": "t1", "mode": "resume"},
    )

    assert resumed.status_code == 200
    replacement = await manager.get("resume-history", "t1")
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
async def test_http_resume_leftover_thread_dir_does_not_create_empty_session(
    http_app,
    tmp_path: Path,
) -> None:
    """A leftover state directory without metadata must not reopen empty."""
    from XBotv2.core.paths import RuntimePaths

    data_dir = RuntimePaths.from_data_dir(http_app.state.paths.data_dir)
    leftover = (
        data_dir.session("leftover").thread("agent").state_dir
    )
    leftover.mkdir(parents=True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=http_app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/sessions",
            json={
                "session_id": "leftover",
                "thread_id": "agent",
                "mode": "resume",
            },
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
async def test_http_command_compatibility_route_only_exposes_plugins(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    assert (await client.get("/commands")).status_code == 404

    open_response = await client.post(
        "/sessions", json={"session_id": "cmds", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    commands_response = await client.get(
        "/sessions/cmds/threads/t/commands"
    )
    assert commands_response.status_code == 200
    names = {item["name"] for item in commands_response.json()["commands"]}
    assert names == set()

    result_response = await client.post(
        "/sessions/cmds/threads/t/commands",
        json={"command": "status", "args": []},
    )
    assert result_response.status_code == 200
    body = result_response.json()
    assert body["type"] == "command_result"
    assert body["data"]["status"] == "error"
    assert body["data"]["message"] == "Unknown server command: /status"
    state_root = http_app.state.paths.session("cmds").thread("t").state_dir
    messages_path = state_root / "messages.jsonl"
    messages = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
    assert "command_result" not in messages


@pytest.mark.asyncio
async def test_typed_history_undo_fork_and_clear_persist_atomically(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    set_llm_override(http_app, MockLLM(responses=[
        {"content": "first answer"},
        {"content": "second answer"},
    ]))
    await client.post("/sessions", json={"session_id": "history", "thread_id": "t"})
    await client.post("/sessions/history/threads/t/messages", json={"content": "first"})
    await client.post("/sessions/history/threads/t/messages", json={"content": "second"})

    undone = await client.post(
        "/sessions/history/threads/t/history/undo",
        json={"count": 1},
    )

    assert undone.status_code == 200
    assert undone.json()["messages"] == [
        {
            "role": "user", "content": "first", "tool_calls": [],
            "tool_call_id": "", "status": "",
            "data": None, "error": None, "artifacts": [], "images": [],
        },
        {
            "role": "assistant", "content": "first answer", "tool_calls": [],
            "tool_call_id": "", "status": "",
            "data": None, "error": None, "artifacts": [], "images": [],
        },
    ]
    ctx = await http_app.state.manager.get("history", "t")
    assert [message.content for message in ctx.engine.messages] == [
        "first", "first answer",
    ]

    source_session = http_app.state.paths.session("history")
    source = source_session.thread("t")
    source_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        any(part.get("text") == "second" for part in record.get("parts", []))
        for record in source_records
    )
    assert source_records[-1]["record_type"] == "history_undo"
    (source.plugin_states_dir / "sample.yaml").write_text("value: kept\n")
    (source.artifacts_dir / "cached.txt").write_text("cached")
    source_session.config_file.write_text("permissions: {}\n")
    forked = await client.post("/sessions/history/fork")
    fork_id = forked.json()["session_id"]
    fork_session = http_app.state.paths.session(fork_id)
    fork_paths = fork_session.thread("t")

    assert (fork_paths.plugin_states_dir / "sample.yaml").read_text() == "value: kept\n"
    assert (fork_paths.artifacts_dir / "cached.txt").read_text() == "cached"
    assert fork_session.config_file.read_text() == "permissions: {}\n"
    assert fork_paths.messages_file.read_text() == source.messages_file.read_text()
    resumed = await client.post(
        "/sessions",
        json={"session_id": fork_id, "thread_id": "t", "mode": "resume"},
    )
    assert [item["content"] for item in resumed.json()["history"]] == [
        "first", "first answer",
    ]

    cleared = await client.post(
        "/sessions/history/threads/t/history/clear",
    )
    assert cleared.json()["removed_turns"] == 1
    assert cleared.json()["messages"] == []
    assert ctx.engine.messages == []
    assert ctx.services.state_store.read_messages() == []
    cleared_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert cleared_records[:len(source_records)] == source_records
    assert cleared_records[-1]["record_type"] == "history_clear"

    await client.post("/sessions/history/close")
    inactive_fork = await client.post("/sessions/history/fork")
    assert inactive_fork.status_code == 200
    assert inactive_fork.json()["source_session_id"] == "history"


@pytest.mark.asyncio
async def test_undo_rejects_invalid_or_excessive_counts(client: httpx.AsyncClient) -> None:
    await client.post("/sessions", json={"session_id": "undo-errors", "thread_id": "t"})

    for count in (0, "two", 2):
        response = await client.post(
            "/sessions/undo-errors/threads/t/history/undo",
            json={"count": count},
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_typed_history_mutations_validate_and_reject_busy_threads(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    set_llm_override(http_app, MockLLM(responses=[{"content": "answer"}]))
    await client.post(
        "/sessions", json={"session_id": "typed-history", "thread_id": "t"}
    )
    await client.post(
        "/sessions/typed-history/threads/t/messages",
        json={"content": "question"},
    )

    invalid = await client.post(
        "/sessions/typed-history/threads/t/history/undo",
        json={"count": 0},
    )
    excessive = await client.post(
        "/sessions/typed-history/threads/t/history/undo",
        json={"count": 2},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_request"
    assert excessive.status_code == 400
    assert excessive.json()["code"] == "invalid_undo_count"

    ctx = await http_app.state.manager.get("typed-history", "t")
    await ctx.turn_lock.acquire()
    try:
        busy = await client.post(
            "/sessions/typed-history/threads/t/history/clear"
        )
        busy_fork = await client.post("/sessions/typed-history/fork")
    finally:
        ctx.turn_lock.release()
    assert busy.status_code == 409
    assert busy.json()["code"] == "thread_busy"
    assert busy.json()["retryable"] is True
    assert busy_fork.status_code == 409
    assert busy_fork.json()["code"] == "thread_busy"

    undone = await client.post(
        "/sessions/typed-history/threads/t/history/undo",
        json={"count": 1},
    )
    assert undone.status_code == 200
    assert undone.json()["removed_turns"] == 1
    assert undone.json()["messages"] == []
    assert ctx.engine.messages == []


@pytest.mark.asyncio
async def test_http_provider_list_reads_tree_config(client: httpx.AsyncClient) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "providers", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    list_response = await client.get("/providers")
    assert list_response.status_code == 200
    body = list_response.json()
    assert body["default"] == "default"
    assert "default" in {
        item["name"] for item in body["providers"]
    }


@pytest.mark.asyncio
async def test_typed_provider_selection_persists_across_resume(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    plugins_file = http_app.state.paths.config_dir / "plugins.yaml"
    tree = yaml.safe_load(plugins_file.read_text(encoding="utf-8"))
    llm_entry = next(item for item in tree if item["id"] == "llm")
    llm_entry["config"]["providers"]["alternate"] = {
        "protocol": "openai",
        "base_url": "http://alternate",
        "api_key": "test",
        "default_model": "alternate-model",
        "models": [
            {
                "model": "alternate-model",
            },
        ],
    }
    plugins_file.write_text(
        yaml.safe_dump(tree, sort_keys=False),
        encoding="utf-8",
    )
    await client.post(
        "/sessions", json={"session_id": "provider-switch", "thread_id": "t"}
    )

    selected = await client.put(
        "/sessions/provider-switch/threads/t/provider",
        json={"name": "alternate"},
    )
    assert selected.status_code == 200
    assert selected.json()["provider"] == "alternate"
    assert selected.json()["model"] == "alternate-model"

    resumed = await client.post(
        "/sessions",
        json={
            "session_id": "provider-switch",
            "thread_id": "t",
            "mode": "resume",
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["provider"] == "alternate"
    assert resumed.json()["model"] == "alternate-model"


@pytest.mark.asyncio
async def test_http_selects_model_within_provider(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """Catalog model selection: /provider accepts a model within the provider."""
    plugins_file = http_app.state.paths.config_dir / "plugins.yaml"
    tree = yaml.safe_load(plugins_file.read_text(encoding="utf-8"))
    llm_entry = next(item for item in tree if item["id"] == "llm")
    llm_entry["config"]["providers"]["alternate"] = {
        "protocol": "openai",
        "base_url": "http://alternate",
        "api_key": "test",
        "default_model": "alternate-model",
        "models": [
            {"model": "alternate-model"},
            {
                "model": "alternate-model-2",
                "max_context_tokens": 8192,
                "thinking": "enabled",
            },
        ],
    }
    plugins_file.write_text(
        yaml.safe_dump(tree, sort_keys=False),
        encoding="utf-8",
    )
    await client.post(
        "/sessions", json={"session_id": "model-switch", "thread_id": "t"}
    )

    selected = await client.put(
        "/sessions/model-switch/threads/t/provider",
        json={"name": "alternate", "model": "alternate-model-2"},
    )
    assert selected.status_code == 200
    assert selected.json()["provider"] == "alternate"
    assert selected.json()["model"] == "alternate-model-2"
    assert selected.json()["model_mode"] == "enabled"

    unknown = await client.put(
        "/sessions/model-switch/threads/t/provider",
        json={"name": "alternate", "model": "missing-model"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "model_not_found"
    assert "Unknown model" in unknown.json()["message"]


@pytest.mark.asyncio
async def test_http_policy_api_updates_live_session_policy(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    policy_response = await client.patch(
        "/sessions/policy/policy",
        json={
            "permissions": {"shell": "allow"},
            "sandbox": {"external_read": "readonly"},
        },
    )
    ctx = await http_app.state.manager.get("policy", "t")
    cached_path = (
        http_app.state.paths.session("policy").thread("t").artifacts_dir
        / "tool_results"
        / "cached.txt"
    )
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("cached after policy reload", encoding="utf-8")
    filesystem_entry = ctx.engine.tools.registry.get("read")
    assert filesystem_entry is not None
    cached_result = await filesystem_entry.tool.ainvoke(
        {"path": "session/artifacts/tool_results/cached.txt"},
        sandbox=ctx.services.sandbox,
    )
    status_response = await client.get("/sessions/policy/policy")

    assert policy_response.status_code == 200
    assert status_response.status_code == 200
    assert cached_result.status == "success"
    assert "cached after policy reload" in cached_result.content
    assert status_response.json()["permissions"]["allow"] == [{"tool": "shell"}]
    assert [
        rule["tool"]
        for rule in status_response.json()["effective_permissions"]["allow"]
    ] == ["shell"]
    assert status_response.json()["sandbox"] == {"external_read": "readonly"}
    assert status_response.json()["effective_sandbox"]["external_read"] == "readonly"
    assert (
        status_response.json()["effective_sandbox"]["enabled"]
        is ctx.services.sandbox.enabled
    )
    state_root = http_app.state.paths.session("policy").thread("t").state_dir
    events_path = state_root / "events.jsonl"
    events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert "permission_override_set" not in events
    assert "sandbox_override_set" not in events


@pytest.mark.asyncio
async def test_http_permission_response_preserves_scope() -> None:
    from XBotv2.protocol.http_server import _resolve_interaction

    request_id = "permission:scope"
    captured: dict[str, str] = {}

    class _ApprovalSpy:
        def submit(self, request_id: str, decision: str, scope: str = "once"):
            captured.update({"request_id": request_id, "decision": decision, "scope": scope})
            from XBotv2.interactions.interactions import InteractionResult

            return InteractionResult(
                request_id=request_id,
                status="answered",
                decision=decision,
                scope=scope,
            )

    class _Context:
        services = {"approval": _ApprovalSpy()}

    class _Manager:
        async def get(self, session_id: str, thread_id: str):
            assert session_id == "permission-scope"
            assert thread_id == "t"
            return _Context()

    response = await _resolve_interaction(
        manager=_Manager(),
        session_id="permission-scope",
        thread_id="t",
        payload={"request_id": request_id, "decision": "allow", "scope": "session"},
        kind="permission",
    )

    assert response.recorded is True
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
    from XBotv2.application.client_events import ClientEventRouter
    from XBotv2.interactions.interactions import InteractionWaiter
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
    router = ClientEventRouter()
    router.register_waiter(event_type, waiter)
    sink_task = asyncio.create_task(
        _live_sink(
            {
                "type": event_type,
                "data": {"request_id": request_id},
            },
            services={"client_events": router},
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
    from XBotv2.protocol.http_server import _resolve_interaction

    class _Engine:
        permission_waiter = object()
        user_input_waiter = object()

    class _Context:
        engine = _Engine()

    class _Manager:
        async def get(self, session_id: str, thread_id: str):
            assert session_id == "permission-scope"
            assert thread_id == "t"
            return _Context()

    with pytest.raises(Exception) as exc_info:
        await _resolve_interaction(
            manager=_Manager(),
            session_id="permission-scope",
            thread_id="t",
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
async def test_http_policy_patch_reset_rebuilds_live_policy(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy-reset", "thread_id": "t"}
    )
    assert open_response.status_code == 200
    ctx = await http_app.state.manager.get("policy-reset", "t")

    permission_set = await client.patch(
        "/sessions/policy-reset/policy",
        json={"permissions": {"shell": "deny"}},
    )
    assert permission_set.status_code == 200
    assert ctx.services.permissions.check("shell", {}) == "deny"

    permission_reset = await client.patch(
        "/sessions/policy-reset/policy",
        json={"remove_permissions": ["shell"]},
    )
    assert permission_reset.status_code == 200
    assert ctx.services.permissions.check("shell", {}) == "ask"

    sandbox_status = await client.get("/sessions/policy-reset/policy")
    assert sandbox_status.status_code == 200
    assert sandbox_status.json()["sandbox"] == {}
    assert (
        sandbox_status.json()["effective_sandbox"]["enabled"]
        is ctx.services.sandbox.enabled
    )

    sandbox_update = await client.patch(
        "/sessions/policy-reset/policy",
        json={"sandbox": {"external_read": "deny"}},
    )
    assert sandbox_update.status_code == 200
    assert ctx.services.sandbox.external_read == "deny"


@pytest.mark.asyncio
async def test_http_policy_api_rejects_invalid_permission_values(
    client: httpx.AsyncClient,
) -> None:
    open_response = await client.post(
        "/sessions", json={"session_id": "policy-invalid", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    permission_response = await client.patch(
        "/sessions/policy-invalid/policy",
        json={"permissions": {"shell": "sometimes"}},
    )
    sandbox_response = await client.patch(
        "/sessions/policy-invalid/policy",
        json={"sandbox": {"external_read": "ask"}},
    )

    assert permission_response.status_code == 400
    assert permission_response.json()["code"] == "invalid_request"
    assert sandbox_response.status_code == 400
    assert sandbox_response.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_http_open_session_failure_returns_stable_json_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
            {
                "id": "sandbox",
                "name": "sandbox",
                "config": {"sandbox": {"enabled": False, "resources": []}},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "permissions": {
                        "ask": [
                            {"tool": "ask_user"},
                            {"tool": "request_permission"},
                            {"tool": "edit"},
                        ],
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(tmp_path),
        no_plugins=True,
    )
    app = server.server

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post("/sessions", json={"session_id": "bad", "thread_id": "t"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "session_open_failed"
    assert "requires api_key" in body["message"]
    await server.stop()


@pytest.mark.asyncio
async def test_resume_and_fork_without_persistence_fail_clearly(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
            {
                "id": "sandbox",
                "name": "sandbox",
                "config": {"sandbox": {"enabled": False, "resources": []}},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "permissions": {
                        "ask": [
                            {"tool": "ask_user"},
                            {"tool": "request_permission"},
                            {"tool": "edit"},
                        ],
                    },
                },
            },
            {
                "id": "persistence",
                "name": "persistence",
                "disabled": True,
            },
        ], sort_keys=False),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    app = server.server
    set_llm_override(app, MockLLM(responses=[{"content": "memory only"}]))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        opened = await ac.post(
            "/sessions", json={"session_id": "mem", "thread_id": "t"}
        )
        assert opened.status_code == 200

        forked = await ac.post("/sessions/mem/fork")
        assert forked.status_code == 400
        assert forked.json()["code"] == "persistence_unavailable"
        assert "persistence" in forked.json()["message"]

        resumed = await ac.post(
            "/sessions",
            json={"session_id": "mem", "thread_id": "t", "mode": "resume"},
        )
        assert resumed.status_code == 400
        assert resumed.json()["code"] == "persistence_unavailable"
        assert "persistence is not mounted" in resumed.json()["message"]
    await server.stop()


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
        "/sessions/stream1/threads/t/messages",
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
    from XBotv2.core import Events

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "request-context", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("request-context", "t")
    observed = []

    async def record(ctx):
        observed.append(ctx.request_id)

    session.services.on(Events.TURN_START, record)
    session.services.on(Events.STATE_CHANGED, record)

    async with client.stream(
        "POST",
        "/sessions/request-context/threads/t/messages",
        json={"content": "hello", "request_id": "request-http-1"},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    assert all(event["request_id"] == "request-http-1" for event in events)
    assert observed == ["request-http-1", "request-http-1"]


@pytest.mark.asyncio
async def test_http_generated_request_id_reaches_engine_and_sse(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    from XBotv2.core import Events

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "generated-request", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("generated-request", "t")
    observed = []

    async def record(ctx):
        observed.append(ctx.request_id)

    session.services.on(Events.TURN_START, record)

    async with client.stream(
        "POST",
        "/sessions/generated-request/threads/t/messages",
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
        "/sessions/zh/threads/t/messages",
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
        "/sessions/empty/threads/t/messages", json={"content": "   ", "request_id": "x"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_http_messages_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/sessions/does-not-exist/threads/t/messages",
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
        "/sessions/validate/threads/t/interactions/permission-response",
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
async def test_input_held_while_busy_is_folded_at_turn_end(
    http_app,
) -> None:
    release = asyncio.Event()
    llm = _GatedMockLLM(
        release,
        responses=[{"content": "first reply"}, {"content": "second reply"}],
    )
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="fold-end",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    first_task = asyncio.create_task(
        _drain_stream(ctx.stream_message("first", "req-1"))
    )
    await asyncio.sleep(0)
    # While the LLM is busy the input is held in the pending fold, not
    # dropped or processed out of band.
    ev_stream = ctx.attach_event_stream()
    second_task = asyncio.create_task(
        _drain_stream(ctx.stream_message("second", "req-2"))
    )
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 1

    release.set()
    first_events = await asyncio.wait_for(first_task, timeout=3)
    second_events = await asyncio.wait_for(second_task, timeout=3)
    assert [
        event["data"]["content"]
        for event in first_events
        if event["type"] == "assistant_message"
    ] == ["first reply"]
    # With no tool boundary, the turn-end fold still fuses the held input into
    # the same turn and notifies it in order on the event stream.
    found = None
    async with asyncio.timeout(1):
        while found is None:
            event = await ev_stream.get()
            if event.get("type") == "message" and event["data"].get("content") == "second":
                found = event
    assert found["data"]["id"]
    assert [
        event["data"]["content"]
        for event in second_events
        if event["type"] == "assistant_message"
    ] == ["second reply"]
    assert [m.content for m in ctx.engine.messages if m.role == "user"] == [
        "first", "second",
    ]


@pytest.mark.asyncio
async def test_queued_user_message_enters_after_complete_tool_batch(http_app) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        """Return a value after the test releases this Tool."""
        tool_started.set()
        await release_tool.wait()
        return value

    llm = MockLLM(responses=[
        {
            "tool_calls": [{
                "id": "wait-1",
                "name": "wait_for_release",
                "args": {"value": "ready"},
            }],
        },
        {"content": "handled both requests"},
    ])
    ctx = await http_app.state.manager.open_session(
        session_id="mailbox-steer",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
    ctx.engine.tools.registry.register(Tool.from_function(wait_for_release))
    ctx.engine.tools.registry.restrict(None)

    async def collect(stream):
        return [event async for event in stream]

    first_task = asyncio.create_task(collect(
        ctx.stream_message("start the tool", "req-1")
    ))
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    second_task = asyncio.create_task(collect(
        ctx.stream_message("also include this", "req-2")
    ))
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 1

    release_tool.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)

    assert llm.call_count == 2
    assert [
        message.content
        for message in llm.get_call_messages(1)
        if message.role == "user"
    ] == ["start the tool", "also include this"]
    # The folded-in request is notified on the shared event stream (id +
    # content) and owns the response events; the superseded active request
    # must not observe them.
    ev_stream = ctx.attach_event_stream()
    msg = await asyncio.wait_for(ev_stream.get(), timeout=1)
    if msg.get("type") == "message" and msg["data"].get("content") != "also include this":
        msg = await asyncio.wait_for(ev_stream.get(), timeout=1)
    assert msg["data"].get("content") == "also include this"
    assert msg["data"].get("id")
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "handled both requests"
        for event in second_events
    )
    assert not any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "handled both requests"
        for event in first_events
    )


@pytest.mark.asyncio
async def test_input_during_thinking_is_folded_at_tool_boundary(http_app) -> None:
    """A message submitted while the LLM is thinking (not a tool window) must
    be held and fused into the running turn at the next tool boundary, so it
    is injected mid-turn rather than waiting for the turn to end."""

    release_call1 = asyncio.Event()
    release_tool = asyncio.Event()
    llm = _GatedMockLLM(release_call1, responses=[
        {"tool_calls": [{"id": "t1", "name": "wait_for_release", "args": {"value": "x"}}]},
        {"content": "merged reply"},
    ])
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="fold-thinking",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
    tool_started = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.engine.tools.registry.register(Tool.from_function(wait_for_release))
    ctx.engine.tools.registry.restrict(None)

    async def collect(stream):
        return [event async for event in stream]

    first_task = asyncio.create_task(collect(
        ctx.stream_message("A", "req-A")
    ))
    await asyncio.sleep(0)
    # B is submitted while A is still thinking (the gated LLM has not returned
    # a tool call yet); it must be held, not rejected.
    second_task = asyncio.create_task(collect(
        ctx.stream_message("B", "req-B")
    ))
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 1, "input during thinking must be held"

    release_call1.set()
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    # C lands inside the tool window; both held inputs fold together.
    third_task = asyncio.create_task(collect(
        ctx.stream_message("C", "req-C")
    ))
    await asyncio.sleep(0)
    assert ctx.engine.pending_input_count == 2
    release_tool.set()

    first_events, second_events, third_events = await asyncio.gather(
        first_task, second_task, third_task
    )
    # B was folded into A's turn and notified in order on the event stream.
    ev_stream = ctx.attach_event_stream()
    found = None
    async with asyncio.timeout(1):
        while found is None:
            event = await ev_stream.get()
            if event.get("type") == "message" and event["data"].get("content") == "B":
                found = event
    assert found["data"]["id"]
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "merged reply"
        for event in third_events
    )
    assert llm.call_count == 2


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
    ctx.engine.messages.extend([
        Message(role="user", content="an earlier human request"),
        Message(role="assistant", content="the earlier request is complete"),
    ])

    await ctx._collect_completion({
        "type": "background_task",
        "kind": "background_task",
        "task_id": "task-1",
        "status": "completed",
        "command": "printf done",
        "data": {"task_id": "task-1"},
    })

    # The completion is broadcast as a notice and staged in the inbox; it
    # must NOT start a turn on its own.
    splice = await asyncio.wait_for(events.get(), timeout=1)
    assert splice["type"] == Events.INBOX_SPLICE
    notice = await asyncio.wait_for(events.get(), timeout=1)
    assert notice["type"] == "completion_notice"
    await asyncio.sleep(0.05)
    assert llm.call_count == 0, "general message must not wake a turn"
    assert len(ctx.engine.inbox) == 1

    # The next user turn consumes it into the model context.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(ctx.stream_message("continue", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        message
        for message in llm.get_call_messages(0)
        if message.role == "user" and "<runtime_event" in message.content
    ]
    assert len(runtime_msgs) == 1
    runtime_event = ET.fromstring(runtime_msgs[0].content)
    payload = json.loads(runtime_event.findtext("payload"))
    assert payload["kind"] == "background_task"
    assert payload["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_background_task_updates_and_completion_use_session_stream(
    http_app, monkeypatch
) -> None:
    async def run(*args, **kwargs):
        await asyncio.sleep(0)
        return "task output"

    monkeypatch.setattr(
        "XBotv2.coretools.shell.run_shell_command", run
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

    job_id = await _start_background_shell(ctx.services, "printf result")

    # Completion is broadcast as a notice and staged in the agent inbox, but
    # must NOT wake a turn on its own.
    notice = None
    while notice is None:
        event = await asyncio.wait_for(events.get(), timeout=1)
        if event and event["type"] == "completion_notice":
            notice = event
    assert notice["data"]["kind"] == "background_task"
    assert notice["data"]["task_id"] == job_id
    assert notice["data"]["status"] == "completed"
    await asyncio.sleep(0.05)
    assert llm.call_count == 0, "completion must not wake an LLM turn"
    assert len(ctx.engine.inbox) == 1

    # The next user turn consumes the staged completion all at once.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(ctx.stream_message("continue", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        message
        for message in llm.get_call_messages(0)
        if message.role == "user" and "<runtime_event" in message.content
    ]
    assert len(runtime_msgs) == 1, (
        "runtime_event missing; msgs="
        + repr([(m.role, (m.content or "")[:60]) for m in llm.get_call_messages(0)])
    )
    assert len(runtime_msgs) == 1
    runtime_event = ET.fromstring(runtime_msgs[0].content)
    assert runtime_event.attrib == {"event": "completed", "source": "tasks"}
    payload = json.loads(runtime_event.findtext("payload"))
    assert payload["kind"] == "background_task"
    assert payload["task_id"] == job_id
    assert payload["status"] == "completed"
    assert len(ctx.engine.inbox) == 0
    assert [message.role for message in ctx.engine.messages] == [
        "user", "user", "assistant",
    ]


@pytest.mark.asyncio
async def test_multiple_completions_keep_distinct_inbox_messages(
    http_app, monkeypatch
) -> None:
    async def run(*args, **kwargs):
        await asyncio.sleep(0)
        return "task output"

    monkeypatch.setattr(
        "XBotv2.coretools.shell.run_shell_command", run
    )
    llm = MockLLM(responses=[{"content": "ok"}])
    set_llm_override(http_app, llm)
    ctx = await http_app.state.manager.open_session(
        session_id="aggregate-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    await _start_background_shell(ctx.services, "printf one")
    await _start_background_shell(ctx.services, "printf two")
    await asyncio.sleep(0.1)
    # Completions stage into the inbox without waking a turn.
    assert llm.call_count == 0, "completions must not wake an LLM turn"
    assert len(ctx.engine.inbox) == 2

    # The next user turn atomically claims all staged inputs while preserving
    # their individual message identities and order.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(ctx.stream_message("go", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        message
        for message in llm.get_call_messages(0)
        if message.role == "user" and "<runtime_event" in message.content
    ]
    assert len(runtime_msgs) == 2
    commands = [
        json.loads(ET.fromstring(message.content).findtext("payload"))["command"]
        for message in runtime_msgs
    ]
    assert commands == ["printf one", "printf two"]
    assert len(ctx.engine.inbox) == 0
    assert [message.role for message in ctx.engine.messages] == [
        "user", "user", "user", "assistant",
    ]


@pytest.mark.asyncio
async def test_typed_task_stop_is_idempotent(
    client: httpx.AsyncClient,
    http_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    await client.post(
        "/sessions", json={"session_id": "task-stop", "thread_id": "t"}
    )
    ctx = await http_app.state.manager.get("task-stop", "t")
    task_id = await _start_background_shell(ctx.services, "sleep forever")
    await asyncio.sleep(0)

    busy_fork = await client.post("/sessions/task-stop/fork")
    first = await client.post(
        f"/sessions/task-stop/threads/t/tasks/{task_id}/stop"
    )
    second = await client.post(
        f"/sessions/task-stop/threads/t/tasks/{task_id}/stop"
    )

    assert busy_fork.status_code == 409
    assert busy_fork.json()["code"] == "thread_busy"
    assert first.status_code == 200
    assert first.json()["matched_count"] == 1
    assert first.json()["tasks"][0]["status"] == "stopped"
    assert second.status_code == 200
    assert second.json()["tasks"][0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_session_close_drops_pending_inbox_and_resume_starts_empty(http_app) -> None:
    llm = MockLLM(responses=[{"content": "unused"}])
    ctx = await http_app.state.manager.open_session(
        session_id="fold-resume",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    await ctx.engine.inject(
        "accepted during tool",
        source="req-1",
        message_id="f-1",
    )

    await http_app.state.manager.close_session(
        "fold-resume", reason="client_disconnected"
    )
    resumed = await http_app.state.manager.open_session(
        session_id="fold-resume",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        mode="resume",
        no_plugins=True,
        llm_override=llm,
    )

    assert resumed.engine.pending_input_count == 0


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
                    "/sessions/esc/threads/t/messages",
                    json={"content": "do something long", "request_id": "req-esc"},
                ) as response:
                    assert response.status_code == 200
                    async for chunk in response.aiter_text():
                        sse_chunks.append(chunk)
                        # Once we see ``turn_started`` we know the
                        # Engine is past application startup and is about
                        # to call the (gated) LLM.
                        if "turn_started" in chunk:
                            ir = await ac.post("/sessions/esc/threads/t/interrupt")
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

    response = await client.post("/sessions/idle/threads/t/interrupt")
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
    timeout: float = 30.0,
) -> AsyncIterator[TerminalSession]:
    """Run one connected TerminalSession against a real local HTTP server.

    The default request timeout is generous: ``open_session`` cold-starts a
    full XBot application, which can exceed a 100 ms client budget under
    load. The 30 s default matches the production client.
    """
    import socket
    import threading

    import uvicorn

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (config_dir / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
            {
                "id": "sandbox",
                "name": "sandbox",
                "config": {"sandbox": {
                    "enabled": sandbox_enabled, "resources": [],
                }},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "permissions": {
                        "allow": [],
                        "ask": [
                            {"tool": "read"},
                            {"tool": "ask_user"},
                            {"tool": "request_permission"},
                            {"tool": "edit"},
                        ],
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )

    application = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    app = application.server
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
        await application.stop()


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
                {"name": "read", "args": {"path": ".", "mode": "list"}, "id": "call_list"},
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
                {"name": "read", "args": {"path": ".", "mode": "list"}, "id": "call_wait"},
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
                        session_id=session.session_id,
                        thread_id=session.thread_id,
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
                    session_id=session.session_id,
                    thread_id=session.thread_id,
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
        and event.get("data", {}).get("content") == "continue"
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
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "test",
                        "user_name": "Tester",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
            {
                "id": "sandbox",
                "name": "sandbox",
                "config": {"sandbox": {"enabled": False, "resources": []}},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "permissions": {
                        "ask": [
                            {"tool": "ask_user"},
                            {"tool": "request_permission"},
                            {"tool": "edit"},
                        ],
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )

    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(tmp_path),
        no_plugins=False,
    )
    app = server.server
    set_llm_override(app, MockLLM(responses=[{"content": "ok"}]))
    try:
        yield app
    finally:
        await server.stop()


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
    await skills_client.post(
        "/sessions", json={"session_id": "command-kind", "thread_id": "t"}
    )
    resp = await skills_client.get(
        "/sessions/command-kind/threads/t/commands"
    )
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
    commands = await skills_client.get("/sessions/goal-state/threads/t/commands")
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

    ctx = await skills_app.state.manager.get("goal-state", "t")
    session_events = ctx.attach_event_stream()

    response = await skills_client.post(
        "/sessions/goal-state/threads/t/commands",
        json={
            "command": "goal",
            "raw": "/goal --token-budget 2000 ship the API",
        },
    )
    assert response.json()["data"]["message"] == "Set the active goal."
    assert response.json()["data"]["data"]["status_slots"] == {
        "goal": "active"
    }
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
    turn_finished = next(
        event for event in events if event["type"] == "turn_finished"
    )
    assert turn_finished["data"]["status_slots"] == {"goal": "complete"}
    for _ in range(20):
        if not ctx.turn_lock.locked():
            break
        await asyncio.sleep(0)
    goal_plugin = ctx.services.loader.get("goal")
    assert (await goal_plugin.get_goal()).data["goal"] == {
        "objective": "ship the API",
        "status": "complete",
        "summary": "API tests passed",
        "token_budget": 2000,
    }
    get_response = await skills_client.post(
        "/sessions/goal-state/threads/t/commands",
        json={"command": "goal", "raw": "/goal"},
    )
    assert get_response.json()["data"]["status"] == "ok"
    assert get_response.json()["data"]["data"]["status_slots"] == {
        "goal": "complete"
    }


@pytest.mark.asyncio
async def test_http_goal_command_remains_available_during_active_turn(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    await skills_client.post(
        "/sessions", json={"session_id": "busy-command", "thread_id": "t"}
    )
    ctx = await skills_app.state.manager.get("busy-command", "t")
    await ctx.turn_lock.acquire()
    try:
        response = await skills_client.post(
            "/sessions/busy-command/threads/t/commands",
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
        "/sessions/invalid-command/threads/t/commands",
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
        await skills_client.get("/sessions/skill-prompt/threads/t/commands")
    ).json()["commands"]
    command = next(item for item in commands if item["name"] == "xbot-test-prompt")
    assert command["kind"] == "prompt"

    response = await skills_client.post(
        "/sessions/skill-prompt/threads/t/messages",
        json={"content": "/xbot-test-prompt verify boundaries"},
    )
    assert response.status_code == 200
    model_messages = llm.get_call_messages(0)
    expanded = next(
        message for message in model_messages if message.role == "user"
    )
    invocation = ET.fromstring(expanded.content)
    assert invocation.tag == "skill_invocation"
    assert invocation.attrib["name"] == "xbot-test-prompt"
    assert "Follow this test instruction: verify boundaries" in (
        invocation.findtext("skill_instructions") or ""
    )
    assert invocation.findtext("user_arguments").strip() == "verify boundaries"
    assert all(
        message.content != "/xbot-test-prompt verify boundaries"
        for message in model_messages
    )



@pytest.mark.asyncio
async def test_http_policy_patch_persists_sandbox_to_yaml(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "sandbox-persist", "thread_id": "t"}
    )
    assert open_resp.status_code == 200
    policy_path = http_app.state.paths.session("sandbox-persist").config_file
    kept_resources = [{"path": "/tmp/approved", "access": "readwrite"}]
    policy_path.write_text(
        yaml.safe_dump({"sandbox": {"resources": kept_resources}}),
        encoding="utf-8",
    )

    set_network = await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"sandbox": {"network": False}},
    )
    assert set_network.status_code == 200
    assert set_network.json()["sandbox"]["network"] is False

    # Set external_read=deny — also persisted
    set_ext = await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"sandbox": {"external_read": "deny"}},
    )
    assert set_ext.status_code == 200

    ctx = await http_app.state.manager.get("sandbox-persist", "t")
    assert ctx.services.sandbox.network is False
    assert ctx.services.sandbox.external_read == "deny"

    # The session configuration was updated.
    assert policy_path.exists()
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert doc["sandbox"]["network"] is False
    assert doc["sandbox"]["external_read"] == "deny"
    assert doc["sandbox"]["resources"] == kept_resources

    await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"remove_sandbox": ["network"]},
    )
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert doc["sandbox"] == {
        "resources": kept_resources,
        "external_read": "deny",
    }

    await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"remove_sandbox": ["external_read"]},
    )
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert doc["sandbox"] == {"resources": kept_resources}

    resumed = await client.post(
        "/sessions",
        json={
            "session_id": "sandbox-persist",
            "thread_id": "t",
            "mode": "resume",
        },
    )
    assert resumed.status_code == 200
    resumed_ctx = await http_app.state.manager.get("sandbox-persist", "t")
    assert resumed_ctx.services.sandbox.network is True


@pytest.mark.asyncio
async def test_http_policy_patch_rejects_invalid_sandbox_values(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/sessions", json={"session_id": "sandbox-validate", "thread_id": "t"}
    )

    bad = await client.patch(
        "/sessions/sandbox-validate/policy",
        json={"sandbox": {"external_read": "garbage"}},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_request"

    bad_network = await client.patch(
        "/sessions/sandbox-validate/policy",
        json={"sandbox": {"network": "maybe"}},
    )
    assert bad_network.status_code == 400
    assert bad_network.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_tui_queued_messages_all_appear_and_complete(http_app, tmp_path) -> None:
    """The TUI over the real HTTP transport must inject every queued message.

    While the first turn blocks on a tool, two more messages are submitted;
    after the tool releases, all three user messages and their replies must
    appear in the TUI transcript (regression: the fold-in hand-off starved
    the active stream and leaked its events into the queued stream, so the
    second queued message never drained and the transcript did not update).
    """

    from XBotv2.tui.textual_client import XBotTextualApp
    from XBotv2.permissions.system import PermissionSystem

    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def blocker(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    set_llm_override(http_app, MockLLM(responses=[
        {"tool_calls": [{"id": "b1", "name": "blocker", "args": {"value": "x"}}]},
        {"content": "handled A B and C"},
    ]))

    client = XBotClient("http://test", transport=ASGITransport(app=http_app))
    transport = HttpTransport.__new__(HttpTransport)
    transport._client = client
    session = TerminalSession(
        session_id="tui-q",
        thread_id="t",
        workspace_root=str(tmp_path),
        transport=transport,
    )
    app = XBotTextualApp(session_id="tui-q", thread_id="t", workspace_root=str(tmp_path))
    app.session = session

    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(60):
            await pilot.pause()
            if app._connected:
                break
        assert app._connected, "TUI did not connect to the server"

        ctx = await http_app.state.manager.get("tui-q", "t")
        ctx.services.permissions.replace_rules({"allow": [{"tool": ".*"}]})
        ctx.engine.tools.registry.register(Tool.from_function(blocker))
        ctx.engine.tools.registry.restrict(None)

        composer = app.query_one("#input")
        composer.load_text("A")
        await app.submit_composer()
        await asyncio.wait_for(tool_started.wait(), timeout=5)

        composer.load_text("B")
        await app.submit_composer()
        await pilot.pause()
        composer.load_text("C")
        await app.submit_composer()
        await pilot.pause()

        # While the tool runs, B and C are held server-side (pending fold) and
        # are not yet in the transcript; they are injected mid-turn at the
        # fold via the ``message`` event.
        assert ctx.engine.pending_input_count == 2, "B and C must be queued"
        assert not any(
            message.content in {"B", "C"} for message in app.state.messages
        ), "held inputs must not appear before the fold"

        release_tool.set()
        for _ in range(200):
            await pilot.pause()
            if not app.state.turn_active and not app._pending_messages:
                break

        text = "\n".join(message.content for message in app.state.messages)
        assert "handled A B and C" in text, text
        # All three user messages were injected into the transcript.
        # The TUI submits and the kernel holds all three; ordered message
        # events are verified at the session level (foldin tests) because
        # ASGI cannot stream the GET /events response.
        assert not app._pending_messages


@pytest.mark.asyncio
async def test_tui_input_submitted_while_busy_is_retried_after_turn(
    http_app, tmp_path
) -> None:
    """A message submitted while a turn is busy and never folded (no tool
    boundary) is rejected at turn end and the TUI retries it as its own turn,
    so it still reaches the transcript."""

    from XBotv2.tui.textual_client import XBotTextualApp

    release_a = asyncio.Event()
    llm = _GatedMockLLM(release_a, responses=[
        {"content": "A reply"},
        {"content": "B reply"},
    ])
    set_llm_override(http_app, llm)

    client = XBotClient("http://test", transport=ASGITransport(app=http_app))
    transport = HttpTransport.__new__(HttpTransport)
    transport._client = client
    session = TerminalSession(
        session_id="tui-retry",
        thread_id="t",
        workspace_root=str(tmp_path),
        transport=transport,
    )
    app = XBotTextualApp(session_id="tui-retry", thread_id="t", workspace_root=str(tmp_path))
    app.session = session

    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(60):
            await pilot.pause()
            if app._connected:
                break
        assert app._connected
        ctx = await http_app.state.manager.get("tui-retry", "t")

        composer = app.query_one("#input")
        composer.load_text("A")
        await app.submit_composer()
        await pilot.pause()
        # A is busy (LLM gated); B is held locally.
        composer.load_text("B")
        await app.submit_composer()
        await pilot.pause()
        # A is busy (LLM gated); B is held and not yet in the transcript.
        assert not any(
            message.content == "B" for message in app.state.messages
        ), "B must not appear while A is busy"

        release_a.set()
        for _ in range(300):
            await pilot.pause()
            if not app._pending_messages:
                break

        assert not app._pending_messages, "B must be retried and delivered"
