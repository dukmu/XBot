"""Thread-scoped task list and the four fine-grained task tools."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from XBotv2.agentloop.api.contracts import TOOLS, AgentLoopPort, InboxItem, RuntimeInput
from XBotv2.agentloop.api.state import LOOP_STATE
import copy
import asyncio
from functools import wraps
from collections.abc import Mapping
from dataclasses import replace
from xml.sax.saxutils import escape
from xcore.api.context import Context
from XBotv2.persistence.api.contracts import StatePort
from XBotv2.session.api.storage import THREAD_REPOSITORY
from XBotv2.todolist.impl.storage import TaskStorage
from XBotv2.agentloop.api.events import Events, TurnStarted
from XBotv2.agentloop.api.events import RUNTIME_EVENT
from XBotv2.agentloop.api.events import RuntimeEvent
from XBotv2.core.api.tools import Tool, ToolOutcome, failed_text, succeeded_text
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.server.api.contracts import contribute_router
from XBotv2.session.api.contracts import HISTORY_CHANGED, HistoryChanged
from XBotv2.todolist.api.models import (
    DependencyEdge,
    Task,
    TaskConfig,
    TaskList,
    TaskStatusInput,
    TaskValidationError,
)
from XBotv2.todolist.api.events import TaskChanged
from XBotv2.todolist.api.service import (GET_TODOS, GET_COMPLETED_TODOS, CompletedTasksRequest, CompletedTasksPage)
from XBotv2.todolist.impl.parsing import (
    parse_task_id,
    parse_task_ids,
    parse_task_status,
    parse_task_text,
)
from XBotv2.todolist.impl.http import build_router

_STALE_TURNS_KEY = "stale_turns"
_COMPACT_OPERATION = "compact:"
#: Nudge injected as a persisted turn when the list has not moved for a while.
_STALE_NAG = (
    "If the task list still applies to the current work, update it to reflect "
    "actual progress. Do not create tasks solely to acknowledge this reminder."
)
_VERIFICATION_NUDGE = (
    "The task list is complete. Confirm the requested outcome against the "
    "available evidence; reopen any unfinished task."
)


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.todolist.http",
        router=build_router(sessions=ctx.sessions),
    )


def _serialized(method):
    @wraps(method)
    async def run(self, *args, **kwargs):
        async with self._mutation_lock:
            return await method(self, *args, **kwargs)

    return run


class TaskService:
    """Own the typed task list for one thread."""

    def __init__(
        self,
        store: StatePort,
        *,
        agent_name: str,
        thread_id: str,
        thread_repository,
        engine: AgentLoopPort,
        events: Context,
        config: TaskConfig,
    ) -> None:
        self._store = store
        self._storage = TaskStorage(store, thread_id, thread_repository)
        self._agent_name = agent_name
        self._engine = engine
        self._events = events
        self._config = config
        self._mutation_lock = asyncio.Lock()

    async def snapshot(self) -> TaskList:
        return await self._storage.load()

    @_serialized
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
                subject,
                field="subject",
                limit=self._config.max_subject_chars,
                required=True,
            )
            parsed_description = parse_task_text(
                description,
                field="description",
                limit=self._config.max_description_chars,
                required=False,
            )
            parsed_active_form = parse_task_text(
                active_form,
                field="active_form",
                limit=self._config.max_active_form_chars,
                required=False,
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
        try:
            await self._storage.change(task=task, max_tasks=self._config.max_tasks)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))
        await self._changed()
        return await self._result(
            f"Created task #{task.id}: {task.subject}",
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
        found = await self._storage.get(parsed_id)
        if found is None:
            return _unknown_task(parsed_id)
        task, edges = found
        return succeeded_text(_format_task_detail(task, edges))

    @_serialized
    async def task_update(
        self,
        taskId: str,
        status: TaskStatusInput | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        add_blocks: list[str | int] | None = None,
        add_blocked_by: list[str | int] | None = None,
        remove_blocks: list[str | int] | None = None,
        remove_blocked_by: list[str | int] | None = None,
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
            add_blocks: Ids of tasks this task must be completed before.
                Each id may be a string (e.g. ``"2"``) or an integer; values
                are normalized to strings. Do not prefix ids with ``"#"``.
            add_blocked_by: Ids of tasks that must be completed before this
                one. Same string-or-integer format as ``add_blocks``.
            remove_blocks: Ids from which to remove this task as a
                prerequisite.
            remove_blocked_by: Ids of prerequisites to remove.
        """
        try:
            parsed_id = parse_task_id(taskId)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))

        current = await self.snapshot()
        found = await self._storage.get(parsed_id)
        if found is None:
            return _unknown_task(parsed_id)
        task, existing_edges = found

        try:
            changes: dict[str, object] = {}
            new_status = None
            if status is not None:
                new_status = parse_task_status(status)
            if subject is not None:
                changes["subject"] = parse_task_text(
                    subject,
                    field="subject",
                    limit=self._config.max_subject_chars,
                    required=True,
                )
            if description is not None:
                changes["description"] = parse_task_text(
                    description,
                    field="description",
                    limit=self._config.max_description_chars,
                    required=False,
                )
            if active_form is not None:
                changes["active_form"] = parse_task_text(
                    active_form,
                    field="active_form",
                    limit=self._config.max_active_form_chars,
                    required=False,
                )
            if owner is not None:
                changes["owner"] = parse_task_text(
                    owner,
                    field="owner",
                    limit=self._config.max_subject_chars,
                    required=False,
                )
            blocks = parse_task_ids(add_blocks, field="add_blocks")
            blocked_by = parse_task_ids(
                add_blocked_by,
                field="add_blocked_by",
            )
            unblocks = parse_task_ids(remove_blocks, field="remove_blocks")
            unblocked_by = parse_task_ids(
                remove_blocked_by,
                field="remove_blocked_by",
            )
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))

        if new_status is not None:
            if new_status == "in_progress":
                if not changes.get("owner") and not task.owner and self._agent_name:
                    changes["owner"] = self._agent_name
            changes["status"] = new_status

        if not (changes or blocks or blocked_by or unblocks or unblocked_by):
            return failed_text(
                "invalid_task_update",
                "Provide at least one field to update",
            )

        additions = (
            *(
                DependencyEdge(prerequisite=task.id, dependent=value)
                for value in blocks
            ),
            *(
                DependencyEdge(prerequisite=value, dependent=task.id)
                for value in blocked_by
            ),
        )
        removals = (
            *(
                DependencyEdge(prerequisite=task.id, dependent=value)
                for value in unblocks
            ),
            *(
                DependencyEdge(prerequisite=value, dependent=task.id)
                for value in unblocked_by
            ),
        )
        if set(additions).intersection(removals):
            return failed_text(
                "invalid_task_update",
                "Cannot add and remove the same dependency",
            )
        for value in (*blocks, *blocked_by, *unblocks, *unblocked_by):
            if await self._storage.get(value) is None:
                return _unknown_task(value)
        updated_task = task.model_copy(update=changes)
        if (
            updated_task == task
            and all(edge in existing_edges for edge in additions)
            and all(edge not in existing_edges for edge in removals)
        ):
            return succeeded_text(f"Task #{task.id} is unchanged.")
        try:
            await self._storage.change(
                task=updated_task,
                additions=additions,
                removals=removals,
                max_tasks=self._config.max_tasks,
            )
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))
        updated = await self._changed()
        content = f"Updated task #{task.id}: {_describe_update(updated_task)}"
        if (
            self._config.verification_nudge
            and _has_unfinished(current)
            and not _has_unfinished(updated)
        ):
            content = f"{content}\n{_VERIFICATION_NUDGE}"
        return await self._result(content)

    @_serialized
    async def task_delete(self, task_id: str) -> ToolOutcome:
        """Delete one task and all dependency edges connected to it."""
        try:
            parsed_id = parse_task_id(task_id)
        except TaskValidationError as exc:
            return failed_text(exc.code, str(exc))
        if await self._storage.get(parsed_id) is None:
            return _unknown_task(parsed_id)
        await self._storage.change(delete=parsed_id, max_tasks=self._config.max_tasks)
        await self._changed()
        return await self._result(
            f"Deleted task #{parsed_id}.",
        )

    async def task_list(
        self, after: int = 0, limit: int = 100, completed: bool = False
    ) -> ToolOutcome:
        """List active tasks, or a page of completed tasks by numeric id.

        Args:
            after: Return completed tasks after this numeric id.
            limit: Maximum completed tasks to return (1 to 400).
            completed: Read completed history instead of active tasks.

        Call this when the current plan is unknown, or before choosing the next
        task after completing one.
        """
        if completed:
            try:
                page = await self._storage.completed_page(after, limit)
            except ValueError as exc:
                return failed_text("invalid_task_page", str(exc))
            if not page:
                return succeeded_text("No completed tasks in this page.")
            lines = [f"Completed tasks (continue after {page[-1].id}):"]
            lines.extend(f"- #{task.id} [x] {task.subject}" for task in page)
            return succeeded_text("\n".join(lines))
        current = await self.snapshot()
        if not current.tasks:
            return succeeded_text(
                f"No active tasks. {current.completed_count} completed."
            )
        return succeeded_text(_format_task_list(current))

    async def completed_page(
        self, request: CompletedTasksRequest
    ) -> CompletedTasksPage:
        tasks = await self._storage.completed_page(request.after, request.limit)
        return CompletedTasksPage(
            tasks=tasks,
            next_after=int(tasks[-1].id) if len(tasks) == request.limit else None,
        )

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

    async def _changed(self) -> TaskList:
        tasks = await self.snapshot()
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=TaskChanged(snapshot=tasks)),
        )
        return tasks

    async def _stale_turns(self) -> int:
        stored = await self._store.get(_STALE_TURNS_KEY)
        return stored if isinstance(stored, int) and stored > 0 else 0

    async def _result(self, content: str) -> ToolOutcome:
        # Any mutation of the list resets the "tools not used recently" nudge.
        await self._store.set(_STALE_TURNS_KEY, 0)
        return succeeded_text(content)


def _blocking_tasks(tasks: TaskList, task: Task) -> list[str]:
    """Prerequisite ids that are not completed."""
    return [
        value
        for value in tasks.prerequisites(task.id)
        if value not in tasks.completed_prerequisites and ((blocker := tasks.find(value)) is None or blocker.status != "completed")
    ]


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
    completed = tasks.completed_count
    lines = [f"Tasks ({completed}/{completed + len(tasks.tasks)} completed):"]
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
        line += " \u00b7 blocks " + ", ".join(f"#{value}" for value in dependents)
    return line


def _format_task_detail(task: Task, edges: tuple[DependencyEdge, ...]) -> str:
    lines = [
        f"#{task.id} [{task.status}] {task.subject}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.active_form:
        lines.append(f"Active form: {task.active_form}")
    if task.owner:
        lines.append(f"Owner: {task.owner}")
    dependents = tuple(edge.dependent for edge in edges if edge.prerequisite == task.id)
    prerequisites = tuple(edge.prerequisite for edge in edges if edge.dependent == task.id)
    if dependents:
        lines.append("Blocks: " + ", ".join(f"#{value}" for value in dependents))
    if prerequisites:
        lines.append("Blocked by: " + ", ".join(f"#{value}" for value in prerequisites))
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


def _render_compaction_reminder(tasks: TaskList, unfinished: list[Task]) -> str:
    lines = ["Unfinished tasks (owned by task_update):"]
    lines.extend(_format_task_line(task, tasks, xml=True) for task in unfinished)
    return (
        '<system_reminder source="todo" event="compaction">\n'
        + "\n".join(lines)
        + "\n</system_reminder>"
    )


class TodolistRuntimeComponent:
    inject = {"required": [TOOLS, "state", "agent_loop", LOOP_STATE, THREAD_REPOSITORY]}
    name = "todolist"
    Config = TaskConfig

    def apply(self, ctx: Context, config: TaskConfig | None = None) -> None:
        engine: AgentLoopPort = ctx.require(AGENT_LOOP)
        resolved = config or TaskConfig()
        service = TaskService(
            ctx.state.namespace(self.name),
            agent_name=ctx.require(
                LOOP_STATE
            ).metadata.value.runtime_selection.agent_name,
            thread_id=ctx.require(LOOP_STATE).session.thread_id,
            thread_repository=ctx.require(THREAD_REPOSITORY),
            engine=engine,
            events=ctx,
            config=resolved,
        )
        ctx.set("todolist", service)
        ctx.on(GET_TODOS.name, service.get_snapshot)
        ctx.on(GET_COMPLETED_TODOS.name, service.completed_page)
        ctx.on(Events.TURN_START, service.on_turn_start)
        ctx.on(HISTORY_CHANGED, service.on_compaction)
        for function in (
            service.task_create,
            service.task_get,
            service.task_update,
            service.task_delete,
            service.task_list,
        ):
            ctx.require(TOOLS).register(_task_tool(function, resolved))


async def mount_thread(ctx, config):
    await ctx.plugin(TodolistRuntimeComponent(), config)


mount_thread.inject = ()
mount_thread.provide = ("todolist",)
mount_thread.Config = TaskConfig


def mount_process(ctx, config):
    from XBotv2.session.api.storage import FORK_COPY_STATE

    ctx.on(FORK_COPY_STATE, lambda event: event.handled.add("todolist"))
    ctx.inject(["server", "sessions"], mount_http)


mount_process.inject = ()
mount_process.provide = ()
