"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from functools import partial
from typing import Any, AsyncIterator

from XBotv2.agents import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.application import (
    RUNTIME_EVENT,
    AgentApplicationPort,
    ClientEventsPort,
    RuntimeEvent,
)
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import ImageContent
from XBotv2.core.errors import OperationError
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop import EventContext, Events
from XBotv2.session.history import display_history_page
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ClientEvent, JsonObject, json_object
from XBotv2.interactions import interaction_recorded_event
from XBotv2.session import HISTORY_CHANGED, HistoryChanged
from XBotv2.session.protocol import session_error_event, session_event
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
    runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG
    interactive: bool = True
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None
    wakeup_task: asyncio.Task | None = None
    # Protocol routing only. Input content lives exclusively in engine.inbox.
    pending_responses: dict[str, PendingResponse] = field(default_factory=dict)
    response_output: asyncio.Queue[dict[str, Any] | None] | None = None
    event_streams: set[asyncio.Queue[dict[str, Any] | None]] = field(
        default_factory=set
    )
    close_reason: str = "session_closed"
    last_activity: float = field(default_factory=time.monotonic)
    # ``message`` events published before the event stream attaches; flushed
    # on connect so early inputs are never lost.
    _pending_message_events: list[dict[str, Any]] = field(default_factory=list)
    _wakeup_requested: bool = False
    _log: RuntimeLog = field(init=False)

    def __post_init__(self) -> None:
        self._log = self.runtime_log.bind(
            "session",
            session_id=self.session_id,
            thread_id=self.thread_id,
        )
        self.engine.set_wake_driver(self._request_wakeup)
        self.touch()
        events = self.application.events
        events.on(Events.INBOX_SPLICE, self._on_inbox_splice)
        events.on(RUNTIME_EVENT, self._on_runtime_event)
        events.on(HISTORY_CHANGED, self._on_history_changed)
        events.on(AGENT_CONFIGURED, self._on_agent_configured)

    def touch(self) -> None:
        """Mark the runtime active; resets the idle-reaper deadline."""
        self.last_activity = time.monotonic()

    async def _on_history_changed(self, event: HistoryChanged) -> None:
        """Project history replacement (``/clear``, ``/undo``) as an event."""
        history, cursor = display_history_page(event.messages, 160)
        self._publish_runtime_event(session_event(
            "history_updated",
            {
                "history": history,
                "history_cursor": cursor,
                "operation": event.operation,
                "turns": event.turns,
            },
        ))

    async def _on_agent_configured(self, event: AgentConfigured) -> None:
        """Project provider/model selection changes for status displays."""
        self.provider_name = event.provider
        data = {
            "agent_name": event.agent_name,
            "provider": event.provider,
            "model": event.model,
            "model_mode": event.model_mode,
            "context_window": event.context_window,
        }
        if data:
            self._publish_runtime_event(session_event("agent_configured", data))

    def _publish_runtime_event(self, event: dict[str, Any]) -> None:
        for stream in tuple(self.event_streams):
            stream.put_nowait(event)

    def _on_inbox_splice(self, event: EventContext) -> None:
        self.touch()
        payload = event.client_event
        if payload is not None:
            self._publish_runtime_event(payload.to_dict())

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self.touch()
        self._publish_runtime_event(event.client_event.to_dict())

    def _message_event(
        self,
        message_id: str,
        content: str,
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> dict[str, Any]:
        return session_event(
            "message",
            {
                "id": message_id,
                "role": "user",
                "content": content,
                "images": [image.to_dict() for image in images or []],
                "artifacts": [artifact.to_dict() for artifact in artifacts or []],
            },
        )

    def _publish_message_event(
        self,
        message_id: str,
        content: str,
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> None:
        """Broadcast one accepted user message on the shared event stream.

        Input ordering is owned by the agent inbox. This event is only the
        protocol projection used by clients to render accepted input.
        """
        event = self._message_event(
            message_id, content, images, artifacts
        )
        if self.event_streams:
            self._publish_runtime_event(event)
        else:
            self._pending_message_events.append(event)

    async def stream_message(
        self,
        content: str,
        request_id: str,
        *,
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deliver one user input.

        Idle input enters ``next-turn``. Busy input enters ``next-step`` and is
        claimed by the same loop inbox between model/tool steps.
        """
        if not self.turn_lock.locked():
            self._publish_message_event(
                request_id, content, images, artifacts
            )
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
        self._publish_message_event(
            item.message_id, content, images, artifacts
        )
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
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for event in self._pending_message_events:
            events.put_nowait(event)
        self._pending_message_events.clear()
        self.event_streams.add(events)
        self._log.debug(
            "session.events.attached",
            streams=len(self.event_streams),
        )
        return events

    def detach_event_stream(
        self,
        events: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        self.event_streams.discard(events)
        self._log.debug(
            "session.events.detached",
            streams=len(self.event_streams),
        )

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
                self._publish_runtime_event(event)
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
        for stream in tuple(self.event_streams):
            stream.put_nowait(None)
        self.event_streams.clear()
        try:
            await self.engine.close_session()
        except Exception as exc:
            self._log.exception(
                "session.engine.close.failed",
                error_type=type(exc).__name__,
            )
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
    tool_call_id: str = "",
) -> JsonObject:
    del tool_call_id
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
    client_events = runtime.application.client_events
    sink = partial(
        _live_sink,
        client_events=client_events,
        events=events,
        disconnect_task=disconnect_task,
    )
    previous = client_events.set_sink(sink)
    try:
        yield
    finally:
        client_events.set_sink(previous)
        if not disconnect_task.done():
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)


def _event_payload(event: dict[str, Any]) -> JsonObject:
    """Validate a loop event before projecting it onto the session stream."""
    return ClientEvent.from_mapping(event).to_dict()


async def _pump_turn(
    runtime: SessionRuntime,
    events: asyncio.Queue[dict[str, Any] | None],
    content: str | None,
    request_id: str,
    images: list[ImageContent] | None = None,
    artifacts: list[ArtifactRef] | None = None,
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
        runtime._log.info("session.turn.cancelled", request_id=request_id)
        raise
    except Exception as exc:  # noqa: BLE001
        runtime._log.exception(
            "session.turn.failed",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
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
    artifacts: list[ArtifactRef] | None = None,
    interactive: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)

    async with runtime.turn_lock:
        async for event in _drive_turn_stream(
            runtime,
            content=content,
            request_id=request_id,
            images=images,
            artifacts=artifacts,
            interactive=interactive,
        ):
            yield event
    if runtime._wakeup_requested and runtime.wakeup_task is None:
        runtime._request_wakeup()


async def regenerate_turn_stream(
    runtime: SessionRuntime,
    *,
    request_id: str,
    interactive: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Atomically replace the latest human turn and run it again."""
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    async with runtime.turn_lock:
        message = await runtime.application.history.regenerate_history()
        artifacts = [
            value
            for value in message.artifact or []
            if isinstance(value, ArtifactRef)
        ]
        history, cursor = display_history_page(runtime.engine.messages, 160)
        yield session_event(
            "history_updated",
            {
                "history": history,
                "history_cursor": cursor,
                "operation": "regenerate",
                "turns": 1,
            },
        )
        accepted = runtime._message_event(
            request_id,
            message.content,
            list(message.images),
            artifacts,
        )
        runtime._publish_runtime_event(accepted)
        yield accepted
        async for event in _drive_turn_stream(
            runtime,
            content=message.content,
            request_id=request_id,
            images=list(message.images),
            artifacts=artifacts,
            interactive=interactive,
        ):
            yield event
    if runtime._wakeup_requested and runtime.wakeup_task is None:
        runtime._request_wakeup()


async def _drive_turn_stream(
    runtime: SessionRuntime,
    *,
    content: str | None,
    request_id: str,
    images: list[ImageContent] | None,
    artifacts: list[ArtifactRef] | None,
    interactive: bool | None,
) -> AsyncIterator[dict[str, Any]]:
    """Drive one stream while the caller owns ``runtime.turn_lock``."""
    events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    disconnected = asyncio.Event()
    stream_completed = False
    handed_off = False
    pump_task = asyncio.create_task(
        _pump_turn(runtime, events, content, request_id, images, artifacts)
    )
    runtime.turn_task = pump_task
    try:
        live_interactive = (
            runtime.interactive if interactive is None else interactive
        )
        interaction_sink = (
            _live_interaction_sink(runtime, events, disconnected)
            if live_interactive
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


__all__ = [
    "SessionBusy",
    "SessionRuntime",
    "regenerate_turn_stream",
    "run_turn_stream",
]
