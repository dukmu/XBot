"""Informational benchmark for the headless TUI event consumer."""

from __future__ import annotations

import asyncio
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
