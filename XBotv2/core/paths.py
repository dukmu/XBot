"""Canonical runtime filesystem layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from XBotv2.core.artifacts import ArtifactKind

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")


def _identifier(name: str, value: str) -> str:
    if not value or value in {".", ".."} or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{name} must use only letters, numbers, '.', '_', or '-'"
        )
    return value


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Filesystem roots shared by one XBot server process."""

    data_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path | str) -> "RuntimePaths":
        return cls(Path(data_dir).expanduser().resolve())

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def default_log_file(self) -> Path:
        return self.logs_dir / "xbotv2.log"

    def session(self, session_id: str) -> SessionPaths:
        return SessionPaths(self, _identifier("session_id", session_id))


@dataclass(frozen=True, slots=True)
class SessionPaths:
    """Core-owned paths shared by every thread in one session."""

    runtime: RuntimePaths
    session_id: str

    @property
    def root(self) -> Path:
        return self.runtime.sessions_dir / self.session_id

    @property
    def config_file(self) -> Path:
        return self.root / "config.yaml"

    @property
    def threads_dir(self) -> Path:
        return self.root / "threads"

    @property
    def threads_log(self) -> Path:
        return self.root / "threads.jsonl"

    def thread(self, thread_id: str) -> ThreadPaths:
        return ThreadPaths(self, _identifier("thread_id", thread_id))

    def has_thread(self, thread_id: str) -> bool:
        thread = self.thread(thread_id)
        return thread.root.exists()


@dataclass(frozen=True, slots=True)
class ThreadPaths:
    """Mutable state owned by one thread within a session."""

    session: SessionPaths
    thread_id: str

    @property
    def runtime(self) -> RuntimePaths:
        return self.session.runtime

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def root(self) -> Path:
        return self.session.threads_dir / self.thread_id

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def messages_file(self) -> Path:
        return self.state_dir / "messages.jsonl"

    @property
    def inbox_file(self) -> Path:
        return self.state_dir / "inbox.json"

    @property
    def plugin_state_dir(self) -> Path:
        return self.state_dir / "plugin_state"

    @property
    def plugin_state_file(self) -> Path:
        return self.plugin_state_dir / "state.json"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    def artifact_dir(self, kind: ArtifactKind) -> Path:
        return self.artifacts_dir / kind.value

    def artifact_file(self, artifact_id: str) -> Path:
        """Resolve one validated logical artifact id to its physical file."""
        parts = artifact_id.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid artifact id: {artifact_id!r}")
        category, filename = parts
        try:
            kind = ArtifactKind(category)
        except ValueError as exc:
            raise ValueError(f"Unknown artifact kind: {category!r}") from exc
        if (
            not filename
            or filename in {".", ".."}
            or Path(filename).name != filename
        ):
            raise ValueError(f"Invalid artifact file name: {filename!r}")
        return self.artifact_dir(kind) / filename

    @property
    def metadata_file(self) -> Path:
        return self.root / "thread.json"


__all__ = ["RuntimePaths", "SessionPaths", "ThreadPaths"]
