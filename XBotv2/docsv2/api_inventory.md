# API Inventory

This inventory records the current supported Python extension surface. It is
kept in sync with `xbotv2.api.__all__` and `tests/core/test_public_api.py`.
Updating the list is allowed, but it must be deliberate, documented, and tested.

## Import Rule

Plugins and external extensions import from:

```python
from xbotv2.api import ...
```

Submodules under `xbotv2.api` may hold the implementation of these types, but
new plugin examples should use the aggregate package unless they need a local
type-only import inside XBotv2 itself.

## Exported Symbols

| Symbol | Kind | Purpose |
|---|---|---|
| `ArtifactRef` | dataclass | Tool-produced artifact metadata. |
| `ClientEvent` | dataclass | Client-facing event emitted by tools. |
| `ContextComponent` | dataclass | Immutable, source-tagged context fragment exposed to Hooks. |
| `HookAction` | enum | Hook control-flow action. |
| `HookContext` | dataclass | Stage-specific hook payload envelope. |
| `HookDecision` | dataclass | Guard hook decision with reason/value. |
| `HookStage` | enum | Complete set of current hook stages. |
| `JsonValue` | type alias | JSON-compatible tool data shape. |
| `Message` | dataclass | Provider-neutral conversation message. |
| `ModelChunk` | dataclass | Provider-neutral streaming model chunk. |
| `ModelResponse` | dataclass | Provider-neutral model response. |
| `PluginBase` | class | Plugin lifecycle base class. |
| `PluginConfigError` | exception | Plugin configuration validation failure with plugin name and path. |
| `PluginSetupContext` | protocol | Setup-time registration capabilities. |
| `PluginManifest` | pydantic model | Validated plugin manifest. |
| `PluginStore` | protocol | Per-plugin persistent key-value storage. |
| `PromptFragmentStage` | type alias | Supported plugin prompt insertion stages. |
| `RuntimePluginContext` | protocol | Runtime hook capabilities owned by a plugin record. |
| `RuntimePaths` | dataclass | Server process filesystem layout. |
| `SessionInfo` | dataclass | Session identity and status metadata. |
| `SessionPaths` | dataclass | Per-session filesystem layout. |
| `ToolCall` | dataclass | Parsed tool call request. |
| `ToolCallDelta` | dataclass | Streaming tool call fragment. |
| `ToolError` | dataclass | Structured tool failure. |
| `ToolResult` | dataclass | Tool output, error, artifacts, and events. |
| `Tool` | dataclass | Tool definition and invocation wrapper. |
| `ToolRegistrationOptions` | dataclass | Setup-time plugin tool registration options. |

`PluginBase.on_load` and `PluginBase.on_unload` have safe no-op defaults.
`HookContext.request_id` carries the current message/turn correlation id for
turn-scoped hooks, including error and persistence hooks. Session lifecycle
hooks use an empty value because they are not owned by one message request.
Engine-created contexts also expose `invoke_model(messages)` for one unbound
auxiliary provider call. It returns `ModelResponse` without recursively running
model Hooks or exposing the provider implementation.
Persistence Hook contexts are emitted only for a changed normalized message
snapshot; repeated save attempts with no state change do not emit them.
`PromptFragmentStage` contains `system_prefix`, `system_instructions`,
`system_rules`, and `context_suffix` in render order. Manifest declarations are
validated against this list before plugin setup.
`HookContext.context_components` exposes a `list[ContextComponent]` at
`AFTER_CONTEXT_COMPONENTS_BUILD`. Components are immutable; the Hook may
replace the list with another list of public components. Invalid entries fail
before provider-message conversion.
Model-request Hooks inspect `HookContext.model_request`. Transform Hooks use
their documented stage-specific return dictionaries for replacements.
`PluginSetupContext` owns transactional setup registrations, while
`RuntimePluginContext.register_tool()` records dynamic tools into the same
unload record. `unregister_tool(registered_name)` can remove only a tool in
that plugin's ownership record and removes the matching unload record entry.
Duplicate canonical names or provider-visible tool names are rejected before
registry mutation.
Entered `on_load` callbacks receive best-effort `on_unload` after failure, and
bootstrap failures after loading trigger reverse plugin unload, including
runtime tools created by `ON_SESSION_INIT` hooks.
`PluginManifest.config_schema` is checked as Draft 2020-12 JSON Schema when the
manifest is parsed, and configured values are validated before plugin import.
Hook declarations accept every current `HookStage` value and reject unknown
stages during manifest parsing. Tool declarations apply the same early
validation to `host` and `sandboxed` execution modes.
`PluginStore` mutations persist immediately with atomic replacement; reads are
fresh snapshots and unload does not erase plugin state.
Command discovery exposes `registered_name` and `namespace` metadata while
retaining existing display names and slash invocation values. HTTP/SSE streams
use `ServerEvent`, the shared SSE codec, and fixtures covering every current
event type. Every current `KNOWN_SERVER_EVENT_TYPES` member has a typed payload
DTO and is validated when a `ServerEvent` is constructed or decoded;
`TYPED_SERVER_EVENT_TYPES` makes that coverage testable. HTTP failures are
serialized through `ErrorResponse`. Session open responses expose only typed,
display-safe `SessionHistoryItem` values; extension-facing `Message` remains
the richer in-process representation.

## Current Gaps To Improve

- `HookContext` still carries broad mutable fields. Improve it by documenting
  stage-specific payloads and reusing existing public types. Add a new public
  type only when multiple independent consumers share a gap that existing
  fields cannot express.
- Every current `ServerEvent.data` family has a typed DTO. New event types must
  add producer/consumer tests and join `TYPED_SERVER_EVENT_TYPES` deliberately.
- Server-owned error codes are documented and stable in shape; Hook-provided
  error events may still use extension-defined string codes.
- Typed and dictionary-returning tool results now preserve data, error,
  artifact, and client-event metadata through runtime normalization; new
  extensions should use `ToolResult` directly.
- Tool registration exposes only behavior the dispatcher enforces. Parallel
  execution and lock metadata are absent until their semantics are implemented.
