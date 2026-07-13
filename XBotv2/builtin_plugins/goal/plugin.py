"""Session-scoped active goal plugin."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    ContextComponent,
    HookContext,
    HookStage,
    PluginBase,
    PluginSetupContext,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)


_MAX_OBJECTIVE_CHARS = 2_000
_STATUSES = {"active", "completed", "abandoned"}


class GoalPlugin(PluginBase):
    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(
            HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
            self._add_active_goal_context,
        )
        options = ToolRegistrationOptions(
            sandbox_mode="host",
            namespace="plugin:goal",
        )
        ctx.register_tool(Tool.from_function(self.create_goal), options=options)
        ctx.register_tool(Tool.from_function(self.inspect_goal), options=options)
        ctx.register_tool(Tool.from_function(self.update_goal), options=options)
        ctx.register_tool(Tool.from_function(self.complete_goal), options=options)
        ctx.register_tool(Tool.from_function(self.abandon_goal), options=options)

    async def create_goal(self, objective: str) -> ToolResult:
        """Create the active session goal when no active goal exists."""
        error = _objective_error(objective)
        if error is not None:
            return error
        current = await self._read_goal()
        if current is not None and current["status"] == "active":
            return ToolResult.failure(
                "goal_exists",
                "Complete or abandon the active goal before creating another",
            )
        goal = {"objective": objective.strip(), "status": "active"}
        await self.store.set("goal", goal)
        return ToolResult.success("Created the active goal.", data={"goal": goal})

    async def inspect_goal(self) -> ToolResult:
        """Inspect the current or most recently finished session goal."""
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.success("No goal has been created.", data={"goal": None})
        return ToolResult.success(
            f"[{goal['status']}] {goal['objective']}",
            data={"goal": goal},
        )

    async def update_goal(self, objective: str) -> ToolResult:
        """Replace the objective of the active session goal."""
        error = _objective_error(objective)
        if error is not None:
            return error
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        goal["objective"] = objective.strip()
        await self.store.set("goal", goal)
        return ToolResult.success("Updated the active goal.", data={"goal": goal})

    async def complete_goal(self) -> ToolResult:
        """Mark the active session goal completed."""
        return await self._finish_goal("completed")

    async def abandon_goal(self) -> ToolResult:
        """Mark the active session goal abandoned."""
        return await self._finish_goal("abandoned")

    async def _finish_goal(self, status: str) -> ToolResult:
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        goal["status"] = status
        await self.store.set("goal", goal)
        return ToolResult.success(
            f"Goal marked {status}.",
            data={"goal": goal},
        )

    async def _active_goal(self) -> dict[str, str] | None:
        goal = await self._read_goal()
        if goal is None or goal["status"] != "active":
            return None
        return goal

    async def _read_goal(self) -> dict[str, str] | None:
        goal = await self.store.get("goal")
        if goal is None:
            return None
        if (
            not isinstance(goal, dict)
            or not isinstance(goal.get("objective"), str)
            or not goal["objective"].strip()
            or goal.get("status") not in _STATUSES
        ):
            raise ValueError("Goal state is invalid")
        return {
            "objective": goal["objective"],
            "status": goal["status"],
        }

    async def _add_active_goal_context(self, ctx: HookContext) -> None:
        goal = await self._read_goal()
        if (
            goal is None
            or goal["status"] != "active"
            or ctx.context_components is None
        ):
            return
        ctx.context_components = [
            *ctx.context_components,
            ContextComponent(
                role="system",
                source="plugin_fragment",
                content=f"## Active Goal\n\n{goal['objective']}",
                plugin_name="goal",
                stage="context_suffix",
            ),
        ]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "scope": "session",
            "goal_statuses": sorted(_STATUSES),
            "automatic_continuation": False,
        }


def _objective_error(objective: str) -> ToolResult | None:
    objective = objective.strip()
    if not objective:
        return ToolResult.failure(
            "invalid_objective",
            "Goal objective must not be empty",
        )
    if len(objective) > _MAX_OBJECTIVE_CHARS:
        return ToolResult.failure(
            "objective_too_long",
            f"Goal objective must not exceed {_MAX_OBJECTIVE_CHARS} characters",
        )
    return None


def _no_active_goal() -> ToolResult:
    return ToolResult.failure("no_active_goal", "No active goal exists")
