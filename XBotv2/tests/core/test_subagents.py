"""Blocking subagent execution tests."""

import asyncio
import json

import pytest

from xbotv2.api import AgentDefinition, RuntimePaths
from xbotv2.core.agents import AgentRegistry
from xbotv2.core.bootstrap import bootstrap
from xbotv2.core.subagents import SubagentManager
from xbotv2.core.session import SessionRuntime
from xbotv2.llm.mock import MockLLM
from xbotv2.persistence.store import CoreStateStore
from xbotv2.tools.permissions import PermissionIntersection, PermissionSystem


@pytest.mark.asyncio
async def test_blocking_task_runs_child_thread_and_returns_to_parent(
    temp_data_dir, temp_workspace
):
    agents_dir = temp_workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\n"
        "description: Review a focused change\n"
        "mode: subagent\n"
        "tools: []\n"
        "---\n"
        "Act as the workspace reviewer.",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[
        {
            "content": "Delegating review",
            "tool_calls": [{
                "name": "task",
                "args": {"agent": "reviewer", "prompt": "Review change A"},
                "id": "call_task",
            }],
        },
        {"content": "Child review result"},
        {"content": "Parent summary"},
    ])
    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="parent-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        llm_override=llm,
    )
    await engine.start_session()

    events = [event async for event in engine.run_turn("Review this change")]

    assert events[-1]["type"] == "turn_finished"
    assert any(
        event["type"] == "tool_result"
        and event["data"]["content"] == "Child review result"
        for event in events
    )
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "Parent summary"
        for event in events
    )
    assert "reviewer: Review a focused change" in "\n".join(
        str(message.content) for message in llm.get_call_messages(0)
    )
    assert "Act as the workspace reviewer." in "\n".join(
        str(message.content) for message in llm.get_call_messages(1)
    )

    records = [
        json.loads(line)
        for line in engine.state_store.paths.session.threads_log.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["event"] for record in records] == ["started", "completed"]
    child_thread = records[0]["thread_id"]
    child_messages = RuntimePaths.from_data_dir(temp_data_dir).session(
        "parent-session"
    ).thread(child_thread).messages_file.read_text(encoding="utf-8")
    assert "Review change A" in child_messages
    assert "Child review result" in child_messages
    assert engine.state_store.thread_id == "agent"


@pytest.mark.asyncio
async def test_blocking_subagent_can_ask_user_through_parent_session(
    temp_data_dir, temp_workspace
):
    (temp_data_dir / "config" / "permissions.yaml").write_text(
        "allow:\n  - tool: ask_user\n",
        encoding="utf-8",
    )
    agents_dir = temp_workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "interviewer.md").write_text(
        "---\n"
        "description: Ask for missing details\n"
        "mode: subagent\n"
        "tools:\n  - ask_user\n"
        "---\n"
        "Ask for the missing detail before answering.",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[
        {
            "content": "Delegating",
            "tool_calls": [{
                "name": "task",
                "args": {"agent": "interviewer", "prompt": "Clarify the target"},
                "id": "call_task",
            }],
        },
        {
            "content": "Need input",
            "tool_calls": [{
                "name": "ask_user",
                "args": {
                    "question": "Which target?",
                    "options": [
                        {"label": "A", "description": "Use target A"},
                        {"label": "B", "description": "Use target B"},
                    ],
                },
                "id": "call_ask",
            }],
        },
        {"content": "The target is A"},
        {"content": "Parent received the clarification"},
    ])
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    engine = await bootstrap(
        paths=paths,
        session_id="interaction-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        llm_override=llm,
    )
    await engine.start_session()
    runtime = SessionRuntime(
        session_id="interaction-session",
        thread_id="agent",
        provider_name="default",
        paths=paths,
        workspace_root=str(temp_workspace),
        no_plugins=False,
        engine=engine,
    )

    events = []
    async for event in runtime.stream_message("Clarify this", "request-1"):
        events.append(event)
        if event["type"] == "user_input_required":
            engine.user_input_waiter.answer(
                event["data"]["request_id"], answer="A"
            )

    assert any(event["type"] == "user_input_required" for event in events)
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "Parent received the clarification"
        for event in events
    )
    await runtime.close()
    await asyncio.get_running_loop().shutdown_default_executor()


@pytest.mark.asyncio
async def test_blocking_subagent_can_request_permission_through_parent_session(
    temp_data_dir, temp_workspace
):
    (temp_workspace / "target.txt").write_text("target content", encoding="utf-8")
    agents_dir = temp_workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reader.md").write_text(
        "---\n"
        "description: Read one requested file\n"
        "mode: subagent\n"
        "tools:\n  - filesystem_read\n"
        "---\n"
        "Read only the requested file.",
        encoding="utf-8",
    )
    llm = MockLLM(responses=[
        {
            "content": "Delegating",
            "tool_calls": [{
                "name": "task",
                "args": {"agent": "reader", "prompt": "Read target.txt"},
                "id": "call_task",
            }],
        },
        {
            "content": "Reading",
            "tool_calls": [{
                "name": "filesystem_read",
                "args": {"path": "target.txt"},
                "id": "call_read",
            }],
        },
        {"content": "The file contains target content"},
        {"content": "Parent received the file result"},
    ])
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    engine = await bootstrap(
        paths=paths,
        session_id="permission-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        llm_override=llm,
    )
    await engine.start_session()
    runtime = SessionRuntime(
        session_id="permission-session",
        thread_id="agent",
        provider_name="default",
        paths=paths,
        workspace_root=str(temp_workspace),
        no_plugins=False,
        engine=engine,
    )

    events = []
    async for event in runtime.stream_message("Read this", "request-1"):
        events.append(event)
        if event["type"] == "permission_request":
            engine.permission_waiter.answer(
                event["data"]["request_id"],
                decision="allow",
                scope="once",
            )

    assert any(event["type"] == "permission_request" for event in events)
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "Parent received the file result"
        for event in events
    )
    await runtime.close()
    await asyncio.get_running_loop().shutdown_default_executor()


@pytest.mark.asyncio
async def test_primary_agent_configures_engine_and_resumes_from_thread_metadata(
    temp_data_dir, temp_workspace
):
    agents_dir = temp_workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        "---\n"
        "description: Build focused changes\n"
        "mode: primary\n"
        "tools: []\n"
        "---\n"
        "Follow the builder workflow.",
        encoding="utf-8",
    )
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    first_llm = MockLLM(responses=[{"content": "built"}])
    first = await bootstrap(
        paths=paths,
        session_id="primary-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        selected_agent="builder",
        llm_override=first_llm,
    )
    await first.start_session()

    _ = [event async for event in first.run_turn("build")]

    assert first.config.agent_name == "builder"
    assert first.tool_registry.get_all() == []
    assert "Follow the builder workflow." in "\n".join(
        str(message.content) for message in first_llm.get_call_messages(0)
    )
    assert first.state_store.read_thread_metadata()["agent"] == "builder"
    await first.close_session()

    resumed = await bootstrap(
        paths=paths,
        session_id="primary-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        llm_override=MockLLM(responses=[]),
    )
    await resumed.start_session()

    assert resumed.config.agent_name == "builder"
    assert [message.content for message in resumed.messages] == ["build", "built"]
    await resumed.close_session()


@pytest.mark.asyncio
async def test_unknown_primary_agent_does_not_leave_new_session(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path)

    with pytest.raises(ValueError, match="Unknown primary agent"):
        await bootstrap(
            paths=paths,
            session_id="invalid-primary",
            selected_agent="missing",
            llm_override=MockLLM(responses=[]),
        )

    assert not paths.session("invalid-primary").root.exists()


@pytest.mark.asyncio
async def test_invalid_workspace_agent_fails_startup_and_rolls_back_session(
    tmp_path
):
    workspace = tmp_path / "workspace"
    agents_dir = workspace / ".xbot" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "invalid.md").write_text(
        "---\n"
        "description: Invalid definition\n"
        "unsupported: true\n"
        "---\n"
        "Prompt.",
        encoding="utf-8",
    )
    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    with pytest.raises(ValueError, match="Unknown Agent fields"):
        await bootstrap(
            paths=paths,
            session_id="invalid-definition",
            workspace_root=workspace,
            llm_override=MockLLM(responses=[]),
        )

    assert not paths.session("invalid-definition").root.exists()


@pytest.mark.asyncio
async def test_subagent_manager_rejects_unknown_and_primary_agents(tmp_path):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(name="primary", description="Primary", mode="primary"),
        owner="test",
    )

    async def unused_factory(*_args):
        raise AssertionError("factory must not run")

    manager = SubagentManager(
        registry=registry,
        session_paths=RuntimePaths.from_data_dir(tmp_path).session("s"),
        parent_thread_id="agent",
        engine_factory=unused_factory,
    )

    assert (await manager.run("missing", "work")).error.code == "agent_not_found"
    assert (await manager.run("primary", "work")).error.code == "agent_not_found"


@pytest.mark.asyncio
async def test_background_subagent_requires_live_mailbox(tmp_path):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(name="worker", description="Do focused work"),
        owner="test",
    )

    async def unused_factory(*_args):
        raise AssertionError("factory must not run")

    manager = SubagentManager(
        registry=registry,
        session_paths=RuntimePaths.from_data_dir(tmp_path).session("s"),
        parent_thread_id="agent",
        engine_factory=unused_factory,
    )

    result = await manager.run("worker", "work", background=True)

    assert result.error.code == "background_unavailable"


def test_child_permissions_cannot_expand_parent_policy():
    parent = PermissionSystem({"ask": [{"tool": "shell"}]}, default_decision="allow")
    child = PermissionSystem({"allow": [{"tool": "shell"}]}, default_decision="allow")
    permissions = PermissionIntersection(parent, child)

    assert permissions.check("shell", {"command": "pwd"}) == "ask"


def test_child_permissions_can_restrict_parent_policy():
    parent = PermissionSystem({"allow": [{"tool": "shell"}]})
    child = PermissionSystem({"deny": [{"tool": "shell"}]}, default_decision="allow")
    permissions = PermissionIntersection(parent, child)

    assert permissions.check("shell", {"command": "pwd"}) == "deny"


class _ChildEngine:
    def __init__(
        self,
        *,
        wait: asyncio.Event | None = None,
        output: str = "background result",
    ) -> None:
        self.wait = wait
        self.output = output
        self.closed = False
        self.session_usage = {"total_tokens": 12}

    async def start_session(self) -> None:
        return None

    async def run_turn(self, _prompt):
        if self.wait is not None:
            await self.wait.wait()
        yield {"type": "assistant_message", "data": {"content": self.output}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def close_session(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_background_subagent_returns_immediately_and_completes(tmp_path):
    registry = AgentRegistry()
    definition = AgentDefinition(name="worker", description="Do focused work")
    registry.register(definition, owner="test")
    release = asyncio.Event()
    child = _ChildEngine(wait=release)

    async def factory(*_args):
        return child

    manager = SubagentManager(
        registry=registry,
        session_paths=RuntimePaths.from_data_dir(tmp_path).session("s"),
        parent_thread_id="agent",
        engine_factory=factory,
    )
    completed = asyncio.Event()
    manager.on_complete = lambda _task: _set_event(completed)

    started = await manager.run("worker", "Do work", background=True)

    assert started.status == "success"
    assert started.data["status"] == "pending"
    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    task = manager.snapshots()[0]
    assert task["status"] == "completed"
    assert task["output"] == "background result"
    assert task["usage"] == {"total_tokens": 12}
    assert child.closed is True


@pytest.mark.asyncio
async def test_session_runtime_buffers_background_subagent_completion(tmp_path):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(name="worker", description="Do focused work"),
        owner="test",
    )

    async def factory(*_args):
        return _ChildEngine()

    paths = RuntimePaths.from_data_dir(tmp_path)
    state_store = CoreStateStore.create(
        paths.session("s"),
        thread_id="agent",
        workspace_root=str(tmp_path),
        provider="default",
    )
    manager = SubagentManager(
        registry=registry,
        session_paths=paths.session("s"),
        parent_thread_id="agent",
        engine_factory=factory,
    )

    class ParentEngine:
        background_tasks = None
        subagents = manager
        enqueue_mailbox = None

    parent_engine = ParentEngine()
    parent_engine.state_store = state_store

    runtime = SessionRuntime(
        session_id="s",
        thread_id="agent",
        provider_name="default",
        paths=paths,
        workspace_root=str(tmp_path),
        no_plugins=False,
        engine=parent_engine,
    )

    await manager.run("worker", "Do work", background=True)
    for _ in range(20):
        if manager.snapshots()[0]["status"] == "completed":
            break
        await asyncio.sleep(0)

    assert runtime.mailbox.size == 1
    item = await runtime.mailbox.get()
    assert item.kind == "general"
    assert item.message["source"] == "subagent"
    assert item.message["data"]["output"] == "background result"

    await runtime._enqueue_subagent_completion({
        "task_id": "agent-task-long",
        "status": "completed",
        "agent": "worker",
        "output": "x" * 13_000,
    })
    long_item = await runtime.mailbox.get()
    bounded = long_item.message["data"]["output"]
    assert bounded.startswith("[Long context cached]")
    assert "cache_path: session/artifacts/context/" in bounded
    assert list((state_store.artifacts_dir / "context").glob("*.txt"))


@pytest.mark.asyncio
async def test_background_subagent_stop_cancels_and_closes_child(tmp_path):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(name="worker", description="Do focused work"),
        owner="test",
    )
    child = _ChildEngine(wait=asyncio.Event())

    async def factory(*_args):
        return child

    manager = SubagentManager(
        registry=registry,
        session_paths=RuntimePaths.from_data_dir(tmp_path).session("s"),
        parent_thread_id="agent",
        engine_factory=factory,
    )
    manager.on_complete = lambda _task: _set_event(asyncio.Event())
    started = await manager.run("worker", "Wait", background=True)
    task_id = started.data["task_id"]
    for _ in range(20):
        if manager.snapshots()[0]["status"] == "running":
            break
        await asyncio.sleep(0)

    stopped = await manager.stop_task(task_id)

    assert stopped.status == "success"
    assert stopped.data["status"] == "stopped"
    assert child.closed is True


async def _set_event(event: asyncio.Event) -> None:
    event.set()
