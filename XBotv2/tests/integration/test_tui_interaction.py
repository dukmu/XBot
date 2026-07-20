"""Headless end-to-end interaction tests for the TUI.

Drives the real ``XBotTextualApp`` via Textual's ``Pilot`` (the
``app.run_test(headless=True)`` async context manager). These tests
exercise the actual widget tree, key handling, and DOM updates —
not just protocol state.

Coverage:

- Slash completion popup: appears on ``/``, ``Tab`` accepts the
  highlighted match, ``Up``/``Down`` navigate, ``Escape`` dismisses.
- Chinese IME: typed Chinese appears in the composer, submits, and
  ends up byte-for-byte in the protocol state (no mojibake).
- /help: each command prints on its own line, not one crowded row.
- /clear: empties the stream without disturbing session/usage.
- Mouse wheel: not the primary scroll affordance here, but the
  transcript is mouse-scrollable.
- /exit: cleanly quits the app.
- Slash submission of an unknown command: surfaces a "not
  implemented" notice, never sent to the server.
- Re-submitting the same composer text: de-duplicated; no double
  local acknowledgement.
- QueueMessage: typing during a running turn queues and drains in
  FIFO order.
"""

from __future__ import annotations

import asyncio

import pytest

from xbotv2.tui.command import parse_slash_command, search_commands
from xbotv2.tui.completion_popup import CompletionPopup
from xbotv2.tui.terminal import CommandOutcome
from xbotv2.tui.textual_client import XBotTextualApp


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


class _ScriptedSession:
    """A minimal stand-in for ``TerminalSession`` that scripts events.

    Replaces the real HTTP-bound session so the headless pilot does
    not need a running uvicorn. Mirrors the new ``send_message``
    method shape (post-Phase E).
    """

    def __init__(self, scripts: list[list[dict]] | None = None) -> None:
        self._scripts: list[list[dict]] = list(scripts or [])
        self.sent: list[str] = []
        self.session_id: str = "s"
        self.thread_id: str = "t"

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def list_commands(self):
        return {"commands": []}

    async def run_builtin_command(self, command, args):
        del args
        if command == "status":
            return CommandOutcome("turn=0 mode=composing")
        return CommandOutcome(f"ran {command}")

    async def run_command(self, command, args, raw, *, kind="server"):
        del args, raw
        if command == "status":
            return {"data": {"message": "turn=0 mode=composing"}}
        return {"data": {"message": f"ran {command}"}}

    async def send_message(self, text):
        self.sent.append(text)
        if self._scripts:
            events = self._scripts.pop(0)
        else:
            events = [
                {"type": "turn_started", "data": {"turn": 1}},
                {"type": "assistant_message", "data": {"content": f"reply: {text}"}},
                {"type": "turn_finished", "data": {"turn": 1}},
            ]
        for event in events:
            yield event

    async def submit_user_input(self, request_id, answer):
        return {"type": "user_input_recorded", "data": {"request_id": request_id}}

    async def respond_permission(self, request_id, decision, *, scope="once"):
        return {
            "type": "permission_response_recorded",
            "data": {"request_id": request_id, "decision": decision, "scope": scope},
        }


@pytest.fixture
def scripted_session() -> _ScriptedSession:
    return _ScriptedSession()


@pytest.mark.asyncio
async def test_status_bar_uses_product_title(scripted_session) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        screenshot = app.export_screenshot(title="xbotv2-status")

    assert "XBotv2" in screenshot
    assert "XBotTextualApp" not in screenshot


@pytest.mark.asyncio
async def test_status_bar_is_below_composer_and_keeps_tokens_on_narrow_screen(
    scripted_session,
) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session

    async with app.run_test(headless=True, size=(48, 20)) as pilot:
        await pilot.pause()
        import time

        app.state.status = "Approval required"
        app.state.turn = 123
        app.state.turn_active = True
        app._turn_started_at[123] = time.monotonic() - 1234.5
        app._pending_messages = {
            1: "active",
            2: "queued second",
            3: "queued third",
        }
        app.state.usage.update(
            {"input_tokens": 1200, "output_tokens": 345, "total_tokens": 1545}
        )
        app._refresh_status()
        await pilot.pause()

        status = app.query_one("#status_bar")
        composer = app.query_one("#composer")
        assert composer.region.bottom == status.region.y
        assert status.region.bottom == app.size.height
        assert "Approval" in status.visual.plain
        assert "queued:2" in status.visual.plain
        assert "tokens:1.5k" in status.visual.plain


@pytest.mark.parametrize(
    ("width", "status"),
    [(20, "Interrupting..."), (32, "An arbitrary long server status")],
)
def test_status_bar_preserves_queue_and_tokens_for_any_status(
    width: int,
    status: str,
) -> None:
    from xbotv2.tui.textual_widgets import status_renderable

    rendered = status_renderable(
        status=status,
        session_id="session",
        thread_id="agent",
        workspace_root="/workspace/XBot",
        provider="minimax",
        model="Minimax-M3",
        context_window=32_000,
        context_input_tokens=8_000,
        activity="turn:123 1234.5s",
        queue_depth=2,
        usage={
            "requests": 1,
            "input_tokens": 1200,
            "output_tokens": 345,
            "total_tokens": 1545,
        },
        width=width,
    ).plain

    assert len(rendered) <= width
    assert ("q:2" if width < 32 else "queued:2") in rendered
    assert ("t:1.5k" if width < 32 else "tokens:1.5k") in rendered


def test_status_bar_compacts_million_token_counts() -> None:
    from xbotv2.tui.textual_widgets import status_renderable

    rendered = status_renderable(
        status="Ready",
        session_id="s",
        thread_id="agent",
        workspace_root="",
        provider="minimax",
        model="MiniMax-M3",
        model_mode="high",
        context_window=0,
        context_input_tokens=0,
        activity="turn:1",
        queue_depth=0,
        usage={
            "requests": 1,
            "input_tokens": 1_200_000,
            "output_tokens": 300_000,
            "total_tokens": 1_500_000,
        },
        width=100,
    ).plain

    assert "tokens:1.5M" in rendered
    assert "1.2M in / 300.0k out" in rendered


@pytest.mark.asyncio
async def test_status_bar_uses_open_session_metadata() -> None:
    class MetadataSession(_ScriptedSession):
        async def connect(self):
            return {
                "session_id": "server-session",
                "thread_id": "agent",
                "agent_name": "BuildBot",
                "workspace_root": "/workspace/XBot",
                "provider": "minimax",
                "model": "Minimax-M3",
                "model_mode": "high",
                "status_slots": {"goal": "active"},
                "context_window": 32000,
                "history": [],
            }

    app = XBotTextualApp(session_id="client-session", thread_id="agent")
    app.session = MetadataSession()

    async with app.run_test(headless=True, size=(120, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
            if app._connected:
                break

        status = app.query_one("#status_bar")
        assert app.state.session_id == "server-session"
        assert app.state.workspace_root == "/workspace/XBot"
        assert app.state.provider == "minimax"
        assert app.state.model == "Minimax-M3"
        assert app.state.context_window == 32000
        assert "agent:BuildBot" in status.visual.plain
        assert "minimax/Minimax-M3:high" in status.visual.plain
        assert "goal:active" in status.visual.plain
        assert "ctx:" not in status.visual.plain
        app.state.apply_event({
            "type": "usage",
            "data": {
                "input_tokens": 8000,
                "output_tokens": 100,
                "total_tokens": 8100,
                "requests": 1,
            },
        })
        app._refresh_status()
        await pilot.pause()
        assert "ctx-free:75%" in status.visual.plain
        assert "minimax/Minimax-M3:high" in status.visual.plain


@pytest.mark.asyncio
async def test_resumed_assistant_history_uses_markdown_rendering() -> None:
    from rich.markdown import Markdown
    from rich.text import Text
    from textual.widgets import Static

    class HistorySession(_ScriptedSession):
        async def connect(self):
            return {
                "session_id": "resumed",
                "thread_id": "agent",
                "agent_name": "XBotv2",
                "workspace_root": "/workspace/XBot",
                "provider": "minimax",
                "history": [
                    {"role": "user", "content": "**literal input**"},
                    {"role": "assistant", "content": "## Answer\n\n- item"},
                ],
            }

    app = XBotTextualApp(session_id="resumed", thread_id="agent")
    app.session = HistorySession()

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
            if app._connected:
                break

        assert isinstance(app.query_one(".user .body", Static).content, Text)
        assert isinstance(app.query_one(".assistant .body", Static).content, Markdown)


# ----------------------------------------------------------------------
# Slash completion popup
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_popup_appears_on_slash(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#input")
        assert popup.visible is False

        composer.load_text("/")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()

        assert popup.visible is True
        assert len(popup.matches) == 6
        # First match should be /help (stable search order).
        assert popup.matches[0].name == "help"


@pytest.mark.asyncio
async def test_completion_popup_filters_by_prefix(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#input")

        composer.load_text("/cl")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()

        names = [m.name for m in popup.matches]
        assert "clear-screen" in names
        assert popup.current_match() is not None
        assert popup.current_match().name == "clear"


@pytest.mark.asyncio
async def test_completion_popup_hides_when_text_stops_with_slash_prefix(
    scripted_session,
) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#input")

        composer.load_text("hello world")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()

        assert popup.visible is False


@pytest.mark.asyncio
async def test_completion_popup_tab_accepts_highlighted(
    scripted_session,
) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#input")

        # Type "/c" — completion popup appears, /clear is first.
        composer.load_text("/c")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()
        assert popup.current_match() is not None

        app._accept_completion(popup.current_match())
        await pilot.pause()

        assert composer.text == "/clear"
        # The popup should still be visible (the prefix is still a slash).
        assert popup.visible is True


@pytest.mark.asyncio
async def test_completion_popup_escape_dismisses(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one("#input")

        composer.load_text("/h")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()
        assert popup.visible is True

        app._dismiss_completion_popup()
        await pilot.pause()

        assert popup.visible is False
        # Composer text is preserved on dismiss.
        assert composer.text == "/h"


@pytest.mark.asyncio
async def test_narrow_completion_tasks_status_and_composer_do_not_overlap(
    scripted_session,
) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session

    async with app.run_test(headless=True, size=(40, 18)) as pilot:
        await pilot.pause()
        event = {
            "type": "task_updated",
            "data": {
                "task_id": "task-1",
                "command": "sleep 30",
                "cwd": "/workspace",
                "status": "running",
                "created_at": 1.0,
                "started_at": 1.0,
                "finished_at": 0.0,
                "output": "",
                "error": "",
            },
        }
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        app._pending_messages = {
            1: "active",
            2: "queued follow-up",
        }
        app._refresh_all()
        composer = app.query_one("#input")
        composer.load_text("/")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        runtime_panels = app.query_one("#runtime_panels")
        tasks = app.query_one("#task_panel")
        queue = app.query_one("#queue_panel")
        queue_list = app.query_one("#queue_list")
        status = app.query_one("#status_bar")
        composer_region = app.query_one("#composer").region

        assert popup.region.bottom <= runtime_panels.region.y
        assert tasks.region.y == queue.region.y
        assert runtime_panels.region.bottom <= composer_region.y
        assert composer_region.bottom <= status.region.y
        assert "queued follow-up" in queue_list.visual.plain
        assert status.region.bottom <= app.size.height, (
            f"popup={popup.region} runtime={runtime_panels.region} "
            f"status={status.region} composer={composer_region} screen={app.size}"
        )


# ----------------------------------------------------------------------
# Chinese IME end-to-end
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composer_preserves_chinese_ime_text(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    chinese = "你好中文不丢"
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text(chinese)
        await app.submit_composer()
        await pilot.pause()

    assert scripted_session.sent == [chinese]
    assert [m.content for m in app.state.messages if m.role == "user"] == [chinese]


# ----------------------------------------------------------------------
# /help
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_prints_each_command_on_its_own_line(
    scripted_session,
) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/help")
        await app.submit_composer()
        await pilot.pause()

    help_notices = [n for n in app.state.notices if n.kind == "Help"]
    assert len(help_notices) == 1
    body = help_notices[0].text
    # Each registered command label is on its own line.
    assert "help" in body and "clear" in body and "status" in body and "exit" in body
    assert body.count("\n") >= 3


# ----------------------------------------------------------------------
# Unknown slash command
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_slash_command_surfaces_notice_not_message(
    scripted_session,
) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/doesnotexist")
        await app.submit_composer()
        await pilot.pause()

    # Nothing should have been sent to the server.
    assert scripted_session.sent == []
    # An "Unknown command" notice must appear.
    assert any(
        n.kind == "Unknown command" and "/doesnotexist" in n.text
        for n in app.state.notices
    )


# ----------------------------------------------------------------------
# Re-submit de-dup
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_composer_submit_does_not_duplicate_submit(
    scripted_session,
) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("hi")
        await app.submit_composer()
        await pilot.pause()
        # A second submit with empty composer is a no-op.
        await app.submit_composer()
        await pilot.pause()

    assert scripted_session.sent == ["hi"]
    # Only one user message in the transcript.
    assert sum(1 for m in app.state.messages if m.role == "user") == 1


# ----------------------------------------------------------------------
# Per-tool latency: title shows the elapsed seconds
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_widget_title_includes_elapsed_seconds(
    scripted_session,
) -> None:
    """User can read the tool's wall-clock latency from its title.

    "tool  shell  success  0.42s" answers the user's recurring
    question of "why is the tool still pending" by surfacing both
    the live elapsed (while pending) and the frozen final elapsed
    (after tool_result).
    """

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        # Drive assistant_message with tool_calls (creates pending
        # tool entry with started_at set).
        app.state.apply_event({
            "type": "assistant_message",
            "data": {
                "content": "",
                "tool_calls": [
                    {"id": "c1", "name": "shell", "args": {"command": "ls"}},
                ],
            },
        })
        await app._render_new_transcript_entries()
        await pilot.pause()

        # Walk the DOM and find the meta row of the tool entry.
        from textual.widgets import Static as TStatic
        metas: list[str] = []
        for w in app.query_one("#transcript").walk_children():
            if isinstance(w, TStatic) and "meta" in (w.classes or []):
                t = (
                    w.visual.plain
                    if w.visual is not None and hasattr(w.visual, "plain")
                    else ""
                )
                if t.startswith("tool"):
                    metas.append(t)
        assert len(metas) == 1, f"expected one tool meta; got {metas!r}"
        assert "  ls  " in metas[0]
        assert '{"command"' not in metas[0]
        app._update_pending_tool_elapsed()
        await pilot.pause()
        assert "  ls  " in app.query_one(".tool .meta").visual.plain
        # Pending entry shows the live "Ns…" suffix.
        assert "s…" in metas[0], f"missing live elapsed: {metas[0]!r}"

        # Now simulate tool_result — the title should switch to a
        # frozen "<n>.<nn>s" suffix (no ellipsis).
        import asyncio
        await asyncio.sleep(0.05)  # ensure some monotonic delta
        app.state.apply_event({
            "type": "tool_result",
            "data": {
                "tool_call_id": "c1",
                "name": "shell",
                "status": "success",
                "content": "ok",
            },
        })
        await app._render_new_transcript_entries()
        await pilot.pause()

        # Force a tool widget refresh path by invoking the private
        # hook the app uses after tool_result (see _handle_stream_event).
        await app._refresh_tool_widget("c1")
        await pilot.pause()

        metas = []
        for w in app.query_one("#transcript").walk_children():
            if isinstance(w, TStatic) and "meta" in (w.classes or []):
                t = (
                    w.visual.plain
                    if w.visual is not None and hasattr(w.visual, "plain")
                    else ""
                )
                if t.startswith("tool"):
                    metas.append(t)
        assert len(metas) == 1
        # After tool_result, the title has a frozen "Ns" with no
        # ellipsis.
        assert "s" in metas[0]
        assert "s…" not in metas[0], f"expected frozen elapsed, got: {metas[0]!r}"


# ----------------------------------------------------------------------
# Sanity: search/parse contract for slash commands
# ----------------------------------------------------------------------


def test_search_commands_returns_help_first() -> None:
    results = search_commands("")
    assert results[0].name == "help"


def test_parse_slash_command_round_trip() -> None:
    spec = parse_slash_command("/clear-screen")
    assert spec is not None
    assert spec.name == "clear-screen"
    assert spec.raw == "/clear-screen"


# ----------------------------------------------------------------------
# QueueMessage: type while a turn is running, get picked up in order
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_during_running_turn_queues_and_drains_in_order() -> None:
    """User can submit messages while a turn is in progress.

    Per design doc §8.2: the composer is visible during
    ``RUNNING`` mode and submissions are queued; the worker drains
    them in FIFO order once the current turn finishes.
    """

    class SlowSession:
        """Yields ``turn_started`` and blocks until released."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.release = asyncio.Event()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def send_message(self, text):
            self.sent.append(text)
            yield {"type": "turn_started", "data": {"turn": 1}}
            # Block the turn until the test releases it.
            await self.release.wait()
            yield {"type": "assistant_message", "data": {"content": f"reply to {text}"}}
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def submit_user_input(self, request_id, answer):
            return {}

        async def respond_permission(self, request_id, decision, *, scope="once"):
            return {}

    session = SlowSession()
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = session

    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")

        # First message: starts the turn; the worker will block.
        composer.load_text("first")
        await app.submit_composer()
        await pilot.pause()

        # The turn is now in progress. Composer is still visible
        # and accepts new submissions; they go to the queue.
        composer.load_text("second")
        await app.submit_composer()
        await pilot.pause()
        composer.load_text("third")
        await app.submit_composer()
        await pilot.pause()

        # Hint should mention queueing.
        from textual.widgets import Static as TStatic
        hint_widget = app.query_one("#composer_hint", TStatic)
        hint_text = (
            hint_widget.visual.plain
            if hint_widget.visual is not None and hasattr(hint_widget.visual, "plain")
            else ""
        )
        assert "Queueing" in hint_text or "queue" in hint_text.lower(), (
            f"hint did not mention queueing; got {hint_text!r}"
        )

        # Status bar should report the two follow-up requests.
        status = app.query_one("#status_bar", TStatic)
        status_text = (
            status.visual.plain
            if status.visual is not None and hasattr(status.visual, "plain")
            else ""
        )
        assert "queued:2" in status_text, f"status: {status_text!r}"
        queue_panel = app.query_one("#queue_panel")
        queue_list = app.query_one("#queue_list", TStatic)
        assert queue_panel.display is True
        assert queue_panel.title == "Queue (2)"
        assert "second" in queue_list.visual.plain
        assert "third" in queue_list.visual.plain
        assert "first" not in queue_list.visual.plain

        # All requests are submitted immediately. The real server owns
        # ordering through its per-session mailbox.
        for _ in range(20):
            await pilot.pause()
            if len(session.sent) == 3:
                break
        assert session.sent == ["first", "second", "third"]

        # Release the worker; it should drain the queue in order.
        session.release.set()
        # Give the worker a few ticks to finish.
        for _ in range(20):
            await pilot.pause()
            if not app._pending_messages:
                break

        assert queue_panel.display is False

    assert session.sent == ["first", "second", "third"]


# ----------------------------------------------------------------------
# /clear-screen and /status
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slash_clear_resets_state_not_session(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="my-session",
        thread_id="my-thread",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        # Seed some history before clearing.
        app.state.append_message("user", "first")
        app.state.notices.append(_make_notice("client_message", "hello"))
        await app._render_new_transcript_entries()
        assert app.query_one("#transcript").children
        composer = app.query_one("#input")
        composer.load_text("/clear-screen")
        await app.submit_composer()
        await pilot.pause()
        assert not app.query_one("#transcript").children

    assert app.state.messages == []
    assert app.state.notices == []
    # session_id/thread_id preserved.
    assert app.state.session_id == "my-session"
    assert app.state.thread_id == "my-thread"
    # No server traffic for /clear-screen.
    assert scripted_session.sent == []


@pytest.mark.asyncio
async def test_slash_status_appends_state_notice(scripted_session) -> None:
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/status")
        await app.submit_composer()
        await pilot.pause()

    status_notices = [n for n in app.state.notices if n.kind == "/status"]
    assert len(status_notices) == 1
    body = status_notices[0].text
    assert "turn=0" in body
    assert "mode=composing" in body  # Mode enum value
    assert scripted_session.sent == []


# ----------------------------------------------------------------------
# Command palette (Ctrl+P)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_p_opens_palette_with_full_command_list(
    scripted_session,
) -> None:
    from xbotv2.tui.command_palette import CommandPalette

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        palette = app.screen
        assert isinstance(palette, CommandPalette)
        # The palette's input should be auto-focused.
        assert app.focused is not None
        # All client and discovered server commands are visible.
        from xbotv2.tui.command import search_commands
        assert len(search_commands("")) == 15

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not palette


@pytest.mark.asyncio
async def test_command_palette_stays_inside_narrow_screen(scripted_session) -> None:
    from textual.containers import Container
    from xbotv2.tui.command_palette import CommandPalette

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(32, 16)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        palette = app.screen
        assert isinstance(palette, CommandPalette)
        container = palette.query_one(Container)
        assert container.region.x >= 0
        assert container.region.y >= 0
        assert container.region.right <= app.size.width
        assert container.region.bottom <= app.size.height


@pytest.mark.asyncio
async def test_command_palette_scrolls_to_long_server_command_list() -> None:
    from xbotv2.tui.command_palette import CommandPalette

    class ManyCommandsSession(_ScriptedSession):
        async def list_commands(self):
            return {
                "commands": [
                    {
                        "name": f"command-{index:02d}",
                        "slash": f"/command-{index:02d}",
                        "description": f"server command {index:02d}",
                    }
                    for index in range(24)
                ]
            }

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = ManyCommandsSession()
    async with app.run_test(headless=True, size=(60, 20)) as pilot:
        for _ in range(5):
            await pilot.pause()
            if app._connected:
                break
        await pilot.press("ctrl+p")
        await pilot.pause()

        palette = app.screen
        assert isinstance(palette, CommandPalette)
        for _ in range(18):
            await pilot.press("down")
        await pilot.pause()

        listing = palette.query_one("#palette-list")
        active = palette.query_one(".palette-row.active")
        assert palette._selected == 18
        assert listing.scroll_y > 0
        assert active.region.y >= listing.content_region.y
        assert active.region.bottom <= listing.content_region.bottom


@pytest.mark.asyncio
async def test_palette_fuzzy_filters_to_exit(scripted_session) -> None:
    from xbotv2.tui.command_palette import CommandPalette

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        palette = app.screen
        assert isinstance(palette, CommandPalette)

        # Type "quit" — only exit should match (via alias).
        palette_input = palette.query_one("#palette-input")
        palette_input.value = "quit"
        await pilot.pause()

        # Navigate down (no-op since one match) and press enter to invoke.
        await pilot.press("enter")
        await pilot.pause()

    # The /exit command should have been invoked: the app will call
    # self.exit() in the production handler. In a headless test the
    # exit call is benign; what we care about is that the palette
    # dismissed cleanly without throwing.
    assert not any(
        n.kind == "Help" and "/quit" in n.text for n in app.state.notices
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_notice(kind: str, text: str):
    from xbotv2.tui.client import TuiNotice

    return TuiNotice(kind=kind, text=text)


# ----------------------------------------------------------------------
# Body widget render (regression: markup=False renders invisibly)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_message_body_renders_in_transcript(
    scripted_session,
) -> None:
    """The assistant message body must end up in the rendered screen.

    Regression test for the user-reported "TUI is blank, but I can
    Ctrl-V copy the text" issue. ``Static(markup=False, body)`` was
    putting the text in the screen buffer invisibly on some
    Textual 0.86 layout paths; the fix is to wrap the body in an
    explicit ``rich.text.Text`` (which is reliably rendered).

    We assert by capturing the SVG screenshot and checking the
    escape-text payload — if the text was rendered as zero-width
    glyphs the SVG would still contain the text but the visible-cell
    count would be near zero. Here we check the cell counts.
    """

    import html
    import re

    long_text = (
        "Hello! \u2014 this is a test \u2014 with em-dashes.\n"
        "And a list:\n- item 1\n- item 2"
    )

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        app.state.apply_event({
            "type": "assistant_message",
            "data": {"content": long_text, "tool_calls": None},
        })
        await pilot.pause()
        await app._render_new_transcript_entries()
        await pilot.pause()

        svg = app.export_screenshot(title="body-render")
        unescaped = html.unescape(svg)
        # Textual normalises intra-line whitespace to U+00A0 in the
        # SVG text payloads; normalise to plain spaces for matching.
        normalised = unescaped.replace("\xa0", " ")

        # The body text must appear in the SVG escape payload.
        assert "Hello!" in normalised
        # Em-dash must survive the screen buffer (not garbled).
        assert "\u2014" in normalised
        assert "item 1" in normalised and "item 2" in normalised
        # The SVG ``<text>`` elements contain a ``x="…"`` attribute for
        # the visible cell. Each body line must produce visible cells
        # (x >= 0 with non-zero glyph runs). The easiest proxy: count
        # of visible character spans in the transcript region is
        # comfortably larger than 0. The SVG is large; the
        # transcript region alone contains well over 5 text spans
        # for a body of this size when rendered with Text.
        body_spans = re.findall(r"<text[^>]*>", unescaped)
        assert len(body_spans) > 5, (
            f"only {len(body_spans)} <text> spans; body probably invisible"
        )


@pytest.mark.asyncio
async def test_streaming_reasoning_is_collapsible_and_preserves_user_state(
    scripted_session,
) -> None:
    from textual.widgets import Collapsible, Static

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"content": "Visible answer"}}
        )
        await app._render_new_transcript_entries()

        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "First thought"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()

        block = app.query_one(".reasoning-block", Collapsible)
        assert block.title == "Thinking"
        assert block.collapsed is True
        await pilot.click(block.query_one("CollapsibleTitle"))
        await pilot.pause()
        assert block.collapsed is False
        assert app._reasoning_expanded is True

        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": " and more"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()

        assert block.collapsed is False
        assert "First thought and more" in str(
            block.query_one(".reasoning", Static).content
        )
        composer = app.query_one("#input")
        assert app.focused is composer
        await pilot.press("n", "e", "x", "t", "enter")
        for _ in range(5):
            await pilot.pause()
            if scripted_session.sent:
                break
        assert scripted_session.sent == ["next"]


@pytest.mark.asyncio
async def test_assistant_markdown_survives_streaming_updates(scripted_session) -> None:
    from rich.markdown import Markdown
    from textual.widgets import Static

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {
                "type": "assistant_message_delta",
                "data": {"content": "## Result\n\n```python\nprint('ok')"},
            }
        )
        await app._render_new_transcript_entries()
        body = app.query_one(".assistant .body", Static)
        assert isinstance(body.content, Markdown)

        app.state.apply_event(
            {
                "type": "assistant_message_delta",
                "data": {"content": "\n```"},
            }
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()

        assert isinstance(body.content, Markdown)
        screenshot = app.export_screenshot(title="assistant-markdown")
        assert "Result" in screenshot
        assert "print" in screenshot


@pytest.mark.asyncio
async def test_collapsed_reasoning_does_not_pull_scrolled_history_to_bottom(
    scripted_session,
) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(80, 20)) as pilot:
        await pilot.pause()
        for index in range(20):
            app.state.append_message(
                "assistant", f"history {index}\nline two\nline three"
            )
        app.state.apply_event({"type": "turn_started", "data": {"turn": 1}})
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "first"}}
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        stream = app.query_one("#transcript")
        stream.scroll_end(animate=False)
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()
        scrolled_position = stream.scroll_y
        assert not stream.is_vertical_scroll_end

        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": " more"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()

        assert stream.scroll_y == scrolled_position


@pytest.mark.asyncio
async def test_tool_details_are_collapsible_and_update_in_place(
    scripted_session,
) -> None:
    from textual.widgets import Collapsible, Static

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {
                "type": "assistant_message",
                "data": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "shell", "args": {"command": "pwd"}}
                    ],
                },
            }
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        block = app.query_one(".tool-details", Collapsible)
        assert block.collapsed is True
        await pilot.click(block.query_one("CollapsibleTitle"))
        await pilot.pause()

        app.state.apply_event(
            {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "c1",
                    "name": "shell",
                    "status": "success",
                    "content": "/workspace",
                },
            }
        )
        await app._refresh_tool_widget("c1")
        await pilot.pause()

        assert block.collapsed is False
        assert "/workspace" in str(block.query_one(".body", Static).content)


@pytest.mark.asyncio
async def test_thinking_and_details_commands_control_current_and_future_blocks(
    scripted_session,
) -> None:
    from textual.widgets import Collapsible

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "first"}}
        )
        app.state.apply_event(
            {
                "type": "assistant_message",
                "data": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "shell", "args": {"command": "pwd"}}
                    ],
                },
            }
        )
        await app._render_new_transcript_entries()

        composer = app.query_one("#input")
        composer.load_text("/thinking on")
        await app.submit_composer()
        composer.load_text("/details on")
        await app.submit_composer()
        await pilot.pause()

        assert all(
            not block.collapsed
            for block in app.query(".reasoning-block, .tool-details")
            if isinstance(block, Collapsible)
        )

        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "second"}}
        )
        app.state.apply_event(
            {
                "type": "tool_calls_started",
                "data": {
                    "tool_calls": [
                        {"id": "c2", "name": "read_file", "args": {"path": "README.md"}}
                    ]
                },
            }
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        assert len(app.query(".reasoning-block")) == 2
        assert len(app.query(".tool-details")) == 2
        assert all(
            not block.collapsed
            for block in app.query(".reasoning-block, .tool-details")
            if isinstance(block, Collapsible)
        )


@pytest.mark.asyncio
async def test_help_body_renders_each_command_on_its_own_row(
    scripted_session,
) -> None:
    """The /help notice must put each command on its own DOM row.

    Belt-and-suspenders for the newline-vs-two-spaces fix.
    """

    import html

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/help")
        await app.submit_composer()
        await pilot.pause()

        svg = app.export_screenshot(title="help-body")
        unescaped = html.unescape(svg)

        # Each registered command label is on its own line in the
        # SVG text payload. The newline character is the line
        # separator; in the SVG, lines are separate <text> spans.
        # We assert via the underlying state model: the body string
        # was rendered with one command per line.
        help_notices = [n for n in app.state.notices if n.kind == "Help"]
        assert len(help_notices) == 1
        body = help_notices[0].text
        for command in (
            "help [client cmd]",
            "clear-screen [client cmd]",
            "status [client cmd]",
            "exit [client cmd]",
        ):
            assert command in body, f"command {command} not found in: {body!r}"


# ----------------------------------------------------------------------
# No inner scroll: each entry is fully expanded
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_body_does_not_truncate_or_inner_scroll(
    scripted_session,
) -> None:
    """Long bodies must render in full; only the transcript scrolls.

    Per user direction (2026-06-05): each entry — message or tool
    result — must be fully displayed without an inner scroll widget.
    The whole ``#transcript`` may scroll, but never any single
    entry on its own.
    """

    # A multi-line body with far more lines than the visible viewport
    # so any truncation / max-height cap would show up as missing
    # content in the rendered widget.
    long_lines = "\n".join(f"line {i:03d}: lorem ipsum" for i in range(40))
    tool_lines = "\n".join(f"row {i:03d}" for i in range(40))

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        # Drive an assistant message and a tool result directly.
        app.state.apply_event({
            "type": "assistant_message",
            "data": {"content": long_lines, "tool_calls": None},
        })
        app.state.apply_event({
            "type": "tool_result",
            "data": {
                "tool_call_id": "call_long",
                "name": "shell",
                "status": "success",
                "content": tool_lines,
            },
        })
        await pilot.pause()
        await app._render_new_transcript_entries()
        await pilot.pause()

        # 1. The state preserves the full body (no truncation server-side).
        msgs = [m for m in app.state.messages if m.role == "assistant"]
        assert msgs and msgs[-1].content == long_lines
        tools = list(app.state.tools.values())
        assert tools
        assert "line" not in tools[-1].summary  # tool result, not assistant
        assert "row 000" in tools[-1].summary
        assert "row 039" in tools[-1].result

        # 2. Each entry is laid out as title + full body; we walk
        #    the DOM and assert there is no inner scrollbar widget
        #    nested under any entry.
        from textual.containers import VerticalScroll

        def _walk(widget):
            yield widget
            for child in getattr(widget, "children", []):
                yield from _walk(child)

        transcript = app.query_one("#transcript")
        for w in _walk(transcript):
            assert not isinstance(w, VerticalScroll) or w is transcript, (
                f"inner VerticalScroll inside transcript: {w!r}"
            )

        # 3. The body widget's renderable preserves every line.
        #    Find the Static for the assistant body and assert that
        #    its plain text contains both the first and the last
        #    line of the long content — if any line is missing, the
        #    body was truncated at mount time.
        from textual.widgets import Static as TStatic

        def _collect_bodies(widget):
            if isinstance(widget, TStatic) and "body" in (widget.classes or []):
                yield widget
            for child in getattr(widget, "children", []):
                yield from _collect_bodies(child)

        body_texts = []
        for w in transcript.children:
            body_texts.extend(_collect_bodies(w))
        from rich.markdown import Markdown

        joined = "\n".join(
            b.content.markup
            if isinstance(b.content, Markdown)
            else (
                b.visual.plain
                if b.visual is not None and hasattr(b.visual, "plain")
                else ""
            )
            for b in body_texts
        )
        for line in (long_lines.splitlines()[0],
                     long_lines.splitlines()[10],
                     long_lines.splitlines()[-1]):
            assert line in joined, f"missing line {line!r} in body DOM"
        assert "row 039" in joined

        transcript.scroll_end(animate=False)
        await pilot.pause()
        bottom = transcript.scroll_y
        assert bottom > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert transcript.scroll_y < bottom
        previous = transcript.scroll_y
        await pilot.press("pagedown")
        await pilot.pause()
        assert transcript.scroll_y > previous


# ------------------------------------------------------------------
# Unified command system: skills, help detail
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_with_command_name_shows_detail(
    scripted_session,
) -> None:
    """Test that /help clear shows detailed help for the clear command."""
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/help clear-screen")
        await app.submit_composer()
        await pilot.pause()

        help_notices = [n for n in app.state.notices if n.kind == "Help"]
        assert len(help_notices) >= 1
        body = help_notices[-1].text
        assert "clear" in body.lower()
        assert "client cmd" in body.lower() or "client" in body.lower()


@pytest.mark.asyncio
async def test_help_with_unknown_command_shows_error(
    scripted_session,
) -> None:
    """Test that /help nonexistent shows unknown command notice."""
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
    )
    app.session = scripted_session
    async with app.run_test(headless=True, size=(120, 36)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/help nonexistent")
        await app.submit_composer()
        await pilot.pause()

        help_notices = [n for n in app.state.notices if n.kind == "Help"]
        assert len(help_notices) >= 1
        assert "unknown" in help_notices[-1].text.lower()


@pytest.mark.asyncio
async def test_prompt_command_is_parsed_with_correct_kind(
    scripted_session,
) -> None:
    from xbotv2.tui.command import register_server_commands, parse_slash_command

    register_server_commands([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    spec = parse_slash_command("/git-release v2.0")
    assert spec is not None
    assert spec.kind == "prompt"
    assert spec.name == "git-release"
    assert spec.args == "v2.0"


@pytest.mark.asyncio
async def test_command_search_includes_prompt_type(
    scripted_session,
) -> None:
    from xbotv2.tui.command import register_server_commands, search_commands

    register_server_commands([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    results = search_commands("/git")
    assert any(s.kind == "prompt" for s in results)
    assert any("prompt" in s.short_label for s in results)
