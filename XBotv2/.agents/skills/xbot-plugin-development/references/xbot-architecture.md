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
