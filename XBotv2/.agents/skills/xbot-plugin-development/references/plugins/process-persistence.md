# `process-persistence`

Process/server facet of the root `persistence` plugin. It publishes the
typed factory used by `SessionManager` to inspect or open inactive threads.
Agent-profile hydration is another dependency-gated facet of the same root
plugin; there is no separate `process.persistence` tree entry.

Source: `XBotv2/persistence/plugin.py`, `store.py`, `contracts.py`.

## Factory

```python
def thread_persistence_factory(
    session_paths: SessionPaths | ThreadPaths,
    *,
    thread_id: str,
    workspace_root: str = "",
    provider: str = "",
) -> ThreadPersistence:
    return ThreadPersistence.open(
        session_paths,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider=provider,
    )
```

`SessionManager` obtains `SessionPaths` from `RuntimePaths.session()` and
passes the explicit thread id. Consumers must not build this path by joining
strings.

## Port contracts

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
    inbox: InboxPersistencePort
    lifecycle: ThreadLifecyclePort
    def has_persisted_state(self) -> bool: ...

class ThreadPersistenceFactory(Protocol):
    def __call__(
        self, session_paths: SessionPaths | ThreadPaths, *, thread_id: str = "",
        workspace_root: str = "", provider: str = "",
    ) -> ThreadPersistencePort: ...
```

The inbox contract is `InboxPersistencePort`. History replacement uses
`source_node_ids`, `messages`, `operation`, and `preserve_transcript`; it does
not expose the obsolete `target_node_ids`/`replace_node_ids` names.

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
