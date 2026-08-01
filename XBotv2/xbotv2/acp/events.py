"""Translate XBot runtime events into ACP session updates."""

from __future__ import annotations

from typing import Any

from acp import (
    plan_entry,
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_plan,
    update_tool_call,
    update_user_message_text,
)
from acp.schema import UsageUpdate


class ACPEventMapper:
    """Stateful event mapper for one ACP prompt turn."""

    def __init__(self, *, context_size: int = 0) -> None:
        self.stop_reason = "end_turn"
        self.error: dict[str, Any] | None = None
        self.usage: dict[str, int] | None = None
        self._streamed_message = False
        self._context_size = context_size
        self._tasks: set[str] = set()

    def updates(self, event: dict[str, Any]) -> list[Any]:
        event_type = str(event.get("type") or "")
        data = event.get("data") or {}

        if event_type == "turn_started":
            self._streamed_message = False
            return []
        if event_type == "assistant_message_delta":
            updates = []
            content = data.get("content")
            reasoning = data.get("reasoning")
            if content:
                self._streamed_message = True
                updates.append(update_agent_message_text(str(content)))
            if reasoning:
                updates.append(update_agent_thought_text(str(reasoning)))
            return updates
        if event_type == "assistant_message":
            content = data.get("content")
            if content and not self._streamed_message:
                return [update_agent_message_text(str(content))]
            return []
        if event_type == "client_message":
            message = data.get("message")
            return [update_agent_message_text(str(message))] if message else []
        if event_type == "tool_calls_started":
            updates = [
                start_tool_call(
                    str(call["id"]),
                    str(call["name"]),
                    kind=_tool_kind(str(call["name"])),
                    status="pending",
                    raw_input=call.get("args"),
                )
                for call in data.get("tool_calls") or []
            ]
            self._streamed_message = False
            return updates
        if event_type == "tool_result":
            updates = [
                update_tool_call(
                    str(data.get("tool_call_id") or ""),
                    status=(
                        "completed"
                        if data.get("status") == "success"
                        else "failed"
                    ),
                    content=[
                        tool_content(text_block(_display_content(data.get("content"))))
                    ],
                    raw_output=(
                        data.get("data")
                        if data.get("data") is not None
                        else data.get("content")
                    ),
                )
            ]
            todos = (data.get("data") or {}).get("todos") if isinstance(
                data.get("data"), dict
            ) else None
            if data.get("name") == "update_todos" and isinstance(todos, list):
                updates.append(update_plan([
                    plan_entry(
                        str(item.get("content") or ""),
                        status=_plan_status(str(item.get("status") or "")),
                    )
                    for item in todos
                    if isinstance(item, dict) and item.get("content")
                ]))
            return updates
        if event_type == "task_updated":
            task_id = str(data.get("task_id") or "")
            status = str(data.get("status") or "")
            title = str(
                data.get("command")
                or data.get("agent")
                or "Background task"
            )
            output = data.get("output") or data.get("error")
            content = (
                [tool_content(text_block(str(output)))]
                if output else None
            )
            if task_id not in self._tasks:
                self._tasks.add(task_id)
                return [start_tool_call(
                    task_id,
                    title,
                    kind="execute" if data.get("kind") == "shell" else "other",
                    status=_task_status(status),
                    content=content,
                    raw_output=(
                        data if status in {"completed", "failed", "stopped"}
                        else None
                    ),
                )]
            return [update_tool_call(
                task_id,
                status=_task_status(status),
                content=content,
                raw_output=data,
            )]
        if event_type == "usage":
            current = {
                key: int(data.get(key) or 0)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "context_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "prompt_cache_write_tokens",
                )
            }
            if self.usage is None:
                self.usage = current
            else:
                for key in current:
                    if key == "context_tokens":
                        self.usage[key] = current[key]
                    else:
                        self.usage[key] += current[key]
            size = int(data.get("max_context_tokens") or self._context_size)
            return [
                UsageUpdate(
                    session_update="usage_update",
                    used=self.usage["context_tokens"],
                    size=size,
                )
            ] if size > 0 else []
        if event_type == "turn_cancelled":
            self.stop_reason = "cancelled"
            return []
        if event_type == "error":
            self.error = dict(data)
            return []
        return []


def replay_history(messages: list[Any]) -> list[Any]:
    """Translate persisted conversation messages into ACP load updates."""
    updates: list[Any] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        if message.role == "user" and message.content:
            updates.append(update_user_message_text(str(message.content)))
            continue
        if message.role == "assistant":
            reasoning = message.reasoning
            if reasoning:
                updates.append(update_agent_thought_text(str(reasoning)))
            if message.content:
                updates.append(update_agent_message_text(str(message.content)))
            for call in message.tool_calls:
                tool_names[call.id] = call.name
                updates.append(start_tool_call(
                    call.id,
                    call.name,
                    kind=_tool_kind(call.name),
                    status="pending",
                    raw_input=call.args,
                ))
            continue
        if message.role != "tool" or not message.tool_call_id:
            continue
        data = message.additional_kwargs.get("xbotv2_data")
        updates.append(update_tool_call(
            message.tool_call_id,
            status="completed" if message.status == "success" else "failed",
            content=[tool_content(text_block(str(message.content)))],
            raw_output=data if data is not None else message.content,
        ))
        todos = data.get("todos") if isinstance(data, dict) else None
        if (
            tool_names.get(message.tool_call_id) == "update_todos"
            and isinstance(todos, list)
        ):
            updates.append(update_plan([
                plan_entry(
                    str(item.get("content") or ""),
                    status=_plan_status(str(item.get("status") or "")),
                )
                for item in todos
                if isinstance(item, dict) and item.get("content")
            ]))
    return updates


def _display_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _plan_status(status: str) -> str:
    return status if status in {"pending", "in_progress", "completed"} else "pending"


def _task_status(status: str) -> str:
    if status == "pending":
        return "pending"
    if status == "completed":
        return "completed"
    if status in {"failed", "stopped"}:
        return "failed"
    return "in_progress"


def _tool_kind(name: str) -> str:
    if name.startswith("filesystem_read") or name in {"read_file", "list_files"}:
        return "read"
    if name.startswith("filesystem_") or name in {"write_file", "edit_file"}:
        return "edit"
    if name in {"shell", "shell_start", "run_command"}:
        return "execute"
    if "search" in name:
        return "search"
    if "fetch" in name:
        return "fetch"
    if name in {"update_todos", "create_goal", "update_goal"}:
        return "think"
    return "other"


__all__ = ["ACPEventMapper", "replay_history"]
