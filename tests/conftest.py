"""Shared test fixtures and helpers for XBot Hermes tests."""

import shutil
import tempfile
from pathlib import Path

import pytest

from xbot.hooks.core import LoopHooks
from xbot.hooks import load_standard_hooks
from xbot.registry import ToolRegistry, bootstrap_registry
from xbot.config import RuntimePaths
from xbot.models import UserContext
from xbot.runtime import (
    PersonalityProjection,
    RuntimeContext,
    RuntimeFrame,
    SandboxProjection,
    TaskProjection,
    ToolRegistrySnapshot,
)


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp(prefix="xbot_test_")
    data_dir = Path(temp_dir) / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "workspace").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "cache").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "subagents").mkdir(parents=True)
    (data_dir / "personalities" / "default").mkdir(parents=True)
    yield data_dir
    shutil.rmtree(temp_dir)


def make_default_hooks() -> LoopHooks:
    """Create a LoopHooks with all standard hooks registered."""
    hooks = load_standard_hooks()

    async def inject_test_runtime_frame(ctx):
        state = ctx.get("state") or {}
        if "runtime_frame" in state:
            return None
        user = state.get("user_context")
        if isinstance(user, dict):
            user = UserContext(**user)
        if not isinstance(user, UserContext):
            return None
        frame = RuntimeFrame(
            runtime=RuntimeContext(
                paths=RuntimePaths(data_dir=Path("/tmp/xbot-test-data"), session_id="default", personality_id="default"),
                thread_id="test-thread",
                task_id="agent",
                run_id="run_test",
                trace_id="trace_test",
            ),
            user=user,
            personality=PersonalityProjection(
                name="default",
                agent_role="Test Hermes agent.",
                system_template="User: {{ user_context.user_name }}\nRole: {{ agent_config.agent_role }}",
                instructions="Follow the test request.",
                memory="No test memory.",
                skills_summary="No test skills.",
            ),
            sandbox=SandboxProjection(summary="test sandbox"),
            tools=ToolRegistrySnapshot(names=(), sandbox_modes={}),
            task=TaskProjection(context_text="Test task projection.", pending_mailbox_items=0),
            system_notice="test runtime",
            active_subagents=(),
        )
        state.update(frame.to_context_state())
        return None

    hooks.register("before_agent", inject_test_runtime_frame)
    return hooks


def make_default_registry() -> ToolRegistry:
    """Create a ToolRegistry bootstrapped from all built-in tools."""
    return bootstrap_registry()
