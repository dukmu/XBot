"""Thread-scoped task list and the four fine-grained task tools."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import replace
from xml.sax.saxutils import escape

from xcore import Context
from xcore.state import StateService

from XBotv2.agentloop import (
    AgentLoopDriverPort,
    Events,
    InboxItem,
    InboxTarget,
    RuntimeInput,
)
from XBotv2.agentloop.events import TurnStarted
from XBotv2.application import (
    ApplicationEventsPort,
    RUNTIME_EVENT,
    RuntimeEvent,
)
from XBotv2.core import Tool, ToolOutcome, failed_text, succeeded_text
from XBotv2.core.operations import EmptyRequest
from XBotv2.server import contribute_router
from XBotv2.session import HISTORY_CHANGED, HistoryChanged
from XBotv2.todolist.contracts import (
    GET_TODOS,
    DependencyEdge,
    Task,
    TaskChanged,
    TaskConfig,
    TaskList,
    TaskStatusInput,
    TaskValidationError,
    parse_task_id,
    parse_task_ids,
    parse_task_status,
    parse_task_text,
)
from XBotv2.todolist.protocol import build_router

_STALE_TURNS_KEY = "stale_turns"
_COMPACT_OPERATION = "compact:"
#: Nudge injected as a persisted turn when the list has not moved for a while.
_STALE_NAG = (
    "The task tools haven't been used recently. If you're working on tasks that "
    "would benefit from tracking progress, consider using task_create to add new "
    "tasks and task_update to update task status (set to in_progress when "
    "starting, completed when done). Also consider cleaning up the task list if "
    "it has become stale. Only use these if relevant to the current work. This "
    "is just a gentle reminder - ignore if not applicable. Make sure that you "
    "NEVER mention this reminder to the user"
)
_VERIFICATION_NUDGE = (
    "NOTE: you closed out the whole task list and none of the tasks was a "
    "verification step. Spawn a subagent to verify the work independently "
    "before reporting it done."
)


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.todolist.http",
        router=build_router(sessions=ctx.sessions),
    )


class TaskService:
    """Own the typed task list for one thread."""

    def __init__(
        self,
        store: StateService,
        *,
        agent_name: str,
        engine: AgentLoopDriverPort,
        events: ApplicationEventsPort,
        config: TaskConfig,
    ) -> None:
        self._store = store
        self._agent_name = agent_name
        self._engine = engine
        self._events = events
        self._config = config

    async def snapshot(self) -> TaskList:
        stored = await self._store.get("snapshot")
        if stored is None:
            return TaskList()
        if not isinstance(stored, Mapping):
            raise TypeError("Persisted task list must be an object")
        return TaskList.model_validate(stored)

    async def task_create(
        self,
        subject: str,
        description: str = "",
        active_form: str = "",
    ) -> ToolOutcome:
        """Create one task and return its id.

        Use the task tools for multi-step work that needs a visible plan, and
        for a list the human gave you. Do not use them for a single
        straightforward action, and never add a task for the final reply.

        Args:
            subject: Imperative one-line title, for example "Fix the auth bug".
            description: What the task must accomplish and how it is verified.
            active_form: Present-continuous label shown while it runs, for
                example "Fixing the auth bug".
        """
        try:
            parsed_subject = parse_task_text(
                subject, field="subject", limit=self._config.max_subject_chars,
                required=True,
            )
            parsed_description = parse_task_text(
                description, field="description",
                limit=self._config.max_description_chars, required=False,
            )
            parsed_active_form = parse_task_text(
                active_form, field="active_form",
                limit=self._config.max_active_form_chars, required=False,
            )
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))

        current = await self.snapshot()
        if len(current.tasks) >= self._config.max_tasks:
            return failed_text(
                "task_limit",
                f"A task list must not exceed {self._config.max_tasks} tasks",
            )
        task = Task(
            id=current.allocate_id(),
            subject=parsed_subject,
            description=parsed_description,
            active_form=parsed_active_form,
        )
        updated = current.append(task)
        await self._write(updated)
        return await self._result(
            f"Created task #{task.id}: {task.subject}",
            updated,
            changes={task.id},
        )

    async def task_get(self, taskId: str) -> ToolOutcome:
        """Read one task's full details by id.

        Args:
            taskId: Id returned by ``task_create`` or listed by ``task_list``.
        """
        try:
            parsed_id = parse_task_id(taskId)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))
        current = await self.snapshot()
        task = current.find(parsed_id)
        if task is None:
            return _unknown_task(parsed_id)
        return succeeded_text(_format_task_detail(task, current))

    async def task_update(
        self,
        taskId: str,
        status: TaskStatusInput | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> ToolOutcome:
        """Update one task: status, text, owner, or dependency edges.

        ``status`` accepts pending, in_progress, or completed. Set a
        task in_progress when you start it and completed only from observed
        results — never because it was started, intended, or summarized. A task
        whose prerequisites are not all completed cannot start.

        Args:
            taskId: Id of the task to change.
            status: New status.
            subject: Replacement imperative title.
            description: Replacement description.
            active_form: Replacement present-continuous label.
            owner: Agent that claimed the task.
            add_blocks: Task ids this task must be completed before.
            add_blocked_by: Task ids that must be completed before this one.
        """
        try:
            parsed_id = parse_task_id(taskId)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))

        current = await self.snapshot()
        task = current.find(parsed_id)
        if task is None:
            return _unknown_task(parsed_id)

        try:
            changes: dict[str, object] = {}
            new_status = None
            if status is not None:
                new_status = parse_task_status(status)
            if subject is not None:
                changes["subject"] = parse_task_text(
                    subject, field="subject", limit=self._config.max_subject_chars,
                    required=True,
                )
            if description is not None:
                changes["description"] = parse_task_text(
                    description, field="description",
                    limit=self._config.max_description_chars, required=False,
                )
            if active_form is not None:
                changes["active_form"] = parse_task_text(
                    active_form, field="active_form",
                    limit=self._config.max_active_form_chars, required=False,
                )
            if owner is not None:
                changes["owner"] = parse_task_text(
                    owner, field="owner", limit=self._config.max_subject_chars,
                    required=False,
                )
            blocks = parse_task_ids(add_blocks, field="add_blocks")
            blocked_by = parse_task_ids(
                add_blocked_by,
                field="add_blocked_by",
            )
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))

        if new_status is not None:
            if new_status == "in_progress":
                unfinished = _blocking_tasks(current, task)
                if unfinished:
                    return failed_text(
                        "blocked",
                        "Task #" f"{task.id} is blocked by "
                        + ", ".join(f"#{value}" for value in unfinished),
                    )
                if not changes.get("owner") and not task.owner and self._agent_name:
                    changes["owner"] = self._agent_name
            changes["status"] = new_status

        if not changes and not blocks and not blocked_by:
            return failed_text(
                "invalid_task_update",
                "Provide at least one field to update",
            )

        updated_task = task.model_copy(update=changes)
        updated = current.replace(updated_task)
        for target_id in blocks:
            error, updated = _link(updated, from_id=task.id, to_id=target_id)
            if error is not None:
                return error
        for target_id in blocked_by:
            error, updated = _link(updated, from_id=target_id, to_id=task.id)
            if error is not None:
                return error

        await self._write(updated)
        content = f"Updated task #{task.id}: {_describe_update(updated.find(task.id))}"
        nudge = _verification_nudge(updated, self._config)
        if nudge:
            content = f"{content}\n{nudge}"
        return await self._result(content, updated, changes={task.id, *blocks, *blocked_by})

    async def task_delete(self, task_id: str) -> ToolOutcome:
        """Delete one task and all dependency edges connected to it."""
        try:
            parsed_id = parse_task_id(task_id)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))
        current = await self.snapshot()
        if current.find(parsed_id) is None:
            return _unknown_task(parsed_id)
        updated = current.remove(parsed_id)
        await self._write(updated)
        return await self._result(
            f"Deleted task #{parsed_id}.",
            updated,
            changes={parsed_id},
        )

    async def task_list(self) -> ToolOutcome:
        """List every task with id, status, owner, and dependencies.

        Call this when the current plan is unknown, or before choosing the next
        task after completing one.
        """
        current = await self.snapshot()
        if not current.tasks:
            return succeeded_text("The task list is empty.")
        return succeeded_text(_format_task_list(current))

    async def get_snapshot(self, _request: EmptyRequest) -> TaskList:
        return await self.snapshot()

    async def on_turn_start(self, _event: TurnStarted) -> None:
        """Count turns since the last task change and nudge once past the bound.

        The nudge is a persisted turn-reminder, not a per-request projection:
        the task list itself already travels in the tool calls, so the reminder
        only says that the tools have gone unused. The counter resets after
        each nudge, so the reminder recurs every ``reminder_after_turns`` turns
        instead of on every request.
        """
        current = await self.snapshot()
        if not _has_unfinished(current):
            return
        turns = await self._stale_turns() + 1
        if turns >= self._config.reminder_after_turns:
            await self._inject(_render_stale_reminder(), event="reminder")
            turns = 0
        await self._store.set(_STALE_TURNS_KEY, turns)

    async def on_compaction(self, event: HistoryChanged) -> None:
        """Re-state the unfinished tasks once, after history is summarized.

        The summary replaces the tool calls that carried the list, so this is
        the one place the outstanding work is repeated. It is injected after
        the compacted history is committed and nothing else re-states it.
        """
        if not event.operation.startswith(_COMPACT_OPERATION):
            return
        current = await self.snapshot()
        unfinished = [task for task in current.tasks if task.status != "completed"]
        if not unfinished:
            return
        await self._inject(
            _render_compaction_reminder(current, unfinished),
            event="compaction",
        )

    async def _inject(self, content: str, *, event: str) -> None:
        """Deliver one persisted, attributed reminder without waking a turn."""
        await self._engine.submit_input(
            InboxItem(
                target=InboxTarget.NEXT_STEP,
                input=RuntimeInput(
                    source="todo",
                    event=event,
                    content=content,
                ),
            ),
            wake=False,
        )

    async def _write(self, tasks: TaskList) -> None:
        await self._store.set("snapshot", tasks.model_dump(mode="json"))
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=TaskChanged(snapshot=tasks)),
        )


    async def _stale_turns(self) -> int:
        stored = await self._store.get(_STALE_TURNS_KEY)
        return stored if isinstance(stored, int) and stored > 0 else 0

    async def _result(
        self,
        content: str,
        tasks: TaskList,
        *,
        changes: set[str] | None = None,
    ) -> ToolOutcome:
        del changes
        # Any mutation of the list resets the "tools not used recently" nudge.
        await self._store.set(_STALE_TURNS_KEY, 0)
        return succeeded_text(content)


def _blocking_tasks(tasks: TaskList, task: Task) -> list[str]:
    """Prerequisite ids that are not completed."""
    return [
        value
        for value in tasks.prerequisites(task.id)
        if (blocker := tasks.find(value)) is None or blocker.status != "completed"
    ]


def _link(
    tasks: TaskList,
    *,
    from_id: str,
    to_id: str,
) -> tuple[ToolOutcome | None, TaskList]:
    """Add one canonical prerequisite-to-dependent edge."""
    source = tasks.find(from_id)
    target = tasks.find(to_id)
    if source is None or target is None:
        missing = from_id if source is None else to_id
        return (_unknown_task(missing), tasks)
    try:
        return None, tasks.add_edge(
            DependencyEdge(prerequisite=from_id, dependent=to_id)
        )
    except ValueError as exc:
        return failed_text("invalid_task_dependency", str(exc)), tasks


def _verification_nudge(tasks: TaskList, config: TaskConfig) -> str:
    if not config.verification_nudge or not config.verification_hint:
        return ""
    if len(tasks.tasks) < 3:
        return ""
    if any(task.status != "completed" for task in tasks.tasks):
        return ""
    if any(
        re.search(config.verification_hint, task.subject, re.IGNORECASE)
        for task in tasks.tasks
    ):
        return ""
    return _VERIFICATION_NUDGE


def _unknown_task(task_id: str) -> ToolOutcome:
    return failed_text("task_not_found", f"Unknown task: #{task_id}")


def _describe_update(task: Task | None) -> str:
    if task is None:
        return "removed"
    detail = task.status
    if task.owner:
        detail += f", owner {task.owner}"
    return detail


def _format_task_list(tasks: TaskList) -> str:
    completed = sum(task.status == "completed" for task in tasks.tasks)
    lines = [f"Tasks ({completed}/{len(tasks.tasks)} completed):"]
    lines.extend(_format_task_line(task, tasks) for task in tasks.tasks)
    return "\n".join(lines)


def _format_task_line(task: Task, tasks: TaskList, *, xml: bool = False) -> str:
    """Render one list row; ``xml`` escapes the model-authored fields."""
    def text(value: str) -> str:
        return escape(value) if xml else value

    marker = {
        "pending": " ",
        "in_progress": ">",
        "completed": "x",
    }[task.status]
    line = f"- #{task.id} [{marker}] {text(task.subject)}"
    if task.status == "in_progress":
        line += f" ({text(task.display_label())})"
    if task.owner:
        line += f" \u00b7 owner: {text(task.owner)}"
    blocking = _blocking_tasks(tasks, task)
    if blocking:
        line += " \u00b7 blocked by " + ", ".join(f"#{value}" for value in blocking)
    dependents = tasks.dependents(task.id)
    if dependents:
        line += " \u00b7 blocks " + ", ".join(
            f"#{value}" for value in dependents
        )
    return line


def _format_task_detail(task: Task, tasks: TaskList) -> str:
    lines = [
        f"#{task.id} [{task.status}] {task.subject}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.active_form:
        lines.append(f"Active form: {task.active_form}")
    if task.owner:
        lines.append(f"Owner: {task.owner}")
    dependents = tasks.dependents(task.id)
    prerequisites = tasks.prerequisites(task.id)
    if dependents:
        lines.append("Blocks: " + ", ".join(f"#{value}" for value in dependents))
    if prerequisites:
        lines.append(
            "Blocked by: " + ", ".join(f"#{value}" for value in prerequisites)
        )
    return "\n".join(lines)


def _task_tool(function, config: TaskConfig) -> Tool:
    """Register one task tool with the configured bounds advertised.

    The model must be able to see the limits it is validated against, so the
    derived schema carries ``maxLength`` per text field instead of the bounds
    living only in the validator.
    """
    tool = replace(Tool.from_function(function), kind="think")
    schema = copy.deepcopy(tool.parameters)
    properties = schema.get("properties", {})
    bounds = {
        "subject": config.max_subject_chars,
        "description": config.max_description_chars,
        "active_form": config.max_active_form_chars,
        "owner": config.max_subject_chars,
    }
    for name, limit in bounds.items():
        _bound_text(properties.get(name), limit)
    return replace(tool, parameters=schema)


def _bound_text(schema: object, limit: int) -> None:
    """Add ``maxLength`` to a string schema, including an ``anyOf`` branch."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "string":
        schema["maxLength"] = limit
        return
    for branch in schema.get("anyOf", []):
        if isinstance(branch, dict) and branch.get("type") == "string":
            branch["maxLength"] = limit


def _has_unfinished(tasks: TaskList) -> bool:
    return any(task.status != "completed" for task in tasks.tasks)


def _render_stale_reminder() -> str:
    return (
        '<system_reminder source="todo" event="reminder">\n'
        + _STALE_NAG
        + "\n</system_reminder>"
    )


def _render_compaction_reminder(
    tasks: TaskList, unfinished: list[Task]
) -> str:
    lines = ["Unfinished tasks (owned by task_update):"]
    lines.extend(
        _format_task_line(task, tasks, xml=True) for task in unfinished
    )
    return (
        '<system_reminder source="todo" event="compaction">\n'
        + "\n".join(lines)
        + "\n</system_reminder>"
    )


class TodolistRuntimeComponent:
    inject = {"required": ["tools", "state", "engine", "loop_state"]}
    name = "todolist"
    Config = TaskConfig

    def apply(
        self, ctx: Context, config: TaskConfig | None = None
    ) -> None:
        engine: AgentLoopDriverPort = ctx.engine
        resolved = config or TaskConfig()
        service = TaskService(
            ctx.state.namespace(self.name),
            agent_name=ctx.loop_state.metadata.value.runtime_selection.agent_name,
            engine=engine,
            events=ctx,
            config=resolved,
        )
        ctx.set("todolist", service)
        ctx.on(GET_TODOS.name, service.get_snapshot)
        ctx.on(Events.TURN_START, service.on_turn_start)
        ctx.on(HISTORY_CHANGED, service.on_compaction)
        for function in (
            service.task_create,
            service.task_get,
            service.task_update,
            service.task_delete,
            service.task_list,
        ):
            ctx.tools.register(_task_tool(function, resolved))


class TodolistPlugin:
    """Compose the Agent task service and its HTTP projection."""

    name = "xbot.todolist"
    Config = TaskConfig

    async def apply(
        self, ctx: Context, config: TaskConfig | None = None
    ) -> None:
        await ctx.plugin(TodolistRuntimeComponent(), config)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = TodolistPlugin()

__all__ = ["TaskService", "TodolistPlugin"]
