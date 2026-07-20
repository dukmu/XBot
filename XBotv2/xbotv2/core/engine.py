"""Core ReAct loop engine.

The engine runs a 3-node ReAct loop and contains no planning, DAG, skill,
compaction, memory, summary, or subagent concepts. A session-owned background
shell manager is attached only for tool access and lifecycle cleanup; it does
not participate in ReAct state.

Without plugins, the engine implements:
    prepare_context → agent → tools → repeat (ReAct loop)

Each stage runs registered hooks. Loop hooks (before/after context/agent/tools)
can short-circuit on truthy return values.

Architecture constraint: Engine NEVER imports from builtin_plugins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from xbotv2.core.content_cache import bound_context_messages
from xbotv2.core.interactions import (
    InteractionDisconnected,
    InteractionResult,
    InteractionWaiter,
)
from xbotv2.core.internal_messages import (
    DISPLAY_CONTENT_KEY,
    structure_tool_message,
)
from xbotv2.core.mailbox import MailboxMessage
from xbotv2.api.runtime import SessionInfo
from xbotv2.api.hooks import HookContext, HookStage
from xbotv2.api.messages import Message, ModelChunk, ModelResponse
from xbotv2.api.context import ContextComponent
from xbotv2.api.prompts import prompt_container, prompt_element
from xbotv2.api.tools import ToolCall, ToolCallDelta, provider_tool_schema
from xbotv2.api.variables import RuntimeVariables
from xbotv2.persistence.store import message_to_dict


@dataclass(slots=True)
class _TurnStartResult:
    user_input: str
    events: list[dict[str, Any]]
    proceed: bool


@dataclass(slots=True)
class _ContextBuildResult:
    messages: list[Any] | None = None
    event: dict[str, Any] | None = None
    turn_complete: bool | None = None


@dataclass(slots=True)
class _ModelRequestResult:
    request: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    turn_complete: bool | None = None


@dataclass(slots=True)
class _ToolBatchResult:
    stop_loop: bool = False
    turn_complete: bool = False

logger = logging.getLogger("xbotv2.engine")

# Maximum time a single LLM provider call may take before the engine
# cancels the turn.  On timeout the engine yields an ``error`` event
# and saves state — the user can retry or switch providers.
_LLM_DISPATCH_TIMEOUT = 120.0  # seconds
_MODEL_REQUEST_ATTEMPTS = 2


def _retryable_model_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or status_code >= 500
    return isinstance(error, (ConnectionError, TimeoutError)) or type(error).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
    }


def merge_xbot_chunk(
    aggregate: ModelResponse | None,
    chunk: ModelChunk,
) -> ModelResponse:
    if not isinstance(aggregate, ModelResponse):
        aggregate = ModelResponse()
    aggregate.content += chunk.content
    if chunk.tool_calls:
        aggregate.tool_calls = chunk.tool_calls
    if chunk.response_metadata:
        aggregate.response_metadata.update(chunk.response_metadata)
    if chunk.usage_metadata:
        aggregate.usage_metadata.update(chunk.usage_metadata)
    if chunk.additional_kwargs:
        aggregate.additional_kwargs.update(chunk.additional_kwargs)
    return aggregate


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
    metadata = message.additional_kwargs
    if "xbotv2_data" in metadata:
        data["data"] = metadata["xbotv2_data"]
    if "xbotv2_error" in metadata:
        data["error"] = metadata["xbotv2_error"]
    if message.artifact:
        artifacts = (
            message.artifact
            if isinstance(message.artifact, (list, tuple))
            else [message.artifact]
        )
        data["artifacts"] = [
            artifact.to_dict()
            if hasattr(artifact, "to_dict")
            else dict(artifact)
            if isinstance(artifact, dict)
            else {"id": str(artifact)}
            for artifact in artifacts
        ]
    return data


class Engine:
    """Core ReAct loop engine.

    No plugin imports. No DAG, skills, or compaction logic.
    All extension behavior comes through hooks and the tool registry.

    Usage::

        engine = await bootstrap(...)
        async for event in engine.run_turn("list files"):
            print(event)
    """

    def __init__(
        self,
        *,
        llm: Any,
        tool_registry: Any,  # ToolRegistry
        hook_manager: Any,  # HookManager
        state_store: Any,  # CoreStateStore
        context_builder: Any,  # ContextBuilder
        sandbox_policy: Any,  # SandboxPolicy
        permission_system: Any,  # PermissionSystem
        config: Any,  # RuntimeConfig
        workspace_root: str | None = None,
        max_iterations: int = 50,
        plugin_loader: Any | None = None,
        background_tasks: Any | None = None,
        subagents: Any | None = None,
        agent_registry: Any | None = None,
        startup_config: Any | None = None,
        model: str = "",
        model_mode: str = "",
        context_window: int = 0,
        llm_is_override: bool = False,
        user_context: Any | None = None,
        runtime_variables: RuntimeVariables | None = None,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.hook_manager = hook_manager
        self.state_store = state_store
        self.context_builder = context_builder
        self.sandbox_policy = sandbox_policy
        self.permission_system = permission_system
        self.config = config
        self.workspace_root = workspace_root or ""
        self.max_iterations = max_iterations
        self.plugin_loader = plugin_loader
        self.background_tasks = background_tasks
        self.subagents = subagents
        self.agent_registry = agent_registry
        self.startup_config = startup_config or config
        self.model = model
        self.model_mode = model_mode
        self.context_window = context_window or int(
            getattr(config, "max_context_tokens", 0) or 0
        )
        self.llm_is_override = llm_is_override
        self.user_context = user_context
        self.runtime_variables = runtime_variables or RuntimeVariables.for_thread(
            state_store.paths.runtime,
            self.workspace_root,
            state_store.paths,
        )

        self.messages: list[Message] = []
        self._persisted_messages: list[dict[str, Any]] = []
        self.session: SessionInfo | None = None
        self.turn_count = 0
        self.session_usage = self._empty_usage()
        self.user_input_waiter = InteractionWaiter()
        self.permission_waiter = InteractionWaiter()
        self.client_event_sink: Any | None = None
        self.runtime_event_sink: Callable[[dict[str, Any]], None] | None = None
        self.enqueue_mailbox: (
            Callable[[str | dict[str, Any]], Awaitable[Any]] | None
        ) = None
        self.paths = state_store.paths.runtime
        self._request_id: ContextVar[str] = ContextVar(
            f"xbotv2_request_id_{id(self)}",
            default="",
        )
        self._mailbox_message: ContextVar[MailboxMessage | None] = ContextVar(
            f"xbotv2_mailbox_message_{id(self)}",
            default=None,
        )
        self._turn_instruction: ContextVar[str] = ContextVar(
            f"xbotv2_turn_instruction_{id(self)}",
            default="",
        )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self) -> None:
        """Create a new session. Runs ON_SESSION_START hooks."""
        self.session = SessionInfo(
            session_id=self.state_store.session_id,
            thread_id=self.state_store.thread_id,
            workspace_root=self.workspace_root,
            provider=str(getattr(self.config, "provider", "default")),
        )
        if self.state_store.has_existing_session():
            self.messages = self.state_store.read_messages()
            self._restore_usage()
            self._persisted_messages = self._message_snapshot()
            self._close_interrupted_tool_calls("session_restarted")
            self.turn_count = max(
                sum(1 for m in self.messages if m.role == "user"), 0
            )
            self.session.turn_count = self.turn_count
            ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
            await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)
            await self.save_messages()
        else:
            self._restore_usage()
            ctx = self._make_hook_context(HookStage.ON_SESSION_START)
            await self.hook_manager.run(HookStage.ON_SESSION_START, ctx, short_circuit=False)

    async def resume_session(self) -> None:
        """Explicit resume: load persisted messages and run ON_SESSION_RESUME hooks."""
        self.messages = self.state_store.read_messages()
        self._restore_usage()
        self._persisted_messages = self._message_snapshot()
        self._close_interrupted_tool_calls("session_restarted")
        self.turn_count = max(
            sum(1 for m in self.messages if m.role == "user"), 0
        )
        self.session = SessionInfo(
            session_id=self.state_store.session_id,
            thread_id=self.state_store.thread_id,
            workspace_root=self.workspace_root,
            provider=str(getattr(self.config, "provider", "default")),
            turn_count=self.turn_count,
        )
        ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
        await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)
        await self.save_messages()

    async def close_session(self) -> None:
        """Close hooks, persist messages, and release plugin resources."""
        self.cancel_pending_user_inputs("session_closed")
        self.cancel_pending_permissions("session_closed")
        errors: list[BaseException] = []
        if self.background_tasks is not None:
            try:
                await self.background_tasks.close()
            except BaseException as exc:
                errors.append(exc)
        if self.subagents is not None:
            try:
                await self.subagents.close()
            except BaseException as exc:
                errors.append(exc)
        try:
            ctx = self._make_hook_context(HookStage.ON_SESSION_CLOSE)
            await self.hook_manager.run(HookStage.ON_SESSION_CLOSE, ctx, short_circuit=False)
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.save_messages()
        except BaseException as exc:
            errors.append(exc)

        plugin_loader = self.plugin_loader
        self.plugin_loader = None
        if plugin_loader is not None:
            try:
                await plugin_loader.unload_all()
            except BaseException as exc:
                errors.append(exc)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Session close failed", errors)

    async def replace_history(
        self,
        messages: list[Message],
        *,
        operation: str = "checkpoint",
        turns: int = 0,
    ) -> None:
        """Replace persisted conversation history at an idle command boundary."""
        self.messages = list(messages)
        self.turn_count = sum(1 for message in self.messages if message.role == "user")
        if self.session is not None:
            self.session.turn_count = self.turn_count
        await self.save_messages(history_operation=(operation, turns))

    async def run_context_maintenance(self) -> bool:
        """Apply a pending ``BEFORE_CONTEXT`` rewrite at an idle boundary."""
        before_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT)
        result = await self.hook_manager.run(
            HookStage.BEFORE_CONTEXT,
            before_ctx,
            short_circuit=True,
        )
        if result is None:
            return False
        event = await self._handle_compaction(result)
        if event is not None:
            message = event.get("data", {}).get(
                "message",
                "Context maintenance was rejected by a hook.",
            )
            raise RuntimeError(message)
        await self.save_messages()
        return True

    async def _prepare_tool_calls(
        self,
        tool_calls: list[ToolCall],
        *,
        agent_response: ModelResponse | None = None,
    ) -> bool:
        before_ctx = self._make_hook_context(
            HookStage.BEFORE_TOOLS,
            tool_calls=tool_calls,
            agent_response=agent_response,
        )
        before_result = await self.hook_manager.run(
            HookStage.BEFORE_TOOLS,
            before_ctx,
            short_circuit=True,
        )
        if before_result is not None:
            return False

        parsed_ctx = self._make_hook_context(
            HookStage.ON_TOOL_CALLS_PARSED,
            tool_calls=tool_calls,
            agent_response=agent_response,
        )
        await self.hook_manager.run(
            HookStage.ON_TOOL_CALLS_PARSED,
            parsed_ctx,
            short_circuit=False,
        )
        return True

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
    ) -> list[Message]:
        from xbotv2.tools.runtime import execute_tools

        results = await execute_tools(
            tool_calls,
            self.tool_registry,
            sandbox_policy=self.sandbox_policy,
            permission_system=self.permission_system,
            hook_manager=self.hook_manager,
            hook_context_factory=self._make_hook_context,
            client_interaction_handler=(
                self._handle_user_input_request
                if self.client_event_sink is not None
                else None
            ),
            permission_interaction_handler=(
                self._handle_permission_request
                if self.client_event_sink is not None
                else None
            ),
            workspace_root=self.workspace_root,
        )
        after_ctx = self._make_hook_context(
            HookStage.AFTER_TOOLS,
            tool_results=results,
        )
        after_result = await self.hook_manager.run(
            HookStage.AFTER_TOOLS,
            after_ctx,
            short_circuit=True,
        )
        if isinstance(after_result, dict) and "tool_results" in after_result:
            results = list(after_result["tool_results"])
        return results

    def set_client_event_sink(self, sink: Any | None) -> Any | None:
        """Install a live protocol sink for client-directed events."""
        previous = self.client_event_sink
        self.client_event_sink = sink
        return previous

    def emit_runtime_event(self, event: dict[str, Any]) -> None:
        if self.runtime_event_sink is not None:
            self.runtime_event_sink(event)

    def submit_user_input(self, request_id: str, answer: Any) -> InteractionResult:
        return self.user_input_waiter.answer(request_id, answer=answer)

    def cancel_user_input(self, request_id: str, reason: str = "cancelled") -> InteractionResult:
        return self.user_input_waiter.cancel(request_id, reason)

    def cancel_pending_user_inputs(self, reason: str = "cancelled") -> list[InteractionResult]:
        return self.user_input_waiter.cancel_all(reason)

    def submit_permission_response(
        self,
        request_id: str,
        decision: str,
    ) -> InteractionResult:
        return self.permission_waiter.answer(request_id, decision=decision)

    def cancel_pending_permissions(self, reason: str = "cancelled") -> list[InteractionResult]:
        return self.permission_waiter.cancel_all(reason)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        user_input: str,
        *,
        request_id: str = "",
        mailbox_message: MailboxMessage | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        request_token = self._request_id.set(request_id)
        mailbox_token = self._mailbox_message.set(mailbox_message)
        instruction_token = self._turn_instruction.set("")
        if mailbox_message is not None:
            self.state_store.append_mailbox_delivery(mailbox_message)
        turn_started = False
        turn_ended = False
        try:
            async for event in self._run_turn_impl(
                user_input,
                input_kind=(
                    mailbox_message.kind
                    if mailbox_message is not None
                    else "user_message"
                ),
            ):
                if event.get("type") == "turn_started":
                    turn_started = True
                elif event.get("type") in {"turn_finished", "turn_cancelled"}:
                    turn_ended = True
                yield event
        except asyncio.CancelledError:
            logger.info("Turn %s interrupted by client", self.turn_count)
            self._close_interrupted_tool_calls("client_interrupt")
            if not turn_ended:
                turn_ctx = self._make_hook_context(
                    HookStage.ON_TURN_END,
                    stop_reason="client_interrupt",
                )
                await self.hook_manager.run(
                    HookStage.ON_TURN_END,
                    turn_ctx,
                    short_circuit=False,
                )
            yield {
                "type": "turn_cancelled",
                "data": {
                    "turn": self.turn_count,
                    "reason": "client_interrupt",
                },
            }
            raise
        except InteractionDisconnected:
            logger.info("Turn stopped because the client disconnected during an interaction")
            self._close_interrupted_tool_calls("client_disconnected")
            yield {
                "type": "turn_cancelled",
                "data": {
                    "turn": self.turn_count,
                    "reason": "client_disconnected",
                },
            }
        except BaseException as exc:
            logger.exception("Turn failed")
            failure_ctx = self._make_hook_context(
                HookStage.ON_STOP_FAILURE,
                user_input=user_input,
                stop_reason="error",
                error=exc,
            )
            await self.hook_manager.run(HookStage.ON_STOP_FAILURE, failure_ctx, short_circuit=False)
            ctx = self._make_hook_context(HookStage.ON_ERROR, user_input=user_input, error=exc)
            await self.hook_manager.run(HookStage.ON_ERROR, ctx, short_circuit=False)
            yield {
                "type": "error",
                "data": {
                    "code": "engine_error",
                    "message": str(exc),
                    "details": {"exception_type": type(exc).__name__},
                },
            }
            if turn_started:
                yield {
                    "type": "turn_finished",
                    "data": {"turn": self.turn_count},
                }
        finally:
            try:
                await self.save_messages()
            finally:
                self._request_id.reset(request_token)
                self._mailbox_message.reset(mailbox_token)
                self._turn_instruction.reset(instruction_token)

    async def _run_turn_impl(
        self,
        user_input: str,
        *,
        input_kind: str = "user_message",
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute one user turn through the ReAct loop.

        Yields event dicts: {"type": str, "data": {...}}
        """
        turn_start = await self._start_turn(user_input, input_kind=input_kind)
        for event in turn_start.events:
            yield event
        if not turn_start.proceed:
            return
        user_input = turn_start.user_input

        # 4. ReAct loop
        iteration = 0
        turn_complete = False

        while not turn_complete and iteration < self.max_iterations:
            iteration += 1

            context_build = await self._build_turn_context()
            if context_build.event is not None:
                yield context_build.event
            if context_build.turn_complete is not None:
                turn_complete = context_build.turn_complete
                break
            assert context_build.messages is not None
            context_messages = context_build.messages

            model_preparation = await self._prepare_model_request(context_messages)
            if model_preparation.event is not None:
                yield model_preparation.event
            if model_preparation.turn_complete is not None:
                turn_complete = model_preparation.turn_complete
                break
            assert model_preparation.request is not None
            model_request = model_preparation.request
            context_messages = model_request["messages"]
            llm_with_tools = model_request["llm"]
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
            except asyncio.TimeoutError:
                logger.error(
                    "engine.turn LLM timed out after %ss (turn=%d)",
                    _LLM_DISPATCH_TIMEOUT,
                    self.turn_count,
                )
                raise asyncio.TimeoutError(
                    f"LLM call timed out after {_LLM_DISPATCH_TIMEOUT}s"
                ) from None
            except BaseException as exc:
                err_ctx = self._make_hook_context(
                    HookStage.ON_MODEL_REQUEST_ERROR,
                    context_messages=context_messages,
                    model_request=model_request,
                    error=exc,
                )
                await self.hook_manager.run(
                    HookStage.ON_MODEL_REQUEST_ERROR,
                    err_ctx,
                    short_circuit=False,
                )
                raise
            content = response.content if hasattr(response, "content") else str(response)
            if not str(content).strip() and not response.tool_calls:
                reasoning = str(
                    (getattr(response, "additional_kwargs", None) or {}).get(
                        "reasoning_content", ""
                    )
                )
                after_tool = bool(
                    self.messages and self.messages[-1].role == "tool"
                )
                stop_reason = (
                    getattr(response, "response_metadata", None) or {}
                ).get("stop_reason", "unknown")
                context = " after ToolResult" if after_tool else ""
                logger.debug(
                    "invalid model response%s stop_reason=%s reasoning=%r",
                    context,
                    stop_reason,
                    reasoning[:1000],
                )
                raise RuntimeError(
                    f"LLM returned no assistant content or ToolUse{context} "
                    f"(stop_reason={stop_reason}, reasoning_chars={len(reasoning)})"
                )
            response_msg = Message(
                role="assistant",
                content=content,
                tool_calls=getattr(response, "tool_calls", None) or [],
                usage_metadata=getattr(response, "usage_metadata", None) or {},
                response_metadata=getattr(response, "response_metadata", None) or {},
                additional_kwargs=getattr(response, "additional_kwargs", None) or {},
            )
            self.messages.append(response_msg)
            self._record_usage(response_msg.usage_metadata)

            yield {
                "type": "assistant_message",
                "data": {
                    "content": content,
                    "tool_calls": [call.to_dict() for call in response.tool_calls],
                },
            }
            usage = self._extract_usage(response)
            if usage:
                yield {
                    "type": "usage",
                    "data": usage,
                }

            # ON_ASSISTANT_MESSAGE hook
            am_ctx = self._make_hook_context(
                HookStage.ON_ASSISTANT_MESSAGE, agent_response=response
            )
            await self.hook_manager.run(HookStage.ON_ASSISTANT_MESSAGE, am_ctx, short_circuit=False)

            response_ctx = self._make_hook_context(
                HookStage.AFTER_MODEL_RESPONSE,
                context_messages=context_messages,
                agent_response=response,
                model_request=model_request,
                model_response=response,
            )
            await self.hook_manager.run(
                HookStage.AFTER_MODEL_RESPONSE,
                response_ctx,
                short_circuit=False,
            )

            # AFTER_AGENT hook
            aa_ctx = self._make_hook_context(HookStage.AFTER_AGENT, agent_response=response)
            agent_result = await self.hook_manager.run(
                HookStage.AFTER_AGENT, aa_ctx, short_circuit=True
            )
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
                    break

            # Check for tool calls
            tool_calls = response.tool_calls
            if not tool_calls:
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

        yield await self._finish_turn(
            "completed" if turn_complete else "max_iterations"
        )

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

        logger.info(
            "engine.turn tool_calls_parsed turn=%d n=%d names=%s",
            self.turn_count,
            len(tool_calls),
            [call.name for call in tool_calls],
        )
        yield {
            "type": "tool_calls_started",
            "data": {"tool_calls": [call.to_dict() for call in tool_calls]},
        }
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

        logger.info(
            "engine.turn tool_messages_built turn=%d n=%d ids=%s statuses=%s",
            self.turn_count,
            len(tool_messages),
            [getattr(message, "tool_call_id", None) for message in tool_messages],
            [getattr(message, "status", None) for message in tool_messages],
        )
        self.messages.extend(tool_messages)
        # Commit tool responses before exposing them or requesting another model step.
        await self.save_messages()

        for message, event_payload in zip(
            tool_messages,
            tool_event_payloads,
            strict=True,
        ):
            client_events = getattr(message, "additional_kwargs", {}).get(
                "xbotv2_events",
                [],
            )
            for client_event in client_events:
                event_ctx = self._make_hook_context(
                    HookStage.ON_CLIENT_EVENT,
                    tool_result=message,
                    client_event=client_event,
                )
                await self.hook_manager.run(
                    HookStage.ON_CLIENT_EVENT,
                    event_ctx,
                    short_circuit=False,
                )
                yield client_event
            yield {
                "type": "tool_result",
                "data": event_payload,
            }

        for message in tool_messages:
            message_ctx = self._make_hook_context(
                HookStage.ON_TOOL_MESSAGE,
                tool_results=[message],
            )
            await self.hook_manager.run(
                HookStage.ON_TOOL_MESSAGE,
                message_ctx,
                short_circuit=False,
            )

        if any(
            getattr(message, "additional_kwargs", {}).get(
                "xbotv2_turn_complete"
            )
            for message in tool_messages
        ):
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
    ) -> _TurnStartResult:
        if input_kind == "general":
            self.turn_count += 1
            if self.session is not None:
                self.session.turn_count = self.turn_count
            turn_ctx = self._make_hook_context(
                HookStage.ON_TURN_START,
                user_input=user_input,
            )
            await self.hook_manager.run(
                HookStage.ON_TURN_START,
                turn_ctx,
                short_circuit=False,
            )
            user_input = str(turn_ctx.user_input or user_input)
            user_input = self._runtime_event_content(
                user_input,
                mailbox_message=self._mailbox_message.get(),
            )
            self._turn_instruction.set(user_input)
            return _TurnStartResult(
                user_input,
                [{"type": "turn_started", "data": {"turn": self.turn_count}}],
                True,
            )
        accept_ctx = self._make_hook_context(
            HookStage.BEFORE_USER_MESSAGE_ACCEPT,
            user_input=user_input,
        )
        accept_result = await self.hook_manager.run(
            HookStage.BEFORE_USER_MESSAGE_ACCEPT,
            accept_ctx,
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

        self.turn_count += 1
        if self.session is not None:
            self.session.turn_count = self.turn_count
        self.messages.append(Message(role="user", content=user_input))

        accepted_ctx = self._make_hook_context(
            HookStage.AFTER_USER_MESSAGE_ACCEPT,
            user_input=user_input,
        )
        await self.hook_manager.run(
            HookStage.AFTER_USER_MESSAGE_ACCEPT,
            accepted_ctx,
            short_circuit=False,
        )
        user_ctx = self._make_hook_context(
            HookStage.ON_USER_MESSAGE,
            user_input=user_input,
        )
        await self.hook_manager.run(
            HookStage.ON_USER_MESSAGE,
            user_ctx,
            short_circuit=False,
        )
        turn_ctx = self._make_hook_context(
            HookStage.ON_TURN_START,
            user_input=user_input,
        )
        await self.hook_manager.run(
            HookStage.ON_TURN_START,
            turn_ctx,
            short_circuit=False,
        )
        events.append({"type": "turn_started", "data": {"turn": self.turn_count}})
        return _TurnStartResult(user_input, events, True)

    @staticmethod
    def _user_message_rejected_event() -> dict[str, Any]:
        return {
            "type": "error",
            "data": {
                "code": "user_message_rejected",
                "message": "User message was rejected before entering history.",
            },
        }

    async def _build_turn_context(self) -> _ContextBuildResult:
        before_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT)
        compact_result = await self.hook_manager.run(
            HookStage.BEFORE_CONTEXT,
            before_ctx,
            short_circuit=True,
        )
        if compact_result is not None:
            event = await self._handle_compaction(compact_result)
            if event is not None:
                return _ContextBuildResult(event=event, turn_complete=True)

        turn_messages = list(self.messages)
        turn_instruction = self._turn_instruction.get()
        if turn_instruction:
            mailbox_message = self._mailbox_message.get()
            turn_messages.append(Message(
                role=(
                    "user"
                    if mailbox_message is not None
                    and mailbox_message.kind == "general"
                    else "system"
                ),
                content=turn_instruction,
                additional_kwargs={
                    "xbotv2_source": "runtime_mailbox",
                    "xbotv2_runtime_input": True,
                },
            ))
        context_kwargs = {
            "messages": turn_messages,
            "agent_name": getattr(self.config, "agent_name", "XBotv2"),
            "agent_role": getattr(self.config, "agent_role", ""),
            "user_name": getattr(self.user_context, "user_name", "User"),
            "user_id": getattr(self.user_context, "user_id", "default-user"),
            "developer_instructions": getattr(self.config, "instructions", ""),
            "instructions": getattr(self.config, "agent_instructions", ""),
            "memory": getattr(self.config, "memory", ""),
            "sandbox_summary": (
                self.sandbox_policy.describe() if self.sandbox_policy else ""
            ),
            "runtime_paths": {
                "workspace": self.runtime_variables.get("workspace", "."),
                "session": "session/ (read-only)",
                "artifacts": "session/artifacts/ (read-only)",
                "tool_results": "session/artifacts/tool_results/ (read-only)",
            },
            "system_notice": self._agent_catalog_notice(),
            "turn_count": self.turn_count,
        }
        build_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT_BUILD)
        build_result = await self.hook_manager.run(
            HookStage.BEFORE_CONTEXT_BUILD,
            build_ctx,
            short_circuit=True,
        )
        if isinstance(build_result, dict):
            if "messages" in build_result:
                self.messages = build_result["messages"]
                context_kwargs["messages"] = self.messages
            if "context_kwargs" in build_result:
                context_kwargs.update(build_result["context_kwargs"])
            if "event" in build_result:
                return _ContextBuildResult(
                    event=build_result["event"],
                    turn_complete=bool(build_result.get("turn_complete", True)),
                )
        elif build_result is not None:
            return _ContextBuildResult(
                event=self._default_hook_rejection_event(
                    HookStage.BEFORE_CONTEXT_BUILD
                ),
                turn_complete=True,
            )

        if hasattr(self.context_builder, "build_components"):
            components = self.context_builder.build_components(**context_kwargs)
            component_ctx = self._make_hook_context(
                HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
                context_components=components,
            )
            await self.hook_manager.run(
                HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
                component_ctx,
                short_circuit=False,
            )
            if component_ctx.context_components is not None:
                components = component_ctx.context_components
            context_messages = self.context_builder.messages_from_components(components)
        else:
            context_messages = self.context_builder.build(**context_kwargs)

        after_ctx = self._make_hook_context(
            HookStage.AFTER_CONTEXT,
            context_messages=context_messages,
        )
        after_result = await self.hook_manager.run(
            HookStage.AFTER_CONTEXT,
            after_ctx,
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
                event=self._default_hook_rejection_event(HookStage.AFTER_CONTEXT),
                turn_complete=True,
            )

        complete_ctx = self._make_hook_context(
            HookStage.AFTER_CONTEXT_BUILD,
            context_messages=context_messages,
        )
        await self.hook_manager.run(
            HookStage.AFTER_CONTEXT_BUILD,
            complete_ctx,
            short_circuit=False,
        )
        return _ContextBuildResult(messages=context_messages)

    def _agent_catalog_notice(self) -> str:
        registry = self.agent_registry
        if registry is None or self.tool_registry.get("task") is None:
            return ""
        definitions = [
            definition
            for definition in registry.definitions()
            if definition.mode in {"subagent", "all"} and not definition.hidden
        ]
        if not definitions:
            return ""
        lines = ["Available subagents for the task tool:"]
        lines.extend(
            f"- {definition.name}: {definition.description}"
            for definition in definitions
        )
        return "\n".join(lines)

    async def _prepare_model_request(
        self,
        context_messages: list[Any],
    ) -> _ModelRequestResult:
        before_agent_ctx = self._make_hook_context(HookStage.BEFORE_AGENT)
        before_agent = await self.hook_manager.run(
            HookStage.BEFORE_AGENT,
            before_agent_ctx,
            short_circuit=True,
        )
        if before_agent is not None:
            if isinstance(before_agent, dict) and "messages" in before_agent:
                self.messages.extend(before_agent["messages"])
            return _ModelRequestResult(turn_complete=True)

        tools = self.tool_registry.get_all()
        pre_schema_request = {
            "messages": context_messages,
            "tools": tools,
            "llm": self.llm,
        }
        pre_schema_ctx = self._make_hook_context(
            HookStage.BEFORE_TOOL_SCHEMA_BIND,
            context_messages=context_messages,
            model_request=pre_schema_request,
        )
        pre_schema_result = await self.hook_manager.run(
            HookStage.BEFORE_TOOL_SCHEMA_BIND,
            pre_schema_ctx,
            short_circuit=True,
        )
        if isinstance(pre_schema_result, dict):
            if "tools" in pre_schema_result:
                tools = pre_schema_result["tools"]
                pre_schema_request["tools"] = tools
            if "messages" in pre_schema_result:
                context_messages = pre_schema_result["messages"]
                pre_schema_request["messages"] = context_messages
            if "event" in pre_schema_result:
                return _ModelRequestResult(
                    event=pre_schema_result["event"],
                    turn_complete=bool(
                        pre_schema_result.get("turn_complete", True)
                    ),
                )
        elif pre_schema_result is not None:
            return _ModelRequestResult(
                event=self._default_hook_rejection_event(
                    HookStage.BEFORE_TOOL_SCHEMA_BIND
                ),
                turn_complete=True,
            )

        model_request = {
            "messages": context_messages,
            "tools": tools,
            "llm": self._bind_tools_for_provider(tools),
        }
        schema_ctx = self._make_hook_context(
            HookStage.AFTER_TOOL_SCHEMA_BIND,
            context_messages=context_messages,
            model_request=model_request,
        )
        await self.hook_manager.run(
            HookStage.AFTER_TOOL_SCHEMA_BIND,
            schema_ctx,
            short_circuit=False,
        )

        request_ctx = self._make_hook_context(
            HookStage.BEFORE_MODEL_REQUEST,
            context_messages=context_messages,
            model_request=model_request,
        )
        request_result = await self.hook_manager.run(
            HookStage.BEFORE_MODEL_REQUEST,
            request_ctx,
            short_circuit=True,
        )
        if isinstance(request_result, dict):
            if "messages" in request_result:
                model_request["messages"] = request_result["messages"]
            if "tools" in request_result:
                model_request["tools"] = request_result["tools"]
                model_request["llm"] = self._bind_tools_for_provider(
                    model_request["tools"]
                )
            if "llm" in request_result:
                model_request["llm"] = request_result["llm"]
            if "event" in request_result:
                return _ModelRequestResult(
                    event=request_result["event"],
                    turn_complete=bool(request_result.get("turn_complete", True)),
                )
        elif request_result is not None:
            return _ModelRequestResult(
                event=self._default_hook_rejection_event(
                    HookStage.BEFORE_MODEL_REQUEST
                ),
                turn_complete=True,
            )
        model_request["messages"] = bound_context_messages(
            model_request["messages"], self.state_store
        )
        return _ModelRequestResult(request=model_request)

    async def _finish_turn(self, stop_reason: str) -> dict[str, Any]:
        turn_ctx = self._make_hook_context(
            HookStage.ON_TURN_END,
            stop_reason=stop_reason,
        )
        await self.hook_manager.run(
            HookStage.ON_TURN_END,
            turn_ctx,
            short_circuit=False,
        )
        stop_ctx = self._make_hook_context(
            HookStage.ON_STOP,
            stop_reason=stop_reason,
        )
        try:
            await self.hook_manager.run(
                HookStage.ON_STOP,
                stop_ctx,
                short_circuit=False,
            )
        except BaseException as exc:
            failure_ctx = self._make_hook_context(
                HookStage.ON_STOP_FAILURE,
                stop_reason=stop_reason,
                error=exc,
            )
            await self.hook_manager.run(
                HookStage.ON_STOP_FAILURE,
                failure_ctx,
                short_circuit=False,
            )
            raise
        await self.save_messages()
        return {"type": "turn_finished", "data": {"turn": self.turn_count}}

    def _bind_tools_for_provider(self, tools: list[Any]) -> Any:
        if not tools:
            return self.llm
        schemas = [provider_tool_schema(tool) for tool in tools]
        try:
            return self.llm.bind_tools(schemas)
        except NotImplementedError:
            return self.llm

    async def _stream_model_response(
        self,
        llm: Any,
        context_messages: list[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream provider chunks and reconstruct the final response."""
        for attempt in range(1, _MODEL_REQUEST_ATTEMPTS + 1):
            aggregate: ModelResponse | None = None
            tool_stream_ids: dict[int, str] = {}
            model_output_emitted = False
            try:
                async with asyncio.timeout(_LLM_DISPATCH_TIMEOUT):
                    async for chunk in llm.astream(context_messages):
                        if isinstance(chunk, ModelChunk):
                            aggregate = merge_xbot_chunk(aggregate, chunk)
                            if chunk.content:
                                model_output_emitted = True
                                is_reasoning = bool(
                                    (chunk.additional_kwargs or {}).get(
                                        "reasoning_content"
                                    )
                                )
                                key = "reasoning" if is_reasoning else "content"
                                yield {
                                    "type": "assistant_message_delta",
                                    "data": {key: chunk.content},
                                }
                            for tool_delta in xbot_tool_call_deltas(
                                chunk, tool_stream_ids
                            ):
                                model_output_emitted = True
                                yield {
                                    "type": "tool_call_delta",
                                    "data": tool_delta,
                                }
                            continue
                        if isinstance(chunk, ModelResponse):
                            aggregate = chunk
                            continue
                        logger.warning(
                            "_stream_model_response: unexpected chunk type %s",
                            type(chunk).__name__,
                        )
            except Exception as exc:
                if (
                    model_output_emitted
                    or attempt == _MODEL_REQUEST_ATTEMPTS
                    or not _retryable_model_error(exc)
                ):
                    raise
                delay = 0.5 * attempt
                logger.warning(
                    "model request failed before output; retrying attempt=%d error=%s",
                    attempt,
                    exc,
                )
                yield {
                    "type": "client_message",
                    "data": {
                        "message": (
                            "Model request failed before producing output; "
                            f"retrying in {delay:.1f}s."
                        ),
                        "level": "warning",
                        "source": "runtime",
                        "tool_call_id": "",
                    },
                }
                await asyncio.sleep(delay)
                continue

            if aggregate is None:
                raise RuntimeError("LLM stream produced no chunks")
            yield {"type": "_model_response", "data": {"response": aggregate}}
            return

    async def _invoke_model(self, messages: list[Message]) -> ModelResponse:
        """Run one unbound auxiliary model call for a Hook."""
        for attempt in range(1, _MODEL_REQUEST_ATTEMPTS + 1):
            aggregate: ModelResponse | None = None
            try:
                async with asyncio.timeout(_LLM_DISPATCH_TIMEOUT):
                    async for chunk in self.llm.astream(messages):
                        if isinstance(chunk, ModelChunk):
                            aggregate = merge_xbot_chunk(aggregate, chunk)
                        elif isinstance(chunk, ModelResponse):
                            aggregate = chunk
                        else:
                            logger.warning(
                                "_invoke_model: unexpected chunk type %s",
                                type(chunk).__name__,
                            )
            except Exception as exc:
                if (
                    attempt == _MODEL_REQUEST_ATTEMPTS
                    or not _retryable_model_error(exc)
                ):
                    raise
                await asyncio.sleep(0.5 * attempt)
                continue
            if aggregate is None:
                raise RuntimeError("LLM stream produced no chunks")
            self._record_usage(aggregate.usage_metadata)
            return aggregate
        raise RuntimeError("Model request retry loop ended unexpectedly")

    async def _handle_compaction(self, short_circuit: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(short_circuit, dict) or "messages" not in short_circuit:
            return self._default_hook_rejection_event(HookStage.BEFORE_CONTEXT)

        compact_reason = str(short_circuit.get("compact_reason", "before_context"))
        pre_compact_ctx = self._make_hook_context(
            HookStage.PRE_COMPACT,
            compact_reason=compact_reason,
        )
        pre_compact_result = await self.hook_manager.run(
            HookStage.PRE_COMPACT,
            pre_compact_ctx,
            short_circuit=True,
        )
        if isinstance(pre_compact_result, dict):
            if "messages" in pre_compact_result:
                short_circuit["messages"] = pre_compact_result["messages"]
            if "compact_reason" in pre_compact_result:
                compact_reason = str(pre_compact_result["compact_reason"])
        elif pre_compact_result is not None:
            return self._default_hook_rejection_event(HookStage.PRE_COMPACT)

        previous_message_count = len(self.messages)
        self.messages = short_circuit["messages"]
        post_compact_ctx = self._make_hook_context(
            HookStage.POST_COMPACT,
            compact_reason=compact_reason,
        )
        post_compact_ctx.state.update({
            "previous_message_count": previous_message_count,
            "current_message_count": len(self.messages),
        })
        await self.hook_manager.run(HookStage.POST_COMPACT, post_compact_ctx, short_circuit=False)
        await self.save_messages(
            history_operation=(f"compact:{compact_reason}", 0)
        )
        self.emit_runtime_event({
            "type": "compaction_completed",
            "data": {
                "reason": compact_reason,
                "metrics": dict(short_circuit.get("compact_metrics") or {}),
                "usage": dict(self.session_usage),
            },
        })
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------



    async def save_messages(
        self,
        *,
        history_operation: tuple[str, int] | None = None,
    ) -> bool:
        """Persist changed message history and bracket the write with hooks."""
        if (
            history_operation is None
            and self._message_snapshot() == self._persisted_messages
        ):
            return False
        before_ctx = self._make_hook_context(HookStage.BEFORE_STATE_PERSIST)
        await self.hook_manager.run(HookStage.BEFORE_STATE_PERSIST, before_ctx, short_circuit=False)
        snapshot = self._message_snapshot()
        if history_operation is None:
            self.state_store.sync_messages(self.messages)
        else:
            operation, turns = history_operation
            if operation == "clear":
                self.state_store.append_clear()
            elif operation == "undo":
                self.state_store.append_undo(turns)
            else:
                self.state_store.append_checkpoint(
                    self.messages,
                    reason=operation,
                )
        self._persisted_messages = snapshot
        after_ctx = self._make_hook_context(HookStage.AFTER_STATE_PERSIST)
        await self.hook_manager.run(HookStage.AFTER_STATE_PERSIST, after_ctx, short_circuit=False)
        return True

    def _message_snapshot(self) -> list[dict[str, Any]]:
        return [message_to_dict(message) for message in self.messages]

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

    async def _handle_client_interaction(
        self,
        client_event: dict[str, Any],
        waiter: Any,
        result_fields: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
        on_sink_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        ctx = self._make_hook_context(HookStage.ON_CLIENT_EVENT, client_event=client_event)
        await self.hook_manager.run(HookStage.ON_CLIENT_EVENT, ctx, short_circuit=False)

        if self.client_event_sink is not None:
            sink_result = await self.client_event_sink(
                client_event, timeout_seconds=timeout_seconds, tool_call_id="",
            )
            if sink_result.get("status") == "disconnected":
                raise InteractionDisconnected(
                    "Client disconnected while waiting for "
                    f"{sink_result.get('request_id')}"
                )
            if on_sink_result:
                on_sink_result(sink_result)
            return sink_result
        request_id = str((client_event.get("data") or {}).get("request_id") or "")
        wait_timeout = 0 if timeout_seconds is None else timeout_seconds
        result = await waiter.wait(request_id, wait_timeout)
        return {
            field: getattr(result, field, "")
            for field in result_fields
        } | {
            "request_id": result.request_id,
            "status": result.status,
            "reason": result.reason,
        }

    async def _handle_user_input_request(
        self,
        client_event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        return await self._handle_client_interaction(
            client_event,
            self.user_input_waiter,
            ("answer",),
            timeout_seconds=timeout_seconds,
        )

    async def _request_user_input(
        self,
        question: str,
        *,
        options: list[str] | None = None,
        timeout_seconds: float | None = None,
        source: str = "plugin",
    ) -> dict[str, Any]:
        request_id = f"user_input:{source}:{uuid.uuid4().hex}"
        return await self._handle_user_input_request(
            {
                "type": "user_input_required",
                "data": {
                    "request_id": request_id,
                    "source": source,
                    "question": question,
                    "options": options or [],
                    "timeout_seconds": timeout_seconds,
                    "resume_supported": False,
                },
            },
            timeout_seconds=timeout_seconds,
        )

    async def _handle_permission_request(
        self,
        client_event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        return await self._handle_client_interaction(
            client_event, self.permission_waiter, ("decision", "scope"),
            timeout_seconds=timeout_seconds,
            on_sink_result=lambda result: self.record_permission_decision(
                client_event, result
            ),
        )

    def record_permission_decision(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        decision = str(result.get("decision") or "")
        if decision not in {"allow", "deny"}:
            return
        data = client_event.get("data") or {}
        scope = str(result.get("scope") or "once")
        if (
            decision == "allow"
            and scope == "once"
            and data.get("source") == "request_permission"
        ):
            permission = dict(data.get("permission") or {})
            self.permission_system.grant_once(
                str(permission.get("tool") or ""),
                dict(permission.get("params") or {}),
            )
            return
        if scope != "session":
            return
        try:
            from pathlib import Path
            from xbotv2.config.policy import persist_permission_decision
            persist_permission_decision(
                paths=self.paths,
                session_id=self.state_store.session_id,
                client_event=client_event,
                decision=str(result.get("decision") or ""),
                scope="session",
                engine=self,
            )
        except Exception:
            logger.exception("permission persistence failed")

    @staticmethod
    def _default_hook_rejection_event(stage: HookStage) -> dict[str, Any]:
        return {
            "type": "error",
            "data": {
                "code": "hook_short_circuit_rejected",
                "message": f"Hook {stage.value} short-circuited without a structured result.",
                "stage": stage.value,
            },
        }

    def _make_hook_context(
        self,
        stage: HookStage,
        *,
        user_input: str | None = None,
        context_components: list[ContextComponent] | None = None,
        context_messages: list[Any] | None = None,
        agent_response: Any = None,
        model_request: dict[str, Any] | None = None,
        model_response: Any = None,
        tool_calls: list[ToolCall] | None = None,
        tool_call: ToolCall | None = None,
        tool_results: list[Any] | None = None,
        tool_result: Any = None,
        stop_reason: str | None = None,
        compact_reason: str | None = None,
        permission_decision: str | None = None,
        client_event: dict[str, Any] | None = None,
        error: Exception | None = None,
        mailbox_message: MailboxMessage | None = None,
    ) -> HookContext:
        return HookContext(
            stage=stage,
            request_id=self._request_id.get(),
            state={"messages": self.messages},
            config=self.config,
            tools=self.tool_registry,
            sandbox=self.sandbox_policy,
            plugin_store=None,
            invoke_model=self._invoke_model,
            request_user_input=self._request_user_input,
            enqueue_mailbox=self.enqueue_mailbox,
            emit=self.emit_runtime_event,
            session=self.session or SessionInfo(
                session_id=self.state_store.session_id,
                thread_id=self.state_store.thread_id,
                workspace_root=self.workspace_root,
                provider=str(getattr(self.config, "provider", "default")),
                turn_count=self.turn_count,
            ),
            user_input=user_input,
            context_components=context_components,
            context_messages=context_messages,
            agent_response=agent_response,
            model_request=model_request,
            model_response=model_response,
            tool_calls=tool_calls,
            tool_call=tool_call,
            tool_results=tool_results,
            tool_result=tool_result,
            stop_reason=stop_reason,
            compact_reason=compact_reason,
            permission_decision=permission_decision,
            client_event=client_event,
            error=error,
            mailbox_message=(
                mailbox_message
                if mailbox_message is not None
                else self._mailbox_message.get()
            ),
        )

    async def run_mailbox_hook(
        self,
        stage: HookStage,
        message: MailboxMessage,
        *,
        error: Exception | None = None,
    ) -> None:
        ctx = self._make_hook_context(
            stage,
            mailbox_message=message,
            error=error,
        )
        await self.hook_manager.run(stage, ctx, short_circuit=False)

    @staticmethod
    def mailbox_content(message: MailboxMessage) -> str:
        if isinstance(message.message, str):
            return message.message
        return json.dumps(
            message.message,
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _runtime_event_content(
        payload: str,
        *,
        mailbox_message: MailboxMessage | None = None,
    ) -> str:
        message = getattr(mailbox_message, "message", None)
        attributes = {"kind": "general", "source": "mailbox"}
        if isinstance(message, dict):
            attributes.update({
                "source": str(message.get("source") or "runtime"),
                "event": str(message.get("event") or "message"),
            })
        try:
            json.loads(payload)
            encoding = "json"
        except (json.JSONDecodeError, TypeError):
            encoding = "text"
        return prompt_container(
            "runtime_event",
            [
                prompt_element(
                    "instruction",
                    "This is runtime-generated input, not a human message. "
                    "Continue the active work using the event and do not ask "
                    "the human to repeat the preceding request.",
                ),
                prompt_element(
                    "payload",
                    payload,
                    attributes={"encoding": encoding},
                ),
            ],
            attributes=attributes,
        )

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "context_tokens": 0,
        }

    def _restore_usage(self) -> None:
        usage = self.state_store.read_usage()
        if usage is None:
            usage = self._empty_usage()
            for message in self.messages:
                self._add_usage(usage, message.usage_metadata)
            self.state_store.write_usage(usage)
        self.session_usage = usage

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        if self._add_usage(self.session_usage, usage):
            self.state_store.write_usage(self.session_usage)

    @staticmethod
    def _add_usage(
        total: dict[str, int],
        usage: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(usage, dict):
            return False
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if not input_tokens and not output_tokens:
            return False
        total["input_tokens"] += input_tokens
        total["output_tokens"] += output_tokens
        total["total_tokens"] += int(
            usage.get("total_tokens") or input_tokens + output_tokens
        )
        total["requests"] += int(usage.get("requests") or 1)
        total["context_tokens"] = int(
            usage.get("context_tokens") or input_tokens
        )
        return True

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int] | None:
        usage_dict = getattr(response, "usage_metadata", None)
        if not isinstance(usage_dict, dict):
            return None
        input_tokens = int(usage_dict.get("input_tokens") or 0)
        output_tokens = int(usage_dict.get("output_tokens") or 0)
        if not input_tokens and not output_tokens:
            return None
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage_dict.get("total_tokens") or input_tokens + output_tokens),
            "requests": int(usage_dict.get("requests") or 1),
            "context_tokens": int(
                usage_dict.get("context_tokens") or input_tokens
            ),
        }
