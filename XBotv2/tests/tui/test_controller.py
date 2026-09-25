"""The controller: coalesced rendering, one wiring point, no view-side logic.

The controller is exercised against the shared recording view from ``factories``,
so these tests need no terminal. What they check is the contract the app depends
on: events reach the reducer, rendering is coalesced, and the models the views
receive are built here rather than re-derived by each view.
"""

from __future__ import annotations

import pytest

from XBotv2.tests.tui.factories import (
    SESSION,
    THREAD,
    RecordingView,
    ScriptedBackend,
    assistant_record,
    frame,
    frames,
    history_page,
    human_record,
    snapshot,
    stream,
    thread,
)
from XBotv2.tui.controller import TuiController
from XBotv2.tui.events import ConnectionChanged, ThreadRead, TurnStarted
from XBotv2.tui.state import HistoryAvailable, HistoryFailed
from XBotv2.tui.status import Connection, ServerTurn, Status, derive
from XBotv2.tui.transport import TransportConfig


class FakeSleep:
    """A sleep worth counting; ``on_tick`` lets a test stop the loop."""

    def __init__(self) -> None:
        self.ticks = 0
        self.on_tick = None

    async def __call__(self, _seconds: float) -> None:
        self.ticks += 1
        if self.on_tick is not None:
            self.on_tick()


class FakeClock:
    """A clock worth asserting on."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def controller(
    backend: ScriptedBackend,
    *,
    view: RecordingView | None = None,
    clock: FakeClock | None = None,
    sleep=None,
    **overrides,
) -> tuple[TuiController, RecordingView]:
    view = view or RecordingView()
    fields = {
        "session_id": SESSION,
        "thread_id": THREAD,
        "reconnect_delays": (0.0,),
        "cursor_recoveries": 0,
        "baseline_rebuilds": 0,
        **overrides,
    }
    config = TransportConfig(**fields)
    return TuiController(
        backend,
        view=view,
        config=config,
        clock=clock or FakeClock(),
        sleep=sleep or _no_sleep,
    ), view


async def _no_sleep(_seconds: float) -> None:
    return None


# --- events reach the reducer --------------------------------------------


def test_dispatching_an_event_updates_the_state(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    assert derive(control.state.facts) is Status.READY


def test_dispatching_does_not_render(backend: ScriptedBackend) -> None:
    """A render per event is how render work used to pile up behind the stream."""
    control, view = controller(backend)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    assert view.renders == 0
    assert control.dirty is True


async def test_a_burst_of_events_costs_one_render(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    for _ in range(50):
        control.dispatch(TurnStarted(payload=_turn(1)))
    await control.flush()
    assert view.renders == 1


async def test_flushing_twice_without_changes_renders_once(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    assert await control.flush() is True
    assert await control.flush() is False
    assert view.renders == 1


async def test_every_flush_updates_all_four_views(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    await control.flush()
    assert view.transcripts
    assert view.statuses
    assert view.job_lists
    assert view.composers


# --- connect and run ------------------------------------------------------


async def test_connecting_renders_the_adopted_baseline(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.connect()
    assert view.renders == 1
    assert control.state.session_id == SESSION


async def test_connecting_reads_the_thread_so_a_running_turn_is_visible(
    backend: ScriptedBackend,
) -> None:
    backend.threads = (thread(turn_status="running"),)
    control, view = controller(backend)
    await control.connect()
    assert derive(control.state.facts) is Status.RUNNING
    assert "Running" in _plain(view.last_status)


async def test_a_failed_connect_still_renders_the_failure(backend: ScriptedBackend) -> None:
    backend.open_error = RuntimeError("refused")
    control, view = controller(backend)
    with pytest.raises(RuntimeError):
        await control.connect()
    assert derive(control.state.facts) is Status.DISCONNECTED
    assert _recorded_errors(control), "the failure must be in the transcript"


async def test_running_the_stream_renders_the_result(backend: ScriptedBackend) -> None:
    backend.streams = [
        stream(*frames(
            ("turn_started", {"turn": 1}),
            ("assistant_completed", assistant_record("a1", "hello")),
            (
                "turn_ended",
                {
                    "turn": 1,
                    "outcome": {"kind": "finished", "stop_reason": "completed"},
                },
            ),
        ))
    ]
    control, view = controller(backend, reconnect_delays=())
    await control.connect()
    await control.run()
    from XBotv2.tui.timeline import AssistantEntry

    assert [
        entry.content for entry in control.state.timeline if isinstance(entry, AssistantEntry)
    ] == ["hello"]
    assert view.renders >= 1


# --- models ---------------------------------------------------------------


async def test_the_status_model_reflects_a_running_turn(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    await control.connect()
    control.dispatch(TurnStarted(payload=_turn(3)))
    model = control.status_model()
    assert derive(model.facts) is Status.RUNNING


def test_the_activity_text_reports_the_turn_duration(backend: ScriptedBackend) -> None:
    """Progress lives in the status line, from the turn's recorded start time."""
    clock = FakeClock(100.0)
    control, _view = controller(backend, clock=clock)
    control.dispatch(TurnStarted(payload=_turn(2)))
    clock.advance(4.25)
    assert control.activity() == "turn:2 4.2s"


def test_there_is_no_activity_before_a_turn(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    assert control.activity() == ""


def test_the_composer_says_steer_while_a_turn_runs(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    control.dispatch(TurnStarted(payload=_turn(1)))
    hint = _composer_hint(control.composer_model())
    assert "steer" in hint.lower()
    assert "queue" not in hint.lower()


# --- actions delegate -----------------------------------------------------


async def test_submitting_goes_through_the_transport_with_a_client_id(
    backend: ScriptedBackend,
) -> None:
    control, _view = controller(backend)
    await control.connect()
    input_id = await control.submit("hello")
    assert backend.sent[0]["request_id"] == input_id
    assert backend.sent[0]["delivery"] == "steer"


async def test_submitting_renders_the_optimistic_entry(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.connect()
    before = view.renders
    await control.submit("hello")
    assert view.renders > before
    assert any(
        getattr(entry, "content", "") == "hello" for entry in control.state.timeline
    )
    assert view.pages == ["tail"], "sending is an explicit return to the live tail"


async def test_interrupting_delegates_and_renders(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.connect()
    await control.interrupt()
    assert backend.interrupts == 1
    assert view.renders >= 2


async def test_paging_delegates_to_the_view(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.connect()
    assert await control.page_older() is True
    assert await control.page_newer() is True
    await control.go_to_tail()
    assert view.pages == ["older", "newer", "tail"]


# --- the render loop ------------------------------------------------------


async def test_the_render_loop_renders_while_events_arrive(backend: ScriptedBackend) -> None:
    sleeper = FakeSleep()
    control, view = controller(backend, sleep=sleeper)
    control.dispatch(ConnectionChanged(Connection.CONNECTED))
    sleeper.on_tick = lambda: control.stop() if sleeper.ticks >= 3 else None
    await control.flush_loop(interval=0.05)
    assert sleeper.ticks == 3
    assert view.renders == 1, "one render for the whole loop"


async def test_the_render_loop_stops_without_rendering_when_nothing_changed(
    backend: ScriptedBackend,
) -> None:
    sleeper = FakeSleep()
    control, view = controller(backend, sleep=sleeper)
    await control.flush()
    sleeper.on_tick = lambda: control.stop() if sleeper.ticks >= 3 else None
    await control.flush_loop(interval=0.05)
    assert view.renders == 1, "only the explicit flush rendered"


async def test_a_stopped_controller_does_not_render_again(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.flush()
    control.stop()
    control.dispatch(ConnectionChanged(Connection.DISCONNECTED))
    await control.flush_loop(interval=0.05)
    assert view.renders == 1


# --- helpers --------------------------------------------------------------


def _turn(turn: int):
    from XBotv2.agentloop.protocol import LoopTurnStarted

    return LoopTurnStarted(turn=turn)


def _plain(model) -> str:
    from XBotv2.tui.view.status_bar import render_status_line

    return render_status_line(model, width=120).plain


def _composer_hint(model) -> str:
    from XBotv2.tui.view.composer import composer_hint

    return composer_hint(model)


def _recorded_errors(control: TuiController) -> list:
    """Errors the reducer recorded, read from the transcript."""
    from XBotv2.tui.timeline import ErrorEntry

    return [entry for entry in control.state.timeline if isinstance(entry, ErrorEntry)]


# --- attachments ----------------------------------------------------------


def image(data: str = "AAAA") -> object:
    from XBotv2.session.contracts import ImageInput

    return ImageInput(data=data, media_type="image/png")


def test_attaching_an_image_shows_it_in_the_composer(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    control.attach(image())
    assert len(control.attachments) == 1
    model = control.composer_model()
    assert model.pending_images == 1
    assert "1 image" in _composer_hint(model)


def test_attachments_can_be_cleared(backend: ScriptedBackend) -> None:
    control, _view = controller(backend)
    control.attach(image())
    control.clear_attachments()
    assert control.attachments == ()
    assert control.composer_model().pending_images == 0


async def test_submitting_sends_the_attachments_and_consumes_them(
    backend: ScriptedBackend,
) -> None:
    control, _view = controller(backend)
    await control.connect()
    control.attach(image("Zm9v"))
    await control.submit("look at this")
    assert backend.sent[-1]["images"] == [image("Zm9v")], (
        "the image travels with the message, as the repository's own model"
    )
    assert control.attachments == (), "the attachment is consumed by the submission"


async def test_submitting_an_attachment_with_no_text_is_allowed(
    backend: ScriptedBackend,
) -> None:
    control, _view = controller(backend)
    await control.connect()
    control.attach(image())
    await control.submit("")
    assert backend.sent, "an image-only message is still a message"
    assert backend.sent[-1]["content"] == ""
    assert backend.sent[-1]["images"]


# --- reading a subagent thread -------------------------------------------


async def test_the_composer_is_read_only_on_a_subagent_thread(
    backend: ScriptedBackend,
) -> None:
    control, _view = controller(backend)
    await control.connect()
    assert control.composer_model().read_only is False
    control.dispatch(
        ThreadRead(payload=thread(kind="subagent", thread_id="child")),
    )
    assert control.composer_model().read_only is True
    assert "read-only" in _composer_hint(control.composer_model()).lower()


async def test_switching_to_a_child_thread_attaches_to_it(
    backend: ScriptedBackend,
) -> None:
    from XBotv2.tests.tui.factories import snapshot

    control, _view = controller(backend)
    await control.connect()
    backend.session = snapshot(
            thread_id="child",
            history=[human_record("node-1", "sub")],
        )
    backend.threads = (
        thread(),
        thread(thread_id="child", kind="subagent", agent="task"),
    )
    await control.switch_thread("child")
    assert backend.opened[-1]["thread_id"] == "child"
    assert backend.opened[-1]["mode"] == "resume"
    assert control.state.thread_id == "child"
    assert control.composer_model().read_only is True, "the server's kind is the authority"


async def test_switching_back_to_main_reuses_the_server_listing(
    backend: ScriptedBackend,
) -> None:
    from XBotv2.tests.tui.factories import snapshot

    control, _view = controller(backend)
    await control.connect()
    backend.threads = (thread(), thread(thread_id="child", kind="subagent"))
    backend.session = snapshot(thread_id="child")
    await control.switch_thread("child")
    backend.session = snapshot(thread_id=THREAD)
    await control.switch_thread("")
    assert backend.opened[-1]["thread_id"] == THREAD, "no thread means the main one"
    assert control.composer_model().read_only is False


async def test_the_thread_list_comes_from_the_server(backend: ScriptedBackend) -> None:
    backend.threads = (thread(), thread(thread_id="child", kind="subagent"))
    control, _view = controller(backend)
    threads = await control.threads()
    assert [item.thread_id for item in threads] == [THREAD, "child"]


# --- the queued-input panel ------------------------------------------------


async def test_the_queue_is_rendered_from_state(backend: ScriptedBackend) -> None:
    from XBotv2.tui.events import QueueReplaced
    from XBotv2.session.protocol import QueueUpdatedData

    control, view = controller(backend)
    await control.connect()
    control.dispatch(
        QueueReplaced(
            payload=QueueUpdatedData(
                items=[
                    {
                        "message_id": "q1",
                        "content": "after this",
                        "target": "next-turn",
                    }
                ]
            )
        )
    )
    await control.flush()
    assert [item.message_id for item in view.queues[-1]] == ["q1"]


async def test_an_empty_queue_is_rendered_as_empty(backend: ScriptedBackend) -> None:
    control, view = controller(backend)
    await control.connect()
    await control.flush()
    assert view.queues[-1] == ()


# --- the server command catalogue -----------------------------------------


async def test_the_controller_reads_the_catalogue(backend: ScriptedBackend) -> None:
    from XBotv2.tests.tui.factories import command

    backend.commands = (command("status"), command("model"))
    control, _view = controller(backend)
    await control.connect()
    assert [item.name for item in await control.commands()] == ["status", "model"]


async def test_the_controller_runs_a_server_command(backend: ScriptedBackend) -> None:
    from XBotv2.tests.tui.factories import command, execution

    backend.commands = (command("status"),)
    backend.command_result = execution(message="fine")
    control, _view = controller(backend)
    await control.connect()
    result = await control.run_command("/status")
    assert result.message == "fine"
    assert backend.command_calls[-1]["raw"] == "/status"


# --- older history --------------------------------------------------------


async def test_paging_up_at_the_oldest_entry_asks_the_server_for_more(
    backend: ScriptedBackend,
) -> None:
    backend.session = snapshot(history_cursor="c1")
    control, view = controller(backend, history_window=10)
    view.can_move_older = False
    await control.connect()

    requested = await control.page_older()

    assert requested is True
    assert backend.page_reads == [{
        "session_id": SESSION, "thread_id": THREAD, "cursor": "c1", "limit": 10,
    }]


async def test_paging_up_inside_the_window_asks_the_server_for_nothing(
    backend: ScriptedBackend,
) -> None:
    backend.session = snapshot(history_cursor="c1")
    control, view = controller(backend)
    await control.connect()

    requested = await control.page_older()

    assert requested is True
    assert backend.page_reads == []


async def test_a_page_that_arrived_is_not_asked_for_again(
    backend: ScriptedBackend,
) -> None:
    """Once the client holds the beginning there is no page left to ask for."""
    backend.session = snapshot(history_cursor="c1")
    backend.pages = [history_page(human_record("u1", "older"))]
    control, view = controller(backend)
    view.can_move_older = False
    await control.connect()
    await control.page_older()

    assert await control.page_older() is False
    assert len(backend.page_reads) == 1


async def test_a_failed_page_is_retried_on_the_next_ask(
    backend: ScriptedBackend,
) -> None:
    backend.session = snapshot(history_cursor="c1")
    backend.page_error = RuntimeError("server is unreachable")
    control, view = controller(backend)
    view.can_move_older = False
    await control.connect()
    await control.page_older()
    assert isinstance(control.state.older, HistoryFailed)

    backend.page_error = None
    retried = await control.page_older()

    assert retried is True
    assert len(backend.page_reads) == 2


async def test_nothing_is_asked_when_the_client_holds_the_beginning(
    backend: ScriptedBackend,
) -> None:
    control, view = controller(backend)
    view.can_move_older = False
    await control.connect()

    assert await control.page_older() is False
    assert backend.page_reads == []


async def test_residency_lets_a_loaded_page_go_when_the_reader_returns_to_the_tail(
    backend: ScriptedBackend,
) -> None:
    """A reader who paged back and then returned to the tail is not served by
    holding every page they walked past; the cursor chain makes them reachable
    again, so the client can let them go."""
    backend.session = snapshot(
        history=[human_record("a9", "tail")],
        history_cursor="c1",
    )
    backend.pages = [history_page(human_record("u1", "older"))]
    control, view = controller(backend, history_retention=1)
    view.can_move_older = False
    view.at_end = False
    await control.connect()
    await control.page_older()
    assert control.state.timeline.ids() == ("u1", "a9")

    view.at_end = True
    await control.flush()

    assert control.state.timeline.ids() == ("a9",)
    # ...and the page the reader gave up residency for can be fetched again.
    assert control.state.older == HistoryAvailable(cursor="c1")


async def test_a_page_the_reader_is_looking_at_is_never_released(
    backend: ScriptedBackend,
) -> None:
    backend.session = snapshot(
        history=[human_record("a9", "tail")],
        history_cursor="c1",
    )
    backend.pages = [history_page(human_record("u1", "older"))]
    control, view = controller(backend, history_retention=1)
    view.can_move_older = False
    view.at_end = False
    await control.connect()
    await control.page_older()

    await control.flush()

    assert control.state.timeline.ids() == ("u1", "a9")
