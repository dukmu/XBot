# API Inventory

This inventory records the current supported Python extension surface. It is
kept in sync with `api.__all__` and `tests/core/test_public_api.py`.
Updating the list is allowed, but it must be deliberate, documented, and tested.

## Import Rule

Plugins and external extensions import from:

```python
from api import ...
```

Submodules under `api` may hold the implementation of these types, but
new plugin examples should use the aggregate package unless they need a local
type-only import inside XBotv2 itself.

## Exported Symbols

| Symbol | Kind | Purpose |
|---|---|---|
| `ArtifactRef` | dataclass |
| `AgentDefinition` | dataclass |
| `AgentMode` | type alias |
| `AgentRuntime` | protocol |
| `AgentSession` | protocol |
| `AgentSessionResult` | dataclass |
| `CancelResult` | dataclass |
| `ChildEngineFactory` | type alias |
| `ClientEvent` | dataclass |
| `Command` | dataclass |
| `CommandResult` | dataclass |
| `ContentPart` | type alias |
| `ContextComponent` | dataclass |
| `EventContext` | dataclass | Payload object passed to runtime event listeners (replaces the hook context). |
| `Events` | class | Runtime event names dispatched on the XCore context. |
| `JsonValue` | type alias |
| `ImageContent` | dataclass |
| `ImagePart` | dataclass |
| `InputModality` | type alias |
| `Job` | dataclass |
| `JobContext` | class |
| `JobError` | dataclass |
| `JobId` | type alias |
| `JobKind` | enum |
| `JobNotFound` | exception |
| `JobRegistry` | class |
| `JobRegistryClosed` | exception |
| `JobResult` | dataclass |
| `JobRunner` | protocol |
| `JobStatus` | enum |
| `JobSummary` | dataclass |
| `Message` | dataclass |
| `MESSAGE_FORMAT_KEY` | constant |
| `ModelChunk` | dataclass |
| `ModelResponse` | dataclass |
| `OutputChunk` | dataclass |
| `OutputStore` | protocol |
| `PluginStore` | protocol |
| `ProviderCapabilities` | dataclass |
| `ReasoningPart` | dataclass |
| `PromptFragmentStage` | type alias |
| `calibrated_context_tokens` | function |
| `context_token_limit` | function |
| `estimate_messages_tokens` | function |
| `estimate_request_tokens` | function |
| `prompt_container` | function |
| `prompt_element` | function |
| `RuntimePluginContext` | protocol |
| `RuntimePaths` | dataclass |
| `RuntimeVariables` | mapping |
| `SessionInfo` | dataclass |
| `SessionPaths` | dataclass |
| `ThreadPaths` | dataclass |
| `TextPart` | dataclass |
| `SHORT_CIRCUIT_EVENTS` | frozenset | Events dispatched with ctx.serial (first non-None result is the answer). |
| `StreamOutputStore` | class |
| `SubagentAgentError` | exception |
| `SubagentTurnError` | exception |
| `TERMINAL_STATES` | frozenset |
| `TextOutputStore` | class |
| `ToolAction` | enum | Permission decision for a tool call (returned by before/tool-call). |
| `ToolCall` | dataclass |
| `ToolCallDelta` | dataclass |
| `ToolCallPart` | dataclass |
| `ToolDecision` | dataclass | Decision returned by before/tool-call listeners. |
| `ToolError` | dataclass |
| `ToolResult` | dataclass |
| `Tool` | dataclass |
| `ToolRegistrationOptions` | dataclass |
| `WaitResult` | dataclass |
Plugins are plain XCore plugins (function/object/class) configured through the plugin tree; `apply(ctx, config)` is the plugin body and registrations are fiber effects.
`status_slots()` may return compact `dict[str, str]` display values;
the default is empty and failures do not break session execution.
`EventContext.request_id` carries the current message/turn correlation id for
turn-scoped hooks, including error and persistence hooks. Session lifecycle
hooks use an empty value because they are not owned by one message request.
`EventContext.messages` carries the current persisted history. `POST_COMPACT`
also provides explicit before/after message counts.
Engine-created contexts also expose `invoke_model(messages)` for one unbound
auxiliary provider call. It returns `ModelResponse` without recursively running
model listeners or exposing the provider implementation.
`request_user_input(question, ...)` uses the active C/S interaction channel and
returns the structured live response. It is connection-owned: disconnect
cancels the turn, and resume never restores the pending request.
Persistence event contexts are emitted only for a changed normalized message
snapshot; repeated save attempts with no state change do not emit them.
`PromptFragmentStage` contains `system_prefix`, `system_instructions`,
`system_rules`, and `context_suffix` as compatible ordering zones. They do not
grant authority or describe wire positions. Manifest declarations are validated
against this list before plugin apply. `ctx.prompts.add`
accepts an optional source label which is preserved in `ContextComponent` and
the rendered system envelope.
`EventContext.context_components` exposes a `list[ContextComponent]` at
`AFTER_CONTEXT_COMPONENTS_BUILD`. Components are immutable; the listener may
replace the list with another list of public components. Invalid entries fail
before provider-message conversion.
Model-request listeners inspect `EventContext.model_request`. Transform listeners use
their documented stage-specific return dictionaries for replacements.
Plugin ``apply(ctx)`` registrations are fiber effects: Agent definitions,
event listeners, Tools, Commands, and prompt fragments are undone automatically when the
plugin unloads (XCore lifecycle). Runtime unregister operations can remove only
resources owned by that plugin.
``ctx.variables`` exposes the immutable `RuntimeVariables` mapping
used by Core configuration consumers. Plugins may read and expand it but cannot
add or replace built-in values. Markdown prompt fragments use fenced `var`
blocks through `RuntimeVariables.expand_markdown`; ordinary Markdown references
remain untouched.
Tool registrations may set a positive `timeout_seconds`; the dispatcher applies
it through the normal Tool execution path. `None` leaves the Tool without a
dispatcher deadline, while turn cancellation still cancels the Tool.
Duplicate canonical names or provider-visible tool names are rejected before
registry mutation.
Entered `on_load` callbacks receive best-effort `on_unload` after failure, and
bootstrap failures after loading trigger reverse plugin unload, including
runtime tools created by `SESSION_INIT` listeners.
Plugin `Config` (an `xcore` `S` schema) is validated with defaults applied when the
manifest is parsed, and configured values are validated before plugin import.
Hook declarations accept any event name and reject unknown
targets during manifest parsing. Tool declarations apply the same early
validation to `host` and `sandboxed` execution modes.
Prompt fragment declarations require exactly one non-empty `file` or `handler`;
their stage remains limited to the complete `PromptFragmentStage` inventory.
`PluginStore` mutations persist immediately with atomic replacement; reads are
fresh snapshots and unload does not erase plugin state.
Command discovery exposes human syntax and descriptions. It does not expose
Tool registry identities because commands and Tools have separate dispatch
paths. HTTP/SSE streams use `ServerEvent`, the shared SSE codec, and fixtures covering every current
event type. Every current `KNOWN_SERVER_EVENT_TYPES` member has a typed payload
DTO and is validated when a `ServerEvent` is constructed or decoded;
`TYPED_SERVER_EVENT_TYPES` makes that coverage testable. HTTP failures are
serialized through `ErrorResponse`. Session open responses expose only typed,
display-safe `SessionHistoryItem` values; extension-facing `Message` remains
the richer in-process representation.

## Current Gaps To Improve

- Some `EventContext` fields remain broadly typed. Narrow them when the
  producer and independent consumers share one stable public representation;
  do not add wrapper payload types for plugin-local data.
- Every current `ServerEvent.data` family has a typed DTO. New event types must
  add producer/consumer tests and join `TYPED_SERVER_EVENT_TYPES` deliberately.
- Server-owned error codes are documented and stable in shape; listener-provided
  error events may still use extension-defined string codes.
- Typed and dictionary-returning tool results now preserve data, error,
  artifact, and client-event metadata through runtime normalization; new
  extensions should use `ToolResult` directly.
- Tool registration exposes only behavior the dispatcher enforces. Parallel
  execution and lock metadata are absent until their semantics are implemented.