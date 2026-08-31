"""Process-level durable Workspace registry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
import uuid

from xcore.state import StateService

from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.workspaces.models import (
    WorkspaceRecord,
    WorkspaceListing,
    WorkspaceSnapshot,
    WorkspaceView,
)
from XBotv2.workspaces.contracts import (
    ARCHIVED_SESSIONS_CHANGED,
    ArchivedSessionsChanged,
    WORKSPACE_ORDER_CHANGED,
    WORKSPACE_RESOURCE_CHANGED,
    WORKSPACE_RESOURCE_REMOVED,
    WorkspaceOrderChanged,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
)


class WorkspaceNotFound(LookupError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"Workspace {workspace_id!r} was not found")
        self.workspace_id = workspace_id


class WorkspaceSessionNotFound(LookupError):
    pass


class WorkspaceSessionMoveInvalid(ValueError):
    pass


class SessionSummary(Protocol):
    session_id: str
    workspace_root: str


class SessionListing(Protocol):
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...


class ResourceEvents(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...


class WorkspaceRegistry:
    """Own Workspace identity, ordering, titles, and session membership."""

    def __init__(
        self,
        state: StateService,
        sessions: SessionListing,
        events: ResourceEvents,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._state = state
        self._sessions = sessions
        self._events = events
        self._log = runtime_log.bind("workspaces")
        self._lock = asyncio.Lock()

    async def list(self) -> WorkspaceListing:
        sessions = await self._sessions.list_sessions()
        session_ids = {str(session.session_id) for session in sessions}
        async with self._lock:
            snapshot = await self._snapshot()
            snapshot, membership_changes = _reconcile_membership(snapshot, sessions)
            archived_session_ids = tuple(
                session_id
                for session_id in snapshot.archived_session_ids
                if session_id in session_ids
            )
            archive_changed = archived_session_ids != snapshot.archived_session_ids
            if archive_changed:
                removed = len(snapshot.archived_session_ids) - len(archived_session_ids)
                snapshot = snapshot.model_copy(update={
                    "archived_session_ids": archived_session_ids,
                })
            if membership_changes or archive_changed:
                await self._write(snapshot)
            if membership_changes:
                self._log.info(
                    "workspace.membership.reconciled",
                    workspaces=len(membership_changes),
                )
            if archive_changed:
                self._log.info(
                    "workspace.archive.reconciled",
                    removed=removed,
                )
        for record in membership_changes:
            await self._events.emit(
                WORKSPACE_RESOURCE_CHANGED,
                WorkspaceResourceChanged(_view(record)),
            )
        return WorkspaceListing(
            items=_views(snapshot.items),
            archived_session_ids=archived_session_ids,
        )

    async def create(self, path: Path | str) -> tuple[WorkspaceView, bool]:
        record, created = await self._register(path)
        listing = await self.list()
        workspace = next(
            item for item in listing.items
            if item.workspace_id == record.id
        )
        return workspace, created

    async def ensure(self, path: Path | str) -> bool:
        """Ensure registry identity without projecting potentially corrupt sessions."""
        _record, created = await self._register(path)
        return created

    async def attach_session(
        self,
        session_id: str,
        workspace_root: Path | str,
    ) -> None:
        """Persist membership from a committed Session resource event."""
        await self._register(workspace_root, session_id=session_id)

    async def detach_session(self, session_id: str) -> None:
        """Remove a deleted Session from membership and archive state."""
        async with self._lock:
            snapshot = await self._snapshot()
            changed: list[WorkspaceRecord] = []
            items: list[WorkspaceRecord] = []
            for record in snapshot.items:
                if session_id in record.session_ids:
                    record = record.model_copy(update={
                        "session_ids": tuple(
                            value for value in record.session_ids
                            if value != session_id
                        ),
                        "updated_at": _now(),
                    })
                    changed.append(record)
                items.append(record)
            archived = tuple(
                value for value in snapshot.archived_session_ids
                if value != session_id
            )
            archive_changed = archived != snapshot.archived_session_ids
            if not changed and not archive_changed:
                return
            await self._write(snapshot.model_copy(update={
                "items": tuple(items),
                "archived_session_ids": archived,
            }))
        for record in changed:
            await self._events.emit(
                WORKSPACE_RESOURCE_CHANGED,
                WorkspaceResourceChanged(_view(record)),
            )
        if archive_changed:
            await self._events.emit(
                ARCHIVED_SESSIONS_CHANGED,
                ArchivedSessionsChanged(archived),
            )
        self._log.info(
            "workspace.session.detached",
            session_id=session_id,
            workspaces=len(changed),
            archive_changed=archive_changed,
        )

    async def _register(
        self,
        path: Path | str,
        *,
        session_id: str = "",
    ) -> tuple[WorkspaceRecord, bool]:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("Workspace path must be an existing directory")
        value = str(resolved)
        async with self._lock:
            snapshot = await self._snapshot()
            existing = next(
                (record for record in snapshot.items if record.path == value),
                None,
            )
            if existing is not None:
                if session_id and session_id not in existing.session_ids:
                    record = existing.model_copy(update={
                        "session_ids": (session_id, *existing.session_ids),
                        "updated_at": _now(),
                    })
                    items = list(snapshot.items)
                    items[_index(snapshot, existing.id)] = record
                    await self._write(snapshot.model_copy(update={
                        "items": tuple(items),
                    }))
                    changed = True
                else:
                    record = existing
                    changed = False
                created = False
            else:
                now = _now()
                record = WorkspaceRecord(
                    id=f"ws_{uuid.uuid4().hex[:12]}",
                    path=value,
                    title=resolved.name or value,
                    session_ids=(session_id,) if session_id else (),
                    created_at=now,
                    updated_at=now,
                )
                await self._write(snapshot.model_copy(update={
                    "items": (*snapshot.items, record),
                }))
                created = True
                changed = True
        if created:
            self._log.info("workspace.created", workspace_id=record.id, path=record.path)
        elif changed:
            self._log.info(
                "workspace.session.attached",
                workspace_id=record.id,
                session_id=session_id,
            )
        if changed:
            await self._events.emit(
                WORKSPACE_RESOURCE_CHANGED,
                WorkspaceResourceChanged(_view(record)),
            )
        return record, created

    async def rename(self, workspace_id: str, title: str) -> WorkspaceView:
        value = title.strip()
        if not value:
            raise ValueError("Workspace title must be non-empty")
        async with self._lock:
            snapshot = await self._snapshot()
            index = _index(snapshot, workspace_id)
            if any(
                record.id != workspace_id and record.title == value
                for record in snapshot.items
            ):
                raise ValueError(f"Workspace title {value!r} is already in use")
            current = snapshot.items[index]
            updated = current.model_copy(update={"title": value, "updated_at": _now()})
            items = list(snapshot.items)
            items[index] = updated
            await self._write(snapshot.model_copy(update={"items": tuple(items)}))
        self._log.info("workspace.renamed", workspace_id=workspace_id)
        workspace = _view(updated)
        await self._events.emit(
            WORKSPACE_RESOURCE_CHANGED,
            WorkspaceResourceChanged(workspace),
        )
        return workspace

    async def delete(self, workspace_id: str) -> bool:
        async with self._lock:
            snapshot = await self._snapshot()
            items = tuple(
                record for record in snapshot.items if record.id != workspace_id
            )
            if len(items) == len(snapshot.items):
                return False
            await self._write(snapshot.model_copy(update={"items": items}))
        self._log.info("workspace.deleted", workspace_id=workspace_id)
        await self._events.emit(
            WORKSPACE_RESOURCE_REMOVED,
            WorkspaceResourceRemoved(workspace_id),
        )
        return True

    async def insert_before(
        self,
        workspace_id: str,
        before_workspace_id: str | None,
    ) -> tuple[str, ...]:
        async with self._lock:
            snapshot = await self._snapshot()
            source = _index(snapshot, workspace_id)
            anchor = (
                len(snapshot.items)
                if before_workspace_id is None
                else _index(snapshot, before_workspace_id)
            )
            items = list(snapshot.items)
            record = items.pop(source)
            if source < anchor:
                anchor -= 1
            items.insert(anchor, record)
            if tuple(items) == snapshot.items:
                return tuple(item.id for item in items)
            await self._write(snapshot.model_copy(update={"items": tuple(items)}))
        order = tuple(item.id for item in items)
        self._log.info("workspace.reordered", workspace_ids=order)
        await self._events.emit(
            WORKSPACE_ORDER_CHANGED,
            WorkspaceOrderChanged(order),
        )
        return order

    async def insert_session_before(
        self,
        workspace_id: str,
        session_id: str,
        before_session_id: str | None,
    ) -> WorkspaceView:
        await self.list()
        async with self._lock:
            snapshot = await self._snapshot()
            index = _index(snapshot, workspace_id)
            record = snapshot.items[index]
            if session_id not in record.session_ids:
                raise WorkspaceSessionMoveInvalid(
                    f"Session {session_id!r} is not in Workspace {workspace_id!r}"
                )
            if before_session_id is not None and before_session_id not in record.session_ids:
                raise WorkspaceSessionMoveInvalid(
                    f"Session anchor {before_session_id!r} is not in Workspace {workspace_id!r}"
                )
            if before_session_id == session_id:
                return _view(record)
            session_ids = [value for value in record.session_ids if value != session_id]
            position = (
                len(session_ids)
                if before_session_id is None
                else session_ids.index(before_session_id)
            )
            session_ids.insert(position, session_id)
            ordered = tuple(session_ids)
            if ordered == record.session_ids:
                return _view(record)
            updated = record.model_copy(update={
                "session_ids": ordered,
                "updated_at": _now(),
            })
            items = list(snapshot.items)
            items[index] = updated
            await self._write(snapshot.model_copy(update={"items": tuple(items)}))
        self._log.info(
            "workspace.session.reordered",
            workspace_id=workspace_id,
            session_id=session_id,
        )
        workspace = _view(updated)
        await self._events.emit(
            WORKSPACE_RESOURCE_CHANGED,
            WorkspaceResourceChanged(workspace),
        )
        return workspace

    async def set_archived(
        self,
        session_id: str,
        archived: bool,
    ) -> tuple[str, ...]:
        known = {
            str(session.session_id)
            for session in await self._sessions.list_sessions()
        }
        if session_id not in known:
            raise WorkspaceSessionNotFound(
                f"Session {session_id!r} was not found"
            )
        async with self._lock:
            snapshot = await self._snapshot()
            current = list(snapshot.archived_session_ids)
            if archived and session_id not in current:
                current.append(session_id)
            elif not archived:
                current = [value for value in current if value != session_id]
            archived_session_ids = tuple(current)
            if archived_session_ids == snapshot.archived_session_ids:
                return archived_session_ids
            await self._write(snapshot.model_copy(update={
                "archived_session_ids": archived_session_ids,
            }))
        self._log.info(
            "workspace.session.archive.updated",
            session_id=session_id,
            archived=archived,
        )
        await self._events.emit(
            ARCHIVED_SESSIONS_CHANGED,
            ArchivedSessionsChanged(archived_session_ids),
        )
        return archived_session_ids

    async def _snapshot(self) -> WorkspaceSnapshot:
        stored = await self._state.get("snapshot")
        if stored is None:
            return WorkspaceSnapshot()
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted Workspace snapshot must be an object")
        return WorkspaceSnapshot.from_dict(stored)

    async def _write(self, snapshot: WorkspaceSnapshot) -> None:
        await self._state.set("snapshot", snapshot.to_dict())


def _views(records: tuple[WorkspaceRecord, ...]) -> tuple[WorkspaceView, ...]:
    return tuple(_view(record) for record in records)


def _reconcile_membership(
    snapshot: WorkspaceSnapshot,
    sessions: tuple[SessionSummary, ...],
) -> tuple[WorkspaceSnapshot, tuple[WorkspaceRecord, ...]]:
    by_path: dict[str, set[str]] = {record.path: set() for record in snapshot.items}
    for session in sessions:
        members = by_path.get(str(session.workspace_root))
        if members is not None:
            members.add(str(session.session_id))
    changed: list[WorkspaceRecord] = []
    items: list[WorkspaceRecord] = []
    for record in snapshot.items:
        known = by_path[record.path]
        retained = tuple(value for value in record.session_ids if value in known)
        added = tuple(sorted(known.difference(retained), reverse=True))
        session_ids = (*added, *retained)
        if session_ids != record.session_ids:
            record = record.model_copy(update={
                "session_ids": session_ids,
                "updated_at": _now(),
            })
            changed.append(record)
        items.append(record)
    if not changed:
        return snapshot, ()
    return snapshot.model_copy(update={"items": tuple(items)}), tuple(changed)


def _index(snapshot: WorkspaceSnapshot, workspace_id: str) -> int:
    for index, record in enumerate(snapshot.items):
        if record.id == workspace_id:
            return index
    raise WorkspaceNotFound(workspace_id)


def _view(
    record: WorkspaceRecord,
) -> WorkspaceView:
    return WorkspaceView(
        workspace_id=record.id,
        path=record.path,
        title=record.title,
        session_ids=record.session_ids,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "WorkspaceNotFound",
    "WorkspaceRegistry",
    "WorkspaceSessionMoveInvalid",
    "WorkspaceSessionNotFound",
]
