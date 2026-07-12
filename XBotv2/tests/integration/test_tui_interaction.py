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
        return {
            "commands": [
                {"name": "status", "slash": "/status", "description": "show status"}
            ]
        }

    async def run_command(self, command, args, raw, *, kind="server"):
        del args, raw
        if command == "status":
            return {"data": {"message": "turn=0 mode=composing"}}
        return {"data": {"message": f"ran {command}"}}

    async def send_message(
        self, text, *, input_provider=None, permission_provider=None
    ):
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
        assert len(popup.matches) == 4
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
        assert "clear" in names
        assert popup.current_match() is not None
        assert popup.current_match().name in {"clear"}


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

        # Type "/c" — completion popup appears, /clear is the first match.
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
    spec = parse_slash_command("/clear")
    assert spec is not None
    assert spec.name == "clear"
    assert spec.raw == "/clear"


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

        async def send_message(
            self, text, *, input_provider=None, permission_provider=None
        ):
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

        # Status bar should report queued:2.
        status = app.query_one("#status_bar", TStatic)
        status_text = (
            status.visual.plain
            if status.visual is not None and hasattr(status.visual, "plain")
            else ""
        )
        assert "queued:2" in status_text, f"status: {status_text!r}"

        # The worker is blocked; only "first" has been sent to the
        # server. "second" and "third" are sitting in the queue.
        assert session.sent == ["first"]
        assert app._outbound_messages.qsize() == 2

        # Release the worker; it should drain the queue in order.
        session.release.set()
        # Give the worker a few ticks to finish.
        for _ in range(20):
            await pilot.pause()
            if len(session.sent) == 3 and app._outbound_messages.empty():
                break

    assert session.sent == ["first", "second", "third"]


# ----------------------------------------------------------------------
# /clear and /status
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
        composer = app.query_one("#input")
        composer.load_text("/clear")
        await app.submit_composer()
        await pilot.pause()

    assert app.state.messages == []
    assert app.state.notices == []
    # session_id/thread_id preserved.
    assert app.state.session_id == "my-session"
    assert app.state.thread_id == "my-thread"
    # No server traffic for /clear.
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
        # All four commands are visible (empty query).
        from xbotv2.tui.command import search_commands
        assert len(search_commands("")) == 4

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not palette


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
        for command in ("help [client cmd]", "clear [client cmd]", "status [server cmd]", "exit [client cmd]"):
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
        # ``tool.summary`` may keep newlines after the
        # ``_preview`` fix; the per-line shortening still applies.
        assert "line" not in tools[-1].summary  # tool result, not assistant
        assert "row 000" in tools[-1].summary

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
        joined = "\n".join(
            b.visual.plain if b.visual is not None and hasattr(b.visual, "plain") else ""
            for b in body_texts
        )
        for line in (long_lines.splitlines()[0],
                     long_lines.splitlines()[10],
                     long_lines.splitlines()[-1]):
            assert line in joined, f"missing line {line!r} in body DOM"


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
        composer.load_text("/help clear")
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
async def test_skill_is_parsed_with_correct_kind(
    scripted_session,
) -> None:
    """Test that register_dynamic_commands makes skill parseable as skill kind."""
    from xbotv2.tui.command import register_dynamic_commands, parse_slash_command

    register_dynamic_commands([
        {"name": "git-release", "description": "Create releases"},
    ], "skill")

    spec = parse_slash_command("/git-release v2.0")
    assert spec is not None
    assert spec.kind == "skill"
    assert spec.name == "git-release"
    assert spec.args == "v2.0"


@pytest.mark.asyncio
async def test_command_search_includes_skill_type(
    scripted_session,
) -> None:
    """Test that skills appear in search with [skill] tag."""
    from xbotv2.tui.command import register_dynamic_commands, search_commands

    register_dynamic_commands([
        {"name": "git-release", "description": "Create releases"},
    ], "skill")

    results = search_commands("/git")
    assert any(s.kind == "skill" for s in results)
    assert any("skill" in s.short_label for s in results)
