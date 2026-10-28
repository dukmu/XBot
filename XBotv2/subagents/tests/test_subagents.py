"""Subagent collaboration uses session-owned threads and ordinary jobs."""

import pytest

from XBotv2.agents.api.contracts import AgentDefinition
from XBotv2.session.api.contracts import ThreadSummary
from XBotv2.subagents.api.contracts import SubagentAgentError, SubagentsConfig
from XBotv2.subagents.impl.service import SubagentLauncher, SubagentCoordinator


def test_max_live_threads_defaults_to_unlimited_and_accepts_none():
    assert SubagentsConfig().max_live_threads is None
    assert SubagentsConfig.model_validate({"max_live_threads": None}).max_live_threads is None


class _Catalog:
    def __init__(self, definition: AgentDefinition | None = None):
        self.definition = definition

    def get(self, _name):
        return self.definition


class _Session:
    session_id = "parent-session"
    thread_id = "parent-thread"
    workspace_root = "/workspace"

    def new_thread_id(self, name):
        return f"child-{name}"


class _Sessions:
    def __init__(self, threads=()):
        self.threads = tuple(threads)
        self.opened = []

    async def new_thread_id(self, session_id, name):
        assert session_id == "parent-session"
        return f"child-{name}"

    async def list_threads(self, session_id, *, limit=100, cursor=None, parent_thread_id=None, kind=None):
        from XBotv2.session.api.contracts import ThreadPage
        assert session_id == "parent-session"
        threads = [t for t in self.threads if (parent_thread_id is None or t.parent_thread_id == parent_thread_id) and (kind is None or t.kind == kind)]
        start = int(cursor) if cursor else 0
        return ThreadPage(items=tuple(threads[start:start + limit]), next_cursor=str(start + limit) if start + limit < len(threads) else None)

    async def thread_summary(self, session_id, thread_id):
        from XBotv2.session.api.contracts import SessionNotFound
        for thread in self.threads:
            if thread.thread_id == thread_id:
                return thread
        raise SessionNotFound(thread_id)

    async def open_thread(self, request):
        self.opened.append(request)


def _definition(name="explorer", mode="subagent"):
    return AgentDefinition(name=name, description=f"{name} agent", mode=mode)


def _launcher(definition=None, *, threads=()):
    sessions = _Sessions(threads)
    launcher = SubagentLauncher(
        catalog=_Catalog(definition),
        session=_Session(),
        sessions=sessions,
        coordinator=SubagentCoordinator(_Session(), sessions),
        provider_name="mock",
        no_plugins=False,
        archive=None,
        inbox=None,
    )
    return launcher, sessions


@pytest.mark.asyncio
async def test_launcher_creates_child_through_the_session_owner():
    launcher, sessions = _launcher(_definition())

    await launcher.ensure_open(
        thread_id="child-explorer",
        agent="explorer",
        create=True,
    )

    [request] = sessions.opened
    assert request.session_id == "parent-session"
    assert request.thread_id == "child-explorer"
    assert request.parent_thread_id == "parent-thread"
    assert request.workspace_root == "/workspace"
    assert request.provider_name == "mock"
    assert request.mode == "new"
    assert request.selected_agent == "explorer"


@pytest.mark.asyncio
async def test_launcher_resumes_only_a_direct_inactive_child():
    child = ThreadSummary(
        session_id="parent-session",
        thread_id="child-explorer",
        status="inactive",
        kind="subagent",
        parent_thread_id="parent-thread",
        agent="explorer",
    )
    launcher, sessions = _launcher(_definition(), threads=(child,))

    await launcher.ensure_open(
        thread_id="child-explorer",
        agent="explorer",
        create=False,
    )

    [request] = sessions.opened
    assert request.mode == "resume"
    assert request.thread_id == "child-explorer"

    sibling = child.model_copy(update={"parent_thread_id": "another-parent"})
    launcher, _sessions = _launcher(_definition(), threads=(sibling,))
    with pytest.raises(SubagentAgentError, match="Unknown child thread"):
        await launcher.ensure_open(
            thread_id="child-explorer",
            agent="explorer",
            create=False,
        )

@pytest.mark.asyncio
async def test_wait_freezes_the_current_child_turn():
    import asyncio
    from XBotv2.subagents.impl.service import SubagentTools, _Turn

    child = ThreadSummary(session_id="parent-session", thread_id="child", status="active", kind="subagent", parent_thread_id="parent-thread", agent="explorer")
    launcher, _ = _launcher(_definition(), threads=(child,))
    first = _Turn("first")
    launcher._coordinator.turns["child"] = first
    tools = SubagentTools(launcher=launcher)
    waiting = asyncio.create_task(tools.wait_subagent(ids=["child"], timeout_ms=1000))
    await asyncio.sleep(0)
    first.status = "completed"
    first.done.set()
    launcher._coordinator.turns["child"] = _Turn("second")
    outcome = await waiting
    assert "ready" in outcome.output.parts[0].text
    assert "completed" in outcome.output.parts[0].text

@pytest.mark.asyncio
async def test_removed_unclaimed_task_is_not_confused_with_older_execution():
    from XBotv2.agentloop.api.storage import InputRecord
    from XBotv2.core.api.domain import InboxTarget
    from XBotv2.core.api.tools import ToolFailed
    from XBotv2.subagents.impl.service import SubagentTools

    child = ThreadSummary(session_id="parent-session", thread_id="child", status="inactive", kind="subagent", parent_thread_id="parent-thread", agent="explorer")
    launcher, _ = _launcher(_definition(), threads=(child,))

    class Archive:
        async def latest_input(self, session_id, thread_id, *, source, event):
            assert (source, event) == ("subagent:parent-thread", "task")
            return InputRecord(input_id="removed", status="removed", source=source, event=event, target=InboxTarget.NEXT_TURN)

        async def execution_for_input(self, session_id, thread_id, input_id):
            assert input_id == "removed"
            return None

    launcher._archive = Archive()
    tools = SubagentTools(launcher=launcher)
    waited = await tools.wait_subagent(ids=["child"])
    assert "cancelled" in waited.output.parts[0].text
    assert isinstance(await tools.read_subagent(id="child"), ToolFailed)

@pytest.mark.asyncio
@pytest.mark.parametrize("active,started", [(False, True), (False, False), (True, True), (True, False)])
async def test_cold_unfinished_child_is_interrupted(active, started):
    from types import SimpleNamespace
    from XBotv2.agentloop.api.contracts import AGENT_LOOP
    from XBotv2.agentloop.api.storage import ExecutionRecord, InputRecord
    from XBotv2.core.api.domain import InboxTarget
    from XBotv2.session.api.contracts import THREAD_EVENTS, ThreadNotActive
    from XBotv2.subagents.impl.service import SubagentTools

    child = ThreadSummary(session_id="parent-session", thread_id="child", status="active" if active else "inactive", kind="subagent", parent_thread_id="parent-thread", agent="explorer")
    launcher, sessions = _launcher(_definition(), threads=(child,))
    class Subscription:
        async def aclose(self):
            pass
    class Scope:
        def require(self, key):
            if key == THREAD_EVENTS:
                return SimpleNamespace(subscribe=Subscription)
            assert key == AGENT_LOOP
            return SimpleNamespace(execution_id=None, inbox=SimpleNamespace(pending=[]))
    async def locate(*_):
        if not active:
            raise ThreadNotActive("child")
        return Scope()
    sessions.locate = locate
    class Archive:
        async def latest_input(self, *args, **kwargs):
            return InputRecord(input_id="old", status="consumed" if started else "pending", source="subagent:parent-thread", event="task", target=InboxTarget.NEXT_TURN)
        async def execution_for_input(self, *args):
            return ExecutionRecord(turn_id="old-turn", input_ids=("old",), status="running") if started else None
    launcher._archive = Archive()
    tools = SubagentTools(launcher=launcher)
    waited = await tools.wait_subagent(ids=["child"], timeout_ms=10)
    assert "ready: child: interrupted" in waited.output.parts[0].text
    result = await tools.read_subagent(id="child")
    assert "interrupted" in result.output.parts[0].text


@pytest.mark.asyncio
async def test_expired_event_window_uses_frozen_execution():
    from types import SimpleNamespace
    from XBotv2.agentloop.api.contracts import AGENT_LOOP
    from XBotv2.agentloop.api.outputs import LoopTurnStarted
    from XBotv2.agentloop.api.storage import ExecutionRecord
    from XBotv2.core.api.domain import TurnScope
    from XBotv2.session.api.contracts import THREAD_EVENTS
    from XBotv2.session.impl.event_stream import SessionEventStream
    from XBotv2.session.impl.replay import ReplayPool
    from XBotv2.subagents.impl.service import _Turn

    launcher, sessions = _launcher(_definition())
    stream = SessionEventStream(SimpleNamespace(event_cursor=0), pool=ReplayPool(1024), capacity=1)
    subscription = stream.subscribe()
    stream.publish(LoopTurnStarted(turn=1), scope=TurnScope("first-turn"))
    stream.publish(LoopTurnStarted(turn=2), scope=TurnScope("next-turn"))
    class Scope:
        def require(self, key):
            if key == THREAD_EVENTS:
                return stream
            assert key == AGENT_LOOP
            return SimpleNamespace(execution_id="next-turn", inbox=SimpleNamespace(pending=[]))
    async def locate(*_):
        return Scope()
    sessions.locate = locate
    class Archive:
        async def execution_for_turn(self, session_id, thread_id, turn_id):
            assert turn_id == "first-turn"
            assert stream.subscriber_count == 1
            return ExecutionRecord(turn_id=turn_id, input_ids=("first",), status="completed", final_message_id="answer")
    launcher._archive = Archive()
    notices = []
    async def submit(thread_id, item, *, wake):
        notices.append((thread_id, item.input.event, wake))
    launcher.submit = submit
    turn = _Turn("first", claimed=True, turn_id="first-turn", status="started")
    await launcher._observe("child", turn, subscription)
    assert turn.done.is_set() and turn.status == "completed"
    assert notices == [("parent-thread", "subagent/completed", False)]
    assert stream.subscriber_count == 0


@pytest.mark.asyncio
async def test_list_subagents_pages_direct_children():
    import json
    from XBotv2.subagents.impl.service import SubagentTools

    threads = tuple(
        ThreadSummary(
            session_id="parent-session", thread_id=f"child-{i:04}",
            status="inactive", kind="subagent", parent_thread_id="parent-thread",
            agent="explorer",
        )
        for i in range(205)
    )
    sibling = ThreadSummary(
        session_id="parent-session", thread_id="other", status="inactive",
        kind="subagent", parent_thread_id="other-parent",
    )
    launcher, _ = _launcher(_definition(), threads=(*threads, sibling))
    tools = SubagentTools(launcher=launcher)
    cursor = None
    seen = []
    while True:
        page = json.loads((await tools.list_subagents(limit=100, cursor=cursor)).output.parts[0].text)
        seen.extend(item["thread_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [thread.thread_id for thread in threads]
