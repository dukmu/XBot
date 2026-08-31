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
from XBotv2.session.runtime import SessionRuntime


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


class FakeClientEvents:
    def __init__(self) -> None:
        self.sink = None

    def set_sink(self, sink):
        previous = self.sink
        self.sink = sink
        return previous


class FakeApplication:
    def __init__(self, driver) -> None:
        self.driver = driver
        self.events = SimpleNamespace(on=lambda *_args, **_kwargs: None)
        self.client_events = FakeClientEvents()
        self.history_pages = ConversationHistory()
        self.closed = False

    async def status_slots(self):
        return {}

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


@pytest.mark.asyncio
async def test_idle_user_turn_runs_directly(tmp_path):
    session = runtime(tmp_path)

    events = [event async for event in session.stream_message("start", "request")]

    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message"
    ] == ["reply"]
    assert session.turn_task is None
    await session.close()
    assert session.engine.closed is True
    assert session.engine.close_count == 1


@pytest.mark.asyncio
async def test_runtime_event_is_forwarded_without_starting_a_turn(tmp_path):
    session = runtime(tmp_path)
    events = session.attach_event_stream()

    session._on_runtime_event(RuntimeEvent(client_event=ClientEvent(
        "completion_notice",
        {"task_id": "t1", "status": "completed"},
    )))

    assert session.engine.inbox == []
    notice = (await asyncio.wait_for(anext(events), timeout=1)).event.to_dict()
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
        "completion_notice",
        {"task_id": "t1", "status": "completed"},
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

    event = (await asyncio.wait_for(anext(events), timeout=1)).event.to_dict()
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
        msg = (await asyncio.wait_for(anext(events), timeout=1)).event.to_dict()
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
