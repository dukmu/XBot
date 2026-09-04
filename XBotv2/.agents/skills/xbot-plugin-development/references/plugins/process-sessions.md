# `process.sessions`

The process-wide `SessionManager` for server/ACP carriers. It owns the
session directory tree (`RuntimePaths.sessions_dir`), tracks live
runtimes in memory, and exposes a transport-neutral `SessionsPort`
that route plugins (HTTP, ACP) call into.

This is **not** the Agent-profile per-thread `session` service — see
[session.md](session.md) for that one.

- **Import/profile:** import `session.host`, server/ACP profiles
  (tree id is `process.sessions`).
- **Source:** `XBotv2/session/host/plugin.py`,
  `XBotv2/session/manager.py`, `XBotv2/session/services.py`,
  `XBotv2/session/contracts.py`, `XBotv2/session/types.py`.
- **Injects/provides:** `thread_persistence_factory`,
  `runtime_paths`, `agent_application_factory`, `workspace_root`,
  `runtime_log` → `sessions` (process `SessionManager`).
- **Emits events:** `session/resource-changed`
  (`SessionResourceChanged`), `session/resource-removed`
  (`SessionResourceRemoved`), `session/prepare-fork` (`PrepareFork`),
  `session/history-changed` (`HistoryChanged`).

## Public data models

### `SessionInfo` (per-thread mutable identity)

```python
@dataclass
class SessionInfo:
    session_id: str                          # YYYYMMDD-HHMMSS-<4hex>
    thread_id: str                           # default "agent"
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"
```

### `OpenSession` / `OpenedSession` / `OpenThread`

```python
@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str
    mode: SessionMode                        # "new" | "resume"
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None
    plugin_configs: dict[str, dict[str, JsonValue]] | None = None

@dataclass(frozen=True, slots=True)
class OpenThread:
    session_id: str
    thread_id: str
    parent_thread_id: str
    workspace_root: str | None
    provider_name: str
```

`OpenedSession` adds the resolved `session_id` plus paths and the
runtime handle.

### `SessionSummary` / `ThreadSummary`

```python
@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    workspace_root: str
    created_at: datetime
    updated_at: datetime
    active_thread_id: str
    thread_ids: tuple[str, ...]
    archived: bool

@dataclass(frozen=True, slots=True)
class ThreadSummary:
    thread_id: str
    parent_thread_id: str
    session_id: str
    workspace_root: str
    created_at: datetime
    updated_at: datetime
    archived: bool
    message_count: int
    turn_count: int
```

### `SendMessage` / `PendingInputData` / `PendingInputUpdate` / `RegenerateMessage`

All in `session/types.py`. `SendMessage` is the streaming input shape:
`session_id`, `thread_id`, `content`, optional `images` /
`artifacts` / `model_override` / `plugin_configs`.

### Resource events (emitted by the manager)

```python
@dataclass(frozen=True, slots=True)
class PrepareFork:
    session_id: str
    thread_id: str

@dataclass(frozen=True, slots=True)
class HistoryChanged:
    messages: tuple[Message, ...]
    operation: str
    turns: int = 0

@dataclass(frozen=True, slots=True)
class SessionResourceChanged:
    session: SessionSummary
    added: bool = False

@dataclass(frozen=True, slots=True)
class SessionResourceRemoved:
    session_id: str

@dataclass(frozen=True, slots=True)
class SessionStatus:
    session_id: str
    thread_id: str
    provider: str
    model: str
```

Event names: `PREPARE_FORK = "session/prepare-fork"`,
`HISTORY_CHANGED = "session/history-changed"`,
`SESSION_RESOURCE_CHANGED = "session/resource-changed"`,
`SESSION_RESOURCE_REMOVED = "session/resource-removed"`.

## `SessionsPort` consumer Protocol

```python
class SessionPort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str

    @property
    def provider(self) -> str: ...
    def new_thread_id(self, owner: str) -> str: ...
    def status(self) -> SessionStatus: ...
    async def fork(self) -> str: ...
    async def clear_history(self) -> int: ...
    async def undo_history(self, count: int) -> list[Message]: ...
    async def regenerate_history(self) -> Message: ...


class SessionsPort(Protocol):
    """Transport-neutral process API for persistent sessions and threads."""

    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> OpenedSession: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(
        self, session_id: str, title: str
    ) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(
        self, session_id: str
    ) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedSession: ...
    async def thread_summary(
        self, session_id: str, thread_id: str
    ) -> ThreadSummary: ...
    async def messages(
        self, session_id: str, thread_id: str
    ) -> tuple[Message, ...]: ...
    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> ConversationPage: ...
    async def artifact(
        self,
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> ArtifactPayload: ...
    async def clear_history(
        self, session_id: str, thread_id: str
    ) -> HistoryMutation: ...
    async def undo_history(
        self, session_id: str, thread_id: str, count: int
    ) -> HistoryMutation: ...
    async def stream_message(
        self, request: SendMessage
    ) -> AsyncIterator[ClientEvent]: ...
    async def pending_inputs(
        self, session_id: str, thread_id: str
    ) -> tuple[PendingInputData, ...]: ...
    async def update_pending_input(
        self, request: PendingInputUpdate
    ) -> tuple[PendingInputData, ...]: ...
    async def regenerate_message(
        self, request: RegenerateMessage
    ) -> AsyncIterator[ClientEvent]: ...
    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> AsyncIterator[SessionEventFrame]: ...
    async def respond_permission(
        self, session_id, thread_id, request_id, decision, scope
    ) -> InteractionReceipt: ...
    async def respond_user_input(
        self, session_id, thread_id, request_id, answer
    ) -> InteractionReceipt: ...
    async def cancel_interaction(
        self, session_id, thread_id, event_type, request_id, reason
    ) -> InteractionReceipt: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close_thread(
        self, session_id: str, thread_id: str
    ) -> None: ...
    async def interrupt(
        self, session_id: str, thread_id: str
    ) -> InterruptResult: ...
    async def dispatch(
        self, session_id, thread_id, operation, request
    ) -> ResponseT: ...
    async def dispatch_all(
        self, session_id, operation, request
    ) -> tuple[ResponseT, ...]: ...
```

## `SessionManager` (`session/manager.py:105-...`)

Tracks live runtimes in `self._sessions: dict[(session_id, thread_id), SessionRuntime]`
and emits resource events. Lifecycle methods:

```python
class SessionManager:
    def start_reaper(self) -> None: ...           # background idle close

    async def get(
        self, session_id: str, thread_id: str
    ) -> SessionRuntime: ...

    async def open_session(self, ...) -> OpenedSession: ...
    async def open(self, request: OpenSession) -> OpenedSession: ...

    async def close_thread(
        self, session_id: str, thread_id: str
    ) -> None: ...
    async def close_session(
        self, session_id: str, *, reason: str = "session_closed"
    ) -> None: ...
    async def delete_session(self, session_id: str) -> None: ...

    async def active_threads(
        self
    ) -> dict[tuple[str, str], SessionRuntime]: ...

    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(
        self, session_id: str
    ) -> SessionSummary: ...
    async def rename_session(
        self, session_id: str, title: str
    ) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def list_threads(
        self, session_id: str
    ) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedSession: ...
    async def thread_summary(
        self, session_id: str, thread_id: str
    ) -> ThreadSummary: ...

    async def dispatch(
        self, session_id, thread_id, operation, request
    ) -> ResponseT: ...
    async def dispatch_all(
        self, session_id, operation, request
    ) -> tuple[ResponseT, ...]: ...

    def close_all(self) -> None: ...               # ctx.dispose disposer
```

A session is removed from `self._sessions` *before* the engine closes;
`SESSION_CLOSE` then fires on the runtime's per-loop context. Server
plugins listening to `session/resource-removed` see the post-close
state, not the close itself.

## On-disk layout

Same `RuntimePaths.sessions_dir` layout as [session.md](session.md):

```text
<data_dir>/sessions/<session_id>/threads/<thread_id>/...
```

See [../session-trace.md](../session-trace.md) for the JSONL record
schema.

## Typical extension: route plugin

```python
from fastapi import APIRouter
from XBotv2.commands.contracts import LIST_COMMANDS
from XBotv2.core.operations import EmptyRequest

def build_commands_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions/{sid}/threads/{tid}/commands")
    async def list_cmds(sid: str, tid: str):
        catalog = await sessions.dispatch(
            sid, tid, LIST_COMMANDS, EmptyRequest()
        )
        return {"commands": [c.model_dump() for c in catalog.commands]}

    return router
```

Routes never touch the file system directly; they call into
`SessionsPort`.

## Cross-references

- Depends on: `thread_persistence_factory`, `runtime_paths`,
  `agent_application_factory`, `workspace_root`, `runtime_log`.
- Depended on by: every `server.routes.*` plugin that exposes a
  session/workspace route, `acp-plugin`, `workspaces`.
- Pairs with: [session.md](session.md) (per-thread Agent session),
  [process-workspaces.md](process-workspaces.md) (workspace catalog).

## Common pitfalls

- **Calling `SessionManager.delete_session(...)` while a thread has
  an active turn**: `delete_session` raises `OperationError(
  "thread_busy", retryable=True)`. Close the thread first or wait
  for the turn lock.
- **Reading the session directory from a route plugin**: every read
  goes through `SessionsPort`. Direct file access desyncs from the
  manager's in-memory state.
- **Listening to `Events.SESSION_CLOSE` for process-wide close
  notification**: the event fires on the per-thread loop context, not
  the server context. Use `session/resource-changed` (with
  `added=False`) or `session/resource-removed` for cross-session
  visibility.
- **Importing `SessionManager` from a route plugin**: depend on
  `SessionsPort` (Protocol). The implementation is per-process and
  carrier-specific.
- **Reusing the same `session_id` after `delete_session` without
  waiting for `resource-removed`**: `SessionExists` will be raised by
  `open_thread`. Wait for the resource-removed event before reopening.
