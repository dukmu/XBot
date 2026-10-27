from XBotv2.tests.runtime import engine_of
"""Task-list state owns identity, dependency, and transition invariants."""

import pytest
from XBotv2.core.api.messages import ToolMessage
from XBotv2.todolist.api.models import DependencyEdge, Task, TaskList, TaskValidationError
from XBotv2.todolist.impl.parsing import (
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
    from XBotv2.core.api.parts import TextPart

    return "".join(
        part.text
        for part in outcome.output.parts
        if isinstance(part, TextPart)
    )


def _prompt(request) -> str:
    from XBotv2.core.api.parts import TextPart

    return "\n".join(
        part.text
        for message in request.messages
        for part in getattr(message, "parts", ())
        if isinstance(part, TextPart)
    )


async def _run_turn(engine, content: str) -> list:
    from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
    from XBotv2.core.api.domain import InboxTarget

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
    mode="new",
):
    from XBotv2.tests.runtime import thread_scope
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.permissions.api.models import PermissionPolicy

    llm = MockLLM(responses=responses)
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="todolist-production",
        mode=mode,
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
    from XBotv2.agentloop.api.outputs import LoopError
    from XBotv2.core.api.messages import AssistantMessage, ToolMessage
    from XBotv2.core.api.parts import TextPart
    from XBotv2.core.api.tools import ToolFailed, ToolSucceeded
    from XBotv2.core.api.paths import RuntimePaths

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
        events = await _run_turn(engine_of(application), "Manage the work")
        messages = application.loop_state.history.snapshot()
    finally:
        await application.root.close()

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
    ], mode="resume")
    try:
        assert resumed.loop_state.history.snapshot() == tuple(messages)
    finally:
        await resumed.root.close()


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
        await _run_turn(engine_of(application), "Plan")
        messages = application.loop_state.history.snapshot()
        request = llm.request_history[0]
    finally:
        await application.root.close()

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
        await _run_turn(engine_of(application), "Coordinate the work")
        messages = application.loop_state.history.snapshot()
    finally:
        await application.root.close()

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
async def test_plan_can_be_revised_without_false_progress_or_forced_delegation(
    temp_data_dir, temp_workspace,
):
    from XBotv2.agentloop.api.events import RUNTIME_EVENT
    from XBotv2.core.api.operations import EmptyRequest, dispatch_operation
    from XBotv2.todolist.api.service import GET_TODOS
    from XBotv2.todolist.api.events import TaskChanged

    calls = [
        ("a", "task_create", {"subject": "Verify implementation"}),
        ("b", "task_create", {"subject": "Ship result"}),
        ("invalid-claim", "task_update", {
            "taskId": "2", "status": "in_progress", "add_blocked_by": ["1"],
        }),
        ("link", "task_update", {"taskId": "1", "add_blocks": ["2"]}),
        ("revise", "task_update", {
            "taskId": "2", "remove_blocked_by": ["1"], "status": "in_progress",
        }),
        ("same", "task_update", {"taskId": "2", "status": "in_progress"}),
        ("done-b", "task_update", {"taskId": "2", "status": "completed"}),
        ("done-a", "task_update", {"taskId": "1", "status": "completed"}),
        ("edit-done", "task_update", {"taskId": "1", "description": "Evidence recorded"}),
        ("same-done", "task_update", {"taskId": "1", "status": "completed"}),
    ]
    application, _ = await _start(temp_data_dir, temp_workspace, [
        {"tool_calls": [{"id": call_id, "name": name, "args": args}]}
        for call_id, name, args in calls
    ] + [{"content": "The revised plan is complete."}])
    updates = []

    def observe(event):
        if isinstance(event.event, TaskChanged):
            updates.append(event.event.snapshot)

    try:
        application.on(RUNTIME_EVENT, observe)
        await _run_turn(engine_of(application), "Revise and finish the plan")
        outcomes = _tool_messages(application.loop_state.history.snapshot())
        snapshot = await dispatch_operation(application, GET_TODOS, EmptyRequest())
    finally:
        await application.root.close()

    assert outcomes["invalid-claim"].outcome.kind == "failed"
    assert outcomes["invalid-claim"].outcome.error.code == "blocked"
    assert outcomes["revise"].outcome.kind == "succeeded"
    assert [item.version for item in updates] == list(range(1, 8))
    assert snapshot.edges == ()
    assert snapshot.tasks == ()
    assert snapshot.completed_count == 2
    completion = _outcome_text(outcomes["done-a"].outcome)
    assert "evidence" in completion.lower()
    assert "subagent" not in completion.lower()
    for call_id in ("same", "edit-done", "same-done"):
        assert "Before reporting" not in _outcome_text(outcomes[call_id].outcome)


@pytest.mark.asyncio
async def test_dependency_revisions_are_atomic_and_survive_resume(
    temp_data_dir, temp_workspace,
):
    from XBotv2.agentloop.api.events import RUNTIME_EVENT
    from XBotv2.core.api.operations import EmptyRequest, dispatch_operation
    from XBotv2.todolist.api.service import GET_TODOS
    from XBotv2.todolist.api.events import TaskChanged

    calls = [
        ("a", "task_create", {"subject": "Prepare"}),
        ("b", "task_create", {"subject": "Deliver"}),
        ("link", "task_update", {"taskId": "1", "add_blocks": ["2"]}),
        ("conflict", "task_update", {
            "taskId": "1", "subject": "Must not be saved",
            "add_blocks": ["2"], "remove_blocks": ["2"],
        }),
        ("unlink", "task_update", {"taskId": "1", "remove_blocks": ["2"]}),
        ("start", "task_update", {"taskId": "2", "status": "in_progress"}),
        ("block-running", "task_update", {
            "taskId": "1", "subject": "Also must not be saved", "add_blocks": ["2"],
        }),
        ("pause", "task_update", {"taskId": "2", "status": "pending"}),
        ("relink", "task_update", {"taskId": "2", "add_blocked_by": ["1"]}),
    ]
    application, _ = await _start(temp_data_dir, temp_workspace, [
        {"tool_calls": [{"id": call_id, "name": name, "args": args}]}
        for call_id, name, args in calls
    ] + [{"content": "Revised plan saved."}])
    updates = []

    def observe(event):
        if isinstance(event.event, TaskChanged):
            updates.append(event.event.snapshot)

    try:
        application.on(RUNTIME_EVENT, observe)
        await _run_turn(engine_of(application), "Revise the prerequisites")
        outcomes = _tool_messages(application.loop_state.history.snapshot())
        saved = await dispatch_operation(application, GET_TODOS, EmptyRequest())
        assert outcomes["conflict"].outcome.error.code == "invalid_task_update"
        assert outcomes["block-running"].outcome.error.code == "blocked"
        assert outcomes["unlink"].outcome.kind == "succeeded"
        assert [value.version for value in updates] == list(range(1, 8))
        assert saved.find("1").subject == "Prepare"
        assert saved.prerequisites("2") == ("1",)
    finally:
        await application.root.close()

    resumed, _ = await _start(temp_data_dir, temp_workspace, [
        {"tool_calls": [{"id": "blocked-after-resume", "name": "task_update",
                         "args": {"taskId": "2", "status": "in_progress"}}]},
        {"content": "Prerequisite remains incomplete."},
    ], mode="resume")
    try:
        restored = await dispatch_operation(resumed, GET_TODOS, EmptyRequest())
        assert restored == saved
        await _run_turn(engine_of(resumed), "Try starting delivery")
        outcomes = _tool_messages(resumed.loop_state.history.snapshot())
        assert outcomes["blocked-after-resume"].outcome.error.code == "blocked"
        assert await dispatch_operation(resumed, GET_TODOS, EmptyRequest()) == saved
    finally:
        await resumed.root.close()


@pytest.mark.asyncio
async def test_stale_reminder_queues_folds_into_one_turn_and_resets_on_use(
    temp_data_dir, temp_workspace,
):
    """The reminder is a queued runtime notice: it reaches the turn after the
    threshold exactly once, and any task mutation resets the counter."""
    from XBotv2.core.api.messages import HumanInputMessage, RuntimeNoticeMessage

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
        await _run_turn(engine_of(application), "Please draft the plan")   # 1: creates
        await _run_turn(engine_of(application), "Keep going")             # 2: counter 1
        await _run_turn(engine_of(application), "Keep going again")       # 3: queues
        await _run_turn(engine_of(application), "Where are we?")          # 4: folds
        messages = application.loop_state.history.snapshot()
        requests = llm.request_history
    finally:
        await application.root.close()

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
    from XBotv2.core.api.operations import EmptyRequest, dispatch_operation
    from XBotv2.core.api.messages import RuntimeNoticeMessage
    from XBotv2.todolist.api.service import GET_TODOS
    from XBotv2.todolist.api.models import TaskList

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
        await _run_turn(engine_of(application), "Please draft the plan")
        await _run_turn(engine_of(application), "Keep going")
        assert not [
            message for message in application.loop_state.history.snapshot()
            if isinstance(message, RuntimeNoticeMessage)
        ]
    finally:
        await application.root.close()

    # One stale turn left the counter at 1; a fresh application reads that
    # back from disk, so a single further turn queues the reminder.
    resumed, llm = await _start(
        temp_data_dir, temp_workspace,
        [
            {"content": "Still working."},
            {"content": "Noted."},
        ],
        config=config,
        mode="resume",
    )
    try:
        snapshot = await dispatch_operation(resumed, GET_TODOS, EmptyRequest())
        assert isinstance(snapshot, TaskList)
        assert [task.subject for task in snapshot.tasks] == ["Draft the plan"]
        assert snapshot.tasks[0].status == "pending"

        await _run_turn(engine_of(resumed), "Keep going again")
        requests = llm.request_history
    finally:
        await resumed.root.close()

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
    from XBotv2.agentloop.api.events import Events, InputAccepted
    from XBotv2.commands.api.contracts import EXECUTE_COMMAND, ExecuteCommand
    from XBotv2.compact.api.protocol import CompactionCompleted
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.core.api.messages import RuntimeNoticeMessage
    from XBotv2.agentloop.api.events import RUNTIME_EVENT
    from XBotv2.core.api.operations import EmptyRequest, dispatch_operation
    from XBotv2.agentloop.api.storage import ARCHIVE
    from XBotv2.main import open_runtime
    from XBotv2.todolist.api.service import GET_TODOS
    from XBotv2.todolist.api.models import TaskList

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
        await _run_turn(engine_of(application), "Plan the work")
        compacted = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )
        assert compacted.status == "ok"
        assert any(isinstance(event, CompactionCompleted) for event in runtime_events)
        await _run_turn(engine_of(application), "Continue")
        prompt = _prompt(llm.request_history[-1])
        assert '<system_reminder source="todo" event="compaction">' in prompt
        assert "Review the module" in prompt
        assert len(todo_inputs) == 1
        await _run_turn(engine_of(application), "Finish everything")
        completed = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )
        assert completed.status == "ok"
        assert len(todo_inputs) == 1
        requests = llm.request_history
    finally:
        await application.root.close()

    assert any(isinstance(event, CompactionCompleted) for event in runtime_events)
    assert requests

    archive_root = await open_runtime(paths=RuntimePaths.from_data_dir(temp_data_dir))
    try:
        persisted_transcript = (await archive_root.require(ARCHIVE).message_page(
            "todolist-production", "main", limit=160
        )).items
    finally:
        await archive_root.close()
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
        mode="resume",
    )
    try:
        tasks = await dispatch_operation(resumed, GET_TODOS, EmptyRequest())
        assert isinstance(tasks, TaskList)
        assert tasks.tasks == ()
        assert tasks.completed_count == 2
    finally:
        await resumed.root.close()


# ---------------------------------------------------------------------------
# Regression: dependency-graph args accept integer ids (model often serialises
# them as ints; the schema must accept either form).  See _annotation_schema
# in XBotv2/core/tools.py for the corresponding anyOf emission.
# ---------------------------------------------------------------------------


def test_dependency_ids_accept_integer_strings():
    """``parse_task_ids`` normalises ints to strings before storing."""
    assert parse_task_ids([9, 10], field="add_blocks") == ("9", "10")
    assert parse_task_ids(["9", 10], field="add_blocks") == ("9", "10")
    assert parse_task_ids([], field="add_blocks") == ()
    assert parse_task_ids(None, field="add_blocks") == ()


def test_task_update_parameters_schema_accepts_integer_id_lists():
    """Schema validation must allow int-id arrays, not just strings."""
    from XBotv2.core.api.tools import tool_parameters_schema
    from XBotv2.todolist.impl.plugin import TaskService, _task_tool, TaskConfig
    from jsonschema import Draft202012Validator

    cfg = TaskConfig()

    class _FakeService:
        _config = cfg
        _agent_name = "test"

    import types as _types

    tool = _task_tool(
        _types.MethodType(TaskService.task_update, _FakeService()),
        cfg,
    )
    schema = tool_parameters_schema(tool)
    Draft202012Validator(schema).validate({"taskId": "11", "add_blocked_by": [9]})
    Draft202012Validator(schema).validate({"taskId": "11", "add_blocked_by": [9, 10]})
    Draft202012Validator(schema).validate({"taskId": "11", "add_blocks": ["2"]})
    Draft202012Validator(schema).validate({"taskId": "11", "add_blocks": ["2", 3]})
    Draft202012Validator(schema).validate({"taskId": "11", "add_blocked_by": None})
    Draft202012Validator(schema).validate({"taskId": "11"})


@pytest.mark.asyncio
async def test_completed_history_is_paged_and_releases_active_quota(
    temp_data_dir,
    temp_workspace,
):
    """Real mounted service crosses the SQL row budget, then resumes its graph."""
    import gc
    import weakref
    from XBotv2.core.api.operations import dispatch_operation
    from XBotv2.todolist.api.service import CompletedTasksRequest, GET_COMPLETED_TODOS

    application, _ = await _start(
        temp_data_dir, temp_workspace, [], config={"max_tasks": 2}
    )
    try:
        service = application.require("todolist")
        assert (await service.task_create("Anchor")).kind == "succeeded"
        for number in range(1, 522):
            assert (
                await service.task_create(f"Done {number}", description="正文" * 800)
            ).kind == "succeeded"
            task_id = str(number + 1)
            if number == 1:
                active = await service.snapshot()
                reference = weakref.ref(active.find(task_id))
                del active
            assert (
                await service.task_update(task_id, status="completed")
            ).kind == "succeeded"
        gc.collect()
        assert reference() is None
        snapshot = await service.snapshot()
        assert [task.id for task in snapshot.tasks] == ["1"]
        assert snapshot.completed_count == 521
        assert "正文" * 800 in _outcome_text(await service.task_get("2"))
        ids = []
        cursor = 0
        while True:
            page = await dispatch_operation(
                application,
                GET_COMPLETED_TODOS,
                CompletedTasksRequest(after=cursor, limit=173),
            )
            ids.extend(task.id for task in page.tasks)
            if page.next_after is None:
                break
            cursor = page.next_after
        assert ids == [str(i) for i in range(2, 523)]
        assert "#2 [x] Done 1" in _outcome_text(
            await service.task_list(completed=True, limit=1)
        )
        assert (await service.task_create("New active")).kind == "succeeded"
        assert (
            await service.task_update("2", status="pending")
        ).error.code == "task_limit"
        assert (
            await service.task_update("1", add_blocked_by=["2"])
        ).kind == "succeeded"
        assert (
            await service.task_update("1", status="in_progress")
        ).kind == "succeeded"
        assert (await service.task_delete("523")).kind == "succeeded"
        assert (
            await service.task_update("2", status="pending")
        ).error.code == "blocked"
        assert (await service.task_create("Active again")).kind == "succeeded"
        saved = await service.snapshot()
    finally:
        await application.root.close()

    resumed, _ = await _start(
        temp_data_dir, temp_workspace, [], config={"max_tasks": 2}, mode="resume"
    )
    try:
        service = resumed.require("todolist")
        assert await service.snapshot() == saved
        assert (await service.task_create("over quota")).error.code == "task_limit"
        assert (
            await service.task_update("2", status="pending")
        ).error.code == "task_limit"
        assert "正文" * 800 in _outcome_text(await service.task_get("2"))
        assert (
            await service.task_update("1", add_blocked_by=["9999"])
        ).error.code == "task_not_found"
    finally:
        await resumed.root.close()


@pytest.mark.asyncio
async def test_cancelled_after_commit_reloads_the_committed_task(
    temp_data_dir,
    temp_workspace,
    monkeypatch,
):
    import asyncio

    application, _ = await _start(temp_data_dir, temp_workspace, [])
    try:
        service = application.require("todolist")
        original = service._storage.change

        async def cancel_after_commit(**kwargs):
            await original(**kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(service._storage, "change", cancel_after_commit)
        with pytest.raises(asyncio.CancelledError):
            await service.task_create("Committed before cancellation")
        assert [task.subject for task in (await service.snapshot()).tasks] == [
            "Committed before cancellation"
        ]
    finally:
        await application.root.close()

    resumed, _ = await _start(temp_data_dir, temp_workspace, [], mode="resume")
    try:
        assert [
            task.subject
            for task in (await resumed.require("todolist").snapshot()).tasks
        ] == ["Committed before cancellation"]
    finally:
        await resumed.root.close()


@pytest.mark.asyncio
async def test_fork_copies_active_graph_and_completed_history(
    temp_data_dir, temp_workspace
):
    from XBotv2.session.api.contracts import OpenSession, SESSIONS
    from XBotv2.todolist.api.service import CompletedTasksRequest

    application, llm = await _start(
        temp_data_dir, temp_workspace, [], config={"max_tasks": 2}
    )
    try:
        service = application.require("todolist")
        await service.task_create("Prerequisite", "Saved evidence")
        await service.task_create("Dependent")
        await service.task_update("2", add_blocked_by=["1"])
        await service.task_update("1", status="completed")
        source = await service.snapshot()
        manager = application.root.require(SESSIONS)
        fork_id = await manager.fork_session("todolist-production")
        forked = await manager.open(
            OpenSession(
                fork_id, "main", None, None, "resume", False, model_override=llm
            )
        )
        fork_service = forked.require("todolist")
        assert await fork_service.snapshot() == source
        page = await fork_service.completed_page(CompletedTasksRequest())
        assert [(task.id, task.description) for task in page.tasks] == [
            ("1", "Saved evidence")
        ]
        assert (await fork_service.task_delete("1")).kind == "succeeded"
        assert "Saved evidence" in _outcome_text(await service.task_get("1"))
    finally:
        await application.root.close()


@pytest.mark.asyncio
async def test_unsupported_counter_schema_fails_without_modifying_existing_rows(
    temp_data_dir,
    temp_workspace,
):
    import sqlite3
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.todolist.api.service import CompletedTasksRequest

    application, _ = await _start(
        temp_data_dir, temp_workspace, [], config={"max_tasks": 2}
    )
    try:
        service = application.require("todolist")
        await service.task_create("Old completed", "Original detail")
        await service.task_update("1", status="completed")
        await service.task_create("Old active")
    finally:
        await application.root.close()
    database = (
        RuntimePaths.from_data_dir(temp_data_dir)
        .session("todolist-production")
        .database_file
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE todolist_counters DROP COLUMN completed_count")
        connection.execute("ALTER TABLE todolist_counters DROP COLUMN active_count")
        connection.execute("UPDATE todolist_tasks SET ordinal=0 WHERE task_id='1'")
        connection.execute("DROP INDEX todolist_tasks_status_id")
        connection.execute("DROP INDEX todolist_edges_dependent")

    with sqlite3.connect(database) as connection:
        original_rows = connection.execute(
            "SELECT task_id,subject,description,status FROM todolist_tasks ORDER BY task_id"
        ).fetchall()
        original_columns = connection.execute(
            "PRAGMA table_info(todolist_counters)"
        ).fetchall()

    resumed, _ = await _start(
        temp_data_dir, temp_workspace, [], config={"max_tasks": 2}, mode="resume"
    )
    try:
        service = resumed.require("todolist")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await service.snapshot()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await service.task_create("Must not be added")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await service.task_get("1")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await service.completed_page(CompletedTasksRequest())
    finally:
        await resumed.root.close()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT task_id,subject,description,status FROM todolist_tasks ORDER BY task_id"
            ).fetchall()
            == original_rows
        )
        assert (
            connection.execute("PRAGMA table_info(todolist_counters)").fetchall()
            == original_columns
        )


@pytest.mark.asyncio
async def test_dependency_changes_obey_database_queue_byte_budget(
    temp_data_dir,
    temp_workspace,
    monkeypatch,
):
    from dataclasses import replace
    from XBotv2.persistence.api.contracts import PERSISTENCE

    application, _ = await _start(temp_data_dir, temp_workspace, [])
    try:
        service = application.require("todolist")
        await service.task_create("Anchor", description="中" * 950)
        for number in range(2, 47):
            assert (await service.task_create(f"Task {number}")).kind == "succeeded"
        worker = application.root.require(PERSISTENCE).worker
        original_limits = worker.limits
        ids = list(range(2, 47))
        before = await service.snapshot()
        monkeypatch.setattr(
            worker, "limits", replace(original_limits, queue_bytes=4096)
        )
        with pytest.raises(ValueError, match="queue byte budget"):
            await service.task_update("1", add_blocks=ids)
        assert await service.snapshot() == before

        monkeypatch.setattr(worker, "limits", original_limits)
        assert (await service.task_update("1", add_blocks=ids)).kind == "succeeded"
        with_edges = await service.snapshot()
        monkeypatch.setattr(
            worker, "limits", replace(original_limits, queue_bytes=4096)
        )
        with pytest.raises(ValueError, match="queue byte budget"):
            await service.task_update("1", remove_blocks=ids)
        assert await service.snapshot() == with_edges
        monkeypatch.setattr(worker, "limits", original_limits)
    finally:
        await application.root.close()
