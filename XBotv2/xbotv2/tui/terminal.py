"""Session facade for the TUI over a ``Transport``.

The TUI calls ``TerminalSession`` for all server interaction. v1 ships
only ``HttpTransport``; the transport can be injected for testing.

This module replaces the historical stdio ``ProtocolClient`` with a
``Transport``-based implementation. The stdio path is removed in v1
per the design document §10.5.2.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from xbotv2.tui.transport import Transport
from xbotv2.tui.transport_http import HttpTransport


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class TerminalSession:
    """High-level session over a ``Transport``.

    Lifecycle::

        session = TerminalSession(base_url="http://127.0.0.1:4096")
        await session.connect()
        async for event in session.send_message("hi"):
            ...
        await session.disconnect()
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        thread_id: str = "agent",
        agent: str | None = None,
        workspace_root: Path | str | None = None,
        session_mode: str | None = None,
        base_url: str = "http://127.0.0.1:4096",
        transport: Transport | None = None,
        token: str | None = None,
        uds_path: str | None = None,
    ) -> None:
        self._session_id = session_id or _new_session_id()
        self._session_mode = session_mode or "new"
        self._thread_id = thread_id
        self._agent = agent
        self._workspace_root = str(Path(workspace_root or Path.cwd()).resolve())
        self._transport: Transport = transport or HttpTransport(base_url, token=token, uds_path=uds_path)
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

    async def connect(self) -> dict[str, Any] | None:
        """Perform hello + open_session."""

        if self._connected:
            return None
        hello = await self._transport.hello(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        server_session = str(hello.get("session_id") or self._session_id)
        server_thread = str(hello.get("thread_id") or self._thread_id)
        self._session_id = server_session
        self._thread_id = server_thread
        open_kwargs = dict(
            session_id=self._session_id,
            thread_id=self._thread_id,
            workspace_root=self._workspace_root,
            mode=self._session_mode,
        )
        if self._agent:
            open_kwargs["agent"] = self._agent
        session = await self._transport.open_session(**open_kwargs)
        self._connected = True
        return session

    async def list_commands(self) -> dict[str, Any]:
        return await self._transport.list_commands(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )

    async def run_command(self, command: str, args: list[str], raw: str, *, kind: str = "server") -> dict[str, Any]:
        return await self._transport.run_command(
            session_id=self._session_id,
            thread_id=self._thread_id,
            command=command,
            args=args,
            raw=raw,
            kind=kind,
        )

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

    async def send_message(
        self,
        content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send one user message and yield every non-transport SSE event."""

        request_id = f"tui-{self._session_id}-{secrets.token_hex(8)}"
        stream = self._transport.send_message(
            session_id=self._session_id,
            thread_id=self._thread_id,
            content=content,
            request_id=request_id,
        )
        async for event in stream:
            event_type = str(event.get("type") or "")
            if event_type == "end":
                return
            yield event

    async def session_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield turns initiated by runtime general messages."""

        async for event in self._transport.session_events(
            session_id=self._session_id,
            thread_id=self._thread_id,
        ):
            if str(event.get("type") or "") != "end":
                yield event

    async def submit_user_input(self, request_id: str, answer: Any) -> dict[str, Any]:
        return await self._transport.send_user_input(
            session_id=self._session_id,
            thread_id=self._thread_id,
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
            thread_id=self._thread_id,
            request_id=request_id,
            decision=decision,
            scope=scope,
        )
