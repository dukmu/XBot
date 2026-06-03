"""Materialized view builder for CoreStateStore."""

from __future__ import annotations

from typing import Any


def build_materialized_state(
    *,
    schema_version: int,
    session_id: str,
    thread_id: str,
    personality_id: str,
    events: list[dict[str, Any]],
    message_count: int,
    plugin_states: dict[str, Any],
    artifacts_root: str,
) -> dict[str, Any]:
    """Pure function: build a materialized state dict from raw inputs.

    This is separated from CoreStateStore so it can be tested independently.
    """
    from datetime import datetime, timezone

    turn_count = sum(1 for e in events if e.get("type") == "turn_started")
    event_count = len(events)

    # Determine status from the last relevant event
    status = "active"
    for e in reversed(events):
        t = e.get("type")
        if t == "session_closed":
            status = "closed"
            break
        if t == "error":
            status = "error"
            break
        if t == "interrupted":
            status = "interrupted"
            break

    # Mailbox pending
    sent = sum(1 for e in events if e.get("type") == "mailbox_send")
    acked = sum(1 for e in events if e.get("type") == "mailbox_acknowledge")
    mailbox_pending = max(0, sent - acked)

    return {
        "schema_version": schema_version,
        "session_id": session_id,
        "thread_id": thread_id,
        "personality_id": personality_id,
        "turn_count": turn_count,
        "event_count": event_count,
        "message_count": message_count,
        "status": status,
        "mailbox_pending": mailbox_pending,
        "plugin_states": plugin_states,
        "artifacts_root": artifacts_root,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
