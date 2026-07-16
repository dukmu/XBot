"""Canonical runtime filesystem layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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
    def system_config(self) -> Path:
        return self.config_dir / "system.yaml"

    @property
    def providers_config(self) -> Path:
        return self.config_dir / "providers.yaml"

    @property
    def permissions_config(self) -> Path:
        return self.config_dir / "permissions.yaml"

    @property
    def sandbox_config(self) -> Path:
        return self.config_dir / "sandbox.yaml"

    @property
    def user_config(self) -> Path:
        return self.config_dir / "user.yaml"

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
    def policy_file(self) -> Path:
        return self.root / "policy.yaml"

    @property
    def threads_dir(self) -> Path:
        return self.root / "threads"

    @property
    def threads_log(self) -> Path:
        return self.root / "threads.jsonl"

    def thread(self, thread_id: str, *, legacy: bool = False) -> ThreadPaths:
        return ThreadPaths(self, _identifier("thread_id", thread_id), legacy)

    def has_thread(self, thread_id: str) -> bool:
        thread = self.thread(thread_id)
        return thread.state_dir.exists() or (
            thread_id == "agent" and (self.root / "state").exists()
        )


@dataclass(frozen=True, slots=True)
class ThreadPaths:
    """Mutable state owned by one thread within a session."""

    session: SessionPaths
    thread_id: str
    legacy: bool = False

    @property
    def runtime(self) -> RuntimePaths:
        return self.session.runtime

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def root(self) -> Path:
        if self.legacy:
            return self.session.root
        return self.session.threads_dir / self.thread_id

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def messages_file(self) -> Path:
        return self.state_dir / "messages.jsonl"

    @property
    def usage_file(self) -> Path:
        return self.state_dir / "usage.yaml"

    @property
    def plugin_states_dir(self) -> Path:
        return self.state_dir / "plugin_states"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    @property
    def mailbox_log(self) -> Path:
        return self.root / "logs" / "mailbox.jsonl"

    @property
    def metadata_file(self) -> Path:
        return self.root / "thread.yaml"


__all__ = ["RuntimePaths", "SessionPaths", "ThreadPaths"]
