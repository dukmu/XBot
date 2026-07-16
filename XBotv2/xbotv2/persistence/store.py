"""Persistent message store.

Manages:
- messages.jsonl: append-only message and history-operation journal
- plugin_states/: opaque per-plugin state files (core never interprets)
- artifacts/: cached large tool outputs
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from xbotv2.api.messages import Message
from xbotv2.api.paths import SessionPaths
from xbotv2.api.tools import ToolCall

_PERSISTED_XBOT_KWARGS = {"xbotv2_data", "xbotv2_error"}


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
        d["tool_calls"] = [call.to_dict() for call in msg.tool_calls]
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.additional_kwargs:
        d["additional_kwargs"] = _json_safe({
            k: v for k, v in msg.additional_kwargs.items()
            if not str(k).startswith("xbotv2_") or k in _PERSISTED_XBOT_KWARGS
        })
    if msg.response_metadata:
        d["response_metadata"] = _json_safe(msg.response_metadata)
    if msg.usage_metadata:
        d["usage_metadata"] = _json_safe(msg.usage_metadata)
    if msg.artifact is not None:
        d["artifact"] = _json_safe(msg.artifact)
    return d


def dict_to_message(d: dict[str, Any]) -> Message:
    return Message(
        role=d.get("role", "assistant"),
        content=d.get("content", ""),
        status=d.get("status", ""),
        tool_calls=[ToolCall.from_dict(call) for call in d.get("tool_calls") or []],
        tool_call_id=d.get("tool_call_id", ""),
        name=d.get("name", ""),
        additional_kwargs=dict(d.get("additional_kwargs") or {}),
        response_metadata=dict(d.get("response_metadata") or {}),
        usage_metadata=dict(d.get("usage_metadata") or {}),
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

    SCHEMA_VERSION = 3

    def __init__(
        self,
        paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str,
        provider: str,
    ) -> None:
        self.paths = paths
        self.root = paths.state_dir
        self.session_id = paths.session_id
        self.thread_id = thread_id
        self.workspace_root = workspace_root
        self.provider = provider

        self.messages_path = paths.messages_file
        self.usage_path = paths.usage_file
        self.plugin_states_dir = paths.plugin_states_dir
        self.artifacts_dir = paths.artifacts_dir
        self._max_msg_id = 0

    @classmethod
    def create(
        cls,
        paths: SessionPaths,
        *,
        thread_id: str,
        workspace_root: str,
        provider: str,
    ) -> "CoreStateStore":
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        paths.plugin_states_dir.mkdir(exist_ok=True)
        paths.artifacts_dir.mkdir(exist_ok=True)

        store = cls(
            paths=paths,
            thread_id=thread_id,
            workspace_root=workspace_root,
            provider=provider,
        )
        if not store.messages_path.exists():
            store.messages_path.touch()
        return store

    def append_message(self, msg: Message) -> dict[str, Any]:
        _discard_incomplete_tail(self.messages_path)
        d = message_to_dict(msg)
        d["msg_id"] = self._next_message_id()
        d["ts"] = now_iso()
        with open(self.messages_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return d

    def append_messages(self, messages: list[Message]) -> int:
        if not messages:
            return 0
        _discard_incomplete_tail(self.messages_path)
        with open(self.messages_path, "a", encoding="utf-8") as stream:
            for msg in messages:
                d = message_to_dict(msg)
                d["msg_id"] = self._next_message_id()
                d["ts"] = now_iso()
                stream.write(json.dumps(d, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return len(messages)

    def sync_messages(self, messages: list[Message]) -> int:
        """Persist a normal history extension without rewriting the journal."""
        previous = self.read_messages()
        serialized = [message_to_dict(message) for message in messages]
        previous_payloads = [message_to_dict(message) for message in previous]
        if serialized[:len(previous_payloads)] == previous_payloads:
            self.append_messages(messages[len(previous_payloads):])
            return len(messages)
        self.append_checkpoint(messages, reason="sync")
        return len(messages)

    def append_checkpoint(
        self,
        messages: list[Message],
        *,
        reason: str,
    ) -> None:
        self._append_record({
            "record_type": "history_checkpoint",
            "reason": reason,
            "messages": [message_to_dict(message) for message in messages],
        })

    def append_undo(self, turns: int) -> None:
        if turns < 1:
            raise ValueError("Undo turns must be positive")
        self._append_record({
            "record_type": "history_undo",
            "turns": turns,
        })

    def append_clear(self) -> None:
        self._append_record({"record_type": "history_clear"})

    def append_mailbox_delivery(self, message: Any) -> None:
        self._append_record({
            "record_type": "mailbox_delivery",
            "mailbox_id": str(getattr(message, "id", "")),
            "kind": str(getattr(message, "kind", "")),
            "message": _json_safe(getattr(message, "message", None)),
            "request_id": str(getattr(message, "request_id", "")),
        })

    def read_messages(self) -> list[Message]:
        entries = list(_iter_jsonl(self.messages_path))
        checkpoint = next(
            (
                index
                for index in range(len(entries) - 1, -1, -1)
                if entries[index].get("record_type") == "history_checkpoint"
            ),
            None,
        )
        if checkpoint is None:
            messages: list[Message] = []
            replay = entries
        else:
            messages = [
                dict_to_message(item)
                for item in entries[checkpoint].get("messages") or []
            ]
            replay = entries[checkpoint + 1:]
        for entry in replay:
            record_type = entry.get("record_type")
            if record_type is None:
                messages.append(dict_to_message(entry))
            elif record_type == "history_undo":
                messages = _undo_turns(messages, int(entry.get("turns") or 0))
            elif record_type == "history_clear":
                messages = []
            elif record_type in {"history_checkpoint", "mailbox_delivery"}:
                continue
            else:
                raise ValueError(f"Unknown message journal record: {record_type}")
        return messages

    def message_count(self) -> int:
        return len(self.read_messages())

    def record_count(self) -> int:
        return sum(1 for _ in _iter_jsonl(self.messages_path))

    def truncate_messages(self, keep_last: int = 0) -> int:
        messages = self.read_messages()
        if len(messages) <= keep_last:
            return 0
        removed = len(messages) - max(0, keep_last)
        if keep_last <= 0:
            self.append_clear()
        else:
            self.append_checkpoint(messages[-keep_last:], reason="truncate")
        return removed

    def clear_messages(self) -> None:
        self.append_clear()

    def has_existing_session(self) -> bool:
        return self.messages_path.exists() and self.record_count() > 0

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
            "pending_interactions": [],
            "permission_overrides": {},
            "sandbox_overrides": {},
            "workspace": {},
            "plugin_states": self._read_all_plugin_states(),
            "artifacts_root": str(self.artifacts_dir),
        }

    def read_usage(self) -> dict[str, int] | None:
        if not self.usage_path.exists():
            return None
        data = yaml.safe_load(self.usage_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Session usage state must contain a mapping")
        return {
            key: int(data.get(key) or 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "requests",
                "context_tokens",
            )
        }

    def write_usage(self, usage: dict[str, int]) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_yaml(self.usage_path, usage)

    def get_plugin_state(self, plugin_name: str) -> dict[str, Any]:
        path = self._plugin_state_path(plugin_name)
        if path.exists():
            with open(path, encoding="utf-8") as stream:
                state = yaml.safe_load(stream)
            if state is None:
                return {}
            if not isinstance(state, dict):
                raise ValueError(
                    f"Plugin state for {plugin_name!r} must contain a mapping"
                )
            return state
        return {}

    def set_plugin_state(self, plugin_name: str, data: dict[str, Any]) -> None:
        self.plugin_states_dir.mkdir(parents=True, exist_ok=True)
        path = self._plugin_state_path(plugin_name)
        _atomic_write_yaml(path, data)

    def delete_plugin_state(self, plugin_name: str) -> None:
        path = self._plugin_state_path(plugin_name)
        if path.exists():
            path.unlink()

    def _plugin_state_path(self, plugin_name: str) -> Path:
        if (
            not plugin_name
            or plugin_name in {".", ".."}
            or Path(plugin_name).name != plugin_name
        ):
            raise ValueError(f"Invalid plugin state name: {plugin_name!r}")
        return self.plugin_states_dir / f"{plugin_name}.yaml"

    def _next_message_id(self) -> int:
        if self._max_msg_id == 0 and self.messages_path.exists():
            for d in _iter_jsonl(self.messages_path):
                mid = max(d.get("msg_id", 0), d.get("record_id", 0))
                if mid > self._max_msg_id:
                    self._max_msg_id = mid
        self._max_msg_id += 1
        return self._max_msg_id

    def _append_record(self, record: dict[str, Any]) -> None:
        _discard_incomplete_tail(self.messages_path)
        record = dict(record)
        record["record_id"] = self._next_message_id()
        record["ts"] = now_iso()
        with self.messages_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _read_all_plugin_states(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not self.plugin_states_dir.exists():
            return result
        for path in sorted(self.plugin_states_dir.iterdir()):
            if path.suffix == ".yaml":
                name = path.stem
                result[name] = self.get_plugin_state(name)
        return result

def _undo_turns(messages: list[Message], turns: int) -> list[Message]:
    if turns <= 0:
        return messages
    user_indexes = [
        index for index, message in enumerate(messages) if message.role == "user"
    ]
    if turns >= len(user_indexes):
        return []
    return messages[:user_indexes[-turns]]


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    return
                raise


def _discard_incomplete_tail(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb+") as stream:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) == b"\n":
            return
        position = stream.tell() - 1
        while position > 0:
            size = min(4096, position)
            position -= size
            stream.seek(position)
            chunk = stream.read(size)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                position += newline + 1
                break
        else:
            position = 0
        stream.truncate(position)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.stem}-",
        suffix=".yaml.tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, default_flow_style=False, sort_keys=False, encoding="utf-8", allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
