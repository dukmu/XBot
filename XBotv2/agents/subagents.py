"""Model-facing subagent job tools.

Subagents run as SUBAGENT jobs in the shared JobRegistry. This plugin only
implements the adapter that requests a child session from the session service,
and the typed model-facing tools. It never owns lifecycle
state; waiting, cancellation, output storage, and listing live in the registry.
Agent definition registration belongs to the independent catalog plugins.
"""

from __future__ import annotations

from typing import Any

from XBotv2.agents.services import AgentCatalogPort
from XBotv2.application.services import (
    ChildApplicationRequest,
    ChildApplicationsPort,
)
from XBotv2.core import (
    Tool,
    ToolResult,
)
from XBotv2.agentloop import Events
from XBotv2.agents import AgentSession, SubagentAgentError
from XBotv2.jobs import (
    Job,
    JobKind,
    JobNotFound,
    JobRegistryClosed,
    JobResult,
    JobRunnerContext,
    JobsPort,
    JobStatus,
)
from XBotv2.session.services import SessionPort
from xcore import S

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
        parent_permissions: object,
        client_events: object | None,
    ) -> None:
        self._catalog = catalog
        self._session = session
        self._children = children
        self._parent_permissions = parent_permissions
        self._client_events = client_events
        self._active: list[AgentSession] = []

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        *,
        parent_job_id: str | None = None,
    ) -> AgentSession:
        del parent_job_id
        definition = self._catalog.get(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        if not prompt.strip():
            raise SubagentAgentError("Subagent prompt cannot be empty")
        child = await self._children.spawn(ChildApplicationRequest(
            definition=definition,
            thread_id=self._session.new_thread_id(definition.name),
            prompt=prompt,
            parent_permissions=self._parent_permissions,
            client_events=self._client_events,
        ))
        self._active.append(child)
        return child


class SubagentRunner:
    """Runs one SUBAGENT job through a spawned child session."""

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
                "usage": dict(result.usage),
            },
        )

    async def cancel(self, job: Job) -> None:
        del job
        if self._child is not None:
            await self._child.cancel()


class SubagentsPlugin:
    inject = [
        "session",
        "agent_catalog",
        "child_applications",
        "permissions",
        "client_events",
        "jobs",
        "tools",
        "prompts",
    ]
    """Register subagent job tools and their prompt catalog."""

    name = "agents.subagents"
    Config = S.object({
        "timeout_seconds": S.number().optional(),
    })

    def __init__(self) -> None:
        self._timeout_seconds = 600.0

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        self._timeout_seconds = float((config or {}).get("timeout_seconds", 600.0))
        ctx.on(Events.SESSION_INIT, self._on_session_init)
        if ctx.session is None or ctx.jobs is None:
            return

        launcher = SubagentLauncher(
            catalog=ctx.agent_catalog,
            session=ctx.session,
            children=ctx.child_applications,
            parent_permissions=ctx.permissions,
            client_events=ctx.client_events,
        )
        registry: JobsPort = ctx.jobs

        async def spawn_subagent(
            agent: str,
            prompt: str,
            name: str | None = None,
        ) -> ToolResult:
            """Delegate a focused task to a registered subagent.

            The child runs asynchronously in a separate thread under the current
            session and returns a job ID immediately. Use ``wait_subagent`` when
            later work depends on completion and ``read_subagent`` to read the
            final response. The full history remains in the child thread; only
            the final response and usage are captured here. The child may spawn
            further subagents itself.

            Args:
                agent: Registered subagent name shown in the system instructions.
                prompt: Complete task, relevant context, constraints, and expected output.
                name: Optional short label for listing.
            """
            if registry.closing:
                return ToolResult.failure(
                    "session_closing", "Session is closing"
                )
            definition = ctx.agent_catalog.definitions()
            known = {item.name for item in definition}
            if agent not in known:
                return ToolResult.failure(
                    "agent_not_found", f"Unknown subagent: {agent}"
                )
            if not prompt.strip():
                return ToolResult.failure(
                    "invalid_prompt", "Subagent prompt cannot be empty"
                )
            try:
                job = await registry.create(
                    kind=JobKind.SUBAGENT,
                    metadata={
                        "agent": agent,
                        "command": f"{agent}: {_preview(prompt, _MAX_PROMPT_PREVIEW)}",
                    },
                    name=name,
                )
            except JobRegistryClosed:
                return ToolResult.failure(
                    "session_closing", "Session is closing"
                )
            registry.start(
                job.id,
                SubagentRunner(session=launcher, agent=agent, prompt=prompt),
            )
            return ToolResult.success(f"Started {job.id} (status: {job.status.value})")

        async def list_subagents(status: str | None = None) -> ToolResult:
            """List subagent jobs with lightweight metadata only.

            Returns IDs, names, statuses, and elapsed time — never prompts or
            responses. Use ``read_subagent`` to read a completed response.

            Args:
                status: Optional filter: pending, running, completed, failed, cancelled.
            """
            status_filter = _parse_status(status)
            summaries = registry.list(
                kind=JobKind.SUBAGENT, status=status_filter
            )
            return ToolResult.success(f"{len(summaries)} subagent job(s)")

        async def wait_subagent(
            ids: list[str] | None = None,
            mode: str = "all",
            timeout_ms: int | None = None,
        ) -> ToolResult:
            """Wait for subagent jobs to reach a terminal state.

            Returns only IDs and statuses, never responses; use
            ``read_subagent`` for the final text.

            Args:
                ids: Subagent job IDs to wait for. Omit to wait for any subagent
                    owned by this session.
                mode: ``all`` waits for every listed job; ``any`` returns on the first.
                timeout_ms: Optional maximum wait time in milliseconds.
            """
            if mode not in {"all", "any"}:
                return ToolResult.failure(
                    "invalid_mode", "mode must be 'all' or 'any'"
                )
            resolved = ids or [
                job.id for job in registry.all() if job.kind is JobKind.SUBAGENT
            ]
            if not resolved:
                return ToolResult.failure(
                    "subagent_not_found", "No subagent jobs to wait for"
                )
            try:
                result = await registry.wait(
                    resolved,
                    mode=mode,
                    timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
                )
            except JobNotFound:
                return ToolResult.failure(
                    "subagent_not_found", "Unknown subagent job id"
                )
            return ToolResult.success("Wait complete")

        async def read_subagent(
            id: str,
            cursor: int | None = None,
            max_chars: int = 8000,
        ) -> ToolResult:
            """Read the final response from one subagent job.

            Continue reading by passing the returned ``next_cursor``. Reading
            never changes the job's status.

            Args:
                id: Subagent job ID returned by spawn_subagent.
                cursor: Character offset to start reading from.
                max_chars: Maximum characters to return (default 8000).
            """
            job = registry.get_or_none(id)
            if job is None or job.kind is not JobKind.SUBAGENT:
                return ToolResult.failure(
                    "subagent_not_found", f"Unknown subagent job: {id}"
                )
            store = job.result.output_store if job.result is not None else None
            if store is None:
                return ToolResult.success("No response captured yet")
            chunk = await store.read(cursor=cursor, max_bytes=max_chars)
            return ToolResult.success(chunk.data)

        async def cancel_subagent(id: str) -> ToolResult:
            """Cancel one subagent job (idempotent).

            Args:
                id: Subagent job ID returned by spawn_subagent.
            """
            job = registry.get_or_none(id)
            if job is None or job.kind is not JobKind.SUBAGENT:
                return ToolResult.failure(
                    "subagent_not_found", f"Unknown subagent job: {id}"
                )
            result = await registry.cancel(id)
            return ToolResult.success(f"Subagent {id} {result.status}")

        ctx.tools.register(
            Tool.from_function(spawn_subagent, name="spawn_subagent"),
            timeout_seconds=self._timeout_seconds,
        )
        for function in (
            list_subagents,
            wait_subagent,
            read_subagent,
            cancel_subagent,
        ):
            ctx.tools.register(
                Tool.from_function(function),
            )

    def _on_session_init(self, ctx: Any) -> None:
        """Publish the subagent catalog once all definitions are registered."""
        visible_subagents = [
            definition
            for definition in self.ctx.agent_catalog.definitions()
            if definition.mode in {"subagent", "all"} and not definition.hidden
        ]
        if visible_subagents:
            lines = ["Available subagents for the spawn_subagent tool:"]
            lines.extend(
                f"- {definition.name}: {definition.description}"
                for definition in visible_subagents
            )
            self.ctx.prompts.add(
                "context_suffix",
                "\n".join(lines),
                source="available_subagents",
            )


def _parse_status(value: str | None) -> JobStatus | None:
    if value is None:
        return None
    try:
        return JobStatus(value)
    except ValueError:
        return None


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[truncated]"


plugin = SubagentsPlugin()
