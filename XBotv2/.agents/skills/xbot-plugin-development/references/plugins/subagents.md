# `subagents`

Model-facing subagent job tools. Subagents run as `kind="subagent"` jobs in the
shared `JobRegistry`; this plugin only owns the adapter that requests a child
application from the application's child-lifecycle service, plus the typed
model-facing Tools. It holds no job state — waiting, cancellation, results, and
listing all live in the registry.

- **Import/profile:** `subagents`, Agent profile.
- **Source:** `XBotv2/subagents/plugin.py` (the plugin and runtime component),
  `XBotv2/subagents/contracts.py` (`SubagentsConfig`, `SubagentAgentError`),
  `XBotv2/subagents/service.py` (`AgentJobSpec`, `AgentJobResult`,
  `SubagentLauncher`, `SubagentRunner`, `SubagentTools`, `SubagentCatalogPrompt`).
- **Injects/provides:** `session`, `agent_catalog`, `child_applications`,
  `permissions`, `client_events`, `jobs`, `tools`, `thread_persistence` →
  (none directly; registers Tools).
- **Subscribes to events:** `CONTEXT_COMPONENTS_BUILT`; contributes the currently
  visible subagent catalog to each context build.
- **Config:** `timeout_seconds` (default 600.0s, must be `> 0`).

There is **no** `prompts` dependency. `SubagentsRuntimeComponent.inject` lists
exactly the eight services above, and the package never reads `ctx.prompts`; the
catalog reaches the prompt through `after/context-components-build`, not by
consuming `PromptsService`.

## Configuration (`XBotv2/subagents/contracts.py`)

```python
class SubagentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=600.0, gt=0)


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"
```

`timeout_seconds` is applied to the `spawn_subagent` Tool registration
(`ctx.tools.register(Tool.from_function(handlers.spawn_subagent),
timeout_seconds=timeout_seconds)`); the other four Tools take the default.

## Plugin composition (`XBotv2/subagents/plugin.py`)

```python
class SubagentsRuntimeComponent:
    inject = [
        "session", "agent_catalog", "child_applications", "permissions",
        "client_events", "jobs", "tools", "thread_persistence",
    ]
    name = "xbot.subagents"

    def apply(self, ctx: Context, config: SubagentsConfig) -> None: ...


class SubagentsPlugin:
    """Mount subagent support only when thread persistence is available."""

    name = "xbot.subagents"
    Config = SubagentsConfig

    async def apply(self, ctx: Context, config: SubagentsConfig) -> None:
        await ctx.plugin(SubagentsRuntimeComponent(), config)


plugin = SubagentsPlugin()
```

`SubagentsConfig` is declared in `XBotv2/subagents/contracts.py` and is the single
source for its JSON Schema and runtime defaults.

The runtime component builds one `SubagentLauncher` from `ctx.agent_catalog`,
`ctx.session`, `ctx.child_applications`, `ctx.thread_persistence.lifecycle`,
`ctx.permissions`, and `ctx.client_events`, then registers the five handler Tools
through `Tool.from_function`.

## Public data models (`XBotv2/subagents/service.py`)

```python
@dataclass(frozen=True, slots=True)
class AgentJobSpec:
    agent: str
    prompt: str
    label: str
    kind: str = "subagent"


@dataclass(frozen=True, slots=True)
class AgentJobResult:
    child: ChildApplicationResult
```

`AgentJobSpec` satisfies the `JobSpec` protocol (`kind` + `label`), which is why
`registry.create(spec=AgentJobSpec(...))` needs no separate job-spec type.

### `SubagentLauncher`

```python
class SubagentLauncher:
    """Resolve definitions and request child applications for subagent jobs."""

    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        session: SessionPort,
        children: ChildApplicationsPort,
        lifecycle: ThreadLifecycleWriterPort,
        parent_permissions: PermissionsPort,
        client_events: ClientEventsPort | None,
    ) -> None: ...

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> ChildApplication: ...
```

There is no `self._active: list[ChildApplication] = []` attribute. The launcher
stores only the six collaborators above and deliberately keeps no reference to
spawned children: the source comment states that the child's lifecycle belongs to
the executor, so completed children are not pinned for the session. It also does
not "append the result" anywhere. `parent_job_id` is accepted and immediately
discarded (`del parent_job_id`).

`spawn_subagent` validates, then delegates:

- `catalog.get(agent)` must return a definition whose `mode` is not `"primary"`,
  otherwise `SubagentAgentError(f"Unknown subagent: {agent}")`.
- An empty or whitespace-only prompt raises
  `SubagentAgentError("Subagent prompt cannot be empty")`.
- Otherwise it calls
  `children.spawn(ChildApplicationRequest(definition=..., thread_id=session.new_thread_id(definition.name), prompt=..., parent_permissions=..., client_events=...), lifecycle)`
  and returns the `ChildApplication`.

### `SubagentRunner`

```python
class SubagentRunner:
    """Runs one SUBAGENT job through a spawned child session."""

    def __init__(
        self, *, session: SessionPort, agent: str, prompt: str
    ) -> None:
        self.session = session
        self.agent = agent
        self.prompt = prompt
        self._child: ChildApplication | None = None

    async def run(self, job: Job) -> AgentJobResult: ...
    async def cancel(self, job: Job) -> None: ...
```

The parameter is named `session` and annotated `SessionPort`, but the plugin
always constructs it as `SubagentRunner(session=self._launcher, ...)`: the runtime
object is the `SubagentLauncher`, and `self.session.spawn_subagent(...)`
dispatches to `SubagentLauncher.spawn_subagent`. `SessionPort` itself declares no
`spawn_subagent` method — do not read the annotation as the actual collaborator.
`run()` passes `parent_job_id=job.parent_job_id`, awaits `session.wait()`, and
returns `AgentJobResult(child=result)`. `cancel()` is a no-op unless a child was
latched, in which case it awaits `self._child.cancel()`.

### `SubagentTools`

```python
class SubagentTools:
    """Named handlers for the subagent job tool surface."""

    def __init__(
        self,
        *,
        registry: JobsPort,
        launcher: SubagentLauncher,
        catalog: AgentCatalogPort,
    ) -> None: ...

    async def spawn_subagent(
        self, agent: str, prompt: str, name: str | None = None
    ) -> ToolOutcome: ...
    async def list_subagents(self, status: str | None = None) -> ToolOutcome: ...
    async def wait_subagent(
        self, ids: list[str] | None = None,
        mode: str = "all", timeout_ms: int | None = None
    ) -> ToolOutcome: ...
    async def read_subagent(
        self, id: str, cursor: int | None = None, max_chars: int = 8000
    ) -> ToolOutcome: ...
    async def cancel_subagent(self, id: str) -> ToolOutcome: ...
```

Each handler's docstring becomes its Tool description, because
`Tool.from_function` reads `inspect.getdoc(function)`.

- `spawn_subagent` returns `failed_text("session_closing", ...)` when
  `registry.closing`, `failed_text("agent_not_found", ...)` for an agent not in
  `catalog.definitions()`, and `failed_text("invalid_prompt", ...)` for a blank
  prompt. On success it returns
  `succeeded_text(f"Started {job.id} (status: {job.status})")`.
- `list_subagents` calls `registry.list(kind="subagent", status=...)` and reports
  only a count: `f"{len(summaries)} subagent job(s)"`.
- `wait_subagent` rejects a `mode` outside `{"all", "any"}` with
  `failed_text("invalid_mode", ...)`; when neither `ids` nor any live `subagent`
  job exists it returns
  `failed_text("subagent_not_found", "No subagent jobs to wait for")`; an unknown
  id surfaces `JobNotFound` as
  `failed_text("subagent_not_found", "Unknown subagent job id")`. Success is
  `succeeded_text("Wait complete")`. `timeout_ms` is converted to seconds.
- `read_subagent` requires a `subagent`-kind job, otherwise
  `failed_text("subagent_not_found", ...)`. With a non-`AgentJobResult` result it
  reports `job.error` when present, else `succeeded_text("No response captured
  yet")`. Otherwise it slices `result.child.final_response` from `cursor`
  (clamped to `>= 0` and `<= len(...)`) for `max_chars` characters.
- `cancel_subagent` requires a `subagent`-kind job and then returns
  `succeeded_text(f"Subagent {id} {result.status}")` from
  `registry.cancel(id)`.

### `SubagentCatalogPrompt`

```python
class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort) -> None: ...

    def contribute(self, event: BuiltContext) -> None: ...
```

`contribute` filters `catalog.definitions()` to entries whose `mode` is
`"subagent"` or `"all"` and that are not `hidden`, returns early when nothing is
visible, and otherwise appends one component per build:

```python
event.components.append(InlinePromptComponent(
    stage="context_suffix",
    source="xbot.subagents",
    text="\n".join(lines),
))
```

`lines[0]` is `"Available subagents for the spawn_subagent tool:"`, followed by
`f"- {definition.name}: {definition.description}"` per visible definition.

## Job lifecycle

```
SubagentTools.spawn_subagent()
  → registry.create(
        spec=AgentJobSpec(agent=..., prompt=..., label=name or f"{agent}: {preview}"),
        owner="subagents",
        name=name,
    )
  → registry.start(job.id, SubagentRunner(session=launcher, agent=agent, prompt=prompt))
  → SubagentRunner.run() → SubagentLauncher.spawn_subagent() → ChildApplication
  → child.wait() → AgentJobResult(child=ChildApplicationResult)
  → stored as Succeeded.result
```

There is no `registry.create(kind=SUBAGENT)` call. The kind is a field of
`AgentJobSpec` (`kind="subagent"`), passed inside `spec=`, and the owner string is
`"subagents"`. `read_subagent` reads `job.result.child.final_response`; there is
no `output_store` attribute on the result and no `TextOutputStorePort` involved.

## Typical extension: register a subagent definition

Agent definitions belong to the independent catalog plugins, not to
`subagents`. Register through the agent catalog:

```python
from XBotv2.agents import AgentCatalogPort, AgentDefinition


class RegisterReviewAgent:
    def __init__(self, catalog: AgentCatalogPort) -> None:
        self._catalog = catalog

    def register(self) -> None:
        self._catalog.register(AgentDefinition(
            name="reviewer",
            description="Review a focused change for correctness.",
            mode="subagent",
            prompt="Inspect the requested scope and report concrete defects.",
        ), overlay=True)


class ReviewAgentPlugin:
    inject = ["agent_catalog"]

    def apply(self, ctx, config=None) -> None:
        RegisterReviewAgent(ctx.agent_catalog).register()
```

Once a definition is visible with `mode="subagent"` (or `"all"`) and
`hidden=False`, the catalog prompt announces it and `spawn_subagent` accepts its
name.

Do not invoke a subagent Tool through a private name-based dispatch shortcut;
`ToolsPort` exposes the standard registration/execution surface, not a second
name-based executor. The Agent requests the built-in subagent Tools by name.
Application code that owns child lifecycle uses `ChildApplicationsPort` directly.

## Cross-references

- Depends on: `context_builder`, `session`, `agent_catalog`,
  `child_applications`, `permissions`, `client_events`, `jobs`, `tools`,
  `thread_persistence`.
- Depended on by: the Agent (spawn/list/wait/read/cancel Tools) and the `agents`
  catalog for definition discovery.
- Pairs with: the agents catalog, `jobs` (`subagent`-kind job registry), and the
  application-owned child lifecycle.

## Common pitfalls

- **`thread_persistence` not available**: the runtime component stays `PENDING`
  and no Tools are registered. It does not probe `ctx.has()` or silently degrade.
- **Calling `wait_subagent` with no `subagent` jobs**: resolves to an empty list
  and returns the error code `subagent_not_found`.
- **Reading before waiting**: `job.result` may not yet be an `AgentJobResult`, so
  `read_subagent` returns `"No response captured yet"`.
- **Cancelling a terminal job**: `registry.cancel()` returns
  `CancelResult(cancelled=False)`; the child is not interrupted, and the Tool
  still reports `succeeded_text("Subagent <id> <status>")`.
- **Empty prompt**: both entry points validate `prompt.strip()`. The Tool returns
  `failed_text("invalid_prompt", ...)`; `SubagentLauncher` raises
  `SubagentAgentError`, whose `code` is `"agent_not_found"` (the class is shared
  by both the unknown-agent and empty-prompt failures — it is not an empty-prompt
  specific error).
- **The catalog is dynamic**: `CONTEXT_COMPONENTS_BUILT` re-reads the currently
  visible subagent definitions on every build; it is not a session-start
  snapshot.
- **Do not add a `prompts` dependency**: the catalog component is contributed per
  build because its content changes; a statically registered prompt fragment
  would go stale.
