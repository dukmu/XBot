"""Tests for protocol-driven TUI client state."""

import ast
import argparse
import asyncio
import html
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import xbotv2.__main__ as xbot_main
from xbotv2.protocol.frames import ProtocolFrame
from xbotv2.tui.client import CursesTuiClient, TuiState, _parse_permission_decision
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
        _frame("usage", {"total": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "requests": 1}}),
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
    assert state.usage["total_tokens"] == 15

    rendered = "\n".join(state.lines(width=80, height=12))
    assert "TestBot> hello world" in rendered
    assert "Tool filesystem_read [success]" in rendered
    assert "cached result" in rendered
    assert "Notice> heads up" in rendered
    assert "Tokens 15" in rendered


def test_tui_state_applies_usage_totals():
    state = TuiState()

    state.apply_frame(_frame("usage", {"total": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "requests": 2}}))

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

    assert state.messages == []
    assert state.tools["call_1"].name == "shell"
    assert len(state.transcript) == 1
    assert state.transcript[0].kind == "tool"
    assert state.transcript[0].key == "call_1"


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
    assert "Question> Proceed?" in rendered
    assert "Options:" not in rendered


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
async def test_protocol_trace_records_unicode_frames(tmp_path, monkeypatch):
    from xbotv2.tui.terminal import ProtocolClient

    class FakeStdin:
        def __init__(self):
            self.written = b""

        def write(self, data):
            self.written += data

        async def drain(self):
            return None

    class FakeStdout:
        def __init__(self, frame):
            self._line = frame.to_json_line().encode("utf-8")

        async def readline(self):
            line = self._line
            self._line = b""
            return line

    class FakeProcess:
        def __init__(self, frame):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout(frame)

    trace_path = tmp_path / "protocol-trace.jsonl"
    monkeypatch.setenv("XBOTV2_TUI_TRACE", str(trace_path))
    frame = ProtocolFrame(
        seq=1,
        direction="server_to_client",
        type="assistant_message",
        session_id="s",
        thread_id="t",
        request_id="",
        payload={"content": "收到：当前磁盘用了多少"},
    )
    client = ProtocolClient([])
    client._process = FakeProcess(frame)

    await client.send(
        "user.message",
        "s",
        "t",
        {"content": "当前磁盘用了多少"},
    )
    received = await client.read_frame()

    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert received is not None
    assert [record["stage"] for record in records] == ["protocol.send", "protocol.recv"]
    assert records[0]["payload"]["frame"]["payload"]["content"] == "当前磁盘用了多少"
    assert records[1]["payload"]["frame"]["payload"]["content"] == "收到：当前磁盘用了多少"


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

        async def send_message_with_input(self, text, input_provider=None, permission_provider=None):
            del text, input_provider, permission_provider
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
        input_widget.load_text("hello")
        await app.submit_composer()
        await pilot.pause()
        status = app.query_one("#status_bar").content
        assert "usage req:1 in:12 out:8 total:20" in str(status)


@pytest.mark.asyncio
async def test_textual_app_headless_renders_inline_permission_options():
    from textual.widgets import Button
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        permission_decision = None

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message_with_input(self, text, input_provider=None, permission_provider=None):
            del text, input_provider
            yield {"type": "turn_started", "data": {"turn": 1}}
            payload = {
                "request_id": "permission:c1",
                "source": "permission_system",
                "reason": "Permission approval required for tool: shell.",
            }
            yield {
                "type": "permission_request",
                "data": payload,
            }
            parsed = permission_provider(payload)
            if hasattr(parsed, "__await__"):
                parsed = await parsed
            self.permission_decision = parsed
            yield {
                "type": "permission_response_recorded",
                "data": {
                    "request_id": "permission:c1",
                    "decision": parsed["decision"],
                    "scope": parsed["scope"],
                },
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        data_dir="data",
        personality_id="default",
        provider_name="mock",
        session_id="s",
        thread_id="t",
        no_plugins=True,
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
        assert app._active_choice_key == "0"
        assert "Allow" in str(app._choice_widgets["0"].content)
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
        app.state.apply_event({
            "type": "permission_request",
            "data": {
                "request_id": "permission:dup",
                "reason": "Permission approval required for tool: shell.",
            },
        })
        await app._render_new_transcript_entries()
        await pilot.pause()

        assert await app.confirm_active_choice() is True
        assert await app.confirm_active_choice() is False

    assert await app._permission_decisions.get() == {
        "decision": "allow",
        "scope": "once",
    }
    assert app._permission_decisions.empty()
    assert [
        notice.kind for notice in app.state.notices
        if notice.kind == "Approval queued"
    ] == ["Approval queued"]


@pytest.mark.asyncio
async def test_textual_app_headless_renders_inline_ask_user_options():
    from textual.widgets import Button
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        answer = None

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message_with_input(self, text, input_provider=None, permission_provider=None):
            del text, permission_provider
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
            answer = input_provider(payload)
            if hasattr(answer, "__await__"):
                answer = await answer
            self.answer = answer
            yield {
                "type": "user_input_recorded",
                "data": {"request_id": "user_input:c1", "status": "recorded"},
            }
            yield {"type": "turn_finished", "data": {"turn": 1}}

    app = XBotTextualApp(
        data_dir="data",
        personality_id="default",
        provider_name="mock",
        session_id="s",
        thread_id="t",
        no_plugins=True,
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

    assert session.answer == "继续"


@pytest.mark.asyncio
async def test_textual_app_replays_tool_permission_sequence_without_swallowing_messages():
    from xbotv2.tui.textual_client import XBotTextualApp

    class FakeSession:
        permission_decision = None

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_message_with_input(self, text, input_provider=None, permission_provider=None):
            del input_provider
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
                "reason": "Permission approval required for tool: shell.",
            }
            yield {"type": "permission_request", "data": payload}
            parsed = permission_provider(payload)
            if hasattr(parsed, "__await__"):
                parsed = await parsed
            self.permission_decision = parsed
            yield {
                "type": "permission_response_recorded",
                "data": {
                    "request_id": "permission:shell",
                    "decision": parsed["decision"],
                    "scope": parsed["scope"],
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

    app = XBotTextualApp(
        data_dir="data",
        personality_id="default",
        provider_name="mock",
        session_id="s",
        thread_id="t",
        no_plugins=True,
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
        await pilot.press("down")
        await pilot.press("enter")
        for _ in range(3):
            await pilot.pause()
        rendered = html.unescape(app.export_screenshot(title="xbotv2-tui-replay")).replace("\xa0", " ")

    assert session.permission_decision == {"decision": "allow", "scope": "always"}
    assert [(message.role, message.content.strip()) for message in app.state.messages] == [
        ("user", "当前磁盘用了多少"),
        ("assistant", "当前磁盘使用情况：已执行 df -h。问题是：当前磁盘用了多少"),
    ]
    assert rendered.count("当前磁盘用了多少") >= 2
    assert "当前磁盘使用情况" in rendered
    assert "Filesystem" in rendered
    assert rendered.count("approval queued") == 1


@pytest.mark.asyncio
async def test_textual_composer_history_and_multiline_resize():
    from textual.events import Key
    from xbotv2.tui.textual_client import XBotTextualApp

    app = XBotTextualApp(
        data_dir="data",
        personality_id="default",
        provider_name="mock",
        session_id="s",
        thread_id="t",
        no_plugins=True,
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

    assert client._permission_decisions.get_nowait() == {"decision": "allow", "scope": "once"}
    assert client.state.messages == []
    assert client._pending == set()


def test_permission_decision_parser_supports_scopes():
    assert _parse_permission_decision("session allow") == {
        "decision": "allow",
        "scope": "session",
    }
    assert _parse_permission_decision("deny always") == {
        "decision": "deny",
        "scope": "always",
    }


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
