# `subagents`

Model-facing subagent job tools. Spawns child sessions via the
`ChildApplicationsPort`, registers a `SUBAGENT` job in the shared
`JobRegistry`, and provides tools for spawning, listing, waiting,
reading, and cancelling subagent jobs.

- **Import/profile:** `subagents`, Agent profile.
- **Source:** `XBotv2/subagents/plugin.py` (the only plugin export),
  `XBotv2/subagents/contracts.py`, and `XBotv2/subagents/service.py`.
- **Injects/provides:** `session`, `agent_catalog`, `child_applications`,
  `permissions`, `client_events`, `jobs`, `tools`, `prompts`,
  `thread_persistence` → (none directly; registers Tools).
- **Subscribes to events:** `application/initialized` (publishes subagent
  catalog to prompts).
- **Config:** `timeout_seconds` (default 600s).

## Public data models

### `SubagentLauncher` (`XBotv2/subagents/service.py`)

```python
class SubagentLauncher:
    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        session: SessionPort,
        children: ChildApplicationsPort,
        lifecycle: ThreadLifecycleWriterPort,
        parent_permissions: PermissionsPort,
        client_events: ClientEventsPort | None,
    ) -> None:
        self._catalog = catalog
        self._session = session
        self._children = children
        self._lifecycle = lifecycle
        self._parent_permissions = parent_permissions
        self._client_events = client_events
        self._active: list[ChildApplication] = []

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> ChildApplication: ...
```

`spawn_subagent()` validates the agent exists and is not `mode="primary"`,
then calls `self._children.spawn(ChildApplicationRequest(...))` and
appends the result to `self._active`.

### `SubagentRunner` (`XBotv2/subagents/service.py`)

```python
class SubagentRunner:
    def __init__(
        self,
        *,
        session: SessionPort,
        agent: str,
        prompt: str,
    ) -> None:
        self.session = session
        self.agent = agent
        self.prompt = prompt
        self._child: ChildApplication | None = None

    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult: ...
    async def cancel(self, job: Job) -> None: ...
```

`run()` calls `session.spawn_subagent(agent, prompt, parent_job_id=job.id)`,
then `await session.wait()` and stores the final response in
`ctx.primary_output`. Returns `JobResult(summary="Subagent {agent} completed",
data={"agent": agent, "usage": ...})`.

### `SubagentTools` (`XBotv2/subagents/service.py`)

```python
class SubagentTools:
    def __init__(
        self,
        *,
        registry: JobsPort,
        launcher: SubagentLauncher,
        catalog: AgentCatalogPort,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._catalog = catalog

    async def spawn_subagent(
        self, agent: str, prompt: str, name: str | None = None
    ) -> ToolResult: ...

    async def list_subagents(
        self, status: str | None = None
    ) -> ToolResult: ...

    async def wait_subagent(
        self, ids: list[str] | None = None,
        mode: str = "all", timeout_ms: int | None = None
    ) -> ToolResult: ...

    async def read_subagent(
        self, id: str, cursor: int | None = None, max_chars: int = 8000
    ) -> ToolResult: ...

    async def cancel_subagent(self, id: str) -> ToolResult: ...
```

### `SubagentCatalogPrompt` (`XBotv2/subagents/service.py`)

```python
class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort, prompts: PromptsPort) -> None:
        self._catalog = catalog
        self._prompts = prompts

    def publish(self, _event: ApplicationInitialized) -> None:
        visible = [
            d for d in self._catalog.definitions()
            if d.mode in {"subagent", "all"} and not d.hidden
        ]
        if not visible:
            return
        lines = ["Available subagents for the spawn_subagent tool:"]
        lines.extend(
            f"- {d.name}: {d.description}" for d in visible
        )
        self._prompts.add(
            "context_suffix", "\n".join(lines),
            source="available_subagents",
        )
```

## `SubagentsPlugin` (`XBotv2/subagents/plugin.py`)

```python
class SubagentsRuntimeComponent:
    inject = [
        "session", "agent_catalog", "child_applications", "permissions",
        "client_events", "jobs", "tools", "prompts", "thread_persistence",
    ]
    name = "xbot.subagents"

class SubagentsPlugin:
    name = "xbot.subagents"
    Config = S.object({"timeout_seconds": S.number().optional()})

    async def apply(self, ctx, config=None) -> None:
        await ctx.plugin(SubagentsRuntimeComponent(), config)
```

## Job lifecycle

```
spawn_subagent() → registry.create(kind=SUBAGENT) →
  registry.start(job.id, SubagentRunner(session, agent, prompt)) →
  SubagentRunner.run() → session.spawn_subagent() →
  child.wait() → ctx.primary_output.store(final_response) →
  JobResult(summary=..., data={agent, usage})
```

`wait_subagent()` blocks until all listed jobs reach terminal state.
`read_subagent()` reads from `job.result.output_store` (a
`TextOutputStorePort`).

## Typical extension: register a subagent definition

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

Do not call `spawn_subagent` through a private `ctx.tools.dispatch` shortcut;
`ToolsPort` deliberately exposes the standard registration/execution surface,
not a second name-based executor. The Agent requests the built-in subagent
Tools. Application code that owns child lifecycle uses
`ChildApplicationsPort` directly.

## Cross-references

- Depends on: `session`, `agent_catalog`, `child_applications`,
  `permissions`, `client_events`, `jobs`, `tools`, `prompts`,
  `thread_persistence`, and application `APPLICATION_INITIALIZED`.
- Depended on by: the Agent (spawn/list/wait/read/cancel tools).
- Pairs with: the agents catalog, `jobs` (SUBAGENT job registry), and the
  application-owned child lifecycle.

## Common pitfalls

- **`thread_persistence` not available**: the internal runtime component stays
  `PENDING`; no tools are registered. It does not probe `ctx.has()` or silently
  degrade.
- **Using `mode="all"` on `wait_subagent` with no SUBAGENT jobs**:
  resolves to an empty list → returns
  `"subagent_not_found"` error.
- **Reading `read_subagent` before `wait_subagent` completes**:
  `job.result` may be None → returns `"No response captured yet"`.
- **Cancelling a job that already reached terminal state**:
  `registry.cancel()` returns `CancelResult(cancelled=False)` —
  the subagent is not interrupted.
- **Subagent prompt must not be empty**: both `spawn_subagent` and
  `SubagentLauncher.spawn_subagent` validate `prompt.strip()` —
  an empty prompt raises `SubagentAgentError`.
- **`SubagentCatalogPrompt` only fires once**: `APPLICATION_INITIALIZED`
  fires once per session; the catalog is not refreshed mid-session.
