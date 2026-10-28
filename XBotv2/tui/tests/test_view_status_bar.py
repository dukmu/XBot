"""Terminal context fits its available width and preserves urgent state."""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from XBotv2.core.api.domain import UsageSnapshot
from XBotv2.tui.impl.status import Connection, Interaction, ServerTurn, StatusFacts
from XBotv2.tui.impl.view.status_bar import StatusLine, render_status_line


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


def test_status_line_shows_context_without_repeating_turn_activity() -> None:
    model = line(
        facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING),
        activity="turn:3 1.2s",
        context_window=4096,
        context_input_tokens=350,
        model_mode="high",
    )
    shown = plain(model)
    assert "deepseek/v4" in shown
    assert "high" in shown
    assert "ctx:350/4.1k" in shown
    assert "Running" not in shown
    assert "turn:3" not in shown


def test_status_line_preserves_urgent_state_and_thread() -> None:
    model = line(
        facts=StatusFacts(connection=Connection.CONNECTED, interaction=Interaction.PERMISSION),
        thread_id="child-1", thread_kind="subagent", queue_depth=2,
    )
    shown = plain(model)
    assert "Approval required" in shown
    assert "subagent:child-1" in shown
    assert "queued:2" in shown


def test_context_overflow_is_explicit() -> None:
    shown = plain(line(context_window=4096, context_input_tokens=5700,
                       context_input_estimated=True))
    assert "ctx:~5.7k/4.1k!" in shown


@pytest.mark.parametrize("width", range(1, 120))
def test_status_line_fits_terminal_cells(width: int) -> None:
    model = line(
        facts=StatusFacts(connection=Connection.DISCONNECTED),
        provider="供应商", model="模型-🧠", thread_kind="subagent", thread_id="线程-🙂",
    )
    shown = plain(model, width)
    assert cell_len(shown) <= width
    assert not shown.endswith("\u200d")


def test_thread_read_updates_the_authoritative_session_title() -> None:
    from XBotv2.session.api.contracts import ThreadSummary
    from XBotv2.tui.impl.state import SessionState, reduce

    state = SessionState(title="session-id")
    thread = ThreadSummary(
        session_id="session-id",
        thread_id="main",
        status="active",
        title="Caption from the server",
    )

    reduce(state, thread)

    assert state.title == "Caption from the server"
