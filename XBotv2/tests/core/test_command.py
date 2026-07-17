"""Unit tests for the slash command registry extensions (v1.1).

Covers the new ``search_commands`` / ``complete_command`` API that
drives the composer completion popup and the command palette.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xbotv2.api.commands import Command, CommandResult

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
    assert [spec.name for spec in results] == [
        "help",
        "status",
        "provider",
        "agent",
        "clear",
        "undo",
        "fork",
        "tasks",
        "task",
        "permission",
        "sandbox",
        "clear-screen",
        "thinking",
        "details",
        "exit",
    ]


def test_search_commands_whitespace_only_query_returns_all() -> None:
    assert len(search_commands("   ")) == 15


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


def test_server_command_alias_ignores_usage_parameters() -> None:
    register_server_commands([
        {
            "name": "agent",
            "slash": "/agent [list|status|use <name>]",
            "description": "switch Agent",
        }
    ])

    spec = parse_slash_command("/agent list")

    assert spec is not None
    assert spec.name == "agent"
    assert spec.kind == "client"
    assert spec.args == "list"


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
    assert "clear-screen" in names


def test_search_commands_deduplicates_results() -> None:
    results = search_commands("/")
    names = [spec.name for spec in results]
    assert len(names) == len(set(names)) == 15


def test_register_server_commands_adds_dynamic_completion() -> None:
    register_server_commands([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])

    assert [spec.name for spec in search_commands("/st")][0] == "status"
    assert parse_slash_command("/status").name == "status"


def test_register_server_commands_replaces_previous_server_catalog() -> None:
    register_server_commands([
        {"name": "old", "slash": "/old", "description": "old command"}
    ])
    register_server_commands([
        {"name": "new", "slash": "/new", "description": "new command"}
    ])

    assert parse_slash_command("/old").name == "unknown"
    assert parse_slash_command("/new").name == "new"


def test_server_catalog_cannot_override_client_commands() -> None:
    register_server_commands([
        {"name": "help", "slash": "/help", "description": "remote help"},
        {"name": "remote", "slash": "/q", "description": "remote alias"},
    ])

    assert parse_slash_command("/help").kind == "client"
    assert parse_slash_command("/q").name == "exit"


# ----------------------------------------------------------------------
# Fuzzy palette search
# ----------------------------------------------------------------------


def test_search_commands_palette_query_finds_help() -> None:
    results = search_commands("help")
    assert any(spec.name == "help" for spec in results)


def test_search_commands_palette_query_word_match() -> None:
    results = search_commands("clear transcript")
    assert [spec.name for spec in results] == ["clear-screen"]


def test_search_commands_palette_query_no_match() -> None:
    assert search_commands("totally unknown") == []


def test_search_commands_palette_query_returns_only_matching() -> None:
    results = search_commands("quit")
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
    assert spec.description == "Show commands or detailed help for one command"
    assert spec.parameters["[command-name]"] == "Optional command name"


def test_server_command_has_kind_server() -> None:
    register_server_commands([
        {"name": "deploy", "slash": "/deploy", "description": "deploy app",
         "parameters": {"--env": "target environment"}}
    ])
    spec = parse_slash_command("/deploy")
    assert spec is not None
    assert spec.kind == "server"
    assert spec.parameters["--env"] == "target environment"


def test_register_prompt_commands() -> None:
    register_server_commands([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
        {"name": "code-review", "description": "Review code", "kind": "prompt"},
    ])

    spec = parse_slash_command("/git-release")
    assert spec is not None
    assert spec.kind == "prompt"
    assert spec.description == "Create releases"

    spec2 = parse_slash_command("/code-review")
    assert spec2 is not None
    assert spec2.kind == "prompt"


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
    register_server_commands([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    spec = complete_command("/git-release")
    assert spec is not None
    assert spec.name == "git-release"
    assert spec.kind == "prompt"


# ----------------------------------------------------------------------
# parse_slash_command detaches kind from CommandSpec
# ----------------------------------------------------------------------


def test_parse_slash_command_preserves_args_for_skill() -> None:
    register_server_commands([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    spec = parse_slash_command("/git-release Create v2.1.0")
    assert spec is not None
    assert spec.name == "git-release"
    assert spec.kind == "prompt"
    assert spec.args == "Create v2.1.0"


@pytest.mark.asyncio
async def test_plugin_command_registry_owns_server_dispatch() -> None:
    from xbotv2.protocol.commands import execute_command

    async def handler(_ctx, raw_args):
        return CommandResult(f"sample:{raw_args}")

    extension = Command(
        name="sample",
        description="Sample extension command.",
        handler=handler,
    )
    loader = SimpleNamespace(get_command=lambda name: extension if name == "sample" else None)
    ctx = SimpleNamespace(engine=SimpleNamespace(plugin_loader=loader))

    result = await execute_command(ctx, "sample", ["a", "b"], raw_args="a b")

    assert result["data"]["message"] == "sample:a b"


@pytest.mark.asyncio
async def test_task_commands_use_typed_transport_operations(tmp_path) -> None:
    from xbotv2.tui.terminal import TerminalSession

    task = {
                "task_id": "task-1",
                "kind": "shell",
                "status": "running",
                "command": "sleep 30",
    }
    transport = SimpleNamespace(
        list_tasks=AsyncMock(return_value={"tasks": [task]}),
        stop_task=AsyncMock(return_value={"matched_count": 1, "tasks": [task]}),
        stop_all_tasks=AsyncMock(
            return_value={"matched_count": 1, "tasks": [task]}
        ),
    )
    session = TerminalSession(
        session_id="s",
        thread_id="t",
        workspace_root=tmp_path,
        transport=transport,
    )

    listed = await session.run_builtin_command("tasks", ["ps"])
    stopped = await session.run_builtin_command("task", ["stop", "task-1"])
    stopped_all = await session.run_builtin_command("task", ["stopall"])

    assert listed.data["tasks"][0]["task_id"] == "task-1"
    assert stopped.message == "Stopped background task task-1."
    assert stopped_all.message == "Stopped 1 background task(s)."


@pytest.mark.asyncio
async def test_history_builtins_use_typed_transport_operations(tmp_path) -> None:
    from xbotv2.tui.terminal import TerminalSession

    transport = SimpleNamespace(
        clear_history=AsyncMock(
            return_value={"removed_turns": 2, "messages": []}
        ),
        undo_history=AsyncMock(
            return_value={
                "removed_turns": 1,
                "messages": [{"role": "user", "content": "kept"}],
            }
        ),
        fork_session=AsyncMock(
            return_value={"session_id": "forked", "source_session_id": "s"}
        ),
    )
    session = TerminalSession(
        session_id="s",
        thread_id="t",
        workspace_root=tmp_path,
        transport=transport,
    )

    cleared = await session.run_builtin_command("clear", [])
    undone = await session.run_builtin_command("undo", ["1"])
    forked = await session.run_builtin_command("fork", [])

    assert cleared.history == []
    assert undone.history == [{"role": "user", "content": "kept"}]
    assert forked.data["session_id"] == "forked"
    transport.undo_history.assert_awaited_once_with(
        session_id="s", thread_id="t", count=1
    )


@pytest.mark.asyncio
async def test_policy_builtins_use_session_policy_resource(tmp_path) -> None:
    from xbotv2.tui.terminal import TerminalSession

    transport = SimpleNamespace(
        get_session_policy=AsyncMock(
            return_value={"permissions": {}, "sandbox": {}}
        ),
        update_session_policy=AsyncMock(
            return_value={
                "permissions": {"allow": [{"tool": "shell"}]},
                "sandbox": {},
            }
        ),
    )
    session = TerminalSession(
        session_id="s",
        thread_id="t",
        workspace_root=tmp_path,
        transport=transport,
    )

    status = await session.run_builtin_command("permission", [])
    await session.run_builtin_command("permission", ["set", "shell", "allow"])
    await session.run_builtin_command("permission", ["reset", "shell"])

    assert status.message == "Session permission policy: {}"
    assert transport.update_session_policy.await_args_list[0].kwargs == {
        "session_id": "s",
        "permissions": {"shell": "allow"},
    }
    assert transport.update_session_policy.await_args_list[1].kwargs == {
        "session_id": "s",
        "remove_permissions": ["shell"],
    }
