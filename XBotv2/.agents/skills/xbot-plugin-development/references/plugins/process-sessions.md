# `process-sessions`

The process-wide session manager used by HTTP and ACP carriers. It is a
facet of the root `session` plugin, not a separate plugin tree entry. It owns
live `SessionRuntime` instances and exposes the transport-neutral
`SessionsPort` consumed by routers.

- **Import/profile:** `session`, process/server profile.
- **Source:** `XBotv2/session/manager.py`, `XBotv2/session/contracts.py`,
  `XBotv2/session/runtime.py`, `XBotv2/session/plugin.py`.
- **Injects/provides:** `runtime_paths`, `agent_application_factory`,
  `workspace_root`, `runtime_log` (required) and `thread_persistence_factory`
  (optional) → `sessions` (`SessionManager`).

## Composition

```text
runtime_paths + agent_application_factory + workspace_root + runtime_log
    (+ optional thread_persistence_factory)
        -> session plugin -> sessions: SessionsPort
```

`_MANAGER_DEPENDENCIES` lists exactly one optional dependency,
`thread_persistence_factory`. When it is absent the manager still constructs:
`_thread_persistence()` raises `OperationError("persistence_unavailable", ...)`
only when a route actually asks for persisted state.

The manager stores live runtimes in a process-local map keyed by
`(session_id, thread_id)` (`self._sessions: dict[tuple[str, str], SessionRuntime]`),
with a second `_opening` map of in-flight build tasks. Persisted sessions are
opened through the typed persistence factory; route code never reconstructs
the session directory.

## Public port

The authoritative protocol is `XBotv2.session.contracts.SessionsPort`:

```python
class SessionsPort(Protocol):
    def session_exists(self, session_id: str) -> bool: ...
    async def open(self, request: OpenSession) -> OpenedThread: ...
    async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    async def session_summary(self, session_id: str) -> SessionSummary: ...
    async def rename_session(self, session_id: str, title: str) -> SessionSummary: ...
    async def fork_session(self, session_id: str) -> str: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def list_threads(self, session_id: str) -> tuple[ThreadSummary, ...]: ...
    async def open_thread(self, request: OpenThread) -> OpenedThread: ...
    async def thread_summary(self, session_id: str, thread_id: str) -> ThreadSummary: ...
    async def messages(self, session_id: str, thread_id: str) -> tuple[ConversationMessage, ...]: ...
    async def message_page(
        self, session_id: str, thread_id: str, *,
        cursor: Cursor | None, limit: int | None,
    ) -> HistoryPage[ConversationMessage]: ...
    async def trajectory_page(
        self, session_id: str, thread_id: str, *,
        cursor: Cursor | None, limit: int, before: int | None = None,
    ) -> TrajectoryRead: ...
    async def artifact(self, session_id: str, thread_id: str, artifact_id: str) -> ArtifactPayload: ...
    async def clear_history(
        self, session_id: str, thread_id: str, *, history_limit: int | None,
    ) -> HistoryMutation: ...
    async def undo_history(
        self, session_id: str, thread_id: str, count: int, *,
        history_limit: int | None,
    ) -> HistoryMutation: ...
    async def send_message(self, request: SendMessage) -> None: ...
    async def pending_inputs(self, session_id: str, thread_id: str) -> tuple[PendingInputData, ...]: ...
    async def update_pending_input(self, request: PendingInputUpdate) -> tuple[PendingInputData, ...]: ...
    async def regenerate_message(self, request: RegenerateMessage) -> None: ...
    async def stream_events(
        self, session_id: str, thread_id: str, *, after: int | None = None,
    ) -> SessionEventSubscription: ...
    async def respond_permission(
        self, session_id: str, thread_id: str, request_id: str,
        decision: str, scope: str,
    ) -> InteractionReceipt: ...
    async def respond_user_input(
        self, session_id: str, thread_id: str, request_id: str,
        answer: JsonValue,
    ) -> InteractionReceipt: ...
    async def cancel_interaction(
        self, session_id: str, thread_id: str,
        event_type: Literal["permission_request", "user_input_required"],
        request_id: str, reason: str,
    ) -> InteractionReceipt: ...
    async def close_session(self, session_id: str) -> None: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...
    async def interrupt(self, session_id: str, thread_id: str) -> InterruptResult: ...
    async def dispatch(
        self, session_id: str, thread_id: str,
        operation: Operation[RequestT, ResponseT], request: RequestT,
    ) -> ResponseT: ...
    async def dispatch_all(
        self, session_id: str, operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> tuple[ResponseT, ...]: ...
```

Details that are easy to get wrong:

- `open()` and `open_thread()` both return **`OpenedThread`** (a frozen
  Pydantic model), not an `OpenedSession`. That name does not exist.
- `message_page()` returns `HistoryPage[ConversationMessage]`. There is no
  `ConversationPage` type.
- `clear_history()` and `undo_history()` both take a **keyword-only,
  required** `history_limit: int | None`. There is no default, so a caller
  that omits it gets a `TypeError`, not a full-history fallback.
- `stream_events()` is declared **once**, returning the
  `SessionEventSubscription` protocol (`AsyncIterator[SessionEventFrame]` plus
  `aclose()`), not a bare `AsyncIterator`.
- `respond_user_input()` takes `answer: JsonValue` — a structured answer, not
  a `str`.
- `dispatch()` and `dispatch_all()` are generic in `RequestT`/`ResponseT`;
  the concrete response type is recovered from the `Operation`, not erased to
  `object`.

Import the protocol instead of copying this snippet.

## Domain models

`OpenSession` and `OpenThread` are internal composition commands and are
frozen dataclasses, not HTTP request models:

```python
@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str | None
    mode: SessionMode                      # Literal["new", "resume"]
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
    provider_name: str | None
    mode: SessionMode
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None
```

`provider_name` is **optional** in both commands: it is `str | None`, and
`None` means "resolve the provider from configuration". `OpenSession.session_id`
is also `str | None` (a new session lets the manager generate one), while
`OpenThread.session_id` is required.

The HTTP `OpenSessionRequest`/`OpenThreadRequest` deliberately omit server
composition facts (`provider_name`, `no_plugins`, and provider objects).

`OpenedThread` is the open-result payload:

```python
class OpenedThread(BaseModel):
    key: SessionKey
    metadata: ThreadMetadata
    usage: UsageSnapshot
    status_slots: dict[str, str]
    event_cursor: int
    history: HistoryPage[ConversationRecord]
    pending_inputs: tuple[PendingInputData, ...]
    pending_interactions: tuple[PendingInteraction, ...]
```

`SessionSummary` and `ThreadSummary` are also frozen Pydantic models. Use
`model_json_schema()` when generating a client contract rather than copying
old field lists. `SessionSummary` carries `session_id`, `status`
(`"active"`/`"inactive"`), `active_threads`, `thread_count`, `workspace_root`,
`title`, `blank`, and `unreadable` — it exposes **no** `created_at` or
`updated_at`. `ThreadSummary` carries identity, `status`, `kind`
(`"main"`/`"subagent"`), `turn_status`, `parent_thread_id`, `agent`,
`provider`, `model`, `model_mode`, `context_window`, `message_count`, `usage`,
`session_stats`, `pending_interactions`, `status_slots`, `workspace_root`, and
`title`.

## Lifecycle

```python
class SessionManager(SessionsPort):
    def __init__(
        self, paths: RuntimePaths, events: ResourceEvents, *,
        idle_timeout: float | None = 3600.0, reap_interval: float = 60.0,
        application_factory: AgentApplicationFactory,
        thread_persistence_factory: ThreadPersistenceFactory | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None: ...
    async def open(self, request: OpenSession) -> OpenedThread: ...
    async def open_thread(self, request: OpenThread) -> OpenedThread: ...
    async def close_thread(self, session_id: str, thread_id: str) -> None: ...
    async def close_session(
        self, session_id: str, *, reason: str = "session_closed",
    ) -> None: ...
    async def delete_session(self, session_id: str) -> None: ...
```

`events` is a positional second argument and the two path/events parameters are
**not** keyword-only; everything after them is. `start_reaper()` starts
idle-runtime cleanup (idempotent, guarded by `reap_interval`). `close_all()` is
registered with `ctx.dispose`, so a shutting-down context releases every
runtime.

`clear_history`/`undo_history` call `require_idle(runtime, ...)`
(`session/runtime.py`) before taking `runtime.turn_lock`. `require_idle` raises
a retryable `OperationError("thread_busy", ...)` while a turn is active, so a
history rewrite on a busy thread is refused rather than raced.

Closing a thread stops its runtime and emits the session resource events
consumed by the workspace registry. Use the port and events rather than
reaching into `_sessions`.

## Events

The manager publishes resource facts on its own bus:

```python
PREPARE_FORK = "session/prepare-fork"
HISTORY_CHANGED = "session/history-changed"
SESSION_RESOURCE_CHANGED = "session/resource-changed"
SESSION_RESOURCE_REMOVED = "session/resource-removed"
```

`SESSION_RESOURCE_CHANGED` carries a `SessionResourceChanged`
(`session: SessionSummary`, `added: bool = False`) and is emitted only when the
newly built summary differs from the last published one.
`SESSION_RESOURCE_REMOVED` carries a `SessionResourceRemoved(session_id)`.
`PREPARE_FORK` is emitted **on the runtime's application bus** — not the
manager's — with a `PrepareFork(session_id, thread_id)`, and subscribers may
reject the fork. `HISTORY_CHANGED` carries the dataclass `HistoryChanged`
(`messages`, `operation`, `turns`); it is published by the session runtime and
consumed by the compaction and todolist plugins.

The event stream exposed to HTTP is a separate, cursor-based
`SessionEventFrame(sequence, scope, event)` stream; do not conflate the two.

## Common mistakes

- Do not model the manager as the per-thread `ctx.session` service. `ctx.session`
  is the `SessionPort` handle registered by `SessionRuntimeComponent`;
  `ctx.sessions` is the process manager.
- Do not make `close_session()` synchronous; all lifecycle operations are
  async.
- Do not add an HTTP-only method to `SessionsPort`; add a transport-neutral
  domain operation only when another carrier needs it too.
- Do not bypass `thread_persistence_factory` or `RuntimePaths` to inspect
  session files.
