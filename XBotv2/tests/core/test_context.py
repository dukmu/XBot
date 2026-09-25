"""Context compilation is the sole conversation-to-provider projection."""

import pytest

from XBotv2.core.artifacts import ArtifactKind
from XBotv2.config.contracts import UserContext
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.context_builder.contracts import (
    BuiltContext,
    HistoryComponent,
    InlinePromptComponent,
)
from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationSettings,
    InputId,
    MessageId,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
    ToolCallId,
    ToolTiming,
)
from XBotv2.core.messages import HumanInputMessage, ToolMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderSystem, ProviderTool, ProviderUser
from XBotv2.core.tools import (
    ToolCallRef,
    ToolDenied,
    ToolSucceeded,
    text_output,
)
from XBotv2.core.variables import RuntimeVariables


def _selection() -> ResolvedRuntimeSelection:
    return ResolvedRuntimeSelection(
        agent_name="default",
        prompt="Follow the task.",
        limits=AgentExecutionLimits(),
        enabled_tools=(),
        model=ResolvedModelSelection(
            route=ModelRoute(provider="mock", model="test"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=128,
            ),
            context_window=4096,
        ),
    )


def _human(text="hello") -> HumanInputMessage:
    return HumanInputMessage(
        id=MessageId("message-1"),
        input_id=InputId("input-1"),
        parts=(TextPart(text=text),),
    )


def test_builder_orders_system_components_before_canonical_history():
    builder = ContextBuilder()
    builder.register_component("policy", InlinePromptComponent(
        stage="system_rules", source="policy", text="Never invent results.",
    ))

    messages = builder.build(
        history=(_human(),),
        runtime_selection=_selection(),
        user_identity=UserContext(user_id="u1", user_name="Alice"),
        memory="remember this",
        sandbox_summary="workspace only",
        runtime_paths=RuntimeVariables({"workspace": "/work"}),
        turn=1,
    )

    assert isinstance(messages[0], ProviderSystem)
    system = messages[0].parts[0].text
    assert system.index("core_instructions") < system.index("agent_instructions")
    assert system.index("agent_instructions") < system.index("policy")
    assert isinstance(messages[1], ProviderUser)
    assert messages[1].parts == (TextPart(text="hello"),)


def test_tool_outcome_projection_is_explicit_for_output_and_no_output_variants():
    call = ToolCallRef(id=ToolCallId("call-1"), name="probe")
    succeeded = ToolMessage(
        id=MessageId("tool-1"),
        call=call,
        outcome=ToolSucceeded(output=text_output("done")),
        timing=ToolTiming(duration_ms=1),
    )
    denied = ToolMessage(
        id=MessageId("tool-2"),
        call=call,
        outcome=ToolDenied(reason="policy"),
        timing=ToolTiming(duration_ms=1),
    )

    projected = ContextBuilder.messages_from_components(
        BuiltContext([
            HistoryComponent(succeeded),
            HistoryComponent(denied),
        ]),
    )

    assert all(isinstance(message, ProviderTool) for message in projected)
    assert projected[0].parts == (TextPart(text="done"),)
    assert projected[1].parts == (
        TextPart(text="Tool execution did not produce output"),
    )


def test_builder_rejects_negative_turn():
    with pytest.raises(ValueError, match="non-negative"):
        ContextBuilder().build(
            history=(),
            runtime_selection=_selection(),
            user_identity=UserContext(),
            memory="",
            sandbox_summary="",
            runtime_paths=RuntimeVariables(),
            turn=-1,
        )


def test_user_attachments_resolve_logical_ids_to_request_local_paths(artifact_store):
    ref = artifact_store.put(
        ArtifactKind.ATTACHMENT,
        b"original attachment",
        media_type="text/plain",
        name="notes.txt",
    )
    message = HumanInputMessage(
        id=MessageId("message-attachment"),
        input_id=InputId("input-attachment"),
        parts=(TextPart(text="inspect this"),),
        artifacts=(ref,),
    )

    request = ContextBuilder(artifacts=artifact_store).build(
        history=(message,),
        runtime_selection=_selection(),
        user_identity=UserContext(),
        memory="",
        sandbox_summary="",
        runtime_paths=RuntimeVariables(),
        turn=0,
    )
    user = next(item for item in request if isinstance(item, ProviderUser))
    prompt = "".join(part.text for part in user.parts if isinstance(part, TextPart))

    assert message.artifacts == (ref,)
    assert message.artifacts[0].id != artifact_store.model_path(ref)
    assert f'path="{artifact_store.model_path(ref)}"' in prompt
    assert 'name="notes.txt"' in prompt
    assert "Use filesystem or shell tools" in prompt
