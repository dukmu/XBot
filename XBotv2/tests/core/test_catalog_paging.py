"""Bounded workspace pages and local writes over large durable catalogs."""
import pytest
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.main import open_runtime
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.session.api.contracts import SessionSummary
from XBotv2.workspaces.impl.service import WorkspaceRegistry


class Sessions:
    def __init__(self, path):
        self.path = path
    def session_exists(self, session_id):
        return True
    async def catalog_summary(self, session_id):
        return SessionSummary(session_id=session_id, status="inactive", workspace_root=self.path)


class Events:
    def __init__(self):
        self.changes = []
    async def emit(self, key, change):
        self.changes.append(change)


@pytest.mark.asyncio
async def test_large_workspace_and_member_pages(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    try:
        path = str(tmp_path)
        events = Events()
        registry = WorkspaceRegistry(root.require(PERSISTENCE).state(), Sessions(path), events)
        view, _ = await registry.create(path)
        def seed(sql):
            for i in range(550):
                sql.execute("INSERT INTO workspaces_items VALUES(?,?,?,?,?,?)", (f"ws_{i:04}", f"/x/{i}", f"x{i}", "now", "now", i + 10))
                sql.execute("INSERT INTO workspaces_members VALUES(?,?,?)", (view.workspace_id, f"s{i:04}", i))
                sql.execute("INSERT INTO workspaces_archived VALUES(?,?)", (f"a{i:04}", i))
        await registry._storage.transaction(seed, write=True)
        first = await registry.list(limit=100)
        second = await registry.list(limit=100, cursor=first.next_cursor)
        assert (await registry.list(limit=100, cursor=second.previous_cursor)).items == first.items
        page = await registry.list_sessions(view.workspace_id, limit=100)
        next_page = await registry.list_sessions(view.workspace_id, limit=100, cursor=page.next_cursor)
        assert next_page.sessions[0].session_id == "s0100"
        assert (await registry.list_sessions(view.workspace_id, limit=100, cursor=next_page.previous_cursor)).sessions == page.sessions
        last = first
        while last.next_cursor:
            last = await registry.list(limit=100, cursor=last.next_cursor)
        assert any(w.workspace_id == "ws_0549" for w in last.items)
        member_last = page
        while member_last.next_cursor:
            member_last = await registry.list_sessions(view.workspace_id, limit=100, cursor=member_last.next_cursor)
        assert member_last.sessions[-1].session_id == "s0549"
        await registry.rename(view.workspace_id, "renamed")
        await registry.set_archived("s0001", True)
        assert next(w for w in (await registry.list()).items if w.workspace_id == view.workspace_id).session_count == 549
        assert all(s.session_id != "s0001" for s in (await registry.list_sessions(view.workspace_id)).sessions)
        archived = await registry.list_archived(limit=100)
        assert archived.next_cursor is not None
        archived_second = await registry.list_archived(limit=100, cursor=archived.next_cursor)
        assert archived_second.sessions[0].session_id == "a0100"
        assert (await registry.list_archived(limit=100, cursor=archived_second.previous_cursor)).sessions == archived.sessions
        archived_last = archived_second
        while archived_last.next_cursor:
            archived_last = await registry.list_archived(limit=100, cursor=archived_last.next_cursor)
        assert any(s.session_id == "a0549" for s in archived_last.sessions)
        await registry.insert_session_before(view.workspace_id, "s0549", "s0000")
        assert (await registry.list_sessions(view.workspace_id, limit=1)).sessions[0].session_id == "s0549"
        await registry.insert_before("ws_0549", view.workspace_id)
        ordered = (await registry.list()).items
        assert next(i for i, w in enumerate(ordered) if w.workspace_id == "ws_0549") + 1 == next(i for i, w in enumerate(ordered) if w.workspace_id == view.workspace_id)
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_deleted_last_page_can_return_to_previous(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    try:
        registry = WorkspaceRegistry(root.require(PERSISTENCE).state(), Sessions(str(tmp_path)), Events())
        view, _ = await registry.create(tmp_path)
        for identity in ("a", "b", "c"):
            await registry.attach_session(identity, tmp_path)
        first = await registry.list_sessions(view.workspace_id, limit=2)
        assert first.next_cursor
        await registry.detach_session("a")
        empty = await registry.list_sessions(view.workspace_id, limit=2, cursor=first.next_cursor)
        assert not empty.sessions and empty.previous_cursor
        back = await registry.list_sessions(view.workspace_id, limit=2, cursor=empty.previous_cursor)
        assert [s.session_id for s in back.sessions] == ["c", "b"]
    finally:
        await root.close()
