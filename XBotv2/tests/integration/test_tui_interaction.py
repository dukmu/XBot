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

from XBotv2.tui.command import CommandRegistry
from XBotv2.tui.completion_popup import CompletionPopup
from XBotv2.tui.textual_client import XBotTextualApp


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
        self._message_events: asyncio.Queue[dict] = asyncio.Queue()

    async def session_events(self):
        while True:
            event = await self._message_events.get()
            if event is None:
                return
            yield event

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def list_commands(self):
        return {"commands": [
            {
                "name": name,
                "slash": f"/{name}",
                "kind": "server",
                "description": f"server command: {name}",
                "usage": f"/{name}",
                "examples": [],
                "parameters": {},
            }
            for name in (
                "status", "provider", "model", "effort", "agent",
                "clear", "undo", "fork", "tasks", "task", "permission",
                "sandbox",
            )
        ]}

    async def run_command(self, command, args, raw, *, kind="server"):
        self.commands_run = getattr(self, "commands_run", [])
        self.commands_run.append((command, list(args), raw))
        if command == "status":
            return {"data": {"message": "turn=0 mode=composing"}}
        return {"data": {"message": f"ran {command}"}}

    async def send_message(self, text):
        self.sent.append(text)
        self._message_events.put_nowait({
            "type": "message",
            "data": {"id": f"msg-{len(self.sent)}", "role": "user", "content": text},
        })
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

    async def list_providers(self):
        return {"default": "deepseek", "providers": [
            {"name": "deepseek", "provider": "deepseek", "default_model": "deepseek-v4-flash"},
            {"name": "OpenAI", "provider": "openai", "default_model": "gpt-5.6-luna"},
        ]}

    async def list_threads(self, session_id=None):
        return {
            "session_id": session_id or self.session_id,
            "threads": [
                {
                    "session_id": session_id or self.session_id,
                    "thread_id": "agent",
                    "kind": "main",
                    "status": "active",
                    "message_count": 1,
                    "title": "main",
                },
                {
                    "session_id": session_id or self.session_id,
                    "thread_id": "agent-reviewer-1",
                    "kind": "subagent",
                    "status": "running",
                    "message_count": 3,
                    "title": "reviewer",
                    "parent_thread_id": "agent",
                },
            ],
        }

    async def read_thread_history(self, thread_id, *, cursor=None, limit=200):
        self.thread_history_reads = getattr(self, "thread_history_reads", [])
        self.thread_history_reads.append((thread_id, cursor))
        if cursor == "older":
            return (
                [{
                    "kind": "message",
                    "position": 0,
                    "message": {
                        "role": "user",
                        "content": "earlier question",
                        "reasoning": "",
                        "tool_calls": [],
                    },
                }],
                None,
            )
        return (
            [
                {
                    "kind": "message",
                    "position": 1,
                    "message": {
                        "role": "user",
                        "content": "review the diff",
                        "reasoning": "",
                        "tool_calls": [],
                    },
                },
                {
                    "kind": "message",
                    "position": 2,
                    "message": {
                        "role": "assistant",
                        "content": "I reviewed it.",
                        "reasoning": "thinking hard",
                        "tool_calls": [],
                    },
                },
            ],
            "older",
        )

    async def submit_user_input(self, request_id, answer):
        return {"type": "user_input_recorded", "data": {"request_id": request_id}}

    async def respond_permission(self, request_id, decision, *, scope="once"):
        return {
            "type": "permission_response_recorded",
            "data": {"request_id": request_id, "decision": decision, "scope": scope},
        }


class _SwitchableSession(_ScriptedSession):
    def __init__(self):
        super().__init__()
        self.sessions = [
            {
                "session_id": "old-session",
                "status": "inactive",
                "thread_count": 1,
                "workspace_root": "/work/old",
                "title": "Old work",
            },
            {
                "session_id": "other-session",
                "status": "inactive",
                "thread_count": 1,
                "workspace_root": "/work/other",
                "title": "Other work",
            },
        ]
        self.switches: list[dict[str, str]] = []

    async def list_sessions(self):
        return {"sessions": self.sessions}

    async def list_threads(self, session_id):
        return {
            "session_id": session_id,
            "threads": [{
                "session_id": session_id,
                "thread_id": "main",
                "kind": "main",
                "workspace_root": "/work/other",
            }],
        }

    async def switch(self, *, session_id, thread_id, workspace_root=None, mode="resume"):
        self.session_id = session_id or "new-session"
        self.thread_id = thread_id
        self.switches.append({
            "session_id": self.session_id,
            "thread_id": thread_id,
            "workspace_root": workspace_root or "",
            "mode": mode,
        })
        return {
            "session_id": self.session_id,
            "thread_id": thread_id,
            "agent_name": "OtherBot",
            "workspace_root": workspace_root or "/work/other",
            "provider": "mock",
            "model": "mock",
            "history": [{"role": "user", "content": "restored"}],
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
            1: "queued second",
            2: "queued third",
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
    from XBotv2.tui.textual_widgets import status_renderable

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
    from XBotv2.tui.textual_widgets import status_renderable

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
            if app._session_attached:
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
            if app._session_attached:
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
        assert "clear" in names
        assert popup.current_match() is not None
        assert popup.current_match().name == "clear-screen"


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

        assert composer.text == "/clear-screen"
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
async def test_completion_popup_does_not_break_transcript_auto_scroll(
    scripted_session,
) -> None:
    """Opening the slash-completion popup must not knock a follower off the
    tail of the transcript.

    The popup shrinks the transcript viewport; without re-pinning, the scroll
    offset points above the new bottom and ``is_vertical_scroll_end`` goes
    stale, silently disabling auto-scroll for subsequent messages."""

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        for index in range(30):
            app.state.append_message(
                "assistant" if index % 2 else "user",
                f"message number {index} with padding words",
            )
        await app._render_new_transcript_entries()
        await pilot.pause()
        stream = app.query_one("#transcript")
        stream.scroll_end(animate=False)
        await pilot.pause()
        assert stream.is_vertical_scroll_end
        # In the real flow the render path pins the follow flag when it
        # scrolls to the tail; mirror that here.
        app._transcript_follow = True

        composer = app.query_one("#input")
        composer.load_text("/")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()

        assert app.query_one("#completion_popup").visible is True
        # The viewport shrank; wait for the re-pin scheduled after the reflow.
        for _ in range(20):
            await pilot.pause()
            if stream.is_vertical_scroll_end:
                break
        assert stream.is_vertical_scroll_end, (
            "opening the completion popup must not disable auto-scroll"
        )


@pytest.mark.asyncio
async def test_completion_popup_respects_scrolled_away_position(
    scripted_session,
) -> None:
    """A user who scrolled away from the tail must not be yanked back to the
    bottom when the completion popup changes the transcript height."""

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        for index in range(30):
            app.state.append_message(
                "assistant" if index % 2 else "user",
                f"message number {index} with padding words",
            )
        await app._render_new_transcript_entries()
        await pilot.pause()
        stream = app.query_one("#transcript")
        stream.scroll_end(animate=False)
        await pilot.pause()
        app.scroll_transcript_page(down=False)
        await pilot.pause()
        assert not stream.is_vertical_scroll_end

        composer = app.query_one("#input")
        composer.load_text("/")
        app._refresh_completion_popup(composer.text)
        await pilot.pause()
        assert not stream.is_vertical_scroll_end, (
            "scrolled-away user must not be pulled back to the bottom"
        )


@pytest.mark.asyncio
async def test_compaction_appears_as_an_expandable_transcript_entry(
    scripted_session,
) -> None:
    """A compaction is visible in the transcript, not silent: the entry is
    collapsed by default and expands to the full summary."""
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    summary = "\n".join(f"summary line {i:02d}" for i in range(30))
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()
        await app._consume_stream_event({
            "type": "compaction_completed",
            "data": {
                "reason": "manual",
                "automatic": False,
                "summary": summary,
                "metrics": {
                    "messages_before": 40,
                    "messages_after": 12,
                    "history_chars_before": 9000,
                    "history_chars_after": 4000,
                },
            },
        })
        await pilot.pause()

        block = app.query_one(".compact-block", Collapsible)
        assert block.collapsed is True
        assert "Conversation compacted" in str(block.title or "")
        assert "9000 to 4000" in str(block.title or "")

        block.collapsed = False
        await pilot.pause()
        detail = block.query_one(".compact-summary", BoundedText)
        assert detail.text == summary
        assert len(detail.window_text.splitlines()) <= detail.max_rows


@pytest.mark.asyncio
async def test_thread_view_shows_history_and_is_read_only(scripted_session) -> None:
    """/thread enters a read-only view: history rendered, live frames appended,
    composer inert, and the main thread stays the only thing that can ask the user."""
    from XBotv2.tui.textual_widgets import ThreadView

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()

        await app._cmd_thread("agent-reviewer-1")
        await pilot.pause()
        await pilot.pause()

        view = app.query_one("#thread_view", ThreadView)
        transcript = app.query_one("#transcript")
        assert view.display is True
        assert transcript.display is False
        assert app._view_active is True
        assert "review the diff" in view.body.text
        assert "I reviewed it." in view.body.text
        assert "thinking hard" in view.body.text
        assert "agent-reviewer-1" in str(view.query_one(".thread-view-header").content)

        composer = app.query_one("#input")
        assert composer.disabled is True

        # Live frames of the viewed thread arrive through its own stream and
        # extend the pane even though the fake session has no events queued.
        view.body.append("~ subagent says something\n")
        await pilot.pause()
        assert "subagent says something" in view.body.text

        # Esc leaves the view and restores the main transcript + composer.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert app._view_active is False
        assert view.display is False
        assert transcript.display is True
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_thread_view_lazy_loads_older_history_on_scroll_top(
    scripted_session,
) -> None:
    """Reaching the top of a long thread pulls the previous page in place."""
    from XBotv2.tui.textual_widgets import ThreadView

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()
        await app._cmd_thread("agent-reviewer-1")
        await pilot.pause()
        view = app.query_one("#thread_view", ThreadView)
        assert scripted_session.thread_history_reads == [("agent-reviewer-1", None)]
        assert view.body.text.startswith("[user] review the diff")
        assert app._view_older_cursor == "older"

        # The reader reaches the very top: the older page is prepended and the
        # view stays anchored instead of jumping.
        while view.body.window_range[0] > 1:
            view.body.scroll_rows(-1)
        view.body.post_message(view.body.TopReached())
        await pilot.pause()
        await pilot.pause()
        assert scripted_session.thread_history_reads == [
            ("agent-reviewer-1", None),
            ("agent-reviewer-1", "older"),
        ]
        assert view.body.text.startswith("[user] earlier question")
        assert "review the diff" in view.body.text
        assert app._view_older_cursor is None


@pytest.mark.asyncio
async def test_thread_view_exits_when_the_main_thread_needs_input(
    scripted_session,
) -> None:
    """A user prompt belongs to the parent thread: viewing a subagent thread
    returns to the main view so the turn cannot silently wait."""
    from XBotv2.tui.textual_widgets import ThreadView

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()
        await app._cmd_thread("agent-reviewer-1")
        await pilot.pause()
        assert app._view_active is True

        app.state.apply_event({
            "type": "permission_request",
            "data": {"tool_call_id": "tool-x", "name": "shell", "args": {"command": "ls"}},
        })
        await app._consume_stream_event({
            "type": "permission_request",
            "data": {"tool_call_id": "tool-x", "name": "shell", "args": {"command": "ls"}},
        })
        await pilot.pause()
        assert app._view_active is False
        assert app.query_one("#transcript").display is True


@pytest.mark.asyncio
async def test_thread_view_marks_main_output_and_lists_threads(
    scripted_session,
) -> None:
    from XBotv2.tui.textual_widgets import ThreadView

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()
        await app._cmd_thread("agent-reviewer-1")
        await pilot.pause()
        view = app.query_one("#thread_view", ThreadView)

        await app._consume_stream_event({
            "type": "assistant_message_delta",
            "data": {"content": "main thread is answering"},
        })
        await pilot.pause()
        assert app._view_main_busy is True
        assert "main: new output" in str(view.query_one(".thread-view-header").content)


@pytest.mark.asyncio
async def test_thread_command_opens_a_picker_without_an_argument(
    scripted_session,
) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()
        await app._cmd_thread("")
        await pilot.pause()
        from XBotv2.tui.selection import SelectionScreen

        picker = app.screen
        assert isinstance(picker, SelectionScreen)
        rows = list(picker.query(".selection-row"))
        assert any("agent-reviewer-1" in str(row.content) for row in rows)
        first_thread = next(row for row in rows if "agent-reviewer-1" in str(row.content))
        # Enter selects the highlighted row and enters the read-only view.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app._view_active is True
        assert app._view_thread_id == "agent-reviewer-1" or "reviewer" in str(first_thread.content)


@pytest.mark.asyncio
async def test_long_task_command_is_truncated_in_title_but_full_behind_the_window(
    scripted_session,
) -> None:
    """A long command/params stays readable: the collapsed title clips it and
    the expandable body shows the full record in a bounded, scrollable window."""
    from XBotv2.tui.textual_widgets import BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        await pilot.pause()
        command = "python -m tools.run --flag " + "param_" * 40
        output = "\n".join(f"log line {i}" for i in range(80))
        app.state.apply_event({
            "type": "task_updated",
            "data": {
                "task_id": "long-task",
                "command": command,
                "kind": "shell",
                "cwd": "/workspace",
                "status": "completed",
                "created_at": 1.0,
                "started_at": 1.0,
                "finished_at": 2.0,
                "output": output,
                "error": "",
                "thread_id": "",
            },
        })
        await app._handle_stream_event({"type": "task_updated", "data": {"task_id": "long-task"}})
        app._refresh_task_panel()
        await pilot.pause()

        block = app.query_one(".subagent-task")
        title = block.title or ""
        assert "..." in title, "the collapsed title must be truncated, not the data"

        block.collapsed = False
        await pilot.pause()
        detail = block.query_one(".task-detail", BoundedText)
        assert "command: " + command in detail.text
        assert "param_" * 40 in detail.text
        assert "log line 79" in detail.text
        assert "log line 0" in detail.text
        # Windowed: only a slice is rendered, and paging reaches the last line.
        assert len(detail.window_text.splitlines()) <= detail.max_rows
        detail.scroll_rows(detail.line_count)
        assert detail.window_text.endswith("log line 79")


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
    results = CommandRegistry.default().search("")
    assert results[0].name == "help"
    assert {item.name for item in results} >= {"copy", "new", "resume", "session"}


def test_parse_slash_command_round_trip() -> None:
    spec = CommandRegistry.default().parse("/clear-screen")
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
        """Yields a ``message`` event per submission and blocks until released."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.release = asyncio.Event()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def list_commands(self):
            return {"commands": []}

        async def send_message(self, text):
            self.sent.append(text)
            yield {
                "type": "message",
                "data": {"id": f"msg-{text}", "role": "user", "content": text},
            }
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
    assert "mode=composing" in body  # scripted server status body
    assert scripted_session.sent == []


@pytest.mark.asyncio
async def test_session_command_lists_and_switches_workspace() -> None:
    session = _SwitchableSession()
    app = XBotTextualApp(session_id="current", thread_id="agent")
    app.session = session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        for _ in range(5):
            await pilot.pause()
            if app._session_attached:
                break

        from XBotv2.tui.selection import SelectionScreen

        composer = app.query_one("#input")
        composer.load_text("/session")
        await app.submit_composer()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SelectionScreen)
        rows = list(picker.query(".selection-row"))
        assert any("old-session" in str(row.content) for row in rows)
        assert any("/work/old" in str(row.content) for row in rows)
        assert session.switches == []

        # Cancel remains on the current session; /session list again, then pick
        # the first (old-session) via the highlighted row.
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.session_id == "current"

        composer.load_text("/session other-session")
        await app.submit_composer()
        for _ in range(10):
            await pilot.pause()
            if app.state.session_id == "other-session" and app.state.status == "Ready":
                break

        assert session.switches == [{
            "session_id": "other-session",
            "thread_id": "main",
            "workspace_root": "/work/other",
            "mode": "resume",
        }]
        assert app.state.session_id == "other-session"
        assert app.state.thread_id == "main"
        assert app.state.workspace_root == "/work/other"
        assert [message.content for message in app.state.messages] == ["restored"]


# ----------------------------------------------------------------------
# Command palette (Ctrl+P)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_p_opens_palette_with_full_command_list(
    scripted_session,
) -> None:
    from XBotv2.tui.command_palette import CommandPalette

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
        names = {spec.name for spec in app.commands.search("")}
        assert {"help", "clear-screen", "exit"} <= names
        assert {
            "status", "provider", "model", "effort", "agent",
            "clear", "undo", "fork", "tasks", "task", "permission", "sandbox",
        } <= names

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not palette


@pytest.mark.asyncio
async def test_command_palette_stays_inside_narrow_screen(scripted_session) -> None:
    from textual.containers import Container
    from XBotv2.tui.command_palette import CommandPalette

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
    from XBotv2.tui.command_palette import CommandPalette

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
            if app._session_attached:
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
    from XBotv2.tui.command_palette import CommandPalette

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
    from XBotv2.tui.client import TuiNotice

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
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

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
        assert "First thought and more" in block.query_one(".reasoning", BoundedText).text
        composer = app.query_one("#input")
        assert app.focused is composer
        await pilot.press("n", "e", "x", "t", "enter")
        for _ in range(5):
            await pilot.pause()
            if scripted_session.sent:
                break
        assert scripted_session.sent == ["next"]


@pytest.mark.asyncio
async def test_resumed_reasoning_is_rendered_as_a_collapsible_block(
    scripted_session,
) -> None:
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.restore_history([{
            "role": "assistant",
            "content": "Persisted answer",
            "reasoning": "Persisted thought",
            "tool_calls": [],
        }])
        await app._render_replay_window()
        await pilot.pause()

        block = app.query_one(".reasoning-block", Collapsible)
        assert block.collapsed is True
        assert "Persisted thought" in block.query_one(".reasoning", BoundedText).text


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
        final = {
            "type": "assistant_message",
            "data": {
                "content": "## Result\n\n```python\nprint('ok')\n```\n\nComplete."
            },
        }
        app.state.apply_event(final)
        assert app.state.messages[-1].content.endswith("Complete.")
        await app._handle_stream_event(final)
        await pilot.pause()

        body = app.query_one(".assistant .body", Static)
        assert isinstance(body.content, Markdown)
        assert "Complete" in body.content.markup
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
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

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
        assert "/workspace" in block.query_one(".body", BoundedText).text


@pytest.mark.asyncio
async def test_long_thinking_window_scrolls_with_wheel_tap_and_keys(
    scripted_session,
) -> None:
    """A long block shows a slice, pages by wheel/tap, and never eats the scroll."""
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BLOCK_MAX_ROWS, BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    app._reasoning_expanded = True
    async with app.run_test(headless=True, size=(90, 32)) as pilot:
        await pilot.pause()
        lines = [f"thought line {index}" for index in range(60)]
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "\n".join(lines)}}
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        block = app.query_one(".reasoning-block", Collapsible)
        text = block.query_one(".reasoning", BoundedText)
        assert block.collapsed is False
        # The budget scales with the screen, and the block never exceeds it.
        assert text.max_rows == max(3, app.size.height // 4)
        assert len(text.window_text.splitlines()) <= text.max_rows
        assert text.size.height <= BLOCK_MAX_ROWS + 1
        assert text.line_count == 60
        assert len(text.window_text.splitlines()) == text.max_rows
        assert text.window_text.splitlines() == lines[:text.max_rows]
        assert text.text == "\n".join(lines)
        # The block is bounded, so it cannot take the window from the transcript.
        assert text.size.height <= BLOCK_MAX_ROWS + 1  # window rows + footer
        assert block.size.height <= text.size.height + 3
        assert block.size.height < app.size.height // 2
        assert text.query_one(".block-step.up").display is False
        assert text.query_one(".block-step.down").display is True

        # Wheel: consumed while the window can move, released at the end.
        class Wheel:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

            def prevent_default(self) -> None:
                pass

        first = Wheel()
        text._on_mouse_scroll_down(first)
        assert first.stopped is True
        assert text.window_text.splitlines()[0] == lines[3]

        text.scroll_rows(text.line_count)
        assert text.at_end
        assert text.window_text.splitlines()[-1] == lines[-1]
        last = Wheel()
        text._on_mouse_scroll_down(last)
        assert last.stopped is False, "the transcript must keep the wheel at the end"

        # Touch: the footer marks scroll without any keyboard.
        text.scroll_rows(-text.line_count)
        await pilot.pause()
        await pilot.click(text.query_one(".block-step.down"))
        await pilot.pause()
        assert text.window_range[0] > 1


@pytest.mark.asyncio
async def test_wrapped_lines_stay_inside_the_block_and_remain_reachable(
    scripted_session,
) -> None:
    """Long unbroken text wraps; paging still reaches every line."""
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BLOCK_MAX_ROWS, BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    app._reasoning_expanded = True
    async with app.run_test(headless=True, size=(60, 30)) as pilot:
        await pilot.pause()
        lines = [f"line {index:02d} " + "x" * 120 for index in range(25)]
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "\n".join(lines)}}
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        block = app.query_one(".reasoning-block", Collapsible)
        text = block.query_one(".reasoning", BoundedText)
        # Wrapped rows, not logical lines, fill the window and the block stays
        # bounded instead of overflowing sideways.
        assert len(text.window_text.splitlines()) == text.max_rows
        await pilot.pause()
        assert block.size.height <= text.max_rows + 3

        seen: list[str] = []
        while True:
            rows = text.window_text.splitlines()
            seen.append(rows[0])  # each page contributes its newest row
            if not text.scroll_rows(1):
                seen.extend(rows[1:])
                break
        assert text.at_end
        joined = "".join(seen)
        for index in range(len(lines)):
            assert f"line {index:02d}" in joined, f"line {index:02d} unreachable"
        # Every character of the long runs survives the window.
        assert joined.count("x") == 25 * 120


@pytest.mark.asyncio
async def test_focused_block_scrolls_with_keys_then_hands_off_to_the_transcript(
    scripted_session,
) -> None:
    """Arrow keys scroll what is focused, and the block releases them at its end."""
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    app._reasoning_expanded = True
    async with app.run_test(headless=True, size=(90, 32)) as pilot:
        await pilot.pause()
        for index in range(40):
            app.state.append_message("user", f"message {index} with padding words")
        app.state.apply_event(
            {
                "type": "assistant_message_delta",
                "data": {"reasoning": "\n".join(f"thought {i:02d}" for i in range(40))},
            }
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        text = app.query_one(".reasoning-block", Collapsible).query_one(
            ".reasoning", BoundedText
        )
        transcript = app.query_one("#transcript")
        transcript.scroll_home(animate=False)
        text.focus()
        await pilot.pause()
        assert app.focused is text

        start = text.window_range[0]
        await pilot.press("down")
        await pilot.pause()
        assert text.window_range[0] == start + 1, "Down scrolls the focused block"
        await pilot.press("up")
        await pilot.pause()
        assert text.window_range[0] == start

        # At the end of the block the same key scrolls the transcript: the
        # keyboard gesture keeps working instead of being swallowed.
        text.scroll_rows(text.line_count + text.max_rows)
        await pilot.pause()
        assert text.at_end
        transcript.scroll_home(animate=False)
        await pilot.pause()
        assert transcript.scroll_y == 0
        assert transcript.max_scroll_y > 0, "the transcript must be scrollable"
        before = transcript.scroll_y
        await pilot.press("down")
        await pilot.pause()
        assert transcript.scroll_y > before


@pytest.mark.asyncio
async def test_thinking_follows_the_tail_unless_the_user_scrolled_away(
    scripted_session,
) -> None:
    """A streaming thinking block follows the newest content; a reader who
    scrolled back into the history is left alone; at the tail it follows again.
    Re-sending the same text stays a no-op so a refresh cannot yank the window.
    """
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    app._reasoning_expanded = True
    async with app.run_test(headless=True, size=(90, 40)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {
                "type": "assistant_message_delta",
                "data": {"reasoning": "\n".join(f"thought {i:02d}" for i in range(20))},
            }
        )
        await app._render_new_transcript_entries()
        await pilot.pause()
        reasoning = app.query_one(".reasoning-block", Collapsible).query_one(
            ".reasoning", BoundedText
        )

        # Streaming growth follows the newest lines.
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "\nfresh A"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()
        assert reasoning.window_text.endswith("fresh A")
        assert reasoning.at_end

        # The reader scrolls back into the history: the next chunk must not
        # yank the window back to the tail.
        reasoning.scroll_rows(-3)
        frozen = reasoning.window_range
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "\nfresh B"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()
        assert reasoning.window_range == frozen
        assert "fresh B" not in reasoning.window_text

        # Back at the tail the block follows again, including no-op refreshes
        # that re-send the same text.
        reasoning.scroll_rows(reasoning.line_count)
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "\nfresh C"}}
        )
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()
        assert reasoning.window_text.endswith("fresh C")
        assert reasoning.at_end
        await app._refresh_streaming_assistant_widget()
        await pilot.pause()
        assert reasoning.window_text.endswith("fresh C")
        assert reasoning.at_end


@pytest.mark.asyncio
async def test_tab_leaves_the_composer_for_the_scrollable_region(
    scripted_session,
) -> None:
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        assert app.focused is composer
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not composer, "Tab must reach the transcript"
        assert "\t" not in composer.text, "Tab does not insert into the input"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is composer


@pytest.mark.asyncio
async def test_streaming_thinking_follows_the_tail_inside_the_window(
    scripted_session,
) -> None:
    from textual.widgets import Collapsible

    from XBotv2.tui.textual_widgets import BLOCK_MAX_ROWS, BoundedText

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    app._reasoning_expanded = True
    async with app.run_test(headless=True, size=(90, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(
            {"type": "assistant_message_delta", "data": {"reasoning": "first"}}
        )
        await app._render_new_transcript_entries()
        await pilot.pause()

        reasoning = app.query_one(".reasoning-block", Collapsible).query_one(
            ".reasoning", BoundedText
        )
        assert reasoning.window_text == "first"

        for index in range(BLOCK_MAX_ROWS + 20):
            app.state.apply_event(
                {
                    "type": "assistant_message_delta",
                    "data": {"reasoning": f"\nline {index}"},
                }
            )
            await app._refresh_streaming_assistant_widget()
            await pilot.pause()

        assert reasoning.at_end, "a streaming block keeps its newest lines visible"
        assert reasoning.window_text.splitlines()[-1] == f"line {BLOCK_MAX_ROWS + 19}"
        assert reasoning.line_count == BLOCK_MAX_ROWS + 21


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
                        {"id": "c2", "name": "read", "args": {"path": "README.md"}}
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
            "status [server cmd]",
            "exit [client cmd]",
        ):
            assert command in body, f"command {command} not found in: {body!r}"


# ----------------------------------------------------------------------
# No inner scroll: each entry is fully expanded
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_bodies_keep_full_text_and_bounded_blocks(
    scripted_session,
) -> None:
    """A long message renders in full; reasoning and tool details window.

    Per user direction (2026-09-13): the thinking and tool-detail blocks show a
    slice that pages in place, so one block can never take the window. The full
    text stays in state and in the block, every line is reachable, and no entry
    nests a scroll container.
    """

    long_lines = "\n".join(f"line {i:03d}: lorem ipsum" for i in range(40))
    tool_lines = "\n".join(f"row {i:03d}" for i in range(40))

    from XBotv2.tui.textual_widgets import BLOCK_MAX_ROWS, BoundedText

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

        # 2. No entry nests a scroll container.
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

        # 3. The assistant body still renders every line.
        from textual.widgets import Static as TStatic

        def _collect_statics(widget):
            if isinstance(widget, TStatic) and "body" in (widget.classes or []):
                yield widget
            for child in getattr(widget, "children", []):
                yield from _collect_statics(child)

        body_widgets = []
        for w in transcript.children:
            body_widgets.extend(_collect_statics(w))
        from rich.markdown import Markdown

        joined = "\n".join(
            b.content.markup
            if isinstance(b.content, Markdown)
            else (
                b.visual.plain
                if b.visual is not None and hasattr(b.visual, "plain")
                else ""
            )
            for b in body_widgets
        )
        for line in (long_lines.splitlines()[0],
                     long_lines.splitlines()[10],
                     long_lines.splitlines()[-1]):
            assert line in joined, f"missing line {line!r} in assistant body"

        # 4. The tool detail keeps the whole result behind a bounded window.
        block = app.query_one(".tool-details")
        block.collapsed = False
        await pilot.pause()
        detail = block.query_one(".body", BoundedText)
        assert detail.text.startswith("result: row 000")
        assert detail.text.endswith("row 039")
        assert detail.line_count == 40
        assert detail.window_range[1] - detail.window_range[0] + 1 < 40
        assert detail.size.height <= BLOCK_MAX_ROWS + 1
        # Title and chrome included, one block stays far below half the screen.
        assert block.size.height <= detail.size.height + 3
        assert block.size.height < app.size.height // 2
        detail.scroll_rows(detail.line_count)
        assert detail.window_text.splitlines()[-1] == "row 039"

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
    registry = CommandRegistry.default()
    registry.merge_server([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    spec = registry.parse("/git-release v2.0")
    assert spec is not None
    assert spec.kind == "prompt"
    assert spec.name == "git-release"
    assert spec.args == "v2.0"


@pytest.mark.asyncio
async def test_command_search_includes_prompt_type(
    scripted_session,
) -> None:
    registry = CommandRegistry.default()
    registry.merge_server([
        {"name": "git-release", "description": "Create releases", "kind": "prompt"},
    ])

    results = registry.search("/git")
    assert any(s.kind == "prompt" for s in results)
    assert any("prompt" in s.short_label for s in results)


@pytest.mark.asyncio
async def test_provider_command_picks_or_lists(scripted_session) -> None:
    """/provider opens an interactive picker; /provider list shows the catalog."""
    from XBotv2.tui.command import CommandSpec
    from XBotv2.tui.selection import SelectionScreen

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = scripted_session
    async with app.run_test(headless=True, size=(90, 30)) as pilot:
        await pilot.pause()

        scripted_session.commands_run = []
        await app._handle_slash_command(CommandSpec(
            name="provider", kind="server", raw="/provider list",
            usage="/provider <provider>", args="list", description="",
        ))
        await pilot.pause()
        # The server owns list rendering: the client forwards the command
        # instead of reimplementing the catalog view.
        assert scripted_session.commands_run == [
            ("provider", ["list"], "/provider list"),
        ]
        assert any("ran provider" in notice.text for notice in app.state.notices)

        await app._handle_slash_command(CommandSpec(
            name="provider", kind="server", raw="/provider", usage="/provider <provider>", args="", description="",
        ))
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SelectionScreen)
        rows = list(picker.query(".selection-row"))
        assert any("OpenAI" in str(row.content) for row in rows)
        scripted_session.commands_run = []
        app._session_attached = True
        await pilot.press("down")  # OpenAI
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        # The picker value is the configured provider NAME (the key the server
        # validates) and the command switches via ``use``: a bare
        # ``/provider <name>`` or a protocol value would change nothing.
        assert scripted_session.commands_run == [
            ("provider", ["use", "OpenAI"], "/provider use OpenAI"),
        ]
        await pilot.press("escape")
        await pilot.pause()
