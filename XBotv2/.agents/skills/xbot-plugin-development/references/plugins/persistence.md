# `persistence`

Thread-scoped durable state. It owns the append-only conversation trajectory,
the current surface projection, inbox projection, typed thread metadata,
plugin state, lifecycle records, and artifact storage.

- **Import/profile:** `persistence`, Agent profile.
- **Source:** `XBotv2/persistence/plugin.py`, `store.py`, `models.py`,
  `contracts.py`, `XBotv2/core/history.py`, `XBotv2/core/paths.py`, and
  `XBotv2/core/artifacts.py`.
- **Injects/provides:** `loop_state`, `thread_persistence`, `runtime_log` →
  `agent_inbox`, plus the process-level `thread_persistence_factory`.

## Composition

```text
loop_state + thread_persistence + runtime_log
    -> persistence -> agent_inbox
```

`ThreadPersistenceComponent.inject` is the literal list
`["loop_state", "thread_persistence", "runtime_log"]`; it is an injected
service name, not a `thread_paths` dependency. The component hydrates the loop
state from `persistence.history`, reconciles the durable inbox, restores the
lifetime turn count, publishes `agent_inbox`, and subscribes the metadata store
to `THREAD_METADATA_CHANGED` / `THREAD_METADATA_INITIALIZED`.

The same root `PersistencePlugin` also publishes the process-level factory
used by server/ACP session management:

```python
def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str,
) -> ThreadPersistence:
    return ThreadPersistence.open(session_paths, thread_id=thread_id)
```

There is no second persistence host plugin.

## Facade and ports

```python
class ThreadPersistence(ThreadPersistencePort):
    paths: ThreadPaths
    session_id: str
    thread_id: str
    metadata: ThreadMetadataStore | DeferredThreadMetadataStore
    history: MessageHistoryStore
    artifacts: ArtifactStorePort
    inbox: InboxStore
    lifecycle: ThreadLifecycleStore
    state: StateService

    def __init__(
        self, paths: ThreadPaths, *, state: StateService,
        artifacts: ArtifactStorePort | None = None,
        metadata: ThreadMetadataStore | DeferredThreadMetadataStore | None = None,
    ) -> None: ...

    @classmethod
    def create(
        cls, paths: SessionPaths | ThreadPaths, *, thread_id: str,
        artifacts: ArtifactStorePort | None = None, defer_metadata: bool = False,
    ) -> "ThreadPersistence": ...
    @classmethod
    def open(
        cls, paths: SessionPaths | ThreadPaths, *, thread_id: str,
    ) -> "ThreadPersistence": ...
```

The facade has **no** `workspace_root` and **no** `provider` attribute, and
neither `create()` nor `open()` accepts them. `open()` is the only classmethod
that takes just `paths` and `thread_id`; `create()` additionally accepts
`artifacts` and `defer_metadata`, and eagerly creates the thread `state_dir`
unless metadata is deferred. `open()` builds one private `StateService` over
`thread_paths.plugin_state_file` and is the entry point used for inactive
threads.

The public consumer protocols are in `persistence/contracts.py`, notably
`HistoryPort`, `InboxPersistencePort`, `MetadataPort`, `StatePort`,
`ThreadLifecyclePort`, `ThreadLifecycleWriterPort`, `ThreadPersistencePort`,
and `ThreadPersistenceFactory`. The inbox protocol is named
`InboxPersistencePort`; do not invent an `InboxPort` alias. Note that
`ThreadPersistencePort` declares only `session_id`, `thread_id`, `history`,
`state`, `artifacts`, `metadata`, `inbox`, `lifecycle`, and
`has_persisted_state()` — a consumer typed against the port cannot reach
`paths`.

## History API

```python
class MessageHistoryStore(HistoryPort):
    def load(self) -> list[ConversationMessage]: ...
    def load_surface(self) -> tuple[ConversationMessage, ...]: ...
    def load_transcript(self) -> list[ConversationMessage]: ...
    def append(self, messages: Sequence[ConversationMessage]) -> tuple[ConversationMessage, ...]: ...
    def replace(self, messages: Sequence[ConversationMessage]) -> None: ...
    def replace_surface(
        self, source_ids: Sequence[str], messages: Sequence[ConversationMessage], *,
        operation: str, preserve_transcript: bool,
    ) -> tuple[ConversationMessage, ...]: ...
    def record(self, event: DurableEvent, *, durable: bool = False) -> None: ...
    def open_transactions(self, transaction_kind: str) -> frozenset[str]: ...
    def count(self) -> int: ...
    def count_turns(self) -> int: ...
    def page(self, *, limit: int, cursor: Cursor | None = None) -> HistoryPage[ConversationMessage]: ...
    def page_transcript(self, *, limit: int, cursor: Cursor | None = None) -> HistoryPage[ConversationMessage]: ...
    def page_trajectory(
        self, *, limit: int, cursor: Cursor | None = None, before: int | None = None,
    ) -> TrajectoryRead: ...
```

`record()` takes a `DurableEvent` (the `TransactionStarted | TransactionEnded`
union) and an optional keyword-only `durable=` flag — it is **not**
`record(event: str, data: dict[str, JsonValue])`. The replacement parameter is
positional and named `source_ids`, not `source_node_ids`. `page()` and
`page_transcript()` return `HistoryPage[ConversationMessage]`, a frozen
dataclass with `items: tuple[T, ...]` and `older_cursor: Cursor | None`;
`Cursor` is `NewType("Cursor", str)`. There is no `ConversationPage` and no
`HistoryNode` type anywhere in the repository.

`messages.jsonl` is append-only. Undo, clear, regenerate, and compact append a
`SurfaceReplaced` record; they do not rewrite or truncate existing records. The
persisted shape is a `StoredTrajectoryRecord` envelope with
`schema_version: Literal[1]` and a single discriminated `entry` field whose
discriminator is `kind`. A replacement on disk looks like:

```json
{
  "schema_version": 1,
  "entry": {
    "kind": "surface_replaced",
    "position": 2,
    "operation": "compact",
    "transcript_policy": "preserve",
    "source_ids": ["1", "2"],
    "replacements": [{"kind": "human_input", "id": "m1", "input_id": "i1", "parts": [], "artifacts": []}]
  }
}
```

The three entry kinds are defined in `core/history.py`:

```python
class MessageAppended(BaseModel):
    kind: Literal["message_appended"] = "message_appended"
    position: int
    message: ConversationMessage

class SurfaceReplaced(BaseModel):
    kind: Literal["surface_replaced"] = "surface_replaced"
    position: int
    operation: str
    transcript_policy: Literal["preserve", "replace"]
    source_ids: tuple[str, ...]
    replacements: tuple[ConversationMessage, ...]

class DurableEventRecorded(BaseModel):
    kind: Literal["durable_event_recorded"] = "durable_event_recorded"
    position: int
    timestamp: datetime
    event: DurableEvent
```

There is no `surface_replace` discriminator, no top-level `transcript` field,
no `source_node_ids`, and no `messages` key: the replacement payload's keys are
`source_ids` and `replacements`, and the transcript policy is expressed as
`transcript_policy` (`"preserve"`/`"replace"`). `DurableEventRecorded` is the
timestamped, log-only fact and never enters the current surface.

The trajectory is folded into two independent projections. `_SurfaceState`
applies `MessageAppended` and, for `SurfaceReplaced`, splices `replacements`
over `source_ids` in place. `_TranscriptState` keeps a lineage map: a
`"preserve"` replacement must produce exactly one message and only re-points
its lineage, so compaction stays model-only while the transcript keeps the
original turns. Both projections are recomputed from the same record list and
must accept a transition before it becomes durable.

## Inbox, metadata, and state

```python
class InboxStore(InboxPersistencePort):
    def load(self) -> list[InboxItem]: ...
    def replace(self, items: Sequence[InboxItem]) -> None: ...
    def reconcile(self, committed_input_ids: set[str]) -> list[InboxItem]: ...
```

The inbox stores `InboxItem` values (from `XBotv2.agentloop.contracts`), not an
`InboxInput` type. The envelope is `InboxSnapshot`: a frozen Pydantic model
with an integer-literal `version: Literal[1] = 1` and `items`, **not** a
`schema_version` field. There is no `InboxItemRecord`.

Metadata is a `ThreadMetadata` model (`schema_version`,
`runtime_selection`, `parent_thread_id`, `workspace_root`, `title`) behind
`MetadataPort.load()`/`save()`. `DeferredThreadMetadataStore` buffers writes
while the messages file is empty and writes through once it has records;
`ThreadPersistence.materialize()` flushes it, and the plugin calls that on the
first `Events.TURN_END`. Plugin state belongs in the namespaced XCore
`StateService`, not in the conversation trajectory and not in a second copy
of history.

Lifecycle entries are `ThreadLifecycleRecord` (`schema_version: Literal[1]`,
`event: Literal["started", "completed", "failed", "cancelled"]`, `thread_id`,
`parent_thread_id`, `agent`, `timestamp` with a required timezone offset, and
`error`), appended to `SessionPaths.threads_log`.

## Artifact contract

```python
class ArtifactRef(BaseModel):
    id: str
    media_type: str = "application/octet-stream"
    name: str = ""
    kind: ArtifactKind = ArtifactKind.ATTACHMENT
    size: int = 0
    sha256: str = ""

class ArtifactStorePort(Protocol):
    def put(self, kind: ArtifactKind, payload: bytes, *, media_type: str = "application/octet-stream", name: str = "", suffix: str = "") -> ArtifactRef: ...
    def read(self, artifact: ArtifactRef | str) -> bytes: ...
    def exists(self, artifact: ArtifactRef | str) -> bool: ...
    def model_path(self, artifact: ArtifactRef | str) -> str: ...
```

`ArtifactKind` is a `str` enum with members `media`, `attachments`,
`tool_results`, `context`, and `browser`. `kind` defaults to `ATTACHMENT`, and
`size` is constrained `ge=0`. `ThreadPersistence` composes a filesystem
`ArtifactStore` when none is supplied. Use the injected store and
`ThreadPaths.artifact_file()`; never construct an artifact path from a user or
model string.

## Paths

```text
RuntimePaths.session(session_id).thread(thread_id)
├── thread.json
└── state/messages.jsonl
    state/inbox.json
    state/plugin_state/state.json
    state/artifacts/<kind>/...
```

Obtain paths from `ctx.thread_paths` / `ctx.runtime_paths`. The property names
are `metadata_file`, `state_dir`, `messages_file`, `inbox_file`,
`plugin_state_file`, and `artifacts_dir`; avoid obsolete `thread_json` or
`messages_jsonl` names. `ThreadLifecycleStore` writes to
`ThreadPaths.session.threads_log` (`<session root>/threads.jsonl`), not into
the thread `state` directory.

## Runtime log events

The store and plugin bind a `persistence` logger and emit
`persistence.hydrated`, `persistence.history.loaded`,
`persistence.history.appended`, `persistence.surface.replaced`,
`persistence.trajectory.event`, `persistence.trajectory.tail_dropped`,
`persistence.metadata.saved`, `persistence.inbox.replaced`,
`persistence.inbox.reconciled`, and `persistence.lifecycle.appended`. These are
log facts, not bus events.

## Invariants

- Positions in the trajectory are contiguous and start at one; a record whose
  `position` does not equal its 1-based line index is rejected at parse time.
- A final line without its trailing newline is treated as an in-flight append
  and dropped rather than parsed.
- Message identities must be unique across the surface; a record that reuses
  one raises `ValueError`.
- Every persisted record is validated with a strict Pydantic model
  (`extra="forbid"`, frozen).
- History writes go through `ThreadPersistence.history`.
- Observers may consume persistence events but must not write history again.
- Runtime-only loop state is not serialized as conversation messages.
