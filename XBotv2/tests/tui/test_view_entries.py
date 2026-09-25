"""Entry formatting is pure, so it is tested without a terminal.

The header and body of an entry are functions of that entry. Keeping them pure is
what makes it cheap to check the things a user actually reads -- is my message
marked as still sending, does a finished tool say how long it took, is a
structured tool payload shown readably.
"""

from __future__ import annotations

import pytest
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult

from XBotv2.tui.timeline import (
    AssistantEntry,
    Delivery,
    ErrorEntry,
    NoticeEntry,
    ToolEntry,
    UserEntry,
)
from XBotv2.tui.view.entries import (
    BlockVisibility,
    EntryWidget,
    block_choice,
    entry_body,
    entry_body_renderable,
    entry_classes,
    entry_header,
    entry_reasoning,
    entry_widget,
    format_payload,
    looks_like_markdown,
)


def user(delivery: Delivery = Delivery.ACCEPTED, content: str = "hello") -> UserEntry:
    return UserEntry(id="u1", content=content, delivery=delivery)


def assistant(content: str = "answer", *, streaming: bool = False, reasoning: str = "") -> AssistantEntry:
    return AssistantEntry(
        id="a1", content=content, reasoning=reasoning, streaming=streaming
    )


def tool(**overrides) -> ToolEntry:
    fields = {
        "id": "c1",
        "name": "bash",
        "args": {},
        "status": "success",
        "result": "",
        "started_at": 10.0,
        "finished_at": 12.5,
    }
    return ToolEntry(**{**fields, **overrides})


# --- headers --------------------------------------------------------------


def test_a_pending_message_says_it_is_still_sending() -> None:
    assert "sending" in entry_header(user(Delivery.PENDING))


def test_a_failed_message_says_it_was_not_delivered() -> None:
    assert "not delivered" in entry_header(user(Delivery.FAILED))


def test_an_accepted_message_carries_no_caveat() -> None:
    assert entry_header(user(Delivery.ACCEPTED)) == "You"


def test_the_assistant_header_is_the_configured_label() -> None:
    assert entry_header(assistant(), assistant_label="Reviewer") == "Reviewer"


def test_a_streaming_answer_says_it_is_replying() -> None:
    assert "replying" in entry_header(assistant(streaming=True))


def test_a_running_tool_shows_no_duration_yet() -> None:
    header = entry_header(tool(status="running", finished_at=0.0))
    assert "running" in header
    assert header.endswith("…"), "an unfinished tool has no elapsed time to show"


def test_a_finished_tool_shows_how_long_it_took() -> None:
    assert "2.5s" in entry_header(tool())


def test_notice_and_error_headers() -> None:
    assert entry_header(NoticeEntry(id="n1", notice_kind="compact", text="t")) == "compact"
    assert entry_header(ErrorEntry(id="e1", message="boom")) == "error"


def test_an_unknown_entry_is_rejected_loudly() -> None:
    with pytest.raises(TypeError):
        entry_header(object())  # type: ignore[arg-type]


# --- bodies ---------------------------------------------------------------


def test_a_tool_body_shows_arguments_and_result() -> None:
    body = entry_body(tool(args={"command": "ls"}, result="a\nb"))
    assert "args:" in body
    assert '"command": "ls"' in body
    assert "result: a\nb" in body


def test_a_tool_body_omits_what_is_empty() -> None:
    assert entry_body(tool(args={}, result="")) == ""


def test_a_notice_body_includes_its_detail() -> None:
    entry = NoticeEntry(id="n1", notice_kind="compact", text="compacted", detail="kept the gist")
    assert entry_body(entry) == "compacted\nkept the gist"


def test_classes_describe_the_entry_kind() -> None:
    assert entry_classes(user()) == "entry user"
    assert entry_classes(tool()) == "entry tool"


def test_reasoning_is_only_rendered_for_an_assistant_entry() -> None:
    assert entry_reasoning(assistant(reasoning="why")) is not None
    assert entry_reasoning(user()) is None
    assert entry_reasoning(assistant()) is None


# --- payload formatting ---------------------------------------------------


def test_a_string_payload_is_shown_verbatim() -> None:
    assert format_payload("line\nline") == "line\nline"


def test_a_structured_payload_is_shown_as_readable_json() -> None:
    rendered = format_payload({"b": 1, "a": [1, 2]})
    assert rendered.splitlines()[0] == "{"
    assert '"a"' in rendered
    assert rendered.index('"a"') < rendered.index('"b"'), "keys are sorted"


def test_an_empty_payload_renders_as_nothing() -> None:
    assert format_payload(None) == ""


def test_unicode_payloads_are_not_escaped() -> None:
    assert "中文" in format_payload({"note": "中文"})


# --- markdown heuristic ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["## heading", "```\ncode\n```", "**bold**", "- item", "1. item", "| a | b |"],
)
def test_markdown_is_recognised(text: str) -> None:
    assert looks_like_markdown(text)


def test_plain_prose_is_not_treated_as_markdown() -> None:
    assert not looks_like_markdown("just a sentence about things")


def test_an_assistant_body_is_parsed_only_when_it_has_markup() -> None:
    assert isinstance(entry_body_renderable(assistant("## hi")), Markdown)
    assert isinstance(entry_body_renderable(assistant("plain words")), Text)


def test_a_user_body_is_never_parsed_as_markdown() -> None:
    """What the human typed is shown as typed, not reflowed."""
    assert isinstance(entry_body_renderable(user(content="## not a heading")), Text)


# --- an update that cannot be applied must be visible --------------------


async def test_updating_a_widget_that_has_not_composed_reports_failure() -> None:
    """The caller must be able to tell that nothing was written.

    An entry that changes in the same frame it was mounted has no children yet;
    silently succeeding there left a stale "sending…" header on screen forever.
    """
    from XBotv2.tui.view.entries import entry_widget, update_entry_widget

    widget = entry_widget(user(Delivery.PENDING))
    assert await update_entry_widget(widget, user(Delivery.ACCEPTED)) is False


# --- what the reader asked to see -----------------------------------------


def test_both_blocks_are_shown_by_default() -> None:
    visibility = BlockVisibility()
    assert visibility.reasoning is True
    assert visibility.details is True


def test_hidden_reasoning_is_not_rendered() -> None:
    entry = assistant(reasoning="I thought about it")
    assert entry_reasoning(entry) is not None
    assert entry_reasoning(entry, visibility=BlockVisibility(reasoning=False)) is None


def test_hidden_tool_details_leave_the_header() -> None:
    entry = tool(args={"cmd": "ls"}, result="ok")
    assert "cmd" in entry_body(entry)
    assert entry_body(entry, visibility=BlockVisibility(details=False)) == ""
    assert "bash" in entry_header(entry), "the call itself is still visible"


def test_hiding_details_does_not_hide_an_answer() -> None:
    hidden = BlockVisibility(details=False)
    assert entry_body(assistant("the answer"), visibility=hidden) == "the answer"


def test_block_choice_reads_on_off_toggle() -> None:
    assert block_choice("on", current=False) is True
    assert block_choice("OFF", current=True) is False
    assert block_choice("toggle", current=True) is False
    assert block_choice("toggle", current=False) is True
    assert block_choice("", current=True) is False, "no argument means toggle"
    assert block_choice(" on ", current=False) is True


def test_block_choice_refuses_anything_else() -> None:
    assert block_choice("maybe", current=True) is None
    assert block_choice("on off", current=True) is None


# --- long content becomes a clamped block ---------------------------------


class EntryHarness(App[None]):
    """One entry widget, mounted, so its children exist."""

    def __init__(self, widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def blocks_of(entry_widget) -> list:
    from XBotv2.tui.view.blocks import ClampedBlock

    return [child for child in entry_widget.children if isinstance(child, ClampedBlock)]


async def test_a_long_tool_result_is_previewed_inside_a_block() -> None:
    from XBotv2.tui.view.blocks import BLOCK_MAX_LINES
    from XBotv2.tui.view.entries import entry_widget

    huge = "\n".join(f"output {index}" for index in range(500))
    app = EntryHarness(entry_widget(tool(result=huge)))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        blocks = blocks_of(app.query_one(EntryWidget))
        assert len(blocks) == 1, "the payload lives in one block"
        block = blocks[0]
        assert block.collapsible is True
        assert block.region.height <= BLOCK_MAX_LINES, "500 lines must not reach the screen"
        assert "output 499" not in str(block.body_widget.content.plain)


async def test_a_long_answer_is_folded_but_a_short_one_is_not() -> None:
    """Both bodies are blocks; only the long one folds and grows a summary row."""
    from XBotv2.tui.view.entries import entry_widget

    short = EntryHarness(entry_widget(assistant(content="one\ntwo")))
    async with short.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        (block,) = blocks_of(short.query_one(EntryWidget))
        assert block.collapsible is False
        assert block.head_widget is None, "a summary row for two lines is noise"

    long = EntryHarness(
        entry_widget(assistant(content="\n".join(f"para {i}" for i in range(80))))
    )
    async with long.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        (block,) = blocks_of(long.query_one(EntryWidget))
        assert block.collapsible is True


async def test_reasoning_is_its_own_clamped_block() -> None:
    from XBotv2.tui.view.entries import entry_widget

    app = EntryHarness(
        entry_widget(assistant(reasoning="\n".join(f"thought {i}" for i in range(60))))
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        blocks = [b for b in blocks_of(app.query_one(EntryWidget)) if b.label == "Think"]
        assert len(blocks) == 1, "reasoning is its own block, beside the body"
        assert "60 lines" in str(blocks[0].head_widget.content.plain)


async def test_short_reasoning_remains_a_collapsible_think_block() -> None:
    app = EntryHarness(entry_widget(assistant(reasoning="brief thought")))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        (block,) = [
            item for item in blocks_of(app.query_one(EntryWidget))
            if item.label == "Think"
        ]
        assert block.collapsible is True
        assert "Think" in block.head_text
        assert "ctrl+e expands" in block.head_text


async def test_short_tool_output_remains_collapsible() -> None:
    app = EntryHarness(entry_widget(tool(args={"cmd": "pwd"}, result="/repo")))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        (block,) = blocks_of(app.query_one(EntryWidget))
        assert block.collapsible is True


async def test_an_error_is_never_folded_away() -> None:
    """I4: the reason a turn failed has to be readable, however long it is."""
    from XBotv2.tui.view.entries import entry_widget

    message = "\n".join(f"failure line {index}" for index in range(80))
    app = EntryHarness(entry_widget(ErrorEntry(id="e1", message=message)))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        entry = app.query_one(EntryWidget)
        assert blocks_of(entry) == []
        from textual.widgets import Static

        assert "failure line 79" in str(entry.query_one(".body", Static).content)


async def test_help_is_never_folded() -> None:
    """Reference output the user explicitly asked for must be readable.

    Everything else may fold; a help listing that says "… 12 more lines" is a
    command that does not work.
    """
    from XBotv2.tui.view.entries import entry_widget

    listing = "\n".join(f"/command-{index}  does something" for index in range(15))
    app = EntryHarness(entry_widget(NoticeEntry(id="n1", notice_kind="help", text=listing)))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        entry = app.query_one(EntryWidget)
        assert blocks_of(entry) == []
        from textual.widgets import Static

        assert "command-14" in str(entry.query_one(".body", Static).content)
