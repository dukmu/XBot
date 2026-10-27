"""TUI status derived from facts, never assigned.

The reported defect was "the agent is running but the status bar says Ready".
The root cause was that the client held its own belief about that fact and
wrote it directly into a string from nineteen places. Here the belief is
replaced by explicit facts, one of which (``server_turn``) is the server's own
answer, and the displayed status is a pure function of those facts.

Nothing in this module imports Textual or performs IO, so the whole status
machine is testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Connection(Enum):
    """Whether the client currently holds a usable transport."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class ServerTurn(Enum):
    """The server's own answer for the attached thread.

    ``UNKNOWN`` means the client has not received an authoritative reading yet
    (no snapshot field, no watchdog answer).
    """

    UNKNOWN = "unknown"
    IDLE = "idle"
    RUNNING = "running"


class Interaction(Enum):
    """An unanswered prompt that blocks the turn."""

    NONE = "none"
    PERMISSION = "permission"
    USER_INPUT = "user_input"


class Interrupt(Enum):
    NONE = "none"
    REQUESTED = "requested"


class Status(Enum):
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    APPROVAL_REQUIRED = "approval_required"
    COMPACTING = "compacting"
    INTERRUPTING = "interrupting"
    ERROR = "error"


@dataclass(frozen=True)
class StatusFacts:
    """Everything the status depends on.

    ``server_turn`` is authoritative. ``turn_open`` only covers the window
    between the client seeing a turn start and the server's terminal frame
    arriving; it must be cleared by an authoritative ``idle`` reading, never by
    an error frame or a snapshot adoption that carries no answer.
    """

    connection: Connection = Connection.CONNECTING
    server_turn: ServerTurn = ServerTurn.UNKNOWN
    turn_open: bool = False
    interaction: Interaction = Interaction.NONE
    compaction: bool = False
    interrupt: Interrupt = Interrupt.NONE
    jobs_running: int = 0
    last_error: str | None = None


_LABELS: dict[Status, str] = {
    Status.CONNECTING: "Connecting",
    Status.DISCONNECTED: "Disconnected",
    Status.READY: "Ready",
    Status.RUNNING: "Running",
    Status.WAITING_USER: "Waiting for user",
    Status.APPROVAL_REQUIRED: "Approval required",
    Status.COMPACTING: "Compacting",
    Status.INTERRUPTING: "Interrupting…",
    Status.ERROR: "Error",
}


def derive(facts: StatusFacts) -> Status:
    """The one place that decides what the status is."""
    if facts.connection is Connection.CONNECTING:
        return Status.CONNECTING
    if facts.connection is Connection.DISCONNECTED:
        return Status.DISCONNECTED
    if facts.interrupt is Interrupt.REQUESTED:
        return Status.INTERRUPTING
    if facts.interaction is Interaction.PERMISSION:
        return Status.APPROVAL_REQUIRED
    if facts.interaction is Interaction.USER_INPUT:
        return Status.WAITING_USER
    if facts.compaction:
        return Status.COMPACTING
    # The server's answer wins; a local open turn only covers the gap until an
    # authoritative reading says otherwise.
    if facts.server_turn is ServerTurn.RUNNING or facts.turn_open:
        return Status.RUNNING
    if facts.last_error is not None:
        return Status.ERROR
    return Status.READY


def status_label(status: Status, facts: StatusFacts) -> str:
    """Human-readable status, including background work.

    Background jobs are part of "is it busy", so they are surfaced here rather
    than only in a separate Tasks panel that can contradict this line.
    """
    label = _LABELS[status]
    if facts.jobs_running <= 0:
        return label
    noun = "task" if facts.jobs_running == 1 else "tasks"
    return f"{label} · {facts.jobs_running} {noun} running"


__all__ = [
    "Connection",
    "Interaction",
    "Interrupt",
    "ServerTurn",
    "Status",
    "StatusFacts",
    "derive",
    "status_label",
]
