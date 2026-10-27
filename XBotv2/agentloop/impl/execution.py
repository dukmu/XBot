"""Core ownership for one live Agent session."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
import uuid
from contextlib import aclosing, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import AsyncIterator
from XBotv2.agents.api.events import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agentloop.api.contracts import (
    Claimed,
    Consumed,
    Inserted,
    HumanInput,
    InboxItem,
)
from XBotv2.agentloop.api.outputs import (
    LoopError,
    LoopTurnStarted,
    LoopTurnEnded,
    TurnFinished,
)
from XBotv2.agentloop.api.events import RUNTIME_EVENT
from xcore.api.context import Context
from XBotv2.agentloop.impl.engine import Engine, _UNCHANGED, _Unchanged
from XBotv2.llm.api.contracts import ModelPort
from XBotv2.core.api.operations import dispatch_operation
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.agentloop.api.events import RuntimeEvent
from XBotv2.core.api.artifacts import ArtifactRef, ImageRef, ArtifactKind
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.domain import (
    EventScope,
    InboxTarget,
    SessionScope,
    TurnId,
    TurnScope,
    ReasoningGenerationMode,
)
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.agentloop.api.events import Events, ObserveInbox
from XBotv2.core.api.timing import conversation_stats
from XBotv2.core.api.history import HistoryPage
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.interactions.api.contracts import InteractionRequest, InteractionResolution
from XBotv2.session.api.contracts import (
    HISTORY_CHANGED,
    HistoryChanged,
    SessionEvent,
    SessionKey,
)
from XBotv2.agentloop.api.models import HistoryMutation, SessionStatus, PendingInputData
from XBotv2.session.api.events import (
    AgentConfiguredEvent,
    HistoryUpdatedEvent,
    InputAcceptedEvent,
    InputClaimedEvent,
    InputConsumedEvent,
    MessagePublishedEvent,
    QueueReplacedEvent,
)
from XBotv2.session.api.records import InputRecordPayload
from XBotv2.session.api.records import (
    conversation_replay,
    project_message,
    project_human_input,
)


class SessionBusy(OperationError):
    """The live session cannot accept the requested concurrent operation."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "thread_busy",
            f"Cannot start a turn in session {session_id!r} while a turn is active.",
            retryable=True,
        )


def require_idle(ctx: "AgentLoop", action: str) -> None:
    """Reject engine-mutating operations while a turn is active."""
    if ctx._turn_lock.locked():
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
class AgentLoop:
    """Protocol streams and one concrete agent-loop driver."""

    _context: Context
    _engine: Engine
    runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG
    interactive: bool = True
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submission_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _turn_task: asyncio.Task | None = None
    wakeup_task: asyncio.Task | None = None
    last_activity: float = field(default_factory=time.monotonic)
    _wakeup_requested: bool = False
    _idle_owner: asyncio.Task | None = field(default=None, init=False)
    _active_router: "TurnEventRouter | None" = field(default=None, init=False)
    _log: RuntimeLog = field(init=False)

    @property
    def key(self) -> SessionKey:
        return self._key

    @property
    def session_id(self) -> str:
        return self.key.session_id

    @property
    def thread_id(self) -> str:
        return self.key.thread_id

    def __post_init__(self) -> None:
        self._key = self._context.require(LOOP_STATE).session.key
        self._log = self.runtime_log.bind(
            "session",
            session_id=self.session_id,
            thread_id=self.thread_id,
        )
        self.touch()
        events = self._context
        events.on(Events.INBOX_CHANGED, self._on_inbox_changed)
        events.on(RUNTIME_EVENT, self._on_runtime_event)
        events.on(HISTORY_CHANGED, self._on_history_changed)
        events.on(AGENT_CONFIGURED, self._on_agent_configured)

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    @property
    def pending_input_count(self) -> int:
        return self._engine.pending_input_count

    @asynccontextmanager
    async def idle(self, action):
        current = asyncio.current_task()
        if self._idle_owner is current:
            yield
            return
        require_idle(self, action)
        async with self._submission_lock, self._turn_lock:
            self._idle_owner = current
            try:
                yield
            finally:
                self._idle_owner = None

    async def invoke(self, operation, request):
        self.touch()
        if not self._context.is_active:
            raise OperationError("session_closing", "Thread is closing")
        if not operation.requires_exclusive(request):
            return await dispatch_operation(self._context, operation, request)
        async with self.idle(f"run {operation.name!r}"):
            return await dispatch_operation(self._context, operation, request)

    def configure(
        self,
        *,
        model_client: ModelPort,
        max_iterations: int | None | _Unchanged = _UNCHANGED,
    ) -> None:
        self._engine.configure(model_client=model_client, max_iterations=max_iterations)

    async def submit_input(self, item, *, wake):
        if (
            not self._context.is_active
            or self._context.require(LOOP_STATE).session.status != "active"
        ):
            raise OperationError("session_closing", "Thread is closing")
        await self._engine.submit_input(item, wake=wake)
        self.touch()

    @property
    def provider_name(self) -> str:
        return self._context.require(
            LOOP_STATE
        ).metadata.value.runtime_selection.model.route.provider

    @property
    def workspace_root(self) -> str:
        return self._context.require(LOOP_STATE).metadata.value.workspace_root

    def touch(self) -> None:
        """Mark the runtime active; resets the idle-reaper deadline."""
        self.last_activity = time.monotonic()

    def resume_pending_inputs(self) -> bool:
        """Resume durable inbox work after the runtime is fully registered."""
        pending = self._engine.pending_input_count
        if not pending:
            return False
        self._log.info("session.inbox.resuming", pending_inputs=pending)
        self._request_wakeup()
        return True

    async def _on_history_changed(self, event: HistoryChanged) -> None:
        """Project history replacement (``/clear``, ``/undo``) as an event."""
        page = await self._context.require(ARCHIVE).message_page(
            self.session_id, self.thread_id, limit=160
        )
        self.publish_event(
            HistoryUpdatedEvent(
                operation=event.operation,
                mutation=HistoryMutation(
                    removed_turns=event.turns,
                    history=HistoryPage(
                        items=conversation_replay(page.items),
                        older_cursor=page.older_cursor,
                    ),
                    stats=conversation_stats(event.messages),
                ),
            )
        )

    async def _on_agent_configured(self, event: AgentConfigured) -> None:
        """Publish the effective selection without flattening its fields."""
        self.publish_event(
            AgentConfiguredEvent(
                runtime_selection=event.runtime_selection,
            )
        )

    def publish_event(
        self,
        event: SessionEvent,
        *,
        scope: EventScope = SessionScope(),
    ) -> None:
        self._event_stream.publish(event, scope=scope)

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
        self.publish_event(splice_event)
        if isinstance(change, Inserted):
            self.publish_event(
                InputAcceptedEvent(
                    message_ids=(change.item.id,),
                    target=change.item.target,
                )
            )
        if isinstance(change, Claimed):
            claimed = InputClaimedEvent(message_ids=change.ids)
            if self._active_router is not None:
                self._active_router.emit(claimed)
            else:
                self.publish_event(claimed)
        if isinstance(change, Consumed):
            consumed_ids = set(change.ids)
            for message in self._context.require(LOOP_STATE).messages:
                if (
                    isinstance(message, (HumanInputMessage, RuntimeNoticeMessage))
                    and message.id in consumed_ids
                ):
                    self.publish_event(
                        MessagePublishedEvent(
                            record=InputRecordPayload(project_message(message))
                        )
                    )
            consumed = InputConsumedEvent(message_ids=change.ids)
            if self._active_router is not None:
                self._active_router.emit(consumed)
            else:
                self.publish_event(consumed)

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self.touch()
        self.publish_event(event.event)

    def pending_inputs(self) -> tuple[PendingInputData, ...]:
        return tuple(
            _pending_input_snapshot(item) for item in self._engine.pending_inputs
        )

    async def update_pending_input(
        self,
        message_id: str,
        action: str,
        content: str = "",
    ) -> tuple[PendingInputData, ...]:
        try:
            if action == "edit":
                await self._engine.edit_input(message_id, content)
            elif action == "remove":
                await self._engine.remove_input(message_id)
            elif action == "steer":
                await self._engine.retarget_input(message_id, InboxTarget.NEXT_STEP)
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
            status = self._context.require(LOOP_STATE).session.status
            if not self._context.is_active or status != "active":
                raise OperationError(
                    "session_closing",
                    f"Cannot accept input while the session is {status}.",
                )
            busy = self._turn_lock.locked()
            item = InboxItem(
                id=request_id or f"input-{uuid.uuid4().hex}",
                target=InboxTarget.NEXT_STEP
                if busy and delivery == "steer"
                else InboxTarget.NEXT_TURN,
                input=user_input,
            )
            # Durable acceptance happens in this caller before any model task
            # or successful HTTP response can escape. Retry keeps the same ID.
            if not await self._engine.submit_input(item, wake=False):
                return
            if not busy:
                await start_turn(self, item=None, request_id=request_id)
            if self._engine.pending_input_count:
                self._request_wakeup()
            self.touch()

    async def submit_message(self, request) -> None:
        if (
            not self._context.is_active
            or self._context.require(LOOP_STATE).session.status != "active"
        ):
            raise OperationError("session_closing", "Thread is closing")
        images, attachments = [], []
        for item in request.images:
            ref = await self._context.artifacts.put(
                ArtifactKind.MEDIA, _upload_bytes(item.data), media_type=item.media_type
            )
            images.append(
                ImageRef(artifact_id=ref.id, media_type=ref.media_type, size=ref.size)
            )
        for item in request.attachments:
            attachments.append(
                await self._context.artifacts.put(
                    ArtifactKind.ATTACHMENT,
                    _upload_bytes(item.data),
                    media_type=item.media_type,
                    name=item.name,
                )
            )
        await self.send_message(
            request.content,
            request.request_id,
            delivery=request.delivery,
            images=images,
            artifacts=attachments,
        )

    async def clear_history(self, *, history_limit=None):
        return await self._change_history(None, history_limit)

    async def undo_history(self, count, *, history_limit=None):
        return await self._change_history(count, history_limit)

    async def _change_history(self, count, limit):
        async with self.idle("change history"):
            history = self._context.require(LOOP_STATE).history
            if count is None:
                removed = sum(
                    isinstance(message, HumanInputMessage) for message in history
                )
                await history.clear()
                operation = "clear"
            else:
                try:
                    await history.undo(count)
                except ValueError as exc:
                    raise OperationError("invalid_undo_count", str(exc)) from exc
                removed = count
                operation = "undo"
            await self._context.emit(
                HISTORY_CHANGED, HistoryChanged(history.snapshot(), operation, removed)
            )
        history = self._context.require(LOOP_STATE).history
        page = (
            HistoryPage(items=history.snapshot(), older_cursor=None)
            if limit is None
            else history.page(limit=limit)
        )
        return HistoryMutation(
            removed_turns=removed,
            history=HistoryPage(
                items=conversation_replay(page.items), older_cursor=page.older_cursor
            ),
            stats=conversation_stats(history.snapshot()),
        )

    async def interrupt(self) -> bool:
        task = self._turn_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def _request_wakeup(self) -> None:
        """Wake the loop driver for followup/steer; inject never calls this."""
        if self._context.require(LOOP_STATE).session.status != "active":
            self._wakeup_requested = False
            return
        self._wakeup_requested = True
        if self._turn_lock.locked() or self.wakeup_task is not None:
            return
        self.wakeup_task = asyncio.create_task(self._run_wakeup())

    async def _run_wakeup(self) -> None:
        try:
            if self._turn_lock.locked():
                return
            self._wakeup_requested = False
            await start_turn(self, item=None)
            if self._engine.pending_input_count:
                self._wakeup_requested = True
            task = self._turn_task
            if task is not None:
                await task
        except SessionBusy:
            pass
        except OperationError as exc:
            if exc.code != "session_closing":
                raise
        finally:
            self.wakeup_task = None
            if self._wakeup_requested and not self._turn_lock.locked():
                self._request_wakeup()

    @property
    def _event_stream(self):
        return self._context.require(THREAD_EVENTS)

    def status(self) -> SessionStatus:
        state = self._context.require(LOOP_STATE)
        metadata = state.metadata.value
        selection = metadata.runtime_selection
        mode = selection.model.generation.mode
        return SessionStatus(
            session_id=self.session_id,
            thread_id=self.thread_id,
            workspace_root=metadata.workspace_root,
            agent=selection.agent_name,
            provider=selection.model.route.provider,
            model=selection.model.route.model,
            model_mode=mode.effort if isinstance(mode, ReasoningGenerationMode) else "",
            context_window=selection.model.context_window,
            title=metadata.title,
            status=state.session.status,
            resumed=state.resumed,
            turn_count=state.turn_count,
            message_count=len(state.history),
            pending_inputs=self.pending_input_count,
        )

    async def _regenerate_history(self):
        history = self._context.require(LOOP_STATE).history
        index = next(
            (
                position
                for position in range(len(history) - 1, -1, -1)
                if isinstance(history[position], HumanInputMessage)
            ),
            None,
        )
        if index is None:
            raise OperationError(
                "nothing_to_regenerate",
                "History has no human-authored turn to regenerate.",
            )
        message = history[index]
        await history.replace_range(index, len(history), (), operation="regenerate")
        await self._context.emit(
            HISTORY_CHANGED, HistoryChanged(history.snapshot(), "regenerate", 1)
        )
        return message

    async def regenerate(self, request_id: str) -> None:
        await start_regenerate_turn(self, request_id=request_id)

    async def drain(self) -> None:
        async with self._submission_lock:
            self._context.require(LOOP_STATE).session.status = "closing"
            self._wakeup_requested = False
        task = self._turn_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._turn_task = None
        continuation = self.wakeup_task
        if (
            continuation is not None
            and not continuation.done()
            and continuation is not asyncio.current_task()
        ):
            continuation.cancel()
            await asyncio.gather(continuation, return_exceptions=True)
        self.wakeup_task = None
        failure: BaseException | None = None
        try:
            try:
                await self._engine.close_session()
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(f"Engine close also failed: {exc!r}")
            self._event_stream.close()
        finally:
            self._context.require(LOOP_STATE).session.status = "closed"
        if failure is not None:
            raise failure


class TurnEventRouter:
    """Publish a turn once on the runtime's central event stream."""

    def __init__(
        self,
        runtime: AgentLoop,
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
        waiter = self._runtime._context.require(CLIENT_EVENTS).waiter_for(client_event)
        pending = waiter.register(request_id)
        self.emit(client_event)
        result = await waiter.wait_registered(
            request_id,
            pending,
            self._runtime._context.require(CLIENT_EVENTS).timeout_for(client_event),
        )
        self.emit(
            self._runtime._context.require(CLIENT_EVENTS).recorded_event(
                client_event,
                result,
            )
        )
        return result


@asynccontextmanager
async def _live_interaction_sink(
    runtime: AgentLoop,
    router: TurnEventRouter,
) -> AsyncIterator[None]:
    # Installing the turn's live sink returns its disposer: the runtime owns
    # release, so the shared router is never left swapped.
    dispose = runtime._context.require(CLIENT_EVENTS).install(router.live_sink)
    try:
        yield
    finally:
        dispose()


async def _execute_turn(
    runtime: AgentLoop,
    router: TurnEventRouter,
    *,
    item: InboxItem | None,
    request_id: str,
    interactive: bool | None,
    ready: asyncio.Event | None = None,
) -> None:
    """Run one turn independently of any transport response consumer."""
    turn_open = False
    terminal = None
    try:
        live_interactive = runtime.interactive if interactive is None else interactive
        interaction_sink = (
            _live_interaction_sink(runtime, router)
            if live_interactive
            else nullcontext()
        )
        async with interaction_sink:
            turn_stream = (
                runtime._engine.run_turn(
                    item,
                    request_id=request_id,
                )
                if item is not None
                else runtime._engine.run_pending(request_id=request_id)
            )
            async with aclosing(turn_stream):
                async for event in turn_stream:
                    if ready is not None:
                        ready.set()
                    if isinstance(event, LoopTurnEnded):
                        terminal = event
                    else:
                        router.emit(event)
                        if isinstance(event, LoopTurnStarted):
                            turn_open = True
    except asyncio.CancelledError:
        runtime._log.info("session.turn.cancelled", request_id=request_id)
        raise
    except Exception as exc:  # noqa: BLE001
        runtime._log.exception(
            "session.turn.failed",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        router.emit(
            LoopError(
                code="turn_failed",
                message=str(exc) or type(exc).__name__,
            )
        )
        if turn_open:
            # Only a turn that reached its started boundary receives a
            # lifecycle terminal frame; pre-start failures terminate via the
            # typed error event above.
            terminal = LoopTurnEnded(
                turn=runtime._engine.turn_count,
                outcome=TurnFinished(stop_reason="session_error"),
            )
    finally:
        if ready is not None:
            ready.set()
        if runtime._active_router is router:
            runtime._active_router = None
        runtime._turn_task = None
        runtime._turn_lock.release()
        runtime.touch()
        if terminal is not None:
            router.emit(terminal)
        if runtime._wakeup_requested and runtime.wakeup_task is None:
            runtime._request_wakeup()


async def start_turn(
    runtime: AgentLoop,
    *,
    item: InboxItem | None,
    request_id: str = "",
    interactive: bool | None = None,
) -> None:
    """Start a turn whose authoritative output is the Session event stream."""
    if runtime._context.require(LOOP_STATE).session.status != "active":
        raise OperationError(
            "session_closing",
            "Cannot start a turn while the session is closing.",
        )
    if runtime._turn_lock.locked():
        raise SessionBusy(runtime.session_id)
    await runtime._turn_lock.acquire()
    try:
        router = TurnEventRouter(runtime, TurnId(uuid.uuid4().hex))
        runtime._active_router = router
        ready = asyncio.Event()
        task = asyncio.create_task(
            _execute_turn(
                runtime,
                router,
                item=item,
                request_id=request_id,
                interactive=interactive,
                ready=ready,
            )
        )
    except BaseException:
        runtime._turn_lock.release()
        raise
    runtime._turn_task = task
    await ready.wait()


async def start_regenerate_turn(
    runtime: AgentLoop,
    *,
    request_id: str,
    interactive: bool | None = None,
) -> None:
    """Start regeneration with output published on the Session event stream."""
    async with runtime._submission_lock:
        if runtime._context.require(LOOP_STATE).session.status != "active":
            raise OperationError(
                "session_closing",
                "Cannot regenerate while the session is closing.",
            )
        if runtime._turn_lock.locked():
            raise SessionBusy(runtime.session_id)
        await runtime._turn_lock.acquire()
        try:
            message = await runtime._regenerate_history()
            record = project_human_input(message)
            router = TurnEventRouter(runtime, TurnId(uuid.uuid4().hex))
            runtime._active_router = router
        except BaseException:
            runtime._turn_lock.release()
            raise
        task = asyncio.create_task(
            _execute_turn(
                runtime,
                router,
                item=InboxItem(
                    id=request_id or f"input-{uuid.uuid4().hex}",
                    target=InboxTarget.NEXT_TURN,
                    input=HumanInput(
                        content=record.content,
                        images=record.images,
                        artifacts=record.artifacts,
                    ),
                ),
                request_id=request_id,
                interactive=interactive,
            )
        )
        runtime._turn_task = task


def _upload_bytes(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise OperationError("invalid_upload", "Invalid base64 attachment") from exc
