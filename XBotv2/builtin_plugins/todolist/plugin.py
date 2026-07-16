"""Session-scoped todo list plugin."""

from __future__ import annotations

import inspect
import json
from typing import Any

from xbotv2.api import (
    PluginBase,
    PluginSetupContext,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)


_STATUSES = {"pending", "in_progress", "completed"}
_UPDATE_TODOS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["todos"],
}


class TodolistPlugin(PluginBase):
    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_tool(
            Tool(
                name="update_todos",
                description=inspect.getdoc(self.update_todos) or "",
                function=self.update_todos,
                parameters=_UPDATE_TODOS_SCHEMA,
            ),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:todolist",
            ),
        )

    async def update_todos(self, todos: list[dict[str, str]]) -> ToolResult:
        """Replace the current Todo checklist with one complete list.

        Use this for non-trivial work with multiple meaningful steps, or when
        the user explicitly requests a checklist. Do not use it for a simple
        task, conversational answer, or as a substitute for doing the work.
        Call only when the checklist's contents or status actually changes.
        After updating it, perform the current work before updating it again;
        never repeatedly submit the same list. Each call replaces the whole
        list. Keep exactly one item in_progress while unfinished work remains,
        and mark work completed only after verification. An empty list clears
        the checklist.

        Args:
            todos: Complete ordered checklist. Each item contains content and a
                status: pending, in_progress, or completed.
        """
        normalized = _normalize_todos(todos)
        if isinstance(normalized, ToolResult):
            return normalized

        in_progress = sum(
            item["status"] == "in_progress" for item in normalized
        )
        unfinished = any(item["status"] != "completed" for item in normalized)
        if unfinished and in_progress != 1:
            return ToolResult.failure(
                "invalid_todo_progress",
                "An unfinished Todo list must contain exactly one in_progress item",
            )

        current = await self._read_items()
        cleared = bool(normalized) and not unfinished
        active = [] if cleared else normalized
        changed = current != active
        if changed:
            await self.store.set("state", {"items": active})

        data = {"todos": normalized, "cleared": cleared}
        if cleared:
            content = "All todos completed; the active checklist was cleared."
        elif not active:
            content = "Todo list cleared." if changed else "Todo list is already empty."
        else:
            action = "updated" if changed else "unchanged"
            content = f"Todo list {action}:\n{json.dumps(active, ensure_ascii=False)}"
            if not changed:
                content += "\nDo not call update_todos again until the work changes."
        return ToolResult.success(content, data=data)

    async def _read_items(self) -> list[dict[str, str]]:
        state = await self.store.get("state")
        if state is None:
            return []
        if not isinstance(state, dict) or not isinstance(state.get("items"), list):
            raise ValueError("Todo list state is invalid")
        items: list[dict[str, str]] = []
        for item in state["items"]:
            if not _valid_item(item):
                raise ValueError("Todo list contains an invalid item")
            items.append({
                "content": item["content"],
                "status": item["status"],
            })
        return items

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "scope": "session",
            "tool": "update_todos",
            "item_statuses": sorted(_STATUSES),
        }


def _normalize_todos(value: Any) -> list[dict[str, str]] | ToolResult:
    if not isinstance(value, list):
        return ToolResult.failure("invalid_todos", "Todos must be a list")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return ToolResult.failure(
                "invalid_todos",
                f"Todo at index {index} must be an object",
            )
        if set(item) != {"content", "status"}:
            return ToolResult.failure(
                "invalid_todos",
                f"Todo at index {index} must contain only content and status",
            )
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            return ToolResult.failure(
                "invalid_todo",
                f"Todo at index {index} must have non-empty content",
            )
        if status not in _STATUSES:
            return ToolResult.failure(
                "invalid_todo_status",
                f"Todo at index {index} has an invalid status",
            )
        normalized.append({"content": content.strip(), "status": status})
    return normalized


def _valid_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("content"), str)
        and bool(item["content"].strip())
        and item.get("status") in _STATUSES
    )
