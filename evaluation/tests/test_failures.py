"""Failure taxonomy tests for XBot evaluation runs."""

from __future__ import annotations

import json
from pathlib import Path

from xbot_eval.failures import analyze_failures, tool_error_kind


def _tool_message(name: str, text: str) -> dict[str, object]:
    return {
        "role": "tool",
        "status": "error",
        "name": name,
        "parts": [{"type": "text", "text": text}],
    }


def test_tool_error_kind_prefers_structured_error_code():
    message = _tool_message(
        "shell",
        '<tool_result name="shell" status="error">\n'
        "<content>x</content>\n"
        '<error encoding="json">\n'
        '{"code": "command_failed", "details": {}, "message": "boom"}\n'
        "</error>\n</tool_result>",
    )

    assert tool_error_kind(message) == "error:command_failed"


def test_tool_error_kind_recognizes_unregistered_tool():
    message = _tool_message(
        "write",
        '<tool_result name="write" status="error">\n'
        "<content>Error: Tool not registered: write</content>\n"
        "</tool_result>",
    )

    assert tool_error_kind(message) == "tool_not_registered"


def test_analyze_failures_aggregates_session_state(tmp_path: Path):
    state_dir = (
        tmp_path
        / "run"
        / "data"
        / "xbot"
        / "sessions"
        / "s1"
        / "threads"
        / "agent"
        / "state"
    )
    state_dir.mkdir(parents=True)
    messages = [
        _tool_message(
            "shell",
            '<tool_result name="shell" status="error">\n<content>x</content>\n'
            '<error encoding="json">\n'
            '{"code": "command_failed", "details": {}, "message": "boom"}\n'
            "</error>\n</tool_result>",
        ),
        _tool_message(
            "write",
            '<tool_result name="write" status="error">\n'
            "<content>Error: Tool not registered: write</content>\n"
            "</tool_result>",
        ),
        {
            "role": "user",
            "parts": [{"type": "text", "text": "not a tool"}],
        },
    ]
    (state_dir / "messages.jsonl").write_text(
        "\n".join(json.dumps(item) for item in messages) + "\n",
        encoding="utf-8",
    )

    stats = analyze_failures(tmp_path / "run")

    assert stats.total == 2
    assert stats.by_tool == {"shell": 1, "write": 1}
    assert stats.by_kind == {
        "error:command_failed": 1,
        "tool_not_registered": 1,
    }
    assert len(stats.examples) == 2
