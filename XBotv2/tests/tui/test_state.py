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

import itertools

import pytest

from XBotv2.agentloop.protocol import (
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    LoopTurnStarted,
    StartedToolCall,
    ToolCallsStarted as LoopToolCallsStarted,
    TurnCancelled as CancelledOutcome,
    TurnFinished as FinishedOutcome,
)
from XBotv2.compact.protocol import (
    CompactionCompleted,
    CompactionFailed,
    CompactionMetrics,
    CompactionStarted,
)
from XBotv2.core.domain import (
    AgentExecutionLimits,
    CompletedStop,
    Cursor,
    GenerationSettings,
    ModelRoute,
    ModelTiming,
    ReasoningGenerationMode,
    ProviderMeasured,
    RequestObservation,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
    TokenCounters,
    TurnId,
    TurnRequest,
    UsageSnapshot,
)
from XBotv2.core.messages import CompactionSummaryMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.tools import (
    ToolCall,
    ToolCallRef,
    ToolDenied,
    ToolError,
    ToolFailed,
    ToolOutput,
    ToolSucceeded,
    ToolTiming,
)
from XBotv2.core.history import HistoryPage
from XBotv2.core.timing import SessionStats
from XBotv2.interactions.protocol import (
    Answered,
    ClientNotice as ClientNoticeModel,
    UserInputOption,
    UserInputRecorded,
    UserInputRequest,
)
from XBotv2.jobs.contracts import JobView
from XBotv2.permissions.contracts import Allowed, PermissionRequest, ToolPermission
from XBotv2.permissions.protocol import PermissionResponseRecorded
from XBotv2.session.contracts import HistoryMutation, PendingInputData, ThreadSummary
from XBotv2.session.records import (
    AssistantRecord,
    ConversationRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
)
from XBotv2.usage import UsageUpdated
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedEvent,
    OpenSessionResponse,
    QueueUpdatedData,
)
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNoticeReceived,
    CompactionChanged,
    ConnectionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
    InteractionResolved,
    InterruptAsked,
    InterruptSettled,
    JobUpdated,
    JobsReplaced,
    OlderHistoryFailed,
    OlderHistoryLoaded,
    OlderHistoryRequested,
    QueueReplaced,
    RuntimeNoticePublished,
    SessionConfigured,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    ToolCallsStarted,
    ToolRecordReceived,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UsageSnapshotReceived,
    UserInputFailed,
    UserInputSubmitted,
    UserMessagePublished,
    ThreadRead,
)
from XBotv2.tui.state import (
    HistoryAvailable,
    HistoryComplete,
    HistoryFailed,
    HistoryLoading,
    SessionState,
    reduce,
    release_oldest_loaded_page,
)
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
    return TurnStarted(payload=LoopTurnStarted(turn=turn))


def turn_finished(turn: int = 1) -> TurnFinished:
    return TurnFinished(payload=LoopTurnEnded(
        turn=turn, outcome=FinishedOutcome(stop_reason="completed"),
    ))


def turn_cancelled(turn: int = 1, reason: str = "client_interrupt") -> TurnCancelled:
    return TurnCancelled(payload=LoopTurnEnded(
        turn=turn, outcome=CancelledOutcome(reason=reason),
    ))


def delta(*, content: str | None = None, reasoning: str | None = None) -> AssistantDelta:
    if reasoning is not None:
        return AssistantDelta(payload=AssistantReasoningDelta(text=reasoning))
    return AssistantDelta(payload=AssistantTextDelta(text=content or ""))


def completed(
    message_id: str = "a1", content: str = "", *, reasoning: str = ""
) -> AssistantCompleted:
    return AssistantCompleted(payload=AssistantRecord(
        id=message_id, content=content, reasoning=reasoning,
        timing=ModelTiming(total_ms=0),
        stop=CompletedStop(),
    ))


def tool_started(*calls: StartedToolCall) -> ToolCallsStarted:
    return ToolCallsStarted(payload=LoopToolCallsStarted(calls=tuple(calls)))


def tool_result(
    call_id: str = "c1",
    *,
    name: str = "bash",
    outcome_kind: str = "succeeded",
    content: object = "",
    error: dict | None = None,
) -> ToolRecordReceived:
    text = content if isinstance(content, str) else __import__("json").dumps(content, ensure_ascii=False)
    output = ToolOutput(parts=(TextPart(text=text),))
    if outcome_kind == "succeeded":
        outcome = ToolSucceeded(output=output)
    elif outcome_kind == "failed":
        outcome = ToolFailed(
            error=ToolError(
                code=str((error or {}).get("code", "tool_error")),
                message=str((error or {}).get("message", text)),
            ),
            output=output,
        )
    elif outcome_kind == "denied":
        outcome = ToolDenied(reason=text or "denied")
    else:
        raise ValueError(f"unsupported tool outcome in test: {outcome_kind}")
    return ToolRecordReceived(payload=ToolRecord(
        id=call_id,
        call=ToolCallRef(id=call_id, name=name),
        outcome=outcome,
        timing=ToolTiming(duration_ms=0),
    ))


def error_frame(code: str = "engine_error", message: str = "boom") -> ErrorFrame:
    return ErrorFrame(payload=LoopError(code=code, message=message))


def usage(**counters) -> UsageSnapshotReceived:
    observed_context = counters.pop("observed_context", None)
    observation = None
    if observed_context is not None:
        selection = ResolvedModelSelection(
            route=ModelRoute(provider="p", model="m"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128,
            ),
            context_window=4096,
        )
        observation = RequestObservation(
            selection=selection,
            purpose=TurnRequest(turn_id=TurnId("turn-1")),
            estimated_input_tokens=0,
            observed_context=ProviderMeasured(tokens=observed_context),
        )
    return UsageSnapshotReceived(payload=UsageUpdated(
        snapshot=UsageSnapshot(
            total_counters=TokenCounters(**counters),
            latest_turn_observation=observation,
        ),
    ))


def user_message(message_id: str = "m1", content: str = "hi") -> UserMessagePublished:
    return UserMessagePublished(payload=HumanInputRecord(id=message_id, content=content))


def queue(*items: PendingInputData) -> QueueReplaced:
    return QueueReplaced(payload=QueueUpdatedData(items=list(items)))


def history(
    *items: HumanInputRecord | AssistantRecord | ToolRecord,
    operation: str = "clear",
    removed_turns: int = 0,
    turns: int = 0,
    older_cursor: str | None = None,
) -> HistoryReplaced:
    return HistoryReplaced(payload=HistoryUpdatedEvent(
        operation=operation,
        mutation=HistoryMutation(
            removed_turns=removed_turns,
            history=HistoryPage(items=items, older_cursor=older_cursor),
            stats=SessionStats(turns=turns),
        ),
    ))


def configured(*, agent_name: str = "XBotv2", provider: str = "p", model: str = "m", model_mode: str = "", context_window: int = 4096) -> SessionConfigured:
    selection = ResolvedRuntimeSelection(
        agent_name=agent_name,
        prompt="",
        limits=AgentExecutionLimits(),
        enabled_tools=(),
        model=ResolvedModelSelection(
            route=ModelRoute(provider=provider, model=model),
            generation=GenerationSettings(
                mode=(
                    ReasoningGenerationMode(effort=model_mode)
                    if model_mode
                    else StandardGenerationMode()
                ),
                max_output_tokens=context_window,
            ),
            context_window=context_window,
        ),
    )
    return SessionConfigured(payload=AgentConfiguredData(runtime_selection=selection))


def notice(text: str = "heads up", level: str = "info") -> ClientNotice:
    return ClientNoticeReceived(payload=ClientNoticeModel(message=text, level=level, source="test"))


def compaction_started() -> CompactionChanged:
    return CompactionChanged(payload=CompactionStarted(
        reason="manual", messages_before=1, history_chars_before=1, context_tokens_before=1
    ))


def compaction_completed(summary: str = "") -> CompactionChanged:
    return CompactionChanged(payload=CompactionCompleted(
        reason="manual", summary=CompactionSummaryMessage(id="summary", summary=summary), metrics=_metrics()
    ))


def compaction_failed(message: str = "no budget") -> CompactionChanged:
    return CompactionChanged(payload=CompactionFailed(reason="manual", message=message))


def snapshot(**overrides) -> OpenSessionResponse:
    from XBotv2.tests.tui.factories import snapshot as build_snapshot

    return build_snapshot(**overrides)


def thread(**overrides) -> ThreadSummary:
    base: dict = {
        "session_id": "s1",
        "thread_id": "agent",
        "status": "active",
        "kind": "main",
        "turn_status": "idle",
    }
    return ThreadSummary(**{**base, **overrides})


#: Transcript identity for fixtures that do not care which node they mean. The
#: wire requires one, so the fixture supplies a distinct one per item.
_NODE_IDS = itertools.count(1)


def _node_id(overrides: dict) -> str:
    return str(overrides.pop("id", None) or f"node-{next(_NODE_IDS)}")


def history_user(content: str = "hello", **overrides) -> HumanInputRecord:
    message_id = _node_id(overrides)
    if overrides:
        raise TypeError(f"unsupported human record fields: {tuple(overrides)}")
    return HumanInputRecord(id=message_id, content=content)


def history_assistant(
    content: str = "hi", *, tool_calls: tuple[ToolCall, ...] = (), **overrides
) -> AssistantRecord:
    message_id = _node_id(overrides)
    if overrides:
        raise TypeError(f"unsupported assistant record fields: {tuple(overrides)}")
    return AssistantRecord(
        id=message_id, content=content, tool_calls=tool_calls,
        timing=ModelTiming(total_ms=0), stop=CompletedStop(),
    )


def history_tool(call_id: str, content: str, *, id: str) -> ToolRecord:
    return ToolRecord(
        id=id,
        call=ToolCallRef(id=call_id, name="bash"),
        outcome=ToolSucceeded(output=ToolOutput(parts=(TextPart(text=content),))),
        timing=ToolTiming(duration_ms=0),
    )


def call(call_id: str = "c1", name: str = "bash", args: dict | None = None) -> StartedToolCall:
    return StartedToolCall(call=ToolCall(id=call_id, name=name, args=args or {}), category="execute")


def job(job_id: str = "j1", status: str = "running", kind: str = "agent") -> JobView:
    return JobView(
        id=job_id,
        kind=kind,
        label="review",
        state=status,
        elapsed_ms=0,
    )


def interaction_recorded(
    request_id: str, *, status: str = "answered", decision: str = ""
) -> InteractionResolved:
    if decision:
        approval = Allowed(scope="session" if decision == "allow" else "once")
        return InteractionResolved(payload=PermissionResponseRecorded(
            interaction_id=request_id, approval=approval,
        ))
    return InteractionResolved(payload=UserInputRecorded(
        interaction_id=request_id, resolution=Answered(answer=status),
    ))


def permission_request(request_id: str = "r1", reason: str = "needs approval"):
    return PermissionRequest(
        interaction_id=request_id,
        source="permission_system",
        reason=reason,
        subject=ToolPermission(tool_call=ToolCall(id="c1", name="bash", args={"command": "rm -rf /"})),
    )


def user_input_request(request_id: str = "q1", question: str = "Which one?"):
    return UserInputRequest(
        interaction_id=request_id,
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
    values: list[str] = []
    for entry in state.timeline:
        if isinstance(entry, (UserEntry, AssistantEntry)):
            values.append(entry.content)
        elif isinstance(entry, ToolEntry):
            values.append(str(entry.result))
        elif isinstance(entry, NoticeEntry):
            values.append(entry.text)
        else:
            raise TypeError(f"Unsupported timeline entry: {type(entry).__name__}")
    return values


def _metrics() -> CompactionMetrics:
    return CompactionMetrics(
        context_tokens_before=1,
        context_tokens_after_estimate=1,
        context_tokens_released_estimate=0,
        estimate_source="heuristic",
        history_chars_before=1,
        history_chars_after=1,
        summary_chars=1,
        summary_truncated=False,
        messages_before=1,
        messages_after=1,
        messages_removed=0,
    )


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


def test_thread_read_restores_the_persisted_turn_number() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot()),
        ThreadRead(payload=thread(session_stats={"turns": 3})),
    )
    assert state.turn == 3


# --- snapshot contents ----------------------------------------------------


def test_snapshot_seeds_the_timeline_from_history() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(
                history=[
                    history_user("hello"),
                    history_assistant(
                        "hi",
                        tool_calls=(ToolCall(
                            id="t1", name="bash", args={"command": "pwd"}
                        ),),
                    ),
                    history_tool("t1", "ok", id="tool-1"),
                ]
            )
        ),
    )
    assert kinds(state) == [EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL]
    assert contents(state) == ["hello", "hi", "ok"]
    tool = state.timeline.get("tool-1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "success"
    assert tool.args == {"command": "pwd"}


def test_model_facing_runtime_input_is_not_rendered_as_a_transcript_turn() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(history=[RuntimeNoticeRecord(
                id="notice-1", source="goal", event="round", content="reminder",
            )])
        ),
    )
    assert kinds(state) == []


def test_live_model_facing_runtime_input_is_not_rendered_as_a_transcript_turn() -> None:
    state = session(
        connected(),
        RuntimeNoticePublished(payload=RuntimeNoticeRecord(
            id="notice-1",
            source="subagent_1",
            event="completed",
            content='<runtime_event><payload encoding="json">{}</payload></runtime_event>',
        )),
    )

    assert kinds(state) == []


def test_snapshot_replaces_a_previous_timeline_instead_of_appending() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history=[history_user("old")])),
        SnapshotAdopted(snapshot(history=[history_user("new")])),
    )
    assert contents(state) == ["new"]


# --- replayed identity ----------------------------------------------------
#
# A completed assistant record carries its server identity, and history uses the
# same identity. Replaying it must replace rather than duplicate the live entry.


def test_replayed_items_are_stored_under_their_wire_identity() -> None:
    state = session(
        connected(),
        SnapshotAdopted(
            snapshot(
                history=[
                    history_user("hello", id="input-1"),
                    history_assistant("hi", id="assistant-1-0-abcd"),
                    history_tool("call-9", "ok", id="tool-call-9"),
                ]
            )
        ),
    )
    assert state.timeline.ids() == (
        "input-1", "assistant-1-0-abcd", "tool-call-9",
    )


def test_two_records_of_one_node_are_one_entry() -> None:
    """The identity is the node's, so a page that repeats a node the client
    already holds updates that entry instead of adding a second one."""
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history=[history_assistant("hi", id="7")])),
        history(history_assistant("hi, corrected", id="7")),
    )

    assert state.timeline.ids() == ("7",)
    entry = state.timeline.get("7")
    assert isinstance(entry, AssistantEntry)
    assert entry.content == "hi, corrected"


def test_a_replayed_history_replaces_what_the_frames_built() -> None:
    """A reload is a fresh start using the same server-owned record identities."""
    state = session(
        connected(),
        UserInputSubmitted(input_id="input-1", content="hello"),
        user_message("input-1", "hello"),
        delta(content="par"),
        completed("assistant-1", "answer"),
    )
    live = state.timeline.ids()
    assert live[0] == "input-1", "the prompt keeps the id the client submitted"
    assert live[1] == "assistant-1", "completion adopts the server record identity"

    history(history_user("hello", id="1"), history_assistant("answer", id="2"))
    reduce(state, history(history_user("hello", id="1"), history_assistant("answer", id="2")))

    assert state.timeline.ids() == ("1", "2")
    assert contents(state) == ["hello", "answer"]


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
                usage=UsageSnapshot(
                    total_counters=TokenCounters(input=100, output=20),
                ),
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
    assert state.usage.total_counters.input == 100
    assert state.usage.total_counters.output == 20
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
        completed("a1", "partial answer"),
    )
    assert len(state.timeline) == 1
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.content == "partial answer"
    assert entry.streaming is False
    assert state.stream_entry_id is None


def test_completion_uses_the_canonical_record_reasoning() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(reasoning="why"),
        completed("a1", "answer", reasoning="canonical reasoning"),
    )
    entry = next(iter(state.timeline))
    assert isinstance(entry, AssistantEntry)
    assert entry.reasoning == "canonical reasoning"


def test_completion_without_deltas_appends_one_entry() -> None:
    state = session(connected(), turn_started(), completed("a1", "no deltas arrived"))
    assert state.timeline.ids() == ("a1",)


def test_reasoning_streams_into_the_same_entry() -> None:
    state = session(
        connected(),
        turn_started(),
        delta(reasoning="thinking "),
        delta(reasoning="hard"),
        delta(content="answer"),
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
        tool_result("c1", outcome_kind="succeeded", content="a\nb"),
    )
    tool = state.timeline.get("c1")
    assert isinstance(tool, ToolEntry)
    assert tool.status == "success"
    assert tool.result == "a\nb"


def test_tool_result_without_a_started_call_still_lands() -> None:
    state = session(
        connected(), tool_result("c9", outcome_kind="denied", content="no")
    )
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
    """Nothing was cancelled, so nothing may be reported as cancelled -- and the
    server's "idle" is adopted rather than contradicted by a stale local flag.

    This test used to assert the contradiction (``turn_open is True`` next to
    ``server_turn is IDLE``): it kept the status on "Running" and the turn's
    tools open for good. Ported from the runtime-compat work on
    ``fix-runtime-stream-context-compat``.
    """
    state = session(connected(), turn_started(), InterruptAsked(), InterruptSettled(cancelled=False))
    assert state.facts.interrupt is Interrupt.NONE
    assert state.facts.server_turn is ServerTurn.IDLE
    assert state.facts.turn_open is False
    assert derive(state.facts) is Status.READY
    assert not [
        entry for entry in state.timeline if isinstance(entry, NoticeEntry)
        and "interrupted" in entry.text.lower()
    ], "no success is claimed for an interrupt that landed on nothing"


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
    assert "ID: r1" in entry.text
    assert "bash" in entry.text


def test_user_input_request_blocks_the_turn_visibly() -> None:
    state = session(connected(), turn_started(), InteractionOpened(user_input_request()))
    assert derive(state.facts) is Status.WAITING_USER
    entry = next(iter(state.timeline))
    assert isinstance(entry, NoticeEntry)
    assert "ID: q1" in entry.text
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


def test_usage_events_replace_the_authoritative_snapshot() -> None:
    state = session(
        connected(),
        usage(input=10, output=5),
        usage(input=2, output=3),
    )
    assert state.usage.total_counters.input == 2
    assert state.usage.total_counters.output == 3


def test_usage_snapshot_does_not_merge_missing_fields_from_an_older_snapshot() -> None:
    state = session(connected(), usage(input=10), usage(output=4))
    assert state.usage.total_counters.input == 0
    assert state.usage.total_counters.output == 4


def test_context_tokens_track_the_reported_context_size() -> None:
    state = session(connected(), usage(observed_context=1234))
    observation = state.usage.latest_turn_observation
    assert observation is not None
    assert isinstance(observation.observed_context, ProviderMeasured)
    assert observation.observed_context.tokens == 1234


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
        JobUpdated(job("j1", "succeeded")),
    )
    assert list(state.jobs) == ["j1"]
    assert state.facts.jobs_running == 0


def test_terminal_jobs_do_not_count_as_running() -> None:
    state = session(
        connected(),
        JobUpdated(job("j1", "succeeded")),
        JobUpdated(job("j2", "cancelled_before_start", "shell")),
    )
    assert state.facts.jobs_running == 0


def test_authoritative_job_snapshot_replaces_stale_thread_jobs() -> None:
    from XBotv2.jobs.protocol import JobListResponse

    state = session(
        connected(),
        JobUpdated(job("old", "running")),
        JobsReplaced(payload=JobListResponse(
            session_id="s1",
            thread_id="agent",
            jobs=[job("current", "succeeded")],
        )),
    )

    assert list(state.jobs) == ["current"]
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


# --- settling what a lost terminal frame leaves behind --------------------
#
# Ported from the in-flight runtime-compat work on fix-runtime-stream-context-compat
# (see the merge that landed it): the old client confirmed the interrupt against
# the thread list and cancelled whatever was left running. The rewrite kept the
# local turn flag open after the server had already answered "idle", so the
# status stayed Running and tools stayed running with it.


def test_an_interrupt_that_found_nothing_running_settles_the_turn() -> None:
    state = session(
        connected(),
        turn_started(),
        tool_started(call("c1")),
        InterruptAsked(),
        InterruptSettled(cancelled=False),
    )
    assert state.facts.server_turn is ServerTurn.IDLE, "the server's answer is adopted"
    assert state.facts.turn_open is False, "a local flag must not outlive it"
    assert derive(state.facts) is Status.READY
    assert state.timeline.get("c1").status == "cancelled", "no tool stays running forever"


def test_a_thread_read_that_says_idle_cancels_what_is_still_open() -> None:
    """The watchdog is the authority when the terminal frame never arrives."""
    state = session(
        connected(),
        turn_started(),
        tool_started(call("c1")),
        ThreadRead(payload=thread(turn_status="idle")),
    )
    assert state.facts.turn_open is False
    assert derive(state.facts) is Status.READY
    assert state.timeline.get("c1").status == "cancelled"


def test_a_thread_read_that_says_running_leaves_open_work_alone() -> None:
    state = session(
        connected(),
        turn_started(),
        tool_started(call("c1")),
        ThreadRead(payload=thread(turn_status="running")),
    )
    assert state.facts.turn_open is True
    assert state.timeline.get("c1").status == "running"


# --- older history --------------------------------------------------------
#
# The client holds a window, not the conversation. These tests pin what it knows
# about the part it does not hold: whether there is more, whether a page is in
# flight, and why the last one failed -- all of it derived from frames and page
# results, never guessed.


def page(
    *items: ConversationRecord,
    cursor: str | None = None,
    requested: str = "c1",
) -> OlderHistoryLoaded:
    return OlderHistoryLoaded(
        cursor=Cursor(requested),
        payload=HistoryPage(
            items=items,
            older_cursor=Cursor(cursor) if cursor is not None else None,
        ),
    )


def test_attach_records_that_older_history_exists() -> None:
    state = session(connected(), SnapshotAdopted(snapshot(history_cursor="c1")))

    assert state.older == HistoryAvailable(cursor="c1")


def test_attach_without_a_cursor_means_the_beginning_is_held() -> None:
    state = session(connected(), SnapshotAdopted(snapshot()))

    assert state.older == HistoryComplete()


def test_asking_for_older_history_reports_it_is_loading() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history_cursor="c1")),
        OlderHistoryRequested(),
    )

    assert state.older == HistoryLoading(cursor="c1")


def test_an_older_page_is_prepended_keeping_server_order() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(
            history=[history_assistant("last", id="a2")], history_cursor="c1",
        )),
        page(
            history_user("first", id="u1"),
            history_assistant("reply", id="a1"),
        ),
    )

    assert contents(state) == ["first", "reply", "last"]
    assert state.timeline.ids() == ("u1", "a1", "a2")
    assert state.older == HistoryComplete()


def test_an_older_page_advances_the_cursor_to_the_page_before_it() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history_cursor="c1")),
        page(history_user("first", id="u1"), cursor="c0"),
    )

    assert state.older == HistoryAvailable(cursor="c0")


def test_an_older_page_that_repeats_held_entries_does_not_duplicate_them() -> None:
    """A page boundary is not a promise: a message that arrived live can also
    appear in the page that overlaps it."""
    state = session(
        connected(),
        SnapshotAdopted(snapshot(
            history=[history_assistant("last", id="a2")], history_cursor="c1",
        )),
        page(
            history_user("first", id="u1"),
            history_assistant("last", id="a2"),
        ),
    )

    assert state.timeline.ids() == ("u1", "a2")


def test_loading_the_assistant_page_backfills_a_split_tool_calls_arguments() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(
            history=[history_tool("call-1", "done", id="tool-1")],
            history_cursor="c1",
        )),
    )
    tool = state.timeline.get("tool-1")
    assert isinstance(tool, ToolEntry)
    assert tool.call_id == "call-1"
    assert tool.args == {}

    reduce(state, page(history_assistant(
        "",
        id="assistant-1",
        tool_calls=(ToolCall(
            id="call-1", name="read", args={"path": "/repo/README.md"}
        ),),
    )))

    tool = state.timeline.get("tool-1")
    assert isinstance(tool, ToolEntry)
    assert tool.args == {"path": "/repo/README.md"}


def test_a_failed_older_page_is_visible_and_retryable() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history_cursor="c1")),
        OlderHistoryRequested(),
        OlderHistoryFailed("older history is unavailable"),
    )

    assert state.older == HistoryFailed(
        cursor="c1", message="older history is unavailable",
    )

    reduce(state, OlderHistoryRequested())
    assert state.older == HistoryLoading(cursor="c1")


def test_a_history_rewrite_puts_the_cursor_where_the_server_says() -> None:
    """``/clear`` and ``/undo`` send a fresh page: what the client held is gone,
    and so is the cursor the older pages were relative to."""
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history_cursor="c1")),
        page(history_user("first", id="u1")),
    )
    assert state.older == HistoryComplete()

    reduce(state, history(
        history_user("kept", id="u2"),
        operation="undo",
        removed_turns=1,
        turns=1,
        older_cursor="c9",
    ))

    assert contents(state) == ["kept"]
    assert state.older == HistoryAvailable(cursor="c9")


def test_releasing_the_front_page_restores_the_cursor_that_preceded_it() -> None:
    """Residency is bounded by letting a whole loaded page go, not by trimming.

    A loaded page is exactly the span between two cursors, so releasing it puts
    the client back where it stood before the load: the cursor in effect then is
    the cursor for the page before the new front. Nothing becomes unreachable --
    the reader can load the same page again.
    """
    state = session(
        connected(),
        SnapshotAdopted(snapshot(history=[history_assistant("tail", id="a9")], history_cursor="c1")),
        page(history_user("second", id="u2"), requested="c1", cursor="c0"),
        page(history_user("first", id="u1"), requested="c0", cursor=None),
    )
    assert state.older == HistoryComplete()

    release_oldest_loaded_page(state)

    assert state.timeline.ids() == ("u2", "a9")
    # Back to the cursor the second page was read with, so the first page can be
    # fetched again.
    assert state.older == HistoryAvailable(cursor="c0")


def test_releasing_never_touches_the_window_the_attach_returned() -> None:
    """The attach window is what the client always holds; only pages the reader
    asked for can be released, so a client that never paged never shrinks."""
    state = session(
        connected(),
        SnapshotAdopted(snapshot(
            history=[history_user("only", id="u1")], history_cursor="c1",
        )),
    )

    assert state.loaded_pages == []
    assert state.timeline.ids() == ("u1",)


def test_releasing_a_page_that_repeated_held_entries_removes_only_what_it_added() -> None:
    state = session(
        connected(),
        SnapshotAdopted(snapshot(
            history=[history_user("tail", id="u3")], history_cursor="c1",
        )),
        page(
            history_user("old", id="u1"),
            history_user("tail", id="u3"),
            requested="c1",
            cursor=None,
        ),
    )

    release_oldest_loaded_page(state)

    assert state.timeline.ids() == ("u3",)
