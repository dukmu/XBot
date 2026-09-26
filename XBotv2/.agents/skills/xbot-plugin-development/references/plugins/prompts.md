# `prompts`

Registers a plugin's prompt text into the context builder's system message.
It is a thin, fiber-owned facade over the `context_builder` component registry:
`ctx.prompts` is the supported entry point, and ownership/cleanup is derived
from the applying plugin's fiber.

- **Import/profile:** `prompts`, Agent profile.
- **Source:** `XBotv2/prompts/plugin.py`, `contracts.py`.
- **Injects/provides:** `context_builder` → `prompts` (`PromptsService`).
- **Operations / Events / Commands / Routes:** none. This is a synchronous
  in-process service only.

The package exports exactly one public symbol, `PromptsPort`. `PromptsService`
and `PromptsComponent` live in `XBotv2/prompts/plugin.py` and are not
re-exported from the package root — import them from that module when a test
harness needs them.

## Service contract

```python
class PromptsPort(Protocol):
    def add(self, component: PromptComponent) -> None: ...


class PromptsService(PromptsPort):
    """Plugin-facing prompt component registry."""

    def __init__(self, context_builder: PromptComponentRegistry) -> None:
        self._builder = context_builder

    def add(self, component: PromptComponent) -> None:
        plugin_name = current_plugin_name()
        if bound_effect(partial(self.remove, plugin_name)) is False:
            raise RuntimeError(
                "Prompt components must be registered from within a plugin "
                f"apply() (no owning fiber for source={component.source!r}); contribute "
                "per-build components via CONTEXT_COMPONENTS_BUILT instead"
            )
        self._builder.register_component(plugin_name, component)

    def remove(self, plugin_name: str) -> None:
        self._builder.unregister_owner(plugin_name)
```

`add` takes a **component object**, not a stage and text. Registration is only
legal inside a plugin `apply()`: outside an active fiber `bound_effect` returns
`False` and `add` raises `RuntimeError` rather than registering ownerless data.
Per-build content does not belong here — use the `after/context-components-build`
event (`CONTEXT_COMPONENTS_BUILT`) instead.

`remove` is on the concrete service but **not** on `PromptsPort`. The owner key
is the applying plugin's name, so one call removes every component that plugin
registered.

## Component types

Components are declared by `context_builder`, not by this plugin. Import them
from `XBotv2.context_builder`:

```python
PromptStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]


@dataclass(frozen=True, slots=True)
class InlinePromptComponent:
    stage: PromptStage
    source: str
    text: str


@dataclass(frozen=True, slots=True)
class FilePromptComponent:
    stage: PromptStage
    source: str
    logical_path: str
    text: str


PromptComponent: TypeAlias = InlinePromptComponent | FilePromptComponent
```

`PromptComponent` is precisely the union of those two dataclasses — a
`HistoryComponent` is not a `PromptComponent` and is rejected by
`register_component`. There is no `PromptFragmentStage`, no
`register_fragment`, and no `FRAGMENT_STAGES`; the real stage alias is
`PromptStage` and the real registry methods are `register_component` /
`unregister_owner`.

Stages are rendered in `PromptStage` order — `system_prefix`,
`system_instructions`, `system_rules`, `context_suffix`.

## Composition

```python
class PromptsComponent:
    inject = ['context_builder']
    name = "xbot.prompts"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        ctx.set("prompts", PromptsService(ctx.context_builder))


plugin = PromptsComponent()
```

There is no `Config` model and `apply` is synchronous. The component's whole job
is to publish the service.

## Registering a component

```python
from XBotv2.context_builder import InlinePromptComponent


class MyPlugin:
    name = "my-plugin"
    inject = ["prompts"]

    def apply(self, ctx, config=None):
        ctx.prompts.add(
            InlinePromptComponent(
                stage="context_suffix",
                source="my-plugin",
                text="Custom instruction text here.",
            )
        )
```

`source` identifies the contributing plugin in the rendered prompt; it is a
required field of every component, unlike the old optional `source=` keyword.
For text that lives in a file, use `FilePromptComponent(stage=..., source=...,
logical_path=..., text=...)`.

## Lifecycle

Registration is a fiber effect: the component is released when the plugin
unloads, and a failed `apply` rolls the registration back. Nothing is written to
disk — components are in-memory and scoped to the activation.

## Who actually consumes this

Current XBot plugins do **not** read `ctx.prompts`. `workspace-instructions`
subscribes to `after/context-components-build` and appends to the built
component list instead. Treat `prompts` as a public extension point for
third-party plugins, not as the internal path the built-ins use; do not repeat
the claim that `subagents` or `skills` depend on it.

## Pitfalls

- Do not keep a reference to `ctx.prompts` in a long-lived domain object or call
  it from an event callback: registration outside `apply` raises. Contribute
  dynamic content through `after/context-components-build`.
- Do not call `ctx.context_builder.register_component(...)` directly from a
  plugin. Going through `ctx.prompts` is what attributes the registration to
  your fiber and makes cleanup automatic.
- An unknown `stage` is rejected by the builder, so a typo fails at `apply`
  rather than silently rendering nothing.
- Registering twice from the same plugin replaces that owner's components rather
  than appending, because ownership is keyed by plugin name.
