"""Session catalog resilience: one unreadable session must not hide the rest."""

import asyncio
import os
import time

import pytest

from XBotv2.core.domain import InputId, MessageId
from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
)
from XBotv2.core.metadata import ThreadMetadata
from XBotv2.core.messages import HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.plugin import thread_persistence_factory
from XBotv2.persistence.store import ThreadPersistence
from XBotv2.session.contracts import SESSION_RESOURCE_REMOVED, SessionNotFound
from XBotv2.session.manager import ResourceEvents, SessionManager


class _Events(ResourceEvents):
    def __init__(self):
        self.events = []

    async def emit(self, *_args, **_kwargs) -> None:
        self.events.append((_args, _kwargs))


def _manager(tmp_path) -> SessionManager:
    return SessionManager(
        RuntimePaths.from_data_dir(tmp_path),
        _Events(),
        application_factory=_unused_application_factory,
        thread_persistence_factory=thread_persistence_factory,
    )


async def _unused_application_factory(_options):
    raise AssertionError("session catalog tests must not open applications")


def test_session_manager_requires_application_factory(tmp_path):
    with pytest.raises(TypeError, match="application_factory"):
        SessionManager(RuntimePaths.from_data_dir(tmp_path), _Events())


def _session(tmp_path, session_id: str):
    persistence = ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp_path).session(session_id),
        thread_id="agent",
    )
    persistence.history.append([HumanInputMessage(
        id=MessageId(f"input-{session_id}"),
        input_id=InputId(f"input-{session_id}"),
        parts=(TextPart(text=session_id),),
    )])
    persistence.metadata.save(ThreadMetadata(
        runtime_selection=ResolvedRuntimeSelection(
            agent_name="default",
            prompt="",
            limits=AgentExecutionLimits(),
            enabled_tools=(),
            model=ResolvedModelSelection(
                route=ModelRoute(provider="default", model="test"),
                generation=GenerationSettings(
                    mode=StandardGenerationMode(), max_output_tokens=128,
                ),
                context_window=4096,
            ),
        ),
        workspace_root="/workspace",
    ))
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
    _write_new_session(tmp_path, "ghost")
    # Opening a runtime is transient process state, not evidence that the user
    # has created a durable session.
    manager._sessions[("ghost", "agent")] = object()
    listed = await manager.list_sessions()
    assert listed == ()
    manager._sessions.clear()

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


@pytest.mark.asyncio
async def test_shutdown_removes_unwritten_session_and_notifies_catalog(tmp_path):
    events = _Events()
    manager = SessionManager(
        RuntimePaths.from_data_dir(tmp_path),
        events,
        application_factory=_unused_application_factory,
        thread_persistence_factory=thread_persistence_factory,
    )

    class Runtime:
        session_id = "unused"
        thread_id = "agent"

        async def close(self, _reason):
            return None

    root = manager.paths.session("unused").root
    (root / "threads" / "agent" / "state").mkdir(parents=True)
    manager._sessions[("unused", "agent")] = Runtime()

    await manager.close_all()

    assert not root.exists()
    assert await manager.list_sessions() == ()
    assert any(
        args[0] == SESSION_RESOURCE_REMOVED
        for args, _kwargs in events.events
    )


@pytest.mark.asyncio
async def test_close_all_finishes_every_runtime_and_stops_reaper_after_close_error(
    tmp_path,
):
    manager = _manager(tmp_path)
    close_order = []

    class Runtime:
        def __init__(self, session_id):
            self.session_id = session_id
            self.thread_id = "agent"

        async def close(self, _reason):
            close_order.append(self.session_id)
            if self.session_id == "broken":
                raise RuntimeError("runtime close failed")

    for session_id in ("broken", "healthy"):
        _write_new_session(tmp_path, session_id)
        manager._sessions[(session_id, "agent")] = Runtime(session_id)
    reaper = asyncio.create_task(asyncio.Event().wait())
    manager._reaper = reaper

    with pytest.raises(ExceptionGroup) as failure:
        await manager.close_all()

    assert [str(error) for error in failure.value.exceptions] == [
        "runtime close failed",
    ]
    assert close_order == ["broken", "healthy"]
    assert reaper.cancelled()
    assert not manager.paths.session("broken").root.exists()
    assert not manager.paths.session("healthy").root.exists()


@pytest.mark.asyncio
async def test_close_session_finishes_every_thread_after_one_close_error(tmp_path):
    manager = _manager(tmp_path)
    close_order = []

    class Runtime:
        session_id = "mixed"

        def __init__(self, thread_id):
            self.thread_id = thread_id

        async def close(self, _reason):
            close_order.append(self.thread_id)
            if self.thread_id == "broken":
                raise RuntimeError("thread close failed")

    for thread_id in ("broken", "healthy"):
        manager._sessions[("mixed", thread_id)] = Runtime(thread_id)

    with pytest.raises(ExceptionGroup) as failure:
        await manager.close_session("mixed")

    assert [str(error) for error in failure.value.exceptions] == [
        "thread close failed",
    ]
    assert close_order == ["broken", "healthy"]
    assert await manager.active_threads() == {}


def _write_new_session(tmp_path, session_id):
    root = tmp_path / "sessions" / session_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.lock").touch()


def _write_evidence(tmp_path, session_id):
    paths = RuntimePaths.from_data_dir(tmp_path).session(session_id).thread("agent")
    paths.messages_file.parent.mkdir(parents=True, exist_ok=True)
    paths.messages_file.write_text("{}\n", encoding="utf-8")
