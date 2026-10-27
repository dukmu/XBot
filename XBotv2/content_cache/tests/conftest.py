import pytest
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.persistence.store import ThreadPersistence
from XBotv2.tests.conftest import temp_data_dir


@pytest.fixture
def artifact_store(temp_data_dir):
    store = ThreadPersistence.open(
        RuntimePaths.from_data_dir(temp_data_dir).session("test-session"),
        thread_id="test-thread",
    )
    return ArtifactStore(store.paths)
