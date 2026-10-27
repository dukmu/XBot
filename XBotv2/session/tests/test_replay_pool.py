"""The process budget is shared by thread and workspace event owners."""

import asyncio
import gc
import weakref
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic_core import to_json

from XBotv2.core.api.domain import SessionScope
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import (
    OpenSession, OpenThread, SESSIONS, THREAD_EVENTS, SessionEventCursorExpired,
    SessionEventFrame, SessionResourceRemoved,
)
from XBotv2.session.api.replay import REPLAY_POOL
from XBotv2.session.impl.event_stream import SessionEventStream
from XBotv2.session.impl.replay import ReplayPool
from XBotv2.workspaces.api.contracts import WORKSPACE_EVENTS
from XBotv2.workspaces.api.events import WorkspaceCursorExpired, WorkspaceEventFrame
from XBotv2.workspaces.impl.events import WorkspaceEventStream


@dataclass(frozen=True)
class Event:
    kind: str
    text: str


def test_public_pool_stats_follow_retention_and_close():
    pool = ReplayPool(100)
    first = pool.window(capacity=2, max_bytes=100)
    second = pool.window(capacity=2, max_bytes=100)
    assert (pool.stats["budget_bytes"], pool.stats["resident_bytes"], pool.stats["frames"], pool.stats["windows"]) == (100, 0, 0, 2)
    first.append("first", 60)
    second.append("second", 60)
    assert (pool.stats["budget_bytes"], pool.stats["resident_bytes"], pool.stats["frames"], pool.stats["windows"]) == (100, 60, 1, 2)
    second.close()
    assert (pool.stats["resident_bytes"], pool.stats["frames"], pool.stats["windows"]) == (0, 0, 1)
    pool.close()
    assert (pool.stats["resident_bytes"], pool.stats["frames"], pool.stats["windows"]) == (0, 0, 0)


async def test_cross_stream_eviction_wakes_slow_subscriber_and_expires_cursor():
    event = Event("notice", "界" * 70)
    change = SessionResourceRemoved("w" * 150)
    first_size = len(to_json(SessionEventFrame(1, SessionScope(), event)))
    other_size = len(to_json(WorkspaceEventFrame(1, change)))
    pool = ReplayPool(first_size + other_size - 1)
    thread = SessionEventStream(SimpleNamespace(event_cursor=0), pool=pool)
    workspace = WorkspaceEventStream(pool=pool)
    try:
        slow = thread.subscribe(0)
        slow_workspace = workspace.subscribe(0)
        thread.publish(event)
        workspace.publish(change)
        assert pool.stats["resident_bytes"] <= pool.stats["budget_bytes"]
        with pytest.raises(SessionEventCursorExpired):
            await asyncio.wait_for(anext(slow), 1)
        assert (await anext(workspace.subscribe(0))).change == change
        thread.publish(event)
        with pytest.raises(WorkspaceCursorExpired):
            await asyncio.wait_for(anext(slow_workspace), 1)
        later = thread.publish(Event("notice", "again"))
        assert (await anext(thread.subscribe(later.sequence - 1))) == later
    finally:
        thread.close()
        workspace.close()
        pool.close()
    assert pool.stats["resident_bytes"] == 0 and pool.stats["windows"] == 0


async def test_close_releases_frames_even_when_subscriber_is_retained():
    pool = ReplayPool(4096)
    stream = SessionEventStream(SimpleNamespace(event_cursor=0), pool=pool)
    subscriber = stream.subscribe(0)
    event = Event("notice", "body" * 100)
    event_ref = weakref.ref(event)
    stream.publish(event)
    del event
    stream.close()
    gc.collect()
    assert event_ref() is None
    assert pool.stats["resident_bytes"] == 0 and pool.stats["windows"] == 0
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)
    pool.close()


async def test_oversized_frame_expires_own_window_without_discarding_other_stream():
    pool = ReplayPool(500)
    first = SessionEventStream(SimpleNamespace(event_cursor=0), pool=pool)
    other = SessionEventStream(SimpleNamespace(event_cursor=0), pool=pool)
    try:
        first.publish(Event("notice", "first"))
        other_frame = other.publish(Event("notice", "other"))
        first.publish(Event("notice", "x" * 1000))
        with pytest.raises(SessionEventCursorExpired):
            await anext(first.subscribe(0))
        assert (await anext(other.subscribe(0))) == other_frame
    finally:
        first.close()
        other.close()
        pool.close()


@pytest.mark.parametrize("budget", [0, 1])
async def test_unretained_frame_expires_cursor_and_empty_window_recovers(budget):
    pool = ReplayPool(budget)
    stream = SessionEventStream(SimpleNamespace(event_cursor=0), pool=pool)
    try:
        subscriber = stream.subscribe(0)
        stream.publish(Event("large", "x" * 500))
        assert stream.oldest_sequence == stream.sequence + 1
        with pytest.raises(SessionEventCursorExpired):
            await asyncio.wait_for(anext(subscriber), 1)
        assert pool.stats["resident_bytes"] == 0
    finally:
        stream.close()
        pool.close()


async def test_loader_shares_pool_and_scope_close_releases_windows(tmp_path):
    root = await open_runtime(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        overrides=PluginOverlay.parse([{"id": "session", "config": {"replay_process_bytes": 1500}}]),
    )
    pool = root.require(REPLAY_POOL)
    sessions = root.require(SESSIONS)
    try:
        main = await sessions.open(OpenSession("shared", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
        child = await sessions.open_thread(OpenThread("shared", "child", "main", None, None, "new", True, model_override=MockLLM()))
        first = main.require(THREAD_EVENTS)
        second = child.require(THREAD_EVENTS)
        workspace = root.require(WORKSPACE_EVENTS)
        assert pool.stats["windows"] == 3
        slow = first.subscribe(first.sequence)
        first.publish(Event("notice", "a" * 650))
        workspace.publish(SessionResourceRemoved("b" * 650))
        second.publish(Event("notice", "c" * 650))
        assert pool.stats["resident_bytes"] <= 1500
        with pytest.raises(SessionEventCursorExpired):
            await asyncio.wait_for(anext(slow), 1)
        await sessions.close_session("shared")
        assert pool.stats["windows"] == 1
    finally:
        await root.close()
    assert pool.stats["resident_bytes"] == 0 and pool.stats["windows"] == 0


async def test_process_budget_rejects_thread_override(tmp_path):
    from XBotv2.loader.api.contracts import LoadError

    root = await open_runtime(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        overrides=PluginOverlay.parse([{"id": "session", "config": {"replay_process_bytes": 4096}}]),
    )
    try:
        assert root.require(REPLAY_POOL).stats["budget_bytes"] == 4096
        sessions = root.require(SESSIONS)
        with pytest.raises(LoadError, match="process-scoped"):
            await sessions.open(OpenSession(
                "override", "main", str(tmp_path), None, "new", True,
                model_override=MockLLM(), plugin_configs={"session": {"replay_process_bytes": 8192}},
            ))
    finally:
        await root.close()
