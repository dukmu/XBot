"""Task-list state owns identity, dependency, and transition invariants."""

import pytest

from XBotv2.core.messages import ToolMessage
from XBotv2.todolist.contracts import (
    DependencyEdge,
    Task,
    TaskList,
    TaskValidationError,
    parse_task_id,
    parse_task_ids,
    parse_task_status,
    parse_task_text,
)


def test_task_list_allocates_stable_numeric_identity():
    tasks = TaskList.from_items((
        Task(id="2", subject="second"),
        Task(id="7", subject="seventh"),
    ))
    assert tasks.allocate_id() == "8"
    assert tasks.find("2") == Task(id="2", subject="second")


def test_task_list_rejects_duplicate_unknown_and_cyclic_dependencies():
    with pytest.raises(ValueError, match="unique"):
        TaskList(tasks=(Task(id="1", subject="a"), Task(id="1", subject="b")))
    with pytest.raises(ValueError, match="existing"):
        TaskList(
            tasks=(Task(id="1", subject="a"),),
            edges=(DependencyEdge(prerequisite="1", dependent="2"),),
        )
    with pytest.raises(ValueError, match="acyclic"):
        TaskList(
            tasks=(Task(id="1", subject="a"), Task(id="2", subject="b")),
            edges=(
                DependencyEdge(prerequisite="1", dependent="2"),
                DependencyEdge(prerequisite="2", dependent="1"),
            ),
        )


def test_active_form_is_only_the_in_progress_display_label():
    task = Task(
        id="1", subject="Run tests", activeForm="Running tests", status="in_progress",
    )
    assert task.display_label() == "Running tests"
    assert task.model_copy(update={"status": "completed"}).display_label() == "Run tests"


@pytest.mark.parametrize(
    ("parser", "value", "expected"),
    [
        (parse_task_status, "completed", "completed"),
        (parse_task_id, 12, "12"),
        (lambda value: parse_task_ids(value, field="blocked_by"), ["2", 1], ("1", "2")),
        (lambda value: parse_task_text(value, field="subject", limit=10, required=True), "  work  ", "work"),
    ],
)
def test_task_input_parsers_normalize_at_the_owner_boundary(parser, value, expected):
    assert parser(value) == expected


def test_task_input_parsers_reject_malformed_values():
    with pytest.raises(TaskValidationError):
        parse_task_status("unknown")
    with pytest.raises(TaskValidationError):
        parse_task_text("", field="subject", limit=10, required=True)


# ---------------------------------------------------------------------
# Production-path evidence for the B3 ledger.  Every test below mounts
# the plugin tree through the real application factory, so the calls
# reach the tools through ``ctx.tools.register`` and the engine's
# standard dispatch, and the outcomes land in durable history.
# ---------------------------------------------------------------------


def _tool_messages(messages):
    """Durable tool outcomes keyed by the model-authored call id."""
    return {
        message.call.id: message
        for message in messages
        if isinstance(message, ToolMessage)
    }


def _outcome_text(outcome) -> str:
    from XBotv2.core.parts import TextPart

    return "".join(
        part.text
        for part in outcome.output.parts
        if isinstance(part, TextPart)
    )


def _prompt(request) -> str:
    from XBotv2.core.parts import TextPart

    return "\n".join(
        part.text
        for message in request.messages
        for part in getattr(message, "parts", ())
        if isinstance(part, TextPart)
    )


async def _run_turn(engine, content: str) -> list:
    from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget

    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content=content),
    )
    return [event async for event in engine.run_turn(item)]


async def _start(
    temp_data_dir,
    temp_workspace,
    responses,
    *,
    config=None,
    additional_plugins=(),
):
    from XBotv2.application.app import start_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM
    from XBotv2.permissions.contracts import PermissionPolicy

    llm = MockLLM(responses=responses)
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="todolist-production",
        thread_id="main",
        workspace_root=temp_workspace,
        llm_override=llm,
        extra_plugins=[
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
            {"id": "todolist", "config": config or {}},
            *additional_plugins,
        ],
    )
    application.permissions.replace_policies((PermissionPolicy(
        default_decision="allow",
    ),))
    return application, llm


@pytest.mark.asyncio
async def test_task_tool_failures_are_typed_and_the_turn_continues(
    temp_data_dir, temp_workspace,
):
    """Each rejection has its own typed code, and none of them ends the turn.

    Production path: application factory → plugin tree mount →
    ``ctx.tools.register`` → engine tool dispatch → durable history.
    """
    from XBotv2.agentloop.protocol import LoopError
    from XBotv2.core.messages import AssistantMessage, ToolMessage
    from XBotv2.core.parts import TextPart
    from XBotv2.core.tools import ToolFailed, ToolSucceeded
    from XBotv2.core.paths import RuntimePaths

    application, _llm = await _start(temp_data_dir, temp_workspace, [
        {"tool_calls": [
            {"id": "good-create", "name": "task_create",
             "args": {"subject": "Write the report"}},
            {"id": "empty-subject", "name": "task_create",
             "args": {"subject": "   "}},
            {"id": "no-fields", "name": "task_update", "args": {"taskId": "1"}},
            {"id": "unknown-id", "name": "task_update",
             "args": {"taskId": "404", "status": "completed"}},
        ]},
        {"content": "One task was created; three calls were rejected."},
    ])
    try:
        events = await _run_turn(application.engine, "Manage the work")
        messages = application.loop_state.history.snapshot()
    finally:
        await application.destroy()

    assert not [event for event in events if isinstance(event, LoopError)]
    outcomes = _tool_messages(messages)
    assert isinstance(outcomes["good-create"].outcome, ToolSucceeded)
    assert _outcome_text(outcomes["good-create"].outcome) == (
        "Created task #1: Write the report"
    )
    assert outcomes["empty-subject"].outcome.error.code == "invalid_task"
    assert outcomes["no-fields"].outcome.error.code == "invalid_task_update"
    assert outcomes["unknown-id"].outcome.error.code == "task_not_found"
    assert all(
        isinstance(outcomes[call_id].outcome, ToolFailed)
        for call_id in ("empty-subject", "no-fields", "unknown-id")
    )
    # The turn still reached its final assistant reply, durably.
    assert any(
        isinstance(message, AssistantMessage)
        and "rejected" in "".join(
            part.text for part in message.parts if isinstance(part, TextPart)
        )
        for message in messages
    )
    resumed, _ = await _start(temp_data_dir, temp_workspace, [
        {"content": "Resumed."},
    ])
    try:
        assert resumed.loop_state.history.snapshot() == tuple(messages)
    finally:
        await resumed.destroy()


@pytest.mark.asyncio
async def test_config_reaches_the_registry_schema_and_the_task_limit(
    temp_data_dir, temp_workspace,
):
    """Plugin config decides both the advertised bounds and the limit."""
    application, llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"tool_calls": [
                {"id": "first", "name": "task_create",
                 "args": {"subject": "Only task"}},
                {"id": "second", "name": "task_create",
                 "args": {"subject": "One too many"}},
            ]},
            {"content": "The list is full."},
        ],
        config={"max_tasks": 1, "max_subject_chars": 80},
    )
    try:
        await _run_turn(application.engine, "Plan")
        messages = application.loop_state.history.snapshot()
        request = llm.request_history[0]
    finally:
        await application.destroy()

    outcomes = _tool_messages(messages)
    assert "Created task #1: Only task" in _outcome_text(
        outcomes["first"].outcome
    )
    assert outcomes["second"].outcome.error.code == "task_limit"
    # The configured bound is what the registry advertises to the model.
    tool = next(t for t in request.tools if t.name == "task_create")
    assert tool.parameters["properties"]["subject"]["maxLength"] == 80


@pytest.mark.asyncio
async def test_dependencies_block_start_and_reject_cycles(
    temp_data_dir, temp_workspace,
):
    """A blocked task cannot start until its prerequisite completes, and the
    graph never accepts a cycle or a self edge."""
    application, _llm = await _start(temp_data_dir, temp_workspace, [
        {"tool_calls": [
            {"id": "create-a", "name": "task_create",
             "args": {"subject": "Ship the module"}},
            {"id": "create-b", "name": "task_create",
             "args": {"subject": "Review the module"}},
            {"id": "link-b", "name": "task_update",
             "args": {"taskId": "2", "add_blocked_by": ["1"]}},
            {"id": "start-b-blocked", "name": "task_update",
             "args": {"taskId": "2", "status": "in_progress"}},
        ]},
        {"tool_calls": [
            {"id": "complete-a", "name": "task_update",
             "args": {"taskId": "1", "status": "completed"}},
            {"id": "start-b", "name": "task_update",
             "args": {"taskId": "2", "status": "in_progress"}},
        ]},
        {"tool_calls": [
            {"id": "cycle", "name": "task_update",
             "args": {"taskId": "1", "add_blocked_by": ["2"]}},
            {"id": "self-edge", "name": "task_update",
             "args": {"taskId": "1", "add_blocks": ["1"]}},
        ]},
        {"content": "Blocked start rejected, unblocked after the prerequisite, "
                    "cycles rejected."},
    ])
    try:
        await _run_turn(application.engine, "Coordinate the work")
        messages = application.loop_state.history.snapshot()
    finally:
        await application.destroy()

    outcomes = _tool_messages(messages)
    assert "Updated task #2" in _outcome_text(outcomes["link-b"].outcome)
    blocked = outcomes["start-b-blocked"].outcome
    assert blocked.error.code == "blocked"
    assert "#1" in blocked.error.message
    assert outcomes["complete-a"].outcome.kind == "succeeded"
    assert outcomes["start-b"].outcome.kind == "succeeded"
    cycle = outcomes["cycle"].outcome
    assert cycle.error.code == "invalid_task_dependency"
    assert "acyclic" in cycle.error.message
    self_edge = outcomes["self-edge"].outcome
    assert self_edge.error.code == "invalid_task_dependency"
    assert "itself" in self_edge.error.message


@pytest.mark.asyncio
async def test_stale_reminder_queues_folds_into_one_turn_and_resets_on_use(
    temp_data_dir, temp_workspace,
):
    """The reminder is a queued runtime notice: it reaches the turn after the
    threshold exactly once, and any task mutation resets the counter."""
    from XBotv2.core.messages import HumanInputMessage, RuntimeNoticeMessage

    application, llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"tool_calls": [
                {"id": "create", "name": "task_create",
                 "args": {"subject": "Draft the plan"}},
            ]},
            {"content": "Plan saved."},
            {"content": "Working without the task tools."},
            {"content": "Still working without the task tools."},
            {"tool_calls": [
                {"id": "start", "name": "task_update",
                 "args": {"taskId": "1", "status": "in_progress"}},
            ]},
            {"content": "Back on the plan."},
            {"content": "Done for now."},
        ],
        config={"reminder_after_turns": 2},
    )
    try:
        await _run_turn(application.engine, "Please draft the plan")   # 1: creates
        await _run_turn(application.engine, "Keep going")             # 2: counter 1
        await _run_turn(application.engine, "Keep going again")       # 3: queues
        await _run_turn(application.engine, "Where are we?")          # 4: folds
        messages = application.loop_state.history.snapshot()
        requests = llm.request_history
    finally:
        await application.destroy()

    # The reminder is folded at the first available step boundary. Later
    # requests see the same persisted notice, never duplicate injections.
    reminder_requests = [request for request in requests if "system_reminder" in _prompt(request)]
    assert reminder_requests
    assert reminder_requests[0] is requests[4]
    for request in reminder_requests:
        assert _prompt(request).count(
            '<system_reminder source="todo" event="reminder">'
        ) == 1
    assert "task tools haven't been used recently" in _prompt(reminder_requests[0])
    # Exactly one todo notice was persisted, folded before that turn's input.
    notices = [
        message for message in messages
        if isinstance(message, RuntimeNoticeMessage) and message.source == "todo"
    ]
    assert len(notices) == 1
    asked = next(
        index for index, message in enumerate(messages)
        if isinstance(message, HumanInputMessage)
        and "Where are we?" in message.parts[0].text
    )
    assert messages.index(notices[0]) < asked


@pytest.mark.asyncio
async def test_reminder_counter_and_snapshot_survive_close_and_resume(
    temp_data_dir, temp_workspace,
):
    """Plugin state is durable: the resumed application continues the counter
    and serves the same list through the production operation."""
    from XBotv2.core.operations import EmptyRequest, dispatch_operation
    from XBotv2.core.messages import RuntimeNoticeMessage
    from XBotv2.todolist.contracts import GET_TODOS, TaskList

    config = {"reminder_after_turns": 2}
    application, _llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"tool_calls": [
                {"id": "create", "name": "task_create",
                 "args": {"subject": "Draft the plan"}},
            ]},
            {"content": "Plan saved."},
            {"content": "Working without the task tools."},
        ],
        config=config,
    )
    try:
        await _run_turn(application.engine, "Please draft the plan")
        await _run_turn(application.engine, "Keep going")
        assert not [
            message for message in application.loop_state.history.snapshot()
            if isinstance(message, RuntimeNoticeMessage)
        ]
    finally:
        await application.destroy()

    # One stale turn left the counter at 1; a fresh application reads that
    # back from disk, so a single further turn queues the reminder.
    resumed, llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"content": "Still working."},
            {"content": "Noted."},
        ],
        config=config,
    )
    try:
        snapshot = await dispatch_operation(resumed, GET_TODOS, EmptyRequest())
        assert isinstance(snapshot, TaskList)
        assert [task.subject for task in snapshot.tasks] == ["Draft the plan"]
        assert snapshot.tasks[0].status == "pending"

        await _run_turn(resumed.engine, "Keep going again")
        requests = llm.request_history
    finally:
        await resumed.destroy()

    assert requests[-1].messages
    assert '<system_reminder source="todo" event="reminder">' in _prompt(
        requests[-1]
    )


@pytest.mark.asyncio
async def test_compaction_restates_unfinished_tasks_once(
    temp_data_dir, temp_workspace,
):
    """Real compact commands notify unfinished work once through the engine inbox.

    A second compaction after the task list is complete must not enqueue another
    runtime notice. The test crosses the application command, compact service,
    history mutation, plugin event, engine input, and durable history paths.
    """
    from XBotv2.agentloop.events import Events, InputAccepted
    from XBotv2.commands import EXECUTE_COMMAND, ExecuteCommand
    from XBotv2.compact.protocol import CompactionCompleted
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.core.messages import RuntimeNoticeMessage
    from XBotv2.application import RUNTIME_EVENT
    from XBotv2.core.operations import EmptyRequest, dispatch_operation
    from XBotv2.persistence.store import ThreadPersistence
    from XBotv2.todolist.contracts import GET_TODOS, TaskList

    application, llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"tool_calls": [
                {"id": "create-a", "name": "task_create",
                 "args": {"subject": "Ship the module"}},
                {"id": "create-b", "name": "task_create",
                 "args": {"subject": "Review the module"}},
                {"id": "complete-a", "name": "task_update",
                 "args": {"taskId": "1", "status": "completed"}},
            ]},
            {"content": "Planned."},
            {"content": "The task list was preserved."},
            {"tool_calls": [
                {"id": "complete-b", "name": "task_update",
                 "args": {"taskId": "2", "status": "completed"}},
            ]},
            {"content": "All done."},
            {"content": "Wrapping up."},
            {"content": "Second compact summary."},
        ],
        config={"reminder_after_turns": 50},
        additional_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    runtime_events = []
    todo_inputs = []

    def observe_input(event: InputAccepted) -> None:
        if (
            isinstance(event.message, RuntimeNoticeMessage)
            and event.message.source == "todo"
        ):
            todo_inputs.append(event.message)

    try:
        application.on(RUNTIME_EVENT, lambda event: runtime_events.append(event.event))
        application.on(Events.INPUT_ACCEPTED, observe_input)
        await _run_turn(application.engine, "Plan the work")
        compacted = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )
        assert compacted.status == "ok"
        assert any(isinstance(event, CompactionCompleted) for event in runtime_events)
        await _run_turn(application.engine, "Continue")
        prompt = _prompt(llm.request_history[-1])
        assert '<system_reminder source="todo" event="compaction">' in prompt
        assert "Review the module" in prompt
        assert len(todo_inputs) == 1
        await _run_turn(application.engine, "Finish everything")
        completed = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )
        assert completed.status == "ok"
        assert len(todo_inputs) == 1
        requests = llm.request_history
    finally:
        await application.destroy()

    assert any(isinstance(event, CompactionCompleted) for event in runtime_events)
    assert requests

    paths = RuntimePaths.from_data_dir(temp_data_dir)
    persisted = ThreadPersistence.open(
        paths.session("todolist-production").thread("main"),
        thread_id="main",
    )
    persisted_transcript = persisted.history.page_transcript(limit=160).items
    assert sum(
        isinstance(message, RuntimeNoticeMessage)
        and message.source == "todo"
        and message.event == "compaction"
        for message in persisted_transcript
    ) == 1

    resumed, _ = await _start(
        temp_data_dir,
        temp_workspace,
        [],
        config={"reminder_after_turns": 50},
        additional_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    try:
        tasks = await dispatch_operation(resumed, GET_TODOS, EmptyRequest())
        assert isinstance(tasks, TaskList)
        assert [task.status for task in tasks.tasks] == ["completed", "completed"]
    finally:
        await resumed.destroy()
