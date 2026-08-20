"""Tests for the capability event contract registry (``ctx.server_events``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from XBotv2.compact.events import (
    CompactionCompletedData,
    CompactionFailedData,
    CompactionStartedData,
)
from XBotv2.protocol.http_util import _format_sse
from XBotv2.protocol.models import (
    KNOWN_SERVER_EVENT_TYPES,
    ServerEvent,
    TaskUpdatedData,
)
from XBotv2.protocol.sse import encode_server_event
from XBotv2.server.events import ServerEvents
from XBotv2.session.events import AgentConfiguredData, ClientMessageData, HistoryUpdatedData

_REGISTERED_DTOS = {
    "agent_configured": AgentConfiguredData,
    "client_message": ClientMessageData,
    "compaction_completed": CompactionCompletedData,
    "compaction_failed": CompactionFailedData,
    "compaction_started": CompactionStartedData,
    "history_updated": HistoryUpdatedData,
    "task_updated": TaskUpdatedData,
}


def _load_fixture() -> list[dict[str, object]]:
    path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "sse"
        / "server_registered_event_contracts.jsonl"
    )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_registered_events_have_sse_contract_fixtures() -> None:
    registry = ServerEvents()
    for event_type, dto in _REGISTERED_DTOS.items():
        registry.register(event_type, dto)
    contracts = _load_fixture()
    assert [event["type"] for event in contracts] == list(registry.types())
    for expected in contracts:
        event_type = str(expected["type"])
        payload = registry.validate(event_type, dict(expected["data"]))
        event = ServerEvent.model_validate({**expected, "data": payload})
        frame = _format_sse(
            event={"type": event.type, "data": event.data},
            seq=event.sequence,
            session_id=event.session_id,
            thread_id=event.thread_id,
            request_id=event.request_id,
        )
        assert f"event: {event_type}\n" in frame.decode("utf-8")


def test_registration_is_an_effect_and_disposes() -> None:
    registry = ServerEvents()
    disposer = registry.register("hello_event", ClientMessageData)
    assert "hello_event" in registry.types()
    disposer()
    assert "hello_event" not in registry.types()


def test_duplicate_registration_is_a_composition_error() -> None:
    registry = ServerEvents()
    registry.register("dup_event", ClientMessageData)
    with pytest.raises(RuntimeError, match="server_events collision: dup_event"):
        registry.register("dup_event", HistoryUpdatedData)


def test_core_event_types_are_protocol_owned() -> None:
    registry = ServerEvents()
    with pytest.raises(RuntimeError, match="protocol core event"):
        registry.register("turn_finished", ClientMessageData)
    assert set(KNOWN_SERVER_EVENT_TYPES).isdisjoint(registry.types())


def test_validate_applies_registered_dto_and_passes_unknown_through() -> None:
    registry = ServerEvents()
    registry.register("agent_configured", AgentConfiguredData)
    normalized = registry.validate(
        "agent_configured", {"agent_name": "default", "provider": "minimax"}
    )
    assert normalized == {"agent_name": "default", "provider": "minimax"}
    assert registry.validate("unknown_event", {"a": 1}) == {"a": 1}


def test_unregistered_events_frame_without_normalization() -> None:
    registry = ServerEvents()
    payload = registry.validate("mystery_event", {"raw": True})
    encoded = encode_server_event(
        ServerEvent.model_validate(
            {"type": "mystery_event", "data": payload, "sequence": 1}
        )
    )
    assert b"event: mystery_event" in encoded