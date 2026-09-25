"""Active Agent selection and loop composition service.

Definitions live in the independent Agent catalog. This service consumes that
catalog and a loop factory, then owns only active Agent/model selection and
loop composition.
"""

from __future__ import annotations

from pydantic import JsonValue

from XBotv2.agents.contracts import (
    AgentCreateOptions,
    AgentDefinition,
    AgentRuntimePort,
    AgentSelection,
    AllTools,
    InheritGeneration,
)
from XBotv2.agents.events import AGENT_CONFIGURED, AgentConfigured
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.application import ApplicationEventsPort
from XBotv2.agentloop import (
    DEFAULT_MAX_ITERATIONS,
    AgentInbox,
    AgentLoopDriverPort,
    AgentLoopFactoryPort,
    LoopFactoryOptions,
    ToolsPort,
    LoopState,
)
from XBotv2.config import RuntimeConfig
from XBotv2.config import SettingsPort
from XBotv2.config.contracts import ConfigPluginConfig
from XBotv2.coretools.contracts import CoreToolsConfig
from XBotv2.loader.contracts import PluginTree
from XBotv2.core.errors import OperationError
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.metadata import (
    MetadataReady,
    MetadataUninitialized,
    ThreadMetadata,
)
from XBotv2.core.domain import (
    AgentExecutionLimits,
    ReasoningGenerationMode,
    StandardGenerationMode,
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
)
from XBotv2.core.runtime_logging import RuntimeLog
from XBotv2.session.contracts import SessionKey
from XBotv2.llm import (
    EffortSelection,
    LlmServicePort,
    ModelConfig,
    ModelPort,
    ProviderConfig,
    ProviderSelection,
)
from XBotv2.agents.contracts import AgentCatalogPort


class AgentsService(AgentRuntimePort):
    """Compose and reconfigure the active Agent loop."""

    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        factory: AgentLoopFactoryPort,
        events: ApplicationEventsPort,
        state: LoopState,
        settings: SettingsPort,
        providers: LlmServicePort,
        model: ModelPort,
        tools: ToolsPort,
        artifacts: ArtifactStorePort,
        inbox: AgentInbox,
        runtime_log: RuntimeLog,
    ) -> None:
        self.catalog = catalog
        self._factory = factory
        self._events = events
        self._state = state
        self._settings = settings
        self._providers = providers
        self._model = model
        self._tools = tools
        self._artifacts = artifacts
        self._inbox = inbox
        self._log = runtime_log.bind("agent")
        self._engine: AgentLoopDriverPort | None = None
        self._model_is_override = False
        self._restored_runtime = False

    async def create(self, options: AgentCreateOptions) -> AgentLoopDriverPort:
        """Resolve one Agent and publish the driver returned by its factory."""
        state = self._state
        config = self._runtime_config(
            self._settings.load_plugin_tree(
                options.workspace_root,
                options.session_id,
                thread_id=options.thread_id,
            )
        )
        metadata_lifecycle = state.metadata.lifecycle
        stored_metadata = (
            metadata_lifecycle.metadata
            if isinstance(metadata_lifecycle, MetadataReady)
            else None
        )
        definition = self._resolve_definition(options, stored_metadata)
        self._restored_runtime = state.resumed
        if self._restored_runtime:
            if stored_metadata is None:
                raise RuntimeError(
                    "Cannot resume a persisted thread without thread metadata"
                )
            runtime_selection = stored_metadata.runtime_selection
            provider_name = runtime_selection.model.route.provider
        else:
            provider_name = self._resolve_provider(
                options,
                definition,
                stored_metadata,
            )
        provider = self._providers.provider_config(
            provider_name,
            require_key=options.model_override is None,
        )
        model_config = (
            provider.resolve(runtime_selection.model.route.model)
            if self._restored_runtime
            else self._resolve_model_config(provider, definition)
        )

        if self._restored_runtime:
            self._tools.restrict(runtime_selection.enabled_tools)
        else:
            self._restrict_tools(self._tools, config, definition)
        if not self._restored_runtime:
            title = stored_metadata.title if stored_metadata is not None else ""
            if (
                options.is_subagent
                and definition is not None
                and (not title or title == state.session.session_id)
            ):
                title = definition.name
            runtime_selection = self._resolved_runtime_selection(
                config=config,
                definition=definition,
                provider_name=provider_name,
                model_config=model_config,
            )
            metadata = ThreadMetadata(
                runtime_selection=runtime_selection,
                parent_thread_id=options.parent_thread_id,
                workspace_root=options.workspace_root,
                title=title,
            )
            if isinstance(metadata_lifecycle, MetadataUninitialized):
                await state.metadata.initialize(metadata)
            else:
                await state.metadata.replace(metadata)

        model = (
            options.model_override
            if options.model_override is not None
            else self._providers.create(provider, model_config)
        )
        user = self._settings.user_context()
        self._model_is_override = options.model_override is not None
        self._model.replace(model)
        engine = self._factory.create(LoopFactoryOptions(
            model_client=self._model,
            tools=self._tools,
            events=self._events,
            state=state,
            user_identity=user,
            memory=config.memory,
            max_iterations=(
                runtime_selection.limits.max_turns
                if runtime_selection.limits.max_turns is not None
                else DEFAULT_MAX_ITERATIONS
            ),
            inbox=self._inbox,
        ))
        self._engine = engine
        self._log.info(
            "agent.created",
            session_id=options.session_id,
            thread_id=options.thread_id,
            agent=runtime_selection.agent_name,
            provider=provider_name,
            model=model_config.model,
            context_window=runtime_selection.model.context_window,
            tools_enabled=len(self._tools.enabled()),
            resumed=state.resumed,
        )
        return engine

    async def announce_initialized(self) -> None:
        """Notify fully mounted plugins after the dependency graph is running."""
        definition = self.active_definition()
        await self._events.emit(
            APPLICATION_INITIALIZED,
            ApplicationInitialized(
                session=self._state.session,
                metadata=self._state.metadata.value,
            ),
        )
        if self._restored_runtime:
            desired = self._state.metadata.value.runtime_selection.enabled_tools
            self._tools.restrict(desired)
            actual = tuple(tool.name for tool in self._tools.enabled())
            if set(actual) != set(desired):
                missing = sorted(set(desired) - set(actual))
                raise RuntimeError(
                    "Persisted runtime selection references unavailable tools: "
                    + ", ".join(missing)
                )
        else:
            self._restrict_tools(
                self._tools,
                self.runtime_config(definition),
                definition,
            )
            current = self._state.metadata.value.runtime_selection
            await self._state.metadata.replace_runtime_selection(
                current.model_copy(update={
                    "enabled_tools": tuple(tool.name for tool in self._tools.enabled()),
                })
            )

    def definition(self, name: str) -> AgentDefinition | None:
        return self.catalog.get(name)

    def _require_engine(self) -> AgentLoopDriverPort:
        if self._engine is None:
            raise RuntimeError("Agent runtime has not been created")
        return self._engine

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return self.catalog.definitions()

    def active_definition(self) -> AgentDefinition | None:
        name = self._state.metadata.value.runtime_selection.agent_name
        return self.catalog.get(name) if name else None

    async def _replace_runtime_selection(
        self,
        *,
        provider: str,
        model: str,
        model_mode: str,
        context_window: int,
        max_output_tokens: int,
        agent_name: str | None = None,
    ) -> None:
        current = self._state.metadata.value.runtime_selection
        selection = current.model_copy(update={
            "agent_name": current.agent_name if agent_name is None else agent_name,
            "model": current.model.model_copy(update={
                "route": ModelRoute(provider=provider, model=model),
                "generation": current.model.generation.model_copy(update={
                    "mode": (
                        ReasoningGenerationMode(effort=model_mode)
                        if model_mode else StandardGenerationMode()
                    ),
                    "max_output_tokens": max(1, max_output_tokens),
                }),
                "context_window": max(1, context_window),
            }),
        })
        await self._state.metadata.replace_runtime_selection(selection)

    def current_selection(self) -> AgentSelection:
        runtime = self._state.metadata.value.runtime_selection
        mode = runtime.model.generation.mode
        match mode:
            case ReasoningGenerationMode(effort=effort):
                model_mode = effort
            case StandardGenerationMode():
                model_mode = ""
        return AgentSelection(
            active=runtime.agent_name,
            provider=runtime.model.route.provider,
            model=runtime.model.route.model,
            model_mode=model_mode,
            context_window=runtime.model.context_window,
        )

    def runtime_config(
        self,
        definition: AgentDefinition | None = None,
    ) -> RuntimeConfig:
        """Resolve current runtime config with the active Agent overlay."""
        state = self._state
        config = self._runtime_config(
            self._settings.load_plugin_tree(
                state.session.workspace_root,
                state.session.session_id,
                thread_id=state.session.thread_id,
            )
        )
        definition = definition or self.active_definition()
        return config

    async def activate(self, name: str) -> AgentSelection:
        """Atomically apply a registered primary Agent to the live driver."""
        definition = self.catalog.get(name)
        if definition is None or definition.mode == "subagent":
            raise ValueError(f"Unknown primary Agent: {name}")

        engine = self._require_engine()
        state = self._state
        config = self.runtime_config(definition)
        current = state.metadata.value.runtime_selection
        policy_route = definition.model_policy.route
        provider_name = (
            policy_route.provider
            if isinstance(policy_route, ModelRoute)
            else current.model.route.provider
        )
        provider = self._providers.provider_config(
            provider_name,
            require_key=not self._model_is_override,
        )
        model_config = self._resolve_model_config(provider, definition)
        if not self._model_is_override:
            self._model.replace(
                self._providers.create(provider, model_config)
            )

        self._restrict_tools(self._tools, config, definition)
        engine.configure(
            model_client=self._model,
            max_iterations=definition.limits.max_turns or DEFAULT_MAX_ITERATIONS,
        )
        runtime = self._resolved_runtime_selection(
            config=config,
            definition=definition,
            provider_name=provider_name,
            model_config=model_config,
        )
        await state.metadata.replace_runtime_selection(runtime)
        await self._events.emit(AGENT_CONFIGURED, AgentConfigured(
            runtime_selection=runtime,
            session_key=SessionKey(
                session_id=state.session.session_id,
                thread_id=state.session.thread_id,
            ),
        ))
        self._log.info(
            "agent.selected",
            agent=definition.name,
            provider=provider_name,
            model=model_config.model,
            context_window=runtime.model.context_window,
        )
        return AgentSelection(
            active=definition.name,
            provider=provider_name,
            model=model_config.model,
            model_mode=model_config.model_mode,
            context_window=runtime.model.context_window,
        )

    async def select_provider(
        self,
        name: str,
        model: str | None = None,
    ) -> ProviderSelection:
        """Apply a configured provider (and optional model) to the driver.

        ``model`` selects one catalog entry of the provider; unknown model
        names fail closed before the client is recreated.
        """
        if name not in self._providers.names():
            raise ValueError(f"Unknown provider: {name}")
        engine = self._require_engine()
        state = self._state
        provider = self._providers.provider_config(
            name,
            require_key=not self._model_is_override,
        )
        model_config = provider.resolve(model)
        if model_config.max_output_tokens is None:
            raise ValueError(
                f"Model {name}/{model_config.model} must declare "
                "max_output_tokens before it can become a runtime selection"
            )
        if not self._model_is_override:
            self._model.replace(
                self._providers.create(provider, model_config)
            )
        engine.configure(
            model_client=self._model,
        )
        await self._replace_runtime_selection(
            provider=name,
            model=model_config.model,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens,
        )
        await self._events.emit(AGENT_CONFIGURED, AgentConfigured(
            runtime_selection=state.metadata.value.runtime_selection,
            session_key=SessionKey(
                session_id=state.session.session_id,
                thread_id=state.session.thread_id,
            ),
        ))
        self._log.info(
            "provider.selected",
            provider=name,
            model=model_config.model,
            model_mode=model_config.model_mode,
        )
        return ProviderSelection(
            provider=name,
            model=model_config.model,
            model_mode=model_config.model_mode,
        )

    async def select_effort(self, value: str) -> EffortSelection:
        """Switch the active model's reasoning effort to an advertised tier.

        Only tiers the model advertises in its ``effort`` list are accepted;
        the provider client is rebuilt with the new tier.
        """
        engine = self._require_engine()
        runtime = self._state.metadata.value.runtime_selection
        provider_name = runtime.model.route.provider
        model_name = runtime.model.route.model
        entry = self._providers.provider_config(
            provider_name,
            require_key=not self._model_is_override,
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
        if model_config.max_output_tokens is None:
            raise ValueError(
                f"Model {provider_name}/{model_name} must declare "
                "max_output_tokens before it can become a runtime selection"
            )
        if not self._model_is_override:
            self._model.replace(
                self._providers.create(entry, model_config)
            )
        engine.configure(
            model_client=self._model,
        )
        await self._replace_runtime_selection(
            provider=provider_name,
            model=model_name,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens,
        )
        self._log.info(
            "model.effort.selected",
            provider=provider_name,
            model=model_name,
            reasoning_effort=value,
        )
        return EffortSelection(
            provider=provider_name,
            model=model_name,
            reasoning_effort=value,
            model_mode=model_config.model_mode,
            available=tuple(tiers),
        )

    async def select(self, name: str) -> AgentSelection:
        """Activate one primary Agent (caller owns idle-check and turn lock).

        Unknown or subagent-only names fail closed with
        ``OperationError("agent_not_found")``.
        """
        definition = self.definition(name)
        if definition is None or definition.mode == "subagent":
            raise OperationError("agent_not_found", f"Unknown primary Agent: {name}")
        engine = self._require_engine()
        if definition.name != self._state.metadata.value.runtime_selection.agent_name:
            await self.activate(definition.name)
        return self.current_selection()

    def _resolve_definition(
        self,
        options: AgentCreateOptions,
        metadata: ThreadMetadata | None,
    ) -> AgentDefinition | None:
        definition = options.agent_definition
        stored_name = (
            metadata.runtime_selection.agent_name
            if metadata is not None
            else None
        )

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
        metadata: ThreadMetadata | None,
    ) -> str:
        provider_name = options.provider_name
        if definition is not None and isinstance(
            definition.model_policy.route,
            ModelRoute,
        ):
            provider_name = definition.model_policy.route.provider
        if metadata is not None:
            return metadata.runtime_selection.model.route.provider
        return (
            provider_name
            if provider_name is not None
            else self._providers.default_name()
        )

    def _runtime_config(self, tree: PluginTree) -> RuntimeConfig:
        """Build the Agent-owned runtime view from generic plugin entries.

        Configuration loading only resolves the layered tree.  The Agent
        composition boundary is the first place that needs developer
        instructions and memory, so it validates the config plugin's own
        declaration here and combines it with provider-neutral runtime facts.
        """
        config_entry = tree.entry("config")
        settings = ConfigPluginConfig.model_validate(
            config_entry.config if config_entry is not None else {}
        )
        coretools_entry = tree.entry("coretools")
        coretools = CoreToolsConfig.model_validate(
            coretools_entry.config if coretools_entry is not None else {}
        )
        return RuntimeConfig(
            enabled_tools=coretools.enabled_tools,
            instructions=settings.instructions,
            memory=self._settings.memory(),
        )

    @staticmethod
    def _resolved_prompt(
        config: RuntimeConfig,
        definition: AgentDefinition | None,
    ) -> str:
        """Resolve the one effective prompt stored in thread metadata."""
        parts = [config.instructions]
        if definition is not None:
            parts.append(definition.prompt)
        return "\n\n".join(part.strip() for part in parts if part.strip())

    def _resolved_runtime_selection(
        self,
        *,
        config: RuntimeConfig,
        definition: AgentDefinition | None,
        provider_name: str,
        model_config: ModelConfig,
    ) -> ResolvedRuntimeSelection:
        if model_config.max_output_tokens is None:
            raise ValueError(
                f"Model {provider_name}/{model_config.model} must declare "
                "max_output_tokens before it can become a runtime selection"
            )
        mode = (
            ReasoningGenerationMode(effort=model_config.model_mode)
            if model_config.model_mode
            else StandardGenerationMode()
        )
        return ResolvedRuntimeSelection(
            agent_name=(
                definition.name if definition is not None else "XBotv2"
            ),
            prompt=self._resolved_prompt(config, definition),
            limits=(definition.limits if definition is not None else AgentExecutionLimits()),
            enabled_tools=tuple(tool.name for tool in self._tools.enabled()),
            model=ResolvedModelSelection(
                route=ModelRoute(provider=provider_name, model=model_config.model),
                generation=GenerationSettings(
                    mode=(
                        definition.model_policy.generation
                        if definition is not None
                        and not isinstance(
                            definition.model_policy.generation,
                            InheritGeneration,
                        )
                        else mode
                    ),
                    temperature=(
                        definition.model_policy.temperature
                        if definition is not None
                        and definition.model_policy.temperature is not None
                        else model_config.temperature
                    ),
                    max_output_tokens=model_config.max_output_tokens,
                ),
                context_window=(
                    definition.model_policy.context_window
                    if definition is not None
                    and definition.model_policy.context_window is not None
                    else model_config.max_context_tokens
                ),
            ),
        )

    @staticmethod
    def _resolve_model_config(
        provider: ProviderConfig,
        definition: AgentDefinition | None,
    ) -> ModelConfig:
        """Resolve the catalog model for an Agent definition.

        A concrete Agent route selects one catalog entry (default when inherited);
        Agent-level sampling overrides apply on top of that entry.  A model
        declared by the Agent frontmatter but absent from the catalog
        inherits the provider default entry's settings (with the frontmatter
        overrides applied); explicit provider/model selection stays
        fail-closed (see ``select_provider``).
        """
        route = definition.model_policy.route if definition is not None else None
        model_config = provider.resolve(
            route.model if isinstance(route, ModelRoute) else None
        )
        if definition is not None:
            updates: dict[str, JsonValue] = {}
            if definition.model_policy.temperature is not None:
                updates["temperature"] = definition.model_policy.temperature
            if definition.model_policy.max_output_tokens is not None:
                updates["max_output_tokens"] = (
                    definition.model_policy.max_output_tokens
                )
            if updates:
                model_config = model_config.model_copy(update=updates)
        return model_config

    @staticmethod
    def _restrict_tools(
        tools: ToolsPort,
        config: RuntimeConfig,
        definition: AgentDefinition | None,
    ) -> None:
        selection = config.enabled_tools
        if definition is not None and not isinstance(
            definition.tool_policy.enabled,
            AllTools,
        ):
            selection = definition.tool_policy.enabled
        tools.restrict(selection)
        if definition is not None and definition.tool_policy.disabled:
            tools.exclude(list(definition.tool_policy.disabled))

__all__ = ["AgentsService"]
