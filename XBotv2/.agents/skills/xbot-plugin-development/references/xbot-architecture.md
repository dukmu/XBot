# XBot Architecture Reference

This page is the version-matched architecture guide shipped with the skill.
Inspect the installed `XBotv2` package when verifying a pip/uv installation;
the executable package remains authoritative.

## Ownership Map

| Area | Owner | Extension point |
|---|---|---|
| ReAct loop and generic Tool execution | `XBotv2/agentloop` | loop hooks and `ToolsPort` |
| Shared messages, `Tool`, `ToolCall`, `ToolOutcome` | `XBotv2/core` | stable data contracts |
| Session/thread identity and runtime | `XBotv2/session` | session services and protocol |
| Agent definitions and active Agent selection | `XBotv2/agents` | Agent declarations and typed events |
| Child application lifecycle | `XBotv2/application` | `ChildApplication*` contracts |
| Prompt assembly | `XBotv2/context_builder` | typed context components |
| Transport routes and wire models | owning package `protocol.py` | FastAPI/transport router |
| Permission, sandbox, interaction, persistence | their named plugins | declared services and typed events |
| Filesystem and Shell Tools | `XBotv2/coretools` | session-bound Tool factories |

Core does not import a concrete built-in plugin. A plugin may import stable
contracts, but should not reach into a sibling's implementation to obtain a
service or bypass its public API.

## Composition

The plugin tree (`XBotv2/xcore.yaml`, data-directory `plugins.yaml`, workspace
`.xbot/plugins.yaml`, session `config.yaml`, and in-memory launch overlays)
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

Each capability has one tree identity and exports `plugin` only from its root
`plugin.py`. A capability that participates in more than one carrier profile
uses named `ctx.inject(...)` callbacks inside that root plugin. For example,
the Agents plugin mounts its Agent runtime when loop dependencies exist and
mounts its own router when `server` and `sessions` exist. Do not represent
those facets as `agents/runtime/plugin.py` or `agents/http/plugin.py`.

State namespaces are logical ownership boundaries. StateService alone chooses
the `plugin_state` layout and serialized model; plugins never join a data path
or share state files with configuration.

## Tool Pipeline

The standard path validates provider arguments, applies registered guards,
dispatches the Tool, and records the resulting `ToolExecution`. A plugin Tool
should enter this path, return a `ToolOutcome`, and use `ToolCall` metadata only
when it genuinely needs call identity.

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

- `SessionRuntimeState` carries current runtime identity/facts in loop events;
  client-facing summaries are separate projections.
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

## Event ownership and dispatch

The current loop event names are the constants in `XBotv2.agentloop.Events`.
Payloads are producer-owned typed objects, not an `EventContext` with a bag of
optional fields. Subscribe with `ctx.on`; observer callbacks receive their
declared payload and return no replacement. Only names listed in
`SHORT_CIRCUIT_EVENTS` use serial dispatch, and each has its own result type.
The current vocabulary and stage boundary are summarized in the
[agentloop reference](plugins/agentloop.md); do not copy older hook names from
archived design material.

Internal XCore loop events, application `RuntimeEvent` values, and validated
client wire events are separate contracts. Context building, compaction,
commands, session, application, and workspace packages own their business
payloads. Define new event or operation contracts beside their semantic owner.
Keep HTTP request/response models and routes in the owning package's
`protocol.py`, where an adapter translates a domain result to the wire model.
