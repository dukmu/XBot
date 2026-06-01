"""Test bootstrap and shared fixtures."""
from pathlib import Path
import shutil
import sys
import tempfile

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xbot.mock_llm import MockLLM
from xbot.models import UserContext


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp(prefix="agent_test_")
    data_dir = Path(temp_dir) / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "workspace").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "cache").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "subagents").mkdir(parents=True)
    (data_dir / "personalities" / "default").mkdir(parents=True)
    yield data_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def user_context():
    """Default user context for tests."""
    return UserContext(
        user_id="test_user",
        user_name="TestUser",
        platform="local",
        session_type="private",
    )


@pytest_asyncio.fixture
async def mock_llm():
    """Create a mock LLM instance."""
    llm = MockLLM(response_sequence=[{"content": "OK"}])
    yield llm
    llm.reset()
