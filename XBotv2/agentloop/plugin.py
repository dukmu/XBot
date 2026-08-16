"""Agent loop component: assembles the :class:`Engine` from XCore services.

The agent loop (DSH's ``dsh-agent-loop``) is a plugin mounted last: its
``apply`` resolves the selected Agent, creates the LLM client through the
``ctx.llm`` service, dispatches ``SESSION_INIT``, applies the tool filter,
builds the :class:`Engine` from the context's services, and provides it as
``ctx.engine`` (the main agent instance).  The turn loop therefore runs on an
XCore context: extension points are XCore events, tools/state/config are
services.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from XBotv2.agentloop.agents import (
    _restore_agent_definition,
    apply_agent_definition,
    apply_agent_provider,
    apply_agent_tools,
)
from XBotv2.agentloop.engine import DEFAULT_MAX_ITERATIONS, Engine
from XBotv2.core.agents import AgentDefinition
from XBotv2.core.events import EventContext, Events
from XBotv2.core.runtime import SessionInfo
from XBotv2.permissions.system import PermissionIntersection, PermissionSystem

SUBAGENT_FORBIDDEN_TOOLS = frozenset({
    "spawn_subagent",
    "list_subagents",
    "wait_subagent",
    "read_subagent",
    "cancel_subagent",
})


class AgentLoopComponent:
    inject = ['agents', 'tools', 'sandbox', 'permissions', 'jobs', 'loader', 'llm', 'settings', 'state_store', 'session', 'context_builder', 'commands', 'prompts', 'paths', 'variables']
    """Build the runtime Engine from the context's services.

    Configured by the plugin tree entry (``xbot.agentloop``): reads the engine
    parameters from its config, the tool layer / runtime info from services,
    and provides the Engine as ``ctx.engine``.
    """

    name = "xbot.agentloop"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        self._session_id = config["session_id"]
        self._thread_id = config["thread_id"]
        self._workspace_root = str(config["workspace_root"])
        self._provider_name = config["provider_name"]
        self._agent_definition = config.get("agent_definition")
        self._llm_override = config.get("llm_override")
        self._selected_agent = config.get("selected_agent")
        self._parent_permission_system = config.get("parent_permission_system")
        self._parent_thread_id = config.get("parent_thread_id", "")
        self._is_subagent = bool(config.get("is_subagent", False))
        self._interactive = bool(config.get("interactive", True))
        self._tree_config = config
        await self._assemble(ctx)

    async def _assemble(self, ctx: Any) -> None:
        agent_registry = ctx.agents.registry
        state_store = ctx.state_store
        runtime_variables = ctx.variables
        tool_registry = ctx.tools.registry
        sandbox = ctx.sandbox
        job_registry = ctx.jobs
        plugin_loader = ctx.loader
        permissions = ctx.permissions
        context_builder = ctx.context_builder

        from XBotv2.config.models import (
            PermissionConfig,
            RuntimeConfig,
            SandboxConfig,
        )

        cfg = self._tree_config
        provider_name = self._provider_name
        if provider_name == "default":
            provider_name, _names = ctx.settings.provider_names()
        self._user_context = ctx.settings.user_context()
        memory = cfg.get("memory", "")
        if not memory:
            memory_file = ctx.paths.memory_file
            if memory_file.exists():
                memory = memory_file.read_text(encoding="utf-8")
        agent_config = RuntimeConfig(
            provider=provider_name,
            instructions=cfg.get("instructions", ""),
            memory=memory,
            tools=cfg.get("tools"),
            permissions=PermissionConfig.model_validate(
                ctx.permissions.config or {}
            ),
            sandbox=SandboxConfig.model_validate(ctx.sandbox.config or {}),
        )
        resolved_agent = self._agent_definition
        selected_agent = self._selected_agent
        is_subagent = self._is_subagent
        parent_permission_system = self._parent_permission_system

        # Thread metadata recovery (the agent loop owns its own resume state):
        # restore the stored agent definition, validate the requested agent
        # against the thread owner, and default the selection to the stored
        # agent — no composition-root pre-initialization.
        metadata = state_store.read_thread_metadata()
        stored_agent = str(metadata.get("agent") or "") or None
        stored_provider = str(metadata.get("provider") or "") or None
        stored_definition = metadata.get("agent_definition")
        if resolved_agent is None and isinstance(stored_definition, dict):
            resolved_agent = _restore_agent_definition(stored_definition)
        if (
            selected_agent is not None
            and stored_agent is not None
            and selected_agent != stored_agent
        ):
            raise ValueError(
                f"Thread {self._thread_id!r} belongs to Agent {stored_agent!r}, "
                f"not {selected_agent!r}"
            )
        if selected_agent is None and self._agent_definition is None:
            selected_agent = stored_agent

        if selected_agent is None and resolved_agent is None:
            default_agent = agent_registry.get("default")
            if default_agent is not None and default_agent.mode != "subagent":
                selected_agent = default_agent.name

        if selected_agent is not None:
            registered_agent = agent_registry.get(selected_agent)
            if resolved_agent is None:
                if registered_agent is None or (
                    registered_agent.mode == "subagent" and not is_subagent
                ):
                    raise ValueError(f"Unknown primary agent: {selected_agent}")
                resolved_agent = registered_agent
            elif resolved_agent.name != selected_agent:
                raise ValueError(
                    f"Stored Agent {resolved_agent.name!r} does not match "
                    f"{selected_agent!r}"
                )
            elif resolved_agent.mode == "subagent" and not is_subagent:
                raise ValueError(f"Unknown primary agent: {selected_agent}")
        # Apply the resolved Agent (selected or an explicit subagent
        # definition) to the base config / permissions / provider default —
        # the agent loop owns this, not the composition root.
        if resolved_agent is not None:
            apply_agent_definition(agent_config, resolved_agent)
            provider_name = resolved_agent.provider or provider_name
            permissions = PermissionSystem(
                agent_config.permissions,
                variables=runtime_variables,
            )
            if parent_permission_system is not None:
                permissions = PermissionIntersection(
                    parent_permission_system, permissions
                )

        if stored_provider is not None:
            provider_name = stored_provider
        state_store.provider = provider_name
        agent_config.provider = provider_name
        provider_config = ctx.settings.provider_config(provider_name)
        if resolved_agent is not None:
            apply_agent_provider(provider_config, resolved_agent)
        agent_config.max_context_tokens = (
            resolved_agent.context_window
            if resolved_agent is not None
            and resolved_agent.context_window is not None
            else provider_config.max_context_tokens
        )
        agent_config.max_output_tokens = provider_config.max_output_tokens
        state_store.write_thread_metadata({
            "agent": resolved_agent.name if resolved_agent is not None else "",
            "agent_definition": (
                asdict(resolved_agent) if resolved_agent is not None else None
            ),
            "provider": provider_name,
            "parent_thread_id": self._parent_thread_id,
            "workspace_root": self._workspace_root,
            "model": provider_config.model,
            "model_mode": provider_config.model_mode,
            "context_window": agent_config.max_context_tokens,
        })

        if self._llm_override is not None:
            llm = self._llm_override
        else:
            llm = ctx.llm.create(
                provider_config,
                media_root=str(state_store.root),
            )

        # Dispatch SESSION_INIT (plugins discover skills/MCP tools here).
        init_ctx = EventContext(
            config=agent_config,
            tools=tool_registry,
            sandbox=sandbox,
            session=SessionInfo(
                session_id=self._session_id,
                thread_id=self._thread_id,
                workspace_root=self._workspace_root,
                provider=provider_name,
            ),
            emit=lambda e: None,
        )
        await ctx.emit(Events.SESSION_INIT, init_ctx)

        # Apply tool filter AFTER session init so plugin-discovered tools
        # (skills, MCP) are registered before restrict runs.
        if resolved_agent is not None:
            apply_agent_tools(tool_registry, agent_config, resolved_agent)
        elif agent_config.tools:
            tool_registry.restrict(agent_config.tools)
        if is_subagent:
            for tool_name in SUBAGENT_FORBIDDEN_TOOLS:
                entry = tool_registry.get(tool_name)
                if entry is not None:
                    tool_registry.unregister(entry.registered_name)

        engine = Engine(
            llm=llm,
            tool_registry=tool_registry,
            plugin_ctx=ctx,
            state_store=state_store,
            context_builder=context_builder,
            sandbox_policy=sandbox,
            permission_system=permissions,
            workspace_root=self._workspace_root,
            config=agent_config,
            plugin_loader=plugin_loader,
            job_registry=job_registry,
            agent_registry=agent_registry,
            model=provider_config.model,
            model_mode=provider_config.model_mode,
            context_window=agent_config.max_context_tokens,
            llm_is_override=self._llm_override is not None,
            user_context=self._user_context,
            runtime_variables=runtime_variables,
            max_iterations=(
                resolved_agent.max_iterations
                if resolved_agent is not None
                and resolved_agent.max_iterations is not None
                else DEFAULT_MAX_ITERATIONS
            ),
        )
        ctx.set("engine", engine)


plugin = AgentLoopComponent()
