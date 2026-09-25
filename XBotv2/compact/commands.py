"""Human-facing /compact command."""

from __future__ import annotations

from typing import Protocol

from XBotv2.commands import CommandResult
from XBotv2.compact.protocol import CompactionMetrics


class _CompactCommandOwner(Protocol):
    async def _compact_current_history(
        self,
    ) -> tuple[bool, CompactionMetrics | None]: ...


def compact_result_message(metrics: CompactionMetrics | None) -> str:
    if metrics is None:
        return "Conversation history compacted."
    usage = metrics.model_usage
    return (
        "Conversation history compacted "
        f"from about {metrics.context_tokens_before} to "
        f"{metrics.context_tokens_after_estimate} context tokens; "
        f"summary model used {usage.input} input and "
        f"{usage.output} output tokens."
    )


async def run_compact_command(
    owner: _CompactCommandOwner,
    raw_args: str,
) -> CommandResult:
    if raw_args.strip():
        return CommandResult(
            "Usage: /compact",
            status="error",
        )

    try:
        result, metrics = await owner._compact_current_history()
    except Exception as exc:
        return CommandResult(
            f"Conversation compaction failed: {exc}",
            status="error",
        )

    if not result:
        return CommandResult(
            "Conversation history is too short to compact.",
        )

    return CommandResult(
        compact_result_message(metrics),
        effects=("history", "thread"),
    )


__all__ = ["compact_result_message", "run_compact_command"]
