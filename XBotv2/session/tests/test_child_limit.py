"""Child admission is owned by the session lifecycle."""

import asyncio

import pytest

from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS, THREAD_EVENTS


async def test_child_limit_applies_to_direct_open_and_resume(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)

    def child(session, name, mode="new"):
        return OpenThread(session, name, "main", None, None, mode, True, model_override=MockLLM())

    try:
        for session in ("one", "two"):
            await sessions.open(OpenSession(session, "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
            sessions.set_child_limit(session, 1)

        results = await asyncio.gather(
            sessions.open_thread(child("one", "first")),
            sessions.open_thread(child("one", "second")),
            sessions.open_thread(child("two", "other")),
            return_exceptions=True,
        )
        assert sum(isinstance(result, OperationError) and result.code == "child_limit_reached" for result in results[:2]) == 1
        assert not isinstance(results[2], BaseException)
        name = "second" if isinstance(results[0], BaseException) else "first"
        active = await sessions.locate("one", name)
        events = active.require(THREAD_EVENTS).subscribe()
        await active.require(AGENT_LOOP).send_message("persist", "input")
        async with asyncio.timeout(5):
            async for frame in events:
                if frame.event.kind == "turn_ended":
                    break
        await events.aclose()
        assert await sessions.open_thread(child("one", name, "resume")) is active
        await sessions.close_thread("one", name)
        reopened = await sessions.open_thread(child("one", name, "resume"))
        assert reopened is not active
        with pytest.raises(OperationError, match="limit reached"):
            await sessions.open_thread(child("one", "extra"))
    finally:
        await root.close()


async def test_default_unlimited_and_clearing_child_limit(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    try:
        await sessions.open(OpenSession("unlimited", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
        for index in range(5):
            parent = "main" if index < 4 else "child-0"
            await sessions.open_thread(OpenThread("unlimited", f"child-{index}", parent, None, None, "new", True, model_override=MockLLM()))
        sessions.set_child_limit("unlimited", 5)
        with pytest.raises(OperationError, match="limit reached"):
            await sessions.open_thread(OpenThread("unlimited", "blocked", "main", None, None, "new", True, model_override=MockLLM()))
        sessions.set_child_limit("unlimited", None)
        await sessions.open_thread(OpenThread("unlimited", "allowed", "main", None, None, "new", True, model_override=MockLLM()))
        assert await sessions.locate("unlimited", "allowed")
    finally:
        await root.close()


@pytest.mark.parametrize("close_parent", [False, True])
async def test_close_closes_running_descendants_before_parents(tmp_path, close_parent):
    from XBotv2.core.api.stream import ModelCompleted

    class WaitingProvider(MockLLM):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _astream_once(self, request):
            self.started.set()
            await self.release.wait()
            yield ModelCompleted(response=self.to_response({"content": "done"}))

    provider = WaitingProvider()
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    closed = []
    try:
        parent = await sessions.open(OpenSession("closing", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
        child = await sessions.open_thread(OpenThread("closing", "child", "main", None, None, "new", True, model_override=MockLLM()))
        grandchild = await sessions.open_thread(OpenThread("closing", "grandchild", "child", None, None, "new", True, model_override=provider))
        for scope, name in ((parent, "main"), (child, "child"), (grandchild, "grandchild")):
            scope.effect(lambda name=name: lambda: closed.append(name))
        await grandchild.require(AGENT_LOOP).send_message("work", "input")
        await asyncio.wait_for(provider.started.wait(), 5)
        if close_parent:
            await asyncio.wait_for(sessions.close_thread("closing", "child"), 5)
            assert closed == ["grandchild", "child"]
            assert await sessions.locate("closing", "main") is parent
        await asyncio.wait_for(sessions.close_session("closing"), 5)
        assert closed == ["grandchild", "child", "main"]
    finally:
        provider.release.set()
        await root.close()


async def test_parent_close_cancels_opening_child_and_releases_slot(tmp_path, monkeypatch):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    entered = asyncio.Event()
    release = asyncio.Event()
    session_owner = sessions._session_owner

    async def gated_owner(*args):
        entered.set()
        await release.wait()
        return await session_owner(*args)

    try:
        await sessions.open(OpenSession("opening", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
        await sessions.open(OpenSession("opening", "other", str(tmp_path), None, "new", True, model_override=MockLLM()))
        sessions.set_child_limit("opening", 1)
        monkeypatch.setattr(sessions, "_session_owner", gated_owner)
        opening = asyncio.create_task(sessions.open_thread(OpenThread("opening", "opening", "main", None, None, "new", True, model_override=MockLLM())))
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(sessions.close_thread("opening", "main"), 5)
        result = await asyncio.gather(opening, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        release.set()
        replacement = await sessions.open_thread(OpenThread("opening", "replacement", "other", None, None, "new", True, model_override=MockLLM()))
        assert await sessions.locate("opening", "replacement") is replacement
        assert {scope.require("thread_info").thread_id for scope in await sessions.scopes("opening")} == {"other", "replacement"}
    finally:
        release.set()
        await root.close()
