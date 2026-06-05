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
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from xbotv2.core.interactions import (
    InteractionDisconnected,
    InteractionResult,
    InteractionWaiter,
)
from xbotv2.core.state import SessionInfo
from xbotv2.core.workspace import SessionWorkspace
from xbotv2.hooks.types import HookContext, HookStage

logger = logging.getLogger("xbotv2.engine")


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
        llm: Any,  # BaseChatModel
        tool_registry: Any,  # ToolRegistry
        hook_manager: Any,  # HookManager
        state_store: Any,  # CoreStateStore
        context_builder: Any,  # ContextBuilder
        sandbox_policy: Any,  # SandboxPolicy
        permission_system: Any,  # PermissionSystem
        config: Any,  # AgentConfig
        workspace: SessionWorkspace | None = None,
        max_iterations: int = 50,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.hook_manager = hook_manager
        self.state_store = state_store
        self.context_builder = context_builder
        self.sandbox_policy = sandbox_policy
        self.permission_system = permission_system
        self.config = config
        self.workspace = workspace
        self.max_iterations = max_iterations

        # Runtime state (per-session, in-memory)
        self._messages: list[BaseMessage] = []
        self._session: SessionInfo | None = None
        self._turn_count = 0
        self._user_input_waiter = InteractionWaiter()
        self._permission_waiter = InteractionWaiter()
        self._client_event_sink: Any | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self) -> None:
        """Create a new session. Runs ON_SESSION_START hooks.

        If previous persisted state exists on disk, message history and turn
        count are loaded and ON_SESSION_RESUME hooks run.
        """
        self._session = SessionInfo(
            session_id=self.state_store.session_id,
            thread_id=self.state_store.thread_id,
            personality_id=self.state_store.personality_id,
        )

        existing_session = self.state_store.has_existing_session()
        self._ensure_workspace("resume" if existing_session else "start")

        if existing_session:
            self._messages = self.state_store.read_messages()
            self._turn_count = self.state_store.read_state().get("turn_count", 0)
            self._session.turn_count = self._turn_count
            ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
            await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)
        else:
            ctx = self._make_hook_context(HookStage.ON_SESSION_START)
            await self.hook_manager.run(HookStage.ON_SESSION_START, ctx, short_circuit=False)

    async def resume_session(self) -> None:
        """Explicit resume: load persisted state and run ON_SESSION_RESUME hooks."""
        state = self.state_store.read_state()
        self._turn_count = state.get("turn_count", 0)

        # Restore message history from disk
        self._messages = self.state_store.read_messages()

        self._session = SessionInfo(
            session_id=self.state_store.session_id,
            thread_id=self.state_store.thread_id,
            personality_id=self.state_store.personality_id,
            turn_count=self._turn_count,
        )

        self._ensure_workspace("explicit_resume")

        ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
        await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)

    async def close_session(self) -> None:
        """Execute ON_SESSION_CLOSE hooks. Messages remain persisted on disk."""
        self.cancel_pending_user_inputs("session_closed")
        self.cancel_pending_permissions("session_closed")
        self.state_store.append_event("session_closed", {"turn_count": self._turn_count})
        ctx = self._make_hook_context(HookStage.ON_SESSION_CLOSE)
        await self.hook_manager.run(HookStage.ON_SESSION_CLOSE, ctx, short_circuit=False)
        await self._save_messages()

    def set_client_event_sink(self, sink: Any | None) -> Any | None:
        """Install a live protocol sink for client-directed events."""
        previous = self._client_event_sink
        self._client_event_sink = sink
        return previous

    def submit_user_input(self, request_id: str, answer: Any) -> InteractionResult:
        """Answer a live ask_user request owned by this engine."""
        return self._user_input_waiter.answer(request_id, answer=answer)

    def cancel_user_input(self, request_id: str, reason: str = "cancelled") -> InteractionResult:
        """Cancel one live ask_user request."""
        return self._user_input_waiter.cancel(request_id, reason)

    def cancel_pending_user_inputs(self, reason: str = "cancelled") -> list[InteractionResult]:
        """Cancel all live ask_user requests."""
        return self._user_input_waiter.cancel_all(reason)

    def submit_permission_response(
        self,
        request_id: str,
        decision: str,
    ) -> InteractionResult:
        """Answer a live permission request owned by this engine."""
        return self._permission_waiter.answer(request_id, decision=decision)

    def cancel_pending_permissions(self, reason: str = "cancelled") -> list[InteractionResult]:
        """Cancel all live permission requests."""
        return self._permission_waiter.cancel_all(reason)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """Execute one user turn and emit ON_ERROR on failures."""
        try:
            async for event in self._run_turn_impl(user_input):
                yield event
        except asyncio.CancelledError:
            # The TUI pressed ESC (or the HTTP client disconnected
            # mid-turn). Emit a structured turn_cancelled so the
            # transcript shows what happened, then re-raise so the
            # caller's task cancellation propagates.
            logger.info("Turn %s interrupted by client", self._turn_count)
            self.state_store.append_event(
                "turn_cancelled",
                {
                    "turn": self._turn_count,
                    "reason": "client_interrupt",
                },
            )
            await self._save_messages()
            yield {
                "type": "turn_cancelled",
                "data": {
                    "turn": self._turn_count,
                    "reason": "client_interrupt",
                },
            }
            raise
        except InteractionDisconnected as exc:
            logger.info("Turn stopped because the client disconnected during an interaction")
            self.state_store.append_event(
                "turn_cancelled",
                {
                    "turn": self._turn_count,
                    "reason": "client_disconnected",
                    "message": str(exc),
                },
            )
            await self._save_messages()
        except Exception as exc:
            logger.exception("Turn failed")
            self.state_store.append_event(
                "error",
                {
                    "turn": self._turn_count,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            failure_ctx = self._make_hook_context(
                HookStage.ON_STOP_FAILURE,
                user_input=user_input,
                stop_reason="error",
                error=exc,
            )
            await self.hook_manager.run(
                HookStage.ON_STOP_FAILURE,
                failure_ctx,
                short_circuit=False,
            )
            ctx = self._make_hook_context(
                HookStage.ON_ERROR,
                user_input=user_input,
                error=exc,
            )
            await self.hook_manager.run(HookStage.ON_ERROR, ctx, short_circuit=False)
            await self._save_messages()
            yield {
                "type": "error",
                "data": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                },
            }

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

        self._turn_count += 1

        # 1. Record user message
        self._messages.append(HumanMessage(content=user_input))

        accepted_ctx = self._make_hook_context(
            HookStage.AFTER_USER_MESSAGE_ACCEPT,
            user_input=user_input,
        )
        await self.hook_manager.run(
            HookStage.AFTER_USER_MESSAGE_ACCEPT,
            accepted_ctx,
            short_circuit=False,
        )

        # 2. ON_USER_MESSAGE hook
        um_ctx = self._make_hook_context(HookStage.ON_USER_MESSAGE, user_input=user_input)
        await self.hook_manager.run(HookStage.ON_USER_MESSAGE, um_ctx, short_circuit=False)

        # 3. ON_TURN_START hook
        ts_ctx = self._make_hook_context(HookStage.ON_TURN_START, user_input=user_input)
        await self.hook_manager.run(HookStage.ON_TURN_START, ts_ctx, short_circuit=False)

        self.state_store.append_event("turn_started", {"turn": self._turn_count})
        yield {"type": "turn_started", "data": {"turn": self._turn_count}}

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
                # Hook short-circuited — could be compaction replacing messages
                if isinstance(short_circuit, dict) and "messages" in short_circuit:
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
                        yield self._default_hook_rejection_event(HookStage.PRE_COMPACT)
                        turn_complete = True
                        break

                    previous_message_count = len(self._messages)
                    self._messages = short_circuit["messages"]
                    post_compact_ctx = self._make_hook_context(
                        HookStage.POST_COMPACT,
                        compact_reason=compact_reason,
                    )
                    post_compact_ctx.state.update({
                        "previous_message_count": previous_message_count,
                        "current_message_count": len(self._messages),
                    })
                    await self.hook_manager.run(
                        HookStage.POST_COMPACT,
                        post_compact_ctx,
                        short_circuit=False,
                    )
                elif not isinstance(short_circuit, dict):
                    yield self._default_hook_rejection_event(HookStage.BEFORE_CONTEXT)
                    turn_complete = True
                    break

            context_kwargs = {
                "messages": self._messages,
                "agent_name": getattr(self.config, "agent_name", "XBotv2"),
                "agent_role": getattr(self.config, "agent_role", ""),
                "user_name": "User",
                "user_id": "default-user",
                "instructions": getattr(self.config, "instructions", ""),
                "memory": getattr(self.config, "memory", ""),
                "sandbox_summary": self.sandbox_policy.describe() if self.sandbox_policy else "",
                "turn_count": self._turn_count,
            }
            bcb_ctx = self._make_hook_context(HookStage.BEFORE_CONTEXT_BUILD)
            build_result = await self.hook_manager.run(
                HookStage.BEFORE_CONTEXT_BUILD,
                bcb_ctx,
                short_circuit=True,
            )
            if isinstance(build_result, dict):
                if "messages" in build_result:
                    self._messages = build_result["messages"]
                    context_kwargs["messages"] = self._messages
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
                if isinstance(component_result, dict) and "context_components" in component_result:
                    context_components = component_result["context_components"]

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
                    self._messages.extend(short_circuit["messages"])
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

            try:
                llm_with_tools = self.llm.bind_tools(tools) if tools else self.llm
            except NotImplementedError:
                llm_with_tools = self.llm
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
                    try:
                        model_request["llm"] = (
                            self.llm.bind_tools(model_request["tools"])
                            if model_request["tools"]
                            else self.llm
                        )
                    except NotImplementedError:
                        model_request["llm"] = self.llm
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
                response = await llm_with_tools.ainvoke(context_messages)
            except Exception as exc:
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
            self._messages.append(response)

            # Yield assistant message
            content = response.content if hasattr(response, "content") else str(response)
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
                        self._messages.extend(agent_result["messages"])
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
                    if self._client_event_sink is not None
                    else None
                ),
                permission_interaction_handler=(
                    self._handle_permission_request
                    if self._client_event_sink is not None
                    else None
                ),
            )

            # AFTER_TOOLS hooks may redact/cache large outputs before they
            # enter message history or cross the protocol boundary.
            at_ctx = self._make_hook_context(HookStage.AFTER_TOOLS, tool_results=tool_messages)
            tools_result = await self.hook_manager.run(
                HookStage.AFTER_TOOLS, at_ctx, short_circuit=True
            )
            if isinstance(tools_result, dict) and "tool_results" in tools_result:
                tool_messages = tools_result["tool_results"]

            self._messages.extend(tool_messages)

            # Yield tool results
            for tm in tool_messages:
                for client_event in getattr(tm, "additional_kwargs", {}).get("xbotv2_events", []):
                    await self._record_client_event(client_event, tool_result=tm)
                    user_input_result = getattr(tm, "additional_kwargs", {}).get(
                        "xbotv2_user_input_result"
                    )
                    if (
                        client_event.get("type") == "user_input_required"
                        and isinstance(user_input_result, dict)
                    ):
                        self.record_user_input_result(client_event, user_input_result)
                    yield client_event

                yield {
                    "type": "tool_result",
                    "data": {
                        "tool_call_id": tm.tool_call_id,
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
        except Exception as exc:
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

        self.state_store.append_event("turn_finished", {"turn": self._turn_count})

        # Persist all messages to disk after each turn
        await self._save_messages()

        yield {"type": "turn_finished", "data": {"turn": self._turn_count}}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _save_messages(self) -> None:
        """Persist messages and materialize state after each turn.

        Uses truncate-then-append to keep the message log in sync with
        the current message list (compaction may remove old messages).
        Also materializes state.yaml so turn_count et al. are current.
        """
        before_ctx = self._make_hook_context(HookStage.BEFORE_STATE_PERSIST)
        await self.hook_manager.run(HookStage.BEFORE_STATE_PERSIST, before_ctx, short_circuit=False)
        self.state_store.replace_messages(self._messages)
        self.state_store.materialize()
        after_ctx = self._make_hook_context(HookStage.AFTER_STATE_PERSIST)
        await self.hook_manager.run(HookStage.AFTER_STATE_PERSIST, after_ctx, short_circuit=False)

    def _ensure_workspace(self, lifecycle: str) -> None:
        """Ensure the session workspace exists before lifecycle hooks run."""
        if self.workspace is None:
            return
        status = self.workspace.ensure(lifecycle)  # type: ignore[arg-type]
        self.state_store.append_event(status.event_type(), status.to_event_payload())
        self.state_store.materialize()

    async def _handle_user_input_request(
        self,
        client_event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """Emit a user-input request and wait for the live client answer."""
        await self._record_client_event(client_event)
        sink_result: dict[str, Any] | None = None
        if self._client_event_sink is not None:
            sink_result = await self._client_event_sink(
                client_event,
                timeout_seconds=timeout_seconds,
                tool_call_id=tool_call_id,
            )
            if sink_result.get("status") == "disconnected":
                if not sink_result.get("persisted"):
                    self.record_user_input_result(client_event, sink_result)
                raise InteractionDisconnected(
                    f"Client disconnected while waiting for {sink_result.get('request_id')}"
                )
        if sink_result is None:
            request_id = str((client_event.get("data") or {}).get("request_id") or "")
            wait_timeout = 0 if timeout_seconds is None else timeout_seconds
            result = await self._user_input_waiter.wait(request_id, wait_timeout)
            sink_result = {
                "request_id": result.request_id,
                "status": result.status,
                "answer": result.answer,
                "reason": result.reason,
            }
        if not sink_result.get("persisted"):
            self.record_user_input_result(client_event, sink_result)
        return sink_result

    async def _handle_permission_request(
        self,
        client_event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """Emit a permission request and wait for the live client decision."""
        await self._record_client_event(client_event)
        sink_result: dict[str, Any] | None = None
        if self._client_event_sink is not None:
            sink_result = await self._client_event_sink(
                client_event,
                timeout_seconds=timeout_seconds,
                tool_call_id=tool_call_id,
            )
            if sink_result.get("status") == "disconnected":
                if not sink_result.get("persisted"):
                    self.record_permission_result(client_event, sink_result)
                raise InteractionDisconnected(
                    f"Client disconnected while waiting for {sink_result.get('request_id')}"
                )
        if sink_result is None:
            request_id = str((client_event.get("data") or {}).get("request_id") or "")
            wait_timeout = 0 if timeout_seconds is None else timeout_seconds
            result = await self._permission_waiter.wait(request_id, wait_timeout)
            sink_result = {
                "request_id": result.request_id,
                "status": result.status,
                "decision": result.decision,
                "reason": result.reason,
            }
        if not sink_result.get("persisted"):
            self.record_permission_result(client_event, sink_result)
        return sink_result

    def record_user_input_result(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a live ask_user completion and refresh materialized state."""
        self._record_user_input_completion(client_event, result)
        return self.state_store.materialize()

    def record_permission_result(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a live permission completion and refresh materialized state."""
        self._record_permission_completion(client_event, result)
        return self.state_store.materialize()

    async def _record_client_event(
        self,
        client_event: dict[str, Any],
        *,
        tool_result: Any = None,
    ) -> None:
        event_ctx = self._make_hook_context(
            HookStage.ON_CLIENT_EVENT,
            tool_result=tool_result,
            client_event=client_event,
        )
        await self.hook_manager.run(
            HookStage.ON_CLIENT_EVENT,
            event_ctx,
            short_circuit=False,
        )
        self.state_store.append_event(
            client_event.get("type", "client_event"),
            client_event.get("data", {}),
        )

    def _record_user_input_completion(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        data = client_event.get("data") or {}
        request_id = str(result.get("request_id") or data.get("request_id") or "")
        status = str(result.get("status") or "cancelled")
        payload = {
            "request_id": request_id,
            "request_type": "user_input_required",
            "request_source": data.get("source", "ask_user"),
            "request_payload": data,
        }
        if status == "answered":
            self.state_store.append_event("user_input_response", {
                **payload,
                "answer": result.get("answer", ""),
            })
            return
        self.state_store.append_event("user_input_cancelled", {
            **payload,
            "status": status,
            "reason": result.get("reason") or status,
        })

    def _record_permission_completion(
        self,
        client_event: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        data = client_event.get("data") or {}
        request_id = str(result.get("request_id") or data.get("request_id") or "")
        status = str(result.get("status") or "cancelled")
        payload = {
            "request_id": request_id,
            "request_type": "permission_request",
            "request_source": data.get("source", "permission_system"),
            "request_payload": data,
        }
        if status == "answered":
            self.state_store.append_event("permission_response", {
                **payload,
                "decision": result.get("decision", ""),
                "scope": result.get("scope", "once"),
            })
            return
        self.state_store.append_event("permission_cancelled", {
            **payload,
            "status": status,
            "reason": result.get("reason") or status,
        })

    def _restore_messages(self) -> int:
        """Load messages from disk into memory. Returns count loaded."""
        self._messages = self.state_store.read_messages()
        return len(self._messages)

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
        """Build a HookContext for the current engine state."""
        return HookContext(
            stage=stage,
            state={"messages": self._messages},
            config=self.config,
            tools=self.tool_registry,
            plugin_store=None,  # Plugins use their own store reference
            session=self._session or SessionInfo(
                session_id=self.state_store.session_id,
                thread_id=self.state_store.thread_id,
                personality_id=self.state_store.personality_id,
                turn_count=self._turn_count,
            ),
            emit=lambda e: self.state_store.append_event("hook_event", e),
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
        """Normalize tool calls from various formats to a standard dict."""
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
        """Extract provider token usage from LangChain response metadata."""
        for candidate in (
            getattr(response, "usage_metadata", None),
            getattr(response, "response_metadata", None),
            getattr(response, "additional_kwargs", None),
        ):
            usage = Engine._normalize_usage(candidate)
            if usage:
                return usage
        return None

    @staticmethod
    def _normalize_usage(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        for nested_key in ("token_usage", "usage"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                usage = Engine._normalize_usage(nested)
                if usage:
                    return usage

        input_tokens = value.get("input_tokens", value.get("prompt_tokens"))
        output_tokens = value.get("output_tokens", value.get("completion_tokens"))
        total_tokens = value.get("total_tokens")
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return None

        input_count = int(input_tokens or 0)
        output_count = int(output_tokens or 0)
        total_count = int(total_tokens or input_count + output_count)
        return {
            "input_tokens": input_count,
            "output_tokens": output_count,
            "total_tokens": total_count,
            "requests": int(value.get("requests") or 1),
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[BaseMessage]:
        """Current message history."""
        return list(self._messages)

    @property
    def turn_count(self) -> int:
        """Current turn count."""
        return self._turn_count
