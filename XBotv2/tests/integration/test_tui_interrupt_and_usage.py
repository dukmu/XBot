"""Integration tests for the TUI interrupt + real-time usage surfaces.

User asks (2026-06-05):
- 'when I press ESC, the agent should interrupt' — wired end-to-end
  here through Transport → HTTP /interrupt → session task cancel →
  engine turn_cancelled.
- 'token usage should update realtime' — ``usage`` events should
  reflect in the activity row + status bar without waiting for
  ``turn_finished``.
"""

from __future__ import annotations

import asyncio

import pytest

from xbotv2.tui.textual_client import XBotTextualApp
from xbotv2.tui.transport import Transport


# ----------------------------------------------------------------------
# ESC interrupt
# ----------------------------------------------------------------------


class _InterruptibleSession:
    """A scripted session that records transport.interrupt() calls
    and actually cancels the in-flight turn (mirrors the real
    Engine + HTTP session flow on the server side).
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.interrupt_calls: list[str] = []
        self.release = asyncio.Event()
        # Match the production ``TerminalSession`` shape: the TUI
        # addresses the transport through ``session.transport`` and
        # reads ``session.session_id`` to scope the interrupt.
        self.session_id = "s"
        self.transport = None  # wired by the test fixture
        # The asyncio task currently running ``send_message``;
        # the ``interrupt`` method cancels it to abort the turn.
        self.turn_task: asyncio.Task | None = None

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send_message(
        self, text, *, input_provider=None, permission_provider=None
    ):
        self.turn_task = asyncio.current_task()
        self.sent.append(text)
        yield {"type": "turn_started", "data": {"turn": 1}}
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            yield {
                "type": "turn_cancelled",
                "data": {"turn": 1, "reason": "client_interrupt"},
            }
            raise
        yield {
            "type": "assistant_message",
            "data": {"content": f"reply to {text}"},
        }
        yield {"type": "turn_finished", "data": {"turn": 1}}
        self.turn_task = None

    async def submit_user_input(self, request_id, answer):
        return {}

    async def respond_permission(self, request_id, decision, *, scope="once"):
        return {}

    async def interrupt(self, *, session_id: str):
        self.interrupt_calls.append(session_id)
        if self.turn_task is not None and not self.turn_task.done():
            # Mirror the real HTTP session runtime: cancel the in-flight turn
            # task. The send_message generator's ``release.wait()`` will
            # raise CancelledError, the except branch yields
            # turn_cancelled, then re-raises to close the SSE stream.
            self.turn_task.cancel()
        return {"status": "interrupting", "cancelled": True}


@pytest.mark.asyncio
async def test_esc_during_running_turn_calls_transport_interrupt() -> None:
    """Pressing ESC while a turn is in progress must call
    ``Transport.interrupt(session_id)``."""

    session = _InterruptibleSession()
    # The TUI addresses the transport through ``session.transport``.
    # In production this is ``HttpTransport``; in tests we wire the
    # scripted session to itself so ``session.transport.interrupt``
    # lands on our ``_InterruptibleSession.interrupt`` recorder.
    session.transport = session
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = session

    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("hi")
        await app.submit_composer()
        await pilot.pause()
        # Turn is now blocked on the session.release event.
        assert app.state.turn_active

        # Press ESC: the TUI's action_clear_input should detect the
        # running turn and delegate to action_interrupt_turn, which
        # calls session.transport.interrupt.
        await pilot.press("escape")
        # Let the interrupt worker run.
        for _ in range(10):
            await pilot.pause()
            if session.interrupt_calls:
                break

    assert session.interrupt_calls == ["s"], (
        f"expected one interrupt call for session 's'; got {session.interrupt_calls!r}"
    )


@pytest.mark.asyncio
async def test_turn_cancelled_event_drives_status_to_interrupted() -> None:
    """A ``turn_cancelled`` event from the engine flips status to
    'Interrupted' so the user has visual confirmation that the
    interrupt landed.
    """

    class CancellableSession(_InterruptibleSession):
        async def interrupt(self, *, session_id: str):
            # Simulate the protocol: the engine emits turn_cancelled,
            # the HTTP session runtime pipes it through the SSE stream, the
            # TUI's state.apply_event fires.
            await super().interrupt(session_id=session_id)
            return {"status": "interrupting", "cancelled": True}

    session = CancellableSession()
    session.transport = session
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = session

    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("hi")
        await app.submit_composer()
        await pilot.pause()
        await pilot.press("escape")
        for _ in range(20):
            await pilot.pause()
            if app.state.status == "Interrupted":
                break

    assert app.state.status == "Interrupted", (
        f"expected status 'Interrupted'; got {app.state.status!r}"
    )
    # And the turn is no longer active.
    assert app.state.turn_active is False


# ----------------------------------------------------------------------
# Token usage real-time
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_event_updates_status_bar_in_realtime() -> None:
    """A ``usage`` event arriving mid-turn should reflect in the
    status bar without waiting for ``turn_finished``.
    """

    class UsageSession:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(
            self, text, *, input_provider=None, permission_provider=None
        ):
            self.sent.append(text)
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {
                "type": "usage",
                "data": {
                    "delta": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                        "requests": 1,
                    },
                    "total": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                        "requests": 1,
                    },
                },
            }
            # Block so we can observe the live usage.
            await asyncio.Event().wait()
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def submit_user_input(self, request_id, answer):
            return {}

        async def respond_permission(self, request_id, decision, *, scope="once"):
            return {}

    session = UsageSession()
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = session

    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("hi")
        await app.submit_composer()
        # Give the event stream a few ticks to deliver the usage event
        # and the TUI to refresh the status bar.
        for _ in range(20):
            await pilot.pause()
            if app.state.usage["total_tokens"] == 125:
                break

        from textual.widgets import Static as TStatic
        status_widget = app.query_one("#status_bar", TStatic)
        status_text = (
            status_widget.visual.plain
            if status_widget.visual is not None
            and hasattr(status_widget.visual, "plain")
            else ""
        )
        # The status bar must show the live token counts *before*
        # turn_finished arrives.
        assert "in:100" in status_text, (
            f"status bar missing live input tokens: {status_text!r}"
        )
        assert "out:25" in status_text
        assert "total:125" in status_text

        # The activity row also reflects the live usage.
        activity = app._activity_text(final=False)
        assert "in:100" in activity
        assert "out:25" in activity
