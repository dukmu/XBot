# `server-routes-workspaces`

Workspace catalog HTTP routes — list, create, rename, delete, reorder
workspaces and sessions, archive/unarchive sessions, directory listing,
and SSE event streaming. Registered via `contribute_router()` as
`xbot.http.workspaces`.

- **Import/profile:** `server-routes-workspaces`, server profile.
- **Source:** `XBotv2/workspaces/protocol.py`,
  `XBotv2/workspaces/http/plugin.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Routes (`build_router`)

```python
def build_router(
    *,
    workspaces: WorkspacesPort,
    workspace_events: WorkspaceEventsPort,
    directories: DirectoriesPort,
) -> APIRouter:
```

### `GET /directories` → `DirectoryListing`

```python
@router.get("/directories", operation_id="list_workspace_directories")
async def list_workspace_directories(
    path: str | None = Query(default=None),
) -> DirectoryListing:
```

Returns `directories.list(path)` or raises `HttpServerError("directory_not_found", status=404)` / `HttpServerError("directory_not_readable", status=403)`.

### `GET /workspaces` → `WorkspaceListResponse`

```python
@router.get("/workspaces", operation_id="list_workspaces")
async def list_workspaces() -> WorkspaceListResponse:
```

### `GET /workspaces/events` → SSE

```python
@router.get(
    "/workspaces/events",
    operation_id="stream_workspace_events",
    response_class=StreamingResponse,
    responses=_SSE_RESPONSE,
)
async def stream_workspace_events(
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
```

SSE event types:
| Change type | Event type |
|---|---|
| `SessionResourceChanged` (added) | `catalog/session-added` |
| `SessionResourceChanged` (changed) | `catalog/session-changed` |
| `SessionResourceRemoved` | `catalog/session-removed` |
| `WorkspaceResourceChanged` | `catalog/workspace-changed` |
| `WorkspaceResourceRemoved` | `catalog/workspace-removed` |
| `WorkspaceOrderChanged` | `catalog/workspace-order-changed` |
| `ArchivedSessionsChanged` | `catalog/archived-sessions-changed` |

### `POST /workspaces` → `WorkspaceCreateResponse`

```python
@router.post("/workspaces", operation_id="create_workspace")
async def create_workspace(
    request: WorkspaceCreateRequest,
) -> WorkspaceCreateResponse:
```

### `PATCH /workspaces/{workspace_id}` → `WorkspaceResponse`

```python
@router.patch("/workspaces/{workspace_id}", operation_id="rename_workspace")
async def rename_workspace(
    workspace_id: str,
    request: WorkspaceRenameRequest,
) -> WorkspaceResponse:
```

### `DELETE /workspaces/{workspace_id}` → `WorkspaceDeleteResponse`

```python
@router.delete("/workspaces/{workspace_id}", operation_id="delete_workspace")
```

### `POST /workspaces/{workspace_id}/order` → `WorkspaceOrderResponse`

```python
@router.post("/workspaces/{workspace_id}/order", operation_id="reorder_workspace")
```

### `POST /workspaces/{workspace_id}/sessions/{session_id}/order` → `WorkspaceResponse`

```python
@router.post("/workspaces/{workspace_id}/sessions/{session_id}/order", ...)
```

### `PUT /sessions/{session_id}/archive` → `ArchivedSessionsResponse`

```python
@router.put("/sessions/{session_id}/archive", operation_id="archive_session")
```

### `DELETE /sessions/{session_id}/archive` → `ArchivedSessionsResponse`

```python
@router.delete("/sessions/{session_id}/archive", operation_id="unarchive_session")
```

## Wire models

```python
class WorkspaceListResponse(WireModel):
    items: list[WorkspaceView] = Field(default_factory=list)
    archived_session_ids: list[str] = Field(default_factory=list)
    event_cursor: int = Field(default=0, ge=0)

class WorkspaceCreateRequest(WireModel):
    path: str = Field(min_length=1)

class WorkspaceCreateResponse(WireModel):
    workspace: WorkspaceView
    created: bool

class WorkspaceRenameRequest(WireModel):
    title: str = Field(min_length=1, max_length=200)

class WorkspaceResponse(WireModel):
    workspace: WorkspaceView

class WorkspaceOrderRequest(WireModel):
    before_workspace_id: str | None = None

class WorkspaceOrderResponse(WireModel):
    workspace_ids: list[str]

class WorkspaceSessionOrderRequest(WireModel):
    before_session_id: str | None = None

class ArchivedSessionsResponse(WireModel):
    archived_session_ids: list[str] = Field(default_factory=list)
```

## Port Protocols

```python
class WorkspacesPort(Protocol):
    async def list(self) -> WorkspaceListing: ...
    async def create(self, path: str) -> tuple[WorkspaceView, bool]: ...
    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...
    async def delete(self, workspace_id: str) -> bool: ...
    async def insert_before(
        self, workspace_id: str, before_workspace_id: str | None
    ) -> tuple[str, ...]: ...
    async def insert_session_before(
        self, workspace_id: str, session_id: str, before_session_id: str | None
    ) -> WorkspaceView: ...
    async def set_archived(self, session_id: str, archived: bool) -> tuple[str, ...]: ...

class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...
    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...

class DirectoriesPort(Protocol):
    def list(self, path: str | None = None) -> DirectoryListing: ...
```

## Cross-references

- Depends on: `server` (`contribute_router`), `workspaces` (Ports),
  `directories`, `session.events`.
- Depended on by: HTTP workspace clients, TUI workspace views.
- Pairs with: `process-workspaces` (`WorkspacesPort` implementation).

## Common pitfalls

- **SSE cursor expiration**: `workspace_events.subscribe(after)` raises
  `WorkspaceCursorExpired` if `after` is older than the oldest
  retained event. Use `status=409` with `retryable=True` and the
  `oldest_sequence` in details.
- **`WorkspaceRenameRequest.title` max_length=200**: exceeds this
  and Pydantic schema validation fails (400).
- **`insert_session_before` session must be in the workspace**:
  `WorkspaceSessionMoveInvalid` is raised if the session is not
  a child of the target workspace.
- **Archive/unarchive is session-level, not workspace-level**:
  the routes are under `/workspaces/{id}` for ordering but
  `/sessions/{id}/archive` for archiving.
