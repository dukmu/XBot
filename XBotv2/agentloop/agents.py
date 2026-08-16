"""Engine-side helpers: apply a resolved Agent definition.

These helpers turn an :class:`~XBotv2.core.agents.AgentDefinition` into
runtime state — base config fields, provider model settings, and the
model-visible tool set — during agent-loop assembly (``agentloop/plugin.py``)
and the composition root.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.policy import merge_permission_config
from XBotv2.core.agents import AgentDefinition


def apply_agent_definition(config: Any, definition: AgentDefinition) -> None:
    """Apply one resolved Agent definition to a fresh base configuration."""
    config.agent_name = definition.name
    config.agent_role = definition.description
    config.agent_instructions = definition.prompt
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
        provider.max_output_tokens = definition.max_output_tokens


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


__all__ = ["_restore_agent_definition", "apply_agent_definition", "apply_agent_provider", "apply_agent_tools"]


def _restore_agent_definition(data: dict[str, Any]) -> AgentDefinition:
    """Rebuild an AgentDefinition from persisted thread metadata."""
    values = dict(data)
    for field_name in ("tools", "disabled_tools"):
        value = values.get(field_name)
        if isinstance(value, list):
            values[field_name] = tuple(str(item) for item in value)
    return AgentDefinition(**values)
