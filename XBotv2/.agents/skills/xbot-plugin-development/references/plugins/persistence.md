# `persistence`

Owns the durable conversation state for one thread: the append-only
trajectory in `messages.jsonl`, the inbox projection, typed metadata,
and the artifact store. The plugin hydrates the loop state from disk
on startup; the loop itself pushes subsequent writes through the
contracts published here.

- **Import/profile:** `persistence`, Agent profile.
- **Source:** `XBotv2/persistence/plugin.py`,
  `XBotv2/persistence/store.py`, `XBotv2/persistence/models.py`,
  `XBotv2/persistence/contracts.py`,
  `XBotv2/core/filesystem/artifacts.py`.
- **Injects/provides:** `loop_state`, `thread_persistence`,
  `runtime_log` → `thread_metadata` (`ThreadMetadataState`).
- **Subscribes to events:** none in `apply`; the loop engine pushes
  history writes directly through `ThreadPersistence.history` rather
  than via the `state/changed` observer event.
- **Server counterpart:** `process.persistence` (server/ACP) hosts
  the persistence factory.

## Public data models

### `ThreadPersistence` store (`XBotv2/persistence/store.py`)

```python
class ThreadPersistence:
    session_id: str
    thread_id: str
    workspace_root: str
    provider: str
    history: MessageHistoryStore
    inbox: InboxStore
    metadata: MetadataPort
    artifacts: ArtifactStorePort
    lifecycle: ThreadLifecyclePort
    state: StatePort

    @classmethod
    def open(
        cls,
        session_paths: SessionPaths,
        *,
        thread_id: str = "",
        workspace_root: str = "",
        provider: str = "",
    ) -> "ThreadPersistence": ...

    def has_persisted_state(self) -> bool: ...
```

`ThreadPersistence` wraps the per-domain stores (`MessageHistoryStore`,
`InboxStore`, `ArtifactStore`, `MetadataPort`, `StatePort`) into a
single facade. The `open()` classmethod reads `thread.json` from the
`SessionPaths` directory to populate identity fields.

### `MessageHistoryStore` (`XBotv2/persistence/store.py`)

```python
class MessageHistoryStore:
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
    def record(self, event: str, data: JsonValue) -> None: ...
    def count(self) -> int: ...
    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage: ...
    def page_transcript(
        self, *, limit: int, cursor: str | None = None
    ) -> ConversationPage: ...
```

`load_surface()` returns `tuple[HistoryNode, ...]` (not `list`).
`HistoryNode` carries the original `Message` plus its typed `position`.

### `InboxStore` / `InboxSnapshot` (`XBotv2/persistence/store.py`)

```python
@dataclass(frozen=True, slots=True)
class InboxSnapshot:
    items: tuple[InboxItem, ...]
    version: int

@dataclass(frozen=True, slots=True)
class InboxItem:
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
    source: str
    images: tuple[ImageInput, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)

class InboxStore:
    def load(self) -> list[InboxInput]: ...
    def replace(self, items: Sequence[InboxInput]) -> None: ...
    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]: ...
```

`InboxItem` is the record type; the loop consumes `InboxInput`
(defined in `XBotv2.agentloop.inbox`). Both share the same on-disk
format.

### `ArtifactStore` (`XBotv2/core/filesystem/artifacts.py`)

The artifact store lives in `XBotv2.core.filesystem.artifacts`:

```python
@dataclass(frozen=True, slots=True)
class ArtifactRef(BaseModel):
    kind: str
    uri: str
    digest: str
    size: int
    metadata: dict[str, JsonValue] = field(default_factory=dict)

class ArtifactStore:
    def put(
        self, kind: str, data: bytes | str, *, name: str | None = None
    ) -> ArtifactRef: ...
    def open(self, ref: ArtifactRef) -> BinaryIO: ...
    def path_for(self, ref: ArtifactRef) -> Path: ...
```

The store owns `<thread>/state/artifacts/<kind>/...`; never construct
this path yourself.

### Trajectory records (`persistence/models.py`)

Every record carries `schema_version: 1` and a contiguous `position`.

```python
# MessageRecord — one accepted provider-neutral Message
{
    "schema_version": 1,
    "role": "user",                    # or "assistant" | "tool" | "system"
    "parts": [...],                    # discriminated union of ContentPart
    "status": "",
    "data": None,
    "tool_call_id": "",
    "input_id": "",
    "name": "",
    "additional_kwargs": {},
    "response_metadata": {},
    "usage_metadata": {},
    "artifact": [],
    "error": null,
    "position": 1,
}

# SurfaceReplaceRecord — undo / clear / regenerate / compact
{
    "schema_version": 1,
    "record_type": "surface_replace",
    "transcript": "replace" | "preserve",
    "target_node_ids": [...],
    "replace_node_ids": [...],
    "position": 2,
}

# TrajectoryEventRecord — log-only plugin/runtime fact
{
    "schema_version": 1,
    "record_type": "event",
    "data": {...},
    "name": null,
    "position": 3,
}
```

`parts` preserves `text`, `reasoning`, `image`, `tool_call`. Missing
optional metadata falls back to model defaults — do not assume a legacy
`content` field.

## What `apply()` does (`persistence/plugin.py:18-50`)

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    state = ctx.loop_state
    persistence = ctx.thread_persistence
    nodes = persistence.history.load_surface()
    messages = [node.message for node in nodes]
    committed_input_ids = {
        message.input_id for message in messages if message.input_id
    }
    pending_inputs = persistence.inbox.reconcile(committed_input_ids)
    state.set_history(ConversationHistory(sink=persistence.history, nodes=nodes))
    state.resumed = persistence.has_persisted_state()
    state.metadata = ThreadMetadataState(
        persistence.metadata.load(), sink=persistence.metadata
    )
    state.inbox_items = pending_inputs
    state.inbox_sink = persistence.inbox
    state.session.provider = persistence.provider
    ctx.set("thread_metadata", state.metadata)
```

It does not register event listeners; the engine is the single writer.

## On-disk layout (per thread)

```text
<data_dir>/sessions/<session_id>/threads/<thread_id>/
├── thread.json                    # typed ThreadMetadata
└── state/
    ├── messages.jsonl             # append-only trajectory
    ├── inbox.json                 # InboxSnapshot
    ├── plugin_state/state.json    # XCore StateService namespaces
    └── artifacts/<kind>/...       # ArtifactStore-owned files
```

**Never edit this file by hand** — see
[../session-trace.md](../session-trace.md) for the full schema and
ownership rules.

## Typical extension: read-only observer

The plugin itself should not be subclassed. Instead, observe
`state/changed` or typed session events to react to projection changes:

```python
from XBotv2.agentloop import Events, EventContext

class HistoryMetrics:
    name = "history-metrics"
    inject = ["runtime_log"]

    def apply(self, ctx, config):
        ctx.on(Events.STATE_CHANGED, self._on_state_change)

    async def _on_state_change(self, event: EventContext) -> None:
        n = len(event.context_messages or [])
        ctx.runtime_log.bind("history-metrics").info(
            "surface.changed", messages=n
        )
```

For durable new facts (audit log, plugin state, etc.), use the typed
`XCore StateService` namespace:

```python
store = ctx.state.namespace("my-plugin")
snapshot = await store.get("snapshot")
await store.set("snapshot", typed_snapshot.model_dump(mode="json"))
```

## Cross-references

- Depends on: `loop_state`, `thread_persistence`, `runtime_log`.
- Depended on by: every plugin that reads/writes conversation state;
  the engine itself for history writes; `compact`, `usage`,
  `session`, `coretools`, `interactions`.
- Pairs with: [process-persistence.md](process-persistence.md)
  (factory host for server/ACP).

## Common pitfalls

- **Appending directly to `messages.jsonl`**: always go through
  `ThreadPersistence.history` / `InboxStore` / `ArtifactStore`.
  The codec handles `position`, `schema_version`, and surface
  reconstruction; hand-edits silently desync.
- **Duplicating conversation in plugin state**: one typed snapshot
  per related domain; never store a copy of `messages.jsonl` in
  `ctx.state.namespace(...)`.
- **Observing `state/changed` and writing back**: `state/changed`
  announces the projection change after persistence has already
  written; observers must not call back into the write path or they
  will loop.
- **Trusting `state.resumed` before persistence is mounted**: in
  tests, mount the persistence component before the engine or set
  `state.resumed` manually to exercise the resume path.
- **Constructing a `RuntimePaths` and forgetting the data_dir shape**:
  always go through `ctx.runtime_paths`; the `ThreadPersistence`
  factory needs `thread_paths`, which the `session` plugin already
  provides as `ctx.thread_paths`.
