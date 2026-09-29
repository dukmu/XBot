"""Subagent collaboration on session-owned child threads.

The session manager owns live child runtimes and their canonical inboxes. This
module adapts those capabilities to model-facing tools and one-shot jobs; it
does not keep another mailbox, transcript, or child lifecycle cache.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from XBotv2.agentloop import InboxItem, RuntimeInput
from XBotv2.agentloop.outputs import (
    AssistantCompleted,
    LoopError,
    LoopTurnEnded,
    TurnCancelled,
)
from XBotv2.agents import AgentDefinition
from XBotv2.agents.contracts import AgentCatalogPort
from XBotv2.context_builder import BuiltContext
from XBotv2.context_builder.contracts import InlinePromptComponent
from XBotv2.core import ToolOutcome, failed_text, succeeded_text
from XBotv2.core.domain import InboxTarget
from XBotv2.core.errors import OperationError
from XBotv2.core.parts import TextPart
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.jobs import (
    Job,
    JobNotFound,
    JobRegistryClosed,
    JobsPort,
    parse_job_status,
)
from XBotv2.session.contracts import (
    OpenThread,
    PendingInputUpdate,
    SessionPort,
    SessionNotFound,
    SessionsPort,
    ThreadNotActive,
    ThreadSummary,
)
from XBotv2.session.events import InputClaimedEvent
from XBotv2.subagents.contracts import (
    SubagentAgentError,
    SubagentExecutionError,
    SubagentResult,
)

_MAX_PROMPT_PREVIEW = 100


@dataclass(frozen=True, slots=True)
class AgentJobSpec:
    agent: str
    prompt: str
    label: str
    thread_id: str
    kind: str = "subagent"


class SubagentLauncher:
    """Resolve direct children and operate them through ``SessionsPort``."""

    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        session: SessionPort,
        sessions: SessionsPort,
        provider_name: str | None,
        no_plugins: bool,
    ) -> None:
        self._catalog = catalog
        self._session = session
        self._sessions = sessions
        self._provider_name = provider_name
        self._no_plugins = no_plugins
        self._locks: dict[str, asyncio.Lock] = {}
        self._owned_threads: set[str] = set()

    @property
    def parent_thread_id(self) -> str:
        return self._session.thread_id

    def allocate(self, agent: str) -> str:
        return self._session.new_thread_id(self._definition(agent).name)

    async def resolve(self, thread_id: str) -> ThreadSummary:
        for summary in await self._sessions.list_threads(self._session.session_id):
            if summary.thread_id != thread_id:
                continue
            if (
                summary.kind == "subagent"
                and summary.parent_thread_id == self._session.thread_id
            ):
                return summary
            break
        raise SubagentAgentError(f"Unknown child thread: {thread_id}")

    def lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    async def ensure_open(
        self,
        *,
        thread_id: str,
        agent: str,
        create: bool,
    ) -> None:
        definition = self._definition(agent)
        mode = "new"
        if not create:
            summary = await self.resolve(thread_id)
            if summary.status == "active":
                self._owned_threads.add(thread_id)
                return
            mode = "resume"
        await self._sessions.open_thread(OpenThread(
            session_id=self._session.session_id,
            thread_id=thread_id,
            parent_thread_id=self._session.thread_id,
            workspace_root=self._session.workspace_root,
            provider_name=self._provider_name,
            mode=mode,
            no_plugins=self._no_plugins,
            selected_agent=definition.name,
        ))
        self._owned_threads.add(thread_id)

    async def submit(self, thread_id: str, item: InboxItem, *, wake: bool) -> None:
        await self._sessions.submit_runtime_input(
            self._session.session_id,
            thread_id,
            item,
            wake=wake,
        )

    async def close_thread(self, thread_id: str) -> None:
        await self._sessions.close_thread(self._session.session_id, thread_id)
        self._owned_threads.discard(thread_id)

    async def subscribe(self, thread_id: str):
        return await self._sessions.stream_events(
            self._session.session_id,
            thread_id,
        )

    async def usage(self, thread_id: str):
        return (await self._sessions.thread_summary(
            self._session.session_id,
            thread_id,
        )).usage

    async def cancel_input(
        self,
        thread_id: str,
        input_id: str,
        *,
        claimed: bool,
    ) -> None:
        if not claimed:
            try:
                await self._sessions.update_pending_input(PendingInputUpdate(
                    session_id=self._session.session_id,
                    thread_id=thread_id,
                    message_id=input_id,
                    action="remove",
                ))
                return
            except OperationError as exc:
                # Removal and claim can cross. Once claimed, interruption is
                # the only operation that still owns the executing turn.
                if exc.code != "queue_item_not_found":
                    raise
        await self._sessions.interrupt(self._session.session_id, thread_id)

    async def close(self) -> None:
        errors: list[Exception] = []
        for thread_id in tuple(self._owned_threads):
            try:
                await self.close_thread(thread_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close child threads", errors)

    def _definition(self, agent: str) -> AgentDefinition:
        definition = self._catalog.get(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        return definition


class SubagentRunner:
    """Run and observe one turn on a stable child thread."""

    def __init__(
        self,
        *,
        launcher: SubagentLauncher,
        thread_id: str,
        agent: str,
        message: str,
    ) -> None:
        self._launcher = launcher
        self._thread_id = thread_id
        self._agent = agent
        self._message = message
        self._input_id = f"input-{uuid.uuid4().hex}"
        self._submitted = False
        self._claimed = False

    async def run(self, job: Job) -> SubagentResult:
        del job
        async with self._launcher.lock(self._thread_id):
            subscription = None
            try:
                await self._launcher.ensure_open(
                    thread_id=self._thread_id,
                    agent=self._agent,
                    create=False,
                )
                subscription = await self._launcher.subscribe(self._thread_id)
                self._submitted = True
                await self._launcher.submit(
                    self._thread_id,
                    _message_item(
                        input_id=self._input_id,
                        sender=self._launcher.parent_thread_id,
                        content=self._message,
                        event="task",
                        target=InboxTarget.NEXT_TURN,
                    ),
                    wake=True,
                )
                output = await self._wait(subscription)
                usage = await self._launcher.usage(self._thread_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = str(exc) or type(exc).__name__
                raise SubagentExecutionError(failure) from exc
            finally:
                if subscription is not None:
                    await subscription.aclose()
            return SubagentResult(
                thread_id=self._thread_id,
                final_response=output,
                usage=usage,
            )

    async def _wait(self, subscription) -> str:
        output = ""
        async for frame in subscription:
            event = frame.event
            if isinstance(event, InputClaimedEvent):
                if self._input_id in event.message_ids:
                    self._claimed = True
                continue
            if not self._claimed:
                continue
            if isinstance(event, AssistantCompleted):
                output = "".join(
                    part.text
                    for part in event.message.parts
                    if isinstance(part, TextPart)
                )
            elif isinstance(event, LoopError):
                raise SubagentExecutionError(
                    event.message or "Subagent turn failed"
                )
            elif isinstance(event, LoopTurnEnded):
                if isinstance(event.outcome, TurnCancelled):
                    raise SubagentExecutionError(event.outcome.reason)
                break
        if not output:
            raise SubagentExecutionError(
                "Subagent completed without an assistant response"
            )
        return output

    async def cancel(self, job: Job) -> None:
        del job
        if self._submitted:
            await self._launcher.cancel_input(
                self._thread_id,
                self._input_id,
                claimed=self._claimed,
            )


class SubagentTools:
    """Model-facing tools for stable child threads and one-shot jobs."""

    def __init__(
        self,
        *,
        registry: JobsPort,
        launcher: SubagentLauncher,
    ) -> None:
        self._registry = registry
        self._launcher = launcher

    async def spawn_subagent(
        self,
        agent: str,
        prompt: str,
        name: str | None = None,
    ) -> ToolOutcome:
        """Start a task on a new child thread.

        The result contains a stable thread id for messaging and a job id for
        waiting, reading the response, or cancelling this execution.
        """
        if self._registry.closing:
            return failed_text("session_closing", "Session is closing")
        if not prompt.strip():
            return failed_text("invalid_prompt", "Subagent prompt cannot be empty")
        try:
            thread_id = self._launcher.allocate(agent)
            job = await self._new_job(
                agent=agent,
                message=prompt,
                thread_id=thread_id,
                name=name,
                create=True,
            )
        except SubagentAgentError as exc:
            return failed_text("agent_not_found", str(exc))
        except JobRegistryClosed as exc:
            return failed_text("subagent_start_failed", str(exc))
        return succeeded_text(
            f"Started subagent thread {thread_id}; job {job.id} ({job.status})"
        )

    async def send_message(self, target: str, message: str) -> ToolOutcome:
        """Deliver a message to a direct child without starting a new turn.

        A running child receives it at a safe model boundary. An idle child
        retains it in its canonical inbox until a later follow-up task.
        """
        if not message.strip():
            return failed_text("invalid_message", "Message cannot be empty")
        if self._registry.closing:
            return failed_text("session_closing", "Session is closing")
        input_id = f"input-{uuid.uuid4().hex}"
        try:
            summary = await self._launcher.resolve(target)
            await self._launcher.ensure_open(
                thread_id=target,
                agent=summary.agent,
                create=False,
            )
            await self._launcher.submit(
                target,
                _message_item(
                    input_id=input_id,
                    sender=self._launcher.parent_thread_id,
                    content=message,
                    event="message",
                    target=InboxTarget.NEXT_STEP,
                ),
                wake=False,
            )
        except (
            OperationError,
            SessionNotFound,
            SubagentAgentError,
            ThreadNotActive,
        ) as exc:
            return failed_text("subagent_message_failed", str(exc))
        related = self._active_job(target)
        suffix = f"; active job {related.id}" if related is not None else ""
        return succeeded_text(
            f"Delivered input {input_id} to subagent thread {target}{suffix}"
        )

    async def followup_task(self, target: str, message: str) -> ToolOutcome:
        """Run a follow-up task on an existing direct child thread."""
        if not message.strip():
            return failed_text("invalid_message", "Message cannot be empty")
        if self._registry.closing:
            return failed_text("session_closing", "Session is closing")
        try:
            summary = await self._launcher.resolve(target)
            job = await self._new_job(
                agent=summary.agent,
                message=message,
                thread_id=target,
                name=None,
                create=False,
            )
        except SubagentAgentError as exc:
            return failed_text("subagent_not_found", str(exc))
        except JobRegistryClosed:
            return failed_text("session_closing", "Session is closing")
        return succeeded_text(
            f"Started follow-up on subagent thread {target}; job {job.id} ({job.status})"
        )

    async def list_subagents(self, status: str | None = None) -> ToolOutcome:
        """List subagent jobs, optionally filtered by terminal status."""
        summaries = self._registry.list(
            kind="subagent",
            status=parse_job_status(status),
        )
        if not summaries:
            return succeeded_text("No subagent jobs")
        lines = []
        for view in summaries:
            job = self._registry.get_or_none(view.id)
            thread_id = (
                job.spec.thread_id
                if job is not None and isinstance(job.spec, AgentJobSpec)
                else "unknown"
            )
            lines.append(
                f"{view.id}: {view.state} · thread {thread_id} · {view.label}"
            )
        return succeeded_text("\n".join(lines))

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
        if not isinstance(result, SubagentResult):
            if job.error is not None:
                return failed_text(job.error.code, job.error.message)
            return succeeded_text("No response captured yet")
        start = max(0, min(cursor or 0, len(result.final_response)))
        return succeeded_text(result.final_response[start : start + max_chars])

    async def cancel_subagent(self, id: str) -> ToolOutcome:
        """Cancel one execution; the child thread remains available."""
        job = self._registry.get_or_none(id)
        if job is None or job.kind != "subagent":
            return failed_text("subagent_not_found", f"Unknown subagent job: {id}")
        result = await self._registry.cancel(id)
        return succeeded_text(f"Subagent {id} {result.status}")

    async def _new_job(
        self,
        *,
        agent: str,
        message: str,
        thread_id: str,
        name: str | None,
        create: bool,
    ) -> Job:
        if create:
            await self._launcher.ensure_open(
                thread_id=thread_id,
                agent=agent,
                create=True,
            )
        try:
            job = await self._registry.create(
                spec=AgentJobSpec(
                    agent=agent,
                    prompt=message,
                    label=name or f"{agent}: {_preview(message, _MAX_PROMPT_PREVIEW)}",
                    thread_id=thread_id,
                ),
                owner="subagents",
                name=name,
            )
        except BaseException:
            if create:
                await self._launcher.close_thread(thread_id)
            raise
        self._registry.start(job.id, SubagentRunner(
            launcher=self._launcher,
            thread_id=thread_id,
            agent=agent,
            message=message,
        ))
        return job

    def _active_job(self, thread_id: str) -> Job | None:
        return next((
            job
            for job in reversed(self._registry.all())
            if (
                not job.terminal
                and isinstance(job.spec, AgentJobSpec)
                and job.spec.thread_id == thread_id
            )
        ), None)


class SubagentCatalogPrompt:
    def __init__(self, catalog: AgentCatalogPort) -> None:
        self._catalog = catalog

    def contribute(self, event: BuiltContext) -> None:
        visible = [
            definition
            for definition in self._catalog.definitions()
            if definition.mode in {"subagent", "all"} and not definition.hidden
        ]
        if not visible:
            return
        lines = [
            "Available subagents for spawn_subagent; use the returned thread id "
            "with send_message or followup_task:"
        ]
        lines.extend(
            f"- {definition.name}: {definition.description}"
            for definition in visible
        )
        event.components.append(InlinePromptComponent(
            stage="context_suffix",
            source="xbot.subagents",
            text="\n".join(lines),
        ))


def _message_item(
    *,
    input_id: str,
    sender: str,
    content: str,
    event: str,
    target: InboxTarget,
) -> InboxItem:
    guidance = (
        "Temporary input from your parent agent. Incorporate it into the "
        "current task; do not abandon that task unless explicitly directed."
        if event == "message"
        else "Task from your parent agent."
    )
    return InboxItem(
        id=input_id,
        target=target,
        input=RuntimeInput(
            source=f"subagent:{sender}",
            event=event,
            content=prompt_container(
                "subagent_message",
                [
                    prompt_element("guidance", guidance),
                    prompt_element("message", content),
                ],
                attributes={"sender": sender, "delivery": target.value},
            ),
        ),
    )


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[truncated]"
