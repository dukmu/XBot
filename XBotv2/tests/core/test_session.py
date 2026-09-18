"""Core live-session lifecycle tests independent of HTTP."""

import asyncio
from types import SimpleNamespace

import pytest

from XBotv2.agents import AgentConfigured
from XBotv2.application import RuntimeEvent
from XBotv2.core.paths import RuntimePaths
from XBotv2.agentloop import AgentInbox, EventContext
from XBotv2.agentloop.contracts import InboxInput, InboxSplice, InboxTarget
from XBotv2.core import Message
from XBotv2.core.history import ConversationHistory
from XBotv2.core.tools import ClientEvent
from XBotv2.session import HistoryChanged, SessionInfo
from XBotv2.session.contracts import SessionEventCursorExpired
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
        message_id = kwargs.get("message_id", f"msg-{len(self.inbox)}")
        self.inbox.append(str(content))
        return SimpleNamespace(message_id=message_id)

    @property
    def pending_input_count(self):
        return len(self.inbox)

    @property
    def pending_inputs(self):
        return ()

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


class InboxBlockingEngine(FakeEngine):
    """Run a turn from the real inbox and hold its model step."""

    def __init__(self) -> None:
        super().__init__()
        self.inbox = AgentInbox()
        self.claimed_ids: list[str] = []
        self.model_started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def pending_input_count(self):
        return len(self.inbox)

    @property
    def pending_inputs(self):
        return tuple(self.inbox.pending)

    async def followup(self, content, **kwargs):
        return await self.inbox.followup(content, **kwargs)

    async def run_turn(self, content, *, request_id="", images=None, artifacts=None):
        item = await self.inbox.send(
            content,
            target=InboxTarget.NEXT_TURN,
            wakeup=False,
            source="user",
            message_id=request_id,
            images=images,
            artifacts=artifacts,
        )
        claimed = await self.inbox.claim_turn()
        self.claimed_ids.extend(item.message_id for item in claimed)
        self.model_started.set()
        yield {"type": "turn_started", "data": {"turn": 1}}
        await self.release.wait()
        await self.inbox.commit([item.message_id for item in claimed])
        yield {"type": "assistant_message", "data": {"content": "reply"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def discard_inputs(self):
        await self.inbox.discard()


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


async def collect_turn(
    session: SessionRuntime,
    content: str,
    request_id: str,
    *,
    delivery: str = "steer",
) -> list[ClientEvent]:
    """Submit a command and collect its frames from the central stream."""
    events = session.event_stream.subscribe()
    await session.send_message(content, request_id, delivery=delivery)
    collected: list[ClientEvent] = []
    async for frame in events:
        collected.append(frame.event)
        if frame.event.type in {"turn_finished", "turn_cancelled"}:
            break
        if frame.event.type == "error" and frame.event.data.get("code") == "turn_failed":
            break
    await events.aclose()
    return collected


@pytest.mark.asyncio
async def test_idle_user_turn_runs_directly(tmp_path):
    session = runtime(tmp_path)

    events = await collect_turn(session, "start", "request")

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
async def test_first_input_is_claimed_before_queue_submission_returns(tmp_path):
    engine = InboxBlockingEngine()
    session = SessionRuntime(
        session_id="session",
        thread_id="agent",
        provider_name="mock",
        paths=RuntimePaths.from_data_dir(tmp_path),
        workspace_root=str(tmp_path),
        no_plugins=True,
        application=FakeApplication(engine),
        engine=engine,
    )

    first_task = asyncio.create_task(
        session.send_message("first", "req-first")
    )
    while not session.turn_lock.locked():
        await asyncio.sleep(0)
    second_task = asyncio.create_task(
        session.send_message("queued", "req-queued", delivery="queue")
    )
    await asyncio.gather(first_task, second_task)

    assert [(item.message_id, item.target.value) for item in engine.pending_inputs] == [
        ("req-queued", "next-turn"),
    ]
    assert engine.claimed_ids == ["req-first"]
    assert engine.model_started.is_set()

    turn_task = session.turn_task
    assert turn_task is not None
    engine.release.set()
    await asyncio.wait_for(turn_task, timeout=1)
    await session.close()


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
    observed = []
    shared = session.event_stream.subscribe()
    await session.send_message("fail", "failed-request")
    async with asyncio.timeout(1):
        async for frame in shared:
            observed.append(frame.event.type)
            if frame.event.type == "error":
                break
    assert "error" in observed
    next_events = await collect_turn(session, "retry", "next-request")
    assert any(event.type == "assistant_message" for event in next_events)
    await shared.aclose()
    await session.close()


@pytest.mark.asyncio
async def test_failure_before_turn_started_closes_request_stream(tmp_path):
    class FailingEngine(FakeEngine):
        turn_count = 0

        async def run_turn(self, content, **kwargs):
            del content, kwargs
            raise RuntimeError("provider unavailable")
            yield  # keep this an async generator

    session = runtime(tmp_path)
    session.engine = FailingEngine()

    events = await asyncio.wait_for(
        collect_turn(session, "fail", "failed-before-start"), timeout=1
    )

    assert [event.type for event in events] == ["message", "error"]
    assert events[-1].data["code"] == "turn_failed"
    await session.close()


@pytest.mark.asyncio
async def test_live_subscription_after_long_history_starts_at_current_cursor(tmp_path):
    session = runtime(tmp_path)
    for index in range(1024):
        session._publish_runtime_event(ClientEvent(
            type="completion_notice",
            data={"task_id": str(index), "status": "completed"},
        ))

    events = await collect_turn(session, "latest", "latest")

    assert any(event.type == "assistant_message" for event in events)
    await session.close()


@pytest.mark.asyncio
async def test_slow_central_stream_reports_cursor_expiry(tmp_path):
    class BurstEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def run_turn(self, content, **kwargs):
            del content, kwargs
            yield {"type": "turn_started", "data": {"turn": 1}}
            await self.release.wait()
            for index in range(1024):
                yield {
                    "type": "assistant_message_delta",
                    "data": {"content": str(index)},
                }
            yield {"type": "turn_finished", "data": {"turn": 1}}

    session = runtime(tmp_path)
    session.engine = BurstEngine()
    stream = session.event_stream.subscribe()
    await session.send_message("burst", "burst")
    assert (await anext(stream)).event.type == "message"
    task = session.turn_task
    assert task is not None
    session.engine.release.set()
    await task

    with pytest.raises(SessionEventCursorExpired):
        await anext(stream)
    await stream.aclose()
    await session.close()


@pytest.mark.asyncio
async def test_background_claim_does_not_steal_user_post_ownership(tmp_path):
    session = blocking_runtime(tmp_path)
    stream = session.event_stream.subscribe()
    await session.send_message("go", "user-request")
    assert (await anext(stream)).event.type == "message"

    user_item = InboxInput(
        content="go",
        target=InboxTarget.NEXT_STEP,
        source="user",
        message_id="user-request",
    )
    background = InboxInput(
        content="job finished",
        target=InboxTarget.NEXT_STEP,
        source="background",
        message_id="background-1",
    )
    session._on_inbox_splice(EventContext(inbox_splice=InboxSplice(
        operation="claim",
        target=InboxTarget.NEXT_STEP,
        message_ids=[user_item.message_id, background.message_id],
        items=[user_item, background],
    )))
    session.engine.release.set()

    events = []
    async for frame in stream:
        events.append(frame)
        if frame.event.type == "turn_finished":
            break
    assert any(frame.event.type == "assistant_message" for frame in events)
    assert events[-1].event.type == "turn_finished"
    await session.close()


@pytest.mark.asyncio
async def test_unclaimed_other_target_post_waits_for_its_own_claim(tmp_path):
    session = blocking_runtime(tmp_path)
    active = session.event_stream.subscribe()
    await session.send_message("first", "req-1")
    assert (await anext(active)).event.type == "message"
    async def next_or_none(stream):
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return None

    active_terminal_task = asyncio.create_task(next_or_none(active))
    queued = session.event_stream.subscribe()
    steered = session.event_stream.subscribe()
    await session.send_message("next turn", "req-2", delivery="queue")
    await session.send_message("next step", "req-3", delivery="steer")

    item = InboxInput(
        content="next step",
        target=InboxTarget.NEXT_STEP,
        source="user",
        message_id="req-3",
    )
    session._on_inbox_splice(EventContext(inbox_splice=InboxSplice(
        operation="claim",
        target=InboxTarget.NEXT_STEP,
        message_ids=[item.message_id],
        items=[item],
    )))
    assert not active_terminal_task.done()
    session.engine.release.set()
    assert (await active_terminal_task).event.type == "turn_started"
    events = []
    async for frame in steered:
        events.append(frame)
        if frame.event.type == "turn_finished":
            break
    assert events[0].event.type in {"message", "input_claimed"}
    assert any(frame.event.type == "turn_finished" for frame in events)
    await queued.aclose()
    await session.close()


@pytest.mark.asyncio
async def test_turn_survives_response_stream_disconnect_and_replays(tmp_path):
    session = blocking_runtime(tmp_path)
    shared = session.event_stream.subscribe()
    await session.send_message("start", "request")
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


@pytest.mark.asyncio
async def test_runtime_event_is_forwarded_without_starting_a_turn(tmp_path):
    session = runtime(tmp_path)
    events = session.event_stream.subscribe()

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
    first = session.event_stream.subscribe()
    second = session.event_stream.subscribe()

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
    await first.aclose()
    assert session.event_stream.subscriber_count == 1
    await session.close()


@pytest.mark.asyncio
async def test_history_change_is_projected_from_typed_session_event(tmp_path):
    session = runtime(tmp_path)
    events = session.event_stream.subscribe()
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
        # agent-owned inbox; the input content is never copied into a separate
        # session-side queue.
        events = session.event_stream.subscribe()
        await session.send_message("queued", "request")
        assert session.engine.steered == [("queued", "request")]
        msg = (
            await asyncio.wait_for(anext(events), timeout=1)
        ).event.model_dump(mode="json")
        assert msg["type"] == "message"
        assert msg["data"]["role"] == "user"
        assert msg["data"]["content"] == "queued"
        assert msg["data"]["id"]

        await events.aclose()
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
