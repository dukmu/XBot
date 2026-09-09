# `process-sessions`

The process-wide session manager used by HTTP and ACP carriers. It is a
facet of the root `session` plugin, not a separate plugin tree entry. It owns
live `SessionRuntime` instances and exposes the transport-neutral
`SessionsPort` consumed by routers.

Source: `XBotv2/session/plugin.py`, `XBotv2/session/manager.py`,
`XBotv2/session/contracts.py`.

## Composition

```text
runtime_paths + thread_persistence_factory + agent_application_factory
    + workspace_root + runtime_log
        -> session plugin -> sessions: SessionsPort
```

The manager stores live runtimes in a process-local map keyed by
`(session_id, thread_id)`. Persisted sessions are opened through the typed
persistence factory; route code never reconstructs the session directory.

## Public port

The authoritative protocol is `XBotv2.session.contracts.SessionsPort`:

```python
class SessionsPort(Protocol):
    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> OpenedSession: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(self, session_id: str, title: str) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedSession: ...
    async def thread_summary(self, session_id: str, thread_id: str) -> ThreadSummary: ...
    async def message_page(self, session_id: str, thread_id: str, *, cursor: str | None, limit: int | None) -> ConversationPage: ...
    async def artifact(self, session_id: str, thread_id: str, artifact_id: str) -> ArtifactPayload: ...
    async def clear_history(self, session_id: str, thread_id: str) -> HistoryMutation: ...
    async def undo_history(self, session_id: str, thread_id: str, count: int) -> HistoryMutation: ...
    async def stream_message(self, request: SendMessage) -> AsyncIterator[ClientEvent]: ...
    async def pending_inputs(self, session_id: str, thread_id: str) -> tuple[PendingInputData, ...]: ...
    async def update_pending_input(self, request: PendingInputUpdate) -> tuple[PendingInputData, ...]: ...
    async def regenerate_message(self, request: RegenerateMessage) -> AsyncIterator[ClientEvent]: ...
    async def stream_events(self, session_id: str, thread_id: str, *, after: int | None = None) -> AsyncIterator[SessionEventFrame]: ...
    async def respond_permission(self, session_id: str, thread_id: str, request_id: str, decision: str, scope: str) -> InteractionReceipt: ...
    async def respond_user_input(self, session_id: str, thread_id: str, request_id: str, answer: str) -> InteractionReceipt: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...
    async def interrupt(self, session_id: str, thread_id: str) -> InterruptResult: ...
    async def dispatch(self, session_id: str, thread_id: str, operation: Operation[object, object], request: object) -> object: ...
```

The port also has `messages()` and `dispatch_all()` in the current source;
the signatures above show the routes' core surface, not a replacement for
the source contract. Import the protocol instead of copying this snippet.

## Domain models

`OpenSession` and `OpenThread` are internal composition commands and are
dataclasses, not HTTP request models:

```python
@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str
    mode: Literal["new", "resume"]
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
    mode: Literal["new", "resume"]
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None
```

The HTTP `OpenSessionRequest`/`OpenThreadRequest` deliberately omit server
composition facts (`provider_name`, `no_plugins`, and provider objects).

`SessionSummary` and `ThreadSummary` are frozen Pydantic models. Their
current wire shape is defined in `session/contracts.py`; use
`model_json_schema()` when generating a client contract rather than copying
old timestamp/message-count examples. In particular, the current summaries
contain status, thread/model/usage fields and do not expose the old
`created_at`/`updated_at` shape.

## Lifecycle

```python
class SessionManager:
    async def open(self, request: OpenSession) -> OpenedSession: ...
    async def open_thread(self, request: OpenThread) -> OpenedSession: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...
    async def close_session(self, session_id: str, *, reason: str = "session_closed") -> None: ...
    async def delete_session(self, session_id: str) -> None: ...
```

`start_reaper()` starts idle-runtime cleanup. Closing a thread stops its
runtime and emits the session resource events consumed by the workspace
registry. Use the port and events rather than reaching into `_sessions`.

## Events

The manager emits typed `SessionResourceChanged`,
`SessionResourceRemoved`, `PrepareFork`, and `HistoryChanged` events through
the session event contract. The event stream exposed to HTTP is a separate,
cursor-based `SessionEventFrame` stream.

## Common mistakes

- Do not model the manager as the per-thread `ctx.session` service.
- Do not make `close_session()` synchronous; all lifecycle operations are
  async.
- Do not add an HTTP-only method to `SessionsPort`; add a transport-neutral
  domain operation only when another carrier needs it too.
- Do not bypass `thread_persistence_factory` or `RuntimePaths` to inspect
  session files.
