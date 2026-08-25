"""Conversation history and strict thread persistence tests."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import ImageContent, Message
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ToolCall
from XBotv2.agentloop.inbox import InboxInput, InboxTarget
from XBotv2.persistence.models import (
    MessageRecord,
    ThreadLifecycleRecord,
    ThreadMetadata,
)
from XBotv2.persistence.store import ThreadPersistence


def thread_persistence(tmp_path, session_id="s1"):
    return ThreadPersistence.create(
        RuntimePaths.from_data_dir(tmp_path).session(session_id),
        thread_id="t1",
        workspace_root="/workspace",
        provider="default",
    )


class TestMessageRecord:
    def test_roundtrip_preserves_model_visible_fields(self):
        message = Message(
            role="assistant",
            content="calling",
            tool_calls=[ToolCall("call-1", "echo", {"value": "hello"})],
            name="assistant",
            status="success",
            additional_kwargs={"provider_note": {"a": 1}},
            response_metadata={"model": "mock"},
            usage_metadata={"input_tokens": 2, "output_tokens": 1},
        )

        record = MessageRecord.from_message(message, 1)
        restored = MessageRecord.from_dict(record.to_dict()).to_message()

        assert restored.role == message.role
        assert restored.content == message.content
        assert restored.tool_calls == message.tool_calls
        assert restored.additional_kwargs == message.additional_kwargs
        assert restored.response_metadata == message.response_metadata
        assert restored.usage_metadata == message.usage_metadata

    def test_rejects_unknown_record_fields(self):
        record = MessageRecord.from_message(Message(role="user", content="x"), 1)
        raw = record.to_dict()
        raw["surprise"] = True

        with pytest.raises(ValueError, match="unknown"):
            MessageRecord.from_dict(raw)

    def test_rejects_non_json_provider_metadata(self):
        message = Message(
            role="assistant",
            content="x",
            response_metadata={"bad": object()},
        )

        with pytest.raises(TypeError, match="JSON-compatible"):
            MessageRecord.from_message(message, 1)

    def test_runtime_only_fields_are_not_persisted(self):
        message = Message(
            role="tool",
            content="done",
            tool_call_id="call-1",
            client_events=[{"type": "notice", "data": {}}],
            turn_complete=True,
        )

        restored = MessageRecord.from_message(message, 1).to_message()

        assert restored.client_events == []
        assert restored.turn_complete is False


class TestMessageHistoryStore:
    def test_append_uses_strict_contiguous_records(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
        ])

        records = _raw_records(persistence)
        assert [record["position"] for record in records] == [1, 2]
        assert all(record["schema_version"] == 1 for record in records)
        assert [message.content for message in persistence.history.load()] == [
            "one", "two",
        ]

    def test_replace_stores_only_effective_history(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([
            Message(role="user", content="discarded input"),
            Message(role="assistant", content="discarded answer"),
        ])

        persistence.history.replace([
            Message(role="system", content="summary"),
            Message(role="user", content="retained"),
        ])

        text = persistence.history.path.read_text(encoding="utf-8")
        assert "discarded input" not in text
        assert "discarded answer" not in text
        assert [message.content for message in persistence.history.load()] == [
            "summary", "retained",
        ]

    def test_incomplete_record_is_an_explicit_error(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([Message(role="user", content="durable")])
        with persistence.history.path.open("a", encoding="utf-8") as stream:
            stream.write('{"schema_version": 1')

        with pytest.raises(ValueError, match="Invalid messages.jsonl"):
            persistence.history.load()

    def test_artifacts_are_references_not_payloads(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        payload = b"small-image"
        ref = persistence.artifacts.put(
            ArtifactKind.MEDIA,
            payload,
            media_type="image/png",
        )
        image = ImageContent(ref.id, ref.media_type, ref.size)

        persistence.history.append([
            Message(role="user", images=[image], artifact=[ref])
        ])

        text = persistence.history.path.read_text(encoding="utf-8")
        assert "small-image" not in text
        restored = persistence.history.load()[0]
        assert restored.images == [image]
        assert restored.artifact == [ref]
        assert persistence.artifacts.read(ref) == payload

    def test_recreation_reads_same_history(self, tmp_path):
        first = thread_persistence(tmp_path)
        first.history.append([Message(role="user", content="persistent")])

        second = thread_persistence(tmp_path)

        assert [message.content for message in second.history.load()] == [
            "persistent"
        ]


class TestConversationHistory:
    def test_append_and_extend_are_durable_before_visible(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)

        history.append(Message(role="user", content="one"))
        history.extend([Message(role="assistant", content="two")])

        assert [message.content for message in history] == ["one", "two"]
        assert persistence.history.load() == history

    def test_undo_and_clear_replace_current_history(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        history.extend([
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ])

        assert [message.content for message in history.undo(1)] == [
            "one", "answer one",
        ]
        assert "answer two" not in persistence.history.path.read_text(
            encoding="utf-8"
        )

        history.clear()

        assert history.snapshot() == ()
        assert persistence.history.load() == []

    def test_persisted_message_nested_fields_are_immutable(self, tmp_path):
        history = ConversationHistory(sink=thread_persistence(tmp_path).history)
        message = Message(
            role="assistant",
            tool_calls=[ToolCall("call-1", "echo", {"nested": {"value": 1}})],
            usage_metadata={"input_tokens": 1},
            data={"items": [{"status": "pending"}]},
        )

        history.append(message)

        with pytest.raises(RuntimeError, match="immutable"):
            message.usage_metadata["input_tokens"] = 2
        with pytest.raises(RuntimeError, match="immutable"):
            message.tool_calls[0].args["nested"]["value"] = 2
        with pytest.raises(RuntimeError, match="immutable"):
            message.data["items"].append({"status": "completed"})

    def test_failed_sink_write_does_not_change_history(self):
        class FailingSink:
            def append(self, _messages):
                raise OSError("disk full")

            def replace(self, _messages):
                raise OSError("disk full")

        original = Message(role="user", content="stable")
        history = ConversationHistory([original], sink=FailingSink())

        with pytest.raises(OSError, match="disk full"):
            history.append(Message(role="assistant", content="not durable"))
        assert history.snapshot() == (original,)

        with pytest.raises(OSError, match="disk full"):
            history.clear()
        assert history.snapshot() == (original,)


class TestThreadMetadataStore:
    def test_typed_metadata_roundtrip(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        metadata = ThreadMetadata(
            provider="mock",
            model="mock-1",
            workspace_root="/workspace",
            title="Example",
        )

        persistence.metadata.save(metadata)

        assert persistence.metadata.load() == metadata

    def test_unknown_metadata_is_rejected(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.paths.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        persistence.paths.metadata_file.write_text(
            json.dumps({"schema_version": 1, "unknown": True}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="fields mismatch"):
            persistence.metadata.load()

    def test_metadata_state_persists_each_typed_replacement(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        state = ThreadMetadataState(
            persistence.metadata.load(),
            sink=persistence.metadata,
        )
        selected = ThreadMetadata(
            provider="mock",
            model="mock-2",
            model_mode="high",
            workspace_root="/workspace",
        )

        state.replace(selected)

        assert state.value == selected
        assert persistence.metadata.load() == selected


class TestInboxStore:
    def test_reconcile_removes_inputs_already_committed_to_history(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        inputs = [
            InboxInput(
                content="one",
                target=InboxTarget.NEXT_TURN,
                source="user",
                message_id="accepted",
            ),
            InboxInput(
                content="two",
                target=InboxTarget.NEXT_STEP,
                source="user",
                message_id="pending",
            ),
        ]
        persistence.inbox.replace(inputs)

        pending = persistence.inbox.reconcile({"accepted"})

        assert [item.message_id for item in pending] == ["pending"]
        assert [item.message_id for item in persistence.inbox.load()] == ["pending"]

    def test_unknown_snapshot_fields_fail_explicitly(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.paths.inbox_file.write_text(
            json.dumps({"schema_version": 1, "items": [], "legacy": []}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="fields mismatch"):
            persistence.inbox.load()


class TestThreadLifecycleStore:
    def test_typed_lifecycle_roundtrip(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        record = ThreadLifecycleRecord.create(
            "started",
            thread_id="child",
            parent_thread_id="t1",
            agent="builder",
        )

        persistence.lifecycle.append(record)

        assert persistence.lifecycle.load() == [record]

    def test_invalid_lifecycle_timestamp_fails_explicitly(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        raw = ThreadLifecycleRecord.create(
            "started",
            thread_id="child",
            parent_thread_id="t1",
            agent="builder",
        ).to_dict()
        raw["timestamp"] = "not-a-time"
        persistence.paths.session.threads_log.parent.mkdir(parents=True, exist_ok=True)
        persistence.paths.session.threads_log.write_text(
            json.dumps(raw) + "\n", encoding="utf-8"
        )

        with pytest.raises(ValueError, match="ISO 8601"):
            persistence.lifecycle.load()

    def test_concurrent_thread_writers_append_complete_records(self, tmp_path):
        session = RuntimePaths.from_data_dir(tmp_path).session("shared")

        def append(index: int) -> None:
            persistence = ThreadPersistence.create(
                session,
                thread_id=f"child-{index}",
                workspace_root="/workspace",
                provider="default",
            )
            persistence.lifecycle.append(ThreadLifecycleRecord.create(
                "completed",
                thread_id=f"child-{index}",
                parent_thread_id="agent",
                agent="worker",
            ))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append, range(40)))

        records = ThreadPersistence.open(
            session,
            thread_id="agent",
        ).lifecycle.load()
        assert len(records) == 40
        assert {record.thread_id for record in records} == {
            f"child-{index}" for index in range(40)
        }


def _raw_records(persistence: ThreadPersistence) -> list[dict]:
    return [
        json.loads(line)
        for line in persistence.history.path.read_text(encoding="utf-8").splitlines()
    ]
