"""Every selection builds its rows the same way, and the rows are pure data.

One screen offers all of them, so what is worth testing is exactly the part that
differs per command: which model the rows come from, what a row shows, and what
value it carries when chosen. No terminal is needed for any of that.
"""

from __future__ import annotations

from XBotv2.tests.tui.factories import agent_list, catalog, thread
from XBotv2.tui.view.pickers import (
    agent_options,
    effort_options,
    effort_tiers,
    model_options,
    provider_options,
    session_options,
    thread_options,
)


def summary(session_id: str = "s1", **overrides):
    from XBotv2.session.contracts import SessionSummary

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
    options = session_options([summary("s1", title="work"), summary("s2")])
    assert [option.value for option in options] == ["s1", "s2"]
    assert options[0].label == "work"
    assert options[1].label == "s2", "a session without a title is named by its id"


# --- threads --------------------------------------------------------------


def test_thread_rows_show_the_id_alongside_a_title() -> None:
    options = thread_options(
        [thread(thread_id="child-1", kind="subagent", agent="task", title="Research", message_count=4)]
    )
    assert options[0].value == "child-1"
    assert "Research" in options[0].label and "child-1" in options[0].label
    assert "subagent" in options[0].detail
    assert "4 msg" in options[0].detail


def test_thread_rows_do_not_repeat_an_id_as_the_title() -> None:
    options = thread_options([thread(thread_id="agent", title="agent")])
    assert options[0].label == "agent"


# --- providers and models -------------------------------------------------


def test_provider_rows_mark_the_current_and_default_ones() -> None:
    options = provider_options(catalog(), current="p2")
    assert [option.value for option in options] == ["p1", "p2"]
    assert "default" in options[0].detail
    assert "current" in options[1].detail


def test_model_rows_are_the_models_of_one_provider() -> None:
    options = model_options(catalog(), provider="p1")
    assert [option.value for option in options] == ["m1", "m2"]
    assert "default for p1" in options[0].detail
    assert "4096 ctx" in options[0].detail


def test_model_rows_are_empty_for_an_unknown_provider() -> None:
    assert model_options(catalog(), provider="nope") == ()


def test_effort_tiers_come_from_the_current_model() -> None:
    assert effort_tiers(catalog(), provider="p1", model="m1") == ("low", "high")
    assert effort_tiers(catalog(), provider="p1", model="m2") == ("low",)
    assert effort_tiers(catalog(), provider="p1", model="unknown") == ()


def test_effort_rows_mark_the_current_tier() -> None:
    options = effort_options(("low", "high"), current="high")
    assert [option.value for option in options] == ["low", "high"]
    assert options[1].detail == "current"


# --- agents ---------------------------------------------------------------


def test_agent_rows_mark_the_active_agent() -> None:
    options = agent_options(agent_list())
    assert [option.value for option in options] == ["default", "reviewer"]
    assert "active" in options[0].detail
    assert "reviews diffs" in options[1].detail


# --- filtering: the one chooser offers it, so long lists stay usable ------


def test_a_query_keeps_only_matching_rows() -> None:
    from XBotv2.tui.view.pickers import filter_options

    rows = provider_options(catalog())
    assert [row.value for row in filter_options(rows, "p2")] == ["p2"]
    assert [row.value for row in filter_options(rows, "anthropic")] == ["p2"], (
        "the detail is searched too: the protocol is what the user may remember"
    )


def test_every_term_must_match() -> None:
    from XBotv2.tui.view.pickers import filter_options

    rows = model_options(catalog(), provider="p1")
    assert [row.value for row in filter_options(rows, "m1 4096")] == ["m1"]
    assert filter_options(rows, "m1 8192") == ()


def test_an_empty_query_keeps_everything() -> None:
    from XBotv2.tui.view.pickers import filter_options

    rows = provider_options(catalog())
    assert filter_options(rows, "") == rows
