"""Model-facing subagent job tools.

Subagents run as SUBAGENT jobs in the shared JobRegistry. This plugin only
implements the adapter that requests a child session from the session service,
and the typed model-facing tools. It never owns lifecycle
state; waiting, cancellation, output storage, and listing live in the registry.
Agent definition registration belongs to the independent catalog plugins.
"""

from __future__ import annotations

from XBotv2.agents.contracts import AgentCatalogPort
from XBotv2.application import (
    ChildApplication,
    ChildApplicationRequest,
    ChildApplicationsPort,
    ClientEventsPort,
)
from XBotv2.core import (
    Tool,
    ToolResult,
)
from XBotv2.subagents.contracts import SubagentAgentError
from XBotv2.jobs import (
    Job,
    JobKind,
    JobNotFound,
    JobRegistryClosed,
    JobResult,
    JobRunnerContext,
    JobsPort,
    parse_job_status,
)
from XBotv2.persistence import ThreadLifecycleWriterPort
from XBotv2.context_builder import CONTEXT_COMPONENTS_BUILT, ContextComponentsBuilt
from XBotv2.context_builder.contracts import ContextComponent
from XBotv2.permissions import PermissionsPort
from XBotv2.session.contracts import SessionPort

_MAX_PROMPT_PREVIEW = 100
_MAX_SUMMARY = 256


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
    ) -> None:
        self._catalog = catalog
        self._session = session
        self._children = children
        self._lifecycle = lifecycle
        self._parent_permissions = parent_permissions
        self._client_events = client_events

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> ChildApplication:
        del parent_job_id
        definition = self._catalog.get(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        if not prompt.strip():
            raise SubagentAgentError("Subagent prompt cannot be empty")
        # The child application's lifecycle belongs to the executor
        # (SubagentRunner/children service); the launcher holds no extra
        # reference, so completed children are not pinned for the session.
        return await self._children.spawn(
            ChildApplicationRequest(
                definition=definition,
                thread_id=self._session.new_thread_id(definition.name),
                prompt=prompt,
                parent_permissions=self._parent_permissions,
                client_events=self._client_events,
            ),
            self._lifecycle,
        )


class SubagentRunner:
    """Runs one SUBAGENT job through a spawned child session."""

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

    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult:
        session = await self.session.spawn_subagent(
            self.agent,
            self.prompt,
            parent_job_id=job.parent_job_id,
        )
        self._child = session
        result = await session.wait()
        output = ctx.outputs.create_text(result.final_response)
        ctx.primary_output = output
        return JobResult(
            summary=_preview(
                f"Subagent {self.agent} completed", _MAX_SUMMARY
            ),
            output_store=output,
            data={
                "agent": self.agent,
                "usage": result.usage.model_dump(mode="json"),
            },
        )

    async def cancel(self, job: Job) -> None:
        del job
        if self._child is not None:
            await self._child.cancel()


class SubagentTools:
    """Named handlers for the subagent job tool surface."""

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
        self,
        agent: str,
        prompt: str,
        name: str | None = None,
    ) -> ToolResult:
        """Delegate a focused task to a registered subagent.

        Args:
            agent: Registered subagent name shown in the system instructions.
            prompt: Complete task, context, constraints, and expected output.
            name: Optional short label for listing.
        """
        if self._registry.closing:
            return ToolResult.failure("session_closing", "Session is closing")
        if agent not in {item.name for item in self._catalog.definitions()}:
            return ToolResult.failure("agent_not_found", f"Unknown subagent: {agent}")
        if not prompt.strip():
            return ToolResult.failure("invalid_prompt", "Subagent prompt cannot be empty")
        try:
            job = await self._registry.create(
                kind=JobKind.SUBAGENT,
                metadata={
                    "agent": agent,
                    "command": f"{agent}: {_preview(prompt, _MAX_PROMPT_PREVIEW)}",
                },
                name=name,
            )
        except JobRegistryClosed:
            return ToolResult.failure("session_closing", "Session is closing")
        self._registry.start(
            job.id,
            SubagentRunner(session=self._launcher, agent=agent, prompt=prompt),
        )
        return ToolResult.success(f"Started {job.id} (status: {job.status.value})")

    async def list_subagents(self, status: str | None = None) -> ToolResult:
        """List subagent jobs, optionally filtered by terminal status."""
        summaries = self._registry.list(
            kind=JobKind.SUBAGENT,
            status=parse_job_status(status),
        )
        return ToolResult.success(f"{len(summaries)} subagent job(s)")

    async def wait_subagent(
        self,
        ids: list[str] | None = None,
        mode: str = "all",
        timeout_ms: int | None = None,
    ) -> ToolResult:
        """Wait for subagent jobs; read_subagent returns their final text."""
        if mode not in {"all", "any"}:
            return ToolResult.failure("invalid_mode", "mode must be 'all' or 'any'")
        resolved = ids or [
            job.id for job in self._registry.all() if job.kind is JobKind.SUBAGENT
        ]
        if not resolved:
            return ToolResult.failure("subagent_not_found", "No subagent jobs to wait for")
        try:
            await self._registry.wait(
                resolved,
                mode=mode,
                timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
            )
        except JobNotFound:
            return ToolResult.failure("subagent_not_found", "Unknown subagent job id")
        return ToolResult.success("Wait complete")

    async def read_subagent(
        self,
        id: str,
        cursor: int | None = None,
        max_chars: int = 8000,
    ) -> ToolResult:
        """Read one completed subagent response from the given character offset."""
        job = self._registry.get_or_none(id)
        if job is None or job.kind is not JobKind.SUBAGENT:
            return ToolResult.failure("subagent_not_found", f"Unknown subagent job: {id}")
        store = job.result.output_store if job.result is not None else None
        if store is None:
            if job.error is not None:
                return ToolResult.failure(job.error.code, job.error.message)
            return ToolResult.success("No response captured yet")
        chunk = await store.read(cursor=cursor, max_bytes=max_chars)
        return ToolResult.success(chunk.data)

    async def cancel_subagent(self, id: str) -> ToolResult:
        """Cancel one subagent job idempotently."""
        job = self._registry.get_or_none(id)
        if job is None or job.kind is not JobKind.SUBAGENT:
            return ToolResult.failure("subagent_not_found", f"Unknown subagent job: {id}")
        result = await self._registry.cancel(id)
        return ToolResult.success(f"Subagent {id} {result.status}")


class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort) -> None:
        self._catalog = catalog

    def contribute(self, event: ContextComponentsBuilt) -> None:
        """Append the visible-subagent catalog to one build's components.

        Dynamic per-build content (the catalog and hidden flags can change),
        so it joins the component list at build time instead of registering
        a static, ownerless prompt fragment from a listener.
        """
        visible = [
            definition
            for definition in self._catalog.definitions()
            if definition.mode in {"subagent", "all"} and not definition.hidden
        ]
        if not visible:
            return
        lines = ["Available subagents for the spawn_subagent tool:"]
        lines.extend(
            f"- {definition.name}: {definition.description}"
            for definition in visible
        )
        event.components.append(ContextComponent(
            role="system",
            source="available_subagents",
            content="\n".join(lines),
            plugin_name="xbot.subagents",
            stage="context_suffix",
        ))


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[truncated]"
