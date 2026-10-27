"""Catalog resilience through the actual process/session owner."""
import pytest
import pytest_asyncio

from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.core.api.messages import HumanInputMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import SESSIONS, OpenSession, SessionNotFound


async def _session(tmp_path, manager, session_id):
    scope = await manager.open(OpenSession(session_id, "main", str(tmp_path), None, "new", False, model_override=MockLLM()))
    await scope.require(LOOP_STATE).history.append(HumanInputMessage(id=f"input-{session_id}", parts=(TextPart(text=session_id),)))
    await manager.close_session(session_id)
    return RuntimePaths.from_data_dir(tmp_path).session(session_id)


def _corrupt(paths):
    with paths.thread("main").rollout_file.open("ab") as stream:
        stream.write(b"{complete bad json}\n")

@pytest.mark.asyncio
async def test_list_sessions_keeps_an_unreadable_session_visible(tmp_path, caplog, manager):
    caplog.set_level("WARNING", logger="xbotv2.session")
    await _session(tmp_path, manager, "good")
    _corrupt(await _session(tmp_path, manager, "broken"))

    summaries = {
        summary.session_id: summary
        for summary in await manager.list_sessions()
    }

    assert set(summaries) == {"good", "broken"}
    assert summaries["good"].unreadable is False
    assert summaries["broken"].unreadable is True
    assert summaries["broken"].thread_count == 1
    assert "session.catalog.unreadable" in caplog.text

@pytest.mark.asyncio
async def test_session_summary_still_reports_an_unreadable_session(tmp_path, manager):
    _corrupt(await _session(tmp_path, manager, "broken"))

    with pytest.raises(ValueError, match="Invalid rollout"):
        await manager.session_summary("broken")

@pytest.mark.asyncio
async def test_delete_session_does_not_read_the_trajectory(tmp_path, manager):
    paths = await _session(tmp_path, manager, "broken")
    _corrupt(paths)

    await manager.delete_session("broken")

    assert not paths.root.exists()

@pytest.mark.asyncio
async def test_mutating_a_missing_session_still_fails_clearly(tmp_path, manager):

    with pytest.raises(SessionNotFound):
        await manager.delete_session("absent")

@pytest_asyncio.fixture
async def manager(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path))
    try:
        yield root.require(SESSIONS)
    finally:
        await root.close()


async def test_unused_open_is_invisible_and_shutdown_removes_it(tmp_path, manager):
    scope = await manager.open(OpenSession("unused", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
    assert not RuntimePaths.from_data_dir(tmp_path).session("unused").root.exists()
    assert await manager.list_sessions() == ()
    await manager.close_session("unused")
    assert not scope.is_active
    assert not RuntimePaths.from_data_dir(tmp_path).session("unused").root.exists()
    assert await manager.list_sessions() == ()
