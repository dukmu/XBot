"""Textual protocol TUI client.

This frontend is a standard JSONL protocol client. It talks to
``xbotv2 --mode server`` through ``TerminalSession`` and does not import the
runtime engine, bootstrap, LangChain, or LangGraph.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog, Static

from xbotv2.tui.client import TuiNotice, TuiState, _parse_permission_decision
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.textual_state import (
    queue_user_message,
    render_transcript_entry,
    route_submitted_text,
)


class TextualTuiClient:
    """Run the Textual UI over the JSONL protocol client/server boundary."""

    def __init__(
        self,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
        session_id: str = "default",
        thread_id: str = "agent",
        no_plugins: bool = False,
    ) -> None:
        self.app = XBotTextualApp(
            data_dir=data_dir,
            personality_id=personality_id,
            provider_name=provider_name,
            session_id=session_id,
            thread_id=thread_id,
            no_plugins=no_plugins,
        )

    async def run(self) -> None:
        await self.app.run_async()


class XBotTextualApp(App[None]):
    """OpenCode-style full-screen TUI backed by XBotv2 protocol frames."""

    CSS = """
    Screen {
        layout: vertical;
        background: #101418;
        color: #d8dee9;
    }

    #status_bar {
        height: 1;
        padding: 0 1;
        background: #151b22;
        color: #d8dee9;
    }

    #transcript {
        height: 1fr;
        border: tall #5e81ac;
        padding: 1 2;
        background: #121820;
        color: #e5e9f0;
        scrollbar-color: #88c0d0;
        scrollbar-color-hover: #8fbcbb;
        scrollbar-background: #1b222b;
    }

    #input {
        dock: bottom;
        height: 3;
        border: tall #88c0d0;
        background: #111820;
        color: #eceff4;
        padding: 0 1;
    }

    #input:focus {
        border: tall #a3be8c;
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
        session_id: str,
        thread_id: str,
        no_plugins: bool,
    ) -> None:
        super().__init__()
        self.session = TerminalSession(
            data_dir=data_dir,
            personality_id=personality_id,
            provider_name=provider_name,
            session_id=session_id,
            thread_id=thread_id,
            no_plugins=no_plugins,
        )
        self.state = TuiState(session_id=session_id, thread_id=thread_id)
        self._answers: asyncio.Queue[str] = asyncio.Queue()
        self._permission_decisions: asyncio.Queue[str] = asyncio.Queue()
        self._outbound_messages: asyncio.Queue[str] = asyncio.Queue()
        self._connected = False
        self._turn_worker_running = False
        self._rendered_transcript_entries = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status_bar", markup=True)
        yield RichLog(
            id="transcript",
            wrap=True,
            markup=False,
            highlight=False,
            auto_scroll=True,
        )
        yield Input(placeholder="Message XBotv2", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        self._refresh_all()
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

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text in {"/exit", "/quit"}:
            self.exit()
            return
        route = route_submitted_text(
            self.state,
            self._answers,
            self._permission_decisions,
            text,
        )
        if route == "user_input":
            self._append_local_notice("Answer queued", text)
            self._refresh_all()
            return
        if route == "permission":
            decision = _parse_permission_decision(text)
            self._append_local_notice("Approval queued", decision)
            self._refresh_all()
            return
        if not self._connected:
            self._append_local_notice("Not connected", "Server is not ready yet.")
            self._refresh_all()
            return

        queue_user_message(self.state, self._outbound_messages, text)
        self._refresh_all()
        if not self._turn_worker_running:
            self._turn_worker_running = True
            self.run_worker(self._drain_message_queue, exclusive=True, name="turn")

    def action_clear_input(self) -> None:
        """Clear the input box without changing protocol interaction state."""
        self.query_one("#input", Input).value = ""

    async def _drain_message_queue(self) -> None:
        try:
            while not self._outbound_messages.empty():
                text = await self._outbound_messages.get()
                self.state.append_message("user", text)
                self._refresh_all()
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
                self._refresh_all()
        except Exception as exc:
            self._record_error(exc)

    async def _answer_live_input(self, payload: dict[str, Any]) -> str:
        del payload
        self._set_input_placeholder("Answer question")
        return await self._answers.get()

    async def _answer_live_permission(self, payload: dict[str, Any]) -> str:
        del payload
        self._set_input_placeholder("Type yes/allow or no/deny")
        return await self._permission_decisions.get()

    def _record_error(self, exc: BaseException) -> None:
        self.state.status = "Error"
        self.state.errors.append(str(exc))
        self._refresh_all()

    def _append_local_notice(self, kind: str, text: str) -> None:
        self.state.notices.append(TuiNotice(kind=kind, text=text))

    def _refresh_all(self) -> None:
        if not self.is_mounted:
            return
        self._refresh_status()
        self._refresh_transcript()
        self._refresh_input_mode()

    def _refresh_status(self) -> None:
        panel = self.query_one("#status_bar", Static)
        queue_depth = self._outbound_messages.qsize()
        usage = self.state.usage
        panel.update(
            "  ".join([
                f"[b]XBotv2[/b] {_status_badge(self.state.status)}",
                f"{self.state.session_id}/{self.state.thread_id}",
                f"agent:{self.state.agent_name}",
                f"turn:{self.state.turn}",
                f"queued:{queue_depth}",
                (
                    f"usage req:{usage['requests']} "
                    f"in:{usage['input_tokens']} "
                    f"out:{usage['output_tokens']} "
                    f"total:{usage['total_tokens']}"
                ),
            ])
        )

    def _refresh_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        for entry in self.state.transcript[self._rendered_transcript_entries:]:
            rendered = render_transcript_entry(self.state, entry)
            if rendered:
                log.write(rendered)
        self._rendered_transcript_entries = len(self.state.transcript)

    def _refresh_input_mode(self) -> None:
        if self.state.pending_user_input_active:
            self._set_input_placeholder("Answer the pending question")
        elif self.state.pending_permission_active:
            self._set_input_placeholder("Approve? type yes/allow or no/deny")
        else:
            self._set_input_placeholder("Message XBotv2")

    def _set_input_placeholder(self, text: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#input", Input).placeholder = text


def _status_badge(status: str) -> str:
    styles = {
        "Ready": "green",
        "Running": "yellow",
        "Connecting": "yellow",
        "Waiting for user": "cyan",
        "Approval required": "magenta",
        "Permission denied": "red",
        "Error": "red",
        "Shutdown": "dim",
    }
    style = styles.get(status, "white")
    return f"[{style}]{status}[/{style}]"
