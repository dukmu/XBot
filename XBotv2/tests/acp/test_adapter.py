from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any

import yaml
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

from XBotv2.acp_plugin.xbot_agent import XBotACPAgent
from XBotv2.acp_plugin.events import ACPEventMapper, replay_history
from XBotv2.session.history import conversation_replay
from XBotv2.session.event_stream import SessionEventFrame
from XBotv2.application.acp import start_acp_application
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ToolCall
from XBotv2.llm.mock import MockLLM
from XBotv2.session import OpenedSession, SessionSnapshot, SessionStreamEvent, ThreadSnapshot


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


class FakeSessions:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.last_open = None
        self.messages: list[Message] = []
        self.interaction_responses: list[tuple[str, tuple[Any, ...]]] = []
        self.event_queue: asyncio.Queue[SessionEventFrame | None] = asyncio.Queue()
        self.event_sequence = 0

    async def open(self, request):
        self.last_open = request
        return OpenedSession(
            session_id=request.session_id or "session-1",
            thread_id="agent",
            agent_name="",
            workspace_root=request.workspace_root,
            provider="default",
            model="test",
            model_mode="",
            context_window=200_000,
            usage={},
            history=tuple(self.messages),
            status_slots={},
            event_cursor=0,
        )

    async def list_sessions(self):
        return (SessionSnapshot(
            "session-1",
            "active",
            workspace_root="/workspace",
            title="session-1",
        ),)

    async def session_summary(self, session_id: str):
        return (await self.list_sessions())[0]

    async def thread_summary(self, session_id: str, thread_id: str):
        return ThreadSnapshot(
            session_id,
            thread_id,
            "active",
            provider="default",
            context_window=200_000,
        )

    async def stream_message(self, request):
        assert request.content == "hello"
        assert request.request_id == "acp:session-1"
        assert not request.images

        async def stream():
            for event in self.events:
                value = SessionStreamEvent.from_mapping(event)
                self.event_sequence += 1
                await self.event_queue.put(SessionEventFrame(
                    self.event_sequence,
                    request.request_id,
                    value,
                ))
                yield value

        return stream()

    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ):
        del after
        async def stream():
            while True:
                event = await self.event_queue.get()
                if event is None:
                    return
                yield event

        return stream()

    async def dispatch(self, session_id, thread_id, operation, request):
        del session_id, thread_id, request
        if operation.name == "commands/list":
            return SimpleNamespace(commands=())
        if operation.name == "agents/list":
            return SimpleNamespace(active="", agents=())
        if operation.name == "llm/providers/list":
            return SimpleNamespace(
                default="default",
                providers=(SimpleNamespace(name="default"),),
            )
        if operation.name == "commands/execute":
            return SimpleNamespace(message="done")
        return SimpleNamespace()

    async def messages(self, session_id: str, thread_id: str):
        return tuple(self.messages)

    async def fork_session(self, session_id: str) -> str:
        return "forked"

    async def interrupt(self, session_id: str, thread_id: str):
        return SimpleNamespace(cancelled=True)

    async def respond_permission(self, *args, **kwargs):
        self.interaction_responses.append(("permission", args))
        return SimpleNamespace()

    async def respond_user_input(self, *args, **kwargs):
        self.interaction_responses.append(("user_input", args))
        return SimpleNamespace()

    async def cancel_interaction(self, *args, **kwargs):
        self.interaction_responses.append(("cancel", args))
        return SimpleNamespace()

    async def close_session(self, session_id: str) -> None:
        assert session_id == "session-1"
        await self.event_queue.put(None)


def _agent(tmp_path, events: list[dict[str, Any]]) -> tuple[XBotACPAgent, FakeConnection]:
    sessions = FakeSessions(events)
    agent = XBotACPAgent(
        sessions=sessions,
        provider_name="default",
        no_plugins=True,
    )
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

    replayed = replay_history(conversation_replay([
        Message(role="user", content="inspect"),
        Message(
            role="user",
            content="background task completed",
            input_id="runtime-1",
            additional_kwargs={
                "runtime_input": {"source": "task-1", "event": "notification"}
            },
        ),
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
            data={"exit_code": 0},
            artifact=[ArtifactRef(id="tool_results/out.txt")],
        ),
    ]))
    assert [update.session_update for update in replayed] == [
        "user_message_chunk",
        "tool_call",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
    ]
    assert replayed[1].title == "Injected context · task-1 / notification"
    assert replayed[-1].raw_output == {
        "content": "/workspace",
        "data": {"exit_code": 0},
        "error": None,
        "artifacts": [ArtifactRef(id="tool_results/out.txt").to_dict()],
        "images": [],
    }


async def test_interactions_use_acp_permission_and_elicitation(tmp_path) -> None:
    agent, _ = _agent(tmp_path, [])
    permission_event = SessionStreamEvent.from_mapping({
            "type": "permission_request",
            "data": {
                "request_id": "permission-1",
                "tool_call": {
                    "id": "call-1",
                    "name": "shell",
                    "args": {"command": "pwd"},
                },
            },
        })
    user_input_event = SessionStreamEvent.from_mapping({
            "type": "user_input_required",
            "data": {
                "request_id": "question-1",
                "question": "Choose",
                "options": [
                    {"label": "first"},
                    {"label": "second"},
                ],
            },
        })
    permission = await agent._handle_interaction("session-1", permission_event)
    answer = await agent._handle_interaction("session-1", user_input_event)
    await agent._resolve_interaction("session-1", permission_event)
    await agent._resolve_interaction("session-1", user_input_event)

    assert permission == {
        "request_id": "permission-1",
        "status": "answered",
        "decision": "allow",
        "scope": "session",
    }
    assert answer["answer"] == "second"
    host = agent.sessions
    assert isinstance(host, FakeSessions)
    assert [kind for kind, _ in host.interaction_responses] == [
        "permission",
        "user_input",
    ]


class ProtocolClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, **_: Any) -> None:
        self.updates.append(_["update"])


async def test_official_sdk_jsonrpc_prompt_flow(tmp_path) -> None:
    host = FakeSessions([
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
    ])
    agent = XBotACPAgent(
        sessions=host,
        provider_name="default",
        no_plugins=False,
    )
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
        assert host.last_open.plugin_configs == {
            "mcp_plugin": {
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
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([{
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
        }]),
        encoding="utf-8",
    )
    context = await start_acp_application(
        paths=RuntimePaths.from_data_dir(data_dir),
        provider_name="default",
        no_plugins=True,
        selected_agent=None,
        llm_override=MockLLM([{
            "content": "real runtime",
            "usage_metadata": {
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
            },
        }]),
    )
    agent = context.acp_agent
    connection = FakeConnection()
    agent.on_connect(connection)
    await agent.initialize(PROTOCOL_VERSION, ClientCapabilities())
    try:
        session = await agent.new_session(str(workspace))
        assert session.config_options is not None
        assert [option.id for option in session.config_options] == [
            "agent",
            "provider",
        ]
        response = await agent.prompt(
            session.session_id,
            [text_block("hello")],
        )
        forked = await agent.fork_session(
            session.session_id,
            str(workspace),
        )
        forked_messages = await agent.sessions.messages(
            forked.session_id,
            "agent",
        )
    finally:
        await context.destroy()

    assert response.stop_reason == "end_turn"
    assert response.usage is not None
    assert response.usage.total_tokens == 6
    content_updates = [
        update
        for _, update in connection.updates
        if getattr(update, "content", None) is not None
    ]
    assert content_updates[0].content.text == "real runtime"
    assert [message.content for message in forked_messages] == [
        "hello",
        "real runtime",
    ]
