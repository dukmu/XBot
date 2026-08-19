"""Summary prompt, normalization, and auxiliary-model helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from XBotv2.core import (
    MESSAGE_FORMAT_KEY,
    Message,
    prompt_container,
    prompt_element,
)


_SUMMARY_HEADING = "## Conversation Summary"
_TRUNCATION_MARKER = "\n\n[Middle of overlong summary omitted]\n\n"


def summary_request(
    messages: Sequence[Message],
    max_chars: int,
    *,
    stable_prefix: Message | Sequence[Any] | None = None,
) -> list[Any]:
    instruction = (
        "Summarize the supplied older conversation for future continuation. "
        "Preserve the current objective and constraints, human corrections, accepted "
        "decisions, verified state and essential evidence, unresolved problems, known "
        "unknowns, and remaining work. Distinguish verified facts and completed work "
        "from plans or unverified claims. Preserve still-relevant information from any "
        "prior <historical_context source=\"compaction\"> summary and merge it with the "
        "newer supplied history. The leading stable system context is reference context: "
        "do not restate generic core/runtime boilerplate in the summary. Omit repetition, "
        "superseded discussion, raw logs, and recoverable detail. Do not continue the "
        "task or call tools. Return only concise Markdown using no more than "
        f"{max_chars} characters."
    )
    if stable_prefix is None:
        stable: tuple[Any, ...] = ()
    elif isinstance(stable_prefix, Message):
        stable = (stable_prefix,)
    else:
        stable = tuple(stable_prefix)

    return [
        *stable,
        Message(
            role="system",
            content=prompt_element("summary_instructions", instruction),
        ),
        *messages,
        Message(
            role="user",
            content=prompt_element(
                "summary_request",
                "Produce the conversation summary now.",
            ),
        ),
    ]


def normalize_summary(summary: str, max_chars: int) -> tuple[str, bool]:
    summary = strip_summary_heading(summary.strip())
    if not summary:
        raise RuntimeError("Compaction model returned an empty summary")
    return limit_summary(summary, max_chars)


def strip_summary_heading(summary: str) -> str:
    while summary.startswith(_SUMMARY_HEADING):
        summary = summary[len(_SUMMARY_HEADING):].lstrip(" \r\n")
    return summary


def limit_summary(summary: str, max_chars: int) -> tuple[str, bool]:
    """Hard-limit summary length for every positive ``max_chars`` value."""
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    if len(summary) <= max_chars:
        return summary, False
    if max_chars <= len(_TRUNCATION_MARKER):
        return summary[:max_chars], True

    remaining = max_chars - len(_TRUNCATION_MARKER)
    head = remaining * 2 // 3
    tail = remaining - head
    limited = (
        summary[:head].rstrip()
        + _TRUNCATION_MARKER
        + summary[-tail:].lstrip()
    )
    # rstrip/lstrip can only shorten the result, but keep the hard contract
    # explicit if the marker changes later.
    return limited[:max_chars], True


def compacted_message(summary: str, *, reason: str) -> Message:
    return Message(
        role="system",
        content=prompt_container(
            "historical_context",
            [
                prompt_element(
                    "conversation_summary",
                    summary,
                    attributes={"reason": reason},
                ),
            ],
            attributes={"source": "compaction"},
        ),
        additional_kwargs={MESSAGE_FORMAT_KEY: "xml"},
    )


def model_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(
            usage.get("total_tokens") or input_tokens + output_tokens
        ),
        "context_tokens": int(usage.get("context_tokens") or input_tokens),
    }
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if usage.get(key) is not None:
            result[key] = int(usage[key])
    return result


async def invoke_llm(llm: Any, messages: list[Any]) -> Any:
    """Run one unbound auxiliary model call for compaction."""
    from XBotv2.core.messages import merge_model_chunk

    aggregate: Any = None
    async for chunk in llm.astream(messages):
        aggregate = merge_model_chunk(aggregate, chunk)
    if aggregate is None:
        raise RuntimeError("Compaction model produced no response")
    return aggregate


__all__ = [
    "compacted_message",
    "invoke_llm",
    "limit_summary",
    "model_usage",
    "normalize_summary",
    "strip_summary_heading",
    "summary_request",
]
