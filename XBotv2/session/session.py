"""Session identity, paths, and history mutations."""

from __future__ import annotations

import secrets
import shutil
from typing import Any

from XBotv2.agentloop import EventContext, Events
from XBotv2.core.errors import OperationError
from XBotv2.core.paths import SessionPaths
from XBotv2.session.contracts import PREPARE_FORK, PrepareFork, SessionStatus


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
            provider=str(self.state.metadata.get("provider") or self.provider),
            model=str(self.state.metadata.get("model") or ""),
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
        removed = sum(
            message.role == "user"
            for message in self.ctx.engine.messages
        )
        await self._replace_history([], operation="clear")
        return removed

    async def undo_history(self, count: int) -> list[Any]:
        """Undo complete user turns; caller owns idle-check and turn lock."""
        messages = list(self.ctx.engine.messages)
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.role == "user"
        ]
        if count > len(user_indexes):
            raise OperationError(
                "invalid_undo_count",
                f"Cannot undo {count} turns; session has {len(user_indexes)}.",
            )
        kept = messages[:user_indexes[-count]]
        await self._replace_history(kept, operation="undo", turns=count)
        return kept

    async def _replace_history(
        self,
        messages: list[Any],
        *,
        operation: str,
        turns: int = 0,
    ) -> None:
        state = self.state
        state.replace_messages(messages)
        await self.ctx.emit(Events.STATE_CHANGED, EventContext(
            messages=state.messages,
            session=state.session,
            event={"history_operation": (operation, turns)},
        ))

    def new_thread_id(self, agent: str) -> str:
        while True:
            thread_id = f"{agent}-{secrets.token_hex(3)}"
            if not self.session_paths.has_thread(thread_id):
                return thread_id


__all__ = ["Session"]
