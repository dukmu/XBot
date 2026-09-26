# `process-persistence`

Process/server facet of the root `persistence` plugin. It publishes the
typed factory used by `SessionManager` to inspect or open inactive threads.
Agent-profile hydration is another dependency-gated facet of the same root
plugin; there is no separate `process.persistence` tree entry.

- **Import/profile:** `persistence`, process/server profile.
- **Source:** `XBotv2/persistence/plugin.py`, `store.py`, `contracts.py`.
- **Injects/provides:** `persistence` root plugin → `thread_persistence_factory`.

## Factory

```python
def thread_persistence_factory(
    session_paths: SessionPaths,
    *,
    thread_id: str,
) -> ThreadPersistence:
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
    )
```

`ThreadPersistence.open()` accepts only `paths` (a `SessionPaths` or an
already-resolved `ThreadPaths`) and keyword-only `thread_id`. It does **not**
accept `workspace_root` or `provider`, and the resulting `ThreadPersistence`
exposes neither attribute — workspace and provider identity live in thread
metadata, not in the persistence facade.

`PersistencePlugin.apply` registers the factory with
`ctx.set("thread_persistence_factory", thread_persistence_factory)` and then
gates the Agent-profile hydration on
`ctx.inject(["loop_state", "thread_persistence", "runtime_log"], ...)`.

`SessionManager` obtains `SessionPaths` from `RuntimePaths.session()` and
passes the explicit thread id. Consumers must not build this path by joining
strings.

## Port contracts

```python
class ThreadPersistencePort(Protocol):
    session_id: str
    thread_id: str
    history: HistoryPort
    state: StatePort
    artifacts: ArtifactStorePort
    metadata: MetadataPort
    inbox: InboxPersistencePort
    lifecycle: ThreadLifecyclePort
    def has_persisted_state(self) -> bool: ...

class ThreadPersistenceFactory(Protocol):
    def __call__(
        self,
        session_paths: SessionPaths,
        *,
        thread_id: str,
    ) -> ThreadPersistencePort: ...

class ThreadLifecycleWriterPort(Protocol):
    def append(self, record: ThreadLifecycleRecord) -> None: ...
```

`ThreadPersistenceFactory` is narrower than the concrete factory function: the
protocol takes `SessionPaths` only and has no defaults, while the registered
function accepts `SessionPaths | ThreadPaths`. `ThreadLifecycleWriterPort` is
the append-only lifecycle slice handed to child-application orchestration, so
a child cannot read back the parent's lifecycle log.

The inbox contract is `InboxPersistencePort`. History replacement uses
`source_ids`, `messages`, `operation`, and `preserve_transcript`; it does
not expose the obsolete `target_node_ids`/`replace_node_ids` names.

`has_persisted_state()` is true when the trajectory has bytes, or the metadata
file, inbox file, or plugin state file exists. The persistence component uses
it to restore `LoopState.resumed`.

## Boundary rules

- The factory is for typed process-level reads and runtime construction.
- The Agent plugin receives `thread_persistence` and hydrates `loop_state`
  from its `history`, `inbox`, `metadata`, and state services.
- HTTP/ACP route code uses `SessionsPort`, not this factory, for session
  operations.
- No route or plugin may directly edit `messages.jsonl`, `inbox.json`, or
  `plugin_state/state.json`.

See [persistence.md](persistence.md) for trajectory and filesystem schemas
and [process-sessions.md](process-sessions.md) for the manager that consumes
this factory.
