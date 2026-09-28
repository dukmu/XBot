"""Tests for ThreadPersistence message persistence."""

from XBotv2.persistence.store import ThreadPersistence
from XBotv2.core.domain import MessageId
from XBotv2.core.messages import HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.paths import RuntimePaths
from xcore import Context


def _session_paths(data_dir, session_id="s1"):
    return RuntimePaths.from_data_dir(data_dir).session(session_id)


class TestThreadPersistenceCreation:
    """State store creation and directory layout."""

    def test_create_initializes_directories(self, temp_data_dir):
        paths = _session_paths(temp_data_dir).thread("t1")
        context = Context(data_dir=paths.plugin_state_dir)
        store = ThreadPersistence.create(
            paths,
            thread_id="t1",
            state=context.state,
        )
        assert store.paths.state_dir.exists()
        assert not store.history.path.exists()
        assert not store.paths.artifacts_dir.exists()

    def test_threads_keep_independent_state(self, temp_data_dir):
        paths = _session_paths(temp_data_dir)
        first = ThreadPersistence.open(
            paths, thread_id="first",
        )
        second = ThreadPersistence.open(
            paths, thread_id="second",
        )

        first.history.append([HumanInputMessage(
            id=MessageId("input-1"),
            parts=(TextPart(text="first thread"),),
        )])
        assert second.history.load() == []
        assert first.paths.state_dir == paths.threads_dir / "first" / "state"
        assert second.paths.state_dir == paths.threads_dir / "second" / "state"
