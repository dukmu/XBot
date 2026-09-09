# `process-workspaces`

Process-level workspace registry, directory browser, ordering, archive
state, and event cursor. It is provided by the root `workspaces` plugin for
server/ACP profiles.

Source: `XBotv2/workspaces/plugin.py`, `service.py`, `events.py`,
`contracts.py`, and `directories.py`.

## Services

```text
workspace_root + runtime_log + state + session resource events
    -> workspaces: WorkspacesPort
    -> workspace_events: WorkspaceEventsPort
    -> workspace_directories: DirectoriesPort
```

The public contracts are in `XBotv2/workspaces/contracts.py`:

```python
class WorkspacesPort(Protocol):
    async def list(self) -> WorkspaceListing: ...
    async def create(self, path: str) -> tuple[WorkspaceView, bool]: ...
    async def rename(self, workspace_id: str, title: str) -> WorkspaceView: ...
    async def delete(self, workspace_id: str) -> bool: ...
    async def insert_before(self, workspace_id: str, before_workspace_id: str | None) -> tuple[str, ...]: ...

class WorkspaceEventsPort(Protocol):
    @property
    def sequence(self) -> int: ...
    def subscribe(self, after: int) -> WorkspaceEventSubscription: ...

class DirectoriesPort(Protocol):
    def list(self, path: str | None = None) -> DirectoryListing: ...
```

`insert_session_before` and `set_archived` are implemented by the workspace
registry and are exposed through the current registry/event composition; use
the exact protocol declarations in source when calling those operations.

## Models

```python
class WorkspaceView(BaseModel):
    workspace_id: str
    path: str
    title: str
    session_ids: tuple[str, ...] = ()
    created_at: str
    updated_at: str

class WorkspaceListing(BaseModel):
    items: tuple[WorkspaceView, ...] = ()
    archived_session_ids: tuple[str, ...] = ()

class DirectoryEntry(BaseModel):
    name: str
    path: str
    hidden: bool

class DirectoryListing(BaseModel):
    path: str
    parent: str | None
    home: str
    separator: Literal["/", "\\"]
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
```

All models are frozen Pydantic models with `extra="forbid"`. `WorkspaceView`
uses `workspace_id`; `DirectoryEntry` is not a `kind`-only model.

## Events

The event stream uses `WorkspaceEventFrame(sequence, change)`. Changes are
`WorkspaceResourceChanged`, `WorkspaceResourceRemoved`,
`WorkspaceOrderChanged`, and `ArchivedSessionsChanged`, plus session resource
changes forwarded from the session manager. `WorkspaceEventStream.publish()`
is asynchronous; `subscribe()` is synchronous and returns an async iterator.

## Persistence and ownership

Workspace records are persisted through the XCore `StateService` namespace
owned by the registry. A route or plugin must not write the catalog file
directly. Directory listing is a separate read-only service and does not
mutate workspace state.

Related reference: [server-routes-workspaces.md](server-routes-workspaces.md).
