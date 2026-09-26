# `workspace_instructions`

Loads `AGENTS.md` from the workspace root and contributes it as an ordinary
prompt component for each context build.

- **Tree id/name:** `workspace_instructions` / `workspace_instructions`
  (the page filename is `workspace-instructions.md`); Agent profile.
- **Source:** `XBotv2/workspace_instructions/plugin.py` (the package's only
  implementation module).
- **Injects/provides:** `variables`, `workspace_root` → (none directly;
  appends a component to `BuiltContext`).
- **Subscribes to events:** `after/context-components-build`
  (`CONTEXT_COMPONENTS_BUILT`).

## `WorkspaceInstructionsPlugin` (`XBotv2/workspace_instructions/plugin.py`)

```python
class WorkspaceInstructionsPlugin:
    """Contribute ``AGENTS.md`` instructions from one workspace."""

    inject = ["variables", "workspace_root"]
    name = "workspace_instructions"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
        self._variables: RuntimeVariables = ctx.variables
        ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)

    def _inject_workspace_instructions(self, event: BuiltContext) -> None: ...


plugin = WorkspaceInstructionsPlugin()
```

The event payload is a `BuiltContext`, not a `ContextComponentsBuilt` class —
there is no such type. The listener return type is `None`; it is a synchronous
`ctx.on` handler.

## The contributed component

The listener appends exactly one `FilePromptComponent` to `event.components`:

```python
component = FilePromptComponent(
    stage="system_instructions",
    source=self.name,           # "workspace_instructions"
    logical_path="AGENTS.md",
    text=text,
)
event.components.append(component)
```

`ContextComponent` is a type alias in `XBotv2/context_builder/contracts.py`
(`ContextComponent: TypeAlias = PromptComponent | HistoryComponent`), **not** a
class, and it has no `role`, `content`, `plugin_name`, or `source_path` fields.
Construct the concrete dataclass instead:

```python
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


@dataclass(frozen=True, slots=True)
class HistoryComponent:
    message: ConversationMessage


PromptComponent: TypeAlias = InlinePromptComponent | FilePromptComponent
ContextComponent: TypeAlias = PromptComponent | HistoryComponent
```

`PromptStage = Literal["system_prefix", "system_instructions",
"system_rules", "context_suffix"]`. There is no `PromptFragmentStage` type;
`stage` is a `PromptStage`.

## Injection position

There is **no** index-computation algorithm. The plugin does not scan for
`before_sources`, does not compute an index, and never calls
`event.components.insert(...)`. It appends:

```python
event.components.append(component)
```

Position in the built list therefore does not decide prompt order. Ordering is
decided later by `ContextBuilder.messages_from_components`, which separates
prompt components from history components and then sorts the prompt components
by `PROMPT_STAGES.index(component.stage)`, where

```python
PROMPT_STAGES = ("system_prefix", "system_instructions", "system_rules", "context_suffix")
```

Because this component uses `stage="system_instructions"`, it is rendered with
the other system instructions regardless of where it sits in
`event.components`. Do not document or rely on an "inserted between plugin
fragments and memory" position: that claim does not describe the code.

## Rendering

`_render_system_component()` decides the prompt element name from the component
`source`:

```python
_NAMED_PROMPT_SOURCES = frozenset({
    "core_instructions",
    "runtime_environment",
    "agent_identity",
    "agent_instructions",
    "memory",
})
```

`workspace_instructions` is **not** in that set. The claim that it is a reserved,
specially formatted source is inverted: this plugin's component always renders
through the generic path, producing a `plugin_instruction` element with
`name="workspace_instructions"`, `stage="system_instructions"`, and
`source="AGENTS.md"` (the `FilePromptComponent.logical_path`). Only the five
named sources above render as `prompt_element(component.source, component.text)`.
An `InlinePromptComponent` rendered generically carries no `source` attribute at
all, only `name` and `stage`.

## Text processing

```python
text = self._variables.expand_markdown(
    source_text.strip(),
    source="AGENTS.md",
)
if not text:
    return
```

`RuntimeVariables.expand_markdown(value, *, source=...)` does **not** expand
`${VAR}` / `$VAR` references inline. It replaces only *explicit Markdown variable
blocks* — a fenced ```` ```var ```` block whose entire content is `${NAME}` — and
raises for an undefined name. Ordinary `${VAR}` text inside `AGENTS.md` is left
untouched; `RuntimeVariables.expand()` is the general reference expander. Empty
text is skipped and contributes nothing.

Reading is UTF-8 only and failure is loud, not silent:

- a missing or non-file `AGENTS.md` is the only silent no-op
  (`if not self._instructions_path.is_file(): return`);
- non-UTF-8 content re-raises as `UnicodeError` naming the path;
- any other read failure re-raises as `OSError` naming the path.

## Cross-references

- Depends on: `variables`, `workspace_root`; subscribes to
  `CONTEXT_COMPONENTS_BUILT` from `context_builder`.
- Depended on by: the context builder pipeline — `ContextBuildHandler.build`
  emits the event, then `messages_from_components` consumes the appended
  component.
- Pairs with: `context-builder` (the component pipeline) and `prompts`
  (fragment registration — this plugin does not use it).

## Common pitfalls

- **`AGENTS.md` must be at the workspace root**: the path is
  `Path(ctx.workspace_root) / "AGENTS.md"`, resolved once at `apply()` time.
- **This is not a cache**: the file is read on every
  `CONTEXT_COMPONENTS_BUILT` event, so edits are visible on the next context
  build. There is no startup-only snapshot and no `bound_effect` registration.
- **Do not use `insert()` to "position" a component**: only `stage` ordering
  matters to `messages_from_components`. Appending is sufficient and is what this
  plugin does.
- **Do not expect named-source formatting**: `source` must literally be one of
  `core_instructions`, `runtime_environment`, `agent_identity`,
  `agent_instructions`, or `memory` to render as a named prompt element;
  `workspace_instructions` is not one of them.
- **Missing variables raise**: `expand_markdown` calls `_require(name, source)`,
  so a ```` ```var ```` block naming an unpopulated variable raises `ValueError`
  rather than leaving the text alone.
