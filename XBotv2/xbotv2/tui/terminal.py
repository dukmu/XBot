"""Session facade for the TUI over a ``Transport``.

The TUI calls ``TerminalSession`` for all server interaction. v1 ships
only ``HttpTransport``; the transport can be injected for testing.

This module replaces the historical stdio ``ProtocolClient`` with a
``Transport``-based implementation. The stdio path is removed in v1
per the design document §10.5.2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from xbotv2.tui.transport import Transport
from xbotv2.tui.transport_http import HttpTransport


# Aliases for the live-interaction provider callbacks. A provider may be
# either a coroutine function (called by the TUI's per-prompt queues) or
# a plain function that returns a value synchronously. The two helpers
# below let ``send_message`` accept both.
InputProvider = Callable[[dict[str, Any]], Awaitable[Any] | Any]
PermissionProvider = Callable[[dict[str, Any]], Awaitable[dict[str, str]] | dict[str, str]]


def _await_maybe(value: Awaitable[Any] | Any) -> Any:
    if hasattr(value, "__await__"):
        return value  # type: ignore[return-value]
    return value


class TerminalSession:
    """High-level session over a ``Transport``.

    Lifecycle::

        session = TerminalSession(base_url="http://127.0.0.1:4096")
        await session.connect()
        async for event in session.send_message(
            "hi", input_provider=..., permission_provider=...
        ):
            ...
        await session.disconnect()
    """

    def __init__(
        self,
        *,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
        session_id: str | None = None,
        thread_id: str = "agent",
        base_url: str = "http://127.0.0.1:4096",
        no_plugins: bool = False,
        transport: Transport | None = None,
        token: str | None = None,
    ) -> None:
        self._data_dir = str(data_dir)
        self._personality_id = personality_id
        self._provider_name = provider_name
        self._no_plugins = no_plugins
        self._session_id = session_id or "default"
        self._thread_id = thread_id
        self._transport: Transport = transport or HttpTransport(base_url, token=token)
        self._connected = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def transport(self) -> Transport:
        return self._transport

    async def connect(self) -> None:
        """Perform hello + open_session."""

        if self._connected:
            return
        hello = await self._transport.hello(
            session_id=self._session_id,
            thread_id=self._thread_id,
            personality_id=self._personality_id,
        )
        server_session = str(hello.get("session_id") or self._session_id)
        server_thread = str(hello.get("thread_id") or self._thread_id)
        self._session_id = server_session
        self._thread_id = server_thread
        await self._transport.open_session(
            session_id=self._session_id, thread_id=self._thread_id
        )
        self._connected = True

    async def disconnect(self) -> None:
        """Best-effort session shutdown + transport close."""

        if not self._connected:
            await self._transport.close()
            return
        try:
            await self._transport.shutdown(session_id=self._session_id)
        except Exception:
            pass
        self._connected = False
        await self._transport.close()

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    def send_message(
        self,
        content: str,
        *,
        input_provider: InputProvider | None = None,
        permission_provider: PermissionProvider | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send one user message and yield every server event.

        The yielded events are the SSE payloads shaped as
        ``{"type": str, "data": dict}``. Live ``permission_request`` and
        ``user_input_required`` events are intercepted: their payload is
        passed to the matching provider, the provider's response is sent
        back to the server via the transport, and the recording events
        (``permission_response_recorded`` / ``user_input_recorded``) are
        also yielded as they arrive.
        """

        return self._send_message_impl(content, input_provider, permission_provider)

    async def _send_message_impl(
        self,
        content: str,
        input_provider: InputProvider | None,
        permission_provider: PermissionProvider | None,
    ) -> AsyncIterator[dict[str, Any]]:
        request_id = f"tui-{self._session_id}-{id(self)}"
        stream = self._transport.send_message(
            session_id=self._session_id,
            content=content,
            request_id=request_id,
        )
        async for event in stream:
            event_type = str(event.get("type") or "")

            # **Order matters**: yield the event to the TUI FIRST so
            # ``apply_event`` + ``_handle_stream_event`` can update
            # the status bar and transcript BEFORE we block on the
            # user's response.  Otherwise the UI stays frozen on
            # "Running" while the tools sit in "pending" forever.
            if event_type == "permission_request":
                yield event
                if permission_provider is not None:
                    payload = event.get("data") or {}
                    response = _await_maybe(permission_provider(payload))
                    if hasattr(response, "__await__"):
                        response = await response  # type: ignore[unreachable]
                    decision = str(response.get("decision") or "deny")
                    scope = str(response.get("scope") or "once")
                    await self._transport.send_permission_response(
                        session_id=self._session_id,
                        request_id=str(payload.get("request_id") or ""),
                        decision=decision,
                        scope=scope,
                    )
                continue
            if event_type == "user_input_required":
                yield event
                if input_provider is not None:
                    payload = event.get("data") or {}
                    answer = _await_maybe(input_provider(payload))
                    if hasattr(answer, "__await__"):
                        answer = await answer  # type: ignore[unreachable]
                    await self._transport.send_user_input(
                        session_id=self._session_id,
                        request_id=str(payload.get("request_id") or ""),
                        answer=answer,
                    )
                continue

            yield event

    async def submit_user_input(self, request_id: str, answer: Any) -> dict[str, Any]:
        return await self._transport.send_user_input(
            session_id=self._session_id,
            request_id=request_id,
            answer=answer,
        )

    async def respond_permission(
        self,
        request_id: str,
        decision: str,
        *,
        scope: str = "once",
    ) -> dict[str, Any]:
        return await self._transport.send_permission_response(
            session_id=self._session_id,
            request_id=request_id,
            decision=decision,
            scope=scope,
        )
