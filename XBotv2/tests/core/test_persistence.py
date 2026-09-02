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
            tool_calls=[
                ToolCall(id="call-1", name="echo", args={"value": "hello"})
            ],
            name="assistant",
            status="success",
            additional_kwargs={"provider_note": {"a": 1}},
            response_metadata={"model": "mock"},
            usage_metadata={"input_tokens": 2, "output_tokens": 1},
        )

        record = MessageRecord.from_message(message, 1)
        restored = MessageRecord.model_validate(
            record.model_dump(mode="json")
        ).to_message()

        assert restored.role == message.role
        assert restored.content == message.content
        assert restored.tool_calls == message.tool_calls
        assert restored.additional_kwargs == message.additional_kwargs
        assert restored.response_metadata == message.response_metadata
        assert restored.usage_metadata == message.usage_metadata

    def test_rejects_unknown_record_fields(self):
        record = MessageRecord.from_message(Message(role="user", content="x"), 1)
        raw = record.model_dump(mode="json")
        raw["surprise"] = True

        with pytest.raises(ValueError, match="Extra inputs"):
            MessageRecord.model_validate(raw)

    def test_rejects_non_json_provider_metadata(self):
        message = Message(
            role="assistant",
            content="x",
            response_metadata={"bad": object()},
        )

        with pytest.raises(ValueError, match="valid JSON"):
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

    def test_replace_appends_surface_operation_without_destroying_trajectory(
        self,
        tmp_path,
    ):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([
            Message(role="user", content="discarded input"),
            Message(role="assistant", content="discarded answer"),
        ])
        before = persistence.history.path.read_bytes()

        surface = persistence.history.load_surface()
        persistence.history.replace_surface(
            tuple(node.node_id for node in surface),
            [Message(role="system", content="summary")],
            operation="compact:first",
            preserve_transcript=True,
        )

        trajectory = persistence.history.path.read_bytes()
        assert trajectory.startswith(before)
        assert b"discarded input" in trajectory
        assert b"discarded answer" in trajectory
        assert b'"record_type": "surface_replace"' in trajectory
        assert [message.content for message in persistence.history.load()] == [
            "summary",
        ]
        assert [
            message.content for message in persistence.history.load_transcript()
        ] == ["discarded input", "discarded answer"]

    def test_nested_surface_replacements_replay_deterministically(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        history.extend([
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ])

        history.replace_range(
            0,
            2,
            [Message(role="system", content="summary one")],
            operation="compact:first",
            preserve_transcript=True,
        )
        history.replace_range(
            0,
            2,
            [Message(role="system", content="summary two")],
            operation="compact:second",
            preserve_transcript=True,
        )

        assert [message.content for message in history] == [
            "summary two", "answer two",
        ]
        assert persistence.history.load() == history
        assert [
            message.content for message in persistence.history.load_transcript()
        ] == ["one", "answer one", "two", "answer two"]
        records = _raw_records(persistence)
        assert [record.get("record_type", "message") for record in records] == [
            "message", "message", "message", "message",
            "surface_replace", "surface_replace",
        ]

    def test_surface_replay_rejects_non_current_source_nodes(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([Message(role="user", content="one")])
        with persistence.history.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "schema_version": 1,
                "position": 2,
                "record_type": "surface_replace",
                "operation": "compact",
                "transcript": "preserve",
                "source_node_ids": ["missing"],
                "messages": [],
            }) + "\n")

        with pytest.raises(ValueError, match="source nodes are not current"):
            persistence.history.load()

    def test_invalid_transcript_preserving_replace_writes_nothing(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        persistence.history.append([Message(role="user", content="one")])
        before = persistence.history.path.read_bytes()
        source = persistence.history.load_surface()

        with pytest.raises(ValueError, match="must produce one surface node"):
            persistence.history.replace_surface(
                [source[0].node_id],
                [
                    Message(role="system", content="first"),
                    Message(role="system", content="second"),
                ],
                operation="compact:invalid",
                preserve_transcript=True,
            )

        assert persistence.history.path.read_bytes() == before

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

    def test_append_preserves_cursor_and_surface_replace_invalidates_it(self, tmp_path):
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

    def test_compact_preserves_transcript_cursor_but_invalidates_surface_cursor(
        self,
        tmp_path,
    ):
        from XBotv2.core.history import HistoryCursorInvalid

        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        history.extend([
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ])
        surface_cursor = persistence.history.page(limit=1).next_cursor
        transcript_cursor = persistence.history.page_transcript(limit=1).next_cursor

        history.replace_range(
            0,
            2,
            [Message(role="system", content="summary")],
            operation="compact:test",
            preserve_transcript=True,
        )

        with pytest.raises(HistoryCursorInvalid, match="current history"):
            persistence.history.page(limit=1, cursor=surface_cursor)
        transcript_page = persistence.history.page_transcript(
            limit=1,
            cursor=transcript_cursor,
        )
        assert [message.content for message in transcript_page.messages] == ["two"]

        history.undo(1)
        with pytest.raises(HistoryCursorInvalid, match="current history"):
            persistence.history.page_transcript(limit=1, cursor=transcript_cursor)
        assert [
            message.content for message in persistence.history.load_transcript()
        ] == ["one", "answer one"]

    def test_clear_after_compact_removes_original_transcript_lineage(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)
        history.extend([
            Message(role="user", content="one"),
            Message(role="assistant", content="answer one"),
            Message(role="user", content="two"),
            Message(role="assistant", content="answer two"),
        ])
        history.replace_range(
            0,
            2,
            [Message(role="system", content="summary")],
            operation="compact:test",
            preserve_transcript=True,
        )

        history.clear()

        assert persistence.history.load() == []
        assert persistence.history.load_transcript() == []

    def test_cursor_is_bound_to_one_thread_history(self, tmp_path):
        from XBotv2.core.history import HistoryCursorInvalid

        first = thread_persistence(tmp_path, "first")
        second = thread_persistence(tmp_path, "second")
        for persistence in (first, second):
            persistence.history.append([
                Message(role="user", content="one"),
                Message(role="assistant", content="two"),
            ])
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
        image = ImageContent(path=ref.id, media_type=ref.media_type, size=ref.size)

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
    def test_in_memory_compaction_preserves_human_transcript(self):
        messages = [
            Message(role="user", content="one"),
            Message(role="assistant", content="answer"),
            Message(role="user", content="two"),
        ]
        history = ConversationHistory(messages)

        history.replace_range(
            0,
            2,
            [Message(role="system", content="summary")],
            operation="compact:test",
            preserve_transcript=True,
        )

        assert [message.content for message in history] == ["summary", "two"]
        assert [
            message.content for message in history.page_transcript(limit=10).messages
        ] == ["one", "answer", "two"]

    def test_append_and_extend_are_durable_before_visible(self, tmp_path):
        persistence = thread_persistence(tmp_path)
        history = ConversationHistory(sink=persistence.history)

        history.append(Message(role="user", content="one"))
        history.extend([Message(role="assistant", content="two")])

        assert [message.content for message in history] == ["one", "two"]
        assert persistence.history.load() == history

    def test_undo_and_clear_append_surface_operations(self, tmp_path):
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
        trajectory_after_undo = persistence.history.path.read_text(encoding="utf-8")
        assert "answer two" in trajectory_after_undo
        assert '"operation": "undo"' in trajectory_after_undo

        history.clear()

        assert history.snapshot() == ()
        assert persistence.history.load() == []
        trajectory_after_clear = persistence.history.path.read_text(encoding="utf-8")
        assert trajectory_after_clear.startswith(trajectory_after_undo)
        assert '"operation": "clear"' in trajectory_after_clear

    def test_persisted_message_nested_fields_are_immutable(self, tmp_path):
        history = ConversationHistory(sink=thread_persistence(tmp_path).history)
        message = Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="echo",
                    args={"nested": {"value": 1}},
                )
            ],
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

            def replace_surface(
                self,
                _source_node_ids,
                _messages,
                *,
                operation,
                preserve_transcript,
            ):
                del operation, preserve_transcript
                raise OSError("disk full")

            def record(self, _event, _data):
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

        with pytest.raises(ValueError, match="Extra inputs"):
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

        with pytest.raises(ValueError, match="Extra inputs"):
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
        ).model_dump(mode="json")
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
