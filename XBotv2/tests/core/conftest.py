"""Core test fixtures — no built-in/Phase4 plugins loaded."""

import pytest

import xcore
from xbotv2.api.events import EventContext, Events
from xbotv2.api.runtime import SessionInfo
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.sandbox import SandboxPolicy
from xbotv2.core.context import ContextBuilder
from xbotv2.llm.mock import MockLLM
from xbotv2.persistence.store import CoreStateStore
from xbotv2.api.paths import RuntimePaths


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
        config=None,
        tools=tool_registry,
        plugin_store=None,
        session=session_info,
    )
