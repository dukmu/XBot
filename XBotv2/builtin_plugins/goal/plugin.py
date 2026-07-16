"""Persistent session goal state machine."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    Command,
    CommandResult,
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)


_MAX_TEXT_CHARS = 2_000
_STATUSES = {"active", "complete", "blocked", "paused"}
_GOAL_TOOLS = {"create_goal", "get_goal", "update_goal"}


class GoalPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._continuation_pending = False

    async def on_unload(self) -> None:
        self._continuation_pending = False

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_TURN_START, self._start_goal_turn)
        ctx.register_hook(HookStage.ON_TURN_END, self._on_turn_end)
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._allow_goal)
        ctx.register_hook(
            HookStage.BEFORE_MAILBOX_DELIVERY,
            self._on_mailbox_delivery,
        )
        ctx.register_tool(
            Tool.from_function(self.create_goal, name="create_goal"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:goal",
            ),
        )
        ctx.register_tool(
            Tool.from_function(self.get_goal, name="get_goal"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:goal",
            ),
        )
        ctx.register_tool(
            Tool(
                name="update_goal",
                description=(
                    "Finish the active goal. Use complete only when all required "
                    "work is done, or blocked when progress cannot continue."
                ),
                function=self.update_goal,
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["complete", "blocked"],
                        },
                        "summary": {
                            "type": "string",
                            "description": "Execution or blocking summary.",
                        },
                    },
                    "required": ["status", "summary"],
                    "additionalProperties": False,
                },
            ),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:goal",
            ),
        )
        ctx.register_command(Command(
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
        ))

    async def create_goal(
        self,
        objective: str,
        token_budget: int | None = None,
    ) -> ToolResult:
        """Create the persistent session goal explicitly requested by the human.

        Use this only when the human asks for sustained autonomous work across
        turns. Do not infer a Goal from an ordinary task. Only one active Goal may
        exist; inspect it with get_goal before replacing prior state.

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

    async def update_goal(self, status: str, summary: str) -> ToolResult:
        """Finish the active Goal after reaching a terminal outcome.

        Use complete only after every required outcome is verified. Use blocked
        only when progress cannot continue without external change; transient
        Provider or internal errors are not a blocked Goal. This transition stops
        automatic Goal turns, after which the assistant must summarize to human.

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

    async def _allow_goal(self, ctx: HookContext):
        if ctx.tool_call is not None and ctx.tool_call.name in _GOAL_TOOLS:
            return HookDecision(
                HookAction.ALLOW,
                "Goal state operations are pre-approved by the Goal plugin",
            )

    async def _goal_command(self, ctx: Any, raw_args: str) -> CommandResult:
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
            await self.start(ctx.enqueue_general)
        return _command_result(result)

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
        return ToolResult.success("Set the active goal.", data={"goal": goal})

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

    async def _start_goal_turn(self, ctx: HookContext) -> None:
        item = ctx.mailbox_message
        message = getattr(item, "message", None)
        if not (
            getattr(item, "kind", None) == "general"
            and isinstance(message, dict)
            and message.get("source") == "goal"
            and message.get("event") == "continue"
        ):
            return
        goal = await self._active_goal()
        if goal is not None:
            ctx.user_input = _goal_context(goal)

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
    lines.append(
        "Persist with this objective. Call update_goal with status=complete and "
        "a concise execution summary only after all required work is finished; "
        "use status=blocked only when progress cannot continue. After the transition, "
        "give the human a concise final summary."
    )
    return "\n\n".join(lines)


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
        data=result.data,
    )
