"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.application import AgentApplicationPort, ClientEventsPort
from XBotv2.core.messages import ImageContent
from XBotv2.core.errors import OperationError
from XBotv2.agentloop import EventContext, Events
from XBotv2.core.history import display_history
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ClientEvent, JsonObject, json_object
from XBotv2.interactions import interaction_recorded_event
from XBotv2.session.protocol import session_error_event, session_event
logger = logging.getLogger("xbotv2.session")


class SessionBusy(RuntimeError):
    """The live session cannot accept the requested concurrent operation."""


def require_idle(ctx: Any, action: str) -> None:
    """Reject engine-mutating operations while a turn is active."""
    if ctx.turn_lock.locked():
        raise OperationError(
            "thread_busy",
            f"Cannot {action} while a turn is active.",
            retryable=True,
        )


@dataclass
class PendingResponse:
    """Transport-only reply waiter keyed by an agent-inbox message id."""

    message_id: str
    request_id: str
    events: asyncio.Queue[dict[str, Any] | None]


@dataclass
class SessionRuntime:
    """Protocol streams and one concrete agent-loop driver."""

    session_id: str
    thread_id: str
    provider_name: str
    paths: RuntimePaths
    workspace_root: str
    no_plugins: bool
    application: AgentApplicationPort
    engine: AgentLoopDriverPort
    interactive: bool = True
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None
    wakeup_task: asyncio.Task | None = None
    # Protocol routing only. Input content lives exclusively in engine.inbox.
    pending_responses: dict[str, PendingResponse] = field(default_factory=dict)
    response_output: asyncio.Queue[dict[str, Any] | None] | None = None
    session_events: asyncio.Queue[dict[str, Any] | None] | None = None
    close_reason: str = "session_closed"
    last_activity: float = field(default_factory=time.monotonic)
    # ``message`` events published before the event stream attaches; flushed
    # on connect so early inputs are never lost.
    _pending_message_events: list[dict[str, Any]] = field(default_factory=list)
    _wakeup_requested: bool = False

    def __post_init__(self) -> None:
        self.engine.set_wake_driver(self._request_wakeup)
        self.touch()
        events = self.application.events
        events.on(Events.INBOX_SPLICE, self._on_runtime_event)
        events.on(Events.RUNTIME_EVENT, self._on_runtime_event)
        events.on(Events.JOB_UPDATED, self._on_runtime_event)
        events.on(Events.JOB_COMPLETED, self._on_job_completed)
        events.on(Events.STATE_CHANGED, self._on_state_changed)
        events.on(Events.AGENT_CONFIGURED, self._on_agent_configured)

    def touch(self) -> None:
        """Mark the runtime active; resets the idle-reaper deadline."""
        self.last_activity = time.monotonic()

    async def send_input(self, content: str, **kwargs: Any) -> Any:
        """Queue a plugin-command follow-up through the Agent's sole inbox."""
        self.touch()
        return await self.engine.followup(content, **kwargs)

    async def _on_job_completed(self, event: EventContext) -> None:
        await self._enqueue_job_completion(dict(event.event or {}))

    async def _on_state_changed(self, event: EventContext) -> None:
        """Project history replacement (``/clear``, ``/undo``) as an event."""
        operation = (event.event or {}).get("history_operation")
        if not operation or self.session_events is None:
            return
        op, turns = operation
        await self.session_events.put(session_event(
            "history_updated",
            {
                "history": display_history(event.messages or []),
                "operation": op,
                "turns": turns,
            },
        ))

    async def _on_agent_configured(self, event: EventContext) -> None:
        """Project provider/model selection changes for status displays."""
        config = event.config
        provider = getattr(config, "provider", None)
        if provider:
            self.provider_name = str(provider)
        data = {
            key: value
            for key in (
                "agent_name",
                "provider",
                "model",
                "model_mode",
                "context_window",
            )
            if (value := getattr(config, key, None)) is not None
        }
        if data and self.session_events is not None:
            await self.session_events.put(session_event("agent_configured", data))

    def _publish_runtime_event(self, event: dict[str, Any]) -> None:
        if self.session_events is not None:
            self.session_events.put_nowait(event)

    def _on_runtime_event(self, event: EventContext) -> None:
        payload = event.client_event
        if payload is not None:
            self._publish_runtime_event(payload.to_dict())

    def _publish_message_event(self, message_id: str, content: str) -> None:
        """Broadcast one accepted user message on the shared event stream.

        Input ordering is owned by the agent inbox. This event is only the
        protocol projection used by clients to render accepted input.
        """
        event = session_event(
            "message",
            {"id": message_id, "role": "user", "content": content},
        )
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
        payload = {
                "kind": str(notice.get("kind") or ""),
                "status": str(notice.get("status") or ""),
                "task_id": str(notice.get("task_id") or ""),
                "command": str(notice.get("command") or ""),
                "agent": str(notice.get("agent") or ""),
        }
        fused = prompt_container(
            "runtime_event",
            [prompt_element(
                "payload",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                attributes={"encoding": "json"},
            )],
            attributes={"source": "tasks", "event": "completed"},
        )
        await self.engine.inject(
            fused,
            source=str(notice.get("task_id") or "tasks"),
            metadata={"kind": "notification", "payload": payload},
        )
        self.touch()
        if self.session_events is not None:
            await self.session_events.put(session_event(
                "completion_notice",
                {
                    key: notice[key]
                    for key in (
                        "type",
                        "kind",
                        "task_id",
                        "status",
                        "command",
                        "agent",
                    )
                    if key in notice
                },
            ))

    async def stream_message(
        self,
        content: str,
        request_id: str,
        *,
        images: list[ImageContent] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deliver one user input.

        Idle input enters ``next-turn``. Busy input enters ``next-step`` and is
        claimed by the same loop inbox between model/tool steps.
        """
        if not self.turn_lock.locked():
            self._publish_message_event(request_id, content)
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
                # Another request acquired the driver; route this input to
                # next-step through the same inbox.
                pass

        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        item = await self.engine.steer(
            content,
            source="user",
            message_id=request_id,
            images=images,
            artifacts=artifacts,
        )
        pending = PendingResponse(
            message_id=item.message_id,
            request_id=request_id,
            events=events,
        )
        self.pending_responses[item.message_id] = pending
        self._publish_message_event(item.message_id, content)
        completed = False
        try:
            while True:
                event = await events.get()
                if event is None:
                    completed = True
                    return
                yield event
        finally:
            if not completed:
                self.pending_responses.pop(item.message_id, None)
            self.touch()

    def claim_response_output(self, message_ids: list[str]) -> bool:
        """Hand the reply to the final claimed input without storing content."""
        claimed = [
            self.pending_responses.pop(message_id)
            for message_id in message_ids
            if message_id in self.pending_responses
        ]
        if not claimed:
            return False
        for pending in claimed[:-1]:
            pending.events.put_nowait(None)
        self.response_output = claimed[-1].events
        return True

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

    def _request_wakeup(self) -> None:
        """Wake the loop driver for followup/steer; inject never calls this."""
        self._wakeup_requested = True
        if self.turn_lock.locked() or self.wakeup_task is not None:
            return
        self.wakeup_task = asyncio.create_task(self._run_wakeup())

    async def _run_wakeup(self) -> None:
        try:
            if self.turn_lock.locked():
                return
            self._wakeup_requested = False
            async for event in run_turn_stream(
                self,
                content=None,
            ):
                if self.session_events is not None:
                    await self.session_events.put(event)
        except SessionBusy:
            pass
        finally:
            self.wakeup_task = None
            if self._wakeup_requested and not self.turn_lock.locked():
                self._request_wakeup()

    async def close(self, reason: str = "session_closed") -> None:
        self.close_reason = reason
        task = self.turn_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.turn_task = None
        continuation = self.wakeup_task
        if (
            continuation is not None
            and not continuation.done()
            and continuation is not asyncio.current_task()
        ):
            continuation.cancel()
            await asyncio.gather(continuation, return_exceptions=True)
        self.wakeup_task = None
        for item in self.pending_responses.values():
            item.events.put_nowait(None)
        self.pending_responses.clear()
        if self.response_output is not None:
            await self.response_output.put(None)
            self.response_output = None
        await self.engine.discard_inputs()
        if self.session_events is not None:
            await self.session_events.put(None)
            self.session_events = None
        try:
            await self.engine.close_session()
        except Exception:
            logger.exception("Engine close_session failed for %s", self.session_id)
        finally:
            # The session owns the XCore application lifetime. Engine only
            # closes its loop lifecycle; unloading plugin fibers belongs to
            # the surrounding application context.
            await self.application.close()


async def _live_sink(
    client_event: ClientEvent,
    *,
    client_events: ClientEventsPort,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnect_task: asyncio.Task[Any],
    timeout_seconds: float | None = None,
) -> JsonObject:
    event_type = client_event.type
    event_data = client_event.data
    request_id = str(event_data.get("request_id") or "")
    waiter = client_events.waiter(event_type)
    if waiter is None:
        raise RuntimeError(f"No waiter registered for client event {event_type!r}")
    pending = waiter.register(request_id)
    wait_task = asyncio.create_task(
        waiter.wait_registered(request_id, pending, timeout_seconds)
    )
    try:
        await events.put(client_event.to_dict())
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
    await events.put(interaction_recorded_event(
        (
            "permission_response_recorded"
            if event_type == "permission_request"
            else "user_input_recorded"
        ),
        {
            "request_id": request_id,
            "status": result.status,
            "decision": result.decision,
            "scope": result.scope,
            "answer": result.answer,
            "pending_interactions": [],
        },
    ))
    return json_object(result.__dict__)


@asynccontextmanager
async def _live_interaction_sink(
    runtime: SessionRuntime,
    events: asyncio.Queue[dict[str, Any] | None],
    disconnected: asyncio.Event,
) -> AsyncIterator[None]:
    disconnect_task = asyncio.create_task(disconnected.wait())

    async def sink(
        client_event: ClientEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> JsonObject:
        del tool_call_id
        return await _live_sink(
            client_event,
            client_events=runtime.application.client_events,
            events=events,
            disconnect_task=disconnect_task,
            timeout_seconds=timeout_seconds,
        )

    previous = install_client_event_sink(runtime.application.client_events, sink)
    try:
        yield
    finally:
        restore_client_event_sinks(runtime.application.client_events, previous)
        if not disconnect_task.done():
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)


def install_client_event_sink(
    client_events: ClientEventsPort,
    sink: Any | None,
) -> Any | None:
    """Install one live protocol sink on the application event router.

    Feature services publish through the shared router, so the transport does
    not discover or modify individual plugins. Returns the previous sink.
    """
    return client_events.set_sink(sink)


def restore_client_event_sinks(
    client_events: ClientEventsPort,
    previous: Any | None,
) -> None:
    client_events.set_sink(previous)


def _event_payload(event: dict[str, Any]) -> JsonObject:
    """Validate a loop event before projecting it onto the session stream."""
    return ClientEvent.from_mapping(event).to_dict()


async def _pump_turn(
    runtime: SessionRuntime,
    events: asyncio.Queue[dict[str, Any] | None],
    content: str | None,
    request_id: str,
    images: list[ImageContent] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    turn_stream = None
    try:
        turn_stream = (
            runtime.engine.run_turn(
                content,
                request_id=request_id,
                images=images,
                artifacts=artifacts,
            )
            if content is not None
            else runtime.engine.run_pending(request_id=request_id)
        )
        async for event in turn_stream:
            payload = _event_payload(event)
            if payload["type"] in {"turn_finished", "turn_cancelled"}:
                slots = await runtime.application.status_slots()
                if slots:
                    payload["data"]["status_slots"] = slots
            await events.put(payload)
    except asyncio.CancelledError:
        logger.info("Turn cancelled for session %s", runtime.session_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Engine run_turn failed")
        await events.put(session_error_event(
            "turn_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
        ))
    finally:
        close = getattr(turn_stream, "aclose", None)
        if close is not None:
            await close()
        await events.put(None)


async def run_turn_stream(
    runtime: SessionRuntime,
    *,
    content: str | None,
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
                    if runtime.response_output is not None and handed_off:
                        await runtime.response_output.put(event)
                        continue
                    if (
                        event.get("type") == "_inbox_claimed"
                        and runtime.claim_response_output(
                            list(event.get("data", {}).get("message_ids") or [])
                        )
                    ):
                        handed_off = True
                        continue
                    if event.get("type") == "_inbox_claimed":
                        continue
                    yield event
        finally:
            disconnected.set()
            if not stream_completed and not pump_task.done():
                pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            runtime.turn_task = None
            runtime.touch()
            if runtime.response_output is not None:
                await runtime.response_output.put(None)
                runtime.response_output = None
    if runtime._wakeup_requested and runtime.wakeup_task is None:
        runtime._request_wakeup()


__all__ = ["SessionBusy", "SessionRuntime", "run_turn_stream"]
