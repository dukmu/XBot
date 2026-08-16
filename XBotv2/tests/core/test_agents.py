"""Agent definition and registry contract tests."""

import pytest

from XBotv2.agents.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.core import AgentDefinition, RuntimeVariables
from XBotv2.tools.agents import AgentRegistry
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agents.plugin import _load_definition


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


def test_builtin_agents_cover_default_and_explorer():
    by_name = {definition.name: definition for definition in BUILTIN_AGENT_DEFINITIONS}
    assert set(by_name) == {"default", "Explorer"}
    assert by_name["default"].mode == "all"
    assert by_name["Explorer"].mode == "all"


def test_builtin_explorer_definition_is_read_only():
    definition = next(
        item for item in BUILTIN_AGENT_DEFINITIONS if item.name == "Explorer"
    )

    assert definition.mode == "all"
    assert "filesystem_stat" in definition.tools
    assert "content_read" in definition.tools
    permissions = PermissionSystem(definition.permissions)
    assert permissions.check("filesystem_write") == "deny"
    assert permissions.check("shell") == "deny"
    assert permissions.check("spawn_subagent") == "deny"
    assert permissions.check("wait_subagent") == "deny"
    assert permissions.check("filesystem_read") == "ask"


def test_builtin_default_definition_is_primary_capable():
    definition = next(
        item for item in BUILTIN_AGENT_DEFINITIONS if item.name == "default"
    )

    assert definition.name == "default"
    assert definition.mode == "all"
    assert definition.tools is None


def test_agent_markdown_expands_prompt_but_preserves_permission_variables(tmp_path):
    path = tmp_path / "reviewer.md"
    path.write_text(
        "---\n"
        "description: Reviewer\n"
        "permissions:\n"
        "  allow:\n"
        "    - tool: filesystem_read\n"
        "      paths: ${workspace}\n"
        "---\n"
        "```var\n"
        "${tool_results}\n"
        "```\n",
        encoding="utf-8",
    )
    variables = RuntimeVariables({
        "workspace": tmp_path / "workspace",
        "tool_results": tmp_path / "state" / "artifacts" / "tool_results",
    })

    definition = _load_definition(path, variables)

    assert definition.prompt == str(tmp_path / "state/artifacts/tool_results")
    assert definition.permissions["allow"][0]["paths"] == "${workspace}"
