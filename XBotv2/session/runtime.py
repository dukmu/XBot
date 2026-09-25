"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import aclosing, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import AsyncIterator

from XBotv2.agents import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agentloop import AgentLoopDriverPort, Claimed, Consumed, Inserted
from XBotv2.agentloop.protocol import (
    LoopError,
    LoopTurnStarted,
    LoopTurnEnded,
    TurnFinished,
)
from XBotv2.agentloop.contracts import HumanInput, InboxItem, InboxTarget, RuntimeInput
from XBotv2.application import (
    RUNTIME_EVENT,
    AgentApplicationPort,
    RuntimeEvent,
)
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.artifacts import ImageRef
from XBotv2.core.errors import OperationError
from XBotv2.core.domain import (
    EventScope,
    SessionScope,
    TurnId,
    TurnScope,
)
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop import Events
from XBotv2.agentloop.events import ObserveInbox
from XBotv2.core.timing import conversation_stats
from XBotv2.core.metadata import THREAD_METADATA_CHANGED, ThreadMetadataChanged
from XBotv2.core.history import HistoryPage
from XBotv2.core.paths import RuntimePaths
from XBotv2.interactions import (
    InteractionRequest,
    InteractionResolution,
)
from XBotv2.session.contracts import (
    HISTORY_CHANGED,
    HistoryChanged,
    HistoryMutation,
    SessionPort,
    SessionKey,
    conversation_replay,
)
from XBotv2.session.event_stream import SessionEventStream
from XBotv2.session.protocol import (
    AgentConfiguredEvent,
    HistoryUpdatedEvent,
    InputAcceptedEvent,
    InputClaimedEvent,
    InputConsumedEvent,
    MessagePublishedEvent,
    QueueReplacedEvent,
    session_error_event,
)
from XBotv2.session.contracts import PendingInputData
from XBotv2.session.records import (
    HumanInputRecord,
    InputRecordPayload,
    RuntimeNoticeRecord,
    project_human_input,
)


class SessionBusy(RuntimeError):
    """The live session cannot accept the requested concurrent operation."""


def require_idle(ctx: "SessionRuntime", action: str) -> None:
    """Reject engine-mutating operations while a turn is active."""
    if ctx.turn_lock.locked():
        raise OperationError(
            "thread_busy",
            f"Cannot {action} while a turn is active.",
            retryable=True,
        )


def _pending_input_snapshot(item: InboxItem) -> PendingInputData:
    return PendingInputData(
        message_id=item.id,
        content=item.input.content,
        target=item.target.value,
        image_count=len(item.input.images),
        artifact_count=len(item.input.artifacts),
    )


@dataclass
class SessionRuntime(SessionPort):
    """Protocol streams and one concrete agent-loop driver."""

    paths: RuntimePaths
    no_plugins: bool
    application: AgentApplicationPort
    engine: AgentLoopDriverPort
    runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG
    interactive: bool = True
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submission_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    turn_task: asyncio.Task | None = None
    wakeup_task: asyncio.Task | None = None
    event_stream: SessionEventStream = field(init=False)
    close_reason: str = "session_closed"
    last_activity: float = field(default_factory=time.monotonic)
    _wakeup_requested: bool = False
    _active_router: "TurnEventRouter | None" = field(default=None, init=False)
    _log: RuntimeLog = field(init=False)

    @property
    def key(self) -> SessionKey:
        return self.application.loop_state.session.key

    @property
    def session_id(self) -> str:
        return self.key.session_id

    @property
    def thread_id(self) -> str:
        return self.key.thread_id

    def __post_init__(self) -> None:
        self._log = self.runtime_log.bind(
            "session",
            session_id=self.session_id,
            thread_id=self.thread_id,
        )
        self.event_stream = SessionEventStream(self.application.loop_state.session)
        self.touch()
        events = self.application.events
        events.on(Events.INBOX_CHANGED, self._on_inbox_changed)
        events.on(RUNTIME_EVENT, self._on_runtime_event)
        events.on(HISTORY_CHANGED, self._on_history_changed)
        events.on(AGENT_CONFIGURED, self._on_agent_configured)

    @property
    def provider_name(self) -> str:
        return (
            self.application.loop_state.metadata.value
            .runtime_selection.model.route.provider
        )

    @property
    def workspace_root(self) -> str:
        return self.application.loop_state.metadata.value.workspace_root

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
        self._publish_runtime_event(HistoryUpdatedEvent(
            operation=event.operation,
            mutation=HistoryMutation(
                removed_turns=event.turns,
                history=HistoryPage(
                    items=conversation_replay(page.items),
                    older_cursor=page.older_cursor,
                ),
                stats=conversation_stats(event.messages),
            ),
        ))

    async def _on_agent_configured(self, event: AgentConfigured) -> None:
        """Publish the effective selection without flattening its fields."""
        self._publish_runtime_event(AgentConfiguredEvent(
            runtime_selection=event.runtime_selection,
        ))

    def _publish_runtime_event(self, event) -> None:
        self.publish_event(event)

    def publish_event(
        self,
        event,
        *,
        scope: EventScope = SessionScope(),
    ) -> None:
        self.event_stream.publish(event, scope=scope)

    def _on_inbox_changed(self, event: ObserveInbox) -> None:
        self.touch()
        change = event.change
        if isinstance(change, Inserted) and change.wake:
            # The producer declares the wake intent; the runtime owns whether
            # and when the loop actually runs.
            self._request_wakeup()
        splice_event = QueueReplacedEvent(items=self.pending_inputs())
        # Preserve the canonical Agent event for protocol consumers while the
        # queue projection gives UI clients the current editable snapshot.
        self._publish_runtime_event(splice_event)
        if isinstance(change, Inserted):
            self._publish_runtime_event(InputAcceptedEvent(
                message_ids=[change.item.id],
                target=change.item.target.value,
            ))
        if isinstance(change, Claimed):
            claimed = InputClaimedEvent(
                message_ids=[item.id for item in change.items]
            )
            if self._active_router is not None:
                self._active_router.emit(claimed)
            else:
                self._publish_runtime_event(claimed)
        if isinstance(change, Consumed):
            consumed = InputConsumedEvent(
                message_ids=[item.id for item in change.items]
            )
            if self._active_router is not None:
                self._active_router.emit(consumed)
            else:
                self._publish_runtime_event(consumed)
        if not isinstance(change, Claimed):
            return
        for item in change.items:
            if isinstance(item.input, HumanInput):
                record = HumanInputRecord(
                    id=item.id,
                    content=item.input.content,
                    images=item.input.images,
                    artifacts=item.input.artifacts,
                )
            elif isinstance(item.input, RuntimeInput):
                record = RuntimeNoticeRecord(
                    id=item.id,
                    source=item.input.source,
                    event=item.input.event,
                    content=item.input.content,
                    images=item.input.images,
                    artifacts=item.input.artifacts,
                )
            else:  # pragma: no cover - InputPayload is closed
                raise TypeError(f"Unsupported inbox input: {item.input!r}")
            event = MessagePublishedEvent(record=InputRecordPayload(record))
            self.publish_event(event)

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self.touch()
        self._publish_runtime_event(event.event)

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

    async def send_message(
        self,
        content: str,
        request_id: str,
        *,
        delivery: str = "steer",
        images: list[ImageRef] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> None:
        """Submit input; output is delivered by the session event stream."""
        if delivery not in {"queue", "steer"}:
            raise ValueError(f"Unsupported input delivery mode: {delivery}")
        user_input = HumanInput(
            content=content,
            images=tuple(images or ()),
            artifacts=tuple(artifacts or ()),
        )
        async with self._submission_lock:
            if not self.turn_lock.locked():
                try:
                    await start_turn(
                        self,
                        item=InboxItem(
                            id=request_id or f"input-{uuid.uuid4().hex}",
                            target=InboxTarget.NEXT_TURN,
                            input=user_input,
                        ),
                        request_id=request_id,
                    )
                except SessionBusy:
                    pass
                else:
                    self.touch()
                    return
            queued = delivery == "queue"
            await self.engine.submit_input(
                InboxItem(
                    id=request_id or f"input-{uuid.uuid4().hex}",
                    target=(
                        InboxTarget.NEXT_TURN
                        if queued
                        else InboxTarget.NEXT_STEP
                    ),
                    input=user_input,
                ),
                wake=True,
            )
            self.touch()

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
            await start_turn(self, item=None)
            task = self.turn_task
            if task is not None:
                await task
        except SessionBusy:
            pass
        finally:
            self.wakeup_task = None
            if self._wakeup_requested and not self.turn_lock.locked():
                self._request_wakeup()

    async def close(self, reason: str = "session_closed") -> None:
        self.close_reason = reason
        self.application.loop_state.session.status = "closing"
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
        await self.engine.discard_inputs()
        try:
            await self.engine.close_session()
        finally:
            # The session owns the XCore application lifetime. Engine only
            # closes its loop lifecycle; unloading plugin fibers belongs to
            # the surrounding application context.
            try:
                await self.application.close()
            finally:
                self.event_stream.close()
                self.application.loop_state.session.status = "closed"


class TurnEventRouter:
    """Publish a turn once on the runtime's central event stream."""

    def __init__(
        self,
        runtime: SessionRuntime,
        turn_id: TurnId,
    ) -> None:
        self._runtime = runtime
        self._turn_id = turn_id

    def emit(self, event) -> None:
        self._runtime.publish_event(
            event,
            scope=TurnScope(self._turn_id),
        )

    async def live_sink(
        self,
        client_event: InteractionRequest,
    ) -> InteractionResolution:
        request_id = client_event.interaction_id
        waiter = self._runtime.application.client_events.waiter_for(client_event)
        pending = waiter.register(request_id)
        self.emit(client_event)
        result = await waiter.wait_registered(
            request_id,
            pending,
            self._runtime.application.client_events.timeout_for(client_event),
        )
        self.emit(
            self._runtime.application.client_events.recorded_event(
                client_event,
                result,
            )
        )
        return result


@asynccontextmanager
async def _live_interaction_sink(
    runtime: SessionRuntime,
    router: TurnEventRouter,
) -> AsyncIterator[None]:
    # Installing the turn's live sink returns its disposer: the runtime owns
    # release, so the shared router is never left swapped.
    dispose = runtime.application.client_events.install(router.live_sink)
    try:
        yield
    finally:
        dispose()


async def _execute_turn(
    runtime: SessionRuntime,
    router: TurnEventRouter,
    *,
    item: InboxItem | None,
    request_id: str,
    interactive: bool | None,
    ready: asyncio.Event | None = None,
) -> None:
    """Run one turn independently of any transport response consumer."""
    turn_open = False
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
                    item,
                    request_id=request_id,
                )
                if item is not None
                else runtime.engine.run_pending(request_id=request_id)
            )
            async with aclosing(turn_stream):
                async for event in turn_stream:
                    if ready is not None:
                        ready.set()
                    router.emit(event)
                    if isinstance(event, LoopTurnStarted):
                        turn_open = True
                    elif isinstance(event, LoopTurnEnded):
                        turn_open = False
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
            str(exc) or type(exc).__name__,
        ))
        if turn_open:
            # Only a turn that reached its started boundary receives a
            # lifecycle terminal frame; pre-start failures terminate via the
            # typed error event above.
            router.emit(LoopTurnEnded(
                turn=runtime.engine.turn_count,
                outcome=TurnFinished(stop_reason="session_error"),
            ))
    finally:
        if ready is not None:
            ready.set()
        if runtime._active_router is router:
            runtime._active_router = None
        runtime.turn_task = None
        runtime.turn_lock.release()
        runtime.touch()
        if runtime._wakeup_requested and runtime.wakeup_task is None:
            runtime._request_wakeup()


async def start_turn(
    runtime: SessionRuntime,
    *,
    item: InboxItem | None,
    request_id: str = "",
    interactive: bool | None = None,
) -> None:
    """Start a turn whose authoritative output is the Session event stream."""
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    await runtime.turn_lock.acquire()
    try:
        router = TurnEventRouter(runtime, TurnId(uuid.uuid4().hex))
        runtime._active_router = router
        ready = asyncio.Event()
        task = asyncio.create_task(_execute_turn(
            runtime,
            router,
            item=item,
            request_id=request_id,
            interactive=interactive,
            ready=ready,
        ))
    except BaseException:
        runtime.turn_lock.release()
        raise
    runtime.turn_task = task
    await ready.wait()


async def start_regenerate_turn(
    runtime: SessionRuntime,
    *,
    request_id: str,
    interactive: bool | None = None,
) -> None:
    """Start regeneration with output published on the Session event stream."""
    if runtime.turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    await runtime.turn_lock.acquire()
    try:
        message = await runtime.application.history.regenerate_history()
        record = project_human_input(message)
        router = TurnEventRouter(runtime, TurnId(uuid.uuid4().hex))
        runtime._active_router = router
    except BaseException:
        runtime.turn_lock.release()
        raise
    task = asyncio.create_task(_execute_turn(
        runtime,
        router,
        item=InboxItem(
            id=message.input_id or request_id or f"input-{uuid.uuid4().hex}",
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(
                content=record.content,
                images=record.images,
                artifacts=record.artifacts,
            ),
        ),
        request_id=request_id,
        interactive=interactive,
    ))
    runtime.turn_task = task
__all__ = [
    "SessionBusy",
    "SessionRuntime",
    "start_regenerate_turn",
    "start_turn",
]
