"""Textual HTTP/SSE client over ``TerminalSession``."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import shlex
import time
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Collapsible, Static, TextArea

from XBotv2.tui.client import (
    TuiNotice,
    TuiState,
    TuiTranscriptEntry,
    _effective_context_tokens,
    _parse_permission_decision,
)
from XBotv2.tui.command import (
    CommandRegistry,
    CommandSpec,
)
from XBotv2.tui.command_palette import CommandPalette
from XBotv2.tui.completion_popup import CompletionPopup
from XBotv2.client import XBotClientError
from XBotv2.tui.selection import SelectionScreen
from XBotv2.tui.session_config import TuiSessionConfig
from XBotv2.tui.textual_theme import TEXTUAL_TUI_CSS
from XBotv2.tui.trace import trace_event
from XBotv2.tui.textual_widgets import (
    ComposerTextArea,
    InlineChoice,
    ThreadView,
    TranscriptScroll,
    TaskListWidget,
    _build_title,
    _markdown_plain_text,
    entry_widget,
    message_widget,
    compact_widget,
    notice_title,
    queue_renderable,
    render_message,
    render_reasoning,
    render_text,
    reasoning_widget,
    spinner,
    status_renderable,
    tool_detail,
    tool_detail_widget,
    tool_widget,
)

# Status bar refresh throttle: streaming deltas can arrive many times per
# second; rebuilding the status renderable on every delta is wasteful.
_STATUS_REFRESH_INTERVAL = 0.2
# Replay window: number of most-recent transcript entries mounted on startup
# after resuming a session. Older entries are lazy-loaded on scroll.
_REPLAY_WINDOW = 50
# Replay lazy-load batch size: entries mounted per scroll-to-top batch.
_REPLAY_BATCH = 50
# The transcript is windowed: at most this many entry widgets stay mounted.
# Older/newer entries are re-mounted from ``state.transcript`` as the user
# scrolls, so a long conversation never grows the DOM unboundedly.
_MAX_MOUNTED_ENTRIES = _REPLAY_WINDOW + _REPLAY_BATCH
# A disconnected event stream is resumed from TerminalSession's event cursor.
# Keep retries short and bounded so a dead server still eventually releases an
# active turn instead of leaving the TUI waiting forever.
_SESSION_EVENT_RECONNECT_DELAYS = (0.05, 0.1, 0.25)
# A cursor the server has already evicted is recoverable: resubscribe from the
# oldest frame it still holds instead of failing the turn.
_SESSION_EVENT_CURSOR_RECOVERIES = 3
# When even the oldest retained frame is behind, the session snapshot is the
# recovery point: re-open the session for a fresh cursor, history, and pending
# interactions. Bounded, because a rebuild resets the rewind budget above.
_SESSION_EVENT_BASELINE_REBUILDS = 2
# Raw SSE frames are kept only while the Textual consumer catches up. Once
# this bounded hand-off fills, backpressure reaches the cursor-based server
# subscription; an overrun is then reported as explicit cursor expiry rather
# than becoming an unbounded client-side backlog.
_SESSION_EVENT_PENDING_CAPACITY = 512


logger = logging.getLogger("xbotv2.tui")


def _kind_tag(kind: str) -> str:
    _tags = {"client": "client cmd", "server": "server cmd", "skill": "skill", "tool": "tool", "mcp": "mcp"}
    return f"[{_tags.get(kind, kind)}]"


# Widget cache caps: long sessions grow the transcript unboundedly; bound the
# number of cached (and thus mounted) message/tool widgets so DOM and layout
# work stay flat instead of growing with history length.
_MAX_MESSAGE_WIDGETS = 200
_MAX_TOOL_WIDGETS = 100


class TextualTuiClient:
    """Run the Textual UI over the HTTP/SSE transport (Phase E)."""

    def __init__(
        self,
        session_id: str | None = None,
        thread_id: str = "agent",
        agent: str | None = None,
        workspace_root: Path | str | None = None,
        session_mode: str | None = None,
        base_url: str = "http://127.0.0.1:4096",
        uds_path: str | None = None,
    ) -> None:
        config = TuiSessionConfig(
            session_id=session_id,
            thread_id=thread_id,
            agent=agent,
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

    TITLE = "XBotv2"
    # Disable Textual's built-in command palette (default ctrl+p) so
    # our custom ``CommandPalette`` (slash-command only) owns the
    # binding. Per design doc §2.3.1: OpenCode's ``command_list`` is
    # also ctrl+p, but it is implemented in Solid and we are in
    # Python/Textual, so we use the latter's extensibility rather than
    # the former's runtime palette of every command.
    ENABLE_COMMAND_PALETTE = False

    CSS = TEXTUAL_TUI_CSS

    BINDINGS = [
        ("ctrl+c", "cancel_or_quit", "Clear input or quit"),
        ("alt+c", "copy_last", "Copy last reply"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "clear_input", "Clear input"),
        ("ctrl+p", "open_palette", "Command palette"),
        ("ctrl+t", "toggle_thread_view", "View session threads"),
    ]

    def __init__(
        self,
        *,
        config: TuiSessionConfig | None = None,
        session_id: str | None = None,
        thread_id: str = "agent",
        agent: str | None = None,
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
                agent=agent,
                workspace_root=workspace_root,
                session_mode=session_mode,
                base_url=base_url,
                uds_path=uds_path,
            )
        self.session = config.create_terminal_session()
        self.commands = CommandRegistry.default()
        self.state = TuiState(session_id=self.session.session_id, thread_id=self.session.thread_id)
        self._answers: asyncio.Queue[str] = asyncio.Queue()
        self._permission_decisions: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._server_reachable = False
        self._session_attached = False
        self._event_stream_connected = False
        self._request_sequence = 0
        self._pending_messages: dict[int, str] = {}
        # Windowed transcript: ``state.transcript[_window_start:_window_end]``
        # is what is mounted in the DOM (bounded by ``_MAX_MOUNTED_ENTRIES``).
        # ``_mounted_entry_widgets`` runs in parallel to that slice so the
        # DOM can be trimmed from either end without touching activity widgets.
        self._window_start = 0
        self._window_end = 0
        self._mounted_entry_widgets: list[Any] = []
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
        self._tool_refresh_timer: asyncio.Task | None = None
        self._deferred_tool_ids: set[str] = set()
        self._last_status_refresh = 0.0
        self._status_refresh_pending = False
        self._replay_loading = False
        # Recovery starts the live reader before the optional trajectory
        # lookup, so events emitted during that lookup remain queued.
        self._restore_compactions_when_reading = False
        self._choice_results: dict[str, str] = {}
        self._choice_request_ids: dict[str, str] = {}
        self._interaction_response_pending = False
        self._interaction_response_task: asyncio.Task[None] | None = None
        # A failed compatibility stream and the authoritative event stream can
        # report the same broken turn independently.  Keep the local terminal
        # transition idempotent so one transport failure does not produce two
        # error entries.
        self._stream_failure_reported = False
        self._turn_started_at: dict[int, float] = {}
        self._input_history: list[str] = []
        self._history_index: int | None = None
        self._spinner_index = 0
        self._activity_timer = None
        self._session_events_worker = None
        self._reasoning_expanded = False
        self._tool_details_expanded = False
        self._pending_images: list[tuple[str, dict[str, str]]] = []
        self._transcript_follow: bool = False
        # Read-only thread view: the user can look into a subagent thread while
        # the attached (main) thread keeps streaming.
        self._view_active = False
        self._view_thread_id = ""
        self._view_task: asyncio.Task | None = None
        self._view_main_busy = False
        self._view_rolling: list[str] = ["", ""]
        self._view_catalog: list[dict[str, str]] = []
        self._view_older_cursor: str | None = None
        self._view_loading_older = False

    def compose(self) -> ComposeResult:
        yield TranscriptScroll(id="transcript")
        yield ThreadView(id="thread_view")
        yield CompletionPopup(id="completion_popup", registry=self.commands)
        with Horizontal(id="runtime_panels"):
            yield Collapsible(
                TaskListWidget(id="task_list"),
                title="Tasks",
                collapsed=False,
                id="task_panel",
            )
            yield Collapsible(
                Static(id="queue_list"),
                title="Queue",
                collapsed=False,
                id="queue_panel",
            )
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
        yield Static(id="status_bar", markup=False)

    async def on_mount(self) -> None:
        self.query_one("#input", ComposerTextArea).focus()
        self._refresh_all()
        self._activity_timer = self.set_interval(0.5, self._tick_activity)
        self.run_worker(self._connect, exclusive=True, name="connect")

    @on(ThreadView.OlderRequested)
    def _on_view_older_requested(self) -> None:
        self.run_worker(self._load_older_thread_history(), exclusive=False)

    @on(TranscriptScroll.ReplayTopReached)
    def _handle_replay_top(self) -> None:
        """Lazy-load earlier replayed history when scrolled to the top."""
        if self._window_start <= 0 or self._replay_loading:
            return
        self.run_worker(self._load_earlier_replay, exclusive=False)

    @on(TranscriptScroll.ReplayBottomReached)
    def _handle_replay_bottom(self) -> None:
        """Re-mount the newest entries dropped while the user scrolled up."""
        if self._replay_loading or self._window_end >= len(self.state.transcript):
            return
        self.run_worker(self._load_newer_replay, exclusive=False)

    @on(TranscriptScroll.Scrolled)
    def _handle_transcript_scrolled(self, event: TranscriptScroll.Scrolled) -> None:
        """Track whether the user is following the live tail."""
        self._transcript_follow = event.at_end

    @on(TranscriptScroll.HeightChanged)
    def _handle_transcript_resized(self) -> None:
        """Re-pin a follower to the tail when the viewport height changes.

        Layout changes (the slash-completion popup, the runtime panels, a
        taller composer) shrink the transcript viewport; the current scroll
        offset then points above the new bottom and ``is_vertical_scroll_end``
        goes stale, silently breaking auto-scroll until the next render. If
        the user was following, scroll back to the end after the reflow.
        """
        if not self._transcript_follow:
            return
        stream = self._safe_query_one("#transcript", TranscriptScroll)
        if stream is not None:
            self.call_after_refresh(lambda s=stream: s.scroll_end(animate=False))

    async def on_unmount(self) -> None:
        self._cancel_interaction_response()
        await self._cancel_tool_refresh_timer()
        # Both pumps are raw tasks owned by this screen's lifecycle: leaving
        # them alive keeps a 20 Hz render loop and a thread-view SSE
        # subscription running against an unmounted DOM.
        await self._cancel_stream_timer()
        await self._exit_thread_view()
        self._deferred_tool_ids.clear()
        self._stop_session_events()
        if self._server_reachable:
            await self.session.disconnect()

    @on(Collapsible.Toggled)
    def _restore_composer_focus(self, event: Collapsible.Toggled) -> None:
        if not event.collapsible.display:
            return
        if event.collapsible.has_class("reasoning-block"):
            self._reasoning_expanded = not event.collapsible.collapsed
        composer = self._safe_query_one("#input", ComposerTextArea)
        if composer is not None and not composer.disabled:
            focused = self.focused

            def restore_focus() -> None:
                if self.focused is focused or self.focused is None:
                    composer.focus()

            self.call_after_refresh(restore_focus)

    async def _connect(self) -> None:
        try:
            self.state.status = "Connecting"
            self._refresh_all()
            session = await self.session.connect()
            self._server_reachable = True
            await self._apply_open_session(session)
            self._session_attached = True
            try:
                payload = await self.session.list_commands()
                commands = payload.get("commands") if isinstance(payload, dict) else []
                if isinstance(commands, list):
                    self.commands.merge_server(commands)
            except Exception:
                logger.exception("failed to load server commands")
            self.state.status = "Ready"
            self._refresh_all()
            self._start_session_events()
        except Exception as exc:
            self._record_error(exc)

    async def _refresh_session_identity(self) -> None:
        """Re-read the session descriptor's identity fields at turn end.

        The descriptor is the single source of the session title: a caption
        written during the turn becomes visible here without a client-only
        event. Only identity fields are applied; transcript/queue state is
        owned by the event stream.
        """
        try:
            session = await self.session.refresh_descriptor()
        except Exception:
            logger.exception("failed to refresh session descriptor")
            return
        if not isinstance(session, dict):
            return
        title = str(session.get("title") or "")
        agent_name = str(session.get("agent_name") or "")
        provider = str(session.get("provider") or "")
        model = str(session.get("model") or "")
        model_mode = str(session.get("model_mode") or "")
        context_window = int(session.get("context_window") or 0)
        changed = (
            title != self.state.session_title
            or agent_name != self.state.agent_name
            or provider != self.state.provider
            or model != self.state.model
            or model_mode != self.state.model_mode
            or context_window != self.state.context_window
        )
        if not changed:
            return
        self.state.session_title = title or self.state.session_title
        self.state.agent_name = agent_name or self.state.agent_name
        self.state.provider = provider or self.state.provider
        self.state.model = model or self.state.model
        self.state.model_mode = model_mode or self.state.model_mode
        if context_window:
            self.state.context_window = context_window
        self._refresh_all()

    async def _apply_open_session(
        self,
        session: dict[str, JsonValue] | None,
        *,
        restore_compactions: bool = True,
    ) -> None:
        if isinstance(session, dict):
            self.state.session_id = str(session.get("session_id") or self.state.session_id)
            self.state.session_title = str(session.get("title") or self.state.session_title)
            self.state.thread_id = str(session.get("thread_id") or self.state.thread_id)
            self.state.agent_name = str(session.get("agent_name") or self.state.agent_name)
            self.state.workspace_root = str(session.get("workspace_root") or "")
            self.state.provider = str(session.get("provider") or "")
            self.state.model = str(session.get("model") or "")
            self.state.model_mode = str(session.get("model_mode") or "")
            slots = session.get("status_slots")
            if isinstance(slots, dict):
                self.state.status_slots = {str(name): str(value) for name, value in slots.items()}
            self.state.context_window = int(session.get("context_window") or 0)
            usage = session.get("usage")
            if isinstance(usage, dict):
                for key in self.state.usage:
                    self.state.usage[key] = int(usage.get(key) or 0)
                self.state.context_input_tokens = _effective_context_tokens(usage)
            history = session.get("history")
            if isinstance(history, list):
                self.state.restore_history(history)
                if restore_compactions:
                    await self._restore_compaction_entries()
                await self._render_replay_window()
            if self._restore_pending_interactions(session):
                await self._render_new_transcript_entries()
                self._refresh_status()

    async def _restore_compaction_entries(self) -> None:
        """Rebuild expandable compaction entries a resumed session had.

        Compactions live in the event stream, so a reloaded client would
        otherwise lose them; the durable summary comes from the trajectory
        replay. The entries are prepended in front of the surviving messages:
        each one folds an earlier prefix that is no longer in the surface.
        """
        try:
            compactions = await self.session.read_thread_compactions(
                self.state.thread_id,
            )
        except Exception:  # noqa: BLE001 — a read-only replay must not break resume
            logger.warning("thread compaction replay failed", exc_info=True)
            return
        for item in reversed(compactions):
            summary = str(item.get("summary") or "")
            if not summary:
                continue
            self.state.notices.append(TuiNotice(
                kind="compact",
                text="Conversation compacted",
                payload={"summary": summary},
            ))
            self.state.transcript.insert(
                0,
                TuiTranscriptEntry(
                    kind="notice",
                    key=str(len(self.state.notices) - 1),
                ),
            )

    def _restore_pending_interactions(self, session: dict[str, JsonValue]) -> bool:
        """Rebuild unanswered dialogs from an open/resume snapshot.

        A live approval or question exists only in the event stream, so a
        reloaded or reconnected client would otherwise never see the dialog
        again and the turn would look stuck.
        """
        pending = session.get("pending_interactions")
        if not isinstance(pending, list):
            return False
        restored = False
        for item in pending:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("type") or "")
            data = item.get("data")
            if not event_type or not isinstance(data, dict):
                continue
            self.state.apply_event({"type": event_type, "data": data})
            restored = True
        return restored

    def _start_session_events(self) -> None:
        if not hasattr(self.session, "session_events"):
            return
        self._stop_session_events()
        self._event_stream_connected = True
        self._session_events_worker = self.run_worker(
            self._collect_session_events,
            exclusive=False,
            name="session_events",
        )

    def _stop_session_events(self) -> None:
        worker = self._session_events_worker
        self._session_events_worker = None
        self._event_stream_connected = False
        if worker is not None and not getattr(worker, "is_finished", False):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()

    async def submit_composer(self) -> None:
        composer = self.query_one("#input", ComposerTextArea)
        if self._choice_mode_active():
            return
        text = composer.text.strip()
        trace_event("tui.submit", {"text": text, "repr": repr(text)})
        composer.load_text("")
        composer.clear()
        self._history_index = None
        self._resize_composer()
        self._refresh_all()
        if self._view_active:
            # There is no client-facing channel into a subagent thread; the
            # view is read-only and the composer is disabled.
            if text:
                await self._append_local_notice(
                    "thread",
                    "Thread views are read-only; press Esc to return to the main thread.",
                )
            return
        if not text and not self._pending_images:
            return
        if text and self.commands.is_slash(text):
            spec = self.commands.parse(text)
            if spec is not None and spec.kind == "server":
                self.state.append_message("user", text)
                await self._render_new_transcript_entries()
            await self._handle_slash_command(spec)
            return
        if self.state.pending_user_input_payload is not None:
            self._answers.put_nowait(text)
            self._remember_input(text)
            self._interaction_response_pending = True
            self._resolve_active_choice(f"typed: {text}")
            return
        if self.state.pending_permission_payload is not None:
            parsed = _parse_permission_decision(text)
            self._permission_decisions.put_nowait(parsed)
            self._interaction_response_pending = True
            self._resolve_active_choice(f"typed: {parsed['decision']} ({parsed['scope']})")
            return
        if not self._session_attached:
            await self._append_local_notice("Not connected", "Server is not ready yet.")
            return

        self._remember_input(text)
        attachments = self._pending_images
        self._pending_images = []
        images = [payload for _name, payload in attachments]
        append_on_start = self.state.turn_active or bool(self._pending_messages)
        visible_text = text or "[image]"
        display_text = self._attachment_label(
            visible_text,
            [name for name, _payload in attachments],
        )
        # The transcript is event-driven: the server emits a ``message``
        # event (carrying the server-side id and content) when the input is
        # accepted, and that event appends the text. Queued inputs stay in
        # ``_pending_messages`` (queue panel) until their ``message`` event.
        self._request_sequence += 1
        sequence = self._request_sequence
        if append_on_start:
            self._pending_messages[sequence] = display_text
        self._refresh_all()
        self.run_worker(
            self._collect_queued_message(text, images),
            exclusive=False,
            name=f"turn-{sequence}",
        )

    def action_clear_input(self) -> None:
        """Return to the composer, or interrupt a turn / clear the input."""

        if self._view_active:
            self.run_worker(self._exit_thread_view(), exclusive=True)
            return
        composer = self._safe_query_one("#input", ComposerTextArea)
        if composer is not None and not composer.has_focus and composer.display:
            composer.focus()
            return
        if self.state.turn_active or self._pending_messages:
            self.action_interrupt_turn()
            return
        self.query_one("#input", ComposerTextArea).load_text("")
        self._pending_images.clear()
        self._history_index = None
        self._resize_composer()

    def action_cancel_or_quit(self) -> None:
        """Clear a non-empty composer; quit when there is nothing to clear."""
        composer = self.query_one("#input", ComposerTextArea)
        if composer.text or self._pending_images:
            composer.load_text("")
            self._pending_images.clear()
            self._history_index = None
            self._resize_composer()
            self._refresh_all()
            return
        self.exit()

    def action_interrupt_turn(self) -> None:
        """Schedule one non-exclusive interrupt request."""

        self.run_worker(
            self._interrupt_turn(),
            exclusive=False,
            name="tui_interrupt",
            description="ESC: cancel running turn",
        )

    async def _interrupt_turn(self) -> None:
        try:
            result = await self.session.interrupt()
        except Exception:  # noqa: BLE001 — worker must not raise
            return
        if not self.is_mounted:
            return
        if result.get("cancelled"):
            self.state.status = "Interrupting..."
        elif self.state.turn_active:
            self.state.status = "Running"
        else:
            return
        self._refresh_status()

    def action_copy_last(self) -> None:
        """Copy the latest assistant reply as plain text."""
        assistant = [m for m in self.state.messages if m.role == "assistant"]
        if not assistant:
            self._copy_feedback(0)
            return
        text = _markdown_plain_text(assistant[-1].content, width=self.size.width)
        self.app.copy_to_clipboard(text)
        self._copy_feedback(len(text))

    def _copy_feedback(self, chars: int) -> None:
        if not self.is_mounted:
            return
        if chars <= 0:
            self.state.status = "Nothing to copy"
            self._refresh_status()
            return
        self.state.status = f"Copied {chars} chars"
        self._refresh_status()

    def action_open_palette(self) -> None:
        """Open the command palette modal (Ctrl+P)."""

        self.push_screen(CommandPalette(registry=self.commands))

    # --- read-only thread view ----------------------------------------
    async def action_toggle_thread_view(self) -> None:
        """Toggle between the main thread and the previously viewed thread."""
        if self._view_active:
            await self._exit_thread_view()
            return
        threads = await self._thread_catalog()
        if not threads:
            await self._append_local_notice("thread", "No threads available")
            return
        candidate = next(
            (t for t in threads if t["thread_id"] != self.state.thread_id),
            threads[0],
        )
        await self._enter_thread_view(candidate["thread_id"], candidate.get("title") or "")

    async def _pick_provider(self, on_choice) -> None:
        """Choose a provider from the server catalog instead of typing its id."""
        try:
            payload = await self.session.list_providers()
        except Exception as exc:
            self._record_error(exc)
            return
        providers = payload.get("providers") if isinstance(payload, dict) else []
        options = []
        if isinstance(providers, list):
            for item in providers:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                provider = str(item.get("provider") or "")
                model = str(item.get("default_model") or "")
                if not name:
                    continue
                label = name
                if provider and provider != name:
                    label += f" ({provider})"
                if model:
                    label += f"  {model}"
                options.append((name, label))
        if not options:
            await self._append_local_notice("Providers", "No providers available")
            return
        self.push_screen(SelectionScreen("Providers", options), callback=on_choice)

    async def _thread_catalog(self) -> list[dict[str, str]]:
        try:
            payload = await self.session.list_threads(self.state.session_id)
        except Exception as exc:
            self._record_error(exc)
            return []
        rows = payload.get("threads") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return []
        catalog: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            catalog.append({
                "thread_id": str(row.get("thread_id") or ""),
                "kind": str(row.get("kind") or "subagent"),
                "status": str(row.get("status") or ""),
                "message_count": str(row.get("message_count") or ""),
                "title": str(row.get("title") or ""),
            })
        self._view_catalog = [row for row in catalog if row["thread_id"]]
        return self._view_catalog

    async def _cmd_thread(self, args: str) -> None:
        """List session threads or switch the view to one of them."""
        if not self._server_reachable:
            await self._append_local_notice("/thread", "Not connected")
            return
        value = args.strip()
        if value in {"main", "agent", self.state.thread_id}:
            # Returning to the main (attached) thread is the one command that
            # always means "stop viewing".
            if self._view_active:
                await self._exit_thread_view()
                await self._append_local_notice("/thread", "Back on the main thread")
            return
        if self._view_active:
            await self._exit_thread_view()
        if not value:
            threads = await self._thread_catalog()
            if not threads:
                await self._append_local_notice("Threads", "No persisted threads")
                return
            self._pick_thread(threads)
            return
        if value == "main":
            return
        await self._enter_thread_view(value, "")

    def _pick_thread(self, threads: list[dict[str, str]]) -> None:
        options = []
        for thread in threads:
            label = f"{thread['thread_id']}  {thread['kind']} {thread['status']}"
            if thread["message_count"]:
                label += f"  {thread['message_count']} msg"
            if thread["title"]:
                label += f"  {thread['title']}"
            options.append((thread["thread_id"], label))
        if len(options) == 1:
            self.run_worker(self._enter_thread_view(options[0][0], options[0][1]))
            return
        self.push_screen(SelectionScreen("Threads", options), callback=lambda choice: (
            self.run_worker(self._enter_thread_view(choice, "")) if choice else None
        ))

    async def _enter_thread_view(self, thread_id: str, title: str) -> None:
        if self._view_active:
            await self._exit_thread_view()
        self._view_active = True
        self._view_thread_id = thread_id
        self._view_main_busy = False
        self._view_rolling = ["", ""]
        view = self._safe_query_one("#thread_view", ThreadView)
        transcript = self._safe_query_one("#transcript")
        if view is None or transcript is None:
            self._view_active = False
            return
        history, older_cursor = await self.session.read_thread_history(thread_id)
        self._view_older_cursor = older_cursor
        summary = title or self._thread_summary(thread_id)
        view.show(thread_id, summary)
        lines = [
            self._history_line(item)
            for item in history
            if isinstance(item, dict)
        ]
        view.body.update("\n".join(line for line in lines if line) + ("\n" if lines else ""))
        view.display = True
        transcript.display = False

        self._view_task = asyncio.create_task(self._pump_thread_view(thread_id))
        self._refresh_input_mode()
        self._refresh_status()

    def _thread_summary(self, thread_id: str) -> str:
        for thread in self._view_catalog:
            if thread["thread_id"] == thread_id:
                return f"{thread['kind']} {thread['status']}"
        return "subagent"

    async def _exit_thread_view(self) -> None:
        if not self._view_active:
            return
        self._view_active = False
        if self._view_task is not None:
            self._view_task.cancel()
            self._view_task = None
        view = self._safe_query_one("#thread_view", ThreadView)
        transcript = self._safe_query_one("#transcript")
        if view is not None:
            view.display = False
        if transcript is not None:
            transcript.display = True
        self._view_thread_id = ""
        self._view_main_busy = False
        self._view_older_cursor = None
        self._view_loading_older = False
        self._refresh_input_mode()
        self._refresh_status()

    async def _pump_thread_view(self, thread_id: str) -> None:
        try:
            async for event in self.session.stream_thread_events(thread_id):
                if not self._view_active or self._view_thread_id != thread_id:
                    return
                line = self._view_event_line(event)
                if line:
                    view = self._safe_query_one("#thread_view", ThreadView)
                    if view is not None:
                        view.body.append(line + "\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a read-only view must not kill the app
            if self._view_active:
                await self._append_local_notice("thread", f"thread view error: {exc}")

    def _history_line(self, item: dict[str, JsonValue]) -> str:
        role = str(item.get("message", item).get("role") or "")
        content = str(item.get("message", item).get("content") or "")
        if role == "assistant":
            reasoning = str(item.get("message", item).get("reasoning") or "")
            return f"[assistant] {content}" + (
                f"\n  > {reasoning}" if reasoning else ""
            )
        if role == "user":
            return f"[user] {content}"
        if role == "tool":
            return f"[tool] {content}"
        kind = str(item.get("kind") or "")
        if kind == "surface_replace":
            summary = str(item.get("summary") or "")
            if summary:
                return f"[compact] {summary}"
        return f"[{kind}]" if kind else ""

    def _view_event_line(self, event: dict[str, JsonValue]) -> str | None:
        """One frame of a viewed thread as a display line, merging deltas."""
        type_ = str(event.get("type") or "")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if type_ == "assistant_message_delta":
            self._view_rolling[0] += str(data.get("reasoning") or "")
            self._view_rolling[1] += str(data.get("content") or "")
            return None
        flush = ""
        if self._view_rolling[0] or self._view_rolling[1]:
            if self._view_rolling[0]:
                flush += f"  > {self._view_rolling[0]}"
            if self._view_rolling[1]:
                flush = (flush + "\n" if flush else "") + f"\u25b8 {self._view_rolling[1]}"
            self._view_rolling = ["", ""]
        own = ""
        if type_ == "assistant_message":
            own = f"[assistant] {data.get('content') or ''}"
        elif type_ == "tool_calls_started":
            own = "\n".join(
                f"[tool] {str(call.get('name') or 'tool')}"
                for call in data.get("tool_calls") or []
            )
        elif type_ == "tool_result":
            content = str(data.get("content") or "")
            own = f"[tool] {data.get('name') or 'tool'} {data.get('status') or ''}:\n{content.strip()}"
        elif type_ == "message":
            own = f"[user] {data.get('content') or ''}"
        elif type_ == "turn_started":
            own = f"— turn {data.get('turn') or ''} started"
        elif type_ == "turn_finished":
            own = f"— turn {data.get('turn') or ''} finished"
        elif type_ == "usage":
            return flush or None
        elif type_ == "end":
            return (flush + "\n" if flush else "") + "— stream ended"
        combined = (flush + "\n" if flush else "") + own
        return combined if combined.strip() else None

    async def _load_older_thread_history(self) -> None:
        """Scroll-up at the top pulls the previous page of the viewed thread."""
        if (
            not self._view_active
            or self._view_loading_older
            or not self._view_older_cursor
        ):
            return
        thread_id = self._view_thread_id
        self._view_loading_older = True
        try:
            records, older_cursor = await self.session.read_thread_history(
                thread_id,
                cursor=self._view_older_cursor,
            )
        except Exception as exc:  # noqa: BLE001 — a read-only pane must not crash
            self._view_older_cursor = None
            await self._append_local_notice("thread", f"older history failed: {exc}")
            return
        finally:
            self._view_loading_older = False
        if not self._view_active or self._view_thread_id != thread_id:
            return
        self._view_older_cursor = older_cursor
        lines = [
            self._history_line(item)
            for item in records
            if isinstance(item, dict)
        ]
        text = "\n".join(line for line in lines if line)
        if text:
            view = self._safe_query_one("#thread_view", ThreadView)
            if view is not None:
                view.body.prepend(text + "\n")

    # Command execution table: name -> bound handler.  Only TUI-local commands
    # and interactive overrides are registered here; every server command from
    # the catalog is executed by the single default remote forwarder, so the
    # dispatcher never branches per command name.
    _CLIENT_HANDLERS = {
        "exit": "_cmd_exit",
        "clear-screen": "_cmd_clear_entry",
        "copy": "_cmd_copy",
        "help": "_cmd_help_entry",
        "thinking": "_cmd_thinking_toggle",
        "details": "_cmd_details_toggle",
        "attach": "_cmd_attach_entry",
        "session": "_cmd_session_entry",
        "resume": "_cmd_resume",
        "new": "_cmd_new",
        "thread": "_cmd_thread_entry",
        "provider": "_cmd_provider",
    }

    async def _handle_slash_command(self, spec: CommandSpec | None) -> None:
        if spec is None:
            return
        trace_event("tui.slash", {"name": spec.name, "raw": spec.raw, "kind": spec.kind})
        handler = self._command_handler(spec.name)
        if handler is None:
            if spec.name == "unknown" or spec.kind == "unknown":
                await self._append_local_notice(
                    "Unknown command", spec.display_label
                )
                return
            handler = self._dispatch_remote_command
        await handler(spec)

    def _command_handler(self, name: str):
        """Resolve one command to its bound handler, or None for the default."""
        method = self._CLIENT_HANDLERS.get(name)
        if method is None:
            return None
        return getattr(self, method, None)

    async def _cmd_exit(self, _spec: CommandSpec) -> None:
        self.exit()

    async def _cmd_copy(self, _spec: CommandSpec) -> None:
        self.action_copy_last()

    async def _cmd_clear_entry(self, _spec: CommandSpec) -> None:
        await self._cmd_clear()

    async def _cmd_help_entry(self, spec: CommandSpec) -> None:
        await self._cmd_help(spec.args.strip() if spec.args else None)

    async def _cmd_attach_entry(self, spec: CommandSpec) -> None:
        await self._cmd_attach(spec.args)

    async def _cmd_session_entry(self, spec: CommandSpec) -> None:
        await self._cmd_session(spec.args)

    async def _cmd_thread_entry(self, spec: CommandSpec) -> None:
        await self._cmd_thread(spec.args)

    async def _cmd_resume(self, spec: CommandSpec) -> None:
        await self._cmd_session(spec.args.strip() or self.state.session_id)

    async def _cmd_new(self, spec: CommandSpec) -> None:
        await self._cmd_session(f"new {spec.args.strip()}".strip())

    async def _cmd_thinking_toggle(self, spec: CommandSpec) -> None:
        await self._cmd_toggle_blocks("thinking", spec.args)

    async def _cmd_details_toggle(self, spec: CommandSpec) -> None:
        await self._cmd_toggle_blocks("details", spec.args)

    async def _cmd_provider(self, spec: CommandSpec) -> None:
        """Provider: interactive picker on no args; the server owns the rest.

        ``/provider`` (no args) opens the picker this client requires;
        ``status``/``list``/``use`` keep the server's single definition and
        rendering instead of a client-side reimplementation.
        """
        provider_args = spec.args.strip()
        if not provider_args:
            await self._pick_provider(lambda choice: (
                self.run_worker(
                    self._dispatch_remote_command(
                        CommandSpec(
                            name=spec.name,
                            kind=spec.kind,
                            description=spec.description,
                            usage=spec.usage,
                            raw=f"/{spec.name} use {choice}",
                            args=f"use {choice}",
                        )
                    )
                )
                if choice
                else None
            ))
            return
        await self._dispatch_remote_command(spec)

    async def _cmd_exit_spec(self, spec: CommandSpec) -> None:
        self.exit()

    async def _cmd_session(self, args: str) -> None:
        """List or switch sessions using the same persisted session API as WebUI."""
        if not self._server_reachable:
            await self._append_local_notice("/session", "Not connected")
            return
        try:
            values = shlex.split(args)
        except ValueError as exc:
            await self._append_local_notice("/session", str(exc))
            return
        if not values or (
            len(values) == 1 and values[0].lower() in {"list", "ls"}
        ):
            try:
                payload = await self.session.list_sessions()
            except Exception as exc:
                self._record_error(exc)
                return
            sessions = payload.get("sessions") if isinstance(payload, dict) else []
            if not sessions:
                await self._append_local_notice("Sessions", "No persisted sessions")
                return
            options = []
            for item in sessions:
                sid = str(item.get("session_id") or "")
                title = str(item.get("title") or "")
                workspace = str(item.get("workspace_root") or "")
                label = f"{sid}"
                # The title falls back to the session id: show it only once.
                if title and title != sid:
                    label += f"  {title}"
                if workspace:
                    label += f"  {workspace}"
                options.append((sid, label))
            self.push_screen(SelectionScreen("Sessions", options), callback=lambda choice: (
                self.run_worker(self._cmd_session(choice)) if choice else None
            ))
            return
        if values[0].lower() == "new":
            if len(values) > 2:
                await self._append_local_notice(
                    "/session", "Usage: /session [<session-id> [workspace] | new [workspace]]"
                )
                return
            workspace = values[1] if len(values) > 1 else self.state.workspace_root or None
            mode = "new"
            session_id = None
        else:
            if len(values) > 2:
                await self._append_local_notice(
                    "/session", "Usage: /session [<session-id> [workspace] | new [workspace]]"
                )
                return
            session_id = values[0]
            try:
                threads_payload = await self.session.list_threads(session_id)
            except Exception as exc:
                self._record_error(exc)
                return
            threads = threads_payload.get("threads") if isinstance(threads_payload, dict) else []
            main = next((item for item in threads if item.get("kind") == "main"), None)
            if main is None and threads:
                main = threads[0]
            if main is None:
                await self._append_local_notice("/session", f"Session has no resumable threads: {session_id}")
                return
            thread_id = str(main.get("thread_id") or "agent")
            workspace = values[1] if len(values) > 1 else str(main.get("workspace_root") or "") or None
            mode = "resume"
        if self.state.turn_active or self._pending_messages:
            await self._append_local_notice("/session", "Finish or interrupt the active turn before switching")
            return
        try:
            self._stop_session_events()
            opened = await self.session.switch(
                session_id=session_id,
                thread_id=thread_id if session_id else "agent",
                workspace_root=workspace,
                mode=mode,
            )
            self._cancel_interaction_response()
            self._pending_images.clear()
            self._history_index = None
            await self._cmd_clear()
            self.state = TuiState(
                session_id=self.session.session_id,
                thread_id=self.session.thread_id,
            )
            await self._apply_open_session(opened)
            try:
                payload = await self.session.list_commands()
                commands = payload.get("commands") if isinstance(payload, dict) else []
                if isinstance(commands, list):
                    self.commands.merge_server(commands)
            except Exception:
                logger.exception("failed to load server commands after session switch")
            self._session_attached = True
            self.state.status = "Ready"
            self._refresh_all()
            self._start_session_events()
            await self._append_local_notice(
                "/session",
                f"Switched to {self.state.session_id} ({self.state.workspace_root or 'workspace unavailable'})",
            )
        except Exception as exc:
            self._start_session_events()
            self._record_error(exc)

    async def _dispatch_remote_command(self, spec: CommandSpec) -> None:
        if not self._session_attached:
            await self._append_local_notice("Not connected", "Server is not ready yet.")
            return
        if spec.kind == "prompt":
            self.state.append_message("user", spec.raw)
            await self._render_new_transcript_entries()
            await self._collect_response(spec.raw)
            return
        try:
            parts = shlex.split(spec.args)
        except ValueError as exc:
            await self._append_local_notice(f"/{spec.name}", str(exc))
            return
        try:
            result = await self.session.run_command(
                spec.name, parts, spec.raw, kind=spec.kind
            )
            data = result.get("data") if isinstance(result, dict) else {}
            message = str(data.get("message") or result)
        except ValueError as exc:
            await self._append_local_notice(f"/{spec.name}", str(exc))
            return
        except Exception as exc:
            self._record_error(exc)
            return
        await self._append_local_notice(f"/{spec.name}", message)

    async def _cmd_clear(self) -> None:
        """Reset the visible render log; session/thread/usage are untouched."""

        await self._clear_rendered_transcript()
        self.state.reset_history()
        await self._render_new_transcript_entries()
        self._refresh_all()

    async def _clear_rendered_transcript(self) -> None:
        """Remove mounted transcript widgets without mutating protocol state."""
        await self._cancel_tool_refresh_timer()
        self._deferred_tool_ids.clear()
        stream = self._safe_query_one("#transcript", VerticalScroll)
        if stream is not None:
            await stream.remove_children()
        self._window_start = 0
        self._window_end = 0
        self._mounted_entry_widgets.clear()
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

    async def _cmd_attach(self, args: str) -> None:
        try:
            values = shlex.split(args)
        except ValueError as exc:
            await self._append_local_notice("/attach", str(exc))
            return
        if values == ["clear"]:
            self._pending_images.clear()
            self._refresh_all()
            return
        if len(values) != 1:
            await self._append_local_notice(
                "/attach", "Usage: /attach <path> | /attach clear"
            )
            return
        path = Path(values[0]).expanduser()
        if not path.is_absolute():
            path = Path(self.state.workspace_root or Path.cwd()) / path
        path = path.resolve()
        media_type = mimetypes.guess_type(path.name)[0] or ""
        if not path.is_file():
            await self._append_local_notice("/attach", f"File not found: {path}")
            return
        if media_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
            await self._append_local_notice(
                "/attach", f"Unsupported image type: {media_type or path.suffix or 'unknown'}"
            )
            return
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        self._pending_images.append((path.name, {"data": payload, "media_type": media_type}))
        self._refresh_all()

    @staticmethod
    def _attachment_label(text: str, names: list[str]) -> str:
        if not names:
            return text
        return f"{text}\n\nAttachments: {', '.join(names)}"

    async def _cmd_help(self, command_name: str | None = None) -> None:
        if command_name:
            spec = self.commands.get(command_name.strip().lstrip("/"))
            if spec is None:
                await self._append_local_notice("Help", f"Unknown command: {command_name}")
                return
            lines = [
                f"{spec.name} [{_kind_tag(spec.kind)}] {spec.description}",
                "",
            ]
            if spec.parameters:
                lines.append("Parameters:")
                parameters = spec.parameters
                if parameters.get("type") == "object":
                    parameters = parameters.get("properties", {})
                for param, details in parameters.items():
                    description = (
                        details.get("description", details.get("type", ""))
                        if isinstance(details, dict)
                        else details
                    )
                    lines.append(f"  {param}: {description}")
                lines.append("")
            if spec.usage or spec.raw:
                lines.append(f"Usage: {spec.usage or spec.raw}")
            await self._append_local_notice("Help", "\n".join(lines))
            return
        body = "Slash commands:\n" + "\n".join(self.commands.labels())
        await self._append_local_notice("Help", body)

    async def _cmd_toggle_blocks(self, kind: str, argument: str) -> None:
        value = argument.strip().lower() or "toggle"
        if value not in {"on", "off", "toggle"}:
            await self._append_local_notice(
                f"/{kind}", f"Usage: /{kind} [on|off|toggle]"
            )
            return
        if kind == "thinking":
            current = self._reasoning_expanded
            selector = ".reasoning-block"
        else:
            current = self._tool_details_expanded
            selector = ".tool-details"
        expanded = not current if value == "toggle" else value == "on"
        if kind == "thinking":
            self._reasoning_expanded = expanded
        else:
            self._tool_details_expanded = expanded
        for block in self.query(selector):
            if isinstance(block, Collapsible):
                block.collapsed = not expanded
        await self._append_local_notice(
            f"/{kind}", "Expanded" if expanded else "Collapsed"
        )

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

    async def _collect_queued_message(
        self,
        text: str,
        images: list[dict[str, str]],
    ) -> None:
        await self._collect_response(text, images=images)
        self._refresh_all()

    def _pop_pending_message(self, content: str) -> None:
        """Drop the first locally queued input matching ``content``."""
        for sequence, text in list(self._pending_messages.items()):
            if text == content:
                self._pending_messages.pop(sequence, None)
                return

    def _recover_expired_cursor(self, exc: BaseException) -> bool:
        """Resubscribe from the oldest frame the server still retains."""
        if not isinstance(exc, XBotClientError):
            return False
        if exc.code != "session_event_cursor_expired":
            return False
        oldest = int(exc.details.get("oldest_sequence") or 0)
        rewind = max(0, oldest - 1)
        self.session.rewind_event_cursor(rewind)
        logger.warning("session event cursor expired; replaying from %s", rewind)
        return True

    async def _rebuild_session_baseline(self, exc: BaseException) -> bool:
        """Re-open the session for a fresh cursor after an eviction.

        The replay window moving past this client's cursor says nothing about
        the session's health, so the Web client rebuilds its baseline instead of
        reporting an error. Do the same: adopt the snapshot (cursor, history,
        unanswered interactions) and resubscribe from it.
        """
        if not isinstance(exc, XBotClientError):
            return False
        if exc.code != "session_event_cursor_expired":
            return False
        refresh = getattr(self.session, "refresh_baseline", None)
        if not callable(refresh):
            return False
        try:
            session = await refresh()
        except Exception:  # noqa: BLE001 — an unreachable snapshot is a real failure
            logger.warning("session baseline rebuild failed", exc_info=True)
            return False
        if not isinstance(session, dict):
            return False
        await self._apply_open_session(session, restore_compactions=False)
        self._restore_compactions_when_reading = True
        logger.warning("session event cursor expired; rebuilt the client baseline")
        return True

    async def _collect_session_events(self) -> None:
        reconnect_attempt = 0
        cursor_recoveries = 0
        baseline_rebuilds = 0
        try:
            while self._session_attached:
                try:
                    # Keep the network reader independent from Textual work.
                    # Applying a delta can trigger layout/markdown rendering
                    # and briefly block this worker; if that also blocks the
                    # SSE iterator, the server-side live queue can overflow
                    # during an otherwise healthy burst.  The reader advances
                    # TerminalSession's cursor as soon as each frame arrives,
                    # while this loop remains the sole state/render consumer.
                    pending: asyncio.Queue[tuple[str, object]] = asyncio.Queue(
                        maxsize=_SESSION_EVENT_PENDING_CAPACITY,
                    )

                    async def read_events() -> None:
                        try:
                            async for event in self.session.session_events():
                                await pending.put(("event", event))
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            await pending.put(("error", exc))
                        else:
                            await pending.put(("end", None))

                    reader = asyncio.create_task(read_events())
                    try:
                        if self._restore_compactions_when_reading:
                            self._restore_compactions_when_reading = False
                            # The reader is live while this optional lookup
                            # runs. Frames accumulate in `pending` and are
                            # applied below after the baseline is ready.
                            await self._restore_compaction_entries()
                        while True:
                            kind, payload = await pending.get()
                            if kind == "error":
                                if isinstance(payload, BaseException):
                                    raise payload
                                raise RuntimeError("session event reader failed")
                            if kind == "end":
                                if not self._session_attached or not self.state.turn_active:
                                    return
                                # An SSE end sentinel without a terminal turn
                                # event is incomplete; reconnect from the
                                # cursor already advanced by the reader.
                                raise RuntimeError(
                                    "session event stream ended before the turn completed"
                                )
                            event = payload
                            if not isinstance(event, dict):
                                raise TypeError("session event must be an object")
                            reconnect_attempt = 0
                            try:
                                await self._consume_stream_event(event, pop_pending=True)
                            except Exception:  # noqa: BLE001
                                # A single malformed event must not abort the
                                # stream, otherwise turn_active stays stuck.
                                logger.exception(
                                    "tui session event failed type=%s", event.get("type")
                                )
                    finally:
                        if not reader.done():
                            reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if not self._session_attached:
                        return
                    if (
                        cursor_recoveries < _SESSION_EVENT_CURSOR_RECOVERIES
                        and self._recover_expired_cursor(exc)
                    ):
                        cursor_recoveries += 1
                        continue
                    if (
                        baseline_rebuilds < _SESSION_EVENT_BASELINE_REBUILDS
                        and await self._rebuild_session_baseline(exc)
                    ):
                        baseline_rebuilds += 1
                        # The eviction was about the old cursor, so the rewind
                        # budget and the reconnect streak start over.
                        cursor_recoveries = 0
                        reconnect_attempt = 0
                        continue
                    if reconnect_attempt >= len(_SESSION_EVENT_RECONNECT_DELAYS):
                        await self._handle_stream_failure(
                            exc, source="session event stream"
                        )
                        return
                    delay = _SESSION_EVENT_RECONNECT_DELAYS[reconnect_attempt]
                    reconnect_attempt += 1
                    logger.warning(
                        "session event stream failed; reconnecting in %.2fs",
                        delay,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
        finally:
            self._event_stream_connected = False

    async def _collect_response(
        self,
        text: str,
        *,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Submit one message while the shared session stream consumes events."""
        try:
            logger.info("tui.collect_response start session=%s chars=%d", self.state.session_id, len(text))
            if images:
                await self.session.send_message(text, images=images)
            else:
                await self.session.send_message(text)
        except Exception as exc:
            logger.exception("tui.collect_response failed")
            await self._handle_stream_failure(exc, source="message submission")

    async def _handle_stream_failure(
        self, exc: BaseException, *, source: str
    ) -> None:
        """Turn a broken HTTP stream into a terminal local error.

        The server normally closes a turn with an ``error``/``turn_finished``
        event.  A provider or connection failure can instead terminate either
        HTTP stream before that boundary, leaving ``turn_active`` set forever.
        Apply the same state transition as a protocol error so the composer
        and queued-input worker are released even when no terminal frame was
        delivered.  The two TUI streams may fail together, hence the guard.
        """
        logger.error(
            "tui.%s failed",
            source,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if self._stream_failure_reported:
            return
        if not self.state.turn_active:
            self._record_error(exc)
            return
        self._stream_failure_reported = True
        message = f"{source} failed: {exc}"
        event = {
            "type": "error",
            "data": {"code": "client_stream_error", "message": message},
        }
        self.state.apply_event(event)
        await self._handle_stream_event(event)

    async def _consume_stream_event(
        self,
        event: dict[str, JsonValue],
        *,
        pop_pending: bool = False,
    ) -> None:
        """Apply one transport event."""
        event_type = event.get("type")
        if event_type == "message":
            data = event.get("data") or {}
            if data.get("role") == "user":
                # The session event stream is authoritative for accepted
                # queued inputs; the message stream is authoritative for the
                # request currently being consumed.
                content = str(data.get("content") or "")
                if pop_pending:
                    self._pop_pending_message(content)
                self.state.append_message("user", content)
                await self._render_new_transcript_entries()
                if pop_pending:
                    self._refresh_all()
                return
        self.state.apply_event(event)
        if self._view_active:
            if event_type in {
                "assistant_message",
                "assistant_message_delta",
                "tool_calls_started",
                "tool_result",
            }:
                self._view_main_busy = True
                view = self._safe_query_one("#thread_view", ThreadView)
                if view is not None and view.thread:
                    view.set_main_busy(True, self._thread_summary(self._view_thread_id))
            if (
                self.state.pending_permission_payload is not None
                or self.state.pending_user_input_payload is not None
            ):
                # Subagent threads never ask the user: any prompt belongs to the
                # parent (main) thread and must be answered on the main view.
                await self._exit_thread_view()
        await self._handle_stream_event(event)
        await self._start_interaction_response(event)

    async def _submit_live_input(self, payload: dict[str, JsonValue]) -> None:
        self._set_input_placeholder("Answer the request, or choose an inline option")
        answer = await self._answers.get()
        await self.session.submit_user_input(
            str(payload.get("request_id") or ""),
            answer,
        )

    async def _submit_live_permission(self, payload: dict[str, JsonValue]) -> None:
        self._set_input_placeholder("Choose an inline approval option, or type a decision")
        response = await self._permission_decisions.get()
        await self.session.respond_permission(
            str(payload.get("request_id") or ""),
            str(response.get("decision") or "deny"),
            scope=str(response.get("scope") or "once"),
        )

    async def _start_interaction_response(self, event: dict[str, JsonValue]) -> None:
        event_type = str(event.get("type") or "")
        payload = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type not in {"permission_request", "user_input_required"}:
            return
        if self._interaction_response_task is not None:
            await asyncio.gather(
                self._interaction_response_task,
                return_exceptions=True,
            )
        if event_type == "permission_request":
            response = self._submit_live_permission(payload)
        else:
            response = self._submit_live_input(payload)
        task = asyncio.create_task(response)
        self._interaction_response_task = task
        task.add_done_callback(self._interaction_response_done)

    def _interaction_response_done(self, task: asyncio.Task[None]) -> None:
        if self._interaction_response_task is task:
            self._interaction_response_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        # The server invalidates an interaction as soon as its turn ends
        # (interrupt, timeout, or terminal error). A response that races
        # that invalidation is rejected with ``interaction_no_longer_pending``.
        # Treat it as an expired request rather than a failure: drop the
        # stale dialog and explain, instead of flipping the UI to "Error".
        if getattr(error, "code", "") == "interaction_no_longer_pending":
            self._interaction_response_pending = False
            self.state._clear_pending_interactions(tool_status="cancelled")
            self.run_worker(
                self._refresh_changed_tool_widgets,
                exclusive=False,
                name="refresh_expired_interaction",
            )
            self.run_worker(
                self._render_new_transcript_entries,
                exclusive=False,
                name="render_expired_interaction_notice",
            )
            self.state.append_notice(
                "interaction",
                "Request expired (the turn was interrupted); nothing was decided.",
            )
            self._refresh_status()
            self._refresh_input_mode()
            return
        self._record_error(error)

    def _cancel_interaction_response(self) -> None:
        task = self._interaction_response_task
        self._interaction_response_task = None
        self._interaction_response_pending = False
        if task is not None and not task.done():
            task.cancel()

    def _safe_query_one(self, selector: str, expect_type: type | None = None) -> Any:
        """Query a widget that may be unmounted or absent."""

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
        # Errors are rare and critical: bypass the status throttle so the
        # failure is visible immediately.
        self._refresh_status_now()
        self._refresh_queue_panel()
        self._refresh_input_mode()

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
        self._refresh_queue_panel()
        self._refresh_input_mode()

    def _refresh_status(self) -> None:
        if not self.is_mounted:
            return
        # Throttle: streaming deltas arrive many times per second; the status
        # bar only needs to reflect the latest state a few times per second.
        now = time.monotonic()
        if (
            self._stream_timer is not None
            and now - self._last_status_refresh < _STATUS_REFRESH_INTERVAL
        ):
            self._status_refresh_pending = True
            return
        self._last_status_refresh = now
        self._status_refresh_pending = False
        self._refresh_status_now()

    def _refresh_status_now(self) -> None:
        try:
            panel = self.query_one("#status_bar", Static)
        except Exception:  # noqa: BLE001 — defensive; widget may be unmounting
            return
        queue_depth = self._queued_request_count()
        usage = self.state.usage
        panel.update(
            status_renderable(
                status=self.state.status,
                session_id=self.state.session_id,
                session_title=self.state.session_title,
                thread_id=self.state.thread_id,
                workspace_root=self.state.workspace_root,
                provider=self.state.provider,
                model=self.state.model,
                agent_name=self.state.agent_name,
                model_mode=self.state.model_mode,
                status_slots=self.state.status_slots,
                context_window=self.state.context_window,
                context_input_tokens=self.state.context_input_tokens,
                activity=self._activity_status(),
                queue_depth=queue_depth,
                usage=usage,
                width=panel.size.width or self.size.width,
            )
        )

    def _refresh_task_panel(self) -> None:
        panel = self._safe_query_one("#task_panel", Collapsible)
        body = self._safe_query_one("#task_list", TaskListWidget)
        if panel is None or body is None:
            return
        tasks = list(self.state.tasks.values())
        panel.display = bool(tasks)
        self._refresh_runtime_panels()
        if not tasks:
            return
        running = sum(
            task.status in {"pending", "running"} for task in tasks
        )
        panel.title = f"Tasks ({running} running)" if running else "Tasks"
        active = [
            task for task in tasks if task.status in {"pending", "running"}
        ]
        terminal = [
            task for task in tasks
            if task.status not in {"pending", "running"}
        ]
        visible = active[:5]
        if len(visible) < 5:
            visible += terminal[-(5 - len(visible)):]
        body.update_tasks(
            visible,
            width=body.size.width or self.size.width,
        )

    def _refresh_queue_panel(self) -> None:
        panel = self._safe_query_one("#queue_panel", Collapsible)
        body = self._safe_query_one("#queue_list", Static)
        if panel is None or body is None:
            return
        messages = self._queued_messages()
        panel.display = bool(messages)
        self._refresh_runtime_panels()
        if not messages:
            return
        panel.title = f"Queue ({len(messages)})"
        body.update(
            queue_renderable(
                messages,
                width=body.size.width or self.size.width,
            )
        )

    def _refresh_runtime_panels(self) -> None:
        container = self._safe_query_one("#runtime_panels", Horizontal)
        task_panel = self._safe_query_one("#task_panel", Collapsible)
        queue_panel = self._safe_query_one("#queue_panel", Collapsible)
        if container is not None and task_panel is not None and queue_panel is not None:
            container.display = task_panel.display or queue_panel.display
            container.set_class(self.size.height < 24, "compact")

    async def _handle_stream_event(self, event: dict[str, JsonValue]) -> None:
        event_type = str(event.get("type") or "")
        refresh_input = False
        if event_type == "turn_started":
            self._stream_failure_reported = False
            self._finalize_activity()
            await self._append_activity()
        elif event_type == "turn_finished":
            self._cancel_interaction_response()
            self._resolve_active_choice("request ended")
            await self._cancel_stream_timer()
            await self._flush_tool_refresh()
            self._finalize_activity()
            await self._refresh_changed_tool_widgets()
            await self._refresh_session_identity()
            refresh_input = True
        elif event_type == "turn_cancelled":
            self._cancel_interaction_response()
            self._resolve_active_choice("request cancelled")
            await self._cancel_stream_timer()
            await self._flush_tool_refresh()
            self._finalize_activity()
            await self._refresh_changed_tool_widgets()
            self._refresh_status()
            refresh_input = True
        elif event_type == "usage":
            self._update_activity()
            self._refresh_status_now()
        elif event_type == "assistant_message_delta":
            if self._stream_timer is None:
                self._stream_timer = asyncio.create_task(self._stream_tick())
            self._refresh_status()
            return  # The timer handles delta rendering.
        elif event_type == "assistant_message":
            await self._cancel_stream_timer()
            await self._refresh_streaming_assistant_widget()
        elif event_type == "tool_call_delta":
            self._deferred_tool_ids.update(self.state._changed_tool_ids)
            self._schedule_tool_refresh()
            self._refresh_status()
            return
        elif event_type == "tool_calls_started":
            await self._flush_tool_refresh()
            await self._refresh_changed_tool_widgets()
        elif event_type == "tool_result":
            await self._flush_tool_refresh()
            await self._refresh_changed_tool_widgets()
        elif event_type == "task_updated":
            self._refresh_task_panel()
        elif event_type == "history_updated":
            await self._clear_rendered_transcript()
        elif event_type == "permission_request":
            await self._flush_tool_refresh()
            await self._render_new_transcript_entries()
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
            if event_type == "error":
                self._cancel_interaction_response()
                self._resolve_active_choice("request failed")
                self.state.status = "Error"
            self._interaction_response_pending = False
            if event_type == "error":
                await self._refresh_changed_tool_widgets()
            refresh_input = True
        elif event_type in {"user_input_required"}:
            refresh_input = True
        await self._render_new_transcript_entries()
        if event_type == "error":
            # Errors are rare and critical: bypass the status throttle.
            self._refresh_status_now()
        else:
            self._refresh_status()
        if refresh_input:
            self._refresh_input_mode()

    async def _stream_tick(self) -> None:
        """Refresh streaming assistant widget at ~50ms intervals."""
        try:
            while True:
                await asyncio.sleep(0.05)
                await self._refresh_streaming_assistant_widget()
                if self._status_refresh_pending:
                    self._refresh_status()
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

    def _schedule_tool_refresh(self) -> None:
        if self._tool_refresh_timer is None:
            self._tool_refresh_timer = asyncio.create_task(
                self._delayed_tool_refresh()
            )

    async def _delayed_tool_refresh(self) -> None:
        try:
            await asyncio.sleep(0.05)
            self._tool_refresh_timer = None
            await self._flush_tool_refresh()
        except asyncio.CancelledError:
            pass

    async def _cancel_tool_refresh_timer(self) -> None:
        task = self._tool_refresh_timer
        self._tool_refresh_timer = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _flush_tool_refresh(self) -> None:
        ids = set(self._deferred_tool_ids)
        self._deferred_tool_ids.clear()
        task = self._tool_refresh_timer
        if task is not None and task is not asyncio.current_task():
            await self._cancel_tool_refresh_timer()
        if ids:
            await self._render_new_transcript_entries()
            await self._refresh_changed_tool_widgets(ids)

    async def _render_replay_window(self) -> None:
        """Mount a bounded tail of resumed history."""
        total = len(self.state.transcript)
        if total <= _REPLAY_WINDOW:
            self._window_start = 0
            self._window_end = 0
            await self._render_new_transcript_entries()
            return
        self._window_start = total - _REPLAY_WINDOW
        self._window_end = self._window_start
        await self._render_new_transcript_entries()

    async def _load_earlier_replay(self) -> None:
        """Shift the bounded replay window to an earlier batch."""
        if self._replay_loading or self._window_start <= 0:
            return
        self._replay_loading = True
        self._transcript_follow = False
        try:
            async with self._render_lock:
                stream = self._safe_query_one("#transcript", VerticalScroll)
                if stream is None:
                    return
                batch_start = max(0, self._window_start - _REPLAY_BATCH)
                entries = self.state.transcript[batch_start:self._window_start]
                if not entries:
                    self._window_start = 0
                    return
                widgets = await self._mount_entries(
                    stream, batch_start, self._window_start, prepend=True
                )
                if not widgets:
                    self._window_start = batch_start
                    return
                inserted_height = self._widgets_height(widgets)
                self._window_start = batch_start
                await self._drop_trailing_excess(stream)
                # The inserted batch shifts content down; keep the viewport
                # pinned where it was by scrolling down by the inserted size.
                self.call_after_refresh(
                    lambda h=inserted_height: stream.scroll_to(
                        y=max(0, stream.scroll_y + h), animate=False
                    )
                )
        finally:
            self._replay_loading = False

    async def _load_newer_replay(self) -> None:
        """Shift the bounded replay window toward the live tail."""
        async with self._render_lock:
            stream = self._safe_query_one("#transcript", VerticalScroll)
            if stream is None:
                return
            if self._window_end >= len(self.state.transcript):
                await self._drop_leading_excess(stream, follow=True)
                return
            end = min(len(self.state.transcript), self._window_end + _REPLAY_BATCH)
            await self._mount_entries(stream, self._window_end, end)
            self._window_end = end
            await self._drop_leading_excess(stream, follow=True)
            self._transcript_follow = True
            self.call_after_refresh(lambda: stream.scroll_end(animate=False))

    async def _render_new_transcript_entries(self) -> bool:
        async with self._render_lock:
            stream = self._safe_query_one("#transcript", VerticalScroll)
            if stream is None:
                return False
            end = len(self.state.transcript)
            if end <= self._window_end:
                # Follow the live tail only when the user is already at the
                # bottom; never yank the viewport while reading older entries.
                follow = stream.is_vertical_scroll_end
                await self._drop_leading_excess(stream, follow)
                return False
            # Follow the live tail only when the user is already at the
            # bottom; never yank the viewport while reading older entries.
            follow = stream.is_vertical_scroll_end
            self._transcript_follow = follow
            # Keep live updates in state while the user is reading older
            # content. They are mounted in bounded batches when the user
            # returns to the bottom, so a long-running turn cannot grow the
            # Textual DOM behind the user's back.
            if not follow:
                return False
            await self._mount_entries(stream, self._window_end, end)
            self._window_end = end
            await self._drop_leading_excess(stream, follow)
            if follow:
                self.call_after_refresh(
                    lambda: stream.scroll_end(animate=False)
                )
            return True

    async def _mount_entries(
        self,
        stream: VerticalScroll,
        start: int,
        end: int,
        *,
        prepend: bool = False,
    ) -> list[Any]:
        """Mount and track one transcript slice in entry order."""
        widgets: list[Any] = []
        reference = stream.children[0] if prepend and stream.children else None
        for entry in self.state.transcript[start:end]:
            widget = self._widget_for_entry(entry)
            if widget is None:
                continue
            widgets.append(widget)
            # Textual's ``parent`` attribute is updated synchronously by
            # ``mount()``, so mounting the same widget twice raises.
            if widget.parent is stream:
                continue
            if widget.parent is not None:
                try:
                    await widget.remove()
                except Exception:  # noqa: BLE001
                    pass
            if reference is not None:
                await stream.mount(widget, before=reference)
            else:
                await stream.mount(widget)
        if prepend:
            self._mounted_entry_widgets[0:0] = widgets
        else:
            self._mounted_entry_widgets.extend(widgets)
        return widgets

    @staticmethod
    def _widgets_height(widgets: list[Any]) -> int:
        return sum(
            (w.virtual_size.height if w.virtual_size else 1)
            for w in widgets
        )

    async def _drop_leading_excess(self, stream: VerticalScroll, follow: bool) -> int:
        """Bound the mounted tail without moving a historical viewport."""
        if not follow:
            return 0
        excess = self._window_end - self._window_start - _MAX_MOUNTED_ENTRIES
        if excess <= 0:
            return 0
        removed = self._mounted_entry_widgets[:excess]
        self._mounted_entry_widgets = self._mounted_entry_widgets[excess:]
        for widget in removed:
            if widget.parent is stream:
                await widget.remove()
        self._window_start += len(removed)
        return len(removed)

    async def _drop_trailing_excess(self, stream: VerticalScroll) -> int:
        """Drop the newest mounted entries when the window exceeds the cap."""
        excess = self._window_end - self._window_start - _MAX_MOUNTED_ENTRIES
        if excess <= 0:
            return 0
        removed = self._mounted_entry_widgets[-excess:]
        self._mounted_entry_widgets = self._mounted_entry_widgets[:-excess]
        for widget in removed:
            if widget.parent is stream:
                await widget.remove()
        self._window_end -= len(removed)
        return len(removed)

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
        if self._view_active:
            # Read-only thread view: keep the composer visible but inert, and
            # keep the input out of the way while the subagent thread streams.
            composer.load_text("")
            composer.disabled = True
            hint.update(f"viewing {self._view_thread_id} — read-only · Ctrl+T / Esc to return")
            self._set_input_placeholder("read-only thread view")
            return
        composer.disabled = False
        composer.display = True
        if self.state.pending_user_input_payload is not None:
            hint.update("Answer required")
            self._set_input_placeholder("Type an answer")
        elif self.state.pending_permission_payload is not None:
            hint.update("Approval required")
            self._set_input_placeholder("Type allow/deny")
        elif self.state.turn_active:
            # Turn is in progress; the composer remains visible and
            # messages get queued (see submit_composer). Surface the
            # queue depth so the user knows their input will be picked
            # up after the current turn ends.
            depth = self._queued_request_count()
            if depth > 0:
                hint.update(
                    f"Queueing: {depth} pending — type more or wait"
                )
            else:
                hint.update("Turn running — type to queue a follow-up")
            self._set_input_placeholder("Message XBotv2 (queue)")
        else:
            count = len(self._pending_images)
            hint.update(
                f"{count} image{'s' if count != 1 else ''} attached"
                if count
                else "Tab scrolls the transcript · Esc returns to the input"
            )
            self._set_input_placeholder("Message XBotv2")
        if self.focused is None:
            composer.focus()

    def _queued_request_count(self) -> int:
        return len(self._queued_messages())

    def _queued_messages(self) -> list[str]:
        return list(self._pending_messages.values())

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
        choice = choices[self._active_choice_index]
        if choice.kind == "answer_custom":
            self._resolve_active_choice("Other")
            self._interaction_response_pending = False
            self._refresh_input_mode()
            return True
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

    def scroll_transcript_page(self, *, down: bool) -> None:
        stream = self._safe_query_one("#transcript", VerticalScroll)
        if stream is None:
            return
        if down:
            stream.scroll_page_down(animate=False)
        else:
            stream.scroll_page_up(animate=False)
        # An explicit page move releases the live tail right away; the settled
        # position reported by the transcript re-enables it at the bottom.
        self._transcript_follow = False

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
        self.state.prune_finished_tasks()
        self._refresh_task_panel()
        self._refresh_status()

    def _update_pending_tool_elapsed(self) -> None:
        for tool_call_id, widget in list(self._tool_widgets.items()):
            tool = self.state.tools.get(tool_call_id)
            if tool is None or tool.finished_at > 0:
                continue
            try:
                meta = widget.query_one(".meta", Static)
            except Exception:
                continue
            meta.update(_build_title(tool, tool.elapsed(time.monotonic())))

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
        full_input = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("prompt_cache_write_tokens", 0)
        )
        marker = "done" if final else spinner(self._spinner_index)
        verb = "completed" if final else "working"
        return (
            f"{marker} turn {self.state.turn} {verb} "
            f"{elapsed:.1f}s  "
            f"tokens in:{full_input} out:{usage['output_tokens']} "
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
            widget = message_widget(
                self.state,
                message,
                reasoning_expanded=self._reasoning_expanded,
            )
            self._message_widgets[int(key)] = widget
            self._trim_message_widgets()
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
            widget = tool_widget(tool, details_expanded=self._tool_details_expanded)
            self._tool_widgets[widget_id] = widget
            self._trim_tool_widgets()
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

    def _trim_message_widgets(self) -> None:
        """Bound the widget cache without removing mounted entries."""
        while len(self._message_widgets) > _MAX_MESSAGE_WIDGETS:
            oldest = next(iter(self._message_widgets))
            self._message_widgets.pop(oldest, None)

    def _trim_tool_widgets(self) -> None:
        while len(self._tool_widgets) > _MAX_TOOL_WIDGETS:
            oldest = next(iter(self._tool_widgets))
            self._tool_widgets.pop(oldest, None)

    def _refresh_tool_widget_sync(self, tool_call_id: str) -> None:
        """Refresh cached tool content; the async path owns choices."""
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
            body.update(render_text(detail))
        elif detail:
            widget.mount(
                tool_detail_widget(
                    detail, expanded=self._tool_details_expanded
                )
            )

    async def _refresh_changed_tool_widgets(
        self, tool_ids: set[str] | None = None
    ) -> None:
        for old_id, new_id in self.state._tool_id_renames.items():
            widget = self._tool_widgets.pop(old_id, None)
            if widget is not None:
                self._tool_widgets[new_id] = widget
        changed_ids = tool_ids if tool_ids is not None else self.state._changed_tool_ids
        for tool_call_id in list(changed_ids):
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
        stream = self._safe_query_one("#transcript", VerticalScroll)
        follow_output = stream is not None and stream.is_vertical_scroll_end
        await self._apply_streaming_message_widget(widget, message)
        if stream is not None and follow_output:
            stream.scroll_end(animate=False)

    async def _apply_streaming_message_widget(
        self, widget: Any, message: TuiMessage
    ) -> None:
        """Render separate reasoning and visible-content blocks."""
        reasoning = self._query_child_first(widget, ".reasoning")
        if message.reasoning:
            if reasoning is not None:
                reasoning.update(render_reasoning(message.reasoning))
            else:
                block = reasoning_widget(
                    render_reasoning(message.reasoning),
                    expanded=self._reasoning_expanded,
                )
                body = self._query_child_first(widget, ".body")
                await widget.mount(block, before=body)
        body = self._query_child_first(widget, ".body")
        if body is not None:
            body.update(render_message(message.content, role=message.role))
        elif message.content:
            await widget.mount(
                Static(
                    render_message(message.content, role=message.role),
                    classes="body",
                )
            )

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
            body.update(render_text(detail))
        elif detail:
            await widget.mount(
                tool_detail_widget(
                    detail, expanded=self._tool_details_expanded
                )
            )
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
            classes = child.classes or ""
            if isinstance(child, Static) and (
                "choice" in classes or "permission-context" in classes
            ):
                await child.remove()
        for ck in list(self._choice_payloads.keys()):
            if ck == key:
                self._choice_payloads.pop(ck, None)
                self._choice_request_ids.pop(ck, None)
                self._choice_widgets.pop(ck, None)
                if self._active_choice_key == ck:
                    self._active_choice_key = None

        if not tool.permission_pending or not tool.permission_request_id:
            return
        # A resolved interaction must stay resolved. It may only be answered
        # once: re-arming the dialog would let the user respond again to an
        # interaction the server already resolved, which fails with
        # ``interaction_no_longer_pending``. The resolved label is rendered
        # below but the choice set is never re-activated.
        if key in self._resolved_choice_keys:
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
        context_widget = Static(
            tool.permission_reason or f"{tool.name} requires approval",
            classes="permission-context",
            markup=False,
        )
        await widget.mount(context_widget, choice_widget)
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
            source = str(notice.payload.get("source") or "")
            choices = []
            if isinstance(options, list):
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    label = str(option.get("label") or "")
                    description = str(option.get("description") or "")
                    if label and description:
                        choices.append(InlineChoice(
                            f"{label}: {description}",
                            "answer",
                            {"answer": label},
                        ))
                if choices and source != "ask_user":
                    choices.append(InlineChoice("Other", "answer_custom", {}))
            return self._request_widget(notice, key=key, title=f"{notice.ts}  question", choices=choices)
        if notice.kind == "compact":
            summary = str(notice.payload.get("summary") or "")
            if summary:
                return compact_widget(
                    title=f"{notice.ts}  {notice.text}",
                    summary=summary,
                )
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
