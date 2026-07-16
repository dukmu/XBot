"""Agent definition and registry contract tests."""

from pathlib import Path

import pytest

from xbotv2.api import AgentDefinition
from xbotv2.core.agents import AgentRegistry
from xbotv2.tools.permissions import PermissionSystem
from builtin_plugins.agents.plugin import _load_definition


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


def test_shipped_explorer_definition_is_read_only():
    definition = _load_definition(
        Path(__file__).parents[2] / "data" / ".agents" / "Explorer.md"
    )

    assert definition.mode == "all"
    assert definition.tools == (
        "filesystem_read",
        "filesystem_list",
        "search_text",
        "find_files",
        "ask_user",
    )
    permissions = PermissionSystem(definition.permissions)
    assert permissions.check("filesystem_write") == "deny"
    assert permissions.check("shell") == "deny"
    assert permissions.check("task") == "deny"
    assert permissions.check("filesystem_read") == "ask"


def test_shipped_default_definition_is_primary_capable():
    definition = _load_definition(
        Path(__file__).parents[2] / "data" / ".agents" / "default.md"
    )

    assert definition.name == "default"
    assert definition.mode == "all"
    assert definition.tools is None
