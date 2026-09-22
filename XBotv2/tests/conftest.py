"""Shared test fixtures for XBotv2 core tests.

Principles:
- No module-level state — all caches are constructor-injected.
- temp_data_dir only — never real data/sessions/.
- MockLLM for deterministic responses.
- Each test creates its own engine — no shared state.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _local_proxy_environment():
    """Keep the ambient proxy configuration out of the suite.

    Every HTTP test here talks to a server on this machine, and ``httpx`` parses
    each ``NO_PROXY`` entry as a URL when it builds a client -- so an entry such
    as ``[::1]`` (its port parses as ``":1]"``) makes ``httpx.AsyncClient()``
    raise before a request is ever made. That is a property of the machine, not
    of the code under test.

    Only the *ambient* settings are normalised; the client's own behaviour is
    covered by ``tests/core/test_client.py``, which asserts that a loopback
    target ignores them and a remote one does not.
    """
    names = ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY",
             "https_proxy", "ALL_PROXY", "all_proxy")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


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
