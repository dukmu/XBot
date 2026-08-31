"""Replay, fan-out, and baseline behavior of Workspace catalog events."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from XBotv2.session.contracts import SessionResourceChanged
from XBotv2.session.protocol import build_session_router
from XBotv2.session.types import SessionSnapshot
from XBotv2.workspaces.events import WorkspaceCursorExpired, WorkspaceEventStream


@pytest.mark.asyncio
async def test_workspace_event_stream_replays_then_fans_out_to_every_client():
    stream = WorkspaceEventStream(capacity=4)
    first = stream.publish(SessionResourceChanged(SessionSnapshot("s1", "inactive")))
    left = stream.subscribe(0)
    right = stream.subscribe(1)

    assert await anext(left) == first
    changed = stream.publish(SessionResourceChanged(SessionSnapshot("s1", "active")))
    assert await anext(left) == changed
    assert await anext(right) == changed

    await left.aclose()
    await right.aclose()


@pytest.mark.asyncio
async def test_workspace_event_stream_rejects_an_expired_cursor():
    stream = WorkspaceEventStream(capacity=2)
    for number in range(3):
        stream.publish(SessionResourceChanged(SessionSnapshot(
            f"s{number}",
            "inactive",
        )))

    with pytest.raises(WorkspaceCursorExpired, match="expired"):
        stream.subscribe(0)


@pytest.mark.asyncio
async def test_session_baseline_cursor_precedes_the_snapshot_read():
    workspace_events = SimpleNamespace(sequence=11)

    class RacingSessions:
        async def list_sessions(self):
            workspace_events.sequence = 12
            return ()

    app = FastAPI()
    app.include_router(build_session_router(
        sessions=RacingSessions(),
        options=SimpleNamespace(),
        workspace_events=workspace_events,
    ))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/sessions")

    assert response.status_code == 200
    assert response.json()["event_cursor"] == 11
