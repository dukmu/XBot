"""Every selection builds its rows the same way, and the rows are pure data.

One screen offers all of them, so what is worth testing is exactly the part that
differs per command: which model the rows come from, what a row shows, and what
value it carries when chosen. No terminal is needed for any of that.
"""

from __future__ import annotations

from XBotv2.tui.tests.factories import thread
from XBotv2.tui.impl.view.pickers import (
    session_options,
    thread_options,
)


def summary(session_id: str = "s1", **overrides):
    from XBotv2.session.api.contracts import SessionSummary

    fields = {
        "session_id": session_id,
        "title": "",
        "status": "active",
        "workspace_root": "/w",
    }
    fields.update(overrides)
    return SessionSummary(**fields)


# --- sessions -------------------------------------------------------------


def test_session_rows_carry_the_id_the_client_switches_to() -> None:
    options = session_options(
        [summary("s1", title="work"), summary("s2")],
        current="s1",
    )
    assert [option.value for option in options] == ["s1", "s2"]
    assert options[0].label == "s1 · work"
    assert options[1].label == "s2", "a session without a title is named by its id"
    assert "current" in options[0].detail
    assert "active" in options[0].detail
    assert "/w" in options[0].detail


def test_a_long_session_title_cannot_hide_the_stable_session_id() -> None:
    [option] = session_options([
        summary("20260929-184532-ae5e", title="很长的会话标题" * 20),
    ])
    assert option.label.startswith("20260929-184532-ae5e · ")


# --- threads --------------------------------------------------------------


def test_thread_rows_show_the_id_alongside_a_title() -> None:
    options = thread_options(
        [thread(thread_id="child-1", kind="subagent", agent="task", title="Research", message_count=4)]
    )
    assert options[0].value == "child-1"
    assert "Research" in options[0].label and "child-1" in options[0].label
    assert "subagent" in options[0].detail
    assert "4 msg" in options[0].detail


def test_thread_rows_name_liveness_and_current_thread() -> None:
    options = thread_options(
        [
            thread(thread_id="main", kind="main", turn_status="idle"),
            thread(
                thread_id="worker-1",
                kind="subagent",
                agent="reviewer",
                turn_status="running",
            ),
            thread(
                thread_id="worker-2",
                kind="subagent",
                agent="researcher",
                status="inactive",
            ),
        ],
        current="worker-1",
    )

    assert options[0].label.startswith("Main")
    assert "idle" in options[0].detail
    assert "working" in options[1].detail
    assert "current" in options[1].detail
    assert "closed" in options[2].detail


def test_thread_rows_do_not_repeat_an_id_as_the_title() -> None:
    options = thread_options([thread(thread_id="agent", title="agent")])
    assert options[0].label == "Main · agent"


# --- filtering: the one chooser offers it, so long lists stay usable ------


def test_a_query_keeps_only_matching_rows() -> None:
    from XBotv2.tui.impl.view.pickers import filter_options
    rows = session_options([summary("first", title="alpha"), summary("second", title="beta")])
    assert [row.value for row in filter_options(rows, "beta")] == ["second"]
    assert [row.value for row in filter_options(rows, "first active")] == ["first"]
    assert filter_options(rows, "first beta") == ()
    assert filter_options(rows, "") == rows
