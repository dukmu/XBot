"""Continuous child threads backed by the canonical session and AgentLoop."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from XBotv2.agentloop.api.contracts import AGENT_LOOP, InboxItem, RuntimeInput
from XBotv2.agentloop.api.outputs import ExecutionEnded
from XBotv2.agentloop.api.wait import wait_for_targets
from XBotv2.agents.api.contracts import AgentDefinition, AgentCatalogPort
from XBotv2.context_builder.api.contracts import BuiltContext, InlinePromptComponent
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.messages import AssistantMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.prompts import prompt_container, prompt_element
from XBotv2.core.api.tools import ToolOutcome, failed_text, succeeded_text
from XBotv2.session.api.contracts import (
    OpenThread, THREAD_EVENTS, THREAD_INFO, SessionNotFound, ThreadNotActive,
    SessionEventCursorExpired,
)
from XBotv2.core.api.errors import OperationError
from XBotv2.session.api.events import InputClaimedEvent
from XBotv2.subagents.api.contracts import SubagentAgentError


@dataclass(slots=True)
class _Turn:
    input_id: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    claimed: bool = False
    turn_id: str | None = None
    status: str = "accepted"
    error: str = ""


class SubagentCoordinator:
    """Session-scoped admission and current execution references."""
    def __init__(self, session_info, sessions):
        self._info = session_info
        self._sessions = sessions
        self._locks = WeakValueDictionary()
        self.turns: dict[str, _Turn] = {}
        self.observers: dict[str, asyncio.Task] = {}

    async def allocate(self, agent):
        return await self._sessions.new_thread_id(self._info.session_id, agent)

    def lock(self, thread_id):
        return self._locks.setdefault(thread_id, asyncio.Lock())


class SubagentLauncher:
    def __init__(self, *, catalog, session, sessions, coordinator, provider_name, no_plugins, archive, inbox):
        self._catalog = catalog
        self._session = session
        self._sessions = sessions
        self._coordinator = coordinator
        self._provider_name = provider_name
        self._no_plugins = no_plugins
        self._archive = archive
        self._inbox = inbox

    @property
    def parent_thread_id(self):
        return self._session.thread_id

    def _definition(self, agent):
        definition = self._catalog.get(agent)
        if definition is None or definition.mode == "primary":
            raise SubagentAgentError(f"Unknown subagent: {agent}")
        return definition

    async def resolve(self, thread_id):
        try:
            summary = await self._sessions.thread_summary(self._session.session_id, thread_id)
        except SessionNotFound as exc:
            raise SubagentAgentError(f"Unknown child thread: {thread_id}") from exc
        if summary.kind != "subagent" or summary.parent_thread_id != self.parent_thread_id:
            raise SubagentAgentError(f"Unknown child thread: {thread_id}")
        return summary

    async def ensure_open(self, thread_id, agent, *, create=False):
        definition = self._definition(agent)
        if not create:
            summary = await self.resolve(thread_id)
            if summary.status == "active":
                return
        await self._sessions.open_thread(OpenThread(
            session_id=self._session.session_id, thread_id=thread_id,
            parent_thread_id=self.parent_thread_id, workspace_root=self._session.workspace_root,
            provider_name=self._provider_name, mode="new" if create else "resume",
            no_plugins=self._no_plugins, selected_agent=definition.name,
        ))

    async def submit(self, thread_id, item, *, wake):
        scope = await self._sessions.locate(self._session.session_id, thread_id)
        await scope.require(AGENT_LOOP).submit_input(item, wake=wake)

    async def start(self, thread_id, agent, message, *, create=False):
        async with self._coordinator.lock(thread_id):
            await self.ensure_open(thread_id, agent, create=create)
            scope = await self._sessions.locate(self._session.session_id, thread_id)
            loop = scope.require(AGENT_LOOP)
            current = self._coordinator.turns.get(thread_id)
            if loop.busy or (current and any(item.id == current.input_id for item in loop.inbox.pending)):
                raise RuntimeError("Child already has an active task")
            if current and not current.done.is_set():
                await current.done.wait()
            subscription = scope.require(THREAD_EVENTS).subscribe()
            turn = _Turn(input_id=f"input-{uuid.uuid4().hex}")
            self._coordinator.turns[thread_id] = turn
            try:
                await self.submit(thread_id, _message_item(input_id=turn.input_id, sender=self.parent_thread_id, content=message, event="task", target=InboxTarget.NEXT_TURN), wake=True)
            except BaseException:
                self._coordinator.turns.pop(thread_id, None)
                await subscription.aclose()
                if create:
                    await self.close_thread(thread_id)
                raise
            self._track(thread_id, turn, subscription)
            await asyncio.sleep(0)
            return turn

    def _track(self, thread_id, turn, subscription):
        task = asyncio.create_task(self._observe(thread_id, turn, subscription))
        self._coordinator.observers[thread_id] = task
        def release(completed):
            if self._coordinator.observers.get(thread_id) is completed:
                self._coordinator.observers.pop(thread_id, None)
            if not completed.cancelled() and completed.exception() is not None:
                logging.getLogger(__name__).error("Child observer failed", exc_info=completed.exception())
        task.add_done_callback(release)

    async def current_turn(self, thread_id, summary):
        async with self._coordinator.lock(thread_id):
            turn = self._coordinator.turns.get(thread_id)
            if turn is not None:
                return turn
            subscription = None
            if summary.status == "active":
                scope = await self._sessions.locate(self._session.session_id, thread_id)
                subscription = scope.require(THREAD_EVENTS).subscribe()
            try:
                item, record = await self._last_task(thread_id)
                if item is None:
                    turn = _Turn(input_id="", status="idle")
                    turn.done.set()
                    return turn
                if item.status in {"removed", "discarded"}:
                    turn = _Turn(input_id=item.input_id, status="cancelled")
                    turn.done.set()
                    return turn
                loop = scope.require(AGENT_LOOP) if subscription is not None else None
                executing = bool(loop and record and loop.execution_id == record.turn_id)
                pending = bool(loop and any(entry.id == item.input_id for entry in loop.inbox.pending))
                if record is not None and record.status != "running" and not executing:
                    turn = _Turn(input_id=item.input_id, status=record.status)
                    turn.done.set()
                    return turn
                if not executing and not (record is None and item.status == "pending" and pending):
                    turn = _Turn(input_id=item.input_id, status="interrupted")
                    turn.done.set()
                    return turn
                turn = _Turn(input_id=item.input_id, claimed=record is not None,
                             turn_id=record.turn_id if record else None,
                             status="started" if record else "accepted")
                self._coordinator.turns[thread_id] = turn
                self._track(thread_id, turn, subscription)
                subscription = None
                return turn
            finally:
                if subscription is not None:
                    await subscription.aclose()

    async def _observe(self, thread_id, turn, subscription):
        try:
            while True:
                try:
                    async for frame in subscription:
                        event = frame.event
                        if isinstance(event, InputClaimedEvent) and turn.input_id in event.message_ids:
                            turn.claimed = True
                            turn.status = "started"
                            turn.turn_id = frame.scope.turn_id
                        elif isinstance(event, ExecutionEnded) and turn.input_id in event.execution.input_ids:
                            if turn.turn_id is not None and event.execution.turn_id != turn.turn_id:
                                continue
                            turn.status = event.execution.status
                            return
                    turn.status, turn.error = "interrupted", "Child event stream closed"
                    return
                except SessionEventCursorExpired:
                    await subscription.aclose()
                    try:
                        scope = await self._sessions.locate(self._session.session_id, thread_id)
                    except (SessionNotFound, ThreadNotActive):
                        turn.status = "interrupted"
                        return
                    subscription = scope.require(THREAD_EVENTS).subscribe()
                    loop = scope.require(AGENT_LOOP)
                    if turn.turn_id is None:
                        record = await self._archive.execution_for_input(
                            self._session.session_id, thread_id, turn.input_id
                        )
                        if record is not None:
                            turn.turn_id = record.turn_id
                    else:
                        record = await self._archive.execution_for_turn(
                            self._session.session_id, thread_id, turn.turn_id
                        )
                    if record is not None and record.status != "running" and loop.execution_id != record.turn_id:
                        turn.status = record.status
                        return
                    if turn.turn_id is not None and loop.execution_id == turn.turn_id:
                        continue
                    if record is None and any(item.id == turn.input_id for item in loop.inbox.pending):
                        continue
                    turn.status = "interrupted"
                    return
        except Exception as exc:
            turn.status, turn.error = "failed", str(exc)
            logging.getLogger(__name__).exception("Child observation failed")
        finally:
            await subscription.aclose()
            try:
                await self.submit(self.parent_thread_id, InboxItem(
                    target=InboxTarget.NEXT_STEP,
                    input=RuntimeInput(source=thread_id, event="subagent/completed", content=f"Subagent {thread_id} {turn.status}"),
                ), wake=False)
            except (SessionNotFound, ThreadNotActive):
                logging.getLogger(__name__).info("Parent closed before child completion notice", exc_info=True)
            except OperationError as exc:
                if exc.code != "session_closing":
                    raise
                logging.getLogger(__name__).info("Parent closing before child completion notice")
            finally:
                turn.done.set()
                if self._coordinator.turns.get(thread_id) is turn:
                    self._coordinator.turns.pop(thread_id, None)

    async def close_thread(self, thread_id):
        await self._sessions.close_thread(self._session.session_id, thread_id)
        observer = self._coordinator.observers.get(thread_id)
        if observer is not None:
            await observer
        self._coordinator.turns.pop(thread_id, None)

    async def close(self):
        errors = []
        scopes = await self._sessions.scopes(self._session.session_id)
        children = [scope.require(THREAD_INFO).thread_id for scope in scopes
                    if scope.require(THREAD_INFO).parent_thread_id == self.parent_thread_id]
        for thread_id in children:
            try:
                await self.close_thread(thread_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close child threads", errors)

    async def _last_task(self, thread_id):
        session_id = self._session.session_id
        item = await self._archive.latest_input(
            session_id, thread_id, source=f"subagent:{self.parent_thread_id}", event="task"
        )
        record = None if item is None else await self._archive.execution_for_input(
            session_id, thread_id, item.input_id
        )
        return item, record

    async def read(self, thread_id):
        item, record = await self._last_task(thread_id)
        if item is None:
            return None, "unavailable"
        if item.status in {"removed", "discarded"}:
            return None, "cancelled"
        if record is None or record.status == "running":
            try:
                scope = await self._sessions.locate(self._session.session_id, thread_id)
            except (SessionNotFound, ThreadNotActive):
                return None, "interrupted"
            loop = scope.require(AGENT_LOOP)
            if record is not None and loop.execution_id == record.turn_id:
                return None, "running"
            if record is None and item.status == "pending" and any(entry.id == item.input_id for entry in loop.inbox.pending):
                return None, "running"
            return None, "interrupted"
        if record.status != "completed" or record.final_message_id is None:
            return None, record.status
        message = await self._archive.message(self._session.session_id, thread_id, record.final_message_id)
        if not isinstance(message, AssistantMessage):
            raise RuntimeError("Archived final response is missing")
        return "".join(part.text for part in message.parts if isinstance(part, TextPart)), record.status


class SubagentTools:
    def __init__(self, *, launcher):
        self._launcher = launcher

    async def spawn_subagent(self, agent: str, prompt: str) -> ToolOutcome:
        """Start a child task and return its stable thread ID."""
        if not prompt.strip():
            return failed_text("invalid_prompt", "Subagent prompt cannot be empty")
        try:
            definition = self._launcher._definition(agent)
            thread_id = await self._launcher._coordinator.allocate(definition.name)
            turn = await self._launcher.start(thread_id, agent, prompt, create=True)
        except Exception as exc:
            return failed_text("subagent_start_failed", str(exc))
        return succeeded_text(f"Subagent thread {thread_id}: {turn.status}")

    async def send_message(self, target: str, message: str) -> ToolOutcome:
        """Send a message to a child without waking an idle thread."""
        if not message.strip():
            return failed_text("invalid_message", "Message cannot be empty")
        try:
            summary = await self._launcher.resolve(target)
            await self._launcher.ensure_open(target, summary.agent)
            await self._launcher.submit(target, _message_item(input_id=f"input-{uuid.uuid4().hex}", sender=self._launcher.parent_thread_id, content=message, event="message", target=InboxTarget.NEXT_STEP), wake=False)
        except Exception as exc:
            return failed_text("subagent_message_failed", str(exc))
        return succeeded_text(f"Message delivered to {target}")

    async def followup_task(self, target: str, message: str) -> ToolOutcome:
        """Start another task on the same child thread."""
        if not message.strip():
            return failed_text("invalid_message", "Message cannot be empty")
        try:
            summary = await self._launcher.resolve(target)
            turn = await self._launcher.start(target, summary.agent, message)
        except Exception as exc:
            return failed_text("subagent_start_failed", str(exc))
        return succeeded_text(f"Subagent thread {target}: {turn.status}")

    async def list_subagents(self) -> ToolOutcome:
        """List direct child threads and their current state."""
        threads = await self._launcher._sessions.list_threads(self._launcher._session.session_id)
        lines = []
        for thread in threads:
            if thread.kind == "subagent" and thread.parent_thread_id == self._launcher.parent_thread_id:
                turn = self._launcher._coordinator.turns.get(thread.thread_id)
                lines.append(f"{thread.thread_id}: {turn.status if turn else thread.turn_status if thread.status == 'active' else thread.status} ({thread.agent})")
        return succeeded_text("\n".join(lines) or "No child threads")

    async def wait_subagent(self, ids: list[str] | None = None, mode: str = "any", timeout_ms: int | None = None) -> ToolOutcome:
        """Wait for the current task of direct child threads."""
        if mode not in {"any", "all"} or (timeout_ms is not None and timeout_ms < 0):
            return failed_text("invalid_wait", "Invalid wait mode or timeout")
        try:
            if not ids:
                scopes = await self._launcher._sessions.scopes(self._launcher._session.session_id)
                ids = []
                for scope in scopes:
                    thread = scope.require(THREAD_INFO)
                    if thread.parent_thread_id != self._launcher.parent_thread_id:
                        continue
                    loop = scope.require(AGENT_LOOP)
                    turn = self._launcher._coordinator.turns.get(thread.thread_id)
                    if loop.busy or (turn is not None and not turn.done.is_set()) or any(
                        isinstance(item.input, RuntimeInput)
                        and item.input.source == f"subagent:{self._launcher.parent_thread_id}"
                        and item.input.event == "task"
                        for item in loop.inbox.pending
                    ):
                        ids.append(thread.thread_id)
            turns = {}
            for thread_id in ids:
                summary = await self._launcher.resolve(thread_id)
                turns[thread_id] = await self._launcher.current_turn(thread_id, summary)
            targets = {thread_id: (turn.done.is_set, turn.done) for thread_id, turn in turns.items()}
            reason = await wait_for_targets(targets, mode=mode, timeout=None if timeout_ms is None else timeout_ms / 1000, inbox=self._launcher._inbox, completion_event="subagent/completed")
        except Exception as exc:
            return failed_text("subagent_wait_failed", str(exc))
        states = ", ".join(f"{id}: {turn.status if turn else 'idle'}" for id, turn in turns.items())
        return succeeded_text(f"{reason}: {states}" if states else reason)

    async def read_subagent(self, id: str, cursor: int | None = None, max_chars: int = 8000) -> ToolOutcome:
        """Read the latest completed response from the child archive."""
        if (cursor is not None and cursor < 0) or max_chars < 1:
            return failed_text("invalid_range", "Invalid cursor or max_chars")
        try:
            await self._launcher.resolve(id)
            response, status = await self._launcher.read(id)
        except Exception as exc:
            return failed_text("subagent_read_failed", str(exc))
        if response is None:
            if status in {"failed", "cancelled"}:
                return failed_text("subagent_failed", status)
            return succeeded_text(f"No response available ({status})")
        start = cursor or 0
        end = min(start + max_chars, len(response))
        return succeeded_text(f"{response[start:end]}\n[next_cursor: {end if end < len(response) else 'end'}]")

    async def cancel_subagent(self, id: str) -> ToolOutcome:
        """Cancel the current child task, keeping the thread open."""
        try:
            await self._launcher.resolve(id)
            turn = self._launcher._coordinator.turns.get(id)
            if turn is None or turn.done.is_set():
                return succeeded_text("No active task")
            scope = await self._launcher._sessions.locate(self._launcher._session.session_id, id)
            loop = scope.require(AGENT_LOOP)
            if not turn.claimed:
                try:
                    await loop.update_pending_input(turn.input_id, "remove")
                    turn.status = "cancelled"
                    observer = self._launcher._coordinator.observers.get(id)
                    if observer:
                        observer.cancel()
                        await asyncio.gather(observer, return_exceptions=True)
                    return succeeded_text("cancelled")
                except Exception as exc:
                    if getattr(exc, "code", None) != "queue_item_not_found":
                        raise
            await loop.interrupt()
        except Exception as exc:
            return failed_text("subagent_cancel_failed", str(exc))
        return succeeded_text("Cancellation requested")

    async def close_subagent(self, id: str) -> ToolOutcome:
        """Close a child thread and release its live-thread slot."""
        try:
            await self._launcher.resolve(id)
            await self._launcher.close_thread(id)
        except Exception as exc:
            return failed_text("subagent_close_failed", str(exc))
        return succeeded_text(f"Closed {id}")


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
            f"- {definition.name}: {definition.description}" for definition in visible
        )
        event.components.append(
            InlinePromptComponent(
                stage="context_suffix",
                source="xbot.subagents",
                text="\n".join(lines),
            )
        )


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
