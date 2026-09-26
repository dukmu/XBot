"""The status line is a pure function of facts, and never exceeds its width.

This is the line the user reported as wrong: it said Ready while the agent was
running. So the tests here care about two things -- that the status it shows comes
from the derived facts, and that whatever it shows fits the terminal it is given.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from XBotv2.core.domain import (
    GenerationSettings,
    ModelRoute,
    ProviderMeasured,
    ReasoningGenerationMode,
    RequestObservation,
    ResolvedModelSelection,
    TokenCounters,
    TurnRequest,
    UsageDelta,
    UsageSnapshot,
)
from XBotv2.tui.events import ConnectionChanged
from XBotv2.tui.status import (
    Connection,
    Interaction,
    Interrupt,
    ServerTurn,
    Status,
    StatusFacts,
    derive,
)
from XBotv2.tui.view.status_bar import (
    StatusLine,
    render_status_line,
)


def line(**overrides) -> StatusLine:
    """The shortest way to describe what a status line is given."""
    facts = StatusFacts(connection=Connection.CONNECTED)
    fields = {
        "facts": facts,
        "session_label": "s1",
        "agent_name": "XBotv2",
        "provider": "deepseek",
        "model": "v4",
        "model_mode": "",
        "status_slots": {},
        "context_window": 0,
        "usage": UsageSnapshot(),
        "context_input_tokens": 0,
        "queue_depth": 0,
        "activity": "",
        "workspace": "",
        "thread_id": "",
        "thread_kind": "main",
    }
    fields.update(overrides)
    return StatusLine(**fields)


def plain(model: StatusLine, width: int = 200) -> str:
    return render_status_line(model, width=width).plain


# --- what it says ---------------------------------------------------------


def test_it_shows_the_status_the_facts_derive() -> None:
    model = line(facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING))
    assert "Running" in plain(model)


def test_a_running_turn_is_running_even_without_a_local_turn() -> None:
    """The reported defect, at the rendering layer: the server says running, so
    the line says running."""
    model = line(
        facts=StatusFacts(
            connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING, turn_open=False
        )
    )
    assert "Ready" not in plain(model)
    assert "Running" in plain(model)


def test_a_pending_approval_is_announced() -> None:
    model = line(facts=StatusFacts(connection=Connection.CONNECTED, interaction=Interaction.PERMISSION))
    assert "Approval" in plain(model)


def test_an_interrupt_in_flight_is_announced() -> None:
    model = line(facts=StatusFacts(connection=Connection.CONNECTED, interrupt=Interrupt.REQUESTED))
    assert "Interrupting" in plain(model)


def test_background_jobs_appear_in_the_same_line() -> None:
    """The Tasks panel and this line must not contradict each other."""
    model = line(facts=StatusFacts(connection=Connection.CONNECTED, jobs_running=2))
    assert "2 tasks" in plain(model)


def test_a_disconnected_client_says_so() -> None:
    model = line(facts=StatusFacts(connection=Connection.DISCONNECTED))
    assert "Disconnected" in plain(model)


# --- optional detail ------------------------------------------------------


def test_the_queue_depth_is_shown_only_while_something_is_queued() -> None:
    assert "queued" not in plain(line())
    assert "queued:2" in plain(line(queue_depth=2))


def test_session_usage_totals_are_visible_in_the_status_line() -> None:
    model = line(usage=UsageSnapshot(total_counters=TokenCounters(input=100, output=20)))
    shown = plain(model)
    assert "in:100" in shown
    assert "out:20" in shown


def test_usage_breakdown_context_size_and_cache_rate_are_shown() -> None:
    selection = ResolvedModelSelection(
        route=ModelRoute(provider="test", model="test-model"),
        generation=GenerationSettings(
            mode=ReasoningGenerationMode(effort="low"), max_output_tokens=512
        ),
        context_window=4096,
    )
    observation = RequestObservation(
        selection=selection,
        purpose=TurnRequest(turn_id="turn-1"),
        estimated_input_tokens=350,
        observed_context=ProviderMeasured(tokens=350),
    )
    snapshot = UsageSnapshot(
        total_counters=TokenCounters(input=300, output=50, cache_read=100),
        requests=(observation,),
        latest_turn_observation=observation,
    )

    shown = plain(line(
        usage=snapshot,
        context_window=4096,
        context_input_tokens=350,
    ))

    assert "in:300" in shown
    assert "out:50" in shown
    assert "ctx:350/4096" in shown
    assert "cache:25%" in shown


def test_context_overflow_is_explicit_instead_of_looking_like_a_valid_ratio() -> None:
    shown = plain(line(
        context_window=4096,
        context_input_tokens=5700,
        context_input_estimated=True,
    ))

    assert "ctx:~5.7k/4096!" in shown


def test_statusline_combines_session_context_and_usage() -> None:
    snapshot = UsageSnapshot(
        total_counters=TokenCounters(input=300, output=50, cache_read=100)
    )
    status = plain(line(
        session_label="session-1",
        usage=snapshot,
        context_window=4096,
        context_input_tokens=350,
    ), width=100)

    assert "session:session-1" in status
    assert "ctx:350/4096" in status
    assert "in:300" in status
    assert "out:50" in status
    assert "cache:25%" in status


def test_statusline_orders_session_context_and_model() -> None:
    snapshot = UsageSnapshot(
        total_counters=TokenCounters(input=300, output=50, cache_read=100)
    )
    shown = plain(line(
        session_label="session-1",
        thread_id="agent",
        usage=snapshot,
        context_window=4096,
        context_input_tokens=350,
    ), width=120)

    assert "session:session-1" in shown
    assert shown.index("ctx:350/4096") < shown.index("session:session-1")
    assert shown.index("session:session-1") < shown.index("deepseek/v4")
    assert "in:300" in shown


def test_live_usage_survives_at_80_cells_ahead_of_low_priority_details() -> None:
    model = line(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
        ),
        activity="turn:123 5.6s",
        queue_depth=3,
        agent_name="a-very-long-agent-name",
        model_mode="adaptive",
        status_slots={"goal": "ship"},
        workspace="/workspace/with/a/long/path",
        usage=UsageSnapshot(
            total_counters=TokenCounters(input=12_345, output=2_000, cache_read=3_000)
        ),
    )

    shown = plain(model, width=80)

    assert "Running" in shown
    assert "turn:123 5.6s" in shown
    assert "queued:3" in shown
    assert "in:12.3k" in shown
    assert "out:2.0k" in shown
    assert "cache:20%" in shown
    assert "a-very-long-agent-name" not in shown


@pytest.mark.parametrize("width", range(1, 24))
def test_statusline_fits_terminal_cells_for_unicode(width: int) -> None:
    model = line(
        session_label="会话 👩‍💻 界面很长",
        thread_id="线程-🙂",
        provider="供应商",
        model="模型-🧠",
        workspace="/工作区/非常长的路径",
    )

    status = render_status_line(model, width=width).plain

    assert cell_len(status) <= width
    assert not status.endswith("\u200d")


def test_the_activity_text_is_shown_while_a_turn_runs() -> None:
    assert "turn:3" in plain(line(activity="turn:3 1.2s"))


def test_status_line_combines_runtime_and_session_context_at_the_bottom() -> None:
    shown = plain(line(model_mode="adaptive"))
    assert "XBotv2" in shown
    assert "deepseek/v4" in shown
    assert "session:s1" in shown
    assert "mode:adaptive" in shown


def test_status_slots_are_shown() -> None:
    assert "goal:ship" in plain(line(status_slots={"goal": "ship"}))


def test_status_detail_follows_the_planned_priority_order() -> None:
    model = line(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
        ),
        activity="turn:3 1.2s",
        queue_depth=2,
        agent_name="agent-a",
        model_mode="high",
        status_slots={"goal": "ship"},
        workspace="/workspace",
        usage=UsageSnapshot(
            total_counters=TokenCounters(input=3_000, output=500, cache_read=1_000)
        ),
    )

    shown = plain(model)
    fields = [
        "turn:3",
        "queued:2",
        "in:3.0k",
        "out:500",
        "cache:25%",
        "agent:agent-a",
        "mode:high",
        "goal:ship",
        "cwd:/workspace",
    ]
    positions = [shown.index(field) for field in fields]
    assert positions == sorted(positions)


def test_unicode_optional_fields_do_not_overrun_the_terminal_cell_width() -> None:
    model = line(
        agent_name="",
        status_slots={"项目": "🎨"},
        workspace="x",
    )

    rendered = render_status_line(model, width=20)
    assert cell_len(rendered.plain) <= 20
    assert "cwd:x" not in rendered.plain


# --- it must fit ----------------------------------------------------------


@pytest.mark.parametrize("width", [20, 28, 32, 40, 48, 60, 80, 96, 120, 160, 200])
def test_the_line_never_exceeds_the_width_it_is_given(width: int) -> None:
    """A status line that overflows corrupts the layout below it."""
    model = line(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
            jobs_running=3,
            compaction=False,
        ),
        status_slots={"goal": "ship the rewrite", "effort": "high"},
        context_window=128_000,
        context_input_tokens=64_000,
        usage=UsageSnapshot(
            total_counters=TokenCounters(input=1_225_567, output=9_000)
        ),
        queue_depth=4,
        activity="turn:12 34.5s",
    )
    rendered = render_status_line(model, width=width)
    assert rendered.plain
    assert cell_len(rendered.plain) <= width, f"{width}: {rendered.plain!r}"


@pytest.mark.parametrize("width", [20, 28, 32, 40, 60, 96, 160])
def test_the_status_itself_survives_narrowing(width: int) -> None:
    """Detail yields to the one thing the user must always be able to read."""
    model = line(
        facts=StatusFacts(
            connection=Connection.CONNECTED,
            server_turn=ServerTurn.RUNNING,
            interaction=Interaction.USER_INPUT,
        )
    )
    assert "Waiting for user" in render_status_line(model, width=width).plain


def test_an_absurdly_narrow_terminal_still_produces_something() -> None:
    assert render_status_line(line(), width=4).plain


def test_a_very_long_session_title_is_clipped() -> None:
    model = line(session_label="a-session-with-a-very-long-name")
    assert len(plain(model, width=40)) <= 40


# --- the model is derived, not assembled ---------------------------------


def test_the_model_can_be_built_from_a_session_state() -> None:
    """The view must not re-derive status; it asks the model for it."""
    from XBotv2.tui.state import SessionState
    from XBotv2.tui.view.status_bar import status_line_for

    state = SessionState()
    state.facts = StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING)
    model = status_line_for(state, session_label="s1", workspace="")
    assert derive(model.facts) is Status.RUNNING
    assert "Running" in plain(model)


# --- which thread is on screen --------------------------------------------


def test_the_main_thread_is_not_announced() -> None:
    assert "thread:" not in plain(line())
    assert "subagent:" not in plain(line())


def test_a_subagent_view_names_the_thread() -> None:
    model = line(thread_id="child-1", thread_kind="subagent")
    assert "subagent:child-1" in plain(model)


def test_the_subagent_marker_survives_a_narrow_terminal() -> None:
    """Detail is given up from the end, so the thread marker goes last."""
    model = line(thread_id="child-1", thread_kind="subagent")
    assert "subagent:child-1" in plain(model, width=30)
    assert "cwd:" not in plain(model, width=30)


# --- the status report: what /status renders, locally ---------------------
#
# The server publishes a /status command that assembles this text from its own
# state. The client already holds every input (the thread read, the queue, the
# jobs), so it renders the report itself: no round trip, and it can say things
# the server cannot, such as how long the running turn has been going.


def report_state(**overrides):
    from XBotv2.tests.tui.factories import SESSION, THREAD, thread as thread_summary
    from XBotv2.tui.events import SnapshotAdopted, ThreadRead, UserInputSubmitted
    from XBotv2.tui.state import SessionState, reduce
    from XBotv2.tui.status import Connection
    from XBotv2.tests.tui.factories import snapshot

    state = SessionState()
    reduce(state, ConnectionChanged(Connection.CONNECTED))
    reduce(state, SnapshotAdopted(snapshot(workspace_root="/w", provider="p1", model="m1")))
    summary = thread_summary(session_id=SESSION, thread_id=THREAD)
    fields = {
        "message_count": 12,
        "model": "m1",
        "provider": "p1",
        "workspace_root": "/w",
    }
    fields.update(overrides.pop("summary", {}))
    reduce(state, ThreadRead(payload=thread_summary(**fields)))
    return state


def test_the_report_names_the_session_thread_and_workspace() -> None:
    from XBotv2.tui.view.status_bar import status_report

    text = status_report(report_state(), workspace="/w")
    assert "ID: s1" in text
    assert "Thread: agent" in text
    assert "Workspace: /w" in text


def test_the_report_carries_the_derived_status_not_a_guess() -> None:
    from XBotv2.tui.view.status_bar import status_report

    text = status_report(report_state(summary={"turn_status": "running"}))
    assert "Running" in text, "the same derivation the footer uses"
    assert "Ready" not in text


def test_the_report_counts_what_the_server_queued() -> None:
    """Being submitted is not being queued: only the server's queue counts."""
    from XBotv2.session.protocol import QueueUpdatedData
    from XBotv2.tui.events import QueueReplaced
    from XBotv2.tui.state import reduce
    from XBotv2.tui.view.status_bar import status_report

    state = report_state()
    reduce(
        state,
        QueueReplaced(
            payload=QueueUpdatedData(
                items=[{"message_id": "q1", "content": "later", "target": "next-turn"}]
            )
        ),
    )
    text = status_report(state)
    assert "Queued: 1" in text


def test_the_report_says_so_before_the_first_thread_read() -> None:
    from XBotv2.tui.state import SessionState
    from XBotv2.tui.view.status_bar import status_report

    text = status_report(SessionState())
    assert "no thread read yet" in text.lower()


def test_the_report_marks_a_subagent_thread_read_only() -> None:
    from XBotv2.tui.view.status_bar import status_report

    text = status_report(report_state(summary={"kind": "subagent"}))
    assert "read-only" in text


def test_thread_read_updates_the_authoritative_session_title() -> None:
    from XBotv2.session.contracts import ThreadSummary
    from XBotv2.tui.events import ThreadRead
    from XBotv2.tui.state import SessionState, reduce

    state = SessionState(title="session-id")
    thread = ThreadSummary(
        session_id="session-id",
        thread_id="main",
        status="active",
        title="Caption from the server",
    )

    reduce(state, ThreadRead(payload=thread))

    assert state.title == "Caption from the server"
