"""Completion while the user is still typing a command name.

The decision "is the popup showing, and what is highlighted" is a pure function of
the composer text and the catalogue, so it is tested without a terminal. The
widget only renders what this returns.
"""

from __future__ import annotations

from XBotv2.commands import CommandDescription
from XBotv2.tui.commands import CommandRegistry
from XBotv2.tui.view.completion import (
    accepted_text,
    completion_for,
    dismiss,
    move,
)


def registry() -> CommandRegistry:
    reg = CommandRegistry.with_builtins()
    reg.merge(
        (
            CommandDescription(
                name="status",
                slash="/status",
                kind="server",
                description="show the current status",
                usage="/status",
                exclusive=False,
            ),
        )
    )
    return reg


# --- when it shows --------------------------------------------------------


def test_an_empty_composer_shows_nothing() -> None:
    assert completion_for(registry(), "").visible is False


def test_plain_text_shows_nothing() -> None:
    assert completion_for(registry(), "hello").visible is False


def test_a_bare_slash_lists_every_command() -> None:
    state = completion_for(registry(), "/")
    assert state.visible is True
    assert len(state.matches) == len(registry().names())


def test_typing_narrows_the_matches() -> None:
    state = completion_for(registry(), "/st")
    assert state.visible is True
    assert state.matches[0].name == "status"


def test_arguments_end_the_completion() -> None:
    """Once the user is typing arguments, offering command names is noise."""
    assert completion_for(registry(), "/session other").visible is False


def test_a_trailing_space_ends_the_completion() -> None:
    assert completion_for(registry(), "/help ").visible is False


def test_a_second_line_shows_nothing() -> None:
    assert completion_for(registry(), "/he\nmore").visible is False


def test_a_query_with_no_matches_hides_the_popup() -> None:
    assert completion_for(registry(), "/zzzz").visible is False


def test_the_query_is_carried_for_rendering() -> None:
    assert completion_for(registry(), "/he").query == "/he"


# --- the highlighted match ------------------------------------------------


def test_the_first_match_is_highlighted_by_default() -> None:
    state = completion_for(registry(), "/")
    assert state.index == 0
    assert state.current is not None
    assert state.current.name == "help"


def test_moving_down_advances_and_wraps() -> None:
    state = completion_for(registry(), "/")
    total = len(state.matches)
    moved = state
    for _ in range(total):
        moved = move(moved, 1)
    assert moved.index == 0, "wrapping past the end returns to the start"


def test_moving_up_from_the_first_wraps_to_the_last() -> None:
    state = move(completion_for(registry(), "/"), -1)
    assert state.index == len(state.matches) - 1


def test_moving_an_invisible_popup_changes_nothing() -> None:
    hidden = completion_for(registry(), "hello")
    assert move(hidden, 1) == hidden


def test_the_index_is_clamped_when_the_matches_shrink() -> None:
    """Typing another character must not leave the highlight past the end."""
    wide = move(completion_for(registry(), "/"), 3)
    assert wide.index == 3
    narrow = completion_for(registry(), "/statu", index=wide.index)
    assert [spec.name for spec in narrow.matches] == ["status"]
    assert narrow.index == 0


# --- accepting ------------------------------------------------------------


def test_accepting_fills_in_the_highlighted_command() -> None:
    state = completion_for(registry(), "/he")
    assert accepted_text(state) == "/help"


def test_accepting_fills_in_the_whole_command_name() -> None:
    state = completion_for(registry(), "/cle")
    assert accepted_text(state) == "/clear-screen"


def test_accepting_a_hidden_popup_does_nothing() -> None:
    assert accepted_text(completion_for(registry(), "hello")) is None
    assert accepted_text(dismiss(completion_for(registry(), "/"))) is None


def test_dismissing_hides_the_popup_without_losing_the_query() -> None:
    dismissed = dismiss(completion_for(registry(), "/he"))
    assert dismissed.visible is False
    assert dismissed.query == "/he"


def test_dismissal_only_lasts_until_the_text_changes() -> None:
    dismissed = dismiss(completion_for(registry(), "/he"))
    assert completion_for(registry(), dismissed.query + "l").visible is True


# --- the presenter owns the state and mirrors it -------------------------


def test_the_presenter_tracks_what_is_typed() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/st")
    assert presenter.visible is True
    assert presenter.current is not None and presenter.current.name == "status"


def test_the_presenter_works_before_any_popup_is_attached() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/he")
    assert presenter.accept() == "/help"


def test_accepting_hides_the_presenter() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/he")
    presenter.accept()
    assert presenter.visible is False


def test_rewriting_the_composer_after_accepting_does_not_reopen_the_popup() -> None:
    """Accepting writes the text back, which raises a change event with exactly
    that text; re-opening the popup would fight the user."""
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/he")
    accepted = presenter.accept()
    presenter.refresh(accepted)
    assert presenter.visible is False


def test_editing_the_text_after_accepting_reopens_the_popup() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/cle")
    accepted = presenter.accept()
    assert accepted == "/clear-screen"
    presenter.refresh(accepted[:-1])
    assert presenter.visible is True


def test_moving_the_presenter_moves_the_highlight() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/")
    presenter.move(1)
    assert presenter.rows[1].startswith("▸")


def test_the_presenter_rows_mark_the_highlight() -> None:
    from XBotv2.tui.view.completion import CompletionPresenter

    presenter = CompletionPresenter(registry())
    presenter.refresh("/he")
    assert presenter.rows[0].startswith("▸")
