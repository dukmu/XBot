"""Status derivation: the server is the authority on whether a turn runs.

These tests are the falsifying evidence for invariant I1 and for the reported
defect "the agent is running but the TUI shows Ready". They exercise the pure
``derive``/``status_label`` functions only -- no Textual, no IO.
"""

from __future__ import annotations

import pytest

from XBotv2.tui.status import (
    Connection,
    Interaction,
    Interrupt,
    ServerTurn,
    Status,
    StatusFacts,
    derive,
    status_label,
)


def facts(**overrides) -> StatusFacts:
    base = StatusFacts(connection=Connection.CONNECTED)
    return StatusFacts(**{**base.__dict__, **overrides})


# --- I1: the server decides whether a turn runs ---------------------------


def test_server_running_is_running_even_when_no_local_turn_is_open() -> None:
    """Attaching mid-turn, a session switch, or a snapshot rebuild must not
    hide a turn the server says is running."""
    assert derive(facts(server_turn=ServerTurn.RUNNING, turn_open=False)) is Status.RUNNING


def test_server_running_is_running_while_a_local_turn_is_also_open() -> None:
    assert derive(facts(server_turn=ServerTurn.RUNNING, turn_open=True)) is Status.RUNNING


def test_local_turn_open_keeps_running_before_the_terminal_frame_arrives() -> None:
    """The final frame can be lost; showing Ready before the authoritative
    reading says idle is exactly the reported defect."""
    assert derive(facts(server_turn=ServerTurn.IDLE, turn_open=True)) is Status.RUNNING


def test_authoritative_idle_with_no_open_turn_is_ready() -> None:
    assert derive(facts(server_turn=ServerTurn.IDLE, turn_open=False)) is Status.READY


def test_unknown_server_turn_is_not_running() -> None:
    assert derive(facts(server_turn=ServerTurn.UNKNOWN, turn_open=False)) is Status.READY


# --- connection -----------------------------------------------------------


def test_connecting_precedes_every_other_fact() -> None:
    assert derive(facts(connection=Connection.CONNECTING, server_turn=ServerTurn.RUNNING)) is (
        Status.CONNECTING
    )


def test_disconnected_precedes_turn_and_interaction_facts() -> None:
    assert derive(
        facts(
            connection=Connection.DISCONNECTED,
            server_turn=ServerTurn.RUNNING,
            interaction=Interaction.PERMISSION,
        )
    ) is Status.DISCONNECTED


# --- priority order -------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"interrupt": Interrupt.REQUESTED}, Status.INTERRUPTING),
        ({"interaction": Interaction.PERMISSION}, Status.APPROVAL_REQUIRED),
        ({"interaction": Interaction.USER_INPUT}, Status.WAITING_USER),
        ({"compaction": True}, Status.COMPACTING),
        ({"server_turn": ServerTurn.RUNNING}, Status.RUNNING),
        ({"last_error": "boom"}, Status.ERROR),
        ({}, Status.READY),
    ],
)
def test_priority_order(overrides: dict, expected: Status) -> None:
    assert derive(facts(**overrides)) is expected


def test_interrupt_outranks_a_pending_approval() -> None:
    """The user just asked to stop: that request must be visible, not hidden
    behind a dialog they are no longer acting on."""
    assert derive(
        facts(interrupt=Interrupt.REQUESTED, interaction=Interaction.PERMISSION)
    ) is Status.INTERRUPTING


def test_error_surfaces_only_when_nothing_is_running() -> None:
    """A transient provider error frame must not claim the turn stopped: the
    server keeps streaming until its terminal frame."""
    assert derive(facts(last_error="boom", server_turn=ServerTurn.RUNNING)) is Status.RUNNING


# --- jobs must be visible (status bar and Tasks panel agree) --------------


def test_label_mentions_running_jobs_when_idle() -> None:
    label = status_label(Status.READY, facts(jobs_running=2))
    assert "Ready" in label
    assert "2" in label


def test_label_mentions_running_jobs_while_a_turn_runs() -> None:
    label = status_label(Status.RUNNING, facts(server_turn=ServerTurn.RUNNING, jobs_running=1))
    assert "Running" in label
    assert "1" in label


def test_label_omits_jobs_when_none_run() -> None:
    assert status_label(Status.READY, facts()) == "Ready"


@pytest.mark.parametrize(
    ("status", "word"),
    [
        (Status.CONNECTING, "Connecting"),
        (Status.DISCONNECTED, "Disconnected"),
        (Status.READY, "Ready"),
        (Status.RUNNING, "Running"),
        (Status.WAITING_USER, "Waiting"),
        (Status.APPROVAL_REQUIRED, "Approval"),
        (Status.COMPACTING, "Compacting"),
        (Status.INTERRUPTING, "Interrupting"),
        (Status.ERROR, "Error"),
    ],
)
def test_every_status_has_a_label(status: Status, word: str) -> None:
    assert word in status_label(status, facts())
