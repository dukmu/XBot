"""Public service Protocol for one active session."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, TypeVar

from XBotv2.core.messages import Message
from XBotv2.core.operations import Operation
from XBotv2.session.contracts import SessionStatus
from XBotv2.session.types import (
    HistoryMutation,
    InteractionReceipt,
    InterruptResult,
    OpenedSession,
    OpenSession,
    OpenThread,
    SendMessage,
    SessionStreamEvent,
    SessionSnapshot,
    ThreadSnapshot,
)

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


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

    async def undo_history(self, count: int) -> list[Message]: ...


class SessionsPort(Protocol):
    """Transport-neutral process API for persistent sessions and threads."""

    def session_exists(self, session_id: str) -> bool: ...

    async def open(self, request: OpenSession) -> OpenedSession: ...

    async def list_sessions(self) -> tuple[SessionSnapshot, ...]: ...

    async def session_summary(self, session_id: str) -> SessionSnapshot: ...

    async def fork_session(self, session_id: str) -> str: ...

    async def list_threads(self, session_id: str) -> tuple[ThreadSnapshot, ...]: ...

    async def open_thread(self, request: OpenThread) -> OpenedSession: ...

    async def thread_summary(
        self,
        session_id: str,
        thread_id: str,
    ) -> ThreadSnapshot: ...

    async def messages(self, session_id: str, thread_id: str) -> tuple[Message, ...]: ...

    async def clear_history(
        self,
        session_id: str,
        thread_id: str,
    ) -> HistoryMutation: ...

    async def undo_history(
        self,
        session_id: str,
        thread_id: str,
        count: int,
    ) -> HistoryMutation: ...

    async def stream_message(
        self,
        request: SendMessage,
    ) -> AsyncIterator[SessionStreamEvent]: ...

    async def stream_events(
        self,
        session_id: str,
        thread_id: str,
    ) -> AsyncIterator[SessionStreamEvent]: ...

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> InteractionReceipt: ...

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: Any,
    ) -> InteractionReceipt: ...

    async def cancel_interaction(
        self,
        session_id: str,
        thread_id: str,
        event_type: Literal["permission_request", "user_input_required"],
        request_id: str,
        reason: str,
    ) -> InteractionReceipt: ...

    async def close_session(self, session_id: str) -> None: ...

    async def close_thread(self, session_id: str, thread_id: str) -> None: ...

    async def interrupt(self, session_id: str, thread_id: str) -> InterruptResult: ...

    async def dispatch(
        self,
        session_id: str,
        thread_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> ResponseT: ...

    async def dispatch_all(
        self,
        session_id: str,
        operation: Operation[RequestT, ResponseT],
        request: RequestT,
    ) -> tuple[ResponseT, ...]: ...

__all__ = ["SessionPort", "SessionsPort"]
