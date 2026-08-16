"""Tests for message history persistence and session restore."""

import json
import base64

import pytest

from XBotv2.core.messages import Message
from XBotv2.persistence.store import (
    CoreStateStore,
    message_to_dict,
    dict_to_message,
)
from XBotv2.core.engine import Engine
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.config.models import RuntimeConfig
import xcore
from XBotv2.llm.mock import MockLLM
from XBotv2.tools.registry import ToolRegistry
from XBotv2.permissions.system import PermissionSystem
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.core.tools import Tool, ToolCall
from XBotv2.core.paths import RuntimePaths


# ------------------------------------------------------------------
# Message serialization
# ------------------------------------------------------------------

class TestMessageSerialization:
    """message_to_dict / dict_to_message round-trip."""

    def test_human_message_roundtrip(self):
        msg = Message(role="user", content="hello world")
        d = message_to_dict(msg)
        restored = dict_to_message(d)
        assert restored.role == "user"
        assert restored.content == "hello world"

    def test_ai_message_with_tool_calls_roundtrip(self):
        msg = Message(
            role="assistant",
            content="calling tool",
            tool_calls=[
                ToolCall("call_1", "shell", {"command": "ls"}),
            ],
        )
        d = message_to_dict(msg)
        assert d["parts"] == [
            {"type": "text", "text": "calling tool"},
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "shell",
                "args": {"command": "ls"},
            },
        ]
        assert "content" not in d and "tool_calls" not in d
        restored = dict_to_message(d)
        assert restored.role == "assistant"
        assert restored.tool_calls is not None
        assert len(restored.tool_calls) == 1
        assert restored.tool_calls[0].name == "shell"

    def test_ai_message_metadata_roundtrip(self):
        msg = Message(
            role="assistant",
            content="response text",
            name="assistant",
            additional_kwargs={"refusal": None, "provider_note": {"a": 1}},
            response_metadata={"model_name": "mock", "token_usage": {"total_tokens": 9}},
            usage_metadata={"input_tokens": 5, "output_tokens": 4},
        )
        d = message_to_dict(msg)
        restored = dict_to_message(d)
        assert restored.role == "assistant"
        assert restored.name == "assistant"
        assert restored.additional_kwargs["provider_note"] == {"a": 1}
        assert restored.response_metadata["token_usage"]["total_tokens"] == 9
        # usage_metadata must round-trip — without this, TUI
        # token totals reset to 0 on resume (see issue from
        # session 20260609-170727-7449).
        assert restored.usage_metadata == {"input_tokens": 5, "output_tokens": 4}
        assert d["usage_metadata"] == {"input_tokens": 5, "output_tokens": 4}

    def test_tool_message_roundtrip(self):
        msg = Message(role="tool", content="output", tool_call_id="call_1")
        d = message_to_dict(msg)
        restored = dict_to_message(d)
        assert restored.role == "tool"
        assert restored.content == "output"
        assert restored.tool_call_id == "call_1"

    def test_tool_message_metadata_roundtrip(self):
        msg = Message(
            role="tool",
            content="output",
            tool_call_id="call_1",
            name="filesystem_read",
            additional_kwargs={"visible": "kept"},
            response_metadata={"duration_ms": 5},
            data={"count": 1},
            error={"code": "failed"},
            client_events=[{"type": "client_message", "data": {}}],
            turn_complete=True,
        )
        d = message_to_dict(msg)
        restored = dict_to_message(d)
        assert restored.role == "tool"
        assert restored.name == "filesystem_read"
        assert restored.additional_kwargs == {"visible": "kept"}
        assert restored.data == {"count": 1}
        assert restored.error == {"code": "failed"}
        assert restored.client_events == []
        assert restored.turn_complete is False
        assert restored.response_metadata == {"duration_ms": 5}

    def test_multiline_content(self):
        msg = Message(role="user", content="line 1\nline 2\nline 3")
        d = message_to_dict(msg)
        assert d["parts"] == [{
            "type": "text",
            "text": "line 1\nline 2\nline 3",
        }]
        assert "content" not in d
        restored = dict_to_message(d)
        assert restored.content == "line 1\nline 2\nline 3"


# ------------------------------------------------------------------
# CoreStateStore message persistence
# ------------------------------------------------------------------

class TestMessagePersistence:
    """Messages stored in and restored from CoreStateStore."""

    @pytest.fixture
    def store(self, tmp_path):
        return CoreStateStore.create(
            RuntimePaths.from_data_dir(tmp_path).session("s1"),
            thread_id="t1",
            workspace_root="/workspace",
            provider="default",
        )

    def test_append_and_read_single_message(self, store):
        msg = Message(role="user", content="hello")
        store.append_messages([msg])
        assert store.message_count() == 1

        restored = store.read_messages()
        assert len(restored) == 1
        assert restored[0].content == "hello"

    def test_image_payload_is_stored_once_outside_the_journal(self, store):
        payload = b"small-image"
        image = store.store_image(
            base64.b64encode(payload).decode("ascii"),
            "image/png",
        )
        store.append_messages([Message(role="user", images=[image])])

        record = json.loads(store.messages_path.read_text(encoding="utf-8"))
        assert "small-image" not in store.messages_path.read_text(encoding="utf-8")
        assert record["parts"] == [{"type": "image", **image.to_dict()}]
        assert "images" not in record
        assert (store.root / image.path).read_bytes() == payload
        assert store.read_messages()[0].images == [image]

    def test_uploaded_attachment_is_stored_outside_the_journal(self, store):
        payload = b"binary\x00payload"
        attachment = store.store_attachment(
            base64.b64encode(payload).decode("ascii"),
            "application/octet-stream",
            "archive.bin",
        )
        store.append_messages([Message(role="user", artifact=[attachment])])

        assert (store.root / attachment["id"]).read_bytes() == payload
        assert "binary" not in store.messages_path.read_text(encoding="utf-8")
        assert store.read_messages()[0].artifact == [attachment]

    def test_append_multiple_messages(self, store):
        messages = [
            Message(role="user", content="first"),
            Message(role="assistant", content="response"),
            Message(role="user", content="second"),
            Message(role="assistant", content="done"),
        ]
        store.append_messages(messages)
        assert store.message_count() == 4

        restored = store.read_messages()
        assert len(restored) == 4
        assert restored[0].content == "first"
        assert restored[3].content == "done"

    def test_sync_messages_preserves_existing_message_ids(self, store):
        messages = [
            Message(role="user", content="first"),
            Message(role="assistant", content="response"),
        ]
        store.append_messages(messages)
        before = _raw_messages(store)

        messages.append(Message(role="user", content="second"))
        count = store.sync_messages(messages)
        messages.append(Message(role="assistant", content="done"))
        store.sync_messages(messages)

        after = _raw_messages(store)
        assert count == 3
        assert after[0]["msg_id"] == before[0]["msg_id"]
        assert after[0]["ts"] == before[0]["ts"]
        assert after[1]["msg_id"] == before[1]["msg_id"]
        assert after[1]["ts"] == before[1]["ts"]
        assert after[2]["msg_id"] == 3
        assert after[3]["msg_id"] == 4
        assert all("record_type" not in record for record in after)

    def test_history_operations_append_without_removing_prior_records(self, store):
        store.append_messages([
            Message(role="user", content="first"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="second"),
            Message(role="assistant", content="answer two"),
        ])
        original = store.messages_path.read_text(encoding="utf-8")

        store.append_undo(1)
        after_undo = store.messages_path.read_text(encoding="utf-8")
        store.append_clear()
        after_clear = store.messages_path.read_text(encoding="utf-8")

        assert after_undo.startswith(original)
        assert after_clear.startswith(after_undo)
        assert [message.content for message in store.read_messages()] == []
        records = _raw_messages(store)
        assert records[-2]["record_type"] == "history_undo"
        assert records[-1]["record_type"] == "history_clear"

    def test_replay_starts_from_last_checkpoint_then_applies_undo(self, store):
        store.append_messages([
            Message(role="user", content="discarded raw input"),
            Message(role="assistant", content="discarded raw answer"),
        ])
        checkpoint = [
            Message(role="system", content="summary"),
            Message(role="user", content="kept turn"),
            Message(role="assistant", content="kept answer"),
            Message(role="user", content="undo turn"),
            Message(role="assistant", content="undo answer"),
        ]
        store.append_checkpoint(checkpoint, reason="compact:automatic")
        store.append_undo(1)

        restored = store.read_messages()

        assert [message.content for message in restored] == [
            "summary", "kept turn", "kept answer",
        ]
        assert "discarded raw input" in store.messages_path.read_text(
            encoding="utf-8"
        )

    def test_replay_ignores_only_an_incomplete_trailing_record(self, store):
        store.append_messages([Message(role="user", content="durable")])
        with store.messages_path.open("a", encoding="utf-8") as stream:
            stream.write('{"record_type":"history_checkpoint"')

        assert [message.content for message in store.read_messages()] == ["durable"]

        store.append_messages([Message(role="assistant", content="continued")])

        assert [message.content for message in store.read_messages()] == [
            "durable", "continued",
        ]
        assert store.messages_path.read_bytes().endswith(b"\n")

    def test_message_ids_are_sequential(self, store):
        store.append_messages([
            Message(role="user", content="m1"),
            Message(role="user", content="m2"),
        ])
        assert [record["msg_id"] for record in _raw_messages(store)] == [1, 2]

    def test_has_existing_session(self, store):
        """Session detection works based on stored messages."""
        assert store.has_existing_session() is False

        store.append_messages([Message(role="user", content="hello")])
        assert store.has_existing_session() is True

    def test_persistence_survives_store_recreation(self, tmp_path):
        """Messages persist even after creating a new store instance."""
        paths = RuntimePaths.from_data_dir(tmp_path).session("s1")

        # First store — write messages
        store1 = CoreStateStore.create(
            paths,
            thread_id="t1",
            workspace_root="/workspace",
            provider="p",
        )
        store1.append_messages([
            Message(role="user", content="persistent"),
            Message(role="assistant", content="survives restart"),
        ])

        # Second store — read them back
        store2 = CoreStateStore(
            paths=paths,
            thread_id="t1",
            workspace_root="/workspace",
            provider="p",
        )
        assert store2.message_count() == 2
        restored = store2.read_messages()
        assert len(restored) == 2
        assert restored[0].content == "persistent"
        assert restored[1].content == "survives restart"


# ------------------------------------------------------------------
# Engine integration — save and restore
# ------------------------------------------------------------------

def echo(message: str) -> str:
    """Echo a message."""
    return f"Echo: {message}"

echo_tool = Tool.from_function(echo, name="echo")


def make_engine(llm, registry, store, workspace, plugin_ctx=None):
    return Engine(
        llm=llm,
        tool_registry=registry,
        plugin_ctx=plugin_ctx or xcore.Context(),
        state_store=store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(workspace)),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )


class TestEnginePersistence:
    """Engine saves messages after turns and restores on resume."""

    @pytest.mark.asyncio
    async def test_messages_persisted_after_turn(self, temp_data_dir, temp_workspace):
        """After run_turn, messages are on disk."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        llm = MockLLM(responses=[{"content": "Hello!"}])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, store, temp_workspace)
        await engine.start_session()

        _ = [e async for e in engine.run_turn("hi")]

        # Messages should be persisted
        assert store.message_count() > 0
        restored = store.read_messages()
        contents = [m.content for m in restored]
        assert "hi" in contents  # User message
        assert "Hello!" in contents  # AI response

    @pytest.mark.asyncio
    async def test_session_restores_messages(self, temp_data_dir, temp_workspace):
        """A new engine on the same store restores previous messages."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        llm = MockLLM(responses=[{"content": "First"}, {"content": "Second"}])
        registry = ToolRegistry()

        # First engine — run 2 turns
        engine1 = make_engine(llm, registry, store, temp_workspace)
        await engine1.start_session()
        _ = [e async for e in engine1.run_turn("turn 1")]
        _ = [e async for e in engine1.run_turn("turn 2")]

        msg_count = store.message_count()
        assert msg_count >= 4  # 2 user + 2 AI

        # Second engine — should restore all messages
        engine2 = make_engine(llm, registry, store, temp_workspace)
        await engine2.start_session()
        assert len(engine2.messages) == msg_count
        assert engine2.turn_count == 2

    @pytest.mark.asyncio
    async def test_restored_messages_are_sent_to_the_model(
        self, temp_data_dir, temp_workspace
    ):
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"),
            thread_id="t1",
            workspace_root="/workspace",
            provider="p",
        )
        first_llm = MockLLM(responses=[{"content": "remembered answer"}])
        engine1 = make_engine(first_llm, ToolRegistry(), store, temp_workspace)
        await engine1.start_session()
        _ = [event async for event in engine1.run_turn("remembered question")]
        await engine1.close_session()

        resumed_llm = MockLLM(responses=[{"content": "resumed"}])
        engine2 = make_engine(resumed_llm, ToolRegistry(), store, temp_workspace)
        await engine2.start_session()
        _ = [event async for event in engine2.run_turn("what came before?")]

        request = resumed_llm.get_call_messages(0)
        history = [(message.role, message.content) for message in request]
        assert ("user", "remembered question") in history
        assert ("assistant", "remembered answer") in history
        assert ("user", "what came before?") in history

    @pytest.mark.asyncio
    async def test_engine_save_preserves_existing_message_ids(self, temp_data_dir, temp_workspace):
        """Repeated turn saves do not churn ids for unchanged history messages."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        llm = MockLLM(responses=[{"content": "First"}, {"content": "Second"}])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, store, temp_workspace)
        await engine.start_session()
        _ = [e async for e in engine.run_turn("turn 1")]
        first_save = _raw_messages(store)

        _ = [e async for e in engine.run_turn("turn 2")]
        second_save = _raw_messages(store)

        assert second_save[0]["parts"] == first_save[0]["parts"]
        assert second_save[0]["msg_id"] == first_save[0]["msg_id"]
        assert second_save[0]["ts"] == first_save[0]["ts"]
        assert second_save[1]["parts"] == first_save[1]["parts"]
        assert second_save[1]["msg_id"] == first_save[1]["msg_id"]
        assert second_save[1]["ts"] == first_save[1]["ts"]

    @pytest.mark.asyncio
    async def test_resume_session_explicit(self, temp_data_dir, temp_workspace):
        """Explicit resume_session loads messages and turn count."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        llm = MockLLM(responses=[{"content": "Before resume"}])
        registry = ToolRegistry()

        engine1 = make_engine(llm, registry, store, temp_workspace)
        await engine1.start_session()
        _ = [e async for e in engine1.run_turn("before")]
        await engine1.close_session()

        # New engine — explicit resume
        engine2 = make_engine(llm, registry, store, temp_workspace)
        await engine2.resume_session()
        assert engine2.turn_count == 1
        restored = engine2.messages
        contents = [m.content for m in restored]
        assert "before" in contents

    @pytest.mark.asyncio
    async def test_tool_call_messages_persist(self, temp_data_dir, temp_workspace):
        """Messages with tool calls round-trip through persistence."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("s1"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        llm = MockLLM(responses=[
            {
                "content": "Calling echo",
                "tool_calls": [{"name": "echo", "args": {"message": "test"}, "id": "call_1"}],
            },
            {"content": "Done after tool."},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")

        engine = make_engine(llm, registry, store, temp_workspace)
        await engine.start_session()
        _ = [e async for e in engine.run_turn("use echo")]

        # All messages should be on disk
        restored = store.read_messages()
        roles = [m.role for m in restored]
        assert "user" in roles

        # Verify tool call detail preserved
        model_msgs = [m for m in restored if m.tool_calls]
        assert len(model_msgs) >= 1
        assert model_msgs[0].tool_calls[0].name == "echo"

    @pytest.mark.asyncio
    async def test_fresh_session_has_no_messages(self, temp_data_dir, temp_workspace):
        """A brand-new session starts with zero messages."""
        store = CoreStateStore.create(
            RuntimePaths.from_data_dir(temp_data_dir).session("fresh"), thread_id="t1", workspace_root="/workspace", provider="p",
        )
        assert store.message_count() == 0
        assert store.has_existing_session() is False

        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        engine = make_engine(llm, registry, store, temp_workspace)
        await engine.start_session()
        assert len(engine.messages) == 0

def _raw_messages(store: CoreStateStore) -> list[dict]:
    if not store.messages_path.exists():
        return []
    return [
        json.loads(line)
        for line in store.messages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
