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
        for summary in (await manager.list_sessions()).items
    }

    assert set(summaries) == {"good", "broken"}
    assert summaries["good"].unreadable is False
    assert summaries["broken"].unreadable is True
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
    assert (await manager.list_sessions()).items == ()
    await manager.close_session("unused")
    assert not scope.is_active
    assert not RuntimePaths.from_data_dir(tmp_path).session("unused").root.exists()
    assert (await manager.list_sessions()).items == ()


@pytest.mark.asyncio
async def test_session_pages_move_both_directions_and_filter(tmp_path, manager):
    for session_id in ("a", "b", "c"):
        await _session(tmp_path, manager, session_id)
    first = await manager.list_sessions(limit=1, workspace_root=str(tmp_path))
    assert [item.session_id for item in first.items] == ["a"]
    second = await manager.list_sessions(limit=1, cursor=first.next_cursor, workspace_root=str(tmp_path))
    assert [item.session_id for item in second.items] == ["b"]
    previous = await manager.list_sessions(limit=1, cursor=second.previous_cursor, workspace_root=str(tmp_path))
    assert previous.items == first.items
    last = await manager.list_sessions(limit=1, cursor=second.next_cursor, workspace_root=str(tmp_path))
    assert [item.session_id for item in last.items] == ["c"]
    assert last.next_cursor is None
    assert not (await manager.list_sessions(workspace_root="/missing")).items


@pytest.mark.asyncio
async def test_workspace_membership_recovers_at_startup(tmp_path):
    from XBotv2.workspaces.api.contracts import WORKSPACES
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    try:
        manager = root.require(SESSIONS)
        await root.require(WORKSPACES).create(tmp_path)
        await _session(tmp_path, manager, "recover")
        registry = root.require(WORKSPACES)
        workspace = next(item for item in (await registry.list()).items if item.path == str(tmp_path))
        await registry._storage.transaction(lambda sql: sql.execute("DELETE FROM workspaces_members WHERE session_id=?", ("recover",)), write=True)
    finally:
        await root.close()
    root = await open_runtime(paths=paths)
    try:
        page = await root.require(WORKSPACES).list_sessions(workspace.workspace_id)
        assert [session.session_id for session in page.sessions] == ["recover"]
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_directory_catalog_pages_past_500(tmp_path, manager, monkeypatch):
    from XBotv2.session.api.contracts import SessionSummary
    from XBotv2.session.impl import manager as module
    directory = manager.paths.sessions_dir
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(550):
        (directory / f"s{index:04}").mkdir()
    monkeypatch.setattr(module, "session_has_evidence", lambda paths, session_id: True)
    async def summary(_manager, session_id, _active):
        return SessionSummary(session_id=session_id, status="inactive")
    monkeypatch.setattr(module, "_build_session_summary", summary)
    page = await manager.list_sessions()
    while page.next_cursor:
        page = await manager.list_sessions(cursor=page.next_cursor)
    assert page.items[-1].session_id == "s0549"
    assert len(page.items) == 50
    assert (await manager.list_sessions(cursor=page.previous_cursor)).items[-1].session_id == "s0499"
