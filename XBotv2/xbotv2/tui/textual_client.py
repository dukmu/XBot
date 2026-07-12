"""Textual HTTP/SSE TUI client.

This frontend talks to ``xbotv2 --mode server`` through ``TerminalSession``
and does not import runtime engine or bootstrap modules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Header, Static, TextArea

from xbotv2.tui.client import (
    TuiNotice,
    TuiState,
    TuiTranscriptEntry,
    _parse_permission_decision,
    _repair_mojibake,
)
from xbotv2.tui.command import (
    CommandSpec,
    get_command,
    is_slash_command,
    known_command_labels,
    parse_slash_command,
    register_server_commands,
)
from xbotv2.tui.command_palette import CommandPalette
from xbotv2.tui.completion_popup import CompletionPopup
from xbotv2.tui.mode import Mode
from xbotv2.tui.session_config import TuiSessionConfig
from xbotv2.tui.textual_theme import TEXTUAL_TUI_CSS
from xbotv2.tui.textual_state import queue_user_message, route_submitted_text
from xbotv2.tui.trace import trace_event
from xbotv2.tui.textual_widgets import (
    ComposerTextArea,
    InlineChoice,
    TranscriptScroll,
    _build_title,
    entry_widget,
    message_widget,
    notice_title,
    render_reasoning,
    render_text,
    spinner,
    status_renderable,
    tool_detail,
    tool_widget,
)


logger = logging.getLogger("xbotv2.tui")


def _kind_tag(kind: str) -> str:
    _tags = {"client": "client cmd", "server": "server cmd", "skill": "skill", "tool": "tool", "mcp": "mcp"}
    return f"[{_tags.get(kind, kind)}]"


class TextualTuiClient:
    """Run the Textual UI over the HTTP/SSE transport (Phase E)."""

    def __init__(
        self,
        session_id: str | None = None,
        thread_id: str = "agent",
        workspace_root: Path | str | None = None,
        session_mode: str | None = None,
        base_url: str = "http://127.0.0.1:4096",
        uds_path: str | None = None,
    ) -> None:
        config = TuiSessionConfig(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=workspace_root,
            session_mode=session_mode,
            base_url=base_url,
            uds_path=uds_path,
        )
        self.app = XBotTextualApp(config=config)

    async def run(self) -> None:
        await self.app.run_async()


class XBotTextualApp(App[None]):
    """OpenCode-style full-screen TUI backed by XBotv2 protocol frames."""

    # Disable Textual's built-in command palette (default ctrl+p) so
    # our custom ``CommandPalette`` (slash-command only) owns the
    # binding. Per design doc §2.3.1: OpenCode's ``command_list`` is
    # also ctrl+p, but it is implemented in Solid and we are in
    # Python/Textual, so we use the latter's extensibility rather than
    # the former's runtime palette of every command.
    ENABLE_COMMAND_PALETTE = False

    CSS = TEXTUAL_TUI_CSS

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "clear_input", "Clear input"),
        ("ctrl+p", "open_palette", "Command palette"),
    ]

    def __init__(
        self,
        *,
        config: TuiSessionConfig | None = None,
        session_id: str | None = None,
        thread_id: str = "agent",
        workspace_root: Path | str | None = None,
        session_mode: str | None = None,
        base_url: str = "http://127.0.0.1:4096",
        uds_path: str | None = None,
    ) -> None:
        super().__init__()
        if config is None:
            config = TuiSessionConfig(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                session_mode=session_mode,
                base_url=base_url,
                uds_path=uds_path,
            )
        self.session = config.create_terminal_session()
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
        self._message_widgets: dict[int, Vertical] = {}
        self._choice_widgets: dict[str, Static] = {}
        self._choice_payloads: dict[str, list[InlineChoice]] = {}
        self._resolved_choice_keys: set[str] = set()
        self._active_choice_key: str | None = None
        self._active_choice_index = 0
        self._pending_stream_deltas = 0
        self._stream_timer: asyncio.Task | None = None
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
        yield CompletionPopup(id="completion_popup")
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
            try:
                payload = await self.session.list_commands()
                commands = payload.get("commands") if isinstance(payload, dict) else []
                if isinstance(commands, list):
                    register_server_commands(commands)
            except Exception:
                logger.exception("failed to load server commands")
            self._connected = True
            self.state.status = "Ready"
            self._refresh_all()
        except Exception as exc:
            self._record_error(exc)

    async def submit_composer(self) -> None:
        composer = self.query_one("#input", ComposerTextArea)
        if self._choice_mode_active():
            return
        text = _repair_mojibake(composer.text.strip())
        trace_event("tui.submit", {"text": text, "repr": repr(text)})
        composer.load_text("")
        composer.clear()
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
        """ESC handler: interrupt the running turn or clear the composer.

        Per OpenCode convention (design doc §2.3.1: ``session_interrupt
        = escape``): while a turn is in progress, ESC cancels it.
        Otherwise (composer is free), ESC clears the composer text
        like the old behaviour.
        """

        if self.state.turn_active or self._turn_worker_running:
            self.action_interrupt_turn()
            return
        self.query_one("#input", ComposerTextArea).load_text("")
        self._history_index = None
        self._resize_composer()

    def action_interrupt_turn(self) -> None:
        """Cancel the running turn via the HTTP /interrupt endpoint.

        Textual's action system does not auto-await coroutine
        actions. We schedule the actual HTTP round-trip on a
        worker. The worker is ``exclusive=False`` so the in-flight
        ``_drain_message_queue`` keeps running. We bind to a unique
        worker name so re-pressing ESC does not stack workers.
        """

        # Build a fresh coroutine each call so ESC spam doesn't
        # reuse a finished one.
        async def _do() -> None:
            try:
                result = await self.session.transport.interrupt(
                    session_id=self.session.session_id
                )
            except Exception:  # noqa: BLE001 — worker must not raise
                return
            if not self.is_mounted:
                return
            if result.get("cancelled"):
                self.state.status = "Interrupting..."
                self._refresh_status()
            elif self.state.turn_active:
                self.state.status = "Running"
                self._refresh_status()

        self.run_worker(
            _do(),
            exclusive=False,
            name="tui_interrupt",
            description="ESC: cancel running turn",
        )

    def action_open_palette(self) -> None:
        """Open the command palette modal (Ctrl+P)."""

        self.push_screen(CommandPalette())

    def _current_tui_mode(self) -> Mode:
        """Derive the high-level Mode from existing TUI state predicates.

        Single source for mode classification; the rest of the app consults
        this method instead of re-running the same boolean ladder.

        Renamed from ``_current_mode`` to avoid clashing with Textual's
        built-in ``App.current_mode`` (a string property used by its
        mode system, unrelated to ours).
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
        if spec is None:
            return
        trace_event("tui.slash", {"name": spec.name, "raw": spec.raw, "kind": spec.kind})
        if spec.name == "exit":
            self.exit()
            return
        if spec.name == "clear":
            await self._cmd_clear()
            return
        if spec.name == "help":
            await self._cmd_help(spec.args.strip() if spec.args else None)
            return
        if spec.name == "unknown":
            await self._append_local_notice("Unknown command", spec.display_label)
            return
        await self._run_server_command(spec)

    async def _run_server_command(self, spec: CommandSpec) -> None:
        if not self._connected:
            await self._append_local_notice("Not connected", "Server is not ready yet.")
            return
        if spec.kind not in ("server",):
            self.state.append_message("user", spec.raw)
            await self._render_new_transcript_entries()
            await self._collect_response(spec.raw)
            return
        parts = [part for part in spec.args.split() if part]
        try:
            result = await self.session.run_command(spec.name, parts, spec.raw, kind=spec.kind)
        except Exception as exc:
            self._record_error(exc)
            return
        data = result.get("data") if isinstance(result, dict) else {}
        message = str(data.get("message") or result)
        await self._append_local_notice(f"/{spec.name}", message)

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
        self._message_widgets.clear()
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

    async def _cmd_help(self, command_name: str | None = None) -> None:
        if command_name:
            spec = get_command(command_name.strip().lstrip("/"))
            if spec is None:
                await self._append_local_notice("Help", f"Unknown command: {command_name}")
                return
            lines = [
                f"{spec.name} [{_kind_tag(spec.kind)}] {spec.description}",
                "",
            ]
            if spec.parameters:
                lines.append("Parameters:")
                for param, desc in spec.parameters.items():
                    lines.append(f"  {param}: {desc}")
                lines.append("")
            if spec.raw:
                lines.append(f"Usage: {spec.raw} [args]")
            await self._append_local_notice("Help", "\n".join(lines))
            return
        body = "Slash commands:\n" + "\n".join(known_command_labels())
        await self._append_local_notice("Help", body)

    def _get_completion_popup(self):
        try:
            return self.query_one("#completion_popup", CompletionPopup)
        except Exception:
            return None

    def _accept_completion(self, spec) -> None:
        """Fill the composer with the highlighted slash command."""

        composer = self._safe_query_one("#input", ComposerTextArea)
        if composer is None:
            return
        composer.load_text(spec.raw)
        self._refresh_completion_popup(spec.raw)
        # Move caret to the end so the user can extend the command.
        composer.cursor_location = (0, len(spec.raw))

    def _dismiss_completion_popup(self) -> None:
        popup = self._get_completion_popup()
        if popup is not None:
            popup.update_for("")

    async def _cmd_status(self) -> None:
        """Append a snapshot of the current TUI state to the stream."""

        usage = self.state.usage
        await self._append_local_notice(
            "Status",
            (
                f"mode={self._current_tui_mode().value} "
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
                if not self.is_mounted:
                    return
                text = await self._outbound_messages.get()
                self.state.append_message("user", text)
                try:
                    await self._render_new_transcript_entries()
                    await self._collect_response(text)
                except Exception as exc:  # noqa: BLE001
                    # App may be tearing down (headless test exit);
                    # swallow so the worker can exit cleanly.
                    if not self.is_mounted:
                        return
                    self._record_error(exc)
        finally:
            self._turn_worker_running = False
            if self.is_mounted and not self._outbound_messages.empty():
                self._turn_worker_running = True
                self.run_worker(self._drain_message_queue, exclusive=True, name="turn")

    async def _collect_response(self, text: str) -> None:
        try:
            logger.info("tui.collect_response start session=%s chars=%d", self.state.session_id, len(text))
            async for event in self.session.send_message(
                text,
                input_provider=self._answer_live_input,
                permission_provider=self._answer_live_permission,
            ):
                logger.debug("tui.collect_response event type=%s", event.get("type"))
                self.state.apply_event(event)
                await self._handle_stream_event(event)
        except Exception as exc:
            logger.exception("tui.collect_response failed")
            self._record_error(exc)

    async def _answer_live_input(self, payload: dict[str, Any]) -> str:
        del payload
        self._set_input_placeholder("Answer the request, or choose an inline option")
        return await self._answers.get()

    async def _answer_live_permission(self, payload: dict[str, Any]) -> dict[str, str]:
        del payload
        self._set_input_placeholder("Choose an inline approval option, or type a decision")
        return await self._permission_decisions.get()

    def _safe_query_one(self, selector: str, expect_type: type | None = None) -> Any:
        """``query_one`` that returns ``None`` instead of raising when the
        widget is unmounting or not found.  All DOM lookups in
        tear-down-safe code should go through this method.
        """

        if not self.is_mounted:
            return None
        try:
            if expect_type is not None:
                return self.query_one(selector, expect_type)
            return self.query_one(selector)
        except Exception:  # noqa: BLE001 — NoMatches typically
            return None

    def _record_error(self, exc: BaseException) -> None:
        logger.error(
            "tui error recorded: %s",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        # If the app is being torn down, DOM lookups can fail. Don't
        # clobber a meaningful status (e.g. "Interrupted") with "Error"
        # just because the last UI refresh raced the teardown.
        if not self.is_mounted:
            return
        self.state.status = "Error"
        self.state.errors.append(str(exc))
        self.state.transcript.append(
            TuiTranscriptEntry(kind="error", key=str(len(self.state.errors) - 1))
        )
        self.run_worker(
            self._render_new_transcript_entries,
            exclusive=False,
            name="render_error",
        )
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
        if not self.is_mounted:
            return
        try:
            panel = self.query_one("#status_bar", Static)
        except Exception:  # noqa: BLE001 — defensive; widget may be unmounting
            return
        queue_depth = self._outbound_messages.qsize()
        usage = self.state.usage
        panel.update(
            status_renderable(
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
        refresh_input = False
        if event_type == "turn_started":
            self._finalize_activity()
            await self._append_activity()
        elif event_type == "turn_finished":
            await self._cancel_stream_timer()
            self._finalize_activity()
            refresh_input = True
        elif event_type == "turn_cancelled":
            self._interaction_response_pending = False
            await self._cancel_stream_timer()
            self._finalize_activity()
            self._refresh_status()
            refresh_input = True
        elif event_type == "usage":
            self._update_activity()
        elif event_type == "assistant_message_delta":
            if self._stream_timer is None:
                self._stream_timer = asyncio.create_task(self._stream_tick())
            return  # timer handles all rendering
        elif event_type == "assistant_message":
            await self._cancel_stream_timer()
            await self._refresh_streaming_assistant_widget()
        elif event_type == "tool_call_delta":
            await self._refresh_changed_tool_widgets()
        elif event_type == "tool_calls_started":
            await self._refresh_changed_tool_widgets()
        elif event_type == "tool_result":
            await self._refresh_changed_tool_widgets()
        elif event_type == "permission_request":
            await self._refresh_changed_tool_widgets()
            refresh_input = True
        elif event_type == "permission_denied":
            self._interaction_response_pending = False
            await self._refresh_changed_tool_widgets()
            refresh_input = True
        elif event_type == "permission_response_recorded":
            self._interaction_response_pending = False
            await self._refresh_changed_tool_widgets()
            refresh_input = True
        elif event_type in {
            "user_input_recorded", "error",
        }:
            self._interaction_response_pending = False
            refresh_input = True
        elif event_type in {"user_input_required"}:
            refresh_input = True
        await self._render_new_transcript_entries()
        self._refresh_status()
        if refresh_input:
            self._refresh_input_mode()

    async def _stream_tick(self) -> None:
        """Refresh streaming assistant widget at ~50ms intervals."""
        try:
            while True:
                await asyncio.sleep(0.05)
                await self._refresh_streaming_assistant_widget()
        except asyncio.CancelledError:
            pass

    async def _cancel_stream_timer(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.cancel()
            try:
                await self._stream_timer
            except asyncio.CancelledError:
                pass
            self._stream_timer = None

    async def _render_new_transcript_entries(self) -> bool:
        async with self._render_lock:
            stream = self.query_one("#transcript", VerticalScroll)
            start = self._rendered_transcript_entries
            entries = self.state.transcript[start:]
            if not entries:
                return False
            self._rendered_transcript_entries = len(self.state.transcript)
            for entry in entries:
                widget = self._widget_for_entry(entry)
                if widget is None:
                    continue
                # If this widget is already mounted in the DOM,
                # skip the mount.  Textual's parent attribute is
                # updated synchronously by ``mount()``, so a second
                # mount of the SAME widget object would raise
                # MountError.
                if widget.parent is stream:
                    continue
                # If the widget is mounted somewhere else (orphan),
                # detach it first.
                if widget.parent is not None:
                    try:
                        await widget.remove()
                    except Exception:  # noqa: BLE001
                        pass
                await stream.mount(widget)
            self.call_after_refresh(
                lambda: stream.scroll_end(animate=False)
            )
            return True

    def _refresh_input_mode(self) -> None:
        if not self.is_mounted:
            return
        try:
            composer = self.query_one("#input", ComposerTextArea)
            hint = self.query_one("#composer_hint", Static)
        except Exception:  # noqa: BLE001 — defensive; widgets unmounting
            return
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
        elif self.state.turn_active:
            # Turn is in progress; the composer remains visible and
            # messages get queued (see submit_composer). Surface the
            # queue depth so the user knows their input will be picked
            # up after the current turn ends.
            depth = self._outbound_messages.qsize()
            if depth > 0:
                hint.update(
                    f"Queueing: {depth} pending — type more or wait"
                )
            else:
                hint.update("Turn running — type to queue a follow-up")
            self._set_input_placeholder("Message XBotv2 (queue)")
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
            # Update the tool widget status in-place so the title
            # transitions from "pending approval" to the decision
            # immediately — no separate notice entry.
            for tool in list(self.state.tools.values()):
                if tool.permission_request_id == request_id:
                    decision = choice.payload.get("decision", "allow")
                    scope = choice.payload.get("scope", "once")
                    tool.permission_pending = False
                    tool.status = f"{decision} ({scope})"
                    self.state._changed_tool_ids.add(tool.tool_call_id)
                    await self._refresh_tool_widget(tool.tool_call_id)
                    break
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
            self._refresh_completion_popup(event.text_area.text)

    def _refresh_completion_popup(self, text: str) -> None:
        try:
            popup = self.query_one("#completion_popup", CompletionPopup)
        except Exception:
            return
        popup.update_for(text)

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
        stream = self._safe_query_one("#transcript", VerticalScroll)
        if stream is None:
            return
        await stream.mount(activity)
        stream.scroll_end(animate=False)

    def _tick_activity(self) -> None:
        if not self.is_mounted:
            return
        self._spinner_index += 1
        self._update_activity()
        # Tick the still-pending tool widgets so their "Ns…"
        # elapsed counter updates every 0.5s without waiting for
        # the next event. Helps the user answer "why is this tool
        # still pending" without watching the activity spinner.
        self._update_pending_tool_elapsed()
        self._refresh_status()

    def _update_pending_tool_elapsed(self) -> None:
        for tool_call_id, widget in list(self._tool_widgets.items()):
            tool = self.state.tools.get(tool_call_id)
            if tool is None or tool.finished_at > 0:
                continue
            elapsed = tool.elapsed(time.monotonic())
            try:
                meta = widget.query_one(".meta", Static)
            except Exception:
                continue
            meta.update(
                f"tool  {tool.name}  {tool.status}  {elapsed:.1f}s…"
            )

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
        marker = "done" if final else spinner(self._spinner_index)
        verb = "completed" if final else "working"
        return (
            f"{marker} turn {self.state.turn} {verb} "
            f"{elapsed:.1f}s  "
            f"tokens in:{usage['input_tokens']} out:{usage['output_tokens']} "
            f"total:{usage['total_tokens']}"
        )

    def _activity_status(self) -> str:
        if self.state.turn_active:
            return f"turn:{self.state.turn} {spinner(self._spinner_index)} {self._turn_elapsed():.1f}s"
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
            existing = self._message_widgets.get(int(key))
            if existing is not None:
                return existing
            widget = message_widget(self.state, message)
            self._message_widgets[int(key)] = widget
            return widget
        if kind == "tool":
            tool = self.state.tools.get(key)
            if tool is None:
                return None
            widget_id = tool.tool_call_id
            existing = self._tool_widgets.get(widget_id)
            if existing is not None:
                # Make sure the cached widget still reflects the
                # current tool state.  If the previous render used
                # a DIFFERENT tool object (e.g. after a resume that
                # rebuilt state.tools) the widget body is stale and
                # must be refreshed.
                try:
                    self._refresh_tool_widget_sync(widget_id)
                except Exception:  # noqa: BLE001
                    pass
                return existing
            widget = tool_widget(tool)
            self._tool_widgets[widget_id] = widget
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
            return entry_widget("error", "Error", error)
        return None

    def _refresh_tool_widget_sync(self, tool_call_id: str) -> None:
        """Synchronously refresh the cached tool widget in place.

        Used by ``_widget_for_entry`` to make sure a reused
        widget body matches the current tool state.  Only the
        title and body widgets are updated — choice widgets are
        not touched here; ``_sync_tool_permission_choices``
        (async) handles those.
        """
        tool = self.state.tools.get(tool_call_id)
        widget = self._tool_widgets.get(tool_call_id)
        if tool is None or widget is None:
            return
        elapsed = tool.elapsed(time.monotonic())
        title = _build_title(tool, elapsed)
        meta = self._query_child_first(widget, ".meta")
        if meta is not None:
            meta.update(title)
        detail = tool_detail(tool)
        body = self._query_child_first(widget, ".body")
        if body is not None:
            body.update(detail)
        elif detail:
            widget.mount(Static(render_text(detail), classes="body"))

    async def _refresh_changed_tool_widgets(self) -> None:
        for old_id, new_id in self.state._tool_id_renames.items():
            widget = self._tool_widgets.pop(old_id, None)
            if widget is not None:
                self._tool_widgets[new_id] = widget
        for tool_call_id in list(self.state._changed_tool_ids):
            await self._refresh_tool_widget(tool_call_id)

    async def _refresh_streaming_assistant_widget(self) -> None:
        index = self.state._streaming_assistant_index
        if index is None and self.state.messages:
            index = len(self.state.messages) - 1
        if index is None:
            return
        try:
            message = self.state.messages[index]
        except IndexError:
            return
        widget = self._message_widgets.get(index)
        if widget is None:
            await self._render_new_transcript_entries()
            widget = self._message_widgets.get(index)
        if widget is None:
            return
        await self._apply_streaming_message_widget(widget, message)
        stream = self._safe_query_one("#transcript", VerticalScroll)
        if stream is not None:
            stream.scroll_end(animate=False)

    async def _apply_streaming_message_widget(
        self, widget: Any, message: TuiMessage
    ) -> None:
        """Render reasoning + content of a streaming message into *widget*.

        Reasoning (when present) goes into a separate ``.reasoning``
        Static so the user can distinguish model thinking from the
        final reply. The body always reflects the visible content
        only — never a concatenation of reasoning + content.
        """
        reasoning = self._query_child_first(widget, ".reasoning")
        if message.reasoning:
            if reasoning is not None:
                reasoning.update(render_reasoning(message.reasoning))
            else:
                await widget.mount(
                    Static(render_reasoning(message.reasoning), classes="reasoning")
                )
        body = self._query_child_first(widget, ".body")
        if body is not None:
            body.update(render_text(message.content))
        elif message.content:
            await widget.mount(Static(render_text(message.content), classes="body"))

    async def _refresh_tool_widget(self, tool_call_id: str) -> None:
        if not tool_call_id:
            return
        tool = self.state.tools.get(tool_call_id)
        widget = self._tool_widgets.get(tool_call_id)
        if tool is None or widget is None:
            return
        elapsed = tool.elapsed(time.monotonic())
        title = _build_title(tool, elapsed)
        meta = self._query_child_first(widget, ".meta")
        if meta is None:
            return
        meta.update(title)
        detail = tool_detail(tool)
        body = self._query_child_first(widget, ".body")
        if body is not None:
            body.update(detail)
        elif detail:
            await widget.mount(Static(render_text(detail), classes="body"))
        # Permission choices are mounted / removed inside the tool
        # widget so the user can approve / deny a tool call inline
        # without a separate notice entry in the transcript.
        await self._sync_tool_permission_choices(widget, tool)

    async def _sync_tool_permission_choices(
        self, widget: Vertical, tool: TuiTool
    ) -> None:
        key = tool.tool_call_id
        # Remove any existing choice widget from this tool entry
        for child in list(widget.children):
            if isinstance(child, Static) and "choice" in (child.classes or ""):
                await child.remove()
        for ck in list(self._choice_payloads.keys()):
            if ck == key:
                self._choice_payloads.pop(ck, None)
                self._choice_request_ids.pop(ck, None)
                self._choice_widgets.pop(ck, None)
                self._resolved_choice_keys.discard(ck)
                if self._active_choice_key == ck:
                    self._active_choice_key = None

        if not tool.permission_pending or not tool.permission_request_id:
            return

        choices = [
            InlineChoice("Allow once", "permission", {"decision": "allow", "scope": "once"}),
            InlineChoice("Deny", "permission", {"decision": "deny", "scope": "once"}),
            InlineChoice("Allow session", "permission", {"decision": "allow", "scope": "session"}),
        ]
        self._choice_payloads[key] = choices
        self._choice_request_ids[key] = tool.permission_request_id
        if self._active_choice_key is None and key not in self._resolved_choice_keys:
            self._active_choice_key = key
            self._active_choice_index = 0
        choice_widget = Static(
            self._choice_renderable(key),
            classes=self._choice_classes(key),
            markup=False,
        )
        self._choice_widgets[key] = choice_widget
        await widget.mount(choice_widget)
        self.call_after_refresh(self._refresh_input_mode)

    def _query_child_first(self, widget: Any, selector: str) -> Any | None:
        try:
            return widget.query(selector).first()
        except Exception:  # noqa: BLE001 — child may not exist until later chunks
            return None

    def _notice_widget(self, notice: TuiNotice, key: str) -> Vertical:
        if notice.kind == "permission_request":
            choices = [
                InlineChoice("Allow once", "permission", {"decision": "allow", "scope": "once"}),
                InlineChoice("Deny", "permission", {"decision": "deny", "scope": "once"}),
                InlineChoice("Allow session", "permission", {"decision": "allow", "scope": "session"}),
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
        return entry_widget("notice", f"{notice.ts}  {notice_title(notice.kind)}", notice.text)

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
