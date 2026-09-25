"""Core ReAct loop engine.

The engine runs a 3-node ReAct loop and contains no planning, DAG, skill,
compaction, memory, summary, persistence, or subagent concepts.

Without plugins, the engine implements:
    prepare_context → agent → tools → repeat (ReAct loop)

Each stage dispatches runtime events on the plugin context. Loop events
(before/after context/agent/tools) are short-circuit: the first non-None result
is interpreted by the engine.

Architecture constraint: Engine imports only loop-owned services and core
contracts. Application composition resolves all feature-plugin dependencies.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from pydantic import JsonValue

from XBotv2.agentloop.inbox import AgentInbox
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopEvent,
    LoopTurnEnded,
    LoopTurnStarted,
    StartedToolCall,
    ToolCallArgumentsDelta,
    ToolCallsStarted,
    ToolCompleted,
    TurnCancelled,
    TurnFinished,
    UsageObserved,
    is_loop_event,
)
from XBotv2.agentloop.events import (
    EventPort,
    Events,
    AcceptInput,
    AfterContextBuild,
    AfterModelResponse,
    BeforeContextBuild,
    BeforeModelRequest,
    CompleteTurn as HookCompleteTurn,
    InputAccepted,
    KeepContext,
    KeepContextRequest,
    KeepRequest,
    KeepResponse,
    LoopFailure,
    ModelRequestReady,
    ModelResponseObserved,
    OnModelFailure,
    OnTurnInput,
    PropagateFailure,
    RejectInput,
    ReplaceContext,
    ReplaceContextRequest,
    ReplaceRequest,
    ReplaceResponse,
    RetryRequest,
    SessionLifecycle,
    StateChanged,
    ToolCallsObserved,
    ToolMessageObserved,
    TurnEnded,
    TurnStarted,
    SHORT_CIRCUIT_EVENTS,
)
from XBotv2.agentloop.contracts import (
    DEFAULT_MAX_ITERATIONS,
    HumanInput,
    InboxItem,
    InboxTarget,
    LoopState,
    RuntimeInput,
)
from XBotv2.agentloop.contracts import AgentLoopDriverPort, ToolsPort
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import (
    AssistantMessage,
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.provider import ModelRequest, ProviderMessage, ProviderSystem, ToolSchema
from XBotv2.core.providers import ProviderFailure
from XBotv2.core.stream import (
    ModelCancelled,
    ModelCompleted,
    ModelFailed,
    ModelResponse,
    ModelStreamEvent,
    ToolCallDelta,
    ReasoningDelta,
    TextDelta,
)
from XBotv2.core.domain import (
    ModelExchange,
    ModelTiming,
    RequestObservation,
    ToolCallId,
    TurnRequest,
    ToolTiming,
    UsageDelta,
)
from XBotv2.config.contracts import UserContext
from XBotv2.core.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.context_builder import (
    BUILD_CONTEXT,
    CONTEXT_BUILD_INPUTS_READY,
    ContextBuildRequest,
)
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.tokens import estimate_request_tokens
from XBotv2.llm import ModelPort
from XBotv2.session.contracts import SessionRuntimeState
from XBotv2.core.tools import (
    Tool,
    ToolCall,
    ToolCallRef,
    CompleteTurn as CompleteTurnDirective,
    ToolCancelled,
    ToolExecution,
    ContinueTurn,
    TurnDirective,
    ToolError,
    ToolFailed,
    ToolOutput,
    ToolOutcome,
    ToolSucceeded,
)

class _Unchanged:
    """Sentinel distinguishing an omitted model replacement from ``None``."""


_UNCHANGED = _Unchanged()


@dataclass(slots=True)
class _TurnStartResult:
    user_input: str
    events: list[LoopEvent]
    proceed: bool


@dataclass(frozen=True, slots=True)
class _ContextReady:
    messages: tuple[ProviderMessage, ...]


@dataclass(frozen=True, slots=True)
class _ContextCompleted:
    event: LoopEvent


_ContextBuildResult = _ContextReady | _ContextCompleted


@dataclass(frozen=True, slots=True)
class _ModelRequestReady:
    request: ModelRequest


@dataclass(frozen=True, slots=True)
class _ModelRequestRebuild:
    pass


@dataclass(frozen=True, slots=True)
class _ModelRequestCompleted:
    event: LoopEvent


_ModelRequestResult = (
    _ModelRequestReady | _ModelRequestRebuild | _ModelRequestCompleted
)


@dataclass(frozen=True, slots=True)
class _ToolBatchResult:
    directive: TurnDirective


@dataclass(slots=True)
class _ModelResponseEvent:
    response: ModelResponse
    timing: ModelTiming


class Engine(AgentLoopDriverPort):
    """Core ReAct loop engine.

    No plugin imports. No DAG, skills, or compaction logic.
    All extension behavior comes through runtime events and the tool registry.

    Usage::

        services = await start_application(...)
        engine = services.engine
        item = InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="list files"),
        )
        async for event in engine.run_turn(item):
            print(event)
    """

    def __init__(
        self,
        *,
        model_client: ModelPort,
        tools: ToolsPort,
        events: EventPort,
        state: LoopState,
        user_identity: UserContext,
        memory: str,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
        inbox: AgentInbox,
    ) -> None:
        self.model_client = model_client
        self.tools = tools
        self._events = events
        self.state = state
        self.user_identity = user_identity
        self.memory = memory
        self.max_iterations = max_iterations
        self._log = runtime_log.bind("engine")
        self.inbox = inbox
        self._request_id: ContextVar[str] = ContextVar(
            f"xbotv2_request_id_{id(self)}",
            default="",
        )

    @property
    def messages(self) -> ConversationHistory:
        return self.state.messages

    @property
    def turn_count(self) -> int:
        return self.state.turn_count

    @turn_count.setter
    def turn_count(self, value: int) -> None:
        self.state.turn_count = value

    @property
    def session(self) -> SessionRuntimeState:
        return self.state.session

    async def _dispatch(
        self,
        event: str,
        payload: object,
        *,
        short_circuit: bool | None = None,
    ) -> Any:
        """Dispatch one runtime event on the plugin context.

        Short-circuit events use ``ctx.serial``: listeners run in registration
        order and the first non-``None`` dict answer wins, so later listeners
        do not run. Independent observers of a short-circuit event must
        therefore register with ``prepend=True`` (they never answer) — that
        is the explicit priority contract for these events; observers of
        non-short-circuit events use ``ctx.emit`` and all run.
        """
        if short_circuit is None:
            short_circuit = event in SHORT_CIRCUIT_EVENTS
        if short_circuit:
            return await self._events.serial(event, payload)
        await self._events.emit(event, payload)
        return None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self) -> None:
        """Dispatch lifecycle for the already-prepared loop state."""
        if self.state.resumed:
            await self._resume_loaded_state()
            return
        await self._dispatch(
            Events.SESSION_START, SessionLifecycle(self.session), short_circuit=False
        )

    async def _resume_loaded_state(self) -> None:
        self._close_interrupted_tool_calls(
            ToolFailed(
                error=ToolError(
                    code="session_restarted",
                    message="Tool call did not complete: session_restarted",
                ),
                output=ToolOutput(
                    parts=(TextPart(text="Tool call did not complete: session_restarted."),)
                ),
            )
        )
        await self._dispatch(
            Events.SESSION_RESUME, SessionLifecycle(self.session), short_circuit=False
        )
        await self._publish_state_change()

    async def close_session(self) -> None:
        """Dispatch the loop lifecycle close boundary."""
        await self._dispatch(
            Events.SESSION_CLOSE, SessionLifecycle(self.session), short_circuit=False
        )
        await self._publish_state_change()

    async def _prepare_tool_calls(
        self,
        tool_calls: list[ToolCall],
        *,
        agent_response: ModelResponse | None = None,
    ) -> bool:
        del agent_response
        await self._dispatch(
            Events.TOOL_CALLS_OBSERVED,
            ToolCallsObserved(tuple(tool_calls)),
            short_circuit=False,
        )
        return True

    def _tool_kind(self, name: str) -> str:
        """Read the tool owner's declared model-facing category."""
        tool = self.tools.resolve(name) if name else None
        return tool.kind if tool is not None else "other"

    async def submit_input(self, item: InboxItem, *, wake: bool) -> None:
        await self.inbox.submit(item, wake=wake)

    @property
    def pending_input_count(self) -> int:
        return len(self.inbox)

    @property
    def pending_inputs(self) -> tuple[InboxItem, ...]:
        return tuple(self.inbox.pending)

    async def edit_input(self, message_id: str, content: str) -> InboxItem:
        return await self.inbox.edit(message_id, content)

    async def remove_input(self, message_id: str) -> InboxItem:
        return await self.inbox.remove(message_id)

    async def retarget_input(
        self,
        message_id: str,
        target: InboxTarget,
    ) -> InboxItem:
        return await self.inbox.retarget(message_id, target)

    async def discard_inputs(self) -> None:
        await self.inbox.discard()

    def configure(
        self,
        *,
        model_client: ModelPort | _Unchanged = _UNCHANGED,
        max_iterations: int | None = None,
    ) -> None:
        """Replace loop-owned runtime ports without copying effective config."""
        if model_client is not _UNCHANGED:
            self.model_client = model_client
        if max_iterations is not None:
            self.max_iterations = max_iterations

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        item: InboxItem,
        *,
        request_id: str = "",
    ) -> AsyncIterator[LoopEvent]:
        if item.target is not InboxTarget.NEXT_TURN:
            raise ValueError("A direct turn input must target the next turn")
        await self.inbox.submit(item, wake=False)
        async for event in self.run_pending(request_id=request_id):
            yield event

    async def run_pending(
        self,
        *,
        request_id: str = "",
    ) -> AsyncIterator[LoopEvent]:
        """Run one turn claimed from the agent-owned inbox."""
        claimed = await self.inbox.claim_turn()
        if not claimed:
            return
        request_token = self._request_id.set(request_id)
        log_token = push_log_context(
            session_id=self.session.session_id,
            thread_id=self.session.thread_id,
            request_id=request_id,
        )
        turn_started = False
        turn_ended = False
        turn_started_at = time.perf_counter()
        outcome = "completed"
        self._log.info(
            "turn.start",
            inputs=len(claimed),
            input_chars=sum(len(item.input.content) for item in claimed),
        )
        try:
            async for event in self._run_turn_impl(
                claimed,
            ):
                if event.kind == "turn_started":
                    turn_started = True
                elif event.kind == "turn_ended":
                    turn_ended = True
                yield event
        except asyncio.CancelledError:
            outcome = "cancelled"
            self._log.info("turn.interrupted", turn=self.turn_count)
            interrupted = self._close_interrupted_tool_calls(
                ToolCancelled(reason="client_interrupt")
            )
            for message, name in interrupted:
                yield ToolCompleted(
                    execution=ToolExecution(
                        message=message,
                        directive=ContinueTurn(),
                    )
                )
            if not turn_ended:
                await self._dispatch(Events.TURN_END, TurnEnded(
                    self.session, tuple(self.messages), "client_interrupt"
                ),
                    short_circuit=False,
                )
            yield LoopTurnEnded(
                turn=self.turn_count,
                outcome=TurnCancelled(reason="client_interrupt"),
            )
            raise
        except BaseException as exc:
            outcome = "error"
            self._log.exception(
                "turn.failed",
                turn=self.turn_count,
                error_type=type(exc).__name__,
            )
            failure_ctx = LoopFailure(self.session, tuple(self.messages), exc)
            await self._dispatch(Events.ON_STOP_FAILURE, failure_ctx, short_circuit=False)
            await self._dispatch(Events.ON_ERROR, failure_ctx, short_circuit=False)
            yield LoopError(
                code="engine_error",
                message=str(exc) or type(exc).__name__,
                exception_type=type(exc).__name__,
            )
            if turn_started:
                yield LoopTurnEnded(
                    turn=self.turn_count,
                    outcome=TurnFinished(stop_reason="error"),
                )
        finally:
            try:
                await self.inbox.reconcile(
                    [item.id for item in claimed],
                    {
                        message.input_id
                        for message in self.messages
                        if isinstance(message, HumanInputMessage)
                    },
                )
                await self._publish_state_change()
            except Exception:
                outcome = "state_error"
                raise
            finally:
                self._request_id.reset(request_token)
                self._log.info(
                    "turn.finish",
                    turn=self.turn_count,
                    outcome=outcome,
                    duration_ms=round(
                        (time.perf_counter() - turn_started_at) * 1000,
                        3,
                    ),
                )
                reset_log_context(log_token)

    async def _run_turn_impl(
        self,
        claimed: list[InboxItem],
    ) -> AsyncIterator[LoopEvent]:
        """Execute one user turn through the ReAct loop.

        Yields event dicts: {"type": str, "data": {...}}
        """
        turn_start = await self._start_claimed_turn(claimed)
        for event in turn_start.events:
            yield event
        if not turn_start.proceed:
            return
        # 4. ReAct loop
        iteration = 0
        turn_complete = False
        iteration_limit_reached = False
        retrying_iteration = False
        retry_request: ModelRequest | None = None

        while not turn_complete:
            if retrying_iteration:
                retrying_iteration = False
            else:
                finalizing = iteration >= self.max_iterations
                if finalizing:
                    if iteration_limit_reached:
                        break
                    iteration_limit_reached = True
                else:
                    iteration += 1

            if retry_request is not None:
                model_request = retry_request
                retry_request = None
            else:
                while True:
                    context_build = await self._build_turn_context()
                    if isinstance(context_build, _ContextCompleted):
                        yield context_build.event
                        turn_complete = True
                        break
                    context_messages = list(context_build.messages)
                    if finalizing:
                        notice_at = (
                            1
                            if context_messages
                            and context_messages[0].role == "system"
                            else 0
                        )
                        context_messages.insert(
                            notice_at,
                            ProviderSystem(parts=(TextPart(text=self._iteration_limit_notice()),)),
                        )
                    model_preparation = await self._prepare_model_request(
                        context_messages
                    )
                    if isinstance(model_preparation, _ModelRequestCompleted):
                        yield model_preparation.event
                        turn_complete = True
                        break
                    if isinstance(model_preparation, _ModelRequestRebuild):
                        continue
                    model_request = model_preparation.request
                    if finalizing:
                        model_request = model_request.model_copy(update={"tools": ()})
                    break
            if turn_complete:
                break
            try:
                response = None
                response_timing = ModelTiming(total_ms=0.0)
                async for model_event in self._stream_model_response(
                    model_request,
                ):
                    if isinstance(model_event, _ModelResponseEvent):
                        response = model_event.response
                        response_timing = model_event.timing
                    else:
                        yield model_event
                if response is None:
                    raise RuntimeError("LLM stream completed without a response")
            except asyncio.TimeoutError as exc:
                self._log.error("llm.request.timeout", turn=self.turn_count)
                failure_revision = self.state.history.surface_revision
                recovery = await self._dispatch(
                    Events.MODEL_REQUEST_ERROR,
                    OnModelFailure(model_request, exc),
                    short_circuit=True,
                )
                if isinstance(recovery, RetryRequest):
                    retry_request = (
                        recovery.request
                        if self.state.history.surface_revision == failure_revision
                        else None
                    )
                    retrying_iteration = True
                    continue
                if isinstance(recovery, HookCompleteTurn):
                    if not is_loop_event(recovery.result):
                        raise TypeError("CompleteTurn result must be a LoopEvent")
                    yield recovery.result
                    turn_complete = True
                    break
                if recovery is not None and not isinstance(recovery, PropagateFailure):
                    raise TypeError(
                        "OnModelFailure must return PropagateFailure, "
                        "RetryRequest, CompleteTurn, or None"
                    )
                raise asyncio.TimeoutError("LLM call timed out") from None
            except Exception as exc:
                self._log.exception(
                    "llm.request.error",
                    provider=model_request.selection.route.provider,
                    model=model_request.selection.route.model,
                    error_type=type(exc).__name__,
                )
                failure_revision = self.state.history.surface_revision
                recovery = await self._dispatch(
                    Events.MODEL_REQUEST_ERROR,
                    OnModelFailure(model_request, exc),
                    short_circuit=True,
                )
                if isinstance(recovery, RetryRequest):
                    retry_request = (
                        recovery.request
                        if self.state.history.surface_revision == failure_revision
                        else None
                    )
                    retrying_iteration = True
                    continue
                if isinstance(recovery, HookCompleteTurn):
                    if not is_loop_event(recovery.result):
                        raise TypeError("CompleteTurn result must be a LoopEvent")
                    yield recovery.result
                    turn_complete = True
                    break
                if recovery is not None and not isinstance(recovery, PropagateFailure):
                    raise TypeError(
                        "OnModelFailure must return PropagateFailure, "
                        "RetryRequest, CompleteTurn, or None"
                    )
                raise
            response_result = await self._dispatch(
                Events.AFTER_MODEL_RESPONSE,
                AfterModelResponse(model_request, response),
                short_circuit=True,
            )
            if isinstance(response_result, ReplaceResponse):
                response = response_result.response
            elif isinstance(response_result, HookCompleteTurn):
                if not is_loop_event(response_result.result):
                    raise TypeError("CompleteTurn result must be a LoopEvent")
                yield response_result.result
                turn_complete = True
                break
            elif response_result is not None and not isinstance(
                response_result, KeepResponse
            ):
                raise TypeError(
                    "AfterModelResponse must return KeepResponse, "
                    "ReplaceResponse, CompleteTurn, or None"
                )
            text_parts = tuple(part for part in response.parts if isinstance(part, TextPart))
            reasoning_parts = tuple(
                part for part in response.parts if isinstance(part, ReasoningPart)
            )
            tool_calls = tuple(part for part in response.parts if isinstance(part, ToolCall))
            content = "".join(part.text for part in text_parts)
            if finalizing and tool_calls:
                names = ", ".join(call.name for call in tool_calls)
                raise RuntimeError(
                    "LLM requested tools after the iteration budget was "
                    f"exhausted: {names}"
                )
            if not content.strip() and not tool_calls:
                reasoning = "".join(part.text for part in reasoning_parts)
                after_tool = bool(
                    self.messages and isinstance(self.messages[-1], ToolMessage)
                )
                stop_reason = response.stop.kind
                context = " after ToolResult" if after_tool else ""
                self._log.debug(
                    "llm.response.invalid",
                    after_tool=after_tool,
                    stop_reason=stop_reason,
                    reasoning_chars=len(reasoning),
                )
                raise RuntimeError(
                    f"LLM returned no assistant content or ToolUse{context} "
                    f"(stop_reason={stop_reason}, reasoning_chars={len(reasoning)})"
                )
            response_id = f"assistant-{self.turn_count}-{iteration}-{uuid.uuid4().hex[:8]}"
            observation = RequestObservation(
                selection=model_request.selection,
                purpose=TurnRequest(turn_id=self._request_id.get()),
                estimated_input_tokens=estimate_request_tokens(
                    model_request.messages,
                    model_request.tools,
                ),
                observed_context=response.observed_context,
            )
            exchange = ModelExchange(
                observation=observation,
                usage=response.usage,
                timing=response_timing,
                stop=response.stop,
                provider_extensions=response.provider_extensions,
            )
            response_msg = AssistantMessage(
                id=response_id,
                parts=response.parts,
                exchange=exchange,
            )
            await self._dispatch(
                Events.MODEL_RESPONSE_OBSERVED,
                ModelResponseObserved(exchange),
                short_circuit=False,
            )
            yield AssistantCompleted(message=response_msg)
            if response.usage.counters.output or response.usage.counters.input:
                yield UsageObserved(usage=response.usage)

            self.messages.append(response_msg)

            # Check for tool calls
            if not tool_calls:
                # A complete response: fold any pending input so it is
                # answered in this same turn instead of waiting for a later
                # one. This is the no-tool-boundary path.
                if await self._claim_step_inputs():
                    continue
                turn_complete = True
                break

            batch_result: _ToolBatchResult | None = None
            async for tool_event in self._run_tool_batch(response):
                if isinstance(tool_event, _ToolBatchResult):
                    batch_result = tool_event
                else:
                    yield tool_event
            if batch_result is None:
                raise RuntimeError("Tool batch completed without an outcome")
            if isinstance(batch_result.directive, CompleteTurnDirective):
                turn_complete = True
                break
            await self._claim_step_inputs()

        stop_reason = (
            "max_iterations" if iteration_limit_reached else "completed"
        )
        yield await self._finish_turn(stop_reason)

    async def _run_tool_batch(
        self,
        response: ModelResponse,
    ) -> AsyncIterator[LoopEvent | _ToolBatchResult]:
        tool_calls = [
            part for part in response.parts if isinstance(part, ToolCall)
        ]
        if not await self._prepare_tool_calls(
            tool_calls,
            agent_response=response,
        ):
            yield _ToolBatchResult(directive=CompleteTurnDirective())
            return

        self._log.info(
            "tool.batch.started",
            turn=self.turn_count,
            count=len(tool_calls),
            names=[call.name for call in tool_calls],
        )
        yield ToolCallsStarted(
            calls=tuple(
                StartedToolCall(
                    call=call,
                    category=self._tool_kind(call.name),
                )
                for call in tool_calls
            ),
        )
        tool_names_by_id = {
            call.id: call.name or "tool" for call in tool_calls
        }

        tool_messages: list[ToolMessage] = []
        directives: list[TurnDirective] = []

        # The serial stream yields one completed result at a time; the
        # AFTER_TOOLS boundary below remains before the next model request.
        async for execution in self.tools.execute_each(
            tool_calls,
        ):
            message = execution.message
            directives.append(execution.directive)
            tool_messages.append(message)
            call_id = str(message.call.id)
            name = tool_names_by_id.get(call_id, "tool")
            self.messages.append(message)
            await self._publish_state_change()
            for client_event in execution.events:
                yield client_event
            yield ToolCompleted(execution=execution)

        self._log.info(
            "tool.batch.finished",
            turn=self.turn_count,
            count=len(tool_messages),
            names=[
                tool_names_by_id.get(str(message.call.id), "tool")
                for message in tool_messages
            ],
            statuses=[message.outcome.kind for message in tool_messages],
            result_chars=sum(
                len("".join(part.text for part in message.outcome.output.parts if isinstance(part, TextPart)))
                if isinstance(message.outcome, (ToolSucceeded, ToolFailed)) else 0
                for message in tool_messages
            ),
        )
        for message in tool_messages:
            await self._dispatch(
                Events.TOOL_MESSAGE_OBSERVED,
                ToolMessageObserved(message),
                short_circuit=False,
            )

        if any(
            isinstance(directive, CompleteTurnDirective)
            for directive in directives
        ):
            yield _ToolBatchResult(directive=CompleteTurnDirective())
            return

        yield _ToolBatchResult(directive=ContinueTurn())

    async def _start_turn(
        self,
        item: InboxItem,
    ) -> _TurnStartResult:
        accepted = await self._accept_user_message(
            item,
            new_turn=True,
        )
        if not accepted.proceed:
            return accepted
        user_input = accepted.user_input
        await self._dispatch(Events.TURN_START, TurnStarted(
            self.session, tuple(self.messages)
        ),
            short_circuit=False,
        )
        accepted.events.append(LoopTurnStarted(turn=self.turn_count))
        return accepted

    async def _start_claimed_turn(
        self,
        claimed: list[InboxItem],
    ) -> _TurnStartResult:
        """Start a turn from one atomic DSH-style boundary claim."""
        primary_index = next(
            (
                index
                for index, item in enumerate(claimed)
                if item.target is InboxTarget.NEXT_TURN
            ),
            len(claimed) - 1,
        )
        events: list[LoopEvent] = []
        for item in claimed[:primary_index]:
            accepted = await self._accept_user_message(
                item,
            )
            events.extend(accepted.events)
            if not accepted.proceed:
                await self.inbox.commit([
                    claimed_item.id for claimed_item in claimed
                ])
                return _TurnStartResult(item.input.content, events, False)
        primary = claimed[primary_index]
        started = await self._start_turn(primary)
        await self.inbox.commit([item.id for item in claimed])
        started.events = [*events, *started.events]
        return started

    @staticmethod
    def _user_message_rejected_event() -> LoopEvent:
        return LoopError(
            code="user_message_rejected",
            message="User message was rejected before entering history.",
        )

    async def _build_turn_context(self) -> _ContextBuildResult:
        build_request = ContextBuildRequest(
            history=tuple(self.messages),
            runtime_selection=self.state.metadata.value.runtime_selection,
            user_identity=self.user_identity,
            memory=self.memory,
            runtime_paths=self.state.variables,
            turn=self.turn_count,
            sandbox_summary="",
        )
        before_context_result = await self._dispatch(
            Events.BEFORE_CONTEXT_BUILD,
            BeforeContextBuild(build_request),
            short_circuit=True,
        )
        if isinstance(before_context_result, ReplaceContextRequest):
            build_request = before_context_result.request
        elif isinstance(before_context_result, HookCompleteTurn):
            if not is_loop_event(before_context_result.result):
                raise TypeError("CompleteTurn result must be a LoopEvent")
            return _ContextCompleted(event=before_context_result.result)
        elif before_context_result is not None and not isinstance(
            before_context_result, KeepContextRequest
        ):
            raise TypeError(
                "BeforeContextBuild must return KeepContextRequest, "
                "ReplaceContextRequest, CompleteTurn, or None"
            )

        await self._events.emit(CONTEXT_BUILD_INPUTS_READY, build_request)
        context_messages = await self._events.serial(BUILD_CONTEXT, build_request)
        if context_messages is None:
            raise RuntimeError(
                f"No context builder handled {BUILD_CONTEXT}"
            )
        if not isinstance(context_messages, tuple):
            raise TypeError(
                f"{BUILD_CONTEXT} must return a tuple of ProviderMessage values"
            )

        after_result = await self._dispatch(
            Events.AFTER_CONTEXT_BUILD,
            AfterContextBuild(tuple(context_messages)),
            short_circuit=True,
        )
        if isinstance(after_result, ReplaceContext):
            context_messages = list(after_result.context)
        elif isinstance(after_result, HookCompleteTurn):
            if not is_loop_event(after_result.result):
                raise TypeError("CompleteTurn result must be a LoopEvent")
            return _ContextCompleted(event=after_result.result)
        elif after_result is not None and not isinstance(after_result, KeepContext):
            raise TypeError(
                "AfterContextBuild must return KeepContext, ReplaceContext, "
                "CompleteTurn, or None"
            )

        return _ContextReady(messages=tuple(context_messages))

    async def _prepare_model_request(
        self,
        context_messages: list[ProviderMessage],
    ) -> _ModelRequestResult:
        tools = tuple(self.tools.enabled())
        schemas = tuple(
            ToolSchema(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in tools
        )
        model_request = ModelRequest(
            messages=tuple(context_messages),
            tools=schemas,
            selection=self.state.metadata.value.runtime_selection.model,
        )
        history_revision = self.state.history.surface_revision
        request_result = await self._dispatch(
            Events.BEFORE_MODEL_REQUEST,
            BeforeModelRequest(model_request),
            short_circuit=True,
        )
        if isinstance(request_result, ReplaceRequest):
            model_request = request_result.request
        elif isinstance(request_result, HookCompleteTurn):
            if not is_loop_event(request_result.result):
                raise TypeError("CompleteTurn result must be a LoopEvent")
            return _ModelRequestCompleted(event=request_result.result)
        elif request_result is not None and not isinstance(request_result, KeepRequest):
            raise TypeError(
                "BeforeModelRequest must return KeepRequest, ReplaceRequest, "
                "CompleteTurn, or None"
            )
        if self.state.history.surface_revision != history_revision:
            return _ModelRequestRebuild()
        await self._dispatch(
            Events.MODEL_REQUEST_READY,
            ModelRequestReady(model_request, self.session),
            short_circuit=False,
        )
        # MODEL_REQUEST_READY observers inspect the fully transformed request.
        # An observer may compact durable history; if so, this candidate is no
        # longer valid and the outer loop must rebuild it before invocation.
        if self.state.history.surface_revision != history_revision:
            return _ModelRequestRebuild()
        self._log.debug(
            "llm.tools.bound",
            names=[schema.name for schema in model_request.tools],
        )
        self._log.info(
            "llm.request.ready",
            provider=model_request.selection.route.provider,
            model=model_request.selection.route.model,
            messages=len(model_request.messages),
            tools=len(model_request.tools),
            estimated_input_tokens=estimate_request_tokens(
                model_request.messages,
                model_request.tools,
            ),
            context_window=model_request.selection.context_window,
            max_output_tokens=model_request.selection.generation.max_output_tokens,
        )
        return _ModelRequestReady(request=model_request)

    async def _finish_turn(self, stop_reason: str) -> LoopEvent:
        self._log.info(
            "turn.stop",
            turn=self.turn_count,
            reason=stop_reason,
        )
        turn_event = TurnEnded(self.session, tuple(self.messages), stop_reason)
        await self._dispatch(Events.TURN_END, turn_event,
            short_circuit=False,
        )
        try:
            await self._dispatch(Events.ON_STOP, turn_event,
                short_circuit=False,
            )
        except BaseException as exc:
            failure_ctx = LoopFailure(self.session, tuple(self.messages), exc)
            await self._dispatch(Events.ON_STOP_FAILURE, failure_ctx,
                short_circuit=False,
            )
            raise
        return LoopTurnEnded(
            turn=self.turn_count,
            outcome=TurnFinished(stop_reason=stop_reason),
        )

    def _iteration_limit_notice(self) -> str:
        return (
            f"Iteration limit: the tool iteration budget of "
            f"{self.max_iterations} has been exhausted. Do not call more "
            "tools. Give the human a concise status, clearly identify "
            "unfinished work, and state the next required action."
        )

    async def _stream_model_response(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[LoopEvent | _ModelResponseEvent]:
        """Stream canonical provider events and return the completed response."""
        aggregate: ModelResponse | None = None
        started = time.perf_counter()
        first_delta_at: float | None = None
        chunk_count = 0
        terminal_seen = False
        async for chunk in self.model_client.astream(request):
            chunk_count += 1
            if terminal_seen:
                raise RuntimeError(
                    "Model stream produced an event after its terminal event"
                )
            if isinstance(chunk, (TextDelta, ReasoningDelta, ToolCallDelta)):
                if first_delta_at is None:
                    first_delta_at = time.perf_counter()
                if isinstance(chunk, TextDelta):
                    yield AssistantTextDelta(text=chunk.text)
                elif isinstance(chunk, ReasoningDelta):
                    yield AssistantReasoningDelta(text=chunk.text)
                else:
                    yield ToolCallArgumentsDelta(
                        call_id=chunk.call_id,
                        name_delta=chunk.name_delta or "",
                        arguments_delta=chunk.arguments_delta,
                    )
                continue
            if isinstance(chunk, ModelCompleted):
                aggregate = chunk.response
                terminal_seen = True
                continue
            if isinstance(chunk, ModelFailed):
                raise ProviderFailure(chunk.error)
            if isinstance(chunk, ModelCancelled):
                raise asyncio.CancelledError(chunk.reason)
            raise TypeError(
                f"Unsupported model stream event: {type(chunk).__name__}"
            )
        if aggregate is None:
            raise RuntimeError("Model stream ended without a terminal event")
        finished = time.perf_counter()
        llm_ms = (finished - started) * 1000
        ttft_ms = (
            (first_delta_at - started) * 1000
            if first_delta_at is not None
            else None
        )
        timing = ModelTiming(
            total_ms=round(llm_ms, 3),
            first_delta_ms=round(ttft_ms, 3) if ttft_ms is not None else None,
        )
        usage = aggregate.usage.counters
        self._log.info(
            "llm.response",
            provider=request.selection.route.provider,
            model=request.selection.route.model,
            chunks=chunk_count,
            content_chars=sum(len(part.text) for part in aggregate.parts if isinstance(part, TextPart)),
            reasoning_chars=sum(
                len(part.text)
                for part in aggregate.parts
                if isinstance(part, ReasoningPart)
            ),
            tool_calls=sum(1 for part in aggregate.parts if isinstance(part, ToolCall)),
            input_tokens=usage.input,
            output_tokens=usage.output,
            stop_reason=aggregate.stop.kind,
            duration_ms=round(llm_ms, 3),
        )
        yield _ModelResponseEvent(aggregate, timing)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------



    async def _publish_state_change(self) -> None:
        """Announce a mutation of the loop-owned state projection."""
        await self._dispatch(Events.STATE_CHANGED, StateChanged(), short_circuit=False)

    def _close_interrupted_tool_calls(
        self,
        outcome: ToolOutcome,
    ) -> list[tuple[ToolMessage, str]]:
        """Close unanswered calls with the terminal outcome of their turn."""
        assistant_index = next(
            (
                index
                for index in range(len(self.messages) - 1, -1, -1)
                if isinstance(self.messages[index], AssistantMessage)
                and any(isinstance(part, ToolCall) for part in self.messages[index].parts)
            ),
            None,
        )
        if assistant_index is None:
            return []

        tail = self.messages[assistant_index + 1:]
        if any(not isinstance(message, ToolMessage) for message in tail):
            return []
        answered = {
            str(message.call.id) for message in tail
        }
        calls = tuple(
            part for part in self.messages[assistant_index].parts
            if isinstance(part, ToolCall)
        )
        missing = [
            call for call in calls if str(call.id) not in answered
        ]
        closed: list[tuple[ToolMessage, str]] = []
        for call in missing:
            message = ToolMessage(
                id=f"tool-{uuid.uuid4().hex}",
                call=ToolCallRef(id=call.id, name=call.name),
                outcome=outcome,
                timing=ToolTiming(duration_ms=0),
            )
            self.messages.append(message)
            closed.append((message, call.name))
        return closed

    async def _claim_step_inputs(self) -> bool:
        """Claim and accept every input addressed to the next loop step."""
        items = await self.inbox.claim_step()
        if not items:
            return False
        for item in items:
            await self._accept_user_message(item)
        await self.inbox.commit([item.id for item in items])
        return True

    async def _accept_user_message(
        self,
        item: InboxItem,
        *,
        new_turn: bool = False,
    ) -> _TurnStartResult:
        user_input = item.input.content
        accept_result = await self._dispatch(
            Events.ON_TURN_INPUT,
            OnTurnInput(item, tuple(self.messages)),
            short_circuit=True,
        )
        events: list[LoopEvent] = []
        if isinstance(accept_result, AcceptInput):
            accepted_input = accept_result.input
        elif isinstance(accept_result, RejectInput):
            events.append(LoopError(
                code="user_message_rejected",
                message=accept_result.error,
            ))
            return _TurnStartResult(user_input, events, False)
        elif isinstance(accept_result, HookCompleteTurn):
            if not is_loop_event(accept_result.result):
                raise TypeError("CompleteTurn result must be a LoopEvent")
            events.append(accept_result.result)
            return _TurnStartResult(user_input, events, False)
        elif accept_result is not None:
            raise TypeError(
                "OnTurnInput must return AcceptInput, RejectInput, "
                "CompleteTurn, or None"
            )
        else:
            accepted_input = item

        payload = accepted_input.input
        user_input = payload.content

        parts = (
            TextPart(text=user_input),
            *tuple(ImagePart(image=image) for image in payload.images),
        )
        if isinstance(payload, HumanInput):
            message = HumanInputMessage(
                id=f"message-{uuid.uuid4().hex}",
                input_id=accepted_input.id,
                parts=parts,
                artifacts=payload.artifacts,
            )
        elif isinstance(payload, RuntimeInput):
            message = RuntimeNoticeMessage(
                id=f"message-{uuid.uuid4().hex}",
                notice_id=f"notice-{uuid.uuid4().hex}",
                source=payload.source,
                event=payload.event,
                parts=parts,
                artifacts=payload.artifacts,
            )
        else:  # pragma: no cover - InputPayload is a closed discriminated union
            raise TypeError(f"Unsupported inbox payload: {payload!r}")

        accepted_event = InputAccepted(accepted_input, message)
        accepted_result = await self._dispatch(
            Events.INPUT_ACCEPTED,
            accepted_event,
            short_circuit=True,
        )
        if accepted_result is not None:
            if not isinstance(accepted_result, InputAccepted):
                raise TypeError("INPUT_ACCEPTED must return InputAccepted or None")
            if accepted_result.input != accepted_input:
                raise ValueError("INPUT_ACCEPTED cannot change the accepted input")
            replacement = accepted_result.message
            if type(replacement) is not type(message) or replacement.id != message.id:
                raise ValueError(
                    "INPUT_ACCEPTED may only replace a message while preserving "
                    "the accepted message type and identity"
                )
            if (
                isinstance(message, HumanInputMessage)
                and isinstance(replacement, HumanInputMessage)
                and replacement.input_id != accepted_input.id
            ):
                raise ValueError(
                    "INPUT_ACCEPTED cannot change accepted input identity"
                )
            message = replacement

        if new_turn:
            self.turn_count += 1
        self.messages.append(message)
        return _TurnStartResult(user_input, events, True)
