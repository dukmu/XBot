"""Active Agent selection and loop composition service.

Definitions live in the independent Agent catalog. This service consumes that
catalog and a loop factory, then owns only active Agent/model selection and
loop composition.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from XBotv2.agents.contracts import (
    AgentCreateOptions,
    AgentDefinition,
    AgentSelection,
)
from XBotv2.agents.events import AGENT_CONFIGURED, AgentConfigured
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.agentloop import (
    DEFAULT_MAX_ITERATIONS,
    AgentLoopFactoryPort,
    LoopFactoryOptions,
    LoopSettings,
)
from XBotv2.core.errors import OperationError
from XBotv2.loader import SOFT_RELOAD, SoftReload
from XBotv2.agents.services import AgentCatalogPort


class AgentsService:
    """Compose and reconfigure the active Agent loop."""

    def __init__(
        self,
        ctx: Any,
        catalog: AgentCatalogPort,
        factory: AgentLoopFactoryPort,
    ) -> None:
        self.ctx = ctx
        self.catalog = catalog
        self._factory = factory

    async def create(self, options: AgentCreateOptions) -> Any:
        """Resolve one Agent and publish the driver returned by its factory."""
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
        user = ctx.settings.user_context()
        loop_settings = LoopSettings(
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
        )
        ctx.model.replace(model)
        await ctx.emit(
            APPLICATION_INITIALIZED,
            ApplicationInitialized(
                agent=definition,
                session=state.session,
                settings=loop_settings,
            ),
        )
        self._restrict_tools(ctx.tools, config, definition)

        engine = self._factory.create(LoopFactoryOptions(
            model_client=ctx.model,
            tools=ctx.tools,
            events=ctx,
            state=state,
            settings=loop_settings,
            max_iterations=(
                definition.max_iterations
                if definition is not None and definition.max_iterations is not None
                else DEFAULT_MAX_ITERATIONS
            ),
        ))
        ctx.set("engine", engine)
        return engine

    def definition(self, name: str) -> AgentDefinition | None:
        return self.catalog.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return self.catalog.definitions()

    def active_definition(self) -> AgentDefinition | None:
        stored = self.ctx.loop_state.metadata.get("agent_definition")
        return (
            self._restore_definition(stored)
            if isinstance(stored, dict)
            else None
        )

    def current_selection(self) -> AgentSelection:
        engine = self.ctx.engine
        return AgentSelection(
            active=engine.settings.agent_name,
            provider=engine.settings.provider,
            model=engine.settings.model,
            model_mode=engine.settings.model_mode,
            context_window=engine.context_window,
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
        definition = self.catalog.get(name)
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

        self._restrict_tools(ctx.tools, config, definition)
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
        await ctx.emit(AGENT_CONFIGURED, AgentConfigured(
            agent=definition,
            session=state.session,
            agent_name=engine.settings.agent_name,
            provider=engine.settings.provider,
            model=engine.settings.model,
            model_mode=engine.settings.model_mode,
            context_window=engine.settings.context_window,
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
        await ctx.emit(AGENT_CONFIGURED, AgentConfigured(
            agent=None,
            session=state.session,
            agent_name=engine.settings.agent_name,
            provider=engine.settings.provider,
            model=engine.settings.model,
            model_mode=engine.settings.model_mode,
            context_window=engine.settings.context_window,
        ))
        return {
            "provider": name,
            "model": model_config.model,
            "model_mode": model_config.model_mode,
        }

    async def rebind_active(self) -> dict[str, Any]:
        """Rebuild the active model client from the current catalog.

        The catalog is configured by the LLM plugin; this method only
        re-resolves the active provider/model and rebinds the engine.  When
        the active binding no longer resolves, the previous client is kept
        and the error is reported (last-good semantics).
        """
        ctx = self.ctx
        engine = ctx.engine
        provider_name = engine.settings.provider
        model_name = engine.settings.model
        try:
            entry = ctx.llm.provider_config(
                provider_name,
                require_key=not engine.settings.llm_is_override,
            )
            model_config = entry.resolve(model_name)
            if not engine.settings.llm_is_override:
                ctx.model.replace(
                    ctx.llm.create(
                        entry, model_config, media_root=ctx.loop_state.media_root
                    )
                )
            engine.configure(
                model_client=ctx.model,
                provider=provider_name,
                model=model_config.model,
                model_mode=model_config.model_mode,
                context_window=model_config.max_context_tokens,
                max_output_tokens=model_config.max_output_tokens or 0,
            )
            ctx.loop_state.session.provider = provider_name
            ctx.loop_state.metadata.update({
                "provider": provider_name,
                "model": model_config.model,
                "model_mode": model_config.model_mode,
                "context_window": model_config.max_context_tokens,
            })
            return {
                "provider": provider_name,
                "model": model_config.model,
                "model_mode": model_config.model_mode,
                "context_window": model_config.max_context_tokens,
                "errors": [],
            }
        except Exception as error:  # noqa: BLE001 - keep last good binding
            return {
                "provider": provider_name,
                "model": model_name,
                "model_mode": engine.settings.model_mode,
                "context_window": engine.context_window,
                "errors": [f"active {provider_name}/{model_name}: {error}"],
            }

    async def select_effort(self, value: str) -> dict[str, Any]:
        """Switch the active model's reasoning effort to an advertised tier.

        Only tiers the model advertises in its ``effort`` list are accepted;
        the provider client is rebuilt with the new tier.
        """
        ctx = self.ctx
        engine = ctx.engine
        provider_name = engine.settings.provider
        model_name = engine.settings.model
        entry = ctx.llm.provider_config(
            provider_name,
            require_key=not engine.settings.llm_is_override,
        )
        model_config = entry.resolve(model_name)
        tiers = list(model_config.effort or [])
        if not tiers:
            raise ValueError(
                f"Model {provider_name}/{model_name} does not advertise "
                "effort tiers"
            )
        if value not in tiers:
            raise ValueError(
                f"Unsupported reasoning effort {value!r} for "
                f"{provider_name}/{model_name}; available: {', '.join(tiers)}"
            )
        model_config = model_config.model_copy(
            update={"reasoning_effort": value}
        )
        if not engine.settings.llm_is_override:
            ctx.model.replace(
                ctx.llm.create(entry, model_config, media_root=ctx.loop_state.media_root)
            )
        engine.configure(
            model_client=ctx.model,
            provider=provider_name,
            model=model_name,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens or 0,
        )
        ctx.loop_state.metadata.update({
            "provider": provider_name,
            "model": model_name,
            "model_mode": model_config.model_mode,
            "context_window": model_config.max_context_tokens,
        })
        return {
            "provider": provider_name,
            "model": model_name,
            "reasoning_effort": value,
            "model_mode": model_config.model_mode,
            "available": tiers,
        }

    async def select(self, name: str) -> dict[str, Any]:
        """Activate one primary Agent (caller owns idle-check and turn lock).

        Unknown or subagent-only names fail closed with
        ``OperationError("agent_not_found")``.
        """
        definition = self.definition(name)
        if definition is None or definition.mode == "subagent":
            raise OperationError("agent_not_found", f"Unknown primary Agent: {name}")
        if definition.name != self.ctx.engine.settings.agent_name:
            await self.activate(definition.name)
        return {
            "active": definition.name,
            "agent_name": definition.name,
            "provider": self.ctx.engine.settings.provider,
            "model": self.ctx.engine.settings.model,
            "model_mode": self.ctx.engine.settings.model_mode,
            "context_window": self.ctx.engine.context_window,
        }

    async def reload_active(self) -> dict[str, Any]:
        """Reload Agent definitions and notify a soft restart.

        Caller owns idle-check and turn lock; the agents plugin rebinds the
        active model and workspace_instructions re-reads its workspace
        sources through the ``SOFT_RELOAD`` event.
        """
        loader = self.ctx.loader
        active = self.ctx.engine.settings.agent_name
        if not await loader.reload("agents-builtins"):
            raise OperationError(
                "plugin_unavailable",
                "Agent definition plugin is not loaded.",
            )
        await self.ctx.emit(SOFT_RELOAD, SoftReload(scope="agents"))
        definition = self.definition(active)
        if definition is None or definition.mode == "subagent":
            raise OperationError(
                "agent_not_found",
                f"Active Agent definition no longer exists: {active}",
            )
        return {
            "active": active,
            "agents": self.definitions(),
        }

    async def rebind_on_soft_reload(self, event: SoftReload) -> None:
        """Rebind the active model client after a soft restart."""
        active = self.ctx.engine.settings.agent_name
        definition = self.definition(active)
        if definition is not None and definition.mode != "subagent":
            try:
                selected = await self.activate(active)
            except Exception as error:  # noqa: BLE001 - keep last good binding
                event.errors.append(
                    f"active agent {active}: {error}"
                )
                selected = await self.rebind_active()
        else:
            selected = await self.rebind_active()
        errors = selected.pop("errors", [])
        if errors:
            event.errors.extend(errors)

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
            default = self.catalog.get("default")
            if default is not None and default.mode != "subagent":
                selected = default.name
        if selected is not None:
            registered = self.catalog.get(selected)
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
    ) -> Any:
        """Resolve the catalog model for an Agent definition.

        ``definition.model`` selects one catalog entry (default when unset);
        Agent-level sampling overrides apply on top of that entry.  A model
        declared by the Agent frontmatter but absent from the catalog
        inherits the provider default entry's settings (with the frontmatter
        overrides applied); explicit provider/model selection stays
        fail-closed (see ``select_provider``).
        """
        model_name = (
            definition.model
            if definition is not None and definition.model is not None
            else None
        )
        try:
            model_config = provider.resolve(model_name)
        except ValueError:
            if definition is None or definition.model is None:
                raise
            model_config = provider.resolve(None).model_copy(
                update={"model": definition.model}
            )
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
        tools: Any,
        config: Any,
        definition: AgentDefinition | None,
    ) -> None:
        selectors = (
            list(definition.tools)
            if definition is not None and definition.tools is not None
            else list(config.tools) if config.tools is not None else None
        )
        tools.restrict(selectors)
        if definition is not None and definition.disabled_tools:
            tools.exclude(list(definition.disabled_tools))

__all__ = ["AgentsService"]
