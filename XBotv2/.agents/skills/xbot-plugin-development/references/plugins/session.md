# `session`

The per-thread Agent session plugin. It exposes the runtime identity, typed
paths, variables, loop state, and command registration used by an active
thread. The same root plugin contributes the process `sessions` facet for
server/ACP profiles.

Source: `XBotv2/session/plugin.py`, `session.py`, `contracts.py`,
`commands.py`.

## Injected services

```text
runtime_paths + session_launch + artifacts
    -> session, paths, session_paths, thread_paths, loop_state,
       workspace_root, data_root, variables, thread_metadata
```

Use the typed services from `Context`; do not derive filesystem paths from
`data_root` or retain the whole context in a plugin.

## Thread identity

```python
@dataclass
class SessionInfo:
    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"
```

`EventContext.session` is `SessionInfo`, not the runtime `Session` object.

## Composition commands

`OpenSession` and `OpenThread` are internal frozen dataclasses. HTTP request
models are intentionally separate:

```python
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

`OpenSession` additionally accepts `session_id`, `plugin_configs`, and its
resolved thread id. Server composition supplies provider and plugin facts;
clients must not send provider objects or plugin internals.

## Paths

The canonical path API is in `XBotv2/core/paths.py`:

```python
paths: RuntimePaths
session_paths: SessionPaths
thread_paths: ThreadPaths

thread_paths.metadata_file
thread_paths.state_dir
thread_paths.messages_file
thread_paths.inbox_file
thread_paths.plugin_state_file
thread_paths.artifacts_dir
```

`RuntimePaths.sessions_dir` is a property, not a method. Always use
`RuntimePaths.session(session_id).thread(thread_id)` or injected path objects;
never recreate the layout with string concatenation.

## Runtime service

`ctx.session` is a runtime handle and `ctx.loop_state` is the mutable loop
state. Neither is a persistence model. Persist only typed metadata/state via
the persistence ports and XCore `StateService` namespaces.

## Commands and lifecycle

```python
def build_session_commands(session) -> tuple[Command, ...]: ...
```

Session commands belong to the session plugin. Plugins should subscribe to
typed lifecycle events (`session/start`, `session/resume`, `session/close`)
when they need lifecycle behavior, and must not route slash commands through
Agent tools.

## Common mistakes

- `event.session.paths` is invalid; use the plugin context's `thread_paths`.
- `ThreadPaths` uses `metadata_file` and `messages_file`, not
  `thread_json`/`messages_jsonl` properties.
- A process `SessionsPort` is not the same object as per-thread `ctx.session`.
- Do not serialize the runtime `Session` object or `LoopState` as conversation
  history.
