"""Tests for ThreadPersistence message persistence."""

from XBotv2.persistence.store import ThreadPersistence
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths


def _session_paths(data_dir, session_id="s1"):
    return RuntimePaths.from_data_dir(data_dir).session(session_id)


class TestThreadPersistenceCreation:
    """State store creation and directory layout."""

    def test_create_initializes_directories(self, temp_data_dir):
        store = ThreadPersistence.create(
            _session_paths(temp_data_dir),
            thread_id="t1", workspace_root="/workspace", provider="default"
        )
        assert store.paths.state_dir.exists()
        assert not store.history.path.exists()
        assert not store.paths.artifacts_dir.exists()

    def test_threads_keep_independent_state(self, temp_data_dir):
        paths = _session_paths(temp_data_dir)
        first = ThreadPersistence.create(
            paths, thread_id="first", workspace_root="/workspace", provider="default"
        )
        second = ThreadPersistence.create(
            paths, thread_id="second", workspace_root="/workspace", provider="default"
        )

        first.history.append([Message(role="user", content="first thread")])
        assert second.history.load() == []
        assert first.paths.state_dir == paths.threads_dir / "first" / "state"
        assert second.paths.state_dir == paths.threads_dir / "second" / "state"
