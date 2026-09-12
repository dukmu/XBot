"""Session catalog resilience: one unreadable session must not hide the rest."""

import pytest

from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.plugin import thread_persistence_factory
from XBotv2.persistence.store import ThreadPersistence
from XBotv2.session.contracts import SessionNotFound
from XBotv2.session.manager import ResourceEvents, SessionManager


class _Events(ResourceEvents):
    async def emit(self, *_args, **_kwargs) -> None:
        return None


def _manager(tmp_path) -> SessionManager:
    return SessionManager(
        RuntimePaths.from_data_dir(tmp_path),
        _Events(),
        thread_persistence_factory=thread_persistence_factory,
    )


def _session(tmp_path, session_id: str):
    persistence = ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp_path).session(session_id),
        thread_id="agent",
        workspace_root="/workspace",
        provider="default",
    )
    persistence.history.append([Message(role="user", content=session_id)])
    return persistence


def _corrupt(persistence) -> None:
    path = persistence.history.path
    path.write_text(
        path.read_text(encoding="utf-8") + '{"schema_version":\n',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_list_sessions_keeps_an_unreadable_session_visible(tmp_path, caplog):
    caplog.set_level("WARNING", logger="xbotv2.session")
    _session(tmp_path, "good")
    _corrupt(_session(tmp_path, "broken"))

    summaries = {
        summary.session_id: summary
        for summary in await _manager(tmp_path).list_sessions()
    }

    assert set(summaries) == {"good", "broken"}
    assert summaries["good"].unreadable is False
    assert summaries["broken"].unreadable is True
    assert summaries["broken"].thread_count == 1
    assert "session.catalog.unreadable" in caplog.text


@pytest.mark.asyncio
async def test_session_summary_still_reports_an_unreadable_session(tmp_path):
    _corrupt(_session(tmp_path, "broken"))

    with pytest.raises(ValueError, match="Invalid messages.jsonl"):
        await _manager(tmp_path).session_summary("broken")


@pytest.mark.asyncio
async def test_delete_session_does_not_read_the_trajectory(tmp_path):
    persistence = _session(tmp_path, "broken")
    _corrupt(persistence)
    manager = _manager(tmp_path)

    await manager.delete_session("broken")

    assert not persistence.paths.session.root.exists()


@pytest.mark.asyncio
async def test_mutating_a_missing_session_still_fails_clearly(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(SessionNotFound):
        await manager.delete_session("absent")
