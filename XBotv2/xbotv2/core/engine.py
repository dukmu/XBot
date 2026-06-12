"""Core ReAct loop engine.

The engine runs a 3-node ReAct loop and contains NO references to
plan, task, dag, skill, compact, memory, summary, or subagent concepts.

Without plugins, the engine implements:
    prepare_context → agent → tools → repeat (ReAct loop)

Each stage runs registered hooks. Loop hooks (before/after context/agent/tools)
can short-circuit on truthy return values.

Architecture constraint: Engine NEVER imports from builtin_plugins.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from xbotv2.core.interactions import (
    InteractionDisconnected,
    InteractionResult,
    InteractionWaiter,
)
from xbotv2.core.state import SessionInfo
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.llm.messages import Message, XBotModelChunk, XBotModelResponse
from xbotv2.tools.types import provider_tool_schema

logger = logging.getLogger("xbotv2.engine")

# Maximum time a single LLM provider call may take before the engine
# cancels the turn.  On timeout the engine yields an ``error`` event
# and saves state — the user can retry or switch providers.
_LLM_DISPATCH_TIMEOUT = 120.0  # seconds


def merge_xbot_chunk(
    aggregate: XBotModelResponse | None,
    chunk: XBotModelChunk,
) -> XBotModelResponse:
    if not isinstance(aggregate, XBotModelResponse):
        aggregate = XBotModelResponse()
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
    chunk: XBotModelChunk,
    tool_stream_ids: dict[int, str],
) -> list[dict[str, Any]]:
    raw_chunks = chunk.tool_call_chunks or chunk.tool_calls
    deltas: list[dict[str, Any]] = []
    for index, tool_call in enumerate(raw_chunks):
        chunk_index = int(tool_call.get("index", index))
        prior_id = tool_stream_ids.get(chunk_index)
        tool_id = tool_call.get("id") or prior_id or f"tool_{chunk_index}"
        tool_stream_ids[chunk_index] = tool_id
        args_delta = tool_call.get("args", {})
        delta = {
                "tool_call_id": tool_id,
                "id": tool_id,
                "name": tool_call.get("name", "tool"),
                "args_delta": args_delta,
                "args": args_delta,
                "index": chunk_index,
        }
        if prior_id and prior_id != tool_id:
            delta["replaces_tool_call_id"] = prior_id
        deltas.append({"tool_calls": [delta]})
    return deltas


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
        config: Any,  # SystemConfig
        workspace_root: str | None = None,
        max_iterations: int = 50,
        data_dir: str | None = None,
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

        self.messages: list[Message] = []
        self.session: SessionInfo | None = None
        self.turn_count = 0
        self.user_input_waiter = InteractionWaiter()
        self.permission_waiter = InteractionWaiter()
        self.client_event_sink: Any | None = None

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
            self.turn_count = max(
                sum(1 for m in self.messages if m.role == "user"), 0
            )
            self.session.turn_count = self.turn_count
            ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
            await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)
        else:
            ctx = self._make_hook_context(HookStage.ON_SESSION_START)
            await self.hook_manager.run(HookStage.ON_SESSION_START, ctx, short_circuit=False)

    async def resume_session(self) -> None:
        """Explicit resume: load persisted messages and run ON_SESSION_RESUME hooks."""
        self.messages = self.state_store.read_messages()
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

    async def close_session(self) -> None:
        """Execute ON_SESSION_CLOSE hooks. Messages remain persisted on disk."""
        self.cancel_pending_user_inputs("session_closed")
        self.cancel_pending_permissions("session_closed")
        ctx = self._make_hook_context(HookStage.ON_SESSION_CLOSE)
        await self.hook_manager.run(HookStage.ON_SESSION_CLOSE, ctx, short_circuit=False)
        await self.save_messages()

    def set_client_event_sink(self, sink: Any | None) -> Any | None:
        """Install a live protocol sink for client-directed events."""
        previous = self.client_event_sink
        self.client_event_sink = sink
        return previous

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

    async def run_turn(self, user_input: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._run_turn_impl(user_input):
                yield event
        except asyncio.CancelledError:
            logger.info("Turn %s interrupted by client", self.turn_count)
            yield {"type": "turn_cancelled", "data": {"turn": self.turn_count, "reason": "client_interrupt"}}
            raise
        except InteractionDisconnected as exc:
            logger.info("Turn stopped because the client disconnected during an interaction")
        except BaseException as exc:
            logger.exception("Turn failed")
            failure_ctx = self._make_hook_context(HookStage.ON_STOP_FAILURE, user_input=user_input, stop_reason="error", error=exc)
            await self.hook_manager.run(HookStage.ON_STOP_FAILURE, failure_ctx, short_circuit=False)
            ctx = self._make_hook_context(HookStage.ON_ERROR, user_input=user_input, error=exc)
            await self.hook_manager.run(HookStage.ON_ERROR, ctx, short_circuit=False)
            yield {"type": "error", "data": {"code": type(exc).__name__, "message": str(exc)}}
        finally:
            await self.save_messages()

    async def _run_turn_impl(self, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """Execute one user turn through the ReAct loop.

        Yields event dicts: {"type": str, "data": {...}}
        """
        accept_ctx = self._make_hook_context(
            HookStage.BEFORE_USER_MESSAGE_ACCEPT,
            user_input=user_input,
        )
        accept_result = await self.hook_manager.run(
            HookStage.BEFORE_USER_MESSAGE_ACCEPT,
            accept_ctx,
            short_circuit=True,
        )
        if isinstance(accept_result, dict):
            if "user_input" in accept_result:
                user_input = str(accept_result["user_input"])
            if "event" in accept_result:
                yield accept_result["event"]
                if accept_result.get("turn_complete", True):
                    return
            elif accept_result.get("turn_complete"):
                yield {
                    "type": "error",
                    "data": {
                        "code": "user_message_rejected",
                        "message": "User message was rejected before entering history.",
                    },
                }
                return
        elif accept_result is not None:
            yield {
                "type": "error",
                "data": {
                    "code": "user_message_rejected",
                    "message": "User message was rejected before entering history.",
                },
            }
            return

        self.turn_count += 1

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

        um_ctx = self._make_hook_context(HookStage.ON_USER_MESSAGE, user_input=user_input)
        await self.hook_manager.run(HookStage.ON_USER_MESSAGE, um_ctx, short_circuit=False)

        ts_ctx = self._make_hook_context(HookStage.ON_TURN_START, user_input=user_input)
        await self.hook_manager.run(HookStage.ON_TURN_START, ts_ctx, short_circuit=False)

        yield {"type": "turn_started", "data": {"turn": self.turn_count}}

        # 4. ReAct loop
        iteration = 0
        turn_complete = False

        while not turn_complete and iteration < self.max_iterations:
            iteration += 1

            # --- prepare_context equivalent ---
            bc_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT)
            short_circuit = await self.hook_manager.run(
                HookStage.BEFORE_CONTEXT, bc_ctx, short_circuit=True
            )
            if short_circuit is not None:
                result = await self._handle_compaction(short_circuit)
                if result is not None:
                    yield result
                    turn_complete = True
                    break

            context_kwargs = {
                "messages": self.messages,
                "agent_name": getattr(self.config, "agent_name", "XBotv2"),
                "agent_role": getattr(self.config, "agent_role", ""),
                "user_name": "User",
                "user_id": "default-user",
                "instructions": getattr(
                    self.config,
                    "effective_instructions",
                    getattr(self.config, "instructions", ""),
                ),
                "memory": getattr(self.config, "memory", ""),
                "sandbox_summary": self.sandbox_policy.describe() if self.sandbox_policy else "",
                "turn_count": self.turn_count,
            }
            bcb_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT_BUILD)
            build_result = await self.hook_manager.run(
                HookStage.BEFORE_CONTEXT_BUILD,
                bcb_ctx,
                short_circuit=True,
            )
            if isinstance(build_result, dict):
                if "messages" in build_result:
                    self.messages = build_result["messages"]
                    context_kwargs["messages"] = self.messages
                if "context_kwargs" in build_result:
                    context_kwargs.update(build_result["context_kwargs"])
                if "event" in build_result:
                    yield build_result["event"]
                    turn_complete = bool(build_result.get("turn_complete", True))
                    break
            elif build_result is not None:
                yield self._default_hook_rejection_event(HookStage.BEFORE_CONTEXT_BUILD)
                turn_complete = True
                break

            if hasattr(self.context_builder, "build_components"):
                # Build source-tagged context components and provider messages.
                context_components = self.context_builder.build_components(
                    **context_kwargs,
                )
                component_ctx = self._make_hook_context(
                    HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
                    context_components=context_components,
                )
                component_result = await self.hook_manager.run(
                    HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
                    component_ctx,
                    short_circuit=False,
                )
                if component_ctx.context_components is not None:
                    context_components = component_ctx.context_components

                context_messages = self.context_builder.messages_from_components(
                    context_components
                )
            else:
                context_messages = self.context_builder.build(
                    **context_kwargs,
                )

            ac_ctx = self._make_hook_context(
                HookStage.AFTER_CONTEXT,
                context_messages=context_messages,
            )
            context_result = await self.hook_manager.run(
                HookStage.AFTER_CONTEXT,
                ac_ctx,
                short_circuit=True,
            )
            if isinstance(context_result, dict):
                if "context_messages" in context_result:
                    context_messages = context_result["context_messages"]
                elif "messages" in context_result:
                    context_messages = context_result["messages"]
                if "event" in context_result:
                    yield context_result["event"]
                    turn_complete = bool(context_result.get("turn_complete", True))
                    break
            elif context_result is not None:
                yield self._default_hook_rejection_event(HookStage.AFTER_CONTEXT)
                turn_complete = True
                break

            acb_ctx = self._make_hook_context(
                HookStage.AFTER_CONTEXT_BUILD,
                context_messages=context_messages,
            )
            await self.hook_manager.run(
                HookStage.AFTER_CONTEXT_BUILD,
                acb_ctx,
                short_circuit=False,
            )

            # --- agent ---
            ba_ctx = self._make_hook_context(HookStage.BEFORE_AGENT)
            short_circuit = await self.hook_manager.run(
                HookStage.BEFORE_AGENT, ba_ctx, short_circuit=True
            )
            if short_circuit is not None:
                if isinstance(short_circuit, dict) and "messages" in short_circuit:
                    self.messages.extend(short_circuit["messages"])
                turn_complete = True
                break

            # Select and bind tools if available.
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
                    yield pre_schema_result["event"]
                    turn_complete = bool(pre_schema_result.get("turn_complete", True))
                    break
            elif pre_schema_result is not None:
                yield self._default_hook_rejection_event(HookStage.BEFORE_TOOL_SCHEMA_BIND)
                turn_complete = True
                break

            llm_with_tools = self._bind_tools_for_provider(tools)
            model_request = {
                "messages": context_messages,
                "tools": tools,
                "llm": llm_with_tools,
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
                    model_request["llm"] = self._bind_tools_for_provider(model_request["tools"])
                if "llm" in request_result:
                    model_request["llm"] = request_result["llm"]
                if "event" in request_result:
                    yield request_result["event"]
                    turn_complete = bool(request_result.get("turn_complete", True))
                    break
            elif request_result is not None:
                yield self._default_hook_rejection_event(HookStage.BEFORE_MODEL_REQUEST)
                turn_complete = True
                break

            context_messages = model_request["messages"]
            tools = model_request["tools"]
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
            response_msg = Message(
                role="assistant",
                content=content,
                tool_calls=getattr(response, "tool_calls", None) or [],
                usage_metadata=getattr(response, "usage_metadata", None) or {},
                response_metadata=getattr(response, "response_metadata", None) or {},
                additional_kwargs=getattr(response, "additional_kwargs", None) or {},
            )
            self.messages.append(response_msg)

            yield {
                "type": "assistant_message",
                "data": {"content": content, "tool_calls": getattr(response, "tool_calls", None)},
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
            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                turn_complete = True
                break

            # --- tools ---
            bt_ctx = self._make_hook_context(HookStage.BEFORE_TOOLS)
            short_circuit = await self.hook_manager.run(
                HookStage.BEFORE_TOOLS, bt_ctx, short_circuit=True
            )
            if short_circuit is not None:
                # Hook denied tool execution
                break

            # Normalize tool calls
            normalized_calls = self._normalize_tool_calls(tool_calls)
            logger.info(
                "engine.turn tool_calls_parsed turn=%d n=%d names=%s",
                self.turn_count,
                len(normalized_calls),
                [tc.get("name") for tc in normalized_calls],
            )
            parsed_ctx = self._make_hook_context(
                HookStage.ON_TOOL_CALLS_PARSED,
                tool_calls=normalized_calls,
                agent_response=response,
            )
            await self.hook_manager.run(
                HookStage.ON_TOOL_CALLS_PARSED,
                parsed_ctx,
                short_circuit=False,
            )
            yield {
                "type": "tool_calls_started",
                "data": {"tool_calls": normalized_calls},
            }
            tool_names_by_id = {
                str(tc.get("id") or ""): str(tc.get("name") or "tool")
                for tc in normalized_calls
            }

            # Execute tools
            from xbotv2.tools.runtime import execute_tools
            tool_messages = await execute_tools(
                normalized_calls,
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

            # AFTER_TOOLS hooks may redact/cache large outputs before they
            # enter message history or cross the protocol boundary.
            at_ctx = self._make_hook_context(HookStage.AFTER_TOOLS, tool_results=tool_messages)
            tools_result = await self.hook_manager.run(
                HookStage.AFTER_TOOLS, at_ctx, short_circuit=True
            )
            if isinstance(tools_result, dict) and "tool_results" in tools_result:
                tool_messages = tools_result["tool_results"]

            logger.info(
                "engine.turn tool_messages_built turn=%d n=%d ids=%s statuses=%s",
                self.turn_count,
                len(tool_messages),
                [getattr(tm, "tool_call_id", None) for tm in tool_messages],
                [getattr(tm, "status", None) for tm in tool_messages],
            )
            self.messages.extend(tool_messages)
            # Persist immediately after tool messages are committed so
            # that even if the turn is cancelled later in this
            # iteration (during tool_result yield or the next LLM
            # call), the disk is consistent: every assistant message with
            # tool_calls has its matching tool messages on disk.
            await self.save_messages()

            # Yield tool results
            for tm in tool_messages:
                for client_event in getattr(tm, "additional_kwargs", {}).get("xbotv2_events", []):
                    event_ctx = self._make_hook_context(
                        HookStage.ON_CLIENT_EVENT,
                        tool_result=tm,
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
                    "data": {
                        "tool_call_id": tm.tool_call_id,
                        "name": tool_names_by_id.get(str(tm.tool_call_id), "tool"),
                        "content": tm.content,
                        "status": getattr(tm, "status", "success"),
                    },
                }

            # ON_TOOL_MESSAGE hooks
            for tm in tool_messages:
                t_ctx = self._make_hook_context(
                    HookStage.ON_TOOL_MESSAGE, tool_results=[tm]
                )
                await self.hook_manager.run(HookStage.ON_TOOL_MESSAGE, t_ctx, short_circuit=False)

            if any(
                getattr(tm, "additional_kwargs", {}).get("xbotv2_turn_complete")
                for tm in tool_messages
            ):
                turn_complete = True
                break

            if tools_result is not None:
                if isinstance(tools_result, dict):
                    if "event" in tools_result:
                        yield tools_result["event"]
                    turn_complete = bool(tools_result.get("turn_complete", True))
                else:
                    turn_complete = True
                if turn_complete:
                    break

        stop_reason = "completed" if turn_complete else "max_iterations"

        # 5. ON_TURN_END hook
        te_ctx = self._make_hook_context(HookStage.ON_TURN_END)
        await self.hook_manager.run(HookStage.ON_TURN_END, te_ctx, short_circuit=False)

        stop_ctx = self._make_hook_context(HookStage.ON_STOP, stop_reason=stop_reason)
        try:
            await self.hook_manager.run(HookStage.ON_STOP, stop_ctx, short_circuit=False)
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

        yield {"type": "turn_finished", "data": {"turn": self.turn_count}}

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

        aggregate: XBotModelResponse | None = None
        tool_stream_ids: dict[int, str] = {}
        async with asyncio.timeout(_LLM_DISPATCH_TIMEOUT):
            async for chunk in llm.astream(context_messages):
                if isinstance(chunk, XBotModelChunk):
                    aggregate = merge_xbot_chunk(aggregate, chunk)
                    if chunk.content:
                        is_reasoning = bool(
                            (chunk.additional_kwargs or {}).get("reasoning_content")
                        )
                        # Tag reasoning vs content so the TUI can
                        # render them separately and never mix them
                        # into the same message.
                        if is_reasoning:
                            yield {
                                "type": "assistant_message_delta",
                                "data": {"reasoning": chunk.content},
                            }
                        else:
                            yield {
                                "type": "assistant_message_delta",
                                "data": {"content": chunk.content},
                            }
                    for tool_delta in xbot_tool_call_deltas(chunk, tool_stream_ids):
                        yield {"type": "tool_call_delta", "data": tool_delta}
                    continue
                if isinstance(chunk, XBotModelResponse):
                    aggregate = chunk
                    continue
                logger.warning("_stream_model_response: unexpected chunk type %s", type(chunk).__name__)

        if aggregate is None:
            raise RuntimeError("LLM stream produced no chunks")
        yield {"type": "_model_response", "data": {"response": aggregate}}

    async def _handle_compaction(self, short_circuit: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(short_circuit, dict) or "messages" not in short_circuit:
            return self._default_hook_rejection_event(HookStage.BEFORE_CONTEXT)

        compact_reason = str(short_circuit.get("compact_reason", "before_context"))
        pre_compact_ctx = self._make_hook_context(HookStage.PRE_COMPACT, compact_reason=compact_reason)
        pre_compact_result = await self.hook_manager.run(HookStage.PRE_COMPACT, pre_compact_ctx, short_circuit=True)
        if isinstance(pre_compact_result, dict):
            if "messages" in pre_compact_result:
                short_circuit["messages"] = pre_compact_result["messages"]
            if "compact_reason" in pre_compact_result:
                compact_reason = str(pre_compact_result["compact_reason"])
        elif pre_compact_result is not None:
            return self._default_hook_rejection_event(HookStage.PRE_COMPACT)

        previous_message_count = len(self.messages)
        self.messages = short_circuit["messages"]
        post_compact_ctx = self._make_hook_context(HookStage.POST_COMPACT, compact_reason=compact_reason)
        post_compact_ctx.state.update({"previous_message_count": previous_message_count, "current_message_count": len(self.messages)})
        await self.hook_manager.run(HookStage.POST_COMPACT, post_compact_ctx, short_circuit=False)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------



    async def save_messages(self) -> None:
        """Persist messages after each turn. Runs BEFORE/AFTER_STATE_PERSIST hooks."""
        before_ctx = self._make_hook_context(HookStage.BEFORE_STATE_PERSIST)
        await self.hook_manager.run(HookStage.BEFORE_STATE_PERSIST, before_ctx, short_circuit=False)
        self.state_store.replace_messages(self.messages)
        after_ctx = self._make_hook_context(HookStage.AFTER_STATE_PERSIST)
        await self.hook_manager.run(HookStage.AFTER_STATE_PERSIST, after_ctx, short_circuit=False)

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
                raise InteractionDisconnected(f"Client disconnected while waiting for {sink_result.get('request_id')}")
            if on_sink_result:
                on_sink_result(sink_result)
            return sink_result
        request_id = str((client_event.get("data") or {}).get("request_id") or "")
        wait_timeout = 0 if timeout_seconds is None else timeout_seconds
        result = await waiter.wait(request_id, wait_timeout)
        return {f: getattr(result, f, "") for f in result_fields} | {"request_id": result.request_id, "status": result.status, "reason": result.reason}

    async def _handle_user_input_request(self, client_event: dict[str, Any], *, timeout_seconds: float | None = None, tool_call_id: str = "") -> dict[str, Any]:
        return await self._handle_client_interaction(client_event, self.user_input_waiter, ("answer",), timeout_seconds=timeout_seconds)

    async def _handle_permission_request(self, client_event: dict[str, Any], *, timeout_seconds: float | None = None, tool_call_id: str = "") -> dict[str, Any]:
        return await self._handle_client_interaction(
            client_event, self.permission_waiter, ("decision", "scope"),
            timeout_seconds=timeout_seconds,
            on_sink_result=lambda r: self.persist_permission_if_session_scope(client_event, r),
        )

    def persist_permission_if_session_scope(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if str(result.get("scope") or "once") != "session":
            return
        try:
            from pathlib import Path
            from xbotv2.config.policy import persist_permission_decision
            persist_permission_decision(
                config_dir=Path(getattr(self.state_store, "root", "data")).parent.parent,
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
        context_components: list[Any] | None = None,
        context_messages: list[Any] | None = None,
        agent_response: Any = None,
        model_request: dict[str, Any] | None = None,
        model_response: Any = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call: dict[str, Any] | None = None,
        tool_results: list[Any] | None = None,
        tool_result: Any = None,
        stop_reason: str | None = None,
        compact_reason: str | None = None,
        permission_decision: str | None = None,
        client_event: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> HookContext:
        return HookContext(
            stage=stage,
            state={"messages": self.messages},
            config=self.config,
            tools=self.tool_registry,
            plugin_store=None,
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
        )

    @staticmethod
    def _normalize_tool_calls(tool_calls: list[Any]) -> list[dict[str, Any]]:
        result = []
        for i, tc in enumerate(tool_calls):
            if isinstance(tc, dict):
                result.append({
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "id": tc.get("id", f"call_{i}"),
                })
            else:
                result.append({
                    "name": getattr(tc, "name", ""),
                    "args": getattr(tc, "args", {}),
                    "id": getattr(tc, "id", f"call_{i}"),
                })
        return result

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
        }
