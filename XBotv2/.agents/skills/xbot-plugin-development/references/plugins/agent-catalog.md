# `agent-catalog`

Immutable Agent definition catalog with base and workspace-overlay
layers. Each agent has a `name`, `description`, `mode`, optional
provider/model/temperature overrides, permissions, and tool selections.

- **Import/profile:** `agent-catalog`, Agent profile.
- **Source:** `XBotv2/agents/catalog_component.py`,
  `XBotv2/agents/catalog.py`,
  `XBotv2/agents/loader.py`,
  `XBotv2/agents/builtins.py`,
  `XBotv2/agents/contracts.py`.
- **Injects/provides:** `data_root`, `variables`, `workspace_root` →
  `agent_catalog` (`AgentCatalog`).
- **Operations:** `LIST_AGENTS`, `SELECT_AGENT`.

## Public data models

### `AgentCatalog` (`XBotv2/agents/catalog.py:17-90`)

```python
class AgentCatalog:
    def __init__(self) -> None:
        self._base: dict[str, AgentDefinition] = {}
        self._base_owners: dict[str, str] = {}
        self._overlay: dict[str, AgentDefinition] = {}
        self._overlay_owners: dict[str, str] = {}

    def register(
        self,
        definition: AgentDefinition,
        *,
        overlay: bool = False,
    ) -> str: ...               # returns definition.name

    def register_markdown(
        self,
        directory: Path,
        *,
        variables: RuntimeVariables | None = None,
        overlay: bool = True,
        owner: str | None = None,
    ) -> tuple[str, ...]: ...   # returns registered names

    def unregister_owned(
        self,
        owner: str | None = None,
        *,
        overlay: bool = True,
    ) -> list[str]: ...

    def get(self, name: str) -> AgentDefinition | None: ...

    def definitions(self) -> tuple[AgentDefinition, ...]: ...
```

Overlay wins over base for `get()`. `definitions()` merges both layers.

### `AgentDefinition` (`XBotv2/agents/contracts.py`)

```python
class AgentDefinition(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: str = Field(min_length=1)
    mode: AgentMode = "subagent"
    prompt: str = ""
    provider: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, gt=0)
    permissions: dict[str, JsonValue] = Field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    hidden: bool = False
```

`AgentMode = Literal["primary", "subagent", "all"]`.
`mode="subagent"` means the agent can be spawned as a subagent;
`"primary"` is the default agent for the session.

### `load_definitions` (`XBotv2/agents/loader.py`)

```python
def load_definitions(
    directory: Path,
    variables: RuntimeVariables | None = None,
) -> list[AgentDefinition]: ...
```

Loads `.agents/` markdown files as `AgentDefinition` instances.

### `BUILTIN_AGENT_DEFINITIONS` (`XBotv2/agents/builtins.py`)

Pre-defined agents shipped with XBot. `apply()` in the catalog component
registers them first, then loads `.agents/` from `data_root/.agents`,
then loads workspace `.agents/` as overlay.

## How `apply()` works (`AgentCatalogComponent`)

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    catalog = AgentCatalog()
    definitions = {
        d.name: d for d in BUILTIN_AGENT_DEFINITIONS
    }
    definitions.update({
        d.name: d for d in load_definitions(
            Path(ctx.data_root) / ".agents",
            ctx.variables,
        )
    })
    for definition in definitions.values():
        catalog.register(definition)
    catalog.register_markdown(
        Path(ctx.workspace_root) / ".agents",
        variables=ctx.variables,
        overlay=True,
    )
    ctx.set("agent_catalog", catalog)
```

Three layers: builtins → `data_root/.agents/` → `workspace_root/.agents/`
(overlay).

## Typical extension: register a custom agent

```python
from XBotv2.agents.contracts import AgentDefinition

class CustomAgentPlugin:
    name = "custom-agent"
    inject = ["agent_catalog"]

    def apply(self, ctx, config):
        ctx.agent_catalog.register(AgentDefinition(
            name="code-reviewer",
            description="Reviews code changes",
            mode="subagent",
            provider="openai",
            model="gpt-4",
        ))
```

## Cross-references

- Depends on: `data_root`, `variables`, `workspace_root`.
- Depended on by: `agent-runtime` (list/select), `subagents` (spawn),
  `agent-runtime` HTTP routes.
- Pairs with: `agent-runtime` (selection binding).

## Common pitfalls

- **Registering the same agent name twice**: raises `ValueError`.
  Base and overlay share the same name space — overlay wins on `get()`
  but both must be unique within their layer.
- **Using `hidden=True` and expecting the agent to appear in lists**:
  `LIST_AGENTS` filters out hidden agents. Use `hidden=True` to
  register system agents that are selectable but not UI-visible.
- **Setting `mode="primary"` on an agent that also appears in
  `BUILTIN_AGENT_DEFINITIONS`**: the overlay wins, which may
  unexpectedly override builtins.
