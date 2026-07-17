"""TUI adapter over the typed XBot HTTP client."""

from __future__ import annotations

from typing import Any, AsyncIterator
from urllib.parse import quote

from xbotv2.client import XBotClient
from xbotv2.protocol.models import (
    CommandListResponse,
    CommandRequest,
    CommandResponse,
    ServerEvent,
)
from xbotv2.tui.trace import trace_event


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
        payload = (
            await self._client.hello(
                client_name="xbotv2-tui",
                session_id=session_id,
                thread_id=thread_id,
            )
        ).model_dump()
        _trace_response("hello", payload)
        return payload

    async def open_session(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        mode: str = "new",
        agent: str | None = None,
    ) -> dict[str, Any]:
        payload = (
            await self._client.open_session(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                mode=mode,
                agent=agent,
            )
        ).model_dump()
        _trace_response("open_session", payload)
        return payload

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
    ) -> AsyncIterator[dict[str, Any]]:
        return self._trace_events(
            self._client.send_message(
                session_id,
                thread_id,
                content,
                request_id=request_id,
            ),
            trace_label="messages",
            path=f"{_thread_path(session_id, thread_id)}/messages",
            body={"content": content, "request_id": request_id},
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
        payload = (
            await self._client.respond_permission(
                session_id,
                thread_id,
                request_id=request_id,
                decision=decision,
                scope=scope,
            )
        ).model_dump()
        _trace_response("permission_response", payload)
        return payload

    async def send_user_input(
        self,
        *,
        session_id: str,
        thread_id: str,
        request_id: str,
        answer: Any,
    ) -> dict[str, Any]:
        payload = (
            await self._client.respond_user_input(
                session_id,
                thread_id,
                request_id=request_id,
                answer=answer,
            )
        ).model_dump()
        _trace_response("user_input", payload)
        return payload

    async def shutdown(self, *, session_id: str) -> dict[str, Any]:
        return (await self._client.close_session(session_id)).model_dump()

    async def interrupt(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        payload = (
            await self._client.interrupt(session_id, thread_id)
        ).model_dump()
        _trace_response("interrupt", payload)
        return payload

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


def _trace_response(stage: str, payload: dict[str, Any]) -> None:
    trace_event("tui.http", {"stage": stage, "status": 200, "payload": payload})
