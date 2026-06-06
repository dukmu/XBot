"""Shared TUI session configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xbotv2.tui.terminal import TerminalSession


@dataclass(frozen=True)
class TuiSessionConfig:
    data_dir: Path | str = "data"
    personality_id: str = "default"
    provider_name: str = "default"
    session_id: str | None = None
    thread_id: str = "agent"
    no_plugins: bool = False
    base_url: str = "http://127.0.0.1:4096"

    def create_terminal_session(self) -> TerminalSession:
        return TerminalSession(
            data_dir=self.data_dir,
            personality_id=self.personality_id,
            provider_name=self.provider_name,
            session_id=self.session_id,
            thread_id=self.thread_id,
            no_plugins=self.no_plugins,
            base_url=self.base_url,
        )
