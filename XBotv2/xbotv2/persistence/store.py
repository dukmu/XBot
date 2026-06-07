"""Persistent message store.

Manages:
- messages.jsonl: append-only message history
- plugin_states/: opaque per-plugin state files (core never interprets)
- artifacts/: cached large tool outputs
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from xbotv2.llm.messages import Message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def message_to_dict(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "status": msg.status,
    }
    if msg.name:
        d["name"] = msg.name
    if msg.tool_calls:
        d["tool_calls"] = _json_safe(list(msg.tool_calls))
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.additional_kwargs:
        d["additional_kwargs"] = _json_safe({
            k: v for k, v in msg.additional_kwargs.items()
            if not str(k).startswith("xbotv2_")
        })
    if msg.response_metadata:
        d["response_metadata"] = _json_safe(msg.response_metadata)
    if msg.artifact is not None:
        d["artifact"] = _json_safe(msg.artifact)
    return d


def dict_to_message(d: dict[str, Any]) -> Message:
    return Message(
        role=d.get("role", "assistant"),
        content=d.get("content", ""),
        status=d.get("status", ""),
        tool_calls=list(d.get("tool_calls") or []),
        tool_call_id=d.get("tool_call_id", ""),
        name=d.get("name", ""),
        additional_kwargs=dict(d.get("additional_kwargs") or {}),
        response_metadata=dict(d.get("response_metadata") or {}),
        artifact=d.get("artifact"),
    )


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in value]
        return str(value)


class CoreStateStore:

    SCHEMA_VERSION = 2

    def __init__(
        self,
        root: Path,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        provider: str,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.thread_id = thread_id
        self.workspace_root = workspace_root
        self.provider = provider

        self.messages_path = self.root / "messages.jsonl"
        self.plugin_states_dir = self.root / "plugin_states"
        self.artifacts_dir = self.root / "artifacts"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        provider: str,
    ) -> "CoreStateStore":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "plugin_states").mkdir(exist_ok=True)
        (root / "artifacts").mkdir(exist_ok=True)

        store = cls(
            root=root,
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=workspace_root,
            provider=provider,
        )
        if not store.messages_path.exists():
            store.messages_path.touch()
        return store

    def append_message(self, msg: Message) -> dict[str, Any]:
        d = message_to_dict(msg)
        d["msg_id"] = self._next_message_id()
        d["ts"] = now_iso()
        with open(self.messages_path, "a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        return d

    def append_messages(self, messages: list[Message]) -> int:
        for msg in messages:
            self.append_message(msg)
        return len(messages)

    def replace_messages(self, messages: list[Message]) -> int:
        previous = list(_iter_jsonl(self.messages_path))
        previous_by_key: dict[str, list[dict[str, Any]]] = {}
        for entry in previous:
            key = _message_identity_key(entry)
            previous_by_key.setdefault(key, []).append(entry)

        rewritten: list[dict[str, Any]] = []
        next_id = max((entry.get("msg_id", 0) for entry in previous), default=0) + 1
        for msg in messages:
            d = message_to_dict(msg)
            key = _message_identity_key(d)
            matches = previous_by_key.get(key) or []
            if matches:
                old = matches.pop(0)
                d["msg_id"] = old.get("msg_id", next_id)
                d["ts"] = old.get("ts", now_iso())
            else:
                d["msg_id"] = next_id
                d["ts"] = now_iso()
                next_id += 1
            rewritten.append(d)

        self.messages_path.write_text("")
        with open(self.messages_path, "a") as f:
            for d in rewritten:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        return len(rewritten)

    def read_messages(self) -> list[Message]:
        raw = _read_jsonl(self.messages_path)
        return [dict_to_message(d) for d in raw]

    def message_count(self) -> int:
        if not self.messages_path.exists():
            return 0
        return sum(1 for _ in _iter_jsonl(self.messages_path))

    def truncate_messages(self, keep_last: int = 0) -> int:
        if not self.messages_path.exists() or keep_last <= 0:
            if self.messages_path.exists():
                removed = self.message_count()
                self.messages_path.unlink()
                return removed
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
        if self.messages_path.exists():
            self.messages_path.unlink()

    def has_existing_session(self) -> bool:
        return self.messages_path.exists() and self.message_count() > 0

    def read_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "workspace_root": self.workspace_root,
            "provider": self.provider,
            "turn_count": 0,
            "event_count": 0,
            "message_count": self.message_count(),
            "status": "active",
            "mailbox_pending": 0,
            "pending_interactions": [],
            "permission_overrides": {},
            "sandbox_overrides": {},
            "workspace": {},
            "plugin_states": self._read_all_plugin_states(),
            "artifacts_root": str(self.artifacts_dir),
        }

    def get_plugin_state(self, plugin_name: str) -> dict[str, Any]:
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def set_plugin_state(self, plugin_name: str, data: dict[str, Any]) -> None:
        self.plugin_states_dir.mkdir(parents=True, exist_ok=True)
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def delete_plugin_state(self, plugin_name: str) -> None:
        path = self.plugin_states_dir / f"{plugin_name}.yaml"
        if path.exists():
            path.unlink()

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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(_iter_jsonl(path))


def _message_identity_key(d: dict[str, Any]) -> str:
    payload = {k: v for k, v in d.items() if k not in {"msg_id", "ts"}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
