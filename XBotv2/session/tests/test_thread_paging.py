"""Thread catalog pages retain owner filtering and active runtime visibility."""
import pytest

from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.core.api.messages import HumanInputMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS, ThreadSummary


@pytest.mark.asyncio
async def test_unpersisted_active_threads_are_listed_and_addressable(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    try:
        manager = root.require(SESSIONS)
        await manager.open(OpenSession(
            "s", "zmain", str(tmp_path), None, "new", True, model_override=MockLLM(),
        ))
        child_scope = await manager.open_thread(OpenThread(
            "s", "child", "zmain", str(tmp_path), None, "new", True, model_override=MockLLM(),
        ))
        assert not await manager._repository.has_thread("s", "child")
        assert (await manager.thread_summary("s", "child")).status == "active"
        main = await manager.list_threads("s", limit=1, kind="main")
        assert [item.thread_id for item in main.items] == ["zmain"]
        child = await manager.list_threads("s", parent_thread_id="zmain", kind="subagent")
        assert [item.thread_id for item in child.items] == ["child"]
        first = await manager.list_threads("s", limit=1)
        second = await manager.list_threads("s", limit=1, cursor=first.next_cursor)
        assert [first.items[0].thread_id, second.items[0].thread_id] == ["child", "zmain"]
        assert (await manager.list_threads("s", limit=1, cursor=second.previous_cursor)).items == first.items
        await child_scope.require(LOOP_STATE).history.append(
            HumanInputMessage(id="child-input", parts=(TextPart(text="hello"),))
        )
        assert [item.thread_id for item in (await manager.list_threads("s")).items] == ["child", "zmain"]
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_thread_pages_beyond_500_and_parent_filter(tmp_path, monkeypatch):
    from XBotv2.session.impl import manager as module
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    try:
        manager = root.require(SESSIONS)
        scope = await manager.open(OpenSession(
            "s", "zzmain", str(tmp_path), None, "new", True, model_override=MockLLM(),
        ))
        await scope.require(LOOP_STATE).history.append(
            HumanInputMessage(id="one", parts=(TextPart(text="hello"),))
        )
        def seed(sql):
            for index in range(550):
                parent = "zzmain" if index % 2 == 0 else "other"
                sql.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (
                    f"child-{index:04}", parent, "agent", "{}", "now",
                ))
        await manager._repository.domain("s").transaction(seed)
        built = []
        async def summary(_manager, session_id, thread_id, active):
            built.append(thread_id)
            return ThreadSummary(session_id=session_id, thread_id=thread_id, status="active" if active else "inactive")
        monkeypatch.setattr(module, "_thread_summary", summary)
        first = await manager.list_threads("s", limit=100)
        assert len(built) == 100
        page = first
        while page.next_cursor:
            page = await manager.list_threads("s", limit=100, cursor=page.next_cursor)
        assert page.items[-1].thread_id == "zzmain"
        assert (await manager.list_threads("s", limit=100, cursor=page.previous_cursor)).items[-1].thread_id == "child-0499"
        selected = await manager.list_threads("s", limit=100, parent_thread_id="zzmain", kind="subagent")
        assert len(selected.items) == 100
        assert selected.items[-1].thread_id == "child-0198"
        assert (await manager.list_threads("s", limit=1, kind="main")).items[0].thread_id == "zzmain"
        monkeypatch.undo()
        exact = await manager.thread_summary("s", "child-0549")
        assert exact.thread_id == "child-0549" and exact.status == "inactive"
        assert exact.parent_thread_id == "other"
        await manager._repository.domain("s").transaction(lambda sql: sql.execute(
            "DELETE FROM threads WHERE parent_thread_id=? AND thread_id>?",
            ("zzmain", "child-0198"),
        ))
        empty = await manager.list_threads(
            "s", limit=100, cursor=selected.next_cursor,
            parent_thread_id="zzmain", kind="subagent",
        )
        assert not empty.items and empty.previous_cursor
        back = await manager.list_threads(
            "s", limit=100, cursor=empty.previous_cursor,
            parent_thread_id="zzmain", kind="subagent",
        )
        assert [item.thread_id for item in back.items] == [item.thread_id for item in selected.items]
    finally:
        await root.close()
