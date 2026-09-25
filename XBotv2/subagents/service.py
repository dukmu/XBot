"""Model-facing subagent job tools.

Subagents run as SUBAGENT jobs in the shared JobRegistry. This plugin only
implements the adapter that requests a child session from the session service,
and the typed model-facing tools. It never owns lifecycle
state; waiting, cancellation, output storage, and listing live in the registry.
Agent definition registration belongs to the independent catalog plugins.
"""

from __future__ import annotations

from XBotv2.agents.contracts import AgentCatalogPort
from dataclasses import dataclass
from XBotv2.application import (
    ChildApplication,
    ChildApplicationRequest,
    ChildApplicationResult,
    ChildApplicationsPort,
    ClientEventsPort,
)
from XBotv2.core import Tool, ToolOutcome, failed_text, succeeded_text
from XBotv2.subagents.contracts import SubagentAgentError
from XBotv2.jobs import (
    Job,
    JobNotFound,
    JobRegistryClosed,
    JobsPort,
    parse_job_status,
)
from XBotv2.persistence import ThreadLifecycleWriterPort
from XBotv2.context_builder import CONTEXT_COMPONENTS_BUILT, BuiltContext
from XBotv2.context_builder.contracts import InlinePromptComponent
from XBotv2.permissions import PermissionsPort
from XBotv2.session.contracts import SessionPort

_MAX_PROMPT_PREVIEW = 100


@dataclass(frozen=True, slots=True)
class AgentJobSpec:
    agent: str
    prompt: str
    label: str
    kind: str = "subagent"


@dataclass(frozen=True, slots=True)
class AgentJobResult:
    child: ChildApplicationResult


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

    async def run(self, job: Job) -> AgentJobResult:
        session = await self.session.spawn_subagent(
            self.agent,
            self.prompt,
            parent_job_id=job.parent_job_id,
        )
        self._child = session
        result = await session.wait()
        return AgentJobResult(child=result)

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
    ) -> ToolOutcome:
        """Delegate a focused task to a registered subagent.

        Args:
            agent: Registered subagent name shown in the system instructions.
            prompt: Complete task, context, constraints, and expected output.
            name: Optional short label for listing.
        """
        if self._registry.closing:
            return failed_text("session_closing", "Session is closing")
        if agent not in {item.name for item in self._catalog.definitions()}:
            return failed_text("agent_not_found", f"Unknown subagent: {agent}")
        if not prompt.strip():
            return failed_text("invalid_prompt", "Subagent prompt cannot be empty")
        try:
            job = await self._registry.create(
                spec=AgentJobSpec(
                    agent=agent,
                    prompt=prompt,
                    label=name or f"{agent}: {_preview(prompt, _MAX_PROMPT_PREVIEW)}",
                ),
                owner="subagents",
                name=name,
            )
        except JobRegistryClosed:
            return failed_text("session_closing", "Session is closing")
        self._registry.start(
            job.id,
            SubagentRunner(session=self._launcher, agent=agent, prompt=prompt),
        )
        return succeeded_text(f"Started {job.id} (status: {job.status})")

    async def list_subagents(self, status: str | None = None) -> ToolOutcome:
        """List subagent jobs, optionally filtered by terminal status."""
        summaries = self._registry.list(
            kind="subagent",
            status=parse_job_status(status),
        )
        return succeeded_text(f"{len(summaries)} subagent job(s)")

    async def wait_subagent(
        self,
        ids: list[str] | None = None,
        mode: str = "all",
        timeout_ms: int | None = None,
    ) -> ToolOutcome:
        """Wait for subagent jobs; read_subagent returns their final text."""
        if mode not in {"all", "any"}:
            return failed_text("invalid_mode", "mode must be 'all' or 'any'")
        resolved = ids or [
            job.id for job in self._registry.all() if job.kind == "subagent"
        ]
        if not resolved:
            return failed_text("subagent_not_found", "No subagent jobs to wait for")
        try:
            await self._registry.wait(
                resolved,
                mode=mode,
                timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
            )
        except JobNotFound:
            return failed_text("subagent_not_found", "Unknown subagent job id")
        return succeeded_text("Wait complete")

    async def read_subagent(
        self,
        id: str,
        cursor: int | None = None,
        max_chars: int = 8000,
    ) -> ToolOutcome:
        """Read one completed subagent response from the given character offset."""
        job = self._registry.get_or_none(id)
        if job is None or job.kind != "subagent":
            return failed_text("subagent_not_found", f"Unknown subagent job: {id}")
        result = job.result
        if not isinstance(result, AgentJobResult):
            if job.error is not None:
                return failed_text(job.error.code, job.error.message)
            return succeeded_text("No response captured yet")
        start = max(0, min(cursor or 0, len(result.child.final_response)))
        return succeeded_text(result.child.final_response[start : start + max_chars])

    async def cancel_subagent(self, id: str) -> ToolOutcome:
        """Cancel one subagent job idempotently."""
        job = self._registry.get_or_none(id)
        if job is None or job.kind != "subagent":
            return failed_text("subagent_not_found", f"Unknown subagent job: {id}")
        result = await self._registry.cancel(id)
        return succeeded_text(f"Subagent {id} {result.status}")


class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort) -> None:
        self._catalog = catalog

    def contribute(self, event: BuiltContext) -> None:
        """Append the visible-subagent catalog to one build's components.

        The visible catalog can change between turns, so it joins the current
        build rather than being retained as stale prompt state.
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
        event.components.append(InlinePromptComponent(
            stage="context_suffix",
            source="xbot.subagents",
            text="\n".join(lines),
        ))


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[truncated]"
