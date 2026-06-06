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
    # "clear" starts with "c" (rank 0); "help" contains "c" in its short
    # label ("commands") and ranks after.
    assert names[0] == "clear"
    assert "help" in names
    # "exit" short_label has no "c" so it is not in the result.
    assert "exit" not in names


def test_search_commands_slash_prefix_cle_matches_clear_only() -> None:
    results = search_commands("/cle")
    assert [spec.name for spec in results] == ["clear"]


def test_search_commands_slash_prefix_ex_matches_exit_only() -> None:
    results = search_commands("/ex")
    assert [spec.name for spec in results] == ["exit"]


def test_search_commands_slash_prefix_st_returns_status() -> None:
    register_server_commands([
        {"name": "status", "slash": "/status", "description": "show current status"}
    ])

    results = search_commands("/st")
    # "status" starts with "st" (rank 0); "help" contains "st"
    # via "list" and "slash" (rank 2, substring fallback).
    assert results[0].name == "status"
    assert any(spec.name == "help" for spec in results)


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
    # Both words must appear in the short label.
    results = search_commands("clear event")
    assert [spec.name for spec in results] == ["clear"]


def test_search_commands_palette_query_no_match() -> None:
    assert search_commands("totally unknown") == []


def test_search_commands_palette_query_returns_only_matching() -> None:
    results = search_commands("quit")
    assert [spec.name for spec in results] == ["exit"]


# ----------------------------------------------------------------------
# complete_command
# ----------------------------------------------------------------------


def test_complete_command_returns_first_match() -> None:
    spec = complete_command("/c")
    assert spec is not None
    assert spec.name == "clear"


def test_complete_command_handles_aliases_via_canonical() -> None:
    # "/q" maps to exit via alias; the completion still resolves to exit.
    spec = complete_command("/q")
    assert spec is not None
    assert spec.name == "exit"


def test_complete_command_returns_none_for_no_match() -> None:
    assert complete_command("/xyz") is None


def test_complete_command_returns_none_without_slash() -> None:
    assert complete_command("clear") is None


# ----------------------------------------------------------------------
# Backward-compat smoke tests
# ----------------------------------------------------------------------


def test_parse_slash_command_still_works() -> None:
    spec = parse_slash_command("/help")
    assert isinstance(spec, CommandSpec)
    assert spec.name == "help"


def test_known_command_labels_preserves_stable_order() -> None:
    labels = known_command_labels()
    assert len(labels) == 3
    assert labels[0].startswith("/help")
