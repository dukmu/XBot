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
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from xbotv2.tui.client import TuiNotice, TuiState, TuiTool, _parse_permission_decision
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.textual_state import route_submitted_text


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
    }

    #main {
        height: 1fr;
    }

    #transcript {
        width: 2fr;
        height: 1fr;
        border: solid $primary;
    }

    #side {
        width: 1fr;
        min-width: 32;
        height: 1fr;
    }

    #status_panel, #tools_panel, #notices_panel {
        border: solid $surface;
        padding: 0 1;
    }

    #status_panel {
        height: 7;
    }

    #tools_panel {
        height: 1fr;
    }

    #notices_panel {
        height: 12;
    }

    #input {
        dock: bottom;
        height: 3;
        border: solid $accent;
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
        self._connected = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield RichLog(id="transcript", wrap=True, markup=True, highlight=True)
            with Vertical(id="side"):
                yield Static(id="status_panel")
                yield Static(id="tools_panel")
                yield Static(id="notices_panel")
        yield Input(placeholder="Message XBotv2", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        self._refresh_all()
        self.run_worker(self._connect(), exclusive=True, name="connect")

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

        self.state.append_message("user", text)
        self._refresh_all()
        self.run_worker(self._collect_response(text), exclusive=False, name="turn")

    def action_clear_input(self) -> None:
        """Clear the input box without changing protocol interaction state."""
        self.query_one("#input", Input).value = ""

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
        self._refresh_tools()
        self._refresh_notices()
        self._refresh_input_mode()

    def _refresh_status(self) -> None:
        panel = self.query_one("#status_panel", Static)
        panel.update(
            "\n".join([
                f"[b]XBotv2[/b]  {self.state.status}",
                f"Session: {self.state.session_id}",
                f"Thread:  {self.state.thread_id}",
                f"Agent:   {self.state.agent_name}",
                f"Turn:    {self.state.turn}",
            ])
        )

    def _refresh_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.clear()
        for entry in self.state.transcript:
            if entry.kind == "message":
                try:
                    message = self.state.messages[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                label = "You" if message.role == "user" else self.state.agent_name
                style = "cyan" if message.role == "user" else "green"
                log.write(f"[{style}][b]{label}[/b][/{style}]\n{message.content}\n")
            elif entry.kind == "tool":
                tool = self.state.tools.get(entry.key)
                if tool is None:
                    continue
                log.write(
                    f"[yellow][b]Tool[/b][/yellow] {tool.name} "
                    f"[{tool.status}]\n{tool.args_preview}\n{tool.summary}\n"
                )
            elif entry.kind == "notice":
                try:
                    notice = self.state.notices[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                log.write(f"[magenta][b]{notice.kind}[/b][/magenta] {notice.text}\n")
            elif entry.kind == "error":
                try:
                    error = self.state.errors[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                log.write(f"[red][b]Error[/b][/red] {error}\n")

        for error in self.state.errors:
            log.write(f"[red][b]Error[/b][/red] {error}\n")

    def _refresh_tools(self) -> None:
        panel = self.query_one("#tools_panel", Static)
        lines = ["[b]Tools[/b]"]
        tools = list(self.state.tools.values())[-12:]
        if not tools:
            lines.append("No tool calls yet.")
        for tool in tools:
            lines.extend(_tool_lines(tool))
        panel.update("\n".join(lines))

    def _refresh_notices(self) -> None:
        panel = self.query_one("#notices_panel", Static)
        lines = ["[b]Events[/b]"]
        notices = self.state.notices[-8:]
        if not notices:
            lines.append("No notices.")
        for notice in notices:
            lines.append(f"{notice.kind}: {notice.text}")
        panel.update("\n".join(lines))

    def _refresh_input_mode(self) -> None:
        if self.state.pending_user_input_request_id:
            self._set_input_placeholder("Answer the pending question")
        elif self.state.pending_permission_request_id:
            self._set_input_placeholder("Approve? type yes/allow or no/deny")
        else:
            self._set_input_placeholder("Message XBotv2")

    def _set_input_placeholder(self, text: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#input", Input).placeholder = text


def _tool_lines(tool: TuiTool) -> list[str]:
    lines = [f"- {tool.name} [{tool.status}]"]
    if tool.args_preview:
        lines.append(f"  args: {tool.args_preview}")
    if tool.summary:
        lines.append(f"  result: {tool.summary}")
    return lines
