"""Runtime identity and filesystem API."""

from __future__ import annotations

from dataclasses import dataclass

from xbotv2.api.paths import RuntimePaths, SessionPaths


@dataclass
class SessionInfo:
    session_id: str
    thread_id: str
    workspace_root: str = ""
    provider: str = "default"
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"
    mailbox_pending: int = 0


__all__ = ["RuntimePaths", "SessionInfo", "SessionPaths"]
