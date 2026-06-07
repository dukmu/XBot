"""Tests for CoreStateStore — plugin state and message persistence."""

from pathlib import Path

import pytest

from xbotv2.persistence.store import CoreStateStore
from xbotv2.llm.messages import Message


class TestCoreStateStoreCreation:
    """State store creation and directory layout."""

    def test_create_initializes_directories(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default"
        )
        assert store.messages_path.exists()
        assert store.plugin_states_dir.exists()
        assert store.artifacts_dir.exists()

    def test_create_is_idempotent(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        CoreStateStore.create(root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default")
        CoreStateStore.create(root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default")


class TestPluginState:
    """Plugin state isolation."""

    def test_get_plugin_state_defaults_empty(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default"
        )
        assert store.get_plugin_state("nonexistent") == {}

    def test_set_and_get_plugin_state(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default"
        )
        store.set_plugin_state("test_plugin", {"key": "value", "count": 42})
        assert store.get_plugin_state("test_plugin") == {"key": "value", "count": 42}

    def test_delete_plugin_state(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default"
        )
        store.set_plugin_state("test_plugin", {"key": "value"})
        store.delete_plugin_state("test_plugin")
        assert store.get_plugin_state("test_plugin") == {}

    def test_plugin_state_isolation(self, temp_data_dir):
        root = temp_data_dir / "sessions" / "test" / "state"
        store = CoreStateStore.create(
            root, session_id="s1", thread_id="t1", workspace_root="/workspace", provider="default"
        )
        store.set_plugin_state("plugin_a", {"enabled": True})
        store.set_plugin_state("plugin_b", {"enabled": False})
        assert store.get_plugin_state("plugin_a") == {"enabled": True}
        assert store.get_plugin_state("plugin_b") == {"enabled": False}
