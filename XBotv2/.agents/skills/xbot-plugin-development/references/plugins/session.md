# `session`

The per-thread Agent session — its identity, paths, and loop state. This
plugin is the Agent-profile counterpart to `process.sessions` (the
process-wide `SessionManager` for server/ACP carriers).

- **Import/profile:** `session`, Agent profile.
- **Source:** `XBotv2/session/plugin.py`, `XBotv2/session/session.py`,
  `XBotv2/session/types.py`, `XBotv2/session/commands.py`.
- **Injects/provides:** `runtime_paths`, `session_launch`, `commands`,
  `artifacts` → `session`, `paths`, `thread_paths`, `loop_state`,
  `workspace_root`, `data_root`, `variables`, `thread_metadata`.
- **Subscribes to events:** none directly; the loop engine dispatches
  `session/start`, `session/resume`, `session/close` against the same
  context.

## Public data models

### `SessionInfo` (`XBotv2/session/types.py`)

```python
@dataclass
class SessionInfo:
    """Mutable identity and counters for one active Agent thread."""
    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"
```

`EventContext.session` carries this exact type — *not* the `Session`
object. Get `event.session.session_id` for the closing thread.

### `SessionLaunch` (`XBotv2/application/services.py`)

```python
@dataclass(frozen=True, slots=True)
class SessionLaunch:
    session_id: str
    thread_id: str
    workspace_root: Path
    provider_name: str
    session_paths: SessionPaths
    interactive: bool
    is_subagent: bool
```

Application-injected; a plugin reads `ctx.session_launch` only when it
needs the launch facts at composition time (rare).

### `OpenSession` / `OpenedSession` / `OpenThread`

```python
@dataclass(frozen=True, slots=True)
class OpenSession:
    session_id: str | None
    thread_id: str
    workspace_root: str
    provider_name: str
    mode: SessionMode                            # "new" | "resume"
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

# OpenedSession is the concrete result — adds the resolved session_id
# plus paths and the live runtime handle.
```

`SessionMode = Literal["new", "resume"]`.

### `ThreadPaths` / `SessionPaths`

```python
# Pseudocode of the public surface
class ThreadPaths:
    @property
    def root(self) -> Path: ...              # {data_dir}/sessions/<sid>/threads/<tid>
    @property
    def thread_json(self) -> Path: ...      # root/thread.json
    @property
    def state_dir(self) -> Path: ...        # root/state
    @property
    def messages_jsonl(self) -> Path: ...    # root/state/messages.jsonl
    @property
    def inbox_json(self) -> Path: ...
    @property
    def plugin_state_json(self) -> Path: ...
    @property
    def artifacts_dir(self) -> Path: ...

class SessionPaths:
    @property
    def root(self) -> Path: ...              # {data_dir}/sessions/<sid>
    def has_thread(self, thread_id: str) -> bool: ...
    def thread(self, thread_id: str) -> ThreadPaths: ...
```

Always read these from `ctx.thread_paths` / `ctx.runtime_paths`; never
re-derive by joining strings.

## `Session` object (`XBotv2/session/session.py`)

`ctx.session` is a `Session` instance. Its public surface:

```python
class Session:
    info: SessionInfo                  # == ctx.loop_state.session
    paths: RuntimePaths                # == ctx.paths
    variables: RuntimeVariables        # == ctx.variables
    state: LoopState                   # == ctx.loop_state (same instance)
    session_paths: SessionPaths        # == ctx.session_launch.session_paths

    def status(self) -> SessionStatus: ...
    # Plus methods used by the loop and command plane (not for plugins).
```

`ctx.loop_state` is the same `LoopState`; the engine dispatches events
against it.

## On-disk layout (per thread)

```text
<data_dir>/sessions/<session_id>/
├── config.yaml              # per-session config snapshot
└── threads/<thread_id>/
    ├── thread.json          # typed ThreadMetadata
    └── state/
        ├── messages.jsonl   # append-only trajectory (schema_version=1)
        ├── inbox.json       # InboxSnapshot
        ├── plugin_state/state.json
        └── artifacts/<kind>/...
```

The session-level `threads.jsonl` is owned by `ThreadLifecycleStore` and
is plugin-private. See [../session-trace.md](../session-trace.md) for
the JSONL record schema and ownership rules.

## Slash commands registered by `session`

```python
def build_session_commands(session) -> tuple[Command, ...]: ...
```

These are session/thread listing and switching commands. Add
session-shaped commands here rather than in your own plugin.

## Typical extension: react to session/close

```python
from XBotv2.agentloop import EventContext, Events

class CleanupPlugin:
    name = "session-cleanup"
    inject = ["runtime_paths"]

    def apply(self, ctx, config):
        ctx.on(Events.SESSION_CLOSE, self._on_close)

    async def _on_close(self, event: EventContext) -> None:
        sid = event.session.session_id if event.session else None
        # paths.sessions_dir is a @property returning a Path
        sessions = ctx.runtime_paths.sessions_dir
        # ... walk sessions, archive empty traces ...
```

## Cross-references

- Depends on: `commands` (registers session commands), `artifacts`,
  `runtime_paths`, `session_launch`.
- Depended on by: nearly every Agent-profile plugin reads
  `ctx.session` or `ctx.loop_state`.
- Pairs with: [process-sessions.md](process-sessions.md) (process
  manager on server/ACP) — per-thread here, process-wide there.

## Common pitfalls

- **Treating `event.session` as the `Session` object**: it's
  `SessionInfo`. `event.session.paths` does not exist; use
  `ctx.session.paths` or `ctx.runtime_paths`.
- **Calling `paths.sessions_dir()`**: it is a `@property` returning a
  `Path`. No parentheses.
- **Constructing paths by hand**: never join `data_dir + "sessions"`;
  always read `RuntimePaths.sessions_dir` or `SessionPaths.thread(...)`.
- **Persisting `ctx.session` itself**: Session is a runtime handle.
  Only `SessionInfo` (or its fields) are JSON-safe.
- **Assuming the agent can see server sessions**: this is the
  per-thread Agent session. The server's `SessionManager` lives on
  `ctx.sessions` and is a separate API.
