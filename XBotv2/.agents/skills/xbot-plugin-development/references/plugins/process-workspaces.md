# `process-workspaces`

Process-level workspace registry and event stream for the server/ACP.
Owns the workspace catalog, session ordering within workspaces,
archive/unarchive operations, and workspace event subscription.

- **Import/profile:** `workspaces`, server/ACP profiles.
- **Source:** `XBotv2/workspaces/plugin.py`,
  `XBotv2/workspaces/service.py`,
  `XBotv2/workspaces/events.py`,
  `XBotv2/workspaces/models.py`,
  `XBotv2/workspaces/contracts.py`,
  `XBotv2/workspaces/directories.py`.
- **Injects/provides:** `workspace_root`, `runtime_log` → `workspaces`
  (`WorkspaceManager`).
- **Emits events:** `workspace/resource-changed`
  (`WorkspaceResourceChanged`), `workspace/resource-removed`
  (`WorkspaceResourceRemoved`), `workspace/order-changed`
  (`WorkspaceOrderChanged`), `archived-sessions-changed`
  (`ArchivedSessionsChanged`).

## Public data models

### `WorkspacesPort` consumer Protocol

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
    async def attach_session(self, session_id: str, workspace_root: str) -> None: ...
    async def detach_session(self, session_id: str) -> None: ...
```

### `WorkspaceEventsPort` consumer Protocol

```python
class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...
    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...

class WorkspaceEventSubscription(Protocol):
    def __aiter__(self) -> "WorkspaceEventSubscription": ...
    async def __anext__(self) -> WorkspaceEventFrame: ...
    async def aclose(self) -> None: ...
```

### `DirectoriesPort` consumer Protocol

```python
class DirectoriesPort(Protocol):
    def list(self, path: str | None = None) -> DirectoryListing: ...
```

### `WorkspaceEventFrame` / Change types

```python
@dataclass(frozen=True, slots=True)
class WorkspaceEventFrame:
    sequence: int
    change: WorkspaceChange

# WorkspaceChange is a union of:
# - SessionResourceChanged
# - SessionResourceRemoved
# - WorkspaceResourceChanged
# - WorkspaceResourceRemoved
# - WorkspaceOrderChanged
# - ArchivedSessionsChanged
```

### `DirectoryListing` / `DirectoryNotFound` / `DirectoryNotReadable`

```python
@dataclass(frozen=True, slots=True)
class DirectoryListing:
    entries: tuple[DirectoryEntry, ...]

@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    kind: Literal["file", "directory"]

class DirectoryNotFound(ValueError): ...
class DirectoryNotReadable(ValueError): ...
```

### `WorkspaceNotFound` / `WorkspaceSessionMoveInvalid` / `WorkspaceSessionNotFound`

```python
class WorkspaceNotFound(LookupError): ...
class WorkspaceSessionMoveInvalid(ValueError): ...
class WorkspaceSessionNotFound(LookupError): ...
```

> Note: `WorkspaceSessionMoveInvalid` and `WorkspaceSessionNotFound` are
> defined in `XBotv2.workspaces.service`. `WorkspaceNotFound` is also
> in `service.py` and is a `LookupError` (not `ValueError`).

## `WorkspaceRegistry` (`XBotv2/workspaces/service.py`)

```python
class WorkspaceRegistry:
    def __init__(
        self,
        state: StateService,
        sessions: SessionListing,
        events: ResourceEvents,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None: ...

    async def list(self) -> WorkspaceListing: ...
    async def create(self, path: Path | str) -> tuple[WorkspaceView, bool]: ...
    async def ensure(self, path: Path | str) -> bool: ...
    async def attach_session(
        self, session_id: str, workspace_root: Path | str
    ) -> None: ...
    async def detach_session(self, session_id: str) -> None: ...
    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...
    async def delete(self, workspace_id: str) -> bool: ...
    async def insert_before(
        self, workspace_id: str, before_workspace_id: str | None
    ) -> tuple[str, ...]: ...
    async def insert_session_before(
        self, workspace_id: str, session_id: str,
        before_session_id: str | None
    ) -> WorkspaceView: ...
```

## `WorkspaceEventStream` (`XBotv2/workspaces/events.py`)

```python
class WorkspaceEventStream:
    def __init__(self) -> None:
        self._sequence: int = 0
        self._subscribers: dict[int, WorkspaceEventSubscription] = {}

    @property
    def sequence(self) -> int: ...

    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...

    async def publish(self, change: WorkspaceChange) -> None: ...
```

`WorkspaceEventStream` owns the sequence counter and subscriber list.
`subscribe()` returns a `WorkspaceEventSubscription` that can be
`__anext__`-ed to receive frames, or `aclose()`-ed to unsubscribe.

## How `apply()` works (`XBotv2/workspaces/plugin.py`)

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    stream = WorkspaceEventStream()
    registry = WorkspaceRegistry(
        ctx.state.namespace("workspaces"),
        ctx.sessions,
        ctx,
        ctx.runtime_log,
    )
    await registry.ensure(ctx.workspace_root)
    ctx.on(SESSION_RESOURCE_CHANGED, handlers.changed)
    ctx.on(SESSION_RESOURCE_REMOVED, handlers.removed)
    ctx.on(WORKSPACE_RESOURCE_CHANGED, catalog.publish)
    ctx.on(WORKSPACE_RESOURCE_REMOVED, catalog.publish)
    ctx.on(WORKSPACE_ORDER_CHANGED, catalog.publish)
    ctx.on(ARCHIVED_SESSIONS_CHANGED, catalog.publish)
    ctx.set("workspaces", registry)
    ctx.set("workspace_events", stream)
    ctx.set("workspace_directories", DirectoryBrowser(ctx.workspace_root))
```

Three services exposed: `workspaces` (`WorkspaceRegistry`),
`workspace_events` (`WorkspaceEventStream`),
`workspace_directories` (`DirectoryBrowser`).

## On-disk artifacts

Workspace catalog is persisted via the XCore `StateService` under
the `"workspaces"` namespace — typically at
`<data_dir>/state/workspaces/state.json`. The format is a JSON object
with `items` (workspace records) and `archived_session_ids` (set).

## Cross-references

- Depends on: `workspace_root`, `runtime_log`, `session.events`
  (session resource events feed into workspace catalog).
- Depended on by: `server-routes-workspaces` (HTTP routes),
  `process-sessions` (workspace assignment).
- Pairs with: `process-sessions` (session lifecycle),
  `server-routes-workspaces` (HTTP exposure).

## Common pitfalls

- **`WorkspaceNotFound` is a `LookupError`**: it's raised by
  `list()`/`insert_before()` when a workspace ID doesn't exist.
  Route plugins should catch it and return `HttpServerError(status=404)`.
- **`insert_session_before` requires the session to be in the workspace**:
  raises `WorkspaceSessionMoveInvalid` if the session is not a
  child of the target workspace.
- **`delete()` returns bool, not raises**: returns `False` if the
  workspace doesn't exist. Route plugins should check the return
  value and raise `HttpServerError("workspace_not_found", status=404)`.
- **`directories.list()` raises, doesn't return error**: `DirectoryNotFound`
  and `DirectoryNotReadable` are ValueError subclasses. Route
  plugins should wrap in try/except.
- **Archiving is session-level, not workspace-level**:
  `detach_session()` removes sessions from workspace membership
  and updates archived state. The workspace catalog is updated to
  reflect these changes.
