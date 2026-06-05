"""Session workspace initialization.

The workspace is the agent's internal, session-scoped working directory. It is
separate from persisted state: state stores events/messages/artifacts, while the
workspace stores files tools may read or write during a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

WorkspaceLifecycle = Literal["start", "resume", "explicit_resume"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkspaceStatus:
    """Result of ensuring a session workspace exists."""

    root: Path
    lifecycle: WorkspaceLifecycle
    status: str
    metadata_path: Path

    def event_type(self) -> str:
        if self.status == "recovered":
            return "workspace_recovered"
        return "workspace_initialized"

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "workspace_root": str(self.root),
            "metadata_path": str(self.metadata_path),
            "lifecycle": self.lifecycle,
            "status": self.status,
        }


class SessionWorkspace:
    """Idempotent manager for a session-scoped workspace directory."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path | str,
        *,
        session_id: str,
        thread_id: str,
        base_root: Path | str | None = None,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.thread_id = thread_id
        self.base_root = Path(base_root) if base_root is not None else self.root.parent

    def ensure(self, lifecycle: WorkspaceLifecycle) -> WorkspaceStatus:
        """Create or validate the workspace layout.

        Existing user files are never deleted. If a resumed session has lost its
        workspace directory or metadata, it is recreated and reported as
        recovered.
        """
        self._assert_root_within_base()

        existed = self.root.exists()
        metadata_path = self.root / ".xbot" / "workspace.yaml"
        metadata_existed = metadata_path.exists()

        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".xbot").mkdir(exist_ok=True)
        (self.root / "files").mkdir(exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)

        status = "ready"
        if lifecycle in {"resume", "explicit_resume"} and (not existed or not metadata_existed):
            status = "recovered"
        elif not metadata_existed:
            status = "created"

        metadata = self._read_metadata(metadata_path)
        created_at = metadata.get("created_at") or _now_iso()
        metadata.update({
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "workspace_root": str(self.root),
            "created_at": created_at,
            "updated_at": _now_iso(),
        })
        metadata_path.write_text(
            yaml.safe_dump(metadata, sort_keys=False),
            encoding="utf-8",
        )

        return WorkspaceStatus(
            root=self.root,
            lifecycle=lifecycle,
            status=status,
            metadata_path=metadata_path,
        )

    def _read_metadata(self, metadata_path: Path) -> dict[str, Any]:
        if not metadata_path.exists():
            return {}
        with open(metadata_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}

    def _assert_root_within_base(self) -> None:
        root = self.root.resolve()
        base = self.base_root.resolve()
        try:
            root.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"Workspace root must stay under session root: {root}"
            ) from exc
