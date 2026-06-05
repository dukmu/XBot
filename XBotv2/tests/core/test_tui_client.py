"""Tests for protocol-driven TUI client state."""

import ast
import argparse
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import xbotv2.__main__ as xbot_main
from xbotv2.protocol.frames import ProtocolFrame
from xbotv2.tui.client import CursesTuiClient, TuiState
from xbotv2.tui.textual_state import (
    queue_user_message,
    render_transcript_entry,
    route_submitted_text,
)


def test_tui_state_applies_protocol_frames_and_renders_lines():
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
        _frame("turn_finished", {"turn": 1}),
    ]

    for frame in frames:
        state.apply_frame(frame)

    assert state.status == "Ready"
    assert state.agent_name == "TestBot"
    assert state.messages[-1].content == "hello world"
    assert state.tools["call_1"].status == "success"
    assert state.tools["call_1"].summary == "cached result"
    assert state.notices[-1].kind == "client_message"

    rendered = "\n".join(state.lines(width=80, height=12))
    assert "TestBot> hello world" in rendered
    assert "Tool filesystem_read [success]" in rendered
    assert "cached result" in rendered
    assert "Notice> heads up" in rendered


def test_tui_state_turn_finished_preserves_waiting_for_user():
    state = TuiState()

    state.apply_frame(_frame("turn_started", {"turn": 1}))
    state.apply_frame(
        _frame("user_input_required", {"question": "Proceed?", "options": ["yes", "no"]})
    )
    state.apply_frame(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Waiting for user"
    assert state.notices[-1].kind == "user_input_required"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Question> Proceed? Options: yes, no" in rendered


def test_tui_state_turn_finished_preserves_permission_states():
    state = TuiState()

    state.apply_frame(_frame("turn_started", {"turn": 1}))
    state.apply_frame(_frame("permission_request", {"reason": "approval needed"}))
    state.apply_frame(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Approval required"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Approval> approval needed" in rendered

    state.apply_frame(_frame("turn_started", {"turn": 2}))
    state.apply_frame(_frame("permission_denied", {"reason": "approval denied"}))
    state.apply_frame(_frame("turn_finished", {"turn": 2}))

    assert state.status == "Permission denied"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Denied> approval denied" in rendered


def test_tui_state_renders_interaction_response_acknowledgements():
    state = TuiState()

    state.apply_frame(_frame("user_input_required", {"question": "Proceed?"}))
    state.apply_frame(_frame("user_input_recorded", {"request_id": "user_input:c1"}))

    assert state.status == "Ready"
    assert state.notices[-1].kind == "user_input_recorded"
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Answer> user_input:c1" in rendered

    state.apply_frame(_frame("permission_request", {"request_id": "permission:c2"}))
    assert state.pending_permission_request_id == "permission:c2"
    state.apply_frame(
        _frame(
            "permission_response_recorded",
            {"request_id": "permission:c2", "decision": "allow"},
        )
    )

    assert state.status == "Ready"
    assert state.pending_permission_request_id is None
    rendered = "\n".join(state.lines(width=80, height=8))
    assert "Approval> permission:c2: allow" in rendered


def test_tui_state_ack_keeps_running_until_turn_finished():
    state = TuiState()

    state.apply_frame(_frame("turn_started", {"turn": 1}))
    state.apply_frame(_frame("user_input_required", {"request_id": "user_input:c1"}))
    state.apply_frame(_frame("user_input_recorded", {"request_id": "user_input:c1"}))

    assert state.status == "Running"
    assert state.pending_user_input_request_id is None

    state.apply_frame(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Ready"


def test_tui_state_permission_denied_resets_on_next_turn():
    state = TuiState()

    state.apply_frame(_frame("turn_started", {"turn": 1}))
    state.apply_frame(_frame("permission_denied", {"reason": "no"}))
    state.apply_frame(_frame("turn_finished", {"turn": 1}))

    assert state.status == "Permission denied"

    state.apply_frame(_frame("turn_started", {"turn": 2}))

    assert state.status == "Running"


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
    assert first.plain == "You\n你好 [不要解析] 中文\n"
    assert second.plain == "助手\n收到：中文正常显示\n"


def test_curses_client_drains_background_events_without_curses():
    client = CursesTuiClient()
    client._events.put({"type": "assistant_message", "data": {"content": "live"}})

    client._drain_events()

    assert client.state.messages[-1].content == "live"


def test_curses_client_forwards_no_plugin_mode_to_terminal_session():
    client = CursesTuiClient(no_plugins=True)

    assert client.session._no_plugins is True


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


def test_mode_curses_uses_legacy_curses_client():
    args = argparse.Namespace(
        data_dir="data",
        personality="default",
        provider="default",
        no_plugins=True,
    )

    with patch("xbotv2.tui.client.CursesTuiClient") as client_cls:
        client = client_cls.return_value
        client.run = AsyncMock()
        xbot_main._run_curses(args)

    client_cls.assert_called_once()
    client.run.assert_awaited_once()


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
    assert await permission_decisions.get() == "allow"
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

        async def send_message_with_input(self, text, input_provider=None, permission_provider=None):
            del input_provider, permission_provider
            yield {"type": "turn_started", "data": {"turn": 1}}
            yield {"type": "assistant_message", "data": {"content": f"回复：{text}"}}
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        data_dir="data",
        personality_id="default",
        provider_name="mock",
        session_id="s",
        thread_id="t",
        no_plugins=True,
    )
    app.session = FakeSession()

    async with app.run_test(headless=True, size=(100, 32)) as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        await app.on_input_submitted(input_widget.Submitted(input_widget, "你好"))
        await pilot.pause()

    assert [(message.role, message.content) for message in app.state.messages] == [
        ("user", "你好"),
        ("assistant", "回复：你好"),
    ]
    assert app.state.status == "Ready"


def test_curses_client_records_reader_errors():
    client = CursesTuiClient()
    client._events.put(RuntimeError("reader failed"))

    client._drain_events()

    assert client.state.status == "Error"
    assert client.state.errors == ["reader failed"]


def test_curses_client_routes_text_to_live_user_input_queue():
    client = CursesTuiClient()
    client._loop = object()
    client.state.apply_event({
        "type": "user_input_required",
        "data": {"request_id": "user_input:c1", "question": "Proceed?"},
    })

    client._send_text("yes")

    assert client._answers.get_nowait() == "yes"
    assert client.state.messages == []
    assert client._pending == set()


def test_curses_client_routes_text_to_live_permission_queue():
    client = CursesTuiClient()
    client._loop = object()
    client.state.apply_event({
        "type": "permission_request",
        "data": {"request_id": "permission:c1", "reason": "approve?"},
    })

    client._send_text("yes")

    assert client._permission_decisions.get_nowait() == "allow"
    assert client.state.messages == []
    assert client._pending == set()


@pytest.mark.asyncio
async def test_curses_client_marks_ready_after_session_connect():
    client = CursesTuiClient()
    client.session.connect = AsyncMock()
    client.session.disconnect = AsyncMock()

    async def fake_to_thread(func, *args, **kwargs):
        func(*args, **kwargs)

    with patch("xbotv2.tui.client.curses.wrapper") as wrapper, \
            patch("xbotv2.tui.client.asyncio.to_thread", fake_to_thread):
        await client.run()

    wrapper.assert_called_once()
    assert client.state.status == "Ready"


def test_tui_modules_do_not_import_runtime_boundaries():
    forbidden = ("xbotv2.core", "langchain", "langgraph")

    for path in [
        Path("XBotv2/xbotv2/tui/client.py"),
        Path("XBotv2/xbotv2/tui/terminal.py"),
        Path("XBotv2/xbotv2/tui/textual_state.py"),
        Path("XBotv2/xbotv2/tui/textual_client.py"),
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        assert not any(name.startswith(forbidden) for name in imports)


def _frame(frame_type: str, payload: dict) -> ProtocolFrame:
    return ProtocolFrame(
        seq=1,
        direction="server_to_client",
        type=frame_type,
        session_id="s",
        thread_id="t",
        request_id="req",
        payload=payload,
    )
