"""Process-level durable Workspace registry."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import uuid

from XBotv2.workspaces.impl.storage import WorkspaceStorage
from XBotv2.workspaces.api.contracts import (
    WorkspaceView, WorkspaceListing, WorkspaceSessionPage, WorkspaceNotFound,
    WorkspaceSessionNotFound, WorkspaceResourceChanged, WorkspaceResourceRemoved,
    WorkspaceOrderChanged, WorkspaceSessionOrderChanged, ArchivedSessionsChanged,
    WORKSPACE_RESOURCE_CHANGED, WORKSPACE_RESOURCE_REMOVED, WORKSPACE_ORDER_CHANGED,
    WORKSPACE_SESSION_ORDER_CHANGED, ARCHIVED_SESSIONS_CHANGED,
)
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG


def _now():
    return datetime.now(timezone.utc).isoformat()


def _limit(limit):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


class WorkspaceRegistry:
    def __init__(self, state, sessions, events, runtime_log=DEFAULT_RUNTIME_LOG):
        self._storage = WorkspaceStorage(state)
        self._sessions = sessions
        self._events = events
        self._log = runtime_log.bind("workspaces")
        self._lock = asyncio.Lock()

    async def list(self, *, limit=100, cursor=None):
        _limit(limit)
        rows, next_cursor, previous_cursor = await self._storage.page("workspace", limit, cursor)
        return WorkspaceListing(
            items=tuple(
                WorkspaceView(
                    workspace_id=r[1], path=r[2], title=r[3],
                    created_at=r[4], updated_at=r[5], session_count=r[6],
                )
                for r in rows
            ),
            next_cursor=next_cursor, previous_cursor=previous_cursor,
        )

    async def _session_page(self, kind, limit, cursor, workspace_id=None):
        _limit(limit)
        rows, next_cursor, previous_cursor = await self._storage.page(kind, limit, cursor, workspace_id)
        summaries = []
        archived = []
        workspace_path = (await self._workspace(workspace_id)).path if kind == "member" else None
        for _, session_id in rows:
            try:
                summary = await self._sessions.catalog_summary(session_id)
            except (FileNotFoundError, LookupError):
                continue
            if kind == "member" and not summary.unreadable and summary.workspace_root != workspace_path:
                continue
            summaries.append(summary)
        archived = [s.session_id for s in summaries] if kind == "archived" else []
        return WorkspaceSessionPage(
            sessions=tuple(summaries), next_cursor=next_cursor,
            previous_cursor=previous_cursor, archived_session_ids=tuple(archived),
        )

    async def list_sessions(self, workspace_id, *, limit=100, cursor=None):
        return await self._session_page("member", limit, cursor, workspace_id)

    async def list_archived(self, *, limit=100, cursor=None):
        return await self._session_page("archived", limit, cursor)

    async def _workspace(self, workspace_id):
        view = await self._storage.transaction(lambda sql: self._storage.view(sql, workspace_id))
        if view is None:
            raise WorkspaceNotFound(workspace_id)
        return view

    async def create(self, path):
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("Workspace path must be an existing directory")
        return await self._register(resolved)

    async def ensure(self, path):
        _, created = await self._register(path)
        return created

    async def reconcile(self):
        """Repair the durable membership projection once at process startup."""
        after = ("", "")
        while True:
            rows = await self._storage.transaction(
                lambda sql: sql.execute(
                    "SELECT workspace_id,session_id FROM workspaces_members "
                    "WHERE workspace_id>? OR (workspace_id=? AND session_id>?) "
                    "ORDER BY workspace_id,session_id LIMIT 100",
                    (after[0], after[0], after[1]),
                )
            ) or ()
            if not rows:
                break
            for workspace_id, session_id in rows:
                if not self._sessions.session_exists(session_id):
                    await self.detach_session(session_id)
                    continue
                try:
                    summary = await self._sessions.session_summary(session_id)
                    workspace = await self._workspace(workspace_id)
                except Exception as exc:
                    self._log.warning(
                        "workspace.reconcile.unreadable", session_id=session_id,
                        error_type=type(exc).__name__, error=str(exc) or type(exc).__name__,
                    )
                    continue
                if summary.workspace_root != workspace.path:
                    await self._storage.transaction(
                        lambda sql: sql.execute(
                            "DELETE FROM workspaces_members "
                            "WHERE workspace_id=? AND session_id=?",
                            (workspace_id, session_id),
                        ),
                        write=True,
                    )
            after = rows[-1]
        after_id = ""
        while True:
            rows = await self._storage.transaction(
                lambda sql: sql.execute(
                    "SELECT session_id FROM workspaces_archived WHERE session_id>? "
                    "ORDER BY session_id LIMIT 100", (after_id,),
                )
            ) or ()
            if not rows:
                break
            for (session_id,) in rows:
                if not self._sessions.session_exists(session_id):
                    await self.detach_session(session_id)
            after_id = rows[-1][0]
        cursor = None
        while True:
            page = await self._sessions.list_sessions(limit=100, cursor=cursor)
            for summary in page.items:
                if summary.unreadable or not summary.workspace_root:
                    continue
                def known(sql):
                    return sql.execute(
                        "SELECT id FROM workspaces_items w WHERE path=? AND NOT EXISTS "
                        "(SELECT 1 FROM workspaces_members m WHERE m.workspace_id=w.id "
                        "AND m.session_id=?) LIMIT 1",
                        (summary.workspace_root, summary.session_id),
                    )
                if await self._storage.transaction(known):
                    await self.attach_session(summary.session_id, summary.workspace_root)
            cursor = page.next_cursor
            if cursor is None:
                break

    async def attach_session(self, session_id, workspace_root):
        await self._register(workspace_root, session_id)

    async def _register(self, path, session_id=""):
        path = str(Path(path).expanduser().resolve())
        now = _now()
        def write(sql):
            found = sql.execute("SELECT id FROM workspaces_items WHERE path=? LIMIT 1", (path,))
            created = not found
            identity = found[0][0] if found else f"ws_{uuid.uuid4().hex[:12]}"
            if created:
                sql.execute(
                    "INSERT INTO workspaces_items VALUES(?,?,?,?,?,"
                    "(SELECT coalesce(max(ordinal),-1)+1 FROM workspaces_items))",
                    (identity, path, Path(path).name or path, now, now),
                )
            changed = created
            old_views = []
            if session_id:
                old = sql.execute(
                    "SELECT workspace_id FROM workspaces_members "
                    "WHERE session_id=? AND workspace_id!=? LIMIT 500",
                    (session_id, identity),
                )
                sql.execute(
                    "DELETE FROM workspaces_members WHERE session_id=? AND workspace_id!=?",
                    (session_id, identity),
                )
                old_views = [self._storage.view(sql, row[0]) for row in old]
                sql.execute(
                    "INSERT OR IGNORE INTO workspaces_members VALUES(?,?,"
                    "(SELECT coalesce(min(ordinal),1)-1 FROM workspaces_members "
                    "WHERE workspace_id=?))", (identity, session_id, identity),
                )
                if sql.changed:
                    sql.execute("UPDATE workspaces_items SET updated_at=? WHERE id=?", (now, identity))
                    changed = True
            return self._storage.view(sql, identity), created, changed, old_views
        async with self._lock:
            view, created, changed, old_views = await self._storage.transaction(write, write=True)
        for old_view in old_views:
            await self._events.emit(WORKSPACE_RESOURCE_CHANGED, WorkspaceResourceChanged(old_view))
        if changed:
            await self._events.emit(WORKSPACE_RESOURCE_CHANGED, WorkspaceResourceChanged(view))
        return view, created

    async def detach_session(self, session_id):
        def write(sql):
            rows = sql.execute(
                "SELECT workspace_id FROM workspaces_members WHERE session_id=? LIMIT 500",
                (session_id,),
            )
            sql.execute("DELETE FROM workspaces_members WHERE session_id=?", (session_id,))
            sql.execute("DELETE FROM workspaces_archived WHERE session_id=?", (session_id,))
            archived = bool(sql.changed)
            return [self._storage.view(sql, r[0]) for r in rows], archived
        if not await self._storage.domain.exists():
            return
        async with self._lock:
            views, archived = await self._storage.transaction(write, write=True)
        for view in views:
            await self._events.emit(WORKSPACE_RESOURCE_CHANGED, WorkspaceResourceChanged(view))
        if archived:
            await self._events.emit(ARCHIVED_SESSIONS_CHANGED, ArchivedSessionsChanged(session_id, False))

    async def rename(self, workspace_id, title):
        title = title.strip()
        if not title:
            raise ValueError("Workspace title must be non-empty")
        def write(sql):
            self._storage.view(sql, workspace_id)
            if sql.execute("SELECT 1 FROM workspaces_items WHERE title=? AND id!=? LIMIT 1", (title, workspace_id)):
                raise ValueError(f"Workspace title {title!r} is already in use")
            sql.execute("UPDATE workspaces_items SET title=?,updated_at=? WHERE id=?", (title, _now(), workspace_id))
            return self._storage.view(sql, workspace_id)
        async with self._lock:
            view = await self._storage.transaction(write, write=True)
        await self._events.emit(WORKSPACE_RESOURCE_CHANGED, WorkspaceResourceChanged(view))
        return view

    async def delete(self, workspace_id):
        def write(sql):
            sql.execute("DELETE FROM workspaces_items WHERE id=?", (workspace_id,))
            found = bool(sql.changed)
            sql.execute("DELETE FROM workspaces_members WHERE workspace_id=?", (workspace_id,))
            return found
        async with self._lock:
            found = await self._storage.transaction(write, write=True)
        if found:
            await self._events.emit(WORKSPACE_RESOURCE_REMOVED, WorkspaceResourceRemoved(workspace_id))
        return found

    async def insert_before(self, workspace_id, before_workspace_id):
        async with self._lock:
            changed = await self._storage.transaction(
                lambda sql: self._storage.move(
                    sql, "workspaces_items", workspace_id, before_workspace_id,
                ),
                write=True,
            )
        if changed:
            await self._events.emit(WORKSPACE_ORDER_CHANGED, WorkspaceOrderChanged(workspace_id, before_workspace_id))

    async def insert_session_before(self, workspace_id, session_id, before_session_id):
        def write(sql):
            self._storage.view(sql, workspace_id)
            changed = self._storage.move(sql, "workspaces_members", session_id, before_session_id, workspace_id)
            return changed, self._storage.view(sql, workspace_id)
        async with self._lock:
            changed, view = await self._storage.transaction(write, write=True)
        if changed:
            await self._events.emit(
                WORKSPACE_SESSION_ORDER_CHANGED,
                WorkspaceSessionOrderChanged(workspace_id, session_id, before_session_id),
            )
        return view

    async def set_archived(self, session_id, archived):
        if not self._sessions.session_exists(session_id):
            raise WorkspaceSessionNotFound(f"Session {session_id!r} was not found")
        def write(sql):
            if archived:
                sql.execute(
                    "INSERT OR IGNORE INTO workspaces_archived VALUES(?,"
                    "(SELECT coalesce(max(ordinal),-1)+1 FROM workspaces_archived))",
                    (session_id,),
                )
            else:
                sql.execute("DELETE FROM workspaces_archived WHERE session_id=?", (session_id,))
            changed = bool(sql.changed)
            rows = sql.execute(
                "SELECT workspace_id FROM workspaces_members WHERE session_id=? LIMIT 500",
                (session_id,),
            )
            return changed, [self._storage.view(sql, row[0]) for row in rows]
        async with self._lock:
            changed, views = await self._storage.transaction(write, write=True)
        if changed:
            for view in views:
                await self._events.emit(WORKSPACE_RESOURCE_CHANGED, WorkspaceResourceChanged(view))
            await self._events.emit(ARCHIVED_SESSIONS_CHANGED, ArchivedSessionsChanged(session_id, archived))
