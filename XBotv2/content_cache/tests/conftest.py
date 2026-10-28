import pytest
from XBotv2.persistence.impl.artifacts import ArtifactStore
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.tests.conftest import temp_data_dir


@pytest.fixture
def artifact_store(temp_data_dir):
    paths = RuntimePaths.from_data_dir(temp_data_dir).session("test-session").thread("test-thread")
    return ArtifactStore(paths)
