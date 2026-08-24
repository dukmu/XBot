"""Thread-scoped Todo state and tool projection."""

from __future__ import annotations

from collections.abc import Mapping
import inspect

from xcore import Context
from xcore.state import StateService

from XBotv2.core import Tool, ToolResult
from XBotv2.todolist.models import TodoSnapshot, TodoValidationError


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


class TodolistService:
    """Own the typed Todo snapshot for one thread."""

    def __init__(self, store: StateService) -> None:
        self._store = store

    async def snapshot(self) -> TodoSnapshot:
        stored = await self._store.get("snapshot")
        if stored is None:
            return TodoSnapshot()
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted Todo snapshot must be an object")
        return TodoSnapshot.from_dict(stored)

    async def update_todos(self, todos: list[dict[str, str]]) -> ToolResult:
        """Replace the current Todo checklist with one complete list.

        Todo is persistent plan state for work with several distinct phases,
        not an action log. Each call replaces the list, so include every
        unfinished item. Update it only when a phase or scope changes.

        While work remains, exactly one item must be ``in_progress``. Mark
        status from observed progress, not intent. Submitting a non-empty list
        with every item ``completed`` clears the checklist. Submit an empty list
        only to discard an obsolete checklist. Do not add an item for the final
        reply or summary.

        Args:
            todos: Complete ordered checklist. Each item contains content and a
                status: pending, in_progress, or completed.
        """
        try:
            requested = TodoSnapshot.from_items(todos)
        except TodoValidationError as exc:
            return ToolResult.failure(exc.code, str(exc))

        in_progress = sum(item.status == "in_progress" for item in requested.items)
        unfinished = any(item.status != "completed" for item in requested.items)
        if unfinished and in_progress != 1:
            return ToolResult.failure(
                "invalid_todo_progress",
                "An unfinished Todo list must contain exactly one in_progress item",
            )

        current = await self.snapshot()
        cleared = bool(requested.items) and not unfinished
        active = TodoSnapshot(items=() if cleared else requested.items)
        changed = current != active
        if changed:
            await self._store.set("snapshot", active.to_dict())

        if cleared:
            content = "All todos completed; the active checklist was cleared."
        elif not active.items:
            content = "Todo list cleared." if changed else "Todo list is already empty."
        else:
            action = "updated" if changed else "unchanged"
            content = f"Todo list {action}."
            if not changed:
                content += "\nDo not call update_todos again until the work changes."
        return ToolResult.success(content, data=active.projection())


class TodolistPlugin:
    inject = ["tools", "state"]
    name = "todolist"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        service = TodolistService(ctx.state.namespace(self.name))
        ctx.set("todolist", service)
        ctx.tools.register(
            Tool(
                name="update_todos",
                description=inspect.getdoc(service.update_todos) or "",
                function=service.update_todos,
                parameters=_UPDATE_TODOS_SCHEMA,
            ),
        )


plugin = TodolistPlugin()

__all__ = ["TodolistPlugin", "TodolistService"]
