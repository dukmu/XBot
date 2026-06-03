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

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from xbotv2.core.state import SessionInfo
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
        self.max_iterations = max_iterations

        # Runtime state (per-session, in-memory)
        self._messages: list[BaseMessage] = []
        self._session: SessionInfo | None = None
        self._turn_count = 0

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self) -> None:
        """Create a new session. Runs ON_SESSION_START hooks.

        If previous message history exists on disk, it is loaded
        (this session is a resume from persisted state).
        """
        self._session = SessionInfo(
            session_id=self.state_store.session_id,
            thread_id=self.state_store.thread_id,
            personality_id=self.state_store.personality_id,
        )

        # Restore persisted messages if any exist
        if self.state_store.message_count() > 0:
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

        ctx = self._make_hook_context(HookStage.ON_SESSION_RESUME)
        await self.hook_manager.run(HookStage.ON_SESSION_RESUME, ctx, short_circuit=False)

    async def close_session(self) -> None:
        """Execute ON_SESSION_CLOSE hooks. Messages remain persisted on disk."""
        self.state_store.append_event("session_closed", {"turn_count": self._turn_count})
        self._save_messages()
        ctx = self._make_hook_context(HookStage.ON_SESSION_CLOSE)
        await self.hook_manager.run(HookStage.ON_SESSION_CLOSE, ctx, short_circuit=False)

    async def close_session(self) -> None:
        """Execute ON_SESSION_CLOSE hooks."""
        self.state_store.append_event("session_closed", {"turn_count": self._turn_count})
        ctx = self._make_hook_context(HookStage.ON_SESSION_CLOSE)
        await self.hook_manager.run(HookStage.ON_SESSION_CLOSE, ctx, short_circuit=False)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """Execute one user turn through the ReAct loop.

        Yields event dicts: {"type": str, "data": {...}}
        """
        self._turn_count += 1

        # 1. Record user message
        self._messages.append(HumanMessage(content=user_input))

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
                    self._messages = short_circuit["messages"]

            # Build context messages
            context_messages = self.context_builder.build(
                messages=self._messages,
                agent_name=getattr(self.config, "agent_name", "XBotv2"),
                agent_role=getattr(self.config, "agent_role", ""),
                user_name="User",
                user_id="default-user",
                instructions=getattr(self.config, "instructions", ""),
                memory=getattr(self.config, "memory", ""),
                sandbox_summary=self.sandbox_policy.describe() if self.sandbox_policy else "",
                turn_count=self._turn_count,
            )

            ac_ctx = self._make_hook_context(HookStage.AFTER_CONTEXT)
            await self.hook_manager.run(HookStage.AFTER_CONTEXT, ac_ctx, short_circuit=False)

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

            # Call LLM (bind tools if available)
            tools = self.tool_registry.get_all()
            try:
                llm_with_tools = self.llm.bind_tools(tools) if tools else self.llm
            except NotImplementedError:
                llm_with_tools = self.llm
            response = await llm_with_tools.ainvoke(context_messages)
            self._messages.append(response)

            # Yield assistant message
            content = response.content if hasattr(response, "content") else str(response)
            yield {
                "type": "assistant_message",
                "data": {"content": content, "tool_calls": getattr(response, "tool_calls", None)},
            }

            # ON_ASSISTANT_MESSAGE hook
            am_ctx = self._make_hook_context(
                HookStage.ON_ASSISTANT_MESSAGE, agent_response=response
            )
            await self.hook_manager.run(HookStage.ON_ASSISTANT_MESSAGE, am_ctx, short_circuit=False)

            # AFTER_AGENT hook
            aa_ctx = self._make_hook_context(HookStage.AFTER_AGENT, agent_response=response)
            await self.hook_manager.run(HookStage.AFTER_AGENT, aa_ctx, short_circuit=False)

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
            )

            # AFTER_TOOLS hooks may redact/cache large outputs before they
            # enter message history or cross the protocol boundary.
            at_ctx = self._make_hook_context(HookStage.AFTER_TOOLS, tool_results=tool_messages)
            await self.hook_manager.run(HookStage.AFTER_TOOLS, at_ctx, short_circuit=False)

            self._messages.extend(tool_messages)

            # Yield tool results
            for tm in tool_messages:
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

        # 5. ON_TURN_END hook
        te_ctx = self._make_hook_context(HookStage.ON_TURN_END)
        await self.hook_manager.run(HookStage.ON_TURN_END, te_ctx, short_circuit=False)

        self.state_store.append_event("turn_finished", {"turn": self._turn_count})

        # Persist all messages to disk after each turn
        self._save_messages()

        yield {"type": "turn_finished", "data": {"turn": self._turn_count}}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_messages(self) -> None:
        """Persist messages and materialize state after each turn.

        Uses truncate-then-append to keep the message log in sync with
        the current message list (compaction may remove old messages).
        Also materializes state.yaml so turn_count et al. are current.
        """
        self.state_store.clear_messages()
        self.state_store.append_messages(self._messages)
        self.state_store.materialize()

    def _restore_messages(self) -> int:
        """Load messages from disk into memory. Returns count loaded."""
        self._messages = self.state_store.read_messages()
        return len(self._messages)

    def _make_hook_context(
        self,
        stage: HookStage,
        *,
        user_input: str | None = None,
        agent_response: Any = None,
        tool_results: list[Any] | None = None,
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
            agent_response=agent_response,
            tool_results=tool_results,
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
