"""Thread-local artifact and plugin-state storage."""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any

import yaml

from XBotv2.core.messages import ImageContent
from XBotv2.core.paths import ThreadPaths
from XBotv2.filesystem.atomic import write_text_atomic


class ThreadStorage:
    """Own non-journal files for one runtime thread."""

    def __init__(self, paths: ThreadPaths, *, workspace_root: str) -> None:
        self.paths = paths
        self.root = paths.state_dir
        self.session_id = paths.session_id
        self.thread_id = paths.thread_id
        self.workspace_root = workspace_root
        self.plugin_states_dir = paths.plugin_states_dir
        self.artifacts_dir = paths.artifacts_dir

    @classmethod
    def create(cls, paths: ThreadPaths, *, workspace_root: str) -> "ThreadStorage":
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        paths.plugin_states_dir.mkdir(exist_ok=True)
        paths.artifacts_dir.mkdir(exist_ok=True)
        return cls(paths, workspace_root=workspace_root)

    def store_image(self, data: str, media_type: str) -> ImageContent:
        if not media_type.startswith("image/"):
            raise ValueError("media_type must be an image MIME type")
        payload = self._decode(data, "image")
        media_dir = self.artifacts_dir / "media"
        media_dir.mkdir(exist_ok=True)
        path = media_dir / hashlib.sha256(payload).hexdigest()
        if not path.exists():
            path.write_bytes(payload)
        return ImageContent(
            path=str(path.relative_to(self.root)),
            media_type=media_type,
            size=len(payload),
        )

    def store_attachment(self, data: str, media_type: str, name: str) -> dict[str, Any]:
        payload = self._decode(data, "attachment")
        safe_name = Path(name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("attachment name must be a file name")
        path = self.artifacts_dir / "attachments" / hashlib.sha256(payload).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        return {
            "id": path.relative_to(self.root).as_posix(),
            "name": safe_name,
            "media_type": media_type or "application/octet-stream",
            "size": len(payload),
        }

    def get_plugin_state(self, plugin_name: str) -> dict[str, Any]:
        path = self._plugin_state_path(plugin_name)
        if not path.exists():
            return {}
        state = yaml.safe_load(path.read_text(encoding="utf-8"))
        if state is None:
            return {}
        if not isinstance(state, dict):
            raise ValueError(
                f"Plugin state for {plugin_name!r} must contain a mapping"
            )
        return state

    def set_plugin_state(self, plugin_name: str, data: dict[str, Any]) -> None:
        write_text_atomic(
            self._plugin_state_path(plugin_name),
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        )

    def delete_plugin_state(self, plugin_name: str) -> None:
        path = self._plugin_state_path(plugin_name)
        if path.exists():
            path.unlink()

    def _plugin_state_path(self, plugin_name: str) -> Path:
        if not plugin_name or plugin_name in {".", ".."} or Path(plugin_name).name != plugin_name:
            raise ValueError(f"Invalid plugin state name: {plugin_name!r}")
        return self.plugin_states_dir / f"{plugin_name}.yaml"

    @staticmethod
    def _decode(data: str, kind: str) -> bytes:
        try:
            payload = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"{kind} data must be valid base64") from exc
        if not payload:
            raise ValueError(f"{kind} data must not be empty")
        return payload


__all__ = ["ThreadStorage"]
