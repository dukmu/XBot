"""Client transport contract tests."""

import json

import httpx
import pytest

from XBotv2.client import XBotClient, XBotClientError, uses_proxy_environment


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
async def test_an_evicted_cursor_rebuilds_the_baseline_over_the_real_client():
    """After a cursor eviction the client re-opens the session and resumes there.

    Driven through the real ``XBotClient`` (with a mock HTTP transport) and the
    real transport, so the error mapping, the resume request, and the adopted
    cursor are all exercised together.
    """
    import asyncio

    from XBotv2.protocol import ErrorResponse
    from XBotv2.session.protocol import OpenSessionResponse
    from XBotv2.tui.controller import TuiController
    from XBotv2.tui.transport import TransportConfig, TransportSession

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
    expired = {
        "code": "session_event_cursor_expired",
        "message": "cursor evicted",
        "details": {"oldest_sequence": 90},
    }
    streams = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        requests.append(request)
        if request.url.path.endswith("/events"):
            streams += 1
            if streams == 1:
                return httpx.Response(409, json=expired)
            return httpx.Response(200, content=b"")
        if request.url.path == "/hello":
            return httpx.Response(200, json={"server_name": "x", "session_id": "s", "thread_id": "t"})
        if request.url.path == "/sessions":
            return httpx.Response(200, json=opened)
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"session_id": "s", "threads": [
                {"session_id": "s", "thread_id": "t", "status": "active", "turn_status": "idle"}
            ]})
        return httpx.Response(404, json={"code": "not_found", "message": str(request.url)})

    client = XBotClient(base_url="http://xbot.test", transport=httpx.MockTransport(handler))
    seen: list[object] = []
    session = TransportSession(
        client,
        config=TransportConfig(
            session_id="s",
            thread_id="t",
            reconnect_delays=(),
            cursor_recoveries=0,
            baseline_rebuilds=1,
        ),
        emit=seen.append,
        sleep=_no_sleep,
    )
    try:
        await session.connect()
        await session.run()
    finally:
        await client.close()

    from XBotv2.tui.events import SnapshotAdopted

    adopted = [event.snapshot for event in seen if isinstance(event, SnapshotAdopted)]
    assert adopted, "the baseline must be rebuilt from a fresh open-session"
    assert isinstance(adopted[-1], OpenSessionResponse)
    assert adopted[-1].event_cursor == 42
    assert adopted[-1].pending_interactions[0].data["request_id"] == "permission:call-1"
    assert session.cursor == 42, "the adopted cursor is the point the stream resumes from"
    resume = [r for r in requests if json.loads(r.content or b"{}").get("mode") == "resume"]
    assert resume, "the rebuild re-opens the session in resume mode"
    event_streams = [r for r in requests if r.url.path.endswith("/events")]
    assert event_streams[1].url.params.get("after") == "42", (
        "the resubscription resumes from the cursor the baseline adopted"
    )


async def _no_sleep(_seconds: float) -> None:
    return None


# --- the process proxy environment is not for local servers ----------------
#
# A client whose target is this machine must not be routed by HTTP_PROXY, and
# one variable in particular used to make construction itself raise: httpx
# parses every NO_PROXY entry as a URL, and `[::1]` is not one (the port comes
# out as ":1]"). Local deployments -- which is every `xbot tui` / `xbot web`
# launch -- therefore could not even build a client on such a machine.


def test_loopback_targets_do_not_consult_the_proxy_environment() -> None:
    assert uses_proxy_environment("http://127.0.0.1:4096") is False
    assert uses_proxy_environment("http://localhost:4096") is False
    assert uses_proxy_environment("http://[::1]:4096") is False


def test_a_remote_target_still_honours_the_proxy_environment() -> None:
    assert uses_proxy_environment("https://xbot.example.com") is True


def test_a_unix_socket_does_not_consult_the_proxy_environment() -> None:
    assert uses_proxy_environment("http://127.0.0.1", uds_path="/tmp/x.sock") is False


def test_a_loopback_client_is_built_on_a_machine_with_a_hostile_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    client = XBotClient("http://127.0.0.1:9")
    assert client is not None, "constructing a local client must never raise"


def test_a_client_may_be_told_to_ignore_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "[::1]")
    client = XBotClient("https://xbot.example.com", trust_env=False)
    assert client is not None



# --- the command plane, as part of the typed SDK ---------------------------


@pytest.mark.asyncio
async def test_the_catalogue_is_a_typed_resource():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "commands": [
                    {
                        "name": "status",
                        "slash": "/status",
                        "kind": "server",
                        "description": "Show the current session and thread status",
                        "usage": "/status",
                        "effects": ["thread"],
                        "exclusive": True,
                    }
                ]
            },
        )

    client = XBotClient(base_url="http://xbot.test", transport=httpx.MockTransport(handler))
    try:
        response = await client.list_commands("s1", "agent")
    finally:
        await client.close()

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/sessions/s1/threads/agent/commands"
    assert [item.name for item in response.commands] == ["status"]
    assert response.commands[0].effects == ("thread",), "what it touches, before running it"


@pytest.mark.asyncio
async def test_running_a_command_sends_the_line_the_user_typed():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "type": "command_result",
                "data": {
                    "command": "status",
                    "status": "ok",
                    "message": "session s1 · idle",
                    "effects": [],
                },
            },
        )

    client = XBotClient(base_url="http://xbot.test", transport=httpx.MockTransport(handler))
    try:
        response = await client.run_command("s1", "agent", raw="/status verbose")
    finally:
        await client.close()

    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"raw": "/status verbose"}, (
        "the server resolves the line, not the client"
    )
    assert response.data.message == "session s1 · idle"
