"""Unit tests for the slash command registry and search behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from XBotv2.core.commands import Command, CommandResult

from XBotv2.tui.command import CommandRegistry


def _registry() -> CommandRegistry:
    return CommandRegistry.default()


# ----------------------------------------------------------------------
# Slash completion
# ----------------------------------------------------------------------


def test_search_commands_empty_query_returns_all_in_stable_order() -> None:
    results = _registry().search("")
    assert [spec.name for spec in results] == [
        "help",
        "clear-screen",
        "thinking",
        "details",
        "attach",
        "exit",
    ]


def test_search_commands_whitespace_only_query_returns_all() -> None:
    assert len(_registry().search("   ")) == 6


def test_search_commands_slash_prefix_filters_by_name() -> None:
    results = _registry().search("/c")
    names = [spec.name for spec in results]
    assert names[0] == "clear-screen"


def test_search_commands_slash_prefix_st_returns_status() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])
    results = registry.search("/st")
    assert results[0].name == "status"


def test_server_command_alias_ignores_usage_parameters() -> None:
    registry = _registry()
    registry.merge_server([
        {
            "name": "agent",
            "slash": "/agent [list|status|use <name>]",
            "description": "switch Agent",
        }
    ])

    spec = registry.parse("/agent list")

    assert spec is not None
    assert spec.name == "agent"
    assert spec.kind == "server"
    assert spec.args == "list"


def test_search_commands_slash_prefix_no_match_returns_empty() -> None:
    assert _registry().search("/xyz") == []


def test_search_commands_is_case_insensitive() -> None:
    registry = _registry()
    lower = [s.name for s in registry.search("/c")]
    upper = [s.name for s in registry.search("/C")]
    assert lower == upper


def test_search_commands_falls_back_to_substring() -> None:
    results = _registry().search("/h")
    names = [spec.name for spec in results]
    assert "help" in names
    assert "clear-screen" in names


def test_search_commands_deduplicates_results() -> None:
    results = _registry().search("/")
    names = [spec.name for spec in results]
    assert len(names) == len(set(names)) == 6


def test_merge_server_adds_dynamic_completion() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])

    assert [spec.name for spec in registry.search("/st")][0] == "status"
    assert registry.parse("/status").name == "status"


def test_merge_server_replaces_previous_server_catalog() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "old", "slash": "/old", "description": "old command"}
    ])
    registry.merge_server([
        {"name": "new", "slash": "/new", "description": "new command"}
    ])

    assert registry.parse("/old").name == "unknown"
    assert registry.parse("/new").name == "new"


def test_server_catalog_cannot_override_client_commands() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "help", "slash": "/help", "description": "remote help"},
        {"name": "remote", "slash": "/q", "description": "remote alias"},
    ])

    assert registry.parse("/help").kind == "client"
    assert registry.parse("/q").name == "exit"


# ----------------------------------------------------------------------
# Fuzzy palette search
# ----------------------------------------------------------------------


def test_search_commands_palette_query_finds_help() -> None:
    results = _registry().search("help")
    assert any(spec.name == "help" for spec in results)


def test_search_commands_palette_query_word_match() -> None:
    results = _registry().search("clear transcript")
    assert [spec.name for spec in results] == ["clear-screen"]


def test_search_commands_palette_query_no_match() -> None:
    assert _registry().search("totally unknown") == []


def test_search_commands_palette_query_returns_only_matching() -> None:
    results = _registry().search("quit")
    assert [spec.name for spec in results] == ["exit"]


def test_labels_preserves_stable_order() -> None:
    labels = _registry().labels()
    assert labels[0].startswith("help")
    assert any("exit" in label for label in labels)


# ----------------------------------------------------------------------
# CommandSpec kind field
# ----------------------------------------------------------------------


def test_command_spec_has_kind() -> None:
    spec = _registry().parse("/help")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.description == "Show commands or detailed help for one command"
    assert spec.parameters["[command-name]"] == "Optional command name"


def test_server_command_has_kind_server() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "deploy", "slash": "/deploy", "description": "deploy app",
         "parameters": {"--env": "target environment"}}
    ])
    spec = registry.parse("/deploy")
    assert spec is not None
    assert spec.kind == "server"
    assert spec.parameters["--env"] == "target environment"


def test_register_prompt_commands() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
        {"name": "code-review", "description": "Review code", "kind": "prompt"},
    ])

    spec = registry.parse("/git-release")
    assert spec is not None
    assert spec.kind == "prompt"
    assert spec.description == "Create releases"

    spec2 = registry.parse("/code-review")
    assert spec2 is not None
    assert spec2.kind == "prompt"


# ----------------------------------------------------------------------
# get_command
# ----------------------------------------------------------------------


def test_get_command_returns_client_command() -> None:
    spec = _registry().get("help")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.name == "help"


def test_get_command_returns_server_command() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "deploy", "slash": "/deploy", "description": "deploy app"}
    ])
    spec = registry.get("deploy")
    assert spec is not None
    assert spec.kind == "server"


def test_get_command_returns_none_for_unknown() -> None:
    assert _registry().get("nonexistent") is None


# ----------------------------------------------------------------------
# parse detaches kind from CommandSpec
# ----------------------------------------------------------------------


def test_parse_preserves_args_for_skill() -> None:
    registry = _registry()
    registry.merge_server([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    spec = registry.parse("/git-release Create v2.1.0")
    assert spec is not None
    assert spec.name == "git-release"
    assert spec.kind == "prompt"
    assert spec.args == "Create v2.1.0"


@pytest.mark.asyncio
async def test_plugin_command_registry_owns_server_dispatch() -> None:
    from XBotv2.protocol.commands import execute_command

    async def handler(_ctx, raw_args):
        return CommandResult(f"sample:{raw_args}")

    extension = Command(
        name="sample",
        description="Sample extension command.",
        handler=handler,
    )
    loader = SimpleNamespace(
        get_command=lambda name: extension if name == "sample" else None,
        status_slots=lambda: {},
    )
    ctx = SimpleNamespace(
        engine=SimpleNamespace(plugin_loader=loader),
        services=SimpleNamespace(
            get=lambda name: loader if name == "loader" else None
        ),
    )

    result = await execute_command(ctx, "sample", ["a", "b"], raw_args="a b")

    assert result["data"]["message"] == "sample:a b"
