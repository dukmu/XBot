"""Core test fixtures — no built-in/Phase4 plugins loaded."""

import pytest

import xcore
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.permissions.system import PermissionSystem
from XBotv2.permissions import PermissionPolicy
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import ThreadPersistence
from XBotv2.core.paths import RuntimePaths


@pytest.fixture
def plugin_ctx():
    """Empty XCore plugin context."""
    return xcore.Context()


@pytest.fixture
def tool_registry():
    """Empty ToolRegistry."""
    return ToolRegistry()


@pytest.fixture
def permission_system():
    """Default PermissionSystem (ask on everything)."""
    return PermissionSystem(PermissionPolicy(default_decision="ask"))


@pytest.fixture
def sandbox_policy(temp_workspace):
    """SandboxPolicy with workspace."""
    return SandboxPolicy(
        enabled=False,
        workspace_root=str(temp_workspace),
        data_root=str(temp_workspace / "data"),
    )


@pytest.fixture
def context_builder():
    """Fresh ContextBuilder."""
    return ContextBuilder()


@pytest.fixture
def mock_llm():
    """MockLLM with no responses (configure per test)."""
    return MockLLM(responses=[])


@pytest.fixture
def state_store(temp_data_dir):
    """ThreadPersistence in a temporary data directory."""
    store = ThreadPersistence.create(
        RuntimePaths.from_data_dir(temp_data_dir).session("test-session"),
        thread_id="test-thread",
    )
    return store


@pytest.fixture
def artifact_store(state_store):
    return state_store.artifacts
