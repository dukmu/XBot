"""Conversation history and strict thread persistence tests."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.history import ConversationHistory
from XBotv2.core.messages import ImageContent, Message
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.runtime_logging import RuntimeLog
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


def test_artifact_operations_log_metadata_without_content(tmp_path, caplog):
    caplog.set_level("DEBUG", logger="xbotv2.persistence")
    paths = RuntimePaths.from_data_dir(tmp_path).session("s1").thread("t1")
    store = ArtifactStore(paths, RuntimeLog())
    payload = b"artifact-secret-content"

    artifact = store.put(
        ArtifactKind.ATTACHMENT,
        payload,
        media_type="application/octet-stream",
        name="private-name.bin",
    )
    assert store.read(artifact) == payload

    text = caplog.text
    assert "persistence.artifact.stored" in text
    assert "persistence.artifact.read" in text
    assert artifact.id in text
    assert "artifact-secret-content" not in text
    assert "private-name.bin" not in text


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

    def test_pages_read_backwards_without_loading_the_full_history(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([
            Message(role="user", content=f"message-{index}")
            for index in range(5)
        ])

        latest = persistence.history.page(limit=2)
        older = persistence.history.page(limit=2, cursor=latest.next_cursor)
        oldest = persistence.history.page(limit=2, cursor=older.next_cursor)

        assert [message.content for message in latest.messages] == [
            "message-3", "message-4",
        ]
        assert [message.content for message in older.messages] == [
            "message-1", "message-2",
        ]
        assert [message.content for message in oldest.messages] == ["message-0"]
        assert oldest.next_cursor is None

    def test_append_preserves_cursor_and_replace_invalidates_it(self, tmp_path):
        from XBotv2.core.history import HistoryCursorInvalid

        persistence = thread_persistence(tmp_path)
        persistence.history.append([
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
            Message(role="user", content="three"),
        ])
        latest = persistence.history.page(limit=1)

        persistence.history.append([Message(role="assistant", content="four")])
        older = persistence.history.page(limit=1, cursor=latest.next_cursor)
        assert [message.content for message in older.messages] == ["two"]

        persistence.history.replace([Message(role="user", content="replacement")])
        with pytest.raises(HistoryCursorInvalid, match="current history"):
            persistence.history.page(limit=1, cursor=latest.next_cursor)

    def test_cursor_is_bound_to_one_thread_history(self, tmp_path):
        from XBotv2.core.history import HistoryCursorInvalid

        first = thread_persistence(tmp_path, "first")
        second = thread_persistence(tmp_path, "second")
        for persistence in (first, second):
            persistence.history.append([
                Message(role="user", content="one"),
                Message(role="assistant", content="two"),
            ])
        second.paths.history_revision_file.write_text(
            first.paths.history_revision_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        cursor = first.history.page(limit=1).next_cursor
        with pytest.raises(HistoryCursorInvalid, match="current history"):
            second.history.page(limit=1, cursor=cursor)

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

    def test_recoverable_replace_preserves_exact_jsonl_and_post_replace_tail(
        self,
        tmp_path,
    ):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        original = [
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ]
        history.extend(original)
        original_jsonl = persistence.paths.messages_file.read_bytes()

        checkpoint = history.replace_recoverable(
            [Message(role="system", content="summary"), *original[-2:]],
            operation="compact",
            reason="manual",
        )
        assert checkpoint is not None
        archive = persistence.paths.history_checkpoint_messages(
            checkpoint.checkpoint_id
        )
        assert archive.read_bytes() == original_jsonl
        assert archive.stat().st_ino != persistence.paths.messages_file.stat().st_ino
        assert history.checkpoints(operation="compact") == (checkpoint,)

        tail = Message(role="user", content="after compact")
        history.append(tail)
        restored = history.restore(
            checkpoint.checkpoint_id,
            operation="compact",
        )

        assert restored.status == "restored"
        assert list(history) == [*original, tail]
        assert persistence.history.load() == list(history)
        assert history.checkpoints(operation="compact")[0].status == "restored"

    def test_nested_recoverable_replacements_restore_in_reverse_order(
        self,
        tmp_path,
    ):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        original = [
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ]
        history.extend(original)
        first = history.replace_recoverable(
            [Message(role="system", content="summary one"), *original[-2:]],
            operation="compact",
            reason="automatic",
        )
        history.append(Message(role="user", content="between"))
        before_second = list(history)
        second = history.replace_recoverable(
            [Message(role="system", content="summary two"), before_second[-1]],
            operation="compact",
            reason="automatic",
        )
        history.append(Message(role="assistant", content="after"))
        assert first is not None and second is not None
        assert [item.status for item in history.checkpoints(operation="compact")] == [
            "superseded",
            "active",
        ]

        history.restore(second.checkpoint_id, operation="compact")
        assert list(history) == [
            *before_second,
            Message(role="assistant", content="after"),
        ]
        assert history.checkpoints(operation="compact")[0].status == "active"

        history.restore(first.checkpoint_id, operation="compact")
        assert [message.content for message in history] == [
            "one",
            "answer one",
            "two",
            "answer two",
            "between",
            "after",
        ]

    def test_failed_recoverable_replace_leaves_original_and_prepared_checkpoint(
        self,
        tmp_path,
        monkeypatch,
    ):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        original = Message(role="user", content="do not lose")
        history.append(original)
        original_jsonl = persistence.paths.messages_file.read_bytes()

        def fail_replace(_messages):
            raise OSError("disk full")

        monkeypatch.setattr(persistence.history, "replace", fail_replace)
        with pytest.raises(OSError, match="disk full"):
            history.replace_recoverable(
                [Message(role="system", content="summary")],
                operation="compact",
                reason="automatic",
            )

        assert history.snapshot() == (original,)
        assert persistence.paths.messages_file.read_bytes() == original_jsonl
        checkpoints = persistence.history.checkpoints(operation="compact")
        assert len(checkpoints) == 1
        assert checkpoints[0].status == "prepared"
        assert persistence.paths.history_checkpoint_messages(
            checkpoints[0].checkpoint_id
        ).read_bytes() == original_jsonl


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
