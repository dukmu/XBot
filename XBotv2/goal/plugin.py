"""Persistent thread Goal state and automatic continuation."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Literal

from xcore import Context
from xcore.state import StateService

from XBotv2.agentloop import AgentLoopDriverPort, EventContext, Events
from XBotv2.application import COLLECT_STATUS_SLOTS, StatusSlots
from XBotv2.commands import Command, CommandResult
from XBotv2.core import Tool, ToolResult
from XBotv2.goal.models import GOAL_STATUSES, GoalSnapshot


_MAX_TEXT_CHARS = 2_000


class GoalService:
    """Own one thread's typed Goal state and continuation scheduling."""

    def __init__(self, store: StateService, engine: AgentLoopDriverPort) -> None:
        self._store = store
        self._engine = engine
        self._continuation_pending = False

    async def snapshot(self) -> GoalSnapshot | None:
        stored = await self._store.get("snapshot")
        if stored is None:
            return None
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted Goal snapshot must be an object")
        return GoalSnapshot.from_dict(stored)

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
        goal = await self.snapshot()
        if goal is None:
            return ToolResult.success("No goal has been created.")
        return ToolResult.success(_format_goal(goal))

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
                "invalid_status", "Goal status must be complete or blocked"
            )
        return await self._finish(
            "block" if status == "blocked" else "complete", summary
        )

    async def command(self, raw_args: str) -> CommandResult:
        action, value, token_budget = _parse_goal_command(raw_args)
        if action == "get":
            result = await self.get_goal()
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

    async def contribute_status(self, slots: StatusSlots) -> None:
        goal = await self.snapshot()
        if goal is not None:
            slots.add("goal", goal.status)

    async def start_goal_turn(self, event: EventContext) -> None:
        if not event.continuation:
            return
        self._continuation_pending = False
        goal = await self._active_goal()
        if goal is not None:
            event.user_input = _goal_context(goal)

    async def on_turn_end(self, event: EventContext) -> None:
        if event.stop_reason == "client_interrupt":
            goal = await self._active_goal()
            if goal is not None:
                await self._write(goal.model_copy(update={"status": "paused"}))
            return
        await self.start()

    async def start(self) -> None:
        """Schedule the next active-goal turn if one is not already pending."""
        goal = await self._active_goal()
        if goal is None or self._continuation_pending:
            return
        self._continuation_pending = True
        await self._engine.followup(
            "[goal continuation]",
            source="goal",
            metadata={"continuation": True},
        )

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
        if not _valid_budget(token_budget):
            return _invalid_budget()
        current = await self.snapshot()
        if current is not None and current.status == "active":
            return ToolResult.failure(
                "goal_exists",
                "Complete, block, or clear the active goal before creating another",
            )
        goal = GoalSnapshot(
            objective=objective.strip(),
            summary=summary.strip() if summary is not None else "",
            token_budget=token_budget,
        )
        await self._write(goal)
        return ToolResult.success(_format_goal(goal))

    async def _set(
        self, objective: str | None, token_budget: int | None
    ) -> ToolResult:
        error = _text_error("objective", objective)
        if error is not None:
            return error
        if not _valid_budget(token_budget):
            return _invalid_budget()
        goal = GoalSnapshot(
            objective=objective.strip(),
            token_budget=token_budget,
        )
        await self._write(goal)
        return ToolResult.success(_format_goal(goal))

    async def _finish(self, action: str, summary: str | None) -> ToolResult:
        error = _text_error("summary", summary)
        if error is not None:
            return error
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        status = "blocked" if action == "block" else "complete"
        updated = goal.model_copy(update={"status": status, "summary": summary.strip()})
        await self._write(updated)
        message = "Goal completed." if status == "complete" else "Goal blocked."
        return ToolResult.success(
            f"{message}\nExecution summary: {updated.summary}"
        )

    async def _resume(self) -> ToolResult:
        goal = await self.snapshot()
        if goal is None:
            return ToolResult.failure("no_goal", "No goal exists to resume")
        if goal.status == "active":
            return ToolResult.failure("goal_active", "The goal is already active")
        updated = goal.model_copy(update={"status": "active"})
        await self._write(updated)
        return ToolResult.success(_format_goal(updated))

    async def _pause(self) -> ToolResult:
        goal = await self._active_goal()
        if goal is None:
            return _no_active_goal()
        updated = goal.model_copy(update={"status": "paused"})
        await self._write(updated)
        return ToolResult.success(_format_goal(updated))

    async def _clear(self) -> ToolResult:
        goal = await self.snapshot()
        if goal is None:
            return ToolResult.success("No goal has been created.")
        await self._store.delete("snapshot")
        return ToolResult.success("No goal has been created.")

    async def _active_goal(self) -> GoalSnapshot | None:
        goal = await self.snapshot()
        if goal is None or goal.status != "active":
            return None
        return goal

    async def _write(self, goal: GoalSnapshot) -> None:
        await self._store.set("snapshot", goal.to_dict())


class GoalPlugin:
    """Register a GoalService for each mounted application."""

    inject = ["tools", "commands", "engine", "state"]
    name = "goal"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        service = GoalService(ctx.state.namespace(self.name), ctx.engine)
        ctx.set("goal", service)
        ctx.on(Events.TURN_START, service.start_goal_turn)
        ctx.on(Events.TURN_END, service.on_turn_end)
        ctx.on(COLLECT_STATUS_SLOTS, service.contribute_status)
        ctx.tools.register(Tool.from_function(service.create_goal, name="create_goal"))
        ctx.tools.register(Tool.from_function(service.get_goal, name="get_goal"))
        ctx.tools.register(Tool.from_function(service.update_goal, name="update_goal"))
        ctx.commands.register(Command(
            name="goal",
            description="Set or manage the persistent session goal.",
            handler=service.command,
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

    def diagnostics(self) -> dict[str, object]:
        return {
            "status": "ready",
            "scope": "session",
            "goal_statuses": sorted(GOAL_STATUSES),
            "automatic_continuation": True,
        }


def _text_error(field: str, value: str | None) -> ToolResult | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return ToolResult.failure(
            f"invalid_{field}", f"Goal {field} must not be empty"
        )
    if len(text) > _MAX_TEXT_CHARS:
        return ToolResult.failure(
            f"{field}_too_long",
            f"Goal {field} must not exceed {_MAX_TEXT_CHARS} characters",
        )
    return None


def _valid_budget(value: int | None) -> bool:
    return value is None or type(value) is int and value > 0


def _invalid_budget() -> ToolResult:
    return ToolResult.failure(
        "invalid_token_budget", "Goal token budget must be a positive integer"
    )


def _format_goal(goal: GoalSnapshot) -> str:
    lines = [f"[{goal.status}] {goal.objective}"]
    if goal.token_budget is not None:
        lines.append(f"Token budget: {goal.token_budget}")
    if goal.summary:
        lines.append(f"Execution summary: {goal.summary}")
    return "\n".join(lines)


def _goal_context(goal: GoalSnapshot) -> str:
    context: dict[str, object] = {
        "objective": goal.objective,
        "status": goal.status,
    }
    if goal.token_budget is not None:
        context["token_budget"] = goal.token_budget
    if goal.summary:
        context["summary"] = goal.summary
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
        status="ok" if result.status == "success" else "error",
    )


plugin = GoalPlugin()

__all__ = ["GoalPlugin", "GoalService"]
