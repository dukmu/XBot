"""Session identity, paths, and history mutations."""

from __future__ import annotations

import secrets
import shutil
from typing import Any

from XBotv2.core.errors import OperationError
from XBotv2.core.messages import Message
from XBotv2.core.paths import SessionPaths
from XBotv2.session.contracts import (
    HISTORY_CHANGED,
    PREPARE_FORK,
    HistoryChanged,
    PrepareFork,
    SessionStatus,
)


def fork_persisted_session(paths: Any, source_session_id: str) -> str:
    """Copy one persisted session to a fresh session id."""
    session_id = _new_fork_id()
    while paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = paths.session(source_session_id).root
    target = paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return session_id


def _new_fork_id() -> str:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class Session:
    """One active session's identity, path allocation, and history surface."""

    def __init__(
        self,
        *,
        ctx: Any = None,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        paths: Any,
        variables: Any,
        state: Any,
        session_paths: SessionPaths,
    ) -> None:
        self.ctx = ctx
        self.session_id = session_id
        self.thread_id = thread_id
        self.workspace_root = workspace_root
        self.paths = paths
        self.variables = variables
        self.state = state
        self.session_paths = session_paths

    # -- session identity (SessionInfo-compatible surface) ------------------

    @property
    def provider(self) -> str:
        return self.state.session.provider

    def status(self) -> SessionStatus:
        return SessionStatus(
            session_id=self.session_id,
            thread_id=self.thread_id,
            provider=self.state.metadata.value.provider or self.provider,
            model=self.state.metadata.value.model,
        )

    async def fork(self) -> str:
        await self.ctx.emit(
            PREPARE_FORK,
            PrepareFork(self.session_id, self.thread_id),
        )
        return fork_persisted_session(self.paths, self.session_id)

    # -- history mutations --------------------------------------------------

    async def clear_history(self) -> int:
        """Remove every user turn; caller owns idle-check and turn lock."""
        history = self.state.history
        removed = sum(message.role == "user" for message in history)
        history.clear()
        self.state._update_turn_count()
        await self._announce_history_change("clear")
        return removed

    async def undo_history(self, count: int) -> list[Message]:
        """Undo complete user turns; caller owns idle-check and turn lock."""
        try:
            messages = self.state.history.undo(count)
        except ValueError as exc:
            raise OperationError(
                "invalid_undo_count",
                str(exc),
            ) from exc
        self.state._update_turn_count()
        await self._announce_history_change("undo", count)
        return list(messages)

    async def _announce_history_change(
        self,
        operation: str,
        turns: int = 0,
    ) -> None:
        await self.ctx.emit(
            HISTORY_CHANGED,
            HistoryChanged(self.state.history.snapshot(), operation, turns),
        )

    def new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["Session"]
