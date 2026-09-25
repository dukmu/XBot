"""Caption requests are auxiliary projections of canonical human input."""

import asyncio

import xcore
import pytest

from XBotv2.agentloop.events import BeforeContextBuild
from XBotv2.agentloop.protocol import LoopError
from XBotv2.caption import CaptionConfig, CaptionRequest, CaptionResult
from XBotv2.caption.service import (
    CaptionService,
    _clean_title,
    _fallback_title,
    caption_request,
)
from XBotv2.context_builder.events import ContextBuildRequest
from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationSettings,
    MeasurementUnavailable,
    ModelRoute,
    ProviderError,
    ProviderExtensions,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
    TokenCounters,
    UsageDelta,
)
from XBotv2.core.domain import InputId, MessageId, NoticeId
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.core.messages import HumanInputMessage, RuntimeNoticeMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderSystem, ProviderUser
from XBotv2.core.stream import ModelFailed
from XBotv2.llm import ModelPort
from XBotv2.llm.mock import MockLLM


def _human(text: str, index: int = 1) -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId(f"message-{index}"),
        input_id=InputId(f"input-{index}"),
        parts=(TextPart(text=text),),
    )


def test_caption_request_uses_human_input_and_ignores_runtime_notices():
    notice = RuntimeNoticeMessage(
        id=MessageId("notice-message"),
        notice_id=NoticeId("notice-1"),
        source="job",
        event="completed",
        parts=(TextPart(text="background noise"),),
    )
    request = caption_request(
        CaptionRequest(
            messages=(_human("billing migration"), notice),
            current_title="session-1",
        ),
        max_chars=80,
    )

    assert isinstance(request[0], ProviderSystem)
    assert isinstance(request[1], ProviderUser)
    assert "billing migration" in request[1].parts[0].text
    assert "session-1" in request[1].parts[0].text
    assert "background noise" not in request[1].parts[0].text


def test_caption_request_bounds_large_input_without_mutating_message():
    content = "x" * 3000
    message = _human(content)
    request = caption_request(
        CaptionRequest(messages=(message,), current_title="session-1"),
        max_chars=80,
    )
    assert len(request[1].parts[0].text) < len(content)
    assert message.parts[0].text == content


def test_title_cleaning_and_fallback_are_deterministic():
    assert _clean_title('  "Billing   plan"  ', 80) == "Billing plan"
    assert _clean_title("abcdefgh", 5) == "abcd…"
    assert _fallback_title(
        CaptionRequest(
            messages=(_human("  first   message  "),),
            current_title="session-1",
        ),
        80,
    ) == "first message"
    assert _fallback_title(
        CaptionRequest(messages=(), current_title="session-1"), 80
    ) == ""


def test_caption_request_and_result_are_typed_plugin_contracts():
    request = CaptionRequest(
        messages=(_human("billing migration"),),
        current_title="session-1",
    )
    assert request.messages == (_human("billing migration"),)
    assert request.current_title == "session-1"

    result = CaptionResult(title="Billing plan")
    assert result.model_dump(mode="json") == {"title": "Billing plan"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("automatic_response", "automatic_title"),
    [
        ({"content": "Automatic title"}, "Automatic title"),
        ({"content": ""}, "Review the session title."),
    ],
)
async def test_caption_agent_tool_uses_typed_results_in_the_standard_agent_loop(
    temp_data_dir,
    temp_workspace,
    automatic_response,
    automatic_title,
):
    from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
    from XBotv2.application.app import start_application
    from XBotv2.core.messages import ToolMessage
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.permissions.contracts import PermissionPolicy

    llm = MockLLM(responses=[
        automatic_response,
        {"tool_calls": [
            {"id": "caption-get", "name": "caption", "args": {"action": "get"}},
            {
                "id": "caption-set",
                "name": "caption",
                "args": {"action": "set", "title": "Reviewed title"},
            },
        ]},
        {"content": "The title is updated."},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="caption-tool-contract",
        thread_id="main",
        workspace_root=temp_workspace,
        llm_override=llm,
    )
    application.permissions.replace_policies((PermissionPolicy(
        default_decision="allow",
    ),))

    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content="Review the session title."),
    )
    try:
        events = [event async for event in application.engine.run_turn(item)]
        messages = application.loop_state.history.snapshot()
        title = application.loop_state.metadata.value.title
    finally:
        await application.stop()

    assert not [event for event in events if isinstance(event, LoopError)]
    caption_results = [
        "".join(part.text for part in message.outcome.output.parts if isinstance(part, TextPart))
        for message in messages
        if isinstance(message, ToolMessage) and message.call.name == "caption"
    ]
    assert caption_results == [
        f"Session title: '{automatic_title}'",
        "Session title set to 'Reviewed title'.",
    ]
    assert title == "Reviewed title"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto", "allow_access"),
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_caption_auto_and_tool_access_are_independent_application_options(
    temp_data_dir,
    temp_workspace,
    auto,
    allow_access,
):
    from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
    from XBotv2.application.app import start_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.permissions.contracts import PermissionPolicy

    responses = []
    if auto:
        responses.append({"content": "Automatically titled"})
    if allow_access:
        responses.append({
            "tool_calls": [{
                "id": "caption-read",
                "name": "caption",
                "args": {"action": "get"},
            }],
        })
    responses.append({"content": "Answer."})
    llm = MockLLM(responses=responses)
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="caption-config-matrix",
        thread_id="main",
        workspace_root=temp_workspace,
        llm_override=llm,
        extra_plugins=[{
            "id": "caption",
            "config": {"auto": auto, "allow_access": allow_access},
        }],
    )
    application.permissions.replace_policies((PermissionPolicy(
        default_decision="allow",
    ),))

    item = InboxItem(
        target=InboxTarget.NEXT_TURN,
        input=HumanInput(content="Explain the option matrix."),
    )
    try:
        events = [event async for event in application.engine.run_turn(item)]
        title = application.loop_state.metadata.value.title
        requests = llm.request_history
    finally:
        await application.stop()

    assert not [event for event in events if isinstance(event, LoopError)]
    assert title == (
        "Automatically titled" if auto else "caption-config-matrix"
    )
    assert len(requests) == 1 + int(auto) + int(allow_access)
    exposed_tools = {
        tool.name for request in requests for tool in request.tools
    }
    assert ("caption" in exposed_tools) is allow_access


@pytest.mark.asyncio
async def test_caption_agent_tool_is_not_registered_for_a_child_application(
    temp_data_dir,
    temp_workspace,
):
    from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
    from XBotv2.application.app import start_application
    from XBotv2.core.paths import RuntimePaths

    llm = MockLLM(responses=[{"content": "Child response."}])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="caption-child-contract",
        thread_id="worker",
        workspace_root=temp_workspace,
        is_subagent=True,
        parent_thread_id="main",
        llm_override=llm,
        extra_plugins=[{
            "id": "caption",
            "config": {"auto": True, "allow_access": True},
        }],
    )
    try:
        events = [event async for event in application.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Inspect this child task."),
        ))]
        request = llm.request_history[0]
    finally:
        await application.stop()

    assert not [event for event in events if isinstance(event, LoopError)]
    assert "caption" not in {tool.name for tool in request.tools}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_title", ["", "''", '\" \n\''])
async def test_caption_tool_rejects_titles_that_normalize_to_empty(
    temp_data_dir,
    temp_workspace,
    invalid_title,
):
    from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
    from XBotv2.application.app import start_application
    from XBotv2.core.messages import ToolMessage
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.core.tools import ToolFailed
    from XBotv2.permissions.contracts import PermissionPolicy

    llm = MockLLM(responses=[
        {"content": "Automatic title"},
        {"tool_calls": [{
            "id": "caption-empty-after-cleaning",
            "name": "caption",
            "args": {"action": "set", "title": invalid_title},
        }]},
        {"content": "The title could not be changed."},
    ])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="caption-empty-normalized",
        thread_id="main",
        workspace_root=temp_workspace,
        llm_override=llm,
    )
    application.permissions.replace_policies((PermissionPolicy(
        default_decision="allow",
    ),))
    try:
        events = [event async for event in application.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Please rename this session."),
        ))]
        messages = application.loop_state.history.snapshot()
        title = application.loop_state.metadata.value.title
    finally:
        await application.stop()

    caption_outcome = next(
        message.outcome
        for message in messages
        if isinstance(message, ToolMessage) and message.call.name == "caption"
    )
    assert not [event for event in events if isinstance(event, LoopError)]
    assert isinstance(caption_outcome, ToolFailed)
    assert caption_outcome.error.code == "caption_empty_title"
    assert title == "Automatic title"


def test_auto_caption_retries_after_provider_failure_on_a_later_turn():
    async def scenario() -> None:
        selection = ResolvedRuntimeSelection(
            agent_name="default",
            prompt="",
            limits=AgentExecutionLimits(),
            enabled_tools=(),
            model=ResolvedModelSelection(
                route=ModelRoute(provider="test", model="test-model"),
                generation=GenerationSettings(
                    mode=StandardGenerationMode(),
                    max_output_tokens=256,
                ),
                context_window=4096,
            ),
        )
        events = xcore.Context()
        state = ThreadMetadataState(
            events,
            session_id="session-1",
            thread_id="main",
        )
        await state.initialize(
            ThreadMetadata(runtime_selection=selection)
        )

        class FlakyCaptionModel:
            def __init__(self) -> None:
                self.calls = 0
                self.success = MockLLM(responses=[{"content": "Billing plan"}])

            async def astream(self, request):
                self.calls += 1
                if self.calls == 1:
                    yield ModelFailed(
                        error=ProviderError(
                            code="temporary_unavailable",
                            message="caption endpoint unavailable",
                            retryable=True,
                            category="transport",
                        )
                    )
                    return
                async for event in self.success.astream(request):
                    yield event

        class UsageRecorder:
            async def record(self, *_args) -> None:
                return None

        model = FlakyCaptionModel()
        service = CaptionService(
            events=events,
            model=model,
            state=state,
            usage=UsageRecorder(),
            session_id="session-1",
            config=CaptionConfig(auto=True, allow_access=False),
        )

        def before_context(turn: int) -> BeforeContextBuild:
            request = ContextBuildRequest(
                history=(_human("Plan a billing migration"),),
                runtime_selection=selection,
                user_identity=None,
                memory="",
                sandbox_summary="",
                runtime_paths=None,
                turn=turn,
            )
            return BeforeContextBuild(request=request)

        await service._on_before_context(before_context(1))
        assert service.title == "session-1"
        await service._on_before_context(before_context(1))
        assert model.calls == 1
        await service._on_before_context(before_context(2))

        assert model.calls == 2
        assert service.title == "Billing plan"

        await state.replace_title("A user-selected title")
        untouched_model = FlakyCaptionModel()
        untouched_service = CaptionService(
            events=events,
            model=untouched_model,
            state=state,
            usage=UsageRecorder(),
            session_id="session-1",
            config=CaptionConfig(auto=True, allow_access=False),
        )
        await untouched_service._on_before_context(before_context(1))
        assert untouched_model.calls == 0
        assert untouched_service.title == "A user-selected title"

    asyncio.run(scenario())
