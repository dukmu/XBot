# Architecture Iteration Backlog

This backlog tracks the next architecture iterations. It is ordered to reduce
ambiguity before large implementation changes.

## 1. API Inventory And Behavior Gate

- Keep `xbotv2.api.__all__`, `api_inventory.md`, and public API tests aligned.
- Add signature/serialization checks when a public type becomes part of plugin
  examples or built-in plugin templates.
- Reject new built-in plugin imports from runtime internals.

## 2. C/S Protocol Unification

- HTTP JSON plus SSE `ServerEvent` is now the only active C/S transport model;
  the parallel JSONL frame model and compatibility event alias were removed.
- Server and client use the shared `xbotv2.protocol.sse` codec, and fixtures
  cover every current `ServerEventType`.
- Extend the current `ServerEvent` envelope into one DTO family for
  server-to-client events.
- Message `request_id` now flows through HTTP submission, `Engine.run_turn`,
  every turn-scoped Hook, and all SSE envelopes. Interaction payload ids remain
  distinct from the outer turn correlation id.
- Keep HTTP/SSE as the main transport path until alternate transports share the
  same event contract.
- Add producer-driven integration scenarios for tool call, permission, usage,
  interrupt, and error payloads beyond the complete envelope fixture inventory.
- Treat agent-initiated permission and user-input requests as first-class C/S
  capabilities. The server now registers each request before publishing its
  SSE event, and both response paths share the same coordination invariant.
- Permission requests, user-input requests, recorded responses, and their two
  HTTP request bodies now have typed DTOs while retaining the single
  `ServerEvent` envelope.
- Error, tool-result, and flat usage events now have typed payload DTOs. HTTP
  exception handlers share `ErrorResponse` serialization.
- Every current `ServerEventType` now has a typed payload DTO, and tests require
  `TYPED_SERVER_EVENT_TYPES` to cover the complete known event inventory.
- Unexpected engine exceptions now use `engine_error` with
  `details.exception_type`; keep the documented server-owned code inventory in
  sync when behavior changes.
- Started turns now close after engine errors with `error` followed by
  `turn_finished`. `TerminalSession` consumes the transport-only `end` sentinel,
  and `TuiState` is the single owner of domain state transitions.
- Interaction ids are treated as opaque correlation keys; the TUI associates
  permission acknowledgements with request state instead of parsing id
  prefixes. The HTTP turn bridge explicitly closes its Engine stream and
  cancels it when the SSE consumer disconnects.
- Real-socket tests cover both `ask_user` and permission response round trips,
  including responses that outlive the transport's ordinary read timeout.
- Design reconnectable interactions only after request ownership, expiry,
  replay, and exactly-once response semantics are specified. The current
  protocol explicitly advertises `resume_supported: false`.
- Persisted message history is restored into subsequent provider requests.
  Provider-request tests and a real Minimax TUI process restart verify this
  separately from reconnecting an in-flight interaction.

## 3. Hook Contract Tightening

- Preserve the existing `HookStage` enum values.
- Keep `hook_stage_matrix.md` aligned with every `HookStage` value, including
  category, payload fields, allowed return value, short-circuit behavior,
  failure behavior, and primary callers.
- Prefer existing `HookContext` fields and public types. Introduce another
  payload type only for a repeated contract gap, not for one plugin's local
  convenience.
- Public immutable `ContextComponent` values now back
  `AFTER_CONTEXT_COMPONENTS_BUILD`; Hooks may replace the typed list, and
  invalid entries fail before provider conversion.
- Keep caller-level contract tests for message, tool, and permission stage
  families. `before_tools` now exposes parsed `tool_calls` and the originating
  `agent_response` directly.
- Persistence Hooks now bracket changed-message checkpoints rather than every
  save attempt. Normal completion no longer emits a duplicate unchanged
  checkpoint, while tool batches retain immediate durability.
- Move direct runtime access out of hook contexts only after equivalent plugin
  capabilities exist.
- Engine turn orchestration now delegates message admission, context building,
  model-request preparation, tool batches, and finish behavior to explicit
  stage methods. These methods retain stage-specific return contracts and do
  not introduce a universal Hook result interpreter.

## 4. Plugin Lifecycle Model

- Setup and runtime registrations now share one ownership record; duplicate
  tool keys fail before mutation, and unload removes core resources even when a
  plugin cleanup callback fails.
- A plugin whose `on_load` fails now receives best-effort `on_unload`, allowing
  partial external resources to be released before loader-wide rollback.
- Failures after plugin loading but before bootstrap completes now trigger
  `unload_all`, including failures from runtime-registering `ON_SESSION_INIT`
  hooks.
- Normal session close now attempts close hooks, message persistence, and
  reverse plugin unload even when an earlier close phase fails.
- Manifest `config_schema` and configured values now use Draft 2020-12
  validation before plugin import.
- `PluginStore` now has immediate atomic persistence, uncached snapshot reads,
  explicit YAML mapping validation, and documented unload persistence.
- Continue migrating runtime/dynamic tool registrations to `ctx.plugin_runtime`
  so unload and rollback remain complete.
- Runtime registration and ownership-limited unregistration now share the
  plugin record, allowing dynamic resource providers to implement shorter
  session lifetimes without direct registry access.
- MCP initialization is idempotent and transactional per server. Optional
  failures roll back that server; required failures roll back the complete init
  attempt; session close removes tools and permits a fresh initialization.
- MCP transports now share a required initialize/initialized handshake. Tool
  schemas survive registration, call data survives normalization, and `isError`
  maps to a structured failure. Stdio requests are serialized and response ids
  are checked.
- Treat the current MCP integration as tools-only. Adopt a maintained SDK or
  design a complete Streamable HTTP/session layer before adding resources,
  prompts, server requests, subscriptions, or HTTP SSE.
- Skills discovery is idempotent per loaded session and rolls back every dynamic
  tool from a partial registration attempt.
- Keep built-in plugins using only the public API.

## 5. Tool System Contract

- Canonical registered names are stored on registry entries and exposed through
  command discovery.
- Provider-visible tool names are unique across namespaces, because model tool
  calls do not carry registry namespaces.
- Command discovery exposes registered-name metadata without changing existing
  string selectors.
- Align documented execution metadata with dispatcher behavior.
- Normalize `ToolResult.data`, errors, artifacts, and client events across
  built-in tools.
- Typed `ToolResult.data`, `error`, and `artifacts` now survive runtime
  conversion and appear on `tool_result` events.
- Large tool results use session-relative `artifacts/` paths through a
  read-only virtual mount. Keep logical paths independent of backing locations.
- Unimplemented `execution_mode` and `lock_fields` registration metadata were
  removed. Define batch scheduling, stable output ordering, Hook concurrency,
  live-interaction serialization, and lock semantics before adding a parallel
  API.
- Keep the core built-in tool set small and dependable.

## 6. Built-in Plugin Templates

- `PluginBase` has optional lifecycle defaults, and the built-ins now expose
  consistent cleanup and diagnostics behavior documented in `plugins.md`.
- Token Manager no longer writes statistics into ephemeral `HookContext.state`;
  it uses explicit collector methods and resets plugin-owned memory on unload.
- Keep Skills as the template for prompt/tool capability plugins.
- Keep MCP as the template for external tool provider plugins.
- Keep token manager as the template for policy/observation plugins.
- Keep each built-in plugin documented as an example of the lifecycle model.

## 7. Built-in Workflow Plugins

Implement these as public-API consumers and reference plugins, in this order:

### Compact

- The initial plugin supports a model-visible request tool and automatic
  character-threshold invocation through `BEFORE_CONTEXT`.
- It compacts only before a user-message boundary and preserves recent complete
  turns, including tool calls, results, and the current request.
- Engine-owned atomic persistence makes resume reconstruct the same summary and
  recent tail. Failed auxiliary calls leave the original history intact.
- Automatic compaction has been verified with a real provider. A shared
  token-budget trigger remains; do not duplicate provider tokenizers inside
  the plugin or add another Hook stage.

### Todo List

- The initial plugin provides explicit list, create, update, and remove tools
  for ordered session-scoped items with stable identifiers.
- A single `PluginStore` value makes each mutation one immediate persisted
  write; resume retains the list and next identifier.
- Mutations remain tool-driven and use only `pending`, `in_progress`, and
  `completed`. Real-provider tool selection, permission interaction, and
  post-restart restoration have been verified.

### Goal

- The initial plugin provides explicit create, inspect, update, complete, and
  abandon tools for one durable session objective.
- Only an active goal appends a concise non-persisted `ContextComponent`;
  completed and abandoned goals remain inspectable without entering context.
- Todo items remain concrete work tracking, and automatic continuation remains
  explicitly out of scope. Real-provider tool selection, permission interaction,
  restart recovery, active context injection, and terminal removal are verified.

Each plugin needs lifecycle rollback/unload tests, persistence and resume tests,
structured tool-result tests, public API boundary tests, and current
documentation before it becomes a shipped default.

## 8. Documentation As Implementation

- Keep examples runnable against current CLI and protocol behavior.
- Link each architecture claim to a typed model, test, or concrete file.
- Remove stale phase-plan claims once their replacement documentation exists.
- The unused planning-specific `dag_suffix` fragment name was replaced by the
  domain-neutral `context_suffix`. `PromptFragmentStage` now enumerates all
  supported values, manifests validate them before setup, and the old name is
  rejected rather than retained as a permanent alias.
