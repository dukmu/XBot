"""HTTP resources for the process Workspace registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from XBotv2.protocol import WireModel
from XBotv2.protocol.http_util import (
    _SSE_RESPONSE,
    _format_sse,
    HttpServerError,
)
from XBotv2.session.contracts import (
    SessionResourceChanged,
    SessionResourceRemoved,
)
from XBotv2.workspaces import (
    WorkspaceListing,
    WorkspaceNotFound,
    WorkspaceSessionMoveInvalid,
    WorkspaceSessionNotFound,
    WorkspaceView,
)
from XBotv2.workspaces.contracts import (
    ArchivedSessionsChanged,
    WorkspaceOrderChanged,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
)
from XBotv2.workspaces.events import (
    WorkspaceCursorExpired,
    WorkspaceEventFrame,
)
from XBotv2.workspaces.directories import (
    DirectoryListing,
    DirectoryNotFound,
    DirectoryNotReadable,
)


class WorkspacesPort(Protocol):
    async def list(self) -> WorkspaceListing: ...

    async def create(self, path: str) -> tuple[WorkspaceView, bool]: ...

    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...

    async def delete(self, workspace_id: str) -> bool: ...

    async def insert_before(
        self,
        workspace_id: str,
        before_workspace_id: str | None,
    ) -> tuple[str, ...]: ...

    async def insert_session_before(
        self,
        workspace_id: str,
        session_id: str,
        before_session_id: str | None,
    ) -> WorkspaceView: ...

    async def set_archived(
        self,
        session_id: str,
        archived: bool,
    ) -> tuple[str, ...]: ...


class WorkspaceEventSubscription(Protocol):
    def __aiter__(self) -> "WorkspaceEventSubscription": ...
    async def __anext__(self) -> WorkspaceEventFrame: ...
    async def aclose(self) -> None: ...


class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...

    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...


class DirectoriesPort(Protocol):
    def list(self, path: str | None = None) -> DirectoryListing: ...


class DirectoryEntryData(WireModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    hidden: bool = False


class DirectoryListingResponse(WireModel):
    path: str = Field(min_length=1)
    parent: str | None = None
    home: str = Field(min_length=1)
    separator: Literal["/", "\\"]
    entries: list[DirectoryEntryData] = Field(default_factory=list)
    truncated: bool = False


class WorkspaceData(WireModel):
    workspace_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    session_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class WorkspaceListResponse(WireModel):
    items: list[WorkspaceData] = Field(default_factory=list)
    archived_session_ids: list[str] = Field(default_factory=list)
    event_cursor: int = Field(default=0, ge=0)


class WorkspaceCreateRequest(WireModel):
    path: str = Field(min_length=1)


class WorkspaceCreateResponse(WireModel):
    workspace: WorkspaceData
    created: bool


class WorkspaceRenameRequest(WireModel):
    title: str = Field(min_length=1)


class WorkspaceResponse(WireModel):
    workspace: WorkspaceData


class WorkspaceOrderRequest(WireModel):
    before_workspace_id: str | None = None


class WorkspaceOrderResponse(WireModel):
    workspace_ids: list[str]


class WorkspaceSessionOrderRequest(WireModel):
    before_session_id: str | None = None


class WorkspaceDeleteResponse(WireModel):
    workspace_id: str
    status: Literal["deleted"] = "deleted"


class ArchivedSessionsResponse(WireModel):
    archived_session_ids: list[str] = Field(default_factory=list)


def build_router(
    *,
    workspaces: WorkspacesPort,
    workspace_events: WorkspaceEventsPort,
    directories: DirectoriesPort,
) -> APIRouter:
    router = APIRouter()

    @router.get("/directories", operation_id="list_workspace_directories")
    async def list_workspace_directories(
        path: str | None = Query(default=None),
    ) -> DirectoryListingResponse:
        try:
            listing = directories.list(path)
        except DirectoryNotFound as exc:
            raise HttpServerError("directory_not_found", str(exc), status=404) from exc
        except DirectoryNotReadable as exc:
            raise HttpServerError("directory_not_readable", str(exc), status=403) from exc
        return DirectoryListingResponse.model_validate(listing, from_attributes=True)

    @router.get("/workspaces", operation_id="list_workspaces")
    async def list_workspaces() -> WorkspaceListResponse:
        event_cursor = workspace_events.sequence
        listing = await workspaces.list()
        return WorkspaceListResponse(
            items=[_workspace_data(item) for item in listing.items],
            archived_session_ids=list(listing.archived_session_ids),
            event_cursor=event_cursor,
        )

    @router.get(
        "/workspaces/events",
        operation_id="stream_workspace_events",
        response_class=StreamingResponse,
        responses=_SSE_RESPONSE,
    )
    async def stream_workspace_events(
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            stream = workspace_events.subscribe(after)
        except WorkspaceCursorExpired as exc:
            raise HttpServerError(
                "workspace_event_cursor_expired",
                str(exc),
                status=409,
                details={"oldest_sequence": exc.oldest},
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise HttpServerError(
                "invalid_workspace_event_cursor",
                str(exc),
                status=400,
            ) from exc
        return StreamingResponse(
            _workspace_sse(stream, after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/workspaces", operation_id="create_workspace")
    async def create_workspace(
        request: WorkspaceCreateRequest,
    ) -> WorkspaceCreateResponse:
        try:
            workspace, created = await workspaces.create(request.path)
        except ValueError as exc:
            raise HttpServerError("invalid_workspace", str(exc), status=400) from exc
        return WorkspaceCreateResponse(
            workspace=_workspace_data(workspace),
            created=created,
        )

    @router.patch("/workspaces/{workspace_id}", operation_id="rename_workspace")
    async def rename_workspace(
        workspace_id: str,
        request: WorkspaceRenameRequest,
    ) -> WorkspaceResponse:
        try:
            workspace = await workspaces.rename(workspace_id, request.title)
        except WorkspaceNotFound as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise HttpServerError("workspace_conflict", str(exc), status=409) from exc
        return WorkspaceResponse(workspace=_workspace_data(workspace))

    @router.delete(
        "/workspaces/{workspace_id}",
        operation_id="delete_workspace",
    )
    async def delete_workspace(workspace_id: str) -> WorkspaceDeleteResponse:
        if not await workspaces.delete(workspace_id):
            raise _not_found(WorkspaceNotFound(workspace_id))
        return WorkspaceDeleteResponse(workspace_id=workspace_id)

    @router.post(
        "/workspaces/{workspace_id}/order",
        operation_id="reorder_workspace",
    )
    async def reorder_workspace(
        workspace_id: str,
        request: WorkspaceOrderRequest,
    ) -> WorkspaceOrderResponse:
        try:
            order = await workspaces.insert_before(
                workspace_id,
                request.before_workspace_id,
            )
        except WorkspaceNotFound as exc:
            raise _not_found(exc) from exc
        return WorkspaceOrderResponse(workspace_ids=list(order))

    @router.post(
        "/workspaces/{workspace_id}/sessions/{session_id}/order",
        operation_id="reorder_workspace_session",
    )
    async def reorder_workspace_session(
        workspace_id: str,
        session_id: str,
        request: WorkspaceSessionOrderRequest,
    ) -> WorkspaceResponse:
        try:
            workspace = await workspaces.insert_session_before(
                workspace_id,
                session_id,
                request.before_session_id,
            )
        except WorkspaceNotFound as exc:
            raise _not_found(exc) from exc
        except WorkspaceSessionMoveInvalid as exc:
            raise HttpServerError(
                "workspace_session_move_invalid",
                str(exc),
                status=409,
            ) from exc
        return WorkspaceResponse(workspace=_workspace_data(workspace))

    @router.put(
        "/sessions/{session_id}/archive",
        operation_id="archive_session",
    )
    async def archive_session(session_id: str) -> ArchivedSessionsResponse:
        return await _archive_response(workspaces, session_id, True)

    @router.delete(
        "/sessions/{session_id}/archive",
        operation_id="unarchive_session",
    )
    async def unarchive_session(session_id: str) -> ArchivedSessionsResponse:
        return await _archive_response(workspaces, session_id, False)

    return router


async def _archive_response(
    workspaces: WorkspacesPort,
    session_id: str,
    archived: bool,
) -> ArchivedSessionsResponse:
    try:
        session_ids = await workspaces.set_archived(session_id, archived)
    except WorkspaceSessionNotFound as exc:
        raise HttpServerError("session_not_found", str(exc), status=404) from exc
    return ArchivedSessionsResponse(archived_session_ids=list(session_ids))


def _workspace_data(workspace: WorkspaceView) -> WorkspaceData:
    return WorkspaceData(
        workspace_id=workspace.workspace_id,
        path=workspace.path,
        title=workspace.title,
        session_ids=list(workspace.session_ids),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


async def _workspace_sse(
    stream: WorkspaceEventSubscription,
    cursor: int,
) -> AsyncIterator[bytes]:
    try:
        yield _format_sse(
            event={"type": "catalog/connected", "data": {"cursor": cursor}},
            seq=cursor,
            thread_id="workspaces",
        )
        async for frame in stream:
            event_type, data = _workspace_event(frame)
            yield _format_sse(
                event={"type": event_type, "data": data},
                seq=frame.sequence,
                thread_id="workspaces",
            )
    finally:
        await stream.aclose()


def _workspace_event(frame: WorkspaceEventFrame) -> tuple[str, dict[str, object]]:
    change = frame.change
    if isinstance(change, SessionResourceChanged):
        return (
            "catalog/session-added" if change.added else "catalog/session-changed",
            {"session": change.session.model_dump(mode="json")},
        )
    if isinstance(change, SessionResourceRemoved):
        return "catalog/session-removed", {"session_id": change.session_id}
    if isinstance(change, WorkspaceResourceChanged):
        return "catalog/workspace-changed", {
            "workspace": change.workspace.model_dump(mode="json")
        }
    if isinstance(change, WorkspaceResourceRemoved):
        return "catalog/workspace-removed", {"workspace_id": change.workspace_id}
    if isinstance(change, WorkspaceOrderChanged):
        return "catalog/workspace-order-changed", {
            "workspace_ids": list(change.workspace_ids),
        }
    if isinstance(change, ArchivedSessionsChanged):
        return "catalog/archived-sessions-changed", {
            "archived_session_ids": list(change.session_ids),
        }
    raise TypeError(f"Unsupported Workspace catalog change: {type(change).__name__}")


def _not_found(error: WorkspaceNotFound) -> HttpServerError:
    return HttpServerError(
        "workspace_not_found",
        str(error),
        status=404,
    )


__all__ = [
    "ArchivedSessionsResponse",
    "WorkspaceCreateRequest",
    "WorkspaceCreateResponse",
    "WorkspaceData",
    "WorkspaceListResponse",
    "WorkspaceOrderRequest",
    "WorkspaceOrderResponse",
    "WorkspaceSessionOrderRequest",
    "WorkspaceRenameRequest",
    "WorkspaceResponse",
    "WorkspaceEventsPort",
    "DirectoriesPort",
    "DirectoryEntryData",
    "DirectoryListingResponse",
    "build_router",
]
