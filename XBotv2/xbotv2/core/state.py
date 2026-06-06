"""Minimal core state types — no DAG/plan/skills concepts.

Core state tracks only what the engine needs: session metadata, event count,
mailbox pending. Plugin state is stored as opaque blobs that core never interprets.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionInfo:
    """Core session metadata.

    Intentionally minimal. No plan, DAG, task mode, skills, or compaction
    fields — those belong to plugins.
    """

    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"  # active | error | interrupted | closed
    mailbox_pending: int = 0


# Canonical status values
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ERROR = "error"
SESSION_STATUS_INTERRUPTED = "interrupted"
SESSION_STATUS_CLOSED = "closed"

VALID_SESSION_STATUSES = frozenset({
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ERROR,
    SESSION_STATUS_INTERRUPTED,
    SESSION_STATUS_CLOSED,
})
