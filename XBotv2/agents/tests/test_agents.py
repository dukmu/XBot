"""Agent definition and registry contract tests."""

import pytest
from XBotv2.agents.impl.builtins import BUILTIN_AGENT_DEFINITIONS
from XBotv2.agents.api.contracts import AgentDefinition
from XBotv2.agentloop.api.contracts import AllTools
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.agents.impl.catalog import AgentCatalog
from XBotv2.permissions.impl.system import PermissionSystem
from XBotv2.agents.impl.loader import load_definition


def test_agent_definition_requires_stable_name_and_description():
    with pytest.raises(ValueError, match="description"):
        AgentDefinition(name="reviewer", description="")
    with pytest.raises(ValueError, match="name"):
        AgentDefinition(name="bad/name", description="Review code")


def test_catalog_enforces_name_ownership():
    catalog = AgentCatalog(ownership="caller")
    definition = AgentDefinition(name="reviewer", description="Review code")

    assert catalog.register(definition) == "reviewer"
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(definition)
    assert catalog.get("reviewer") is definition
    assert catalog.unregister_owned("other") == []
    assert catalog.unregister_owned("unknown", overlay=False) == ["reviewer"]


def test_catalog_workspace_overlay_replaces_and_restores_base():
    catalog = AgentCatalog(ownership="caller")
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
    catalog = AgentCatalog(ownership="caller")
    base = AgentDefinition(name="reviewer", description="Base reviewer")
    overlay = AgentDefinition(name="reviewer", description="Workspace reviewer")

    catalog.register(base)
    catalog.register(overlay, overlay=True)

    # Removing the base owner leaves the independent workspace overlay.
    assert catalog.unregister_owned("unknown", overlay=False) == ["reviewer"]
    assert catalog.get("reviewer") is overlay
    assert catalog.unregister_owned("unknown") == ["reviewer"]
    assert catalog.get("reviewer") is None


def test_catalog_layer_rejects_duplicate_names():
    catalog = AgentCatalog(ownership="caller")
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
    assert definition.tool_policy.enabled == ("read", "search", "ask_user")
    permissions = PermissionSystem(definition.permission_policy)
    assert permissions.check("edit") == "deny"
    assert permissions.check("path") == "deny"
    assert permissions.check("shell") == "deny"
    assert permissions.check("spawn_subagent") == "deny"
    assert permissions.check("wait_subagent") == "deny"
    assert permissions.check("read") == "allow"


def test_builtin_default_definition_is_primary_capable():
    definition = next(
        item for item in BUILTIN_AGENT_DEFINITIONS if item.name == "default"
    )

    assert definition.name == "default"
    assert definition.mode == "all"
    assert isinstance(definition.tool_policy.enabled, AllTools)


def test_agent_frontmatter_expands_permission_config_before_typed_loading(tmp_path):
    path = tmp_path / "reviewer.md"
    path.write_text(
        "---\n"
        "description: Reviewer\n"
        "permission_policy:\n"
        "  rules:\n"
        "    - tool_pattern: filesystem_read\n"
        "      path_scope: '$${workspace}/.*\\.md'\n"
        "      decision: allow\n"
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
    assert definition.permission_policy.rules[0].path_scope == f"{tmp_path}/workspace/.*\\.md"


def test_sources_share_unexpanded_data_and_invalidate(tmp_path, monkeypatch):
    from XBotv2.agents.impl import loader

    path = tmp_path / "worker.md"
    path.write_text(
        '---\ndescription: "Worker $${thread_id}"\n---\n'
        '```var\n${thread_id}\n```\n', encoding="utf-8"
    )
    sources = loader.AgentSources(max_bytes=100_000)
    original = loader._parse_source
    calls = []

    def parse(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(loader, "_parse_source", parse)
    first = AgentCatalog(ownership="caller", sources=sources)
    second = AgentCatalog(ownership="caller", sources=sources)
    first.register_markdown(tmp_path, variables=RuntimeVariables({"thread_id": "one"}))
    second.register_markdown(tmp_path, variables=RuntimeVariables({"thread_id": "two"}))
    assert len(calls) == 1
    assert (first.get("worker").description, first.get("worker").prompt) == ("Worker one", "one")
    assert (second.get("worker").description, second.get("worker").prompt) == ("Worker two", "two")
    path.write_text('---\ndescription: Changed\n---\nNew text\n', encoding="utf-8")
    assert loader.load_definition(path, sources=sources).description == "Changed"
    assert len(calls) == 2 and len(sources._entries) == 1
    path.unlink()
    assert loader.load_definitions(tmp_path, sources=sources) == []
    assert not sources._entries
    assert first.get("worker").prompt == "one"


def test_source_cache_budget_and_uncached_callers(tmp_path, monkeypatch):
    from XBotv2.agents.impl import loader

    a, b = (tmp_path / f"{name}.md" for name in ("a", "b"))
    for path in (a, b):
        path.write_text('---\ndescription: Worker\n---\n' + 'x' * 50, encoding="utf-8")
    original = loader._parse_source
    calls = []

    def parse(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(loader, "_parse_source", parse)
    sources = loader.AgentSources(max_bytes=7_000)
    loader.load_definition(a, sources=sources)
    loader.load_definition(b, sources=sources)
    loader.load_definition(a, sources=sources)
    assert calls == [a, b, a]
    tiny = loader.AgentSources(max_bytes=100)
    loader.load_definition(a, sources=tiny)
    loader.load_definition(a, sources=tiny)
    loader.load_definition(a)
    loader.load_definition(a)
    assert calls == [a, b, a, a, a, a, a]
    assert not tiny._entries


async def test_process_close_releases_source_cache(tmp_path):
    from xcore.api.context import Context
    from XBotv2.agents.impl.loader import load_definition
    from XBotv2.agents.impl.plugin import _AGENT_SOURCES, mount_process

    root = Context()
    mount_process(root, None)
    sources = root.require(_AGENT_SOURCES)
    path = tmp_path / "worker.md"
    path.write_text('---\ndescription: Worker\n---\nPrompt', encoding="utf-8")
    load_definition(path, sources=sources)
    assert sources._entries
    await root.close()
    assert not sources._entries
