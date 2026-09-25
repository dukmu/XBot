"""Clamped blocks: long content never floods the transcript.

A tool result, a reasoning trace, or a long answer can be thousands of lines. The
transcript is a conversation, not a log viewer, so each of those becomes a block
with a fixed maximum height: collapsed it shows a one-line summary and a short
preview, expanded it shows the content inside its own scroll area. The decision of
what to show is a pure function, so it is tested without a terminal.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from XBotv2.tui.view.blocks import (
    BLOCK_MAX_LINES,
    BLOCK_PREVIEW_LINES,
    ClampedBlock,
    count_lines,
    plan_block,
)


def many_lines(count: int, prefix: str = "line") -> str:
    return "\n".join(f"{prefix} {index}" for index in range(count))


# --- what a block shows ---------------------------------------------------


def test_short_content_is_shown_whole_and_is_not_collapsible() -> None:
    plan = plan_block("one\ntwo", label="reply")
    assert plan.collapsible is False
    assert plan.head == ""
    assert plan.body == "one\ntwo"


def test_short_think_or_tool_content_can_still_be_folded() -> None:
    plan = plan_block(
        "one\ntwo",
        label="Think",
        always_collapsible=True,
    )
    assert plan.collapsible is True
    assert "Think" in plan.head
    assert plan.body == "one\ntwo"


def test_long_content_collapses_to_a_summary_and_a_preview() -> None:
    plan = plan_block(many_lines(40), label="tool output")
    assert plan.collapsible is True
    assert plan.lines == 40
    assert "tool output" in plan.head
    assert "40 lines" in plan.head
    assert "ctrl+e" in plan.head
    assert plan.body == "\n".join(
        f"line {index}" for index in range(BLOCK_PREVIEW_LINES)
    ), "a collapsed body is exactly the preview; the head carries the count"


def test_expanding_shows_the_whole_content() -> None:
    text = many_lines(40)
    plan = plan_block(text, label="tool output", expanded=True)
    assert plan.collapsible is True
    assert plan.body == text
    assert "ctrl+e" in plan.head


def test_the_cut_off_is_exactly_the_maximum() -> None:
    assert plan_block(many_lines(BLOCK_MAX_LINES), label="x").collapsible is False
    assert plan_block(many_lines(BLOCK_MAX_LINES + 1), label="x").collapsible is True


def test_a_streaming_body_is_shown_to_its_end() -> None:
    """While an answer is arriving the reader wants the newest lines, not the
    first ones -- and the block still never grows past its cap."""
    plan = plan_block(many_lines(40), label="reply", streaming=True)
    assert plan.body == many_lines(40)
    assert plan.streaming is True


def test_line_counting_ignores_a_trailing_newline() -> None:
    assert count_lines("one\ntwo\n") == 2
    assert count_lines("") == 0
    assert count_lines("one") == 1


# --- the widget -----------------------------------------------------------


class Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield ClampedBlock(many_lines(40), label="tool output", id="block")

    def on_mount(self) -> None:
        self.block = self.query_one("#block", ClampedBlock)


class SingleBlockHarness(App[None]):
    def __init__(self, block: ClampedBlock) -> None:
        super().__init__()
        self._block = block

    def compose(self) -> ComposeResult:
        yield self._block

    def on_mount(self) -> None:
        self.block = self._block


def block_text(block: ClampedBlock) -> str:
    return "\n".join(
        str(getattr(part.content, "plain", "") or "") for part in [block.head_widget, block.body_widget] if part is not None
    )


async def test_a_collapsed_block_stays_short() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.block.expanded is False
        assert app.block.region.height <= BLOCK_PREVIEW_LINES + 1  # preview + summary row
        assert "line 39" not in block_text(app.block)


async def test_toggling_shows_the_content_within_the_fixed_height() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.block.toggle()
        await pilot.pause()
        assert app.block.expanded is True
        assert app.block.region.height <= BLOCK_MAX_LINES
        assert "line 39" in block_text(app.block)


async def test_a_focused_block_toggles_from_the_keyboard() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.block.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.block.expanded is True
        await pilot.press("space")
        await pilot.pause()
        assert app.block.expanded is False


async def test_short_content_has_no_summary_row() -> None:
    app = App[None]()
    block = ClampedBlock("just one line", label="reply", id="b")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert block.collapsible is False
        assert block.head_widget is None


async def test_expanded_short_block_uses_the_fixed_height_window() -> None:
    block = ClampedBlock(
        "one line",
        label="Think",
        always_collapsible=True,
        id="short-block",
    )
    app = SingleBlockHarness(block)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert block.collapsible is True
        block.toggle()
        await pilot.pause()
        assert block.region.height == BLOCK_MAX_LINES


async def test_a_long_answer_stays_open_once_it_has_streamed() -> None:
    """The turn ending must not fold away what the reader was reading."""
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.block.show(many_lines(40), streaming=True)
        await pilot.pause()
        assert app.block.expanded is True
        app.block.show(many_lines(41), streaming=False)
        await pilot.pause()
        assert app.block.expanded is True, "still open after the stream ended"
        assert "line 40" in block_text(app.block)
