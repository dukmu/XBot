"""Session-scoped Goal loop: set a condition, evaluate after every turn.

Mirrors Claude Code's ``/goal``: a human sets a completion condition, an
independent evaluator model judges it after each turn, and the loop keeps
starting turns until the condition is met or judged impossible. The working
model never certifies its own completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal, Protocol
from xml.sax.saxutils import escape
from uuid import uuid4

from pydantic import JsonValue
from xcore import Context
from xcore.state import StateService

from XBotv2.agentloop import (
    AgentLoopDriverPort,
    Events,
    InboxItem,
    InboxTarget,
    RuntimeInput,
)
from XBotv2.agentloop.events import (
    AfterToolExecution,
    LoopFailure,
    SessionLifecycle,
    TurnEnded,
    TurnStarted,
)
from XBotv2.application import (
    COLLECT_STATUS_SLOTS,
    RUNTIME_EVENT,
    RuntimeEvent,
    StatusSlots,
)
from XBotv2.commands import Command, CommandEffect, CommandResult
from XBotv2.core import Tool, ToolOutcome, ToolFailed, ToolSucceeded, failed_text, succeeded_text
from XBotv2.core.messages import RuntimeNoticeMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ModelRequest
from XBotv2.core.domain import (
    AuxiliaryRequest,
    RequestObservation,
    ResolvedModelSelection,
    UsageSnapshot,
)
from XBotv2.core.tokens import estimate_request_tokens
from XBotv2.goal.evaluator import (
    evaluation_request,
    parse_verdict,
    render_transcript,
)
from XBotv2.goal.models import (
    AchievedGoal,
    ActiveGoal,
    FailedGoal,
    MAX_GOAL_CONDITION_CHARS,
    GoalConfig,
    GoalChanged,
    GoalCheckinPolicy,
    GoalProgress,
    GoalSnapshot,
    GoalState,
    GoalStats,
    GoalVerdict,
    Impossible,
    Met,
    NoGoal,
    NotMet,
    PausedGoal,
)
from XBotv2.llm import ModelPort, invoke_llm
from XBotv2.session import HISTORY_CHANGED, HistoryChanged

logger = logging.getLogger("xbotv2.goal")

_STATS_SLOT_KEY = "goal_stats"
_REASON_SLOT_KEY = "goal_reason"
_CLEAR_ALIASES = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})
_COMPACT_OPERATION = "compact:"
_EVALUATOR_OUTPUT_TOKENS = 512

# Claude Code clears a goal outright when the failure cannot be retried away.
_UNRECOVERABLE_HINTS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "api key",
    "credit",
    "quota",
    "insufficient_quota",
    "billing",
    "context length",
    "context_length",
    "context overflow",
    "maximum context",
    "model not found",
    "model_not_found",
    "invalid model",
)


class UsagePort(Protocol):
    """Cumulative-thread usage consumed by Goal statistics."""

    def snapshot(self) -> UsageSnapshot: ...

    async def record(self, observation, usage) -> UsageSnapshot: ...


class JobsPort(Protocol):
    """Live jobs the loop defers evaluation for."""

    def snapshots(self) -> Sequence[Any]: ...


class GoalService:
    """Own one session's Goal condition, evaluation loop, and status."""

    def __init__(
        self,
        store: StateService,
        engine: AgentLoopDriverPort,
        *,
        model: ModelPort,
        events: Context | None = None,
        jobs: JobsPort | None = None,
        usage: UsagePort | None = None,
        model_selection: Callable[[], ResolvedModelSelection] | None = None,
        todolist_getter: Callable[[], Any | None] | None = None,
        config: GoalConfig | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._engine = engine
        self._model = model
        self._events = events
        self._jobs = jobs
        self._usage = usage
        self._model_selection = model_selection
        self._todolist_getter = todolist_getter
        self._config = config or GoalConfig()
        self._now = now
        self._evaluation: asyncio.Task[None] | None = None
        self._timer: asyncio.Task[None] | None = None
        self._continuation_pending = False
        self._pending_tool_calls = 0
        self._turn_used_tools = False
        self._latest_reason = ""

    # -- state -------------------------------------------------------------

    async def snapshot(self) -> GoalSnapshot:
        stored = await self._store.get("snapshot")
        if stored is None:
            return GoalSnapshot(state=NoGoal())
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted Goal snapshot must be an object")
        return GoalSnapshot.model_validate(stored)

    async def _active_goal(self) -> ActiveGoal | None:
        state = (await self.snapshot()).state
        return state if isinstance(state, ActiveGoal) else None

    async def _write(self, state) -> None:
        snapshot = GoalSnapshot(state=state)
        await self._store.set("snapshot", snapshot.model_dump(mode="json"))

    # -- human command -----------------------------------------------------

    async def create_goal(self, condition: str) -> ToolOutcome:
        """Start a session goal that keeps working until the condition holds.

        Use this for substantial work with a verifiable end state when the
        human asked you to keep going until it is done, or when a long task
        needs an automatic continue-until-finished loop. Do not create a goal
        for a single request, and never rewrite a human's condition. One goal
        is active per session; a new condition replaces the previous one.

        Completion is not yours to declare: an independent evaluator judges
        the condition after every turn and continues, achieves, or fails the
        goal from what the conversation demonstrates. Write the condition as
        something your own output can prove, for example "all tests in
        test/auth pass and the lint step is clean".

        Args:
            condition: Measurable completion condition, up to 4000 characters.
        """
        return await self.set_condition(condition)

    async def get_goal(self) -> ToolOutcome:
        """Read the current session goal without changing it.

        Returns no goal when none is set. Use this when you need the condition,
        the evaluator's latest verdict and reason, how many turns have been
        evaluated, or the consumption recorded so far.
        """
        return await self.status()

    async def command(self, raw_args: str) -> CommandResult:
        text = raw_args.strip()
        if not text or text in {"get", "status"}:
            return _command_result(await self.status())
        if text.lower() in _CLEAR_ALIASES:
            return _command_result(await self.clear(), effects=("thread",))
        return _command_result(await self.set_condition(text), effects=("thread",))

    async def status(self) -> ToolOutcome:
        goal = (await self.snapshot()).state
        if isinstance(goal, NoGoal):
            return succeeded_text("No goal set.")
        goal = await self._refresh_stats(goal)
        return succeeded_text(_format_status(
            goal,
            now=self._now(),
            max_rounds=self._config.max_rounds,
            active_reason=self._latest_reason,
        ))

    async def set_condition(self, condition: str) -> ToolOutcome:
        text = condition.strip()
        if not text:
            return failed_text(
                "invalid_condition", "Goal condition must not be empty"
            )
        if len(text) > MAX_GOAL_CONDITION_CHARS:
            return failed_text(
                "condition_too_long",
                "Goal condition must not exceed "
                f"{MAX_GOAL_CONDITION_CHARS} characters",
            )
        previous = (await self.snapshot()).state
        goal = ActiveGoal(
            condition=text,
            started_at=self._now(),
        )
        self._latest_reason = ""
        self._pending_tool_calls = 0
        self._turn_used_tools = False
        # A queued turn for a replaced goal must not swallow the new one.
        self._continuation_pending = False
        self._cancel_timer()
        await self._write(goal)
        await self._write_usage_baseline()
        await self._emit(goal)
        message = _format_status(
            goal,
            now=self._now(),
            max_rounds=self._config.max_rounds,
            active_reason=self._latest_reason,
        )
        if isinstance(previous, ActiveGoal):
            message += f"\nReplaced active goal: {previous.condition}"
        # Claude Code starts a turn immediately, with the condition itself as
        # the directive, so no separate prompt is needed.
        await self._start_round(goal)
        return succeeded_text(message)

    async def clear(self) -> ToolOutcome:
        goal = (await self.snapshot()).state
        if isinstance(goal, NoGoal):
            return succeeded_text("No goal set.")
        self._cancel_timer()
        await self._write(NoGoal())
        await self._emit(NoGoal())
        self._latest_reason = ""
        return succeeded_text(f"Goal cleared: {goal.condition}")

    # -- lifecycle hooks ---------------------------------------------------

    async def on_turn_start(self, event: TurnStarted) -> None:
        self._turn_used_tools = False
        latest = event.history[-1] if event.history else None
        if (
            isinstance(latest, RuntimeNoticeMessage)
            and latest.source == "goal"
            and latest.event == "round"
        ):
            self._continuation_pending = False
            return
        # A human prompt restarts evaluation after a stall and resets the
        # idle check-in budget.
        goal = await self._active_goal()
        if goal is None:
            return
        progress = goal.progress
        if progress.stalled or progress.idle_checkins or progress.retries:
            await self._write(goal.model_copy(update={
                "progress": progress.model_copy(update={
                    "stalled": False,
                    "idle_checkins": 0,
                    "retries": 0,
                }),
                "checkin_policy": GoalCheckinPolicy(),
            }))

    async def on_tool_call(self, event: AfterToolExecution) -> None:
        self._turn_used_tools = True
        if await self._active_goal() is None:
            return
        self._pending_tool_calls += 1

    async def on_turn_end(self, event: TurnEnded) -> None:
        goal = await self._active_goal()
        if goal is None:
            return
        goal = await self._refresh_stats(goal, flush_pending=True)
        if event.stop_reason == "client_interrupt":
            await self._pause(goal, reason="Interrupted.")
            return
        goal = goal.model_copy(update={"progress": goal.progress.model_copy(update={
            "tool_less_turns": (
                0 if self._turn_used_tools else goal.progress.tool_less_turns + 1
            ),
        })})
        await self._write(goal)
        await self._emit(goal)
        if self._background_running():
            # Claude Code defers evaluation while a subagent or background
            # shell runs, and the completion turn evaluates afterwards.
            self._schedule_checkin(goal)
            return
        self._cancel_timer()
        self._schedule_evaluation()

    async def on_error(self, event: LoopFailure) -> None:
        goal = await self._active_goal()
        if goal is None:
            return
        goal = await self._refresh_stats(goal, flush_pending=True)
        error = event.error
        if _is_unrecoverable(error):
            await self._terminate(
                goal,
                outcome="failed",
                reason=(
                    "Stopped after an unrecoverable error: "
                    f"{error}."
                ),
            )
            return
        retries = goal.progress.retries + 1
        if retries > self._config.max_retries:
            await self._pause(
                goal,
                reason=f"Paused after {goal.progress.retries} retries: {error}",
            )
            return
        updated = goal.model_copy(update={
            "progress": goal.progress.model_copy(update={"retries": retries}),
        })
        self._latest_reason = f"Retrying after error: {error}"
        await self._write(updated)
        await self._emit(updated)
        self._schedule_retry(updated)

    async def on_session_resume(self, _event: SessionLifecycle) -> None:
        """Restore an active goal with its counters reset, as Claude Code does."""
        goal = await self._active_goal()
        if goal is None:
            return
        await self._write_usage_baseline()
        updated = goal.model_copy(update={
            "started_at": self._now(),
            "progress": GoalProgress(),
            "checkin_policy": GoalCheckinPolicy(),
        })
        self._latest_reason = ""
        await self._write(updated)
        await self._emit(updated)
        await self._start_round(updated)

    async def on_session_close(self, _event: SessionLifecycle) -> None:
        self.dispose()

    async def on_compaction(self, event: HistoryChanged) -> None:
        """Re-state the active goal once, after history is summarized.

        The summary may shadow the turn that set the goal, so this is the one
        place the objective and round are repeated after compaction.
        """
        if not event.operation.startswith(_COMPACT_OPERATION):
            return
        goal = await self._active_goal()
        if goal is None:
            return
        await self._engine.submit_input(
            InboxItem(
                target=InboxTarget.NEXT_STEP,
                input=RuntimeInput(
                    source="goal",
                    event="compaction",
                    content=_render_compaction_state(
                        goal,
                        round_number=min(
                            goal.progress.turns_evaluated + 1,
                            self._config.max_rounds,
                        ),
                        max_rounds=self._config.max_rounds,
                        reason=self._latest_reason,
                    ),
                ),
            ),
            wake=False,
        )

    def dispose(self) -> None:
        self._cancel_timer()
        self._cancel_evaluation()

    async def contribute_status(self, slots: StatusSlots) -> None:
        goal = (await self.snapshot()).state
        if isinstance(goal, NoGoal):
            return
        goal = await self._refresh_stats(goal)
        slots.add("goal", goal.kind)
        # Clients show the objective next to the state (the WebUI goal bar does),
        # and a slot is the only place they can read it without a command round
        # trip.  Kept short for a status bar.
        slots.add("goal_objective", _short(goal.condition, 120))
        if isinstance(goal, ActiveGoal):
            slots.add("goal_round", _current_round_label(goal, self._config))
        reason = self._latest_reason if isinstance(goal, ActiveGoal) else goal.reason
        if reason:
            slots.add(_REASON_SLOT_KEY, _short(reason, 60))
        if not isinstance(goal, ActiveGoal):
            if _has_stats(goal.stats):
                slots.add(_STATS_SLOT_KEY, _format_stats_short(goal.stats))

    # -- evaluation loop ---------------------------------------------------

    def _schedule_evaluation(self) -> None:
        if self._evaluation is not None and not self._evaluation.done():
            return
        self._evaluation = asyncio.create_task(self._evaluate())

    def _cancel_evaluation(self) -> None:
        task = self._evaluation
        self._evaluation = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_timer(self) -> None:
        task = self._timer
        self._timer = None
        if task is not None and not task.done():
            task.cancel()

    async def _evaluate(self) -> None:
        """Judge the condition against the finished turn's conversation."""
        try:
            goal = await self._active_goal()
            if goal is None:
                return
            transcript = render_transcript(list(self._engine.messages))
            request = evaluation_request(goal.condition, transcript)
            if self._usage is None or self._model_selection is None:
                raise RuntimeError("Goal evaluator requires usage and model selection")
            selection = self._model_selection()
            selection = selection.model_copy(update={
                "generation": selection.generation.model_copy(update={
                    "max_output_tokens": _EVALUATOR_OUTPUT_TOKENS,
                }),
            })
            model_request = ModelRequest(
                messages=request,
                tools=(),
                selection=selection,
            )
            response = await invoke_llm(self._model, model_request)
            await self._usage.record(
                RequestObservation(
                    selection=selection,
                    purpose=AuxiliaryRequest(
                        owner="goal",
                        operation_id=uuid4().hex,
                    ),
                    estimated_input_tokens=estimate_request_tokens(
                        model_request.messages,
                        model_request.tools,
                    ),
                    observed_context=response.observed_context,
                ),
                response.usage,
            )
            content = "".join(
                part.text for part in response.parts if isinstance(part, TextPart)
            )
            verdict = parse_verdict(content)
        except Exception as exc:  # noqa: BLE001 — any failure follows the retry policy
            await self._evaluation_failed(str(exc) or type(exc).__name__)
            return
        await self._apply_verdict(verdict)

    async def _evaluation_failed(self, reason: str) -> None:
        goal = await self._active_goal()
        if goal is None:
            return
        retries = goal.progress.retries + 1
        if retries > self._config.max_retries:
            await self._pause(
                goal,
                reason=(
                    "Paused after "
                    f"{goal.progress.retries} evaluation failures: {reason}"
                ),
            )
            return
        updated = goal.model_copy(update={
            "progress": goal.progress.model_copy(update={"retries": retries}),
        })
        self._latest_reason = f"Evaluation failed: {reason}"
        await self._write(updated)
        await self._emit(updated)
        self._schedule_retry(updated)

    async def _apply_verdict(self, verdict: GoalVerdict) -> None:
        goal = await self._active_goal()
        if goal is None:
            return
        turns = goal.progress.turns_evaluated + 1
        if isinstance(verdict, (Met, Impossible)):
            await self._terminate(
                goal,
                outcome="achieved" if isinstance(verdict, Met) else "failed",
                reason=verdict.reason,
                turns_evaluated=turns,
            )
            return
        if not isinstance(verdict, NotMet):
            raise TypeError(f"Unsupported goal verdict: {type(verdict).__name__}")
        goal = await self._refresh_stats(goal, flush_pending=True)
        if goal.progress.tool_less_turns >= self._config.stall_turns:
            updated = goal.model_copy(update={
                "progress": goal.progress.model_copy(update={
                    "turns_evaluated": turns,
                    "stalled": True,
                }),
            })
            self._latest_reason = verdict.reason
            await self._write(updated)
            await self._emit(updated)
            logger.warning(
                "goal.stalled tool_less_turns=%d",
                goal.progress.tool_less_turns,
            )
            return
        updated = goal.model_copy(update={
            "progress": goal.progress.model_copy(update={
                "turns_evaluated": turns,
                "retries": 0,
                "idle_checkins": 0,
                "stalled": False,
            }),
            "checkin_policy": GoalCheckinPolicy(),
        })
        self._latest_reason = verdict.reason
        await self._write(updated)
        await self._emit(updated)
        if turns >= self._config.max_rounds:
            await self._pause(
                updated,
                reason=(
                    f"Round cap reached ({turns}/{self._config.max_rounds}); "
                    "set the goal again to continue."
                ),
            )
            return
        await self._start_round(updated, reason=verdict.reason)

    async def _start_round(
        self,
        goal: ActiveGoal,
        *,
        reason: str = "",
        note: str = "",
    ) -> None:
        """Queue one admitted goal round as a persisted, attributed turn.

        Every round is self-contained: it names the objective, the round
        number and cap, and the evaluator guidance, so the round survives
        compaction without a separate per-request projection.
        """
        active = await self._active_goal()
        if (
            active is None
            or active.progress.stalled
            or self._continuation_pending
        ):
            return
        round_number = min(
            goal.progress.turns_evaluated + 1,
            self._config.max_rounds,
        )
        self._continuation_pending = True
        try:
            await self._engine.submit_input(
                InboxItem(
                    target=InboxTarget.NEXT_TURN,
                    input=RuntimeInput(
                        source="goal",
                        event="round",
                        content=_render_round(
                            goal,
                            round_number=round_number,
                            max_rounds=self._config.max_rounds,
                            reason=reason,
                            note=note,
                        ),
                    ),
                ),
                wake=True,
            )
        except BaseException:
            self._continuation_pending = False
            raise

    def _schedule_retry(self, goal: ActiveGoal) -> None:
        delay = self._config.retry_seconds * (
            2 ** max(0, goal.progress.retries - 1)
        )
        self._cancel_timer()
        self._timer = asyncio.create_task(self._retry_after(delay))

    async def _retry_after(self, delay: float) -> None:
        if not await _sleep(delay):
            return
        goal = await self._active_goal()
        if goal is None:
            return
        await self._start_round(goal, note="Retry after the previous error.")

    def _background_running(self) -> bool:
        if self._jobs is None:
            return False
        try:
            snapshots = self._jobs.views()
        except Exception:  # noqa: BLE001 — advisory; never block the loop
            logger.warning("goal.jobs_read_failed", exc_info=True)
            return False
        return any(
            snapshot.state in {"queued", "running"}
            for snapshot in snapshots
        )

    def _schedule_checkin(self, goal: ActiveGoal) -> None:
        self._cancel_timer()
        interval = (
            self._config.checkin_seconds
            * goal.checkin_policy.backoff_factor
        )
        self._timer = asyncio.create_task(self._checkin_after(interval))

    async def _checkin_after(self, delay: float) -> None:
        if not await _sleep(delay):
            return
        goal = await self._active_goal()
        if goal is None:
            return
        if not self._background_running():
            self._schedule_evaluation()
            return
        if goal.progress.idle_checkins >= self._config.max_idle_checkins:
            return
        factor = min(
            goal.checkin_policy.backoff_factor * 2,
            self._config.checkin_max_factor,
        )
        updated = goal.model_copy(update={
            "progress": goal.progress.model_copy(update={
                "idle_checkins": goal.progress.idle_checkins + 1,
            }),
            "checkin_policy": GoalCheckinPolicy(
                backoff_factor=factor
            ),
        })
        await self._write(updated)
        await self._emit(updated)
        await self._start_round(updated, note=_checkin_note())
        self._schedule_checkin(updated)

    async def _terminate(
        self,
        goal: ActiveGoal,
        *,
        outcome: Literal["achieved", "failed"],
        reason: str,
        turns_evaluated: int | None = None,
    ) -> None:
        self._cancel_timer()
        goal = await self._refresh_stats(goal, flush_pending=True)
        progress = goal.progress.model_copy(update=(
            {"turns_evaluated": turns_evaluated}
            if turns_evaluated is not None
            else {}
        ))
        terminal_type = AchievedGoal if outcome == "achieved" else FailedGoal
        updated = terminal_type(
            condition=goal.condition,
            started_at=goal.started_at,
            finished_at=self._now(),
            reason=reason,
            progress=progress,
            stats=goal.stats,
        )
        await self._write(updated)
        await self._emit(updated)

    async def _pause(self, goal: ActiveGoal, *, reason: str) -> None:
        self._cancel_timer()
        goal = await self._refresh_stats(goal, flush_pending=True)
        paused = PausedGoal(
            condition=goal.condition,
            started_at=goal.started_at,
            paused_at=self._now(),
            reason=reason,
            progress=goal.progress,
            stats=goal.stats,
            checkin_policy=goal.checkin_policy,
        )
        await self._write(paused)
        await self._emit(paused)

    # -- consumption -------------------------------------------------------

    def _usage_snapshot(self) -> UsageSnapshot:
        return UsageSnapshot() if self._usage is None else self._usage.snapshot()

    async def _write_usage_baseline(self) -> None:
        usage = self._usage_snapshot()
        counters = usage.total_counters
        await self._store.set("usage_baseline", {
            "input_tokens": counters.input,
            "output_tokens": counters.output,
        })

    async def _read_usage_baseline(self) -> dict[str, int]:
        stored = await self._store.get("usage_baseline")
        if not isinstance(stored, Mapping):
            return {"input_tokens": 0, "output_tokens": 0}
        return {
            "input_tokens": int(stored.get("input_tokens") or 0),
            "output_tokens": int(stored.get("output_tokens") or 0),
        }

    async def _refresh_stats(
        self,
        goal: GoalState,
        *,
        flush_pending: bool = False,
    ) -> GoalState:
        """Overlay provider usage and task progress on a Goal snapshot."""
        baseline = await self._read_usage_baseline()
        counters = self._usage_snapshot().total_counters
        stats = goal.stats.model_copy(update={
            "tool_calls": goal.stats.tool_calls + self._pending_tool_calls,
            "input_tokens": max(0, counters.input - baseline["input_tokens"]),
            "output_tokens": max(0, counters.output - baseline["output_tokens"]),
        })
        todolist = self._todolist_getter() if self._todolist_getter is not None else None
        if todolist is not None:
            try:
                task_list = await todolist.snapshot()
            except Exception:  # noqa: BLE001 — a Goal outlives a broken plan
                logger.warning(
                    "goal.task_projection_failed; stats omit task progress",
                    exc_info=True,
                )
                task_list = None
            if task_list is not None:
                tasks = list(task_list.tasks)
                stats = stats.model_copy(update={
                    "todo_items": len(tasks),
                    "todo_completed": sum(
                        task.status == "completed" for task in tasks
                    ),
                })
        if flush_pending:
            self._pending_tool_calls = 0
        return goal.model_copy(update={"stats": stats})

    async def _emit(self, goal: GoalState) -> None:
        if self._events is None:
            return
        event = GoalChanged(snapshot=GoalSnapshot(state=goal))
        try:
            await self._events.emit(
                RUNTIME_EVENT,
                RuntimeEvent(event=event),
            )
        except Exception:  # noqa: BLE001 — a UI notification is advisory
            logger.warning("goal.client_event_failed", exc_info=True)


async def _sleep(delay: float) -> bool:
    """Sleep, returning ``False`` when the wait was cancelled."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return False
    return True


def _is_unrecoverable(error: BaseException | None) -> bool:
    if error is None:
        return False
    text = f"{type(error).__name__} {error}".lower()
    return any(hint in text for hint in _UNRECOVERABLE_HINTS)


def _has_stats(stats: GoalStats) -> bool:
    return any((
        stats.tool_calls,
        stats.input_tokens,
        stats.output_tokens,
        stats.total_tokens,
        stats.todo_items,
        stats.todo_completed,
    ))


def _short(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"


def _format_stats_short(stats: GoalStats) -> str:
    parts = [
        f"{_compact_count(stats.total_tokens)}tok",
        f"{stats.tool_calls}tools",
    ]
    if stats.todo_items:
        parts.append(f"{stats.todo_completed}/{stats.todo_items}tasks")
    return " ".join(parts)


def _format_stats_long(stats: GoalStats) -> str:
    return (
        f"{stats.total_tokens} tokens, {stats.tool_calls} tool calls, "
        f"{stats.todo_completed}/{stats.todo_items} tasks done"
    )


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3_600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3_600}h{(total % 3_600) // 60:02d}m"


def _current_round_label(goal: ActiveGoal, config: GoalConfig) -> str:
    current = min(goal.progress.turns_evaluated + 1, config.max_rounds)
    return f"{current}/{config.max_rounds}"


def _format_status(
    goal: ActiveGoal | PausedGoal | AchievedGoal | FailedGoal,
    *,
    now: float,
    max_rounds: int,
    active_reason: str = "",
) -> str:
    lines = [f"[{goal.kind}] {goal.condition}"]
    if isinstance(goal, ActiveGoal):
        duration = max(0.0, now - goal.started_at)
        lines.append(
            f"Running: {_format_duration(duration)} \u00b7 "
            f"round {min(goal.progress.turns_evaluated + 1, max_rounds)}"
            f"/{max_rounds} \u00b7 "
            f"{_compact_count(goal.stats.total_tokens)} tokens"
        )
        reason = active_reason
    else:
        ended_at = (
            goal.paused_at
            if isinstance(goal, PausedGoal)
            else goal.finished_at
        )
        lines.append(
            f"Ran: {_format_duration(max(0.0, ended_at - goal.started_at))} \u00b7 "
            f"{goal.progress.turns_evaluated} rounds \u00b7 "
            f"{_compact_count(goal.stats.total_tokens)} tokens"
        )
        reason = goal.reason
    if reason:
        lines.append(f"Latest: {reason}")
    if goal.progress.stalled:
        lines.append(
            "The loop stopped: several turns made no tool calls. "
            "Send a message to resume."
        )
    if _has_stats(goal.stats):
        lines.append(f"Consumption: {_format_stats_long(goal.stats)}")
    return "\n".join(lines)


def _render_round(
    goal: ActiveGoal,
    *,
    round_number: int,
    max_rounds: int,
    reason: str,
    note: str,
) -> str:
    lines = [
        f"Objective: {json.dumps(goal.condition, ensure_ascii=False)}",
        f"Round: {round_number}/{max_rounds}",
        "",
        "Continue working toward the objective in this same session. Treat the "
        "current workspace, tool results, and durable session state as "
        "authoritative; inspect them instead of assuming earlier narration is "
        "still current. Make concrete progress and verify the result. If work "
        "remains, leave the goal active for the next round.",
    ]
    if reason:
        lines.append("")
        lines.append(f"Evaluator: {reason}")
    if note:
        lines.append("")
        lines.append(f"Note: {note}")
    return (
        '<system_reminder source="goal" event="round" '
        f'round="{round_number}" max="{max_rounds}">\n'
        + "\n".join(lines)
        + "\n</system_reminder>"
    )


def _render_compaction_state(
    goal: ActiveGoal,
    *,
    round_number: int,
    max_rounds: int,
    reason: str,
) -> str:
    lines = [
        f"Active goal: {json.dumps(goal.condition, ensure_ascii=False)}",
        f"Round: {round_number}/{max_rounds}",
    ]
    if reason:
        lines.append(f"Latest evaluator verdict: {reason}")
    return (
        '<system_reminder source="goal" event="compaction" '
        f'round="{round_number}" max="{max_rounds}">\n'
        + "\n".join(lines)
        + "\n</system_reminder>"
    )


def _checkin_note() -> str:
    return (
        "Check-in: background work has been running for a while. Read the "
        "running jobs' output, keep waiting if they are progressing, and fix "
        "or stop any that are stuck."
    )


def _command_result(
    result: ToolOutcome,
    *,
    effects: tuple[CommandEffect, ...] = (),
) -> CommandResult:
    return CommandResult(
        message=_tool_outcome_text(result),
        status="ok" if isinstance(result, ToolSucceeded) else "error",
        effects=effects,
    )


def _tool_outcome_text(result: ToolOutcome) -> str:
    if isinstance(result, (ToolSucceeded, ToolFailed)):
        return "".join(
            part.text for part in result.output.parts if isinstance(part, TextPart)
        ) or (result.error.message if isinstance(result, ToolFailed) else "")
    return result.reason


class GoalPlugin:
    """Register the evaluator-driven Goal service for each mounted session."""

    inject = {
        "required": ["commands", "engine", "loop_state", "model", "state", "tools", "usage"],
        "optional": ["jobs", "todolist"],
    }
    name = "goal"
    Config = GoalConfig

    def apply(
        self, ctx: Context, config: GoalConfig | None = None
    ) -> None:
        service = GoalService(
            ctx.state.namespace(self.name),
            ctx.engine,
            model=ctx.model,
            events=ctx,
            jobs=ctx.get("jobs", strict=False),
            usage=ctx.usage,
            model_selection=lambda: ctx.loop_state.metadata.value.runtime_selection.model,
            todolist_getter=lambda: ctx.get("todolist", strict=False),
            config=config,
        )
        ctx.set("goal", service)
        ctx.dispose(service.dispose)
        ctx.on(Events.TURN_START, service.on_turn_start)
        ctx.on(Events.AFTER_TOOL_CALL, service.on_tool_call)
        ctx.on(Events.TURN_END, service.on_turn_end)
        ctx.on(Events.ON_ERROR, service.on_error)
        ctx.on(Events.SESSION_RESUME, service.on_session_resume)
        ctx.on(Events.SESSION_CLOSE, service.on_session_close)
        ctx.on(COLLECT_STATUS_SLOTS, service.contribute_status)
        ctx.on(HISTORY_CHANGED, service.on_compaction)
        # The goal is set and judged, never self-certified: the Agent may
        # create a goal and read it, but no Agent tool can end one.
        for function in (service.create_goal, service.get_goal):
            ctx.tools.register(replace(Tool.from_function(function), kind="think"))
        ctx.commands.register(Command(
            name="goal",
            description="Set a completion condition and keep working toward it.",
            handler=service.command,
            effects=("thread",),
            usage="/goal | /goal <condition> | /goal clear",
            examples=(
                "/goal all tests in test/auth pass and the lint step is clean",
                "/goal",
                "/goal clear",
            ),
            exclusive=False,
        ))

    def diagnostics(self) -> dict[str, JsonValue]:
        return {
            "status": "ready",
            "scope": "session",
            "goal_statuses": sorted({
                "none", "active", "achieved", "failed", "paused",
            }),
            "evaluator": "auxiliary_model_call",
            "commands": ["/goal", "/goal <condition>", "/goal clear"],
        }


plugin = GoalPlugin()

__all__ = ["GoalPlugin", "GoalService"]
