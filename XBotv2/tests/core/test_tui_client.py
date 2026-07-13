"""Tests for protocol-driven TUI client state."""

import ast
import argparse
import asyncio
import html
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import xbotv2.__main__ as xbot_main
from xbotv2.tui.client import TuiState, TuiTool, TuiTranscriptEntry, _parse_permission_decision, _repair_mojibake
from xbotv2.tui.terminal import TerminalSession
from xbotv2.tui.command import CommandSpec
from xbotv2.tui.textual_state import (
    queue_user_message,
    render_transcript_entry,
    route_submitted_text,
)


@pytest.mark.asyncio
async def test_clear_dispatch_distinguishes_client_and_server_commands():
    from xbotv2.tui.textual_client import XBotTextualApp

    class Handler:
        _cmd_clear = AsyncMock()
        _run_server_command = AsyncMock()

    handler = Handler()
    await XBotTextualApp._handle_slash_command(handler, CommandSpec(
        name="clear", kind="client", description="clear", raw="/clear",
    ))
    handler._cmd_clear.assert_awaited_once()
    handler._run_server_command.assert_not_awaited()

    handler._cmd_clear.reset_mock()
    await XBotTextualApp._handle_slash_command(handler, CommandSpec(
        name="clear", kind="server", description="clear history", raw="/clear",
    ))
    handler._cmd_clear.assert_not_awaited()
    handler._run_server_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_command_replaces_tui_history_from_protocol_field():
    from xbotv2.tui.textual_client import XBotTextualApp

    history = [{"role": "user", "content": "kept"}]

    class Handler:
        _connected = True
        session = type("Session", (), {"run_command": AsyncMock(return_value={
            "type": "command_result",
            "data": {
                "command": "undo",
                "status": "ok",
                "message": "Removed 1 conversation turn.",
                "data": {"removed_turns": 1},
                "history": history,
            },
        })})()
        state = TuiState()
        _cmd_clear = AsyncMock()
        _render_new_transcript_entries = AsyncMock()
        _append_local_notice = AsyncMock()

        def _record_error(self, error):
            raise AssertionError(error)

    handler = Handler()
    await XBotTextualApp._run_server_command(handler, CommandSpec(
        name="undo", kind="server", description="undo", raw="/undo",
    ))

    handler._cmd_clear.assert_awaited_once()
    assert [(message.role, message.content) for message in handler.state.messages] == [
        ("user", "kept"),
    ]
    handler._append_local_notice.assert_awaited_once_with(
        "/undo", "Removed 1 conversation turn."
    )


def test_tui_state_applies_protocol_events_and_renders_lines():
    state = TuiState()
    frames = [
        _frame("hello_ok", {"server_name": "xbotv2"}),
        _frame("session_ready", {"agent_name": "TestBot"}),
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
        _frame("usage", {"total": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "requests": 1}}),
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

    rendered = "\n".join(state.lines(width=80, height=12))
    assert "TestBot> hello world" in rendered
    assert "Tool filesystem_read [success]" in rendered
    assert "cached result" in rendered
    assert "Notice> heads up" in rendered
    assert "Tokens 15" in rendered


def test_tui_state_applies_usage_totals():
    state = TuiState()

    state.apply_event(_frame("usage", {"total": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "requests": 2}}))

    assert state.usage == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "requests": 2,
    }


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
            "tool_calls": [
                {"id": "call_1", "name": "filesystem_read", "args": {"path": "a.txt"}}
            ],
        },
        {
            "role": "tool",
            "content": "contents",
            "tool_call_id": "call_1",
            "status": "success",
        },
        {"role": "assistant", "content": "done"},
    ])

    assert state.turn == 1
    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "read it"),
        ("assistant", "reading"),
        ("assistant", "done"),
    ]
    assert state.tools["call_1"].name == "filesystem_read"
    assert state.tools["call_1"].status == "success"
    assert state.tools["call_1"].summary == "contents"


def test_tui_state_appends_assistant_deltas_to_one_message():
    state = TuiState()

    state.apply_event({"type": "turn_started", "data": {"turn": 1}})
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "Hel"}})
    state.apply_event({"type": "assistant_message_delta", "data": {"content": "lo"}})
    state.apply_event({"type": "assistant_message", "data": {"content": "Hello"}})

    assert [(m.role, m.content) for m in state.messages] == [("assistant", "Hello")]
    assert [entry.kind for entry in state.transcript] == ["message"]


def test_repair_mojibake_restores_chinese_text():
    mojibake = "完成一个纯Python".encode("utf-8").decode("latin-1")

    assert _repair_mojibake(mojibake) == "完成一个纯Python"


def test_tui_state_repairs_mojibake_messages():
    state = TuiState()

    state.append_message("user", "完成一个纯Python".encode("utf-8").decode("latin-1"))

    assert state.messages[0].content == "完成一个纯Python"


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
        _frame("user_input_required", {"question": "Proceed?", "options": ["yes", "no"]})
    )
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"
    assert state.pending_user_input_payload is None
    assert state.notices[-1].kind == "user_input_required"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Question> Proceed?" in rendered
    assert "Options:" not in rendered


def test_tui_state_turn_finished_clears_pending_but_preserves_denial_status():
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
    assert state.tools["call_req"].status == "cancelled"

    state.apply_event(_frame("turn_started", {"turn": 2}))
    state.apply_event(
        _frame("permission_denied", {"request_id": "perm:call_deny", "tool_call": {"id": "call_deny"}})
    )
    # No matching tool for this denied call, status just flips
    state.apply_event(_frame("turn_finished", {"turn": 2}))

    assert state.status == "Permission denied"


def test_tui_state_renders_interaction_response_acknowledgements():
    state = TuiState()

    state.apply_event(_frame("user_input_required", {"question": "Proceed?"}))
    state.apply_event(_frame("user_input_recorded", {"request_id": "user_input:c1"}))

    assert state.status == "Ready"
    assert state.notices[-1].kind == "user_input_recorded"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Answer> user_input:c1" in rendered

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


def test_tui_state_permission_denied_resets_on_next_turn():
    state = TuiState()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame("permission_denied", {"reason": "no"}))
    state.apply_event(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Permission denied"

    state.apply_event(_frame("turn_started", {"turn": 2}))

    assert state.status == "Running"


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
    answers: asyncio.Queue[str] = asyncio.Queue()
    permission_decisions: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    state.apply_event(_frame("turn_started", {"turn": 1}))
    state.apply_event(_frame(request_type, request_data))
    state.apply_event(_frame(terminal_type, terminal_data))

    assert state.pending_user_input_payload is None
    assert state.pending_permission_payload is None
    assert state.status == expected_status
    assert (
        route_submitted_text(state, answers, permission_decisions, "next")
        == "message"
    )
    assert answers.empty()
    assert permission_decisions.empty()
    assert state.tools["call-1"].permission_pending is False


@pytest.mark.asyncio
async def test_textual_queues_user_messages_without_reordering_transcript():
    state = TuiState()
    messages: asyncio.Queue[str] = asyncio.Queue()

    queue_user_message(state, messages, "first")
    queue_user_message(state, messages, "second")

    assert state.messages == []
    state.append_message("user", await messages.get())
    state.append_message("assistant", "reply")
    state.append_message("user", await messages.get())

    assert [(message.role, message.content) for message in state.messages] == [
        ("user", "first"),
        ("assistant", "reply"),
        ("user", "second"),
    ]


def test_textual_transcript_rendering_preserves_chinese_and_markup_chars():
    state = TuiState(agent_name="助手")
    state.append_message("user", "你好 [不要解析] 中文")
    state.append_message("assistant", "收到：中文正常显示")

    first = render_transcript_entry(state, state.transcript[0])
    second = render_transcript_entry(state, state.transcript[1])

    assert first is not None
    assert second is not None
    assert first.plain == "You\n你好 [不要解析] 中文"
    assert second.plain == "助手\n收到：中文正常显示"


def test_tui_trace_writes_unicode_jsonl(tmp_path, monkeypatch):
    from xbotv2.tui.trace import trace_event

    trace_path = tmp_path / "tui-trace.jsonl"
    monkeypatch.setenv("XBOTV2_TUI_TRACE", str(trace_path))

    trace_event("tui.submit", {"text": "当前磁盘用了多少", "repr": repr("当前磁盘用了多少")})

    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert record["stage"] == "tui.submit"
    assert record["payload"]["text"] == "当前磁盘用了多少"


@pytest.mark.asyncio
async def test_http_transport_trace_records_unicode_payload(tmp_path, monkeypatch):
    """HttpTransport must preserve UTF-8 payload in tui.http trace events.

    Replaces the legacy ``test_protocol_trace_records_unicode_frames``
    stdio test now that stdio is removed (docsv2 v2.2).
    """

    from xbotv2.tui.transport_http import HttpTransport

    trace_path = tmp_path / "http-trace.jsonl"
    monkeypatch.setenv("XBOTV2_TUI_TRACE", str(trace_path))

    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)

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
                    return {"server_name": "xbotv2", "protocol_version": "xbotv2.v1"}

            return Resp()

        def stream(self, method, path, json=None, timeout=None):
            return self._stream

        async def aclose(self):
            return None

    client = HttpTransport("http://127.0.0.1:4096")
    client._client = FakeClient([
        "event: assistant_message",
        "id: 1",
        "data: {\"type\":\"assistant_message\",\"data\":{\"content\":\"\\u6536\\u5230\\uff1a\\u5f53\\u524d\\u78c1\\u76d8\\u7528\\u4e86\\u591a\\u5c11\"}}",
        "",
        "event: end",
        "id: 2",
        "data: {\"type\":\"end\",\"data\":{\"status\":\"ok\"}}",
        "",
    ])

    events: list[dict[str, Any]] = []
    async for event in client.send_message(
        session_id="s", content="当前磁盘用了多少", request_id="r",
    ):
        events.append(event)
    await client.close()

    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    request_records = [
        r for r in records
        if r["stage"] == "tui.http" and r["payload"].get("stage") == "messages.request"
    ]
    assert request_records, f"missing messages.request trace, got: {records}"
    assert request_records[0]["payload"]["body"]["content"] == "当前磁盘用了多少"

    assistant_event = events[0]
    assert assistant_event["type"] == "assistant_message"
    assert assistant_event["data"]["content"] == "收到：当前磁盘用了多少"


def test_mode_tui_imports_textual_client_lazily():
    tree = ast.parse(Path("XBotv2/xbotv2/__main__.py").read_text(encoding="utf-8"))
    run_tui = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_tui"
    )
    imports = [
        node.module
        for node in ast.walk(run_tui)
        if isinstance(node, ast.ImportFrom)
    ]

    assert "xbotv2.tui.textual_client" in imports


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
async def test_textual_routes_submitted_text_to_live_user_input_queue():
    state = TuiState()
    answers: asyncio.Queue[str] = asyncio.Queue()
    permission_decisions: asyncio.Queue[str] = asyncio.Queue()
    state.apply_event({
        "type": "user_input_required",
        "data": {"request_id": "user_input:c1", "question": "Proceed?"},
    })

    route = route_submitted_text(state, answers, permission_decisions, "yes")

    assert route == "user_input"
    assert await answers.get() == "yes"
    assert permission_decisions.empty()
    assert state.messages == []


@pytest.mark.asyncio
async def test_textual_routes_submitted_text_to_live_permission_queue():
    state = TuiState()
    answers: asyncio.Queue[str] = asyncio.Queue()
    permission_decisions: asyncio.Queue[str] = asyncio.Queue()
    state.apply_event({
        "type": "permission_request",
        "data": {"request_id": "permission:c1", "reason": "approve?"},
    })

    route = route_submitted_text(state, answers, permission_decisions, "y")

    assert route == "permission"
    assert await permission_decisions.get() == {"decision": "allow", "scope": "once"}
    assert answers.empty()
    assert state.messages == []


@pytest.mark.asyncio
async def test_textual_app_headless_preserves_message_order_and_chinese():
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message", "data": {"content": f"回复：{text}"}}
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
        input_widget.load_text("你好")
        await app.submit_composer()
        await pilot.pause()

    assert [(message.role, message.content) for message in app.state.messages] == [
        ("user", "你好"),
        ("assistant", "回复：你好"),
    ]
    assert app.state.status == "Ready"


@pytest.mark.asyncio
async def test_textual_app_headless_keeps_transcript_non_focusable():
    from xbotv2.tui.textual_client import XBotTextualApp

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

        assert transcript.can_focus is False
        assert app.focused is input_widget


@pytest.mark.asyncio
async def test_textual_app_headless_shows_usage_in_status_bar():
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            del text
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message", "data": {"content": "reply"}}
            yield {
                "type": "usage",
                "data": {
                    "total": {
                        "input_tokens": 12,
                        "output_tokens": 8,
                        "total_tokens": 20,
                        "requests": 1,
                    }
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
        assert "usage req:1 in:12 out:8 total:20" in str(status)


@pytest.mark.asyncio
async def test_textual_app_headless_handles_tool_call_delta_before_body_mount():
    """Regression for run.log: tool_call_delta must not crash when a
    pending tool widget exists but has no .body child yet.
    """

    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            del text
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
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            del text
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
async def test_textual_app_headless_renders_inline_permission_options():
    from textual.widgets import Button
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.permission_decision = None
            self.permission_answered = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            del text
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
    from xbotv2.tui.textual_client import XBotTextualApp

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
async def test_textual_app_cancellation_clears_pending_permission_ui():
    from xbotv2.tui.textual_client import XBotTextualApp

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
        app._start_interaction_response(permission_event)
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
    from xbotv2.tui.textual_client import XBotTextualApp

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
                "options": ["yes", "no"],
            },
        )
        app.state.apply_event(input_event)
        await app._render_new_transcript_entries()
        app._start_interaction_response(input_event)
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
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.answer = None
            self.answer_recorded = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            del text
            yield {"type": "turn_started", "data": {"turn": 1}}
            payload = {
                "request_id": "user_input:c1",
                "question": "继续执行？",
                "options": ["继续", "停止"],
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

        assert app._choice_results["0"] == "继续"
        assert [notice.kind for notice in app.state.notices] == [
            "user_input_required",
            "user_input_recorded",
        ]
        assert [message.content for message in app.state.messages] == ["ask"]

    assert session.answer == "继续"


@pytest.mark.asyncio
async def test_textual_app_records_typed_answer_without_queued_notice():
    from xbotv2.tui.textual_client import XBotTextualApp

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
            "data": {"request_id": "user_input:c1", "question": "Codename?"},
        }
        app.state.apply_event(event)
        await app._handle_stream_event(event)
        app._start_interaction_response(event)

        composer = app.query_one("#input")
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
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        def __init__(self):
            self.permission_decision = None
            self.permission_answered = asyncio.Event()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
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

    assert session.permission_decision == {"decision": "allow", "scope": "session"}
    assert [(message.role, message.content.strip()) for message in app.state.messages] == [
        ("user", "当前磁盘用了多少"),
        ("assistant", "当前磁盘使用情况：已执行 df -h。问题是：当前磁盘用了多少"),
    ]
    assert rendered.count("当前磁盘用了多少") >= 2
    assert "当前磁盘使用情况" in rendered
    assert "Filesystem" in rendered


@pytest.mark.asyncio
async def test_textual_composer_history_and_multiline_resize():
    from textual.events import Key
    from xbotv2.tui.textual_client import XBotTextualApp

    app = XBotTextualApp(
        session_id="s",
        thread_id="t",
        workspace_root=".",
    )

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
async def test_terminal_session_only_yields_live_interaction_events():
    class FakeTransport:
        async def hello(self, *, session_id, thread_id):
            return {"session_id": session_id, "thread_id": thread_id}

        async def open_session(self, *, session_id, thread_id, workspace_root=None, mode=None):
            del workspace_root, mode
            return {"session_id": session_id, "thread_id": thread_id, "status": "ready"}

        def send_message(self, *, session_id, content, request_id):
            async def _events():
                yield {"type": "turn_started", "data": {"turn": 1}}
                yield {
                    "type": "permission_request",
                    "data": {"request_id": "permission:c1", "reason": "approve?"},
                }
                yield {
                    "type": "user_input_required",
                    "data": {"request_id": "user_input:c2", "question": "continue?"},
                }
                yield {"type": "turn_finished", "data": {"turn": 1}}
                yield {"type": "end", "data": {"status": "ok"}}

            return _events()

        async def send_permission_response(self, **kwargs):
            raise AssertionError("TerminalSession must not auto-answer")

        async def send_user_input(self, **kwargs):
            raise AssertionError("TerminalSession must not auto-answer")

        async def shutdown(self, *, session_id):
            return {"status": "closed"}

        async def interrupt(self, *, session_id):
            return {"status": "idle", "cancelled": False}

        async def close(self):
            return None

    session = TerminalSession(transport=FakeTransport(), session_id="s", thread_id="t")
    await session.connect()

    events = [event async for event in session.send_message("run")]

    assert [event["type"] for event in events] == [
        "turn_started",
        "permission_request",
        "user_input_required",
        "turn_finished",
    ]


@pytest.mark.asyncio
async def test_terminal_session_passes_explicit_resume_mode():
    opened = {}

    class FakeTransport:
        async def hello(self, *, session_id, thread_id):
            return {"session_id": session_id, "thread_id": thread_id}

        async def open_session(self, **payload):
            opened.update(payload)
            return {"session_id": payload["session_id"], "history": []}

        async def close(self):
            return None

    session = TerminalSession(
        transport=FakeTransport(),
        session_id="existing",
        session_mode="resume",
    )

    response = await session.connect()

    assert opened["mode"] == "resume"
    assert response["history"] == []


@pytest.mark.asyncio
async def test_terminal_session_consumes_transport_end_sentinel():
    class FakeTransport:
        def send_message(self, *, session_id, content, request_id):
            async def _events():
                yield {"type": "turn_started", "data": {"turn": 1}}
                yield {"type": "turn_finished", "data": {"turn": 1}}
                yield {"type": "end", "data": {"status": "ok"}}

            return _events()

    session = TerminalSession(
        transport=FakeTransport(), session_id="s", thread_id="t"
    )

    events = [event async for event in session.send_message("run")]

    assert [event["type"] for event in events] == [
        "turn_started",
        "turn_finished",
    ]


def test_tui_modules_do_not_import_runtime_boundaries():
    forbidden = ("xbotv2.core", "langchain", "langgraph")

    for path in [
        Path("XBotv2/xbotv2/tui/client.py"),
        Path("XBotv2/xbotv2/tui/session_config.py"),
        Path("XBotv2/xbotv2/tui/terminal.py"),
        Path("XBotv2/xbotv2/tui/textual_state.py"),
        Path("XBotv2/xbotv2/tui/textual_theme.py"),
        Path("XBotv2/xbotv2/tui/textual_client.py"),
        Path("XBotv2/xbotv2/tui/textual_widgets.py"),
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        assert not any(name.startswith(forbidden) for name in imports)


def _frame(frame_type: str, payload: dict) -> dict:
    return {"type": frame_type, "data": payload}


# ----------------------------------------------------------------------
# Error visibility — v1.2 (§10.5.10)
# ----------------------------------------------------------------------


def test_tui_state_records_engine_error_event():
    """Engine-level ``error`` events (e.g. LangChain 400 on
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


def test_tui_state_renders_error_with_visible_label_in_lines():
    """When the transcript is rendered into plain lines (e.g. for
    snapshot tests or log capture), the error
    must be visible with a leading ``Error>`` marker — *not* buried
    as a normal message.
    """

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

    rendered = state.lines(width=120, height=30)
    flat = "\n".join(rendered)
    # The error must be visible in the transcript body, not just the
    # status row at the top.
    body_rows = "\n".join(rendered[3:-2])
    assert "Error>" in body_rows
    assert "Bad tool message order" in body_rows


@pytest.mark.asyncio
async def test_tui_renders_error_entry_with_error_css_class():
    """Headless TUI: when an engine ``error`` event lands, the
    transcript mounts an entry with classes ``"entry error"`` so the
    ``.error`` CSS rule (red meta + body) actually applies. This is
    the visible signal users get when a tool-call error happens
    (LangChain 400, sandbox rejection, etc.).

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

    from xbotv2.tui.textual_client import XBotTextualApp

    class _ErrorSession:
        def __init__(self):
            self.sent: list[str] = []

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message(self, text):
            self.sent.append(text)
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
    # Pre-create the tool so the request_id can be linked
    state.tools["call_1"] = TuiTool(tool_call_id="call_1", name="shell")
    state._changed_tool_ids.clear()

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
    # No separate notice entry is created
    notice_entries = [e for e in state.transcript if e.kind == "notice"]
    assert len(notice_entries) == 0


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


def test_apply_usage_with_delta_still_works():
    """The ``delta`` / ``total`` sub-key format (used by the older
    integration tests) must keep working.
    """

    state = TuiState()
    state.apply_event({"type": "turn_started", "data": {"turn": 1}})

    state.apply_event(
        {
            "type": "usage",
            "data": {
                "delta": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125, "requests": 1},
                "total": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125, "requests": 1},
            },
        }
    )

    assert state.usage["input_tokens"] == 100
    assert state.turn_usage["input_tokens"] == 100


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
