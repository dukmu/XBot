"""A real socket sees stale replay fail and can resume from a fresh snapshot."""

import asyncio

import httpx
import uvicorn

from XBotv2.agentloop.api.outputs import LoopError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS, THREAD_EVENTS, SessionResourceRemoved
from XBotv2.session.api.replay import REPLAY_POOL
from XBotv2.workspaces.api.contracts import WORKSPACE_EVENTS


async def test_shared_eviction_real_sse_reconnects_from_snapshot(tmp_path):
    root = await open_runtime(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        overrides=PluginOverlay.parse([{"id": "session", "config": {"replay_process_bytes": 1500}}]),
    )
    serving = None
    http = None
    try:
        sessions = root.require(SESSIONS)
        main = await sessions.open(OpenSession("socket", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
        child = await sessions.open_thread(OpenThread("socket", "child", "main", None, None, "new", True, model_override=MockLLM()))
        first = main.require(THREAD_EVENTS)
        second = child.require(THREAD_EVENTS)
        catalog = root.require(WORKSPACE_EVENTS)
        http = uvicorn.Server(uvicorn.Config(root.server, host="127.0.0.1", port=0, log_level="critical", lifespan="off"))
        serving = asyncio.create_task(http.serve())
        async with asyncio.timeout(10):
            while not http.started:
                if serving.done():
                    await serving
                    raise RuntimeError("HTTP server exited before starting")
                await asyncio.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
            cursor = first.sequence
            path = "/sessions/socket/threads/main/events"
            async with client.stream("GET", path, params={"after": cursor}) as response:
                assert response.status_code == 200
                # Publishing without yielding makes this already-connected reader
                # fall behind before the server can pull its next frame.
                first.publish(LoopError(code="test", message="a" * 650))
                catalog.publish(SessionResourceRemoved("b" * 650))
                second.publish(LoopError(code="test", message="c" * 650))
                assert root.require(REPLAY_POOL).stats["resident_bytes"] <= 1500
                try:
                    async with asyncio.timeout(5):
                        await anext(response.aiter_bytes())
                except (httpx.RemoteProtocolError, StopAsyncIteration):
                    pass
                else:
                    raise AssertionError("expired live reader did not terminate")
            stale = await client.get(path, params={"after": cursor})
            assert stale.status_code == 409
            assert stale.json()["code"] == "session_event_cursor_expired"
            opened = await client.post("/sessions", json={"session_id": "socket", "thread_id": "main", "mode": "resume", "workspace_root": str(tmp_path), "include_history": False})
            assert opened.status_code == 200
            fresh_cursor = opened.json()["data"]["event_cursor"]
            async with client.stream("GET", path, params={"after": fresh_cursor}) as response:
                assert response.status_code == 200
                first.publish(LoopError(code="fresh", message="resynchronized"))
                async with asyncio.timeout(5):
                    body = await anext(response.aiter_bytes())
                assert b"resynchronized" in body and b"event: error" in body
    finally:
        if http is not None:
            http.should_exit = True
        if serving is not None:
            await asyncio.wait_for(serving, 10)
        await root.close()
