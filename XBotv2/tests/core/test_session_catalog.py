"""Session catalog resilience: one unreadable session must not hide the rest."""

import os
import time

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


async def test_new_session_is_invisible_until_it_has_evidence(tmp_path):
    """An opened-but-unused session is not listed and has nothing to resume."""
    manager = _manager(tmp_path)
    active = await manager.active_threads()
    assert not active

    _write_new_session(tmp_path, "ghost")
    listed = await manager.list_sessions()
    assert [s.session_id for s in listed] == []

    # Evidence (a message record) makes it visible
    _write_evidence(tmp_path, "ghost")
    listed = await manager.list_sessions()
    assert [s.session_id for s in listed] == ["ghost"]


async def test_empty_session_gc_reclaims_abandoned_ghosts(tmp_path):
    manager = _manager(tmp_path)
    _write_new_session(tmp_path, "ghost")
    directory = tmp_path / "sessions" / "ghost"
    assert directory.exists()
    manager.empty_session_timeout = 60
    # Backdate the ghost so it is already past the grace period.
    old = time.time() - 3600
    os.utime(directory, (old, old))

    await manager._gc_empty_sessions(time.monotonic())
    assert not directory.exists()
    assert await manager.list_sessions() == ()


def _write_new_session(tmp_path, session_id):
    root = tmp_path / "sessions" / session_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.lock").touch()


def _write_evidence(tmp_path, session_id):
    paths = RuntimePaths.from_data_dir(tmp_path).session(session_id).thread("agent")
    paths.messages_file.parent.mkdir(parents=True, exist_ok=True)
    paths.messages_file.write_text("{}\n", encoding="utf-8")
