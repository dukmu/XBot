"""Adapt the Textual app to the generic async terminal-client contract."""

from __future__ import annotations

import asyncio

from XBotv2.application.client import ClientLaunch
from XBotv2.commands import CommandsPort
from XBotv2.tui.app import TuiApp
from XBotv2.tui.config import TextualTuiConfig
from XBotv2.tui.transport import (
    DEFAULT_HISTORY_RETENTION,
    DEFAULT_HISTORY_WINDOW,
    SessionBackend,
    TransportConfig,
)


class TextualTerminalClient:
    def __init__(
        self,
        *,
        backend: SessionBackend,
        commands: CommandsPort,
        launch: ClientLaunch,
        config: TextualTuiConfig,
    ) -> None:
        session_id = launch.session_id
        self.app = TuiApp(
            backend=backend,
            commands=commands,
            config=TransportConfig(
                session_id=session_id or "",
                thread_id=launch.thread_id,
                agent=launch.agent,
                history_window=(
                    launch.history_window
                    if launch.history_window is not None
                    else DEFAULT_HISTORY_WINDOW
                ),
                history_retention=(
                    launch.history_retention
                    if launch.history_retention is not None
                    else DEFAULT_HISTORY_RETENTION
                ),
                workspace_root=launch.workspace,
                mode="resume" if session_id else "new",
            ),
            workspace=launch.workspace,
            transcript_limit=config.transcript_limit,
            render_interval=config.render_interval,
        )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        task_factory = loop.get_task_factory()
        try:
            await self.app.run_async()
        finally:
            # Textual 8.2 sets eager_task_factory on the running loop and leaves
            # it there. The plugin host must not inherit that global mutation.
            loop.set_task_factory(task_factory)

    def request_stop(self, reason: str) -> None:
        self.app.exit(message=reason)


__all__ = ["TextualTerminalClient"]
