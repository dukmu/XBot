# `server-routes-workspaces`

HTTP routes for workspace catalogs, directory browsing, ordering,
archive/unarchive, and the workspace SSE cursor. The root `workspaces`
plugin contributes this router through `mount_http`.

Source: `XBotv2/workspaces/plugin.py`, `XBotv2/workspaces/protocol.py`.

## Routes

| Method | Path | Operation ID | Response |
|---|---|---|---|
| `GET` | `/directories` | `list_workspace_directories` | `DirectoryListing` |
| `GET` | `/workspaces` | `list_workspaces` | `WorkspaceListResponse` |
| `GET` | `/workspaces/events` | `stream_workspace_events` | SSE |
| `POST` | `/workspaces` | `create_workspace` | `WorkspaceCreateResponse` |
| `PATCH` | `/workspaces/{workspace_id}` | `rename_workspace` | `WorkspaceResponse` |
| `DELETE` | `/workspaces/{workspace_id}` | `delete_workspace` | `WorkspaceDeleteResponse` |
| `POST` | `/workspaces/{workspace_id}/order` | `reorder_workspace` | `WorkspaceOrderResponse` |
| `POST` | `/workspaces/{workspace_id}/sessions/{session_id}/order` | `reorder_workspace_session` | `WorkspaceResponse` |
| `PUT` | `/sessions/{session_id}/archive` | `archive_session` | `ArchivedSessionsResponse` |
| `DELETE` | `/sessions/{session_id}/archive` | `unarchive_session` | `ArchivedSessionsResponse` |

## Wire models

```python
class WorkspaceListResponse(WireModel):
    items: list[WorkspaceView] = []
    archived_session_ids: list[str] = []
    event_cursor: int = 0

class WorkspaceCreateRequest(WireModel):
    path: str = Field(min_length=1)

class WorkspaceCreateResponse(WireModel):
    workspace: WorkspaceView
    created: bool

class WorkspaceRenameRequest(WireModel):
    title: str = Field(min_length=1)

class WorkspaceResponse(WireModel):
    workspace: WorkspaceView

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
    archived_session_ids: list[str] = []
```

The actual implementation uses `Field(default_factory=list)` for list fields;
the snippets are schema notation. `WorkspaceRenameRequest` has no
documented 200-character limit in the current source.

## Router contract

```python
def build_router(
    *,
    workspaces: WorkspacesPort,
    workspace_events: WorkspaceEventsPort,
    directories: DirectoriesPort,
) -> APIRouter: ...
```

`GET /directories` accepts optional query `path`. `GET /workspaces/events`
accepts `after: int = 0` and returns `text/event-stream`; stale cursors map to
409 with `workspace_event_cursor_expired` and `oldest_sequence` details.
Archive routes are session-level even though ordering routes are nested under
a workspace.

## SSE event mapping

The stream serializes the typed workspace change frame. The mapping is:

```text
SessionResourceChanged(added=True)   -> catalog/session-added
SessionResourceChanged(added=False)  -> catalog/session-changed
SessionResourceRemoved                -> catalog/session-removed
WorkspaceResourceChanged              -> catalog/workspace-changed
WorkspaceResourceRemoved              -> catalog/workspace-removed
WorkspaceOrderChanged                 -> catalog/workspace-order-changed
ArchivedSessionsChanged               -> catalog/archived-sessions-changed
```

Routes call the `workspaces` and `workspace_events` ports only. They do not
access StateService or filesystem paths directly.
