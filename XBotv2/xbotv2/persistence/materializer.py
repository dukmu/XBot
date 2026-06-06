"""Materialized view builder for CoreStateStore."""

from __future__ import annotations

from typing import Any


def build_materialized_state(
    *,
    schema_version: int,
    session_id: str,
    thread_id: str,
    workspace_root: str,
    provider: str,
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

    # Determine status from ordered events. A new turn re-activates sessions
    # after prior error/interrupted states; turn_finished does not clear an
    # interruption that happened during the same turn.
    status = "active"
    for e in events:
        t = e.get("type")
        if t == "session_closed":
            status = "closed"
        elif t == "turn_started":
            status = "active"
        elif t == "error":
            status = "error"
        elif t in {"interrupted", "turn_cancelled"}:
            status = "interrupted"

    # Mailbox pending
    sent = sum(1 for e in events if e.get("type") == "mailbox_send")
    acked = sum(1 for e in events if e.get("type") == "mailbox_acknowledge")
    mailbox_pending = max(0, sent - acked)
    pending_interactions = _pending_interactions(events)
    workspace = _workspace_state(events)
    provider_override = _provider_override(events)
    permission_overrides = _overrides_state(events, "permission")
    sandbox_overrides = _overrides_state(events, "sandbox")
    effective_provider = provider_override or provider

    return {
        "schema_version": schema_version,
        "session_id": session_id,
        "thread_id": thread_id,
        "workspace_root": workspace_root,
        "provider": effective_provider,
        "turn_count": turn_count,
        "event_count": event_count,
        "message_count": message_count,
        "status": status,
        "mailbox_pending": mailbox_pending,
        "pending_interactions": pending_interactions,
        "permission_overrides": permission_overrides,
        "sandbox_overrides": sandbox_overrides,
        "workspace": workspace,
        "plugin_states": plugin_states,
        "artifacts_root": artifacts_root,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _pending_interactions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        request_id = payload.get("request_id")

        if event_type in {"user_input_required", "interrupted", "permission_request"}:
            if not request_id:
                request_id = f"{event_type}:{event.get('event_id', '')}"
            pending_type = "user_input_required" if event_type == "interrupted" else event_type
            pending[str(request_id)] = {
                "request_id": str(request_id),
                "type": pending_type,
                "source": payload.get("source", ""),
                "payload": payload,
                "event_id": event.get("event_id"),
                "ts": event.get("ts", ""),
            }
            continue

        if event_type == "session_closed":
            pending.clear()
            continue

        if event_type in {
            "user_input_response",
            "user_input_cancelled",
            "permission_response",
            "permission_cancelled",
            "permission_denied",
        } and request_id:
            pending.pop(str(request_id), None)

    return list(pending.values())


def _workspace_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for event in events:
        if event.get("type") not in {"workspace_attached"}:
            continue
        payload = event.get("payload") or {}
        latest = {
            "root": payload.get("workspace_root", ""),
            "lifecycle": payload.get("lifecycle", ""),
            "status": payload.get("status", ""),
            "event_id": event.get("event_id"),
            "updated_at": event.get("ts", ""),
        }
    return latest


def _provider_override(events: list[dict[str, Any]]) -> str:
    provider = ""
    for event in events:
        if event.get("type") != "provider_switched":
            continue
        payload = event.get("payload") or {}
        provider = str(payload.get("provider") or "")
    return provider


def _overrides_state(events: list[dict[str, Any]], name: str) -> dict[str, str]:
    overrides: dict[str, str] = {}
    set_type = f"{name}_override_set"
    reset_type = f"{name}_overrides_reset"
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if event_type == set_type:
            key = str(payload.get("key") or "")
            value = str(payload.get("value") or "")
            if key:
                overrides[key] = value
            continue
        if event_type == reset_type:
            key = str(payload.get("key") or "")
            if key:
                overrides.pop(key, None)
            else:
                overrides.clear()
    return overrides
