# `subagents`

Model-facing subagent job tools. Spawns child sessions via the
`ChildApplicationsPort`, registers a `SUBAGENT` job in the shared
`JobRegistry`, and provides tools for spawning, listing, waiting,
reading, and cancelling subagent jobs.

- **Import/profile:** `subagents`, Agent profile.
- **Source:** `XBotv2/agents/subagents.py`,
  `XBotv2/agents/subagent_tools/plugin.py`.
- **Injects/provides:** `session`, `agent_catalog`, `child_applications`,
  `permissions`, `client_events`, `jobs`, `tools`, `prompts`,
  `thread_persistence` → (none directly; registers Tools).
- **Subscribes to events:** `application/initialized` (publishes subagent
  catalog to prompts).
- **Config:** `timeout_seconds` (default 600s).

## Public data models

### `SubagentLauncher` (`XBotv2/agents/subagents.py:30-62`)

```python
class SubagentLauncher:
    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        session: SessionPort,
        children: ChildApplicationsPort,
        lifecycle: ThreadLifecycleWriterPort,
        parent_permissions: object,
        client_events: object | None,
    ) -> None:
        self._catalog = catalog
        self._session = session
        self._children = children
        self._lifecycle = lifecycle
        self._parent_permissions = parent_permissions
        self._client_events = client_events
        self._active: list[AgentSession] = []

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession: ...
```

`spawn_subagent()` validates the agent exists and is not `mode="primary"`,
then calls `self._children.spawn(ChildApplicationRequest(...))` and
appends the result to `self._active`.

### `SubagentRunner` (`XBotv2/agents/subagents.py:65-94`)

```python
class SubagentRunner:
    def __init__(
        self,
        *,
        session: Any,
        agent: str,
        prompt: str,
    ) -> None:
        self.session = session
        self.agent = agent
        self.prompt = prompt
        self._child: AgentSession | None = None

    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult: ...
    async def cancel(self, job: Job) -> None: ...
```

`run()` calls `session.spawn_subagent(agent, prompt, parent_job_id=job.id)`,
then `await session.wait()` and stores the final response in
`ctx.primary_output`. Returns `JobResult(summary="Subagent {agent} completed",
data={"agent": agent, "usage": ...})`.

### `SubagentTools` (`XBotv2/agents/subagents.py:97-190`)

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

### `SubagentCatalogPrompt` (`XBotv2/agents/subagents.py:193-210`)

```python
class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort, prompts: Any) -> None:
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

## `SubagentsPlugin` (`XBotv2/agents/subagent_tools/plugin.py`)

```python
class SubagentsPlugin:
    inject = {
        "required": [
            "session", "agent_catalog", "child_applications",
            "permissions", "client_events", "jobs", "tools", "prompts",
        ],
        "optional": ["thread_persistence"],
    }
    name = "agents.subagents"
    Config = S.object({"timeout_seconds": S.number().optional()})

    def apply(self, ctx, config=None) -> None:
        if not ctx.has("thread_persistence"):
            return
        timeout_seconds = float((config or {}).get("timeout_seconds", 600.0))
        catalog: AgentCatalogPort = ctx.agent_catalog
        prompts = ctx.prompts
        ctx.on(APPLICATION_INITIALIZED, SubagentCatalogPrompt(catalog, prompts).publish)
        handlers = SubagentTools(
            registry=ctx.jobs,
            catalog=catalog,
            launcher=SubagentLauncher(
                catalog=catalog,
                session=ctx.session,
                children=ctx.child_applications,
                lifecycle=ctx.thread_persistence.lifecycle,
                parent_permissions=ctx.permissions,
                client_events=ctx.client_events,
            ),
        )
        ctx.tools.register(
            Tool.from_function(handlers.spawn_subagent),
            timeout_seconds=timeout_seconds,
        )
        for handler in (
            handlers.list_subagents,
            handlers.wait_subagent,
            handlers.read_subagent,
            handlers.cancel_subagent,
        ):
            ctx.tools.register(Tool.from_function(handler))
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

## Typical extension: spawn and wait

```python
from XBotv2.core import Tool, ToolResult

class OrchestratorPlugin:
    inject = ["tools"]

    def apply(self, ctx, config):
        async def analyze(code: str) -> ToolResult:
            result = await ctx.tools.dispatch(
                "spawn_subagent", {"agent": "default", "prompt": code}
            )
            job_id = result.split("(")[1].split(")")[0]
            # wait for completion
            await ctx.tools.dispatch(
                "wait_subagent", {"ids": [job_id], "mode": "any"}
            )
            # read output
            resp = await ctx.tools.dispatch(
                "read_subagent", {"id": job_id}
            )
            return ToolResult.success(resp.content)
```

## Cross-references

- Depends on: `session`, `agent_catalog`, `child_applications`,
  `permissions`, `client_events`, `jobs`, `tools`, `prompts`,
  `thread_persistence`, `agentloop` (`APPLICATION_INITIALIZED`).
- Depended on by: the Agent (spawn/list/wait/read/cancel tools).
- Pairs with: `agent-catalog` (subagent definitions), `jobs`
  (SUBAGENT job registry), `agent-runtime` (agent selection).

## Common pitfalls

- **`thread_persistence` not available**: `apply()` returns early
  if `ctx.has("thread_persistence")` is False. No tools are
  registered in that case.
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
