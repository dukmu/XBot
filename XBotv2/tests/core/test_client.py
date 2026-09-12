"""Client transport contract tests."""

import httpx
import pytest

from XBotv2.client import XBotClient, XBotClientError


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
