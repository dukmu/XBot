"""Agent definition and registry contract tests."""

import pytest

from xbotv2.api import AgentDefinition
from xbotv2.core.agents import AgentRegistry


def test_agent_definition_requires_stable_name_and_description():
    with pytest.raises(ValueError, match="description"):
        AgentDefinition(name="reviewer", description="")
    with pytest.raises(ValueError, match="name"):
        AgentDefinition(name="bad/name", description="Review code")


def test_registry_enforces_name_ownership():
    registry = AgentRegistry()
    definition = AgentDefinition(name="reviewer", description="Review code")

    assert registry.register(definition, owner="agents") == "reviewer"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition, owner="other")
    assert not registry.unregister("reviewer", owner="other")
    assert registry.get("reviewer") is definition
    assert registry.unregister("reviewer", owner="agents")
