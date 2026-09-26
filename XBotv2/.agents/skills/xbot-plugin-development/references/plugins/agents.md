# `agents`

Session-scoped active Agent runtime. Creates the `Engine` (loop instance),
manages agent/provider/model selection, and registers runtime operations
(`LIST_AGENTS`, `SELECT_AGENT`, `SELECT_PROVIDER`, `SELECT_EFFORT`).

- **Import/profile:** tree id/import name `agents`; Agent and server profiles.
- **Source:** `XBotv2/agents/plugin.py` (the only plugin export),
  `contracts.py`, `events.py`, `protocol.py`, `catalog.py`, `service.py`, and
  `commands.py`.
- **Injects/provides:** `agent_catalog`, `agent_loop_factory`, `settings`,
  `llm`, `model`, `tools`, `artifacts`, `loop_state`, `agent_inbox`,
  `commands`, `agent_options`, `runtime_log` → `agent_runtime`
  (`AgentsService`), `engine` (`Engine`).
- The catalog mount separately consumes `data_root`, `variables`, and
  `workspace_root`. Runtime activation is gated on `agent_inbox`, so the engine
  is only built once restored input is available — plugin-tree order is not what
  decides the mount point.
- **Subscribes to events:** none in `register()`; operations registered
  on `LIST_AGENTS`, `SELECT_AGENT`, `SELECT_PROVIDER`, `SELECT_EFFORT`.
- **Operations:** `agents/list` (`LIST_AGENTS`), `agents/select`
  (`SELECT_AGENT`), `llm/provider/select` (`SELECT_PROVIDER`), and
  `llm/effort/select` (`SELECT_EFFORT`). The last two are owned by `llm` and
  re-bound here against the active runtime.

## Public contracts and internal implementation

### `AgentsService` (`XBotv2/agents/service.py`)

```python
class AgentsService(AgentRuntimePort):
    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        factory: AgentLoopFactoryPort,
        events: ApplicationEventsPort,
        state: LoopState,
        settings: SettingsPort,
        providers: LlmServicePort,
        model: ModelPort,
        tools: ToolsPort,
        artifacts: ArtifactStorePort,
        inbox: AgentInbox,
        runtime_log: RuntimeLog,
    ) -> None: ...

    async def create(self, options: AgentCreateOptions) -> AgentLoopDriverPort: ...

    def current_selection(self) -> AgentSelection: ...

    async def select(self, name: str) -> AgentSelection: ...

    async def select_provider(
        self, name: str, model: str | None = None
    ) -> ProviderSelection: ...

    async def select_effort(self, value: str) -> EffortSelection: ...
```

Every constructor parameter is keyword-only. The service takes the durable
`inbox: AgentInbox`, not a thread-metadata state object.

### `AgentRuntimeOperations` (`plugin.py`)

```python
class AgentRuntimeOperations:
    def __init__(self, service: AgentsService, catalog: AgentCatalogPort) -> None: ...

    def list_agents(self, _request: EmptyRequest) -> AgentCatalogData: ...

    async def select_agent(self, request: SelectAgent) -> AgentSelection: ...

    async def select_provider(self, request: SelectProvider) -> ProviderSelection: ...

    async def select_effort(self, request: SelectEffort) -> EffortSelection: ...

    def register(self, ctx: Context) -> None:
        for command in build_agent_commands(self._service, self._catalog):
            ctx.commands.register(command)
        ctx.on(LIST_AGENTS.name, self.list_agents)
        ctx.on(SELECT_AGENT.name, self.select_agent)
        ctx.on(SELECT_PROVIDER.name, self.select_provider)
        ctx.on(SELECT_EFFORT.name, self.select_effort)
```

`list_agents` returns the wire model `AgentCatalogData`, and it filters out
`hidden` definitions.

### `AgentDefinition` / `AgentSelection` / `AgentCreateOptions`

```python
class AgentDefinition(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: str = Field(min_length=1)
    mode: AgentMode = "subagent"
    prompt: str = ""
    model_policy: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    limits: AgentExecutionLimits = Field(default_factory=AgentExecutionLimits)
    permission_policy: PermissionPolicy = Field(
        default_factory=lambda: PermissionPolicy(default_decision="allow")
    )
    tool_policy: AgentToolPolicy = Field(default_factory=AgentToolPolicy)
    hidden: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class AgentSelection:
    active: str
    provider: str
    model: str
    model_mode: str
    context_window: int


@dataclass(frozen=True, slots=True)
class AgentCreateOptions:
    session_id: str
    thread_id: str
    workspace_root: str
    provider_name: str | None = None
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    model_override: BaseProvider | None = None
    parent_thread_id: str = ""
    is_subagent: bool = False
```

`AgentDefinition` nests its policy objects; see
[the catalog page](agent-catalog.md) for `model_policy`, `limits`,
`permission_policy`, and `tool_policy` field-by-field. `provider_name` defaults
to `None`, meaning "use the configured default".

### `AgentCatalogData` / `AgentCatalog`

```python
@dataclass(frozen=True, slots=True)
class AgentCatalog:
    active: str
    agents: tuple[AgentDefinition, ...]
```

`plugin.py` imports this model as `AgentCatalogData` (via
`from XBotv2.agents.contracts import AgentCatalog as AgentCatalogData`) to
distinguish it from the mutable `AgentCatalog` store in `agents/catalog.py`:

```python
from XBotv2.agents.contracts import AgentCatalog as AgentCatalogData
```

The `agents/list` operation returns the immutable contracts projection, not the
mutable store.

Child application handles do not belong to the Agent catalog. Import
`ChildApplication`, `ChildApplicationResult`, and `ChildApplicationsPort`
from `XBotv2.application` when implementing a child-runtime owner.

### `LIST_AGENTS` / `SELECT_AGENT` / `SELECT_PROVIDER` / `SELECT_EFFORT`

`LIST_AGENTS` and `SELECT_AGENT` are declared in
`XBotv2/agents/contracts.py`; the provider and effort operations are declared in
`XBotv2/llm/contracts.py` and re-bound here against the active runtime.

```python
# XBotv2/agents/contracts.py
LIST_AGENTS = Operation("agents/list", EmptyRequest, AgentCatalog)
SELECT_AGENT = Operation("agents/select", SelectAgent, AgentSelection, exclusive=True)

# XBotv2/llm/contracts.py
SELECT_PROVIDER = Operation("llm/provider/select", SelectProvider, ProviderSelection, exclusive=True)
SELECT_EFFORT = Operation("llm/effort/select", SelectEffort, EffortSelection, exclusive=True)
```

The wire names of the provider/effort operations are `llm/provider/select` and
`llm/effort/select`. All three selection operations are `exclusive=True`: they
refuse to run while the user's input is pending rather than reconfiguring a
busy thread.

## How `apply()` works

```python
async def mount_runtime(ctx: Context) -> None:
    service = AgentsService(
        catalog=ctx.agent_catalog,
        factory=ctx.agent_loop_factory,
        events=ctx, state=ctx.loop_state, settings=ctx.settings,
        providers=ctx.llm, model=ctx.model, tools=ctx.tools,
        artifacts=ctx.artifacts, inbox=ctx.agent_inbox,
        runtime_log=ctx.runtime_log,
    )
    ctx.set("agent_runtime", service)
    engine = await service.create(ctx.agent_options)
    ctx.set("engine", engine)
    AgentRuntimeOperations(service, ctx.agent_catalog).register(ctx)
```

The root `AgentsPlugin.apply()` mounts this named callback only when its Agent
dependencies exist, mounts `mount_catalog` for catalog dependencies, and
mounts the capability's HTTP router when `server` and `sessions` exist.

## Typical extension: list available agents

```python
from XBotv2.agents import LIST_AGENTS, AgentCatalog
from XBotv2.core.operations import EmptyRequest, OperationContext, dispatch_operation

class AgentAwareHandler:
    def __init__(self, events: OperationContext):
        self._events = events

    async def inspect(self, _event) -> None:
        catalog = await dispatch_operation(self._events, LIST_AGENTS, EmptyRequest())
        # catalog is AgentCatalog: catalog.active, catalog.agents
        ...

class AgentAwarePlugin:
    def apply(self, ctx, config):
        handler = AgentAwareHandler(ctx)
        ctx.on("example/inspect-agents", handler.inspect)
```

For cross-plugin requests, prefer the typed `LIST_AGENTS` operation through an
`OperationContext`; do not assume `AgentRuntimePort` exposes a generic
`dispatch` method and do not retain `ctx` in a nested closure.

## Cross-references

- Depends on: `agent_catalog`, `agent_loop_factory`, `settings`,
  `llm`, `model`, `tools`, `artifacts`, `loop_state`, `agent_inbox`,
  `commands`, `agent_options`, `runtime_log`.
- Depended on by: the LLM command facet (delegates to `AgentRuntimePort`) and
  the Agents-owned HTTP facet.
- Pairs with: its catalog facet, `subagents`, and `agentloop`
  (creates the Engine).

## Common pitfalls

- **Importing `AgentsService` directly**: depend on the
  `AgentRuntimePort` Protocol instead.
- **Assuming `current_selection().active` reflects the *agent* name**:
  it is the agent name; `provider` and `model` are the current model
  binding.
- **Calling `create()` twice**: the service holds the engine; a second
  call creates a new one without closing the previous.
- **Registering operations without `dispatch`**: `dispatch` is the
  standard way to invoke them; direct method calls bypass the event
  pipeline.
