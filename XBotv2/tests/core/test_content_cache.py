"""Provider-boundary tests for oversized current user input caching."""

import xml.etree.ElementTree as ET

import pytest
import xcore

from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.config.models import RuntimeConfig
from XBotv2.content_cache.content_cache import (
    DEFAULT_CACHE_THRESHOLD_CHARS,
    cache_user_message,
)
from XBotv2.content_cache.plugin import (
    ContentCacheComponent,
    ContentCacheService,
)
from XBotv2.content_cache.config import (
    ContentCacheConfig,
    parse_content_cache_config,
)
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.messages import Message
from XBotv2.core.tokens import REQUEST_ESTIMATE_KEY, estimate_request_tokens
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.system import PermissionSystem
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.tests.helpers import make_engine
from XBotv2.token_manager.plugin import TokenManagerPlugin


class _CountingArtifacts:
    def __init__(self, delegate):
        self._delegate = delegate
        self.put_calls = 0

    def put(self, *args, **kwargs):
        self.put_calls += 1
        return self._delegate.put(*args, **kwargs)

    def read(self, artifact):
        return self._delegate.read(artifact)

    def exists(self, artifact):
        return self._delegate.exists(artifact)

    def model_path(self, artifact):
        return self._delegate.model_path(artifact)


def test_content_cache_config_controls_threshold_and_preview(artifact_store):
    config = parse_content_cache_config({
        "cache_threshold_chars": 20,
        "preview_chars": 12,
        "tail_chars": 4,
    })
    service = ContentCacheService(artifact_store, config)

    bounded = service.bind_current_user_message([
        Message(role="user", content="abcdefghijklmnopqrstuvwxyz"),
    ])[0]

    root = ET.fromstring(bounded.content)
    assert root.attrib["cache_threshold_chars"] == "20"
    assert root.attrib["inline_limit_chars"] == "12"
    assert root.findtext("preview/beginning") == "\nabcdefgh\n"
    assert root.findtext("preview/ending") == "\nwxyz\n"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"cache_threshold_chars": 0}, "cache_threshold_chars"),
        (
            {"cache_threshold_chars": 10, "preview_chars": 11},
            "preview_chars",
        ),
        ({"preview_chars": 10, "tail_chars": 11}, "tail_chars"),
    ],
)
def test_content_cache_config_rejects_invalid_sizes(config, message):
    with pytest.raises(ValueError, match=message):
        parse_content_cache_config(config)


def test_cache_user_message_keeps_original_and_explains_relative_path(
    artifact_store,
):
    content = "begin:" + "x" * DEFAULT_CACHE_THRESHOLD_CHARS + ":end"
    source = Message(role="user", content=content)

    bounded, artifact = cache_user_message(source, artifact_store)

    assert artifact is not None
    assert bounded is not source
    assert source.content == content
    root = ET.fromstring(bounded.content)
    assert root.attrib == {
        "cache_threshold_chars": str(DEFAULT_CACHE_THRESHOLD_CHARS),
        "inline_limit_chars": "12000",
        "kind": "user_input",
        "omitted_chars": str(len(content) - 12000),
        "original_chars": str(len(content)),
    }
    assert root.findtext("cache_path").strip().startswith(
        "session/artifacts/context/"
    )
    instruction = root.findtext("read_instruction")
    assert "Pass it unchanged" in instruction
    assert "absolute filesystem path" in instruction
    assert artifact_store.read(artifact).decode() == content


def test_only_current_user_message_is_considered(state_store, artifact_store):
    oversized = "x" * (DEFAULT_CACHE_THRESHOLD_CHARS + 1)
    messages = [
        Message(role="user", content=oversized),
        Message(role="assistant", content=oversized),
        Message(role="tool", content=oversized),
        Message(role="user", content="current"),
    ]

    bounded = ContentCacheService(
        artifact_store, ContentCacheConfig()
    ).bind_current_user_message(messages)

    assert bounded is messages
    assert all(message.content in {oversized, "current"} for message in messages)
    assert not state_store.paths.artifact_dir(ArtifactKind.CONTEXT).exists()


def test_react_requests_reuse_one_cached_current_message(
    state_store, artifact_store
):
    artifacts = _CountingArtifacts(artifact_store)
    service = ContentCacheService(artifacts, ContentCacheConfig())
    previous = Message(
        role="user", content="p" * (DEFAULT_CACHE_THRESHOLD_CHARS + 1)
    )
    current = Message(
        role="user", content="c" * (DEFAULT_CACHE_THRESHOLD_CHARS + 1)
    )
    messages = [
        previous,
        Message(
            role="assistant",
            content="a" * (DEFAULT_CACHE_THRESHOLD_CHARS + 1),
        ),
        Message(
            role="tool",
            content="t" * (DEFAULT_CACHE_THRESHOLD_CHARS + 1),
        ),
        current,
    ]

    first = service.bind_current_user_message(messages)
    second = service.bind_current_user_message(messages)

    assert artifacts.put_calls == 1
    assert first[-1] is second[-1]
    assert first[:-1] == messages[:-1]
    assert messages[-1] is current
    cached_files = list(
        state_store.paths.artifact_dir(ArtifactKind.CONTEXT).glob("*.txt")
    )
    assert len(cached_files) == 1
    assert cached_files[0].read_text() == current.content


@pytest.mark.asyncio
async def test_engine_caches_provider_copy_once_without_mutating_history(
    state_store,
    artifact_store,
    temp_workspace,
):
    user_input = "request:" + "z" * DEFAULT_CACHE_THRESHOLD_CHARS
    llm = MockLLM(responses=[{"content": "done"}])
    engine = make_engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        plugin_ctx=xcore.Context(),
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    engine._events.set("artifacts", artifact_store)
    ContentCacheComponent().apply(engine._events, None)
    token_manager = TokenManagerPlugin()
    token_manager.apply(engine._events)

    _ = [event async for event in engine.run_turn(user_input)]

    provider_user = next(
        message for message in llm.get_call_messages(0) if message.role == "user"
    )
    assert ET.fromstring(provider_user.content).attrib["kind"] == "user_input"
    assert engine.messages[0].content == user_input
    assert state_store.history.load()[0].content == user_input
    provider_messages = llm.get_call_messages(0)
    provider_estimate = estimate_request_tokens(provider_messages)
    assert token_manager.diagnostics()["latest_request"]["raw_estimate"] == (
        provider_estimate
    )
    assistant = next(message for message in engine.messages if message.role == "assistant")
    assert assistant.response_metadata[REQUEST_ESTIMATE_KEY] == provider_estimate


@pytest.mark.asyncio
async def test_engine_leaves_user_input_below_threshold_inline(
    state_store,
    artifact_store,
    temp_workspace,
):
    user_input = "request:" + "z" * 12_000
    llm = MockLLM(responses=[{"content": "done"}])
    engine = make_engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        plugin_ctx=xcore.Context(),
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    engine._events.set("artifacts", artifact_store)
    ContentCacheComponent().apply(engine._events, None)

    _ = [event async for event in engine.run_turn(user_input)]

    provider_user = next(
        message for message in llm.get_call_messages(0) if message.role == "user"
    )
    assert provider_user.content == user_input
    assert not state_store.paths.artifact_dir(ArtifactKind.CONTEXT).exists()
