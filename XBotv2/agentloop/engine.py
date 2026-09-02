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
from collections.abc import AsyncIterator, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from XBotv2.agentloop.internal_messages import (
    DISPLAY_CONTENT_KEY,
    structure_tool_message,
)
from XBotv2.agentloop.inbox import AgentInbox, InboxInput, InboxTarget
from XBotv2.agentloop.protocol import agentloop_event
from XBotv2.agentloop.events import EventContext, EventPort, Events, SHORT_CIRCUIT_EVENTS
from XBotv2.agentloop.contracts import (
    DEFAULT_MAX_ITERATIONS,
    LoopSettings,
    LoopState,
    ModelRequest,
)
from XBotv2.agentloop.services import ToolsPort
from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import (
    ImageContent,
    Message,
    ModelChunk,
    ModelResponse,
    merge_model_chunk,
)
from XBotv2.core.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
from XBotv2.context_builder import (
    BEFORE_CONTEXT_BUILD,
    BUILD_CONTEXT,
    CONTEXT_BUILT,
    ContextBuildRequest,
    ContextBuilt,
)
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.tokens import (
    REQUEST_CONTEXT_WINDOW_KEY,
    REQUEST_ESTIMATE_KEY,
    REQUEST_PROVIDER_KEY,
    estimate_request_tokens,
)
from XBotv2.core.timing import TIMING_METADATA_KEY
from XBotv2.llm import ModelPort
from XBotv2.session import SessionInfo
from XBotv2.core.tools import (
    ClientEvent,
    Tool,
    ToolCall,
    ToolCallDelta,
    provider_tool_schema,
)

_UNCHANGED = object()


def _runtime_input(
    source: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retain display provenance without copying private inbox payloads."""
    if source == "user":
        return {}
    values = metadata or {}
    event = values.get("kind")
    if not isinstance(event, str) or not event:
        event = "continuation" if values.get("continuation") else "injected"
    return {"runtime_input": {"source": source, "event": event}}


@dataclass(slots=True)
class _TurnStartResult:
    user_input: str
    events: list[dict[str, Any]]
    proceed: bool


@dataclass(slots=True)
class _ContextBuildResult:
    messages: list[Message] | None = None
    event: dict[str, Any] | None = None
    turn_complete: bool | None = None


@dataclass(slots=True)
class _ModelRequestResult:
    request: ModelRequest | None = None
    event: dict[str, Any] | None = None
    turn_complete: bool | None = None
    rebuild: bool = False


@dataclass(slots=True)
class _ToolBatchResult:
    stop_loop: bool = False
    turn_complete: bool = False

def xbot_tool_call_deltas(
    chunk: ModelChunk,
    tool_stream_ids: dict[int, str],
) -> list[dict[str, Any]]:
    raw_chunks = chunk.tool_call_chunks or chunk.tool_calls
    deltas: list[dict[str, Any]] = []
    for index, tool_call in enumerate(raw_chunks):
        chunk_index = tool_call.index if isinstance(tool_call, ToolCallDelta) else index
        prior_id = tool_stream_ids.get(chunk_index)
        tool_id = tool_call.id or prior_id or f"tool_{chunk_index}"
        tool_stream_ids[chunk_index] = tool_id
        args_delta = tool_call.args
        delta = {
                "tool_call_id": tool_id,
                "id": tool_id,
                "name": tool_call.name or "tool",
                "args_delta": args_delta,
                "args": args_delta,
                "index": chunk_index,
        }
        if prior_id and prior_id != tool_id:
            delta["replaces_tool_call_id"] = prior_id
        deltas.append({"tool_calls": [delta]})
    return deltas


def tool_result_event_data(message: Message, name: str) -> dict[str, Any]:
    """Build the client-visible result without dropping structured metadata."""
    data: dict[str, Any] = {
        "tool_call_id": message.tool_call_id,
        "name": name,
        "content": message.additional_kwargs.get(
            DISPLAY_CONTENT_KEY,
            message.content,
        ),
        "status": message.status or "success",
    }
    if message.data is not None:
        data["data"] = message.data
    if message.error is not None:
        data["error"] = message.error
    if message.artifact:
        artifacts = (
            message.artifact
            if isinstance(message.artifact, (list, tuple))
            else [message.artifact]
        )
        data["artifacts"] = [_artifact_event_data(artifact) for artifact in artifacts]
    if message.images:
        data["images"] = [image.model_dump(mode="json") for image in message.images]
    timing = message.response_metadata.get(TIMING_METADATA_KEY)
    if isinstance(timing, dict):
        data["timing"] = timing
    return data


def _artifact_event_data(artifact: object) -> dict[str, Any]:
    if isinstance(artifact, ArtifactRef):
        return artifact.model_dump(mode="json")
    if isinstance(artifact, Mapping):
        return dict(artifact)
    raise TypeError(f"Unsupported artifact reference: {type(artifact).__name__}")


class Engine:
    """Core ReAct loop engine.

    No plugin imports. No DAG, skills, or compaction logic.
    All extension behavior comes through runtime events and the tool registry.

    Usage::

        services = await start_application(...)
        engine = services.engine
        async for event in engine.run_turn("list files"):
            print(event)
    """

    def __init__(
        self,
        *,
        model_client: ModelPort,
        tools: ToolsPort,
        events: EventPort,
        state: LoopState,
        settings: LoopSettings,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self.model_client = model_client
        self.tools = tools
        self._events = events
        self.state = state
        self.settings = settings
        self.max_iterations = max_iterations
        self._log = runtime_log.bind("engine")
        self.inbox = AgentInbox(
            items=state.inbox_items,
            sink=state.inbox_sink,
            record_splice=self._record_inbox_splice,
        )
        self.continuation: bool = False
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
    def session(self) -> SessionInfo:
        return self.state.session

    @property
    def context_window(self) -> int:
        return self.settings.context_window

    async def _dispatch(
        self,
        event: str,
        payload: EventContext,
        *,
        short_circuit: bool | None = None,
    ) -> Any:
        """Dispatch one runtime event on the plugin context.

        Short-circuit events use ``ctx.serial`` (first non-``None`` result is
        interpreted by the caller); observer events use ``ctx.emit``.
        """
        if short_circuit is None:
            short_circuit = event in SHORT_CIRCUIT_EVENTS
        if short_circuit:
            result = await self._events.serial(event, payload)
            if result is not None and not isinstance(result, dict):
                raise TypeError(
                    f"Short-circuit hook {event} must return a dict, "
                    f"got {type(result).__name__}"
                )
            return result
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
        ctx = self._make_event_context()
        await self._dispatch(Events.SESSION_START, ctx, short_circuit=False)

    async def _resume_loaded_state(self) -> None:
        self._close_interrupted_tool_calls("session_restarted")
        self.session.turn_count = self.turn_count
        ctx = self._make_event_context()
        await self._dispatch(Events.SESSION_RESUME, ctx, short_circuit=False)
        await self._publish_state_change()

    async def close_session(self) -> None:
        """Dispatch the loop lifecycle close boundary."""
        ctx = self._make_event_context()
        await self._dispatch(Events.SESSION_CLOSE, ctx, short_circuit=False)
        await self._publish_state_change()

    async def _prepare_tool_calls(
        self,
        tool_calls: list[ToolCall],
        *,
        agent_response: ModelResponse | None = None,
    ) -> bool:
        before_ctx = self._make_event_context(tool_calls=tool_calls,
            agent_response=agent_response,
        )
        before_result = await self._dispatch(Events.BEFORE_TOOLS, before_ctx,
            short_circuit=True,
        )
        if before_result is not None:
            return False

        parsed_ctx = self._make_event_context(tool_calls=tool_calls,
            agent_response=agent_response,
        )
        await self._dispatch(Events.TOOL_CALLS_PARSED, parsed_ctx,
            short_circuit=False,
        )
        return True

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
    ) -> list[Message]:
        results = await self.tools.execute_all(
            tool_calls,
            context_factory=self._make_event_context,
        )
        after_ctx = self._make_event_context(tool_results=results)
        after_result = await self._dispatch(
            Events.AFTER_TOOLS,
            after_ctx,
            short_circuit=True,
        )
        if isinstance(after_result, dict) and "tool_results" in after_result:
            results = list(after_result["tool_results"])
        return results

    async def _record_inbox_splice(self, event: dict[str, Any]) -> None:
        """Publish an inbox mutation before its live projection changes."""
        await self._dispatch(
            Events.INBOX_SPLICE,
            self._make_event_context(client_event=ClientEvent(**event)),
            short_circuit=False,
        )

    def set_wake_driver(self, wake_driver: Callable[[], None] | None) -> None:
        self.inbox.set_wake_driver(wake_driver)

    async def followup(self, content: str, **kwargs: Any) -> InboxInput:
        return await self.inbox.followup(content, **kwargs)

    async def steer(self, content: str, **kwargs: Any) -> InboxInput:
        return await self.inbox.steer(content, **kwargs)

    async def inject(self, content: str, **kwargs: Any) -> InboxInput:
        return await self.inbox.inject(content, **kwargs)

    @property
    def pending_input_count(self) -> int:
        return len(self.inbox)

    @property
    def pending_inputs(self) -> tuple[InboxInput, ...]:
        return tuple(self.inbox.pending)

    async def edit_input(self, message_id: str, content: str) -> InboxInput:
        return await self.inbox.edit(message_id, content)

    async def remove_input(self, message_id: str) -> InboxInput:
        return await self.inbox.remove(message_id)

    async def retarget_input(
        self,
        message_id: str,
        target: InboxTarget,
    ) -> InboxInput:
        return await self.inbox.retarget(message_id, target)

    async def discard_inputs(self) -> None:
        await self.inbox.discard()

    def configure(
        self,
        *,
        model_client: Any = _UNCHANGED,
        max_iterations: int | None = None,
        **settings: Any,
    ) -> None:
        """Replace loop-owned model/settings without acquiring plugin state."""
        if model_client is not _UNCHANGED:
            self.model_client = model_client
        if settings:
            self.settings = replace(self.settings, **settings)
        if max_iterations is not None:
            self.max_iterations = max_iterations

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        user_input: str,
        *,
        request_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        await self.inbox.send(
            user_input,
            target=InboxTarget.NEXT_TURN,
            wakeup=False,
            source="user",
            message_id=request_id,
            images=images,
            artifacts=artifacts,
        )
        initial_claim = True
        async for event in self.run_pending(request_id=request_id):
            if initial_claim and event.get("type") == "_inbox_claimed":
                initial_claim = False
                continue
            initial_claim = False
            yield event

    async def run_pending(
        self,
        *,
        request_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
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
        self.continuation = any(
            bool(item.metadata.get("continuation")) for item in claimed
        )
        turn_started_at = time.perf_counter()
        outcome = "completed"
        self._log.info(
            "turn.start",
            inputs=len(claimed),
            input_chars=sum(len(item.content) for item in claimed),
            continuation=self.continuation,
        )
        try:
            async for event in self._run_turn_impl(
                claimed,
            ):
                if event.get("type") == "turn_started":
                    turn_started = True
                elif event.get("type") in {"turn_finished", "turn_cancelled"}:
                    turn_ended = True
                yield event
        except asyncio.CancelledError:
            outcome = "cancelled"
            self._log.info("turn.interrupted", turn=self.turn_count)
            self._close_interrupted_tool_calls("client_interrupt")
            if not turn_ended:
                turn_ctx = self._make_event_context(stop_reason="client_interrupt",
                )
                await self._dispatch(Events.TURN_END, turn_ctx,
                    short_circuit=False,
                )
            yield agentloop_event(
                "turn_cancelled",
                {
                    "turn": self.turn_count,
                    "reason": "client_interrupt",
                },
            )
            raise
        except BaseException as exc:
            outcome = "error"
            self._log.exception(
                "turn.failed",
                turn=self.turn_count,
                error_type=type(exc).__name__,
            )
            current_input = next(
                (
                    item.content
                    for item in claimed
                    if item.target is InboxTarget.NEXT_TURN
                ),
                "",
            )
            failure_ctx = self._make_event_context(
                stop_reason="error",
                error=exc,
                user_input=current_input,
            )
            await self._dispatch(Events.ON_STOP_FAILURE, failure_ctx, short_circuit=False)
            ctx = self._make_event_context(
                error=exc,
                user_input=current_input,
            )
            await self._dispatch(Events.ON_ERROR, ctx, short_circuit=False)
            yield agentloop_event(
                "error",
                {
                    "code": "engine_error",
                    "message": str(exc),
                    "details": {"exception_type": type(exc).__name__},
                },
            )
            if turn_started:
                yield agentloop_event(
                    "turn_finished",
                    {"turn": self.turn_count},
                )
        finally:
            try:
                await self.inbox.reconcile(
                    [item.message_id for item in claimed],
                    {
                        message.input_id
                        for message in self.messages
                        if message.input_id
                    },
                )
                await self._publish_state_change()
            except Exception:
                outcome = "state_error"
                raise
            finally:
                self.continuation = False
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
        claimed: list[InboxInput],
    ) -> AsyncIterator[dict[str, Any]]:
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

        while not turn_complete:
            finalizing = iteration >= self.max_iterations
            if finalizing:
                if iteration_limit_reached:
                    break
                iteration_limit_reached = True
            else:
                iteration += 1

            while True:
                context_build = await self._build_turn_context()
                if context_build.event is not None:
                    yield context_build.event
                if context_build.turn_complete is not None:
                    turn_complete = context_build.turn_complete
                    break
                assert context_build.messages is not None
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
                        Message(
                            role="system",
                            content=self._iteration_limit_notice(),
                        ),
                    )
                model_preparation = await self._prepare_model_request(
                    context_messages
                )
                if model_preparation.event is not None:
                    yield model_preparation.event
                if model_preparation.turn_complete is not None:
                    turn_complete = model_preparation.turn_complete
                    break
                if model_preparation.rebuild:
                    continue
                assert model_preparation.request is not None
                model_request = model_preparation.request
                if finalizing:
                    model_request.tools = []
                    model_request.llm = self._llm_without_tools()
                context_messages = model_request.messages
                llm_with_tools = model_request.llm
                break
            if turn_complete:
                break
            try:
                response = None
                async for model_event in self._stream_model_response(
                    llm_with_tools,
                    context_messages,
                ):
                    if model_event.get("type") == "_model_response":
                        response = model_event["data"]["response"]
                    else:
                        yield model_event
                if response is None:
                    raise RuntimeError("LLM stream completed without a response")
            except asyncio.TimeoutError as exc:
                self._log.error("llm.request.timeout", turn=self.turn_count)
                err_ctx = self._make_event_context(
                    context_messages=context_messages,
                    model_request=model_request,
                    error=exc,
                )
                await self._dispatch(
                    Events.MODEL_REQUEST_ERROR,
                    err_ctx,
                    short_circuit=False,
                )
                raise asyncio.TimeoutError("LLM call timed out") from None
            except Exception as exc:
                self._log.exception(
                    "llm.request.error",
                    provider=self.settings.provider,
                    model=self.settings.model,
                    error_type=type(exc).__name__,
                )
                err_ctx = self._make_event_context(context_messages=context_messages,
                    model_request=model_request,
                    error=exc,
                )
                await self._dispatch(Events.MODEL_REQUEST_ERROR, err_ctx,
                    short_circuit=False,
                )
                raise
            content = response.content
            if finalizing and response.tool_calls:
                names = ", ".join(call.name for call in response.tool_calls)
                raise RuntimeError(
                    "LLM requested tools after the iteration budget was "
                    f"exhausted: {names}"
                )
            if not str(content).strip() and not response.tool_calls:
                reasoning = response.reasoning
                after_tool = bool(
                    self.messages and self.messages[-1].role == "tool"
                )
                stop_reason = response.response_metadata.get(
                    "stop_reason", "unknown"
                )
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
            response_metadata = dict(response.response_metadata)
            response_metadata[REQUEST_ESTIMATE_KEY] = estimate_request_tokens(
                model_request.messages,
                model_request.tools,
            )
            response_metadata[REQUEST_CONTEXT_WINDOW_KEY] = self.settings.context_window
            response_metadata[REQUEST_PROVIDER_KEY] = (
                self.session.provider
            )
            response_msg = Message(
                role="assistant",
                parts=response.parts,
                usage_metadata=response.usage_metadata,
                response_metadata=response_metadata,
                additional_kwargs=response.additional_kwargs,
            )
            response_history = [*self.messages, response_msg]
            yield agentloop_event(
                "assistant_message",
                {
                    "content": content,
                    "tool_calls": [
                        call.model_dump(mode="json") for call in response.tool_calls
                    ],
                    "timing": response_metadata.get(TIMING_METADATA_KEY),
                },
            )
            if response_msg.usage_metadata:
                yield agentloop_event("usage", response_msg.usage_metadata)

            # ON_ASSISTANT_MESSAGE hook
            am_ctx = self._make_event_context(
                history_messages=response_history,
                agent_response=response,
            )
            await self._dispatch(Events.ASSISTANT_MESSAGE, am_ctx, short_circuit=False)

            response_ctx = self._make_event_context(
                history_messages=response_history,
                context_messages=context_messages,
                agent_response=response,
                model_request=model_request,
                model_response=response,
            )
            await self._dispatch(Events.AFTER_MODEL_RESPONSE, response_ctx,
                short_circuit=False,
            )

            # AFTER_AGENT hook
            aa_ctx = self._make_event_context(
                history_messages=response_history,
                agent_response=response,
            )
            agent_result = await self._dispatch(Events.AFTER_AGENT, aa_ctx, short_circuit=True
            )
            self.messages.append(response_msg)
            if agent_result is not None:
                if isinstance(agent_result, dict):
                    if "messages" in agent_result:
                        self.messages.extend(agent_result["messages"])
                    if "event" in agent_result:
                        yield agent_result["event"]
                    turn_complete = bool(agent_result.get("turn_complete", True))
                else:
                    turn_complete = True
                if turn_complete:
                    claimed_event = await self._claim_step_inputs()
                    if claimed_event is not None:
                        yield claimed_event
                        continue
                    break

            # Check for tool calls
            tool_calls = response.tool_calls
            if not tool_calls:
                # A complete response: fold any pending input so it is
                # answered in this same turn instead of waiting for a later
                # one. This is the no-tool-boundary path.
                claimed_event = await self._claim_step_inputs()
                if claimed_event is not None:
                    yield claimed_event
                    continue
                turn_complete = True
                break

            batch_result = None
            async for tool_event in self._run_tool_batch(response):
                if tool_event.get("type") == "_tool_batch_result":
                    batch_result = tool_event["data"]["result"]
                else:
                    yield tool_event
            if batch_result is None:
                raise RuntimeError("Tool batch completed without an outcome")
            if batch_result.stop_loop:
                turn_complete = batch_result.turn_complete
                break
            claimed_event = await self._claim_step_inputs()
            if claimed_event is not None:
                yield claimed_event

        stop_reason = (
            "max_iterations" if iteration_limit_reached else "completed"
        )
        yield await self._finish_turn(stop_reason)

    async def _run_tool_batch(
        self,
        response: ModelResponse,
    ) -> AsyncIterator[dict[str, Any]]:
        tool_calls = list(response.tool_calls)
        if not await self._prepare_tool_calls(
            tool_calls,
            agent_response=response,
        ):
            yield self._tool_batch_result_event(
                _ToolBatchResult(stop_loop=True)
            )
            return

        self._log.info(
            "tool.batch.started",
            turn=self.turn_count,
            count=len(tool_calls),
            names=[call.name for call in tool_calls],
        )
        yield agentloop_event(
            "tool_calls_started",
            {
                "tool_calls": [
                    call.model_dump(mode="json") for call in tool_calls
                ]
            },
        )
        tool_names_by_id = {
            call.id: call.name or "tool" for call in tool_calls
        }

        tool_messages = await self._execute_tool_calls(tool_calls)
        tool_event_payloads = [
            tool_result_event_data(
                message,
                tool_names_by_id.get(str(message.tool_call_id), "tool"),
            )
            for message in tool_messages
        ]
        for message in tool_messages:
            structure_tool_message(
                message,
                tool_names_by_id.get(str(message.tool_call_id), "tool"),
            )

        self._log.info(
            "tool.batch.finished",
            turn=self.turn_count,
            count=len(tool_messages),
            names=[
                tool_names_by_id.get(str(message.tool_call_id), "tool")
                for message in tool_messages
            ],
            statuses=[message.status for message in tool_messages],
            result_chars=sum(len(str(message.content)) for message in tool_messages),
        )
        self.messages.extend(tool_messages)
        # Announce committed state before exposing results or requesting the
        # next model step. Persistence, UIs, and other observers decide how
        # to project that state.
        await self._publish_state_change()

        for message, event_payload in zip(
            tool_messages,
            tool_event_payloads,
            strict=True,
        ):
            client_events = message.client_events
            for client_event in client_events:
                event_ctx = self._make_event_context(tool_result=message,
                    client_event=ClientEvent(**client_event),
                )
                await self._dispatch(Events.CLIENT_EVENT, event_ctx,
                    short_circuit=False,
                )
                yield client_event
            yield agentloop_event("tool_result", event_payload)

        for message in tool_messages:
            message_ctx = self._make_event_context(tool_results=[message],
            )
            await self._dispatch(Events.TOOL_MESSAGE, message_ctx,
                short_circuit=False,
            )

        if any(message.turn_complete for message in tool_messages):
            yield self._tool_batch_result_event(
                _ToolBatchResult(stop_loop=True, turn_complete=True)
            )
            return

        yield self._tool_batch_result_event(_ToolBatchResult())

    @staticmethod
    def _tool_batch_result_event(result: _ToolBatchResult) -> dict[str, Any]:
        return {"type": "_tool_batch_result", "data": {"result": result}}

    async def _start_turn(
        self,
        user_input: str,
        *,
        input_kind: str = "user_message",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        input_id: str = "",
        source: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> _TurnStartResult:
        accepted = await self._accept_user_message(
            user_input,
            images=images,
            artifacts=artifacts,
            input_id=input_id,
            source=source,
            metadata=metadata,
            new_turn=True,
        )
        if not accepted.proceed:
            return accepted
        user_input = accepted.user_input
        turn_ctx = self._make_event_context(user_input=user_input,
        )
        await self._dispatch(Events.TURN_START, turn_ctx,
            short_circuit=False,
        )
        if (
            turn_ctx.user_input is not None
            and str(turn_ctx.user_input) != user_input
            and self.messages
            and self.messages[-1].role == "user"
        ):
            # A hook replaced the user input (e.g. the goal plugin injects the
            # active goal context on a continuation turn); reflect it in the
            # retained message so the model sees it on the next step.
            previous = self.messages[-1]
            self.state.history.replace_last(Message(
                role="user",
                content=str(turn_ctx.user_input),
                input_id=previous.input_id,
                images=previous.images,
                artifact=previous.artifact,
                additional_kwargs=previous.additional_kwargs,
            ))
        accepted.events.append(agentloop_event(
            "turn_started",
            {"turn": self.turn_count},
        ))
        return accepted

    async def _start_claimed_turn(
        self,
        claimed: list[InboxInput],
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
        events: list[dict[str, Any]] = []
        for item in claimed[:primary_index]:
            accepted = await self._accept_user_message(
                item.content,
                images=item.images,
                artifacts=item.artifacts,
                input_id=item.message_id,
                source=item.source,
                metadata=item.metadata,
            )
            events.extend(accepted.events)
            if not accepted.proceed:
                await self.inbox.commit([
                    claimed_item.message_id for claimed_item in claimed
                ])
                return _TurnStartResult(item.content, events, False)
        primary = claimed[primary_index]
        started = await self._start_turn(
            primary.content,
            images=primary.images,
            artifacts=primary.artifacts,
            input_id=primary.message_id,
            source=primary.source,
            metadata=primary.metadata,
        )
        await self.inbox.commit([item.message_id for item in claimed])
        started.events = [
            {
                "type": "_inbox_claimed",
                "data": {"message_ids": [item.message_id for item in claimed]},
            },
            *events,
            *started.events,
        ]
        return started

    @staticmethod
    def _user_message_rejected_event() -> dict[str, Any]:
        return agentloop_event(
            "error",
            {
                "code": "user_message_rejected",
                "message": "User message was rejected before entering history.",
            },
        )

    async def _build_turn_context(self) -> _ContextBuildResult:
        before_ctx = self._make_event_context()
        before_context_result = await self._dispatch(Events.BEFORE_CONTEXT, before_ctx,
            short_circuit=True,
        )
        if isinstance(before_context_result, dict):
            if "event" in before_context_result:
                return _ContextBuildResult(
                    event=before_context_result["event"],
                    turn_complete=bool(before_context_result.get("turn_complete", True)),
                )
        elif before_context_result is not None:
            return _ContextBuildResult(
                event=self._default_hook_rejection_event(Events.BEFORE_CONTEXT),
                turn_complete=True,
            )

        build_request = ContextBuildRequest(
            messages=list(self.messages),
            session=self.session,
            agent_name=self.settings.agent_name,
            agent_role=self.settings.agent_role,
            user_name=self.settings.user_name,
            user_id=self.settings.user_id,
            developer_instructions=self.settings.developer_instructions,
            instructions=self.settings.agent_instructions,
            memory=self.settings.memory,
            runtime_paths={
                "workspace": self.settings.workspace,
                "session": "session/ (read-only)",
                "artifacts": "session/artifacts/ (read-only)",
                "tool_results": "session/artifacts/tool_results/ (read-only)",
            },
            turn_count=self.turn_count,
        )
        build_result = await self._events.serial(
            BEFORE_CONTEXT_BUILD,
            build_request,
        )
        if build_result is not None:
            raise TypeError(
                f"{BEFORE_CONTEXT_BUILD} listeners must mutate "
                "ContextBuildRequest and return None"
            )

        await self._events.emit(BUILD_CONTEXT, build_request)
        context_messages = build_request.context_messages
        if context_messages is None:
            raise RuntimeError(
                f"No context builder handled {BUILD_CONTEXT}"
            )

        after_ctx = self._make_event_context(context_messages=context_messages,
        )
        after_result = await self._dispatch(Events.AFTER_CONTEXT, after_ctx,
            short_circuit=True,
        )
        if isinstance(after_result, dict):
            if "context_messages" in after_result:
                context_messages = after_result["context_messages"]
            elif "messages" in after_result:
                context_messages = after_result["messages"]
            if "event" in after_result:
                return _ContextBuildResult(
                    event=after_result["event"],
                    turn_complete=bool(after_result.get("turn_complete", True)),
                )
        elif after_result is not None:
            return _ContextBuildResult(
                event=self._default_hook_rejection_event(Events.AFTER_CONTEXT),
                turn_complete=True,
            )

        await self._events.emit(
            CONTEXT_BUILT,
            ContextBuilt(tuple(context_messages), self.session),
        )
        return _ContextBuildResult(messages=context_messages)

    async def _prepare_model_request(
        self,
        context_messages: list[Message],
    ) -> _ModelRequestResult:
        before_agent_ctx = self._make_event_context()
        before_agent = await self._dispatch(Events.BEFORE_AGENT, before_agent_ctx,
            short_circuit=True,
        )
        if before_agent is not None:
            if isinstance(before_agent, dict) and "messages" in before_agent:
                self.messages.extend(before_agent["messages"])
            return _ModelRequestResult(turn_complete=True)

        tools = self.tools.enabled()
        pre_schema_request = ModelRequest(
            messages=context_messages,
            tools=list(tools),
            llm=self.model_client,
        )
        pre_schema_ctx = self._make_event_context(context_messages=context_messages,
            model_request=pre_schema_request,
        )
        pre_schema_result = await self._dispatch(Events.BEFORE_TOOL_SCHEMA_BIND, pre_schema_ctx,
            short_circuit=True,
        )
        if isinstance(pre_schema_result, dict):
            if "tools" in pre_schema_result:
                tools = list(pre_schema_result["tools"])
                pre_schema_request.tools = tools
            if "messages" in pre_schema_result:
                context_messages = list(pre_schema_result["messages"])
                pre_schema_request.messages = context_messages
            if "event" in pre_schema_result:
                return _ModelRequestResult(
                    event=pre_schema_result["event"],
                    turn_complete=bool(
                        pre_schema_result.get("turn_complete", True)
                    ),
                )
        elif pre_schema_result is not None:
            return _ModelRequestResult(
                event=self._default_hook_rejection_event(Events.BEFORE_TOOL_SCHEMA_BIND),
                turn_complete=True,
            )
        pre_schema_request = pre_schema_ctx.model_request or pre_schema_request
        tools = list(pre_schema_request.tools)
        context_messages = list(pre_schema_request.messages)

        model_request = ModelRequest(
            messages=context_messages,
            tools=list(tools),
            llm=self._bind_tools_for_provider(list(tools)),
        )
        schema_ctx = self._make_event_context(context_messages=context_messages,
            model_request=model_request,
        )
        await self._dispatch(Events.AFTER_TOOL_SCHEMA_BIND, schema_ctx,
            short_circuit=False,
        )

        request_ctx = self._make_event_context(
            context_messages=context_messages,
            model_request=ModelRequest(
                messages=list(model_request.messages),
                tools=list(model_request.tools),
                llm=model_request.llm,
            ),
        )
        model_request = request_ctx.model_request
        assert model_request is not None
        tools_before_hook = tuple(model_request.tools)
        llm_before_hook = model_request.llm
        request_result = await self._dispatch(Events.BEFORE_MODEL_REQUEST, request_ctx,
            short_circuit=True,
        )
        if isinstance(request_result, dict):
            if request_result.get("rebuild"):
                return _ModelRequestResult(rebuild=True)
            if "messages" in request_result:
                model_request.messages = list(request_result["messages"])
            if "tools" in request_result:
                model_request.tools = list(request_result["tools"])
                model_request.llm = self._bind_tools_for_provider(
                    model_request.tools
                )
            if "llm" in request_result:
                model_request.llm = request_result["llm"]
            if "event" in request_result:
                return _ModelRequestResult(
                    event=request_result["event"],
                    turn_complete=bool(request_result.get("turn_complete", True)),
                )
        elif request_result is not None:
            return _ModelRequestResult(
                event=self._default_hook_rejection_event(Events.BEFORE_MODEL_REQUEST),
                turn_complete=True,
            )
        if (
            tuple(model_request.tools) != tools_before_hook
            and model_request.llm is llm_before_hook
        ):
            model_request.llm = self._bind_tools_for_provider(
                model_request.tools
            )
        ready_ctx = self._make_event_context(
            context_messages=context_messages,
            model_request=model_request,
        )
        await self._dispatch(
            Events.MODEL_REQUEST_READY,
            ready_ctx,
            short_circuit=False,
        )
        model_request = ready_ctx.model_request or model_request
        self._log.debug(
            "llm.tools.bound",
            names=[tool.name for tool in model_request.tools],
        )
        self._log.info(
            "llm.request.ready",
            provider=self.settings.provider,
            model=self.settings.model,
            messages=len(model_request.messages),
            tools=len(model_request.tools),
            estimated_input_tokens=estimate_request_tokens(
                model_request.messages,
                model_request.tools,
            ),
            context_window=self.settings.context_window,
            max_output_tokens=self.settings.max_output_tokens,
        )
        return _ModelRequestResult(request=model_request)

    async def _finish_turn(self, stop_reason: str) -> dict[str, Any]:
        self._log.info(
            "turn.stop",
            turn=self.turn_count,
            reason=stop_reason,
        )
        turn_ctx = self._make_event_context(stop_reason=stop_reason,
        )
        await self._dispatch(Events.TURN_END, turn_ctx,
            short_circuit=False,
        )
        stop_ctx = self._make_event_context(stop_reason=stop_reason,
        )
        try:
            await self._dispatch(Events.ON_STOP, stop_ctx,
                short_circuit=False,
            )
        except BaseException as exc:
            failure_ctx = self._make_event_context(stop_reason=stop_reason,
                error=exc,
            )
            await self._dispatch(Events.ON_STOP_FAILURE, failure_ctx,
                short_circuit=False,
            )
            raise
        return agentloop_event(
            "turn_finished",
            {"turn": self.turn_count},
        )

    def _bind_tools_for_provider(self, tools: list[Tool]) -> ModelPort:
        if not tools:
            return self.model_client
        return self.model_client.bind_tools([
            provider_tool_schema(tool) for tool in tools
        ])

    def _llm_without_tools(self) -> ModelPort:
        return self.model_client.bind_tools([])

    def _iteration_limit_notice(self) -> str:
        return (
            f"Iteration limit: the tool iteration budget of "
            f"{self.max_iterations} has been exhausted. Do not call more "
            "tools. Give the human a concise status, clearly identify "
            "unfinished work, and state the next required action."
        )

    async def _stream_model_response(
        self,
        llm: ModelPort,
        context_messages: list[Message],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream provider chunks and reconstruct the final response."""
        aggregate: ModelResponse | None = None
        tool_stream_ids: dict[int, str] = {}
        started = time.perf_counter()
        first_delta_at: float | None = None
        chunk_count = 0
        async for chunk in llm.astream(context_messages):
            chunk_count += 1
            if isinstance(chunk, ModelChunk):
                if first_delta_at is None and (
                    chunk.content or chunk.reasoning or chunk.tool_calls
                ):
                    first_delta_at = time.perf_counter()
                aggregate = merge_model_chunk(aggregate, chunk)
                if chunk.content:
                    yield agentloop_event(
                        "assistant_message_delta",
                        {"content": chunk.content},
                    )
                if chunk.reasoning:
                    yield agentloop_event(
                        "assistant_message_delta",
                        {"reasoning": chunk.reasoning},
                    )
                for tool_delta in xbot_tool_call_deltas(
                    chunk, tool_stream_ids
                ):
                    yield agentloop_event("tool_call_delta", tool_delta)
                continue
            if isinstance(chunk, ModelResponse):
                aggregate = chunk
                continue
            self._log.warning(
                "llm.response.unexpected_chunk",
                chunk_type=type(chunk).__name__,
            )
        if aggregate is None:
            raise RuntimeError("LLM stream produced no chunks")
        finished = time.perf_counter()
        llm_ms = (finished - started) * 1000
        ttft_ms = (
            (first_delta_at - started) * 1000
            if first_delta_at is not None
            else None
        )
        timing: dict[str, float] = {"llm_ms": round(llm_ms, 3)}
        if ttft_ms is not None:
            timing["ttft_ms"] = round(ttft_ms, 3)
            timing["decode_ms"] = round(max(0.0, llm_ms - ttft_ms), 3)
        aggregate.response_metadata[TIMING_METADATA_KEY] = timing
        usage = aggregate.usage_metadata
        self._log.info(
            "llm.response",
            provider=self.settings.provider,
            model=self.settings.model,
            chunks=chunk_count,
            content_chars=len(aggregate.content),
            reasoning_chars=len(aggregate.reasoning),
            tool_calls=len(aggregate.tool_calls),
            input_tokens=usage.get("input_tokens", "unknown"),
            output_tokens=usage.get("output_tokens", "unknown"),
            total_tokens=usage.get("total_tokens", "unknown"),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            stop_reason=aggregate.response_metadata.get("stop_reason", "unknown"),
            duration_ms=round(llm_ms, 3),
        )
        yield {"type": "_model_response", "data": {"response": aggregate}}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------



    async def _publish_state_change(self) -> None:
        """Announce a mutation of the loop-owned state projection."""
        event = self._make_event_context()
        await self._dispatch(Events.STATE_CHANGED, event, short_circuit=False)

    def _close_interrupted_tool_calls(self, reason: str) -> None:
        """Append error results for an interrupted trailing tool batch."""
        assistant_index = next(
            (
                index
                for index in range(len(self.messages) - 1, -1, -1)
                if self.messages[index].role == "assistant"
                and self.messages[index].tool_calls
            ),
            None,
        )
        if assistant_index is None:
            return

        tail = self.messages[assistant_index + 1:]
        if any(message.role != "tool" for message in tail):
            return
        answered = {
            message.tool_call_id for message in tail if message.tool_call_id
        }
        missing = [
            call for call in self.messages[assistant_index].tool_calls
            if call.id not in answered
        ]
        for call in missing:
            message = Message(
                role="tool",
                content=f"Tool call did not complete: {reason}.",
                tool_call_id=call.id,
                status="error",
            )
            structure_tool_message(message, call.name)
            self.messages.append(message)

    def _default_hook_rejection_event(event: str) -> dict[str, Any]:
        return agentloop_event(
            "error",
            {
                "code": "hook_short_circuit_rejected",
                "message": f"Hook {event} short-circuited without a structured result.",
                "stage": event,
            },
        )

    def _make_event_context(
        self,
        *,
        history_messages: list[Message] | None = None,
        user_input: str | None = None,
        context_messages: list[Message] | None = None,
        agent_response: ModelResponse | None = None,
        model_request: ModelRequest | None = None,
        model_response: ModelResponse | None = None,
        tool_calls: list[ToolCall] | None = None,
        tool_call: ToolCall | None = None,
        tool_results: list[Message] | None = None,
        tool_result: Message | None = None,
        stop_reason: str | None = None,
        client_event: ClientEvent | None = None,
        error: BaseException | None = None,
    ) -> EventContext:
        return EventContext(
            request_id=self._request_id.get(),
            messages=(
                history_messages
                if history_messages is not None
                else self.messages
            ),
            settings=self.settings,
            continuation=self.continuation,
            session=self.session,
            user_input=user_input,
            context_messages=context_messages,
            agent_response=agent_response,
            model_request=model_request,
            model_response=model_response,
            tool_calls=tool_calls,
            tool_call=tool_call,
            tool_results=tool_results,
            tool_result=tool_result,
            stop_reason=stop_reason,
            client_event=client_event,
            error=error,
        )

    async def _claim_step_inputs(self) -> dict[str, Any] | None:
        """Claim and accept every input addressed to the next loop step."""
        items = await self.inbox.claim_step()
        if not items:
            return None
        accepted_ids: list[str] = []
        for item in items:
            accepted = await self._accept_user_message(
                item.content,
                images=item.images,
                artifacts=item.artifacts,
                input_id=item.message_id,
                source=item.source,
                metadata=item.metadata,
            )
            if accepted.proceed:
                accepted_ids.append(item.message_id)
        await self.inbox.commit([item.message_id for item in items])
        return {
            "type": "_inbox_claimed",
            "data": {"message_ids": accepted_ids},
        }

    async def _accept_user_message(
        self,
        user_input: str,
        *,
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        input_id: str = "",
        source: str = "user",
        metadata: dict[str, Any] | None = None,
        new_turn: bool = False,
    ) -> _TurnStartResult:
        accept_ctx = self._make_event_context(user_input=user_input,
        )
        accept_result = await self._dispatch(Events.BEFORE_USER_MESSAGE_ACCEPT, accept_ctx,
            short_circuit=True,
        )
        events: list[dict[str, Any]] = []
        if isinstance(accept_result, dict):
            if "user_input" in accept_result:
                user_input = str(accept_result["user_input"])
            if "event" in accept_result:
                events.append(accept_result["event"])
                if accept_result.get("turn_complete", True):
                    return _TurnStartResult(user_input, events, False)
            elif accept_result.get("turn_complete"):
                events.append(self._user_message_rejected_event())
                return _TurnStartResult(user_input, events, False)
        elif accept_result is not None:
            events.append(self._user_message_rejected_event())
            return _TurnStartResult(user_input, events, False)

        if new_turn:
            self.turn_count += 1
            self.session.turn_count = self.turn_count
        self.messages.append(Message(
            role="user",
            content=user_input,
            input_id=input_id,
            images=list(images or []),
            artifact=list(artifacts or []),
            additional_kwargs=_runtime_input(source, metadata),
        ))
        for event in (
            Events.AFTER_USER_MESSAGE_ACCEPT,
            Events.USER_MESSAGE,
        ):
            await self._dispatch(
                event,
                self._make_event_context(user_input=user_input),
            )
        return _TurnStartResult(user_input, events, True)
