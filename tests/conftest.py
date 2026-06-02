"""Shared test fixtures and helpers for XBot Hermes tests."""

import shutil
import tempfile
from pathlib import Path

import pytest

from xbot.hooks import LoopHooks, build_default_hooks
from xbot.registry import ToolRegistry, bootstrap_registry
from xbot.tool_runtime import register_default_guard_hooks


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
    """Create a LoopHooks with all default guard hooks registered."""
    hooks = build_default_hooks()
    register_default_guard_hooks(hooks)
    return hooks


def make_default_registry() -> ToolRegistry:
    """Create a ToolRegistry bootstrapped from all built-in tools."""
    return bootstrap_registry()
