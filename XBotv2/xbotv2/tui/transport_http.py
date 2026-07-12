"""HTTP/SSE transport for the TUI.

Implementation of the ``Transport`` protocol on top of FastAPI (server)
and ``httpx.AsyncClient`` (client). v1 only supports loopback binds
(``127.0.0.1``); remote binds are rejected upstream in the server.

See ``docsv2/tui_opencode_requirements.md`` §10.5.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from xbotv2.protocol.version import PROTOCOL_VERSION
from xbotv2.protocol.models import (
    CommandListResponse,
    CommandRequest,
    CommandResponse,
    HelloRequest,
    HelloResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    PermissionResponseRequest,
    ServerEvent,
    UserInputResponseRequest,
)
from xbotv2.protocol.sse import SseDecoder, SseMessage, decode_server_event

from xbotv2.tui.trace import trace_event


class HttpTransport:
    """Async HTTP/SSE transport for the TUI."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        uds_path: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        transport = httpx.AsyncHTTPTransport(uds=uds_path) if uds_path else None
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._closed = False

    async def hello(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/hello",
            json=HelloRequest(
                client_name="xbotv2-tui",
                protocol_version=PROTOCOL_VERSION,
                session_id=session_id,
                thread_id=thread_id,
            ).model_dump(),
        )
        _raise_for_status(response)
        payload = HelloResponse.model_validate(response.json()).model_dump()
        trace_event(
            "tui.http",
            {"stage": "hello", "status": response.status_code, "payload": payload},
        )
        return payload

    async def open_session(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        mode: str = "new",
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/sessions",
            json=OpenSessionRequest(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                mode=mode,
            ).model_dump(),
        )
        _raise_for_status(response)
        payload = OpenSessionResponse.model_validate(response.json()).model_dump()
        trace_event(
            "tui.http",
            {"stage": "open_session", "status": response.status_code, "payload": payload},
        )
        return payload

    async def list_commands(self, session_id: str | None = None) -> dict[str, Any]:
        url = f"/sessions/{session_id}/commands" if session_id else "/commands"
        response = await self._client.get(url)
        _raise_for_status(response)
        return CommandListResponse.model_validate(response.json()).model_dump()

    async def run_command(
        self, *, session_id: str, command: str, args: list[str], raw: str,
        kind: str = "server",
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"/sessions/{session_id}/commands",
            json=CommandRequest(
                command=command,
                args=args,
                raw=raw,
                kind=kind,
            ).model_dump(),
        )
        _raise_for_status(response)
        return CommandResponse.model_validate(response.json()).model_dump()

    def send_message(
        self,
        *,
        session_id: str,
        content: str,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._sse_iter(
            f"/sessions/{session_id}/messages",
            json_body=MessageRequest(
                content=content,
                request_id=request_id,
            ).model_dump(),
            trace_label="messages",
        )

    async def send_permission_response(
        self,
        *,
        session_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"/sessions/{session_id}/interactions/permission-response",
            json=PermissionResponseRequest(
                request_id=request_id,
                decision=decision,
                scope=scope,
            ).model_dump(),
        )
        _raise_for_status(response)
        payload = response.json()
        trace_event(
            "tui.http",
            {
                "stage": "permission_response",
                "status": response.status_code,
                "payload": payload,
            },
        )
        return payload

    async def send_user_input(
        self,
        *,
        session_id: str,
        request_id: str,
        answer: Any,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"/sessions/{session_id}/interactions/user-input",
            json=UserInputResponseRequest(
                request_id=request_id,
                answer=answer,
            ).model_dump(),
        )
        _raise_for_status(response)
        payload = response.json()
        trace_event(
            "tui.http",
            {"stage": "user_input", "status": response.status_code, "payload": payload},
        )
        return payload

    async def shutdown(self, *, session_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/sessions/{session_id}/shutdown")
        _raise_for_status(response)
        return response.json()

    async def interrupt(self, *, session_id: str) -> dict[str, Any]:
        response = await self._client.post(
            f"/sessions/{session_id}/interrupt"
        )
        _raise_for_status(response)
        payload = response.json()
        trace_event(
            "tui.http",
            {"stage": "interrupt", "status": response.status_code, "payload": payload},
        )
        return payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def _sse_iter(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        trace_label: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate one SSE stream from the server.

        Yields one dict per event in the order they arrive. The iterator
        ends cleanly when the server sends an ``end`` event or closes
        the connection.
        """

        trace_event(
            "tui.http",
            {"stage": f"{trace_label}.request", "url": path, "body": json_body},
        )
        async with self._client.stream(
            "POST",
            path,
            json=json_body,
            timeout=httpx.Timeout(self._timeout, read=None),
        ) as response:
            _raise_for_status(response)
            decoder = SseDecoder()
            async for raw_line in response.aiter_lines():
                message = decoder.feed(raw_line)
                if message is None:
                    continue
                event = decode_server_event(message)
                _trace_sse_event(trace_label, message, event)
                yield event.model_dump()
                if event.type == "end":
                    return

            message = decoder.finish()
            if message is not None:
                event = decode_server_event(message)
                _trace_sse_event(trace_label, message, event)
                yield event.model_dump()


def _trace_sse_event(
    trace_label: str,
    message: SseMessage,
    event: ServerEvent,
) -> None:
    trace_event(
        "tui.http",
        {
            "stage": f"{trace_label}.event",
            "event": message.event,
            "id": message.event_id,
            "event_type": event.type,
        },
    )


def _raise_for_status(response: httpx.Response) -> None:
    """Raise a readable protocol error for non-2xx HTTP responses."""

    is_success = getattr(response, "is_success", None)
    if is_success is None:
        response.raise_for_status()
        return
    if is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
    code = str(payload.get("code") or response.status_code)
    message = str(payload.get("message") or response.text or response.reason_phrase)
    raise RuntimeError(f"{code}: {message}")
