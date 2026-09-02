"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import time
from contextlib import aclosing, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from XBotv2.agents import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agentloop import AgentLoopDriverPort
from XBotv2.agentloop.inbox import InboxInput, InboxTarget
from XBotv2.application import (
    RUNTIME_EVENT,
    AgentApplicationPort,
    RuntimeEvent,
)
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import ImageContent
from XBotv2.core.errors import OperationError
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop import EventContext, Events
from XBotv2.session.history import conversation_replay
from XBotv2.core.timing import conversation_stats
from XBotv2.core.paths import RuntimePaths
from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent
from XBotv2.interactions import interaction_recorded_event
from XBotv2.session import HISTORY_CHANGED, HistoryChanged
from XBotv2.session.event_stream import (
    SessionEventStream,
    SessionEventSubscription,
)
from XBotv2.session.protocol import session_error_event, session_event
from XBotv2.session.types import PendingInputData


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


def _pending_input_snapshot(item: InboxInput) -> PendingInputData:
    return PendingInputData(
        message_id=item.message_id,
        content=item.content,
        target=item.target.value,
        source=item.source,
        image_count=len(item.images),
        artifact_count=len(item.artifacts),
    )


@dataclass
class TurnResponse:
    """A detachable compatibility view of the authoritative event stream."""

    message_id: str
    request_id: str
    events: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=512),
        init=False,
    )
    attached: bool = True

    def emit(self, event: dict[str, Any]) -> None:
        if not self.attached:
            return
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow()

    def finish(self) -> None:
        if not self.attached:
            return
        try:
            self.events.put_nowait(None)
        except asyncio.QueueFull:
            self._overflow()

    def detach(self) -> None:
        self.attached = False
        while not self.events.empty():
            self.events.get_nowait()

    def _overflow(self) -> None:
        self.detach()
        self.events.put_nowait(session_error_event(
            "response_stream_overflow",
            "The POST response consumer fell behind; resume from the "
            "Session event cursor.",
        ))
        self.events.put_nowait(None)


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
    pending_responses: dict[str, TurnResponse] = field(default_factory=dict)
    event_stream: SessionEventStream = field(default_factory=SessionEventStream)
    close_reason: str = "session_closed"
    last_activity: float = field(default_factory=time.monotonic)
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

    def resume_pending_inputs(self) -> bool:
        """Resume durable inbox work after the runtime is fully registered."""
        pending = self.engine.pending_input_count
        if not pending:
            return False
        self._log.info("session.inbox.resuming", pending_inputs=pending)
        self._request_wakeup()
        return True

    async def _on_history_changed(self, event: HistoryChanged) -> None:
        """Project history replacement (``/clear``, ``/undo``) as an event."""
        page = self.application.history_pages.page(limit=160)
        self._publish_runtime_event(session_event(
            "history_updated",
            {
                "history": conversation_replay(page.messages),
                "history_cursor": page.next_cursor,
                "operation": event.operation,
                "turns": event.turns,
                "session_stats": conversation_stats(event.messages),
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
        data = event.get("data")
        request_id = (
            str(data.get("request_id") or "")
            if isinstance(data, dict)
            else ""
        )
        self.event_stream.publish(event, request_id=request_id)

    def _on_inbox_splice(self, event: EventContext) -> None:
        self.touch()
        payload = event.client_event
        if payload is None:
            return
        raw = asdict(payload)
        # Preserve the canonical Agent event for protocol consumers while the
        # queue projection gives UI clients the current editable snapshot.
        self._publish_runtime_event(raw)
        data = raw.get("data")
        self._publish_runtime_event(session_event(
            "queue_updated",
            {
                "items": [
                    item.model_dump(mode="json") for item in self.pending_inputs()
                ]
            },
        ))
        if not isinstance(data, dict) or data.get("operation") != "claim":
            return
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or not metadata.get("defer_message_event"):
                continue
            self._publish_runtime_event(session_event(
                "message",
                {
                    "id": str(item.get("message_id") or ""),
                    "role": "user",
                    "content": str(item.get("content") or ""),
                    "images": item.get("images") if isinstance(item.get("images"), list) else [],
                    "artifacts": item.get("artifacts") if isinstance(item.get("artifacts"), list) else [],
                },
            ))

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self.touch()
        self._publish_runtime_event(asdict(event.client_event))

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
                "images": [image.model_dump(mode="json") for image in images or []],
                "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts or []],
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
        self.event_stream.publish(event, request_id=message_id)

    def pending_inputs(self) -> tuple[PendingInputData, ...]:
        return tuple(_pending_input_snapshot(item) for item in self.engine.pending_inputs)

    async def update_pending_input(
        self,
        message_id: str,
        action: str,
        content: str = "",
    ) -> tuple[PendingInputData, ...]:
        try:
            if action == "edit":
                await self.engine.edit_input(message_id, content)
            elif action == "remove":
                await self.engine.remove_input(message_id)
                response = self.pending_responses.pop(message_id, None)
                if response is not None:
                    response.finish()
            elif action == "steer":
                await self.engine.retarget_input(message_id, InboxTarget.NEXT_STEP)
                self._request_wakeup()
            else:
                raise ValueError(f"Unsupported pending input action: {action}")
        except KeyError as exc:
            raise OperationError(
                "queue_item_not_found",
                f"Pending input {message_id!r} is no longer available.",
            ) from exc
        return self.pending_inputs()

    async def stream_message(
        self,
        content: str,
        request_id: str,
        *,
        delivery: str = "steer",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deliver one user input.

        Idle input starts directly. Busy ``queue`` input enters ``next-turn``;
        busy ``steer`` input enters ``next-step``. Both remain owned by the
        same loop inbox until claimed at their respective boundary.
        """
        if delivery not in {"queue", "steer"}:
            raise ValueError(f"Unsupported input delivery mode: {delivery}")
        if not self.turn_lock.locked():
            try:
                async for event in run_turn_stream(
                    self,
                    content=content,
                    request_id=request_id,
                    images=images,
                    artifacts=artifacts,
                    accepted_event=self._message_event(
                        request_id,
                        content,
                        images,
                        artifacts,
                    ),
                ):
                    yield event
                return
            except SessionBusy:
                # Another request acquired the driver; route this input to
                # next-step through the same inbox.
                pass

        queued = delivery == "queue"
        enqueue = self.engine.followup if queued else self.engine.steer
        item = await enqueue(
            content,
            source="user",
            message_id=request_id,
            images=images,
            artifacts=artifacts,
            metadata={"defer_message_event": True} if queued else None,
        )
        pending = TurnResponse(
            message_id=item.message_id,
            request_id=request_id,
        )
        self.pending_responses[item.message_id] = pending
        if not queued:
            self._publish_message_event(
                item.message_id, content, images, artifacts
            )
        completed = False
        try:
            while True:
                event = await pending.events.get()
                if event is None:
                    completed = True
                    return
                yield event
        finally:
            if not completed:
                self.pending_responses.pop(item.message_id, None)
            self.touch()

    def claim_response(
        self,
        message_ids: list[str],
    ) -> TurnResponse | None:
        """Hand the reply to the final claimed input without storing content."""
        claimed = [
            self.pending_responses.pop(message_id)
            for message_id in message_ids
            if message_id in self.pending_responses
        ]
        if not claimed:
            return None
        for pending in claimed[:-1]:
            pending.finish()
        return claimed[-1]

    def attach_event_stream(
        self,
        after: int | None = None,
    ) -> SessionEventSubscription:
        cursor = 0 if after is None else after
        events = self.event_stream.subscribe(cursor)
        self._log.debug(
            "session.events.attached",
            streams=self.event_stream.subscriber_count,
            after=cursor,
        )
        return events

    def detach_event_stream(
        self,
        events: SessionEventSubscription,
    ) -> None:
        events.close()
        self._log.debug(
            "session.events.detached",
            streams=self.event_stream.subscriber_count,
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
            async for _event in run_turn_stream(
                self,
                content=None,
            ):
                pass
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
            item.finish()
        self.pending_responses.clear()
        await self.engine.discard_inputs()
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
            try:
                await self.application.close()
            finally:
                self.event_stream.close()


def _event_payload(event: dict[str, Any]) -> dict[str, JsonValue]:
    """Validate a loop event before projecting it onto the session stream."""
    return asdict(ClientEvent(**event))


class TurnEventRouter:
    """Publish a turn once, with detachable transport response views."""

    def __init__(self, runtime: SessionRuntime, response: TurnResponse) -> None:
        self._runtime = runtime
        self._response = response
        self._responses = [response]

    def emit(self, event: dict[str, Any]) -> None:
        self._runtime.event_stream.publish(
            event,
            request_id=self._response.request_id,
        )
        self._response.emit(event)

    def claim(self, message_ids: list[str]) -> None:
        response = self._runtime.claim_response(message_ids)
        if response is None:
            return
        self._response = response
        self._responses.append(response)

    def finish(self) -> None:
        for response in self._responses:
            response.finish()

    async def live_sink(
        self,
        client_event: ClientEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue]:
        del tool_call_id
        event_type = client_event.type
        request_id = str(client_event.data.get("request_id") or "")
        waiter = self._runtime.application.client_events.waiter(event_type)
        if waiter is None:
            raise RuntimeError(
                f"No waiter registered for client event {event_type!r}"
            )
        pending = waiter.register(request_id)
        self.emit(asdict(client_event))
        try:
            result = await waiter.wait_registered(
                request_id,
                pending,
                timeout_seconds,
            )
        except Exception as exc:
            return {
                "request_id": request_id,
                "status": "error",
                "reason": str(exc),
            }
        self.emit(interaction_recorded_event(
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
        return dict(result.__dict__)


@asynccontextmanager
async def _live_interaction_sink(
    runtime: SessionRuntime,
    router: TurnEventRouter,
) -> AsyncIterator[None]:
    client_events = runtime.application.client_events
    previous = client_events.set_sink(router.live_sink)
    try:
        yield
    finally:
        client_events.set_sink(previous)


async def _execute_turn(
    runtime: SessionRuntime,
    router: TurnEventRouter,
    *,
    content: str | None,
    request_id: str,
    images: list[ImageContent] | None,
    artifacts: list[ArtifactRef] | None,
    interactive: bool | None,
) -> None:
    """Run one turn independently of any transport response consumer."""
    try:
        live_interactive = (
            runtime.interactive if interactive is None else interactive
        )
        interaction_sink = (
            _live_interaction_sink(runtime, router)
            if live_interactive
            else nullcontext()
        )
        async with interaction_sink:
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
            async with aclosing(turn_stream):
                async for event in turn_stream:
                    payload = _event_payload(event)
                    if payload["type"] == "_inbox_claimed":
                        router.claim(list(
                            payload["data"].get("message_ids") or []
                        ))
                        continue
                    if payload["type"] in {"turn_finished", "turn_cancelled"}:
                        slots = await runtime.application.status_slots()
                        if slots:
                            payload["data"]["status_slots"] = slots
                        snapshot = await runtime.application.snapshot()
                        payload["data"]["session_stats"] = conversation_stats(
                            snapshot.messages
                        ).model_dump(mode="json")
                    router.emit(payload)
    except asyncio.CancelledError:
        runtime._log.info("session.turn.cancelled", request_id=request_id)
        raise
    except Exception as exc:  # noqa: BLE001
        runtime._log.exception(
            "session.turn.failed",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        router.emit(session_error_event(
            "turn_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
        ))
    finally:
        router.finish()
        runtime.turn_task = None
        runtime.turn_lock.release()
        runtime.touch()
        if runtime._wakeup_requested and runtime.wakeup_task is None:
            runtime._request_wakeup()


async def run_turn_stream(
    runtime: SessionRuntime,
    *,
    content: str | None,
    request_id: str = "",
    images: list[ImageContent] | None = None,
    artifacts: list[ArtifactRef] | None = None,
    interactive: bool | None = None,
    accepted_event: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    await runtime.turn_lock.acquire()
    try:
        response = TurnResponse(request_id, request_id)
        router = TurnEventRouter(runtime, response)
        if accepted_event is not None:
            runtime.event_stream.publish(accepted_event, request_id=request_id)
        task = asyncio.create_task(_execute_turn(
            runtime,
            router,
            content=content,
            request_id=request_id,
            images=images,
            artifacts=artifacts,
            interactive=interactive,
        ))
    except BaseException:
        runtime.turn_lock.release()
        raise
    runtime.turn_task = task
    try:
        while True:
            event = await response.events.get()
            if event is None:
                return
            yield event
    finally:
        response.detach()


async def regenerate_turn_stream(
    runtime: SessionRuntime,
    *,
    request_id: str,
    interactive: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Atomically replace the latest human turn and run it again."""
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    await runtime.turn_lock.acquire()
    try:
        message = await runtime.application.history.regenerate_history()
        artifacts = [
            value
            for value in message.artifact or []
            if isinstance(value, ArtifactRef)
        ]
        page = runtime.application.history_pages.page(limit=160)
        snapshot = await runtime.application.snapshot()
        response = TurnResponse(request_id, request_id)
        router = TurnEventRouter(runtime, response)
        router.emit(session_event(
            "history_updated",
            {
                "history": conversation_replay(page.messages),
                "history_cursor": page.next_cursor,
                "operation": "regenerate",
                "turns": 1,
                "session_stats": conversation_stats(snapshot.messages),
            },
        ))
        accepted = runtime._message_event(
            request_id,
            message.content,
            list(message.images),
            artifacts,
        )
        router.emit(accepted)
    except BaseException:
        runtime.turn_lock.release()
        raise
    task = asyncio.create_task(_execute_turn(
        runtime,
        router,
        content=message.content,
        request_id=request_id,
        images=list(message.images),
        artifacts=artifacts,
        interactive=interactive,
    ))
    runtime.turn_task = task
    try:
        while True:
            event = await response.events.get()
            if event is None:
                return
            yield event
    finally:
        response.detach()


__all__ = [
    "SessionBusy",
    "SessionRuntime",
    "regenerate_turn_stream",
    "run_turn_stream",
]
