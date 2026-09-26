# `session`

The per-thread Agent session plugin. It exposes the runtime identity, typed
paths, variables, loop state, and command registration used by an active
thread. The same root plugin contributes the process `sessions` facet for
server/ACP profiles.

- **Import/profile:** `session`, Agent profile.
- **Source:** `XBotv2/session/plugin.py`, `session.py`, `contracts.py`,
  `commands.py`.
- **Injects/provides:** `runtime_paths`, `session_launch`, `commands`,
  `artifacts` (required) and `thread_persistence` (optional) → `session`,
  `paths`, `workspace_root`, `data_root`, `variables`, `thread_paths`,
  `agent_inbox`.

## Composition

```text
runtime_paths + session_launch + commands + artifacts (+ thread_persistence)
    -> session plugin -> session, paths, workspace_root, data_root,
       variables, thread_paths, agent_inbox
```

`SessionRuntimeComponent.inject` is a mapping with `"required"` and
`"optional"` keys, not a flat list:

```python
class SessionRuntimeComponent:
    inject = {
        "required": ["runtime_paths", "session_launch", "commands", "artifacts"],
        "optional": ["thread_persistence"],
    }
    name = "xbot.session"
```

There is **no** `session_paths` injected service. `session_paths` is a field of
the `SessionLaunch` service (`application/contracts.py`) and an attribute of
the runtime `Session` object; nothing registers a service named
`session_paths`. `artifacts` is listed even though this component never reads
it: the inject set selects the fiber scope, and dropping it would mount the
runtime before the session scope owns `workspace_root`.

`loop_state` and `thread_metadata` are not injected here either — constructing
`LoopState(ctx, key=..., variables=...)` provides both on the context for the
owning fiber's lifetime. `SessionRuntimeComponent` builds that state and then
registers `session`, `paths`, `workspace_root`, `data_root`, `variables`, and
`thread_paths`.

Use the typed services from `Context`; do not derive filesystem paths from
`data_root` or retain the whole context in a plugin.

## Thread identity

Loop lifecycle events carry `SessionRuntimeState` from
`XBotv2.session.contracts`. It owns a `SessionKey` and a `ThreadMetadataState`,
and derives `session_id`, `thread_id`, and `workspace_root` from them; it is
not an older `SessionInfo` read model or the runtime `Session` service object.
Public client-facing summaries are separate `SessionSummary` and
`ThreadSummary` projections.

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
    provider_name: str | None
    mode: SessionMode                      # Literal["new", "resume"]
    no_plugins: bool
    selected_agent: str | None = None
    model_override: BaseProvider | None = None
```

`provider_name` is optional (`str | None`) in both `OpenThread` and
`OpenSession`; `None` means the provider is resolved from configuration.
`OpenSession` additionally carries `session_id: str | None`, `plugin_configs`,
and a required `workspace_root: str`. Server composition supplies provider and
plugin facts; clients must not send provider objects or plugin internals.

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

`ctx.session` is a runtime handle (the `Session` object, typed by
`SessionPort`) and `ctx.loop_state` is the mutable loop state. Neither is a
persistence model. Persist only typed metadata/state via the persistence ports
and XCore `StateService` namespaces.

## Commands and lifecycle

```python
def build_session_commands(
    session: SessionPort,
    *,
    pending_input_count: Callable[[], int],
) -> tuple[Command, ...]: ...
```

Both parameters matter: `session` is positional and `pending_input_count` is a
keyword-only callback that `/status` calls live, so the engine stays the single
owner of pending-input state. The built-in session commands are `/status`,
`/clear`, `/undo [count]`, and `/fork`; `/help` belongs to the Textual client
rather than this server plugin.

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
