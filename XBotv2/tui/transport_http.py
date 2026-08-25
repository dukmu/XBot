"""TUI adapter over the typed XBot HTTP client."""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable
from urllib.parse import quote

from XBotv2.client import XBotClient
from XBotv2.commands import (
    CommandListResponse,
    CommandRequest,
    CommandResponse,
)
from XBotv2.protocol import ServerEvent, WireModel
from XBotv2.tui.trace import trace_event


class HttpTransport:
    """Adapt typed client models to the dict-based TUI transport contract."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        uds_path: str | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = XBotClient(
            base_url,
            timeout=timeout,
            uds_path=uds_path,
            headers=headers,
        )

    async def hello(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        return await _response(
            "hello",
            self._client.hello(
                client_name="xbotv2-tui",
                session_id=session_id,
                thread_id=thread_id,
            ),
        )

    async def open_session(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str | None,
        mode: str = "new",
        agent: str | None = None,
    ) -> dict[str, Any]:
        return await _response(
            "open_session",
            self._client.open_session(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                mode=mode,
                agent=agent,
            ),
        )

    async def list_sessions(self) -> dict[str, Any]:
        return await _response("list_sessions", self._client.list_sessions())

    async def list_threads(self, *, session_id: str) -> dict[str, Any]:
        return await _response("list_threads", self._client.list_threads(session_id))

    async def list_commands(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        # Human command compatibility is intentionally outside the public SDK.
        result = await self._client._request(
            "GET",
            f"{_thread_path(session_id, thread_id)}/commands",
            CommandListResponse,
        )
        return result.model_dump()

    async def run_command(
        self,
        *,
        session_id: str,
        thread_id: str,
        command: str,
        args: list[str],
        raw: str,
        kind: str = "server",
    ) -> dict[str, Any]:
        result = await self._client._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/commands",
            CommandResponse,
            CommandRequest(
                command=command,
                args=args,
                raw=raw,
                kind=kind,
            ),
        )
        return result.model_dump()

    def send_message(
        self,
        *,
        session_id: str,
        thread_id: str,
        content: str,
        request_id: str,
        images: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._trace_events(
            self._client.send_message(
                session_id,
                thread_id,
                content,
                request_id=request_id,
                images=images,
            ),
            trace_label="messages",
            path=f"{_thread_path(session_id, thread_id)}/messages",
            body={
                "content": content,
                "request_id": request_id,
                "image_count": len(images or []),
            },
        )

    def session_events(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        return self._trace_events(
            self._client.stream_events(session_id, thread_id),
            trace_label="session_events",
            path=f"{_thread_path(session_id, thread_id)}/events",
        )

    async def send_permission_response(
        self,
        *,
        session_id: str,
        thread_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> dict[str, Any]:
        return await _response(
            "permission_response",
            self._client.respond_permission(
                session_id,
                thread_id,
                request_id=request_id,
                decision=decision,
                scope=scope,
            ),
        )

    async def send_user_input(
        self,
        *,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: Any,
    ) -> dict[str, Any]:
        return await _response(
            "user_input",
            self._client.respond_user_input(
                session_id,
                thread_id,
                request_id=request_id,
                answer=answer,
            ),
        )

    async def interrupt(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        return await _response(
            "interrupt", self._client.interrupt(session_id, thread_id)
        )

    async def close(self) -> None:
        await self._client.close()

    async def _trace_events(
        self,
        events: AsyncIterator[ServerEvent],
        *,
        trace_label: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        trace_event(
            "tui.http",
            {"stage": f"{trace_label}.request", "url": path, "body": body},
        )
        async for event in events:
            trace_event(
                "tui.http",
                {
                    "stage": f"{trace_label}.event",
                    "event": event.type,
                    "id": event.sequence,
                    "event_type": event.type,
                },
            )
            yield event.model_dump()


def _thread_path(session_id: str, thread_id: str) -> str:
    return (
        f"/sessions/{quote(session_id, safe='')}/threads/"
        f"{quote(thread_id, safe='')}"
    )


async def _response(
    stage: str, request: Awaitable[WireModel]
) -> dict[str, Any]:
    payload = (await request).model_dump()
    trace_event("tui.http", {"stage": stage, "status": 200, "payload": payload})
    return payload
