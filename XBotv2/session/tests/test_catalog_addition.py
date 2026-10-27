"""Session catalog additions follow the lifetime of blank session owners."""

import asyncio

from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import (
    OpenSession, OpenThread, SESSIONS, SESSION_RESOURCE_CHANGED, SessionSummary, THREAD_EVENTS,
)


async def test_concurrent_first_inputs_add_once_and_eviction_does_not_add(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    changes = []
    root.on(SESSION_RESOURCE_CHANGED, changes.append)
    try:
        main = await sessions.open(OpenSession(
            "shared", "main", str(tmp_path), None, "new", True,
            model_override=MockLLM(responses=[{"content": "done"}, {"content": "again"}]),
        ))
        child = await sessions.open_thread(OpenThread(
            "shared", "child", "main", None, None, "new", True,
            model_override=MockLLM(responses=[{"content": "done"}]),
        ))
        await sessions.close_thread("shared", "missing")
        streams = [scope.require(THREAD_EVENTS).subscribe() for scope in (main, child)]
        await asyncio.gather(*(
            scope.require(AGENT_LOOP).send_message(name, f"input-{name}")
            for name, scope in (("main", main), ("child", child))
        ))
        async def wait_for_turn(stream):
            async with asyncio.timeout(5):
                async for frame in stream:
                    if frame.event.kind == "turn_ended":
                        break
            await stream.aclose()
        await asyncio.gather(*(wait_for_turn(stream) for stream in streams))
        matching = [change for change in changes if change.session.session_id == "shared"]
        assert sum(change.added for change in matching) == 1
        assert "shared" not in sessions._unpublished_sessions

        for index in range(257):
            sessions._remember_summary(f"other-{index}", SessionSummary(
                session_id=f"other-{index}", status="inactive",
            ))
        assert "shared" not in sessions._published_summaries
        changes.clear()
        stream = main.require(THREAD_EVENTS).subscribe()
        await main.require(AGENT_LOOP).send_message("again", "input-again")
        await wait_for_turn(stream)
        assert changes and all(change.added is False for change in changes)
    finally:
        await root.close()


async def test_blank_owner_close_releases_unpublished_marker(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    try:
        await sessions.open(OpenSession(
            "blank", "main", str(tmp_path), None, "new", True,
            model_override=MockLLM(),
        ))
        assert "blank" in sessions._unpublished_sessions
        await sessions.close_session("blank")
        assert "blank" not in sessions._unpublished_sessions
    finally:
        await root.close()
