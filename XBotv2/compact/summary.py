"""Summary prompt construction and normalization."""

from __future__ import annotations

from collections.abc import Sequence

from XBotv2.core import prompt_element
from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import CompactionSummaryMessage, ConversationMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderMessage, ProviderSystem, ProviderUser
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.context_builder.contracts import BuiltContext, HistoryComponent


_SUMMARY_HEADING = "## Conversation Summary"


def summary_request(
    messages: Sequence[ConversationMessage],
    max_chars: int,
    *,
    stable_prefix: ProviderMessage | Sequence[ProviderMessage] | None = None,
    artifacts: ArtifactStorePort | None = None,
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
            BuiltContext([HistoryComponent(message) for message in messages]),
            artifacts=artifacts,
        ),
        ProviderUser(parts=(TextPart(text=prompt_element("summary_request", "Produce the conversation summary now.")),)),
    ]


def normalize_summary(summary: str) -> str:
    """Return the model's summary text with any heading it echoed removed."""
    summary = strip_summary_heading(summary.strip())
    if not summary:
        raise RuntimeError("Compaction model returned an empty summary")
    return summary


def strip_summary_heading(summary: str) -> str:
    while summary.startswith(_SUMMARY_HEADING):
        summary = summary[len(_SUMMARY_HEADING):].lstrip(" \r\n")
    return summary


def compacted_message(summary: str, *, reason: str) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        id=f"summary-{abs(hash((summary, reason)))}",
        summary=summary,
    )


__all__ = [
    "compacted_message",
    "normalize_summary",
    "strip_summary_heading",
    "summary_request",
]
