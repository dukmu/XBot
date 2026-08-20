"""Core test fixtures — no built-in/Phase4 plugins loaded."""

import pytest

import xcore
from XBotv2.agentloop import EventContext, Events
from XBotv2.session import SessionInfo
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.permissions.system import PermissionSystem
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import CoreStateStore
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
    return PermissionSystem(default_decision="ask")


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
    """CoreStateStore in temp directory."""
    store = CoreStateStore.create(
        RuntimePaths.from_data_dir(temp_data_dir).session("test-session"),
        thread_id="test-thread",
        workspace_root=str(temp_data_dir),
        provider="default",
    )
    return store


@pytest.fixture
def session_info():
    """Minimal SessionInfo."""
    return SessionInfo(
        session_id="test-session",
        thread_id="test-thread",
        workspace_root="/workspace",
        provider="default",
    )


@pytest.fixture
def event_context(session_info, tool_registry):
    """Basic EventContext for loop events."""
    return EventContext(
        messages=[],
        session=session_info,
    )
