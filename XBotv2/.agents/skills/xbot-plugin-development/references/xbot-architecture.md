# XBot Architecture Reference

Primary source when a checkout is available: `XBotv2/docs/architecture.md` and
`XBotv2/docs/core/core.md`. For pip/uv installations, use the matching bundled
skill references and inspect the installed `XBotv2` package; this page is a
decision guide, not a replacement for the version-matched source docs.

## Ownership Map

| Area | Owner | Extension point |
|---|---|---|
| ReAct loop and generic Tool execution | `XBotv2/agentloop` | loop hooks and `ToolsPort` |
| Shared messages, `Tool`, `ToolCall`, `ToolResult` | `XBotv2/core` | stable data contracts |
| Session/thread identity and runtime | `XBotv2/session` | session services and protocol |
| Agent definitions and child applications | `XBotv2/agents` | Agent declarations and typed events |
| Prompt assembly | `XBotv2/context_builder` | typed context components |
| Transport routes and wire models | owning package `protocol.py` | FastAPI/transport router |
| Permission, sandbox, interaction, persistence | their named plugins | declared services and typed events |
| Filesystem and Shell Tools | `XBotv2/coretools` | session-bound Tool factories |

Core does not import a concrete built-in plugin. A plugin may import stable
contracts, but should not reach into a sibling's implementation to obtain a
service or bypass its public API.

## Composition

The plugin tree (`XBotv2/xcore.yaml`, global overlays, and workspace overlays)
selects plugin modules, profiles, and configuration. XCore resolves declared
services and mounts the plugin. Application composition publishes the typed
`APPLICATION_INITIALIZED` event after session/Agent facts exist; tool
restriction follows discovery so dynamic Tools can be selected.

Runtime-only state (clients, waiters, jobs, browser pages) belongs to the live
plugin. Persisted conversation and plugin state belong in their storage
contracts. Do not put a live handle or a session-specific object into a
persisted namespace.

`apply` is the composition boundary. It may read declared services from `ctx`,
but constructed services and handlers receive narrow typed dependencies and do
not retain `Context`. Required dependencies are resolved by XCore before
activation; runtime probing is not a replacement for `inject`.

State namespaces are logical ownership boundaries. StateService alone chooses
the `plugin_state` layout and serialized model; plugins never join a data path
or share state files with configuration.

## Tool Pipeline

The standard path is: `BEFORE_TOOL_CALL` rewrite -> schema validation ->
monotonic guards (permissions and plugin guards) -> Tool dispatch ->
`AFTER_TOOL_CALL`. A plugin Tool should enter this path, return `ToolResult`,
and use `ToolCall` metadata only when it genuinely needs call identity.

## Choosing an Extension

- Need model work: register a Tool.
- Need a human slash command: register a `Command`; do not synthesize a ToolCall.
- Need prompt context: use context-builder prompt/component APIs.
- Need a cross-plugin fact: define an owner-typed event or operation.
- Need a public HTTP/SSE/ACP route: add it to the owning package's protocol.
- Need durable per-session values: use `ctx.state.namespace(...)`.
- Need a capability shared with other plugins: provide a declared service.

## Application composition at a glance

The application creates all launch facts, mounts the complete selected tree,
and calls `Context.start()` once. XCore then activates fibers to a dependency
fixpoint. The Agent composition is:

```text
RuntimePaths + launch facts
        │
        ▼
mount bundled tree → data overlay → workspace overlay → session patches
        │
        ▼
Context.start() → session/persistence/tools/model services
        │
        ▼
AgentRuntime creates Engine → session/init(ApplicationInitialized)
        │
        ▼
tool restriction and client transport (HTTP/SSE, TUI, Web, or ACP)
```

The process server and ACP carrier compose their own profile first and create
Agent applications through `agent_application_factory`; they do not share a
single `Context` with every session. A Web or TUI client talks to the public
session/thread protocol and never imports or calls the Engine.

## Runtime services supplied by the composition root

`runtime_log` is supplied by `boot_application`. Agent applications additionally
receive `runtime_paths`, `session_launch`, `agent_options`, `client_events`,
`child_applications`, `parent_permissions`, and a thread-owned `artifacts`
store. Persistence-enabled threads receive `thread_persistence`; a
no-persistence composition receives `thread_metadata` instead. The server
carrier receives `server_options` and `agent_application_factory`; ACP receives
`acp_launch` and the same factory. These names explain why an isolated
`Context(data_dir=...)` test is often pending until the fixture supplies the
right launch facts. See [plugins_list.md](plugins_list.md) for constructors and
provider ownership.

## Session, thread, and loop identities

- `SessionInfo` is the immutable identity/facts value passed in loop events.
- `Session` is the session-level runtime object that owns variables, paths,
  commands, and the `LoopState` view.
- `SessionManager` is the process service that opens, resumes, lists, forks,
  and closes persisted sessions; it is not the Agent loop and is not a plugin
  state namespace.
- `ThreadPersistence` groups the canonical history, inbox, metadata, artifact,
  lifecycle, and StateService ports for one thread.
- `LoopState` is live Agent state. `ConversationHistory` is its canonical
  message surface; the append-only trace and plugin state are separate stores.

The normal lifecycle events are `session/start` or `session/resume`, repeated
turn boundaries (`turn/start` → context/model/tool events → `turn/end`), and
`session/close`. A client reconnects to the session event stream using the
opaque cursor; it does not replay JSONL itself. For storage fields and folding
rules, read [session-trace.md](session-trace.md).

## Event quick reference

These are the stable loop-hook names currently exposed by `Events`. Their
payload is an `EventContext`; only fields relevant to that phase are populated.
The `before/*` hooks are short-circuit points (`ctx.serial`) and may return the
documented replacement/rejection value. The remaining loop events are
observer notifications (`ctx.emit`) and normally return `None`.

| Phase | Names | Common populated fields |
|---|---|---|
| Session | `session/start`, `session/resume`, `session/close` | `session`, `settings`, `messages` |
| Turn | `turn/start`, `turn/end`, `error`, `stop`, `stop/failure` | `continuation`, `turn_complete`, `error`, `stop_reason` |
| User/context | `before/user-message-accept`, `after/user-message-accept`, `before/context`, `after/context` | `user_input`, `messages`, `context_messages`, `rebuild` |
| Agent/model | `before/agent`, `after/agent`, `before/tool-schema-bind`, `after/tool-schema-bind`, `before/model-request`, `model/request-ready`, `after/model-response`, `model/request-error` | `model_request`, `model_response`, `agent_response`, `error` |
| Tools | `before/tools`, `after/tools`, `tool/calls-parsed`, `before/tool-call`, `after/tool-call`, `tool/call-failure`, `tool/denied`, `tool/batch-done`, `agent/inbox/spliced` | `tool_calls`, `tool_call`, `args`, `tool_result`, `tool_results` |
| Messages/client | `user/message`, `assistant/message`, `tool/message`, `client/event`, `state/changed` | `messages`, `client_event` |

Application and owner-specific payloads are separate from `EventContext`:
`session/init` carries `ApplicationInitialized`, `runtime/event` carries
`RuntimeEvent`, context building uses `ContextBuildRequest`/`ContextBuilt`, and
compaction uses `BeforeCompact`/`AfterCompact`. Session/workspace resource
events carry their package-owned typed payloads. Import these from the owning
package; never put a plugin implementation object in `protocol.py`.

## Event payload ownership

Use `EventContext` only for Agent-loop hook data: messages, session facts,
`ModelRequest`, `ModelResponse`, Tool calls/results, and errors. The context
builder, compact, commands, session, application, and workspace packages own
their own typed payload classes. A new plugin event should live beside the
producer's public contract and carry a narrow dataclass or Pydantic model.
Do not export plugin implementation objects from `protocol.py` merely because
a route needs them; inject the service at the route composition boundary and
translate its domain result to a wire model there.
