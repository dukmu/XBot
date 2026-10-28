"""Replay, fan-out, and baseline behavior of Workspace catalog events."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from XBotv2.session.api.contracts import SessionResourceChanged, SessionResourceRemoved
from XBotv2.session.impl.protocol import build_session_router
from XBotv2.session.api.contracts import SessionSummary
from XBotv2.workspaces.api.events import WorkspaceCursorExpired
from XBotv2.workspaces.impl.events import WorkspaceEventStream
from XBotv2.session.impl.replay import ReplayPool


@pytest.mark.asyncio
async def test_workspace_event_stream_replays_then_fans_out_to_every_client():
    stream = WorkspaceEventStream(pool=ReplayPool(32 * 1024 * 1024), capacity=4)
    first = stream.publish(SessionResourceChanged(SessionSummary(
        session_id="s1", status="inactive"
    )))
    left = stream.subscribe(0)
    right = stream.subscribe(1)

    assert await anext(left) == first
    changed = stream.publish(SessionResourceChanged(SessionSummary(
        session_id="s1", status="active"
    )))
    assert await anext(left) == changed
    assert await anext(right) == changed

    await left.aclose()
    await right.aclose()


@pytest.mark.asyncio
async def test_workspace_event_stream_rejects_an_expired_cursor():
    stream = WorkspaceEventStream(pool=ReplayPool(32 * 1024 * 1024), capacity=2)
    for number in range(3):
        stream.publish(SessionResourceChanged(SessionSummary(
            session_id=f"s{number}",
            status="inactive",
        )))

    with pytest.raises(WorkspaceCursorExpired, match="expired"):
        stream.subscribe(0)


@pytest.mark.asyncio
async def test_session_baseline_cursor_precedes_the_snapshot_read():
    workspace_events = SimpleNamespace(sequence=11)

    class RacingSessions:
        async def list_sessions(self, **kwargs):
            workspace_events.sequence = 12
            return SimpleNamespace(items=(), next_cursor=None, previous_cursor=None)

    app = FastAPI()
    app.include_router(build_session_router(
        sessions=RacingSessions(),
        archive=None,
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


@pytest.mark.asyncio
async def test_workspace_byte_budget_expires_slow_cursor_and_keeps_other_stream_live():
    stream = WorkspaceEventStream(pool=ReplayPool(32 * 1024 * 1024), max_bytes=200)
    other = WorkspaceEventStream(pool=ReplayPool(32 * 1024 * 1024), max_bytes=200)
    slow = stream.subscribe(0)
    first = stream.publish(SessionResourceRemoved("small"))
    stream.publish(SessionResourceRemoved("界" * 200))
    other_frame = other.publish(SessionResourceRemoved("other"))

    with pytest.raises(WorkspaceCursorExpired, match="expired"):
        await anext(slow)
    with pytest.raises(WorkspaceCursorExpired, match="expired"):
        stream.subscribe(first.sequence)
    assert await anext(other.subscribe(0)) == other_frame
    cursor = stream.sequence
    following = stream.publish(SessionResourceRemoved("following"))
    assert await anext(stream.subscribe(cursor)) == following


@pytest.mark.asyncio
async def test_workspace_byte_budget_evicts_retained_frames():
    stream = WorkspaceEventStream(pool=ReplayPool(32 * 1024 * 1024), max_bytes=240)
    slow = stream.subscribe(0)
    stream.publish(SessionResourceRemoved("x" * 100))
    latest = stream.publish(SessionResourceRemoved("y" * 100))
    with pytest.raises(WorkspaceCursorExpired):
        await anext(slow)
    assert await anext(stream.subscribe(1)) == latest


def test_single_change_event_payloads():
    from XBotv2.workspaces.api.contracts import WorkspaceOrderChanged, WorkspaceSessionOrderChanged, ArchivedSessionsChanged
    from XBotv2.workspaces.api.events import WorkspaceEventFrame
    from XBotv2.workspaces.impl.protocol import _workspace_event
    assert _workspace_event(WorkspaceEventFrame(1, WorkspaceOrderChanged("w", None))) == ("catalog/workspace-order-changed", {"workspace_id": "w", "before_workspace_id": None})
    assert _workspace_event(WorkspaceEventFrame(2, WorkspaceSessionOrderChanged("w", "s", "t"))) == ("catalog/workspace-session-order-changed", {"workspace_id": "w", "session_id": "s", "before_session_id": "t"})
    assert _workspace_event(WorkspaceEventFrame(3, ArchivedSessionsChanged("s", True))) == ("catalog/archived-sessions-changed", {"session_id": "s", "archived": True})
