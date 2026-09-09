# `process-persistence`

Process-level persistence facet — the root persistence plugin provides the
typed reader factory used by server/ACP session management. The same root
plugin also hydrates an active Agent thread when its thread-local dependencies
become available; there is no second host/process plugin.

- **Import/profile:** tree id/import name `persistence`, server/ACP profiles.
- **Source:** `XBotv2/persistence/plugin.py`,
  `XBotv2/persistence/contracts.py`,
  `XBotv2/persistence/store.py`.
- **Injects/provides:** (none) → `thread_persistence_factory` (Callable).
- **Subscribes to events:** none.
- **API:** `thread_persistence_factory()` — constructs a
  `ThreadPersistence` reader for a persisted thread.

## Public data models

### `thread_persistence_factory` (`XBotv2/persistence/plugin.py`)

```python
def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str = "",
    workspace_root: str = "",
    provider: str = "",
) -> ThreadPersistence:
    """Construct a :class:`ThreadPersistence` reader for a persisted thread.

    ``session_paths`` is a ``SessionPaths`` object from
    ``RuntimePaths.session(session_id)``.
    """
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider=provider,
    )
```

### Root plugin composition (`XBotv2/persistence/plugin.py`)

```python
class PersistencePlugin:
    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("thread_persistence_factory", thread_persistence_factory)
        ctx.inject(ThreadPersistenceComponent.inject, mount_thread_persistence)
```

The root plugin sets `thread_persistence_factory` on every selected profile.
Agent applications receive `thread_persistence` (a single
`ThreadPersistence` instance for the active thread) from
`persistence/plugin.py`, not from this host.

## Port Protocols (`XBotv2/persistence/contracts.py`)

### `ThreadPersistencePort`

```python
class ThreadPersistencePort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str
    provider: str
    history: HistoryPort
    state: StatePort
    artifacts: ArtifactStorePort
    metadata: MetadataPort
    inbox: InboxPort
    lifecycle: ThreadLifecyclePort

    def has_persisted_state(self) -> bool: ...
```

### `ThreadPersistenceFactory`

```python
class ThreadPersistenceFactory(Protocol):
    def __call__(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str = "",
        provider: str = "",
    ) -> ThreadPersistencePort: ...
```

### `HistoryPort`

```python
class HistoryPort(Protocol):
    def load(self) -> list[Message]: ...
    def load_surface(self) -> tuple[HistoryNode, ...]: ...
    def load_transcript(self) -> list[Message]: ...
    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]: ...
    def replace(self, messages: Sequence[Message]) -> None: ...
    def replace_surface(
        self,
        source_node_ids: Sequence[str],
        messages: Sequence[Message],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[HistoryNode, ...]: ...
    def record(self, event: str, data: dict[str, object]) -> None: ...
    def count(self) -> int: ...
    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage: ...
    def page_transcript(self, *, limit: int, cursor: str | None = None) -> ConversationPage: ...
```

### `MetadataPort`

```python
class MetadataPort(Protocol):
    def load(self) -> ThreadMetadata: ...
    def save(self, metadata: ThreadMetadata) -> None: ...
```

### `InboxPort`

```python
class InboxPort(Protocol):
    def load(self) -> list[InboxInput]: ...
    def replace(self, items: Sequence[InboxInput]) -> None: ...
    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]: ...
```

### `StatePort`

```python
class StatePort(Protocol):
    async def get(self, key: str, default: object | None = None) -> object: ...
    async def set(self, key: str, value: object) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def clear(self) -> None: ...
    def namespace(self, prefix: str) -> "StatePort": ...
```

### `ThreadLifecyclePort` / `ThreadLifecycleWriterPort`

```python
class ThreadLifecyclePort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...
    def load(self) -> list[ThreadLifecycleRecord]: ...

class ThreadLifecycleWriterPort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...
```

## `ThreadPersistence` (`XBotv2/persistence/store.py`)

The concrete implementation opened by `thread_persistence_factory()`:

```python
class ThreadPersistence:
    session_id: str
    thread_id: str
    workspace_root: str
    provider: str
    history: ConversationHistory
    inbox: InboxStore
    metadata: ThreadMetadata
    artifacts: ArtifactStore

    @classmethod
    def open(
        cls,
        session_paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str,
        provider: str,
    ) -> "ThreadPersistence": ...

    def has_persisted_state(self) -> bool: ...
```

`open()` reads `thread.json` for identity, `messages.jsonl` for
history, `inbox.json` for inbox, `plugin_state/` for state, and
`artifacts/` for artifact store.

## On-disk layout

Same `RuntimePaths` layout as `process-sessions.md`:

```text
<data_dir>/sessions/<session_id>/threads/<thread_id>/
├── thread.json
└── state/
    ├── messages.jsonl
    ├── inbox.json
    ├── plugin_state/state.json
    └── artifacts/<kind>/...
```

## How `apply()` works

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    ctx.set("thread_persistence_factory", thread_persistence_factory)
    ctx.inject(ThreadPersistenceComponent.inject, mount_thread_persistence)
```

The process facet is the factory setter. The dependency-gated Agent facet
hydrates `loop_state` from `thread_persistence`, restores history, inbox and
metadata, and exposes `thread_metadata`. Both are owned by the same root plugin.

## Cross-references

- Depends on: `runtime_paths` (`SessionPaths`), `persistence` (`ThreadPersistence`).
- Depended on by: `process-sessions` (session manager uses the factory
  to create `ThreadPersistence` for each new thread).
- Pairs with: `persistence` (Agent-profile hydrator), `process-sessions`
  (session lifecycle).

## Common pitfalls

- **Do not create a second persistence host plugin**: process factory and
  Agent hydration are dependency-gated facets of `persistence/plugin.py`.
- **Never open session files directly from a route plugin**: use
  `SessionsPort` for all session reads. The factory is only for
  process-level metadata operations (listing sessions, reading
  thread summaries).
- **`session_paths` must be from `RuntimePaths.session()`**: passing
  a raw `SessionPaths` constructed by hand will desync from the
  actual on-disk layout.
