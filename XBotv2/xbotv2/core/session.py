"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from xbotv2.api.hooks import HookStage
from xbotv2.api.paths import RuntimePaths
from xbotv2.core.mailbox import MailboxMessage, SessionMailbox

logger = logging.getLogger("xbotv2.session")


class SessionBusy(RuntimeError):
    """The live session cannot accept the requested concurrent operation."""


@dataclass
class SessionRuntime:
    """Engine, mailbox, interactions, and tasks owned by one live session."""

    session_id: str
    thread_id: str
    provider_name: str
    paths: RuntimePaths
    workspace_root: str
    no_plugins: bool
    engine: Any
    interactive: bool = True
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None
    mailbox: SessionMailbox = field(init=False)
    mailbox_responses: dict[
        str, asyncio.Queue[dict[str, Any] | None]
    ] = field(default_factory=dict)
    mailbox_worker: asyncio.Task | None = None
    mailbox_output: asyncio.Queue[dict[str, Any] | None] | None = None
    session_events: asyncio.Queue[dict[str, Any] | None] | None = None
    close_reason: str = "session_closed"

    def __post_init__(self) -> None:
        self.mailbox = SessionMailbox(
            self.paths.session(self.session_id).thread(self.thread_id).mailbox_log
        )
        self.engine.enqueue_mailbox = self.enqueue_general
        self.engine.runtime_event_sink = self._publish_runtime_event
        background_tasks = getattr(self.engine, "background_tasks", None)
        if background_tasks is not None:
            background_tasks.on_update = self._publish_task_update
            background_tasks.on_complete = self._enqueue_task_completion
        subagents = getattr(self.engine, "subagents", None)
        if subagents is not None:
            subagents.on_update = self._publish_task_update
            subagents.on_complete = self._enqueue_subagent_completion

    async def _publish_task_update(self, task: dict[str, Any]) -> None:
        if self.session_events is not None:
            await self.session_events.put({"type": "task_updated", "data": task})

    def _publish_runtime_event(self, event: dict[str, Any]) -> None:
        if self.session_events is not None:
            self.session_events.put_nowait(event)

    async def _enqueue_task_completion(self, task: dict[str, Any]) -> None:
        await self.enqueue_general({
            "source": "background_task",
            "event": "background_task_finished",
            "content": (
                f"Background task {task['task_id']} {task['status']}: "
                f"{task['command']}"
            ),
            "data": task,
        })

    async def _enqueue_subagent_completion(self, task: dict[str, Any]) -> None:
        from xbotv2.core.content_cache import externalize_content

        task = dict(task)
        task["output"] = externalize_content(
            str(task.get("output") or ""),
            self.engine.state_store,
            kind="subagent_output",
        )
        await self.enqueue_general({
            "source": "subagent",
            "event": "finished",
            "content": (
                f"Subagent task {task['task_id']} {task['status']}: "
                f"{task['agent']}"
            ),
            "data": task,
        })

    async def enqueue_user_message(
        self,
        content: str,
        request_id: str,
    ) -> tuple[MailboxMessage, asyncio.Queue[dict[str, Any] | None], bool, int]:
        queued = self.turn_lock.locked() or self.mailbox.size > 0
        position = self.mailbox.size + 1
        item = MailboxMessage.create(
            "user_message",
            content,
            request_id=request_id,
        )
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.mailbox_responses[item.id] = events
        await self.mailbox.put(item)
        self.ensure_mailbox_worker()
        return item, events, queued, position

    async def stream_message(
        self,
        content: str,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.turn_lock.locked() and self.mailbox.size == 0:
            try:
                async for event in run_turn_stream(
                    self,
                    content=content,
                    request_id=request_id,
                ):
                    yield event
                return
            except SessionBusy:
                # Another request acquired the turn between the idle check and
                # entering run_turn_stream; preserve ordering through mailbox.
                pass

        item, events, queued, position = await self.enqueue_user_message(
            content, request_id
        )
        completed = False
        try:
            if queued:
                yield {
                    "type": "message_queued",
                    "data": {"message_id": item.id, "position": position},
                }
            while True:
                event = await events.get()
                if event is None:
                    completed = True
                    return
                yield event
        finally:
            if not completed:
                if await self.mailbox.discard(item.id, "request_disconnected"):
                    self.mailbox_responses.pop(item.id, None)
                elif self.mailbox_output is events:
                    self.request_interrupt()

    async def enqueue_general(self, message: str | dict[str, Any]) -> MailboxMessage:
        item = MailboxMessage.create("general", message)
        await self.mailbox.put(item)
        self.ensure_mailbox_worker()
        return item

    def ensure_mailbox_worker(self) -> None:
        if self.mailbox.size == 0:
            return
        if (
            self.mailbox.next_kind == "general"
            and self.mailbox_output is None
            and self.session_events is None
        ):
            return
        if self.turn_lock.locked() and self.mailbox_worker is None:
            return
        if self.mailbox_worker is None or self.mailbox_worker.done():
            self.mailbox_worker = asyncio.create_task(
                _run_mailbox(self),
                name=f"xbotv2-mailbox-{self.session_id}",
            )
            self.mailbox_worker.add_done_callback(self._mailbox_worker_done)

    def _mailbox_worker_done(self, task: asyncio.Task) -> None:
        if self.mailbox_worker is task:
            self.mailbox_worker = None
        if self.mailbox.size:
            self.ensure_mailbox_worker()

    def attach_event_stream(self) -> asyncio.Queue[dict[str, Any] | None]:
        if self.session_events is not None:
            raise SessionBusy("session event stream is already connected")
        self.session_events = asyncio.Queue()
        self.ensure_mailbox_worker()
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

    async def close(self, reason: str = "session_closed") -> None:
        self.close_reason = reason
        background_tasks = getattr(self.engine, "background_tasks", None)
        if background_tasks is not None:
            await background_tasks.close()
        subagents = getattr(self.engine, "subagents", None)
        if subagents is not None:
            await subagents.close()
        await self.mailbox.close(reason)
        worker = self.mailbox_worker
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.mailbox_worker = None
        task = self.turn_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.turn_task = None
        for events in self.mailbox_responses.values():
            await events.put(None)
        self.mailbox_responses.clear()
        if self.mailbox_output is not None:
            await self.mailbox_output.put(None)
            self.mailbox_output = None
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
    mailbox_message: MailboxMessage | None = None,
) -> None:
    turn_stream = (
        runtime.engine.run_turn(
            content,
            request_id=request_id,
            mailbox_message=mailbox_message,
        )
        if mailbox_message is not None
        else runtime.engine.run_turn(content, request_id=request_id)
    )
    try:
        async for event in turn_stream:
            payload = _event_payload(event)
            if payload["type"] in {"turn_finished", "turn_cancelled"}:
                loader = getattr(runtime.engine, "plugin_loader", None)
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
    mailbox_message: MailboxMessage | None = None,
) -> AsyncIterator[dict[str, Any]]:
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)

    async with runtime.turn_lock:
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        disconnected = asyncio.Event()
        stream_completed = False
        pump_task = asyncio.create_task(
            _pump_turn(runtime, events, content, request_id, mailbox_message)
        )
        runtime.turn_task = pump_task
        try:
            interaction_sink = (
                _live_interaction_sink(runtime, events, disconnected)
                if getattr(runtime, "interactive", True)
                else nullcontext()
            )
            async with interaction_sink:
                while True:
                    event = await events.get()
                    if event is None:
                        stream_completed = True
                        break
                    yield event
        finally:
            disconnected.set()
            if not stream_completed and not pump_task.done():
                pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            runtime.turn_task = None
    runtime.ensure_mailbox_worker()


async def _run_mailbox(runtime: SessionRuntime) -> None:
    while True:
        item = await runtime.mailbox.get()
        if item is None:
            return
        target = (
            runtime.mailbox_responses.pop(item.id, None)
            if item.kind == "user_message"
            else runtime.mailbox_output or runtime.session_events
        )
        if item.kind == "user_message":
            runtime.mailbox_output = target
        error: Exception | None = None
        try:
            await runtime.engine.run_mailbox_hook(
                HookStage.BEFORE_MAILBOX_DELIVERY, item
            )
            content = (
                str(item.message)
                if item.kind == "user_message"
                else runtime.engine.mailbox_content(item)
            )
            async for event in run_turn_stream(
                runtime,
                content=content,
                request_id=item.request_id,
                mailbox_message=item,
            ):
                if target is not None:
                    await target.put(event)
            runtime.mailbox.delivered(item)
        except asyncio.CancelledError:
            runtime.mailbox.dropped(item, runtime.close_reason)
            raise
        except Exception as exc:  # noqa: BLE001
            error = exc
            runtime.mailbox.failed(item, exc)
            if target is not None:
                await target.put({
                    "type": "error",
                    "data": {
                        "code": "mailbox_delivery_failed",
                        "message": str(exc),
                        "details": {"exception_type": type(exc).__name__},
                    },
                })
        finally:
            await runtime.engine.run_mailbox_hook(
                HookStage.AFTER_MAILBOX_DELIVERY, item, error=error
            )

        if runtime.mailbox.next_kind != "general":
            if runtime.mailbox_output is not None:
                await runtime.mailbox_output.put(None)
            runtime.mailbox_output = None
        if runtime.mailbox.next_kind is None:
            return


__all__ = ["SessionBusy", "SessionRuntime", "run_turn_stream"]
