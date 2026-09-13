"""Client transport contract tests."""

import json

import httpx
import pytest

from XBotv2.client import XBotClient, XBotClientError
from XBotv2.tui.terminal import TerminalSession


@pytest.mark.asyncio
async def test_streaming_error_response_is_read_before_it_is_parsed():
    """An SSE endpoint that answers non-2xx must still yield its error payload.

    A streaming response has not read its body, so parsing it directly used to
    raise ``httpx.ResponseNotRead`` and hide the real status and code.
    """
    body = (
        b'{"code": "session_event_cursor_expired", "message": "cursor expired",'
        b' "details": {"oldest_sequence": 7}, "retryable": true}'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, stream=httpx.ByteStream(body))

    client = XBotClient(
        base_url="http://xbot.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(XBotClientError) as raised:
            async for _event in client.stream_events("s", "t", after=3):
                pass
    finally:
        await client.close()

    assert raised.value.status_code == 409
    assert raised.value.code == "session_event_cursor_expired"
    assert raised.value.details == {"oldest_sequence": 7}
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_streaming_error_without_a_json_body_keeps_the_status():
    """A non-JSON error body still reports the status instead of a read error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, stream=httpx.ByteStream(b"upstream gone"))

    client = XBotClient(
        base_url="http://xbot.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(XBotClientError) as raised:
            async for _event in client.stream_events("s", "t"):
                pass
    finally:
        await client.close()

    assert raised.value.status_code == 502
    assert raised.value.code == "502"
    assert "upstream gone" in raised.value.message


@pytest.mark.asyncio
async def test_refresh_baseline_resumes_the_session_and_adopts_the_cursor():
    """The recovery point after a cursor eviction is a fresh open-session."""

    requests: list[httpx.Request] = []
    opened = {
        "session_id": "s",
        "thread_id": "t",
        "status": "ready",
        "agent_name": "default",
        "workspace_root": "/workspace",
        "provider": "minimax",
        "model": "MiniMax-M2",
        "model_mode": "",
        "context_window": 1000,
        "usage": {},
        "session_stats": {},
        "history": [],
        "event_cursor": 42,
        "status_slots": {},
        "pending_inputs": [],
        "pending_interactions": [
            {
                "type": "permission_request",
                "data": {
                    "request_id": "permission:call-1",
                    "source": "permission_system",
                    "reason": "write the report",
                    "tool_call": {"id": "call-1", "name": "filesystem_write", "args": {}},
                },
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=opened)

    client = XBotClient(
        base_url="http://xbot.test",
        transport=httpx.MockTransport(handler),
    )
    session = TerminalSession(session_id="s", thread_id="t", client=client)
    session._session_attached = True
    try:
        snapshot = await session.refresh_baseline()
    finally:
        await client.close()

    assert snapshot is not None
    assert snapshot["event_cursor"] == 42
    assert snapshot["pending_interactions"] == opened["pending_interactions"]
    body = json.loads(requests[0].content)
    assert body["mode"] == "resume"
    assert body["session_id"] == "s"
    assert session._event_cursor == 42
