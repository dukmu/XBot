"""Explicit same-session goals driven by the working Agent."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue
from xcore import Context
from xcore.state import StateService

from XBotv2.agentloop import AgentLoopDriverPort, Events, InboxItem, RuntimeInput
from XBotv2.agentloop.events import (
    AfterToolExecution,
    LoopFailure,
    OnTurnInput,
    RejectInput,
    SessionLifecycle,
    TurnEnded,
)
from XBotv2.application import COLLECT_STATUS_SLOTS, RUNTIME_EVENT, RuntimeEvent, StatusSlots
from XBotv2.commands import Command, CommandEffect, CommandResult
from XBotv2.core import Tool, ToolFailed, ToolOutcome, ToolSucceeded, failed_text, succeeded_text
from XBotv2.core.domain import InboxTarget, TokenCounters, UsageSnapshot
from XBotv2.core.parts import TextPart
from XBotv2.goal.models import (
    ActiveGoal,
    BlockedGoal,
    CompleteGoal,
    GoalChanged,
    GoalSnapshot,
    GoalState,
    GoalStats,
    MAX_GOAL_OBJECTIVE_CHARS,
    NoGoal,
    PausedGoal,
)
from XBotv2.jobs import JOB_COMPLETED, JobView, JobsCommandPort

_CLEAR_ALIASES = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})


class UsagePort(Protocol):
    def snapshot(self) -> UsageSnapshot: ...


class _StoredGoal(BaseModel):
    state: GoalState
    usage_baseline: TokenCounters
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoalService:
    """Own one persisted goal and its process-local continuation authority."""

    def __init__(
        self,
        store: StateService,
        engine: AgentLoopDriverPort,
        *,
        events: Context,
        usage: UsagePort,
        jobs: JobsCommandPort | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._engine = engine
        self._events = events
        self._jobs = jobs
        self._usage = usage
        self._now = now
        self._armed = False
        self._turn_goal_id: str | None = None
        self._lock = asyncio.Lock()

    async def snapshot(self) -> GoalSnapshot:
        state = await self._read_state()
        activation = "armed" if isinstance(state, ActiveGoal) and self._armed else (
            "disarmed" if isinstance(state, ActiveGoal) else "none"
        )
        return GoalSnapshot(state=state, activation=activation)

    async def _read_state(self) -> GoalState:
        return (await self._read_record()).state

    async def _read_record(self) -> _StoredGoal:
        stored = await self._store.get("snapshot")
        if stored is None:
            return _StoredGoal(state=NoGoal(), usage_baseline=TokenCounters())
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted Goal snapshot must be an object")
        return _StoredGoal.model_validate(stored)

    async def _write(
        self, state: GoalState, *, usage_baseline: TokenCounters
    ) -> None:
        record = _StoredGoal(state=state, usage_baseline=usage_baseline)
        await self._store.set("snapshot", record.model_dump(mode="json"))

    async def create_goal(self, objective: str) -> ToolOutcome:
        """Create a goal only for an explicitly requested persistent task.

        Do not infer a goal merely because work is long. The goal continues in
        this session until you explicitly call update_goal with complete,
        blocked, or paused.

        Args:
            objective: The user's concrete completion objective.
        """
        return await self._create(objective, start_now=False, allow_replace=False)

    async def get_goal(self) -> ToolOutcome:
        """Read the exact current goal, revision, activation, and status."""
        return await self.status()

    async def update_goal(
        self,
        goal_id: str,
        revision: int,
        status: Literal["complete", "blocked", "paused"],
        reason: str,
    ) -> ToolOutcome:
        """Explicitly finish, block, or pause the exact current goal revision.

        Args:
            goal_id: Exact identifier returned by create_goal or get_goal.
            revision: Exact current revision returned by create_goal or get_goal.
            status: complete only after checking evidence; blocked only when
                progress cannot continue; paused only when the user explicitly
                asked to pause this goal.
            reason: Concise evidence or blocking explanation.
        """
        text = reason.strip()
        if not text:
            return failed_text("invalid_reason", "Goal reason must not be empty")
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal):
                return failed_text("no_goal", "No goal set")
            if current.goal_id != goal_id or current.revision != revision:
                return failed_text("stale_goal", "Goal id or revision is no longer current")
            if not isinstance(current, ActiveGoal):
                return failed_text("goal_not_active", f"Goal is already {current.kind}")
            current = await self._refresh_stats(current)
            common = dict(
                goal_id=current.goal_id,
                revision=current.revision + 1,
                objective=current.objective,
                started_at=current.started_at,
                stats=current.stats,
                reason=text,
            )
            if status == "complete":
                updated: GoalState = CompleteGoal(finished_at=self._now(), **common)
            elif status == "blocked":
                updated = BlockedGoal(blocked_at=self._now(), **common)
            else:
                updated = PausedGoal(paused_at=self._now(), **common)
            await self._write(
                updated, usage_baseline=self._usage_snapshot().total_counters
            )
            self._armed = False
            await self._remove_pending(current)
            await self._emit(updated)
        return succeeded_text(_format_snapshot(GoalSnapshot(state=updated)))

    async def command(self, raw_args: str) -> CommandResult:
        text = raw_args.strip()
        lowered = text.lower()
        if not text or lowered in {"get", "status"}:
            return _command_result(await self.status())
        if lowered in _CLEAR_ALIASES:
            return _command_result(await self.clear(), effects=("thread",))
        if lowered in {"resume", "continue"}:
            return _command_result(await self.resume(), effects=("thread",))
        if lowered == "pause":
            return _command_result(await self.pause(), effects=("thread",))
        return _command_result(
            await self._create(text, start_now=True, allow_replace=True),
            effects=("thread",),
        )

    async def status(self) -> ToolOutcome:
        async with self._lock:
            state = await self._refresh_stats(await self._read_state())
            snapshot = GoalSnapshot(
                state=state,
                activation=("armed" if isinstance(state, ActiveGoal) and self._armed else
                            "disarmed" if isinstance(state, ActiveGoal) else "none"),
            )
        return succeeded_text(_format_snapshot(snapshot))

    async def _create(
        self, objective: str, *, start_now: bool, allow_replace: bool
    ) -> ToolOutcome:
        text = objective.strip()
        if not text:
            return failed_text("invalid_objective", "Goal objective must not be empty")
        if len(text) > MAX_GOAL_OBJECTIVE_CHARS:
            return failed_text(
                "objective_too_long",
                f"Goal objective must not exceed {MAX_GOAL_OBJECTIVE_CHARS} characters",
            )
        async with self._lock:
            previous = await self._read_state()
            if isinstance(previous, ActiveGoal) and not allow_replace:
                return failed_text(
                    "goal_already_active",
                    "An active goal already exists; use get_goal or update_goal",
                )
            state = ActiveGoal(
                goal_id=uuid4().hex,
                revision=1,
                objective=text,
                started_at=self._now(),
                stats=GoalStats(rounds_started=0 if start_now else 1),
            )
            await self._write(
                state, usage_baseline=self._usage_snapshot().total_counters
            )
            self._armed = True
            if isinstance(previous, ActiveGoal):
                await self._remove_pending(previous)
            await self._emit(state)
            if not start_now:
                self._turn_goal_id = state.goal_id
        if start_now:
            await self._queue_continuation()
        return succeeded_text(_format_snapshot(await self.snapshot()))

    async def pause(self) -> ToolOutcome:
        snapshot = await self.snapshot()
        current = snapshot.state
        if isinstance(current, NoGoal):
            return failed_text("no_goal", "No goal set")
        if not isinstance(current, ActiveGoal):
            return failed_text("goal_not_active", f"Goal is already {current.kind}")
        return await self.update_goal(
            current.goal_id,
            current.revision,
            "paused",
            "Paused by user.",
        )

    async def resume(self) -> ToolOutcome:
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal):
                return failed_text("no_goal", "No goal set")
            if isinstance(current, CompleteGoal):
                return failed_text("goal_complete", "A complete goal cannot be resumed")
            if isinstance(current, ActiveGoal):
                if self._armed:
                    return failed_text("goal_active", "Goal continuation is already armed")
                updated = current.model_copy(update={
                    "revision": current.revision + 1,
                    "pending_input_id": None,
                })
            else:
                updated = ActiveGoal(
                    goal_id=current.goal_id,
                    revision=current.revision + 1,
                    objective=current.objective,
                    started_at=current.started_at,
                    stats=current.stats,
                )
            await self._write(
                updated, usage_baseline=self._usage_snapshot().total_counters
            )
            self._armed = True
            if isinstance(current, ActiveGoal):
                await self._remove_pending(current)
            await self._emit(updated)
        await self._queue_continuation()
        return succeeded_text(_format_snapshot(await self.snapshot()))

    async def clear(self) -> ToolOutcome:
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal):
                return succeeded_text("No goal set.")
            await self._write(
                NoGoal(), usage_baseline=await self._read_usage_baseline()
            )
            self._armed = False
            if isinstance(current, ActiveGoal):
                await self._remove_pending(current)
            await self._emit(NoGoal())
        return succeeded_text(f"Goal cleared: {current.objective}")

    async def on_turn_input(self, event: OnTurnInput):
        payload = event.input.input
        if not isinstance(payload, RuntimeInput) or payload.source != "goal" or payload.event != "continue":
            return None
        async with self._lock:
            current = await self._read_state()
            if (
                not isinstance(current, ActiveGoal)
                or not self._armed
                or current.pending_input_id != event.input.id
            ):
                if isinstance(current, ActiveGoal) and current.pending_input_id == event.input.id:
                    await self._write(
                        current.model_copy(update={"pending_input_id": None}),
                        usage_baseline=await self._read_usage_baseline(),
                    )
                return RejectInput(error="Stale goal continuation")
            stats = current.stats.model_copy(update={
                "rounds_started": current.stats.rounds_started + 1,
            })
            updated = current.model_copy(update={"pending_input_id": None, "stats": stats})
            self._turn_goal_id = current.goal_id
            await self._write(
                updated, usage_baseline=await self._read_usage_baseline()
            )
            await self._emit(updated)
        return None

    async def on_tool_call(self, _event: AfterToolExecution) -> None:
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal) or current.goal_id != self._turn_goal_id:
                return
            updated = current.model_copy(update={
                "stats": current.stats.model_copy(update={
                    "tool_calls": current.stats.tool_calls + 1,
                }),
            })
            await self._write(
                updated, usage_baseline=await self._read_usage_baseline()
            )
            await self._emit(updated)

    async def on_turn_end(self, event: TurnEnded) -> None:
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal):
                self._turn_goal_id = None
                return
            owned = current.goal_id == self._turn_goal_id
            if not owned:
                if (
                    isinstance(current, ActiveGoal)
                    and event.stop_reason == "client_interrupt"
                    and self._armed
                ):
                    self._armed = False
                    await self._emit(current)
                self._turn_goal_id = None
                return
            current = await self._refresh_stats(current)
            if isinstance(current, ActiveGoal) and event.stop_reason == "client_interrupt":
                self._armed = False
            self._turn_goal_id = None
            await self._write(
                current, usage_baseline=self._usage_snapshot().total_counters
            )
            await self._emit(current)
            should_continue = (
                isinstance(current, ActiveGoal)
                and self._armed
                and not self._background_running()
            )
        if should_continue:
            await self._queue_continuation()

    async def on_job_completed(self, _view: JobView) -> None:
        """Let the authoritative jobs event wake a deferred active goal once."""
        await self._queue_continuation()

    async def on_error(self, _event: LoopFailure) -> None:
        async with self._lock:
            current = await self._read_state()
            if isinstance(current, NoGoal):
                return
            owned = current.goal_id == self._turn_goal_id
            if owned:
                current = await self._refresh_stats(current)
            self._turn_goal_id = None
            pending_owner = current if isinstance(current, ActiveGoal) else None
            if isinstance(current, ActiveGoal):
                self._armed = False
                current = current.model_copy(update={"pending_input_id": None})
            baseline = (
                self._usage_snapshot().total_counters
                if owned
                else await self._read_usage_baseline()
            )
            await self._write(current, usage_baseline=baseline)
            if pending_owner is not None:
                await self._remove_pending(pending_owner)
            await self._emit(current)

    async def dispose(self) -> None:
        async with self._lock:
            current = await self._read_state()
            self._armed = False
            self._turn_goal_id = None
            if not isinstance(current, ActiveGoal):
                return
            updated = current.model_copy(update={"pending_input_id": None})
            await self._write(
                updated, usage_baseline=await self._read_usage_baseline()
            )
            await self._remove_pending(current)

    async def on_session_resume(self, _event: SessionLifecycle) -> None:
        async with self._lock:
            current = await self._read_state()
            self._armed = False
            self._turn_goal_id = None
            if isinstance(current, ActiveGoal):
                await self._remove_pending(current)
                updated = current.model_copy(update={"pending_input_id": None})
                await self._write(
                    updated, usage_baseline=await self._read_usage_baseline()
                )
                await self._emit(updated)

    async def on_session_close(self, _event: SessionLifecycle) -> None:
        self._armed = False
        self._turn_goal_id = None

    async def contribute_status(self, slots: StatusSlots) -> None:
        snapshot = await self.snapshot()
        state = snapshot.state
        if isinstance(state, NoGoal):
            return
        state = await self._refresh_stats(state)
        slots.add("goal", state.kind)
        slots.add("goal_activation", snapshot.activation)
        slots.add("goal_objective", _short(state.objective, 120))
        slots.add("goal_round", str(state.stats.rounds_started))
        if isinstance(state, (PausedGoal, BlockedGoal, CompleteGoal)):
            slots.add("goal_reason", _short(state.reason, 60))
        if _has_stats(state.stats):
            slots.add("goal_stats", _format_stats(state.stats))

    async def _queue_continuation(self) -> None:
        async with self._lock:
            current = await self._read_state()
            if (
                not isinstance(current, ActiveGoal)
                or not self._armed
                or current.pending_input_id is not None
                or self._background_running()
            ):
                return
            item = InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=RuntimeInput(
                    source="goal",
                    event="continue",
                    content=_render_continuation(current),
                ),
            )
            reserved = current.model_copy(update={"pending_input_id": str(item.id)})
            await self._write(
                reserved, usage_baseline=await self._read_usage_baseline()
            )
            await self._emit(reserved)
            try:
                await self._engine.submit_input(item, wake=True)
            except BaseException:
                await self._write(
                    current, usage_baseline=await self._read_usage_baseline()
                )
                await self._emit(current)
                raise

    async def _remove_pending(self, state: ActiveGoal) -> None:
        input_id = state.pending_input_id
        if input_id is None:
            return
        try:
            await self._engine.remove_input(input_id)
        except KeyError:
            # It may already be claimed; ON_TURN_INPUT rejects it after this
            # control mutation changes or clears the owning reservation.
            pass

    def _background_running(self) -> bool:
        if self._jobs is None:
            return False
        return any(view.state in {"queued", "running"} for view in self._jobs.views())

    def _usage_snapshot(self) -> UsageSnapshot:
        return self._usage.snapshot()

    async def _read_usage_baseline(self) -> TokenCounters:
        return (await self._read_record()).usage_baseline

    async def _refresh_stats(self, state: GoalState) -> GoalState:
        if isinstance(state, NoGoal):
            return state
        if state.goal_id != self._turn_goal_id:
            return state
        baseline = await self._read_usage_baseline()
        counters = self._usage_snapshot().total_counters
        stats = state.stats.model_copy(update={
            "input_tokens": state.stats.input_tokens + max(0, counters.input - baseline.input),
            "output_tokens": state.stats.output_tokens + max(0, counters.output - baseline.output),
        })
        return state.model_copy(update={"stats": stats})

    async def _emit(self, state: GoalState) -> None:
        activation = "armed" if isinstance(state, ActiveGoal) and self._armed else (
            "disarmed" if isinstance(state, ActiveGoal) else "none"
        )
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=GoalChanged(snapshot=GoalSnapshot(state=state, activation=activation))),
        )


def _render_continuation(goal: ActiveGoal) -> str:
    return (
        '<system_reminder source="goal" event="continue">\n'
        f"Objective: {json.dumps(goal.objective, ensure_ascii=False)}\n\n"
        "Continue working toward this objective in the same session. Inspect "
        "the current workspace and tool results; do not rely on narration as "
        "proof. When the objective is actually achieved, call update_goal with "
        f"goal_id={json.dumps(goal.goal_id)}, revision={goal.revision}, and "
        "status=\"complete\". If progress cannot continue, use status=\"blocked\"; "
        "use status=\"paused\" only if the user explicitly asked to pause. "
        "Otherwise keep making "
        "concrete progress.\n</system_reminder>"
    )


def _format_snapshot(snapshot: GoalSnapshot) -> str:
    state = snapshot.state
    if isinstance(state, NoGoal):
        return "No goal set."
    lines = [
        f"[{state.kind}] {state.objective}",
        f"Goal: {state.goal_id} revision {state.revision} · continuation {snapshot.activation}",
    ]
    if isinstance(state, (PausedGoal, BlockedGoal, CompleteGoal)):
        lines.append(f"Reason: {state.reason}")
    return "\n".join(lines)


def _format_stats(stats: GoalStats) -> str:
    return f"{stats.total_tokens} tokens, {stats.tool_calls} tool calls"


def _has_stats(stats: GoalStats) -> bool:
    return bool(stats.rounds_started or stats.tool_calls or stats.total_tokens)


def _short(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _command_result(result: ToolOutcome, *, effects: tuple[CommandEffect, ...] = ()) -> CommandResult:
    return CommandResult(
        message=_tool_outcome_text(result),
        status="ok" if isinstance(result, ToolSucceeded) else "error",
        effects=effects,
    )


def _tool_outcome_text(result: ToolOutcome) -> str:
    if isinstance(result, (ToolSucceeded, ToolFailed)):
        return "".join(part.text for part in result.output.parts if isinstance(part, TextPart)) or (
            result.error.message if isinstance(result, ToolFailed) else ""
        )
    return result.reason


class GoalPlugin:
    inject = {
        "required": ["commands", "engine", "state", "tools", "usage"],
        "optional": ["jobs"],
    }
    name = "goal"
    def apply(self, ctx: Context, config: object | None = None) -> None:
        if config is not None and (not isinstance(config, Mapping) or bool(config)):
            raise ValueError("goal plugin does not accept configuration")
        service = GoalService(
            ctx.state.namespace(self.name),
            ctx.engine,
            events=ctx,
            jobs=ctx.get("jobs", strict=False),
            usage=ctx.usage,
        )
        ctx.set("goal", service)
        ctx.on(Events.ON_TURN_INPUT, service.on_turn_input)
        ctx.on(Events.AFTER_TOOL_CALL, service.on_tool_call)
        ctx.on(Events.TURN_END, service.on_turn_end)
        ctx.on(JOB_COMPLETED, service.on_job_completed)
        ctx.on(Events.ON_ERROR, service.on_error)
        ctx.on(Events.SESSION_RESUME, service.on_session_resume)
        ctx.on(Events.SESSION_CLOSE, service.on_session_close)
        ctx.on(COLLECT_STATUS_SLOTS, service.contribute_status)
        for function in (service.create_goal, service.get_goal, service.update_goal):
            ctx.tools.register(replace(Tool.from_function(function), kind="think"))
        ctx.commands.register(Command(
            name="goal",
            description="Manage an explicit same-session goal.",
            handler=service.command,
            effects=("thread",),
            usage="/goal | /goal <objective> | /goal pause | /goal resume | /goal clear",
            examples=("/goal all tests pass", "/goal pause", "/goal resume", "/goal clear"),
            exclusive=False,
        ))
        ctx.dispose(service.dispose)

    def diagnostics(self) -> dict[str, JsonValue]:
        return {
            "status": "ready",
            "scope": "session",
            "goal_statuses": ["none", "active", "paused", "blocked", "complete"],
            "continuation": "same_session",
            "commands": [
                "/goal", "/goal <objective>", "/goal pause",
                "/goal resume", "/goal clear",
            ],
        }


plugin = GoalPlugin()

__all__ = ["GoalPlugin", "GoalService"]
