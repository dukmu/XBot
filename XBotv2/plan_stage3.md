# XBotv2 Stage 3 Plan (Historical)

> Archived implementation plan. This is not a current specification; see
> `docsv2/README.md`.

## Goal

Stage 3 turns XBotv2 from a framework-like runtime into a small coding-agent
harness similar in spirit to `cc-mini`, while preserving the parts that matter:

- HTTP/SSE protocol and TUI transport.
- Hook and plugin mechanisms.
- Workspace-root session model from Stage 2.
- Session-scoped provider, permission, and sandbox controls.

The core design target is:

```text
one session runtime
one engine turn loop
one tool protocol
one persistence model
```

The main anti-goal is runtime indirection: no event-sourced state machine for
ordinary session state, no LangChain/LangGraph message objects in core, and no
command path that writes events and waits for materialization before affecting
the active session.

## Confirmed Decisions

- Keep HTTP/SSE as the production protocol.
- Persist only:
  - provider-facing messages
  - session config overlays
  - plugin-private data
- Remove runtime event trace persistence.
- Remove `state.yaml` and materialized state as runtime concepts.
- Keep hooks and plugins, but do not grow the hook surface during the rewrite.
- Replace LangChain/LangGraph-dependent tool protocol and execution with XBotv2-owned types.
- Remove LangChain/LangGraph dependencies from core runtime.
- Server commands operate directly on the active session runtime.
- Permissions and sandbox are owned and managed by the session runtime.
- Reduce dispatcher/event-bus wrappers and extra per-turn state objects.
- Live approval scopes support only `once` and `session`; Stage 3 does not write
  global config from live approvals.
- Plugin private data may include plugin-owned artifacts directories.
- First provider-adapter pass supports OpenAI-compatible APIs and Anthropic SDK.
- No forward compatibility with Stage 2 runtime state is required.

## Target Architecture

### Runtime Shape

```text
HTTP app
  -> SessionRuntime
      -> Engine
      -> SessionConfigOverlay
      -> SessionPolicy
      -> SessionSandbox
      -> PluginStore
```

`SessionRuntime` is the authoritative object for one active session. It owns:

- `session_id`
- `thread_id`
- `workspace_root`
- `provider_name`
- `provider_client`
- `messages`
- `config_overlay`
- `permission_policy`
- `sandbox_policy`
- `plugin_stores`
- active turn cancellation state
- live interaction waiters

There should be no separate materialized state object that commands or tools
must update before the session changes.

### HTTP/SSE Boundary

Keep endpoints from Stage 2 unless explicitly removed later:

```http
GET /health
POST /hello
POST /sessions
GET /commands
GET /sessions/{sid}/commands
POST /sessions/{sid}/commands
POST /sessions/{sid}/messages
POST /sessions/{sid}/interactions/permission-response
POST /sessions/{sid}/interactions/user-input
POST /sessions/{sid}/interrupt
POST /sessions/{sid}/shutdown
```

The server can still host multiple sessions from multiple workspace roots.

The `POST /sessions/{sid}/messages` handler should call directly into:

```python
async for event in session.run_turn(content):
    yield sse(event)
```

No separate dispatcher event bus should be required for the normal path. If a
small helper remains for SSE formatting or cancellation, it should not own
session state.

## Persistence Model

### Files To Keep

Session directory:

```text
data/sessions/<session_id>/
  messages.jsonl
  overlay.yaml
  plugins/
    <plugin_name>/
      state.yaml
      artifacts/
```

`messages.jsonl` contains only provider-facing conversation messages.

`overlay.yaml` contains session-scoped runtime config:

```yaml
session_id: ...
thread_id: agent
workspace_root: /repo/project
provider: default
permissions:
  allow: []
  deny: []
  ask: []
sandbox:
  external_read: ask
  external_write: deny
  workspace_read: allow
  workspace_write: allow
```

Plugin private data remains opaque to core:

```text
data/sessions/<session_id>/plugins/<plugin_name>/state.yaml
data/sessions/<session_id>/plugins/<plugin_name>/artifacts/
```

### Files To Remove

Remove these runtime files and concepts:

```text
events.jsonl
state.yaml
artifacts/ as a core state concept
```

Core-owned `artifacts/` is removed. If a plugin needs large output caches or
other artifacts, it owns them under its plugin private directory. If a future
trace/debug feature is needed, it should be opt-in diagnostic output, not the
source of truth for session behavior.

### Resume Semantics

`mode="resume"` loads:

1. `messages.jsonl`
2. `overlay.yaml`
3. plugin private data

If `mode="resume"` is requested and the session directory is missing, return
404. If `mode="new"` is requested and the explicit session id already exists,
return an error.

## Message Model

Use XBotv2-owned provider-neutral message dictionaries in core:

```python
Message = dict[str, Any]
```

Preferred shape follows Anthropic-style content blocks because it naturally
represents tool use and tool results:

```python
{"role": "user", "content": "..."}
{"role": "assistant", "content": [{"type": "text", "text": "..."}]}
{"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {...}}]}
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "...", "is_error": False}]}
```

Provider adapters translate this internal shape to Anthropic, OpenAI, or any
OpenAI-compatible provider. Core should not import LangChain message classes.

## Provider Client

Create an XBotv2 provider client interface:

```python
class ProviderClient:
    async def stream_messages(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
    ) -> AsyncIterator[ProviderEvent]: ...
```

Provider events should be XBotv2-owned:

```python
{"type": "text_delta", "text": "..."}
{"type": "tool_call_delta", "tool_call_id": "...", ...}
{"type": "message", "message": {...}, "usage": {...}}
```

First implementation supports OpenAI-compatible providers, Anthropic SDK, and
mock providers. The core interface should still not depend on either SDK's
native message classes.

## Tool Protocol

Replace LangChain `StructuredTool`, `AIMessage`, and `ToolMessage` dependencies
with XBotv2-owned types:

```python
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]

@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    turn_complete: bool = False

class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    async def execute(self, **kwargs) -> ToolResult: ...
```

The execution pipeline is:

```text
ToolCall
  -> session.permission_policy.check(...)
  -> session.sandbox_policy.check(...)
  -> hook BEFORE_TOOL_CALL
  -> tool.execute(...)
  -> hook AFTER_TOOL_CALL
  -> ToolResult
```

Read-only tools may run concurrently. Write/bash/interactive tools run
sequentially unless explicitly marked safe.

## Engine Turn Loop

The Stage 3 engine should resemble the `cc-mini` main loop:

```python
async def run_turn(self, user_input: str) -> AsyncIterator[RuntimeEvent]:
    append_user_message()
    while True:
        provider_message = await stream_provider_response()
        append_assistant_message(provider_message)
        if no_tool_calls(provider_message):
            break
        tool_results = await execute_tool_calls(provider_message)
        append_tool_results(tool_results)
```

Events streamed to TUI remain explicit dictionaries:

```python
{"type": "turn_started", "data": {...}}
{"type": "assistant_message_delta", "data": {...}}
{"type": "assistant_message", "data": {...}}
{"type": "tool_calls_started", "data": {...}}
{"type": "permission_request", "data": {...}}
{"type": "tool_result", "data": {...}}
{"type": "usage", "data": {...}}
{"type": "turn_finished", "data": {...}}
{"type": "turn_cancelled", "data": {...}}
{"type": "error", "data": {...}}
```

Cancellation should rollback to the message count at turn start, not repair
LangChain-specific orphan states.

## Commands

Server commands operate directly on `SessionRuntime`:

```python
def provider_use(session, name):
    session.switch_provider(name)
    session.overlay.provider = name
    session.save_overlay()

def permission_set(session, tool, decision):
    session.permissions.set(tool, decision)
    session.overlay.permissions = session.permissions.to_overlay()
    session.save_overlay()

def sandbox_set(session, key, value):
    session.sandbox.set(key, value)
    session.overlay.sandbox = session.sandbox.to_overlay()
    session.save_overlay()
```

No command should append runtime events or wait for materialization to change
live behavior.

Command results should still be returned to the TUI and should still not enter
provider-facing message history.

## Permission And Sandbox Ownership

`SessionRuntime` owns effective policy objects:

```python
session.permissions
session.sandbox
```

Effective policy is built at session open:

```text
built-in defaults -> global config -> session overlay -> one-shot live decision
```

Live permission responses support only `scope="once"` and `scope="session"`.
`scope="session"` updates the session overlay directly. `scope="always"` is not
supported in Stage 3 and should return a clear unsupported-scope error instead
of updating global config.

External read/write sandbox behavior remains:

```yaml
external_read: ask
external_write: deny
workspace_read: allow
workspace_write: allow
```

Workspace symlink escapes remain denied.

## Hooks And Plugins

Keep the hook/plugin mechanisms but simplify their data dependencies.

Hooks should receive XBotv2-owned types:

```python
HookContext(
    session=session.info,
    messages=session.messages,
    tool_call=ToolCall | None,
    tool_result=ToolResult | None,
    model_request=dict | None,
    model_response=dict | None,
)
```

PluginStore should map directly to:

```text
data/sessions/<session_id>/plugins/<plugin_name>/state.yaml
data/sessions/<session_id>/plugins/<plugin_name>/artifacts/
```

Do not add new hook stages during Stage 3 unless a regression proves an existing
stage cannot represent required behavior.

## Modules To Replace Or Remove

### Replace

- `xbotv2/llm/client.py`: replace LangChain model creation with XBotv2 provider adapters.
- `xbotv2/tools/runtime.py`: replace LangChain `ToolMessage` execution with XBotv2 `ToolResult` execution.
- `xbotv2/tools/registry.py`: keep registry concept but store XBotv2 `Tool` objects only.
- `xbotv2/core/engine.py`: rewrite around internal dict messages and XBotv2 provider events.
- `xbotv2/protocol/commands.py`: make commands mutate `SessionRuntime` directly.
- `xbotv2/protocol/http_server.py`: call session runtime directly and remove dispatcher dependency.

### Remove

- `xbotv2/protocol/dispatcher.py` or reduce it to a tiny session map helper.
- `xbotv2/persistence/materializer.py`.
- `state.yaml` generation.
- `events.jsonl` runtime persistence.
- LangChain/LangGraph imports and dependencies.
- LangChain-specific message serialization/deserialization paths.

### Keep

- `xbotv2/hooks/` with simplified context data.
- `xbotv2/plugin/` with plugin manifests and plugin stores.
- HTTP/SSE protocol endpoints.
- TUI transport boundary.
- Workspace-root model.

## Implementation Order

1. Add XBotv2-owned runtime data types: `Message`, `Tool`, `ToolCall`, `ToolResult`, `ProviderEvent`, `RuntimeEvent`.
2. Add `SessionRuntime` and `SessionOverlay` with direct load/save for `messages.jsonl`, `overlay.yaml`, and plugin stores.
3. Rewrite provider client layer to remove LangChain/LangGraph from core calls.
4. Implement OpenAI-compatible, Anthropic SDK, and mock provider adapters.
5. Convert built-in tools to the XBotv2 tool protocol.
6. Rewrite tool execution using XBotv2 `ToolCall` and `ToolResult`.
7. Rewrite engine turn loop around internal dict messages and provider events.
8. Update hooks to use XBotv2-owned context types.
9. Update plugin loader/store to use the new tool protocol and plugin private data path.
10. Rewrite HTTP server session handling to use `SessionRuntime` directly.
11. Rewrite server commands to mutate session runtime directly.
12. Delete event persistence, materializer, and `state.yaml` tests.
13. Remove LangChain/LangGraph dependencies from project metadata.
14. Update docs and tests to Stage 3 model.

## Test Coverage Targets

- New session creates `messages.jsonl`, `overlay.yaml`, and plugin store directory only.
- Resume loads messages and overlay without `state.yaml` or `events.jsonl`.
- HTTP/SSE message stream still emits turn, assistant, tool, permission, usage, error, and end events.
- `/provider use` changes the active session provider and persists overlay.
- `/permission set/reset` changes live session policy and persists overlay.
- `/sandbox set/reset` changes live session sandbox and persists overlay.
- Permission response with `scope=session` changes live policy and overlay immediately.
- Permission response with `scope=always` returns an unsupported-scope error and
  never mutates global config.
- Command results do not enter `messages.jsonl`.
- Built-in read-only tools can run concurrently.
- Write/bash tools run sequentially and honor permission/sandbox policy.
- Workspace-relative paths resolve under `workspace_root`.
- External read asks; external write denies; symlink escape denies.
- Hooks fire with XBotv2-owned context objects.
- Plugin tools register and execute through the new tool protocol.
- Plugin private data persists under `plugins/<plugin>/state.yaml`.
- Plugin-owned artifacts persist under `plugins/<plugin>/artifacts/`.
- No runtime import of LangChain or LangGraph remains.

## Migration Notes

No forward or backward compatibility is required for Stage 2 runtime session
directories. Stage 3 may delete or ignore Stage 2 session data. Prefer starting
new Stage 3 sessions over migration code.

## Closed Decisions

- Live permission scopes are `once` and `session` only.
- `scope="always"` does not mutate global config.
- Core-owned artifacts are removed; plugin artifacts are plugin-private data.
- First provider-adapter pass includes OpenAI-compatible, Anthropic SDK, and mock.
- Stage 3 does not preserve Stage 2 runtime compatibility.
