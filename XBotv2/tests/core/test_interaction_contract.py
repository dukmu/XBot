"""Interaction options become owned values before leaving the tool boundary."""

import pytest

from XBotv2.core.tools import ToolCall, ToolCancelled, ToolDenied, ToolSucceeded
from XBotv2.agentloop import Events
from XBotv2.agentloop.events import ReplaceToolCall
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools
from XBotv2.interactions import Answered, InteractionNotPending, UserInputOption
from XBotv2.interactions.interactions import InteractionWaiter
from XBotv2.interactions.tools import build_ask_user_tool
from XBotv2.core.tools import Tool


class _InteractionRecorder:
    def __init__(self) -> None:
        self.options: tuple[UserInputOption, ...] = ()

    async def request_user_input(
        self,
        question: str,
        *,
        options: tuple[UserInputOption, ...] = (),
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> Answered:
        assert question == "Continue?"
        assert source == "ask_user"
        assert tool_call_id == "call-1"
        self.options = options
        return Answered(answer="yes")


@pytest.mark.asyncio
async def test_ask_user_validates_raw_tool_options_into_owned_values() -> None:
    interactions = _InteractionRecorder()
    tool = build_ask_user_tool(interactions)  # type: ignore[arg-type]

    result = await tool.ainvoke(
        {
            "question": "Continue?",
            "options": [
                {"label": "yes", "description": "Continue"},
                {"label": "no", "description": "Stop"},
            ],
        },
        tool_call=ToolCall(id="call-1", name="ask_user"),
    )

    assert isinstance(result, ToolSucceeded)
    assert interactions.options == (
        UserInputOption(label="yes", description="Continue"),
        UserInputOption(label="no", description="Stop"),
    )


@pytest.mark.asyncio
async def test_ask_user_rejects_malformed_options_at_plugin_boundary() -> None:
    tool = build_ask_user_tool(_InteractionRecorder())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await tool.ainvoke(
            {
                "question": "Continue?",
                "options": [{"label": "yes"}, {"label": "no"}],
            },
            tool_call=ToolCall(id="call-1", name="ask_user"),
        )


@pytest.mark.asyncio
async def test_interaction_waiter_rejects_second_resolution_before_waiter_cleanup() -> None:
    waiter = InteractionWaiter(
        timed_out=lambda reason: Answered(answer=reason),
        cancelled=lambda reason: Answered(answer=reason),
    )
    pending = waiter.register("request-1")
    first = Answered(answer="continue")
    waiter.resolve("request-1", first)

    with pytest.raises(InteractionNotPending):
        waiter.resolve("request-1", Answered(answer="stop"))

    assert await waiter.wait_registered("request-1", pending, None) == first
    assert waiter.pending_request_ids() == []


@pytest.mark.asyncio
async def test_replaced_call_cannot_fall_back_to_original_tool_registration() -> None:
    invoked = False

    def original_tool() -> str:
        nonlocal invoked
        invoked = True
        return "original"

    class ReplacingEvents:
        async def serial(self, event: str, *_args: object) -> object | None:
            if event == Events.BEFORE_TOOL_CALL:
                return ReplaceToolCall(ToolCall(id="call-1", name="missing"))
            return None

        async def emit(self, _event: str, *_args: object) -> None:
            return None

    registry = ToolRegistry()
    registry.register(Tool.from_function(original_tool))
    executions = [
        execution
        async for execution in execute_tools(
            [ToolCall(id="call-1", name="original_tool")],
            registry,
            events=ReplacingEvents(),  # type: ignore[arg-type]
        )
    ]

    assert len(executions) == 1
    outcome = executions[0].message.outcome
    assert outcome.kind == "failed"
    assert outcome.error.code == "not_registered"
    assert not invoked


@pytest.mark.parametrize(
    "returned_outcome",
    [ToolDenied(reason="policy"), ToolCancelled(reason="cancelled")],
)
@pytest.mark.asyncio
async def test_tool_outcome_variants_are_not_reinterpreted_as_success(
    returned_outcome: ToolDenied | ToolCancelled,
) -> None:
    def tool() -> ToolDenied | ToolCancelled:
        return returned_outcome

    registry = ToolRegistry()
    registry.register(Tool.from_function(tool))
    executions = [
        execution
        async for execution in execute_tools(
            [ToolCall(id="call-1", name="tool")],
            registry,
        )
    ]

    assert len(executions) == 1
    assert executions[0].message.outcome == returned_outcome
