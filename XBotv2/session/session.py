"""Session identity, paths, and history mutations."""

from __future__ import annotations

import secrets
import shutil
from typing import Any, Protocol

from XBotv2.agentloop import LoopState
from XBotv2.core.errors import OperationError
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths, SessionPaths
from XBotv2.session.contracts import (
    HISTORY_CHANGED,
    PREPARE_FORK,
    HistoryChanged,
    PrepareFork,
    SessionStatus,
)
from XBotv2.session.types import SessionInfo


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


def delete_persisted_session(paths: RuntimePaths, session_id: str) -> None:
    """Permanently remove one validated persisted session tree."""
    root = paths.session(session_id).root
    if root.is_symlink():
        raise OperationError(
            "invalid_session_storage",
            f"Cannot delete symlinked session storage: {session_id}",
        )
    shutil.rmtree(root)


def _new_fork_id() -> str:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class SessionEventsPort(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...


class Session:
    """One active session's identity, path allocation, and history surface."""

    def __init__(
        self,
        *,
        events: SessionEventsPort,
        info: SessionInfo,
        paths: RuntimePaths,
        variables: Any,
        state: LoopState,
        session_paths: SessionPaths,
    ) -> None:
        self._events = events
        self.info = info
        self.paths = paths
        self.variables = variables
        self.state = state
        self.session_paths = session_paths

    @property
    def session_id(self) -> str:
        return self.info.session_id

    @property
    def thread_id(self) -> str:
        return self.info.thread_id

    @property
    def workspace_root(self) -> str:
        return self.info.workspace_root

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
        await self._events.emit(
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

    async def regenerate_history(self) -> Message:
        """Remove the latest human-authored turn and return its input."""
        history = self.state.history
        index = next(
            (
                position
                for position in range(len(history) - 1, -1, -1)
                if history[position].role == "user"
                and "runtime_input" not in history[position].additional_kwargs
            ),
            None,
        )
        if index is None:
            raise OperationError(
                "nothing_to_regenerate",
                "History has no human-authored turn to regenerate.",
            )
        message = history[index]
        history.replace(history[:index])
        self.state._update_turn_count()
        await self._announce_history_change("regenerate", 1)
        return message

    async def _announce_history_change(
        self,
        operation: str,
        turns: int = 0,
    ) -> None:
        await self._events.emit(
            HISTORY_CHANGED,
            HistoryChanged(self.state.history.snapshot(), operation, turns),
        )

    def new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["Session"]
