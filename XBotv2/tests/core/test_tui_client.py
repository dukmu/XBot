"""Tests for protocol-driven TUI client state."""

import ast
from pathlib import Path

from xbotv2.protocol.frames import ProtocolFrame
from xbotv2.tui.client import CursesTuiClient, TuiState


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
        _frame("turn_finished", {"turn": 1}),
    ]

    for frame in frames:
        state.apply_frame(frame)

    assert state.status == "Ready"
    assert state.agent_name == "TestBot"
    assert state.messages[-1].content == "hello world"
    assert state.tools["call_1"].status == "success"
    assert state.tools["call_1"].summary == "cached result"

    rendered = "\n".join(state.lines(width=80, height=12))
    assert "TestBot> hello world" in rendered
    assert "Tool filesystem_read [success]" in rendered
    assert "cached result" in rendered


def test_curses_client_drains_background_events_without_curses():
    client = CursesTuiClient()
    client._events.put({"type": "assistant_message", "data": {"content": "live"}})

    client._drain_events()

    assert client.state.messages[-1].content == "live"


def test_curses_client_records_reader_errors():
    client = CursesTuiClient()
    client._events.put(RuntimeError("reader failed"))

    client._drain_events()

    assert client.state.status == "Error"
    assert client.state.errors == ["reader failed"]


def test_tui_modules_do_not_import_runtime_boundaries():
    forbidden = ("xbotv2.core", "langchain", "langgraph")

    for path in [
        Path("XBotv2/xbotv2/tui/client.py"),
        Path("XBotv2/xbotv2/tui/terminal.py"),
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
