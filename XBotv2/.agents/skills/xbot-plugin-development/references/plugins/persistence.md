# `persistence`

Thread-scoped durable state. It owns the append-only conversation trajectory,
the current surface projection, inbox projection, typed thread metadata,
plugin state, lifecycle records, and artifact storage.

Source: `XBotv2/persistence/plugin.py`, `store.py`, `models.py`,
`contracts.py`, `XBotv2/core/paths.py`, and `core/artifacts.py`.

## Composition

```text
loop_state + thread_paths + runtime_log
    -> persistence -> thread_persistence
```

The same root `persistence` plugin also publishes the process-level factory
used by server/ACP session management. There is no second persistence host
plugin.

## Facade and ports

```python
class ThreadPersistence:
    paths: ThreadPaths
    session_id: str
    thread_id: str
    workspace_root: str
    provider: str
    history: MessageHistoryStore
    inbox: InboxStore
    metadata: ThreadMetadataStore
    lifecycle: ThreadLifecycleStore
    artifacts: ArtifactStorePort
    state: StateService

    @classmethod
    def create(cls, paths: SessionPaths | ThreadPaths, *, thread_id: str,
               workspace_root: str, provider: str, artifacts=None) -> "ThreadPersistence": ...
    @classmethod
    def open(cls, paths: SessionPaths | ThreadPaths, *, thread_id: str,
             workspace_root: str = "", provider: str = "") -> "ThreadPersistence": ...
```

The public consumer protocols are in `persistence/contracts.py`, notably
`HistoryPort`, `InboxPersistencePort`, `MetadataPort`, `StatePort`,
`ThreadLifecyclePort`, and `ThreadPersistencePort`. The inbox protocol is
named `InboxPersistencePort`; do not invent an `InboxPort` alias.

## History API

```python
class MessageHistoryStore:
    def load(self) -> list[Message]: ...
    def load_surface(self) -> tuple[HistoryNode, ...]: ...
    def load_transcript(self) -> list[Message]: ...
    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]: ...
    def replace(self, messages: Sequence[Message]) -> None: ...
    def replace_surface(
        self, source_node_ids: Sequence[str], messages: Sequence[Message], *,
        operation: str, preserve_transcript: bool,
    ) -> tuple[HistoryNode, ...]: ...
    def record(self, event: str, data: dict[str, JsonValue]) -> None: ...
    def page(self, *, limit: int, cursor: str | None = None) -> ConversationPage: ...
    def page_transcript(self, *, limit: int, cursor: str | None = None) -> ConversationPage: ...
```

`messages.jsonl` is append-only. Undo, clear, regenerate, and compact append
a `surface_replace` record; they do not rewrite or truncate existing records.
The `surface_replace` schema is:

```json
{
  "schema_version": 1,
  "position": 2,
  "record_type": "surface_replace",
  "operation": "compact",
  "transcript": "preserve",
  "source_node_ids": ["1", "2"],
  "messages": [{"role": "user", "parts": []}]
}
```

The complete validated shape is `SurfaceReplaceRecord` in
`persistence/models.py`; each replacement message uses
`MessagePayloadRecord`. `MessageRecord` adds a contiguous `position` to one
provider-neutral message. `TrajectoryEventRecord` is a timestamped,
log-only fact and never enters the current surface.

## Inbox and state

```python
class InboxStore:
    def load(self) -> list[InboxInput]: ...
    def replace(self, items: Sequence[InboxInput]) -> None: ...
    def reconcile(self, committed_input_ids: set[str]) -> list[InboxInput]: ...
```

`InboxSnapshot` and `InboxItemRecord` are frozen Pydantic models with
`schema_version=1`. Plugin state belongs in the namespaced XCore
`StateService`, not in the conversation trajectory and not in a second copy
of history.

## Artifact contract

```python
class ArtifactRef(BaseModel):
    id: str
    media_type: str = "application/octet-stream"
    name: str = ""
    kind: ArtifactKind
    size: int = 0
    sha256: str = ""

class ArtifactStorePort(Protocol):
    def put(self, kind: ArtifactKind, payload: bytes, *, media_type: str = "application/octet-stream", name: str = "", suffix: str = "") -> ArtifactRef: ...
    def read(self, artifact: ArtifactRef | str) -> bytes: ...
    def exists(self, artifact: ArtifactRef | str) -> bool: ...
    def model_path(self, artifact: ArtifactRef | str) -> str: ...
```

Use the injected store and `ThreadPaths.artifact_file()`; never construct an
artifact path from a user or model string.

## Paths

```text
RuntimePaths.session(session_id).thread(thread_id)
└── thread.json
    state/messages.jsonl
    state/inbox.json
    state/plugin_state/state.json
    state/artifacts/<kind>/...
```

Obtain paths from `ctx.thread_paths`/`ctx.runtime_paths`. The property names
are `metadata_file`, `messages_file`, `inbox_file`, `plugin_state_file`, and
`artifacts_dir`; avoid obsolete `thread_json` or `messages_jsonl` names.

## Invariants

- Positions in the trajectory are contiguous and start at one.
- Every persisted record is validated with a strict Pydantic model.
- History writes go through `ThreadPersistence.history`.
- Observers may consume persistence events but must not write history again.
- Runtime-only loop state is not serialized as conversation messages.
