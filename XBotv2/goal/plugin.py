"""Persistent session goal state machine."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from XBotv2.core import (
    Tool,
    ToolResult,
)
from XBotv2.agentloop import AgentLoopDriverPort, EventContext, Events
from XBotv2.commands import Command, CommandResult
from XBotv2.application import COLLECT_STATUS_SLOTS, StatusSlots


_MAX_TEXT_CHARS = 2_000
_STATUSES = {"active", "complete", "blocked", "paused"}


class GoalPlugin:
    inject = {
        "required": ["tools", "commands"],
        "optional": ["engine"],
    }
    name = "goal"

    def __init__(self) -> None:
        self._continuation_pending = False

    async def on_unload(self) -> None:
        self._continuation_pending = False

    async def _contribute_status(self, slots: StatusSlots) -> None:
        goal = await self._read_goal()
        if goal is not None:
            slots.add("goal", goal["status"])

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        self.store = ctx.state.namespace("goal")
        ctx.on(Events.TURN_START, self._start_goal_turn)
        ctx.on(Events.TURN_END, self._on_turn_end)
        ctx.on(COLLECT_STATUS_SLOTS, self._contribute_status)
        ctx.tools.register(
            Tool.from_function(self.create_goal, name="create_goal"),
        )
        ctx.tools.register(
            Tool.from_function(self.get_goal, name="get_goal"),
        )
        ctx.tools.register(
            Tool.from_function(self.update_goal, name="update_goal"),
        )
        ctx.commands.register(Command(
            name="goal",
            description="Set or manage the persistent session goal.",
            handler=self._goal_command,
            usage=(
                "/goal | /goal [--token-budget <tokens>] <objective> | "
                "/goal pause|resume|clear|complete <summary>|block <summary>"
            ),
            examples=(
                "/goal Stabilize the C/S API",
                "/goal pause",
                "/goal complete Implementation, tests, and docs are complete",
            ),
            exclusive=False,
        ))

    async def create_goal(
        self,
        objective: str,
        token_budget: int | None = None,
    ) -> ToolResult:
        """Create the persistent session goal explicitly requested by the human.

        Call only when the human explicitly asks the Agent to create a
        persistent Goal. Complexity, duration, or a Todo list is not such a
        request. Never rewrite the human's objective into a new Goal. Only one
        active Goal may exist; use get_goal when its state is unknown.

        Args:
            objective: Concrete outcome that determines when the Goal is complete.
            token_budget: Optional positive total-token budget supplied by the human.
        """
        return await self._create(objective, None, token_budget)

    async def get_goal(self) -> ToolResult:
        """Read the current session Goal without changing or advancing it.

        Use this when Goal status, objective, summary, or budget is needed. It
        returns no Goal when the session has none.
        """
        return await self._get()

    async def update_goal(
        self,
        status: Literal["complete", "blocked"],
        summary: str,
    ) -> ToolResult:
        """Finish the active Goal after reaching a terminal outcome.

        Compare the entire objective with observed evidence. Complete only when
        every outcome and required check is finished; name concrete tests,
        checks, or artifacts. Intent, confidence, and a started task are not
        evidence. Continue if work, Todo items, or verification remain. Block
        only when an exact external condition prevents progress after reasonable
        attempts; transient errors are not enough. Otherwise do not call this
        Tool. A terminal transition stops automatic turns; then summarize.

        Args:
            status: Terminal state, either complete or blocked.
            summary: Concise evidence of completion or the exact blocking condition.
        """
        if status not in {"complete", "blocked"}:
            return ToolResult.failure(
                "invalid_status",
                "Goal status must be complete or blocked",
            )
        return await self._finish(
            "block" if status == "blocked" else "complete",
            summary,
        )

    async def _goal_command(
        self,
        raw_args: str,
    ) -> CommandResult:
        action, value, token_budget = _parse_goal_command(raw_args)
        if action == "get":
            result = await self._get()
        elif action == "set":
            result = await self._set(value, token_budget)
        elif action == "pause":
            result = await self._pause()
        elif action == "resume":
            result = await self._resume()
        elif action == "clear":
            result = await self._clear()
        else:
            result = await self._finish(action, value)
        if result.status == "success" and action in {"set", "resume"}:
            await self.start()
        return _command_result(result)

    async def _get(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.success("No goal has been created.")
        return ToolResult.success(_format_goal(goal))

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
        return ToolResult.success(_format_goal(goal))

    async def _set(
        self,
        objective: str | None,
        token_budget: int | None,
    ) -> ToolResult:
        error = _text_error("objective", objective)
        if error is not None:
            return error
        if token_budget is not None and token_budget < 1:
            return ToolResult.failure(
                "invalid_token_budget",
                "Goal token budget must be a positive integer",
            )
        goal = {
            "objective": objective.strip(),
            "status": "active",
            "summary": "",
            "token_budget": token_budget,
        }
        await self.store.set("goal", goal)
        return ToolResult.success(_format_goal(goal))

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
        )

    async def _resume(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.failure("no_goal", "No goal exists to resume")
        if goal["status"] == "active":
            return ToolResult.failure("goal_active", "The goal is already active")
        goal["status"] = "active"
        await self.store.set("goal", goal)
        return ToolResult.success(_format_goal(goal))

    async def _pause(self) -> ToolResult:
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        goal["status"] = "paused"
        await self.store.set("goal", goal)
        return ToolResult.success(_format_goal(goal))

    async def _clear(self) -> ToolResult:
        goal = await self._read_goal()
        if goal is None:
            return ToolResult.success("No goal has been created.")
        await self.store.delete("goal")
        return ToolResult.success("No goal has been created.")

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

    async def _start_goal_turn(self, ctx: EventContext) -> None:
        if not ctx.continuation:
            return
        self._continuation_pending = False
        goal = await self._active_goal()
        if goal is not None:
            ctx.user_input = _goal_context(goal)

    async def _on_turn_end(self, ctx: EventContext) -> None:
        if ctx.stop_reason == "client_interrupt":
            goal = await self._active_goal()
            if goal is None:
                return
            goal["status"] = "paused"
            await self.store.set("goal", goal)
            return
        await self.start()

    async def start(self) -> None:
        """Schedule the next active-goal turn if one is not already pending."""
        goal = await self._active_goal()
        engine = cast(
            AgentLoopDriverPort | None,
            self.ctx.get("engine", strict=False),
        )
        if goal is None or self._continuation_pending or engine is None:
            return
        self._continuation_pending = True
        await engine.followup(
            "[goal continuation]",
            source="goal",
            metadata={"continuation": True},
        )

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
    context = {
        "objective": goal["objective"],
        "status": goal["status"],
    }
    if goal["token_budget"] is not None:
        context["token_budget"] = goal["token_budget"]
    if goal["summary"]:
        context["summary"] = goal["summary"]
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _no_active_goal() -> ToolResult:
    return ToolResult.failure("no_active_goal", "No active goal exists")


def _parse_goal_command(raw_args: str) -> tuple[str, str | None, int | None]:
    text = raw_args.strip()
    if not text or text in {"get", "status"}:
        return "get", None, None
    if text in {"pause", "resume", "clear"}:
        return text, None, None
    for action in ("complete", "block"):
        if text == action:
            return action, None, None
        prefix = f"{action} "
        if text.startswith(prefix):
            return action, text[len(prefix):].strip(), None

    token_budget = None
    if text.startswith("--token-budget"):
        budget_text, separator, objective = text.removeprefix(
            "--token-budget"
        ).strip().partition(" ")
        if not separator:
            return "set", "", 0
        try:
            token_budget = int(budget_text)
        except ValueError:
            token_budget = 0
        text = objective.strip()
    return "set", text, token_budget


def _command_result(result: ToolResult) -> CommandResult:
    return CommandResult(
        message=result.content,
        status="ok" if result.status == "success" else "error"
    )


plugin = GoalPlugin()
