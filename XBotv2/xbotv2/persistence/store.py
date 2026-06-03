"""Persistent append-only state store.

Manages:
- events.jsonl: append-only event log
- state.yaml: materialized view (rebuildable from events)
- plugin_states/: opaque per-plugin state files (core never interprets)

Design principle: the JSONL files are append-only source of truth.
state.yaml is a materialized view that can be rebuilt from logs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoreStateStore:
    """Minimal append-only state store for the core engine.

    Manages:
    - events.jsonl: append-only event log
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
        store._ensure_event_log()
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
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.events_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    # ------------------------------------------------------------------
    # Materialized state
    # ------------------------------------------------------------------

    def materialize(self) -> dict[str, Any]:
        """Build (and persist) the materialized state.yaml from events."""
        events = self.read_events()
        state = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "personality_id": self.personality_id,
            "turn_count": sum(1 for e in events if e.get("type") == "turn_started"),
            "event_count": len(events),
            "status": self._determine_status(events),
            "mailbox_pending": self._count_mailbox_pending(events),
            "plugin_states": self._read_all_plugin_states(),
            "artifacts_root": str(self.artifacts_dir),
            "updated_at": _now_iso(),
        }
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

    def _ensure_event_log(self) -> None:
        if not self.events_path.exists():
            self.events_path.touch()

    def _next_event_id(self) -> int:
        events = self.read_events()
        if not events:
            return 1
        return max(e.get("event_id", 0) for e in events) + 1

    @staticmethod
    def _determine_status(events: list[dict[str, Any]]) -> str:
        for e in reversed(events):
            if e.get("type") == "session_closed":
                return "closed"
            if e.get("type") == "error":
                return "error"
            if e.get("type") == "interrupted":
                return "interrupted"
        return "active"

    @staticmethod
    def _count_mailbox_pending(events: list[dict[str, Any]]) -> int:
        sent = 0
        acked = 0
        for e in events:
            if e.get("type") == "mailbox_send":
                sent += 1
            elif e.get("type") == "mailbox_acknowledge":
                acked += 1
        return max(0, sent - acked)

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
