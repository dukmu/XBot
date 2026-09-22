"""The queued-input panel: rows derived from the server's pending inputs.

Queued follow-ups are part of "what is the agent about to do", so the status line
and this panel must agree. As with jobs, rows are reconciled by message id: a
rebuild on every tick is what made the old panel's rows flicker.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from XBotv2.session.contracts import PendingInputData
from XBotv2.tui.view.queue import QueuePanel, queue_row


def pending(
    message_id: str = "q1",
    *,
    content: str = "do this next",
    target: str = "next-turn",
    source: str = "user",
    image_count: int = 0,
    artifact_count: int = 0,
) -> PendingInputData:
    return PendingInputData(
        message_id=message_id,
        content=content,
        target=target,
        source=source,
        image_count=image_count,
        artifact_count=artifact_count,
    )


class Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield QueuePanel(id="queue")

    def on_mount(self) -> None:
        self.panel = self.query_one("#queue", QueuePanel)


# --- what a row says ------------------------------------------------------


def test_a_row_shows_the_position_the_text_and_the_target() -> None:
    row = queue_row(pending(content="run the tests"), position=1)
    assert "run the tests" in row
    assert "next-turn" in row
    assert row.strip().startswith("1"), "the position is where the item waits"


def test_a_row_says_how_many_images_travel_with_it() -> None:
    assert "2 images" in queue_row(pending(image_count=2), position=1)
    assert "image" not in queue_row(pending(), position=1)


def test_a_row_never_exceeds_the_width_it_is_given() -> None:
    row = queue_row(pending(content="x" * 200), position=3, width=40)
    assert all(len(line) <= 40 for line in row.splitlines())


def test_a_multiline_input_is_shown_on_one_line() -> None:
    row = queue_row(pending(content="first\nsecond"), position=1)
    assert "first second" in row
    assert "\n" not in row


# --- the panel ------------------------------------------------------------


async def test_the_panel_lists_every_queued_input() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.panel.show([pending("a"), pending("b", content="second")], width=60)
        await pilot.pause()
        assert app.panel.order == ("a", "b")
        assert app.panel.rows == 2


async def test_an_unchanged_input_keeps_its_row() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.panel.show([pending("a")], width=60)
        await pilot.pause()
        widget = app.panel.row_widget("a")
        app.panel.show([pending("a"), pending("b")], width=60)
        await pilot.pause()
        assert app.panel.row_widget("a") is widget, "rows are updated, not rebuilt"


async def test_a_dequeued_input_loses_its_row() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.panel.show([pending("a"), pending("b")], width=60)
        await pilot.pause()
        app.panel.show([pending("b")], width=60)
        await pilot.pause()
        assert app.panel.order == ("b",)
        assert app.panel.row_widget("a") is None


async def test_an_emptied_queue_leaves_no_rows() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.panel.show([pending("a")], width=60)
        await pilot.pause()
        app.panel.show([], width=60)
        await pilot.pause()
        assert app.panel.rows == 0
        assert app.panel.order == ()
