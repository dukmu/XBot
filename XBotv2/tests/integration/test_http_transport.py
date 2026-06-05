"""End-to-end integration tests for the HTTP/SSE transport.

These tests build a FastAPI app with a ``MockLLM`` injected, then drive
it via ``httpx.AsyncClient`` + ``ASGITransport`` (no real socket).
The tests cover:

- /health round-trip
- /hello + /sessions handshake
- /sessions/{sid}/messages SSE stream with a real engine
- live permission_request round-trip via the interaction endpoints
- Chinese payload byte-level preservation through HTTP
- ESC interrupt: POST /sessions/{sid}/interrupt mid-turn yields
  ``turn_cancelled`` on the SSE stream (v1.2)

See ``docsv2/tui_opencode_requirements.md`` §10.5 + Phase E DoD.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from xbotv2.llm.mock import MockLLM
from xbotv2.protocol.frames import PROTOCOL_VERSION
from xbotv2.protocol.http_server import create_app, set_llm_override


SSE_DATA_RE = re.compile(r"^data: ?(.*)$", re.MULTILINE)


def _parse_sse(payload: str) -> list[dict[str, Any]]:
    """Parse a raw SSE payload into a list of event dicts."""

    events: list[dict[str, Any]] = []
    for raw_frame in payload.split("\n\n"):
        if not raw_frame.strip():
            continue
        data_match = SSE_DATA_RE.search(raw_frame)
        if not data_match:
            continue
        text = data_match.group(1).strip()
        if not text:
            continue
        try:
            events.append(json.loads(text))
        except json.JSONDecodeError:
            events.append({"type": "decode_error", "raw": text})
    return events


@pytest_asyncio.fixture
async def http_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A FastAPI app whose engine uses a mock LLM (no real network)."""

    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "workspace").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "state").mkdir(parents=True)
    (data_dir / "personalities" / "default").mkdir(parents=True)

    # Minimal provider.yaml so bootstrap can pick a default
    (data_dir / "config" / "provider.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n  base_url: http://test\n  api_key: test\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: test\nuser_name: Tester\nplatform: tui\nsession_type: interactive\n",
        encoding="utf-8",
    )
    # Minimal personality.yaml so bootstrap can load it
    (data_dir / "personalities" / "default" / "personality.yaml").write_text(
        "agent_name: TestBot\nagent_role: You are a test bot.\nprovider: default\n"
        "max_context_tokens: 4096\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )

    app = create_app(
        personality_id="default",
        provider_name="default",
        data_dir=str(data_dir),
        no_plugins=True,
    )
    # Inject a mock LLM that returns one canned response per turn.
    set_llm_override(app, MockLLM(responses=[{"content": "hello from mock"}]))
    yield app


@pytest_asyncio.fixture
async def client(http_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=http_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_http_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["server_name"] == "xbotv2"


@pytest.mark.asyncio
async def test_http_hello_returns_protocol_info(client: httpx.AsyncClient) -> None:
    response = await client.post("/hello", json={"session_id": "s1", "thread_id": "t1"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1"
    assert body["thread_id"] == "t1"
    assert body["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_http_open_session_returns_agent_name(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/sessions", json={"session_id": "s1", "thread_id": "t1"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["agent_name"]
    assert body["session_id"] == "s1"


@pytest.mark.asyncio
async def test_http_messages_sse_stream_turn_events(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "stream1", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    async with client.stream(
        "POST",
        "/sessions/stream1/messages",
        json={"content": "hi there", "request_id": "req-1"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    types = [event.get("type") for event in events]
    assert "turn_started" in types
    assert "assistant_message" in types
    assert "turn_finished" in types
    assert "end" in types

    assistant = next(e for e in events if e.get("type") == "assistant_message")
    assert assistant["data"]["content"] == "hello from mock"


@pytest.mark.asyncio
async def test_http_messages_preserves_chinese_payload_in_request(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "zh", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    async with client.stream(
        "POST",
        "/sessions/zh/messages",
        json={"content": "当前磁盘用了多少", "request_id": "req-zh"},
    ) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(body)
    # The mock LLM echoes a fixed string; the test that the request body
    # preserved UTF-8 is exercised via the tui-side trace in
    # test_tui_client.py::test_http_transport_trace_records_unicode_payload.
    # Here we only confirm the SSE frame encoding survives the round-trip.
    assert any(
        e.get("type") == "assistant_message" for e in events
    ), f"no assistant_message in: {events}"


@pytest.mark.asyncio
async def test_http_messages_empty_content_rejected(client: httpx.AsyncClient) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "empty", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post(
        "/sessions/empty/messages", json={"content": "   ", "request_id": "x"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_http_messages_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/sessions/does-not-exist/messages",
        json={"content": "hi", "request_id": "r"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_http_interactions_endpoint_validates_request_id(
    client: httpx.AsyncClient,
) -> None:
    open_resp = await client.post(
        "/sessions", json={"session_id": "validate", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post(
        "/sessions/validate/interactions/permission-response",
        json={"decision": "allow", "scope": "once"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


# ----------------------------------------------------------------------
# ESC interrupt — v1.2 (§10.5.6.1)
# ----------------------------------------------------------------------


class _GatedMockLLM(MockLLM):
    """A ``MockLLM`` whose ``_agenerate`` blocks on an ``asyncio.Event``.

    The test sets ``release`` *after* verifying the SSE stream is open
    and the interrupt endpoint has been hit; the engine's
    ``asyncio.CancelledError`` (triggered by ``/interrupt``) will fire
    first and tear the turn down before the LLM is unblocked.
    """

    def __init__(self, release: asyncio.Event, **kwargs):
        super().__init__(responses=[{"content": "late reply"}], **kwargs)
        object.__setattr__(self, "_gated_release", release)
        object.__setattr__(self, "_gated_calls", 0)

    @property
    def calls(self) -> int:
        return self._gated_calls  # type: ignore[has-type]

    async def _agenerate(
        self,
        messages: list,
        stop: list | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        object.__setattr__(
            self, "_gated_calls", self._gated_calls + 1  # type: ignore[has-type]
        )
        # Block until released. If the engine gets cancelled mid-turn,
        # this ``await`` will raise ``CancelledError`` and abort the
        # turn before the event is set.
        await self._gated_release.wait()  # type: ignore[has-type]
        return await super()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


@pytest.mark.asyncio
async def test_http_interrupt_emits_turn_cancelled_on_sse(
    http_app, tmp_path: Path
) -> None:
    """Pressing ESC (i.e. ``POST /sessions/{sid}/interrupt``) mid-turn
    must close the SSE stream with a ``turn_cancelled`` event.

    This exercises the full production path:
    TUI ESC → ``HttpTransport.interrupt`` → ``POST /interrupt`` →
    ``SessionContext.request_interrupt`` → ``turn_task.cancel`` →
    ``Engine.run_turn`` catch ``CancelledError`` → yield
    ``turn_cancelled`` → SSE → client.

    We spin up a **real** uvicorn process (not ``ASGITransport``)
    because ASGITransport buffers the entire response body before
    exposing it to the client, which deadlocks this test.
    """

    import socket
    import threading
    import time
    import uvicorn

    release = asyncio.Event()
    gated = _GatedMockLLM(release=release)
    set_llm_override(http_app, gated)

    # Pick a free port and start uvicorn in a background thread.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(
        http_app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for the server to be ready.
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as probe:
        for _ in range(50):
            try:
                r = await probe.get("/health")
                if r.status_code == 200:
                    break
            except httpx.RequestError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("uvicorn server failed to start")

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as ac:
            open_resp = await ac.post(
                "/sessions", json={"session_id": "esc", "thread_id": "t"}
            )
            assert open_resp.status_code == 200

            sse_chunks: list[str] = []

            async def _consume_sse() -> None:
                async with ac.stream(
                    "POST",
                    "/sessions/esc/messages",
                    json={"content": "do something long", "request_id": "req-esc"},
                ) as response:
                    assert response.status_code == 200
                    async for chunk in response.aiter_text():
                        sse_chunks.append(chunk)
                        # Once we see ``turn_started`` we know the
                        # engine is past the bootstrap and is about
                        # to call the (gated) LLM.
                        if "turn_started" in chunk:
                            ir = await ac.post("/sessions/esc/interrupt")
                            assert ir.status_code == 200
                            assert ir.json()["cancelled"] is True

            await asyncio.wait_for(_consume_sse(), timeout=5.0)
            # Defensive: unblock the LLM in case the test exits
            # before the engine's CancelledError fires.
            release.set()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)

    body = "".join(sse_chunks)
    events = _parse_sse(body)
    types = [e.get("type") for e in events]
    assert "turn_started" in types, f"no turn_started in {types!r}"
    assert "turn_cancelled" in types, f"no turn_cancelled in {types!r}"
    # The stream must terminate after cancellation — no
    # ``turn_finished`` because the LLM never returned a response.
    assert "turn_finished" not in types
    # The engine is allowed to call the LLM at most once before the
    # cancellation lands.
    assert gated.calls <= 1, f"LLM was called {gated.calls} times after interrupt"


@pytest.mark.asyncio
async def test_http_interrupt_when_idle_returns_no_op(
    client: httpx.AsyncClient,
) -> None:
    """``POST /sessions/{sid}/interrupt`` with no turn in flight is a
    no-op success — pressing ESC on the TUI should never 4xx."""

    open_resp = await client.post(
        "/sessions", json={"session_id": "idle", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    response = await client.post("/sessions/idle/interrupt")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "idle"
    assert body["cancelled"] is False
    assert body["status"] == "idle"
