"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from xbotv2.api.hooks import HookStage
from xbotv2.api.messages import ImageContent
from xbotv2.api.paths import RuntimePaths
from xbotv2.core.engine import Engine
from xbotv2.core.inbox import AgentInbox, InboxMessage

logger = logging.getLogger("xbotv2.session")


class SessionBusy(RuntimeError):
    """The live session cannot accept the requested concurrent operation."""


@dataclass
class PendingFold:
    """One user input accepted during a tool window, awaiting fold delivery.

    The client (TUI) already holds the text locally; ``events`` carries the
    delivery signal and (for the final folded input) the merged reply.
    """

    item_id: str
    request_id: str
    content: str
    images: list[ImageContent]
    artifacts: list[dict[str, Any]]
    events: asyncio.Queue[dict[str, Any] | None]


@dataclass
class SessionRuntime:
    """Engine, fold buffer, interactions, and tasks owned by one live session."""

    session_id: str
    thread_id: str
    provider_name: str
    paths: RuntimePaths
    workspace_root: str
    no_plugins: bool
    engine: Engine
    interactive: bool = True
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None
    continuation_task: asyncio.Task | None = None
    # User inputs accepted during a tool-execution window, waiting to be
    # fused into the running turn at the next tool boundary.
    pending_fold: list[PendingFold] = field(default_factory=list)
    # The stream that owns the merged reply once a fold hands off.
    fold_output: asyncio.Queue[dict[str, Any] | None] | None = None
    session_events: asyncio.Queue[dict[str, Any] | None] | None = None
    close_reason: str = "session_closed"
    last_activity: float = field(default_factory=time.monotonic)
    # ``message`` events published before the event stream attaches; flushed
    # on connect so early inputs are never lost.
    _pending_message_events: list[dict[str, Any]] = field(default_factory=list)
    # Model-visible runtime notifications (job/subagent completions). These
    # are drained into the next turn's context, never used to start a turn.
    inbox: AgentInbox = field(default_factory=AgentInbox)

    def __post_init__(self) -> None:
        self.engine.take_pending_fold = self._take_pending_fold
        self.engine.runtime_event_sink = self._publish_runtime_event
        self.engine.drain_inbox = self.inbox.drain
        self.engine.request_continuation = self.request_continuation
        self.touch()
        job_registry = self.engine.job_registry
        if job_registry is not None:
            job_registry.on_update = self._publish_task_update
            job_registry.on_complete = self._enqueue_job_completion

    def touch(self) -> None:
        """Mark the runtime active; resets the idle-reaper deadline."""
        self.last_activity = time.monotonic()

    async def _publish_task_update(self, task: dict[str, Any]) -> None:
        if self.session_events is not None:
            await self.session_events.put({"type": "task_updated", "data": task})

    def _publish_runtime_event(self, event: dict[str, Any]) -> None:
        if self.session_events is not None:
            self.session_events.put_nowait(event)

    def _publish_message_event(self, message_id: str, content: str) -> None:
        """Broadcast one accepted user message on the shared event stream.

        Queued inputs are held FIFO in the pending fold and, when consumed all
        at once, are notified to the client one by one in the same order via
        this single ``message`` event stream. The client renders from it, so
        ordering is deterministic across per-message turn streams.
        """
        event = {
            "type": "message",
            "data": {"id": message_id, "role": "user", "content": content},
        }
        if self.session_events is not None:
            self.session_events.put_nowait(event)
        else:
            self._pending_message_events.append(event)

    async def _enqueue_job_completion(self, task: dict[str, Any]) -> None:
        if str(task.get("kind") or "") == "shell":
            await self._collect_completion({
                "type": "background_task",
                "kind": "background_task",
                "task_id": str(task.get("task_id") or ""),
                "status": str(task.get("status") or "finished"),
                "command": str(task.get("command") or ""),
                "data": task,
            })
        else:
            await self._collect_completion({
                "type": "subagent",
                "kind": "subagent",
                "task_id": str(task.get("task_id") or ""),
                "status": str(task.get("status") or "finished"),
                "agent": str(task.get("agent") or ""),
                "data": task,
            })

    async def _collect_completion(self, notice: dict[str, Any]) -> None:
        """Stage one completion into the agent inbox and broadcast a notice.

        The completion is model-visible via ``inbox`` (drained into the next
        turn's context) but never starts a turn by itself: the TUI task panel
        already tracks status through ``task_updated``.
        """
        self.inbox.enqueue(InboxMessage(
            type=str(notice.get("type") or "job_completed"),
            source=str(notice.get("task_id") or ""),
            payload={
                "kind": str(notice.get("kind") or ""),
                "status": str(notice.get("status") or ""),
                "task_id": str(notice.get("task_id") or ""),
                "command": str(notice.get("command") or ""),
                "agent": str(notice.get("agent") or ""),
            },
        ))
        self.touch()
        if self.session_events is not None:
            await self.session_events.put({
                "type": "completion_notice",
                "data": notice,
            })

    async def stream_message(
        self,
        content: str,
        request_id: str,
        *,
        images: list[ImageContent] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deliver one user input.

        While the agent is idle a fresh turn runs directly. While it is busy,
        the input is held in the pending fold and fused into the running turn
        at the next tool boundary, so it is injected mid-turn rather than
        waiting for the turn to end. A leftover that no boundary ever fuses
        is rejected with ``input_rejected`` at turn end and the client retries.
        """
        if not self.turn_lock.locked() and not self.pending_fold:
            self._publish_message_event(request_id or f"msg-{uuid.uuid4().hex}", content)
            try:
                async for event in run_turn_stream(
                    self,
                    content=content,
                    request_id=request_id,
                    images=images,
                    artifacts=artifacts,
                ):
                    yield event
                return
            except SessionBusy:
                # Another request acquired the turn between the idle check
                # and entering run_turn_stream; fall through to the fold.
                pass

        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        item = PendingFold(
            item_id=f"fold-{uuid.uuid4().hex}",
            request_id=request_id,
            content=content,
            images=list(images or []),
            artifacts=list(artifacts or []),
            events=events,
        )
        self.pending_fold.append(item)
        completed = False
        try:
            while True:
                event = await events.get()
                if event is None:
                    completed = True
                    return
                yield event
        finally:
            if not completed and item in self.pending_fold:
                self.pending_fold.remove(item)
            self.touch()

    def _take_pending_fold(self) -> list[PendingFold]:
        """Drain every accepted input for one fused mid-turn delivery.

        Each accepted stream gets a ``message`` event carrying the server-side
        id and content, so the client renders it from the event (not from a
        locally stored copy); every non-final stream is then terminated. The
        final stream owns the merged reply via ``fold_output``.
        """
        if self.fold_output is not None:
            # This turn already handed its stream over to a fused request.
            return []
        items, self.pending_fold = self.pending_fold, []
        if not items:
            return []
        for index, item in enumerate(items):
            self._publish_message_event(item.item_id, item.content)
            if index == len(items) - 1:
                self.fold_output = item.events
            else:
                item.events.put_nowait(None)
        return items

    def _reject_leftover_fold(self) -> None:
        """Reject accepted-but-unfolded inputs (a fold race straggler)."""
        items, self.pending_fold = self.pending_fold, []
        for item in items:
            item.events.put_nowait({
                "type": "input_rejected",
                "data": {"reason": "fold_missed", "request_id": item.request_id},
            })
            item.events.put_nowait(None)

    def attach_event_stream(self) -> asyncio.Queue[dict[str, Any] | None]:
        if self.session_events is not None:
            raise SessionBusy("session event stream is already connected")
        self.session_events = asyncio.Queue()
        for event in self._pending_message_events:
            self.session_events.put_nowait(event)
        self._pending_message_events.clear()
        return self.session_events

    def detach_event_stream(
        self,
        events: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        if self.session_events is events:
            self.session_events = None

    def request_interrupt(self) -> bool:
        task = self.turn_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def request_continuation(self) -> None:
        """Schedule an automatic continuation turn (used by the goal plugin).

        This is a deliberate wake: a new turn runs that lets the model keep
        working on an active goal. It is scheduled in the background so the
        caller (a slash command handler) returns immediately, and it does not
        go through the agent inbox, which by design never starts a turn.
        """
        if self.turn_lock.locked() or self.continuation_task is not None:
            return
        self.engine.continuation = True
        self.continuation_task = asyncio.create_task(self._run_continuation())

    async def _run_continuation(self) -> None:
        try:
            async for event in run_turn_stream(
                self,
                content="[goal continuation]",
                request_id="goal-continuation",
            ):
                if self.session_events is not None:
                    await self.session_events.put(event)
        except SessionBusy:
            pass
        finally:
            self.engine.continuation = False
            self.continuation_task = None

    async def close(self, reason: str = "session_closed") -> None:
        self.close_reason = reason
        task = self.turn_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.turn_task = None
        continuation = self.continuation_task
        if (
            continuation is not None
            and not continuation.done()
            and continuation is not asyncio.current_task()
        ):
            continuation.cancel()
            await asyncio.gather(continuation, return_exceptions=True)
        self.continuation_task = None
        for item in self.pending_fold:
            item.events.put_nowait(None)
        self.pending_fold.clear()
        if self.fold_output is not None:
            await self.fold_output.put(None)
            self.fold_output = None
        if self.session_events is not None:
            await self.session_events.put(None)
            self.session_events = None
        try:
            await self.engine.close_session()
        except Exception:
            logger.exception("Engine close_session failed for %s", self.session_id)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": event.get("type", ""), "data": event.get("data", {})}


async def _live_sink(
    client_event: dict[str, Any],
    *,
    engine: Any,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnect_task: asyncio.Task[Any],
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    event_type = str(client_event.get("type") or "")
    event_data = client_event.get("data") or {}
    request_id = str(event_data.get("request_id") or "")
    waiter = (
        engine.permission_waiter
        if event_type == "permission_request"
        else engine.user_input_waiter
    )
    pending = waiter.register(request_id)
    wait_task = asyncio.create_task(
        waiter.wait_registered(request_id, pending, timeout_seconds)
    )
    try:
        await events.put(_event_payload(client_event))
        done, _ = await asyncio.wait(
            {wait_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        raise
    if wait_task not in done:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        return {
            "request_id": request_id,
            "status": "disconnected",
            "reason": "client_disconnected",
        }
    try:
        result = wait_task.result()
    except Exception as exc:
        return {"request_id": request_id, "status": "error", "reason": str(exc)}
    await events.put({
        "type": (
            "permission_response_recorded"
            if event_type == "permission_request"
            else "user_input_recorded"
        ),
        "data": {
            "request_id": request_id,
            "status": result.status,
            "decision": result.decision,
            "scope": result.scope,
            "answer": result.answer,
            "pending_interactions": [],
        },
    })
    return result.__dict__


@asynccontextmanager
async def _live_interaction_sink(
    runtime: SessionRuntime,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
) -> AsyncIterator[None]:
    disconnect_task = asyncio.create_task(disconnected.wait())

    async def sink(client_event, *, timeout_seconds=None, tool_call_id=""):
        del tool_call_id
        return await _live_sink(
            client_event,
            engine=runtime.engine,
            events=events,
            disconnect_task=disconnect_task,
            timeout_seconds=timeout_seconds,
        )

    previous = runtime.engine.set_client_event_sink(sink)
    try:
        yield
    finally:
        runtime.engine.set_client_event_sink(previous)
        if not disconnect_task.done():
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)


async def _pump_turn(
    runtime: SessionRuntime,
    events: asyncio.Queue[dict[str, Any] | None],
    content: str,
    request_id: str,
    images: list[ImageContent] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    turn_stream = None
    try:
        turn_stream = runtime.engine.run_turn(
            content,
            request_id=request_id,
            images=images,
            artifacts=artifacts,
        )
        async for event in turn_stream:
            payload = _event_payload(event)
            if payload["type"] in {"turn_finished", "turn_cancelled"}:
                loader = runtime.engine.plugin_loader
                if loader is not None:
                    payload["data"]["status_slots"] = await loader.status_slots()
            await events.put(payload)
    except asyncio.CancelledError:
        logger.info("Turn cancelled for session %s", runtime.session_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Engine run_turn failed")
        await events.put({
            "type": "error",
            "data": {
                "code": "turn_failed",
                "message": str(exc),
                "details": {"exception_type": type(exc).__name__},
            },
        })
    finally:
        close = getattr(turn_stream, "aclose", None)
        if close is not None:
            await close()
        await events.put(None)


async def run_turn_stream(
    runtime: SessionRuntime,
    *,
    content: str,
    request_id: str = "",
    images: list[ImageContent] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    interactive: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)

    async with runtime.turn_lock:
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        disconnected = asyncio.Event()
        stream_completed = False
        handed_off = False
        pump_task = asyncio.create_task(
            _pump_turn(
                runtime,
                events,
                content,
                request_id,
                images,
                artifacts,
            )
        )
        runtime.turn_task = pump_task
        try:
            interaction_sink = (
                _live_interaction_sink(runtime, events, disconnected)
                if (
                    runtime.interactive
                    if interactive is None
                    else interactive
                )
                else nullcontext()
            )
            async with interaction_sink:
                while True:
                    event = await events.get()
                    if event is None:
                        stream_completed = True
                        break
                    if (
                        runtime.fold_output is not None
                        and handed_off
                    ):
                        # After the fold boundary the merged response belongs
                        # to the final folded request's owner.
                        await runtime.fold_output.put(event)
                        continue
                    if (
                        runtime.fold_output is not None
                        and event.get("type") == "_fold_handoff"
                    ):
                        # The active turn handed its stream over to the folded
                        # input. Consume the control boundary (never forwarded)
                        # and route everything after it — the merged reply — to
                        # the folded request; events queued before it stay on
                        # this (active) stream.
                        handed_off = True
                        continue
                    yield event
        finally:
            disconnected.set()
            if not stream_completed and not pump_task.done():
                pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            runtime.turn_task = None
            runtime.touch()
            if runtime.fold_output is not None:
                await runtime.fold_output.put(None)
                runtime.fold_output = None
    runtime._reject_leftover_fold()


__all__ = ["SessionBusy", "SessionRuntime", "run_turn_stream"]
