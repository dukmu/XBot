"""Persistent append-only state store.

Manages:
- events.jsonl: append-only event log
- messages.jsonl: append-only message history
- state.yaml: materialized view (rebuildable from events)
- plugin_states/: opaque per-plugin state files (core never interprets)

Design principle: the JSONL files are append-only source of truth.
state.yaml is a materialized view that can be rebuilt from logs.
Message history persists across restarts for session resume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from xbotv2.persistence.materializer import build_materialized_state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Message serialization
# ------------------------------------------------------------------

def message_to_dict(msg: BaseMessage) -> dict[str, Any]:
    """Serialize a LangChain message to a JSON-safe dict."""
    d: dict[str, Any] = {
        "type": type(msg).__name__,
        "content": _serialize_content(msg.content),
    }
    if isinstance(msg, AIMessage):
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = list(tool_calls)
    if isinstance(msg, ToolMessage):
        d["tool_call_id"] = getattr(msg, "tool_call_id", "")
        status = getattr(msg, "status", None)
        if status:
            d["status"] = status
    return d


def dict_to_message(d: dict[str, Any]) -> BaseMessage:
    """Deserialize a dict back to a LangChain message."""
    msg_type = d.get("type", "AIMessage")
    content = d.get("content", "")

    if msg_type == "HumanMessage":
        return HumanMessage(content=content)
    elif msg_type == "AIMessage":
        tool_calls = d.get("tool_calls")
        if tool_calls:
            return AIMessage(content=content, tool_calls=tool_calls)
        return AIMessage(content=content)
    elif msg_type == "ToolMessage":
        return ToolMessage(
            content=content,
            tool_call_id=d.get("tool_call_id", ""),
            status=d.get("status", "success"),
        )
    elif msg_type == "SystemMessage":
        return SystemMessage(content=content)
    else:
        return AIMessage(content=content)


def _serialize_content(content: Any) -> str:
    """Ensure message content is a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal content blocks → extract text
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


# ------------------------------------------------------------------
# Store
# ------------------------------------------------------------------

class CoreStateStore:
    """Minimal append-only state store for the core engine.

    Manages:
    - events.jsonl: append-only event log
    - messages.jsonl: append-only message history (persisted across restarts)
    - state.yaml: materialized view
    - plugin states: opaque blobs owned by plugins
    """

    SCHEMA_VERSION = 2

    def __init__(
        self,
        root: Path,
        *,
        session_id: str,
        thread_id: str,
        personality_id: str,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.thread_id = thread_id
        self.personality_id = personality_id

        self.events_path = self.root / "events.jsonl"
        self.messages_path = self.root / "messages.jsonl"
        self.state_path = self.root / "state.yaml"
        self.plugin_states_dir = self.root / "plugin_states"
        self.artifacts_dir = self.root / "artifacts"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        session_id: str,
        thread_id: str,
        personality_id: str,
    ) -> "CoreStateStore":
        """Create a new state store with the required directory layout."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "plugin_states").mkdir(exist_ok=True)
        (root / "artifacts").mkdir(exist_ok=True)

        store = cls(
            root=root,
            session_id=session_id,
            thread_id=thread_id,
            personality_id=personality_id,
        )
        store._ensure_logs()
        store.materialize()
        return store

    # ------------------------------------------------------------------
    # Events (append-only JSONL)
    # ------------------------------------------------------------------

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one event to events.jsonl. Returns the enriched event dict."""
        event = {
            "event_id": self._next_event_id(),
            "ts": _now_iso(),
            "type": event_type,
            "payload": payload or {},
        }
        with open(self.events_path, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_events(self) -> list[dict[str, Any]]:
        """Read all events from the log."""
        return _read_jsonl(self.events_path)

    # ------------------------------------------------------------------
    # Messages (append-only JSONL — persisted across restarts)
    # ------------------------------------------------------------------

    def append_message(self, msg: BaseMessage) -> dict[str, Any]:
        """Append one message to messages.jsonl. Returns the serialized dict."""
        d = message_to_dict(msg)
        d["msg_id"] = self._next_message_id()
        d["ts"] = _now_iso()
        with open(self.messages_path, "a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        return d

    def append_messages(self, messages: list[BaseMessage]) -> int:
        """Append multiple messages. Returns count written."""
        for msg in messages:
            self.append_message(msg)
        return len(messages)

    def read_messages(self) -> list[BaseMessage]:
        """Read all messages from the log, deserialized."""
        raw = _read_jsonl(self.messages_path)
        return [dict_to_message(d) for d in raw]

    def message_count(self) -> int:
        """Return the number of persisted messages."""
        if not self.messages_path.exists():
            return 0
        return sum(1 for _ in _iter_jsonl(self.messages_path))

    def truncate_messages(self, keep_last: int = 0) -> int:
        """Remove old messages, keeping the last *keep_last* entries.

        Used by compaction. Returns number removed.
        """
        if not self.messages_path.exists() or keep_last <= 0:
            if self.messages_path.exists():
                self.messages_path.unlink()
                return 0
            return 0

        all_msgs = list(_iter_jsonl(self.messages_path))
        if len(all_msgs) <= keep_last:
            return 0

        removed = len(all_msgs) - keep_last
        kept = all_msgs[-keep_last:]
        self.messages_path.write_text("")
        for d in kept:
            with open(self.messages_path, "a") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        return removed

    def clear_messages(self) -> None:
        """Remove all persisted messages."""
        if self.messages_path.exists():
            self.messages_path.unlink()

    # ------------------------------------------------------------------
    # Session existence (for resume detection)
    # ------------------------------------------------------------------

    def has_existing_session(self) -> bool:
        """Return True if this session has prior state on disk."""
        return (
            self.messages_path.exists()
            or (self.events_path.exists() and self.events_path.stat().st_size > 0)
        )

    # ------------------------------------------------------------------
    # Materialized state
    # ------------------------------------------------------------------

    def materialize(self) -> dict[str, Any]:
        """Build (and persist) the materialized state.yaml from events."""
        events = self.read_events()
        state = build_materialized_state(
            schema_version=self.SCHEMA_VERSION,
            session_id=self.session_id,
            thread_id=self.thread_id,
            personality_id=self.personality_id,
            events=events,
            message_count=self.message_count(),
            plugin_states=self._read_all_plugin_states(),
            artifacts_root=str(self.artifacts_dir),
        )
        with open(self.state_path, "w") as f:
            yaml.safe_dump(state, f, default_flow_style=False, sort_keys=False)
        return state

    def read_state(self) -> dict[str, Any]:
        """Read the materialized state from disk (or rebuild it)."""
        if self.state_path.exists():
            with open(self.state_path) as f:
                return yaml.safe_load(f) or {}
        return self.materialize()

    # ------------------------------------------------------------------
    # Plugin state (opaque to core)
    # ------------------------------------------------------------------

    def get_plugin_state(self, plugin_name: str) -> dict[str, Any]:
        """Read a plugin's state file. Returns {} if not found."""
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def set_plugin_state(self, plugin_name: str, data: dict[str, Any]) -> None:
        """Write a plugin's state file."""
        self.plugin_states_dir.mkdir(parents=True, exist_ok=True)
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def delete_plugin_state(self, plugin_name: str) -> None:
        """Remove a plugin's state file."""
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_logs(self) -> None:
        if not self.events_path.exists():
            self.events_path.touch()

    def _next_event_id(self) -> int:
        events = self.read_events()
        if not events:
            return 1
        return max(e.get("event_id", 0) for e in events) + 1

    def _next_message_id(self) -> int:
        if not self.messages_path.exists():
            return 1
        max_id = 0
        for d in _iter_jsonl(self.messages_path):
            mid = d.get("msg_id", 0)
            if mid > max_id:
                max_id = mid
        return max_id + 1

    def _read_all_plugin_states(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not self.plugin_states_dir.exists():
            return result
        for path in sorted(self.plugin_states_dir.iterdir()):
            if path.suffix == ".yaml":
                name = path.stem
                with open(path) as f:
                    result[name] = yaml.safe_load(f) or {}
        return result


# ------------------------------------------------------------------
# Low-level JSONL helpers
# ------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all lines from a JSONL file."""
    return list(_iter_jsonl(path))


def _iter_jsonl(path: Path):
    """Iterate over parsed dicts from a JSONL file."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
