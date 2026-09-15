"""Core live-session lifecycle tests independent of HTTP."""

import asyncio
from types import SimpleNamespace

import pytest

from XBotv2.agents import AgentConfigured
from XBotv2.application import RuntimeEvent
from XBotv2.core.paths import RuntimePaths
from XBotv2.agentloop import EventContext
from XBotv2.core import Message
from XBotv2.core.history import ConversationHistory
from XBotv2.core.tools import ClientEvent
from XBotv2.session import HistoryChanged, SessionInfo
from XBotv2.session.runtime import SessionRuntime, TurnResponse


class FakeEngine:
    turn_count = 1
    continuation = False

    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0
        self.inbox: list[str] = []
        self.steered: list[tuple[str, str]] = []
        self._wake_driver = None

    def set_wake_driver(self, callback):
        self._wake_driver = callback

    async def inject(self, content, **kwargs):
        self.inbox.append(str(content))

    async def steer(self, content, **kwargs):
        message_id = kwargs.get("message_id", f"msg-{len(self.steered)}")
        self.steered.append((content, message_id))
        return SimpleNamespace(message_id=message_id)

    async def followup(self, content, **kwargs):
        return content

    @property
    def pending_input_count(self):
        return len(self.inbox)

    async def run_pending(self, *, request_id=""):
        del request_id
        if not self.inbox:
            return
        self.inbox.clear()
        yield {"type": "turn_started", "data": {"turn": 1}}
        yield {"type": "assistant_message", "data": {"content": "resumed"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def discard_inputs(self):
        self.inbox.clear()

    async def run_turn(
        self,
        content,
        *,
        request_id="",
        images=None,
        artifacts=None,
    ):
        del content, request_id, images, artifacts
        yield {"type": "turn_started", "data": {"turn": 1}}
        yield {"type": "assistant_message", "data": {"content": "reply"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def close_session(self):
        self.closed = True
        self.close_count += 1


class BlockingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = False

    async def run_turn(self, content, **kwargs):
        del content, kwargs
        self.started.set()
        yield {"type": "turn_started", "data": {"turn": 1}}
        await self.release.wait()
        yield {"type": "assistant_message", "data": {"content": "recovered"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}
        self.completed = True


class FakeClientEvents:
    def __init__(self) -> None:
        self.sink = None

    def install(self, sink):
        previous = self.sink
        self.sink = sink

        def dispose():
            if self.sink is sink:
                self.sink = previous

        return dispose


class FakeApplication:
    def __init__(self, driver) -> None:
        self.driver = driver
        self.events = SimpleNamespace(on=lambda *_args, **_kwargs: None)
        self.client_events = FakeClientEvents()
        self.history_pages = ConversationHistory()
        self.closed = False

    async def status_slots(self):
        return {}

    async def snapshot(self):
        return SimpleNamespace(messages=tuple(self.history_pages))

    async def close(self):
        self.closed = True


def runtime(tmp_path) -> SessionRuntime:
    engine = FakeEngine()
    return SessionRuntime(
        session_id="session",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        application=FakeApplication(engine),
        engine=engine,
    )


def blocking_runtime(tmp_path) -> SessionRuntime:
    engine = BlockingEngine()
    return SessionRuntime(
        session_id="session",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        application=FakeApplication(engine),
        engine=engine,
    )


@pytest.mark.asyncio
async def test_idle_user_turn_runs_directly(tmp_path):
    session = runtime(tmp_path)

    events = [event async for event in session.stream_message("start", "request")]

    assert [
        event.data["content"]
        for event in events
        if event.type == "assistant_message"
    ] == ["reply"]
    assert session.turn_task is None
    await session.close()
    assert session.engine.closed is True
    assert session.engine.close_count == 1


@pytest.mark.asyncio
async def test_driver_failure_finishes_shared_turn_and_allows_next_input(tmp_path):
    class FailingOnceEngine(FakeEngine):
        async def run_turn(self, content, **kwargs):
            if content == "fail":
                yield {"type": "turn_started", "data": {"turn": 1}}
                yield {"type": "assistant_message_delta", "data": {"content": "partial"}}
                raise RuntimeError("response interrupted")
            async for event in super().run_turn(content, **kwargs):
                yield event

    session = runtime(tmp_path)
    session.engine = FailingOnceEngine()
    shared = session.attach_event_stream()
    failed = [event async for event in session.stream_message("fail", "failed-request")]
    assert [event.type for event in failed][-2:] == ["error", "turn_finished"]
    observed = []
    async with asyncio.timeout(1):
        async for frame in shared:
            observed.append(frame.event.type)
            if frame.event.type == "turn_finished":
                break
    assert "error" in observed
    next_events = [event async for event in session.stream_message("retry", "next-request")]
    assert any(event.type == "assistant_message" for event in next_events)
    await shared.aclose()
    await session.close()


@pytest.mark.asyncio
async def test_turn_survives_response_stream_disconnect_and_replays(tmp_path):
    session = blocking_runtime(tmp_path)
    shared = session.attach_event_stream()
    response = session.stream_message("start", "request")

    assert (await anext(response)).type == "turn_started"
    await response.aclose()
    session.engine.release.set()

    recovered = []
    async with asyncio.timeout(1):
        async for frame in shared:
            recovered.append(frame.event.model_dump(mode="json"))
            if frame.event.type == "turn_finished":
                break

    assert session.engine.completed is True
    assert [event["type"] for event in recovered] == [
        "message",
        "turn_started",
        "assistant_message",
        "turn_finished",
    ]
    await session.close()


def test_slow_compatibility_response_does_not_backpressure_turn() -> None:
    response = TurnResponse(
        "message",
        "request",
    )

    for index in range(513):
        response.emit(ClientEvent(
            type="assistant_message_delta",
            data={"content": str(index)},
        ))

    assert response.attached is False
    error = response.events.get_nowait()
    assert error.type == "error"
    assert error.data["code"] == "response_stream_overflow"
    assert response.events.get_nowait() is None


@pytest.mark.asyncio
async def test_runtime_event_is_forwarded_without_starting_a_turn(tmp_path):
    session = runtime(tmp_path)
    events = session.attach_event_stream()

    session._on_runtime_event(RuntimeEvent(client_event=ClientEvent(
        type="completion_notice",
        data={"task_id": "t1", "status": "completed"},
    )))

    assert session.engine.inbox == []
    notice = (
        await asyncio.wait_for(anext(events), timeout=1)
    ).event.model_dump(mode="json")
    assert notice["type"] == "completion_notice"
    assert session.turn_task is None
    await session.close()


@pytest.mark.asyncio
async def test_resume_pending_inputs_runs_after_runtime_registration(tmp_path):
    session = runtime(tmp_path)
    session.engine.inbox.append("persisted")

    assert session.resume_pending_inputs() is True
    for _ in range(20):
        if session.engine.pending_input_count == 0 and session.wakeup_task is None:
            break
        await asyncio.sleep(0)

    assert session.engine.pending_input_count == 0
    assert session.resume_pending_inputs() is False
    await session.close()


@pytest.mark.asyncio
async def test_runtime_events_are_broadcast_to_multiple_clients(tmp_path):
    session = runtime(tmp_path)
    first = session.attach_event_stream()
    second = session.attach_event_stream()

    session._on_runtime_event(RuntimeEvent(client_event=ClientEvent(
        type="completion_notice",
        data={"task_id": "t1", "status": "completed"},
    )))

    assert (await asyncio.wait_for(anext(first), timeout=1)).event.type == (
        "completion_notice"
    )
    assert (await asyncio.wait_for(anext(second), timeout=1)).event.type == (
        "completion_notice"
    )
    session.detach_event_stream(first)
    assert session.event_stream.subscriber_count == 1
    await session.close()


@pytest.mark.asyncio
async def test_history_change_is_projected_from_typed_session_event(tmp_path):
    session = runtime(tmp_path)
    events = session.attach_event_stream()
    history = (Message(role="user", content="keep"),)
    session.application.history_pages.replace(history)

    await session._on_history_changed(HistoryChanged(
        messages=history,
        operation="undo",
        turns=1,
    ))

    event = (
        await asyncio.wait_for(anext(events), timeout=1)
    ).event.model_dump(mode="json")
    assert event["type"] == "history_updated"
    assert event["data"]["operation"] == "undo"
    assert event["data"]["turns"] == 1
    assert event["data"]["history"][0]["content"] == "keep"
    await session.close()


@pytest.mark.asyncio
async def test_agent_configuration_updates_session_provider_projection(tmp_path):
    session = runtime(tmp_path)

    await session._on_agent_configured(AgentConfigured(
        agent=None,
        session=SessionInfo(
            session_id="session",
            thread_id="agent",
            workspace_root=str(tmp_path),
            provider="selected",
        ),
        provider="selected",
        agent_name="default",
        model="model-1",
        model_mode="chat",
        context_window=4096,
    ))

    assert session.provider_name == "selected"
    await session.close()


@pytest.mark.asyncio
async def test_busy_turn_holds_input_for_fold_delivery(tmp_path):
    session = runtime(tmp_path)
    await session.turn_lock.acquire()
    try:
        # A message submitted while the turn is busy is steered into the
        # agent-owned inbox and tracked as a pending response; the input
        # content is never copied into a separate session-side queue.
        events = session.attach_event_stream()
        stream = session.stream_message("queued", "request")

        async def _first():
            try:
                return await anext(stream)
            except StopAsyncIteration:
                return None

        first_task = asyncio.create_task(_first())
        await asyncio.sleep(0)
        assert session.engine.steered == [("queued", "request")]
        assert len(session.pending_responses) == 1
        msg = (
            await asyncio.wait_for(anext(events), timeout=1)
        ).event.model_dump(mode="json")
        assert msg["type"] == "message"
        assert msg["data"]["role"] == "user"
        assert msg["data"]["content"] == "queued"
        assert msg["data"]["id"]

        # Release the pending response so the busy stream can finish.
        pending = next(iter(session.pending_responses.values()))
        pending.events.put_nowait(None)
        assert await asyncio.wait_for(first_task, timeout=1) is None
    finally:
        session.turn_lock.release()
    await session.close()


@pytest.mark.asyncio
async def test_status_command_asks_the_engine_for_pending_count(tmp_path):
    """/status must read the pending-input count from its single owner (the
    engine) at command time, never from a frozen projection."""
    from XBotv2.session.commands import build_session_commands
    from XBotv2.session.contracts import SessionStatus

    class StubSession:
        session_id = "s1"
        thread_id = "t1"
        workspace_root = str(tmp_path)
        provider = "default"

        def new_thread_id(self, owner):
            del owner
            return "t2"

        def status(self, *, pending_input_count):
            return SessionStatus(
                session_id=self.session_id,
                thread_id=self.thread_id,
                workspace_root=self.workspace_root,
                agent="",
                provider="default",
                model="",
                model_mode="",
                context_window=0,
                status="idle",
                resumed=False,
                turn_count=1,
                message_count=2,
                pending_inputs=pending_input_count,
            )

        async def fork(self):
            return "forked"

        async def clear_history(self):
            return 1

        async def undo_history(self, count):
            del count
            return []

        async def regenerate_history(self):
            from XBotv2.core.messages import Message
            return Message(role="assistant", content="ok")

    commands = build_session_commands(
        StubSession(),
        pending_input_count=lambda: 7,
    )
    status = next(command for command in commands if command.name == "status")
    result = await status.handler("")

    assert result.status == "ok"
    assert "Queued inputs: 7" in result.message

    # The same command reports a live engine value on every invocation.
    commands = build_session_commands(
        StubSession(),
        pending_input_count=lambda: 0,
    )
    status = next(command for command in commands if command.name == "status")
    assert "Queued inputs: 0" in (await status.handler("")).message
