"""Tests for CoreStateStore — append-only persistence."""

import json
from pathlib import Path

import pytest
import yaml

from xbotv2.persistence.materializer import build_materialized_state
from xbotv2.persistence.store import CoreStateStore


class TestCoreStateStoreCreation:
    """State store creation and directory layout."""

    def test_create_initializes_directories(self, temp_data_dir):
        """Creating a store creates all required directories."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        assert store.events_path.exists()
        assert store.plugin_states_dir.exists()
        assert store.artifacts_dir.exists()

    def test_create_writes_initial_state(self, temp_data_dir):
        """Initial state.yaml is created."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        state = store.read_state()
        assert state["schema_version"] == 2
        assert state["session_id"] == "s1"
        assert state["turn_count"] == 0
        assert state["event_count"] == 0
        assert state["status"] == "active"

    def test_create_is_idempotent(self, temp_data_dir):
        """Creating twice doesn't error."""
        root = temp_data_dir / "sessions" / "test" / "state"
        CoreStateStore.create(root, session_id="s1", thread_id="t1", personality_id="default")
        CoreStateStore.create(root, session_id="s1", thread_id="t1", personality_id="default")


class TestEventAppending:
    """Append-only JSONL event log."""

    def test_append_event(self, temp_data_dir):
        """Appending an event writes to events.jsonl."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        event = store.append_event("turn_started", {"turn": 1})
        assert event["type"] == "turn_started"
        assert event["event_id"] == 1
        assert "ts" in event

    def test_append_multiple_events(self, temp_data_dir):
        """Events get sequential IDs."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        e1 = store.append_event("turn_started", {"turn": 1})
        e2 = store.append_event("turn_finished", {"turn": 1})
        assert e1["event_id"] == 1
        assert e2["event_id"] == 2

    def test_read_events(self, temp_data_dir):
        """Events can be read back."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.append_event("turn_started", {"turn": 1})
        store.append_event("turn_finished", {"turn": 1})
        events = store.read_events()
        assert len(events) == 2
        assert events[0]["type"] == "turn_started"
        assert events[1]["type"] == "turn_finished"

    def test_events_are_append_only_jsonl(self, temp_data_dir):
        """The events file is valid JSONL — one JSON object per line."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.append_event("test", {"key": "value"})
        store.append_event("test2", {"key2": "value2"})

        with open(store.events_path) as f:
            lines = f.readlines()

        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line.strip())
            assert "event_id" in obj
            assert "ts" in obj
            assert "type" in obj


class TestMaterialization:
    """state.yaml materialized view."""

    def test_materializer_pure_function_matches_state_shape(self):
        """The planned materializer module owns derived state fields."""
        state = build_materialized_state(
            schema_version=2,
            session_id="s1",
            thread_id="t1",
            personality_id="default",
            events=[
                {"type": "turn_started"},
                {"type": "mailbox_send"},
                {"type": "mailbox_send"},
                {"type": "mailbox_acknowledge"},
                {"type": "error"},
            ],
            message_count=3,
            plugin_states={"plugin_a": {"enabled": True}},
            artifacts_root="/tmp/artifacts",
        )

        assert state["turn_count"] == 1
        assert state["event_count"] == 5
        assert state["message_count"] == 3
        assert state["status"] == "error"
        assert state["mailbox_pending"] == 1
        assert state["pending_interactions"] == []
        assert state["plugin_states"] == {"plugin_a": {"enabled": True}}

    def test_materialize_reflects_events(self, temp_data_dir):
        """Materialized state reflects appended events."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.append_event("turn_started", {"turn": 1})
        store.append_event("turn_finished", {"turn": 1})

        state = store.materialize()
        assert state["turn_count"] == 1  # Only turn_started counts
        assert state["event_count"] == 2
        assert state["status"] == "active"

    def test_materialize_rebuildable_from_events(self, temp_data_dir):
        """Deleting state.yaml and re-materializing gives same result."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.append_event("turn_started", {"turn": 1})
        s1 = store.materialize()

        # Delete state.yaml
        store.state_path.unlink()

        s2 = store.materialize()
        assert s1["turn_count"] == s2["turn_count"]
        assert s1["event_count"] == s2["event_count"]

    def test_status_from_events(self, temp_data_dir):
        """Status is derived from the last relevant event."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        assert store.materialize()["status"] == "active"

        store.append_event("error", {"msg": "test"})
        assert store.materialize()["status"] == "error"

        store.append_event("session_closed", {})
        assert store.materialize()["status"] == "closed"

    def test_turn_started_reactivates_after_error_or_interrupt(self):
        """A new turn can move a session out of error/interrupted status."""
        state = build_materialized_state(
            schema_version=2,
            session_id="s1",
            thread_id="t1",
            personality_id="default",
            events=[
                {"type": "turn_started"},
                {"type": "error"},
                {"type": "turn_started"},
                {"type": "turn_finished"},
                {"type": "interrupted"},
                {"type": "turn_started"},
            ],
            message_count=0,
            plugin_states={},
            artifacts_root="/tmp/artifacts",
        )

        assert state["status"] == "active"

    def test_turn_finished_does_not_clear_same_turn_interrupt(self):
        """An ask-user interrupt remains visible after the turn finishes."""
        state = build_materialized_state(
            schema_version=2,
            session_id="s1",
            thread_id="t1",
            personality_id="default",
            events=[
                {"type": "turn_started"},
                {"type": "interrupted"},
                {"type": "turn_finished"},
            ],
            message_count=0,
            plugin_states={},
            artifacts_root="/tmp/artifacts",
        )

        assert state["status"] == "interrupted"

    def test_mailbox_pending_count(self, temp_data_dir):
        """Mailbox pending = sent - acknowledged."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        assert store.materialize()["mailbox_pending"] == 0

        store.append_event("mailbox_send", {})
        store.append_event("mailbox_send", {})
        assert store.materialize()["mailbox_pending"] == 2

        store.append_event("mailbox_acknowledge", {})
        assert store.materialize()["mailbox_pending"] == 1

    def test_pending_interactions_from_events(self):
        """Interaction requests remain pending until a matching response event."""
        state = build_materialized_state(
            schema_version=2,
            session_id="s1",
            thread_id="t1",
            personality_id="default",
            events=[
                {
                    "event_id": 1,
                    "type": "user_input_required",
                    "payload": {
                        "request_id": "user_input:c1",
                        "source": "ask_user",
                        "question": "Proceed?",
                    },
                },
                {
                    "event_id": 2,
                    "type": "permission_request",
                    "payload": {
                        "request_id": "permission:c2",
                        "source": "permission_system",
                    },
                },
                {
                    "event_id": 3,
                    "type": "user_input_response",
                    "payload": {"request_id": "user_input:c1", "answer": "yes"},
                },
            ],
            message_count=0,
            plugin_states={},
            artifacts_root="/tmp/artifacts",
        )

        assert [item["request_id"] for item in state["pending_interactions"]] == [
            "permission:c2"
        ]
        assert state["pending_interactions"][0]["type"] == "permission_request"

    def test_session_closed_clears_pending_interactions(self):
        """Closing a session clears unresolved user and permission requests."""
        state = build_materialized_state(
            schema_version=2,
            session_id="s1",
            thread_id="t1",
            personality_id="default",
            events=[
                {
                    "event_id": 1,
                    "type": "user_input_required",
                    "payload": {"request_id": "user_input:c1"},
                },
                {
                    "event_id": 2,
                    "type": "permission_request",
                    "payload": {"request_id": "permission:c2"},
                },
                {"event_id": 3, "type": "session_closed", "payload": {}},
            ],
            message_count=0,
            plugin_states={},
            artifacts_root="/tmp/artifacts",
        )

        assert state["status"] == "closed"
        assert state["pending_interactions"] == []


class TestPluginState:
    """Plugin state isolation."""

    def test_get_plugin_state_defaults_empty(self, temp_data_dir):
        """Unwritten plugin state returns {}."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        assert store.get_plugin_state("nonexistent") == {}

    def test_set_and_get_plugin_state(self, temp_data_dir):
        """Plugin state round-trips correctly."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.set_plugin_state("test_plugin", {"key": "value", "count": 42})
        assert store.get_plugin_state("test_plugin") == {"key": "value", "count": 42}

    def test_delete_plugin_state(self, temp_data_dir):
        """Plugin state can be deleted."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.set_plugin_state("test_plugin", {"key": "value"})
        store.delete_plugin_state("test_plugin")
        assert store.get_plugin_state("test_plugin") == {}

    def test_plugin_states_in_materialized_view(self, temp_data_dir):
        """Materialized state includes all plugin states."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.set_plugin_state("plugin_a", {"a": 1})
        store.set_plugin_state("plugin_b", {"b": 2})

        state = store.materialize()
        assert state["plugin_states"] == {"plugin_a": {"a": 1}, "plugin_b": {"b": 2}}

    def test_plugin_state_isolation(self, temp_data_dir):
        """Plugin states don't leak between plugins."""
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", personality_id="default"
        )
        store.set_plugin_state("plugin_a", {"data": "a"})
        store.set_plugin_state("plugin_b", {"data": "b"})

        assert store.get_plugin_state("plugin_a") == {"data": "a"}
        assert store.get_plugin_state("plugin_b") == {"data": "b"}
