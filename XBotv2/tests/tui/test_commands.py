"""The slash-command registry: parse, search, and merge the server catalog.

The catalogue entry is the repository's own ``CommandDescription`` -- the same
model the server publishes -- so a client command and a server command are the
same kind of thing, and no field is restated here.

Parsing never guesses: an unknown slash returns the raw text with no match instead
of inventing a command whose name happens to look plausible.
"""

from __future__ import annotations

import pytest

from XBotv2.commands import CommandDescription
from XBotv2.tests.tui.factories import tui_command_registry
from XBotv2.tui.commands import CommandRegistry, ParsedCommand


def registry() -> CommandRegistry:
    return tui_command_registry()


def server_command(name: str, **overrides) -> CommandDescription:
    fields = {
        "name": name,
        "slash": f"/{name}",
        "kind": "server",
        "description": f"the {name} command",
        "usage": f"/{name}",
        "exclusive": False,
    }
    return CommandDescription(**{**fields, **overrides})


# --- the builtins ---------------------------------------------------------


def test_the_builtins_are_declared_in_their_search_order() -> None:
    assert registry().names() == (
        "help",
        "status",
        "settings",
        "session",
        "thread",
        "jobs",
        "provider",
        "model",
        "effort",
        "agent",
        "thinking",
        "details",
        "attach",
        "approve",
        "deny",
        "answer",
        "clear-screen",
        "copy",
        "exit",
    )


def test_settings_documents_the_overlay_entry_point() -> None:
    spec = registry().get("settings")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.slash == "/settings"


def test_the_runtime_selections_document_their_argument_forms() -> None:
    for name, fragment in (
        ("provider", "use"),
        ("model", "use"),
        ("effort", "<level>"),
        ("agent", "use"),
        ("jobs", "stop"),
    ):
        spec = registry().get(name)
        assert spec is not None and fragment in spec.usage, name


def test_thinking_documents_how_it_is_used() -> None:
    spec = registry().get("thinking")
    assert spec is not None
    assert "on|off|toggle" in spec.usage
    assert "reasoning" in spec.description


def test_details_documents_how_it_is_used() -> None:
    spec = registry().get("details")
    assert spec is not None
    assert "on|off|toggle" in spec.usage
    assert "tool" in spec.description


def test_thread_documents_how_it_is_used() -> None:
    spec = registry().get("thread")
    assert spec is not None
    assert "[thread-id]" in spec.usage
    assert "read-only" in spec.description


def test_attach_documents_how_it_is_used() -> None:
    spec = registry().get("attach")
    assert spec is not None
    assert "<path>" in spec.usage
    assert "clear" in spec.usage



def test_a_builtin_is_a_normal_catalog_entry() -> None:
    spec = registry().get("copy")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.description
    assert spec.slash == "/copy"


# --- parsing --------------------------------------------------------------


def test_plain_text_is_not_a_command() -> None:
    assert registry().parse("hello there") is None


def test_an_empty_composer_is_not_a_command() -> None:
    assert registry().parse("   ") is None


def test_a_command_is_parsed_by_its_slash() -> None:
    parsed = registry().parse("/help")
    assert parsed is not None
    assert parsed.name == "help"
    assert parsed.args == ""
    assert parsed.description is not None


def test_arguments_are_split_off() -> None:
    parsed = registry().parse("/session other-session /workspace")
    assert parsed is not None
    assert parsed.name == "session"
    assert parsed.args == "other-session /workspace"


def test_an_alias_resolves_to_its_command() -> None:
    parsed = registry().parse("/q")
    assert parsed is not None
    assert parsed.name == "exit"


def test_quoted_arguments_survive_parsing() -> None:
    parsed = registry().parse('/session "a b"')
    assert parsed is not None
    assert parsed.args == '"a b"'


def test_the_slash_is_case_insensitive() -> None:
    parsed = registry().parse("/HELP")
    assert parsed is not None
    assert parsed.name == "help"


def test_an_unknown_command_is_reported_as_unknown_not_invented() -> None:
    parsed = registry().parse("/nope --flag")
    assert parsed is not None
    assert parsed.description is None
    assert parsed.name == "nope"
    assert parsed.args == "--flag"
    assert parsed.raw == "/nope --flag"


def test_trailing_whitespace_does_not_create_arguments() -> None:
    parsed = registry().parse("/help   ")
    assert parsed is not None
    assert parsed.args == ""


def test_is_command_recognises_a_slash() -> None:
    assert registry().is_command("/help") is True
    assert registry().is_command("  /help") is True
    assert registry().is_command("help") is False


# --- searching ------------------------------------------------------------


def test_an_empty_query_lists_every_command_in_order() -> None:
    assert [spec.name for spec in registry().search("")] == list(registry().names())


def test_a_whitespace_query_lists_everything() -> None:
    assert len(registry().search("   ")) == len(registry().names())


def test_a_prefix_match_outranks_a_mention() -> None:
    results = registry().search("/c")
    assert results[0].name == "clear-screen", "the name prefix wins"
    assert "copy" in [spec.name for spec in results], "a mention still matches"


def test_a_server_command_named_like_a_builtin_is_still_searchable() -> None:
    reg = registry()
    reg.merge((server_command("clear-ish", description="clear house"),))
    results = reg.search("/c")
    assert results[0].name == "clear-screen"


def test_a_multi_word_query_requires_every_word() -> None:
    reg = registry()
    reg.merge((server_command("screen-report", description="a screen report"),))
    results = reg.search("clear screen")
    assert [spec.name for spec in results] == ["clear-screen"]


def test_search_order_is_stable_within_a_group() -> None:
    first = [spec.name for spec in registry().search("")]
    second = [spec.name for spec in registry().search("/")]
    assert first == second


def test_searching_finds_nothing_for_an_unknown_query() -> None:
    assert registry().search("zzzz") == ()


# --- merging the server catalogue ----------------------------------------


def test_a_server_command_becomes_searchable() -> None:
    reg = registry()
    reg.merge((server_command("status", description="show the current status"),))
    assert reg.get("status") is not None
    assert reg.search("/st")[0].name == "status"


def test_server_commands_follow_the_client_ones() -> None:
    reg = registry()
    reg.merge((server_command("zzz-server"),))
    assert reg.names()[-1] == "zzz-server"


def test_a_server_command_cannot_take_over_a_client_one() -> None:
    """``/status`` is rendered from local state now, so the server's copy must
    not replace it -- that precedence is what makes the move possible."""
    reg = registry()
    reg.merge((server_command("status", description="the server's own status"),))
    spec = reg.get("status")
    assert spec is not None
    assert spec.kind == "client"
    assert spec.description != "the server's own status"


def test_merging_replaces_the_previous_catalogue() -> None:
    """The server catalogue is the source of truth; a second merge must not
    accumulate a command the server no longer advertises."""
    reg = registry()
    reg.merge((server_command("first"),))
    reg.merge((server_command("second"),))
    assert reg.get("first") is None
    assert reg.get("second") is not None


def test_a_server_command_never_shadows_a_client_command() -> None:
    reg = registry()
    reg.merge((server_command("help", description="server help"),))
    assert reg.get("help").description != "server help"
    assert len([name for name in reg.names() if name == "help"]) == 1


def test_a_server_alias_is_its_first_slash_token() -> None:
    """The catalogue's slash carries usage, e.g. ``/agent [list|use <name>]``."""
    reg = registry()
    reg.merge((server_command("agent", slash="/agent [list|status|use <name>]"),))
    parsed = reg.parse("/agent list")
    assert parsed is not None
    assert parsed.name == "agent"
    assert parsed.args == "list"


def test_a_server_alias_never_shadows_a_client_alias() -> None:
    reg = registry()
    reg.merge((server_command("quit-like", slash="/q"),))
    parsed = reg.parse("/q")
    assert parsed is not None
    assert parsed.name == "exit", "the client alias still wins"


def test_merging_nothing_keeps_the_builtins() -> None:
    reg = registry()
    reg.merge(())
    assert reg.names() == registry().names()


def test_merging_does_not_duplicate_on_repeat() -> None:
    reg = registry()
    reg.merge((server_command("status"),))
    reg.merge((server_command("status"),))
    assert len([name for name in reg.names() if name == "status"]) == 1


# --- ParsedCommand --------------------------------------------------------


def test_a_parsed_command_reports_its_own_raw_text() -> None:
    parsed = ParsedCommand(raw="/help me", name="help", args="me", description=None)
    assert parsed.raw == "/help me"
    assert parsed.args == "me"


def test_parsing_a_bare_slash_is_unknown() -> None:
    parsed = registry().parse("/")
    assert parsed is not None
    assert parsed.description is None
    assert parsed.name == ""


# --- completion matches names, not descriptions --------------------------


def test_completion_ignores_descriptions() -> None:
    """Otherwise the prefix "/st" would offer "copy" (its description says
    "latest reply"), which is noise while typing a command name."""
    results = registry().complete("/st")
    assert [spec.name for spec in results] == ["status"] or all(
        spec.name.startswith("st") for spec in results
    )
    assert "copy" not in [spec.name for spec in results]


def test_completion_matches_a_name_prefix_first() -> None:
    reg = registry()
    reg.merge((server_command("copy-report"),))
    results = reg.complete("/copy")
    assert results[0].name == "copy", "the exact prefix outranks a longer name"


def test_completion_with_an_empty_query_lists_everything() -> None:
    assert [spec.name for spec in registry().complete("/")] == list(registry().names())


# --- catalogue kinds the server publishes --------------------------------


def test_a_prompt_command_from_the_server_is_usable() -> None:
    """The client's own commands are all ``client`` kind; the server catalogue
    also carries ``server`` and ``prompt`` entries, and both must work."""
    reg = registry()
    reg.merge((server_command("skill", kind="prompt", description="run a skill"),))
    spec = reg.get("skill")
    assert spec is not None and spec.kind == "prompt"
    parsed = reg.parse("/skill review")
    assert parsed is not None and parsed.name == "skill" and parsed.args == "review"


def test_the_palette_query_finds_a_command_by_its_description() -> None:
    reg = registry()
    reg.merge((server_command("zzz-server", description="shows the banana report"),))
    assert [spec.name for spec in reg.search("banana")] == ["zzz-server"]


def test_the_palette_query_reports_no_match() -> None:
    assert registry().search("zzzz") == ()
