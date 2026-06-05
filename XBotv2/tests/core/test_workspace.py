"""Tests for session workspace initialization."""

import shutil

import pytest
import yaml

from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.core.workspace import SessionWorkspace
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookStage
from xbotv2.llm.mock import MockLLM
from xbotv2.persistence.store import CoreStateStore
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


def make_workspace_engine(temp_data_dir, hook_manager=None):
    """Create an engine wired to the real session workspace manager."""
    session_id = "test-session"
    thread_id = "test-thread"
    session_root = temp_data_dir / "sessions" / session_id
    store = CoreStateStore.create(
        session_root / "state",
        session_id=session_id,
        thread_id=thread_id,
        personality_id="default",
    )
    workspace_root = session_root / "workspace"
    return Engine(
        llm=MockLLM(responses=[]),
        tool_registry=ToolRegistry(),
        hook_manager=hook_manager or HookManager(),
        state_store=store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(enabled=False, workspace_root=workspace_root),
        permission_system=PermissionSystem(default_decision="allow"),
        workspace=SessionWorkspace(
            workspace_root,
            session_id=session_id,
            thread_id=thread_id,
            base_root=session_root,
        ),
        config=None,
    )


@pytest.mark.asyncio
async def test_start_session_initializes_workspace_before_start_hook(temp_data_dir):
    calls = []

    async def on_start(ctx):
        workspace = ctx.state["messages"]  # Hook context remains otherwise unchanged.
        assert workspace == []
        root = temp_data_dir / "sessions" / "test-session" / "workspace"
        assert root.exists()
        assert (root / ".xbot" / "workspace.yaml").exists()
        calls.append(ctx.stage.value)

    hooks = HookManager()
    hooks.register(HookStage.ON_SESSION_START, on_start)
    engine = make_workspace_engine(temp_data_dir, hooks)

    await engine.start_session()

    assert calls == ["on_session_start"]
    state = engine.state_store.read_state()
    workspace_state = state["workspace"]
    assert workspace_state["root"].endswith("sessions/test-session/workspace")
    assert workspace_state["lifecycle"] == "start"
    assert workspace_state["status"] == "created"
    assert engine.state_store.read_events()[0]["type"] == "workspace_initialized"

    metadata = yaml.safe_load(
        (temp_data_dir / "sessions" / "test-session" / "workspace" / ".xbot" / "workspace.yaml")
        .read_text(encoding="utf-8")
    )
    assert metadata["session_id"] == "test-session"
    assert metadata["thread_id"] == "test-thread"


@pytest.mark.asyncio
async def test_workspace_event_does_not_turn_first_start_into_resume(temp_data_dir):
    calls = []

    async def record_call(ctx):
        calls.append(ctx.stage.value)

    hooks = HookManager()
    hooks.register(HookStage.ON_SESSION_START, record_call)
    hooks.register(HookStage.ON_SESSION_RESUME, record_call)

    engine = make_workspace_engine(temp_data_dir, hooks)
    await engine.start_session()

    assert calls == ["on_session_start"]


@pytest.mark.asyncio
async def test_resume_preserves_workspace_files(temp_data_dir):
    engine1 = make_workspace_engine(temp_data_dir)
    await engine1.start_session()

    workspace_root = temp_data_dir / "sessions" / "test-session" / "workspace"
    user_file = workspace_root / "files" / "kept.txt"
    user_file.write_text("keep me", encoding="utf-8")

    hooks = HookManager()
    calls = []

    async def record_call(ctx):
        calls.append(ctx.stage.value)

    hooks.register(HookStage.ON_SESSION_RESUME, record_call)
    engine2 = make_workspace_engine(temp_data_dir, hooks)
    await engine2.start_session()

    assert calls == ["on_session_resume"]
    assert user_file.read_text(encoding="utf-8") == "keep me"
    state = engine2.state_store.read_state()
    assert state["workspace"]["lifecycle"] == "resume"
    assert state["workspace"]["status"] == "ready"


@pytest.mark.asyncio
async def test_resume_recovers_missing_workspace(temp_data_dir):
    engine1 = make_workspace_engine(temp_data_dir)
    await engine1.start_session()

    workspace_root = temp_data_dir / "sessions" / "test-session" / "workspace"
    shutil.rmtree(workspace_root)

    engine2 = make_workspace_engine(temp_data_dir)
    await engine2.start_session()

    assert workspace_root.exists()
    assert (workspace_root / ".xbot" / "workspace.yaml").exists()
    assert (workspace_root / "files").exists()
    assert (workspace_root / "tmp").exists()
    events = engine2.state_store.read_events()
    assert events[-1]["type"] == "workspace_recovered"
    assert engine2.state_store.read_state()["workspace"]["status"] == "recovered"


def test_workspace_rejects_root_outside_session_root(tmp_path):
    workspace = SessionWorkspace(
        tmp_path / "outside",
        session_id="s1",
        thread_id="t1",
        base_root=tmp_path / "session",
    )

    with pytest.raises(ValueError, match="Workspace root"):
        workspace.ensure("start")
