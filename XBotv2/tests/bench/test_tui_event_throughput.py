"""Informational benchmark for the headless TUI event consumer."""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest

from XBotv2.tui.textual_client import XBotTextualApp


class FakeSession:
    """A single queued event source with no network transport."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict | None] = asyncio.Queue()
        self.release = asyncio.Event()
        self.events_started = asyncio.Event()

    async def connect(self) -> dict:
        return {
            "session_id": "bench",
            "thread_id": "agent",
            "event_cursor": 0,
            "history": [],
        }

    async def disconnect(self) -> None:
        return None

    async def list_commands(self) -> dict:
        return {"commands": []}

    async def refresh_descriptor(self) -> dict:
        return {"session_id": "bench", "thread_id": "agent"}

    async def session_events(self):
        self.events_started.set()
        await self.release.wait()
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


@pytest.mark.asyncio
async def test_tui_event_throughput_headless() -> None:
    session = FakeSession()
    app = XBotTextualApp(session_id="bench", thread_id="agent")
    app.session = session

    consumed = 0
    final_assistant_content = ""
    turn_finished = asyncio.Event()
    original_consume = app._consume_stream_event

    async def consume(event, *, pop_pending=False):
        nonlocal consumed, final_assistant_content
        consumed += 1
        if event.get("type") == "assistant_message":
            final_assistant_content = str(
                (event.get("data") or {}).get("content") or ""
            )
        await original_consume(event, pop_pending=pop_pending)
        if event.get("type") == "turn_finished":
            turn_finished.set()

    app._consume_stream_event = consume

    async with app.run_test(headless=True, size=(120, 36)):
        await asyncio.wait_for(session.events_started.wait(), timeout=1)

        async def produce() -> None:
            await session.release.wait()
            await session.events.put({
                "type": "turn_started",
                "data": {"turn": 1},
            })
            for _ in range(2000):
                await session.events.put({
                    "type": "assistant_message_delta",
                    "data": {"content": "x"},
                })
            await session.events.put({
                "type": "assistant_message",
                "data": {"content": "x" * 2000},
            })
            await session.events.put({
                "type": "turn_finished",
                "data": {"turn": 1},
            })
            await session.events.put(None)

        producer = asyncio.create_task(produce())
        started = time.perf_counter()
        session.release.set()
        await asyncio.wait_for(turn_finished.wait(), timeout=10)
        elapsed_ms = (time.perf_counter() - started) * 1000
        await producer

    events_per_second = consumed / (elapsed_ms / 1000) if elapsed_ms else float("inf")
    print(
        "[bench] TUI event throughput "
        f"events={consumed} elapsed_ms={elapsed_ms:.2f} "
        f"events_per_second={events_per_second:.2f}"
    )

    assert consumed == 2003
    assert len(final_assistant_content) == 2000
    assert app.state.messages[-1].role == "assistant"
    assert len(app.state.messages[-1].content) == 2000
    assert app.state.turn_active is False
    assert app.state.errors == []


@pytest.mark.asyncio
async def test_tui_streaming_refreshes_across_multiple_ticks() -> None:
    session = FakeSession()
    app = XBotTextualApp(session_id="bench-ticks", thread_id="agent")
    app.session = session

    consumed = 0
    stream_refreshes = 0
    turn_finished = asyncio.Event()
    original_consume = app._consume_stream_event
    original_refresh = app._refresh_streaming_assistant_widget

    async def consume(event, *, pop_pending=False):
        nonlocal consumed
        consumed += 1
        await original_consume(event, pop_pending=pop_pending)
        if event.get("type") == "turn_finished":
            turn_finished.set()

    async def refresh_streaming_assistant_widget():
        nonlocal stream_refreshes
        stream_refreshes += 1
        await original_refresh()

    app._consume_stream_event = consume
    app._refresh_streaming_assistant_widget = refresh_streaming_assistant_widget

    async with app.run_test(headless=True, size=(120, 36)):
        await asyncio.wait_for(session.events_started.wait(), timeout=1)

        async def produce() -> None:
            await session.release.wait()
            await session.events.put({
                "type": "turn_started",
                "data": {"turn": 1},
            })
            for _ in range(150):
                await session.events.put({
                    "type": "assistant_message_delta",
                    "data": {"content": "x"},
                })
                await asyncio.sleep(0.002)
            await session.events.put({
                "type": "assistant_message",
                "data": {"content": "x" * 150},
            })
            await session.events.put({
                "type": "turn_finished",
                "data": {"turn": 1},
            })
            await session.events.put(None)

        producer = asyncio.create_task(produce())
        started = time.perf_counter()
        session.release.set()
        await asyncio.wait_for(turn_finished.wait(), timeout=10)
        elapsed_ms = (time.perf_counter() - started) * 1000
        await producer

    events_per_second = consumed / (elapsed_ms / 1000) if elapsed_ms else float("inf")
    print(
        "[bench] TUI streaming refreshes "
        f"events={consumed} elapsed_ms={elapsed_ms:.2f} "
        f"events_per_second={events_per_second:.2f} "
        f"stream_refreshes={stream_refreshes}"
    )

    assert consumed == 153
    assert stream_refreshes >= 3
    assert len(app.state.messages[-1].content) == 150
    assert app.state.turn_active is False
    assert app.state.errors == []


def _body_children(widget: object) -> list[object]:
    """The `.body` children of a mounted entry widget, if it has any."""
    try:
        return list(widget.query(".body"))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a widget without DOM support has none
        return []


def _widget_text(widget: object) -> str:
    """Plain text of a mounted entry child, whatever renderable it holds."""
    for source in (getattr(widget, "renderable", None), getattr(widget, "content", None)):
        if source is None:
            continue
        for attribute in ("plain", "markup"):
            value = getattr(source, attribute, None)
            if value is not None:
                return str(value)
        if isinstance(source, str):
            return source
    return ""


@pytest.mark.asyncio
async def test_tui_window_stays_bounded_and_renders_the_newest_tail(monkeypatch) -> None:
    """Memory, DOM, and per-update render work stay bounded on a long session.

    The state window is shrunk for this test so a short run crosses many
    eviction cycles; the production ceilings are asserted by the state-level
    tests.
    """
    import XBotv2.tui.client as tui_client
    from XBotv2.tui.textual_client import (
        _MAX_MESSAGE_WIDGETS,
        _MAX_MOUNTED_ENTRIES,
    )

    monkeypatch.setattr(tui_client, "_MAX_STATE_MESSAGES", 60)
    monkeypatch.setattr(tui_client, "_MAX_STATE_TRANSCRIPT", 80)
    monkeypatch.setattr(tui_client, "_TRIM_SLACK", 20)
    window = tui_client._MAX_STATE_MESSAGES + tui_client._TRIM_SLACK

    session = FakeSession()
    app = XBotTextualApp(session_id="bench-window", thread_id="agent")
    app.session = session
    counter = itertools.count()

    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await asyncio.wait_for(session.events_started.wait(), timeout=1)
        surface = app._surface()
        assert surface is not None
        assert surface.max_mounted_entries == _MAX_MOUNTED_ENTRIES

        # Every mount is recorded, so "bounded work per update" is asserted on
        # the amount materialized rather than on wall-clock time.
        widths: list[int] = []
        original_mount = surface.mount_entries

        async def recording_mount(start: int, end: int, *, prepend: bool = False):
            widths.append(end - start)
            return await original_mount(start, end, prepend=prepend)

        surface.mount_entries = recording_mount  # type: ignore[method-assign]

        async def drive(events: int) -> int:
            index = -1
            for _ in range(events):
                index = next(counter)
                await app._consume_stream_event({
                    "type": "assistant_message",
                    "data": {"id": f"m{index}", "content": f"answer {index}", "tool_calls": []},
                })
            await surface.catch_up()
            await pilot.pause()
            return index

        # A burst far larger than the window, then many times the window again:
        # the reader follows the tail throughout.
        await drive(30)
        await drive(200)
        last_index = await drive(200)

        assert len(app.state.messages) <= window
        assert len(app.state.transcript) <= tui_client._MAX_STATE_TRANSCRIPT + tui_client._TRIM_SLACK
        assert app.state.evicted_messages > 0
        assert len(surface.mounted_entry_widgets) <= _MAX_MOUNTED_ENTRIES
        assert len(surface.message_widgets) <= _MAX_MESSAGE_WIDGETS
        # No update ever materialized more than one window, however large the
        # burst behind it was or how long the session had already run.
        assert widths, "no mount happened"
        assert max(widths) <= _MAX_MOUNTED_ENTRIES, max(widths)
        assert max(widths) > 0

        # The reader's end of the window is intact: the newest answer is
        # mounted, and every mounted body shows retained content.
        retained = {message.content for message in app.state.messages}
        mounted_texts = [
            _widget_text(child)
            for widget in surface.mounted_entry_widgets
            for child in _body_children(widget)
        ]
        assert any(f"answer {last_index}" in text for text in mounted_texts), mounted_texts[-3:]
        for text in mounted_texts:
            if text.strip():
                assert any(text.strip() in content for content in retained), text[:80]
        assert "answer 1 " not in " ".join(mounted_texts)

        # A backlog larger than the window (events consumed without a render
        # between them) is materialized as one window, not as one widget per
        # event.  The state ceilings are restored first so the backlog can
        # exceed the render window.
        monkeypatch.setattr(tui_client, "_MAX_STATE_MESSAGES", 400)
        monkeypatch.setattr(tui_client, "_MAX_STATE_TRANSCRIPT", 400)
        monkeypatch.setattr(tui_client, "_TRIM_SLACK", 0)
        for index in range(150):
            app.state.apply_event({
                "type": "assistant_message",
                "data": {"id": f"backlog{index}", "content": f"backlog {index}", "tool_calls": []},
            })
        widths.clear()
        await surface.catch_up()
        await pilot.pause()
        assert widths, "backlog sync did not mount"
        assert max(widths) <= _MAX_MOUNTED_ENTRIES, max(widths)

        print(
            "[bench] TUI window "
            f"messages={len(app.state.messages)} transcript={len(app.state.transcript)} "
            f"mounted={len(surface.mounted_entry_widgets)} "
            f"widgets={len(surface.message_widgets)} "
            f"max_mount={max(widths)} mounts={len(widths)} last_message={last_index}"
        )


def test_tui_state_per_event_cost_does_not_grow_with_history() -> None:
    """The state window, measured without Textual's render cost."""
    from XBotv2.tui.client import TuiState

    state = TuiState()

    def block(start: int, events: int) -> float:
        began = time.perf_counter()
        for index in range(start, start + events):
            state.apply_event({
                "type": "assistant_message",
                "data": {"id": f"m{index}", "content": f"answer {index}", "tool_calls": []},
            })
        return time.perf_counter() - began

    first = block(0, 1000)
    middle = block(1000, 19_000)
    last = block(20_000, 1000)
    print(
        "[bench] TUI state window "
        f"first_ms={first * 1000:.1f} middle_20k_ms={middle * 1000:.1f} last_ms={last * 1000:.1f}"
    )
    assert state.evicted_messages > 0
    assert last < max(first, 0.02) * 4
