"""HTTP/SSE transport for the TUI.

Implementation of the ``Transport`` protocol on top of FastAPI (server)
and ``httpx.AsyncClient`` (client). v1 only supports loopback binds
(``127.0.0.1``); remote binds are rejected upstream in the server.

See ``docsv2/tui_opencode_requirements.md`` §10.5.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from xbotv2.tui.trace import trace_event


class HttpTransport:
    """Async HTTP/SSE transport for the TUI."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )
        self._closed = False

    async def hello(
        self,
        *,
        session_id: str,
        thread_id: str,
        personality_id: str = "default",
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/hello",
            json={
                "client_name": "xbotv2-tui",
                "session_id": session_id,
                "thread_id": thread_id,
                "personality_id": personality_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
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
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/sessions",
            json={"session_id": session_id, "thread_id": thread_id},
        )
        response.raise_for_status()
        payload = response.json()
        trace_event(
            "tui.http",
            {"stage": "open_session", "status": response.status_code, "payload": payload},
        )
        return payload

    def send_message(
        self,
        *,
        session_id: str,
        content: str,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._sse_iter(
            f"/sessions/{session_id}/messages",
            json_body={"content": content, "request_id": request_id},
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
            json={"request_id": request_id, "decision": decision, "scope": scope},
        )
        response.raise_for_status()
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
            json={"request_id": request_id, "answer": answer},
        )
        response.raise_for_status()
        payload = response.json()
        trace_event(
            "tui.http",
            {"stage": "user_input", "status": response.status_code, "payload": payload},
        )
        return payload

    async def shutdown(self, *, session_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/sessions/{session_id}/shutdown")
        response.raise_for_status()
        return response.json()

    async def interrupt(self, *, session_id: str) -> dict[str, Any]:
        response = await self._client.post(
            f"/sessions/{session_id}/interrupt"
        )
        response.raise_for_status()
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
        ) as response:
            response.raise_for_status()
            event_name: str | None = None
            data_lines: list[str] = []
            event_id: int | None = None
            async for raw_line in response.aiter_lines():
                # aiter_lines strips the trailing newline but keeps blank lines.
                if raw_line == "":
                    if not data_lines:
                        event_name = None
                        data_lines = []
                        event_id = None
                        continue
                    payload_text = "\n".join(data_lines)
                    try:
                        event = json.loads(payload_text)
                    except json.JSONDecodeError:
                        event = {
                            "type": "error",
                            "data": {
                                "code": "sse_decode_error",
                                "message": payload_text,
                            },
                        }
                    trace_event(
                        "tui.http",
                        {
                            "stage": f"{trace_label}.event",
                            "event": event_name,
                            "id": event_id,
                            "event_type": event.get("type"),
                        },
                    )
                    yield event
                    if event.get("type") == "end":
                        return
                    event_name = None
                    data_lines = []
                    event_id = None
                    continue
                if raw_line.startswith(":"):
                    # Comment / keep-alive
                    continue
                if ":" in raw_line:
                    field, _, value = raw_line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_name = value
                    elif field == "data":
                        data_lines.append(value)
                    elif field == "id":
                        try:
                            event_id = int(value)
                        except ValueError:
                            event_id = None
            # Server closed the stream without an explicit "end" event.
            if data_lines:
                payload_text = "\n".join(data_lines)
                try:
                    yield json.loads(payload_text)
                except json.JSONDecodeError:
                    pass
