from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any

from acp import PROTOCOL_VERSION, connect_to_agent, run_agent, text_block
from acp.schema import (
    AcceptElicitationResponse,
    AllowedOutcome,
    ClientCapabilities,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    EnvVariable,
    McpServerStdio,
    RequestPermissionResponse,
)

from XBotv2.acp.xbot_agent import XBotACPAgent
from XBotv2.acp.events import ACPEventMapper, replay_history
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ToolCall
from XBotv2.llm.mock import MockLLM


class FakeConnection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, **_: Any) -> RequestPermissionResponse:
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id="allow_session",
            )
        )

    async def create_elicitation(self, **_: Any) -> AcceptElicitationResponse:
        return AcceptElicitationResponse(
            action="accept",
            content={"answer": "second"},
        )


class FakeRuntime:
    session_id = "session-1"
    thread_id = "agent"
    provider_name = "default"

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.engine = FakeEngine()
        self.interrupted = False
        self.session_events: asyncio.Queue | None = None

    async def stream_message(self, content: str, request_id: str, *, images=None):
        assert content == "hello"
        assert request_id == "acp:session-1"
        assert not images
        for event in self.events:
            yield event

    def request_interrupt(self) -> bool:
        self.interrupted = True
        return True

    def attach_event_stream(self) -> asyncio.Queue:
        self.session_events = asyncio.Queue()
        return self.session_events

    def detach_event_stream(self, events: asyncio.Queue) -> None:
        if self.session_events is events:
            self.session_events = None


class FakeManager:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.last_open: dict[str, Any] = {}

    async def get(self, session_id: str, thread_id: str) -> FakeRuntime:
        assert (session_id, thread_id) == ("session-1", "agent")
        return self.runtime

    async def open_session(self, **kwargs: Any) -> FakeRuntime:
        self.last_open = kwargs
        return self.runtime

    async def close_all(self) -> None:
        if self.runtime.session_events is not None:
            await self.runtime.session_events.put(None)

    async def close_session(self, session_id: str) -> None:
        assert session_id == "session-1"
        await self.close_all()


class FakeEngine(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(context_window=200_000, plugin_loader=None)
        self.client_event_sink = None

    def set_client_event_sink(self, sink: Any) -> Any:
        previous = self.client_event_sink
        self.client_event_sink = sink
        return previous


def _agent(tmp_path, events: list[dict[str, Any]]) -> tuple[XBotACPAgent, FakeConnection]:
    agent = XBotACPAgent(
        paths=RuntimePaths.from_data_dir(tmp_path),
        provider_name="default",
        no_plugins=True,
    )
    agent.manager = FakeManager(FakeRuntime(events))  # type: ignore[assignment]
    connection = FakeConnection()
    agent.on_connect(connection)
    agent.client_capabilities = ClientCapabilities(
        elicitation=ElicitationCapabilities(
            form=ElicitationFormCapabilities()
        )
    )
    return agent, connection


def test_event_mapper_preserves_stream_and_structured_updates() -> None:
    mapper = ACPEventMapper(context_size=200_000)
    updates = []
    for event in [
        {"type": "assistant_message_delta", "data": {"content": "hello"}},
        {"type": "assistant_message", "data": {"content": "hello"}},
        {
            "type": "tool_calls_started",
            "data": {
                "tool_calls": [
                    {"id": "call-1", "name": "shell", "args": {"command": "pwd"}}
                ]
            },
        },
        {
            "type": "tool_result",
            "data": {
                "tool_call_id": "call-1",
                "name": "shell",
                "content": "/workspace",
                "status": "success",
            },
        },
        {
            "type": "usage",
            "data": {
                "input_tokens": 90,
                "output_tokens": 10,
                "total_tokens": 100,
                "context_tokens": 80,
            },
        },
        {
            "type": "task_updated",
            "data": {
                "task_id": "task-1",
                "kind": "shell",
                "command": "pytest",
                "status": "completed",
                "output": "passed",
            },
        },
    ]:
        updates.extend(mapper.updates(event))

    assert [update.session_update for update in updates] == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "usage_update",
        "tool_call",
    ]
    assert updates[-2].used == 80
    assert updates[-2].size == 200_000
    assert updates[-1].status == "completed"

    replayed = replay_history([
        Message(role="user", content="inspect"),
        Message(
            role="assistant",
            reasoning="checking",
            tool_calls=[ToolCall("call-1", "shell", {"command": "pwd"})],
        ),
        Message(
            role="tool",
            content="/workspace",
            tool_call_id="call-1",
            status="success",
        ),
    ])
    assert [update.session_update for update in replayed] == [
        "user_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
    ]


async def test_interactions_use_acp_permission_and_elicitation(tmp_path) -> None:
    agent, _ = _agent(tmp_path, [])
    permission = await agent._handle_interaction(
        "session-1",
        {
            "type": "permission_request",
            "data": {
                "request_id": "permission-1",
                "tool_call": {
                    "id": "call-1",
                    "name": "shell",
                    "args": {"command": "pwd"},
                },
            },
        },
    )
    answer = await agent._handle_interaction(
        "session-1",
        {
            "type": "user_input_required",
            "data": {
                "request_id": "question-1",
                "question": "Choose",
                "options": [
                    {"label": "first"},
                    {"label": "second"},
                ],
            },
        },
    )

    assert permission == {
        "request_id": "permission-1",
        "status": "answered",
        "decision": "allow",
        "scope": "session",
    }
    assert answer["answer"] == "second"


class ProtocolClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, **_: Any) -> None:
        self.updates.append(_["update"])


async def test_official_sdk_jsonrpc_prompt_flow(tmp_path) -> None:
    agent = XBotACPAgent(
        paths=RuntimePaths.from_data_dir(tmp_path),
        provider_name="default",
        no_plugins=False,
    )
    manager = FakeManager(FakeRuntime([
        {"type": "assistant_message_delta", "data": {"content": "done"}},
        {
            "type": "usage",
            "data": {
                "input_tokens": 4,
                "output_tokens": 1,
                "total_tokens": 5,
                "context_tokens": 4,
            },
        },
        {"type": "turn_finished", "data": {"turn": 1}},
    ]))
    agent.manager = manager  # type: ignore[assignment]
    left, right = socket.socketpair()
    agent_reader, agent_writer = await asyncio.open_connection(sock=left)
    client_reader, client_writer = await asyncio.open_connection(sock=right)
    task = asyncio.create_task(
        run_agent(
            agent,
            input_stream=agent_writer,
            output_stream=agent_reader,
        )
    )
    protocol_client = ProtocolClient()
    connection = connect_to_agent(
        protocol_client,
        client_writer,
        client_reader,
    )
    try:
        response = await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
        )
        assert response.protocol_version == PROTOCOL_VERSION
        assert response.agent_info is not None
        assert response.agent_info.name == "xbot"
        session = await connection.new_session(
            cwd=str(tmp_path),
            mcp_servers=[
                McpServerStdio(
                    name="example",
                    command="example-mcp",
                    args=["--stdio"],
                    env=[EnvVariable(name="MODE", value="test")],
                )
            ],
        )
        assert session.session_id == "session-1"
        assert manager.last_open["plugin_configs"] == {
            "mcp": {
                "servers": {
                    "example": {
                        "type": "local",
                        "command": ["example-mcp", "--stdio"],
                        "cwd": str(tmp_path),
                        "env": {"MODE": "test"},
                        "required": True,
                    }
                }
            }
        }
        prompt = await connection.prompt(
            session_id=session.session_id,
            prompt=[text_block("hello")],
        )
        assert prompt.stop_reason == "end_turn"
        assert prompt.usage is not None
        assert prompt.usage.total_tokens == 5
        assert [update.session_update for update in protocol_client.updates] == [
            "agent_message_chunk",
            "usage_update",
        ]
    finally:
        await connection.close()
        await asyncio.wait_for(task, timeout=2)
        await agent.close()


async def test_adapter_uses_real_xbot_session_runtime(tmp_path) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "providers.yaml").write_text(
        "default: default\n"
        "providers:\n"
        "  default:\n"
        "    provider: openai\n"
        "    model: test\n"
        "    base_url: http://test\n"
        "    api_key: test\n"
        "    max_context_tokens: 4096\n",
        encoding="utf-8",
    )
    agent = XBotACPAgent(
        paths=RuntimePaths.from_data_dir(data_dir),
        provider_name="default",
        no_plugins=True,
        llm_override=MockLLM([{
            "content": "real runtime",
            "usage_metadata": {
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
            },
        }]),
    )
    connection = FakeConnection()
    agent.on_connect(connection)
    await agent.initialize(PROTOCOL_VERSION, ClientCapabilities())
    try:
        session = await agent.new_session(str(workspace))
        assert session.config_options is not None
        assert [option.id for option in session.config_options] == ["provider"]
        response = await agent.prompt(
            session.session_id,
            [text_block("hello")],
        )
        forked = await agent.fork_session(
            session.session_id,
            str(workspace),
        )
        forked_runtime = await agent.manager.get(forked.session_id, "agent")
    finally:
        await agent.close()

    assert response.stop_reason == "end_turn"
    assert response.usage is not None
    assert response.usage.total_tokens == 6
    assert connection.updates[0][1].content.text == "real runtime"
    assert [message.content for message in forked_runtime.engine.messages] == [
        "hello",
        "real runtime",
    ]
