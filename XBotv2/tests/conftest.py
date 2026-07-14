"""Shared test fixtures for XBotv2 core tests.

Principles:
- No module-level state — all caches are constructor-injected.
- temp_data_dir only — never real data/sessions/.
- MockLLM for deterministic responses.
- Each test creates its own engine — no shared state.
"""

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory with required subdirectories."""
    temp_dir = tempfile.mkdtemp(prefix="xbotv2_test_")
    data_dir = Path(temp_dir) / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "state").mkdir(parents=True)
    yield data_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    temp_dir = tempfile.mkdtemp(prefix="xbotv2_ws_")
    ws = Path(temp_dir)
    yield ws
    shutil.rmtree(temp_dir, ignore_errors=True)
