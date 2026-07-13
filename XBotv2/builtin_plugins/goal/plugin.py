"""Persistent session goal state machine."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    ContextComponent,
    HookContext,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)


_ACTIONS = {
    "get", "create", "update", "complete", "block", "pause", "resume", "clear",
}
_MAX_TEXT_CHARS = 2_000
_STATUSES = {"active", "complete", "blocked", "paused"}


class GoalPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._continuation_pending = False

    async def on_unload(self) -> None:
        self._continuation_pending = False

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(
            HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
            self._add_goal_context,
        )
        ctx.register_hook(HookStage.ON_TURN_END, self._on_turn_end)
        ctx.register_hook(
            HookStage.BEFORE_MAILBOX_DELIVERY,
            self._on_mailbox_delivery,
        )
        ctx.register_tool(
            Tool(
                name="goal",
                description=(
                    "Manage the persistent session goal. Use create to start one, "
                    "get to inspect it, update to record objective or progress, "
                    "complete with an execution summary only when all work is "
                    "finished, block with a blocking summary, resume to reactivate "
                    "it, pause it, or clear to remove it."
                ),
                function=self.goal,
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": sorted(_ACTIONS),
                            "description": "Goal state transition to perform.",
                        },
                        "objective": {
                            "type": "string",
                            "description": "Required by create; optional for update only.",
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "Progress for create/update; required execution or "
                                "blocking summary for complete/block."
                            ),
                        },
                        "token_budget": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Create only, and only when the user explicitly "
                                "requests a token budget."
                            ),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:goal",
            ),
        )

    async def goal(
        self,
        action: str,
        objective: str | None = None,
        summary: str | None = None,
        token_budget: int | None = None,
    ) -> ToolResult:
        """Read or transition the persistent session goal."""
        action = action.strip().lower()
        if action not in _ACTIONS:
            return ToolResult.failure(
                "invalid_action",
                f"Goal action must be one of: {', '.join(sorted(_ACTIONS))}",
            )
        if action == "get":
            if any(value is not None for value in (objective, summary, token_budget)):
                return _unexpected_arguments(action)
            return await self._get()
        if action == "create":
            return await self._create(objective, summary, token_budget)
        if action == "update":
            if token_budget is not None:
                return _unexpected_arguments(action)
            return await self._update(objective, summary)
        if action in {"complete", "block"}:
            if objective is not None or token_budget is not None:
                return _unexpected_arguments(action)
            return await self._finish(action, summary)
        if action == "resume":
            if any(value is not None for value in (objective, summary, token_budget)):
                return _unexpected_arguments(action)
            return await self._resume()
        if action == "pause":
            if any(value is not None for value in (objective, summary, token_budget)):
                return _unexpected_arguments(action)
            return await self._pause()
        if any(value is not None for value in (objective, summary, token_budget)):
            return _unexpected_arguments(action)
        return await self._clear()

    async def _get(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.success("No goal has been created.", data={"goal": None})
        return ToolResult.success(_format_goal(goal), data={"goal": goal})

    async def _create(
        self,
        objective: str | None,
        summary: str | None,
        token_budget: int | None,
    ) -> ToolResult:
        error = _text_error("objective", objective)
        if error is not None:
            return error
        if summary is not None:
            error = _text_error("summary", summary)
            if error is not None:
                return error
        if token_budget is not None and token_budget < 1:
            return ToolResult.failure(
                "invalid_token_budget",
                "Goal token budget must be a positive integer",
            )
        current = await self._read_goal()
        if current is not None and current["status"] == "active":
            return ToolResult.failure(
                "goal_exists",
                "Complete, block, or clear the active goal before creating another",
            )
        goal = {
            "objective": objective.strip(),
            "status": "active",
            "summary": summary.strip() if summary is not None else "",
            "token_budget": token_budget,
        }
        await self.store.set("goal", goal)
        return ToolResult.success("Created the active goal.", data={"goal": goal})

    async def _update(
        self,
        objective: str | None,
        summary: str | None,
    ) -> ToolResult:
        if objective is None and summary is None:
            return ToolResult.failure(
                "invalid_update",
                "Goal update requires an objective or progress summary",
            )
        for field, value in (("objective", objective), ("summary", summary)):
            if value is not None:
                error = _text_error(field, value)
                if error is not None:
                    return error
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        if objective is not None:
            goal["objective"] = objective.strip()
        if summary is not None:
            goal["summary"] = summary.strip()
        await self.store.set("goal", goal)
        return ToolResult.success("Updated the active goal.", data={"goal": goal})

    async def _finish(self, action: str, summary: str | None) -> ToolResult:
        error = _text_error("summary", summary)
        if error is not None:
            return error
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        goal["status"] = "blocked" if action == "block" else "complete"
        goal["summary"] = summary.strip()
        await self.store.set("goal", goal)
        message = "Goal completed." if action == "complete" else "Goal blocked."
        return ToolResult.success(
            f"{message}\nExecution summary: {goal['summary']}",
            data={"goal": goal},
        )

    async def _resume(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.failure("no_goal", "No goal exists to resume")
        if goal["status"] == "active":
            return ToolResult.failure("goal_active", "The goal is already active")
        goal["status"] = "active"
        await self.store.set("goal", goal)
        return ToolResult.success("Resumed the goal.", data={"goal": goal})

    async def _pause(self) -> ToolResult:
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        goal["status"] = "paused"
        await self.store.set("goal", goal)
        return ToolResult.success("Paused the goal.", data={"goal": goal})

    async def _clear(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.success("No goal has been created.", data={"goal": None})
        await self.store.delete("goal")
        return ToolResult.success("Cleared the goal.", data={"goal": None})

    async def _active_goal(self) -> dict[str, Any] | None:
        goal = await self._read_goal()
        if goal is None or goal["status"] != "active":
            return None
        return goal

    async def _read_goal(self) -> dict[str, Any] | None:
        goal = await self.store.get("goal")
        if goal is None:
            return None
        if not _valid_goal(goal):
            raise ValueError("Goal state is invalid")
        return {
            "objective": goal["objective"],
            "status": goal["status"],
            "summary": goal["summary"],
            "token_budget": goal["token_budget"],
        }

    async def _add_goal_context(self, ctx: HookContext) -> None:
        goal = await self._read_goal()
        if goal is None or ctx.context_components is None:
            return
        ctx.context_components = [
            *ctx.context_components,
            ContextComponent(
                role="system",
                source="plugin_fragment",
                content=_goal_context(goal),
                plugin_name="goal",
                stage="context_suffix",
            ),
        ]

    async def _on_turn_end(self, ctx: HookContext) -> None:
        if ctx.stop_reason == "client_interrupt":
            goal = await self._active_goal()
            if goal is None:
                return
            goal["status"] = "paused"
            await self.store.set("goal", goal)
            return
        await self.start(ctx.enqueue_mailbox)

    async def start(self, enqueue_mailbox) -> None:
        """Schedule the next active-goal turn if one is not already pending."""
        goal = await self._active_goal()
        if goal is None or self._continuation_pending or enqueue_mailbox is None:
            return
        self._continuation_pending = True
        await enqueue_mailbox({
            "source": "goal",
            "event": "continue",
            "content": "Continue progressing the active session goal.",
            "data": {"objective": goal["objective"]},
        })

    async def _on_mailbox_delivery(self, ctx: HookContext) -> None:
        item = ctx.mailbox_message
        message = getattr(item, "message", None)
        if (
            getattr(item, "kind", None) == "general"
            and isinstance(message, dict)
            and message.get("source") == "goal"
            and message.get("event") == "continue"
        ):
            self._continuation_pending = False

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "scope": "session",
            "goal_statuses": sorted(_STATUSES),
            "automatic_continuation": True,
        }


def _text_error(field: str, value: str | None) -> ToolResult | None:
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return ToolResult.failure(
            f"invalid_{field}",
            f"Goal {field} must not be empty",
        )
    if len(value) > _MAX_TEXT_CHARS:
        return ToolResult.failure(
            f"{field}_too_long",
            f"Goal {field} must not exceed {_MAX_TEXT_CHARS} characters",
        )
    return None


def _valid_goal(goal: Any) -> bool:
    budget = goal.get("token_budget") if isinstance(goal, dict) else None
    return (
        isinstance(goal, dict)
        and isinstance(goal.get("objective"), str)
        and bool(goal["objective"].strip())
        and goal.get("status") in _STATUSES
        and isinstance(goal.get("summary"), str)
        and (
            budget is None
            or isinstance(budget, int) and not isinstance(budget, bool) and budget > 0
        )
    )


def _format_goal(goal: dict[str, Any]) -> str:
    lines = [f"[{goal['status']}] {goal['objective']}"]
    if goal["token_budget"] is not None:
        lines.append(f"Token budget: {goal['token_budget']}")
    if goal["summary"]:
        lines.append(f"Execution summary: {goal['summary']}")
    return "\n".join(lines)


def _goal_context(goal: dict[str, Any]) -> str:
    lines = [
        "## Session Goal",
        f"Status: {goal['status']}",
        f"Objective: {goal['objective']}",
    ]
    if goal["token_budget"] is not None:
        lines.append(f"Token budget: {goal['token_budget']}")
    if goal["summary"]:
        lines.append(f"Execution summary: {goal['summary']}")
    if goal["status"] == "active":
        lines.append(
            "Persist with this objective. Call goal action=complete with a concise "
            "execution summary only after all required work is finished; use "
            "action=block only when progress cannot continue. After the transition, "
            "give the human a concise final summary."
        )
    elif goal["status"] == "complete":
        lines.append(
            "This goal is complete. Do not restart or continue its work unless it is "
            "explicitly resumed. Give the human a concise final summary of the result."
        )
    elif goal["status"] == "blocked":
        lines.append(
            "This goal is blocked. Do not continue it until its blocker changes and "
            "it is explicitly resumed."
        )
    else:
        lines.append(
            "This goal is paused. Do not continue it until it is explicitly resumed."
        )
    return "\n\n".join(lines)


def _no_active_goal() -> ToolResult:
    return ToolResult.failure("no_active_goal", "No active goal exists")


def _unexpected_arguments(action: str) -> ToolResult:
    return ToolResult.failure(
        "invalid_arguments",
        f"Goal action {action!r} received unsupported arguments",
    )
