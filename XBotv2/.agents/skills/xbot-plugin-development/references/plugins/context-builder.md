# `context_builder`

Compiles canonical conversation history and prompt components into
provider-facing `ProviderMessage` values. It owns the context compiler and
prompt-component registry; it does not own durable history or context-window
policy.

- **Source:** `XBotv2/context_builder/`.
- **Profile:** Agent.
- **Requires:** `runtime_log`, `artifacts`.
- **Provides:** `ctx.context_builder` (`ContextBuilder`).
- **Event:** handles `BUILD_CONTEXT` (`context/build`) with a typed
  `ContextBuildRequest`, returns provider messages, and emits
  `CONTEXT_COMPONENTS_BUILT` with a `BuiltContext`.

## Current contracts

```python
@dataclass(slots=True)
class ContextBuildRequest:
    history: tuple[ConversationMessage, ...]
    runtime_selection: ResolvedRuntimeSelection
    user_identity: UserContext
    memory: str
    sandbox_summary: str
    runtime_paths: RuntimeVariables
    turn: int

@dataclass(slots=True)
class BuiltContext:
    components: list[ContextComponent]
```

Prompt components are `InlinePromptComponent(stage, source, text)` or
`FilePromptComponent(stage, source, logical_path, text)`. `HistoryComponent`
wraps one canonical `ConversationMessage`. The prompt stages are
`system_prefix`, `system_instructions`, `system_rules`, and `context_suffix`.
The builder supplies core/runtime/agent/memory components, registered plugin
prompt components, then history components; conversion to provider messages
resolves referenced artifacts using the active thread's artifact store.

Register or remove prompt components through
`ctx.context_builder.register_component(owner, component)` and
`unregister_owner(owner)`. Do not mutate conversation history through this
registry or persist resolved absolute artifact paths. Event payloads are
defined in `context_builder.events` and `context_builder.contracts`; there is
no general `SessionInfo`-bearing `ContextBuildRequest`.

See [agentloop](agentloop.md) for the loop lifecycle and
[content cache](content-cache.md) for large-content artifact projection.
