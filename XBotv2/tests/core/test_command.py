"""Unit tests for the slash command registry extensions (v1.1).

Covers the new ``search_commands`` / ``complete_command`` API that
drives the composer completion popup and the command palette.
"""

from __future__ import annotations

import pytest

from xbotv2.tui.command import (
    CommandSpec,
    complete_command,
    known_command_labels,
    parse_slash_command,
    register_server_commands,
    search_commands,
)


@pytest.fixture(autouse=True)
def restore_command_registry(monkeypatch):
    from xbotv2.tui import command

    monkeypatch.setattr(command, "_ALIASES", dict(command._ALIASES))
    monkeypatch.setattr(command, "_COMMANDS", dict(command._COMMANDS))
    monkeypatch.setattr(command, "_SEARCH_ORDER", list(command._SEARCH_ORDER))


# ----------------------------------------------------------------------
# Slash completion
# ----------------------------------------------------------------------


def test_search_commands_empty_query_returns_all_in_stable_order() -> None:
    results = search_commands("")
    assert [spec.name for spec in results] == ["help", "clear", "exit"]


def test_search_commands_whitespace_only_query_returns_all() -> None:
    assert len(search_commands("   ")) == 3


def test_search_commands_slash_prefix_filters_by_name() -> None:
    results = search_commands("/c")
    names = [spec.name for spec in results]
    assert names[0] == "clear"
    assert "help" in names


def test_search_commands_slash_prefix_st_returns_status() -> None:
    register_server_commands([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])
    results = search_commands("/st")
    assert results[0].name == "status"


def test_search_commands_slash_prefix_no_match_returns_empty() -> None:
    assert search_commands("/xyz") == []


def test_search_commands_is_case_insensitive() -> None:
    lower = [s.name for s in search_commands("/c")]
    upper = [s.name for s in search_commands("/C")]
    assert lower == upper


def test_search_commands_falls_back_to_substring() -> None:
    # "h" matches "help" (prefix) and "clear" (substring via "the").
    results = search_commands("/h")
    names = [spec.name for spec in results]
    assert "help" in names
    assert "clear" in names


def test_search_commands_deduplicates_results() -> None:
    results = search_commands("/")
    names = [spec.name for spec in results]
    assert len(names) == len(set(names)) == 3


def test_register_server_commands_adds_dynamic_completion() -> None:
    register_server_commands([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])

    assert [spec.name for spec in search_commands("/st")][0] == "status"
    assert parse_slash_command("/status").name == "status"


# ----------------------------------------------------------------------
# Fuzzy palette search
# ----------------------------------------------------------------------


def test_search_commands_palette_query_finds_help() -> None:
    results = search_commands("help")
    assert any(spec.name == "help" for spec in results)


def test_search_commands_palette_query_word_match() -> None:
    results = search_commands("clear 事件")
    assert [spec.name for spec in results] == ["clear"]


def test_search_commands_palette_query_no_match() -> None:
    assert search_commands("totally unknown") == []


def test_search_commands_palette_query_returns_only_matching() -> None:
    results = search_commands("退")
    assert [spec.name for spec in results] == ["exit"]


def test_known_command_labels_preserves_stable_order() -> None:
    labels = known_command_labels()
    assert labels[0].startswith("help")
    assert any("exit" in l for l in labels)


# ----------------------------------------------------------------------
# CommandSpec kind field
# ----------------------------------------------------------------------


def test_command_spec_has_kind() -> None:
    spec = parse_slash_command("/help")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.description == "显示帮助信息。用法: /help [command-name]"
    assert spec.parameters["[command-name]"] == "要查看详情的命令名（可选）"


def test_server_command_has_kind_server() -> None:
    register_server_commands([
        {"name": "deploy", "slash": "/deploy", "description": "deploy app",
         "parameters": {"--env": "target environment"}}
    ])
    spec = parse_slash_command("/deploy")
    assert spec is not None
    assert spec.kind == "server"
    assert spec.parameters["--env"] == "target environment"


def test_register_dynamic_commands_skill_kind() -> None:
    from xbotv2.tui.command import register_dynamic_commands

    register_dynamic_commands([
        {"name": "git-release", "description": "Create releases"},
        {"name": "code-review", "description": "Review code"},
    ], "skill")

    spec = parse_slash_command("/git-release")
    assert spec is not None
    assert spec.kind == "skill"
    assert spec.description == "Create releases"

    spec2 = parse_slash_command("/code-review")
    assert spec2 is not None
    assert spec2.kind == "skill"


def test_register_dynamic_commands_mcp_kind() -> None:
    from xbotv2.tui.command import register_dynamic_commands

    register_dynamic_commands([
        {"name": "mcp__github__search", "description": "Search GitHub"},
    ], "mcp")

    spec = parse_slash_command("/mcp__github__search")
    assert spec is not None
    assert spec.kind == "mcp"


# ----------------------------------------------------------------------
# get_command
# ----------------------------------------------------------------------


def test_get_command_returns_client_command() -> None:
    from xbotv2.tui.command import get_command
    spec = get_command("help")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.name == "help"


def test_get_command_returns_server_command() -> None:
    from xbotv2.tui.command import get_command

    register_server_commands([
        {"name": "deploy", "slash": "/deploy", "description": "deploy app"}
    ])
    spec = get_command("deploy")
    assert spec is not None
    assert spec.kind == "server"


def test_get_command_returns_none_for_unknown() -> None:
    from xbotv2.tui.command import get_command
    assert get_command("nonexistent") is None


# ----------------------------------------------------------------------
# complete_command with aliases
# ----------------------------------------------------------------------


def test_complete_command_with_alias_returns_canonical() -> None:
    spec = complete_command("/q")
    assert spec is not None
    assert spec.name == "exit"
    assert spec.kind == "client"


def test_complete_command_skill_alias_exists() -> None:
    from xbotv2.tui.command import register_dynamic_commands

    register_dynamic_commands([
        {"name": "git-release", "description": "Create releases"},
    ], "skill")

    spec = complete_command("/git-release")
    assert spec is not None
    assert spec.name == "git-release"
    assert spec.kind == "skill"


# ----------------------------------------------------------------------
# parse_slash_command detaches kind from CommandSpec
# ----------------------------------------------------------------------


def test_parse_slash_command_preserves_args_for_skill() -> None:
    from xbotv2.tui.command import register_dynamic_commands

    register_dynamic_commands([
        {"name": "git-release", "description": "Create releases"},
    ], "skill")

    spec = parse_slash_command("/git-release Create v2.1.0")
    assert spec is not None
    assert spec.name == "git-release"
    assert spec.kind == "skill"
    assert spec.args == "Create v2.1.0"


@pytest.mark.asyncio
async def test_server_command_registry_owns_metadata_and_dispatch(monkeypatch) -> None:
    from xbotv2.protocol.commands import COMMANDS, ServerCommand, execute_command

    async def handler(ctx, args):
        return {
            "type": "command_result",
            "data": {
                "command": "sample",
                "status": "ok",
                "message": f"{ctx}:{','.join(args)}",
                "data": None,
            },
        }

    monkeypatch.setitem(COMMANDS, "sample", ServerCommand(
        name="sample",
        slash="/sample",
        description="Sample extension command.",
        handler=handler,
    ))

    result = await execute_command("context", "sample", ["a", "b"])

    assert result["data"]["message"] == "context:a,b"
