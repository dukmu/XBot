"""Core component: assembles the :class:`Engine` from XCore services.

The engine is a component of the XCore application: mounted after the
runtime/tools/hooks components and the builtin plugins, its ``apply`` resolves
the selected Agent, creates the LLM client, runs ``ON_SESSION_INIT`` hooks,
applies the tool filter, builds the :class:`Engine` from the context's
services, and provides it as ``ctx.engine``.  The turn loop therefore runs on
an XCore context: hooks are XCore events, tools/state/config are services.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from xbotv2.api.agents import AgentDefinition
from xbotv2.api.hooks import HookContext, HookStage
from xbotv2.api.runtime import SessionInfo
from xbotv2.config.models import RuntimeConfig
from xbotv2.core.agents import apply_agent_definition, apply_agent_provider, apply_agent_tools
from xbotv2.core.engine import DEFAULT_MAX_ITERATIONS, Engine
from xbotv2.tools.permissions import PermissionIntersection, PermissionSystem

SUBAGENT_FORBIDDEN_TOOLS = frozenset({
    "spawn_subagent",
    "list_subagents",
    "wait_subagent",
    "read_subagent",
    "cancel_subagent",
})


class EngineComponent:
    """Build the runtime Engine from the context's services."""

    def __init__(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        provider_name: str,
        agent_config: RuntimeConfig,
        agent_definition: AgentDefinition | None,
        llm_override: Any,
        selected_agent: str | None,
        parent_permission_system: Any,
        parent_thread_id: str,
        is_subagent: bool,
        interactive: bool,
        user_context: Any,
        plugin_loader: Any,
        context_builder: Any,
        thread_preexisting: bool,
        stored_provider: str | None,
    ) -> None:
        self._session_id = session_id
        self._thread_id = thread_id
        self._workspace_root = workspace_root
        self._provider_name = provider_name
        self._agent_config = agent_config
        self._agent_definition = agent_definition
        self._llm_override = llm_override
        self._selected_agent = selected_agent
        self._parent_permission_system = parent_permission_system
        self._parent_thread_id = parent_thread_id
        self._is_subagent = is_subagent
        self._interactive = interactive
        self._user_context = user_context
        self._plugin_loader = plugin_loader
        self._context_builder = context_builder
        self._thread_preexisting = thread_preexisting
        self._stored_provider = stored_provider
        self.name = "xbot.core"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        agent_registry = ctx.agents.registry
        state_store = ctx.state_store
        runtime_variables = ctx.variables
        tool_registry = ctx.tools.registry
        sandbox = ctx.sandbox
        hook_manager = ctx.hooks
        job_registry = ctx.job_registry
        agent_runtime = ctx.get("agent_runtime")
        paths = ctx.paths
        permissions = ctx.permissions

        agent_config = self._agent_config
        resolved_agent = self._agent_definition
        selected_agent = self._selected_agent
        provider_name = self._provider_name
        is_subagent = self._is_subagent
        parent_permission_system = self._parent_permission_system

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

        if self._thread_preexisting and self._stored_provider is not None:
            provider_name = self._stored_provider
        state_store.provider = provider_name
        agent_config.provider = provider_name
        provider_config = load_provider_config(paths, provider_name)
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
            from xbotv2.llm.client import create_llm

            llm = create_llm(
                provider_config,
                media_root=str(state_store.root),
            )

        # Run ON_SESSION_INIT hooks (plugins discover skills/MCP tools here).
        init_ctx = HookContext(
            stage=HookStage.ON_SESSION_INIT,
            config=agent_config,
            tools=tool_registry,
            sandbox=sandbox,
            plugin_store=None,
            session=SessionInfo(
                session_id=self._session_id,
                thread_id=self._thread_id,
                workspace_root=self._workspace_root,
                provider=provider_name,
            ),
            emit=lambda e: None,
        )
        await hook_manager.run(
            HookStage.ON_SESSION_INIT,
            init_ctx,
            short_circuit=False,
        )

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
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=self._context_builder,
            sandbox_policy=sandbox,
            permission_system=permissions,
            workspace_root=self._workspace_root,
            config=agent_config,
            plugin_loader=self._plugin_loader,
            job_registry=job_registry,
            agent_runtime=agent_runtime,
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


def load_provider_config(paths: Any, provider_name: str) -> Any:
    from xbotv2.config.loader import load_provider_config as _load

    return _load(paths, provider_name)
