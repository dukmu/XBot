"""The app: keys, layout, and lifecycle, driven through a real Textual pilot.

This is where the whole stack meets a terminal. The scripted backend stands in
for the server; everything above it -- transport, reducer, controller, views -- is
the real code.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Static

from XBotv2.tests.tui.factories import (
    PNG_BYTES,
    SESSION,
    THREAD,
    ScriptedBackend,
    command,
    execution,
    frames,
    snapshot,
    stream,
    thread,
)
from textual.screen import Screen

from XBotv2.tui.app import TuiApp
from XBotv2.tui.view.selection import SelectionScreen
from XBotv2.tui.transport import TransportConfig
from XBotv2.tui.view.completion import CompletionPopup
from XBotv2.tui.view.composer import Composer
from XBotv2.tui.view.jobs import JobPanel
from XBotv2.tui.view.queue import QueuePanel
from XBotv2.tui.view.status_bar import StatusBar
from XBotv2.tui.view.transcript import TranscriptScroll


def app_for(
    backend: ScriptedBackend, *, new_input_id=None, workspace="", **overrides
) -> TuiApp:
    fields = {
        "session_id": SESSION,
        "thread_id": THREAD,
        "reconnect_delays": (),
        "cursor_recoveries": 0,
        "baseline_rebuilds": 0,
        **overrides,
    }
    return TuiApp(
        backend=backend,
        config=TransportConfig(**fields),
        render_interval=0.01,
        transcript_limit=10,
        new_input_id=new_input_id,
        workspace=workspace,
    )


async def settle(pilot, seconds: float = 0.06) -> None:
    """Let the render loop tick at least once."""
    await asyncio.sleep(seconds)
    await pilot.pause()


def status_text(app: TuiApp) -> str:
    content = app.query_one("#status", StatusBar).content
    return str(getattr(content, "plain", "") or "")


def transcript_text(app: TuiApp) -> str:
    parts = []
    scroll = app.query_one("#transcript", TranscriptScroll)
    for widget in scroll.children:
        for stat in widget.query(Static):
            content = getattr(stat, "content", None)
            parts.append(str(getattr(content, "plain", "") or getattr(content, "markup", "")))
    return "\n".join(parts)


# --- booting --------------------------------------------------------------


async def test_the_app_boots_and_shows_a_status_line() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert app.controller is not None
        assert status_text(app), "the status line must not be empty on boot"
        assert "Ready" in status_text(app)


async def test_the_app_reads_the_thread_so_a_running_turn_shows_as_running() -> None:
    """The reported defect, end to end: attach mid-turn and the line says so."""
    backend = ScriptedBackend(threads=(thread(turn_status="running"),))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert "Running" in status_text(app)


async def test_connecting_starts_the_background_tasks() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        names = {task.get_name() for task in app.background_tasks}
        assert names == {"tui-read", "tui-render", "tui-watch"}


# --- a turn renders -------------------------------------------------------


async def test_a_scripted_turn_reaches_the_screen() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    ("turn_started", {"turn": 1}),
                    ("assistant_message_delta", {"content": "working on it"}),
                    ("assistant_message", {"id": "a1", "content": "working on it"}),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        rendered = transcript_text(app)
        assert "working on it" in rendered
        assert "Running" in status_text(app)


async def test_the_prompt_entry_loses_its_sending_marker_once_accepted() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    ("turn_started", {"turn": 1}),
                    ("message", {"id": "in-1", "role": "user", "content": "hello"}),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend, new_input_id=lambda: "in-1")
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        await app.controller.submit("hello")
        await settle(pilot)
        rendered = transcript_text(app)
        assert "hello" in rendered
        assert "sending" not in rendered


async def test_jobs_appear_in_their_panel() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    (
                        "job_updated",
                        {
                            "job_id": "j1",
                            "kind": "agent",
                            "status": "running",
                            "command": "review the diff",
                            "cwd": "/w",
                            "created_at": 0,
                            "started_at": 1,
                            "finished_at": 0,
                        },
                    )
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        panel = app.query_one("#jobs", JobPanel)
        assert panel.rows == 1
        assert "review the diff" in panel.row_text("j1")
        assert panel.display is True
        assert "1 task" in status_text(app)


# --- keys -----------------------------------------------------------------


async def test_enter_in_the_composer_sends_a_message() -> None:
    backend = ScriptedBackend(streams=[stream(hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("please look at this")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent, "the message must reach the server"
        assert backend.sent[0]["content"] == "please look at this"
        assert composer.text == ""


async def test_escape_interrupts_the_running_turn() -> None:
    backend = ScriptedBackend(
        threads=(thread(turn_status="running"),),
        streams=[stream(*frames(("turn_started", {"turn": 1})), hold=True)],
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert backend.interrupts == 1


async def test_page_up_moves_the_transcript_window() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    *[
                        ("message", {"id": f"m{index}", "role": "user", "content": str(index)})
                        for index in range(30)
                    ]
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(80, 12)) as pilot:
        await settle(pilot)
        view = app.view
        assert view is not None
        newest = view.transcript.mounted_ids
        assert newest == tuple(f"m{index}" for index in range(20, 30))
        await pilot.press("pageup")
        await settle(pilot)
        assert view.transcript.mounted_ids == tuple(f"m{index}" for index in range(10, 20))
        await pilot.press("pagedown")
        await settle(pilot)
        assert view.transcript.mounted_ids == newest


# --- lifecycle ------------------------------------------------------------


async def test_shutting_down_cancels_every_task() -> None:
    backend = ScriptedBackend(streams=[stream(hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert app.background_tasks
    assert app.background_tasks == (), "no task may outlive the app"


async def test_a_disconnect_is_visible_on_screen() -> None:
    backend = ScriptedBackend(streams=[stream(*frames(("turn_started", {"turn": 1})))])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot, seconds=0.12)
        assert "Disconnected" in status_text(app)
        assert "stream" in transcript_text(app).lower() or "ended" in transcript_text(app).lower()


# --- slash-command completion --------------------------------------------


async def test_typing_a_slash_shows_the_completion_popup() -> None:
    from XBotv2.tui.view.completion import CompletionPopup

    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.focus()
        await pilot.press("/", "h", "e")
        await settle(pilot)
        popup = app.query_one("#completion", CompletionPopup)
        assert app.completion.visible is True
        assert popup.visible is True
        assert app.completion.current is not None
        assert app.completion.current.name == "help"


async def test_tab_accepts_the_highlighted_command() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.focus()
        await pilot.press("/", "c", "l")
        await settle(pilot)
        await pilot.press("tab")
        await settle(pilot)
        assert composer.text == "/clear-screen"
        assert app.completion.visible is False


async def test_escape_dismisses_the_popup_without_clearing_the_input() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.focus()
        await pilot.press("/", "h")
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert app.completion.visible is False
        assert composer.text == "/h"


async def test_the_popup_hides_once_arguments_are_typed() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.focus()
        await pilot.press("/", "h", "e", "l", "p", "space")
        await settle(pilot)
        assert app.completion.visible is False


async def test_the_app_adopts_the_server_command_catalogue() -> None:
    from XBotv2.commands import CommandDescription

    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        app.merge_commands((
            CommandDescription(
                name="status",
                slash="/status",
                kind="server",
                description="show the current status",
                usage="/status",
                exclusive=False,
            ),
        ))
        assert app.commands.get("status") is not None
        parsed = app.commands.parse("/status")
        assert parsed is not None and parsed.name == "status"


# --- the command palette and slash commands ------------------------------


async def test_ctrl_p_opens_the_palette_and_fills_the_composer() -> None:
    from XBotv2.tui.view.palette import CommandPalette

    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        await pilot.press("ctrl+p")
        await settle(pilot)
        assert isinstance(app.screen, CommandPalette)
        await pilot.press("down", "enter")
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        assert composer.text.startswith("/")


async def test_a_slash_command_is_run_locally_not_sent_as_a_message() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/help")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent == [], "a client command must never reach the server"
        assert "clear-screen" in transcript_text(app)


async def test_an_unknown_command_is_reported_and_not_sent() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/nope")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent == []
        assert "Unknown command" in transcript_text(app)


async def test_clear_screen_forgets_the_rendered_conversation() -> None:
    backend = ScriptedBackend(
        streams=[stream(*frames(("message", {"id": "m1", "role": "user", "content": "kept"})), hold=True)]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert "kept" in transcript_text(app)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/clear-screen")
        await pilot.press("enter")
        await settle(pilot)
        assert app.view is not None
        assert app.view.transcript.mounted_ids == ()
        assert app.controller.state.timeline.get("m1") is None
        assert backend.sent == []


async def test_copy_without_a_reply_says_so() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/copy")
        await pilot.press("enter")
        await settle(pilot)
        assert "no reply" in transcript_text(app).lower()


async def test_exit_quits_the_app() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/exit")
        await pilot.press("enter")
        await settle(pilot)
        assert app.is_running is False


def test_the_app_uses_the_theme_module() -> None:
    """Otherwise the stylesheet could rot in a file nothing reads."""
    from XBotv2.tui.theme import TUI_CSS

    assert TuiApp.CSS == TUI_CSS
    assert TuiApp.ENABLE_COMMAND_PALETTE is False, (
        "Textual's own ctrl+p palette would take the binding"
    )


# --- /session: list, pick, switch ----------------------------------------


def summary(session_id: str, *, title: str = "", status: str = "inactive"):
    from XBotv2.session.contracts import SessionSummary

    return SessionSummary(
        session_id=session_id, status=status, title=title or session_id, blank=False
    )


async def test_slash_session_switches_to_the_named_session() -> None:
    from XBotv2.tests.tui.factories import snapshot

    backend = ScriptedBackend()
    backend.session = snapshot(
        session_id="tui-e2e",
        history=[{"role": "user", "content": "old history"}],
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(
            session_id="other-session",
            history=[{"role": "user", "content": "from the other one"}],
        )
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session other-session")
        await pilot.press("enter")
        await settle(pilot)
        rendered = transcript_text(app)
        assert "from the other one" in rendered
        assert "old history" not in rendered
        assert app.controller is not None
        assert app.controller.state.session_id == "other-session"


async def test_slash_session_without_an_argument_opens_a_picker() -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    backend = ScriptedBackend(session_catalog=[summary("one"), summary("two", title="Second")])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        values = [option.value for option in app.screen.model.options]
        assert values == ["one", "two"]


async def test_picking_a_session_from_the_picker_switches_to_it() -> None:
    from XBotv2.tests.tui.factories import snapshot

    backend = ScriptedBackend(session_catalog=[summary("one"), summary("two")])
    backend.session = snapshot(session_id="tui-e2e")
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(
            session_id="two", history=[{"role": "user", "content": "picked two"}]
        )
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session")
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.state.session_id == "two"
        assert "picked two" in transcript_text(app)


async def test_a_failed_session_switch_is_reported_and_changes_nothing() -> None:
    from XBotv2.tests.tui.factories import snapshot

    backend = ScriptedBackend()
    backend.session = snapshot(session_id="tui-e2e")
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.open_error = RuntimeError("no such session")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session missing")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.state.session_id == "tui-e2e"
        assert "no such session" in transcript_text(app)


# --- /attach --------------------------------------------------------------


def image_file(tmp_path, name: str = "shot.png"):
    path = tmp_path / name
    path.write_bytes(PNG_BYTES)
    return path


async def test_attach_holds_a_local_image_for_the_next_message(tmp_path) -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    path = image_file(tmp_path)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text(f"/attach {path}")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent == [], "attaching is local; nothing is sent yet"
        assert app.controller is not None
        assert [image.media_type for image in app.controller.attachments] == ["image/png"]
        assert f"Attached {path.name}" in transcript_text(app)
        assert "1 image" in composer.hint_text
        composer.load_text("look at this")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent[-1]["images"][0].media_type == "image/png"


async def test_a_relative_attach_is_resolved_against_the_workspace(tmp_path) -> None:
    backend = ScriptedBackend()
    image_file(tmp_path)
    app = app_for(backend, workspace=str(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/attach shot.png")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None
        assert len(app.controller.attachments) == 1


async def test_attaching_a_missing_file_attaches_nothing_and_says_so(tmp_path) -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text(f"/attach {tmp_path / 'nope.png'}")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.attachments == ()
        assert "No such file" in transcript_text(app)


async def test_attaching_a_non_image_attaches_nothing_and_says_so(tmp_path) -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text(f"/attach {notes}")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.attachments == ()
        assert "not an image" in transcript_text(app)


async def test_attach_clear_drops_what_was_pending(tmp_path) -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        for path in (image_file(tmp_path, "a.png"), image_file(tmp_path, "b.png")):
            composer.load_text(f"/attach {path}")
            await pilot.press("enter")
            await settle(pilot)
        assert app.controller is not None
        assert len(app.controller.attachments) == 2
        composer.load_text("/attach clear")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller.attachments == ()
        assert app.controller.composer_model().pending_images == 0
        assert "cleared" in transcript_text(app).lower()
        composer.load_text("plain text")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent[-1]["images"] == []


async def test_attach_without_arguments_explains_itself() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/attach")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.attachments == ()
        assert "/attach <path>" in transcript_text(app)


# --- /thread: reading a subagent thread ------------------------------------


async def test_thread_lists_what_the_session_holds_and_picks_one() -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    backend = ScriptedBackend(
        threads=(
            thread(),
            thread(
                thread_id="child-1",
                kind="subagent",
                agent="task",
                title="Research",
                message_count=4,
            ),
        )
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(thread_id="child-1")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent == [], "listing threads is local"
        assert isinstance(app.screen, SelectionScreen)
        assert "child-1" in selection_text(app), (
            "the picker shows the id the user would type"
        )
        await pilot.press("down", "enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.state.thread_id == "child-1"


def selection_text(app: TuiApp) -> str:
    """Everything the current screen is showing, for picker assertions."""
    parts: list[str] = []
    for widget in app.screen.query(Static):
        content = getattr(widget, "content", None)
        parts.append(str(getattr(content, "plain", "") or ""))
    return "\n".join(parts)


async def test_choosing_a_thread_shows_it_read_only() -> None:
    backend = ScriptedBackend(
        threads=(thread(), thread(thread_id="child-1", kind="subagent", agent="task"))
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(
            thread_id="child-1",
            history=[{"role": "user", "content": "subagent work"}],
        )
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread child-1")
        await pilot.press("enter")
        await settle(pilot)
        assert "subagent work" in transcript_text(app)
        assert app.controller is not None
        assert app.controller.composer_model().read_only is True
        assert "subagent:child-1" in status_text(app)
        before = list(backend.sent)
        composer.load_text("this must not be sent")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent == before, "a subagent thread is not user-driven"
        assert "read-only" in transcript_text(app).lower(), (
            "a refused message says why instead of vanishing"
        )


async def test_returning_to_the_main_thread_restores_the_composer() -> None:
    backend = ScriptedBackend(
        threads=(thread(), thread(thread_id="child-1", kind="subagent"))
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(thread_id="child-1")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread child-1")
        await pilot.press("enter")
        await settle(pilot)
        assert app.controller is not None and app.controller.composer_model().read_only
        backend.session = snapshot(history=[{"role": "user", "content": "main work"}])
        composer.load_text("/thread main")
        await pilot.press("enter")
        await settle(pilot)
        assert "main work" in transcript_text(app)
        assert backend.opened[-1]["thread_id"] == THREAD, (
            "/thread main asks the server for the session's own main thread"
        )
        assert app.controller.composer_model().read_only is False
        composer.load_text("hello again")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.sent[-1]["content"] == "hello again"


async def test_a_failed_thread_read_is_reported_and_changes_nothing() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.open_error = RuntimeError("no such thread")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread ghost")
        await pilot.press("enter")
        await settle(pilot)
        assert "no such thread" in transcript_text(app)
        assert app.controller is not None
        assert app.controller.state.thread_id == THREAD


# --- /thinking and /details: what the reader asked to see -----------------


def reasoning_turn(text: str = "deliberating") -> tuple:
    return (
        frames(
            ("assistant_message_delta", {"reasoning": text}),
            ("assistant_message", {"id": "a1", "content": "the answer"}),
        )[0],
        frames(
            ("assistant_message_delta", {"reasoning": text}),
            ("assistant_message", {"id": "a1", "content": "the answer"}),
        )[1],
    )


async def test_thinking_off_hides_reasoning_and_says_so() -> None:
    backend = ScriptedBackend(streams=[stream(*reasoning_turn(), hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert "deliberating" in transcript_text(app)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thinking off")
        await pilot.press("enter")
        await settle(pilot)
        assert "deliberating" not in transcript_text(app)
        assert "the answer" in transcript_text(app)
        assert "reasoning hidden" in transcript_text(app).lower()


async def test_thinking_toggles_back_on() -> None:
    backend = ScriptedBackend(streams=[stream(*reasoning_turn(), hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thinking off")
        await pilot.press("enter")
        await settle(pilot)
        composer.load_text("/thinking")
        await pilot.press("enter")
        await settle(pilot)
        assert "deliberating" in transcript_text(app)
        assert "reasoning shown" in transcript_text(app).lower()


async def test_details_off_hides_a_tool_payload() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    ("tool_calls_started", {"tool_calls": [{"id": "c1", "name": "bash", "args": {"cmd": "ls -la"}}]}),
                    ("tool_result", {"tool_call_id": "c1", "name": "bash", "status": "success", "content": "SECRET-OUTPUT"}),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert "SECRET-OUTPUT" in transcript_text(app)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/details off")
        await pilot.press("enter")
        await settle(pilot)
        assert "SECRET-OUTPUT" not in transcript_text(app)
        assert "bash" in transcript_text(app), "the call itself stays visible"
        assert "details hidden" in transcript_text(app).lower()


async def test_a_nonsense_block_argument_reports_usage_and_changes_nothing() -> None:
    backend = ScriptedBackend(streams=[stream(*reasoning_turn(), hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thinking maybe")
        await pilot.press("enter")
        await settle(pilot)
        assert "Usage: /thinking [on|off|toggle]" in transcript_text(app)
        assert "deliberating" in transcript_text(app), "nothing was hidden"


# --- the queued follow-ups panel ------------------------------------------


def queued(message_id: str, content: str) -> tuple[str, dict]:
    return (
        "queue_updated",
        {"items": [{"message_id": message_id, "content": content, "target": "next-turn"}]},
    )


async def test_a_queued_follow_up_is_visible_and_counted() -> None:
    backend = ScriptedBackend(streams=[stream(*frames(queued("q1", "after this")), hold=True)])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        panel = app.query_one("#queue", QueuePanel)
        assert panel.rows == 1
        assert "after this" in panel.row_text("q1")
        assert panel.display is True
        assert "queued:1" in status_text(app)


async def test_the_queue_panel_hides_when_the_queue_empties() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    queued("q1", "after this"),
                    ("queue_updated", {"items": []}),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        panel = app.query_one("#queue", QueuePanel)
        assert panel.rows == 0
        assert panel.display is False, "an empty panel must not take the screen"


async def test_the_jobs_panel_still_shows_while_the_queue_is_hidden() -> None:
    """Both panels share one container, so neither may hide the other."""
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    (
                        "job_updated",
                        {
                            "job_id": "j1",
                            "kind": "shell",
                            "status": "running",
                            "command": "pytest",
                            "cwd": "/w",
                            "created_at": 0,
                            "started_at": 1,
                            "finished_at": 0,
                        },
                    ),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert app.query_one("#jobs", JobPanel).display is True
        assert app.query_one("#queue", QueuePanel).display is False
        assert app.query_one("#panels").display is True


# --- a failed start must be readable, not a traceback ---------------------


async def test_a_failed_connect_reports_why_and_keeps_the_app_up() -> None:
    """A misconfigured server used to crash the app out of the alt screen.

    ``connect`` emits a visible error and *then* re-raises, so the app has to
    decide what to do with it: the user needs the reason on screen, in the client
    they are looking at, not a Python traceback they have to scroll.
    """
    backend = ScriptedBackend()
    backend.open_error = RuntimeError("Environment variable MINIMAX_API_TOKEN is not set")
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert isinstance(app.screen, Screen), "the app must still be running"
        assert "MINIMAX_API_TOKEN" in transcript_text(app)
        assert "Disconnected" in status_text(app)
        assert app.background_tasks == (), (
            "a client that could not attach must not start a reader it cannot serve"
        )
        composer = app.query_one("#composer", Composer)
        composer.load_text("/exit")
        await pilot.press("enter")
        await settle(pilot)
        assert app.is_running is False, "the user can still leave a client that failed to attach"


# --- where the pieces sit on the screen -----------------------------------


async def test_the_status_line_is_the_bottom_row() -> None:
    """It used to sit between the panels and the composer, not at the bottom.

    The line answers "what is happening right now"; the reader looks for it
    under everything else, where every other terminal client puts its footer.
    """
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        status = app.query_one("#status", StatusBar)
        composer = app.query_one("#composer", Composer)
        transcript = app.query_one("#transcript", TranscriptScroll)
        assert status.region.y > composer.region.y, "the status line goes under the input"
        assert status.region.y > transcript.region.y
        assert status.region.bottom == app.size.height, "and it is the last row"


async def test_the_completion_popup_sits_above_the_input() -> None:
    """A dropdown belongs next to the field it completes, not below it."""
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/")
        await settle(pilot)
        popup = app.query_one("#completion", CompletionPopup)
        assert popup.display is True
        assert popup.region.y < composer.region.y



# --- a verbose turn cannot take the screen --------------------------------


async def test_a_huge_tool_result_leaves_the_input_on_screen() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    ("tool_calls_started", {"tool_calls": [{"id": "c1", "name": "bash", "args": {}}]}),
                    (
                        "tool_result",
                        {
                            "tool_call_id": "c1",
                            "name": "bash",
                            "status": "success",
                            "content": "\n".join(f"row {index}" for index in range(2000)),
                        },
                    ),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        status = app.query_one("#status", StatusBar)
        assert composer.region.height > 0 and composer.region.y < 24
        assert status.region.bottom == app.size.height
        assert "row 1999" not in transcript_text(app), "the payload stays folded"


# --- server commands are discovered, never hardcoded ----------------------


async def test_the_server_catalogue_is_discovered_on_attach() -> None:
    backend = ScriptedBackend(
        commands=(
            command("status", description="Show the current session and thread status"),
            command("model", description="List or switch the model within a provider"),
        )
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert backend.command_reads, "the client asks the server what it offers"
        assert backend.command_reads[0] == (SESSION, THREAD)
        assert app.commands.get("status") is not None
        composer = app.query_one("#composer", Composer)
        composer.load_text("/help")
        await pilot.press("enter")
        await settle(pilot)
        assert "/status" in transcript_text(app)
        assert "/model" in transcript_text(app)


async def test_a_server_command_is_run_on_the_server_and_its_answer_shown() -> None:
    from XBotv2.tests.tui.factories import execution

    backend = ScriptedBackend(
        commands=(command("status"),),
        command_result=execution(message="session s1 · thread agent · idle"),
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/status verbose")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls[-1]["raw"] == "/status verbose"
        assert "session s1 · thread agent · idle" in transcript_text(app)
        assert backend.sent == [], "a server command is not a chat message"


async def test_a_failing_server_command_is_visible() -> None:
    from XBotv2.tests.tui.factories import execution

    backend = ScriptedBackend(
        commands=(command("undo"),),
        command_result=execution(
            command="undo", status="error", message="nothing to undo"
        ),
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/undo")
        await pilot.press("enter")
        await settle(pilot)
        assert "nothing to undo" in transcript_text(app)


async def test_a_command_call_that_raises_is_reported_not_swallowed() -> None:
    backend = ScriptedBackend(
        commands=(command("undo"),), command_error=RuntimeError("server is gone")
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/undo")
        await pilot.press("enter")
        await settle(pilot)
        assert "server is gone" in transcript_text(app)


async def test_a_catalogue_that_cannot_be_read_is_reported_and_harmless() -> None:
    backend = ScriptedBackend(commands_error=RuntimeError("no catalogue for you"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert "no catalogue for you" in transcript_text(app)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/help")
        await pilot.press("enter")
        await settle(pilot)
        assert "/clear-screen" in transcript_text(app), "client commands still work"


async def test_the_catalogue_is_refreshed_after_switching_sessions() -> None:
    backend = ScriptedBackend(commands=(command("status"),))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        assert len(backend.command_reads) == 1
        backend.session = snapshot(session_id="other")
        backend.commands = (command("status"), command("effort"))
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session other")
        await pilot.press("enter")
        await settle(pilot)
        assert len(backend.command_reads) >= 2, "a new thread may offer other commands"
        assert app.commands.get("effort") is not None


async def test_a_folded_block_expands_from_the_keyboard() -> None:
    """The summary row promises a key, so the key has to work from the composer.

    Focus sits in the input while the reader types; a binding that only worked
    once the block was focused would make the promise a lie.
    """
    from XBotv2.tui.view.blocks import ClampedBlock

    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    ("assistant_message_delta", {"content": "\n".join(f"line {i}" for i in range(40))}),
                    ("assistant_message", {"id": "a1", "content": "\n".join(f"line {i}" for i in range(40))}),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        block = app.query_one(ClampedBlock)
        assert block.collapsible is True and block.expanded is False
        assert "line 39" not in transcript_text(app)

        await pilot.press("ctrl+e")
        await settle(pilot)
        assert block.expanded is True
        assert "line 39" in transcript_text(app), "the folded content is now readable"

        await pilot.press("ctrl+e")
        await settle(pilot)
        assert block.expanded is False


# --- one chooser for every selection --------------------------------------


async def test_the_session_picker_goes_through_the_shared_chooser() -> None:
    backend = ScriptedBackend(session_catalog=[summary("one"), summary("two")])
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        backend.session = snapshot(session_id="two")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/session")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen), transcript_text(app)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert app.controller is not None
        assert app.controller.state.session_id == "two"


async def test_the_provider_picker_switches_the_provider() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/provider")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        assert "p1" in selection_text(app) and "p2" in selection_text(app)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert backend.selections[-1]["kind"] == "provider"
        assert backend.selections[-1]["name"] == "p2"
        assert "p2" in transcript_text(app), "the choice is confirmed on screen"


async def test_the_model_picker_switches_within_the_current_provider() -> None:
    # The picker lists the *current* provider's models, which the client learns
    # from the snapshot and the thread read.
    backend = ScriptedBackend(session=snapshot(provider="p1", model="m1"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/model")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert backend.selections[-1]["kind"] == "provider"
        assert backend.selections[-1]["name"] == "p1"
        assert backend.selections[-1]["model"] == "m2"


async def test_the_effort_picker_switches_the_tier() -> None:
    backend = ScriptedBackend(session=snapshot(provider="p1", model="m1"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/effort")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert backend.selections[-1]["kind"] == "effort"
        assert backend.selections[-1]["effort"] == "high"


async def test_the_agent_picker_switches_the_agent() -> None:
    backend = ScriptedBackend()
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/agent")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert backend.selections[-1]["kind"] == "agent"
        assert backend.selections[-1]["name"] == "reviewer"


async def test_arguments_still_go_to_the_server_command() -> None:
    """The picker owns the bare form; everything else is the server's."""
    backend = ScriptedBackend(commands=(command("model"),), command_result=execution(message="ok"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/model use m9")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls[-1]["raw"] == "/model use m9"
        assert backend.selections == [], "an argument form is not a picker"
        assert app.screen is not None and not isinstance(app.screen, SelectionScreen)


async def test_status_is_rendered_locally() -> None:
    """No round trip: the client has every input, and it says more."""
    backend = ScriptedBackend(
        commands=(command("status"),), command_result=execution(message="SERVER TEXT")
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/status")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls == [], "/status belongs to the client now"
        rendered = transcript_text(app)
        assert f"ID: {SESSION}" in rendered
        assert "Thread: agent" in rendered
        assert "SERVER TEXT" not in rendered


async def test_the_model_picker_lists_the_current_provider_only() -> None:
    backend = ScriptedBackend(session=snapshot(provider="p2", model="m3"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/model")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, SelectionScreen)
        assert "m3" in selection_text(app)
        assert "m2" not in selection_text(app), "another provider's models are not choices"


async def test_a_picker_that_cannot_list_its_rows_says_so() -> None:
    backend = ScriptedBackend(providers_error=RuntimeError("no catalogue"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/provider")
        await pilot.press("enter")
        await settle(pilot)
        assert not isinstance(app.screen, SelectionScreen)
        assert "no catalogue" in transcript_text(app)


async def test_a_refused_selection_is_reported() -> None:
    backend = ScriptedBackend(selection_error=RuntimeError("unknown provider: p2"))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/provider")
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("down", "enter")
        await settle(pilot)
        assert "unknown provider: p2" in transcript_text(app)


async def test_jobs_are_listed_locally() -> None:
    backend = ScriptedBackend(
        streams=[
            stream(
                *frames(
                    (
                        "job_updated",
                        {
                            "job_id": "j1",
                            "kind": "shell",
                            "status": "running",
                            "command": "pytest -q",
                            "cwd": "/w",
                            "created_at": 0,
                            "started_at": 1,
                            "finished_at": 0,
                        },
                    ),
                ),
                hold=True,
            )
        ]
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/jobs")
        await pilot.press("enter")
        await settle(pilot)
        rendered = transcript_text(app)
        assert "j1" in rendered and "pytest -q" in rendered
        assert backend.command_calls == [], "the list is already on the client"


async def test_jobs_arguments_go_to_the_server() -> None:
    backend = ScriptedBackend(
        commands=(command("jobs"),), command_result=execution(message="stopped j1")
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/jobs stop j1")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls[-1]["raw"] == "/jobs stop j1"
        assert "stopped j1" in transcript_text(app)


async def test_a_prompt_command_is_submitted_as_a_message() -> None:
    """``kind`` is the catalogue's word for who runs a line.

    ``prompt`` is not a command the server executes: it is a prompt template, and
    the endpoint that carries it is the message endpoint. The TUI used to post it
    to the command resource and show the server's refusal.
    """
    backend = ScriptedBackend(commands=(command("goal", kind="prompt"),))
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/goal ship the API")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls == [], "a prompt is not executed as a command"
        assert backend.sent[-1]["content"] == "/goal ship the API"


async def test_a_server_command_is_posted_as_one_line() -> None:
    from XBotv2.tests.tui.factories import execution

    backend = ScriptedBackend(
        commands=(command("status"),), command_result=execution(message="ok")
    )
    app = app_for(backend)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot)
        composer = app.query_one("#composer", Composer)
        composer.load_text("/status verbose")
        await pilot.press("enter")
        await settle(pilot)
        assert backend.command_calls[-1]["raw"] == "/status verbose"
        assert "kind" not in backend.command_calls[-1], (
            "the server resolves the line; the client does not label it"
        )
