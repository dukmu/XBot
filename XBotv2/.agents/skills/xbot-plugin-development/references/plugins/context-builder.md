# `context-builder`

The prompt context builder — assembles the model-facing system message
from registered components (core instructions, runtime env, agent
identity, plugin fragments, memory, history). Exposes events for
hooking into context construction.

- **Import/profile:** `context-builder`, Agent profile.
- **Source:** `XBotv2/context_builder/plugin.py`,
  `XBotv2/context_builder/builder.py`,
  `XBotv2/context_builder/contracts.py`,
  `XBotv2/context_builder/events.py`.
- **Injects/provides:** `runtime_log` → `context_builder` (`ContextBuilder`).
- **Subscribes to events:** `context/build` (via `ContextBuildHandler`).
- **Emits:** `after/context-components-build` (`ContextComponentsBuilt`),
  `after/context-build` (`ContextBuilt`).

## Public data models

### `ContextBuilder` (`XBotv2/context_builder/builder.py:51-180`)

```python
class ContextBuilder:
    FRAGMENT_STAGES: tuple[PromptFragmentStage, ...] = (
        "system_prefix",
        "system_instructions",
        "system_rules",
        "context_suffix",
    )

    def __init__(self) -> None:
        self._fragments: dict[str, dict[str, _PromptFragment]] = {
            stage: {} for stage in self.FRAGMENT_STAGES
        }

    def register_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
        text: str,
        *,
        source: str | None = None,
    ) -> None:
        """Register one plugin-owned prompt fragment."""

    def unregister_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
    ) -> None:
        """Remove a plugin's fragment."""

    def build(
        self,
        *,
        messages: list[Message],
        agent_name: str = "XBotv2",
        agent_role: str = "",
        user_name: str = "User",
        user_id: str = "default-user",
        developer_instructions: str = "",
        instructions: str = "",
        memory: str = "",
        sandbox_summary: str = "",
        runtime_paths: dict[str, str] | None = None,
        system_notice: str = "",
        turn_count: int = 0,
        active_subagents: int = 0,
    ) -> list[Message]: ...

    def build_components(
        self,
        *,
        messages: list[Message],
        agent_name: str = "XBotv2",
        agent_role: str = "",
        user_name: str = "User",
        user_id: str = "default-user",
        developer_instructions: str = "",
        instructions: str = "",
        memory: str = "",
        sandbox_summary: str = "",
        runtime_paths: dict[str, str] | None = None,
        system_notice: str = "",
        turn_count: int = 0,
        active_subagents: int = 0,
    ) -> list[ContextComponent]: ...

    @staticmethod
    def messages_from_components(
        components: list[ContextComponent]
    ) -> list[Message]: ...

    @staticmethod
    def _sanitize_history(messages: list[Message]) -> list[Message]: ...
```

### `ContextComponent` (`XBotv2/context_builder/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class ContextComponent:
    role: str
    source: str
    content: str
    plugin_name: str | None = None
    stage: PromptFragmentStage | None = None
    source_path: str | None = None
    message: Message | None = None

PromptFragmentStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]
```

### `ContextBuildRequest` / `ContextComponentsBuilt` / `ContextBuilt`

```python
@dataclass(slots=True)
class ContextBuildRequest:
    messages: list[Message]
    session: SessionInfo | None = None
    agent_name: str = "XBotv2"
    agent_role: str = ""
    user_name: str = "User"
    user_id: str = "default-user"
    developer_instructions: str = ""
    instructions: str = ""
    memory: str = ""
    sandbox_summary: str = ""
    runtime_paths: dict[str, str] | None = None
    system_notice: str = ""
    turn_count: int = 0
    active_subagents: int = 0
    context_messages: list[Message] | None = None

@dataclass(slots=True)
class ContextComponentsBuilt:
    components: list[ContextComponent]
    session: SessionInfo | None = None

@dataclass(frozen=True, slots=True)
class ContextBuilt:
    messages: tuple[Message, ...]
    session: SessionInfo | None = None

BEFORE_CONTEXT_BUILD = "before/context-build"
BUILD_CONTEXT = "context/build"
CONTEXT_COMPONENTS_BUILT = "after/context-components-build"
CONTEXT_BUILT = "after/context-build"
```

### `ContextBuildHandler`

```python
class ContextBuildHandler:
    def __init__(
        self,
        builder: ContextBuilder,
        events: Any,
        runtime_log: RuntimeLog,
    ) -> None:
        self._builder = builder
        self._events = events
        self._log = runtime_log.bind("context")

    async def build(self, event: ContextBuildRequest) -> None:
        components = self._builder.build_components(...)
        event.context_messages = self._builder.messages_from_components(components)
        await self._events.emit(CONTEXT_COMPONENTS_BUILT, ContextComponentsBuilt(...))
```

### `CORE_INSTRUCTIONS` (`XBotv2/context_builder/builder.py:17-47`)

The system instruction hierarchy text:

```
1. These core instructions and enforced runtime constraints.
2. Configured developer instructions.
3. The active Agent and workspace instructions.
4. The current human request.
5. Plugin instructions.
6. Summarized history, persistent memory, runtime state, and events.
```

## How `apply()` works

```python
def apply(self, ctx, config=None):
    builder = ContextBuilder()
    ctx.set("context_builder", builder)
    ctx.on(
        BUILD_CONTEXT,
        ContextBuildHandler(builder, ctx, ctx.runtime_log).build,
    )
```

## Context assembly order

1. `core_instructions` (CORE_INSTRUCTIONS text)
2. `runtime_environment` (user identity, paths, sandbox summary)
3. `developer_instructions` (if non-empty)
4. `agent_identity` (Name + Description)
5. `agent_instructions` (if non-empty)
6. Plugin fragments (per stage in order: system_prefix, system_instructions, system_rules, context_suffix)
7. `memory` (if non-empty)
8. `runtime_state` (active subagents count if > 0)
9. History messages (sanitized)

Each component is rendered as a prompt element:

```python
if source == "workspace_instructions":
    return prompt_element("workspace_instructions", content, attributes={...})
elif source == "plugin_fragment":
    return prompt_element("plugin_instruction", content, attributes={...})
elif source in _SYSTEM_COMPONENT_SOURCES:
    return prompt_element(source, content)
else:
    return prompt_element("context_component", content, attributes={...})
```

## `_sanitize_history` logic

```python
def _sanitize_history(messages):
    valid_tool_call_ids = set()
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            valid_tool_call_ids.update(call.id for call in message.tool_calls if call.id)
        elif message.role == "tool":
            if message.tool_call_id and message.tool_call_id in valid_tool_call_ids:
                yield message
        else:
            yield message
```

Drops orphaned tool messages (whose `tool_call_id` doesn't match any
`assistant.tool_calls` from the same session).

## Typical extension: register a plugin fragment

```python
from XBotv2.context_builder import ContextBuilder, PromptFragmentStage

class MyPlugin:
    name = "my-plugin"
    inject = ["context_builder"]

    def apply(self, ctx, config):
        ctx.context_builder.register_fragment(
            stage="context_suffix",
            plugin_name="my-plugin",
            text="Custom instruction text here.",
            source="my-plugin/fragment.md",
        )
```

## Cross-references

- Depends on: `runtime_log`, `agentloop` (consumes BUILD_CONTEXT).
- Depended on by: `agentloop` (context building), `prompts` (fragment
  registration), `context_builder` (the builder itself).
- Pairs with: `prompts` (fragment registration API).

## Common pitfalls

- **Registering fragments in non-existent stages**: only four stages
  are valid (`system_prefix`, `system_instructions`, `system_rules`,
  `context_suffix`). `ValueError` is raised for unknown stages.
- **Plugin fragments have lower authority than core instructions**:
  the stage ordering ensures plugins cannot override core, runtime,
  or agent instructions.
- **`context_messages` in `ContextBuildRequest` is mutable**: the
  handler sets it after building; observers of
  `CONTEXT_COMPONENTS_BUILT` should use the `components` list, not
  `event.context_messages`.
- **`_sanitize_history` drops orphaned tool messages**: if an
  `assistant` message's `tool_calls` are malformed or have no `id`,
  the corresponding `tool` messages are silently dropped.
- **Not calling `unregister_fragment` on cleanup**: fragments persist
  for the lifecycle of the `ContextBuilder` instance. Use
  `unregister_fragment` in `ctx.dispose` to prevent stale content.
