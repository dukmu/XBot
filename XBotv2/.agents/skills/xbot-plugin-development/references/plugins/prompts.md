# `prompts`

The prompt-fragment registry — a per-plugin namespace for registering
prompt text that gets inserted into the context builder's system message
at the appropriate stage. Auto-cleanup is handled by `bound_effect`.

- **Import/profile:** `prompts`, Agent profile.
- **Source:** `XBotv2/prompts/plugin.py`.
- **Injects/provides:** `context_builder` → `prompts` (`PromptsService`).
- **Subscribes to events:** none.
- **Emits:** none directly (delegates to `context_builder`).

## Public data models

### `PromptsService` (`XBotv2/prompts/plugin.py:13-30`)

```python
class PromptsService:
    """Plugin-facing prompt-fragment registry (per-plugin namespace)."""

    def __init__(self, context_builder: Any) -> None:
        self._builder = context_builder

    def add(
        self,
        stage: Any,
        text: str,
        *,
        source: str | None = None,
    ) -> None:
        """Register one fragment for this plugin, with auto-cleanup."""

    def remove(self, stage: Any, plugin_name: str) -> None:
        """Remove a fragment by stage and plugin name."""
```

### `stage` parameter

Must be one of:

```python
PromptFragmentStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]
```

Stages are ordered in the context builder assembly — `system_prefix`
before `system_instructions` before `system_rules` before
`context_suffix`. Plugins cannot override earlier stages with
higher-authority content.

## How `add()` works

```python
def add(self, stage, text, *, source=None):
    plugin_name = current_plugin_name()
    self._builder.register_fragment(stage, plugin_name, text, source=source)
    bound_effect(partial(self.remove, stage, plugin_name))
```

`bound_effect` registers `self.remove(stage, plugin_name)` as a
cleanup callback that fires when the plugin's fiber is unloaded.
This means fragments are **fiber-scoped** — they persist only for
the lifetime of the plugin's activation.

## How `apply()` works

```python
def apply(self, ctx, config=None):
    ctx.set("prompts", PromptsService(ctx.context_builder))
```

The component itself does nothing beyond exposing the service.

## Typical extension: register a prompt fragment

```python
from XBotv2.prompts import PromptFragmentStage

class MyPlugin:
    name = "my-plugin"
    inject = ["prompts", "context_builder"]

    def apply(self, ctx, config):
        ctx.prompts.add(
            stage=PromptFragmentStage.CONTEXT_SUFFIX,
            text="Custom instruction text here.",
            source="my-plugin/instructions.md",
        )
```

The `context_builder` is the actual builder; `prompts` is the
convenience wrapper.

## On-disk artifacts

None. Fragments are in-memory, fiber-scoped, and cleaned up on
plugin unload.

## Cross-references

- Depends on: `context_builder` (delegates `register_fragment` /
  `unregister_fragment`).
- Depended on by: plugins that need to add prompt fragments
  (`subagents` for subagent catalog, `skills` for skill context,
  `workspace-instructions` for workspace rules).
- Pairs with: `context-builder` (the actual builder),
  `workspace-instructions` (uses `ctx.prompts` to register
  workspace rules).

## Common pitfalls

- **Forgetting the `source` parameter**: the `source` is rendered
  into the prompt element attributes but is not required. Omitting
  it means the fragment's origin is lost in debug logs.
- **Using an invalid stage**: `context_builder.register_fragment()`
  raises `ValueError` for stages not in `FRAGMENT_STAGES`.
- **Not using `ctx.prompts` directly**: the `PromptsService` is the
  intended API. Calling `ctx.context_builder.register_fragment()`
  directly bypasses `bound_effect` auto-cleanup.
- **Assuming fragments are session-persistent**: they are
  fiber-scoped and cleaned up when the plugin is unloaded. For
  session-wide fragments, use the `context_builder` directly.
- **Registering the same stage twice from the same plugin**:
  `register_fragment()` overwrites the previous fragment for that
  `(stage, plugin_name)` pair.
