"""Goal evaluation: the auxiliary model call and its verdict contract.

Mirrors Claude Code's ``/goal`` evaluator, which is a session-scoped
prompt-based Stop hook: after each turn a separate model judges the condition
against the conversation so far and returns one of three verdicts with a
reason. It never calls tools and never reads files.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from XBotv2.core import Message, prompt_container, prompt_element
from XBotv2.goal.models import GOAL_VERDICT_VALUES, GoalVerdict

_TRANSCRIPT_MAX_CHARS = 48_000
_MESSAGE_MAX_CHARS = 4_000
_REASON_MAX_CHARS = 500
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_EVALUATION_INSTRUCTIONS = (
    "You judge whether a completion condition has been met. You cannot run "
    "commands, read files, or call tools, so judge only what the supplied "
    "transcript demonstrates. Answer with a single JSON object and nothing "
    "else:\n"
    '{"verdict": "met" | "not_yet_met" | "impossible", "reason": "<one short sentence>"}\n'
    "Use met only when the transcript shows the condition holds. Use "
    "not_yet_met when the work is unfinished or unproven. Use impossible only "
    "when the condition can never be satisfied. Never treat an intention, a "
    "plan, or a summary as evidence."
)


class GoalEvaluationError(RuntimeError):
    """The evaluator call failed or returned no usable verdict."""


def evaluation_request(condition: str, transcript: str) -> list[Message]:
    """Build the one-shot evaluator request envelope."""
    return [
        Message(
            role="system",
            content=prompt_element(
                "goal_evaluation_instructions", _EVALUATION_INSTRUCTIONS
            ),
        ),
        Message(
            role="user",
            content=prompt_container("goal_evaluation", [
                prompt_element("completion_condition", condition),
                prompt_element("conversation", transcript),
            ]),
        ),
    ]


def render_transcript(
    messages: Sequence[Message],
    *,
    max_chars: int = _TRANSCRIPT_MAX_CHARS,
) -> str:
    """Render the conversation as an evaluator-readable transcript."""
    entries: list[str] = []
    for message in messages:
        content = str(message.content or "").strip()
        if len(content) > _MESSAGE_MAX_CHARS:
            content = f"{content[:_MESSAGE_MAX_CHARS]}\n[truncated]"
        entry = f"[{message.role}]"
        tool_names = [call.name for call in message.tool_calls or () if call.name]
        if tool_names:
            entry += f" (tool calls: {', '.join(tool_names)})"
        if content:
            entry += f"\n{content}"
        entries.append(entry)
    text = "\n\n".join(entries)
    if len(text) > max_chars:
        text = "[earlier conversation omitted]\n\n" + text[-max_chars:]
    return text


def parse_verdict(content: str) -> GoalVerdict:
    """Parse one evaluator answer, rejecting anything malformed.

    A malformed answer is an evaluation failure, not a silent default: guessing
    ``not_yet_met`` here would keep an unsatisfiable goal looping forever.
    """
    text = str(content or "").strip()
    if not text:
        raise GoalEvaluationError("evaluator returned an empty answer")
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise GoalEvaluationError("evaluator returned no JSON verdict")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise GoalEvaluationError(f"evaluator verdict is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GoalEvaluationError("evaluator verdict must be a JSON object")
    verdict = payload.get("verdict")
    if verdict not in GOAL_VERDICT_VALUES:
        raise GoalEvaluationError(
            f"evaluator returned an unknown verdict: {verdict!r}"
        )
    reason = str(payload.get("reason") or "").strip()
    return GoalVerdict(verdict=verdict, reason=reason[:_REASON_MAX_CHARS])


__all__ = [
    "GoalEvaluationError",
    "evaluation_request",
    "parse_verdict",
    "render_transcript",
]
