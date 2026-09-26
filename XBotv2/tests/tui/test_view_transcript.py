"""The transcript view: an id-diffed window over a real timeline.

These tests drive the real widgets through a real Textual app, and build the
state the way production does: wire frames through the protocol translator and
the reducer. Nothing is hand-assembled, so a change to any layer shows up here.

Invariants under test:
  I2  a streaming entry updates *its own* widget and no other
  I3  nothing outside the reader's window is touched, and nothing is dropped
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from textual.app import App, ComposeResult

from XBotv2.agentloop.protocol import AssistantReasoningDelta
from XBotv2.core.domain import CompletedStop, ModelTiming
from XBotv2.session.records import AssistantRecord
from XBotv2.session.records import HumanInputRecord
from XBotv2.tests.tui.factories import (
    assistant_record,
    history_page,
    human_record,
    tool_record,
)
from XBotv2.tests.tui.factories import SESSION, THREAD, frames, snapshot
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ConnectionChanged,
    OlderHistoryLoaded,
    SnapshotAdopted,
)
from XBotv2.tui.protocol import FrameTranslator
from XBotv2.tui.state import SessionState, reduce
from XBotv2.tui.status import Connection
from XBotv2.tui.view.entries import BlockVisibility
from XBotv2.tui.view.transcript import (
    ThinkingActivity,
    TranscriptScroll,
    TranscriptView,
)


def build_state(*pairs: tuple[str, dict]) -> SessionState:
    """Feed wire frames through the translator and the reducer, as production does."""
    state = SessionState()
    reduce(state, ConnectionChanged(Connection.CONNECTED))
    translator = FrameTranslator(session_id=SESSION, thread_id=THREAD)
    for frame in frames(*pairs):
        for event in translator.translate(frame):
            reduce(state, event)
    return state


def user(message_id: str, content: str) -> tuple[str, dict]:
    return ("message", human_record(message_id, content).model_dump(mode="json"))


def tool_started(call_id: str, name: str = "bash") -> tuple[str, dict]:
    return (
        "tool_calls_started",
        {"calls": [{"call": {"id": call_id, "name": name, "args": {}}, "category": "execute"}]},
    )


def tool_finished(call_id: str, content: str = "ok") -> tuple[str, dict]:
    return (
        "tool_completed",
        tool_record(call_id, "bash", content),
    )


class Harness(App[None]):
    """A bare app with one transcript container."""

    def __init__(self, *, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.view: TranscriptView | None = None

    def compose(self) -> ComposeResult:
        yield TranscriptScroll(id="transcript")

    def on_mount(self) -> None:
        self.view = TranscriptView(
            self.query_one("#transcript", TranscriptScroll), limit=self.limit
        )


@asynccontextmanager
async def harness(limit: int = 3, *, size: tuple[int, int] = (80, 12)) -> AsyncIterator[tuple]:
    app = Harness(limit=limit)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        yield app, pilot


def part_of(widget, selector: str) -> str:
    """The text one part of an entry widget is showing, or "" when it is absent.

    A body or reasoning part is a clamped block, so its text lives inside the
    block rather than on the part itself.
    """
    from textual.widgets import Static

    from XBotv2.tui.view.blocks import ClampedBlock

    try:
        part = widget.query(selector).first()
    except Exception:  # noqa: BLE001 - an absent part is the normal case here
        return ""
    if isinstance(part, ClampedBlock):
        return part.shown_text
    if not isinstance(part, Static):
        return ""
    content = part.content
    return str(getattr(content, "plain", None) or getattr(content, "markup", "") or "")


def body_of(widget) -> str:
    """The rendered body text; Markdown keeps its markup, Text its plain form."""
    return part_of(widget, ".body")


# --- the mounted window ---------------------------------------------------


async def test_a_short_transcript_mounts_every_entry() -> None:
    state = build_state(user("m1", "one"), user("m2", "two"))
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        assert app.view.mounted_ids == ("m1", "m2")


async def test_the_window_is_bounded_and_shows_the_newest_entries() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(6)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        assert app.view.mounted_ids == ("m3", "m4", "m5")
        assert app.view.widget_count == 3


async def test_a_new_entry_at_the_tail_replaces_the_oldest_widget() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(3)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        oldest = app.view.widget_for("m0")
        reduce(state, FrameTranslator(session_id=SESSION, thread_id=THREAD).translate(
            frames(user("m3", "three"))[0]
        )[0])
        await app.view.render(state)
        assert app.view.mounted_ids == ("m1", "m2", "m3")
        assert app.view.widget_for("m0") is None
        assert oldest is not None


async def test_rendering_an_unchanged_state_mounts_nothing_new() -> None:
    state = build_state(user("m1", "one"), user("m2", "two"))
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        widget = app.view.widget_for("m1")
        assert await app.view.render(state) is False
        assert app.view.widget_for("m1") is widget, "an unchanged entry is not rebuilt"


async def test_streamed_thinking_widget_survives_canonical_completion_identity() -> None:
    from XBotv2.tui.view.blocks import ClampedBlock

    state = build_state(user("human-record-1", "explain the next step"))
    reduce(state, AssistantDelta(
        payload=AssistantReasoningDelta(text="checking the request")
    ))
    streamed_id = state.stream_entry_id
    assert streamed_id is not None

    async with harness(limit=5) as (app, pilot):
        await app.view.render(state)
        await pilot.pause()
        streamed_widget = app.view.widget_for(streamed_id)
        assert streamed_widget is not None
        streamed_block = streamed_widget.query_one(".reasoning", ClampedBlock)
        assert streamed_block.expanded

        reduce(state, AssistantCompleted(payload=AssistantRecord(
            id="assistant-record-1",
            content="the answer",
            reasoning="checking the request",
            timing=ModelTiming(total_ms=1),
            stop=CompletedStop(),
        )))
        await app.view.render(state)
        await pilot.pause()

        completed_widget = app.view.widget_for("assistant-record-1")
        assert completed_widget is streamed_widget
        completed_block = completed_widget.query_one(".reasoning", ClampedBlock)
        assert not completed_block.expanded
        assert completed_block.shown_text == "checking the request"
        assert "ctrl+e expands" in completed_block.head_text

        human_widget = app.view.widget_for("human-record-1")
        assert human_widget is not None
        transcript = app.view.container
        assert (
            human_widget.region.y < transcript.region.bottom
            and human_widget.region.bottom > transcript.region.y
        ), "the compact completed Think block leaves the user's prompt in view"


async def test_an_entry_that_left_the_timeline_is_unmounted() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(3)])
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        assert app.view.widget_for("m1") is not None
        state.timeline.remove("m1")
        await app.view.render(state)
        assert app.view.widget_for("m1") is None
        assert app.view.mounted_ids == ("m0", "m2")


# --- streaming integrity --------------------------------------------------


async def test_a_growing_answer_updates_its_own_widget() -> None:
    state = build_state(
        ("turn_started", {"turn": 1}),
        ("assistant_text_delta", {"text": "thinking"}),
    )
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        streaming_id = state.stream_entry_id
        widget = app.view.widget_for(streaming_id)
        body = widget.query_one(".body")
        assert body_of(widget) == "● thinking"

        translator = FrameTranslator(session_id=SESSION, thread_id=THREAD)
        for event in translator.translate(
            frames(("assistant_text_delta", {"text": " harder"}))[0]
        ):
            reduce(state, event)
        await app.view.render(state)

        assert app.view.widget_for(streaming_id) is widget, "the same widget grows"
        assert widget.query_one(".body") is body, "streaming updates the body in place"
        assert body_of(widget) == "● thinking harder"


async def test_a_growing_answer_never_writes_into_another_entry() -> None:
    """The defect: a streamed tail landed in whichever widget was last."""
    state = build_state(
        ("turn_started", {"turn": 1}),
        ("assistant_completed", assistant_record("a1", "first answer")),
        user("m2", "steer"),
        ("assistant_text_delta", {"text": "second"}),
    )
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        first = app.view.widget_for("a1")
        assert body_of(first) == "● first answer"
        translator = FrameTranslator(session_id=SESSION, thread_id=THREAD)
        for event in translator.translate(
            frames(("assistant_text_delta", {"text": " answer"}))[0]
        ):
            reduce(state, event)
        await app.view.render(state)
        assert body_of(first) == "● first answer", "the committed entry is not rewritten"
        assert body_of(app.view.widget_for("m2")) == "❯ steer"


async def test_a_tool_result_adopts_its_record_id_without_touching_other_rows() -> None:
    """The record replaces its temporary call row with its canonical identity."""
    state = build_state(
        ("turn_started", {"turn": 1}),
        tool_started("c1"),
        user("m2", "meanwhile"),
        user("m3", "and later"),
    )
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        running = app.view.widget_for("c1")
        last = app.view.widget_for("m3")
        translator = FrameTranslator(session_id=SESSION, thread_id=THREAD)
        for event in translator.translate(frames(tool_finished("c1", "listing"))[0]):
            reduce(state, event)
        await app.view.render(state)
        assert app.view.widget_for("c1") is None
        widget = app.view.widget_for("tool-c1")
        assert widget is not None
        assert widget is running, "completion updates the mounted tool row in place"
        from XBotv2.tui.view.blocks import ClampedBlock

        details = widget.query_one(".tool-result", ClampedBlock)
        assert details.shown_text == "", "tool result is folded by default"
        details.toggle()
        await _pilot.pause()
        assert "listing" in details.shown_text
        assert body_of(last) == "❯ and later", "the last widget is not a dumping ground"


# --- paging ---------------------------------------------------------------


async def test_paging_older_moves_the_window_and_keeps_it_bounded() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(9)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        assert app.view.mounted_ids == ("m6", "m7", "m8")
        assert await app.view.page_older(state) is True
        assert app.view.mounted_ids == ("m3", "m4", "m5")
        assert app.view.widget_count == 3
        assert app.view.anchor == "m5"


async def test_paging_older_stops_at_the_oldest_entry() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(3)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        assert await app.view.page_older(state) is False


async def test_paging_newer_returns_towards_the_tail() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(9)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        await app.view.page_older(state)
        await app.view.page_older(state)
        assert app.view.mounted_ids == ("m0", "m1", "m2")
        assert await app.view.page_newer(state) is True
        assert app.view.mounted_ids == ("m3", "m4", "m5")
        assert await app.view.page_newer(state) is True
        assert app.view.mounted_ids == ("m6", "m7", "m8")
        assert app.view.anchor is None, "reaching the newest window is the tail"
        assert await app.view.page_newer(state) is False


async def test_going_to_the_tail_after_paging_back() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(9)])
    async with harness(limit=3, size=(80, 6)) as (app, pilot):
        await app.view.render(state)
        await settle(pilot)
        await app.view.page_older(state)
        await app.view.go_to_tail(state)
        await settle(pilot)
        assert app.view.mounted_ids == ("m6", "m7", "m8")
        assert app.view.anchor is None
        assert app.view.reader_at_end is True


async def test_an_anchor_that_left_the_timeline_falls_back_to_the_tail() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(9)])
    async with harness(limit=3) as (app, _pilot):
        await app.view.render(state)
        await app.view.page_older(state)
        anchored = app.view.anchor
        state.timeline.remove(anchored)
        await app.view.render(state)
        assert app.view.anchor is None
        assert app.view.mounted_ids == ("m6", "m7", "m8")


async def settle(pilot, rounds: int = 4) -> None:
    """Let every scheduled scroll and layout pass land.

    The view pins the tail on the next refresh, so a test that scrolls by hand
    must wait for that to have happened first; otherwise it races the pin and the
    result depends on how busy the loop is.
    """
    for _ in range(rounds):
        await pilot.pause()


async def test_the_reader_position_is_asked_of_the_container_not_remembered() -> None:
    state = build_state(*[user(f"m{index}", str(index)) for index in range(20)])
    async with harness(limit=20, size=(80, 6)) as (app, pilot):
        await app.view.render(state)
        await settle(pilot)
        assert app.view.reader_at_end is True, "a fresh render follows the tail"
        app.view.container.scroll_to(y=0, animate=False, immediate=True)
        await settle(pilot)
        assert app.view.reader_at_end is False
        await app.view.render(state)
        await settle(pilot)
        assert app.view.reader_at_end is False, "rendering must not move the reader"


async def test_tail_updates_do_not_move_a_reader_who_scrolled_back() -> None:
    state = build_state(
        *[
            user(f"m{index}", f"message {index}\nsecond line\nthird line")
            for index in range(20)
        ],
        ("turn_started", {"turn": 1}),
        ("assistant_text_delta", {"text": "live"}),
    )
    async with harness(limit=30, size=(80, 10)) as (app, pilot):
        await app.view.render(state)
        await settle(pilot)
        scroll = app.view.container
        scroll.scroll_to(y=18, animate=False, immediate=True)
        await settle(pilot)
        scroll._remember_reader_anchor()
        anchor = scroll._reader_anchor
        assert anchor is not None
        visible_y = anchor.region.y - scroll.scroll_y

        translator = FrameTranslator(session_id=SESSION, thread_id=THREAD)
        for suffix in (" result", " continues", " and finishes"):
            for event in translator.translate(
                frames(("assistant_text_delta", {"text": suffix}))[0]
            ):
                reduce(state, event)
            await app.view.render(state)
            await settle(pilot)
            assert scroll._reader_anchor is anchor
            assert anchor.region.y - scroll.scroll_y == visible_y


async def test_an_invalid_limit_is_rejected() -> None:
    app = Harness(limit=1)
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(ValueError):
            TranscriptView(app.query_one("#transcript", TranscriptScroll), limit=0)


async def test_two_renders_at_once_do_not_duplicate_rows() -> None:
    """The frame loop and a submission both flush.

    Without serializing, both plans are computed from the same mounted set, each
    mounts a widget for the same entry, and one is left orphaned in the DOM --
    a duplicated row that no later render removes.
    """
    state = build_state(user("m1", "one"), user("m2", "two"), user("m3", "three"))
    async with harness(limit=10) as (app, pilot):
        await asyncio.gather(app.view.render(state), app.view.render(state))
        await settle(pilot)
        scroll = app.query_one("#transcript", TranscriptScroll)
        assert len(scroll.children) == 3, [str(c.classes) for c in scroll.children]
        assert app.view.mounted_ids == ("m1", "m2", "m3")


async def test_a_render_racing_a_submission_keeps_one_row_per_entry() -> None:
    state = build_state(user("m1", "one"))
    async with harness(limit=10) as (app, pilot):
        await app.view.render(state)
        grown = build_state(user("m1", "one"), user("m2", "two"))
        await asyncio.gather(app.view.render(grown), app.view.render(grown))
        await settle(pilot)
        scroll = app.query_one("#transcript", TranscriptScroll)
        assert len(scroll.children) == 2


# --- what the reader asked to see ----------------------------------------


def reply(message_id: str, reasoning: str) -> tuple[tuple[str, dict], ...]:
    """A turn with streamed and canonical reasoning."""
    return (
        ("assistant_reasoning_delta", {"text": reasoning}),
        ("assistant_completed", assistant_record(message_id, "the answer", reasoning=reasoning)),
    )


def first_assistant(view):
    """The one assistant widget on screen, whatever id the stream gave it."""
    return view.widget_for(view.mounted_ids[-1])


async def test_reasoning_is_mounted_by_default() -> None:
    state = build_state(*reply("a1", "thinking hard"))
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        widget = first_assistant(app.view)
        assert "thinking hard" in part_of(widget, ".reasoning")


async def test_thinking_activity_is_transient_and_not_a_timeline_entry() -> None:
    state = build_state(user("u1", "a question"))
    async with harness(limit=5) as (app, pilot):
        await app.view.render(state, thinking=True)
        await settle(pilot)
        assert len(app.query(ThinkingActivity)) == 1
        assert "Thinking" in str(app.query_one(ThinkingActivity).content)
        assert state.timeline.ids() == ("u1",)

        await app.view.render(state, thinking=False)
        await settle(pilot)
        assert len(app.query(ThinkingActivity)) == 0
        assert state.timeline.ids() == ("u1",)


async def test_hiding_reasoning_takes_it_out_of_the_mounted_window() -> None:
    state = build_state(*reply("a1", "thinking hard"))
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        widget = first_assistant(app.view)
        app.view.set_visibility(BlockVisibility(reasoning=False))
        await app.view.render(state)
        assert first_assistant(app.view) is widget, "the entry is updated, not rebuilt"
        assert part_of(widget, ".reasoning") == ""
        assert "the answer" in part_of(widget, ".body")


async def test_showing_reasoning_again_restores_it() -> None:
    state = build_state(*reply("a1", "thinking hard"))
    async with harness(limit=5) as (app, _pilot):
        app.view.set_visibility(BlockVisibility(reasoning=False))
        await app.view.render(state)
        widget = first_assistant(app.view)
        assert part_of(widget, ".reasoning") == ""
        app.view.set_visibility(BlockVisibility(reasoning=True))
        await app.view.render(state)
        assert "thinking hard" in part_of(widget, ".reasoning")


async def test_the_same_visibility_is_not_a_change() -> None:
    state = build_state(*reply("a1", "thinking hard"))
    async with harness(limit=5) as (app, _pilot):
        await app.view.render(state)
        assert app.view.set_visibility(BlockVisibility()) is False
        assert await app.view.render(state) is False, "nothing was invalidated"


# --- one verbose step cannot flood the conversation -----------------------


async def test_a_huge_tool_result_cannot_flood_the_transcript() -> None:
    """The reported flooding: 2 000 lines of tool output used to be 2 000 rows."""
    from XBotv2.tui.view.blocks import BLOCK_MAX_LINES

    huge = "\n".join(f"output {index}" for index in range(2000))
    state = build_state(
        user("m1", "run it"),
        tool_started("c1"),
        tool_finished("c1", huge),
    )
    async with harness(limit=10) as (app, _pilot):
        await app.view.render(state)
        scroll = app.query_one("#transcript", TranscriptScroll)
        tool_widget = app.view.widget_for("tool-c1")
        assert tool_widget is not None
        assert tool_widget.region.height <= BLOCK_MAX_LINES + 2, (
            "the meta row plus the clamped block, never the content size"
        )
        assert scroll.virtual_size.height <= 40, (
            f"the transcript holds {scroll.virtual_size.height} rows for 2 000 lines"
        )
        assert "run it" in part_of(app.view.widget_for("m1"), ".body")


# --- the older-history notice --------------------------------------------


def history_user(node: str, content: str) -> HumanInputRecord:
    return human_record(node, content)


def test_the_notice_says_what_the_client_knows_and_nothing_when_it_knows_all():
    from XBotv2.tui.state import (
        HistoryAvailable,
        HistoryComplete,
        HistoryFailed,
        HistoryLoading,
    )
    from XBotv2.tui.view.transcript import older_history_label

    assert older_history_label(HistoryComplete()) is None
    assert "PageUp" in older_history_label(HistoryAvailable(cursor="c1"))
    assert "Loading" in older_history_label(HistoryLoading(cursor="c1"))
    assert "boom" in older_history_label(HistoryFailed(cursor="c1", message="boom"))


def attached(*items: HumanInputRecord, cursor: str | None = None) -> SessionState:
    """A state built the way production builds one: the server's attach answer."""
    state = SessionState()
    reduce(state, ConnectionChanged(Connection.CONNECTED))
    reduce(state, SnapshotAdopted(snapshot(history=list(items), history_cursor=cursor)))
    return state


async def test_the_notice_is_above_the_window_and_gone_when_nothing_is_missing():
    state = attached(history_user("m1", "one"))
    async with harness(limit=5) as (app, pilot):
        await app.view.render(state)
        assert not app.query("#older-history")

        state = attached(history_user("m1", "one"), cursor="c1")
        await app.view.render(state)
        await pilot.pause()

        order = list(app.query("#transcript > *"))
        assert order.index(app.view.older_notice) < order.index(
            app.view.widget_for("m1")
        )

        # Once the client holds the beginning there is nothing left to say.
        state = attached(history_user("m1", "one"))
        await app.view.render(state)
        await pilot.pause()

        assert not app.query("#older-history")


async def test_the_notice_stays_first_when_an_older_page_is_prepended():
    state = attached(history_user("m2", "two"), cursor="c1")
    async with harness(limit=5) as (app, pilot):
        await app.view.render(state)
        reduce(state, OlderHistoryLoaded(
            cursor="c1",
            payload=history_page(human_record("m1", "one"), older_cursor="c0"),
        ))
        await app.view.render(state)
        await pilot.pause()

        order = list(app.query("#transcript > *"))
        assert order.index(app.view.older_notice) < order.index(
            app.view.widget_for("m1")
        )
        assert app.view.mounted_ids == ("m1", "m2")


async def test_a_prepended_older_page_does_not_move_the_readers_window():
    """Prepending is the one insert that lands *above* the reader.

    A page the reader asked for arrives behind them: the entry their window ends
    at must stay exactly where it was, and the page must be reachable by paging
    older once more.
    """
    state = attached(
        history_user("m2", "two"),
        history_user("m3", "three"),
        cursor="c1",
    )
    async with harness(limit=1) as (app, pilot):
        await app.view.render(state)
        assert app.view.mounted_ids == ("m3",)

        await app.view.page_older(state)
        assert app.view.mounted_ids == ("m2",)
        anchor = app.view.anchor
        assert anchor == "m2"

        reduce(state, OlderHistoryLoaded(
            cursor="c1",
            payload=history_page(human_record("m1", "one")),
        ))
        await app.view.render(state)
        await pilot.pause()

        assert app.view.mounted_ids == ("m2",), "the page landed behind the reader"
        assert app.view.anchor == anchor

        # ...and it is reachable, one window older.
        await app.view.page_older(state)
        assert app.view.mounted_ids == ("m1",)


async def test_releasing_the_front_keeps_the_tail_window_on_screen():
    """Retention is residency, not navigation: the reader stays at the tail and
    the newest entries stay mounted."""
    state = attached(
        history_user("m2", "two"),
        history_user("m3", "three"),
        cursor="c1",
    )
    async with harness(limit=2) as (app, pilot):
        reduce(state, OlderHistoryLoaded(
            cursor="c1",
            payload=history_page(human_record("m1", "one")),
        ))
        await app.view.render(state)
        assert app.view.mounted_ids == ("m2", "m3")

        from XBotv2.tui.state import release_oldest_loaded_page

        release_oldest_loaded_page(state)
        await app.view.render(state)
        await pilot.pause()

        assert app.view.mounted_ids == ("m2", "m3")
        assert app.view.anchor is None, "the reader is still following the tail"
        assert state.older.__class__.__name__ == "HistoryAvailable"
