"""Subagent collaboration uses session-owned threads and ordinary jobs."""

import pytest

from XBotv2.agents import AgentDefinition
from XBotv2.session import ThreadSummary
from XBotv2.subagents.contracts import SubagentAgentError
from XBotv2.subagents.service import AgentJobSpec, SubagentLauncher


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

    async def list_threads(self, session_id):
        assert session_id == "parent-session"
        return self.threads

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
        provider_name="mock",
        no_plugins=False,
    )
    return launcher, sessions


def test_agent_job_spec_binds_one_execution_to_its_stable_thread():
    spec = AgentJobSpec(
        agent="explorer",
        prompt="inspect",
        label="Inspect",
        thread_id="explorer-a1b2c3",
    )

    assert spec.kind == "subagent"
    assert spec.label == "Inspect"
    assert spec.thread_id == "explorer-a1b2c3"


def test_launcher_allocates_only_registered_non_primary_agents():
    launcher, _sessions = _launcher(_definition())
    assert launcher.allocate("explorer") == "child-explorer"

    for definition in (None, _definition("default", "primary")):
        launcher, _sessions = _launcher(definition)
        with pytest.raises(SubagentAgentError):
            launcher.allocate("explorer")


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
