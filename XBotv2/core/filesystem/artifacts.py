"""Filesystem implementation of the thread ArtifactStore contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

from XBotv2.core.artifacts import ArtifactKind, ArtifactRef
from XBotv2.core.filesystem.atomic import write_bytes_atomic
from XBotv2.core.paths import ThreadPaths
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog


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
        if not payload:
            raise ValueError("artifact payload must not be empty")
        if suffix and (
            not suffix.startswith(".")
            or "/" in suffix
            or "\\" in suffix
        ):
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
        return f"session/artifacts/{_artifact_id(artifact)}"


def _artifact_id(artifact: ArtifactRef | str) -> str:
    return artifact.id if isinstance(artifact, ArtifactRef) else artifact


__all__ = ["ArtifactStore"]
