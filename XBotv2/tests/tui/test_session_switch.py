"""Switching the attached session, without dropping the client.

The previous client could resume another session mid-run; that capability was
removed with the old package and is restored here. It is the one path that moves
the whole client -- transport, cursor, translator, and transcript -- onto a
different session while the read loop keeps running.

Written before the transport grew a ``switch``.
"""

from __future__ import annotations

import asyncio

import pytest

from XBotv2.tests.tui.factories import (
    SESSION,
    THREAD,
    RecordingView,
    ScriptedBackend,
    StreamScript,
    frame,
    frames,
    snapshot,
    stream,
    thread,
)
from XBotv2.tui.controller import TuiController
from XBotv2.tui.events import (
    ConnectionChanged,
    ErrorFrame,
    SnapshotAdopted,
    TurnStarted,
)
from XBotv2.tui.status import Connection, Status, derive
from XBotv2.tui.transport import TransportConfig, TransportSession

OTHER_SESSION = "s2"
OTHER_THREAD = "agent"


async def no_sleep(_seconds: float) -> None:
    return None


def build(
    backend: ScriptedBackend,
    recorder,
    *,
    reconnect_delays: tuple[float, ...] = (0.0,),
) -> TransportSession:
    return TransportSession(
        backend,
        config=TransportConfig(
            session_id=SESSION,
            thread_id=THREAD,
            reconnect_delays=reconnect_delays,
            cursor_recoveries=0,
            baseline_rebuilds=0,
        ),
        emit=recorder,
        sleep=no_sleep,
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    def __call__(self, event: object) -> None:
        self.events.append(event)

    def of(self, cls: type) -> list:
        return [event for event in self.events if isinstance(event, cls)]


async def running(backend: ScriptedBackend, recorder: Recorder) -> tuple[TransportSession, asyncio.Task]:
    """A connected transport with its read loop actually running."""
    transport = build(backend, recorder)
    await transport.connect()
    recorder.events.clear()
    task = asyncio.create_task(transport.run(), name="read")
    await asyncio.sleep(0)
    return transport, task


async def finish(transport: TransportSession, task: asyncio.Task) -> None:
    transport.stop()
    await asyncio.wait_for(task, timeout=2.0)


# --- switching ------------------------------------------------------------


async def test_switching_opens_the_new_session_and_adopts_it(backend: ScriptedBackend) -> None:
    backend.session = snapshot(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
    recorder = Recorder()
    transport = build(backend, recorder)
    await transport.connect()
    recorder.events.clear()

    await transport.switch(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)

    adopted = recorder.of(SnapshotAdopted)
    assert adopted and adopted[-1].snapshot.session_id == OTHER_SESSION
    assert transport.session_id == OTHER_SESSION
    assert transport.thread_id == OTHER_THREAD


async def test_switching_repoints_the_read_loop_at_the_new_session(
    backend: ScriptedBackend,
) -> None:
    """The old subscription is idle; the switch must not wait for a frame."""
    backend.session = snapshot(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
    recorder = Recorder()
    transport, task = await running(backend, recorder)
    try:
        await transport.switch(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
        for _ in range(50):
            if backend.stream_opens >= 2:
                break
            await asyncio.sleep(0.01)
        assert backend.stream_opens >= 2, "the read loop must re-subscribe"
        assert backend.subscriptions[1].session_id == OTHER_SESSION
        assert backend.subscriptions[1].thread_id == OTHER_THREAD
    finally:
        await finish(transport, task)


async def test_switching_resets_the_cursor_to_the_new_snapshot(backend: ScriptedBackend) -> None:
    backend.session = snapshot(session_id=OTHER_SESSION, event_cursor=17)
    backend.streams = [stream(hold=True), stream(frame("turn_started", {"turn": 1}, sequence=18, session_id=OTHER_SESSION))]
    recorder = Recorder()
    transport, task = await running(backend, recorder)
    try:
        await transport.switch(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
        for _ in range(50):
            if recorder.of(TurnStarted):
                break
            await asyncio.sleep(0.01)
        assert backend.subscriptions[1].after == 17, "resume from the adopted cursor"
        assert transport.cursor == 18
    finally:
        await finish(transport, task)


async def test_switching_is_not_a_reconnect(backend: ScriptedBackend) -> None:
    """A switch is a deliberate move, not a failure.

    The reconnect budget is empty here: if a switch were mistaken for a stream
    that ended, the transport would immediately report a failure instead of
    resubscribing.
    """
    backend.session = snapshot(session_id=OTHER_SESSION)
    recorder = Recorder()
    transport = build(backend, recorder, reconnect_delays=())
    await transport.connect()
    recorder.events.clear()
    task = asyncio.create_task(transport.run(), name="read")
    await asyncio.sleep(0)
    try:
        await transport.switch(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
        for _ in range(50):
            if backend.stream_opens >= 2:
                break
            await asyncio.sleep(0.01)
        assert backend.stream_opens >= 2, "the switch must resubscribe"
        assert not recorder.of(ErrorFrame)
        assert not [
            event
            for event in recorder.of(ConnectionChanged)
            if event.connection is Connection.DISCONNECTED
        ]
    finally:
        await finish(transport, task)


async def test_a_frame_for_the_old_thread_is_rejected_after_switching(
    backend: ScriptedBackend,
) -> None:
    """A late frame from the session we left must not land in the new transcript."""
    backend.session = snapshot(session_id=OTHER_SESSION, thread_id="other")
    backend.streams = [
        stream(hold=True),
        stream(
            frame("turn_started", {"turn": 1}, sequence=1, session_id=SESSION, thread_id=THREAD),
            frame("turn_started", {"turn": 2}, sequence=2, session_id=OTHER_SESSION, thread_id="other"),
            hold=True,
        ),
    ]
    recorder = Recorder()
    transport, task = await running(backend, recorder)
    try:
        await transport.switch(session_id=OTHER_SESSION, thread_id="other")
        for _ in range(50):
            if recorder.of(TurnStarted):
                break
            await asyncio.sleep(0.01)
        started = recorder.of(TurnStarted)
        assert [event.payload.turn for event in started] == [2], "only the new thread's frames apply"
        assert recorder.of(ErrorFrame), "the foreign frame is reported, not ignored"
    finally:
        await finish(transport, task)


async def test_a_failed_switch_leaves_the_client_where_it_was(
    backend: ScriptedBackend,
) -> None:
    recorder = Recorder()
    transport = build(backend, recorder)
    await transport.connect()
    recorder.events.clear()
    backend.open_error = RuntimeError("no such session")

    with pytest.raises(RuntimeError):
        await transport.switch(session_id="missing", thread_id=THREAD)

    assert transport.session_id == SESSION
    assert not recorder.of(SnapshotAdopted), "nothing was adopted"


async def test_switching_replays_the_new_sessions_pending_prompts(
    backend: ScriptedBackend,
) -> None:
    from XBotv2.tui.events import InteractionOpened

    backend.session = snapshot(
        session_id=OTHER_SESSION,
        pending_interactions=[
            {
                "type": "permission_request",
                "data": {
                    "request_id": "r2",
                    "source": "permission_system",
                    "reason": "needs approval",
                    "tool_call": {"id": "c2", "name": "bash", "args": {}},
                },
            }
        ],
    )
    recorder = Recorder()
    transport = build(backend, recorder)
    await transport.connect()
    recorder.events.clear()

    await transport.switch(session_id=OTHER_SESSION, thread_id=OTHER_THREAD)
    assert recorder.of(InteractionOpened)[-1].request.request_id == "r2"


# --- the controller -------------------------------------------------------


async def test_the_controller_switches_and_renders(backend: ScriptedBackend) -> None:
    backend.session = snapshot(
        session_id=OTHER_SESSION,
        history=[{"role": "user", "content": "from the other session"}],
    )
    view = RecordingView()
    controller = TuiController(
        backend,
        view=view,
        config=TransportConfig(session_id=SESSION, thread_id=THREAD, reconnect_delays=(0.0,)),
        sleep=no_sleep,
    )
    await controller.connect()
    before = view.renders

    await controller.switch_session(OTHER_SESSION, OTHER_THREAD)

    assert controller.state.session_id == OTHER_SESSION
    assert any(
        getattr(entry, "content", "") == "from the other session"
        for entry in controller.state.timeline
    )
    assert view.renders > before, "the new transcript must be drawn"


async def test_the_controller_switch_clears_the_previous_transcript(
    backend: ScriptedBackend,
) -> None:
    """Entries from the session we left must not survive the switch."""
    backend.session = snapshot(session_id=OTHER_SESSION)
    view = RecordingView()
    controller = TuiController(
        backend,
        view=view,
        config=TransportConfig(session_id=SESSION, thread_id=THREAD, reconnect_delays=(0.0,)),
        sleep=no_sleep,
    )
    await controller.connect()
    from XBotv2.tui.events import UserMessagePublished

    controller.dispatch(UserMessagePublished(payload=_message("m1", "old session")))
    await controller.flush()
    assert controller.state.timeline.get("m1") is not None

    await controller.switch_session(OTHER_SESSION, OTHER_THREAD)
    assert controller.state.timeline.get("m1") is None


def _message(message_id: str, content: str):
    from XBotv2.session.protocol import MessageData

    return MessageData(id=message_id, role="user", content=content)


async def test_switching_without_a_thread_uses_the_sessions_main_thread(
    backend: ScriptedBackend,
) -> None:
    """A session names its threads; the picker offers sessions, not threads."""
    backend.session = snapshot(session_id=OTHER_SESSION, thread_id="")
    backend.threads = (
        thread(thread_id="side", kind="subagent"),
        thread(thread_id="main", kind="main"),
    )
    recorder = Recorder()
    transport = build(backend, recorder)
    await transport.connect()
    recorder.events.clear()

    await transport.switch(session_id=OTHER_SESSION, thread_id="")

    assert transport.thread_id == "main"
