"""Provider-boundary tests for oversized context externalization."""

from pathlib import Path

import pytest

from xbotv2.api.messages import Message
from xbotv2.api.tools import ToolCall
from xbotv2.core.content_cache import MAX_INLINE_CHARS, bound_context_messages
from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.llm.mock import MockLLM
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


def test_externalizes_provider_copy_without_mutating_history(state_store):
    content = "begin:" + "a" * MAX_INLINE_CHARS + ":end"
    argument = "argument:" + "b" * MAX_INLINE_CHARS
    reasoning = "reasoning:" + "c" * MAX_INLINE_CHARS
    message = Message(
        role="assistant",
        content=content,
        tool_calls=[ToolCall("call-1", "echo", {"value": argument})],
        additional_kwargs={"reasoning_content": reasoning},
    )

    bounded = bound_context_messages([message], state_store)[0]

    assert bounded is not message
    assert "cache_path: session/artifacts/context/" in bounded.content
    assert "cache_path: session/artifacts/context/" in bounded.tool_calls[0].args["value"]
    assert "cache_path: session/artifacts/context/" in bounded.additional_kwargs["reasoning_content"]
    assert message.content == content
    assert message.tool_calls[0].args["value"] == argument
    assert message.additional_kwargs["reasoning_content"] == reasoning

    cached = sorted((Path(state_store.artifacts_dir) / "context").glob("*.txt"))
    assert {path.read_text(encoding="utf-8") for path in cached} == {
        content,
        argument,
        reasoning,
    }


def test_reuses_relative_content_cache_reference(state_store):
    content = "x" * (MAX_INLINE_CHARS + 1)

    first = bound_context_messages([Message(role="user", content=content)], state_store)
    second = bound_context_messages([Message(role="user", content=content)], state_store)

    assert first[0].content == second[0].content
    assert "cache_path: session/artifacts/context/" in first[0].content
    assert len(list((Path(state_store.artifacts_dir) / "context").glob("*.txt"))) == 1


@pytest.mark.asyncio
async def test_engine_bounds_user_input_only_at_provider_boundary(
    state_store,
    temp_workspace,
):
    user_input = "request:" + "z" * MAX_INLINE_CHARS
    llm = MockLLM(responses=[{"content": "done"}])
    engine = Engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        hook_manager=HookManager(),
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )

    events = [event async for event in engine.run_turn(user_input)]

    provider_user = next(
        message
        for message in llm.get_call_messages(0)
        if message.role == "user"
    )
    assert "cache_path: session/artifacts/context/" in provider_user.content
    assert engine.messages[0].content == user_input
    assert state_store.read_messages()[0].content == user_input
    assert any(event["type"] == "turn_finished" for event in events)
