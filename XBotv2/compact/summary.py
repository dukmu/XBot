"""Summary prompt construction and normalization."""

from __future__ import annotations

from collections.abc import Sequence

from XBotv2.core import prompt_container, prompt_element
from XBotv2.core.messages import CompactionSummaryMessage, ConversationMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderMessage, ProviderSystem, ProviderUser
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.context_builder.contracts import BuiltContext, HistoryComponent


_SUMMARY_HEADING = "## Conversation Summary"
_TRUNCATION_MARKER = "\n\n[Middle of overlong summary omitted]\n\n"


def summary_request(
    messages: Sequence[ConversationMessage],
    max_chars: int,
    *,
    stable_prefix: ProviderMessage | Sequence[ProviderMessage] | None = None,
) -> list[ProviderMessage]:
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
        stable: tuple[ProviderMessage, ...] = ()
    elif isinstance(stable_prefix, (ProviderSystem, ProviderUser)):
        stable = (stable_prefix,)
    else:
        stable = tuple(stable_prefix)

    return [
        *stable,
        ProviderSystem(parts=(TextPart(text=prompt_element("summary_instructions", instruction)),)),
        *ContextBuilder.messages_from_components(
            BuiltContext([HistoryComponent(message) for message in messages])
        ),
        ProviderUser(parts=(TextPart(text=prompt_element("summary_request", "Produce the conversation summary now.")),)),
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


def compacted_message(summary: str, *, reason: str) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        id=f"summary-{abs(hash((summary, reason)))}",
        summary=prompt_container("historical_context", [prompt_element(
            "conversation_summary", summary, attributes={"reason": reason})],
            attributes={"source": "compaction"}),
    )


__all__ = [
    "compacted_message",
    "limit_summary",
    "normalize_summary",
    "strip_summary_heading",
    "summary_request",
]
