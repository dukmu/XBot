"""Tool failure taxonomy for XBot evaluation runs.

Failed ACP tool events carry only a transport status.  The semantic failure
— which tool returned an error and why — lives in the persisted session tool
messages.  This module classifies the session-side error messages so a run can
separate semantic tool failures from transport-level failures (a failed ACP
event with no matching tool message).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ERROR_JSON_RE = re.compile(
    r'<error encoding="json">\n(\{.*?\})\n</error>',
    re.DOTALL,
)
_TOOL_NOT_REGISTERED_RE = re.compile(r"Tool not registered: (\S+)")
_SANDBOX_EXIT_RE = re.compile(r"Sandbox command failed with exit code (\d+)")
_MESSAGE_TEXT_LIMIT = 500
_EXAMPLE_LIMIT = 10


def tool_error_kind(message: dict[str, Any]) -> str:
    """Classify one persisted error tool message into a coarse failure kind.

    Prefers the structured ``<error encoding="json">`` code; falls back to
    recognizable message patterns and finally ``unknown``.
    """
    text = _message_text(message)
    match = _ERROR_JSON_RE.search(text)
    if match is not None:
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            data = None
        code = (data or {}).get("code")
        if isinstance(code, str) and code:
            return f"error:{code}"
    if _TOOL_NOT_REGISTERED_RE.search(text):
        return "tool_not_registered"
    if _SANDBOX_EXIT_RE.search(text):
        return "sandbox_command_failed"
    return "unknown"


@dataclass(frozen=True)
class FailureStats:
    """Aggregated semantic tool failures from one evaluation run."""

    total: int
    by_tool: dict[str, int]
    by_kind: dict[str, int]
    examples: list[dict[str, Any]]


def analyze_failures(run_root: Path) -> FailureStats:
    """Aggregate error tool messages from a run's session state.

    ``run_root`` is the result directory (``evaluation/results/<name>``);
    session state is read from ``<run_root>/data/xbot/sessions``.
    """
    by_tool: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    session_root = run_root / "data" / "xbot" / "sessions"
    for path in sorted(
        session_root.glob("*/threads/*/state/messages.jsonl")
    ):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if (
                not isinstance(message, dict)
                or message.get("role") != "tool"
                or message.get("status") != "error"
            ):
                continue
            name = str(message.get("name") or "?")
            kind = tool_error_kind(message)
            by_tool[name] += 1
            by_kind[kind] += 1
            if len(examples) < _EXAMPLE_LIMIT:
                examples.append({
                    "tool": name,
                    "kind": kind,
                    "message": _message_text(message)[:_MESSAGE_TEXT_LIMIT],
                })
    return FailureStats(
        total=sum(by_tool.values()),
        by_tool=dict(by_tool),
        by_kind=dict(by_kind),
        examples=examples,
    )


def _message_text(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    return "\n".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


__all__ = ["FailureStats", "analyze_failures", "tool_error_kind"]
