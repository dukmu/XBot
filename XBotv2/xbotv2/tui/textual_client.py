"""Textual protocol TUI client.

This frontend is a standard JSONL protocol client. It talks to
``xbotv2 --mode server`` through ``TerminalSession`` and does not import the
runtime engine, bootstrap, LangChain, or LangGraph.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Header, Static, TextArea

from xbotv2.tui.client import (
    TuiMessage,
    TuiNotice,
    TuiState,
    TuiTool,
    TuiTranscriptEntry,
    _parse_permission_decision,
)
from xbotv2.tui.command import (
    CommandSpec,
    is_slash_command,
    known_command_labels,
    parse_slash_command,
)
from xbotv2.tui.mode import Mode
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.textual_state import queue_user_message, route_submitted_text
from xbotv2.tui.trace import trace_event


class TextualTuiClient:
    """Run the Textual UI over the HTTP/SSE transport (Phase E)."""

    def __init__(
        self,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
        session_id: str | None = None,
        thread_id: str = "agent",
        no_plugins: bool = False,
        base_url: str = "http://127.0.0.1:4096",
    ) -> None:
        self.app = XBotTextualApp(
            data_dir=data_dir,
            personality_id=personality_id,
            provider_name=provider_name,
            session_id=session_id,
            thread_id=thread_id,
            no_plugins=no_plugins,
            base_url=base_url,
        )

    async def run(self) -> None:
        await self.app.run_async()


class XBotTextualApp(App[None]):
    """OpenCode-style full-screen TUI backed by XBotv2 protocol frames."""

    CSS = """
    Screen {
        layout: vertical;
        background: #0f1115;
        color: #d6dae2;
    }

    #status_bar {
        height: 1;
        padding: 0 1;
        background: #171a21;
        color: #d6dae2;
    }

    #transcript {
        height: 1fr;
        padding: 1 2 0 2;
        background: #0f1115;
        scrollbar-color: #7aa2f7;
        scrollbar-color-hover: #9ece6a;
        scrollbar-background: #171a21;
    }

    .entry {
        width: 1fr;
        margin: 0 0 1 0;
    }

    .meta {
        height: 1;
        color: #8b95a7;
    }

    .body {
        color: #d6dae2;
        padding: 0 0 0 2;
    }

    .user .meta {
        color: #7dcfff;
    }

    .assistant .meta {
        color: #9ece6a;
    }

    .notice .meta {
        color: #bb9af7;
    }

    .tool .meta {
        color: #e0af68;
    }

    .activity .meta {
        color: #7aa2f7;
    }

    .choices {
        height: auto;
        padding: 0 0 0 2;
        color: #d6dae2;
    }

    .choices.resolved {
        color: #8b95a7;
    }

    #composer {
        dock: bottom;
        height: auto;
        padding: 0 1 1 1;
        background: #0f1115;
    }

    #composer_hint {
        height: 1;
        color: #8b95a7;
        padding: 0 1;
    }

    #input {
        height: 3;
        border: tall #2d3440;
        background: #171a21;
        color: #e5e7eb;
        padding: 0 1;
    }

    #input:focus {
        border: tall #7aa2f7;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "clear_input", "Clear input"),
    ]

    def __init__(
        self,
        *,
        data_dir: Path | str,
        personality_id: str,
        provider_name: str,
        session_id: str | None,
        thread_id: str,
        no_plugins: bool,
        base_url: str = "http://127.0.0.1:4096",
    ) -> None:
        super().__init__()
        self.session = TerminalSession(
            data_dir=data_dir,
            personality_id=personality_id,
            provider_name=provider_name,
            session_id=session_id,
            thread_id=thread_id,
            no_plugins=no_plugins,
            base_url=base_url,
        )
        self.state = TuiState(session_id=self.session.session_id, thread_id=self.session.thread_id)
        self._answers: asyncio.Queue[str] = asyncio.Queue()
        self._permission_decisions: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._outbound_messages: asyncio.Queue[str] = asyncio.Queue()
        self._connected = False
        self._turn_worker_running = False
        self._rendered_transcript_entries = 0
        self._render_lock = asyncio.Lock()
        self._activity_widgets: dict[int, Static] = {}
        self._tool_widgets: dict[str, Vertical] = {}
        self._choice_widgets: dict[str, Static] = {}
        self._choice_payloads: dict[str, list[InlineChoice]] = {}
        self._resolved_choice_keys: set[str] = set()
        self._active_choice_key: str | None = None
        self._active_choice_index = 0
        self._choice_results: dict[str, str] = {}
        self._choice_request_ids: dict[str, str] = {}
        self._submitted_interaction_ids: set[str] = set()
        self._interaction_response_pending = False
        self._turn_started_at: dict[int, float] = {}
        self._input_history: list[str] = []
        self._history_index: int | None = None
        self._spinner_index = 0
        self._activity_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status_bar", markup=False)
        yield TranscriptScroll(id="transcript")
        with Vertical(id="composer"):
            yield Static(id="composer_hint")
            yield ComposerTextArea(
                "",
                id="input",
                soft_wrap=True,
                show_line_numbers=False,
                compact=True,
                placeholder="Message XBotv2",
            )

    async def on_mount(self) -> None:
        self.query_one("#input", ComposerTextArea).focus()
        self._refresh_all()
        self._activity_timer = self.set_interval(0.5, self._tick_activity)
        self.run_worker(self._connect, exclusive=True, name="connect")

    async def on_unmount(self) -> None:
        if self._connected:
            await self.session.disconnect()

    async def _connect(self) -> None:
        try:
            self.state.status = "Connecting"
            self._refresh_all()
            await self.session.connect()
            self._connected = True
            self.state.status = "Ready"
            self._refresh_all()
        except Exception as exc:
            self._record_error(exc)

    async def submit_composer(self) -> None:
        composer = self.query_one("#input", ComposerTextArea)
        if self._choice_mode_active():
            return
        text = composer.text.strip()
        trace_event("tui.submit", {"text": text, "repr": repr(text)})
        composer.load_text("")
        self._history_index = None
        self._resize_composer()
        if not text:
            return
        if is_slash_command(text):
            await self._handle_slash_command(parse_slash_command(text))
            return
        route = route_submitted_text(
            self.state,
            self._answers,
            self._permission_decisions,
            text,
        )
        if route == "user_input":
            self._remember_input(text)
            self._interaction_response_pending = True
            self._resolve_active_choice(f"typed: {text}")
            await self._append_local_notice("Answer queued", text)
            return
        if route == "permission":
            parsed = _parse_permission_decision(text)
            self._interaction_response_pending = True
            self._resolve_active_choice(f"typed: {parsed['decision']} ({parsed['scope']})")
            await self._append_local_notice(
                "Approval queued",
                f"{parsed['decision']} ({parsed['scope']})",
            )
            return
        if not self._connected:
            await self._append_local_notice("Not connected", "Server is not ready yet.")
            return

        self._remember_input(text)
        queue_user_message(self.state, self._outbound_messages, text)
        self._refresh_all()
        if not self._turn_worker_running:
            self._turn_worker_running = True
            self.run_worker(self._drain_message_queue, exclusive=True, name="turn")

    def action_clear_input(self) -> None:
        """Clear the input box without changing protocol interaction state."""
        self.query_one("#input", ComposerTextArea).load_text("")
        self._history_index = None
        self._resize_composer()

    def _current_mode(self) -> Mode:
        """Derive the high-level Mode from existing TUI state predicates.

        Single source for mode classification; the rest of the app consults
        this method instead of re-running the same boolean ladder.
        """

        if self._choice_mode_active():
            return Mode.CHOOSING
        if self._interaction_response_pending:
            return Mode.SUBMITTED
        if self.state.status == "Error":
            return Mode.ERROR
        if self.state.turn_active:
            return Mode.RUNNING
        return Mode.COMPOSING

    async def _handle_slash_command(self, spec: CommandSpec | None) -> None:
        """Execute a parsed slash command.

        Slash commands are local TUI operations and never reach the server
        (per design doc §9.2).
        """

        if spec is None:
            return
        trace_event("tui.slash", {"name": spec.name, "raw": spec.raw})
        if spec.name == "exit":
            self.exit()
            return
        if spec.name == "clear":
            await self._cmd_clear()
            return
        if spec.name == "help":
            await self._cmd_help()
            return
        if spec.name == "status":
            await self._cmd_status()
            return
        if spec.name == "unknown":
            await self._append_local_notice("Unknown command", spec.display_label)

    async def _cmd_clear(self) -> None:
        """Reset the visible render log; session/thread/usage are untouched."""

        self.state.transcript.clear()
        self.state.messages.clear()
        self.state.tools.clear()
        self.state.notices.clear()
        self.state.errors.clear()
        self._rendered_transcript_entries = 0
        self._activity_widgets.clear()
        self._tool_widgets.clear()
        self._choice_widgets.clear()
        self._choice_payloads.clear()
        self._choice_results.clear()
        self._choice_request_ids.clear()
        self._resolved_choice_keys.clear()
        self._active_choice_key = None
        self._active_choice_index = 0
        self._submitted_interaction_ids.clear()
        await self._render_new_transcript_entries()
        self._refresh_all()

    async def _cmd_help(self) -> None:
        """Append a help block listing the registered slash commands."""

        await self._append_local_notice(
            "Help",
            "Slash commands: " + "  ".join(known_command_labels()),
        )

    async def _cmd_status(self) -> None:
        """Append a snapshot of the current TUI state to the stream."""

        usage = self.state.usage
        await self._append_local_notice(
            "Status",
            (
                f"mode={self._current_mode().value} "
                f"status={self.state.status} "
                f"turn={self.state.turn} "
                f"queued={self._outbound_messages.qsize()} "
                f"req={usage['requests']} "
                f"in={usage['input_tokens']} "
                f"out={usage['output_tokens']} "
                f"total={usage['total_tokens']}"
            ),
        )

    async def _drain_message_queue(self) -> None:
        try:
            while not self._outbound_messages.empty():
                text = await self._outbound_messages.get()
                self.state.append_message("user", text)
                await self._render_new_transcript_entries()
                await self._collect_response(text)
        finally:
            self._turn_worker_running = False
            if not self._outbound_messages.empty():
                self._turn_worker_running = True
                self.run_worker(self._drain_message_queue, exclusive=True, name="turn")

    async def _collect_response(self, text: str) -> None:
        try:
            async for event in self.session.send_message_with_input(
                text,
                input_provider=self._answer_live_input,
                permission_provider=self._answer_live_permission,
            ):
                self.state.apply_event(event)
                await self._handle_stream_event(event)
        except Exception as exc:
            self._record_error(exc)

    async def _answer_live_input(self, payload: dict[str, Any]) -> str:
        del payload
        self._set_input_placeholder("Answer the request, or choose an inline option")
        return await self._answers.get()

    async def _answer_live_permission(self, payload: dict[str, Any]) -> dict[str, str]:
        del payload
        self._set_input_placeholder("Choose an inline approval option, or type a decision")
        return await self._permission_decisions.get()

    def _record_error(self, exc: BaseException) -> None:
        self.state.status = "Error"
        self.state.errors.append(str(exc))
        self._refresh_all()

    async def _append_local_notice(self, kind: str, text: str) -> None:
        self.state.notices.append(TuiNotice(kind=kind, text=text))
        self.state.transcript.append(
            TuiTranscriptEntry(kind="notice", key=str(len(self.state.notices) - 1))
        )
        await self._render_new_transcript_entries()

    def _refresh_all(self) -> None:
        if not self.is_mounted:
            return
        self._refresh_status()
        self._refresh_input_mode()

    def _refresh_status(self) -> None:
        panel = self.query_one("#status_bar", Static)
        queue_depth = self._outbound_messages.qsize()
        usage = self.state.usage
        panel.update(
            _status_renderable(
                status=self.state.status,
                session_id=self.state.session_id,
                thread_id=self.state.thread_id,
                agent_name=self.state.agent_name,
                activity=self._activity_status(),
                queue_depth=queue_depth,
                usage=usage,
            )
        )

    async def _handle_stream_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "turn_started":
            await self._append_activity()
        elif event_type == "turn_finished":
            self._interaction_response_pending = False
            self._finalize_activity()
        elif event_type == "usage":
            self._update_activity()
        elif event_type == "tool_result":
            await self._refresh_tool_widget(str((event.get("data") or {}).get("tool_call_id") or ""))
        elif event_type in {
            "user_input_recorded",
            "permission_response_recorded",
            "permission_denied",
            "error",
        }:
            self._interaction_response_pending = False
        await self._render_new_transcript_entries()
        self._refresh_all()

    async def _render_new_transcript_entries(self) -> None:
        async with self._render_lock:
            stream = self.query_one("#transcript", VerticalScroll)
            start = self._rendered_transcript_entries
            entries = self.state.transcript[start:]
            self._rendered_transcript_entries = len(self.state.transcript)
            for entry in entries:
                widget = self._widget_for_entry(entry)
                if widget is not None:
                    await stream.mount(widget)
            stream.scroll_end(animate=False)

    def _refresh_input_mode(self) -> None:
        composer = self.query_one("#input", ComposerTextArea)
        hint = self.query_one("#composer_hint", Static)
        if self._choice_mode_active():
            composer.load_text("")
            composer.disabled = True
            composer.display = False
            hint.update("Use Up/Down to choose, Enter to confirm")
            if self.focused is composer:
                self.set_focus(None)
            return
        if self._interaction_response_pending:
            composer.load_text("")
            composer.disabled = True
            composer.display = False
            hint.update("Waiting for response")
            if self.focused is composer:
                self.set_focus(None)
            return
        composer.disabled = False
        composer.display = True
        if self.state.pending_user_input_active:
            hint.update("Answer required")
            self._set_input_placeholder("Type an answer")
        elif self.state.pending_permission_active:
            hint.update("Approval required")
            self._set_input_placeholder("Type allow/deny")
        else:
            hint.update("")
            self._set_input_placeholder("Message XBotv2")
        if self.focused is None:
            composer.focus()

    def _set_input_placeholder(self, text: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#input", ComposerTextArea).placeholder = text

    def select_previous_choice(self) -> bool:
        choices = self._active_choices()
        if not choices:
            return False
        self._active_choice_index = (self._active_choice_index - 1) % len(choices)
        self._refresh_active_choice_widget()
        return True

    def select_next_choice(self) -> bool:
        choices = self._active_choices()
        if not choices:
            return False
        self._active_choice_index = (self._active_choice_index + 1) % len(choices)
        self._refresh_active_choice_widget()
        return True

    async def confirm_active_choice(self) -> bool:
        choices = self._active_choices()
        key = self._active_choice_key
        if not choices or key is None:
            return False
        request_id = self._choice_request_ids.get(key, key)
        if request_id in self._submitted_interaction_ids:
            return False
        self._submitted_interaction_ids.add(request_id)
        choice = choices[self._active_choice_index]
        self._interaction_response_pending = True
        self._resolve_active_choice(choice.label)
        if choice.kind == "permission":
            self._permission_decisions.put_nowait(dict(choice.payload))
            await self._append_local_notice(
                "Approval queued",
                f"{choice.payload['decision']} ({choice.payload['scope']})",
            )
        else:
            self._answers.put_nowait(str(choice.payload["answer"]))
            await self._append_local_notice("Answer queued", choice.label)
        self._refresh_input_mode()
        return True

    def _resolve_active_choice(self, label: str) -> None:
        key = self._active_choice_key
        if key is None:
            return
        self._resolved_choice_keys.add(key)
        self._choice_results[key] = label
        self._active_choice_key = None
        self._refresh_choice_widget(key)
        self._refresh_input_mode()

    def _choice_mode_active(self) -> bool:
        return bool(self._active_choices())

    async def on_key(self, event: Key) -> None:
        if not self._choice_mode_active():
            return
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.select_previous_choice()
            return
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self.select_next_choice()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            await self.confirm_active_choice()
            return

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "input":
            self._resize_composer(event.text_area)

    def _resize_composer(self, composer: ComposerTextArea | TextArea | None = None) -> None:
        if not self.is_mounted:
            return
        if composer is None:
            try:
                composer = self.query_one("#input", ComposerTextArea)
            except Exception:
                return
        line_count = max(1, composer.text.count("\n") + 1)
        max_height = max(3, self.size.height - 8)
        composer.styles.height = min(max(3, line_count + 2), max_height)

    def history_previous(self) -> None:
        if not self._input_history:
            return
        composer = self.query_one("#input", ComposerTextArea)
        if composer.text and self._history_index is None:
            return
        if self._history_index is None:
            self._history_index = len(self._input_history) - 1
        else:
            self._history_index = max(0, self._history_index - 1)
        composer.load_text(self._input_history[self._history_index])
        self._resize_composer()

    def history_next(self) -> None:
        if self._history_index is None:
            return
        composer = self.query_one("#input", ComposerTextArea)
        self._history_index += 1
        if self._history_index >= len(self._input_history):
            self._history_index = None
            composer.load_text("")
        else:
            composer.load_text(self._input_history[self._history_index])
        self._resize_composer()

    def _remember_input(self, text: str) -> None:
        if not text:
            return
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)

    async def _append_activity(self) -> None:
        turn = self.state.turn
        self._turn_started_at[turn] = time.monotonic()
        activity = Static(self._activity_text(final=False), classes="entry activity")
        self._activity_widgets[turn] = activity
        stream = self.query_one("#transcript", VerticalScroll)
        await stream.mount(activity)
        stream.scroll_end(animate=False)

    def _tick_activity(self) -> None:
        if not self.is_mounted:
            return
        self._spinner_index += 1
        self._update_activity()
        self._refresh_status()

    def _update_activity(self) -> None:
        if not self.state.turn_active:
            return
        widget = self._activity_widgets.get(self.state.turn)
        if widget is not None:
            widget.update(self._activity_text(final=False))

    def _finalize_activity(self) -> None:
        widget = self._activity_widgets.get(self.state.turn)
        if widget is not None:
            widget.update(self._activity_text(final=True))

    def _activity_text(self, *, final: bool) -> str:
        elapsed = self._turn_elapsed()
        usage = self.state.turn_usage
        marker = "done" if final else _spinner(self._spinner_index)
        verb = "completed" if final else "working"
        return (
            f"{marker} turn {self.state.turn} {verb} "
            f"{elapsed:.1f}s  "
            f"tokens in:{usage['input_tokens']} out:{usage['output_tokens']} "
            f"total:{usage['total_tokens']}"
        )

    def _activity_status(self) -> str:
        if self.state.turn_active:
            return f"turn:{self.state.turn} {_spinner(self._spinner_index)} {self._turn_elapsed():.1f}s"
        return f"turn:{self.state.turn}"

    def _turn_elapsed(self) -> float:
        started = self._turn_started_at.get(self.state.turn)
        if started is None:
            return 0.0
        return max(0.0, time.monotonic() - started)

    def _widget_for_entry(self, entry: object) -> Vertical | Static | None:
        kind = str(getattr(entry, "kind", ""))
        key = str(getattr(entry, "key", ""))
        if kind == "message":
            try:
                message = self.state.messages[int(key)]
            except (ValueError, IndexError):
                return None
            return _message_widget(self.state, message)
        if kind == "tool":
            tool = self.state.tools.get(key)
            if tool is None:
                return None
            widget = _tool_widget(tool)
            self._tool_widgets[tool.tool_call_id] = widget
            return widget
        if kind == "notice":
            try:
                notice = self.state.notices[int(key)]
            except (ValueError, IndexError):
                return None
            return self._notice_widget(notice, key)
        if kind == "error":
            try:
                error = self.state.errors[int(key)]
            except (ValueError, IndexError):
                return None
            return _entry_widget("error", "Error", error)
        return None

    async def _refresh_tool_widget(self, tool_call_id: str) -> None:
        if not tool_call_id:
            return
        tool = self.state.tools.get(tool_call_id)
        widget = self._tool_widgets.get(tool_call_id)
        if tool is None or widget is None:
            return
        meta = widget.query_one(".meta", Static)
        meta.update(f"tool  {tool.name}  {tool.status}")
        detail = _tool_detail(tool)
        body = widget.query(".body").first()
        if body is not None:
            body.update(detail)
        elif detail:
            await widget.mount(Static(detail, classes="body"))

    def _notice_widget(self, notice: TuiNotice, key: str) -> Vertical:
        if notice.kind == "permission_request":
            choices = [
                InlineChoice("Allow once", "permission", {"decision": "allow", "scope": "once"}),
                InlineChoice("Deny", "permission", {"decision": "deny", "scope": "once"}),
                InlineChoice("Allow session", "permission", {"decision": "allow", "scope": "session"}),
                InlineChoice("Always allow", "permission", {"decision": "allow", "scope": "always"}),
            ]
            return self._request_widget(notice, key=key, title=f"{notice.ts}  approval request", choices=choices)
        if notice.kind == "user_input_required":
            options = notice.payload.get("options")
            choices = (
                [InlineChoice(str(option), "answer", {"answer": str(option)}) for option in options]
                if isinstance(options, list)
                else []
            )
            return self._request_widget(notice, key=key, title=f"{notice.ts}  question", choices=choices)
        return _entry_widget("notice", f"{notice.ts}  {_notice_title(notice.kind)}", notice.text)

    def _request_widget(
        self,
        notice: TuiNotice,
        *,
        key: str,
        title: str,
        choices: list["InlineChoice"],
    ) -> Vertical:
        children: list[Static] = [Static(title, classes="meta")]
        if notice.text:
            children.append(Static(notice.text, classes="body", markup=False))
        if choices:
            self._choice_payloads[key] = choices
            self._choice_request_ids[key] = str(notice.payload.get("request_id") or key)
            if self._active_choice_key is None and key not in self._resolved_choice_keys:
                self._active_choice_key = key
                self._active_choice_index = 0
            choice_widget = Static(
                self._choice_renderable(key),
                classes=self._choice_classes(key),
                markup=False,
            )
            self._choice_widgets[key] = choice_widget
            children.append(choice_widget)
            self.call_after_refresh(self._refresh_input_mode)
        return Vertical(*children, classes="entry notice")

    def _active_choices(self) -> list["InlineChoice"]:
        key = self._active_choice_key
        if key is None:
            return []
        if key in self._resolved_choice_keys:
            return []
        return self._choice_payloads.get(key, [])

    def _refresh_active_choice_widget(self) -> None:
        key = self._active_choice_key
        if key is not None:
            self._refresh_choice_widget(key)

    def _refresh_choice_widget(self, key: str) -> None:
        widget = self._choice_widgets.get(key)
        if widget is None:
            return
        widget.set_classes(self._choice_classes(key))
        widget.update(self._choice_renderable(key))

    def _choice_classes(self, key: str) -> str:
        classes = "choices"
        if key in self._resolved_choice_keys:
            classes += " resolved"
        return classes

    def _choice_renderable(self, key: str) -> Text:
        choices = self._choice_payloads.get(key, [])
        result = self._choice_results.get(key)
        text = Text()
        if result is not None:
            text.append(f"selected: {result}", style="dim")
            return text
        for index, choice in enumerate(choices):
            if index:
                text.append("   ")
            if key == self._active_choice_key and index == self._active_choice_index:
                text.append(f"> {choice.label}", style="reverse bold")
            else:
                text.append(f"  {choice.label}", style="dim")
        return text


def _status_badge(status: str) -> str:
    """Plain-text status label.

    The previous version returned a Rich markup string, but the status bar
    is rendered with ``markup=False`` so user-supplied segments can never
    leak as markup. Colors now come from the ``_status_renderable`` helper
    which uses ``rich.text.Text`` directly.
    """
    return status


_STATUS_BADGE_STYLE: dict[str, str] = {
    "Ready": "green",
    "Running": "yellow",
    "Connecting": "yellow",
    "Waiting for user": "cyan",
    "Approval required": "magenta",
    "Permission denied": "red",
    "Error": "red",
    "Shutdown": "dim",
}


def _status_renderable(
    *,
    status: str,
    session_id: str,
    thread_id: str,
    agent_name: str,
    activity: str,
    queue_depth: int,
    usage: dict[str, int],
) -> Text:
    """Build the status bar as a styled ``Text`` segment list.

    Returning ``Text`` lets us keep visual emphasis (bold name, colored
    status) without the markup parsing that ``markup=False`` disables.
    """

    style = _STATUS_BADGE_STYLE.get(status, "white")
    text = Text()
    text.append("XBotv2", style="bold")
    text.append("  ")
    text.append(status, style=style)
    text.append("  ")
    text.append(f"{session_id}/{thread_id}")
    text.append("  ")
    text.append(f"agent:{agent_name}")
    text.append("  ")
    text.append(activity)
    text.append("  ")
    text.append(f"queued:{queue_depth}")
    text.append("  ")
    text.append(
        f"usage req:{usage['requests']} "
        f"in:{usage['input_tokens']} "
        f"out:{usage['output_tokens']} "
        f"total:{usage['total_tokens']}"
    )
    return text


class ComposerTextArea(TextArea):
    """Multiline composer with Enter-submit and Shift+Enter-newline behavior."""

    async def _on_key(self, event: Key) -> None:
        app = self.app
        if isinstance(app, XBotTextualApp):
            if app._choice_mode_active():
                event.stop()
                event.prevent_default()
                return
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                await app.submit_composer()
                return
            if event.key == "shift+enter":
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            if event.key == "up" and (not self.text.strip() or app._history_index is not None):
                event.stop()
                event.prevent_default()
                app.history_previous()
                return
            if event.key == "down" and app._history_index is not None:
                event.stop()
                event.prevent_default()
                app.history_next()
                return
        await super()._on_key(event)


class TranscriptScroll(VerticalScroll):
    """Mouse-scrollable transcript that never participates in keyboard focus."""

    can_focus = False


@dataclass(frozen=True)
class InlineChoice:
    label: str
    kind: str
    payload: dict[str, str]


def _message_widget(state: TuiState, message: TuiMessage) -> Vertical:
    label = "You" if message.role == "user" else state.agent_name
    return _entry_widget(message.role, f"{message.ts}  {label}", message.content)


def _tool_widget(tool: TuiTool) -> Vertical:
    return _entry_widget("tool", f"tool  {tool.name}  {tool.status}", _tool_detail(tool))


def _tool_detail(tool: TuiTool) -> str:
    return "\n".join(
        part for part in (
            f"args: {tool.args_preview}" if tool.args_preview else "",
            f"result: {tool.summary}" if tool.summary else "",
        )
        if part
    )


def _entry_widget(kind: str, title: str, body: str) -> Vertical:
    children = [Static(title, classes="meta")]
    if body:
        children.append(Static(body, classes="body", markup=False))
    return Vertical(*children, classes=f"entry {kind}")


def _notice_title(kind: str) -> str:
    return {
        "client_message": "message",
        "permission_denied": "denied",
        "user_input_recorded": "answer",
        "permission_response_recorded": "approval",
        "Approval queued": "approval queued",
        "Answer queued": "answer queued",
        "Not connected": "not connected",
    }.get(kind, kind)


def _spinner(index: int) -> str:
    return "|/-\\"[index % 4]
