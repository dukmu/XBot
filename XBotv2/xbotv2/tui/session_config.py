"""Shared TUI session configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xbotv2.tui.terminal import TerminalSession


@dataclass(frozen=True)
class TuiSessionConfig:
    session_id: str | None = None
    thread_id: str = "agent"
    agent: str | None = None
    workspace_root: Path | str | None = None
    session_mode: str | None = None
    base_url: str = "http://127.0.0.1:4096"
    uds_path: str | None = None

    def create_terminal_session(self) -> TerminalSession:
        return TerminalSession(
            session_id=self.session_id,
            thread_id=self.thread_id,
            agent=self.agent,
            workspace_root=self.workspace_root,
            session_mode=self.session_mode,
            base_url=self.base_url,
            uds_path=self.uds_path,
        )
