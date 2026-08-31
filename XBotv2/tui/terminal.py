"""Session facade for the TUI over the public HTTP client."""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote

from XBotv2.client import XBotClient
from XBotv2.commands import CommandListResponse, CommandRequest, CommandResponse
from XBotv2.protocol import ServerEvent, WireModel
from XBotv2.session import SessionMode
from XBotv2.tui.trace import trace_event


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class TerminalSession:
    """High-level session over :class:`XBotClient`.

    Lifecycle::

        session = TerminalSession(base_url="http://127.0.0.1:4096")
        await session.connect()
        events = asyncio.create_task(consume(session.session_events()))
        await drain(session.send_message("hi"))
        await session.disconnect()

    ``session_events`` is the authoritative resumable event channel.
    ``send_message`` drains the compatibility POST stream and only exposes an
    immediate ``input_rejected`` control result to the TUI submitter.
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
        client: XBotClient | None = None,
        token: str | None = None,
        uds_path: str | None = None,
    ) -> None:
        self._session_id = session_id or _new_session_id()
        self._session_mode = session_mode or "new"
        self._thread_id = thread_id
        self._agent = agent
        self._workspace_root = (
            None
            if self._session_mode == "resume" and workspace_root is None
            else str(Path(workspace_root or Path.cwd()).resolve())
        )
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = client or XBotClient(
            base_url, uds_path=uds_path, headers=headers
        )
        self._session_attached = False
        self._event_cursor = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def client(self) -> XBotClient:
        return self._client

    async def connect(self) -> dict[str, Any] | None:
        """Perform hello + open_session."""

        if self._session_attached:
            return None
        hello = await self._client.hello(
            client_name="xbotv2-tui",
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        server_session = hello.session_id or self._session_id
        server_thread = hello.thread_id or self._thread_id
        open_kwargs = dict(
            session_id=server_session,
            thread_id=server_thread,
            workspace_root=self._workspace_root,
            mode=self._session_mode,
        )
        if self._agent:
            open_kwargs["agent"] = self._agent
        session = await self._client.open_session(**open_kwargs)
        self._session_id = server_session
        self._thread_id = server_thread
        self._session_attached = True
        self._event_cursor = session.event_cursor
        return _dump(session)

    async def list_commands(self) -> dict[str, Any]:
        return _dump(await self._client._request(
            "GET", f"{self._thread_path}/commands", CommandListResponse
        ))

    async def list_sessions(self) -> dict[str, Any]:
        return _dump(await self._client.list_sessions())

    async def list_threads(self, session_id: str | None = None) -> dict[str, Any]:
        return _dump(await self._client.list_threads(session_id or self._session_id))

    async def switch(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        workspace_root: str | None = None,
        mode: SessionMode = "resume",
    ) -> dict[str, Any] | None:
        """Attach to another session/thread without destroying the current runtime.

        The HTTP transport remains open so switching does not invalidate the
        client connection. Persisted sessions retain their recorded workspace
        when ``workspace_root`` is omitted; ``new`` sessions use the supplied
        workspace or the current working directory.
        """
        target_session = session_id or _new_session_id()
        target_workspace = (
            None
            if mode == "resume" and workspace_root is None
            else str(Path(workspace_root or Path.cwd()).resolve())
        )
        opened = await self._client.open_session(
            session_id=target_session,
            thread_id=thread_id,
            workspace_root=target_workspace,
            mode=mode,
            **({"agent": self._agent} if self._agent else {}),
        )
        self._session_id = target_session
        self._thread_id = thread_id
        self._workspace_root = target_workspace
        self._session_mode = mode
        self._session_attached = True
        self._event_cursor = opened.event_cursor
        return _dump(opened)

    async def run_command(
        self,
        command: str,
        args: list[str],
        raw: str,
        *,
        kind: Literal["server", "prompt"] = "server",
    ) -> dict[str, Any]:
        return _dump(await self._client._request(
            "POST",
            f"{self._thread_path}/commands",
            CommandResponse,
            CommandRequest(command=command, args=args, raw=raw, kind=kind),
        ))

    async def disconnect(self) -> None:
        """Detach this client and close its transport without destroying a session."""

        self._session_attached = False
        await self._client.close()

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    async def send_message(
        self,
        content: str,
        *,
        images: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Submit input while runtime events arrive through ``session_events``."""

        request_id = f"tui-{self._session_id}-{secrets.token_hex(8)}"
        request = {
            "session_id": self._session_id,
            "thread_id": self._thread_id,
            "content": content,
            "request_id": request_id,
        }
        if images:
            request["images"] = images
        stream = self._client.send_message(
            self._session_id,
            self._thread_id,
            content,
            request_id=request_id,
            images=images,
        )
        async for event in self._events(stream, "messages", request):
            if event.get("type") == "input_rejected":
                yield event

    async def session_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield turns initiated by runtime general messages."""

        stream = self._client.stream_events(
            self._session_id,
            self._thread_id,
            after=self._event_cursor,
        )
        async for event in self._events(stream, "session_events"):
            self._event_cursor = max(
                self._event_cursor,
                int(event.get("sequence") or 0),
            )
            yield event

    async def submit_user_input(self, request_id: str, answer: Any) -> dict[str, Any]:
        return _dump(
            await self._client.respond_user_input(
                self._session_id,
                self._thread_id,
                request_id=request_id,
                answer=answer,
            )
        )

    async def respond_permission(
        self,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        scope: Literal["once", "session"] = "once",
    ) -> dict[str, Any]:
        return _dump(
            await self._client.respond_permission(
                self._session_id,
                self._thread_id,
                request_id=request_id,
                decision=decision,
                scope=scope,
            )
        )

    async def interrupt(self) -> dict[str, Any]:
        return _dump(await self._client.interrupt(self._session_id, self._thread_id))

    @property
    def _thread_path(self) -> str:
        return (
            f"/sessions/{quote(self._session_id, safe='')}/threads/"
            f"{quote(self._thread_id, safe='')}"
        )

    async def _events(
        self,
        stream: AsyncIterator[ServerEvent],
        label: str,
        body: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        trace_event("tui.http", {"stage": f"{label}.request", "body": body})
        async for event in stream:
            trace_event(
                "tui.http",
                {
                    "stage": f"{label}.event",
                    "event": event.type,
                    "id": event.sequence,
                },
            )
            if event.type == "end":
                return
            yield event.model_dump()


def _dump(model: WireModel) -> dict[str, Any]:
    payload = model.model_dump()
    trace_event("tui.http", {"status": 200, "payload": payload})
    return payload
