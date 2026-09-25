"""The transport: sequence continuity, recovery, watchdog, and writes.

These tests drive the transport against the one scripted backend from
``conftest``. What matters is what the transport *emits* to the reducer and what
cursor it resubscribes from, not how it is implemented.

Invariants under test:
  I1  an authoritative thread read is what settles the turn state
  I4  a gap, a rejected frame, a failed write, and a dead stream are all visible
  I5  a stale frame is never re-applied, and the stream never waits on rendering
"""

from __future__ import annotations

import asyncio

import pytest

from XBotv2.client import XBotClientError
from XBotv2.core.domain import TokenCounters, UsageSnapshot
from XBotv2.usage import UsageUpdated as UsageUpdatedPayload
from XBotv2.protocol import ErrorResponse
from XBotv2.core.tools import ToolCall
from XBotv2.permissions.contracts import PermissionRequest, ToolPermission
from XBotv2.tests.tui.factories import (
    SESSION,
    THREAD,
    ScriptedBackend,
    StreamScript,
    assistant_record,
    frame,
    frames,
    snapshot,
    stream,
    thread,
)
from XBotv2.tests.tui.factories import history_page, human_record
from XBotv2.tui.events import (
    AssistantDelta,
    ClientNoticeReceived,
    ConnectionChanged,
    ErrorFrame,
    InteractionOpened,
    InterruptAsked,
    InterruptSettled,
    OlderHistoryFailed,
    OlderHistoryLoaded,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    TurnFinished,
    TurnStarted,
    UiEvent,
    UsageSnapshotReceived,
    UserInputFailed,
    UserInputSubmitted,
    ThreadRead,
)
from XBotv2.tui.state import SessionState, reduce
from XBotv2.tui.status import Connection, ServerTurn, Status, derive
from XBotv2.tui.transport import TransportConfig, TransportSession


def cursor_expired(oldest: int = 42) -> XBotClientError:
    return XBotClientError(
        409,
        ErrorResponse(
            code="session_event_cursor_expired",
            message="cursor fell out of the replay window",
            details={"oldest_sequence": oldest},
        ),
    )


async def no_sleep(_seconds: float) -> None:
    return None


class Recorder:
    """Collects emitted events, optionally folding them into a reducer.

    ``on_event`` lets a test stop the transport at a chosen event, so a test that
    only cares about one scripted stream does not sit through a reconnect.
    """

    def __init__(self, state: SessionState | None = None) -> None:
        self.events: list[UiEvent] = []
        self.state = state
        self.on_event = None

    def __call__(self, event: UiEvent) -> None:
        self.events.append(event)
        if self.state is not None:
            reduce(self.state, event)
        if self.on_event is not None:
            self.on_event(event)

    def of(self, cls: type) -> list:
        return [event for event in self.events if isinstance(event, cls)]

    def only(self, cls: type):
        found = self.of(cls)
        assert len(found) == 1, f"expected one {cls.__name__}, got {len(found)}"
        return found[0]


def build(
    backend: ScriptedBackend,
    recorder: Recorder,
    *,
    sleep=no_sleep,
    **overrides,
) -> TransportSession:
    fields = {
        "session_id": SESSION,
        "thread_id": THREAD,
        "reconnect_delays": (0.0,),
        "cursor_recoveries": 0,
        "baseline_rebuilds": 0,
        **overrides,
    }
    config = TransportConfig(**fields)
    return TransportSession(backend, config=config, emit=recorder, sleep=sleep)


async def connected(
    backend: ScriptedBackend,
    recorder: Recorder,
    **overrides,
) -> TransportSession:
    transport = build(backend, recorder, **overrides)
    await transport.connect()
    recorder.events.clear()
    return transport


def stop_on(recorder: Recorder, transport: TransportSession, *kinds: type) -> None:
    def hook(event: UiEvent) -> None:
        if isinstance(event, kinds):
            transport.stop()

    recorder.on_event = hook


# --- connect --------------------------------------------------------------


async def test_connect_announces_connecting_then_connected(backend: ScriptedBackend) -> None:
    recorder = Recorder()
    await build(backend, recorder).connect()
    assert [event.connection for event in recorder.of(ConnectionChanged)] == [
        Connection.CONNECTING,
        Connection.CONNECTED,
    ]


async def test_connect_adopts_the_snapshot(backend: ScriptedBackend) -> None:
    recorder = Recorder()
    await build(backend, recorder).connect()
    assert recorder.only(SnapshotAdopted).snapshot.data.key.session_id == SESSION


async def test_connect_replays_unanswered_prompts(backend: ScriptedBackend) -> None:
    """A live prompt exists only in the event stream, so a reconnecting client
    rebuilds it from the snapshot."""
    backend.session = snapshot(
        pending_interactions=[
            PermissionRequest(
                interaction_id="r1",
                source="permission_system",
                reason="needs approval",
                subject=ToolPermission(
                    tool_call=ToolCall(id="c1", name="bash", args={}),
                ),
            )
        ]
    )
    recorder = Recorder()
    await build(backend, recorder).connect()
    assert recorder.only(InteractionOpened).request.interaction_id == "r1"


async def test_connect_reads_the_thread_so_a_mid_turn_attach_is_running(
    backend: ScriptedBackend,
) -> None:
    """The session descriptor carries no turn answer; the thread listing does."""
    backend.threads = (thread(turn_status="running", status_slots={"goal": "ship"}),)
    state = SessionState()
    recorder = Recorder(state)
    await build(backend, recorder).connect()
    assert recorder.only(ThreadRead).payload.turn_status == "running"
    assert recorder.only(StatusSlotsUpdated).slots == {"goal": "ship"}
    assert derive(state.facts) is Status.RUNNING


async def test_connect_does_not_double_count_usage(backend: ScriptedBackend) -> None:
    """A thread read reports the session *total*, while usage frames are deltas.
    Feeding that total through the accumulating path would double it."""
    total = UsageSnapshot(total_counters=TokenCounters(input=100))
    backend.session = snapshot(usage=total)
    backend.threads = (thread(usage=total),)
    state = SessionState()
    recorder = Recorder(state)
    await build(backend, recorder).connect()
    assert state.usage.total_counters.input == 100
    assert not recorder.of(UsageSnapshotReceived), "an authoritative total is not a delta"


async def test_connect_failure_is_visible_and_disconnects(backend: ScriptedBackend) -> None:
    backend.open_error = RuntimeError("server refused")
    state = SessionState()
    recorder = Recorder(state)
    with pytest.raises(RuntimeError):
        await build(backend, recorder).connect()
    assert recorder.of(ErrorFrame), "a failed connect must be reported"
    assert derive(state.facts) is Status.DISCONNECTED


# --- the read loop --------------------------------------------------------


async def test_run_applies_frames_in_order(backend: ScriptedBackend) -> None:
    backend.streams = [
        stream(*frames(
            ("turn_started", {"turn": 1}),
            ("assistant_text_delta", {"text": "hi"}),
            (
                "turn_ended",
                {"turn": 1, "outcome": {"kind": "finished", "stop_reason": "completed"}},
            ),
        ))
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, TurnFinished)
    await transport.run()
    assert recorder.of(TurnStarted)
    assert recorder.only(AssistantDelta).payload.text == "hi"
    assert transport.cursor == 3


async def test_a_sequence_gap_is_reported_and_the_frame_still_applies(
    backend: ScriptedBackend,
) -> None:
    """Losing frames is not an excuse to lose the ones that did arrive."""
    backend.streams = [
        stream(
            frame("turn_started", {"turn": 1}, sequence=1),
            frame("assistant_text_delta", {"text": "after the gap"}, sequence=9),
        )
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, AssistantDelta)
    await transport.run()
    gap = recorder.only(StreamGapDetected)
    assert (gap.expected, gap.received) == (2, 9)
    assert recorder.only(AssistantDelta).payload.text == "after the gap"


async def test_a_replayed_frame_is_not_applied_twice(backend: ScriptedBackend) -> None:
    """Recovery rewinds the cursor, so the server replays frames the client
    already applied; counting them again would inflate usage."""
    backend.streams = [
        stream(
            frame(
                "usage_updated",
                UsageUpdatedPayload(
                    snapshot=UsageSnapshot(total_counters=TokenCounters(input=5))
                ).model_dump(mode="json"),
                sequence=1,
            ),
            frame(
                "usage_updated",
                UsageUpdatedPayload(
                    snapshot=UsageSnapshot(total_counters=TokenCounters(input=8))
                ).model_dump(mode="json"),
                sequence=2,
            ),
        )
    ]
    state = SessionState()
    recorder = Recorder(state)
    transport = await connected(backend, recorder)
    transport._cursor = 1  # noqa: SLF001 - simulate a resubscription replay
    stop_on(recorder, transport, UsageSnapshotReceived)
    await transport.run()
    assert len(recorder.of(UsageSnapshotReceived)) == 1
    assert state.usage.total_counters.input == 8


async def test_an_unsequenced_frame_is_applied(backend: ScriptedBackend) -> None:
    """A locally produced frame (a decode failure) carries no sequence."""
    backend.streams = [
        stream(frame("client_message", {"message": "hi", "source": "s"}, sequence=0))
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, ClientNoticeReceived)
    await transport.run()
    assert recorder.of(ClientNoticeReceived)


async def test_a_frame_for_another_session_is_reported_not_applied(
    backend: ScriptedBackend,
) -> None:
    backend.streams = [
        stream(frame("turn_started", {"turn": 1}, sequence=1, session_id="other"))
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, ErrorFrame)
    await transport.run()
    assert recorder.of(ErrorFrame)
    assert not recorder.of(TurnStarted)


async def test_an_unknown_frame_type_is_reported_not_ignored(backend: ScriptedBackend) -> None:
    backend.streams = [stream(frame("something_new", {}, sequence=1))]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, ErrorFrame)
    await transport.run()
    assert "something_new" in recorder.only(ErrorFrame).payload.message


async def test_a_malformed_payload_is_reported(backend: ScriptedBackend) -> None:
    backend.streams = [stream(frame("turn_started", {"turn": "nope"}, sequence=1))]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, ErrorFrame)
    await transport.run()
    assert recorder.of(ErrorFrame)


# --- reconnect ------------------------------------------------------------


async def test_a_dead_stream_resubscribes_from_the_cursor(backend: ScriptedBackend) -> None:
    backend.streams = [
        stream(frame("turn_started", {"turn": 1}, sequence=1)),
        stream(frame("assistant_text_delta", {"text": "back"}, sequence=2)),
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, AssistantDelta)
    await transport.run()
    assert backend.streams[1].after == 1
    assert recorder.of(AssistantDelta)


async def test_exhausted_reconnects_are_reported_and_stop_the_transport(
    backend: ScriptedBackend,
) -> None:
    backend.streams = [
        StreamScript(fail_with=RuntimeError("boom")),
        StreamScript(fail_with=RuntimeError("boom")),
    ]
    state = SessionState()
    recorder = Recorder(state)
    transport = await connected(backend, recorder, reconnect_delays=(0.0,))
    await transport.run()
    assert recorder.of(ErrorFrame)
    assert recorder.of(ConnectionChanged)[-1].connection is Connection.DISCONNECTED
    assert derive(state.facts) is Status.DISCONNECTED
    assert backend.stream_opens == 2


async def test_reconnect_waits_between_attempts(backend: ScriptedBackend) -> None:
    backend.streams = [
        StreamScript(fail_with=RuntimeError("boom")),
        StreamScript(fail_with=RuntimeError("boom")),
        StreamScript(fail_with=RuntimeError("boom")),
    ]
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    recorder = Recorder()
    transport = await connected(
        backend, recorder, sleep=record_sleep, reconnect_delays=(0.1, 0.2)
    )
    await transport.run()
    assert slept == [0.1, 0.2]


async def test_a_stopped_transport_does_not_open_a_stream(backend: ScriptedBackend) -> None:
    recorder = Recorder()
    transport = await connected(backend, recorder)
    transport.stop()
    await transport.run()
    assert backend.stream_opens == 0


async def test_cancelling_transport_closes_the_active_stream(backend: ScriptedBackend) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def held_stream(*_args, **_kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield frame("end", {})
        finally:
            closed.set()

    backend.stream_events = held_stream
    transport = await connected(backend, Recorder())
    task = asyncio.create_task(transport.run())
    await asyncio.wait_for(entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set(), "cancellation must close the backend's SSE iterator"


# --- cursor recovery ------------------------------------------------------


async def test_an_evicted_cursor_resubscribes_from_the_oldest_retained_frame(
    backend: ScriptedBackend,
) -> None:
    backend.streams = [
        StreamScript(fail_with=cursor_expired(oldest=42)),
        stream(frame("turn_started", {"turn": 2}, sequence=42)),
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder, cursor_recoveries=1)
    stop_on(recorder, transport, TurnStarted)
    await transport.run()
    assert backend.streams[1].after == 41, "resume from the frame before the oldest retained"
    assert recorder.of(TurnStarted)


async def test_the_rewind_budget_is_bounded(backend: ScriptedBackend) -> None:
    """A cursor that is permanently behind must surface, not loop forever."""
    backend.streams = [
        StreamScript(fail_with=cursor_expired()),
        StreamScript(fail_with=cursor_expired()),
        StreamScript(fail_with=cursor_expired()),
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder, cursor_recoveries=1, reconnect_delays=())
    await transport.run()
    assert recorder.of(ErrorFrame)
    assert backend.stream_opens == 2


async def test_exhausted_rewinds_rebuild_the_baseline(backend: ScriptedBackend) -> None:
    backend.session = snapshot(event_cursor=7)
    backend.streams = [
        StreamScript(fail_with=cursor_expired()),
        stream(frame("turn_started", {"turn": 3}, sequence=8)),
    ]
    recorder = Recorder()
    transport = await connected(backend, recorder, cursor_recoveries=0, baseline_rebuilds=1)
    stop_on(recorder, transport, TurnStarted)
    await transport.run()
    assert any(opened.get("mode") == "resume" for opened in backend.opened)
    assert recorder.of(SnapshotAdopted)
    assert backend.streams[1].after == 7
    assert transport.cursor == 8


async def test_a_baseline_rebuild_is_bounded(backend: ScriptedBackend) -> None:
    """A cursor that stays behind must surface, not rebuild forever."""
    backend.streams = [
        StreamScript(fail_with=cursor_expired()),
        StreamScript(fail_with=cursor_expired()),
        StreamScript(fail_with=cursor_expired()),
    ]
    recorder = Recorder()
    transport = await connected(
        backend, recorder, cursor_recoveries=0, baseline_rebuilds=1, reconnect_delays=()
    )
    await transport.run()
    assert recorder.of(ErrorFrame)
    assert backend.stream_opens == 2, "one rebuild, then give up"


# --- watchdog -------------------------------------------------------------


async def test_watchdog_reports_the_authoritative_turn_state(backend: ScriptedBackend) -> None:
    backend.threads = (thread(turn_status="running"),)
    recorder = Recorder()
    transport = await connected(backend, recorder)
    await transport.watchdog_once()
    assert recorder.only(ThreadRead).payload.turn_status == "running"


async def test_watchdog_reports_the_threads_status_slots(backend: ScriptedBackend) -> None:
    backend.threads = (thread(status_slots={"goal": "ship"}),)
    recorder = Recorder()
    transport = await connected(backend, recorder)
    await transport.watchdog_once()
    assert recorder.only(StatusSlotsUpdated).slots == {"goal": "ship"}


async def test_watchdog_reports_a_vanished_thread(backend: ScriptedBackend) -> None:
    """The attached thread disappearing is not something to shrug at."""
    recorder = Recorder()
    transport = await connected(backend, recorder)
    backend.threads = ()
    await transport.watchdog_once()
    assert recorder.of(ErrorFrame)


async def test_watchdog_failure_is_reported_once_per_streak(backend: ScriptedBackend) -> None:
    """A watchdog that cannot read must say so, but must not spam every tick."""
    recorder = Recorder()
    transport = await connected(backend, recorder)
    backend.thread_error = RuntimeError("unreachable")
    await transport.watchdog_once()
    await transport.watchdog_once()
    assert len(recorder.of(ErrorFrame)) == 1


async def test_watchdog_reports_again_after_a_recovery(backend: ScriptedBackend) -> None:
    recorder = Recorder()
    transport = await connected(backend, recorder)
    backend.thread_error = RuntimeError("unreachable")
    await transport.watchdog_once()
    backend.thread_error = None
    await transport.watchdog_once()
    backend.thread_error = RuntimeError("unreachable")
    await transport.watchdog_once()
    assert len(recorder.of(ErrorFrame)) == 2


async def test_watch_polls_on_the_configured_interval(backend: ScriptedBackend) -> None:
    backend.threads = (thread(turn_status="running"),)
    slept: list[float] = []
    recorder = Recorder()

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    transport = await connected(
        backend, recorder, sleep=record_sleep, watchdog_seconds=2.5
    )

    def stop_after_three(event: UiEvent) -> None:
        if len(recorder.of(ThreadRead)) >= 3:
            transport.stop()

    recorder.on_event = stop_after_three
    await transport.watch()
    assert slept == [2.5, 2.5, 2.5]
    assert len(recorder.of(ThreadRead)) == 3


# --- writes ---------------------------------------------------------------


async def test_submit_binds_the_input_to_a_client_generated_id(backend: ScriptedBackend) -> None:
    recorder = Recorder()
    transport = await connected(backend, recorder)
    input_id = await transport.submit("hello")
    assert recorder.only(UserInputSubmitted).input_id == input_id
    assert backend.sent[0]["request_id"] == input_id
    assert backend.sent[0]["content"] == "hello"


async def test_submit_reports_a_failure_instead_of_leaving_it_pending(
    backend: ScriptedBackend,
) -> None:
    backend.send_error = RuntimeError("connection reset")
    recorder = Recorder()
    transport = await connected(backend, recorder)
    input_id = await transport.submit("hello")
    failure = recorder.only(UserInputFailed)
    assert failure.input_id == input_id
    assert "connection reset" in failure.error


async def test_submit_states_the_delivery_mode(backend: ScriptedBackend) -> None:
    """The client must say whether it is steering or queueing; the old TUI never
    sent the field and silently got steer semantics while telling the user the
    message was queued."""
    recorder = Recorder()
    transport = await connected(backend, recorder)
    await transport.submit("later", delivery="queue")
    assert backend.sent[0]["delivery"] == "queue"


async def test_interrupt_reports_the_servers_answer(backend: ScriptedBackend) -> None:
    backend.interrupt_cancelled = False
    recorder = Recorder()
    transport = await connected(backend, recorder)
    await transport.interrupt()
    assert recorder.only(InterruptAsked)
    assert recorder.only(InterruptSettled).cancelled is False


async def test_interrupt_failure_is_visible_and_not_claimed_as_success(
    backend: ScriptedBackend,
) -> None:
    backend.interrupt_error = RuntimeError("no route")
    recorder = Recorder()
    transport = await connected(backend, recorder)
    await transport.interrupt()
    assert recorder.of(ErrorFrame)
    assert recorder.only(InterruptSettled).cancelled is False


# --- end to end through the reducer --------------------------------------


async def test_a_scripted_turn_lands_in_the_transcript(backend: ScriptedBackend) -> None:
    backend.streams = [
        stream(*frames(
            ("turn_started", {"turn": 1}),
            ("assistant_text_delta", {"text": "looking"}),
            ("message", human_record("m2", "steer").model_dump(mode="json")),
            ("assistant_text_delta", {"text": "done"}),
            ("assistant_completed", assistant_record("a2", "done")),
            (
                "turn_ended",
                {"turn": 1, "outcome": {"kind": "finished", "stop_reason": "completed"}},
            ),
        ))
    ]
    state = SessionState()
    recorder = Recorder(state)
    transport = await connected(backend, recorder)
    stop_on(recorder, transport, TurnFinished)
    await transport.run()
    assert [getattr(entry, "content", "") for entry in state.timeline] == [
        "looking",
        "steer",
        "done",
    ]
    assert derive(state.facts) is Status.READY


# --- a fresh attach has no session id until the server assigns one --------


async def test_a_new_session_adopts_the_id_the_server_assigned(
    backend: ScriptedBackend,
) -> None:
    """``xbot tui`` with no ``--session`` asks for no session at all.

    The server then creates one and answers ``open_session`` with it. The client
    used to keep the empty id it had: its watchdog read ``list_threads("")``
    (404) and its frame translator rejected every frame of the session it had
    just created as foreign, so the UI ended up Disconnected.
    """
    backend.hello_session_id = ""
    backend.session = snapshot(session_id="assigned-1")
    recorder = Recorder()
    session = await connected(backend, recorder, session_id="")
    assert backend.opened[-1]["session_id"] is None, "no session was requested"
    assert session.session_id == "assigned-1"
    assert session.thread_id == THREAD


async def test_the_reader_subscribes_to_the_assigned_session(
    backend: ScriptedBackend,
) -> None:
    backend.hello_session_id = ""
    backend.session = snapshot(session_id="assigned-1")
    backend.streams = [
        stream(frame("turn_started", {"turn": 1}, session_id="assigned-1"), hold=True)
    ]
    recorder = Recorder()
    session = await connected(backend, recorder, session_id="")
    stop_on(recorder, session, TurnStarted)
    try:
        # Bounded on purpose: a transport that never applies the frame would
        # otherwise reconnect forever, and a hanging test is not a failing test.
        await asyncio.wait_for(session.run(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail("the reader never applied a frame of the session it attached to")
    finally:
        session.stop()
    assert backend.subscriptions[-1].session_id == "assigned-1", (
        "reading the session the server assigned is the whole point of attaching"
    )
    assert isinstance(recorder.only(TurnStarted), TurnStarted), (
        "its frames must not be rejected as foreign"
    )


async def test_the_watchdog_reads_the_assigned_session(
    backend: ScriptedBackend,
) -> None:
    backend.hello_session_id = ""
    backend.session = snapshot(session_id="assigned-1")
    backend.read_threads = []
    recorder = Recorder()
    await connected(backend, recorder, session_id="")
    assert backend.read_threads == ["assigned-1"], (
        "a watchdog read of an empty session id is a 404, not a status"
    )


# --- the command plane ----------------------------------------------------


async def test_the_catalogue_is_read_from_the_attached_thread(
    backend: ScriptedBackend,
) -> None:
    from XBotv2.tests.tui.factories import command

    backend.commands = (command("status"), command("model"))
    recorder = Recorder()
    session = await connected(backend, recorder)
    catalogue = await session.list_commands()
    assert [item.name for item in catalogue] == ["status", "model"]
    assert backend.command_reads == [(SESSION, THREAD)]


async def test_running_a_server_command_goes_through_the_backend(
    backend: ScriptedBackend,
) -> None:
    from XBotv2.tests.tui.factories import execution

    backend.command_result = execution(message="session s1 · idle")
    recorder = Recorder()
    session = await connected(backend, recorder)
    result = await session.run_command("/status verbose")
    assert result.message == "session s1 · idle"
    assert backend.command_calls == [
        {"session_id": SESSION, "thread_id": THREAD, "raw": "/status verbose"}
    ]


async def test_permission_and_user_input_responses_target_the_attached_thread(
    backend: ScriptedBackend,
) -> None:
    recorder = Recorder()
    session = await connected(backend, recorder)

    await session.respond_permission("permission:1", "allow", "session")
    await session.respond_permission("permission:2", "deny")
    await session.respond_user_input("question:1", "keep the backup")

    assert backend.interaction_responses == [
        {
            "kind": "permission",
            "session_id": SESSION,
            "thread_id": THREAD,
            "request_id": "permission:1",
            "decision": "allow",
            "scope": "session",
        },
        {
            "kind": "permission",
            "session_id": SESSION,
            "thread_id": THREAD,
            "request_id": "permission:2",
            "decision": "deny",
            "scope": "once",
        },
        {
            "kind": "user_input",
            "session_id": SESSION,
            "thread_id": THREAD,
            "request_id": "question:1",
            "answer": "keep the backup",
        },
    ]


# --- the windowed attach and older pages ----------------------------------


async def test_attach_asks_the_server_for_a_bounded_window(
    backend: ScriptedBackend,
) -> None:
    """The client must not download a conversation it is not going to hold.

    The bound is part of the attach request, so the server -- not the client --
    decides what the newest page is and hands back the cursor for the one before
    it.
    """
    recorder = Recorder()
    await build(backend, recorder, history_window=40).connect()

    assert backend.opened[-1]["history_limit"] == 40


async def test_switching_threads_asks_for_a_bounded_window_too(
    backend: ScriptedBackend,
) -> None:
    recorder = Recorder()
    transport = await connected(backend, recorder, history_window=40)

    await transport.switch(session_id="s2", thread_id="other")

    assert backend.opened[-1]["history_limit"] == 40


async def test_a_baseline_rebuild_keeps_the_window_bounded(
    backend: ScriptedBackend,
) -> None:
    """Recovery re-attaches; a rebuild that forgot the window would pull the
    whole conversation back in through the one path nobody watches."""
    backend.streams = [
        StreamScript(fail_with=cursor_expired()),
        stream(frame("turn_started", {"turn": 3}, sequence=8)),
    ]
    recorder = Recorder()
    transport = await connected(
        backend, recorder, cursor_recoveries=0, baseline_rebuilds=1, history_window=40,
    )
    stop_on(recorder, transport, TurnStarted)

    await transport.run()

    assert [opened["history_limit"] for opened in backend.opened] == [40, 40]


async def test_load_older_reads_the_page_before_the_cursor(
    backend: ScriptedBackend,
) -> None:
    recorder = Recorder()
    backend.pages = [history_page(human_record("u1", "first"))]
    transport = await connected(backend, recorder, history_window=25)

    await transport.load_older("cursor-1")

    assert backend.page_reads == [{
        "session_id": SESSION,
        "thread_id": THREAD,
        "cursor": "cursor-1",
        "limit": 25,
    }]
    loaded = recorder.only(OlderHistoryLoaded)
    assert [item.id for item in loaded.payload.items] == ["u1"]


async def test_a_failed_older_page_is_reported_not_raised(
    backend: ScriptedBackend,
) -> None:
    recorder = Recorder()
    backend.page_error = RuntimeError("server is unreachable")
    transport = await connected(backend, recorder)

    await transport.load_older("cursor-1")

    assert "unreachable" in recorder.only(OlderHistoryFailed).message
    assert not recorder.of(OlderHistoryLoaded)
