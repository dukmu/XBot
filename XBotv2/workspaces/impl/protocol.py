"""HTTP resources for the process Workspace registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import JsonValue
from XBotv2.protocol.api.models import WireModel, server_event
from XBotv2.protocol.api.http_util import _SSE_RESPONSE, _sse_response, HttpServerError
from XBotv2.protocol.api.sse import encode_server_event
from XBotv2.core.api.domain import SessionScope
from XBotv2.session.api.contracts import SessionResourceChanged, SessionResourceRemoved
from XBotv2.workspaces.api.contracts import (
    DirectoriesPort,
    DirectoryListing,
    DirectoryNotFound,
    DirectoryNotReadable,
    WorkspaceListing,
    WorkspaceNotFound,
    WorkspaceSessionMoveInvalid,
    WorkspaceSessionNotFound,
    WorkspaceView,
    WorkspaceEventSubscription,
    WorkspaceEventsPort,
    WorkspacesPort,
    ArchivedSessionsChanged,
    WorkspaceOrderChanged,
    WorkspaceSessionOrderChanged,
    WorkspaceResourceChanged,
    WorkspaceResourceRemoved,
)
from XBotv2.workspaces.api.events import WorkspaceCursorExpired, WorkspaceEventFrame


from XBotv2.workspaces.api.protocol import (
    WorkspaceSessionPageResponse, WorkspaceListResponse, WorkspaceCreateRequest,
    WorkspaceCreateResponse, WorkspaceRenameRequest, WorkspaceResponse,
    WorkspaceOrderRequest, WorkspaceOrderResponse, WorkspaceSessionOrderRequest,
    WorkspaceDeleteResponse, ArchivedSessionsResponse,
)


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
    ) -> DirectoryListing:
        try:
            listing = directories.list(path)
        except DirectoryNotFound as exc:
            raise HttpServerError("directory_not_found", str(exc), status=404) from exc
        except DirectoryNotReadable as exc:
            raise HttpServerError("directory_not_readable", str(exc), status=403) from exc
        return listing

    @router.get("/workspaces", operation_id="list_workspaces")
    async def list_workspaces(
        limit: int = Query(default=100, ge=1, le=100), cursor: str | None = None,
    ) -> WorkspaceListResponse:
        event_cursor = workspace_events.sequence
        try:
            listing = await workspaces.list(limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HttpServerError("invalid_catalog_cursor", str(exc), status=400) from exc
        return WorkspaceListResponse(
            items=list(listing.items),
            next_cursor=listing.next_cursor, previous_cursor=listing.previous_cursor,
            event_cursor=event_cursor,
        )

    @router.get("/workspaces/archived-sessions", operation_id="list_archived_sessions")
    async def list_archived_sessions(
        limit: int = Query(default=100, ge=1, le=100), cursor: str | None = None,
    ) -> WorkspaceSessionPageResponse:
        event_cursor = workspace_events.sequence
        try:
            page = await workspaces.list_archived(limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HttpServerError("invalid_catalog_cursor", str(exc), status=400) from exc
        return WorkspaceSessionPageResponse(
            sessions=list(page.sessions), archived_session_ids=list(page.archived_session_ids),
            next_cursor=page.next_cursor, previous_cursor=page.previous_cursor,
            event_cursor=event_cursor,
        )

    @router.get("/workspaces/{workspace_id}/sessions", operation_id="list_workspace_sessions")
    async def list_workspace_sessions(
        workspace_id: str, limit: int = Query(default=100, ge=1, le=100),
        cursor: str | None = None,
    ) -> WorkspaceSessionPageResponse:
        event_cursor = workspace_events.sequence
        try:
            page = await workspaces.list_sessions(workspace_id, limit=limit, cursor=cursor)
        except WorkspaceNotFound as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise HttpServerError("invalid_catalog_cursor", str(exc), status=400) from exc
        return WorkspaceSessionPageResponse(
            sessions=list(page.sessions), archived_session_ids=list(page.archived_session_ids),
            next_cursor=page.next_cursor, previous_cursor=page.previous_cursor,
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
        return _sse_response(
            _workspace_sse(stream, after),
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
            workspace=workspace,
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
        return WorkspaceResponse(workspace=workspace)

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
            await workspaces.insert_before(
                workspace_id,
                request.before_workspace_id,
            )
        except WorkspaceNotFound as exc:
            raise _not_found(exc) from exc
        return WorkspaceOrderResponse(workspace_id=workspace_id, before_workspace_id=request.before_workspace_id)

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
        return WorkspaceResponse(workspace=workspace)

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
        await workspaces.set_archived(session_id, archived)
    except WorkspaceSessionNotFound as exc:
        raise HttpServerError("session_not_found", str(exc), status=404) from exc
    return ArchivedSessionsResponse(session_id=session_id, archived=archived)


async def _workspace_sse(
    stream: WorkspaceEventSubscription,
    cursor: int,
) -> AsyncIterator[bytes]:
    try:
        yield encode_server_event(server_event(
            kind="catalog/connected",
            payload={"cursor": cursor},
            sequence=cursor,
            session_id="",
            thread_id="workspaces",
            scope=SessionScope(),
        ))
        async for frame in stream:
            kind, payload = _workspace_event(frame)
            yield encode_server_event(server_event(
                kind=kind,
                payload=payload,
                sequence=frame.sequence,
                session_id="",
                thread_id="workspaces",
                scope=SessionScope(),
            ))
    finally:
        await stream.aclose()


def _workspace_event(frame: WorkspaceEventFrame) -> tuple[str, dict[str, JsonValue]]:
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
            "workspace_id": change.workspace_id, "before_workspace_id": change.before_workspace_id,
        }
    if isinstance(change, WorkspaceSessionOrderChanged):
        return "catalog/workspace-session-order-changed", {
            "workspace_id": change.workspace_id, "session_id": change.session_id,
            "before_session_id": change.before_session_id,
        }
    if isinstance(change, ArchivedSessionsChanged):
        return "catalog/archived-sessions-changed", {
            "session_id": change.session_id, "archived": change.archived,
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
    "WorkspaceListResponse",
    "WorkspaceOrderRequest",
    "WorkspaceOrderResponse",
    "WorkspaceSessionOrderRequest",
    "WorkspaceRenameRequest",
    "WorkspaceResponse",
    "WorkspaceEventsPort",
    "DirectoriesPort",
    "build_router",
]
