"""The session reducer: turn lifecycle, streaming, steering, tools, recovery.

Every test drives the reducer through typed ``UiEvent``s and asserts observable
outcomes (timeline ids/content, derived status). No test writes state fields
directly, because no production code does either: the reducer is the single
owner of the facts the status is derived from.

Invariants under test:
  I1  only turn frames and watchdog readings decide whether a turn runs
  I2  a streamed entry only ever changes through an upsert of its own id
  I3  nothing is dropped for being outside whatever the reader is looking at
  I4  a gap, a failed submission, and an unanswered prompt are all visible
  I6  a user interjection commits the streaming suffix before it lands
  I7  turn/status facts are only ever written here
"""

from __future__ import annotations

import pytest

from XBotv2.agentloop.protocol import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    ErrorEventData,
    ToolCallStartedItem,
    ToolCallsStartedData,
    ToolResultData,
    TurnCancelledData,
    TurnData,
)
from XBotv2.compact.protocol import (
    CompactionCompletedData,
    CompactionFailedData,
    CompactionStartedData,
)
from XBotv2.core.tools import ToolCall
from XBotv2.core.usage import UsageData
from XBotv2.interactions.protocol import (
    ClientMessageData,
    InteractionRecordedData,
    UserInputOption,
    UserInputRequiredData,
)
from XBotv2.jobs.contracts import JobSnapshot
from XBotv2.permissions.protocol import PermissionRequestData
from XBotv2.session.contracts import PendingInputData, SessionHistoryItem, ThreadSummary
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedData,
    MessageData,
    OpenSessionResponse,
    QueueUpdatedData,
)
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNotice,
    CompactionChanged,
    ConnectionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
    InteractionResolved,
    InterruptAsked,
    InterruptSettled,
    JobUpdated,
    QueueReplaced,
    SessionConfigured,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    ToolCallsStarted,
    ToolResult,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
    UserInputFailed,
    UserInputSubmitted,
    UserMessagePublished,
    ThreadRead,
)
from XBotv2.tui.state import SessionState, reduce
from XBotv2.tui.status import Connection, Interaction, Interrupt, ServerTurn, Status, derive
from XBotv2.tui.timeline import (
    AssistantEntry,
    Delivery,
    EntryKind,
    NoticeEntry,
    ToolEntry,
    UserEntry,
)


# --- builders -------------------------------------------------------------


def session(*events) -> SessionState:
    state = SessionState()
    for event in events:
        reduce(state, event)
    return state


def connected() -> ConnectionChanged:
    return ConnectionChanged(Connection.CONNECTED)


def turn_started(turn: int = 1, slots: dict | None = None) -> TurnStarted:
    return TurnStarted(payload=TurnData(turn=turn, status_slots=slots or {}))


def turn_finished(turn: int = 1) -> TurnFinished:
    return TurnFinished(payload=TurnData(turn=turn))


def turn_cancelled(turn: int = 1, reason: str = "client_interrupt") -> TurnCancelled:
    return TurnCancelled(payload=TurnCancelledData(turn=turn, reason=reason))


def delta(*, content: str | None = None, reasoning: str | None = None) -> AssistantDelta:
    fields = {}
    if content is not None:
        fields["content"] = content
    if reasoning is not None:
        fields["reasoning"] = reasoning
    return AssistantDelta(payload=AssistantMessageDeltaData(**fields or {"content": ""}))


def completed(message_id: str = "a1", content: str = "") -> AssistantCompleted:
    return AssistantCompleted(payload=AssistantMessageData(id=message_id, content=content))


def tool_started(*calls: ToolCallStartedItem) -> ToolCallsStarted:
    return ToolCallsStarted(payload=ToolCallsStartedData(tool_calls=list(calls)))


def tool_result(
    call_id: str = "c1",
    *,
    name: str = "bash",
    status: str = "success",
    content: object = "",
    error: dict | None = None,
) -> ToolResult:
    return ToolResult(payload=ToolResultData(
        tool_call_id=call_id, name=name, status=status, content=content, error=error
    ))


def error_frame(code: str = "engine_error", message: str = "boom") -> ErrorFrame:
    return ErrorFrame(payload=ErrorEventData(code=code, message=message))


def usage(**counters) -> UsageUpdated:
    return UsageUpdated(payload=UsageData(**counters))


def user_message(message_id: str = "m1", content: str = "hi") -> UserMessagePublished:
    return UserMessagePublished(payload=MessageData(id=message_id, role="user", content=content))


def queue(*items: PendingInputData) -> QueueReplaced:
    return QueueReplaced(payload=QueueUpdatedData(items=list(items)))


def history(*items: SessionHistoryItem) -> HistoryReplaced:
    return HistoryReplaced(payload=HistoryUpdatedData(
        history=list(items), operation="clear", turns=0
    ))


def configured(**fields) -> SessionConfigured:
    return SessionConfigured(payload=AgentConfiguredData(**fields))


def notice(text: str = "heads up", level: str = "info") -> ClientNotice:
    return ClientNotice(payload=ClientMessageData(message=text, level=level, source="test"))


def compaction_started() -> CompactionChanged:
    return CompactionChanged(payload=CompactionStartedData(
        reason="manual", messages_before=1, history_chars_before=1, context_tokens_before=1
    ))


def compaction_completed(summary: str = "") -> CompactionChanged:
    return CompactionChanged(payload=CompactionCompletedData(
        reason="manual", summary=summary, metrics=_metrics()
    ))


def compaction_failed(message: str = "no budget") -> CompactionChanged:
    return CompactionChanged(payload=CompactionFailedData(reason="manual", message=message))


def snapshot(**overrides) -> OpenSessionResponse:
    base: dict = {
        "session_id": "s1",
        "thread_id": "agent",
        "agent_name": "XBotv2",
        "workspace_root": "/w",
        "provider": "p",
        "model": "m",
        "model_mode": "",
        "context_window": 0,
        "usage": UsageData(),
        "status_slots": {},
        "event_cursor": 0,
    }
    return OpenSessionResponse(**{**base, **overrides})


def thread(**overrides) -> ThreadSummary:
    base: dict = {
        "session_id": "s1",
        "thread_id": "agent",
        "status": "active",
        "kind": "main",
        "turn_status": "idle",
    }
    return ThreadSummary(**{**base, **overrides})


def history_user(content: str = "hello", **overrides) -> SessionHistoryItem:
    return SessionHistoryItem(role="user", content=content, **overrides)


def history_assistant(content: str = "hi", **overrides) -> SessionHistoryItem:
    return SessionHistoryItem(role="assistant", content=content, **overrides)


def call(call_id: str = "c1", name: str = "bash", args: dict | None = None) -> ToolCallStartedItem:
    return ToolCallStartedItem(id=call_id, name=name, args=args or {})


def job(job_id: str = "j1", status: str = "running", kind: str = "agent") -> JobSnapshot:
    return JobSnapshot(
        job_id=job_id,
        kind=kind,
        status=status,
        command="review",
        cwd="/w",
        created_at=0.0,
        started_at=0.0,
        finished_at=0.0,
    )


def interaction_recorded(
    request_id: str, *, status: str = "answered", decision: str = ""
) -> InteractionResolved:
    return InteractionResolved(payload=InteractionRecordedData(
        request_id=request_id, status=status, decision=decision
    ))


def permission_request(request_id: str = "r1", reason: str = "needs approval"):
    return PermissionRequestData(
        request_id=request_id,
        source="permission_system",
        reason=reason,
        tool_call=ToolCall(id="c1", name="bash", args={"command": "rm -rf /"}),
    )


def user_input_request(request_id: str = "q1", question: str = "Which one?"):
    return UserInputRequiredData(
        request_id=request_id,
        source="ask_user",
        tool_call_id="c1",
        question=question,
        options=[
            UserInputOption(label="A", description="the first"),
            UserInputOption(label="B", description="the second"),
        ],
    )


def kinds(state: SessionState) -> list[EntryKind]:
    return [entry.kind for entry in state.timeline]


def contents(state: SessionState) -> list[str]:
    """Body text of each entry, whichever field carries it."""
    return [
        getattr(entry, "content", None)
        or getattr(entry, "result", None)
        or getattr(entry, "text", "")
        for entry in state.timeline
    ]


def _metrics() -> dict:
    return {
        "context_tokens_before": 1,
        "context_tokens_after_estimate": 1,
        "context_tokens_released_estimate": 0,
        "estimate_source": "heuristic",
        "history_chars_before": 1,
        "history_chars_after": 1,
        "summary_chars": 1,
        "summary_truncated": False,
        "messages_before": 1,
        "messages_after": 1,
        "messages_removed": 0,
    }


# --- I7/I1: the reducer owns the status facts ----------------------------


def test_new_state_is_connecting() -> None:
    assert derive(SessionState().facts) is Status.CONNECTING


def test_connection_event_moves_the_status() -> None:
    state = session(connected())
    assert derive(state.facts) is Status.READY
    reduce(state, ConnectionChanged(Connection.DISCONNECTED, "server closed"))
    assert derive(state.facts) is Status.DISCONNECTED


def test_turn_started_marks_the_turn_open_and_running() -> None:
    state = session(connected(), turn_started(3))
    assert state.facts.turn_open is True
    assert state.facts.server_turn is ServerTurn.RUNNING
    assert state.turn == 3
    assert derive(state.facts) is Status.RUNNING


def test_turn_finished_closes_the_turn_and_marks_the_server_idle() -> None:
    state = session(connected(), turn_started(), turn_finished())
    assert state.facts.turn_open is False
    assert state.facts.server_turn is ServerTurn.IDLE
    assert derive(state.facts) is Status.READY


def test_turn_cancelled_closes_the_turn_and_says_so() -> None:
    state = session(connected(), turn_started(), turn_cancelled())
    assert state.facts.turn_open is False
    assert derive(state.facts) is Status.READY
    assert kinds(state) == [EntryKind.NOTICE]
    assert "interrupt" in next(iter(state.timeline)).text.lower()


def test_error_frame_does_not_stop_the_turn() -> None:
    """A transient provider error frame is not a terminal frame; the server
    keeps streaming until turn_finished. Treating it as terminal is how a
    running turn ended up displayed as Ready/Error."""
    state = session(connected(), turn_started(), error_frame(message="hiccup"))
    assert state.facts.turn_open is True
    assert state.facts.last_error == "hiccup"
    assert derive(state.facts) is Status.RUNNING


def test_error_message_is_recorded_in_the_transcript() -> None:
    state = session(connected(), error_frame())
    assert kinds(state) == [EntryKind.ERROR]
    assert derive(state.facts) is Status.ERROR
    assert state.facts.turn_open is False


def test_a_new_turn_clears_the_previous_error() -> None:
    state = session(connected(), error_frame(), turn_started(2))
    assert state.facts.last_error is None
    assert derive(state.facts) is Status.RUNNING


# --- I1: the snapshot is a baseline, not a turn answer -------------------


def test_snapshot_does_not_change_what_the_client_knows_about_the_turn() -> None:
    """The descriptor the protocol returns carries no turn answer, so adopting
    one must never settle a turn the client believes is running."""
    state = session(connected(), turn_started(), SnapshotAdopted(snapshot()))
    assert state.facts.turn_open is True
    assert state.facts.server_turn is ServerTurn.RUNNING
    assert derive(state.facts) is Status.RUNNING


def test_snapshot_does_not_invent_a_running_turn() -> None:
    state = session(connected(), SnapshotAdopted(snapshot()))
    assert state.facts.turn_open is False
    assert state.facts.server_turn is ServerTurn.UNKNOWN
    assert derive(state.facts) is Status.READY


def test_watchdog_idle_settles_a_turn_whose_terminal_frame_was_lost() -> None:
    state = session(
        connected(),
        turn_started(),
        ThreadRead(payload=thread(turn_status="idle")),
    )
    assert state.facts.turn_open is False
    assert derive(state.facts) is Status.READY


def test_watchdog_running_restores_a_turn_the_client_never_saw_start() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot()),
        ThreadRead(payload=thread(turn_status="running")),
    )
    assert state.facts.server_turn is ServerTurn.RUNNING
    assert derive(state.facts) is Status.RUNNING


def test_watchdog_readings_are_honoured_in_both_directions() -> None:
    state = session(
        connected(),
        ThreadRead(payload=thread(turn_status="running")),
        ThreadRead(payload=thread(turn_status="idle")),
    )
    assert derive(state.facts) is Status.READY


# --- snapshot contents ----------------------------------------------------


def test_snapshot_seeds_the_timeline_from_history() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(
                history=[
                    history_user("hello"),
                    history_assistant("hi"),
                    SessionHistoryItem(
                        role="tool", content="ok", tool_call_id="t1", status="success"
                    ),
                ]
            )
        ),
    )
    assert kinds(state) == [EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL]
    assert contents(state) == ["hello", "hi", "ok"]
    tool = state.timeline.get("t1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "success"


def test_history_tool_without_a_status_is_a_success() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(history=[SessionHistoryItem(role="tool", content="ok", tool_call_id="t1")])
        ),
    )
    tool = state.timeline.get("t1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "success"


def test_injected_history_turn_is_a_notice_not_typed_input() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(history=[history_user("reminder", runtime={"source": "goal", "event": "round"})])
        ),
    )
    assert kinds(state) == [EntryKind.NOTICE]
    assert not any(isinstance(entry, UserEntry) for entry in state.timeline)


def test_snapshot_replaces_a_previous_timeline_instead_of_appending() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history=[history_user("old")])),
        SnapshotAdopted(snapshot(history=[history_user("new")])),
    )
    assert contents(state) == ["new"]


def test_snapshot_seeds_usage_identity_and_queue() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(
                agent_name="Other",
                provider="deepseek",
                model="v4",
                model_mode="fast",
                context_window=64_000,
                status_slots={"goal": "ship"},
                usage=UsageData(input_tokens=100, output_tokens=20, total_tokens=120),
                pending_inputs=[
                    PendingInputData(message_id="q1", content="queued", target="next-turn")
                ],
            )
        ),
    )
    assert (state.agent_name, state.provider, state.model, state.model_mode) == (
        "Other",
        "deepseek",
        "v4",
        "fast",
    )
    assert state.context_window == 64_000
    assert state.status_slots == {"goal": "ship"}
    assert state.usage["input_tokens"] == 100
    assert state.usage["total_tokens"] == 120
    assert [item.message_id for item in state.queue] == ["q1"]


def test_history_replaced_swaps_the_timeline_and_drops_the_stream_target() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(content="streaming"),
        history(history_user("rewritten")),
    )
    assert contents(state) == ["rewritten"]
    assert state.stream_entry_id is None, "a rewrite must not leave a stale stream target"


# --- I2: streaming integrity ---------------------------------------------


def test_deltas_grow_one_assistant_entry_in_place() -> None:
    state = session(connected(), turn_started(), delta(content="Hel"), delta(content="lo"))
    assert kinds(state) == [EntryKind.ASSISTANT]
    assert contents(state) == ["Hello"]
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.streaming is True


def test_deltas_never_touch_an_entry_that_is_not_the_stream() -> None:
    """The defect: a streamed tail landed in whatever entry happened to be
    last, rewriting an already-rendered historical message."""
    state = session(
        connected(),
        turn_started(),
        delta(content="first answer"),
        completed("a1", "first answer"),
    )
    before = state.timeline.get("a1")
    reduce(state, delta(content="\n\nsecond answer"))
    assert state.timeline.get("a1") == before
    assert len(state.timeline) == 2


def test_completion_closes_the_stream_and_keeps_one_entry() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(content="partial"),
        AssistantCompleted(
            payload=AssistantMessageData(id="a1", content="partial answer")
        ),
    )
    assert len(state.timeline) == 1
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.content == "partial answer"
    assert entry.streaming is False
    assert state.stream_entry_id is None


def test_completion_keeps_the_reasoning_that_only_deltas_carried() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(reasoning="why"),
        completed("a1", "answer"),
    )
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.reasoning == "why"


def test_completion_without_deltas_appends_one_entry() -> None:
    state = session(connected(), turn_started(), completed("a1", "no deltas arrived"))
    assert state.timeline.ids() == ("a1",)


def test_reasoning_streams_into_the_same_entry() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(reasoning="thinking "),
        delta(content="answer", reasoning="hard"),
    )
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.reasoning == "thinking hard"
    assert entry.content == "answer"


def test_an_empty_delta_changes_nothing() -> None:
    state = session(connected(), turn_started(), delta(content=""))
    assert len(state.timeline) == 0


# --- I6: a steering message commits the streaming suffix -----------------


def test_user_message_arriving_mid_stream_lands_after_the_committed_suffix() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(content="looking at the code"),
        user_message("m2", "change B instead"),
        delta(content="understood"),
        completed("a2", "understood"),
    )
    assert kinds(state) == [EntryKind.ASSISTANT, EntryKind.USER, EntryKind.ASSISTANT]
    assert contents(state) == ["looking at the code", "change B instead", "understood"]
    assert getattr(list(state.timeline)[0], "streaming") is False


def test_committing_the_suffix_does_not_merge_the_two_answers() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(content="A"),
        user_message("m1", "steer"),
        delta(content="B"),
    )
    assert contents(state) == ["A", "steer", "B"]


# --- I3: nothing is dropped ----------------------------------------------


def test_a_long_stream_of_entries_is_fully_retained() -> None:
    state = session(connected(), turn_started(), delta(content="answer"))
    for index in range(300):
        reduce(state, user_message(f"m{index}", str(index)))
    assert len(state.timeline) == 301
    assert state.timeline.ids()[0].startswith("local:"), "the streamed entry keeps its slot"
    assert state.timeline.get("m299") is not None


# --- I4: a gap is visible ------------------------------------------------


def test_stream_gap_is_surfaced_to_the_user() -> None:
    state = session(connected(), StreamGapDetected(expected=41, received=57))
    assert kinds(state) == [EntryKind.NOTICE]
    notice_entry = next(iter(state.timeline))
    assert isinstance(notice_entry, NoticeEntry)
    assert "41" in notice_entry.text and "57" in notice_entry.text


# --- tools ----------------------------------------------------------------


def test_tool_calls_started_appends_a_running_tool_entry() -> None:
    state = session(
        connected(),
        turn_started(),
        tool_started(call("c1", "bash", {"command": "ls"})),
    )
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.name == "bash"
    assert tool.args == {"command": "ls"}
    assert tool.status == "running"
    assert tool.started_at > 0


def test_tool_result_completes_the_same_entry() -> None:
    state = session(
        connected(),
        turn_started(),
        tool_started(call()),
        tool_result("c1", status="success", content="a\nb"),
    )
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "success"
    assert tool.result == "a\nb"


def test_structured_tool_result_is_kept_as_sent() -> None:
    """Tool payloads are stored raw; formatting them is the view's job."""
    state = session(connected(), tool_result("c1", content={"a": 1}))
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.result == {"a": 1}


def test_tool_result_without_a_started_call_still_lands() -> None:
    state = session(connected(), tool_result("c9", status="denied", content="no"))
    tool = state.timeline.get("c9")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "denied"


def test_error_frame_finalizes_pending_tools() -> None:
    """A tool left pending forever after a failed turn kept showing a growing
    elapsed timer for work that had already stopped."""
    state = session(
        connected(),
        turn_started(),
        tool_started(call()),
        error_frame(),
    )
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "error"
    assert tool.finished_at > 0


def test_turn_finished_finalizes_pending_tools() -> None:
    state = session(
        connected(),
        turn_started(),
        tool_started(call()),
        turn_finished(),
    )
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "cancelled"


# --- local submission correlation ----------------------------------------


def test_submitted_input_appears_immediately_as_pending() -> None:
    """The user sees their own message before the server echoes it, and the
    composer can show "sending" -- but the *status* is not a guess about a turn
    the server may not have started yet (a queued input leaves the thread idle).
    """
    state = session(connected(), UserInputSubmitted(input_id="in-1", content="hello"))
    entry = state.timeline.get("in-1")
    assert isinstance(entry, UserEntry)
    assert entry.delivery is Delivery.PENDING
    assert state.submission_in_flight is True
    assert derive(state.facts) is Status.READY


def test_published_message_upgrades_the_pending_entry_without_duplicating() -> None:
    state = session(
        connected(),
        UserInputSubmitted(input_id="in-1", content="hello"),
        user_message("in-1", "hello"),
    )
    assert state.timeline.ids() == ("in-1",)
    entry = state.timeline.get("in-1")
    assert isinstance(entry, UserEntry)
    assert entry.delivery is Delivery.ACCEPTED
    assert state.submission_in_flight is False


def test_published_message_from_another_client_is_appended() -> None:
    state = session(connected(), user_message("remote-1", "from elsewhere"))
    entry = state.timeline.get("remote-1")
    assert isinstance(entry, UserEntry)
    assert entry.delivery is Delivery.ACCEPTED


def test_failed_submission_is_visible_and_not_left_pending() -> None:
    state = session(
        connected(),
        UserInputSubmitted(input_id="in-1", content="hello"),
        UserInputFailed(input_id="in-1", error="connection reset"),
    )
    entry = state.timeline.get("in-1")
    assert isinstance(entry, UserEntry)
    assert entry.delivery is Delivery.FAILED
    assert len([e for e in state.timeline if e.kind is EntryKind.ERROR]) == 1


def test_submitted_input_binds_by_id_not_by_content() -> None:
    """Two identical messages must not be conflated: matching on content was
    how a queued entry could be popped by the wrong event."""
    state = session(
        connected(),
        UserInputSubmitted(input_id="in-1", content="same"),
        UserInputSubmitted(input_id="in-2", content="same"),
        user_message("in-2", "same"),
    )
    first = state.timeline.get("in-1")
    second = state.timeline.get("in-2")
    assert isinstance(first, UserEntry) and isinstance(second, UserEntry)
    assert first.delivery is Delivery.PENDING
    assert second.delivery is Delivery.ACCEPTED


# --- interrupt ------------------------------------------------------------


def test_interrupt_request_is_visible_until_the_turn_ends() -> None:
    state = session(connected(), turn_started(), InterruptAsked())
    assert derive(state.facts) is Status.INTERRUPTING


def test_settled_interrupt_that_never_landed_does_not_claim_success() -> None:
    state = session(connected(), turn_started(), InterruptAsked(), InterruptSettled(cancelled=False))
    assert state.facts.interrupt is Interrupt.NONE
    assert state.facts.turn_open is True
    assert derive(state.facts) is Status.RUNNING


def test_settled_interrupt_ends_the_turn() -> None:
    state = session(connected(), turn_started(), InterruptAsked(), InterruptSettled(cancelled=True))
    assert state.facts.turn_open is False
    assert derive(state.facts) is Status.READY


# --- interactions ---------------------------------------------------------


def test_permission_request_blocks_the_turn_visibly() -> None:
    state = session(connected(), turn_started(), InteractionOpened(permission_request()))
    assert derive(state.facts) is Status.APPROVAL_REQUIRED
    assert state.pending_interactions == {"r1": permission_request()}
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert "bash" in entry.text


def test_user_input_request_blocks_the_turn_visibly() -> None:
    state = session(connected(), turn_started(), InteractionOpened(user_input_request()))
    assert derive(state.facts) is Status.WAITING_USER
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert "Which one?" in entry.text
    assert "A — the first" in entry.text


def test_a_permission_prompt_outranks_a_question() -> None:
    state = session(
        connected(),
        InteractionOpened(user_input_request()),
        InteractionOpened(permission_request()),
    )
    assert derive(state.facts) is Status.APPROVAL_REQUIRED


def test_resolving_one_prompt_leaves_the_other_blocking() -> None:
    state = session(
        connected(),
        InteractionOpened(permission_request()),
        InteractionOpened(user_input_request()),
        interaction_recorded("r1", status="answered", decision="allow"),
    )
    assert state.pending_interactions == {"q1": user_input_request()}
    assert derive(state.facts) is Status.WAITING_USER


def test_resolution_updates_the_prompt_entry_in_place() -> None:
    state = session(
        connected(),
        InteractionOpened(permission_request()),
        interaction_recorded("r1", status="answered", decision="allow"),
    )
    assert len(state.timeline) == 1, "resolution must not append a second entry"
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert "→ allow" in entry.text


def test_resolving_an_unknown_prompt_changes_nothing() -> None:
    state = session(connected(), InteractionOpened(permission_request()))
    reduce(state, interaction_recorded("nope", decision="deny"))
    assert state.pending_interactions == {"r1": permission_request()}
    assert derive(state.facts) is Status.APPROVAL_REQUIRED


# --- usage, compaction, queue, notices -----------------------------------


def test_usage_accumulates_across_events() -> None:
    state = session(
        connected(),
        usage(input_tokens=10, output_tokens=5, total_tokens=15),
        usage(input_tokens=2, output_tokens=3, total_tokens=5),
    )
    assert state.usage["input_tokens"] == 12
    assert state.usage["output_tokens"] == 8
    assert state.usage["total_tokens"] == 20


def test_usage_fields_the_provider_did_not_report_are_untouched() -> None:
    state = session(connected(), usage(input_tokens=10), usage(output_tokens=4))
    assert state.usage["input_tokens"] == 10
    assert state.usage["output_tokens"] == 4


def test_context_tokens_track_the_reported_context_size() -> None:
    state = session(connected(), usage(context_tokens=1234))
    assert state.context_input_tokens == 1234


def test_context_tokens_fall_back_to_the_input_breakdown() -> None:
    state = session(connected(), usage(input_tokens=7, cache_read_input_tokens=3))
    assert state.context_input_tokens == 10


def test_compaction_makes_the_status_visible_then_returns_a_summary() -> None:
    state = session(connected(), compaction_started())
    assert derive(state.facts) is Status.COMPACTING
    assert len(state.timeline) == 0
    reduce(state, compaction_completed(summary="kept the gist"))
    assert derive(state.facts) is Status.READY
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert entry.detail == "kept the gist"


def test_failed_compaction_is_visible() -> None:
    state = session(connected(), compaction_failed("no budget"))
    assert derive(state.facts) is Status.READY
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert "no budget" in entry.text


def test_queue_snapshot_replaces_the_queue() -> None:
    state = session(
        connected(),
        queue(PendingInputData(message_id="q1", content="a", target="next-turn")),
    )
    assert [item.message_id for item in state.queue] == ["q1"]
    reduce(state, queue())
    assert state.queue == ()


def test_server_notice_is_visible_with_its_level() -> None:
    state = session(connected(), notice("careful", level="warning"))
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert entry.notice_kind == "client_message"
    assert entry.text == "careful"
    assert entry.level == "warning"


# --- session configuration ------------------------------------------------


def test_agent_configured_updates_identity() -> None:
    state = session(
        connected(),
        configured(
            agent_name="Reviewer",
            provider="openai",
            model="gpt",
            model_mode="fast",
            context_window=32_000,
        ),
    )
    assert (state.agent_name, state.provider, state.model) == ("Reviewer", "openai", "gpt")
    assert state.model_mode == "fast"
    assert state.context_window == 32_000


def test_status_slots_are_replaced_not_merged() -> None:
    state = session(connected(), StatusSlotsUpdated({"goal": "ship"}))
    assert state.status_slots == {"goal": "ship"}
    reduce(state, StatusSlotsUpdated({"effort": "high"}))
    assert state.status_slots == {"effort": "high"}


# --- jobs -----------------------------------------------------------------


def test_running_jobs_are_visible_while_the_thread_is_idle() -> None:
    state = session(connected(), JobUpdated(job("j1", "running", "agent")))
    assert state.facts.jobs_running == 1
    assert derive(state.facts) is Status.READY, "the thread itself is idle"


def test_job_updates_replace_by_id_instead_of_appending() -> None:
    state = session(
        connected(),
        JobUpdated(job("j1", "running")),
        JobUpdated(job("j1", "completed")),
    )
    assert list(state.jobs) == ["j1"]
    assert state.facts.jobs_running == 0


def test_terminal_jobs_do_not_count_as_running() -> None:
    state = session(
        connected(),
        JobUpdated(job("j1", "completed")),
        JobUpdated(job("j2", "stopped", "shell")),
    )
    assert state.facts.jobs_running == 0


# --- the reducer is the only writer --------------------------------------


def test_reduce_returns_the_same_state_object() -> None:
    state = SessionState()
    assert reduce(state, connected()) is state


def test_unknown_event_is_rejected_loudly() -> None:
    with pytest.raises(TypeError):
        reduce(SessionState(), object())  # type: ignore[arg-type]


# --- turn duration lives in the state, not in a transcript row -------------


def test_a_turn_records_when_it_started() -> None:
    """Progress needs a start time; the previous client kept it in a widget that
    a snapshot rebuild could drop, and the transcript grew a row per turn."""
    state = SessionState()
    reduce(state, connected(), now=100.0)
    reduce(state, turn_started(3), now=104.0)
    assert state.turn_started_at == 104.0
    assert state.facts.turn_open is True


def test_no_turn_has_no_duration() -> None:
    assert SessionState().turn_started_at == 0.0


def test_a_second_turn_restarts_the_clock() -> None:
    state = SessionState()
    reduce(state, connected(), now=0.0)
    reduce(state, turn_started(1), now=10.0)
    reduce(state, turn_finished(1), now=12.0)
    reduce(state, turn_started(2), now=20.0)
    assert state.turn_started_at == 20.0


def test_a_submission_never_downgrades_a_confirmed_message() -> None:
    """The server's echo can arrive before the local submission is recorded; a
    confirmed message must not be pushed back to "sending"."""
    state = session(
        connected(),
        user_message("in-1", "hello"),
        UserInputSubmitted(input_id="in-1", content="hello"),
    )
    entry = state.timeline.get("in-1")
    assert isinstance(entry, UserEntry)
    assert entry.delivery is Delivery.ACCEPTED


def test_clearing_the_transcript_forgets_the_local_window() -> None:
    """Durable history lives on the server; the client only forgets its window."""
    from XBotv2.tui.events import TranscriptCleared

    state = session(connected(), user_message("m1", "hello"))
    assert len(state.timeline) == 1
    reduce(state, TranscriptCleared())
    assert state.timeline.ids() == ()
    assert state.stream_entry_id is None


# --- the attached thread's kind is the server's answer ---------------------


def test_a_subagent_thread_read_makes_the_view_read_only() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(thread_id="child")),
        ThreadRead(payload=thread(thread_id="child", kind="subagent", turn_status="running")),
    )
    assert state.thread_kind == "subagent"
    assert state.read_only is True, "a subagent thread is not user-driven"
    assert state.facts.server_turn is ServerTurn.RUNNING


def test_the_main_thread_read_stays_writable() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot()),
        ThreadRead(payload=thread(kind="main", turn_status="idle")),
    )
    assert state.thread_kind == "main"
    assert state.read_only is False
