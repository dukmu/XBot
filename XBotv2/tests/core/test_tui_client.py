"""Tests for protocol-driven TUI client state."""

import ast
import argparse
import asyncio
import html
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import XBotv2.main as xbot_main
from XBotv2.tui.client import (
    TuiState,
    TuiJob,
    TuiTool,
    TuiTranscriptEntry,
    _MAX_STATE_MESSAGES,
    _MAX_STATE_NOTICES,
    _MAX_STATE_TOOLS,
    _MAX_STATE_TRANSCRIPT,
    _TRIM_SLACK,
    _parse_permission_decision,
)
from XBotv2.tui.terminal import TerminalSession
from XBotv2.tui.command import CommandSpec


@pytest.mark.asyncio
async def test_clear_dispatch_distinguishes_screen_and_history_commands():
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _cmd_clear_entry = AsyncMock()
        _dispatch_remote_command = AsyncMock()

        def _command_handler(self, name):
            return self._cmd_clear_entry if name == "clear-screen" else None

    handler = Handler()
    await XBotTextualApp._handle_slash_command(handler, CommandSpec(
        name="clear-screen", kind="client", description="clear", raw="/clear-screen",
    ))
    handler._cmd_clear_entry.assert_awaited_once()
    handler._dispatch_remote_command.assert_not_awaited()

    handler._cmd_clear_entry.reset_mock()
    await XBotTextualApp._handle_slash_command(handler, CommandSpec(
        name="clear", kind="client", description="clear history", raw="/clear",
    ))
    handler._cmd_clear_entry.assert_not_awaited()
    handler._dispatch_remote_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_command_dispatch_shows_command_notice():
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _session_attached = True
        session = type("Session", (), {"run_command": AsyncMock(
            return_value={
                "data": {
                    "command": "undo",
                    "status": "ok",
                    "message": "Removed 1 conversation turn.",
                }
            }
        )})()
        state = TuiState()
        _cmd_clear = AsyncMock()
        _render_new_transcript_entries = AsyncMock()
        _append_local_notice = AsyncMock()

        def _record_error(self, error):
            raise AssertionError(error)

    handler = Handler()
    await XBotTextualApp._dispatch_remote_command(handler, CommandSpec(
        name="undo", kind="server", description="undo", raw="/undo",
    ))

    handler._append_local_notice.assert_awaited_once_with(
        "/undo", "Removed 1 conversation turn."
    )


@pytest.mark.asyncio
async def test_thread_effect_command_refreshes_session_identity():
    """A command that changes thread projections refreshes them immediately."""
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _session_attached = True
        session = type("Session", (), {"run_command": AsyncMock(
            return_value={
                "data": {
                    "command": "goal",
                    "status": "ok",
                    "message": "[complete] ship the API",
                    "effects": ["thread"],
                }
            }
        )})()
        state = TuiState()
        _append_local_notice = AsyncMock()
        _refresh_session_identity = AsyncMock()

        def _record_error(self, error):
            raise AssertionError(error)

    handler = Handler()
    await XBotTextualApp._dispatch_remote_command(handler, CommandSpec(
        name="goal",
        kind="server",
        description="goal",
        raw="/goal complete shipped",
    ))

    handler._refresh_session_identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_refresh_applies_status_slots_without_identity_change():
    """Status slots refresh from the descriptor even when identity is stable."""
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        session = type("Session", (), {"refresh_descriptor": AsyncMock(
            return_value={
                "title": "session-1",
                "agent_name": "default",
                "provider": "minimax",
                "model": "m3",
                "model_mode": "",
                "context_window": 0,
                "status_slots": {
                    "goal": "complete",
                    "goal_stats": "3t 11tok 4tools",
                },
            }
        )})()
        state = TuiState(
            session_title="session-1",
            agent_name="default",
            provider="minimax",
            model="m3",
        )
        _apply_status_slots = XBotTextualApp._apply_status_slots
        _refresh_all = Mock()
        _refresh_status_now = Mock()

    handler = Handler()
    await XBotTextualApp._refresh_session_identity(handler)

    assert handler.state.status_slots == {
        "goal": "complete",
        "goal_stats": "3t 11tok 4tools",
    }
    handler._refresh_status_now.assert_called_once()
    handler._refresh_all.assert_not_called()


def test_history_updated_event_restores_tui_history() -> None:
    state = TuiState()
    state.apply_event({
        "type": "history_updated",
        "data": {
            "history": [{"role": "user", "content": "kept"}],
            "operation": "undo",
            "turns": 1,
        },
    })

    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "kept"),
    ]


def test_agent_configured_event_refreshes_status_metadata() -> None:
    state = TuiState(provider="old", workspace_root="/old")
    state.apply_event({
        "type": "agent_configured",
        "data": {
            "provider": "minimax",
            "model": "m3",
            "model_mode": "reasoning",
            "context_window": 32000,
        },
    })

    assert state.provider == "minimax"
    assert state.model == "m3"
    assert state.model_mode == "reasoning"
    assert state.context_window == 32000
    assert state.workspace_root == "/old"


@pytest.mark.asyncio
async def test_invalid_remote_syntax_is_a_notice_not_tui_error() -> None:
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _session_attached = True
        session = type("Session", (), {"run_command": AsyncMock(
            side_effect=ValueError("Usage: /undo [count]")
        )})()
        _append_local_notice = AsyncMock()

        def _record_error(self, error):
            raise AssertionError(error)

    handler = Handler()
    await XBotTextualApp._dispatch_remote_command(
        handler,
        CommandSpec(
            name="undo",
            kind="server",
            description="undo",
            args="many",
            raw="/undo many",
        ),
    )

    handler._append_local_notice.assert_awaited_once_with(
        "/undo", "Usage: /undo [count]"
    )


def test_tui_state_applies_protocol_events_and_renders_lines():
    state = TuiState()
    frames = [
        _frame("agent_configured", {"agent_name": "TestBot", "provider": "mock"}),
        _frame("turn_started", {"turn": 1}),
        _frame(
            "assistant_message",
            {
                "content": "hello world",
                "tool_calls": [{"id": "call_1", "name": "filesystem_read", "args": {"path": "a.txt"}}],
            },
        ),
        _frame("tool_result", {"tool_call_id": "call_1", "content": "cached result", "status": "success"}),
        _frame("client_message", {"message": "heads up"}),
        _frame("usage", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "requests": 1}),
        _frame("turn_finished", {"turn": 1}),
    ]

    for frame in frames:
        state.apply_event(frame)

    assert state.status == "Ready"
    assert state.agent_name == "TestBot"
    assert state.messages[-1].content == "hello world"
    assert state.tools["call_1"].status == "success"
    assert state.tools["call_1"].summary == "cached result"
    assert state.notices[-1].kind == "client_message"
    assert state.usage["total_tokens"] == 15

def test_tui_state_applies_flat_usage_delta():
    state = TuiState()

    state.apply_event(_frame("usage", {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "requests": 2}))

    assert state.usage == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "requests": 2,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "prompt_cache_write_tokens": 0,
    }


def test_tool_details_preserve_full_structured_result():
    from XBotv2.tui.textual_widgets import tool_detail

    state = TuiState()
    content = "x" * 500
    state.apply_event({
        "type": "tool_result",
        "data": {
            "tool_call_id": "call-1",
            "name": "example",
            "status": "error",
            "content": content,
            "error": {"code": "failed", "message": "bad input"},
            "artifacts": [
                {"id": "artifact-1", "name": "report.txt", "media_type": "text/plain"}
            ],
        },
    })

    tool = state.tools["call-1"]
    detail = tool_detail(tool)

    assert len(tool.summary) < len(content)
    assert tool.result == content
    assert content in detail
    assert '"code": "failed"' in detail
    assert "report.txt" in detail


def test_todo_tool_details_use_current_snapshot_projection():
    from XBotv2.tui.textual_widgets import tool_detail

    state = TuiState()
    state.apply_event({
        "type": "tool_result",
        "data": {
            "tool_call_id": "todo-1",
            "name": "task_update",
            "status": "success",
            "content": "Updated task #2: in_progress",
            "data": {
                "kind": "todo_snapshot",
                "schema_version": 2,
                "next_id": 3,
                "jobs": [
                    {
                        "id": "1", "subject": "Inspect", "status": "completed",
                        "blocks": [], "blockedBy": [],
                    },
                    {
                        "id": "2", "subject": "Implement", "status": "in_progress",
                        "activeForm": "Implementing", "blocks": [], "blockedBy": [],
                    },
                ],
            },
        },
    })

    detail = tool_detail(state.tools["todo-1"])

    assert "[x] #1 Inspect" in detail
    assert "[>] #2 Implement" in detail


def test_tui_state_ignores_blank_assistant_message_but_keeps_tool_calls():
    state = TuiState()

    state.apply_event({
        "type": "assistant_message",
        "data": {
            "content": "\n  \t",
            "tool_calls": [{"id": "call_1", "name": "shell", "args": {"command": "df -h"}}],
        },
    })

    # No placeholder — the tool widget itself shows the model is
    # acting. Reasoning, if any, was already streamed via deltas.
    assert len(state.messages) == 0
    assert state.tools["call_1"].name == "shell"
    assert len(state.transcript) == 1  # tool entry only


def test_tui_state_restores_resumed_message_and_tool_history():
    state = TuiState()

    state.restore_history([
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "reading",
            "reasoning": "inspect the file first",
            "tool_calls": [
                {"id": "call_1", "name": "filesystem_read", "args": {"path": "a.txt"}}
            ],
        },
        {
            "role": "tool",
            "content": "contents",
            "tool_call_id": "call_1",
            "status": "success",
            "error": {"code": "warning", "message": "partial"},
            "artifacts": [
                {"id": "artifact-1", "name": "a.txt", "media_type": "text/plain"}
            ],
        },
        {"role": "assistant", "content": "done"},
        {
            "role": "user",
            "content": "<runtime_event />",
            "runtime": {"source": "jobs", "event": "completed"},
        },
    ])

    assert state.turn == 1
    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "read it"),
        ("assistant", "reading"),
        ("assistant", "done"),
    ]
    assert state.messages[1].reasoning == "inspect the file first"
    assert state.tools["call_1"].name == "filesystem_read"
    assert state.tools["call_1"].status == "success"
    assert state.tools["call_1"].summary == "contents"
    assert state.tools["call_1"].error["code"] == "warning"
    assert state.tools["call_1"].artifacts[0]["name"] == "a.txt"
    assert [(notice.kind, notice.text) for notice in state.notices] == [
        ("jobs:completed", "jobs completed"),
    ]


@pytest.mark.asyncio
async def test_injected_reminder_is_a_notice_not_typed_input():
    """A persisted harness turn carries runtime provenance, never human input."""
    from unittest.mock import patch

    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _view_active = False
        state = TuiState()
        _render_new_transcript_entries = AsyncMock()

    handler = Handler()
    with patch.object(
        XBotTextualApp, "_consume_stream_event", XBotTextualApp._consume_stream_event
    ):
        await XBotTextualApp._consume_stream_event(
            handler,
            {
                "type": "message",
                "data": {
                    "id": "m1",
                    "role": "user",
                    "content": '<system_reminder source="todo" event="reminder">nag</system_reminder>',
                    "runtime": {"source": "todo", "event": "reminder"},
                },
            },
            pop_pending=False,
        )

    assert handler.state.messages == []
    assert [(notice.kind, notice.text) for notice in handler.state.notices] == [
        ("todo:reminder", "todo reminder"),
    ]


def test_tui_state_replaces_history_without_stale_tool_indexes():
    state = TuiState()
    state.restore_history([
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "done",
            "tool_calls": [{"id": "call-1", "name": "shell", "args": {"command": "pwd"}}],
        },
    ])
    state.restore_history([
        {"role": "user", "content": "second"},
        {
            "role": "assistant",
            "content": "new",
            "tool_calls": [{"id": "call-1", "name": "shell", "args": {"command": "ls"}}],
        },
    ])

    assert [message.content for message in state.messages] == ["second", "new"]
    assert state.turn == 1
    assert state.tools["call-1"].args == {"command": "ls"}
    assert len([entry for entry in state.transcript if entry.kind == "tool"]) == 1


def test_tui_state_appends_assistant_deltas_to_one_message():
    state = TuiState()

    state.apply_event({"type": "turn_started", "data": {"turn": 1}})
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "Hel"}})
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "lo"}})
    state.apply_event({"type": "assistant_message", "data": {"content": "Hello"}})

    assert [(m.role, m.content) for m in state.messages] == [("assistant", "Hello")]


def test_json_payload_reuses_one_adapter_instead_of_rebuilding(monkeypatch):
    """Every streamed frame must not recompile the wire->JSON adapter.

    Building a ``TypeAdapter`` per frame recompiles a core schema (~1ms
    measured). A streaming turn publishes far faster than the resulting
    ~1000 events/s ceiling, so the client falls behind, its backlog overflows
    the server's bounded 512-frame replay window, and the user is shown
    ``session_event_cursor_expired``. The adapter must be built once.
    """

    from XBotv2.protocol.models import ServerEvent
    from XBotv2.tui import terminal as terminal_module

    constructions: list[object] = []
    original = terminal_module.TypeAdapter

    class CountingTypeAdapter:
        def __init__(self, *args, **kwargs):
            constructions.append(args)
            self._inner = original(*args, **kwargs)

        def validate_python(self, *args, **kwargs):
            return self._inner.validate_python(*args, **kwargs)

    # Patch after import: the module-level adapter already exists, so a
    # correct implementation never touches TypeAdapter again at call time.
    monkeypatch.setattr(terminal_module, "TypeAdapter", CountingTypeAdapter)

    event = ServerEvent(
        session_id="s",
        thread_id="t",
        sequence=7,
        type="assistant_message_delta",
        data={"reasoning": "think"},
    )
    payloads = [terminal_module._json_payload(event) for _ in range(50)]

    assert constructions == [], (
        "_json_payload rebuilt TypeAdapter per call; hoist it to module scope"
    )
    assert payloads[0] == payloads[-1]
    assert payloads[0]["type"] == "assistant_message_delta"
    assert payloads[0]["sequence"] == 7
    assert payloads[0]["data"] == {"reasoning": "think"}


def test_tui_state_distinguishes_thinking_from_visible_output() -> None:
    state = TuiState()
    state.apply_event({"type": "turn_started", "data": {"turn": 1}})

    state.apply_event(
        {"type": "assistant_message_delta", "data": {"reasoning": "checking"}}
    )
    assert state.status == "Thinking"

    state.apply_event(
        {"type": "assistant_message_delta", "data": {"content": "answer"}}
    )
    assert state.status == "Running"


def test_tui_state_preserves_utf8_messages_without_transcoding():
    state = TuiState()

    state.append_message("user", "完成一个纯Python é")

    assert state.messages[0].content == "完成一个纯Python é"


def test_tui_state_updates_streaming_tool_call_args_by_index():
    state = TuiState()

    state.apply_event({"type": "turn_started", "data": {"turn": 1}})
    state.apply_event({
        "type": "tool_call_delta",
        "data": {
            "tool_calls": [
                {
                    "tool_call_id": "call_1",
                    "name": "shell",
                    "args_delta": '{"command"',
                    "index": 0,
                }
            ],
        },
    })
    state.apply_event({
        "type": "tool_call_delta",
        "data": {"tool_calls": [{"tool_call_id": "call_1", "args_delta": ': "df -h"}', "index": 0}]},
    })

    assert state.tools["call_1"].name == "shell"
    # Mid-stream: raw JSON accumulates in args_streaming, args_preview
    # is empty so the title does not show half-formed JSON.
    assert state.tools["call_1"].args_preview == ""
    assert state.tools["call_1"].args_streaming == '{"command": "df -h"}'
    assert state.tools["call_1"].args_finalized is False
    assert [entry.kind for entry in state.transcript] == ["tool"]


def test_tui_state_finalizes_tool_args_on_tool_calls_started():
    state = TuiState()

    state.apply_event({"type": "turn_started", "data": {"turn": 1}})
    state.apply_event({
        "type": "tool_call_delta",
        "data": {
            "tool_calls": [
                {"tool_call_id": "call_1", "name": "shell", "args_delta": '{"command"', "index": 0},
            ]
        },
    })
    state.apply_event({
        "type": "tool_calls_started",
        "data": {
            "tool_calls": [
                {"tool_call_id": "call_1", "name": "shell", "args": {"command": "df -h"}},
            ]
        },
    })

    # tool_calls_started carries the final parsed dict; args_preview
    # becomes the clean dict repr and args_finalized is True.
    assert state.tools["call_1"].args_finalized is True
    assert state.tools["call_1"].args == {"command": "df -h"}
    assert state.tools["call_1"].args_preview == '{"command": "df -h"}'


def test_tui_state_renames_provisional_streaming_tool_id():
    state = TuiState()

    state.apply_event({"type": "turn_started", "data": {"turn": 1}})
    state.apply_event({
        "type": "tool_call_delta",
        "data": {
            "tool_calls": [
                {"tool_call_id": "tool_0", "name": "shell", "args_delta": '{"command"', "index": 0},
            ]
        },
    })
    assert "tool_0" in state.tools
    state.apply_event({
        "type": "tool_calls_started",
        "data": {
            "tool_calls": [
                {"id": "call_shell", "name": "shell", "args": {"command": "df -h"}, "index": 0},
            ],
        },
    })
    state.apply_event({
        "type": "tool_result",
        "data": {
            "tool_call_id": "call_shell",
            "name": "shell",
            "status": "success",
            "content": "ok",
        },
    })

    assert "tool_0" not in state.tools
    assert state.tools["call_shell"].status == "success"
    assert state.tools["call_shell"].summary == "ok"
    assert [(entry.kind, entry.key) for entry in state.transcript] == [("tool", "call_shell")]


def test_tui_state_keeps_minimax_parallel_ids_with_shared_stream_index():
    state = TuiState()
    calls = [
        {"id": "call_date", "name": "shell", "args": {"command": "date"}},
        {"id": "call_pwd", "name": "shell", "args": {"command": "pwd"}},
        {"id": "call_list", "name": "filesystem_list", "args": {"path": "output"}},
    ]
    deltas = [
        {
            "tool_call_id": call["id"],
            "name": call["name"],
            "args_delta": call["args"],
            "index": 0,
            **(
                {"replaces_tool_call_id": calls[index - 1]["id"]}
                if index
                else {}
            ),
        }
        for index, call in enumerate(calls)
    ]

    state.apply_event(_frame("turn_started", {"turn": 1}))
    for delta in deltas:
        state.apply_event(_frame("tool_call_delta", {"tool_calls": [delta]}))
    state.apply_event(_frame("assistant_message", {"content": "", "tool_calls": calls}))
    state.apply_event(_frame("tool_calls_started", {"tool_calls": calls}))
    state.apply_event(_frame("permission_request", {
        "request_id": "permission:call_date",
        "reason": "Approval: shell",
        "tool_call": calls[0],
    }))

    assert list(state.tools) == ["call_date", "call_pwd", "call_list"]
    assert [entry.key for entry in state.transcript if entry.kind == "tool"] == [
        "call_date",
        "call_pwd",
        "call_list",
    ]
    assert state.tools["call_date"].permission_pending is True
    assert state.tools["call_pwd"].permission_pending is False


def test_tui_state_replay_folds_duplicate_assistant_message_by_id():
    """A replayed thread stream must not append the same assistant message
    again when the loaded trajectory already contains its message id."""
    state = TuiState()
    state.restore_history([
        {
            "role": "assistant",
            "message_id": "assistant-1",
            "content": "durable answer",
            "reasoning": "",
            "tool_calls": [],
        }
    ])

    state.apply_event(_frame("assistant_message", {
        "id": "assistant-1",
        "content": "durable answer",
        "reasoning": "",
        "tool_calls": [],
    }))

    assert [message.content for message in state.messages] == ["durable answer"]
    assert [(entry.kind, entry.key) for entry in state.transcript] == [
        ("message", "0")
    ]


def test_tui_state_replay_folds_provisional_tool_into_existing_history_tool():
    """Replayed tool-call deltas must not create a second transcript entry
    when the final tool call already exists in loaded history."""
    state = TuiState()
    state.restore_history([
        {
            "role": "assistant",
            "content": "",
            "reasoning": "",
            "tool_calls": [
                {"id": "call_1", "name": "shell", "args": {"command": "ls"}}
            ],
        },
        {
            "role": "tool",
            "content": "a.py",
            "tool_call_id": "call_1",
            "status": "success",
        },
    ])

    state.apply_event(_frame("tool_call_delta", {"tool_calls": [{
        "tool_call_id": "tool_0",
        "index": 0,
        "name": "shell",
        "args_delta": '{"command": "ls"}',
    }]}))
    state.apply_event(_frame("assistant_message", {
        "content": "",
        "tool_calls": [
            {"id": "call_1", "name": "shell", "args": {"command": "ls"}}
        ],
    }))
    state.apply_event(_frame("tool_calls_started", {"tool_calls": [
        {"id": "call_1", "name": "shell", "args": {"command": "ls"}}
    ]}))

    assert list(state.tools) == ["call_1"]
    assert [(entry.kind, entry.key) for entry in state.transcript] == [
        ("tool", "call_1")
    ]


def test_tui_state_keeps_sequential_tool_batches_distinct():
    state = TuiState()
    first_call = {
        "id": "call_1",
        "name": "create_goal",
        "args": {"objective": "first"},
        "index": 0,
    }
    second_call = {
        "id": "call_2",
        "name": "inspect_goal",
        "args": {},
        "index": 0,
    }
    events = [
        _frame("turn_started", {"turn": 1}),
        _frame("tool_call_delta", {"tool_calls": [{
            "tool_call_id": "call_1", "name": "create_goal",
            "args_delta": '{"objective": "first"}', "index": 0,
        }]}),
        _frame("assistant_message", {"tool_calls": [first_call]}),
        _frame("tool_calls_started", {"tool_calls": [first_call]}),
        _frame("tool_result", {
            "tool_call_id": "call_1", "name": "create_goal", "content": "created",
        }),
        _frame("tool_call_delta", {"tool_calls": [{
            "tool_call_id": "tool_0", "name": "inspect_goal",
            "args_delta": "{}", "index": 0,
        }]}),
        _frame("tool_call_delta", {"tool_calls": [{
            "tool_call_id": "call_2", "replaces_tool_call_id": "tool_0",
            "name": "inspect_goal", "index": 0,
        }]}),
        _frame("assistant_message", {"tool_calls": [second_call]}),
        _frame("tool_calls_started", {"tool_calls": [second_call]}),
        _frame("tool_result", {
            "tool_call_id": "call_2", "name": "inspect_goal", "content": "inspected",
        }),
    ]

    for event in events:
        state.apply_event(event)

    assert list(state.tools) == ["call_1", "call_2"]
    assert state.tools["call_1"].args_preview == '{"objective": "first"}'
    assert state.tools["call_1"].summary == "created"
    assert state.tools["call_2"].name == "inspect_goal"
    assert state.tools["call_2"].summary == "inspected"
    assert [(entry.kind, entry.key) for entry in state.transcript] == [
        ("tool", "call_1"),
        ("tool", "call_2"),
    ]


def test_tui_state_turn_finished_clears_waiting_state_but_keeps_history():
    state = TuiState()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(
        _frame("user_input_required", {
            "question": "Proceed?",
            "options": [
                {"label": "yes", "description": "Continue."},
                {"label": "no", "description": "Stop."},
            ],
        })
    )
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"
    assert state.pending_user_input_payload is None
    assert state.notices[-1].kind == "user_input_required"
    assert state.notices[-1].text == "Proceed?"


def test_tui_state_turn_finished_clears_pending_and_denial_status():
    state = TuiState()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    # Permission requests now link to tool widgets
    state.tools["call_req"] = TuiTool(tool_call_id="call_req", name="shell")
    state.apply_event(
        _frame("permission_request", {"reason": "approval needed", "tool_call": {"id": "call_req"}})
    )
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"
    assert state.pending_permission_payload is None
    assert state.tools["call_req"].permission_pending is False
    assert state.tools["call_req"].status == "error"

    state.apply_event(_frame("turn_started", {"turn": 2}))
    state.apply_event(
        _frame("permission_denied", {"request_id": "perm:call_deny", "tool_call": {"id": "call_deny"}})
    )
    # A denial is visible on its tool, not as a sticky global state.
    state.apply_event(_frame("turn_finished", {"turn": 2}))

    assert state.status == "Ready"


def test_tui_state_renders_interaction_response_acknowledgements():
    state = TuiState()

    state.apply_event(_frame("user_input_required", {"question": "Proceed?"}))
    state.apply_event(_frame("user_input_recorded", {"request_id": "user_input:c1"}))

    assert state.status == "Ready"
    assert state.notices[-1].kind == "user_input_recorded"
    assert state.notices[-1].text == "user_input:c1"

    # Permission requests attach to tool widgets now, not notices
    state.tools["c2"] = TuiTool(tool_call_id="c2", name="shell")
    state.apply_event(
        _frame(
            "permission_request",
            {
                "request_id": "approval-7f3a",
                "tool_call": {"name": "shell", "id": "c2"},
            },
        )
    )
    assert state.pending_permission_payload is not None
    assert state.pending_permission_payload["request_id"] == "approval-7f3a"
    assert state.tools["c2"].permission_pending is True
    state.apply_event(
        _frame(
            "permission_response_recorded",
            {"request_id": "approval-7f3a", "decision": "allow"},
        )
    )

    assert state.status == "Ready"
    assert state.pending_permission_payload is None
    assert state.tools["c2"].permission_pending is False
    assert state.tools["c2"].status == "allow"


def test_tui_state_ack_keeps_running_until_turn_finished():
    state = TuiState()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame("user_input_required", {"request_id": "user_input:c1"}))
    state.apply_event(_frame("user_input_recorded", {"request_id": "user_input:c1"}))

    assert state.status == "Running"
    assert state.pending_user_input_payload is None

    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"


def test_tui_state_permission_denied_keeps_active_turn_running():
    state = TuiState()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame("permission_denied", {"reason": "no"}))
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"

    state.apply_event(_frame("turn_started", {"turn": 2}))

    assert state.status == "Running"


def test_tui_notice_preserves_explicit_newlines():
    state = TuiState()
    state.append_notice("client_message", "first line\nsecond line")

    assert state.notices[-1].text == "first line\nsecond line"


@pytest.mark.parametrize(
    ("request_type", "request_data", "terminal_type", "terminal_data", "expected_status"),
    [
        (
            "user_input_required",
            {"request_id": "question-1", "question": "Continue?"},
            "turn_cancelled",
            {"turn": 1},
            "Interrupted",
        ),
        (
            "permission_request",
            {
                "request_id": "approval-1",
                "tool_call": {"id": "call-1", "name": "shell"},
            },
            "error",
            {"message": "turn failed"},
            "Error",
        ),
    ],
)
def test_terminal_events_clear_pending_interactions(
    request_type, request_data, terminal_type, terminal_data, expected_status
):
    state = TuiState()
    state.tools["call-1"] = TuiTool(tool_call_id="call-1", name="shell")
    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame(request_type, request_data))
    state.apply_event(_frame(terminal_type, terminal_data))

    assert state.pending_user_input_payload is None
    assert state.pending_permission_payload is None
    assert state.status == expected_status
    assert state.tools["call-1"].permission_pending is False


@pytest.mark.parametrize(
    ("terminal_type", "terminal_data", "expected_tool_status"),
    [
        ("turn_finished", {"turn": 1}, "error"),
        ("turn_cancelled", {"turn": 1, "reason": "client_interrupt"}, "cancelled"),
    ],
)
def test_tui_state_terminal_events_finalize_unanswered_tool_calls(
    terminal_type, terminal_data, expected_tool_status
):
    state = TuiState()
    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame("tool_calls_started", {"tool_calls": [
        {"id": "pending", "name": "shell", "args": {}},
        {"id": "running", "name": "shell", "args": {}},
    ]}))
    state.tools["running"].status = "running"
    state.apply_event(_frame("permission_request", {
        "request_id": "approval-1",
        "tool_call": {"id": "approval", "name": "shell", "args": {}},
    }))
    state.apply_event(_frame("tool_result", {
        "tool_call_id": "completed", "name": "shell", "status": "success", "content": "ok",
    }))
    state.apply_event(_frame("tool_result", {
        "tool_call_id": "cancelled", "name": "shell", "status": "cancelled", "content": "",
    }))

    state.apply_event(_frame(terminal_type, terminal_data))

    assert {
        tool_id: state.tools[tool_id].status
        for tool_id in ("pending", "running", "approval")
    } == {
        "pending": expected_tool_status,
        "running": expected_tool_status,
        "approval": expected_tool_status,
    }
    assert state.tools["completed"].status == "success"
    assert state.tools["cancelled"].status == "cancelled"
    assert all(state.tools[tool_id].finished_at > 0 for tool_id in state.tools)


@pytest.mark.asyncio
async def test_textual_tool_refresh_treats_source_as_plain_text():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        tool = TuiTool(
            tool_call_id="source",
            name="filesystem_read",
            args={"path": "context_cache.py"},
            args_finalized=True,
        )
        app.state.tools[tool.tool_call_id] = tool
        app.state.transcript.append(
            TuiTranscriptEntry(kind="tool", key=tool.tool_call_id)
        )
        await app._render_new_transcript_entries()

        tool.result = "calls: list[ToolCall]\nreturn [call for call in calls]"
        app.state._changed_tool_ids.add(tool.tool_call_id)
        await app._refresh_changed_tool_widgets()

        body = app.query_one(".tool .body")
        assert "list[ToolCall]" in body.text


async def test_facade_streams_other_threads_with_an_independent_cursor():
    """The read-only thread view streams a second thread without touching the
    attached thread's cursor."""
    from XBotv2.tui.terminal import TerminalSession

    from XBotv2.protocol.models import server_event

    class StubClient:
        def __init__(self):
            self.called = []

        def stream_events(self, session_id, thread_id, *, after=None):
            self.called.append((session_id, thread_id, after))
            events = [
                server_event(
                    type="assistant_message_delta",
                    data={"content": "a"},
                    sequence=7,
                ),
                server_event(type="end", data={}, sequence=8),
            ]
            return _async_iter(events)

    session = TerminalSession(
        session_id="s", thread_id="agent", base_url="http://test", client=StubClient()
    )
    frames = []
    async for event in session.stream_thread_events("agent-reviewer-1"):
        frames.append(event)
    assert frames
    assert session._view_cursor == 7  # the end sentinel is not a consumable frame
    assert session._event_cursor == 0, "the main cursor must not move"
    assert session._client.called[0][1] == "agent-reviewer-1"
    assert session._client.called[0][2] == 0  # first call: from the start


def test_facade_reads_thread_history_read_only():
    """Persisted conversation of another thread comes back as message records."""
    from XBotv2.tui.terminal import TerminalSession

    class StubClient:
        async def list_trajectory(self, session_id, thread_id, *, cursor=None, limit=160):
            assert thread_id == "agent-reviewer-1"
            return _trajectory_response()


    async def run():
        session = TerminalSession(
            session_id="s", thread_id="agent", base_url="http://test", client=StubClient()
        )
        items, next_cursor = await session.read_thread_history(
            "agent-reviewer-1", limit=50
        )
        assert [item["message"]["role"] for item in items] == ["user", "assistant"]
        assert items[1]["message"]["reasoning"] == "thinking hard"
        assert next_cursor is None  # no older pages in this stub

    asyncio.run(run())


def _async_iter(events):
    async def gen():
        for event in events:
            yield event
    return gen()


def _trajectory_response():
    from XBotv2.session.contracts import SessionTrajectoryMessage, SessionHistoryItem
    from XBotv2.session.protocol import ThreadTrajectoryResponse

    first = SessionTrajectoryMessage(
        position=1,
        message=SessionHistoryItem(role="user", content="review the diff"),
    )
    second = SessionTrajectoryMessage(
        position=2,
        message=SessionHistoryItem(
            role="assistant", content="I reviewed it.", reasoning="thinking hard"
        ),
    )
    return ThreadTrajectoryResponse(
        session_id="s", thread_id="agent-reviewer-1", items=[first, second]
    )


def test_tui_trace_writes_unicode_jsonl(tmp_path, monkeypatch):
    from XBotv2.tui.trace import trace_event

    trace_path = tmp_path / "tui-trace.jsonl"
    monkeypatch.setenv("XBOT_TUI_TRACE", str(trace_path))

    trace_event("tui.submit", {"text": "当前磁盘用了多少", "repr": repr("当前磁盘用了多少")})

    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert record["stage"] == "tui.submit"
    assert record["payload"]["text"] == "当前磁盘用了多少"


@pytest.mark.asyncio
async def test_terminal_session_trace_records_unicode_payload(tmp_path, monkeypatch):
    """The TUI HTTP boundary must preserve UTF-8 trace payloads."""

    from XBotv2.client import XBotClient

    trace_path = tmp_path / "http-trace.jsonl"
    monkeypatch.setenv("XBOT_TUI_TRACE", str(trace_path))

    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)
            self.is_success = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class FakeClient:
        def __init__(self, lines):
            self._stream = FakeStream(lines)

        async def post(self, path, json=None):
            class Resp:
                def raise_for_status(self_inner):
                    return None

                def json(self_inner):
                    return {"server_name": "xbotv2", "protocol_version": "xbotv2.v3"}

            return Resp()

        async def request(self, method, path, json=None, params=None):
            del method, path, params

            class Resp:
                is_success = True

                def json(self_inner):
                    return {
                        "session_id": "s",
                        "thread_id": "t",
                        "request_id": json["request_id"],
                        "message_id": json["request_id"],
                        "status": "started",
                    }

            return Resp()

        def stream(self, method, path, json=None, params=None, headers=None, timeout=None):
            del params, headers
            return self._stream

        async def aclose(self):
            return None

    client = XBotClient("http://127.0.0.1:4096")
    client._http = FakeClient([
        "event: assistant_message",
        "id: 1",
        "data: {\"type\":\"assistant_message\",\"data\":{\"content\":\"\\u6536\\u5230\\uff1a\\u5f53\\u524d\\u78c1\\u76d8\\u7528\\u4e86\\u591a\\u5c11\"}}",
        "",
        "event: end",
        "id: 2",
        "data: {\"type\":\"end\",\"data\":{\"status\":\"ok\"}}",
        "",
    ])

    session = TerminalSession(client=client, session_id="s", thread_id="t")
    events: list[dict[str, Any]] = []
    await session.send_message("当前磁盘用了多少")
    client._http._stream = FakeStream([
        "event: assistant_message",
        "id: 1",
        "data: {\"type\":\"assistant_message\",\"data\":{\"content\":\"\\u6536\\u5230\\uff1a\\u5f53\\u524d\\u78c1\\u76d8\\u7528\\u4e86\\u591a\\u5c11\"}}",
        "",
        "event: end",
        "id: 2",
        "data: {\"type\":\"end\",\"data\":{\"status\":\"ok\"}}",
        "",
    ])
    async for event in session.session_events():
        events.append(event)
    await session.disconnect()

    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    request_records = [
        r for r in records
        if r["stage"] == "tui.http" and r["payload"].get("stage") == "messages.request"
    ]
    assert request_records, f"missing messages.request trace, got: {records}"
    assert request_records[0]["payload"]["content"] == "当前磁盘用了多少"

    assistant_event = events[0]
    assert assistant_event["type"] == "assistant_message"
    assert assistant_event["data"]["content"] == "收到：当前磁盘用了多少"


def test_mode_tui_imports_textual_client_lazily():
    tree = ast.parse(Path("XBotv2/main.py").read_text(encoding="utf-8"))
    run_tui = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_tui"
    )
    imports = [
        node.module
        for node in ast.walk(run_tui)
        if isinstance(node, ast.ImportFrom)
    ]

    assert "XBotv2.tui.textual_client" in imports


def test_spawn_server_propagates_log_args(monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    args = argparse.Namespace(
        data_dir="data",
        provider="deepseek",
        workspace=None,
        mode="tui",
        bind="127.0.0.1",
        port=4096,
        log_level="DEBUG",
        log_file="./run.log",
        no_plugins=False,
    )

    xbot_main._spawn_server(args)

    assert "--log-level" in captured["cmd"]
    assert "DEBUG" in captured["cmd"]
    assert "--log-file" in captured["cmd"]
    assert "./run.log" in captured["cmd"]



@pytest.mark.asyncio
async def test_message_event_pops_queue_before_turn_end():
    """When the server publishes a ``message`` event (delivery signal), the
    TUI must pop the matching entry from the local queue AND append it to the
    transcript immediately — not wait for the turn to finish.

    Regression: the queue pop was tied to the per-message stream ending, so a
    folded input stayed in the queue panel until the whole turn completed.
    """

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def _submission_events(self, text):
            # Mirrors a queued/folded input: the message is published on the
            # event stream, then the stream stays quiet until turn end.
            yield {"type": "turn_started", "data": {"turn": 1}}
            await asyncio.sleep(0.1)
            yield {"type": "assistant_message", "data": {"content": "reply"}}
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

    session = FakeSession()
    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = session

    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        await pilot.pause()
        # Simulate a queued input whose delivery signal arrives while the turn
        # is still running (before turn_finished).
        app._pending_messages = {2: "queued input"}
        app.state.turn_active = True
        session._events.put_nowait({
            "type": "message",
            "data": {"id": "msg-2", "role": "user", "content": "queued input"},
        })
        await pilot.pause()
        await pilot.pause()

        assert app._pending_messages == {}, (
            "queue must clear when the message event arrives, not at turn end"
        )
        assert [m.content for m in app.state.messages if m.role == "user"] == [
            "queued input",
        ]


@pytest.mark.asyncio
async def test_textual_app_headless_preserves_message_order_and_chinese():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def send_message(self, text):
            for event in (
                {"type": "message", "data": {"id": "msg-1", "role": "user", "content": text}},
                {"type": "turn_started", "data": {"turn": 1}},
                {"type": "assistant_message", "data": {"content": f"回复：{text}"}},
                {"type": "turn_finished", "data": {"turn": 1}},
            ):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("你好")
        await app.submit_composer()
        await pilot.pause()

    assert [(message.role, message.content) for message in app.state.messages] == [
        ("user", "你好"),
        ("assistant", "回复：你好"),
    ]
    assert app.state.status == "Ready"


@pytest.mark.asyncio
async def test_textual_app_restores_session_usage_and_displays_server_command():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.commands = []

        async def connect(self):
            return {
                "session_id": "s",
                "thread_id": "t",
                "agent_name": "XBotv2",
                "context_window": 32_000,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "requests": 2,
                    "context_tokens": 8_000,
                },
                "history": [],
            }

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {
                "commands": [{
                    "name": "goal",
                    "slash": "/goal",
                    "kind": "server",
                    "description": "Set the active goal",
                    "usage": "/goal <objective>",
                    "examples": [],
                    "parameters": {},
                }]
            }

        async def run_command(self, name, args, raw, *, kind):
            self.commands.append((name, args, raw, kind))
            return {
                "data": {
                    "message": "Goal started",
                    "data": {"model_mode": ""},
                }
            }

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.state.model_mode = "high"
    session = FakeSession()
    app.session = session

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("/goal audit repo")
        await app.submit_composer()
        await pilot.pause()

    assert app.state.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "requests": 2,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "prompt_cache_write_tokens": 0,
    }
    assert app.state.context_input_tokens == 8_000
    assert app.state.model_mode == ""
    assert [(message.role, message.content) for message in app.state.messages] == [
        ("user", "/goal audit repo")
    ]
    assert session.commands == [
        ("goal", ["audit", "repo"], "/goal audit repo", "server")
    ]


@pytest.mark.asyncio
async def test_textual_app_headless_scrolls_transcript_by_keyboard():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript")
        input_widget = app.query_one("#input")

        # The transcript takes focus so the arrow keys scroll what the wheel
        # would scroll; the composer starts focused for typing.
        assert transcript.can_focus is True
        assert app.focused is input_widget

        app.state.append_message("user", "\n".join(f"line {i}" for i in range(60)))
        await app._render_new_transcript_entries()
        await pilot.pause()
        transcript.scroll_end(animate=False)
        await pilot.pause()
        bottom = transcript.scroll_y
        transcript.focus()
        await pilot.press("up")
        await pilot.pause()
        assert transcript.scroll_y < bottom
        await pilot.press("pagedown")
        await pilot.pause()
        assert transcript.scroll_y > bottom - 1
        # Escape returns to the composer instead of clearing it.
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is input_widget


@pytest.mark.asyncio
async def test_textual_app_headless_shows_usage_in_status_bar():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            del text
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message", "data": {"content": "reply"}}
            yield {
                "type": "usage",
                "data": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                    "requests": 1,
                },
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("hello")
        await app.submit_composer()
        await pilot.pause()
        status = app.query_one("#status_bar").content
        assert "tokens:20 (12 in / 8 out)" in str(status)



@pytest.mark.asyncio
async def test_textual_app_alt_c_copies_last_reply():
    """alt+c (and /copy) copy the last assistant reply as plain text,
    so text can be pulled out of the TUI even when the mouse cannot be used
    to create a selection (e.g. under a terminal that captures ctrl+shift+c).
    """

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return {}

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(120, 50)) as pilot:
        await pilot.pause()
        app.state.append_message("assistant", "**last** reply with `code`")
        await app._render_new_transcript_entries()
        await pilot.pause()
        await pilot.press("alt+c")
        await pilot.pause()
        assert "last" in app.clipboard
        assert "code" in app.clipboard
        assert "**" not in app.clipboard, "markdown markers must be stripped"
        assert "Copied" in app.state.status



@pytest.mark.asyncio
async def test_textual_app_ctrl_c_without_selection_clears_input():
    """ctrl+c with no selection keeps clearing the composer (cancel_or_quit)."""

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return {}

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(120, 50)) as pilot:
        await pilot.pause()
        app.query_one("#input").load_text("abc")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.query_one("#input").text == ""
        assert app.state.status != "Shutdown"



@pytest.mark.asyncio
async def test_textual_app_headless_handles_tool_call_delta_before_body_mount():
    """Regression for run.log: tool_call_delta must not crash when a
    pending tool widget exists but has no .body child yet.
    """

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            del text
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {
                "type": "tool_call_delta",
                "data": {
                    "tool_calls": [
                        {
                            "tool_call_id": "call_shell",
                            "index": 0,
                            "name": "shell",
                            "args_delta": "",
                        }
                    ]
                },
            }
            yield {
                "type": "tool_call_delta",
                "data": {
                    "tool_calls": [
                        {
                            "tool_call_id": "call_shell",
                            "index": 0,
                            "name": "shell",
                            "args_delta": '{"command": "df -h"}',
                        }
                    ]
                },
            }
            yield {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "call_shell",
                    "name": "shell",
                    "status": "success",
                    "content": "ok",
                },
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("run df")
        await app.submit_composer()
        for _ in range(5):
            await pilot.pause()

    assert app.state.errors == []
    assert app.state.tools["call_shell"].status == "success"
    assert app.state.tools["call_shell"].summary == "ok"


@pytest.mark.asyncio
async def test_textual_app_streaming_deltas_do_not_schedule_empty_scrolls():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            del text
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message_delta", "data": {"content": "a"}}
            yield {"type": "assistant_message_delta", "data": {"content": "b"}}
            yield {"type": "assistant_message_delta", "data": {"content": "c"}}
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()
    scheduled_refreshes = 0
    original_call_after_refresh = app.call_after_refresh

    def count_call_after_refresh(*args, **kwargs):
        nonlocal scheduled_refreshes
        scheduled_refreshes += 1
        return original_call_after_refresh(*args, **kwargs)

    app.call_after_refresh = count_call_after_refresh

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("stream")
        await app.submit_composer()
        for _ in range(5):
            await pilot.pause()

    assert app.state.messages[-1].content == "abc"
    # One scroll for the submitted user message, one for the first
    # assistant streaming entry. Later deltas update that same entry
    # in place and must not schedule empty scrolls.
    assert scheduled_refreshes == 2


@pytest.mark.asyncio
async def test_textual_app_new_entries_follow_only_when_at_bottom():
    """New transcript entries must follow the tail only when the user is
    already at the bottom; mounting entries while the user is scrolled up
    must not yank the viewport down (regression: unconditional scroll_end)."""

    from unittest.mock import PropertyMock, patch

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            del text
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message", "data": {"content": "reply"}}
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        stream = app.query_one("#transcript")
        scroll_ends = 0
        original_scroll_end = stream.scroll_end

        def spy_scroll_end(*args, **kwargs):
            nonlocal scroll_ends
            scroll_ends += 1
            original_scroll_end(*args, **kwargs)

        stream.scroll_end = spy_scroll_end

        # Scrolled up to read older content: new entries must NOT yank down.
        with patch.object(
            type(stream), "is_vertical_scroll_end", new_callable=PropertyMock,
            return_value=False,
        ):
            app.state.append_message("user", "scrolled up here")
            await app._render_new_transcript_entries()
            await pilot.pause()
            await pilot.pause()
        assert scroll_ends == 0, (
            "scroll_end must not fire while the user is not at the bottom"
        )
        assert len(stream.children) == 0, (
            "live entries should stay out of the DOM while reading older content"
        )

        # At the bottom: follow behavior is preserved.
        with patch.object(
            type(stream), "is_vertical_scroll_end", new_callable=PropertyMock,
            return_value=True,
        ):
            app.state.append_message("user", "following tail")
            await app._render_new_transcript_entries()
            await pilot.pause()
            await pilot.pause()
        assert scroll_ends >= 1, "scroll_end must fire while following the tail"


@pytest.mark.asyncio
async def test_textual_app_foldin_shows_queued_text_and_usage_once():
    """Fold-in queued message: the queued user text is shown on its own
    turn boundary, the response appears exactly once, and usage events are
    applied exactly once (no duplication from the superseded active stream)."""

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()
            self.calls = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            self.calls += 1
            if self.calls == 1:
                # Active turn, superseded by the fold-in: stream ends after
                # the tool batch without a turn boundary.
                yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": text}}
                yield {"type": "turn_started", "data": {"turn": 1}}
                yield {"type": "assistant_message", "data": {"content": "starting tool"}}
                yield {
                    "type": "usage",
                    "data": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                        "requests": 1,
                    },
                }
                yield {
                    "type": "tool_result",
                    "data": {
                        "tool_call_id": "t1",
                        "name": "shell",
                        "content": "ok",
                        "status": "completed",
                    },
                }
            else:
                yield {"type": "message", "data": {"id": "msg-2", "role": "user", "content": text}}
                yield {"type": "turn_started", "data": {"turn": 1}}
                yield {"type": "assistant_message", "data": {"content": "handled both"}}
                yield {
                    "type": "usage",
                    "data": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "total_tokens": 280,
                        "requests": 1,
                    },
                }
                yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("first")
        await app.submit_composer()
        await pilot.pause()
        input_widget.load_text("second queued")
        await app.submit_composer()
        for _ in range(6):
            await pilot.pause()

    user_texts = [m.content for m in app.state.messages if m.role == "user"]
    assert user_texts.count("second queued") == 1, (
        f"queued user text shown {user_texts.count('second queued')} times"
    )
    assistant_texts = [m.content for m in app.state.messages if m.role == "assistant"]
    assert assistant_texts.count("handled both") == 1, (
        f"fold-in response shown {assistant_texts.count('handled both')} times"
    )
    assert app.state.usage["total_tokens"] == 150 + 280, (
        f"usage double counted: {app.state.usage}"
    )




@pytest.mark.asyncio
async def test_textual_app_headless_renders_inline_permission_options():
    from textual.widgets import Button
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()
            self.permission_decision = None
            self.permission_answered = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            del text
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            # Tool must exist before permission can be linked to it
            yield {
                "type": "tool_calls_started",
                "data": {
                    "tool_calls": [
                        {"name": "shell", "args": {"command": "ls"}, "id": "c1"},
                    ]
                },
            }
            payload = {
                "request_id": "permission:c1",
                "source": "permission_system",
                "reason": "Approval: shell",
                "tool_call": {"name": "shell", "args": {"command": "ls"}, "id": "c1"},
            }
            yield {
                "type": "permission_request",
                "data": payload,
            }
            await self.permission_answered.wait()
            yield {
                "type": "permission_response_recorded",
                "data": {
                    "request_id": "permission:c1",
                    "decision": self.permission_decision["decision"],
                    "scope": self.permission_decision["scope"],
                },
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def respond_permission(self, request_id, decision, *, scope="once"):
            assert request_id == "permission:c1"
            self.permission_decision = {"decision": decision, "scope": scope}
            self.permission_answered.set()
            return {"recorded": True}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    session = FakeSession()
    app.session = session

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("run")
        await app.submit_composer()
        await pilot.pause()

        assert list(app.query(Button)) == []
        # Choice key is the tool_call_id, not a numeric notice index
        assert app._active_choice_key == "c1"
        assert "Allow" in str(app._choice_widgets["c1"].content)
        assert "Approval: shell" in app.query_one(".permission-context").content
        assert input_widget.disabled is True
        assert input_widget.display is False
        assert app.focused is None
        await pilot.press("down")
        assert app._active_choice_index == 1
        await pilot.press("up")
        assert app._active_choice_index == 0
        await pilot.press("enter")
        await pilot.pause()
        assert input_widget.disabled is False
        assert input_widget.display is True

    assert session.permission_decision == {
        "decision": "allow",
        "scope": "once",
    }


@pytest.mark.asyncio
async def test_textual_app_confirming_permission_twice_submits_once():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        # Pre-create tool so permission can be linked
        app.state.tools["c_dup"] = TuiTool(tool_call_id="c_dup", name="shell")
        app.state.apply_event({
            "type": "permission_request",
            "data": {
                "request_id": "permission:dup",
                "reason": "Approval: shell",
                "tool_call": {"name": "shell", "args": {}, "id": "c_dup"},
            },
        })
        # Mount the tool widget so choices are registered
        app.state.transcript = [TuiTranscriptEntry(kind="tool", key="c_dup")]
        await app._render_new_transcript_entries()
        await app._refresh_changed_tool_widgets()
        await pilot.pause()

        assert await app.confirm_active_choice() is True
        assert await app.confirm_active_choice() is False

    assert await app._permission_decisions.get() == {
        "decision": "allow",
        "scope": "once",
    }
    assert app._permission_decisions.empty()
    # No separate notice — tool status updates in place
    assert app.state.tools["c_dup"].status == "allow (once)"


@pytest.mark.asyncio
async def test_textual_app_starts_next_permission_after_previous_response():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.responses = []

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def respond_permission(self, request_id, decision, *, scope="once"):
            self.responses.append((request_id, decision, scope))
            if request_id == "permission:first":
                self.first_started.set()
                await self.release_first.wait()

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    session = FakeSession()
    app.session = session
    first = {
        "type": "permission_request",
        "data": {
            "request_id": "permission:first",
            "tool_call": {"id": "first", "name": "shell"},
        },
    }
    second = {
        "type": "permission_request",
        "data": {
            "request_id": "permission:second",
            "tool_call": {"id": "second", "name": "shell"},
        },
    }

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.tools = {
            "first": TuiTool(tool_call_id="first", name="shell"),
            "second": TuiTool(tool_call_id="second", name="shell"),
        }
        app.state.transcript = [
            TuiTranscriptEntry(kind="tool", key="first"),
            TuiTranscriptEntry(kind="tool", key="second"),
        ]
        await app._render_new_transcript_entries()
        app.state.apply_event(first)
        await app._handle_stream_event(first)
        await app._start_interaction_response(first)
        assert app._active_choice_key == "first"
        assert await app.confirm_active_choice() is True
        await asyncio.wait_for(session.first_started.wait(), timeout=1)

        recorded = {
            "type": "permission_response_recorded",
            "data": {
                "request_id": "permission:first",
                "decision": "allow",
                "scope": "once",
            },
        }
        app.state.apply_event(recorded)
        await app._handle_stream_event(recorded)
        app.state.apply_event(second)
        await app._handle_stream_event(second)
        start_second = asyncio.create_task(
            app._start_interaction_response(second)
        )
        await asyncio.sleep(0)
        assert not start_second.done()
        assert app._active_choice_key == "second"
        assert "second" in app._choice_widgets
        assert "first" not in app._choice_widgets

        session.release_first.set()
        await asyncio.wait_for(start_second, timeout=1)
        app._active_choice_index = 1
        assert await app.confirm_active_choice() is True
        for _ in range(10):
            if len(session.responses) == 2:
                break
            await pilot.pause()

    assert session.responses == [
        ("permission:first", "allow", "once"),
        ("permission:second", "deny", "once"),
    ]


@pytest.mark.asyncio
async def test_textual_app_cancellation_clears_pending_permission_ui():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(_frame("turn_started", {"turn": 1}))
        app.state.tools["call-cancel"] = TuiTool(
            tool_call_id="call-cancel",
            name="shell",
        )
        app.state.transcript.append(
            TuiTranscriptEntry(kind="tool", key="call-cancel")
        )
        permission_event = _frame(
            "permission_request",
            {
                "request_id": "approval-cancel",
                "tool_call": {"id": "call-cancel", "name": "shell"},
            },
        )
        app.state.apply_event(permission_event)
        await app._render_new_transcript_entries()
        await app._refresh_changed_tool_widgets()
        await app._start_interaction_response(permission_event)
        await pilot.pause()

        assert app._active_choice_key == "call-cancel"
        assert app._interaction_response_task is not None

        event = _frame("turn_cancelled", {"turn": 1, "reason": "client_interrupt"})
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        await pilot.pause()

        composer = app.query_one("#input")
        assert app._active_choice_key is None
        assert app._interaction_response_task is None
        assert app.state.pending_permission_payload is None
        assert app.state.tools["call-cancel"].status == "cancelled"
        assert composer.disabled is False
        assert composer.display is True


@pytest.mark.asyncio
async def test_textual_app_turn_finished_clears_pending_user_input_ui():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(_frame("turn_started", {"turn": 1}))
        input_event = _frame(
            "user_input_required",
            {
                "request_id": "question-finished",
                "question": "Continue?",
                "options": [
                    {"label": "yes", "description": "Continue."},
                    {"label": "no", "description": "Stop."},
                ],
            },
        )
        app.state.apply_event(input_event)
        await app._render_new_transcript_entries()
        await app._start_interaction_response(input_event)
        app._interaction_response_pending = True
        await pilot.pause()

        assert app._active_choice_key == "0"
        assert app._interaction_response_task is not None

        event = _frame("turn_finished", {"turn": 1})
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        await pilot.pause()

        composer = app.query_one("#input")
        assert app._active_choice_key is None
        assert app._interaction_response_task is None
        assert app._interaction_response_pending is False
        assert app.state.pending_user_input_payload is None
        assert app.state.status == "Ready"
        assert composer.disabled is False
        assert composer.display is True


@pytest.mark.asyncio
async def test_textual_app_headless_renders_inline_ask_user_options():
    from textual.widgets import Button
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()
            self.answer = None
            self.answer_recorded = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": text}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            payload = {
                "request_id": "user_input:c1",
                "source": "ask_user",
                "question": "继续执行？",
                "options": [
                    {"label": "继续", "description": "继续当前工作"},
                    {"label": "停止", "description": "停止当前工作"},
                ],
            }
            yield {
                "type": "user_input_required",
                "data": payload,
            }
            await self.answer_recorded.wait()
            yield {
                "type": "user_input_recorded",
                "data": {"request_id": "user_input:c1", "status": "recorded"},
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def submit_user_input(self, request_id, answer):
            assert request_id == "user_input:c1"
            self.answer = answer
            self.answer_recorded.set()
            return {"recorded": True}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    session = FakeSession()
    app.session = session

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("ask")
        await app.submit_composer()
        await pilot.pause()

        assert list(app.query(Button)) == []
        assert app._active_choice_key == "0"
        assert "继续" in str(app._choice_widgets["0"].content)
        assert [choice.kind for choice in app._choice_payloads["0"]] == [
            "answer",
            "answer",
        ]
        assert input_widget.disabled is True
        assert input_widget.display is False
        assert app.focused is None
        await pilot.press("down")
        assert app._active_choice_index == 1
        await pilot.press("up")
        assert app._active_choice_index == 0
        await pilot.press("enter")
        await pilot.pause()
        assert input_widget.disabled is False
        assert input_widget.display is True

        assert app._choice_results["0"] == "继续: 继续当前工作"
        assert [notice.kind for notice in app.state.notices] == [
            "user_input_required",
            "user_input_recorded",
        ]
        assert [message.content for message in app.state.messages] == ["ask"]

    assert session.answer == "继续"


@pytest.mark.asyncio
async def test_textual_app_records_typed_answer_without_queued_notice():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.answer = None

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def submit_user_input(self, request_id, answer):
            assert request_id == "user_input:c1"
            self.answer = answer

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    session = FakeSession()
    app.session = session

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        event = {
            "type": "user_input_required",
            "data": {
                "request_id": "user_input:c1",
                "source": "mcp",
                "question": "Codename?",
                "options": [
                    {"label": "ALPHA", "description": "Use the default name."},
                    {"label": "BETA", "description": "Use the alternate name."},
                ],
            },
        }
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        await app._start_interaction_response(event)

        app._active_choice_index = len(app._active_choices()) - 1
        assert await app.confirm_active_choice() is True

        composer = app.query_one("#input")
        assert composer.display is True
        assert composer.disabled is False
        composer.load_text("NOVA")
        await app.submit_composer()
        await pilot.pause()

        assert session.answer == "NOVA"
        assert [notice.kind for notice in app.state.notices] == [
            "user_input_required",
        ]
        assert app.state.messages == []


@pytest.mark.asyncio
async def test_textual_app_replays_tool_permission_sequence_without_swallowing_messages():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self._events = asyncio.Queue()
            self.permission_decision = None
            self.permission_answered = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": text}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {
                "type": "assistant_message",
                "data": {
                    "content": "\n\n",
                    "tool_calls": [{
                        "id": "call_shell",
                        "name": "shell",
                        "args": {"command": "df -h"},
                    }],
                },
            }
            payload = {
                "request_id": "permission:shell",
                "source": "permission_system",
                "reason": "Approval: shell",
                "tool_call": {"name": "shell", "args": {"command": "df -h"}, "id": "call_shell"},
            }
            yield {"type": "permission_request", "data": payload}
            await self.permission_answered.wait()
            yield {
                "type": "permission_response_recorded",
                "data": {
                    "request_id": "permission:shell",
                    "decision": self.permission_decision["decision"],
                    "scope": self.permission_decision["scope"],
                },
            }
            yield {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "call_shell",
                    "name": "shell",
                    "status": "success",
                    "content": "Filesystem Size Used Avail Use% Mounted on /dev/sda 242G 226G 16G 94% /",
                },
            }
            yield {
                "type": "assistant_message",
                "data": {"content": f"当前磁盘使用情况：已执行 df -h。问题是：{text}"},
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def respond_permission(self, request_id, decision, *, scope="once"):
            assert request_id == "permission:shell"
            self.permission_decision = {"decision": decision, "scope": scope}
            self.permission_answered.set()
            return {"recorded": True}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    session = FakeSession()
    app.session = session

    async with app.run_test(headless=True, size=(110, 36)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.load_text("当前磁盘用了多少")
        await app.submit_composer()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        for _ in range(3):
            await pilot.pause()
        rendered = html.unescape(app.export_screenshot(title="xbotv2-tui-replay")).replace("\xa0", " ")
        assert "Filesystem" not in rendered
        details = app.query_one(".tool-details")
        await pilot.click(details.query_one("CollapsibleTitle"))
        await pilot.pause()
        expanded = html.unescape(
            app.export_screenshot(title="xbotv2-tui-replay-expanded")
        ).replace("\xa0", " ")

    assert session.permission_decision == {"decision": "allow", "scope": "session"}
    assert [(message.role, message.content.strip()) for message in app.state.messages] == [
        ("user", "当前磁盘用了多少"),
        ("assistant", "当前磁盘使用情况：已执行 df -h。问题是：当前磁盘用了多少"),
    ]
    assert rendered.count("当前磁盘用了多少") >= 2
    assert "当前磁盘使用情况" in rendered
    assert "Filesystem" in expanded


@pytest.mark.asyncio
async def test_textual_composer_history_and_multiline_resize():
    from textual.events import Key
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        app._remember_input("first")
        app._remember_input("second")
        await input_widget._on_key(Key("up", None))
        assert input_widget.text == "second"
        await input_widget._on_key(Key("up", None))
        assert input_widget.text == "first"
        await input_widget._on_key(Key("down", None))
        assert input_widget.text == "second"
        await input_widget._on_key(Key("down", None))
        assert input_widget.text == ""
        await input_widget._on_key(Key("shift+enter", None))
        assert input_widget.text == "\n"
        assert int(input_widget.styles.height.value) >= 3


@pytest.mark.asyncio
async def test_ctrl_c_clears_nonempty_composer_then_exits_when_empty():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("draft")
        await pilot.press("ctrl+c")
        assert composer.text == ""

        with patch.object(app, "exit") as exit_app:
            await pilot.press("ctrl+c")
            exit_app.assert_called_once_with()


def test_permission_decision_parser_supports_scopes():
    assert _parse_permission_decision("session allow") == {
        "decision": "allow",
        "scope": "session",
    }
    assert _parse_permission_decision("deny once") == {
        "decision": "deny",
        "scope": "once",
    }


@pytest.mark.asyncio
async def test_terminal_session_uses_shared_events_for_turn_delivery():
    class FakeClient:
        async def hello(self, *, client_name, session_id, thread_id):
            del client_name
            return Mock(session_id=session_id, thread_id=thread_id)

        async def open_session(self, *, session_id, thread_id, workspace_root=None, mode=None):
            del workspace_root, mode
            return Mock(model_dump=lambda **_: {
                "session_id": session_id, "thread_id": thread_id, "status": "ready"
            })

        async def send_message(self, session_id, thread_id, content, *, request_id, images=None):
            del session_id, thread_id, content, images
            return None

        async def send_permission_response(self, **kwargs):
            raise AssertionError("TerminalSession must not auto-answer")

        async def send_user_input(self, **kwargs):
            raise AssertionError("TerminalSession must not auto-answer")

        async def shutdown(self, *, session_id):
            return {"status": "closed"}

        async def interrupt(self, *, session_id, thread_id):
            return {"status": "idle", "cancelled": False}

        async def close(self):
            return None

    session = TerminalSession(client=FakeClient(), session_id="s", thread_id="t")
    await session.connect()

    await session.send_message("run")


@pytest.mark.asyncio
async def test_terminal_session_passes_explicit_resume_mode():
    opened = {}

    class FakeClient:
        async def hello(self, *, client_name, session_id, thread_id):
            del client_name
            return Mock(session_id=session_id, thread_id=thread_id)

        async def open_session(self, **payload):
            opened.update(payload)
            return Mock(model_dump=lambda **_: {
                "session_id": payload["session_id"], "history": []
            })

        async def close(self):
            return None

    session = TerminalSession(
        client=FakeClient(),
        session_id="existing",
        session_mode="resume",
        agent="builder",
    )

    response = await session.connect()

    assert opened["mode"] == "resume"
    assert opened["agent"] == "builder"
    assert response["history"] == []


@pytest.mark.asyncio
async def test_terminal_session_switch_is_transactional_and_does_not_shutdown():
    class FakeClient:
        def __init__(self):
            self.fail = False
            self.shutdown_calls = 0

        async def hello(self, *, client_name, session_id, thread_id):
            del client_name
            return Mock(session_id=session_id, thread_id=thread_id)

        async def open_session(self, **payload):
            if self.fail:
                raise RuntimeError("open failed")
            return Mock(model_dump=lambda **_: payload)

        async def shutdown(self, **_payload):
            self.shutdown_calls += 1

        async def close(self):
            return None

    client = FakeClient()
    session = TerminalSession(
        client=client,
        session_id="old",
        thread_id="main",
    )
    await session.connect()
    await session.switch(
        session_id="new",
        thread_id="agent",
        workspace_root="/workspace",
    )

    assert (session.session_id, session.thread_id) == ("new", "agent")
    assert client.shutdown_calls == 0

    client.fail = True
    with pytest.raises(RuntimeError, match="open failed"):
        await session.switch(session_id="broken", thread_id="agent")

    assert (session.session_id, session.thread_id) == ("new", "agent")
    await session.disconnect()
    assert client.shutdown_calls == 0


@pytest.mark.asyncio
async def test_terminal_session_submission_does_not_open_response_stream():
    class FakeClient:
        async def send_message(self, session_id, thread_id, content, *, request_id, images=None):
            del session_id, thread_id, content, images
            return None

    session = TerminalSession(
        client=FakeClient(), session_id="s", thread_id="t"
    )

    await session.send_message("run")


def test_tui_modules_do_not_import_core():
    for path in [
        Path("XBotv2/tui/client.py"),
        Path("XBotv2/tui/session_config.py"),
        Path("XBotv2/tui/terminal.py"),
        Path("XBotv2/tui/textual_theme.py"),
        Path("XBotv2/tui/textual_client.py"),
        Path("XBotv2/tui/textual_widgets.py"),
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        assert not any(name.startswith("core") for name in imports)


def _frame(frame_type: str, payload: dict) -> dict:
    return {"type": frame_type, "data": payload}


# ----------------------------------------------------------------------
# Error visibility — v1.2 (§10.5.10)
# ----------------------------------------------------------------------


def test_tui_state_records_engine_error_event():
    """Engine-level ``error`` events (for example, provider HTTP 400 on
    ``tool_calls → tool_messages`` mismatches) must be captured on
    ``TuiState.errors`` AND surface as a transcript entry so the
    transcript shows the failure even if the user has scrolled away
    from the status bar.
    """

    state = TuiState()
    state.apply_event(
        {
            "type": "error",
            "data": {
                "code": "engine_error",
                "message": (
                    "An assistant message with 'tool_calls' must be "
                    "followed by tool messages"
                ),
            },
        }
    )

    assert state.status == "Error"
    assert state.errors == [
        "An assistant message with 'tool_calls' must be "
        "followed by tool messages"
    ]
    error_entries = [e for e in state.transcript if e.kind == "error"]
    assert len(error_entries) == 1
    # The error entry must point at the recorded error so the
    # transcript can resolve and render it.
    assert error_entries[0].key == "0"


def test_tui_state_notices_only_unrequested_compaction():
    """The plugin decides whether a compaction was unrequested."""

    state = TuiState()
    state.apply_event({
        "type": "compaction_completed",
        "data": {
            "reason": "automatic",
            "automatic": True,
            "metrics": {"history_chars_before": 40_000, "history_chars_after": 900},
        },
    })
    assert [notice.kind for notice in state.notices] == ["compact"]

    # A manual compaction is user-visible too: the transcript entry carries
    # the live summary payload for the expandable context row.
    state.apply_event({
        "type": "compaction_completed",
        "data": {
            "reason": "manual",
            "automatic": False,
            "summary": "kept requirements",
            "metrics": {"history_chars_before": 40_000, "history_chars_after": 900},
        },
    })
    compact_notices = [notice for notice in state.notices if notice.kind == "compact"]
    assert len(compact_notices) == 2
    assert compact_notices[1].payload == {"reason": "manual", "automatic": False, "summary": "kept requirements", "metrics": {"history_chars_before": 40_000, "history_chars_after": 900}}

    state.apply_event({
        "type": "compaction_failed",
        "data": {
            "reason": "context-overflow",
            "automatic": True,
            "message": "no room",
        },
    })
    assert len([notice for notice in state.notices if notice.kind == "compact"]) == 3


@pytest.mark.asyncio
async def test_textual_stream_failure_releases_active_turn():
    """A broken HTTP stream must not leave the composer stuck in Running."""

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def session_events(self):
            await asyncio.sleep(3600)
            yield {}

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        await pilot.pause()
        app.state.apply_event(_frame("turn_started", {"turn": 1}))
        await app._handle_stream_failure(
            RuntimeError("provider disconnected"), source="session event stream"
        )
        await pilot.pause()

        assert app.state.turn_active is False
        assert app.state.status == "Error"
        assert app.state.errors == [
            "session event stream failed: provider disconnected"
        ]
        assert len([entry for entry in app.state.transcript if entry.kind == "error"]) == 1

        # The POST compatibility stream can fail at the same time; don't add a
        # second terminal error for the same turn.
        await app._handle_stream_failure(
            RuntimeError("same disconnect"), source="response stream"
        )
        assert len(app.state.errors) == 1


@pytest.mark.asyncio
async def test_textual_session_events_reconnect_after_incomplete_stream():
    """A transient SSE failure resumes from the session event cursor."""

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.calls = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def session_events(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("incomplete SSE response")
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    app._session_attached = True
    app.state.apply_event(_frame("turn_started", {"turn": 1}))
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        for _ in range(20):
            await pilot.pause()
            if app.session.calls >= 2 and not app.state.turn_active:
                break

        assert app.session.calls == 2
        assert app.state.turn_active is False
        assert app.state.errors == []


@pytest.mark.asyncio
async def test_textual_session_events_recover_an_expired_cursor():
    """An evicted cursor resumes from the oldest frame the server still holds."""

    from XBotv2.client import XBotClientError
    from XBotv2.protocol import ErrorResponse
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.rewinds: list[int] = []

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        def rewind_event_cursor(self, sequence: int) -> None:
            self.rewinds.append(sequence)

        async def session_events(self):
            self.calls += 1
            if self.calls == 1:
                raise XBotClientError(409, ErrorResponse(
                    code="session_event_cursor_expired",
                    message="cursor expired",
                    details={"oldest_sequence": 7},
                    retryable=True,
                ))
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    app._session_attached = True
    app.state.apply_event(_frame("turn_started", {"turn": 1}))
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        for _ in range(20):
            await pilot.pause()
            if app.session.calls >= 2 and not app.state.turn_active:
                break

        assert app.session.calls == 2
        assert app.session.rewinds == [6]
        assert app.state.turn_active is False
        assert app.state.errors == []


@pytest.mark.asyncio
async def test_textual_session_events_rebuild_the_baseline_after_repeated_evictions():
    """A cursor that keeps expiring falls back to a fresh session snapshot."""

    from XBotv2.client import XBotClientError
    from XBotv2.protocol import ErrorResponse
    from XBotv2.tui.textual_client import XBotTextualApp

    def expired() -> XBotClientError:
        return XBotClientError(409, ErrorResponse(
            code="session_event_cursor_expired",
            message="cursor expired",
            details={"oldest_sequence": 7},
            retryable=True,
        ))

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.rewinds: list[int] = []
            self.rebuilds = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        def rewind_event_cursor(self, sequence: int) -> None:
            self.rewinds.append(sequence)

        async def refresh_baseline(self):
            self.rebuilds += 1
            return {"session_id": "s", "thread_id": "t", "history": []}

        async def session_events(self):
            self.calls += 1
            if self.calls <= 4:
                raise expired()
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    app._session_attached = True
    app.state.apply_event(_frame("turn_started", {"turn": 1}))
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        for _ in range(40):
            await pilot.pause()
            if app.session.rebuilds and not app.state.turn_active:
                break

        # Three rewind recoveries, then the session snapshot, then the turn.
        assert app.session.rewinds == [6, 6, 6]
        assert app.session.rebuilds == 1
        assert app.session.calls == 5
        assert app.state.turn_active is False
        assert app.state.errors == []


@pytest.mark.asyncio
async def test_textual_session_events_bound_baseline_rebuilds():
    """A session that never offers a usable cursor still ends in one error."""

    from XBotv2.client import XBotClientError
    from XBotv2.protocol import ErrorResponse
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.rebuilds = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        def rewind_event_cursor(self, sequence: int) -> None:
            del sequence

        async def refresh_baseline(self):
            self.rebuilds += 1
            return {"session_id": "s", "thread_id": "t", "history": []}

        async def session_events(self):
            self.calls += 1
            raise XBotClientError(409, ErrorResponse(
                code="session_event_cursor_expired",
                message="cursor expired",
                details={"oldest_sequence": 7},
                retryable=True,
            ))
            yield  # pragma: no cover — keeps this an async generator

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    app._session_attached = True
    app.state.apply_event(_frame("turn_started", {"turn": 1}))
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        for _ in range(60):
            await pilot.pause()
            if app.state.errors:
                break

        assert app.session.rebuilds == 2
        # Parallel to the bounded rewind/reconnect budgets, not an endless loop:
        # 3 rewinds + 1 rebuild, twice, then 3 reconnect attempts + the terminal
        # failure.
        assert app.session.calls <= 15
        assert app.state.turn_active is False
        assert app.state.errors


@pytest.mark.asyncio
async def test_open_session_restores_pending_interactions():
    """A reloaded client rebuilds an unanswered approval dialog."""

    from XBotv2.tui.textual_client import XBotTextualApp

    app = XBotTextualApp(session_id="s", thread_id="t")
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        await pilot.pause()
        await app._apply_open_session({
            "session_id": "s",
            "thread_id": "t",
            "history": [],
            "pending_interactions": [
                {
                    "type": "permission_request",
                    "data": {
                        "request_id": "permission:call-1",
                        "source": "permission_system",
                        "reason": "write the report",
                        "tool_call": {
                            "id": "call-1",
                            "name": "filesystem_write",
                            "args": {"path": "report.md"},
                        },
                        "resume_supported": True,
                    },
                },
            ],
        })

        assert app.state.pending_permission_payload is not None
        assert app.state.pending_permission_payload["request_id"] == "permission:call-1"


@pytest.mark.asyncio
async def test_textual_session_events_reconnect_after_unexpected_eof():
    """An SSE end before turn completion is treated as an incomplete stream."""

    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.calls = 0

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

        async def session_events(self):
            self.calls += 1
            if self.calls > 1:
                yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(session_id="s", thread_id="t")
    app.session = FakeSession()
    app._session_attached = True
    app.state.apply_event(_frame("turn_started", {"turn": 1}))
    async with app.run_test(headless=True, size=(100, 30)) as pilot:
        for _ in range(20):
            await pilot.pause()
            if app.session.calls >= 2 and not app.state.turn_active:
                break

        assert app.session.calls == 2
        assert app.state.turn_active is False
        assert app.state.errors == []


def test_tui_state_closes_failed_turn_without_hiding_error():
    state = TuiState()
    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(
        _frame("error", {"code": "engine_error", "message": "failed"})
    )
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.turn_active is False
    assert state.status == "Error"

    state.apply_event(_frame("turn_started", {"turn": 2}))

    assert state.turn_active is True
    assert state.status == "Running"


def test_tui_state_records_error_in_transcript():
    state = TuiState()
    state.apply_event(
        {
            "type": "turn_started",
            "data": {"turn": 1},
        }
    )
    state.apply_event(
        {
            "type": "error",
            "data": {
                "code": "engine_error",
                "message": "Bad tool message order",
            },
        }
    )

    assert state.errors == ["Bad tool message order"]
    assert state.transcript[-1] == TuiTranscriptEntry(kind="error", key="0")


@pytest.mark.asyncio
async def test_tui_renders_error_entry_with_error_css_class():
    """Headless TUI: when an engine ``error`` event lands, the
    transcript mounts an entry with classes ``"entry error"`` so the
    ``.error`` CSS rule (red meta + body) actually applies. This is
    the visible signal users get when a tool-call error happens
    (provider HTTP 400, sandbox rejection, etc.).

    Uses the **real** error text reported in v1.2 testing:

        Error code: 400 - {'error': {'message': "An assistant
        message with 'tool_calls' must be followed by tool messages
        ..."}}
    """

    REAL_ERROR = (
        "Error code: 400 - {'error': {'message': \"An assistant "
        "message with 'tool_calls' must be followed by tool messages "
        "responding to each 'tool_call_id'. (insufficient tool "
        "messages following tool_calls message)\", 'type': "
        "'invalid_request_error', 'param': None, 'code': "
        "'invalid_request_error'}}"
    )

    from XBotv2.tui.textual_client import XBotTextualApp

    class _ErrorSession:
        def __init__(self):
            self._events = asyncio.Queue()
            self.sent: list[str] = []

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            async for event in self._submission_events(text):
                self._events.put_nowait(event)

        async def session_events(self):
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def _submission_events(self, text):
            self.sent.append(text)
            yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {
                "type": "error",
                "data": {
                    "code": "engine_error",
                    "message": REAL_ERROR,
                },
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

        async def submit_user_input(self, r, a):
            return {}

        async def respond_permission(self, r, d, *, scope="once"):
            return {}

    session = _ErrorSession()
    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )
    app.session = session

    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        composer = app.query_one("#input")
        composer.load_text("use the shell tool three times")
        await app.submit_composer()
        # Wait for the error event to land and the transcript to render.
        for _ in range(30):
            await pilot.pause()
            if app.state.status == "Error":
                # Render pass needs an extra tick so the widget is
                # mounted with its final classes.
                await pilot.pause()
                break

        assert app.state.status == "Error"
        assert any(REAL_ERROR in e for e in app.state.errors)

        # The transcript must contain at least one DOM node with the
        # ``error`` class so the CSS rule can highlight it.
        error_widgets = list(app.query(".error"))
        assert error_widgets, "no widget with .error class in the transcript"
        # The error text lives in a child ``.body`` Static; the
        # wrapping ``.error`` Vertical has no renderable of its own.
        # Walk descendants to find the actual text.
        found = False
        for widget in error_widgets:
            for descendant in [widget, *widget.walk_children()]:
                visual = getattr(descendant, "visual", None)
                if visual is None:
                    continue
                plain = getattr(visual, "plain", "")
                if "tool_calls" in plain and "tool messages" in plain:
                    found = True
                    break
            if found:
                break
        assert found, (
            f"error text not found under any .error widget: {error_widgets!r}"
        )

        # The status bar should also show "Error" so the user can
        # see something is wrong even if the transcript is scrolled.
        from textual.widgets import Static as TStatic
        status = app.query_one("#status_bar", TStatic)
        status_text = status.visual.plain if status.visual else ""
        assert "Error" in status_text, (
            f"status bar missing Error: {status_text!r}"
        )


# ----------------------------------------------------------------------
# Permission_request must reach the TUI before the provider blocks
# ----------------------------------------------------------------------


def test_apply_event_permission_request_sets_status_to_approval_required():
    """A permission request becomes the active payload and updates its tool."""

    state = TuiState()

    state.apply_event(
        {
            "type": "permission_request",
            "data": {
                "request_id": "perm:call_1",
                "reason": "Tool 'shell' needs approval",
                "tool_call": {"name": "shell", "args": {"command": "ls"}, "id": "call_1"},
            },
        }
    )

    assert state.pending_permission_payload is not None
    assert state.pending_permission_payload["request_id"] == "perm:call_1"
    assert state.status == "Approval required"
    # Permission is attached to the tool widget, not a separate notice
    tool = state.tools["call_1"]
    assert tool.permission_pending is True
    assert tool.permission_request_id == "perm:call_1"
    assert tool.status == "pending approval"
    assert "call_1" in state._changed_tool_ids
    assert state.transcript == [
        TuiTranscriptEntry(kind="tool", key="call_1")
    ]
    # No separate notice entry is created
    notice_entries = [e for e in state.transcript if e.kind == "notice"]
    assert len(notice_entries) == 0


def test_permission_rule_request_is_rendered_without_a_tool_call():
    state = TuiState()
    payload = {
        "request_id": "permission:rule-1",
        "reason": "Allow report writes.",
        "permission": {
            "tool": "filesystem_write",
            "params": {"path": r"reports/.*"},
        },
    }

    state.apply_event({"type": "permission_request", "data": payload})

    assert state.pending_permission_payload == payload
    assert state.notices[-1].kind == "permission_request"
    assert "filesystem_write" in state.notices[-1].text
    assert state.transcript[-1].kind == "notice"


@pytest.mark.asyncio
async def test_permission_before_tool_event_still_renders_inline_choices():
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()
    event = {
        "type": "permission_request",
        "data": {
            "request_id": "permission:early",
            "reason": "Approval: shell",
            "tool_call": {
                "id": "early",
                "name": "shell",
                "args": {"command": "date"},
            },
        },
    }

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        await pilot.pause()

        assert app._active_choice_key == "early"
        assert "Allow once" in app._choice_widgets["early"].visual.plain
        assert app.state.tools["early"].args == {"command": "date"}


# ----------------------------------------------------------------------
# Usage events — flat data must update turn_usage (not just cumulative)
# ----------------------------------------------------------------------


def test_apply_usage_updates_turn_usage_from_flat_data():
    """When the engine sends ``{"input_tokens": 12, "output_tokens": 3,
    "total_tokens": 15}`` without a ``delta`` sub-key, ``turn_usage``
    must still accumulate — the activity row reads from it.
    """

    state = TuiState()
    state.apply_event({"type": "turn_started", "data": {"turn": 1}})

    # Simulate one LLM call returning 15 tokens
    state.apply_event(
        {
            "type": "usage",
            "data": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "requests": 1},
        }
    )

    assert state.turn_usage["input_tokens"] == 10
    assert state.turn_usage["output_tokens"] == 5
    assert state.turn_usage["total_tokens"] == 15
    assert state.turn_usage["requests"] == 1

    # Simulate a second LLM call in the same turn
    state.apply_event(
        {
            "type": "usage",
            "data": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28, "requests": 1},
        }
    )

    assert state.turn_usage["input_tokens"] == 30   # 10 + 20
    assert state.turn_usage["output_tokens"] == 13   # 5 + 8
    assert state.turn_usage["total_tokens"] == 43    # 15 + 28
    assert state.turn_usage["requests"] == 2
    assert state.context_input_tokens == 20
    assert state.usage == {
        "input_tokens": 30,
        "output_tokens": 13,
        "total_tokens": 43,
        "requests": 2,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "prompt_cache_write_tokens": 0,
    }


def test_usage_event_accumulates_once():

    state = TuiState()
    state.apply_event({"type": "turn_started", "data": {"turn": 1}})

    state.apply_event(
        {
            "type": "usage",
            "data": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125, "requests": 1},
        }
    )

    assert state.usage["input_tokens"] == 100
    assert state.turn_usage["input_tokens"] == 100


def test_usage_accepts_zero_context_tokens():
    state = TuiState(context_input_tokens=120)

    state.apply_event(
        {
            "type": "usage",
            "data": {
                "input_tokens": 0,
                "output_tokens": 3,
                "total_tokens": 3,
                "context_tokens": 0,
            },
        }
    )

    assert state.context_input_tokens == 0


def test_usage_prefers_effective_context_tokens():
    state = TuiState(context_input_tokens=1)

    state.apply_event({
        "type": "usage",
        "data": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "requests": 1,
            "context_tokens": 800,
        },
    })

    assert state.context_input_tokens == 800


def test_usage_accumulates_cache_read_into_session_totals():
    """Provider usage where almost all input is cache-read (e.g. deepseek)
    must still report a meaningful full-input figure: uncached + cache-read.
    """

    state = TuiState()
    state.apply_event({
        "type": "usage",
        "data": {
            "input_tokens": 0,
            "output_tokens": 215,
            "total_tokens": 9909,
            "requests": 1,
            "context_tokens": 9694,
            "cache_read_input_tokens": 9694,
        },
    })
    assert state.usage["input_tokens"] == 0
    assert state.usage["cache_read_input_tokens"] == 9694
    assert state.usage["total_tokens"] == 9909
    assert state.turn_usage["cache_read_input_tokens"] == 9694
    # The full input shown by the status bar = uncached + cache-read.
    full_input = (
        state.usage["input_tokens"] + state.usage["cache_read_input_tokens"]
    )
    assert full_input == 9694


def test_usage_accumulates_all_cache_keys():

    state = TuiState()
    state.apply_event({
        "type": "usage",
        "data": {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "requests": 1,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "prompt_cache_write_tokens": 5,
        },
    })
    assert state.usage["input_tokens"] == 100
    assert state.usage["cache_read_input_tokens"] == 40
    assert state.usage["cache_creation_input_tokens"] == 10
    assert state.usage["prompt_cache_write_tokens"] == 5


def test_usage_turn_cycle_with_cache_heavy_provider_does_not_error():
    """A deepseek-style usage event (uncached input 0, all cache-read) across
    full turn boundaries must not raise, must reset per-turn state on
    turn_started, and must keep the session total accurate. Regression: the
    turn_usage reset omitted the cache keys, so ``_apply_usage`` raised
    KeyError and the turn never reached ``turn_finished`` (stuck "Running")."""

    state = TuiState()
    for turn in (1, 2):
        state.apply_event({"type": "turn_started", "data": {"turn": turn}})
        state.apply_event({
            "type": "usage",
            "data": {
                "input_tokens": 0,
                "output_tokens": 215,
                "total_tokens": 9909,
                "requests": 1,
                "context_tokens": 9694,
                "cache_read_input_tokens": 9694,
            },
        })
        state.apply_event({"type": "turn_finished", "data": {"turn": turn}})

    assert state.errors == []
    assert state.turn_active is False
    assert state.usage["cache_read_input_tokens"] == 2 * 9694
    assert state.usage["total_tokens"] == 2 * 9909
    assert state.turn_usage["cache_read_input_tokens"] == 9694
    assert state.turn_usage["output_tokens"] == 215


# ----------------------------------------------------------------------
# Thinking: assistant_message with tool_calls but NO content
# ----------------------------------------------------------------------


def test_assistant_message_with_tool_calls_but_no_content_shows_thinking():
    """When the LLM returns tool_calls without visible text, the TUI
    does NOT insert a placeholder — the tool widget itself signals
    activity. Reasoning was already streamed via deltas if present.
    """

    state = TuiState()
    state.apply_event(
        {
            "type": "assistant_message",
            "data": {
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "name": "shell", "args": {"command": "ls"}}
                ],
            },
        }
    )

    # No placeholder message — tool widget is sufficient
    assert len(state.messages) == 0
    assert "call_1" in state.tools
    assert state.tools["call_1"].status == "pending"


def test_assistant_message_with_content_does_not_insert_thinking():
    """When the LLM includes text content (the thinking IS visible),
    do NOT insert a redundant ``Thinking…`` entry.
    """

    state = TuiState()
    state.apply_event(
        {
            "type": "assistant_message",
            "data": {"content": "Let me check the workspace first."},
        }
    )

    messages = [m.content for m in state.messages]
    assert "Let me check the workspace first." in messages
    assert "Thinking…" not in messages


def test_task_updates_replace_one_authoritative_tui_snapshot():
    state = TuiState()
    base = {
        "job_id": "task-1",
        "command": "sleep 1",
        "cwd": "/workspace",
        "created_at": 1.0,
        "started_at": 1.0,
        "finished_at": 0.0,
        "output": "",
        "error": "",
    }

    state.apply_event({"type": "job_updated", "data": {**base, "status": "running"}})
    state.apply_event({
        "type": "job_updated",
        "data": {
            **base,
            "status": "completed",
            "finished_at": 2.0,
            "output": "done",
        },
    })

    assert list(state.tasks) == ["task-1"]
    assert state.tasks["task-1"].status == "completed"
    assert state.tasks["task-1"].output == "done"

    state.apply_event({
        "type": "job_updated",
        "data": {
            **base,
            "job_id": "agent-task-1",
            "kind": "agent",
            "command": "reviewer: inspect changes",
            "status": "running",
        },
    })
    assert state.tasks["agent-task-1"].kind == "agent"

    from XBotv2.tui.textual_widgets import jobs_renderable

    rendered = jobs_renderable(
        [state.tasks["agent-task-1"]], width=100
    ).plain
    assert "agent-task-1  agent  reviewer" in rendered


@pytest.mark.asyncio
async def test_textual_job_panel_updates_in_place():
    from textual.widgets import Collapsible, Static
    from XBotv2.tui.textual_client import XBotTextualApp
    from XBotv2.tui.textual_widgets import JobListWidget

    class FakeSession:
        session_id = "s"
        thread_id = "t"

        async def connect(self):
            return {
                "session_id": "s",
                "thread_id": "t",
                "agent_name": "XBotv2",
                "workspace_root": "/workspace",
                "provider": "mock",
                "history": [],
            }

        async def list_commands(self):
            return {"commands": []}

        async def disconnect(self):
            return None

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()
    event = {
        "type": "job_updated",
        "data": {
            "job_id": "task-1",
            "command": "sleep 30",
            "cwd": "/workspace",
            "status": "running",
            "created_at": 1.0,
            "started_at": 1.0,
            "finished_at": 0.0,
            "output": "",
            "error": "",
        },
    }

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        await pilot.pause()
        from XBotv2.tui.textual_widgets import BoundedText

        panel = app.query_one("#job_panel", Collapsible)
        body = app.query_one("#job_list", JobListWidget)
        block = body.query_one(".subagent-job", Collapsible)

        assert panel.display is True
        assert panel.title == "Tasks (1 running)"
        assert "task-1" in str(block.title or "")
        block.collapsed = False
        await pilot.pause()
        assert "command: sleep 30" in block.query_one(
            ".job-detail", BoundedText
        ).text


def test_tui_state_prunes_successful_tasks_but_keeps_failures():
    state = TuiState()
    base = {
        "command": "work",
        "cwd": "",
        "created_at": 1.0,
        "started_at": 1.0,
        "finished_at": 2.0,
        "output": "done",
        "error": "",
    }
    state.apply_event({
        "type": "job_updated",
        "data": {**base, "job_id": "done", "status": "completed"},
    })
    state.apply_event({
        "type": "job_updated",
        "data": {
            **base,
            "job_id": "failed",
            "status": "failed",
            "error": "boom",
        },
    })
    terminal_since = state.tasks["done"].terminal_since

    assert state.prune_finished_tasks(now=terminal_since + 2.9) is False
    assert state.prune_finished_tasks(now=terminal_since + 3.0) is True
    assert list(state.tasks) == ["failed"]


@pytest.mark.asyncio
async def test_subagent_task_is_expandable_with_scrollable_fixed_body():
    from textual.containers import VerticalScroll
    from XBotv2.tui.textual_client import XBotTextualApp
    from XBotv2.tui.textual_widgets import (
        BoundedText,
        SubagentJobWidget,
        JobListWidget,
    )

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    task = TuiJob(
        job_id="agent-task-1",
        kind="agent",
        command="reviewer: inspect changes",
        status="completed",
        agent="reviewer",
        thread_id="agent-reviewer-1",
        output="\n".join(f"line {index}" for index in range(20)),
        usage={"total_tokens": 12_500},
    )
    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(80, 18)) as pilot:
        from XBotv2.tui.textual_widgets import BoundedText

        widget = app.query_one("#job_list", JobListWidget)
        widget.update_jobs([task], width=80)
        await pilot.pause()
        subagent = widget.query_one(SubagentJobWidget)
        assert subagent.collapsed is True
        assert "reviewer" in str(subagent.title or "")
        subagent.collapsed = False
        await pilot.pause()
        detail = subagent.query_one(".job-detail", BoundedText)
        assert "command: reviewer: inspect changes" in detail.text
        assert "line 0" in detail.text and "line 19" in detail.text
        assert detail.line_count >= 20
        assert detail.size.height <= 8


@pytest.mark.asyncio
async def test_job_panel_refreshes_in_place_without_collapsing_expanded_rows():
    from XBotv2.tui.textual_client import XBotTextualApp
    from XBotv2.tui.textual_widgets import (
        BoundedText,
        SubagentJobWidget,
        JobListWidget,
    )

    class FakeSession:
        session_id = "s"
        thread_id = "t"

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def list_commands(self):
            return {"commands": []}

    task = TuiJob(
        job_id="agent-task-1",
        kind="agent",
        command="reviewer: inspect changes",
        status="running",
        agent="reviewer",
        thread_id="agent-reviewer-1",
        output="line 0",
    )
    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(80, 18)) as pilot:
        widget = app.query_one("#job_list", JobListWidget)
        widget.update_jobs([task], width=80)
        await pilot.pause()
        subagent = widget.query_one(SubagentJobWidget)
        subagent.collapsed = False
        await pilot.pause()

        # A same-task refresh must reuse the mounted widget and keep the
        # reader's expansion state.
        widget.update_jobs([task], width=80)
        await pilot.pause()
        assert widget.query_one(SubagentJobWidget) is subagent
        assert subagent.collapsed is False

        # Real task changes still update the existing widget in place.
        task.output = "line 0\nline 1"
        widget.update_jobs([task], width=80)
        await pilot.pause()
        assert widget.query_one(SubagentJobWidget) is subagent
        detail = subagent.query_one(".job-detail", BoundedText)
        assert "line 1" in detail.text


@pytest.mark.asyncio
async def test_narrow_job_panel_does_not_overlap_status_or_composer():
    from textual.widgets import Collapsible
    from XBotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        session_id = "s"
        thread_id = "t"

        async def connect(self):
            return {
                "session_id": "s",
                "thread_id": "t",
                "agent_name": "XBotv2",
                "workspace_root": "/workspace",
                "provider": "mock",
                "history": [],
            }

        async def list_commands(self):
            return {"commands": []}

        async def disconnect(self):
            return None

    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(40, 18)) as pilot:
        await pilot.pause()
        for index in range(6):
            event = {
                "type": "job_updated",
                "data": {
                    "job_id": f"task-{index}",
                    "command": "long command argument " * 10,
                    "cwd": "/workspace",
                    "status": "running" if index == 0 else "completed",
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "finished_at": 0.0 if index == 0 else 2.0,
                    "output": "long output " * 20,
                    "error": "",
                },
            }
            app.state.apply_event(event)
            await app._handle_stream_event(event)
        await pilot.pause()

        tasks = app.query_one("#job_panel", Collapsible)
        status = app.query_one("#status_bar")
        composer = app.query_one("#composer")

        assert tasks.region.height <= 9
        assert tasks.region.bottom <= composer.region.y
        assert composer.region.bottom == status.region.y
        assert status.region.bottom == app.size.height


class _ReplayFakeSession:
    def __init__(self):
        self.history = []

    async def connect(self):
        return {"history": self.history}

    async def disconnect(self):
        return None

    async def list_commands(self):
        return {"commands": []}

    async def send_message(self, text):
        return [event async for event in self._submission_events(text)]

    async def _submission_events(self, text):
        yield {"type": "message", "data": {"id": "msg-1", "role": "user", "content": "queued"}}
        yield {"type": "turn_started", "data": {"turn": 1}}
        yield {"type": "assistant_message", "data": {"content": "ok"}}
        yield {"type": "turn_finished", "data": {"turn": 1}}

    async def session_events(self):
        if False:
            yield {}


@pytest.mark.asyncio
async def test_replay_window_mounts_only_tail_then_lazy_loads():
    from textual.containers import VerticalScroll
    from textual.containers import VerticalScroll
    from XBotv2.tui.textual_client import (
        XBotTextualApp,
        _MAX_MOUNTED_ENTRIES,
        _REPLAY_BATCH,
        _REPLAY_WINDOW,
    )

    session = _ReplayFakeSession()
    # 120 messages: 60 user + 60 assistant
    session.history = [
        {"role": "user", "content": f"msg {i}"}
        for i in range(60)
    ] + [
        {"role": "assistant", "content": f"ans {i}"}
        for i in range(60)
    ]
    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = session
    async with app.run_test(headless=True, size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # restore_history creates 60 user + 60 assistant = 120 transcript entries
        assert len(app.state.transcript) == 120
        # The tail window IS mounted after resume (regression: the window must
        # not be empty), bounded to the replay window.
        stream = app.query_one("#transcript", VerticalScroll)
        mounted = len(list(stream.children))
        assert mounted == _REPLAY_WINDOW, f"expected {_REPLAY_WINDOW}, got {mounted}"
        assert app._window_start == 120 - _REPLAY_WINDOW
        assert app._window_end == 120
        # Lazy load: simulate scroll-to-top (shifts the window earlier)
        await app._load_earlier_replay()
        await pilot.pause()
        assert app._window_start <= 120 - _REPLAY_WINDOW - _REPLAY_BATCH
        assert app._window_end - app._window_start <= _MAX_MOUNTED_ENTRIES
        assert len(list(stream.children)) <= _MAX_MOUNTED_ENTRIES + 1
        # The newest entries dropped from the far end are re-mountd when the
        # user scrolls back to the bottom.
        await app._load_newer_replay()
        await pilot.pause()
        assert app._window_end == 120
        assert app._window_end - app._window_start <= _MAX_MOUNTED_ENTRIES


@pytest.mark.asyncio
async def test_replay_window_scrolls_all_the_way_to_the_beginning():
    """Scrolling up repeatedly through a long history must eventually reach
    the very first entry, keep the mounted window bounded, and never lose
    contiguity (no gaps between batches)."""

    from textual.containers import VerticalScroll
    from XBotv2.tui.textual_client import (
        XBotTextualApp,
        _MAX_MOUNTED_ENTRIES,
        _REPLAY_WINDOW,
    )

    session = _ReplayFakeSession()
    # 300 entries: 150 user + 150 assistant
    session.history = [
        {"role": "user", "content": f"msg {i}"}
        for i in range(150)
    ] + [
        {"role": "assistant", "content": f"ans {i}"}
        for i in range(150)
    ]
    app = XBotTextualApp(session_id="s", thread_id="t", workspace_root=".")
    app.session = session
    async with app.run_test(headless=True, size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert len(app.state.transcript) == 300
        stream = app.query_one("#transcript", VerticalScroll)
        assert app._window_start == 300 - _REPLAY_WINDOW
        # Scroll up in batches until the beginning is reached.
        guard = 0
        while app._window_start > 0:
            await app._load_earlier_replay()
            await pilot.pause()
            # The window is contiguous and bounded at every step.
            assert app._window_end - app._window_start <= _MAX_MOUNTED_ENTRIES
            guard += 1
            assert guard < 50, "scroll-up never reached the beginning"
        # The very first entries are mounted and reachable.
        first_texts = []
        for widget in stream.children:
            if widget.parent is stream:
                first_texts.append(str(getattr(widget, "renderable", ""))[:40])
        assert app._window_start == 0
        assert len(list(stream.children)) <= _MAX_MOUNTED_ENTRIES + 1
        # Scrolling back to the bottom re-mounts the newest tail in batches,
        # again bounded at every step.
        guard = 0
        while app._window_end < 300:
            await app._load_newer_replay()
            await pilot.pause()
            assert app._window_end - app._window_start <= _MAX_MOUNTED_ENTRIES
            guard += 1
            assert guard < 50, "scroll-down never re-mounted the tail"
        assert app._window_end == 300


@pytest.mark.asyncio
async def test_provider_command_keeps_local_picker_and_remote_view():
    """``/provider`` (no args) opens the client picker the user requires;
    every other form is forwarded so the server owns list/status rendering."""
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _pick_provider = AsyncMock()
        _dispatch_remote_command = AsyncMock()

    handler = Handler()
    app = XBotTextualApp

    await app._cmd_provider(handler, CommandSpec(
        name="provider", kind="client", description="provider", raw="/provider", args="",
    ))
    handler._pick_provider.assert_awaited_once()
    handler._dispatch_remote_command.assert_not_awaited()

    handler._pick_provider.reset_mock()
    await app._cmd_provider(handler, CommandSpec(
        name="provider", kind="client", description="provider",
        raw="/provider list", args="list",
    ))
    handler._pick_provider.assert_not_awaited()
    handler._dispatch_remote_command.assert_awaited_once()

    # The removed client alias is no longer special-cased: it forwards too.
    handler._dispatch_remote_command.reset_mock()
    await app._cmd_provider(handler, CommandSpec(
        name="provider", kind="client", description="provider",
        raw="/provider ls", args="ls",
    ))
    handler._dispatch_remote_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_catalog_session_change_applies_the_readable_title():
    """The Web-style catalog channel updates the attached session identity."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from XBotv2.tui.client import TuiState
    from XBotv2.tui.textual_client import XBotTextualApp

    handler = SimpleNamespace(
        state=TuiState(session_id="session-1", session_title="session-1"),
        _refresh_all=Mock(),
        _log_trace_title=Mock(),
    )

    await XBotTextualApp._apply_catalog_event(handler, {
        "type": "catalog/session-changed",
        "data": {"session": {
            "session_id": "session-1",
            "title": "Python GIL 讨论",
        }},
    })

    assert handler.state.session_title == "Python GIL 讨论"
    handler._refresh_all.assert_called_once()


@pytest.mark.asyncio
async def test_turn_end_refresh_applies_the_captioned_title():
    """The session descriptor is the single source of identity fields: the
    turn-end refresh makes a title written mid-session (caption) visible."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from XBotv2.tui.client import TuiState
    from XBotv2.tui.textual_client import XBotTextualApp

    class Handler:
        session = SimpleNamespace(
            refresh_descriptor=AsyncMock(return_value={
                "title": "Python GIL 讨论",
                "agent_name": "default",
                "provider": "minimax",
                "model": "MiniMax-M2",
                "model_mode": "",
                "context_window": 200000,
            }),
        )
        state = TuiState(session_title="session-1")
        _apply_status_slots = XBotTextualApp._apply_status_slots
        _refresh_all = Mock()
        _refresh_status_now = Mock()

    handler = Handler()
    await XBotTextualApp._refresh_session_identity(handler)

    assert handler.state.session_title == "Python GIL 讨论"
    assert handler.state.provider == "minimax"
    assert handler.state.model == "MiniMax-M2"
    assert handler.state.context_window == 200000
    handler._refresh_all.assert_called_once()


def _assistant_message(state: TuiState, message_id: str, content: str) -> None:
    state.apply_event({
        "type": "assistant_message",
        "data": {"id": message_id, "content": content, "tool_calls": []},
    })


def test_state_window_evicts_oldest_payloads_and_keeps_keys_aligned():
    """The retained conversation is a window, not a copy of the session."""
    state = TuiState()
    total = _MAX_STATE_MESSAGES + _TRIM_SLACK + 50
    for index in range(total):
        _assistant_message(state, f"m{index}", f"answer {index}")

    assert len(state.messages) <= _MAX_STATE_MESSAGES + _TRIM_SLACK
    assert len(state.transcript) <= _MAX_STATE_TRANSCRIPT + _TRIM_SLACK
    assert state.evicted_messages > 0
    # The newest output is always retained, and the oldest retained payload is
    # the one the front of the window points at: no gap, no duplicate.
    assert state.messages[-1].message_id == f"m{total - 1}"
    assert state.messages[0].message_id == f"m{total - len(state.messages)}"

    # Every retained transcript key still resolves to the payload it was
    # created for; a shifted key that resolved to a neighbour would render one
    # message's text under another's identity.
    for entry in state.transcript:
        if entry.kind != "message":
            continue
        message = state.messages[int(entry.key)]
        assert message.content == f"answer {message.message_id[1:]}"


def test_state_window_bounds_notices_and_errors():
    state = TuiState()
    for index in range(_MAX_STATE_NOTICES + _TRIM_SLACK + 20):
        state.append_notice("local", f"notice {index}")
    assert len(state.notices) <= _MAX_STATE_NOTICES + _TRIM_SLACK
    assert state.notices[-1].text == f"notice {_MAX_STATE_NOTICES + _TRIM_SLACK + 19}"
    for entry in state.transcript:
        if entry.kind == "notice":
            assert state.notices[int(entry.key)].text.startswith("notice ")


def test_state_window_keeps_pending_tools_and_drops_terminal_ones():
    state = TuiState()
    for index in range(_MAX_STATE_TOOLS + _TRIM_SLACK + 20):
        state.apply_event({
            "type": "tool_calls_started",
            "data": {"tool_calls": [{"id": f"call_{index}", "name": "shell", "args": {}}]},
        })
        state.apply_event({
            "type": "tool_result",
            "data": {"tool_call_id": f"call_{index}", "name": "shell", "content": "ok", "status": "success"},
        })
    assert len(state.tools) <= _MAX_STATE_TOOLS + _TRIM_SLACK
    assert f"call_{_MAX_STATE_TOOLS + _TRIM_SLACK + 19}" in state.tools

    # A tool that is still running is never evicted, however old it is.
    state.apply_event({
        "type": "tool_calls_started",
        "data": {"tool_calls": [{"id": "call_pending", "name": "shell", "args": {}}]},
    })
    for index in range(_TRIM_SLACK + 10):
        _assistant_message(state, f"later{index}", "text")
    assert state.tools["call_pending"].status == "pending"


def test_state_window_keeps_streaming_index_on_the_same_message():
    """Evicting old messages must not re-point the streaming message."""
    state = TuiState()
    for index in range(_MAX_STATE_MESSAGES + _TRIM_SLACK + 5):
        _assistant_message(state, f"m{index}", f"answer {index}")
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "more"}})
    assert state._streaming_assistant_index is not None
    streaming = state.messages[state._streaming_assistant_index]
    assert streaming.streaming is True
    assert streaming.content.endswith("more")

    # Drive far past the cap again with a live stream open, then assert the
    # index still addresses the streaming message.
    for index in range(_TRIM_SLACK + 60):
        _assistant_message(state, f"n{index}", f"later {index}")
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "!"}})
    streaming = state.messages[state._streaming_assistant_index]
    assert streaming.content.endswith("!")
