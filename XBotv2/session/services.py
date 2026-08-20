"""Public service Protocol for one active session."""

from __future__ import annotations

from typing import Protocol

from XBotv2.session.contracts import SessionStatus


class SessionPort(Protocol):
    session_id: str
    thread_id: str
    workspace_root: str

    @property
    def provider(self) -> str: ...

    def new_thread_id(self, owner: str) -> str: ...

    def status(self) -> SessionStatus: ...

    async def fork(self) -> str: ...

    async def clear_history(self) -> int: ...

    async def undo_history(self, count: int) -> list[object]: ...


__all__ = ["SessionPort"]
