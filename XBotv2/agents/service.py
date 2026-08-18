"""Agent registry, selection, and creation service.

The service owns Agent definitions and delegates concrete driver construction
to the factory registered by the agent-loop component.  Application startup
only supplies launcher facts and calls :meth:`AgentsService.create`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from xcore import bound_effect, current_plugin_name

from XBotv2.agents.loader import load_definitions
from XBotv2.core.agents import AgentCreateOptions, AgentDefinition
from XBotv2.core.events import EventContext, Events
from XBotv2.core.loop import (
    DEFAULT_MAX_ITERATIONS,
    LoopFactoryOptions,
    LoopSettings,
)
from XBotv2.core.runtime import SessionInfo
from XBotv2.llm.config import ModelConfig


class AgentRegistry:
    """Store immutable Agent definitions under their registering owner.

    Base definitions (built-ins, data root, programmatic registrations) and
    workspace overlays live in separate layers: an overlay replaces a base
    definition by name without mutating it, and unloading either side keeps
    the other intact.
    """

    def __init__(self) -> None:
        self._base: dict[str, AgentDefinition] = {}
        self._base_owners: dict[str, str] = {}
        self._overlay: dict[str, AgentDefinition] = {}
        self._overlay_owners: dict[str, str] = {}

    def register(
        self,
        definition: AgentDefinition,
        *,
        owner: str,
        overlay: bool = False,
    ) -> str:
        layer = self._overlay if overlay else self._base
        owners = self._overlay_owners if overlay else self._base_owners
        if definition.name in layer:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        layer[definition.name] = definition
        owners[definition.name] = owner
        return definition.name

    def unregister(self, name: str, *, owner: str) -> bool:
        for layer, owners in (
            (self._overlay, self._overlay_owners),
            (self._base, self._base_owners),
        ):
            if owners.get(name) == owner:
                owners.pop(name, None)
                layer.pop(name, None)
                return True
        return False

    def get(self, name: str) -> AgentDefinition | None:
        return self._overlay.get(name) or self._base.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        merged = dict(self._base)
        merged.update(self._overlay)
        return tuple(merged.values())


class AgentsService:
    """Own Agent definitions and delegate creation to the active loop factory."""

    def __init__(self, ctx: Any, registry: AgentRegistry | None = None) -> None:
        self.ctx = ctx
        self.registry = registry or AgentRegistry()
        self._factory: Any = None

    def set_factory(self, factory: Any) -> Any:
        if self._factory is not None:
            raise RuntimeError("an Agent factory is already registered")
        self._factory = factory

        def clear() -> None:
            if self._factory is factory:
                self._factory = None

        return bound_effect(clear)

    async def create(self, options: AgentCreateOptions) -> Any:
        """Resolve one Agent and publish the driver returned by its factory."""
        if self._factory is None:
            raise RuntimeError("no Agent factory registered")

        ctx = self.ctx
        state = ctx.loop_state
        config = ctx.settings.load_runtime_config(
            options.workspace_root,
            options.session_id,
        )
        definition = self._resolve_definition(options, state.metadata)
        provider_name = self._resolve_provider(
            options,
            definition,
            state.metadata,
            configured_provider=config.provider,
        )
        if definition is not None:
            self._apply_definition(config, definition)

        provider = ctx.llm.provider_config(
            provider_name,
            require_key=options.model_override is None,
        )
        model_config = self._resolve_model_config(provider, definition)

        config.provider = provider_name
        config.max_context_tokens = (
            definition.context_window
            if definition is not None and definition.context_window is not None
            else model_config.max_context_tokens
        )
        config.max_output_tokens = model_config.max_output_tokens
        state.session.provider = provider_name
        state.metadata = {
            "agent": definition.name if definition is not None else "",
            "agent_definition": asdict(definition) if definition is not None else None,
            "provider": provider_name,
            "parent_thread_id": options.parent_thread_id,
            "workspace_root": options.workspace_root,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
            "context_window": config.max_context_tokens,
        }

        model = (
            options.model_override
            if options.model_override is not None
            else ctx.llm.create(provider, model_config, media_root=state.media_root)
        )
        ctx.model.replace(model)
        await ctx.emit(
            Events.SESSION_INIT,
            EventContext(
                config=config,
                agent=definition,
                tools=ctx.tools.registry,
                session=SessionInfo(
                    session_id=options.session_id,
                    thread_id=options.thread_id,
                    workspace_root=options.workspace_root,
                    provider=provider_name,
                ),
            ),
        )
        self._restrict_tools(ctx.tools.registry, config, definition)

        user = ctx.settings.user_context()
        engine = self._factory.create(LoopFactoryOptions(
            model_client=ctx.model,
            tools=ctx.tools,
            events=ctx,
            state=state,
            settings=LoopSettings(
                provider=provider_name,
                model=model_config.model,
                model_mode=model_config.model_mode,
                context_window=config.max_context_tokens,
                max_output_tokens=config.max_output_tokens or 0,
                agent_name=config.agent_name,
                agent_role=config.agent_role,
                user_name=user.user_name,
                user_id=user.user_id,
                developer_instructions=config.instructions,
                agent_instructions=config.agent_instructions,
                memory=config.memory,
                workspace=options.workspace_root,
                llm_is_override=options.model_override is not None,
            ),
            max_iterations=(
                definition.max_iterations
                if definition is not None and definition.max_iterations is not None
                else DEFAULT_MAX_ITERATIONS
            ),
        ))
        ctx.set("engine", engine)
        return engine

    def definition(self, name: str) -> AgentDefinition | None:
        return self.registry.get(name)

    def active_definition(self) -> AgentDefinition | None:
        stored = self.ctx.loop_state.metadata.get("agent_definition")
        return (
            self._restore_definition(stored)
            if isinstance(stored, dict)
            else None
        )

    def runtime_config(
        self,
        definition: AgentDefinition | None = None,
    ) -> Any:
        """Resolve current runtime config with the active Agent overlay."""
        state = self.ctx.loop_state
        config = self.ctx.settings.load_runtime_config(
            state.session.workspace_root,
            state.session.session_id,
        )
        definition = definition or self.active_definition()
        if definition is not None:
            self._apply_definition(config, definition)
        return config

    async def activate(self, name: str) -> dict[str, Any]:
        """Atomically apply a registered primary Agent to the live driver."""
        definition = self.registry.get(name)
        if definition is None or definition.mode == "subagent":
            raise ValueError(f"Unknown primary Agent: {name}")

        ctx = self.ctx
        engine = ctx.engine
        state = ctx.loop_state
        config = self.runtime_config(definition)
        provider_name = definition.provider or engine.settings.provider
        provider = ctx.llm.provider_config(
            provider_name,
            require_key=not engine.settings.llm_is_override,
        )
        model_config = self._resolve_model_config(provider, definition)
        config.provider = provider_name
        config.max_context_tokens = (
            definition.context_window or model_config.max_context_tokens
        )
        config.max_output_tokens = model_config.max_output_tokens
        if not engine.settings.llm_is_override:
            ctx.model.replace(
                ctx.llm.create(provider, model_config, media_root=state.media_root)
            )

        self._restrict_tools(ctx.tools.registry, config, definition)
        engine.configure(
            model_client=ctx.model,
            max_iterations=definition.max_iterations or DEFAULT_MAX_ITERATIONS,
            provider=provider_name,
            model=model_config.model,
            model_mode=model_config.model_mode,
            context_window=config.max_context_tokens,
            max_output_tokens=config.max_output_tokens or 0,
            agent_name=config.agent_name,
            agent_role=config.agent_role,
            developer_instructions=config.instructions,
            agent_instructions=config.agent_instructions,
            memory=config.memory,
        )
        state.session.provider = provider_name
        state.metadata.update({
            "agent": definition.name,
            "agent_definition": asdict(definition),
            "provider": provider_name,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
            "context_window": config.max_context_tokens,
        })
        await ctx.emit(Events.AGENT_CONFIGURED, EventContext(
            config=config,
            agent=definition,
            tools=ctx.tools.registry,
            session=state.session,
        ))
        return {
            "agent": definition,
            "provider": provider_name,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
            "context_window": config.max_context_tokens,
        }

    async def select_provider(
        self,
        name: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Apply a configured provider (and optional model) to the driver.

        ``model`` selects one catalog entry of the provider; unknown model
        names fail closed before the client is recreated.
        """
        ctx = self.ctx
        if name not in ctx.llm.names():
            raise ValueError(f"Unknown provider: {name}")
        engine = ctx.engine
        state = ctx.loop_state
        provider = ctx.llm.provider_config(
            name,
            require_key=not engine.settings.llm_is_override,
        )
        model_config = provider.resolve(model)
        if not engine.settings.llm_is_override:
            ctx.model.replace(
                ctx.llm.create(provider, model_config, media_root=state.media_root)
            )
        engine.configure(
            model_client=ctx.model,
            provider=name,
            model=model_config.model,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens or 0,
        )
        state.session.provider = name
        state.metadata.update({
            "provider": name,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
            "context_window": model_config.max_context_tokens,
        })
        await ctx.emit(Events.AGENT_CONFIGURED, EventContext(
            config=engine.settings,
            session=state.session,
        ))
        return {
            "provider": name,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
        }

    def _resolve_definition(
        self,
        options: AgentCreateOptions,
        metadata: dict[str, Any],
    ) -> AgentDefinition | None:
        definition = options.agent_definition
        stored_name = str(metadata.get("agent") or "") or None
        stored_definition = metadata.get("agent_definition")
        if definition is None and isinstance(stored_definition, dict):
            definition = self._restore_definition(stored_definition)

        selected = options.selected_agent
        if selected is not None and stored_name is not None and selected != stored_name:
            raise ValueError(
                f"Thread {options.thread_id!r} belongs to Agent {stored_name!r}, "
                f"not {selected!r}"
            )
        if selected is None and options.agent_definition is None:
            selected = stored_name
        if selected is None and definition is None:
            default = self.registry.get("default")
            if default is not None and default.mode != "subagent":
                selected = default.name
        if selected is not None:
            registered = self.registry.get(selected)
            if definition is None:
                if registered is None or (
                    registered.mode == "subagent" and not options.is_subagent
                ):
                    raise ValueError(f"Unknown primary agent: {selected}")
                definition = registered
            elif definition.name != selected:
                raise ValueError(
                    f"Stored Agent {definition.name!r} does not match {selected!r}"
                )
        if (
            definition is not None
            and definition.mode == "subagent"
            and not options.is_subagent
        ):
            raise ValueError(f"Unknown primary agent: {definition.name}")
        return definition

    def _resolve_provider(
        self,
        options: AgentCreateOptions,
        definition: AgentDefinition | None,
        metadata: dict[str, Any],
        *,
        configured_provider: str,
    ) -> str:
        provider_name = options.provider_name
        if provider_name == "default":
            provider_name = configured_provider
        if provider_name == "default":
            provider_name = self.ctx.llm.default_name()
        if definition is not None and definition.provider:
            provider_name = definition.provider
        return str(metadata.get("provider") or provider_name)

    @staticmethod
    def _restore_definition(data: dict[str, Any]) -> AgentDefinition:
        values = dict(data)
        for field_name in ("tools", "disabled_tools"):
            if isinstance(values.get(field_name), list):
                values[field_name] = tuple(str(item) for item in values[field_name])
        return AgentDefinition(**values)

    @staticmethod
    def _apply_definition(config: Any, definition: AgentDefinition) -> None:
        config.agent_name = definition.name
        config.agent_role = definition.description
        config.agent_instructions = definition.prompt
        if definition.tools is not None:
            config.tools = list(definition.tools)
        if definition.context_window is not None:
            config.max_context_tokens = definition.context_window

    @staticmethod
    def _resolve_model_config(
        provider: Any,
        definition: AgentDefinition | None,
    ) -> ModelConfig:
        """Resolve the catalog model for an Agent definition.

        ``definition.model`` selects one catalog entry (default when unset);
        Agent-level sampling overrides apply on top of that entry.
        """
        model_name = (
            definition.model
            if definition is not None and definition.model is not None
            else None
        )
        model_config = provider.resolve(model_name)
        if definition is not None:
            updates: dict[str, Any] = {}
            if definition.temperature is not None:
                updates["temperature"] = definition.temperature
            if definition.max_output_tokens is not None:
                updates["max_output_tokens"] = definition.max_output_tokens
            if updates:
                model_config = model_config.model_copy(update=updates)
        return model_config

    @staticmethod
    def _restrict_tools(
        registry: Any,
        config: Any,
        definition: AgentDefinition | None,
    ) -> None:
        selectors = (
            list(definition.tools)
            if definition is not None and definition.tools is not None
            else list(config.tools) if config.tools is not None else None
        )
        registry.restrict(selectors)
        if definition is not None and definition.disabled_tools:
            registry.exclude(list(definition.disabled_tools))

    def register(
        self,
        definition: AgentDefinition,
        *,
        overlay: bool = False,
    ) -> str:
        owner = current_plugin_name()
        name = self.registry.register(definition, owner=owner, overlay=overlay)
        bound_effect(lambda: self.registry.unregister(name, owner=owner))
        return name

    def register_markdown(
        self,
        directory: Any,
        *,
        variables: Any = None,
        overlay: bool = True,
    ) -> tuple[str, ...]:
        """Parse and register every Markdown definition in *directory*.

        Used by workspace-scoped plugins (``workspace_instructions``) to
        register definitions discovered outside the data root.  Overlay
        registrations replace a same-named base definition without mutating
        it, so unloading the discoverer restores the underlying definition.
        """
        owner = current_plugin_name()
        names = [
            self.registry.register(
                definition,
                owner=owner,
                overlay=overlay,
            )
            for definition in load_definitions(directory, variables)
        ]
        if names:
            bound_effect(
                lambda: [
                    self.registry.unregister(name, owner=owner)
                    for name in names
                ]
            )
        return tuple(names)

    def unregister(self, name: str) -> bool:
        return self.registry.unregister(name, owner=current_plugin_name())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


__all__ = ["AgentRegistry", "AgentsService"]
