"""Subagent job execution tests.

Subagents run as SUBAGENT jobs in the shared JobRegistry; the agents plugin
owns the SubagentRunner, and core only provides the AgentRuntime spawn hook.
"""

import asyncio
import json
import xml.etree.ElementTree as ET

import pytest

from XBotv2.core import AgentDefinition, RuntimePaths
from XBotv2.jobs import JobKind, JobRegistry
from XBotv2.core.messages import ModelChunk
from XBotv2.tools.agents import AgentRegistry
from XBotv2.session.session import Session
from XBotv2.bootstrap import bootstrap
from XBotv2.agentloop.session import SessionRuntime
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import CoreStateStore
from XBotv2.permissions.system import PermissionIntersection, PermissionSystem

from XBotv2.agents.plugin import SubagentRunner


class RoutingLLM(MockLLM):
    """Routes responses by the child's system prompt so parent and child turns
    are deterministic even though the child runs asynchronously."""

    def __init__(self, *, child_marker, parents, children):
        super().__init__([])
        self._child_marker = child_marker
        self._parents = list(parents)
        self._children = list(children)
        self._pi = 0
        self._ci = 0

    async def _astream_once(self, messages, **_kwargs):
        system = "\n".join(
            str(message.content) for message in messages if message.role == "system"
        )
        if self._child_marker in system:
            if self._ci >= len(self._children):
                raise RuntimeError("child responses exhausted")
            response = self._children[self._ci]
            self._ci += 1
        else:
            if self._pi >= len(self._parents):
                raise RuntimeError("parent responses exhausted")
            response = self._parents[self._pi]
            self._pi += 1
        result = self.to_response(response)
        self.call_history.append(list(messages))
        yield ModelChunk(
            content=result.content,
            reasoning=result.reasoning,
            tool_calls=result.tool_calls,
            response_metadata=result.response_metadata,
            usage_metadata=result.usage_metadata,
            additional_kwargs=result.additional_kwargs,
        )
        yield result


@pytest.mark.asyncio
async def test_subagent_flow_runs_child_and_returns_to_parent(
    temp_data_dir, temp_workspace
):
    (temp_data_dir / "config" / "config.yaml").write_text(
        "permissions:\n  allow:\n    - tool: spawn_subagent\n    - tool: wait_subagent\n    - tool: read_subagent\n",
        encoding="utf-8",
    )
    agents_dir = temp_workspace / ".agents"
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
    llm = RoutingLLM(
        child_marker="Act as the workspace reviewer",
        parents=[
            {
                "content": "Delegating review",
                "tool_calls": [{
                    "name": "spawn_subagent",
                    "args": {"agent": "reviewer", "prompt": "Review change A"},
                    "id": "call_spawn",
                }],
            },
            {
                "content": "Waiting",
                "tool_calls": [{
                    "name": "wait_subagent",
                    "args": {"ids": ["sa_1"]},
                    "id": "call_wait",
                }],
            },
            {
                "content": "Reading",
                "tool_calls": [{
                    "name": "read_subagent",
                    "args": {"id": "sa_1"},
                    "id": "call_read",
                }],
            },
            {"content": "Parent summary"},
        ],
        children=[{"content": "Child review result"}],
    )
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
    read_results = [
        event for event in events
        if event["type"] == "tool_result"
        and event["data"]["name"] == "read_subagent"
    ]
    assert read_results
    assert read_results[0]["data"]["content"] == "Child review result"
    assert any(
        event["type"] == "assistant_message"
        and event["data"]["content"] == "Parent summary"
        for event in events
    )
    assert "reviewer: Review a focused change" in "\n".join(
        str(message.content) for message in llm.get_call_messages(0)
    )
    assert all(
        str(messages[0].content).count("<core_instructions>") == 1
        for messages in (
            llm.get_call_messages(0),
            llm.get_call_messages(1),
        )
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
    await engine.close_session()


@pytest.mark.asyncio
async def test_subagent_can_ask_user_through_parent_session(
    temp_data_dir, temp_workspace
):
    (temp_data_dir / "config" / "config.yaml").write_text(
        "permissions:\n  allow:\n"
        "    - tool: spawn_subagent\n"
        "    - tool: wait_subagent\n"
        "    - tool: ask_user\n",
        encoding="utf-8",
    )
    agents_dir = temp_workspace / ".agents"
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
    llm = RoutingLLM(
        child_marker="Ask for the missing detail",
        parents=[
            {
                "content": "Delegating",
                "tool_calls": [{
                    "name": "spawn_subagent",
                    "args": {"agent": "interviewer", "prompt": "Clarify the target"},
                    "id": "call_spawn",
                }],
            },
            {
                "content": "Waiting",
                "tool_calls": [{
                    "name": "wait_subagent",
                    "args": {"ids": ["sa_1"]},
                    "id": "call_wait",
                }],
            },
            {"content": "Parent received the clarification"},
        ],
        children=[
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
        ],
    )
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
async def test_subagent_can_request_permission_through_parent_session(
    temp_data_dir, temp_workspace
):
    from XBotv2.tests.core.test_bootstrap import _write_plugins

    _write_plugins(temp_data_dir, {"permissions": {"config": {
        "permissions": {
            "allow": [{"tool": "spawn_subagent"}, {"tool": "wait_subagent"}],
            "ask": [{"tool": "filesystem_read"}],
        },
    }}})
    (temp_workspace / "target.txt").write_text("target content", encoding="utf-8")
    agents_dir = temp_workspace / ".agents"
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
    llm = RoutingLLM(
        child_marker="Read only the requested file",
        parents=[
            {
                "content": "Delegating",
                "tool_calls": [{
                    "name": "spawn_subagent",
                    "args": {"agent": "reader", "prompt": "Read target.txt"},
                    "id": "call_spawn",
                }],
            },
            {
                "content": "Waiting",
                "tool_calls": [{
                    "name": "wait_subagent",
                    "args": {"ids": ["sa_1"]},
                    "id": "call_wait",
                }],
            },
            {"content": "Parent received the file result"},
        ],
        children=[
            {
                "content": "Reading",
                "tool_calls": [{
                    "name": "filesystem_read",
                    "args": {"path": "target.txt"},
                    "id": "call_read",
                }],
            },
            {"content": "The file contains target content"},
        ],
    )
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
    agents_dir = temp_workspace / ".agents"
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

    (agents_dir / "builder.md").write_text(
        "---\n"
        "description: Changed builder\n"
        "mode: primary\n"
        "tools:\n  - filesystem_read\n"
        "---\n"
        "Changed instructions must not alter an existing thread.",
        encoding="utf-8",
    )

    resumed = await bootstrap(
        paths=paths,
        session_id="primary-session",
        thread_id="agent",
        workspace_root=temp_workspace,
        llm_override=MockLLM(responses=[]),
    )
    await resumed.start_session()

    assert resumed.config.agent_name == "builder"
    assert resumed.config.agent_role == "Build focused changes"
    assert resumed.tool_registry.get_all() == []
    assert "Follow the builder workflow." in resumed.config.agent_instructions
    assert "Changed instructions" not in resumed.config.agent_instructions
    assert [message.content for message in resumed.messages] == ["build", "built"]
    await resumed.close_session()


@pytest.mark.asyncio
async def test_workspace_agent_overrides_builtin_definition(
    temp_data_dir, temp_workspace
):
    builtin_dir = temp_data_dir / ".agents"
    builtin_dir.mkdir()
    (builtin_dir / "reviewer.md").write_text(
        "---\ndescription: Built-in reviewer\nmode: all\n---\nBuilt-in prompt.",
        encoding="utf-8",
    )
    workspace_dir = temp_workspace / ".agents"
    workspace_dir.mkdir()
    (workspace_dir / "reviewer.md").write_text(
        "---\n"
        "description: Workspace reviewer\n"
        "mode: all\n"
        "model: default/test-model\n"
        "temperature: 0.1\n"
        "max_output_tokens: 2048\n"
        "context_window: 64000\n"
        "steps: 7\n"
        "tools:\n"
        "  filesystem_read: true\n"
        "  filesystem_write: false\n"
        "permission:\n  deny:\n    - tool: shell\n"
        "---\nWorkspace prompt.",
        encoding="utf-8",
    )
    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="agent-precedence",
        workspace_root=temp_workspace,
        selected_agent="reviewer",
        llm_override=MockLLM(responses=[]),
    )

    definition = engine.agent_registry.get("reviewer")
    assert definition.description == "Workspace reviewer"
    assert definition.provider == "default"
    assert definition.model == "test-model"
    assert definition.temperature == 0.1
    assert definition.max_output_tokens == 2048
    assert definition.disabled_tools == ("filesystem_write",)
    definition_permissions = PermissionSystem(definition.permissions)
    assert definition_permissions.check("filesystem_read") == "allow"
    assert definition_permissions.check("filesystem_write") == "deny"
    assert definition_permissions.check("shell") == "deny"
    assert engine.model == "test-model"
    assert engine.context_window == 64000
    assert engine.max_iterations == 7
    assert engine.tool_registry.get("filesystem_write") is None
    await engine.close_session()


@pytest.mark.asyncio
async def test_new_primary_thread_selects_builtin_default_agent(
    temp_data_dir, temp_workspace
):
    builtin_dir = temp_data_dir / ".agents"
    builtin_dir.mkdir()
    (builtin_dir / "default.md").write_text(
        "---\ndescription: Default coding agent\nmode: all\n---\nDefault prompt.",
        encoding="utf-8",
    )
    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="default-agent",
        workspace_root=temp_workspace,
        llm_override=MockLLM(responses=[]),
    )

    assert engine.config.agent_name == "default"
    assert "Default prompt." in engine.config.agent_instructions
    assert engine.state_store.read_thread_metadata()["agent"] == "default"
    await engine.close_session()


@pytest.mark.asyncio
async def test_subagent_runtime_does_not_load_agents_plugin(
    temp_data_dir, temp_workspace
):
    definition = AgentDefinition(
        name="worker",
        description="Focused worker",
        mode="subagent",
    )
    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="nested-disabled",
        thread_id="worker-1",
        workspace_root=temp_workspace,
        agent_definition=definition,
        is_subagent=True,
        llm_override=MockLLM(responses=[]),
    )

    assert engine.plugin_loader.get_command("agent") is None
    assert engine.tool_registry.get_registered("spawn_subagent") is None
    assert engine.tool_registry.get_registered("wait_subagent") is None
    await engine.close_session()


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
    agents_dir = workspace / ".agents"
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


def _make_session(tmp_path, *, registry, factory):
    import types

    class FakeCtx:
        def __init__(self, registry):
            self.agents = types.SimpleNamespace(registry=registry)

    return Session(
        FakeCtx(registry),
        session_id="s",
        thread_id="agent",
        workspace_root=str(tmp_path),
        paths=None,
        variables=None,
        state_store=None,
        session_paths=RuntimePaths.from_data_dir(tmp_path).session("s"),
        parent_thread_id="agent",
        engine_factory=factory,
    )


@pytest.mark.asyncio
async def test_agent_runtime_rejects_unknown_and_primary_agents(tmp_path):
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(name="primary", description="Primary", mode="primary"),
        owner="test",
    )

    async def unused_factory(*_args):
        raise AssertionError("factory must not run")

    runtime = _make_session(tmp_path, registry=registry, factory=unused_factory)

    with pytest.raises(Exception) as missing:
        await runtime.spawn_subagent("missing", "work")
    assert getattr(missing.value, "code", "") == "agent_not_found"
    with pytest.raises(Exception) as primary:
        await runtime.spawn_subagent("primary", "work")
    assert getattr(primary.value, "code", "") == "agent_not_found"


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
    agent_registry = AgentRegistry()
    definition = AgentDefinition(name="worker", description="Do focused work")
    agent_registry.register(definition, owner="test")
    release = asyncio.Event()
    child = _ChildEngine(wait=release)

    async def factory(_definition, _thread_id, background):
        del background
        return child

    runtime = _make_session(
        tmp_path, registry=agent_registry, factory=factory
    )
    job_registry = JobRegistry()
    job = await job_registry.create(
        kind=JobKind.SUBAGENT, metadata={"agent": "worker"}
    )
    job_registry.start(
        job.id,
        SubagentRunner(session=runtime, agent="worker", prompt="Do work"),
    )

    assert job.status.value in {"pending", "running"}
    release.set()
    await asyncio.wait_for(job_registry.wait([job.id]), timeout=1)

    assert job.status.value == "completed"
    assert job.result.data["usage"] == {"total_tokens": 12}
    store = job.result.output_store
    assert (await store.read(max_bytes=100_000)).data == "background result"
    assert child.closed is True


@pytest.mark.asyncio
async def test_session_runtime_buffers_background_subagent_completion(tmp_path):
    agent_registry = AgentRegistry()
    agent_registry.register(
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
    job_registry = JobRegistry()
    runtime_impl = _make_session(
        tmp_path, registry=agent_registry, factory=factory
    )

    class ParentEngine:
        plugin_loader = None
        enqueue_mailbox = None

    parent_engine = ParentEngine()
    parent_engine.job_registry = job_registry
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

    job = await job_registry.create(
        kind=JobKind.SUBAGENT, metadata={"agent": "worker"}
    )
    job_registry.start(
        job.id,
        SubagentRunner(session=runtime_impl, agent="worker", prompt="Do work"),
    )
    for _ in range(20):
        if job.status.value == "completed":
            break
        await asyncio.sleep(0)

    # Completions stage into the agent inbox with a small notification; they
    # do not wake a turn and do not carry the full subagent output.
    await runtime.turn_lock.acquire()
    await runtime._collect_completion({
        "type": "subagent",
        "kind": "subagent",
        "task_id": "sa_1",
        "status": "completed",
        "agent": "worker",
        "command": "",
        "data": {"output": "background result"},
    })
    await runtime._collect_completion({
        "type": "subagent",
        "kind": "subagent",
        "task_id": "sa_2",
        "status": "completed",
        "agent": "worker",
        "command": "",
        "data": {"output": "x" * 13_000},
    })
    assert not runtime.pending_fold
    assert len(runtime.inbox) == 2
    staged = {message.source: message for message in runtime.inbox.pending}
    assert "sa_1" in staged
    short = staged["sa_1"]
    assert short.type == "subagent"
    assert short.payload["status"] == "completed"
    assert short.payload["agent"] == "worker"
    # The notification stays small; the full output is not staged.
    assert "output" not in short.payload
    runtime.turn_lock.release()
    await runtime.close()


@pytest.mark.asyncio
async def test_background_subagent_stop_cancels_and_closes_child(tmp_path):
    agent_registry = AgentRegistry()
    agent_registry.register(
        AgentDefinition(name="worker", description="Do focused work"),
        owner="test",
    )
    child = _ChildEngine(wait=asyncio.Event())

    async def factory(*_args):
        return child

    runtime = _make_session(
        tmp_path, registry=agent_registry, factory=factory
    )
    job_registry = JobRegistry()
    job = await job_registry.create(
        kind=JobKind.SUBAGENT, metadata={"agent": "worker"}
    )
    job_registry.start(
        job.id,
        SubagentRunner(session=runtime, agent="worker", prompt="Wait"),
    )
    for _ in range(20):
        if job.status.value == "running":
            break
        await asyncio.sleep(0)

    result = await job_registry.cancel(job.id)

    assert result.cancelled is True
    assert job.status.value == "cancelled"
    assert child.closed is True


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
