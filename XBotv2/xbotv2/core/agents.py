"""Ownership-aware registry for plugin-defined agents."""

from __future__ import annotations

from typing import Any

from xbotv2.api.agents import AgentDefinition
from xbotv2.config.policy import merge_permission_config


class AgentRegistry:
    """Stores immutable Agent definitions under one plugin owner."""

    def __init__(self) -> None:
        self._definitions: dict[str, AgentDefinition] = {}
        self._owners: dict[str, str] = {}

    def register(self, definition: AgentDefinition, *, owner: str) -> str:
        if definition.name in self._definitions:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        self._definitions[definition.name] = definition
        self._owners[definition.name] = owner
        return definition.name

    def unregister(self, name: str, *, owner: str) -> bool:
        if self._owners.get(name) != owner:
            return False
        self._owners.pop(name, None)
        self._definitions.pop(name, None)
        return True

    def get(self, name: str) -> AgentDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions.values())


def apply_agent_definition(config: Any, definition: AgentDefinition) -> None:
    """Apply one resolved Agent definition to a fresh base configuration."""
    config.agent_name = definition.name
    config.agent_role = definition.description
    config.instructions = "\n\n".join(
        part
        for part in (config.instructions, definition.prompt)
        if part.strip()
    )
    if definition.tools is not None:
        config.tools = list(definition.tools)
    if definition.context_window is not None:
        config.max_context_tokens = definition.context_window
    config.permissions = merge_permission_config(
        config.permissions,
        definition.permissions,
    )


def apply_agent_provider(provider: Any, definition: AgentDefinition) -> None:
    """Apply model request settings to a loaded provider configuration."""
    if definition.model is not None:
        provider.model = definition.model
    if definition.temperature is not None:
        provider.temperature = definition.temperature
    if definition.max_output_tokens is not None:
        provider.max_tokens = definition.max_output_tokens


def apply_agent_tools(registry: Any, config: Any, definition: AgentDefinition) -> None:
    """Replace the model-visible tool set for one active Agent."""
    selectors = (
        list(definition.tools)
        if definition.tools is not None
        else list(config.tools) if config.tools else None
    )
    registry.restrict(selectors)
    if definition.disabled_tools:
        registry.exclude(list(definition.disabled_tools))


__all__ = [
    "AgentRegistry",
    "apply_agent_definition",
    "apply_agent_provider",
    "apply_agent_tools",
]
