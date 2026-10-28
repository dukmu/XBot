import os

import pytest

from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.persistence.impl.artifacts import ArtifactStore


async def test_artifact_import_export_preserves_identity_and_existing_objects(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    source = ArtifactStore(paths.session("source").thread("main"))
    destination = ArtifactStore(paths.session("destination").thread("main"))
    ref = (await source.put(ArtifactKind.ATTACHMENT, b"original payload", suffix=".txt"))
    exported = tmp_path / "exported"
    await source.export_file(ref, exported)
    await destination.import_file(exported, ref)
    assert (await destination.read(ref)) == b"original payload"

    # A fork can share the CAS inode; failed imports must not damage either copy.
    fork = tmp_path / "fork-object"
    os.link(destination.model_path(ref), fork)
    conflicting = ref.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError, match="identity and hash"):
        await destination.import_file(exported, conflicting)
    exported.write_bytes(b"incorrect data!")
    with pytest.raises(ValueError, match="hash or size"):
        await destination.import_file(exported, ref)
    assert (await destination.read(ref)) == b"original payload"
    assert fork.read_bytes() == b"original payload"
