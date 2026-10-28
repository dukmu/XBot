from __future__ import annotations

import asyncio
import pytest
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
from XBotv2.acp_plugin.impl.xbot_agent import XBotACPAgent
from XBotv2.acp_plugin.impl.events import ACPEventMapper, replay_history
from XBotv2.session.api.contracts import OpenedThread, SessionEventFrame, SessionKey
from XBotv2.session.api.records import conversation_replay
from XBotv2.main import open_runtime
from XBotv2.acp_plugin.api.contracts import ACP_SERVER
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.llm.api.contracts import LLM
from xcore.api.context import Context
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.usage.api.service import USAGE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.artifacts import ArtifactRef
from XBotv2.core.api.domain import (
    ToolTiming,
    AgentExecutionLimits,
    CompletedStop,
    GenerationSettings,
    MessageId,
    ModelExchange,
    ModelRoute,
    ModelTiming,
    ProviderExtensions,
    ProviderMeasured,
    RequestObservation,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    SessionScope,
    StandardGenerationMode,
    StructuredToolOutput,
    TokenCounters,
    TurnId,
    TurnRequest,
    TurnScope,
    UsageDelta,
    UsageSnapshot,
)
from XBotv2.core.api.history import HistoryPage
from XBotv2.core.api.messages import (
    AssistantMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.api.parts import ReasoningPart, TextPart
from XBotv2.core.api.metadata import ThreadMetadata
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import (
    ContinueTurn,
    ToolCall,
    ToolCallRef,
    ToolExecution,
    ToolOutput,
    ToolSucceeded,
    text_output,
)
from XBotv2.agentloop.api.outputs import (
    AssistantCompleted,
    AssistantTextDelta,
    LoopTurnEnded,
    LoopTurnStarted,
    StartedToolCall,
    ToolCallsStarted,
    ToolCompleted,
    TurnFinished,
    UsageObserved,
    is_loop_event,
)
from XBotv2.jobs.api.contracts import JobView
from XBotv2.jobs.api.protocol import JobUpdatedEvent
from XBotv2.interactions.api.models import Answered, UserInputOption, UserInputRequest
from XBotv2.permissions.api.models import Allowed, PermissionRequest, ToolPermission
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.api.contracts import SessionSummary, ThreadSummary


def _exchange() -> ModelExchange:
    return ModelExchange(
        observation=RequestObservation(
            selection=_selection().model,
            purpose=TurnRequest(turn_id=TurnId("turn-1")),
            estimated_input_tokens=0,
            observed_context=ProviderMeasured(tokens=0),
        ),
        usage=UsageDelta(counters=TokenCounters()),
        timing=ModelTiming(total_ms=0),
        stop=CompletedStop(),
        provider_extensions=ProviderExtensions(provider="default"),
    )


def _selection() -> ResolvedRuntimeSelection:
    return ResolvedRuntimeSelection(
        agent_name="default",
        prompt="",
        limits=AgentExecutionLimits(),
        enabled_tools=(),
        model=ResolvedModelSelection(
            route=ModelRoute(provider="default", model="test"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(),
                max_output_tokens=200_000,
            ),
            context_window=200_000,
        ),
    )


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
        return await self.locate(request.session_id or "session-1", "main", workspace=request.workspace_root)

    async def locate(self, session_id, thread_id, *, workspace="/workspace"):
        if not hasattr(self, "scope"):
            self.scope = Context()
            host = self
            class Loop:
                def pending_inputs(self):
                    return ()
                async def invoke(self, operation, request):
                    return await host.dispatch(session_id, thread_id, operation, request)
                async def submit_message(self, request):
                    return await host.send_message(request)
                async def interrupt(self):
                    return True
            class Events:
                sequence = 0
                def subscribe(self, after=None):
                    async def stream():
                        while True:
                            event = await host.event_queue.get()
                            if event is None:
                                return
                            yield event
                    return stream()
            class Clients:
                def pending_interactions(self):
                    return ()
                def resolve(self, request_id, resolution):
                    host.interaction_responses.append(("permission" if isinstance(resolution, Allowed) else "user_input", (request_id, resolution)))
                def cancel(self, *args, **kwargs):
                    host.interaction_responses.append(("cancel", args))
            self.scope.provide(ARCHIVE, self)
            self.scope.provide(AGENT_LOOP, Loop())
            self.scope.provide(THREAD_EVENTS, Events())
            self.scope.provide(CLIENT_EVENTS, Clients())
            self.scope.provide(USAGE, SimpleNamespace(snapshot=lambda: UsageSnapshot()))
            self.scope.provide(LOOP_STATE, SimpleNamespace(session=SimpleNamespace(key=SessionKey(session_id, "main"), session_id=session_id, thread_id="main"), metadata=SimpleNamespace(value=ThreadMetadata(runtime_selection=_selection(), workspace_root=workspace)), history=SimpleNamespace(snapshot=lambda: tuple(self.messages))))
            await self.scope.start()
        return self.scope

    async def message_page(self, session_id, thread_id, *, cursor=None, limit=None):
        return HistoryPage(items=tuple(self.messages), older_cursor=None)

    async def list_sessions(self, *, limit=100, cursor=None, workspace_root=None):
        return SimpleNamespace(items=(SessionSummary(
            session_id="session-1",
            status="active",
            workspace_root="/workspace",
            title="session-1",
        ),), next_cursor=None)

    async def session_summary(self, session_id: str):
        return (await self.list_sessions()).items[0]

    async def thread_summary(self, session_id: str, thread_id: str):
        return ThreadSummary(
            session_id = session_id,
            thread_id = thread_id,
            status = "active",
            provider="default",
            context_window=200_000,
        )

    async def send_message(self, request):
        assert request.content == "hello"
        assert request.request_id == "acp:session-1"
        assert not request.media
        turn_id = TurnId("turn-1")
        for event in self.events:
            self.event_sequence += 1
            await self.event_queue.put(SessionEventFrame(
                sequence=self.event_sequence,
                scope=(
                    TurnScope(turn_id)
                    if is_loop_event(event)
                    else SessionScope()
                ),
                event=event,
            ))
        return None

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
        archive=sessions,
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
        LoopTurnStarted(turn=1),
        AssistantTextDelta(text="hello"),
        AssistantCompleted(message=AssistantMessage(
            id=MessageId("assistant-1"),
            parts=(TextPart(text="hello"),),
            exchange=_exchange(),
        )),
        ToolCallsStarted(calls=(StartedToolCall(
            call=ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
            category="execute",
        ),)),
        ToolCompleted(execution=ToolExecution(
            message=ToolMessage(
                id=MessageId("tool-1"),
                call=ToolCallRef(id="call-1", name="shell"),
                outcome=ToolSucceeded(output=text_output("/workspace")),
                timing=ToolTiming(duration_ms=0),
            ),
            directive=ContinueTurn(),
        )),
        UsageObserved(usage=UsageDelta(counters=TokenCounters(
            input=80,
            output=10,
        ))),
        JobUpdatedEvent(view=JobView(
            id="task-1",
            kind="shell",
            label="pytest",
            state="succeeded",
            elapsed_ms=0,
            summary="passed",
        )),
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
        HumanInputMessage(
            id=MessageId("1"),
            parts=(TextPart(text="inspect"),),
        ),
        RuntimeNoticeMessage(
            id=MessageId("2"),
            source="task-1",
            event="notification",
            parts=(TextPart(text="background task completed"),),
        ),
        AssistantMessage(
            id=MessageId("3"),
            parts=(
                ReasoningPart(text="checking"),
                ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
            ),
            exchange=_exchange(),
        ),
        ToolMessage(
            id=MessageId("4"),
            call=ToolCallRef(id="call-1", name="shell"),
            outcome=ToolSucceeded(output=ToolOutput(
                parts=(TextPart(text="/workspace"),),
                structured=StructuredToolOutput(kind="exit_code"),
                artifacts=(ArtifactRef(id="tool_results/out.txt"),),
            )),
            timing=ToolTiming(duration_ms=0),
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
    output = replayed[-1].raw_output["output"]
    assert output["parts"][0]["text"] == "/workspace"
    assert output["artifacts"] == [
        ArtifactRef(id="tool_results/out.txt").model_dump(mode="json")
    ]


async def test_interactions_use_acp_permission_and_elicitation(tmp_path) -> None:
    agent, _ = _agent(tmp_path, [])
    permission_event = PermissionRequest(
        interaction_id="permission-1",
        source="tool",
        subject=ToolPermission(
            tool_call=ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
        ),
        reason="run shell",
    )
    user_input_event = UserInputRequest(
        interaction_id="question-1",
        source="tool",
        question="Choose",
        options=(
            UserInputOption(label="first", description="the first"),
            UserInputOption(label="second", description="the second"),
        ),
    )
    permission = await agent._handle_interaction("session-1", permission_event)
    answer = await agent._handle_interaction("session-1", user_input_event)
    await agent._resolve_interaction("session-1", permission_event)
    await agent._resolve_interaction("session-1", user_input_event)

    assert permission == Allowed(scope="session")
    assert answer == Answered(answer="second")
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
        AssistantTextDelta(text="done"),
        UsageObserved(usage=UsageDelta(counters=TokenCounters(
            input=4,
            output=1,
        ))),
        LoopTurnEnded(
            turn=1,
            outcome=TurnFinished(stop_reason="completed"),
        ),
    ])
    agent = XBotACPAgent(
        sessions=host,
        archive=host,
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
        }]),
        encoding="utf-8",
    )
    context = await open_runtime(paths=RuntimePaths.from_data_dir(data_dir), profile="acp", overrides=PluginOverlay.parse([{"id": "acp", "config": {"provider_name": "default", "no_plugins": True}}]))
    providers = context.require(LLM)
    providers.unregister("openai")
    providers.register("openai", lambda *_: MockLLM([{"content": "real runtime", "usage_metadata": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}]))
    agent = context.require(ACP_SERVER)
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
        for option in session.config_options:
            changed = await agent.set_config_option(option.id, session.session_id, option.current_value)
            assert changed.config_options
        response = await agent.prompt(
            session.session_id,
            [text_block("hello")],
        )
        before_resume = len(connection.updates)
        resumed = await agent.resume_session(session.session_id, str(workspace))
        assert resumed.config_options
        assert len(connection.updates) == before_resume
        loaded = await agent.load_session(str(workspace), session.session_id)
        assert loaded.config_options
        replayed = [
            update.content.text for _, update in connection.updates[before_resume:]
            if getattr(update, "content", None) is not None
        ]
        assert replayed == ["hello", "real runtime"]
        forked = await agent.fork_session(
            session.session_id,
            str(workspace),
        )
        forked_messages = await context.require(ARCHIVE).surface(forked.session_id, "main")
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
    assert [message.parts[0].text for message in forked_messages] == [
        "hello",
        "real runtime",
    ]


@pytest.mark.asyncio
async def test_prompt_is_released_when_the_session_stream_ends() -> None:
    """A session stream that ends without a turn terminal frame must release
    the active prompt instead of hanging its waiter and the prompt slot."""
    from XBotv2.acp_plugin.impl.xbot_agent import ActivePrompt, XBotACPAgent
    from XBotv2.acp_plugin.impl.events import ACPEventMapper

    class ThreadSummary:
        context_window = 4096

    class Sessions:
        async def thread_summary(self, session_id, thread_id):
            del session_id, thread_id
            return ThreadSummary()

    agent = XBotACPAgent(sessions=Sessions(), archive=None, provider_name="default")
    prompt = ActivePrompt("acp:s", ACPEventMapper(context_size=4096), asyncio.Event())
    agent._active_prompts["s"] = prompt

    class EmptySubscription:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    events = EmptySubscription()
    await agent._forward_session_events("s", events)

    assert prompt.completed.is_set()
    assert isinstance(prompt.failure, RuntimeError)
    assert events.closed


@pytest.mark.asyncio
async def test_prompt_notification_failure_closes_stream_and_releases_prompt() -> None:
    from XBotv2.acp_plugin.impl.xbot_agent import ActivePrompt, XBotACPAgent

    class FailingConnection:
        async def session_update(self, **_):
            raise RuntimeError("client disconnected")

    class Sessions:
        async def thread_summary(self, session_id, thread_id):
            del session_id, thread_id
            return SimpleNamespace(context_window=4096)

    class OneFrameSubscription:
        def __init__(self):
            self.closed = False
            self._frame = SessionEventFrame(
                sequence=1,
                scope=SessionScope(),
                event=AssistantTextDelta(text="hi"),
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._frame is None:
                raise StopAsyncIteration
            frame, self._frame = self._frame, None
            return frame

        async def aclose(self):
            self.closed = True

    agent = XBotACPAgent(sessions=Sessions(), archive=None, provider_name="default")
    agent.connection = FailingConnection()
    prompt = ActivePrompt("acp:s", ACPEventMapper(context_size=4096), asyncio.Event())
    agent._active_prompts["s"] = prompt
    events = OneFrameSubscription()

    with pytest.raises(RuntimeError, match="client disconnected"):
        await agent._forward_session_events("s", events)

    assert events.closed
    assert prompt.completed.is_set()
    assert isinstance(prompt.failure, RuntimeError)


@pytest.mark.asyncio
async def test_cancelled_event_forwarding_closes_stream_and_releases_prompt() -> None:
    from XBotv2.acp_plugin.impl.xbot_agent import ActivePrompt, XBotACPAgent

    class Sessions:
        async def thread_summary(self, session_id, thread_id):
            del session_id, thread_id
            return SimpleNamespace(context_window=4096)

    class BlockingSubscription:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def aclose(self):
            self.closed = True

    agent = XBotACPAgent(sessions=Sessions(), archive=None, provider_name="default")
    prompt = ActivePrompt("acp:s", ACPEventMapper(context_size=4096), asyncio.Event())
    agent._active_prompts["s"] = prompt
    events = BlockingSubscription()
    task = asyncio.create_task(agent._forward_session_events("s", events))
    await events.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events.closed
    assert prompt.completed.is_set()
    assert isinstance(prompt.failure, RuntimeError)


def test_prompt_image_maps_to_media_input():
    from types import SimpleNamespace
    from XBotv2.acp_plugin.impl.xbot_agent import _prompt_content

    text, media = _prompt_content([
        SimpleNamespace(type="text", text="describe"),
        SimpleNamespace(type="image", data="aW1hZ2U=", mime_type="image/png"),
    ])
    assert text == "describe"
    assert len(media) == 1
    assert media[0].model_dump() == {
        "data": "aW1hZ2U=", "media_type": "image/png", "name": "",
    }

async def test_list_sessions_passes_cursor_and_workspace_to_session_owner(tmp_path):
    agent, _ = _agent(tmp_path, [])
    calls = []

    async def list_sessions(*, cursor=None, workspace_root=None):
        calls.append((cursor, workspace_root))
        return SimpleNamespace(
            items=(SessionSummary(session_id="second", status="inactive", workspace_root=str(tmp_path)),),
            next_cursor="next-page",
        )

    agent.sessions.list_sessions = list_sessions
    result = await agent.list_sessions(cwd=str(tmp_path), cursor="page-two")
    assert calls == [("page-two", str(tmp_path.resolve()))]
    assert [session.session_id for session in result.sessions] == ["second"]
    assert result.next_cursor == "next-page"
    assert result.model_dump(by_alias=True, exclude_none=True)["nextCursor"] == "next-page"
