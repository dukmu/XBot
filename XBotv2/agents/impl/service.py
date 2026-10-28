"""Resolve current Agent definitions and bind thread-local model/tool choices."""

from __future__ import annotations

from pydantic import JsonValue
from XBotv2.agents.api.contracts import (
    AgentCreateOptions,
    AgentDefinition,
    AgentRuntimePort,
    AgentSelection,
    AllTools,
    InheritGeneration,
    AgentCatalogPort,
)
from XBotv2.agents.api.events import AGENT_CONFIGURED, AgentConfigured
from XBotv2.agentloop.api.events import THREAD_INITIALIZED
from XBotv2.agentloop.api.events import ThreadInitialized
from XBotv2.session.api.contracts import ThreadInfo
from xcore.api.context import Context
from XBotv2.agentloop.api.contracts import (
    AGENT_LOOP,
    AgentLoopPort,
    ToolsPort,
    Inbox,
)
from XBotv2.agentloop.api.state import LoopState
from XBotv2.config.api.contracts import RuntimeConfig, SettingsPort
from XBotv2.config.api.contracts import ConfigPluginConfig
from XBotv2.coretools.api.models import CoreToolsConfig
from XBotv2.loader.api.contracts import PluginTree
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.artifacts import ArtifactStorePort
from XBotv2.core.api.metadata import (
    MetadataReady,
    MetadataUninitialized,
    ThreadMetadata,
)
from XBotv2.core.api.domain import (
    AgentExecutionLimits,
    ReasoningGenerationMode,
    StandardGenerationMode,
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
)
from XBotv2.core.api.runtime_logging import RuntimeLog
from XBotv2.session.api.contracts import SessionKey
from XBotv2.llm.api.contracts import (
    EffortSelection,
    LlmServicePort,
    ModelConfig,
    ModelPort,
    ProviderConfig,
    ProviderSelection,
)


class AgentsService(AgentRuntimePort):
    """Compose and reconfigure the active Agent loop."""

    def __init__(
        self,
        *,
        catalog: AgentCatalogPort,
        events: Context,
        state: LoopState,
        settings: SettingsPort,
        providers: LlmServicePort,
        model: ModelPort,
        tools: ToolsPort,
        runtime_log: RuntimeLog,
        stored_facts=None,
        session_title="",
    ) -> None:
        self._stored_facts = stored_facts
        self._session_title = session_title
        self.catalog = catalog
        self._events = events
        self._state = state
        self._settings = settings
        self._providers = providers
        self._model = model
        self._tools = tools
        self._log = runtime_log.bind("agent")
        self._provider_name: str | None = None
        self._model_is_override = False

    async def bind(
        self,
        options: AgentCreateOptions,
        launch: ThreadInfo,
    ) -> None:
        """Bind the definition and model selection for the current thread."""
        self._provider_name = options.provider_name
        state = self._state
        config = self._runtime_config(
            self._settings.load_plugin_tree(
                launch.workspace_root,
                launch.session_id,
                thread_id=launch.thread_id,
            )
        )
        metadata_lifecycle = state.metadata.lifecycle
        stored_metadata = self._stored_facts
        definition = self._resolve_definition(options, launch, stored_metadata)
        if state.resumed and stored_metadata is None:
            raise RuntimeError(
                "Cannot resume a persisted thread without thread metadata"
            )
        provider_override = (
            stored_metadata.provider_override if stored_metadata is not None else None
        )
        model_override = (
            stored_metadata.model_override if stored_metadata is not None else None
        )
        effort_override = (
            stored_metadata.effort_override if stored_metadata is not None else None
        )
        if (
            stored_metadata is None
            and options.provider_name is not None
            and not (
                definition is not None
                and isinstance(definition.model_policy.route, ModelRoute)
            )
        ):
            provider_override = options.provider_name
        provider_name = provider_override or self._resolve_provider(
            launch, definition, None
        )
        provider = self._providers.provider_config(provider_name)
        model_config = self._resolve_model_config(
            provider, definition, model=model_override
        )
        if effort_override is not None:
            if effort_override not in (model_config.effort or ()):
                raise ValueError(
                    f"Saved effort {effort_override!r} is unavailable for {provider_name}/{model_config.model}"
                )
            model_config = model_config.model_copy(
                update={"reasoning_effort": effort_override}
            )
        self._restrict_tools(self._tools, config, definition)
        runtime_selection = self._resolved_runtime_selection(
            config=config,
            definition=definition,
            provider_name=provider_name,
            model_config=model_config,
        )
        if effort_override is not None:
            runtime_selection = runtime_selection.model_copy(
                update={
                    "model": runtime_selection.model.model_copy(
                        update={
                            "generation": runtime_selection.model.generation.model_copy(
                                update={
                                    "mode": ReasoningGenerationMode(
                                        effort=effort_override
                                    )
                                }
                            )
                        }
                    )
                }
            )
        metadata = ThreadMetadata(
            runtime_selection=runtime_selection,
            provider_override=provider_override,
            model_override=model_override,
            effort_override=effort_override,
            parent_thread_id=launch.parent_thread_id,
            workspace_root=str(launch.workspace_root),
            title=stored_metadata.title
            if stored_metadata is not None
            else self._session_title,
        )
        if isinstance(metadata_lifecycle, MetadataUninitialized):
            await state.metadata.initialize(metadata)
        else:
            await state.metadata.replace(metadata)

        self._model_is_override = options.model_override is not None
        if options.model_override is not None:
            await self._model.replace_override(options.model_override)
        else:
            await self._model.replace(self._providers.acquire(provider, model_config))
        self._log.info(
            "agent.created",
            session_id=launch.session_id,
            thread_id=launch.thread_id,
            agent=runtime_selection.agent_name,
            provider=provider_name,
            model=model_config.model,
            context_window=runtime_selection.model.context_window,
            tools_enabled=len(self._tools.enabled()),
            resumed=state.resumed,
        )

    async def announce_initialized(self) -> None:
        """Notify fully mounted plugins after the dependency graph is running."""
        definition = self.active_definition()
        await self._events.emit(
            THREAD_INITIALIZED,
            ThreadInitialized(
                session=self._state.session,
                metadata=self._state.metadata.value,
            ),
        )
        self._restrict_tools(self._tools, self.runtime_config(), definition)
        current = self._state.metadata.value.runtime_selection
        await self._state.metadata.replace_runtime_selection(
            current.model_copy(
                update={
                    "enabled_tools": tuple(tool.name for tool in self._tools.enabled())
                }
            )
        )

    def definition(self, name: str) -> AgentDefinition | None:
        return self.catalog.get(name)

    def _require_engine(self) -> AgentLoopPort:
        return self._events.require(AGENT_LOOP)

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
        overrides: dict[str, str | None] | None = None,
    ) -> None:
        current = self._state.metadata.value.runtime_selection
        selection = current.model_copy(
            update={
                "agent_name": current.agent_name if agent_name is None else agent_name,
                "model": current.model.model_copy(
                    update={
                        "route": ModelRoute(provider=provider, model=model),
                        "generation": current.model.generation.model_copy(
                            update={
                                "mode": (
                                    ReasoningGenerationMode(effort=model_mode)
                                    if model_mode
                                    else StandardGenerationMode()
                                ),
                                "max_output_tokens": max(1, max_output_tokens),
                            }
                        ),
                        "context_window": max(1, context_window),
                    }
                ),
            }
        )
        await self._state.metadata.replace(
            self._state.metadata.value.model_copy(
                update={"runtime_selection": selection, **(overrides or {})}
            )
        )

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

    def runtime_config(self) -> RuntimeConfig:
        """Resolve the current instructions and tool configuration."""
        state = self._state
        config = self._runtime_config(
            self._settings.load_plugin_tree(
                state.session.workspace_root,
                state.session.session_id,
                thread_id=state.session.thread_id,
            )
        )
        return config

    async def activate(self, name: str) -> AgentSelection:
        """Atomically apply a registered primary Agent to the live driver."""
        definition = self.catalog.get(name)
        if definition is None or definition.mode == "subagent":
            raise ValueError(f"Unknown primary Agent: {name}")

        engine = self._require_engine()
        state = self._state
        config = self.runtime_config()
        current = state.metadata.value.runtime_selection
        policy_route = definition.model_policy.route
        saved = state.metadata.value
        provider_name = (
            policy_route.provider
            if isinstance(policy_route, ModelRoute)
            else (saved.provider_override or self._providers.default_name())
        )
        provider = self._providers.provider_config(provider_name)
        model_config = self._resolve_model_config(provider, definition)
        # After handoff, finish recording the new selection before reporting
        # an error or cancellation from releasing the previous client.
        replace_error = None
        if not self._model_is_override:
            use = self._providers.acquire(provider, model_config)
            try:
                await self._model.replace(use)
            except BaseException as exc:
                replace_error = exc

        self._restrict_tools(self._tools, config, definition)
        engine.configure(
            model_client=self._model,
            max_iterations=definition.limits.max_turns,
        )
        runtime = self._resolved_runtime_selection(
            config=config,
            definition=definition,
            provider_name=provider_name,
            model_config=model_config,
        )
        await state.metadata.replace(
            state.metadata.value.model_copy(
                update={
                    "runtime_selection": runtime,
                    "provider_override": None
                    if isinstance(policy_route, ModelRoute)
                    else saved.provider_override,
                    "model_override": None,
                    "effort_override": None,
                }
            )
        )
        if replace_error is not None:
            raise replace_error
        await self._events.emit(
            AGENT_CONFIGURED,
            AgentConfigured(
                runtime_selection=runtime,
                session_key=SessionKey(
                    session_id=state.session.session_id,
                    thread_id=state.session.thread_id,
                ),
            ),
        )
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
        provider = self._providers.provider_config(name)
        model_config = provider.resolve(model)
        if model_config.max_output_tokens is None:
            raise ValueError(
                f"Model {name}/{model_config.model} must declare "
                "max_output_tokens before it can become a runtime selection"
            )
        replace_error = None
        if not self._model_is_override:
            use = self._providers.acquire(provider, model_config)
            try:
                await self._model.replace(use)
            except BaseException as exc:
                replace_error = exc
        engine.configure(
            model_client=self._model,
        )
        await self._replace_runtime_selection(
            provider=name,
            model=model_config.model,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens,
            overrides={
                "provider_override": name,
                "model_override": model,
                "effort_override": None,
            },
        )
        if replace_error is not None:
            raise replace_error
        await self._events.emit(
            AGENT_CONFIGURED,
            AgentConfigured(
                runtime_selection=state.metadata.value.runtime_selection,
                session_key=SessionKey(
                    session_id=state.session.session_id,
                    thread_id=state.session.thread_id,
                ),
            ),
        )
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
        entry = self._providers.provider_config(provider_name)
        model_config = entry.resolve(model_name)
        tiers = list(model_config.effort or [])
        if not tiers:
            raise ValueError(
                f"Model {provider_name}/{model_name} does not advertise effort tiers"
            )
        if value not in tiers:
            raise ValueError(
                f"Unsupported reasoning effort {value!r} for "
                f"{provider_name}/{model_name}; available: {', '.join(tiers)}"
            )
        model_config = model_config.model_copy(update={"reasoning_effort": value})
        if model_config.max_output_tokens is None:
            raise ValueError(
                f"Model {provider_name}/{model_name} must declare "
                "max_output_tokens before it can become a runtime selection"
            )
        replace_error = None
        if not self._model_is_override:
            use = self._providers.acquire(entry, model_config)
            try:
                await self._model.replace(use)
            except BaseException as exc:
                replace_error = exc
        engine.configure(
            model_client=self._model,
        )
        await self._replace_runtime_selection(
            provider=provider_name,
            model=model_name,
            model_mode=model_config.model_mode,
            context_window=model_config.max_context_tokens,
            max_output_tokens=model_config.max_output_tokens,
            overrides={"effort_override": value},
        )
        if replace_error is not None:
            raise replace_error
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
        launch: ThreadInfo,
        metadata,
    ) -> AgentDefinition | None:
        definition = options.agent_definition
        stored_name = metadata.agent_ref if metadata is not None else None

        selected = options.selected_agent
        if selected is not None and stored_name is not None and selected != stored_name:
            raise ValueError(
                f"Thread {launch.thread_id!r} belongs to Agent {stored_name!r}, "
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
                    registered.mode == "subagent" and not launch.is_subagent
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
            and not launch.is_subagent
        ):
            raise ValueError(f"Unknown primary agent: {definition.name}")
        return definition

    def _resolve_provider(
        self,
        launch: ThreadInfo,
        definition: AgentDefinition | None,
        metadata,
    ) -> str:
        provider_name = self._provider_name
        if definition is not None and isinstance(
            definition.model_policy.route,
            ModelRoute,
        ):
            provider_name = definition.model_policy.route.provider
        return (
            provider_name
            if provider_name is not None
            else self._providers.default_name()
        )

    def _runtime_config(self, tree: PluginTree) -> RuntimeConfig:
        """Build the Agent-owned runtime view from generic plugin entries.

        Configuration loading only resolves the layered tree.  The Agent
        composition boundary validates developer instructions and tool
        configuration before constructing the runtime selection.
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
            agent_name=(definition.name if definition is not None else "XBotv2"),
            prompt=self._resolved_prompt(config, definition),
            limits=(
                definition.limits if definition is not None else AgentExecutionLimits()
            ),
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
        *,
        model: str | None = None,
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
            model
            if model is not None
            else route.model
            if isinstance(route, ModelRoute)
            else None
        )
        if definition is not None:
            updates: dict[str, JsonValue] = {}
            if definition.model_policy.temperature is not None:
                updates["temperature"] = definition.model_policy.temperature
            if definition.model_policy.max_output_tokens is not None:
                updates["max_output_tokens"] = definition.model_policy.max_output_tokens
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
