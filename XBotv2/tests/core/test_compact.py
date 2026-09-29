"""Compaction preserves typed history boundaries and bounded summaries."""

import asyncio
from pathlib import Path

import pytest

from XBotv2.agentloop import HumanInput, InboxItem
from XBotv2.agentloop.outputs import LoopError, ToolCompleted
from XBotv2.agentloop.events import Events, ModelRequestReady
from XBotv2.application.app import start_application
from XBotv2.application import RUNTIME_EVENT
from XBotv2.commands.contracts import EXECUTE_COMMAND, ExecuteCommand
from XBotv2.compact.history import compact_prefix_end, history_chars
from XBotv2.compact.events import PRE_COMPACT
from XBotv2.compact.protocol import CompactionCompleted, CompactionFailed
from XBotv2.compact.summary import (
    compacted_message,
    normalize_summary,
    strip_summary_heading,
    summary_request,
)
from XBotv2.core.artifacts import ArtifactKind, ArtifactRef
from XBotv2.core.domain import (
    InboxTarget,
    MessageId,
    ProviderError,
    ToolCallId,
    ToolTiming,
    TransactionAborted,
    TransactionEnded,
    TransactionRef,
    TransactionStarted,
)
from XBotv2.core.tokens import (
    context_token_limit,
    estimate_messages_tokens,
    estimate_model_request_tokens,
)
from XBotv2.core.history import DurableEventRecorded
from XBotv2.core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    HumanInputMessage,
    ToolMessage,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderSystem, ProviderTool, ProviderUser
from XBotv2.core.stream import ModelFailed
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import (
    ToolCall,
    ToolCallRef,
    ToolOutput,
    ToolSucceeded,
)
from XBotv2.llm.mock import MockLLM


def _human(index: int, text: str) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(f"message-{index}"),
        parts=(TextPart(text=text),),
    )


def _externalized_tool_message(store, text: str = "bounded provider preview") -> ToolMessage:
    """One tool message whose complete result lives in the ArtifactStore."""
    ref = store.put(
        ArtifactKind.TOOL_RESULT,
        b"the complete original tool result",
        media_type="text/plain; charset=utf-8",
        name="original-tool-result.txt",
        suffix=".txt",
    )
    return ToolMessage(
        id=MessageId("message-tool"),
        call=ToolCallRef(id=ToolCallId("call-1"), name="read"),
        outcome=ToolSucceeded(output=ToolOutput(
            parts=(TextPart(text=text),),
            artifacts=(ref,),
        )),
        timing=ToolTiming(duration_ms=1),
    )


def test_compaction_prefix_retains_requested_recent_turns():
    messages = tuple(_human(index, f"turn {index}") for index in range(4))
    assert compact_prefix_end(messages, keep_recent_turns=2) == 2
    assert compact_prefix_end(messages, keep_recent_turns=4) == 0
    assert history_chars(messages) == sum(len(f"turn {index}") for index in range(4))


def test_compaction_prefix_requires_at_least_one_recent_turn():
    with pytest.raises(ValueError, match=">= 1"):
        compact_prefix_end((_human(1, "one"),), keep_recent_turns=0)


def test_summary_normalization_removes_heading_and_keeps_full_text():
    assert strip_summary_heading("## Conversation Summary\nbody") == "body"
    # The summary is never truncated: an overlong answer is kept verbatim so
    # no evidence is lost, and length stays a prompt-level instruction.
    overlong = "## Conversation Summary\n" + "a" * 100
    assert normalize_summary(overlong) == "a" * 100
    with pytest.raises(RuntimeError, match="empty"):
        normalize_summary("  ")


def test_summary_request_is_provider_typed_and_does_not_mutate_history():
    message = _human(1, "important decision")
    request = summary_request((message,), max_chars=500)
    assert isinstance(request[0], ProviderSystem)
    assert isinstance(request[1], ProviderUser)
    assert isinstance(request[-1], ProviderUser)
    assert message.parts[0].text == "important decision"


def test_summary_request_resolves_tool_artifacts_through_the_store(artifact_store):
    message = _externalized_tool_message(artifact_store)

    request = summary_request(
        (message,),
        max_chars=500,
        artifacts=artifact_store,
    )

    tool = next(item for item in request if isinstance(item, ProviderTool))
    prompt = "".join(part.text for part in tool.parts if isinstance(part, TextPart))
    path = artifact_store.model_path(message.outcome.output.artifacts[0])
    assert path in prompt
    assert 'name="original-tool-result.txt"' in prompt
    assert "Use filesystem or shell tools" in prompt
    # The summary input keeps the bounded preview, never the full original.
    assert "the complete original tool result" not in prompt
    assert "bounded provider preview" in prompt


def test_summary_request_requires_a_store_for_artifact_bearing_history():
    ref = ArtifactRef(
        id="tool_results/unresolvable.txt",
        media_type="text/plain",
        name="original-tool-result.txt",
        kind=ArtifactKind.TOOL_RESULT,
        size=1,
    )
    message = ToolMessage(
        id=MessageId("message-tool"),
        call=ToolCallRef(id=ToolCallId("call-1"), name="read"),
        outcome=ToolSucceeded(output=ToolOutput(
            parts=(TextPart(text="preview"),),
            artifacts=(ref,),
        )),
        timing=ToolTiming(duration_ms=1),
    )

    with pytest.raises(RuntimeError, match="ArtifactStore for tool outputs"):
        summary_request((message,), max_chars=500)


def test_compacted_message_has_its_own_canonical_kind_and_identity():
    message = compacted_message("kept facts", reason="manual")
    assert isinstance(message, CompactionSummaryMessage)
    assert message.id.startswith("summary-")
    assert message.summary == "kept facts"


async def _run_turn(engine, content: str) -> None:
    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content=content),
    )
    [event async for event in engine.run_turn(item)]


@pytest.mark.asyncio
async def test_compact_command_runs_through_application_and_persists_summary(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
        {"content": "The user is researching X. Keep decision Y."},
    ])
    options = dict(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    application = await start_application(**options)
    compact_service = application.compact
    commands = application.commands
    tools = application.engine.tools
    runtime_events = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        assert application.commands.get("compact") is not None
        await _run_turn(application.engine, "Research X")
        await _run_turn(application.engine, "Remember decision Y")
        application.thread_persistence.history.record(
            TransactionStarted(transaction=TransactionRef(
                kind="compaction", id="orphaned-compaction"
            )),
            durable=True,
        )

        result = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )

        assert result.status == "ok"
        history = application.engine.messages
        summaries = [item for item in history if isinstance(item, CompactionSummaryMessage)]
        assert len(summaries) == 1
        assert "decision Y" in summaries[0].summary
        assert [item.parts[0].text for item in history if isinstance(item, HumanInputMessage)] == [
            "Remember decision Y",
        ]
        assert provider.call_count == 4
        assert application.thread_persistence.history.load_surface() == tuple(history)
        assert application.thread_persistence.history.open_transactions("compaction") == frozenset()
    finally:
        await application.destroy()
    assert any(isinstance(event, CompactionCompleted) for event in runtime_events)
    assert compact_service.diagnostics()["compactions"] == 0
    assert commands.get("compact") is None
    assert "compact" not in tools.names()


@pytest.mark.asyncio
async def test_compact_command_failure_preserves_live_and_persisted_history(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-failure-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    try:
        await _run_turn(application.engine, "Research X")
        await _run_turn(application.engine, "Remember decision Y")
        before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()

        result = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )

        assert result.status == "error"
        assert tuple(application.engine.messages) == before
        assert application.thread_persistence.history.load_surface() == durable_before
        assert not any(
            isinstance(item, CompactionSummaryMessage)
            for item in application.engine.messages
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_cancelled_compact_command_publishes_failure_and_aborts_transaction(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-cancelled-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    runtime_events = []
    summary_started = asyncio.Event()

    async def blocked_summary(_request):
        summary_started.set()
        await asyncio.Event().wait()
        yield  # Keep this an async iterator; cancellation must reach its await.

    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Research X")
        await _run_turn(application.engine, "Remember decision Y")
        history_before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()
        application.compact.model.provider.astream = blocked_summary

        command = asyncio.create_task(application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        ))
        await asyncio.wait_for(summary_started.wait(), timeout=2)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

        assert tuple(application.engine.messages) == history_before
        assert application.thread_persistence.history.load_surface() == durable_before
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
        trajectory_items = (
            application.thread_persistence.history
            .page_trajectory(limit=100).page.items
        )
        transaction_ends = [
            item.event
            for item in trajectory_items
            if isinstance(item, DurableEventRecorded)
            and isinstance(item.event, TransactionEnded)
            and item.event.transaction.kind == "compaction"
        ]
        assert transaction_ends
        assert isinstance(transaction_ends[-1].outcome, TransactionAborted)
        assert transaction_ends[-1].outcome.reason == "cancelled"
        assert any(isinstance(event, CompactionFailed) for event in runtime_events)
        failure = next(
            event for event in runtime_events if isinstance(event, CompactionFailed)
        )
        assert failure.message == "Compaction cancelled."
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_cancelled_precompact_listener_publishes_failure_and_aborts_transaction(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
        {"content": "Summary ready for listener"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-precommit-cancelled-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    runtime_events = []
    listener_started = asyncio.Event()

    async def wait_before_commit(_event):
        listener_started.set()
        await asyncio.Event().wait()

    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        application.on(PRE_COMPACT, wait_before_commit)
        await _run_turn(application.engine, "Research X")
        await _run_turn(application.engine, "Remember decision Y")
        live_before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()

        command = asyncio.create_task(application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        ))
        await asyncio.wait_for(listener_started.wait(), timeout=2)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len(failures) == 1
        assert failures[0].message == "Compaction cancelled."
        assert tuple(application.engine.messages) == live_before
        assert application.thread_persistence.history.load_surface() == durable_before
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
        trajectory_items = application.thread_persistence.history.page_trajectory(
            limit=100
        ).page.items
        transaction_ends = [
            item.event
            for item in trajectory_items
            if isinstance(item, DurableEventRecorded)
            and isinstance(item.event, TransactionEnded)
            and item.event.transaction.kind == "compaction"
        ]
        assert transaction_ends
        assert isinstance(transaction_ends[-1].outcome, TransactionAborted)
        assert transaction_ends[-1].outcome.reason == "cancelled"
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_cancelled_agent_compact_tool_preserves_history_and_aborts_transaction(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {
            "tool_calls": [{
                "id": "compact-call-before-cancel",
                "name": "compact",
                "args": {},
            }],
        },
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-tool-cancelled-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    runtime_events = []
    summary_started = asyncio.Event()

    async def wait_on_summary(request):
        if not request.tools:
            summary_started.set()
            await asyncio.Event().wait()
        async for event in original_astream(request):
            yield event

    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Earlier work " + "x" * 100)
        bound_provider = application.compact.model.provider
        original_astream = bound_provider.astream
        bound_provider.astream = wait_on_summary

        observed_events = []

        async def run_tool_turn():
            async for event in application.engine.run_turn(InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Compact this conversation"),
            )):
                observed_events.append(event)

        turn = asyncio.create_task(run_tool_turn())
        await asyncio.wait_for(summary_started.wait(), timeout=2)

        compact_result = next(
            event for event in observed_events
            if isinstance(event, ToolCompleted)
            and event.execution.message.call.name == "compact"
        )
        assert isinstance(compact_result.execution.message.outcome, ToolSucceeded)
        live_before_cancel = tuple(application.engine.messages)
        durable_before_cancel = (
            application.thread_persistence.history.load_surface()
        )
        assert not any(
            isinstance(message, CompactionSummaryMessage)
            for message in live_before_cancel
        )

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert tuple(application.engine.messages) == live_before_cancel
        assert (
            application.thread_persistence.history.load_surface()
            == durable_before_cancel
        )
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
        trajectory_items = application.thread_persistence.history.page_trajectory(
            limit=100
        ).page.items
        transaction_ends = [
            item.event
            for item in trajectory_items
            if isinstance(item, DurableEventRecorded)
            and isinstance(item.event, TransactionEnded)
            and item.event.transaction.kind == "compaction"
        ]
        assert transaction_ends
        assert isinstance(transaction_ends[-1].outcome, TransactionAborted)
        assert transaction_ends[-1].outcome.reason == "cancelled"
        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len(failures) == 1
        assert failures[0].message == "Compaction cancelled."
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_cancelled_automatic_compaction_preserves_history_and_aborts_transaction(
    temp_data_dir, temp_workspace
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
    ])
    plugin_overrides = [
        {
            "id": "llm",
            "config": {
                "default_provider": "test",
                "providers": {
                    "test": {
                        "protocol": "mock",
                        "default_model": "mock",
                        "models": [{
                            "model": "mock",
                            "max_context_tokens": 512,
                            "max_output_tokens": 16,
                        }],
                    },
                },
            },
        },
        {
            "id": "compact",
            "config": {
                "automatic": True,
                "trigger_ratio": 0.01,
                "output_reservation": 1,
                "keep_recent_turns": 1,
            },
        },
    ]
    application = await start_application(
        paths=paths,
        session_id="compact-automatic-cancelled-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=plugin_overrides,
    )
    runtime_events = []
    summary_started = asyncio.Event()

    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Old context " + "x" * 150)
        await _run_turn(application.engine, "New context " + "y" * 150)

        bound_provider = application.compact.model.provider
        original_astream = bound_provider.astream

        async def wait_on_automatic_summary(request):
            if not request.tools:
                summary_started.set()
                await asyncio.Event().wait()
            async for event in original_astream(request):
                yield event

        bound_provider.astream = wait_on_automatic_summary
        turn = asyncio.create_task(_run_turn(
            application.engine, "Cancel during automatic compaction"
        ))
        await asyncio.wait_for(summary_started.wait(), timeout=2)
        live_before_cancel = tuple(application.engine.messages)
        durable_before_cancel = (
            application.thread_persistence.history.load_surface()
        )

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert tuple(application.engine.messages) == live_before_cancel
        assert (
            application.thread_persistence.history.load_surface()
            == durable_before_cancel
        )
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
        trajectory_items = application.thread_persistence.history.page_trajectory(
            limit=100
        ).page.items
        transaction_ends = [
            item.event
            for item in trajectory_items
            if isinstance(item, DurableEventRecorded)
            and isinstance(item.event, TransactionEnded)
            and item.event.transaction.kind == "compaction"
        ]
        assert transaction_ends
        assert isinstance(transaction_ends[-1].outcome, TransactionAborted)
        assert transaction_ends[-1].outcome.reason == "cancelled"
        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len(failures) == 1
        assert failures[0].reason == "automatic"
        assert failures[0].automatic is True
        assert failures[0].message == "Compaction cancelled."
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_cancelled_context_overflow_compaction_preserves_history_and_aborts_transaction(
    temp_data_dir, temp_workspace
):
    class OverflowDuringSummaryMock(MockLLM):
        def __init__(self):
            super().__init__(responses=[
                {"content": "A title"},
                {"content": "First answer"},
                {"content": "Second answer"},
            ])
            self.probe = {
                "agent_requests": 0,
                "block_summary": False,
                "summary_started": asyncio.Event(),
            }

        async def _astream_once(self, request):
            if request.tools:
                self.probe["agent_requests"] += 1
                if self.probe["agent_requests"] == 3:
                    yield ModelFailed(error=ProviderError(
                        code="context_length_exceeded",
                        message="The request exceeds the model context window.",
                        retryable=False,
                        category="context_overflow",
                    ))
                    return
            elif self.probe["block_summary"]:
                self.probe["summary_started"].set()
                await asyncio.Event().wait()
            async for event in super()._astream_once(request):
                yield event

    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = OverflowDuringSummaryMock()
    plugin_overrides = [
        {
            "id": "llm",
            "config": {
                "default_provider": "test",
                "providers": {
                    "test": {
                        "protocol": "mock",
                        "default_model": "mock",
                        "models": [{
                            "model": "mock",
                            "max_context_tokens": 65536,
                            "max_output_tokens": 16,
                        }],
                    },
                },
            },
        },
        {
            "id": "compact",
            "config": {
                "automatic": True,
                "trigger_ratio": 1.0,
                "output_reservation": 0,
                "keep_recent_turns": 1,
            },
        },
    ]
    application = await start_application(
        paths=paths,
        session_id="compact-overflow-cancelled-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=plugin_overrides,
    )
    runtime_events = []

    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Old context " + "x" * 150)
        await _run_turn(application.engine, "New context " + "y" * 150)
        provider.probe["block_summary"] = True

        turn = asyncio.create_task(_run_turn(
            application.engine, "Cancel context-overflow recovery"
        ))
        await asyncio.wait_for(
            provider.probe["summary_started"].wait(), timeout=2
        )
        live_before_cancel = tuple(application.engine.messages)
        durable_before_cancel = (
            application.thread_persistence.history.load_surface()
        )

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert tuple(application.engine.messages) == live_before_cancel
        assert (
            application.thread_persistence.history.load_surface()
            == durable_before_cancel
        )
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
        trajectory_items = application.thread_persistence.history.page_trajectory(
            limit=100
        ).page.items
        transaction_ends = [
            item.event
            for item in trajectory_items
            if isinstance(item, DurableEventRecorded)
            and isinstance(item.event, TransactionEnded)
            and item.event.transaction.kind == "compaction"
        ]
        assert transaction_ends
        assert isinstance(transaction_ends[-1].outcome, TransactionAborted)
        assert transaction_ends[-1].outcome.reason == "cancelled"
        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len(failures) == 1
        assert failures[0].reason == "context-overflow"
        assert failures[0].automatic is True
        assert failures[0].message == "Compaction cancelled."
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_compact_listener_rejection_aborts_before_live_or_durable_commit(
    temp_data_dir, temp_workspace, tmp_path
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
        {"content": "This proposal should be rejected."},
    ])
    gate = tmp_path / "compact_gate"
    gate.mkdir()
    listener_marker = tmp_path / "compact-listener-called"
    (gate / "__init__.py").write_text(
        "from XBotv2.compact.events import PRE_COMPACT\n"
        "from pathlib import Path\n"
        "\n"
        "class CompactGate:\n"
        "    name = 'compact-gate'\n"
        "    def apply(self, ctx, _config):\n"
        "        async def reject(_event):\n"
        f"            Path({str(listener_marker)!r}).write_text('called')\n"
        "            return 'policy denied compaction'\n"
        "        ctx.on(PRE_COMPACT, reject, global_=True)\n"
        "\n"
        "plugin = CompactGate()\n",
        encoding="utf-8",
    )
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-rejected-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[tmp_path],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    try:
        await _run_turn(application.engine, "Research X")
        await _run_turn(application.engine, "Remember decision Y")
        before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()

        result = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )

        assert result.status == "error"
        assert listener_marker.read_text(encoding="utf-8") == "called"
        assert "rejected before commit" in result.message
        assert tuple(application.engine.messages) == before
        assert application.thread_persistence.history.load_surface() == durable_before
        assert application.thread_persistence.history.open_transactions(
            "compaction"
        ) == frozenset()
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_automatic_compaction_below_threshold_leaves_history_unchanged(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-below-threshold-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {
                "automatic": True,
                "trigger_ratio": 1.0,
                "output_reservation": 0,
                "keep_recent_turns": 1,
            },
        }],
    )
    runtime_events = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "A short request")

        assert not any(
            isinstance(item, CompactionSummaryMessage)
            for item in application.engine.messages
        )
        assert not any(
            isinstance(event, (CompactionCompleted, CompactionFailed))
            for event in runtime_events
        )
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
        assert provider.call_count == 2
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_automatic_compaction_uses_final_request_budget_not_history_only(
    temp_data_dir,
    temp_workspace,
):
    (Path(temp_workspace) / "AGENTS.md").write_text(
        "Follow this detailed workspace policy. " * 500,
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {"content": "first answer"},
        {"content": "summary of the first turn"},
        {"content": "second answer"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-built-request-budget",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "test",
                    "providers": {
                        "test": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_context_tokens": 4096,
                                "max_output_tokens": 1024,
                            }],
                        },
                    },
                },
            },
            {
                "id": "caption",
                "config": {"auto": False, "allow_access": False},
            },
            {
                "id": "compact",
                "config": {
                    "automatic": True,
                    "trigger_ratio": 0.1,
                    "output_reservation": 64,
                    "keep_recent_turns": 1,
                },
            },
        ],
    )
    ready_requests: list[ModelRequestReady] = []
    runtime_events = []
    try:
        application.on(
            Events.MODEL_REQUEST_READY,
            lambda event: ready_requests.append(event),
        )
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "old turn " * 100)
        model = application.loop_state.metadata.value.runtime_selection.model
        context_limit = context_token_limit(
            model.context_window,
            trigger_ratio=0.1,
            output_reservation=64,
        )
        first_request_tokens = estimate_model_request_tokens(
            ready_requests[-1].request,
        )
        assert first_request_tokens > context_limit

        next_input = _human(999, "new turn content")
        history_tokens = estimate_messages_tokens((
            *application.engine.messages,
            next_input,
        ))
        assert history_tokens < context_limit

        second_events = [event async for event in application.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="new turn content"),
            ),
        )]

        assert not [event for event in second_events if isinstance(event, LoopError)]
        compactions = [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]
        assert provider.call_count == 3
        assert len(compactions) == 1
        metrics = compactions[0].metrics
        assert compactions[0].reason == "automatic"
        assert metrics.estimate_source == "estimated_request"
        assert metrics.request_estimate == metrics.context_tokens_before
        assert metrics.context_tokens_before > context_limit
        assert metrics.context_tokens_after_estimate < metrics.context_tokens_before
        assert len(ready_requests) == 3
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_automatic_compaction_is_triggered_before_request_and_survives_resume(
    temp_data_dir, temp_workspace
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": "Second answer"},
        {"content": "Keep the key decision: use the typed runtime boundary."},
        {"content": "Keep the key decision: use the typed runtime boundary."},
        {"content": "Third answer"},
    ])
    plugin_overrides = [
        {
            "id": "llm",
            "config": {
                "default_provider": "test",
                "providers": {
                    "test": {
                        "protocol": "mock",
                        "default_model": "mock",
                        "models": [{
                            "model": "mock",
                            "max_context_tokens": 512,
                            "max_output_tokens": 16,
                        }],
                    },
                },
            },
        },
        {
            "id": "compact",
            "config": {
                "automatic": True,
                "trigger_ratio": 0.01,
                "output_reservation": 1,
                "keep_recent_turns": 1,
            },
        },
    ]
    application = await start_application(
        paths=paths,
        session_id="compact-automatic-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=plugin_overrides,
    )
    try:
        await _run_turn(application.engine, "Old context " + "x" * 150)
        await _run_turn(application.engine, "New context " + "y" * 150)
        await _run_turn(application.engine, "Continue with the next request")

        summaries = [
            item for item in application.engine.messages
            if isinstance(item, CompactionSummaryMessage)
        ]
        assert len(summaries) == 1
        assert "typed runtime boundary" in summaries[0].summary
        assert provider.call_count == 6
        assert any(
            isinstance(part, TextPart)
            and "typed runtime boundary" in part.text
            for message in provider.request_history[-1].messages
            for part in message.parts
        )
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
    finally:
        await application.destroy()

    resumed = await start_application(
        paths=paths,
        session_id="compact-automatic-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=MockLLM(responses=[]),
        extra_plugins=plugin_overrides,
    )
    try:
        summaries = [
            item for item in resumed.engine.messages
            if isinstance(item, CompactionSummaryMessage)
        ]
        assert len(summaries) == 1
        assert "typed runtime boundary" in summaries[0].summary
    finally:
        await resumed.destroy()


@pytest.mark.asyncio
async def test_automatic_summary_failure_preserves_the_full_live_and_durable_history(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {"content": ""},
        {"content": "Third answer after summary failure"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-automatic-failure-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "test",
                    "providers": {
                        "test": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_context_tokens": 512,
                                "max_output_tokens": 16,
                            }],
                        },
                    },
                },
            },
            {
                "id": "compact",
                "config": {
                    "automatic": True,
                    "trigger_ratio": 0.01,
                    "output_reservation": 1,
                    "keep_recent_turns": 1,
                },
            },
        ],
    )
    try:
        await _run_turn(application.engine, "Old context " + "x" * 150)
        before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()

        await _run_turn(application.engine, "Continue after summary failure")

        live = tuple(application.engine.messages)
        durable = application.thread_persistence.history.load_surface()
        assert len(live) == len(before) + 2
        assert live[:-2] == before
        assert durable[:-2] == durable_before
        assert isinstance(live[-2], HumanInputMessage)
        assert live[-2].parts[0].text == "Continue after summary failure"
        assert isinstance(live[-1], AssistantMessage)
        assert any(
            isinstance(part, TextPart)
            and part.text == "Third answer after summary failure"
            for part in live[-1].parts
        )
        assert [
            item for item in live if isinstance(item, CompactionSummaryMessage)
        ] == [item for item in before if isinstance(item, CompactionSummaryMessage)]
        assert provider.call_count == 4
    finally:
        await application.destroy()


@pytest.mark.parametrize("overflow_count", [1, 2])
@pytest.mark.asyncio
async def test_provider_context_overflow_recovery_retries_at_most_once(
    temp_data_dir, temp_workspace, overflow_count
):
    class OverflowMock(MockLLM):
        def __init__(self):
            super().__init__(responses=[
                {"content": "A title"},
                {"content": "First answer"},
                {"content": "Second answer"},
                {"content": "Retain the key decision: typed boundaries."},
                {"content": "Recovered after context overflow"},
            ])
            self.probe = {"agent_request_count": 0, "overflow_request": None}

        async def _astream_once(self, request):
            if request.tools:
                self.probe["agent_request_count"] += 1
                if 3 <= self.probe["agent_request_count"] < 3 + overflow_count:
                    self.probe["overflow_request"] = request
                    yield ModelFailed(error=ProviderError(
                        code="context_length_exceeded",
                        message="The request exceeds the model context window.",
                        retryable=False,
                        category="context_overflow",
                    ))
                    return
            async for event in super()._astream_once(request):
                yield event

    paths = RuntimePaths.from_data_dir(temp_data_dir)
    provider = OverflowMock()
    plugin_overrides = [
        {
            "id": "llm",
            "config": {
                "default_provider": "test",
                "providers": {
                    "test": {
                        "protocol": "mock",
                        "default_model": "mock",
                        "models": [{
                            "model": "mock",
                            "max_context_tokens": 65536,
                            "max_output_tokens": 16,
                        }],
                    },
                },
            },
        },
        {
            "id": "compact",
            "config": {
                "automatic": True,
                "trigger_ratio": 1.0,
                "output_reservation": 0,
                "keep_recent_turns": 1,
            },
        },
    ]
    application = await start_application(
        paths=paths,
        session_id="compact-context-overflow-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=plugin_overrides,
    )
    runtime_events = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Old context " + "x" * 150)
        await _run_turn(application.engine, "New context " + "y" * 150)
        if overflow_count == 1:
            await _run_turn(application.engine, "Recover this request after overflow")
        else:
            events = [
                event async for event in application.engine.run_turn(InboxItem(
                    target=InboxTarget.NEXT_TURN,
                    input=HumanInput(content="Propagate a repeated context overflow"),
                ))
            ]
            failures = [event for event in events if isinstance(event, LoopError)]
            assert len(failures) == 1
            assert failures[0].exception_type == "ProviderFailure"
            assert failures[0].message == (
                "The request exceeds the model context window."
            )

        summaries = [
            item for item in application.engine.messages
            if isinstance(item, CompactionSummaryMessage)
        ]
        assert len(summaries) == 1
        assert provider.probe["overflow_request"] is not None
        assert provider.probe["agent_request_count"] == 4
        if overflow_count == 1:
            assert any(
                isinstance(part, TextPart)
                and "typed boundaries" in part.text
                for message in provider.request_history[-1].messages
                for part in message.parts
            )
            assert any(
                isinstance(part, TextPart)
                and part.text == "Recovered after context overflow"
                for part in application.engine.messages[-1].parts
            )
        else:
            assert isinstance(application.engine.messages[-1], HumanInputMessage)
            assert application.engine.messages[-1].parts[0].text == (
                "Propagate a repeated context overflow"
            )
        completed = [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]
        assert len(completed) == 1
        assert completed[0].reason == "context-overflow"
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_automatic_summary_provider_failure_continues_with_original_request(
    temp_data_dir, temp_workspace
):
    class SummaryFailureMock(MockLLM):
        def __init__(self):
            super().__init__(responses=[
                {"content": "A title"},
                {"content": "First answer"},
                {"content": "Second answer"},
                {"content": "Original request continued"},
                {"content": "Original request continued after failure"},
            ])
            self.probe = {"fail_next_summary": False, "summary_failure": None}

        async def _astream_once(self, request):
            if not request.tools and self.probe["fail_next_summary"]:
                self.probe["fail_next_summary"] = False
                self.probe["summary_failure"] = request
                yield ModelFailed(error=ProviderError(
                    code="summary_unavailable",
                    message="The summary provider is unavailable.",
                    retryable=False,
                    category="transport",
                ))
                return
            async for event in super()._astream_once(request):
                yield event

    provider = SummaryFailureMock()
    runtime_events = []
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-summary-provider-failure-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "test",
                    "providers": {
                        "test": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_context_tokens": 512,
                                "max_output_tokens": 16,
                            }],
                        },
                    },
                },
            },
            {
                "id": "compact",
                "config": {
                    "automatic": True,
                    "trigger_ratio": 0.01,
                    "output_reservation": 1,
                    "keep_recent_turns": 1,
                },
            },
        ],
    )
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Old context " + "x" * 150)
        await _run_turn(application.engine, "New context " + "y" * 150)
        before = tuple(application.engine.messages)
        durable_before = application.thread_persistence.history.load_surface()
        completed_before = [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]
        provider.probe["fail_next_summary"] = True

        await _run_turn(application.engine, "Continue after provider failure")

        live = tuple(application.engine.messages)
        durable = application.thread_persistence.history.load_surface()
        assert provider.probe["summary_failure"] is not None
        assert live[:-2] == before
        assert durable[:-2] == durable_before
        assert isinstance(live[-2], HumanInputMessage)
        assert live[-2].parts[0].text == "Continue after provider failure"
        assert isinstance(live[-1], AssistantMessage)
        assert any(
            isinstance(part, TextPart)
            and part.text == "Original request continued after failure"
            for part in live[-1].parts
        )
        assert [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ] == completed_before
        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len(failures) == 1
        assert failures[0].reason == "automatic"
        assert application.thread_persistence.history.load_surface() == live
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_agent_compact_tool_runs_through_registry_and_continues_the_turn(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "A title"},
        {"content": "First answer"},
        {
            "tool_calls": [{
                "id": "compact-call",
                "name": "compact",
                "args": {},
            }],
        },
        {"content": "Preserve the decision made earlier."},
        {"content": "The task continued after compaction."},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-tool-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "compact",
            "config": {"automatic": False, "keep_recent_turns": 1},
        }],
    )
    try:
        assert "compact" in application.engine.tools.names()
        await _run_turn(application.engine, "First task " + "a" * 100)
        events = [event async for event in application.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Continue and retain the earlier decision."),
            ),
        )]

        completed = next(event for event in events if isinstance(event, ToolCompleted))
        assert isinstance(completed.execution.message.outcome, ToolSucceeded)
        assert any(
            isinstance(item, CompactionSummaryMessage)
            and "decision made earlier" in item.summary
            for item in application.engine.messages
        )
        retained_calls = {
            str(part.id)
            for item in application.engine.messages
            if isinstance(item, AssistantMessage)
            for part in item.parts
            if isinstance(part, ToolCall)
        }
        retained_results = {
            str(item.call.id)
            for item in application.engine.messages
            if isinstance(item, ToolMessage)
        }
        assert retained_calls & retained_results
        assert provider.call_count == 5
        assert any(
            isinstance(part, TextPart)
            and "task continued after compaction" in part.text
            for item in application.engine.messages
            if isinstance(item, AssistantMessage)
            for part in item.parts
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_compact_command_resolves_externalized_tool_artifacts(
    temp_data_dir, temp_workspace
):
    """A real externalized tool result must not break the summary request."""
    (Path(temp_workspace) / "notes.txt").write_text(
        "original tool output " * 20,
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {
            "tool_calls": [{
                "id": "read-notes",
                "name": "read",
                "args": {"path": "notes.txt"},
            }],
        },
        {"content": "First answer"},
        {"content": "The user inspected notes.txt and chose decision Y."},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-artifact-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
            {
                "id": "content_cache",
                "config": {
                    "threshold_chars": 10,
                    "preview_chars": 8,
                    "tail_chars": 3,
                },
            },
            {"id": "compact", "config": {"automatic": False, "keep_recent_turns": 1}},
        ],
    )
    runtime_events = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Read notes.txt")

        externalized = [
            item
            for item in application.engine.messages
            if isinstance(item, ToolMessage)
            and isinstance(item.outcome, ToolSucceeded)
            and item.outcome.output.artifacts
        ]
        assert externalized, "the tool result was not externalized into an artifact"

        result = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )

        assert result.status == "ok", result.message
        summaries = [
            item
            for item in application.engine.messages
            if isinstance(item, CompactionSummaryMessage)
        ]
        assert len(summaries) == 1
        assert "decision Y" in summaries[0].summary
        assert any(
            isinstance(event, CompactionCompleted) for event in runtime_events
        )

        # The auxiliary summary request resolved the logical ref to a real path.
        summary_requests = [
            request
            for request in provider.request_history
            if not request.tools
        ]
        assert summary_requests
        tool_parts = [
            part
            for request in summary_requests
            for message in request.messages
            if isinstance(message, ProviderTool)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assert any(
            application.artifacts.model_path(ref) in part.text
            for ref in externalized[0].outcome.output.artifacts
            for part in tool_parts
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_automatic_compaction_commits_over_artifact_bearing_history(
    temp_data_dir, temp_workspace
):
    """Automatic compaction must not silently skip artifact-bearing history."""
    (Path(temp_workspace) / "AGENTS.md").write_text(
        "Follow this detailed workspace policy. " * 500,
        encoding="utf-8",
    )
    (Path(temp_workspace) / "notes.txt").write_text(
        "original tool output " * 20,
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {
            "tool_calls": [{
                "id": "read-notes",
                "name": "read",
                "args": {"path": "notes.txt"},
            }],
        },
        {"content": "first answer"},
        {"content": "summary retaining decision Y"},
        {"content": "second answer"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-artifact-automatic-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "test",
                    "providers": {
                        "test": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_context_tokens": 4096,
                                "max_output_tokens": 1024,
                            }],
                        },
                    },
                },
            },
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
            {
                "id": "content_cache",
                "config": {
                    "threshold_chars": 10,
                    "preview_chars": 8,
                    "tail_chars": 3,
                },
            },
            {
                "id": "compact",
                "config": {
                    "automatic": True,
                    "trigger_ratio": 0.1,
                    "output_reservation": 64,
                    "keep_recent_turns": 1,
                },
            },
        ],
    )
    runtime_events = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        await _run_turn(application.engine, "Read notes.txt")

        externalized = [
            item
            for item in application.engine.messages
            if isinstance(item, ToolMessage)
            and isinstance(item.outcome, ToolSucceeded)
            and item.outcome.output.artifacts
        ]
        assert externalized, "the tool result was not externalized into an artifact"

        await _run_turn(application.engine, "Continue after reading notes")

        completions = [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]
        failures = [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert completions, f"automatic compaction did not commit: {failures}"
        assert completions[0].reason == "automatic"
        assert not failures
        assert any(
            isinstance(item, CompactionSummaryMessage)
            and "decision Y" in item.summary
            for item in application.engine.messages
        )
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_compaction_summary_keeps_the_model_output_budget(
    temp_data_dir, temp_workspace
):
    """A thinking model must keep its full output budget for the summary.

    The auxiliary request previously narrowed ``max_output_tokens`` to a small
    compaction cap. A reasoning model then spent that whole budget thinking and
    returned no text, so compaction failed with an empty summary.
    """
    provider = MockLLM(responses=[
        {"content": "First answer"},
        {"content": "Second answer"},
        # A reasoning-only reply: the summary text is empty, which must stay a
        # real failure rather than being silently accepted.
        {"reasoning": "thinking only, no summary text"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="compact-output-budget-e2e",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
            {"id": "compact", "config": {"automatic": False, "keep_recent_turns": 1}},
        ],
    )
    try:
        await _run_turn(application.engine, "First task " + "a" * 100)
        await _run_turn(application.engine, "Second task")

        result = await application.serial(
            EXECUTE_COMMAND.name,
            ExecuteCommand(command="compact", kind="server", raw_args=""),
        )

        # A reasoning-only reply is still rejected: compaction never accepts an
        # empty summary.
        assert result.status == "error"
        assert "empty summary" in result.message

        # The auxiliary request reuses the runtime output budget unchanged.
        summary_requests = [r for r in provider.request_history if not r.tools]
        assert summary_requests
        runtime_output = (
            application.loop_state.metadata.value
            .runtime_selection.model.generation.max_output_tokens
        )
        assert all(
            request.selection.generation.max_output_tokens == runtime_output
            for request in summary_requests
        )
    finally:
        await application.destroy()


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.asyncio
async def test_compaction_releases_context_and_the_session_continues(
    temp_data_dir, temp_workspace, automatic
):
    """Compaction over the threshold returns the session to low usage.

    Covers both operator paths over the same session shape: a manual /compact,
    and automatic compaction when the prepared request crosses the threshold.
    After either one the rebuilt request must be back under the threshold, so
    the following turns neither overflow nor compact again.
    """
    provider = MockLLM(responses=[
        {"content": "answer one"},
        {"content": "answer two"},
        {"content": "summary of the earlier work"},
        {"content": "answer three"},
        {"content": "answer four"},
        {"content": "answer five"},
        {"content": "answer six"},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id=f"compact-release-{'auto' if automatic else 'manual'}",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[
            {
                "id": "llm",
                "config": {
                    "default_provider": "test",
                    "providers": {
                        "test": {
                            "protocol": "mock",
                            "default_model": "mock",
                            "models": [{
                                "model": "mock",
                                "max_context_tokens": 65_536,
                                "max_output_tokens": 1024,
                            }],
                        },
                    },
                },
            },
            {"id": "caption", "config": {"auto": False, "allow_access": False}},
            {
                "id": "compact",
                "config": {
                    "automatic": automatic,
                    "trigger_ratio": 0.2,
                    "output_reservation": 64,
                    "keep_recent_turns": 1,
                },
            },
        ],
    )
    runtime_events = []
    ready_requests: list[ModelRequestReady] = []
    try:
        application.on(
            RUNTIME_EVENT,
            lambda event: runtime_events.append(event.event),
        )
        application.on(
            Events.MODEL_REQUEST_READY,
            lambda event: ready_requests.append(event),
        )
        model = application.loop_state.metadata.value.runtime_selection.model
        context_limit = context_token_limit(
            model.context_window,
            trigger_ratio=0.2,
            output_reservation=64,
        )

        # A large first turn plus a short second one push the request estimate
        # over the threshold, leaving a compactable prefix behind.
        await _run_turn(application.engine, "big " * 5_000)
        await _run_turn(application.engine, "short follow-up")

        if automatic:
            # The over-threshold request is compacted before it is sent, and
            # the engine rebuilds the candidate from the compacted surface.
            assert any(
                isinstance(event, CompactionCompleted)
                for event in runtime_events
            ), "automatic compaction did not run over the threshold"
        else:
            over = estimate_model_request_tokens(ready_requests[-1].request)
            assert over > context_limit
            result = await application.serial(
                EXECUTE_COMMAND.name,
                ExecuteCommand(command="compact", kind="server", raw_args=""),
            )
            assert result.status == "ok", result.message

        completions = [
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]
        assert len(completions) == 1
        assert completions[0].reason == ("automatic" if automatic else "manual")
        assert (
            completions[0].metrics.context_tokens_after_estimate
            < completions[0].metrics.context_tokens_before
        )
        assert any(
            isinstance(item, CompactionSummaryMessage)
            for item in application.engine.messages
        )

        # Continue the session: the rebuilt request is back under the threshold.
        await _run_turn(application.engine, "continue after compaction")
        after_estimate = estimate_model_request_tokens(ready_requests[-1].request)
        assert after_estimate < context_limit, (
            f"request after compaction is {after_estimate}, still over {context_limit}"
        )

        # Later turns run normally and never compact again.
        for text in ("question two", "question three", "question four"):
            events = [
                event async for event in application.engine.run_turn(
                    InboxItem(target=InboxTarget.NEXT_TURN, input=HumanInput(content=text)),
                )
            ]
            assert not [event for event in events if isinstance(event, LoopError)]

        assert not [
            event for event in runtime_events
            if isinstance(event, CompactionFailed)
        ]
        assert len([
            event for event in runtime_events
            if isinstance(event, CompactionCompleted)
        ]) == 1, "compaction ran more than once"
        assert application.thread_persistence.history.load_surface() == tuple(
            application.engine.messages
        )
    finally:
        await application.destroy()
