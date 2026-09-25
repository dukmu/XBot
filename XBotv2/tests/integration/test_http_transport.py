"""End-to-end integration tests for the HTTP/SSE transport.

These tests build a FastAPI app with a ``MockLLM`` injected, then drive
it via ``httpx.AsyncClient`` + ``ASGITransport`` (no real socket).
The tests cover:

- /health round-trip
- /hello + /sessions handshake
- shared GET /events stream with a real engine
- live permission_request round-trip via the interaction endpoints
- Chinese payload byte-level preservation through HTTP
- ESC interrupt: POST /sessions/{sid}/interrupt mid-turn yields
  ``turn_cancelled`` on the SSE stream (v1.2)

See ``XBotv2/docs/http-api.md`` and the bundled
``xbot-plugin-development`` skill for the current transport and client
contract.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import XBotv2.client as client_module
import yaml
from openai.types.chat import ChatCompletionChunk
from pydantic import ValidationError
from XBotv2.jobs import Running
from XBotv2.jobs.protocol import JobCompletedEvent
from XBotv2.core.paths import RuntimePaths
from XBotv2.agentloop import AllTools
from XBotv2.core.tools import Tool, ToolCall, ToolCancelled, ToolDenied, ToolSucceeded
from XBotv2.client import XBotClient, XBotClientError
from XBotv2.coretools.shell import ShellJobSpec, ShellRunner, start_shell
from httpx import ASGITransport

from XBotv2.llm.mock import MockLLM
from XBotv2.llm.openai import OpenAICompatibleProvider
from XBotv2.interactions.protocol import Answered
from XBotv2.application import RuntimeEvent
from XBotv2.application.app import create_agent_application
from XBotv2.application.server import start_server_application
from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.session import (
    InteractionReceipt,
    OpenSession,
    SendMessage,
)
from XBotv2.session.contracts import SessionEvent
from XBotv2.session.contracts import SessionResourceChanged
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    LoopError,
    LoopTurnEnded,
    LoopTurnStarted,
)
from XBotv2.agentloop.contracts import InboxItem, InboxTarget, RuntimeInput
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderUser
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.domain import TokenCounters, TurnScope, UsageSnapshot
from XBotv2.session.protocol import (
    InputAcceptedEvent,
    InputClaimedEvent,
    InputConsumedEvent,
    MessagePublishedEvent,
    QueueReplacedEvent,
)
from XBotv2.session.records import (
    AssistantRecord,
    HumanInputRecord,
    ToolRecord,
    project_message,
)
from XBotv2.core.messages import AssistantMessage, HumanInputMessage
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.interactions import ClientNotice
from XBotv2.session.runtime import (
    TurnEventRouter,
)
from XBotv2.session.event_stream import SessionEventStream
from XBotv2.usage import UsageUpdated


SSE_DATA_RE = re.compile(r"^data: ?(.*)$", re.MULTILINE)


async def _drain_stream(stream):
    return [event async for event in stream]


def _ends_turn(event: SessionEvent) -> bool:
    return isinstance(event, LoopTurnEnded) or (
        isinstance(event, LoopError) and event.code == "turn_failed"
    )


async def _submit_turn(
    client: httpx.AsyncClient,
    app,
    session_id: str,
    thread_id: str,
    payload: dict[str, Any],
) -> list[SessionEvent]:
    """Submit one message and wait for its turn to terminate.

    The message endpoints only acknowledge the command; every frame arrives on
    the shared session event stream, so the subscription opens before the POST.
    """
    runtime = await app.state.manager.get(session_id, thread_id)
    events = runtime.event_stream.subscribe()
    collected: list[SessionEvent] = []
    try:
        response = await client.post(
            f"/sessions/{session_id}/threads/{thread_id}/messages",
            json=payload,
        )
        assert response.status_code == 202, response.text
        async with asyncio.timeout(5):
            async for frame in events:
                collected.append(frame.event)
                if _ends_turn(frame.event):
                    break
    finally:
        await events.aclose()
    return collected


async def _await_turn_end(events, *, timeout: float = 5.0) -> None:
    """Consume the shared stream until one turn reaches a terminal frame."""
    async with asyncio.timeout(timeout):
        async for frame in events:
            if _ends_turn(frame.event):
                return


def _runtime_command(runtime, content: str, request_id: str, *, delivery="steer"):
    """Issue a runtime command while consuming the central event stream."""
    async def stream():
        events = runtime.event_stream.subscribe()
        try:
            await runtime.send_message(
                content, request_id, delivery=delivery,
            )
            async for frame in events:
                yield frame.event
                if _ends_turn(frame.event):
                    break
        finally:
            await events.aclose()
    return stream()


async def _wait_for_pending_input_count(
    runtime,
    expected: int,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while runtime.engine.pending_input_count != expected:
            await asyncio.sleep(0)


async def _collect_turn(runtime, content: str, request_id: str, *, delivery="steer"):
    """Submit a runtime command and collect its central event frames."""
    events = runtime.event_stream.subscribe()
    await runtime.send_message(content, request_id, delivery=delivery)
    collected = []
    async for frame in events:
        collected.append(frame.event)
        if _ends_turn(frame.event):
            break
    await events.aclose()
    return collected


async def _collect_sdk_turn(stream):
    events = []
    async for event in stream:
        events.append(event)
        if _ends_turn(event):
            break
    return events


async def _wait_for_sdk_messages(sdk, session_id: str, thread_id: str, count: int):
    async with asyncio.timeout(3):
        while True:
            page = await sdk.list_messages(session_id, thread_id)
            if len(page.items) >= count:
                return page
            await asyncio.sleep(0)


async def _start_background_shell(application: Any, command: str) -> str:
    spec = ShellJobSpec(
        command=command,
        cwd=None,
        escalated=False,
        label=command,
    )
    job = await application.jobs.create(spec=spec, owner="integration-test")
    application.jobs.start(
        job.id,
        ShellRunner(spec, artifacts=application.artifacts),
    )
    return job.id


@pytest.mark.asyncio
async def test_python_sdk_uses_typed_resources_and_events(http_app) -> None:
    assert client_module.XBotClient is XBotClient
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(
            responses=[
                {"content": "sdk answer"},
                {"content": "sdk regenerated"},
            ]
        ),
    )
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        health = await sdk.health()
        opened = await sdk.open_session(
            session_id="sdk-client",
            thread_id="main",
        )
        await sdk.send_message(
            "sdk-client", "main", "sdk question",
            request_id="sdk-request", delivery="queue",
        )
        messages = await _wait_for_sdk_messages(sdk, "sdk-client", "main", 2)
        pending = await sdk.list_pending_inputs("sdk-client", "main")
        messages = await sdk.list_messages("sdk-client", "main")
        latest = await sdk.list_messages("sdk-client", "main", limit=1)
        older = await sdk.list_messages(
            "sdk-client", "main", limit=1, cursor=latest.older_cursor
        )
        trajectory = await sdk.list_trajectory("sdk-client", "main")
        # A windowed client anchors its next page on the oldest position it
        # still holds, and reads the tail from the same response.
        anchored = await sdk.list_trajectory(
            "sdk-client",
            "main",
            limit=1,
            before=trajectory.newest_position,
        )
        await sdk.regenerate_message(
            "sdk-client", "main", request_id="sdk-regenerate"
        )
        regenerated_messages = await _wait_for_sdk_messages(sdk, "sdk-client", "main", 2)
        regenerated_messages = await sdk.list_messages("sdk-client", "main")
        undone = await sdk.undo_history("sdk-client", "main")

        assert health.status == "ok"
        assert pending.items == []
        assert trajectory.newest_position >= 1
        assert anchored.page.items
        assert anchored.newest_position == trajectory.newest_position
        assert anchored.page.items[-1].position <= trajectory.newest_position - 1
        assert opened.data.key.session_id == "sdk-client"
        assert any(
            item.content == "sdk answer" for item in messages.items
        )
        assert [item.content for item in messages.items] == [
            "sdk question",
            "sdk answer",
        ]
        assert [item.content for item in latest.items] == ["sdk answer"]
        assert [item.content for item in older.items] == ["sdk question"]
        assert [item.kind for item in trajectory.page.items] == [
            "message_appended", "message_appended",
        ]
        assert [item.content for item in regenerated_messages.items] == [
            "sdk question", "sdk regenerated",
        ]
        assert undone.removed_turns == 1
        assert undone.history.items == ()
        # The command plane is part of the SDK now: one client procedure for
        # every client, instead of each re-implementing the HTTP call.
        assert hasattr(sdk, "list_commands") and hasattr(sdk, "run_command")

        with pytest.raises(XBotClientError) as raised:
            await sdk.get_thread("sdk-client", "missing")
        assert raised.value.status_code == 404
        assert raised.value.code == "session_not_found"
        assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_python_sdk_submit_uses_only_the_shared_session_stream(http_app) -> None:
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(
            responses=[
                {"content": "submitted answer"},
                {"content": "regenerated answer"},
            ]
        ),
    )
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        opened = await sdk.open_session(
            session_id="sdk-submit",
            thread_id="main",
        )
        await sdk.send_message(
            "sdk-submit", "main", "submitted question",
            request_id="submit-request",
        )
        first_messages = await _wait_for_sdk_messages(sdk, "sdk-submit", "main", 2)

        await sdk.regenerate_message(
            "sdk-submit",
            "main",
            request_id="submit-regenerate",
        )
        second_messages = await _wait_for_sdk_messages(sdk, "sdk-submit", "main", 2)
    assert [item.content for item in first_messages.items] == [
        "submitted question", "submitted answer",
    ]
    assert [item.content for item in second_messages.items] == [
        "submitted question", "regenerated answer",
    ]


@pytest.mark.asyncio
async def test_python_sdk_uploads_attachment_as_session_artifact(http_app) -> None:
    llm = MockLLM(responses=[{"content": "attachment received"}])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        opened = await sdk.open_session(session_id="sdk-attachment", thread_id="main")
        await sdk.send_message(
            "sdk-attachment",
            "main",
            "inspect this",
            attachments=[{
                "name": "sample.bin",
                "media_type": "application/octet-stream",
                "data": "YmluYXJ5",
            }],
        )
        messages = await _wait_for_sdk_messages(sdk, "sdk-attachment", "main", 2)
        downloaded = await sdk.read_artifact(
            "sdk-attachment",
            "main",
            messages.items[0].artifacts[0].id,
        )

    assert downloaded == b"binary"
    artifact = messages.items[0].artifacts[0]
    assert artifact.name == "sample.bin"
    assert not artifact.id.startswith("/")
    runtime = await http_app.state.manager.get("sdk-attachment", "main")
    model_path = runtime.application.artifacts.model_path(artifact)
    user = next(
        message for message in llm.request_history[0].messages
        if isinstance(message, ProviderUser)
    )
    user_text = "".join(part.text for part in user.parts if isinstance(part, TextPart))
    assert f'path="{model_path}"' in user_text
    assert 'name="sample.bin"' in user_text


@pytest.mark.asyncio
async def test_message_pages_artifact_download_and_regenerate_are_authoritative(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    llm = MockLLM(responses=[
        {"content": "first answer"},
        {"content": "second answer"},
        {"content": "regenerated answer"},
    ])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    await client.post(
        "/sessions", json={"session_id": "message-api", "thread_id": "main"}
    )
    await _submit_turn(client, http_app, "message-api", "main", {
        "content": "first question",
        "attachments": [{
            "name": "context.txt",
            "media_type": "text/plain",
            "data": "Y29udGV4dA==",
        }],
    })
    await _submit_turn(client, http_app, "message-api", "main", {
        "content": "second question",
        "attachments": [{
            "name": "latest.txt",
            "media_type": "text/plain",
            "data": "bGF0ZXN0",
        }],
    })

    reopened = await client.post(
        "/sessions",
        json={
            "session_id": "message-api",
            "thread_id": "main",
            "mode": "resume",
            "history_limit": 2,
        },
    )
    opened_data = reopened.json()["data"]
    assert [item["content"] for item in opened_data["history"]["items"]] == [
        "second question", "second answer",
    ]
    assert opened_data["history"]["older_cursor"]

    latest = await client.get(
        "/sessions/message-api/threads/main/messages", params={"limit": 2}
    )
    assert [item["content"] for item in latest.json()["items"]] == [
        "second question", "second answer",
    ]
    assert latest.json()["older_cursor"] == opened_data["history"]["older_cursor"]

    older = await client.get(
        "/sessions/message-api/threads/main/messages",
        params={"limit": 2, "cursor": latest.json()["older_cursor"]},
    )
    assert [item["content"] for item in older.json()["items"]] == [
        "first question", "first answer",
    ]
    assert older.json()["older_cursor"] is None

    artifact = older.json()["items"][0]["artifacts"][0]
    downloaded = await client.get(
        "/sessions/message-api/threads/main/artifacts/" + artifact["id"]
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"context"
    assert downloaded.headers["content-type"].startswith("text/plain")
    assert "context.txt" in downloaded.headers["content-disposition"]

    events = await http_app.state.manager.stream_events(
        "message-api", "main", after=opened_data["event_cursor"]
    )
    regenerated = await client.post(
        "/sessions/message-api/threads/main/history/regenerate",
        json={"request_id": "regen-1"},
    )
    assert regenerated.status_code == 202
    async with asyncio.timeout(2):
        async for frame in events:
            if isinstance(frame.event, LoopTurnEnded):
                break
    await events.aclose()

    stale_page = await client.get(
        "/sessions/message-api/threads/main/messages",
        params={"limit": 2, "cursor": latest.json()["older_cursor"]},
    )
    assert stale_page.status_code == 400
    assert stale_page.json()["code"] == "invalid_cursor"

    current = await client.get("/sessions/message-api/threads/main/messages")
    assert [item["content"] for item in current.json()["items"]] == [
        "first question",
        "first answer",
        "second question",
        "regenerated answer",
    ]
    runtime = await http_app.state.manager.get("message-api", "main")
    latest_artifact = latest.json()["items"][0]["artifacts"][0]
    latest_model_path = runtime.application.artifacts.model_path(
        ArtifactRef.model_validate(latest_artifact)
    )
    regenerated_text = "\n".join(
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in llm.request_history[-1].messages
        if isinstance(message, ProviderUser)
    )
    assert "second question" in regenerated_text
    assert latest_model_path in regenerated_text
    assert 'name="latest.txt"' in regenerated_text


@pytest.mark.asyncio
async def test_message_cursor_rejects_invalid_positions(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/sessions", json={"session_id": "bad-cursor", "thread_id": "main"}
    )
    response = await client.get(
        "/sessions/bad-cursor/threads/main/messages",
        params={"limit": 10, "cursor": "not-a-cursor"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_session_policy_api_persists_reloads_and_preserves_rules(http_app) -> None:
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        await sdk.open_session(session_id="sdk-policy", thread_id="main")
        policy_path = http_app.state.paths.session("sdk-policy").config_file
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            yaml.safe_dump({
                "plugins": [
                    {
                        "id": "permissions",
                        "config": {
                            "default_decision": "ask",
                            "rules": [{
                                "tool_pattern": "read",
                                "param_patterns": {},
                                "path_scope": None,
                                "decision": "allow",
                            }],
                        },
                    },
                    {
                        "id": "sandbox",
                        "config": {
                            "resources": [{"path": "/tmp/approved", "access": "readwrite"}],
                        },
                    },
                ],
            }),
            encoding="utf-8",
        )

        updated = await sdk.update_session_policy(
            "sdk-policy",
            permissions={"shell": "allow", "edit": "deny"},
            sandbox={"network": False, "external_write": "deny"},
        )
        ctx = await http_app.state.manager.get("sdk-policy", "main")
        sandbox = ctx.application._context.sandbox

        assert [
            (rule.tool_pattern, rule.decision)
            for rule in updated.permissions.rules
        ] == [("edit", "deny"), ("shell", "allow"), ("read", "allow")]
        assert updated.sandbox.resources[0].path == "/tmp/approved"
        assert updated.sandbox.resources[0].access == "readwrite"
        assert ctx.application._context.permissions.check("shell") == "allow"
        assert ctx.application._context.permissions.check(
            "edit", {"path": "a.txt", "mode": "write"}
        ) == "deny"
        assert ctx.application._context.sandbox.network is False
        assert ctx.application._context.sandbox.external_write == "deny"
        assert ctx.application._context.sandbox is sandbox
        assert ctx.application._context.jobs is not None
        assert ctx.application._context.tools._registry.get("shell") is not None

        cleared = await sdk.update_session_policy(
            "sdk-policy",
            remove_permissions=["shell", "edit"],
            remove_sandbox=["network"],
        )
        assert [rule.tool_pattern for rule in cleared.permissions.rules] == ["read"]
        assert cleared.sandbox.network is True
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
async def test_session_close_cancels_turn_before_closing_engine(http_app) -> None:
    runtime = await http_app.state.manager.open_session(
        session_id="closing",
        thread_id="main",
        provider_name="default",
        workspace_root=str(http_app.state.workspace_root),
        no_plugins=True,
        llm_override=MockLLM(responses=[{
            "chunks": ["partial", "completion"],
            "chunk_delay_ms": 500,
        }]),
    )
    await runtime.send_message("wait", "close-request")
    task = runtime.turn_task
    assert task is not None

    await runtime.close()

    assert task.cancelled()
    assert runtime.turn_task is None
    assert runtime.application.loop_state.session.status == "closed"
    assert not runtime.turn_lock.locked()


@pytest.mark.asyncio
async def test_closing_turn_stream_keeps_session_owned_turn_running(http_app) -> None:
    runtime = await http_app.state.manager.open_session(
        session_id="disconnect",
        thread_id="main",
        provider_name="default",
        workspace_root=str(http_app.state.workspace_root),
        no_plugins=True,
        llm_override=MockLLM(responses=[{
            "chunks": ["partial", "completion"],
            "chunk_delay_ms": 500,
        }]),
    )
    stream = runtime.event_stream.subscribe()
    await runtime.send_message("wait", "disconnect-request")
    task = runtime.turn_task
    assert task is not None
    await stream.aclose()

    assert not task.cancelled()
    assert runtime.turn_task is task
    assert runtime.turn_lock.locked()

    await runtime.close()

    assert task.cancelled()
    assert runtime.turn_task is None
    assert not runtime.turn_lock.locked()


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
                    "default_provider": "default",
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
                                    "max_output_tokens": 1024,
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
                "config": {"enabled": False, "resources": []},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "default_decision": "allow",
                    "rules": [
                        {"tool_pattern": "ask_user", "decision": "ask"},
                        {"tool_pattern": "request_permission", "decision": "ask"},
                        {"tool_pattern": "edit", "decision": "ask"},
                    ],
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
    # Integration setup needs runtime capabilities that HTTP consumers reach
    # only through routes. Keep these test-only handles out of the carrier.
    app.state.test_context = server
    app.state.manager = server.sessions
    app.state.paths = server.runtime_paths
    app.state.workspace_root = server.workspace_root
    # Inject a mock LLM that returns one canned response per turn.
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "hello from mock"}]),
    )
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


@pytest_asyncio.fixture
async def full_http_app(http_app):
    """HTTP app whose Agent sessions include optional built-in plugins."""
    server = await start_server_application(
        provider_name="default",
        paths=http_app.state.paths,
        workspace_root=str(http_app.state.workspace_root),
        no_plugins=False,
    )
    server.server.state.manager = server.sessions
    try:
        yield server.server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def full_client(full_http_app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=full_http_app), base_url="http://test"
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
async def test_blank_session_projection_tracks_turn_and_clear(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    opened = await client.post(
        "/sessions",
        json={"session_id": "blank-summary", "thread_id": "main"},
    )
    assert opened.status_code == 200
    listed = (await client.get("/sessions")).json()["sessions"]
    assert all(item["session_id"] != "blank-summary" for item in listed)
    workspace_listing = (await client.get("/workspaces")).json()["items"]
    assert all("blank-summary" not in item["session_ids"] for item in workspace_listing)

    workspace_events = http_app.state.test_context.workspace_events
    subscription = workspace_events.subscribe(workspace_events.sequence)
    await _submit_turn(client, http_app, "blank-summary", "main", {
        "request_id": "blank-turn",
        "content": "engage this session",
    })
    changes = [
        (await asyncio.wait_for(anext(subscription), timeout=1)).change,
        (await asyncio.wait_for(anext(subscription), timeout=1)).change,
    ]
    engaged = next(
        change for change in changes
        if isinstance(change, SessionResourceChanged)
    )
    assert engaged.session.blank is False
    assert engaged.added is True
    listed = (await client.get("/sessions")).json()["sessions"]
    assert next(item for item in listed if item["session_id"] == "blank-summary")["blank"] is False

    cleared = await client.post(
        "/sessions/blank-summary/threads/main/history/clear",
    )
    assert cleared.status_code == 200
    blank_again = await asyncio.wait_for(anext(subscription), timeout=1)
    assert isinstance(blank_again.change, SessionResourceChanged)
    assert blank_again.change.session.blank is True
    await subscription.aclose()


@pytest.mark.asyncio
async def test_resumed_history_preserves_assistant_reasoning(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{
            "reasoning": "inspect persisted context",
            "content": "restored answer",
        }]),
    )
    await client.post(
        "/sessions",
        json={"session_id": "reasoning-resume", "thread_id": "main"},
    )
    await _submit_turn(client, http_app, "reasoning-resume", "main", {
        "request_id": "reasoning-turn",
        "content": "reason first",
    })
    closed = await client.post("/sessions/reasoning-resume/close")
    assert closed.status_code == 200

    resumed = await client.post(
        "/sessions",
        json={
            "session_id": "reasoning-resume",
            "thread_id": "main",
            "mode": "resume",
        },
    )

    assert resumed.status_code == 200
    assistant = next(
        item for item in resumed.json()["data"]["history"]["items"]
        if item["kind"] == "assistant"
    )
    assert assistant["content"] == "restored answer"
    assert assistant["reasoning"] == "inspect persisted context"


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
    opened = response.json()["data"]
    selection = opened["metadata"]["runtime_selection"]
    assert opened["key"]["session_id"] == "s1"
    assert selection["agent_name"]
    assert selection["model"]["route"]["model"] == "test"
    assert selection["model"]["context_window"] == 4096

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
                "max_output_tokens": 1024,
                "reasoning_effort": "",
                "effort": [],
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
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[
            {"content": "main reply"},
            {"content": "child reply"},
        ]),
    )
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
        "workspace_root": str(http_app.state.workspace_root),
        "title": "thread-resources",
        "blank": True,
        "unreadable": False,
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

    await _submit_turn(client, http_app, "thread-resources", "agent", {
        "content": "main message",
    })
    await _submit_turn(client, http_app, "thread-resources", "child", {
        "content": "child message",
    })
    main_messages = (
        await client.get(
            "/sessions/thread-resources/threads/agent/messages"
        )
    ).json()["items"]
    child_messages = (
        await client.get(
            "/sessions/thread-resources/threads/child/messages"
        )
    ).json()["items"]
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
        "/sessions/thread-resources/threads/child/jobs"
    )
    assert inactive_tasks.status_code == 409
    assert inactive_tasks.json()["code"] == "thread_not_active"


@pytest.mark.asyncio
async def test_todo_and_usage_survive_http_close_resume(
    full_client: httpx.AsyncClient,
    full_http_app,
) -> None:
    from XBotv2.todolist.contracts import TaskChanged

    client = full_client

    # Exercise the production OpenAI adapter with actual typed SDK chunks, then
    # carry its normalized usage through the HTTP/runtime/persistence lifecycle.
    sdk_responses = [
        {"content": "session title"},
        {
            "content": "Planning.",
            "tool_calls": [{
                "id": "todo-create",
                "name": "task_create",
                "args": {
                    "subject": "implement",
                    "active_form": "Implementing",
                },
            }],
            "usage": {
                "prompt_tokens": 13,
                "completion_tokens": 2,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 1},
                "cache_creation_input_tokens": 2,
                "prompt_cache_write_tokens": 3,
            },
        },
        {
            "content": "Plan saved.",
            "usage": {
                "prompt_tokens": 21,
                "completion_tokens": 3,
                "total_tokens": 24,
                "prompt_tokens_details": {"cached_tokens": 4},
                "cache_creation_input_tokens": 5,
                "prompt_cache_write_tokens": 6,
            },
        },
        {
            "content": "Finishing.",
            "tool_calls": [{
                "id": "todo-complete",
                "name": "task_update",
                "args": {"taskId": "1", "status": "completed"},
            }],
            "usage": {
                "prompt_tokens": 29,
                "completion_tokens": 2,
                "total_tokens": 31,
                "prompt_tokens_details": {"cached_tokens": 7},
                "cache_creation_input_tokens": 8,
                "prompt_cache_write_tokens": 9,
            },
        },
        {
            "content": "Checklist complete.",
            "usage": {
                "prompt_tokens": 37,
                "completion_tokens": 3,
                "total_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 10},
                "cache_creation_input_tokens": 11,
                "prompt_cache_write_tokens": 12,
            },
        },
    ]

    class SDKCompletions:
        def __init__(self):
            self.responses = iter(sdk_responses)

        async def create(self, **_kwargs):
            response = next(self.responses)

            def chunk(*, choices, usage=None):
                payload = {
                    "id": "completion-1",
                    "choices": choices,
                    "created": 1,
                    "model": "test-model",
                    "object": "chat.completion.chunk",
                }
                if usage is not None:
                    payload["usage"] = usage
                return ChatCompletionChunk.model_validate(payload)

            delta = {"content": response["content"]}
            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                delta["tool_calls"] = [{
                    "index": index,
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["args"]),
                    },
                } for index, call in enumerate(tool_calls)]
            finish_reason = "tool_calls" if tool_calls else "stop"
            async def chunks():
                yield chunk(choices=[{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }])
                if "usage" in response:
                    yield chunk(choices=[], usage=response["usage"])

            return chunks()

    sdk_provider = OpenAICompatibleProvider(api_key="test", base_url=None)
    sdk_provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SDKCompletions())
    )
    full_http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=sdk_provider,
    )
    opened = await client.post(
        "/sessions", json={"session_id": "todo-recovery", "thread_id": "main"}
    )
    assert opened.status_code == 200

    first_turn_events = await _submit_turn(
        client,
        full_http_app,
        "todo-recovery",
        "main",
        {"content": "make a plan"},
    )
    created_events = [
        event for event in first_turn_events if isinstance(event, TaskChanged)
    ]
    assert len(created_events) == 1
    assert created_events[0].snapshot.version == 1
    assert [task.subject for task in created_events[0].snapshot.tasks] == [
        "implement"
    ]
    runtime = await full_http_app.state.manager.get("todo-recovery", "main")
    application_snapshot = await runtime.application.snapshot()
    assistant_messages = [
        message
        for message in application_snapshot.messages
        if isinstance(message, AssistantMessage)
    ]
    assert [message.exchange.usage.counters for message in assistant_messages] == [
        TokenCounters(
            input=10,
            output=2,
            cache_read=1,
            cache_create=2,
            prompt_cache_write=3,
        ),
        TokenCounters(
            input=12,
            output=3,
            cache_read=4,
            cache_create=5,
            prompt_cache_write=6,
        ),
    ]
    usage_updates = [
        event for event in first_turn_events if isinstance(event, UsageUpdated)
    ]
    assert usage_updates
    active = (
        await client.get("/sessions/todo-recovery/threads/main")
    ).json()
    usage = active["usage"]
    assert usage["total_counters"]["input"] == 22
    assert usage["total_counters"]["output"] == 5
    assert usage["total_counters"]["cache_read"] == 5
    assert usage["total_counters"]["cache_create"] == 7
    assert usage["total_counters"]["prompt_cache_write"] == 9
    assert application_snapshot.usage == UsageSnapshot.model_validate(usage)
    assert usage_updates[-1].snapshot == application_snapshot.usage
    assert [request["purpose"]["kind"] for request in usage["requests"]].count(
        "turn"
    ) == 2
    assert [request["purpose"]["kind"] for request in usage["requests"]].count(
        "auxiliary"
    ) == 1
    assert usage["latest_turn_observation"]["observed_context"] == {
        "kind": "provider_measured",
        "tokens": 21,
    }
    messages = (
        await client.get("/sessions/todo-recovery/threads/main/messages")
    ).json()["items"]
    todo_tool_results = [
        "".join(part["text"] for part in item["outcome"]["output"]["parts"])
        for item in messages
        if item["kind"] == "tool"
        and item["call"]["name"] == "task_create"
    ]
    assert todo_tool_results == ["Created task #1: implement"]
    todo_state = await client.get(
        "/sessions/todo-recovery/threads/main/todos"
    )
    assert todo_state.status_code == 200
    created_tasks = todo_state.json()["tasks"]
    assert created_tasks[0]["subject"] == "implement"
    assert created_tasks[0]["status"] == "pending"
    assert created_tasks[0]["activeForm"] == "Implementing"

    closed = await client.post(
        "/sessions/todo-recovery/threads/main/close"
    )
    assert closed.status_code == 200
    inactive = (
        await client.get("/sessions/todo-recovery/threads/main")
    ).json()
    assert inactive["status"] == "inactive"
    assert inactive["usage"] == active["usage"]

    resumed = await client.post(
        "/sessions",
        json={
            "session_id": "todo-recovery",
            "thread_id": "main",
            "mode": "resume",
        },
    )
    assert resumed.status_code == 200
    assert any(
        item["kind"] == "tool"
        and item["call"]["name"] == "task_create"
        and "Created task #1: implement" in "".join(
            part["text"] for part in item["outcome"]["output"]["parts"]
        )
        for item in resumed.json()["data"]["history"]["items"]
    )
    resumed_todos = await client.get(
        "/sessions/todo-recovery/threads/main/todos"
    )
    assert resumed_todos.json()["tasks"] == created_tasks

    second_turn_events = await _submit_turn(
        client,
        full_http_app,
        "todo-recovery",
        "main",
        {"content": "finish it"},
    )
    completed_events = [
        event for event in second_turn_events if isinstance(event, TaskChanged)
    ]
    assert len(completed_events) == 1
    assert completed_events[0].snapshot.version == 2
    assert completed_events[0].snapshot.tasks[0].status == "completed"
    final_updates = [
        event for event in second_turn_events if isinstance(event, UsageUpdated)
    ]
    assert final_updates
    final_usage = (
        await client.get("/sessions/todo-recovery/threads/main")
    ).json()["usage"]
    expected_counters = TokenCounters(
        input=52,
        output=10,
        cache_read=22,
        cache_create=26,
        prompt_cache_write=30,
    )
    assert (
        UsageSnapshot.model_validate(final_usage).total_counters
        == expected_counters
    )
    assert final_updates[-1].snapshot.total_counters == expected_counters
    final_messages = (
        await client.get("/sessions/todo-recovery/threads/main/messages")
    ).json()["items"]
    projections = [
        "".join(part["text"] for part in message["outcome"]["output"]["parts"])
        for message in final_messages
        if message["kind"] == "tool"
        and message["call"]["name"] == "task_update"
    ]
    assert "Updated task #1" in projections[-1]
    assert (
        await client.get("/sessions/todo-recovery/threads/main/todos")
    ).json()["tasks"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_idle_runtime_is_reaped_after_timeout(http_app) -> None:
    manager = http_app.state.manager
    workspace_root = http_app.state.workspace_root
    await manager.close_all()
    # Start reaping only after the runtime has opened, so startup latency cannot
    # race the assertion that it was registered.
    manager.idle_timeout = 3600.0
    manager.reap_interval = 0.02
    runtime = await manager.open_session(
        session_id="idle-reap", thread_id="agent", provider_name="default",
        workspace_root=str(workspace_root), no_plugins=True,
        llm_override=MockLLM(responses=[{"content": "hi"}]),
    )
    assert await manager.get("idle-reap", "agent") is not None
    manager.idle_timeout = 0.05
    runtime.touch()
    manager.start_reaper()
    await asyncio.sleep(0.2)
    assert ("idle-reap", "agent") not in await manager.active_threads()
    assert not manager.session_exists("idle-reap")
    # restore defaults so other tests are unaffected
    manager.idle_timeout = 3600.0
    manager.reap_interval = 60.0


@pytest.mark.asyncio
async def test_open_event_cursor_replays_later_shared_runtime_events(http_app) -> None:
    manager = http_app.state.manager
    opened = await manager.open(OpenSession(
        session_id="runtime-event-replay",
        thread_id="agent",
        provider_name="default",
        workspace_root=str(http_app.state.workspace_root),
        mode="new",
        no_plugins=True,
        model_override=MockLLM(responses=[]),
    ))
    runtime = await manager.get(opened.key.session_id, opened.key.thread_id)
    runtime._on_runtime_event(RuntimeEvent(event=ClientNotice(
        message="Task task-1 completed",
        source="test",
    )))

    events = await manager.stream_events(
        opened.key.session_id,
        opened.key.thread_id,
        after=opened.event_cursor,
    )
    frame = await asyncio.wait_for(anext(events), timeout=1)

    assert frame.sequence == opened.event_cursor + 1
    assert frame.event == ClientNotice(
        message="Task task-1 completed",
        source="test",
    )
    await events.aclose()


@pytest.mark.asyncio
async def test_main_turn_uses_the_resumable_session_event_sequence(http_app) -> None:
    manager = http_app.state.manager
    opened = await manager.open(OpenSession(
        session_id="main-turn-replay",
        thread_id="agent",
        provider_name="default",
        workspace_root=str(http_app.state.workspace_root),
        mode="new",
        no_plugins=True,
        model_override=MockLLM(responses=[{"content": "shared reply"}]),
    ))
    shared = await manager.stream_events(
        opened.key.session_id,
        opened.key.thread_id,
        after=opened.event_cursor,
    )
    runtime = await manager.get(opened.key.session_id, opened.key.thread_id)
    await runtime.send_message("hello", "shared-request")

    frames = []
    async with asyncio.timeout(1):
        async for frame in shared:
            frames.append(frame)
            if isinstance(frame.event, LoopTurnEnded):
                break

    events = [frame.event for frame in frames]
    assert sum(isinstance(event, InputAcceptedEvent) for event in events) == 1
    published_inputs = [
        event.record.root for event in events
        if isinstance(event, MessagePublishedEvent)
    ]
    assert [(record.content, record.id) for record in published_inputs] == [
        ("hello", "shared-request"),
    ]
    assert sum(isinstance(event, LoopTurnStarted) for event in events) == 1
    assert sum(isinstance(event, AssistantCompleted) for event in events) == 1
    terminals = [event for event in events if isinstance(event, LoopTurnEnded)]
    assert len(terminals) == 1
    assert all(isinstance(frame.scope, TurnScope) for frame in frames if isinstance(
        frame.event, (LoopTurnStarted, AssistantCompleted, LoopTurnEnded)
    ))
    assert [frame.sequence for frame in frames] == list(range(
        opened.event_cursor + 1,
        opened.event_cursor + len(frames) + 1,
    ))
    await shared.aclose()


@pytest.mark.asyncio
async def test_session_event_endpoint_rejects_expired_and_future_cursors(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    opened = await client.post(
        "/sessions",
        json={"session_id": "event-cursor-errors", "thread_id": "agent"},
    )
    runtime = await http_app.state.manager.get("event-cursor-errors", "agent")
    for index in range(513):
        runtime._on_runtime_event(RuntimeEvent(event=ClientNotice(
            message=f"Task task-{index} completed",
            source="test",
        )))

    expired = await client.get(
        "/sessions/event-cursor-errors/threads/agent/events",
        params={"after": opened.json()["data"]["event_cursor"]},
    )
    future = await client.get(
        "/sessions/event-cursor-errors/threads/agent/events",
        params={"after": runtime.event_stream.sequence + 1},
    )

    assert expired.status_code == 409
    assert expired.json()["code"] == "session_event_cursor_expired"
    assert expired.json()["retryable"] is True
    assert future.status_code == 400
    assert future.json()["code"] == "invalid_session_event_cursor"

    # The SDK stream path must surface that 409 as a typed error instead of an
    # httpx ResponseNotRead raised from the unread streaming body.
    async with XBotClient(
        "http://test",
        transport=ASGITransport(app=http_app),
    ) as sdk:
        with pytest.raises(XBotClientError) as raised:
            async for _event in sdk.stream_events(
                "event-cursor-errors",
                "agent",
                after=opened.json()["data"]["event_cursor"],
            ):
                pass
    assert raised.value.status_code == 409
    assert raised.value.code == "session_event_cursor_expired"
    assert int(raised.value.details["oldest_sequence"]) > 0


@pytest.mark.asyncio
async def test_session_open_rejects_a_missing_workspace_before_registration(
    client: httpx.AsyncClient,
    http_app,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-workspace"
    response = await client.post(
        "/sessions",
        json={
            "session_id": "missing-workspace",
            "thread_id": "agent",
            "workspace_root": str(missing),
        },
    )

    assert response.status_code == 404
    assert response.json()["code"] == "workspace_not_found"
    assert not http_app.state.manager.session_exists("missing-workspace")


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
        "tool_policy:\n  enabled: []\n"
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
    app.state.manager = server.sessions
    app.state.paths = server.runtime_paths
    app.state.workspace_root = server.workspace_root
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[]),
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
    assert opened.json()["data"]["metadata"]["runtime_selection"]["agent_name"] == "builder"
    assert agents.json()["active"] == "builder"
    assert any(
        item["name"] == "builder" for item in agents.json()["agents"]
    )
    assert resumed.status_code == 200
    assert resumed.json()["data"]["metadata"]["runtime_selection"]["agent_name"] == "builder"
    await server.stop()


@pytest.mark.asyncio
async def test_http_switches_primary_agent_without_replacing_thread_history(
    http_app, tmp_path: Path
) -> None:
    workspace = tmp_path / "switch-agent-workspace"
    agents_dir = workspace / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        "---\ndescription: Build changes\nmode: primary\n"
        "tool_policy:\n  enabled: []\n---\nBuild.",
        encoding="utf-8",
    )
    explorer_path = agents_dir / "Explorer.md"
    explorer_path.write_text(
        "---\n"
        "description: Read-only exploration\n"
        "mode: all\n"
        "model_policy:\n"
        "  route:\n    provider: default\n    model: test\n"
        "  context_window: 64000\n"
        "tool_policy:\n  enabled:\n    - read\n"
        "permission_policy:\n  default_decision: deny\n  rules:\n"
        "    - tool_pattern: read\n      decision: allow\n"
        "    - tool_pattern: edit\n      decision: deny\n"
        "    - tool_pattern: shell\n      decision: deny\n"
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
    app.state.manager = server.sessions
    app.state.paths = server.runtime_paths
    app.state.workspace_root = server.workspace_root
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[
            {"content": "session title"},  # caption auto-titles the first message
            {"content": "existing answer"},
        ]),
    )
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
        await _submit_turn(ac, app, "switch-primary", "main", {
            "content": "keep this history",
        })
        switched = await ac.put(
            "/sessions/switch-primary/threads/main/agent",
            json={"name": "Explorer"},
        )
        ctx = await app.state.manager.get("switch-primary", "main")

        assert switched.status_code == 200
        assert switched.json()["agent"] == "Explorer"
        assert switched.json()["model"] == "test"
        assert switched.json()["context_window"] == 64000
        assert ctx.key.session_id == "switch-primary"
        assert ctx.key.thread_id == "main"
        snapshot = await ctx.application.snapshot()
        assert [
            record.content
            for record in map(project_message, snapshot.messages)
            if isinstance(record, (HumanInputRecord, AssistantRecord))
        ] == [
            "keep this history",
            "existing answer",
        ]
        assert ctx.application._context.tools._registry.get("read") is not None
        assert ctx.application._context.tools._registry.get("edit") is None
        selection = ctx.application.loop_state.metadata.value.runtime_selection
        assert selection.model.route.model == "test"
        assert selection.model.context_window == 64000
        # History was written before the switch, so the deferred metadata sink
        # flushed on that first record and the disk is authoritative again.
        assert ctx.application._context.thread_persistence.metadata.load().runtime_selection.agent_name == "Explorer"
        child_only = await ac.put(
            "/sessions/switch-primary/threads/main/agent",
            json={"name": "worker"},
        )
        assert child_only.status_code == 404
        assert child_only.json()["code"] == "agent_not_found"
        assert ctx.application.loop_state.metadata.value.runtime_selection.agent_name == "Explorer"

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
        assert ctx.application.loop_state.metadata.value.runtime_selection.agent_name == "Explorer"

        resumed = await ac.post(
            "/sessions",
            json={
                "session_id": "switch-primary",
                "thread_id": "main",
                "mode": "resume",
            },
        )

    assert resumed.status_code == 200
    resumed_data = resumed.json()["data"]
    selection = resumed_data["metadata"]["runtime_selection"]
    assert selection["agent_name"] == "Explorer"
    assert selection["model"]["route"]["model"] == "test"
    assert selection["model"]["context_window"] == 64000
    assert [item["content"] for item in resumed_data["history"]["items"]] == [
        "keep this history",
        "existing answer",
    ]
    await server.stop()


@pytest.mark.asyncio
async def test_http_open_session_without_id_creates_generated_session(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/sessions", json={"thread_id": "t1"})

    assert response.status_code == 200
    opened = response.json()["data"]
    assert opened["key"]["session_id"]
    assert "-" in opened["key"]["session_id"]


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
    assert response_a.json()["data"]["metadata"]["workspace_root"] == str(
        workspace_a.resolve()
    )
    assert response_b.json()["data"]["metadata"]["workspace_root"] == str(
        workspace_b.resolve()
    )


@pytest.mark.asyncio
async def test_http_session_listing_and_resume_preserve_main_thread_workspace(
    client: httpx.AsyncClient,
    http_app,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "history-workspace"
    workspace.mkdir()
    opened = await client.post(
        "/sessions",
        json={
            "session_id": "history-main",
            "thread_id": "main",
            "workspace_root": str(workspace),
        },
    )
    assert opened.status_code == 200

    sessions_before_input = await client.get("/sessions")
    assert "history-main" not in {
        item["session_id"] for item in sessions_before_input.json()["sessions"]
    }
    assert not RuntimePaths.from_data_dir(http_app.state.paths.data_dir).session(
        "history-main"
    ).root.exists()
    threads = await client.get("/sessions/history-main/threads")
    await _submit_turn(
        client,
        http_app,
        "history-main",
        "main",
        {"content": "Create durable session evidence", "request_id": "history-main-1"},
    )
    sessions = await client.get("/sessions")
    assert RuntimePaths.from_data_dir(http_app.state.paths.data_dir).session(
        "history-main"
    ).root.exists(), "the first committed turn materializes the session"
    resumed = await client.post(
        "/sessions",
        json={"session_id": "history-main", "thread_id": "main", "mode": "resume"},
    )

    assert sessions.json()["sessions"][-1]["workspace_root"] == str(workspace.resolve())
    assert threads.json()["threads"][0]["workspace_root"] == str(workspace.resolve())
    assert resumed.status_code == 200
    assert resumed.json()["data"]["key"]["thread_id"] == "main"
    assert resumed.json()["data"]["metadata"]["workspace_root"] == str(
        workspace.resolve()
    )


@pytest.mark.asyncio
async def test_http_open_then_close_without_input_leaves_no_session_data(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """An idle TUI attachment must not create a durable empty conversation."""
    from XBotv2.core.paths import RuntimePaths

    session_id = "unused-no-input"
    paths = RuntimePaths.from_data_dir(http_app.state.paths.data_dir)
    session_root = paths.session(session_id).root

    opened = await client.post(
        "/sessions",
        json={
            "session_id": session_id,
            "thread_id": "agent",
            "mode": "new",
            "history_limit": 100,
        },
    )
    assert opened.status_code == 200
    assert not session_root.exists(), "opening an idle session must remain in memory"

    closed = await client.post(f"/sessions/{session_id}/close")
    assert closed.status_code == 200
    assert not session_root.exists(), "closing an unused session must not leave data"
    listed = await client.get("/sessions")
    assert session_id not in {
        item["session_id"] for item in listed.json()["sessions"]
    }


@pytest.mark.asyncio
async def test_http_command_plane_exposes_platform_builtins(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """The command wire lists platform built-ins and executes them."""
    assert (await client.get("/commands")).status_code == 404

    open_response = await client.post(
        "/sessions", json={"session_id": "cmds", "thread_id": "t"}
    )
    assert open_response.status_code == 200

    commands_response = await client.get(
        "/sessions/cmds/threads/t/commands"
    )
    assert commands_response.status_code == 200
    entries = {item["name"]: item for item in commands_response.json()["commands"]}
    names = set(entries)
    assert entries["clear"]["effects"] == ["history", "thread", "sessions"], (
        "a client can tell what a command touches before running it"
    )
    assert entries["status"]["effects"] == []
    assert entries["permission"]["effects"] == ["policy", "commands"]
    assert {"status", "provider", "model", "effort",
            "clear", "undo", "fork", "jobs",
            "permission", "sandbox"} <= names
    # no_plugins excludes capability plugins: goal/skills/compact/agents
    # commands must not leak into the directory.
    assert not (names & {"goal", "compact"})

    result_response = await client.post(
        "/sessions/cmds/threads/t/commands",
        json={"raw": "/status"},
    )
    assert result_response.status_code == 200
    body = result_response.json()
    assert body["type"] == "command_result"
    assert body["data"]["status"] == "ok"
    assert body["data"]["effects"] == []
    assert "Provider: default" in body["data"]["message"]
    assert "Workspace:" in body["data"]["message"]
    assert "History: 0 turns, 0 messages" in body["data"]["message"]
    messages_response = await client.get("/sessions/cmds/threads/t/messages")
    assert messages_response.status_code == 200
    assert messages_response.json()["items"] == []


@pytest.mark.asyncio
async def test_http_builtin_commands_execute_through_command_plane(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """Provider, model, effort, and task commands use POST /commands."""
    await client.post(
        "/sessions", json={"session_id": "builtin", "thread_id": "t"}
    )

    async def run(raw: str) -> dict:
        response = await client.post(
            "/sessions/builtin/threads/t/commands", json={"raw": raw}
        )
        assert response.status_code == 200
        return response.json()["data"]

    status = await run("/provider status")
    assert status["status"] == "ok"
    assert status["message"] == "Provider: default (test)"

    listed = await run("/model list")
    assert listed["status"] == "ok"
    assert "default: test*" in listed["message"]

    switched = await run("/model use test")
    assert switched["status"] == "ok"
    assert switched["message"] == "Model switched to default (test)."
    assert switched["effects"] == ["thread"]

    effort = await run("/effort")
    assert effort["status"] == "ok"
    assert "no effort tiers" in effort["message"]

    tasks = await run("/jobs")
    assert tasks["status"] == "ok"
    assert tasks["message"] == "No background jobs."

    unknown = await run("/provider use nope")
    assert unknown["status"] == "error"
    assert "Unknown provider" in unknown["message"]

    usage = await run("/undo many")
    assert usage["status"] == "error"
    assert "Undo count must be a positive integer." in usage["message"]

    policy = await run("/permission status")
    assert policy["status"] == "ok"
    assert "Permission policy" in policy["message"]
    assert "Approved grants: 0" in policy["message"]

    sandbox = await run("/sandbox status")
    assert sandbox["status"] == "ok"
    assert "Sandbox policy" in sandbox["message"]
    assert "hard guard" in sandbox["message"]

    added = await run("/sandbox add readonly /tmp/reference")
    assert added["status"] == "ok"
    resources = await run("/sandbox resources")
    assert '\"path\": \"/tmp/reference\"' in resources["message"]
    removed = await run("/sandbox remove 1")
    assert removed["status"] == "ok"
    resources = await run("/sandbox resources")
    assert "/tmp/reference" not in resources["message"]


@pytest.mark.asyncio
async def test_typed_history_undo_fork_and_clear_persist_atomically(
    client: httpx.AsyncClient,
    http_app,
    tmp_path: Path,
) -> None:
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[
            {"content": "first answer"},
            {"content": "second answer"},
        ]),
    )
    await client.post("/sessions", json={"session_id": "history", "thread_id": "t"})
    await _submit_turn(client, http_app, "history", "t", {"content": "first"})
    await _submit_turn(client, http_app, "history", "t", {"content": "second"})

    undone = await client.post(
        "/sessions/history/threads/t/history/undo",
        json={"count": 1},
    )

    assert undone.status_code == 200
    payload = undone.json()
    assert set(payload) == {"removed_turns", "history", "stats"}
    assert set(payload["history"]) == {"items", "older_cursor"}
    messages = payload["history"]["items"]
    timing = messages[1].pop("timing")
    assert timing["total_ms"] >= 0
    assert timing["first_delta_ms"] is None or (
        timing["total_ms"] >= timing["first_delta_ms"] >= 0
    )
    assert payload["stats"]["turns"] == 1
    assert payload["stats"]["steps"] == 1
    replayed = [message.pop("id") for message in messages]
    assert all(replayed), "a replayed item must carry the id its live frame published"
    assert [
        (message["kind"], message["content"])
        for message in messages
    ] == [("human_input", "first"), ("assistant", "first answer")]
    current = await client.get("/sessions/history/threads/t/messages")
    # The same messages read back through the paging endpoint name themselves
    # exactly as the mutation response did: that agreement is what lets a client
    # merge history with frames it already holds.
    assert [message["id"] for message in current.json()["items"]] == replayed
    assert [message["content"] for message in current.json()["items"]] == [
        "first", "first answer",
    ]

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    source_session = paths.session("history")
    source = source_session.thread("t")
    source_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["entry"]["kind"] == "message_appended"
        and any(
            part.get("text") == "second"
            for part in record["entry"]["message"]["parts"]
        )
        for record in source_records
    )
    assert source_records[-1]["entry"]["kind"] == "surface_replaced"
    assert source_records[-1]["entry"]["operation"] == "undo"
    assert all(record["schema_version"] == 1 for record in source_records)
    source.plugin_state_dir.mkdir(exist_ok=True)
    (source.plugin_state_dir / "state.json").write_text(
        '{"sample.value": "kept"}\n'
    )
    source.artifact_file("context/cached.txt").parent.mkdir(parents=True)
    source.artifact_file("context/cached.txt").write_text("cached")
    source_session.config_file.write_text(
        "plugins:\n- id: permissions\n  config: {}\n",
        encoding="utf-8",
    )
    forked = await client.post("/sessions/history/fork")
    fork_id = forked.json()["session_id"]
    fork_session = paths.session(fork_id)
    fork_paths = fork_session.thread("t")

    assert (fork_paths.plugin_state_dir / "state.json").read_text() == (
        '{"sample.value": "kept"}\n'
    )
    assert fork_paths.artifact_file("context/cached.txt").read_text() == "cached"
    assert fork_session.config_file.read_text() == (
        "plugins:\n- id: permissions\n  config: {}\n"
    )
    assert fork_paths.messages_file.read_text() == source.messages_file.read_text()
    resumed = await client.post(
        "/sessions",
        json={"session_id": fork_id, "thread_id": "t", "mode": "resume"},
    )
    assert [
        item["content"]
        for item in resumed.json()["data"]["history"]["items"]
    ] == [
        "first", "first answer",
    ]

    cleared = await client.post(
        "/sessions/history/threads/t/history/clear",
    )
    assert cleared.json()["removed_turns"] == 1
    assert cleared.json()["history"]["items"] == []
    current = await client.get("/sessions/history/threads/t/messages")
    assert current.json()["items"] == []
    cleared_records = [
        json.loads(line)
        for line in source.messages_file.read_text(encoding="utf-8").splitlines()
    ]
    assert cleared_records[:len(source_records)] == source_records
    assert cleared_records[-1]["entry"]["kind"] == "surface_replaced"
    assert cleared_records[-1]["entry"]["operation"] == "clear"

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
async def test_delete_session_closes_runtime_and_removes_persisted_state(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    opened = await client.post(
        "/sessions",
        json={"session_id": "delete-me", "thread_id": "agent"},
    )
    assert opened.status_code == 200
    session_root = http_app.state.paths.session("delete-me").root
    assert not session_root.exists()
    await _submit_turn(client, http_app, "delete-me", "agent", {
        "content": "persist before deletion",
    })
    assert session_root.is_dir()

    deleted = await client.delete("/sessions/delete-me")
    assert deleted.status_code == 200
    assert deleted.json() == {"session_id": "delete-me", "status": "deleted"}
    assert not session_root.exists()
    assert (await client.get("/sessions/delete-me")).status_code == 404
    assert (await client.delete("/sessions/delete-me")).status_code == 404


@pytest.mark.asyncio
async def test_typed_history_mutations_validate_and_reject_busy_threads(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "answer"}]),
    )
    await client.post(
        "/sessions", json={"session_id": "typed-history", "thread_id": "t"}
    )
    await _submit_turn(client, http_app, "typed-history", "t", {
        "content": "question",
    })

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
        busy_delete = await client.delete("/sessions/typed-history")
    finally:
        ctx.turn_lock.release()
    assert busy.status_code == 409
    assert busy.json()["code"] == "thread_busy"
    assert busy.json()["retryable"] is True
    assert busy_fork.status_code == 409
    assert busy_fork.json()["code"] == "thread_busy"
    assert busy_delete.status_code == 409
    assert busy_delete.json()["code"] == "thread_busy"
    assert http_app.state.paths.session("typed-history").root.is_dir()

    undone = await client.post(
        "/sessions/typed-history/threads/t/history/undo",
        json={"count": 1},
    )
    assert undone.status_code == 200
    assert undone.json()["removed_turns"] == 1
    assert undone.json()["history"]["items"] == []
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
                "max_output_tokens": 1024,
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
    resumed_selection = resumed.json()["data"]["metadata"]["runtime_selection"]
    assert resumed_selection["model"]["route"]["provider"] == "alternate"
    assert resumed_selection["model"]["route"]["model"] == "alternate-model"


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
            {"model": "alternate-model", "max_output_tokens": 1024},
            {
                "model": "alternate-model-2",
                "max_context_tokens": 8192,
                "max_output_tokens": 1024,
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
async def test_new_session_reads_updated_global_plugin_config(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    plugins_file = http_app.state.paths.config_dir / "plugins.yaml"
    tree = yaml.safe_load(plugins_file.read_text(encoding="utf-8"))
    await client.post(
        "/sessions", json={"session_id": "before-config", "thread_id": "t"}
    )
    before = await http_app.state.manager.get("before-config", "t")
    assert before.application._context.sandbox.enabled is False

    sandbox_entry = next(item for item in tree if item["id"] == "sandbox")
    sandbox_entry["config"]["enabled"] = True
    plugins_file.write_text(
        yaml.safe_dump(tree, sort_keys=False),
        encoding="utf-8",
    )
    await client.post(
        "/sessions", json={"session_id": "after-config", "thread_id": "t"}
    )
    after = await http_app.state.manager.get("after-config", "t")

    assert before.application._context.sandbox.enabled is False
    assert after.application._context.sandbox.enabled is True


@pytest.mark.asyncio
async def test_workspace_overlay_applies_when_session_starts(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    overlay_dir = Path(http_app.state.workspace_root) / ".xbot"
    overlay_dir.mkdir()
    (overlay_dir / "plugins.yaml").write_text(
        yaml.safe_dump([{
            "id": "sandbox",
            "name": "sandbox",
            "config": {"enabled": True},
        }], sort_keys=False),
        encoding="utf-8",
    )

    response = await client.post(
        "/sessions", json={"session_id": "workspace-overlay", "thread_id": "t"}
    )
    ctx = await http_app.state.manager.get("workspace-overlay", "t")

    assert response.status_code == 200
    assert ctx.application._context.sandbox.enabled is True


@pytest.mark.asyncio
async def test_http_effort_switches_only_advertised_tiers(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """/effort switches among the active model's advertised tiers."""
    plugins_file = http_app.state.paths.config_dir / "plugins.yaml"
    tree = yaml.safe_load(plugins_file.read_text(encoding="utf-8"))
    llm_entry = next(item for item in tree if item["id"] == "llm")
    await client.post(
        "/sessions", json={"session_id": "effort", "thread_id": "t"}
    )

    no_tiers = await client.put(
        "/sessions/effort/threads/t/effort", json={"effort": "high"}
    )
    assert no_tiers.status_code == 400
    assert no_tiers.json()["code"] == "unsupported_effort"

    model = llm_entry["config"]["providers"]["default"]["models"][0]
    model["reasoning_effort"] = "high"
    model["effort"] = ["low", "medium", "high"]
    plugins_file.write_text(
        yaml.safe_dump(tree, sort_keys=False),
        encoding="utf-8",
    )
    opened = await client.post(
        "/sessions",
        json={"session_id": "effort-configured", "thread_id": "t"},
    )
    assert opened.status_code == 200

    switched = await client.put(
        "/sessions/effort-configured/threads/t/effort",
        json={"effort": "low"},
    )
    assert switched.status_code == 200
    body = switched.json()
    assert body["provider"] == "default"
    assert body["model"] == "test"
    assert body["reasoning_effort"] == "low"
    assert body["model_mode"] == "low"
    assert body["available"] == ["low", "medium", "high"]

    unsupported = await client.put(
        "/sessions/effort-configured/threads/t/effort",
        json={"effort": "max"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["code"] == "unsupported_effort"
    assert "available: low, medium, high" in unsupported.json()["message"]


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
            "permissions": {"read": "allow", "shell": "allow"},
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
    cached_path.write_text("cached after policy update", encoding="utf-8")
    [cached_result] = await ctx.application._context.tools.execute_all([
        ToolCall(
            id="read-cached-policy",
            name="read",
            args={"path": str(cached_path)},
        ),
    ])
    status_response = await client.get("/sessions/policy/policy")

    assert policy_response.status_code == 200
    assert status_response.status_code == 200
    assert isinstance(cached_result.message.outcome, ToolSucceeded)
    assert "cached after policy update" in "".join(
        part.text for part in cached_result.message.outcome.output.parts
    )
    assert {
        rule["tool_pattern"]
        for rule in status_response.json()["permissions"]["rules"]
    } == {"read", "shell"}
    assert {
        rule["tool_pattern"]
        for rule in status_response.json()["effective_permissions"]["rules"]
    } == {"read", "shell"}
    assert status_response.json()["sandbox"]["external_read"] == "readonly"
    assert status_response.json()["effective_sandbox"]["external_read"] == "readonly"
    assert (
        status_response.json()["effective_sandbox"]["enabled"]
        is ctx.application._context.sandbox.enabled
    )
    state_root = http_app.state.paths.session("policy").thread("t").state_dir
    events_path = state_root / "events.jsonl"
    events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert "permission_override_set" not in events
    assert "sandbox_override_set" not in events


@pytest.mark.asyncio
async def test_http_interaction_response_maps_session_receipt() -> None:
    from XBotv2.session.protocol import _interaction_response

    async def receipt() -> InteractionReceipt:
        return InteractionReceipt(
            interaction_id="permission:scope",
            resolution=Answered(answer={"decision": "allow"}),
            pending_ids=("user-input:next",),
        )

    response = await _interaction_response(receipt())

    assert response.recorded is True
    assert response.request_id == "permission:scope"
    assert response.pending_interactions == ["user-input:next"]


@pytest.mark.parametrize(
    ("event_type", "request_id", "expected_value"),
    [
        ("permission_request", "permission:fast", "allow"),
        ("user_input_required", "user_input:fast", "continue"),
    ],
)
@pytest.mark.asyncio
async def test_live_interaction_is_pending_before_event_is_published(
    event_type: str,
    request_id: str,
    expected_value: str,
) -> None:
    from XBotv2.application.client_events import ClientEventRouter
    from XBotv2.interactions.contracts import InteractionRegistration
    from XBotv2.interactions.interactions import InteractionWaiter
    from XBotv2.interactions.protocol import (
        Answered,
        InputCancelled,
        InputTimedOut,
        UserInputRecorded,
        UserInputRequest,
    )
    from XBotv2.permissions.contracts import (
        Allowed,
        Denied,
        NamedPermission,
        PermissionRequest,
    )
    from XBotv2.permissions.protocol import PermissionResponseRecorded

    permission_waiter = InteractionWaiter(
        timed_out=lambda reason: Denied(reason=reason),
        cancelled=lambda reason: Denied(reason=reason),
    )
    user_input_waiter = InteractionWaiter(
        timed_out=lambda reason: InputTimedOut(reason=reason),
        cancelled=lambda reason: InputCancelled(reason=reason),
    )
    if event_type == "permission_request":
        waiter = permission_waiter
        request = PermissionRequest(
            interaction_id=request_id,
            source="test",
            subject=NamedPermission(tool="shell"),
            reason="test approval",
        )
        resolution = Allowed(scope="once")
        registration = InteractionRegistration(
            kind=event_type,
            request_type=PermissionRequest,
            resolution_types=(Allowed, Denied),
            waiter=waiter,
            timeout_seconds=lambda _request: None,
            recorded_event=lambda receipt: PermissionResponseRecorded(
                interaction_id=receipt.interaction_id,
                approval=receipt.resolution,
                pending_ids=receipt.pending_ids,
            ),
        )
    else:
        waiter = user_input_waiter
        request = UserInputRequest(
            interaction_id=request_id,
            source="test",
            question="continue?",
        )
        resolution = Answered(answer=expected_value)
        registration = InteractionRegistration(
            kind=event_type,
            request_type=UserInputRequest,
            resolution_types=(Answered, InputTimedOut, InputCancelled),
            waiter=waiter,
            timeout_seconds=lambda _request: None,
            recorded_event=lambda receipt: UserInputRecorded(
                interaction_id=receipt.interaction_id,
                resolution=receipt.resolution,
                pending_ids=receipt.pending_ids,
            ),
        )
    router = ClientEventRouter()
    router.register_interaction(registration)
    emitted = []

    class _Runtime:
        application = SimpleNamespace(client_events=router)

        def publish_event(self, event, *, scope):
            emitted.append((event, scope))

    runtime = _Runtime()
    turn_events = TurnEventRouter(runtime, request_id)
    dispose_sink = router.install(turn_events.live_sink)
    sink_task = asyncio.create_task(router.request(request))
    await asyncio.sleep(0)
    assert request_id in waiter.pending_request_ids()
    assert emitted[0][0] == request

    router.resolve(request_id, resolution)
    result = await sink_task
    dispose_sink()
    assert result == resolution
    assert emitted[1][0].interaction_id == request_id
    if isinstance(result, Allowed):
        assert expected_value == "allow"
    elif isinstance(result, Answered):
        assert result.answer == expected_value


@pytest.mark.asyncio
async def test_request_permission_tool_emits_request_id() -> None:
    """Permission requests carry a typed interaction ID through approval."""
    from XBotv2.permissions.tools import request_tool_permission
    from XBotv2.permissions import Allowed

    captured: dict[str, Any] = {}

    class _Approval:
        async def request(self, event):
            captured["event"] = event
            return Allowed(scope="once")

    async def apply_decision(_event, decision):
        captured["decision"] = (decision.kind, decision.scope)
        return decision

    result = await request_tool_permission(
        "shell",
        {},
        "needs approval",
        approval=_Approval(),
        apply_permission_decision=apply_decision,
    )
    assert isinstance(result, ToolSucceeded)
    assert captured["decision"] == ("allowed", "once")
    event = captured["event"]
    assert event.interaction_id
    assert event.source == "request_permission"


@pytest.mark.asyncio
async def test_http_permission_response_rejects_always_scope() -> None:
    from XBotv2.permissions import PermissionResponseRequest

    with pytest.raises(ValidationError, match="scope"):
        PermissionResponseRequest(
            request_id="permission:scope",
            decision="allow",
            scope="always",
        )


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
    assert ctx.application._context.permissions.check("shell", {}) == "deny"

    permission_reset = await client.patch(
        "/sessions/policy-reset/policy",
        json={"remove_permissions": ["shell"]},
    )
    assert permission_reset.status_code == 200
    # Removing the session override reveals the bundled xcore.yaml rule.
    assert ctx.application._context.permissions.check("shell", {}) == "allow"

    sandbox_status = await client.get("/sessions/policy-reset/policy")
    assert sandbox_status.status_code == 200
    from XBotv2.sandbox.contracts import SandboxConfig
    assert sandbox_status.json()["sandbox"] == SandboxConfig().model_dump()
    assert (
        sandbox_status.json()["effective_sandbox"]["enabled"]
        is ctx.application._context.sandbox.enabled
    )

    sandbox_update = await client.patch(
        "/sessions/policy-reset/policy",
        json={"sandbox": {"external_read": "deny"}},
    )
    assert sandbox_update.status_code == 200
    assert ctx.application._context.sandbox.external_read == "deny"


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
                    "default_provider": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_output_tokens": 1024,
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
                "config": {"enabled": False, "resources": []},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "default_decision": "allow",
                    "rules": [
                        {"tool_pattern": "ask_user", "decision": "ask"},
                        {"tool_pattern": "request_permission", "decision": "ask"},
                        {"tool_pattern": "edit", "decision": "ask"},
                    ],
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
async def test_active_attach_without_persistence_succeeds_but_rebuild_fails(
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
                    "default_provider": "default",
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
                                    "max_output_tokens": 1024,
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
                "config": {"enabled": False, "resources": []},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "default_decision": "allow",
                    "rules": [
                        {"tool_pattern": "ask_user", "decision": "ask"},
                        {"tool_pattern": "request_permission", "decision": "ask"},
                        {"tool_pattern": "edit", "decision": "ask"},
                    ],
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
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "memory only"}]),
    )
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
        assert resumed.status_code == 200

        closed = await ac.post("/sessions/mem/close")
        assert closed.status_code == 200
        resumed = await ac.post(
            "/sessions",
            json={"session_id": "mem", "thread_id": "t", "mode": "resume"},
        )
        assert resumed.status_code == 404
        assert resumed.json()["code"] == "session_not_found"
    await server.stop()


@pytest.mark.asyncio
async def test_http_events_stream_turn_events(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "stream1", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    runtime = await http_app.state.manager.get("stream1", "t")
    shared = runtime.event_stream.subscribe()
    response = await client.post(
        "/sessions/stream1/threads/t/messages",
        json={"content": "hi there", "request_id": "req-1"},
    )
    assert response.status_code == 202
    frames = []
    async with asyncio.timeout(1):
        async for frame in shared:
            frames.append(frame)
            if isinstance(frame.event, LoopTurnEnded):
                break
    await shared.aclose()
    events = [frame.event for frame in frames]
    assert any(isinstance(event, LoopTurnStarted) for event in events)
    assert any(isinstance(event, LoopTurnEnded) for event in events)
    assistant = next(
        event.message for event in events if isinstance(event, AssistantCompleted)
    )
    timing = assistant.exchange.timing
    assert timing.total_ms >= 0
    assert timing.first_delta_ms is None or 0 <= timing.first_delta_ms <= timing.total_ms
    assert timing.decode_ms is None or timing.decode_ms >= 0
    assert "".join(
        part.text for part in assistant.parts if isinstance(part, TextPart)
    ) == "hello from mock"


@pytest.mark.asyncio
async def test_http_message_request_id_reaches_engine_hooks_and_sse(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    from XBotv2.agentloop import Events

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "request-context", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("request-context", "t")
    observed = []

    async def record(ctx):
        observed.append(type(ctx))

    session.application.events.on(Events.TURN_START, record)
    session.application.events.on(Events.STATE_CHANGED, record)

    shared = session.event_stream.subscribe()
    response = await client.post(
        "/sessions/request-context/threads/t/messages",
        json={"content": "hello", "request_id": "request-http-1"},
    )
    assert response.status_code == 202
    events = []
    async for frame in shared:
        events.append(frame)
        if isinstance(frame.event, LoopTurnEnded):
            break
    await shared.aclose()
    assistant = next(
        frame.event.message for frame in events
        if isinstance(frame.event, AssistantCompleted)
    )
    assert assistant.exchange.observation.purpose.turn_id == "request-http-1"
    assert all(
        isinstance(frame.scope, TurnScope)
        for frame in events
        if isinstance(frame.event, (LoopTurnStarted, AssistantCompleted, LoopTurnEnded))
    )
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_http_generated_request_id_reaches_engine_and_sse(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    from XBotv2.agentloop import Events

    open_resp = await client.post(
        "/sessions",
        json={"session_id": "generated-request", "thread_id": "t"},
    )
    assert open_resp.status_code == 200
    session = await http_app.state.manager.get("generated-request", "t")
    observed = []

    async def record(ctx):
        observed.append(type(ctx))

    session.application.events.on(Events.TURN_START, record)

    shared = session.event_stream.subscribe()
    response = await client.post(
        "/sessions/generated-request/threads/t/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 202
    events = []
    async for frame in shared:
        events.append(frame)
        if isinstance(frame.event, LoopTurnEnded):
            break
    await shared.aclose()
    assistant = next(
        frame.event.message for frame in events
        if isinstance(frame.event, AssistantCompleted)
    )
    generated_id = assistant.exchange.observation.purpose.turn_id
    assert generated_id.startswith("req-")
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_http_messages_preserves_chinese_payload_in_request(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    llm = MockLLM(responses=[{"content": "received unicode"}])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    open_resp = await client.post(
        "/sessions", json={"session_id": "zh", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    runtime = await http_app.state.manager.get("zh", "t")
    shared = runtime.event_stream.subscribe()
    response = await client.post(
        "/sessions/zh/threads/t/messages",
        json={"content": "当前磁盘用了多少", "request_id": "req-zh"},
    )
    assert response.status_code == 202
    events = []
    async for frame in shared:
        events.append(frame.event)
        if isinstance(frame.event, LoopTurnEnded):
            break
    await shared.aclose()
    assert any(isinstance(event, AssistantCompleted) for event in events)
    [user_message] = [
        message
        for message in llm.request_history[-1].messages
        if isinstance(message, ProviderUser)
    ]
    assert any(part.text == "当前磁盘用了多少" for part in user_message.parts)


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
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="fold-end",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    first_task = asyncio.create_task(
        _drain_stream(_runtime_command(ctx, "first", "req-1"))
    )
    await asyncio.sleep(0)
    # While the LLM is busy the input is held in the pending fold, not
    # dropped or processed out of band.
    ev_stream = ctx.event_stream.subscribe()
    second_task = asyncio.create_task(
        _drain_stream(_runtime_command(ctx, "second", "req-2"))
    )
    await _wait_for_pending_input_count(ctx, 1)

    release.set()
    first_events = await asyncio.wait_for(first_task, timeout=3)
    second_events = await asyncio.wait_for(second_task, timeout=3)
    def assistant_text(events):
        return [
            "".join(
                part.text for part in event.message.parts
                if isinstance(part, TextPart)
            )
            for event in events
            if isinstance(event, AssistantCompleted)
        ]

    assert assistant_text(first_events) == ["first reply", "second reply"]
    # With no tool boundary, the turn-end fold still fuses the held input into
    # the same turn. Both request subscriptions observe the central stream.
    found = None
    async with asyncio.timeout(1):
        while found is None:
            event = (await anext(ev_stream)).event
            if (
                isinstance(event, MessagePublishedEvent)
                and event.record.root.content == "second"
            ):
                found = event.record.root
    assert found.id
    assert assistant_text(second_events) == ["first reply", "second reply"]
    snapshot = await ctx.application.snapshot()
    assert [
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in snapshot.messages
        if isinstance(message, HumanInputMessage)
    ] == [
        "first", "second",
    ]


@pytest.mark.asyncio
async def test_pending_queue_is_authoritative_editable_and_removable_over_http(
    http_app,
    client: httpx.AsyncClient,
) -> None:
    release = asyncio.Event()
    llm = _GatedMockLLM(release, responses=[
        {"content": "session title"},
        {"content": "first reply"},
    ])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="queue-resource",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    first_task = asyncio.create_task(
        _drain_stream(_runtime_command(ctx, "first", "req-first"))
    )
    await asyncio.sleep(0)
    await http_app.state.manager.send_message(SendMessage(
        session_id="queue-resource",
        thread_id="t",
        content="queued draft",
        request_id="req-queued",
        delivery="queue",
    ))
    await http_app.state.manager.send_message(SendMessage(
        session_id="queue-resource",
        thread_id="t",
        content="discarded draft",
        request_id="req-discarded",
        delivery="queue",
    ))
    await asyncio.sleep(0)

    turn_events = []
    try:
        queue_url = "/sessions/queue-resource/threads/t/queue"
        listed = await client.get(queue_url)
        assert listed.status_code == 200
        assert [item["message_id"] for item in listed.json()["items"]] == [
            "req-queued",
            "req-discarded",
        ]
        resumed = await client.post("/sessions", json={
            "session_id": "queue-resource",
            "thread_id": "t",
            "mode": "resume",
        })
        assert resumed.status_code == 200
        assert resumed.json()["data"]["pending_inputs"] == listed.json()["items"]

        edited = await client.patch(
            f"{queue_url}/req-queued",
            json={"action": "edit", "content": "edited draft"},
        )
        assert edited.status_code == 200
        assert edited.json()["items"][0]["content"] == "edited draft"

        steered = await client.patch(
            f"{queue_url}/req-queued",
            json={"action": "steer"},
        )
        assert steered.status_code == 200
        assert steered.json()["items"] == [{
            "message_id": "req-queued",
            "content": "edited draft",
            "target": "next-step",
            "image_count": 0,
            "artifact_count": 0,
        }, {
            "message_id": "req-discarded",
            "content": "discarded draft",
            "target": "next-turn",
            "image_count": 0,
            "artifact_count": 0,
        }]

        removed = await client.patch(
            f"{queue_url}/req-discarded",
            json={"action": "remove"},
        )
        assert removed.status_code == 200
        assert removed.json()["items"] == steered.json()["items"][:1]

        missing = await client.patch(
            f"{queue_url}/req-discarded",
            json={"action": "remove"},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "queue_item_not_found"
    finally:
        release.set()
        turn_events = await asyncio.wait_for(first_task, timeout=3)

    assistant_replies = [
        "".join(
            part.text for part in event.message.parts
            if isinstance(part, TextPart)
        )
        for event in turn_events
        if isinstance(event, AssistantCompleted)
    ]
    assert assistant_replies == ["session title", "first reply"]
    model_user_text = [
        part.text
        for request in llm.request_history
        for message in request.messages
        if isinstance(message, ProviderUser)
        for part in message.parts
        if isinstance(part, TextPart)
    ]
    assert "edited draft" in model_user_text
    assert "discarded draft" not in model_user_text
    snapshot = await ctx.application.snapshot()
    committed_inputs = [
        "".join(
            part.text for part in message.parts
            if isinstance(part, TextPart)
        )
        for message in snapshot.messages
        if isinstance(message, HumanInputMessage)
    ]
    assert committed_inputs == ["first", "edited draft"]
    assert (await client.get(queue_url)).json()["items"] == []


@pytest.mark.asyncio
async def test_queued_input_enters_transcript_only_when_the_next_turn_claims_it(
    http_app,
) -> None:
    release = asyncio.Event()
    llm = _GatedMockLLM(
        release,
        responses=[
            {"content": "session title"},
            {"content": "first reply"},
            {"content": "queued reply"},
        ],
    )
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="queue-claim",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    shared = ctx.event_stream.subscribe()
    first_task = asyncio.create_task(
        _drain_stream(_runtime_command(ctx, "first", "req-first"))
    )
    await asyncio.sleep(0)
    queued_task = asyncio.create_task(_drain_stream(_runtime_command(ctx,
        "second",
        "req-second",
        delivery="queue",
    )))

    observed = []
    try:
        async with asyncio.timeout(1):
            while not (
                any(
                    isinstance(event, QueueReplacedEvent)
                    and any(item.message_id == "req-second" for item in event.items)
                    for event in observed
                )
                and any(
                    isinstance(event, InputAcceptedEvent)
                    and event.message_ids == ["req-second"]
                    for event in observed
                )
            ):
                observed.append((await anext(shared)).event)
        assert not any(
            isinstance(event, MessagePublishedEvent)
            and event.record.root.content == "second"
            for event in observed
        )
        accepted = next(
            event for event in observed
            if isinstance(event, InputAcceptedEvent)
            and event.message_ids == ["req-second"]
        )
        assert accepted.target == "next-turn"
        assert ctx.pending_inputs()[0].target == "next-turn"

        release.set()
        first_events, queued_events = await asyncio.gather(first_task, queued_task)
        async with asyncio.timeout(1):
            while not (
                any(
                    isinstance(event, MessagePublishedEvent)
                    and event.record.root.content == "second"
                    for event in observed
                )
                and any(
                    isinstance(event, InputConsumedEvent)
                    and event.message_ids == ["req-second"]
                    for event in observed
                )
            ):
                observed.append((await anext(shared)).event)
        queue_drained_at = next(
            index for index, event in enumerate(observed)
            if isinstance(event, QueueReplacedEvent) and not event.items
        )
        message_at = next(
            index for index, event in enumerate(observed)
            if isinstance(event, MessagePublishedEvent)
            and event.record.root.content == "second"
        )
        assert queue_drained_at < message_at
        assert sum(
            isinstance(event, InputClaimedEvent)
            and event.message_ids == ["req-second"]
            for event in observed
        ) == 1
        assert sum(
            isinstance(event, InputConsumedEvent)
            and event.message_ids == ["req-second"]
            for event in observed
        ) == 1
        assert any(isinstance(event, AssistantCompleted) for event in queued_events)
        assert not any(
            isinstance(event, AssistantCompleted)
            and "".join(
                part.text for part in event.message.parts if isinstance(part, TextPart)
            ) == "queued reply"
            for event in first_events
        )
    finally:
        release.set()
        for task in (first_task, queued_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first_task, queued_task, return_exceptions=True)
        await shared.aclose()


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
    ctx.application._context.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(tool_pattern=".*", decision="allow"),),
        default_decision="ask",
    ),))
    ctx.application._context.tools._registry.register(Tool.from_function(wait_for_release))
    ctx.application._context.tools._registry.restrict(AllTools())

    async def collect(stream):
        return [event async for event in stream]

    # This subscriber must exist before the turn starts; the event stream is
    # live and a new subscription after completion does not replay history.
    ev_stream = ctx.event_stream.subscribe()
    first_task = asyncio.create_task(collect(
        _runtime_command(ctx, "start the tool", "req-1")
    ))
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    second_task = asyncio.create_task(collect(
        _runtime_command(ctx, "also include this", "req-2")
    ))
    await _wait_for_pending_input_count(ctx, 1)

    release_tool.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)

    assert llm.call_count == 2
    assert [
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in llm.request_history[1].messages
        if isinstance(message, ProviderUser)
    ] == ["start the tool", "also include this"]
    # The folded-in request is notified on the shared event stream (id +
    # content), and both request subscriptions observe the response events.
    async with asyncio.timeout(1):
        while True:
            msg = (await anext(ev_stream)).event
            if (
                isinstance(msg, MessagePublishedEvent)
                and msg.record.root.content == "also include this"
            ):
                break
    assert msg.record.root.id
    assert any(
        isinstance(event, AssistantCompleted)
        and any(
            isinstance(part, TextPart) and part.text == "handled both requests"
            for part in event.message.parts
        )
        for event in second_events
    )
    assert any(
        isinstance(event, AssistantCompleted)
        and any(
            isinstance(part, TextPart) and part.text == "handled both requests"
            for part in event.message.parts
        )
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
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="fold-thinking",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    ctx.application._context.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(tool_pattern=".*", decision="allow"),),
        default_decision="ask",
    ),))
    tool_started = asyncio.Event()

    async def wait_for_release(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    ctx.application._context.tools._registry.register(Tool.from_function(wait_for_release))
    ctx.application._context.tools._registry.restrict(AllTools())

    async def collect(stream):
        return [event async for event in stream]

    # Keep this subscription live from before the first message; completed
    # event streams are not replayed for a newly attached subscriber.
    ev_stream = ctx.event_stream.subscribe()
    first_task = asyncio.create_task(collect(
        _runtime_command(ctx, "A", "req-A")
    ))
    await asyncio.sleep(0)
    # B is submitted while A is still thinking (the gated LLM has not returned
    # a tool call yet); it must be held, not rejected.
    second_task = asyncio.create_task(collect(
        _runtime_command(ctx, "B", "req-B")
    ))
    await _wait_for_pending_input_count(ctx, 1)

    release_call1.set()
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    # C lands inside the tool window; both held inputs fold together.
    third_task = asyncio.create_task(collect(
        _runtime_command(ctx, "C", "req-C")
    ))
    await _wait_for_pending_input_count(ctx, 2)
    release_tool.set()

    first_events, second_events, third_events = await asyncio.gather(
        first_task, second_task, third_task
    )
    # B was folded into A's turn and notified in order on the live event stream.
    found = None
    async with asyncio.timeout(1):
        while found is None:
            event = (await anext(ev_stream)).event
            if (
                isinstance(event, MessagePublishedEvent)
                and event.record.root.content == "B"
            ):
                found = event.record.root
    assert found.id
    assert any(
        isinstance(event, AssistantCompleted)
        and any(
            isinstance(part, TextPart) and part.text == "merged reply"
            for part in event.message.parts
        )
        for event in third_events
    )
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_general_message_uses_session_event_stream(http_app) -> None:
    llm = MockLLM(responses=[{"content": "background result"}])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="general-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    events = ctx.event_stream.subscribe()
    job_id = await _start_background_shell(
        ctx.application._context,
        "printf done",
    )
    await ctx.application._context.jobs.wait([job_id], timeout=1)

    # The completion is broadcast as a notice and staged in the inbox; it
    # must NOT start a turn on its own.
    observed = []
    async with asyncio.timeout(1):
        while not {
            QueueReplacedEvent,
            JobCompletedEvent,
        }.issubset({type(event) for event in observed}):
            observed.append((await anext(events)).event)
    await asyncio.sleep(0.05)
    assert llm.call_count == 0, "general message must not wake a turn"
    assert len(ctx.engine.inbox) == 1

    # The next user turn consumes it into the model context.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(_runtime_command(ctx, "continue", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in llm.request_history[0].messages
        if isinstance(message, ProviderUser)
        and "<runtime_event" in "".join(
            part.text for part in message.parts if isinstance(part, TextPart)
        )
    ]
    assert len(runtime_msgs) == 1
    runtime_event = ET.fromstring(runtime_msgs[0])
    payload = json.loads(runtime_event.findtext("payload"))
    assert payload["kind"] == "job_completed"
    assert payload["view"]["id"] == job_id


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
    llm = MockLLM(responses=[
        {"content": "session title"},
        {"content": "task acknowledged"},
    ])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="background-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    events = ctx.event_stream.subscribe()

    job_id = await _start_background_shell(ctx.application._context, "printf result")

    # Completion is broadcast as a notice and staged in the agent inbox, but
    # must NOT wake a turn on its own.
    notice = None
    while notice is None:
        event = (await asyncio.wait_for(anext(events), timeout=1)).event
        if isinstance(event, JobCompletedEvent):
            notice = event
    assert notice.view.id == job_id
    assert notice.view.state == "succeeded"
    await asyncio.sleep(0.05)
    assert llm.call_count == 0, "completion must not wake an LLM turn"
    assert len(ctx.engine.inbox) == 1

    # The next user turn consumes the staged completion all at once.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(_runtime_command(ctx, "continue", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        message
        for message in llm.request_history[-1].messages
        if isinstance(message, ProviderUser)
        and "<runtime_event" in "".join(
            part.text for part in message.parts if isinstance(part, TextPart)
        )
    ]
    assert len(runtime_msgs) == 1
    assert len(runtime_msgs) == 1
    runtime_text = "".join(
        part.text for part in runtime_msgs[0].parts if isinstance(part, TextPart)
    )
    runtime_event = ET.fromstring(runtime_text)
    assert runtime_event.attrib == {"event": "completed", "source": "jobs"}
    payload = json.loads(runtime_event.findtext("payload"))
    assert payload["kind"] == "job_completed"
    assert payload["view"]["id"] == job_id
    assert payload["view"]["state"] == "succeeded"
    assert len(ctx.engine.inbox) == 0
    assert [message.kind for message in ctx.engine.messages] == [
        "runtime_notice", "human_input", "assistant",
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
    llm = MockLLM(responses=[
        {"content": "session title"},
        {"content": "ok"},
    ])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    ctx = await http_app.state.manager.open_session(
        session_id="aggregate-events",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    await _start_background_shell(ctx.application._context, "printf one")
    await _start_background_shell(ctx.application._context, "printf two")
    await asyncio.sleep(0.1)
    # Completions stage into the inbox without waking a turn.
    assert llm.call_count == 0, "completions must not wake an LLM turn"
    assert len(ctx.engine.inbox) == 2

    # The next user turn atomically claims all staged inputs while preserving
    # their individual message identities and order.
    await asyncio.wait_for(
        asyncio.create_task(_drain_stream(_runtime_command(ctx, "go", "req-2"))),
        timeout=3,
    )
    assert llm.call_count == 1
    runtime_msgs = [
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in llm.request_history[-1].messages
        if isinstance(message, ProviderUser)
        and "<runtime_event" in "".join(
            part.text for part in message.parts if isinstance(part, TextPart)
        )
    ]
    assert len(runtime_msgs) == 2
    commands = [
        json.loads(ET.fromstring(message).findtext("payload"))["view"]["label"]
        for message in runtime_msgs
    ]
    assert commands == ["printf one", "printf two"]
    assert len(ctx.engine.inbox) == 0
    assert [message.kind for message in ctx.engine.messages] == [
        "runtime_notice", "runtime_notice", "human_input", "assistant",
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
    job_id = await _start_background_shell(ctx.application._context, "sleep forever")
    await asyncio.sleep(0)

    busy_fork = await client.post("/sessions/task-stop/fork")
    first = await client.post(
        f"/sessions/task-stop/threads/t/jobs/{job_id}/stop"
    )
    second = await client.post(
        f"/sessions/task-stop/threads/t/jobs/{job_id}/stop"
    )

    assert busy_fork.status_code == 409
    assert busy_fork.json()["code"] == "thread_busy"
    assert first.status_code == 200
    assert first.json()["matched_count"] == 1
    assert first.json()["jobs"][0]["state"] == "cancelled_running"
    assert second.status_code == 200
    assert second.json()["jobs"][0]["state"] == "cancelled_running"


@pytest.mark.asyncio
async def test_jobs_command_lists_stops_and_reports_unknown_job(
    http_app,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def run(*_args: object, **_kwargs: object) -> str:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    await client.post(
        "/sessions", json={"session_id": "jobs-command", "thread_id": "t"}
    )
    ctx = await http_app.state.manager.get("jobs-command", "t")
    job_id = await _start_background_shell(
        ctx.application._context,
        "wait for command stop",
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    listed = await client.post(
        "/sessions/jobs-command/threads/t/commands",
        json={"raw": "/jobs ps"},
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["status"] == "ok"
    assert job_id in listed.json()["data"]["message"]
    assert "running" in listed.json()["data"]["message"]

    stopped = await client.post(
        "/sessions/jobs-command/threads/t/commands",
        json={"raw": f"/jobs stop {job_id}"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["status"] == "ok"
    job = ctx.application._context.jobs.get_or_none(job_id)
    assert job is not None
    assert job.status == "cancelled_running"

    unknown = await client.post(
        "/sessions/jobs-command/threads/t/commands",
        json={"raw": "/jobs stop missing-job"},
    )
    assert unknown.status_code == 200
    assert unknown.json()["data"]["status"] == "error"
    assert "Unknown job" in unknown.json()["data"]["message"]


@pytest.mark.asyncio
async def test_http_interrupt_leaves_shared_background_shell_running(
    http_app,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP interrupt cancels the foreground turn, not a shared SHELL job."""

    shell_started = asyncio.Event()
    foreground_started = asyncio.Event()
    llm = MockLLM(responses=[{
        "tool_calls": [{
            "id": "interrupt-blocker",
            "name": "interrupt_blocker",
            "args": {},
        }],
    }])
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )

    async def run_shell_command(*args, **kwargs):
        shell_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "XBotv2.coretools.shell.run_shell_command", run_shell_command
    )

    ctx = await http_app.state.manager.open_session(
        session_id="interrupt-shared-shell",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.workspace_root),
        no_plugins=True,
        llm_override=llm,
    )
    workspace = Path(http_app.state.workspace_root)
    jobs = ctx.application._context.jobs
    assert jobs is not None

    async def interrupt_blocker() -> str:
        """Block the foreground turn until HTTP interrupt cancels it."""
        foreground_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    ctx.application._context.permissions.replace_policies((PermissionPolicy(
        rules=(PermissionRule(
            tool_pattern="interrupt_blocker",
            decision="allow",
        ),),
    ),))
    ctx.application._context.tools.register(
        Tool.from_function(interrupt_blocker), cleanup="caller",
    )
    ctx.application._context.tools.restrict(AllTools())

    shell_result = await start_shell(
        "sleep forever",
        cwd=str(workspace),
        job_registry=jobs,
        artifacts=ctx.application._context.artifacts,
    )
    assert isinstance(shell_result, ToolSucceeded)
    shell_text = "".join(part.text for part in shell_result.output.parts)
    shell_job_id = shell_text.removeprefix("Started ")
    shell_job = jobs.get_or_none(shell_job_id)
    assert shell_job is not None
    await asyncio.wait_for(shell_started.wait(), timeout=1)
    assert isinstance(shell_job.state, Running)

    events = ctx.event_stream.subscribe()
    seen_events = []

    async def consume_events() -> None:
        async for frame in events:
            seen_events.append(frame.event)
            if (
                isinstance(frame.event, LoopTurnEnded)
                and frame.event.outcome.kind == "cancelled"
            ):
                return

    consumer = asyncio.create_task(consume_events())
    try:
        response = await client.post(
            "/sessions/interrupt-shared-shell/threads/t/messages",
            json={"content": "run the blocker", "request_id": "req-interrupt"},
        )
        assert response.status_code == 202, response.text
        await asyncio.wait_for(foreground_started.wait(), timeout=1)

        interrupt = await client.post(
            "/sessions/interrupt-shared-shell/threads/t/interrupt"
        )
        assert interrupt.status_code == 200, interrupt.text
        assert interrupt.json()["cancelled"] is True

        await asyncio.wait_for(consumer, timeout=2)
        assert any(isinstance(event, LoopTurnStarted) for event in seen_events)
        assert any(
            isinstance(event, LoopTurnEnded)
            and event.outcome.kind == "cancelled"
            for event in seen_events
        )
        assert isinstance(shell_job.state, Running)

        cancelled = await jobs.cancel(shell_job_id)
        assert cancelled.cancelled is True
        cancelled_job = jobs.get_or_none(shell_job_id)
        assert cancelled_job is not None
        assert cancelled_job.status == "cancelled_running"
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await events.aclose()
        if not shell_job.terminal:
            await jobs.cancel(shell_job_id)


@pytest.mark.asyncio
async def test_session_close_drops_pending_inbox_and_resume_starts_empty(http_app) -> None:
    llm = MockLLM(responses=[{"content": "seeded"}])
    ctx = await http_app.state.manager.open_session(
        session_id="fold-resume",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )
    await _drain_stream(_runtime_command(ctx, "seed history", "seed-request"))
    await ctx.engine.submit_input(
        InboxItem(
            id="f-1",
            target=InboxTarget.NEXT_STEP,
            input=RuntimeInput(
                source="test",
                event="completion",
                content="accepted during tool",
            ),
        ),
        wake=False,
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
async def test_resume_active_session_attaches_without_rebuilding(http_app) -> None:
    llm = MockLLM(responses=[{"content": "unused"}])
    runtime = await http_app.state.manager.open_session(
        session_id="active-attach",
        thread_id="t",
        provider_name="default",
        workspace_root=str(http_app.state.paths.data_dir),
        no_plugins=True,
        llm_override=llm,
    )

    attached = await http_app.state.manager.open_session(
        session_id="active-attach",
        thread_id="t",
        provider_name="different-request-does-not-reconfigure",
        workspace_root="/different/request/workspace",
        mode="resume",
        no_plugins=True,
    )

    assert attached is runtime
    assert attached.workspace_root == str(http_app.state.paths.data_dir)


@pytest.mark.asyncio
async def test_http_interrupt_emits_turn_cancelled_on_sse(
    http_app, tmp_path: Path
) -> None:
    """Pressing ESC (i.e. ``POST /sessions/{sid}/interrupt``) mid-turn
    must publish a terminal ``turn_ended`` event whose outcome is cancelled.

    This exercises the full production path:
    TUI ESC → ``POST /interrupt`` →
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
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=gated,
    )

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

            ready = asyncio.Event()
            turn_started = asyncio.Event()
            turn_cancelled = asyncio.Event()

            async def _consume_sse() -> None:
                async with ac.stream(
                    "GET",
                    f"/sessions/esc/threads/t/events?after="
                    f"{open_resp.json()['data']['event_cursor']}",
                ) as response:
                    assert response.status_code == 200, await response.aread()
                    ready.set()
                    async for line in response.aiter_lines():
                        sse_chunks.append(f"{line}\n")
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line.removeprefix("data: "))
                        if event["kind"] == "turn_started" and not turn_started.is_set():
                            turn_started.set()
                            ir = await ac.post("/sessions/esc/threads/t/interrupt")
                            assert ir.status_code == 200
                            assert ir.json()["cancelled"] is True
                        if (
                            event["kind"] == "turn_ended"
                            and event["payload"]["outcome"]["kind"] == "cancelled"
                        ):
                            turn_cancelled.set()
                            return

            consumer = asyncio.create_task(_consume_sse())
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            response = await ac.post(
                "/sessions/esc/threads/t/messages",
                json={"content": "do something long", "request_id": "req-esc"},
            )
            assert response.status_code == 202, response.text
            await asyncio.wait_for(consumer, timeout=5.0)
            assert turn_started.is_set()
            assert turn_cancelled.is_set()
            # Defensive: unblock the LLM in case the test exits
            # before the engine's CancelledError fires.
            release.set()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)

    events = [
        json.loads(line.removeprefix("data: "))
        for line in sse_chunks
        if line.startswith("data: ")
    ]
    kinds = [event["kind"] for event in events]
    assert "turn_started" in kinds, f"no turn_started in {kinds!r}"
    ended = [event for event in events if event["kind"] == "turn_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"]["outcome"]["kind"] == "cancelled"
    # The engine is allowed to call the LLM at most once before the
    # cancellation lands.
    assert gated.calls <= 1, f"LLM was called {gated.calls} times after interrupt"


@pytest.mark.asyncio
async def test_workspace_sse_updates_and_replays_across_http_clients(http_app) -> None:
    """A resource commit reaches another client and remains cursor-replayable."""
    import socket
    import threading

    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        http_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws="none",
    ))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as probe:
        for _ in range(50):
            try:
                if (await probe.get("/health")).status_code == 200:
                    break
            except httpx.RequestError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("uvicorn server failed to start")

    try:
        async with (
            httpx.AsyncClient(base_url=base_url, timeout=5.0) as stream_client,
            httpx.AsyncClient(base_url=base_url, timeout=5.0) as mutation_client,
        ):
            cursor = (await mutation_client.get("/sessions")).json()["event_cursor"]
            async with stream_client.stream(
                "GET", f"/workspaces/events?after={cursor}"
            ) as response:
                assert response.status_code == 200
                created = await mutation_client.post(
                    "/sessions",
                    json={"session_id": "host-sync", "thread_id": "main"},
                )
                assert created.status_code == 200
                submitted = await mutation_client.post(
                    "/sessions/host-sync/threads/main/messages",
                    json={
                        "content": "Make this session durable",
                        "request_id": "host-sync-1",
                    },
                )
                assert submitted.status_code == 202
                live = await _read_workspace_frames(
                    response,
                    {"catalog/session-added", "catalog/workspace-changed"},
                )

            async with stream_client.stream(
                "GET", f"/workspaces/events?after={cursor}"
            ) as response:
                replayed = await _read_workspace_frames(
                    response,
                    {"catalog/session-added", "catalog/workspace-changed"},
                )

            workspaces = (await mutation_client.get("/workspaces")).json()["items"]
            workspace = next(
                item for item in workspaces
                if item["workspace_id"]
                == live["catalog/workspace-changed"]["payload"]["workspace"]["workspace_id"]
            )
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)

    assert live["catalog/session-added"]["payload"]["session"]["session_id"] == "host-sync"
    assert workspace["session_ids"][0] == "host-sync"
    assert {
        event_type: frame["sequence"] for event_type, frame in replayed.items()
    } == {
        event_type: frame["sequence"] for event_type, frame in live.items()
    }


async def _read_workspace_frames(
    response: httpx.Response,
    expected_types: set[str],
) -> dict[str, dict[str, Any]]:
    frames: dict[str, dict[str, Any]] = {}
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        frame = json.loads(line.removeprefix("data:").strip())
        if frame["kind"] in expected_types:
            frames[frame["kind"]] = frame
        if frames.keys() >= expected_types:
            return frames
    raise AssertionError(
        f"Workspace stream ended before frames arrived: {sorted(frames)}"
    )


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
async def _real_client(
    tmp_path: Path,
    *,
    llm: MockLLM,
    sandbox_enabled: bool,
    timeout: float = 30.0,
    no_plugins: bool = True,
    plugin_overlays: tuple[dict[str, Any], ...] = (),
) -> AsyncIterator[tuple[XBotClient, str, str]]:
    """A real local HTTP server and a connected ``XBotClient``.

    Yields ``(client, session_id, thread_id)``. The default request timeout is
    generous: ``open_session`` cold-starts a full XBot application, which can
    exceed a 100 ms client budget under load; 30 s matches the production client.
    """
    import socket
    import threading

    import uvicorn

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    plugin_configs = [
        {
            "id": "llm",
            "name": "llm",
            "config": {
                "default_provider": "default",
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
                                "max_output_tokens": 1024,
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
            "config": {
                "enabled": sandbox_enabled, "resources": [],
            },
        },
        {
            "id": "permissions",
            "name": "permissions",
            "config": {
                "default_decision": "allow",
                "rules": [
                    {
                        "tool_pattern": tool,
                        "param_patterns": {},
                        "decision": "ask",
                    }
                    for tool in ("read", "ask_user", "request_permission", "edit")
                ],
            },
        },
    ]
    plugin_configs.extend(plugin_overlays)
    (config_dir / "plugins.yaml").write_text(
        yaml.safe_dump(
            plugin_configs,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    application = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=no_plugins,
    )
    app = application.server
    application.sessions.application_factory = partial(create_agent_application, model_override=llm)

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
    client: XBotClient | None = None
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

        client = XBotClient(base_url, timeout=timeout)
        await client.open_session(
            session_id="default",
            thread_id="agent",
            workspace_root=str(workspace),
            mode="new",
        )
        yield client, "default", "agent"
    finally:
        if client is not None:
            await client.close()
        server.should_exit = True
        server_thread.join(timeout=3.0)
        await application.stop()


@pytest.mark.asyncio
async def test_real_http_timing_and_usage_are_identical_across_live_history_and_resume(
    tmp_path: Path,
) -> None:
    """One canonical exchange feeds SSE, history, stats, usage, and resume."""
    llm = MockLLM(responses=[
        {
            "tool_calls": [{
                "id": "timing-shell",
                "name": "shell",
                "args": {"command": "printf timing-tool"},
            }],
            "usage_metadata": {"input_tokens": 2, "output_tokens": 1},
        },
        {
            "content": "alpha beta",
            "chunks": ["alpha ", "beta"],
            "chunk_delay_ms": 10,
            "usage_metadata": {
                "input_tokens": 13,
                "output_tokens": 7,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 3,
                "prompt_cache_write_tokens": 2,
            },
        },
    ])
    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as (client, session_id, thread_id):
        submitted = asyncio.create_task(client.send_message(
            session_id,
            thread_id,
            "measure this turn",
            request_id="timing-turn",
        ))
        live = []
        async with asyncio.timeout(5):
            async for event in client.stream_events(
                session_id,
                thread_id,
                after=0,
            ):
                live.append(event)
                if event.kind == "turn_ended":
                    break
        await submitted

        assistant_events = [
            event for event in live if event.kind == "assistant_completed"
        ]
        assert len(assistant_events) == 2
        live_timing = assistant_events[-1].payload["timing"]
        assert live_timing["total_ms"] >= live_timing["first_delta_ms"] > 0
        tool_event = next(event for event in live if event.kind == "tool_completed")

        history = await client.list_messages(session_id, thread_id)
        assistants = [
            item for item in history.items if isinstance(item, AssistantRecord)
        ]
        assert len(assistants) == 2
        assistant = assistants[-1]
        assert assistant.timing.model_dump(mode="json") == live_timing
        tool = next(item for item in history.items if isinstance(item, ToolRecord))
        assert tool.timing.model_dump(mode="json") == tool_event.payload["timing"]

        active = await client.get_thread(session_id, thread_id)
        timing = assistant.timing
        assert active.session_stats.turns == 1
        assert active.session_stats.steps == 2
        assert active.session_stats.llm_ms == round(
            sum(item.timing.total_ms for item in assistants), 3
        )
        assert active.session_stats.tool_ms == tool.timing.duration_ms
        assert active.session_stats.ttft_ms == timing.first_delta_ms
        assert active.session_stats.ttft_steps == 1
        assert active.session_stats.decode_ms == timing.decode_ms
        assert active.session_stats.decode_tokens == 7
        assert active.usage.total_counters == TokenCounters(
            input=15,
            output=8,
            cache_read=5,
            cache_create=3,
            prompt_cache_write=2,
        )

        await client.close_thread(session_id, thread_id)
        inactive = await client.get_thread(session_id, thread_id)
        assert inactive.session_stats == active.session_stats
        assert inactive.usage == active.usage

        reopened = await client.open_session(
            session_id=session_id,
            thread_id=thread_id,
            mode="resume",
        )
        assert reopened.data.history.items == history.items
        resumed = await client.get_thread(session_id, thread_id)
        assert resumed.session_stats == active.session_stats
        assert resumed.usage == active.usage


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
    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=True,
    ) as (client, session_id, thread_id):
        (workspace / "hello.txt").write_text("hello", encoding="utf-8")

        events = []
        submitted = asyncio.create_task(
            client.send_message(session_id, thread_id, "list workspace", request_id="perm")
        )
        async for frame in client.stream_events(session_id, thread_id):
            events.append(frame)
            if frame.kind == "permission_request":
                await asyncio.sleep(0.2)
                await client.respond_permission(
                    session_id,
                    thread_id,
                    request_id=frame.payload["interaction_id"],
                    decision="allow",
                )
            if frame.kind == "turn_ended":
                break
        await submitted

    assert "permission_request" in [event.kind for event in events]
    assert any(
        event.kind == "tool_completed"
        and event.payload.get("kind") == "tool"
        and event.payload.get("call", {}).get("id") == "call_list"
        and event.payload.get("outcome", {}).get("kind") == "succeeded"
        for event in events
    ), [(event.kind, event.payload) for event in events]


@pytest.mark.asyncio
async def test_real_http_multi_tool_denial_preserves_batch_order_and_continues(
    tmp_path: Path,
) -> None:
    target = tmp_path / "workspace" / "denied.txt"
    source = tmp_path / "workspace" / "source.txt"
    llm = MockLLM(responses=[
        {
            "content": "writing",
            "tool_calls": [
                {
                    "name": "edit",
                    "args": {
                        "path": "denied.txt",
                        "mode": "write",
                        "content": "must not be written",
                    },
                    "id": "call-denied-write",
                },
                {
                    "name": "read",
                    "args": {"path": "source.txt", "mode": "utf8"},
                    "id": "call-read-source",
                },
            ],
        },
        {"content": "The write was denied; source content was read."},
    ])
    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=True,
    ) as (client, session_id, thread_id):
        source.write_text("source content", encoding="utf-8")
        events = []
        requested_interactions = []
        submitted = asyncio.create_task(client.send_message(
            session_id,
            thread_id,
            "write the file",
            request_id="deny-write",
        ))
        async for frame in client.stream_events(session_id, thread_id):
            events.append(frame)
            if frame.kind == "permission_request":
                interaction_id = frame.payload["interaction_id"]
                requested_interactions.append(interaction_id)
                decisions = {
                    "permission:call-denied-write": "deny",
                    "permission:call-read-source": "allow",
                }
                await client.respond_permission(
                    session_id,
                    thread_id,
                    request_id=interaction_id,
                    decision=decisions[interaction_id],
                )
            if frame.kind == "turn_ended":
                break
        await submitted

        recorded = [
            event for event in events
            if event.kind == "permission_response_recorded"
        ]
        tool_completions = [
            event for event in events
            if event.kind == "tool_completed"
            and event.payload["call"]["id"] in {
                "call-denied-write",
                "call-read-source",
            }
        ]
        assistant_completed = [
            event for event in events if event.kind == "assistant_completed"
        ]
        turn_ended = [event for event in events if event.kind == "turn_ended"]
        assert requested_interactions == [
            "permission:call-denied-write",
            "permission:call-read-source",
        ]
        assert len(recorded) == len(tool_completions) == 2
        assert len(assistant_completed) == 2
        assert len(turn_ended) == 1
        assert [
            (event.payload["interaction_id"], event.payload["approval"]["kind"])
            for event in recorded
        ] == [
            ("permission:call-denied-write", "denied"),
            ("permission:call-read-source", "allowed"),
        ]
        assert [
            (event.payload["call"]["id"], event.payload["outcome"]["kind"])
            for event in tool_completions
        ] == [
            ("call-denied-write", "denied"),
            ("call-read-source", "succeeded"),
        ]
        assert [event.payload["content"] for event in assistant_completed] == [
            "writing",
            "The write was denied; source content was read.",
        ]
        assert events.index(assistant_completed[0]) < events.index(recorded[0])
        assert events.index(recorded[0]) < events.index(tool_completions[0])
        assert events.index(tool_completions[0]) < events.index(recorded[1])
        assert events.index(recorded[1]) < events.index(tool_completions[1])
        assert events.index(tool_completions[1]) < events.index(assistant_completed[1])
        assert events.index(assistant_completed[1]) < events.index(turn_ended[0])
        page = await client.list_messages(session_id, thread_id)
        assert [record.kind for record in page.items] == [
            "human_input", "assistant", "tool", "tool", "assistant",
        ]
        assert all(isinstance(record, ToolRecord) for record in page.items[2:4])
        denied_record = page.items[2]
        read_record = page.items[3]
        assert isinstance(denied_record, ToolRecord)
        assert denied_record.call.id == "call-denied-write"
        assert isinstance(denied_record.outcome, ToolDenied)
        assert isinstance(read_record, ToolRecord)
        assert read_record.call.id == "call-read-source"
        assert isinstance(read_record.outcome, ToolSucceeded)
        assert "source content" in "".join(
            part.text for part in read_record.outcome.output.parts
        )
        assert not target.exists()


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
    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=True,
    ) as (client, session_id, thread_id):
        async def collect_events() -> list[dict[str, Any]]:
            collected = []
            submitted = asyncio.create_task(
                client.send_message(session_id, thread_id, "list workspace", request_id="wait")
            )
            async for frame in client.stream_events(session_id, thread_id):
                collected.append(frame)
                if frame.kind == "permission_request":
                    response = await client.interrupt(session_id, thread_id)
                    assert response.cancelled is True
                if frame.kind == "turn_ended":
                    break
            await submitted
            return collected

        events = await asyncio.wait_for(collect_events(), timeout=5.0)
        request = next(
            event for event in events if event.kind == "permission_request"
        )
        with pytest.raises(RuntimeError, match="interaction_no_longer_pending"):
            await client.respond_permission(
                session_id,
                thread_id,
                request_id=request.payload["interaction_id"],
                decision="allow",
            )
        page = await client.list_messages(session_id, thread_id)

    event_types = [event.kind for event in events]
    assert "permission_request" in event_types
    assert "permission_response_recorded" not in event_types
    assert "turn_ended" in event_types
    ended = next(event for event in events if event.kind == "turn_ended")
    assert ended.payload["outcome"]["kind"] == "cancelled"
    cancelled_result = next(
        event
        for event in events
        if event.kind == "tool_completed"
        and event.payload.get("call", {}).get("id") == "call_wait"
    )
    assert cancelled_result.payload["outcome"]["kind"] == "cancelled"
    assert event_types.index("tool_completed") < event_types.index("turn_ended")
    assert [record.kind for record in page.items] == [
        "human_input", "assistant", "tool",
    ]
    cancelled_record = page.items[-1]
    assert isinstance(cancelled_record, ToolRecord)
    assert cancelled_record.call.id == "call_wait"
    assert isinstance(cancelled_record.outcome, ToolCancelled)


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

    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as (client, session_id, thread_id):
        events = []
        submitted = asyncio.create_task(
            client.send_message(session_id, thread_id, "ask before continuing", request_id="ask")
        )
        async for frame in client.stream_events(session_id, thread_id):
            events.append(frame)
            if frame.kind == "permission_request":
                await client.respond_permission(
                    session_id,
                    thread_id,
                    request_id=frame.payload["interaction_id"],
                    decision="allow",
                )
            elif frame.kind == "user_input_required":
                response = await client.interrupt(session_id, thread_id)
                assert response.cancelled is True
            if frame.kind == "turn_ended":
                break
        await submitted

        request = next(
            event for event in events if event.kind == "user_input_required"
        )
        with pytest.raises(RuntimeError, match="interaction_no_longer_pending"):
            await client.respond_user_input(
                session_id,
                thread_id,
                request_id=request.payload["interaction_id"],
                answer="yes",
            )
        page = await client.list_messages(session_id, thread_id)

    event_types = [event.kind for event in events]
    assert "user_input_required" in event_types
    assert event_types.count("permission_response_recorded") == 1
    assert "user_input_recorded" not in event_types
    assert "turn_ended" in event_types
    ended = next(event for event in events if event.kind == "turn_ended")
    assert ended.payload["outcome"]["kind"] == "cancelled"
    cancelled_result = next(
        event
        for event in events
        if event.kind == "tool_completed"
        and event.payload.get("call", {}).get("id") == "call_wait"
    )
    assert cancelled_result.payload["outcome"]["kind"] == "cancelled"
    assert event_types.index("tool_completed") < event_types.index("turn_ended")
    assert [record.kind for record in page.items] == [
        "human_input", "assistant", "tool",
    ]
    cancelled_record = page.items[-1]
    assert isinstance(cancelled_record, ToolRecord)
    assert cancelled_record.call.id == "call_wait"
    assert isinstance(cancelled_record.outcome, ToolCancelled)


@pytest.mark.asyncio
async def test_real_http_ask_user_timeout_is_recorded_and_turn_continues(
    tmp_path: Path,
) -> None:
    llm = MockLLM(responses=[
        {
            "content": "asking",
            "tool_calls": [{
                "name": "ask_user",
                "args": {
                    "question": "Continue?",
                    "options": [
                        {"label": "yes", "description": "Continue."},
                        {"label": "no", "description": "Stop."},
                    ],
                    "timeout_seconds": 0.05,
                },
                "id": "call_timeout",
            }],
        },
        {"content": "continued after timeout"},
    ])

    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as (client, session_id, thread_id):
        events = []
        submitted = asyncio.create_task(client.send_message(
            session_id,
            thread_id,
            "ask with a deadline",
            request_id="ask-timeout",
        ))
        async for frame in client.stream_events(session_id, thread_id):
            events.append(frame)
            if frame.kind == "permission_request":
                await client.respond_permission(
                    session_id,
                    thread_id,
                    request_id=frame.payload["interaction_id"],
                    decision="allow",
                )
            if frame.kind == "turn_ended":
                break
        await submitted
        page = await client.list_messages(session_id, thread_id)

    recorded = next(
        event for event in events if event.kind == "user_input_recorded"
    )
    assert recorded.payload["interaction_id"] == "user_input:call_timeout"
    assert recorded.payload["resolution"] == {
        "kind": "timeout",
        "reason": "timeout",
    }
    completed = next(
        event
        for event in events
        if event.kind == "tool_completed"
        and event.payload["call"]["id"] == "call_timeout"
    )
    assert completed.payload["outcome"]["kind"] == "failed"
    assert completed.payload["outcome"]["error"]["code"] == "interaction_not_answered"
    assert any(
        event.kind == "assistant_completed"
        and event.payload["content"] == "continued after timeout"
        for event in events
    )
    assert [record.kind for record in page.items] == [
        "human_input", "assistant", "tool", "assistant",
    ]
    timed_out = page.items[2]
    assert isinstance(timed_out, ToolRecord)
    assert timed_out.outcome.kind == "failed"
    assert timed_out.outcome.error.code == "interaction_not_answered"


@pytest.mark.asyncio
async def test_real_http_open_session_replays_an_unanswered_interaction(
    tmp_path: Path,
) -> None:
    """A resumed session returns the question its client never answered.

    The request otherwise exists only in the bounded event-replay window, so a
    client that reloads or reconnects would lose the dialog and the turn would
    look stuck.
    """
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

    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as (client, session_id, thread_id):
        events = []
        submitted = asyncio.create_task(
            client.send_message(session_id, thread_id, "ask before continuing", request_id="ask")
        )
        request_id = ""

        async def wait_for_question() -> None:
            nonlocal request_id
            async for frame in client.stream_events(session_id, thread_id):
                events.append(frame)
                if frame.kind == "permission_request":
                    # The configured policy asks before `ask_user` runs.
                    await client.respond_permission(
                        session_id,
                        thread_id,
                        request_id=frame.payload["interaction_id"],
                        decision="allow",
                    )
                elif frame.kind == "user_input_required":
                    request_id = str(frame.payload["interaction_id"])
                    return

        try:
            await asyncio.wait_for(wait_for_question(), timeout=20.0)
        except TimeoutError:
            pytest.fail(f"wait_for_question timed out; events={events!r}")

        assert request_id
        snapshot_response = await asyncio.wait_for(
            client.open_session(
                session_id=session_id,
                thread_id=thread_id,
                mode="resume",
            ),
            timeout=20.0,
        )
        snapshot = snapshot_response.data
        pending = snapshot.pending_interactions
        assert [item.kind for item in pending] == ["user_input_required"]
        assert pending[0].interaction_id == request_id
        assert pending[0].question == "Continue?"
        assert pending[0].resume_supported is True

        await client.respond_user_input(
            session_id, thread_id, request_id=request_id, answer="continue"
        )
        with pytest.raises(RuntimeError, match="interaction_no_longer_pending"):
            await client.respond_user_input(
                session_id, thread_id, request_id=request_id, answer="stop"
            )

        reconnected_events = []

        async def wait_for_turn_end() -> None:
            async for frame in client.stream_events(
                session_id, thread_id, after=snapshot.event_cursor
            ):
                events.append(frame)
                reconnected_events.append(frame)
                if frame.kind == "turn_ended":
                    return

        try:
            await asyncio.wait_for(wait_for_turn_end(), timeout=20.0)
        except TimeoutError:
            pytest.fail(f"wait_for_turn_end timed out; events={events!r}")
        await submitted

        recorded = [
            event for event in reconnected_events
            if event.kind == "user_input_recorded"
        ]
        tool_completed = [
            event for event in reconnected_events
            if event.kind == "tool_completed"
            and event.payload["call"]["id"] == "call_ask"
        ]
        assistant_completed = [
            event for event in reconnected_events
            if event.kind == "assistant_completed"
        ]
        turn_ended = [
            event for event in reconnected_events
            if event.kind == "turn_ended"
        ]
        assert len(recorded) == len(tool_completed) == 1
        assert len(assistant_completed) == len(turn_ended) == 1
        assert recorded[0].payload["interaction_id"] == request_id
        assert recorded[0].payload["resolution"] == {
            "kind": "answered",
            "answer": "continue",
        }
        assert assistant_completed[0].payload["content"] == "continued"
        assert reconnected_events.index(recorded[0]) < reconnected_events.index(
            tool_completed[0]
        ) < reconnected_events.index(assistant_completed[0]) < reconnected_events.index(
            turn_ended[0]
        )
        page = await client.list_messages(session_id, thread_id)
        assert [record.kind for record in page.items] == [
            "human_input",
            "assistant",
            "tool",
            "assistant",
        ]
        assert page.items[0].content == "ask before continuing"
        assert page.items[1].content == "asking"
        assert isinstance(page.items[2], ToolRecord)
        assert page.items[2].call.id == "call_ask"
        assert isinstance(page.items[2].outcome, ToolSucceeded)
        assert "continue" in "".join(
            part.text for part in page.items[2].outcome.output.parts
        )
        assert page.items[3].content == "continued"

    assert "user_input_required" in [event.kind for event in events]
    assert "turn_ended" in [event.kind for event in events]


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
    seen_payloads = []
    seen_permissions = []

    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
    ) as (client, session_id, thread_id):

        async def collect_events() -> list[Any]:
            collected = []
            submitted = asyncio.create_task(
                client.send_message(session_id, thread_id, "ask before continuing", request_id="ask")
            )
            async for frame in client.stream_events(session_id, thread_id):
                collected.append(frame)
                if frame.kind == "permission_request":
                    seen_permissions.append(frame.payload)
                    await client.respond_permission(
                        session_id,
                        thread_id,
                        request_id=frame.payload["interaction_id"],
                        decision="allow",
                    )
                elif frame.kind == "user_input_required":
                    seen_payloads.append(frame.payload)
                    await asyncio.sleep(0.2)
                    await client.respond_user_input(
                        session_id,
                        thread_id,
                        request_id=frame.payload["interaction_id"],
                        answer="continue",
                    )
                if frame.kind == "turn_ended":
                    break
            await submitted
            return collected

        try:
            events = await asyncio.wait_for(collect_events(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                f"ask_user stream did not finish; provider payloads={seen_payloads!r}"
            )

    assert len(seen_permissions) == 1
    assert seen_permissions[0]["interaction_id"] == "permission:call_ask"
    assert len(seen_payloads) == 1
    assert seen_payloads[0]["interaction_id"] == "user_input:call_ask"
    assert seen_payloads[0]["question"] == "Continue?"
    assert seen_payloads[0]["options"] == [
        {"label": "continue", "description": "Keep working."},
        {"label": "stop", "description": "Stop now."},
    ]
    assert any(event.kind == "user_input_recorded" for event in events)
    assert any(
        event.kind == "tool_completed"
        and event.payload.get("call", {}).get("id") == "call_ask"
        and event.payload.get("outcome", {}).get("kind") == "succeeded"
        for event in events
    )
    assert any(
        event.kind == "assistant_completed"
        and event.payload.get("content") == "continued"
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
                    "default_provider": "default",
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
                                    "max_output_tokens": 1024,
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
                "config": {"enabled": False, "resources": []},
            },
            {
                "id": "permissions",
                "name": "permissions",
                "config": {
                    "default_decision": "allow",
                    "rules": [
                        {
                            "tool_pattern": tool,
                            "param_patterns": {},
                            "decision": "ask",
                        }
                        for tool in ("ask_user", "request_permission", "edit")
                    ],
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
    app.state.manager = server.sessions
    app.state.paths = server.runtime_paths
    app.state.workspace_root = server.workspace_root
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "ok"}]),
    )
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
async def test_http_compact_command_commits_history_and_streams_typed_events(
    tmp_path: Path,
) -> None:
    """The command, durable history, and real SSE event wire stay aligned."""
    llm = MockLLM(responses=(
        [{"content": f"answer {index}"} for index in range(5)]
        + [{"content": "summary of the earlier discussion"}] * 2
    ))
    async with _real_client(
        tmp_path,
        llm=llm,
        sandbox_enabled=False,
        no_plugins=False,
        plugin_overlays=(
            {
                "id": "compact",
                "name": "compact",
                "config": {"automatic": False, "keep_recent_turns": 4},
            },
        ),
    ) as (client, session_id, thread_id):
        commands = await client.list_commands(session_id, thread_id)
        compact = next(command for command in commands.commands if command.name == "compact")
        assert compact.kind == "server"
        assert compact.usage == "/compact"

        cursor = 0
        for index in range(5):
            submitted = asyncio.create_task(
                client.send_message(
                    session_id,
                    thread_id,
                    f"question {index}",
                    request_id=f"turn-{index}",
                )
            )
            turn_events = []
            async with asyncio.timeout(5):
                async for event in client.stream_events(
                    session_id, thread_id, after=cursor
                ):
                    turn_events.append(event)
                    cursor = max(cursor, event.sequence)
                    if event.kind == "turn_ended":
                        break
            await submitted
            assert any(event.kind == "assistant_completed" for event in turn_events)

        result = await client.run_command(session_id, thread_id, raw="/compact")
        assert result.data.status == "ok"
        assert result.data.effects == ("history", "thread")
        assert "Conversation history compacted" in result.data.message

        streamed = []
        async with asyncio.timeout(5):
            async for event in client.stream_events(
                session_id, thread_id, after=cursor
            ):
                streamed.append(event)
                cursor = max(cursor, event.sequence)
                if event.kind == "compaction_completed":
                    break

        started = next(event for event in streamed if event.kind == "compaction_started")
        completed = next(
            event for event in streamed if event.kind == "compaction_completed"
        )
        assert started.payload["reason"] == "manual"
        assert completed.payload["reason"] == "manual"
        assert completed.payload["metrics"]["messages_removed"] > 0
        summary = completed.payload["summary"]["summary"]
        assert "summary of the earlier discussion" in summary
        assert not any(event.kind == "compaction_failed" for event in streamed)

        history = await client.list_messages(session_id, thread_id)
        summaries = [item for item in history.items if item.kind == "compaction_summary"]
        assert len(summaries) == 1
        assert "summary of the earlier discussion" in summaries[0].summary
        assert any(
            item.kind == "human_input" and item.content == "question 4"
            for item in history.items
        )


@pytest.mark.asyncio
async def test_http_goal_command_runs_the_evaluator_loop(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    """`/goal` sets a condition, starts a turn, and an evaluator model judges it."""
    skills_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[
            {"content": "Working toward shipping the API."},
            # The evaluator is a separate auxiliary call answering the verdict
            # contract; the working model never certifies its own completion.
            {"content": '{"verdict": "met", "reason": "API tests pass."}'},
        ]),
    )
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
    session_events = ctx.event_stream.subscribe()

    response = await skills_client.post(
        "/sessions/goal-state/threads/t/commands",
        json={"raw": "/goal ship the API"},
    )
    assert response.json()["data"]["message"].startswith("[active] ship the API")

    events = []
    achieved = None
    try:
        for _ in range(200):
            event = (
                await asyncio.wait_for(anext(session_events), timeout=2)
            ).event.model_dump(mode="json")
            events.append(event)
            if (
                event["kind"] == "goal_changed"
                and event["snapshot"]["state"]["kind"] == "achieved"
            ):
                achieved = event
                break
    except TimeoutError:
        pytest.fail(f"goal event stream timed out; observed={events!r}")
    await session_events.aclose()

    assert achieved is not None
    goal_state = achieved["snapshot"]["state"]
    assert goal_state["condition"] == "ship the API"
    assert goal_state["reason"] == "API tests pass."
    assert goal_state["progress"]["turns_evaluated"] == 1
    assert any(
        event["kind"] == "assistant_completed"
        and "Working toward shipping the API." in event["message"]["parts"][0]["text"]
        for event in events
    )
    for _ in range(20):
        if not ctx.turn_lock.locked():
            break
        await asyncio.sleep(0)
    get_response = await skills_client.post(
        "/sessions/goal-state/threads/t/commands",
        json={"raw": "/goal"},
    )
    assert get_response.json()["data"]["status"] == "ok"
    message = get_response.json()["data"]["message"]
    assert message.startswith("[achieved] ship the API")
    assert "Latest: API tests pass." in message


@pytest.mark.asyncio
async def test_goal_close_cancels_evaluator_and_resume_restarts_active_goal(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    import asyncio

    from XBotv2.core.stream import ModelCompleted
    from XBotv2.goal.models import AchievedGoal, GoalChanged

    class ResumeGoalLLM(MockLLM):
        def __init__(self):
            super().__init__(responses=[])
            self.first_evaluator_started = asyncio.Event()
            self.first_evaluator_cancelled = asyncio.Event()
            self.resumed_round_started = asyncio.Event()
            self.resumed_round_release = asyncio.Event()
            self.rounds = 0
            self.evaluations = 0

        async def _astream_once(self, request):
            self._state.request_history.append(request)
            text = "\n".join(
                part.text
                for message in request.messages
                for part in message.parts
                if isinstance(part, TextPart)
            )
            if "You judge whether a completion condition has been met." in text:
                self.evaluations += 1
                if self.evaluations == 1:
                    self.first_evaluator_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.first_evaluator_cancelled.set()
                        raise
                yield ModelCompleted(response=self.to_response({
                    "content": '{"verdict":"met","reason":"Verified after resume."}',
                }))
                return

            if '<system_reminder source="goal" event="round"' in text:
                self.rounds += 1
                if self.rounds == 2:
                    self.resumed_round_started.set()
                    await self.resumed_round_release.wait()
            yield ModelCompleted(response=self.to_response({
                "content": f"Goal work round {self.rounds}.",
            }))

    llm = ResumeGoalLLM()
    manager = skills_app.state.manager
    launch = {
        "session_id": "goal-close-resume",
        "thread_id": "t",
        "provider_name": "default",
        "workspace_root": skills_app.state.workspace_root,
        "no_plugins": False,
        "plugin_configs": {"goal": {"retry_seconds": 0.1}},
        "llm_override": llm,
    }
    runtime = await manager.open_session(**launch)
    first_events = runtime.event_stream.subscribe()
    resumed_events = None
    try:
        response = await skills_client.post(
            "/sessions/goal-close-resume/threads/t/commands",
            json={"raw": "/goal ship the API"},
        )
        assert response.status_code == 200
        await asyncio.wait_for(llm.first_evaluator_started.wait(), timeout=3)

        closed = await skills_client.post("/sessions/goal-close-resume/close")
        assert closed.status_code == 200
        await asyncio.wait_for(llm.first_evaluator_cancelled.wait(), timeout=3)
        await first_events.aclose()

        resumed = await manager.open_session(**(launch | {"mode": "resume"}))
        resumed_events = resumed.event_stream.subscribe()
        await asyncio.wait_for(llm.resumed_round_started.wait(), timeout=3)

        status = await skills_client.post(
            "/sessions/goal-close-resume/threads/t/commands",
            json={"raw": "/goal"},
        )
        assert status.status_code == 200
        assert status.json()["data"]["message"].startswith("[active] ship the API")

        llm.resumed_round_release.set()
        achieved = None
        async with asyncio.timeout(3):
            while achieved is None:
                event = (await anext(resumed_events)).event
                if (
                    isinstance(event, GoalChanged)
                    and isinstance(event.snapshot.state, AchievedGoal)
                ):
                    achieved = event.snapshot.state

        assert achieved.reason == "Verified after resume."
        assert achieved.progress.turns_evaluated == 1
        assert llm.rounds == 2
        assert llm.evaluations == 2
    finally:
        llm.resumed_round_release.set()
        if resumed_events is not None:
            await resumed_events.aclose()
        await manager.close_session("goal-close-resume", reason="test_cleanup")


@pytest.mark.asyncio
async def test_goal_close_cancels_scheduled_retry_timer(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    import asyncio

    from XBotv2.application.events import RUNTIME_EVENT
    from XBotv2.core.stream import ModelCompleted
    from XBotv2.goal.models import ActiveGoal, GoalChanged

    class RetryTimerLLM(MockLLM):
        def __init__(self):
            super().__init__(responses=[])
            self.retry_scheduled = asyncio.Event()
            self.rounds = 0
            self.evaluations = 0

        async def _astream_once(self, request):
            self._state.request_history.append(request)
            text = "\n".join(
                part.text
                for message in request.messages
                for part in message.parts
                if isinstance(part, TextPart)
            )
            if "You judge whether a completion condition has been met." in text:
                self.evaluations += 1
                yield ModelCompleted(response=self.to_response({
                    "content": "malformed evaluator response",
                }))
                return
            if '<system_reminder source="goal" event="round"' in text:
                self.rounds += 1
            yield ModelCompleted(response=self.to_response({
                "content": f"Goal work round {self.rounds}.",
            }))

    llm = RetryTimerLLM()
    manager = skills_app.state.manager
    runtime = await manager.open_session(
        session_id="goal-close-retry-timer",
        thread_id="t",
        provider_name="default",
        workspace_root=skills_app.state.workspace_root,
        no_plugins=False,
        plugin_configs={"goal": {"max_retries": 1, "retry_seconds": 0.2}},
        llm_override=llm,
    )

    def observe_goal(event):
        if (
            isinstance(event.event, GoalChanged)
            and isinstance(event.event.snapshot.state, ActiveGoal)
            and event.event.snapshot.state.progress.retries == 1
        ):
            llm.retry_scheduled.set()

    runtime.application.events.on(RUNTIME_EVENT, observe_goal)
    try:
        response = await skills_client.post(
            "/sessions/goal-close-retry-timer/threads/t/commands",
            json={"raw": "/goal finish the API"},
        )
        assert response.status_code == 200
        await asyncio.wait_for(llm.retry_scheduled.wait(), timeout=3)
        # The GoalChanged notification precedes task creation; allow the
        # evaluator-failure handler to finish scheduling its timer.
        await asyncio.sleep(0.02)

        closed = await skills_client.post(
            "/sessions/goal-close-retry-timer/close"
        )
        assert closed.status_code == 200
        await asyncio.sleep(0.25)

        assert llm.rounds == 1
        assert llm.evaluations == 1
        assert runtime.engine.pending_input_count == 0
    finally:
        await manager.close_session(
            "goal-close-retry-timer", reason="test_cleanup"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["met", "not_yet_met"])
async def test_terminal_goal_persists_usage_tool_and_todolist_stats(
    skills_client: httpx.AsyncClient,
    skills_app,
    verdict: str,
) -> None:
    import asyncio

    from XBotv2.core.stream import ModelCompleted
    from XBotv2.goal.models import ActiveGoal, AchievedGoal, GoalChanged, PausedGoal

    class GoalUsageLLM(MockLLM):
        def __init__(self):
            super().__init__(responses=[])
            self.work_requests = 0

        async def _astream_once(self, request):
            self._state.request_history.append(request)
            text = "\n".join(
                part.text
                for message in request.messages
                for part in message.parts
                if isinstance(part, TextPart)
            )
            if "You judge whether a completion condition has been met." in text:
                response = {
                    "content": (
                        '{"verdict":"'
                        f'{verdict}'
                        '","reason":"API verified."}'
                    ),
                    "usage_metadata": {"input_tokens": 23, "output_tokens": 4},
                }
            elif self.work_requests == 0:
                self.work_requests += 1
                response = {
                    "tool_calls": [{
                        "id": "goal-status",
                        "name": "get_goal",
                        "args": {},
                    }, {
                        "id": "create-task",
                        "name": "task_create",
                        "args": {"subject": "verify the API"},
                    }],
                    "usage_metadata": {"input_tokens": 101, "output_tokens": 7},
                }
            elif self.work_requests == 1:
                self.work_requests += 1
                response = {
                    "tool_calls": [{
                        "id": "complete-task",
                        "name": "task_update",
                        "args": {"taskId": "1", "status": "completed"},
                    }],
                    "usage_metadata": {"input_tokens": 5, "output_tokens": 1},
                }
            else:
                self.work_requests += 1
                response = {
                    "content": "The API is complete.",
                    "usage_metadata": {"input_tokens": 6, "output_tokens": 1},
                }
            yield ModelCompleted(response=self.to_response(response))

    llm = GoalUsageLLM()
    manager = skills_app.state.manager
    runtime = await manager.open_session(
        session_id="goal-usage-stats",
        thread_id="t",
        provider_name="default",
        workspace_root=skills_app.state.workspace_root,
        no_plugins=False,
        plugin_configs={"goal": {"max_rounds": 1}},
        llm_override=llm,
    )
    events = runtime.event_stream.subscribe()
    try:
        response = await skills_client.post(
            "/sessions/goal-usage-stats/threads/t/commands",
            json={"raw": "/goal finish the API"},
        )
        assert response.status_code == 200

        terminal = None
        goal_states = []
        async with asyncio.timeout(5):
            while terminal is None:
                event = (await anext(events)).event
                if isinstance(event, GoalChanged):
                    state = event.snapshot.state
                    goal_states.append(state)
                    if isinstance(state, (AchievedGoal, PausedGoal)):
                        terminal = state

        usage = runtime.application.usage.snapshot().total_counters
        assert usage.input == 135
        assert usage.output == 13
        if verdict == "met":
            assert isinstance(terminal, AchievedGoal)
        else:
            assert isinstance(terminal, PausedGoal)
        assert terminal.stats.input_tokens == usage.input
        assert terminal.stats.output_tokens == usage.output
        assert terminal.stats.tool_calls == 3
        assert terminal.stats.todo_items == 1
        assert terminal.stats.todo_completed == 1
        if verdict == "not_yet_met":
            updated_active = [
                state for state in goal_states
                if isinstance(state, ActiveGoal)
                and state.progress.turns_evaluated == 1
            ]
            assert len(updated_active) == 1
            assert updated_active[0].stats.input_tokens == usage.input
            assert updated_active[0].stats.output_tokens == usage.output
            assert updated_active[0].stats.tool_calls == 3
            assert updated_active[0].stats.todo_items == 1
            assert updated_active[0].stats.todo_completed == 1
    finally:
        await events.aclose()
        await manager.close_session("goal-usage-stats", reason="test_cleanup")


@pytest.mark.asyncio
async def test_http_goal_impossible_verdict_persists_failed_state(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    from XBotv2.goal.models import FailedGoal, GoalChanged

    skills_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[
            {"content": "The API cannot be shipped."},
            {"content": '{"verdict":"impossible","reason":"Required credentials are unavailable."}'},
        ]),
    )
    await skills_client.post(
        "/sessions", json={"session_id": "goal-failed", "thread_id": "t"}
    )
    ctx = await skills_app.state.manager.get("goal-failed", "t")
    events = ctx.event_stream.subscribe()
    response = await skills_client.post(
        "/sessions/goal-failed/threads/t/commands",
        json={"raw": "/goal ship the API"},
    )
    assert response.status_code == 200

    failed = None
    async with asyncio.timeout(5):
        while failed is None:
            event = (await anext(events)).event
            if isinstance(event, GoalChanged) and isinstance(
                event.snapshot.state, FailedGoal
            ):
                failed = event.snapshot.state
    await events.aclose()

    assert failed is not None
    assert failed.condition == "ship the API"
    assert failed.reason == "Required credentials are unavailable."
    assert failed.progress.turns_evaluated == 1
    status = await skills_client.post(
        "/sessions/goal-failed/threads/t/commands",
        json={"raw": "/goal"},
    )
    assert status.json()["data"]["message"].startswith("[failed] ship the API")


@pytest.mark.asyncio
async def test_http_goal_interrupt_pauses_and_persists_goal(
    skills_client: httpx.AsyncClient,
    skills_app,
) -> None:
    from XBotv2.core.stream import ModelCompleted
    from XBotv2.goal.models import GoalChanged, PausedGoal

    class BlockingGoalLLM(MockLLM):
        def __init__(self):
            super().__init__(responses=[])
            self.goal_started = asyncio.Event()

        async def _astream_once(self, request):
            self._state.request_history.append(request)
            user_text = "\n".join(
                part.text
                for message in request.messages
                for part in message.parts
                if isinstance(part, TextPart)
            )
            if '<system_reminder source="goal" event="round"' in user_text:
                self.goal_started.set()
                await asyncio.Event().wait()
            yield ModelCompleted(
                response=self.to_response({"content": "session title"})
            )

    llm = BlockingGoalLLM()
    skills_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
    await skills_client.post(
        "/sessions", json={"session_id": "goal-interrupted", "thread_id": "t"}
    )
    ctx = await skills_app.state.manager.get("goal-interrupted", "t")
    events = ctx.event_stream.subscribe()
    response = await skills_client.post(
        "/sessions/goal-interrupted/threads/t/commands",
        json={"raw": "/goal ship the API"},
    )
    assert response.status_code == 200
    await asyncio.wait_for(llm.goal_started.wait(), timeout=3)

    interrupted = await skills_client.post(
        "/sessions/goal-interrupted/threads/t/interrupt"
    )
    assert interrupted.status_code == 200
    assert interrupted.json()["cancelled"] is True
    async with asyncio.timeout(3):
        while ctx.turn_lock.locked():
            await asyncio.sleep(0)

    paused = None
    async with asyncio.timeout(3):
        while paused is None:
            event = (await anext(events)).event
            if isinstance(event, GoalChanged) and isinstance(
                event.snapshot.state, PausedGoal
            ):
                paused = event.snapshot.state
    await events.aclose()

    assert paused is not None
    assert paused.condition == "ship the API"
    assert paused.reason == "Interrupted."
    status = await skills_client.post(
        "/sessions/goal-interrupted/threads/t/commands",
        json={"raw": "/goal"},
    )
    assert status.json()["data"]["message"].startswith("[paused] ship the API")


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
            json={"raw": "/goal"},
        )
    finally:
        ctx.turn_lock.release()

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_http_command_reports_its_own_usage_error(
    skills_client: httpx.AsyncClient,
) -> None:
    """A line the command cannot parse is the command's answer, not a crash.

    The route resolves the *name* and hands the rest over untouched; parsing the
    arguments belongs to the command that understands them, and its failure is
    reported as a result the client can show (``guard_command``).
    """
    await skills_client.post(
        "/sessions", json={"session_id": "invalid-command", "thread_id": "t"}
    )

    response = await skills_client.post(
        "/sessions/invalid-command/threads/t/commands",
        json={"raw": "/permission set \"unterminated"},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "error"
    assert "syntax" in body["message"].lower()


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
    llm = MockLLM(responses=[
        {"content": "session title"},  # caption auto-titles the first message
        {"content": "expanded"},
    ])
    skills_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=llm,
    )
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

    await _submit_turn(skills_client, skills_app, "skill-prompt", "t", {
        "content": "/xbot-test-prompt verify boundaries",
    })
    # Call 0 is the automatic caption; the skill turn is the latest call.
    model_messages = llm.request_history[-1].messages
    expanded = next(
        message for message in model_messages if isinstance(message, ProviderUser)
    )
    expanded_text = "".join(
        part.text for part in expanded.parts if isinstance(part, TextPart)
    )
    invocation = ET.fromstring(expanded_text)
    assert invocation.tag == "skill_invocation"
    assert invocation.attrib["name"] == "xbot-test-prompt"
    assert "Follow this test instruction: verify boundaries" in (
        invocation.findtext("skill_instructions") or ""
    )
    assert invocation.findtext("user_arguments").strip() == "verify boundaries"
    assert all(
        not (
            isinstance(message, ProviderUser)
            and any(
                isinstance(part, TextPart)
                and part.text == "/xbot-test-prompt verify boundaries"
                for part in message.parts
            )
        )
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
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    kept_resources = [{"path": "/tmp/approved", "access": "readwrite"}]
    policy_path.write_text(
        yaml.safe_dump({
            "plugins": [
                {"id": "sandbox", "config": {"resources": kept_resources}},
            ],
        }),
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
    assert ctx.application._context.sandbox.network is False
    assert ctx.application._context.sandbox.external_read == "deny"

    # The session configuration was updated.
    assert policy_path.exists()
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    sandbox_entry = next(row for row in doc["plugins"] if row["id"] == "sandbox")
    assert sandbox_entry["config"]["network"] is False
    assert sandbox_entry["config"]["external_read"] == "deny"
    assert sandbox_entry["config"]["resources"] == kept_resources

    await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"remove_sandbox": ["network"]},
    )
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    sandbox_entry = next(row for row in doc["plugins"] if row["id"] == "sandbox")
    assert sandbox_entry["config"] == {
        "external_read": "deny",
        "resources": kept_resources,
    }

    await client.patch(
        "/sessions/sandbox-persist/policy",
        json={"remove_sandbox": ["external_read"]},
    )
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    sandbox_entry = next(row for row in doc["plugins"] if row["id"] == "sandbox")
    assert sandbox_entry["config"] == {"resources": kept_resources}

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
    assert resumed_ctx.application._context.sandbox.network is True


@pytest.mark.asyncio
async def test_http_plugin_config_catalog_is_schema_driven_and_revisioned(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    opened = await client.post(
        "/sessions", json={"session_id": "plugin-config", "thread_id": "t"}
    )
    assert opened.status_code == 200

    listed = await client.get(
        "/sessions/plugin-config/threads/t/plugin-config",
        params={"scope": "global"},
    )
    assert listed.status_code == 200
    catalog = listed.json()
    compact = next(item for item in catalog["plugins"] if item["plugin_id"] == "compact")
    assert compact["editable"] is True
    assert compact["config_schema"]["type"] == "object"
    llm = next(item for item in catalog["plugins"] if item["plugin_id"] == "llm")
    assert llm["editable"] is True
    assert llm["config_schema"]["type"] == "object"

    updated = await client.patch(
        "/sessions/plugin-config/threads/t/plugin-config/compact",
        params={"scope": "global"},
        json={"revision": catalog["revision"], "config": {"automatic": False}},
    )
    assert updated.status_code == 200
    compact = next(item for item in updated.json()["plugins"] if item["plugin_id"] == "compact")
    assert compact["scope_config"] == {"automatic": False}

    conflict = await client.patch(
        "/sessions/plugin-config/threads/t/plugin-config/compact",
        params={"scope": "global"},
        json={"revision": catalog["revision"], "config": {"automatic": True}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "plugin_config_conflict"


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
async def test_bounded_attach_on_a_brand_new_session(
    client: httpx.AsyncClient,
) -> None:
    """The windowed client always attaches with a limit.

    Older pages are read from the persisted transcript, so a bounded attach has
    to work before any transcript exists; otherwise every new session would open
    with a not-found error.
    """
    response = await client.post(
        "/sessions",
        json={
            "session_id": "bounded-fresh",
            "thread_id": "t",
            "history_limit": 50,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["history"]["items"] == []
    assert response.json()["data"]["history"]["older_cursor"] is None


@pytest.mark.asyncio
async def test_every_read_of_a_message_names_the_same_node(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """The identity a windowed client merges on, end to end.

    A client holds a window and later loads the pages before it. Merging them
    requires every read of one message to name it the same way: the attach
    response, the paged messages read, and the trajectory record all have to
    agree, because that name is the only thing they share.
    """
    http_app.state.manager.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "first answer"}]),
    )
    await client.post("/sessions", json={"session_id": "identity", "thread_id": "t"})
    await _submit_turn(client, http_app, "identity", "t", {"content": "first"})

    latest = await client.get(
        "/sessions/identity/threads/t/messages", params={"limit": 1}
    )
    assert latest.status_code == 200
    newest = latest.json()
    assert [item["content"] for item in newest["items"]] == ["first answer"]
    newest_id = newest["items"][0]["id"]

    older = await client.get(
        "/sessions/identity/threads/t/messages",
        params={"limit": 1, "cursor": newest["older_cursor"]},
    )
    assert [item["content"] for item in older.json()["items"]] == ["first"]
    oldest_id = older.json()["items"][0]["id"]

    reopened = await client.post(
        "/sessions",
        json={
            "session_id": "identity",
            "thread_id": "t",
            "mode": "resume",
            "history_limit": 1,
        },
    )
    assert [
        item["id"] for item in reopened.json()["data"]["history"]["items"]
    ] == [newest_id]

    trajectory = await client.get(
        "/sessions/identity/threads/t/trajectory", params={"limit": 10}
    )
    recorded = [
        item["message"]["id"]
        for item in trajectory.json()["page"]["items"]
        if item["kind"] == "message_appended"
    ]
    assert recorded == [oldest_id, newest_id]
