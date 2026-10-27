"""Filesystem implementation of the thread ArtifactStore contract."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef
from XBotv2.persistence.api.files import run_file_io, write_bytes_atomic
from XBotv2.core.api.paths import ThreadPaths
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog


class ArtifactStore:
    def __init__(
        self,
        paths: ThreadPaths,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self._paths = paths
        self._log = runtime_log.bind(
            "persistence",
            session_id=paths.session_id,
            thread_id=paths.thread_id,
        )

    def put(
        self,
        kind: ArtifactKind,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        suffix: str = "",
    ) -> ArtifactRef:
        if suffix and (not suffix.startswith(".") or "/" in suffix or "\\" in suffix):
            raise ValueError(f"Invalid artifact suffix: {suffix!r}")
        if name and Path(name).name != name:
            raise ValueError("artifact name must be a file name")
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"{kind.value}/{digest}{suffix}"
        path = self._paths.artifact_file(artifact_id)
        created = not path.exists()
        if created:
            write_bytes_atomic(path, payload)
        self._log.info(
            "persistence.artifact.stored",
            artifact_id=artifact_id,
            kind=kind.value,
            media_type=media_type,
            bytes=len(payload),
            created=created,
        )
        return ArtifactRef(
            id=artifact_id,
            kind=kind,
            media_type=media_type,
            name=name,
            size=len(payload),
            sha256=digest,
        )

    async def export_file(self, artifact: ArtifactRef, destination: Path) -> None:
        digest = _expected_digest(artifact)
        source = self._paths.artifact_file(artifact.id)
        await run_file_io(
            _copy_verified, source, Path(destination), digest, artifact.size
        )

    async def import_file(self, source: Path, artifact: ArtifactRef) -> None:
        digest = _expected_digest(artifact)
        target = self._paths.artifact_file(artifact.id)
        await run_file_io(_copy_verified, Path(source), target, digest, artifact.size)

    def read(self, artifact: ArtifactRef | str) -> bytes:
        artifact_id = _artifact_id(artifact)
        payload = self._paths.artifact_file(artifact_id).read_bytes()
        self._log.debug(
            "persistence.artifact.read",
            artifact_id=artifact_id,
            bytes=len(payload),
        )
        return payload

    def exists(self, artifact: ArtifactRef | str) -> bool:
        return self._paths.artifact_file(_artifact_id(artifact)).is_file()

    def model_path(self, artifact: ArtifactRef | str) -> str:
        return str(self._paths.artifact_file(_artifact_id(artifact)).resolve())


def _artifact_id(artifact: ArtifactRef | str) -> str:
    return artifact.id if isinstance(artifact, ArtifactRef) else artifact


def _expected_digest(artifact: ArtifactRef) -> str:
    digest = artifact.id.rsplit("/", 1)[-1].split(".", 1)[0]
    if artifact.sha256 and artifact.sha256 != digest:
        raise ValueError("Artifact identity and hash mismatch")
    return digest


def _copy_verified(source: Path, target: Path, expected_digest: str, size: int) -> None:
    from XBotv2.persistence.impl.stream import sync_parents

    if source.is_symlink():
        raise ValueError("Artifact source must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}."
    )
    try:
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                count += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if digest.hexdigest() != expected_digest or count != size:
            raise ValueError("Artifact hash or size mismatch")
        os.replace(temporary, target)
        sync_parents(target.parent, target.parent.parent.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
