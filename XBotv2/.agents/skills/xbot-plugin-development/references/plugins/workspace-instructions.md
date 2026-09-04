# `workspace-instructions`

Loads `AGENTS.md` from the workspace root and injects it into the
context builder's system message at the appropriate stage.

- **Import/profile:** `workspace-instructions`, Agent profile.
- **Source:** `XBotv2/workspace_instructions/plugin.py`.
- **Injects/provides:** `variables`, `workspace_root` → (none
  directly; injects into `ContextComponentsBuilt`).
- **Subscribes to events:** `after/context-components-build`
  (`CONTEXT_COMPONENTS_BUILT`).

## Public data models

### `WorkspaceInstructionsPlugin` (`XBotv2/workspace_instructions/plugin.py:12-50`)

```python
class WorkspaceInstructionsPlugin:
    """Contribute ``AGENTS.md`` instructions from one workspace."""

    inject = ["variables", "workspace_root"]
    name = "workspace_instructions"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
        self._variables: RuntimeVariables = ctx.variables
        ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)

    def _inject_workspace_instructions(
        self, event: ContextComponentsBuilt
    ) -> None:
        """Insert the workspace instructions into the correct position."""
```

### `ContextComponent` injection

```python
component = ContextComponent(
    role="system",
    source="workspace_instructions",
    content=text,
    plugin_name=self.name,
    stage="system_instructions",
    source_path="AGENTS.md",
)
```

The component is inserted at position `index` where:

```python
before_sources = {
    "plugin_fragment",
    "memory",
    "runtime_state",
    "history",
}
index = next(
    (i for i, c in enumerate(event.components)
     if c.source in before_sources),
    len(event.components),
)
event.components.insert(index, component)
```

This places workspace instructions **after** plugin fragments but
**before** memory, runtime state, and history. The `stage` is
`"system_instructions"` which matches the `PromptFragmentStage`.

### Text processing

```python
text = self._variables.expand_markdown(
    self._instructions_path.read_text(encoding="utf-8").strip(),
    source="AGENTS.md",
)
```

`${VAR}` and `$VAR` are expanded via `RuntimeVariables.expand_markdown()`.
Empty results are silently skipped.

## How `apply()` works

```python
def apply(self, ctx, config=None):
    self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
    self._variables = ctx.variables
    ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)
```

The plugin does not register tools, commands, or fragments. It
injects the workspace `AGENTS.md` content directly into the
`ContextComponentsBuilt` event, which is emitted after
`build_components()` but before `messages_from_components()`.

## Injection position

```
[components built by ContextBuilder]
├── core_instructions
├── runtime_environment
├── developer_instructions
├── agent_identity
├── agent_instructions
├── plugin_fragment (all stages)
├── [workspace_instructions injected HERE] ←
├── memory
├── runtime_state
└── history
```

The workspace instructions appear after all plugin fragments but
before memory, runtime state, and history — giving them higher
priority than runtime state but lower priority than core instructions.

## Cross-references

- Depends on: `variables`, `workspace_root`, `context_builder`
  (subscribes to `CONTEXT_COMPONENTS_BUILT`).
- Depended on by: the context builder (receives the component).
- Pairs with: `context-builder` (the actual component pipeline),
  `prompts` (fragment registration, but this plugin uses direct
  component injection).

## Common pitfalls

- **`AGENTS.md` must be at workspace root**: the path is
  `Path(ctx.workspace_root) / "AGENTS.md"`. If the file doesn't
  exist, the plugin does nothing (silent no-op).
- **`${VAR}` expansion can fail**: `RuntimeVariables.expand_markdown()`
  raises if a referenced variable is undefined. Use
  `RuntimeVariables.from_roots(...)` to pre-populate known vars.
- **No auto-cleanup**: unlike `PromptsService`, this plugin does
  not use `bound_effect`. The `AGENTS.md` content is read once at
  startup and cached in the component — it is not re-read on
  subsequent turns.
- **`source="workspace_instructions"` in `ContextComponent`**:
  this is a reserved source name. If another plugin uses the same
  source, the context builder's `_render_system_component()` will
  render it as a generic component, not with workspace-specific
  formatting.
- **Insertion index is position-based, not source-based**: if a
  plugin removes components, the insertion index may shift. The
  plugin uses `next(..., len(event.components))` as a fallback,
  which places the component at the end if no `before_sources`
  component is found.
