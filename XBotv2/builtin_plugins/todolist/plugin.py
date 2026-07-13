"""Session-scoped todo list plugin."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    PluginBase,
    PluginSetupContext,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)


_STATUSES = {"pending", "in_progress", "completed"}


class TodolistPlugin(PluginBase):
    def setup(self, ctx: PluginSetupContext) -> None:
        options = ToolRegistrationOptions(
            sandbox_mode="host",
            namespace="plugin:todolist",
        )
        ctx.register_tool(Tool.from_function(self.list_todos), options=options)
        ctx.register_tool(Tool.from_function(self.create_todo), options=options)
        ctx.register_tool(Tool.from_function(self.update_todo), options=options)
        ctx.register_tool(Tool.from_function(self.remove_todo), options=options)

    async def list_todos(self) -> ToolResult:
        """List todo items in creation order."""
        state = await self._read_state()
        items = state["items"]
        if not items:
            return ToolResult.success("Todo list is empty.", data={"items": []})
        lines = [
            f"{item['id']} [{item['status']}] {item['content']}"
            for item in items
        ]
        return ToolResult.success("\n".join(lines), data={"items": items})

    async def create_todo(self, content: str) -> ToolResult:
        """Create a todo item with pending status."""
        content = content.strip()
        if not content:
            return ToolResult.failure("invalid_todo", "Todo content must not be empty")
        state = await self._read_state()
        item = {
            "id": f"todo-{state['next_id']}",
            "content": content,
            "status": "pending",
        }
        state["next_id"] += 1
        state["items"].append(item)
        await self.store.set("state", state)
        return ToolResult.success(
            f"Created {item['id']}.",
            data={"item": item},
        )

    async def update_todo(
        self,
        todo_id: str,
        content: str | None = None,
        status: str | None = None,
    ) -> ToolResult:
        """Update content or status: pending, in_progress, or completed."""
        if content is None and status is None:
            return ToolResult.failure(
                "invalid_update",
                "Provide content or status to update",
            )
        if content is not None:
            content = content.strip()
            if not content:
                return ToolResult.failure(
                    "invalid_todo",
                    "Todo content must not be empty",
                )
        if status is not None and status not in _STATUSES:
            return ToolResult.failure(
                "invalid_status",
                "Todo status must be pending, in_progress, or completed",
            )

        state = await self._read_state()
        item = next(
            (item for item in state["items"] if item["id"] == todo_id),
            None,
        )
        if item is None:
            return ToolResult.failure("todo_not_found", f"Todo {todo_id!r} not found")
        if content is not None:
            item["content"] = content
        if status is not None:
            item["status"] = status
        await self.store.set("state", state)
        return ToolResult.success(
            f"Updated {todo_id}.",
            data={"item": item},
        )

    async def remove_todo(self, todo_id: str) -> ToolResult:
        """Remove a todo item by its stable identifier."""
        state = await self._read_state()
        index = next(
            (
                index
                for index, item in enumerate(state["items"])
                if item["id"] == todo_id
            ),
            None,
        )
        if index is None:
            return ToolResult.failure("todo_not_found", f"Todo {todo_id!r} not found")
        item = state["items"].pop(index)
        await self.store.set("state", state)
        return ToolResult.success(
            f"Removed {todo_id}.",
            data={"item": item},
        )

    async def _read_state(self) -> dict[str, Any]:
        state = await self.store.get("state")
        if state is None:
            return {"next_id": 1, "items": []}
        if not isinstance(state, dict):
            raise ValueError("Todo list state must be an object")
        next_id = state.get("next_id")
        items = state.get("items")
        if not isinstance(next_id, int) or next_id < 1 or not isinstance(items, list):
            raise ValueError("Todo list state is invalid")
        for item in items:
            if not _valid_item(item):
                raise ValueError("Todo list contains an invalid item")
        return {"next_id": next_id, "items": [dict(item) for item in items]}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "scope": "session",
            "item_statuses": sorted(_STATUSES),
        }


def _valid_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("content"), str)
        and item.get("status") in _STATUSES
    )
