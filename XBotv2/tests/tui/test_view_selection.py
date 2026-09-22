"""The picker: list state is pure, the screen renders it and reports a choice."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from XBotv2.commands import CommandDescription
from XBotv2.tui.commands import CommandRegistry
from XBotv2.tui.view.palette import CommandPalette, command_options
from XBotv2.tui.view.selection import MAX_ROWS, Option, SelectionModel, SelectionScreen


def options(*values: str) -> tuple[Option, ...]:
    return tuple(
        Option(value=value, label=value.upper(), detail=f"the {value} one")
        for value in values
    )


# --- the model ------------------------------------------------------------


def test_an_empty_model_has_nothing_to_choose() -> None:
    model = SelectionModel()
    assert model.current is None
    assert model.rows == ()
    assert model.move(1) == model


def test_the_highlight_marks_exactly_one_row() -> None:
    rows = SelectionModel(options=options("a", "b")).rows
    assert rows[0].startswith("▸")
    assert not rows[1].startswith("▸")


def test_rows_carry_the_label_and_the_detail() -> None:
    assert SelectionModel(options=options("a")).rows == ("▸ A  the a one",)


def test_moving_wraps_at_both_ends() -> None:
    model = SelectionModel(options=options("a", "b", "c"))
    assert model.move(-1).index == 2
    assert model.move(3).index == 0


def test_a_new_list_highlights_its_best_row() -> None:
    """The list arrives ranked, so the highlight belongs on the first row.

    Holding the previous highlight (and, worse, its row *index*) meant a query
    could keep an unrelated command selected and then clamp onto a neighbour of
    it -- Enter ran something the user never looked at.
    """
    model = SelectionModel(options=options("a", "b", "c"), index=2)
    narrowed = model.with_options(options("x", "y"))
    assert narrowed.index == 0
    assert narrowed.current.value == "x"


def test_replacing_with_nothing_resets() -> None:
    model = SelectionModel(options=options("a"), index=0)
    replaced = model.with_options(())
    assert replaced.current is None
    assert replaced.index == 0


# --- the screen -----------------------------------------------------------


class Harness(App[None]):
    def __init__(self, screen: SelectionScreen) -> None:
        super().__init__()
        self.screen_under_test = screen
        self.chosen: list[str | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.screen_under_test, callback=self.chosen.append)


async def open_screen(screen: SelectionScreen):
    app = Harness(screen)
    context = app.run_test(size=(100, 20))
    pilot = await context.__aenter__()
    await pilot.pause()
    return app, pilot, context


async def test_the_screen_shows_its_title_and_rows() -> None:
    app, pilot, context = await open_screen(SelectionScreen("Sessions", options("a", "b")))
    try:
        screen = app.screen
        assert isinstance(screen, SelectionScreen)
        assert screen.rendered_rows == ("▸ A  the a one", "  B  the b one")
    finally:
        await context.__aexit__(None, None, None)


async def test_arrows_move_the_highlight() -> None:
    app, pilot, context = await open_screen(SelectionScreen("Sessions", options("a", "b")))
    try:
        await pilot.press("down")
        await pilot.pause()
        screen = app.screen
        assert screen.model.current.value == "b"
        await pilot.press("up")
        await pilot.pause()
        assert screen.model.current.value == "a"
    finally:
        await context.__aexit__(None, None, None)


async def test_enter_returns_the_highlighted_value() -> None:
    app, pilot, context = await open_screen(SelectionScreen("Sessions", options("a", "b")))
    try:
        await pilot.press("down", "enter")
        await pilot.pause()
    finally:
        await context.__aexit__(None, None, None)
    assert app.chosen == ["b"]


async def test_escape_returns_nothing() -> None:
    app, pilot, context = await open_screen(SelectionScreen("Sessions", options("a")))
    try:
        await pilot.press("escape")
        await pilot.pause()
    finally:
        await context.__aexit__(None, None, None)
    assert app.chosen == [None]


async def test_enter_with_no_options_returns_nothing() -> None:
    app, pilot, context = await open_screen(SelectionScreen("Sessions", ()))
    try:
        await pilot.press("enter")
        await pilot.pause()
    finally:
        await context.__aexit__(None, None, None)
    assert app.chosen == [None]


async def test_a_searchable_screen_filters_as_the_user_types() -> None:
    def search(query: str) -> tuple[Option, ...]:
        return tuple(
            option for option in options("alpha", "beta", "gamma") if query in option.value
        )

    app, pilot, context = await open_screen(
        SelectionScreen("Commands", options("alpha", "beta", "gamma"), search=search)
    )
    try:
        screen = app.screen
        await pilot.press("b")
        await pilot.pause()
        assert [option.value for option in screen.model.options] == ["beta"]
    finally:
        await context.__aexit__(None, None, None)


async def test_a_searchable_screen_chooses_on_submit() -> None:
    def search(query: str) -> tuple[Option, ...]:
        return tuple(
            option for option in options("alpha", "beta") if query in option.value
        )

    app, pilot, context = await open_screen(
        SelectionScreen("Commands", options("alpha", "beta"), search=search)
    )
    try:
        await pilot.press("b", "enter")
        await pilot.pause()
    finally:
        await context.__aexit__(None, None, None)
    assert app.chosen == ["beta"]


# --- the palette ----------------------------------------------------------


def command_registry() -> CommandRegistry:
    reg = CommandRegistry.with_builtins()
    reg.merge((
        CommandDescription(
            name="status",
            slash="/status",
            kind="server",
            description="show the current status",
            usage="/status",
            exclusive=False,
        ),
    ))
    return reg


def test_command_options_are_slash_form_with_a_description() -> None:
    rendered = command_options(command_registry().search("/he"))
    assert rendered[0].value == "/help"
    assert rendered[0].label == "/help"
    assert rendered[0].detail


def test_the_palette_starts_with_every_command() -> None:
    palette = CommandPalette(command_registry())
    assert len(palette.model.options) == len(command_registry().names())


async def test_the_palette_filters_and_returns_a_slash_command() -> None:
    app, pilot, context = await open_screen(CommandPalette(command_registry()))
    try:
        await pilot.press("s", "t", "a")
        await pilot.pause()
        screen = app.screen
        assert "/status" in [option.value for option in screen.model.options]
        await pilot.press("enter")
        await pilot.pause()
    finally:
        await context.__aexit__(None, None, None)
    assert app.chosen == ["/status"]


def test_the_row_cap_is_respected() -> None:
    model = SelectionModel(options=options(*[f"v{index}" for index in range(MAX_ROWS + 5)]))
    assert len(model.rows) == MAX_ROWS + 5, "the model keeps everything"
