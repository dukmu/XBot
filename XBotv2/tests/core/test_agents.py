"""Agent definition and registry contract tests."""

import pytest

from XBotv2.agents.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.core import AgentDefinition, RuntimeVariables
from XBotv2.agents.catalog import AgentCatalog
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agents.loader import load_definition


def test_agent_definition_requires_stable_name_and_description():
    with pytest.raises(ValueError, match="description"):
        AgentDefinition(name="reviewer", description="")
    with pytest.raises(ValueError, match="name"):
        AgentDefinition(name="bad/name", description="Review code")


def test_catalog_enforces_name_ownership():
    catalog = AgentCatalog()
    definition = AgentDefinition(name="reviewer", description="Review code")

    assert catalog.register(definition) == "reviewer"
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(definition)
    assert catalog.get("reviewer") is definition
    assert catalog.unregister_owned("other") == []
    assert catalog.unregister_owned("unknown", overlay=False) == ["reviewer"]


def test_catalog_workspace_overlay_replaces_and_restores_base():
    catalog = AgentCatalog()
    base = AgentDefinition(name="reviewer", description="Base reviewer")
    overlay = AgentDefinition(name="reviewer", description="Workspace reviewer")

    catalog.register(base)
    catalog.register(overlay, overlay=True)

    assert catalog.get("reviewer") is overlay
    assert catalog.definitions() == (overlay,)
    # Unloading the overlay reveals the untouched base definition.
    assert catalog.unregister_owned("unknown") == ["reviewer"]
    assert catalog.get("reviewer") is base
    assert catalog.unregister_owned("unknown", overlay=False) == ["reviewer"]
    assert catalog.get("reviewer") is None


def test_catalog_base_unload_keeps_workspace_overlay():
    catalog = AgentCatalog()
    base = AgentDefinition(name="reviewer", description="Base reviewer")
    overlay = AgentDefinition(name="reviewer", description="Workspace reviewer")

    catalog.register(base)
    catalog.register(overlay, overlay=True)

    # Reloading the agents plugin removes its base layer only.
    assert catalog.unregister_owned("unknown", overlay=False) == ["reviewer"]
    assert catalog.get("reviewer") is overlay
    assert catalog.unregister_owned("unknown") == ["reviewer"]
    assert catalog.get("reviewer") is None


def test_catalog_layer_rejects_duplicate_names():
    catalog = AgentCatalog()
    catalog.register(AgentDefinition(name="worker", description="Worker"))
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(AgentDefinition(name="worker", description="Other"))


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
    assert "read" in definition.tools
    assert "search" in definition.tools
    permissions = PermissionSystem(definition.permissions)
    assert permissions.check("edit") == "deny"
    assert permissions.check("path") == "deny"
    assert permissions.check("shell") == "deny"
    assert permissions.check("spawn_subagent") == "deny"
    assert permissions.check("wait_subagent") == "deny"
    assert permissions.check("read") == "ask"


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

    definition = load_definition(path, variables)

    assert definition.prompt == str(tmp_path / "state/artifacts/tool_results")
    assert definition.permissions["allow"][0]["paths"] == "${workspace}"
